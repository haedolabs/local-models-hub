#!/usr/bin/env python3
"""The sharing and keeping rules, tested in a sandbox (temporary folders, a pretend LM Studio, no model
ever loaded or unloaded): the recycle policy, the door's queue with people first, hold-and-resend across a crash, the
server-start guard, a partial model listing, a client that gives up, the agent engine's retries. `localmodels check` runs it;
so can you: python3 tests/test_rules.py
"""
import json, os, shutil, socket, stat, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _sandbox  # noqa: E402  (first: the state folder, port, LM Studio and lms point into a temporary folder)
from localmodels import crashes, llmshare, recycle

B, C2 = 'qwen/qwen3.6-35b-a3b', 'qwen/qwen3.6-35b-a3b:2'
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))


def sandbox(box):
    llmshare.use_state(box)                                   # every shared path, every module's: never this Mac's live state
    llmshare.api_models = lambda timeout=3: None              # no real LM Studio in the sandbox (a test that needs a listing sets one)
    crashes.LOGS = os.path.join(box, 'logs')                  # (the repo's default settings apply: there is no models.json in the box)
    os.makedirs(os.path.join(crashes.LOGS, '2026-11'))
    crashes._seen_state.update(at=0.0, st={})
    for p in (llmshare.REQUESTS, crashes.STATE, crashes.RECORD):
        if os.path.exists(p): os.remove(p)


def served(copy, n, written_each=2500, prompt_each=4000, since=None, hours=2.0):
    """n request records on a copy, spread over the last `hours`: 6,500 tokens each by default."""
    t0 = since or time.time() - hours * 3600
    with open(llmshare.REQUESTS, 'a') as f:
        for k in range(n):
            f.write(json.dumps(dict(at=round(t0 + 10 + k * (hours * 3600 - 20) / max(1, n), 1), project='proj', kind='batch', model=B, copy=copy, status=200,
                                    seconds=80, waited=0, prompt_tokens=prompt_each, completion_tokens=written_each)) + '\n')
    recycle._counts.update(at=0.0, by={})


def row(i, state='IDLE', context=65536): return dict(id=i, key=B, state=state, size='37.75 GB', context=context, parallel=4)


def reset(since):
    if os.path.exists(llmshare.REQUESTS): os.remove(llmshare.REQUESTS)
    recycle._counts.update(at=0.0, by={})
    with crashes._state() as st:
        st['seen'] = {B: dict(loaded=True, since=since), C2: dict(loaded=True, since=since)}
        st['recycle'] = dict(job=None, last={}, fresh={}, retry_after=0); st['down'] = {}


def test_recycle():
    print('the recycle policy (800,000 tokens processed since load: drain; 560,000 and idle: recycle at once)')
    real = llmshare.settings
    fam = {B: dict(key=B, copies=2, context=65536, parallel=4, recycle=dict(tokens=800000, written=0, soft=560000, before_batch=0.5))}
    llmshare.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    since = time.time() - 3 * 3600
    reset(since)
    rows, busy = [row(B), row(C2)], {llmshare._safe(B): 2, llmshare._safe(C2): 1}
    t('settings: off, partial, default', recycle.thresholds(dict(recycle=False)) is None and recycle.thresholds(dict(recycle=dict(tokens=50)))['soft'] == 220000
      and recycle.thresholds({})['tokens'] == 300000 and recycle.thresholds({}).get('requests') == 0)
    served(B, 40); served(C2, 30)                              # 260,000 and 195,000 tokens
    t('below the band: nothing to do', recycle.plan(rows, busy) is None and not recycle.draining())
    v = recycle.view(rows)
    t('the view counts each copy since its load', v['copies'][B]['requests'] == 40 and v['copies'][B]['tokens'] == 260000 and v['copies'][C2]['written'] == 75000
      and v['copies'][B]['band']['tokens'] == 800000 and v['copies'][B]['share'] == 0.33, v['copies'][B])
    served(C2, 100)                                             # copy 2: 130 requests, 845,000 tokens
    w = recycle.plan(rows, busy)
    j = recycle.job()
    t('copy 2 past 800,000 tokens: it drains', j and j['copy'] == C2 and j['state'] == 'draining' and recycle.draining() == {C2}, w)
    served(B, 100)                                              # copy 1 past the band too
    t('one recycle at a time', recycle.plan(rows, busy) is None and recycle.job()['copy'] == C2)
    jj, rdy = recycle.ready(rows, busy)
    t('busy: not ready', jj and not rdy)
    jj, rdy = recycle.ready([row(B), row(C2, 'GENERATING')], {})
    t('LM Studio still generating: not ready', not rdy)
    jj, rdy = recycle.ready(rows, {})
    t('nothing in flight and idle: ready', rdy)
    recycle.reloading(C2)
    t('reloading: the door keeps leaving it out', recycle.job()['state'] == 'reloading' and recycle.draining() == {C2})
    recycle.done(C2, True, 12.5)
    st = crashes._read(fresh=True)
    rec = recycle.recent(1)
    t('back: the job is over, the count starts again, the record says what it had done',
      recycle.job() is None and st['seen'][C2]['since'] > since + 1 and rec and rec[0]['event'] == 'recycled' and rec[0]['requests'] == 130 and rec[0]['tokens'] == 845000, rec and rec[0])
    t('two minutes between two recycles of a family', recycle.plan(rows, busy) is None)
    with crashes._state() as st: st['recycle']['last'][B] = time.time() - 200
    recycle.plan(rows, busy)
    t('then copy 1, which is past the band too', recycle.job() and recycle.job()['copy'] == B)
    recycle.done(B, False, 3.0, 'load failed')
    st = crashes._read(fresh=True)
    t('a recycle whose load fails hands the copy to the crash machinery: the door holds', recycle.job() is None and crashes.reloading(B) and st['down'][B]['models'] == [B])
    reset(since); served(C2, 130, since=time.time() - 60)       # past the band again
    t('a copy missing: the keeper does its other job first', recycle.plan([row(C2)], busy) is None)
    with crashes._state() as st: st['down'] = {B: dict(crashed=time.time(), models=[B], state='loading again')}
    t('the family down after a crash: no recycle', recycle.plan(rows, busy) is None)
    reset(since); served(B, 70); served(C2, 20)                 # copy 1 at 455,000: 57% of its band
    t('57% of the band: not due', recycle.plan(rows, busy) is None)
    t('a batch starts: fresh start asked', recycle.fresh_start(B) == B and recycle.fresh_start('other/model') is None)
    recycle.plan(rows, busy)
    j = recycle.job()
    t('then the copy past half its band is recycled first', j and j['copy'] == B and 'batch starting' in j['why'], j and j['why'])
    with crashes._state() as st: st['recycle']['job']['since'] = time.time() - 700
    w = recycle.give_up_if_stuck()
    t('a drain stuck for 10 minutes is given up and tried later', recycle.job() is None and crashes._read(fresh=True)['recycle']['retry_after'] > time.time() and 'deferred' in (w or ''), w)
    t('meanwhile nothing is planned', recycle.plan(rows, busy) is None)
    reset(since); served(C2, 90)                                # 585,000: past the soft limit
    t('past the soft limit but busy: left alone', recycle.plan(rows, busy) is None)
    recycle.plan(rows, {})
    j = recycle.job()
    t('past the soft limit and idle: recycled now', j and j['copy'] == C2 and 'idle' in j['why'] and recycle.ready(rows, {})[1], j and j['why'])
    reset(since)
    recycle.plan([row(B), row(C2, context=4096)], {})
    j = recycle.job()
    t("a copy loaded with other settings (LM Studio's own loading) is recycled to the family's", j and j['copy'] == C2 and 'settings' in j['why'], j and j['why'])

    print('the written count: long answers reach their threshold by tokens written first')
    fam[B]['recycle'] = dict(tokens=300000, written=150000, soft=220000, before_batch=0.5)
    reset(since); served(C2, 32, written_each=5000, prompt_each=1500)     # 208,000 processed (69%), 160,000 written (107%)
    v = recycle.view(rows)['copies'][C2]
    t('the view says the written count leads, against its own threshold', v['by'] == 'written' and v['share'] == 1.07 and v['band']['written'] == 150000, v)
    w = recycle.plan(rows, busy); j = recycle.job()
    t('past 150,000 written though under 300,000 processed: it drains, and the words say both counts', j and j['copy'] == C2 and '160,000 written' in j['why']
      and 'or 150,000 written' in j['why'], j and j['why'])
    reset(since); served(C2, 23, written_each=5000, prompt_each=1500)     # 115,000 written: 77%, past the soft share (73%)
    recycle.plan(rows, {}); j = recycle.job()
    t('idle past the soft share of the written threshold: recycled now', j and j['copy'] == C2 and 'idle at 76%' in j['why'], j and j['why'])
    t('the default counts tokens written', recycle.thresholds({})['written'] == 150000 and recycle.thresholds(dict(recycle=dict(written=0)))['written'] == 0)

    print('prompt-heavy work: the context in flight counts toward the threshold, so the drain starts before it is crossed')
    reset(since); served(C2, 37, written_each=500, prompt_each=6000)       # 240,500 processed (80%), 18,500 written
    t('80% of the band and busy, nothing in flight counted: not due', recycle.plan(rows, busy, flying={}) is None)
    w = recycle.plan(rows, busy, flying={C2: 70000}); j = recycle.job()
    t('80% plus 70,000 tokens in flight: it drains now, and says the requests in flight carried it there',
      j and j['copy'] == C2 and 'with the requests in flight' in j['why'] and '~70,000 tokens in flight' in j['why'], j and j['why'])
    reset(since); served(C2, 37, written_each=500, prompt_each=6000)
    os.makedirs(llmshare.INFLIGHT, exist_ok=True)
    marks = [os.path.join(llmshare.INFLIGHT, f'{os.getpid()}-t{k}.json') for k in range(3)]
    for p, (state, ctx) in zip(marks, (('answering', 40000), ('answering', 30000), ('queued', 90000))):
        llmshare._write(p, dict(hub_pid=os.getpid(), since=time.time(), state=state, copy=C2, ctx=ctx))
    recycle.plan(rows, busy); j = recycle.job()
    t("...read from the gateway's in-flight markers by default (answering requests only)", j and j['copy'] == C2 and '~70,000 tokens in flight' in j['why'], j and j['why'])
    for p in marks: os.remove(p)
    reset(since); served(C2, 50, written_each=500, prompt_each=6000)       # 325,000: past the threshold
    with crashes._state() as st: st['recycle']['last'][B] = time.time() - 10
    t('a copy past its threshold waits GAP_HARD (30 s) after its sibling\'s recycle', recycle.plan(rows, busy, flying={}) is None)
    with crashes._state() as st: st['recycle']['last'][B] = time.time() - 40
    recycle.plan(rows, busy, flying={}); j = recycle.job()
    t('...then drains, though two minutes have not passed (its sibling took every request while it drained)', j and j['copy'] == C2, j)
    reset(since); served(C2, 36, written_each=500, prompt_each=6000)       # 234,000: 78%, past the soft share, idle
    with crashes._state() as st: st['recycle']['last'][B] = time.time() - 40
    t('a copy only past the soft share still waits the two minutes', recycle.plan(rows, {}, flying={}) is None)
    with crashes._state() as st: st['recycle']['last'][B] = time.time() - 200
    recycle.plan(rows, {}, flying={}); j = recycle.job()
    t('...and is recycled idle after them', j and j['copy'] == C2 and 'idle' in j['why'], j and j['why'])
    llmshare.settings = real


def test_admission(box):
    print("admission by context size: a request's size, an instance's budget, who fits")
    from localmodels import admission, models
    real = llmshare.settings, admission.MODELS_DIR, admission.free_gib, llmshare.requests_log
    fam = {B: dict(key=B, copies=2, context=65536, parallel=4, recycle=False)}
    llmshare.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    admission._ratio.update(at=time.time() + 3600, v=3.0)
    t('a request: its prompt from its bytes (3 a token here), and its answer up to 4,096 counted',
      admission.estimate(b'x' * 30000, dict(max_tokens=1000)) == 11000 and admission.estimate(b'x' * 30000, {}) == 14096
      and admission.estimate(b'x' * 30000, dict(max_tokens=20000)) == 14096 and admission.estimate(b'x' * 30000, dict(max_completion_tokens=500)) == 10500,
      [admission.estimate(b'x' * 30000, j) for j in (dict(max_tokens=1000), {}, dict(max_tokens=20000))])
    t('...at most one context window; an embedding writes nothing', admission.estimate(b'x' * 300000, {}, 65536) == 65536
      and admission.estimate(b'x' * 30000, {}, 0, answer=False) == 10000)
    img = b'{"content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,' + b'A' * 500000 + b'"}}]}'
    t('an image sent inline counts what a model reads of it, not its encoding', admission.prompt_bytes(img) < 4200, admission.prompt_bytes(img))
    rows_ = [dict(at=time.time() - 60, status=200, prompt_tokens=10000, bytes=32000)] * 20 + [dict(at=time.time() - 60, status=200, prompt_tokens=10000, bytes=44000)] * 20
    llmshare.requests_log = lambda hours=1.0, tail=0: list(rows_)
    admission._ratio.update(at=0.0)
    r1 = admission.ratio()
    llmshare.requests_log = lambda hours=1.0, tail=0: list(rows_[:15])
    admission._ratio.update(at=0.0)
    r2 = admission.ratio()
    t("bytes per token from this Mac's own requests: the lower quartile, so estimates err high; 3.0 until 20 are on record", r1 == 3.2 and r2 == 3.0, (r1, r2))
    llmshare.requests_log = real[3]
    admission._ratio.update(at=time.time() + 3600, v=3.0)

    qwen = dict(text_config=dict(num_hidden_layers=40, num_attention_heads=16, num_key_value_heads=2, head_dim=256, hidden_size=2048,
                                 layer_types=['linear_attention'] * 3 + ['full_attention'] + ['linear_attention'] * 3 + ['full_attention']
                                 + (['linear_attention'] * 3 + ['full_attention']) * 8))
    dense = dict(num_hidden_layers=80, num_attention_heads=64, num_key_value_heads=8, hidden_size=8192)
    t("a model's memory per token from its config: KV cache in its full-attention layers, attention scores while it reads",
      admission.shape(qwen) == (20480, 65536) and admission.shape(dense) == (327680, 262144) and admission.shape({}) is None,
      (admission.shape(qwen), admission.shape(dense)))
    d = os.path.join(box, 'lms-models', 'lmstudio-community', 'Qwen3.6-35B-A3B-MLX-8bit'); os.makedirs(d)
    json.dump(qwen, open(os.path.join(d, 'config.json'), 'w'))
    admission.MODELS_DIR = os.path.join(box, 'lms-models'); admission._shapes.clear()
    pt, alone = admission.per_token(dict(id=B, key=B, variant=B + '@8bit')), admission.per_token(dict(id=B, key=B, variant=B + '@8bit'), False)
    t('...found by the model and its loaded variant under LM Studio\'s models folder, times the cache share beside a sibling; a model it cannot find counts PER_TOKEN',
      pt == int((20480 + 65536) * admission.CACHE_SHARE) and alone == 20480 + 65536 and admission.per_token(dict(id='x/other', key='x/other')) == admission.PER_TOKEN, (pt, alone))
    two = [dict(row(B), variant=B + '@8bit', size='35.16 GB'), dict(row(C2), variant=B + '@8bit', size='35.16 GB')]
    want = int((((128 - 22.4 - 8) / 2) - 35.16 - admission.KEPT) * 2**30 / pt)
    b2 = admission.default_budget(C2, two, memory=(128.0, 22.4, 107.5))
    t('the M5 Max: each of two instances gets its share of memory less other programs and headroom, less its weights and the cache it keeps',
      b2 == want and 90000 < b2 < 100000, (b2, want))
    fam[B]['copies'] = 1
    llmshare.settings = lambda: dict(families=fam, batch_at_once=4, headroom_gb=4)
    one = two[:1]
    want1 = int((min(48 - 5.4 - 4, 40 - admission.OTHER_GPU) - 35.16) * 2**30 / alone)
    b1 = admission.default_budget(B, one, memory=(48.0, 5.4, 40.0))
    t('the M4 Pro peer: one instance, the lesser of memory and the GPU limit, no sibling\'s cache to count', b1 == want1 and 30000 < b1 < 40000, (b1, want1))
    t('...never below LEAST, never above every slot\'s window', admission.default_budget(B, one, memory=(40.0, 5.4, 36.0)) == admission.LEAST
      and admission.default_budget(B, one, memory=(1024.0, 5.4, 900.0)) == 65536 * 4)
    fam[B]['copies'] = 2
    llmshare.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    fam[B]['admit'] = dict(tokens=50000); admission._budgets.clear()
    t('"admit": {"tokens": N} sets it; "admit": false turns it off; a peer\'s instance has none here',
      admission.budget(C2) == 50000 and (fam[B].update(admit=False) or admission.budget(C2) == 0) and admission.budget(B + ' on macbook') == 0)
    fam[B]['admit'] = dict(tokens=50000)
    free = {'v': 30.0}
    admission.free_gib = lambda: free['v']
    t('an instance with nothing in flight takes any request; a busy one, while its load and the request fit the budget',
      admission.fits(C2, 0, 60000) and admission.fits(C2, 30000, 20000) and not admission.fits(C2, 30001, 20000))
    free['v'] = 12.0
    real_listed, llmshare.listed = llmshare.listed, (lambda: list(two))      # two instances: each counts the sibling-shared cost
    g20, g40 = admission.need_gib(C2, 20000), admission.need_gib(C2, 40000)
    t('a busy instance takes a request while free memory, less what the request takes at its model\'s cost a token, stays above headroom_gb',
      admission.fits(C2, 10000, 20000) and not admission.fits(C2, 10000, 40000) and admission.fits(C2, 0, 40000)
      and 2.2 < g20 < 2.3 and 12 - g40 < 8 < 12 - g20, (round(g20, 2), round(g40, 2)))
    w = admission.why_waiting([C2], {C2: 10000}, 40000)
    t('...and says so', 'memory headroom: 12.0 GiB free' in (w or '') and '~4.5 GiB for this request' in w and 'headroom_gb 8' in w, w)
    llmshare.listed = lambda: list(two[:1]); free['v'] = 2.0                # one instance: its engine reclaims its own cache
    t('an instance alone on its Mac is bounded by its budget alone, whatever free memory says (its engine fills it with cache)',
      admission.fits(B, 10000, 40000) and not admission.fits(B, 10001, 40000) and admission.why_waiting([B], {B: 10000}, 40000) is None)
    llmshare.listed = real_listed
    marks = [dict(copy=C2, state='answering', ctx=7000), dict(copy=C2, state='answering', ctx=3000), dict(copy=C2, state='queued', ctx=9000), dict(copy=B, state='answering')]
    t('the context in flight per instance, from the in-flight markers: answering requests only', admission.in_flight(marks) == {C2: 10000}, admission.in_flight(marks))
    llmshare.settings, admission.MODELS_DIR, admission.free_gib, _ = real
    admission._ratio.update(at=0.0, v=admission.RATIO); admission._budgets.clear(); admission._shapes.clear()


def test_listing(box):
    print('the model listing: lms ps --json, the text table, a slow or failed read')
    import stat
    from localmodels import llmshare as L
    script, data = os.path.join(box, 'lms'), os.path.join(box, 'ps.out')
    open(script, 'w').write('#!/bin/sh\nsleep "$(cat ' + box + '/sleep 2>/dev/null || echo 0)"\ncat ' + data + '\nexit "$(cat ' + box + '/rc 2>/dev/null || echo 0)"\n')
    os.chmod(script, stat.S_IRWXU)
    real = L.LMS, L.PS_TIMEOUT, dict(L._ps)
    L.LMS, L.PS_TIMEOUT = script, 1
    L._ps.update(at=0.0, rows=[], ok=False, good_at=0.0, tried=0.0)
    js = [dict(identifier=B, modelKey=B, status='processingPrompt', sizeBytes=40 * 2**30, contextLength=65536, parallel=4, queued=1, type='llm'),
          dict(identifier=C2, modelKey=B, status='idle', sizeBytes=40 * 2**30, contextLength=65536, parallel=4, queued=0, type='llm')]
    open(data, 'w').write(json.dumps(js))
    rows = L.loaded(fresh=True, force=True)
    t('JSON: every instance listed whatever its status, with its slots', [r['id'] for r in rows] == [B, C2] and rows[0]['state'] == 'PROCESSINGPROMPT' and rows[0]['parallel'] == 4
      and L.read_ok() and L.copies(B) == [B, C2] and L.at_once(B) == 8, rows)
    real_api = L.api_models
    try:
        open(data, 'w').write(json.dumps(js[:1]))                      # a partial listing under load: only the first instance
        L.api_models = lambda timeout=3: {B: 65536, C2: 65536}           # ...while LM Studio's server still holds both
        part = L.loaded(fresh=True, force=True)
        t('a partial lms ps never takes slots away: an instance LM Studio still lists keeps its place', [r['id'] for r in part] == [B, C2], part)
        L.api_models = lambda timeout=3: {B: 65536}                      # the server agrees it is gone: it goes at once
        gone = L.loaded(fresh=True, force=True)
        t('an instance the server no longer lists drops at once', [r['id'] for r in gone] == [B], gone)
        open(data, 'w').write(json.dumps(js))
        L.loaded(fresh=True, force=True)
    finally: L.api_models = real_api
    open(data, 'w').write('IDENTIFIER   MODEL   STATUS   SIZE   CONTEXT   PARALLEL   LOAD CONFIG   DEVICE   TTL\n'
                          f'{B}   {B}   PROCESSINGPROMPT   37.75 GB   65536   4   LM Studio   Local\n{C2}   {B}   IDLE   37.75 GB   65536   4   LM Studio   Local\n')
    rows = L.loaded(fresh=True, force=True)
    t('the text table (an older lms): the same, any upper-case status kept', [r['id'] for r in rows] == [B, C2] and rows[0]['parallel'] == 4 and L.read_ok(), rows)
    open(box + '/sleep', 'w').write('3')
    t0 = time.time(); rows = L.loaded(fresh=True, force=True)
    t('a listing that hangs: the last good one stands, marked unsure', [r['id'] for r in rows] == [B, C2] and not L.read_ok() and L.listing_age() < 60 and time.time() - t0 < 5)
    open(box + '/sleep', 'w').write('0'); open(box + '/rc', 'w').write('1')
    rows = L.loaded(fresh=True, force=True)
    t('a listing that fails: the same', [r['id'] for r in rows] == [B, C2] and not L.read_ok())
    open(box + '/rc', 'w').write('0'); open(data, 'w').write(json.dumps(js[:1]))
    rows = L.loaded(fresh=True, force=True)
    t('a good listing again: taken', [r['id'] for r in rows] == [B] and L.read_ok())
    t('between looks nobody waits: a thread that is not asking for a fresh look gets the cache at once', L.loaded() is not None and time.time() - t0 < 20)
    L.LMS, L.PS_TIMEOUT = real[0], real[1]; L._ps.update(real[2])


def test_unload(box):
    print("unloading one instance: by its exact id through LM Studio's REST API; the command only where that API is missing")
    import http.server, threading
    from localmodels import llmshare as L, models

    class Fake(http.server.BaseHTTPRequestHandler):
        mode, ids, seen = 'exact', [C2, B], []                # LM Studio lists the second replica first (seen on this hardware)

        def log_message(self, *a): pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
            Fake.seen.append((self.path, body.get('instance_id')))
            if self.path != '/api/v1/models/unload' or Fake.mode == 'missing':      # an LM Studio without the API answers 200 with an error
                out, code = dict(error=f'Unexpected endpoint or method. (POST {self.path})'), 200
            else:
                i = body.get('instance_id')
                hit = next((x for x in Fake.ids if L.family(x) == i), None) if Fake.mode == 'by key' else (i if i in Fake.ids else None)
                if hit: Fake.ids.remove(hit); out, code = dict(instance_id=i), 200
                else: out, code = dict(error=dict(type='model_not_found', message=f'no instance {i}')), 404
            data = json.dumps(out).encode()
            self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake); threading.Thread(target=srv.serve_forever, daemon=True).start()
    real, cmd = (L.LMSTUDIO, L.api_models, models._lms), []
    L.LMSTUDIO = f'http://127.0.0.1:{srv.server_address[1]}'
    L.api_models = lambda timeout=3: {i: 65536 for i in Fake.ids}
    models._lms = lambda *a, **k: (cmd.append(a), (True, 'ok'))[1]
    try:
        ok_, words = models.unload(B)
        t('by its exact id through the REST API: the first replica goes, the second stays, no command run',
          ok_ and Fake.seen[-1] == ('/api/v1/models/unload', B) and Fake.ids == [C2] and not cmd, (Fake.seen, Fake.ids, cmd))
        ok_, words = models.unload(B)
        t('an instance that is not loaded: said so, and the command is not tried', not ok_ and 'not loaded' in words and not cmd, (words, cmd))
        Fake.ids[:] = [C2, B]; Fake.mode = 'by key'
        ok_, words, gone = models.unload_exactly(B, wait=2)
        t('a server that resolves the name by model key takes the first in its list: seen at once (the second replica went)', gone == [C2] and B in Fake.ids, (gone, Fake.ids))
        Fake.ids[:] = [C2, B]; Fake.mode = 'missing'
        ok_, words = models.unload(B)
        t('an LM Studio without the REST API: the command, as before', cmd == [('unload', B)], cmd)
    finally:
        L.LMSTUDIO, L.api_models, models._lms = real; srv.shutdown(); srv.server_close()


def test_server_probe(box):
    print("LM Studio's server is started only when a connection is refused")
    import http.server, threading
    from localmodels import llmshare as L, models
    class Slow(http.server.BaseHTTPRequestHandler):
        delay = 0.0
        def log_message(self, *a): pass
        def do_GET(self):
            time.sleep(Slow.delay); self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'[]')
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Slow); threading.Thread(target=srv.serve_forever, daemon=True).start()
    script, log = os.path.join(box, 'lms-probe'), os.path.join(box, 'lms-probe.log')
    open(script, 'w').write(f'#!/bin/sh\necho "$@" >> {log}\n'); os.chmod(script, stat.S_IRWXU)
    calls = lambda: [x for x in open(log).read().split('\n') if x] if os.path.exists(log) else []
    real = L.LMSTUDIO, L.LMS, time.sleep
    L.LMS, L.LMSTUDIO = script, f'http://127.0.0.1:{srv.server_address[1]}'
    t('the server answers: no start', models.ensure_server() and not calls())
    Slow.delay = 4.0
    t('the server is slow (busy): no start', models.ensure_server() and not calls())
    Slow.delay = 0.0; srv.shutdown(); port = srv.server_address[1]; srv.server_close()
    L.LMSTUDIO = f'http://127.0.0.1:{port}'
    time.sleep = lambda s: None
    try: up = models.ensure_server()
    finally: time.sleep = real[2]
    t('the connection is refused: one start, and the truth that it is still down', not up and calls() == ['server start'], calls())
    L.LMSTUDIO, L.LMS = real[0], real[1]


def test_keep(box):
    print('the keeper in the sandbox: missing copies, the recycle, a crash during a recycle')
    from localmodels import llmshare as L, models
    real = L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, crashes.MISS, L.SWAP_LOCK, L.read_ok
    L.read_ok = lambda: True
    fam = {B: dict(key=B, copies=2, context=65536, parallel=4, recycle=dict(tokens=800000, soft=560000, before_batch=0.5))}
    L.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    L.SWAP_LOCK = os.path.join(box, 'swap.lock'); crashes.MISS = 0.3
    listings, api, busy = [[row(B), row(C2)]], {'ids': {B: 65536, C2: 65536}}, {'held': {}}
    L.loaded = lambda fresh=False, force=False: list(listings[-1] if len(listings) == 1 else listings.pop(0))
    L.api_models = lambda timeout=3: api['ids']
    L.busy_slots = lambda: dict(busy['held'])
    loads, unloads = [], []
    def fake_load(key, ident, context, parallel):          # a pretend LM Studio: what is loaded and unloaded shows in its listing
        loads.append(ident); crashes.loaded_now([ident]); api['ids'] = dict(api['ids'], **{ident: 65536}); return True, 'ok'

    def fake_unload(ident, takes=None):                    # takes: the instance LM Studio unloads instead (a name resolved by model key)
        unloads.append(ident); api['ids'] = {k: v for k, v in api['ids'].items() if k != (takes or ident)}; return True, ''
    models.load, models.unload = fake_load, fake_unload
    models._lms, models.free_gb, models.ensure_server = (lambda *a, **k: (True, '')), (lambda: 100.0), (lambda *a, **k: True)
    reset(time.time() - 3600)
    t('a whole listing: nothing to do', models.keep() == [] and not loads)
    listings[:] = [[row(B)]]; busy['held'] = {L._safe(C2): 1}
    models.keep(); time.sleep(0.4); models.keep()
    t('copy 2 missing from the listing but a request runs on it: no load', not loads)
    busy['held'] = {}
    models.keep(); time.sleep(0.4); models.keep()
    t("copy 2 missing but LM Studio's own server lists it: no load", not loads)
    api['ids'] = {B: 65536}
    w1 = models.keep(); time.sleep(0.4); w2 = models.keep()
    t('copy 2 gone for sure, two looks apart: loaded once', loads == [C2] and any('loaded' in w for w in w2), (loads, w1, w2))
    listings[:] = [[row(B), row(C2)]]; api['ids'] = {B: 65536, C2: 65536}; loads.clear()
    reset(time.time() - 3 * 3600); served(C2, 130)             # past the band, idle
    words = models.keep()
    t('a copy past its band and idle: drained and recycled in one pass', unloads == [C2] and loads == [C2] and recycle.job() is None and any('recycled' in w for w in words), (unloads, loads, words))
    t('its count starts again', crashes._read(fresh=True)['seen'][C2]['since'] > time.time() - 5)
    unloads.clear(); loads.clear(); reset(time.time() - 3 * 3600); served(C2, 130)
    with crashes._state() as st: st['down'] = {B: dict(crashed=time.time() - 10, models=[B], state='loading again', keeper=True)}
    listings[:] = [[row(C2)]]
    words = models.keep()
    t('a family down after a crash: its reload first, no recycle in the same pass', loads == [B] and not unloads and recycle.job() is None, (loads, unloads, words))

    with crashes._state() as st: st['down'] = {}
    unloads.clear(); loads.clear(); reset(time.time() - 3 * 3600); served(B, 130)          # copy 1 past the band, idle
    listings[:] = [[row(B), row(C2)]]; api['ids'] = {B: 65536, C2: 65536}
    models.unload = lambda ident: fake_unload(ident, takes=C2)                           # LM Studio takes copy 2 instead
    counted = lambda: len([r for r in crashes.records(1) if r.get('event') in ('crash', 'recycled')])
    n0 = counted(); words = models.keep()
    rec = [r for r in crashes.records(1) if r.get('event') == 'recycle given up']
    t('an unload that took the other replica: that one is loaded again at once, the drained one keeps serving, the recycle is tried later',
      unloads == [B] and loads == [C2] and recycle.job() is None and set(api['ids']) == {B, C2} and crashes._read(fresh=True)['recycle']['retry_after'] > time.time()
      and rec and f'unloaded {C2} when asked to unload {B}' in rec[-1].get('why', ''), (unloads, loads, words, rec[-1:]))
    t('...counted neither as a crash nor as a recycle', counted() == n0, (n0, counted()))
    models.unload = fake_unload

    unloads.clear(); loads.clear(); reset(time.time() - 3 * 3600); served(B, 130)          # copy 1 due, but copy 2 is the older instance
    listings[:] = [[row(C2), row(B)]]; api['ids'] = {C2: 65536, B: 65536}
    words = models.keep()
    rec = [r for r in crashes.records(1) if r.get('event') == 'recycled']
    t("copy 1 due while copy 2 is older (LM Studio unloads the oldest by copy 1's name): copy 2 is recycled first, and says why",
      unloads == [C2] and loads == [C2] and rec and rec[-1]['model'] == C2 and 'so that ' + B + ' becomes the oldest' in (rec[-1].get('why') or ''), (unloads, words))
    with crashes._state() as st: st['recycle']['last'][B] = time.time() - 200
    unloads.clear(); loads.clear(); listings[:] = [[row(B), row(C2)]]
    words = models.keep()
    t('then copy 1, now the oldest: its own recycle unloads it', unloads == [B] and loads == [B] and recycle.job() is None, (unloads, words))

    unloads.clear(); loads.clear(); reset(time.time() - 3 * 3600); served(C2, 130)          # copy 2 draining, then it crashes
    with crashes._state() as st:
        st['recycle']['job'] = dict(copy=C2, family=B, state='draining', since=time.time() - 60, why='test', counts={}, share=1.2)
        st['down'] = {B: dict(crashed=time.time() - 10, models=[C2], state='loading again', keeper=True, gone=True)}
    listings[:] = [[row(B)]]; api['ids'] = {B: 65536}
    n0 = len([r for r in crashes.records(1) if r.get('event') == 'recycled'])
    words = models.keep()
    rec = [r for r in crashes.records(1) if r.get('event') == 'recycle given up']
    t('a copy that crashes while it drains: crash recovery reloads it, and that reload ends the drain (it is fresh)',
      loads == [C2] and recycle.job() is None and rec and 'crashed while draining' in rec[-1].get('why', '') and C2 not in recycle.draining(), (loads, words, rec[-1:]))
    listings[:] = [[row(B), row(C2)]]; api['ids'] = {B: 65536, C2: 65536}
    words = models.keep()
    t('...so it is not unloaded again for the old count, and no recycle is recorded for it',
      not unloads and len([r for r in crashes.records(1) if r.get('event') == 'recycled']) == n0, (unloads, words))
    L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, crashes.MISS, L.SWAP_LOCK, L.read_ok = real
    with crashes._state() as st: st['down'] = {}


def test_recycle_resumed(box):
    print('a recycle cut between its unload and its load (the gateway stopped): resumed with the load, not another unload')
    from localmodels import llmshare as L, models
    real = L.api_models, L.loaded, models.load, models.unload, models.ensure_server, L.settings
    api, loads, unloads = {'ids': {B: 65536}}, [], []
    L.api_models = lambda timeout=3: dict(api['ids'])
    L.loaded = lambda fresh=False, force=False: [row(i) for i in api['ids']]
    L.settings = lambda: dict(families={B: dict(key=B, copies=2, context=65536, parallel=4)}, batch_at_once=8, headroom_gb=8)
    models.load = lambda key, ident, context, parallel: (loads.append(ident), api['ids'].update({ident: context}), (True, 'ok'))[2]
    models.unload = lambda ident: (unloads.append(ident), api['ids'].pop(B, None), (True, ''))[2]     # a missing copy's name takes the key's instance
    models.ensure_server = lambda *a, **k: True
    try:
        reset(time.time() - 600)
        with crashes._state() as st: st['recycle'] = dict(job=dict(copy=C2, family=B, state='reloading', since=time.time() - 30, why='test', counts={}), last={}, fresh={}, retry_after=0)
        words = models._recycle(dict(copy=C2, family=B, why='test'), L.settings()['families'])
        t('its copy is not listed: it is loaded, and nothing is unloaded (by name, that would take its sibling)', unloads == [] and loads == [C2] and B in api['ids'], (unloads, loads, words))
    finally:
        L.api_models, L.loaded, models.load, models.unload, models.ensure_server, L.settings = real


def test_swap_away(box):
    print('a swap to another model: the family it unloads is not read as crashed, and is not loaded back')
    from localmodels import llmshare as L, models
    real = L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, crashes.MISS, L.SWAP_LOCK, L.read_ok
    O = 'other/model'
    L.read_ok = lambda: True
    L.settings = lambda: dict(families={B: dict(key=B, copies=2, context=65536, parallel=4, recycle=False)}, batch_at_once=8, headroom_gb=8)
    L.SWAP_LOCK = os.path.join(box, 'swap-away.lock'); crashes.MISS = 0.3
    api = {'ids': {B: 65536, C2: 65536}}
    L.api_models = lambda timeout=3: dict(api['ids'])
    L.loaded = lambda fresh=False, force=False: [dict(row(i), key=B if L.family(i) == B else i) for i in api['ids']]
    L.busy_slots = lambda: {}
    loads = []
    models.load = lambda key, ident, context, parallel: (loads.append(ident), crashes.loaded_now([ident]), api['ids'].update({ident: context}), (True, 'ok'))[3]
    models.unload = lambda ident: (api['ids'].pop(ident, None), (True, f'{ident} unloaded'))[1]   # the REST API: LM Studio's log says only "Received request"
    models._lms, models.free_gb, models.ensure_server = (lambda *a, **k: (True, '')), (lambda: 100.0), (lambda *a, **k: True)
    ps_ok, L._ps['ok'] = L._ps['ok'], True                    # the listing above stands for a good `lms ps`
    try:
        reset(time.time() - 600)
        n0 = len([r for r in crashes.records(1) if r.get('event') == 'crash'])
        code, words = models.use(O, context=8192)
        did = models.keep(); time.sleep(0.4); did += models.keep(); time.sleep(0.4); did += models.keep()
        st = crashes._read(fresh=True)
        t('after `localmodels use other/model` no crash is recorded for the family it unloaded, and nothing loads it back',
          code == 0 and loads == [O] and len([r for r in crashes.records(1) if r.get('event') == 'crash']) == n0 and not st.get('down')
          and not st['seen'].get(B, {}).get('loaded') and not st['seen'].get(C2, {}).get('loaded'), (code, words, loads, did, st.get('down')))
    finally:
        L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, crashes.MISS, L.SWAP_LOCK, L.read_ok = real
        L._ps['ok'] = ps_ok


def test_prune(box):
    print('localmodels prune: old job folders and logs go, a running batch and recent work stay')
    from localmodels import cli
    root = os.path.join(box, 'agents'); proj = os.path.join(root, 'proj')
    old, new, live = (os.path.join(proj, n) for n in ('20261001-010101-batch', '20261106-010101-batch', '20261001-020202-batch'))
    for d in (old, new, live):
        os.makedirs(d); open(os.path.join(d, 'a.json'), 'w').write('{"answer": "x" }')
    json.dump(dict(pid=os.getpid()), open(os.path.join(live, 'runner.json'), 'w'))      # its runner (this process) is alive, no summary yet
    open(os.path.join(proj, 'old-ask.json'), 'w').write('{}'); open(os.path.join(proj, 'new-ask.json'), 'w').write('{}')
    ago40, ago2 = time.time() - 40 * 86400, time.time() - 2 * 86400
    for p in (old, live, os.path.join(proj, 'old-ask.json')): os.utime(p, (ago40, ago40))
    for p in (new, os.path.join(proj, 'new-ask.json')): os.utime(p, (ago2, ago2))
    f, n, b = cli.prune(30, root)
    t('one old folder and one old log removed, their bytes counted', (f, n) == (1, 1) and b > 0, (f, n, b))
    t('the recent folder, the recent log and the running batch kept', os.path.isdir(new) and os.path.isdir(live) and os.path.exists(os.path.join(proj, 'new-ask.json')) and not os.path.exists(old))
    for n, age in (('20261001-010101-1-ask.json', ago40), ('20261106-010101-1-ask.json', ago2)):    # job logs written straight under the root
        open(os.path.join(root, n), 'w').write('{}'); os.utime(os.path.join(root, n), (age, age))
    f, n, b = cli.prune(30, root)
    t('a job log written straight under the root is pruned by age too', n == 1 and not os.path.exists(os.path.join(root, '20261001-010101-1-ask.json'))
      and os.path.exists(os.path.join(root, '20261106-010101-1-ask.json')), (f, n))


def test_settings(box):
    print('the settings file: a bad one leaves the last good settings standing, and is said')
    from localmodels import llmshare as L
    real = L.MODELS, dict(L._conf)
    L.MODELS = os.path.join(box, 'models.json')
    open(L.MODELS, 'w').write(json.dumps(dict(families={}, batch_at_once=3, headroom_gb=8)))
    t('a good file is read', L.settings().get('batch_at_once') == 3 and L.settings_error() is None)
    open(L.MODELS, 'w').write('{"families": nonsense'); os.utime(L.MODELS, (time.time() + 2, time.time() + 2))
    t('a bad file: the last good settings stand and the error names the file', L.settings().get('batch_at_once') == 3 and L.settings_error() and 'models.json' in L.settings_error(), L.settings_error())
    open(L.MODELS, 'w').write(json.dumps(dict(families={}, batch_at_once=4, headroom_gb=8))); os.utime(L.MODELS, (time.time() + 4, time.time() + 4))
    t('fixed: read again, the error gone', L.settings().get('batch_at_once') == 4 and L.settings_error() is None)
    k = [6]
    def bad(d, key):
        open(L.MODELS, 'w').write(json.dumps(d)); k[0] += 2; os.utime(L.MODELS, (time.time() + k[0], time.time() + k[0]))
        return L.settings().get('batch_at_once') == 4 and key in (L.settings_error() or ''), L.settings_error()
    for d, key in ((dict(families={}, batch_at_once=5, headroom_gb='8GB'), 'headroom_gb'), (dict(families=[], batch_at_once=5), 'families'),
                   (dict(families={B: dict(key=B, copies='2')}, batch_at_once=5), 'copies'), (dict(families={B: dict(key=B, copies=0)}, batch_at_once=5), 'copies'),
                   (dict(families={B: dict(key=B, recycle='off')}, batch_at_once=5), 'recycle'), (dict(families={B: 'qwen'}, batch_at_once=5), B)):
        good, words = bad(d, key)
        t(f'a file of the wrong shape ({key}) is not taken: the last good settings stand, and the error names the key', good, words)
    print('the settings file: the last good copy carries a restart over a file that does not parse')
    open(L.MODELS, 'w').write(json.dumps(dict(families={}, batch_at_once=7, headroom_gb=8))); k[0] += 2; os.utime(L.MODELS, (time.time() + k[0], time.time() + k[0]))
    keep_was, L.KEEP_GOOD = L.KEEP_GOOD, True                      # the gateway's process keeps the last good copy
    t('the gateway reads a good file and keeps a copy of it', L.settings().get('batch_at_once') == 7 and os.path.exists(L.MODELS_GOOD), L.MODELS_GOOD)
    L.KEEP_GOOD = keep_was
    open(L.MODELS, 'w').write('{"families": nonsense'); k[0] += 2; os.utime(L.MODELS, (time.time() + k[0], time.time() + k[0]))
    L._conf.update(path=None, mtime=None, data={}, error=None)     # a new process: nothing read yet
    t('a new process whose first read fails runs on the last good copy, and says so', L.settings().get('batch_at_once') == 7 and 'last good copy' in (L.settings_error() or ''), L.settings_error())
    os.remove(L.MODELS_GOOD); L._conf.update(path=None, mtime=None, data={}, error=None)
    t('with no good copy either, it says it runs with none', L.settings() == {} and 'no valid settings' in (L.settings_error() or ''), L.settings_error())
    L.MODELS = real[0]; L._conf.update(real[1])


def test_write_problem(box):
    print("a record the hub cannot write is said everywhere, not dropped in silence (recycle counts would freeze)")
    blocked = os.path.join(box, 'not-a-folder'); open(blocked, 'w').write('a file where a folder should be')
    path = os.path.join(blocked, 'requests.jsonl')
    ok = llmshare._append(path, dict(at=1))
    words = llmshare.write_problem()
    t('a failed append returns False, and the words name the file and what is frozen', ok is False and words and 'requests.jsonl' in words and 'recycle' in words, (ok, words))
    good = llmshare._append(os.path.join(box, 'requests-ok.jsonl'), dict(at=1))
    t('the next append that works clears it', good is True and llmshare.write_problem() is None, llmshare.write_problem())


def test_cli_args():
    print('the command line: an unknown option, a help flag or a bad value runs nothing')
    import contextlib, io
    from localmodels import cli
    ran = []
    names = ('publish', 'uninstall', 'install', 'prune', 'door', 'demo_cmd', 'status')
    real = {n: getattr(cli, n) for n in names}
    for n in names: setattr(cli, n, lambda *a, n=n, **k: ran.append((n, a, k)) or 0)
    try:
        def run(*argv):
            ran.clear(); out = io.StringIO()
            with contextlib.redirect_stdout(out): code = cli.main(list(argv))
            return code, list(ran), out.getvalue()
        code, did, out = run('publish', '--dry-run')
        t('publish --dry-run is the dry run (the checks only), not a release', code == 0 and did and '--dry' in did[0][1][0], (code, did))
        for argv in (('publish', '--help'), ('uninstall', '--help'), ('install', '-h'), ('--help',), ('-h',)):
            code, did, out = run(*argv)
            t(f'{" ".join(argv)}: the help, nothing run', code == 0 and not did and 'localmodels publish' in out, (argv, code, did))
        for argv in (('install', '--bogus'), ('publish', '--version'), ('prune', '--days', '-1'), ('uninstall', '--force'), ('start', '--now')):
            code, did, out = run(*argv)
            t(f'{" ".join(argv)}: refused with exit 2, nothing run', code == 2 and not did, (argv, code, did, out[-120:]))
        code, did, out = run('install', '--wait=480')
        t('--wait=480 is read as --wait 480', code == 0 and did and did[0][1][0] == 480 * 60, did)
        from localmodels import demo                       # the demo starts one program per project: each one's command line is taken
        kids, real_popen, real_plan = [], demo.subprocess.Popen, demo.plan
        class Kid:
            returncode = 0
            def wait(self): return 0
        demo.subprocess.Popen = lambda argv, **k: kids.append(argv) or Kid()
        demo.plan = lambda minutes: ('test/family', 3, {p[0]: 1 for p in demo.PROJECTS})
        try: demo.spawn(minutes=1, say=lambda *a: None, seed=1)
        finally: demo.subprocess.Popen, demo.plan = real_popen, real_plan
        took = [run(*argv[argv.index('demo'):]) for argv in kids]
        t("the demo's own programs: the options it starts them with are taken (each runs, none refused)",
          kids and all(code == 0 and did and did[0][0] == 'demo_cmd' for code, did, out in took), [(c, d[:1], o[-100:]) for c, d, o in took])
    finally:
        for n, f in real.items(): setattr(cli, n, f)


def test_install_dry(box):
    print('install --dry says what it would do and writes nothing; check notices a missing lms')
    import contextlib, io
    from localmodels import cli, llmshare as L
    real = cli.PLIST, L.MODELS, L.LMS
    cli.PLIST, L.MODELS = os.path.join(box, 'agent.plist'), os.path.join(box, 'settings.json')
    out = io.StringIO()
    with contextlib.redirect_stdout(out): code = cli.install(dry=True)
    words = out.getvalue()
    t('a dry run: the plan in words, exit 0, nothing written', code == 0 and words.count('would') >= 4 and 'nothing written' in words and not os.path.exists(cli.PLIST) and not os.path.exists(L.MODELS), words[:120])
    out = io.StringIO()
    with contextlib.redirect_stdout(out): cli.install(dry=True, no_claude=True)
    t('--no-claude: no registration and no signpost line in the plan', 'register nothing' in out.getvalue())
    t('the plan names the Claude Code hook', 'hook' in words, words[-300:])
    cmd = 'python3 "/x y/bin/localmodels" notice'
    other = dict(hooks=dict(UserPromptSubmit=[dict(hooks=[dict(type='command', command='echo mine')])]), model='opus')
    once = cli.with_hook(json.loads(json.dumps(other)), cmd)
    twice = cli.with_hook(json.loads(json.dumps(once)), cmd)
    t("the hook is added to Claude Code's settings for SessionStart and UserPromptSubmit, beside the person's own, once",
      once == twice and once['model'] == 'opus' and any(h['command'] == 'echo mine' for e in once['hooks']['UserPromptSubmit'] for h in e['hooks'])
      and all(any(h['command'] == cmd for e in once['hooks'][ev] for h in e['hooks']) for ev in ('SessionStart', 'UserPromptSubmit')), once)
    gone = cli.with_hook(json.loads(json.dumps(once)), cmd, add=False)
    t('uninstall takes out only the hub\'s hook', gone['hooks'].get('UserPromptSubmit') == other['hooks']['UserPromptSubmit'] and 'SessionStart' not in gone['hooks'], gone)
    import subprocess
    real_inst, cli.INSTALLED = cli.INSTALLED, os.path.join(box, 'no-installed-copy')
    r = subprocess.run(['/bin/sh', '-c', cli.hook_command()], capture_output=True, text=True)
    cli.INSTALLED = real_inst
    t('the hook command exits 0 when the installed copy is missing (exit 2 would refuse every prompt in every session)', r.returncode == 0, (r.returncode, r.stderr[-200:]))
    new = cli.with_hook(json.loads(json.dumps(once)), cli.hook_command())
    t("an install replaces a hook written in the old form, beside the person's own",
      [h['command'] for e in new['hooks']['UserPromptSubmit'] for h in e['hooks']] == ['echo mine', cli.hook_command()], new['hooks']['UserPromptSubmit'])
    t("the plugin's hook command cannot fail either", all('|| true' in h['command'] for ev in json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hooks', 'hooks.json')))['hooks'].values()
                                                       for e in ev for h in e['hooks']))
    import inspect                                           # never run for real here: install and uninstall act on this Mac's LaunchAgent and Claude Code settings
    w = cli.sandboxed()
    t('a shell with LOCALMODELS_STATE set and no LOCALMODELS_LABEL (this sandbox) is named: install, uninstall, stop and restart would act on the real LaunchAgent',
      w and 'LOCALMODELS_LABEL' in w and 'LOCALMODELS_STATE' in w, w)
    first = {f.__name__: [l.strip() for l in inspect.getsource(f).split('\n')[1:] if l.strip() and not l.strip().startswith(('"""', '#'))][:2] for f in (cli.install, cli.uninstall, cli.door)}
    t('install, uninstall and door check it before they do anything', all(any('sandboxed()' in l for l in ls) for ls in first.values()), first)
    L.LMS = os.path.join(box, 'no-such-lms')
    t('a missing lms is noticed', cli.lms_found() is False)
    cli.PLIST, L.MODELS, L.LMS = real


def test_first_settings():
    print("the first settings fit this Mac: its memory and the models it has downloaded")
    from localmodels import models
    G = 2**30
    llm = lambda key, gb, ctx=131072: dict(type='llm', modelKey=key, sizeBytes=int(gb * G), maxContextLength=ctx)
    have = [llm(B, 37.75, 262144), llm('big-70b', 75), dict(type='embedding', modelKey='embed', sizeBytes=int(0.08 * G), maxContextLength=2048)]
    s = models.first_settings(have, 128)
    f = s['families'].get(B, {})
    t('128 GB with the measured model: the measured default stands (2 copies, 65,536, recycling)',
      list(s['families']) == [B] and (f.get('copies'), f.get('context'), f.get('parallel')) == (2, 65536, 4) and f.get('recycle'), f)
    s = models.first_settings(have, 64)
    f = s['families'].get(B, {})
    t('64 GB: one copy of it, since two do not fit', f.get('copies') == 1 and s['batch_at_once'] == f.get('parallel'), f)
    s = models.first_settings([llm('small-8b', 8.5, 32768), llm('mid-14b', 15)], 32)
    f = s['families'].get('mid-14b', {})
    t('32 GB without the measured model: the largest model that fits, with a context it allows',
      list(s['families']) == ['mid-14b'] and f.get('copies') == 1 and f.get('context') == 32768 and f.get('key') == 'mid-14b', s['families'])
    s = models.first_settings([llm('small-8b', 8.5, 16384)], 64)
    f = s['families'].get('small-8b', {})
    t("a model's own context limit is never passed", f.get('context') == 16384 and f.get('copies') == 2, f)
    s = models.first_settings([llm('big-70b', 75)], 16)
    t('nothing fits: no family, and the reason is said', s['families'] == {} and 'why' in s, s.get('why'))
    s = models.first_settings(None, 128)
    t('no list of models (no lms): no family, and the reason is said', s['families'] == {} and 'lms' in s.get('why', ''), s.get('why'))
    t('every first settings file reads back as JSON', all(json.loads(json.dumps(models.first_settings(h, m))) for h, m in ((have, 128), (None, 8))))


def test_runs():
    print("runs: a whole run's progress and time left, from what the program says and the speed measured on this Mac")
    from localmodels import runs
    now = 1_000_000.0

    def rec(at, seconds=60, status=200, project='proj', **kw):
        return dict(at=at, seconds=seconds, status=status, project=project, kind='batch', **kw)
    t('the step words "extract files 8 of 360" read as a phase, its item and its size', runs.parse_step('extract files 8 of 360') == ('extract files', 8, 360)
      and runs.parse_step('reading chapter 3') is None and runs.parse_step(None) is None)
    t('the run headers read whole, and nonsense is dropped', runs.from_headers({'X-Run': 'report', 'X-Run-Total': '420', 'X-Run-Done': '17'}) == dict(run='report', run_total=420, run_done=17)
      and runs.from_headers({'X-Run': 'report', 'X-Run-Total': 'lots', 'X-Run-Done': '-3'}) == dict(run='report') and runs.from_headers({}) == {})

    # a run that says its size: 100 units, 1 unit finished every 30 s for the last 20 minutes (40 done)
    rows = [rec(now - 1200 + 30 * k + 30, run='report', run_total=100, run_done=k, step=f'extract {k + 1} of 100') for k in range(40)]
    g = runs.progress(rows, now=now)
    r = g.get(('proj', 'report'), {})
    t('a run with headers: done and total as the program said, the percent', (r.get('done'), r.get('total'), r.get('pct')) == (39, 100, 39), r)
    t('its time left comes from the speed measured in this run: 61 units at 30 s each, about 30 min', r.get('left') and 1650 <= r['left'] <= 2000, r.get('left'))
    t("it starts when the run's first request began, not when the model was loaded", abs(r.get('started', 0) - (now - 1200 + 30 - 60)) < 1, r.get('started'))
    t('the phase it is in now comes from the step', r.get('phase') == 'extract', r.get('phase'))
    t('the finish time is now plus the time left (the dash shows it in local time)', r.get('finish') == now + r.get('left', -1), (r.get('finish'), r.get('left')))
    t('a run that names itself is the whole batch; a phase from step words is only that phase', r.get('scope') == 'whole run', r.get('scope'))

    # the batch and the job inside it: 150 units in all, the 100-item extract phase done, now 10 of 50 checks done
    two = [rec(now - 4000 + 30 * k, run='report', run_total=150, run_done=k, step=f'extract {k + 1} of 100') for k in range(100)] + \
          [rec(now - 600 + 60 * k, run='report', run_total=150, run_done=100 + k, step=f'check {k + 1} of 50') for k in range(10)]
    r = runs.progress(two, now=now)[('proj', 'report')]
    j = r.get('job') or {}
    t('a batch carries the job it is on now: its name, done and size', (j.get('name'), j.get('done'), j.get('total')) == ('check', 10, 50), j)
    t('the job has its own bar, time left and finish, from its own pace (40 checks at ~60 s, about 40 min)',
      j.get('pct') == 20 and j.get('left') and 2000 <= j['left'] <= 3000 and j.get('finish') == now + j['left'], j)
    t("the batch still runs to its own end: its time left is at least the job's",r.get('left') and r['left'] >= j['left'], (r.get('left'), j.get('left')))

    # twice as fast lately: the estimate follows the recent speed, not the run's average
    fast = [rec(now - 3600 + 60 * k + 60, run='r2', run_total=200, run_done=k) for k in range(40)] + \
           [rec(now - 1200 + 15 * k + 15, run='r2', run_total=200, run_done=40 + k) for k in range(80)]
    r = runs.progress(fast, now=now)[('proj', 'r2')]
    t('after a speed-up the estimate follows the recent speed (81 left at ~15 s, about 20 min, not 40)', 1000 <= r['left'] <= 1500, r['left'])

    # the same run name came earlier today and took 10 s a unit; a new run has just begun
    before = [rec(now - 7200 + 10 * k + 10, run='nightly', run_total=50, run_done=k) for k in range(50)]
    hist = runs.history(before)
    begun = [rec(now - 5, seconds=5, run='nightly', run_total=60, run_done=0)]
    r = runs.progress(before + begun, now=now, history=hist)[('proj', 'nightly')]
    t("a new run of the same name: a fresh start, not the old one's count", (r.get('done'), r.get('total')) == (0, 60), r)
    t('before its first unit is done, the estimate comes from the last run of that name on this Mac (60 x 10 s)', r.get('left') and 500 <= r['left'] <= 700 and r.get('basis') == 'the last run', r)
    again = [rec(now - 400 + 5 * k, seconds=5, run='nightly', run_total=60, run_done=k) for k in range(60)] + \
            [rec(now - 50 + 5 * k, seconds=5, run='nightly', run_total=60, run_done=k) for k in range(4)]
    r = runs.progress(again, now=now)[('proj', 'nightly')]
    t('the same run started again two minutes after it ended: its count drops back, so it is a new run (3 done, not 59)', r['done'] == 3, r['done'])
    r = runs.progress(begun, now=now, history={})[('proj', 'nightly')]
    t('no earlier run and nothing done yet: no made-up number, it says it is estimating', r.get('left') is None and r.get('finish') is None and r.get('basis') == 'estimating after the first one', r)

    # the same work on a slower Mac: the estimate is that Mac's own, nothing assumed
    slow = [rec(now - 1200 + 120 * k + 120, run='report', run_total=100, run_done=k) for k in range(10)]
    r = runs.progress(slow, now=now)[('proj', 'report')]
    t('a Mac four times slower gets an estimate four times longer, from its own measurements', r['left'] >= 4 * 1650, r['left'])

    # no headers: a step that says "N of M" still gives the phase's progress (8 at once, items finishing out of order)
    ph = []
    for k in range(24):
        n = k + 1
        ph.append(rec(now - 600 + 25 * k, seconds=200, step=f'extract files {n} of 360'))
    ph.append(rec(now - 30, seconds=30, status=503, step='extract files 25 of 360'))
    r = runs.progress(ph, now=now).get(('proj', 'extract files'), {})
    t('a phase from the step words alone: items answered count as done, a failed one does not', (r.get('done'), r.get('total'), r.get('kind')) == (24, 360, 'phase'), r)
    t('and it too gets a time left, from its own pace', r.get('left') and r['left'] > 0, r.get('left'))
    t('a phase says its estimate covers that phase only', r.get('scope') == 'this phase' and r.get('finish') == now + r['left'], (r.get('scope'), r.get('finish')))
    inf = [dict(project='proj', since=now - 3, run='report', run_total=100, run_done=41, step='extract 42 of 100')]
    r = runs.progress(rows, inflight=inf, now=now)[('proj', 'report')]
    t('a request still in flight moves the count on at once', r['done'] == 41, r['done'])
    old = [rec(now - 5 * 3600, run='gone', run_total=10, run_done=3)]
    t('a run nobody has touched for an hour is not shown as running', ('proj', 'gone') not in runs.progress(old, now=now))
    mixed = [rec(now - 900 + 30 * k, step=f'extract {k + 1} of 9') for k in range(3)] + \
            [rec(now - 600 + 30 * k, run='extract', run_total=9, run_done=3 + k) for k in range(3)]
    try: g, h, err = runs.progress(mixed, now=now), runs.history(mixed), None
    except Exception as e: g, h, err = {}, {}, e
    r = g.get(('proj', 'extract'), {})
    t('a phase from step words and a run (X-Run) of the same name are kept apart: no error, and the run is the one shown',
      err is None and r.get('kind') == 'run' and (r.get('done'), r.get('total')) == (5, 9), repr(err) if err else r)

    from localmodels import dash
    real = dash.live_runs
    dash.live_runs = lambda: {('lab', 'survey'): dict(kind='run', project='lab', name='survey', done=153, total=842, pct=18, started=100.0,
                                                         job=dict(name='extract files', done=105, total=360))}
    try:
        rows = [dict(project='lab', batch=dict(name='', done=105, total=360, pct=29, left=None, now=[]), job=None, since=200.0)]
        dash.attach_runs(rows)
    finally: dash.live_runs = real
    b, j = rows[0]['batch'], rows[0]['job']
    t("a step's own lease counts never hide the named run: the run (X-Run) is the batch, the step's phase the job",
      b['name'] == 'survey' and (b['done'], b['total']) == (153, 842) and (j['name'], j['total']) == ('extract files', 360) and rows[0]['since'] == 100.0, rows[0])
    from localmodels import errors
    dash.live_runs = lambda: 1 / 0
    rows = [dict(project='lab', batch=None, job=None, since=200.0)]
    try: dash.attach_runs(rows); err = None
    except Exception as e: err = e
    finally: dash.live_runs = real
    t('progress that cannot be read leaves the client rows as they are, and the error is recorded',
      err is None and rows[0]['since'] == 200.0 and any('run progress' in x['where'] for x in errors.recent(5)), repr(err) if err else errors.recent(5))

def test_maintenance(box):
    print('maintenance: announced, on, the keeper standing down, the models put back after, the notice for every session')
    from localmodels import llmshare as L, maint, models
    maint.FILE = os.path.join(box, 'maintenance.json')
    now = time.time()
    t('nothing announced: nothing said', maint.read() is None and maint.notice() == '' and maint.header(None) is None)
    maint.announce(start=now + 600, minutes=30, why='updating LM Studio')
    m = maint.read(now=now)
    t('announced for 10 minutes from now: the phase, its end, plain words with the local time',
      m['phase'] == 'announced' and m['end'] == m['start'] + 1800 and time.strftime('%H:%M', time.localtime(now + 600)) in maint.words(m, now=now)
      and 'updating LM Studio' in maint.words(m, now=now), maint.words(m, now=now))
    t('the header says the same for programs', maint.header(m, now=now).startswith('announced; starts=') and 'why=updating LM Studio' in maint.header(m, now=now), maint.header(m, now=now))
    t('the notice for Claude sessions names the time and what to do', 'maintenance' in maint.notice(now=now) and 'Retry-After' in maint.notice(now=now), maint.notice(now=now))
    m = maint.read(now=now + 700)
    t('at its start it is on, and Retry-After runs to its end', m['phase'] == 'on' and abs(maint.retry_after(m, now=now + 700) - 1700) <= 1, maint.retry_after(m, now=now + 700))
    t('running past its end: Retry-After a minute, and the words say it runs over', maint.retry_after(maint.read(now=now + 3000), now=now + 3000) == 60
      and 'over' in maint.words(maint.read(now=now + 3000), now=now + 3000), maint.words(maint.read(now=now + 3000), now=now + 3000))
    t('a reason with odd characters is cleaned for the header', '\n' not in maint.header(maint.announce(start=now, minutes=5, why='a\nb;c'), now=now))
    prev = maint.end()
    t('off: the record comes back (with what was loaded before), nothing is announced after', prev and maint.read() is None, prev)

    print('maintenance: the command line, read as a person means it')
    from localmodels import cli
    P = lambda s: cli.maintenance_args(s.split(), now=now)
    t('"in 2 hours" starts in 120 minutes; "now for 1.5h" lasts 90; units in words or letters',
      P('in 2 hours')[0] == now + 7200 and P('now for 1.5h')[2] == 90 and P('in 90 min for 2h')[0:3:2] == (now + 5400, 120), (P('in 2 hours'), P('now for 1.5h')))
    t('words of the reason are never read as times: "update now available", "restart after update"',
      P('at 23:59 for 30m update now available')[0] == cli._at('23:59') and P('at 23:59 for 30m update now available')[3] == 'update now available'
      and P('in 15m for 30m restart after update')[1] is False and P('in 15m for 30m restart after update')[3] == 'restart after update', P('in 15m for 30m restart after update'))
    t('"scheduled maintenance" keeps both words', P('now scheduled maintenance')[3] == 'scheduled maintenance', P('now scheduled maintenance'))
    bad = []
    for s in ('in soon', 'for ever now', 'in 2 fortnights', 'at 25:00'):
        try: P(s); bad.append(s)
        except ValueError: pass
    t('a time word whose value cannot be read is refused, nothing set', not bad, bad)

    print('maintenance after the batches: it starts when no batch has sent a request for 2 minutes')
    maint.announce(after_batches=True, minutes=20, why='reboot')
    t('waiting for the batches: phase says so, nothing refused yet', maint.read()['phase'] == 'waiting' and 'batch traffic drains' in maint.words(maint.read()))
    busy = [dict(kind='batch', since=now - 5)]
    t('a batch request in flight: it waits', maint.tick(busy, [], now=now) is False and maint.read()['phase'] == 'waiting')
    t('a batch answered a minute ago: it still waits', maint.tick([], [dict(kind='batch', at=now - 60)], now=now) is False)
    t('no batch for 2 minutes (a chat does not count): it starts, and runs its 20 minutes from then',
      maint.tick([dict(kind='chat', since=now - 1)], [dict(kind='batch', at=now - 130)], now=now) is True and maint.read(now=now)['phase'] == 'on'
      and abs(maint.read(now=now)['end'] - (now + 1200)) < 2, maint.read(now=now))
    maint.end()

    print('maintenance and the keeper: it stands down while on, and puts the models back after')
    real = L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, crashes.MISS, L.SWAP_LOCK, L.read_ok
    L.read_ok = lambda: True
    fam = {B: dict(key=B, copies=2, context=65536, parallel=4, recycle=False)}
    L.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    L.SWAP_LOCK = os.path.join(box, 'swap2.lock'); crashes.MISS = 0.3
    listing = [[row(B), row(C2)]]
    L.loaded = lambda fresh=False, force=False: list(listing[0])
    L.api_models = lambda timeout=3: {r['id']: 65536 for r in listing[0]}
    L.busy_slots = lambda: {}
    loads, starts = [], []
    models.load = lambda key, ident, context, parallel: (loads.append(ident), crashes.loaded_now([ident]), (True, 'ok'))[2]
    models.unload = lambda ident: (True, '')
    models._lms, models.free_gb = (lambda *a, **k: (True, '')), (lambda: 100.0)
    models.ensure_server = lambda *a, **k: starts.append(1) or True
    reset(time.time() - 600)
    crashes_before = sum(1 for l in open(crashes.RECORD) if '"crash"' in l) if os.path.exists(crashes.RECORD) else 0
    maint.announce(start=time.time() - 1, minutes=30, why='updating LM Studio')
    words = models.keep()
    t('on: the keeper notes what is loaded and stands down', maint.read()['before'] == [B] and any('maintenance' in w for w in words), (maint.read().get('before'), words))
    listing[0] = [row(B)]
    models.keep(); time.sleep(0.4); words = models.keep()
    t('on: a copy gone (LM Studio restarting) is not loaded again, and no server is started', not loads and not starts, (loads, words))
    listing[0] = []                                            # LM Studio came back with nothing loaded
    words = models.end_maintenance()
    t('off: the families loaded before are loaded again (first copy; the keeper adds the rest), the server started first',
      loads == [B] and starts and any('reloaded after maintenance' in w for w in words), (loads, words))
    crashes_after = sum(1 for l in open(crashes.RECORD) if '"crash"' in l) if os.path.exists(crashes.RECORD) else 0
    t('nothing that went away during maintenance is recorded as a crash', crashes_after == crashes_before, (crashes_before, crashes_after))
    L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, crashes.MISS, L.SWAP_LOCK, L.read_ok = real


def test_switch_off(box):
    print('switched off: requests refused, every instance unloaded once idle, nothing loaded or started, then on again')
    from localmodels import llmshare as L, maint, models
    maint.OFF, maint.SWITCHING = os.path.join(box, 'off.json'), os.path.join(box, 'switching.json')
    real = (L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server,
            models.server_up, crashes.MISS, L.SWAP_LOCK, L.read_ok, L.inflight, L.swap_blockers)
    L.read_ok = lambda: True
    fam = {B: dict(key=B, copies=2, context=65536, parallel=4, recycle=False)}
    L.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    L.SWAP_LOCK = os.path.join(box, 'swap3.lock'); crashes.MISS = 0.3
    api = {'ids': {B: 65536, C2: 65536}}
    L.loaded = lambda fresh=False, force=False: [row(i) for i in api['ids']]
    L.api_models = lambda timeout=3: dict(api['ids'])
    L.busy_slots = lambda: {}
    flight, held = [dict(kind='batch', since=time.time())], {'by': []}
    L.inflight = lambda: list(flight)
    L.swap_blockers = lambda ids: list(held['by'])
    loads, unloads, starts = [], [], []
    def fake_load(key, ident, context, parallel):
        loads.append(ident); crashes.loaded_now([ident]); api['ids'] = dict(api['ids'], **{ident: 65536}); return True, 'ok'
    def fake_unload(ident):                                   # LM Studio resolves a bare model key to its oldest instance
        unloads.append(ident); took = C2 if ident == B and C2 in api['ids'] else ident
        api['ids'] = {k: v for k, v in api['ids'].items() if k != took}; return True, ''
    models.load, models.unload = fake_load, fake_unload
    models._lms, models.free_gb, models.server_up = (lambda *a, **k: (True, '')), (lambda: 100.0), (lambda *a, **k: True)
    models.ensure_server = lambda *a, **k: starts.append(1) or True
    reset(time.time() - 600)
    crashes_before = sum(1 for l in open(crashes.RECORD) if '"crash"' in l) if os.path.exists(crashes.RECORD) else 0

    held['by'] = ['analysis holds a lease on qwen/qwen3.6-35b-a3b']
    code, body = models.switch_off()
    t('a run holds a lease: refused (409) and nothing changes', code == 409 and 'analysis' in body['error'] and maint.switched_off() is None, (code, body))
    code, body = models.switch_off(force=True)
    m = maint.read()
    t('forced: switched off, read as maintenance on with no end, 503 for 5 minutes, the header a front reads as maintenance',
      code == 200 and m['off'] and m['phase'] == 'on' and maint.on() and maint.retry_after(m) == maint.OFF_RETRY
      and maint.header(m).startswith('on') and 'switched off' in maint.words(m) and maint.read()['before'] == [B], (code, m))
    t('the Claude Code hook stays silent while switched off', maint.notice() == '')
    t('the screens see the switch-off under way, with the requests it waits for', maint.view()['inflight'] == 1 and not maint.view()['unloaded'], maint.view())
    words = models.keep()
    t('a request in flight: nothing unloaded yet, and it says so', not unloads and any('waiting for 1 request' in w for w in words), words)
    flight.clear()
    words = models.keep()
    t('idle: every instance unloaded (replica 2 first), seen gone from the listing, recorded done',
      unloads[0] == C2 and not api['ids'] and maint.switched_off().get('unloaded') and any('no instance listed' in w for w in words), (unloads, words))
    api['ids'] = {B: 65536}                                     # a person loads one by hand while switched off
    unloads.clear(); words = models.keep(); time.sleep(0.4); words = models.keep()
    t('then: a model loaded by hand is left alone, no replica is added, nothing started', not unloads and not loads and not starts, (unloads, loads, words))
    api['ids'] = {}
    models.keep(); time.sleep(0.4); words = models.keep()
    t('an instance that goes away while off is not reloaded as a crash', not loads and not starts, (loads, words))
    t('switching off twice keeps the first record', models.switch_off()[1].get('note') == 'already switched off')
    seen = {}
    def watch_load(key, ident, context, parallel):
        seen['during'] = maint.switching(); return fake_load(key, ident, context, parallel)
    models.load = watch_load
    words = models.switch_on()
    models.load = fake_load
    sw = maint.switching()
    t('the screens see the switch-on: loading (with its families) while it loads, then done and ok with what came of it',
      (seen.get('during') or {}).get('phase') == 'loading' and seen['during']['families'] == [B] and sw and sw['phase'] == 'done' and sw['ok'] and 'loaded' in sw['words'], (seen, sw))
    t('the outcome is shown for a while, then no more', maint.switching(now=time.time() + maint.SHOWN_FOR + 1) is None)
    t('on: the record goes, the families loaded before are loaded (first replica), the server started first',
      maint.switched_off() is None and maint.read() is None and loads == [B] and starts and any('switched on' in w for w in words), (loads, words))
    crashes_after = sum(1 for l in open(crashes.RECORD) if '"crash"' in l) if os.path.exists(crashes.RECORD) else 0
    t('nothing unloaded while off is recorded as a crash', crashes_after == crashes_before, (crashes_before, crashes_after))
    t('on when not off: says so, loads nothing', models.switch_on() == ['the local models were not switched off'] and loads == [B])
    (L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server,
     models.server_up, crashes.MISS, L.SWAP_LOCK, L.read_ok, L.inflight, L.swap_blockers) = real

def test_watchdog():
    print("the watchdog: an instance still listed whose engine stopped (LM Studio's log), read per instance")
    from localmodels import watchdog
    log = ['[2026-11-13 03:38:30][INFO][qwen/qwen3.6-35b-a3b] Prompt processing progress: 99.3%',
           '[2026-11-13 03:38:37][INFO][qwen/qwen3.6-35b-a3b] Done reasoning. Reasoned for 0.2 seconds.',
           '[2026-11-13 03:38:40][INFO][qwen/qwen3.6-35b-a3b:2] Generated prediction: {',
           '  "id": "chatcmpl-x",',
           '[2026-11-13 03:38:53][INFO][qwen/qwen3.6-35b-a3b] Running chat completion on conversation with 2 messages.',
           '[2026-11-13 03:38:54][WARN][qwen/qwen3.6-35b-a3b] Reasoning setting \'low\' is not supported by model',
           '[2026-11-13 03:38:55][INFO][qwen/qwen3.6-35b-a3b] Running Anthropic messages API on conversation with 3 messages.',
           '[2026-11-13 03:38:56][INFO][qwen/qwen3.6-35b-a3b] Generating synchronous v1/responses response',
           '[2026-11-13 03:39:02][INFO][LMSAuthenticator][Client=lms-cli][Endpoint=listLoaded] Listing loaded models',
           '[2026-11-13 03:39:05][DEBUG] Received request: GET to /api/v0/models']
    ctx = {}
    crashes.parse(log, ctx)
    T = lambda s: time.mktime(time.strptime('2026-11-13 ' + s, '%Y-%m-%d %H:%M:%S'))
    t("each instance's last engine line; the server's lines on taking a request (any API) and its WARN lines do not count",
      ctx.get('engine', {}).get(B) == T('03:38:37') and ctx['engine'].get(C2) == T('03:38:40'), ctx.get('engine'))
    t('the last engine step and the last line of any kind', ctx.get('step') == T('03:38:30') and ctx.get('line') == T('03:39:05'), (ctx.get('step'), ctx.get('line')))
    fams, listed = {B: {}}, {B, C2}
    now = T('03:41:00')
    eng = {B: T('03:38:37'), C2: T('03:40:59')}
    mark = lambda c, s, ep='completions', answer=3000: dict(state='answering', copy=c, answering_since=s, ep=ep, answer=answer)
    hung = [mark(B, T('03:37:58')), mark(B, T('03:38:03')), mark(B, T('03:38:22')), mark(B, T('03:38:53'))]
    got = watchdog.look(hung + [mark(C2, T('03:40:30'))], eng, T('03:41:00'), T('03:41:00'), fams, listed, now=now)
    t('a request sent 127 s ago and no engine line since (the first-token rule): stalled, in words; its busy sibling is not',
      list(got) == [B] and 'after a request was sent' in got[B][0] and got[B][1] == 4, got)
    got = watchdog.look(hung, eng, T('03:40:40'), T('03:40:40'), fams, listed, now=now)
    t('...measured against the newest line LM Studio wrote, not the clock: 107 s of log is not yet a stall', got == {}, got)
    t("...and nothing at all while LM Studio's log has shown no engine step within a day", watchdog.look(hung, eng, now, now - 90000, fams, listed, now=now) == {})
    eng2 = dict(eng, **{B: T('03:38:54')})
    t('an engine line after the newest request: not the first-token rule', watchdog.look(hung, eng2, now, now, fams, listed, now=now) == {})
    four = [mark(B, T('03:28:00'))] * 4
    got = watchdog.look(four, {B: T('03:31:00')}, T('03:41:01'), T('03:41:01'), fams, listed, now=now)
    t('every slot busy and no engine line for 601 s (the silence rule): stalled', list(got) == [B] and 'with 4 requests in flight' in got[B][0], got)
    t('...600 s of silence is within the limit at 599', watchdog.look(four, {B: T('03:31:02')}, T('03:41:01'), T('03:41:01'), fams, listed, now=now) == {})
    long8k = [mark(B, T('03:28:00'), answer=8000)] + [mark(B, T('03:28:00'))] * 3
    t('an answer of 8,000 tokens in flight: the silence limit is the time it takes at 8 tokens a second (1,000 s), not 600',
      watchdog.look(long8k, {B: T('03:31:00')}, T('03:41:01'), T('03:41:01'), fams, listed, now=now) == {}
      and list(watchdog.look(long8k, {B: T('03:31:00')}, T('03:47:41'), T('03:47:41'), fams, listed, now=now)) == [B])
    unset = [mark(B, T('03:00:00'), answer=0)] * 4
    t('an answer with no max_tokens: 30 min at most, not less', watchdog.look(unset, {B: T('03:01:00')}, T('03:30:59'), T('03:30:59'), fams, listed, now=now) == {}
      and list(watchdog.look(unset, {B: T('03:01:00')}, T('03:31:01'), T('03:31:01'), fams, listed, now=now)) == [B])
    t('an embedding, a copy on a peer, an instance no longer listed, a request still queued: never judged',
      watchdog.look([mark(B, T('03:30:00'), 'embeddings'), mark(f'{B} on m4pro', T('03:30:00')), mark('gone/model', T('03:30:00')),
                     dict(state='queued', copy=None, since=T('03:30:00'))], {}, now, now, {B: {}, 'gone/model': {}}, listed, now=now) == {})
    t('a family with "watchdog": false is not judged; its own limits are taken',
      watchdog.look(hung, eng, now, now, {B: dict(watchdog=False)}, listed, now=now) == {}
      and list(watchdog.look(hung, eng, T('03:39:30'), T('03:39:30'), {B: dict(watchdog=dict(first=30))}, listed, now=now)) == [B])
    t('the defaults: 120 s to a first engine line, 600 s of silence', watchdog.thresholds({}) == dict(first=120, silent=600) and watchdog.thresholds(dict(watchdog=False)) is None)


def test_stalls(box):
    print('a stalled instance: out of routing, reloaded without a drain (its older sibling first), at most 3 an hour; a failed drain holds back no sibling')
    from localmodels import llmshare as L, models, watchdog
    real = L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, L.SWAP_LOCK, L.read_ok, L.inflight
    L.read_ok = lambda: True
    fam = {B: dict(key=B, copies=2, context=65536, parallel=4, recycle=dict(tokens=800000, soft=560000, before_batch=0.5))}
    L.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    L.SWAP_LOCK = os.path.join(box, 'swap4.lock')
    order, busy, flight, stuck = [B, C2], {'held': {}}, [], set()     # stuck: what LM Studio still calls GENERATING (a stalled engine)
    L.loaded = lambda fresh=False, force=False: [row(i, state='GENERATING' if i in stuck else 'IDLE') for i in order]
    L.api_models = lambda timeout=3: {i: 65536 for i in order}
    L.busy_slots = lambda: dict(busy['held'])
    L.inflight = lambda: list(flight)
    loads, unloads = [], []
    def fake_load(key, ident, context, parallel):
        loads.append(ident); crashes.loaded_now([ident])
        if ident not in order: order.append(ident)              # loaded last: the newest instance
        return True, 'ok'
    def fake_unload(ident):                                    # LM Studio resolves a bare model key to its oldest instance
        took = order[0] if ident == B else ident
        unloads.append(took); order.remove(took); return True, ''
    models.load, models.unload = fake_load, fake_unload
    models._lms, models.free_gb, models.ensure_server = (lambda *a, **k: (True, '')), (lambda: 100.0), (lambda *a, **k: True)
    crashes.after_maintenance([row(B), row(C2)])               # a fresh picture: both loaded, nothing down, the log read from its end
    reset(time.time() - 600)
    now = time.time()
    with crashes._state() as st:                              # LM Studio's log as crashes.watch leaves it: copy 1 silent since a request was sent
        st.update(engine={B: now - 300, C2: now - 2}, log_line=now - 1, log_step=now - 1)
    flight[:] = [dict(state='answering', copy=B, answering_since=now - 250, ep='completions')] * 4
    n0 = len([r for r in crashes.records(1) if r.get('event') == 'stall'])
    stuck.add(B)
    t('the first-token rule finds copy 1 stalled: marked, recorded once', watchdog.check([row(B), row(C2)], fam) and crashes.stalled_copies() == {B}
      and len([r for r in crashes.records(1) if r.get('event') == 'stall']) == n0 + 1 and not watchdog.check([row(B), row(C2)], fam), crashes.stalled_copies())
    busy['held'] = {L._safe(B): 4}                             # its requests not yet let go: the job waits for them
    words = models.keep()
    j = recycle.job()
    t('copy 1 is the oldest, so its own stall job: not reloaded while its cut requests still hold their slots', j and j['copy'] == B and j.get('stall') and not unloads, (j, words))
    t('...the screens say it is reloading after a stall', 'reloading after a stall' in recycle.words(j) and crashes.stalled()[0]['copy'] == B, recycle.words(j))
    busy['held'] = {}; flight.clear()
    words = models.keep()
    rec = [r for r in crashes.records(1) if r.get('event') == 'loaded again' and r.get('after') == 'stall']
    t('the slots let go: unloaded and loaded again at once (LM Studio still says GENERATING), the stall over, recorded as back, not as a recycle',
      unloads == [B] and loads == [B] and recycle.job() is None and not crashes.stalled_copies() and rec, (unloads, loads, words))
    hist = [x for x in crashes.history(1) if x.get('stall')]
    t('the crash history shows the stall and its reload', hist and hist[0]['back'] and 'reloaded by the instance manager' in hist[0]['then'] and 'stalled' in crashes.brief(hist[0]), hist[:1])

    unloads.clear(); loads.clear(); order[:] = [C2, B]           # copy 1 stalls while copy 2 is the older instance
    with crashes._state() as st: st['recycle']['last'] = {}
    crashes.stall(B, 'stalled: test')
    busy['held'] = {L._safe(C2): 1}                            # copy 2 has a request of its own to finish first
    words = models.keep()
    j = recycle.job()
    busy['held'] = {}
    t("copy 1 stalled while copy 2 is older (LM Studio unloads the oldest by copy 1's name): copy 2 drains first, copy 1 stays out of routing",
      j and j['copy'] == C2 and not j.get('stall') and 'so that ' + B + ' becomes the oldest' in j['why'] and B in crashes.stalled_copies() and not unloads, (j, words))
    for _ in range(3): models.keep()
    t('then copy 2 recycled, and copy 1, now the oldest, reloaded after its stall', unloads == [C2, B] and loads == [C2, B] and not crashes.stalled_copies(), (unloads, loads))

    unloads.clear(); loads.clear(); order[:] = [B, C2]
    with crashes._state() as st:                              # copy 2 draining for a recycle, its drain never emptying
        st['recycle']['job'] = dict(copy=C2, family=B, state='draining', since=time.time() - 60, why='test', counts={}, share=1.2)
    busy['held'] = {L._safe(C2): 4}
    crashes.stall(C2, 'stalled: test')
    words = models.keep()
    t('the copy draining stalls: its job becomes a stall job, which waits STALL_GRACE at most for the slots', recycle.job().get('stall') and any('stalled while draining' in w for w in words), words)
    t('...not before', not unloads, unloads)
    with crashes._state() as st: st['recycle']['job']['stall_at'] = time.time() - recycle.STALL_GRACE - 1
    words = models.keep(); busy['held'] = {}
    t('...then reloaded although slots are still held (a program of its own can hold one on a stalled engine)', unloads == [C2] and loads == [C2] and recycle.job() is None, (unloads, words))

    unloads.clear(); loads.clear()
    for k in range(3): crashes._record(dict(at=time.time() - 60 * (k + 1), event='stall', model=B, family=B, cause='test'))
    crashes.stall(B, 'stalled: again')
    words = models.keep()
    t('a fourth stall of a family within an hour: left out of routing, not reloaded, in words for a person', not unloads and B in crashes.stalled_copies()
      and 'times within 60 min' in (recycle.held_back().get(B) or '') and 'localmodels use' in recycle.held_back()[B], (unloads, recycle.held_back()))
    crashes.loaded_now([B])
    t('a load of it by anyone ends the stall', not crashes.stalled_copies())

    unloads.clear(); loads.clear(); reset(time.time() - 3 * 3600); order[:] = [B, C2]
    with crashes._state() as st:                              # copy 1's drain was given up a minute ago; copy 2 is past its threshold
        st['recycle']['retry'] = {B: time.time() + 540}; st['recycle']['retry_after'] = time.time() + 540
    served(C2, 130)
    words = models.keep()
    t("one copy's given-up drain holds back no sibling: copy 2, past its threshold, is recycled meanwhile", unloads == [C2] and loads == [C2], (unloads, loads, words))
    served(B, 130); unloads.clear(); loads.clear()
    with crashes._state() as st: st['recycle']['last'] = {}
    words = models.keep()
    t('...and copy 1 waits out its own pause', not unloads, (unloads, words))
    L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, L.SWAP_LOCK, L.read_ok, L.inflight = real
    with crashes._state() as st: st['down'], st['stalled'], st['recycle'] = {}, {}, dict(job=None, last={}, fresh={}, retry_after=0, retry={})


def test_peers_only(box):
    print('switched off to peers only: requests admitted for the peers, nothing loaded here, this Mac unloaded once its own requests end, at_once the peers alone')
    from localmodels import llmshare as L, maint, models, peers
    maint.OFF, maint.SWITCHING = os.path.join(box, 'off-p.json'), os.path.join(box, 'switching-p.json')
    real = (L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server,
            models.server_up, L.SWAP_LOCK, L.read_ok, L.inflight, L.swap_blockers, peers.view, peers.capacity, L.parallel_of)
    L.read_ok = lambda: True
    fam = {B: dict(key=B, copies=2, context=65536, parallel=4, recycle=False, load_at_start=True)}
    L.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    L.SWAP_LOCK = os.path.join(box, 'swap5.lock')
    api = {'ids': {B: 65536, C2: 65536}}
    L.loaded = lambda fresh=False, force=False: [row(i) for i in api['ids']]
    L.api_models = lambda timeout=3: dict(api['ids'])
    L.parallel_of = lambda m: 4
    held, flight = {'slots': {}}, []
    L.busy_slots = lambda: dict(held['slots'])
    L.inflight = lambda: list(flight)
    L.swap_blockers = lambda ids: ['analysis holds a lease on qwen/qwen3.6-35b-a3b']
    peer_up = [dict(name='m4pro', state='up', families={B: dict(match=True, cap=4, slots=4)})]
    peers.view, peers.capacity = (lambda max_age=30: [dict(p) for p in peer_up]), (lambda model: 4 if peer_up and peer_up[0]['state'] == 'up' else 0)
    loads, unloads = [], []
    def fake_load(key, ident, context, parallel):
        loads.append(ident); api['ids'] = dict(api['ids'], **{ident: 65536}); return True, 'ok'
    def fake_unload(ident):
        unloads.append(ident); api['ids'] = {k: v for k, v in api['ids'].items() if k != ident}; return True, ''
    models.load, models.unload = fake_load, fake_unload
    models._lms, models.free_gb, models.server_up, models.ensure_server = (lambda *a, **k: (True, '')), (lambda: 100.0), (lambda *a, **k: True), (lambda *a, **k: True)
    reset(time.time() - 600)
    t('on: at_once counts this Mac and the peer', L.at_once(B) == 12, L.at_once(B))
    code, body = models.switch_off(peers_only=True)
    m = maint.read()
    t('peers only: switched off even with a run holding a lease (nothing is cut, nothing refused)', code == 200 and m['off'] and m['peers'] and 'peers' in body['note'], (code, body))
    t('...the instance manager pauses (maintenance on), the gateway does not refuse, the header says peers-only',
      maint.on() and not maint.refusing() and maint.peers_only() and maint.header(m).startswith('peers-only') and 'peers only' in maint.words(m), (maint.header(m), maint.words(m)))
    t('at_once: the peer alone (4), never this Mac\'s instances, still listed', L.at_once(B) == 4 and L.at_once() == 4, (L.at_once(B), L.at_once()))
    flight[:] = [dict(state='answering', copy=f'{B} on m4pro', answering_since=time.time()), dict(state='answering', copy=B, answering_since=time.time())]
    words = models.keep()
    t('a request still answering on this Mac: nothing unloaded yet, and it says so', not unloads and any('on this Mac' in w for w in words), words)
    flight[:] = [dict(state='answering', copy=f'{B} on m4pro', answering_since=time.time()), dict(state='queued', copy=None)]
    held['slots'] = {L._safe(B): 1}
    words = models.keep()
    t('...nor while a slot is held here (a program of its own)', not unloads, words)
    held['slots'] = {}
    words = models.keep()
    t('only requests on the peer: every instance here unloaded (they never end, so they are not waited for)',
      unloads and not api['ids'] and maint.switched_off().get('unloaded') and any('peers only' in w for w in words + [maint.words(maint.read())]), (unloads, words))
    with crashes._state() as st: st['down'] = {B: dict(crashed=time.time() - 4000, models=[C2], state='left down', why='test', since=time.time() - 4000, retry_at=time.time() - 4000)}
    api['ids'] = {B: 65536}                                    # one loaded by hand: no replica added
    loads.clear(); words = models.keep(); models.keep()
    t('nothing loads here: no crash reload (a family left down), no missing replica, no load_at_start', not loads, loads)
    rec = [r for r in crashes.records(1) if r.get('event') == 'cancelled']
    t('...and the family left down is cancelled, recorded, off the screens (switching on starts afresh)', not crashes.down() and rec and rec[-1].get('why') == 'local models switched off'
      and any('cancelled' in w for w in words), (crashes.down(), rec[-1:], words))
    api['ids'] = {}
    code, body = models.switch_off()
    t('off without peers: the same record, now refusing (at_once 0)', code == 200 and maint.refusing() and not maint.peers_only() and L.at_once(B) == 0 and L.at_once() == 0, (code, body))
    code, body = models.switch_off(peers_only=True)
    t('and back to peers only', code == 200 and maint.peers_only() and maint.read()['unloaded'], (code, body))
    peer_up[0]['state'] = 'down'
    t('peers only with the peer down: at_once 0, and switching to it says requests get 503', L.at_once(B) == 0 and 'no peer is reachable' in models._peer_words(), models._peer_words())
    peer_up[0]['state'] = 'up'
    t('the screens say where requests go, and count only this Mac\'s requests for the unload', maint.view()['peers'] is True)
    words = models.switch_on()
    t('on: the record goes and the families loaded before are loaded again', maint.switched_off() is None and loads == [B], (loads, words))
    with crashes._state() as st: st['down'] = {}
    (L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server,
     models.server_up, L.SWAP_LOCK, L.read_ok, L.inflight, L.swap_blockers, peers.view, peers.capacity, L.parallel_of) = real


def test_privacy_check(box):
    print('publish: the privacy check reads every commit the push sends against a word list kept outside the repo')
    import subprocess
    from localmodels import cli
    repo, origin = os.path.join(box, 'repo'), os.path.join(box, 'origin.git'); os.makedirs(repo)
    g = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True, check=True).stdout
    subprocess.run(['git', 'init', '-q', '--template=', '--bare', origin], check=True)
    g('init', '-q', '--template=', '-b', 'main'); g('config', 'user.email', 't@example.org'); g('config', 'user.name', 'T'); g('config', 'commit.gpgsign', 'false'); g('config', 'tag.gpgsign', 'false')
    open(os.path.join(repo, 'a.txt'), 'w').write('old line written by Pat Sample\n')
    g('add', '.'); g('commit', '-q', '-m', 'first')
    words = os.path.join(box, 'words.txt')
    open(words, 'w').write('# a comment\npat\nsample\n\\bzephyr\\b\nproject-x\nallow: zephyr-free\nallow: sample\\.org/pat\n')
    r = cli.private_check(repo, words)
    t('before the first push every commit is read (the push sends them all)', r['status'] == 'hits' and r['since'] is None
      and any('old line' in h[1] for h in r['hits']), {k: r[k] for k in ('status', 'since', 'hits')})
    g('remote', 'add', 'origin', origin); g('push', '-q', 'origin', 'main'); g('tag', '-a', 'v1.0.0', '-m', 'release')
    with open(os.path.join(repo, 'a.txt'), 'a') as f: f.write('this is zephyr-free and fine\nthe zephyr notes\nsee sample.org/pat for more\n')
    open(os.path.join(repo, 'project-x-notes.md'), 'w').write('nothing here\n')
    g('add', '.'); g('commit', '-q', '-m', 'notes for Pat')
    r = cli.private_check(repo, words)
    where, texts = [h[0] for h in r['hits']], [h[1] for h in r['hits']]
    t('a line already on origin is not read again', not any('old line' in x for x in texts), r['hits'])
    t('a new line holding a word is caught', any('the zephyr notes' in x for x in texts), r['hits'])
    t('an allowed phrase and an allowed address pass', not any('zephyr-free' in x or 'sample.org/pat' in x for x in texts), r['hits'])
    t('a new file whose name holds a word is caught', any('project-x-notes.md' in w for w in where), where)
    t('a commit message is read too (it goes public with the history)', any(w.startswith('commit ') for w in where), where)
    t('it says what it read: the commits not on origin, so many lines, files and messages',
      r['status'] == 'hits' and r['since'] == 'origin' and r['lines'] == 4 and r['files'] == 1 and r['commits'] == 1, {k: r[k] for k in ('status', 'since', 'lines', 'files', 'commits')})
    t('no word list: it says the check was skipped, never a silent pass', cli.private_check(repo, os.path.join(box, 'none.txt'))['status'] == 'skipped')

    print('publish: a word stays in the commit that added it; a later commit does not clear it')
    g('reset', '-q', '--hard', 'origin/main')
    open(os.path.join(repo, 'b.txt'), 'w').write('zephyr in one commit\n')
    g('add', '.'); g('commit', '-q', '-m', 'add')
    first = g('rev-parse', '--short=7', 'HEAD').strip()
    open(os.path.join(repo, 'b.txt'), 'w').write('replaced in the next\n')
    g('commit', '-q', '-am', 'replace')
    r = cli.private_check(repo, words)
    t('a word added in one commit and replaced in the next is still a hit, named by the commit that holds it',
      any(h[0].startswith(first) and 'b.txt:1' in h[0] for h in r['hits']), r['hits'])
    open(os.path.join(repo, 'project-x.md'), 'w').write('a\n')
    g('add', '.'); g('commit', '-q', '-m', 'file'); g('mv', 'project-x.md', 'plain.md'); g('commit', '-q', '-m', 'rename')
    r = cli.private_check(repo, words)
    t('a file added under a listed name and renamed later is still a hit', any('project-x.md' in h[0] for h in r['hits']), r['hits'])
    g('tag', 'handmade')
    t('a tag at HEAD does not empty what is read', cli.private_check(repo, words)['hits'] == r['hits'], cli.private_check(repo, words)['hits'])

    print('publish: what the diff parser must not skip')
    g('reset', '-q', '--hard', 'origin/main')
    open(os.path.join(repo, 'résumé "q".txt'), 'w').write('zephyr in a quoted name\n')
    open(os.path.join(repo, 'p.txt'), 'w').write('++ a line that looks like a header\nzephyr after it\n')
    g('add', '.'); g('commit', '-q', '-m', 'names')
    r = cli.private_check(repo, words)
    texts = [h[1] for h in r['hits']]
    t('a file whose name git quotes is read', 'zephyr in a quoted name' in texts, r['hits'])
    t("an added line that begins '++ ' does not hide the lines after it", 'zephyr after it' in texts, r['hits'])
    g('checkout', '-q', '-b', 'side', 'origin/main'); open(os.path.join(repo, 's.txt'), 'w').write('one\n'); g('add', '.'); g('commit', '-q', '-m', 'side')
    g('checkout', '-q', 'main'); g('merge', '-q', '--no-ff', '--no-commit', 'side')
    open(os.path.join(repo, 'merged.txt'), 'w').write('zephyr written in the merge\n'); g('add', '.'); g('commit', '-q', '-m', 'merge')
    r = cli.private_check(repo, words)
    t('a line added in a merge commit itself is read', 'zephyr written in the merge' in [h[1] for h in r['hits']], r['hits'])
    g('tag', '-a', 'v9', '-m', 'release for Pat')
    t("an annotated tag's message is read (the push sends it with --follow-tags)", any(h[0] == 'tag v9' for h in cli.private_check(repo, words)['hits']),
      cli.private_check(repo, words)['hits'])
    os.makedirs(os.path.join(box, 'not-a-repo'), exist_ok=True)
    try: bad = cli.private_check(os.path.join(box, 'not-a-repo'), words)['status']
    except RuntimeError: bad = 'refused'
    t('a git command that fails is never a clean check', bad == 'refused', bad)
    open(words, 'w').write('nothing-matches-this\n[unclosed\n')
    t('nothing new holds a word: clean (a line that is not a valid pattern is read as plain text)', cli.private_check(repo, words)['status'] == 'clean')

    print('publish: the release commit holds the version stamp alone, on the commit the checks read')
    os.makedirs(os.path.join(repo, 'localmodels'), exist_ok=True); os.makedirs(os.path.join(repo, '.claude-plugin'), exist_ok=True)
    open(os.path.join(repo, 'localmodels', '__init__.py'), 'w').write("VERSION = '1.0.0'\n")
    open(os.path.join(repo, '.claude-plugin', 'plugin.json'), 'w').write(json.dumps(dict(name='x', version='1.0.0')) + '\n')
    g('add', '.'); g('commit', '-q', '-m', 'code')
    head = g('rev-parse', 'HEAD').strip()
    open(os.path.join(repo, 'other.txt'), 'w').write('staged by someone else\n'); g('add', 'other.txt')
    try: sha = cli.stamp(repo, '1.0.1', head)
    except RuntimeError as e: sha = f'refused: {e}'
    t('something else staged after the checks: refused, nothing stamped', str(sha).startswith('refused') and g('rev-parse', 'HEAD').strip() == head
      and "'1.0.0'" in open(os.path.join(repo, 'localmodels', '__init__.py')).read(), sha)
    g('reset', '-q', 'other.txt'); os.remove(os.path.join(repo, 'other.txt'))
    try: sha = cli.stamp(repo, '1.0.1', head)
    except RuntimeError as e: sha = f'refused: {e}'
    files = g('show', '--name-only', '--format=', 'HEAD').split()
    t('the release commit holds the two stamped files and nothing else', sha == g('rev-parse', 'HEAD').strip()
      and sorted(files) == ['.claude-plugin/plugin.json', 'localmodels/__init__.py'], (sha, files))
    try: moved = cli.stamp(repo, '1.0.2', head)
    except RuntimeError as e: moved = f'refused: {e}'
    t('HEAD moved since the checks: refused', str(moved).startswith('refused'), moved)

    print('publish: before anything is stamped, the version, the tag and origin are checked')
    g('reset', '-q', '--hard', 'HEAD~1'); g('push', '-q', '-f', 'origin', 'HEAD:refs/heads/main'); g('fetch', '-q', 'origin')
    P = lambda want, cur='1.0.0': cli.preflight(repo, want, cur, 'main')
    t('a version not above the current one is refused', P('1.0.0') and P('0.9.9') and P('1.0') , (P('1.0.0'), P('0.9.9'), P('1.0')))
    g('tag', '-a', 'v1.0.5', '-m', 'old'); t('a tag that exists here is refused', P('1.0.5') and 'v1.0.5' in P('1.0.5'), P('1.0.5')); g('tag', '-d', 'v1.0.5')
    other = os.path.join(box, 'other-clone'); subprocess.run(['git', 'clone', '-q', origin, other], check=True)
    o = lambda *a: subprocess.run(['git', '-C', other, *a], capture_output=True, text=True, check=True).stdout
    o('config', 'user.email', 't@example.org'); o('config', 'user.name', 'T'); o('config', 'commit.gpgsign', 'false'); o('config', 'tag.gpgsign', 'false')
    o('tag', '-a', 'v1.0.6', '-m', 'pushed elsewhere'); o('push', '-q', 'origin', 'v1.0.6')
    w6 = P('1.0.6')
    t('a tag that is already on origin is refused', w6 and 'origin' in w6, w6)
    t('a new version with origin contained in HEAD: nothing to say', P('1.0.7') is None, P('1.0.7'))
    open(os.path.join(other, 'later.txt'), 'w').write('x\n'); o('add', '.'); o('commit', '-q', '-m', 'pushed from elsewhere'); o('push', '-q', 'origin', 'HEAD:main')
    t('origin has a commit this branch lacks: refused (pull first), nothing stamped', P('1.0.7') and 'pull' in P('1.0.7'), P('1.0.7'))


def test_secret_check(box):
    print('publish and every push: the secrets check reads what the push sends for credentials, key files and code that runs on GitHub')
    import subprocess
    from localmodels import cli
    box = os.path.join(box, 'secrets'); os.makedirs(box)
    repo, origin = os.path.join(box, 'repo'), os.path.join(box, 'origin.git'); os.makedirs(repo)
    g = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True, check=True).stdout
    subprocess.run(['git', 'init', '-q', '--template=', '--bare', origin], check=True)
    g('init', '-q', '--template=', '-b', 'main'); g('config', 'user.email', 't@example.org'); g('config', 'user.name', 'T')
    g('config', 'commit.gpgsign', 'false'); g('config', 'tag.gpgsign', 'false')
    def commit(files, msg='c'):
        for name, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(repo, name)) or repo, exist_ok=True)
            open(os.path.join(repo, name), 'w').write(text)
        g('add', '-A'); g('commit', '-q', '-m', msg)
    commit({'a.txt': 'plain text, a password field named in prose, sk-short\n', '.env.example': 'KEY=\n'})
    r = cli.secret_check(repo)
    t('plain text and an example env file: clean, every commit read before the first push', r['status'] == 'clean' and r['since'] is None and r['commits'] == 1, r)
    g('remote', 'add', 'origin', origin); g('push', '-q', 'origin', 'main')
    token = 'gh' + 'p_' + 'A1b2C3d4' * 5                     # built here, so this file holds no token itself
    commit({'conf.py': f'TOKEN = "{token}"\n'})
    hits = cli.secret_check(repo)['hits']
    t('a GitHub token in an added line is caught, named by its commit and line', any('conf.py:1' in w for w, _ in hits), hits)
    g('reset', '-q', '--hard', 'origin/main')
    commit({'notes.md': '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----\nabc\n'})
    t('a private key block is caught', any('notes.md' in w for w, _ in cli.secret_check(repo)['hits']))
    g('reset', '-q', '--hard', 'origin/main')
    commit({'keys/id_ed25519': 'x\n', 'prod.env': 'x\n', '.env': 'x\n', 'cert.pem': 'x\n'})
    names = [w for w, _ in cli.secret_check(repo)['hits']]
    t('key files by name: id_ed25519, .env and *.pem caught', all(any(n in w for w in names) for n in ('id_ed25519', '.env', 'cert.pem')), names)
    g('reset', '-q', '--hard', 'origin/main')
    commit({'.github/workflows/ci.yml': 'on: push\n'}, 'ci: refresh build cache')
    hits = cli.secret_check(repo)['hits']
    t('a workflow file is caught (this repository runs nothing on GitHub)', any('.github/workflows/ci.yml' in w for w, _ in hits), hits)
    g('reset', '-q', '--hard', 'origin/main')
    commit({'package.json': '{}\n'})
    t('a package manifest (install scripts run on npm install) is caught', any('package.json' in w for w, _ in cli.secret_check(repo)['hits']))
    g('reset', '-q', '--hard', 'origin/main')
    commit({'b.txt': 'nothing\n'}, f'key {token}')
    t('a token in a commit message is caught (it goes public with the history)', any(w.startswith('commit ') for w, _ in cli.secret_check(repo)['hits']))

    print('every push: the pre-push hook runs the checks and refuses a rewrite of the remote branch')
    words = os.path.join(box, 'words.txt'); open(words, 'w').write('zephyr\n')
    real, here = cli.PRIVATE_WORDS, os.getcwd()
    cli.PRIVATE_WORDS = words
    os.chdir(repo)
    try:
        g('reset', '-q', '--hard', 'origin/main')
        base = g('rev-parse', 'origin/main').strip()
        commit({'c.txt': 'fine\n'})
        tip = g('rev-parse', 'HEAD').strip()
        line = lambda lsha, rsha: f'refs/heads/main {lsha} refs/heads/main {rsha}\n'
        t('a clean fast-forward push goes', cli.prepush(['origin'], line(tip, base)) == 0)
        commit({'d.txt': 'the zephyr notes\n'})
        t('a private word in the push stops it', cli.prepush(['origin'], line(g('rev-parse', 'HEAD').strip(), base)) == 1)
        g('reset', '-q', '--hard', 'origin/main'); commit({'e.txt': f'{token}\n'})
        t('a credential in the push stops it', cli.prepush(['origin'], line(g('rev-parse', 'HEAD').strip(), base)) == 1)
        g('reset', '-q', '--hard', 'origin/main'); commit({'f.txt': 'x\n'})
        g('push', '-q', 'origin', 'main'); g('fetch', '-q', 'origin')
        pushed = g('rev-parse', 'HEAD').strip()
        g('reset', '-q', '--hard', 'HEAD~1'); commit({'g.txt': 'y\n'})
        other = g('rev-parse', 'HEAD').strip()
        t('a force push that rewrites the remote branch is refused', cli.prepush(['origin'], line(other, pushed)) == 1)
        os.environ['LOCALMODELS_ALLOW_REWRITE'] = '1'
        try: t('...and goes with LOCALMODELS_ALLOW_REWRITE=1, on purpose', cli.prepush(['origin'], line(other, pushed)) == 0)
        finally: os.environ.pop('LOCALMODELS_ALLOW_REWRITE')
        t('a commit on the remote this checkout has never seen: refused (fetch and look first)', cli.prepush(['origin'], line(other, 'ab' * 20)) == 1)
        t('deleting the remote branch is refused', cli.prepush(['origin'], f'(delete) {cli.ZERO} refs/heads/main {pushed}\n') == 1)
        g('reset', '-q', '--hard', pushed)
        hook = os.path.join(repo, '.git', 'hooks', 'pre-push')
        words1 = cli.set_prepush_hook(repo)
        t('install writes the hook, calling this checkout\'s command', 'written' in words1 and cli.HOOK_MARK in open(hook).read() and os.access(hook, os.X_OK), words1)
        t('a second install leaves it as it is', 'in place' in cli.set_prepush_hook(repo))
        env = dict(os.environ, LOCALMODELS_PRIVATE_WORDS=words)
        commit({'h.txt': f'{token}\n'})
        r = subprocess.run(['git', '-C', repo, 'push', '-q', 'origin', 'main'], capture_output=True, text=True, env=env)
        t('a plain git push with a credential is stopped by the hook, and nothing reaches the remote', r.returncode != 0 and 'push stopped' in r.stderr
          and subprocess.run(['git', '--git-dir', origin, 'rev-parse', 'main'], capture_output=True, text=True).stdout.strip() == pushed, (r.returncode, r.stderr[-300:]))
        g('reset', '-q', '--hard', pushed); commit({'i.txt': 'ok\n'})
        r = subprocess.run(['git', '-C', repo, 'push', '-q', 'origin', 'main'], capture_output=True, text=True, env=env)
        t('a clean plain git push goes through the hook', r.returncode == 0, r.stderr[-300:])
        open(hook, 'w').write('#!/bin/sh\nexit 0\n')
        t('a hook of another tool is left in place and named', 'left as it is' in cli.set_prepush_hook(repo) and 'exit 0' in open(hook).read())
    finally:
        cli.PRIVATE_WORDS = real; os.chdir(here)


def test_about(box):
    print("the dashboard's corner: the version, the license and the source, read from the hub's own files at run time")
    from localmodels import NAME, VERSION, web
    home = os.path.join(box, 'about'); os.makedirs(os.path.join(home, '.claude-plugin'))
    open(os.path.join(home, 'LICENSE'), 'w').write('MIT License\n\nCopyright (c) 2026 Pat Sample\n\nPermission is hereby granted...\n')
    manifest = lambda **kw: open(os.path.join(home, '.claude-plugin', 'plugin.json'), 'w').write(json.dumps(dict(name='x', **kw)))
    manifest(repository='https://example.org/pat/hub', author=dict(name='Pat Sample', url='https://pat.example'))
    a = web.about(home)
    t('version, license, copyright, its link and source, as the files say', a == dict(name=NAME, version=VERSION, license='MIT License', copyright='© 2026 Pat Sample',
      copyright_url='https://pat.example', source='https://example.org/pat/hub'), a)
    manifest(author=dict(name='Pat Sample', url='http://pat.example'))
    t('an author address that is not https: no link', web.about(home)['copyright_url'] is None)
    manifest(author='Pat Sample')
    t('an author given as plain text: no link', web.about(home)['copyright_url'] is None)
    t('with neither file (an old install), the version alone', web.about(os.path.join(box, 'nowhere')) == dict(name=NAME, version=VERSION, license=None, copyright=None, copyright_url=None, source=None))


def test_progress(box):
    print("progress words: a batch folder's time left and finish; the status line never needs a key a progress may lack")
    from localmodels import cli, dash
    d = os.path.join(box, 'progress-batch'); os.makedirs(d)
    open(os.path.join(d, 'plan.json'), 'w').write(json.dumps(dict(jobs=[dict(id=i) for i in range(6)], parallel=2)))
    for i in range(2): open(os.path.join(d, f'job{i}.json'), 'w').write(json.dumps(dict(seconds=60)))
    b = dash.batch(d)
    t('a batch folder: done, total and time left', b['done'] == 2 and b['total'] == 6 and b['left'] == 120, b)
    t('a batch folder: the finish time is now plus the time left', bool(b.get('finish')) and abs(b['finish'] - (time.time() + 120)) < 5, b)
    try: words = cli.track_words('batch', dict(name='x', done=2, total=6, left=120))
    except KeyError as e: words = f'KeyError: {e}'
    t('the status line for a progress without a finish time: the time left, no crash', 'left' in words and 'done about' not in words and 'KeyError' not in words, words)
    t('the status line for one with it: done about a time', 'done about' in cli.track_words('batch', b), cli.track_words('batch', b))
    open(os.path.join(d, 'plan.json'), 'w').write(json.dumps(dict(jobs=[dict(id=i) for i in range(6)], parallel='hub slots')))
    for i in range(2): open(os.path.join(d, f'job{i + 2}.running'), 'w').write(json.dumps(dict(since=time.time())))
    try: b = dash.batch(d)
    except Exception as e: b = f'{type(e).__name__}: {e}'
    t("a plan whose parallel is a word: no exception; the jobs running now stand for it", isinstance(b, dict) and b['total'] == 6 and b['left'] == 120, b)
    for bad in ('[1, 2]', '{"jobs": 3, "parallel": null}', 'not json'):
        open(os.path.join(d, 'plan.json'), 'w').write(bad)
        try: b = dash.batch(d)
        except Exception as e: b = f'{type(e).__name__}: {e}'
        t(f'a plan.json of the wrong shape ({bad}): no exception, no total', isinstance(b, dict) and b['total'] is None, b)


def test_run_plans(box):
    print("run plans: a run's phases declared once, each phase's progress from its headers, an estimate for every phase and the whole run")
    from localmodels import cli, dash, runs
    runs.PLANS = os.path.join(box, 'runs')
    now = 1_000_000.0
    t('the phase headers read whole with the run, and nonsense is dropped',
      runs.from_headers({'X-Run': 'survey', 'X-Phase': 'D', 'X-Phase-Done': '75', 'X-Phase-Total': '247'}) == dict(run='survey', phase='D', phase_done=75, phase_total=247)
      and runs.from_headers({'X-Run': 'survey', 'X-Phase': 'D', 'X-Phase-Done': 'many'}) == dict(run='survey', phase='D')
      and runs.from_headers({'X-Phase': 'D', 'X-Phase-Done': '3'}) == {})

    # the plan: declared once, merged by phase name, order kept, new phases appended
    p = runs.declare('proj', 'survey', [dict(name='B', items=30), dict(name='C', items=50), dict(name='D', items=40), dict(name='E', items=8, approx=True)], now=now - 9000)
    p = runs.declare('proj', 'survey', [dict(name='E', items=10, approx=False), dict(name='F')], now=now - 9000)
    t('a plan: phases in the order first declared, a later declaration updates by name and appends new phases',
      [(x['name'], x.get('items'), x.get('approx', False)) for x in p['phases']] == [('B', 30, False), ('C', 50, False), ('D', 40, False), ('E', 10, False), ('F', None, False)], p['phases'])
    p = runs.declare('proj', 'survey', [dict(name='F', items=None)], now=now - 9000)
    p['phases'] = [x for x in p['phases'] if x['name'] != 'F']; runs._save(p)
    for bad in (dict(run='', phases=[]), dict(run='x', phases='B'), dict(run='x', phases=[dict(items=3)]), dict(run='x', phases=[dict(name='B', items='lots')])):
        try: runs.declare('proj', bad['run'], bad['phases'], now=now); err = None
        except ValueError as e: err = e
        t(f'a plan the hub cannot take is refused with the reason ({bad})', err is not None and str(err), err)
    t('the plan is on disk, one file per project and run', [x['run'] for x in runs.plans(now=now - 8000)] == ['survey'])

    def rec(at, phase, done, total, tokens, seconds=60, run='survey', project='proj'):
        return dict(at=at, seconds=seconds, status=200, project=project, kind='batch', run=run, phase=phase, phase_done=done, phase_total=total,
                    prompt_tokens=int(tokens * 0.8), completion_tokens=tokens - int(tokens * 0.8), step=f'item {done}')
    earlier = [rec(now - 86400 + 60 * k + 60, 'D', k, 10, 10_000, run='survey-yesterday') for k in range(10)]   # D took 10,000 tokens an item before
    b = [rec(now - 7200 + 60 * k + 60, 'B', k, 30, 4_000) for k in range(30)]        # B: 30 items at 4,000 tokens, two hours ago
    c = [rec(now - 1200 + 60 * k + 60, 'C', k, 50, 5_000) for k in range(20)]        # C: now, one item a minute at 5,000 tokens
    costs = runs.costs(earlier + b + c)
    t('the cost of a phase from the records: tokens per item, by project and phase name', abs(costs.get(('proj', 'D'), 0) - 10_000) < 1, costs)
    g = runs.progress(b + c, now=now, plans=runs.plans(now=now), costs=costs)
    r = g.get(('proj', 'survey'), {})
    ph = {x['name']: x for x in r.get('phases') or []}
    t('a planned run: every phase listed in plan order', list(ph) == ['B', 'C', 'D', 'E'], list(ph))
    t('a phase a later one has begun after is done, counted whole', ph.get('B', {}).get('state') == 'done' and ph['B'].get('done') == 30, ph.get('B'))
    t('the phase at work: running, its count from its headers', (ph.get('C', {}).get('state'), ph['C'].get('done'), ph['C'].get('total')) == ('running', 19, 50), ph.get('C'))
    t("the phase at work: time left from its own tokens per item and the run's throughput (31 items x 5,000 tokens at ~83 tokens/s, ~31 min)",
      ph['C'].get('left') and 1700 <= ph['C']['left'] <= 2000 and ph['C'].get('basis') == 'measured', ph.get('C'))
    t('a phase not begun, done before in an earlier run: 40 items x 10,000 tokens at the same throughput (~80 min)',
      ph.get('D', {}).get('state') == 'waiting' and ph['D'].get('left') and 4500 <= ph['D']['left'] <= 5100 and ph['D'].get('basis') == 'an earlier run', ph.get('D'))
    t("a phase never measured anywhere: this run's other phases stand for it, and it says so",
      ph.get('E', {}).get('left') and 400 <= ph['E']['left'] <= 700 and ph['E'].get('basis') == "this run's other phases", ph.get('E'))
    t('each phase finishes after the ones before it', ph['C'].get('finish') < ph['D'].get('finish') < ph['E'].get('finish')
      and abs(ph['E']['finish'] - (now + ph['C']['left'] + ph['D']['left'] + ph['E']['left'])) < 2, [ph[k].get('finish') for k in 'CDE'])
    t('the whole run: items of every phase, its time left the sum of the phases left, its finish the last phase\'s',
      (r.get('done'), r.get('total')) == (49, 130) and r.get('left') == ph['C']['left'] + ph['D']['left'] + ph['E']['left'] and abs(r.get('finish', 0) - ph['E']['finish']) < 2
      and r.get('scope') == 'whole run' and r.get('kind') == 'run', {k: r.get(k) for k in ('done', 'total', 'left', 'finish', 'scope')})
    t("the whole run's basis is its least certain phase's", r.get('basis') == "this run's other phases", r.get('basis'))
    t('the job is the phase at work, as the dashboard reads it', (r.get('job') or {}).get('name') == 'C' and r['job'].get('left') == ph['C']['left'], r.get('job'))

    slow = [rec(now - 1200 + 120 * k + 120, 'C', k, 50, 5_000) for k in range(10)]
    r2 = runs.progress(b + slow, now=now, plans=runs.plans(now=now), costs=costs)[('proj', 'survey')]
    d2 = {x['name']: x for x in r2['phases']}['D']
    t("half the throughput (another project took the other half): the same work's estimate doubles", d2.get('left') and 1.8 <= d2['left'] / ph['D']['left'] <= 2.2, (d2.get('left'), ph['D']['left']))

    r3 = runs.progress(b + c, now=now, plans=[], costs={}).get(('proj', 'survey'), {})
    t('no plan declared: the phases the headers name, in the order they began', [x['name'] for x in r3.get('phases') or []] == ['B', 'C'] and r3.get('total') == 80, r3.get('phases'))
    runs.declare('proj', 'fresh', [dict(name='one', items=12), dict(name='two', items=5)], now=now - 30)
    r4 = runs.progress([], now=now, plans=runs.plans(now=now), costs={}).get(('proj', 'fresh'), {})
    t('a plan just declared, nothing measured yet: shown, no made-up number', r4.get('total') == 17 and r4.get('left') is None and r4.get('finish') is None
      and all(x['left'] is None and x['state'] == 'waiting' for x in r4.get('phases') or [None]), r4)
    runs.declare('proj', 'fresh', end=True, now=now - 20)
    t('a run its client ended is no longer shown once idle', ('proj', 'fresh') not in runs.progress([], now=now + runs.ACTIVE + 60, plans=runs.plans(now=now + runs.ACTIVE + 60), costs={}))
    open(os.path.join(runs.PLANS, 'junk.json'), 'w').write('[1, 2]')
    try: n, err = len(runs.plans(now=now)), None
    except Exception as e: n, err = 0, e
    t('a plan file of the wrong shape is skipped, never an exception', err is None and n >= 1, err)

    words = cli.track_words('run', r)
    t('the status line: the whole run, then the phase at work and the ones to come, each with its time',
      'run survey: 49 of 130' in words and 'phase C 19 of 50' in words and 'then D 40' in words and 'E 10' in words, words)
    real = dash.live_runs
    dash.live_runs = lambda: {('proj', 'survey'): r}
    try:
        rows = [dict(project='proj', pid=1, batch=None, since=now - 60)]
        dash.attach_runs(rows)
    finally: dash.live_runs = real
    t('the dashboard row: the whole run with its phases, and the phase at work as its job',
      len((rows[0].get('batch') or {}).get('phases') or []) == 4 and (rows[0].get('job') or {}).get('name') == 'C', rows[0])
    from localmodels import names
    names.set_private('proj', True); names.set_label('proj', 'D', 'deep check')
    try: shown, g2 = names.user(rows[0]), names.run(r)
    finally: names.set_private('proj', False); names.forget('proj')
    t('a private project: its phase and run names hidden on every screen unless labelled, the counts kept',
      [x['name'] for x in shown['batch']['phases']] == [None, None, 'deep check', None] and shown['batch']['name'] is None and shown['job']['name'] is None
      and [x['name'] for x in g2['phases']] == [None, None, 'deep check', None] and g2['phases'][1]['done'] == 19, shown['batch']['phases'])
    t('the status line numbers a phase with no name to show', 'phase #2 19 of 50' in cli.track_words('run', shown['batch']), cli.track_words('run', shown['batch']))


def test_sessions(box):
    print("Claude Code's records: of subagents and workflows, only how many; never a description or an agent's label")
    from localmodels import sessions
    real = sessions.CLAUDE, sessions.DESKTOP
    sessions.CLAUDE, sessions.DESKTOP = os.path.join(box, 'claude-projects'), os.path.join(box, 'claude-desktop')
    sdir = os.path.join(sessions.CLAUDE, '-Users-x-Documents-proj', 'sid1')
    os.makedirs(os.path.join(sdir, 'subagents', 'workflows', 'wf'))
    open(sdir + '.jsonl', 'w').write(json.dumps(dict(cwd='/Users/x/Documents/proj')) + '\n')
    open(os.path.join(sdir, 'subagents', 'agent-1.meta.json'), 'w').write(json.dumps(dict(description='a secret description')))
    open(os.path.join(sdir, 'subagents', 'agent-1.jsonl'), 'w').write('{}\n')
    open(os.path.join(sdir, 'subagents', 'workflows', 'wf', 'journal.jsonl'), 'w').write(
        '\n'.join(json.dumps(e) for e in (dict(type='started', agentId='a', label='secret label one'), dict(type='started', agentId='b', label='secret label two'),
                                             dict(type='result', agentId='a'))) + '\n')
    try: r = sessions.recent()
    finally: sessions.CLAUDE, sessions.DESKTOP = real
    t('a session with one subagent and a workflow of two agents, one done: the counts, and no free text',
      r and r[0]['subagents'] == 1 and r[0]['workflows'][0]['running'] == 1 and r[0]['workflows'][0]['agents'] == 2 and 'secret' not in json.dumps(r), r)


def test_names(box):
    print('names: what every screen calls a project, its batches and jobs, set by the person; private shows only those')
    from localmodels import llmshare as L, names
    names.FILE = os.path.join(box, 'names.json')
    t('nothing set: every name shows as it is seen', names.project('folder-123') == 'folder-123' and names.step('proj', 'extract files 8 of 360') == 'extract files 8 of 360')
    names.set_project('folder-123', 'casework')
    t("a project's folder name shows as the name set", names.project('folder-123') == 'casework')
    s = names.session(dict(id='s1', project='folder-123', title='Secret title'))
    t('a Claude session in that folder shows under the name; its title stays while the project is not private', s['project'] == 'casework' and s['title'] == 'Secret title', s)
    rows = [dict(project='folder-123', kind='batch', seconds=10, waited=0, prompt_tokens=5, completion_tokens=5, step='notes'),
            dict(project='casework', kind='batch', seconds=10, waited=0, prompt_tokens=5, completion_tokens=5, step='extract files 3 of 9')]
    by = L.by_project([names.request(r) for r in rows])
    t('two names seen (a folder and a header) show as one project, their counts together', list(by) == ['casework'] and by['casework']['requests'] == 2, by)
    names.set_private('casework', True)
    s = names.session(dict(id='s1', project='folder-123', title='Secret title'))
    t('private: no Claude session title, only the words every screen can print', s['project'] == 'casework' and s['title'] == 'a Claude Code session', s)
    from localmodels import dash
    sess = dict(s, id='s1', working=True, last=time.time(), subagents=0, workflows=[])
    try: rows = dash.running(dict(claude=[sess], users=[], extra={})); err = None
    except Exception as e: rows, err = [], e
    t("the terminal dash's rows render a private project's session", err is None and any('a Claude Code session' in r['what'] for r in rows), repr(err) if err else rows)
    t('private: a step in free words is hidden; one that counts keeps only its count', names.step('folder-123', 'reading notes.pdf') is None
      and names.step('casework', 'extract files 8 of 360') == '8 of 360', (names.step('folder-123', 'reading notes.pdf'), names.step('casework', 'extract files 8 of 360')))
    names.set_label('casework', 'extract files', 'Exhibit text')
    names.set_label('casework', 'nightly_run.py', 'Nightly check')
    names.set_label('casework', 'night run', 'Nightly batch')
    t('a job named: its label shows, with its count', names.step('casework', 'extract files 8 of 360') == 'Exhibit text 8 of 360')
    u = names.user(dict(project='casework', app='nightly_run.py', title='Secret title', step='extract files 8 of 360',
                        batch=dict(name='night run', phase='extract files', total=9, done=3), job=dict(name='extract files', total=360, done=8)))
    t('a program on the dash: project, script, batch and job show their labels; the title is hidden',
      (u['project'], u['app'], u['title'], u['batch']['name'], u['batch']['phase'], u['job']['name'], u['step'])
      == ('casework', 'Nightly check', None, 'Nightly batch', 'Exhibit text', 'Exhibit text', 'Exhibit text 8 of 360'), u)
    u = names.user(dict(project='casework', app='other.py', title='t', step=None, batch=dict(name='unnamed run', total=4, done=1), job=None))
    t('private: a script or batch with no label shows no name of its own', u['app'] is None and u['batch']['name'] is None, u)
    names.set_private('casework', False)
    u = names.user(dict(project='casework', app='other.py', title='t', step=None, batch=dict(name='unnamed run', total=4, done=1), job=None))
    t('public again: unlabelled names show as they are', u['app'] == 'other.py' and u['batch']['name'] == 'unnamed run' and u['title'] == 't', u)
    from localmodels import web
    real_colors, web.COLORS = web.COLORS, os.path.join(box, 'colors.json')
    now_ = time.time()
    with open(L.REQUESTS, 'a') as f:
        for k in range(3): f.write(json.dumps(dict(at=now_ - 60 * k - 5, project='folder-123', kind='batch', status=200, seconds=4, waited=0, completion_tokens=10)) + '\n')
    h = web._history(60)
    rec = web.recent(5)
    t('the charts and the last requests show the name set, never the folder name',
      'folder-123' not in h['answering'] and 'casework' in h['answering'] and 'folder-123' not in h['requests']['by']
      and all(r['project'] != 'folder-123' for r in rec), (list(h['answering']), list(h['requests']['by']), [r['project'] for r in rec]))
    t('the colors sent to the page cover only the names shown; a renamed name gives up its slot',
      'folder-123' not in web.colors(['casework']) and 'folder-123' not in json.load(open(web.COLORS))['slots'], json.load(open(web.COLORS)))
    web.COLORS = real_colors
    names.forget('casework', 'night run'); names.forget('folder-123')
    t('forget a label, then a project name: back to as seen', names.project('folder-123') == 'folder-123' and names.label('casework', 'night run') == 'night run')
    t('the names file stays a plain JSON object', isinstance(json.load(open(names.FILE)), dict))

    print('screenshot mode: the projects kept show by name; every other one as "other", private; no session title anywhere')
    from localmodels import web
    names.set_present(5, ['research', 'local-models'])
    t('a kept project shows as it is, any other as "other"', names.project('research') == 'research' and names.project('folder-123') == 'other'
      and names.project('casework') == 'other', (names.project('research'), names.project('folder-123')))
    u = names.user(dict(project='folder-123', app='private-script.py', step='extract files 3 of 9', title='Secret title',
                        batch=dict(name='filings', phase='extract files', now=[dict(step='notes on a person')])))
    t('a client of another project: "other", no script, no title, no batch or job names, a count only',
      u['project'] == 'other' and u['app'] is None and u['title'] is None and u['step'] == '3 of 9' and u['batch']['name'] is None
      and u['batch']['now'][0]['step'] is None, u)
    k = names.user(dict(project='research', app='demo', step='records 4 of 40', title='a title', batch=dict(name='consistency check')))
    t("a kept project's steps and batch show, but never a session title", k['step'] == 'records 4 of 40' and k['batch']['name'] == 'consistency check' and k['title'] is None, k)
    c = names.session(dict(id='s2', project='local-models', title='Email to someone'))
    t('a Claude Code session: its kept project shows, its title is replaced', c['project'] == 'local-models' and c['title'] == 'a Claude Code session', c)
    real = web.errors.summary
    web.errors.summary = lambda minutes=10: dict(count=2, minutes=10, last=dict(at=1, source='gateway', where='GET /x', error='a path /Users/someone'))
    try: snap = web.snapshot()
    except Exception as e: snap = {'error': repr(e)}
    web.errors.summary = real
    t('the error banner keeps its count, never the details', snap.get('errors') == dict(count=2, minutes=10), snap.get('errors'))
    t("the dashboard's data carries no list of this Mac's busiest programs (the page never drew it)", 'top' not in snap, sorted(snap))
    with open(names._present_file(), 'w') as f: json.dump(dict(until=time.time() - 1, keep=['research']), f)
    names._present['checked'] = 0.0
    t('at its end it is off by itself: names as set again', names.project('folder-123') == 'folder-123' and names.presenting() is None)
    names.present_off()
    t('off: names as set', names.project('folder-123') == 'folder-123' and names.presenting() is None)


# ---------------------------------------------------------------- the shared layer, across processes
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from localmodels import llmshare as L
what, arg = sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else '')
if what == 'gateway':
    r = L.register(dict(gen=1))
    if arg: r.update(started=round(time.time() - float(arg), 2))
    out = 'registered'
elif what == 'role': out = 'took' if L.Role(arg).try_take() else 'busy'
elif what == 'slot': out = 'took' if L.Counted('peer', 'family').take(int(arg)) is not None else 'full'
elif what == 'start': out = 'up' if L.start_server('test', 60) else 'down'
elif what == 'ctx':                                       # a gateway's request of `arg` tokens holding a slot (admission by context size)
    L.parallel_of = lambda m: 4
    with L.slot_any(['test/ctx'], wait=5, need=int(arg), fits=lambda m, load, need: load <= 0 or load + need <= 100) as (m, w):
        print('took', flush=True); sys.stdin.read()
    sys.exit(0)
print(out, flush=True)
if what != 'start': sys.stdin.read()                      # hold what it took until the test lets go
"""


def helper(state, *args, env=None):
    """A separate process on the shared state: -> (the process, what it said)."""
    p = subprocess.Popen([sys.executable, '-c', HELPER, REPO, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                         env=dict(os.environ, LOCALMODELS_STATE=state, **(env or {})))
    return p, p.stdout.readline().strip()


def stop(p, kill=False):
    if kill: p.kill()
    else:
        try: p.stdin.close()
        except OSError: pass
    try: p.wait(10)
    except subprocess.TimeoutExpired: p.kill(); p.wait()


def fresh():
    """Forget what llmshare cached about the registry (it keeps each answer for a second)."""
    llmshare._live.clear(); llmshare._gw['at'] = 0.0; llmshare._fq['at'] = 0.0


def test_shared(box):
    print('the shared layer: the registry, roles, the admission order across gateways, marks, leases, peer slots, one server start, the listing')
    from localmodels import errors, llmshare as L
    state = os.path.join(box, 'shared'); os.makedirs(state)
    old, procs = L.use_state(state), []
    real = L.parallel_of, L.LMS, L.api_models, dict(L._ps)
    try:
        print('  admission by context size across gateways: the context a slot holds is written in its lock file')
        L.parallel_of = lambda m: 4
        fit = lambda m, load, need: load <= 0 or load + need <= 100          # a budget of 100 tokens
        g, said = helper(state, 'ctx', '60'); procs.append(g)
        t('another gateway holds 60 of the instance\'s 100 tokens: every process reads it', said == 'took' and L.slot_load('test/ctx') == 60, (said, L.slot_load('test/ctx')))
        try:
            with L.slot_any(['test/ctx'], wait=1, need=60, fits=fit): took = True
        except TimeoutError: took = False
        t('a request of 60 more in this process waits (a handoff never admits twice against one budget)', not took)
        with L.slot_any(['test/ctx'], wait=2, need=30, fits=fit) as (m, w):
            t('one of 30 fits beside it, and the load counts both', m == 'test/ctx' and L.slot_load('test/ctx') == 90, L.slot_load('test/ctx'))
        t('released: its slot no longer counts', L.slot_load('test/ctx') == 60, L.slot_load('test/ctx'))
        stop(g, kill=True); procs.remove(g)
        t('a gateway killed with its slot held: the kernel frees the lock, and what its file says no longer counts', L.slot_load('test/ctx') == 0, L.slot_load('test/ctx'))
        L.parallel_of = real[0]
        t("use_state: every shared path moves, every module's too", L.SHARED == state and L.GATEWAYS == os.path.join(state, 'run', 'gateways')
          and L.LISTING == os.path.join(state, 'listing.json') and errors.FILE == os.path.join(state, 'errors.jsonl') and crashes.STATE.startswith(state))

        a, said = helper(state, 'gateway', str(4 * 3600)); procs.append(a)          # registered "four hours ago"
        b, _ = helper(state, 'gateway', '3600'); procs.append(b)                      # (a mark never predates its gateway)
        fresh(); rows = L.live_gateways()
        t('the registry: two live gateways, each by the lock it holds, with its row', said == 'registered'
          and sorted(r['pid'] for r in rows) == sorted([a.pid, b.pid]) and all(r.get('state_protocol') == L.STATE_PROTOCOL for r in rows), rows)
        t('gateway_live: True for a registered gateway, None for a pid that never registered', L.gateway_live(a.pid) is True and L.gateway_live(os.getpid()) is None)

        os.makedirs(L.INFLIGHT, exist_ok=True)
        def mark(name, **d):
            path = os.path.join(L.INFLIGHT, name); L._write(path, d); return path
        now = time.time()
        long_ = mark(f'{a.pid}-1.json', hub_pid=a.pid, since=now - 3 * 3600, state='answering', kind='batch')
        stale = mark(f'{a.pid}-2.json', hub_pid=a.pid, since=now - 60, state='answering', kind='batch'); os.utime(stale, (now - 400, now - 400))
        reused = mark(f'{a.pid}-3.json', hub_pid=a.pid, since=now - 5 * 3600, state='answering', kind='batch')
        legacy = mark(f'{os.getpid()}-1.json', hub_pid=os.getpid(), since=now - 3600, state='answering', kind='batch')
        legacy_old = mark(f'{os.getpid()}-2.json', hub_pid=os.getpid(), since=now - 3 * 3600, state='answering', kind='batch')
        fresh(); L.inflight()
        there = lambda f: os.path.exists(f)
        t("marks: a live registered gateway's mark lives past 2 h while touched", there(long_))
        t('marks: one untouched for MARK_STALE is removed, and one older than its gateway (a reused pid)', not there(stale) and not there(reused))
        t('marks: a gateway older than the registry keeps its rule: its pid alive, 2 h at most', there(legacy) and not there(legacy_old))
        os.remove(legacy)

        L.parallel_of = lambda m: 1
        M = 'test/one'
        mark(f'{a.pid}-10.json', hub_pid=a.pid, state='queued', prio=1, t0=now - 10, cands=[M], until=now + 60, n=10)
        mark(f'{b.pid}-11.json', hub_pid=b.pid, state='queued', prio=0, t0=now - 5, cands=[M], until=now + 60, n=11)
        def turn(kind, since=None, wait=0.6):
            seen = []
            try:
                with L.slot_any([M], wait=wait, kind=kind, since=since, publish=lambda c, u: seen.append((c, u))) as (got, w): return got, seen
            except TimeoutError: return None, seen
        fresh()
        got, seen = turn('batch')
        t('admission across gateways: a batch waits behind earlier waiters in other gateways', got is None and seen and seen[0][0] == [M]
          and abs(seen[0][1] - (time.time() + 0.6)) < 1.5, seen)
        t('people first: a chat waits only behind an earlier chat', turn('chat')[0] is None and turn('chat', since=now - 20)[0] == M)
        t('people first: a batch queued earliest still waits behind a chat', turn('batch', since=now - 20)[0] is None)
        L._write(os.path.join(L.INFLIGHT, f'{b.pid}-11.json'), dict(hub_pid=b.pid, state='queued', prio=1, t0=now - 10, cands=[M], until=now + 60, n=11))
        fresh(); first = min(a.pid, b.pid, os.getpid())
        t('equal since: the lower pid goes first', (turn('batch', since=now - 10)[0] == M) == (first == os.getpid()))
        for f in (f'{a.pid}-10.json', f'{b.pid}-11.json'):
            d = json.load(open(os.path.join(L.INFLIGHT, f))); d['until'] = now - 10; L._write(os.path.join(L.INFLIGHT, f), d)
        fresh()
        t('a queued mark whose until passed (by 5 s) is ignored', turn('batch')[0] == M)
        t('waiting(): every live gateway\'s queued marks, by kind', L.waiting() == dict(people=0, others=2), L.waiting())

        os.makedirs(L.LEASES, exist_ok=True)
        path = L.hub_lease_path('proj', 4242, 'peer')
        t('the hub lease is per gateway: <project>-<holder>-<front>-hub-<gateway pid>.json; holder 0 is never written',
          os.path.basename(path) == f'proj-4242-peer-hub-{os.getpid()}.json' and L.hub_lease_path('proj', 0) is None, path)
        me = os.getpid()
        for n in (f'proj-{me}-1.json', f'proj-{me}-hub.json', f'proj-{me}-hub-77.json'): open(os.path.join(L.LEASES, n), 'w').write('{}')
        t("a client's own leases leave the hub's out, in both name forms", [os.path.basename(f) for f in L.run_leases(me)] == [f'proj-{me}-1.json'])
        for n in (f'proj-{me}-1.json', f'proj-{me}-hub.json', f'proj-{me}-hub-77.json'): os.remove(os.path.join(L.LEASES, n))
        gone = subprocess.Popen([sys.executable, '-c', 'pass']); gone.wait()
        L._write(os.path.join(L.LEASES, f'proj-{gone.pid}-hub-{b.pid}.json'), dict(v=2, project='proj', pid=gone.pid, models=[M], kind='batch', since=now, via='hub', hub_pid=b.pid, beat=time.time()))
        L._write(os.path.join(L.LEASES, f'proj-{gone.pid}-9.json'), dict(v=2, project='proj', pid=gone.pid, models=[M], kind='batch', since=now, beat=time.time()))
        fresh(); ls = L.leases()
        t("a hub lease lives while its gateway does, though its client left; a run lease dies with its process",
          [d.get('via') for d in ls] == ['hub'] and not os.path.exists(os.path.join(L.LEASES, f'proj-{gone.pid}-9.json')), ls)
        stop(b, kill=True); fresh()
        t('its gateway gone, the hub lease goes too, and the registry forgets that gateway', L.leases() == [] and L.gateway_live(b.pid) is False
          and L.gateway_live(b.pid) is False and [r['pid'] for r in (fresh() or L.live_gateways())] == [a.pid])
        stop(a, kill=True); fresh(); L.inflight()
        t("a gateway gone: its marks go at once", not there(long_) and L.gateway_live(a.pid) is not True)

        role = L.Role('instance-manager')
        r1, said = helper(state, 'role', 'instance-manager'); procs.append(r1)
        t('a role has one holder: another process holds it and this one cannot take it', said == 'took' and role.holder() == r1.pid and not role.try_take())
        stop(r1, kill=True)
        t('its holder gone, the role is free and taken here', role.holder() is None and role.try_take() and role.holder() == os.getpid()
          and L.roles().get('instance-manager') == os.getpid())
        r2, said = helper(state, 'role', 'instance-manager'); procs.append(r2)
        t('while held here, another process is refused', said == 'busy')
        role.release(); stop(r2)
        t('released: nobody holds it', role.holder() is None and 'instance-manager' not in L.roles())

        took = [helper(state, 'slot', '2') for _ in range(3)]; procs += [p for p, _ in took]
        t('peer slots: three processes, cap 2: two hold one, the third is refused', sorted(w for _, w in took) == ['full', 'took', 'took'], [w for _, w in took])
        holders = [p for p, w in took if w == 'took']
        stop(holders[0], kill=True)
        c = L.Counted('peer', 'family'); fd = c.take(2)
        t('a killed holder frees its slot', fd is not None and c.held() == 2)
        p3, w3 = helper(state, 'slot', '1'); procs.append(p3)
        t('a smaller cap is respected: 2 held, cap 1: refused', w3 == 'full')
        c.give(fd)
        t('still one held, cap 1: refused', c.take(1) is None and c.held() == 1)
        stop(holders[1], kill=True)
        fd = c.take(1)
        t('none held: taken', fd is not None and c.held() == 1); c.give(fd)

        s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
        fake, log, pidf = os.path.join(box, 'lms-start'), os.path.join(box, 'lms-start.log'), os.path.join(box, 'server.pid')
        open(fake, 'w').write(f'#!/bin/sh\necho "$@" >> {log}\nsleep 1\n{sys.executable} -c "import http.server as h; '
                              f"h.HTTPServer(('127.0.0.1', {port}), h.BaseHTTPRequestHandler).serve_forever()\" >/dev/null 2>&1 &\necho $! > {pidf}\nsleep 0.5\n")
        os.chmod(fake, stat.S_IRWXU)
        env = dict(LMS=fake, LMSTUDIO_URL=f'http://127.0.0.1:{port}')
        starts = [subprocess.Popen([sys.executable, '-c', HELPER, REPO, 'start'], stdout=subprocess.PIPE, text=True,
                                   env=dict(os.environ, LOCALMODELS_STATE=state, **env)) for _ in range(2)]
        said = [p.communicate(timeout=60)[0].strip().split('\n')[-1] for p in starts]   # (the one that starts it logs a line first)
        lines = open(log).read().split('\n') if os.path.exists(log) else []
        t("LM Studio's server down in two processes at once: one 'lms server start', and both see it up",
          said == ['up', 'up'] and [l for l in lines if l] == ['server start'], (said, lines))
        try: os.kill(int(open(pidf).read()), 9)
        except (OSError, ValueError): pass

        rows2 = [dict(id=B, key=B, state='IDLE', size='1 GB', context=4096, parallel=4), dict(id=C2, key=B, state='IDLE', size='1 GB', context=4096, parallel=4)]
        L._write(L.LISTING, dict(v=1, rows=rows2, at=time.time(), good_at=time.time(), by=1))
        L._ps.update(at=0.0, rows=[], ok=False, good_at=0.0, tried=0.0); L._listing.update(checked=0.0, mtime=None)
        t('warm-up: the newest listing.json stands until the first lms ps', L.seed_listing() and [r['id'] for r in L._ps['rows']] == [B, C2] and L.read_ok())
        ps = os.path.join(box, 'lms-ps')
        open(ps, 'w').write("#!/bin/sh\necho '" + json.dumps([dict(identifier=B, modelKey=B, status='idle', sizeBytes=2**30, contextLength=4096, parallel=4)]) + "'\n")
        os.chmod(ps, stat.S_IRWXU)
        L.LMS, L.api_models = ps, (lambda timeout=3: {B: 4096, C2: 4096})
        got = L.loaded(fresh=True, force=True)
        t('a seeded listing keeps the carry-forward: an instance the server still lists stays', [r['id'] for r in got] == [B, C2], got)
        d = json.load(open(L.LISTING))
        t('each good listing is written for the other gateways', d.get('by') == os.getpid() and [r['id'] for r in d.get('rows', [])] == [B, C2], d.get('by'))
        later = time.time() + 1
        L._write(L.LISTING, dict(v=1, rows=rows2[:1], at=later, good_at=later, by=1)); L._listing.update(checked=0.0, mtime=None)
        t("a newer listing from another gateway is taken by a plain look", [r['id'] for r in L.loaded()] == [B])

        L.nudge('instance-manager'); n1 = L.nudged('instance-manager')
        t('a nudge is a touched file the role holder watches', n1 > 0 and L.nudged('peer-poller') == 0.0)
    finally:
        for p in procs:
            if p.poll() is None: stop(p, kill=True)
        L.parallel_of, L.LMS, L.api_models = real[0], real[1], real[2]; L._ps.clear(); L._ps.update(real[3])
        L.use_state(old); fresh()


def test_registry_views(box):
    print('the screens and the registry: gateway pids, the code a gateway runs, error records')
    from localmodels import dash, errors, llmshare as L
    state = os.path.join(box, 'views'); os.makedirs(state)
    old, procs = L.use_state(state), []
    try:
        g, _ = helper(state, 'gateway', '60'); procs.append(g)
        os.makedirs(L.INFLIGHT, exist_ok=True)
        L._write(os.path.join(L.INFLIGHT, f'{os.getpid()}-1.json'), dict(hub_pid=os.getpid(), since=time.time(), state='answering', kind='batch'))
        fresh(); real = dash._hub_pid
        dash._hub_pid = lambda: 999999
        pids = dash.gateway_pids()
        t('gateway pids: the registry, and a gateway older than it by its marks (never counted as a client)', g.pid in pids and os.getpid() in pids and 999999 not in pids, pids)
        stop(g, kill=True); os.remove(os.path.join(L.INFLIGHT, f'{os.getpid()}-1.json')); fresh()
        t('no gateway registered: /hub/health names it', dash.gateway_pids() == {999999})
        dash._hub_pid = real
    finally:
        for p in procs:
            if p.poll() is None: stop(p, kill=True)
        L.use_state(old); fresh()
    tree = os.path.join(box, 'release'); os.makedirs(os.path.join(tree, 'localmodels', '__pycache__')); os.makedirs(os.path.join(tree, 'bin'))
    open(os.path.join(tree, 'localmodels', 'a.py'), 'w').write('x = 1\n'); open(os.path.join(tree, 'bin', 'localmodels'), 'w').write('#!/bin/sh\n')
    d1 = dash.deploy_digest(tree)
    open(os.path.join(tree, 'localmodels', '__pycache__', 'a.cpython-39.pyc'), 'wb').write(b'cache'); open(os.path.join(tree, 'localmodels', '.DS_Store'), 'w').write('x')
    d2 = dash.deploy_digest(tree)
    open(os.path.join(tree, 'localmodels', 'a.py'), 'w').write('x = 2\n')
    t('the deploy digest: 16 hex characters, blind to __pycache__ and dotfiles, changed by any code change', len(d1) == 16 and d1 == d2 != dash.deploy_digest(tree)
      and dash.deploy_files(tree) == [os.path.join('bin', 'localmodels'), os.path.join('localmodels', 'a.py')], dash.deploy_files(tree))
    rec = errors.record('test: the record says which process', ValueError('x'), source='test')
    t('an error record says which process wrote it', rec and rec.get('pid') == os.getpid(), rec and {k: rec.get(k) for k in ('pid', 'gen')})


def test_web_state(box):
    print("the dashboard's shared files: colors.json under its lock, memory.jsonl from one sampler at a time")
    import fcntl, threading
    from localmodels import web
    hold = threading.Event()
    def holder():
        with open(web.COLORS + '.lock', 'a+') as h:
            fcntl.flock(h, fcntl.LOCK_EX); hold.set(); time.sleep(0.6)
    th = threading.Thread(target=holder); th.start(); hold.wait(5)
    t0 = time.time(); got = web.colors(['lab']); waited = time.time() - t0; th.join()
    t('colors.json: a second writer waits for the lock another process holds', waited >= 0.4 and got.get('lab'), round(waited, 2))
    web._mem.clear()
    now = time.time()
    open(web.MEMORY, 'w').write('\n'.join(json.dumps([round(now - k * 15), 50.0]) for k in (40, 30, 20)) + '\n')
    web.load_tail(); n1 = len(web._mem)
    open(web.MEMORY, 'a').write(json.dumps([round(now - 5), 51.0]) + '\n')
    web.load_tail()
    t('memory.jsonl: a gateway taking the sampler role adds only the rows newer than its own last one', n1 == 3 and len(web._mem) == 4, (n1, len(web._mem)))
    stop, real = threading.Event(), web.dash.pool
    web.dash.pool = lambda: dict(hosts=[dict(name='here', used=40.0, total=128.0)])
    th = threading.Thread(target=web.sampler, args=(stop,)); th.start(); time.sleep(0.3)
    t0 = time.time(); stop.set(); th.join(5)
    web.dash.pool = real
    t('the sampler samples at once, then returns as soon as it is stopped (not 15 s later)', not th.is_alive() and time.time() - t0 < 1 and web._mem[-1][1] == 40.0, web._mem[-1])
    web._mem.clear()


def test_pictures(box):
    print('localmodels pictures: only in screenshot mode; crop boxes from the page, at the size the shot ends at; never the person\'s Chrome profile')
    from localmodels import pictures, names
    rects = dict(vw=1512, vh=900, tile0=[16, 82, 194, 90], tile3=[629, 82, 194, 90], models=[16, 182, 703, 370])
    b = pictures.crop_box(rects, ('tile0', 'tile3'), (3024, 1800))
    t('a crop is the union of its boxes, a margin around it, at 2x: x 10..829, y 76..178 in the page',
      b == (20, 152, 1638, 204), b)
    b = pictures.crop_box(dict(rects, edge=[0, 0, 100, 50]), ('edge',), (3024, 1800))
    t('a crop at the page edge stays inside the picture and never starts at 0 (sips centres a crop offset of 0)', b[0] >= 1 and b[1] >= 1 and b[0] + b[2] <= 3024, b)
    t('a box the page did not measure: no crop, said so', pictures.crop_box(rects, ('gone',), (3024, 1800)) is None)
    args = pictures.chrome_args('/x/chrome', '/tmp/p', 1512, 900, 'light', shot='/tmp/a.png')
    t("Chrome runs headless with its own profile, never the person's", '--headless=new' in args and '--user-data-dir=/tmp/p' in args, args)
    t('light and dark are forced, not taken from the Mac',
      any('preferredColorScheme=1' in a for a in args) and '--force-dark-mode' in pictures.chrome_args('/x', '/p', 1, 1, 'dark', shot='/a.png'))
    t('every picture the README shows is made', {s[0] for s in pictures.SHOTS} | {c[0] for c in pictures.CROPS}
      == {'dashboard-dark', 'dashboard-light', 'dashboard-large-dark', 'dashboard-large-light', 'tiles-1-dark', 'tiles-2-dark', 'models-dark', 'concurrency-dark', 'clients-dark'})
    real = names.presenting
    out = os.path.join(box, 'pics'); said = []
    try:
        names.presenting = lambda: None
        code = pictures.take(out, say=said.append)
        t('screenshot mode off: refused (exit 2), nothing written, and the words say how to turn it on',
          code == 2 and not os.path.exists(out) and any('localmodels present on' in w for w in said), said)
        names.presenting = lambda: dict(until=time.time() + 60, keep=['research'])
        said.clear(); code = pictures.take(out, say=said.append)
        t('screenshot mode ending within 5 minutes: refused too (it would end mid-capture)', code == 2 and not os.path.exists(out), said)
    finally: names.presenting = real


def test_affinity():
    print("prefix affinity: a conversation's key from its opening, in every API's shape; the copy that served it first, if it is a candidate")
    from localmodels import affinity
    affinity._seen.clear()
    sys_, u1, u2 = {'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'task 1'}, {'role': 'user', 'content': 'task 2'}
    more = [{'role': 'assistant', 'content': 'a'}, {'role': 'user', 'content': 'b'}]
    k = affinity.key(dict(model=B, messages=[sys_, u1]))
    t('chat: every turn of one conversation has its key', k and affinity.key(dict(model=B, messages=[sys_, u1] + more)) == k)
    t('chat: another task under the same system prompt, or another model, has another key',
      affinity.key(dict(model=B, messages=[sys_, u2])) != k and affinity.key(dict(model='other/model', messages=[sys_, u1])) != k)
    t('chat: leading system and developer messages are all part of the opening',
      affinity.key(dict(model=B, messages=[sys_, {'role': 'developer', 'content': 'D'}, u1])) != k)
    a = affinity.key(dict(model=B, system='S', messages=[u1])); a2 = affinity.key(dict(model=B, system='S', messages=[u1] + more))
    t('Anthropic messages: the system field and the first message', a and a == a2 and affinity.key(dict(model=B, system='T', messages=[u1])) != a)
    r = affinity.key(dict(model=B, instructions='S', input=[u1])); r2 = affinity.key(dict(model=B, instructions='S', input=[u1] + more))
    t('Responses: instructions and the first input item', r and r == r2)
    long_ = 'x' * affinity.PREFIX_CHARS
    t('a prompt as one string: its first PREFIX_CHARS characters', affinity.key(dict(model=B, prompt=long_ + 'tail one')) == affinity.key(dict(model=B, prompt=long_ + 'tail two'))
      and affinity.key(dict(model=B, input='hello')) != affinity.key(dict(model=B, input='hullo')))
    t('nothing to key on: None', affinity.key(dict(model=B, messages=[])) is None and affinity.key(dict(model=B)) is None and affinity.key('x') is None
      and affinity.key(dict(model=B, messages=[{'role': 'user', 'content': ''}])) is None)
    t('the key is a digest: no prompt text in it', len(k) == 24 and 'task' not in k)
    t('nothing remembered: the order is kept', affinity.order(k, [B, C2]) == [B, C2])
    affinity.remember(k, C2)
    t('remembered: its copy first, the rest in their order', affinity.order(k, [B, C2, 'm on peer']) == [C2, B, 'm on peer'])
    t('its copy not a candidate (down, draining, stalled, skipped): the order is kept', affinity.order(k, [B, 'm on peer']) == [B, 'm on peer'])
    affinity._seen[k] = (C2, time.time() - affinity.TTL - 1)
    t('after TTL it is forgotten', affinity.preferred(k) is None and k not in affinity._seen)
    size = affinity.SIZE; affinity.SIZE = 3
    for i in range(5): affinity.remember(f'k{i}', B)
    t('at most SIZE conversations, the least recently used dropped first', list(affinity._seen) == ['k2', 'k3', 'k4'])
    affinity.SIZE = size; affinity._seen.clear()


def main():
    box = tempfile.mkdtemp(prefix='localmodels-rulestest-')
    try:
        sandbox(box)
        test_recycle()
        test_affinity()
        test_admission(box)
        test_listing(box)
        test_server_probe(box)
        test_unload(box)
        test_keep(box)
        test_swap_away(box)
        test_recycle_resumed(box)
        test_prune(box)
        test_settings(box)
        test_write_problem(box)
        test_cli_args()
        test_install_dry(box)
        test_first_settings()
        test_runs()
        test_maintenance(box)
        test_switch_off(box)
        test_watchdog()
        test_stalls(box)
        test_peers_only(box)
        test_privacy_check(box)
        test_secret_check(box)
        test_about(box)
        test_progress(box)
        test_run_plans(box)
        test_sessions(box)
        test_names(box)
        test_shared(box)
        test_web_state(box)
        test_registry_views(box)
        test_pictures(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    t('no hub path points into the live state folder (tests/_sandbox.py)', not _sandbox.stray(), _sandbox.stray())
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
