"""
Recebe eventos de ciclo de vida do usuário no IdP e bloqueia/libera o acesso
dele no LiteLLM, sem apagá-lo (apagar perde o histórico de spend/logs
vinculado ao user_id) e sem mexer no orçamento (max_budget). É o mesmo papel
do "Lambda worker" do desenho de produção (que reage a change notification do
Microsoft Graph) — aqui, sem IdP corporativo disponível pro PoC, o evento é
disparado manualmente (curl).

Eventos:
  POST /events/user-disabled  suspensão reversível (férias, licença)
  POST /events/user-deleted   desligamento definitivo
  POST /events/user-enabled   volta da suspensão

LiteLLM_UserTable não tem flag de bloqueio (só as keys têm), então
"desabilitar" é:
  1. /user/update com metadata.deprovisioned=true (o auth-bridge recusa login
     de UI pra esse usuário) e metadata.deprovision_state
  2. /key/block nas virtual keys do usuário que ainda não estavam bloqueadas;
     os hashes bloqueados por este bridge ficam em metadata.blocked_key_hashes
     pra reativação desbloquear só o que foi bloqueado aqui (uma key bloqueada
     manualmente antes continua bloqueada)

max_budget nunca é alterado: o teto continua do usuário e, no desligamento,
o gestor decide a redistribuição olhando metadata.budget_snapshot.

user_id no LiteLLM = e-mail (mesma convenção do auth-bridge).
"""

import os
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
ADMIN_HEADERS = {"Authorization": f"Bearer {MASTER_KEY}"}
KEY_LIST_PAGE_SIZE = 100

STATE_SUSPENDED = "suspended"
STATE_OFFBOARDED = "offboarded"

# chaves de metadata geridas por este bridge, removidas na reativação.
# "deprovisioned" é a flag lida pelo auth-bridge.
MANAGED_METADATA_KEYS = (
    "deprovisioned",
    "deprovision_state",
    "deprovisioned_at",
    "blocked_key_hashes",
)

app = FastAPI()
client = httpx.AsyncClient(base_url=LITELLM_BASE_URL, headers=ADMIN_HEADERS, timeout=30.0)


class UserEvent(BaseModel):
    email: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def raise_for_litellm(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)


async def get_user_info(user_id: str) -> dict:
    info = await client.get("/user/info", params={"user_id": user_id})
    raise_for_litellm(info)
    user_info = info.json().get("user_info")
    if not user_info:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")
    return user_info


async def update_metadata(user_id: str, metadata: dict) -> None:
    # /user/update substitui metadata inteiro: quem chama manda o dict completo.
    resp = await client.post("/user/update", json={"user_id": user_id, "metadata": metadata})
    raise_for_litellm(resp)


async def list_user_key_hashes(user_id: str) -> list[str]:
    hashes: list[str] = []
    page = 1
    while True:
        resp = await client.get(
            "/key/list",
            params={"user_id": user_id, "page": page, "size": KEY_LIST_PAGE_SIZE},
        )
        raise_for_litellm(resp)
        body = resp.json()
        hashes.extend(body.get("keys") or [])
        if page >= (body.get("total_pages") or 1):
            return hashes
        page += 1


async def is_key_blocked(key_hash: str) -> bool:
    resp = await client.get("/key/info", params={"key": key_hash})
    raise_for_litellm(resp)
    return bool((resp.json().get("info") or {}).get("blocked"))


async def set_keys_blocked(key_hashes: list[str], blocked: bool, tolerate_missing: bool = False) -> None:
    path = "/key/block" if blocked else "/key/unblock"
    for key_hash in key_hashes:
        resp = await client.post(path, json={"key": key_hash})
        # key apagada entre o bloqueio e a reativação: nada a desbloquear.
        if tolerate_missing and resp.status_code == 404:
            continue
        raise_for_litellm(resp)


async def deprovision_user(user_id: str, requested_state: str) -> tuple[str, int]:
    user_info = await get_user_info(user_id)
    metadata = dict(user_info.get("metadata") or {})
    now = now_iso()

    # desligamento é terminal: um "disabled" tardio não rebaixa pra suspenso.
    state = STATE_OFFBOARDED if STATE_OFFBOARDED in (requested_state, metadata.get("deprovision_state")) else STATE_SUSPENDED
    owned = list(metadata.get("blocked_key_hashes") or [])

    metadata.update(
        {
            "deprovisioned": True,
            "deprovision_state": state,
            "deprovisioned_at": metadata.get("deprovisioned_at") or now,
            "blocked_key_hashes": owned,
        }
    )
    if state == STATE_OFFBOARDED and "budget_snapshot" not in metadata:
        metadata["budget_snapshot"] = {
            "max_budget": user_info.get("max_budget"),
            "spend": user_info.get("spend"),
            "captured_at": now,
        }

    # 1) flag antes de listar as keys: login concorrente já é recusado pelo
    #    auth-bridge e não gera key fora da lista.
    await update_metadata(user_id, metadata)

    # 2) separa o que é nosso: key já bloqueada por outro motivo não entra na
    #    lista (reativar não pode liberar o que ninguém pediu pra liberar).
    #    Hash já registrado numa execução anterior continua sendo nosso, então
    #    repetir o evento (retry) é idempotente.
    key_hashes = await list_user_key_hashes(user_id)
    new_keys = [h for h in key_hashes if h not in owned and not await is_key_blocked(h)]
    to_block = [h for h in key_hashes if h in owned or h in new_keys]

    # 3) registra antes de bloquear: se cair no meio, a reativação desbloqueia
    #    uma key a mais (inofensivo) em vez de esquecer uma bloqueada.
    if new_keys:
        metadata["blocked_key_hashes"] = owned + new_keys
        await update_metadata(user_id, metadata)

    await set_keys_blocked(to_block, blocked=True)
    return state, len(to_block)


async def enable_user(user_id: str) -> int:
    user_info = await get_user_info(user_id)
    metadata = dict(user_info.get("metadata") or {})

    if not metadata.get("deprovisioned"):
        return 0
    if metadata.get("deprovision_state") == STATE_OFFBOARDED:
        raise HTTPException(status_code=409, detail=f"user {user_id} is offboarded; reactivation not allowed")

    owned = list(metadata.get("blocked_key_hashes") or [])
    # keys primeiro, flag por último: se falhar no meio o usuário segue
    # marcado (login recusado) e o evento pode ser repetido.
    await set_keys_blocked(owned, blocked=False, tolerate_missing=True)

    cleaned = {k: v for k, v in metadata.items() if k not in MANAGED_METADATA_KEYS}
    cleaned["reactivated_at"] = now_iso()
    await update_metadata(user_id, cleaned)
    return len(owned)


@app.post("/events/user-disabled")
async def user_disabled(event: UserEvent):
    state, blocked = await deprovision_user(event.email, STATE_SUSPENDED)
    return {"disabled": event.email, "state": state, "blocked_keys": blocked}


@app.post("/events/user-deleted")
async def user_deleted(event: UserEvent):
    state, blocked = await deprovision_user(event.email, STATE_OFFBOARDED)
    return {"disabled": event.email, "state": state, "blocked_keys": blocked}


@app.post("/events/user-enabled")
async def user_enabled(event: UserEvent):
    unblocked = await enable_user(event.email)
    return {"enabled": event.email, "unblocked_keys": unblocked}


@app.get("/health")
async def health():
    return {"status": "ok"}
