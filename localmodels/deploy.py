"""deploy - how `localmodels install` puts a release in front of the clients: the paths it chooses between, and the one-time
migration from a gateway that runs before releases.

install builds releases/<D> (cli.build_release: an immutable copy, named by its digest), then takes one path:

  fresh      no LaunchAgent loaded: SHARED/hub -> releases/D, the plist written, bootstrapped; gen 1 serves D.
  migrate    a gateway is loaded whose /hub/health has no "handoff": a gateway from before releases. Once, at a quiet
             moment (no request in flight, no slot held), under the swap lock: install binds the port itself the moment
             the old gateway goes (retried every 5 ms), converts SHARED/hub to a symlink, bootstraps the supervisor and
             hands it the listening sockets ('hold'), so connections wait in the backlog rather than being refused.
  plist      the LaunchAgent changed: a detach marker, bootout (the supervisor exits, its workers keep serving),
             bootstrap; the new supervisor adopts the workers; then the deploy below.
  upgrade    D's supervisor differs from the running one: `gateway --check` from D, then the 'upgrade' op (the supervisor
             points SHARED/hub at D and exits; launchd starts D's, which adopts the workers); then the deploy below.
  none       the serving worker already runs D: nothing to deploy.
  deploy     the 'deploy' op: a new worker warms beside the serving one and takes over; the old one drains. Nothing is
             refused or cut. A release whose shared-state protocol differs switches without overlap at a quiet moment.

Every path ends by checking /hub/health on 127.0.0.1 and [::1]: role serving, code D. -> (exit code, words): 0 done,
1 failed (what was there before is serving again), 3 busy or refused (nothing changed).
"""
import fcntl, json, os, plistlib, socket, subprocess, threading, time, urllib.request

from . import errors, llmshare

READY_MAX = 120          # seconds a new worker may take to serve
UPGRADE_WAIT = 30        # seconds for launchd's new supervisor after an upgrade or a plist change
DETACH_TTL = 60          # seconds a detach marker is valid
HOLD_EVERY = 0.005       # the migration's port holder retries its bind this often


def _say(say, words):
    if say: say(words)


def health(host='127.0.0.1', timeout=3):
    """The gateway's /hub/health on one address, or None."""
    url = f"http://{'[::1]' if host == '::1' else host}:{llmshare.HUB_PORT}/hub/health"
    req = urllib.request.Request(url, headers={'Host': f'localhost:{llmshare.HUB_PORT}'})
    try: return json.load(llmshare.urlopen(req, timeout=timeout))
    except (OSError, ValueError): return None


def serving(code, wait):
    """Wait until both addresses answer from a serving worker with this code, and the supervisor has no deploy under way
    (its overlap over). -> the health, or None."""
    end = time.time() + wait
    while time.time() < end:
        a, b = health(), health('::1')
        if a and b and a.get('role') == 'serving' and a.get('code') == code and b.get('code') == code \
                and not (supervisor_state() or {}).get('deploy'): return a
        time.sleep(0.25)
    return None


def run_dir():
    d = os.path.join(llmshare.SHARED, 'run'); os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def supervisor_state():
    """supervisor.json when its supervisor is alive (its lock held), else None."""
    return llmshare.supervisor()


def hub_path(): return os.path.join(llmshare.SHARED, 'hub')


def link_hub(d):
    """SHARED/hub -> releases/<D>, a relative symlink switched with one rename. A real directory there (a gateway from before
    releases) is renamed aside first, to releases/legacy-<its digest>; rename(2) cannot put a symlink over a directory, so
    the path is missing for microseconds between the two renames. -> the legacy folder, or None."""
    hub, legacy = hub_path(), None
    tmp = hub + f'.tmp-{os.getpid()}'
    if os.path.lexists(tmp): os.remove(tmp)
    os.symlink(os.path.relpath(d, llmshare.SHARED), tmp)
    if os.path.isdir(hub) and not os.path.islink(hub):
        from . import dash
        legacy = os.path.join(llmshare.SHARED, 'releases', f'legacy-{dash.digest(hub)}')
        if os.path.exists(legacy): legacy += f'-{int(time.time())}'
        os.rename(hub, legacy)
    os.replace(tmp, hub)
    return legacy


def unlink_hub(legacy):
    """The migration's revert: SHARED/hub a real directory again."""
    hub = hub_path()
    if os.path.islink(hub): os.remove(hub)
    if legacy and os.path.isdir(legacy): os.rename(legacy, hub)


def checks(d, py):
    """The release's own checks, before anything switches to it. -> words of the first that failed, or None."""
    for args in (['gateway', '--check'], ['gateway', '--worker', '--check']):
        try: r = subprocess.run([py, os.path.join(d, 'bin', 'localmodels'), *args], capture_output=True, text=True, timeout=120,
                                env=dict(os.environ, LOCALMODELS_PORT=str(llmshare.HUB_PORT)))
        except (OSError, subprocess.SubprocessError) as e: return f"{' '.join(args)}: {e}"
        if r.returncode: return f"{' '.join(args)}: {(r.stdout + r.stderr).strip()[-400:]}"
    return None


def write_plist(plist, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f: plistlib.dump(plist, f)


def choose(cli, d, supervisor_digest=None):
    """The path for release folder d. -> (path, words)."""
    from . import supervisor
    name = os.path.basename(d)
    if not cli._loaded():
        if supervisor_state() or supervisor.registry(): return 'busy', 'a supervisor or worker is still running: retry install'
        return 'fresh', 'no gateway loaded: a fresh start'
    h = health()
    sup = supervisor_state() or {}
    if not h:
        status = cli.supervisor_call('status', timeout=3) or {}
        h = health('::1')
        if not h:
            worker = next((w for w in status.get('workers', []) if w.get('role') == 'serving'), None)
            if not worker or worker.get('release') == name:
                return 'busy', 'gateway loaded but its health is unconfirmed: serving workers left running; retry install'
            h = dict(handoff=1)
        if status.get('ok'): sup = status
    if 'handoff' not in h:
        if sup or supervisor.registry(): return 'busy', 'registered gateway and HTTP health disagree: retry install'
        return 'migrate', f"the running gateway (pid {h.get('pid')}, {h.get('version')}) predates releases: the one-time migration"
    try: was = plistlib.load(open(cli.PLIST, 'rb'))
    except (OSError, ValueError): was = {}
    if was != cli._plist(): return 'plist', 'the LaunchAgent changed: the supervisor detaches, launchd starts it again, it adopts the workers'
    if supervisor_digest is None: supervisor_digest = json.load(open(os.path.join(d, 'RELEASE.json'))).get('supervisor')
    if sup and supervisor_digest and sup.get('digest') != supervisor_digest:
        return 'upgrade', f"the supervisor changes ({sup.get('digest')} to {supervisor_digest}): it hands over, its workers keep serving"
    if h.get('code') == name and h.get('role') == 'serving': return 'none', f'nothing to deploy: release {name} is serving'
    return 'deploy', f"a handoff to release {name}"


def install_gateway(cli, wait=90, force=False, dry=False, say=None):
    """Build the release and put it in front of the clients by the path choose() picks. -> (exit code, words)."""
    if dry:
        from . import dash, supervisor
        if llmshare._flock_test(os.path.join(llmshare.RUN, 'install.lock')): return 3, 'another install is in progress'
        name = dash.deploy_digest(cli.HOME)
        path, words = choose(cli, name, supervisor.supervisor_digest(cli.HOME))
        _say(say, f'release {name}: {words}')
        return (3, words) if path == 'busy' else (0, f'would take the {path} path (--dry: nothing changed)')
    lock = open(os.path.join(run_dir(), 'install.lock'), 'a+')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close(); return 3, 'another install is in progress'
    try:
        d = cli.build_release()
        path, words = choose(cli, d)
        _say(say, f'release {os.path.basename(d)}: {words}')
        if path == 'busy': return 3, words
        if path == 'none': return 0, words
        if path == 'fresh': return fresh(cli, d, say)
        if path == 'migrate': return migrate(cli, d, wait, say)
        if path == 'plist':
            code, w = replist(cli, d, say)
            if code: return code, w
            if (health() or {}).get('code') == os.path.basename(d): return 0, w
        if path == 'upgrade' or (path == 'plist' and choose(cli, d)[0] == 'upgrade'):
            code, w = upgrade(cli, d, say)
            if code: return code, w
        return handoff(cli, d, wait, force, say)
    finally:
        try: fcntl.flock(lock, fcntl.LOCK_UN)
        finally: lock.close()


def fresh(cli, d, say):
    uid = f'gui/{os.getuid()}'
    if cli._loaded(): return 3, 'a gateway was loaded during install: retry for a handoff'
    legacy = link_hub(d)
    if legacy: _say(say, f'the old installed copy kept as {legacy}')
    write_plist(cli._plist(), cli.PLIST)
    r = cli._launchctl('bootstrap', uid, cli.PLIST)
    h = serving(os.path.basename(d), READY_MAX)
    if not h: return 1, f"no worker serving release {os.path.basename(d)} within {READY_MAX} s: {(r.stderr or '').strip() or 'see gateway.log'}"
    return 0, f"gen {h.get('gen')} (pid {h.get('pid')}, release {h.get('code')}, {h.get('version')}) serving"


def handoff(cli, d, wait, force, say):
    name = os.path.basename(d)
    end = time.time() + 30
    while True:                                              # a deploy of its own under way (a replacement, a start): wait for it
        r = cli.supervisor_call('deploy', timeout=max(300, wait + READY_MAX + 60), say=say, release=name, force=force, wait=wait)
        if r is None: return 1, 'no answer from the supervisor (localmodels check)'
        if r.get('ok') or not str(r.get('why') or '').startswith('busy: gen') or 'warming' not in str(r.get('why')) or time.time() > end: break
        time.sleep(0.5)
    if not r.get('ok'):
        why = r.get('why') or r.get('words') or 'refused'
        return (3 if str(why).startswith('busy') or 'marked bad' in str(why) or 'quiet' in str(why) else 1), why
    h = serving(name, 15)
    if not h: return 1, f'the supervisor reported a handoff, but /hub/health does not show release {name} serving on both addresses'
    return 0, r.get('words') or f"gen {h.get('gen')} serving release {name}"


def _detach_marker(on):
    p = os.path.join(run_dir(), 'detach.json')
    if on: llmshare._write(p, dict(pid=os.getpid(), until=time.time() + DETACH_TTL))
    else:
        try: os.remove(p)
        except OSError: pass


def _new_supervisor(old_pid, digest=None, wait=UPGRADE_WAIT):
    """Wait for a live supervisor other than old_pid (with this digest, when given) that has adopted a serving worker."""
    end = time.time() + wait
    while time.time() < end:
        st = supervisor_state()
        if st and st.get('pid') != old_pid and (digest is None or st.get('digest') == digest) \
                and any(w.get('role') == 'serving' for w in st.get('workers') or []): return st
        time.sleep(0.25)
    return None


def replist(cli, d, say):
    """10.2: a new LaunchAgent without stopping the workers."""
    uid = f'gui/{os.getuid()}'
    old = supervisor_state() or {}
    try: was = open(cli.PLIST, 'rb').read()
    except OSError: was = None
    _detach_marker(True)
    try:
        write_plist(cli._plist(), cli.PLIST)
        cli._launchctl('bootout', f'{uid}/{cli.LABEL}')
        r = cli._launchctl('bootstrap', uid, cli.PLIST)
        if _new_supervisor(old.get('pid')): return 0, 'the LaunchAgent replaced; the new supervisor adopted the workers'
        if was is not None: open(cli.PLIST, 'wb').write(was)        # revert: the old bytes, bootstrapped
        cli._launchctl('bootout', f'{uid}/{cli.LABEL}'); cli._launchctl('bootstrap', uid, cli.PLIST)
        return 1, f"the new LaunchAgent started no supervisor ({(r.stderr or '').strip() or 'see gateway.log'}): the old one restored"
    finally: _detach_marker(False)


def upgrade(cli, d, say):
    """10.1: the supervisor hands over to the release's own."""
    bad = checks(d, cli.PY)
    if bad: return 1, f'release {os.path.basename(d)} failed its checks ({bad}): nothing changed'
    old = supervisor_state() or {}
    was = os.path.realpath(hub_path())
    want = json.load(open(os.path.join(d, 'RELEASE.json'))).get('supervisor')
    r = cli.supervisor_call('upgrade', release=os.path.basename(d))
    if not r or not r.get('ok'): return 1, f"upgrade refused: {(r or {}).get('why') or 'no answer from the supervisor'}"
    _say(say, 'the supervisor handed over; waiting for launchd to start the new one')
    if _new_supervisor(old.get('pid'), want): return 0, 'the supervisor upgraded; it adopted the workers'
    # The hub symlink chooses the next process, not the code of a failed new supervisor still held by launchd. Detach it
    # without cutting its workers, then explicitly start and verify the old supervisor.
    uid = f'gui/{os.getuid()}'
    _detach_marker(True)
    try:
        cli._launchctl('bootout', f'{uid}/{cli.LABEL}')
        link_hub(was)
        cli._launchctl('bootstrap', uid, cli.PLIST)
    except Exception as e:
        errors.record('install: supervisor rollback', e)
        return 1, f'no supervisor from release {os.path.basename(d)}; restoring the old supervisor failed: {type(e).__name__}: {e}'
    finally: _detach_marker(False)
    restored = _new_supervisor(old.get('pid'), old.get('digest'), wait=UPGRADE_WAIT) if old.get('digest') else None
    if restored: return 1, f'upgrade to {os.path.basename(d)} failed; the previous supervisor was restored and adopted its serving worker'
    return 1, f'upgrade to {os.path.basename(d)} failed and the previous supervisor could not be verified: localmodels check'


# ---------------------------------------------------------------- the one-time migration
class Holder:
    """The migration's port holder: from start() it retries binding 127.0.0.1:PORT and [::1]:PORT every HOLD_EVERY,
    listen() on the very next line, until both are held; a prober counts the connects refused meanwhile."""
    def __init__(self, port):
        self.port, self.socks, self.stop, self.refused, self.first, self.last = port, {}, threading.Event(), 0, None, None
        self.threads = []

    def _bind(self, key, six):
        while not self.stop.is_set() and key not in self.socks:
            s = socket.socket(socket.AF_INET6 if six else socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if six: s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                s.bind(('::1' if six else '127.0.0.1', self.port)); s.listen(128); s.setblocking(False)
                self.socks[key] = s; return
            except OSError: s.close(); time.sleep(HOLD_EVERY)

    def _probe(self):
        while not self.stop.is_set():
            c = socket.socket(); c.settimeout(0.5)
            try: c.connect(('127.0.0.1', self.port))
            except ConnectionRefusedError:
                self.refused += 1; self.first = self.first or time.time(); self.last = time.time()
            except OSError: pass                              # not recorded: a slow connect is not a refused one
            finally: c.close()
            time.sleep(0.02)

    def start(self):
        for target, args in ((self._bind, ('v4', False)), (self._bind, ('v6', True)), (self._probe, ())):
            th = threading.Thread(target=target, args=args, daemon=True); th.start(); self.threads.append(th)

    def held(self, wait):
        end = time.time() + wait
        while time.time() < end and len(self.socks) < 2: time.sleep(0.005)
        return len(self.socks) == 2

    def hand_over(self, sock_path):
        """Send the held listeners to the supervisor ('hold'), then close this process's copies. -> its reply."""
        items = [dict(key=k, family='inet6' if k == 'v6' else 'inet', addr=['::1' if k == 'v6' else '127.0.0.1', self.port], link=False) for k in self.socks]
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.settimeout(10)
        try:
            c.connect(sock_path)
            socket.send_fds(c, [(json.dumps(dict(op='hold', pid=os.getpid(), items=items)) + '\n').encode()], [self.socks[i['key']].fileno() for i in items])
            c.shutdown(socket.SHUT_WR)
            buf = b''
            while b'\n' not in buf:
                x = c.recv(4096)
                if not x: break
                buf += x
            return json.loads(buf.split(b'\n')[0] or b'{}')
        finally: c.close()

    def close(self):
        self.stop.set()
        for s in self.socks.values(): s.close()
        self.socks = {}

    def window(self):
        return f"{self.refused} connects refused" + (f" within {1000 * (self.last - self.first):.0f} ms" if self.refused > 1 else '')


def migrate(cli, d, wait, say):
    """13: from a gateway that predates releases, once. Staged first (nothing stopped); then at a quiet moment the old
    gateway is booted out while install holds the port, and the new supervisor is handed the listeners."""
    uid, name = f'gui/{os.getuid()}', os.path.basename(d)
    bad = checks(d, cli.PY)
    if bad: return 1, f'release {name} failed its checks ({bad}): nothing changed'
    try: was = open(cli.PLIST, 'rb').read()
    except OSError: was = None
    end = time.time() + wait
    while True:                                                  # (b) quiet, then the swap lock, then quiet again
        left = end - time.time()
        if left <= 0 or not cli.quiet(left, slots=True): return 3, f'no quiet moment within {wait / 60:.1f} min: nothing changed (localmodels install --wait 480)'
        with llmshare.swapping(wait=120) as got:
            if not got: continue
            if not cli.quiet(3, still=3, slots=True): continue
            return _switch(cli, d, uid, name, was, say)


def _switch(cli, d, uid, name, was, say):
    holder, legacy, ok = Holder(llmshare.HUB_PORT), None, False
    try:
        holder.start()                                           # (c) retries bind until the old gateway lets go
        cli._launchctl('bootout', f'{uid}/{cli.LABEL}')          # (d)
        if not holder.held(15): raise RuntimeError(f'port {llmshare.HUB_PORT} was not free 15 s after the bootout')
        legacy = link_hub(d)                                     # (e)
        write_plist(cli._plist(), cli.PLIST)                     # (f)
        r = cli._launchctl('bootstrap', uid, cli.PLIST)
        end, st = time.time() + 20, None                         # (g)
        while time.time() < end and not (st and st.get('sock')):
            st = supervisor_state(); time.sleep(0.05)
        if not st: raise RuntimeError(f"no supervisor within 20 s of bootstrap ({(r.stderr or '').strip() or 'see gateway.log'})")
        rep = holder.hand_over(st['sock'])
        if not rep.get('ok'): raise RuntimeError(f'the supervisor did not take the listeners: {rep}')
        holder.close()
        h = serving(name, READY_MAX)                             # (h)
        if not h: raise RuntimeError(f'no worker serving release {name} within {READY_MAX} s')
        ok = True
        return 0, f"migrated: gen {h.get('gen')} (pid {h.get('pid')}, release {name}) serving; {holder.window()}" + (f'; the old copy kept as {legacy}' if legacy else '')
    except Exception as e:                                       # the revert: the old gateway as it was
        errors.record('install: migration', e)
        cli._launchctl('bootout', f'{uid}/{cli.LABEL}')
        try: unlink_hub(legacy)
        except OSError as e2: _say(say, f'could not restore SHARED/hub: {e2}')
        if was is not None: open(cli.PLIST, 'wb').write(was)
        holder.close()
        cli._launchctl('bootstrap', uid, cli.PLIST)
        return 1, f'migration failed ({type(e).__name__}: {e}): the old gateway restored'
    finally:
        if not ok: holder.close()
