# local-models hub

Reverse proxy, instance manager and MCP server for one LM Studio shared by every process and Claude Code session on an Apple-silicon Mac.

LM Studio serves local models on `localhost:1234`. The moment two programs use it at once, things go wrong quietly: requests pile up past what the model answers at once and time out, one program reloads a model another is using, the engine crashes in the night and nothing loads the model again, and nobody can see who is using what. The hub is a small door in front of LM Studio that fixes those four things and shows the whole picture.

Standard-library Python, no dependencies. macOS on Apple silicon (it leans on launchd, `lms` and the Mac's own process facts). MIT.

## What it does

- **A door on `localhost:1235`** with every API LM Studio serves: OpenAI's (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/responses`), Anthropic's (`/v1/messages`) and LM Studio's own (`/api/v1/...`, `/api/v0/...`). Point any client at it instead of 1234; nothing else changes. Streaming passes through, and each API's answers are counted and its errors shaped the way its own clients read them.
- **Turns.** All programs together never send a model more requests than it answers at once (its *Max Concurrent Predictions*); the rest wait their turn instead of timing out. A person's chat gets the next free slot ahead of a batch's queue. A program that says how long it will wait (`X-Timeout`) gets `503` with `Retry-After` before that runs out.
- **Names.** Every request is named by the project (from the program's folder) and, when Claude Code started the program, by the Claude Code session — read from process facts, so nothing has to report itself. Optional headers `X-Project`, `X-Kind` (batch, chat, ask, eval, test) and `X-Step` say more.
- **Copies.** A model can be loaded several times (`qwen/qwen3.6-35b-a3b` and `qwen/qwen3.6-35b-a3b:2`); the door treats the copies as one model, fills the first copy's slots before the second's, and the keeper keeps the copies loaded whenever the first one is.
- **Crashes.** When LM Studio's engine crashes and takes a model with it, the keeper notices within seconds (it reads LM Studio's own log), loads the model again, routes around the copy that is down meanwhile, and resends a request that was being answered. Still failing, the program gets `503` with `Retry-After`, never a bare `400`. The crash is recorded and shown.
- **Recycling before the crash.** The engine's Metal buffer count grows with the work a copy has done until it crashes; the keeper recycles each copy (drain, unload, load) before that band, one copy at a time, the way web servers recycle leaky workers, and a batch can ask for fresh copies when it starts.
- **Swaps that respect others.** Before a model is unloaded or reloaded, the hub says who is using it; a swap under another run is refused.
- **Local agents.** `ask` and `batch`: a tool-using loop on the loaded model with tools that only read a project's folder (read, list, grep), answers as JSON matched to a schema, logs per job. A job whose request fails because the model is down waits and tries again for up to an hour; an answer that lists nothing is asked for once more; the JSON step runs with thinking off. Every Claude Code session gets them as MCP tools; a terminal gets `localmodels ask` and `localmodels batch`.
- **Batch progress.** A program sends `X-Run`, `X-Run-Total` and `X-Run-Done`, and the dashboard shows the whole batch and the job it is on now, each with a bar, the time left and the finish time in local time, measured on this Mac. A step that reads `extract 8 of 360` shows the job alone, with no headers.
- **Maintenance.** `localmodels maintenance in 15m for 30m <why>` tells every program (a header on every answer) and every Claude Code session (a hook) ahead of time; while it is on, new requests get `503` with `Retry-After` and the keeper stands down; `off` loads the models back.
- **Awake.** While any program is using a model the door holds the Mac awake (`caffeinate`).
- **One dashboard** at `http://localhost:1235`: each copy's slots coloured by project and its work against its recycle band, every program using a model with its step, every Claude Code session, charts over the last 15 minutes to 24 hours, crashes and recycles, the Mac. It fits one screen. Nothing it shows is anyone's content: the hub keeps names, times and token counts, never a prompt or an answer.
- **One command:** `localmodels` (status), `dash`, `log`, `crashes`, `maintenance`, `swapcheck`, `use`, `keep`, `recycle`, `ask`, `batch`, `jobs`, `prune`, `check`, `install`, `publish`.

## Install

**Before you start:** a Mac with Apple silicon, [LM Studio](https://lmstudio.ai) with at least one model downloaded, its command-line tool (open LM Studio once, then run `~/.lmstudio/bin/lms bootstrap`), and `python3` 3.9 or later. In LM Studio's developer settings, turn **just-in-time model loading** off (the door never needs it).

**The short way:** in Claude Code, say *"Set up the local-models hub from github.com/matthaedo/local-models-hub."* Claude reads the next section and does the rest.

**If you are Claude Code setting this up for someone:**
1. Check the ground: `uname -m` says `arm64`; `~/.lmstudio/bin/lms ls` lists at least one model (if `lms` is missing, ask the person to open LM Studio once and run `~/.lmstudio/bin/lms bootstrap`); `python3 --version` is 3.9 or later. Stop and say what is missing rather than working around it.
2. Slash commands are the person's to type. Ask them to type the two plugin lines below; if they would rather not use the plugin, clone the repo and run `bin/localmodels install` yourself (the terminal way below).
3. Run `localmodels check` and show its lines as they are. If anything fails, read `~/Library/Logs/LocalModels/gateway.log` and say what it says.
4. Show the settings the install wrote (`~/Library/Application Support/LocalModels/models.json`, fitted to this Mac's memory and downloaded models) and ask whether that model is the one they want kept loaded.
5. Ask the person to turn just-in-time model loading off in LM Studio's developer settings.
6. Tell them, in plain words: send requests to `http://localhost:1235/v1` instead of port 1234 (any API key); the dashboard is at http://localhost:1235; the MCP tools appear in Claude Code sessions from the next session on.
7. Never stop LM Studio's server, and never unload or reload a model the person did not ask you to.

**With Claude Code** (the repo is a Claude Code plugin and its own marketplace), in any session:

```
/plugin install local-models --marketplace matthaedo/local-models-hub
/local-models:setup
```

The first line installs the plugin (the MCP tools and a skill that teaches the session the rules); the second has Claude open the door on this Mac and run the self-test. On Claude Code before 2.1.275: `/plugin marketplace add matthaedo/local-models-hub`, then `/plugin install local-models@local-models-hub`.

**Without Claude Code**, in a terminal:

```bash
git clone https://github.com/matthaedo/local-models-hub.git ~/local-models
~/local-models/bin/localmodels install
```

`install` opens the door at login (a LaunchAgent running a copy of the code from `~/Library/Application Support/LocalModels/hub/`), writes the settings file if there is none (fitted to this Mac: the measured default when its model is downloaded and fits twice, else the largest downloaded model that fits, as two copies when two fit), registers the MCP tools for Claude Code if `claude` is on your PATH, adds one signpost line to `~/.claude/CLAUDE.md`, and puts `localmodels` on your PATH. `--dry` says what it would do; `--no-claude` leaves Claude Code alone. Run it again after changing the code; it waits for a moment with no request passing through before restarting the door. `localmodels uninstall` undoes all of it. `localmodels check` is the self-test.

Then point clients at `http://localhost:1235/v1` (any API key). In Claude Code sessions the MCP tools `status`, `guide`, `ask`, `batch`, `job_status` and `swap_check` appear under `local-models`.

**Publishing a new version** (the maintainer): `localmodels publish` (patch bump) or `localmodels publish --version X.Y.0` runs the sandbox tests, `claude plugin validate`, and a privacy check, then stamps the version, commits, tags and pushes; plugin users get it on `/plugin update`. The privacy check reads every line added since the last tag, every new file name and every commit message against a word list kept outside the repo (`~/Library/Application Support/LocalModels/private-words.txt`: one regex per line, `allow: <regex>` for exceptions) and refuses to push on a match. `--dry` runs the checks only.

## Settings

`~/Library/Application Support/LocalModels/models.json` (written on the first install, fitted to this Mac's memory and downloaded models; the repo's `localmodels/models.json` is the measured default it starts from):

```json
{
 "families": {
  "qwen/qwen3.6-35b-a3b": {"key": "qwen/qwen3.6-35b-a3b", "copies": 2, "context": 65536, "parallel": 4,
                           "recycle": {"tokens": 800000, "soft": 560000, "before_batch": 0.5}}
 },
 "batch_at_once": 8,
 "headroom_gb": 8
}
```

A *family* is a model the keeper manages: how many copies, with what context and how many answers at once each, and when to recycle a copy: `tokens` processed (prompt and answer) since it was loaded drains and reloads it; past `soft` it is reloaded whenever it is idle; at a batch's start a copy past `before_batch` of its band goes first; `"recycle": false` turns it off. `batch_at_once` is what a batch sends at once; `headroom_gb` is the memory the keeper leaves free when it loads a copy. The door reads the file as it changes; a file that cannot be read leaves the last good settings standing and is said on the status line and the dash.

Environment: `LOCALMODELS_PORT` (default 1235), `LMSTUDIO_URL` (default `http://localhost:1234`), `LMSTUDIO_LOGS` (LM Studio's server logs, for crash detection), `LOCALMODELS_STATE` (the shared state folder), `LOCALMODELS_LABEL` (the LaunchAgent's label), `LMS` (the `lms` command when it is not at `~/.lmstudio/bin/lms`); `LOCALMODELS_HOME` is only for a project's own code that imports `llmshare` from the repo.

Headers a client may send: `X-Project` (a name instead of the folder's), `X-Kind` (`batch`, `chat`, `ask`, `eval`, `test`; `chat` and `ask` go first when slots are scarce), `X-Step` (what the request is for, shown on the dashboard), `X-Timeout` (the program's own timeout in seconds; the door answers `503` before it runs out), `X-Session` (a tag for the request log).

## How it is built

| file | job |
|---|---|
| `localmodels/gateway.py` | the door: names each request, takes a turn, passes it to LM Studio, logs one line, holds and resends across a crash |
| `localmodels/llmshare.py` | the sharing rules: slot locks, leases, the swap lock, the model listing, the readers of the shared state |
| `localmodels/models.py` | the keeper: copies kept loaded, crashed models loaded again, copies recycled, swaps |
| `localmodels/crashes.py` | LM Studio's log read for crashes; the keeper's state and record |
| `localmodels/recycle.py` | the recycle policy: work since load against the band, draining, fresh starts |
| `localmodels/agent.py` | local agents: the tool loop, read-only file tools, JSON answers, retries |
| `localmodels/mcp_server.py` | the MCP tools for Claude Code; the detached job runner |
| `localmodels/whois.py`, `sessions.py` | who is behind a connection; every Claude Code session on the Mac |
| `localmodels/web.py`, `dash.html`, `dash.py` | the dashboard and the terminal dash |
| `localmodels/cli.py`, `bin/localmodels` | the command |
| `tests/` | sandbox tests with a fake LM Studio (no model is ever loaded or unloaded by a test); `localmodels check` runs them |
| `tools/` | `thinkbench.py` (thinking on against off), `loadbench.py` (long prompts and long JSON answers, both layouts) |

## What it reads, what it keeps, whom it trusts

To name a request the door reads process facts about the program on the other end of the connection (`lsof` for the peer, `libproc` for its folder, arguments and environment, where Claude Code leaves its session ids) and, for the dashboard, Claude Code's own session files and the first lines of its transcripts (the folder and the title only), and LM Studio's server logs (for crashes). It keeps names, times, kinds and token counts in `~/Library/Application Support/LocalModels/` (`requests.jsonl`, `waits.jsonl`, `crashes.jsonl`, `keeper.json`, `leases/`, `slots/`, `inflight/`), never a prompt or an answer; the local agents' job logs under `~/Library/Logs/LocalModels/agents/` are the one place content lives, as each project's own record (`localmodels prune` removes old ones). The door serves this Mac only and trusts every program on it: `X-Project` and `X-Session` are taken at their word, and the dashboard shows session titles and project names to any local page. That is the right model for one person's Mac; a shared Mac would want a token in front of `/hub/`.

Tested on macOS 26 (Apple silicon) with LM Studio 0.4 (an `lms` with `ps --json`) and Python 3.12–3.14; the code uses nothing newer than Python 3.9.

## Known limits

- The recycle thresholds come from three measured crashes and one survivor; `localmodels crashes` shows each copy's work before a crash, and the reload-and-resend path stands on its own if a copy dies below the band.
- A batch run by a project's own script depends on that script staying alive; the hub shows that it died, it cannot restart it. The hub's own runner cannot yet resume a batch that was killed.
- `lms ps` takes tens of seconds under heavy load; the hub tolerates it (the last good listing stands) rather than fixing it.
- Every request costs one shared `lsof` per second; at hundreds of requests a minute the naming would be the first thing to cache harder.
- macOS only by design. A Linux port would replace `whois.py`, the memory reading and the LaunchAgent with `/proc` and a systemd unit.

## The rules for programs using the models

Send requests to the door; hold a lease for a long run that pauses between requests; ask before a swap; never stop LM Studio's server. [GUIDE.md](GUIDE.md) is the full guide, the one the MCP `guide` tool serves to every session.

## License

MIT — see [LICENSE](LICENSE).
