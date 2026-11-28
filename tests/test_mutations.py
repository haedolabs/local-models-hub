#!/usr/bin/env python3
"""Model-mutation failure rules: ambiguous unloads, verified loads, parameter drift, and concurrent off/on."""
import io, json, os, shutil, sys, tempfile, threading, time, urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _sandbox  # noqa: E402
from localmodels import llmshare, maint, models  # noqa: E402

B = 'test/family'
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))


def row(ident=B, context=4096, parallel=4):
    return dict(id=ident, key=B, state='IDLE', size='1 GB', context=context, parallel=parallel)


def run(box):
    fams = {B: dict(key=B, copies=1, context=4096, parallel=4, recycle=False)}
    with patch.object(llmshare, 'settings', return_value=dict(families=fams)), \
         patch.object(llmshare, 'loaded', return_value=[row()]), patch.object(llmshare, 'read_ok', return_value=True):
        want, drop = models.plan(B, 4096, 8)
        t('a parallel-only change plans a reload of the already loaded instance', want == [B] and drop == [B], (want, drop))

    with patch.object(models, '_lms', return_value=(True, 'ok')), \
         patch.object(llmshare, 'settings', return_value=dict(families=fams)), \
         patch.object(llmshare, 'loaded', return_value=[]), patch.object(llmshare, 'read_ok', return_value=True), \
         patch.object(models.crashes, 'loaded_now') as marked:
        good, words = models.load(B, B, 4096, 4)
        t('exit zero without the exact instance in a fresh listing is failure, not recovery', not good and 'not confirmed' in words and not marked.called, words)

    with patch.object(models, '_lms', return_value=(True, 'ok')), \
         patch.object(llmshare, 'settings', return_value=dict(families=fams)), \
         patch.object(llmshare, 'loaded', return_value=[row(parallel=2)]), patch.object(llmshare, 'read_ok', return_value=True):
        good, words = models.load(B, B, 4096, 4)
        t('exit zero with the wrong slot count is failure with the drift named', not good and '2 slots instead of 4' in words, words)

    for failure in (urllib.error.HTTPError('http://localhost', 500, 'lost', {}, io.BytesIO(b'{}')),
                    urllib.error.URLError(TimeoutError('response timed out'))):
        command = Mock()
        with patch.object(llmshare, 'urlopen', side_effect=failure), patch.object(models, '_lms', command):
            good, words = models.unload(B)
            t('an ambiguous REST unload never falls through to a second unload command',
              not good and 'not retrying' in words and not command.called, words)

    with patch.object(llmshare, 'urlopen', side_effect=urllib.error.URLError(ConnectionRefusedError('refused'))), \
         patch.object(models, '_lms', return_value=(True, 'ok')) as command:
        t('a refused connection is known not to have sent the mutation, so the older CLI path remains available',
          models.unload(B)[0] and command.call_args.args[:2] == ('unload', B))

    maint.OFF = os.path.join(box, 'off.json'); maint.SWITCHING = os.path.join(box, 'switching.json')
    maint._off_cache.update(mtime=None, rec=None); maint._cache.update(mtime=None, rec=None)
    maint.switch_off([B])
    entered, release = threading.Event(), threading.Event()
    calls, active, most = [], [0], [0]
    guard = threading.Lock()

    def restore(*args):
        with guard:
            calls.append(args); active[0] += 1; most[0] = max(most[0], active[0])
        entered.set(); release.wait(5)
        with guard: active[0] -= 1
        return [f'{B} reloaded after switch-off']

    with patch.object(llmshare, 'loaded', return_value=[]), patch.object(models, '_load_back', side_effect=restore):
        first = threading.Thread(target=models.switch_on); first.start()
        assert entered.wait(5)
        second = threading.Thread(target=models.switch_on); second.start()
        time.sleep(0.1); release.set(); first.join(5); second.join(5)
    t('two concurrent switch-on calls produce one restore, never overlapping model loads',
      len(calls) == 1 and most[0] == 1 and maint.switched_off() is None, (len(calls), most[0]))

    maint.switch_off([B])
    with patch.object(llmshare, 'loaded', return_value=[]), patch.object(models, '_load_back', return_value=[f'{B}: reload after switch-off failed: test']):
        words = models.switch_on()
    failure = maint.switching(now=time.time() + maint.SHOWN_FOR + 1000)
    t('a failed restore stays off, says so, and keeps its failure visible for a later retry',
      maint.switched_off() is not None and any('remain switched off' in w for w in words)
      and failure and failure.get('ok') is False and failure.get('phase') == 'done', (words, failure))

    from localmodels import cli
    source = os.path.join(box, 'source')
    for rel, text in (('localmodels/__init__.py', "VERSION = '1.0.0'\n"), ('localmodels/llmshare.py', 'STATE_PROTOCOL = 2\n'),
                      ('localmodels/supervisor.py', 'CHANNEL = 1\n'), ('bin/localmodels', '#!/bin/sh\n'), ('LICENSE', 'test\n')):
        os.makedirs(os.path.dirname(os.path.join(source, rel)), exist_ok=True)
        open(os.path.join(source, rel), 'w').write(text)
    release = cli.build_release(source, shared=box)
    t('an unchanged immutable release is safely reused', cli.build_release(source, shared=box) == release)
    with open(os.path.join(release, 'localmodels', '__init__.py'), 'a') as f: f.write('# changed after the build\n')
    try: cli.build_release(source, shared=box); refused = False
    except RuntimeError as e: refused = 'differs' in str(e)
    t('an existing release whose content changed is rejected before any worker can run it', refused)

    real_open = open
    class Fault:
        def __init__(self, path): self.file = real_open(path, 'a+')
        def write(self, _): raise OSError('injected write failure')
        def __getattr__(self, name): return getattr(self.file, name)
    def faulty_open(path, mode='r', **kwargs):
        return Fault(path) if str(path).endswith('slot-0.lock') else real_open(path, mode, **kwargs)
    with patch('builtins.open', side_effect=faulty_open), patch.object(llmshare, 'parallel_of', return_value=1):
        t('a slot whose context cannot be published is never returned to admission', llmshare._take_slot(B, need=100) is None)

    from localmodels import deploy
    caller = Mock(PY=sys.executable, LABEL='test.label', PLIST=os.path.join(box, 'agent.plist'))
    caller.supervisor_call.return_value = dict(ok=True)
    old = dict(pid=100, digest='old-supervisor')
    restored = dict(pid=102, digest='old-supervisor', workers=[dict(role='serving')])
    with patch.object(deploy, 'checks', return_value=None), patch.object(deploy, 'supervisor_state', return_value=old), \
         patch.object(deploy, 'link_hub'), patch.object(deploy, '_detach_marker'), \
         patch.object(deploy, '_new_supervisor', side_effect=[None, restored]) as new_supervisor:
        code, words = deploy.upgrade(caller, release, lambda _: None)
    t('a failed upgrade explicitly restarts and verifies the old supervisor, not merely the hub symlink',
      code == 1 and 'restored' in words and new_supervisor.call_args_list[-1].args[:2] == (100, 'old-supervisor')
      and [c.args[0] for c in caller._launchctl.call_args_list] == ['bootout', 'bootstrap'], (code, words, caller._launchctl.call_args_list))


def main():
    box = tempfile.mkdtemp(prefix='localmodels-mutationtest-')
    try: run(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
