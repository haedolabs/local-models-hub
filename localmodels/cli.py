"""cli - `localmodels`: every hub job as one command that prints a short summary.

  localmodels                    what is using the local models now, in a few lines
  localmodels open               the dashboard in the browser (http://localhost:1235)
  localmodels dash               the live console in a terminal (--once, --json)
  localmodels log [--hours 24]   the hub's requests by project
  localmodels swapcheck [model ...]   may these models be unloaded or reloaded now? (exit 0 yes, 3 no)
  localmodels use <model> [--context N] [--parallel N] [--dry]   swap to a model (all its copies, for a family)
  localmodels keep               one pass of the door's keeper: a crashed model loaded again, missing copies, a recycle due
  localmodels recycle [model] | --status   ask for fresh copies before a batch (copies past half their band are recycled
                                 first) | each copy's work since it was loaded against its band
  localmodels maintenance [in 15m | at 20:30 | now | after-batches] [for 30m] [why ...] | off
                                 tell every program and Claude session the local models go down for a while; while on,
                                 new requests get 503 with Retry-After and the keeper stands down; off loads again what was
                                 loaded. With no words: what is set, and which batches would run into it
  localmodels name               each project as seen and as shown on every screen, with its batches, jobs and scripts
  localmodels name project <as seen> <shown>            what every screen calls a project (a folder or an X-Project)
  localmodels name batch <project> <as seen> <shown>    a name for one of its batches, jobs or scripts
  localmodels name private <project> | public <project> private: screens show only the names set for it
  localmodels name forget <project> [<as seen>]         drop a name: one label, or the project's whole entry
  localmodels crashes [--days 7] [--scan]   LM Studio's crashes, the reloads and recycles (--scan: older ones in its logs)
  localmodels peer               this Mac's peers (other Macs whose hubs take this door's requests when its slots are full)
                                 and the link to each; and this Mac as a peer, when it serves as one
  localmodels peer add <name> <address>   a peer: its Thunderbolt Bridge address (port 1235 unless given)
  localmodels peer remove <name>
  localmodels peer serve <address> | off  this Mac as a peer: its door also listens on that address (its Thunderbolt
                                 Bridge's), for requests with the shared token
  localmodels peer token         make the shared token if there is none, and say how to copy it to the other Mac
  localmodels peer test [name]   the door looks at its peers now, and this terminal looks too (to tell macOS blocking the
                                 door from a link that is down)
  localmodels ask "<question>" [--dir FOLDER] [--no-files] [--no-think] [--schema FILE] [--max-turns N]
                                 (--no-think: no thinking: several times faster, for lookups and extraction)
  localmodels batch <jobs.json> [--dir FOLDER] [--parallel N]
  localmodels jobs               the last local-agent jobs started through the hub
  localmodels prune [--days 30]  remove local-agent job folders and logs older than N days (they hold prompts and answers)
  localmodels check              the hub's self-test
  localmodels install [--wait MINUTES] [--plugin] [--dry] [--no-claude]
                                 set the hub up on this Mac; run it again after changing the hub's code (it waits for a
                                 moment with no request passing through the door: 1.5 minutes unless told longer).
                                 --plugin: installed as a Claude Code plugin, which brings the tools. --dry: say what
                                 would be done, write nothing. --no-claude: no MCP registration, no signpost line
  localmodels uninstall          undo install
  localmodels start | stop | restart     the hub's door (start on an open door does nothing; restart waits for quiet)
  localmodels gateway            the door in the foreground (what launchd runs)
  localmodels mcp                the MCP server (Claude Code starts it)
  localmodels publish [--version X.Y.Z] [--dry] [--remote URL]   the tests, the plugin check and the privacy check, then
                                 the version stamped, committed, tagged and pushed (--dry: the checks only)
"""
import glob, json, os, plistlib, re, shutil, subprocess, sys, time, urllib.request

from . import HOME, NAME, VERSION, llmshare

LABEL = os.environ.get('LOCALMODELS_LABEL') or 'com.local-models.hub'
OLD_LABELS = ()                                          # earlier LaunchAgent labels an install retires
PLIST = os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')
INSTALLED = os.path.join(llmshare.SHARED, 'hub')
LOGS = os.path.expanduser('~/Library/Logs/LocalModels')
PY = shutil.which('python3') or sys.executable or '/usr/bin/python3'   # the PATH's python3 (a stable link, e.g. /opt/homebrew/bin/python3), not a versioned Cellar path
BINS = ('/opt/homebrew/bin', '/usr/local/bin', os.path.expanduser('~/.local/bin'))
SIGNPOST_FILE = os.path.expanduser('~/.claude/CLAUDE.md')
SIGNPOST = (f"Local models on this Mac (LM Studio) are shared through the local-models hub: its rules live in {HOME}/GUIDE.md "
            f"(or the local-models tools' guide). Send requests to {llmshare.HUB}/v1, never straight to LM Studio's port 1234. "
            "<!-- local-models hub signpost -->")
TESTS = ('test_crashes.py', 'test_rules.py', 'test_door.py', 'test_agent.py', 'test_peers.py')


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
    when = (f"{ago_left(p['left'])} left, done about {finish_words(p['finish'])}" if p.get('left') else 'finishing') if p.get('left') is not None \
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
    print(f"the hub's door: {'open on ' + llmshare.HUB if d['up'] else 'closed (localmodels start)'}"
          + (' · runs older code: localmodels install' if d['up'] and d['stale'] else ''))
    print(f"LM Studio: server {lm['server']} · " + ('; '.join(dash.family_words(f) for f in lm['families']) or 'no model loaded')
          + ('' if llmshare.read_ok() else ' · (the last listing failed: this is the one before)'))
    if llmshare.settings_error(): print(f'  SETTINGS NOT READ: {llmshare.settings_error()} (the last good settings stand)')
    for line in peer_lines(d['up']): print(line)
    try:
        v = recycle.view(lm['models'])
        if v['copies']:
            print('  since loaded: ' + ' · '.join(f"{dash.short(i)} {c['tokens'] / 1000:.0f}k tokens" + (f" ({int(100 * c['share'])}% of its band)" if c.get('share') is not None else '')
                                              for i, c in v['copies'].items()))
        if v.get('job'): print('  ' + recycle.words(v['job']))
    except Exception as e: print(f'  (could not read the copies\' work: {e})')
    for line in crash_lines(): print(line)
    print('using the models now:' if users else 'using the models now: nothing')
    for r in users:
        b = r.get('batch') or {}
        state = (f"{r['answering']} answering" + (f", {r['waiting']} waiting" if r['waiting'] else '')) if r['answering'] or r['waiting'] else \
            f"{r['direct']} connected straight to LM Studio (not through the hub)" if r['direct'] else 'between requests'
        who = f" · Claude: {r['title']}" if r.get('title') else ' · a Claude session' if r.get('session') else f" · {r['app']}" if r.get('app') else ''
        print(f"  {r['project']}: {dash.kind_words(r)} on {', '.join(dash.short(m) for m in r['models']) or '?'} · {state} · {'started' if (r.get('batch') or r.get('job') or {}).get('started') else 'since'} {ago(r['since'])}"
              + (track_words('batch', b) if b.get('total') else '') + track_words('job', r.get('job')) + (f" · {r['step']}" if r.get('step') and not b.get('total') and not r.get('job') else '') + who)
    work = [c for c in cs if c['working']]
    print(f"Claude Code: {len(work)} working" + (f" ({', '.join(sorted({c['project'] for c in work}))})" if work else '')
          + f", {len(cs) - len(work)} idle in the last 3 hours")
    lh = dash.last_hour()
    if lh: print('last hour through the hub: ' + '; '.join(f"{p} {v['requests']} requests, {v['tokens']:,} tokens" for p, v in lh.items()))
    n, secs, refused = llmshare.waits(7)
    print(f'last 7 days: {n} waits for a turn ({secs / 60:.1f} min in all), {refused} swaps refused')
    return 0


def log(hours=24):
    from . import names
    rows = [names.request(r) for r in llmshare.requests_log(hours)]
    if not rows: print(f'no requests through the hub in the last {hours:g} hours'); return 0
    print(f'the last {hours:g} hours through the hub: {len(rows)} requests')
    for p, v in sorted(llmshare.by_project(rows).items(), key=lambda x: -x[1]['requests']):
        kinds = {}
        for d in rows:
            if (d.get('project') or '?') == p: kinds[d.get('kind') or 'other'] = kinds.get(d.get('kind') or 'other', 0) + 1
        print(f"  {p}: {v['requests']} requests ({', '.join(f'{n} {k}' for k, n in kinds.items())}), {v['tokens']:,} tokens, "
              f"{v['seconds'] / 60:.1f} min answering, {v['waited'] / 60:.1f} min waiting for a turn")
    bad = [d for d in rows if (d.get('status') or 0) >= 400 and d.get('status') != 499]
    gone = [d for d in rows if d.get('status') == 499]
    resent = [d for d in rows if (d.get('tries') or 1) > 1]
    if bad: print(f"  {len(bad)} answered with an error (e.g. {bad[-1].get('status')} for {bad[-1].get('project')}: {bad[-1].get('note') or ''})")
    if resent: print(f"  {len(resent)} sent again by the door after the model crashed; {sum(1 for d in resent if d.get('status') == 200)} then answered")
    if gone: print(f"  {len(gone)} dropped because the program hung up while it waited")
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
        print(f"read LM Studio's logs of the last {days:g} days: {n} crash{'es' if n != 1 else ''} the hub had no record of, recorded now")
    xs = crashes.history(days)
    print(f"LM Studio crashed {len(xs)} time{'s' if len(xs) != 1 else ''} in the last {days:g} days" + (':' if xs else ''))
    for x in xs:
        print('  ' + crashes.words(x))
        for s in x.get('served') or []:
            print(f"    {s['model']} had answered {s['requests']} requests ({s.get('tokens', s['written']):,} tokens processed) since {ago(s['since'])}")
    rs = recycle.recent(days)
    print(f"copies recycled: {sum(1 for r in rs if r['event'] == 'recycled')}" + (':' if rs else ''))
    for r in rs[:12]:
        when = time.strftime('%d %b %H:%M', time.localtime(r['at']))
        print(f"  {when} {r.get('model')}: " + (f"{r.get('tokens') or 0:,} tokens processed in {r.get('hours') or 0:.1f} h, back in {r.get('seconds') or 0:.0f} s" if r['event'] == 'recycled' else f"given up ({r.get('why')})"))
    down = crashes.down()
    print('down now: ' + '; '.join(d['words'] for d in down) if down else 'down now: nothing (every model that crashed is loaded again, or was let go)')
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
        seen.add(p['name']); out.append('  peer ' + p['words'])
    for name in peers.configured():
        if name not in seen: out.append(f"  peer {name}: the door has not looked at it yet" + ('' if door_up else ' (the door is closed)'))
    for w in peers.problems(): out.append(f'  PEER SETTINGS: {w}')
    if peers.serving():
        d = door_link() if door_up else None
        out.append('  this Mac serves as a peer: ' + ((d or {}).get('listening') or ('the door is closed' if not door_up else
                                                                                   'not listening yet (the door runs older code until localmodels install?)')))
    return out


def edit_settings(change):
    """Change this Mac's settings file (models.json) in place; the door rereads it as it changes. -> words or None."""
    src = llmshare.MODELS if os.path.exists(llmshare.MODELS) else llmshare.MODELS_DEFAULT
    try: d = json.load(open(src))
    except (OSError, ValueError) as e: return f'could not read {src} ({type(e).__name__}: {e}): fix it first'
    change(d)
    os.makedirs(os.path.dirname(llmshare.MODELS), exist_ok=True)
    tmp = llmshare.MODELS + '.tmp'
    with open(tmp, 'w') as f: json.dump(d, f, indent=1); f.write('\n')
    os.replace(tmp, llmshare.MODELS)
    return None


def token_copy(peer_ssh='<user>@<the other Mac>'):
    """The command that copies this Mac's token to another Mac over SSH (the token never shows on a screen)."""
    from . import peers
    rel = os.path.relpath(peers.TOKEN_FILE, os.path.expanduser('~'))
    return f"""ssh {peer_ssh} 'mkdir -p "{os.path.dirname(rel)}" && umask 077 && cat > "{rel}" && chmod 600 "{rel}"' < "{peers.TOKEN_FILE}\""""


def token_words():
    from . import peers
    made = peers.make_token()
    return (f"the token is {'made' if made else 'in'} {peers.TOKEN_FILE} (mode 0600; the same file must be on every Mac of the set, "
            'and it never goes on a screen or in the settings)')


def peer_cmd(a):
    """localmodels peer [add <name> <address> | remove <name> | serve <address> | serve off | token | test [name]]."""
    from . import peers
    w = [x for x in a if x != 'peer']
    verb = w[0] if w else ''
    if not verb:
        print(f'this Mac shows as {peers.me()} to its peers ("name" in the settings changes it)')
        lines = peer_lines(llmshare.hub_up(fresh=True))
        if not lines: print('no peers, and this Mac does not serve as one: localmodels peer add <name> <address> (GUIDE.md, Peers)')
        for line in lines: print(line)
        if (peers.configured() or peers.serving()) and peers.token_problem(): print(f'  TOKEN: {peers.token_problem()}')
        return 0
    if verb == 'add' and len(w) == 3:
        name, u = w[1], peers.parse_url(w[2])
        if not peers.NAME.match(name): print(f'{name!r}: a peer name is one plain word (letters, digits, . _ -)'); return 2
        if not u: print(f'{w[2]!r} is not an address (192.168.213.2, or http://192.168.213.2:1235)'); return 2
        err = edit_settings(lambda s: s.setdefault('peers', {}).__setitem__(name, dict(url=u[0])))
        if err: print(err); return 1
        print(f'{name} is a peer of this door: {u[0]} (in {llmshare.MODELS}; the door looks at it within seconds)')
        if peers.address_words(u[1]): print(f'  note: {peers.address_words(u[1])}')
        print('  ' + token_words())
        print(f'next (GUIDE.md, Peers, has every step and which need a person):')
        print(f'  1. copy the token to {name}: {token_copy(f"<user>@{u[1]}")}')
        print(f'  2. on {name}: localmodels peer serve {u[1]}')
        print('  3. here: localmodels peer test')
        return 0
    if verb == 'remove' and len(w) == 2:
        if w[1] not in peers.configured(): print(f'{w[1]} is not a peer of this door'); return 2
        err = edit_settings(lambda s: (s.get('peers') or {}).pop(w[1], None) if isinstance(s.get('peers'), dict) else None)
        if err: print(err); return 1
        print(f'{w[1]} is no longer a peer: nothing new is sent there (requests on it now finish)'); return 0
    if verb == 'serve' and len(w) == 2:
        if w[1] == 'off':
            err = edit_settings(lambda s: s.pop('serve', None))
            if err: print(err); return 1
            print('this Mac no longer serves as a peer: the door stops listening beyond this Mac within 5 s'); return 0
        addr = w[1].strip('[]')
        try: peers.ipaddress.ip_address(addr.split('%')[0])
        except ValueError: print(f'{addr!r} is not an address of this Mac (its Thunderbolt Bridge\'s, e.g. 192.168.213.2)'); return 2
        err = edit_settings(lambda s: s.__setitem__('serve', dict(address=addr)))
        if err: print(err); return 1
        iface = peers.interface_of(addr)
        port = peers.ports().get(iface) if iface else None
        print(f'this Mac serves as a peer on {addr}:{llmshare.HUB_PORT}' + (f' ({port or iface})' if iface else ''))
        if not iface: print('  note: no interface of this Mac holds that address yet: the door listens once one does (GUIDE.md, Peers: the fixed address)')
        elif not peers.bridge(iface, {iface: port} if port else {}) and not peers.loopback(addr): print(f'  note: {iface} is not the Thunderbolt Bridge')
        if peers.address_words(addr): print(f'  note: {peers.address_words(addr)}')
        print('  ' + token_words())
        print('  the door listens there within 5 s, only for requests with the token; `localmodels peer` says whether it does')
        return 0
    if verb == 'token' and len(w) == 1:
        print(token_words())
        print(f'copy it to another Mac: {token_copy()}')
        return 0
    if verb == 'test' and len(w) <= 2:
        d = door_link(force=True)
        if d is None: print("the door does not answer /hub/link: it is closed (localmodels start), or runs older code (localmodels install)"); return 1
        ps = [p for p in d['peers'] if len(w) == 1 or p['name'] == w[1]]
        if not ps: print('no such peer' if len(w) == 2 else 'no peers: localmodels peer add <name> <address>'); return 2
        bad = 0
        for p in ps:
            print('the door: ' + p['words'])
            tok, here = peers.token(), None
            try:
                req = urllib.request.Request(p['url'] + '/hub/health', headers={'X-Hub-Token': tok or ''})
                peers._open(req, timeout=peers.CONNECT).read(); here = 'reached it, and it took the token'
            except urllib.error.HTTPError as e: here = f"reached it, but it answered {e.code}" + (' (the token)' if e.code in (401, 403) else '')
            except OSError as e: here = peers.reach_words(e)
            print(f'  this terminal: {here}')
            if p['state'] == 'down' and here.startswith('reached'):
                print("  this terminal reaches it and the door does not: macOS's Local Network permission is blocking the door. "
                      'System Settings → Privacy & Security → Local Network: turn on python3, then: localmodels peer test')
            iface, port = peers.route_of(p['host'])
            print(f"  the route: {port or iface or 'none'}" + (f' ({iface})' if iface and port and iface not in port else '')
                  + (f" · {peers.route_words(iface, {iface: port} if port else {})}" if peers.route_words(iface, {iface: port} if port else {}) else ''))
            bad += p['state'] != 'up'
        if d.get('serving'): print(f"this Mac serves as a peer: {d.get('listening') or 'not listening yet'}")
        return 1 if bad else 0
    print('localmodels peer [add <name> <address> | remove <name> | serve <address> | serve off | token | test [name]]')
    return 2


def recycle_cmd(a):
    from . import recycle
    if '--status' in a:
        v = recycle.view(llmshare.loaded(fresh=True))
        for i, c in v['copies'].items():
            band = f" · band {c['band']['tokens']:,} (soft {c['band']['soft']:,}): {int(100 * c['share'])}%" if c.get('band') else ' · not recycled (no settings)'
            print(f"{i}: {c['tokens']:,} tokens processed ({c['requests']} requests) since {ago(c['since'])}{band}" + (f" · {c['drift']}" if c.get('drift') else ''))
        print(recycle.words(v['job']) if v.get('job') else 'no recycle in progress')
        return 0
    model = next((x for x in a if not x.startswith('-')), None) or next(iter(llmshare.settings().get('families') or {}), None)
    if not model: print('no family in the settings'); return 1
    r = llmshare.hub_post('/hub/recycle', dict(model=model))
    if r is None: print("the hub's door is closed: localmodels start"); return 1
    print(f"{model}: {r.get('note')}" if r.get('ok') else f"{model}: {r.get('note') or r.get('error')}")
    return 0 if r.get('ok') else 1


def swapcheck(models):
    models = models or [r['id'] for r in llmshare.loaded(fresh=True)]
    why = llmshare.swap_blockers(models)
    if why: print('not now: ' + '; '.join(why)); return 3
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
    print('; '.join(did) if did else 'nothing to do: every family whose first copy is loaded has all its copies, nothing is due for a recycle')
    return 0


def jobs(n=8):
    from . import mcp_server
    ds = sorted(glob.glob(os.path.join(mcp_server.AGENTS, '*', '*')), key=os.path.getmtime)[-n:]
    if not ds: print('no local-agent jobs started through the hub yet'); return 0
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
    return '; '.join(f"{k} as {f['copies']} {'copy' if f['copies'] == 1 else 'copies'} of {f['parallel']} answers at once, context {f['context']:,}"
                     for k, f in s['families'].items())


def install(wait=90, plugin=False, dry=False, no_claude=False):
    uid = f'gui/{os.getuid()}'
    if dry:
        if os.path.exists(llmshare.MODELS): print(f'would keep the settings: {llmshare.MODELS}')
        else: print(f'would write the settings file {llmshare.MODELS}, fitted to this Mac: {_fitted_words(_fitted())}')
        print(f"would copy the door's code to {INSTALLED} and start it at login as {LABEL} ({PLIST}) with {PY}, after a moment with no request passing through")
        if plugin: print("would leave the hub's tools and the rules to the Claude Code plugin (no registration, no signpost line)")
        elif no_claude: print('would register nothing with Claude Code and add no signpost line (--no-claude)')
        else: print(f"would register the hub's tools for every Claude Code session (claude mcp add --scope user local-models), add one signpost line to {SIGNPOST_FILE}, "
                    f"and add the maintenance hook to {SETTINGS_FILE} (silent unless maintenance is set)")
        print(f"would link the localmodels command into the first writable of {', '.join(BINS)}")
        print('nothing written (--dry)')
        return 0
    os.makedirs(LOGS, exist_ok=True)
    if not os.path.exists(llmshare.MODELS):
        os.makedirs(llmshare.SHARED, exist_ok=True); fitted = _fitted()
        with open(llmshare.MODELS, 'w') as f: json.dump(fitted, f, indent=1); f.write('\n')
        print(f'0. the settings file {llmshare.MODELS} written, fitted to this Mac: {_fitted_words(fitted)} (edit it; the door reads it as it changes)')
    else: print(f'0. the settings: {llmshare.MODELS}')
    running = _loaded() or any(_loaded(o) for o in OLD_LABELS)
    if running and not quiet(wait):
        print(f'the door stayed busy for {wait / 60:.1f} minutes ({len(llmshare.inflight())} requests passing through): nothing changed; '
              'try again when it is quiet, or: localmodels install --wait 480')
        return 3
    with llmshare.swapping(wait=120) as got:                    # no load or recycle half done when the door restarts
        if not got: print('a swap or a reload is under way: try again in a moment'); return 3
        for o in OLD_LABELS:
            if _loaded(o):
                _launchctl('bootout', f'{uid}/{o}')
                try: os.remove(os.path.expanduser(f'~/Library/LaunchAgents/{o}.plist'))
                except OSError: pass
                print(f'   (the door under its old name {o} is stopped; it comes back as {LABEL})')
        deploy()
        print(f'1. the door\'s code copied to {INSTALLED} ({digest(INSTALLED)})')
        plist = _plist()
        try: changed = plistlib.load(open(PLIST, 'rb')) != plist
        except (OSError, ValueError): changed = True
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, 'wb') as f: plistlib.dump(plist, f)
        if _loaded() and changed: _launchctl('bootout', f'{uid}/{LABEL}'); time.sleep(1); r = _launchctl('bootstrap', uid, PLIST)
        elif _loaded(): r = _launchctl('kickstart', '-k', f'{uid}/{LABEL}')
        else: r = _launchctl('bootstrap', uid, PLIST)
    up = any(llmshare.hub_up(fresh=True) or time.sleep(0.5) for _ in range(30))
    print(f"2. the door starts at login ({PLIST}): {'open on ' + llmshare.HUB if up else 'NOT answering: ' + (r.stderr.strip() or 'see ' + os.path.join(LOGS, 'gateway.log'))}")
    if plugin:
        print("3. the hub's tools come with the Claude Code plugin (its .mcp.json): nothing registered here")
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
                print('3. the hub\'s tools are already registered for every Claude Code session (local-models)')
            else:
                subprocess.run([claude, 'mcp', 'remove', '--scope', 'user', 'local-models'], capture_output=True, text=True)
                r = subprocess.run([claude, 'mcp', 'add', '--scope', 'user', 'local-models', '--', *want], capture_output=True, text=True)
                print('3. the hub\'s tools registered for every Claude Code session (local-models)' if r.returncode == 0 else
                      f'3. could not register the tools: {(r.stderr or r.stdout).strip()[:300]}')
        else: print('3. the claude command is not on PATH: register the tools by hand (GUIDE.md, setting up)')
        os.makedirs(os.path.dirname(SIGNPOST_FILE), exist_ok=True)
        text = open(SIGNPOST_FILE).read() if os.path.exists(SIGNPOST_FILE) else ''
        if 'local-models hub signpost' in text: print(f'4. the signpost line is already in {SIGNPOST_FILE}')
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
    print('1. the door is stopped and no longer starts at login')
    claude = shutil.which('claude')
    if claude: subprocess.run([claude, 'mcp', 'remove', '--scope', 'user', 'local-models'], capture_output=True, text=True)
    print('2. the hub\'s tools are no longer registered')
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


def maintenance_cmd(a):
    """localmodels maintenance [in 15m | at 20:30 | now | after-batches] [for 30m] [why ...] | off."""
    from . import dash, maint, models
    words = [x for x in a if x != 'maintenance']
    if words[:1] == ['off']:
        for line in models.end_maintenance(): print(line)
        print('the hub is back to normal: requests are answered, the keeper keeps the copies loaded')
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
    print('  every answer from the door now carries X-Hub-Maintenance; the dash, `localmodels`, the status tool and the')
    print('  Claude Code hook say it; when it starts, new requests get 503 with Retry-After and the keeper stands down')
    from . import names
    for g in sorted(dash.live_runs().values(), key=lambda g: g['started']):
        if m.get('start') and g.get('finish') and g['finish'] > m['start']:
            print(f"  note: {names.project(g['project'])} · {names.label(g['project'], g['name']) or 'a batch'} is due to finish about {finish_words(g['finish'])}, after it starts")
    if m['phase'] != 'on': return 0
    rows = llmshare.loaded(fresh=True)
    if llmshare.read_ok(): maint.freeze(llmshare.family(r['id']) for r in rows)
    n = len(llmshare.inflight())
    if n: print(f'waiting for the {n} requests being answered to finish (up to 10 min) ...')
    if quiet(wait=600, still=3): print('quiet: nothing is being answered. Go ahead. When done: localmodels maintenance off')
    else: print(f'{len(llmshare.inflight())} requests are still being answered after 10 min; they finish on their own. When done: localmodels maintenance off')
    return 0


def door(verb):
    uid = f'gui/{os.getuid()}'
    if verb == 'start' and _loaded() and llmshare.hub_up(fresh=True): print(f'the door is open already on {llmshare.HUB}'); return 0
    if verb in ('stop', 'restart') and _loaded() and not quiet():
        print(f'the door is busy ({len(llmshare.inflight())} requests passing through for 90 s): left open; try again when it is quiet')
        return 3
    if verb == 'stop':
        r = _launchctl('bootout', f'{uid}/{LABEL}'); print('the door is closed until the next login (localmodels start)' if r.returncode == 0 else r.stderr.strip()); return r.returncode
    if not os.path.exists(PLIST): print('not installed: localmodels install'); return 1
    if verb == 'start': r = _launchctl('kickstart', f'{uid}/{LABEL}') if _loaded() else _launchctl('bootstrap', uid, PLIST)   # never -k: a running door is left alone
    else: r = _launchctl('kickstart', '-k', f'{uid}/{LABEL}') if _loaded() else _launchctl('bootstrap', uid, PLIST)
    up = any(llmshare.hub_up(fresh=True) or time.sleep(0.5) for _ in range(30))
    print(f"the door is {'open on ' + llmshare.HUB if up else 'not answering: see ' + os.path.join(LOGS, 'gateway.log')}")
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
    t('the door answers', llmshare.hub_up(fresh=True), llmshare.HUB)
    try:
        page = urllib.request.urlopen(llmshare.HUB + '/', timeout=5).read()
        snap = json.load(urllib.request.urlopen(llmshare.HUB + '/hub/snapshot', timeout=30))
        t('the dashboard answers', NAME.encode() in page and b'__HUB_NAME__' not in page and 'lmstudio' in snap, f'{llmshare.HUB}/ (localmodels open)')
    except (OSError, ValueError) as e: t('the dashboard answers', False, str(e))
    try: models = json.load(urllib.request.urlopen(llmshare.HUB + '/v1/models', timeout=5)).get('data', []); t('LM Studio answers through the door', True, f'{len(models)} models listed')
    except OSError as e: t('LM Studio answers through the door', False, str(e))
    t('the lms command is found', lms_found(), llmshare.LMS if lms_found() else f"not at {llmshare.LMS} and not on PATH: open LM Studio once and run ~/.lmstudio/bin/lms bootstrap, or set LMS; without it the hub keeps no copies and reloads nothing")
    rows = llmshare.loaded(fresh=True)
    ids = {r['id'] for r in rows}
    t('the model listing reads whole', llmshare.read_ok(), f"{len(rows)} loaded" if llmshare.read_ok() else 'lms ps failed or is unsure: the keeper waits for a good one')
    for base, f in (llmshare.settings().get('families') or {}).items():
        if base not in ids: continue                       # the keeper loads copies only while the first one is loaded
        want = llmshare.copy_ids(base, f.get('copies', 1))
        have = [c for c in want if c in ids]
        t(f'{base} has its copies', len(have) == len(want), f'{len(have)} of {len(want)} loaded, {llmshare.at_once(base)} answers at once')
    from . import peers
    for p in peers.view():
        t(f"the peer {p['name']} answers over the link", p['state'] == 'up', p['words'])
    if peers.serving():
        dl = door_link()
        t('the door listens for its peers', bool(dl and (dl.get('listening') or '').startswith('listening')),
          (dl or {}).get('listening') or 'the door does not say (it runs older code until localmodels install)')
    for name, good, note in run_tests():
        t(f'{name[5:-3]}: the sandbox tests pass', good, note)
    down = crashes.down()
    t('no model is down after a crash', not down, '; '.join(d['words'] for d in down) or 'localmodels crashes for the record')
    if dash.door()['stale']: print("  skip the keeper's look (the door runs older code until localmodels install)")
    else:
        seen = time.time() - (crashes._read(fresh=True).get('at') or 0)
        t("the keeper looks every 20 s (crashes, copies, recycles)", seen < 90, f'last look {seen:.0f} s ago')
    me = whois.identity(os.getpid())
    t('whois names this program', me.get('project') is not None, f"{me.get('project')} · {me.get('app')}")
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
        t('a request through the door is answered and logged', got and bool(mine),
          f"{mine[-1].get('project')} · {mine[-1].get('kind')} · {mine[-1].get('seconds')} s" + (f" (waited {mine[-1].get('waited')} s for a turn)" if (mine[-1].get('waited') or 0) > 2 else '')
          if mine else 'nothing logged')
    else: print('  skip a request through the door (no model loaded, the door closed, or every slot busy with a batch)')
    p = subprocess.run([PY, os.path.join(HOME, 'bin', 'localmodels'), 'mcp'], input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                       '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n', capture_output=True, text=True, timeout=30)
    names = []
    for line in p.stdout.split('\n'):
        try: names += [x['name'] for x in json.loads(line).get('result', {}).get('tools', [])]
        except (ValueError, AttributeError): pass
    t('the tools answer (MCP)', set(names) == {x['name'] for x in mcp_server.TOOLS}, ', '.join(names))
    t('the door runs this code', not dash.door()['stale'], 'localmodels install refreshes it' if dash.door()['stale'] else digest(HOME))
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
    if verb == 'name': return name_cmd(a[1:] if a[:1] == ['name'] else a)
    if verb in ('peer', 'peers'): return peer_cmd(a)
    if verb == 'notice':
        from . import maint; return maint.notice_main()
    if verb == 'crashes':
        d = num(a, '--days', 7.0); return crashes_cmd(d, '--scan' in a) if d is not None else 2
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
