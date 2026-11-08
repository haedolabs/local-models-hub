# Local Models Hub

Reverse proxy, instance manager and MCP server for one LM Studio shared by every process and Claude Code session on an Apple-silicon Mac.

LM Studio serves an OpenAI- and Anthropic-compatible API on `localhost:1234` with no admission control across clients. Under concurrent use (batch scripts, local agents, several Claude Code sessions) four things fail silently: requests beyond an instance's *Max Concurrent Predictions* queue inside the engine until client timeouts fire; one client's load, unload or just-in-time load evicts a model another client is mid-run on; a Metal engine crash (`[metal::malloc] Resource limit exceeded`) unloads the model, fails in-flight requests with a bare `400`, and nothing reloads it; and there is no per-client attribution of load. The hub puts a gateway on `localhost:1235` that adds admission control, replica management, crash recovery, attribution and observability. Clients change only their base URL.

Standard-library Python 3.9+, no third-party dependencies. macOS on Apple silicon (launchd, `lms`, `lsof`, `libproc` and `sysctl` process introspection). MIT.

## Features

### Gateway (`localhost:1235`)
- **API surface.** A transparent proxy for every API LM Studio serves: OpenAI (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/responses`), Anthropic (`/v1/messages`) and LM Studio's REST API (`/api/v1/*`, `/api/v0/*`). SSE streams pass through line by line. Stream completion is detected per API (`data: [DONE]`, `message_stop`, `response.completed`, `chat.end`), and token usage is read from each API's own fields (`usage`, `message.usage`, `response.usage`, `stats`).
- **Admission control.** One slot per concurrent prediction of each loaded instance, implemented as `flock(2)` lock files shared by every process on the Mac (the gateway, and any project code using `llmshare.slot`). Requests beyond capacity queue in the gateway, not in the engine. The queue is priority-ordered: interactive requests (`X-Kind: chat` or `ask`) ahead of batch, FIFO within a class.
- **Client deadlines.** A client that sends `X-Timeout: <seconds>` gets `503` with `Retry-After` at 90% of that budget instead of hitting its own timeout in the queue.
- **Replica pooling.** A *family* loaded as N instances (`<key>`, `<key>:2`, …) is addressed by its base id. The gateway routes each request to the first instance with a free slot (fill-first), skips instances that are down or draining, and rewrites the model id in the response to the one requested.
- **Crash handling.** A crash-shaped failure (LM Studio's crash text, a dropped connection, an instance gone from the listing) is held while the instance reloads (up to 10 minutes), then retried once; if that fails too, the client gets `503` with `Retry-After`, never the engine's `400`. A stream that breaks after it started gets an error event in its own API's format. A request for a family with no loaded instance is refused with `503` instead of being forwarded, which would trigger LM Studio's just-in-time load with default settings.
- **Attribution.** Each request is attributed to a project (the peer process's working directory, via `lsof` and `proc_pidinfo`) and a Claude Code session (`CLAUDE_CODE_SESSION_ID` from the environment of the peer or its nearest ancestor, via `sysctl KERN_PROCARGS2`), with no client cooperation. Optional headers override or refine it (see [Configuration](#configuration)).
- **Swap guard.** Every process using an instance holds a lease (implicitly for 2 minutes after its last request, or explicitly via `llmshare.lease`). A load or unload sent through the gateway while another process holds a lease on the instance gets `409`; `localmodels swapcheck` and the `swap_check` tool give the same answer ahead of time.
- **Power.** `caffeinate -i` while any request is in flight, released 10 minutes after the last.
- **Request log.** One JSONL record per request: project, session, kind, model, instance, endpoint, status, latency, queue wait, token counts, step and run. Never a prompt or a completion.

### Instance manager
Runs in the gateway process every 20 s, and within seconds of a failed request.
- **Replica count.** Whenever a family's first instance is loaded, the instance manager loads the missing instances with the family's context length and parallelism (memory permitting, with `headroom_gb` kept free). It never loads a family from nothing and never unloads for idleness.
- **Crash recovery.** It tails LM Studio's server logs (`~/.lmstudio/server-logs`) for engine failures and confirms them against `lms ps` and the REST model listing (two reads 20 s apart). The crashed instances are reloaded with their original settings, at most 3 times per instance per hour; after that the family is left down for a person to reload (`localmodels use <model>`), and is retried hourly. Each crash is recorded with the tokens the instance had processed since load.
- **Pre-emptive recycling.** The measured Metal resource-limit crashes occurred after 931k to 1.18M tokens processed by one instance. Before configurable thresholds (default: a soft threshold at 560k, where it reloads when idle, and a hard one at 800k, where it drains), each instance is drained (no new requests routed to it, in-flight ones finish), unloaded and reloaded with the family's settings, one instance at a time, so the family stays available throughout: the worker-recycling pattern (compare gunicorn's `max_requests`). A batch can request fresh instances at start (`POST /hub/recycle`; instances past `before_batch` of their threshold go first). An instance loaded with other settings than its family's (from LM Studio's window or a just-in-time load) is recycled the same way.

### Local agents
- **`ask` and `batch`:** a tool-calling loop on the loaded model, with read-only file tools (`read`, `list`, `grep`) confined to a project root by realpath checks. Answers are structured output: strict `response_format: json_schema` first, falling back to plain JSON, validated against the job's schema. Thinking is disabled for the schema step. Failures from the model being down (`503`, a refused connection) are retried for up to an hour, and an empty structured answer is retried once.
- **How they run:** batches run detached, with per-job logs and `job_status` polling. They are exposed as MCP tools (`status`, `guide`, `ask`, `batch`, `job_status`, `swap_check`) and as `localmodels ask` and `localmodels batch`.

### Batch progress
A client declares a run with three request headers (`X-Run`, `X-Run-Total`, `X-Run-Done`); the dashboard shows the run (*batch*) and the phase it is in (*job*, parsed from `X-Step: <phase> N of M`), each with a progress bar, remaining time and a local-time ETA. Remaining time is measured throughput over the last 15 minutes or 20 units, whichever spans further back; before the third unit completes it is seeded from the previous run of the same name. There are no assumed constants, so estimates are per machine. A client that sends only `X-Step: <phase> N of M` gets the job bar.

### Peers (other Macs)
Every host runs its own hub; the one the clients use (the front) lists peer gateways in `peers`, and a host that serves replicas sets the address of its link listener in `serve`. Over a Thunderbolt Bridge link with fixed addresses:
- **Replicas across Macs.** The front polls each peer's `GET /hub/peer` every 2 s (families loaded, slots, busy, how many of those are this front's, fingerprints, maintenance, the instance manager's warnings). A peer's instances of a family count as one more replica, with capacity `slots − (busy − this front's)`, in the same priority queue, never with more in flight on a peer than that capacity; `at_once` includes it. Batch requests fill each Mac's first instance before a second instance on any one Mac (local first instance, peers' first instances, local second instance, the peers' rest); `chat` and `ask` take local instances first. Measured on the extraction load of `tools/loadbench.py`: one M5 Max instance ×4 156.4 tokens/s, two ×8 192.6 (+23%), and a MacBook Pro M4 Pro peer ×4 73.3 on top (≈266, +38%), at the same accuracy (GUIDE.md, *Peers*).
- **Model identity.** A family matches across Macs only when the family id, the loaded variant (`lms ps --json` `selectedVariant`), the size in bytes, the format and a digest of the model's definition (`~/.lmstudio/hub/models/<key>/model.yaml`, `manifest.json`) all match; `families.<id>.variant` pins the variant to accept when this Mac has no fingerprint of its own yet.
- **Attribution.** `X-Project`, `X-Session`, `X-Kind`, `X-Step`, `X-Run`, `X-Run-Total` and `X-Run-Done` are forwarded, with `X-Hub-From`; the peer logs and leases under the front's project, and the front logs the request with `copy: "<family> on <peer>"`, so no peer's tokens count against a local recycle threshold.
- **Failure.** A peer that refuses the connection, is in maintenance, rejects the token or has no slot within 5 s (`X-Timeout: 5` to the peer) answers before any byte reaches the client, and the request is requeued locally. Polling backs off from 2 s to 60 s on a peer that does not answer; a rejected token is not retried until the token or the settings change (or 5 min). Connections to peers use TCP keepalive (a dead link is detected in about 25 s).
- **The link listener.** With `serve` set, the gateway also listens on that address, bound to its interface (`IP_BOUND_IF`), and accepts only requests with the shared token (`X-Hub-Token`, constant-time compare), no `Origin`, and only generation endpoints, the model listing, `/hub/peer` and `/hub/health`. It is reconciled with the settings every 5 s (no restart).
- **Per host.** Maintenance, recycling, crash recovery and the instance manager stay local to each host. A request received over the link is served locally only. The instance manager recognises LM Studio's loading-guardrail refusal, does not retry until the guardrail setting or `iogpu.wired_limit_mb` changes, and does not recycle a single-instance family before it has loaded it on that Mac once; it reports a model larger than the GPU's wired limit, or one that fits only because the limit was raised by hand. `localmodels peer gpu-limit` writes the boot LaunchDaemon (`com.local-models.gpu-limit`, `sysctl iogpu.wired_limit_mb=<total − 8 GB>` at load) and prints the `sudo` lines that install it.
- **Dedicated peers.** A family with `"load_at_start": true` is loaded by the instance manager when the gateway starts and none of it is loaded (after `lms daemon up` if LM Studio is not running, and once the GPU limit fits it; 3 tries, 30 s apart). A login LaunchAgent `com.local-models.load-at-login` from before the hub keeps the job while installed; `install` retires it once a family has `load_at_start`.
- **Copying a model to a peer.** `localmodels peer copy-model <model> <user@peer>` replicates a catalog model to the peer as LM Studio stores it: the definition folder, the variant's weights folder (rsync over SSH), the two `model-data.json` records (merged into the peer's superjson Map, preserving its entries and `undefined` annotations, with a backup) and the default load config. It refuses while LM Studio runs on the peer; `--dry` prints the plan. With the guardrail on and a model above three quarters of the GPU limit, the peer reports that loads will be refused before any is.
- **LM Link** is not used; `lms ps` rows from LM Link devices (`deviceIdentifier` set) are excluded from the local listing.

### Maintenance windows
`localmodels maintenance in 15m | at HH:MM | now | after-batches [for 30m] [reason]`.
- **Announced:** every response carries `X-Hub-Maintenance` (phase, ISO 8601 start and end, reason), and `/hub/health`, the dashboard, the `status` tool and a Claude Code hook (`SessionStart` and `UserPromptSubmit`, about 50 ms, silent unless a window is set) all report it.
- **Active:** new requests get `503` with `Retry-After` set to the window's end (`overloaded_error` on `/v1/messages`). In-flight requests complete, and queued ones are refused when dequeued. The instance manager performs no loads, reloads, recycles or server starts.
- **`off`:** the engine log written during the window is skipped, so restarts are not recorded as crashes, and the families loaded at the window's start are reloaded.

### Observability
- **Dashboard** at `http://localhost:1235`: one page, no build step, fits one screen. It shows:
  - per-instance slot occupancy by project (one module per slot), and tokens processed against the recycle threshold; the card's layout scales to its space for any number of models, folding detail as models are added;
  - active clients with batch and job progress;
  - Claude Code sessions;
  - memory as one pool across this Mac and its peers, with a per-host breakdown;
  - 15-minute to 24-hour series of concurrency, throughput, requests and memory (stacked by host);
  - crashes and recycles marked on the time series (the full record: `localmodels crashes`).
- **JSON endpoints:** `/hub/health`, `/hub/status`, `/hub/snapshot`, `/hub/history`, `/hub/recent`, `/hub/peer` (this Mac's report as a peer) and `/hub/link` (this Mac's view of its peers; `POST` polls them now).
- **CLI:** `localmodels` (status), `dash` (live TUI), `log`, `crashes`, `maintenance`, `name`, `peer`, `swapcheck`, `use`, `keep`, `recycle`, `ask`, `batch`, `jobs`, `prune`, `check` (self-test), `install`, `uninstall` and `publish`.

## Install

**Requirements:**
- a Mac with Apple silicon;
- [LM Studio](https://lmstudio.ai) with at least one model downloaded;
- its CLI, `lms` (open LM Studio once, then run `~/.lmstudio/bin/lms bootstrap`);
- `python3` 3.9 or later.

Also disable **just-in-time model loading** in LM Studio's developer settings; the gateway never needs it.

**The short way:** in Claude Code, say *"Set up the local-models hub from github.com/matthaedo/local-models-hub."* Claude reads the next section and does the rest.

**If you are Claude Code setting this up for someone:**
1. Check the requirements: `uname -m` prints `arm64`; `~/.lmstudio/bin/lms ls` lists at least one model (if `lms` is missing, ask the person to open LM Studio once and run `~/.lmstudio/bin/lms bootstrap`); `python3 --version` is 3.9 or later. Stop and report anything missing rather than working around it.
2. Slash commands are the person's to type. Ask them to type the two plugin lines below; if they would rather not use the plugin, clone the repo and run `bin/localmodels install` yourself (the terminal install below).
3. Run `localmodels check` and show its output verbatim. If anything fails, read `~/Library/Logs/LocalModels/gateway.log` and report what it says.
4. Show the settings the install wrote (`~/Library/Application Support/LocalModels/models.json`, sized to this Mac's memory and downloaded models) and confirm the family is the model they want kept loaded.
5. Ask the person to disable just-in-time model loading in LM Studio's developer settings.
6. Tell them: point clients at `http://localhost:1235/v1` instead of port 1234 (any API key); the dashboard is at http://localhost:1235; the MCP tools appear in Claude Code from the next session on.
7. Never stop LM Studio's server, and never load or unload a model the person did not ask you to.

**With Claude Code** (the repo is a Claude Code plugin and its own marketplace), in any session:

```
/plugin install local-models --marketplace matthaedo/local-models-hub
/local-models:setup
```

The first line installs the plugin (the MCP server, a skill with the client contract, and the maintenance hook); the second has Claude install the gateway on this Mac and run the self-test. On Claude Code before 2.1.275: `/plugin marketplace add matthaedo/local-models-hub`, then `/plugin install local-models@local-models-hub`.

**Without Claude Code**, in a terminal:

```bash
git clone https://github.com/matthaedo/local-models-hub.git ~/local-models
~/local-models/bin/localmodels install
```

`install` does the following:
- copies the code to `~/Library/Application Support/LocalModels/hub/` and starts the gateway from there as a LaunchAgent (`KeepAlive`, started at login);
- writes the settings file if there is none, sized to this Mac: the measured default when its model is downloaded and two instances fit, else the largest downloaded model that fits, as two instances when two fit;
- registers the MCP server at user scope (when `claude` is on `PATH`), adds the maintenance hook to `~/.claude/settings.json`, and adds one signpost line to `~/.claude/CLAUDE.md` (`--plugin` leaves all three to the plugin; `--no-claude` skips them);
- links `localmodels` onto `PATH`.

Options and related commands:
- `--dry` prints the plan and writes nothing.
- Rerun `install` after changing the code: it restarts the gateway only once no request has been in flight for 3 s (up to `--wait` minutes).
- `localmodels uninstall` reverses all of it. `localmodels check` runs the self-test: sandbox tests plus live checks of the installed gateway.

Then point clients at `http://localhost:1235/v1` (any API key), or `http://localhost:1235` as the `base_url` of an Anthropic SDK client.

**Releases** (maintainer): `localmodels publish` (patch bump) or `localmodels publish --version X.Y.0`.
- **Before anything changes**, it runs the sandbox tests, `claude plugin validate`, and a privacy check. The privacy check reads every line added since the last tag, every new file name and every commit message against a word list kept outside the repo (`~/Library/Application Support/LocalModels/private-words.txt`: one regex per line, plus `allow: <regex>` exceptions). It refuses to push on any match.
- **Then** it stamps the version into `localmodels/__init__.py` and `.claude-plugin/plugin.json`, commits, tags `vX.Y.Z` and pushes. Plugin users pick the release up with `/plugin update`.
- **`--dry`** runs the checks only.

## Configuration

`~/Library/Application Support/LocalModels/models.json` (written by the first install; the repo's `localmodels/models.json` is the measured default it starts from):

```json
{
 "families": {
  "qwen/qwen3.6-35b-a3b": {"key": "qwen/qwen3.6-35b-a3b", "copies": 2, "context": 65536, "parallel": 4,
                           "recycle": {"tokens": 300000, "soft": 220000, "before_batch": 0.5}}
 },
 "batch_at_once": 8,
 "headroom_gb": 8
}
```

| field | meaning |
|---|---|
| `families.<id>.key` | the model key `lms load` takes |
| `copies` | replica count: instances kept loaded while the first is loaded |
| `context` | context length per instance, in tokens |
| `parallel` | max concurrent predictions per instance (its slot count) |
| `recycle.tokens` | hard threshold: tokens processed (prompt and completion) since load at which an instance is drained and reloaded |
| `recycle.soft` | soft threshold: past it, an idle instance is reloaded |
| `recycle.before_batch` | the fraction of the threshold past which an instance is recycled when a batch requests fresh instances |
| `"recycle": false` | disables recycling for the family |
| `families.<id>.variant` | optional: the variant (`<key>@<quantization>`) a peer's instances must have, when this Mac has not loaded the family itself |
| `families.<id>.load_at_start` | dedicated peer: the instance manager loads the family at gateway start if no instance of it is loaded |
| `batch_at_once` | the client concurrency a batch should use (reported by the `status` tool) |
| `headroom_gb` | free memory kept when loading an instance |
| `name` | this Mac's name as its peers show it (default: the short host name) |
| `peers.<name>.url` | a peer's gateway, `http://<address>:1235` (the front's side; `localmodels peer add <name> <address>`) |
| `serve.address`, `serve.port` | this Mac as a peer: the address (and port, default the gateway's) its link listener binds (`localmodels peer serve <address>`) |

The shared token for peers lives in `~/Library/Application Support/LocalModels/peer-token` (mode 0600), identical on every host of the set, never in `models.json`; `localmodels peer token` creates it and prints an `ssh` command that copies it without displaying it. GUIDE.md, *Peers*, has the setup steps (fixed Thunderbolt Bridge addresses, the GPU wired limit, LM Studio's loading guardrail, macOS's Local Network permission).

The file is re-read when its modification time changes. A file that fails to parse leaves the last valid configuration in force and is reported on the status line and the dashboard.

**Display names:** `~/Library/Application Support/LocalModels/names.json`, set with `localmodels name`, maps what a project is seen as (its `X-Project`, else its folder) to what every screen shows, at read time, so renames apply to past history too:
- `localmodels name project <as seen> <shown>`: one project shown under one name; a folder and a header can show as one.
- `localmodels name batch <project> <as seen> <shown>`: a label for one of its batches (`X-Run`), jobs (the phase in `X-Step`) or scripts.
- `localmodels name private <project>`: show only the labels set for it, with no Claude Code session titles and no unlabelled scripts, steps or batch names.

**Environment:**

| variable | meaning |
|---|---|
| `LOCALMODELS_PORT` | gateway port (default `1235`) |
| `LMSTUDIO_URL` | LM Studio's server (default `http://localhost:1234`) |
| `LMSTUDIO_LOGS` | LM Studio's server logs, for crash detection |
| `LOCALMODELS_STATE` | shared state directory |
| `LOCALMODELS_LABEL` | the LaunchAgent label |
| `LMS` | the `lms` binary, when it is not at `~/.lmstudio/bin/lms` |
| `LOCALMODELS_PRIVATE_WORDS` | the privacy-check word list for `publish` |
| `LOCALMODELS_HOME` | where project code that imports `llmshare` finds the repo |

**Request headers** (all optional):

| header | meaning |
|---|---|
| `X-Project` | overrides the attributed project (also `/p/<project>/v1/...`, for clients that only take a base URL) |
| `X-Kind` | `batch`, `chat`, `ask`, `eval`, `test` or `other`; `chat` and `ask` are dequeued first |
| `X-Step` | free text shown on the dashboard; `<phase> N of M` drives the job bar |
| `X-Run`, `X-Run-Total`, `X-Run-Done` | run name, total units and completed units, for batch progress |
| `X-Timeout` | the client's own timeout, in seconds; the gateway answers `503` at 90% of it |
| `X-Session` | a tag for the request log |

**Response header:** `X-Hub-Maintenance` while a maintenance window is announced or active.

## Architecture

| module | role |
|---|---|
| `localmodels/gateway.py` | the gateway: attribution, admission, routing, proxying (streaming included), per-API completion and usage parsing, hold-and-retry across crashes, the request log, maintenance refusals |
| `localmodels/llmshare.py` | shared state: slot locks, leases, the swap lock, the model listing (`lms ps` and REST), settings, readers of the logs |
| `localmodels/models.py` | the instance manager: replica counts, crash reloads, recycles, swaps, restoring after maintenance |
| `localmodels/crashes.py` | LM Studio log tailing and crash classification; the instance manager's state and crash record |
| `localmodels/recycle.py` | recycle policy: tokens since load against the recycle threshold, draining, fresh starts |
| `localmodels/runs.py` | batch and job progress, throughput and ETA from the request log |
| `localmodels/maint.py` | maintenance windows: state, header, notice |
| `localmodels/peers.py` | peers: the shared token, the peer's report, the front's link (polling, backoff, capacity, model fingerprints), network checks (`ifconfig`, `route`, `networksetup`) |
| `localmodels/agent.py` | local agents: the tool loop, sandboxed read-only file tools, structured output, retries |
| `localmodels/mcp_server.py` | the MCP server (stdio) and the detached job runner |
| `localmodels/whois.py`, `sessions.py` | peer-process attribution; discovery of Claude Code sessions |
| `localmodels/web.py`, `dash.html`, `dash.py` | the dashboard and its JSON endpoints; the terminal UI |
| `localmodels/cli.py`, `bin/localmodels` | the CLI |
| `hooks/hooks.json` | the plugin's maintenance hook |
| `tests/` | sandbox tests against a fake LM Studio on an ephemeral port (no model is ever loaded or unloaded), and two gateways with a fake LM Studio each for peers; run by `localmodels check` and `publish` |
| `tools/` | `thinkbench.py` (thinking on versus off), `loadbench.py` (long prompts and long structured completions, one instance versus two) |

## Data, privacy and trust model

**Reads:**
- peer-process metadata, for attribution: `lsof` for the socket's peer, `proc_pidinfo` for its working directory and parent, `KERN_PROCARGS2` for its arguments and environment;
- for the dashboard, Claude Code's session files and the first lines of its transcripts (working directory and title only);
- LM Studio's server logs, for crash detection.

**Writes** (in `~/Library/Application Support/LocalModels/`): `requests.jsonl` and `waits.jsonl` (rotated at 20 MB), `crashes.jsonl`, `keeper.json`, `maintenance.json`, `link.json` (the front's view of its peers), `peer-token`, and the `leases/`, `slots/` and `inflight/` directories. These hold metadata and token counts, never a prompt or a completion. The local agents' job logs under `~/Library/Logs/LocalModels/agents/` are the only place content is stored, as each project's own record; `localmodels prune` deletes old ones.

**Trust boundary:**
- **Loopback only.** A request must carry a loopback `Host` header and, if it has an `Origin`, the gateway's own origin. This blocks DNS rebinding and cross-origin requests from web pages.
- **The link listener (peers).** Only with `serve` set: one address, bound to its interface; every request must carry the shared token and no `Origin`, and only generation endpoints, the model listing and the peer report are served. The token and the requests cross the Thunderbolt cable as plain HTTP: the link assumes a direct cable between trusted Macs, not a shared network. Forwarded attribution headers are trusted from a peer that holds the token.
- **Every local process is trusted.** `X-Project` and `X-Session` are not authenticated, and the dashboard shows project names and session titles to any local client. That suits a single-user Mac; a multi-user host would need authentication in front of `/hub/*`.

Tested on macOS 26 (Apple silicon) with LM Studio 0.4 (`lms ps --json`) and Python 3.12–3.14; the code targets Python 3.9.

## Known limitations

- **The recycle thresholds rest on few data points:** three measured crashes and one instance that survived 1.35M tokens. `localmodels crashes` reports each instance's tokens at crash time, and hold-and-retry still covers a crash below the threshold.
- **Client-run batches depend on the client process.** A batch run by a project's own script lives only as long as that script; the hub shows that it stopped but cannot resume it. The hub's own `batch` runner cannot yet resume a killed batch either.
- **`lms ps` takes tens of seconds under heavy load;** the instance manager keeps the last valid listing rather than act on a partial one.
- **Attribution costs one shared `lsof` call per second;** at hundreds of requests a minute it is the first thing to cache harder.
- **Peers:** this Mac's maintenance refuses requests even when a peer could serve them; the local agents (`ask`, `batch`) need the model loaded on this Mac; a peer's link has one shared token for the whole set of Macs; a LaunchAgent's connections to the local network need macOS's Local Network permission, which only a person can grant (`localmodels peer test` tells it from a link that is down).
- **macOS only.** A Linux port would replace `whois.py`, the memory accounting and the LaunchAgent with `/proc` and a systemd unit.

## Client contract

Send every request through the gateway, never to port 1234. Hold a lease for a run that pauses for more than 2 minutes between requests. Call `swap_check` before loading or unloading a model. Never stop LM Studio's server. Honour `Retry-After` on `503`. [GUIDE.md](GUIDE.md) is the full contract; the MCP `guide` tool serves it to every session.

## License

MIT; see [LICENSE](LICENSE).
