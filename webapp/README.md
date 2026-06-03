# webapp — simulator control panel (Entra-gated, same-origin)

A **React + TypeScript** single-page app (Vite build) that drives the cloud
simulator's control API (`simulator-cloud/`). It is served **same-origin**
by the simulator's FastAPI control server (no separate Static Web App), and
access is gated behind **Microsoft Entra ID** sign-in.

From the panel you can:

- see each simulated machine's **state** and latest sensor values,
- **force** a machine's state — pin any FSM state (e.g. `OFF`, `IDLE`,
  `PRODUCTION_HEAVY`) or pick **Auto (FSM)** to release control back to the
  autonomous state machine. The CNC machine (M-003) has no forcible states,
  so its selector is hidden,
- toggle **random anomalies** per machine,
- **inject** a `spike` / `drift` / `stuck` anomaly manually,
- watch a **per-sensor 5-minute live chart** of each machine (one chart per
  sensor, fixed height, numbered axes, zero always in view),
- detect when the **container is stopped** (the panel goes inactive).

The app is built with **Vite** (React 19 + Recharts + MSAL). Production assets
are compiled inside the Docker multi-stage build; nothing is served from a
CDN, so it satisfies the strict `script-src 'self'` Content-Security-Policy.

## How it connects

The page is served from the same origin as the API, so there is **one URL,
one redirect URI, and no CORS**. On load it fetches `GET /config.js` (public,
emitted by the control server from env) to learn the Entra `tenantId`,
`clientId` and `scope`, then uses **MSAL.js** to sign the user in. Only users
**assigned to the app registration** can sign in and reach the API.

Every API call sends `Authorization: Bearer <token>`; the server validates
the JWT (signature via JWKS, issuer, audience). `401`/`403` surface as
"Access denied — not authorized".

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | liveness |
| `GET /config.js` | none | front-end config (tenant/client/scope) |
| `GET /api/state` | Entra | fleet snapshot (state, last sample, `forced_state`, `valid_states`) — polled every ~2 s |
| `POST /api/machines/{id}/random` | Entra | `{"enabled": bool}` |
| `POST /api/machines/{id}/inject` | Entra | `{"kind": "spike\|drift\|stuck", "sensor": "<optional>"}` |
| `POST /api/machines/{id}/state` | Entra | `{"state": "<FSM state>"\|null}` — `null` releases to Auto |

## The per-sensor live charts (zero extra backend load)

The charts are built **entirely in the browser** from the data already
returned by the existing `/api/state` poll (which includes each machine's
`last_sample`). There is **no** second server→browser stream:

- each machine renders **one chart per sensor**, each a fixed height, so a
  machine with more sensors gets a taller card,
- the **X axis** is the last 5 minutes (numbered HH:MM:SS ticks); the
  **Y axis** auto-scales to the data but **always keeps zero in view**, even
  when the signal sits far from zero,
- it adds no load to the telemetry path or the control API beyond the poll
  that already runs,
- it **stops when the page closes** (polling stops with it),
- a **Pause** button puts it in standby (no requests at all),
- it **auto-pauses when the browser tab is hidden** and resumes when visible.

Charts are on by default behind the header **Charts** toggle. Rendering uses
**Recharts** (bundled locally by Vite), so it works under the strict
`script-src 'self'` Content-Security-Policy.

## Auth modes

- **Entra ID (preferred):** `SIM_AUTH_ENABLED=1` on the Container App with
  `SIM_AUTH_TENANT_ID` / `SIM_AUTH_CLIENT_ID`. The panel signs in with MSAL
  and sends a bearer token.
- **X-API-Key (test fallback):** with `SIM_AUTH_ALLOW_APIKEY=1` (or Entra
  off) the API accepts the `SIM_CONTROL_API_KEY` shared secret. The key would
  ship to the browser, so this is **demo-grade** only — fine for local dev,
  not for the live panel.

## Run locally

The app has a build step. For day-to-day UI work use the Vite dev server with
hot reload; it proxies `/api`, `/config.js` and `/healthz` to the running
simulator (default `http://localhost:8080`, override with `SIM_BACKEND`).

1. Start the simulator with the control API (no Event Hubs needed):

   ```pwsh
   $env:SIM_CONTROL_ENABLED = "1"
   $env:SIM_CONTROL_API_KEY = "dev-key"   # X-API-Key fallback for local dev
   $env:SIM_DRY_RUN         = "1"          # no Event Hub sink
   .\.venv\Scripts\python.exe simulator-cloud\src\cloud_runner.py
   ```

2. In another terminal, start the Vite dev server:

   ```pwsh
   cd webapp
   npm install      # first time only
   npm run dev      # http://localhost:5173
   ```

   Open <http://localhost:5173>. To preview the production bundle served the
   same way as in the container, run `npm run build` then point the simulator
   at the output with `$env:SIM_WEB_DIR = "webapp/dist"`.

> For the Entra-gated flow locally, register the dev origin as an SPA redirect
> URI on the app registration (see
> [`scripts/setup-app-registration.ps1`](../scripts/setup-app-registration.ps1)).

## Deploy

The panel is **baked into the simulator image** and served same-origin, so
deploying it is part of deploying the Container App. The one-shot, idempotent
orchestrator wires the container, the Entra app registration (redirect URI =
the container FQDN), and the Entra-gated redeploy:

```pwsh
pwsh ./scripts/deploy-control-panel.ps1
```

See [`simulator-cloud/README.md`](../simulator-cloud/README.md) for the
container-only deploy and the control-plane env vars, and
[`docs/architecture.md` §2b](../docs/architecture.md#2b-current-live-deployment--per-machine-models-4-ingested-3-scored)
for where the panel sits in the overall demo.
