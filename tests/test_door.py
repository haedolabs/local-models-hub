#!/usr/bin/env python3
"""The door's rules (localmodels/gateway.py), tested against a pretend LM Studio on a random port: hold-and-resend across a
crash-shaped failure (LM Studio's words, a dropped connection, or a copy gone from the listing), 503 with Retry-After when
the resend fails too, a genuine 400 passed through, routing around a copy that is down or draining, a named copy that is
not there, nothing loaded, a hold that runs out, a program that hangs up while it waits or during the answer, people
before batches in the queue, a chunked request body, a route with a trailing slash, a model left down. Nothing real is
loaded or unloaded. `localmodels check` runs it; so can you: python3 tests/test_door.py
"""
import http.server, json, os, shutil, socket, sys, tempfile, threading, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from localmodels import crashes, gateway, llmshare, recycle

F, F2 = 'test/family', 'test/family:2'
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))


class Fake(http.server.BaseHTTPRequestHandler):
    """A pretend LM Studio: answers by a plan ('ok', 'crash', 'bad', 'odd', 'slow', 'reset'), and remembers who asked, in order."""
    plan, seen, slow, on_odd = [], [], 3.0, None
    lock = threading.Lock()

    def log_message(self, *a): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with Fake.lock:
            what = Fake.plan.pop(0) if Fake.plan else 'ok'
            Fake.seen.append((body.get('model'), body.get('tag'), what))
        if what == 'reset':                                   # the server drops the connection before any answer
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.close_connection = True; return
        if what == 'slow': time.sleep(Fake.slow)
        if what == 'sse-cut':                                 # a stream that breaks off after one chunk, no [DONE]
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"One"}}]}\n\n'); self.wfile.flush()
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.close_connection = True; return
        if what == 'crash':
            out, code = json.dumps(dict(error='Error: The model has crashed without additional information. (Exit code: null)')).encode(), 400
        elif what == 'bad':
            out, code = json.dumps(dict(error=dict(message="Invalid 'messages': must be an array", type='invalid_request_error'))).encode(), 400
        elif what == 'odd':
            if Fake.on_odd: Fake.on_odd()                     # the copy dies as it answers, with words the door has not seen
            out, code = json.dumps(dict(error='something new')).encode(), 400
        else:
            out, code = json.dumps(dict(model=body.get('model'), choices=[dict(message=dict(content='OK ' + str(body.get('tag'))))],
                                        usage=dict(prompt_tokens=3, completion_tokens=2))).encode(), 200
        try:
            self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out)))
            self.end_headers(); self.wfile.write(out)
        except OSError: pass                                  # the door hung up on us (its program left)


def main():
    box = tempfile.mkdtemp(prefix='localmodels-doortest-')
    try: run(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


def run(box):
    for name in ('SLOTS', 'LEASES', 'INFLIGHT'): setattr(llmshare, name, os.path.join(box, name.lower()))
    llmshare.REQUESTS, llmshare.WAITS = os.path.join(box, 'requests.jsonl'), os.path.join(box, 'waits.jsonl')
    crashes.STATE, crashes.RECORD, crashes.LOGS = os.path.join(box, 'keeper.json'), os.path.join(box, 'crashes.jsonl'), os.path.join(box, 'logs')
    crashes._seen_state.update(at=0.0, st={})
    row = lambda i: dict(id=i, key=F, state='IDLE', size='1 GB', context=4096, parallel=1)
    rows = [row(F), row(F2)]
    llmshare.loaded, llmshare.parallel_of, llmshare.read_ok = (lambda fresh=False, force=False: list(rows)), (lambda m: 1), (lambda: True)
    llmshare.api_models = lambda timeout=3: {r['id']: 4096 for r in rows}
    llmshare.settings = lambda: dict(families={F: dict(key=F, copies=2, context=4096, parallel=1, recycle=False)}, batch_at_once=2, headroom_gb=1)
    up = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake); threading.Thread(target=up.serve_forever, daemon=True).start()
    door = gateway.Server(('127.0.0.1', 0), gateway.Door); threading.Thread(target=door.serve_forever, daemon=True).start()
    gateway.UP, gateway.PORT = f'http://127.0.0.1:{up.server_address[1]}', door.server_address[1]
    url = f'http://localhost:{gateway.PORT}'

    def ask(model=F, tag=None, kind='test', timeout=60, path='/v1/chat/completions', headers=None):
        req = urllib.request.Request(url + path, data=json.dumps(dict(model=model, messages=[], tag=tag)).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Project': 'local-models', 'X-Kind': kind, **(headers or {})})
        try: r = urllib.request.urlopen(req, timeout=timeout); return r.status, json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e: return e.code, None, dict(e.headers)

    def rows_logged(n=1):
        time.sleep(0.4)
        lines = open(llmshare.REQUESTS).read().strip().split('\n')
        return [json.loads(l) for l in lines[-n:]]

    def plan(*what):
        with Fake.lock: Fake.plan[:] = list(what); Fake.seen.clear()

    def raw(model, tag, kind='batch', close_after=None, stream=False):
        """A request sent by hand over a socket; close_after: hang up that many seconds later (a program giving up)."""
        s = socket.create_connection(('127.0.0.1', gateway.PORT))
        body = json.dumps(dict(model=model, messages=[], tag=tag, stream=stream)).encode()
        s.sendall(b'POST /v1/chat/completions HTTP/1.0\r\nHost: localhost:%d\r\nX-Kind: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % (gateway.PORT, kind.encode(), len(body)) + body)
        if close_after is not None: time.sleep(close_after); s.close(); return None
        return s

    try:
        print("the door: a crash-shaped failure is held and resent")
        plan('crash', 'ok')
        code, r, h = ask(tag='a')
        row_ = rows_logged()[0]
        t('LM Studio says the model crashed: the request is sent again and answered', code == 200 and r and r['choices'][0]['message']['content'] == 'OK a'
          and [s[2] for s in Fake.seen] == ['crash', 'ok'], (code, Fake.seen))
        t('the log says it took two tries, in classified words', row_.get('tries') == 2 and row_.get('note') == 'LM Studio answered 400: the model crashed', {k: row_.get(k) for k in ('tries', 'note', 'status')})
        plan('crash', 'crash')
        code, r, h = ask(tag='b')
        t('failing twice: 503 with Retry-After, never LM Studio\'s 400', code == 503 and h.get('Retry-After') == str(gateway.RETRY_AFTER), (code, h.get('Retry-After')))
        plan('bad')
        code, r, h = ask(tag='c')
        t('a genuine 400 passes through once', code == 400 and len(Fake.seen) == 1, (code, Fake.seen))
        plan('reset', 'ok')
        code, r, h = ask(tag='r')
        row_ = rows_logged()[0]
        t('LM Studio drops the connection before answering: sent again, answered, no server start', code == 200 and row_.get('tries') == 2 and 'dropped' in (row_.get('note') or ''), (code, row_.get('note')))
        plan('odd', 'ok')
        Fake.on_odd = lambda: rows.pop(0) if rows and rows[0]['id'] == F else None
        code, r, h = ask(tag='s')
        Fake.on_odd = None
        row_ = rows_logged()[0]
        t('a 400 with unknown words from a copy that is gone from the listing: treated as a crash, answered by the other copy',
          code == 200 and Fake.seen[-1][0] == F2 and 'gone' in (row_.get('note') or ''), (code, Fake.seen, row_.get('note')))
        rows.insert(0, row(F))

        print('the door: copies that are down, draining, missing or left down')
        plan()
        with crashes._state() as st: st['down'] = {F: dict(crashed=time.time(), models=[F], state='loading again')}
        code, r, h = ask(tag='d')
        row_ = rows_logged()[0]
        t('copy 1 down after a crash: copy 2 answers at once, and the answer names the model asked for',
          code == 200 and r['model'] == F and Fake.seen[0][0] == F2 and row_.get('copy') == F2 and not row_.get('held'), (code, r and r.get('model'), Fake.seen, row_.get('held')))
        with crashes._state() as st: st['down'] = {F: dict(crashed=time.time(), models=[F, F2], state='loading again')}
        got, t0 = {}, time.time()
        th = threading.Thread(target=lambda: got.update(zip(('code', 'r', 'h'), ask(tag='e')))); th.start()
        time.sleep(2)
        marks = llmshare.inflight()
        held_state = marks and marks[0].get('state')
        with crashes._state() as st: st['down'] = {}
        th.join(30)
        row_ = rows_logged()[0]
        t('both copies down: held until one is back, then answered', got.get('code') == 200 and held_state == 'waiting: the model crashed and is loading again'
          and 2 <= row_.get('held', 0) < 8, (got.get('code'), held_state, row_.get('held')))
        old_hold, crashes.HOLD = crashes.HOLD, 2
        with crashes._state() as st: st['down'] = {F: dict(crashed=time.time(), models=[F, F2], state='loading again')}
        plan(); t0 = time.time()
        code, r, h = ask(tag='h')
        t('the hold runs out with nothing back: 503 with Retry-After, nothing forwarded', code == 503 and h.get('Retry-After') and 2 <= time.time() - t0 < 6 and not Fake.seen, (code, round(time.time() - t0, 1), Fake.seen))
        crashes.HOLD = old_hold
        with crashes._state() as st: st['down'] = {}
        with crashes._state() as st: st['recycle'] = dict(job=dict(copy=F, family=F, state='draining', since=time.time(), why='test'), last={}, fresh={}, retry_after=0)
        plan()
        code, r, h = ask(tag='f')
        t('copy 1 draining for a recycle: copy 2 answers', code == 200 and Fake.seen[0][0] == F2, Fake.seen)
        plan()
        code, r, h = ask(model=F, tag='f2')
        with crashes._state() as st: st['recycle'] = dict(job=dict(copy=F2, family=F, state='draining', since=time.time(), why='test'), last={}, fresh={}, retry_after=0)
        plan()
        code, r, h = ask(model=F2, tag='n')
        t('a request naming the copy that is draining goes to its sibling', code == 200 and Fake.seen[0][0] == F, Fake.seen)
        with crashes._state() as st: st['recycle'] = dict(job=None, last={}, fresh={}, retry_after=0)
        rows.pop(); plan()                                          # copy 2 not loaded
        code, r, h = ask(model=F2, tag='m')
        t('a request naming a copy that is not loaded goes to the family\'s loaded copy', code == 200 and Fake.seen[0][0] == F and r['model'] == F2, (code, Fake.seen, r and r.get('model')))
        with crashes._state() as st: st['down'] = {F: dict(crashed=time.time(), models=[F2], state='left down', why='test', since=time.time())}
        plan()
        code, r, h = ask(tag='l')
        t('a family left down but with a copy loaded: that copy answers', code == 200 and Fake.seen[0][0] == F, (code, Fake.seen))
        rows.clear(); plan()
        code, r, h = ask(tag='g')
        t('a model left down and nothing loaded: 503 at once, nothing forwarded (no stray just-in-time load)', code == 503 and h.get('Retry-After') == '600' and not Fake.seen, (code, Fake.seen))
        with crashes._state() as st: st['down'] = {}
        plan(); t0 = time.time()
        code, r, h = ask(tag='z')
        t('a family in the settings with nothing loaded and nothing on the way: 503 within a second, never the bare id',
          code == 503 and time.time() - t0 < 2 and not Fake.seen and 'no copy' in json.loads(urllib.request.urlopen(url + '/hub/health').read() and b'{}') if False else code == 503 and time.time() - t0 < 2 and not Fake.seen, (code, Fake.seen))
        plan()
        code, r, h = ask(model='other/unknown', tag='u')
        t('a model the settings do not know is still passed through', code == 200 and Fake.seen[0][0] == 'other/unknown', Fake.seen)
        rows[:] = [row(F), row(F2)]

        print('the door: the queue and programs that leave')
        plan('slow', 'slow'); Fake.slow = 3.0
        ths = [threading.Thread(target=ask, kwargs=dict(tag=f'slow{i}', kind='batch')) for i in range(2)]
        for x in ths: x.start()
        time.sleep(0.8)                                              # both slots busy
        threading.Thread(target=raw, args=(F, 'ghost'), kwargs=dict(close_after=0.5)).start()   # gives up while it waits
        time.sleep(1.0)
        batch = threading.Thread(target=lambda: ask(tag='batch', kind='batch')); batch.start()
        time.sleep(0.5)
        chat = threading.Thread(target=lambda: ask(tag='chat', kind='chat')); chat.start()
        for x in ths + [batch, chat]: x.join(30)
        tags = [s_[1] for s_ in Fake.seen]
        ghost_rows = [r_ for r_ in rows_logged(6) if r_.get('status') == 499]
        t('a program that hung up while waiting is dropped, not sent', 'ghost' not in tags and len(ghost_rows) == 1 and 'hung up' in ghost_rows[0].get('note', ''), (tags, [g.get('note') for g in ghost_rows]))
        t('the chat request got the next free slot before the batch request that came first', tags.index('chat') < tags.index('batch'), tags)
        plan('slow'); Fake.slow = 5.0
        s = raw(F, 'leaver', kind='test')
        time.sleep(1.2); s.close()                                   # gives up during the answer
        time.sleep(2.5)
        freed = llmshare.busy_slots()
        row_ = rows_logged()[0]
        t('a program that hangs up during a long answer: the door hangs up on LM Studio, the slot is free within seconds',
          not freed.get(llmshare._safe(F)) and row_.get('status') == 499 and 'during the answer' in (row_.get('note') or ''), (freed, row_.get('status'), row_.get('note')))
        time.sleep(2.5); Fake.slow = 3.0
        plan('slow', 'slow'); Fake.slow = 6.0
        ths = [threading.Thread(target=ask, kwargs=dict(tag=f'busy{i}', kind='batch')) for i in range(2)]
        for x in ths: x.start()
        time.sleep(0.8); t0 = time.time()
        code, r, h = ask(tag='budget', kind='batch', headers={'X-Timeout': '4'})
        took = time.time() - t0
        row_ = rows_logged()[0]
        for x in ths: x.join(30)
        try: kinds = [json.loads(l).get('kind') for l in open(llmshare.WAITS)]
        except OSError: kinds = []
        t('a program that says it waits 4 s gets 503 at 3.6 s with Retry-After, nothing forwarded, the row says it waited',
          code == 503 and h.get('Retry-After') and 3 <= took < 5 and 'budget' not in [s_[1] for s_ in Fake.seen] and row_.get('status') == 503 and 3 <= (row_.get('waited') or 0) < 5
          and 'slot timeout' in kinds, (code, round(took, 1), row_.get('waited'), kinds[-1:]))
        Fake.slow = 3.0

        print('the door: requests as programs send them')
        plan()
        s = socket.create_connection(('127.0.0.1', gateway.PORT))
        chunk = json.dumps(dict(model=F, messages=[], tag='chunked')).encode()
        s.sendall(b'POST /v1/chat/completions HTTP/1.0\r\nHost: localhost:%d\r\nX-Kind: test\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n' % gateway.PORT
                  + b'%x\r\n' % len(chunk[:10]) + chunk[:10] + b'\r\n' + b'%x\r\n' % len(chunk[10:]) + chunk[10:] + b'\r\n0\r\n\r\n')
        s.settimeout(30); data = b''
        while True:
            part = s.recv(65536)
            if not part: break
            data += part
        s.close()
        t('a chunked request body is read whole', data.startswith(b'HTTP/1.0 200') and b'OK chunked' in data and Fake.seen and Fake.seen[0][1] == 'chunked', data[:60])
        plan()
        code, r, h = ask(tag='slash', path='/v1/chat/completions/')
        row_ = rows_logged()[0]
        t('a route with a trailing slash still takes a turn and is logged', code == 200 and row_.get('endpoint') == 'completions' and row_.get('status') == 200, (code, row_.get('endpoint')))
        plan('sse-cut'); crashes.WAKE.clear(); crashes._seen_state['nudged'] = 0   # the nudge is throttled to one in 5 s
        s = raw(F, 'cut', kind='test', stream=True); s.settimeout(30); data = b''
        while True:
            part = s.recv(65536)
            if not part: break
            data += part
        s.close()
        row_ = rows_logged()[0]
        t('a stream that breaks off ends with an error event and [DONE]; the row says cut; the keeper looks',
          b'"content":"One"' in data and b'cut the answer short' in data and data.rstrip().endswith(b'data: [DONE]') and 'cut' in (row_.get('note') or '') and crashes.WAKE.is_set(),
          (data[-120:], row_.get('note')))
        plan()
        code, r, h = ask(tag='caps', path='/v1/CHAT/completions')
        t('a route in uppercase takes a turn too', code == 200 and rows_logged()[0].get('endpoint') == 'completions', code)
        req = urllib.request.Request(url + '/v1/chat/completions', data=b'{}', headers={'Content-Type': 'application/json', 'Origin': 'http://localhost:3000'})
        try: urllib.request.urlopen(req, timeout=5); code = 200
        except urllib.error.HTTPError as e: code = e.code
        t("a web page from another local port is refused", code == 403, code)
        req = urllib.request.Request(url + '/api/v1/models/Unload', data=json.dumps(dict(instance_id=F2, model='other/x')).encode(), headers={'Content-Type': 'application/json'})
        with llmshare.lease('someone', [F2], 'batch'):
            llmshare._write(os.path.join(llmshare.LEASES, 'someone-1-1.json'), dict(v=2, project='someone', pid=1, models=[F2], kind='batch', since=time.time(), beat=time.time()))
            try: urllib.request.urlopen(req, timeout=5); code = 200
            except urllib.error.HTTPError as e: code = e.code
        t('an unload sent through the door, in any case, naming the copy another run uses: refused (409)', code == 409, code)
        st_ = llmshare.waiting_here()
        t('nobody waits once the queue is empty', st_ == dict(people=0, others=0), st_)
    finally:
        door.shutdown(); up.shutdown()


if __name__ == '__main__':
    sys.exit(main())
