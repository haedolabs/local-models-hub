# <img src="docs/icon.png" alt="" width="32" height="32"> Local Models Hub

By [Haedo Labs](https://haedolabs.com).

A gateway in front of LM Studio, for a Mac where several programs and Claude Code sessions share the same local models.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/dashboard-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/dashboard-light.png">
  <img alt="The dashboard during a synthetic workload: every slot in use, two requests queued, the requests by project over an hour, and each client's run with its finish time" src="docs/dashboard-light.png">
</picture>

The dashboard during `localmodels demo`, a synthetic workload.

## Why

LM Studio serves one API on `localhost:1234` and treats every client alike. When several share it:

- requests past an instance's limit wait inside the engine until the client times out;
- one client's load or unload takes away a model another client is using;
- a Metal engine crash unloads the model, fails the requests in flight, and leaves it unloaded;
- nothing says which client is using what.

The hub listens on `localhost:1235` and handles these. Clients change only their base URL.

## What it does

- **Admission.** One slot per concurrent prediction of each instance, shared by every process on the Mac. Extra requests wait in the gateway, interactive ones first.
- **Replicas.** A model can be kept loaded as several instances. Requests go to the first with a free slot.
- **Crashes.** A crashed or stalled instance is reloaded with its settings, and requests that had not started answering are sent again.
- **Recycling.** Each instance is drained and reloaded before the token counts at which the engine has been measured to crash.
- **Attribution.** Each request is logged with its project and Claude Code session, found from the calling process. Prompts and completions are never logged.
- **Other Macs.** A second Mac's hub, over a Thunderbolt Bridge cable, can serve as more replicas.
- **Maintenance and off.** A window or a switch refuses new requests with `Retry-After`, lets the ones in flight finish, and loads nothing until it ends.
- **Local agents.** `ask` and `batch` run the loaded model with read-only file tools, confined to the calling project's folder.
- **Dashboard and status.** At `http://localhost:1235`, in the terminal (`localmodels`), and in an optional menu-bar app.

## Install

Needs a Mac with Apple silicon, [LM Studio](https://lmstudio.ai) with a model downloaded, its `lms` command (`~/.lmstudio/bin/lms bootstrap`), and Python 3.9 or later. No other software.

Turn off **just-in-time model loading** in LM Studio's developer settings.

In Claude Code:

```
/plugin install local-models --marketplace haedolabs/local-models-hub
/local-models:setup
```

In a terminal:

```bash
git clone https://github.com/haedolabs/local-models-hub.git ~/local-models
~/local-models/bin/localmodels install
```

`install` starts the gateway at login, writes settings sized to the Mac, and adds the `localmodels` command. `localmodels check` tests it. `localmodels uninstall` removes all of it.

## Use

Point clients at `http://localhost:1235/v1` (any API key), or at `http://localhost:1235` as an Anthropic SDK client's `base_url`.

Optional headers name the work: `X-Project`, `X-Kind` (`batch`, `chat`, `ask`, `eval`, or `test`), `X-Step`, and `X-Run`, `X-Run-Total`, and `X-Run-Done` for a progress bar and finish time.

In Claude Code, the plugin adds the MCP tools `status`, `guide`, `ask`, `batch`, `job_status`, `name`, and `swap_check`, and a hook that announces maintenance.

## Rules for clients

Send every request through the gateway. Hold a lease for a run that pauses more than 2 minutes. Call `swap_check` before loading or unloading a model. Leave LM Studio's server running. Honor `Retry-After` on `503`. The full contract is [GUIDE.md](GUIDE.md).

## Privacy

Requests stay on the Mac, or go over the peer cable when one is set up. The hub's records hold metadata and token counts, never a prompt or a completion; the local agents' job logs are each project's own record of its answers. The gateway answers loopback requests only, and refuses requests from web pages. The peer link listens on one cable address and needs a shared token.

## Reference

Configuration, endpoints, architecture, the trust model, and known limitations: [docs/reference.md](docs/reference.md).

## License

MIT, © 2026 [Haedo Labs](https://haedolabs.com); see [LICENSE](LICENSE). The dashboard's typeface, Libre Franklin, is under the SIL Open Font License 1.1.
