"""crashes - when LM Studio's engine crashes and takes a model with it, the hub notices, records it and loads it again.

The failures: LM Studio's MLX engine can die with "[metal::malloc] Resource limit (499000) exceeded" (its count of Metal
buffers, which grows with the work a copy has done), or with "[METAL] Command buffer execution failed: Insufficient Memory
(kIOGPUCommandBufferCallbackErrorOutOfMemory)" (the Mac's memory ran out under several long contexts at once), then a
segmentation fault; every request on that copy ends with "Channel Error" or "The model has crashed without additional
information", and LM Studio does not load the model again. Left alone, a Mac can run no model until a person loads one.
ENGINE is the one list of the engine's failure words: the gateway reads a request's error with it (a crash: held and
sent again) and this module reads LM Studio's log with it (an exception about the engine keeps its words in the record).

How the hub tells a crash from an unload, from LM Studio's own log (~/.lmstudio/server-logs): a request on a model that
crashed ends with "- Caused By: Error: The model has crashed ..." under "[ERROR][<model>]"; a model someone unloaded ends
its requests with "ExplicitModelUnloadError: Model unloaded." and leaves "Unloading model: <id>". A model that goes away
with no request on it, no unload line, and either its engine's "Fatal Python error" or a restart of LM Studio's server
in the log counts as crashed when it is a family in models.json; otherwise it is let go (someone unloaded it).

  watch()       one look, by the door's keeper every 20 s (and at once when a request through the door fails): new
                crash lines in LM Studio's log, what went away, and what to load again: a family's every copy with its
                models.json settings, another model as it was loaded. A copy named in a crash line is down whatever the
                listing in hand says (the listing may be older than the crash); a copy that merely seems missing counts as
                missing only when LM Studio's own server does not list it either, no request is running on it, and two
                looks 20 s apart agree (the listing is slow under load). At most 3 reloads of one copy within an hour: a
                copy that crashes again after that, or that cannot be loaded again within an hour, leaves its family down
                for a person (localmodels use <model>), the dash says so, and the keeper tries once more every hour; the
                other copies keep answering meanwhile. A swap (localmodels use) ends the wait: the person chose.
  reloading()   while a crashed model is being loaded again, the door holds requests that only it could answer (up to
                10 minutes) instead of passing on LM Studio's "no model loaded"; down_copies() names the copies to route
                around while the others answer.
  history()     the crashes of the last days, a model's copies that crashed together as one (the dash, localmodels crashes).
  scan()        crashes in LM Studio's logs from before the hub watched for them.

Kept in ~/Library/Application Support/LocalModels/: crashes.jsonl (time, model, LM Studio's own error words, what the
keeper did, and every recycle; never content) and keeper.json (what the keeper last saw loaded and since when, how far it
has read LM Studio's log, any model that is down after a crash, the recycle in progress).
"""
import contextlib, fcntl, glob, json, os, re, threading, time

from . import llmshare

LOGS = os.path.expanduser(os.environ.get('LMSTUDIO_LOGS') or '~/.lmstudio/server-logs')
RECORD = os.path.join(llmshare.SHARED, 'crashes.jsonl')
STATE = os.path.join(llmshare.SHARED, 'keeper.json')
CAP, WINDOW = 3, 3600            # reloads of one model within an hour; a crash after the third leaves it down
GIVE_UP = 3600                   # a crashed model not loaded again within an hour (no room, a load that fails) is left down
RETRY_LEFT = 3600                # a model left down is tried once more this often
HOLD = 600                       # the longest the door holds a request for a model that is loading again
MISS = 20                        # seconds a copy must be missing from the listing (two looks) before it counts as gone
WAKE = threading.Event()         # the door sets it when a request fails: the keeper looks at once, not in 20 s

STAMP = re.compile(r'^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]\[(\w+)\](?:\[([^\]]+)\])?')
CAUSE = re.compile(r'^- Caused By: (?:Error: )?(The model has crashed.{0,120})')
ERROR = re.compile(r'^([A-Za-z_][\w.]*(?:Error|Exception)): (.{0,300})')
FATAL = re.compile(r'Fatal Python error: (.{1,80})')
LOADING = re.compile(r'\[Endpoint=(loadModel|unloadModel)\] (?:Loading|Unloading) model: (\S+)')
# The engine's own failure words, most telling first (engine_failure names a failure by the first that matches): Metal's
# out-of-memory error for a command buffer, its buffer-count limit, the signals that end the engine, and what LM Studio
# answers every request in flight when the generation thread dies (measured: "Channel Error", with the instance still
# listed for a moment). LM Studio's load guardrail ("insufficient system resources") is a refused load, not a crash: no word
# here matches it.
ENGINE_WORDS = (r'insufficient memory', r'out of memory', r'kIOGPUCommandBufferCallbackError\w*', r'command buffer execution failed',
                r'resource limit(?: \(\d+\))?', r'metal::\w+', r'\[metal\]', r'segmentation fault', r'abort trap', r'bus error',
                r'fatal exception', r'generation thread', r'channel error', r'exit code')
ENGINE = re.compile('|'.join(ENGINE_WORDS), re.I)
_ENGINE_EACH = [re.compile(w, re.I) for w in ENGINE_WORDS]
_ctx, _seen_state = {}, {'at': 0.0, 'st': {}}


def engine_failure(text):
    """The engine's failure in LM Studio's words when this text has any of them (the most telling: 'Insufficient Memory'
    before '[METAL]', 'Resource limit (499000)' before 'Channel Error'), else None."""
    for rx in _ENGINE_EACH:
        m = rx.search(str(text or ''))
        if m: return m.group(0)
    return None


# ---------------------------------------------------------------- LM Studio's log
def _t(stamp):
    return time.mktime(time.strptime(stamp, '%Y-%m-%d %H:%M:%S'))


def parse(lines, ctx=None):
    """Lines of LM Studio's server log -> (crashes [{at, model, cause, error}], events [(at, 'loadModel' | 'unloadModel' |
    'serverStart', key or id)], the engine's last fatal error (at, words) or None). Keeps only LM Studio's and the engine's
    own error words, never a request's content: an exception's message is kept only when it is about the engine (Metal,
    memory, a signal), else just its name."""
    ctx = _ctx if ctx is None else ctx
    crashes, events, fatal = [], [], None
    for line in lines:
        if line.startswith('['):
            m = STAMP.match(line)
            if not m: continue
            ctx['at'], ctx['model'] = _t(m.group(1)), (m.group(3) if m.group(2) == 'ERROR' else None)
            if '[Endpoint=' in line:
                x = LOADING.search(line)
                if x: events.append((ctx['at'], x.group(1), x.group(2)))
            elif 'Fatal Python error' in line:
                x = FATAL.search(line)
                if x: fatal = (ctx['at'], x.group(1).strip())
            elif line.endswith('Server started.') or 'HTTP server listening on port' in line:
                events.append((ctx['at'], 'serverStart', ''))
            continue
        if line.startswith('- Caused By:'):
            x = CAUSE.match(line)
            if x and ctx.get('model'):
                err = ctx.get('error') if ctx.get('error_at', 0) >= ctx.get('at', 0) - 30 else None
                crashes.append(dict(at=ctx['at'], model=ctx['model'], cause=x.group(1).strip(), error=err))
            continue
        x = ERROR.match(line)
        if x:
            ctx['error'] = (x.group(1) + (': ' + x.group(2).strip() if ENGINE.search(x.group(2)) else ''))[:200]
            ctx['error_at'] = ctx.get('at', 0)
        elif line.startswith('Fatal Python error'):                       # a fatal error can come on a line of its own, without a stamp
            x = FATAL.search(line)
            if x: fatal = (ctx.get('at', 0), x.group(1).strip())
    return crashes, events, fatal


def _files():
    """LM Studio's server logs, oldest first (server-logs/2026-11/2026-11-07.3.log: that day's third)."""
    out = []
    for p in glob.glob(os.path.join(LOGS, '*', '*.log')):
        m = re.match(r'(\d{4}-\d\d-\d\d)\.(\d+)\.log$', os.path.basename(p))
        if m: out.append((m.group(1), int(m.group(2)), p))
    return [p for *_, p in sorted(out)]


def _new_lines(st, most=32 << 20):
    """What LM Studio has written to its log since the last look. The very first look starts at the end, so nothing old
    is taken for new (scan() reads the old)."""
    files, cur = _files(), st.get('log') or {}
    if not files: return []
    if not cur.get('file'):
        st['log'] = dict(file=files[-1], pos=os.path.getsize(files[-1]), at=time.time()); return []
    if cur['file'] in files: todo = [(cur['file'], cur.get('pos', 0))] + [(f, 0) for f in files[files.index(cur['file']) + 1:]]
    else: todo = [(f, 0) for f in files if os.path.getmtime(f) >= cur.get('at', 0)]       # its file went away: what is newer
    lines = []
    for k, (f, pos) in enumerate(todo):
        try:
            size = os.path.getsize(f)
            if size < pos: pos = 0                                               # the file started again
            with open(f, 'rb') as h: h.seek(pos); data = h.read(min(size - pos, most))
        except OSError: continue
        cut = data.rfind(b'\n') + 1 if k == len(todo) - 1 else len(data)          # the newest: whole lines, the rest next time
        lines += data[:cut].decode('utf-8', 'replace').split('\n')
        st['log'] = dict(file=f, pos=pos + cut, at=time.time())
    return lines


# ---------------------------------------------------------------- the hub's records
@contextlib.contextmanager
def _state():
    """keeper.json, read and written under its own lock (the door's keeper and a `localmodels keep` by hand may look at once)."""
    os.makedirs(llmshare.SHARED, exist_ok=True)
    with open(STATE + '.lock', 'a+') as h:
        fcntl.flock(h, fcntl.LOCK_EX)
        st = _read(fresh=True)
        yield st
        st['at'] = time.time()
        try: llmshare._write(STATE, st)
        except OSError: pass
        _seen_state.update(at=time.time(), st=st)


def _read(fresh=False):
    if fresh or time.time() - _seen_state['at'] > 1:
        try: st = json.load(open(STATE))
        except (OSError, ValueError): st = {}
        _seen_state.update(at=time.time(), st=st)
    return _seen_state['st']


def _record(rec):
    llmshare._append(RECORD, dict(rec, at=round(rec.get('at') or time.time(), 1)))


def records(days=7):
    since, out = time.time() - days * 86400, []
    try:
        for line in open(RECORD):
            try: r = json.loads(line)
            except ValueError: continue
            if r.get('at', 0) >= since: out.append(r)
    except OSError: pass
    return sorted(out, key=lambda r: r.get('at', 0))


def _served(model, since):
    """What a copy had answered through the hub since it was loaded: requests and tokens, prompt and written."""
    if not since: return None
    rows = [r for r in llmshare.requests_log((time.time() - since) / 3600 + 0.1)
            if (r.get('copy') or r.get('model')) == model and r.get('at', 0) >= since]
    w, p = sum(r.get('completion_tokens') or 0 for r in rows), sum(r.get('prompt_tokens') or 0 for r in rows)
    return dict(since=round(since), requests=len(rows), written=w, prompt=p, tokens=w + p)


# ---------------------------------------------------------------- the keeper's look
def _how(base, d, seen):
    """How to load a crashed model again -> (ids, key, context, parallel, size): a family in models.json, every copy with
    its settings; another model, the ones that crashed, as they were loaded."""
    f = (llmshare.settings().get('families') or {}).get(base)
    sizes = [s.get('size') for i, s in seen.items() if llmshare.family(i) == base and s.get('size')]
    if f: return llmshare.copy_ids(base, f.get('copies', 1)), f.get('key') or base, f.get('context'), f.get('parallel'), (sizes or [None])[0]
    s = next((seen[i] for i in d['models'] if i in seen), None)
    if not s: return list(d['models']), None, None, None, None
    return list(d['models']), s.get('key'), s.get('context'), s.get('parallel'), s.get('size')


def _confirm_missing(st, ids, have, api, busy, now):
    """Of these ids, the ones that are gone for sure: not in the listing, not listed by LM Studio's own server (when it
    answered), no slot held on them, and missing for MISS seconds (two looks). The listing must be whole. -> set."""
    missing, gone = st.setdefault('missing', {}), set()
    for i in ids:
        if i in have or (api is not None and i in api) or busy.get(llmshare._safe(i)) or not llmshare.read_ok():
            missing.pop(i, None); continue
        missing.setdefault(i, now)
        if now - missing[i] >= MISS: gone.add(i)
    return gone


def confirm_missing(ids, rows, busy=None):
    """For the keeper's other jobs: which of these ids are gone for sure (see _confirm_missing)."""
    with _state() as st:
        return _confirm_missing(st, ids, {r['id'] for r in rows}, llmshare.api_models(), busy or {}, time.time())


def watch(rows, listed_at=None, busy=None, now=None):
    """One look. rows: what LM Studio holds now (`lms ps`, read just before; listed_at: when that read began); busy: the
    slots held per copy; now: the time of the look (a test moves it). -> the models to load again now: [{base, ids, key,
    context, parallel, size}] (models.keep loads them under the swap lock)."""
    now, have, busy = now or time.time(), {r['id']: r for r in rows}, busy or {}
    listed_at = listed_at or now
    api = llmshare.api_models()
    fams = llmshare.settings().get('families') or {}
    with _state() as st:
        found, events, fatal = parse(_new_lines(st))
        seen, down, last = st.setdefault('seen', {}), st.setdefault('down', {}), st.setdefault('last', {})
        if fatal: st['fatal'] = dict(at=fatal[0], words=fatal[1])
        unloaded = {m for _, verb, m in events if verb == 'unloadModel'}
        restarted = any(verb == 'serverStart' for _, verb, _ in events)
        # a copy named in a crash line is down, whatever the listing in hand says; one crash per copy per life
        for c in found:
            lst, since = last.get(c['model'], 0), (seen.get(c['model']) or {}).get('since') or 0
            if c['at'] <= lst or (c['at'] - lst < 120 and since <= lst): continue
            last[c['model']] = c['at']
            base = llmshare.family(c['model'])
            _record(dict(at=c['at'], event='crash', model=c['model'], family=base, cause=c['cause'], error=c.get('error'),
                         served=_served(c['model'], since or None), found='as it happened'))
            d = down.setdefault(base, dict(crashed=c['at'], models=[], state='loading again'))
            if c['model'] not in d['models']: d['models'].append(c['model'])
            seen.setdefault(c['model'], {})['loaded'] = False
            st.get('missing', {}).pop(c['model'], None)
        # a copy that went away with no request on it: gone for sure only when the server agrees and two looks agree
        was = [i for i, s in seen.items() if s.get('loaded')]
        for i in _confirm_missing(st, was, have, api, busy, now):
            base = llmshare.family(i)
            seen[i]['loaded'] = False
            if i in unloaded or i in last and last[i] >= (seen[i].get('since') or 0): continue       # unloaded on purpose, or its crash is recorded
            f = st.get('fatal') or {}
            died = now - f.get('at', 0) < 600 or restarted
            if base in fams or died:
                why = (f"engine terminated (Fatal Python error: {f['words']})" if now - f.get('at', 0) < 600 else
                       "LM Studio's server restarted and the instance was not reloaded" if restarted else 'instance disappeared with no unload and no request on it')
                last[i] = now
                _record(dict(at=now, event='crash', model=i, family=base, cause=why, error=None, served=_served(i, seen[i].get('since')), found='as it happened'))
                d = down.setdefault(base, dict(crashed=now, models=[], state='loading again'))
                d['gone'] = True                                          # seen missing from this very listing: no newer one is needed
                if i not in d['models']: d['models'].append(i)
        for i in unloaded:
            if i in seen and i not in have: seen[i]['loaded'] = False
        for i, r in have.items():
            s = seen.setdefault(i, {})
            if not s.get('loaded'): s['since'] = now                  # loaded since the last look (or first seen now)
            s.update(key=r['key'], context=r['context'], parallel=r['parallel'], size=r['size'], loaded=True, at=now)
        for i in [i for i, s in seen.items() if not s.get('loaded') and now - s.get('at', 0) > 86400]: del seen[i]
        for m in [m for m, t in last.items() if now - t > 86400]: del last[m]
        jobs = []
        for base, d in list(down.items()):
            ids, key, context, parallel, size = _how(base, d, seen)
            if listed_at <= d['crashed'] + 2 and not d.get('gone'): continue    # the listing in hand predates the crash: decide on the next one
            d['models'] = [i for i in d.get('models') or [] if i not in have]     # a crashed copy listed again serves at once; a sibling
            if not d['models'] or all(i in have for i in ids):                    # still missing is the replica path's (models.keep)
                if not d.get('keeper'): _record(dict(event='back', family=base, by='someone else'))
                del down[base]; continue
            if d['state'] == 'left down':
                if now - d.get('since', now) > 86400: del down[base]; continue   # on the dash for a day, then only in the record
                if now - d.get('retry_at', d.get('since', now)) < RETRY_LEFT: continue
                d.update(state='loading again', why='hourly retry', retry_at=now, fails=0, clock=now)   # a new hour to load it in
            reloads = st.setdefault('reloads', {})                              # reloads that worked, per copy
            tries = max([len([t for t in reloads.get(i) or [] if t > now - WINDOW]) for i in (d.get('models') or ids)] or [0])
            why = ('its load parameters were never observed, so it cannot be reloaded with the same settings' if not key else
                   f'crashed again after {tries} reloads of one instance within an hour' if tries >= CAP else
                   'not reloaded within an hour' if now - d.get('clock', d['crashed']) > GIVE_UP else None)
            if why:
                d.update(state='left down', why=why, since=d.get('since', now), retry_at=now)     # since: when it was first left down
                _record(dict(event='left down', family=base, why=why)); continue
            if d.get('next_try', 0) > now: continue                            # the last load failed: a pause before the next
            jobs.append(dict(base=base, ids=[i for i in ids if i not in have], key=key, context=context, parallel=parallel, size=size))
        st['reloads'] = {b: [t for t in ts if t > now - WINDOW] for b, ts in (st.get('reloads') or {}).items() if any(t > now - WINDOW for t in ts)}
        return jobs


def stale_down(listed_at):
    """Whether a model went down after the listing taken at listed_at began: the keeper then lists again before deciding."""
    return any(d.get('state') in ('loading again', 'waiting for room') and listed_at <= d.get('crashed', 0) + 2 and not d.get('gone')
               for d in (_read(fresh=True).get('down') or {}).values())


def trying(base):
    """The keeper is about to load a crashed model again."""
    with _state() as st:
        d = (st.get('down') or {}).get(base)
        if d: d.update(state='loading again', why=None, keeper=True)


def waiting(base, why):
    with _state() as st:
        d = (st.get('down') or {}).get(base)
        if d and d.get('state') != 'waiting for room': d.update(state='waiting for room', why=why)
        elif d: d['why'] = why


def loaded(base, ids, ok, seconds, words=''):
    """What the keeper's load came to: a copy back serves at once, and when every copy the crash took is back the crash is
    over (a sibling that never fitted is the replica path's). A reload that
    worked counts toward the 3 an hour; one that failed is tried again after a pause (1, 2, then 5 minutes). A copy that
    crashed while it drained for a recycle comes back fresh: the reload was its recycle, so that job ends here (recorded as
    'recycle given up', with why) instead of unloading the fresh copy again."""
    with _state() as st:
        d = (st.get('down') or {}).get(base)
        if ok:
            _record(dict(event='loaded again', family=base, models=ids, seconds=round(seconds, 1), by='the hub'))
            for i in ids: st.setdefault('reloads', {}).setdefault(i, []).append(time.time())
            if d:                                            # the crash is over when every copy it took is back
                d['models'] = [m for m in d.get('models') or [] if m not in ids]
                if not d['models']: del st['down'][base]
            for i in ids: st.setdefault('seen', {}).setdefault(i, {}).update(loaded=True, since=time.time(), at=time.time())
            st.get('missing', {}).pop(base, None)
            r = st.get('recycle') or {}
            j = r.get('job')
            if j and j.get('copy') in ids:
                _record(dict(event='recycle given up', family=base, model=j['copy'],
                             why='crashed while draining; crash recovery reloaded it, so its count starts again'))
                r['job'] = None
                r.setdefault('last', {})[base] = time.time()
        else:
            _record(dict(event='could not load', family=base, models=ids, words=str(words)[-200:]))
            if d:
                d['fails'] = int(d.get('fails') or 0) + 1
                d['next_try'] = time.time() + min(300, 60 * 2 ** (d['fails'] - 1))
                d['why'] = 'last load failed: ' + str(words)[-120:]


def load_ok(base):
    """A load of this family by the hub worked on this Mac: it can load it again (recycle.plan relies on it for a lone copy)."""
    with _state() as st:
        e = st.setdefault('loads', {}).setdefault(base, {})
        e['ok'] = time.time()


def load_refused(base, words, cond):
    """LM Studio refused to load this family for the hub (its loading guardrail): the words for a person, and what it was
    refused under (cond: models.conditions(), the guardrail settings and the GPU's limit), so the keeper does not try
    again until one of them changes."""
    with _state() as st:
        e = st.setdefault('loads', {}).setdefault(base, {})
        e.update(refused=time.time(), words=str(words)[:400], cond=list(cond))
        _record(dict(event='load refused', family=base, words=str(words)[:200]))


def load_blocked(base, cond):
    """Words when the hub's last load of this family was refused and nothing has changed since (cond, as load_refused
    took it), else None: then a load would be refused again, so the keeper does not try."""
    e = (_read().get('loads') or {}).get(base) or {}
    if not e.get('refused') or (e.get('ok') or 0) > e['refused'] or list(cond) != e.get('cond'): return None
    return e.get('words') or 'load refused by LM Studio'


def reload_proven(base):
    """Whether the hub has loaded this family on this Mac since its last refusal (so an unload for a recycle can be undone)."""
    e = (_read().get('loads') or {}).get(base) or {}
    return bool(e.get('ok')) and (e.get('refused') or 0) < e['ok']


def loaded_now(ids):
    """The hub loaded these copies this moment (a swap, a kept copy, a recycle): their counts start again."""
    with _state() as st:
        for i in ids:
            st.setdefault('seen', {}).setdefault(i, {}).update(loaded=True, since=time.time(), at=time.time())
            st.get('missing', {}).pop(i, None)


def swapped(to):
    """A swap (localmodels use) ends any wait to load a crashed model again: the person chose."""
    with _state() as st:
        for base in list((st.get('down') or {})):
            if base == llmshare.family(to): _record(dict(event='back', family=base, by='localmodels use'))
            else: _record(dict(event='cancelled', family=base, why=f'superseded by a swap to {to}'))
            del st['down'][base]


def after_maintenance(rows):
    """Maintenance is over: what LM Studio wrote to its log meanwhile (a restart, unloads) is skipped, never read as
    crashes, and the keeper's picture starts from what is loaded now (a copy still loaded keeps its count)."""
    now = time.time()
    have = {r['id'] for r in rows}
    with _state() as st:
        st['log'] = {}                                       # the next look starts at the end of the log
        seen = {i: dict(s, loaded=True) for i, s in (st.get('seen') or {}).items() if i in have}
        for i in have: seen.setdefault(i, dict(loaded=True, since=now))
        st['seen'], st['down'], st['missing'] = seen, {}, {}


def nudge():
    """A request through the door failed: maybe a crash. The keeper looks at once (at most every 5 s)."""
    if time.time() - _seen_state.get('nudged', 0) > 5:
        _seen_state['nudged'] = time.time(); WAKE.set()


def down_copies(model):
    """The copies of this model that are down after a crash right now (being loaded again, or waiting for room): the
    door routes around them, and holds a request only when no copy is left to answer it."""
    d = (_read().get('down') or {}).get(llmshare.family(model))
    if not d or d.get('state') not in ('loading again', 'waiting for room'): return set()
    return set(d.get('models') or [])


def reloading(model):
    """Whether this model (a family or one copy) is down after a crash with the hub loading it again."""
    d = (_read().get('down') or {}).get(llmshare.family(model))
    return bool(d) and d.get('state') in ('loading again', 'waiting for room')


def left_down(model):
    """Whether the hub has given up on this model after a crash: a request for it is answered 503 at once, not forwarded
    (LM Studio's just-in-time loading would otherwise load a stray copy with its own settings)."""
    d = (_read().get('down') or {}).get(llmshare.family(model))
    return bool(d) and d.get('state') == 'left down'


def down():
    """The models down after a crash now, in plain words: [{family, crashed, models, state, why, words}]."""
    out = []
    for base, d in (_read(fresh=True).get('down') or {}).items():
        out.append(dict(family=base, crashed=d.get('crashed'), models=d.get('models') or [], state=d.get('state'), why=d.get('why'),
                        label=LABEL.get(d.get('state'), d.get('state')),
                        which=_which(base, d.get('models') or []),
                        words=f"{base} crashed at {time.strftime('%H:%M', time.localtime(d.get('crashed') or 0))} ({_which(base, d.get('models') or [])}); "
                              + _state_words(base, d.get('state'), d.get('why'))))
    return out


# ---------------------------------------------------------------- the record, read back
def _dur(s):
    s = max(0, int(s))
    return f'{s} s' if s < 90 else f'{round(s / 60)} min' if s < 5400 else f'{s // 3600} h {s % 3600 // 60} min'


def _which(base, models):
    n = int(((llmshare.settings().get('families') or {}).get(base) or {}).get('copies', 1))
    if n < 2: return 'the model'
    if len(set(models)) >= n: return 'both replicas' if n == 2 else f'all {n} replicas'
    return ', '.join('replica ' + (re.match(r'.*:(\d+)$', m) or [0, '1'])[1] for m in sorted(set(models)))


LABEL = {'left down': 'left unloaded', 'loading again': 'reloading', 'waiting for room': 'reload pending (memory)', 'cancelled': 'reload cancelled'}


def _state_words(base, state, why):
    if state == 'left down': return f'left unloaded: {why}. Reload manually: localmodels use {base} (the instance manager retries hourly)'
    if state == 'waiting for room': return 'reload pending: insufficient memory' + (f' ({why})' if why else '')
    if state == 'loading again': return 'reloading; requests only it can serve are held' + (f' ({why})' if why else '')
    if state == 'cancelled': return f'reload cancelled: {why}'
    return state or ''


def history(days=7):
    """The crashes of the last days, newest first, a model's copies that crashed together as one: [{at, family, models,
    which, error, cause, found, served, failed (requests that got an error), back, by, seconds, state, why, then (words)}]."""
    out = []
    for r in records(days):
        fam, ev = r.get('family') or llmshare.family(r.get('model')), r.get('event')
        if ev == 'crash':
            x = next((x for x in out if x['family'] == fam and r['at'] - x['at'] < 120), None)
            if x:
                if r.get('model') not in x['models']: x['models'].append(r.get('model'))
                x['error'] = x['error'] or r.get('error')
                if r.get('served'): x['served'].append(dict(r['served'], model=r.get('model')))
                continue
            out.append(dict(at=r['at'], family=fam, models=[r.get('model')], error=r.get('error'), cause=r.get('cause'), found=r.get('found'),
                            served=[dict(r['served'], model=r.get('model'))] if r.get('served') else [], back=None, by=None,
                            seconds=None, state=None, why=None))
            continue
        if ev in ('recycled', 'recycle given up'): continue
        x = next((x for x in reversed(out) if x['family'] == fam and x['at'] <= r['at'] and not x['back'] and x['state'] != 'cancelled'), None)
        if not x: continue
        if ev in ('loaded again', 'back'): x.update(back=r['at'], by=r.get('by'), seconds=r.get('seconds'), state=None, why=None)
        elif ev in ('left down', 'cancelled'): x.update(state=ev, why=r.get('why'))
    bad = [q for q in llmshare.requests_log(days * 24 + 1) if (q.get('status') or 0) >= 400]
    now = _read(fresh=True).get('down') or {}
    for x in out:
        x['which'] = _which(x['family'], x['models'])
        end = x['back'] or (x['at'] + HOLD)
        x['failed'] = sum(1 for q in bad if llmshare.family(q.get('copy') or q.get('model')) == x['family'] and x['at'] - 5 <= q.get('at', 0) <= end)
        d = now.get(x['family'])
        if d and not x['back'] and x['state'] != 'cancelled' and abs((d.get('crashed') or 0) - x['at']) < 120: x['state'], x['why'] = d.get('state'), d.get('why')
        if x['back']:
            x['then'] = (f"reloaded by the instance manager after {_dur(x['back'] - x['at'])}" if x['by'] == 'the hub' else
                         f"back after {_dur(x['back'] - x['at'])}, loaded " + ('outside the gateway' if x['by'] == 'someone else' else f"by {x['by']}"))
        else: x['then'] = _state_words(x['family'], x['state'], x['why']) or 'not reloaded yet'
    return list(reversed(out))


def brief(x):
    """One crash in a few words, for an 80-column dash: '02 Mar 04:15, both replicas · back after 18 s'."""
    when = time.strftime('%d %b %H:%M', time.localtime(x['at']))
    return f"{when}, {x['which']} · " + (f"back after {_dur(x['back'] - x['at'])}" + (' (instance manager)' if x['by'] == 'the hub' else '') if x['back'] else LABEL.get(x['state'], x['state']) or 'not back yet')


def words(x):
    """One crash in a line: '02 Mar 04:15 qwen/qwen3.6-35b-a3b, both replicas: RuntimeError: ... · 12 requests failed · ...'."""
    return (f"{time.strftime('%d %b %H:%M', time.localtime(x['at']))} {x['family']}, {x['which']}: {x['error'] or x['cause']}"
            + (f" · {x['failed']} request{'s' if x['failed'] != 1 else ''} failed" if x['failed'] else '') + f" · {x['then']}")


# ---------------------------------------------------------------- crashes from before the hub looked
def scan(days=7):
    """Read LM Studio's logs of the last days for crashes the hub has no record of (from before it watched),
    and record them, with when the model was loaded again (LM Studio's first load of it afterwards). -> how many added."""
    since, ctx, found, loads = time.time() - days * 86400, {}, [], []
    for f in _files():
        if os.path.getmtime(f) < since: continue
        try:
            with open(f, 'rb') as h: lines = h.read().decode('utf-8', 'replace').split('\n')
        except OSError: continue
        c, l, _ = parse(lines, ctx)
        found += [dict(x, log=os.path.basename(f)) for x in c]; loads += l
    known = [r for r in records(days + 1) if r.get('event') == 'crash']
    fams, added, last = llmshare.settings().get('families') or {}, [], {}
    for c in found:
        if c['at'] - last.get(c['model'], 0) < 120: continue
        last[c['model']] = c['at']
        if any(r.get('model') == c['model'] and abs(r.get('at', 0) - c['at']) < 120 for r in known): continue
        base = llmshare.family(c['model'])
        _record(dict(at=c['at'], event='crash', model=c['model'], family=base, cause=c['cause'], error=c.get('error'),
                     found=f"later, in LM Studio's log ({c['log']})"))
        added.append(dict(at=c['at'], family=base))
    done = []
    for a in added:
        if any(a['family'] == d['family'] and abs(a['at'] - d['at']) < 120 for d in done): continue
        done.append(a)
        key = (fams.get(a['family']) or {}).get('key') or a['family']
        t = next((t for t, verb, k in sorted(loads) if verb == 'loadModel' and k == key and t > a['at']), None)
        if t: _record(dict(at=t, event='back', family=a['family'], by="someone else (LM Studio's log shows the load)"))
    return len(added)
