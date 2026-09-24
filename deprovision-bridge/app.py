"""
Recebe um evento de "usuário desativado/excluído" e aplica no LiteLLM
(POST /user/delete, com master key). É o mesmo papel do "Lambda worker"
do desenho de produção (que reage a change notification do Microsoft
Graph) — aqui, sem IdP corporativo disponível pro PoC, o evento é
disparado manualmente (curl) em vez de vir de um push real do Google.

Uso: user_id no LiteLLM = e-mail (mesma convenção do auth-bridge), então
apagar por e-mail funciona direto.
"""

import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]

app = FastAPI()
client = httpx.AsyncClient(base_url=LITELLM_BASE_URL, timeout=30.0)


class UserDeletedEvent(BaseModel):
    email: str


@app.post("/events/user-deleted")
async def user_deleted(event: UserDeletedEvent):
    resp = await client.post(
        "/user/delete",
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        json={"user_ids": [event.email]},
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return {"deleted": event.email, "litellm_response": resp.json()}


@app.get("/health")
async def health():
    return {"status": "ok"}
