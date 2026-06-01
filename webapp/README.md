# webapp — simulator control panel (Azure Static Web App)

A tiny, no-build static site (plain HTML/CSS/JS) that talks to the cloud
simulator's control API (`simulator-cloud/`). It lets you:

- see each simulated machine's **state** and latest sensor values,
- toggle **random anomalies** per machine,
- **inject** a `spike` / `drift` / `stuck` anomaly manually,
- detect when the **container is stopped** (panel goes inactive).

There is no build step — the three files (`index.html`, `styles.css`,
`app.js`) are served as-is by Azure Static Web Apps.

## How it connects

The page asks for two values (stored in your browser's `localStorage`, never
committed):

| Field        | Example                                                        |
|--------------|----------------------------------------------------------------|
| API base URL | `https://ca-simulator.<region>.azurecontainerapps.io`          |
| API key      | the `SIM_CONTROL_API_KEY` shared secret (sent as `X-API-Key`)  |

It then polls `GET {base}/api/state` every 2 s and POSTs to
`/api/machines/{id}/random` and `/api/machines/{id}/inject`.

> The API key ships to the browser, so it is **demo-grade** only — it stops
> casual abuse, not a determined attacker. Treat the whole control plane as a
> demo feature.

## Run locally

1. Start the simulator with the control API, no Event Hubs needed:

   ```pwsh
   $env:SIM_CONTROL_ENABLED = "1"
   $env:SIM_CONTROL_API_KEY = "dev-key"
   $env:SIM_DRY_RUN = "1"
   .\.venv\Scripts\python.exe simulator-cloud\src\cloud_runner.py
   ```

   (or run `simulator-cloud/src/simulate_machines.py --dry-run` wired to a
   `ControlState` + `server.serve_in_thread` — see `tools`/tests.)

2. Serve this folder on another port and open it:

   ```pwsh
   .\.venv\Scripts\python.exe -m http.server 5500 --directory webapp
   ```

   Open <http://localhost:5500>, set API base URL to
   `http://localhost:8080` and API key to `dev-key`, click **Connect**.

## Deploy

Infrastructure is in [`../infra/swa.bicep`](../infra/swa.bicep). The SWA is
free-tier and hosts only these static files; all dynamic behaviour lives in
the container's control API. Enabling inbound ingress on the Container App is
a manual step (see the Bicep comments) so the live demo isn't changed
unattended.
