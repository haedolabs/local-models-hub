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

Switched off (`localmodels off`, the dashboard's "turn models off"): a standing state with no end, kept in off.json until
`localmodels on` or the dashboard's "turn models on". read() reports it as maintenance that is on (phase "on", off=True),
so every rule above applies: requests get 503, the gateway never starts LM Studio's server, the instance manager loads,
reloads and recycles nothing, and a front skips this Mac as a peer. In addition the instance manager unloads every
instance once no request is in flight (models.off_pass), once; a model a person loads by hand afterwards is left alone.
The Claude Code hook says nothing while switched off (one line on every prompt for days would be noise); a refused request
says why.

Peers only (`localmodels off --peers`, the dashboard's "peers only"): this Mac's local models switched off as above (off.json
with peers=True: nothing is loaded, reloaded or started here, every instance is unloaded once the requests in flight on it
finish), while the gateway keeps admitting requests and sends every one to its peers' slots; a request no peer can take
gets 503 with Retry-After. refusing() is what the gateway reads: maintenance that is on, or switched off without peers.
"""
import json, os, re, time

from . import llmshare

FILE = OFF = SWITCHING = None                            # maintenance.json, off.json, switching.json in the shared state (_state_paths)
SHOWN_FOR = 120                                          # seconds switching.json shows a switch-on's outcome after it ends


@llmshare.on_state
def _state_paths(root):
    global FILE, OFF, SWITCHING
    FILE, OFF, SWITCHING = (os.path.join(root, f) for f in ('maintenance.json', 'off.json', 'switching.json'))


QUIET_FOR = 120          # "after the batches": no batch request for this long, then it starts
OFF_RETRY = 300          # Retry-After while switched off: there is no scheduled end
_cache = {'mtime': None, 'rec': None}
_off_cache = {'mtime': None, 'rec': None}


def _write(rec, path=None, locked=False):
    path = path or FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if locked:
        llmshare._write(path, rec)
    else:
        with llmshare.flocked(path + '.lock'): llmshare._write(path, rec)
    _cache['mtime'] = _off_cache['mtime'] = None


def switched_off():
    """The off record ({at, why, before, unloaded, peers}) while the local models are switched off, else None."""
    try: m = os.path.getmtime(OFF)
    except OSError: return None
    if m != _off_cache['mtime']:
        try: rec = json.load(open(OFF))
        except (OSError, ValueError): rec = None
        _off_cache.update(mtime=m, rec=rec if isinstance(rec, dict) and rec.get('at') else None)
    return _off_cache['rec']


def switch_off(before, why='', now=None, peers=False):
    """Switch the local models off, or change its peers-only mode, as one transaction with off_done/switch_on."""
    with llmshare.flocked(OFF + '.lock'):
        rec = switched_off()
        if rec:
            if bool(rec.get('peers')) != bool(peers):
                rec = dict(rec, peers=bool(peers), mode_at=now or time.time())
                _write(rec, OFF, locked=True)
            return rec
        rec = dict(v=1, at=now or time.time(), why=llmshare.clean(why, 120) or '', before=sorted(set(before or [])), unloaded=None, peers=bool(peers))
        _write(rec, OFF, locked=True)
        return rec


def peers_only():
    """The off record while the local models are switched off and requests go to the peers (localmodels off --peers), else None."""
    rec = switched_off()
    return rec if rec and rec.get('peers') else None


def off_done(at, words=''):
    """The instance manager unloaded everything for the off record set at `at` (not a newer one, not after switch_on)."""
    with llmshare.flocked(OFF + '.lock'):
        rec = switched_off()
        if rec and rec.get('at') == at and not rec.get('unloaded'):
            _write(dict(rec, unloaded=time.time(), unload_words=str(words)[:300]), OFF, locked=True)


def switch_on():
    """Clear the off record under the same transaction lock as switch_off/off_done."""
    with llmshare.flocked(OFF + '.lock'):
        rec = switched_off()
        try: os.remove(OFF)
        except OSError: pass
        _off_cache['mtime'] = None
        return rec


def on_started(families, now=None):
    """A switch-on began loading `families` (models.switch_on): the screens show it under way until on_done."""
    _write(dict(v=1, at=now or time.time(), pid=os.getpid(), families=sorted(families or []), done=None, ok=None, words=''), SWITCHING)


def on_done(ok, words=''):
    with llmshare.flocked(SWITCHING + '.lock'):
        rec = _switching_raw()
        if rec: _write(dict(rec, done=time.time(), ok=bool(ok), words=str(words)[:300]), SWITCHING, locked=True)


def _switching_raw():
    try: rec = json.load(open(SWITCHING))
    except (OSError, ValueError): return None
    return rec if isinstance(rec, dict) and rec.get('at') else None


def switching(now=None):
    """The switch-on the screens show: {at, families, done, ok, words, phase}; phase 'loading' while it runs, 'done' for
    SHOWN_FOR seconds after it ends; None otherwise, and when the process that ran it died mid-way."""
    rec, now = _switching_raw(), now or time.time()
    if not rec: return None
    if rec.get('done'):
        # A successful notice is transient; a failed restore stays visible until a later attempt replaces it.
        return dict(rec, phase='done') if not rec.get('ok') or now - rec['done'] < SHOWN_FOR else None
    try: os.kill(int(rec.get('pid') or 0), 0); alive = bool(rec.get('pid'))
    except (OSError, ValueError): alive = False
    return dict(rec, phase='loading') if alive and now - rec['at'] < 1800 else None


def _raw():
    try: m = os.path.getmtime(FILE)
    except OSError: return None
    if m != _cache['mtime']:
        try: rec = json.load(open(FILE))
        except (OSError, ValueError): rec = None
        _cache.update(mtime=m, rec=rec if isinstance(rec, dict) else None)
    return _cache['rec']


def read(now=None):
    """The maintenance record with its phase ('waiting', 'announced' or 'on') and expected end, or None. Switched off
    reads as maintenance that is on, with no end (off=True)."""
    off = switched_off()
    if off: return dict(v=1, off=True, phase='on', set_at=off['at'], start=off['at'], end=None, minutes=0, why=off.get('why') or '',
                        unloaded=off.get('unloaded'), before=off.get('before') or [], peers=bool(off.get('peers')))
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
    with llmshare.flocked(FILE + '.lock'):
        old = _raw() or {}
        if old.get('before'): rec['before'] = old['before']
        _write(rec, locked=True)
        return read(now=now)


def begin(now=None):
    """Start it now (`localmodels maintenance now`, or the batches went quiet), without racing an operator ending it."""
    now = now or time.time()
    with llmshare.flocked(FILE + '.lock'):
        rec = _raw() or dict(v=1, set_at=now, minutes=30, why='', after_batches=False)
        if rec.get('start') is None or rec['start'] > now: rec = dict(rec, start=now); _write(rec, locked=True)
        return read(now=now)


def freeze(families):
    """Note what was loaded when maintenance began once, without recreating a record an operator just ended."""
    with llmshare.flocked(FILE + '.lock'):
        rec = _raw()
        if rec and 'before' not in rec: _write(dict(rec, before=sorted(set(families))), locked=True)


def end():
    """End under the freeze/begin transaction lock, so no paused keeper can recreate the window afterwards."""
    with llmshare.flocked(FILE + '.lock'):
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
    """The record while maintenance is on (switched off included, peers only too), else None: the instance manager and the
    gateway load, reload, recycle and start nothing on this Mac."""
    m = read(now=now)
    return m if m and m['phase'] == 'on' else None


def refusing(now=None):
    """The record while the gateway refuses new requests (503 with Retry-After): maintenance that is on, or the local
    models switched off without peers. Peers only admits them (to the peers' slots). Else None."""
    m = on(now=now)
    return m if m and not m.get('peers') else None


def retry_after(m, now=None):
    """Seconds a refused program should wait: to the expected end, or a minute when it runs over."""
    now = now or time.time()
    if m and m.get('off'): return OFF_RETRY
    return max(60, int(m['end'] - now)) if m and m.get('end') else 60


def _clock(t): return time.strftime('%H:%M', time.localtime(t))


def _stretch(minutes): return f'{minutes} min' if minutes < 90 else f'{minutes / 60:.1f} h'.replace('.0 h', ' h')


def words(m, now=None):
    """One plain sentence for the dash, the status line and the tools."""
    if not m: return ''
    now = now or time.time()
    why = f": {m['why']}" if m.get('why') else ''
    if m.get('off') and m.get('peers'):
        return (f"local models switched off since {_clock(m['start'])}{why}; requests go to peers only"
                + ('' if m.get('unloaded') else ' (unloading once the requests in flight here finish)') + '; turn on: localmodels on')
    if m.get('off'):
        return (f"local models switched off since {_clock(m['start'])}{why}"
                + ('' if m.get('unloaded') else ' (unloading once the requests in flight finish)') + '; turn on: localmodels on')
    if m['phase'] == 'waiting': return f"maintenance pending: starts once batch traffic drains, est. {_stretch(m['minutes'])}{why}"
    if m['phase'] == 'announced': return f"maintenance scheduled at {_clock(m['start'])} (in {_stretch(max(1, round((m['start'] - now) / 60)))}), est. {_stretch(m['minutes'])}{why}"
    if now > m['end']: return f"in maintenance since {_clock(m['start'])}, overrunning (scheduled end {_clock(m['end'])}){why}"
    return f"in maintenance until ~{_clock(m['end'])}{why}"


def header(m, now=None):
    """The X-Hub-Maintenance header: 'announced; starts=...; until=...; why=...' (ISO local times), or None."""
    if not m: return None
    iso = lambda t: time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(t))
    if m.get('off') and m.get('peers'): return f"peers-only; switched-off; since={iso(m['start'])}; why=local models switched off, requests go to peers"
    if m.get('off'): return f"on; switched-off; since={iso(m['start'])}; why=local models switched off"
    parts = ['after-batches' if m['phase'] == 'waiting' else m['phase']]
    if m.get('start') is not None and m['phase'] == 'announced': parts.append(f"starts={iso(m['start'])}")
    if m.get('end'): parts.append(f"until={iso(m['end'])}")
    parts.append(f"minutes={m['minutes']}")
    if m.get('why'): parts.append('why=' + re.sub(r'[;\r\n]+', ' ', m['why']))
    return '; '.join(parts)


def view(now=None):
    """What the screens show: {phase, start, end, minutes, why, words}, or None."""
    m = read(now=now)
    return dict(phase=m['phase'], start=m.get('start'), end=m.get('end'), minutes=m['minutes'], why=m.get('why'), words=words(m, now),
                off=bool(m.get('off')), unloaded=m.get('unloaded'), peers=bool(m.get('peers')),
                inflight=(len(local_work()) if m.get('peers') else len(llmshare.inflight())) if m.get('off') and not m.get('unloaded') else None) if m else None


def local_work(marks=None):
    """The requests in flight on this Mac's own instances (answering on one, not on a peer's): what an unload while
    switched off to peers only waits for. marks: llmshare.inflight() (read now when None)."""
    return [d for d in (llmshare.inflight() if marks is None else marks) if d.get('state') == 'answering' and d.get('copy') and ' on ' not in str(d['copy'])]


def notice(now=None):
    """The line the Claude Code hook adds to a session, or '' when there is nothing to say."""
    m = read(now=now)
    if not m or m.get('off'): return ''
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
    except Exception as e:                                   # never fails a prompt: recorded, not raised
        from . import errors; errors.record('maintenance notice hook', e, source='hook')
    return 0
