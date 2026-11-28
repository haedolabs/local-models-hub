"""models - the models LM Studio holds, kept the way the settings say (models.json): a family as so many copies, always.

The settings name a family (a model id), how many copies of it to keep loaded, with what context and how many answers at
once each, and when to recycle a copy (recycle.py). Why copies: two copies of four answers at once measured faster in
all than one copy of eight (GUIDE.md, "Where to send requests"). So:
  keep()   whenever a family's first copy is loaded, its other copies are loaded too, with the same context and slots
           and no time limit. The door runs it at its start and every 20 s. Nothing is loaded from nothing (a Mac with no
           model loaded stays free), except a model that crashed: LM Studio does not reload one, so the keeper does
           (crashes.py: at most 3 times an hour). A copy whose engine stalled is taken out of routing and reloaded
           (watchdog.py). A copy is recycled before the work at which the engine has crashed (recycle.py). Nothing is
           unloaded for being idle.
  keep()   an embedding family ("kind": "embedding", keep_embedding) is the exception to "nothing from nothing": it is small
           (an 84 MB model) and clients search by meaning with it, so whenever it is missing for sure it is loaded, at
           gateway start, after a crash, an eject or an `lms unload`, and never unloaded for being idle. Its load waits only
           for its own weights and EMBED_ROOM free, never for headroom_gb, and it is left out of the chat families' room
           (room_for), slots (llmshare.at_once) and admission budgets. A failed load is tried again after 1, 2, then 5 min.
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
family is loaded at start, so one loader runs, never two. On a dedicated Mac no load waits for free memory (room_for):
at start, after a crash, or a missing replica, it waits only for the GPU's wired limit.
"""
import json, os, re, subprocess, sys, time, urllib.error, urllib.request

from . import crashes, errors, llmshare, maint, peers, recycle, watchdog

GUARDRAIL = re.compile(r'insufficient system resources|loading guardrail|resource guardrail', re.I)
GPU_DAEMON = '/Library/LaunchDaemons/com.local-models.gpu-limit.plist'       # sets the GPU's limit at every boot (a person installs it)
LOADER = os.path.expanduser('~/Library/LaunchAgents/com.local-models.load-at-login.plist')   # a dedicated Mac's loader from before the hub
START_TRIES, START_GAP = 3, 30                            # loads tried at start, and seconds between them
EMBED_ROOM = 1.0                                          # GiB free an embedding model's load needs beside its weights
EMBED_PAUSES = (60, 120, 300)                             # seconds before an embedding family's next try after 1, 2, 3+ failed loads
PEER_EMBED_TTL = 180                                      # a front renews a missing embedding once a minute
KV_ROOM = 1.5                                             # GiB an instance needs within the GPU's limit beside its weights, at least
_gpu, _cond = {'at': 0.0, 'v': None}, {'at': 0.0, 'v': (0, 0)}
_sizes, _BORN = {}, time.time()                          # sizes on disk (read once a process); when this process started


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
    if size_gb + KV_ROOM > g['limit_gb']:
        if size_gb + KV_ROOM > room:
            return f"model needs {size_gb:.1f} GiB + KV cache, above the maximum GPU wired limit for this host ({room} GiB): use a smaller quantization"
        return (f"GPU wired limit {g['limit_gb']:.0f} GiB < model {size_gb:.1f} GiB + KV cache: run `sudo sysctl iogpu.wired_limit_mb={room * 1024}`, "
                "then restart LM Studio; the value resets on reboot (`localmodels peer gpu-limit` writes the boot LaunchDaemon)")
    if g.get('set') and not g.get('boot') and size_gb + KV_ROOM > total * (0.75 if total >= 36 else 2 / 3):
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


def memory(total=None):
    """(Used, total) GiB as Activity Monitor counts memory, or (0, 0) if unavailable."""
    total = total or mem_gb()
    try: vm = subprocess.check_output(['vm_stat'], text=True, timeout=10)
    except (OSError, subprocess.SubprocessError): return 0, 0
    if not total or 'Anonymous pages:' not in vm: return 0, 0
    page = int((re.search(r'page size of (\d+)', vm) or [0, 16384])[1])
    get = lambda k: int((re.search(rf'{k}:\s+(\d+)', vm) or [0, 0])[1])
    held = get('Anonymous pages') - get('Pages purgeable') + get('Pages wired down') + get('Pages occupied by compressor')
    return held * page / 2**30, total


def free_gb():
    """GiB not held by programs, including reclaimable file cache."""
    used, total = memory()
    return total - used


def room_for(f, size, room, rows=None):
    """Why an instance of `size` GiB of family settings `f` cannot be loaded on this Mac now, in words, or None when it
    can: the test before every load the keeper makes (load_at_start, crash recovery, a missing replica; a recycle reloads
    what it has just unloaded and asks nothing). A family with "load_at_start" marks a dedicated Mac: it waits only until
    the GPU's wired limit holds its weights and KV cache beside what is loaded (rows: the listing, the last in hand by
    default). What other programs hold there is for macOS to compress or swap, as it does at every recycle, and waiting
    for it to be free never ends: macOS fills freed memory with caches and decompressed pages (on the 48 GiB M4 Pro, 9.0
    GiB in use a second after a crash and 13.2 GiB an hour later, while the reload waited for 39.2 GiB free). Any other
    family waits until its weights and `room` (headroom_gb) are free."""
    if not size: return None
    if isinstance(f, dict) and f.get('kind') == 'embedding':  # a small model: its weights and EMBED_ROOM, never headroom_gb
        free = free_gb()
        return f'{free:.1f} GiB free, the embedding model needs {size + EMBED_ROOM:.1f} GiB' if free < size + EMBED_ROOM else None
    if isinstance(f, dict) and f.get('load_at_start'):
        g, listed = gpu(), (llmshare.loaded() if rows is None else rows)
        weights = sum(gb(r.get('size')) for r in listed)       # an embedding model's weights count, its context is the chat rows' not
        if g.get('limit_gb') and weights + size + KV_ROOM * (len(llmshare.chat_rows(listed)) + 1) > g['limit_gb']:
            return f"GPU wired limit {g['limit_gb']:.0f} GiB < {size:.1f} GiB + KV cache" + (f' beside {weights:.1f} GiB loaded' if listed else '')
        return None
    free = free_gb()
    return f'{free:.0f} GiB free, an instance needs {size + room:.0f} GiB incl. headroom' if free < size + room else None


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
    embed = embedding_family(listing)                       # an embedding model downloaded: kept loaded beside the chat family
    room, need = mem * 0.75, lambda m: m['sizeBytes'] / 2**30 * 1.2
    for key, fam in default.get('families', {}).items():
        m = next((m for m in llms if m['modelKey'] == fam.get('key', key)), None)
        if m and 2 * need(m) <= room: return dict(default, families=dict(default.get('families') or {}, **embed), headroom_gb=base['headroom_gb'])
    fits = sorted((m for m in llms if need(m) <= room), key=lambda m: -m['sizeBytes'])
    if not fits:
        return dict(families=embed, **base, why=f'no downloaded model fits in {mem:.0f} GB of memory: download a smaller one and add it here (GUIDE.md, settings)')
    m = fits[0]
    copies, parallel = (2 if 2 * need(m) <= room else 1), (4 if mem >= 32 else 2)
    recycle_band = next(iter(default.get('families', {}).values()), {}).get('recycle', False)
    measured = any(m['modelKey'] == f.get('key', k) for k, f in default.get('families', {}).items())
    fam = dict(key=m['modelKey'], copies=copies, context=min(int(m.get('maxContextLength') or 32768), 65536 if mem >= 64 else 32768),
               parallel=parallel, recycle=recycle_band,
               why=f'Fitted at install to {mem:.0f} GB and the models downloaded'
                   + ('; 1 replica, since 2 do not fit in memory.' if measured else '; the recycle thresholds were measured on another model: '
                      'read `localmodels crashes` after a few long runs and adjust them.'))
    return dict(families=dict({m['modelKey']: fam}, **embed), batch_at_once=copies * parallel, headroom_gb=base['headroom_gb'])


EMBED_PREFERRED = 'nomic-embed-text'                       # the embedding model the hub's clients were measured with


def embedding_family(listing):
    """The embedding family a fresh install writes when an embedding model is downloaded (`lms ls --json` rows): nomic-embed-text
    when it is there, else the smallest. -> {id: family} or {}."""
    ms = [m for m in listing or [] if isinstance(m, dict) and m.get('type') == 'embedding' and _ok_id(m.get('modelKey'))]
    if not ms: return {}
    m = sorted(ms, key=lambda m: (EMBED_PREFERRED not in m['modelKey'], m.get('sizeBytes') or 0, m['modelKey']))[0]
    fam = dict(key=m['modelKey'], kind='embedding', why='Kept loaded beside the chat family, for clients that search by meaning '
               '(GUIDE.md, Embedding models). Fitted at install to the embedding models downloaded.')
    if m.get('maxContextLength'): fam['context'] = int(m['maxContextLength'])
    return {m['modelKey']: fam}


def _ok_id(s):
    """A model key or identifier that is safe to hand to lms (never one that would read as a flag)."""
    s = str(s or '')
    return bool(s) and not s.startswith('-') and not re.search(r'[\s\x00-\x1f]', s)


def server_up(timeout=3):
    """Whether LM Studio's server is up: only a refused connection means down (a slow answer means up and busy)."""
    return llmshare.server_up(timeout)


def ensure_server(why='after a load'):
    """LM Studio's server, started only when it is down, with one start across every process (llmshare.start_server; the
    instance manager does not wait out the request path's minute between two starts). -> whether it answers now."""
    return llmshare.start_server(why, debounce=0)


# ---------------------------------------------------------------- load_at_start, once per gateway run (keeper.json)
def run_id():
    """This gateway run: the supervisor's (LOCALMODELS_RUN in a worker, or supervisor.json's while the supervisor holds its
    lock), so a handoff, or a supervisor restart that adopts its workers, is not a new start; else this process's own."""
    r = os.environ.get('LOCALMODELS_RUN')
    if r: return r
    if llmshare._flock_test(os.path.join(llmshare.RUN, 'supervisor.lock')):
        try: r = json.load(open(os.path.join(llmshare.RUN, 'supervisor.json'))).get('run')
        except (OSError, ValueError, AttributeError): r = None
        if r: return str(r)
    return f'{os.getpid()}-{int(_BORN)}'


def _starts(st):
    """keeper.json 'at_start' for this run: {run, families: {family: {tries, next, done, words}}, woke}, new for a new run."""
    a = st.get('at_start')
    if not isinstance(a, dict) or a.get('run') != run_id(): a = st['at_start'] = dict(run=run_id(), families={}, woke=0.0)
    return a


def start_state(b):
    """A family's load_at_start record in this gateway run: {tries, next, done[, words]}."""
    a = crashes._read(fresh=True).get('at_start')
    if isinstance(a, dict) and a.get('run') == run_id(): return dict((a.get('families') or {}).get(b) or dict(tries=0, next=0.0, done=False))
    return dict(tries=0, next=0.0, done=False)


def _start_update(b, **fields):
    with crashes._state() as st: _starts(st)['families'].setdefault(b, dict(tries=0, next=0.0, done=False)).update(fields)


def reset_start():
    """Forget the load_at_start record (a new run starts without one anyway)."""
    with crashes._state() as st: st.pop('at_start', None)


def load(key, ident, context, parallel):
    """Load one copy with lms and verify the exact instance and parameters in a fresh listing before reporting success."""
    if not (_ok_id(key) and _ok_id(ident)): return False, f'refused: {key!r} / {ident!r} is not a model id'
    f = (llmshare.settings().get('families') or {}).get(llmshare.family(ident))
    if isinstance(f, dict) and f.get('kind') == 'embedding':   # its own settings or LM Studio's defaults, never a chat model's 32,768 and 4
        context, parallel = f.get('context'), f.get('parallel')
    args = ['load', key, '--identifier', ident] + (['-c', str(int(context))] if context else []) + (['--parallel', str(int(parallel))] if parallel else [])
    ok, out = _lms(*args, '-y')
    if not ok:
        if refused(out): crashes.load_refused(llmshare.family(ident), refused(out), conditions())
        return False, out
    rows = llmshare.loaded(fresh=True, force=True)
    row = next((r for r in rows if r.get('id') == ident and r.get('state') != 'LOADING'), None)
    if not llmshare.read_ok() or row is None:
        return False, f'lms load returned success, but {ident} is not confirmed in LM Studio\'s fresh model listing'
    drift = recycle.drift(row, dict(context=context, parallel=parallel))
    if drift: return False, f'lms load returned success, but {ident} has {drift}'
    crashes.loaded_now([ident]); crashes.load_ok(llmshare.family(ident))
    return True, out


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
        if isinstance(d, dict) and 'Unexpected endpoint or method' in str(d.get('error') or ''):
            return _lms('unload', ident, timeout=120)                  # the server positively rejected the route without acting
        return False, f'{ident}: the unload API returned an unexpected response; not retrying an ambiguous mutation'
    except urllib.error.HTTPError as e:
        try: err = (json.loads(e.read() or b'{}') or {}).get('error')
        except (ValueError, AttributeError): err = None
        if e.code == 404 and isinstance(err, dict) and err.get('type') == 'model_not_found': return False, f'{ident} is not loaded'
        if e.code == 404: return _lms('unload', ident, timeout=120)       # positive evidence that an older server has no such route
        return False, f'{ident}: unload API returned HTTP {e.code}; not retrying an ambiguous mutation'
    except urllib.error.URLError as e:
        if isinstance(getattr(e, 'reason', None), ConnectionRefusedError):
            return _lms('unload', ident, timeout=120)                  # the connection was refused: no request reached the server
        return False, f'{ident}: unload outcome is unknown ({e}); not retrying an ambiguous mutation'
    except (OSError, ValueError) as e:
        return False, f'{ident}: unload outcome is unknown ({type(e).__name__}: {e}); not retrying an ambiguous mutation'


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
    off = maint.switched_off()
    if off: return did + off_pass(off)                       # switched off: unload once, then nothing at all
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
    try: did += watchdog.check(rows, fams)                  # an instance still listed whose engine stopped: out of routing, reloaded below
    except Exception as e: did.append(f'watchdog failed: {type(e).__name__}: {e}'); errors.record('instance manager: watchdog', e)
    have = {r['id'] for r in rows}
    state = crashes._read(fresh=True)
    downs = state.get('down') or {}
    down = set(downs)
    reloading = {b for b, d in downs.items() if d.get('state') in ('loading again', 'waiting for room')}
    todo = []
    for b, f in fams.items():
        if b not in have or b in down or llmshare.embedding(b, fams): continue   # the first copy is not loaded, the crash machinery
                                                             # has the family, or it is an embedding family (loaded below, every copy)
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
    embeds = []                                              # embedding families: loaded whenever missing for sure, from nothing too
    embedding_families = {b: dict(key=b, kind='embedding') for b, rec in (state.get('embed') or {}).items()
                          if rec.get('requested_until', 0) > time.time()}
    embedding_families.update(fams)                          # the peer's own configuration takes precedence
    for b, f in embedding_families.items():
        if not isinstance(f, dict) or f.get('kind') != 'embedding' or b in down: continue   # down: the crash machinery has it
        want = [c for c in llmshare.copy_ids(b, f.get('copies', 1)) if c not in have]
        if not want:
            if embed_state(b).get('fails') or embed_state(b).get('words'): _embed_update(b, fails=0, next=0.0, words=None)
            continue
        if time.time() < embed_state(b).get('next', 0): continue          # the pause after a failed load
        gone = crashes.confirm_missing(want, rows, busy)
        embeds += [(b, f, c) for c in want if c in gone]
    try:
        w = recycle.give_up_if_stuck() or recycle.plan(rows, busy)
        if w: did.append(w)
    except Exception as e: did.append(f'recycle planner failed: {type(e).__name__}: {e}'); errors.record('instance manager: recycle planner', e)
    job, ready = recycle.ready(rows, busy)
    if job and (job['family'] in reloading if job.get('stall') else down): job, ready = None, False   # a crash reload first, no recycle in the
    # same pass; a stalled copy waits only for a reload under way in its family, not for a sibling left down (it serves nothing as it is)
    if not todo and not back and not boot and not embeds and not (job and ready): return did
    with llmshare.swapping(wait=0) as got:
        if not got: return did                              # a swap is under way: the next pass looks again
        for j in back: did += _again(j, room)
        for b, f in boot: did += _load_at_start(b, f)
        for b, f, c in embeds: did += _load_embedding(b, f, c)
        for base, f, c in todo:
            ids = {r['id']: r for r in llmshare.loaded(fresh=True)}
            if base not in ids or c in ids: continue        # it changed meanwhile
            blocked = crashes.load_blocked(base, conditions())
            if blocked: did.append(f'{c}: load skipped: {blocked}'); break
            why = room_for(f, gb(ids[base]['size']), room, list(ids.values()))
            if why: did.append(f"{c}: load deferred: {why}"); break
            first = ids[base]
            ok, out = load(f.get('key') or first['key'], c, f.get('context') or first['context'], f.get('parallel') or first['parallel'])
            did.append(f'{c} loaded' if ok else f'{c}: load failed: {refused(out) or out[-200:]}')
        if job and ready: did += _recycle(job, fams)
    llmshare.loaded(fresh=True)
    return did


def embed_state(b):
    """An embedding family's load record (keeper.json 'embed'): {fails, next, words, at}; {} when nothing failed."""
    return dict((crashes._read(fresh=True).get('embed') or {}).get(b) or {})


def _embed_update(b, **fields):
    with crashes._state() as st: st.setdefault('embed', {}).setdefault(b, {}).update(fields)


def _downloaded_embeddings():
    rows = downloaded()
    if not isinstance(rows, list): return None
    return {m['modelKey']: m for m in rows if isinstance(m, dict) and m.get('modelKey')
            and m.get('type') == 'embedding' and not m.get('deviceIdentifier')}


def request_embeddings(names):
    """A linked front needs these families. Only the peer's installed embedding models qualify. The regular
    instance manager performs the loads; this request never changes settings, unloads a model, or downloads weights."""
    if not isinstance(names, list) or not names or any(not isinstance(b, str) or len(b) > 200 or not _ok_id(b)
                                                       or llmshare.family(b) != b for b in names):
        return 400, dict(error='models must be a nonempty list of embedding family ids')
    if maint.on(): return 503, dict(error='local model loads paused on this peer')
    available = _downloaded_embeddings()
    if available is None: return 503, dict(error='could not list the models downloaded on this peer')
    fams = llmshare.settings().get('families') or {}
    wanted, unavailable = [], []
    for b in sorted(set(names)):
        f = fams.get(b)
        key = (f or {}).get('key') or b
        if key in available and (f is None or llmshare.embedding(b, fams)):
            wanted.append(b)
        else:
            unavailable.append(b)
    if wanted:
        with crashes._state() as st:
            for b in wanted:
                if b not in fams:
                    st.setdefault('embed', {}).setdefault(b, {})['requested_until'] = time.time() + PEER_EMBED_TTL
        crashes.nudge()
    return 200, dict(requested=wanted, unavailable=unavailable)


def _load_embedding(b, f, c):
    """Load a missing instance of an embedding family (under the swap lock): its own settings, or LM Studio's defaults. A
    failed load pauses 1, 2, then 5 minutes before the next. -> words."""
    if maint.on() or c in {r['id'] for r in llmshare.loaded(fresh=True)} or not llmshare.read_ok(): return []
    blocked = crashes.load_blocked(b, conditions())
    if blocked: return [f'{c}: embedding model load skipped: {blocked}']
    key = f.get('key') or b
    configured = (llmshare.settings().get('families') or {}).get(b)
    if configured is None:                                 # the catalog or request can change before the keeper takes the swap lock
        if embed_state(b).get('requested_until', 0) <= time.time(): return []
        model = (_downloaded_embeddings() or {}).get(key)
        if not model: return [f'{c}: requested embedding no longer available on disk']
        size = (model.get('sizeBytes') or 0) / 2**30
    else:
        if configured != f: return []
        size = _size_of(key)
    why = room_for(f, size, 0)
    if why:
        _embed_update(b, words=f'load deferred: {why}'); return [f'{c}: embedding model load deferred: {why}']
    if maint.on(): return []
    ok, out = load(key, c, None, None)
    if ok:
        ensure_server()                                    # LM Studio may turn its server off when a model changes
        listed = llmshare.loaded(fresh=True, force=True)
        ok = llmshare.read_ok() and any(r['id'] == c for r in listed)
        if not ok: out = 'could not verify the loaded embedding in LM Studio'
    if ok:
        _embed_update(b, fails=0, next=0.0, words=None, at=time.time())
        return [f'{c} loaded (embedding model, kept loaded)']
    fails = int(embed_state(b).get('fails') or 0) + 1
    pause, words = EMBED_PAUSES[min(fails, len(EMBED_PAUSES)) - 1], refused(out) or out[-200:]
    _embed_update(b, fails=fails, next=time.time() + pause, words=f'load failed: {words}')
    return [f'{c}: embedding model load failed ({words}); next try in {pause // 60} min']


def embeddings_view(rows=None):
    """For the screens: each embedding family in the settings, loaded or not: [{id, key, loaded, down, words}]. down: the
    crash machinery's state for it, if any; words: why it is not loaded, when that is known."""
    fams = llmshare.settings().get('families') or {}
    rows = llmshare.listed() if rows is None else rows
    have, downs = {r['id'] for r in rows}, crashes._read(fresh=True).get('down') or {}
    out = []
    for b, f in fams.items():
        if not isinstance(f, dict) or f.get('kind') != 'embedding': continue
        loaded = all(c in have for c in llmshare.copy_ids(b, f.get('copies', 1)))
        d, e = downs.get(b) or {}, embed_state(b)
        words = None if loaded else (d.get('why') or d.get('state')) if d else e.get('words') or 'not loaded: the instance manager loads it within a minute'
        out.append(dict(id=b, key=f.get('key') or b, loaded=loaded, down=d.get('state') if d else None, words=words))
    return out


def wake(fams):
    """A dedicated Mac right after login: a family to load at start, and LM Studio not running at all (its server refuses
    connections and `lms ps` fails). Its daemon is started (lms daemon up; never stopped), once a door run. -> words or None."""
    a = crashes._read(fresh=True).get('at_start')
    if (isinstance(a, dict) and a.get('run') == run_id() and a.get('woke')) or not any(isinstance(f, dict) and f.get('load_at_start') for f in fams.values()) or server_up(): return None
    with crashes._state() as st: _starts(st)['woke'] = time.time()
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
    hub is installed (it loads it), when LM Studio refused it and nothing has changed, while the GPU's wired limit does not
    hold it yet (room_for: a dedicated Mac waits for no free memory), or during the pause after a failed try."""
    s = start_state(b)
    if any(llmshare.family(i) == b for i in have):
        if not s['done']: _start_update(b, done=True)
        return False, None
    if s['done']: return False, None
    if os.path.exists(LOADER): return False, f'{b}: load_at_start deferred to LaunchAgent com.local-models.load-at-login (`localmodels install` retires it)'
    blocked = crashes.load_blocked(b, conditions())
    if blocked: return False, f'{b}: load_at_start blocked: {blocked}'
    why = room_for(f, _size_of(f.get('key') or b), room)  # a dedicated Mac's: the GPU's wired limit, never free memory
    if why: return False, f'{b}: load_at_start waiting: {why}'
    if time.time() < s['next']: return False, None
    return True, None


def _load_at_start(b, f):
    """Load a family's first copy at start (under the swap lock): its settings' key, context and slots. -> words."""
    tries = start_state(b)['tries'] + 1
    _start_update(b, tries=tries)
    ok, out = load(f.get('key') or b, b, f.get('context') or 32768, f.get('parallel') or 4)
    if ok:
        _start_update(b, done=True); ensure_server()       # LM Studio may turn its server off when a model changes
        return [f'{b}: loaded (load_at_start' + (f', attempt {tries}' if tries > 1 else '') + ')']
    if refused(out): return [f'{b}: load_at_start failed: {refused(out)}']   # recorded: no try until something changes
    if tries >= START_TRIES:
        words = f'{b}: load_at_start failed after {START_TRIES} attempts ({out[-160:]}); load manually: localmodels use {b}'
        _start_update(b, next=time.time() + START_GAP, done=True, words=words)
        return [words]
    _start_update(b, next=time.time() + START_GAP)
    return [f'{b}: load_at_start attempt {tries}/{START_TRIES} failed, retry in {START_GAP} s: {out[-160:]}']


def end_maintenance():
    """Maintenance is over: skip what LM Studio logged meanwhile, clear the record, then load again each model that was
    loaded when it began (its first copy; the keeper adds the other copies on its next pass). -> what it did, in words."""
    rows = llmshare.loaded(fresh=True, force=True)
    crashes.after_maintenance(rows)                          # before the record goes, so no keeper pass reads the restart as crashes
    prev = maint.end()
    if not prev: return ['no maintenance was set']
    return _load_back(prev.get('before') or [], {r['id'] for r in rows}, 'maintenance')


def _load_back(families, have, after):
    """Load each family's first copy again (the keeper adds the other copies on its next pass). -> words."""
    todo = [b for b in families if b not in have]
    if not todo: return [f'{after} is over; what was loaded before is loaded']
    fams, did = llmshare.settings().get('families') or {}, []
    ensure_server(f'after {after}')
    with llmshare.swapping(wait=120) as got:
        if not got: return [f"{after} ended during a model swap: load {', '.join(todo)} manually (localmodels use <model>)"]
        for b in todo:
            f = fams.get(b)
            if not f: did.append(f'{b} was loaded before {after} but is not in settings: load it manually (localmodels use {b})'); continue
            ok, out = load(f.get('key') or b, b, f.get('context') or 32768, f.get('parallel') or 4)
            did.append(f'{b} reloaded after {after}' if ok else f'{b}: reload after {after} failed: {out[-200:]}')
    llmshare.loaded(fresh=True)
    return did


def _peer_words():
    """Words when no peer can take requests now (switched off to peers only then refuses them all), else ''."""
    v = peers.view()
    if not v: return 'no peer is configured (localmodels peer add <name> <address>): every request gets 503 until one is'
    if not any(p.get('state') == 'up' for p in v): return 'no peer is reachable now: requests get 503 until one is (' + '; '.join(p.get('words') or p['name'] for p in v) + ')'
    return ''


def switch_off(force=False, why='', peers_only=False):
    """Switch the local models off (POST /hub/off, localmodels off): refused (409) while a run holds a lease on a loaded
    model, unless forced. peers_only (localmodels off --peers): the gateway keeps admitting requests and sends them to its
    peers, so no run loses its model and nothing is refused for a lease; this Mac's instances are unloaded once the requests
    in flight on them finish. Already off: the mode changes when it differs. -> (HTTP code, body)."""
    cur = maint.switched_off()
    if cur and bool(cur.get('peers')) == bool(peers_only): return 200, dict(ok=True, note='already switched off', maintenance=maint.view())
    if cur:
        maint.switch_off([], why=why, peers=peers_only)
        crashes.nudge()
        w = _peer_words() if peers_only else ''
        return 200, dict(ok=True, note=('requests now go to peers only' if peers_only else 'requests now get 503; peers are no longer used') + (f'; {w}' if w else ''),
                         maintenance=maint.view())
    rows = llmshare.loaded(fresh=True)
    ids = [r['id'] for r in rows]
    blockers = llmshare.swap_blockers(ids) if ids and not peers_only else []
    if blockers and not force:
        return 409, dict(ok=False, blockers=blockers, error='in use: ' + '; '.join(blockers) + ' (force to switch off anyway, or switch to peers only)')
    fams = set((llmshare.settings().get('families') or {}).keys())
    maint.switch_off(sorted({llmshare.family(i) for i in ids} & fams), why=why, peers=peers_only)
    crashes.nudge()                                          # in the gateway: the instance manager unloads now, not in 20 s
    if peers_only:
        w = _peer_words()
        return 200, dict(ok=True, note='switched off, peers only: new requests go to the peers; every instance here is unloaded once the requests in flight on it finish'
                         + (f'; {w}' if w else ''), maintenance=maint.view())
    return 200, dict(ok=True, note='switched off: new requests get 503; every instance is unloaded once the requests in flight finish',
                     maintenance=maint.view())


def off_pass(off):
    """Switched off (maint.switch_off): once no request is in flight, unload every instance LM Studio lists, under the swap
    lock, and see it gone from LM Studio's own listing; then record it done, so a model a person loads by hand afterwards
    is left alone. Until then each pass tries again. Peers only: the requests going to peers never end, so it waits only
    for those on this Mac's own instances (their in-flight markers, and every slot held on them, a program's own included).
    -> words."""
    did = ['switched off: crash recovery, stalls and the recycle under way cancelled (switching on starts them afresh)'] if crashes.switched_off() else []
    restoring = maint.switching()
    if restoring and restoring.get('phase') == 'loading':
        return did + ['switching on: restoring the families loaded before; unload remains paused']
    if off.get('unloaded'):
        return did + [('switched off, peers only: requests go to the peers' if off.get('peers') else 'switched off')
                      + '; instance manager paused (no loads, reloads, recycles or server starts)']
    if off.get('peers'):                                     # slot locks are this Mac's alone: a peer's slots are counted by the link
        n = max(len(maint.local_work()), sum(llmshare.busy_slots().values()))
    else: n = len(llmshare.inflight())
    if n: return did + [f'switching off: waiting for {n} request{"s" if n != 1 else ""} in flight' + (' on this Mac' if off.get('peers') else '') + ' to finish before unloading']
    if not server_up():
        maint.off_done(off['at'], "LM Studio's server is not running: nothing loaded"); return did + ["switched off: LM Studio's server is not running, nothing to unload"]
    with llmshare.swapping(wait=0) as got:
        if not got: return did + ['switching off: a swap is under way; the unload is tried on the next pass']
        for _ in range(12):
            now = _listed()
            if now is None: return did + ["switching off: LM Studio's listing did not answer; the unload is tried on the next pass"]
            if not now: break
            ident = sorted(now, key=lambda i: (':' not in i, i))[0]   # :N replicas first: a bare id names the oldest instance of its key
            ok, words, gone = unload_exactly(ident)
            did.append(f"{ident} unloaded" + (f" (LM Studio unloaded {', '.join(gone)})" if gone and gone != [ident] else '') if gone else f'{ident}: unload failed: {words[-160:]}')
        left = _listed()
        if left == set():
            maint.off_done(off['at'], '; '.join(did))
            did.append("switched off: no instance listed by LM Studio")
        else: did.append(f"switching off: still listed: {', '.join(sorted(left or [])) or 'listing unavailable'}; tried again on the next pass")
    llmshare.loaded(fresh=True)
    return did


def switch_on():
    """One cross-process restore at a time. Keep the off record until every first copy loads, so failure stays fail-closed
    and a later `localmodels on` can retry; clear it only after a successful restore."""
    with llmshare.flocked(os.path.join(llmshare.RUN, 'switch-on.lock')):
        rec = maint.switched_off()
        if not rec: return ['the local models were not switched off']
        with llmshare.swapping(wait=120) as got:             # an unload under way finishes first; then no pass reads the record
            if not got: return ['a swap or an unload is under way: retry in a moment (localmodels on)']
            rows = llmshare.loaded(fresh=True, force=True)
            crashes.after_maintenance(rows)
        fams = list(rec.get('before') or (llmshare.settings().get('families') or {}).keys())
        maint.on_started([b for b in fams if b not in {r['id'] for r in rows}])
        did = []
        try: did = _load_back(fams, {r['id'] for r in rows}, 'switch-off')
        except Exception as e:
            errors.record('switch on', e)
            did = [f'switch on failed: {type(e).__name__}: {e}']
        finally:
            bad = [w for w in did if 'failed' in w or 'manually' in w or 'not in settings' in w]
            if not bad: maint.switch_on()
            maint.on_done(not bad, '; '.join(bad) if bad else (lambda n: f"{n} instance{'s' if n != 1 else ''} loaded; the instance manager adds the other replicas")(len(llmshare.loaded(fresh=True))))
        return ([f'switch on failed: local models remain switched off; resolve the failure and run localmodels on again'] if bad
                else ['switched on: requests are admitted and the instance manager resumes']) + did


def _again(job, room):
    """Load a crashed model again, every copy it should have, the way it was loaded (under the swap lock). -> words."""
    base, did, t0, tried = job['base'], [], time.time(), False
    blocked = crashes.load_blocked(base, conditions())
    if blocked: return [f'{base}: crash recovery reload skipped: {blocked}']     # it would be refused again: nothing has changed
    f = (llmshare.settings().get('families') or {}).get(base)
    context, parallel = job['context'], job['parallel']
    if f is None and embed_state(base).get('requested_until'):
        f = dict(kind='embedding')                          # a peer-requested embedding uses its observed settings or engine defaults
    if (f or {}).get('kind') != 'embedding': context, parallel = context or 32768, parallel or 4
    for c in job['ids']:
        rows = llmshare.loaded(fresh=True)
        if c in {r['id'] for r in rows}: continue
        why = room_for(f, gb(job['size']), room, rows)     # a dedicated Mac's reload waits only for the GPU's wired limit
        if why:
            crashes.waiting(base, why); did.append(f'{c} crashed; reload deferred: {why}'); return did
        if not tried: crashes.trying(base); tried = True
        t1 = time.time()
        ok, out = load(job['key'], c, context, parallel)
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


def plan(ident, context=None, parallel=None):
    """What a swap to this model would do, including context or slot-count drift: (copies wanted, models to unload)."""
    fams = llmshare.settings().get('families') or {}
    f = fams.get(llmshare.family(ident)) or {}
    want = llmshare.copy_ids(ident, f.get('copies', 1)) if f and llmshare.family(ident) == ident else [ident]
    rows = llmshare.loaded(fresh=True, force=True)
    if f.get('kind') == 'embedding':                        # loading the embedding model unloads nothing else
        context, parallel = context or f.get('context'), parallel or f.get('parallel')
        return want, [r['id'] for r in rows if llmshare.family(r['id']) == llmshare.family(ident)
                      and (r['id'] not in want or (context and r['context'] and r['context'] != int(context))
                           or (parallel and r['parallel'] and r['parallel'] != int(parallel)))]
    context, parallel = int(context or f.get('context') or 32768), int(parallel or f.get('parallel') or 4)
    drop = [r['id'] for r in llmshare.chat_rows(rows, fams)   # a peer-requested embedding also stays loaded through a chat swap
            if r['id'] not in want or (r['context'] and r['context'] != context) or (r['parallel'] and r['parallel'] != parallel)]
    return want, drop


def use(key, ident=None, context=None, parallel=None, dry=False):
    """Swap to one model, with all its copies when it is a family. -> (0 done / 3 refused / 1 failed, words)."""
    ident = ident or key
    if not (_ok_id(key) and _ok_id(ident)): return 1, f'{key!r} is not a model key'
    f = (llmshare.settings().get('families') or {}).get(llmshare.family(ident)) or {}
    if f.get('kind') == 'embedding': context, parallel = f.get('context'), f.get('parallel')   # its family's settings (load() uses them)
    else: context, parallel = int(context or f.get('context') or 32768), int(parallel or f.get('parallel') or 4)
    with llmshare.swapping(wait=120) as got:
        if not got: return 3, 'another swap is under way: try again in a moment'
        want, drop = plan(ident, context, parallel)
        if not llmshare.read_ok(): return 3, "LM Studio did not say which models it holds (lms ps failed): not swapping; try again in a moment"
        with llmshare.swap_guard(drop, loading=want) as why:
            if why: return 3, 'not now: ' + '; '.join(why) + '. Try again when it is done.'
            if dry:
                have = {r['id'] for r in llmshare.loaded()}
                add = [c for c in want if c not in have or c in drop]
                return 0, f"would unload {', '.join(drop) or 'nothing'}; would load {', '.join(add) or 'nothing'}"
            remaining = sorted(drop, key=lambda i: llmshare.family(i) == i)   # :N before a bare key
            for _ in range(2):
                for i in remaining: unload(i)
                now = _listed()
                if now is None: return 1, 'could not verify the loaded instances after unload'
                gone = [i for i in remaining if i not in now]
                if gone: crashes.unloaded_now(gone)
                remaining = [i for i in remaining if i in now]
                if not remaining: break
            if remaining: return 1, f"could not unload {', '.join(remaining)}: still listed"
            for c in want:
                if c in now: continue
                ok, out = load(key, c, context, parallel)
                if not ok: return 1, f'could not load {c}: {out[-300:]}'
            if not ensure_server(): return 1, 'LM Studio server unavailable after swap'
            final = _listed()
            if final is None: return 1, 'could not verify the loaded instances after swap'
            missing, kept = set(want) - final, (set(drop) - set(want)) & final
            if missing: return 1, f"not loaded after swap: {', '.join(sorted(missing))}"
            if kept: return 1, f"still loaded after swap: {', '.join(sorted(kept))}"
            llmshare.loaded(fresh=True, force=True)               # publish the new listing before admissions resume
            crashes.swapped(ident)
    n = len(want)
    return 0, f"{ident} loaded" + (f" as {n} replicas" + (f", {n * parallel} slots" if parallel else '') if n > 1 else '')
