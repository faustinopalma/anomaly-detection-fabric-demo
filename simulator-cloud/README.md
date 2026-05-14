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
    -Machines 10 -Rate 2 -AnomalyProb 0.001 `
    -ImageTag v2
```

To tweak only the runtime parameters without rebuilding the image,
just rerun the script: it updates the env-vars on the existing
container app (no rebuild).

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
