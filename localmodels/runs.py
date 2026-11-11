"""runs - a whole run's progress and time left, for the dash, read from the hub's own request records.

A program says what its run is with three optional headers on each request (any language, no library needed):
  X-Run: <name>        the run: the same on every request of it
  X-Run-Total: <n>     units of work in the whole run (it may rise as the program learns more)
  X-Run-Done: <n>      units finished so far, as the program counts them
The program counts, because only it knows what a unit is: an item may take several requests, a retry, an agent's
back-and-forth. A request without them whose X-Step reads "<phase> N of M" still shows that phase's progress, counting
the items whose request was answered.

A run in phases (run -> phase -> item) says which phase each request is for, and may declare its plan once:
  X-Phase: <name>      the phase this request is for (with X-Run)
  X-Phase-Total: <n>   items in this phase;  X-Phase-Done: <n>  items of it finished so far
  POST /hub/runs       {"run": name, "phases": [{"name", "items", "approx"}...]}: the phases to come, in order, with
                       their sizes; sent again, it updates phases by name and appends new ones; {"end": true} closes it.
The estimate is in tokens, converted to time by the throughput the run is getting: a phase's time left is its items
left x its tokens per item / the run's tokens per second over the last 15 minutes. Tokens per item comes from the phase
itself once it has finished a few items ("measured"), else from the same phase in an earlier run on this Mac ("an
earlier run"), else from this run's other phases ("this run's other phases"). The throughput is measured, so the
estimate follows the load: another project taking half the slots doubles it, a peer joining shortens it.

Time left is measured, never assumed: the pace over the last 15 minutes or the last 20 units, whichever reaches further
back, so it follows a run that speeds up or slows down. Before this run has finished 3 units, the pace of the last run of
the same name on this Mac; with neither, it says it is estimating. Each Mac's estimate is its own.
"""
import glob, json, os, re, time

from . import llmshare

GAP = 20 * 60        # a pause this long between a run's requests starts a new run of the same name
ACTIVE = 15 * 60     # a run with no request for this long is no longer shown as running
WINDOW, UNITS, FEW = 15 * 60, 20, 3
STEP = re.compile(r'^(.*\S)\s+(\d+)\s+of\s+(\d+)\s*$')
PLANS = os.path.join(llmshare.SHARED, 'runs')   # one plan per project and run (POST /hub/runs)
KEEP = 2 * 86400     # a plan not updated for this long is no longer read
MAX_PHASES = 100


def parse_step(step):
    """'extract files 8 of 360' -> ('extract files', 8, 360); None when the step does not say where it is."""
    m = STEP.match(str(step or ''))
    if not m or int(m.group(3)) <= 0: return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def _count(v):
    try: n = int(str(v).strip())
    except (TypeError, ValueError): return None
    return n if 0 <= n <= 10**9 else None


def from_headers(h):
    """The run a request says it belongs to: {run, run_total, run_done}, holding only what the headers say well."""
    name = llmshare.clean(h.get('X-Run'), 80)
    if not name: return {}
    out = dict(run=name)
    for k, field in (('X-Run-Total', 'run_total'), ('X-Run-Done', 'run_done')):
        n = _count(h.get(k))
        if n is not None: out[field] = n
    phase = llmshare.clean(h.get('X-Phase'), 60)
    if phase:
        out['phase'] = phase
        for k, field in (('X-Phase-Total', 'phase_total'), ('X-Phase-Done', 'phase_done')):
            n = _count(h.get(k))
            if n is not None: out[field] = n
    return out


# ---------------------------------------------------------------- run plans
def _plan_path(project, run): return os.path.join(PLANS, f'{llmshare._safe(project)}--{llmshare._safe(run)}.json')


def _save(plan):
    os.makedirs(PLANS, exist_ok=True)
    llmshare._write(_plan_path(plan['project'], plan['run']), plan)


def declare(project, run, phases=None, end=False, now=None):
    """Write or update a run's plan: phases matched by name (items, approx, done updated; an items of None forgets the
    size), new ones appended in the order given. end closes the run. A run declared again after it ended starts afresh.
    -> the plan. ValueError, with the reason, for a body the hub cannot take."""
    now = now or time.time()
    project, run = llmshare.clean(project, 80), llmshare.clean(run, 80)
    if not run: raise ValueError('a plan needs "run": the name the requests send as X-Run')
    if not project: raise ValueError('a plan needs a project (X-Project, or "project" in the body)')
    if phases is not None and not isinstance(phases, list): raise ValueError('"phases" must be a list of {"name", "items", "approx"}')
    try:
        old = json.load(open(_plan_path(project, run)))
        old = old if isinstance(old, dict) and old.get('run') == run and isinstance(old.get('phases'), list) else None
    except (OSError, ValueError): old = None
    if old and old.get('ended') and not end: old = None
    plan = old or dict(v=1, project=project, run=run, since=now, phases=[])
    have = {p.get('name'): p for p in plan['phases'] if isinstance(p, dict)}
    for p in phases or []:
        if not isinstance(p, dict): raise ValueError('each phase must be an object: {"name", "items", "approx"}')
        name = llmshare.clean(p.get('name'), 60)
        if not name: raise ValueError('each phase needs a "name": the X-Phase its requests send')
        cur = have.get(name)
        if cur is None: cur = have[name] = dict(name=name); plan['phases'].append(cur)
        for k in ('items', 'done'):
            if k not in p: continue
            if p[k] is None: cur.pop(k, None); continue
            n = _count(p[k])
            if n is None: raise ValueError(f'phase {name}: "{k}" must be a whole number, not {p[k]!r}')
            cur[k] = n
        if 'approx' in p: cur['approx'] = bool(p['approx'])
    if len(plan['phases']) > MAX_PHASES: raise ValueError(f'at most {MAX_PHASES} phases')
    plan.update(updated=now, ended=now if end else None)
    _save(plan)
    for f in glob.glob(os.path.join(PLANS, '*.json')):     # plans untouched for 30 days go (they hold names and sizes only)
        try:
            if now - os.path.getmtime(f) > 30 * 86400: os.remove(f)
        except OSError: pass
    return plan


def plans(now=None):
    """The plans in use: updated within KEEP, and not ended more than ACTIVE ago. A file of the wrong shape is skipped."""
    now = now or time.time()
    out = []
    for f in sorted(glob.glob(os.path.join(PLANS, '*.json'))):
        try: p = json.load(open(f))
        except (OSError, ValueError): continue
        if not isinstance(p, dict) or not p.get('run') or not p.get('project') or not isinstance(p.get('phases'), list): continue
        if now - (p.get('updated') or 0) > KEEP or (p.get('ended') and now - p['ended'] > ACTIVE): continue
        p['phases'] = [x for x in p['phases'] if isinstance(x, dict) and x.get('name')]
        out.append(p)
    return out


def _events(rows, inflight=()):
    """{(project, name, kind): [event]} where an event is one request: start, end (None while in flight), and what it
    said. kind is 'run' (X-Run) or 'phase' (step words alone): a run and a phase of the same name are counted apart."""
    out = {}
    for r, live in [(r, False) for r in rows] + [(f, True) for f in inflight]:
        project = r.get('project') or '?'
        start = r.get('since') if live else (r.get('at') or 0) - (r.get('seconds') or 0)
        ev = dict(start=start or 0, end=None if live else r.get('at') or 0, ok=not live and r.get('status') == 200, step=r.get('step'),
                  tokens=0 if live else (r.get('prompt_tokens') or 0) + (r.get('completion_tokens') or 0))
        if r.get('run'):
            out.setdefault((project, r['run'], 'run'), []).append(dict(ev, kind='run', done=r.get('run_done'), total=r.get('run_total')))
        else:
            p = parse_step(r.get('step'))
            if p: out.setdefault((project, p[0], 'phase'), []).append(dict(ev, kind='phase', n=p[1], total=p[2]))
    return out


def _episodes(evs):
    """A key's events split into runs: a pause of GAP between requests starts a new one, and so does a named run whose
    count drops below half of where it had got to (the same run started again; requests sent side by side arrive a
    few units out of order, never half)."""
    eps, last_end, best = [], None, 0
    for e in sorted(evs, key=lambda e: e['start']):
        again = e.get('done') is not None and best >= 4 and e['done'] < best / 2
        if not eps or again or (last_end is not None and e['start'] - last_end > GAP): eps.append([]); best = 0
        if e.get('done') is not None: best = max(best, e['done'])
        eps[-1].append(e)
        last_end = max(last_end or 0, e['end'] or e['start'])
    return eps


def _points(ep):
    """[(time, units done by then)] rising, and the units done at the start of this run."""
    if ep[0]['kind'] == 'run':
        pts, best = [], None
        for e in sorted((e for e in ep if e.get('done') is not None), key=lambda e: e['start']):
            best = e['done'] if best is None else max(best, e['done'])
            pts.append((e['start'], best))
        return pts, (pts[0][1] if pts else 0)
    seen, pts = set(), []
    for e in sorted((e for e in ep if e['ok']), key=lambda e: e['end']):
        if e['n'] not in seen: seen.add(e['n']); pts.append((e['end'], len(seen)))
    return pts, 0


def _base(pts, first, started, now):
    """Where "lately" begins: (time, units done then), the last WINDOW seconds or the last UNITS units, whichever reaches
    further back; the run's start when it is younger than both."""
    done = pts[-1][1]
    picks = [x[-1] for x in ([p for p in pts if p[0] <= now - WINDOW], [p for p in pts if p[1] <= done - UNITS]) if x]
    return min(picks) if picks else (started, first)


def _pace(pts, first, started, now):
    """Seconds per unit lately: over the last WINDOW seconds or the last UNITS units, whichever reaches further back."""
    if not pts: return None
    base = _base(pts, first, started, now)
    units, secs = pts[-1][1] - base[1], now - base[0]
    return secs / units if units > 0 and secs > 0 else None


def history(rows):
    """{key: seconds per unit} from the last run of each name in these records that finished at least FEW units."""
    out = {}
    for key, evs in _events(rows).items():
        for ep in _episodes(evs):
            pts, first = _points(ep)
            if not pts or pts[-1][1] - first < FEW: continue
            start, end = min(e['start'] for e in ep), max(e['end'] or e['start'] for e in ep)
            out[key] = (end - start) / (pts[-1][1] - first)
    return out


def _job(ep, now):
    """The job a named run is on now: the phase its latest step names ("check 10 of 50"), counted the way a phase from step
    words is (items answered), with its own pace, time left and finish. None when its steps do not say where they are."""
    steps = [(e, parse_step(e.get('step'))) for e in ep]
    steps = [(e, p) for e, p in steps if p]
    if not steps: return None
    name = max(steps, key=lambda x: x[0]['start'])[1][0]
    evs = [dict(e, kind='phase', n=p[1], total=p[2]) for e, p in steps if p[0] == name]
    total = max(evs, key=lambda e: e['start'])['total']
    pts, _ = _points(evs)
    done = pts[-1][1] if pts else 0
    started = min(e['start'] for e in evs)
    pace = _pace(pts, 0, started, now)
    left = 0 if done >= total else int((total - done) * pace) if pace else None
    return dict(name=name, done=done, total=max(total, done), pct=min(100, int(100 * done / total)), left=left,
                finish=now + left if left is not None else None, started=started)


# ---------------------------------------------------------------- runs in phases: an estimate in tokens
BASES = ('measured', 'this run earlier', 'an earlier run', "this run's other phases")   # most certain first


def _phase_events(rows, inflight=()):
    """{(project, run): {phase: [event]}} for the requests that name a phase, each event with its done and total."""
    out = {}
    for r, live in [(r, False) for r in rows] + [(f, True) for f in inflight]:
        if not r.get('run') or not r.get('phase'): continue
        start = r.get('since') if live else (r.get('at') or 0) - (r.get('seconds') or 0)
        ev = dict(start=start or 0, end=None if live else r.get('at') or 0, kind='run', done=r.get('phase_done'), total=r.get('phase_total'),
                  tokens=0 if live else (r.get('prompt_tokens') or 0) + (r.get('completion_tokens') or 0))
        out.setdefault((r.get('project') or '?', r['run']), {}).setdefault(r['phase'], []).append(ev)
    return out


def _cost(ep, now, recent=True):
    """Tokens per item in an episode of one phase: the tokens of the requests that ended by its last count, over the items
    counted since the base (lately when recent, else the whole episode). None before FEW items."""
    pts, first = _points(ep)
    if not pts: return None
    started = min(e['start'] for e in ep)
    base = _base(pts, first, started, now) if recent else (started, first)
    items = pts[-1][1] - base[1]
    tokens = sum(e['tokens'] for e in ep if e['end'] and base[0] < e['end'] <= pts[-1][0] + 1)
    return tokens / items if items >= FEW and tokens > 0 else None


def costs(rows):
    """{(project, phase): tokens per item} and {(project, run, phase): ...} from the last episode of each that finished at
    least FEW items, for the estimate of a phase before it has measured itself."""
    out = {}
    for (project, run), phases in _phase_events(rows).items():
        for phase, evs in phases.items():
            for ep in _episodes(evs):
                c = _cost(ep, 0, recent=False)
                if not c: continue
                end = max(e['end'] or e['start'] for e in ep)
                for key in ((project, phase), (project, run, phase)):
                    if key not in out or out[key][1] <= end: out[key] = (c, end)
    return {k: v[0] for k, v in out.items()}


def _throughput(evs, now):
    """The run's tokens per second lately: what its requests that ended in the last WINDOW seconds were given, over that
    window (or since the run began, when it is younger). None with nothing ended lately."""
    if not evs: return None
    since = max(now - WINDOW, min(e['start'] for e in evs))
    tokens = sum(e['tokens'] for e in evs if e['end'] and e['end'] > now - WINDOW)
    return tokens / (now - since) if tokens > 0 and now - since >= 60 else None


def phased(project, run, plan, phases_evs, now, costs=None):
    """A run in phases: {name, project, kind 'run', scope 'whole run', done, total, pct, left, finish, basis, started,
    phases: [{name, state ('done', 'running', 'waiting'), done, total, approx, pct, left, finish, basis, started, ended,
    tokens_per_item}], job: the phase at work}. Time left is None until every phase still to do has an estimate;
    at_least then holds what is known."""
    costs = costs or {}
    order = [p['name'] for p in (plan or {}).get('phases', [])]
    order += sorted((n for n in phases_evs if n not in order), key=lambda n: min(e['start'] for e in phases_evs[n]))
    declared = {p['name']: p for p in (plan or {}).get('phases', [])}
    last = {n: _episodes(evs)[-1] for n, evs in phases_evs.items()}
    rate = _throughput([e for ep in last.values() for e in ep], now)
    began = [order.index(n) for n in last]
    out = []
    for i, name in enumerate(order):
        ep, d = last.get(name), declared.get(name, {})
        pts, _ = _points(ep) if ep else ([], 0)
        done = max(pts[-1][1] if pts else 0, d.get('done') or 0)
        total = next((e['total'] for e in sorted(ep or [], key=lambda e: -e['start']) if e.get('total')), None) or d.get('items')
        live = bool(ep) and any(e['end'] is None for e in ep)
        later = any(j > i for j in began)
        if total is not None and done >= total and not live or (not live and later): state = 'done'
        elif ep and (live or now - max(e['end'] or e['start'] for e in ep) <= ACTIVE): state = 'running'
        else: state = 'waiting'
        if state == 'done' and total is not None: done = total
        total = max(total, done) if total is not None else None
        tpi, basis = (_cost(ep, now) if ep else None), None
        if tpi: basis = 'measured'
        elif (project, run, name) in costs: tpi, basis = costs[(project, run, name)], 'this run earlier'
        elif (project, name) in costs: tpi, basis = costs[(project, name)], 'an earlier run'
        out.append(dict(name=name, state=state, done=done, total=total, approx=bool(d.get('approx')), tpi=tpi, basis=basis,
                        started=min(e['start'] for e in ep) if ep else None,
                        ended=max(e['end'] or e['start'] for e in ep) if ep and state == 'done' else None))
    known = [(p['tpi'], p['done']) for p in out if p['tpi'] and p['basis'] in ('measured', 'this run earlier')]
    mean = sum(c * max(1, n) for c, n in known) / sum(max(1, n) for c, n in known) if known else None
    acc, sure, at_least, worst = 0.0, True, 0.0, 0
    for p in out:
        if p['state'] != 'done' and not p['tpi'] and mean: p['tpi'], p['basis'] = mean, "this run's other phases"
        if p['state'] == 'done': p['left'] = 0
        elif p['total'] is not None and p['tpi'] and rate: p['left'] = int((p['total'] - p['done']) * p['tpi'] / rate)
        else: p['left'] = None
        if p['state'] != 'done':
            if p['left'] is None: sure = False
            else: acc += p['left']; at_least += p['left']
            if p['basis'] in BASES: worst = max(worst, BASES.index(p['basis']))
        p['finish'] = now + acc if p['state'] != 'done' and sure and p['left'] is not None else None
        p['pct'] = min(100, int(100 * p['done'] / p['total'])) if p['total'] else None
        tpi = p.pop('tpi', None); p['tokens_per_item'] = int(tpi) if tpi else None
    total = sum(p['total'] for p in out if p['total'] is not None) or None
    done = sum(p['done'] for p in out)
    todo = [p for p in out if p['state'] != 'done']
    left = int(acc) if sure and todo else 0 if not todo else None
    at_work = [p for p in out if p['state'] == 'running'] or todo[:1]
    job = at_work[-1] if at_work else None
    starts = [p['started'] for p in out if p['started']] + ([plan['since']] if plan and plan.get('since') else [])
    return dict(name=run, project=project, kind='run', scope='whole run', phase=job['name'] if job else None, done=done, total=total,
                pct=min(100, int(100 * done / total)) if total else None, left=left, finish=now + left if left is not None else None,
                at_least=int(at_least) if left is None and at_least else None, basis=BASES[worst] if todo and any(p['basis'] for p in todo) else None,
                started=min(starts) if starts else now, phases=out,
                job=dict(name=job['name'], done=job['done'], total=job['total'], pct=job['pct'], left=job['left'], finish=job['finish'],
                         started=job['started'] or now) if job and job['total'] else None)


def progress(rows, inflight=(), now=None, history=None, plans=None, costs=None):
    """{(project, name): {name, project, kind ('run' or 'phase'), phase, done, total, pct, left, basis, started}} for each
    run going on now, with finish (now plus left, a time) and scope: kind "run" (the program said it with X-Run) covers the
    whole run, kind "phase" (read from its step words) only that phase; a run going on hides a phase of the same name. A
    run also carries job: the phase it is on now, with its own done, total, pct, left, finish and started (_job). A run
    whose requests name their phase (X-Phase), or that declared a plan, is estimated phase by phase instead (phased).
    plans: runs.plans(); costs: runs.costs() over a week of records."""
    import time
    now = now or time.time()
    out = {}
    for key, evs in sorted(_events(rows, inflight).items(), key=lambda kv: kv[0][2] != 'run'):   # runs first
        if key[:2] in out: continue
        ep = _episodes(evs)[-1]
        live = any(e['end'] is None for e in ep)
        last = max(e['end'] or e['start'] for e in ep)
        if not live and now - last > ACTIVE: continue
        started = min(e['start'] for e in ep)
        latest = max(ep, key=lambda e: e['start'])
        total = next((e['total'] for e in sorted(ep, key=lambda e: -e['start']) if e.get('total')), None)
        pts, first = _points(ep)
        done = pts[-1][1] if pts else first
        if total is not None: total = max(total, done)
        step = latest.get('step')
        phase = (parse_step(step) or (step,))[0] if step else None
        left, basis = None, 'estimating after the first one'
        if total is not None:
            pace = _pace(pts, first, started, now)
            seed = (history or {}).get(key)
            if done - first >= FEW and pace: left, basis = (total - done) * pace, 'this run'
            elif seed: left, basis = (total - done) * seed, 'the last run'
            elif pace: left, basis = (total - done) * pace, 'this run so far'
        left = int(left) if left is not None else None
        out[key[:2]] = dict(name=key[1], project=key[0], kind=ep[0]['kind'], phase=phase if ep[0]['kind'] == 'run' else None, done=done,
                        total=total, pct=min(100, int(100 * done / total)) if total else None, left=left,
                        finish=now + left if left is not None else None, basis=basis if total else None, started=started,
                        scope='whole run' if ep[0]['kind'] == 'run' else 'this phase', job=_job(ep, now) if ep[0]['kind'] == 'run' else None)
    by_phase = _phase_events(rows, inflight)                  # a run in phases: its plan and its phases' own counts replace the above
    declared = {(p['project'], p['run']): p for p in plans or []}
    for key in set(by_phase) | set(declared):
        evs, plan = by_phase.get(key, {}), declared.get(key)
        flat = [e for es in evs.values() for e in es]
        live = any(e['end'] is None for e in flat)
        last = max([e['end'] or e['start'] for e in flat] + ([plan.get('updated') or 0] if plan else []))
        if not live and (now - last > ACTIVE or (plan and plan.get('ended'))): continue
        out[key] = phased(key[0], key[1], plan, evs, now, costs)
    return out
