#!/usr/bin/env python3
"""Zero-downtime handoffs, tested end to end in a sandbox: a supervisor on a free port with releases in a temporary state
folder, a pretend LM Studio in this process, a pretend lms. While every deploy runs, threads open a new connection every
10 ms on 127.0.0.1 and [::1] and a long stream is in flight: not one may be refused, reset or cut. Then a release that
cannot start (it is marked bad, the serving worker untouched), the supervisor killed (the workers serve on, a new one
adopts them) and a stop (everything ends within 5 s). `localmodels check` runs it; so can you: python3 tests/test_handoff.py
"""
import http.server, json, os, shutil, signal, socket, stat, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from localmodels import dash, supervisor as SUP  # noqa: E402

F = 'qwen/qwen3.6-35b-a3b'
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''), flush=True)


class Fake(http.server.BaseHTTPRequestHandler):
    """A pretend LM Studio: the model listings, plain answers, and a long stream (16 events, 0.25 s apart)."""
    protocol_version = 'HTTP/1.0'

    def log_message(self, *a): pass

    def do_GET(self):
        if self.path.startswith('/api/v0/models'): body = dict(data=[dict(id=i, state='loaded', loaded_context_length=4096) for i in (F, F + ':2')])
        else: body = dict(data=[dict(id=i) for i in (F, F + ':2')])
        out = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
        if body.get('stream'):
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            for k in range(16):
                self.wfile.write(b'data: ' + json.dumps(dict(choices=[dict(delta=dict(content=f'{k} '))])).encode() + b'\n\n'); self.wfile.flush(); time.sleep(0.25)
            self.wfile.write(b'data: [DONE]\n\n'); return
        out = json.dumps(dict(model=body.get('model'), choices=[dict(message=dict(content='OK'))], usage=dict(prompt_tokens=3, completion_tokens=1))).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def build(state, name, change=None):
    """A release folder (cli.build_release, as install builds one): from this checkout, or from a copy change(folder) alters."""
    from localmodels import cli
    if not change: return cli.build_release(ROOT, state)
    src = os.path.join(os.path.dirname(state), f'src-{name}')
    for rel in dash.deploy_files(ROOT):
        os.makedirs(os.path.dirname(os.path.join(src, rel)), exist_ok=True); shutil.copy2(os.path.join(ROOT, rel), os.path.join(src, rel))
    change(src)
    return cli.build_release(src, state)


def call(state, op, timeout=180, **fields):
    """One operation on the supervisor's control socket. -> its final reply."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(timeout)
    s.connect(os.path.join(state, 'run', 'supervisor.sock'))
    s.sendall((json.dumps(dict(op=op, **fields)) + '\n').encode())
    buf, last = b'', {}
    while True:
        x = s.recv(65536)
        if not x: break
        buf += x
        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            m = json.loads(line)
            if m.get('done'): s.close(); return m
            last = m
    s.close(); return last


def health(port, host='127.0.0.1', timeout=3):
    url = f"http://{'[::1]' if host == '::1' else host}:{port}/hub/health"
    req = urllib.request.Request(url, headers={'Host': f'localhost:{port}'})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


class Hammer:
    """Threads that open a new connection every 10 ms, on each address, and keep every failure."""
    def __init__(self, port):
        self.port, self.stop, self.errors, self.n, self.pids, self.threads = port, threading.Event(), [], 0, set(), []

    def run(self, host):
        while not self.stop.is_set():
            try: self.pids.add(health(self.port, host, timeout=10).get('pid')); self.n += 1
            except Exception as e: self.errors.append(f'{host}: {type(e).__name__}: {e}')
            time.sleep(0.01)

    def __enter__(self):
        for host in ('127.0.0.1', '127.0.0.1', '::1', '::1'):
            th = threading.Thread(target=self.run, args=(host,), daemon=True); th.start(); self.threads.append(th)
        return self

    def __exit__(self, *a):
        self.stop.set()
        for th in self.threads: th.join(15)


def stream(port, out):
    req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions', data=json.dumps(dict(model=F, messages=[], stream=True)).encode(),
                                 headers={'Content-Type': 'application/json', 'Host': f'localhost:{port}', 'X-Project': 'test', 'X-Kind': 'test'})
    try:
        r = urllib.request.urlopen(req, timeout=60); data = r.read()
        out.update(status=r.status, done=data.rstrip().endswith(b'[DONE]'), events=data.count(b'data: '))
    except Exception as e: out.update(error=f'{type(e).__name__}: {e}')


def wait_for(fn, seconds, every=0.1):
    end = time.time() + seconds
    while time.time() < end:
        try:
            v = fn()
            if v: return v
        except Exception: pass                               # not recorded: a test waiting for something to come up
        time.sleep(every)
    return None


def main():
    box = tempfile.mkdtemp(prefix='lmh-')
    procs = []
    try: run(box, procs)
    finally:
        if os.environ.get('KEEP_LOG'): shutil.copy(os.path.join(box, 'gateway.log'), os.environ['KEEP_LOG'])
        for p in procs:
            if p.poll() is None: p.kill()
        for pid in [r['pid'] for r in SUP.registry()] if os.path.isdir(os.path.join(box, 's')) else []:
            try: os.kill(pid, signal.SIGKILL)
            except OSError: pass
        shutil.rmtree(box, ignore_errors=True)
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


class FakeConn:
    """A lifeline the test reads: what the supervisor sends a worker."""
    def __init__(self):
        self.sock, self.peer = socket.socketpair()
        self.peer.setblocking(False); self.kind = self.pid = None

    def got(self):
        try: return [json.loads(l)['op'] for l in self.peer.recv(65536).decode().split('\n') if l]
        except BlockingIOError: return []


def unit_restart(state, r1, r2, r3):
    """Review finding: a restarted supervisor forgot what its workers run, and the hub link decided what started next."""
    n = lambda d: os.path.basename(d)
    json.dump(dict(pid=1, current=n(r2), last_good=n(r1), bad=[n(r3)], next_gen=7, probation=dict(release=n(r2), until=time.time() + 60, pid=4242)),
              open(os.path.join(state, 'run', 'supervisor.json'), 'w')) if os.path.isdir(os.path.join(state, 'run')) else None
    s = SUP.Supervisor(); s.restore()
    t('restored from supervisor.json: current, last good, bad, the next gen and the probation under way',
      s.state['current'] == n(r2) and s.state['last_good'] == n(r1) and s.state['bad'] == [n(r3)] and s.state['next_gen'] == 7
      and (s.state.get('probation') or {}).get('release') == n(r2), s.state)
    hub = os.path.join(state, 'hub')
    if os.path.lexists(hub): os.remove(hub)
    os.symlink(os.path.relpath(r3, state), hub)            # the link points at a release that failed
    t('with no worker serving, a new one starts from the release that last served, not from the hub link',
      s.release_dir(None) == os.path.realpath(r2), s.release_dir(None))
    s.state['bad'].append(n(r2))
    t('...and from the last good one when that release is marked bad', s.release_dir(None) == os.path.realpath(r1), s.release_dir(None))
    s.state['bad'].remove(n(r2))
    os.remove(hub)
    a, b = FakeConn(), FakeConn()
    s.hello(a, dict(pid=91001, gen=4, role='serving', release=n(r2), state_protocol=0, accepting=True, keys=[]))
    s.hello(b, dict(pid=91002, gen=5, role='serving', release=n(r2), state_protocol=1, accepting=True, keys=[]))
    wa, wb = s.workers[91001], s.workers[91002]
    t('an adopted worker keeps the release and state protocol it said in its hello', wa.release == os.path.realpath(r2) and wa.protocol == 0 and wb.protocol == 1,
      (wa.release, wa.protocol))
    time.sleep(0.1)
    t('two serving workers adopted (a crash during the overlap): the newest gen serves, every older one drains',
      'drain' in a.got() and 'drain' not in b.got() and wa.role == 'draining' and wb.role == 'serving', (wa.role, wb.role))
    t("a deploy compares state protocols with the serving worker's own (not its release's file)",
      s.same_protocol(SUP.release_info(r1), wb) and not s.same_protocol(SUP.release_info(r1), wa), (SUP.release_info(r1) or {}).get('state_protocol'))
    for c in (a, b): c.sock.close(); c.peer.close()
    os.remove(os.path.join(state, 'run', 'supervisor.json'))


def run(box, procs):
    state, port = os.path.join(box, 's'), free_port()
    os.makedirs(os.path.join(state, 'releases')); os.makedirs(os.path.join(box, 'logs'))
    SUP.SHARED, SUP.RUN = state, os.path.join(state, 'run')                 # this process reads the same registry
    up = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake); threading.Thread(target=up.serve_forever, daemon=True).start()
    ps = os.path.join(box, 'ps.json')
    json.dump([dict(identifier=i, modelKey=F, status='idle', sizeBytes=2**30, contextLength=65536, parallel=4, type='llm') for i in (F, F + ':2')], open(ps, 'w'))
    lms = os.path.join(box, 'lms')
    open(lms, 'w').write(f'#!/bin/sh\ncase "$1" in ps) cat {ps};; *) echo ok;; esac\n'); os.chmod(lms, stat.S_IRWXU)
    r1 = build(state, 'r1')
    r2 = build(state, 'r2', lambda d: open(os.path.join(d, 'localmodels', 'marker.txt'), 'w').write('r2\n'))
    r3 = build(state, 'r3', lambda d: open(os.path.join(d, 'localmodels', 'web.py'), 'a').write('\nraise RuntimeError("this release cannot start")\n'))
    os.makedirs(os.path.join(state, 'run'), mode=0o700, exist_ok=True)
    print('what a supervisor knows after a restart (unit: no worker started)')
    unit_restart(state, r1, r2, r3)
    os.symlink(os.path.relpath(r1, state), os.path.join(state, 'hub'))
    env = dict(os.environ, LOCALMODELS_STATE=state, LOCALMODELS_PORT=str(port), LMSTUDIO_URL=f'http://127.0.0.1:{up.server_address[1]}', LMS=lms,
               LMSTUDIO_LOGS=os.path.join(box, 'logs'), LOCALMODELS_PY=sys.executable, LOCALMODELS_NO_KEEPER='1',
               LOCALMODELS_TIMINGS=json.dumps(dict(SOAK=0.5, PROBATION=5, FAILOPEN_AFTER=60)))
    logf = open(os.path.join(box, 'gateway.log'), 'w')
    start = lambda: subprocess.Popen([sys.executable, os.path.join(state, 'hub', 'bin', 'localmodels'), 'gateway'], env=env, stdout=logf, stderr=logf)

    def tail(n=30):
        logf.flush()
        return '\n'.join(open(os.path.join(box, 'gateway.log')).read().split('\n')[-n:])

    print('a supervisor, and its first worker')
    sup = start(); procs.append(sup)
    h = wait_for(lambda: health(port).get('role') == 'serving' and health(port), 60)
    t('the supervisor starts gen 1 from the release the hub points at; it serves on 127.0.0.1 and [::1]',
      h and h.get('code') == os.path.basename(r1) and h.get('gen') == '1' and wait_for(lambda: health(port, '::1'), 5), h and {k: h.get(k) for k in ('code', 'gen', 'role')} or tail())
    st = call(state, 'status')
    t('status: one serving worker, the listeners, the run', st.get('ok') and [w['role'] for w in st.get('workers', [])] == ['serving'] and sorted(st.get('listeners') or {}) == ['v4', 'v6'], st)
    t('the roles are taken by the serving worker', wait_for(lambda: set(health(port).get('roles') or {}) >= {'peer-poller', 'memory-sampler', 'link-listener'}, 10),
      health(port).get('roles'))

    print('a deploy under load: a new connection every 10 ms on each address, a long stream in flight')
    out = {}
    with Hammer(port) as hm:
        th = threading.Thread(target=stream, args=(port, out)); th.start(); time.sleep(0.5)
        r = call(state, 'deploy', release=os.path.basename(r2))
        th.join(30); time.sleep(0.5)
    h = health(port)
    t('the deploy: gen 2 serves release 2', r.get('ok') and h.get('code') == os.path.basename(r2) and h.get('gen') == '2', r)
    t('not one connection refused, reset or cut while it happened', not hm.errors and hm.n > 50, (hm.n, hm.errors[:3]))
    t('both workers answered during the overlap', len(hm.pids) >= 2, hm.pids)
    t('the stream that began on gen 1 ended whole, after gen 2 took over', out.get('status') == 200 and out.get('done') and out.get('events') == 17, out)
    gone = wait_for(lambda: [w for w in call(state, 'status').get('workers', []) if w['gen'] == 1] == [], 20)
    t('gen 1 drained and exited once its stream had finished', gone, call(state, 'status').get('workers'))
    t('the roles moved to gen 2', wait_for(lambda: set((health(port).get('roles') or {}).values()) == {h.get('pid')}, 10), health(port).get('roles'))
    t('the hub link points at release 2', os.path.realpath(os.path.join(state, 'hub')) == os.path.realpath(r2), os.readlink(os.path.join(state, 'hub')))
    rel = json.load(open(os.path.join(r2, 'RELEASE.json')))
    t('a release is named by its digest and says what it holds', rel.get('digest') == os.path.basename(r2) and rel.get('state_protocol') == 1
      and rel.get('supervisor') == SUP.supervisor_digest(r2) and dash.deploy_digest(r2) == os.path.basename(r2), rel)

    print('a release that cannot start')
    with Hammer(port) as hm:
        r = call(state, 'deploy', release=os.path.basename(r3))
    st = call(state, 'status')
    t('refused: its worker failed while warming, and the release is marked bad', not r.get('ok') and os.path.basename(r3) in (st.get('bad') or []), r)
    t('the serving worker is untouched, and nothing was refused meanwhile', health(port).get('gen') == '2' and not hm.errors, hm.errors[:3])
    r = call(state, 'deploy', release=os.path.basename(r3))
    t('a bad release is refused at once without --force', not r.get('ok') and 'marked bad' in str(r.get('why')), r)

    print('the supervisor killed, then started again')
    sup.send_signal(signal.SIGKILL); sup.wait(5)
    with Hammer(port) as hm: time.sleep(1.5)
    t('its workers serve on (nothing refused) with no supervisor', not hm.errors and hm.n > 20, (hm.n, hm.errors[:3]))
    sup = start(); procs.append(sup)
    st = wait_for(lambda: (lambda s: s if [w for w in s.get('workers', []) if w.get('adopted') and w['role'] == 'serving'] and sorted(s.get('listeners') or {}) == ['v4', 'v6'] else None)(call(state, 'status', timeout=5)), 30)
    t('a new supervisor adopts the serving worker and its listeners', st, st or tail())
    ad = [w for w in (st or {}).get('workers', []) if w.get('adopted')]
    t('...knowing the release it runs and the release that last served', ad and ad[0].get('release') == os.path.basename(r2)
      and (st or {}).get('current') == os.path.basename(r2) and os.path.basename(r3) in ((st or {}).get('bad') or []), st)
    with Hammer(port) as hm:
        r = call(state, 'deploy', release=os.path.basename(r1))
    t('...and hands off from it to release 1, nothing refused', r.get('ok') and health(port).get('code') == os.path.basename(r1) and not hm.errors, (r, hm.errors[:3]))

    print('stop')
    t0 = time.time(); sup.send_signal(signal.SIGTERM)
    try: sup.wait(10)
    except subprocess.TimeoutExpired: pass
    done = wait_for(lambda: not SUP.registry(), 10)
    t('SIGTERM with no detach marker: the supervisor and every worker exit within 5 s', sup.poll() is not None and done and time.time() - t0 < 5.5, (sup.poll(), SUP.registry(), round(time.time() - t0, 1)))
    try: health(port, timeout=2); refused = False
    except (urllib.error.URLError, ConnectionError, OSError): refused = True
    t('then the port is closed: clients go straight to LM Studio', refused)
    up.shutdown()


if __name__ == '__main__':
    sys.exit(main())
