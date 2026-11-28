#!/usr/bin/env python3
"""install's paths and the supervisor's failure rules, tested end to end in a sandbox, with launchd played by the test
(cli._launchctl stubbed: bootstrap starts the plist's program, bootout ends it, KeepAlive starts it again). A fresh
install, nothing to deploy, a deploy, a LaunchAgent change and a supervisor upgrade, each under a new connection every 10 ms
on both addresses; a release on probation that dies (back to the last good one), a wedged worker (handed off), a draining
worker killed (only its own connections), a shared-state protocol change (no overlap, nothing refused), fail-open; then
the one-time migration from a gateway that predates releases (the last release before them, from git), and its revert.
Nothing real is loaded, and nothing touches this Mac's launchd. `localmodels check` runs it; so can you: python3 tests/test_supervisor.py
"""
import errno, json, os, plistlib, shutil, signal, socket, stat, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = tempfile.mkdtemp(prefix='lms-')                     # short: the control socket's path must fit 104 bytes
STATE = os.path.join(BOX, 's')


def _port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


PORT = _port()
os.makedirs(os.path.join(STATE, 'releases')); os.makedirs(os.path.join(BOX, 'logs'))
os.environ.update(HOME=BOX, LOCALMODELS_STATE=STATE, LOCALMODELS_PORT=str(PORT), LMSTUDIO_LOGS=os.path.join(BOX, 'logs'))   # before the package
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, 'tests'))
import test_handoff as H                                   # noqa: E402  (its pretend LM Studio, releases, hammer)
from localmodels import cli, deploy, llmshare, supervisor as SUP   # noqa: E402

LEGACY_TAG = 'v1.13.0'                                    # the last release before the supervisor
TIMINGS = dict(SOAK=0.5, PROBATION=4, FAILOPEN_AFTER=6, FAILOPEN_RETRY=[2, 2, 2, 2], WEDGED=15, WEDGED_KILL=30, STOP_DEADLINE=1.0)
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''), flush=True)


class Launchd:
    """launchctl for one job, played in the test: print, bootstrap (the plist's program and environment), bootout (SIGTERM
    to the job, or its whole process group without AbandonProcessGroup), and KeepAlive (an exited job is started again)."""
    def __init__(self, extra, log):
        self.extra, self.log, self.job, self.plist, self.loaded, self.calls, self.fail = extra, log, None, None, False, [], False
        self.lock = threading.Lock(); self.stop = threading.Event()
        threading.Thread(target=self.keepalive, daemon=True).start()

    def _start(self):
        p = self.plist
        env = dict(os.environ, **(p.get('EnvironmentVariables') or {}), **self.extra)
        self.job = subprocess.Popen(p['ProgramArguments'], env=env, stdout=self.log, stderr=self.log, start_new_session=True)

    def __call__(self, *args):
        self.calls.append(args[0])
        rc, err = 0, ''
        with self.lock:
            if args[0] == 'print': rc = 0 if self.loaded else 113
            elif args[0] == 'bootstrap':
                if self.loaded: rc, err = 5, 'already loaded'
                elif self.fail: rc, err = 5, 'Bootstrap failed: 5: Input/output error'
                else: self.plist = plistlib.load(open(args[2], 'rb')); self.loaded = True; self._start()
            elif args[0] == 'bootout':
                if not self.loaded: rc, err = 3, 'No such process'
                else: self.loaded = False; self._end()
            elif args[0] == 'kickstart':
                if '-k' in args and self.job: self._end()
                if not self.job or self.job.poll() is not None: self._start()
        return subprocess.CompletedProcess(args, rc, '', err)

    def _end(self):
        j = self.job
        if not j or j.poll() is not None: return
        try:
            if self.plist.get('AbandonProcessGroup'): j.send_signal(signal.SIGTERM)
            else: os.killpg(j.pid, signal.SIGTERM)
        except OSError: pass
        try: j.wait(10)
        except subprocess.TimeoutExpired: j.kill(); j.wait()

    def keepalive(self):
        while not self.stop.is_set():
            with self.lock:
                if self.loaded and self.job and self.job.poll() is not None and self.plist.get('KeepAlive'): self._start()
            time.sleep(0.2)


def status(timeout=5):
    try: return H.call(STATE, 'status', timeout=timeout)
    except OSError: return {}


def sup_pid():
    return (deploy.supervisor_state() or {}).get('pid')


def main():
    procs = []
    try: run(procs)
    finally:
        for p in procs:
            try: os.killpg(p.pid, signal.SIGKILL)
            except OSError: pass
        for pid in [r['pid'] for r in SUP.registry()] if os.path.isdir(os.path.join(STATE, 'run')) else []:
            try: os.kill(pid, signal.SIGKILL)
            except OSError: pass
        if os.environ.get('KEEP_LOG'): shutil.copy(os.path.join(BOX, 'gateway.log'), os.environ['KEEP_LOG'])
        shutil.rmtree(BOX, ignore_errors=True)
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


def unresponsive_worker_unit():
    """A worker that never reads its lifeline gets one replacement and one fixed kill deadline, not endless handoffs."""
    s = SUP.Supervisor()
    w = SUP.Worker(987654, 1, None)
    w.role, w.accepting = 'serving', True
    w.progress_at = time.time() - SUP.WEDGED - 1
    s.workers[w.pid] = w
    with patch.object(s, 'handoff') as handoff, patch.object(s, 'running', return_value=True), patch.object(SUP.os, 'kill') as kill:
        s.tick()
        first_at = w.wedged_at
        w.progress_at = time.time() - SUP.WEDGED - 1
        s.tick()
        t('an unresponsive worker keeps its original wedge deadline instead of starting another handoff',
          handoff.call_count == 1 and w.wedged_at == first_at, (handoff.call_count, first_at, w.wedged_at))
        w.wedged_at = time.time() - SUP.WEDGED_KILL - 1
        s.tick()
        t('the unresponsive worker is killed after that deadline even while still marked serving', kill.call_args.args == (w.pid, signal.SIGKILL), kill.call_args)


def ipv6_adoption_unit():
    """An IPv6 address held by a worker being adopted is a retryable state, not an immediate recorded gateway error."""
    s = SUP.Supervisor()
    conflict = OSError(errno.EADDRINUSE, 'Address already in use')
    with patch.object(SUP, 'bind_main', side_effect=[object(), conflict]), patch.object(SUP, '_error') as recorded:
        t('IPv6 EADDRINUSE during adoption is retained for retry without recording an error',
          s.bind_listeners() and s.v6_error and s.v6_retry_at and not recorded.called, s.v6_error)


def run(procs):
    SUP.SHARED, SUP.RUN = STATE, os.path.join(STATE, 'run')
    unresponsive_worker_unit()
    ipv6_adoption_unit()
    up = H.http.server.ThreadingHTTPServer(('127.0.0.1', 0), H.Fake); threading.Thread(target=up.serve_forever, daemon=True).start()
    ps = os.path.join(BOX, 'ps.json')
    json.dump([dict(identifier=i, modelKey=H.F, status='idle', sizeBytes=2**30, contextLength=65536, parallel=4, type='llm') for i in (H.F, H.F + ':2')], open(ps, 'w'))
    lms = os.path.join(BOX, 'lms')
    open(lms, 'w').write(f'#!/bin/sh\ncase "$1" in ps) cat {ps};; *) echo ok;; esac\n'); os.chmod(lms, stat.S_IRWXU)
    os.environ.update(LMSTUDIO_URL=f'http://127.0.0.1:{up.server_address[1]}', LMS=lms)
    logf = open(os.path.join(BOX, 'gateway.log'), 'w')
    ld = Launchd(dict(LOCALMODELS_NO_KEEPER='1', LOCALMODELS_TIMINGS=json.dumps(TIMINGS)), logf)
    cli._launchctl, cli.PLIST, cli.LOGS, cli.PY = ld, os.path.join(BOX, 'agent.plist'), os.path.join(BOX, 'logs'), sys.executable
    deploy.READY_MAX, deploy.UPGRADE_WAIT = 60, 30
    real_build, want = cli.build_release, [None]
    cli.build_release = lambda *a, **k: real_build(*a, **k) if a or k else (want[0] or real_build())   # install builds `want`; H.build its own
    say = []

    def tail(n=25):
        logf.flush(); return '\n'.join(open(os.path.join(BOX, 'gateway.log')).read().split('\n')[-n:])

    def idle():                                              # the supervisor's own deploy (a replacement, a start) over
        def no_deploy():
            st = status()                                    # supervisor.json may predate the replacement now warming
            return st.get('ok') and not st.get('deploy')
        assert H.wait_for(no_deploy, 30), 'the supervisor did not finish the handoff'

    def install(**kw):
        say.clear(); return deploy.install_gateway(cli, say=say.append, **kw)

    try:
        if os.environ.get('ONLY_MIGRATION'):                   # (to work on the migration alone)
            print('the one-time migration from a gateway that predates releases'); return migration(ld, logf, procs, tail)
        print('install: a fresh start (no LaunchAgent loaded)')
        code, words = install()
        h = H.health(PORT)
        d1 = os.path.basename(real_build())
        t('fresh: SHARED/hub -> releases/D, the plist with AbandonProcessGroup, bootstrapped; gen 1 serves D on both addresses',
          code == 0 and h and h.get('code') == d1 and H.health(PORT, '::1').get('code') == d1 and os.path.islink(os.path.join(STATE, 'hub'))
          and plistlib.load(open(cli.PLIST, 'rb')).get('AbandonProcessGroup') is True, (code, words, tail() if code else ''))
        procs.append(ld.job)

        print('install: nothing to deploy')
        ld.calls.clear()
        code, words = install()
        t('the serving worker runs this release: nothing done, no launchctl call but a look', code == 0 and 'nothing to deploy' in words
          and set(ld.calls) <= {'print'}, (code, words, ld.calls))
        code, words = install(dry=True)
        t('--dry names the path and changes nothing', code == 0 and 'none path' in words and H.health(PORT).get('code') == d1, words)

        print('install: a deploy (a handoff, no launchctl call), under load')
        r2 = H.build(STATE, 'r2', lambda d: open(os.path.join(d, 'localmodels', 'marker.txt'), 'w').write('r2\n'))
        want[0] = r2
        ld.calls.clear()
        probe = deploy.health
        def miss_once(*args, **kw):
            deploy.health = probe
            return None
        with H.held_stream(PORT, up) as stream:
            deploy.health = miss_once
            try:
                with H.Hammer(PORT) as hm: code, words = install()
            finally: deploy.health = probe
        t('a missed health probe during install preserves the active stream', stream['done'] and stream['events'] == 17 and 'bootout' not in ld.calls, stream)
        t('deploy: release 2 serves on both addresses, nothing refused or cut, launchd untouched', code == 0 and H.health(PORT).get('code') == os.path.basename(r2)
          and not hm.errors and hm.n > 20 and set(ld.calls) <= {'print'}, (code, words, hm.errors[:3], ld.calls))

        print('install: the LaunchAgent changed (a detach marker, bootout, bootstrap; the workers keep serving)')
        os.makedirs(os.path.join(BOX, 'logs2')); os.environ['LMSTUDIO_LOGS'] = os.path.join(BOX, 'logs2')
        p0 = sup_pid()
        with H.Hammer(PORT) as hm: code, words = install()
        t('plist: a new supervisor adopted the serving worker; nothing refused; the marker removed', code == 0 and sup_pid() not in (None, p0)
          and not hm.errors and plistlib.load(open(cli.PLIST, 'rb'))['EnvironmentVariables'].get('LMSTUDIO_LOGS', '').endswith('logs2')
          and not os.path.exists(os.path.join(STATE, 'run', 'detach.json')), (code, words, hm.errors[:3], tail() if code else ''))
        procs.append(ld.job)

        print('install: a supervisor upgrade (it hands over to its release\'s own; launchd starts that one)')
        r4 = H.build(STATE, 'r4', lambda d: open(os.path.join(d, 'localmodels', 'supervisor.py'), 'a').write('\n# a changed supervisor\n'))
        want[0] = r4
        p0 = sup_pid()
        with H.Hammer(PORT) as hm: code, words = install()
        st = deploy.supervisor_state() or {}
        t('upgrade: the new supervisor (its digest) adopted the workers, then release 4 took over; nothing refused',
          code == 0 and st.get('pid') != p0 and st.get('digest') == SUP.supervisor_digest(r4) and H.health(PORT).get('code') == os.path.basename(r4)
          and not hm.errors, (code, words, st.get('digest'), hm.errors[:3], tail() if code else ''))
        procs.append(ld.job)
        r5 = H.build(STATE, 'r5', lambda d: open(os.path.join(d, 'localmodels', 'supervisor.py'), 'a').write('\nraise RuntimeError("cannot start")\n'))
        want[0] = r5
        p0 = sup_pid()
        code, words = install()
        t("a release whose supervisor fails its check: refused, the supervisor and the workers untouched", code == 1 and 'checks' in words
          and sup_pid() == p0 and H.health(PORT).get('code') == os.path.basename(r4), (code, words))

        print('probation: a new release that dies goes back to the last good one')
        t('release 4 passed its probation: the last good one', H.wait_for(lambda: status().get('last_good') == os.path.basename(r4), TIMINGS['PROBATION'] + 10))
        r6 = build_from(r4, 'r6', lambda d: open(os.path.join(d, 'localmodels', 'marker.txt'), 'w').write('r6\n'))
        idle(); r = H.call(STATE, 'deploy', release=os.path.basename(r6))
        os.kill(r['pid'], signal.SIGKILL)                        # health can still answer from the preceding worker
        def rolled_back():
            if os.path.basename(r6) not in (status().get('bad') or []): return False
            h = H.health(PORT)
            return h.get('code') == os.path.basename(r4) and h.get('role') == 'serving' and os.path.realpath(os.path.join(STATE, 'hub')) == os.path.realpath(r4)
        t('killed on probation: release 6 marked bad, a worker from release 4 serves, SHARED/hub back on it',
          r.get('ok') and H.wait_for(rolled_back, 30), (r, status().get('bad'), tail()))

        print('a wedged worker (no progress for WEDGED s) is handed off')
        r7 = build_from(r4, 'r7', lambda d: _sub(os.path.join(d, 'localmodels', 'worker.py'), 'PROGRESS_EVERY = 5', 'PROGRESS_EVERY = 10 ** 12'))
        idle(); r = H.call(STATE, 'deploy', release=os.path.basename(r7))
        g7 = r['gen']
        moved = H.wait_for(lambda: int(H.health(PORT)['gen']) > g7, TIMINGS['WEDGED'] + 20)
        t('its gen gave way to a new one through a handoff, nothing refused', r.get('ok') and moved, (r, H.health(PORT).get('gen'), g7))
        idle(); r = H.call(STATE, 'deploy', release=os.path.basename(r4), force=True)
        t('(and back to release 4)', r.get('ok') and H.wait_for(lambda: H.health(PORT).get('code') == os.path.basename(r4), 20), r)

        print('a draining worker killed mid-drain: only its own connections are cut')
        idle()
        with H.Hammer(PORT) as hm, H.held_stream(PORT, up) as out:
            old = next(m['hub_pid'] for m in llmshare.inflight() if m.get('state') == 'answering')
            r = H.call(STATE, 'restart')
            draining = H.wait_for(lambda: any(w['pid'] == old and w['role'] == 'draining' for w in status().get('workers', [])), 10)
            def listeners_closed():
                ls = subprocess.run(['lsof', '-nP', '-a', '-p', str(old), f'-iTCP:{PORT}', '-sTCP:LISTEN', '-t'], capture_output=True, timeout=5)
                return ls.returncode == 1 and not ls.stdout and not ls.stderr
            assert draining and H.wait_for(listeners_closed, 10), 'the worker still accepts connections'
            os.kill(old, signal.SIGKILL)
            serving = H.wait_for(lambda: H.health(PORT).get('pid') == r.get('pid'), 10)
        t('the serving worker is unaffected: no hammer failure; the stream on the killed one ended early', r.get('ok') and serving and not hm.errors and hm.n > 20
          and not out.get('done'), (r, hm.errors[:3], out))
        t('its marks are gone from the shared view', H.wait_for(lambda: not [m for m in llmshare.inflight() if m.get('hub_pid') == old], 5))

        print('a shared-state protocol change: no overlap (the old one drains first), nothing refused')
        current = llmshare.STATE_PROTOCOL
        r8 = build_from(r4, 'r8', lambda d: _sub(os.path.join(d, 'localmodels', 'llmshare.py'), f'STATE_PROTOCOL = {current} ', f'STATE_PROTOCOL = {current+1} '))
        t("the release says its own shared-state protocol (read from its files, not from the process that built it)",
          json.load(open(os.path.join(r8, 'RELEASE.json'))).get('state_protocol') == current + 1)
        idle()
        seen = []
        stop = threading.Event()

        def sample():
            while not stop.is_set():
                seen.append(len([r_ for r_ in SUP.registry() if r_.get('role') == 'serving'])); time.sleep(0.05)
        sm = threading.Thread(target=sample); sm.start()
        with H.Hammer(PORT) as hm: r = H.call(STATE, 'deploy', release=os.path.basename(r8), wait=20)
        stop.set(); sm.join()
        t('release 8 serves; two workers never served at once; every connection waited in the backlog, none refused', r.get('ok')
          and H.health(PORT).get('code') == os.path.basename(r8) and max(seen or [0]) <= 1 and not hm.errors, (r, max(seen or [0]), hm.errors[:3]))

        print('stop (bootout, no detach marker): everything ends')
        t0 = time.time(); ld('bootout', 'gui/0/x')
        gone = H.wait_for(lambda: not SUP.registry(), 5)
        t('the supervisor and every worker end within 3 s', gone and time.time() - t0 < 3.5, (SUP.registry(), round(time.time() - t0, 1)))

        print('fail-open: no release can start')
        r9 = build_from(r4, 'r9', lambda d: open(os.path.join(d, 'localmodels', 'web.py'), 'a').write('\nraise RuntimeError("cannot start")\n'))
        hub = os.path.join(STATE, 'hub'); os.remove(hub); os.symlink(os.path.relpath(r9, STATE), hub)
        sj = os.path.join(STATE, 'run', 'supervisor.json'); s_ = json.load(open(sj))
        s_.update(current=os.path.basename(r9), last_good=None, probation=None); json.dump(s_, open(sj, 'w'))
        ld('bootstrap', 'gui/0', cli.PLIST); procs.append(ld.job)
        fo = H.wait_for(lambda: (deploy.supervisor_state() or {}).get('failopen'), TIMINGS['FAILOPEN_AFTER'] + 25)
        try: H.health(PORT, timeout=2); refused = False
        except (urllib.error.URLError, ConnectionError, OSError): refused = True
        t('after FAILOPEN_AFTER with no worker: listeners close, so llmshare.post clients fall back to LM Studio', fo and refused, (fo, tail()))
        os.remove(hub); os.symlink(os.path.relpath(r4, STATE), hub)
        s_ = json.load(open(sj)); s_['current'] = os.path.basename(r4); json.dump(s_, open(sj, 'w'))
        ld('bootout', 'gui/0/x'); ld('bootstrap', 'gui/0', cli.PLIST); procs.append(ld.job)
        t('a good release again: the supervisor binds and serves', H.wait_for(lambda: H.health(PORT).get('code') == os.path.basename(r4), 30), tail())
        ld('bootout', 'gui/0/x')
        want[0] = None

        print('the one-time migration from a gateway that predates releases')
        migration(ld, logf, procs, tail)
    finally:
        ld.stop.set(); cli.build_release = real_build
        if ld.loaded: ld('bootout', 'gui/0/x')
        up.shutdown()


def build_from(base, name, change):
    """A release made from another release's files (so it carries that release's supervisor), changed by change(folder)."""
    from localmodels import dash
    src = os.path.join(BOX, f'src-{name}')
    for rel in dash.deploy_files(base):
        os.makedirs(os.path.dirname(os.path.join(src, rel)), exist_ok=True); shutil.copy2(os.path.join(base, rel), os.path.join(src, rel))
    change(src)
    return cli.build_release(src, STATE)


def _sub(path, a, b):
    s = open(path).read()
    if a not in s: raise AssertionError(f'{a!r} not in {path}')
    open(path, 'w').write(s.replace(a, b, 1))


def migration(ld, logf, procs, tail):
    legacy = os.path.join(BOX, 'legacy')
    r = subprocess.run(f'git -C {ROOT} archive {LEGACY_TAG} localmodels bin LICENSE .claude-plugin | tar -x -C {legacy}', shell=True,
                       capture_output=True, text=True) if not os.makedirs(legacy) else None
    if r is None or r.returncode:
        print(f'  skip the migration: no {LEGACY_TAG} in this checkout ({(r.stderr if r else "").strip()[:120]})'); return
    for d in ('releases', 'run'): shutil.rmtree(os.path.join(STATE, d), ignore_errors=True)
    os.makedirs(os.path.join(STATE, 'releases'))
    hub = os.path.join(STATE, 'hub')
    if os.path.lexists(hub): os.remove(hub)
    shutil.copytree(legacy, hub)                               # as the old install put it: a real directory
    old = dict(Label=cli.LABEL, ProgramArguments=[sys.executable, os.path.join(hub, 'bin', 'localmodels'), 'gateway'], RunAtLoad=True, KeepAlive=True,
               EnvironmentVariables={k: os.environ[k] for k in ('LOCALMODELS_PORT', 'LOCALMODELS_STATE', 'LMSTUDIO_URL', 'LMSTUDIO_LOGS', 'LMS')})
    with open(cli.PLIST, 'wb') as f: plistlib.dump(old, f)
    ld('bootstrap', 'gui/0', cli.PLIST); procs.append(ld.job)
    h = H.wait_for(lambda: H.health(PORT), 30)
    t(f'the {LEGACY_TAG} gateway serves, with no handoff field', h and 'handoff' not in h, h or tail())
    path, words = deploy.choose(cli, cli.build_release())
    t('install chooses the migration', path == 'migrate', (path, words))

    ref = {'refused': 0}
    stop = threading.Event()

    def probe():                                               # a connect every 2 ms through the switch
        while not stop.is_set():
            c = socket.socket(); c.settimeout(1)
            try: c.connect(('127.0.0.1', PORT))
            except ConnectionRefusedError: ref['refused'] += 1
            except OSError: pass                               # not recorded: a slow connect is not a refused one
            finally: c.close()
            time.sleep(0.002)
    pr = threading.Thread(target=probe); pr.start()
    try: code, words = deploy.install_gateway(cli, wait=30)
    finally: stop.set(); pr.join()
    procs.append(ld.job)
    d = os.path.basename(cli.build_release())
    h = H.wait_for(lambda: H.health(PORT), 5) or {}
    print(f'  (install said: {code}, {words})')
    legacies = [x for x in os.listdir(os.path.join(STATE, 'releases')) if x.startswith('legacy-')]
    t('migrated: gen 1 serves the release with handoff 1 on both addresses; SHARED/hub a symlink; the old copy kept',
      code == 0 and h.get('code') == d and h.get('handoff') == 1 and (H.health(PORT, '::1') or {}).get('code') == d
      and os.path.islink(hub) and legacies, (code, words, tail() if code else ''))
    t('at most 3 connects refused through the switch (install held the port)', ref['refused'] <= 3, ref['refused'])

    print('the migration\'s revert: a bootstrap that starts nothing')
    ld('bootout', 'gui/0/x')
    os.remove(hub); os.rename(os.path.join(STATE, 'releases', legacies[0]), hub)
    for x in ('run',): shutil.rmtree(os.path.join(STATE, x), ignore_errors=True)
    with open(cli.PLIST, 'wb') as f: plistlib.dump(old, f)
    ld('bootstrap', 'gui/0', cli.PLIST); procs.append(ld.job)
    H.wait_for(lambda: H.health(PORT), 30)
    real = ld.__call__

    def failing(*args):                                       # the new LaunchAgent starts nothing; the old one is restored
        if args[0] == 'bootstrap' and plistlib.load(open(args[2], 'rb')).get('AbandonProcessGroup'):
            ld.calls.append('bootstrap'); return subprocess.CompletedProcess(args, 5, '', 'Bootstrap failed: 5: Input/output error')
        return real(*args)
    cli._launchctl = failing
    deploy_wait = deploy.READY_MAX
    try: code, words = deploy.install_gateway(cli, wait=30)
    finally: cli._launchctl, deploy.READY_MAX = ld, deploy_wait
    procs.append(ld.job)
    h = H.wait_for(lambda: H.health(PORT), 30) or {}
    t('reverted: the old gateway serves again from its own directory, with its own plist', code == 1 and 'restored' in words and 'handoff' not in h and h.get('pid')
      and not os.path.islink(hub) and os.path.isdir(hub) and not plistlib.load(open(cli.PLIST, 'rb')).get('AbandonProcessGroup'), (code, words, h))
    ld('bootout', 'gui/0/x')


if __name__ == '__main__':
    sys.exit(main())
