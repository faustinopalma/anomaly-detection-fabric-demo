"""FastAPI control server for the simulator.

Runs in a background thread next to the simulator loop. It serves the static
control panel (same-origin, no separate Static Web App) and a small HTTP API:

* ``GET  /healthz``                       — liveness, no auth
* ``GET  /config.js``                     — public front-end config, no auth
* ``GET  /api/state``                     — fleet snapshot (auth)
* ``GET  /api/history?since=<epoch_s>``   — rolling per-sensor history (auth)
* ``POST /api/machines/{id}/random``      — {"enabled": bool} (auth)
* ``POST /api/machines/{id}/inject``      — {"kind": "spike|drift|stuck",
                                             "sensor": "<optional>"} (auth)

The control panel (``index.html`` / ``app.js`` / ``styles.css`` / MSAL) is
mounted at ``/`` from ``web_dir`` (``SIM_WEB_DIR``, default ``/app/webapp``).

Auth modes:

* **Entra ID (preferred).** When ``SIM_AUTH_ENABLED=1`` the API requires a
  valid ``Authorization: Bearer <token>`` access token issued by the tenant
  app registration. The token signature (JWKS), issuer and audience are
  validated. Only users assigned to the app registration in the tenant can
  obtain a token, so this gates access to authorized tenant users.
* **Shared key (legacy / fallback).** The ``X-API-Key`` header. Used when
  Entra auth is disabled, or — when ``SIM_AUTH_ALLOW_APIKEY=1`` — as an
  additional bypass for direct server-to-server testing. Treat as demo-grade.
"""

from __future__ import annotations

import os
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from control import ControlState, VALID_KINDS

# Content-Security-Policy tuned for the control panel: everything same-origin
# except the MSAL sign-in flow, which talks to login.microsoftonline.com.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self' https://login.microsoftonline.com; "
    "frame-src https://login.microsoftonline.com; "
    "form-action 'self' https://login.microsoftonline.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'"
)


class RandomBody(BaseModel):
    enabled: bool


class InjectBody(BaseModel):
    kind: str
    sensor: str | None = None


class StateBody(BaseModel):
    # None / null returns the machine to automatic FSM control.
    state: str | None = None


class JwtValidator:
    """Validates Entra ID v2.0 access tokens against the tenant JWKS."""

    def __init__(self, tenant_id: str, client_id: str) -> None:
        import jwt  # lazy import; only needed when auth is enabled
        from jwt import PyJWKClient

        self._jwt = jwt
        self.audience = client_id
        self.issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        jwks_uri = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        self._jwk_client = PyJWKClient(jwks_uri)

    def validate(self, token: str) -> dict:
        signing_key = self._jwk_client.get_signing_key_from_jwt(token)
        return self._jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
        )


def create_app(
    control: ControlState,
    api_key: str,
    *,
    cors_origins: list[str] | None = None,
    validator: JwtValidator | None = None,
    allow_api_key: bool = True,
    web_dir: str | None = None,
) -> FastAPI:
    app = FastAPI(title="Simulator control API", version="2.0.0")

    # CORS is only needed when the panel is served from a different origin
    # (e.g. a separate Static Web App). When the webapp is served from this
    # same container (web_dir set) requests are same-origin and no CORS
    # headers are required, so we keep the middleware off by default.
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        return resp

    def require_auth(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> None:
        if validator is not None:
            # Entra ID mode: a valid bearer token is the primary credential.
            if authorization and authorization.lower().startswith("bearer "):
                token = authorization.split(" ", 1)[1].strip()
                try:
                    validator.validate(token)
                    return
                except Exception as exc:  # noqa: BLE001 — surface as 401
                    raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
            # Optional shared-key bypass for direct testing.
            if allow_api_key and api_key and x_api_key == api_key:
                return
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

        # Legacy shared-key mode.
        if not api_key or x_api_key != api_key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "machine_count": control.snapshot()["machine_count"]}

    @app.get("/config.js")
    def config_js() -> Response:
        """Front-end runtime config, served same-origin so the panel needs no
        build-time generation. clientId / tenantId / scope are public values
        (not secrets); the API itself stays gated by the bearer token."""
        client_id = os.environ.get("SIM_AUTH_CLIENT_ID", "").strip()
        tenant_id = os.environ.get("SIM_AUTH_TENANT_ID", "").strip()
        scope = os.environ.get("SIM_AUTH_SCOPE", "").strip() or (
            f"api://{client_id}/access_as_user" if client_id else ""
        )
        js = (
            '"use strict";\n'
            "// Served by the simulator control API (same origin). Assigned to\n"
            "// window so the React app bundle can read it as window.CONFIG.\n"
            "window.CONFIG = {\n"
            '  backendUrl: "",\n'
            f'  tenantId: "{tenant_id}",\n'
            f'  clientId: "{client_id}",\n'
            f'  scope: "{scope}",\n'
            "};\n"
        )
        return Response(content=js, media_type="application/javascript")

    @app.get("/api/state", dependencies=[Depends(require_auth)])
    def get_state() -> dict:
        return control.snapshot()

    @app.get("/api/history", dependencies=[Depends(require_auth)])
    def get_history(since: float = 0.0) -> dict:
        """Rolling per-sensor history. ``since`` (epoch seconds) returns only
        newer samples for incremental polling; omit it (or pass 0) to backfill
        the whole retained window after a client reconnect."""
        return control.history(since=since)

    @app.post("/api/machines/{machine_id}/random", dependencies=[Depends(require_auth)])
    def set_random(machine_id: str, body: RandomBody) -> dict:
        if not control.set_random(machine_id, body.enabled):
            raise HTTPException(status_code=404, detail=f"unknown machine {machine_id}")
        return {"machine_id": machine_id, "random_enabled": body.enabled}

    @app.post("/api/machines/{machine_id}/inject", dependencies=[Depends(require_auth)])
    def inject(machine_id: str, body: InjectBody) -> dict:
        if body.kind not in VALID_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"invalid kind {body.kind!r}; expected one of {list(VALID_KINDS)}",
            )
        try:
            ok = control.request_injection(machine_id, body.kind, body.sensor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail=f"unknown machine {machine_id}")
        return {"machine_id": machine_id, "queued": {"kind": body.kind, "sensor": body.sensor}}

    @app.post("/api/machines/{machine_id}/state", dependencies=[Depends(require_auth)])
    def set_state(machine_id: str, body: StateBody) -> dict:
        try:
            ok = control.set_forced_state(machine_id, body.state)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail=f"unknown machine {machine_id}")
        return {"machine_id": machine_id, "forced_state": body.state}

    # Serve the control panel from this same container (same-origin). Mounted
    # last so the explicit API/config routes above take precedence. Skipped
    # when the directory is absent (e.g. API-only local runs).
    if web_dir and os.path.isdir(web_dir):
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


def serve_in_thread(
    control: ControlState,
    api_key: str,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    cors_origins: list[str] | None = None,
    validator: JwtValidator | None = None,
    allow_api_key: bool = True,
    web_dir: str | None = None,
) -> threading.Thread:
    """Start uvicorn on a daemon thread and return it. Imports uvicorn lazily
    so the simulator has no hard dependency on it when control is disabled."""
    import uvicorn

    app = create_app(
        control,
        api_key,
        cors_origins=cors_origins,
        validator=validator,
        allow_api_key=allow_api_key,
        web_dir=web_dir,
    )
    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, name="control-api", daemon=True)
    thread.start()
    return thread


def cors_origins_from_env() -> list[str] | None:
    raw = os.environ.get("SIM_CONTROL_CORS_ORIGINS", "").strip()
    if not raw:
        return None
    return [o.strip() for o in raw.split(",") if o.strip()]


def validator_from_env() -> JwtValidator | None:
    """Build a JWT validator if Entra auth is enabled and configured."""
    if os.environ.get("SIM_AUTH_ENABLED", "0").strip() not in ("1", "true", "True"):
        return None
    tenant_id = os.environ.get("SIM_AUTH_TENANT_ID", "").strip()
    client_id = os.environ.get("SIM_AUTH_CLIENT_ID", "").strip()
    if not tenant_id or not client_id:
        raise RuntimeError(
            "SIM_AUTH_ENABLED=1 but SIM_AUTH_TENANT_ID / SIM_AUTH_CLIENT_ID are not set"
        )
    return JwtValidator(tenant_id, client_id)


def allow_api_key_from_env() -> bool:
    return os.environ.get("SIM_AUTH_ALLOW_APIKEY", "0").strip() in ("1", "true", "True")


def web_dir_from_env() -> str | None:
    """Directory of the static control panel to serve same-origin. Defaults to
    /app/webapp (baked into the image); returns None when unset."""
    d = os.environ.get("SIM_WEB_DIR", "/app/webapp").strip()
    return d or None
