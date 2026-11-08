#!/usr/bin/env python3
"""llmshare - one LM Studio on this Mac, shared by every project and Claude Code session without collisions.

The hub's door (gateway.py, on http://localhost:1235) applies these rules to every request that comes
through it, so a project that sends its requests to the hub needs none of this code. A project that has to talk to LM Studio
itself imports this copy:

    import os, sys; sys.path.append('<where the hub is checked out>')
    from localmodels import llmshare

The shared state, in ~/Library/Application Support/LocalModels/ (no file here ever holds a prompt, an answer, a file's
contents or anything else a project sent):
  slots/<model>/slot-N.lock    one lock per prediction LM Studio runs at once for that model (its Max Concurrent
                               Predictions, from `lms ps`). Every request holds one while it runs, so all projects together
                               never send more than the model takes; the OS frees a lock when its process dies.
  leases/<project>-<pid>-<thread>.json   "this run is using these models": project, model ids, a kind, the Claude
                               session if there is one; refreshed every 30 s, gone when the run ends. The hub writes one
                               for every program that sends it requests, kept for two minutes after its last request.
  inflight/<hub pid>-<n>.json  each request the hub is passing to LM Studio right now
  requests.jsonl               one line per request the hub passed on: project, session, model, times, token counts
  waits.jsonl                  every wait for a slot over 2 s, every wait that ran out, and every refused swap
  models.json                  the hub's settings (families of copies, recycling, batch size, headroom); the repo's
                               localmodels/models.json is the default until this one exists

The rules every project follows:
  1. Send requests to the hub, http://localhost:1235/v1 (the same API as LM Studio's). Code that talks to LM Studio
     itself holds a slot for every request: with llmshare.slot(model): ...  (llmshare.post does the right one). Never hold
     a slot of your own for a request you send through the hub: the door takes one for it, and the two would wait on
     each other.
  2. Every run that lasts holds a lease: with llmshare.lease('<project>', [model], 'batch'): ...
  3. Before unloading or reloading a model (a swap, a context change), ask llmshare.swap_blockers(models): if anything
     is using one of them, do not swap: say who is using it and stop.
  4. Never stop LM Studio's server (starting it is safe at any time). Keep your own logs, ports and MCP server name.

  python3 llmshare.py          what is using LM Studio now: models, runs by project, requests running, the last week
"""
import contextlib, fcntl, glob, itertools, json, os, re, shutil, subprocess, threading, time, urllib.error, urllib.request

SHARED = os.path.expanduser(os.environ.get('LOCALMODELS_STATE') or '~/Library/Application Support/LocalModels')   # the shared state (LOCALMODELS_HOME names the repo, for projects that import it)
SLOTS, LEASES, INFLIGHT = (os.path.join(SHARED, d) for d in ('slots', 'leases', 'inflight'))
WAITS, REQUESTS = os.path.join(SHARED, 'waits.jsonl'), os.path.join(SHARED, 'requests.jsonl')
LMS = os.path.expanduser(os.environ.get('LMS') or '~/.lmstudio/bin/lms')
if not os.path.exists(LMS): LMS = shutil.which('lms') or LMS
LMSTUDIO = os.environ.get('LMSTUDIO_URL', 'http://localhost:1234')
HUB_PORT = int(os.environ.get('LOCALMODELS_PORT') or 1235)
HUB = f'http://localhost:{HUB_PORT}'
STALE, BEAT = 120, 30                                    # seconds: a lease older than STALE is dead; refreshed every BEAT
KINDS = ('batch', 'chat', 'eval', 'test', 'ask', 'other')
PEOPLE = ('chat', 'ask')                                 # kinds a person is waiting on: the next free slot is theirs
VERSION = 2                                              # the lease file format
LISTING_EVERY, LISTING_LEAST, PS_TIMEOUT = 20, 5, 20     # seconds: `lms ps` is read this often at most, never more often than this, and given this long
_ps = {'at': 0.0, 'rows': [], 'ok': False, 'good_at': 0.0, 'tried': 0.0}
_ps_lock = threading.Lock()
_api = {'at': 0.0, 'ids': None}
_hub = {'at': 0.0, 'up': False}
_q, _q_lock, _seq = {}, threading.Lock(), itertools.count()   # this process's waiters for a slot: ticket -> the copies it may take


def _int(x):
    try: return int(x)
    except (TypeError, ValueError): return 0


def _safe(s): return re.sub(r'[^A-Za-z0-9._-]+', '_', str(s))[:120]


def clean(s, n=80):
    """A header value or a name as the hub records and shows it: no control characters, at most n characters."""
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', str(s or '')).strip()[:n]


def _append(path, rec, roll_at=0):
    """One JSON line, appended under a lock so two processes never interleave; past roll_at bytes the file becomes <path>.1."""
    try:
        os.makedirs(SHARED, exist_ok=True)
        with open(path, 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(rec) + '\n')
            if roll_at and f.tell() > roll_at and os.path.getsize(path) > roll_at: os.replace(path, path + '.1')
    except OSError: pass


def _note(kind, model, seconds=0.0):
    """One line in waits.jsonl: a request that waited for a slot, or a swap that was refused. No content, ever."""
    _append(WAITS, dict(at=round(time.time()), kind=kind, model=model, seconds=round(seconds, 1)), roll_at=2_000_000)


def _write(path, rec):
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    with open(tmp, 'w') as f: json.dump(rec, f)
    os.replace(tmp, path)


# ---------------------------------------------------------------- what LM Studio holds
def _lms_ps():
    """-> (rows, ok) from `lms ps --json` (the text table when an older lms has no --json). A row LM Link lists from another
    Mac (its deviceIdentifier set) is left out: it is not a copy on this Mac (the hub links Macs itself: peers.py). Each
    row keeps what names the model across Macs: the variant LM Studio loaded, its size in bytes and its format."""
    try: r = subprocess.run([LMS, 'ps', '--json'], capture_output=True, text=True, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError): return [], False
    out = r.stdout.strip()
    if r.returncode == 0 and out.startswith('['):
        try: data = json.loads(out)
        except ValueError: data = None
        if isinstance(data, list):
            rows = []
            for m in data:
                if not isinstance(m, dict) or not m.get('identifier') or m.get('deviceIdentifier'): continue
                rows.append(dict(id=m['identifier'], key=m.get('modelKey') or m['identifier'], state=str(m.get('status') or 'idle').upper(),
                                 size=f"{(m.get('sizeBytes') or 0) / 2**30:.2f} GB", context=_int(m.get('contextLength')), parallel=_int(m.get('parallel')),
                                 queued=_int(m.get('queued')), type=m.get('type'), variant=m.get('selectedVariant') or None,
                                 bytes=m.get('sizeBytes') if isinstance(m.get('sizeBytes'), int) else None, format=m.get('format')))
            return rows, True
    try: r = subprocess.run([LMS, 'ps'], capture_output=True, text=True, timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError): return [], False
    ok = r.returncode == 0 and ('IDENTIFIER' in r.stdout or 'No models' in r.stdout + r.stderr)
    rows = []
    for line in r.stdout.split('\n'):                    # IDENTIFIER MODEL STATUS SIZE(2 words) CONTEXT PARALLEL ...
        p = line.split()
        if len(p) >= 7 and p[2].isupper() and p[0] != 'IDENTIFIER':
            rows.append(dict(id=p[0], key=p[1], state=p[2], size=' '.join(p[3:5]), context=_int(p[5]), parallel=_int(p[6]), queued=0, type=None))
    return rows, ok


def loaded(fresh=False, force=False):
    """The models LM Studio holds, from `lms ps --json`: [{id, key, state, size, context, parallel, queued}]. Read at most
    every 20 s (fresh=True: now, but never twice within 5 s unless force=True), by one thread at a time; a thread that is
    not asking for a fresh look never waits. When the command fails or hangs, which it does under heavy load, the last good
    listing stands and read_ok() says it is unsure, rather than an empty listing that would make the door route as if
    nothing were loaded."""
    now = time.time()
    due = fresh or now - _ps['at'] > LISTING_EVERY
    if not due or (now - _ps['tried'] < LISTING_LEAST and not force): return _ps['rows']
    if not _ps_lock.acquire(blocking=fresh): return _ps['rows']
    try:
        now = time.time()
        if now - _ps['tried'] < LISTING_LEAST and not force: return _ps['rows']
        _ps['tried'] = now
        rows, ok = _lms_ps()
        if ok: _ps.update(at=now, rows=rows, ok=True, good_at=now)
        else: _ps.update(at=now, ok=False)
        return _ps['rows']
    finally: _ps_lock.release()


def read_ok():
    """Whether the last `lms ps` answered whole: when it did not, nobody can tell what a swap would unload, so do not swap,
    and a copy that seems missing is unsure."""
    return _ps['ok']


def listing_age():
    """Seconds since the last good listing (a big number when there has never been one)."""
    return time.time() - _ps['good_at'] if _ps['good_at'] else 1e9


def api_models(timeout=3):
    """What LM Studio's own server says is loaded (GET /api/v0/models, cached 2 s): {id: context} or None when it did not
    answer. Cheap, and from the server itself, so it is the cross-check when `lms ps` and the door disagree."""
    if time.time() - _api['at'] < 2: return _api['ids']
    try:
        d = json.load(urllib.request.urlopen(LMSTUDIO + '/api/v0/models', timeout=timeout))
        ids = {m['id']: m.get('loaded_context_length') for m in d.get('data', []) if m.get('state') == 'loaded' and m.get('id')}
    except (OSError, ValueError, KeyError, TypeError): ids = None
    _api.update(at=time.time(), ids=ids)
    return ids


def parallel_of(model):
    """How many requests LM Studio answers at once for this model (1 when it is not listed: the safe side)."""
    m = next((r for r in loaded() if r['id'] == model), None)
    return max(1, m['parallel'] or 1) if m else 1


# ---------------------------------------------------------------- copies: one model loaded more than once
MODELS_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models.json')   # the repo's defaults
MODELS = os.path.join(SHARED, 'models.json')                                                 # the live settings on this Mac
SWAP_LOCK = os.path.join(SHARED, 'swap.lock')
_conf = {'path': None, 'mtime': None, 'data': {}, 'error': None}


def settings_path():
    return MODELS if os.path.exists(MODELS) else MODELS_DEFAULT


def settings():
    """The hub's settings: {"families": {id: {key, copies, context, parallel, recycle}}, "batch_at_once", "headroom_gb"}.
    A file that cannot be read (bad JSON, half written) leaves the last good settings standing; settings_error() says so."""
    p = settings_path()
    try: m = os.path.getmtime(p)
    except OSError: return _conf['data']
    if (p, m) != (_conf['path'], _conf['mtime']):
        try:
            d = json.load(open(p))
            if not isinstance(d, dict): raise ValueError('not a JSON object')
            _conf.update(path=p, mtime=m, data=d, error=None)
        except (OSError, ValueError) as e: _conf.update(path=p, mtime=m, error=f'{p}: {type(e).__name__}: {e}')
    return _conf['data']


def settings_error():
    """Why the settings file could not be read, or None (the last good settings stand meanwhile)."""
    return _conf['error']


def family(model):
    """The id a model's copies share: qwen/qwen3.6-35b-a3b:2 -> qwen/qwen3.6-35b-a3b (a model loaded once is its own), and a
    copy on a peer Mac, 'qwen/qwen3.6-35b-a3b on macbook' (peers.py), is a copy of the same family."""
    s = str(model or '')
    on = ' on ' in s
    if on: s = s.split(' on ', 1)[0]
    m = re.match(r'^(.+):(\d+)$', s)
    return m.group(1) if m else (s if on else model)


def copy_ids(base, n):
    """The ids of n copies: the model's own, then <id>:2, <id>:3 ..."""
    return [base] + [f'{base}:{i}' for i in range(2, int(n) + 1)]


def copies(model, fresh=False):
    """The loaded copies a request for this model may go to, first copy first (one still loading is left out). A request
    naming one copy (<id>:2) gets that copy while it is loaded, else its family's loaded copies; [model] when none is loaded
    and the family is unknown to the settings, [] when the settings know it (the door then holds or answers 503, never
    forwards the bare id for LM Studio to load on its own terms)."""
    base = family(model)
    rows = [r for r in loaded(fresh) if family(r['id']) == base and r['state'] != 'LOADING']
    ids = [r['id'] for r in rows]
    if base != model and model in ids: return [model]
    mine = sorted(ids, key=lambda i: (i != base, len(i), i))
    if mine: return mine
    return [] if base in (settings().get('families') or {}) else [model]


def at_once(model=None):
    """How many answers a model's loaded copies give at once in all (two copies of 4: 8). model None: the loaded model
    that gives the most. Batches through the hub send this many."""
    rows = loaded()
    if model is None:
        fams = {family(r['id']) for r in rows}
        try:
            from . import peers                              # the families this Mac's peers serve for it count too
            fams |= {f for p in peers.view() for f in (p.get('families') or {})}
        except Exception: pass
        return max([at_once(f) for f in fams] or [1])
    ids = {r['id'] for r in rows}
    here = sum(parallel_of(c) for c in copies(model) if c in ids)
    try:
        from . import peers
        there = peers.capacity(model)
    except Exception: there = 0
    return (here + there) or parallel_of(model)


@contextlib.contextmanager
def swapping(wait=120):
    """The swap lock: held while loading or unloading models (the hub's keeper and every swap take it), so two never
    interleave. Yields True when held, False when another swap held it for `wait` seconds (then do not swap)."""
    os.makedirs(SHARED, exist_ok=True)
    h, t0, got = open(SWAP_LOCK, 'a+'), time.time(), False
    while True:
        try: fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB); got = True; break
        except OSError:
            if time.time() - t0 >= wait: break
            time.sleep(0.5)
    try: yield got
    finally:
        if got: fcntl.flock(h, fcntl.LOCK_UN)
        h.close()


# ---------------------------------------------------------------- rule 1: a slot per request
REMOTE = None              # the door's link to its peers (peers.LINK): it counts their slots, which only the door sends to


class _Remote:
    """A slot held on a peer Mac's copy (peers.Link.take), given back like a lock."""
    def __init__(self, c): self.c = c

    def release(self):
        if REMOTE is not None: REMOTE.give(self.c)


def _release(h):
    if isinstance(h, _Remote): h.release(); return
    try: fcntl.flock(h, fcntl.LOCK_UN)
    finally: h.close()


def _take(models):
    """Try each model's slots once (a copy on a peer, '<family> on <peer>', by the door's link); -> (the lock handle,
    the model) or (None, None)."""
    for m in models:
        if ' on ' in m:
            if REMOTE is not None and REMOTE.take(m): return _Remote(m), m
            continue
        d = os.path.join(SLOTS, _safe(m)); os.makedirs(d, exist_ok=True)
        for i in range(parallel_of(m)):
            h = open(os.path.join(d, f'slot-{i}.lock'), 'a+')
            try: fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB); return h, m
            except OSError: h.close()
    return None, None


def waiting_here():
    """How many requests in this process are waiting for a slot right now, by kind: {'people': n, 'others': n}."""
    with _q_lock: return dict(people=sum(1 for t in _q if t[0] == 0), others=sum(1 for t in _q if t[0] != 0))


@contextlib.contextmanager
def slot_any(models, wait=3600, kind='other'):
    """Hold one prediction slot on the first of these models that has one free: a model's copies, in order, so the first
    copy's slots fill before the second's (two on each measured slower than four on one). models: a list, or a function
    that gives the list each time it is tried, so a copy that comes back or is being drained counts at once. Waiters in
    this process take turns: a chat or ask request (a person is waiting) before anything else, then first come first
    served, per copy: a waiter leaves a copy's slot to an earlier waiter that may take it. Slots held by other processes
    (llmshare.slot in a project's own code) are simply busy. Yields (the model, seconds waited); TimeoutError after `wait` s."""
    pick = models if callable(models) else (lambda: list(models))
    ticket, t0, held, got = (0 if kind in PEOPLE else 1, next(_seq)), time.time(), None, None
    try:
        while held is None:
            cands = list(pick() or [])
            with _q_lock:
                _q[ticket] = cands
                ahead = {m for t, ms in _q.items() if t < ticket for m in ms}
            mine = [m for m in cands if m not in ahead]
            if mine: held, got = _take(mine)
            if held is None:
                if time.time() - t0 > wait:
                    _note('slot timeout', family(cands[0]) if cands else '?', time.time() - t0)
                    raise TimeoutError(f"no free LM Studio slot for {', '.join(cands) or 'the model'} after {wait:.0f} s")
                time.sleep(0.2 if time.time() - t0 < 10 else 0.5)
    finally:
        with _q_lock: _q.pop(ticket, None)
    waited = time.time() - t0
    if waited > 2: _note('slot wait', family(got), waited)
    try: yield got, waited
    finally: _release(held)


@contextlib.contextmanager
def slot(model, wait=3600):
    """Hold one of the model's prediction slots for one request; waits while every slot is busy. For code that talks to
    LM Studio itself; a request sent through the hub gets its slot from the door."""
    with slot_any([model], wait) as (_, waited): yield waited


def busy_slots():
    """{model: requests running now}, read by trying each slot lock."""
    out = {}
    for d in glob.glob(os.path.join(SLOTS, '*')):
        n = 0
        for f in glob.glob(os.path.join(d, 'slot-*.lock')):
            h = open(f, 'a+')
            try:
                fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(h, fcntl.LOCK_UN)
            except OSError: n += 1
            finally: h.close()
        if n: out[os.path.basename(d)] = n
    return out


# ---------------------------------------------------------------- rule 2: a lease per run
class Held(dict):
    """A lease while it is held: lease.step('reading the plan') or lease.progress(3, 12) shows on the dash at once."""
    def __init__(self, path, rec):
        super().__init__(rec); self.path = path

    def save(self):
        try: _write(self.path, dict(self, beat=time.time()))
        except OSError: pass

    def step(self, words):
        self['step'] = clean(words, 120); self.save()

    def progress(self, done, total):
        self['done'], self['total'] = int(done), int(total); self.save()


def me():
    """This process's Claude Code session, when Claude Code started it (it sets these in every shell it runs)."""
    e = os.environ
    return {k: v for k, v in (('session', e.get('CLAUDE_CODE_SESSION_ID')), ('host_session', e.get('CLAUDE_CODE_HOST_SESSION_ID')))
            if v}


@contextlib.contextmanager
def lease(project, models, kind='other', **info):
    """Say 'this run is using these models' for as long as the block lasts (a heartbeat keeps it live). info: optional
    fields the dash shows, such as log_dir (a batch folder with plan.json and a .running file per job at work)."""
    os.makedirs(LEASES, exist_ok=True)
    path = os.path.join(LEASES, f'{_safe(project)}-{os.getpid()}-{threading.get_ident()}.json')
    rec = dict(v=VERSION, project=clean(project), pid=os.getpid(), models=[models] if isinstance(models, str) else [m for m in models if m],
               kind=kind if kind in KINDS else 'other', since=time.time(), **me(), **{k: v for k, v in info.items() if v is not None})
    held, stop = Held(path, rec), threading.Event()

    def beat():
        while not stop.wait(BEAT): held.save()
    held.save()
    th = threading.Thread(target=beat, daemon=True); th.start()
    try: yield held
    finally:
        stop.set(); th.join(2)
        try: os.remove(path)
        except OSError: pass


def _alive(pid):
    try: os.kill(int(pid), 0); return True
    except PermissionError: return True
    except (OSError, TypeError, ValueError): return False


def leases():
    """Every live lease, all projects; a dead one (its process gone, or no heartbeat for STALE s) is removed."""
    out = []
    for f in glob.glob(os.path.join(LEASES, '*.json')):
        try: d = json.load(open(f))
        except (OSError, ValueError): continue
        if not int(d.get('pid') or 0) or not _alive(d.get('pid')) or time.time() - float(d.get('beat') or 0) > STALE:
            try: os.remove(f)
            except OSError: pass
            continue
        out.append(d)
    return out


# ---------------------------------------------------------------- rule 3: no swap under someone else's run
def swap_blockers(models):
    """Why unloading or reloading these models must wait, in plain words; empty means the swap is safe. A run's own lease
    does not count against itself (a run may swap its own model); the hub's leases for other programs always count."""
    ms, why, seen = set(models or []), [], set()
    fams = {family(m) for m in ms}                        # a run on one copy may be sent to any copy of it next
    loaded(fresh=True)
    if not _ps['ok']:
        return ["LM Studio did not say which models it holds (lms ps failed): try again in a moment"]
    for d in sorted(leases(), key=lambda d: d.get('since', 0)):
        hit = sorted({m for m in d.get('models') or [] if family(m) in fams})
        if (d.get('pid') == os.getpid() and d.get('via') != 'hub') or not hit: continue
        key = (d.get('project'), d.get('pid'), d.get('from'))
        if key in seen: continue
        seen.add(key)
        since = time.strftime('%H:%M', time.localtime(float(d.get('since') or 0)))
        what = 'requests through the hub' if d.get('via') == 'hub' and d.get('kind') == 'other' else f"a {d.get('kind')}"
        why.append(f"{what} in the {d.get('project')} project{' on ' + str(d['from']) if d.get('from') else ''} (since {since}) is using {', '.join(hit)}")
    if not why:
        for r in loaded():
            if r['id'] in ms and r['state'] in ('GENERATING', 'PROCESSING'):
                why.append(f"{r['id']} is answering something right now (LM Studio's own chat, or another app)")
    if why: _note('swap refused', ','.join(sorted(ms)))
    return why


# ---------------------------------------------------------------- talking to LM Studio: through the hub
def hub_up(fresh=False):
    """Whether the hub's door is answering (cached 5 s). A door that is slow to answer is up (something is listening and
    busy); only a refused connection means it is down."""
    if fresh or time.time() - _hub['at'] > 5:
        try: up = json.load(urllib.request.urlopen(HUB + '/hub/health', timeout=3)).get('ok') is True
        except urllib.error.URLError as e: up = not isinstance(getattr(e, 'reason', None), ConnectionRefusedError)
        except ConnectionRefusedError: up = False
        except (OSError, ValueError): up = True
        _hub.update(at=time.time(), up=up)
    return _hub['up']


def base_url():
    """Where to send requests: the hub when it is up, LM Studio itself when it is not."""
    return HUB if hub_up() else LMSTUDIO


def get(path, timeout=10):
    return json.load(urllib.request.urlopen(base_url() + path, timeout=timeout))


def post(path, body, project, kind='other', timeout=3600, headers=None):
    """One request to the local model, by the rules: through the hub, which names the project, takes a slot and logs
    it; or, when the hub is down, straight to LM Studio holding a slot here. headers: more for the hub (X-Step, X-Session)."""
    data, base = json.dumps(body).encode(), {'Content-Type': 'application/json'}
    if hub_up():
        hub_headers = dict(base, **{'X-Project': str(project), 'X-Timeout': str(int(timeout))}, **(headers or {}))   # the door bounds its wait by it
        if kind in KINDS and kind != 'other': hub_headers['X-Kind'] = kind   # else the hub reads the kind from the run's lease
        req = urllib.request.Request(HUB + path, data=data, headers=hub_headers)
        try: return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError: raise
        except urllib.error.URLError as e:                 # the door is restarting (refused: the request never reached the
            if not isinstance(e.reason, ConnectionRefusedError): raise    # model), so this once go straight to LM Studio
            _hub.update(at=time.time(), up=False)
    req = urllib.request.Request(LMSTUDIO + path, data=data, headers=base)
    with (slot(body['model']) if body.get('model') else contextlib.nullcontext()):
        return json.load(urllib.request.urlopen(req, timeout=timeout))


def hub_post(path, body=None, timeout=5):
    """A small request to the hub itself (/hub/...), or None when the door does not answer."""
    try:
        req = urllib.request.Request(HUB + path, data=json.dumps(body or {}).encode(), headers={'Content-Type': 'application/json'})
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except (OSError, ValueError): return None


# ---------------------------------------------------------------- the hub's records, read
def inflight():
    """Each request the hub is passing on right now (a marker whose hub process is gone, or older than two hours, is removed)."""
    out = []
    for f in glob.glob(os.path.join(INFLIGHT, '*.json')):
        try: d = json.load(open(f))
        except (OSError, ValueError): continue
        if not _alive(d.get('hub_pid')) or time.time() - float(d.get('since') or 0) > 7200:
            try: os.remove(f)
            except OSError: pass
            continue
        out.append(d)
    return out


def _tail(path, since, start=4_000_000):
    """The records of one log file from `since` on: read from the end, and further back while the oldest line read is still newer."""
    out = []
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2); size = f.tell(); tail = start
            while True:
                f.seek(max(0, size - tail))
                lines = f.read().split(b'\n')[1 if size > tail else 0:]
                first = None
                for line in lines[:20]:
                    try: first = json.loads(line); break
                    except ValueError: continue
                if tail >= size or not first or first.get('at', 0) <= since: break
                tail *= 2
    except OSError: return out
    for line in lines:
        try: d = json.loads(line)
        except ValueError: continue
        if d.get('at', 0) >= since: out.append(d)
    return out


def requests_log(hours=1.0, tail=4_000_000):
    """The hub's request records from the last `hours` hours (read from the end of the log, as far back as needed)."""
    since = time.time() - hours * 3600
    return _tail(REQUESTS + '.1', since, tail) + _tail(REQUESTS, since, tail)


def by_project(rows):
    """{project: {requests, tokens, waited, seconds}} from request records."""
    out = {}
    for d in rows:
        p = out.setdefault(d.get('project') or '?', dict(requests=0, tokens=0, waited=0.0, seconds=0.0))
        p['requests'] += 1; p['tokens'] += (d.get('prompt_tokens') or 0) + (d.get('completion_tokens') or 0)
        p['waited'] += d.get('waited') or 0; p['seconds'] += d.get('seconds') or 0
    return out


def waits(days=7):
    """-> (slot waits, total seconds waited, swaps refused) over the last `days` days."""
    n = secs = refused = 0
    for path in (WAITS + '.1', WAITS):
        try:
            for line in open(path):
                try: d = json.loads(line)
                except ValueError: continue
                if time.time() - d.get('at', 0) > days * 86400: continue
                if d.get('kind') in ('slot wait', 'slot timeout'): n += 1; secs += d.get('seconds', 0)
                elif d.get('kind') == 'swap refused': refused += 1
        except OSError: pass
    return n, secs, refused


def main():
    ms = loaded(fresh=True)
    print('LM Studio holds: ' + (', '.join(f"{r['id']} ({r['state'].lower()}, {r['parallel']} at once, context {r['context']:,})" for r in ms)
                                 or 'no model') + ('' if read_ok() else ' (the last listing failed: this is the one before)'))
    print(f"the hub's door ({HUB}): " + ('on' if hub_up(fresh=True) else 'off: projects go straight to LM Studio'))
    ls = leases()
    for d in sorted(ls, key=lambda d: d.get('since', 0)):
        print(f"  {d['project']}: {'requests through the hub' if d.get('via') == 'hub' else d['kind']} on {', '.join(d['models'])} "
              f"since {time.strftime('%H:%M:%S', time.localtime(d['since']))} (pid {d['pid']})")
    if not ls: print('  no run is using a model')
    b = busy_slots()
    print('  requests running: ' + (', '.join(f'{m}: {n}' for m, n in b.items()) if b else 'none'))
    n, secs, refused = waits(7)
    print(f'  last 7 days: {n} requests waited for a slot ({secs / 60:.1f} min in all), {refused} swaps refused '
          '(a model another run was using). Rising numbers mean the projects are waiting on each other.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
