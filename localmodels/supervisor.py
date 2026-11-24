"""supervisor - the launchd job's process: it holds the gateway's listening sockets and runs gateway workers from
immutable release folders, so new code takes over without one connection refused or cut.

It binds 127.0.0.1:PORT and [::1]:PORT once (no port-sharing option) and passes the descriptors to each worker it starts
(worker.py). A deploy starts a new worker beside the serving one; the new one warms up and tests itself, the supervisor
says go, both accept for a moment (they share the admission queue, the slots and the peer slots), then the old one is
told to drain: it stops accepting and finishes every connection it accepted, with no deadline. A new release is on
probation for 10 minutes; one that fails goes back to the last good release. A worker that dies is replaced at once.

Standard library only, and nothing else from this package: a broken release can never break the supervisor. It never
handles HTTP, never reads the model settings and never talks to LM Studio. Its own errors go to errors.jsonl
(source 'supervisor'). One thread: a selectors loop over the control socket, its connections and a signal pipe.

  localmodels gateway            the supervisor (what launchd runs)
  localmodels gateway --check    import and validate a release's supervisor (exit 0 or 1)
"""
import errno, fcntl, glob, hashlib, json, os, selectors, signal, socket, struct, subprocess, sys, tempfile, time, traceback

CHANNEL = 1                       # the control channel's protocol (worker.CHANNEL)
STATE_PROTOCOL_KEY = 'state_protocol'
SOAK = 2.0                        # seconds the new and the old worker both accept before the old one drains
READY_MAX = 120                   # seconds a new worker may take to warm up
ACCEPT_MAX = 10                   # seconds from go to 'accepting'
PROBATION = 600                   # seconds a new release is watched before it is the last good one
MAX_DRAINING = 3
WEDGED = 60                       # seconds without a progress message
WEDGED_KILL = 120                 # seconds a wedged worker gets to say 'draining'
FAILOPEN_AFTER = 180              # seconds with no worker serving or warming
FAILOPEN_RETRY = (30, 60, 120, 300)
BACKOFF = (0, 1, 2, 5, 10, 30)    # seconds between replacements after unexpected exits
STOP_DEADLINE = 3.0
_timings = os.environ.get('LOCALMODELS_TIMINGS')      # the tests shorten these: {"SOAK": 0.5, ...}
if _timings:
    try: globals().update({k: v for k, v in json.loads(_timings).items() if k.isupper()})
    except ValueError: pass

SHARED = os.path.expanduser(os.environ.get('LOCALMODELS_STATE') or '~/Library/Application Support/LocalModels')
PORT = int(os.environ.get('LOCALMODELS_PORT') or 1235)
RUN = os.path.join(SHARED, 'run')
HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))     # the release this supervisor runs from
_last_err = {}


# ---------------------------------------------------------------- small things
def now(): return time.time()


def gen_of(x):
    """A worker's generation as a number (a worker's own messages carry it as text)."""
    try: return int(x)
    except (TypeError, ValueError): return 0


def log(words):
    print(f"[supervisor {os.getpid()}] {time.strftime('%Y-%m-%d %H:%M:%S')} {words}", flush=True)


def _error(where, exc=None, words=None):
    """One error record (errors.jsonl, source 'supervisor'), at most once a minute per place and message."""
    what = (f'{type(exc).__name__}: {exc}' if exc is not None else str(words or 'error'))[:300]
    key = (where, what)
    if now() - _last_err.get(key, 0) < 60: return
    _last_err[key] = now()
    rec = dict(at=round(now(), 1), source='supervisor', where=str(where)[:80], error=what, pid=os.getpid(),
               trace=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=6))[-2000:] if exc is not None else '')
    try:
        os.makedirs(SHARED, exist_ok=True)
        with open(os.path.join(SHARED, 'errors.jsonl'), 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX); f.write(json.dumps(rec) + '\n')
    except OSError: pass                                     # not recorded: the record itself cannot be written
    log(f'error ({where}): {what}')


def _write(path, rec):
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f: json.dump(rec, f)
    os.replace(tmp, path)


def flock_test(path):
    """True while some process holds path's lock, False when it is free, None when there is no such file."""
    try: fd = os.open(path, os.O_RDONLY)
    except OSError: return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError: return True
    finally: os.close(fd)


def alive(pid):
    try: os.kill(int(pid), 0); return True
    except PermissionError: return True
    except (OSError, ValueError, TypeError): return False


def digest(root, files):
    h = hashlib.sha256()
    for rel in files:
        try: data = open(os.path.join(root, rel), 'rb').read()
        except OSError: continue
        h.update(rel.encode() + b'\0'); h.update(len(data).to_bytes(8, 'big')); h.update(data)
    return h.hexdigest()[:16]


def supervisor_digest(root):
    """What this supervisor is: its own file and the command that starts it (a change to either is an upgrade)."""
    return digest(root, [os.path.join('localmodels', 'supervisor.py'), os.path.join('bin', 'localmodels')])


def release_info(d):
    """A release folder's RELEASE.json: {digest, version, state_protocol, channel, supervisor, built}, or None."""
    try: r = json.load(open(os.path.join(d, 'RELEASE.json')))
    except (OSError, ValueError): return None
    return r if isinstance(r, dict) else None


def current_release():
    """The release SHARED/hub points at: (its folder, its RELEASE.json or {})."""
    hub = os.path.join(SHARED, 'hub')
    d = os.path.realpath(hub)
    return d, release_info(d) or {}


def switch_hub(d):
    """SHARED/hub -> releases/<D>, switched in one rename (a relative link, so the state folder can move)."""
    hub = os.path.join(SHARED, 'hub')
    rel = os.path.relpath(os.path.realpath(d), os.path.realpath(SHARED))   # both resolved: /var is /private/var
    if os.path.islink(hub) and os.readlink(hub) == rel: return
    tmp = f'{hub}.{os.getpid()}.tmp'
    try: os.remove(tmp)
    except OSError: pass
    os.symlink(rel, tmp)
    os.replace(tmp, hub)


def sock_path():
    p = os.path.join(RUN, 'supervisor.sock')
    if len(p.encode()) <= 100: return p
    return os.path.join(tempfile.gettempdir(), f'lmh-{os.getuid()}-{hashlib.sha1(SHARED.encode()).hexdigest()[:12]}.sock')


def peer_uid(s):
    try: return struct.unpack_from('=II', s.getsockopt(0, getattr(socket, 'LOCAL_PEERCRED', 1), 256))[1]
    except (OSError, struct.error): return None


def bind_main(six):
    """A main listener: bind and listen back to back, non-blocking (every holder shares that flag)."""
    s = socket.socket(socket.AF_INET6 if six else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if six: s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind(('::1' if six else '127.0.0.1', PORT))
        s.listen(128)
        s.setblocking(False)
    except OSError: s.close(); raise
    return s


def holder_words():
    try: return subprocess.run(['lsof', '-nP', f'-iTCP:{PORT}', '-sTCP:LISTEN'], capture_output=True, text=True, timeout=5).stdout.strip()[-400:]
    except (OSError, subprocess.SubprocessError): return '(lsof unavailable)'


def registry():
    """The gateway registry's live rows (run/gateways/<pid>.lock held)."""
    out = []
    for f in glob.glob(os.path.join(RUN, 'gateways', '*.lock')):
        if flock_test(f) is not True: continue
        try: row = json.load(open(f[:-5] + '.json'))
        except (OSError, ValueError): row = {}
        out.append(dict(row if isinstance(row, dict) else {}, pid=int(os.path.basename(f)[:-5])))
    return out


# ---------------------------------------------------------------- the supervisor
class Conn:
    def __init__(self, sock):
        self.sock, self.buf, self.fds, self.kind, self.pid, self.eof = sock, b'', [], None, None, False


class Worker:
    def __init__(self, pid, gen, release, proc=None):
        self.pid, self.gen, self.release, self.proc = pid, gen, release, proc
        self.role, self.conn, self.ready, self.accepting, self.keys = 'warming', None, False, False, []
        self.started, self.progress_at, self.progress, self.failed = now(), now(), {}, None
        self.go_at, self.drain_at, self.listing, self.internal0 = None, None, None, None
        self.protocol = None                 # the shared-state protocol it said in its hello (a deploy compares with it)

    def view(self):
        return dict(pid=self.pid, gen=self.gen, release=os.path.basename(self.release or '') or None, role=self.role, accepting=self.accepting,
                    started=round(self.started, 1), adopted=self.proc is None, **{k: self.progress.get(k) for k in ('open', 'queued', 'answering', 'oldest', 'roles')})


class Supervisor:
    def __init__(self):
        self.lock_fd, self.listeners, self.meta = None, {}, {}      # key -> socket; key -> {family, addr, link, iface, hosts}
        self.workers, self.conns, self.flags = {}, {}, {'term': False, 'hup': False, 'chld': False}
        self.state = dict(current=None, last_good=None, bad=[], next_gen=1, probation=None, failopen=None)
        self.deploy, self.v6_error, self.v6_retry_at, self.exits = None, None, 0.0, []
        self.run, self.started, self.sel = None, now(), selectors.DefaultSelector()
        self.wake_r, self.wake_w = os.pipe()
        self.sock, self.path, self.replace_at, self.no_worker_since, self.starting = None, None, 0.0, None, True
        self.replace_fails = 0               # replacements in a row that failed while nothing served
        self.stopping = False

    # ---- start
    def start(self):
        os.makedirs(RUN, mode=0o700, exist_ok=True)
        self.lock_fd = os.open(os.path.join(RUN, 'supervisor.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        try: fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try: other = json.load(open(os.path.join(RUN, 'supervisor.json'))).get('pid')
            except (OSError, ValueError): other = '?'
            print(f'gateway supervisor already running (pid {other})', flush=True)
            return 1
        old = self.restore()
        self.signals()
        self.control()
        self.save()                                          # the control socket is known before the bind: install's 'hold' finds it
        live = [r for r in registry() if r.get('role') in ('serving', 'draining', 'warming')]
        self.run = (max(live, key=lambda r: float(r.get('started') or 0)).get('run') if live else None) or (old.get('run') if live else None) \
            or f'{os.getpid()}-{int(self.started)}'
        if not self.bind_listeners(adopting=any(r.get('role') == 'serving' for r in live)): return 1
        self.starting = False
        self.save()
        if not self.serving() and not any(w.role == 'warming' for w in self.workers.values()): self.begin(self.release_dir(None), 'start')
        log(f"supervisor {self.digest()} on port {PORT}: run {self.run}, listeners {', '.join(sorted(self.listeners)) or 'none'}")
        return self.loop()

    def restore(self):
        """What the last supervisor knew, from supervisor.json: the release that last served (current), the last good one,
        the bad ones, the next generation and a probation under way, so a restart (an upgrade, a crash, the next login)
        neither forgets them nor lets the hub link decide what starts next. -> the file's contents ({} if none)."""
        try: old = json.load(open(os.path.join(RUN, 'supervisor.json')))
        except (OSError, ValueError): old = {}
        if not isinstance(old, dict): old = {}
        for k in ('current', 'last_good', 'bad', 'next_gen', 'probation'):
            if old.get(k) is not None: self.state[k] = old[k]
        return old

    def begin(self, release_dir, why, urgent=False):
        """A worker to serve when none does: the deploy steps without a client waiting (ready, go, accepting)."""
        self.deploy = dict(release=release_dir, c=None, overlap=not urgent, started=now(), respawns=0, why=why, wait=0, worker=None, state='start', old=None)
        self.advance()

    def digest(self): return supervisor_digest(HERE)

    def signals(self):
        os.set_blocking(self.wake_w, False)
        signal.set_wakeup_fd(self.wake_w)

        def flag(name):
            def h(signum, frame): self.flags[name] = True
            return h
        signal.signal(signal.SIGTERM, flag('term')); signal.signal(signal.SIGINT, flag('term'))
        signal.signal(signal.SIGHUP, flag('hup')); signal.signal(signal.SIGCHLD, flag('chld'))
        self.sel.register(self.wake_r, selectors.EVENT_READ, 'wake')

    def control(self):
        self.path = sock_path()
        try: os.remove(self.path)                            # stale: this supervisor holds supervisor.lock
        except OSError: pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path); os.chmod(self.path, 0o600); s.listen(32); s.setblocking(False)
        self.sock = s
        self.sel.register(s, selectors.EVENT_READ, 'accept')

    def bind_listeners(self, adopting=False):
        """The main listeners. When the port is held: by live serving workers (this supervisor restarted), their copies are
        adopted over the control socket; else the bind is retried, and a 'hold' from install is accepted, for 120 s."""
        for key, six in (('v4', False), ('v6', True)):
            try: self.listeners[key] = bind_main(six); self.meta[key] = dict(family='inet6' if six else 'inet', addr=['::1' if six else '127.0.0.1', PORT], link=False)
            except OSError as e:
                if e.errno != errno.EADDRINUSE and six: self.v6_error, self.v6_retry_at = str(e), now() + 30; _error('listener [::1]', e); continue
                if e.errno != errno.EADDRINUSE: _error(f'listener {key}', e); return False
        if 'v4' in self.listeners: return True
        end = now() + (10 if adopting else 120)
        while now() < end:                                   # serving workers reconnect and are asked for their copies; or install holds
            self.step(0.02)
            if 'v4' in self.listeners: return True
            if not adopting:
                try: self.listeners['v4'] = bind_main(False); self.meta['v4'] = dict(family='inet', addr=['127.0.0.1', PORT], link=False); return True
                except OSError: pass
        print(f'port {PORT} is held and nothing handed it over: {holder_words()}', flush=True)
        return False

    # ---- workers
    def listen_map(self):
        return {k: dict(fd=s.fileno(), **self.meta.get(k, {})) for k, s in self.listeners.items()}

    def spawn(self, release_dir, urgent=False, why=''):
        gen = int(self.state['next_gen']); self.state['next_gen'] = gen + 1
        env = dict(os.environ, LOCALMODELS_LISTEN=json.dumps(self.listen_map()), LOCALMODELS_SUPERVISOR_SOCK=self.path,
                   LOCALMODELS_SUPERVISOR=str(os.getpid()), LOCALMODELS_GEN=str(gen), LOCALMODELS_RUN=self.run, LOCALMODELS_CHANNEL=str(CHANNEL))
        env.pop('LOCALMODELS_URGENT', None)
        if urgent: env['LOCALMODELS_URGENT'] = '1'
        py = os.environ.get('LOCALMODELS_PY') or sys.executable
        try:
            proc = subprocess.Popen([py, os.path.join(release_dir, 'bin', 'localmodels'), 'gateway', '--worker'], pass_fds=[s.fileno() for s in self.listeners.values()],
                                    close_fds=True, cwd='/', stdin=subprocess.DEVNULL, env=env)
        except OSError as e: _error('spawn a worker', e); return None
        w = self.workers[proc.pid] = Worker(proc.pid, gen, release_dir, proc)
        log(f"gen {gen} (pid {proc.pid}) starting from {os.path.basename(release_dir)}" + (f' ({why})' if why else ''))
        self.save()
        return w

    def serving(self): return [w for w in self.workers.values() if w.role == 'serving']

    def send(self, w, op, **fields):
        if w is None or w.conn is None: return False
        try: w.conn.sock.sendall((json.dumps(dict(op=op, **fields)) + '\n').encode()); return True
        except OSError: return False

    # ---- the loop
    def loop(self):
        while True:
            out = self.step(1.0)
            if out is not None: return out

    def step(self, timeout):
        try: events = self.sel.select(timeout)
        except (InterruptedError, OSError): events = []
        for key, mask in events:
            if key.data == 'wake':
                try: os.read(self.wake_r, 512)
                except OSError: pass
            elif key.data == 'accept': self.accept()
            else: self.readable(key.data)
        if self.flags['chld']: self.flags['chld'] = False; self.reap()
        if self.flags['term']:
            self.flags['term'] = False
            return self.detach('a detach marker') if self.marker() else self.stop()
        if self.flags['hup']: self.flags['hup'] = False; self.handoff(None, why='SIGHUP')
        if not self.starting:                                # (while adopting listeners, nothing is started)
            try: self.tick()
            except Exception as e: _error('supervisor: tick', e)
        return getattr(self, '_exit', None)

    def accept(self):
        while True:
            try: s, _ = self.sock.accept()
            except (BlockingIOError, OSError): return
            if peer_uid(s) not in (None, os.getuid()): s.close(); continue
            s.setblocking(False)
            c = Conn(s); self.conns[s.fileno()] = c
            self.sel.register(s, selectors.EVENT_READ, c)

    def readable(self, c):
        try: data, fds, _, _ = socket.recv_fds(c.sock, 65536, 8)
        except BlockingIOError: return
        except OSError: data, fds = b'', []
        c.fds += fds
        if not data: return self.closed(c)
        c.buf += data
        while b'\n' in c.buf:
            line, c.buf = c.buf.split(b'\n', 1)
            try: m = json.loads(line)
            except ValueError: continue
            if isinstance(m, dict):
                try: self.message(c, m)
                except Exception as e: _error(f"supervisor: {m.get('op')}", e)

    def closed(self, c):
        try: self.sel.unregister(c.sock)
        except (KeyError, ValueError): pass
        self.conns.pop(c.sock.fileno(), None)
        try: c.sock.close()
        except OSError: pass
        for fd in c.fds:
            try: os.close(fd)
            except OSError: pass
        w = self.workers.get(c.pid) if c.pid else None
        if w and w.conn is c:
            w.conn = None
            if w.proc is None and not flock_test(os.path.join(RUN, 'gateways', f'{w.pid}.lock')):   # an adopted worker that ended
                self.gone(w, None)

    def message(self, c, m):
        op = m.get('op')
        if op == 'hello': return self.hello(c, m)
        if op == 'hold': return self.hold(c, m)
        if op in ('status', 'deploy', 'restart', 'rollback', 'upgrade'): return self.cli(c, m)
        w = self.workers.get(c.pid)
        if w is None: return
        if op == 'ready':
            w.ready, w.keys, w.listing = True, list(m.get('keys') or []), m.get('listing')
            log(f"gen {w.gen} (pid {w.pid}) ready (listing {w.listing})")
            self.advance()
        elif op == 'failed':
            w.failed = str(m.get('why') or 'failed')[-600:]
            log(f"gen {w.gen} (pid {w.pid}) failed: {w.failed.strip().splitlines()[-1] if w.failed.strip() else ''}")
            self.advance()
        elif op == 'accepting':
            w.accepting, w.role = True, 'serving'
            self.advance()
        elif op == 'progress':
            w.progress, w.progress_at = dict(m), now()
            if w.internal0 is None and w.role == 'serving': w.internal0 = int(m.get('internal') or 0)
        elif op == 'draining': w.role, w.accepting = 'draining', False
        elif op == 'bye': w.role = 'gone'
        elif op == 'drop':
            k = m.get('key')
            if k in self.listeners and k not in ('v4', 'v6'):
                try: self.listeners.pop(k).close()
                except OSError: pass
                self.meta.pop(k, None); self.save()

    def hello(self, c, m):
        pid = int(m.get('pid') or 0)
        c.kind, c.pid = 'lifeline', pid
        w = self.workers.get(pid)
        if w is None:                                       # a worker from before this supervisor: adopted, with the release it runs
            w = self.workers[pid] = Worker(pid, gen_of(m.get('gen')), self.release_dir(m['release']) if m.get('release') else None, None)
            w.role, w.accepting, w.keys = m.get('role') or 'serving', bool(m.get('accepting')), list(m.get('keys') or [])
            w.protocol = m.get('state_protocol')
            log(f"gen {w.gen} (pid {pid}, release {m.get('release') or '?'}) adopted ({w.role})")
            missing = [k for k in w.keys if k not in self.listeners]
            w.conn = c; self.send(w, 'welcome', supervisor=os.getpid(), run=self.run)
            if missing and w.role == 'serving': self.send(w, 'send-listeners', keys=missing)
            if w.role == 'serving' and not self.deploy: self.reconcile()
            self.save()
            return
        w.conn = c
        if m.get('state_protocol') is not None: w.protocol = m.get('state_protocol')
        self.send(w, 'welcome', supervisor=os.getpid(), run=self.run)

    def reconcile(self):
        """Two or more serving workers adopted (a supervisor that crashed during an overlap): the newest gen serves, every
        older one drains (spec 9.5). Only adopted workers: a deploy's own overlap is the deploy's to end."""
        adopted = [w for w in self.serving() if w.proc is None]
        if len(adopted) < 2: return
        newest = max(adopted, key=lambda w: w.gen)
        for w in adopted:
            if w is not newest:
                self.send(w, 'drain'); w.role, w.drain_at = 'draining', now()
                log(f"gen {w.gen} (pid {w.pid}) draining: gen {newest.gen} is newer")

    def same_protocol(self, info, cur):
        """Whether a release (its RELEASE.json) shares the shared-state protocol of the serving worker cur: by what cur said
        in its hello, else by its release's file. No serving worker, or nothing known: True."""
        if not info or cur is None: return True
        theirs = cur.protocol
        if theirs is None and cur.release: theirs = (release_info(cur.release) or {}).get(STATE_PROTOCOL_KEY)
        return theirs is None or info.get(STATE_PROTOCOL_KEY) == theirs

    def hold(self, c, m):
        """Listener copies sent over this connection: validated, kept, and passed to every later worker."""
        items, fds, got = m.get('items') or [], list(c.fds), []
        c.fds = []
        for item, fd in zip(items, fds):
            try:
                s = socket.socket(fileno=fd)
                if s.type != socket.SOCK_STREAM or s.family not in (socket.AF_INET, socket.AF_INET6): raise OSError('not a TCP listener')
                addr = list(item.get('addr') or [])
                if addr and list(s.getsockname()[:2]) != [addr[0], int(addr[1])]: raise OSError(f'bound to {s.getsockname()[:2]}, not {addr}')
                s.listen(128); s.setblocking(False)
                key = item.get('key')
                old = self.listeners.pop(key, None)
                if old is not None: old.close()
                self.listeners[key] = s
                self.meta[key] = {k: item.get(k) for k in ('family', 'addr', 'link', 'iface', 'hosts')}
                got.append(key)
            except (OSError, ValueError) as e:
                _error('hold: a listener copy', e)
                try: os.close(fd)
                except OSError: pass
        for fd in fds[len(items):]:
            try: os.close(fd)
            except OSError: pass
        try: c.sock.sendall((json.dumps(dict(ok=bool(got), keys=got)) + '\n').encode())
        except OSError: pass
        if got: log(f"listeners held: {', '.join(got)}"); self.save()

    # ---- deploys
    def cli(self, c, m):
        c.kind = 'cli'
        op = m.get('op')
        if op == 'status': return self.reply(c, True, **self.view())
        if op == 'upgrade': return self.upgrade(c, m)
        if op == 'restart': release = None
        elif op == 'rollback':
            release = self.state.get('last_good')
            if not release: return self.reply(c, False, why='no last good release')
        else: release = m.get('release')
        self.handoff(release, c=c, overlap=m.get('overlap', True), force=bool(m.get('force')), wait=float(m.get('wait') or 0), why=op)

    def reply(self, c, ok, **fields):
        if c is None:
            if not ok: log(f"refused: {fields.get('why') or fields.get('words')}")
            return
        try: c.sock.sendall((json.dumps(dict(done=True, ok=ok, **fields)) + '\n').encode())
        except OSError: pass

    def say(self, c, words):
        if c is None: return
        try: c.sock.sendall((json.dumps(dict(progress=words)) + '\n').encode())
        except OSError: pass

    def release_dir(self, release):
        """A release's folder. None: the newest serving worker's; with none serving, the release that last served
        (state current), else the last good one, skipping any marked bad; the hub link only when neither is known."""
        if not release:
            w = max(self.serving(), key=lambda x: x.gen, default=None)
            if w and w.release: return w.release
            bad = set(self.state.get('bad') or [])
            for name in (self.state.get('current'), self.state.get('last_good')):
                if name and name not in bad and release_info(os.path.join(SHARED, 'releases', name)) is not None:
                    return os.path.realpath(os.path.join(SHARED, 'releases', name))
            return current_release()[0]
        d = release if os.path.isabs(release) else os.path.join(SHARED, 'releases', release)
        return os.path.realpath(d)

    def handoff(self, release, c=None, overlap=True, force=False, wait=0.0, why=''):
        """Start a new worker beside the serving one (5.2), or after it when the shared state's format changes (5.3)."""
        if self.deploy:
            b = self.deploy.get('worker')
            return self.reply(c, False, why=f"busy: gen {b.gen if b else '?'} warming release {os.path.basename(self.deploy['release'])}")
        draining = [w for w in self.workers.values() if w.role == 'draining']
        if len(draining) >= MAX_DRAINING:
            return self.reply(c, False, why='busy: ' + '; '.join(f"gen {w.gen} (pid {w.pid}) draining: {w.progress.get('open', '?')} connections, oldest {w.progress.get('oldest')} s" for w in draining))
        d = self.release_dir(release)
        info = release_info(d)
        if info is None and release: return self.reply(c, False, why=f'{d}: no RELEASE.json')
        name = os.path.basename(d)
        if name in (self.state.get('bad') or []) and not force: return self.reply(c, False, why=f'release {name} is marked bad (--force to deploy it anyway)')
        if info and (info.get('supervisor') not in (None, self.digest()) or int(info.get('channel') or CHANNEL) != CHANNEL):
            return self.reply(c, False, why='upgrade-required', supervisor=info.get('supervisor'))
        cur = max(self.serving(), key=lambda x: x.gen, default=None)
        same_protocol = self.same_protocol(info, cur)
        self.deploy = dict(release=d, c=c, overlap=overlap and same_protocol, started=now(), respawns=0, why=why, wait=wait, worker=None, state='start', old=cur)
        self.say(c, f"deploying {name}" + ('' if self.deploy['overlap'] else ' (no overlap: the shared state format changes)'))
        self.advance()

    def advance(self):
        """The deploy's next step, on each event and each tick. A step that fails ends the deploy (its new worker
        aborted, the serving one untouched): a step is never tried again by the next event, so no worker is started twice."""
        dp = self.deploy
        if not dp: return
        try: self._advance(dp)
        except Exception as e:
            _error('supervisor: deploy step', e)
            if self.deploy is dp:
                if dp.get('worker') is not None and dp['worker'].role == 'warming': self.abort(dp['worker'])
                self.finish(False, f'deploy step failed: {type(e).__name__}: {e}')

    def _advance(self, dp):
        st, b, c = dp['state'], dp.get('worker'), dp['c']
        if st == 'start':
            dp['state'] = 'spawning'                         # before anything that can fail: never twice
            if not dp['overlap']:
                olds = self.serving()
                if olds:
                    for w in olds: self.send(w, 'drain'); w.drain_at = now()
                    dp['state'] = 'waiting-old'; return
            dp['worker'] = self.spawn(dp['release'], urgent=not dp['overlap'], why=dp['why'])
            if dp['worker'] is None: return self.finish(False, 'the worker could not be started')
            dp['state'] = 'warming'; return
        if st == 'waiting-old':
            if any(w.role in ('serving', 'draining') for w in self.workers.values()): return
            dp['state'], dp['overlap'] = 'spawning', True    # nothing serves now: the new one starts at once
            dp['worker'] = self.spawn(dp['release'], urgent=True, why=dp['why'])
            if dp['worker'] is None: return self.finish(False, 'the worker could not be started')
            dp['state'] = 'warming'; return
        if st == 'warming':
            if b.failed or getattr(b, 'exited', False): return self.failed(b, b.failed or 'exited while warming')
            if now() - dp['started'] > READY_MAX: return self.failed(b, f'not ready within {READY_MAX} s', abort=True)
            if b.ready:
                if sorted(b.keys) != sorted(self.listeners):        # a link listener came or went while it warmed
                    if dp['respawns'] >= 2: return self.failed(b, 'the listener set kept changing', abort=True)
                    dp['respawns'] += 1; self.abort(b)
                    dp['worker'] = self.spawn(dp['release'], why='listeners changed'); return
                self.send(b, 'go'); b.go_at = now(); dp['state'] = 'go'; return
        if st == 'go':
            if b.failed or getattr(b, 'exited', False): return self.failed(b, b.failed or 'exited after go')
            if b.accepting: dp['state'], dp['soak'] = 'soak', now(); return
            if now() - b.go_at > ACCEPT_MAX: return self.failed(b, f'not accepting {ACCEPT_MAX} s after go', abort=True)
        if st == 'soak':
            if getattr(b, 'exited', False): return self.failed(b, 'exited during the overlap')
            if now() - dp['soak'] < SOAK: return
            olds = [w for w in self.serving() if w is not b]
            for w in olds: self.send(w, 'drain'); w.drain_at = now()
            try: switch_hub(dp['release'])
            except OSError as e: _error('switch hub', e)
            name = os.path.basename(dp['release'])
            if name != self.state.get('current'):
                self.state['probation'] = dict(release=name, until=now() + PROBATION, pid=b.pid)
            self.state['current'] = name
            took = now() - dp['started']
            msg = f"gen {b.gen} (pid {b.pid}, release {name}) serving after {took:.1f} s" + ''.join(
                f"; gen {w.gen} (pid {w.pid}) draining: {w.progress.get('open', '?')} connections, {w.progress.get('queued', 0)} queued" for w in olds)
            log(msg)
            self.finish(True, msg, gen=b.gen, pid=b.pid, draining=[dict(gen=w.gen, pid=w.pid) for w in olds])

    def failed(self, b, why, abort=False):
        if abort: self.abort(b)
        name, why_ = os.path.basename(self.deploy['release']), self.deploy['why']
        if why_ in ('replace', 'start', 'fail-open retry') and not self.serving():   # nothing serves: the next try waits (BACKOFF)
            self.replace_fails += 1
            self.replace_at = now() + BACKOFF[min(self.replace_fails, len(BACKOFF) - 1)]
        if not abort and name not in (self.state.get('bad') or []) and (why_ != 'replace' or self.replace_fails >= 3):
            self.state.setdefault('bad', []).append(name)
        tail = ''
        if b and b.proc is not None and b.proc.poll() is not None: tail = f' (exit status {b.proc.returncode})'
        self.finish(False, f'gen {b.gen if b else "?"}: {why}{tail}; the serving worker is untouched')

    def finish(self, ok, words, **fields):
        dp, self.deploy = self.deploy, None
        self.save()
        if dp: self.reply(dp['c'], ok, words=words, **fields)

    def abort(self, w):
        if w is None: return
        self.send(w, 'abort')
        if w.proc is not None:
            try: w.proc.terminate()
            except OSError: pass
            w.kill_at = now() + 5
        w.role = 'aborted'

    # ---- upgrades, detach, stop
    def upgrade(self, c, m):
        d = self.release_dir(m.get('release'))
        if not release_info(d): return self.reply(c, False, why=f'{d}: no RELEASE.json')
        try: switch_hub(d)
        except OSError as e: return self.reply(c, False, why=f'switch hub: {e}')
        self.state['upgrading'] = {'from': self.digest(), 'to': (release_info(d) or {}).get('supervisor'), 'at': round(now(), 1)}
        self.reply(c, True, words='the supervisor hands over: launchd starts the new one, which adopts the workers')
        self.detach('upgrade')

    def marker(self):
        """A valid detach marker (install changes the plist): its pid alive, its time not passed."""
        try: m = json.load(open(os.path.join(RUN, 'detach.json')))
        except (OSError, ValueError): return False
        return isinstance(m, dict) and alive(m.get('pid')) and now() < float(m.get('until') or 0)

    def detach(self, why):
        """Exit without stopping the workers: they keep their listener copies and serve; the next supervisor adopts them."""
        log(f'detaching ({why}): the workers keep serving')
        self.save()
        try: self.sock.close(); os.remove(self.path)
        except OSError: pass
        for s in self.listeners.values():
            try: s.close()
            except OSError: pass
        self._exit = 0
        return 0

    def stop(self):
        """SIGTERM with no detach marker (bootout, logout): every worker stops within the deadline, then this one exits."""
        log('stopping: the workers stop within 3 s')
        self.stopping = True                                 # their exits are expected: no probation failure, no replacement
        try: self.sock.close(); os.remove(self.path)
        except OSError: pass
        for s in self.listeners.values():
            try: s.close()
            except OSError: pass
        deadline = now() + STOP_DEADLINE
        for w in self.workers.values(): self.send(w, 'stop', deadline=deadline)
        while now() < deadline + 0.5 and any(self.running(w) for w in self.workers.values()): time.sleep(0.05); self.reap()
        for w in self.workers.values():
            if self.running(w):
                try:
                    if w.proc is not None: w.proc.kill()
                    elif flock_test(os.path.join(RUN, 'gateways', f'{w.pid}.lock')): os.kill(w.pid, signal.SIGKILL)
                except OSError: pass
        self.reap(); self.save()
        self._exit = 0
        return 0

    def running(self, w):
        if w.proc is not None: return w.proc.poll() is None
        return bool(flock_test(os.path.join(RUN, 'gateways', f'{w.pid}.lock')))

    # ---- exits and the tick
    def reap(self):
        for w in list(self.workers.values()):
            if w.proc is not None and w.proc.poll() is not None: self.gone(w, w.proc.returncode)

    def gone(self, w, status):
        if w.pid not in self.workers: return
        del self.workers[w.pid]
        w.exited = True
        expected = w.role in ('draining', 'gone', 'aborted', 'warming') or self.stopping   # a warming worker's end is the deploy's to judge
        log(f"gen {w.gen} (pid {w.pid}) exited" + (f' with status {status}' if status is not None else '') + ('' if expected else ' unexpectedly'))
        if self.deploy and self.deploy.get('worker') is w: self.advance()
        if not expected and w.role == 'serving':
            self.exits = [t for t in self.exits if now() - t < 600] + [now()]
            prob = self.state.get('probation')
            if prob and prob.get('pid') == w.pid and now() < prob['until']:
                self.state.setdefault('bad', []).append(prob['release'])
                self.state['probation'] = None
                back = self.state.get('last_good')
                _error('probation', words=f"release {prob['release']} failed on probation; back to {back}")
                if back:
                    try: switch_hub(os.path.join(SHARED, 'releases', back))
                    except OSError as e: _error('switch hub', e)
            self.replace_at = now() + BACKOFF[min(len(self.exits) - 1, len(BACKOFF) - 1)]
        self.save()

    def tick(self):
        t = now()
        if 'v6' not in self.listeners and self.v6_error and t >= self.v6_retry_at:
            try:
                self.listeners['v6'] = bind_main(True); self.meta['v6'] = dict(family='inet6', addr=['::1', PORT], link=False); self.v6_error = None
                log('listener [::1] bound: a same-release handoff gives it to the workers'); self.handoff(None, why='listener added')
            except OSError as e: self.v6_error, self.v6_retry_at = str(e), t + 30
        for w in list(self.workers.values()):
            if getattr(w, 'kill_at', None) and t > w.kill_at and w.proc is not None and w.proc.poll() is None:
                try: w.proc.kill()
                except OSError: pass
            if w.role == 'serving' and t - w.progress_at > WEDGED and not self.deploy and w.accepting:
                _error('wedged worker', words=f'gen {w.gen} (pid {w.pid}): no progress for {WEDGED} s; handing off')
                w.progress_at = t; w.wedged_at = t
                self.handoff(None, why='wedged')
            if getattr(w, 'wedged_at', None) and w.role == 'serving' and t - w.wedged_at > WEDGED_KILL:
                _error('wedged worker', words=f'gen {w.gen} (pid {w.pid}) never drained: SIGKILL')
                try: os.kill(w.pid, signal.SIGKILL)
                except OSError: pass
            if w.proc is None and w.conn is None and not self.running(w): self.gone(w, None)
        prob = self.state.get('probation')
        if prob:
            b = self.workers.get(prob.get('pid'))
            if b and b.internal0 is not None and int(b.progress.get('internal') or 0) - b.internal0 >= 3 and not self.deploy:
                _error('probation', words=f"release {prob['release']}: three internal errors; back to {self.state.get('last_good')}")
                self.state.setdefault('bad', []).append(prob['release']); self.state['probation'] = None
                if self.state.get('last_good'): self.handoff(self.state['last_good'], why='rollback', force=True)
            elif t > prob['until']:
                self.state['last_good'], self.state['probation'] = prob['release'], None; self.save()
        if self.deploy: self.advance()
        live = [w for w in self.workers.values() if w.role in ('serving', 'warming')]
        if not self.serving() and not self.deploy and t >= self.replace_at and not self.state.get('failopen'):
            exits = [x for x in self.exits if t - x < 600]
            d = os.path.join(SHARED, 'releases', self.state['last_good']) if len(exits) >= 3 and self.state.get('last_good') else None
            target = d or self.release_dir(None)
            if os.path.basename(target) in (self.state.get('bad') or []): pass   # no release left to try: fail-open comes
            elif not [w for w in self.workers.values() if w.role == 'warming']: self.begin(target, 'replace', urgent=True)
        if self.serving(): self.replace_fails = 0
        if live: self.no_worker_since = None
        elif self.no_worker_since is None: self.no_worker_since = t
        elif t - self.no_worker_since > FAILOPEN_AFTER and not self.state.get('failopen'): self.failopen()
        fo = self.state.get('failopen')
        if fo and t >= fo.get('retry_at', 0) and not self.deploy:
            tries = int(fo.get('tries', 0)) + 1
            self.state['failopen'] = dict(fo, tries=tries, retry_at=t + FAILOPEN_RETRY[min(tries, len(FAILOPEN_RETRY) - 1)])
            if self.rebind():
                self.state['failopen'], self.no_worker_since = None, t
                self.begin(self.release_dir(None), 'fail-open retry', urgent=True)

    def rebind(self):
        """The main listeners again, after fail-open closed them (one try each)."""
        for key, six in (('v4', False), ('v6', True)):
            if key in self.listeners: continue
            try: self.listeners[key] = bind_main(six); self.meta[key] = dict(family='inet6' if six else 'inet', addr=['::1' if six else '127.0.0.1', PORT], link=False)
            except OSError as e: _error(f'listener {key}: bind again', e)
        return 'v4' in self.listeners

    def failopen(self):
        """No worker could start for FAILOPEN_AFTER: answer what waits in the backlog with 503, then close the listeners, so
        new connections are refused and clients go straight to LM Studio."""
        body = json.dumps(dict(error='no gateway worker could start; see ~/Library/Logs/LocalModels/gateway.log')).encode()
        resp = (b'HTTP/1.0 503 Service Unavailable\r\nRetry-After: 30\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % len(body)) + body
        for s in list(self.listeners.values()):
            while True:
                try: conn, _ = s.accept()
                except OSError: break
                try:
                    conn.setblocking(True); conn.settimeout(1.0); conn.sendall(resp); conn.shutdown(socket.SHUT_WR); conn.recv(4096)
                except OSError: pass                         # not recorded: a client that left; the event itself is recorded below
                finally: conn.close()
            s.close()
        self.listeners.clear()
        self.state['failopen'] = dict(at=round(now(), 1), retry_at=now() + FAILOPEN_RETRY[0], tries=0)
        _error('fail-open', words=f'no gateway worker serving or warming for {FAILOPEN_AFTER} s: listeners closed, clients go straight to LM Studio')
        self.save()

    def view(self):
        return dict(pid=os.getpid(), started=round(self.started, 1), run=self.run, digest=self.digest(), code_dir=HERE, sock=self.path, port=PORT,
                    listeners={k: dict(addr=self.meta.get(k, {}).get('addr'), link=bool(self.meta.get(k, {}).get('link'))) for k in self.listeners},
                    v6_error=self.v6_error, workers=[w.view() for w in sorted(self.workers.values(), key=lambda w: w.gen or 0)],
                    deploy=dict(release=os.path.basename(self.deploy['release']), state=self.deploy['state']) if self.deploy else None,
                    **{k: self.state.get(k) for k in ('current', 'last_good', 'bad', 'next_gen', 'probation', 'failopen', 'upgrading')}, at=round(now(), 1))

    def save(self):
        try: _write(os.path.join(RUN, 'supervisor.json'), self.view())
        except OSError as e: _error('supervisor.json', e)


def wanted(argv=()):
    """Whether `localmodels gateway` is the supervisor: with --check, and whenever SHARED/hub is a release folder (it has
    RELEASE.json). A hub copied by an install from before releases runs one gateway on its own, as before."""
    return '--check' in argv or os.path.exists(os.path.join(SHARED, 'hub', 'RELEASE.json'))


def check():
    """`gateway --check`: this release's supervisor imports, its paths are sound, supervisor.json (if any) reads."""
    try:
        p = sock_path()
        if len(p.encode()) > 103: raise ValueError(f'control socket path too long: {p}')
        if os.path.exists(os.path.join(RUN, 'supervisor.json')): json.load(open(os.path.join(RUN, 'supervisor.json')))
        print(f'supervisor {supervisor_digest(HERE)} ok: socket {p}', flush=True)
        return 0
    except (OSError, ValueError) as e:
        print(f'supervisor check failed: {e}', flush=True)
        return 1


def main(argv=()):
    if '--check' in argv: return check()
    return Supervisor().start()
