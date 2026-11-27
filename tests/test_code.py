#!/usr/bin/env python3
"""Rules about the code itself, checked like any other rule: no exception is swallowed silently (every broad handler records
it in the error record or says why not), the dashboard's script calls nothing it does not define, and the error record
behaves (one record a minute per repeated error, newest first, old and new lines read). Nothing is loaded or served.
`localmodels check` runs it; so can you: python3 tests/test_code.py
"""
import json, os, re, shutil, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _sandbox  # noqa: E402  (first: the state folder, port, LM Studio and lms point into a temporary folder)
from localmodels import errors, llmshare  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note and not good else ''))


# ---------------------------------------------------------------- no silent failure
def silent_handlers():
    """Broad handlers (except Exception / bare except) that neither record the error within their first lines nor carry a
    '# not recorded: <why>' comment on the except line. -> ['file:line: text']."""
    out = []
    for path in sorted(os.listdir(os.path.join(ROOT, 'localmodels'))):
        if not path.endswith('.py'): continue
        lines = open(os.path.join(ROOT, 'localmodels', path)).read().split('\n')
        for i, line in enumerate(lines):
            if not re.search(r'\bexcept\s*(Exception\b|:)', line): continue
            window = '\n'.join(lines[i:i + 4])
            if 'errors.record(' in window or '_error(' in window or 'not recorded:' in line: continue
            out.append(f'{path}:{i + 1}: {line.strip()[:100]}')
    return out


# ---------------------------------------------------------------- every sibling module a file uses, it imports
def unimported_siblings():
    """The package's own modules used as `name.attr` where no import of that name is in scope: not at module level, nor
    in the function that uses it or one around it (cli.build_release once used dash, which cli imports only inside other
    functions, and failed with NameError at run time). -> ['file:function: name']."""
    import ast
    pkg = os.path.join(ROOT, 'localmodels')
    mods = {f[:-3] for f in os.listdir(pkg) if f.endswith('.py') and f != '__init__.py'}
    out = []

    def binds(node):
        """Names bound directly in this scope (imports, assignments, arguments), not inside nested functions or classes."""
        got, todo = set(), list(ast.iter_child_nodes(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            got |= {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
        while todo:
            n = todo.pop()
            if isinstance(n, (ast.Import, ast.ImportFrom)): got |= {(a.asname or a.name).split('.')[0] for a in n.names}
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store): got.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): got.add(n.name); continue
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)): todo.extend(ast.iter_child_nodes(n))
        return got

    def visit(node, scope, where, f):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(n, scope | binds(n), getattr(n, 'name', 'lambda'), f)
            elif isinstance(n, ast.ClassDef): visit(n, scope | binds(n), n.name, f)
            else:
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in mods and n.value.id not in scope and n.value.id != f[:-3]:
                    out.append(f'{f}:{where}: {n.value.id}')
                visit(n, scope, where, f)

    for f in sorted(os.listdir(pkg)):
        if not f.endswith('.py'): continue
        tree = ast.parse(open(os.path.join(pkg, f)).read())
        visit(tree, binds(tree), '<module>', f)
    return sorted(set(out))


# ---------------------------------------------------------------- the dashboard's script: nothing called that is not defined
KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'function', 'return', 'typeof', 'new', 'await', 'async', 'do', 'else', 'in',
            'of', 'instanceof', 'void', 'delete', 'throw', 'yield', 'super', 'import', 'case'}
GLOBALS = {'Math', 'JSON', 'Date', 'Number', 'String', 'Array', 'Object', 'Promise', 'Set', 'Map', 'WeakMap', 'Error', 'TypeError',
           'RangeError', 'Boolean', 'Symbol', 'Intl', 'URL', 'URLSearchParams', 'AbortController', 'AbortSignal', 'ResizeObserver', 'fetch', 'confirm', 'alert', 'setTimeout', 'setInterval',
           'clearTimeout', 'clearInterval', 'requestAnimationFrame', 'cancelAnimationFrame', 'getComputedStyle', 'matchMedia',
           'encodeURIComponent', 'decodeURIComponent', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'console', 'window', 'document',
           'localStorage', 'navigator', 'location', 'performance', 'structuredClone', 'queueMicrotask'}


def strip_js(js):
    """The script without its strings, comments and regular expressions; a template literal keeps its ${...} expressions."""
    out, i, n = [], 0, len(js)

    def string_end(i, q):
        i += 1
        while i < n and js[i] != q and js[i] != '\n':
            i += 2 if js[i] == '\\' else 1
        return i + 1

    def template(i):
        i, kept = i + 1, []
        while i < n and js[i] != '`':
            if js[i] == '\\': i += 2; continue
            if js.startswith('${', i):
                j, depth, start = i + 2, 1, i + 2
                while j < n and depth:
                    c = js[j]
                    if c in '\'"': j = string_end(j, c); continue
                    if c == '`': j, sub = template(j); kept.extend(sub); continue
                    depth += c == '{'; depth -= c == '}'; j += 1
                kept.append(js[start:j - 1]); i = j; continue
            i += 1
        return i + 1, kept

    def regex_end(i):
        i, cls = i + 1, False
        while i < n and js[i] != '\n':
            c = js[i]
            if c == '\\': i += 2; continue
            if c == '[': cls = True
            elif c == ']': cls = False
            elif c == '/' and not cls: i += 1; break
            i += 1
        while i < n and js[i].isalpha(): i += 1
        return i

    while i < n:
        c = js[i]
        if c in '\'"': i = string_end(i, c); out.append(' '); continue
        if c == '`': i, kept = template(i); out.append(' ' + ' ; '.join(strip_js(k) for k in kept) + ' '); continue
        if js.startswith('//', i): j = js.find('\n', i); i = n if j < 0 else j; continue
        if js.startswith('/*', i): j = js.find('*/', i + 2); i = n if j < 0 else j + 2; continue
        if c == '/':
            prev = ''.join(out).rstrip()
            if not prev or prev[-1] in '(,=:[!&|?{};' or re.search(r'\breturn$', prev): i = regex_end(i); out.append(' '); continue
        out.append(c); i += 1
    return ''.join(out)


def js_undefined(page):
    """Names the page's script calls, or passes as a callback, that it never defines and the browser does not provide."""
    code = strip_js('\n'.join(re.findall(r'<script>(.*?)</script>', page, re.S)))
    name = r'[A-Za-z_$][\w$]*'
    defined = set(re.findall(rf'\bfunction\s+({name})', code))
    for params in re.findall(r'\bfunction\s*[\w$]*\s*\(([^)]*)\)', code) + re.findall(r'\(([^()]*)\)\s*=>', code):   # parameters
        defined |= set(re.findall(rf'(?:^|[,{{\[])\s*\.{{0,3}}({name})', params))
    defined |= set(re.findall(rf'\b({name})\s*=>', code)) | set(re.findall(rf'\bcatch\s*\(\s*({name})', code))
    for decl in re.findall(r'\b(?:const|let|var)\s+([^;\n]*)', code):                     # declarations, lists, destructuring
        decl = decl.strip()
        defined |= set(re.findall(rf'(?:^|,)\s*({name})\s*=(?!=)', decl)) | set(re.findall(rf'^({name})\b', decl))
        for group in re.findall(r'^[\[{]([^\]}]*)[\]}]', decl):
            defined |= set(re.findall(rf'(?:^|,)\s*(?:[\w$]+\s*:\s*)?\.{{0,3}}({name})', group))
    called = set(re.findall(rf'(?<![\w$.])({name})\s*\(', code))
    passed = set(re.findall(rf'\.(?:map|filter|forEach|flatMap|some|every|find|findIndex|sort|reduce)\(\s*({name})\s*[,)]', code))
    passed |= set(re.findall(rf'\b(?:setTimeout|setInterval|requestAnimationFrame|guard)\(\s*(?:[^,()]*,\s*)?({name})\s*[,)]', code))
    passed |= set(re.findall(rf'addEventListener\(\s*,\s*({name})\s*[,)]', code))
    return sorted((called | passed) - defined - KEYWORDS - GLOBALS)


# ---------------------------------------------------------------- the error record
def test_record(box):
    errors.FILE = os.path.join(box, 'errors.jsonl')
    errors._last.clear(); errors._summary.update(key=None, at=0.0, value=None)
    try: raise KeyError('x')
    except KeyError as e: first = errors.record('test site', e)
    try: raise KeyError('x')
    except KeyError as e: again = errors.record('test site', e)
    t('a caught exception is recorded with where, what and a traceback', first and first['where'] == 'test site'
      and first['error'] == "KeyError: 'x'" and 'Traceback' in first['trace'], first)
    t('the same error from the same place within a minute is counted, not written again', again is None and len(errors.recent(5)) == 1)
    errors._last[('gateway', 'test site', "KeyError: 'x'")][0] -= 61
    try: raise KeyError('x')
    except KeyError as e: later = errors.record('test site', e)
    t('a minute later it is written again, with the repeats it held back', later and later.get('repeats') == 1, later)
    errors.record('dashboard: Clients', words='ReferenceError: short is not defined', trace='at renderUsers', source='dashboard')
    rs = errors.recent(5)
    t('newest first; a dashboard error carries its source', rs[0]['source'] == 'dashboard' and rs[0]['where'] == 'dashboard: Clients', rs[:1])
    s = errors.summary(10)
    t('the summary the dashboard shows: count and the last one', s and s['count'] == 3 and s['last']['where'] == 'dashboard: Clients', s)
    open(errors.FILE, 'a').write('not json\n' + json.dumps(dict(at=time.time() - 7200, source='gateway', where='old', error='E')) + '\n')
    t('a broken line is skipped; an old one falls outside the window', len(errors.recent(5)) == 3 and len(errors.recent(180)) == 4)

    class Full:                                              # a stderr on a full disk
        def write(self, *a): raise OSError(28, 'No space left on device')
        def flush(self): raise OSError(28, 'No space left on device')
    was, sys.stderr = sys.stderr, Full()
    try:
        try: raise KeyError('y')
        except KeyError as e: rec, err = errors.record('loop with a full disk', e), None
    except Exception as e: rec, err = None, e
    finally: sys.stderr = was
    t('the recorder never raises, even when its own output fails (a background loop would end in its error handler)', err is None and rec, repr(err) if err else rec)


def test_mcp_stdout(box):
    import subprocess
    env = dict(os.environ, LOCALMODELS_STATE=os.path.join(box, 'mcp-state'), PYTHONDONTWRITEBYTECODE='1')
    msgs = [dict(jsonrpc='2.0', id=1, method='initialize', params={}),
            dict(jsonrpc='2.0', id=2, method='tools/call', params=dict(name='no-such-tool', arguments={}))]
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'bin', 'localmodels'), 'mcp'], input=''.join(json.dumps(m) + '\n' for m in msgs),
                       capture_output=True, text=True, env=env, timeout=60)
    lines = [x for x in r.stdout.split('\n') if x.strip()]

    def parses(x):
        try: return isinstance(json.loads(x), dict)
        except ValueError: return False
    t("after a tool call that raises, every line the MCP server wrote to stdout is a JSON-RPC message (the error line goes to stderr)",
      len(lines) == 2 and all(parses(x) for x in lines) and 'no-such-tool' in r.stderr, lines)
    t('the error is still recorded, in the state folder the server was given', os.path.exists(os.path.join(box, 'mcp-state', 'errors.jsonl')))


def test_no_proxy(box):
    import http.server, threading, urllib.request
    got = []

    class Proxy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self): got.append(self.path); self.send_response(502); self.end_headers()
        do_POST = do_GET
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Proxy); threading.Thread(target=srv.serve_forever, daemon=True).start()
    was = {k: os.environ.get(k) for k in ('http_proxy', 'HTTP_PROXY', 'no_proxy', 'NO_PROXY')}
    os.environ.update(http_proxy=f'http://127.0.0.1:{srv.server_address[1]}', HTTP_PROXY=f'http://127.0.0.1:{srv.server_address[1]}')
    for k in ('no_proxy', 'NO_PROXY'): os.environ.pop(k, None)
    try:
        try: urllib.request.build_opener().open(llmshare.HUB + '/hub/health', timeout=5)
        except OSError: pass
        t("(the test's proxy is in use: a default urllib opener sends this Mac's own requests to it)", got, got)
        got.clear()
        up = llmshare.hub_up(fresh=True)
        try: llmshare.post('/v1/chat/completions', dict(model='m', messages=[dict(role='user', content='a prompt')]), 'proj', timeout=5)
        except OSError: pass
        t('with http_proxy set, the hub, LM Studio and the gateway are reached directly: the proxy receives nothing', not got and up is False, (got, up))
    finally:
        for k, v in was.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        srv.shutdown()


def test_recent_requests(box):
    from concurrent.futures import ThreadPoolExecutor
    from localmodels import web
    from unittest.mock import patch
    log = os.path.join(box, 'tail.jsonl')
    now = time.time()
    with patch.object(llmshare, 'REQUESTS', log):
        for p, rows in ((log + '.1', range(5)), (log, range(5, 8))):
            with open(p, 'w') as f:
                for n in rows: f.write(json.dumps(dict(at=now, n=n, step=f'item {n}', note='x'*7000)) + '\n')
        with open(log, 'a') as f: f.write('{"at":')
        t('the newest rows cross rotation and block boundaries, skipping an incomplete final record',
          [r['n'] for r in llmshare.recent_requests(4, 1)] == [7, 6, 5, 4])
        t('a filtered tail stops after enough qualifying records', [r['n'] for r in llmshare.recent_requests(3, 1, lambda r: r['n'] % 2)] == [7, 5, 3])
        with ThreadPoolExecutor(4) as pool:
            views = list(pool.map(web.recent, [1, 3, 5, 8]))
        t('concurrent dashboard readers each receive the requested tail across rotation',
          all([r['step'] for r in v] == [f'item {n}' for n in range(7, 7-count, -1)] for v, count in zip(views, [1, 3, 5, 8])))


def test_agent_roots(box):
    import subprocess
    from unittest.mock import patch
    from localmodels import mcp_server, whois
    home = os.path.realpath(os.path.join(box, 'home'))
    proj, other, ssh, lib = (os.path.join(home, *p) for p in (('Documents', 'proj', 'src'), ('Documents', 'other'), ('.ssh',), ('Library', 'x')))
    tree = os.path.join(home, 'Documents', 'proj', '.claude', 'worktrees', 'w')
    for d in (proj, other, ssh, lib): os.makedirs(d)
    repo = os.path.dirname(proj)
    def git(*args):
        subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True)
    git('init', '--template=')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.org', '-c', 'commit.gpgSign=false', 'commit', '--allow-empty', '-m', 'fixture')
    git('worktree', 'add', '--detach', tree)
    saved = os.environ.get('HOME'), os.environ.get('PWD'), whois.ROOTS, mcp_server.AGENTS
    os.environ['HOME'], os.environ['PWD'], whois.ROOTS = home, tree, [os.path.join(home, 'Documents')]
    mcp_server.AGENTS = os.path.join(box, 'agents')
    job = '20260101-120000-ask'
    for p, root in (('proj', proj), ('other', other)):
        job_dir = os.path.join(mcp_server.AGENTS, p, job)
        os.makedirs(job_dir)
        llmshare._write(os.path.join(job_dir, 'jobs.json'), dict(root=root, jobs=[dict(label='ask')]))
        llmshare._write(os.path.join(job_dir, 'ask.json'), dict(ok=True, answer='x' * 7200))
    try:
        R = mcp_server._root
        t('the home folder named with other capitals is refused (it would put ~/Library inside the root)',
          R(os.path.join(os.path.dirname(home), 'HOME'))[0] is None and R(home)[0] is None, R(os.path.join(os.path.dirname(home), 'HOME')))
        t('a dot-folder of the home folder (~/.ssh) is refused, from a terminal too', R(ssh)[0] is None and '.ssh' in R(ssh)[1], R(ssh))
        t('a folder in ~/Library is refused', R(lib)[0] is None)
        t("from a worktree session, its project's folder is accepted", R(proj, within=mcp_server._session_folder()) == (proj, None), R(proj, within=mcp_server._session_folder()))
        r = mcp_server.call('ask', dict(prompt='x', folder=other))
        t("through the MCP tools a folder outside the session's project is refused", r.get('ok') is False and 'outside' in (r.get('error') or ''), r)
        r = mcp_server.call('batch', dict(jobs=[dict(prompt='x')], folder=ssh))
        t('and so is a dot-folder', r.get('ok') is False and '.ssh' in (r.get('error') or ''), r)
        t("job_status serves this project's jobs, not another project's",
          mcp_server.call('job_status', dict(job='proj/' + job)).get('ok') is True and mcp_server.call('job_status', dict(job='other/' + job)).get('ok') is False)
        t('a job id must have exactly the two generated components',
          not mcp_server.call('job_status', dict(job='proj/extra/' + job))['ok'])
        linked = os.path.join(mcp_server.AGENTS, 'proj', '20260101-120001-ask')
        os.symlink(os.path.join(mcp_server.AGENTS, 'other', job), linked)
        t('a linked directory is refused before reading results', not mcp_server.call('job_status', dict(job='proj/' + os.path.basename(linked)))['ok'])
        result = mcp_server.call('job_status', dict(job='proj/' + job, label='ask'))
        t('a labeled result includes the whole answer', len(result['results'][0]['answer']) == 7200)
        result = mcp_server.call('job_status', dict(job='proj/' + job, results=True))['results'][0]
        t('a summary still caps the answer and explains how to retrieve the rest', len(result['answer']) == 6000 and 'label' in result['note'])
        old = os.path.join(mcp_server.AGENTS, 'proj', job)
        for invalid_root in ('.', ['not a path']):
            llmshare._write(os.path.join(old, 'jobs.json'), dict(root=invalid_root, jobs=[]))
            t('legacy jobs require an absolute directory to establish ownership', not mcp_server.call('job_status', dict(job='proj/' + job))['ok'])
        llmshare._write(os.path.join(old, 'jobs.json'), dict(root=proj, jobs=[dict(label='item'), dict(label='item')]))
        llmshare._write(os.path.join(old, 'plan.json'), dict(jobs=['item', 'item-2']))
        llmshare._write(os.path.join(old, 'item-2.json'), dict(ok=True, answer='second item', result=False))
        result = mcp_server.call('job_status', dict(job='proj/' + job, label='item-2'))
        t('legacy batches read the result names the runner actually wrote', result['results'][0]['answer'] == 'second item')
        t('a false structured result survives labeled lookup', result['results'][0].get('result') is False)
        sibling = os.path.join(os.path.dirname(repo), 'group', 'repo')
        os.makedirs(sibling)
        os.environ['PWD'] = sibling
        t('a non-Git project under a shared parent is scoped to the session folder', mcp_server._session_folder().main == sibling)
        git('-C', sibling, 'init', '--template=')
        t('a nested repository is its own project', mcp_server._session_folder().main == sibling)
        t('a legacy job named for a shared parent still requires the saved project root',
          not mcp_server.call('job_status', dict(job='proj/' + job))['ok'])
        external = os.path.join(home, 'Documents', 'external-worktree')
        git('worktree', 'add', '--detach', external)
        os.environ['PWD'] = external
        scope = mcp_server._session_folder()
        t('an external worktree can use its own files and shares its main checkout’s job identity',
          scope.checkout == external and scope.main == repo and R(external, within=scope) == (external, None))
        with patch.object(mcp_server.batches, 'model_info', return_value=('test', 'a' * 64, 4096)):
            jid, _ = mcp_server.start('batch', [dict(prompt='x')], folder=external, within=scope)
        os.environ['PWD'] = repo
        t('the main checkout can read a job started in the external worktree', mcp_server.call('job_status', dict(job=jid))['ok'])
        twin = os.path.join(home, 'Documents', 'group2', os.path.basename(repo))
        os.makedirs(twin); git('-C', twin, 'init', '--template=')
        os.environ['PWD'] = twin
        t('independent repositories with the same name cannot read each other’s jobs', not mcp_server.call('job_status', dict(job=jid))['ok'])
        shared = os.path.join(mcp_server.AGENTS, 'group', job)
        os.makedirs(shared)
        llmshare._write(os.path.join(shared, 'jobs.json'), dict(root=sibling, jobs=[]))
        other_repo = os.path.join(os.path.dirname(sibling), 'sibling')
        os.makedirs(other_repo); git('-C', other_repo, 'init', '--template=')
        os.environ['PWD'] = other_repo
        t('sibling Git checkouts under one display name authorize neither legacy results nor files from each other',
          not mcp_server.call('job_status', dict(job='group/' + job))['ok'] and R(sibling, within=mcp_server._session_folder())[0] is None)
    finally:
        for k, v in (('HOME', saved[0]), ('PWD', saved[1])):
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        whois.ROOTS, mcp_server.AGENTS = saved[2], saved[3]


def test_name_tool(box):
    from localmodels import mcp_server, names
    names.FILE, names._cache['mtime'] = os.path.join(box, 'names.json'), None
    was = os.environ.get('PWD')
    os.environ['PWD'] = os.path.join(os.path.expanduser('~'), 'Documents', 'zz-folder-123', 'scripts')
    try:
        r = mcp_server.name_project('research', ['lab'], True)
        t("the name tool: the session's project folder and its former names show as the new name, private",
          r['ok'] and names.project('zz-folder-123') == 'research' and names.project('lab') == 'research' and names.private('research'), r)
        t('an empty name is refused, and nothing changes', not mcp_server.name_project('  ')['ok'] and names.project('lab') == 'research')
    finally:
        if was is None: os.environ.pop('PWD', None)
        else: os.environ['PWD'] = was


def main():
    box = tempfile.mkdtemp(prefix='localmodels-codetest-')
    try:
        print('no exception is swallowed silently')
        bad = silent_handlers()
        t('every broad handler records the error in the error record, or says why not (# not recorded: ...)', not bad, '; '.join(bad))
        bad = unimported_siblings()
        t("every sibling module a function uses (name.attr) is imported where that function can see it", not bad, '; '.join(bad))
        reuse = [f for f in os.listdir(os.path.join(ROOT, 'localmodels')) if f.endswith('.py') and 'SO_REUSEPORT' in open(os.path.join(ROOT, 'localmodels', f)).read()]
        t('no SO_REUSEPORT anywhere: one listening socket per address, shared by inheritance', not reuse, reuse)
        py39 = '/usr/bin/python3'
        if os.path.exists(py39):
            import subprocess
            r = subprocess.run([py39, '-c', 'import sys, py_compile, glob; [py_compile.compile(f, doraise=True, cfile=None, optimize=-1) or None for f in glob.glob(sys.argv[1] + "/*.py")]; print(sys.version_info[:2])',
                                os.path.join(ROOT, 'localmodels')], capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPYCACHEPREFIX=os.path.join(box, 'pyc')))
            t(f'every module compiles under {py39} (the oldest Python the hub supports)', r.returncode == 0, (r.stdout + r.stderr)[-300:])
        print("the dashboard's script")
        page = open(os.path.join(ROOT, 'localmodels', 'dash.html')).read()
        missing = js_undefined(page)
        t('every name it calls or passes as a callback is defined in the page or by the browser', not missing, ', '.join(missing))
        t("a peer that is not up: the peers chip says why, since when and when the next poll comes, and a poll-now button posts /hub/link",
          'id="poll-now"' in page and "fetch('/hub/link', {method: 'POST'})" in page and 'next poll in' in page and 'p.since' in page and 'p.why' in page)
        t("the color setting: Haedo Labs™ Sapphire is the default, with the system accent (/hub/accent) and one's own color, kept per browser",
          '<option value="sapphire">color: Haedo Labs™ Sapphire</option>' in page and "fetch('/hub/accent')" in page and 'type="color" id="accent-own"' in page
          and "localStorage.setItem('lm-accent'" in page and 'style="fill: var(--brand)"' in page)
        t('the check catches the kind of slip it is for (a helper removed while still passed as a callback)',
          js_undefined('<script>function a(xs) { return xs.map(short).join(", "); }</script>') == ['short'])
        print('the inventories kept by hand')
        import glob
        from localmodels import cli, mcp_server
        suites = sorted(os.path.basename(f) for f in glob.glob(os.path.join(ROOT, 'tests', 'test_*.py')))
        t('every suite in tests/ is one that check and publish run (cli.TESTS)', suites == sorted(cli.TESTS), (suites, cli.TESTS))
        tools = [x['name'] for x in mcp_server.TOOLS]
        lists = {f: open(os.path.join(ROOT, f)).read() for f in ('localmodels/mcp_server.py', 'README.md', '.claude-plugin/plugin.json', 'commands/setup.md')}
        rows = {f: [l for l in text.split('\n') if 'job_status' in l and 'swap_check' in l and 'tools' in l.lower()] for f, text in lists.items()}
        missing = {f: [n for n in tools if not ls or not all(re.search(rf'\b{n}\b', l) for l in ls)] for f, ls in rows.items()}
        t('every MCP tool is named where the tools are listed (module docstring, README, plugin manifest, setup command)', not any(missing.values()), missing)
        print("what a client sent stays out of the hub's records")
        from localmodels import gateway, whois
        apps = [whois._app(a) for a in (['node', '-e', 'fetch("a secret prompt")'], ['python3', '-X', 'dev', '-c', 'print("secret")'],
                                         ['/usr/bin/perl', '-p', '-e', 's/x/secret/'], ['python3', 'tools/run.py', '--x'], ['python3', '-m', 'pkg.mod'])]
        t("inline code (-c, -e, -p, --eval) is never a client's name, only its interpreter's", apps == ['node', 'python', 'perl', 'run.py', 'pkg.mod'], apps)
        w = [gateway.peer_words(x) for x in ('Error rendering prompt: "the text of a prompt"', 'RuntimeError: [METAL] Command buffer execution failed: Insufficient Memory', None)]
        t("a peer's error text is recorded only as the engine's failure words, never as it came (it can quote a prompt)", w == ['', 'Insufficient Memory', ''], w)
        print('the error record')
        test_record(box)
        test_mcp_stdout(box)
        print("a project names itself (the name tool)")
        test_name_tool(box)
        print("the local agents read only the session's own project")
        test_recent_requests(box)
        test_agent_roots(box)
        print('requests to this Mac never go through a proxy')
        test_no_proxy(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    t('no hub path points into the live state folder (tests/_sandbox.py)', not _sandbox.stray(), _sandbox.stray())
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
