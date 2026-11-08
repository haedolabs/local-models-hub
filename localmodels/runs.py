"""runs - a whole run's progress and time left, for the dash, read from the hub's own request records.

A program says what its run is with three optional headers on each request (any language, no library needed):
  X-Run: <name>        the run: the same on every request of it
  X-Run-Total: <n>     units of work in the whole run (it may rise as the program learns more)
  X-Run-Done: <n>      units finished so far, as the program counts them
The program counts, because only it knows what a unit is: an item may take several requests, a retry, an agent's
back-and-forth. A request without them whose X-Step reads "<phase> N of M" still shows that phase's progress, counting
the items whose request was answered.

Time left is measured, never assumed: the pace over the last 15 minutes or the last 20 units, whichever reaches further
back, so it follows a run that speeds up or slows down. Before this run has finished 3 units, the pace of the last run of
the same name on this Mac; with neither, it says it is estimating. Each Mac's estimate is its own.
"""
import re

from . import llmshare

GAP = 20 * 60        # a pause this long between a run's requests starts a new run of the same name
ACTIVE = 15 * 60     # a run with no request for this long is no longer shown as running
WINDOW, UNITS, FEW = 15 * 60, 20, 3
STEP = re.compile(r'^(.*\S)\s+(\d+)\s+of\s+(\d+)\s*$')


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
    return out


def _events(rows, inflight=()):
    """{(project, name): [event]} where an event is one request: start, end (None while in flight), and what it said."""
    out = {}
    for r, live in [(r, False) for r in rows] + [(f, True) for f in inflight]:
        project = r.get('project') or '?'
        start = r.get('since') if live else (r.get('at') or 0) - (r.get('seconds') or 0)
        ev = dict(start=start or 0, end=None if live else r.get('at') or 0, ok=not live and r.get('status') == 200, step=r.get('step'))
        if r.get('run'):
            out.setdefault((project, r['run']), []).append(dict(ev, kind='run', done=r.get('run_done'), total=r.get('run_total')))
        else:
            p = parse_step(r.get('step'))
            if p: out.setdefault((project, p[0]), []).append(dict(ev, kind='phase', n=p[1], total=p[2]))
    return out


def _episodes(evs):
    """A key's events split into runs: a pause of GAP between requests starts a new one."""
    eps, last_end = [], None
    for e in sorted(evs, key=lambda e: e['start']):
        if not eps or (last_end is not None and e['start'] - last_end > GAP): eps.append([])
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


def _pace(pts, first, started, now):
    """Seconds per unit lately: over the last WINDOW seconds or the last UNITS units, whichever reaches further back."""
    if not pts: return None
    done = pts[-1][1]
    base = (started, first)
    by_time = [p for p in pts if p[0] <= now - WINDOW]
    by_units = [p for p in pts if p[1] <= done - UNITS]
    picks = [x[-1] for x in (by_time, by_units) if x]
    if picks: base = min(picks)
    units, secs = done - base[1], now - base[0]
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


def progress(rows, inflight=(), now=None, history=None):
    """{(project, name): {name, project, kind ('run' or 'phase'), phase, done, total, pct, left, basis, started}} for each
    run going on now. kind 'run': the program said it with X-Run; 'phase': read from its step words."""
    import time
    now = now or time.time()
    out = {}
    for key, evs in _events(rows, inflight).items():
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
        out[key] = dict(name=key[1], project=key[0], kind=ep[0]['kind'], phase=phase if ep[0]['kind'] == 'run' else None, done=done,
                        total=total, pct=min(100, int(100 * done / total)) if total else None,
                        left=int(left) if left is not None else None, basis=basis if total else None, started=started)
    return out
