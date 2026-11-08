"""dash - the live console of the local models: who is using them, by project and Claude Code session, every Claude Code
session on this Mac, and how busy the Mac is. Reads only.

  localmodels dash             live, every second, in the terminal (Ctrl-C to stop)
  localmodels dash --once      one look
  localmodels dash --json      machine output
  localmodels dash --no-color

LM STUDIO      its server, the hub's door, each loaded model: working or idle, how many at once, its memory, each
               copy's work since it was loaded against its recycle band, a copy down or being recycled
USING THEM     every program using a model now, by project: through the hub or straight to LM Studio, which Claude
               session started it, the step it is on, a batch's progress and what each of its agents is doing
CLAUDE CODE    every Claude Code session of the last three hours, by project: working or idle, subagents, workflows
LAST HOUR      the hub's requests by project: how many, tokens, time spent waiting for a turn
THE MAC        the programs using the most processor and memory; the bar: time, memory, load, free disk
"""
import glob, hashlib, json, os, re, shutil, subprocess, sys, time, urllib.request

from . import HOME, llmshare, maint, names, runs, sessions, whois

COLOR = sys.stdout.isatty() and '--no-color' not in sys.argv
C = dict(r='31', g='32', y='33', b='34', m='35', c='36', w='37', B='1', d='2', R='7')
INSTALLED = os.path.join(llmshare.SHARED, 'hub')          # the copy launchd runs the door from (localmodels install)
PROVIDERS = []                                            # (title, fn): a project's own rows, added to its own view
_cache, _files = {}, {}
ENV = dict(os.environ, LC_ALL='C')                        # ps and vm_stat print numbers the same way whatever the Mac's language


def k(text, *codes):
    return f"\033[{';'.join(C[x] for x in codes)}m{text}\033[0m" if COLOR else str(text)


def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
        return r.stdout + r.stderr
    except (OSError, subprocess.SubprocessError): return ''


def every(key, seconds, fn):
    """fn(), reused for `seconds` (the dash refreshes every second; some readings need not)."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds: return hit[1]
    val = fn(); _cache[key] = (time.time(), val)
    return val


def _file(path, parse):
    """A file's parsed contents, reread only when it changed."""
    try: m = os.path.getmtime(path)
    except OSError: return None
    hit = _files.get(path)
    if hit and hit[0] == m: return hit[1]
    try: val = parse(path)
    except (OSError, ValueError): val = None
    _files[path] = (m, val)
    if len(_files) > 4000:
        for p in sorted(_files, key=lambda p: _files[p][0])[:2000]: _files.pop(p, None)
    return val


def ago(t):
    s = max(0, int(time.time() - t))
    return f'{s}s' if s < 120 else f'{s // 60}m' if s < 7200 else f'{s // 3600}h'


def short(model):
    return re.sub(r'^[^/]+/', '', model or '?')


def add_provider(title, fn):
    """Another program adds its own rows to its own view: fn() -> [{kind, what, since, pct, left, by}]."""
    PROVIDERS.append((title, fn))


# ---------------------------------------------------------------- gather
def digest(folder):
    h = hashlib.sha1()
    for f in sorted(glob.glob(os.path.join(folder, 'localmodels', '*.py')) + glob.glob(os.path.join(folder, 'localmodels', '*.html'))):
        try: h.update(os.path.basename(f).encode()); h.update(open(f, 'rb').read())
        except OSError: pass
    return h.hexdigest()[:12]


def door():
    """The hub's door: up or not, and whether launchd runs the code that is in this repo now."""
    up = llmshare.hub_up(fresh=True)
    stale = every('stale', 10, lambda: os.path.isdir(INSTALLED) and digest(INSTALLED) != digest(HOME))
    return dict(up=up, port=llmshare.HUB_PORT, stale=bool(stale))


def lmstudio():
    def server():
        try: urllib.request.urlopen(llmshare.LMSTUDIO + '/v1/models', timeout=3); return 'on'
        except (TimeoutError, urllib.error.URLError) as e:
            if isinstance(getattr(e, 'reason', e), (TimeoutError, OSError)) and not isinstance(getattr(e, 'reason', None), ConnectionRefusedError):
                return (_cache.get('server') or (0, 'on'))[1]       # slow under load: as it was
            return 'off'
        except OSError: return 'off'
    models = llmshare.loaded()                                        # the listing the door keeps (read at most every 20 s)
    busy = every('busy', 1, llmshare.busy_slots)
    models = [dict(m, busy=busy.get(llmshare._safe(m['id']), 0)) for m in models]
    return dict(server=every('server', 3, server), models=models, families=families(models))


def family_words(f):
    """A loaded model in a few plain words: 'qwen/qwen3.6-35b-a3b as 2 copies, 8 at once (3 busy, 75.5 GB)'."""
    m = f['copies'][0]
    state = 'working' if f['busy'] or any(c['state'] in ('GENERATING', 'PROCESSING') for c in f['copies']) else m['state'].lower()
    if len(f['copies']) > 1: return f"{f['id']} as {len(f['copies'])} copies, {f['at_once']} at once ({state}, {f['busy']} busy, {f['size']:.1f} GB)"
    return f"{f['id']} {state} ({f['at_once']} at once, {f['busy']} busy, {m.get('size')})"


def families(models):
    """The loaded models grouped by family (a model loaded as two copies is one model):
    [{id, copies: [models], at_once, busy, size}], first copy first."""
    out = {}
    for m in models: out.setdefault(llmshare.family(m['id']), []).append(m)
    from .models import gb
    return [dict(id=f, copies=sorted(ms, key=lambda m: (m['id'] != f, len(m['id']), m['id'])), at_once=sum(m['parallel'] or 0 for m in ms),
                 busy=sum(m.get('busy', 0) for m in ms), size=sum(gb(m.get('size')) for m in ms)) for f, ms in out.items()]


def batch(log_dir):
    """A batch folder's progress (plan.json, one .running file per job at work, one .json per job done)."""
    if not log_dir or not os.path.isdir(log_dir): return None
    plan = _file(os.path.join(log_dir, 'plan.json'), lambda p: json.load(open(p))) or {}
    done = [f for f in glob.glob(os.path.join(log_dir, '*.json')) if os.path.basename(f) not in ('summary.json', 'plan.json', 'jobs.json', 'runner.json')]
    times = []
    for f in done:
        r = _file(f, lambda p: json.load(open(p)))
        if isinstance(r, dict): times.append(r.get('seconds') or 0)
    total = len(plan.get('jobs', [])) or None
    typical = sorted(times)[len(times) // 2] if times else None
    left = (total - len(done)) * (sum(times) / len(times)) / max(1, plan.get('parallel', 1)) if total and times else None
    now = []
    for r in glob.glob(os.path.join(log_dir, '*.running')):
        try:
            raw = open(r).read(); info = json.loads(raw) if raw.startswith('{') else dict(since=float(raw or time.time()))
        except (OSError, ValueError): info = {}
        since = info.get('since') or time.time()
        if (info.get('pid') and not llmshare._alive(info['pid'])) or time.time() - since > 4 * 3600: continue   # a runner that died left it
        pct = min(95, int(100 * (time.time() - since) / typical)) if typical else None
        now.append(dict(job=os.path.basename(r)[:-8], since=since, step=info.get('step'), pct=pct,
                        left=max(0, int(typical - (time.time() - since))) if typical else None))
    return dict(name=os.path.basename(log_dir.rstrip('/')), done=len(done), total=total, left=int(left) if left else None,
                pct=int(100 * len(done) / total) if total else None, now=sorted(now, key=lambda x: x['since']))


def live_runs():
    """The runs going on now (runs.py): from the last 3 hours of requests and those in flight, with each run name's last
    pace on this Mac (the last 7 days, reread every 10 minutes) for an estimate before the first unit is done."""
    rows = every('runrows', 5, lambda: llmshare.requests_log(3))
    hist = every('runhist', 600, lambda: runs.history(llmshare.requests_log(24 * 7)))
    return runs.progress(rows, llmshare.inflight(), history=hist)


def attach_runs(out):
    """Give each program its batch and its job: a run it named (X-Run) is the batch, and the phase it is on now (its step
    words, "check 10 of 50") the job; a phase read from step words alone is a job with no batch size known. The row's
    'since' becomes when the batch (or the job) began, not when the program first used a model."""
    live = sorted(live_runs().values(), key=lambda g: (g['kind'] != 'run', -g['started']))
    for r in out:
        r.setdefault('job', None)
        if r.get('batch'): continue
        mine = [g for g in live if g['project'] == r['project']]
        if not mine: continue
        g = mine[0]
        if g['kind'] == 'run': r['batch'], r['job'] = dict(g, now=[]), g.get('job')
        else: r['job'] = dict(g)
        r['since'] = g['started']


def users(shown=True):
    """Every program using a local model now, one row per program: [{pid, project, app, session, title, kind, models,
    since, answering, waiting, direct, via, step, batch}], busiest first."""
    rows = {}

    def row(pid, project=None):
        r = rows.get(pid)
        if r is None:
            r = rows[pid] = dict(pid=pid, project=project, app=None, session=None, host_session=None, kind=None, models=[], since=time.time(),
                                 answering=0, waiting=0, direct=0, via=None, step=None, batch=None, lease=False)
        return r
    for d in llmshare.leases():
        r = row(d.get('pid'), d.get('project'))
        r['project'] = r['project'] or d.get('project')
        if d.get('via') != 'hub' or not r['kind']:                # a run's own lease says what it is; the hub's only that it is
            r['kind'] = d.get('kind') if d.get('via') != 'hub' or d.get('kind') != 'other' else (r['kind'] or 'other')
        if d.get('via') != 'hub': r['lease'] = True
        r['via'] = r['via'] or ('hub' if d.get('via') == 'hub' else None)
        r['models'] += [m for m in d.get('models') or [] if m not in r['models']]
        r['since'] = min(r['since'], d.get('since') or time.time())
        for f in ('session', 'host_session', 'app', 'step'): r[f] = r[f] or d.get(f)
        if d.get('log_dir'): r['batch'] = batch(d['log_dir'])
        elif d.get('total'): r['batch'] = dict(name='', done=d.get('done', 0), total=d['total'], pct=int(100 * d.get('done', 0) / max(1, d['total'])), left=None, now=[])
    for f in llmshare.inflight():
        r = row(f.get('pid') or ('hub', f.get('project')), f.get('project'))
        r['via'] = 'hub'
        r['answering' if f.get('state') == 'answering' else 'waiting'] += 1
        if f.get('model') and f['model'] not in r['models']: r['models'].append(f['model'])
        for x in ('session', 'host_session', 'app', 'project', 'step'): r[x] = r[x] or f.get(x)
        r['since'] = min(r['since'], f.get('since') or time.time())
    lm = every('lmpid', 30, lambda: {p for p, (_, c) in _procs().items() if 'LM Studio' in c or '.lmstudio' in c})
    hubs = {f.get('hub_pid') for f in llmshare.inflight()} | {_hub_pid()}
    for pid, n in every('direct', 3, lambda: whois.clients_of(1234, skip=lm | hubs)).items():
        who = whois.identity(pid)
        r = row(pid, who.get('project'))
        r['direct'] = n
        for x in ('session', 'host_session', 'app'): r[x] = r[x] or who.get(x)
        r['project'] = r['project'] or who.get('project') or who.get('app')
    out = []
    for r in rows.values():
        if not r['project']: r['project'] = '?'
        r['title'] = sessions.title_of(r['session'], r['host_session'])
        out.append(r)
    models = lmstudio()['models']
    busy = [m['id'] for m in models if m['state'] in ('GENERATING', 'PROCESSING')]
    if busy and not any(r['answering'] or r['direct'] for r in out):
        _cache.setdefault('own', (time.time(), None))           # LM Studio says "generating" a moment after a request ends:
        if time.time() - _cache['own'][0] >= 3:                  # name its own chat window only after 3 s of it
            out.append(dict(pid=0, project="LM Studio's own chat", app='LM Studio', kind='chat', models=busy, since=_cache['own'][0],
                            answering=1, waiting=0, direct=0, via=None, step=None, batch=None, title=None, session=None, lease=False))
    else: _cache.pop('own', None)
    attach_runs(out)                                            # matched on the names as seen; then shown as the person named them
    if shown: out = [names.user(r) for r in out]               # shown=False: the names as seen (localmodels name lists them)
    return sorted(out, key=lambda r: (-(r['answering'] + r['direct'] + r['waiting'] > 0), r['since']))


def _procs():
    out = {}
    for line in sh(['ps', '-axo', 'pid=,ppid=,command=']).split('\n'):
        m = re.match(r'\s*(\d+)\s+(\d+)\s+(.*)', line)
        if m: out[int(m.group(1))] = (int(m.group(2)), m.group(3))
    return out


def _hub_pid():
    try: return json.load(urllib.request.urlopen(llmshare.HUB + '/hub/health', timeout=1)).get('pid')
    except (OSError, ValueError): return None


def claude(hours=3):
    return every('sessions', 3, lambda: [names.session(c) for c in sessions.recent(hours)])


def last_hour():
    return every('lasthour', 10, lambda: llmshare.by_project([names.request(r) for r in llmshare.requests_log(1)]))


NAMES = {'LM Studio': 'LM Studio (the local models)', 'WindowServer': 'the screen', 'kernel_task': 'macOS itself', 'claude': 'Claude Code',
         'python3': 'a script', 'Python': 'a script', 'Google Chrome': 'Chrome', 'Claude': 'the Claude app', 'mds_stores': 'Spotlight, indexing',
         'backupd': 'Time Machine', 'com.apple.WebKit': 'web pages', 'node': "node (LM Studio's models run here)", 'Safari': 'Safari'}


def top(n=6):
    merged = {}
    for line in sh(['ps', '-Arco', 'pcpu,rss,comm']).split('\n')[1:]:
        p = line.split(None, 2)
        if len(p) < 3: continue
        name = os.path.basename(p[2])
        nice = next((v for kk, v in NAMES.items() if name.startswith(kk)), name)
        m = merged.setdefault(nice, dict(name=nice, cpu=0.0, mem=0.0))
        try: m['cpu'] += float(p[0].replace(',', '.')); m['mem'] += int(p[1]) / 2**20
        except ValueError: continue
    return sorted(merged.values(), key=lambda r: -r['cpu'])[:n]


def mac():
    total = every('memsize', 86400, lambda: int(sh(['sysctl', '-n', 'hw.memsize']).strip() or 0))
    vm = sh(['vm_stat']); page = int((re.search(r'page size of (\d+)', vm) or [0, 16384])[1])
    get = lambda kk: int((re.search(rf'{kk}:\s+(\d+)', vm) or [0, 0])[1])
    used = (get('Anonymous pages') - get('Pages purgeable') + get('Pages wired down') + get('Pages occupied by compressor')) * page   # as Activity Monitor's "Memory Used"
    load = sh(['sysctl', '-n', 'vm.loadavg']).strip('{} \n').split()[:3]
    try: st = os.statvfs('/System/Volumes/Data'); free = st.f_bavail * st.f_frsize / 1e9
    except OSError: free = 0
    return dict(used=used / 2**30, total=total / 2**30, load=load, free=free)


def snapshot():
    extra = {}
    for title, fn in PROVIDERS:
        try: extra[title] = fn()
        except Exception as e: extra[title] = [dict(kind='note', what=f'could not read: {e}', since=time.time())]
    return dict(time=time.strftime('%H:%M:%S'), door=door(), lmstudio=lmstudio(), users=users(), claude=claude(),
                last_hour=last_hour(), top=every('top', 5, top), mac=mac(), extra=extra, crashes=every('crashes', 5, crash_view),
                maintenance=maint.view())


def crash_view():
    """Models down after a crash now, the last crash of the day in a line, and the recycle in progress (crashes.py, recycle.py)."""
    from . import crashes, recycle
    down = crashes.down()
    day = [x for x in crashes.history(1) if not any(d['family'] == x['family'] for d in down)]
    j = recycle.job()
    return dict(down=down, last=crashes.words(day[0]) if day else None, brief=crashes.brief(day[0]) if day else None, recycling=recycle.words(j) if j else None)


def kind_words(r):
    if r.get('kind') == 'batch': return 'batch'
    if r.get('kind') in ('eval', 'test'): return 'test'
    if r.get('kind') in ('chat', 'ask'): return r['kind']
    return 'requests'


def running(s=None):
    """What is running, in rows for another program's own page: Claude Code sessions (kind
    claude) and every program using a local model, with a batch's agents under it."""
    s = s or snapshot()
    rows, work = [], [c for c in s['claude'] if c['working']]
    for c in work:
        extra = (f" · {len(c['subagents'])} subagent{'s' if len(c['subagents']) != 1 else ''}" if c['subagents'] else '') + \
                ''.join(f" · workflow {w['finished']} of {w['agents']} done" for w in c['workflows'] if w['live'] and w['running'])
        rows.append(dict(kind='claude', what=f"{c['project']} · {c['title'][:40]}{extra}", since=c['last'], pct=None, left=None))
    if not work:
        rows.append(dict(kind='claude', what=f"Claude Code: idle ({len(s['claude'])} session{'s' if len(s['claude']) != 1 else ''} in the last 3 hours)",
                         since=time.time(), pct=None, left=None, idle=True))
    for r in s['users']:
        b = r.get('batch') or {}
        state = 'straight to LM Studio' if r['direct'] and not r['answering'] else \
            f"{r['answering']} answering" + (f", {r['waiting']} waiting" if r['waiting'] else '') if r['answering'] or r['waiting'] else 'between requests'
        what = f"{r['project']}: {kind_words(r)} on {', '.join(short(m) for m in r['models'][:2]) or 'a model'} · {state}" + (f" · {r['step']}" if r.get('step') else '')
        rows.append(dict(kind=r.get('kind') if r.get('kind') in ('batch', 'chat') else 'local', what=what, since=r['since'],
                         pct=b.get('pct'), left=b.get('left'), by='claude' if r.get('session') or r.get('host_session') else None))
        for j in b.get('now', []):
            rows.append(dict(kind='agent', what=j['job'] + (f" · {j['step']}" if j.get('step') else ''), since=j['since'], pct=j['pct'], left=j['left'],
                             by='claude' if r.get('session') else None))
    for title, xs in (s.get('extra') or {}).items(): rows += xs
    return rows


# ---------------------------------------------------------------- draw
ANSI = re.compile(r'\033\[[0-9;]*m')


def fit(line, w):
    """Cut a colored line to w visible characters."""
    out, seen, i = [], 0, 0
    while i < len(line) and seen < w:
        m = ANSI.match(line, i)
        if m: out.append(m.group(0)); i = m.end(); continue
        out.append(line[i]); seen += 1; i += 1
    return ''.join(out) + ('\033[0m' if COLOR else '')


def _left(sec):
    sec = int(sec)
    if sec < 90: return f'{sec}s left'
    if sec < 5400: return f'{round(sec / 60)}m left'
    if sec < 172800: return f'{sec // 3600}h{round(sec % 3600 / 60):02d}m left'
    return f'{sec // 86400}d{round(sec % 86400 / 3600)}h left'


def prog(pct, left, since):
    if pct is None: return k(f'{ago(since)} so far', 'd')
    fill = pct // 17
    return k('█' * fill, 'c') + k('░' * (6 - fill), 'd') + f' {pct:>2}%' + (f' ~{_left(left)}' if left else ' estimating' if pct < 100 else ' finishing')


def show(s, w=80, h=24):
    rule = lambda title: k('──[ ', 'b') + k(title, 'B', 'c') + k(' ]' + '─' * max(0, w - len(title) - 6), 'b')
    sections = []
    # LM Studio and the door
    lm, dr = s['lmstudio'], s['door']
    srv = k('on', 'g', 'B') if lm['server'] == 'on' else k('off', 'r', 'B')
    hub = (k('open', 'g', 'B') if dr['up'] else k('closed', 'r', 'B')) + f" on :{dr['port']}" + \
          (k(' · runs older code: localmodels install', 'y') if dr['up'] and dr['stale'] else '')
    L = [f"  server {srv} · the hub's door {hub}"]
    if s.get('maintenance'): L.append('  ' + k('⚠ ' + s['maintenance']['words'][:w - 6], 'r' if s['maintenance']['phase'] == 'on' else 'y', 'B'))
    word = lambda m: k('working', 'y', 'B') if m.get('busy') or m['state'] in ('GENERATING', 'PROCESSING') else k('idle', 'g') if m['state'] == 'IDLE' else k(m['state'].lower(), 'y')
    for f in lm['families']:
        if len(f['copies']) == 1:
            m = f['copies'][0]
            L.append(f"  {k(m['id'], 'B')} is {word(m)} · {m['busy']} of {m['parallel']} busy · holds {m.get('size') or '?'}")
            continue
        L.append(f"  {k(f['id'], 'B')} · {len(f['copies'])} copies · {f['busy']} of {f['at_once']} busy · holds {f['size']:.1f} GB")
        L.append('    ' + '   '.join(f"copy {i} {word(m)} · {m['busy']} of {m['parallel']} busy" for i, m in enumerate(f['copies'], 1)))
    if not lm['models']: L.append('  ' + k('no model loaded', 'd'))
    cr = s.get('crashes') or {}
    for d in cr.get('down') or []: L.append('  ' + k('! ' + d['words'], 'r' if d['state'] == 'left down' else 'y', 'B'))
    if cr.get('recycling'): L.append('  ' + k(cr['recycling'][:w - 4], 'y'))
    if cr.get('brief'): L.append('  ' + k('last crash: ' + cr['brief'], 'd'))
    sections.append(['LM Studio', L, 2 + len(cr.get('down') or []) + (1 if s.get('maintenance') else 0)])
    # who is using the models
    L = []
    for r in s['users']:
        b = r.get('batch') or {}
        if r['answering'] or r['waiting']:
            state = k(f"{r['answering']} answering", 'y', 'B') + (k(f" · {r['waiting']} waiting", 'y') if r['waiting'] else '')
        elif r['direct']: state = k(f"{r['direct']} connected", 'y')
        else: state = k('between requests', 'd')
        lines = []                                  # the batch and the job, each a bar with its time left and finish
        for word, p in (('batch', b if b.get('total') else None), ('job', r.get('job'))):
            if not p or not p.get('total'): continue
            fin = k(f" → {time.strftime('%H:%M', time.localtime(p['finish']))}", 'c') if p.get('finish') else ''
            lines.append(f"    {k(word.ljust(5), 'B')} {prog(p.get('pct'), p.get('left'), r['since'])} {p['done']}/{p['total']}{fin} {k((p.get('name') or '')[:24], 'd')}")
        who = k('← Claude', 'm', 'B') + (f" · {r['title']}" if r.get('title') else '') if (r.get('session') or r.get('host_session')) else \
            k(f"← {r['app']}", 'd') if r.get('app') else ''
        flag = k(' ⚠ straight to LM Studio', 'r') if r['direct'] and r.get('via') != 'hub' else ''
        step = k(f" · {r['step'][:30]}", 'd') if r.get('step') and not lines else ''
        L.append(f"  {k(r['project'][:14].ljust(14), 'B')} {kind_words(r):<9} {state}{step}{flag} {k(ago(r['since']), 'd')} {who}")
        L += lines
        for j in b.get('now', [])[:3]:
            L.append(f"    {k('agent', 'y')} {(j['job'] + (' · ' + j['step'] if j.get('step') else ''))[:44]:<44} {prog(j['pct'], j['left'], j['since'])}")
    if not L: L.append('  ' + k('nothing is using a local model', 'd'))
    sections.append(['using the models now', L, 1])
    # Claude Code, every project
    cs = s['claude']
    work = [c for c in cs if c['working']]
    uses = {r.get('session') for r in s['users'] if r.get('session')}
    L = [f"  {k(str(len(work)) + ' working', 'm', 'B') if work else k('none working', 'd')}"
         f" in {len({c['project'] for c in work})} project{'s' if len({c['project'] for c in work}) != 1 else ''}"
         f" · {len(cs) - len(work)} idle · last 3 hours"]
    for c in cs[:8]:
        st = k('working'.ljust(9), 'm', 'B') if c['working'] else k(f"idle {ago(c['last'])}".ljust(9), 'd')
        bits = (f" · {len(c['subagents'])} subagent{'s' if len(c['subagents']) != 1 else ''}" if c['subagents'] else '') + \
               ''.join(f" · workflow {x['finished']}/{x['agents']}" for x in c['workflows'] if x['live'] and x['running']) + \
               (k(' · uses a local model', 'y') if c['id'] in uses else '')
        L.append(f"  {k(c['project'][:14].ljust(14), 'B')} {st} {c['title'][:36]}{bits}")
    sections.append(['claude code', L, 1])
    # another program's own rows
    for title, xs in (s.get('extra') or {}).items():
        if not xs: continue
        L = [f"  {k(x.get('kind', '').ljust(6), 'c', 'B')} {x['what'][:40]:<40} {prog(x.get('pct'), x.get('left'), x.get('since') or time.time())}"
             + (k(f" ← {x['by']}", 'm' if x.get('by') == 'claude' else 'd') if x.get('by') else '') for x in xs]
        sections.append([title, L, 1])
    # the last hour, through the hub
    lh = s['last_hour']
    if lh:
        parts = [f"{k(p, 'B')} {v['requests']} req · {v['tokens'] / 1000:.0f}k tok" + (f" · waited {v['waited'] / 60:.1f}m" if v['waited'] > 30 else '')
                 for p, v in sorted(lh.items(), key=lambda x: -x[1]['requests'])]
        sections.append(['last hour, through the hub', ['  ' + ' │ '.join(parts)], 1])
    # the Mac
    L = []
    for t in s['top'][:3]:
        hot = 'r' if t['cpu'] > 200 else 'y' if t['cpu'] > 50 else 'g'
        L.append(f"  {k(format(t['cpu'], '6.0f') + '%', hot)} cpu  {t['mem']:6.1f} GB  {t['name']}")
    sections.append(['the mac', L, 1])
    # fit h lines: trim the longest sections first, never below their least
    room = h - 2 - len(sections)
    while sum(len(x[1]) for x in sections) > room:
        big = max(sections, key=lambda x: len(x[1]) - x[2])
        if len(big[1]) <= big[2]: break
        big[1] = big[1][:-1]
    body = [k(' local models ', 'R', 'B', 'm') + k(f"  what's running · {s['time']}", 'B')]
    for title, lines, _ in sections: body += [rule(title)] + lines
    body = [fit(l, w) for l in body][:h - 1]
    body += [''] * (h - 1 - len(body))
    m = s['mac']
    share = m['used'] / max(1, m['total'])
    memc = 'r' if share > 0.85 else 'y' if share > 0.65 else 'g'
    busy = sum(r['answering'] + r['direct'] for r in s['users'])
    status = (f" {s['time']} │ mem {m['used']:.0f}/{m['total']:.0f}G │ load {m['load'][0] if m['load'] else '?'} │ {m['free']:.0f}G free │ "
              f"hub {'open' if dr['up'] else 'closed'} │ {busy} working ")
    return '\n'.join(body + [k(status[:w].ljust(w), 'R', memc)])


def main(argv=(), watch=()):
    a = list(argv)
    if '--json' in a: print(json.dumps(snapshot(), indent=1, default=str)); return 0
    size = shutil.get_terminal_size((80, 24))
    w, h = max(60, size.columns), max(16, size.lines)
    if '--once' not in a and sys.stdout.isatty():
        files = [os.path.abspath(f) for f in watch] + glob.glob(os.path.join(HOME, 'localmodels', '*.py'))
        born = {f: os.path.getmtime(f) for f in files if os.path.exists(f)}
        try:
            while True:
                if any(os.path.exists(f) and os.path.getmtime(f) != t for f, t in born.items()):
                    os.execv(sys.executable, [sys.executable] + sys.argv)   # its code changed: start again with the new code
                size = shutil.get_terminal_size((80, 24)); w, h = max(60, size.columns), max(16, size.lines)
                frame = show(snapshot(), w, h)
                sys.stdout.write('\033[H\033[2J' + frame); sys.stdout.flush(); time.sleep(1)
        except KeyboardInterrupt: return 0
    print(show(snapshot(), w, h))
    return 0
