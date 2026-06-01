"""FastAPI control server for the simulator.

Runs in a background thread next to the simulator loop and exposes a small
HTTP API consumed by the Static Web App:

* ``GET  /healthz``                       — liveness, no auth
* ``GET  /api/state``                     — fleet snapshot (auth)
* ``POST /api/machines/{id}/random``      — {"enabled": bool} (auth)
* ``POST /api/machines/{id}/inject``      — {"kind": "spike|drift|stuck",
                                             "sensor": "<optional>"} (auth)

Auth is a shared secret sent in the ``X-API-Key`` header. The key is NOT a
strong secret (it ships to the browser), it only stops casual abuse of the
demo endpoint; treat the whole control plane as demo-grade.
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


def create_app(control: ControlState, api_key: str, *, cors_origins: list[str] | None = None) -> FastAPI:
    app = FastAPI(title="Simulator control API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        # Constant-ish comparison; api_key is demo-grade.
        if not api_key or x_api_key != api_key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "machine_count": control.snapshot()["machine_count"]}

    @app.get("/api/state", dependencies=[Depends(require_key)])
    def get_state() -> dict:
        return control.snapshot()

    @app.post("/api/machines/{machine_id}/random", dependencies=[Depends(require_key)])
    def set_random(machine_id: str, body: RandomBody) -> dict:
        if not control.set_random(machine_id, body.enabled):
            raise HTTPException(status_code=404, detail=f"unknown machine {machine_id}")
        return {"machine_id": machine_id, "random_enabled": body.enabled}

    @app.post("/api/machines/{machine_id}/inject", dependencies=[Depends(require_key)])
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
) -> threading.Thread:
    """Start uvicorn on a daemon thread and return it. Imports uvicorn lazily
    so the simulator has no hard dependency on it when control is disabled."""
    import uvicorn

    app = create_app(control, api_key, cors_origins=cors_origins)
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
