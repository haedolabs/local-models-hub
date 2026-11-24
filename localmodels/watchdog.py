"""watchdog - an instance whose engine stops working while LM Studio still lists it (a stall) is taken out of routing,
its requests are sent again to another instance, and it is reloaded.

Why: LM Studio's MLX engine can stop mid-answer without crashing. Seen on an M5 Max under a batch of agent conversations
with the Mac's memory nearly full: one instance's engine wrote its last line at 03:38:37 and the other's at 20:02 the
same day, while LM Studio kept listing both as loaded and kept taking requests for them (its "Running chat completion"
line, with no prompt processing after it). Nothing crashed and nothing was logged, so every request routed to the first
waited for its client's one-hour timeout, four at a time for 8.5 hours, and a drain for its recycle never emptied.

The signal is LM Studio's own log, which the instance manager already reads as it is written (crashes.watch): the
engine writes a line tagged with the instance's identifier at each step of its work, about every second while it reads
a prompt ("Prompt processing progress"), and as reasoning, a tool call or an answer begins or ends. The server's line on
taking a request (crashes.ACCEPTED) comes before the engine does anything, so it does not count. Measured over 7,763
requests answered in four days of batch work by two healthy instances: the first engine line after a request was sent came
within 0.6 s (median; p99 10.8 s, p99.9 18.7 s, longest 42 s), and with requests in flight an instance never went more
than 195 s without one (p99 73 s; long answers with nothing else under way).

The rules, per family in models.json ("watchdog": {"first": 120, "silent": 600}; false turns it off):
  first   a request has been answering on the instance for `first` seconds, and the instance has written no engine
          line since it was sent: the engine never took it up (a time-to-first-token watchdog, kept per instance, since a
          non-streaming answer shows nothing until it is whole).
  silent  the instance has requests in flight and has written no engine line for `silent` seconds, or for as long as the
          longest answer asked of it (its max_tokens) takes at GEN_RATE tokens a second, up to SILENT_MAX (an answer
          with no max_tokens counts as that): it stopped mid-answer with every slot held, so no new request comes to
          show it. (The clients here ask for 700 to 8,000 tokens: 600 s for most, 1,000 s for 8,000.)
The first rule needs no scaling by prompt size: an engine reading any prompt writes a line about every second, so a
request waiting behind another's long prompt still sees the instance at work.
Both are measured against LM Studio's newest line of any kind rather than the clock, so a log that is not being written
(or read) stalls nothing, and neither applies until the log has shown an engine step at all within the last day (a log
level that writes none would read as a stall everywhere). Embeddings are left out.

What follows (models.keep): the instance is marked stalled (crashes.stall, keeper.json "stalled", a 'stall' record in
crashes.jsonl); the door sends it nothing new and cuts each request in flight on it, which is sent again to another
instance (or a peer) when nothing of its answer has reached the program yet, and otherwise ends its stream with an error
event; recycle.plan then reloads it without a drain (a stall job), its older sibling first when LM Studio would resolve
its name to that sibling. At most CAP stall reloads of one family within an hour; after that a stalled instance stays
out of routing for a person (localmodels use <model>) until the hour has passed. This is passive health checking with
outlier ejection, as load balancers do it (Envoy's outlier detection, NGINX's max_fails), with a reload as the remedy;
the crash recovery and recycling around it are unchanged.
"""
import time

from . import crashes, llmshare

DEFAULT = dict(first=120, silent=600)
GEN_RATE = 8                     # tokens a second an answer is written at, at the least (several times below what an instance writes at four at once)
SILENT_MAX = 1800                # the silence limit at most, however long the answers in flight may be
CAP, WINDOW = 3, 3600            # stall reloads of one family within an hour; past that a stalled instance waits for a person
STEPS_WITHIN = 86400             # the log must have shown an engine step within this long, or the watchdog judges nothing
SKIP = ('embeddings',)           # endpoints left out: an embedding writes no engine steps


def thresholds(f):
    """A family's watchdog settings (models.json "watchdog"), or None when it is turned off."""
    w = f.get('watchdog', True) if isinstance(f, dict) else True
    if w is False or w is None: return None
    out = dict(DEFAULT)
    if isinstance(w, dict): out.update({k: v for k, v in w.items() if k in out and isinstance(v, (int, float)) and v > 0})
    return out


def look(marks, engine, line_at, step_at, fams, listed, now=None):
    """Which instances on this Mac are stalled: -> {instance id: (why, requests in flight)}.
    marks: the gateway's in-flight markers (llmshare.inflight); engine: {instance id: its last engine line}; line_at: LM
    Studio's newest line of any kind; step_at: its newest engine step; fams: the families in the settings; listed: the
    instance ids loaded now. An instance not listed, or in a family whose watchdog is off, is never judged."""
    now = now or time.time()
    if not line_at or not step_at or now - step_at > STEPS_WITHIN: return {}
    work = {}
    for d in marks or []:
        c = d.get('copy')
        if (d.get('state') != 'answering' or not c or ' on ' in str(c) or c not in listed or str(d.get('ep') or '') in SKIP
                or not d.get('answering_since')): continue
        try: asked = int(d.get('answer') or 0)
        except (TypeError, ValueError): asked = 0
        work.setdefault(c, []).append((float(d['answering_since']), asked))
    out = {}
    for c, xs in work.items():
        th = thresholds((fams or {}).get(llmshare.family(c)))
        if not th: continue
        sent = [s for s, _ in xs]
        longest = SILENT_MAX * GEN_RATE if any(a <= 0 for _, a in xs) else max(a for _, a in xs)
        limit = max(th['silent'], min(SILENT_MAX, longest / GEN_RATE))
        last = float(engine.get(c) or 0)
        quiet = [s for s in sent if last < s - 1]              # sent after its last engine line (the log's stamps are whole seconds)
        if quiet and line_at - min(quiet) >= th['first']:
            out[c] = (f"stalled: no engine activity in LM Studio's log for {int(line_at - min(quiet))} s after a request was sent "
                      f"(first-token limit {th['first']} s; {len(sent)} request{'s' if len(sent) != 1 else ''} in flight)", len(sent))
            continue
        since = max(last, min(sent))
        if line_at - since >= limit:
            out[c] = (f"stalled: no engine activity in LM Studio's log for {int(line_at - since)} s with "
                      f"{len(sent)} request{'s' if len(sent) != 1 else ''} in flight (limit {int(limit)} s)", len(sent))
    return out


def recent(base, now=None):
    """The stalls of one family recorded in the last WINDOW seconds (crashes.jsonl)."""
    now = now or time.time()
    return [r for r in crashes.records(1) if r.get('event') == 'stall' and r.get('family') == base and now - (r.get('at') or 0) < WINDOW]


def check(rows, fams=None):
    """One look (models.keep, right after crashes.watch read LM Studio's log): mark each newly stalled instance. -> words."""
    fams = llmshare.settings().get('families') or {} if fams is None else fams
    st = crashes._read(fresh=True)
    found = look(llmshare.inflight(), st.get('engine') or {}, st.get('log_line'), st.get('log_step'), fams, {r['id'] for r in rows})
    did = []
    for c, (why, n) in sorted(found.items()):
        if crashes.stall(c, why, n): did.append(f'{c}: {why}; out of routing, its requests resent, reload follows')
    return did


def view(now=None):
    """For the screens: the watchdog's state in a few words, or None when it watches as it should."""
    st, now = crashes._read(fresh=True), now or time.time()
    if not st.get('log_step') or now - st['log_step'] > STEPS_WITHIN:
        return "watchdog idle: LM Studio's log has shown no engine step in the last day (its log level writes none?)"
    return None
