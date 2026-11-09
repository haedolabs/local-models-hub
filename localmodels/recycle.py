"""recycle - a copy of a model is unloaded and loaded again before it reaches the amount of work at which LM Studio's engine
has crashed, so the crash is less likely to come at all.

Why: LM Studio's MLX engine dies with "[metal::malloc] Resource limit (499000) exceeded" after a large amount of work on a
copy: the engine's count of Metal buffers grows with the work a copy has done and is only freed when the copy is unloaded.
Measured, tokens processed (prompt and answer together) per copy since its load: with short answers (1,700 to 2,300
tokens written per request) three crashes at 931,000 to 1,175,000, and one copy served 1,354,000 without dying; with long
answers (3,100 to 4,300 written per request) four crashes at 374,691 to 478,356 on one Mac and 445,999 on another, one at
244,748, while one copy served 653,132. No single count (tokens processed, tokens written, requests, the sum of squared
answer lengths) separates the crashes from the survivors, so the band is a hazard zone, not a wall: the threshold sits
below the lowest crash under the heaviest load measured, with room for the answers in flight while a copy drains
(4 answers of 10,000 tokens). Recycling is insurance; the hub's reload of a crashed copy with the door holding and
resending requests (crashes.py, gateway.py) stays the safety net.
This is what web servers do with workers that leak (gunicorn's max_requests, Apache's MaxRequestsPerChild): recycle each
worker after a set amount of work, one at a time and staggered, so the service never stops.

How: models.json gives a family "recycle": {"tokens": 300000, "written": 150000, "soft": 220000, "before_batch": 0.5,
"requests": 0}: tokens processed and tokens written (the answers alone) since the copy was loaded, counted from the hub's
request log; a copy is as far along its band as the count furthest along its own threshold ("recycle": false turns it
off; "written": 0 leaves the answers uncounted; "requests" is a backstop for clients that report no usage, 0 = off).
With long answers every crash measured came at 197,524 to 213,269 tokens written (295,340 to 478,356 processed), so the
written count is the tighter one there; with short answers the processed count reaches its threshold first. The door's keeper (models.keep, every 20 s) asks plan(): a copy past
"tokens", counting the context of its requests in flight (each in-flight marker's estimate, admission.py: with prompt-heavy
work, agent conversations of 18,000 to 44,000 tokens that re-send their history, a copy crossed 300,000 within minutes and
its requests in flight carried it to 404,635 before it could drain), is marked DRAINING, the door sends it nothing new
(gateway.generate leaves it out; a lone copy's requests, or
requests that name that copy, are held as after a crash), its requests in flight finish, and when none is left (the slot
locks and LM Studio's own status agree) models.keep unloads it and loads it again with the family's settings, under the
swap lock, and records it in crashes.jsonl (event "recycled"). LM Studio's server resolves an unload by name to the
oldest-loaded instance of that model key, which is a family's first copy's name: so the first copy is recycled only when it
is the oldest, and an older sibling goes first (oldest_first). models._recycle also checks which instance went: when LM
Studio took another one, that one is loaded again at once and the recycle is given up and retried after RETRY. A copy past "soft" that is idle anyway is recycled on the spot, so the hard
drain rarely runs. before_batch: when a batch begins it asks the door (POST /hub/recycle, which
agent.batch does; or `localmodels recycle`), and a copy past that share of its band is recycled first, so a night's work
starts on fresh copies. One copy at a time; never while its family has a copy down or missing; two minutes between two
recycles of a family, except for a copy past its threshold, which drains 30 s after its sibling is back (GAP_HARD: while
one copy drains the other takes every request, so it reaches its own threshold sooner); a crash during a drain ends it
(the reload is the recycle: crashes.loaded); a drain that has not emptied in 10 minutes is given up and tried again 10 minutes later. A copy
loaded with other settings than the family's (LM Studio's window, its just-in-time loading) is recycled the same way, so
the family comes back to its settings.

State: keeper.json, under "recycle" (the job in progress, each family's last recycle, fresh-start requests), beside the
crash state and under the same lock (crashes._state). The count starts at the copy's load as the hub saw it
(crashes.loaded_now); a reload made outside the hub within one 20-s look is invisible, so the dash shows each count's start.
"""
import time

from . import crashes, llmshare

DEFAULT = dict(tokens=300_000, written=150_000, soft=220_000, before_batch=0.5, requests=0)
DRAIN_MAX, RETRY, GAP, FRESH_FOR = 600, 600, 120, 1800   # seconds: a drain given up; try again after; between two recycles of a family; a fresh-start request lasts
GAP_HARD = 30             # seconds after a family's last recycle before a copy past its threshold drains (GAP holds the rest)
_counts = {'at': 0.0, 'by': {}}                           # the request log is read at most every 20 s, for every copy at once
_held = {}                                                # family -> why its lone copy is not recycled (plan's last look)


def held_back():
    """The families whose recycle waits because a lone copy might not load again, with why: {family: words}."""
    return dict(_held)


def guardrail_off():
    """Whether LM Studio's model-loading guardrail is Off on this Mac (its settings.json): then it does not refuse the
    hub's loads, so a lone copy unloaded for a recycle can be loaded again."""
    from . import peers
    return peers.guardrail_mode() == 'off'


def thresholds(f):
    """A family's recycle settings (models.json), or None when it is turned off."""
    r = f.get('recycle', True) if isinstance(f, dict) else True
    if r is False or r is None: return None
    out = dict(DEFAULT)
    if isinstance(r, dict): out.update({k: v for k, v in r.items() if k in out and isinstance(v, (int, float))})
    return out


def _since(seen, copy):
    return (seen.get(copy) or {}).get('since')


def counts(copy, since):
    """What a copy has answered through the hub since it was loaded: {requests, written, prompt, tokens, since}. since None
    (the hub never saw the load): the last day, which can only count too much and recycle too soon, never too late."""
    t0 = since or time.time() - 86400
    hit = _counts['by'].get(copy)
    if hit and hit['since'] == round(t0) and time.time() - _counts['at'] < 20: return hit
    oldest = min([t0] + [v['since'] for v in _counts['by'].values()])
    rows = llmshare.requests_log((time.time() - oldest) / 3600 + 0.1)
    by = {}
    for r in rows:
        c = r.get('copy') or r.get('model')
        x = by.setdefault(c, dict(requests=0, written=0, prompt=0, tokens=0, rows=[]))
        x['rows'].append(r)
    out = {}
    for c, x in by.items():
        s = hit['since'] if (hit := _counts['by'].get(c)) and c != copy else round(t0)
        mine = [r for r in x['rows'] if r.get('at', 0) >= s]
        w, p = sum(r.get('completion_tokens') or 0 for r in mine), sum(r.get('prompt_tokens') or 0 for r in mine)
        out[c] = dict(requests=len(mine), written=w, prompt=p, tokens=w + p, since=s)
    out.setdefault(copy, dict(requests=0, written=0, prompt=0, tokens=0, since=round(t0)))
    _counts.update(at=time.time(), by=out)
    return out[copy]


def _shares(c, th):
    out = dict(tokens=c['tokens'] / max(1, th['tokens']))
    if th.get('written'): out['written'] = c['written'] / th['written']
    if th.get('requests'): out['requests'] = c['requests'] / th['requests']
    return out


def share(c, th):
    """How far along its band a copy is (1.0: at the hard threshold), by whichever count is furthest along its own
    threshold: tokens processed, tokens written, or requests."""
    return max(_shares(c, th).values())


def lead(c, th):
    """The count furthest along its threshold: 'tokens', 'written' or 'requests'."""
    s = _shares(c, th)
    return max(s, key=s.get)


def _work(c, th):
    """A copy's work since load against its band, in words."""
    return (f"{c['tokens']:,} tokens processed ({c['written']:,} written, {c['requests']} requests) since load; threshold {th['tokens']:,} processed"
            + (f" or {th['written']:,} written" if th.get('written') else ''))


def drift(row, f):
    """Why a loaded copy does not match its family's settings, in words, or None."""
    want_c, want_p = int(f.get('context') or 0), int(f.get('parallel') or 0)
    bad = []
    if want_c and row.get('context') and int(row['context']) != want_c: bad.append(f"context {row['context']:,} instead of {want_c:,}")
    if want_p and row.get('parallel') and int(row['parallel']) != want_p: bad.append(f"{row['parallel']} slots instead of {want_p}")
    return ' and '.join(bad) or None


def _r(st):
    r = st.setdefault('recycle', {})
    for k, v in (('job', None), ('last', {}), ('fresh', {}), ('retry_after', 0)):
        r.setdefault(k, v)
    return r


def job():
    """The recycle in progress, or None: {copy, family, state: draining|reloading, since, why, counts, share}."""
    return ((crashes._read().get('recycle') or {}).get('job')) or None


def draining():
    """The copies the door must send nothing new to right now."""
    j = job()
    return {j['copy']} if j else set()


def plan(rows, busy=None, flying=None):
    """One look (the keeper's): mark the copy that is due for a recycle, if any. rows: what LM Studio holds now; busy: the
    slots held per copy (llmshare.busy_slots); flying: the context in flight per copy, in tokens (admission.in_flight, from
    the gateway's in-flight markers), which counts toward the processed threshold, so a copy that its requests in flight
    would carry past it drains now, not once they have. -> words or None."""
    from . import admission
    busy = busy or {}
    st = crashes._read(fresh=True)
    r = st.get('recycle') or {}
    if r.get('job') or time.time() < (r.get('retry_after') or 0): return None
    seen, down, have = st.get('seen') or {}, st.get('down') or {}, {x['id']: x for x in rows}
    flying = admission.in_flight() if flying is None else flying
    best = None
    _held.clear()
    for base, f in (llmshare.settings().get('families') or {}).items():
        th = thresholds(f)
        if not th or base in down: continue
        ids = llmshare.copy_ids(base, f.get('copies', 1))
        if any(i not in have for i in ids): continue                     # a copy is missing: the keeper's other job comes first
        after = time.time() - (r.get('last') or {}).get(base, 0)
        if after < GAP_HARD: continue
        fresh = time.time() - (r.get('fresh') or {}).get(base, 0) < FRESH_FOR
        for i in ids:
            c = counts(i, _since(seen, i))
            s, ahead = share(c, th), int(flying.get(i) or 0)
            sp = share(dict(c, tokens=c['tokens'] + ahead), th) if ahead else s
            idle = not busy.get(llmshare._safe(i)) and have[i].get('state') == 'IDLE'
            wrong = drift(have[i], f)
            if s >= 1: why, rank = f"recycle threshold reached: {_work(c, th)}", 3 + s
            elif sp >= 1: why, rank = f"recycle threshold reached with the requests in flight: {_work(c, th)}, and ~{ahead:,} tokens in flight", 3 + sp
            elif after < GAP: continue                                    # two minutes after the family's last recycle: only a copy past its threshold
            elif wrong and idle: why, rank = f'load parameters differ from the family settings ({wrong})', 2.5
            elif fresh and s >= th['before_batch']: why, rank = f"batch starting at {int(s * 100)}% of its recycle threshold, past {int(th['before_batch'] * 100)}%: {_work(c, th)}", 2 + s
            elif idle and s >= th['soft'] / max(1, th['tokens']): why, rank = f"idle at {int(s * 100)}% of its recycle threshold, past the soft threshold ({int(100 * th['soft'] / max(1, th['tokens']))}%): {_work(c, th)}", 1 + s
            else: continue
            if len(ids) < 2 and not crashes.reload_proven(base) and not guardrail_off():   # make before break: no unload the hub might not undo
                _held[base] = (f'recycle deferred ({why}): single instance on this host and reload not yet proven (no successful load by '
                               "the instance manager here, guardrail not Off); set LM Studio's model-loading guardrail to Off, or load it once "
                               'through the instance manager (crash recovery, or `localmodels use` after an unload)')
                continue
            if best is None or rank > best[0]: best = (rank, i, base, c, round(s, 2), why)
    if not best: return None
    _, copy, base, c, s, why = best
    older = oldest_first(rows, copy)
    if older:                                                # LM Studio would unload `older` by this name: it goes first
        th = thresholds((llmshare.settings().get('families') or {}).get(base)) or DEFAULT
        why = f'{older} first, so that {copy} becomes the oldest instance of its model key (LM Studio unloads the oldest by that name); {copy} is due: {why}'
        copy = older
        c = counts(copy, _since(seen, copy)); s = round(share(c, th), 2)
    with crashes._state() as st2:
        rr = _r(st2)
        if rr['job']: return None
        rr['job'] = dict(copy=copy, family=base, state='draining', since=time.time(), why=why, counts=c, share=s)
    return f'{copy}: draining for recycle ({why})'


def oldest_first(rows, copy):
    """When this copy's identifier is also its model key (a family's first copy), LM Studio's server resolves an unload by
    that name to the oldest-loaded instance of the key, whichever its identifier (measured: its REST API and `lms unload`
    alike, with its listing oldest first). -> the older sibling an unload would take instead, or None when this copy is
    the oldest (or its name is unambiguous)."""
    row = next((x for x in rows if x['id'] == copy), None)
    if not row or row.get('key') != copy: return None
    same = [x['id'] for x in rows if x.get('key') == copy]
    return same[0] if same and same[0] != copy else None


def ready(rows, busy):
    """Whether the draining copy has nothing in flight: no slot held on it, and LM Studio calls it idle. -> (job, True/False)."""
    j = job()
    if not j: return None, False
    row = next((x for x in rows if x['id'] == j['copy']), None)
    held = busy.get(llmshare._safe(j['copy']), 0)
    return j, held == 0 and (row is None or row['state'] == 'IDLE')


def given_up(copy, why, states=('draining', 'reloading')):
    """The recycle of this copy ends without a reload (recorded as 'recycle given up', with why); it is tried again after
    RETRY seconds, and meanwhile the copy serves as before. -> whether a job ended."""
    with crashes._state() as st:
        r = _r(st)
        j = r['job']
        if not j or j['copy'] != copy or j['state'] not in states: return False
        crashes._record(dict(event='recycle given up', family=j['family'], model=copy, why=str(why)[:300]))
        r['job'], r['retry_after'] = None, time.time() + RETRY
    return True


def give_up_if_stuck():
    """A drain that has not emptied in DRAIN_MAX seconds is let go; the copy is tried again after RETRY seconds. -> words or None."""
    j = job()
    if not j or j['state'] != 'draining' or time.time() - j['since'] < DRAIN_MAX: return None
    given_up(j['copy'], f'still active after {DRAIN_MAX // 60} min of draining', states=('draining',))
    return f"{j['copy']}: still active after {DRAIN_MAX // 60} min of draining; recycle deferred {RETRY // 60} min"


def reloading(copy):
    """The keeper is about to unload and load this copy: the door keeps leaving it out."""
    with crashes._state() as st:
        r = _r(st)
        if r['job'] and r['job']['copy'] == copy: r['job'].update(state='reloading', at=time.time())


def done(copy, ok, seconds, words=''):
    """The recycle's outcome: back (the count starts again) or not (the crash machinery takes over and loads it again)."""
    with crashes._state() as st:
        r, j = _r(st), None
        if r['job'] and r['job']['copy'] == copy: j = r['job']
        base = (j or {}).get('family') or llmshare.family(copy)
        c = (j or {}).get('counts') or {}
        if ok:
            crashes._record(dict(event='recycled', family=base, model=copy, requests=c.get('requests'), written=c.get('written'), prompt=c.get('prompt'),
                                 tokens=c.get('tokens'), hours=round((time.time() - c['since']) / 3600, 2) if c.get('since') else None,
                                 seconds=round(seconds, 1), why=(j or {}).get('why')))
            st.setdefault('seen', {}).setdefault(copy, {}).update(loaded=True, since=time.time(), at=time.time())
            r['last'][base] = time.time()
            r['fresh'].pop(base, None)
        else:
            crashes._record(dict(event='could not load', family=base, models=[copy], words=str(words)[-200:], why='after a recycle'))
            st.setdefault('down', {})[base] = dict(crashed=time.time(), models=[copy], state='loading again', keeper=True, why='its recycle did not load it back')
        r['job'] = None
    _counts['by'].pop(copy, None)


def fresh_start(model):
    """A batch is beginning on this model: copies past before_batch of their band are recycled first. -> the family, or None."""
    base = llmshare.family(model)
    f = (llmshare.settings().get('families') or {}).get(base)
    if f is None or not thresholds(f): return None
    with crashes._state() as st:
        _r(st)['fresh'][base] = time.time()
    return base


def view(rows):
    """For the dash and the status: each loaded copy's work since load against its band, and the recycle in progress.
    -> {copies: {id: {requests, written, prompt, tokens, since, share, band, drift}}, job, last: {family: t}}"""
    st = crashes._read(fresh=True)
    seen, out = st.get('seen') or {}, {}
    fams = llmshare.settings().get('families') or {}
    for x in rows:
        base = llmshare.family(x['id'])
        f = fams.get(base)
        th = thresholds(f) if f else None
        c = counts(x['id'], _since(seen, x['id']))
        out[x['id']] = dict(c, share=round(share(c, th), 2) if th else None, by=lead(c, th) if th else None, drift=drift(x, f) if f else None,
                            band=dict(tokens=th['tokens'], written=th.get('written') or 0, soft=th['soft'], requests=th.get('requests') or 0) if th else None)
    r = st.get('recycle') or {}
    return dict(copies=out, job=r.get('job'), last=r.get('last') or {})


def recent(days=7):
    """The recycles of the last days, newest first: [{at, model, family, requests, written, tokens, hours, seconds, why}]."""
    return [x for x in reversed(crashes.records(days)) if x.get('event') in ('recycled', 'recycle given up')]


def words(j):
    """A recycle in progress, in a line."""
    if not j: return ''
    state = 'draining: no new requests routed to it until in-flight requests complete' if j['state'] == 'draining' else 'unload and reload in progress'
    return f"{j['copy']}: recycling ({state}; {j['why']})"
