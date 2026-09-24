# LiteLLM + SSO (oauth2-proxy) sem licença Enterprise — com OpenCode

PoC local (docker compose) que coloca o **LiteLLM community** atrás do **oauth2-proxy** com **Google** como IdP:

- Login na Admin UI via SSO, sem o form nativo do LiteLLM e sem licença Enterprise.
- Usuário provisionado automaticamente no primeiro login (`internal_user`), já podendo criar as próprias virtual keys.
- API OpenAI-compatible (`/v1/*`) exposta pelo **mesmo** ponto de entrada (`:4180`), autenticada por virtual key — usada pelo **OpenCode**.
- Modelos da OpenAI via wildcard.

> Trocar Google por Microsoft Entra ID é só mudar `OAUTH2_PROXY_PROVIDER` e as envs correspondentes.

## Arquitetura

```mermaid
flowchart LR
    B[Browser] -->|/ui, admin| P
    O[OpenCode / SDK] -->|/v1/* + Bearer sk-...| P
    P[oauth2-proxy :4180] -->|OIDC| G[(Google)]
    P --> A[auth-bridge]
    A --> L[LiteLLM :4000]
    L --> D[(Postgres)]
    L --> OA[(OpenAI API)]
```

| Rota | Quem autentica |
|---|---|
| `/ui`, `/key/*`, `/user/*` etc. | oauth2-proxy (sessão Google) |
| `/v1/*` | auth-bridge (exige `Bearer sk-`) + LiteLLM (valida a virtual key) |

Só o oauth2-proxy tem porta publicada. LiteLLM, auth-bridge e Postgres ficam só na rede docker.

**auth-bridge** é um FastAPI pequeno entre o oauth2-proxy e o LiteLLM:

1. Em `/ui/login` com `X-Forwarded-Email` (vindo do oauth2-proxy): garante o usuário (`/user/info` + `/user/new`, role `internal_user`), gera uma virtual key, assina o mesmo JWT que o LiteLLM usa na sessão da UI (`ReturnedUITokenObject`, HS256 com o master key) e seta o cookie `token`.
2. Em `/v1/*`: rejeita request sem `Authorization: Bearer sk-...` e remove headers `X-Forwarded-*` vindos do cliente.
3. Resto: proxy em streaming (SSE) pro LiteLLM, sem timeout de leitura.

## Estrutura

```
.
├── docker-compose.yml
├── .env
├── litellm/
│   └── config.yaml
└── auth-bridge/
    ├── Dockerfile
    ├── requirements.txt
    └── app.py
```

## `.env`

```bash
# Google Cloud Console > APIs & Services > Credentials > OAuth client ID (Web application)
# Authorized redirect URI: http://localhost:4180/oauth2/callback
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# gerar com: openssl rand -base64 32 | tr -- '+/' '-_'
OAUTH2_PROXY_COOKIE_SECRET=

# "*" libera qualquer conta Google (só PoC). Pra restringir: seu-dominio.com.br
OAUTH2_PROXY_EMAIL_DOMAINS=*

LITELLM_MASTER_KEY=sk-test-master-key-troque-me

OPENAI_API_KEY=
```

## `docker-compose.yml`

```yaml
services:
  postgres:
    image: postgres:16-alpine
    container_name: litellm-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: litellm
      POSTGRES_PASSWORD: litellm
      POSTGRES_DB: litellm
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U litellm"]
      interval: 5s
      timeout: 5s
      retries: 10
    networks:
      - poc
    # sem "ports:" — só acessível dentro da rede docker deste PoC.

  litellm:
    image: ghcr.io/berriai/litellm:main-stable
    container_name: litellm
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      DATABASE_URL: postgresql://litellm:litellm@postgres:5432/litellm
      # sem isso, "Logout" só limpa o cookie do LiteLLM — a sessão do
      # oauth2-proxy continua válida e o auth-bridge reloga na hora.
      PROXY_LOGOUT_URL: "http://localhost:4180/oauth2/sign_out?rd=http%3A%2F%2Flocalhost%3A4180%2Fui%2Flogin"
    volumes:
      - ./litellm/config.yaml:/app/config.yaml:ro
    command: ["--config", "/app/config.yaml", "--port", "4000"]
    networks:
      - poc
    # sem "ports:" de propósito — só o oauth2-proxy fica exposto ao host,
    # replicando o desenho real (LiteLLM nunca público direto).

  auth-bridge:
    build: ./auth-bridge
    container_name: auth-bridge
    restart: unless-stopped
    depends_on:
      - litellm
    environment:
      LITELLM_BASE_URL: http://litellm:4000
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}
    networks:
      - poc
    # também sem "ports:" — só alcançável via oauth2-proxy.

  oauth2-proxy:
    image: quay.io/oauth2-proxy/oauth2-proxy:latest
    container_name: oauth2-proxy
    restart: unless-stopped
    depends_on:
      - auth-bridge
    ports:
      - "4180:4180"
    environment:
      OAUTH2_PROXY_PROVIDER: google
      OAUTH2_PROXY_CLIENT_ID: ${GOOGLE_CLIENT_ID}
      OAUTH2_PROXY_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET}
      OAUTH2_PROXY_COOKIE_SECRET: ${OAUTH2_PROXY_COOKIE_SECRET}
      OAUTH2_PROXY_EMAIL_DOMAINS: ${OAUTH2_PROXY_EMAIL_DOMAINS:-*}
      OAUTH2_PROXY_UPSTREAMS: http://auth-bridge:8000
      OAUTH2_PROXY_HTTP_ADDRESS: 0.0.0.0:4180
      OAUTH2_PROXY_REDIRECT_URL: http://localhost:4180/oauth2/callback
      OAUTH2_PROXY_COOKIE_SECURE: "false"
      OAUTH2_PROXY_SET_XAUTHREQUEST: "true"
      OAUTH2_PROXY_SKIP_PROVIDER_BUTTON: "true"
      # API (OpenCode, SDKs) não faz OIDC no browser: /v1/* passa sem sessão
      # do Google e é autenticado pela virtual key (auth-bridge + LiteLLM).
      OAUTH2_PROXY_SKIP_AUTH_ROUTES: "^/v1/"
    networks:
      - poc

networks:
  poc:
    driver: bridge
```

## `litellm/config.yaml`

```yaml
model_list:
  - model_name: "openai/*"
    litellm_params:
      model: "openai/*"
      api_key: os.environ/OPENAI_API_KEY
  - model_name: "*"
    litellm_params:
      model: "openai/*"
      api_key: os.environ/OPENAI_API_KEY

litellm_settings:
  check_provider_endpoint: true

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

- `openai/*` aceita `openai/gpt-5.4`; `*` aceita o nome sem prefixo (`gpt-5.4`).
- `check_provider_endpoint: true` faz o `/v1/models` listar os modelos reais da conta OpenAI.

## `auth-bridge/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

## `auth-bridge/requirements.txt`

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
pyjwt==2.9.0
```

## `auth-bridge/app.py`

```python
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
```

## Subir

1. Google Cloud Console > APIs & Services > Credentials > **OAuth client ID** (Web application). Redirect URI: `http://localhost:4180/oauth2/callback`.
2. Preencher `.env` (client id/secret, cookie secret, `OPENAI_API_KEY`).
3. `docker compose up -d --build`
4. Acessar `http://localhost:4180/ui/login` > login Google > cai direto no dashboard.

## Configurar no OpenCode

1. **Criar a virtual key:** `http://localhost:4180/ui` > **Virtual Keys** > **+ Create New Key**. Copiar o `sk-...` (só aparece uma vez). Não usar a key gerada automaticamente no login (expira em 24h).
2. **Variável de ambiente** (PowerShell; abrir terminal novo depois):
   ```powershell
   setx LITELLM_API_KEY "sk-xxxx"
   ```
   Linux/macOS: `export LITELLM_API_KEY=sk-xxxx` no `~/.bashrc`/`~/.zshrc`.
3. **Provider** em `~/.config/opencode/opencode.json` (global) ou `opencode.json` na raiz do projeto:
   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "provider": {
       "litellm": {
         "npm": "@ai-sdk/openai-compatible",
         "name": "LiteLLM (PoC)",
         "options": {
           "baseURL": "http://localhost:4180/v1",
           "apiKey": "{env:LITELLM_API_KEY}"
         },
         "models": {
           "gpt-5.5":      { "name": "GPT-5.5" },
           "gpt-5.4":      { "name": "GPT-5.4" },
           "gpt-5.4-mini": { "name": "GPT-5.4 mini" },
           "gpt-4.1":      { "name": "GPT-4.1" },
           "o4-mini":      { "name": "o4-mini" }
         }
       }
     },
     "model": "litellm/gpt-5.4"
   }
   ```
   Provider custom não descobre modelos sozinho — listar em `models` os IDs desejados (consultar `/v1/models`). Alternativa ao env var: `opencode auth login` > **Other** > provider id `litellm`, e remover `apiKey` do JSON.
4. **Testar:**
   ```bash
   curl http://localhost:4180/v1/models -H "Authorization: Bearer $LITELLM_API_KEY"
   ```
   No `opencode`: `/models` > **LiteLLM (PoC)** > escolher modelo > prompt. Consumo aparece em **Usage/Logs** na UI do LiteLLM.

### Troubleshooting

| Sintoma | Causa |
|---|---|
| `401 Missing virtual key` | Header `Authorization` ausente / env var não carregada |
| `401` vindo do LiteLLM | Key inválida ou expirada |
| `302` pro Google em `/v1` | oauth2-proxy sem `OAUTH2_PROXY_SKIP_AUTH_ROUTES` |
| Modelo não aparece no OpenCode | Não listado em `models` do `opencode.json` |
| `400 ... not supported in v1/chat/completions` | Modelo só Responses API (ex.: `*-codex`, `*-pro`) |

## Achados (o que não é óbvio)

- oauth2-proxy em modo reverse-proxy manda **`X-Forwarded-Email`**, não `X-Auth-Request-Email` (esse é do modo nginx `auth_request`).
- LiteLLM **exige `DATABASE_URL`** pra qualquer login de UI — sem banco, `/key/generate` falha.
- Logout do LiteLLM só limpa o cookie `token`; a sessão do oauth2-proxy sobrevive e reloga na hora. Resolver com `PROXY_LOGOUT_URL` apontando pro `/oauth2/sign_out`.
- `/key/generate` com `user_id` **não cria o usuário**. Sem registro em `LiteLLM_UserTable` o dono vira `role=unknown` e a UI bloqueia criação de key: `Only proxy admin can be used to generate ... Your role=unknown`. Criar antes com `/user/new` (`user_role: internal_user`).
- oauth2-proxy **não valida virtual key**: sem sessão, até request com `Bearer sk-...` leva `302` pro IdP. `skip_jwt_bearer_tokens` só aceita JWT do IdP, e clientes de API não fazem OIDC no browser. Saída: `OAUTH2_PROXY_SKIP_AUTH_ROUTES=^/v1/` + validação da key no bridge/LiteLLM.
- Proxy que bufferiza a resposta (ou tem timeout curto) quebra o streaming SSE do OpenCode — usar `client.send(stream=True)` + `StreamingResponse`.
- Em rotas liberadas pelo `SKIP_AUTH_ROUTES` o oauth2-proxy não sobrescreve `X-Forwarded-Email` — o cliente pode forjar identidade. Descartar `X-Forwarded-*` nessas rotas.
- Admin UI do LiteLLM não mostra foto/avatar (nem no SSO nativo) — só iniciais.

## Não usar assim em produção

- Key de UI nova a cada login — reaproveitar/revogar a anterior.
- `OAUTH2_PROXY_EMAIL_DOMAINS=*` libera qualquer conta — restringir por domínio.
- Wildcard `*` manda qualquer nome de modelo pra OpenAI — lista explícita ou `models` por key/team.
- Sem TLS (`http://localhost`), cookie `secure=false`, sem WAF/rate limit.
- Master key e senha do Postgres fixas/fracas.
