#!/usr/bin/env python3
"""The extension protocol (localmodels/extensions.py, GUIDE.md "Extensions"): a manifest is read and refused as the
protocol says, a menu's actions stay on the extension's own origin and its own verbs, register and unregister write and
remove only its file, `localmodels ext check` reads a served menu, and the dashboard's launcher lists them and runs only a
listed verb, for a request from the dashboard itself. A made-up extension serves its menu from a temporary port; the state
folder is the sandbox's. `localmodels check` runs it; so can you: python3 tests/test_extensions.py
"""
import contextlib, http.server, io, json, os, shutil, socket, sys, tempfile, threading, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _sandbox  # noqa: E402  (first: the state folder, port, LM Studio and lms point into a temporary folder)
from localmodels import cli, extensions as ex  # noqa: E402

ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note and not good else ''))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


PORT = free_port()
BASE = f'http://localhost:{PORT}'
MENU = {'menu': 1, 'summary': '3 of 8 items ready',
        'alerts': [{'title': 'Something is due tomorrow', 'action': {'open': '/due'}}],
        'lines': [{'title': 'A status line'},
                  {'title': 'Open the report', 'action': {'open': '/report'}, 'indent': 1},
                  {'title': 'Export it', 'action': {'post': '/api/export', 'reveal': True}, 'indent': 1},
                  {'title': 'Leave a note…', 'action': {'post': '/api/note', 'body': {'via': 'menu'},
                                                         'input': {'prompt': 'A note', 'field': 'text', 'placeholder': 'the note'}}},
                  {'title': 'Items', 'children': [{'title': 'First item', 'action': {'open': '/items/1'}}]},
                  {'title': 'Restart it', 'action': {'run': 'restart', 'confirm': 'Restart the example?'}}],
        'events': [{'key': 'done:42', 'title': 'Job 42 finished', 'body': 'all good'}]}
SERVED = {'menu': MENU}


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path in ('/same', '/different'):
            self.send_response(302)
            self.send_header('Location', '/api/menu' if self.path == '/same' else f'http://127.0.0.1:{PORT}/api/menu')
            self.end_headers(); return
        b = json.dumps(SERVED['menu']).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)


srv = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def manifest(**kw):
    m = {'extension': 1, 'id': 'example.first', 'name': 'Example Tool', 'title': 'First Project', 'version': '1.0.0', 'maker': 'Someone',
         'menu': f'{BASE}/api/menu', 'open': BASE, 'token': '/tmp/example.token', 'token_header': 'X-Example-Token',
         'run': {'command': ['/usr/bin/true', 'example'], 'env': {'EXAMPLE_PROJECT': '/tmp/first'}, 'verbs': ['start', 'restart']}}
    m.update(kw)
    return {k: v for k, v in m.items() if v is not None}


print('extensions: manifests, menus, actions')

t('the folder is the sandbox\'s, never the live state', ex.DIR.startswith(os.environ['LOCALMODELS_STATE']), ex.DIR)
t('a sound manifest has no problems', ex.validate(manifest()) == [], ex.validate(manifest()))
fixtures = os.path.join(os.path.dirname(ex.__file__), '..', 'app', 'tests', 'manifest-fixtures.json')
for example in json.load(open(fixtures)):
    m = dict(extension=1, id='fixture', name='Fixture', menu='http://localhost/menu')
    m.update(example['fields'])
    t('shared manifest: ' + example['name'], (not ex.validate(m)) == example['valid'])
for name, m, word in [('another protocol version', manifest(extension=2), '"extension"'),
                      ('an id with capitals', manifest(id='Example'), '"id"'),
                      ('no name', manifest(name=''), '"name"'),
                      ('a menu on another machine', manifest(menu='http://example.com/menu'), '"menu"'),
                      ('a menu over https', manifest(menu=f'https://localhost:{PORT}/menu'), '"menu"'),
                      ('a page on another origin', manifest(open='http://localhost:1/'), '"open"'),
                      ('a token by relative path', manifest(token='page.token'), '"token"'),
                      ('a token header that is not X-', manifest(token_header='Authorization'), '"token_header"'),
                      ('a command by relative path', manifest(run={'command': ['tool'], 'verbs': ['start']}), '"run.command"'),
                      ('a run with no verbs', manifest(run={'command': ['/usr/bin/true'], 'verbs': []}), '"run.verbs"'),
                      ('a verb that is not a word', manifest(run={'command': ['/usr/bin/true'], 'verbs': ['start; rm']}), '"run.verbs"'),
                      ('an env with a number', manifest(run={'command': ['/usr/bin/true'], 'env': {'N': 1}, 'verbs': ['start']}), '"run.env"')]:
    probs = ex.validate(m)
    t(f'refused: {name}', any(word in x for x in probs), probs)

try: ex.register(manifest(menu='http://example.com/x')); t('register refuses an unsound manifest', False)
except ValueError: t('register refuses an unsound manifest, and writes nothing', not os.path.exists(ex.path('example.first')))
p = ex.register(manifest())
t('register writes the manifest under its id', os.path.exists(p) and json.load(open(p))['name'] == 'Example Tool', p)
found = ex.load()
t('load reads it back, sound', len(found) == 1 and found[0]['problems'] == [] and found[0]['manifest']['id'] == 'example.first', found)
open(os.path.join(ex.DIR, 'other.json'), 'w').write(json.dumps(manifest(id='not.other')))
open(os.path.join(ex.DIR, 'broken.json'), 'w').write('{not json')
by = {x['file']: x for x in ex.load()}
t('a file named apart from its id is a problem', any('its id is not.other' in x for x in by['other.json']['problems']), by['other.json']['problems'])
t('an unreadable file is a problem, not a crash', by['broken.json']['manifest'] is None and by['broken.json']['problems'], by['broken.json'])
t('a mismatched manifest cannot run a verb', ex.run('not.other', 'start')[0] == 409)
open(os.path.join(ex.DIR, 'other.json'), 'w').write(json.dumps(manifest()))
t('duplicate ids are absent from the launcher and cannot run either command', not ex.view() and ex.run('example.first', 'start')[0] == 409)
os.remove(os.path.join(ex.DIR, 'other.json')); os.remove(os.path.join(ex.DIR, 'broken.json'))

m = manifest()
t('a sound menu has no problems', ex.validate_menu(MENU, m) == [], ex.validate_menu(MENU, m))
for name, menu, word in [('another menu version', dict(MENU, menu=2), '"menu"'),
                         ('a line without a title', dict(MENU, lines=[{'action': {'open': '/'}}]), 'no "title"'),
                         ('a page on another origin', dict(MENU, lines=[{'title': 'x', 'action': {'open': 'http://example.com/'}}]), '"open"'),
                         ('a protocol-relative page', dict(MENU, lines=[{'title': 'x', 'action': {'open': '//example.com/'}}]), '"open"'),
                         ('a POST to a full URL', dict(MENU, lines=[{'title': 'x', 'action': {'post': 'http://localhost:1/api'}}]), '"post"'),
                         ('a verb the manifest does not list', dict(MENU, lines=[{'title': 'x', 'action': {'run': 'uninstall'}}]), '"run"'),
                         ('two kinds in one action', dict(MENU, lines=[{'title': 'x', 'action': {'open': '/', 'run': 'start'}}]), 'exactly one'),
                         ('an input without a field', dict(MENU, lines=[{'title': 'x', 'action': {'post': '/n', 'input': {'prompt': 'p'}}}]), '"input"'),
                         ('a body with a number', dict(MENU, lines=[{'title': 'x', 'action': {'post': '/n', 'body': {'n': 1}}}]), '"body"'),
                         ('submenus four deep', dict(MENU, lines=[{'title': 'a', 'children': [{'title': 'b', 'children': [{'title': 'c', 'children': [{'title': 'd'}]}]}]}]), 'nest'),
                         ('too many lines', dict(MENU, lines=[{'title': f'l{i}'} for i in range(ex.LINES + 1)]), 'at most'),
                         ('an event without a key', dict(MENU, events=[{'title': 'x'}]), 'events[0]')]:
    probs = ex.validate_menu(menu, m)
    t(f'menu refused: {name}', any(word in x for x in probs), probs)

menu, why = ex.fetch_menu(m)
t('the served menu is read', menu == MENU and why is None, why)
t('the menu follows a redirect on its own origin', ex.fetch_menu(manifest(menu=BASE + '/same')) == (MENU, None))
menu, why = ex.fetch_menu(manifest(menu=BASE + '/different'))
t('the menu reader refuses an origin change before the next request', menu is None and 'redirect to another origin refused' in (why or ''))
gone, why = ex.fetch_menu(manifest(menu=f'http://localhost:{free_port()}/api/menu'), timeout=1)
t('a menu that does not answer says why', gone is None and why, why)


def run_cli(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out): rc = cli.ext_cmd(args)
    return rc, out.getvalue()


rc, text = run_cli(['check'])
t('ext check: a sound extension passes', rc == 0 and 'all sound' in text and 'Example Tool — First Project 1.0.0: answering' in text, text)
SERVED['menu'] = dict(MENU, lines=[{'title': 'x', 'action': {'run': 'uninstall'}}])
rc, text = run_cli(['check'])
t('ext check: a menu asking for an unlisted verb fails it', rc == 1 and 'not one of the manifest\'s verbs' in text, text)
SERVED['menu'] = MENU
rc, text = run_cli([])
t('ext: the list exits 0 whatever it finds', rc == 0 and 'answering' in text, text)

# ---------------------------------------------------------------- the dashboard's launcher, through a gateway of its own
print("extensions: the dashboard's launcher (GET /hub/extensions, POST /hub/extensions/run)")
from localmodels import gateway, names, web  # noqa: E402
door = gateway.Server(('127.0.0.1', 0), gateway.Door); threading.Thread(target=door.serve_forever, daemon=True).start()
gateway.PORT = door.server_address[1]
HUB = f'http://localhost:{gateway.PORT}'


def hub(path, body=None, headers=None):
    req = urllib.request.Request(HUB + path, data=None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode()),
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r: return r.status, json.load(r)
    except urllib.error.HTTPError as e: return e.code, json.load(e)


def listed():
    web._cache.pop('extensions', None)
    return {x['id']: x for x in hub('/hub/extensions')[1]['extensions']}


box = tempfile.mkdtemp(prefix='localmodels-exttest-')
said = os.path.join(box, 'said')
script = 'import os, sys; open(os.environ["EXAMPLE_OUT"], "w").write(sys.argv[1] + " " + str(os.getsid(0) == os.getpid()))'
ex.register(manifest(run={'command': [sys.executable, '-c', script], 'env': {'EXAMPLE_OUT': said}, 'verbs': ['start', 'restart']}))
ex.register(manifest(id='example.second', name='Other Tool', title='', menu=f'http://localhost:{free_port()}/api/menu', open=None,
                     run={'command': [sys.executable, '-c', 'import sys; print("cannot start"); sys.exit(3)'], 'verbs': ['start']}))
got = listed()
first, second = got.get('example.first') or {}, got.get('example.second') or {}
t('each extension listed, its menu asked: answering, its page, its summary and alerts, its verbs',
  first.get('answering') is True and first.get('open') == BASE and first.get('summary') == '3 of 8 items ready'
  and first.get('alerts') == ['Something is due tomorrow'] and first.get('verbs') == ['start', 'restart'] and first.get('label') == 'Example Tool — First Project', first)
t('one not answering says so, and why', second.get('answering') is False and second.get('why') and second.get('label') == 'Other Tool', second)
names.set_present(5, [])
web._cache.pop('extensions', None)
t('screenshot mode lists none (an extension\'s name is another program\'s)', hub('/hub/extensions')[1]['extensions'] == [])
names.present_off()
code, out = hub('/hub/extensions/run', {'id': 'example.first', 'verb': 'start'})
ran = open(said).read() if os.path.exists(said) else ''
t('Start runs its command with the verb, its environment added, in a session of its own (it outlives the gateway)',
  code == 200 and out.get('ok') is True and ran == 'start True', (code, out, ran))
code, out = hub('/hub/extensions/run', {'id': 'example.first', 'verb': 'uninstall'})
t('a verb its manifest does not list: refused (403), nothing run', code == 403 and 'not one of' in out.get('error', ''), (code, out))
code, out = hub('/hub/extensions/run', {'id': 'example.none', 'verb': 'start'})
t('an extension with no manifest: 404', code == 404, (code, out))
code, out = hub('/hub/extensions/run', b'[1, 2]')
t('a body that is not {"id", "verb"}: 400', code == 400, (code, out))
code, out = hub('/hub/extensions/run', {'id': 'example.second', 'verb': 'start'})
t('a start that fails: 502 with its exit code and its words', code == 502 and 'exited 3' in out.get('error', '') and 'cannot start' in out.get('output', ''), (code, out))
os.remove(said)
code, out = hub('/hub/extensions/run', {'id': 'example.first', 'verb': 'start'}, {'Sec-Fetch-Site': 'cross-site', 'Origin': 'http://example.com'})
t('a web page from another site cannot run it (403), and nothing ran', code == 403 and not os.path.exists(said), (code, out))
code, out = hub('/hub/extensions/run', {'id': 'example.first', 'verb': 'start'}, {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'cors'})
t('nor one on another local port (same-site)', code == 403 and not os.path.exists(said), (code, out))
door.shutdown()
ex.unregister('example.second')
shutil.rmtree(box, ignore_errors=True)

t('unregister removes the file', ex.unregister('example.first') and not os.path.exists(ex.path('example.first')))
t('unregister again: nothing to remove', ex.unregister('example.first') is False)
rc, text = run_cli([])
t('ext with none: says how a program joins', rc == 0 and 'no extensions' in text, text)
t('no hub path points into the live state folder', not _sandbox.stray(), _sandbox.stray())

srv.shutdown()
print(f'extensions tests: {sum(ok)} of {len(ok)} pass')
sys.exit(0 if all(ok) else 1)
