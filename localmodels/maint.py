"""maint - maintenance: the hub tells every program and every Claude Code session that the local models go down for a
while, then stands down until it is over.

  announced   a start time is set and not reached: everything works; every answer from the door carries the header
              X-Hub-Maintenance, the dash shows a banner, the status tool and /hub/health say it, and the Claude Code
              hook adds one line to each session (notice()).
  waiting     "after the batches": it starts by itself once no batch has sent a request for QUIET_FOR seconds (tick()).
  on          new requests get 503 with Retry-After to its expected end; requests already being answered finish; the
              door never starts LM Studio's server and the keeper loads, reloads and recycles nothing, so the hub does not
              fight the person working on LM Studio. The keeper notes what was loaded (freeze()).
  off         end() clears it; models.after_maintenance() loads again what was loaded before.
The record lives in maintenance.json in the hub's shared folder. It holds times, minutes and a few words of why; nothing
else.
"""
import json, os, re, time

from . import llmshare

FILE = os.path.join(llmshare.SHARED, 'maintenance.json')
QUIET_FOR = 120          # "after the batches": no batch request for this long, then it starts
_cache = {'mtime': None, 'rec': None}


def _write(rec):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = f'{FILE}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f: json.dump(rec, f)
    os.replace(tmp, FILE)
    _cache['mtime'] = None


def _raw():
    try: m = os.path.getmtime(FILE)
    except OSError: return None
    if m != _cache['mtime']:
        try: rec = json.load(open(FILE))
        except (OSError, ValueError): rec = None
        _cache.update(mtime=m, rec=rec if isinstance(rec, dict) else None)
    return _cache['rec']


def read(now=None):
    """The maintenance record with its phase ('waiting', 'announced' or 'on') and expected end, or None."""
    rec = _raw()
    if not rec: return None
    now = now or time.time()
    start = rec.get('start')
    phase = 'waiting' if start is None else 'announced' if now < start else 'on'
    return dict(rec, phase=phase, end=start + 60 * rec.get('minutes', 30) if start is not None else None)


def announce(start=None, minutes=30, why='', after_batches=False, now=None):
    """Set maintenance: at `start` (a time; now when None and not after the batches), for about `minutes`, for `why`.
    after_batches: no start yet; tick() starts it once the batches are quiet. -> the record as read()."""
    now = now or time.time()
    rec = dict(v=1, set_at=now, minutes=max(1, int(minutes)), why=llmshare.clean(why, 120) or '',
               start=None if after_batches else (start if start is not None else now), after_batches=bool(after_batches))
    old = _raw() or {}
    if old.get('before'): rec['before'] = old['before']
    _write(rec)
    return read(now=now)


def begin(now=None):
    """Start it now (`localmodels maintenance now`, or the batches went quiet). -> the record."""
    now = now or time.time()
    rec = _raw() or dict(v=1, set_at=now, minutes=30, why='', after_batches=False)
    if rec.get('start') is None or rec['start'] > now: rec = dict(rec, start=now); _write(rec)
    return read(now=now)


def freeze(families):
    """Note what was loaded when maintenance began (once), so after_maintenance can load it again."""
    rec = _raw()
    if rec and 'before' not in rec: _write(dict(rec, before=sorted(set(families))))


def end():
    """It is over. -> the record as it was (with `before`), or None when none was set."""
    rec = _raw()
    try: os.remove(FILE)
    except OSError: pass
    _cache['mtime'] = None
    return rec


def tick(inflight, rows, now=None):
    """'After the batches': start it when no batch request is in flight and none ended within QUIET_FOR seconds.
    inflight: llmshare.inflight(); rows: recent request records. -> whether it started now."""
    m = read(now=now)
    if not m or m['phase'] != 'waiting': return False
    now = now or time.time()
    if any(f.get('kind') == 'batch' for f in inflight): return False
    if any(r.get('kind') == 'batch' and now - (r.get('at') or 0) < QUIET_FOR for r in rows): return False
    begin(now=now)
    return True


def on(now=None):
    """The record while maintenance is on, else None."""
    m = read(now=now)
    return m if m and m['phase'] == 'on' else None


def retry_after(m, now=None):
    """Seconds a refused program should wait: to the expected end, or a minute when it runs over."""
    now = now or time.time()
    return max(60, int(m['end'] - now)) if m and m.get('end') else 60


def _clock(t): return time.strftime('%H:%M', time.localtime(t))


def _stretch(minutes): return f'{minutes} min' if minutes < 90 else f'{minutes / 60:.1f} h'.replace('.0 h', ' h')


def words(m, now=None):
    """One plain sentence for the dash, the status line and the tools."""
    if not m: return ''
    now = now or time.time()
    why = f": {m['why']}" if m.get('why') else ''
    if m['phase'] == 'waiting': return f"maintenance pending: starts once batch traffic drains, est. {_stretch(m['minutes'])}{why}"
    if m['phase'] == 'announced': return f"maintenance scheduled at {_clock(m['start'])} (in {_stretch(max(1, round((m['start'] - now) / 60)))}), est. {_stretch(m['minutes'])}{why}"
    if now > m['end']: return f"in maintenance since {_clock(m['start'])}, overrunning (scheduled end {_clock(m['end'])}){why}"
    return f"in maintenance until ~{_clock(m['end'])}{why}"


def header(m, now=None):
    """The X-Hub-Maintenance header: 'announced; starts=...; until=...; why=...' (ISO local times), or None."""
    if not m: return None
    iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(t))
    parts = ['after-batches' if m['phase'] == 'waiting' else m['phase']]
    if m.get('start') is not None and m['phase'] == 'announced': parts.append(f"starts={iso(m['start'])}")
    if m.get('end'): parts.append(f"until={iso(m['end'])}")
    parts.append(f"minutes={m['minutes']}")
    if m.get('why'): parts.append('why=' + re.sub(r'[;\r\n]+', ' ', m['why']))
    return '; '.join(parts)


def view(now=None):
    """What the screens show: {phase, start, end, minutes, why, words}, or None."""
    m = read(now=now)
    return dict(phase=m['phase'], start=m.get('start'), end=m.get('end'), minutes=m['minutes'], why=m.get('why'), words=words(m, now)) if m else None


def notice(now=None):
    """The line the Claude Code hook adds to a session, or '' when there is nothing to say."""
    m = read(now=now)
    if not m: return ''
    then = {'waiting': 'Do not start a new local-model batch; one running now may finish.',
            'announced': 'Finish or pause local-model work before it starts; do not start a batch that would run past it.',
            'on': 'Do not send requests to the local models until it is over.'}[m['phase']]
    return (f"Local models on this Mac (local-models hub): {words(m, now)}. {then} Requests during maintenance get 503 "
            f"with Retry-After (seconds); a batch should sleep that long and resume. `localmodels` shows the state.")


def notice_main():
    """`localmodels notice`: the hook's command. Prints the notice or nothing, never fails, never slows a prompt."""
    try:
        line = notice()
        if line: print(line)
    except Exception: pass
    return 0
