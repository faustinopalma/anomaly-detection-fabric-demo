# Current state

_Last updated: 2026-05-15_

## Where we are

The **Fabric capacity provisioning** workstream is **complete**:

- `infra/fabric-capacity.bicep` — Bicep template for `Microsoft.Fabric/capacities`.
- `scripts/create-capacity.ps1` — wrapper that reads `.env` (via shared
  `scripts/lib/env.ps1`), uses device-code auth, defaults to F4 in
  `italynorth`, and runs the Bicep deployment.
- Pushed in commit `5c9f196` on `main`.

The **simulator + training redesign** workstream is **planned, not started**.
See [`PLAN.md`](PLAN.md) for the 4 phases. Six open questions are listed
there and need answers from the user before any code is written.

## Active focus

Waiting on the user's answers to the 6 open questions in `PLAN.md`
(Section "Open questions").

## Recent context the user might mention

- The user often works across two machines via VS Code Remote Tunnels.
  Chat history does not sync. That's why this folder exists.
- The current Fabric environment (capacity `anomalydetection`, workspace
  `anomaly-detection-dev`) must **not** be modified without explicit
  confirmation — fixes go in scripts/code only.
- A previous deploy bug (KQL DB linked to a wrong Eventhouse via
  `parentEventhouseName`, creating a `<dbname>_auto` orphan) was fixed
  in `scripts/deploy.ps1` (commit `be48112`) by switching to
  `parentEventhouseItemId=<GUID>` lookup. The orphan in the live env was
  left in place on purpose.

## Not yet done (carry-over)

- Test the fixed `scripts/deploy.ps1` on a fresh capacity (the user can
  now provision one with `pwsh ./scripts/create-capacity.ps1`).
- GPU patches in notebook 05 (`device = torch.device('cuda' if ...)`).
  The user has tunneling set up but hasn't asked for the patch yet.
