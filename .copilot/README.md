# `.copilot/` — session handoff for Copilot Chat

VS Code Copilot Chat history is **not** synced across machines. This folder
is the bridge: it lets a session started on one PC (or after a Copilot
context reset) pick up where the previous one left off.

## Files in this folder

| File | Purpose | Update cadence |
|---|---|---|
| [`STATE.md`](STATE.md) | Snapshot of *what we're doing right now*: last completed step, current focus, blockers. | After every meaningful step. |
| [`PLAN.md`](PLAN.md) | The active multi-step plan with checkboxes. | When a plan is agreed; check items as they complete. |
| [`CONTEXT.md`](CONTEXT.md) | Stable knowledge a future session needs but can't easily rediscover: architecture decisions, gotchas, environment IDs, naming conventions. | Rarely; only when a long-lived fact changes. |

## Instructions for the next Copilot session

When you (Copilot) start a new conversation in this workspace:

1. **Read [`STATE.md`](STATE.md)** to learn what the user was last doing.
2. **Read [`PLAN.md`](PLAN.md)** to see the agreed plan and its progress.
3. **Skim [`CONTEXT.md`](CONTEXT.md)** for stable facts (IDs, conventions,
   environment layout). Don't re-derive things already documented there.
4. Greet the user with a one-line summary of where things stand
   (e.g. *"Resuming the simulator/training redesign — Phase 1 not started,
   6 open questions in PLAN.md."*) and ask what to work on.
5. As work progresses, **update these files**:
   - Tick off items in `PLAN.md`.
   - Refresh `STATE.md` whenever the focus shifts.
   - Append new stable facts to `CONTEXT.md`.
6. **Commit the updates** alongside the related code changes so they land
   on `main` and are visible from any clone.

## What does *not* belong here

- Full chat transcripts (use VS Code's "Export chat" if you need them).
- Secrets, connection strings, tokens.
- Long-form documentation that belongs in `docs/`.
- Generated artifacts or logs.

Keep each file under a few hundred lines. If `CONTEXT.md` grows too big,
split it into topic files (`CONTEXT-kql.md`, `CONTEXT-training.md`, ...).
