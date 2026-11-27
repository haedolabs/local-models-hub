#!/usr/bin/env python3
"""llmshare - one LM Studio on this Mac, shared by every project and coding session without collisions.

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
   leases/<project>-<pid>-<thread>.json   "this run is using these models": project, model ids, a kind, the coding
                               session if there is one; refreshed every 30 s, gone when the run ends. The hub writes one
                               for every program that sends it requests, kept for two minutes after its last request.
  inflight/<hub pid>-<n>.json  each request a gateway is passing to LM Studio right now, or has queued for a slot (its
                               place in the admission order: kind, since, candidates), live while its gateway is
  run/gateways/<pid>.lock      the gateway registry: each gateway holds its lock for its whole life (live means held, not
                               a pid that may have been reused); <pid>.json is its row (generation, release, role, counts)
  run/roles/<name>.lock        one holder per singleton loop: the instance manager, the peer poller, the memory sampler,
                               the link listener; run/nudge-<name> is touched to wake that loop in whichever gateway holds it
  run/server-start.lock        one `lms server start` at a time across every process, at most one a minute
  listing.json                 the newest good `lms ps`, which every gateway on this Mac routes by
  peer-slots/<peer>/<family>/  the slots this Mac's gateways hold on a peer's instances, counted across all of them
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

STATE_PROTOCOL = 2     # slot and family-swap locks; the names and fields of marks, leases, registry rows, roles,
                       # peer slots and listing.json; keeper.json keys; link.json. Gateways of two protocols never serve at once.
_on_state = []         # what use_state() repoints: this module's paths, then every module's that registered with on_state()


def _paths(root):
    """Every path of the shared state, under one root."""
    global SHARED, SLOTS, LEASES, INFLIGHT, WAITS, REQUESTS, MODELS, MODELS_GOOD, SWAP_LOCK, LISTING, RUN, GATEWAYS, ROLES, PEER_SLOTS
    SHARED = root
    SLOTS, LEASES, INFLIGHT = (os.path.join(root, d) for d in ('slots', 'leases', 'inflight'))
    WAITS, REQUESTS = os.path.join(root, 'waits.jsonl'), os.path.join(root, 'requests.jsonl')
    MODELS, SWAP_LOCK, LISTING = (os.path.join(root, f) for f in ('models.json', 'swap.lock', 'listing.json'))
    MODELS_GOOD = os.path.join(root, 'models.last-good.json')    # the last settings file the gateway read well
    RUN = os.path.join(root, 'run')                          # mode 0700: the registry, roles, nudges, the server-start lock
    GATEWAYS, ROLES = os.path.join(RUN, 'gateways'), os.path.join(RUN, 'roles')
    PEER_SLOTS = os.path.join(root, 'peer-slots')


_paths(os.path.expanduser(os.environ.get('LOCALMODELS_STATE') or '~/Library/Application Support/LocalModels'))   # (LOCALMODELS_HOME names the repo, for projects that import it)


def on_state(fn):
    """fn(root) keeps a module's own paths under the shared state: called now, and again whenever use_state() moves it."""
    _on_state.append(fn); fn(SHARED)
    return fn


def use_state(root):
    """Point the whole shared state at another folder (the sandbox tests): this module's paths, its caches of the
    registry, and every module that registered with on_state(). -> the previous root."""
    old = SHARED
    _paths(root)
    for fn in _on_state: fn(root)
    _live.clear(); _gw.update(at=0.0, rows=[]); _fq.update(at=0.0, rows=[]); _listing.update(checked=0.0, mtime=None)
    return old


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
MARK_STALE = 300                                         # seconds: a registered gateway touches its marks every BEAT; untouched this long, one is dead
_live = {}                                               # pid -> (checked at, True/False/None): gateway_live, cached 1 s
_gw = {'at': 0.0, 'rows': []}                            # live_gateways(), cached 1 s
_fq = {'at': 0.0, 'rows': []}                            # other gateways' queued marks, read at most every 0.25 s
_listing = {'checked': 0.0, 'mtime': None}               # listing.json, looked at (a stat) at most every second


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(req, timeout=None):
    """urllib's urlopen with no proxy: every address the hub calls is this Mac's own (the gateway, LM Studio), so neither
    http_proxy in the environment nor the system's web proxy may see a prompt on its way there."""
    return _opener.open(req, timeout=timeout)


def _int(x):
    try: return int(x)
    except (TypeError, ValueError): return 0


def _safe(s): return re.sub(r'[^A-Za-z0-9._-]+', '_', str(s))[:120]


def clean(s, n=80):
    """A header value or a name as the hub records and shows it: no control characters, at most n characters."""
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', str(s or '')).strip()[:n]


def _error(where, e):
    """The hub's error record, from any process that uses this module (errors.py imports this module, so it is imported here late)."""
    try:
        from . import errors
        errors.record(where, e, source='llmshare')
    except Exception: pass                                   # not recorded: the recorder itself failed; nothing left to tell


_unwritten = {}                                          # the last record this process could not write: path, error, since


def _append(path, rec, roll_at=0):
    """One JSON line, appended under a lock so two processes never interleave; past roll_at bytes the file becomes
    <path>.1. -> True when written; False when not (a full disk, a folder not writable), kept for write_problem()."""
    try:
        os.makedirs(SHARED, exist_ok=True)
        with open(path, 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(rec) + '\n')
            if roll_at and f.tell() > roll_at and os.path.getsize(path) > roll_at: os.replace(path, path + '.1')
    except OSError as e:
        _unwritten.update(path=path, error=e.strerror or str(e), since=_unwritten.get('since') or time.time())
        return False
    _unwritten.clear()
    return True


def write_problem():
    """Words when this process could not write the hub's records lately, else None: the request log feeds the recycle
    counts, admission's bytes per token, batch progress and the charts, so they stand still until it can."""
    if not _unwritten: return None
    return (f"the hub's records are not writable since {time.strftime('%H:%M', time.localtime(_unwritten['since']))} "
            f"({os.path.basename(_unwritten['path'])}: {_unwritten['error']}): recycle counts, batch progress and the charts are frozen")


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
    nothing were loaded. Every good listing is also written to listing.json, and a newer one written by another process
    is taken in its place (_adopt), so every gateway on this Mac routes by the newest listing any of them read."""
    if not fresh: _adopt()
    now = time.time()
    due = fresh or now - _ps['at'] > LISTING_EVERY
    if not due or (now - _ps['tried'] < LISTING_LEAST and not force): return _ps['rows']
    if not _ps_lock.acquire(blocking=fresh): return _ps['rows']
    try:
        now = time.time()
        if now - _ps['tried'] < LISTING_LEAST and not force: return _ps['rows']
        _ps['tried'] = now
        rows, ok = _lms_ps()
        if ok:
            _listing['checked'] = 0.0; _adopt()             # the carry-forward below compares against the newest rows
            listed = {r['id'] for r in rows}
            gone = [r for r in _ps['rows'] if r['id'] not in listed]
            if gone:                                         # under load `lms ps` can leave an instance out: LM Studio's own
                there = api_models()                         # server decides, so a partial listing never takes slots away
                rows = rows + [dict(r) for r in gone if there is not None and r['id'] in there]
            _ps.update(at=now, rows=rows, ok=True, good_at=now)
            _publish_listing(rows, now)
        else: _ps.update(at=now, ok=False)
        return _ps['rows']
    finally: _ps_lock.release()


def _publish_listing(rows, at):
    """listing.json: this good listing, for every other process on this Mac (no content: model ids, states and sizes)."""
    try:
        os.makedirs(SHARED, exist_ok=True)
        _write(LISTING, dict(v=STATE_PROTOCOL, rows=rows, at=at, good_at=at, by=os.getpid()))
        _listing['mtime'] = os.path.getmtime(LISTING)
    except OSError as e: _error('listing.json: write', e)


def _rows_of(d):
    return [dict(r) for r in d.get('rows') if isinstance(r, dict) and r.get('id')] if isinstance(d, dict) and isinstance(d.get('rows'), list) else None


def _adopt():
    """Take listing.json's rows when another process read a good listing after this one did (a stat at most every second)."""
    now = time.time()
    if now - _listing['checked'] < 1: return
    _listing['checked'] = now
    try: m = os.path.getmtime(LISTING)
    except OSError: return
    if m == _listing['mtime']: return
    _listing['mtime'] = m
    try: d = json.load(open(LISTING))
    except (OSError, ValueError): return
    rows, good = _rows_of(d), float((d or {}).get('good_at') or 0) if isinstance(d, dict) else 0.0
    if rows is not None and good > _ps['good_at']: _ps.update(rows=rows, at=good, good_at=good, ok=True)


def seed_listing(max_age=120):
    """A gateway's warm-up: listing.json, when it is younger than max_age seconds, stands as this process's listing until
    its own first `lms ps` (which can take seconds under load). -> whether it was taken."""
    try: d = json.load(open(LISTING))
    except (OSError, ValueError): return False
    rows, good = _rows_of(d), float(d.get('good_at') or 0) if isinstance(d, dict) else 0.0
    if rows is None or time.time() - good > max_age or good <= _ps['good_at']: return False
    _ps.update(rows=rows, at=good, good_at=good, ok=True)
    return True


def listed():
    """The last listing in hand, never read again here (for code that must not wait on `lms ps`, which can take 20 s)."""
    return list(_ps['rows'])


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
        d = json.load(urlopen(LMSTUDIO + '/api/v0/models', timeout=timeout))
        ids = {m['id']: m.get('loaded_context_length') for m in d.get('data', []) if m.get('state') == 'loaded' and m.get('id')}
    except (OSError, ValueError, KeyError, TypeError): ids = None
    _api.update(at=time.time(), ids=ids)
    return ids


def parallel_of(model):
    """How many requests LM Studio answers at once for this model (1 when it is not listed: the safe side)."""
    m = next((r for r in loaded() if r['id'] == model), None)
    return max(1, m['parallel'] or 1) if m else 1


# ---------------------------------------------------------------- copies: one model loaded more than once
MODELS_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models.json')   # the repo's defaults (MODELS: the live settings on this Mac)
KEEP_GOOD = False                                        # the gateway sets it: its process alone writes MODELS_GOOD
_conf = {'path': None, 'mtime': None, 'data': {}, 'error': None}


def settings_path():
    return MODELS if os.path.exists(MODELS) else MODELS_DEFAULT


def _number(v, positive=False, whole=False):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and (not whole or float(v).is_integer()) and (not positive or v > 0)


def validate(d):
    """The settings' shape, checked before a file is taken: ValueError naming the key that is wrong."""
    if not isinstance(d, dict): raise ValueError('not a JSON object')
    fams = d.get('families', {})
    if not isinstance(fams, dict): raise ValueError('"families" must be an object of families')
    for name, f in fams.items():
        if not isinstance(f, dict): raise ValueError(f'family {name!r} must be an object')
        for k in ('copies', 'context', 'parallel'):
            if k in f and not _number(f[k], positive=True, whole=True): raise ValueError(f'{name}: "{k}" must be a whole number above 0, not {f[k]!r}')
        for k in ('recycle', 'admit', 'watchdog'):
            v = f.get(k)
            if k in f and not (isinstance(v, bool) or isinstance(v, dict) and all(_number(x) for x in v.values())):
                raise ValueError(f'{name}: "{k}" must be true, false or an object of numbers, not {v!r}')
        if 'key' in f and not isinstance(f['key'], str): raise ValueError(f'{name}: "key" must be a model key')
        if 'kind' in f and f['kind'] not in KINDS_OF_MODEL: raise ValueError(f'{name}: "kind" must be one of {", ".join(KINDS_OF_MODEL)}, not {f["kind"]!r}')
    for k, positive in (('headroom_gb', False), ('batch_at_once', True)):
        if k in d and not _number(d[k], positive=positive): raise ValueError(f'"{k}" must be a number, not {d[k]!r}')
    for k in ('peers', 'serve'):
        if k in d and not isinstance(d[k], dict): raise ValueError(f'"{k}" must be an object')


def settings():
    """The hub's settings: {"families": {id: {key, copies, context, parallel, recycle}}, "batch_at_once", "headroom_gb"}.
    A file that cannot be read (bad JSON, half written) or has the wrong shape (validate) leaves the last good settings
    standing: in a process that has read none yet (a gateway just restarted), the last good copy the gateway kept;
    settings_error() says which."""
    p = settings_path()
    try: m = os.path.getmtime(p)
    except OSError: return _conf['data']
    if (p, m) != (_conf['path'], _conf['mtime']):
        first = _conf['path'] is None
        try:
            d = json.load(open(p))
            validate(d)
            _conf.update(path=p, mtime=m, data=d, error=None)
            if KEEP_GOOD and p == MODELS:
                try:
                    tmp = f'{MODELS_GOOD}.{os.getpid()}.tmp'
                    with open(tmp, 'w') as f: json.dump(d, f, indent=2)
                    os.replace(tmp, MODELS_GOOD)
                except OSError: pass
        except (OSError, ValueError) as e:
            why, data = f'{p}: {type(e).__name__}: {e}', _conf['data']
            if first:                                        # nothing read in this process yet: the last good copy, if any
                try:
                    data = json.load(open(MODELS_GOOD)); validate(data)
                    why += f"; the last good copy (saved {time.strftime('%d %b %H:%M', time.localtime(os.path.getmtime(MODELS_GOOD)))}) is in effect"
                except (OSError, ValueError): data, why = {}, why + '; no valid settings: running with none'
            else: why += '; the last valid settings remain in effect'
            _conf.update(path=p, mtime=m, data=data, error=why)
    return _conf['data']


def settings_error():
    """Why the settings file could not be taken, and which settings are in effect meanwhile, or None."""
    return _conf['error']


def family(model):
    """The id a model's copies share: qwen/qwen3.6-35b-a3b:2 -> qwen/qwen3.6-35b-a3b (a model loaded once is its own), and a
    copy on a peer Mac, 'qwen/qwen3.6-35b-a3b on macbook' (peers.py), is a copy of the same family."""
    s = str(model or '')
    on = ' on ' in s
    if on: s = s.split(' on ', 1)[0]
    m = re.match(r'^(.+):(\d+)$', s)
    return m.group(1) if m else (s if on else model)


KINDS_OF_MODEL = ('llm', 'embedding')                   # a family's "kind": a chat model (the default) or an embedding model


def embedding(model, fams=None):
    """Whether this model's family is an embedding family ("kind": "embedding" in models.json): kept loaded beside the
    chat families (models.keep), never unloaded by a swap to another model, and out of the slot counts, the admission
    budgets, the recycles and the watchdog. fams: the settings' families (read now by default)."""
    f = ((settings().get('families') or {}) if fams is None else fams).get(family(model))
    return isinstance(f, dict) and f.get('kind') == 'embedding'


def chat_rows(rows, fams=None):
    """The listing less its embedding models (a row LM Studio types "embedding", or of an embedding family): the
    instances that share the Mac's memory for context, and whose slots a batch counts."""
    fams = (settings().get('families') or {}) if fams is None else fams
    return [r for r in rows if r.get('type') != 'embedding' and not embedding(r.get('id'), fams)]


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
    """How many answers a model's loaded copies give at once in all (two copies of 4: 8), with the slots its peers have
    free for this Mac. model None: the loaded model that gives the most. Batches through the hub send this many. 0 while
    the gateway refuses every request (maintenance that is on, or the local models switched off); switched off to peers
    only, the peers' slots alone (0 when none is up)."""
    try:
        from . import maint
        if maint.refusing(): return 0
        here_too = not maint.peers_only()
    except Exception as e: here_too = True; _error('at_once: maintenance', e)
    rows = loaded() if here_too else []
    if model is None:                                    # chat families only: an embedding model's slots are not a batch's
        fams = {family(r['id']) for r in chat_rows(rows)}
        try:
            from . import peers                              # the families this Mac's peers serve for it count too
            fams |= {f for p in peers.view() for f in (p.get('families') or {}) if not embedding(f)}
        except Exception as e: _error('at_once: peer families', e)
        return max([at_once(f) for f in fams] or [1 if here_too else 0])
    ids = {r['id'] for r in rows}
    here = sum(parallel_of(c) for c in copies(model) if c in ids)
    try:
        from . import peers
        there = peers.capacity(model)
    except Exception as e: there = 0; _error('at_once: peer capacity', e)
    return (here + there) or (parallel_of(model) if here_too else 0)


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


@contextlib.contextmanager
def family_access(models, exclusive=False, wait=False):
    """A swap reserves the affected families through verification. Admission takes a shared lock while publishing
    a lease or taking a slot; once published, either is visible to the swap's final blocker check."""
    directory = os.path.join(RUN, 'families')
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with contextlib.ExitStack() as stack:
        for name in sorted({_safe(family(m)) for m in models}):
            handle = stack.enter_context(open(os.path.join(directory, name + '.lock'), 'a+'))
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try: fcntl.flock(handle, mode if wait else mode | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True


@contextlib.contextmanager
def swap_guard(models, loading=()):
    """The blocker check and the mutation it permits are one operation with respect to admission."""
    with family_access(set(models) | set(loading), exclusive=True) as reserved:
        why = swap_blockers(models)
        if not reserved and not why: why = ['a request or model change is acquiring this family']
        yield why


@contextlib.contextmanager
def flocked(path):
    """An exclusive lock on path for the length of the block: every process that changes the same shared file takes it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a+') as h:
        fcntl.flock(h, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------- gateways on this Mac: the registry, roles, nudges
def _flock_test(path):
    """Whether some process holds an exclusive lock on path: True, False when it is free, None when there is no such file.
    (A shared lock, tried and dropped at once, so two testers never disturb each other.)"""
    try: fd = os.open(path, os.O_RDONLY)
    except OSError: return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError: return True                              # held (an error that cannot tell counts as held: the safe side)
    finally: os.close(fd)


def _lock_own(path, wait=2.0):
    """An exclusive lock on path, kept until its descriptor is closed: retried for `wait` seconds while a tester holds it
    for an instant, and checked to still be the file at that path (a cleaner may have removed it in between). -> the fd."""
    t0 = time.time()
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            if time.time() - t0 > wait: raise
            time.sleep(0.05); continue
        try: same = os.stat(path).st_ino == os.fstat(fd).st_ino
        except OSError: same = False
        if same: return fd
        os.close(fd)


class Registration:
    """This gateway in the registry: run/gateways/<pid>.lock, held for the process's whole life (the OS frees it when the
    process dies: that is what live means, never a bare pid that may have been reused), and <pid>.json, its row:
    {pid, gen, run, release, code, version, state_protocol, channel, role, accepting, open, queued, answering, oldest,
    roles, errors, started, at}. No request content."""
    def __init__(self, fd, row):
        self.fd, self.row, self.lock = fd, row, threading.Lock()

    @property
    def path(self): return os.path.join(GATEWAYS, f"{self.row['pid']}.json")

    def update(self, **fields):
        """Change fields and rewrite the row at once."""
        with self.lock:
            if self.fd is None: return
            self.row.update(fields); self.row['at'] = round(time.time(), 1)
            try: _write(self.path, self.row)
            except OSError as e: _error('registry: write the row', e)

    def close(self):
        """Leave the registry (a clean exit): the row and the lock file go while the lock is held, then the lock."""
        with self.lock:
            if self.fd is None: return
            for p in (self.path, os.path.join(GATEWAYS, f"{self.row['pid']}.lock")):
                try: os.remove(p)
                except OSError: pass
            os.close(self.fd); self.fd = None
        _live.pop(self.row['pid'], None); _gw['at'] = 0.0


def register(row=None, every=5.0, fill=None):
    """Put this process in the gateway registry, before it accepts anything (a mark older than its registration is from
    an earlier process with the same pid). -> its Registration. The row is rewritten every `every` seconds too, with the
    fields fill() gives (the counts that change all the time)."""
    os.makedirs(GATEWAYS, mode=0o700, exist_ok=True)
    try: os.chmod(RUN, 0o700)
    except OSError as e: _error('registry: run/ mode', e)
    pid = os.getpid()
    fd = _lock_own(os.path.join(GATEWAYS, f'{pid}.lock'))
    reg = Registration(fd, dict(row or {}, pid=pid, started=round(time.time(), 2), state_protocol=STATE_PROTOCOL))
    reg.update()
    _live.pop(pid, None); _gw['at'] = 0.0

    def beat():
        while True:
            time.sleep(every)
            if reg.fd is None: return
            try: reg.update(**(fill() if fill else {}))
            except Exception as e: _error('registry: fill the row', e)
    threading.Thread(target=beat, daemon=True, name='registry').start()
    return reg


def _reap(pid):
    """A dead gateway's registry files go, removed under its own lock (a new process with the same pid is never touched)."""
    lock = os.path.join(GATEWAYS, f'{pid}.lock')
    try: fd = os.open(lock, os.O_RDWR)
    except OSError: return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.stat(lock).st_ino != os.fstat(fd).st_ino: return
        for p in (os.path.join(GATEWAYS, f'{pid}.json'), lock):
            try: os.remove(p)
            except OSError: pass
    except OSError: pass                                     # held again (a new process with this pid), or already removed
    finally: os.close(fd)


def gateway_live(pid):
    """Whether a gateway process is live: True while it holds its registry lock, False when its lock file is there and
    free (it died; its files are removed), None when it never registered (a gateway older than the registry, or not a
    gateway). Cached 1 s."""
    pid = _int(pid)
    if pid <= 0: return None
    now = time.time()
    c = _live.get(pid)
    if c and now - c[0] < 1: return c[1]
    v = _flock_test(os.path.join(GATEWAYS, f'{pid}.lock'))
    if v is False: _reap(pid)
    _live[pid] = (now, v)
    return v


def live_gateways():
    """The registry rows of every live gateway on this Mac, oldest first (cached 1 s)."""
    now = time.time()
    if now - _gw['at'] < 1: return _gw['rows']
    rows = []
    for f in glob.glob(os.path.join(GATEWAYS, '*.lock')):
        pid = _int(os.path.basename(f)[:-len('.lock')])
        if gateway_live(pid) is not True: continue
        try: d = json.load(open(os.path.join(GATEWAYS, f'{pid}.json')))
        except (OSError, ValueError): d = {}
        rows.append(dict(d if isinstance(d, dict) else {}, pid=pid))
    rows.sort(key=lambda r: (float(r.get('started') or 0), r['pid']))
    _gw.update(at=now, rows=rows)
    return rows


class Role:
    """A loop only one gateway on this Mac runs at a time (the instance manager, the peer poller, the memory sampler, the
    link listener, and batch recovery): its holder is whoever holds run/roles/<name>.lock, freed by the OS when the holder dies. The lock's
    descriptor is never inherited, so lms and caffeinate children never keep a role. <name>.json says {pid, gen, since}."""
    def __init__(self, name, gen=None):
        self.name, self.gen, self.fd, self.since = name, gen, None, None

    @property
    def path(self): return os.path.join(ROLES, f'{self.name}.lock')

    @property
    def held(self): return self.fd is not None

    def try_take(self):
        """Take the role when nobody holds it. -> whether this process holds it now."""
        if self.fd is not None: return True
        os.makedirs(ROLES, mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: os.close(fd); return False
        self.fd, self.since = fd, time.time()
        try: _write(os.path.join(ROLES, f'{self.name}.json'), dict(pid=os.getpid(), gen=self.gen, since=round(self.since, 1)))
        except OSError as e: _error(f'role {self.name}: write the holder', e)
        return True

    def holder(self):
        """The pid holding the role; None when nobody does; 0 when it is held and the holder has not said who it is yet."""
        if self.fd is not None: return os.getpid()
        if not _flock_test(self.path): return None
        try: return _int(json.load(open(os.path.join(ROLES, f'{self.name}.json'))).get('pid'))
        except (OSError, ValueError, AttributeError): return 0

    def release(self):
        """Give the role up (between two passes of its loop, never inside one)."""
        fd, self.fd, self.since = self.fd, None, None
        if fd is not None: os.close(fd)


def supervisor():
    """The gateway supervisor on this Mac, while it holds run/supervisor.lock: its run/supervisor.json ({pid, run, digest,
    started, ...}), or None."""
    if not _flock_test(os.path.join(RUN, 'supervisor.lock')): return None
    try: d = json.load(open(os.path.join(RUN, 'supervisor.json')))
    except (OSError, ValueError): return None
    return d if isinstance(d, dict) else None


def roles():
    """{name: the pid holding it} for every role some process holds now."""
    out = {}
    for f in glob.glob(os.path.join(ROLES, '*.lock')):
        pid = Role(os.path.basename(f)[:-len('.lock')]).holder()
        if pid is not None: out[os.path.basename(f)[:-len('.lock')]] = pid
    return out


def nudge(name):
    """Wake a role's loop in whichever process holds it: run/nudge-<name> is touched, and the loop watches its mtime."""
    p = os.path.join(RUN, f'nudge-{name}')
    try:
        os.makedirs(RUN, mode=0o700, exist_ok=True)
        with open(p, 'a'): pass
        os.utime(p, None)
    except OSError as e: _error(f'nudge {name}', e)


def nudged(name):
    """When run/nudge-<name> was last touched (0.0: never)."""
    try: return os.path.getmtime(os.path.join(RUN, f'nudge-{name}'))
    except OSError: return 0.0


class Counted:
    """At most `cap` holders at once across every process on this Mac: slot-N.lock files in one folder under peer-slots/
    (N < 64), counted and taken under take.lock; the OS frees a holder's slot when it dies. One per peer and family, so
    all of this Mac's gateways together stay within the slots a peer offers it."""
    MAX = 64

    def __init__(self, *names): self.names = names

    @property
    def folder(self): return os.path.join(PEER_SLOTS, *(_safe(n) for n in self.names))

    def held(self):
        """How many slots some process holds now (this one included)."""
        n = 0
        for i in range(self.MAX):
            v = _flock_test(os.path.join(self.folder, f'slot-{i}.lock'))
            if v is None: break                              # made in order and never removed: none past the first missing
            n += bool(v)
        return n

    def take(self, cap):
        """A slot, when fewer than cap are held now. -> its descriptor (give it back with give()), or None."""
        cap = min(_int(cap), self.MAX)
        if cap <= 0: return None
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, 'take.lock'), 'a+') as m:
            fcntl.flock(m, fcntl.LOCK_EX)
            if self.held() >= cap: return None
            for i in range(self.MAX):
                fd = os.open(os.path.join(self.folder, f'slot-{i}.lock'), os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError: os.close(fd)
        return None

    @staticmethod
    def give(fd):
        try: os.close(fd)
        except OSError as e: _error('peer slot: give back', e)


# ---------------------------------------------------------------- LM Studio's server: started only when it is down
def server_up(timeout=3, url=None):
    """Whether LM Studio's server (url: LMSTUDIO) is up: only a refused connection means down. A slow answer, a reset or a
    cut answer means it is up and busy, and starting it then would restart it under every request in flight."""
    try: urlopen((url or LMSTUDIO) + '/v1/models', timeout=timeout); return True
    except urllib.error.URLError as e: return not isinstance(getattr(e, 'reason', None), ConnectionRefusedError)
    except ConnectionRefusedError: return False
    except OSError: return True


def start_server(why='a request could not connect', debounce=60, url=None):
    """LM Studio's server, started only when it is down: one start across every process on this Mac (a blocking lock on
    run/server-start.lock, the check repeated inside it), and at most one per `debounce` seconds (run/server-start.json:
    60 for the request path, 0 for the instance manager). -> whether it answers now."""
    up = lambda t: server_up(t, url)
    if up(2): return True
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    info = os.path.join(RUN, 'server-start.json')
    with open(os.path.join(RUN, 'server-start.lock'), 'a+') as h:
        fcntl.flock(h, fcntl.LOCK_EX)                       # a second process waits here for the first one's start
        if up(2): return True
        try: last = float(json.load(open(info)).get('at') or 0)
        except (OSError, ValueError, AttributeError, TypeError): last = 0.0
        if debounce and time.time() - last < debounce: return False
        try: _write(info, dict(at=round(time.time(), 1), pid=os.getpid(), why=clean(why, 120)))
        except OSError as e: _error('server start: write the record', e)
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} LM Studio's server is down ({why}): lms server start", flush=True)
        try: subprocess.run([LMS, 'server', 'start'], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e: _error('server start: lms server start', e); return False
        for _ in range(20):
            if up(2): return True
            time.sleep(0.5)
        return False


# ---------------------------------------------------------------- rule 1: a slot per request
REMOTE = None              # the door's link to its peers (peers.LINK): it counts their slots, which only the door sends to


class _Remote:
    """A slot held on a peer Mac's copy (peers.Link.take: a counted lock shared by this Mac's gateways), given back like a lock."""
    def __init__(self, c, h=None): self.c, self.h = c, h

    def release(self):
        if REMOTE is not None: REMOTE.give(self.c, self.h)
        elif self.h is not None: Counted.give(self.h)


def _release(h):
    if isinstance(h, _Remote): h.release(); return
    try:
        try: h.seek(0); h.truncate()                     # its context no longer counts (slot_load), before the lock goes
        except OSError: pass
        fcntl.flock(h, fcntl.LOCK_UN)
    finally: h.close()


def _slot_dir(m):
    d = os.path.join(SLOTS, _safe(m)); os.makedirs(d, exist_ok=True)
    return d


def slot_load(model):
    """The context in flight on one of this Mac's copies, in tokens, held by every process: the sum of what each held slot's
    lock file says (a gateway writes its request's context there when it takes the slot and clears it before it lets go).
    A file left written by a process that died is not counted: its lock is free."""
    d, total = _slot_dir(model), 0
    for i in range(parallel_of(model)):
        p = os.path.join(d, f'slot-{i}.lock')
        try:
            with open(p) as f: n = int(f.read().strip() or 0)
        except (OSError, ValueError): continue
        if n <= 0: continue
        h = open(p, 'a+')
        try:
            fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free: what it says is left from a holder that died
            try: h.seek(0); h.truncate()
            except OSError: pass
            fcntl.flock(h, fcntl.LOCK_UN)
        except OSError: total += n                         # held: it counts
        finally: h.close()
    return total


@contextlib.contextmanager
def _take_lock(m):
    """The per-copy mutex of a take by context size: the load is read and a slot taken under it, by any gateway."""
    h = open(os.path.join(_slot_dir(m), 'take.lock'), 'a+')
    try:
        fcntl.flock(h, fcntl.LOCK_EX); yield
    finally:
        try: fcntl.flock(h, fcntl.LOCK_UN)
        finally: h.close()


def _take(models):
    """Try each model's slots once (a copy on a peer, '<family> on <peer>', by the door's link); -> (the lock handle,
    the model) or (None, None)."""
    for m in models:
        if ' on ' in m:
            h = REMOTE.take(m) if REMOTE is not None else None
            if h is not None: return _Remote(m, h), m
            continue
        h = _take_slot(m)
        if h is not None: return h, m
    return None, None


def _take_slot(m, need=0):
    """One free slot of a local copy, or None; need: the context written into it (slot_load)."""
    d = _slot_dir(m)
    with family_access([m]) as available:
        if not available: return None
        for i in range(parallel_of(m)):
            h = open(os.path.join(d, f'slot-{i}.lock'), 'a+')
            try: fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError: h.close(); continue
            if need:
                try: h.seek(0); h.truncate(); h.write(str(int(need))); h.flush()
                except OSError as e: _error('slot: write its context', e)
            return h
    return None


_pl = {'at': 0.0, 'v': {}}                                 # peer_load(), cached 0.25 s


def peer_load(copy):
    """The context this Mac's gateways have in flight on a peer's copy, in tokens: every live gateway's answering marks
    there, and this process's own requests (a request counts in _load from its take, before its mark says answering)."""
    now = time.time()
    if now - _pl['at'] >= 0.25:
        v = {}
        for d in inflight():
            c = d.get('copy')
            if c and ' on ' in c and d.get('state') == 'answering' and d.get('ctx') and d.get('hub_pid') != os.getpid():
                v[c] = v.get(c, 0) + int(d['ctx'])
        _pl.update(at=now, v=v)
    with _load_lock: mine = _load.get(copy, 0)
    return _pl['v'].get(copy, 0) + mine


def _take_fitting(mine, need, fits):
    """The first of these copies that has a free slot and room for `need` (fits), with every gateway's context counted: a
    local copy under its take mutex (slot_load), a peer's by what this Mac holds there. -> (handle, copy) or (None, None)."""
    for m in mine:
        if ' on ' in m:
            if fits(m, peer_load(m), need):
                h, got = _take([m])
                if h is not None: return h, got
            continue
        with _take_lock(m):
            if not fits(m, slot_load(m), need): continue
            h = _take_slot(m, need)
            if h is not None: return h, m
    return None, None


def waiting_here():
    """How many requests in this process are waiting for a slot right now, by kind: {'people': n, 'others': n}."""
    with _q_lock: return dict(people=sum(1 for t in _q if t[0] == 0), others=sum(1 for t in _q if t[0] != 0))


RESERVE = 20               # seconds a request waits for room (admission.fits) before it keeps its place on every copy it wants
_load, _load_lock = {}, threading.Lock()   # this process's context in flight per copy, in tokens (admission.py)


def load_here(copies=None):
    """The context in flight per copy, in tokens: {copy: n}. A local copy's counts every gateway's requests (slot_load);
    a peer's, what this process sent there. copies: those to read (default: the copies this process holds context on)."""
    with _load_lock: mine = {m: n for m, n in _load.items() if n > 0}
    out = {}
    for m in (mine if copies is None else copies):
        n = peer_load(m) if ' on ' in m else slot_load(m)
        if n > 0: out[m] = n
    return out


@contextlib.contextmanager
def slot_any(models, wait=3600, kind='other', since=None, publish=None, need=0, fits=None, waiting=None, cancelled=None):
    """Hold one prediction slot on the first of these models that has one free: a model's copies, in order, so the first
    copy's slots fill before the second's (two on each measured slower than four on one). models: a list, or a function
    that gives the list each time it is tried, so a copy that comes back or is being drained counts at once. Waiters in
    this process take turns: a chat or ask request (a person is waiting) before anything else, then first come first
    served, per copy: a waiter leaves a copy's slot to an earlier waiter that may take it. Slots held by other processes
    (llmshare.slot in a project's own code) are simply busy. Yields (the model, seconds waited); TimeoutError after `wait` s.
    Across gateways (a handoff runs two): the turn is (people first, since, pid) in wall-clock time, so since (when the
    request first queued) keeps a requeued request's place; publish(cands, until) is called whenever the copies it holds
    its place on change, for the gateway to write into the request's mark; and while two gateways or more are live, a
    waiter also leaves a copy to the earlier waiters the others published. need, fits: the request's context in tokens and
    whether a copy may take it with what it holds (fits(model, load, need); the gateway's is admission.fits), applied under
    one lock with the slot and the copy's load, so two waiters never both take the last room. A waiter that fits no copy
    yet leaves those copies to later waiters that fit (backfilling) until it has waited RESERVE seconds; then it keeps its
    place on every copy it wants, so a long request is never starved by short ones. waiting(copies) hears, whenever it
    changes, which copies it is waiting on for room rather than for a slot."""
    pick = models if callable(models) else (lambda: list(models))
    t0 = time.time()
    ticket = (0 if kind in PEOPLE else 1, float(since) if since else t0, os.getpid(), next(_seq))
    until, held, got, shown, said = t0 + wait, None, None, None, None
    try:
        while held is None:
            if cancelled and cancelled(): raise ConnectionAbortedError('client closed the connection while queued')
            cands = list(pick() or [])
            if fits is None: room = cands
            else:
                loads = load_here(cands)
                room = [m for m in cands if fits(m, loads.get(m, 0), need)]    # a first look; the take checks again under the copy's mutex
            reserve = fits is not None and time.time() - t0 >= RESERVE
            want = cands if reserve or fits is None else room                    # the copies it holds its place on
            if publish is not None and want != shown:
                shown = want
                try: publish(want, until)
                except Exception as e: _error('slot_any: publish the queued mark', e)
            with _q_lock:
                _q[ticket] = want
                ahead = {m for t, ms in _q.items() if t < ticket for m in ms}
            if publish is not None: ahead |= _ahead_elsewhere(ticket)
            mine = [m for m in room if m not in ahead]
            if mine:                                         # the check again with the take: no other waiter, in any gateway, takes the room meanwhile
                held, got = _take(mine) if fits is None else _take_fitting(mine, need, fits)
                if held is not None and need:
                    with _load_lock: _load[got] = _load.get(got, 0) + need
            if held is None:
                unfit = tuple(m for m in cands if m not in room)
                if waiting and unfit != said:
                    said = unfit
                    try: waiting(list(unfit))
                    except Exception as e: _error('slot_any: waiting', e)
                if time.time() - t0 > wait:
                    _note('slot timeout', family(cands[0]) if cands else '?', time.time() - t0)
                    raise TimeoutError(f"no free LM Studio slot for {', '.join(cands) or 'the model'} after {wait:.0f} s")
                time.sleep(0.2 if time.time() - t0 < 10 else 0.5)
    finally:
        with _q_lock: _q.pop(ticket, None)
    waited = time.time() - t0
    if waited > 2: _note('slot wait', family(got), waited)
    try: yield got, waited
    finally:
        _release(held)
        if need:
            with _load_lock: _load[got] = max(0, _load.get(got, 0) - need)


def _mark_key(d, path=''):
    """A queued mark's place in the admission order: (people first, since, its gateway's pid, its number)."""
    prio, n = d.get('prio'), d.get('n')
    if prio is None: prio = 0 if d.get('kind') in PEOPLE else 1
    if n is None:
        m = re.search(r'-(\d+)\.json$', path); n = m.group(1) if m else 0
    try: t = float(d.get('t0') or d.get('since') or 0)
    except (TypeError, ValueError): t = 0.0
    return (_int(prio), t, _int(d.get('hub_pid')), _int(n))


def _ahead_elsewhere(ticket):
    """The copies earlier waiters in other live gateways may take (their queued marks; one whose until has passed by
    5 s is ignored), read at most every 0.25 s, and only while two gateways or more are live."""
    if len(live_gateways()) < 2: return set()
    now = time.time()
    if now - _fq['at'] >= 0.25:
        rows, me = [], os.getpid()
        for f in glob.glob(os.path.join(INFLIGHT, '*.json')):
            try: d = json.load(open(f))
            except (OSError, ValueError): continue
            if not isinstance(d, dict) or d.get('state') != 'queued': continue
            hp = _int(d.get('hub_pid'))
            if hp == me or gateway_live(hp) is not True: continue
            try: until = float(d.get('until') or 0)
            except (TypeError, ValueError): until = 0.0
            if now > until + 5: continue
            rows.append((_mark_key(d, f), list(d.get('cands') or [])))
        _fq.update(at=now, rows=rows)
    return {m for k, ms in _fq['rows'] if k < ticket for m in ms}


def waiting():
    """How many requests are queued for a slot in every live gateway on this Mac (their marks), by kind:
    {'people': n, 'others': n}. waiting_here() counts this process alone."""
    out = dict(people=0, others=0)
    for d in inflight():
        if d.get('state') == 'queued': out['people' if _mark_key(d)[0] == 0 else 'others'] += 1
    return out


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


def me(env=None):
    """Session IDs inherited by a tool or script started by Claude Code or OpenCode."""
    env = os.environ if env is None else env
    sid, host = env.get('CLAUDE_CODE_SESSION_ID'), env.get('CLAUDE_CODE_HOST_SESSION_ID')
    if not (sid or host): sid = env.get('OPENCODE_SESSION_ID')
    return {k: v for k, v in (('session', sid), ('host_session', host)) if v}


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
    with family_access(rec['models'], wait=True): held.save()
    th = threading.Thread(target=beat, daemon=True); th.start()
    try: yield held
    finally:
        stop.set(); th.join(2)
        try: os.remove(path)
        except OSError: pass


def _alive(pid):
    """Whether a process with this pid exists (never pid 0 or less: os.kill would signal a whole process group)."""
    pid = _int(pid)
    if pid <= 0: return False
    try: os.kill(pid, 0); return True
    except PermissionError: return True
    except OSError: return False


HUB_LEASE = re.compile(r'-hub(-\d+)?\.json$')       # the hub's own leases: <project>-<holder>[-<front>]-hub-<gateway pid>.json (1.4.0: -hub.json)


def hub_lease_path(project, holder, front=None):
    """The lease file this gateway keeps for one client (holder: its pid, or the supervisor's for a peer or a client whois
    cannot name), project and front (the peer Mac that sent it): its own file, so two gateways never share one. None for
    a holder that is no pid (0 is never written)."""
    holder = _int(holder)
    if holder <= 0: return None
    return os.path.join(LEASES, f'{_safe(project)}-{holder}' + (f'-{_safe(front)}' if front else '') + f'-hub-{os.getpid()}.json')


def run_leases(pid):
    """The lease files a client process holds itself (llmshare.lease), never the hub's leases for it."""
    return [f for f in glob.glob(os.path.join(LEASES, f'*-{_int(pid)}-*.json')) if not HUB_LEASE.search(f)]


def leases():
    """Every live lease, all projects. A lease lives while its process does, or, for one a gateway wrote (via 'hub'),
    while that gateway is live (a client may come and go between requests, and a supervisor restart changes the holder);
    one with no heartbeat for STALE seconds is dead either way. Dead leases are removed."""
    out, now = [], time.time()
    for f in glob.glob(os.path.join(LEASES, '*.json')):
        try: d = json.load(open(f))
        except (OSError, ValueError): continue
        if not isinstance(d, dict): continue
        try: beat = float(d.get('beat') or 0)
        except (TypeError, ValueError): beat = 0.0
        live = _alive(d.get('pid')) or (_int(d.get('pid')) > 0 and d.get('via') == 'hub' and gateway_live(d.get('hub_pid')) is True)
        if not live or now - beat > STALE:
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
    rows = loaded(fresh=True, force=True)
    if not read_ok():
        return ["lms ps failed, so the loaded models are unknown; retry shortly"]
    for d in sorted(leases(), key=lambda d: d.get('since', 0)):
        hit = sorted({m for m in d.get('models') or [] if family(m) in fams})
        if (d.get('pid') == os.getpid() and d.get('via') != 'hub') or not hit: continue
        key = (d.get('project'), d.get('pid'), d.get('from'))
        if key in seen: continue
        seen.add(key)
        since = time.strftime('%H:%M', time.localtime(float(d.get('since') or 0)))
        what = 'requests via the gateway' if d.get('via') == 'hub' and d.get('kind') == 'other' else f"a {d.get('kind')}"
        why.append(f"{what} in the {d.get('project')} project{' on ' + str(d['from']) if d.get('from') else ''} (since {since}) is using {', '.join(hit)}")
    if not why:
        busy = busy_slots()
        for r in rows:
            if family(r['id']) not in fams: continue
            if busy.get(_safe(r['id'])): why.append(f"a prediction slot on {r['id']} is in use")
            elif r['state'] in ('GENERATING', 'PROCESSING'):
                why.append(f"{r['id']} is generating now (LM Studio's chat or another client, outside the gateway)")
    if why: _note('swap refused', ','.join(sorted(ms)))
    return why


# ---------------------------------------------------------------- talking to LM Studio: through the hub
def hub_up(fresh=False):
    """Whether the hub's door is answering (cached 5 s). A door that is slow to answer is up (something is listening and
    busy); only a refused connection means it is down."""
    if fresh or time.time() - _hub['at'] > 5:
        try:
            health = json.load(urlopen(HUB + '/hub/health', timeout=3))
            up = isinstance(health, dict) and health.get('ok') is True
            _hub['health'] = health if up else {}
        except urllib.error.HTTPError: up = False                # something answered, and it is not the gateway's health
        except urllib.error.URLError as e: up = not isinstance(getattr(e, 'reason', None), ConnectionRefusedError)
        except ConnectionRefusedError: up = False
        except (OSError, ValueError): up = True
        _hub.update(at=time.time(), up=up)
    return _hub['up']


def base_url():
    """Where to send requests: the hub when it is up, LM Studio itself when it is not."""
    return HUB if hub_up() else LMSTUDIO


def get(path, timeout=10):
    return json.load(urlopen(base_url() + path, timeout=timeout))


def post(path, body, project, kind='other', timeout=3600, headers=None):
    """One request to the local model, by the rules: through the hub, which names the project, takes a slot and logs
    it; or, when the hub is down, straight to LM Studio holding a slot here. headers: more for the hub (X-Step, X-Session)."""
    data, base = json.dumps(body).encode(), {'Content-Type': 'application/json'}
    if hub_up():
        hub_headers = dict(base, **{'X-Project': str(project), 'X-Timeout': str(int(timeout))}, **(headers or {}))   # the door bounds its wait by it
        if kind in KINDS and kind != 'other': hub_headers['X-Kind'] = kind   # else the hub reads the kind from the run's lease
        req = urllib.request.Request(HUB + path, data=data, headers=hub_headers)
        try: return json.load(urlopen(req, timeout=timeout))
        except urllib.error.HTTPError: raise
        except urllib.error.URLError as e:                 # the door is restarting (refused: the request never reached the
            if not isinstance(e.reason, ConnectionRefusedError): raise    # model), so this once go straight to LM Studio
            _hub.update(at=time.time(), up=False)
    req = urllib.request.Request(LMSTUDIO + path, data=data, headers=base)
    with (slot(body['model']) if body.get('model') else contextlib.nullcontext()):
        return json.load(urlopen(req, timeout=timeout))


def hub_post(path, body=None, timeout=5):
    """A small request to the hub itself (/hub/...), or None when the door does not answer."""
    try:
        req = urllib.request.Request(HUB + path, data=json.dumps(body or {}).encode(), headers={'Content-Type': 'application/json'})
        return json.load(urlopen(req, timeout=timeout))
    except (OSError, ValueError): return None


# ---------------------------------------------------------------- the hub's records, read
def inflight():
    """Each request the hub's gateways are passing on right now: inflight/<gateway pid>-<n>.json. A gateway that
    registered touches its marks every BEAT, so its mark lives while the gateway does, if touched within MARK_STALE
    seconds and no older than the gateway's own start (else the pid was reused); a gateway older than the registry keeps
    today's rule: its pid alive, two hours at most. Dead marks are removed."""
    out, now, started = [], time.time(), {}
    for f in glob.glob(os.path.join(INFLIGHT, '*.json')):
        try: d = json.load(open(f))
        except (OSError, ValueError): continue
        if isinstance(d, dict) and _mark_live(d, f, now, started): out.append(d); continue
        try: os.remove(f)
        except OSError: pass
    return out


def _mark_live(d, path, now, started):
    hp = _int(d.get('hub_pid'))
    try: since = float(d.get('t0') or d.get('since') or 0)
    except (TypeError, ValueError): since = 0.0
    reg = gateway_live(hp)
    if reg is None: return _alive(hp) and now - since <= 7200
    if not reg: return False
    try:
        if now - os.path.getmtime(path) > MARK_STALE: return False
    except OSError: return False
    if hp not in started:
        try: started[hp] = float(json.load(open(os.path.join(GATEWAYS, f'{hp}.json'))).get('started') or 0)
        except (OSError, ValueError, AttributeError, TypeError): started[hp] = 0.0
    return since >= started[hp] - 2


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


def recent_requests(n, hours, qualifies=None):
    """Up to n matching records, newest first. Read only the blocks needed, including the previous file on rollover."""
    if n <= 0: return []
    since, out, seen = time.time() - hours * 3600, [], set()
    for path in (REQUESTS, REQUESTS + '.1'):
        try: f = open(path, 'rb')
        except OSError: continue
        with f:
            identity = os.fstat(f.fileno())
            key = identity.st_dev, identity.st_ino
            if key in seen: continue                  # the current file rotated while being read
            seen.add(key)
            pos, rest = f.seek(0, os.SEEK_END), b''
            while pos:
                size = min(pos, 16384)
                pos -= size
                f.seek(pos)
                lines = (f.read(size) + rest).split(b'\n')
                rest = lines.pop(0) if pos else b''
                for line in reversed(lines):
                    if not line: continue
                    try: r = json.loads(line)
                    except ValueError: continue
                    if not isinstance(r, dict) or not isinstance(r.get('at'), (int, float)): continue
                    if r['at'] < since: return out
                    if qualifies and not qualifies(r): continue
                    out.append(r)
                    if len(out) == n: return out
    return out


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
    print('LM Studio loaded: ' + (', '.join(f"{r['id']} ({r['state'].lower()}, {r['parallel']} slots, context {r['context']:,})" for r in ms)
                                 or 'none') + ('' if read_ok() else ' (last listing failed; showing the previous one)'))
    print(f"gateway ({HUB}): " + ('listening' if hub_up(fresh=True) else 'down: clients connect directly to LM Studio'))
    ls = leases()
    for d in sorted(ls, key=lambda d: d.get('since', 0)):
        print(f"  {d['project']}: {'requests via the gateway' if d.get('via') == 'hub' else d['kind']} on {', '.join(d['models'])} "
              f"since {time.strftime('%H:%M:%S', time.localtime(d['since']))} (pid {d['pid']})")
    if not ls: print('  no active leases')
    b = busy_slots()
    print('  active requests: ' + (', '.join(f'{m}: {n}' for m, n in b.items()) if b else 'none'))
    n, secs, refused = waits(7)
    print(f'  last 7 days: {n} requests queued for a slot ({secs / 60:.1f} min total), {refused} swaps refused '
          '(model in use by another run). Rising counts mean contention between projects.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
