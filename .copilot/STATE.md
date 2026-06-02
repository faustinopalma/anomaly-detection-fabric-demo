# Current state

_Last updated: 2026-06-02 (docs audit: 4 ingested/3 scored + control panel)_

## Latest session (2026-06-02f) — docs audit & refresh (DONE)

Aligned all documentation with the live state (4 machines ingested, 3 scored)
and documented the new control panel.
- `README.md`: "3 machines" → "4 ingested, 3 scored" table + M-004
  ingested-not-scored note; `SIM_MACHINES=4`/`SIM_CNC_MACHINE=M-003`; added
  control-panel + simulator-cloud doc rows.
- `docs/architecture.md` §2b: heading + body to 4 ingested/3 scored; new
  "Control panel (operator UI)" subsection (Entra, force-state, inject,
  client-side chart). Anchor changed → fixed the RUNBOOK link.
- `webapp/README.md`: full rewrite — same-origin Container App + Entra login
  (was SWA + browser API key); endpoint table incl. `/api/machines/{id}/state`;
  5-min client-side chart section; local-run + `deploy-control-panel.ps1`.
- `simulator-cloud/README.md`: default `-Machines 4 -CncMachineId M-003`; new
  "Control panel (optional)" env-var table + orchestrator.
- `tools/README.md`: local-fleet example → 4 machines `--cnc-machine-id M-003`.
- `.env.example`: control-plane wording SWA → same-origin panel.
- `docs/RUNBOOK.md`: new §13 (optional) always-on cloud sim + control panel.
- No code changes; concepts.md/KQL cookbook unchanged (still accurate).

## Latest session (2026-06-02e) — force state + client-side chart (DONE, live)

Two operator features on the same-origin control panel.

**1. Force machine state.** OFF/IDLE are normal FSM operating-cycle states
(not random glitches); added an operator override.
- `simulator-cloud/src/control.py`: `_MachineEntry.forced_state` +
  `valid_states`; `set_forced_state(id, state|None)` (validates against the
  machine's valid states, None=auto), loop-side `forced_state(id)`; both
  exposed in `snapshot()`.
- `simulator-cloud/src/simulate_machines.py`: `Machine.forced_state` field +
  `set_forced_state()` + `valid_states` (8 State values); `step()` pins the
  state when forced; `CNCMachine` (M-003) no-op + empty `valid_states`;
  `run()` applies `control.forced_state(id)` each tick before `m.step()`.
- `simulator-cloud/src/server.py`: `StateBody` + `POST
  /api/machines/{id}/state` (422 bad state, 404 unknown machine).

**2. Client-side 5-min live chart.** Built ENTIRELY from the existing
`/api/state` poll (already returns `last_sample`), so zero extra backend load;
stops on page close (polling stops) and is pausable. Auto-pauses on hidden tab.
- `webapp/index.html`: header **Charts** toggle + **Pause** (standby) buttons.
- `webapp/app.js`: per-card "Force state" `<select>` (hidden for CNC) →
  `onForceState`; rolling per-machine history (5-min window) fed from the poll;
  `<canvas>` line chart (`drawChart`, per-sensor autoscale, devicePixelRatio);
  `setChartsOn`/`setPaused`; `visibilitychange` auto-standby.
- `webapp/styles.css`: `.force-state`, `button.ghost`, `.chart-wrap`,
  `.chart`, `.chart-legend`.

- Built `acrsimnsb7uf.azurecr.io/simulator:web2` (ACR run `nfc`, Succeeded);
  redeployed `ca-simulator` (revision `0000005`).
- **Verified live**: `/healthz` 200 (machine_count=4); `GET /` panel 200 and
  contains `charts-btn`/`pause-btn`; `/app.js` contains `force-state` +
  `CHART_WINDOW_MS`; `POST /api/machines/M-001/state` → 401 without token
  (auth-gated). `control.py` forced-state logic unit-tested (set/validate/
  reject/release/unknown).

## Latest session (2026-06-02d) — same-origin panel, SWA deleted (DONE, live)

Collapsed the two-origin setup (Static Web App + Container App) into a single
same-origin Container App for the single-user demo. The FastAPI control server
now serves the static panel and a dynamic `/config.js`, so there is one public
URL, one redirect URI, and no CORS.

- `simulator-cloud/src/server.py`: `create_app(web_dir=...)` mounts `webapp/`
  at `/` via `StaticFiles(html=True)` AFTER the API routes; new public
  `GET /config.js` emits `{backendUrl:"", tenantId, clientId, scope}` from env;
  `@app.middleware` adds `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, and a MSAL-tuned `Content-Security-Policy`; CORS is now
  conditional (off by default). New `web_dir_from_env()` (`SIM_WEB_DIR`,
  default `/app/webapp`); `serve_in_thread(web_dir=...)`.
- `simulator-cloud/src/cloud_runner.py`: passes `web_dir=server.web_dir_from_env()`.
- `simulator-cloud/Dockerfile`: `COPY webapp/ /app/webapp/` + `ENV SIM_WEB_DIR`.
- `simulator-cloud/deploy.ps1`: stages repo `webapp/` into the build context
  before `az acr build` (drops SWA-only files); new `-SkipBuild` switch.
- `scripts/deploy-control-panel.ps1`: rewritten to a 3-step same-origin flow
  (bootstrap container → app-reg with the container URL → redeploy with auth).
  Dropped the SWA + webapp-publish steps and `-SwaName/-SwaLocation`.
- **Removed**: `webapp/config.js`, `webapp/staticwebapp.config.json`,
  `webapp/deploy.ps1`, `infra/swa.bicep`; `.gitignore` ignores the staged
  `simulator-cloud/webapp/`.
- Built `acrsimnsb7uf.azurecr.io/simulator:web1` (ACR run `nfb`, Succeeded);
  redeployed `ca-simulator` (revision `v2606020819`).
- App-reg SPA redirect URIs replaced with the container URL +
  `http://localhost:8080`.
- **Deleted the Azure SWA** `swa-anomaly-sim` (confirmed gone).
- **Verified live**: `GET /` → panel HTML 200 (`X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`); `GET /config.js` → 200, correct clientId/tenantId,
  `application/javascript`, CSP present; `/healthz` 200; `/api/state` 401
  without token. Panel URL = the container FQDN.

## Latest session (2026-06-02c) — run M-004 in the cloud simulator (DONE, live)

The control panel showed only 3 machines (M-001..M-003) although 4 models were
trained (M-001..M-004). Root cause: the live simulator ran `SIM_MACHINES=3`.
The CNC engine was hard-pinned to the **last** machine (`M-{n:03d}`), so naively
bumping to 4 would have made **M-004** the CNC and demoted M-003 to a physics
machine — breaking both models' feature mapping (M-003=3 CNC feats, M-004=8
physics feats).

Fix — pin the CNC machine explicitly, then bump to 4:
- `simulator-cloud/src/simulate_machines.py`: new `--cnc-machine-id` flag
  (also reads `SIM_CNC_MACHINE`), threaded into `build_machines(..., cnc_machine_id=)`.
- `simulator-cloud/src/cloud_runner.py`: `SIM_CNC_MACHINE` → `--cnc-machine-id`
  in `_argv_from_env()` (+ docstring).
- `simulator-cloud/deploy.ps1`: new `-CncMachineId` param (default `M-003`),
  default `-Machines` bumped 2→4, emits `SIM_CNC_MACHINE`.
- Built `acrsimnsb7uf.azurecr.io/simulator:m004` (ACR run `nfa`, Succeeded).
- Redeployed `ca-simulator` (revision `v2606012340`) with `SIM_MACHINES=4`,
  `SIM_CNC_MACHINE=M-003`, `SIM_CNC_PROFILE=/app/cnc_profile_M-003.json`.
- **Verified live**: `/api/state` → `machine_count=4`; M-001/M-002/M-004 = 8
  sensors (physics), M-003 = 3 sensors (CNC, idle).

NOTE: M-004's ONNX model exists on disk (`models/transformer_ae_small__M-004/`)
but registering it in the KQL `models` table for scoring is a separate step
(`tools/05_register_model.py` + `kql/02_models.kql`), not done here.

## Latest session (2026-06-02b) — MSAL login fix + scripted repeatable deploy (DONE, live)

The control panel showed "not connected" and never prompted for login because
the MSAL.js CDN URL (`alcdn.msftauth.net/...`) returned **404** → `msal` was
undefined → `new msal.PublicClientApplication(...)` threw at load → boot never
ran. Fixes + the new "everything must be scripted/repeatable" requirement:

- **MSAL vendored locally**: `webapp/vendor/msal-browser.min.js` (2.38.4,
  376 037 bytes, from jsDelivr). `index.html` now loads
  `vendor/msal-browser.min.js` → `config.js` → `app.js` (CDN removed).
- **Fail-loud guard** in `app.js`: if `msal`/`CONFIG` missing, the sign-in card
  is shown with an explicit error instead of dying silently.
- **Republished + verified live**: `vendor/msal-browser.min.js` 200/376037,
  `config.js` 200, `app.js` 200, `index.html` 200 on the SWA. Backend re-check:
  healthz 200, `/api/state` 401 without token, **200 with a real Entra token**.

**Repeatable deploy — now fully scripted (no manual `az rest`/portal steps):**
- `scripts/setup-app-registration.ps1` (new) — idempotent Entra app-reg setup
  (app/SP, identifierUris, SPA redirect URIs, `access_as_user` scope, token v2,
  Azure CLI pre-auth, `appRoleAssignmentRequired`, signed-in-user assignment,
  admin consent). Writes `SIM_AUTH_*` to `.env`; returns ClientId/Scope/etc.
- `simulator-cloud/deploy.ps1` (updated) — new `-EnableControl`,
  `-AuthTenantId/-AuthClientId`, `-AuthAllowApiKey`, `-ControlApiKey`,
  `-CorsOrigins`, `-ControlPort` params; builds a `--secrets` array; sets the
  `SIM_CONTROL_*`/`SIM_AUTH_*` env; enables **external ingress** on the control
  port and prints the FQDN.
- `webapp/deploy.ps1` (updated) — generates `config.js` from the discovered
  backend FQDN + tenant/client/scope, vendors MSAL if missing, then `swa deploy`.
- `scripts/deploy-control-panel.ps1` (new) — master orchestrator: SWA →
  app-reg → container control API → webapp publish, idempotent end-to-end.

All four scripts pass `Parser::ParseFile` syntax validation.

## Earlier session (2026-06-02) — Entra ID login on the control panel (DONE, live)

Refactor: removed the manual API-base/X-API-Key entry from the panel, wired
the backend in permanently, and gated access behind **Microsoft Entra ID
login**. Only users assigned to the app registration in the tenant can sign
in and reach the control API; the API now validates a real Entra bearer JWT.

**App registration (tenant `39d764bc-ae80-46f9-b22c-6246cc5a20c2`):**
- Single-tenant SPA. Client/audience ID `91351088-042c-4d80-a8dd-3983979d70b3`
  (app object `4ae2e5e2-0100-4449-b591-832dbe9d5db2`, SP
  `28cec4d0-7493-4b80-b2f5-54b9e909ead5`).
- Scope `api://91351088-042c-4d80-a8dd-3983979d70b3/access_as_user`
  (id `b6f3c2a1-7d44-4e6b-9a21-0f8e5c2d1a90`), `requestedAccessTokenVersion: 2`.
- SPA redirect URIs: the SWA URL + `http://localhost:4280`.
- `appRoleAssignmentRequired: true` on the SP; **admin user assigned**
  (default access role). Admin consent pre-granted (oauth2PermissionGrant,
  AllPrincipals). Azure CLI app pre-authorized for the scope (for token tests).
- IDs stored git-ignored in `_local/_appreg.txt`.

**Frontend (`webapp/`):**
- `config.js` (new) — hardcoded `backendUrl`, `tenantId`, `clientId`, `scope`.
- `index.html` — removed the config card; added header sign-in/out + a
  "Sign in with Microsoft" card; loads MSAL.js v2 (CDN) → `config.js` → `app.js`.
- `app.js` — MSAL `PublicClientApplication` (localStorage cache),
  `loginRedirect`/`acquireTokenSilent`→`acquireTokenRedirect` fallback,
  `logoutRedirect`; every API call sends `Authorization: Bearer <token>`;
  401/403 surface "Access denied — not authorized".
- `styles.css` — header-right / user-info / sign-out / error styles.

**Backend (`simulator-cloud/src/`):**
- `server.py` — `JwtValidator` (PyJWT `PyJWKClient`, RS256, audience=client id,
  issuer `.../v2.0`). `create_app(..., validator=None, allow_api_key=True)`;
  new `require_auth` dependency: Bearer JWT when a validator is set (optional
  X-API-Key bypass via `allow_api_key`), else legacy X-API-Key. Added
  `validator_from_env()` / `allow_api_key_from_env()`. `/healthz` stays public.
- `cloud_runner.py` — builds the validator from env; control plane stays up
  when Entra auth is on even without a shared key; logs the auth mode.
- `requirements.txt` — added `pyjwt[crypto]>=2.8`.
- New env contract: `SIM_AUTH_ENABLED`, `SIM_AUTH_TENANT_ID`,
  `SIM_AUTH_CLIENT_ID`, `SIM_AUTH_ALLOW_APIKEY` (documented in `.env.example`).

**Live deploy (DONE — user authorized; user owns cost shutdown):**
- New image `acrsimnsb7uf.azurecr.io/simulator:auth1` (ACR build run `nf9`
  Succeeded; ignore the cosmetic Windows UnicodeEncodeError in the log stream).
- `az containerapp update -n ca-simulator -g rg-fabric-demo` → image `auth1`
  + env `SIM_CONTROL_ENABLED=1`, `SIM_AUTH_ENABLED=1`,
  `SIM_AUTH_TENANT_ID=39d764bc-…`, `SIM_AUTH_CLIENT_ID=91351088-…`,
  `SIM_AUTH_ALLOW_APIKEY=0` (shared key disabled). provisioningState Succeeded.
- Webapp republished via `swa deploy ./webapp --env production`.
- **Live validation passed:** `/healthz` → 200 (public); `/api/state` with no
  token → **401** (auth enforced); real Entra token (via
  `az account get-access-token --scope api://91351088-…/access_as_user`) →
  **200** with the 3 machines. Local FastAPI auth-dependency suite: 13/13 PASS.

URL (unchanged): `https://jolly-pebble-0d6f26703.7.azurestaticapps.net` —
now Entra-gated. Backend `https://ca-simulator.thankfulground-943b41a0.italynorth.azurecontainerapps.io`.

## Previous session (2026-06-02) — docs audit + SWA control panel


Two user tasks, done one at a time with incremental commits (all pushed).

**Task A — docs & dashboard audit (DONE).** Live fleet is **3 machines**
(M-001/M-002 synthetic + M-003 CNC); M-004 exists only as a trained
benchmark and is intentionally **not wired live** (decision locked). Audited
and corrected docs (`README.md`, `docs/architecture.md`,
`docs/model_architecture_options.md`, `tools/README.md`) to say 3 machines
and document the CNC machine. The real-time dashboard `rtd_telemetry_live`
was missing CNC coverage → added 3 CNC timecharts (`mandrino_load/power/
torque`) in `tools/04_create_dashboard.py` and applied them live
(idempotent update). Commits `23fefc2`, `91d6eb4`.

**Task B — Static Web App control panel (DONE, infra deploy is manual).**
Added a browser control panel that talks to a NEW control API on the cloud
simulator:
- `simulator-cloud/src/control.py` — thread-safe `ControlState` (per-machine
  random-anomaly toggle + manual injection queue + live status snapshot).
- `simulator-cloud/src/server.py` — FastAPI control server. Endpoints:
  `GET /healthz` (no auth), `GET /api/state`, `POST /api/machines/{id}/random`
  `{enabled}`, `POST /api/machines/{id}/inject` `{kind: spike|drift|stuck,
  sensor?}`. Auth = `X-API-Key` (demo-grade shared secret), CORS enabled.
- `simulator-cloud/src/simulate_machines.py` — per-machine effective anomaly
  prob from ControlState, consumes manual injections (`manual_overlay`:
  spike 0.5 s / drift 12 s / stuck 10 s), reports status. Added `--dry-run`
  (`SIM_DRY_RUN`, null producer) so it runs with no Event Hubs.
- `simulator-cloud/src/cloud_runner.py` — starts the control server thread
  when `SIM_CONTROL_ENABLED=1` (`SIM_CONTROL_API_KEY`, `SIM_CONTROL_PORT`
  default 8080, `SIM_CONTROL_CORS_ORIGINS`).
- `webapp/` — no-build HTML/CSS/JS panel (`index.html`/`styles.css`/`app.js`),
  `staticwebapp.config.json`, `README.md`, `deploy.ps1`. Polls `/api/state`
  every 2 s, per-machine cards (state + sensor values + random toggle +
  spike/drift/stuck buttons), offline banner when the container is
  unreachable.
- `infra/swa.bicep` — Free-SKU Azure Static Web App (validated `bicep build`).
- `.env.example` — added `SIM_CONTROL_ENABLED/PORT/API_KEY/CORS_ORIGINS`.

Validated locally end-to-end over real HTTP (dry-run sim + uvicorn on
:8080): healthz up, 3 machines with correct CNC sensors, random toggle
persists, inject accepted, and `401`/`422`/`404` rejections all correct.
The FastAPI TestClient suite also passed.

**Live deploy (DONE this session — user authorized; user owns cost shutdown).**
The control panel is now fully deployed and validated live on Azure:
- **Container** `ca-simulator` (RG `rg-fabric-demo`, region `italynorth`):
  rebuilt image `acrsimnsb7uf.azurecr.io/simulator:v2606011853` (ACR cloud
  build, verified Succeeded via `az acr task list-runs`), enabled **external
  ingress** on port 8080, set secret `control-api-key`, and env
  `SIM_CONTROL_ENABLED=1`, `SIM_CONTROL_PORT=8080`,
  `SIM_CONTROL_API_KEY=secretref:control-api-key`,
  `SIM_CONTROL_CORS_ORIGINS=https://jolly-pebble-0d6f26703.7.azurestaticapps.net`.
  Running revision `ca-simulator--0000003`.
  Control FQDN: `https://ca-simulator.thankfulground-943b41a0.italynorth.azurecontainerapps.io`.
- **Static Web App** `swa-anomaly-sim` (RG `rg-fabric-demo`, Free SKU,
  westeurope) deployed from `infra/swa.bicep`; `webapp/` published via SWA
  CLI (`swa deploy ./webapp --env production`).
  URL: `https://jolly-pebble-0d6f26703.7.azurestaticapps.net`.
- **Live validation passed:** `/healthz` ok (3 machines), `/api/state`
  returns M-001/M-002 (8 sensors) + M-003 (CNC: mandrino_load/power/torque),
  random toggle persists, manual inject queued, `401`/`422`/`404` correct,
  and CORS `Access-Control-Allow-Origin` matches the SWA origin.
- Demo API key stored git-ignored in `_local/_control_api_key.txt`.

**Cost note:** the container app + Fabric capacity remain running. The USER
will stop the container app and pause the Fabric capacity when minimizing
cost (per explicit instruction). When the container is stopped the panel
shows its offline banner.

Commits this session: `23fefc2`, `91d6eb4` (Task A), `11d4dee`, `82aa841`,
`a22f9d5`, `19c0284`, `5e19c66`, `f357afd` (Task B code), plus the live-deploy
doc update.

## Where we are

**Production-realistic 3-machine + 3-model architecture is live.** Cloud
simulator → Eventstream → KQL (`raw_telemetry`) → per-machine update policies
(`fn_score_demo_M001`, `fn_score_demo_M002`, `fn_score_demo_M003`) →
`anomalies` → real-time dashboard. One ONNX model per machine, each with its
own scaler and threshold read from the model metadata.

**M-003 is a real-data CNC spindle machine** (3 sensors: `mandrino_load` %,
`mandrino_power` kW, `mandrino_torque` N*cm) whose normal behaviour is
driven by a recorded CNC profile (`data/cnc_profile_M-003.json`, derived
from `_data_local/`). M-001/M-002 remain the synthetic 8-sensor machines.
Validated end-to-end on 2026-06-01: M-003 ingests (3 sensors), scores
~1.17 on normal data (threshold 1.882), and an injected drift produced an
anomaly at score 9.79 (~5.2× threshold) landing in the `anomalies` table.

### Why we pivoted (was: "retrain bigger model")
The previous PLAN diagnosed the 17.7% recall as a window/model capacity
issue. Real cause: the scoring function `fn_score_demo()` was hardcoded
to `machine='M-001'` (in `kql/04_update_policy.kql` line 28). All true
positives were on M-001; all M-002..M-005 anomalies were false negatives
purely because they were never scored. Confirmed via
`python tools/06_correlate.py --lookback 2h`.

Rather than re-hardcode for 5 machines, we cut the fleet to a
production-realistic shape: 2 machines + 1 dedicated model each.

### Fabric environment (do NOT modify without confirmation)
- Workspace `anomaly-detection-fresh` (id `35627f40-dcb7-4346-b867-1b04603a8094`),
  capacity F4 `anomalydetectiondemo`, RG `fabric-anomaly-detection`,
  region `italynorth`.
- KQL DB `kql_telemetry` (id `142c5513-05ab-4762-8e9a-3fe60bd5bf3c`),
  cluster `https://trd-53389re9vz38nbzpgn.z5.kusto.fabric.microsoft.com`.
- Eventstream EH endpoint (the working one used by both legacy and new sim):
  `esehitnfrrdlj1y644v1isl.servicebus.windows.net` /
  `esehitnfrrdlj1y644v1isl_eh`. This is the value in `.env` ->
  `EVENTSTREAM_CONNECTION_STRING`.
- Dashboard `rtd_telemetry_live` (id `3dc83f28-04ed-4cd4-b77d-5c98c7ade918`).

### Container Apps (cloud simulator)
Two apps existed at start; only ONE is active now.

| RG | App | Revision | State | Notes |
|---|---|---|---|---|
| `fabric-anomaly-detection` | `ca-simulator` | `ca-simulator--0000002` | **deactivated** | legacy 5-machine sim, deactivated 2026-05-21 |
| `rg-fabric-demo` | `ca-simulator` | `ca-simulator--v2606011249` | Running | 3-machine sim (M-001/M-002 synthetic + M-003 CNC), image `acrsimnsb7uf.azurecr.io/simulator:latest` |

Env on the active app: `SIM_MACHINES=3`, `SIM_RATE=1.0`,
`SIM_ANOMALY_PROB=0.0005` (restored to demo rate),
`SIM_CNC_PROFILE=/app/cnc_profile_M-003.json` (drives M-003 from the real
CNC profile baked into the image).

### Models
Two artifacts in `models/`, each trained from
`data/training/telemetry_wide.parquet` filtered to one machine
(via `tools/train_per_machine.py`, GPU, 12 epochs, ~25 s each):

| Dir | Model name | Machine | Sensors | Threshold (p99.5 val) |
|---|---|---|---|---|
| `models/transformer_ae_small__M-001/` | `transformer_ae_small__M-001` | M-001 | 8 (synthetic) | 1.00679 |
| `models/transformer_ae_small__M-002/` | `transformer_ae_small__M-002` | M-002 | 8 (synthetic) | 0.98171 |
| `models/transformer_ae_small__M-003/` | `transformer_ae_small__M-003` | M-003 | 3 (real CNC) | 1.88170 |

M-003 (`n_parameters=161 419`, 3 input features) was trained on telemetry
reconstructed from the real CNC profile in `data/cnc_profile_M-003.json`
(provenance: `_data_local/`, git-ignored). Registered to the live `models`
table at version 1 (FP16 ONNX, 491.9 KB raw / fits the Kusto 1 MB budget).

(Old `models/transformer_ae_small/` is left in place but is no longer
wired into any KQL update policy. Its threshold 0.0154 is not comparable
because it was trained on all 10 machines with a single combined scaler;
the new per-machine scalers are tighter and produce scores ~65x larger.)

Architecture (identical for both): TransformerAE, WINDOW=64, D_MODEL=56,
4 heads, 2 enc + 2 dec, FF_DIM=160, ~161 984 params. Score function
baked into ONNX: `per_sensor.max(dim=1)` of MSE mean-over-time-per-feature.
Both `model.fp16.onnx` are 493 KB raw / 657 KB base64 (fits Kusto's
1 MB row budget).

### KQL pipeline
- `kql/04_update_policy.kql` — REPLACED. Defines three per-machine
  scoring functions:
  - `fn_score_demo_M001()` calls `score_multivariate_onnx_batch(
    model_name='transformer_ae_small__M-001', machine='M-001', bin=1s,
    threshold=<from metadata>)`.
  - `fn_score_demo_M002()` analogous for M-002.
  - `fn_score_demo_M003()` analogous for M-003 (3-sensor CNC model).
  All three are attached to the `anomalies` update policy. The file also
  drops the legacy `fn_score_demo` and `fn_score_multivariate_demo`
  functions (`ifexists`).
- `kql/05_multivariate_mv.kql` — the materialized view
  `raw_telemetry_wide_mv` and the helper functions are kept (useful
  for ad-hoc queries), but `fn_score_multivariate_demo()` and its
  update-policy attach are REMOVED. The MV is no longer wired into
  the live scoring path.
- `kql/03_scoring_functions.kql` — unchanged; `score_multivariate_onnx_batch`
  already reads the scaler from `metadata.scaler` and normalises sensors
  before invoking ONNX (this matches `tools/train_per_machine.py`).

### Tools changed / added
- `tools/train_per_machine.py` — NEW. Trains one model per machine from
  the existing training parquet (no data regen needed).
- `tools/purge_obsolete_machines.py` — NEW. Tried to `.purge` rows for
  M-003..M-005; **failed with 403 Forbidden** (operation
  `PurgeTableRecordsCommand` not allowed for our Entra principal in this
  Fabric Eventhouse). Skipped: old rows age out with the 30-day
  softdelete retention and live correlation uses a short lookback.
- `simulator-cloud/deploy.ps1`, `simulator-cloud/src/simulate_machines.py`,
  `simulator-cloud/src/cloud_runner.py`,
  `simulator-local/simulate_machines.py` — defaults changed from
  5 machines to 2, then extended for M-003 (3rd machine, real CNC).
  `deploy.ps1` now takes `-CncProfile` (default
  `/app/cnc_profile_M-003.json`), injects `SIM_CNC_PROFILE`, and skips the
  Fabric-capacity RG/region lookup when both `-RgName` and `-Location` are
  passed (the simulator lives in a different subscription than the Fabric
  capacity, so the lookup would otherwise fail).
- `tools/inject_anomaly.py` — used to validate M-003 end-to-end by
  injecting aligned high-value slices on all 3 CNC sensors
  (`mandrino_load/power/torque`); the multivariate window needs all
  sensors present, so single-sensor injection is not enough.

### This session (2026-06-01) — M-003 wiring + the real reason ingestion stalled
- **Root cause of “no data since May 27”:** the eventstream destination
  `kql_raw_telemetry` (id `2ba1d00a-c072-4391-9439-06b49d213864`) on
  eventstream `es_machines` (id `b55b0263-0916-4366-9931-a7da43bd4a47`) was
  **Paused** — NOT a simulator or model problem.
  `tools/_check_topology.py` auto-resumes any Paused destination
  (`POST /eventstreams/{id}/topology/destinations/{id}/resume` with
  `{"startType":"Now"}`, HTTP 200). After ~75 s batching, all 3 machines
  resumed ingesting.
- M-003 model registered to live `models` table (version 1) via
  `tools/05_register_model.py models/transformer_ae_small__M-003`.
- `kql/04_update_policy.kql` applied live via
  `tools/02_setup_kql_tables.py kql/04_update_policy.kql` (6 commands OK).

### Cloud training on Azure ML (NEW, 2026-06-01)

The per-machine models can now be (re)trained **entirely in the cloud** on
Azure ML — no local GPU and no training parquet required (synthetic 8-sensor
telemetry is generated from the repo simulator physics inside the job).

- AML stack deployed via `infra/ml-workspace.bicep` to **westeurope**, RG
  `rg-anomaly-ml-westeurope`: workspace `anomalyml-mlw`, storage
  `stanomalymlfknlnf4v`, KV, App Insights, Log Analytics, compute cluster
  `cpu-cluster` (`Standard_DS3_v2`, min=0 max=1, scale-to-zero).
- Code: `cloud-training/generate_and_train.py` (self-contained: sim physics
  + train + FP16 ONNX export), `cloud-training/conda.yml`,
  `cloud-training/submit_job.py` (azure-ai-ml SDK: submit + poll + AAD
  artifact download). `cloud-training/job.yml` is reference only.
- Run it: `.\.venv\Scripts\python.exe cloud-training/submit_job.py`
  → trains M-001 + M-002, downloads into `models/transformer_ae_small__<M>/`.
- Last run `loyal_juice_mps6ccsph1` (Completed): M-001 val 0.158 / thr 1.475,
  M-002 val 0.147 / thr 1.029; both FP16 ONNX Kusto-fit, parity rel diff
  <0.2%. NOTE: thresholds differ from the local-trained values above because
  the cloud job uses freshly generated synthetic data.

**Gotchas captured (see also `/memories/session/azure-ml-training-plan.md`):**
- Host is Windows **ARM64** → `az extension add -n ml` hangs (no win-arm64
  wheels for native deps). Use the **azure-ai-ml Python SDK**, not the CLI ext.
- Storage has `allowSharedKeyAccess=false` → SDK `jobs.download` (key auth)
  fails. Download artifacts/logs via **AAD** (`BlobServiceClient` +
  `AzureCliCredential`); the signed-in user needs **Storage Blob Data
  Contributor** on the storage account.
- conda pins `torch==2.3.1` → `torch.onnx.export(dynamo=...)` (torch ≥2.5)
  is NOT supported; do not pass `dynamo=`.
- **All GPU AML quota is 0** in westeurope/italynorth/swedencentral/northeurope
  (separate BatchAI "Cluster Dedicated" quota). Min GPU to request:
  `Standard_NC4as_T4_v3`, quota "Standard NCASv3_T4 Family Cluster Dedicated
  vCPUs" ≥ 4 in westeurope. The model is tiny so CPU training is ~2 s/epoch.
- Old `rg-anomaly-ml-italynorth` RG is unusable (no per-family AML quota) —
  safe to delete.

## Outstanding housekeeping

1. **Validate live correlation** with the new pipeline. Run after at
   least 15 min of fresh ingestion:
   ```powershell
   .\.venv\Scripts\python.exe tools\06_correlate.py --lookback 30m --grace 2m
   ```
   Target: Precision >= 95% and Recall >= 60% on M-001, M-002 and M-003.
   (At `SIM_ANOMALY_PROB=0.0005` natural anomalies are rare, so a short
   window may show few/no labelled events; lengthen the lookback or
   temporarily raise the rate if you need a populated correlation.)

2. **Demo anomaly rate is set to 0.0005** (restored this session). No
   action needed unless you bump it for a faster validation run.

3. **Optional cleanup** — the legacy container app
   `fabric-anomaly-detection/ca-simulator` has all revisions deactivated
   and `min-replicas=0`. Safe to delete the whole app + its ACR
   `acrsim3l8kge` if you want to tidy up subscription cost.

4. **`.env` was updated** with the working EH connection string
   (`esehitnfrrdlj1y644v1isl_eh`); the previous value
   (`esehitnfw9yg51rnseq802n_eh`) was stale and DNS-unresolvable. Keep
   the new value; the new simulator app's secret is also synced.

## Resume protocol

```powershell
cd <repo-root>
git pull
.\.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
.\.venv\Scripts\python.exe tools\06_correlate.py --lookback 30m --grace 2m
```

## Conventions to remember
- The user works across two PCs via VS Code Remote Tunnels; chat history
  doesn't sync — that's why this folder exists.
- Auth helper: `tools/_fabric_auth.get_credential(tenant, scope, repo_root)`.
  Each script defines `SCOPE = "https://api.fabric.microsoft.com/.default"`
  locally.
- `.\.venv\Scripts\Activate.ps1` does NOT actually swap `python` in pwsh
  terminals on this PC. Always invoke the venv interpreter explicitly:
  `.\.venv\Scripts\python.exe ...`.
- KQL gotchas: `kind` is reserved (use `anomaly_kind`); `last` is reserved
  (use `last_at`); adjacent
  `.alter` commands need blank lines; no inline `//` comments inside
  `.create-merge table (...)` column lists; `tools/02_setup_kql_tables.py`
  splits commands on blank lines only — keep them between `.drop`s.
- **`az acr build` on Windows ARM64 crashes client-side** with
  `UnicodeEncodeError: 'charmap' ... cp1252` in colorama log streaming
  (chcp 65001 / no_color / PYTHONUTF8 do NOT fix it). The build still
  **succeeds server-side** — verify with
  `az acr task list-runs -r acrsimnsb7uf --top 5 -o table`, then update the
  Container App directly with `az containerapp update` (no log streaming).
