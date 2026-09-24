"""
Intercepta /ui/login quando já vem autenticado pelo oauth2-proxy (header
X-Forwarded-Email) e injeta uma sessão de UI válida no LiteLLM, replicando
o mecanismo nativo dele (ver litellm/proxy/auth/login_utils.py):

  1. garante o usuário (internal_user) via /user/info + /user/new e gera
     uma virtual key pra ele via Admin API (/key/generate)
  2. monta o mesmo payload que o LiteLLM usa (ReturnedUITokenObject)
  3. assina um JWT HS256 com o LITELLM_MASTER_KEY
  4. seta o cookie "token" (o mesmo nome/formato que o LiteLLM espera)
  5. redireciona pra /ui/?login=success já autenticado

/v1/* (liberado de sessão no oauth2-proxy) exige Authorization: Bearer
sk-... e descarta headers X-Forwarded-* do cliente. Qualquer outro path é
repassado pro LiteLLM em streaming.

Simplificação deliberada de PoC: gera uma key de UI nova a cada login
(o usuário é idempotente, a key não). Pra produção, reaproveitar ou
revogar a key anterior.
"""

import os
import time

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
UI_SESSION_TTL_SECONDS = 24 * 60 * 60

SSO_LOGIN_PATHS = {"/ui/login", "/ui/login/"}
# rotas liberadas no oauth2-proxy (OAUTH2_PROXY_SKIP_AUTH_ROUTES): exigem
# virtual key no Authorization, validada de fato pelo LiteLLM.
API_KEY_PREFIXES = ("/v1/",)

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

app = FastAPI()
# read=None: completions em streaming podem ficar abertas por minutos.
client = httpx.AsyncClient(base_url=LITELLM_BASE_URL, timeout=httpx.Timeout(30.0, read=None))


async def ensure_user(email: str) -> None:
    # sem registro em LiteLLM_UserTable o LiteLLM trata o dono da key como
    # role=unknown e bloqueia /key/generate pela UI.
    admin = {"Authorization": f"Bearer {MASTER_KEY}"}
    info = await client.get("/user/info", headers=admin, params={"user_id": email})
    if info.status_code == 200 and info.json().get("user_info"):
        return
    resp = await client.post(
        "/user/new",
        headers=admin,
        json={
            "user_id": email,
            "user_email": email,
            "user_role": "internal_user",
            "auto_create_key": False,
        },
    )
    resp.raise_for_status()


async def jit_provision_and_mint_cookie(email: str) -> str:
    await ensure_user(email)
    resp = await client.post(
        "/key/generate",
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        json={
            "user_id": email,
            "duration": "24h",
            "metadata": {"user_email": email, "provisioned_by": "auth-bridge"},
        },
    )
    resp.raise_for_status()
    key = resp.json()["key"]

    # mesmo shape de litellm/types/proxy/ui_sso.py:ReturnedUITokenObject
    claims = {
        "user_id": email,
        "key": key,
        "user_email": email,
        "user_role": "internal_user",
        "login_method": "sso",
        "premium_user": False,
        "auth_header_name": "Authorization",
        "disabled_non_admin_personal_key_creation": False,
        "server_root_path": "",
        "exp": int(time.time()) + UI_SESSION_TTL_SECONDS,
    }
    return jwt.encode(claims, MASTER_KEY, algorithm="HS256")


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def gateway(full_path: str, request: Request):
    path = "/" + full_path
    # oauth2-proxy, no modo reverse-proxy nativo, manda X-Forwarded-Email
    # (X-Auth-Request-Email é só do modo nginx auth_request).
    email = request.headers.get("x-forwarded-email")

    if path in SSO_LOGIN_PATHS and email:
        jwt_token = await jit_provision_and_mint_cookie(email)
        redirect = RedirectResponse(url="/ui/?login=success", status_code=303)
        redirect.set_cookie(
            key="token",
            value=jwt_token,
            httponly=False,
            samesite="lax",
            secure=False,
        )
        return redirect

    upstream_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}

    if path.startswith(API_KEY_PREFIXES):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer sk-"):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Missing virtual key (Authorization: Bearer sk-...)", "type": "auth_error"}},
            )
        # rota sem SSO: oauth2-proxy não sobrescreve estes headers, então
        # não repassa identidade vinda do cliente.
        upstream_headers = {k: v for k, v in upstream_headers.items() if not k.lower().startswith("x-forwarded-")}

    upstream_req = client.build_request(
        request.method,
        path,
        params=request.query_params,
        headers=upstream_headers,
        content=await request.body(),
    )
    upstream_resp = await client.send(upstream_req, stream=True)
    response_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )
