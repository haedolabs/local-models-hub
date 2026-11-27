"""worker - one gateway process under the supervisor (supervisor.py), which holds the listening sockets and starts each
worker from an immutable release with LOCALMODELS_LISTEN naming the descriptors it passed down.

A worker's life:
  warming    adopt the listeners, sweep what dead gateways left, import every module, read the model listing, answer its
             own self-test on a private port; it accepts nothing yet. Then 'ready'.
  serving    after the supervisor's 'go': serve every listener, take the singleton roles (instance manager, peer poller,
             memory sampler, link listener, batch recovery) whenever nobody holds them, report progress every 5 s.
  draining   after 'drain' (or SIGTERM): stop accepting, give the roles up between two passes, finish every connection it
             accepted, with no deadline; then leave the registry, say 'bye' and exit 0.
A newer worker and an older one share the listeners, the admission queue, the slots and the peer slots, so both may
accept for a moment (the overlap) without any request being refused or cut.

The lifeline to the supervisor is one JSON object a line over its control socket (LOCALMODELS_SUPERVISOR_SOCK). A worker
that loses it while warming exits; one that serves or drains carries on and reconnects: it never drains because its
supervisor went away.
"""
import importlib, json, os, select, signal, socket, struct, sys, threading, time, traceback, urllib.request

from . import HOME, VERSION, batches, errors, gateway, llmshare, maint, peers, web

CHANNEL = 1                       # the lifeline protocol (supervisor.CHANNEL)
WARM_LISTING = 45                 # seconds a warming worker tries to read the model listing
SELF_TEST = 20                    # seconds each self-test request may take
PROGRESS_EVERY = 5
ROLE_TRY = 0.5                    # a serving worker tries to take a free role this often
IM_HANDOVER = 30                  # seconds a draining worker waits for another to take the instance manager
ROLES = ('instance-manager', 'peer-poller', 'memory-sampler', 'link-listener', 'batch-runner')
LAZY = ('models', 'crashes', 'recycle', 'dash', 'peers', 'web', 'maint', 'runs', 'names', 'sessions', 'whois', 'errors')


def peer_uid(s):
    """The uid of the process at the other end of a unix socket (LOCAL_PEERCRED), or None when it cannot be read."""
    try: return struct.unpack_from('=II', s.getsockopt(0, getattr(socket, 'LOCAL_PEERCRED', 1), 256))[1]
    except (OSError, struct.error): return None


class Lifeline:
    """The worker's line to the supervisor: one JSON object a line, both ways."""
    def __init__(self, path):
        self.path, self.sock, self.buf, self.lock = path, None, b'', threading.Lock()

    def connect(self, wait=10.0):
        end = time.time() + wait
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(self.path)
                if peer_uid(s) not in (None, os.getuid()): raise PermissionError('control socket held by another user')
                self.sock, self.buf = s, b''
                return True
            except OSError:
                s.close()
                if time.time() >= end: return False
                time.sleep(0.2)

    def send(self, op, **fields):
        data = (json.dumps(dict(op=op, **fields)) + '\n').encode()
        with self.lock:
            if self.sock is None: return False
            try: self.sock.sendall(data); return True
            except OSError: self._close(); return False

    def read(self):
        """The messages that arrived (call it when the socket is readable). -> a list, or None at EOF."""
        try: data = self.sock.recv(65536)
        except OSError: data = b''
        if not data:
            with self.lock: self._close()
            return None
        self.buf += data
        out = []
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            try: m = json.loads(line)
            except ValueError: continue
            if isinstance(m, dict): out.append(m)
        return out

    def _close(self):
        if self.sock is not None:
            try: self.sock.close()
            except OSError: pass                             # not recorded: closing a socket that already failed
        self.sock = None


class Prefixed:
    """A stream whose every line starts with '[gen N pid P] ', so the supervisor's log tells its workers apart."""
    def __init__(self, stream, prefix): self.stream, self.prefix, self.start, self.lock = stream, prefix, True, threading.Lock()

    def write(self, s):
        with self.lock:
            out = []
            for part in str(s).splitlines(True):
                if self.start: out.append(self.prefix)
                out.append(part); self.start = part.endswith('\n')
            self.stream.write(''.join(out))
        return len(s)

    def flush(self): self.stream.flush()

    def __getattr__(self, k): return getattr(self.stream, k)


class Worker:
    def __init__(self, env=None):
        e = env or os.environ
        self.gen = e.get('LOCALMODELS_GEN') or '?'
        self.run_id = e.get('LOCALMODELS_RUN')
        self.sup_pid = int(e.get('LOCALMODELS_SUPERVISOR') or 0)
        self.listen = json.loads(e['LOCALMODELS_LISTEN'])
        self.line = Lifeline(e.get('LOCALMODELS_SUPERVISOR_SOCK') or '')
        self.release = os.path.basename(HOME) if os.path.basename(os.path.dirname(HOME)) == 'releases' else None
        self.role, self.servers, self.serving, self.role_threads, self.held = 'warming', {}, [], [], set()
        self.stop, self.flags = threading.Event(), {'drain': False}
        self.ready, self.warm_error, self.listing, self.im_released_at, self.reg = False, None, None, None, None
        self.wake_r, self.wake_w = os.pipe()
        self.lost_at = None

    # ---- the life
    def run(self):
        self._signals()                                                  # (a)
        sys.stdout, sys.stderr = Prefixed(sys.stdout, f'[gen {self.gen} pid {os.getpid()}] '), Prefixed(sys.stderr, f'[gen {self.gen} pid {os.getpid()}] ')
        try:                                                             # (b) a thread, a socket and a lock per waiting request
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, 8192), hard), hard))
        except (ValueError, OSError) as e: errors.record('worker: file limit', e)
        peers.LINK.leader = False                                        # a follower until it holds the peer-poller role
        self.reg = llmshare.register(dict(gen=self.gen, run=self.run_id, release=self.release, code=gateway.code_digest(), version=VERSION,
                                          channel=CHANNEL, role='warming', accepting=False, supervisor=self.sup_pid), fill=self.counts)   # (c)
        gateway.SELF.update(role='warming', release=self.release, listeners=self.keys_view())
        if not self.line.connect(10):                                    # (d)
            print('worker: no supervisor on the control socket: exit', flush=True); self.reg.close(); return 3
        self.line.send('hello', **self.hello())
        threading.Thread(target=self.warm, daemon=True, name='warm-up').start()
        return self.loop()

    def hello(self):
        return dict(pid=os.getpid(), gen=self.gen, run=self.run_id, release=self.release, code=gateway.code_digest(), version=VERSION, channel=CHANNEL,
                    state_protocol=llmshare.STATE_PROTOCOL, role=self.role, keys=sorted(self.servers or self.listen), accepting=self.role == 'serving')

    def warm(self):
        """(e) to (l), in this helper thread while the main thread reads the lifeline."""
        try:
            self.adopt()                                                 # (e)
            for f in os.listdir(llmshare.INFLIGHT) if os.path.isdir(llmshare.INFLIGHT) else ():
                if f.startswith(f'{os.getpid()}-'):                      # (f) left by an earlier process with this pid
                    try: os.remove(os.path.join(llmshare.INFLIGHT, f))
                    except OSError: pass
            llmshare.inflight(); llmshare.leases()
            for name in LAZY: importlib.import_module(f'localmodels.{name}')   # (g) a missing file fails now, not mid-drain
            threading.Thread(target=gateway.beats, daemon=True, name='beats').start()   # (h)
            self.listing = self.read_listing()                           # (i)
            peers.LINK.follow(); web.load_tail()                          # (j)
            self.self_test()                                             # (k)
            self.ready = True
            self.line.send('ready', keys=sorted(self.servers), listing=self.listing)   # (l)
        except Exception as e:
            errors.record('worker: warm-up', e)
            self.warm_error = ''.join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:]
            try: os.write(self.wake_w, b'!')
            except OSError: pass

    def adopt(self):
        """The listeners the supervisor passed down, as this process's servers (none serves before go)."""
        for key, e in list(self.listen.items()):
            fd = int(e['fd'])
            s = socket.socket(fileno=fd)
            os.set_inheritable(fd, False)                                # never kept open by an lms or caffeinate child
            s.setblocking(False)
            six = e.get('family') == 'inet6'
            cls = (gateway.LinkServer6 if six else gateway.LinkServer) if e.get('link') else (gateway.Server6 if six else gateway.Server)
            srv = cls(tuple(e['addr'][:2]), gateway.Door, bind_and_activate=False)
            srv.socket.close(); srv.socket = s
            srv.server_address = s.getsockname()
            srv.server_name, srv.server_port = str(e['addr'][0]), int(e['addr'][1])
            if e.get('link'):
                # Older workers could pass an address-bound socket even if its interface restriction had failed.
                try:
                    address = srv.server_address[0]
                    if six and srv.server_address[3] and '%' not in address:
                        address += '%' + socket.if_indextoname(srv.server_address[3])
                    srv.iface = gateway.restrict_link(s, address)
                except OSError as ex:
                    srv.server_close(); self.drop_listener(key)
                    errors.record('adopt peer listener: bind to its interface', ex)
                    continue
                e['iface'] = srv.iface
                srv.hosts = frozenset(str(h).lower() for h in e.get('hosts') or [])
            self.servers[key] = srv
        gateway.SELF['listeners'] = self.keys_view()

    def keys_view(self):
        return {k: dict(addr=v.get('addr'), link=bool(v.get('link'))) for k, v in self.listen.items()}

    def read_listing(self):
        if maint.on(): return 'waived'                                  # the person is working on LM Studio
        seeded = llmshare.seed_listing(max_age=120)
        if os.environ.get('LOCALMODELS_URGENT') and seeded:             # a replacement after a crash: the seed is enough
            threading.Thread(target=lambda: llmshare.loaded(fresh=True, force=True), daemon=True).start()
            return 'seeded'
        end = time.time() + WARM_LISTING
        while time.time() < end:
            llmshare.loaded(fresh=True, force=True)
            if llmshare.read_ok(): return 'fresh'
            time.sleep(2)
        return 'seeded' if seeded else 'none'

    def self_test(self):
        """The gateway answers its own endpoints on a private port before it may accept anything (never a route that could
        start LM Studio's server)."""
        srv = gateway.Server(('127.0.0.1', 0), gateway.Door)
        threading.Thread(target=srv.serve_forever, kwargs=dict(poll_interval=0.2), daemon=True, name='self-test').start()
        try:
            port = srv.server_address[1]
            for path, kind in (('/hub/health', 'json'), ('/hub/status', 'json'), ('/hub/snapshot', 'json'), ('/', 'html')):
                req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={'Host': f'127.0.0.1:{gateway.PORT}'})
                with urllib.request.urlopen(req, timeout=SELF_TEST) as r: status, body = r.status, r.read()
                if status != 200: raise RuntimeError(f'self-test: GET {path} answered {status}')
                if kind == 'json': json.loads(body)
                elif b'<' not in body[:400]: raise RuntimeError(f'self-test: GET {path} is not a page')
        finally:
            srv.shutdown(); srv.server_close()

    def loop(self):
        """The main thread: the lifeline, the signals, progress, and the end of a drain."""
        last = 0.0
        while True:
            socks = [self.wake_r] + ([self.line.sock] if self.line.sock is not None else [])
            try: r, _, _ = select.select(socks, [], [], 1.0)
            except (InterruptedError, ValueError, OSError): r = []
            if self.wake_r in r:
                try: os.read(self.wake_r, 512)
                except OSError: pass
            if self.line.sock is not None and self.line.sock in r:
                msgs = self.line.read()
                if msgs is None: self.lost_at = time.time()
                for m in msgs or ():
                    out = self.handle(m)
                    if out is not None: return out
            if self.warm_error and self.role == 'warming':
                self.line.send('failed', why=self.warm_error); print(f'worker: warm-up failed: {self.warm_error}', flush=True)
                return self.leave(1)
            if self.flags['drain']:
                if self.role == 'warming': print('worker: stopped while warming: exit', flush=True); return self.leave(0)
                self.drain()
            if self.line.sock is None:
                if self.role == 'warming': print('worker: supervisor gone while warming: exit', flush=True); return self.leave(0)
                self.reconnect()
            now = time.time()
            if self.role != 'warming' and now - last >= PROGRESS_EVERY:
                self.line.send('progress', **self.counts()); last = now
            if self.role == 'draining' and self.drained():
                print(f'worker: drained, exit ({self.counts()})', flush=True)
                self.reg.close(); self.line.send('bye')
                return 0

    def handle(self, m):
        op = m.get('op')
        if op == 'welcome':
            new = int(m.get('supervisor') or 0)
            if new and new != self.sup_pid:                              # a new supervisor adopted this worker: leases follow it
                old, self.sup_pid = self.sup_pid, new
                gateway.rekey(old, new); self.reg.update(supervisor=new)
            if m.get('run'): self.run_id = m['run']
        elif op == 'go':
            if self.role == 'warming' and self.ready: self.go()
        elif op == 'drain': self.drain()
        elif op == 'stop': self.shutdown(float(m.get('deadline') or time.time()))
        elif op == 'abort':
            if self.role == 'warming': print('worker: aborted while warming: exit', flush=True); return self.leave(0)
            self.drain()
        elif op == 'send-listeners': self.send_listeners(m.get('keys') or list(self.servers))
        return None                                                      # unknown ops are ignored

    def go(self):
        for key, srv in self.servers.items():
            if srv.link: continue                         # the link-listener role serves inherited sockets too
            th = threading.Thread(target=srv.serve_forever, kwargs=dict(poll_interval=0.5), daemon=True, name=f'serve {key}')
            th.start(); self.serving.append(srv)
        self.role = 'serving'; gateway.SELF['role'] = 'serving'
        self.reg.update(role='serving', accepting=True)
        for name in ROLES:
            if name == 'instance-manager' and os.environ.get('LOCALMODELS_NO_KEEPER'): continue
            th = threading.Thread(target=self.hold_role, args=(name,), daemon=True, name=f'role {name}'); th.start()
            self.role_threads.append(th)
        self.line.send('accepting')
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} local-models gateway {VERSION} (gen {self.gen}) accepting on {', '.join(sorted(self.servers))}", flush=True)

    def hold_role(self, name):
        """Take the role whenever nobody holds it, run its loop, give it up when the loop returns (at stop, between passes)."""
        role = llmshare.Role(name, gen=self.gen)
        while not self.stop.is_set():
            if not role.try_take():
                self.stop.wait(ROLE_TRY); continue
            self.held.add(name)
            try: self.role_loop(name)
            except Exception as e: errors.record(f'role {name}', e)
            finally:
                role.release(); self.held.discard(name)
                if name == 'instance-manager': self.im_released_at = time.time()
            if not self.stop.is_set(): self.stop.wait(1)                 # it ended on its own: take it again after a pause

    def role_loop(self, name):
        if name == 'instance-manager': gateway.keeper(self.stop)
        elif name == 'peer-poller': peers.poller(self.stop)
        elif name == 'memory-sampler': web.sampler(self.stop)
        elif name == 'batch-runner': batches.recover(self.stop)
        elif name == 'link-listener':
            adopted = {k: s for k, s in self.servers.items() if self.listen.get(k, {}).get('link')}
            gateway.listen_link(self.stop, adopted=adopted, hold=self.hold_listener, drop=self.drop_listener)

    def drain(self):
        """Stop accepting (this process's copies close; the listeners stay open in the supervisor and the newer worker),
        give the roles up between two passes, finish every accepted connection."""
        if self.role != 'serving': return
        self.role = 'draining'; gateway.SELF['role'] = 'draining'
        self.reg.update(role='draining', accepting=False)
        self.line.send('draining')
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} worker: draining ({gateway.conns()} connections open)", flush=True)

        def close():
            for srv in list(self.serving):
                try: srv.shutdown(); srv.server_close()
                except Exception as e: errors.record('drain: stop a listener', e)
        threading.Thread(target=close, daemon=True, name='stop accepting').start()
        self.stop.set()
        peers.LINK.leader = False

    def drained(self):
        if gateway.conns() > 0 or any(th.is_alive() for th in self.role_threads): return False
        if self.im_released_at is not None:                              # the instance manager has moved on, or nobody is there to take it
            holder = llmshare.Role('instance-manager').holder()
            others = [r for r in llmshare.live_gateways() if r.get('pid') != os.getpid()]
            if not (holder and holder != os.getpid()) and others and time.time() - self.im_released_at < IM_HANDOVER: return False
        return True

    def shutdown(self, deadline):
        """The supervisor stops: stop accepting at once, stop the roles, wait for the open connections until the deadline."""
        for srv in list(self.serving):
            threading.Thread(target=lambda s=srv: (s.shutdown(), s.server_close()), daemon=True).start()
        self.stop.set()
        while gateway.conns() > 0 and time.time() < deadline: time.sleep(0.1)
        try: sys.stdout.flush(); sys.stderr.flush()
        except OSError: pass
        try: self.reg.close()
        except OSError: pass
        os._exit(0)

    def leave(self, code):
        for srv in self.servers.values():
            try: srv.server_close()
            except OSError: pass
        try: self.reg.close()
        except OSError: pass
        return code

    def reconnect(self):
        """The supervisor went away (an upgrade, a crash): serve on, and reconnect every second (every 5 s after a minute)."""
        lost = self.lost_at or time.time()
        if time.time() - lost > 60 and int(time.time()) % 5: return
        if self.line.connect(0.0): self.line.send('hello', **self.hello())

    def send_listeners(self, keys):
        """The supervisor asks for listener copies (it was restarted and adopts this worker): one 'hold', every fd at once."""
        items = [(k, self.servers[k]) for k in keys if k in self.servers]
        if items: self.hold_listeners(items)

    def hold_listener(self, key, srv):
        """A link listener this worker bound: the supervisor keeps a copy and passes it to every later worker."""
        self.servers[key] = srv
        self.listen[key] = dict(family='inet6' if srv.address_family == socket.AF_INET6 else 'inet', addr=list(srv.server_address[:2]),
                                link=True, iface=srv.iface, hosts=sorted(srv.hosts))
        self.hold_listeners([(key, srv)])
        gateway.SELF['listeners'] = self.keys_view()

    def drop_listener(self, key):
        self.servers.pop(key, None)
        self.listen.pop(key, None)
        self.line.send('drop', key=key)
        gateway.SELF['listeners'] = self.keys_view()

    def hold_listeners(self, items):
        msg = dict(op='hold', pid=os.getpid(), items=[dict(key=k, **{f: self.listen.get(k, {}).get(f) for f in ('family', 'addr', 'link', 'iface', 'hosts')})
                                                    for k, _ in items])
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(10); s.connect(self.line.path)
            socket.send_fds(s, [(json.dumps(msg) + '\n').encode()], [srv.socket.fileno() for _, srv in items])
            s.shutdown(socket.SHUT_WR)
            reply = b''
            while True:
                x = s.recv(4096)
                if not x: break
                reply += x
            if b'"ok": true' not in reply and b'"ok":true' not in reply: errors.record('listeners: hold', words=reply[:200].decode('utf-8', 'replace') or 'no reply')
        except OSError as e: errors.record('listeners: hold', e)
        finally: s.close()

    def counts(self):
        """The registry row's moving parts, and the progress the supervisor shows: connections, marks, roles, errors."""
        marks = [dict(m) for m in list(gateway._marks.values())]
        queued = sum(1 for m in marks if m.get('state') == 'queued')
        answering = sum(1 for m in marks if m.get('state') == 'answering')
        oldest = min((float(m.get('t0') or m.get('since') or time.time()) for m in marks), default=None)
        es = errors.summary(10)
        return dict(open=gateway.conns(), queued=queued, answering=answering, oldest=round(time.time() - oldest, 1) if oldest else None,
                    accepting=self.role == 'serving', role=self.role, roles=sorted(self.held), errors=(es or {}).get('count', 0), internal=gateway._internal['n'])

    def _signals(self):
        os.set_blocking(self.wake_w, False)
        signal.set_wakeup_fd(self.wake_w)

        def term(signum, frame): self.flags['drain'] = True              # a flag only: the main loop acts on it
        signal.signal(signal.SIGTERM, term); signal.signal(signal.SIGINT, term)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)


def check():
    """`gateway --worker --check`: every module of this release imports, and the gateway answers its own endpoints on a
    private port (the warm-up's self-test). Registers nothing, sweeps nothing, binds no shared port. -> 0 or 1."""
    try:
        for name in LAZY: importlib.import_module(f'localmodels.{name}')
        Worker.self_test(None)
        print(f'worker check ok: {gateway.code_digest()}', flush=True)
        return 0
    except Exception as e:                                   # not recorded: the check's answer is its output and exit code
        print(f'worker check failed: {type(e).__name__}: {e}', flush=True)
        return 1


def main(argv=()):
    if '--check' in argv: return check()
    if not os.environ.get('LOCALMODELS_LISTEN'):
        print('gateway --worker runs only under the supervisor: localmodels gateway', flush=True)
        return 2
    return Worker().run()
