# simulator-cloud

Deploy the simulator as an always-on **Azure Container App** with a single
replica. Designed for the "no-gap" scenario: the container stays up 24/7
and the inner runner retries with exponential backoff so that transient
Event Hub errors do not leave gaps in the telemetry stream
(in this demo, gaps *are* anomalies that should be detected).

## What it creates

Everything in the same resource group as the Fabric capacity (resolved
from the `FABRIC_CAPACITY_NAME` value in `.env`).

| Resource               | Default name      | Purpose                          |
| ---------------------- | ----------------- | -------------------------------- |
| Container Registry     | `acrsim<rand>`    | hosts the image                  |
| Container Apps env     | `cae-anomalydet`  | managed runtime                  |
| Container App          | `ca-simulator`    | the producer (1 fixed replica)   |

No Key Vault: `EVENTSTREAM_CONNECTION_STRING` is injected as an
**ACA secret** (encrypted at rest, never exposed as a plain env-var) and
referenced via `secretref:eventstream-conn`.

## Prerequisites

- Azure CLI (`az`) installed.
- The `containerapp` extension is installed automatically.
- `.env` populated with at least: `FABRIC_TENANT_ID`, `FABRIC_CAPACITY_NAME`,
  `EVENTSTREAM_CONNECTION_STRING`.

## Deploy

```pwsh
pwsh ./simulator-cloud/deploy.ps1
```

The first invocation runs `az login --use-device-code` (opens a URL +
code to paste in the browser). Subsequent runs reuse the session.

Main knobs (all optional):

```pwsh
pwsh ./simulator-cloud/deploy.ps1 `
    -Location northeurope `
    -Machines 4 -CncMachineId M-003 -Rate 2 -AnomalyProb 0.001 `
    -ImageTag v2
```

Defaults: `-Machines 4` with the real CNC machine pinned to
`-CncMachineId M-003` and the synthgen CNC machine pinned to
`-SynthMachineId M-002` (driven by `-SynthProfile /app/synth_trace_M-002.json`,
a 1 Hz replay trace baked into the image). So M-004 is added as a 4th physics
machine without moving either CNC role. The live fleet ingests 4 machines;
3 are scored in KQL (M-004 is ingested-only — see
[`../docs/architecture.md` §2b](../docs/architecture.md#2b-current-live-deployment--per-machine-models-4-ingested-3-scored)).

To tweak only the runtime parameters without rebuilding the image,
just rerun the script: it updates the env-vars on the existing
container app (no rebuild).

## Control panel (optional)

The container can also serve an **Entra ID–gated operator control panel**
same-origin (the static `webapp/` baked into the image + the FastAPI control
API in `src/server.py`). It exposes machine state, force-state, anomaly
toggle/inject and a client-side live chart. It is **off by default** and adds
no load to the telemetry path.

Relevant env vars (see [`../.env.example`](../.env.example) for the full
contract):

| Var | Purpose |
| --- | --- |
| `SIM_CONTROL_ENABLED` | `1` turns the control API + panel on |
| `SIM_CONTROL_PORT` | port the control API listens on (must match ingress) |
| `SIM_AUTH_ENABLED` | `1` requires an Entra bearer token (preferred) |
| `SIM_AUTH_TENANT_ID` / `SIM_AUTH_CLIENT_ID` | tenant + SPA app-registration GUIDs |
| `SIM_AUTH_ALLOW_APIKEY` | `1` also accepts the `SIM_CONTROL_API_KEY` (testing) |
| `SIM_CONTROL_API_KEY` | shared secret for the X-API-Key fallback |

For a one-shot, idempotent deploy of the whole panel stack (container +
Entra app registration + Entra-gated redeploy) use the orchestrator:

```pwsh
pwsh ./scripts/deploy-control-panel.ps1
```

See [`../webapp/README.md`](../webapp/README.md) for the panel itself.

## Operations

```pwsh
# Tail logs
az containerapp logs tail -g <rg> -n ca-simulator --follow

# Force a replica restart
$rev = az containerapp revision list -g <rg> -n ca-simulator --query '[0].name' -o tsv
az containerapp revision restart -g <rg> -n ca-simulator --revision $rev
```

## Teardown

```pwsh
# App only
pwsh ./simulator-cloud/teardown.ps1

# App + Container Apps env + ACR
pwsh ./simulator-cloud/teardown.ps1 -RemoveEnv -RemoveAcr
```

## Costs (order of magnitude)

Container Apps consumption ships with 180 000 vCPU-s/month and
360 000 GiB-s/month included free. With `0.25 vCPU / 0.5 GiB` always-on
you consume ~648 000 vCPU-s/month → the free tier covers ~28 % and the
rest costs a few euros/month. ACR Basic ~5 €/month.
