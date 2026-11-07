# local-models hub

A door on `localhost:1235` in front of LM Studio, shared by every project and every Claude Code session on the Mac: it takes turns, names each request by its project and session, keeps model copies loaded, reloads and recycles them across engine crashes, runs local read-only agents, and serves one dashboard. The repo is also a Claude Code plugin and its own marketplace. The guide for programs that use it is [GUIDE.md](GUIDE.md); the overview is [README.md](README.md).

## Layout
- `localmodels/gateway.py` — the door: names each request, takes a slot, passes it to LM Studio, logs one line, holds and resends across a crash.
- `localmodels/llmshare.py` — the sharing rules: slot locks, leases, the swap lock, the model listing, the readers of the shared state.
- `localmodels/models.py`, `crashes.py`, `recycle.py` — the keeper: copies kept loaded, crashed models loaded again, copies recycled before the crash band.
- `localmodels/agent.py` — local agents: the tool loop, read-only file tools, JSON answers, retries.
- `localmodels/mcp_server.py` — the MCP tools and the detached job runner.
- `localmodels/whois.py`, `sessions.py` — who is behind a connection; every Claude Code session on the Mac.
- `localmodels/web.py`, `dash.html`, `dash.py` — the dashboard and the terminal dash.
- `localmodels/cli.py`, `bin/localmodels` — the command.
- `tests/` — sandbox tests with a fake LM Studio; `tools/` — measurements.

## Working on it
- After any change: `localmodels check` (it runs the sandbox tests against the installed door), then `localmodels install` (launchd runs a copy of the code; the dash says *runs older code* until then). A new rule gets a sandbox test first.
- Standard library only, Python 3.9 or later. No dependencies.
- Never stop LM Studio's server. Never unload or reload a model another run is using (`localmodels swapcheck`). Never put a prompt, an answer or a file's contents in the hub's records.
- Plain words in anything a person reads: the dash, the command's output, error messages.
- Commit only this repo's files. Nothing personal, no other project's name, no narration in anything committed; local notes go in `CLAUDE.local.md`, which git ignores.
