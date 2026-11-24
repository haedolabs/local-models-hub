#!/usr/bin/env python3
"""launchd_probe - what the supervisor relies on from this Mac's launchd, checked with a real LaunchAgent before the
gateway's one-time migration (GUIDE.md, Deploys). Manual; not one of the sandbox suites.

  python3 tools/launchd_probe.py [--yes]

It asks first (--yes: does not), then creates a temporary LaunchAgent, com.local-models.probe-<pid>, with its own state
folder, its own free port and a pretend LM Studio in this process, running the supervisor from a release of this
checkout. Nothing of the live gateway is touched: another label, another port, another state folder. It checks:

  1. with AbandonProcessGroup, the workers keep serving when the supervisor is killed (SIGKILL);
  2. KeepAlive starts the supervisor again, and the new one adopts the serving worker;
  3. bootout with a valid detach marker leaves the workers serving;
  4. bootout without one stops the supervisor and every worker within 5 s.

Then it boots the agent out, removes its plist and its state folder, and prints one line per check. Exit 0 when all hold.
"""
import http.server, json, os, plistlib, shutil, signal, socket, stat, subprocess, sys, tempfile, threading, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
LABEL = f'com.local-models.probe-{os.getpid()}'
PLIST = os.path.expanduser(f'~/Library/LaunchAgents/{LABEL}.plist')
UID = f'gui/{os.getuid()}'
F = 'probe/family'
results = []


def check(name, good, note=''):
    results.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''), flush=True)


class Fake(http.server.BaseHTTPRequestHandler):
    """A pretend LM Studio: the model listings only."""
    def log_message(self, *a): pass

    def do_GET(self):
        out = json.dumps(dict(data=[dict(id=F, state='loaded', loaded_context_length=4096)])).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out)))
        self.end_headers(); self.wfile.write(out)


def launchctl(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)


def health(port):
    req = urllib.request.Request(f'http://127.0.0.1:{port}/hub/health', headers={'Host': f'localhost:{port}'})
    try: return json.load(urllib.request.urlopen(req, timeout=2))
    except (OSError, ValueError): return None


def until(fn, secs):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v: return v
        time.sleep(0.2)
    return None


def registry(state):
    from localmodels import supervisor as SUP
    SUP.SHARED, SUP.RUN = state, os.path.join(state, 'run')
    return SUP.registry()


def sup_state(state):
    try: return json.load(open(os.path.join(state, 'run', 'supervisor.json')))
    except (OSError, ValueError): return {}


def main(argv):
    if '--yes' not in argv:
        a = input(f'Create the temporary LaunchAgent {LABEL} ({PLIST}) on its own port and state folder, then remove it? [y/N] ')
        if a.strip().lower() not in ('y', 'yes'): print('nothing done'); return 2
    box = tempfile.mkdtemp(prefix='lmp-')
    state = os.path.join(box, 's'); os.makedirs(os.path.join(state, 'releases')); os.makedirs(os.path.join(box, 'logs'))
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    up = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake); threading.Thread(target=up.serve_forever, daemon=True).start()
    lms = os.path.join(box, 'lms')
    open(lms, 'w').write('#!/bin/sh\ncase "$1" in ps) echo "[]";; *) echo ok;; esac\n'); os.chmod(lms, stat.S_IRWXU)
    os.environ.update(LOCALMODELS_STATE=state, LOCALMODELS_PORT=str(port))
    from localmodels import cli
    d = cli.build_release(ROOT, state)
    os.symlink(os.path.relpath(d, state), os.path.join(state, 'hub'))
    py = cli.PY
    plist = dict(Label=LABEL, ProgramArguments=[py, os.path.join(state, 'hub', 'bin', 'localmodels'), 'gateway'], RunAtLoad=True, KeepAlive=True,
                 ThrottleInterval=10, AbandonProcessGroup=True, StandardOutPath=os.path.join(box, 'gateway.log'), StandardErrorPath=os.path.join(box, 'gateway.log'),
                 EnvironmentVariables=dict(PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin', LOCALMODELS_STATE=state,
                                           LOCALMODELS_PORT=str(port), LMSTUDIO_URL=f'http://127.0.0.1:{up.server_address[1]}', LMS=lms,
                                           LMSTUDIO_LOGS=os.path.join(box, 'logs'), LOCALMODELS_PY=py, LOCALMODELS_NO_KEEPER='1',
                                           LOCALMODELS_TIMINGS=json.dumps(dict(SOAK=0.5))))
    with open(PLIST, 'wb') as f: plistlib.dump(plist, f)
    try:
        launchctl('bootstrap', UID, PLIST)
        h = until(lambda: (lambda x: x if x and x.get('role') == 'serving' else None)(health(port)), 60)
        check('the agent starts the supervisor, and its first worker serves', h, h and h.get('gen'))
        if not h: return 1
        worker, sup = h['pid'], sup_state(state).get('pid')

        os.kill(sup, signal.SIGKILL)
        served = [health(port) for _ in range(10) if time.sleep(0.1) is None]
        check('1. the supervisor killed (SIGKILL): the worker keeps serving', all(x and x.get('pid') == worker for x in served))
        st = until(lambda: (lambda x: x if x.get('pid') not in (None, sup) and any(w.get('adopted') and w.get('role') == 'serving' for w in x.get('workers') or []) else None)(sup_state(state)), 30)
        check('2. KeepAlive starts the supervisor again, and it adopts the worker', st, st and st.get('pid'))

        json.dump(dict(pid=os.getpid(), until=time.time() + 60), open(os.path.join(state, 'run', 'detach.json'), 'w'))
        sup = sup_state(state).get('pid')
        launchctl('bootout', f'{UID}/{LABEL}')
        time.sleep(1.5)
        x = health(port)
        check('3. bootout with a detach marker: the worker keeps serving', x and x.get('pid') == worker, x and x.get('pid'))
        os.remove(os.path.join(state, 'run', 'detach.json'))
        launchctl('bootstrap', UID, PLIST)
        st = until(lambda: (lambda x: x if x.get('pid') not in (None, sup) and any(w.get('adopted') for w in x.get('workers') or []) else None)(sup_state(state)), 30)
        check('   bootstrapped again, the new supervisor adopts it', st)

        t0 = time.time(); launchctl('bootout', f'{UID}/{LABEL}')
        gone = until(lambda: not registry(state) and not health(port), 10)
        check('4. bootout without a marker: the supervisor and every worker end within 5 s', gone and time.time() - t0 <= 5, f'{time.time() - t0:.1f} s')
    finally:
        launchctl('bootout', f'{UID}/{LABEL}')
        try: os.remove(PLIST)
        except OSError: pass
        for r in registry(state):
            try: os.kill(r['pid'], signal.SIGKILL)
            except OSError: pass
        up.shutdown()
        if os.environ.get('KEEP_LOG'): shutil.copy(os.path.join(box, 'gateway.log'), os.environ['KEEP_LOG'])
        shutil.rmtree(box, ignore_errors=True)
    print(f'{sum(results)} of {len(results)} hold; {LABEL} removed')
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
