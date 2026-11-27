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
  localmodels maintenance [in 15m | at 20:30 | now | after-batches] [for 30m] [reason ...] | off
                                 announce a maintenance window (X-Hub-Maintenance, dashboard, MCP status, Claude Code
                                 hook); while active, new requests get 503 + Retry-After and the instance manager pauses;
                                 off reloads the families loaded at its start. No arguments: the window and overlapping runs
  localmodels off [--force] [reason ...]   switch the local models off: new requests get 503, every instance is unloaded
                                 once the requests in flight finish, and nothing is loaded, reloaded or started (crash
                                 recovery, load_at_start, LM Studio's server) until `localmodels on`; kept across restarts.
                                 Refused while a run holds a lease on a loaded model, unless --force
  localmodels off --peers [reason ...]   the same here, while the gateway keeps admitting requests and sends every one to
                                 its peers' slots (503 with Retry-After when no peer can take it); nothing is cut and no
                                 lease refuses it: this Mac's instances are unloaded once the requests on them finish
  localmodels on                 switch them on again: the families loaded when switched off are loaded (all replicas)
  localmodels app install | uninstall   the menu-bar app: built here from app/ with the Command Line Tools' swiftc, signed
                                 ad hoc, in ~/Applications (state icon, on/off per host, two notifications, and a section
                                 for each extension)
  localmodels ext [check]        the extensions the menu-bar app draws (a manifest each in the state folder's extensions/,
                                 GUIDE.md "Extensions"): each one's menu answering or not; check: every manifest and the
                                 menu it serves read against the protocol (exit 1 on a problem)
  localmodels name               each project as seen and as shown on every screen, with its batches, jobs and scripts
  localmodels name project <as seen> <shown>            what every screen calls a project (a folder or an X-Project)
  localmodels name batch <project> <as seen> <shown>    a name for one of its batches, jobs or scripts
  localmodels name private <project> | public <project> private: screens show only the names set for it
  localmodels name forget <project> [<as seen>]         drop a name: one label, or the project's whole entry
  localmodels present on [for 2h] [show a,b] | off      screenshot mode: screens show the projects named (the demo's
                                 and local-models by default) and every other one as "other", with no session titles
  localmodels pictures [--out docs] [--dry]   the README's pictures of the dashboard (headless Chrome, 1512 x 900 and
                                 1728 x 1117 at 2x, dark and light, close-ups cut along the page's own boxes); only in
                                 screenshot mode with 5 minutes left
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
  localmodels peer off <name> [--force] [reason ...]   switch a peer's local models off from this host (what
                                 `localmodels off` does there: unloaded once its requests in flight finish, 503 until on;
                                 refused while a run there holds a lease, unless --force); no request is routed to it
  localmodels peer on <name>     switch them on again there: the families loaded when switched off are loaded
  localmodels peer log [--hours 24]   each change of a peer's state (up, down, refused, maintenance), with its reason
  localmodels peer token         create peer-token (0600) if missing; print the ssh command that copies it to another host
  localmodels peer gpu-limit     write the boot LaunchDaemon com.local-models.gpu-limit (iogpu.wired_limit_mb = total - 8 GiB)
                                 and print the sudo commands that install it; verify an installed one
  localmodels peer copy-model <model> <user@peer> [--dry]   replicate an LM Studio catalog model to a peer over SSH
                                 (definition, weights, model-data.json records, default load config); LM Studio closed there
  localmodels peer test [name]   force a poll from the gateway and probe from this terminal (separates macOS Local
                                 Network blocking of the gateway from a link failure); print the route
  localmodels ask "<question>" [--dir FOLDER] [--no-files] [--no-think] [--schema FILE] [--max-turns N]
                                 (--no-think: no thinking: several times faster, for lookups and extraction)
  localmodels batch <jobs.json|requests.jsonl> [--dir FOLDER] [--parallel N] [--model ID] [--peers NAME,NAME]
                                 submit a durable batch; default: this Mac only. Returns its project-scoped ID
  localmodels jobs [ID [--results | --retry-failed]]   recent jobs, one job's status, JSONL results, or retry only errors
  localmodels prune [--days 30]  remove old private job folders and logs; keep running jobs and unfinished durable batches
  localmodels check              self-test: sandbox suites, then live checks against the installed gateway
  localmodels install [--wait MINUTES] [--force] [--plugin] [--dry] [--no-claude]
                                 set the hub up on this Mac; run it again after changing the hub's code: it builds a
                                 release (releases/<digest>) and hands off to a new gateway worker, nothing refused or
                                 cut. A gateway from before releases is migrated once, at a moment with no request in
                                 flight (1.5 minutes unless --wait says longer). --force: a release marked bad.
                                 --plugin: installed as a Claude Code plugin, which brings the tools. --dry: the path it
                                 would take, nothing written. --no-claude: no MCP registration, no signpost line
  localmodels uninstall          undo install
  localmodels start               the gateway (a no-op when it runs)
  localmodels restart [--cold]   a handoff to a new worker of the same release (--cold: waits for no request in flight,
                                 then stops and starts the supervisor: a new run, so load_at_start applies)
  localmodels rollback           a handoff back to the last good release
  localmodels stop [--now]       the supervisor and every worker, once no request is in flight (--now: at once)
  localmodels gateway            the supervisor, in the foreground (what launchd runs); --check validates a release's
  localmodels mcp                the MCP server (Claude Code starts it)
  localmodels commit [--version X.Y.Z] [--dry]   after committing the changes, stamp and tag the next patch locally
                                 (--version selects a version; --dry shows it). No remote is contacted
  localmodels push [--dry] [--remote URL]   check and push the tagged release at HEAD to origin, without a version bump
  localmodels publish            same as push; --version X.Y.Z verifies the version already committed
  localmodels prepush            git's pre-push hook (install writes it): the privacy and secrets checks on every push,
                                 and no rewrite or deletion of a remote branch (LOCALMODELS_ALLOW_REWRITE=1 on purpose)
  localmodels help | -h | --help anywhere: this text. An option a verb does not take, or a value it cannot use, runs
                                 nothing (exit 2); --name=value is read as --name value, --dry-run as --dry
"""
import contextlib, errno, glob, json, os, plistlib, re, shlex, shutil, subprocess, sys, time, urllib.request

from . import HOME, NAME, VERSION, llmshare

LABEL = os.environ.get('LOCALMODELS_LABEL') or 'com.local-models.hub'
OLD_LABELS = ()                                          # earlier LaunchAgent labels an install retires
PLIST = os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')
INSTALLED = None                                         # SHARED/hub (_state_paths)


@llmshare.on_state
def _state_paths(root):
    global INSTALLED, PRIVATE_WORDS
    INSTALLED = os.path.join(root, 'hub')
    PRIVATE_WORDS = os.environ.get('LOCALMODELS_PRIVATE_WORDS') or os.path.join(root, 'private-words.txt')


LOGS = os.path.expanduser('~/Library/Logs/LocalModels')
PY = shutil.which('python3') or sys.executable or '/usr/bin/python3'   # the PATH's python3 (a stable link, e.g. /opt/homebrew/bin/python3), not a versioned Cellar path
BINS = ('/opt/homebrew/bin', '/usr/local/bin', os.path.expanduser('~/.local/bin'))
SIGNPOST_FILE = os.path.expanduser('~/.claude/CLAUDE.md')
_HOME_SHOWN = '~' + HOME[len(os.path.expanduser('~')):] if HOME.startswith(os.path.expanduser('~') + os.sep) else HOME
SIGNPOST = (f"Local models on this Mac (LM Studio) are shared through the local-models hub: its rules live in {_HOME_SHOWN}/GUIDE.md "
            f"(or the local-models tools' guide). Send requests to {llmshare.HUB}/v1, never directly to LM Studio's port 1234. "
            "<!-- local-models hub signpost -->")
TESTS = ('test_crashes.py', 'test_rules.py', 'test_gateway.py', 'test_agent.py', 'test_batches.py', 'test_peers.py', 'test_extensions.py', 'test_code.py', 'test_handoff.py', 'test_supervisor.py')


def ago(t): return time.strftime('%H:%M', time.localtime(t))


def ago_left(s):
    """A stretch of time in the unit that reads best: 40 s, 12 min, 2 h 15 min, 3 d 4 h."""
    s = max(0, int(round(s)))
    if s < 90: return f'{s} s'
    if s < 5400: return f'{round(s / 60)} min'
    if s < 172800: s = round(s / 60) * 60; h, m = s // 3600, s % 3600 // 60; return f'{h} h' + (f' {m} min' if m else '')   # rounded first: never "1 h 60 min"
    s = round(s / 3600) * 3600; d, h = s // 86400, s % 86400 // 3600; return f'{d} d' + (f' {h} h' if h else '')


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
    if p.get('left') is None and p.get('at_least'): when = f"at least {ago_left(p['at_least'])} left, still estimating"
    return f" · {word}{' ' + p['name'] if p.get('name') else ''}: {p['done']} of {p['total']}, {when}" + phase_words(p)


BASIS_WORDS = {'this run earlier': 'from this run earlier', 'an earlier run': 'from an earlier run', "this run's other phases": "from this run's other phases"}


def phase_words(p):
    """A run in phases, after its whole: '; phase C 19 of 50 (31 min), then D 40 (about 1 h 20 min, from an earlier run), E 10 (...)'."""
    ps = [dict(x, name=x.get('name') or f'#{i + 1}') for i, x in enumerate(p.get('phases') or [])]   # unnamed: a private project's, numbered
    todo = [x for x in ps if x.get('state') != 'done']
    if not todo: return ''
    def one(x, first):
        size = f"{x['done']} of {x['total']}" if x['state'] == 'running' and x.get('total') else f"{'~' if x.get('approx') else ''}{x['total']}" if x.get('total') is not None else 'size not known'
        est = 'not estimated yet' if x.get('left') is None else ago_left(x['left']) if x.get('basis') == 'measured' else f"about {ago_left(x['left'])}, {BASIS_WORDS.get(x.get('basis'), 'estimated')}"
        return f"{'phase ' if first else ''}{x['name']} {size} ({est})"
    done = [x['name'] for x in ps if x.get('state') == 'done']
    return '; ' + (f"{', '.join(done)} done; " if done else '') + one(todo[0], True) + (', then ' + ', '.join(one(x, False) for x in todo[1:]) if todo[1:] else '')


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
    from . import batches, dash, recycle
    d, lm, users, cs = dash.door(), dash.lmstudio(), dash.users(), dash.claude()
    from . import maint
    mt = maint.view()
    if mt: print('⚠ ' + mt['words'][0].upper() + mt['words'][1:] + ('' if mt.get('off') else ' (localmodels maintenance off when done)' if mt['phase'] == 'on' else ''))
    print(f"gateway: {'listening on ' + llmshare.HUB if d['up'] else 'down (localmodels start)'}"
          + (' · stale code: localmodels install' if d['up'] and d['stale'] else ''))
    gw = dash.gateway_words()
    if gw: print('  ' + gw[len('gateway: '):])                          # the workers: serving, and draining with what they finish
    print(f"LM Studio: server {'up' if lm['server'] == 'on' else 'down'} · " + ('; '.join(dash.family_words(f) for f in lm['families']) or 'no model loaded')
          + ('' if llmshare.read_ok() else ' · (lms ps failed: showing the previous listing)'))
    if not (mt and mt.get('off')):
        for w in dash.embedding_words(lm['models']): print('  ' + w)
    if mt and mt.get('off'):
        n = llmshare.at_once()
        print(f"  requests: sent to peers only, {n} slot{'s' if n != 1 else ''} free for this gateway (at_once)" if mt.get('peers') else
              '  requests: refused (503 with Retry-After) until switched on; at_once 0. To keep work going on the peers: localmodels off --peers')
    try:
        from . import crashes, watchdog
        for s in crashes.stalled(): print('  ' + s['words'])
        w = watchdog.view()
        if w and lm['families']: print('  ' + w)
    except Exception as e: print(f'  (could not read the watchdog: {e})')   # not recorded: shown to the person running the command
    if llmshare.settings_error(): print(f'  SETTINGS NOT READ: {llmshare.settings_error()}')
    if d.get('write_problem'): print(f"  RECORDS NOT WRITTEN: {d['write_problem']}")
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
              + (track_words('run', b) if b.get('phases') else (track_words('run', b) if b.get('total') else '') + track_words('phase', r.get('job'))) + (f" · {r['step']}" if r.get('step') and not b.get('total') and not r.get('job') else '') + who)
    for b in batches.overview():
        print(f"batch: {b['project']} · {b['state']} · {b['done']} of {b['total']} done, {b['failed']} failed" + (f" · {b['reason']}" if b['reason'] else ''))
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
        d = json.load(llmshare.urlopen(req, timeout=timeout))
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
    """localmodels peer [add <name> <address> | remove <name> | serve <address> | serve off | off <name> | on <name> | token | test [name] | gpu-limit | copy-model ...]."""
    from . import peers
    w = list(a)                                          # main() has taken the verb: a peer may be named "peer"
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
    if verb in ('off', 'on'):                            # a peer's local models off or on, as `localmodels off` / `on` there
        names = [x for x in w[1:] if not x.startswith('--')]
        if not names: print(f'localmodels peer {verb} <name>' + (' [--force] [reason ...]' if verb == 'off' else '')); return 2
        name, why = names[0], ' '.join(names[1:])
        code, j = peers.switch(name, verb == 'on', '--force' in a, why)
        if code == 409 and j.get('blockers'): print(f"peer {name}: nothing switched off: in use: {'; '.join(j['blockers'])} (--force to switch off anyway)"); return 3
        if code >= 300: print(f"peer {name}: nothing switched {verb}: {j.get('error') or f'HTTP {code}'}"); return 3 if code == 409 else 1
        if llmshare.hub_up(fresh=True):                  # the gateway looks at the peer now, so routing follows at once
            try: urllib.request.urlopen(urllib.request.Request(llmshare.HUB + '/hub/link', data=b'{}', headers={'Content-Type': 'application/json'}), timeout=10).read()
            except OSError: pass
        print(f"peer {name}: {j.get('note') or 'done'}" + ('' if verb == 'on' else f'. This gateway routes nothing there until: localmodels peer on {name}'))
        return 0
    if verb == 'gpu-limit' and len(w) == 1: return gpu_limit_cmd()
    if verb == 'copy-model': return copy_model_cmd(w, '--dry' in a)
    if verb == 'log':                                    # each change of a peer's state, with its reason
        hours = num(w, '--hours', 24.0)
        if hours is None: return 2
        evs = peers.link_events(hours)
        if not evs: print(f'no change of a peer state in the last {hours:g} h ({peers.EVENTS})'); return 0
        for e in evs:
            print(f"{time.strftime('%d %b %H:%M:%S', time.localtime(e['at']))}  {e.get('peer')}: {e.get('was')} -> {e.get('state')}" + (f"  ({e['why']})" if e.get('why') else ''))
        return 0
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
                code, _ = peers.fetch(p['url'], '/hub/health', {'X-Hub-Token': tok or ''})
                here = 'connected, token accepted' if code == 200 else f"connected, HTTP {code}" + (' (peer-token mismatch)' if code in (401, 403) else '')
            except OSError as e: here = peers.reach_words(e) if e.errno != errno.ENETUNREACH else str(e.strerror or e)
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
    print('localmodels peer [add <name> <address> | remove <name> | serve <address> | serve off | off <name> | on <name> | token | test [name] | gpu-limit | copy-model <model> <user@peer>]')
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


def jobs(n=8, args=()):
    from . import batches, mcp_server
    if args:
        ids = [x for x in args if not x.startswith('-')]
        if len(ids) != 1 or '--results' in args and '--retry-failed' in args: print('localmodels jobs ID [--results | --retry-failed]'); return 2
        scope = mcp_server._session_folder()
        d = batches.locate(mcp_server.AGENTS, ids[0], scope)
        if d:
            try:
                if '--results' in args:
                    for row in batches.results(d, finished_only=True): print(json.dumps(row))
                elif '--retry-failed' in args: print(f'{batches.retry(d)} failed items queued for retry')
                else: print(json.dumps(batches.status(d), indent=2))
                return 0
            except ValueError as e: print(e, file=sys.stderr); return 1
        st = mcp_server.job_status(ids[0], results='--results' in args, retry_failed='--retry-failed' in args, within=scope)
        print(json.dumps(st, indent=2)); return 0 if st['ok'] else 1
    ds = sorted(glob.glob(os.path.join(mcp_server.AGENTS, '*', '*')), key=os.path.getmtime)[-n:]
    if not ds: print('no local-agent jobs started through the gateway yet'); return 0
    for d in ds:
        job = os.path.relpath(d, mcp_server.AGENTS)
        durable = batches.locate(mcp_server.AGENTS, job)
        st = batches.status(durable) if durable else mcp_server.job_status(job)
        state = st.get('state') or ('finished' if st.get('finished') else 'running')
        why = st.get('reason')
        if st.get('problem'): state, why = 'stopped', 'the runner did not finish'
        print(f"  {job}: {st.get('done')} of {st.get('of')} done · {state}" + (f' · {why[:100]}' if why else ''))
    return 0


def prune(days=30, root=None):
    """Remove local-agent job folders and loose job logs older than `days` under ~/Library/Logs/LocalModels/agents (the
    one place the hub keeps prompts and answers, as each project's own record). A folder whose runner is alive, or with
    a job still running, or a durable batch still unfinished, is kept. -> (folders removed, files removed, bytes freed)."""
    from . import batches, mcp_server
    root = os.path.realpath(root or mcp_server.AGENTS)
    cutoff, folders, files, size = time.time() - days * 86400, 0, 0, 0
    for p in glob.glob(os.path.join(root, '*', '*')) + glob.glob(os.path.join(root, '*.json')):   # a run_job log can sit at the root
        try: m = os.path.getmtime(p)
        except OSError: continue
        if m >= cutoff or os.path.realpath(p) != p: continue
        if os.path.isdir(p):
            job = os.path.relpath(p, root)
            try:
                # Submission holds this same lock before publishing the database. Retry and prune also exclude each other.
                with batches.lock(p) if batches.JOB_ID.fullmatch(job) else contextlib.nullcontext(True) as acquired:
                    if not acquired: continue
                    if batches.locate(root, job):
                        if not batches.status(p)['finished']: continue
                        batches.dequeue(p)
                    else:
                        try:
                            with open(os.path.join(p, 'runner.json')) as f: pid = json.load(f).get('pid')
                        except (OSError, ValueError): pid = None
                        if (pid and llmshare._alive(pid) and not os.path.exists(os.path.join(p, 'summary.json'))) or glob.glob(os.path.join(p, '*.running')): continue
                    for d, _, fs in os.walk(p):
                        for f in fs:
                            try: size += os.path.getsize(os.path.join(d, f))
                            except OSError: pass
                    shutil.rmtree(p); folders += 1
            except FileNotFoundError: continue                 # another prune got this directory first
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
    from . import batches, mcp_server
    try:
        with open(a['jobs'], 'rb') as f: spec = batches.decode(f.read(), a['jobs'].lower().endswith(('.jsonl', '.ndjson')))
        allowed = a['peers'].split(',') if a.get('peers') else spec.get('peers')
        parallel = a.get('parallel') if a.get('parallel') is not None else spec.get('parallel')
        job, d = mcp_server.start('batch', spec['jobs'], parallel, a.get('dir') or spec.get('folder'), spec.get('read_files', True),
                                  within=None if a.get('dir') else mcp_server._session_folder(), run=spec.get('run'), phase=spec.get('phase'),
                                  model=a.get('model') or spec.get('model'), peers=allowed)
    except (ValueError, TypeError, OSError) as e: print(e); return 1
    print(f"submitted {len(spec['jobs'])} items as {job}; status: localmodels jobs {job}; results in {d}")
    return 0


# ---------------------------------------------------------------- setting the hub up
def digest(folder):
    from . import dash
    return dash.digest(folder)


def _launchctl(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)


def _loaded(label=LABEL):
    return _launchctl('print', f'gui/{os.getuid()}/{label}').returncode == 0


def quiet(wait=90, still=3, slots=False):
    """Wait for a moment with no request passing through the door (for `still` seconds in a row), so a restart cuts
    nobody off; slots: no slot held by any process either (the migration). -> whether it came within `wait` seconds."""
    t0, calm = time.time(), None
    while True:
        if llmshare.inflight() or (slots and llmshare.busy_slots()): calm = None
        else:
            calm = calm or time.time()
            if time.time() - calm >= still: return True
        if time.time() - t0 > wait: return False
        time.sleep(0.5)


def _plist():
    env = dict(PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin')
    for k in ('LOCALMODELS_PORT', 'LMSTUDIO_URL', 'LMSTUDIO_LOGS', 'LOCALMODELS_STATE', 'LMS'):
        if os.environ.get(k): env[k] = os.environ[k]
    env['LOCALMODELS_PY'] = PY                               # the stable interpreter path a worker is started with
    return dict(Label=LABEL, ProgramArguments=[PY, os.path.join(INSTALLED, 'bin', 'localmodels'), 'gateway'], RunAtLoad=True,
                KeepAlive=True, ThrottleInterval=10, AbandonProcessGroup=True, StandardOutPath=os.path.join(LOGS, 'gateway.log'),
                StandardErrorPath=os.path.join(LOGS, 'gateway.log'), EnvironmentVariables=env, SoftResourceLimits=dict(NumberOfFiles=8192))


SETTINGS_FILE = os.path.expanduser('~/.claude/settings.json')
HOOK_EVENTS = ('SessionStart', 'UserPromptSubmit')


def hook_command():
    """The command Claude Code runs before each prompt: the installed copy's `localmodels notice` (silent unless
    maintenance). It cannot fail: with the installed copy gone, python's exit 2 would make Claude Code refuse every
    prompt in every session, the one that would repair it included."""
    script = os.path.join(INSTALLED, 'bin', 'localmodels')
    return f'[ -f "{script}" ] && "{PY}" "{script}" notice || true'


def _claude(*args, timeout=60):
    """One `claude` command, bounded in time. -> the finished process, or None when it could not run in time."""
    claude = shutil.which('claude')
    if not claude: return None
    try: return subprocess.run([claude, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError): return None


def with_hook(settings, cmd, add=True):
    """Claude Code's settings with the hub's hook added for SessionStart and UserPromptSubmit (once), or taken out; the
    person's own hooks and settings are left as they are."""
    hooks = settings.setdefault('hooks', {})
    from shlex import split
    def hub_command(h):
        if not isinstance(h, dict) or h.get('type') != 'command': return False
        text = h.get('command')
        if not isinstance(text, str): return False
        if text == cmd: return True
        try: args = split(text)
        except ValueError: return False
        if len(args) == 10 and args[:2] == ['[', '-f'] and args[2] == args[6] and args[3:5] == [']', '&&'] and args[-2:] == ['||', 'true']:
            args = args[5:8]
        return (len(args) == 3 and re.fullmatch(r'python3(?:\.\d+)?', os.path.basename(args[0])) is not None
                and args[1] == os.path.join(INSTALLED, 'bin', 'localmodels') and args[2] == 'notice')
    for ev in HOOK_EVENTS:
        kept = []
        for group in hooks.get(ev, []):
            actions = group.get('hooks', [])
            left = [h for h in actions if not hub_command(h)]
            if left == actions: kept.append(group)
            elif left: kept.append(dict(group, hooks=left))
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
    words = []
    for key, f in s['families'].items():
        if f.get('kind') == 'embedding': layout = 'embedding model'
        else: layout = f"{f['copies']} replica{'' if f['copies'] == 1 else 's'} × {f['parallel']} slots"
        words.append(f'{key}: {layout}' + (f", context {f['context']:,}" if f.get('context') else ''))
    return '; '.join(words)


def sandboxed():
    """Words when this shell points the hub at another state folder (LOCALMODELS_STATE) without a LaunchAgent label of
    its own (LOCALMODELS_LABEL): an install, uninstall, start, stop or restart from it would act on this Mac's real
    LaunchAgent and Claude Code settings with that folder written into them. None otherwise."""
    if os.environ.get('LOCALMODELS_STATE') and not os.environ.get('LOCALMODELS_LABEL'):
        return (f"LOCALMODELS_STATE is set ({llmshare.SHARED}) and LOCALMODELS_LABEL is not: this would act on the LaunchAgent {LABEL} "
                'and the Claude Code settings with that state folder. Set LOCALMODELS_LABEL too, or unset LOCALMODELS_STATE')
    return None


def install(wait=90, plugin=False, dry=False, no_claude=False, force=False):
    uid = f'gui/{os.getuid()}'
    if not dry and sandboxed(): print(f'nothing installed: {sandboxed()}'); return 2
    if dry:
        if os.path.exists(llmshare.MODELS): print(f'would keep the settings: {llmshare.MODELS}')
        else: print(f'would write the settings file {llmshare.MODELS}, fitted to this Mac: {_fitted_words(_fitted())}')
        from . import deploy
        code, words = deploy.install_gateway(sys.modules[__name__], wait=wait, dry=True, say=lambda w: print('would build ' + w))
        print(f"{words}; the gateway runs at login as {LABEL} ({PLIST}) with {PY}, from {INSTALLED} -> releases/<digest>")
        if plugin: print("would leave the hub's MCP tools and the rules to the Claude Code plugin (no registration, no signpost line)")
        elif no_claude: print('would register nothing with Claude Code and add no signpost line (--no-claude)')
        else: print(f"would register the hub's MCP tools for every Claude Code session (claude mcp add --scope user local-models), add one signpost line to {SIGNPOST_FILE}, "
                    f"and add the maintenance hook to {SETTINGS_FILE} (silent unless maintenance is set)")
        print(f"would link the localmodels command into the first writable of {', '.join(BINS)}")
        if retire_loader(dry=True): print(retire_loader(dry=True))
        print(f"would write the pre-push hook in {os.path.join(HOME, '.git', 'hooks')} (the privacy and secrets checks on every push)")
        print('nothing written (--dry)')
        return code
    os.makedirs(LOGS, exist_ok=True)
    if not os.path.exists(llmshare.MODELS):
        os.makedirs(llmshare.SHARED, exist_ok=True); fitted = _fitted()
        with open(llmshare.MODELS, 'w') as f: json.dump(fitted, f, indent=1); f.write('\n')
        print(f'0. the settings file {llmshare.MODELS} written, fitted to this Mac: {_fitted_words(fitted)} (edit it; the gateway reloads it on change)')
    else: print(f'0. the settings: {llmshare.MODELS}')
    for o in OLD_LABELS:
        if _loaded(o):
            _launchctl('bootout', f'{uid}/{o}')
            try: os.remove(os.path.expanduser(f'~/Library/LaunchAgents/{o}.plist'))
            except OSError: pass
            print(f'   (the gateway under its old label {o} is stopped; it restarts as {LABEL})')
    from . import deploy
    plist = _plist()
    try: was = plistlib.load(open(PLIST, 'rb'))
    except (OSError, ValueError): was = {}
    env, before = plist.get('EnvironmentVariables') or {}, was.get('EnvironmentVariables') or {}
    shown = {k: v for k, v in env.items() if k not in ('PATH', 'LOCALMODELS_PY')}
    print('   the LaunchAgent environment: ' + (', '.join(f'{k}={v}' for k, v in shown.items()) if shown else 'the defaults (PATH only)'))
    for k in sorted(set(before) - set(env)): print(f'   note: {k}={before[k]} was in the LaunchAgent and is not any more (it is not set in this shell)')
    code, words = deploy.install_gateway(sys.modules[__name__], wait=wait, force=force, say=lambda w: print('   ' + w))
    up = code == 0
    print(f"1. {words}")
    if code == 3: return 3
    print(f"2. gateway starts at login ({PLIST}): {'listening on ' + llmshare.HUB if up else 'NOT serving the new release: see ' + os.path.join(LOGS, 'gateway.log')}")
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
            cur = _claude('mcp', 'get', 'local-models')
            if cur and cur.returncode == 0 and all(x in cur.stdout for x in want) and 'User' in cur.stdout:
                print('3. the hub\'s MCP tools are already registered for every Claude Code session (local-models)')
            else:
                _claude('mcp', 'remove', '--scope', 'user', 'local-models')
                r = _claude('mcp', 'add', '--scope', 'user', 'local-models', '--', *want)
                print('3. the hub\'s MCP tools registered for every Claude Code session (local-models)' if r and r.returncode == 0 else
                      f"3. could not register the tools: {((r.stderr or r.stdout) if r else 'claude did not answer within 60 s').strip()[:300]}")
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
    print('   ' + set_prepush_hook())
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
    if sandboxed(): print(f'nothing uninstalled: {sandboxed()}'); return 2
    print('0. ' + set_hook(False))                         # first: a hook left pointing at removed code would refuse every prompt
    uid = f'gui/{os.getuid()}'
    for label in (LABEL,) + OLD_LABELS:
        if _loaded(label): _launchctl('bootout', f'{uid}/{label}')
        try: os.remove(os.path.expanduser(f'~/Library/LaunchAgents/{label}.plist'))
        except OSError: pass
    n = sweep_workers()                                    # adopted workers are not launchd's children
    if os.path.islink(INSTALLED): os.remove(INSTALLED)
    else: shutil.rmtree(INSTALLED, ignore_errors=True)
    for d in ('releases', 'run', 'peer-slots'): shutil.rmtree(os.path.join(llmshare.SHARED, d), ignore_errors=True)
    try: os.remove(llmshare.LISTING)
    except OSError: pass
    print('1. gateway stopped; it no longer starts at login' + (f' ({n} workers ended)' if n else '') + '; its releases and run state removed')
    _claude('mcp', 'remove', '--scope', 'user', 'local-models')
    print('2. the hub\'s MCP tools are no longer registered')
    if os.path.exists(SIGNPOST_FILE):
        lines = [l for l in open(SIGNPOST_FILE).read().split('\n') if 'local-models hub signpost' not in l]
        tmp = SIGNPOST_FILE + '.tmp'; open(tmp, 'w').write('\n'.join(lines)); os.replace(tmp, SIGNPOST_FILE)
    print(f'3. the signpost line is out of {SIGNPOST_FILE}')
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


UNITS = dict(s=1 / 60, sec=1 / 60, secs=1 / 60, second=1 / 60, seconds=1 / 60, m=1, min=1, mins=1, minute=1, minutes=1,
             h=60, hr=60, hrs=60, hour=60, hours=60)
WHEN = ('in', 'at', 'for', 'now', 'after', 'after-batches')
MAINT_USAGE = 'localmodels maintenance in 15m | at 20:30 | now | after-batches [for 30m] [reason ...]'


def _duration(words, j):
    """A duration at words[j]: '30m', '1h30m', '1.5h', '90' (minutes), or a number and a unit word ('2 hours').
    -> (minutes, words used), or (None, 0); a bare number followed by a word that is neither a unit nor a time word
    ('2 fortnights') is not read as minutes."""
    if j >= len(words): return None, 0
    w, nxt = words[j].lower(), (words[j + 1].lower() if j + 1 < len(words) else None)
    m = re.fullmatch(r'(\d+(?:\.\d+)?)([a-z]*)', w)
    if m and m.group(2) in UNITS: return max(1, round(float(m.group(1)) * UNITS[m.group(2)])), 1
    if m and not m.group(2):
        if nxt in UNITS: return max(1, round(float(m.group(1)) * UNITS[nxt])), 2
        if nxt is not None and nxt not in WHEN: return None, 0
        return max(1, round(float(m.group(1)))), 1
    mins = _minutes(w)
    return (mins, 1) if mins else (None, 0)


def maintenance_args(words, now=None):
    """The maintenance command's words: the time words first (in, at, now, after-batches, for), then the reason, taken
    verbatim from its first word on, so a reason's own words ("update now available") are never read as times.
    -> (start, after_batches, minutes, reason); ValueError with the usage when a time word's value cannot be read or no
    start is given."""
    now = now or time.time()
    start, after, minutes, i = None, False, 30, 0
    while i < len(words):
        w = words[i].lower()
        if w in ('in', 'for'):
            d, n = _duration(words, i + 1)
            if d is None: raise ValueError(f'{w} needs a duration (15m, 2h, 1.5h, 90 min): {MAINT_USAGE}')
            if w == 'in': start = now + 60 * d
            else: minutes = d
            i += 1 + n; continue
        if w == 'at':
            at = _at(words[i + 1]) if i + 1 < len(words) else None
            if at is None: raise ValueError(f'at needs a time (20:30): {MAINT_USAGE}')
            start, i = at, i + 2; continue
        if w == 'now': start, i = now, i + 1; continue
        if w in ('after-batches', 'after'): after, i = True, i + 1; continue
        break
    if start is None and not after: raise ValueError(f'say when: {MAINT_USAGE}')
    return start, after, minutes, ' '.join(words[i:])


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
    """localmodels maintenance [in 15m | at 20:30 | now | after-batches] [for 30m] [reason ...] | off."""
    from . import dash, maint, models
    words = list(a)
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
    try: start, after, minutes, why = maintenance_args(words)
    except ValueError as e: print(f'nothing set: {e}'); return 2
    m = maint.announce(start=start, minutes=minutes, why=why, after_batches=after)
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


# ---------------------------------------------------------------- releases and the supervisor
def build_release(root=None, shared=None):
    """releases/<D>: an immutable copy of the files a gateway runs (dash.deploy_files) and its RELEASE.json {digest,
    version, state_protocol, channel, supervisor, built}, built in releases/.tmp-<D>-<pid>/ and renamed into place; an
    existing D is reused. -> its folder."""
    from . import dash, supervisor
    root, shared = root or HOME, shared or llmshare.SHARED
    d = dash.deploy_digest(root)
    dest = os.path.join(shared, 'releases', d)
    if os.path.isfile(os.path.join(dest, 'RELEASE.json')): return dest
    tmp = os.path.join(shared, 'releases', f'.tmp-{d}-{os.getpid()}')
    shutil.rmtree(tmp, ignore_errors=True)
    for rel in dash.deploy_files(root):
        os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
        shutil.copy2(os.path.join(root, rel), os.path.join(tmp, rel))
    if dash.deploy_digest(tmp) != d:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f'the copy differs from {root} (a file changed while it was copied): run install again')
    def stated(rel, name, default):                          # the release's own value, read from its files (not this process's)
        m = re.search(rf'^{name}\s*=\s*(\d+)', open(os.path.join(tmp, rel)).read(), re.M) if os.path.exists(os.path.join(tmp, rel)) else None
        return int(m.group(1)) if m else default
    version = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)", open(os.path.join(tmp, 'localmodels', '__init__.py')).read(), re.M)
    with open(os.path.join(tmp, 'RELEASE.json'), 'w') as f:
        json.dump(dict(digest=d, version=version.group(1) if version else VERSION, state_protocol=stated('localmodels/llmshare.py', 'STATE_PROTOCOL', llmshare.STATE_PROTOCOL),
                       channel=stated('localmodels/supervisor.py', 'CHANNEL', supervisor.CHANNEL), supervisor=supervisor.supervisor_digest(tmp),
                       built=round(time.time(), 1)), f)
    try: os.rename(tmp, dest)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        if not os.path.isfile(os.path.join(dest, 'RELEASE.json')): raise          # else another install built it meanwhile
    return dest


def supervisor_call(op, timeout=300, say=None, **fields):
    """One operation on the running supervisor's control socket (status, deploy, restart, rollback, upgrade). Its progress
    lines go to say(words). -> its final reply {done, ok, ...}, or None when no supervisor answers."""
    import socket
    path = (llmshare.supervisor() or {}).get('sock')
    if not path: return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall((json.dumps(dict(op=op, **fields)) + '\n').encode())
        buf = b''
        while True:
            x = s.recv(65536)
            if not x: return None
            buf += x
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                try: m = json.loads(line)
                except ValueError: continue
                if m.get('done'): return m
                if say and m.get('progress'): say(m['progress'])
    except OSError: return None
    finally: s.close()


def sweep_workers(grace=5.0):
    """After a bootout: SIGTERM every registered gateway whose lock is still held (an adopted worker is not launchd's
    child), SIGKILL what is left after `grace` seconds. -> how many were signalled."""
    import signal as _signal
    pids = [r['pid'] for r in llmshare.live_gateways() if r['pid'] != os.getpid()]
    for pid in pids:
        try: os.kill(pid, _signal.SIGTERM)
        except OSError: pass
    end = time.time() + grace
    while time.time() < end and [r for r in llmshare.live_gateways() if r['pid'] in pids]:
        time.sleep(0.2); llmshare._live.clear(); llmshare._gw['at'] = 0.0
    for r in llmshare.live_gateways():
        if r['pid'] in pids:
            try: os.kill(r['pid'], _signal.SIGKILL)
            except OSError: pass
    return len(pids)


def off_cmd(a):
    """localmodels off [--peers] [--force] [reason ...]: switch off (to peers only), then wait (up to 10 min) for the
    instance manager's unload."""
    from . import maint, models
    code, body = models.switch_off('--force' in a, ' '.join(x for x in a if not x.startswith('--')), peers_only='--peers' in a)
    if code != 200: print(f"nothing switched off: {body.get('error')}"); return 3
    print(maint.words(maint.read()))
    if body.get('note') and ('peer' in body['note'] or body['note'].startswith('requests now')): print('  ' + body['note'])
    end, last = time.time() + 600, None
    while time.time() < end:
        off = maint.switched_off()
        if not off: print('switched on again meanwhile'); return 0
        if off.get('unloaded'): break
        did = models.off_pass(off)                           # the gateway's instance manager does the same; the swap lock keeps one at a time
        if did and did != last: print('  ' + '; '.join(did)); last = did
        if (maint.switched_off() or {}).get('unloaded'): break
        time.sleep(2)
    off = maint.switched_off() or {}
    rows = llmshare.loaded(fresh=True, force=True)
    print('switched off: ' + (f"still listed: {', '.join(r['id'] for r in rows)}" if rows else 'no instance loaded')
          + ('' if off.get('unloaded') else ' (the unload is still pending: the instance manager keeps trying)') + '. Turn on: localmodels on')
    return 0


def on_cmd():
    from . import models
    for line in models.switch_on(): print(line)
    rows = llmshare.loaded(fresh=True, force=True)
    print('loaded now: ' + (', '.join(r['id'] for r in rows) or 'nothing') + ' (the instance manager adds the other replicas within 20 s)')
    return 0


def door(verb, a=()):
    uid = f'gui/{os.getuid()}'
    if sandboxed(): print(f'nothing done: {sandboxed()}'); return 2
    if verb == 'start' and _loaded() and llmshare.hub_up(fresh=True): print(f'gateway already listening on {llmshare.HUB}'); return 0
    sup = llmshare.supervisor()
    if verb in ('restart', 'rollback') and sup and '--cold' not in a:   # a handoff: no quiet gate, nothing refused or cut
        r = supervisor_call(verb, say=lambda w: print('  ' + w))
        print((r or {}).get('words') or (r or {}).get('why') or 'no answer from the supervisor')
        return 0 if r and r.get('ok') else 1
    if verb == 'rollback': print('rollback needs the supervisor (an install with releases)'); return 1
    if verb in ('stop', 'restart') and _loaded() and '--now' not in a and not quiet():
        print(f'gateway busy ({len(llmshare.inflight())} requests in flight for 90 s): left running; retry when idle, or --now')
        return 3
    if verb in ('stop', 'restart') and (_loaded() or sup):   # never under a load or a recycle half done (install does the same)
        with llmshare.swapping(wait=120) as got:
            if not got: print('a swap or a reload is under way: gateway left running; retry in a moment'); return 3
            if verb == 'stop' or sup:                        # restart --cold: a new run (load_at_start applies)
                r = _launchctl('bootout', f'{uid}/{LABEL}') if _loaded() else None
                n = sweep_workers()                          # an adopted worker is not launchd's child
                if verb == 'stop':
                    print(('gateway stopped until the next login (localmodels start)' if r is None or r.returncode == 0 else r.stderr.strip()) + (f'; {n} workers ended' if n else ''))
                    return r.returncode if r is not None else 0
            if not os.path.exists(PLIST): print('not installed: localmodels install'); return 1
            r = _launchctl('kickstart', '-k', f'{uid}/{LABEL}') if _loaded() else _launchctl('bootstrap', uid, PLIST)
    elif verb == 'stop': print('gateway not running'); return 0
    elif not os.path.exists(PLIST): print('not installed: localmodels install'); return 1
    elif verb == 'start': r = _launchctl('kickstart', f'{uid}/{LABEL}') if _loaded() else _launchctl('bootstrap', uid, PLIST)   # never -k: a running door is left alone
    else: r = _launchctl('bootstrap', uid, PLIST)
    up = any(llmshare.hub_up(fresh=True) or time.sleep(0.5) for _ in range(30))
    print(f"gateway {'listening on ' + llmshare.HUB if up else 'not responding: see ' + os.path.join(LOGS, 'gateway.log')}")
    return 0 if up else 1


# ---------------------------------------------------------------- the self-test
def run_tests(quiet=False):
    """The sandbox tests (no model touched): -> [(name, ok, last line or the failures)]."""
    out = []
    for name in TESTS:
        path = os.path.join(HOME, 'tests', name)
        if not os.path.exists(path):                          # a listed suite that is gone fails the gate; a copy with no tests/ has none to run
            if os.path.isdir(os.path.join(HOME, 'tests')): out.append((name, False, 'listed in cli.TESTS but missing from tests/'))
            continue
        try: p = subprocess.run([PY, path], capture_output=True, text=True, timeout=300)
        except subprocess.SubprocessError as e: out.append((name, False, str(e))); continue
        fails = [l.strip()[5:] for l in p.stdout.split('\n') if l.strip().startswith('FAIL')]
        last = (p.stdout.strip().split('\n') or [''])[-1]
        out.append((name, p.returncode == 0, last if not fails else '; '.join(fails)[:300] if p.returncode else last))
        if p.returncode and not fails: out[-1] = (name, False, (p.stderr.strip().split('\n') or ['?'])[-1][:300])
    old = '/usr/bin/python3'                                  # also check handoffs and SQLite batch recovery on the system Python
    if os.path.exists(old) and os.path.realpath(old) != os.path.realpath(PY):
        try: ok39 = subprocess.run([old, '-c', 'import sys; sys.exit(sys.version_info < (3, 9))'], capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError): ok39 = False
        for name in (n for n in TESTS if n in ('test_batches.py', 'test_handoff.py', 'test_supervisor.py')) if ok39 else ():
            path = os.path.join(HOME, 'tests', name)
            if not os.path.exists(path): continue
            try: p = subprocess.run([old, path], capture_output=True, text=True, timeout=300)
            except subprocess.SubprocessError as e: out.append((name[:-3] + ' under 3.9.py', False, str(e))); continue
            fails = [l.strip()[5:] for l in p.stdout.split('\n') if l.strip().startswith('FAIL')]
            last = (p.stdout.strip().split('\n') or [''])[-1]
            out.append((name[:-3] + ' under 3.9.py', p.returncode == 0, '; '.join(fails)[:300] if fails else last))
    if swiftc() and os.path.exists(os.path.join(HOME, 'app', 'tests', 'main.swift')): out.append(app_tests())
    return out


# ---------------------------------------------------------------- the menu-bar app (app/)
APP_ID = 'com.haedolabs.localmodels'
APP_BUNDLE = os.path.expanduser('~/Applications/Local Models Hub.app')
APP_EXE = 'LocalModelsHub'


def swiftc():
    """The Command Line Tools' Swift compiler, or None when they are not installed."""
    w = shutil.which('swiftc')
    if w: return w
    try: p = subprocess.run(['xcrun', '-f', 'swiftc'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError): return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def app_tests():
    """The app's state logic (app/Glance.swift) against its fixture snapshots, and its menu kept in place (app/Menu.swift)
    with real menu items. -> (name, ok, last line or the failures)."""
    import tempfile
    name = 'test_menubar.swift'
    with tempfile.TemporaryDirectory(prefix='localmodels-app-') as d:
        exe = os.path.join(d, 'tests')
        try:
            b = subprocess.run([swiftc(), '-swift-version', '5', *(os.path.join(HOME, 'app', f) for f in ('Glance.swift', 'Menu.swift', 'ExtensionIO.swift', 'tests/main.swift')), '-o', exe],
                               capture_output=True, text=True, timeout=300)
            if b.returncode: return (name, False, 'build failed: ' + (b.stderr.strip().split('\n') or ['?'])[0][:300])
            p = subprocess.run([exe], capture_output=True, text=True, timeout=60)
        except subprocess.SubprocessError as e: return (name, False, str(e))
    fails = [l.strip()[5:] for l in p.stdout.split('\n') if l.strip().startswith('FAIL')]
    return (name, p.returncode == 0, '; '.join(fails)[:300] if fails else (p.stdout.strip().split('\n') or [''])[-1])


def app_installed():
    """The installed app's version, or None when it is not installed."""
    try:
        with open(os.path.join(APP_BUNDLE, 'Contents', 'Info.plist'), 'rb') as f: return plistlib.load(f).get('CFBundleShortVersionString') or '?'
    except (OSError, ValueError, plistlib.InvalidFileException): return None


def app_quit():
    subprocess.run(['pkill', '-x', APP_EXE], capture_output=True)
    for _ in range(30):
        if subprocess.run(['pgrep', '-x', APP_EXE], capture_output=True).returncode: return
        time.sleep(0.2)


def app_cmd(a):
    """localmodels app install | uninstall: the menu-bar app, built on this Mac from app/ (no signing needed: a local
    build carries no quarantine attribute), in ~/Applications."""
    w = [x for x in a if not x.startswith('--')]
    verb = w[0] if w else ''
    if verb not in ('install', 'uninstall'):
        v = app_installed()
        print(f'menu-bar app: ' + (f'{v} installed at {APP_BUNDLE}' + (' (running)' if not subprocess.run(['pgrep', '-x', APP_EXE], capture_output=True).returncode else ' (not running)')
                                    if v else 'not installed') + '\n  localmodels app install | uninstall')
        return 0 if verb == '' else 2
    if sandboxed(): print(f'nothing done: {sandboxed()}'); return 2
    if verb == 'uninstall':
        if not app_installed(): print('menu-bar app: not installed'); return 0
        subprocess.run([os.path.join(APP_BUNDLE, 'Contents', 'MacOS', APP_EXE), '--unregister-login'], capture_output=True, timeout=30)
        app_quit(); shutil.rmtree(APP_BUNDLE, ignore_errors=True)
        print(f'menu-bar app removed ({APP_BUNDLE}), with its login item'); return 0
    sc = swiftc()
    if not sc: print('menu-bar app: needs the Command Line Tools (swiftc): xcode-select --install'); return 2
    name, good, note = app_tests()
    if not good: print(f'menu-bar app not installed: its tests fail: {note}'); return 1
    import tempfile
    os.makedirs(os.path.dirname(APP_BUNDLE), exist_ok=True)
    stage = tempfile.mkdtemp(prefix='.localmodels-app-', dir=os.path.dirname(APP_BUNDLE))
    try:
        app = os.path.join(stage, 'Local Models Hub.app'); macos, res = os.path.join(app, 'Contents', 'MacOS'), os.path.join(app, 'Contents', 'Resources')
        os.makedirs(macos); os.makedirs(res)
        b = subprocess.run([sc, '-O', '-swift-version', '5', '-target', f'{os.uname().machine}-apple-macos13.0',
                            *(os.path.join(HOME, 'app', f) for f in ('Glance.swift', 'Menu.swift', 'ExtensionIO.swift', 'main.swift')), '-o', os.path.join(macos, APP_EXE)],
                           capture_output=True, text=True, timeout=600)
        if b.returncode: print('menu-bar app: build failed:\n' + b.stderr.strip()[-1500:]); return 1
        icon = subprocess.run(['sips', '-s', 'format', 'icns', os.path.join(HOME, 'brand', 'logo-1024.png'), '--out', os.path.join(res, 'AppIcon.icns')],
                              capture_output=True, text=True, timeout=60)
        cli = os.path.realpath(sys.argv[0]) if os.path.basename(sys.argv[0]) == 'localmodels' else os.path.join(HOME, 'bin', 'localmodels')
        with open(os.path.join(app, 'Contents', 'Info.plist'), 'wb') as f:
            plistlib.dump(dict(CFBundleIdentifier=APP_ID, CFBundleName='Local Models Hub', CFBundleDisplayName='Local Models Hub', CFBundleExecutable=APP_EXE,
                               CFBundlePackageType='APPL', CFBundleShortVersionString=VERSION, CFBundleVersion=VERSION, LSMinimumSystemVersion='13.0',
                               LSUIElement=True, NSHighResolutionCapable=True, LMHubURL=llmshare.HUB, LMHubCLI=cli, LMHubState=llmshare.SHARED,
                               **({'CFBundleIconFile': 'AppIcon'} if icon.returncode == 0 else {})), f)
        s = subprocess.run(['codesign', '--force', '--sign', '-', app], capture_output=True, text=True, timeout=120)
        if s.returncode: print('menu-bar app: ad-hoc signing failed: ' + s.stderr.strip()[-300:]); return 1
        was = app_installed()
        app_quit()
        if os.path.exists(APP_BUNDLE): shutil.rmtree(APP_BUNDLE)
        os.rename(app, APP_BUNDLE)
    finally: shutil.rmtree(stage, ignore_errors=True)
    subprocess.run(['open', APP_BUNDLE], capture_output=True)
    print(f"menu-bar app {VERSION} {'replaced ' + was if was else 'installed'}: {APP_BUNDLE} (built here, signed ad hoc; its tests: {note})")
    print('  running in the menu bar; Launch at login is in its menu')
    return 0


def ext_cmd(a):
    """localmodels ext [check]: the extensions the menu-bar app draws, each one's menu answering or not; check reads every
    manifest and the menu it serves against the protocol (localmodels/extensions.py)."""
    from . import extensions as ex
    w = [x for x in a if not x.startswith('--')]
    verb = w[0] if w else ''
    if verb not in ('', 'check'): print('localmodels ext [check]'); return 2
    found = ex.load()
    if not found:
        print(f'no extensions ({ex.DIR}): a program joins the menu-bar app by writing a manifest there (GUIDE.md, "Extensions")'); return 0
    bad = 0
    for x in found:
        m = x['manifest'] if isinstance(x['manifest'], dict) else {}
        label = (m.get('name') or x['file']) + (f" — {m['title']}" if m.get('title') else '') + (f" {m['version']}" if m.get('version') else '')
        probs = list(x['problems'])
        menu, why = (ex.fetch_menu(m) if not probs else (None, 'manifest not sound'))
        if verb == 'check' and menu is not None: probs += [f'menu: {p}' for p in ex.validate_menu(menu, m)]
        state = 'answering' if menu is not None else f'not answering ({why})'
        print(f"  {label}: {state}" + (f" · {m.get('menu')}" if m.get('menu') else '') + (f" · {len(probs)} problem{'s' if len(probs) != 1 else ''}" if probs else ''))
        for p in probs[:12]: print(f'      {p}')
        bad += bool(probs) or (verb == 'check' and menu is None)
    if verb == 'check': print(f"extensions: {len(found)}, {bad} with a problem" if bad else f'extensions: {len(found)}, all sound')
    return 1 if (verb == 'check' and bad) else 0


def lmstudio_up():
    from . import models
    try: return models.server_up()
    except Exception: return False                         # not recorded: the check prints what it finds


def check():
    from . import crashes, dash, mcp_server, whois
    ok = []
    def t(name, good, note=''): ok.append(good); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))
    t('gateway responds', llmshare.hub_up(fresh=True), llmshare.HUB)
    try:
        page = llmshare.urlopen(llmshare.HUB + '/', timeout=5).read()
        snap = json.load(llmshare.urlopen(llmshare.HUB + '/hub/snapshot', timeout=30))
        t('the dashboard answers', NAME.encode() in page and b'__HUB_NAME__' not in page and 'lmstudio' in snap, f'{llmshare.HUB}/ (localmodels open)')
    except (OSError, ValueError) as e: t('the dashboard answers', False, str(e))
    from . import maint as _maint
    if _maint.switched_off() and not lmstudio_up():     # switched off: the gateway starts no LM Studio server, by design
        print("  skip LM Studio check (local models switched off: its server is not started until localmodels on)")
    else:
        try: models = json.load(llmshare.urlopen(llmshare.HUB + '/v1/models', timeout=5)).get('data', []); t('LM Studio reachable via the gateway', True, f'{len(models)} models listed')
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
        t(f'{os.path.splitext(name)[0][5:]}: the sandbox tests pass', good, note)
    down = crashes.down()
    t('no model is down after a crash', not down, '; '.join(d['words'] for d in down) or 'localmodels crashes for the record')
    from . import errors as _errors
    errs = _errors.recent(60, 200)
    t('no errors recorded in the last hour', not errs,
      f"{len(errs)} recorded; last: {errs[0].get('source')} · {errs[0].get('where')}: {errs[0].get('error')} (localmodels errors --trace)" if errs else 'localmodels errors for the record')
    from . import maint
    if dash.door()['stale']: print("  skip instance-manager check (the gateway runs stale code until localmodels install)")
    elif maint.switched_off() or maint.on():            # paused: it skips the crash watch, which records each look
        print(f"  skip instance-manager check (paused: {'local models switched off' if maint.switched_off() else 'maintenance window active'})")
    else:
        seen = time.time() - (crashes._read(fresh=True).get('at') or 0)
        t("instance manager polls every 20 s (crashes, replicas, recycles)", seen < 90, f'last look {seen:.0f} s ago')
    me = whois.identity(os.getpid())
    t('whois identifies the client process', me.get('project') is not None, f"{me.get('project')} · {me.get('app')}")
    # A model may have been swapped or switched off while the sandbox suites ran.
    loaded = [r['id'] for r in llmshare.chat_rows(llmshare.loaded(fresh=True)) if r['state'] != 'LOADING']
    busy = sum(llmshare.busy_slots().values())
    if loaded and not maint.on() and llmshare.hub_up() and busy < llmshare.at_once():
        step = f'gateway check {os.getpid()}-{time.monotonic_ns()}'
        body = dict(model=loaded[0], messages=[{'role': 'user', 'content': 'Say OK.'}], max_tokens=5)
        req = urllib.request.Request(llmshare.HUB + '/v1/chat/completions', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Project': 'local-models', 'X-Kind': 'test', 'X-Step': step, 'X-Timeout': '300'})
        try:
            with llmshare.urlopen(req, timeout=300) as reply: got = bool(json.load(reply).get('choices'))
            note = 'response had no choices' if not got else 'request completed but no matching log record appeared'
        except (OSError, ValueError) as e: got, note = False, str(e)
        mine = []
        end = time.monotonic() + 10
        while got and not mine:
            mine = llmshare.recent_requests(1, 0.5, lambda d: d.get('step') == step and d.get('project') == 'local-models' and d.get('kind') == 'test')
            if mine or time.monotonic() >= end: break
            time.sleep(0.1)
        if mine:
            row = mine[0]
            note = f"{row['project']} · {row['kind']} · {row.get('seconds')} s" + (f" (queued {row['waited']} s)" if (row.get('waited') or 0) > 2 else '')
        t('test request through the gateway completed and logged', got and bool(mine), note)
    else: print('  skip test request (no loaded chat model, maintenance, gateway down, or every slot busy)')
    p = subprocess.run([PY, os.path.join(HOME, 'bin', 'localmodels'), 'mcp'], input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                       '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n', capture_output=True, text=True, timeout=30)
    names = []
    for line in p.stdout.split('\n'):
        try: names += [x['name'] for x in json.loads(line).get('result', {}).get('tools', [])]
        except (ValueError, AttributeError): pass
    t('the tools answer (MCP)', set(names) == {x['name'] for x in mcp_server.TOOLS}, ', '.join(names))
    t('gateway runs this code', not dash.door()['stale'], 'localmodels install refreshes it' if dash.door()['stale'] else digest(HOME))
    for name, good, note in supervisor_checks(dash.health(fresh=True)): t(name, good, note)
    av = app_installed()
    if av:                                                 # the menu-bar app, when installed: running, and built from this release
        running = not subprocess.run(['pgrep', '-x', APP_EXE], capture_output=True).returncode
        t('menu-bar app runs this release', running and av == VERSION,
          f'{av}' + ('' if running else ', not running') + ('' if av == VERSION else f'; this release is {VERSION}: localmodels app install'))
    print(f"{sum(ok)} of {len(ok)} ok")
    return 0 if all(ok) else 1


def supervisor_checks(h, sup=None):
    """The supervisor's items for `localmodels check`, from the serving worker's /hub/health h (and supervisor.json sup):
    -> [(name, ok, note)]; none for a gateway from before releases."""
    if not h or 'handoff' not in h: return []
    from . import deploy, supervisor
    sup = sup if sup is not None else deploy.supervisor_state()
    rows = h.get('gateways') or []
    srv = [r for r in rows if r.get('role') == 'serving']
    drain = [r for r in rows if r.get('role') == 'draining']
    out = [('exactly one serving worker', len(srv) == 1, ', '.join(f"gen {r.get('gen')} (pid {r['pid']})" for r in srv) or 'none')]
    roles, pids = h.get('roles') or {}, {r['pid'] for r in srv}
    out.append(('each role held by the serving worker', bool(roles) and all(p in pids for p in roles.values()),
                ', '.join(f'{k}: {v}' for k, v in sorted(roles.items())) or 'no role held'))
    rel = os.path.join(llmshare.SHARED, 'releases', str(h.get('release') or ''))
    want = (supervisor.release_info(rel) or {}).get('supervisor')
    out.append(('the supervisor is alive, with its release\'s digest', bool(sup) and (want is None or sup.get('digest') == want),
                f"pid {sup.get('pid')}, {sup.get('digest')}" if sup else 'no supervisor holds run/supervisor.lock: workers serve unsupervised'))
    lst = h.get('listeners') or {}
    out.append(('both main listeners bound (127.0.0.1 and [::1])', 'v4' in lst and 'v6' in lst,
                ', '.join(sorted(lst)) + ('' if 'v6' in lst else f"; [::1]: {(sup or {}).get('v6_error') or 'not bound'}")))
    if drain: out.append(('workers draining', True, '; '.join(f"gen {r.get('gen')} (pid {r['pid']}): {r.get('open', '?')} connections" for r in drain)))
    return out


# ---------------------------------------------------------------- publishing
def _git(*args, check=True):
    r = subprocess.run(['git', '-C', HOME, *args], capture_output=True, text=True)
    if check and r.returncode: raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


STAMPED = ('localmodels/__init__.py', '.claude-plugin/plugin.json')   # the files a release commit changes, and nothing else


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


def _unquote(path):
    """A file name as git prints it: in double quotes, with C escapes, when it holds a quote, a backslash or a control
    character (core.quotePath=false leaves other characters as they are)."""
    if not (path.startswith('"') and path.endswith('"')): return path
    esc = dict(n='\n', t='\t', a='\a', b='\b', f='\f', r='\r', v='\v')
    return re.sub(r'\\([0-7]{3}|.)', lambda m: chr(int(m.group(1), 8)) if len(m.group(1)) == 3 else esc.get(m.group(1), m.group(1)), path[1:-1])


_SHA = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?$')
_HUNK = re.compile(r'@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def private_check(repo, words_file, tip='HEAD', remote='origin'):
    """What the next push to origin makes public, read against the private word list: every commit on `tip` that no
    origin ref holds (all of them before the first push), each on its own: the lines it adds (a merge against each
    parent), the names of the files it adds or renames, its message, and the message of each annotated tag on it
    (`--follow-tags` sends those). A word a later commit replaced is still in the commit that added it. ->
    {status ('skipped', 'clean' or 'hits'), since ('origin', or None before the first push), lines, files, commits,
    hits: [(where, text)]}; RuntimeError when a git command fails, so a failure never reads as clean."""
    wl = read_words(words_file)
    if wl is None: return dict(status='skipped', since=None, lines=0, files=0, commits=0, hits=[])
    words, allows = wl

    def held(text):
        for a in allows: text = a.sub(' ', text)
        return any(w.search(text) for w in words)
    return _scan_push(repo, tip, held, held, remote)


SECRETS = [re.compile(p) for p in (                      # credential formats with a fixed prefix: few false alarms
    r'\bgh[pousr]_[A-Za-z0-9]{36,}', r'\bgithub_pat_[A-Za-z0-9_]{60,}',               # GitHub tokens
    r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b',                                                  # AWS access key ids
    r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----',                                        # PEM and OpenSSH private keys
    r'\bxox[abposr]-[A-Za-z0-9-]{10,}', r'https://hooks\.slack\.com/services/T[A-Za-z0-9/]+',   # Slack
    r'\bsk-ant-[A-Za-z0-9_-]{20,}', r'\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}',            # Anthropic, OpenAI
    r'\bAIza[0-9A-Za-z_-]{35}\b',                                                      # Google API keys
    r'\b[rs]k_live_[0-9A-Za-z]{24,}', r'\bnpm_[A-Za-z0-9]{36}\b', r'\bhf_[A-Za-z0-9]{34,}\b',   # Stripe, npm, Hugging Face
    r'\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*["\'][^"\'\s]{12,}["\']',   # a literal assigned to such a name
)]
SECRET_FILES = re.compile(r'(?:^|/)(?:\.env(?:\.[^/]*)?|id_(?:rsa|dsa|ecdsa|ed25519)|credentials(?:\.json)?|\.netrc|\.pypirc|'
                          r'[^/]*\.(?:pem|p12|pfx|key|keystore|jks|mobileprovision))$', re.I)
RUNS_CODE = re.compile(r'(?:^|/)(?:\.github/(?:workflows|actions)/.*|action\.ya?ml|package\.json|\.npmrc|setup\.py|'
                       r'setup\.cfg|pyproject\.toml|Makefile|\.gitlab-ci\.yml|\.circleci/.*)$')
SECRET_OK = re.compile(r'\.env\.(?:example|sample|template)$')


def secret_check(repo, tip='HEAD', remote='origin'):
    """What the next push sends, read for credentials (SECRETS: tokens and keys by their fixed formats), files that hold
    them by name (SECRET_FILES), and files that make code run on GitHub or on install (RUNS_CODE: workflows, package
    manifests and their install scripts; this repository runs nothing there, so a new one is a change to look at, the way
    a planted CI step is the usual route to a repository's secrets). Same commits and same answer as private_check."""
    names = lambda n: bool(SECRET_FILES.search(n) and not SECRET_OK.search(n)) or bool(RUNS_CODE.search(n))
    return _scan_push(repo, tip, lambda text: any(p.search(text) for p in SECRETS), names, remote)


def _scan_push(repo, tip, held, held_name, remote='origin'):
    """The commits on `tip` that no ref of `remote` holds, each read on its own: the lines it adds, the names of the files
    it adds or renames, its message, and the annotated tags on it; `held(text)` and `held_name(name)` say what is a hit."""
    out = dict(status='clean', since=None, lines=0, files=0, commits=0, hits=[])

    def git(*a, stdin=None):
        r = subprocess.run(['git', '-C', repo, '-c', 'core.quotePath=false', *a], input=stdin, capture_output=True,
                           encoding='utf-8', errors='replace')
        if r.returncode: raise RuntimeError(f"git {a[0]}: {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    seen = set()

    def hit(where, text, key):                         # once, under the oldest commit that has it (a merge repeats its parents' lines)
        if key not in seen: seen.add(key); out['hits'].append((where, text))
    out['since'] = remote if git('for-each-ref', '--count=1', f'refs/remotes/{remote}').strip() else None
    shas = git('rev-list', '--reverse', tip, '--not', f'--remotes={remote}').split()
    sha, path, n, old, new, names = '', '', 0, 0, 0, set()
    patch = git('diff-tree', '--stdin', '--patch-with-raw', '-m', '-r', '--root', '-M', '--unified=0', '--no-color', '--no-ext-diff',
                '--no-textconv', '--src-prefix=a/', '--dst-prefix=b/', stdin=''.join(s + '\n' for s in shas)) if shas else ''
    for line in patch.split('\n'):
        if old > 0 or new > 0:                         # inside a hunk, whose header counts its lines: nothing in it is a header
            if line.startswith('+'):
                new -= 1; out['lines'] += 1
                if held(line[1:]): hit(f'{sha[:7]} {path}:{n}', line[1:].strip()[:160], (path, line[1:]))
                n += 1
            elif line.startswith('-'): old -= 1
            elif line.startswith(' '): old, new, n = old - 1, new - 1, n + 1
            continue
        if _SHA.match(line): sha = line; continue      # the commit whose diff follows (a merge's comes once per parent)
        if line.startswith(':') and '\t' in line:     # a raw line: the status and the file names
            f = line.split('\t')
            if f[0].split()[-1][:1] in ('A', 'R'):
                name = _unquote(f[-1])
                if name not in names: names.add(name); out['files'] += 1
                if held_name(name): hit(f'{sha[:7]} file name {name}', name, ('name', name))
        elif line.startswith('+++ '):
            p = _unquote(line[4:].rstrip('\t'))
            path = p[2:] if p.startswith('b/') else p
        else:
            m = _HUNK.match(line)
            if m: old, n, new = int(m.group(1) or 1), int(m.group(2)), int(m.group(3) or 1)
    if shas:
        for rec in git('log', '--stdin', '--no-walk=unsorted', '--no-show-signature', '--format=%h%x00%B%x1e', stdin=''.join(s + '\n' for s in shas)).split('\x1e'):
            if '\x00' not in rec: continue
            short, body = rec.strip().split('\x00', 1)
            out['commits'] += 1
            for line in body.split('\n'):
                if held(line): hit(f'commit {short}', line.strip()[:160], ('commit', short, line))
        sent = set(shas)
        for rec in git('for-each-ref', '--format=%(objecttype)%00%(*objectname)%00%(refname:short)%00%(contents)%1e', 'refs/tags').split('\x1e'):
            kind, target, name, body = (rec.lstrip('\n').split('\x00', 3) + ['', '', '', ''])[:4]
            if kind != 'tag' or target not in sent: continue
            for line in body.split('\n'):
                if held(line): hit(f'tag {name}', line.strip()[:160], ('tag', name, line))
    # An explicit tag can be new even when its commit is already on the remote.
    if git('cat-file', '-t', tip).strip() == 'tag':
        headers, _, body = git('cat-file', 'tag', tip).partition('\n\n')
        name = next(line[4:] for line in headers.splitlines() if line.startswith('tag '))
        for line in body.splitlines():
            if held(line): hit(f'tag {name}', line.strip()[:160], ('tag', name, line))
    out['status'] = 'hits' if out['hits'] else 'clean'
    return out


def secret_words(sc):
    """The secrets check's hits as lines a person acts on."""
    out = [f'{where}: {text}' for where, text in sc['hits'][:30]]
    if len(sc['hits']) > 30: out.append(f"... and {len(sc['hits']) - 30} more")
    return out + ['a credential stays in the commit that added it, so amend, squash or rebase that commit before it leaves this Mac,',
                  'and revoke the credential if it was ever pushed; a file that runs code on GitHub or on install (RUNS_CODE in',
                  'localmodels/cli.py) is a change to that rule: add its path there on purpose, in its own commit']


ZERO = '0' * 40
HOOK_MARK = 'localmodels pre-push hook'


def prepush(a, stdin=None):
    """git's pre-push hook (written by `localmodels install`): every push from this checkout, plain `git push` included,
    gets the privacy and secrets checks publish runs, and a push that would rewrite or delete the branch on the remote is
    refused (LOCALMODELS_ALLOW_REWRITE=1 for one done on purpose). git passes the remote's name and URL as arguments and
    one line a ref on stdin: <local ref> <local sha> <remote ref> <remote sha>. -> 0 to let the push go, 1 to stop it."""
    remote = a[0] if a else 'origin'
    git = lambda *x: subprocess.run(['git', *x], capture_output=True, text=True)
    repo = git('rev-parse', '--show-toplevel').stdout.strip() or os.getcwd()
    why, tips = [], []
    for line in (stdin if stdin is not None else sys.stdin.read()).split('\n'):
        f = line.split()
        if len(f) != 4: continue
        lref, lsha, rref, rsha = f
        if lsha == ZERO:
            if rref.startswith('refs/heads/') and os.environ.get('LOCALMODELS_ALLOW_REWRITE') != '1':
                why.append(f'the push deletes {rref} on {remote}')
            continue
        if rsha != ZERO and os.environ.get('LOCALMODELS_ALLOW_REWRITE') != '1':
            if git('-C', repo, 'cat-file', '-e', rsha).returncode:
                why.append(f'{remote} holds {rsha[:7]} on {rref}, which this checkout does not have: fetch and look at it first '
                           f'(git fetch {remote}; git log {lsha[:7]}..{remote}/{rref.split("/")[-1]})')
            elif rref.startswith('refs/heads/') and git('-C', repo, 'merge-base', '--is-ancestor', rsha, lsha).returncode:
                why.append(f'the push rewrites {rref} on {remote} (a force push)')
        tips.append(lsha)
    for tip in tips:
        try:
            pc = private_check(repo, PRIVATE_WORDS, tip=tip, remote=remote)
            sc = secret_check(repo, tip=tip, remote=remote)
        except RuntimeError as e: why.append(f'could not read what the push sends: {e}'); continue
        if pc['status'] == 'skipped': why.append(f'private word list unavailable at {PRIVATE_WORDS}: privacy check skipped')
        why += [f'private word: {w}: {t}' for w, t in pc['hits'][:20]]
        if sc['hits']: why += secret_words(sc)
    if why:
        print('localmodels pre-push: push stopped', file=sys.stderr)
        for w in why: print(f'  {w}', file=sys.stderr)
        print('  (LOCALMODELS_ALLOW_REWRITE=1 lets a rewrite or a deletion through on purpose; the checks themselves have no bypass)', file=sys.stderr)
        return 1
    return 0


def set_prepush_hook(repo=None):
    """The pre-push hook in this checkout's .git/hooks (worktrees share it), calling this hub's command, so a push
    from any session gets the checks. A hook that is not this one is left in place and named. -> words."""
    repo = repo or HOME
    gitdir = os.path.join(repo, '.git')
    if not os.path.isdir(gitdir): return f'no pre-push hook: {repo} is not a git checkout'
    path = os.path.join(gitdir, 'hooks', 'pre-push')
    body = (f'#!/bin/sh\n# {HOOK_MARK} (written by localmodels install): the privacy and secrets checks on every push\n'
            f'exec "{PY}" "{os.path.join(HOME, "bin", "localmodels")}" prepush "$@"\n')
    try: cur = open(path).read()
    except OSError: cur = None
    if cur == body: return f'the pre-push hook is in place ({path})'
    chained = path + '.chained'                         # another tool's hook that runs the one it replaced first, from beside it
    if cur is not None and HOOK_MARK not in cur and os.path.basename(chained) in cur and os.path.isfile(chained):
        try: was = open(chained).read()
        except OSError: was = ''
        if was == body: return f'the pre-push hook is in place, run first by the hook at {path} ({chained})'
        if HOOK_MARK in was:
            with open(chained, 'w') as f: f.write(body)
            os.chmod(chained, 0o755)
            return f'the pre-push hook updated, run first by the hook at {path} ({chained})'
    if cur is not None and HOOK_MARK not in cur: return f'a pre-push hook of its own is at {path}: left as it is, so pushes are not checked'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f: f.write(body)
    os.chmod(path, 0o755)
    return f'the pre-push hook {"updated" if cur else "written"}: every push from this checkout gets the privacy and secrets checks ({path})'


RELEASE_VERSION = re.compile(r'(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}')


def release_checkout():
    """The clean checkout being released. Both committed version files must agree."""
    if _git('status', '--porcelain'):
        raise ValueError('the working tree has changes: commit the intended changes first (git status)')
    branch, head = _git('rev-parse', '--abbrev-ref', 'HEAD'), _git('rev-parse', 'HEAD')
    if branch == 'HEAD': raise ValueError('HEAD is detached: check out the release branch first')
    match = re.search(r"^VERSION = '([^']+)'$", _git('show', f'{head}:{STAMPED[0]}'), re.M)
    plugin = json.loads(_git('show', f'{head}:{STAMPED[1]}'))
    if not match or not RELEASE_VERSION.fullmatch(match[1]) or not isinstance(plugin, dict) or plugin.get('version') != match[1]:
        raise ValueError('the hub and plugin must have the same X.Y.Z version')
    return branch, head, match[1]


def release_tag(version, head):
    """The exact annotated tag object for this version at head, or None."""
    tag = _git('rev-parse', '-q', '--verify', f'refs/tags/v{version}^{{tag}}', check=False)
    if tag and _git('rev-parse', f'{tag}^{{commit}}') == head: return tag
    return None


def stamp(repo, want, head):
    """The version `want` stamped into the code and the plugin manifest and committed on its own, on `head`, the commit
    the checks read. -> the release commit's sha; RuntimeError when HEAD has moved since the checks or anything is
    staged or changed, so nothing goes into the release that was not read."""
    git = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True)
    now = git('rev-parse', 'HEAD').stdout.strip()
    if now != head: raise RuntimeError(f'HEAD moved since the checks ({head[:7]} to {now[:7]}): run localmodels commit again')
    if git('status', '--porcelain').stdout.strip(): raise RuntimeError('the working tree or the index changed: run localmodels commit again')
    init = os.path.join(repo, 'localmodels', '__init__.py')
    text = open(init).read()
    new = re.sub(r"VERSION = '[^']*'", f"VERSION = '{want}'", text, count=1)
    if new == text: raise RuntimeError('could not find VERSION in localmodels/__init__.py')
    p = os.path.join(repo, STAMPED[1])
    manifest = open(p).read()
    d = json.loads(manifest); d['version'] = want
    updates = [(init, text, new), (p, manifest, json.dumps(d, indent=2) + '\n')]
    try:
        for path, _, after in updates:
            with open(path, 'w') as f: f.write(after)
        r = git('commit', '-q', '-m', f'Release {want}', '--', *STAMPED)
        if r.returncode: raise RuntimeError((r.stderr or r.stdout).strip())
    except (OSError, RuntimeError):
        for path, before, after in updates:
            if os.path.isfile(path) and open(path).read() == after:
                with open(path, 'w') as f: f.write(before)
        raise
    release = git('rev-parse', 'HEAD').stdout.strip()
    if git('rev-parse', release + '^').stdout.strip() != head:
        raise RuntimeError('HEAD moved during the release commit; check the branch before tagging')
    return release


def commit_release(a):
    """Stamp and tag committed changes locally. Retrying after a tag failure finishes the same version."""
    try:
        branch, head, current = release_checkout()
        want = opt(a, '--version')
        tagged = release_tag(current, head)
        if tagged and (want is None or want == current):
            print(f'v{current} is already committed locally at {head[:7]}'); return 0
        pending = (not tagged and _git('log', '-1', '--format=%s', head) == f'Release {current}'
                   and set(_git('diff-tree', '--no-commit-id', '--name-only', '-r', head).splitlines()) == set(STAMPED))
        if pending:
            if want is not None and want != current: raise ValueError(f'finish the untagged release {current} first')
            want = current
        else:
            major, minor, patch = map(int, current.split('.'))
            if want is None: want = f'{major}.{minor}.{patch + 1}'
            if not RELEASE_VERSION.fullmatch(want): raise ValueError(f'{want!r} is not a version like 1.2.3')
            if tuple(map(int, want.split('.'))) <= (major, minor, patch): raise ValueError(f'{want} is not above {current}')
        if _git('tag', '--list', f'v{want}'): raise ValueError(f'tag v{want} already exists')
        if '--dry' in a:
            print(f'would tag v{want} locally on {branch}' + (' (already stamped)' if pending else ' after stamping the version')); return 0
        lock = os.path.join(HOME, _git('rev-parse', '--git-common-dir'), 'localmodels-release.lock')
        with llmshare.flocked(lock):
            if release_checkout() != (branch, head, current) or _git('tag', '--list', f'v{want}'):
                raise ValueError('the checkout or tag changed: run localmodels commit again')
            release = head if pending else stamp(HOME, want, head)
            _git('tag', '-a', f'v{want}', '-m', f'Release {want}', release)
        print(f'v{want} committed locally at {release[:7]} on {branch}'); return 0
    except (OSError, RuntimeError, ValueError) as e:
        print(f'local release incomplete: {e}'); return 2


def release_checks(head):
    """The gates for publishing the tagged checkout, including all commits the push would send."""
    print('1. the sandbox tests:')
    suites = run_tests()
    missing = set(TESTS) - {name for name, _, _ in suites}
    if missing: print('   not run: ' + ', '.join(sorted(missing)) + '; nothing published'); return 1
    bad = False
    for name, good, note in suites:
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
    else: print('   the claude command is not on PATH: plugin not checked; nothing published'); return 1
    print('3. the privacy check:')
    try: pc = private_check(HOME, PRIVATE_WORDS, tip=head)
    except RuntimeError as e: print(f'   could not read what the push sends ({e}); nothing published'); return 1
    if pc['status'] == 'skipped':
        print(f'   no private word list at {PRIVATE_WORDS}: not checked (one word or pattern a line, `allow: <pattern>` for what may')
        print('   appear, # for notes); nothing published'); return 1
    elif pc['status'] == 'hits':
        for where, text in pc['hits'][:30]: print(f'   {where}: {text}')
        if len(pc['hits']) > 30: print(f"   ... and {len(pc['hits']) - 30} more")
        print('   nothing published. A hit stays in the commit named before it: a later commit does not remove it, so amend,')
        print(f"   squash or rebase that commit (none of these is on origin yet), or add an `allow:` line to {PRIVATE_WORDS}")
        print('   for text that is fine to make public'); return 1
    else:
        print(f"   clean: {pc['lines']} added lines, {pc['files']} new file names and {pc['commits']} commit messages, in "
              + ('the commits not yet on origin' if pc['since'] else 'every commit (nothing is on origin yet)'))
    print('   the secrets check:')
    try: sc = secret_check(HOME, tip=head)
    except RuntimeError as e: print(f'   could not read what the push sends ({e}); nothing published'); return 1
    if sc['hits']:
        for line in secret_words(sc): print('   ' + line)
        print('   nothing published'); return 1
    print('   clean: no credential, no key file, no file that runs code on GitHub or on install')
    return 0


def publish(a):
    """Publish the existing local tag and its commit atomically. A retry never creates a release."""
    try:
        branch, head, version = release_checkout()
        want = opt(a, '--version')
        if want is not None and want != version: raise ValueError(f'the checkout is {version}, not {want}')
        tag = release_tag(version, head)
        if not tag: raise ValueError(f'HEAD is not tagged v{version}; prepare the local release with localmodels commit')
        url = opt(a, '--remote')
        has_origin = 'origin' in _git('remote').splitlines()
        if has_origin:
            if url and url != _git('remote', 'get-url', '--push', 'origin'): raise ValueError('--remote differs from the configured origin')
            _git('fetch', '--no-tags', '--prune', 'origin')
        elif not url: raise ValueError('no origin configured: supply --remote URL for the first push')
        elif _git('ls-remote', '--refs', url): raise ValueError('the first remote is not empty: configure it as origin first')
        if release_checks(tag): return 1
        if release_checkout() != (branch, head, version) or release_tag(version, head) != tag:
            raise ValueError('the checked release changed: run localmodels push again')
        dry = '--dry' in a
        if not has_origin and not dry: _git('remote', 'add', 'origin', url)
        dest = url if dry and not has_origin else 'origin'
        _git('push', '--atomic', '--no-follow-tags', *(['--dry-run'] if dry else []), dest,
             f'{head}:refs/heads/{branch}', f'{tag}:refs/tags/v{version}')
        print(f'{"would push" if dry else "pushed"} v{version} ({head[:7]}) and {branch} to origin')
        return 0
    except (OSError, RuntimeError, ValueError) as e:
        print(f'nothing published: {e}'); return 1


FLAGS = {   # verb: (options that take a value, options that stand alone); a verb not listed reads its own words
            # demo's --project, --workers, --model and --seed: one project's program, started by the demo itself
    'status': ((), ()), 'st': ((), ()), 'open': ((), ()), 'log': (('--hours',), ()), 'swapcheck': ((), ()),
    'use': (('--context', '--parallel'), ('--dry',)), 'keep': ((), ()), 'recycle': ((), ('--status',)),
    'demo': (('--minutes', '--project', '--workers', '--model', '--seed'), ('--dry',)), 'pictures': (('--out',), ('--dry',)), 'peer': (('--hours',), ('--dry', '--force')), 'peers': (('--hours',), ('--dry', '--force')), 'crashes': (('--days',), ('--scan',)),
    'errors': (('--minutes',), ('--trace',)), 'ask': (('--dir', '--schema', '--max-turns', '--model'), ('--no-files', '--no-think')),
    'batch': (('--dir', '--parallel', '--model', '--peers'), ()), 'jobs': ((), ('--results', '--retry-failed')), 'prune': (('--days',), ()), 'check': ((), ()),
    'install': (('--wait',), ('--plugin', '--dry', '--no-claude', '--force')), 'uninstall': ((), ()), 'start': ((), ()), 'stop': ((), ('--now',)),
    'restart': ((), ('--cold', '--now')), 'rollback': ((), ()), 'mcp': ((), ()), 'on': ((), ()), 'off': ((), ('--force', '--peers')), 'app': ((), ()), 'ext': ((), ()), 'version': ((), ()),
    'commit': (('--version',), ('--dry',)), 'push': (('--version', '--remote'), ('--dry',)), 'publish': (('--version', '--remote'), ('--dry',)),
}
LEAST = {'--hours': 0, '--days': 0, '--wait': 0, '--minutes': 0, '--max-turns': 1, '--parallel': 0, '--context': 1}


def checked_args(verb, a):
    """The options after a verb, checked before anything runs: one it does not take, one that needs a value and has
    none, or a number below what it allows -> words for a person (exit 2); None when all is well."""
    values, alone = FLAGS[verb]
    i = 0
    while i < len(a):
        x = a[i]
        if x in values:
            if i + 1 >= len(a) or a[i + 1].startswith('--'): return f'{x} needs a value (localmodels help)'
            if x in LEAST:
                try: bad = float(a[i + 1]) < LEAST[x]
                except ValueError: return f'{x}: {a[i + 1]!r} is not a number'
                if bad: return f'{x}: {a[i + 1]} is below {LEAST[x]}'
            i += 2; continue
        if x in alone or not x.startswith('-'): i += 1; continue
        return f'unknown option {x} for {verb} (localmodels help)'
    return None


def main(argv):
    a = []
    for x in argv:                                       # --flag=value is --flag value; --dry-run is --dry
        if x.startswith('--') and '=' in x: a += x.split('=', 1)
        else: a.append('--dry' if x == '--dry-run' else x)
    if '-h' in a or '--help' in a: print(__doc__.strip()); return 0   # anywhere: a help flag never runs a verb
    verb = a.pop(0) if a and not a[0].startswith('-') else 'status'
    if verb in FLAGS:
        why = checked_args(verb, a)
        if why: print(why); return 2
    if verb in ('status', 'st'): return status()
    if verb == 'dash':
        from . import dash
        return dash.main(a)
    if verb == 'open':
        subprocess.run(['open', llmshare.HUB + '/']); print(f'the dashboard: {llmshare.HUB}/'); return 0
    if verb == 'log':
        h = num(a, '--hours', 24.0); return log(h) if h is not None else 2
    if verb == 'swapcheck': return swapcheck([x for x in a if not x.startswith('-')])
    if verb == 'jobs': return jobs(args=a)
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
    if verb == 'off': return off_cmd(a)
    if verb == 'app': return app_cmd(a)
    if verb == 'ext': return ext_cmd(a)
    if verb == 'on': return on_cmd()
    if verb == 'present': return present_cmd(a)
    if verb == 'demo': return demo_cmd(a)
    if verb == 'pictures':
        from . import pictures
        out = a[a.index('--out') + 1] if '--out' in a else None
        return pictures.take(os.path.abspath(os.path.expanduser(out)) if out else None, dry='--dry' in a)
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
        if not a or a[0].startswith('-'): print('localmodels batch <jobs.json|requests.jsonl>'); return 2
        par = opt(a, '--parallel')
        if par is not None and not par.isdecimal(): print('--parallel needs a positive integer'); return 2
        return batch(dict(jobs=a[0], dir=opt(a, '--dir'), parallel=int(par) if par is not None else None, model=opt(a, '--model'), peers=opt(a, '--peers')))
    if verb == 'check': return check()
    if verb == 'install':
        w = num(a, '--wait', 1.5); return install(w * 60, plugin='--plugin' in a, dry='--dry' in a, no_claude='--no-claude' in a, force='--force' in a) if w is not None else 2
    if verb == 'uninstall': return uninstall()
    if verb in ('start', 'stop', 'restart', 'rollback'): return door(verb, a)
    if verb == 'gateway':
        if '--worker' in a:                                  # a worker, started by the supervisor (worker.py)
            from . import worker
            return worker.main(a)
        from . import gateway
        return gateway.main(a)
    if verb == 'mcp':
        from . import mcp_server
        return mcp_server.main()
    if verb == 'agent-run':
        from . import mcp_server
        if not a: print('localmodels agent-run <folder>'); return 2
        return mcp_server.run_folder(a[0])
    if verb == 'commit': return commit_release(a)
    if verb in ('push', 'publish'): return publish(a)
    if verb == 'prepush': return prepush(a)
    if verb == 'help': print(__doc__.strip()); return 0
    if verb == 'version': print(VERSION); return 0
    print(f'unknown: {verb} (localmodels help)'); return 2
