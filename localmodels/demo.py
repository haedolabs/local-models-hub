"""demo - a synthetic workload through the gateway, for screenshots and load tests: three generic projects run batches over
records this module makes up (stock ledgers with wrong balances to find, arithmetic answers to grade, planning notes to
summarize), so no file is read and nothing is about a person. Enough requests go at once to fill every slot the gateway
offers (this Mac's and its peers'), with two waiting, for a set time; each project shows as a batch with its progress and
finish time. Nothing is kept but the gateway's own request log.

  localmodels demo [--minutes 60] [--dry]

With `localmodels present on`, every screen shows these projects by name and any other project as "other", with no
session titles: pictures of the hub at work with nothing personal in them.
"""
import json, os, random, subprocess, sys, threading, time, urllib.error, urllib.request

from . import HOME, agent, llmshare

TOKENS = 700                    # the most an answer may be (thinking off): about 20 s a request on the measured hardware
EST_SECONDS = 25.0              # a first guess at one request, until the run has measured its own
OPS = {'+': lambda a, b: a + b, '-': lambda a, b: a - b, '*': lambda a, b: a * b}


def ledger(rng, n=14):
    """Stock records whose running balance is wrong on two lines."""
    item, bal = rng.choice(('crates', 'pallets', 'drums', 'reels', 'cartons')), rng.randint(20, 80)
    wrong, lines = set(rng.sample(range(3, n + 1), 2)), [f'1. opening balance: {bal} {item}']
    for i in range(2, n + 1):
        d = rng.randint(-15, 25); bal += d
        lines.append(f"{i}. {'received' if d >= 0 else 'shipped'} {abs(d)} {item}, balance {bal + (rng.choice((-3, -2, 2, 4)) if i in wrong else 0)}")
    return ('Check these stock records: on which lines does the balance not follow from the line before? For each, give the line, '
            'the balance stated and the right one.\n\n' + '\n'.join(lines))


def grading(rng, n=10):
    """Arithmetic with answers, about a third of them wrong."""
    rows = []
    for i in range(1, n + 1):
        a, b, op = rng.randint(12, 99), rng.randint(3, 40), rng.choice('+-*')
        right = OPS[op](a, b)
        rows.append(f'{i}. {a} {op} {b} = {right if rng.random() < 0.65 else right + rng.choice((-10, -1, 1, 7))}')
    return 'Grade each answer right or wrong, and give the correct result for every wrong one.\n\n' + '\n'.join(rows)


def notes(rng, n=10):
    """Planning notes for a made-up site."""
    site = rng.choice(('north yard', 'river depot', 'east annex', 'harbor store', 'hill plant'))
    acts = ('move the loading dock schedule to', 'order two more pallets of', 'repaint the bays for', 'test the backup power for',
            'count the stock of', 'replace the lights in', 'retrain the night shift on', 'check the scales in')
    things = ('copper wire', 'cold storage', 'the west gate', 'cardboard', 'bay 4', 'returns', 'the forklifts', 'labels')
    rows = [f'- {rng.choice(acts)} {rng.choice(things)} by week {rng.randint(1, 12)}' for _ in range(n)]
    return f'Summarize these planning notes for the {site} in five bullet points, the most urgent first.\n\n' + '\n'.join(rows)


PROJECTS = (                    # name, batch (X-Run), job (X-Step), weight, records
    ('research', 'consistency check', 'records', 6, ledger),
    ('evals', 'answer grading', 'answers', 5, grading),
    ('docs', 'summaries', 'notes', 3, notes),
)


def names(): return [p[0] for p in PROJECTS]


def model_of():
    """The family the demo runs on: the first in the settings, else the first loaded model."""
    fams = list((llmshare.settings().get('families') or {}))
    if fams: return fams[0]
    rows = llmshare.loaded()
    return llmshare.family(rows[0]['id']) if rows else None


def plan(minutes=60, workers=None, model=None):
    """-> (model, workers in all, {project: workers}): every slot filled, two more waiting, shared by weight."""
    model = model or model_of()
    n = int(workers or ((llmshare.at_once(model) if model else 4) + 2))
    weights = [p[3] for p in PROJECTS]
    share = {p[0]: max(1, round(n * w / sum(weights))) for p, w in zip(PROJECTS, weights)}
    while sum(share.values()) > n and max(share.values()) > 1: share[max(share, key=share.get)] -= 1
    while sum(share.values()) < n: share[PROJECTS[0][0]] += 1
    return model, n, share


def spawn(minutes=60, say=print, seed=None):
    """The demo as the screens should see it: one program per project (each its own process, its own row among the
    clients), together filling every slot. -> 0 when every project got answers."""
    model, n, share = plan(minutes)
    if not model: say('no model to run on: load one (localmodels use <model>)'); return 1
    seed = int(time.time()) if seed is None else seed
    say(f"demo: {n} requests at once on {model} for {minutes:g} min, one program per project: " + ', '.join(f"{p[0]} ({share[p[0]]}, {p[1]})" for p in PROJECTS))
    kids = [subprocess.Popen([sys.executable, os.path.join(HOME, 'bin', 'localmodels'), 'demo', '--minutes', str(minutes), '--project', p[0],
                              '--workers', str(share[p[0]]), '--model', model, '--seed', str(seed + k)]) for k, p in enumerate(PROJECTS)]
    try:
        for kid in kids: kid.wait()
    except KeyboardInterrupt:
        say('demo: stopping; the requests in flight finish')
        for kid in kids:
            try: kid.wait()
            except KeyboardInterrupt: kid.kill()
    return 0 if all(kid.returncode == 0 for kid in kids) else 1


def run(url=None, minutes=60, workers=None, model=None, stop=None, say=print, seed=None, only=None):
    """Run the demo until `minutes` have passed (or stop is set); each request in flight then finishes. only: one project
    with `workers` requests at once (a process of its own, from spawn). -> a summary."""
    url, stop = (url or llmshare.HUB).rstrip('/'), stop or threading.Event()
    model, n, share = plan(minutes, workers, model)
    if only: n, share = int(workers or share.get(only) or 1), {only: int(workers or share.get(only) or 1)}
    if not model: raise RuntimeError('no model to run on: load one (localmodels use <model>)')
    t0, deadline, seed = time.time(), time.time() + minutes * 60, int(time.time()) if seed is None else seed
    st = {p[0]: dict(started=0, done=0, errors=0, tokens=0, total=max(1, int(share[p[0]] * minutes * 60 / EST_SECONDS)), lock=threading.Lock())
          for p in PROJECTS if p[0] in share}

    def total_now(s, now):                               # the batch's size, re-estimated from its own pace: it ends at the deadline
        if s['done'] >= 3: s['total'] = max(s['started'], s['done'] + int(s['done'] / max(1.0, now - t0) * max(0.0, deadline - now)))
        return s['total']

    def worker(k, name, batch, job, make):
        rng, s = random.Random(seed * 1000 + k), st[name]
        while not stop.is_set() and time.time() < deadline:
            with s['lock']:
                s['started'] += 1; i, total, done = s['started'], total_now(s, time.time()), s['done']
            body = json.dumps(dict(model=model, messages=[dict(role='user', content=make(rng))], max_tokens=TOKENS, **agent.SAMPLING, **agent.THINK_OFF)).encode()
            req = urllib.request.Request(url + '/v1/chat/completions', data=body, headers={
                'Content-Type': 'application/json', 'X-Project': name, 'X-Kind': 'batch', 'X-Run': batch,
                'X-Run-Total': str(total), 'X-Run-Done': str(done), 'X-Step': f'{job} {i} of {total}', 'X-Timeout': '3600'})
            try:
                usage = json.load(urllib.request.urlopen(req, timeout=3600)).get('usage') or {}
                with s['lock']: s['done'] += 1; s['tokens'] += int(usage.get('completion_tokens') or 0)
            except urllib.error.HTTPError as e:              # 503 (maintenance, a reload): wait as told, the record is tried again
                with s['lock']: s['started'] -= 1
                if e.code == 503: stop.wait(min(30.0, float(e.headers.get('Retry-After') or 10)))
                else:
                    with s['lock']: s['errors'] += 1
                    stop.wait(2)
            except (OSError, ValueError):
                with s['lock']: s['started'] -= 1; s['errors'] += 1
                stop.wait(2)

    threads, k = [], 0
    for name, batch, job, _, make in PROJECTS:
        for _ in range(share.get(name, 0)):
            th = threading.Thread(target=worker, args=(k, name, batch, job, make), daemon=True); th.start(); threads.append(th); k += 1
    if not only: say(f"demo: {n} requests at once on {model} for {minutes:g} min: " + ', '.join(f"{p[0]} ({share[p[0]]}, {p[1]})" for p in PROJECTS))
    last = time.time()
    try:
        while any(th.is_alive() for th in threads):
            for th in threads: th.join(0.5)
            if time.time() - last >= 60:
                last = time.time()
                say(f"  {int((last - t0) // 60)} min: " + ' · '.join(f"{p}: {v['done']} of {v['total']}" for p, v in st.items())
                    + f" · {sum(v['tokens'] for v in st.values()) / max(1.0, last - t0):.0f} tokens/s written")
    except KeyboardInterrupt:
        stop.set(); say('demo: stopping; the requests in flight finish')
        for th in threads: th.join()
    out = {p: {k2: v for k2, v in s.items() if k2 != 'lock'} for p, s in st.items()}
    say(f"demo{' ' + only if only else ''}: done in {(time.time() - t0) / 60:.1f} min: " + ' · '.join(f"{p}: {v['done']} answered, {v['errors']} errors" for p, v in out.items()))
    return out
