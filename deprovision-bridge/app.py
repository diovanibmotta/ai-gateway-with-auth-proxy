"""
Recebe um evento de "usuário desativado/excluído" e desabilita o usuário no
LiteLLM, sem apagá-lo (apagar perde o histórico de spend/logs vinculado ao
user_id). É o mesmo papel do "Lambda worker" do desenho de produção (que
reage a change notification do Microsoft Graph) — aqui, sem IdP corporativo
disponível pro PoC, o evento é disparado manualmente (curl) em vez de vir de
um push real do Google.

LiteLLM_UserTable não tem flag de bloqueio (só as keys têm), então
"desabilitar" é:
  1. /user/update com max_budget=0 (qualquer key do usuário passa a ser
     recusada por budget) e metadata.deprovisioned=true (o auth-bridge
     recusa login de UI pra esse usuário)
  2. /key/block em todas as virtual keys do usuário

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

app = FastAPI()
client = httpx.AsyncClient(base_url=LITELLM_BASE_URL, headers=ADMIN_HEADERS, timeout=30.0)


class UserDeletedEvent(BaseModel):
    email: str


def raise_for_litellm(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)


async def disable_user(user_id: str) -> None:
    info = await client.get("/user/info", params={"user_id": user_id})
    raise_for_litellm(info)
    user_info = info.json().get("user_info")
    if not user_info:
        raise HTTPException(status_code=404, detail=f"user {user_id} not found")

    # /user/update substitui metadata inteiro: preservar o que já existe.
    metadata = {
        **(user_info.get("metadata") or {}),
        "deprovisioned": True,
        "deprovisioned_at": datetime.now(timezone.utc).isoformat(),
    }
    resp = await client.post(
        "/user/update",
        json={"user_id": user_id, "max_budget": 0, "metadata": metadata},
    )
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


async def block_keys(key_hashes: list[str]) -> None:
    for key_hash in key_hashes:
        resp = await client.post("/key/block", json={"key": key_hash})
        raise_for_litellm(resp)


@app.post("/events/user-deleted")
async def user_deleted(event: UserDeletedEvent):
    # desabilita o usuário antes de listar as keys: um login concorrente
    # já é recusado pelo auth-bridge e não gera key fora da lista.
    await disable_user(event.email)
    key_hashes = await list_user_key_hashes(event.email)
    await block_keys(key_hashes)
    return {"disabled": event.email, "blocked_keys": len(key_hashes)}


@app.get("/health")
async def health():
    return {"status": "ok"}
