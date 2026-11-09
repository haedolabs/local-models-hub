"""cli - `localmodels`: every hub job as one command that prints a short summary.

  localmodels                    status: gateway, LM Studio instances, peers, clients, admission queue
  localmodels open               the dashboard in the browser (http://localhost:1235)
  localmodels dash               the live console in a terminal (--once, --json)
  localmodels log [--hours 24]   request log by project: count, tokens, active and queued time
  localmodels swapcheck [model ...]   may these models be unloaded or reloaded now? (exit 0 yes, 3 no)
  localmodels use <model> [--context N] [--parallel N] [--dry]   load a model (all replicas, for a family), unloading the rest
  localmodels keep               one instance-manager pass: crash reloads, missing replicas, due recycles
  localmodels recycle [model] | --status   request fresh instances before a batch (instances past before_batch of
                                 their threshold recycle first) | tokens since load against the recycle threshold
  localmodels maintenance [in 15m | at 20:30 | now | after-batches] [for 30m] [why ...] | off
                                 announce a maintenance window (X-Hub-Maintenance, dashboard, MCP status, Claude Code
                                 hook); while active, new requests get 503 + Retry-After and the instance manager pauses;
                                 off reloads the families loaded at its start. No arguments: the window and overlapping runs
  localmodels name               each project as seen and as shown on every screen, with its batches, jobs and scripts
  localmodels name project <as seen> <shown>            what every screen calls a project (a folder or an X-Project)
  localmodels name batch <project> <as seen> <shown>    a name for one of its batches, jobs or scripts
  localmodels name private <project> | public <project> private: screens show only the names set for it
  localmodels name forget <project> [<as seen>]         drop a name: one label, or the project's whole entry
  localmodels present on [for 2h] [show a,b] | off      screenshot mode: screens show the projects named (the demo's
                                 and local-models by default) and every other one as "other", with no session titles
  localmodels demo [--minutes 60] [--dry]   a synthetic workload under generic names (made-up records, three projects,
                                 every slot filled) for screenshots and load tests
  localmodels crashes [--days 7] [--scan]   LM Studio's crashes, the reloads and recycles (--scan: older ones in its logs)
  localmodels errors [--minutes 1440] [--trace]   what the hub recorded as going wrong (endpoints, background loops, the
                                 dashboard's render errors), newest first; --trace adds each traceback
  localmodels peer               peer status: each configured peer (state, RTT, interface, replicas, slots) and the link
                                 listener when this host serves as a peer
  localmodels peer add <name> <address>   add a peer gateway by its Thunderbolt Bridge address (port 1235 by default)
  localmodels peer remove <name>
  localmodels peer serve <address> | off  serve as a peer: bind a link listener on that address (token required)
  localmodels peer token         create peer-token (0600) if missing; print the ssh command that copies it to another host
  localmodels peer gpu-limit     write the boot LaunchDaemon com.local-models.gpu-limit (iogpu.wired_limit_mb = total - 8 GiB)
                                 and print the sudo commands that install it; verify an installed one
  localmodels peer copy-model <model> <user@peer> [--dry]   replicate an LM Studio catalog model to a peer over SSH
                                 (definition, weights, model-data.json records, default load config); LM Studio closed there
  localmodels peer test [name]   force a poll from the gateway and probe from this terminal (separates macOS Local
                                 Network blocking of the gateway from a link failure); print the route
  localmodels ask "<question>" [--dir FOLDER] [--no-files] [--no-think] [--schema FILE] [--max-turns N]
                                 (--no-think: no thinking: several times faster, for lookups and extraction)
  localmodels batch <jobs.json> [--dir FOLDER] [--parallel N]
  localmodels jobs               recent local-agent jobs started through the gateway
  localmodels prune [--days 30]  remove local-agent job folders and logs older than N days (they hold prompts and answers)
  localmodels check              self-test: sandbox suites, then live checks against the installed gateway
  localmodels install [--wait MINUTES] [--plugin] [--dry] [--no-claude]
                                 set the hub up on this Mac; run it again after changing the hub's code (it waits for a
                                 moment with no request in flight: 1.5 minutes unless --wait says longer; a change to the
                                 dashboard page alone is replaced in place at once, with no restart).
                                 --plugin: installed as a Claude Code plugin, which brings the tools. --dry: say what
                                 would be done, write nothing. --no-claude: no MCP registration, no signpost line
  localmodels uninstall          undo install
  localmodels start | stop | restart     the gateway (start is a no-op when running; restart waits for no requests in flight)
  localmodels gateway            run the gateway in the foreground (what launchd runs)
  localmodels mcp                the MCP server (Claude Code starts it)
  localmodels publish [--version X.Y.Z] [--dry] [--remote URL]   the tests, the plugin check and the privacy check, then
                                 the version stamped, committed, tagged and pushed (--dry: the checks only)
"""
import glob, json, os, plistlib, re, shlex, shutil, subprocess, sys, time, urllib.request

from . import HOME, NAME, VERSION, llmshare

LABEL = os.environ.get('LOCALMODELS_LABEL') or 'com.local-models.hub'
OLD_LABELS = ()                                          # earlier LaunchAgent labels an install retires
PLIST = os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')
INSTALLED = os.path.join(llmshare.SHARED, 'hub')
LOGS = os.path.expanduser('~/Library/Logs/LocalModels')
PY = shutil.which('python3') or sys.executable or '/usr/bin/python3'   # the PATH's python3 (a stable link, e.g. /opt/homebrew/bin/python3), not a versioned Cellar path
BINS = ('/opt/homebrew/bin', '/usr/local/bin', os.path.expanduser('~/.local/bin'))
SIGNPOST_FILE = os.path.expanduser('~/.claude/CLAUDE.md')
_HOME_SHOWN = '~' + HOME[len(os.path.expanduser('~')):] if HOME.startswith(os.path.expanduser('~') + os.sep) else HOME
SIGNPOST = (f"Local models on this Mac (LM Studio) are shared through the local-models hub: its rules live in {_HOME_SHOWN}/GUIDE.md "
            f"(or the local-models tools' guide). Send requests to {llmshare.HUB}/v1, never directly to LM Studio's port 1234. "
            "<!-- local-models hub signpost -->")
TESTS = ('test_crashes.py', 'test_rules.py', 'test_gateway.py', 'test_agent.py', 'test_peers.py', 'test_code.py')


def ago(t): return time.strftime('%H:%M', time.localtime(t))


def ago_left(s):
    """A stretch of time in the unit that reads best: 40 s, 12 min, 2 h 15 min, 3 d 4 h."""
    s = max(0, int(round(s)))
    if s < 90: return f'{s} s'
    if s < 5400: return f'{round(s / 60)} min'
    if s < 172800: h, m = s // 3600, round(s % 3600 / 60); return f'{h} h' + (f' {m} min' if m else '')
    d, h = s // 86400, round(s % 86400 / 3600); return f'{d} d' + (f' {h} h' if h else '')


def finish_words(t):
    """A time ahead in local time, with the day when it is not today."""
    d, today = time.localtime(t), time.localtime()
    day = '' if d.tm_yday == today.tm_yday and d.tm_year == today.tm_year else \
        'tomorrow ' if time.localtime(time.time() + 86400).tm_yday == d.tm_yday else time.strftime('%a ', d)
    return day + time.strftime('%H:%M', d)


def track_words(word, p):
    """'batch nightly: 109 of 150, 47 min left, done about 20:41'."""
    if not p or not p.get('total'): return ''
    when = (f"{ago_left(p['left'])} left" + (f", done about {finish_words(p['finish'])}" if p.get('finish') else '') if p.get('left') else 'finishing') \
        if p.get('left') is not None \
        else 'estimating the time left' if p['done'] < p['total'] else 'done'
    return f" · {word}{' ' + p['name'] if p.get('name') else ''}: {p['done']} of {p['total']}, {when}"


def num(a, name, default):
    """An option's number, or the default; a word that is not a number is said, not a traceback."""
    if name not in a: return default
    i = a.index(name)
    if i + 1 >= len(a) or a[i + 1].startswith('--'): print(f'{name} needs a number'); return None
    try: return float(a[i + 1])
    except ValueError: print(f'{name}: {a[i + 1]!r} is not a number'); return None


def opt(a, name, default=None):
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) and not a[a.index(name) + 1].startswith('--') else default


# ---------------------------------------------------------------- reading
def status():
    from . import dash, recycle
    d, lm, users, cs = dash.door(), dash.lmstudio(), dash.users(), dash.claude()
    from . import maint
    mt = maint.view()
    if mt: print('⚠ ' + mt['words'][0].upper() + mt['words'][1:] + (' (localmodels maintenance off when done)' if mt['phase'] == 'on' else ''))
    print(f"gateway: {'listening on ' + llmshare.HUB if d['up'] else 'down (localmodels start)'}"
          + (' · stale code: localmodels install' if d['up'] and d['stale'] else ''))
    print(f"LM Studio: server {'up' if lm['server'] == 'on' else 'down'} · " + ('; '.join(dash.family_words(f) for f in lm['families']) or 'no model loaded')
          + ('' if llmshare.read_ok() else ' · (lms ps failed: showing the previous listing)'))
    if llmshare.settings_error(): print(f'  SETTINGS NOT READ: {llmshare.settings_error()} (last valid settings in effect)')
    for line in peer_lines(d['up']): print(line)
    try:
        v = recycle.view(lm['models'])
        if v['copies']:
            print('  tokens since load: ' + ' · '.join(f"{dash.short(i)} {c['tokens'] / 1000:.0f}k" + (f" ({int(100 * c['share'])}% of recycle threshold)" if c.get('share') is not None else '')
                                              for i, c in v['copies'].items()))
        if v.get('job'): print('  ' + recycle.words(v['job']))
    except Exception as e: print(f'  (could not read per-instance token counts: {e})')   # not recorded: shown to the person running the command
    try:
        from . import admission
        a = admission.view(lm['models'])
        if any(x['budget'] for x in a.values()):
            print('  context in flight: ' + ' · '.join(f"{dash.short(i)} {x['load'] / 1000:.0f}k of {x['budget'] / 1000:.0f}k" for i, x in a.items() if x['budget'])
                  + ' (admission by context size)')
    except Exception as e: print(f'  (could not read the context in flight: {e})')   # not recorded: shown to the person running the command
    for line in crash_lines(): print(line)
    print('clients:' if users else 'clients: none')
    for r in users:
        b = r.get('batch') or {}
        state = (f"{r['answering']} active" + (f", {r['waiting']} queued" if r['waiting'] else '')) if r['answering'] or r['waiting'] else \
            f"{r['direct']} direct connections to LM Studio (bypassing the gateway)" if r['direct'] else 'idle (lease held)'
        who = f" · Claude: {r['title']}" if r.get('title') else ' · a Claude session' if r.get('session') else f" · {r['app']}" if r.get('app') else ''
        print(f"  {r['project']}: {dash.kind_words(r)} on {', '.join(dash.short(m) for m in r['models']) or '?'} · {state} · {'started' if (r.get('batch') or r.get('job') or {}).get('started') else 'since'} {ago(r['since'])}"
              + (track_words('batch', b) if b.get('total') else '') + track_words('job', r.get('job')) + (f" · {r['step']}" if r.get('step') and not b.get('total') and not r.get('job') else '') + who)
    work = [c for c in cs if c['working']]
    print(f"Claude Code sessions: {len(work)} active" + (f" ({', '.join(sorted({c['project'] for c in work}))})" if work else '')
          + f", {len(cs) - len(work)} idle in the last 3 hours")
    lh = dash.last_hour()
    if lh: print('last hour via the gateway: ' + '; '.join(f"{p} {v['requests']} requests, {v['tokens']:,} tokens" for p, v in lh.items()))
    n, secs, refused = llmshare.waits(7)
    print(f"last 7 days: {n} queue wait{'s' if n != 1 else ''} > 2 s ({secs / 60:.1f} min total), {refused} swap{'s' if refused != 1 else ''} refused")
    return 0


def log(hours=24):
    from . import names
    rows = [names.request(r) for r in llmshare.requests_log(hours)]
    if not rows: print(f'no requests through the gateway in the last {hours:g} hours'); return 0
    print(f'the last {hours:g} hours via the gateway: {len(rows)} requests')
    for p, v in sorted(llmshare.by_project(rows).items(), key=lambda x: -x[1]['requests']):
        kinds = {}
        for d in rows:
            if (d.get('project') or '?') == p: kinds[d.get('kind') or 'other'] = kinds.get(d.get('kind') or 'other', 0) + 1
        print(f"  {p}: {v['requests']} requests ({', '.join(f'{n} {k}' for k, n in kinds.items())}), {v['tokens']:,} tokens, "
              f"{v['seconds'] / 60:.1f} min active, {v['waited'] / 60:.1f} min queued")
    bad = [d for d in rows if (d.get('status') or 0) >= 400 and d.get('status') != 499]
    gone = [d for d in rows if d.get('status') == 499]
    resent = [d for d in rows if (d.get('tries') or 1) > 1]
    if bad: print(f"  {len(bad)} returned errors (e.g. {bad[-1].get('status')} for {bad[-1].get('project')}: {bad[-1].get('note') or ''})")
    if resent: print(f"  {len(resent)} retried by the gateway after an instance crash; {sum(1 for d in resent if d.get('status') == 200)} succeeded")
    if gone: print(f"  {len(gone)} dropped: client closed the connection while queued (499)")
    return 0


def crash_lines(hours=24):
    """A line for each model down after a crash now, and for each crash of the last day ('' when there were none)."""
    from . import crashes
    down = crashes.down()
    out = [f'DOWN AFTER A CRASH: {d["words"]}' for d in down]
    recent = [x for x in crashes.history(hours / 24) if not any(d['family'] == x['family'] for d in down)]
    if recent: out.append(f"crashes in the last {hours} hours: {len(recent)} · the last: {crashes.words(recent[0])} (localmodels crashes)")
    return out


def crashes_cmd(days=7, scan=False):
    from . import crashes, recycle
    if scan:
        n = crashes.scan(days)
        print(f"scanned LM Studio's logs for the last {days:g} days: {n} crash{'es' if n != 1 else ''} missing from the crash record, now recorded")
    xs = crashes.history(days)
    print(f"LM Studio crashed {len(xs)} time{'s' if len(xs) != 1 else ''} in the last {days:g} days" + (':' if xs else ''))
    for x in xs:
        print('  ' + crashes.words(x))
        for s in x.get('served') or []:
            print(f"    {s['model']} had served {s['requests']} requests ({s.get('tokens', s['written']):,} tokens processed) since {ago(s['since'])}")
    rs = recycle.recent(days)
    print(f"recycles: {sum(1 for r in rs if r['event'] == 'recycled')}" + (':' if rs else ''))
    for r in rs[:12]:
        when = time.strftime('%d %b %H:%M', time.localtime(r['at']))
        print(f"  {when} {r.get('model')}: " + (f"{r.get('tokens') or 0:,} tokens processed in {r.get('hours') or 0:.1f} h, back in {r.get('seconds') or 0:.0f} s" if r['event'] == 'recycled' else f"given up ({r.get('why')})"))
    down = crashes.down()
    print('down now: ' + '; '.join(d['words'] for d in down) if down else 'down now: none (every crashed instance reloaded or released)')
    return 0


# ---------------------------------------------------------------- peers: other Macs' hubs
def door_link(force=False, timeout=8):
    """The door's look at its peers, and this Mac as a peer (GET /hub/link; force: POST, the door looks now), or None
    when the door does not answer it (closed, or running code from before peers)."""
    try:
        req = urllib.request.Request(llmshare.HUB + '/hub/link', data=b'{}' if force else None, headers={'Content-Type': 'application/json'})
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
        return d if isinstance(d, dict) and 'peers' in d else None
    except (OSError, ValueError): return None


def peer_lines(door_up=True):
    """The status's lines about peers: one a peer, one when this Mac serves as one ([] when neither)."""
    from . import peers
    out, seen = [], set()
    for p in peers.view():
        seen.add(p['name']); out.append('  ' + p['words'])                      # (the words begin 'peer <name>: ...')
    for name in peers.configured():
        if name not in seen: out.append(f"  peer {name}: not polled yet" + ('' if door_up else ' (gateway not running)'))
    for w in peers.problems(): out.append(f'  peer settings error: {w}')
    if peers.serving():
        d = door_link() if door_up else None
        out.append('  link listener: ' + ((d or {}).get('listening') or ('gateway not running' if not door_up else
                                                                       'no status (gateway runs code without peer support: localmodels install)')))
        try: out += [f'  peer report warning: {wn}' for wn in (peers.report().get('keeper') or {}).get('warnings') or []]
        except Exception as e: out.append(f'  peer report error: {type(e).__name__}: {e}')   # not recorded: shown to the person running the command
    return out


def edit_settings(change):
    """Change this Mac's settings file (models.json) in place; the door rereads it as it changes. -> words or None."""
    src = llmshare.MODELS if os.path.exists(llmshare.MODELS) else llmshare.MODELS_DEFAULT
    try: d = json.load(open(src))
    except (OSError, ValueError) as e: return f'settings not readable: {src} ({type(e).__name__}: {e})'
    change(d)
    os.makedirs(os.path.dirname(llmshare.MODELS), exist_ok=True)
    tmp = llmshare.MODELS + '.tmp'
    with open(tmp, 'w') as f: json.dump(d, f, indent=1); f.write('\n')
    os.replace(tmp, llmshare.MODELS)
    return None


def token_copy(peer_ssh='<user>@<the other Mac>'):
    """The command that copies peer-token to another host over SSH (the token is never printed)."""
    from . import peers
    rel = os.path.relpath(peers.TOKEN_FILE, os.path.expanduser('~'))
    return f"""ssh {peer_ssh} 'mkdir -p "{os.path.dirname(rel)}" && umask 077 && cat > "{rel}" && chmod 600 "{rel}"' < "{peers.TOKEN_FILE}\""""


def token_words():
    from . import peers
    made = peers.make_token()
    return f"peer-token: {peers.TOKEN_FILE} ({'created' if made else 'existing'}, mode 0600; must be identical on every host; never printed)"


LOADER_LABEL = 'com.local-models.load-at-login'


def retire_loader(dry=False):
    """A dedicated peer's login loader from before the hub (com.local-models.load-at-login): retired once a family in the
    settings is loaded at start by the keeper ("load_at_start": true), so one loader runs, never two; kept otherwise, as
    the only loader. -> a line in words, or '' when there is none."""
    from . import models
    if not os.path.exists(models.LOADER): return ''
    fams = [b for b, f in (llmshare.settings().get('families') or {}).items() if isinstance(f, dict) and f.get('load_at_start')]
    if not fams: return f'LaunchAgent {LOADER_LABEL} stays: no family has "load_at_start": true, so it remains the only loader'
    if dry: return f"would retire LaunchAgent {LOADER_LABEL}: the instance manager loads {', '.join(fams)} at gateway start"
    _launchctl('bootout', f'gui/{os.getuid()}/{LOADER_LABEL}')
    try: os.remove(models.LOADER)
    except OSError as e: return f'could not remove {models.LOADER} ({e}): LaunchAgent {LOADER_LABEL} may still run alongside the instance manager'
    return f"LaunchAgent {LOADER_LABEL} retired (bootout, plist removed; script and log kept): the instance manager loads {', '.join(fams)} at gateway start"


PEER_CHECK = r"""printf 'home=%s\n' "$HOME"
if [ -d "$HOME/.lmstudio" ]; then echo lmstudio=yes; else echo lmstudio=no; fi
s=$("$HOME/.lmstudio/bin/lms" daemon status 2>&1)
if printf '%s' "$s" | grep -q "is running" && ! printf '%s' "$s" | grep -q "not running"; then echo running=its daemon; fi
if pgrep -x "LM Studio" >/dev/null 2>&1; then echo running=its window; fi
d=$(sed -n 's/.*"downloadsFolder"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$HOME/.lmstudio/settings.json" 2>/dev/null | head -1)
printf 'downloads=%s\n' "$d"
"""


def copy_model_cmd(w, dry=False):
    """localmodels peer copy-model <model> <user@peer> [--dry]: the model the same on a peer as here, as LM Studio keeps
    it (peers.copy_plan), with LM Studio closed there (it reads these files when it starts and writes them as it runs)."""
    from . import peers
    w = [x for x in w if not x.startswith('--')]
    if len(w) != 3: print('localmodels peer copy-model <model> <user@peer> [--dry]   (LM Studio closed on the peer)'); return 2
    want, target = w[1], w[2]
    fam = (llmshare.settings().get('families') or {}).get(want) or {}
    key = fam.get('key') or want
    variant = fam.get('variant') or next((r.get('variant') for r in llmshare.loaded() if llmshare.family(r['id']) == want and r.get('variant')), None)
    try: plan = peers.copy_plan(key, variant)
    except ValueError as e: print(e); return 1
    code, out = peers.remote(target, PEER_CHECK)
    info = {}
    for line in out.split('\n'):
        k, eq, v = line.partition('=')
        if eq and k in ('home', 'lmstudio', 'running', 'downloads'): info.setdefault(k, []).append(v.strip())
    if code != 0 or not info.get('home'):
        print(f"ssh {target} failed ({out.strip()[-200:] or code}): key-based auth without a password prompt is required"); return 1
    if info.get('running'):
        print(f"LM Studio is running on {target} ({' and '.join(info['running'])}): quit it there first (the UI app, and llmster: "
              '`lms daemon down`), then retry. LM Studio reads these files at startup and rewrites them while running.'); return 1
    if (info.get('lmstudio') or ['no'])[0] != 'yes':
        print(f'no ~/.lmstudio on {target}: start LM Studio there once, quit it, then retry'); return 1
    home = info['home'][0]
    downloads = (info.get('downloads') or [''])[0] or home + '/.lmstudio/models'
    dest = dict(definition=f"{home}/.lmstudio/hub/models/{key}", weights=f"{downloads}/{plan['weights_key']}",
                data=f'{home}/.lmstudio/.internal/model-data.json', config=f'{home}/.lmstudio/.internal/user-concrete-model-default-config/{key}.json')
    print(f"{'would copy' if dry else 'copying'} {key} to {target}:")
    print(f"  definition: {plan['definition']}/ -> {dest['definition']}/")
    print(f"  weights ({plan['bytes'] / 2**30:.1f} GiB, {plan['weights_key']}): {plan['weights']}/ -> {dest['weights']}/")
    print(f"  model-data.json records: {', '.join(r[0] for r in plan['records'])} (merged; the peer's file backed up)")
    if plan['config']: print(f"  default load config: {plan['config']} -> {dest['config']}")
    if dry: print('--dry: nothing copied'); return 0
    q = shlex.quote
    for what in ('definition', 'weights'):
        code, out = peers.send(plan[what], target, dest[what])
        if code != 0: print(f'  {what}: rsync failed ({out.strip()[-200:]}); nothing else changed on {target}'); return 1
        print(f'  {what}: copied')
    code, out = peers.remote(target, f'cat {q(dest["data"])} 2>/dev/null; true')
    try: doc = json.loads(out) if out.strip() else None
    except ValueError:
        print(f"  model-data.json on {target} is not valid JSON: left unchanged (definition and weights copied; LM Studio may list the model twice)"); return 1
    merged = json.dumps(peers.merge_records(doc, plan['records'])).encode()
    stamp = time.strftime('%Y%m%d-%H%M%S')
    code, out = peers.remote(target, f'set -e; mkdir -p {q(os.path.dirname(dest["data"]))}; if [ -f {q(dest["data"])} ]; then cp {q(dest["data"])} '
                                     f'{q(dest["data"] + ".bak-" + stamp)}; fi; cat > {q(dest["data"] + ".tmp")}; mv {q(dest["data"] + ".tmp")} {q(dest["data"])}', merged)
    if code != 0: print(f'  model-data.json: write failed ({out.strip()[-200:]})'); return 1
    print(f'  model-data.json: records merged (previous file: model-data.json.bak-{stamp})')
    if plan['config']:
        code, out = peers.remote(target, f'mkdir -p {q(os.path.dirname(dest["config"]))} && cat > {q(dest["config"])}', open(plan['config'], 'rb').read())
        print('  default load config: ' + ('copied' if code == 0 else f'copy failed ({out.strip()[-160:]})'))
    print(f"done. On {target} after LM Studio restarts: `lms ls` lists {key} once; `lms ps --json` reports selectedVariant {variant or '<variant>'} once loaded")
    return 0


def gpu_limit_cmd():
    """localmodels peer gpu-limit: the boot job for this Mac's GPU limit, written beside the settings with the lines a
    person runs to install it (an administrator password); one already installed is checked against it."""
    import plistlib
    from . import models
    want = models.gpu_plist()
    name = os.path.basename(models.GPU_DAEMON)
    system = '/Library/LaunchDaemons/' + name
    mb = want['ProgramArguments'][1].split('=', 1)[1]
    try: cur = int(subprocess.run(['sysctl', '-n', 'iogpu.wired_limit_mb'], capture_output=True, text=True, timeout=5).stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError): cur = 0
    have = None
    if os.path.exists(models.GPU_DAEMON):
        try: have = plistlib.load(open(models.GPU_DAEMON, 'rb'))
        except (OSError, ValueError, plistlib.InvalidFileException): have = {}
        if have == want:
            print(f"installed: {name} sets iogpu.wired_limit_mb={mb} at boot (total {models.mem_gb():.0f} GiB - 8 GiB); current value: {cur or 'default (0)'}")
            return 0
        said = ' '.join((have or {}).get('ProgramArguments') or []) or 'something else'
        print(f'installed, but it runs `{said}`; expected iogpu.wired_limit_mb={mb} (total {models.mem_gb():.0f} GiB - 8 GiB)')
    mine = os.path.join(os.path.dirname(llmshare.MODELS), name)
    os.makedirs(os.path.dirname(mine), exist_ok=True)
    with open(mine, 'wb') as f: plistlib.dump(want, f)
    print(f'wrote {mine}: LaunchDaemon setting iogpu.wired_limit_mb={mb} at boot, before login and LM Studio (current value: {cur or "default (0)"})')
    print('install (requires an administrator password):')
    if have is not None: print(f'  sudo launchctl bootout system/{want["Label"]}')
    print(f'  sudo cp "{mine}" /Library/LaunchDaemons/')
    print(f'  sudo chown root:wheel {system}')
    print(f'  sudo chmod 644 {system}')
    print(f'  sudo launchctl bootstrap system {system}')
    return 1 if have is not None else 0


def peer_cmd(a):
    """localmodels peer [add <name> <address> | remove <name> | serve <address> | serve off | token | test [name] | gpu-limit | copy-model ...]."""
    from . import peers
    w = [x for x in a if x != 'peer']
    verb = w[0] if w else ''
    if not verb:
        print(f'gateway name sent to peers: {peers.me()} (settings key "name")')
        lines = peer_lines(llmshare.hub_up(fresh=True))
        if not lines: print('no peers configured and no link listener: localmodels peer add <name> <address> (GUIDE.md, Peers)')
        for line in lines: print(line)
        if (peers.configured() or peers.serving()) and peers.token_problem(): print(f'  peer-token error: {peers.token_problem()}')
        return 0
    if verb == 'add' and len(w) == 3:
        name, u = w[1], peers.parse_url(w[2])
        if not peers.NAME.match(name): print(f'invalid peer name {name!r}: allowed characters are letters, digits, ".", "_", "-"'); return 2
        if not u: print(f'invalid address {w[2]!r}: expected 192.168.213.2 or http://192.168.213.2:1235'); return 2
        err = edit_settings(lambda s: s.setdefault('peers', {}).__setitem__(name, dict(url=u[0])))
        if err: print(err); return 1
        print(f'peer {name} added: {u[0]} ({llmshare.MODELS}; the gateway polls it within seconds)')
        if peers.address_words(u[1]): print(f'  warning: {peers.address_words(u[1])}')
        print('  ' + token_words())
        print('next steps (GUIDE.md, Peers, lists each one and which are manual):')
        print(f'  1. copy peer-token to {name}: {token_copy(f"<user>@{u[1]}")}')
        print(f'  2. on {name}: localmodels peer serve {u[1]}')
        print('  3. here: localmodels peer test')
        return 0
    if verb == 'remove' and len(w) == 2:
        if w[1] not in peers.configured(): print(f'no peer named {w[1]}'); return 2
        err = edit_settings(lambda s: (s.get('peers') or {}).pop(w[1], None) if isinstance(s.get('peers'), dict) else None)
        if err: print(err); return 1
        print(f'peer {w[1]} removed: no new requests are routed to it (in-flight requests complete)'); return 0
    if verb == 'serve' and len(w) == 2:
        if w[1] == 'off':
            err = edit_settings(lambda s: s.pop('serve', None))
            if err: print(err); return 1
            print('link listener disabled: the gateway closes it within 5 s (loopback listeners unaffected)'); return 0
        addr = w[1].strip('[]')
        try: peers.ipaddress.ip_address(addr.split('%')[0])
        except ValueError: print(f'invalid address {addr!r}: expected an IP address of this host (the Thunderbolt Bridge static address, e.g. 192.168.213.2)'); return 2
        err = edit_settings(lambda s: s.__setitem__('serve', dict(address=addr)))
        if err: print(err); return 1
        iface = peers.interface_of(addr)
        port = peers.ports().get(iface) if iface else None
        print(f'link listener configured: {addr}:{llmshare.HUB_PORT}' + (f', interface {iface}' + (f' ({port})' if port else '') if iface else ''))
        if not iface: print('  warning: no interface has that address configured yet; the listener binds once one does (GUIDE.md, Peers: static address)')
        elif not peers.bridge(iface, {iface: port} if port else {}) and not peers.loopback(addr): print(f'  warning: {iface} is not the Thunderbolt Bridge')
        if peers.address_words(addr): print(f'  warning: {peers.address_words(addr)}')
        print('  ' + token_words())
        print('  the gateway binds it within 5 s (X-Hub-Token required); `localmodels peer` shows the listener state')
        return 0
    if verb == 'gpu-limit' and len(w) == 1: return gpu_limit_cmd()
    if verb == 'copy-model': return copy_model_cmd(w, '--dry' in a)
    if verb == 'token' and len(w) == 1:
        print(token_words())
        print(f'copy to another host: {token_copy()}')
        return 0
    if verb == 'test' and len(w) <= 2:
        d = door_link(force=True)
        if d is None: print("gateway did not answer /hub/link: not running (localmodels start), or running code without peer support (localmodels install)"); return 1
        ps = [p for p in d['peers'] if len(w) == 1 or p['name'] == w[1]]
        if not ps: print(f'no peer named {w[1]}' if len(w) == 2 else 'no peers configured: localmodels peer add <name> <address>'); return 2
        bad = 0
        for p in ps:
            tok, here = peers.token(), None
            try:
                req = urllib.request.Request(p['url'] + '/hub/health', headers={'X-Hub-Token': tok or ''})
                peers._open(req, timeout=peers.CONNECT).read(); here = 'connected, token accepted'
            except urllib.error.HTTPError as e: here = f"connected, HTTP {e.code}" + (' (peer-token mismatch)' if e.code in (401, 403) else '')
            except OSError as e: here = peers.reach_words(e)
            if p['state'] == 'down' and here.startswith('connected'):    # a process's first connection after the link comes up can
                time.sleep(2)                                            # fail while macOS evaluates its Local Network policy: poll once more
                p = next((q for q in (door_link(force=True) or {}).get('peers') or [] if q['name'] == p['name']), p)
            print('gateway poll: ' + p['words'])
            print(f'  terminal probe: {here}')
            if p['state'] == 'down' and here.startswith('connected'):
                print('  terminal connects but the gateway does not (two polls): macOS Local Network privacy is blocking the gateway (a LaunchAgent). '
                      'System Settings → Privacy & Security → Local Network → enable Python (python3), then: localmodels peer test')
            iface, port = peers.route_of(p['host'])
            print(f"  route: {port or iface or 'none'}" + (f' ({iface})' if iface and port and iface not in port else '')
                  + (f" · {peers.route_words(iface, {iface: port} if port else {})}" if peers.route_words(iface, {iface: port} if port else {}) else ''))
            bad += p['state'] != 'up'
        if d.get('serving'): print(f"link listener: {d.get('listening') or 'not bound yet'}")
        return 1 if bad else 0
    print('localmodels peer [add <name> <address> | remove <name> | serve <address> | serve off | token | test [name] | gpu-limit | copy-model <model> <user@peer>]')
    return 2


def recycle_cmd(a):
    from . import recycle
    if '--status' in a:
        v = recycle.view(llmshare.loaded(fresh=True))
        for i, c in v['copies'].items():
            band = f" · threshold {c['band']['tokens']:,} (soft {c['band']['soft']:,}): {int(100 * c['share'])}%" if c.get('band') else ' · recycling off (no settings)'
            print(f"{i}: {c['tokens']:,} tokens processed ({c['requests']} requests) since {ago(c['since'])}{band}" + (f" · {c['drift']}" if c.get('drift') else ''))
        print(recycle.words(v['job']) if v.get('job') else 'no recycle in progress')
        return 0
    model = next((x for x in a if not x.startswith('-')), None) or next(iter(llmshare.settings().get('families') or {}), None)
    if not model: print('no family in the settings'); return 1
    r = llmshare.hub_post('/hub/recycle', dict(model=model))
    if r is None: print("gateway down: localmodels start"); return 1
    print(f"{model}: {r.get('note')}" if r.get('ok') else f"{model}: {r.get('note') or r.get('error')}")
    return 0 if r.get('ok') else 1


def swapcheck(models):
    models = models or [r['id'] for r in llmshare.loaded(fresh=True)]
    why = llmshare.swap_blockers(models)
    if why: print('swap refused: ' + '; '.join(why)); return 3
    print(f"safe to unload or reload {', '.join(models) or 'nothing (no model loaded)'}: nothing is using {'it' if len(models) == 1 else 'them'}")
    return 0


def use(a):
    from . import models
    if not a or a[0].startswith('-'): print('localmodels use <model key or id> [--context N] [--parallel N] [--dry]'); return 2
    code, words = models.use(a[0], opt(a, '--identifier'), opt(a, '--context'), opt(a, '--parallel'), dry='--dry' in a)
    print(words)
    return code


def keep():
    from . import models
    did = models.keep()
    print('; '.join(did) if did else 'nothing to do: every family with its first instance loaded has all replicas; no recycle due')
    return 0


def jobs(n=8):
    from . import mcp_server
    ds = sorted(glob.glob(os.path.join(mcp_server.AGENTS, '*', '*')), key=os.path.getmtime)[-n:]
    if not ds: print('no local-agent jobs started through the gateway yet'); return 0
    for d in ds:
        job = os.path.relpath(d, mcp_server.AGENTS)
        st = mcp_server.job_status(job)
        print(f"  {job}: {st.get('done')} of {st.get('of')} done" + (' · finished' if st.get('finished') else ' · running') + (f" · {st['problem'][:80]}" if st.get('problem') else ''))
    return 0


def prune(days=30, root=None):
    """Remove local-agent job folders and loose job logs older than `days` under ~/Library/Logs/LocalModels/agents (the
    one place the hub keeps prompts and answers, as each project's own record). A folder whose runner is alive, or with
    a job still running, is kept. -> (folders removed, files removed, bytes freed)."""
    from . import mcp_server
    root = root or mcp_server.AGENTS
    cutoff, folders, files, size = time.time() - days * 86400, 0, 0, 0
    for p in glob.glob(os.path.join(root, '*', '*')):
        try: m = os.path.getmtime(p)
        except OSError: continue
        if m >= cutoff: continue
        if os.path.isdir(p):
            try: pid = json.load(open(os.path.join(p, 'runner.json'))).get('pid')
            except (OSError, ValueError): pid = None
            live = [r for r in glob.glob(os.path.join(p, '*.running'))]
            if (pid and llmshare._alive(pid) and not os.path.exists(os.path.join(p, 'summary.json'))) or live: continue
            for d, _, fs in os.walk(p):
                for f in fs:
                    try: size += os.path.getsize(os.path.join(d, f))
                    except OSError: pass
            shutil.rmtree(p, ignore_errors=True); folders += 1
        elif p.endswith('.json'):
            try: size += os.path.getsize(p); os.remove(p); files += 1
            except OSError: pass
    return folders, files, size


# ---------------------------------------------------------------- local agents
def ask(a):
    from . import agent, mcp_server, whois
    root, err = mcp_server._root(a.get('dir'))
    if err: print(err); return 2
    m, err = agent.loaded_model(a.get('model'))
    if err: print(err); return 1
    project = whois.project_of(root) or os.path.basename(root)
    files = agent.Files([root], where=f'the {project} project\'s folder')
    schema = None
    if a.get('schema'): schema = json.load(open(a['schema'])) if os.path.exists(a['schema']) else json.loads(a['schema'])
    job = dict(label='ask', prompt=a['prompt'], schema=schema, max_turns=int(a.get('max_turns') or 16), **({} if a.get('think', True) else {'think': False}))
    defs = (lambda j: []) if a.get('no_files') else (lambda j: files.defs())
    with llmshare.lease(project, [m['id']], 'ask'):
        r = agent.run_job(job, m['id'], defs, lambda n, x, j: files.call(n, x), project,
                          logs=os.path.join(mcp_server.AGENTS, llmshare._safe(project)), kind='ask')
    print(json.dumps(r['result'], indent=1) if r.get('result') is not None else r.get('answer') or r.get('error'))
    print(f"\n({r['seconds']} s, {r['turns']} turns, {len(r['calls'])} tool calls; log: {r['log']})")
    return 0 if r['ok'] else 1


def batch(a):
    from . import mcp_server
    spec = json.load(open(a['jobs'])); spec = spec['jobs'] if isinstance(spec, dict) else spec
    try: job, d = mcp_server.start('batch', spec, int(a.get('parallel') or llmshare.at_once() or llmshare.settings().get('batch_at_once', 8)), a.get('dir'))
    except (ValueError, TypeError) as e: print(e); return 1
    print(f'started {len(spec)} jobs as {job}; follow it: localmodels jobs (or the dash). Results in {d}')
    return 0


# ---------------------------------------------------------------- setting the hub up
def digest(folder):
    from . import dash
    return dash.digest(folder)


def _launchctl(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)


def _loaded(label=LABEL):
    return _launchctl('print', f'gui/{os.getuid()}/{label}').returncode == 0


HOT = (os.path.join('localmodels', 'dash.html'),)     # read from disk on every request (web.page): changed alone, replaced in place


def code_changes():
    """The files that differ between this repo's code and the installed copy, repo-relative."""
    def files(root):
        return {os.path.relpath(f, root) for f in glob.glob(os.path.join(root, 'localmodels', '*')) if os.path.isfile(f) and not f.endswith('.pyc')}
    rels = sorted(files(HOME) | files(INSTALLED) | {os.path.join('bin', 'localmodels'), 'LICENSE', os.path.join('.claude-plugin', 'plugin.json')})
    def same(rel):
        a, b = os.path.join(HOME, rel), os.path.join(INSTALLED, rel)
        if not os.path.exists(a) and not os.path.exists(b): return True
        try: return open(a, 'rb').read() == open(b, 'rb').read()
        except OSError: return False
    return [r for r in rels if not same(r)]


def hot_swap(changes):
    """Replace files the gateway reads per request in the installed copy, each atomically: no restart, nothing in flight touched."""
    for rel in changes:
        dst = os.path.join(INSTALLED, rel); tmp = dst + '.new'
        shutil.copy2(os.path.join(HOME, rel), tmp); os.replace(tmp, dst)


def deploy():
    """Copy the hub's code to where launchd runs the door from (macOS keeps background programs out of ~/Documents)."""
    tmp = INSTALLED + '.new'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, 'bin'))
    shutil.copytree(os.path.join(HOME, 'localmodels'), os.path.join(tmp, 'localmodels'), ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(os.path.join(HOME, 'bin', 'localmodels'), os.path.join(tmp, 'bin', 'localmodels'))
    for rel in ('LICENSE', os.path.join('.claude-plugin', 'plugin.json')):    # the dashboard's corner reads them
        if os.path.exists(os.path.join(HOME, rel)):
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True); shutil.copy2(os.path.join(HOME, rel), os.path.join(tmp, rel))
    old = INSTALLED + '.old'
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(INSTALLED): os.replace(INSTALLED, old)
    os.replace(tmp, INSTALLED)
    shutil.rmtree(old, ignore_errors=True)


def quiet(wait=90, still=3):
    """Wait for a moment with no request passing through the door (for `still` seconds in a row), so a restart cuts
    nobody off. -> whether it came within `wait` seconds."""
    t0, calm = time.time(), None
    while True:
        if llmshare.inflight(): calm = None
        else:
            calm = calm or time.time()
            if time.time() - calm >= still: return True
        if time.time() - t0 > wait: return False
        time.sleep(0.5)


def _plist():
    env = dict(PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin')
    for k in ('LOCALMODELS_PORT', 'LMSTUDIO_URL', 'LMSTUDIO_LOGS', 'LOCALMODELS_STATE', 'LMS'):
        if os.environ.get(k): env[k] = os.environ[k]
    return dict(Label=LABEL, ProgramArguments=[PY, os.path.join(INSTALLED, 'bin', 'localmodels'), 'gateway'], RunAtLoad=True,
                KeepAlive=True, ThrottleInterval=10, StandardOutPath=os.path.join(LOGS, 'gateway.log'),
                StandardErrorPath=os.path.join(LOGS, 'gateway.log'), EnvironmentVariables=env, SoftResourceLimits=dict(NumberOfFiles=8192))


SETTINGS_FILE = os.path.expanduser('~/.claude/settings.json')
HOOK_EVENTS = ('SessionStart', 'UserPromptSubmit')


def hook_command():
    """The command Claude Code runs before each prompt: the installed copy's `localmodels notice` (silent unless maintenance)."""
    return f'"{PY}" "{os.path.join(INSTALLED, "bin", "localmodels")}" notice'


def with_hook(settings, cmd, add=True):
    """Claude Code's settings with the hub's hook added for SessionStart and UserPromptSubmit (once), or taken out; the
    person's own hooks and settings are left as they are."""
    hooks = settings.setdefault('hooks', {})
    for ev in HOOK_EVENTS:
        kept = [e for e in hooks.get(ev, []) if not any('localmodels' in h.get('command', '') and h.get('command', '').endswith(' notice')
                                                      for h in e.get('hooks', []))]
        if add: kept.append(dict(hooks=[dict(type='command', command=cmd, timeout=5)]))
        if kept: hooks[ev] = kept
        else: hooks.pop(ev, None)
    if not hooks: settings.pop('hooks')
    return settings


def set_hook(add=True):
    """Write the hook into (or out of) ~/.claude/settings.json. -> a line in words."""
    try: cur = json.load(open(SETTINGS_FILE)) if os.path.exists(SETTINGS_FILE) else {}
    except (OSError, ValueError) as e:
        return f'could not read {SETTINGS_FILE} ({type(e).__name__}): the maintenance hook is not ' + ('added' if add else 'taken out')
    new = with_hook(json.loads(json.dumps(cur)), hook_command(), add)
    if new == cur: return f"the maintenance hook is {'already in' if add else 'not in'} {SETTINGS_FILE}"
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + '.tmp'
    with open(tmp, 'w') as f: json.dump(new, f, indent=2); f.write('\n')
    os.replace(tmp, SETTINGS_FILE)
    return f"the maintenance hook {'added to' if add else 'taken out of'} {SETTINGS_FILE} (it adds a line to a session only while maintenance is set)"


def lms_found():
    """Whether LM Studio's command-line tool is where the hub looks (LMS, ~/.lmstudio/bin/lms, or on PATH)."""
    return os.path.exists(llmshare.LMS)


def _fitted():
    """The first settings for this Mac: its memory and the models LM Studio has downloaded (models.first_settings)."""
    from . import models
    return models.first_settings(models.downloaded() if lms_found() else None, models.mem_gb())


def _fitted_words(s):
    if not s.get('families'): return 'no model managed yet: ' + s.get('why', '')
    return '; '.join(f"{k}: {f['copies']} replica{'' if f['copies'] == 1 else 's'} × {f['parallel']} slots, context {f['context']:,}"
                     for k, f in s['families'].items())


def install(wait=90, plugin=False, dry=False, no_claude=False):
    uid = f'gui/{os.getuid()}'
    if dry:
        if os.path.exists(llmshare.MODELS): print(f'would keep the settings: {llmshare.MODELS}')
        else: print(f'would write the settings file {llmshare.MODELS}, fitted to this Mac: {_fitted_words(_fitted())}')
        print(f"would copy the gateway's code to {INSTALLED} and start it at login as {LABEL} ({PLIST}) with {PY}, after a moment with no request passing through")
        if plugin: print("would leave the hub's MCP tools and the rules to the Claude Code plugin (no registration, no signpost line)")
        elif no_claude: print('would register nothing with Claude Code and add no signpost line (--no-claude)')
        else: print(f"would register the hub's MCP tools for every Claude Code session (claude mcp add --scope user local-models), add one signpost line to {SIGNPOST_FILE}, "
                    f"and add the maintenance hook to {SETTINGS_FILE} (silent unless maintenance is set)")
        print(f"would link the localmodels command into the first writable of {', '.join(BINS)}")
        if retire_loader(dry=True): print(retire_loader(dry=True))
        print('nothing written (--dry)')
        return 0
    os.makedirs(LOGS, exist_ok=True)
    if not os.path.exists(llmshare.MODELS):
        os.makedirs(llmshare.SHARED, exist_ok=True); fitted = _fitted()
        with open(llmshare.MODELS, 'w') as f: json.dump(fitted, f, indent=1); f.write('\n')
        print(f'0. the settings file {llmshare.MODELS} written, fitted to this Mac: {_fitted_words(fitted)} (edit it; the gateway reloads it on change)')
    else: print(f'0. the settings: {llmshare.MODELS}')
    running = _loaded() or any(_loaded(o) for o in OLD_LABELS)
    changes = code_changes() if running and os.path.isdir(INSTALLED) else None
    hot = bool(changes) and all(c in HOT for c in changes)
    if hot:                                                 # the page alone: in place, while batches run (a restart waits for quiet)
        hot_swap(changes)
        print(f"1. {', '.join(changes)} replaced in place in {INSTALLED} (read per request): no gateway restart, nothing in flight touched; reload the page")
        r = None
    elif running and not quiet(wait):
        print(f'the gateway stayed busy for {wait / 60:.1f} minutes ({len(llmshare.inflight())} requests in flight): nothing changed; '
              'try again when it is quiet, or: localmodels install --wait 480')
        return 3
    if not hot:
        with llmshare.swapping(wait=120) as got:                    # no load or recycle half done when the door restarts
            if not got: print('a swap or a reload is under way: try again in a moment'); return 3
            for o in OLD_LABELS:
                if _loaded(o):
                    _launchctl('bootout', f'{uid}/{o}')
                    try: os.remove(os.path.expanduser(f'~/Library/LaunchAgents/{o}.plist'))
                    except OSError: pass
                    print(f'   (the gateway under its old label {o} is stopped; it restarts as {LABEL})')
            deploy()
            print(f'1. gateway code copied to {INSTALLED} ({digest(INSTALLED)})')
            plist = _plist()
            try: changed = plistlib.load(open(PLIST, 'rb')) != plist
            except (OSError, ValueError): changed = True
            os.makedirs(os.path.dirname(PLIST), exist_ok=True)
            with open(PLIST, 'wb') as f: plistlib.dump(plist, f)
            if _loaded() and changed: _launchctl('bootout', f'{uid}/{LABEL}'); time.sleep(1); r = _launchctl('bootstrap', uid, PLIST)
            elif _loaded(): r = _launchctl('kickstart', '-k', f'{uid}/{LABEL}')
            else: r = _launchctl('bootstrap', uid, PLIST)
    up = any(llmshare.hub_up(fresh=True) or time.sleep(0.5) for _ in range(30))
    print(f"2. gateway starts at login ({PLIST}): {'listening on ' + llmshare.HUB if up else 'NOT responding: ' + ((r.stderr.strip() if r else '') or 'see ' + os.path.join(LOGS, 'gateway.log'))}")
    if up and retire_loader(dry=True): print('   ' + retire_loader())
    if plugin:
        print("3. the hub's MCP tools come with the Claude Code plugin (its .mcp.json): nothing registered here")
        print("4. no signpost line: the plugin's skill carries the rules into every session")
    elif no_claude:
        print('3. nothing registered with Claude Code (--no-claude)')
        print('4. no signpost line (--no-claude)')
    else:
        claude = shutil.which('claude')
        if claude:
            want = [PY, os.path.join(HOME, 'bin', 'localmodels'), 'mcp']
            cur = subprocess.run([claude, 'mcp', 'get', 'local-models'], capture_output=True, text=True)
            if cur.returncode == 0 and all(x in cur.stdout for x in want) and 'User' in cur.stdout:
                print('3. the hub\'s MCP tools are already registered for every Claude Code session (local-models)')
            else:
                subprocess.run([claude, 'mcp', 'remove', '--scope', 'user', 'local-models'], capture_output=True, text=True)
                r = subprocess.run([claude, 'mcp', 'add', '--scope', 'user', 'local-models', '--', *want], capture_output=True, text=True)
                print('3. the hub\'s MCP tools registered for every Claude Code session (local-models)' if r.returncode == 0 else
                      f'3. could not register the tools: {(r.stderr or r.stdout).strip()[:300]}')
        else: print('3. the claude command is not on PATH: register the MCP tools manually (GUIDE.md, setting up)')
        os.makedirs(os.path.dirname(SIGNPOST_FILE), exist_ok=True)
        text = open(SIGNPOST_FILE).read() if os.path.exists(SIGNPOST_FILE) else ''
        lines = text.split('\n')
        mine = [i for i, l in enumerate(lines) if 'local-models hub signpost' in l]
        if len(mine) == 1 and lines[mine[0]] == SIGNPOST: print(f'4. the signpost line is already in {SIGNPOST_FILE}')
        elif mine:                                          # the line install owns: brought to the current wording, once
            lines = [SIGNPOST if i == mine[0] else l for i, l in enumerate(lines) if i == mine[0] or i not in mine]
            tmp = f'{SIGNPOST_FILE}.{os.getpid()}.tmp'
            with open(tmp, 'w') as f: f.write('\n'.join(lines))
            os.replace(tmp, SIGNPOST_FILE)
            print(f'4. the signpost line updated in {SIGNPOST_FILE}')
        else:
            with open(SIGNPOST_FILE, 'a') as f: f.write(('' if not text or text.endswith('\n') else '\n') + SIGNPOST + '\n')
            print(f'4. the signpost line added to {SIGNPOST_FILE}')
        print('   ' + set_hook(True))
    src = os.path.join(HOME, 'bin', 'localmodels')
    for d in BINS:
        link = os.path.join(d, 'localmodels')
        if not os.path.isdir(d) or not os.access(d, os.W_OK): continue
        if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(src): print(f'5. {link} already runs this hub'); break
        if not os.path.exists(link): os.symlink(src, link); print(f'5. the localmodels command is on PATH ({link})'); break
        print(f'5. {link} exists and is something else: left as it is'); break
    else: print('5. no folder on PATH to link the command into: use ' + src)
    return 0 if up else 1


def uninstall():
    uid = f'gui/{os.getuid()}'
    for label in (LABEL,) + OLD_LABELS:
        if _loaded(label): _launchctl('bootout', f'{uid}/{label}')
        try: os.remove(os.path.expanduser(f'~/Library/LaunchAgents/{label}.plist'))
        except OSError: pass
    shutil.rmtree(INSTALLED, ignore_errors=True)
    print('1. gateway stopped; it no longer starts at login')
    claude = shutil.which('claude')
    if claude: subprocess.run([claude, 'mcp', 'remove', '--scope', 'user', 'local-models'], capture_output=True, text=True)
    print('2. the hub\'s MCP tools are no longer registered')
    if os.path.exists(SIGNPOST_FILE):
        lines = [l for l in open(SIGNPOST_FILE).read().split('\n') if 'local-models hub signpost' not in l]
        tmp = SIGNPOST_FILE + '.tmp'; open(tmp, 'w').write('\n'.join(lines)); os.replace(tmp, SIGNPOST_FILE)
    print(f'3. the signpost line is out of {SIGNPOST_FILE}')
    print('   ' + set_hook(False))
    src = os.path.join(HOME, 'bin', 'localmodels')
    for d in BINS:
        link = os.path.join(d, 'localmodels')
        if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(src): os.remove(link)
    print('4. the localmodels command is off PATH (the repo and the settings file are untouched)')
    return 0


def name_cmd(a):
    """localmodels name [project <seen> <shown> | batch <project> <seen> <shown> | private|public <project> | forget <project> [<seen>]]."""
    from . import dash, names, runs, sessions
    words = [x for x in a if x != 'name']
    try:
        if words[:1] == ['project'] and len(words) == 3:
            names.set_project(words[1], words[2]); print(f'{words[1]} shows as {names.project(words[1])} on every screen'); return 0
        if words[:1] == ['batch'] and len(words) == 4:
            names.set_label(words[1], words[2], words[3]); print(f'in {names.project(words[1])}, {words[2]} shows as {words[3]}'); return 0
        if words[:1] in (['private'], ['public']) and len(words) == 2:
            names.set_private(words[1], words[0] == 'private')
            print(f"{names.project(words[1])} is {'private: screens show only the names set for it (no session titles, no unnamed scripts, steps or batches)' if words[0] == 'private' else 'public: names show as they are seen unless named'}")
            return 0
        if words[:1] == ['forget'] and len(words) in (2, 3):
            names.forget(words[1], words[2] if len(words) == 3 else None); print('forgotten'); return 0
    except OSError as e: print(f'could not write {names.FILE}: {e}'); return 1
    if words:
        print('localmodels name project <as seen> <shown> | batch <project> <as seen> <shown> | private <project> | public <project> | forget <project> [<as seen>]')
        return 2
    seen = {}                                                   # as seen -> {requests, batches, jobs, scripts, sessions}
    def at(p): return seen.setdefault(p or '?', dict(requests=0, batches=set(), jobs=set(), scripts=set(), sessions=0))
    for r in llmshare.requests_log(24 * 7):
        s = at(r.get('project')); s['requests'] += 1
        if r.get('run'): s['batches'].add(r['run'])
        p = runs.parse_step(r.get('step'))
        if p: s['jobs'].add(p[0])
    for c in sessions.recent(72): at(c.get('project'))['sessions'] += 1
    for u in dash.users(shown=False):
        if u.get('app'): at(u.get('project'))['scripts'].add(u['app'])
    print(f'names are set in {names.FILE}; as seen -> shown, the last 7 days:')
    for p in sorted(seen, key=lambda x: -seen[x]['requests']):
        s, shown = seen[p], names.project(p)
        head = f"  {p}" + (f" -> {shown}" if shown != p else '') + (' (private)' if names.private(p) else '')
        bits = [f"{s['requests']} requests"] if s['requests'] else []
        if s['sessions']: bits.append(f"{s['sessions']} Claude Code session{'s' if s['sessions'] > 1 else ''}")
        print(head + ': ' + ', '.join(bits))
        for kind in ('batches', 'jobs', 'scripts'):
            if s[kind]:
                print(f"     {kind}: " + ', '.join(x + (f" -> {names.label(p, x)}" if names.label(p, x) != x else '') for x in sorted(s[kind])))
    print('set one: localmodels name project <as seen> <shown> | batch <project> <as seen> <shown> | private <project>')
    return 0


def _minutes(word):
    """'30m', '30', '1h', '1h30m', '90s' -> minutes (at least 1), or None."""
    word = (word or '').strip().lower()
    if word.isdigit(): return max(1, int(word))
    m = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', word)
    if not m or not any(m.groups()): return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return max(1, h * 60 + mi + round(s / 60))


def _at(word):
    """'20:30' -> that time today, or tomorrow when it has passed; None when it is not a time."""
    m = re.fullmatch(r'(\d{1,2}):(\d\d)', word or '')
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59: return None
    t = time.localtime()
    at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
    return at + 86400 if at <= time.time() else at


def errors_cmd(minutes=1440, trace=False):
    """localmodels errors: the error record, newest first (a repeat within a minute is counted, not repeated)."""
    from . import errors
    rs = errors.recent(minutes, 200)
    span = f'{minutes / 60:g} hours' if minutes >= 120 else f'{minutes:g} minutes'
    if not rs: print(f'no errors recorded in the last {span} ({errors.FILE})'); return 0
    print(f'{len(rs)} error{"s" if len(rs) != 1 else ""} recorded in the last {span}, newest first ({errors.FILE}):')
    for r in rs:
        print(f"  {time.strftime('%d %b %H:%M:%S', time.localtime(r.get('at', 0)))}  {r.get('source')} · {r.get('where')}: {r.get('error')}"
              + (f"  (+{r['repeats']} repeats)" if r.get('repeats') else ''))
        if trace and r.get('trace'): print('      ' + str(r['trace']).strip().replace('\n', '\n      '))
    return 0


def present_cmd(a):
    """localmodels present on [for 2h] [show a,b] | off: screenshot mode (names.py)."""
    from . import demo, names
    words = [x for x in a if x != 'present']
    if words[:1] == ['off']:
        names.present_off(); print('screenshot mode off: every screen shows names as set'); return 0
    if words[:1] == ['on']:
        minutes, keep, i = 120, demo.names() + ['local-models'], 1
        while i < len(words):
            if words[i] == 'for' and i + 1 < len(words) and _minutes(words[i + 1]): minutes = _minutes(words[i + 1]); i += 2; continue
            if words[i] == 'show' and i + 1 < len(words): keep = [x for x in words[i + 1].split(',') if x]; i += 2; continue
            print(f'not understood: {words[i]} (localmodels present on [for 2h] [show a,b])'); return 2
        d = names.set_present(minutes, keep)
        print(f"screenshot mode on until {time.strftime('%H:%M', time.localtime(d['until']))}: screens show {', '.join(d['keep'])} by name, "
              'every other project as "other", no session titles, no error details (localmodels present off ends it)')
        return 0
    d = names.presenting()
    print(f"screenshot mode on until {time.strftime('%H:%M', time.localtime(d['until']))}, showing {', '.join(d['keep'])}" if d else 'screenshot mode off')
    return 0


def demo_cmd(a):
    """localmodels demo [--minutes 60] [--dry]: a synthetic workload under generic names (demo.py)."""
    from . import demo
    minutes = 60.0
    if '--minutes' in a:
        try: minutes = float(a[a.index('--minutes') + 1])
        except (IndexError, ValueError): print('localmodels demo [--minutes 60] [--dry]'); return 2
    if '--dry' in a:
        model, n, share = demo.plan(minutes)
        print(f"would send {n} requests at once on {model or '(no model loaded)'} for {minutes:g} min, through {llmshare.HUB}: "
              + ', '.join(f'{p[0]} ({share[p[0]]} at once: {p[1]}, {p[2]})' for p in demo.PROJECTS))
        return 0
    if not llmshare.hub_up(fresh=True): print(f'gateway down at {llmshare.HUB}: localmodels start'); return 1
    if '--project' in a:                                  # one project's program, started by the demo itself
        opt = lambda k, d=None: a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else d
        out = demo.run(minutes=minutes, workers=int(opt('--workers', 1)), model=opt('--model'), seed=int(opt('--seed', 0)), only=opt('--project'))
        return 0 if all(v['done'] for v in out.values()) else 1
    return demo.spawn(minutes)


def maintenance_cmd(a):
    """localmodels maintenance [in 15m | at 20:30 | now | after-batches] [for 30m] [why ...] | off."""
    from . import dash, maint, models
    words = [x for x in a if x != 'maintenance']
    if words[:1] == ['off']:
        for line in models.end_maintenance(): print(line)
        print('maintenance ended: requests are admitted again and the instance manager resumes')
        return 0
    if not words:
        mt = maint.view()
        print('maintenance: ' + (mt['words'] if mt else 'none set'))
        from . import names
        for g in sorted(dash.live_runs().values(), key=lambda g: g['started']):
            p = g
            clash = mt and mt.get('start') and p.get('finish') and p['finish'] > mt['start']
            print(f"  {names.project(g['project'])} · {names.label(g['project'], g['name']) or 'a batch'}: {p['done']} of {p['total'] or '?'}"
                  + (f", done about {finish_words(p['finish'])}" if p.get('finish') else ', no estimate yet')
                  + (' · ends after maintenance starts' if clash else ''))
        return 0
    start, after, minutes, why, i = None, False, 30, [], 0
    while i < len(words):
        w = words[i]
        if w == 'in' and i + 1 < len(words) and _minutes(words[i + 1]):
            start = time.time() + 60 * _minutes(words[i + 1]); i += 2; continue
        if w == 'at' and i + 1 < len(words) and _at(words[i + 1]):
            start = _at(words[i + 1]); i += 2; continue
        if w == 'for' and i + 1 < len(words) and _minutes(words[i + 1]):
            minutes = _minutes(words[i + 1]); i += 2; continue
        if w == 'now': start = time.time(); i += 1; continue
        if w in ('after-batches', 'after'): after = True; i += 1; continue
        why.append(w); i += 1
    if start is None and not after:
        print('say when: localmodels maintenance in 15m | at 20:30 | now | after-batches [for 30m] [why ...]'); return 2
    m = maint.announce(start=start, minutes=minutes, why=' '.join(why), after_batches=after)
    print('set: ' + maint.words(m))
    print('  every gateway response now carries X-Hub-Maintenance; the dashboard, `localmodels`, the status tool and the')
    print('  Claude Code hook report it; once active, new requests get 503 + Retry-After and the instance manager pauses')
    from . import names
    for g in sorted(dash.live_runs().values(), key=lambda g: g['started']):
        if m.get('start') and g.get('finish') and g['finish'] > m['start']:
            print(f"  note: {names.project(g['project'])} · {names.label(g['project'], g['name']) or 'a batch'} is due to finish about {finish_words(g['finish'])}, after it starts")
    if m['phase'] != 'on': return 0
    rows = llmshare.loaded(fresh=True)
    if llmshare.read_ok(): maint.freeze(llmshare.family(r['id']) for r in rows)
    n = len(llmshare.inflight())
    if n: print(f'waiting for {n} active requests to complete (up to 10 min) ...')
    if quiet(wait=600, still=3): print('quiet: nothing is being answered. Go ahead. When done: localmodels maintenance off')
    else: print(f'{len(llmshare.inflight())} requests are still being answered after 10 min; they finish on their own. When done: localmodels maintenance off')
    return 0


def door(verb):
    uid = f'gui/{os.getuid()}'
    if verb == 'start' and _loaded() and llmshare.hub_up(fresh=True): print(f'gateway already listening on {llmshare.HUB}'); return 0
    if verb in ('stop', 'restart') and _loaded() and not quiet():
        print(f'gateway busy ({len(llmshare.inflight())} requests in flight for 90 s): left running; retry when idle')
        return 3
    if verb == 'stop':
        r = _launchctl('bootout', f'{uid}/{LABEL}'); print('gateway stopped until the next login (localmodels start)' if r.returncode == 0 else r.stderr.strip()); return r.returncode
    if not os.path.exists(PLIST): print('not installed: localmodels install'); return 1
    if verb == 'start': r = _launchctl('kickstart', f'{uid}/{LABEL}') if _loaded() else _launchctl('bootstrap', uid, PLIST)   # never -k: a running door is left alone
    else: r = _launchctl('kickstart', '-k', f'{uid}/{LABEL}') if _loaded() else _launchctl('bootstrap', uid, PLIST)
    up = any(llmshare.hub_up(fresh=True) or time.sleep(0.5) for _ in range(30))
    print(f"gateway {'listening on ' + llmshare.HUB if up else 'not responding: see ' + os.path.join(LOGS, 'gateway.log')}")
    return 0 if up else 1


# ---------------------------------------------------------------- the self-test
def run_tests(quiet=False):
    """The sandbox tests (no model touched): -> [(name, ok, last line or the failures)]."""
    out = []
    for name in TESTS:
        path = os.path.join(HOME, 'tests', name)
        if not os.path.exists(path): continue
        try: p = subprocess.run([PY, path], capture_output=True, text=True, timeout=300)
        except subprocess.SubprocessError as e: out.append((name, False, str(e))); continue
        fails = [l.strip()[5:] for l in p.stdout.split('\n') if l.strip().startswith('FAIL')]
        last = (p.stdout.strip().split('\n') or [''])[-1]
        out.append((name, p.returncode == 0, last if not fails else '; '.join(fails)[:300] if p.returncode else last))
        if p.returncode and not fails: out[-1] = (name, False, (p.stderr.strip().split('\n') or ['?'])[-1][:300])
    return out


def check():
    from . import crashes, dash, mcp_server, whois
    ok = []
    def t(name, good, note=''): ok.append(good); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))
    t('gateway responds', llmshare.hub_up(fresh=True), llmshare.HUB)
    try:
        page = urllib.request.urlopen(llmshare.HUB + '/', timeout=5).read()
        snap = json.load(urllib.request.urlopen(llmshare.HUB + '/hub/snapshot', timeout=30))
        t('the dashboard answers', NAME.encode() in page and b'__HUB_NAME__' not in page and 'lmstudio' in snap, f'{llmshare.HUB}/ (localmodels open)')
    except (OSError, ValueError) as e: t('the dashboard answers', False, str(e))
    try: models = json.load(urllib.request.urlopen(llmshare.HUB + '/v1/models', timeout=5)).get('data', []); t('LM Studio reachable via the gateway', True, f'{len(models)} models listed')
    except OSError as e: t('LM Studio reachable via the gateway', False, str(e))
    t('the lms command is found', lms_found(), llmshare.LMS if lms_found() else f"not at {llmshare.LMS} and not on PATH: open LM Studio once and run ~/.lmstudio/bin/lms bootstrap, or set LMS; without it the instance manager cannot load, reload or recycle instances")
    rows = llmshare.loaded(fresh=True)
    ids = {r['id'] for r in rows}
    t('the model listing reads whole', llmshare.read_ok(), f"{len(rows)} loaded" if llmshare.read_ok() else 'lms ps failed or partial: the instance manager waits for a complete listing')
    for base, f in (llmshare.settings().get('families') or {}).items():
        if base not in ids: continue                       # the keeper loads copies only while the first one is loaded
        want = llmshare.copy_ids(base, f.get('copies', 1))
        have = [c for c in want if c in ids]
        t(f'{base}: all replicas loaded', len(have) == len(want), f'{len(have)}/{len(want)} loaded, {llmshare.at_once(base)} slots')
    from . import peers
    for p in peers.view():
        t(f"peer {p['name']}: reachable over the link", p['state'] == 'up', p['words'])
    if peers.serving():
        dl = door_link()
        t('link listener bound', bool(dl and (dl.get('listening') or '').startswith('listening')),
          (dl or {}).get('listening') or 'no status (gateway runs code without peer support: localmodels install)')
    for name, good, note in run_tests():
        t(f'{name[5:-3]}: the sandbox tests pass', good, note)
    down = crashes.down()
    t('no model is down after a crash', not down, '; '.join(d['words'] for d in down) or 'localmodels crashes for the record')
    from . import errors as _errors
    errs = _errors.recent(60, 200)
    t('no errors recorded in the last hour', not errs,
      f"{len(errs)} recorded; last: {errs[0].get('source')} · {errs[0].get('where')}: {errs[0].get('error')} (localmodels errors --trace)" if errs else 'localmodels errors for the record')
    if dash.door()['stale']: print("  skip instance-manager check (the gateway runs stale code until localmodels install)")
    else:
        seen = time.time() - (crashes._read(fresh=True).get('at') or 0)
        t("instance manager polls every 20 s (crashes, replicas, recycles)", seen < 90, f'last look {seen:.0f} s ago')
    me = whois.identity(os.getpid())
    t('whois identifies the client process', me.get('project') is not None, f"{me.get('project')} · {me.get('app')}")
    loaded = [r['id'] for r in rows]
    busy = sum(llmshare.busy_slots().values())
    if loaded and llmshare.hub_up() and busy < llmshare.at_once():
        t0 = time.time()
        try:
            r = llmshare.post('/v1/chat/completions', dict(model=loaded[0], messages=[{'role': 'user', 'content': 'Say OK.'}], max_tokens=5), 'local-models', 'test', 300)
            got = bool(r.get('choices'))
        except OSError as e: got = False
        time.sleep(0.3)
        mine = [d for d in llmshare.requests_log(0.5) if d.get('at', 0) >= t0 and d.get('project') == 'local-models' and d.get('kind') == 'test']
        t('test request through the gateway completed and logged', got and bool(mine),
          f"{mine[-1].get('project')} · {mine[-1].get('kind')} · {mine[-1].get('seconds')} s" + (f" (queued {mine[-1].get('waited')} s)" if (mine[-1].get('waited') or 0) > 2 else '')
          if mine else 'nothing logged')
    else: print('  skip test request (no model loaded, gateway down, or every slot busy)')
    p = subprocess.run([PY, os.path.join(HOME, 'bin', 'localmodels'), 'mcp'], input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                       '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n', capture_output=True, text=True, timeout=30)
    names = []
    for line in p.stdout.split('\n'):
        try: names += [x['name'] for x in json.loads(line).get('result', {}).get('tools', [])]
        except (ValueError, AttributeError): pass
    t('the tools answer (MCP)', set(names) == {x['name'] for x in mcp_server.TOOLS}, ', '.join(names))
    t('gateway runs this code', not dash.door()['stale'], 'localmodels install refreshes it' if dash.door()['stale'] else digest(HOME))
    print(f"{sum(ok)} of {len(ok)} ok")
    return 0 if all(ok) else 1


# ---------------------------------------------------------------- publishing
def _git(*args, check=True):
    r = subprocess.run(['git', '-C', HOME, *args], capture_output=True, text=True)
    if check and r.returncode: raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


PRIVATE_WORDS = os.environ.get('LOCALMODELS_PRIVATE_WORDS') or os.path.join(llmshare.SHARED, 'private-words.txt')
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'     # git's empty tree: "since the start" when nothing is tagged


def read_words(path):
    """A private word list: one pattern a line (a regular expression, case does not matter; a line that is not a valid
    one is read as plain text), `allow: <pattern>` for text that may appear though it holds a word, # for comments.
    -> (words, allows), or None when there is no such file."""
    try: lines = open(path).read().split('\n')
    except OSError: return None
    def rx(p):
        try: return re.compile(p, re.I)
        except re.error: return re.compile(re.escape(p), re.I)
    words, allows = [], []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'): continue
        if line.lower().startswith('allow:'): allows.append(rx(line[6:].strip()))
        else: words.append(rx(line))
    return words, allows


def private_check(repo, words_file):
    """What would go public in the next push, read against the private word list: every line added since the last
    release tag, the names of files added, and the commit messages. -> {status ('skipped', 'clean' or 'hits'), since,
    lines, files, commits, hits: [(where, text)]}."""
    out = dict(status='skipped', since=None, lines=0, files=0, commits=0, hits=[])
    wl = read_words(words_file)
    if wl is None: return out
    words, allows = wl
    git = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True).stdout
    tag = git('describe', '--tags', '--abbrev=0').strip() or None
    base, out['since'] = tag or EMPTY_TREE, tag

    def held(text):
        for a in allows: text = a.sub(' ', text)
        return any(w.search(text) for w in words)
    path, n = None, 0
    for line in git('diff', '--unified=0', '--no-color', '--no-ext-diff', f'{base}..HEAD').split('\n'):
        if line.startswith('+++ '): path = line[6:] if line.startswith('+++ b/') else None; continue
        if line.startswith('@@'):
            m = re.search(r'\+(\d+)', line); n = int(m.group(1)) if m else 0; continue
        if line.startswith('+') and path:
            out['lines'] += 1
            if held(line[1:]): out['hits'].append((f'{path}:{n}', line[1:].strip()[:160]))
            n += 1
    for f in git('diff', '--name-only', '--diff-filter=AR', f'{base}..HEAD').split('\n'):
        if not f: continue
        out['files'] += 1
        if held(f): out['hits'].append((f'file name {f}', f))
    for rec in git('log', '--format=%h%x00%B%x1e', f'{base}..HEAD' if tag else 'HEAD').split('\x1e'):
        if '\x00' not in rec: continue
        sha, body = rec.strip().split('\x00', 1)
        out['commits'] += 1
        for line in body.split('\n'):
            if held(line): out['hits'].append((f'commit {sha}', line.strip()[:160]))
    out['status'] = 'hits' if out['hits'] else 'clean'
    return out


def publish(a):
    """Tests, the version stamped into the code and the plugin manifest, a commit, a tag, a push. Nothing leaves this Mac
    until the push, and the push needs a remote (--remote URL the first time, or set up by hand)."""
    try:
        if _git('status', '--porcelain'): print('the working tree has changes: commit or stash them first (git status)'); return 2
        branch = _git('rev-parse', '--abbrev-ref', 'HEAD')
    except RuntimeError as e: print(f'git: {e}'); return 2
    print('1. the sandbox tests:')
    bad = False
    for name, good, note in run_tests():
        print(f"   {'ok  ' if good else 'FAIL'} {name}: {note}"); bad = bad or not good
    if bad: print('   fix the tests first; nothing published'); return 1
    print('2. the plugin check:')
    claude = shutil.which('claude')
    if claude:
        try: r = subprocess.run([claude, 'plugin', 'validate', HOME], capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as e: print(f'   could not run claude plugin validate: {e}; nothing published'); return 1
        if r.returncode != 0:
            for line in (r.stdout + r.stderr).strip().split('\n')[-12:]: print('   ' + line)
            print('   fix the plugin first; nothing published'); return 1
        print('   claude plugin validate: passed' + (' with warnings' if 'warning' in (r.stdout + r.stderr).lower() else ''))
    else: print('   the claude command is not on PATH: not checked')
    print('3. the privacy check:')
    pc = private_check(HOME, PRIVATE_WORDS)
    if pc['status'] == 'skipped':
        print(f'   no private word list at {PRIVATE_WORDS}: not checked (one word or pattern a line, `allow: <pattern>` for what may')
        print('   appear, # for notes; then every release is read against it before anything is pushed)')
    elif pc['status'] == 'hits':
        for where, text in pc['hits'][:30]: print(f'   {where}: {text}')
        if len(pc['hits']) > 30: print(f"   ... and {len(pc['hits']) - 30} more")
        print(f"   nothing published: change these, or add an `allow:` line to {PRIVATE_WORDS} for what is fine to make public"); return 1
    else:
        print(f"   clean: {pc['lines']} new lines, {pc['files']} new files and {pc['commits']} commit messages since {pc['since'] or 'the start'}")
    if '--dry' in a: print('--dry: the checks only; nothing stamped, committed or pushed'); return 0
    cur = VERSION if VERSION.count('.') == 2 else VERSION + '.0'
    want = opt(a, '--version')
    if not want:
        x, y, z = (int(p) for p in cur.split('.')); want = f'{x}.{y}.{z + 1}'
    if not re.match(r'^\d+\.\d+\.\d+$', want): print(f'{want!r} is not a version like 1.2.3'); return 2
    init = os.path.join(HOME, 'localmodels', '__init__.py')
    text = open(init).read()
    new = re.sub(r"VERSION = '[^']*'", f"VERSION = '{want}'", text, count=1)
    if new == text: print('could not find VERSION in localmodels/__init__.py'); return 2
    open(init, 'w').write(new)
    for mf in ('.claude-plugin/plugin.json',):
        p = os.path.join(HOME, mf)
        try:
            d = json.load(open(p)); d['version'] = want
            open(p, 'w').write(json.dumps(d, indent=2) + '\n')
        except (OSError, ValueError) as e: print(f'   {mf}: {e}')
    try:
        _git('add', 'localmodels/__init__.py', '.claude-plugin/plugin.json')
        _git('commit', '-q', '-m', f'Release {want}')
        _git('tag', '-a', f'v{want}', '-m', f'Release {want}')
    except RuntimeError as e: print(f'git: {e}'); return 1
    print(f'4. version {want} stamped, committed and tagged (v{want}) on {branch}')
    remote = opt(a, '--remote')
    try:
        if remote and 'origin' not in _git('remote').split(): _git('remote', 'add', 'origin', remote)
        if 'origin' not in _git('remote').split():
            print('5. no remote yet: create an empty repository on GitHub (public), then either\n'
                  f'   localmodels publish --remote git@github.com:<owner>/<repo>.git   (next time; this release is committed and tagged)\n'
                  '   or: git remote add origin <url> && git push -u origin main --follow-tags')
            return 0
        _git('push', 'origin', branch, '--follow-tags')
        url = _git('remote', 'get-url', 'origin')
    except RuntimeError as e: print(f'5. push failed: {e}'); return 1
    m = re.search(r'github\.com[:/]([^/]+)/([^/.]+)', url)
    print(f'5. pushed to {url}')
    if m: print(f'   install line for Claude Code users: /plugin install local-models --marketplace {m.group(1)}/{m.group(2)}')
    return 0


def main(argv):
    a = list(argv)
    verb = a.pop(0) if a and not a[0].startswith('-') else 'status'
    if verb in ('status', 'st'): return status()
    if verb == 'dash':
        from . import dash
        return dash.main(a)
    if verb == 'open':
        subprocess.run(['open', llmshare.HUB + '/']); print(f'the dashboard: {llmshare.HUB}/'); return 0
    if verb == 'log':
        h = num(a, '--hours', 24.0); return log(h) if h is not None else 2
    if verb == 'swapcheck': return swapcheck([x for x in a if not x.startswith('-')])
    if verb == 'jobs': return jobs()
    if verb == 'prune':
        d = num(a, '--days', 30.0)
        if d is None: return 2
        from . import mcp_server
        f, n, b = prune(d)
        print(f'removed {f} job folder{"s" if f != 1 else ""} and {n} job log{"s" if n != 1 else ""} older than {d:g} days from {mcp_server.AGENTS} ({b / 1e6:.1f} MB freed)')
        return 0
    if verb == 'use': return use(a)
    if verb == 'keep': return keep()
    if verb == 'recycle': return recycle_cmd(a)
    if verb == 'maintenance': return maintenance_cmd(a)
    if verb == 'present': return present_cmd(a)
    if verb == 'demo': return demo_cmd(a)
    if verb == 'name': return name_cmd(a[1:] if a[:1] == ['name'] else a)
    if verb in ('peer', 'peers'): return peer_cmd(a)
    if verb == 'notice':
        from . import maint; return maint.notice_main()
    if verb == 'crashes':
        d = num(a, '--days', 7.0); return crashes_cmd(d, '--scan' in a) if d is not None else 2
    if verb == 'errors':
        m = num(a, '--minutes', 1440.0); return errors_cmd(m, '--trace' in a) if m is not None else 2
    if verb == 'ask':
        skip = {opt(a, '--dir'), opt(a, '--schema'), opt(a, '--max-turns'), opt(a, '--model')}
        q = next((x for x in a if not x.startswith('-') and x not in skip), None)
        if not q: print('localmodels ask "<question>"'); return 2
        mt = num(a, '--max-turns', 16)
        if mt is None: return 2
        return ask(dict(prompt=q, dir=opt(a, '--dir'), schema=opt(a, '--schema'), max_turns=int(mt), model=opt(a, '--model'), no_files='--no-files' in a, think='--no-think' not in a))
    if verb == 'batch':
        if not a or a[0].startswith('-'): print('localmodels batch <jobs.json>'); return 2
        par = num(a, '--parallel', 0)
        if par is None: return 2
        return batch(dict(jobs=a[0], dir=opt(a, '--dir'), parallel=int(par) or None))
    if verb == 'check': return check()
    if verb == 'install':
        w = num(a, '--wait', 1.5); return install(w * 60, plugin='--plugin' in a, dry='--dry' in a, no_claude='--no-claude' in a) if w is not None else 2
    if verb == 'uninstall': return uninstall()
    if verb in ('start', 'stop', 'restart'): return door(verb)
    if verb == 'gateway':
        from . import gateway
        return gateway.main(a)
    if verb == 'mcp':
        from . import mcp_server
        return mcp_server.main()
    if verb == 'agent-run':
        from . import mcp_server
        if not a: print('localmodels agent-run <folder>'); return 2
        return mcp_server.run_folder(a[0])
    if verb == 'publish': return publish(a)
    if verb in ('help', '-h', '--help'): print(__doc__.strip()); return 0
    if verb == 'version': print(VERSION); return 0
    print(f'unknown: {verb} (localmodels help)'); return 2
