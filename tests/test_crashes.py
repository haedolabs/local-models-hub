#!/usr/bin/env python3
"""The crash rules (localmodels/crashes.py, models.keep, the door's hold), tested in a sandbox: LM Studio's own log lines
from an engine crash built in, a pretend LM Studio, the hub's records in a temporary folder. It never loads or unloads a model and
never touches the hub's real records. `localmodels check` runs it; so can you: python3 tests/test_crashes.py
"""
import http.server, json, os, shutil, sys, tempfile, threading, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from localmodels import crashes, gateway, llmshare, models

# LM Studio's log around an engine crash (trimmed), then a load of the model and, from an earlier day, an unload
CRASH_LOG = """[2026-11-07 04:14:59][DEBUG][model_kit][ERROR]: Encountered fatal exception in the backend generation thread: Traceback (most recent call last):
  File "/Users/x/.lmstudio/extensions/backends/vendor/_amphibian/app-mlx-generate-mac26-arm64@41/lib/python3.11/site-packages/mlx_engine/model_kit/batched_vision/batch_generator.py", line 488, in _step
    mx.async_eval(*eval_targets)
RuntimeError: [metal::malloc] Resource limit (499000) exceeded.
[2026-11-07 04:14:59][DEBUG] Fatal Python error: Segmentation fault
""" + """[2026-11-07 04:15:00][ERROR][qwen/qwen3.6-35b-a3b] Error: Channel Error
    at _0x39cfde.chatCompletionSync (/Applications/LM Studio.app/Contents/Resources/app/.webpack/main/index.js:1129:10808)
- Caused By: Error: The model has crashed without additional information. (Exit code: null)
    at _0x4eb4f4.clientPortOnCloseErrorFactory (/Applications/LM Studio.app/Contents/Resources/app/.webpack/main/index.js:273:1711)
""" * 4 + """[2026-11-07 04:15:00][DEBUG] Exception: Encountered fatal exception in the backend generation thread: Traceback (most recent call last):
    mx.async_eval(*eval_targets)
RuntimeError: [metal::malloc] Resource limit (499000) exceeded.
Fatal Python error: Segmentation fault
""" + """[2026-11-07 04:15:01][ERROR][qwen/qwen3.6-35b-a3b:2] Error: Channel Error
    at _0x39cfde.chatCompletionSync (/Applications/LM Studio.app/Contents/Resources/app/.webpack/main/index.js:1129:10808)
- Caused By: Error: The model has crashed without additional information. (Exit code: null)
""" * 4 + """[2026-11-07 09:51:50][INFO][LMSAuthenticator][Client=lms-cli][Endpoint=loadModel] Loading model: qwen/qwen3.6-35b-a3b
[2026-11-07 09:51:58][INFO][LMSAuthenticator][Client=lms-cli][Endpoint=loadModel] Loading model: qwen/qwen3.6-35b-a3b
"""
UNLOAD = """[2026-11-06 00:27:57][INFO][LMSAuthenticator][Client=lms-cli][Endpoint=unloadModel] Unloading model: qwen/qwen3.6-35b-a3b
[2026-11-06 00:27:57][ERROR][qwen/qwen3.6-35b-a3b] Error: Channel Error
- Caused By: ExplicitModelUnloadError: Model unloaded.
"""
B, C2 = 'qwen/qwen3.6-35b-a3b', 'qwen/qwen3.6-35b-a3b:2'
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))


def main():
    box = tempfile.mkdtemp(prefix='localmodels-crashtest-')
    try: run(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


def run(box):
    for name in ('SLOTS', 'LEASES', 'INFLIGHT'): setattr(llmshare, name, os.path.join(box, name.lower()))
    llmshare.REQUESTS, llmshare.WAITS = os.path.join(box, 'requests.jsonl'), os.path.join(box, 'waits.jsonl')
    crashes.STATE, crashes.RECORD, crashes.LOGS = os.path.join(box, 'keeper.json'), os.path.join(box, 'crashes.jsonl'), os.path.join(box, 'logs')
    os.makedirs(os.path.join(crashes.LOGS, '2026-11'))
    llmshare.api_models, llmshare.read_ok, llmshare.busy_slots = (lambda timeout=3: None), (lambda: True), (lambda: {})   # no real LM Studio in the sandbox
    llmshare.settings = lambda: dict(families={B: dict(key=B, copies=2, context=65536, parallel=4, recycle=False)}, batch_at_once=8, headroom_gb=8)
    models.ensure_server, crashes.MISS = (lambda: True), 0
    at = lambda s: time.mktime(time.strptime('2026-11-07 ' + s, '%Y-%m-%d %H:%M:%S'))
    with open(llmshare.REQUESTS, 'w') as f:                  # the 12 requests that failed at the crash
        for k in range(12): f.write(json.dumps(dict(at=at('04:15:00') + k * 2, project='proj', model=B, copy=C2 if k % 2 else B, status=400)) + '\n')
    YEARS = 3650

    print("LM Studio's log of a crash")
    c, events, fatal = crashes.parse((CRASH_LOG + UNLOAD).split('\n'), {})
    t('both copies crashed, nothing else', sorted({x['model'] for x in c}) == [B, C2] and len(c) == 8, len(c))
    t("the engine's error is kept", all(x['error'] == 'RuntimeError: [metal::malloc] Resource limit (499000) exceeded.' for x in c), c[0]['error'])
    t("LM Studio's words are kept", all(x['cause'] == 'The model has crashed without additional information. (Exit code: null)' for x in c))
    t('an unload is not a crash', not any(x['at'] < at('00:00:00') for x in c))
    t('the loads and unloads are seen', [v for _, v, _ in events] == ['loadModel', 'loadModel', 'unloadModel'], events)
    c2, ev2, _ = crashes.parse(['[2026-11-07 11:53:02][INFO] Server started.'], {})
    t("a restart of LM Studio's server is seen", [v for _, v, _ in ev2] == ['serverStart'] and not c2, ev2)

    print('scan: a crash from before the hub looked')
    open(os.path.join(crashes.LOGS, '2026-11', '2026-11-07.3.log'), 'w').write(CRASH_LOG)
    t('recorded once per copy', crashes.scan(YEARS) == 2)
    t('a second scan adds nothing', crashes.scan(YEARS) == 0)
    h = crashes.history(YEARS)
    t('one crash, both copies', len(h) == 1 and h[0]['which'] == 'both replicas', h and h[0]['which'])
    t('back at 09:51:50, 12 requests failed', h and h[0]['back'] == at('09:51:50') and h[0]['failed'] == 12, h and crashes.words(h[0]))

    print('the keeper')
    for f in (crashes.RECORD, crashes.STATE):
        if os.path.exists(f): os.remove(f)
    log = os.path.join(crashes.LOGS, '2026-11', '2026-11-07.4.log'); open(log, 'w').write('')
    row = lambda i: dict(id=i, key=B, state='IDLE', size='37.75 GB', context=65536, parallel=4)
    stamp = lambda dt=0: time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() + dt))
    def crash(ids, dt=0):                                    # LM Studio stamps the crash now (dt is only kept for the reader); a
        time.sleep(1.1)                                      # second passes first, so a crash after a reload is in a new life
        s = f'RuntimeError: [metal::malloc] Resource limit (499000) exceeded.\n[{stamp()}][DEBUG] Fatal Python error: Segmentation fault\n'
        for i in ids: s += f'[{stamp()}][ERROR][{i}] Error: Channel Error\n- Caused By: Error: The model has crashed without additional information. (Exit code: null)\n' * 2
        open(log, 'a').write(s)
    def hour_passes():
        with crashes._state() as st: st['reloads'] = {}
    done, free = [], [100.0]
    models.load = lambda key, ident, context, parallel: (done.append((ident, context, parallel)), (True, 'ok'))[1]
    models.free_gb, models._lms = lambda: free[0], lambda *a, **k: (True, '')
    real = llmshare.loaded, llmshare.parallel_of
    llmshare.loaded = lambda fresh=False, force=False: [row(i) for i, _, _ in done]
    crashes._ctx.clear()
    fresh = lambda rows: crashes.watch(rows, listed_at=time.time() + 3)   # a listing taken after the crash just written

    t('a first look starts at the end of the log', crashes.watch([row(B), row(C2)]) == [])
    crash([B, C2], 0); jobs = fresh([])
    t('both copies crashed: load both again, as models.json says', jobs and jobs[0]['ids'] == [B, C2] and (jobs[0]['context'], jobs[0]['parallel']) == (65536, 4), jobs)
    t('meanwhile the door holds requests for it', crashes.reloading(B) and crashes.reloading(C2) and not crashes.reloading('other/model')
      and crashes.down_copies(B) == {B, C2})
    models._again(jobs[0], 8)
    t('both loaded again, and the hold ends', done == [(B, 65536, 4), (C2, 65536, 4)] and not crashes.reloading(B), done)
    t('the record says the hub did it', crashes.history(1)[0]['then'].startswith('reloaded by the instance manager'), crashes.history(1)[0]['then'])
    crashes.watch([row(B), row(C2)])

    crash([B, C2], 200); jobs = crashes.watch([row(B), row(C2)], listed_at=time.time() - 60)
    t('a crash newer than the listing in hand is neither closed as back nor acted on: the next listing decides',
      jobs == [] and crashes.reloading(B) and crashes.stale_down(time.time() - 60), jobs)
    jobs = fresh([])
    t('the next listing, without the copies: load them again', jobs and jobs[0]['ids'] == [B, C2], jobs)
    done.clear(); free[0] = 20.0
    models._again(jobs[0], 8)
    t('no room: it waits, and requests still wait', not done and crashes.reloading(B) and crashes._read(fresh=True)['down'][B]['state'] == 'waiting for room')
    free[0] = 100.0; models._again(fresh([])[0], 8); crashes.watch([row(B), row(C2)])
    t('room again: loaded', len(done) == 2 and not crashes.reloading(B))

    crash([C2], 260); jobs = fresh([row(B)])
    t('a second crash within two minutes of a reload is a new crash', jobs and jobs[0]['ids'] == [C2] and crashes.reloading(C2), jobs)
    done.clear(); models._again(jobs[0], 8); crashes.watch([row(B), row(C2)])

    crash([B, C2], 600); jobs = fresh([])
    t('a crash after 3 reloads within the hour: left down, no hold', jobs == [] and crashes._read(fresh=True)['down'][B]['state'] == 'left down' and not crashes.reloading(B)
      and crashes.left_down(B))
    t('the dash says how to load it by hand', 'localmodels use ' + B in crashes.down()[0]['words'], crashes.down()[0]['words'])
    hour_passes()
    with crashes._state() as st: st['down'][B]['retry_at'] = time.time() - 4000
    jobs = fresh([])
    t('an hour later the keeper tries once more', jobs and jobs[0]['ids'] == [B, C2] and crashes.reloading(B), jobs)
    crashes.swapped(B)
    t('localmodels use ends it', not crashes._read(fresh=True).get('down'))
    crashes.watch([row(B), row(C2)]); hour_passes()

    for ids in ([B], [B], [C2], [C2]):                            # the cap is per copy: two crashes of each within the hour
        crash(ids, 0); jobs = fresh([row(i) for i in (B, C2) if i not in ids]); done.clear(); models._again(jobs[0], 8); crashes.watch([row(B), row(C2)])
    st = crashes._read(fresh=True)
    t('two crashes of each copy within the hour: each loaded again (the cap counts one copy, not the family)',
      not st.get('down') and len(st['reloads'].get(C2) or []) == 2 and len(st['reloads'].get(B) or []) == 2, {k: len(v) for k, v in st.get('reloads', {}).items()})
    crash([C2], 0); jobs = fresh([row(B)]); done.clear(); models._again(jobs[0], 8); crashes.watch([row(B), row(C2)])
    crash([C2], 0); jobs = fresh([row(B)])
    t('a fourth crash of the same copy within the hour: left down (copy 1 keeps answering: the door routes to it)',
      jobs == [] and crashes._read(fresh=True)['down'][B]['state'] == 'left down' and 'one instance' in crashes._read(fresh=True)['down'][B]['why'], crashes._read(fresh=True)['down'].get(B, {}).get('why'))
    crashes.swapped(B); crashes.watch([row(B), row(C2)]); hour_passes()

    open(log, 'a').write(UNLOAD.replace('2026-11-06 00:27:57', stamp(700)).replace(f'model: {B}', f'model: {C2}').replace(f'[{B}]', f'[{C2}]'))
    t('copy 2 unloaded by hand: left alone', crashes.watch([row(B)]) == [] and not crashes._read(fresh=True).get('down'))
    open(log, 'a').write(f'[{stamp(800)}][DEBUG] Fatal Python error: Segmentation fault\n')
    jobs = crashes.watch([])
    t('copy 1 gone as its engine died, nobody unloaded it: a crash', jobs and jobs[0]['ids'] == [B, C2], jobs)
    crashes.swapped('another/model')
    t('a swap to another model ends the wait', not crashes._read(fresh=True).get('down') and crashes.records(1)[-1]['event'] in ('cancelled', 'crash'))
    crashes.watch([row(B), row(C2)]); hour_passes()
    log = os.path.join(crashes.LOGS, '2026-11', '2026-11-07.5.log'); open(log, 'w').write('')
    crash([C2], 900); jobs = fresh([row(B)])
    t('the log rolled to a new file; copy 2 alone: copy 2 again, copy 1 answers meanwhile', jobs and jobs[0]['ids'] == [C2] and crashes.down_copies(B) == {C2}, jobs)
    done.clear(); models._again(jobs[0], 8); crashes.watch([row(B), row(C2)]); hour_passes()
    llmshare.api_models = lambda timeout=3: {B: 65536, C2: 65536}
    t("a listing that lost a copy LM Studio's server still lists: nothing is missing", crashes.watch([row(B)]) == [] and crashes.confirm_missing([C2], [row(B)]) == set()
      and not crashes._read(fresh=True).get('down'))
    llmshare.api_models = lambda timeout=3: None
    open(log, 'a').write(f'[{stamp(1000)}][INFO] Server started.\n')
    jobs = crashes.watch([row(B)])
    t("LM Studio restarted and a copy of a family is not back: loaded again", jobs and jobs[0]['ids'] == [C2], jobs)
    llmshare.loaded, llmshare.parallel_of = real

    print("the door's hold")
    class Fake(http.server.BaseHTTPRequestHandler):
        status = 200
        def log_message(self, *a): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            out = json.dumps(dict(model=body['model'], choices=[dict(message=dict(content='OK'))], usage=dict(prompt_tokens=3, completion_tokens=1))).encode()
            self.send_response(Fake.status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out)))
            self.end_headers(); self.wfile.write(out)
    up = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake); threading.Thread(target=up.serve_forever, daemon=True).start()
    door = gateway.Server(('127.0.0.1', 0), gateway.Door); threading.Thread(target=door.serve_forever, daemon=True).start()
    gateway.UP, gateway.PORT = f'http://127.0.0.1:{up.server_address[1]}', door.server_address[1]
    M, rows = 'test/fake-model', []
    llmshare.loaded, llmshare.parallel_of = (lambda fresh=False, force=False: list(rows)), (lambda m: 2)
    def ask():
        req = urllib.request.Request(f'http://localhost:{gateway.PORT}/v1/chat/completions', data=json.dumps(dict(model=M, messages=[])).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Project': 'local-models', 'X-Kind': 'test'})
        try: r = urllib.request.urlopen(req, timeout=60); return r.status
        except urllib.error.HTTPError as e: return e.code
    try:
        with crashes._state() as st: st['down'] = {M: dict(crashed=time.time(), models=[M], state='loading again')}
        got, t0 = {}, time.time()
        th = threading.Thread(target=lambda: got.update(code=ask(), seconds=time.time() - t0)); th.start()
        time.sleep(2.5)
        marks = llmshare.inflight()
        t('a request for a model loading again is held', marks and marks[0].get('state') == 'held: reloading after a crash', marks)
        rows.append(dict(id=M, key=M, state='IDLE', size='1 GB', context=4096, parallel=2)); crashes.loaded(M, [M], True, 2.5)
        th.join(30); time.sleep(0.5)
        last = json.loads(open(llmshare.REQUESTS).read().strip().split('\n')[-1])
        t('then answered once it is back', got.get('code') == 200 and 2.5 <= got.get('seconds', 0) < 8, f"{got.get('seconds', 0):.1f} s")
        t('the request log counts the hold as waiting', last.get('held', 0) >= 2.5 and last['waited'] >= last['held'], {k: last.get(k) for k in ('held', 'waited')})
        crashes.WAKE.clear(); Fake.status = 400
        code = ask(); time.sleep(0.5)
        t('a failed request passes through and wakes the keeper', code == 400 and crashes.WAKE.is_set())
    finally:
        door.shutdown(); up.shutdown()
        llmshare.loaded, llmshare.parallel_of = real


if __name__ == '__main__':
    sys.exit(main())
