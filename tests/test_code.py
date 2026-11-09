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
           'RangeError', 'Boolean', 'Symbol', 'Intl', 'URL', 'URLSearchParams', 'AbortController', 'fetch', 'setTimeout', 'setInterval',
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


def test_agent_roots(box):
    from localmodels import mcp_server, whois
    home = os.path.realpath(os.path.join(box, 'home'))
    proj, other, ssh, lib = (os.path.join(home, *p) for p in (('Documents', 'proj', 'src'), ('Documents', 'other'), ('.ssh',), ('Library', 'x')))
    tree = os.path.join(home, 'Documents', 'proj', '.claude', 'worktrees', 'w')
    for d in (proj, other, ssh, lib, tree): os.makedirs(d)
    saved = os.environ.get('HOME'), os.environ.get('PWD'), whois.ROOTS, mcp_server.AGENTS
    os.environ['HOME'], os.environ['PWD'], whois.ROOTS = home, tree, [os.path.join(home, 'Documents')]
    mcp_server.AGENTS = os.path.join(box, 'agents')
    for p in ('proj', 'other'):
        os.makedirs(os.path.join(mcp_server.AGENTS, p, 'ask-1'))
        open(os.path.join(mcp_server.AGENTS, p, 'ask-1', 'jobs.json'), 'w').write(json.dumps(dict(jobs=[])))
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
          mcp_server.call('job_status', dict(job='proj/ask-1')).get('ok') is True and mcp_server.call('job_status', dict(job='other/ask-1')).get('ok') is False,
          (mcp_server.call('job_status', dict(job='proj/ask-1')), mcp_server.call('job_status', dict(job='other/ask-1'))))
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
        t('the check catches the kind of slip it is for (a helper removed while still passed as a callback)',
          js_undefined('<script>function a(xs) { return xs.map(short).join(", "); }</script>') == ['short'])
        print('the error record')
        test_record(box)
        test_mcp_stdout(box)
        print("a project names itself (the name tool)")
        test_name_tool(box)
        print("the local agents read only the session's own project")
        test_agent_roots(box)
        print('requests to this Mac never go through a proxy')
        test_no_proxy(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    t('no hub path points into the live state folder (tests/_sandbox.py)', not _sandbox.stray(), _sandbox.stray())
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
