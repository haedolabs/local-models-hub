#!/usr/bin/env python3
"""The sharing and keeping rules, tested in a sandbox (temporary folders, a pretend LM Studio, no model
ever loaded or unloaded): the recycle policy, the door's queue with people first, hold-and-resend across a crash, the
server-start guard, a partial model listing, a client that gives up, the agent engine's retries. `localmodels check` runs it;
so can you: python3 tests/test_rules.py
"""
import json, os, shutil, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from localmodels import crashes, llmshare, recycle

B, C2 = 'qwen/qwen3.6-35b-a3b', 'qwen/qwen3.6-35b-a3b:2'
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))


def sandbox(box):
    for name in ('SLOTS', 'LEASES', 'INFLIGHT'): setattr(llmshare, name, os.path.join(box, name.lower()))
    llmshare.api_models = lambda timeout=3: None              # no real LM Studio in the sandbox (a test that needs a listing sets one)
    from localmodels import names as _names, peers as _peers           # never this Mac's live settings, names or peers
    llmshare.MODELS, _names.FILE = os.path.join(box, 'models.json'), os.path.join(box, 'names.json')   # (the repo's defaults apply)
    _peers.STATE, _peers.TOKEN_FILE = os.path.join(box, 'link.json'), os.path.join(box, 'peer-token')
    llmshare.REQUESTS, llmshare.WAITS = os.path.join(box, 'requests.jsonl'), os.path.join(box, 'waits.jsonl')
    crashes.STATE, crashes.RECORD, crashes.LOGS = os.path.join(box, 'keeper.json'), os.path.join(box, 'crashes.jsonl'), os.path.join(box, 'logs')
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
    llmshare.settings = real


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
    real = L.LMSTUDIO, models._lms, models.time.sleep
    calls = []
    models._lms = lambda *a, **k: (calls.append(a), (True, ''))[1]
    L.LMSTUDIO = f'http://127.0.0.1:{srv.server_address[1]}'
    t('the server answers: no start', models.ensure_server() and not calls)
    Slow.delay = 4.0
    t('the server is slow (busy): no start', models.ensure_server() and not calls)
    Slow.delay = 0.0; srv.shutdown(); port = srv.server_address[1]; srv.server_close()
    L.LMSTUDIO = f'http://127.0.0.1:{port}'
    models.time.sleep = lambda s: None
    up = models.ensure_server()
    t('the connection is refused: one start, and the truth that it is still down', not up and [c[:2] for c in calls] == [('server', 'start')], calls)
    L.LMSTUDIO, models._lms, models.time.sleep = real


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
    L.loaded, L.api_models, L.busy_slots, L.settings, models.load, models.unload, models._lms, models.free_gb, models.ensure_server, crashes.MISS, L.SWAP_LOCK, L.read_ok = real
    with crashes._state() as st: st['down'] = {}


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
    L.MODELS = real[0]; L._conf.update(real[1])


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


def test_privacy_check(box):
    print('publish: the privacy check reads what is new since the last release against a word list kept outside the repo')
    import subprocess
    from localmodels import cli
    repo = os.path.join(box, 'repo'); os.makedirs(repo)
    g = lambda *a: subprocess.run(['git', '-C', repo, *a], capture_output=True, text=True, check=True).stdout
    g('init', '-q'); g('config', 'user.email', 't@example.org'); g('config', 'user.name', 'T')
    open(os.path.join(repo, 'a.txt'), 'w').write('old line written by Pat Sample\n')
    g('add', '.'); g('commit', '-q', '-m', 'first'); g('tag', '-a', 'v1.0.0', '-m', 'release')
    words = os.path.join(box, 'words.txt')
    open(words, 'w').write('# a comment\npat\nsample\n\\bzephyr\\b\nproject-x\nallow: zephyr-free\nallow: sample\\.org/pat\n')
    with open(os.path.join(repo, 'a.txt'), 'a') as f: f.write('this is zephyr-free and fine\nthe zephyr notes\nsee sample.org/pat for more\n')
    open(os.path.join(repo, 'project-x-notes.md'), 'w').write('nothing here\n')
    g('add', '.'); g('commit', '-q', '-m', 'notes for Pat')
    r = cli.private_check(repo, words)
    where, texts = [h[0] for h in r['hits']], [h[1] for h in r['hits']]
    t('a line from before the last release is not read again', not any('old line' in x for x in texts), r['hits'])
    t('a new line holding a word is caught', any('the zephyr notes' in x for x in texts), r['hits'])
    t('an allowed phrase and an allowed address pass', not any('zephyr-free' in x or 'sample.org/pat' in x for x in texts), r['hits'])
    t('a new file whose name holds a word is caught', any('project-x-notes.md' in w for w in where), where)
    t('a commit message is read too (it goes public with the history)', any(w.startswith('commit ') for w in where), where)
    t('it says what it read: since the last release tag, so many lines, files and messages',
      r['status'] == 'hits' and r['since'] == 'v1.0.0' and r['lines'] == 4 and r['files'] == 1 and r['commits'] == 1, {k: r[k] for k in ('status', 'since', 'lines', 'files', 'commits')})
    t('no word list: it says the check was skipped, never a silent pass', cli.private_check(repo, os.path.join(box, 'none.txt'))['status'] == 'skipped')
    open(words, 'w').write('nothing-matches-this\n[unclosed\n')
    t('nothing new holds a word: clean (a line that is not a valid pattern is read as plain text)', cli.private_check(repo, words)['status'] == 'clean')


def test_about(box):
    print("the dashboard's corner: the version, the license and the source, read from the hub's own files at run time")
    from localmodels import NAME, VERSION, web
    home = os.path.join(box, 'about'); os.makedirs(os.path.join(home, '.claude-plugin'))
    open(os.path.join(home, 'LICENSE'), 'w').write('MIT License\n\nCopyright (c) 2026 Pat Sample\n\nPermission is hereby granted...\n')
    open(os.path.join(home, '.claude-plugin', 'plugin.json'), 'w').write(json.dumps(dict(name='x', repository='https://example.org/pat/hub')))
    a = web.about(home)
    t('version, license, copyright and source, as the files say', a == dict(name=NAME, version=VERSION, license='MIT License', copyright='© 2026 Pat Sample',
      source='https://example.org/pat/hub'), a)
    t('with neither file (an old install), the version alone', web.about(os.path.join(box, 'nowhere')) == dict(name=NAME, version=VERSION, license=None, copyright=None, source=None))


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
    t('private: no Claude session title', s['project'] == 'casework' and s['title'] is None, s)
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


def main():
    box = tempfile.mkdtemp(prefix='localmodels-rulestest-')
    try:
        sandbox(box)
        test_recycle()
        test_listing(box)
        test_server_probe(box)
        test_unload(box)
        test_keep(box)
        test_prune(box)
        test_settings(box)
        test_install_dry(box)
        test_first_settings()
        test_runs()
        test_maintenance(box)
        test_privacy_check(box)
        test_about(box)
        test_names(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
