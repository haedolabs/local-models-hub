# local-models hub

A gateway on `localhost:1235` in front of LM Studio, shared by every project and every Claude Code session on the Mac: admission control across all clients, per-request attribution by project and session, model replicas kept loaded, crash reloads and pre-emptive recycling, read-only local agents, and one dashboard. The repo is also a Claude Code plugin and its own marketplace. The guide for clients is [GUIDE.md](GUIDE.md); the overview is [README.md](README.md).

## Layout
- `localmodels/gateway.py` — the gateway: attributes each request, acquires a slot, forwards to LM Studio, logs one line, holds and resends across a crash.
- `localmodels/llmshare.py` — the sharing rules: slot locks, leases, the swap lock, the model listing, the readers of the shared state.
- `localmodels/models.py`, `crashes.py`, `recycle.py` — the instance manager: replicas kept loaded, crashed instances reloaded, instances recycled before the crash threshold; `crashes.ENGINE_WORDS`, the one list of the engine's failure words.
- `localmodels/admission.py` — admission by context size: each request's estimated context, each instance's budget of context in flight from its model's cost per token and the Mac's memory.
- `localmodels/runs.py`, `maint.py` — run and phase progress with finish times, run plans (`/hub/runs`) and per-phase estimates in tokens per item; maintenance windows and the switched-off state (`localmodels off` / `on`; a peer's, `localmodels peer off` / `on <name>`).
- `localmodels/peers.py` — other Macs' gateways as more replicas of a family: the shared token, a peer's report, the front's link (polls, backoff, free slots, model fingerprints), the network checks.
- `localmodels/agent.py` — local agents: the tool loop, read-only file tools, JSON answers, retries.
- `localmodels/mcp_server.py` — the MCP tools and the detached job runner.
- `localmodels/whois.py`, `sessions.py` — who is behind a connection; every Claude Code session on the Mac.
- `localmodels/web.py`, `dash.html`, `dash.py` — the dashboard and the terminal dash.
- `localmodels/names.py`, `errors.py`, `demo.py`, `icon.py` — display names and screenshot mode; the error recorder; the synthetic workload of `localmodels demo`; the icon (a chip: sapphire die, off-white pins) as PNG and SVG; `python3 -m localmodels.icon` writes `brand/` and `docs/icon.png`.
- `localmodels/cli.py`, `bin/localmodels` — the command.
- `app/` — the optional menu-bar app (Swift and AppKit, no packages): `Glance.swift` turns `/hub/snapshot` into the icon, the menu and the notifications (tested by `app/tests/main.swift`, run with the sandbox suites when `swiftc` is present); `main.swift` is the app. `localmodels app install` builds it on the Mac with the Command Line Tools and signs it ad hoc.
- `tests/` — sandbox tests with a fake LM Studio (two gateways for peers); each imports `tests/_sandbox.py` first, so its state folder, port and LM Studio are temporary; `tools/` — measurements.
- `docs/` — the README's pictures, taken in screenshot mode (`localmodels present on`) during `localmodels demo`.

## Working on it
- After any change: `localmodels check` (it runs the sandbox tests against the installed gateway), then `localmodels install` (launchd runs a copy of the code; `localmodels` reports *stale code* until then). A new rule gets a sandbox test first.
- Standard library only, Python 3.9 or later. No dependencies.
- Never stop LM Studio's server. Never unload or reload a model another run is using (`localmodels swapcheck`). Never put a prompt, a completion or a file's contents in the hub's records.
- Precise technical terms in anything a person reads (the dashboard, CLI output, error messages, MCP tool text): its readers are engineers. Name things by what they are (gateway, instance, replica, slot, admission queue, lease, recycle), not by metaphor ("door", "keeper").
- The look is Haedo Labs': near-black `#111110`, off-white `#ece7df`, the grey `#8d867c`, the hairline `#262522`, and Haedo Labs Sapphire `#0f52ba` as the one color (light theme: the same on off-white paper); it is the default of the dashboard's color setting, which can take the Mac's system accent (`/hub/accent`) or a color of one's own instead (`applyAccent` in dash.html redraws every step from it). Series and project colors are steps of sapphire (its OKLab hue, lighter or darker, neighbours apart); "other" is a neutral. Type is Libre Franklin, 400 and 500 only (served by the gateway, SIL OFL), small labels in tracked capitals; the name is drawn as outlines. Behind it, a faint circuit board in sapphire (`localmodels/pcb.svg`, one seamless tile drawn by `tools/pcb.py`). No red anywhere: states are told apart by words and position first, warning in the beige-grey `#bca496`, critical in the strongest ink; the terminal dash uses the same palette. Motion is slow and subtle and stops under reduced motion. Colors come from the theme tokens, never literals.
- Commit only this repo's files. Nothing personal, no other project's name, no narration in anything committed; local notes go in `CLAUDE.local.md`, which git ignores.
- Release only through `localmodels publish`: it runs the sandbox tests, `claude plugin validate` and the privacy check before it pushes. A plain `git push` skips all three.
- When a default, module, command or endpoint changes, update every document that states it (README, GUIDE, CLAUDE.md, the CLI help): search them for the old value.
- Look at what ships before it ships: render the README with GitHub's stylesheet, open each generated image at full size, read CLI output as its reader would. After a release, load the repository page signed out, in both themes.
- Verify every load and unload against LM Studio's listing afterwards: an unload by identifier takes the oldest instance of that model key.
