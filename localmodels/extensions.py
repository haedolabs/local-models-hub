"""extensions - other programs' sections in the menu-bar app: one manifest file per extension in the hub's state folder,
the menu each one serves as JSON, and the closed set of actions the app may take for it. Protocol version 1; the
reference for an extension's author is GUIDE.md, "Extensions".

An extension is any program on this Mac with an HTTP endpoint on localhost that answers its menu. It joins by writing one
manifest (register()) and leaves by deleting it (unregister()). The hub never imports an extension's code, and never
starts or stops one on its own: the menu-bar app reads the manifests, asks each one's menu URL, draws each menu as its
own section, and acts only through the actions below, on the extension's own origin, or by a verb its manifest lists.
The dashboard lists them too (view(), GET /hub/extensions), each with a link to its page, and runs a listed verb when a
person presses its button (run(), POST /hub/extensions/run); without the gateway the menu-bar app still draws them.

  manifest  <state>/extensions/<id>.json
            {"extension": 1, "id": "name.instance", "name": "...", "title": "...", "version": "...", "maker": "...",
             "menu": "http://localhost:<port>/<path>", "open": "http://localhost:<port>/",
             "token": "<file holding the token>", "token_header": "X-...",
             "run": {"command": ["<program>", "<arg>", ...], "env": {"NAME": "value"}, "verbs": ["start", "restart"]}}
  menu      GET <menu> -> {"menu": 1, "summary": "...", "alerts": [line], "lines": [line], "events": [event]}
            line    {"title": "...", "action": <action>, "indent": 0-3, "separator": true, "children": [line]}
            event   {"key": "...", "title": "...", "body": "..."}   (one notification per key, ever)
  actions   {"open": "/<path>"}                      a page on the extension's own origin, in the browser
            {"post": "/<path>", "body": {"k": "v"}, "confirm": "...", "input": {"prompt": "...", "field": "k",
             "placeholder": "..."}, "reveal": true}  a POST to its own origin, with its token; the answer's "ok",
                                                     "error" or "problems", and "path" (shown in the Finder: reveal)
            {"run": "<verb>", "confirm": "..."}      the manifest's command with that verb, if the manifest lists it
Standard library only.
"""
import json, os, re, subprocess, tempfile, threading, urllib.parse

from . import llmshare

VERSION = 1                                              # the protocol: manifest "extension", menu "menu"
DIR = os.path.join(llmshare.SHARED, 'extensions')
ID = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}$')
HEADER = re.compile(r'X-[A-Za-z0-9-]{1,60}$')
VERB = re.compile(r'[a-z][a-z0-9-]{0,31}$')
DEPTH = 3                                                # a line's submenus nest at most this deep
LINES = 200                                              # at most this many lines in a menu, all levels counted


def local(url):
    """Is this an http URL on this Mac (localhost or 127.0.0.1, any port)?"""
    try: u = urllib.parse.urlsplit(str(url))
    except ValueError: return False
    return u.scheme == 'http' and u.hostname in ('localhost', '127.0.0.1')


def origin(url):
    u = urllib.parse.urlsplit(str(url))
    return f'{u.scheme}://{u.netloc}'


def validate(m):
    """A manifest's problems, in words ([] when it is sound)."""
    if not isinstance(m, dict): return ['not a JSON object']
    p = []
    if m.get('extension') != VERSION: p.append(f'"extension" is {m.get("extension")!r}: this hub reads version {VERSION}')
    if not isinstance(m.get('id'), str) or not ID.match(m['id']): p.append('"id": lower-case letters, digits, ".", "_" and "-", at most 64')
    for k in ('name', 'title', 'version', 'maker'):
        if k in m and not isinstance(m[k], str): p.append(f'"{k}" is not text')
    if not isinstance(m.get('name'), str) or not m.get('name', '').strip(): p.append('"name" is missing')
    if not local(m.get('menu')): p.append('"menu" must be an http URL on localhost or 127.0.0.1')
    if m.get('open') is not None and not local(m['open']): p.append('"open" must be an http URL on localhost or 127.0.0.1')
    if local(m.get('menu')) and m.get('open') and local(m['open']) and origin(m['open']) != origin(m['menu']):
        p.append('"open" is on another origin than "menu"')
    if m.get('token') is not None and not (isinstance(m['token'], str) and os.path.isabs(m['token'])): p.append('"token" must be an absolute file path')
    if m.get('token_header') is not None and not (isinstance(m['token_header'], str) and HEADER.match(m['token_header'])):
        p.append('"token_header" must be an X- header name')
    run = m.get('run')
    if run is not None:
        if not isinstance(run, dict): p.append('"run" is not an object')
        else:
            cmd, env, verbs = run.get('command'), run.get('env', {}), run.get('verbs')
            if not (isinstance(cmd, list) and cmd and all(isinstance(x, str) and x for x in cmd) and os.path.isabs(cmd[0])):
                p.append('"run.command": a list of words, the first an absolute path to a program')
            if not (isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())): p.append('"run.env": names to text')
            if not (isinstance(verbs, list) and verbs and all(isinstance(v, str) and VERB.match(v) for v in verbs)):
                p.append('"run.verbs": a list of lower-case words, the only arguments the app may add to the command')
    return p


def action_problem(a, m):
    """What is wrong with one action of a menu, for this manifest (None when the app may take it)."""
    if not isinstance(a, dict): return 'an action is not an object'
    kinds = [k for k in ('open', 'post', 'run') if k in a]
    if len(kinds) != 1: return 'an action has exactly one of "open", "post", "run"'
    k = kinds[0]
    if a.get('confirm') is not None and not isinstance(a['confirm'], str): return '"confirm" is not text'
    if k == 'open':
        v = a['open']
        if not isinstance(v, str) or not (v.startswith('/') and not v.startswith('//') or (local(v) and origin(v) == origin(m.get('menu', '')))):
            return f'"open": {v!r} is not a path or a URL on the extension\'s own origin'
    elif k == 'post':
        v = a['post']
        if not isinstance(v, str) or not v.startswith('/') or v.startswith('//'): return f'"post": {v!r} is not a path on the extension\'s own origin'
        if a.get('body') is not None and not (isinstance(a['body'], dict) and all(isinstance(x, str) for x in list(a['body']) + list(a['body'].values()))):
            return '"body": names to text'
        inp = a.get('input')
        if inp is not None and not (isinstance(inp, dict) and isinstance(inp.get('prompt'), str) and isinstance(inp.get('field'), str) and inp['field']):
            return '"input": a "prompt" and a "field"'
        if a.get('reveal') is not None and not isinstance(a['reveal'], bool): return '"reveal" is true or false'
    else:
        verbs = (m.get('run') or {}).get('verbs') or []
        if a['run'] not in verbs: return f'"run": {a["run"]!r} is not one of the manifest\'s verbs ({", ".join(verbs) or "none"})'
    return None


def validate_menu(menu, m):
    """A menu's problems for this manifest ([] when the app draws all of it as served)."""
    if not isinstance(menu, dict): return ['the menu is not a JSON object']
    p, count = [], [0]
    if menu.get('menu') != VERSION: p.append(f'"menu" is {menu.get("menu")!r}: this hub reads version {VERSION}')
    if menu.get('summary') is not None and not isinstance(menu['summary'], str): p.append('"summary" is not text')

    def lines(xs, where, depth):
        if xs is None: return
        if not isinstance(xs, list): p.append(f'{where} is not a list'); return
        for i, x in enumerate(xs):
            count[0] += 1
            at = f'{where}[{i}]'
            if not isinstance(x, dict) or not isinstance(x.get('title'), str) or not x['title'].strip(): p.append(f'{at}: no "title"'); continue
            if x.get('action') is not None:
                why = action_problem(x['action'], m)
                if why: p.append(f'{at} ({x["title"][:40]}): {why}')
            if x.get('indent') is not None and x['indent'] not in (0, 1, 2, 3): p.append(f'{at}: "indent" is 0 to 3')
            if x.get('children'):
                if depth >= DEPTH: p.append(f'{at}: submenus nest at most {DEPTH} deep')
                else: lines(x['children'], f'{at}.children', depth + 1)
    lines(menu.get('alerts'), 'alerts', DEPTH)                       # an alert is one line, no submenu
    lines(menu.get('lines'), 'lines', 1)
    if count[0] > LINES: p.append(f'{count[0]} lines: at most {LINES}')
    for i, e in enumerate(menu.get('events') or []):
        if not (isinstance(e, dict) and all(isinstance(e.get(k), str) and e.get(k) for k in ('key', 'title'))): p.append(f'events[{i}]: a "key" and a "title"')
    return p


# ---------------------------------------------------------------- the files
def path(ext_id): return os.path.join(DIR, ext_id + '.json')


def register(m):
    """Write (or replace) an extension's manifest. -> its path; ValueError naming the problems when it is not sound."""
    p = validate(m)
    if p: raise ValueError('; '.join(p))
    os.makedirs(DIR, exist_ok=True)
    tmp = path(m['id']) + '.tmp'
    with open(tmp, 'w') as f: json.dump(m, f, indent=1); f.write('\n')
    os.replace(tmp, path(m['id']))
    return path(m['id'])


def unregister(ext_id):
    """Delete an extension's manifest. -> whether there was one."""
    try: os.remove(path(ext_id)); return True
    except FileNotFoundError: return False


def load():
    """Every manifest in the folder: [{"file", "manifest" (None when unreadable), "problems"}], by file name."""
    out = []
    try: names = sorted(f for f in os.listdir(DIR) if f.endswith('.json'))
    except FileNotFoundError: return out
    for f in names:
        try: m = json.load(open(os.path.join(DIR, f)))
        except (OSError, ValueError) as e: out.append(dict(file=f, manifest=None, problems=[f'unreadable: {e}'])); continue
        probs = validate(m)
        if isinstance(m, dict) and m.get('id') and f != f"{m['id']}.json": probs.append(f'the file is named {f}, its id is {m["id"]}')
        out.append(dict(file=f, manifest=m, problems=probs))
    return out


def fetch_menu(m, timeout=3.0):
    """GET the extension's menu. -> (menu, None) or (None, why not)."""
    try:
        with llmshare.urlopen(m['menu'], timeout=timeout) as r: return json.load(r), None
    except (OSError, ValueError) as e: return None, f'{type(e).__name__}: {e}'


# ---------------------------------------------------------------- the dashboard's launcher
def view(timeout=1.5):
    """Every sound manifest as the dashboard shows it, each menu asked at once (`timeout` each):
    [{"id", "name", "title", "label", "open", "answering", "summary", "alerts", "verbs", "why"}]."""
    found = [x['manifest'] for x in load() if not x['problems']]
    got = {}
    asks = [threading.Thread(target=lambda m=m: got.__setitem__(m['id'], fetch_menu(m, timeout)), daemon=True) for m in found]
    for a in asks: a.start()
    for a in asks: a.join(timeout + 0.5)
    out = []
    for m in found:
        menu, why = got.get(m['id'], (None, f'no answer in {timeout:g} s'))
        sound = isinstance(menu, dict) and menu.get('menu') == VERSION
        alerts = [a['title'] for a in (menu.get('alerts') if sound and isinstance(menu.get('alerts'), list) else [])
                  if isinstance(a, dict) and isinstance(a.get('title'), str) and a['title'].strip()]
        out.append(dict(id=m['id'], name=m['name'], title=m.get('title') or '', label=m['name'] + (f" — {m['title']}" if m.get('title') else ''),
                        open=m.get('open') or '', answering=menu is not None,
                        summary=menu.get('summary') if sound and isinstance(menu.get('summary'), str) else '',
                        alerts=alerts[:5], verbs=list((m.get('run') or {}).get('verbs') or []),
                        why=why if menu is None else None if sound else f'its menu is not protocol {VERSION}'))
    return out


_runs, _runs_lock = {}, threading.Lock()                 # id -> the run under way (one at a time per extension)


def run(ext_id, verb, wait=30.0):
    """Run an extension's command with one of the verbs its manifest lists, as the menu-bar app does on a click (the
    dashboard's Start): its environment added to the gateway's, in a session of its own (so it outlives the gateway),
    waited for up to `wait` seconds. -> (HTTP code, answer)."""
    m = next((x['manifest'] for x in load() if isinstance(x['manifest'], dict) and x['manifest'].get('id') == ext_id), None)
    if m is None: return 404, dict(error=f'no extension {ext_id!r} ({DIR})')
    probs = validate(m)
    if probs: return 409, dict(error='its manifest is not sound: ' + '; '.join(probs))
    r = m.get('run') or {}
    if verb not in (r.get('verbs') or []):
        return 403, dict(error=f'{verb!r} is not one of its manifest\'s verbs ({", ".join(r.get("verbs") or []) or "none"})')
    with _runs_lock:
        p = _runs.get(ext_id)
        if p is not None and p.poll() is None: return 409, dict(error=f'{m["name"]}: an earlier {verb} is still running')
        out = tempfile.TemporaryFile()
        try:
            p = subprocess.Popen(r['command'] + [verb], env=dict(os.environ, **(r.get('env') or {})), cwd=os.path.expanduser('~'),
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e: out.close(); return 500, dict(error=f'{r["command"][0]}: {e.strerror or e}')
        _runs[ext_id] = p
    try: code = p.wait(wait)
    except subprocess.TimeoutExpired: return 202, dict(ok=True, note=f'{verb} still running after {wait:g} s')
    out.seek(0); words = out.read()[-4000:].decode('utf-8', 'replace').strip(); out.close()
    if code: return 502, dict(ok=False, error=f'{m["name"]} {verb} exited {code}' + (f': {words[-300:]}' if words else ''), output=words)
    return 200, dict(ok=True, output=words)
