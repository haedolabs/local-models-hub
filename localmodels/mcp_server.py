"""mcp_server - the hub's tools for every Claude Code session on this Mac (registered once, at user scope, as
"local-models", or shipped by the hub's Claude Code plugin). Its instructions are the signpost: every session that starts
reads them, so the rules live here and in GUIDE.md, nowhere else. Stdlib only: MCP over stdio is JSON-RPC 2.0, one message
per line; tool calls run in threads, so a long `ask` never blocks a session's other calls.

  localmodels mcp              # started by Claude Code, which talks to it over stdin/stdout

Tools: status, guide, ask, batch, job_status, swap_check. ask and batch run local agents on the loaded model with
tools that only read the session's own project folder; they run as their own process (a session that ends does not
stop them) and go through the hub like everything else.
"""
import concurrent.futures as cf, json, os, re, shutil, subprocess, sys, threading, time, traceback

from . import HOME, VERSION, agent, dash, llmshare, whois

AGENTS = os.path.expanduser('~/Library/Logs/LocalModels/agents')
GUIDE = os.path.join(HOME, 'GUIDE.md')
INSTRUCTIONS = (
    "Local models on this Mac (LM Studio) are shared by every project and Claude Code session through the local-models hub "
    "(the full rules: the guide tool). To use a local model, send OpenAI-style requests to http://localhost:1235/v1 (the hub), "
    "never straight to LM Studio's port 1234: the hub takes turns for everyone, names your project and session itself, and "
    "logs who used what (never the content). Optional headers: X-Project (a name), X-Kind (batch, chat, ask, eval, test) and "
    "X-Step (what the request is for). The status tool says which models are loaded and how many answers at once they give: "
    "a batch should send that many (the hub gives each a turn; the rest wait). For bulk reading jobs, use ask and batch here "
    "(local agents that only read your project's folder) instead of Claude multi-agent workflows. "
    "Their answers are leads to check, not verdicts. Never stop LM Studio's server; before unloading or reloading a model, call "
    "swap_check, and if it names anyone, do not swap. Report briefly, in plain words.")
_o = lambda props, req=(): {'type': 'object', 'properties': props, 'required': list(req)}
TOOLS = [
    {'name': 'status', 'description': 'What is using the local models now: LM Studio and its loaded models (each copy\'s work since it was loaded against its '
     'recycle band), the hub, every program using a model by project and Claude session (batches with their progress), crashes and recycles, and the last hour by project.',
     'inputSchema': _o({})},
    {'name': 'guide', 'description': "The hub's rules for any project that uses the local models (where to send requests, "
     "leases, swaps, local agents, logs). section=... reads one part; no section: the Now card and the section list.",
     'inputSchema': _o({'section': {'type': 'string'}})},
    {'name': 'ask', 'description': "One local agent on the loaded model: it answers one well-defined question, reading "
     "files in this project's folder with tools that only read (read_files=false: no files, just the model). schema=... "
     "asks for the answer as JSON matching a JSON Schema. think=false turns the model's thinking off: several times faster "
     "and as right for lookups and extraction; leave it on (the default) for checks and judgment. Waits up to "
     "wait_seconds (default 120), else returns a job id for job_status. Treat the answer as a lead to check.",
     'inputSchema': _o({'prompt': {'type': 'string'}, 'read_files': {'type': 'boolean'}, 'schema': {'type': 'object'}, 'think': {'type': 'boolean'},
                        'max_turns': {'type': 'integer', 'minimum': 1, 'maximum': 40}, 'max_tokens': {'type': 'integer', 'minimum': 256, 'maximum': 32000},
                        'folder': {'type': 'string'}, 'wait_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 600}}, ['prompt'])},
    {'name': 'batch', 'description': "Many local-agent jobs side by side (bulk reading: checking many files, summarising "
     "many documents), each with tools that only read this project's folder. jobs: [{label, prompt, schema?, think?}] "
     "(think=false: no thinking, for lookups and extraction). parallel: how many at once (default: what the loaded copies "
     "answer at once). Returns a job id at once; follow it with job_status. A job whose request fails because the model is "
     "down waits and tries again for up to an hour. The answers are leads to check.",
     'inputSchema': _o({'jobs': {'type': 'array', 'items': _o({'label': {'type': 'string'}, 'prompt': {'type': 'string'},
                        'schema': {'type': 'object'}, 'max_turns': {'type': 'integer'}, 'max_tokens': {'type': 'integer'}, 'think': {'type': 'boolean'}}, ['prompt'])},
                        'parallel': {'type': 'integer', 'minimum': 1, 'maximum': 16}, 'folder': {'type': 'string'}}, ['jobs'])},
    {'name': 'job_status', 'description': 'Progress of an ask or batch job: done, running, failed, and when finished a line per job. '
     'results=true adds every answer (capped); label=... one job\'s full answer.',
     'inputSchema': _o({'job': {'type': 'string'}, 'label': {'type': 'string'}, 'results': {'type': 'boolean'}}, ['job'])},
    {'name': 'swap_check', 'description': 'Whether these models may be unloaded or reloaded now (a swap, a context change): '
     'empty blockers means yes; otherwise who is using them. Never swap when it names anyone.',
     'inputSchema': _o({'models': {'type': 'array', 'items': {'type': 'string'}}})},
]


# ---------------------------------------------------------------- the tools
def status():
    from . import recycle
    s = dict(time=time.strftime('%H:%M:%S'), door=dash.door(), lmstudio=dash.lmstudio(), users=dash.users(),
             claude=dash.claude(), last_hour=dash.last_hour())
    lm = s['lmstudio']
    out = dict(ok=True, hub=('open' if s['door']['up'] else 'closed') + f" on {llmshare.HUB}", dashboard=f'{llmshare.HUB}/',
               lmstudio=f"server {lm['server']}; " + ('; '.join(dash.family_words(f) for f in lm['families']) or 'no model loaded'),
               at_once=llmshare.at_once() if lm['families'] else 0, using_now=[],
               claude_sessions=dict(working=sum(c['working'] for c in s['claude']), idle=sum(not c['working'] for c in s['claude'])),
               last_hour={p: f"{v['requests']} requests, {v['tokens']:,} tokens" for p, v in s['last_hour'].items()})
    try:
        v = recycle.view(lm['models'])
        out['copies'] = {i: (f"{c['tokens']:,} tokens processed since {time.strftime('%H:%M', time.localtime(c['since']))}"
                             + (f" ({int(100 * c['share'])}% of its recycle band)" if c.get('share') is not None else '')) for i, c in v['copies'].items()}
        if v.get('job'): out['recycling'] = recycle.words(v['job'])
    except Exception as e: out['copies'] = f'could not read: {e}'
    if llmshare.settings_error(): out['settings_error'] = llmshare.settings_error() + ' (the last good settings stand)'
    cr = dash.crash_view()
    if cr['down'] or cr['last']:
        out['crashes'] = [f"down after a crash now: {d['words']}" for d in cr['down']] + ([f"last crash: {cr['last']}"] if cr['last'] else [])
    for r in s['users']:
        b = r.get('batch') or {}
        out['using_now'].append(dict(project=r['project'], kind=dash.kind_words(r), models=r['models'],
                                     state=f"{r['answering']} answering, {r['waiting']} waiting" + (f", {r['direct']} straight to LM Studio (not through the hub)" if r['direct'] else ''),
                                     since=time.strftime('%H:%M', time.localtime(r['since'])), claude_session=r.get('title') or (bool(r.get('session')) or None),
                                     step=r.get('step'), progress=f"{b['done']} of {b['total']}" if b.get('total') else None))
    return out


def guide(section=None):
    try: text = open(GUIDE).read()
    except OSError: return dict(ok=False, error=f'{GUIDE} is missing')
    parts = re.split(r'\n(?=## )', text)
    if not section:
        now = next((p for p in parts if p.startswith('## Now')), parts[0])
        return dict(ok=True, now=now.strip(), sections=[p.split('\n', 1)[0][3:] for p in parts if p.startswith('## ')])
    hit = [p for p in parts if p.startswith('## ') and section.lower() in p.split('\n', 1)[0].lower()]
    return dict(ok=True, text='\n\n'.join(hit).strip()[:12000]) if hit else dict(ok=False, error=f'no section like {section!r}')


def _root(folder=None):
    root = os.path.realpath(os.path.expanduser(folder or os.environ.get('PWD') or os.getcwd()))
    home = os.path.realpath(os.path.expanduser('~'))
    if not os.path.isdir(root): return None, f'no folder {folder!r}'
    lib = os.path.join(home, 'Library')
    inside = lambda p, d: p.lower() == d.lower() or p.lower().startswith(d.lower() + os.sep)
    if root == home or not inside(root, home) or inside(root, lib):
        return None, f'{root} is not a project folder (it must be inside your home folder, not ~/Library)'
    return root, None


def start(kind, jobs, parallel=1, folder=None, read_files=True):
    """Write a job folder and start its runner as its own process. -> (job id, folder) or raises ValueError."""
    root, err = _root(folder)
    if err: raise ValueError(err)
    m, err = agent.loaded_model()
    if err: raise ValueError(err)
    project = whois.project_of(root) or os.path.basename(root)
    d = agent.new_dir(os.path.join(AGENTS, llmshare._safe(project)), kind)
    jobs = agent.unique_labels([dict(j, label=j.get('label') or f'{kind}-{i + 1}') for i, j in enumerate(jobs)])
    json.dump(dict(project=project, root=root, model=m['id'], parallel=parallel, kind=kind, read_files=read_files, jobs=jobs),
              open(os.path.join(d, 'jobs.json'), 'w'), indent=1)
    py = shutil.which('python3') or sys.executable
    p = subprocess.Popen([py, os.path.join(HOME, 'bin', 'localmodels'), 'agent-run', d], cwd=root, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=open(os.path.join(d, 'run.log'), 'w'), stderr=subprocess.STDOUT)
    json.dump(dict(pid=p.pid, started=time.time()), open(os.path.join(d, 'runner.json'), 'w'))
    return f'{llmshare._safe(project)}/{os.path.basename(d)}', d


def job_status(job, label=None, results=False, cap=200_000):
    d = os.path.realpath(os.path.join(AGENTS, job or ''))
    if not d.startswith(os.path.realpath(AGENTS) + os.sep) or not os.path.isdir(d): return dict(ok=False, error=f'no job {job!r}')
    b = dash.batch(d) or {}
    try: spec = json.load(open(os.path.join(d, 'jobs.json')))
    except (OSError, ValueError): spec = {}
    out = dict(ok=True, job=job, done=b.get('done', 0), of=len(spec.get('jobs', [])), running=[(x['job'], x.get('step')) for x in b.get('now', [])])
    finished = os.path.exists(os.path.join(d, 'summary.json'))
    out['finished'] = finished
    try: tail = open(os.path.join(d, 'run.log')).read()[-600:]
    except OSError: tail = ''
    if not finished:
        try: runner = json.load(open(os.path.join(d, 'runner.json')))
        except (OSError, ValueError): runner = {}
        if runner.get('pid') and not llmshare._alive(runner['pid']):
            out['problem'] = 'the runner is gone without finishing' + (f': {tail}' if tail else '')
        elif tail and 'Traceback' in tail: out['problem'] = tail
    rows, size = [], 0
    for j in spec.get('jobs', []):
        if label and j['label'] != label: continue
        try: r = json.load(open(os.path.join(d, j['label'] + '.json')))
        except (OSError, ValueError):
            if finished or label: rows.append(dict(label=j['label'], ok=False, error='no result'))
            continue
        u = r.get('usage') or {}
        line = dict(label=r.get('label'), ok=r.get('ok'), seconds=r.get('seconds'), error=r.get('error'),
                    answer_chars=len(r.get('answer') or ''), result_chars=len(json.dumps(r.get('result'))) if r.get('result') is not None else 0,
                    **({'empty_answers': u['empty']} if u.get('empty') else {}), **({'retries': u['retries']} if u.get('retries') else {}))
        if results or label:
            ans, res = (r.get('answer') or '')[:6000], r.get('result')
            if size + len(ans) + (len(json.dumps(res)) if res is not None else 0) > cap and not label: line['note'] = 'left out: the reply is full; ask for this label alone'
            else:
                line.update(answer=ans or None, result=res); size += len(ans) + (len(json.dumps(res)) if res is not None else 0)
        rows.append({k: v for k, v in line.items() if v not in (None, '', 0) or k in ('ok',)})
    if rows: out['results' if (results or label) else 'jobs'] = rows
    return out


def call(name, a):
    a = a or {}
    if name == 'status': return status()
    if name == 'guide': return guide(a.get('section'))
    if name == 'swap_check':
        models = a.get('models') or [r['id'] for r in llmshare.loaded(fresh=True)]
        why = llmshare.swap_blockers(models)
        return dict(ok=True, models=models, blockers=why, safe=not why)
    if name == 'job_status': return job_status(a.get('job', ''), a.get('label'), bool(a.get('results')))
    if name in ('ask', 'batch'):
        if name == 'ask':
            jobs = [dict(label='ask', prompt=a.get('prompt', ''), schema=a.get('schema'), max_turns=a.get('max_turns') or 16,
                         **({'max_tokens': int(a['max_tokens'])} if a.get('max_tokens') else {}), **({'think': False} if a.get('think') is False else {}))]
        else:
            jobs = [j for j in a.get('jobs') or [] if isinstance(j, dict) and j.get('prompt')]
            if not jobs: return dict(ok=False, error='no jobs with a prompt')
        at_once = llmshare.at_once() or llmshare.settings().get('batch_at_once', 8)
        try: job, d = start(name, jobs, int(a.get('parallel') or (1 if name == 'ask' else at_once)), a.get('folder'), a.get('read_files', True))
        except (ValueError, TypeError) as e: return dict(ok=False, error=str(e))
        if name == 'batch': return dict(ok=True, job=job, jobs=len(jobs), note='follow it with job_status')
        t0, wait = time.time(), int(a.get('wait_seconds') if a.get('wait_seconds') is not None else 120)
        while time.time() - t0 < wait:
            if os.path.exists(os.path.join(d, 'summary.json')): break
            time.sleep(1)
        st = job_status(job, label='ask')
        if st.get('finished'):
            r = (st.get('results') or [{}])[0]
            return dict(ok=bool(r.get('ok')), job=job, answer=r.get('answer'), result=r.get('result'), error=r.get('error'), seconds=r.get('seconds'))
        return dict(ok=True, job=job, running=True, note=f'still working after {wait} s: follow it with job_status', problem=st.get('problem'))
    raise KeyError(f'unknown tool {name}')


# ---------------------------------------------------------------- the job runner (its own process)
def run_folder(d):
    """Run the jobs a folder describes (jobs.json), as one batch (or one ask) through the hub."""
    spec = json.load(open(os.path.join(d, 'jobs.json')))
    files = agent.Files([spec['root']], where=f"the {spec['project']} project's folder")
    defs = (lambda job: files.defs()) if spec.get('read_files', True) else (lambda job: [])
    kind = 'ask' if spec.get('kind') == 'ask' else 'batch'
    summary, _, _ = agent.batch(spec['jobs'], spec['model'], defs, lambda n, a, job: files.call(n, a), spec['project'],
                                spec.get('parallel', 1), out_dir=d, quiet=True, by='claude' if os.environ.get('CLAUDECODE') == '1' else None, kind=kind)
    print(f"{summary['ok']} of {summary['jobs']} ok in {summary['seconds']} s")
    return 0 if summary['ok'] == summary['jobs'] else 1


# ---------------------------------------------------------------- MCP over stdio
_out = threading.Lock()


def send(msg):
    with _out:
        sys.stdout.write(json.dumps(msg) + '\n'); sys.stdout.flush()


def _handle(mid, method, params):
    try:
        if method == 'initialize':
            pv = params.get('protocolVersion')
            result = {'protocolVersion': pv if isinstance(pv, str) and pv else '2025-06-18', 'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'local-models', 'version': VERSION}, 'instructions': INSTRUCTIONS}
        elif method == 'ping': result = {}
        elif method == 'tools/list': result = {'tools': TOOLS}
        elif method == 'tools/call':
            out = call(params.get('name'), params.get('arguments'))
            bad = isinstance(out, dict) and out.get('ok') is False
            result = {'content': [{'type': 'text', 'text': json.dumps(out, indent=1, default=str)}], 'isError': bad}
        else:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'no method {method}'}}); return
        send({'jsonrpc': '2.0', 'id': mid, 'result': result})
    except Exception as e:                                     # a broken tool never takes the server down
        print(traceback.format_exc(), file=sys.stderr)
        send({'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': f'error: {e}'}], 'isError': True}})


def main():
    pool = cf.ThreadPoolExecutor(max_workers=8)
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try: msg = json.loads(line)
        except ValueError:
            send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}); continue
        if not isinstance(msg, dict): continue
        mid, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
        if mid is None: continue                                   # a notification: nothing to answer
        if not isinstance(params, dict): params = {}
        if method in ('initialize', 'ping', 'tools/list'): _handle(mid, method, params)   # at once, in order
        else: pool.submit(_handle, mid, method, params)            # a tool call: in a thread, so a long ask blocks nothing
    pool.shutdown(wait=False)
    return 0
