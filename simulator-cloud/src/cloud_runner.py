"""Cloud entrypoint.

Wraps `simulate_machines.main()` in an infinite retry loop with
exponential backoff so transient Event Hubs / network errors don't
leave gaps in the data stream (gaps in the demo are themselves an
anomaly, so the producer must be as gap-free as possible).

All knobs are read from environment variables:

  SIM_MACHINES        (default 2)
  SIM_RATE            (default 1.0)        samples/s/sensor
  SIM_ANOMALY_PROB    (default 0.0005)
  SIM_BATCH_SIZE      (default 200)
  SIM_CNC_PROFILE     (optional)           path to CNC profile JSON; when set,
                                           the last machine is the CNC engine
  SIM_QUIET           (default unset)      set to "1" to suppress per-tick logs

Optional operator control plane (Static Web App backend):

  SIM_CONTROL_ENABLED      set to "1" to start the FastAPI control server
  SIM_CONTROL_PORT         (default 8080)
  SIM_CONTROL_API_KEY      shared secret required in the X-API-Key header
  SIM_CONTROL_CORS_ORIGINS (optional) comma-separated allowed origins

EVENTSTREAM_CONNECTION_STRING must be present in env (injected by ACA
secret reference at deploy time).
"""

from __future__ import annotations

import os
import random
import sys
import time
import traceback

import simulate_machines


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _maybe_start_control():
    """Create a shared ControlState and start the control API thread when
    SIM_CONTROL_ENABLED is set. Returns the ControlState (or None)."""
    if not _truthy(os.environ.get("SIM_CONTROL_ENABLED")):
        return None

    api_key = os.environ.get("SIM_CONTROL_API_KEY", "").strip()
    if not api_key:
        print("[cloud_runner] SIM_CONTROL_ENABLED set but SIM_CONTROL_API_KEY "
              "is empty — control plane disabled", flush=True)
        return None

    from control import ControlState
    import server

    try:
        default_prob = float(os.environ.get("SIM_ANOMALY_PROB", "0.0005"))
    except ValueError:
        default_prob = 0.0005

    control = ControlState(default_anomaly_prob=default_prob)
    port = int(os.environ.get("SIM_CONTROL_PORT", "8080"))
    server.serve_in_thread(
        control,
        api_key,
        port=port,
        cors_origins=server.cors_origins_from_env(),
    )
    print(f"[cloud_runner] control API listening on :{port}", flush=True)
    return control


def _argv_from_env() -> list[str]:
    args: list[str] = ["--duration", "0"]
    for env_key, flag in (
        ("SIM_MACHINES",     "--machines"),
        ("SIM_RATE",         "--rate"),
        ("SIM_ANOMALY_PROB", "--anomaly-prob"),
        ("SIM_BATCH_SIZE",   "--batch-size"),
        ("SIM_CNC_PROFILE",  "--cnc-profile"),
    ):
        v = os.environ.get(env_key)
        if v:
            args += [flag, v]
    if os.environ.get("SIM_QUIET", "").lower() in ("1", "true", "yes"):
        args.append("--quiet")
    return args


def main() -> int:
    control = _maybe_start_control()
    backoff = 5.0
    while True:
        try:
            print("[cloud_runner] starting simulator", flush=True)
            simulate_machines.main(_argv_from_env(), control=control)
            # main() returns only if --duration > 0 (we pass 0) or on
            # graceful shutdown. Treat any return as a transient hiccup
            # and restart.
            print("[cloud_runner] simulator returned — restarting in 5s", flush=True)
            time.sleep(5)
            backoff = 5.0
        except KeyboardInterrupt:
            print("[cloud_runner] SIGINT — exiting", flush=True)
            return 0
        except Exception:
            traceback.print_exc()
            jitter = random.uniform(0, backoff * 0.3)
            wait = min(backoff + jitter, 120.0)
            print(f"[cloud_runner] error — restarting in {wait:.1f}s", flush=True)
            time.sleep(wait)
            backoff = min(backoff * 2, 120.0)


if __name__ == "__main__":
    sys.exit(main())
