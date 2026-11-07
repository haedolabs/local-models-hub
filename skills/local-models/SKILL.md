---
name: local-models
description: The rules for using the local models (LM Studio) on this Mac through the local-models hub. Use whenever a task involves a local model, LM Studio, port 1234 or 1235, a local agent or batch over a project's files, loading or unloading a model, or the question of which model is loaded. Covers where to send requests, the MCP tools (status, guide, ask, batch, job_status, swap_check), leases, swaps, and what happens when a model crashes.
---

# Local models on this Mac, through the hub

- **Send requests to `http://localhost:1235/v1`** (the hub's door: LM Studio's own API, same paths), never straight to LM Studio's port 1234. The door takes turns for everyone, names your project and session itself, and logs who used what, never the content. Optional headers: `X-Project` (a name), `X-Kind` (`batch`, `chat`, `ask`, `eval`, `test`), `X-Step` (what this request is for, shown on the dashboard).
- **Ask before you assume.** The `status` tool says which models are loaded, how many answers at once they give, who is using them now, and whether anything is down after a crash. The `guide` tool gives the full rules (`section=...` for one part).
- **Bulk reading jobs go to local agents**: the `ask` tool (one question, read-only tools over this project's folder, waits up to two minutes) and `batch` (many jobs side by side; follow with `job_status`). Their answers are leads to check, never verdicts: models invent quotes and line numbers. `think=false` makes lookups and extraction several times faster; keep thinking on for checks and judgment.
- **A batch should send as many requests at once as the loaded copies answer** (the `status` tool says; the door queues the rest). A long run that pauses between requests holds a lease (`guide section=lease`).
- **Never stop LM Studio's server. Never unload or reload a model without `swap_check`**; if it names anyone, do not swap. The simplest swap is `localmodels use <model>` in a terminal.
- **If a model crashes, the hub loads it again** within a minute and holds new requests for it meanwhile; a request that was being answered is resent once, and if the model is still down the door answers 503 with `Retry-After`. The hub also recycles each copy of a model before the amount of work at which the engine has crashed, one copy at a time, so a long batch keeps going.
- **See it:** the dashboard at http://localhost:1235 (`localmodels open`), or `localmodels` in a terminal.
- **If the door is closed** (`status` says so): `localmodels start`. If the tools are missing entirely, the hub is not set up on this Mac yet: `/local-models:setup`.
