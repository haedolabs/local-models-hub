"""localmodels - the local-models hub: one LM Studio on this Mac, shared by every project and Claude Code session.

The instructions for any project live in GUIDE.md at the root of this repo; the code:
  llmshare    the sharing rules (slots with people first, leases, swap checks) and the shared state's readers
  gateway     the hub's door: the same API as LM Studio, on its own port, applying the rules to every request,
              holding and resending across a crash
  models      the models LM Studio holds, kept the way the settings say (models.json), recycled, and swaps
  crashes     when LM Studio's engine crashes: notice it, record it, load the model again
  recycle     a copy recycled before the amount of work at which the engine has crashed
  peers       other Macs' hubs as more copies of a family, over the Thunderbolt Bridge
  whois       which project, Claude session and program is behind a connection
  sessions    every Claude Code session on this Mac, by project
  agent       local agents: one well-defined job on the loaded model, with tools that only read
  dash        the live console of all of it; web and dash.html, the dashboard the door serves
  mcp_server  the hub's tools for every Claude Code session (registered once, or shipped by the plugin)
  cli         the `localmodels` command
"""
import os

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = '1.2.2'
NAME = 'Local Models Hub'                    # the product's name wherever a person sees it: the tab, the page, the corner
