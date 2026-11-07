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
    fam = {B: dict(key=B, copies=2, context=65536, parallel=4, recycle=dict(tokens=800000, soft=560000, before_batch=0.5))}
    llmshare.settings = lambda: dict(families=fam, batch_at_once=8, headroom_gb=8)
    since = time.time() - 3 * 3600
    reset(since)
    rows, busy = [row(B), row(C2)], {llmshare._safe(B): 2, llmshare._safe(C2): 1}
    t('settings: off, partial, default', recycle.thresholds(dict(recycle=False)) is None and recycle.thresholds(dict(recycle=dict(tokens=50)))['soft'] == 560000
      and recycle.thresholds({})['tokens'] == 800000 and recycle.thresholds({}).get('requests') == 0)
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
    t('then the copy past half its band is recycled first', j and j['copy'] == B and 'batch is starting' in j['why'], j and j['why'])
    with crashes._state() as st: st['recycle']['job']['since'] = time.time() - 700
    w = recycle.give_up_if_stuck()
    t('a drain stuck for 10 minutes is given up and tried later', recycle.job() is None and crashes._read(fresh=True)['recycle']['retry_after'] > time.time() and 'waits' in (w or ''), w)
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
    models.load = lambda key, ident, context, parallel: (loads.append(ident), crashes.loaded_now([ident]), (True, 'ok'))[2]
    models.unload = lambda ident: (unloads.append(ident), (True, ''))[1]
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
    L.LMS = os.path.join(box, 'no-such-lms')
    t('a missing lms is noticed', cli.lms_found() is False)
    cli.PLIST, L.MODELS, L.LMS = real


def main():
    box = tempfile.mkdtemp(prefix='localmodels-rulestest-')
    try:
        sandbox(box)
        test_recycle()
        test_listing(box)
        test_server_probe(box)
        test_keep(box)
        test_prune(box)
        test_settings(box)
        test_install_dry(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
