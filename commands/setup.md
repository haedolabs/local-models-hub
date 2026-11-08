---
description: Set up the local-models hub on this Mac - the door at localhost:1235 in front of LM Studio, its keeper and its dashboard - and check it
allowed-tools: Bash(python3 *), Bash(localmodels *), Bash(ls *), Bash(open *), Bash(uname *), Bash(~/.lmstudio/bin/lms ls*), Read, Edit
---

Set up the local-models hub on this Mac. It is already installed as a plugin; this command opens its door, which runs in the background from now on.

1. Check the ground: the Mac must have Apple silicon (`uname -m` says `arm64`); LM Studio must be installed with at least one model downloaded, and its command-line tool present at `~/.lmstudio/bin/lms` (LM Studio installs it; if it is missing, tell the user to open LM Studio once and run `~/.lmstudio/bin/lms bootstrap`). `python3` must be on the PATH. If LM Studio's server is off, the hub starts it itself. If something is missing, stop and say what, rather than working around it.

2. Run the install:

   `python3 ${CLAUDE_PLUGIN_ROOT}/bin/localmodels install --plugin`

   It opens the door at login (a LaunchAgent that runs a copy of the code from `~/Library/Application Support/LocalModels/hub/`), writes the settings file `~/Library/Application Support/LocalModels/models.json` if there is none yet, fitted to this Mac's memory and the models it has downloaded, and puts the `localmodels` command on the PATH. `--plugin` leaves the MCP registration to this plugin (its `.mcp.json`) and adds no line to `~/.claude/CLAUDE.md`. It is safe to run again.

3. Run the self-test and show the user its lines as they are:

   `python3 ${CLAUDE_PLUGIN_ROOT}/bin/localmodels check`

4. Show the user the model the settings name (install's line 0 says it) and ask whether that is the one they want kept loaded; if not, change the family in the settings file to the one they name (the `guide` tool explains the keys).

5. Ask the user to turn **just-in-time model loading** off in LM Studio's developer settings: the door never needs it, and with it on LM Studio could load a model with its own settings.

6. Tell the user, in plain words:
   - Point every program at `http://localhost:1235/v1` instead of LM Studio's port 1234 (any API key).
   - The dashboard is at http://localhost:1235 (`localmodels open`); `localmodels` in a terminal gives the same in a few lines.
   - To keep a model loaded as several copies, or to turn recycling on or off, edit `~/Library/Application Support/LocalModels/models.json` (the `guide` tool explains the keys).
   - The MCP tools `status`, `guide`, `ask`, `batch`, `job_status` and `swap_check` are available in every Claude Code session from the next session on.

7. If anything in the check fails, read `~/Library/Logs/LocalModels/gateway.log` and say what it says. Never stop LM Studio's server, and never unload or reload a model without `swap_check` first.
