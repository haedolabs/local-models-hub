"""dash - the live console of the local models: who is using them, by project and Claude Code session, every Claude Code
session on this Mac, and how busy the Mac is. Reads only.

  localmodels dash             live, every second, in the terminal (Ctrl-C to stop)
  localmodels dash --once      one look
  localmodels dash --json      machine output
  localmodels dash --no-color

LM STUDIO      server state, gateway state, each loaded instance: generating or idle, slots, memory, tokens processed
               since load against the recycle threshold, instances down or being recycled, peer replicas
CLIENTS        every process with a request in flight or a lease held, by project: via the gateway or direct to LM
               Studio, its Claude Code session, current step, batch and job progress, local agent jobs
CLAUDE CODE    every Claude Code session of the last three hours, by project: working or idle, subagents, workflows
LAST HOUR      requests through the gateway by project: count, tokens, time queued
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
def digest(folder, pages=True):
    """The hub's code in a folder, as 12 hex digits; pages=False leaves out the dashboard page (read per request)."""
    h = hashlib.sha1()
    for f in sorted(glob.glob(os.path.join(folder, 'localmodels', '*.py')) + (glob.glob(os.path.join(folder, 'localmodels', '*.html')) if pages else [])):
        try: h.update(os.path.basename(f).encode()); h.update(open(f, 'rb').read())
        except OSError: pass
    return h.hexdigest()[:12]


def door():
    """The hub's door: up or not, and whether launchd runs the code that is in this repo now."""
    up = llmshare.hub_up(fresh=True)
    runs = (llmshare._hub.get('health') or {}).get('code') if up else None      # the code the gateway loaded, by its own digest
    stale = every('stale', 10, lambda: bool(runs and runs != digest(HOME, pages=False)) or (os.path.isdir(INSTALLED) and digest(INSTALLED) != digest(HOME)))
    return dict(up=up, port=llmshare.HUB_PORT, stale=bool(stale), write_problem=(llmshare._hub.get('health') or {}).get('write_problem') if up else None)


def lmstudio():
    def server():
        try: llmshare.urlopen(llmshare.LMSTUDIO + '/v1/models', timeout=3); return 'on'
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
    state = 'generating' if f['busy'] or any(c['state'] in ('GENERATING', 'PROCESSING') for c in f['copies']) else m['state'].lower()
    if len(f['copies']) > 1: return f"{f['id']}: {len(f['copies'])} replicas, {f['at_once']} slots ({state}, {f['busy']} active, {f['size']:.1f} GB)"
    return f"{f['id']}: 1 instance, {f['at_once']} slots ({state}, {f['busy']} active, {m.get('size')})"


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
    plan = _file(os.path.join(log_dir, 'plan.json'), lambda p: json.load(open(p)))
    plan = plan if isinstance(plan, dict) else {}         # a client's file: a shape it did not mean is no plan, never an exception
    done = [f for f in glob.glob(os.path.join(log_dir, '*.json')) if os.path.basename(f) not in ('summary.json', 'plan.json', 'jobs.json', 'runner.json')]
    times = []
    for f in done:
        r = _file(f, lambda p: json.load(open(p)))
        if isinstance(r, dict): times.append(r.get('seconds') or 0)
    jobs = plan.get('jobs')
    total = len(jobs) if isinstance(jobs, list) and jobs else None
    typical = sorted(times)[len(times) // 2] if times else None
    running = glob.glob(os.path.join(log_dir, '*.running'))
    parallel = plan.get('parallel')                        # a number, or a word ("hub slots"): then the jobs running now stand for it
    parallel = parallel if isinstance(parallel, int) and not isinstance(parallel, bool) and parallel > 0 else max(1, len(running))
    left = (total - len(done)) * (sum(times) / len(times)) / parallel if total and times and total > len(done) else None
    now = []
    for r in running:
        try:
            raw = open(r).read(); info = json.loads(raw) if raw.startswith('{') else dict(since=float(raw or time.time()))
        except (OSError, ValueError): info = {}
        since = info.get('since') or time.time()
        if (info.get('pid') and not llmshare._alive(info['pid'])) or time.time() - since > 4 * 3600: continue   # a runner that died left it
        pct = min(95, int(100 * (time.time() - since) / typical)) if typical else None
        now.append(dict(job=os.path.basename(r)[:-8], since=since, step=info.get('step'), pct=pct,
                        left=max(0, int(typical - (time.time() - since))) if typical else None))
    return dict(name=os.path.basename(log_dir.rstrip('/')), done=len(done), total=total, left=int(left) if left else None,
                finish=time.time() + int(left) if left else None,     # as runs.py gives it: every progress with a time left has its finish
                pct=int(100 * len(done) / total) if total else None, now=sorted(now, key=lambda x: x['since']))


def live_runs():
    """The runs going on now (runs.py): from the last 3 hours of requests and those in flight, with each run name's last
    pace and each phase's tokens per item on this Mac (the last 7 days, reread every 10 minutes) for an estimate before
    the first unit is done, and the runs' declared plans (POST /hub/runs)."""
    rows = every('runrows', 5, lambda: llmshare.requests_log(3))
    week = lambda: llmshare.requests_log(24 * 7)
    hist = every('runhist', 600, lambda: (lambda w: (runs.history(w), runs.costs(w)))(week()))
    return runs.progress(rows, llmshare.inflight(), history=hist[0], plans=every('runplans', 5, runs.plans), costs=hist[1])


def attach_runs(out):
    """Give each program its batch and its job: a run it named (X-Run) is the batch, and the phase it is on now (its step
    words, "check 10 of 50") the job; a phase read from step words alone is a job with no batch size known. The row's
    'since' becomes when the batch (or the job) began, not when the program first used a model. Progress that cannot be
    read is recorded and leaves the rows as they are: it never takes the client list with it."""
    try: live = sorted(live_runs().values(), key=lambda g: (g['kind'] != 'run', -g['started']))
    except Exception as e:
        from . import errors; errors.record('dash: run progress', e); return
    for r in out:
        r.setdefault('job', None)
        mine = [g for g in live if g['project'] == r['project']]
        run = next((g for g in mine if g['kind'] == 'run'), None)
        if run:                                  # the named run (X-Run) is the batch; counts a step's own lease carries are its job
            own = r.get('batch') if r.get('batch') and not r['batch'].get('name') else None
            r['batch'] = dict(run, now=(own or {}).get('now') or [])
            r['job'] = run.get('job') or (dict(own, name=own.get('phase') or None) if own else None)
            r['since'] = run['started']
        elif not r.get('batch') and mine:
            r['job'], r['since'] = dict(mine[0]), mine[0]['started']


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
    for d in llmshare.leases():                      # a front's requests over the link: one row per front and project
        r = row(('link', d['from'], d.get('project')) if d.get('from') else d.get('pid'), d.get('project'))
        r['project'] = r['project'] or d.get('project')
        if d.get('via') != 'hub' or not r['kind']:                # a run's own lease says what it is; the hub's only that it is
            r['kind'] = d.get('kind') if d.get('via') != 'hub' or d.get('kind') != 'other' else (r['kind'] or 'other')
        if d.get('via') != 'hub': r['lease'] = True
        r['via'] = r['via'] or ('hub' if d.get('via') == 'hub' else None)
        r['models'] += [m for m in d.get('models') or [] if m not in r['models']]
        r['since'] = min(r['since'], d.get('since') or time.time())
        for f in ('session', 'host_session', 'app', 'step'): r[f] = r[f] or d.get(f)
        if d.get('log_dir'):
            try: r['batch'] = batch(d['log_dir'])          # a client's folder: progress that cannot be read is recorded, the row stays
            except Exception as e:
                from . import errors; errors.record('dash: batch progress', e)
        elif d.get('total'): r['batch'] = dict(name='', done=d.get('done', 0), total=d['total'], pct=int(100 * d.get('done', 0) / max(1, d['total'])), left=None, now=[])
    for f in llmshare.inflight():
        r = row(('link', f['from'], f.get('project')) if f.get('from') else f.get('pid') or ('hub', f.get('project')), f.get('project'))
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
    try: return json.load(llmshare.urlopen(llmshare.HUB + '/hub/health', timeout=1)).get('pid')
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


def memory():
    """This host's memory in GiB: (used, total), used as Activity Monitor's "Memory Used". A peer reports the same figure."""
    total = every('memsize', 86400, lambda: int(sh(['sysctl', '-n', 'hw.memsize']).strip() or 0))
    vm = sh(['vm_stat']); page = int((re.search(r'page size of (\d+)', vm) or [0, 16384])[1])
    get = lambda kk: int((re.search(rf'{kk}:\s+(\d+)', vm) or [0, 0])[1])
    used = (get('Anonymous pages') - get('Pages purgeable') + get('Pages wired down') + get('Pages occupied by compressor')) * page
    return used / 2**30, total / 2**30


_cpu = {'lib': None, 'prev': None}


def _cpu_ticks():
    """Per-core CPU ticks (user, system, idle, nice) from host_processor_info, or None where it is not available."""
    import ctypes, ctypes.util
    if _cpu['lib'] is None:
        lib = ctypes.CDLL(ctypes.util.find_library('c'))
        lib.mach_host_self.restype = ctypes.c_uint
        _cpu['lib'] = (lib, ctypes.c_uint.in_dll(lib, 'mach_task_self_'))
    lib, task = _cpu['lib']
    n, info, cnt = ctypes.c_uint(), ctypes.POINTER(ctypes.c_int)(), ctypes.c_uint()
    if lib.host_processor_info(lib.mach_host_self(), 2, ctypes.byref(n), ctypes.byref(info), ctypes.byref(cnt)) != 0: return None
    try: return [tuple(info[i * 4 + k] & 0xffffffff for k in range(4)) for i in range(n.value)]
    finally: lib.vm_deallocate(task, ctypes.cast(info, ctypes.c_void_p), ctypes.c_size_t(cnt.value * 4))   # the kernel's copy, returned


def soc():
    """This Mac's chip at work: {name, cpu: [% busy per core], cpu_pct, gpu_pct, gpu_cores, gpu_mem_gb}. Core load is the
    share of non-idle ticks since the last reading (a first reading samples 0.25 s); the GPU's from IOAccelerator."""
    now, cur = time.time(), _cpu_ticks()
    prev = _cpu['prev']
    if cur and (not prev or now - prev[0] > 30): time.sleep(0.25); prev, cur = (now, cur), _cpu_ticks()
    cores = None
    if cur and prev and len(prev[1]) == len(cur):
        cores = []
        for a, b in zip(prev[1], cur):
            d = [(y - x) & 0xffffffff for x, y in zip(a, b)]
            cores.append(round(100 * (d[0] + d[1] + d[3]) / max(1, sum(d))))
    if cur: _cpu['prev'] = (time.time(), cur)
    io = sh(['ioreg', '-r', '-d', '1', '-w', '0', '-c', 'IOAccelerator'])
    num = lambda k: int((re.search(rf'"{re.escape(k)}"\s*=\s*(\d+)', io) or [0, -1])[1])
    gpu, mem = num('Device Utilization %'), num('In use system memory')
    gcores = every('gpu-cores', 86400, lambda: num('gpu-core-count'))
    return dict(name=every('cpu-name', 86400, lambda: sh(['sysctl', '-n', 'machdep.cpu.brand_string']).strip()),
                cpu=cores, cpu_pct=round(sum(cores) / len(cores)) if cores else None,
                gpu_pct=gpu if gpu >= 0 else None, gpu_cores=gcores if gcores > 0 else None,
                gpu_mem_gb=round(mem / 2**30, 1) if mem >= 0 else None)


def mac():
    used, total = memory()
    load = sh(['sysctl', '-n', 'vm.loadavg']).strip('{} \n').split()[:3]
    try: st = os.statvfs('/System/Volumes/Data'); free = st.f_bavail * st.f_frsize / 1e9
    except OSError: free = 0
    return dict(used=used, total=total, load=load, free=free)


def pool(m=None, ps=None):
    """Memory across this Mac and its peers that are up and report it: {used, total, hosts: [{name, used, total}]}, this
    Mac first; one host when there are no peers. m: mac() if already read; ps: peers.view() if already read."""
    from . import peers
    used, total = (m['used'], m['total']) if m else memory()
    hosts = [dict(name=peers.me(), used=round(used, 1), total=round(total, 1))]
    for p in (peers.view() if ps is None else ps) or []:
        mm = p.get('memory') if isinstance(p.get('memory'), dict) else {}
        if p.get('state') in ('up', 'maintenance') and mm.get('total'):
            hosts.append(dict(name=p.get('name'), used=round(float(mm.get('used') or 0), 1), total=round(float(mm['total']), 1)))
    return dict(used=round(sum(h['used'] for h in hosts), 1), total=round(sum(h['total'] for h in hosts), 1), hosts=hosts)


def snapshot():
    extra = {}
    for title, fn in PROVIDERS:
        try: extra[title] = fn()
        except Exception as e:
            extra[title] = [dict(kind='note', what=f'could not read: {e}', since=time.time())]
            from . import errors; errors.record(f'dash: {title}', e)
    from . import peers
    return dict(time=time.strftime('%H:%M:%S'), door=door(), lmstudio=lmstudio(), users=users(), claude=claude(),
                last_hour=last_hour(), top=every('top', 5, top), mac=mac(), extra=extra, crashes=every('crashes', 5, crash_view),
                maintenance=maint.view(), peers=peers.view(), pool=pool())


def crash_view():
    """Models down after a crash now, the last crash of the day in a line, and the recycle in progress (crashes.py, recycle.py)."""
    from . import crashes, recycle
    down = crashes.down()
    day = [x for x in crashes.history(1) if not any(d['family'] == x['family'] for d in down)]
    j = recycle.job()
    return dict(down=down, last=crashes.words(day[0]) if day else None, brief=crashes.brief(day[0]) if day else None, recycling=recycle.words(j) if j else None)


def kind_words(r):
    return r.get('kind') or 'other'


def running(s=None):
    """What is running, in rows for another program's own page: Claude Code sessions (kind
    claude) and every program using a local model, with a batch's agents under it."""
    s = s or snapshot()
    rows, work = [], [c for c in s['claude'] if c['working']]
    for c in work:
        extra = (f" · {c['subagents']} subagent{'s' if c['subagents'] != 1 else ''}" if c['subagents'] else '') + \
                ''.join(f" · workflow {w['finished']} of {w['agents']} done" for w in c['workflows'] if w['live'] and w['running'])
        rows.append(dict(kind='claude', what=f"{c['project']} · {c['title'][:40]}{extra}", since=c['last'], pct=None, left=None))
    if not work:
        rows.append(dict(kind='claude', what=f"Claude Code: idle ({len(s['claude'])} session{'s' if len(s['claude']) != 1 else ''} in the last 3 hours)",
                         since=time.time(), pct=None, left=None, idle=True))
    for r in s['users']:
        b = r.get('batch') or {}
        state = 'direct to LM Studio' if r['direct'] and not r['answering'] else \
            f"{r['answering']} active" + (f", {r['waiting']} queued" if r['waiting'] else '') if r['answering'] or r['waiting'] else 'idle (lease held)'
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
    srv = k('up', 'g', 'B') if lm['server'] == 'on' else k('down', 'r', 'B')
    hub = (k('listening', 'g', 'B') if dr['up'] else k('down', 'r', 'B')) + f" on :{dr['port']}" + \
          (k(' · stale code: localmodels install', 'y') if dr['up'] and dr['stale'] else '')
    L = [f"  LM Studio server {srv} · gateway {hub}"]
    if s.get('maintenance'): L.append('  ' + k('⚠ ' + s['maintenance']['words'][:w - 6], 'r' if s['maintenance']['phase'] == 'on' else 'y', 'B'))
    word = lambda m: k('generating', 'y', 'B') if m.get('busy') or m['state'] in ('GENERATING', 'PROCESSING') else k('idle', 'g') if m['state'] == 'IDLE' else k(m['state'].lower(), 'y')
    for f in lm['families']:
        if len(f['copies']) == 1:
            m = f['copies'][0]
            L.append(f"  {k(m['id'], 'B')}: {word(m)} · {m['busy']}/{m['parallel']} slots active · {m.get('size') or '?'}")
            continue
        L.append(f"  {k(f['id'], 'B')} · {len(f['copies'])} replicas · {f['busy']}/{f['at_once']} slots active · {f['size']:.1f} GB")
        L.append('    ' + '   '.join(f"replica {i} {word(m)} · {m['busy']}/{m['parallel']} active" for i, m in enumerate(f['copies'], 1)))
    for p in s.get('peers') or []:                 # each peer: its instances are replicas of a family, after the local ones
        if p.get('state') != 'up': L.append('  ' + k((p.get('words') or p['name'])[:w - 4], 'r' if p.get('state') in ('down', 'refused') else 'y')); continue
        for fid, f in (p.get('families') or {}).items():
            if not f.get('match'): L.append('  ' + k(f"peer {p['name']}: {fid} excluded ({f.get('why')})"[:w - 4], 'y')); continue
            L.append(f"  {k(fid, 'B')} @ peer {k(p['name'], 'B')} · {f.get('busy')}/{f.get('slots')} slots busy · {f.get('here')} from this gateway"
                     + (f" · {p['rtt_ms']:.0f} ms RTT" if p.get('rtt_ms') is not None else ''))
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
            state = k(f"{r['answering']} active", 'y', 'B') + (k(f" · {r['waiting']} queued", 'y') if r['waiting'] else '')
        elif r['direct']: state = k(f"{r['direct']} direct connections", 'y')
        else: state = k('idle (lease held)', 'd')
        lines = []                                  # the batch and the job, each a bar with its time left and finish
        for word, p in (('batch', b if b.get('total') else None), ('job', r.get('job'))):
            if not p or not p.get('total'): continue
            fin = k(f" → {time.strftime('%H:%M', time.localtime(p['finish']))}", 'c') if p.get('finish') else ''
            lines.append(f"    {k(word.ljust(5), 'B')} {prog(p.get('pct'), p.get('left'), r['since'])} {p['done']}/{p['total']}{fin} {k((p.get('name') or '')[:24], 'd')}")
        who = k('← Claude', 'm', 'B') + (f" · {r['title']}" if r.get('title') else '') if (r.get('session') or r.get('host_session')) else \
            k(f"← {r['app']}", 'd') if r.get('app') else ''
        flag = k(' ⚠ direct to LM Studio', 'r') if r['direct'] and r.get('via') != 'hub' else ''
        step = k(f" · {r['step'][:30]}", 'd') if r.get('step') and not lines else ''
        L.append(f"  {k(r['project'][:14].ljust(14), 'B')} {kind_words(r):<9} {state}{step}{flag} {k(ago(r['since']), 'd')} {who}")
        L += lines
        for j in b.get('now', [])[:3]:
            L.append(f"    {k('agent', 'y')} {(j['job'] + (' · ' + j['step'] if j.get('step') else ''))[:44]:<44} {prog(j['pct'], j['left'], j['since'])}")
    if not L: L.append('  ' + k('no active clients', 'd'))
    sections.append(['clients', L, 1])
    # Claude Code, every project
    cs = s['claude']
    work = [c for c in cs if c['working']]
    uses = {r.get('session') for r in s['users'] if r.get('session')}
    L = [f"  {k(str(len(work)) + ' working', 'm', 'B') if work else k('none working', 'd')}"
         f" in {len({c['project'] for c in work})} project{'s' if len({c['project'] for c in work}) != 1 else ''}"
         f" · {len(cs) - len(work)} idle · last 3 hours"]
    for c in cs[:8]:
        st = k('working'.ljust(9), 'm', 'B') if c['working'] else k(f"idle {ago(c['last'])}".ljust(9), 'd')
        bits = (f" · {c['subagents']} subagent{'s' if c['subagents'] != 1 else ''}" if c['subagents'] else '') + \
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
        sections.append(['last hour, via the gateway', ['  ' + ' │ '.join(parts)], 1])
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
    m, pl = s['mac'], s.get('pool') or dict(used=s['mac']['used'], total=s['mac']['total'], hosts=[s['mac']])
    share = max(h['used'] / max(1, h['total']) for h in pl['hosts'])           # the fullest host, not the pool's average
    memc = 'r' if share > 0.85 else 'y' if share > 0.65 else 'g'
    busy = sum(r['answering'] + r['direct'] for r in s['users'])
    status = (f" {s['time']} │ mem {pl['used']:.0f}/{pl['total']:.0f}G" + (f" ({len(pl['hosts'])} hosts)" if len(pl['hosts']) > 1 else '')
              + f" │ load {m['load'][0] if m['load'] else '?'} │ {m['free']:.0f}G free │ gateway {'up' if dr['up'] else 'down'} │ {busy} active ")
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
