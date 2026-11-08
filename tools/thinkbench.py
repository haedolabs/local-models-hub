#!/usr/bin/env python3
"""thinkbench - the loaded model with thinking on against off, on four everyday local-agent jobs, through the gateway.

  python3 tools/thinkbench.py [--model ID] [--runs 2] [--anyway]

Each job runs with thinking on and off side by side (the same load for both), --runs times: a consistency check (a memo
with a wrong weekday and a sum that does not add up), an extraction (its dates and amounts as JSON), and two lookups in
this folder with the agents' read-only tools. It prints, per setting: how many answers were right, the generation time
(from the gateway's request log, queue time excluded) and the tokens generated. A report goes to
~/Library/Logs/LocalModels/thinkbench-<time>.json. It refuses to start while a batch is using the models (--anyway: start
regardless). Run it when the model changes; GUIDE.md ("Thinking") keeps what it found.

Measured (qwen/qwen3.6-35b-a3b 8-bit, two copies of 4, three runs in all): thinking on 12 of 12 right, 305 s
answering, 22,598 tokens written; off 10 of 12 right (it missed the wrong sum in 2 of 3 consistency checks), 93 s, 3,969.
"""
import argparse, json, os, sys, threading, time, urllib.request

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOME)
from localmodels import agent, llmshare

MEMO = """Memo to the office, 3 March 2026.
1. The supplier meeting is on Tuesday 10 March 2026 at 14:00 in Room 4.
2. The deposit of $4,500 was paid on 2 March 2026.
3. Bring the signed copy of the contract to Room 4.
4. Reminder: the supplier meeting is on Wednesday 10 March at 14:00.
5. The remaining balance is $5,500, so the contract comes to $9,000 in all.
6. The balance is due on 31 March 2026."""
obj = lambda props, req: {'type': 'object', 'properties': props, 'required': req}
_tag = {}


def _post(path, body, project, kind='batch', timeout=3600, headers=None):
    """agent.post, with each run's name on its requests (X-Session), so the door's log tells the runs apart."""
    req = urllib.request.Request(llmshare.HUB + path, data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json', 'X-Project': 'local-models', 'X-Kind': 'test', 'X-Session': _tag.get(threading.get_ident(), 'thinkbench'), **(headers or {})})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _text(p): return (str(p.get('quote', '')) + ' ' + str(p.get('why', ''))).lower()
def _day(p): return 'wednesday' in _text(p) or 'tuesday' in _text(p)
def _sum(p): return any(s in _text(p) for s in ('9,000', '10,000', '9000', '10000'))


def check_consistency(r):
    ps = [p for p in ((r or {}).get('problems') or []) if isinstance(p, dict)]
    day, money, wrong = any(map(_day, ps)), any(map(_sum, ps)), sum(1 for p in ps if not (_day(p) or _sum(p)))
    return day and money and not wrong, f"weekday {'found' if day else 'missed'}, sum {'found' if money else 'missed'}, {wrong} wrong flags"


def check_extract(r):
    d = set((r or {}).get('dates') or [])
    a = {float(x) for x in ((r or {}).get('amounts') or []) if isinstance(x, (int, float))}
    return d == {'2026-03-03', '2026-03-10', '2026-03-02', '2026-03-31'} and a == {4500.0, 5500.0, 9000.0}, f'dates {sorted(d)}, amounts {sorted(a)}'


def check_hold(r):
    r = r or {}
    return str(r.get('file', '')).endswith('crashes.py') and str(r.get('name', '')).upper() == 'HOLD' and r.get('seconds') == 600, \
        f"{r.get('file')} {r.get('name')} {r.get('seconds')}"


def check_room(r):
    r = r or {}
    return str(r.get('file', '')).endswith('models.json') and r.get('name') == 'headroom_gb' and r.get('gb') == 8, \
        f"{r.get('file')} {r.get('name')} {r.get('gb')}"


JOBS = [
    ('consistency check', False, check_consistency, dict(label='consistency', max_turns=2,
        prompt='Check this memo for statements that contradict each other or do not add up. Report each problem once, with the words '
               'it is about. Report nothing that is fine.\n\n' + MEMO,
        schema=obj({'problems': {'type': 'array', 'items': obj({'quote': {'type': 'string'}, 'why': {'type': 'string'}}, ['quote', 'why'])}}, ['problems']))),
    ('extraction', False, check_extract, dict(label='extract', max_turns=2,
        prompt='List every date in this memo once, in YYYY-MM-DD form, and every amount of money once, in dollars as a number.\n\n' + MEMO,
        schema=obj({'dates': {'type': 'array', 'items': {'type': 'string'}}, 'amounts': {'type': 'array', 'items': {'type': 'number'}}}, ['dates', 'amounts']))),
    ('find in code (tools)', True, check_hold, dict(label='hold', max_turns=12,
        prompt="In this folder, find how long the hub's door holds a request for a model that crashed and is loading again. "
               'Answer with the file (its path in this folder), the name of the constant, and the number of seconds.',
        schema=obj({'file': {'type': 'string'}, 'name': {'type': 'string'}, 'seconds': {'type': 'number'}}, ['file', 'name', 'seconds']))),
    ('find in settings (tools)', True, check_room, dict(label='room', max_turns=12,
        prompt='In this folder, find how many gigabytes of memory the keeper leaves free (its headroom) when it loads another copy of a '
               "model, and where that number is set. Answer with the file (its path in this folder), the setting's name, and the number.",
        schema=obj({'file': {'type': 'string'}, 'name': {'type': 'string'}, 'gb': {'type': 'number'}}, ['file', 'name', 'gb']))),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--model'); ap.add_argument('--runs', type=int, default=2); ap.add_argument('--anyway', action='store_true')
    a = ap.parse_args()
    if not llmshare.hub_up(fresh=True): sys.exit("gateway down: localmodels start")
    busy = [d for d in llmshare.leases() if d.get('kind') == 'batch']
    if busy and not a.anyway:
        print('a batch is using the models (' + ', '.join(sorted({d.get('project') or '?' for d in busy})) + '): try later, or --anyway'); return 3
    fams = llmshare.settings().get('families') or {}
    model = a.model or next(iter(fams), None) or next((llmshare.family(r['id']) for r in llmshare.loaded(fresh=True)), None)
    if not model: sys.exit('no model loaded')
    agent.post = _post
    files = agent.Files([HOME], where="the local-models hub's folder")
    results, t0 = [], time.time()

    def one(name, tools, checker, job, think, run):
        tag = f"thinkbench-{job['label']}-{'on' if think else 'off'}-{run}-{int(t0)}"
        _tag[threading.get_ident()] = tag
        r = agent.run_job(dict(job, think=think), model, (lambda j: files.defs()) if tools else (lambda j: []),
                          lambda n, x, j: files.call(n, x), 'local-models', kind='test')
        good, words = checker(r.get('result'))
        results.append(dict(job=name, think=think, run=run, tag=tag, right=good, words=words, turns=r.get('turns'), seconds=r.get('seconds'),
                            written=r['usage']['completion'], thinking=r['usage']['reasoning'], error=r.get('error')))
        print(f"  {name:26} thinking {'on ' if think else 'off'} #{run}: {'right' if good else 'WRONG'} · {r.get('seconds')} s · "
              f"{r['usage']['completion']} tokens written ({r['usage']['reasoning']} thinking) · {words}", flush=True)

    print(f'{model}: {len(JOBS)} jobs, thinking on and off side by side, {a.runs} time(s) each')
    for run in range(1, a.runs + 1):
        for name, tools, checker, job in JOBS:
            ts = [threading.Thread(target=one, args=(name, tools, checker, job, think, run)) for think in (True, False)]
            for t in ts: t.start()
            for t in ts: t.join()
    time.sleep(1)
    rows = [r for r in llmshare.requests_log((time.time() - t0) / 3600 + 0.05) if str(r.get('session', '')).startswith('thinkbench-')]
    for x in results:
        mine = [r for r in rows if r.get('session') == x['tag']]
        x['answering'] = round(sum((r.get('seconds') or 0) - (r.get('waited') or 0) for r in mine), 1)
        x['waiting'] = round(sum(r.get('waited') or 0 for r in mine), 1)
    print()
    for think in (True, False):
        xs = [x for x in results if x['think'] == think]
        print(f"thinking {'on ' if think else 'off'}: {sum(x['right'] for x in xs)} of {len(xs)} right · {sum(x['answering'] for x in xs):.0f} s answering "
              f"(+{sum(x['waiting'] for x in xs):.0f} s waiting for a turn) · {sum(x['written'] for x in xs):,} tokens written, {sum(x['thinking'] for x in xs):,} thinking")
        for name, *_ in JOBS:
            js = [x for x in xs if x['job'] == name]
            print(f"   {name:26} {sum(x['right'] for x in js)}/{len(js)} right · {sum(x['answering'] for x in js) / len(js):.1f} s · "
                  f"{sum(x['written'] for x in js) / len(js):.0f} tokens a run")
    os.makedirs(os.path.expanduser('~/Library/Logs/LocalModels'), exist_ok=True)
    out = os.path.expanduser(f"~/Library/Logs/LocalModels/thinkbench-{time.strftime('%Y%m%d-%H%M')}.json")
    json.dump(dict(model=model, at=round(t0), runs=a.runs, results=results), open(out, 'w'), indent=1)
    print(f'\n{(time.time() - t0) / 60:.1f} minutes · report: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
