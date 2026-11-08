"""localmodels - the local-models hub: one LM Studio on this Mac, shared by every project and Claude Code session.

The instructions for any project live in GUIDE.md at the root of this repo; the code:
  llmshare    the sharing rules (slot locks with interactive priority, leases, swap checks) and the shared state's readers
  gateway     the reverse proxy: LM Studio's API on its own port, with admission control on every request,
              holding and resending across a crash
  models      the instance manager: replicas kept as the settings say (models.json), recycles, swaps
  crashes     engine crash detection from LM Studio's logs, the crash record, reloads
  recycle     pre-emptive recycling: an instance reloaded before the token count at which the engine has crashed
  peers       other Macs' gateways as more replicas of a family, over the Thunderbolt Bridge
  whois       which project, Claude session and process is behind a connection
  sessions    every Claude Code session on this Mac, by project
  agent       local agents: one well-defined job on the loaded model, with tools that only read
  dash        the terminal dashboard; web and dash.html, the web dashboard the gateway serves
  mcp_server  the hub's MCP tools for every Claude Code session (registered once, or shipped by the plugin)
  cli         the `localmodels` command
"""
import os

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = '1.2.2'
NAME = 'Local Models Hub'                    # the product's name wherever a person sees it: the tab, the page, the corner
