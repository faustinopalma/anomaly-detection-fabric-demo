"""Cloud entrypoint.

Wraps `simulate_machines.main()` in an infinite retry loop with
exponential backoff so transient Event Hubs / network errors don't
leave gaps in the data stream (gaps in the demo are themselves an
anomaly, so the producer must be as gap-free as possible).

All knobs are read from environment variables:

  SIM_MACHINES        (default 5)
  SIM_RATE            (default 1.0)        samples/s/sensor
  SIM_ANOMALY_PROB    (default 0.0005)
  SIM_BATCH_SIZE      (default 200)
  SIM_QUIET           (default unset)      set to "1" to suppress per-tick logs

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


def _argv_from_env() -> list[str]:
    args: list[str] = ["--duration", "0"]
    for env_key, flag in (
        ("SIM_MACHINES",     "--machines"),
        ("SIM_RATE",         "--rate"),
        ("SIM_ANOMALY_PROB", "--anomaly-prob"),
        ("SIM_BATCH_SIZE",   "--batch-size"),
    ):
        v = os.environ.get(env_key)
        if v:
            args += [flag, v]
    if os.environ.get("SIM_QUIET", "").lower() in ("1", "true", "yes"):
        args.append("--quiet")
    return args


def main() -> int:
    backoff = 5.0
    while True:
        try:
            print("[cloud_runner] starting simulator", flush=True)
            simulate_machines.main(_argv_from_env())
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
