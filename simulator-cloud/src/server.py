"""FastAPI control server for the simulator.

Runs in a background thread next to the simulator loop and exposes a small
HTTP API consumed by the Static Web App:

* ``GET  /healthz``                       — liveness, no auth
* ``GET  /api/state``                     — fleet snapshot (auth)
* ``POST /api/machines/{id}/random``      — {"enabled": bool} (auth)
* ``POST /api/machines/{id}/inject``      — {"kind": "spike|drift|stuck",
                                             "sensor": "<optional>"} (auth)

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

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from control import ControlState, VALID_KINDS


class RandomBody(BaseModel):
    enabled: bool


class InjectBody(BaseModel):
    kind: str
    sensor: str | None = None


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
) -> FastAPI:
    app = FastAPI(title="Simulator control API", version="2.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

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

    @app.get("/api/state", dependencies=[Depends(require_auth)])
    def get_state() -> dict:
        return control.snapshot()

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
