"""web - the hub's dashboard: a page at http://localhost:1235 (the door serves it) and the readings behind it.

  /                 the page (dash.html): live state and charts, refreshed every 2 s, laid out to fit one screen
  /hub/snapshot     what is using the models now: LM Studio and each copy's slots (colored by the project holding
                    them) and its work since it was loaded against its recycle band, a copy down or being recycled, the
                    crashes and recycles of the week, every program using a model (with the step it is on), every
                    Claude Code session, the door's queue, the Mac
  /hub/history      the last minutes from the hub's request log and the requests in flight now: answers at once by
                    project, waits, tokens a second, requests by project, and the memory the door samples every 15 s
  /hub/recent       the last requests (project, kind, copy, seconds, waits, tokens, the step; never the content)

Readings are gathered when the page asks (cached 2 s, one computation at a time however many pages ask), so nothing
runs while nobody looks, except the memory sample.
"""
import collections, json, os, re, threading, time

from . import HOME, NAME, VERSION, crashes, dash, errors, llmshare, maint, names, peers, recycle, sessions
from . import names as names_mod          # colors() takes a parameter called names

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dash.html')
COLORS = os.path.join(llmshare.SHARED, 'colors.json')   # project -> color slot, first come, kept while the project is seen
MEMORY = os.path.join(llmshare.SHARED, 'memory.jsonl')  # the memory samples, kept a day, so a restart keeps the chart
SLOTS = 7                                               # categorical slots for projects; the rest share "other"
_lock, _cache = threading.Lock(), {}
_locks = collections.defaultdict(threading.Lock)
_mem = collections.deque(maxlen=24 * 60 * 4)            # (time, GB used here, {peer: [used, total]} or None) every 15 s: a day


# ---------------------------------------------------------------- colors that follow the project
def colors(names=()):
    """{project: slot 1..7, or 0 for "other"}; a new project takes the next free slot and keeps it while it is seen;
    when every slot is taken, the slot of the project not seen for longest (a week or more) is passed on."""
    with _lock:
        try: d = json.load(open(COLORS))
        except (OSError, ValueError): d = {}
        if 'slots' not in d: d = dict(slots={k: v for k, v in d.items() if isinstance(v, int)}, seen={})
        slots, seen, now, changed = d['slots'], d.setdefault('seen', {}), time.time(), False
        for old in [p for p in slots if names_mod.project(p) != p]:    # a name the person renamed gives up its slot
            slots.pop(old, None); seen.pop(old, None); changed = True
        for n in names:
            if not n: continue
            if seen.get(n, 0) < now - 3600: seen[n] = now; changed = True
            if n in slots: continue
            used = set(slots.values())
            free = next((i for i in range(1, SLOTS + 1) if i not in used), None)
            if free is None:
                old = min((p for p in slots if slots[p]), key=lambda p: seen.get(p, 0), default=None)
                if old and seen.get(old, 0) < now - 7 * 86400: free = slots.pop(old)
            slots[n] = free or 0; changed = True
        if changed:
            try: os.makedirs(llmshare.SHARED, exist_ok=True); llmshare._write(COLORS, d)
            except OSError: pass
        return {n: slots.get(n, 0) for n in names if n}               # only the names on screen, never the whole history


def _cached(key, seconds, fn):
    """fn()'s value for `seconds`; while one thread computes it, the others wait for that value instead of computing too."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds: return hit[1]
    with _locks[key]:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < seconds: return hit[1]
        val = fn()
        _cache[key] = (time.time(), val)
        return val


# ---------------------------------------------------------------- now
def about(home=None):
    """The dashboard's corner: {version, license, copyright, copyright_url, source}, read from the hub's own LICENSE and
    plugin manifest (install copies both beside the code), so no name or address is written into the code itself. The
    copyright line links to the manifest author's https address."""
    home = home or HOME
    lic = cr = src = site = None
    try:
        lines = [l.strip() for l in open(os.path.join(home, 'LICENSE')) if l.strip()]
        lic = lines[0] if lines else None
        cr = next((re.sub(r'^Copyright\s*(\(c\)|©)?\s*', '© ', l, flags=re.I) for l in lines if l.lower().startswith('copyright')), None)
    except OSError: pass
    try:
        m = json.load(open(os.path.join(home, '.claude-plugin', 'plugin.json')))
        src = m.get('repository') or m.get('homepage')
        site = m['author'].get('url') if isinstance(m.get('author'), dict) else None
    except (OSError, ValueError): pass
    https = lambda u: u if isinstance(u, str) and u.startswith('https://') else None
    return dict(name=NAME, version=VERSION, license=lic, copyright=cr, copyright_url=https(site), source=https(src))


def snapshot():
    return _cached('snapshot', 2, _snapshot)


def _snapshot():
    lm, users, cs = dash.lmstudio(), dash.users(), dash.claude()
    holders = collections.defaultdict(collections.Counter)          # copy -> {project: answering}
    for f in llmshare.inflight():
        if f.get('state') == 'answering': holders[f.get('copy') or f.get('model')][names.project(f.get('project'))] += 1
    try: view = _cached('recycle-view', 10, lambda: recycle.view(lm['models']))
    except Exception as e: view = dict(copies={}, job=None, last={}); errors.record('snapshot: recycle view', e)
    job = view.get('job')
    fams = []
    for f in lm['families']:
        cps, downs = [], crashes.down_copies(f['id'])
        for c in f['copies']:
            hold = [dict(project=p, n=n) for p, n in holders.get(c['id'], collections.Counter()).most_common()]
            other = max(0, c.get('busy', 0) - sum(h['n'] for h in hold))         # held by a program that went straight in
            if other: hold.append(dict(project='direct to LM Studio', n=other))
            cps.append(dict(id=c['id'], state=c['state'], parallel=c['parallel'], busy=c.get('busy', 0), context=c['context'],
                            size=c.get('size'), holders=hold, served=view['copies'].get(c['id']),
                            recycling=job['state'] if job and job.get('copy') == c['id'] else None, down=c['id'] in downs))
        fams.append(dict(id=f['id'], at_once=f['at_once'], busy=f['busy'], size=round(f['size'], 1), copies=cps))
    n, secs, refused = _cached('waits7', 30, lambda: llmshare.waits(7))
    out = dict(time=time.time(), clock=time.strftime('%H:%M:%S'),
               door=dict(up=True, port=llmshare.HUB_PORT, queue=llmshare.waiting_here()),
               lmstudio=dict(server=lm['server'], families=fams, listing_ok=llmshare.read_ok(), listing_age=round(llmshare.listing_age())),
               settings_error=llmshare.settings_error(),
               crashes=dict(down=crashes.down(), recent=_cached('crash-history', 15, lambda: crashes.history(7))),
               recycle=dict(job=job, words=recycle.words(job) if job else None, recent=_cached('recycles', 15, lambda: recycle.recent(7)[:10])),
               users=[{k: v for k, v in u.items() if k not in ('host_session',)} for u in users],
               claude=[dict(c, uses=any(u.get('session') == c['id'] for u in users)) for c in cs],
               mac=dash.mac(), top=dash.every('top', 5, dash.top), waits7=dict(waits=n, minutes=round(secs / 60, 1), refused=refused))
    out['about'] = _cached('about', 3600, about)
    try: out['maintenance'] = maint.view()
    except Exception as e: out['maintenance'] = None; errors.record('snapshot: maintenance', e)
    try:                                          # this Mac's peers (each one's state, the link, the families it serves) and this Mac as a peer
        out['peers'] = peers.view()
        for p in out['peers']:
            for f in (p.get('families') or {}).values(): f['holders'] = {names.project(k): v for k, v in (f.get('holders') or {}).items()}
        out['link'] = dict(me=peers.me(), serving=peers.serving(), listening=peers.LISTEN.get('words'), problems=peers.problems())
    except Exception as e: out['peers'], out['link'] = [], dict(problems=[f'could not read the peers: {type(e).__name__}: {e}']); errors.record('snapshot: peers', e)
    out['pool'] = dash.pool(out['mac'], out['peers'])                    # memory across this Mac and its peers
    out['errors'] = errors.summary(10)                                   # anything the error record holds from the last 10 minutes
    if out['errors'] and names.presenting(): out['errors'] = {k: v for k, v in out['errors'].items() if k != 'last'}   # (screenshot mode: a count only)
    try: out['soc'] = dash.every('soc', 1.5, dash.soc)                  # the chip at work: per-core CPU load, GPU utilization
    except Exception as e: out['soc'] = None; errors.record('snapshot: soc', e)
    out['colors'] = colors([u['project'] for u in users] + [h['project'] for f in fams for c in f['copies'] for h in c['holders'] if h['project'] != 'direct to LM Studio']
                           + [k for p in out['peers'] for f in (p.get('families') or {}).values() for k in f['holders']])
    return out


# ---------------------------------------------------------------- over time
def history(minutes=60):
    try: minutes = int(float(minutes or 60))
    except (TypeError, ValueError): minutes = 60
    minutes = max(5, min(minutes, 24 * 60))
    return _cached(f'history-{minutes}', 5, lambda: _history(minutes))


def _history(minutes):
    now = time.time()
    step = 10 if minutes <= 60 else 30 if minutes <= 180 else 60 if minutes <= 360 else 240
    t0 = now - minutes * 60
    t0 -= t0 % step
    n = int((now - t0) // step) + 1
    rows = llmshare.requests_log(minutes / 60 + 0.5)
    answering, waiting, tokens = collections.defaultdict(lambda: [0.0] * n), [0.0] * n, [0.0] * n

    def spread(a, b, add):
        """add(bucket, share of the bucket covered) for the interval [a, b]."""
        a, b = max(a, t0), min(b, t0 + n * step)
        if b <= a: return
        i = int((a - t0) // step)
        while i < n and t0 + i * step < b:
            lo, hi = t0 + i * step, t0 + (i + 1) * step
            add(i, (min(b, hi) - max(a, lo)) / step); i += 1
    for r in rows:
        end, dur, wait = r.get('at', 0), r.get('seconds') or 0, r.get('waited') or 0
        start = end - dur
        p = names.project(r.get('project'))
        spread(start, start + wait, lambda i, s: waiting.__setitem__(i, waiting[i] + s))
        spread(start + wait, end, lambda i, s, p=p: answering[p].__setitem__(i, answering[p][i] + s))
        work = max(0.001, end - start - wait)
        toks = r.get('completion_tokens') or 0
        if toks: spread(start + wait, end, lambda i, s, k=toks / work: tokens.__setitem__(i, tokens[i] + s * k))   # tokens a second, averaged over the bucket
    live = 0
    for f in llmshare.inflight():                       # the requests in flight now: the chart's right edge is the present, not the last finished request
        p, since = names.project(f.get('project')), f.get('since') or now
        if f.get('state') == 'answering':
            a0 = f.get('answering_since') or since
            spread(since, a0, lambda i, s: waiting.__setitem__(i, waiting[i] + s))
            spread(a0, now, lambda i, s, p=p: answering[p].__setitem__(i, answering[p][i] + s)); live += 1
        else: spread(since, now, lambda i, s: waiting.__setitem__(i, waiting[i] + s))
    # requests by project, in coarser bars
    bar = max(step, 60 if minutes <= 60 else 300 if minutes <= 360 else 1800)
    b0 = now - minutes * 60; b0 -= b0 % bar
    nb = int((now - b0) // bar) + 1
    req = collections.defaultdict(lambda: [0] * nb)
    for r in rows:
        i = int((r.get('at', 0) - b0) // bar)
        if 0 <= i < nb: req[names.project(r.get('project'))][i] += 1
    mem = [x for x in list(_mem) if x[0] >= now - minutes * 60]
    there = sorted({k for x in mem if x[2] for k in x[2]})                     # peers sampled in the window, stacked over this Mac
    local = round(dash.mac()['total'])
    rnd = lambda xs: [round(x, 2) for x in xs]
    shown = sorted(set(answering) | set(req))
    return dict(start=t0, step=step, n=n, now=now, answering={p: rnd(v) for p, v in answering.items()}, waiting=rnd(waiting),
                tokens=rnd(tokens), at_once=llmshare.at_once(), live=live,
                requests=dict(start=b0, step=bar, n=nb, by=dict(req)),
                memory=dict(t=[round(x[0]) for x in mem], used=[round(x[1], 1) for x in mem], total=local, name=peers.me(),
                            hosts={k: [((x[2] or {}).get(k) or [None])[0] for x in mem] for k in there},
                            cap=[round(local + sum(v[1] for v in (x[2] or {}).values())) for x in mem]),
                colors=colors(shown))


def recent(n=25):
    try: n = max(1, min(int(float(n or 25)), 200))
    except (TypeError, ValueError): n = 25
    rows = llmshare.requests_log(24)[-n:]
    out = []
    for raw in reversed(rows):
        r = names.request(raw)                                   # the project and the step as the person named them
        out.append(dict(at=r.get('at'), project=r.get('project'), kind=r.get('kind'), model=r.get('model'), copy=r.get('copy'),
                        seconds=r.get('seconds'), waited=r.get('waited'), tokens=(r.get('prompt_tokens') or 0) + (r.get('completion_tokens') or 0),
                        out_tokens=r.get('completion_tokens') or 0, status=r.get('status'), step=r.get('step'), note=r.get('note'), tries=r.get('tries'),
                        title=None if names.private(raw.get('project')) else sessions.title_of(r.get('session'), r.get('host_session'))))
    return out


# ---------------------------------------------------------------- the memory sample (the door runs it)
def _mem_row(line):
    """A line of memory.jsonl -> (time, GB used here, {peer: [used, total]} or None), or None. Lines written before peers
    report memory hold [time, GB used here]."""
    try:
        row = json.loads(line)
        return float(row[0]), float(row[1]), (row[2] if len(row) > 2 and isinstance(row[2], dict) else None)
    except (ValueError, TypeError, IndexError, KeyError): return None


def sampler():
    """Memory every 15 s: in memory for the chart, and on disk (a day of it) so a restart of the door keeps the picture."""
    day = time.time() - 86400
    try:
        for line in open(MEMORY):
            x = _mem_row(line)
            if x and x[0] >= day: _mem.append(x)
    except OSError: pass
    written = 0
    while True:
        try:
            t, pl = time.time(), dash.pool()
            u, o = pl['hosts'][0]['used'], {h['name']: [h['used'], h['total']] for h in pl['hosts'][1:]} or None
            _mem.append((t, u, o))
            with open(MEMORY, 'a') as f: f.write(json.dumps([round(t), round(u, 1)] + ([o] if o else [])) + '\n')
            written += 1
            if written % 240 == 0 and os.path.getsize(MEMORY) > 400_000:      # an hour of samples: keep only the last day
                keep = [json.dumps([round(a), round(b, 1)] + ([o] if o else [])) for a, b, o in list(_mem)]
                tmp = MEMORY + '.tmp'; open(tmp, 'w').write('\n'.join(keep) + '\n'); os.replace(tmp, MEMORY)
        except Exception as e: errors.record('memory sampler', e)
        time.sleep(15)


def page():
    try: return open(PAGE, 'rb').read().replace(b'__HUB_NAME__', NAME.encode())     # one name, from the package
    except OSError: return b'<p>dash.html is missing from the installed code (localmodels install)</p>'
