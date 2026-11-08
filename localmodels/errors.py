"""errors - what went wrong inside the hub, kept for whoever fixes it: the gateway's endpoints and request handler, its
background loops (instance manager, peer poller, link listener, memory sampler) and the dashboard's own render errors
(POST /hub/client-error). One recorder for every caught exception, so nothing fails silently.

Each record: time, source (gateway or dashboard), where, the exception's type and message (truncated) and a short
traceback of code locations. Never a prompt, a completion or a file's contents. The same error from the same place is
recorded at most once a minute (a loop that fails every second does not flood the file); repeats are counted.

errors.jsonl in the hub's state folder rolls over past 1 MB. `localmodels errors` and GET /hub/errors read it; the
dashboard shows a line when something was recorded in the last 10 minutes. Recording costs nothing until something fails.
"""
import json, os, threading, time, traceback

from . import llmshare

FILE = os.path.join(llmshare.SHARED, 'errors.jsonl')
ROLL_AT = 1_000_000
QUIET = 60                    # seconds: the same error from the same place is recorded once in this window
_lock, _last = threading.Lock(), {}


def record(where, exc=None, words=None, trace=None, source='gateway'):
    """Record a caught exception (exc) or a reported error (words, trace). -> the record, or None when it was a repeat."""
    what = (f'{type(exc).__name__}: {exc}' if exc is not None else str(words or 'error'))[:300]
    key, now = (source, str(where)[:80], what), time.time()
    with _lock:
        seen = _last.get(key)
        if seen and now - seen[0] < QUIET:
            seen[1] += 1
            return None
        repeats = seen[1] if seen else 0
        _last[key] = [now, 0]
    if trace is None and exc is not None:
        trace = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8))
    rec = dict(at=round(now, 1), source=source, where=str(where)[:80], error=what, trace=str(trace or '')[-2000:],
               **({'repeats': repeats} if repeats else {}))
    try: llmshare._append(FILE, rec, roll_at=ROLL_AT)
    except OSError: pass
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} error ({source}, {rec['where']}): {what}", flush=True)
    return rec


def recent(minutes=1440, n=200):
    """The records of the last minutes, newest first (from the file, so any process can read them)."""
    out, since = [], time.time() - float(minutes) * 60
    for path in (FILE + '.1', FILE):
        try:
            for line in open(path):
                try: r = json.loads(line)
                except ValueError: continue
                if isinstance(r, dict) and r.get('at', 0) >= since: out.append(r)
        except OSError: pass
    return list(reversed(out))[:n]


_summary = {'key': None, 'at': 0.0, 'value': None}


def summary(minutes=10):
    """For the dashboard: {count, minutes, last: {at, source, where, error}} over the last minutes, or None. Read again only
    when the file changed or 30 s passed, so a snapshot every two seconds costs a stat, not a read."""
    try: st = os.stat(FILE); key = (st.st_mtime, st.st_size, minutes)
    except OSError: key = (None, None, minutes)
    if key == _summary['key'] and time.time() - _summary['at'] < 30: return _summary['value']
    rs = recent(minutes, 500)
    value = dict(count=len(rs), minutes=minutes, last={k: rs[0].get(k) for k in ('at', 'source', 'where', 'error')}) if rs else None
    _summary.update(key=key, at=time.time(), value=value)
    return value
