# Copilot instructions — anomaly-detection-fabric-demo

This file is auto-loaded by VS Code Copilot Chat for every conversation in
this workspace. Keep it short and stable; put volatile per-session notes in
`.copilot/` (see below).

## How to work in this repo

- **Read [`.copilot/README.md`](../.copilot/README.md) first.** It explains
  the session-handoff convention and points to `STATE.md`, `PLAN.md`, and
  `CONTEXT.md`.
- After every meaningful change of plan or completed milestone, **update**
  `.copilot/STATE.md` and `.copilot/PLAN.md` so the next session (possibly
  on a different machine) can resume without re-reading the chat history.
- Do not invent file paths or APIs. When unsure, search the repo
  (`grep`/`file_search`) or read the relevant file first.

## Repo conventions

- Python sources use type hints, `from __future__ import annotations`, and
  follow PEP 8. Keep imports sorted (stdlib → third-party → local).
- PowerShell scripts source [`scripts/lib/env.ps1`](../scripts/lib/env.ps1)
  to load `.env` and use `Assert-EnvVars` for required keys. Do not
  reimplement env loading inline.
- Bicep lives under `infra/`. Validate with `bicep build` before committing.
- Notebook cells must remain runnable top-to-bottom in Fabric (no hidden
  state from prior runs).
- Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`,
  `chore:`, `refactor:`).

## Hard rules

- **Never** modify the existing Fabric environment without explicit user
  confirmation. Fixes go in scripts/code; environment changes are manual.
- **Never** delete `.env` or commit secrets. `.env` is gitignored;
  `.env.example` is the contract.
- When a change spans more than one file or area (simulator + KQL + notebook),
  propose the plan first and update `.copilot/PLAN.md` before coding.
