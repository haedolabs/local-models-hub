"""models - the models LM Studio holds, kept the way the settings say (models.json): a family as so many copies, always.

The settings name a family (a model id), how many copies of it to keep loaded, with what context and how many answers at
once each, and when to recycle a copy (recycle.py). Why copies: two copies of four answers at once measured faster in
all than one copy of eight (GUIDE.md, "Where to send requests"). So:
  keep()   whenever a family's first copy is loaded, its other copies are loaded too, with the same context and slots
           and no time limit. The door runs it at its start and every 20 s. Nothing is loaded from nothing (a Mac with no
           model loaded stays free), except a model that crashed: LM Studio does not reload one, so the keeper does
           (crashes.py: at most 3 times an hour). A copy is recycled before the work at which the engine has crashed
           (recycle.py). Nothing is unloaded for being idle.
  use()    a swap: refused while another run is using anything it would unload; then the rest is unloaded and the model
           loaded, with all its copies when it is a family. Swapping away drops the second copy with the first, so the
           other model has the room; swapping back brings both.
Every load and unload holds llmshare's swap lock, so the keeper and a swap never interleave. LM Studio's server is
started only when it is down: `lms server start` on a running server restarts it and cuts every request in flight.

When LM Studio refuses a load (its model-loading guardrail: "insufficient system resources"; the GUI's "Load anyway" has
no equivalent in its CLI or API), the keeper says so in words a person can act on, records what it was refused under
(conditions(): LM Studio's settings file and the GPU's memory limit), and does not try again until one of them changes or
a person loads the model; a lone copy the hub has not loaded on this Mac is never unloaded for a recycle (recycle.plan),
since the load that should follow might be refused: make before break. gpu() and gpu_words() say when a model is
bigger than the memory macOS lets the GPU hold (iogpu.wired_limit_mb, which resets at every restart); gpu_plist() is
the LaunchDaemon that sets it at every boot (com.local-models.gpu-limit, installed by a person: `localmodels peer
gpu-limit`).

A dedicated Mac (a peer that does nothing else) marks its family "load_at_start": when the door starts, the keeper loads
it once nothing of it is loaded, after LM Studio is up (its daemon started if it is not running) and the GPU may hold
it: at most 3 tries, 30 s apart, then it says how to load it by hand. Once loaded (or unloaded later by a person), it
is not loaded at start again until the door starts again. While the login loader from before the hub is installed
(com.local-models.load-at-login), the keeper leaves loading to it; `localmodels install` retires that loader once a
family is loaded at start, so one loader runs, never two.
"""
import json, os, re, subprocess, sys, time, urllib.error, urllib.request

from . import crashes, errors, llmshare, maint, peers, recycle

GUARDRAIL = re.compile(r'insufficient system resources|loading guardrail|resource guardrail', re.I)
GPU_DAEMON = '/Library/LaunchDaemons/com.local-models.gpu-limit.plist'       # sets the GPU's limit at every boot (a person installs it)
LOADER = os.path.expanduser('~/Library/LaunchAgents/com.local-models.load-at-login.plist')   # a dedicated Mac's loader from before the hub
START_TRIES, START_GAP = 3, 30                            # loads tried at start, and seconds between them
_gpu, _cond = {'at': 0.0, 'v': None}, {'at': 0.0, 'v': (0, 0)}
_start, _woke, _sizes = {}, {'at': 0.0}, {}              # this door run: each family's load at start; LM Studio started; sizes on disk


def refused(out):
    """The message when LM Studio's output says its loading guardrail stopped a load, else None."""
    if not GUARDRAIL.search(str(out or '')): return None
    return ("load refused by LM Studio's model-loading guardrail (insufficient system resources). Fix: Settings → Model loading "
            'guardrails → Off (modelLoadingGuardrails.mode "off"; the instance manager enforces headroom_gb itself), or load it '
            'once in the LM Studio UI with "Load anyway". No retry until the guardrail setting or iogpu.wired_limit_mb changes')


def conditions():
    """What a refused load depends on: (when LM Studio's settings file last changed, iogpu.wired_limit_mb). Read at most
    every 5 s (a peer's report asks for it every 2 s)."""
    if time.time() - _cond['at'] < 5: return _cond['v']
    try: m = round(os.path.getmtime(os.path.join(peers.LMSTUDIO_HOME, 'settings.json')), 1)
    except OSError: m = 0
    try: mb = int(subprocess.run(['sysctl', '-n', 'iogpu.wired_limit_mb'], capture_output=True, text=True, timeout=5).stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError): mb = 0
    _cond.update(at=time.time(), v=(m, mb))
    return (m, mb)


# Metal's own figure for what the GPU may hold (MTLDevice.recommendedMaxWorkingSetSize), read in a process of its own so the
# gateway never opens a Metal device itself; it follows iogpu.wired_limit_mb when that is set (measured on both Macs).
METAL_PROBE = '''import ctypes, ctypes.util
objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
metal = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Metal.framework/Metal")
metal.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
objc.sel_registerName.restype, objc.sel_registerName.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
send = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
d = metal.MTLCreateSystemDefaultDevice()
print(send(d, objc.sel_registerName(b"recommendedMaxWorkingSetSize")) if d else 0)'''
_ws = {'mb': None, 'v': None}


def working_set_gb(mb):
    """What Metal says the GPU may hold, in GiB (read again only when iogpu.wired_limit_mb changes), or None."""
    if _ws['mb'] == mb and _ws['v'] is not None: return _ws['v']
    try:
        out = subprocess.run([sys.executable, '-I', '-c', METAL_PROBE], capture_output=True, text=True, timeout=20).stdout.strip()
        v = int(out) / 2**30 if out.isdigit() and int(out) > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError): v = None
    _ws.update(mb=mb, v=v)
    return v


def gpu():
    """How much memory this Mac lets the GPU hold: {limit_gb, set (by iogpu.wired_limit_mb), total_gb, boot} (read once a
    minute): Metal's own figure, which follows iogpu.wired_limit_mb when that is set (measured: 107.5 GiB unset on a
    128 GiB M5 Max, 84% of memory; 40 GiB on a 48 GiB M4 Pro set to 40960 MB); when Metal does not say, the setting, or
    three quarters of memory on a Mac of 36 GB or more, two thirds below."""
    if time.time() - _gpu['at'] < 60 and _gpu['v']: return _gpu['v']
    total = mem_gb()
    try: mb = int(subprocess.run(['sysctl', '-n', 'iogpu.wired_limit_mb'], capture_output=True, text=True, timeout=5).stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError): mb = 0
    limit = working_set_gb(mb) or (mb / 1024 if mb else total * (0.75 if total >= 36 else 2 / 3))
    v = dict(limit_gb=round(limit, 1), set=bool(mb), total_gb=round(total, 1), boot=os.path.exists(GPU_DAEMON))
    _gpu.update(at=time.time(), v=v)
    return v


def gpu_plist(total_gb=None):
    """The LaunchDaemon that sets the GPU's limit at every boot, before login and LM Studio: all but 8 GB of memory
    (40960 MB on a 48 GB Mac)."""
    mb = (int(round(total_gb if total_gb is not None else mem_gb())) - 8) * 1024
    return dict(Label='com.local-models.gpu-limit', ProgramArguments=['/usr/sbin/sysctl', f'iogpu.wired_limit_mb={mb}'], RunAtLoad=True)


def guardrail_words(g, size_gb, mode):
    """Words when LM Studio's guardrail is on and the model nearly fills what the GPU may hold, before any load is refused:
    then only "Off" lets `lms load` through (measured on a 48 GB Mac: refused at every context under Relaxed, Custom
    42 GB and with alwaysAllowLoadAnyway). Else None."""
    if mode == 'off' or not g or not size_gb or not g.get('limit_gb') or size_gb <= 0.75 * g['limit_gb']: return None
    return (f'LM Studio model-loading guardrail enabled (mode "{mode or "default"}") and the model uses over 75% of the GPU wired '
            'limit: CLI/API loads will be refused (load_at_start, crash recovery). Set modelLoadingGuardrails.mode "off" '
            '(Settings → Model loading guardrails → Off); the instance manager enforces headroom_gb itself')


def gpu_words(g, size_gb):
    """Words when a model of size_gb does not fit in what the GPU may hold (with room for its context), or fits only
    because the limit was raised by hand and will reset at the next restart; else None."""
    if not g or not size_gb or not g.get('limit_gb'): return None
    total = g.get('total_gb', 0)
    room = int(total) - 8                                    # what the GPU may hold with 8 GB left to macOS and the apps
    if size_gb + 1.5 > g['limit_gb']:
        if size_gb + 1.5 > room:
            return f"model needs {size_gb:.1f} GiB + KV cache, above the maximum GPU wired limit for this host ({room} GiB): use a smaller quantization"
        return (f"GPU wired limit {g['limit_gb']:.0f} GiB < model {size_gb:.1f} GiB + KV cache: run `sudo sysctl iogpu.wired_limit_mb={room * 1024}`, "
                "then restart LM Studio; the value resets on reboot (`localmodels peer gpu-limit` writes the boot LaunchDaemon)")
    if g.get('set') and not g.get('boot') and size_gb + 1.5 > total * (0.75 if total >= 36 else 2 / 3):
        return (f"GPU wired limit {g['limit_gb']:.0f} GiB is set at runtime only and resets on reboot: "
                "`localmodels peer gpu-limit` writes the boot LaunchDaemon")
    return None


def _lms(*args, timeout=900):
    try:
        r = subprocess.run([llmshare.LMS, *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e: return False, str(e)


def gb(size):
    """'37.75 GB' -> 37.75 (MB and TB too); 0 when unreadable."""
    m = re.match(r'([\d.]+)\s*([KMGT]?B)', str(size or ''))
    return float(m.group(1)) * {'KB': 1e-6, 'MB': 1e-3, 'GB': 1, 'TB': 1e3, 'B': 1e-9}[m.group(2)] if m else 0.0


def free_gb():
    """Memory not held by programs, in GB: the Mac's total less app memory, wired and compressed (as Activity Monitor
    counts "Memory Used"; the file cache is free to take)."""
    def sh(cmd):
        try: return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError): return ''
    total = int(sh(['sysctl', '-n', 'hw.memsize']).strip() or 0)
    vm = sh(['vm_stat']); page = int((re.search(r'page size of (\d+)', vm) or [0, 16384])[1])
    get = lambda k: int((re.search(rf'{k}:\s+(\d+)', vm) or [0, 0])[1])
    held = get('Anonymous pages') - get('Pages purgeable') + get('Pages wired down') + get('Pages occupied by compressor')
    return (total - held * page) / 2**30                   # what programs hold (models included), not the file cache


def downloaded():
    """The models LM Studio has downloaded (`lms ls --json`), or None when lms cannot say."""
    ok, out = _lms('ls', '--json', timeout=60)
    try: return json.loads(out) if ok else None
    except ValueError: return None


def mem_gb():
    """The Mac's memory in GB (0 when unreadable)."""
    try: return int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=10).stdout.strip() or 0) / 2**30
    except (OSError, subprocess.SubprocessError, ValueError): return 0


def first_settings(listing, mem):
    """The settings a fresh install writes, fitted to this Mac: `listing` is `lms ls --json` (None when unknown), `mem` its
    memory in GB. Room for models is three quarters of memory (what macOS lets the GPU hold by default), and a loaded copy
    is taken as 1.2 times its file (the rest is its context). The repo's measured default stands when its model is
    downloaded and two copies fit; else the largest model that fits once, as two copies when two fit."""
    default = json.load(open(llmshare.MODELS_DEFAULT))
    base = dict(batch_at_once=default.get('batch_at_once', 8), headroom_gb=8 if mem >= 48 else 4)
    if listing is None:
        return dict(families={}, **base, why="lms could not list the downloaded models: the instance manager manages no family until one is added here")
    llms = [m for m in listing if m.get('type') == 'llm' and m.get('modelKey') and m.get('sizeBytes')]
    room, need = mem * 0.75, lambda m: m['sizeBytes'] / 2**30 * 1.2
    for key, fam in default.get('families', {}).items():
        m = next((m for m in llms if m['modelKey'] == fam.get('key', key)), None)
        if m and 2 * need(m) <= room: return dict(default, headroom_gb=base['headroom_gb'])
    fits = sorted((m for m in llms if need(m) <= room), key=lambda m: -m['sizeBytes'])
    if not fits:
        return dict(families={}, **base, why=f'no downloaded model fits in {mem:.0f} GB of memory: download a smaller one and add it here (GUIDE.md, settings)')
    m = fits[0]
    copies, parallel = (2 if 2 * need(m) <= room else 1), (4 if mem >= 32 else 2)
    recycle_band = next(iter(default.get('families', {}).values()), {}).get('recycle', False)
    measured = any(m['modelKey'] == f.get('key', k) for k, f in default.get('families', {}).items())
    fam = dict(key=m['modelKey'], copies=copies, context=min(int(m.get('maxContextLength') or 32768), 65536 if mem >= 64 else 32768),
               parallel=parallel, recycle=recycle_band,
               why=f'Fitted at install to {mem:.0f} GB and the models downloaded'
                   + ('; 1 replica, since 2 do not fit in memory.' if measured else '; the recycle thresholds were measured on another model: '
                      'read `localmodels crashes` after a few long runs and adjust them.'))
    return dict(families={m['modelKey']: fam}, batch_at_once=copies * parallel, headroom_gb=base['headroom_gb'])


def _ok_id(s):
    """A model key or identifier that is safe to hand to lms (never one that would read as a flag)."""
    s = str(s or '')
    return bool(s) and not s.startswith('-') and not re.search(r'[\s\x00-\x1f]', s)


def server_up(timeout=3):
    """Whether LM Studio's server is up: only a refused connection means down (a slow answer means up and busy)."""
    try: llmshare.urlopen(llmshare.LMSTUDIO + '/v1/models', timeout=timeout); return True
    except urllib.error.URLError as e: return not isinstance(getattr(e, 'reason', None), ConnectionRefusedError)
    except ConnectionRefusedError: return False
    except OSError: return True


def ensure_server(why='after a load'):
    """LM Studio's server, started only when it is down (starting a running one restarts it). -> whether it answers now."""
    if server_up(): return True
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} LM Studio's server is down ({why}): lms server start", flush=True)
    _lms('server', 'start', timeout=60)
    for _ in range(20):
        if server_up(2): return True
        time.sleep(0.5)
    return False


def load(key, ident, context, parallel):
    """Load one copy with lms; -> (ok, LM Studio's words). What came of it is kept (crashes.load_ok, load_refused)."""
    if not (_ok_id(key) and _ok_id(ident)): return False, f'refused: {key!r} / {ident!r} is not a model id'
    ok, out = _lms('load', key, '--identifier', ident, '-c', str(int(context)), '--parallel', str(int(parallel)), '-y')
    if ok: crashes.loaded_now([ident]); crashes.load_ok(llmshare.family(ident))
    elif refused(out): crashes.load_refused(llmshare.family(ident), refused(out), conditions())
    return ok, out


def unload(ident):
    """Unload one instance: LM Studio's REST API (POST /api/v1/models/unload {instance_id}), `lms unload` where that API is
    missing (an older LM Studio). Both pass a name that LM Studio's server also resolves by model key, to the oldest-loaded
    instance of that key (measured): a family's first replica, named by its key, is unloaded only when it is the oldest
    (recycle.oldest_first orders the recycles), and unload_exactly says what went. -> (ok, words)."""
    if not _ok_id(ident): return False, f'refused: {ident!r} is not a model id'
    req = urllib.request.Request(llmshare.LMSTUDIO + '/api/v1/models/unload', data=json.dumps(dict(instance_id=ident)).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        d = json.load(llmshare.urlopen(req, timeout=120))
        if isinstance(d, dict) and d.get('instance_id') == ident: return True, f'{ident} unloaded'
    except urllib.error.HTTPError as e:                     # 404 model_not_found: the API is there and the instance is not
        try: err = (json.loads(e.read() or b'{}') or {}).get('error')
        except (ValueError, AttributeError): err = None
        if e.code == 404 and isinstance(err, dict) and err.get('type') == 'model_not_found': return False, f'{ident} is not loaded'
    except (OSError, ValueError): pass                       # no answer, or an LM Studio without this API: the command
    return _lms('unload', ident, timeout=120)


def _listed():
    """The instance ids LM Studio's server lists now (its own API, read fresh; `lms ps` when that does not answer), or None."""
    llmshare._api['at'] = 0.0
    ids = llmshare.api_models()
    if ids is not None: return set(ids)
    rows = llmshare.loaded(fresh=True, force=True)
    return {r['id'] for r in rows} if llmshare.read_ok() else None


def unload_exactly(ident, wait=15):
    """Unload one instance and see what went: -> (ok, words, gone): gone is every id that left LM Studio's listing, which
    should be [ident] alone. It waits until something went (up to `wait` seconds), never longer."""
    before = _listed() or set()
    ok, words = unload(ident)
    end, now = time.time() + wait, None
    while True:
        now = _listed()
        if now is not None and (ident not in now or before - now): break
        if time.time() >= end: break
        time.sleep(0.5)
    return ok, words, sorted(before - now) if now is not None else []


def keep():
    """One pass: load again a model that crashed (crashes.watch), any copy of a family that is missing for sure while its
    first copy is loaded (if it fits), and recycle a copy that is due (recycle.py). -> what it did, in words."""
    conf = llmshare.settings()
    fams, room, did = conf.get('families') or {}, float(conf.get('headroom_gb', 8)), []
    try:
        if maint.tick(llmshare.inflight(), llmshare.requests_log(0.1)): did.append('maintenance begins: no batch requests in flight')
    except Exception as e: did.append(f'maintenance check failed: {type(e).__name__}: {e}'); errors.record('instance manager: maintenance check', e)
    if maint.on():                                           # the person is working on LM Studio: note what is loaded, do nothing
        rows = llmshare.loaded(fresh=True)
        if llmshare.read_ok(): maint.freeze(llmshare.family(r['id']) for r in rows)
        return did + ['maintenance: instance manager paused (no loads, reloads, recycles or server starts)']
    began = time.time()
    rows = llmshare.loaded(fresh=True)
    if not llmshare.read_ok():                              # the listing failed or is unsure: nothing to decide on, except
        w = wake(fams)                                       # a dedicated Mac whose LM Studio is not running at all
        return did + ([w] if w else [])
    busy = llmshare.busy_slots()
    try:
        back = crashes.watch(rows, listed_at=began, busy=busy)
        if crashes.stale_down(began):                       # a crash newer than the listing in hand: list again, then decide
            time.sleep(1); began = time.time(); rows = llmshare.loaded(fresh=True, force=True)
            if not llmshare.read_ok(): return did
            busy = llmshare.busy_slots(); back = crashes.watch(rows, listed_at=began, busy=busy)
    except Exception as e: back = []; did.append(f'crash watcher failed: {type(e).__name__}: {e}'); errors.record('instance manager: crash watcher', e)
    have = {r['id'] for r in rows}
    down = set((crashes._read(fresh=True).get('down') or {}))
    todo = []
    for b, f in fams.items():
        if b not in have or b in down: continue             # the first copy is not loaded, or the crash machinery has the family
        want = [c for c in llmshare.copy_ids(b, f.get('copies', 1)) if c not in have]
        if want:
            gone = crashes.confirm_missing(want, rows, busy)
            todo += [(b, f, c) for c in want if c in gone]
    boot = []                                                # a dedicated Mac: families to load at start, now
    for b, f in fams.items():
        if not isinstance(f, dict) or not f.get('load_at_start') or b in down: continue
        go, w = at_start(b, f, have, room)
        if go: boot.append((b, f))
        if w: did.append(w)
    try:
        w = recycle.give_up_if_stuck() or recycle.plan(rows, busy)
        if w: did.append(w)
    except Exception as e: did.append(f'recycle planner failed: {type(e).__name__}: {e}'); errors.record('instance manager: recycle planner', e)
    job, ready = recycle.ready(rows, busy)
    if down: job, ready = None, False                        # a family down after a crash: its reload first, no recycle in the same pass
    if not todo and not back and not boot and not (job and ready): return did
    with llmshare.swapping(wait=0) as got:
        if not got: return did                              # a swap is under way: the next pass looks again
        for j in back: did += _again(j, room)
        for b, f in boot: did += _load_at_start(b, f)
        for base, f, c in todo:
            ids = {r['id']: r for r in llmshare.loaded(fresh=True)}
            if base not in ids or c in ids: continue        # it changed meanwhile
            blocked = crashes.load_blocked(base, conditions())
            if blocked: did.append(f'{c}: load skipped: {blocked}'); break
            first, free = ids[base], free_gb()
            if free < gb(first['size']) + room:
                did.append(f"{c}: load deferred: {free:.0f} GB free, an instance needs {gb(first['size']) + room:.0f} GB incl. headroom"); break
            ok, out = load(f.get('key') or first['key'], c, f.get('context') or first['context'], f.get('parallel') or first['parallel'])
            did.append(f'{c} loaded' if ok else f'{c}: load failed: {refused(out) or out[-200:]}')
        if job and ready: did += _recycle(job, fams)
    llmshare.loaded(fresh=True)
    return did


def wake(fams):
    """A dedicated Mac right after login: a family to load at start, and LM Studio not running at all (its server refuses
    connections and `lms ps` fails). Its daemon is started (lms daemon up; never stopped), once a door run. -> words or None."""
    if _woke['at'] or not any(isinstance(f, dict) and f.get('load_at_start') for f in fams.values()) or server_up(): return None
    _woke['at'] = time.time()
    ok, out = _lms('daemon', 'up', timeout=120)
    ensure_server('to load a family at start')
    return "LM Studio not running: started llmster (`lms daemon up`) for load_at_start: " + ', '.join(b for b, f in fams.items() if isinstance(f, dict) and f.get('load_at_start')) + \
        ('' if ok else f' (lms: {out[-160:]})')


def _size_of(key):
    """A downloaded model's size in GB (`lms ls --json`, read once a door run), or 0 when it cannot be said."""
    if key not in _sizes:
        rows = downloaded()
        if rows is None: return 0
        m = next((m for m in rows if isinstance(m, dict) and m.get('modelKey') == key), None)
        _sizes[key] = (m.get('sizeBytes') or 0) / 2**30 if m else 0
    return _sizes[key]


def at_start(b, f, have, room):
    """Whether to load a family marked load_at_start now: -> (load now, words or None). Not when any copy of it is loaded
    (then this start is done), when it was loaded or given up on in this door run, while the login loader from before the
    hub is installed (it loads it), when LM Studio refused it and nothing has changed, when the GPU may not hold it yet,
    when there is not room, or during the pause after a failed try."""
    s = _start.setdefault(b, dict(tries=0, next=0.0, done=False))
    if any(llmshare.family(i) == b for i in have): s['done'] = True; return False, None
    if s['done']: return False, None
    if os.path.exists(LOADER): return False, f'{b}: load_at_start deferred to LaunchAgent com.local-models.load-at-login (`localmodels install` retires it)'
    blocked = crashes.load_blocked(b, conditions())
    if blocked: return False, f'{b}: load_at_start blocked: {blocked}'
    size, g = _size_of(f.get('key') or b), gpu()
    if size and g.get('limit_gb') and size + 1.5 > g['limit_gb']:
        return False, f"{b}: load_at_start waiting for GPU wired limit ({g['limit_gb']:.0f} GiB < {size:.1f} GiB + KV cache)"
    free = free_gb()
    if size and free < size + room: return False, f'{b}: load_at_start waiting for memory: {free:.0f} GiB free, {size + room:.0f} GiB required (incl. headroom_gb)'
    if time.time() < s['next']: return False, None
    return True, None


def _load_at_start(b, f):
    """Load a family's first copy at start (under the swap lock): its settings' key, context and slots. -> words."""
    s = _start.setdefault(b, dict(tries=0, next=0.0, done=False))
    s['tries'] += 1
    ok, out = load(f.get('key') or b, b, f.get('context') or 32768, f.get('parallel') or 4)
    if ok:
        s['done'] = True; ensure_server()                  # LM Studio may turn its server off when a model changes
        return [f'{b}: loaded (load_at_start' + (f', attempt {s["tries"]}' if s['tries'] > 1 else '') + ')']
    if refused(out): return [f'{b}: load_at_start failed: {refused(out)}']   # recorded: no try until something changes
    s['next'] = time.time() + START_GAP
    if s['tries'] >= START_TRIES:
        s['done'] = True
        s['words'] = f'{b}: load_at_start failed after {START_TRIES} attempts ({out[-160:]}); load manually: localmodels use {b}'
        return [s['words']]
    return [f'{b}: load_at_start attempt {s["tries"]}/{START_TRIES} failed, retry in {START_GAP} s: {out[-160:]}']


def end_maintenance():
    """Maintenance is over: skip what LM Studio logged meanwhile, clear the record, then load again each model that was
    loaded when it began (its first copy; the keeper adds the other copies on its next pass). -> what it did, in words."""
    rows = llmshare.loaded(fresh=True, force=True)
    crashes.after_maintenance(rows)                          # before the record goes, so no keeper pass reads the restart as crashes
    prev = maint.end()
    if not prev: return ['no maintenance was set']
    have = {r['id'] for r in rows}
    todo = [b for b in prev.get('before') or [] if b not in have]
    if not todo: return ['maintenance is over; what was loaded before is loaded']
    fams, did = llmshare.settings().get('families') or {}, []
    ensure_server('after maintenance')
    with llmshare.swapping(wait=120) as got:
        if not got: return [f"maintenance ended during a model swap: load {', '.join(todo)} manually (localmodels use <model>)"]
        for b in todo:
            f = fams.get(b)
            if not f: did.append(f'{b} was loaded before maintenance but is not in settings: load it manually (localmodels use {b})'); continue
            ok, out = load(f.get('key') or b, b, f.get('context') or 32768, f.get('parallel') or 4)
            did.append(f'{b} reloaded after maintenance' if ok else f'{b}: reload after maintenance failed: {out[-200:]}')
    llmshare.loaded(fresh=True)
    return did


def _again(job, room):
    """Load a crashed model again, every copy it should have, the way it was loaded (under the swap lock). -> words."""
    base, did, t0, tried = job['base'], [], time.time(), False
    blocked = crashes.load_blocked(base, conditions())
    if blocked: return [f'{base}: crash recovery reload skipped: {blocked}']     # it would be refused again: nothing has changed
    for c in job['ids']:
        if c in {r['id'] for r in llmshare.loaded(fresh=True)}: continue
        need, free = (gb(job['size']) + room if job['size'] else 0), free_gb()
        if free < need:
            why = f'{free:.0f} GB free, an instance needs {need:.0f} GB incl. headroom'
            crashes.waiting(base, why); did.append(f'{c} crashed; reload deferred: {why}'); return did
        if not tried: crashes.trying(base); tried = True
        t1 = time.time()
        ok, out = load(job['key'], c, job['context'] or 32768, job['parallel'] or 4)
        if not ok:
            crashes.loaded(base, [c], False, time.time() - t0, refused(out) or out)
            did.append(f'{c}: crash recovery reload failed: {refused(out) or out[-200:]}'); return did
        ensure_server()                                    # LM Studio may turn its server off when a model changes
        crashes.loaded(base, [c], True, time.time() - t1)   # each copy serves as soon as it is back, whatever its siblings do
        did.append(f'{c} reloaded after the crash in {time.time() - t1:.0f} s')
    return did


def _recycle(job, fams):
    """Unload a drained copy and load it again with its family's settings (under the swap lock). -> words."""
    copy, base, t0 = job['copy'], job['family'], time.time()
    f = fams.get(base) or {}
    key, context, parallel = f.get('key') or base, f.get('context') or 32768, f.get('parallel') or 4
    recycle.reloading(copy)
    listed = _listed()
    if listed is not None and copy not in listed:          # resumed after a cut between the unload and the load: only the load is
        ok, out = load(key, copy, context, parallel)       # left (an unload by its name would take a sibling of that model key)
        ensure_server()
        recycle.done(copy, ok, time.time() - t0, '' if ok else refused(out) or out)
        return [f'{copy} loaded to finish its recycle in {time.time() - t0:.0f} s: {job["why"]}' if ok else f'{copy}: reload after recycle failed: {refused(out) or out[-200:]}']
    uok, uwords, gone = unload_exactly(copy)
    wrong = [i for i in gone if i != copy]
    if wrong or copy not in gone:                            # another instance went (its requests are resent), or none did
        back = []
        for i in wrong:
            ok, out = load(key, i, context, parallel)
            back.append(f'{i} loaded again' if ok else f'{i} could not be loaded again: {refused(out) or out[-160:]}')
        ensure_server()
        why = (f"LM Studio unloaded {', '.join(wrong)} when asked to unload {copy}; " + '; '.join(back)) if wrong else f'{copy} was not unloaded ({uwords[-160:]})'
        recycle.given_up(copy, why)
        return [f'{copy}: recycle stopped: {why}; tried again in {recycle.RETRY // 60} min']
    ok, out = load(key, copy, context, parallel)
    ensure_server()
    recycle.done(copy, ok, time.time() - t0, '' if ok else refused(out) or out)
    return [f"{copy} recycled in {time.time() - t0:.0f} s: {job['why']}" if ok else f'{copy}: reload after recycle failed: {refused(out) or out[-200:]}']


def plan(ident, context=None):
    """What a swap to this model would do: -> (copies wanted, models to unload), from what LM Studio holds now."""
    f = (llmshare.settings().get('families') or {}).get(llmshare.family(ident)) or {}
    context = int(context or f.get('context') or 32768)
    want = llmshare.copy_ids(ident, f.get('copies', 1)) if f and llmshare.family(ident) == ident else [ident]
    drop = [r['id'] for r in llmshare.loaded(fresh=True) if r['id'] not in want or (r['context'] and r['context'] != context)]
    return want, drop


def use(key, ident=None, context=None, parallel=None, dry=False):
    """Swap to one model, with all its copies when it is a family. -> (0 done / 3 refused / 1 failed, words)."""
    ident = ident or key
    if not (_ok_id(key) and _ok_id(ident)): return 1, f'{key!r} is not a model key'
    f = (llmshare.settings().get('families') or {}).get(llmshare.family(ident)) or {}
    context, parallel = int(context or f.get('context') or 32768), int(parallel or f.get('parallel') or 4)
    with llmshare.swapping(wait=120) as got:
        if not got: return 3, 'another swap is under way: try again in a moment'
        want, drop = plan(ident, context)
        if not llmshare.read_ok(): return 3, "LM Studio did not say which models it holds (lms ps failed): not swapping; try again in a moment"
        why = llmshare.swap_blockers(drop)
        if why: return 3, 'not now: ' + '; '.join(why) + '. Try again when it is done.'
        have = {r['id'] for r in llmshare.loaded()}
        add = [c for c in want if c not in have or c in drop]
        if dry: return 0, f"would unload {', '.join(drop) or 'nothing'}; would load {', '.join(add) or 'nothing'}"
        drop = sorted(drop, key=lambda i: llmshare.family(i) == i)   # <id>:N first: a bare id (a model key) is unambiguous once its siblings are gone
        asked = list(drop)
        for _ in range(2):                                   # what is still listed is unloaded again, once
            for i in drop: unload(i)
            now = _listed()
            if now is None: break
            drop = [i for i in drop if i in now]
            if not drop: break
        now = _listed()
        if now is not None: crashes.unloaded_now([i for i in asked if i not in now])   # gone on purpose, not crashed
        if now is not None: add = [c for c in want if c not in now or c in drop]   # a wanted one that went is loaded back
        for c in add:
            ok, out = load(key, c, context, parallel)
            if not ok: return 1, f'could not load {c}: {out[-300:]}'
        ensure_server()                                        # LM Studio may turn its server off when a model changes
        llmshare.loaded(fresh=True)
        crashes.swapped(ident)                                 # a person chose: no crashed model is waiting to come back
    n = len(want)
    return 0, f"{ident} loaded" + (f" as {n} replicas, {n * parallel} slots" if n > 1 else '')
