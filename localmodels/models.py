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
"""
import re, subprocess, time, urllib.error, urllib.request

from . import crashes, llmshare, recycle


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
    if not (_ok_id(key) and _ok_id(ident)): return False, f'refused: {key!r} / {ident!r} is not a model id'
    ok, out = _lms('load', key, '--identifier', ident, '-c', str(int(context)), '--parallel', str(int(parallel)), '-y')
    if ok: crashes.loaded_now([ident])
    return ok, out


def unload(ident):
    if not _ok_id(ident): return False, f'refused: {ident!r} is not a model id'
    return _lms('unload', ident, timeout=120)


def keep():
    """One pass: load again a model that crashed (crashes.watch), any copy of a family that is missing for sure while its
    first copy is loaded (if it fits), and recycle a copy that is due (recycle.py). -> what it did, in words."""
    conf = llmshare.settings()
    fams, room, did = conf.get('families') or {}, float(conf.get('headroom_gb', 8)), []
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
            first, free = ids[base], free_gb()
            if free < gb(first['size']) + room:
                did.append(f"{c} waits for room: {free:.0f} GB free, a copy needs {gb(first['size']) + room:.0f} with headroom"); break
            ok, out = load(f.get('key') or first['key'], c, f.get('context') or first['context'], f.get('parallel') or first['parallel'])
            did.append(f'{c} loaded' if ok else f'{c} could not load: {out[-200:]}')
        if job and ready: did += _recycle(job, fams)
    llmshare.loaded(fresh=True)
    return did


def _again(job, room):
    """Load a crashed model again, every copy it should have, the way it was loaded (under the swap lock). -> words."""
    base, did, t0, tried = job['base'], [], time.time(), False
    for c in job['ids']:
        if c in {r['id'] for r in llmshare.loaded(fresh=True)}: continue
        need, free = (gb(job['size']) + room if job['size'] else 0), free_gb()
        if free < need:
            why = f'{free:.0f} GB free, a copy needs {need:.0f} with headroom'
            crashes.waiting(base, why); did.append(f'{c} crashed and waits for room to load again: {why}'); return did
        if not tried: crashes.trying(base); tried = True
        ok, out = load(job['key'], c, job['context'] or 32768, job['parallel'] or 4)
        if not ok:
            crashes.loaded(base, [c], False, time.time() - t0, out)
            did.append(f'{c} crashed and could not be loaded again: {out[-200:]}'); return did
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
    recycle.done(copy, ok, time.time() - t0, '' if ok else out)
    return [f"{copy} recycled in {time.time() - t0:.0f} s: {job['why']}" if ok else f'{copy} could not be loaded back after its recycle: {out[-200:]}']


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
