#!/usr/bin/env python3
"""The gateway's rules (localmodels/gateway.py), tested against a pretend LM Studio on a random port: hold-and-resend across a
crash-shaped failure (LM Studio's words, a dropped connection, or a copy gone from the listing), 503 with Retry-After when
the resend fails too, a genuine 400 passed through, routing around a copy that is down or draining, a named copy that is
not there, nothing loaded, a hold that runs out, a program that hangs up while it waits or during the answer, people
before batches in the queue, a chunked request body, a route with a trailing slash, a model left down, and every API
LM Studio serves (OpenAI chat, Anthropic messages, OpenAI responses, its own): how each stream ends, its token counts, its errors. Nothing real is
loaded or unloaded. `localmodels check` runs it; so can you: python3 tests/test_gateway.py
"""
import fcntl, select, http.server, io, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _sandbox  # noqa: E402  (first: the state folder, port, LM Studio and lms point into a temporary folder)
from _sandbox import until
from localmodels import admission, crashes, gateway, llmshare, recycle

F, F2 = 'test/family', 'test/family:2'
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))


SSE = {   # streams as LM Studio sends them: each ends with its own last event, none with [DONE] (only OpenAI's chat does)
    'anthropic': [('message_start', dict(type='message_start', message=dict(id='m', model=F, usage=dict(input_tokens=12, output_tokens=1)))),
                  ('content_block_delta', dict(type='content_block_delta', index=0, delta=dict(type='text_delta', text='OK'))),
                  ('message_delta', dict(type='message_delta', delta=dict(stop_reason='end_turn'), usage=dict(output_tokens=5))),
                  ('message_stop', dict(type='message_stop'))],
    'responses': [('response.created', dict(type='response.created', response=dict(id='r', model=F))),
                  ('response.output_text.delta', dict(type='response.output_text.delta', delta='OK')),
                  ('response.completed', dict(type='response.completed', response=dict(id='r', model=F, usage=dict(input_tokens=7, output_tokens=4))))],
    'native-sse': [('chat.start', dict(type='chat.start', model_instance_id=F)),
                   ('message.delta', dict(type='message.delta', content='OK')),
                   ('chat.end', dict(type='chat.end', result=dict(model_instance_id=F, stats=dict(input_tokens=9, total_output_tokens=6))))],
}
SSE['anthropic-cut'] = SSE['anthropic'][:2]
SSE['anthropic-error'] = SSE['anthropic'][:2] + [('error', dict(type='error', error=dict(type='api_error', message='The model has crashed')))]
OOM = 'RuntimeError: [METAL] Command buffer execution failed: Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)'


class Fake(http.server.BaseHTTPRequestHandler):
    """A pretend LM Studio: answers by a plan ('ok', 'crash', 'bad', 'odd', 'slow', 'reset'), and remembers who asked, in order."""
    plan, seen, slow, on_odd, on_unload, hang = [], [], 3.0, None, None, 8.0
    lock = threading.Lock()
    gates = {}

    def log_message(self, *a): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.lower().endswith('/unload') and Fake.on_unload: Fake.on_unload(body)   # the instance leaves the listing
        with Fake.lock:
            what = Fake.plan.pop(0) if Fake.plan else 'ok'
            Fake.seen.append((body.get('model'), body.get('tag'), what))
        if what == 'reset':                                   # the server drops the connection before any answer
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.close_connection = True; return
        if what == 'slow': time.sleep(Fake.slow)
        if what == 'gated': Fake.gates[body['tag']].wait(30)
        if what in ('hang', 'sse-hang'):                     # a stalled engine: nothing comes, then the door cuts the connection
            if what == 'sse-hang':
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"One"}}]}\n\n'); self.wfile.flush()
            end = time.time() + Fake.hang
            while time.time() < end:
                time.sleep(0.1)
                try:
                    if self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b'': break   # the door hung up
                except BlockingIOError: pass
                except OSError: break
            self.close_connection = True; return
        if what == 'sse-cut':                                 # a stream that breaks off after one chunk, no [DONE]
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"One"}}]}\n\n'); self.wfile.flush()
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.close_connection = True; return
        if what in SSE:                                       # a whole stream in one of LM Studio's other formats
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            try:
                for ev, data in SSE[what]: self.wfile.write((f'event: {ev}\n' if ev else '').encode() + b'data: ' + json.dumps(data).encode() + b'\n\n')
                if what.endswith('-cut'): self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.close_connection = True; return
        if what == 'native':                                  # LM Studio's own REST API: the counts are in stats
            out, code = json.dumps(dict(model_instance_id=body.get('model'), output=[dict(type='message', content='OK')], stats=dict(input_tokens=9, total_output_tokens=6))).encode(), 200
        elif what == 'crash':
            out, code = json.dumps(dict(error='Error: The model has crashed without additional information. (Exit code: null)')).encode(), 400
        elif what == 'channel':                               # the generation thread died; the instance is still listed
            out, code = json.dumps(dict(error='Error: Channel Error')).encode(), 400
        elif what in ('oom', 'oom500'):                       # the engine out of memory, as a 400 or a 500
            out, code = json.dumps(dict(error=OOM)).encode(), 400 if what == 'oom' else 500
        elif what == 'bad':
            out, code = json.dumps(dict(error=dict(message="Invalid 'messages': must be an array", type='invalid_request_error'))).encode(), 400
        elif what == 'odd':
            if Fake.on_odd: Fake.on_odd()                     # the copy dies as it answers, with words the door has not seen
            out, code = json.dumps(dict(error='something new')).encode(), 400
        elif what == 'replica-text':
            out, code = json.dumps(dict(model=F2, choices=[dict(message=dict(content=F2, tool_calls=[dict(function=dict(arguments=F2))]))])).encode(), 200
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
    t('no hub path points into the live state folder (tests/_sandbox.py)', not _sandbox.stray(), _sandbox.stray())
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


@patch.object(gateway.whois, 'peer_pid', lambda *a: 0)  # requests in this suite supply their own attribution
def run(box):
    for name in ('SLOTS', 'LEASES', 'INFLIGHT'): setattr(llmshare, name, os.path.join(box, name.lower()))
    from localmodels import names as _names, peers as _peers           # never this Mac's live settings, names or peers
    llmshare.MODELS, _names.FILE = os.path.join(box, 'models.json'), os.path.join(box, 'names.json')   # (the repo's defaults apply)
    _peers.STATE, _peers.TOKEN_FILE = os.path.join(box, 'link.json'), os.path.join(box, 'peer-token')
    llmshare.REQUESTS, llmshare.WAITS = os.path.join(box, 'requests.jsonl'), os.path.join(box, 'waits.jsonl')
    crashes.STATE, crashes.RECORD, crashes.LOGS = os.path.join(box, 'keeper.json'), os.path.join(box, 'crashes.jsonl'), os.path.join(box, 'logs')
    crashes._seen_state.update(at=0.0, st={})
    row = lambda i: dict(id=i, key=F, state='IDLE', size='1 GB', bytes=2**30, context=4096, parallel=1)
    rows = [row(F), row(F2)]
    llmshare.loaded, llmshare.parallel_of, llmshare.read_ok = (lambda fresh=False, force=False: list(rows)), (lambda m: 1), (lambda: True)
    llmshare.api_models = lambda timeout=3: {r['id']: 4096 for r in rows}
    llmshare._ps['good_at'] = time.time()                     # this gateway has had a model listing (the stub above is it)
    llmshare.settings = lambda: dict(families={F: dict(key=F, copies=2, context=4096, parallel=1, recycle=False)}, batch_at_once=2, headroom_gb=1)
    up = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake); threading.Thread(target=up.serve_forever, daemon=True).start()
    door = gateway.Server(('127.0.0.1', 0), gateway.Door); threading.Thread(target=door.serve_forever, daemon=True).start()
    gateway.UP, gateway.PORT = f'http://127.0.0.1:{up.server_address[1]}', door.server_address[1]
    url = f'http://localhost:{gateway.PORT}'

    def ask(model=F, tag=None, kind='test', timeout=60, path='/v1/chat/completions', headers=None, pad=0, max_tokens=None, messages=None):
        body = dict(model=model, messages=messages if messages is not None else [{'role': 'user', 'content': 'x' * pad}] if pad else [], tag=tag,
                    **({'max_tokens': max_tokens} if max_tokens else {}))
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Project': 'local-models', 'X-Kind': kind, **(headers or {})})
        try: r = urllib.request.urlopen(req, timeout=timeout); return r.status, json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e: return e.code, None, dict(e.headers)

    def rows_logged(n=1):
        time.sleep(0.4)
        lines = open(llmshare.REQUESTS).read().strip().split('\n')
        return [json.loads(l) for l in lines[-n:]]

    def plan(*what):
        with Fake.lock: Fake.plan[:] = list(what); Fake.seen.clear()

    def raw(model, tag, kind='batch', close_after=None, stream=False, path='/v1/chat/completions'):
        """A request sent by hand over a socket; close_after: hang up that many seconds later (a program giving up)."""
        s = socket.create_connection(('127.0.0.1', gateway.PORT))
        body = json.dumps(dict(model=model, messages=[], tag=tag, stream=stream)).encode()
        s.sendall(b'POST %s HTTP/1.0\r\nHost: localhost:%d\r\nX-Kind: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % (path.encode(), gateway.PORT, kind.encode(), len(body)) + body)
        if close_after is not None: time.sleep(close_after); s.close(); return None
        return s

    try:
        print("the door: a crash-shaped failure is held and resent")
        plan('crash', 'ok')
        code, r, h = ask(tag='a')
        row_ = rows_logged()[0]
        t('LM Studio says the model crashed: the request is sent again and answered', code == 200 and r and r['choices'][0]['message']['content'] == 'OK a'
          and [s[2] for s in Fake.seen] == ['crash', 'ok'], (code, Fake.seen))
        t('the log says it took two tries, in classified words', row_.get('tries') == 2 and row_.get('note') == 'LM Studio returned HTTP 400: model crashed', {k: row_.get(k) for k in ('tries', 'note', 'status')})
        plan('crash', 'crash')
        code, r, h = ask(tag='b')
        t('failing twice: 503 with Retry-After, never LM Studio\'s 400', code == 503 and h.get('Retry-After') == str(gateway.RETRY_AFTER), (code, h.get('Retry-After')))
        plan('channel', 'ok')
        code, r, h = ask(tag='e')
        row_ = rows_logged()[0]
        t('an engine failure (400 "Channel Error") while the instance is still listed is resent, never passed through',
          code == 200 and r and r['choices'][0]['message']['content'] == 'OK e' and [s[2] for s in Fake.seen] == ['channel', 'ok']
          and row_.get('tries') == 2 and row_.get('note') == 'LM Studio returned HTTP 400: engine failure (Channel Error)',
          (code, Fake.seen, {k: row_.get(k) for k in ('tries', 'note', 'status')}))
        for what, n in (('oom', 400), ('oom500', 500)):
            plan(what, 'ok')
            code, r, h = ask(tag='o' + what)
            row_ = rows_logged()[0]
            t(f'Metal out of memory ({n} "[METAL] Command buffer execution failed: Insufficient Memory (...OutOfMemory)") with the instance still listed: resent, never passed through',
              code == 200 and r and r['choices'][0]['message']['content'] == 'OK o' + what and [s[2] for s in Fake.seen] == [what, 'ok']
              and row_.get('tries') == 2 and row_.get('note') == f'LM Studio returned HTTP {n}: engine failure (Insufficient Memory)',
              (code, Fake.seen, {k: row_.get(k) for k in ('tries', 'note', 'status')}))
        t('the gateway and the crash classifier read one list of engine-failure words', not hasattr(gateway, 'ENGINE') and crashes.ENGINE.search(OOM)
          and crashes.engine_failure('Error: Channel Error') == 'Channel Error', crashes.engine_failure(OOM))
        plan('bad')
        code, r, h = ask(tag='c')
        t('a genuine 400 passes through once', code == 400 and len(Fake.seen) == 1, (code, Fake.seen))
        plan('reset', 'ok')
        code, r, h = ask(tag='r')
        row_ = rows_logged()[0]
        t('LM Studio drops the connection before answering: sent again, answered, no server start', code == 200 and row_.get('tries') == 2 and 'closed the connection' in (row_.get('note') or ''), (code, row_.get('note')))
        plan('odd', 'ok')
        Fake.on_odd = lambda: rows.pop(0) if rows and rows[0]['id'] == F else None
        code, r, h = ask(tag='s')
        Fake.on_odd = None
        row_ = rows_logged()[0]
        t('a 400 with unknown words from a copy that is gone from the listing: treated as a crash, answered by the other copy',
          code == 200 and Fake.seen[-1][0] == F2 and 'no longer listed' in (row_.get('note') or ''), (code, Fake.seen, row_.get('note')))
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
        marks = until(lambda: [m for m in llmshare.inflight() if m.get('state') == 'held: reloading after a crash'], 15)   # held, then 2 s
        held_state = marks and marks[0].get('state')
        time.sleep(2)
        with crashes._state() as st: st['down'] = {}
        th.join(30)
        row_ = rows_logged()[0]
        t('both copies down: held until one is back, then answered', got.get('code') == 200 and held_state == 'held: reloading after a crash'
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
        llmshare._ps['good_at'] = 0.0; plan(); got = {}
        th = threading.Thread(target=lambda: got.update(r=ask(tag='w'))); th.start()
        held = until(lambda: [m for m in llmshare.inflight() if m.get('state') == 'held: waiting for the first model listing'], 10)
        rows.append(row(F)); llmshare._ps['good_at'] = time.time(); th.join(10)
        t('a gateway that has never had a model listing holds the request (up to 30 s) instead of answering 503, then answers',
          held and got.get('r') and got['r'][0] == 200, (held and held[0].get('state'), got.get('r') and got['r'][0]))
        rows.clear(); plan()
        code, r, h = ask(model='other/unknown', tag='u')
        t('a model the settings do not know is still passed through', code == 200 and Fake.seen[0][0] == 'other/unknown', Fake.seen)
        rows[:] = [row(F), row(F2)]

        with llmshare.slot(F, wait=2):
            plan('replica-text')
            code, result, _ = ask()
            t('only the response model is renamed, including when the answer is exactly the replica id',
              code == 200 and result['model'] == F and result['choices'][0]['message'] == dict(content=F2, tool_calls=[dict(function=dict(arguments=F2))]))
            for route, event, meta in (('/v1/chat/completions', 'message_stop', dict(model=F2)),
                                       ('/v1/messages', 'message_start', dict(message=dict(model=F2))),
                                       ('/v1/responses', 'response.completed', dict(response=dict(model=F2))),
                                       ('/api/v1/chat', 'chat.end', dict(result=dict(model_instance_id=F2)))):
                payload = dict(meta, type=event, delta=dict(text=F2, arguments=F2))
                SSE['replica-sse'] = [(event, payload), ('message_stop', dict(type='message_stop'))]
                plan('replica-sse')
                with raw(F, 'replica', stream=True, path=route) as s, s.makefile('rb') as stream:
                    events = [json.loads(line[6:]) for line in stream if line.startswith(b'data: {')]
                key = next(iter(meta))
                wanted = F if key == 'model' else {next(iter(meta[key])): F}
                t(f'{route}: the stream names the requested model and keeps the replica id in generated fields',
                  events[0][key] == wanted and events[0]['delta'] == payload['delta'])

        print('the door: the queue and programs that leave')
        plan('gated', 'gated')
        Fake.gates = {f'slow{i}': threading.Event() for i in range(2)}
        ths = [threading.Thread(target=ask, kwargs=dict(tag=f'slow{i}', kind='batch')) for i in range(2)]
        for x in ths: x.start()
        assert until(lambda: len(Fake.seen) == 2, 10)
        threading.Thread(target=raw, args=(F, 'ghost'), kwargs=dict(close_after=0.5)).start()   # gives up while it waits
        t('a disconnected waiter leaves admission while all slots are still occupied',
          until(lambda: any(r.get('status') == 499 for r in llmshare.requests_log(1)), 10) and all(x.is_alive() for x in ths))
        from localmodels import batches
        pinned = batches.pin(_peers.fingerprint(rows[0]))
        path = batches.ROUTES['/v1/chat/completions']
        lost = []
        waiter = threading.Thread(target=lambda: lost.append(ask(tag='lost-replica', path=path, headers={'X-Batch-Pin': pinned})))
        waiter.start()
        assert until(lambda: llmshare.waiting_here()['others'] == 1, 10)
        for r in rows: r['bytes'] += 1
        waiter.join(5)
        for r in rows: r['bytes'] -= 1
        t('a queued pinned request reports the loss of its eligible replicas without waiting for a slot timeout',
          lost and lost[0][0] == 503 and lost[0][2].get('Retry-After') and not any(s[1] == 'lost-replica' for s in Fake.seen))
        batch = threading.Thread(target=lambda: ask(tag='batch', kind='ask', path=path,
                                                   headers={'X-Batch-Pin': pinned})); batch.start()
        assert until(lambda: llmshare.waiting_here()['others'] == 1, 10)
        chat = threading.Thread(target=lambda: ask(tag='chat', kind='chat')); chat.start()
        assert until(lambda: llmshare.waiting_here()['people'] == 1, 10)
        Fake.gates['slow0'].set()
        assert until(lambda: any(s[1] == 'chat' for s in Fake.seen), 10)
        Fake.gates['slow1'].set()
        for x in ths + [batch, chat]: x.join(30)
        tags = [s_[1] for s_ in Fake.seen]
        ghost_rows = [r_ for r_ in rows_logged(6) if r_.get('status') == 499]
        t('a program that hung up while waiting is dropped, not sent', 'ghost' not in tags and len(ghost_rows) == 1 and 'client closed' in ghost_rows[0].get('note', ''), (tags, [g.get('note') for g in ghost_rows]))
        t('the chat request got the next free slot before the earlier pinned batch, even with an interactive header', tags.index('chat') < tags.index('batch'), tags)
        plan('slow'); Fake.slow = 8.0; t_leave = time.time()
        s = raw(F, 'leaver', kind='test')
        time.sleep(1.2); s.close()                                   # gives up during the answer, 6.8 s before LM Studio would finish it

        def let_go():
            f_, r_ = llmshare.busy_slots(), rows_logged()[0]
            return (not f_.get(llmshare._safe(F)) and r_.get('status') == 499 and 'during the response' in (r_.get('note') or '')) and (f_, r_)
        got_ = until(let_go, 5)
        freed, row_ = got_ or (llmshare.busy_slots(), rows_logged()[0])
        t('a program that hangs up during a long answer: the door hangs up on LM Studio, the slot is free within seconds',
          bool(got_), (freed, row_.get('status'), row_.get('note')))
        time.sleep(max(0, t_leave + Fake.slow + 0.5 - time.time())); Fake.slow = 3.0
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
        with patch.object(gateway, 'BODY_LIMIT', 16):
            plan()
            t('the fixed-length body limit is applied before forwarding', ask(tag='limit')[0] == 413 and not Fake.seen)
            handler = object.__new__(gateway.Door)
            for data, status in ((b'11\r\n', 413), (b'5\r\nabc', 400), (b'Z\r\n', 400)):
                handler.rfile = io.BytesIO(data)
                try: handler._chunks(); actual = 200
                except gateway.InputError as e: actual = e.status
                t('the chunked reader rejects oversize and incomplete input without forwarding a prefix', actual == status)
        with socket.socket() as conn:
            handler = object.__new__(gateway.Door)
            handler.connection = conn; conn.settimeout(2)
            with patch.object(http.server.BaseHTTPRequestHandler, 'parse_request', lambda h: h.connection.gettimeout()):
                t('headers retain the idle deadline; generation has no upload deadline', handler.parse_request() == 2 and conn.gettimeout() is None)
            handler.headers = http.client.HTTPMessage()
            handler.headers['Content-Length'] = '1'
            with patch.object(handler, 'rfile', create=True) as reader:
                reader.read.side_effect = socket.timeout()
                try: handler._body(); status = 200
                except gateway.InputError as e: status = e.status
                t('a stalled upload receives a timeout and releases its deadline', status == 408 and conn.gettimeout() is None)
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
          b'"content":"One"' in data and b'stream truncated' in data and data.rstrip().endswith(b'data: [DONE]') and 'truncated' in (row_.get('note') or '') and crashes.WAKE.is_set(),
          (data[-120:], row_.get('note')))
        plan()
        code, r, h = ask(tag='caps', path='/v1/CHAT/completions')
        t('a route in uppercase takes a turn too', code == 200 and rows_logged()[0].get('endpoint') == 'completions', code)
        req = urllib.request.Request(url + '/v1/chat/completions', data=b'{}', headers={'Content-Type': 'application/json', 'Origin': 'http://localhost:3000'})
        try: urllib.request.urlopen(req, timeout=5); code = 200
        except urllib.error.HTTPError as e: code = e.code
        t("a web page from another local port is refused", code == 403, code)

        def get(path, site=None, mode=None, dest=None):
            h = {k: v for k, v in (('Sec-Fetch-Site', site), ('Sec-Fetch-Mode', mode), ('Sec-Fetch-Dest', dest)) if v}
            try: return urllib.request.urlopen(urllib.request.Request(url + path, headers=h), timeout=10).status
            except urllib.error.HTTPError as e: return e.code
        codes = [get(p, 'cross-site', 'no-cors', d) for p, d in (('/hub/errors?minutes=x', 'image'), ('/v1/models', 'script'), ('/favicon.png', 'image'))]
        t("a browser's cross-site GET, which carries no Origin (an <img>, a <script>), is refused", codes == [403, 403, 403], codes)
        t('so is a page on another local port (same-site), and the dashboard framed by another site',
          (get('/hub/health', 'same-site', 'no-cors', 'script'), get('/', 'cross-site', 'navigate', 'iframe')) == (403, 403))
        t('a link from another site opens the dashboard; a navigation from another site to any other route is refused',
          (get('/', 'cross-site', 'navigate', 'document'), get('/hub/errors?minutes=x', 'cross-site', 'navigate', 'document')) == (200, 403))
        codes = (get('/hub/health', 'same-origin', 'cors', 'empty'), get('/', 'none', 'navigate', 'document'), get('/hub/health'))
        t("the dashboard's own requests (same-origin), an address typed in (none) and a program (no such header) are served", codes == (200, 200, 200), codes)
        from localmodels import web
        real_run = web.subprocess.run
        try:
            seen = []
            for out, code in (('5\n', 0), ('', 1), ('x\n', 0)):    # Purple; no key (Multicolor: blue); unreadable
                web.subprocess.run = lambda *a, **k: web.subprocess.CompletedProcess(a, code, out, '')
                seen.append(web.accent())
        finally: web.subprocess.run = real_run
        t("/hub/accent: the Mac's accent as System Settings stores it; no key or an unreadable one is Multicolor's blue",
          seen == [dict(name='Purple', color='#953d96'), dict(name='Multicolor', color='#007aff'), dict(name='Multicolor', color='#007aff')]
          and get('/hub/accent') == 200, seen)
        from localmodels import errors
        codes = [get(p) for p in ('/hub/errors?minutes=x', '/hub/history?minutes=x', '/hub/recent?n=x')]
        t('a window or a count that is not a number falls back to the default: answered, and nothing recorded as an error',
          codes == [200, 200, 200] and not [r for r in errors.recent(5) if r['where'].startswith('GET /hub/')], (codes, errors.recent(5)[:1]))
        held = lambda: (len(os.listdir(llmshare.INFLIGHT)) if os.path.isdir(llmshare.INFLIGHT) else 0, sum(e['n'] for e in gateway._live.values()))
        plan(); time.sleep(0.3)
        code, r, h = ask(tag='inf', max_tokens=1e999)
        t('a request whose max_tokens is not finite (1e999) is answered, and leaves no marker or lease count behind', code == 200 and until(lambda: held() == (0, 0), 5), (code, held()))
        real_est, admission.estimate = admission.estimate, lambda *a, **k: 1 / 0
        try: code, r, h = ask(tag='broken')
        finally: admission.estimate = real_est
        time.sleep(0.3)
        t('a request that fails before it is sent (its estimate raises) answers 500 and leaves no marker or lease count', code == 500 and held() == (0, 0), (code, held()))
        req = urllib.request.Request(url + '/api/v1/models/Unload', data=json.dumps(dict(instance_id=F2, model='other/x')).encode(), headers={'Content-Type': 'application/json'})
        ps_ok, llmshare._ps['ok'] = llmshare._ps['ok'], True
        plan()
        with llmshare.lease('someone', [F2], 'batch'):
            llmshare._write(os.path.join(llmshare.LEASES, 'someone-1-1.json'), dict(v=2, project='someone', pid=1, models=[F2], kind='batch', since=time.time(), beat=time.time()))
            try: urllib.request.urlopen(req, timeout=5); code = 200
            except urllib.error.HTTPError as e: code, reason = e.code, json.load(e)['error']
        t('an unload naming a leased copy is refused for that project before reaching LM Studio', code == 409 and 'someone' in reason and not Fake.seen)
        for f in os.listdir(llmshare.LEASES): os.remove(os.path.join(llmshare.LEASES, f))   # no run uses the models now (this suite's own leases too)
        plan(); saved_rows = list(rows); rows[:] = [row(F), row(F2)]
        with crashes._state() as st: st.setdefault('seen', {})[F2] = dict(loaded=True, since=time.time() - 60, at=time.time())
        during = {}
        def unload_while_admitting(body):
            during['gateway'] = ask(tag='during-swap')[0]
            # A separate llmshare client tries a local slot after the blocker check but before the instance disappears.
            rc = subprocess.run([sys.executable, '-c', 'import sys; from localmodels import llmshare as L; sys.exit(L._take_slot(sys.argv[1]) is not None)', F2], timeout=5)
            during['direct'] = rc.returncode
            rows[:] = [r for r in rows if r['id'] != body.get('instance_id')]
        Fake.on_unload = unload_while_admitting
        req = urllib.request.Request(url + '/api/v1/models/unload', data=json.dumps(dict(instance_id=F2)).encode(), headers={'Content-Type': 'application/json'})
        with llmshare.slot(F2, wait=2):
            try: urllib.request.urlopen(req, timeout=5); reason = ''
            except urllib.error.HTTPError as e: reason = json.load(e)['error']
        t('a slot acquired first blocks the HTTP swap, even without a lease', 'slot' in reason and not Fake.seen)
        try: code = urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e: code = e.code
        finally: llmshare._ps['ok'] = ps_ok
        for _ in range(20):                                  # the door marks it after it has answered the program
            st = crashes._read(fresh=True)
            if not (st.get('seen') or {}).get(F2, {}).get('loaded'): break
            time.sleep(0.1)
        t('an unload sent through the door: the instance it took is marked unloaded on purpose (LM Studio logs no unload line for it), never read as a crash',
          code == 200 and not (st.get('seen') or {}).get(F2, {}).get('loaded') and F2 not in (st.get('missing') or {}), (code, (st.get('seen') or {}).get(F2)))
        t('a swap reserved first excludes both HTTP and cross-process slot admission through the unload',
          during == dict(gateway=503, direct=0) and 'during-swap' not in [s[1] for s in Fake.seen], during)
        Fake.on_unload = None; rows[:] = saved_rows

        # Run the adoption between forwarding the request and returning its answer.
        old, new = os.getppid(), os.getpid()
        with patch.dict(os.environ, LOCALMODELS_SUPERVISOR=str(old)), patch.object(door, 'link', True), \
             patch.object(door, 'hosts', {f'localhost:{gateway.PORT}'}, create=True), patch.object(_peers, 'token_ok', return_value=True):
            path = llmshare.hub_lease_path('adopted', new, 'front')
            wait = gateway.Upstream.wait
            def adopt(up):
                gateway.rekey(old, new)
                during['lease'] = gateway._live[path]['n']
                return wait(up)
            with patch.object(gateway.Upstream, 'wait', adopt):
                plan()
                code, _, _ = ask(tag='adoption', headers={'X-Project': 'adopted', 'X-Hub-From': 'front'})
            t('a peer request spanning adoption completes and releases its new holder’s count',
              code == 200 and during.get('lease') == 1 and until(lambda: gateway._live[path]['n'] == 0, 2))
            # end_lease writes the final heartbeat under this lock just after decrementing the count.
            with gateway._lock, patch.object(llmshare, 'STALE', 0): llmshare.leases()
            t('the adopted lease ages out after the last request', not os.path.exists(path))

        plan()
        code, r, h = ask(tag='run', headers={'X-Project': 'run-header-check', 'X-Run': 'report', 'X-Run-Total': '420', 'X-Run-Done': '17', 'X-Step': 'extract 18 of 420'})
        row_ = until(lambda: next((r for r in llmshare.recent_requests(5, 1) if r.get('project') == 'run-header-check'), None), 10) or {}
        t('the run headers are logged with the request (name, total, done), never anything else about it',
          code == 200 and (row_.get('run'), row_.get('run_total'), row_.get('run_done'), row_.get('step')) == ('report', 420, 17, 'extract 18 of 420'),
          (code, {k: row_.get(k) for k in ('run', 'run_total', 'run_done')}))
        plan('slow'); Fake.slow = 2.0
        th = threading.Thread(target=ask, kwargs=dict(tag='runlive', headers={'X-Run': 'report', 'X-Run-Total': '420', 'X-Run-Done': '18'})); th.start()
        live = until(lambda: [f for f in llmshare.inflight() if f.get('run') == 'report'], 10)
        th.join(30); Fake.slow = 3.0
        t('a request in flight carries its run too, so the dash moves on at once', live and live[0].get('run_done') == 18, live)

        print('the door: a run in phases, its plan declared (POST /hub/runs) and each request counted in its phase')
        from localmodels import runs
        runs.PLANS = os.path.join(box, 'runs')
        def runs_post(body, headers=None):
            req = urllib.request.Request(url + '/hub/runs', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', **(headers or {})})
            try: r = urllib.request.urlopen(req, timeout=10); return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b'{}')
        code, body = runs_post({'run': 'survey', 'phases': [{'name': 'B', 'items': 3}, {'name': 'C', 'items': 2}]}, {'X-Project': 'local-models'})
        t('a plan declared: 200, and the plan as stored, its phases in order', code == 200 and [p['name'] for p in body['plan']['phases']] == ['B', 'C']
          and body['plan']['project'] == 'local-models', (code, body))
        code, body = runs_post({'run': 'survey', 'phases': [{'name': 'B', 'items': 'three'}]}, {'X-Project': 'local-models'})
        t('a plan the hub cannot take: 400 with the reason', code == 400 and 'whole number' in body.get('error', ''), (code, body))
        plan()
        code, r, h = ask(tag='phase', headers={'X-Run': 'survey', 'X-Phase': 'B', 'X-Phase-Total': '3', 'X-Phase-Done': '1'})
        row_ = rows_logged()[0]
        t('the phase headers are logged with the request', (row_.get('run'), row_.get('phase'), row_.get('phase_total'), row_.get('phase_done')) == ('survey', 'B', 3, 1), row_)
        got = json.loads(urllib.request.urlopen(url + '/hub/runs', timeout=10).read())
        mine = [g for g in got.get('runs', []) if g.get('name') == 'survey']
        t('GET /hub/runs: the run with its phases, B at work and C waiting', mine and [(p['name'], p['state']) for p in mine[0]['phases']] == [('B', 'running'), ('C', 'waiting')]
          and mine[0]['total'] == 5, mine)

        print('the door: maintenance, announced and on')
        from localmodels import maint
        maint.FILE = os.path.join(box, 'maintenance.json')
        maint.announce(start=time.time() + 600, minutes=30, why='updating LM Studio')
        plan()
        code, r, h = ask(tag='announced')
        t('announced: requests are answered, and every answer carries the notice', code == 200 and (h.get('X-Hub-Maintenance') or '').startswith('announced'), h.get('X-Hub-Maintenance'))
        plan('slow'); Fake.slow = 3.0
        got = {}
        th = threading.Thread(target=lambda: got.update(zip(('code', 'r', 'h'), ask(tag='inflight')))); th.start()
        time.sleep(1.0)
        maint.begin()
        th.join(30)
        t('a request being answered when maintenance starts finishes', got.get('code') == 200, got.get('code'))
        plan()
        code, r, h = ask(tag='during')
        ra = int(h.get('Retry-After') or 0)
        t('on: a new request gets 503 with Retry-After to the end and the notice, nothing forwarded',
          code == 503 and 1700 <= ra <= 1800 and h.get('X-Hub-Maintenance', '').startswith('on') and not Fake.seen, (code, ra, h.get('X-Hub-Maintenance'), Fake.seen))
        req = urllib.request.Request(url + '/v1/messages', data=json.dumps(dict(model=F, messages=[])).encode(), headers={'Content-Type': 'application/json', 'X-Kind': 'test'})
        try: urllib.request.urlopen(req, timeout=10); code, err = 200, {}
        except urllib.error.HTTPError as e: code, err = e.code, json.loads(e.read() or b'{}')
        t("on, Anthropic's API: overloaded_error in words that name the maintenance", code == 503 and err.get('error', {}).get('type') == 'overloaded_error' and 'maintenance' in err['error'].get('message', ''), err)
        health = json.loads(urllib.request.urlopen(url + '/hub/health', timeout=5).read())
        t('the health page says it too', (health.get('maintenance') or {}).get('phase') == 'on', health.get('maintenance'))
        was = gateway._start['at']
        t("on: the door never starts LM Studio's server (the person is working on it)", gateway.start_lmstudio('test') is False and gateway._start['at'] == was)
        maint.end()
        plan()
        code, r, h = ask(tag='after')
        t('off: answered again, no notice', code == 200 and not h.get('X-Hub-Maintenance'), (code, h.get('X-Hub-Maintenance')))
        plan('slow', 'slow'); Fake.slow = 4.0
        ths = [threading.Thread(target=ask, kwargs=dict(tag=f'fill{i}', kind='batch')) for i in range(2)]
        for x in ths: x.start()
        time.sleep(0.8)
        got = {}
        th = threading.Thread(target=lambda: got.update(zip(('code', 'r', 'h'), ask(tag='queued', kind='batch')))); th.start()
        time.sleep(0.8)
        maint.announce(start=time.time() - 1, minutes=10, why='test')
        for x in ths + [th]: x.join(30)
        maint.end(); Fake.slow = 3.0
        t('a request still waiting for its turn when maintenance starts is refused at its turn, never sent',
          got.get('code') == 503 and 'queued' not in [s_[1] for s_ in Fake.seen], (got.get('code'), Fake.seen))

        print('the door: switched off (POST /hub/off) and on again (POST /hub/on)')
        maint.OFF = os.path.join(box, 'off.json')
        def hub_post(route, body):
            try: r = urllib.request.urlopen(urllib.request.Request(url + route, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}), timeout=10); return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b'{}')
        code, body = hub_post('/hub/off', {})
        if code == 409: code, body = hub_post('/hub/off', {'force': True})        # the sandbox's lms cannot list: a blocker until forced
        t('switched off: 200, and the record says so', code == 200 and maint.switched_off(), (code, body))
        plan()
        code, r, h = ask(tag='switched-off')
        t('switched off: a request gets 503 with the header naming it, Retry-After 300 s, nothing forwarded',
          code == 503 and int(h.get('Retry-After') or 0) == maint.OFF_RETRY and 'switched-off' in (h.get('X-Hub-Maintenance') or '') and 'switched-off' not in [s_[1] for s_ in Fake.seen], (code, h.get('X-Hub-Maintenance'), h.get('Retry-After')))
        health = json.loads(urllib.request.urlopen(url + '/hub/health', timeout=5).read())
        t('the health page says it', 'switched off' in ((health.get('maintenance') or {}).get('words') or ''), health.get('maintenance'))
        t("switched off: the door never starts LM Studio's server", gateway.start_lmstudio('test') is False)
        code, body = hub_post('/hub/on', {})
        end = time.time() + 20
        while maint.switched_off() and time.time() < end: time.sleep(0.2)
        t('on: 202, and the record goes', code == 202 and maint.switched_off() is None, (code, body))
        snap = json.loads(urllib.request.urlopen(url + '/hub/snapshot', timeout=10).read())
        t("on: the dashboard's snapshot carries the switch-on (loading, then its outcome)", (snap.get('switching') or {}).get('phase') in ('loading', 'done'), snap.get('switching'))
        plan()
        code, r, h = ask(tag='switched-on')
        t('on: answered again', code == 200, (code, r))

        print('the door: switched off to peers only (POST /hub/off {"peers": true}), with no peer to take anything')
        code, body = hub_post('/hub/off', {'peers': True})
        t('peers only: 200 at once, no lease refuses it', code == 200 and maint.peers_only(), (code, body))
        plan()
        code, r, h = ask(tag='peers-only')
        err = (r or {}).get('error') if isinstance(r, dict) else None
        t('a request no peer can take: 503 with Retry-After 30 s, the header says peers-only, nothing sent to this Mac\'s LM Studio',
          code == 503 and h.get('Retry-After') == str(gateway.RETRY_AFTER) and (h.get('X-Hub-Maintenance') or '').startswith('peers-only') and not Fake.seen,
          (code, h.get('Retry-After'), h.get('X-Hub-Maintenance'), Fake.seen))
        row_ = rows_logged()[0]
        t('...and the row says why, in words', 'peers only' in (row_.get('note') or '') and 'no peer can take' in (row_.get('note') or ''), row_.get('note'))
        t("peers only: the door never starts LM Studio's server either", gateway.start_lmstudio('test') is False)
        code, body = hub_post('/hub/on', {})
        end = time.time() + 20
        while maint.switched_off() and time.time() < end: time.sleep(0.2)
        plan()
        code, r, h = ask(tag='peers-only-on')
        t('on: answered here again', code == 200 and Fake.seen and Fake.seen[0][1] == 'peers-only-on', (code, Fake.seen))

        print('the door: an instance that stalls (watchdog.py): its request cut and sent again, nothing new routed to it')
        plan('hang', 'ok'); Fake.hang = 10.0
        got = {}
        th = threading.Thread(target=lambda: got.update(zip(('code', 'r', 'h'), ask(tag='stuck')))); th.start()
        time.sleep(1.2)
        on = [m_.get('copy') for m_ in llmshare.inflight() if m_.get('state') == 'answering']
        crashes.stall(on[0] if on else F, 'stalled: test')
        th.join(30)
        row_ = rows_logged()[0]
        t('a request on an instance marked stalled is cut within a second or two and answered by its sibling',
          got.get('code') == 200 and [s_[0] for s_ in Fake.seen] == [F, F2] and (got.get('r') or {}).get('choices', [{}])[0].get('message', {}).get('content') == 'OK stuck'
          and row_.get('seconds', 99) < 6, (got.get('code'), Fake.seen, row_.get('seconds')))
        t('the row: answered by the sibling, two tries, resent because the instance stalled', row_.get('copy') == F2 and row_.get('tries') == 2
          and 'resent' in (row_.get('note') or '') and 'stalled' in row_['note'], {k: row_.get(k) for k in ('copy', 'tries', 'note')})
        plan()
        code, r, h = ask(tag='after-stall')
        t('while it is stalled, new requests go to its sibling', code == 200 and Fake.seen and Fake.seen[0][0] == F2, (code, Fake.seen))
        plan('sse-hang'); Fake.hang = 10.0
        crashes.loaded_now([F]); crashes.stall(F2, 'stalled: test'); time.sleep(1.1)
        got = {}
        def stream():
            s_ = raw(F, 'stuck-stream', kind='test', stream=True); s_.settimeout(30); data = b''
            while True:
                part = s_.recv(65536)
                if not part: break
                data += part
            s_.close(); got['data'] = data
        th = threading.Thread(target=stream); th.start()
        time.sleep(1.2)
        crashes.stall(F, 'stalled: test')
        th.join(30)
        row_ = rows_logged()[0]
        t('a stream under way on an instance that stalls: cut at once, ending with an error event, the row says stalled',
          b'"content":"One"' in got.get('data', b'') and b'stream truncated' in got.get('data', b'') and 'stalled: stream cut' in (row_.get('note') or ''), (got.get('data', b'')[-100:], row_.get('note')))
        old_hold, crashes.HOLD = crashes.HOLD, 2
        plan(); t0 = time.time()
        code, r, h = ask(tag='both-stalled')
        t('every instance stalled: held while they are reloaded, then 503 with Retry-After, nothing forwarded',
          code == 503 and h.get('Retry-After') and 2 <= time.time() - t0 < 6 and not Fake.seen, (code, round(time.time() - t0, 1), Fake.seen))
        crashes.HOLD = old_hold
        crashes.loaded_now([F, F2])
        plan()
        code, r, h = ask(tag='reloaded')
        t('reloaded: the stall is over, served again', code == 200 and not crashes.stalled_copies(), (code, crashes.stalled_copies()))
        old_wait, old_resend = gateway.UPSTREAM_WAIT, gateway.RESEND_WAIT
        gateway.UPSTREAM_WAIT, gateway.RESEND_WAIT = 1, 1
        plan('slow', 'ok'); Fake.slow = 2.5
        code, r, h = ask(tag='timeout')
        row_ = rows_logged()[0]
        gateway.UPSTREAM_WAIT, gateway.RESEND_WAIT = old_wait, old_resend; Fake.slow = 3.0
        t("no answer from LM Studio within the gateway's wait: said as that, never as a closed connection, and sent again",
          code == 200 and row_.get('tries') == 2 and 'no response from LM Studio within 1 s' in (row_.get('note') or ''), (code, row_.get('note')))
        time.sleep(2)

        print("the door: every API LM Studio serves (OpenAI chat, Anthropic messages, OpenAI responses, LM Studio's own)")
        def streamed(what, path):
            plan(what); crashes.WAKE.clear(); crashes._seen_state['nudged'] = 0
            s = raw(F, what, kind='test', stream=True, path=path); s.settimeout(30); data = b''
            while True:
                part = s.recv(65536)
                if not part: break
                data += part
            s.close()
            return data, rows_logged()[0]
        for what, path, want in (('anthropic', '/v1/messages', (12, 5)), ('responses', '/v1/responses', (7, 4)), ('native-sse', '/api/v1/chat', (9, 6))):
            data, row_ = streamed(what, path)
            t(f'{path} streamed: its own last event ends it whole, nothing added, nothing flagged',
              data.rstrip().endswith(b'}') and b'stream truncated' not in data and b'[DONE]' not in data and not row_.get('note') and not crashes.WAKE.is_set(),
              (data[-80:], row_.get('note')))
            t(f'{path} streamed: its tokens are counted (they feed the recycle band)', (row_.get('prompt_tokens'), row_.get('completion_tokens')) == want,
              (row_.get('prompt_tokens'), row_.get('completion_tokens')))
        plan('native')
        code, r, h = ask(tag='native', path='/api/v1/chat')
        row_ = rows_logged()[0]
        t("LM Studio's own API, not streamed: its tokens are counted from stats", code == 200 and (row_.get('prompt_tokens'), row_.get('completion_tokens')) == (9, 6),
          (code, row_.get('prompt_tokens'), row_.get('completion_tokens')))
        data, row_ = streamed('anthropic-cut', '/v1/messages')
        t('an Anthropic stream that breaks off ends with an Anthropic error event, no [DONE]; the row says cut; the keeper looks',
          b'event: error' in data and b'"type": "error"' in data and b'api_error' in data and b'[DONE]' not in data and 'truncated' in (row_.get('note') or '') and crashes.WAKE.is_set(),
          (data[-160:], row_.get('note')))
        data, row_ = streamed('anthropic-error', '/v1/messages')
        t("a stream LM Studio ends with its own error event: passed as it is, nothing added; the row says so; the keeper looks",
          data.rstrip().endswith(b'crashed"}}') and b'stream truncated' not in data and 'with an error' in (row_.get('note') or '') and crashes.WAKE.is_set(),
          (data[-100:], row_.get('note')))
        saved = list(rows); rows.clear(); plan()
        req = urllib.request.Request(url + '/v1/messages', data=json.dumps(dict(model=F, messages=[])).encode(), headers={'Content-Type': 'application/json', 'X-Kind': 'test'})
        try: urllib.request.urlopen(req, timeout=10); code, err = 200, {}
        except urllib.error.HTTPError as e: code, err = e.code, json.loads(e.read() or b'{}')
        t("the door's own refusal on /v1/messages is shaped as Anthropic's clients read it, with Retry-After",
          code == 503 and err.get('type') == 'error' and err.get('error', {}).get('type') == 'overloaded_error' and err['error'].get('message'), (code, err))
        req = urllib.request.Request(url + '/v1/chat/completions', data=json.dumps(dict(model=F, messages=[])).encode(), headers={'Content-Type': 'application/json', 'X-Kind': 'test'})
        try: urllib.request.urlopen(req, timeout=10); code, err = 200, {}
        except urllib.error.HTTPError as e: code, err = e.code, json.loads(e.read() or b'{}')
        t('the same refusal on the OpenAI path keeps the shape programs already read', code == 503 and isinstance(err.get('error'), str), (code, err))
        rows[:] = saved
        st_ = llmshare.waiting_here()
        t('nobody waits once the queue is empty', st_ == dict(people=0, others=0), st_)

        print('worker rules: the request-line timeout, the connection count, the queued marks')
        gateway.REQUEST_LINE_TIMEOUT = 1
        idle = socket.create_connection(('127.0.0.1', gateway.PORT)); idle.settimeout(5); t0 = time.time()
        try: closed = idle.recv(1) == b''
        except OSError: closed = True
        t('a connection that sends no request line is closed after the request-line timeout', closed and 0.8 < time.time() - t0 < 3, round(time.time() - t0, 2))
        idle.close()
        late = socket.create_connection(('127.0.0.1', gateway.PORT))
        body = json.dumps(dict(model=F, messages=[], tag='late')).encode()
        late.sendall(b'POST /v1/chat/completions HTTP/1.0\r\nHost: localhost:%d\r\nX-Kind: test\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % (gateway.PORT, len(body)))
        time.sleep(2.5); late.sendall(body); late.settimeout(20)
        got = b''
        while True:
            x = late.recv(65536)
            if not x: break
            got += x
        late.close()
        t('a request whose body comes 2.5 s after its headers is answered (only the request line is timed)', got.startswith(b'HTTP/1.0 200'), got[:40])
        plan('slow'); Fake.slow = 2.0
        code, r, h = ask(tag='slowly')
        t('an answer that takes longer than the timeout is never cut', code == 200, code)
        gateway.REQUEST_LINE_TIMEOUT = 30
        raw(F, 'gone', close_after=0.3); time.sleep(1.0)
        t('the connection count returns to 0 after answers, streams, refusals and a client that left', gateway.conns() == 0, gateway.conns())
        lis = gateway.Server(('127.0.0.1', 0), gateway.Door); lis.socket.setblocking(False)
        c = socket.create_connection(lis.server_address); select.select([lis.socket], [], [], 2)
        conn, _ = lis.get_request(); n1 = gateway.conns()
        t('get_request: a blocking socket from a non-blocking listener, counted', conn.getblocking() and conn.gettimeout() is None and n1 == 1, (conn.getblocking(), conn.gettimeout(), n1))
        lis.shutdown_request(conn); c.close()
        class Refusing(gateway.Server):
            def verify_request(self, request, client_address): return False
        rf = Refusing(('127.0.0.1', 0), gateway.Door); threading.Thread(target=rf.serve_forever, daemon=True).start()
        c = socket.create_connection(rf.server_address); c.settimeout(5)
        try: c.recv(1)
        except OSError: pass
        c.close(); time.sleep(0.3); rf.shutdown(); rf.server_close(); lis.server_close()
        t('the verify_request-false path is counted out too', gateway.conns() == 0, gateway.conns())
        held_slot = open(os.path.join(llmshare.SLOTS, llmshare._safe(F), 'slot-0.lock'), 'a+'); fcntl.flock(held_slot, fcntl.LOCK_EX)
        held2 = open(os.path.join(llmshare.SLOTS, llmshare._safe(F2), 'slot-0.lock'), 'a+'); fcntl.flock(held2, fcntl.LOCK_EX)
        plan(); got = {}
        th = threading.Thread(target=lambda: got.update(r=ask(tag='q', kind='batch'))); th.start(); time.sleep(1.0)
        q = [m for m in llmshare.inflight() if m.get('state') == 'queued']
        fcntl.flock(held_slot, fcntl.LOCK_UN); held_slot.close(); fcntl.flock(held2, fcntl.LOCK_UN); held2.close(); th.join(10)
        mk = q[0] if q else {}
        t('a waiting request is queued in its mark with its place in every gateway\'s queue: kind, since, candidates, deadline',
          mk.get('prio') == 1 and mk.get('t0') and sorted(mk.get('cands') or []) == sorted([F, F2]) and mk.get('until', 0) > time.time() and mk.get('n'), mk)
        t('...and then answered', got.get('r') and got['r'][0] == 200, got.get('r') and got['r'][0])

        print('admission by context size: an instance takes a request while its context in flight fits its budget')
        real_par, real_set = llmshare.parallel_of, llmshare.settings
        llmshare.parallel_of = lambda m: 2                         # two slots a copy, a budget of 10,000 tokens each
        llmshare.settings = lambda: dict(families={F: dict(key=F, copies=2, context=16384, parallel=2, recycle=False, admit=dict(tokens=10000))},
                                         batch_at_once=4, headroom_gb=1)
        admission._ratio.update(at=time.time() + 3600, v=3.0)      # 3 bytes a token, as before any request of this Mac is on record
        real_free, admission.free_gib = admission.free_gib, (lambda: 64.0)   # memory to spare: the budget alone decides here
        big, small = 21000, 600                                      # bytes of prompt: ~7,000 tokens and ~200, plus 100 of answer each
        plan('slow', 'slow', 'slow', 'slow'); Fake.slow = 3.0
        got, marks_seen = {}, {}
        def go(tag, pad, delay):
            time.sleep(delay); got[tag] = ask(tag=tag, kind='batch', pad=pad, max_tokens=100, headers={'X-Step': tag})
        ths = [threading.Thread(target=go, args=a) for a in (('A', big, 0), ('B', big, 0.4), ('C', small, 0.8), ('D', big, 1.2))]
        for x in ths: x.start()
        time.sleep(2.0)
        marks_seen = {m.get('step'): m for m in llmshare.inflight()}
        for x in ths: x.join(30)
        Fake.slow = 3.0
        where = {s_[1]: s_[0] for s_ in Fake.seen}
        logged = {r_.get('step'): r_ for r_ in rows_logged(4)}
        t('a long request goes to the copy with room, though the first copy has a slot free; a short one fits beside it',
          [s_[1] for s_ in Fake.seen][:3] == ['A', 'B', 'C'] and (where.get('A'), where.get('B'), where.get('C')) == (F, F2, F), (Fake.seen, got.get('A', [0])[0]))
        d = marks_seen.get('D') or {}
        t('one that fits no copy waits, and its in-flight marker says why: the context budget',
          d.get('state') == 'queued' and 'context budget' in (d.get('wait_for') or '') and '10,000' in d.get('wait_for', ''), d)
        t('...then runs as soon as a copy has room, and its row says what it waited for',
          got.get('D', [0])[0] == 200 and where.get('D') == F and 'context budget' in (logged.get('D') or {}).get('wait_for', '') and (logged.get('D') or {}).get('waited', 0) >= 1,
          (got.get('D', [0])[0], where.get('D'), {k: (logged.get('D') or {}).get(k) for k in ('waited', 'wait_for')}))
        a = logged.get('A') or {}
        t('each row keeps the request\'s size (body bytes, estimated context), never its content',
          a.get('bytes', 0) > big and 7000 <= a.get('ctx', 0) <= 7300 and 'x' * 50 not in json.dumps(a), {k: a.get(k) for k in ('bytes', 'ctx')})
        t('every room is given back when the requests end', llmshare.load_here() == {}, llmshare.load_here())

        print('admission: a long request is never starved by short ones (backfilling, then a reservation)')
        real_reserve, llmshare.RESERVE = llmshare.RESERVE, 1
        llmshare.parallel_of = lambda m: 3
        saved = list(rows); rows[:] = [row(F)]                      # one copy of 3 slots
        plan('slow', 'slow', 'slow', 'slow')
        s_pad, e_pad = 6000, 24000                                   # ~2,100 and ~8,100 tokens: a short one fits beside another, not beside the long one
        ths = [threading.Thread(target=go, args=a) for a in (('S1', s_pad, 0), ('E', e_pad, 0.3), ('S2', s_pad, 0.6), ('S3', s_pad, 2.0))]
        for x in ths: x.start()
        for x in ths: x.join(40)
        order = [s_[1] for s_ in Fake.seen]
        t('a short request behind the long one takes the room it leaves (backfill); after RESERVE the long one keeps its place, so the next short one waits behind it',
          order == ['S1', 'S2', 'E', 'S3'] and all(got.get(k, [0])[0] == 200 for k in ('S1', 'S2', 'E', 'S3')), (order, {k: got.get(k, [0])[0] for k in ('S1', 'S2', 'E', 'S3')}))
        llmshare.RESERVE, llmshare.parallel_of, llmshare.settings, admission.free_gib = real_reserve, real_par, real_set, real_free
        rows[:] = saved
        admission._ratio.update(at=0.0, v=admission.RATIO)

        print('which code the gateway runs: its digest in /hub/health, compared by every caller')
        from localmodels import dash
        health = json.load(urllib.request.urlopen(url + '/hub/health', timeout=5))
        t('/hub/health carries the digest of the code the gateway loaded (its release digest)', health.get('code') == dash.deploy_digest(gateway.HOME), health.get('code'))
        real_h, real_up = dash.health, llmshare.hub_up
        llmshare.hub_up = lambda fresh=False: True
        try:
            got = {}
            for c in ('0' * 16, '0' * 12):                     # a release's digest, and a gateway from before releases
                dash.health = lambda fresh=False: dict(health, code=c); dash._cache.pop('stale', None); got[len(c)] = dash.door()['stale']
            dash.health = lambda fresh=False: health; dash._cache.pop('stale', None); got['same'] = dash.door()['stale']
        finally: llmshare.hub_up, dash.health = real_up, real_h; dash._cache.pop('stale', None)
        t('a gateway that runs other code than the caller reads as stale, by its own digest (either form); the same code does not',
          got == {16: True, 12: True, 'same': False}, got)
        print('a second gateway on the same port: it leaves the running one alone')
        os.makedirs(llmshare.INFLIGHT, exist_ok=True)
        mine = os.path.join(llmshare.INFLIGHT, 'running-gateway-1.json'); llmshare._write(mine, dict(hub_pid=os.getpid(), since=time.time(), state='answering'))
        port_was = gateway.PORT
        try: code2 = gateway.main()
        finally: gateway.PORT = port_was
        t("a start that finds the port taken returns 1 and leaves the running gateway's in-flight markers", code2 == 1 and os.path.exists(mine), (code2, os.path.exists(mine)))
        os.remove(mine)
        print("prefix affinity (affinity.py): a conversation's follow-ups go back to the copy that served it, while it has a free slot")
        from localmodels import affinity
        affinity._seen.clear(); plan()
        conv = lambda claim, *more: [{'role': 'system', 'content': 'You check claims.'}, {'role': 'user', 'content': claim}] + list(more)
        turn = ({'role': 'assistant', 'content': 'reading'}, {'role': 'user', 'content': 'tool result'})
        on = lambda tag: next((s[0] for s in Fake.seen if s[1] == tag), None)

        def busy(tag, messages):                           # one slow request holds a copy's only slot
            th = threading.Thread(target=ask, kwargs=dict(tag=tag, messages=messages)); th.start()
            until(lambda: any(s[1] == tag for s in Fake.seen), 10)
            return th
        plan('slow')
        th = busy('hold-1', conv('another conversation'))
        code1, _, _ = ask(tag='a1', messages=conv('claim 1')); th.join()
        code2, _, _ = ask(tag='a2', messages=conv('claim 1', *turn))
        row2 = rows_logged()[0]
        code3, _, _ = ask(tag='b1', messages=conv('claim 2'))
        row3 = rows_logged()[0]
        t('copy 1 busy: the first turn goes to copy 2', code1 == 200 and on('hold-1') == F and on('a1') == F2, Fake.seen)
        t('its follow-up goes back to copy 2 with both copies free (without the rule, copy 1)', code2 == 200 and on('a2') == F2
          and row2.get('affinity') == 'same', (on('a2'), row2.get('affinity')))
        t('a new conversation with the same system prompt is not tied to it: copy 1, nothing preferred', code3 == 200 and on('b1') == F
          and 'affinity' not in row3, (on('b1'), row3.get('affinity')))
        plan('slow')
        th = busy('hold-2', conv('claim 1', *turn, *turn))
        t0_ = time.time(); code4, _, _ = ask(tag='a3', messages=conv('claim 1', *turn, *turn, *turn)); took = time.time() - t0_; th.join()
        row4 = rows_logged(2)
        row4 = next((r_ for r_ in row4 if r_.get('seconds', 9) < 2), row4[-1])
        t('its copy busy: the follow-up takes the other copy at once and never waits for it', code4 == 200 and on('hold-2') == F2 and on('a3') == F
          and took < 2 and row4.get('affinity') == 'moved', (on('hold-2'), on('a3'), round(took, 2), row4.get('affinity')))
        plan()
        n_ = len(affinity._seen)
        ask(tag='e1', path='/v1/embeddings', messages=conv('claim 3'))
        t('embeddings are never keyed', 'affinity' not in rows_logged()[0] and len(affinity._seen) == n_ == 3, (rows_logged()[0].get('affinity'), n_, len(affinity._seen)))
        t('only digests and copy names are kept: no prompt text in memory or in the row', all(len(k) == 24 and isinstance(v[0], str) for k, v in affinity._seen.items())
          and 'claim' not in json.dumps(rows_logged(6)) and 'claim' not in repr(affinity._seen), list(affinity._seen.items())[:1])
        affinity._seen.clear()

        print('the icon: a PNG at the standard paths, drawn from the theme tokens')
        import struct, zlib
        from localmodels import icon
        def fetch(path):
            r = urllib.request.urlopen(url + path, timeout=30); return r.status, r.headers.get('Content-Type'), r.read()
        def pixel(data, x, y):
            w = struct.unpack('>I', data[16:20])[0]; at, raw = 8, b''
            while at < len(data):
                n, kind = struct.unpack('>I', data[at:at + 4])[0], data[at + 4:at + 8]
                if kind == b'IDAT': raw += data[at + 8:at + 8 + n]
                at += 12 + n
            rows = zlib.decompress(raw); o = y * (1 + 4 * w) + 1 + 4 * x
            return tuple(rows[o:o + 4])
        die = icon.DIE
        got = {path: fetch(path) for path in ('/favicon.ico', '/favicon.png', '/apple-touch-icon.png')}
        t('/favicon.ico, /favicon.png and /apple-touch-icon.png answer PNG images, 64 and 180 pixels square',
          all(c == 200 and ct == 'image/png' and d[:8] == b'\x89PNG\r\n\x1a\n' for c, ct, d in got.values())
          and [struct.unpack('>II', got[p][2][16:24]) for p in ('/favicon.ico', '/apple-touch-icon.png')] == [(64, 64), (180, 180)], {p: v[:2] for p, v in got.items()})
        d = got['/favicon.png'][2]
        t('its die is sapphire, its corner is transparent', pixel(d, 32, 32) == die + (255,) and pixel(d, 0, 0)[3] == 0, (pixel(d, 32, 32), pixel(d, 0, 0)))
        fonts = {f: fetch(f) for f in ('/libre-franklin-400.woff2', '/libre-franklin-500.woff2')}
        t('the typeface of the page answers as WOFF2 from the gateway itself, both weights',
          all(c == 200 and ct == 'font/woff2' and d[:4] == b'wOF2' for c, ct, d in fonts.values()), {f: v[:2] for f, v in fonts.items()})
        board = fetch('/pcb.svg')
        t('so does the circuit board behind it, as SVG', board[0] == 200 and board[1] == 'image/svg+xml' and board[2].lstrip().startswith(b'<svg'), board[:2])
        page = urllib.request.urlopen(url + '/', timeout=10).read()
        t('the page links it (tab, bookmarks, favorites)', b'rel="icon" type="image/png" sizes="64x64" href="/favicon.png"' in page and b'rel="apple-touch-icon"' in page)
        t('the page asks for nothing from another site', not re.search(rb'(?:src|href)="https?://(?!localhost)|url\("?https?://', page.replace(b'https://haedolabs.com', b'')), re.findall(rb'https?://[^"\' )]+', page)[:5])

        print('the demo: generic batches through the gateway, made-up records, every request named')
        from localmodels import demo
        n0 = len(open(llmshare.REQUESTS).read().strip().split('\n')); plan()
        out = demo.run(url=url, minutes=0.03, workers=3, model=F, say=lambda *x: None, seed=7)
        time.sleep(0.5)
        new = [json.loads(l) for l in open(llmshare.REQUESTS).read().strip().split('\n')[n0:]]
        t('three generic projects, as batches with their progress and steps; nothing else named',
          new and {r['project'] for r in new} == {'research', 'evals', 'docs'} and all(r.get('kind') == 'batch' and r.get('status') == 200 for r in new)
          and {r.get('run') for r in new} == {'consistency check', 'answer grading', 'summaries'} and all(r.get('run_total') and ' of ' in (r.get('step') or '') for r in new),
          (len(new), sorted({r['project'] for r in new}), new[:1]))
        t('the demo says what it did: answers, no errors', all(v['done'] > 0 and v['errors'] == 0 for v in out.values()), {k: (v['done'], v['errors']) for k, v in out.items()})
        text = demo.ledger(__import__('random').Random(1)) + demo.grading(__import__('random').Random(1)) + demo.notes(__import__('random').Random(1))
        t('its records are made up: stock lines, sums and planning notes', 'balance' in text and ' = ' in text and 'week' in text)
    finally:
        door.shutdown(); up.shutdown()


if __name__ == '__main__':
    sys.exit(main())
