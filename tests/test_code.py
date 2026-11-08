#!/usr/bin/env python3
"""Rules about the code itself, checked like any other rule: no exception is swallowed silently (every broad handler records
it in the error record or says why not), the dashboard's script calls nothing it does not define, and the error record
behaves (one record a minute per repeated error, newest first, old and new lines read). Nothing is loaded or served.
`localmodels check` runs it; so can you: python3 tests/test_code.py
"""
import json, os, re, shutil, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
        print("the dashboard's script")
        page = open(os.path.join(ROOT, 'localmodels', 'dash.html')).read()
        missing = js_undefined(page)
        t('every name it calls or passes as a callback is defined in the page or by the browser', not missing, ', '.join(missing))
        t('the check catches the kind of slip it is for (a helper removed while still passed as a callback)',
          js_undefined('<script>function a(xs) { return xs.map(short).join(", "); }</script>') == ['short'])
        print('the error record')
        test_record(box)
        print("a project names itself (the name tool)")
        test_name_tool(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
