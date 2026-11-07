#!/usr/bin/env python3
"""loadbench - the loaded model under a document-extraction load, through the door: long prompts, long strict-JSON answers,
thinking on and off, in both layouts (two copies with 8 at once; one copy with 4 at once).

  python3 tools/loadbench.py [--n 16] [--max-tokens 10000] [--family qwen/qwen3.6-35b-a3b] [--cells all|two|one]
                             [--smoke] [--anyway]

Each request: a system prompt naming 40 quantities to extract, a made-up memo of about 9,000 characters with some 55 of
them planted (dates and amounts; the generator knows the answers), and a strict JSON schema for the list of items; up to
--max-tokens tokens of answer; the model card's sampling. Four cells, --n requests each: {two copies, 8 at once; one copy,
4 at once} x {thinking on; thinking off}. The one-copy cell pins every request to the second copy (<family>:2), so nothing
is unloaded and the first copy idles, which is the same for throughput. Per cell it prints: wall time, completion and
prompt tokens a second in all, seconds a request (median, p90), empty answers, recall of the planted items, invented
values. It holds a 'test' lease so no swap pulls the copies out from under it, and refuses to start while a batch is using
the models (--anyway: start regardless). --smoke: two short requests in one cell, to check the tool itself. Report:
~/Library/Logs/LocalModels/loadbench-<time>.json. Run it when the model or the engine changes; GUIDE.md keeps what it found.
"""
import argparse, concurrent.futures as cf, json, os, random, re, statistics, sys, threading, time, urllib.error, urllib.request

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOME)
from localmodels import agent, llmshare

QUANTITIES = [('order total', 'money'), ('deposit paid', 'money'), ('delivery date', 'date'), ('monthly fee', 'money'),
              ('contract start date', 'date'), ('contract end date', 'date'), ('security deposit', 'money'), ('appraised value', 'money'),
              ('inspection date', 'date'), ('loan amount', 'money'), ('interest reserve', 'money'), ('maturity date', 'date'),
              ('insurance premium', 'money'), ('policy renewal date', 'date'), ('repair estimate', 'money'), ('invoice total', 'money'),
              ('invoice date', 'date'), ('payment received', 'money'), ('payment date', 'date'), ('outstanding balance', 'money'),
              ('late fee', 'money'), ('notice date', 'date'), ('review date', 'date'), ('setup fee', 'money'), ('quoted price', 'money'),
              ('quote date', 'date'), ('consulting retainer', 'money'), ('filing fee', 'money'), ('kickoff date', 'date'),
              ('handover date', 'date'), ('budget approved', 'money'), ('tax assessment', 'money'), ('assessment date', 'date'),
              ('escrow balance', 'money'), ('wire transfer', 'money'), ('transfer date', 'date'), ('commission paid', 'money'),
              ('shipping costs', 'money'), ('final inspection date', 'date'), ('refund issued', 'money')]
MONEY = ['The {q} was recorded as {v}.', 'According to the file, the {q} stands at {v}.', 'Note that the {q} came to {v} in all.',
         'The ledger shows the {q} as {v}, which nobody disputed.', 'Finance confirmed the {q}: {v}.',
         'As of that week the {q} was {v}, up from the earlier figure.', 'The {q} ({v}) appears on page two of the schedule.']
DATES = ['The {q} was {v}.', 'Both teams agreed that the {q} would be {v}.', 'Per the notice, the {q} is {v}.',
         'The {q} was moved to {v} at the request of the supplier.', 'The file lists the {q} as {v}.', 'On the calendar the {q} shows as {v}.']
FILLER = ['The correspondence that followed was routine and added nothing of substance to the record.',
          'Both teams exchanged drafts over the next several days without reaching a final form.',
          'The site manager noted that the contractor had been cooperative throughout the process.',
          'A copy of the signed agreement is kept with the original file in the office.',
          'The inspector described the roof as serviceable and the gutters as in need of cleaning.',
          'Nothing in the later letters changed the position either side had taken at the outset.',
          'The adjuster asked for photographs, which were supplied the same afternoon.',
          'The coordinator acknowledged receipt and said the item would be scheduled in due course.',
          'The accountant reconciled the statements and found no further discrepancies.',
          'Both teams agreed to continue discussions after the holiday.']
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
SCHEMA = {'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'type': 'object', 'properties': {
    'quantity': {'type': 'string'}, 'value': {'type': 'string'}, 'quote': {'type': 'string'}}, 'required': ['quantity', 'value', 'quote']}}},
    'required': ['items']}
SYSTEM = ('You extract named quantities from a memo. The quantities you look for: ' + '; '.join(f'{q} (a {k})' for q, k in QUANTITIES)
          + '. List every mention of any of them, once per mention: the quantity, its value as written, and the sentence it is in. '
            'Report nothing that is not one of these quantities. Answer as JSON matching the schema, and nothing else.')
_tag = {}


# ---------------------------------------------------------------- the memo, with the answers known
def date_forms(y, m, d):
    return [f'{d} {MONTHS[m - 1]} {y}', f'{MONTHS[m - 1]} {d}, {y}', f'{y}-{m:02d}-{d:02d}', f'{m}/{d}/{y}']


def memo(seed, chars=13000, planted_share=0.5):
    """-> (text, planted [{quantity, kind, value, forms}]): about chars characters, planted_share of its sentences a fact."""
    rnd, parts, planted, n = random.Random(seed), [], [], 0
    while n < chars:
        if rnd.random() < planted_share:
            q, kind = rnd.choice(QUANTITIES)
            if kind == 'money':
                v = f'${rnd.randrange(500, 2_000_000, 50):,}' + (f'.{rnd.randrange(1, 99):02d}' if rnd.random() < 0.3 else '')
                s, forms = rnd.choice(MONEY).format(q=q, v=v), [v]
            else:
                y, m, d = rnd.randrange(2019, 2027), rnd.randrange(1, 13), rnd.randrange(1, 29)
                forms = date_forms(y, m, d); v = rnd.choice(forms)
                s = rnd.choice(DATES).format(q=q, v=v)
            planted.append(dict(quantity=q, kind=kind, value=v, forms=forms))
        else: s = rnd.choice(FILLER)
        parts.append(s); n += len(s) + 1
        if rnd.random() < 0.12: parts.append('\n\n')
    return ' '.join(parts).replace(' \n\n ', '\n\n'), planted


def norm(s):
    s = str(s or '').lower()
    s = re.sub(r'\.00\b', '', s)
    return re.sub(r'[^a-z0-9/-]+', '', s)


def score(items, planted, text):
    """-> (found, invented): planted items whose value is in some item's value or quote; items whose value is not in the memo."""
    blob = norm(' '.join(norm(i.get('value', '')) + ' ' + norm(i.get('quote', '')) for i in items))
    found = sum(1 for p in planted if any(norm(f) in blob for f in p['forms']))
    memo_blob, invented = norm(text), 0
    for i in items:
        v = norm(i.get('value', ''))
        if v and v not in memo_blob and not any(norm(f) == v for p in planted for f in p['forms']): invented += 1
    return found, invented


# ---------------------------------------------------------------- one request, through the door
def ask(model, text, think, max_tokens, tag):
    body = dict(model=model, max_tokens=max_tokens, **agent.SAMPLING,
                messages=[{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': 'Extract every quantity from this memo as JSON.\n\n' + text}],
                response_format={'type': 'json_schema', 'json_schema': {'name': 'items', 'strict': True, 'schema': SCHEMA}})
    if not think: body.update(agent.THINK_OFF)
    req = urllib.request.Request(llmshare.HUB + '/v1/chat/completions', data=json.dumps(body).encode(), headers={
        'Content-Type': 'application/json', 'X-Project': 'local-models', 'X-Kind': 'test', 'X-Session': tag})
    t0 = time.time()
    try: r = json.load(urllib.request.urlopen(req, timeout=3600)); err = None
    except urllib.error.HTTPError as e:
        try: err = f'HTTP {e.code}: ' + e.read().decode('utf-8', 'replace')[:200]
        except Exception: err = f'HTTP {e.code}'
        r = {}
    except (OSError, ValueError) as e: err, r = f'{type(e).__name__}: {e}', {}
    m = (r.get('choices') or [{}])[0].get('message') or {}
    obj = agent.json_from(m.get('content')) or agent.json_from(m.get('reasoning_content'))
    items = [i for i in (obj.get('items') or []) if isinstance(i, dict)] if isinstance(obj, dict) else []
    u = r.get('usage') or {}
    return dict(seconds=round(time.time() - t0, 1), items=items, parsed=isinstance(obj, dict), prompt=u.get('prompt_tokens') or 0,
                completion=u.get('completion_tokens') or 0, reasoning=(u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0,
                finish=(r.get('choices') or [{}])[0].get('finish_reason'), error=err)


def cell(name, model, at_once, think, n, max_tokens, run_id, seed0):
    memos = [memo(seed0 + i) for i in range(n)]
    results, t0 = [None] * n, time.time()

    def one(i):
        tag = f'loadbench-{name}-{i}-{run_id}'
        _tag[threading.get_ident()] = tag
        text, planted = memos[i]
        r = ask(model, text, think, max_tokens, tag)
        found, invented = score(r['items'], planted, text)
        r.update(tag=tag, planted=len(planted), found=found, invented=invented, n_items=len(r['items']),
                 empty=r['parsed'] and not r['items'] and r['finish'] != 'length' and not r['error'])   # a well-formed answer with nothing in it
        r.pop('items')
        results[i] = r
        print(f"    {name} #{i + 1:>2}: {r['seconds']:>6.1f} s · {r['completion']:>5} tokens written ({r['reasoning']} thinking) · "
              f"{r['n_items']:>3} items, {found} of {len(planted)} planted found, {invented} invented" + (f" · {r['error']}" if r['error'] else '')
              + (' · EMPTY' if r['empty'] else ''), flush=True)
    with cf.ThreadPoolExecutor(max_workers=at_once) as ex: list(ex.map(one, range(n)))
    wall = time.time() - t0
    rs = [r for r in results if r]
    secs = sorted(r['seconds'] for r in rs)
    comp, prom = sum(r['completion'] for r in rs), sum(r['prompt'] for r in rs)
    rates = [r['completion'] / r['seconds'] for r in rs if r['seconds'] and not r['error'] and r['completion'] >= 50]   # a real answer's own pace; x at_once = the rate while every slot is busy
    busy = round(at_once * sum(rates) / len(rates), 1) if rates else 0.0
    return dict(cell=name, model=model, at_once=at_once, think=think, n=n, wall=round(wall, 1), completion_per_s=round(comp / wall, 1), prompt_per_s=round(prom / wall, 1),
                busy_completion_per_s=busy, per_request_per_s=round(busy / at_once, 1),
                completion=comp, prompt=prom, median_s=secs[len(secs) // 2] if secs else None, p90_s=secs[int(len(secs) * 0.9)] if secs else None,
                empty=sum(r['empty'] for r in rs), errors=sum(1 for r in rs if r['error']), cut_off=sum(1 for r in rs if r['finish'] == 'length'),
                planted=sum(r['planted'] for r in rs), found=sum(r['found'] for r in rs), invented=sum(r['invented'] for r in rs),
                items=sum(r['n_items'] for r in rs), requests=rs)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--n', type=int, default=16); ap.add_argument('--max-tokens', type=int, default=10000)
    ap.add_argument('--family'); ap.add_argument('--cells', default='all', choices=['all', 'two', 'one'])
    ap.add_argument('--smoke', action='store_true'); ap.add_argument('--anyway', action='store_true')
    a = ap.parse_args()
    if not llmshare.hub_up(fresh=True): sys.exit("the hub's door is closed: localmodels start")
    busy = [d for d in llmshare.leases() if d.get('kind') == 'batch']
    if busy and not a.anyway:
        print('a batch is using the models (' + ', '.join(sorted({d.get('project') or '?' for d in busy})) + '): try later, or --anyway'); return 3
    fams = llmshare.settings().get('families') or {}
    family = a.family or next(iter(fams), None) or next((llmshare.family(r['id']) for r in llmshare.loaded(fresh=True)), None)
    if not family: sys.exit('no model loaded')
    loaded = [r['id'] for r in llmshare.loaded(fresh=True) if llmshare.family(r['id']) == family]
    copies = llmshare.copies(family, fresh=True)
    second = next((c for c in copies if c != family), None)
    per_copy = llmshare.parallel_of(copies[0]) if copies else 4
    cells = []
    if a.cells in ('all', 'two') and len(loaded) >= 2:
        cells += [(f'two-copies-x{per_copy * len(loaded)}-on', family, per_copy * len(loaded), True), (f'two-copies-x{per_copy * len(loaded)}-off', family, per_copy * len(loaded), False)]
    elif a.cells == 'two': print(f'{family} is loaded once: no two-copy cell'); return 1
    if a.cells in ('all', 'one'):
        pin = second or family
        cells += [(f'one-copy-x{per_copy}-on', pin, per_copy, True), (f'one-copy-x{per_copy}-off', pin, per_copy, False)]
    n, max_tokens = (2, 600) if a.smoke else (a.n, a.max_tokens)
    if a.smoke: cells = cells[-1:]
    run_id, t0, out = int(time.time()), time.time(), []
    print(f'{family}: {len(loaded)} cop{"y" if len(loaded) == 1 else "ies"} loaded ({", ".join(loaded)}); {len(cells)} cells, {n} requests each, '
          f'up to {max_tokens} tokens an answer' + (' (smoke)' if a.smoke else ''))
    with llmshare.lease('local-models', [family], 'test'):
        for k, (name, model, at_once, think) in enumerate(cells):
            print(f'\n  {name}: {at_once} at once on {model}, thinking {"on" if think else "off"}')
            c = cell(name, model, at_once, think, n, max_tokens, run_id, 1000 * k)
            out.append(c)
            print(f"  -> {c['wall']:.0f} s wall · {c['busy_completion_per_s']} tokens/s written while every slot is busy ({c['per_request_per_s']} a request; "
                  f"{c['completion_per_s']} over the wall time) + {c['prompt_per_s']} prompt tokens/s · a request: median {c['median_s']} s, p90 {c['p90_s']} s · "
                  f"{c['empty']} empty, {c['errors']} errors, {c['cut_off']} cut off at max tokens · "
                  f"recall {c['found']}/{c['planted']} ({100 * c['found'] / max(1, c['planted']):.0f}%), {c['invented']} invented of {c['items']} items", flush=True)
    time.sleep(1)
    rows = {r.get('session'): r for r in llmshare.requests_log((time.time() - t0) / 3600 + 0.1) if str(r.get('session', '')).startswith('loadbench-')}
    for c in out:
        for r in c['requests']:
            d = rows.get(r['tag']) or {}
            r['waited'], r['copy'] = d.get('waited'), d.get('copy')
    print('\nsummary:')
    print(f"  {'cell':<22} {'wall s':>7} {'busy w/s':>9} {'wall w/s':>9} {'prompt/s':>9} {'median s':>9} {'p90 s':>7} {'empty':>6} {'errors':>7} {'cut':>4} {'recall':>7} {'invented':>9}")
    for c in out:
        print(f"  {c['cell']:<22} {c['wall']:>7.0f} {c['busy_completion_per_s']:>9.1f} {c['completion_per_s']:>9.1f} {c['prompt_per_s']:>9.1f} {c['median_s'] or 0:>9.1f} "
              f"{c['p90_s'] or 0:>7.1f} {c['empty']:>6} {c['errors']:>7} {c['cut_off']:>4} {100 * c['found'] / max(1, c['planted']):>6.0f}% {c['invented']:>9}")
    print('  busy w/s: tokens written a second while every slot is busy (each request\'s own pace x slots); wall w/s: over the whole cell, tail included')
    os.makedirs(os.path.expanduser('~/Library/Logs/LocalModels'), exist_ok=True)
    path = os.path.expanduser(f"~/Library/Logs/LocalModels/loadbench-{time.strftime('%Y%m%d-%H%M')}.json")
    json.dump(dict(family=family, loaded=loaded, at=round(t0), n=n, max_tokens=max_tokens, minutes=round((time.time() - t0) / 60, 1), cells=out), open(path, 'w'), indent=1)
    print(f'\n{(time.time() - t0) / 60:.1f} minutes · report: {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
