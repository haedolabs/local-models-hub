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
bigger than the memory macOS lets the GPU hold (iogpu.wired_limit_mb, which resets at every restart).
"""
import json, os, re, subprocess, time, urllib.error, urllib.request

from . import crashes, llmshare, maint, peers, recycle

GUARDRAIL = re.compile(r'insufficient system resources|loading guardrail|resource guardrail', re.I)
_gpu, _cond = {'at': 0.0, 'v': None}, {'at': 0.0, 'v': (0, 0)}


def refused(out):
    """Plain words when LM Studio's output says its loading guardrail stopped a load, else None."""
    if not GUARDRAIL.search(str(out or '')): return None
    return ("LM Studio's model-loading guardrail refused to load it for the hub. A person can load it in LM Studio's window "
            "with Load anyway, or set LM Studio's Settings → Model loading guardrails to Off (the hub checks free memory "
            "itself before every load); the hub does not try again until that setting or the GPU's memory limit changes")


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


def gpu():
    """How much memory this Mac lets the GPU hold: {limit_gb, set (by iogpu.wired_limit_mb), total_gb} (read once a minute).
    Unset, macOS lets it hold about three quarters of memory on a Mac of 36 GB or more, two thirds below."""
    if time.time() - _gpu['at'] < 60 and _gpu['v']: return _gpu['v']
    total = mem_gb()
    try: mb = int(subprocess.run(['sysctl', '-n', 'iogpu.wired_limit_mb'], capture_output=True, text=True, timeout=5).stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError): mb = 0
    v = dict(limit_gb=round(mb / 1024 if mb else total * (0.75 if total >= 36 else 2 / 3), 1), set=bool(mb), total_gb=round(total, 1))
    _gpu.update(at=time.time(), v=v)
    return v


def gpu_words(g, size_gb):
    """Words when a model of size_gb does not fit in what the GPU may hold (with room for its context), else None."""
    if not g or not size_gb or not g.get('limit_gb') or size_gb + 1.5 <= g['limit_gb']: return None
    room = int(g.get('total_gb', 0)) - 8                     # what the GPU may hold with 8 GB left to macOS and the apps
    if size_gb + 1.5 > room:
        return f"the model needs {size_gb:.1f} GB plus its context, more than this Mac can give the GPU and still run ({room} GB): a smaller quantization fits"
    mb = room * 1024
    return (f"this Mac lets the GPU hold about {g['limit_gb']:.0f} GB and the model needs {size_gb:.1f} GB plus its context: "
            f"run sudo sysctl iogpu.wired_limit_mb={mb} on it, then quit and open LM Studio again (the setting resets at "
            "every restart: GUIDE.md, Peers, says how to make it stay)")


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
        return dict(families={}, **base, why="LM Studio's lms command could not list the downloaded models: the keeper manages nothing until a family is added here")
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
                   + ('; one copy, since two do not fit.' if measured else '; the recycle band was measured on another model: '
                      'read `localmodels crashes` after a few long runs and adjust it.'))
    return dict(families={m['modelKey']: fam}, batch_at_once=copies * parallel, headroom_gb=base['headroom_gb'])


def _ok_id(s):
    """A model key or identifier that is safe to hand to lms (never one that would read as a flag)."""
    s = str(s or '')
    return bool(s) and not s.startswith('-') and not re.search(r'[\s\x00-\x1f]', s)


def server_up(timeout=3):
    """Whether LM Studio's server is up: only a refused connection means down (a slow answer means up and busy)."""
    try: urllib.request.urlopen(llmshare.LMSTUDIO + '/v1/models', timeout=timeout); return True
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
    if not _ok_id(ident): return False, f'refused: {ident!r} is not a model id'
    return _lms('unload', ident, timeout=120)


def keep():
    """One pass: load again a model that crashed (crashes.watch), any copy of a family that is missing for sure while its
    first copy is loaded (if it fits), and recycle a copy that is due (recycle.py). -> what it did, in words."""
    conf = llmshare.settings()
    fams, room, did = conf.get('families') or {}, float(conf.get('headroom_gb', 8)), []
    try:
        if maint.tick(llmshare.inflight(), llmshare.requests_log(0.1)): did.append('maintenance starts: the batches are quiet')
    except Exception as e: did.append(f'the maintenance look stumbled: {type(e).__name__}: {e}')
    if maint.on():                                           # the person is working on LM Studio: note what is loaded, do nothing
        rows = llmshare.loaded(fresh=True)
        if llmshare.read_ok(): maint.freeze(llmshare.family(r['id']) for r in rows)
        return did + ['maintenance: the keeper stands down (no loads, reloads, recycles or server starts)']
    began = time.time()
    rows = llmshare.loaded(fresh=True)
    if not llmshare.read_ok(): return did                   # the listing failed or is unsure: nothing to decide on
    busy = llmshare.busy_slots()
    try:
        back = crashes.watch(rows, listed_at=began, busy=busy)
        if crashes.stale_down(began):                       # a crash newer than the listing in hand: list again, then decide
            time.sleep(1); began = time.time(); rows = llmshare.loaded(fresh=True, force=True)
            if not llmshare.read_ok(): return did
            busy = llmshare.busy_slots(); back = crashes.watch(rows, listed_at=began, busy=busy)
    except Exception as e: back = []; did.append(f'the crash watch stumbled: {type(e).__name__}: {e}')
    have = {r['id'] for r in rows}
    down = set((crashes._read(fresh=True).get('down') or {}))
    todo = []
    for b, f in fams.items():
        if b not in have or b in down: continue             # the first copy is not loaded, or the crash machinery has the family
        want = [c for c in llmshare.copy_ids(b, f.get('copies', 1)) if c not in have]
        if want:
            gone = crashes.confirm_missing(want, rows, busy)
            todo += [(b, f, c) for c in want if c in gone]
    try:
        w = recycle.give_up_if_stuck() or recycle.plan(rows, busy)
        if w: did.append(w)
    except Exception as e: did.append(f'the recycle plan stumbled: {type(e).__name__}: {e}')
    job, ready = recycle.ready(rows, busy)
    if down: job, ready = None, False                        # a family down after a crash: its reload first, no recycle in the same pass
    if not todo and not back and not (job and ready): return did
    with llmshare.swapping(wait=0) as got:
        if not got: return did                              # a swap is under way: the next pass looks again
        for j in back: did += _again(j, room)
        for base, f, c in todo:
            ids = {r['id']: r for r in llmshare.loaded(fresh=True)}
            if base not in ids or c in ids: continue        # it changed meanwhile
            blocked = crashes.load_blocked(base, conditions())
            if blocked: did.append(f'{c} is not loaded: {blocked}'); break
            first, free = ids[base], free_gb()
            if free < gb(first['size']) + room:
                did.append(f"{c} waits for room: {free:.0f} GB free, a copy needs {gb(first['size']) + room:.0f} with headroom"); break
            ok, out = load(f.get('key') or first['key'], c, f.get('context') or first['context'], f.get('parallel') or first['parallel'])
            did.append(f'{c} loaded' if ok else f'{c} could not load: {refused(out) or out[-200:]}')
        if job and ready: did += _recycle(job, fams)
    llmshare.loaded(fresh=True)
    return did


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
        if not got: return [f"maintenance is over, but a swap is under way: load {', '.join(todo)} by hand (localmodels use <model>)"]
        for b in todo:
            f = fams.get(b)
            if not f: did.append(f'{b} was loaded before but is not in the settings: load it by hand (localmodels use {b})'); continue
            ok, out = load(f.get('key') or b, b, f.get('context') or 32768, f.get('parallel') or 4)
            did.append(f'{b} loaded again after maintenance' if ok else f'{b} could not be loaded again: {out[-200:]}')
    llmshare.loaded(fresh=True)
    return did


def _again(job, room):
    """Load a crashed model again, every copy it should have, the way it was loaded (under the swap lock). -> words."""
    base, did, t0, tried = job['base'], [], time.time(), False
    blocked = crashes.load_blocked(base, conditions())
    if blocked: return [f'{base} crashed and is not loaded again: {blocked}']     # it would be refused again: nothing has changed
    for c in job['ids']:
        if c in {r['id'] for r in llmshare.loaded(fresh=True)}: continue
        need, free = (gb(job['size']) + room if job['size'] else 0), free_gb()
        if free < need:
            why = f'{free:.0f} GB free, a copy needs {need:.0f} with headroom'
            crashes.waiting(base, why); did.append(f'{c} crashed and waits for room to load again: {why}'); return did
        if not tried: crashes.trying(base); tried = True
        ok, out = load(job['key'], c, job['context'] or 32768, job['parallel'] or 4)
        if not ok:
            crashes.loaded(base, [c], False, time.time() - t0, refused(out) or out)
            did.append(f'{c} crashed and could not be loaded again: {refused(out) or out[-200:]}'); return did
    if not tried: return did                               # someone else loaded it meanwhile: the next look records it
    ensure_server()                                        # LM Studio may turn its server off when a model changes
    crashes.loaded(base, job['ids'], True, time.time() - t0)
    did.append(f"{', '.join(job['ids'])} crashed and {'were' if len(job['ids']) > 1 else 'was'} loaded again ({time.time() - t0:.0f} s)")
    return did


def _recycle(job, fams):
    """Unload a drained copy and load it again with its family's settings (under the swap lock). -> words."""
    copy, base, t0 = job['copy'], job['family'], time.time()
    f = fams.get(base) or {}
    recycle.reloading(copy)
    unload(copy)
    ok, out = load(f.get('key') or base, copy, f.get('context') or 32768, f.get('parallel') or 4)
    ensure_server()
    recycle.done(copy, ok, time.time() - t0, '' if ok else refused(out) or out)
    return [f"{copy} recycled in {time.time() - t0:.0f} s: {job['why']}" if ok else f'{copy} could not be loaded back after its recycle: {refused(out) or out[-200:]}']


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
        for i in drop: unload(i)
        for c in add:
            ok, out = load(key, c, context, parallel)
            if not ok: return 1, f'could not load {c}: {out[-300:]}'
        ensure_server()                                        # LM Studio may turn its server off when a model changes
        llmshare.loaded(fresh=True)
        crashes.swapped(ident)                                 # a person chose: no crashed model is waiting to come back
    n = len(want)
    return 0, f"{ident} loaded" + (f" as {n} copies, {n * parallel} answers at once" if n > 1 else '')
