"""Project-private, recoverable batches. SQLite is the record of accepted inputs and their final outcomes.

One row per custom_id. Only the process holding the run's file lock can execute it or retry errors.
A crash releases the lock. The next runner selects only rows without an outcome; an in-flight model
call can repeat, a committed result cannot. The shared queue contains only project and run IDs.
"""
import base64
import concurrent.futures as cf
import contextlib
import fcntl
import glob
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import HOME, agent, llmshare, peers

DB = 'batch.sqlite3'
JOB_ID = re.compile(r'project-[0-9a-f]{32}/[0-9]{8}-[0-9]{6}-batch(?:-[0-9]+)?')
ROUTES = {'/v1/embeddings': '/hub/infer/embedding', '/v1/chat/completions': '/hub/infer/chat'}
MAX_PARALLEL = 64


def pin(fingerprint):
    """Use the same model identity as the peer protocol, including the variant and model definition."""
    if not fingerprint or not fingerprint.get('variant') or not fingerprint.get('bytes'): return None
    return hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()


def model_info(model, allowed):
    rows = [r for r in llmshare.loaded() if r['state'] != 'LOADING']
    if not llmshare.read_ok(): rows = []
    if not model:
        choices = llmshare.chat_rows(rows)
        if not choices: raise ValueError('no chat model loaded; specify a model offered by an approved peer')
        model = llmshare.family(choices[0]['id'])
    for r in sorted(rows, key=lambda r: r['id'] != model):
        if r['id'] != model and llmshare.family(r['id']) != model: continue
        value = pin(peers.fingerprint(r))
        if value: return model, value, r.get('context') or 32768
    for p in peers.view():
        f = (p.get('families') or {}).get(model) or {}
        if p['name'] in allowed and p['state'] == 'up' and f.get('match') and p.get('batch_inference') == 1:
            value = pin(f.get('fingerprint'))
            if value: return model, value, 32768
    raise ValueError(f'cannot pin {model}: need its loaded fingerprint here or on an approved batch-capable peer')


def normalize(jobs, model=None):
    if not isinstance(jobs, list) or not jobs or not all(isinstance(j, dict) for j in jobs):
        raise ValueError('jobs must be a nonempty list of objects')
    raw = 'url' in jobs[0] or 'body' in jobs[0]
    if not raw:
        jobs = agent.unique_labels([dict(j, label=j.get('label') or f'batch-{i}') for i, j in enumerate(jobs, 1)])
    items, ids, endpoint, dimensions = [], set(), None, None
    for i, j in enumerate(jobs, 1):
        key = j.get('custom_id', None if raw else j.get('label'))
        if not isinstance(key, str) or not key.strip() or len(key) > 256 or key in ids:
            raise ValueError(f'item {i}: custom_id must be a unique nonempty string of at most 256 characters')
        ids.add(key)
        if raw:
            url, body = j.get('url'), j.get('body')
            if url not in ROUTES or j.get('method', 'POST') != 'POST' or not isinstance(body, dict) or body.get('stream'):
                raise ValueError(f'item {i}: use a non-streaming POST to /v1/embeddings or /v1/chat/completions')
            if not isinstance(body.get('model'), str) or not body['model'].strip():
                raise ValueError(f'item {i}: body.model is required')
            if (model and model != body['model']) or (endpoint and endpoint != url):
                raise ValueError('each batch must use one model and one endpoint')
            model, endpoint = body['model'], url
            if endpoint == '/v1/embeddings':
                if not body.get('input'): raise ValueError(f'item {i}: input is required')
                if i > 1 and body.get('dimensions') != dimensions: raise ValueError('embedding dimensions differ within the batch')
                dimensions = body.get('dimensions')
            elif not isinstance(body.get('messages'), list) or not body['messages']:
                raise ValueError(f'item {i}: messages are required')
        elif 'url' in j or 'body' in j or not isinstance(j.get('prompt'), str) or not j['prompt']:
            raise ValueError(f'item {i}: an agent job needs a prompt; submit API requests in a separate batch')
        items.append((key, j))
    return items, model, endpoint


def marker(d):
    return os.path.join(llmshare.SHARED, 'batch-queue', os.path.basename(os.path.dirname(d)), os.path.basename(d))


def enqueue(d):
    path = marker(d)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    Path(path).touch(mode=0o600)


def dequeue(d):
    try: os.unlink(marker(d))
    except FileNotFoundError: pass


@contextlib.contextmanager
def database(d, write=False):
    db = Path(d, DB)
    if any(Path(str(db) + suffix).is_symlink() for suffix in ('', '-wal', '-shm', '-journal')):
        raise ValueError('invalid batch database')
    # Apple's SQLite needs a writable handle to open a WAL database before its sidecars exist.
    # mode=rw still refuses a missing database, and query_only prevents readers from changing its contents.
    with contextlib.closing(sqlite3.connect(db.as_uri() + '?mode=rw', uri=True, timeout=10)) as conn:
        if write: conn.execute('PRAGMA synchronous=FULL')
        else: conn.execute('PRAGMA query_only=ON')
        yield conn


@contextlib.contextmanager
def lock(d):
    fd = os.open(os.path.join(d, 'batch.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: yield False
        else: yield True
    finally: os.close(fd)


def submit(base, scope, root, project, jobs, parallel, read_files, run, phase, model, allowed):
    allowed = [] if allowed is None else allowed
    configured = peers.configured() if allowed else {}
    if not isinstance(allowed, list) or not all(isinstance(n, str) and n in configured for n in allowed):
        raise ValueError('peers must be a list of configured peer names; [] keeps every request on this Mac')
    if not isinstance(read_files, bool): raise ValueError('read_files must be true or false')
    if any(v is not None and not isinstance(v, str) for v in (model, run, phase)):
        raise ValueError('model, run, and phase must be strings')
    items, model, endpoint = normalize(jobs, model)
    model, fingerprint, context = model_info(model, allowed)
    if parallel is None: parallel = min(MAX_PARALLEL, llmshare.at_once(model) or 1)
    if type(parallel) is not int or not 1 <= parallel <= MAX_PARALLEL:
        raise ValueError(f'parallel must be an integer between 1 and {MAX_PARALLEL}')
    namespace = os.path.join(base, scope.namespace)
    if os.path.islink(namespace): raise ValueError('invalid project job directory')
    d = agent.new_dir(namespace, 'batch')
    job = scope.namespace + '/' + os.path.basename(d)
    spec = dict(job=job, project=project, project_root=scope.main, checkout=scope.checkout, root=root, model=model, pin=fingerprint,
                context=context, endpoint=endpoint, peers=list(dict.fromkeys(allowed)), parallel=parallel,
                read_files=read_files, run=llmshare.clean(run, 80), phase=llmshare.clean(phase, 60) or 'all', started=time.time(), total=len(items))
    # Recovery requires the queue entry AND the final database. Publish the latter in one rename.
    with lock(d) as acquired:
        if not acquired: raise ValueError('batch folder is in use; submit again')
        staging = os.path.join(d, 'submitting')
        with contextlib.closing(sqlite3.connect(staging)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('CREATE TABLE run(spec TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, wake REAL NOT NULL, dimensions INTEGER);'
                             'CREATE TABLE item(seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, task TEXT NOT NULL, ok INTEGER, result TEXT);'
                             'CREATE INDEX todo ON item(ok,seq);')
            with db:
                db.execute("INSERT INTO run(spec,state,wake) VALUES (?, 'queued', 0)", (json.dumps(spec),))
                db.executemany('INSERT INTO item(id,task) VALUES (?,?)', ((k, json.dumps(j)) for k, j in items))
        if not endpoint:                                     # file contract for existing agent-batch clients
            llmshare._write(os.path.join(d, 'jobs.json'), dict(spec, jobs=[j for _, j in items], labels_assigned=True, kind='batch'))
            llmshare._write(os.path.join(d, 'plan.json'), dict(spec, jobs=[j['label'] for _, j in items]))
        enqueue(d)
        os.replace(staging, os.path.join(d, DB))
    return job, d


def locate(base, job, scope=None):
    if not isinstance(job, str) or not JOB_ID.fullmatch(job): return None
    namespace = job.split('/')[0]
    if scope and namespace != scope.namespace: return None
    d = os.path.join(os.path.realpath(base), job)
    if os.path.realpath(d) != d or not os.path.isfile(os.path.join(d, DB)): return None
    return d


def _status(d, db):
    data, state, reason, wake, dimensions = db.execute('SELECT spec,state,reason,wake,dimensions FROM run').fetchone()
    counts = dict(db.execute('SELECT ok,count(*) FROM item GROUP BY ok'))
    spec = json.loads(data)
    failed = counts.get(0, 0)
    if state == 'running' and not llmshare._flock_test(os.path.join(d, 'batch.lock')):
        state, reason = 'queued', 'waiting for the batch runner to restart'
    elif state == 'queued': reason = 'waiting for the gateway to start the batch runner'
    return dict(ok=True, job=spec['job'], project=spec['project'], of=spec['total'], done=counts.get(1, 0) + failed,
                failed=failed, finished=state == 'completed', state=state, reason=reason, retry_at=wake or None,
                model=spec['model'], pin=spec['pin'], peers=spec['peers'], dimensions=dimensions)


def status(d):
    with database(d) as db:
        db.execute('BEGIN')
        return _status(d, db)


def _rows(db, after=0, limit=-1, label=None):
    """Yield complete private result rows in submission order; seq is a stable pagination cursor."""
    query = 'SELECT seq,id,result FROM item WHERE ok IS NOT NULL AND seq>?'
    args = [after]
    if label is not None: query += ' AND id=?'; args.append(label)
    for seq, key, value in db.execute(query + ' ORDER BY seq LIMIT ?', args + [limit]):
        yield dict(json.loads(value), seq=seq, custom_id=key)


def results(d, after=0, limit=-1, label=None, finished_only=False):
    with database(d) as db:
        db.execute('BEGIN')
        if finished_only and db.execute('SELECT state FROM run').fetchone()[0] != 'completed':
            raise ValueError('batch is not finished')
        yield from _rows(db, after, limit, label)


class Unfinished(ValueError):
    """Full result pages need a finished run: a later commit could otherwise fall behind the cursor."""


def page(d, label=None, include=False, after=0, limit=100, cap=200000):
    if type(after) is not int or type(limit) is not int or not (0 <= after and 1 <= limit <= 1000):
        raise ValueError('after must be nonnegative and limit between 1 and 1000')
    with database(d) as db:
        db.execute('BEGIN')
        out = _status(d, db)
        if label is None and not out['finished']:
            if include: raise Unfinished('batch not finished; request one custom_id or read status')
            return out
        rows, size = [], 0
        for r in _rows(db, min(after, out['of']), limit + 1, label):
            if not include and label is None:
                r = {k: r[k] for k in ('seq', 'custom_id', 'label', 'ok', 'seconds', 'error') if k in r}
            n = len(json.dumps(r))
            if len(rows) >= limit or (rows and size + n > cap):
                out['next'] = rows[-1]['seq']; break
            rows.append(r); size += n
    out['results' if include or label is not None else 'jobs'] = rows
    return out


def decode(data, jsonl=False):
    """The CLI and HTTP accept the same JSON envelope, array, or JSONL request file."""
    if jsonl:
        return dict(jobs=[json.loads(line) for line in data.splitlines() if line.strip()])
    spec = json.loads(data)
    if isinstance(spec, list): return dict(jobs=spec)
    if isinstance(spec, dict) and isinstance(spec.get('jobs'), list): return spec
    raise ValueError('expected a list of jobs, or an object containing jobs')


def api(method, path, data, content_type, cwd):
    from . import mcp_server
    scope = mcp_server._project(cwd)
    url = urllib.parse.urlsplit(path)
    route = url.path.rstrip('/')
    try:
        if route == '/hub/batches':
            if method != 'POST': return 405, dict(error='use POST to submit a batch')
            options = decode(data, content_type in ('application/x-ndjson', 'application/jsonl'))
            folder = os.path.join(cwd, options.get('folder') or '.')
            job, _ = mcp_server.start('batch', options['jobs'], options.get('parallel'), folder, within=scope,
                                      read_files=options.get('read_files', True), model=options.get('model'),
                                      peers=options.get('peers'), run=options.get('run'), phase=options.get('phase'))
            return 202, dict(ok=True, job=job)
        job = route[len('/hub/batches/'):]
        parts = job.split('/')
        action = parts.pop() if len(parts) == 3 else ''
        d = locate(mcp_server.AGENTS, '/'.join(parts), scope)
        if not d: return 404, dict(error='no such batch in this project')
        if method == 'GET' and not action: return 200, status(d)
        if method == 'GET' and action == 'results':
            q = urllib.parse.parse_qs(url.query)
            label = q.get('custom_id', [None])[0]
            return 200, page(d, label, True, int(q.get('after', [0])[0]), int(q.get('limit', [100])[0]))
        if method == 'POST' and action == 'retry':
            try: return 200, dict(ok=True, retried=retry(d))
            except ValueError as e: return 409, dict(error=str(e))
        return 405, dict(error='use GET for status or results, POST for retry')
    except Unfinished as e: return 409, dict(error=str(e))
    except (TypeError, ValueError, OverflowError) as e: return 400, dict(error=str(e))


def retry(d):
    with lock(d) as acquired:
        if not acquired: raise ValueError('batch is active; retry failed items once it finishes')
        with database(d, True) as db, db:
            if db.execute('SELECT state FROM run').fetchone()[0] != 'completed':
                raise ValueError('batch is not finished')
            n = db.execute('SELECT count(*) FROM item WHERE ok=0').fetchone()[0]
            if n:
                enqueue(d)                                   # the lock covers the marker and the commit
                Path(d, 'summary.json').unlink(missing_ok=True)
                for (task,) in db.execute('SELECT task FROM item WHERE ok=0'):
                    item = json.loads(task)
                    if 'prompt' in item and 'url' not in item: Path(d, item['label'] + '.json').unlink(missing_ok=True)
                db.execute('UPDATE item SET ok=NULL,result=NULL WHERE ok=0')
                db.execute("UPDATE run SET state='queued',reason=NULL,wake=0")
            return n


def request(spec, path, body, headers):
    """Only the pinned inference routes accept these requests; an older gateway cannot silently reroute them."""
    headers = dict(headers, **{'Content-Type': 'application/json', 'X-Project': spec['project'], 'X-Kind': 'batch',
                               'X-Batch-Pin': spec['pin'], 'X-Batch-Peers': ','.join(spec['peers'])})
    headers = {k: str(v).encode('latin1', 'replace').decode('latin1') for k, v in headers.items()}
    req = urllib.request.Request(llmshare.HUB + ROUTES[path], data=json.dumps(body).encode(), headers=headers)
    try:
        with llmshare.urlopen(req, timeout=3600) as reply: return json.load(reply)
    except urllib.error.HTTPError as e:
        again, wait, _ = agent.transient(e)
        if again or e.code in (404, 409):
            # Only fixed messages reach shared state. An upstream diagnostic may quote the request.
            reason = 'waiting for a permitted replica of the pinned model'
            maintenance = e.headers.get('X-Hub-Maintenance', '') if e.headers else ''
            if maintenance.startswith('peers-only') and not spec['peers']: reason = 'gateway is in peers-only mode; this batch permits only this Mac'
            elif 'switched-off' in maintenance and 'peers-only' not in maintenance: reason = 'local models are switched off'
            elif maintenance.startswith('on'): reason = 'gateway maintenance is active'
            elif e.code == 404: reason = 'waiting for a gateway that supports pinned inference'
            elif e.code == 429 or 'no free LM Studio slot' in agent.err_text(e): reason = 'waiting for model capacity'
            elif e.code == 409 or 'model swap in progress' in agent.err_text(e): reason = 'waiting for the model swap to finish'
            elif e.code in (502, 504): reason = 'waiting for the inference gateway to respond'
            raise agent.Deferred(f'HTTP {e.code}: {reason}', wait or 5) from e
        raise
    except (OSError, http.client.HTTPException) as e:
        raise agent.Deferred('waiting for the gateway connection', 5) from e


def embedding_size(vector):
    if isinstance(vector, list) and vector and all(type(x) in (float, int) and math.isfinite(x) for x in vector):
        return len(vector)
    if isinstance(vector, str) and vector:
        size = len(base64.b64decode(vector, validate=True))
        if size and size % 4 == 0: return size // 4           # OpenAI base64 embeddings contain float32 values
    raise ValueError('invalid embedding vector')


def api_item(spec, body, headers):
    path = spec['endpoint']
    try: reply = request(spec, path, body, headers)
    except urllib.error.HTTPError as e: raise ValueError(f'HTTP {e.code}: {agent.err_text(e)}') from e
    if not isinstance(reply, dict) or reply.get('error'): raise ValueError('model returned an error or an invalid response')
    out = dict(ok=True, response=reply)
    if path == '/v1/embeddings':
        data, inputs = reply.get('data'), body['input']
        count = len(inputs) if isinstance(inputs, list) and not isinstance(inputs[0], int) else 1
        if not isinstance(data, list) or len(data) != count or {v['index'] for v in data} != set(range(count)):
            raise ValueError('incomplete embedding response')
        sizes = {embedding_size(v['embedding']) for v in data}
        if len(sizes) != 1 or body.get('dimensions') is not None and body['dimensions'] not in sizes:
            raise ValueError('embedding dimensions differ from the request or between vectors')
        out['dimensions'] = sizes.pop()
    else:
        msg = reply['choices'][0]['message']
        fmt = body.get('response_format') or {}
        if fmt.get('type') in ('json_object', 'json_schema'):
            schema = {'type': 'object'} if fmt['type'] == 'json_object' else fmt['json_schema']['schema']
            kind = schema.get('type') if isinstance(schema, dict) else None
            missing = object()
            obj = agent.json_from(msg.get('content'), kind, missing)
            if obj is missing: obj = agent.json_from(msg.get('reasoning_content'), kind, missing)
            if obj is missing or not agent.valid(obj, schema): raise ValueError('answer did not match the requested JSON format')
            out['result'] = obj
    return out


def perform(spec, task, progress):
    headers = agent.phase_headers(spec['run'] or spec['job'].split('/')[-1], spec['phase'], spec['total'], progress)
    if spec['endpoint']: return api_item(spec, task['body'], headers)
    files = agent.Files([spec['root']], where=f"the {spec['project']} project's folder")
    if spec['read_files'] and files.base != spec['root']: raise agent.Deferred('waiting for the original project folder', 30)
    def send(path, body, project, kind, headers=None):
        return request(spec, path, body, dict(headers or {}, **{'X-Step': 'batch item'}))
    def once():
        return agent.run_job(task, spec['model'], lambda j: files.defs() if spec['read_files'] else [],
                             lambda n, a, j: files.call(n, a), spec['project'], headers=headers, transport=send,
                             context=spec['context'], persist=False)
    r = once()
    if not r['ok'] and not r.get('error'): r = once(); r['retried'] = True
    if not r['ok'] and not r.get('error'): r['error'] = 'no valid answer'
    return r


def execute_items(db, d, spec):
    done = db.execute('SELECT count(*) FROM item WHERE ok IS NOT NULL').fetchone()[0]
    dimensions = db.execute('SELECT dimensions FROM run').fetchone()[0]
    pending, last, delay = {}, 0, None
    with cf.ThreadPoolExecutor(max_workers=spec['parallel']) as pool:
        while True:
            if not delay:
                for seq, data in db.execute('SELECT seq,task FROM item WHERE ok IS NULL AND seq>? ORDER BY seq LIMIT ?',
                                            (last, spec['parallel'] - len(pending))).fetchall():
                    task = json.loads(data)
                    pending[pool.submit(perform, spec, task, done)] = (seq, task)
                    last = seq
            if not pending: return delay
            ready, _ = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            exports = []
            with db:
                for fut in ready:
                    seq, task = pending.pop(fut)
                    try: r = fut.result()
                    except agent.Deferred as e:
                        if delay is None or e.seconds > delay.seconds: delay = e
                        continue
                    except Exception as e:                   # not recorded: the private item outcome carries the exception
                        r = dict(ok=False, error=str(e) or type(e).__name__)
                    if 'dimensions' in r:
                        if dimensions is None:
                            dimensions = r['dimensions']
                            db.execute('UPDATE run SET dimensions=?', (dimensions,))
                        elif r['dimensions'] != dimensions:
                            r = dict(ok=False, error=f"expected {dimensions} embedding dimensions, received {r['dimensions']}")
                    if not spec['endpoint']:
                        r.update(label=task['label'], log=os.path.join(d, task['label'] + '.json'))
                        exports.append((r['log'], dict(r, job=task)))
                    db.execute('UPDATE item SET ok=?, result=? WHERE seq=? AND ok IS NULL', (int(r['ok']), json.dumps(r), seq))
                    done += 1
            for filename, row in exports: llmshare._write(filename, row)


def agent_files(db, d, spec):
    """Existing agent clients consume label.json and summary.json. Rebuild these from committed outcomes after a crash."""
    rows, tokens = [], dict(prompt=0, completion=0, reasoning=0, retries=0, empty=0)
    for data, value in db.execute('SELECT task,result FROM item ORDER BY seq'):
        j, r = json.loads(data), json.loads(value)
        r['label'] = j['label']
        filename = os.path.join(d, j['label'] + '.json')
        if not os.path.exists(filename): llmshare._write(filename, dict(r, job=j))
        rows.append({k: r[k] for k in ('label', 'ok', 'seconds', 'turns', 'error') if k in r})
        for k in tokens: tokens[k] += (r.get('usage') or {}).get(k, 0)
    llmshare._write(os.path.join(d, 'summary.json'), dict(model=spec['model'], jobs=len(rows), done=len(rows), tokens=tokens,
                    ok=sum(r['ok'] for r in rows), results=rows, parallel=spec['parallel'], seconds=time.time()-spec['started']))


def run(d):
    with lock(d) as acquired:
        if not acquired or not os.path.isfile(marker(d)): return 0
        with database(d, True) as db:
            data, state, wake = db.execute('SELECT spec,state,wake FROM run').fetchone()
            spec = json.loads(data)
            if state == 'completed':
                # A retry may have stopped after removing exports but before committing its transaction.
                if not spec['endpoint'] and not Path(d, 'summary.json').exists(): agent_files(db, d, spec)
                dequeue(d)
                return 0
            if wake > time.time(): return 0
            llmshare._write(os.path.join(d, 'runner.json'), dict(pid=os.getpid(), started=time.time()))
            if state == 'queued' and not spec['endpoint'] and spec['total'] > 1:
                llmshare.hub_post('/hub/recycle', dict(model=spec['model']))
                if spec['run']: agent.plan_run(spec['run'], [dict(name=spec['phase'], items=spec['total'])], spec['project'])
            with db: db.execute("UPDATE run SET state='running',reason=NULL,wake=0")
            delay = None
            if not spec['endpoint'] and spec['read_files']:
                from . import mcp_server
                scope = mcp_server._project(spec['checkout'])
                root, _ = mcp_server._root(spec['root'], within=scope)
                if root != spec['root'] or scope.main != spec['project_root']:
                    delay = agent.Deferred('waiting for the original project folder', 30)
            if not delay:
                with llmshare.lease(spec['project'], [spec['model']], 'batch'):
                    delay = execute_items(db, d, spec)
            if delay:
                with db: db.execute("UPDATE run SET state='waiting',reason=?,wake=?", (str(delay), time.time() + delay.seconds))
                return 0
            if not spec['endpoint']: agent_files(db, d, spec)
            with db: db.execute("UPDATE run SET state='completed',reason=NULL,wake=0")
            dequeue(d)
    return 0


def queued():
    from . import mcp_server
    root = os.path.join(llmshare.SHARED, 'batch-queue')
    for path in glob.iglob(os.path.join(root, '*', '*')):
        d = locate(mcp_server.AGENTS, os.path.relpath(path, root))
        if d: yield d


def overview():
    """Only counts and state go to the shared dashboard; a project retrieves its own results."""
    from . import names
    if names.presenting(): return []
    out = []
    for d in queued():
        try: s = status(d)
        except (OSError, sqlite3.Error, ValueError): continue
        out.append(dict(project=names.project(s['project']), done=s['done'], total=s['of'], failed=s['failed'],
                        state=s['state'], reason=s['reason']))
    return out


def recover(stop=None):
    """The serving gateway starts detached runners. File locks survive a handoff and disappear on process exit."""
    from . import errors
    stop = stop or threading.Event()
    children = {}
    while not stop.is_set():
        children = {d: child for d, child in children.items() if child.poll() is None}
        for d in queued():
            try:
                if d in children or llmshare._flock_test(os.path.join(d, 'batch.lock')): continue
                with database(d) as db: wake = db.execute('SELECT wake FROM run').fetchone()[0]
                if wake > time.time(): continue
                with open(os.path.join(d, 'run.log'), 'ab') as log:
                    children[d] = subprocess.Popen([sys.executable, os.path.join(HOME, 'bin', 'localmodels'), 'agent-run', d],
                                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            except (OSError, sqlite3.Error, ValueError) as e: errors.record('batch recovery', e)
        stop.wait(2)
