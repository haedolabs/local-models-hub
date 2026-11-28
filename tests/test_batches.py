#!/usr/bin/env python3
"""Durable batches in a sandbox. --acceptance runs both two-gateway interruption tests at 10,000 items each."""
import collections, http.server, json, os, signal, sqlite3, subprocess, sys, threading, time, unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

CHILD = sys.argv[1:2] in (['--runner'], ['--serve'])
LARGE = '--acceptance' in sys.argv
if LARGE: sys.argv.remove('--acceptance')
if not CHILD:
    import _sandbox
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from localmodels import batches, cli, llmshare, mcp_server

MODEL = 'test/model'
ROW = dict(id=MODEL, key=MODEL, variant=MODEL + '@8bit', bytes=1024, format='test', context=4096, state='IDLE')


def synthetic(spec, path, body, headers):
    text = body['input'] if 'input' in body else body['messages'][-1]['content']
    if os.environ.get('INTERRUPT_BATCH') and int(text) >= 40:
        open(os.path.join(os.environ['BATCH_FIXTURE'], 'interrupt'), 'a').close()
        threading.Event().wait(90)
    if text == 'bad': raise ValueError('invalid test item')
    with open(os.path.join(os.environ['BATCH_FIXTURE'], 'attempts'), 'a') as f: f.write(text + '\n')
    if path.endswith('/embeddings'):
        return dict(data=[dict(index=0, embedding=[int(text), 1])])
    return dict(choices=[dict(message=dict(content=json.dumps(dict(number=int(text)))))])


def item(i, endpoint):
    body = (dict(input=str(i)) if endpoint.endswith('embeddings') else
            dict(messages=[dict(role='user', content=str(i))], response_format=dict(type='json_object')))
    return dict(custom_id='record/' + str(i), method='POST', url=endpoint, body=dict(body, model=MODEL))


class Durable(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(dir=_sandbox.BOX)
        self.addCleanup(self.tmp.cleanup)
        self.project = self.tmp.name
        self.scope = mcp_server._project(self.project)
        self.env = dict(os.environ, BATCH_FIXTURE=self.project)

    def submit(self, items, parallel=4, folder=None):
        with patch.object(llmshare, 'loaded', return_value=[ROW]), patch.object(llmshare, 'read_ok', return_value=True):
            return mcp_server.start('batch', items, parallel=parallel, folder=folder or self.project, within=self.scope)

    def test_recovery(self):
        count = 128
        for endpoint in ('/v1/embeddings', '/v1/chat/completions'):
            with self.subTest(endpoint=endpoint):
                items = [item(i, endpoint) for i in range(count)]
                job, folder = self.submit(items)
                args = [sys.executable, __file__, '--runner', folder]
                with subprocess.Popen(args, env=dict(self.env, INTERRUPT_BATCH='1')) as child:
                    try:
                        mark = os.path.join(self.project, 'interrupt')
                        self.assertTrue(_sandbox.until(lambda: os.path.exists(mark), 30))
                        self.assertGreater(mcp_server.job_status(job, within=self.scope)['done'], 0)
                        self.assertEqual(batches.run(folder), 0, 'a second runner must leave the active one alone')
                    finally:
                        child.kill(); child.wait()
                os.remove(mark)
                saved = {r['custom_id'].split('/')[1] for r in batches.results(folder)}
                self.assertTrue(0 < len(saved) < count)
                subprocess.run(args, check=True, env=self.env, timeout=100)
                state = mcp_server.job_status(job, within=self.scope)
                self.assertTrue(state['finished'])
                self.assertEqual((state['done'], state['failed']), (count, 0))
                results = list(batches.results(folder))
                self.assertEqual([r['custom_id'] for r in results], [r['custom_id'] for r in items])
                for i, r in enumerate(results):
                    body = r['response']
                    n = body['data'][0]['embedding'][0] if 'data' in body else json.loads(body['choices'][0]['message']['content'])['number']
                    self.assertEqual(n, i)
                with open(os.path.join(self.project, 'attempts')) as f: attempts = collections.Counter(f.read().splitlines())
                self.assertTrue(all(attempts[key] == 1 for key in saved))
                os.remove(os.path.join(self.project, 'attempts'))

    def test_failed_only_and_scope(self):
        path = '/v1/embeddings'
        job, folder = self.submit([item(0, path), item('bad', path)])
        with patch.dict(os.environ, self.env), patch.object(batches, 'request', synthetic):
            self.assertEqual(batches.run(folder), 0)
        state = mcp_server.job_status(job, within=self.scope)
        self.assertEqual((state['done'], state['failed']), (2, 1))
        self.assertFalse(mcp_server.job_status(job, within=mcp_server._project(_sandbox.BOX))['ok'])
        self.assertEqual(batches.retry(folder), 1)
        self.assertEqual(mcp_server.job_status(job, within=self.scope)['done'], 1)
        with patch.object(batches, 'request', return_value=dict(data=[dict(index=0, embedding=[0, 1])])) as send:
            batches.run(folder)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(batches.retry(folder), 0)
        self.assertEqual(len(list(batches.results(folder))), 2)
        with self.assertRaises(ValueError): self.submit([item(1, path), item(1, path)])
        with self.assertRaises(ValueError): self.submit([dict(item(1, path), url='https://example.org/')])

    def test_cli_jsonl_and_mcp(self):
        from test_peers import lmstudio_home, ps_row
        home = Path(self.project, 'lmstudio')
        self.env.update(LMS=lmstudio_home(str(home), 'model: test/family\n'), PWD=self.project)
        (home / 'ps.json').write_text(json.dumps([ps_row(MODEL)]))
        command = [sys.executable, os.path.join(cli.HOME, 'bin', 'localmodels')]
        def call(*args, data=None):
            return subprocess.run(command + list(args), cwd=self.project, env=self.env, input=data, capture_output=True,
                                  text=True, check=True, timeout=30).stdout
        def mcp(name, args):
            msg = dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(name=name, arguments=args))
            reply = json.loads(call('mcp', data=json.dumps(msg) + '\n'))['result']
            self.assertFalse(reply['isError'], reply)
            return json.loads(reply['content'][0]['text'])
        items = [item(1, '/v1/embeddings'), item('bad', '/v1/embeddings')]
        filename = Path(self.project, 'requests.jsonl')
        filename.write_text(''.join(json.dumps(j) + '\n' for j in items))
        ids = [batches.JOB_ID.search(call('batch', str(filename)))[0], mcp('batch', dict(jobs=items))['job']]
        self.assertNotEqual(*ids)
        for job in ids:
            folder = batches.locate(mcp_server.AGENTS, job, self.scope)
            self.assertEqual(json.loads(call('jobs', job))['peers'], [])
            with patch.dict(os.environ, self.env), patch.object(batches, 'request', synthetic): batches.run(folder)
            exported = [json.loads(line) for line in call('jobs', job, '--results').splitlines()]
            self.assertEqual(exported, mcp('job_status', dict(job=job, results=True))['results'])
            self.assertEqual([r['ok'] for r in exported], [True, False])
            self.assertEqual(mcp('job_status', dict(job=job, retry_failed=True))['retried'], 1)
            self.assertEqual(json.loads(call('jobs', job))['done'], 1)

    def test_embedding_dimensions_and_ids(self):
        path = '/v1/embeddings'
        _, folder = self.submit([item(i, path) for i in range(4)], parallel=1)
        def send(spec, path, body, headers):
            return dict(data=[dict(index=0, embedding=[0] * (1 if body['input'] == '3' else 2))])
        with patch.object(batches, 'request', send): batches.run(folder)
        self.assertEqual((batches.status(folder)['dimensions'], batches.status(folder)['failed']), (2, 1))
        self.assertIn('dimension', list(batches.results(folder, label='record/3'))[0]['error'])
        page = batches.page(folder, include=True, limit=2)
        self.assertEqual(page['next'], 2)
        self.assertEqual(len(batches.page(folder, include=True, after=page['next'])['results']), 2)

    def test_agent_file_contract(self):
        jobs = [dict(label='digest-001', prompt='read the digest', schema={'type': 'object'}, think=False)]
        subdir = Path(self.project, 'digests')
        subdir.mkdir()
        _, folder = self.submit(jobs, folder=str(subdir))
        with patch.object(batches, 'request', return_value=dict(choices=[dict(message=dict(content='{"number": 2}'))])) as send:
            batches.run(folder)
        result = json.loads(Path(folder, 'digest-001.json').read_text())
        self.assertEqual(result['result'], {'number': 2})
        self.assertEqual(result['job']['prompt'], jobs[0]['prompt'])
        self.assertEqual(json.loads(Path(folder, 'summary.json').read_text())['ok'], 1)
        self.assertEqual(list(batches.results(folder))[0]['custom_id'], 'digest-001')
        self.assertTrue(all(c.args[-1]['X-Step'] == 'batch item' for c in send.call_args_list))

        # A retry can be interrupted after removing failed exports but before clearing the committed error.
        _, failed = self.submit(jobs)
        with patch.object(batches, 'request', side_effect=ValueError('invalid test item')): batches.run(failed)
        batches.enqueue(failed)
        for name in ('summary.json', 'digest-001.json'): Path(failed, name).unlink()
        with patch.object(batches, 'request') as send:
            batches.run(failed)
            send.assert_not_called()
        self.assertEqual(json.loads(Path(failed, 'digest-001.json').read_text())['error'], 'ValueError: invalid test item')
        self.assertEqual(json.loads(Path(failed, 'summary.json').read_text())['ok'], 0)

    def test_wait_prune_and_privacy(self):
        job, folder = self.submit([item(2, '/v1/embeddings')])
        with batches.database(folder) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM item').fetchone(), (1,))
            with self.assertRaises(sqlite3.OperationalError): db.execute('DELETE FROM item')
        refusal = HTTPError('http://localhost/', 503, 'unavailable', {'X-Hub-Maintenance': 'peers-only; switched-off', 'Retry-After': '5'},
                            BytesIO(b'private diagnostic about record/2'))
        with patch.object(llmshare, 'urlopen', side_effect=refusal):
            batches.run(folder)
        state = batches.status(folder)
        self.assertEqual((state['state'], state['done'], state['failed']), ('waiting', 0, 0))
        self.assertIn('permits only this Mac', state['reason'])
        self.assertNotIn('record/2', json.dumps(batches.overview()))
        os.utime(folder, (1, 1))
        self.assertEqual(cli.prune()[0], 0)
        self.assertEqual(batches.api('GET', '/hub/batches/' + job, b'', '', _sandbox.BOX)[0], 404)
        self.assertEqual(batches.api('GET', '/hub/batches/' + job + '/results', b'', '', self.project)[0], 409)
        with batches.database(folder, True) as db, db: db.execute('UPDATE run SET wake=0')
        with patch.object(batches, 'request', return_value=dict(data=[dict(index=0, embedding=[2])])): batches.run(folder)
        self.assertFalse(Path(folder, 'summary.json').exists())  # a raw batch needs only its committed database
        os.utime(folder, (1, 1))
        with batches.lock(folder) as held:
            self.assertTrue(held)
            self.assertEqual(cli.prune()[0], 0)
        self.assertEqual(cli.prune()[0], 1)
        self.assertFalse(Path(folder).exists())

    def test_runner_budget_and_backoff(self):
        folders = []
        for parallel in (3, 2, 1):
            _, folder = self.submit([item(len(folders), '/v1/embeddings')], parallel=parallel)
            folders.append(folder)
        processes = []

        class Child:
            def poll(self): return None

        def spawn(*args, **kwargs):
            processes.append(args[0][-1]); return Child()

        stop = threading.Event(); threading.Thread(target=lambda: (time.sleep(0.1), stop.set())).start()
        with patch.object(batches, 'queued', side_effect=lambda: iter(folders)), \
             patch.object(batches, 'MAX_RUNNERS', 2), patch.object(batches, 'MAX_RUNNER_THREADS', 4), \
             patch.object(batches.subprocess, 'Popen', side_effect=spawn):
            batches.recover(stop)
        self.assertEqual(processes, [folders[0], folders[2]])   # 3 + 1 threads; the parallel=2 job stays queued

        batches._runner_failed(folders[0], -9)
        state = batches.status(folders[0])
        self.assertEqual(state['state'], 'waiting')
        self.assertIn('exited -9', state['reason'])
        self.assertGreater(state['retry_at'], time.time())
        first = state['retry_at']; batches._runner_failed(folders[0], -9)
        self.assertGreater(batches.status(folders[0])['retry_at'], first)

        fingerprint = dict(variant=MODEL + '@8bit', bytes=1024, format='test', definition='abc')
        peer = dict(name='peer', state='up', batch_inference=1,
                    families={MODEL: dict(match=True, fingerprint=fingerprint,
                                          copies=[dict(context=65536, state='IDLE'), dict(context=16384, state='IDLE')])})
        with patch.object(llmshare, 'loaded', return_value=[]), patch.object(llmshare, 'read_ok', return_value=False), \
             patch.object(batches.peers, 'view', return_value=[peer]):
            _, _, context = batches.model_info(MODEL, ['peer'])
        self.assertEqual(context, 16384)

    def test_two_gateways(self):
        for endpoint in batches.ROUTES:
            with self.subTest(endpoint=endpoint): self.interrupt(endpoint, 10000 if LARGE else 64)

    def interrupt(self, endpoint, count):
        from test_peers import TOKEN, free_port, lmstudio_home, ps_row
        from contextlib import ExitStack
        import tempfile

        # The peer blocks with item 8 in flight. Later the local engine blocks with item 24 in flight.
        # Each engine records every invocation, including those that returned no response to a killed worker.
        seen = collections.defaultdict(collections.Counter)
        blocked = {name: threading.Event() for name in ('peer', 'front')}
        release = {name: threading.Event() for name in blocked}
        class Engine(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                text = body['input'] if 'input' in body else body['messages'][0]['content']
                n = int(text.rsplit(' ', 1)[1])
                host = self.server.name
                seen[host][n] += 1
                if n >= (8 if host == 'peer' else 24) and not release[host].is_set():
                    blocked[host].set()
                    release[host].wait(60)
                if self.path == '/v1/embeddings': out = dict(data=[dict(index=0, embedding=[n, 1])])
                else: out = dict(choices=[dict(message=dict(content=json.dumps(dict(number=n))))])
                if n == count - 1: out = {}               # one terminal, private item error
                data = json.dumps(out).encode()
                try:
                    self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
                except OSError: pass                      # this request was in flight when its gateway exited

        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=self.project)))
            port, link = free_port(), free_port()
            api = f'http://127.0.0.1:{port}/hub/batches'
            envs, processes = {}, {}
            for name in ('peer', 'front'):
                state = root / name
                state.mkdir()
                engine = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Engine)
                engine.name = name
                threading.Thread(target=engine.serve_forever, daemon=True).start()
                stack.callback(engine.server_close); stack.callback(engine.shutdown)
                lms = lmstudio_home(str(state / 'lmstudio'), 'model: test/family\n')
                (state / 'lmstudio/ps.json').write_text(json.dumps([dict(ps_row(MODEL), modelKey=MODEL, parallel=4)]))
                settings = dict(name=name, families={MODEL: dict(key=MODEL, copies=1, parallel=4, context=4096, recycle=False)})
                if name == 'peer': settings['serve'] = dict(address='127.0.0.1', port=link)
                else:
                    settings['peers'] = dict(peer=dict(url=f'http://127.0.0.1:{link}'))
                    (state / 'off.json').write_text(json.dumps(dict(at=time.time(), before=[MODEL], peers=True)))
                (state / 'models.json').write_text(json.dumps(settings))
                (state / 'peer-token').write_text(TOKEN); (state / 'peer-token').chmod(0o600)
                envs[name] = dict(self.env, PWD=self.project, LOCALMODELS_STATE=str(state), LMS=lms, LOCALMODELS_NO_KEEPER='1',
                                  LOCALMODELS_PORT=str(port if name == 'front' else free_port()), LMSTUDIO_URL=f'http://127.0.0.1:{engine.server_port}')

            def stop(p):
                if p.poll() is None: p.kill()
                p.wait(timeout=10)
            def start(name):
                with (root / (name + '.log')).open('ab') as log:
                    p = subprocess.Popen([sys.executable, __file__, '--serve'], env=envs[name], stdout=log, stderr=log)
                stack.callback(stop, p)
                processes[name] = p
            start('peer'); start('front')
            stack.callback(release['front'].set); stack.callback(release['peer'].set)
            def linked():
                try: return json.loads((root / 'front/link.json').read_text())['peers'][0]['state'] == 'up'
                except (OSError, KeyError, ValueError, IndexError): return False
            self.assertTrue(_sandbox.until(linked, 30), 'the sandbox peer must be eligible')
            items = [item(i, endpoint) for i in range(count)]
            schema = dict(type='object', properties=dict(number=dict(type='integer')), required=['number'], additionalProperties=False)
            for i, task in enumerate(items):
                if endpoint.endswith('embeddings'):
                    task['body']['input'] = f'search_document: Test passage about shipment number {i}'
                else:
                    task['body']['messages'][0]['content'] = f'Extract the shipment number from this note: shipment number {i}'
                    task['body']['response_format'] = dict(type='json_schema', json_schema=dict(name='shipment', strict=True, schema=schema))
            spec = dict(jobs=items, parallel=2, peers=['peer'])
            filename = root / 'requests.json'
            filename.write_text(json.dumps(spec))
            if endpoint.endswith('embeddings'):
                cmd = [sys.executable, '-c', 'import json,sys,urllib.request as u; '
                       'r=u.Request(sys.argv[1],data=open(sys.argv[2],"rb").read(),headers={"Content-Type":"application/json"}); '
                       'print(json.load(u.urlopen(r))["job"])', api, str(filename)]
            else: cmd = [sys.executable, str(Path(cli.HOME, 'bin/localmodels')), 'batch', str(filename)]
            reply = subprocess.run(cmd, cwd=self.project, env=envs['front'], capture_output=True, text=True, timeout=30)
            self.assertEqual(reply.returncode, 0, reply.stderr + reply.stdout)
            job = batches.JOB_ID.search(reply.stdout)[0]        # the submitter has exited
            folder = batches.locate(mcp_server.AGENTS, job, self.scope)
            def saved_counts():
                ids = (int(r['custom_id'].split('/')[1]) for r in batches.results(folder))
                return {i: seen['front'][i] + seen['peer'][i] for i in ids}
            self.assertTrue(blocked['peer'].wait(30), batches.status(folder))
            before_loss = saved_counts()
            self.assertTrue(before_loss)
            stop(processes['peer'])
            release['peer'].set()
            self.assertTrue(_sandbox.until(lambda: batches.status(folder)['state'] == 'waiting', 35), batches.status(folder))
            self.assertIn('replica', batches.status(folder)['reason'])
            (root / 'front/off.json').unlink()
            self.assertTrue(blocked['front'].wait(45), batches.status(folder))
            stop(processes['front'])
            runner = json.loads(Path(folder, 'runner.json').read_text())['pid']
            os.kill(runner, signal.SIGKILL)
            self.assertTrue(_sandbox.until(lambda: not llmshare._flock_test(str(Path(folder, 'batch.lock'))), 10))
            before_restart = saved_counts()
            self.assertTrue(len(before_loss) < len(before_restart) < count)
            release['front'].set()
            start('front')                                     # recovery discovers the run with no client present
            self.assertTrue(_sandbox.until(lambda: batches.status(folder)['finished'], 1200 if LARGE else 60), batches.status(folder))
            outcomes = list(batches.results(folder))
            self.assertEqual([r['custom_id'] for r in outcomes], [j['custom_id'] for j in items])
            for i, r in enumerate(outcomes[:-1]):
                response = r['response']
                n = response['data'][0]['embedding'][0] if 'data' in response else r['result']['number']
                self.assertEqual(n, i)
            self.assertFalse(outcomes[-1]['ok'])
            self.assertTrue(outcomes[-1]['error'])
            for saved in (before_loss, before_restart):
                self.assertEqual(saved, {i: seen['front'][i] + seen['peer'][i] for i in saved})
            for path in root.glob('*/requests.jsonl*'):
                shared = path.read_text()
                self.assertNotIn('record/', shared)
                self.assertNotIn('shipment number', shared)
            print(f'{endpoint}: {count - 1} answers, 1 item error; {len(before_loss)} saved at peer loss, '
                  f'{len(before_restart)} at restart, none recomputed', flush=True)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--serve']:
        from localmodels import gateway
        # Avoid a system-wide lsof for every synthetic inference. Project authorization still checks the actual socket.
        with patch.object(gateway.whois, 'peer_pid', return_value=0):
            sys.exit(gateway.main())
    if sys.argv[1:2] == ['--runner']:
        with patch.object(batches, 'request', synthetic):
            sys.exit(batches.run(sys.argv[2]))
    suite = unittest.main(exit=False).result
    print(f'{suite.testsRun - len(suite.errors) - len(suite.failures)} of {suite.testsRun} ok')
    sys.exit(not suite.wasSuccessful())
