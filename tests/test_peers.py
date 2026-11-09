#!/usr/bin/env python3
"""Peers: other Macs' hubs as more copies of a family (localmodels/peers.py and the door), tested with two doors and a
pretend LM Studio behind each, on random ports: this Mac's door in this process, the peer's door as its own process with
its own state folder. Local slots first, then the peer's; never more in flight to the peer than it says it has free; the
project, session, kind, step and run forwarded; the token refused when wrong or missing; web pages, swaps and the
dashboard refused over the link; a peer in maintenance or gone skipped, and a request it could not take sent again here;
the network checks (the interface, the route, link-local addresses, macOS's Local Network permission); a model's identity
across Macs (its variant, size and definition); LM Link's rows left out of the listing; and the peer's keeper, which
says plainly when LM Studio's guardrail refuses a load, does not try again until something changed, and never recycles
a lone copy it has not shown it can load again. Nothing real is loaded or unloaded. `localmodels check` runs it; so can
you: python3 tests/test_peers.py
"""
import collections, errno, http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request  # noqa: E401

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOME)
import _sandbox  # noqa: E402  (first: the state folder, port, LM Studio and lms point into a temporary folder)
from localmodels import crashes, gateway, llmshare, models, peers, recycle

F, F2 = 'test/family', 'test/family:2'
TOKEN = 'a3f1' * 16
ok = []


def t(name, good, note=''):
    ok.append(bool(good)); print(f"  {'ok  ' if good else 'FAIL'} {name}" + (f': {note}' if note else ''))


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


def fake(label):
    """A pretend LM Studio: answers by a plan ('ok', 'slow', 'sse'); remembers what it was asked and the headers it got,
    and how many requests of each tag group (the tag's first letter) it answered at once at most."""
    class Fake(http.server.BaseHTTPRequestHandler):
        plan, seen, heads, slow = [], [], [], 2.0
        now, most = collections.Counter(), collections.Counter()
        lock = threading.Lock()
        name = label

        def log_message(self, *a): pass

        def do_GET(self):
            out = json.dumps(dict(data=[])).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out)))
            self.end_headers(); self.wfile.write(out)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            tag = str(body.get('tag') or '')
            with Fake.lock:
                what = Fake.plan.pop(0) if Fake.plan else 'ok'
                Fake.seen.append((body.get('model'), tag, what))
                Fake.heads.append({k.lower(): v for k, v in self.headers.items()})
                Fake.now[tag[:1]] += 1; Fake.most[tag[:1]] = max(Fake.most[tag[:1]], Fake.now[tag[:1]])
            try:
                if what == 'slow': time.sleep(Fake.slow)
                if what == 'sse':
                    self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                    for ev in (dict(model=body.get('model'), choices=[dict(delta=dict(content='O'))]),
                               dict(model=body.get('model'), choices=[dict(delta=dict(content='K'))], usage=dict(prompt_tokens=5, completion_tokens=2))):
                        self.wfile.write(b'data: ' + json.dumps(ev).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n'); self.close_connection = True
                    return
                out = json.dumps(dict(model=body.get('model'), choices=[dict(message=dict(content=f'{label} {tag}'))],
                                      usage=dict(prompt_tokens=3, completion_tokens=2))).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out)))
                self.end_headers(); self.wfile.write(out)
            except OSError: pass
            finally:
                with Fake.lock: Fake.now[tag[:1]] -= 1
    return Fake


def lmstudio_home(root, yaml_text):
    """An ~/.lmstudio with the family's definition (model.yaml and manifest.json) and a pretend lms that prints ps.json."""
    d = os.path.join(root, 'hub', 'models', 'test', 'family')
    os.makedirs(d); os.makedirs(os.path.join(root, 'bin'))
    open(os.path.join(d, 'model.yaml'), 'w').write(yaml_text)
    open(os.path.join(d, 'manifest.json'), 'w').write('{"type": "model", "name": "family", "revision": 1}')
    lms = os.path.join(root, 'bin', 'lms')
    open(lms, 'w').write('#!/bin/sh\ncat "$(dirname "$0")/../ps.json"\n'); os.chmod(lms, 0o755)
    return lms


def ps_row(ident, parallel=2, variant='test/family@8bit', size=1000, device=None):
    return dict(identifier=ident, modelKey=F, type='llm', status='idle', sizeBytes=size, contextLength=4096, parallel=parallel,
                selectedVariant=variant, format='safetensors', quantization=dict(name=variant.rsplit('@', 1)[-1], bits=8), deviceIdentifier=device)


# ---------------------------------------------------------------- the rules that need no door
def test_units(box):
    print('peers: the network checks, a model\'s identity across Macs, the copy ids, LM Link\'s rows')
    ifc = ('lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n'
           'en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255\n'
           'en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n\tinet 169.254.12.34 netmask 0xffff0000 broadcast 169.254.255.255\n'
           'bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n\tinet6 fe80::1c2b:3a4d:5e6f:7081%bridge0 prefixlen 64 scopeid 0x10\n'
           '\tinet 192.168.213.2 netmask 0xffffff00 broadcast 192.168.213.255\n')
    got = peers.interfaces(ifc)
    t('ifconfig read: each interface with its addresses and prefix lengths',
      got.get('bridge0') == [('192.168.213.2', 24)] and got.get('en7') == [('169.254.12.34', 16)] and got.get('lo0') == [('127.0.0.1', 8)], got)
    t('the interface that holds an address', peers.interface_of('192.168.213.2', ifc) == 'bridge0' and peers.interface_of('10.9.9.9', ifc) is None)
    hw = ('\nHardware Port: Wi-Fi\nDevice: en0\nEthernet Address: aa:bb\n\nHardware Port: Thunderbolt Bridge\nDevice: bridge0\nEthernet Address: cc:dd\n'
          '\nHardware Port: Ethernet Adapter (en7)\nDevice: en7\nEthernet Address: ee:ff\n')
    t('networksetup read: the name of each port', peers.ports(hw) == {'en0': 'Wi-Fi', 'bridge0': 'Thunderbolt Bridge', 'en7': 'Ethernet Adapter (en7)'}, peers.ports(hw))
    route = '   route to: 192.168.213.2\ndestination: 192.168.213.0\n       mask: 255.255.255.0\n  interface: bridge0\n      flags: <UP,DONE,CLONING>\n'
    t('route read: the interface the route to a peer takes', peers.route_interface(route) == 'bridge0' and peers.route_interface('route: writing to routing socket: not in table') is None)
    w = peers.address_words('169.254.12.34')
    t('a link-local address is warned about (it changes after a restart and may take the slow interface)', w and 'static address' in w, w)
    t('a fixed private address is fine', peers.address_words('192.168.213.2') is None)
    w = peers.words(dict(name='m4', state='up', version='0.9.0', families={}))
    t("a peer on another release is named with both versions", '0.9.0' in w and peers.VERSION in w, w)
    t("a peer on the same release: no word about it", 'release' not in peers.words(dict(name='m4', state='up', version=peers.VERSION, families={})))
    w = peers.route_words('en7', {'en7': 'Ethernet Adapter (en7)', 'bridge0': 'Thunderbolt Bridge'})
    t('a route that does not take the Thunderbolt Bridge is said, naming the port', w and 'Thunderbolt Bridge' in w and 'en7' in w, w)
    t('a route over the Thunderbolt Bridge is fine', peers.route_words('bridge0', {'bridge0': 'Thunderbolt Bridge'}) is None)
    w = peers.reach_words(OSError(errno.EHOSTUNREACH, 'No route to host'))
    t("EHOSTUNREACH is read as macOS's Local Network permission, with where to turn it on", 'Local Network' in w and 'Privacy' in w, w)
    t('a refused connection is read as the peer gateway not listening', 'ECONNREFUSED' in peers.reach_words(ConnectionRefusedError(errno.ECONNREFUSED, 'Connection refused')))
    t("a peer on a subnet of this Mac's interfaces is reached only through that interface, from this Mac's address there",
      peers.on_link('192.168.213.1', ifc) == ('bridge0', '192.168.213.2', '192.168.213.1'), peers.on_link('192.168.213.1', ifc))
    out = ifc.replace('\tinet 192.168.213.2 netmask 0xffffff00 broadcast 192.168.213.255\n', '')    # the cable out: the bridge has no IPv4 address
    t('with the cable out the peer is on no subnet here: no interface, and the words say nothing is sent',
      peers.on_link('192.168.213.1', out)[0] is None and 'nothing is sent' in peers.on_link('192.168.213.1', out)[1], peers.on_link('192.168.213.1', out))
    t('the link listener is bound to one address of this Mac: an unspecified address (every interface) or a name is refused',
      peers.serve_problem('0.0.0.0') and peers.serve_problem('::') and peers.serve_problem('mac.local') and peers.serve_problem('192.168.213.1') is None)
    t('a loopback peer (a sandbox) is on lo0; an IPv6 link-local address names its interface',
      peers.on_link('127.0.0.1', ifc)[0] == 'lo0' and peers.on_link('fe80::2%bridge0', ifc)[0] == 'bridge0')

    mine = dict(variant='test/family@8bit', bytes=1000, format='safetensors', definition='abc')
    t('the same variant, size, format and definition: the same model', peers.match(mine, dict(mine))[0])
    good, why = peers.match(mine, dict(mine, variant='test/family@4bit'))
    t('another quantization is another model, and the words say which', not good and '4bit' in why and '8bit' in why, why)
    good, why = peers.match(mine, dict(mine, definition=None))
    t('weights copied without the definition (model.yaml): not the same model, and the words say what to copy', not good and 'model.yaml' in why, why)
    good, why = peers.match(None, mine)
    t("this Mac has never seen the family loaded: the peer's copy cannot be checked, so it is not used", not good and 'check' in why, why)
    good, why = peers.match(None, mine, pinned='test/family@8bit')
    t('...unless the settings pin the variant, and the peer has it', good, why)

    c = peers.cid(F, 'macbook')
    t('a copy on a peer is named "<family> on <peer>": never a local id, and its family is the family',
      c == 'test/family on macbook' and llmshare.family(c) == F and llmshare.family(peers.cid(F2, 'macbook')) == F and peers.split(c) == (F, 'macbook') and peers.split(F2) == (F2, None))
    t('a peer name is a short plain word', peers.NAME.match('macbook') and not peers.NAME.match('mac book') and not peers.NAME.match('a/b'))

    lms = lmstudio_home(os.path.join(box, 'units-lmstudio'), 'model: test/family\n')
    open(os.path.join(box, 'units-lmstudio', 'ps.json'), 'w').write(json.dumps([ps_row(F), ps_row(F2, device='other-mac')]))
    was = llmshare.LMS; llmshare.LMS = lms
    rows, good = llmshare._lms_ps()
    llmshare.LMS = was
    t("a model LM Link lists from another Mac (deviceIdentifier set) is left out of this Mac's listing", good and [r['id'] for r in rows] == [F], rows)
    t('the listing keeps what names the model: variant, bytes, format', rows and rows[0].get('variant') == 'test/family@8bit' and rows[0].get('bytes') == 1000 and rows[0].get('format') == 'safetensors', rows[:1])
    home_was = peers.LMSTUDIO_HOME; peers.LMSTUDIO_HOME = os.path.join(box, 'units-lmstudio')
    fp = peers.fingerprint(rows[0])
    peers.LMSTUDIO_HOME = home_was
    t("a family's fingerprint: its variant, size, format and a digest of its definition", fp['variant'] == 'test/family@8bit' and fp['bytes'] == 1000 and fp['definition'], fp)

    print('peers: how many a front may send to a peer, from what the peer says')
    fam = dict(slots=4, busy=3, answering={'front': 1})
    t('4 slots, 3 busy of which 1 from this Mac: this Mac may have 2 in flight there', peers.capacity_for(fam, 'front') == 2)
    t('4 slots, all busy with its own work: none', peers.capacity_for(dict(slots=4, busy=4, answering={}), 'front') == 0)
    t('a garbled report counts as none', peers.capacity_for(dict(slots='x'), 'front') == 0 and peers.capacity_for(None, 'front') == 0)


def test_keeper(box):
    print("the peer's keeper: LM Studio's guardrail refusing a load, said plainly, not retried blindly; no lone copy recycled without proof")
    from localmodels import llmshare as L
    out = ('Error: Model loading was stopped due to insufficient system resources. Under the current settings, this model requires '
           'approximately 39.4 GB of memory. You can adjust the model loading guardrails in settings.')
    w = models.refused(out)
    t("LM Studio's guardrail words are recognised, and the words say what a person can do", w and 'guardrail' in w and 'Load anyway' in w, w)
    t('another failure is not taken for the guardrail', models.refused('Error: model not found: x') is None)
    real = models._lms, L.settings, L.loaded, L.read_ok, L.busy_slots, L.api_models, models.free_gb, models.ensure_server, L.SWAP_LOCK, models.conditions
    calls = []
    models._lms = lambda *a, **k: (calls.append(a), (False, out) if a[0] == 'load' else (True, ''))[1]
    fams = {F: dict(key=F, copies=1, context=4096, parallel=2, recycle=dict(tokens=1000, soft=500, before_batch=0.5))}
    L.settings = lambda: dict(families=fams, batch_at_once=2, headroom_gb=1)
    L.read_ok, L.busy_slots, L.api_models = (lambda: True), (lambda: {}), (lambda timeout=3: {})
    models.free_gb, models.ensure_server = (lambda: 100.0), (lambda *a, **k: True)
    L.SWAP_LOCK = os.path.join(box, 'swap.lock')
    cond = {'v': (1.0, 0)}
    models.conditions = lambda: cond['v']
    with crashes._state() as st:
        st.clear(); st['down'] = {F: dict(crashed=time.time() - 60, models=[F], state='loading again', keeper=True, gone=True)}
        st['seen'] = {F: dict(key=F, context=4096, parallel=2, size='1 GB', loaded=False, since=time.time() - 600, at=time.time() - 60)}
    L.loaded = lambda fresh=False, force=False: []
    w1 = models.keep()
    t('a crashed model whose reload LM Studio refuses: the keeper says why in words a person can act on, once', any('Load anyway' in x for x in w1) and len([c for c in calls if c[0] == 'load']) == 1, w1)
    blocked = crashes.load_blocked(F, cond['v'])
    t('the refusal is recorded with what it was refused under', blocked and 'guardrail' in blocked, blocked)
    with crashes._state() as st: st['down'][F]['next_try'] = 0      # the usual pause after a failed load has passed
    calls.clear(); w2 = models.keep()
    t('nothing changed since: no load is tried again', not [c for c in calls if c[0] == 'load'], (calls, w2))
    cond['v'] = (2.0, 0)                                            # the person changed LM Studio's guardrail setting
    with crashes._state() as st: st['down'][F]['next_try'] = 0
    calls.clear(); models.keep()
    t('the guardrail setting changed: one load is tried', len([c for c in calls if c[0] == 'load']) == 1, calls)
    models._lms = lambda *a, **k: (calls.append(a), (True, 'loaded'))[1]
    with crashes._state() as st: st['down'][F]['next_try'] = 0
    cond['v'] = (3.0, 0); calls.clear(); models.keep()
    t('a load that works clears the refusal', crashes.load_blocked(F, cond['v']) is None and crashes.reload_proven(F), crashes._read(fresh=True).get('loads'))

    with crashes._state() as st:
        st['down'] = {}; st['loads'] = {}; st['recycle'] = dict(job=None, last={}, fresh={}, retry_after=0)
        st['seen'] = {F: dict(key=F, context=4096, parallel=2, size='1 GB', loaded=True, since=time.time() - 3600, at=time.time())}
    rows = [dict(id=F, key=F, state='IDLE', size='1 GB', context=4096, parallel=2)]
    with open(L.REQUESTS, 'a') as f:
        for k in range(5): f.write(json.dumps(dict(at=time.time() - 600 + k, project='p', kind='batch', model=F, copy=F, status=200, seconds=1, waited=0, prompt_tokens=400, completion_tokens=100)) + '\n')
    recycle._counts.update(at=0.0, by={})
    w = recycle.plan(rows, {})
    t('a lone copy past its band, never loaded by the hub here: not recycled (an unload it might not undo), and said why',
      w is None and recycle.job() is None and 'not' in (recycle.held_back().get(F) or '') and 'recycle threshold' in (recycle.held_back().get(F) or ''), (w, recycle.held_back()))
    open(os.path.join(peers.LMSTUDIO_HOME, 'settings.json'), 'w').write(json.dumps(dict(modelLoadingGuardrails=dict(mode='off'))))
    recycle._counts.update(at=0.0, by={})
    w = recycle.plan(rows, {})
    t("LM Studio's guardrail Off (it cannot refuse the load that follows): the lone copy is recycled", w and recycle.job() and recycle.job()['copy'] == F, w)
    os.remove(os.path.join(peers.LMSTUDIO_HOME, 'settings.json'))
    with crashes._state() as st: st['recycle'] = dict(job=None, last={}, fresh={}, retry_after=0)
    crashes.load_ok(F)
    recycle._counts.update(at=0.0, by={})
    w = recycle.plan(rows, {})
    t('once the hub has loaded it here, the lone copy is recycled as usual', w and recycle.job() and recycle.job()['copy'] == F, w)
    with crashes._state() as st: st['recycle'] = dict(job=None, last={}, fresh={}, retry_after=0); st['loads'] = {}
    os.remove(L.REQUESTS); recycle._counts.update(at=0.0, by={})
    models._lms, L.settings, L.loaded, L.read_ok, L.busy_slots, L.api_models, models.free_gb, models.ensure_server, L.SWAP_LOCK, models.conditions = real

    print("the peer's keeper: the GPU's memory limit against the model's size")
    w = models.gpu_words(dict(limit_gb=36.0, set=False, total_gb=48.0), 37.75)
    t('a model bigger than the GPU may hold: the command to raise it, and that it resets at restart',
      w and 'sudo sysctl iogpu.wired_limit_mb=40960' in w and 'restart' in w, w)
    t('a model that fits: nothing to say', models.gpu_words(dict(limit_gb=96.0, set=False, total_gb=128.0), 37.75) is None)
    w = models.gpu_words(dict(limit_gb=40.0, set=True, total_gb=48.0, boot=False), 37.75)
    t('raised by hand but not at boot: said, since it resets at the next restart', w and 'resets' in w and 'localmodels peer gpu-limit' in w, w)
    t('raised at boot by the hub\'s LaunchDaemon: nothing to say', models.gpu_words(dict(limit_gb=40.0, set=True, total_gb=48.0, boot=True), 37.75) is None)
    w = models.guardrail_words(dict(limit_gb=40.0, total_gb=48.0), 35.2, 'relaxed')
    t("a model that nearly fills what the GPU may hold, with LM Studio's guardrail on: said before any load is refused", w and 'Off' in w, w)
    t('...not with the guardrail Off, and not for a model with room to spare', models.guardrail_words(dict(limit_gb=40.0, total_gb=48.0), 35.2, 'off') is None
      and models.guardrail_words(dict(limit_gb=96.0, total_gb=128.0), 35.2, 'high') is None)

    print("a dedicated Mac: a family loaded when the door starts (load_at_start), once, after the GPU's limit, with three tries")
    real = models._lms, L.settings, L.loaded, L.read_ok, L.busy_slots, L.api_models, models.free_gb, models.ensure_server, L.SWAP_LOCK, models.conditions, models.gpu, models.downloaded, models.LOADER, models.server_up
    calls, state = [], {'load': (False, 'Error: something else went wrong'), 'rows': []}
    models._lms = lambda *a, **k: (calls.append(a), state['load'] if a[0] == 'load' else (True, ''))[1]
    fams = {F: dict(key=F, copies=1, context=65536, parallel=4, recycle=False, load_at_start=True)}
    L.settings = lambda: dict(families=fams, batch_at_once=4, headroom_gb=4)
    L.read_ok, L.busy_slots, L.api_models = (lambda: True), (lambda: {}), (lambda timeout=3: {})
    L.loaded = lambda fresh=False, force=False: list(state['rows'])
    models.free_gb, models.ensure_server, models.conditions = (lambda: 45.0), (lambda *a, **k: True), (lambda: (1.0, 40960))
    models.downloaded = lambda: [dict(modelKey=F, sizeBytes=int(37.75 * 2**30), type='llm')]
    models.LOADER = os.path.join(box, 'no-such-agent.plist')
    L.SWAP_LOCK = os.path.join(box, 'swap.lock')
    with crashes._state() as st: st.clear()
    models._start.clear()
    gpu = {'v': dict(limit_gb=36.0, set=False, total_gb=48.0, boot=False)}
    models.gpu = lambda: gpu['v']
    w = models.keep()
    t('the GPU may not hold it yet: no load, and the words say it waits for the limit', not [c for c in calls if c[0] == 'load'] and any('GPU' in x for x in w), w)
    gpu['v'] = dict(limit_gb=40.0, set=True, total_gb=48.0, boot=True)
    w = models.keep()
    t('the limit is up: the first copy is loaded, as the family says (identifier, context, slots)',
      [c for c in calls if c[0] == 'load'] == [('load', F, '--identifier', F, '-c', '65536', '--parallel', '4', '-y')], calls)
    calls.clear(); models.keep()
    t('a load that failed is tried again only after 30 s', not [c for c in calls if c[0] == 'load'], calls)
    for _ in range(3):
        models._start[F]['next'] = 0; models.keep()
    w = models.keep()
    t('three tries in all, then it stops for this start and says how to load it by hand',
      len([c for c in calls if c[0] == 'load']) == 2 and models._start[F].get('done') and 'localmodels use' in models._start[F].get('words', ''), (calls, models._start[F]))
    models._start.clear(); calls.clear(); state['load'] = (True, 'Model loaded')
    models.keep(); state['rows'] = [dict(id=F, key=F, state='IDLE', size='37.75 GB', context=65536, parallel=4)]
    calls.clear(); models.keep()
    t('loaded at start once; with it loaded, nothing more', models._start[F].get('done') and not [c for c in calls if c[0] == 'load'], (calls, models._start[F]))
    state['rows'] = []; calls.clear(); models.keep()
    t('unloaded later in the same run (a person chose): not loaded again until the door starts again', not [c for c in calls if c[0] == 'load'], calls)
    models._start.clear(); open(os.path.join(box, 'agent.plist'), 'w').write('<plist/>'); models.LOADER = os.path.join(box, 'agent.plist')
    calls.clear(); w = models.keep()
    t('the login agent from before the hub is still installed: the keeper leaves loading to it (one loader at a time) and says so',
      not [c for c in calls if c[0] == 'load'] and any('load-at-login' in x for x in w), w)
    models.LOADER = os.path.join(box, 'no-such-agent.plist'); models._start.clear()
    L.read_ok, models.server_up = (lambda: False), (lambda timeout=3: False)
    calls.clear(); models.keep()
    t("LM Studio not running at all on a dedicated Mac: its daemon is started (never stopped), once", ('daemon', 'up') in calls, calls)
    calls.clear(); models.keep()
    t('...and not started twice in a row', ('daemon', 'up') not in calls, calls)
    models._lms, L.settings, L.loaded, L.read_ok, L.busy_slots, L.api_models, models.free_gb, models.ensure_server, L.SWAP_LOCK, models.conditions, models.gpu, models.downloaded, models.LOADER, models.server_up = real
    models._start.clear()
    with crashes._state() as st: st.clear()


def test_commands(box):
    print('localmodels peer: add, serve, remove; the token made once, mode 0600, never on the screen')
    import contextlib, io
    from localmodels import cli
    d = os.path.join(box, 'cmd'); os.makedirs(d)
    was = llmshare.MODELS, peers.TOKEN_FILE
    llmshare.MODELS, peers.TOKEN_FILE = os.path.join(d, 'models.json'), os.path.join(d, 'peer-token')
    json.dump(dict(families={F: dict(key=F, copies=2, context=4096, parallel=4)}, batch_at_once=8, headroom_gb=8), open(llmshare.MODELS, 'w'))

    def run(*a):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = cli.peer_cmd(list(a))
        return code, out.getvalue()
    try:
        code, out = run('add', 'macbook', '192.168.213.2')
        conf = json.load(open(llmshare.MODELS))
        tok = open(peers.TOKEN_FILE).read().strip()
        t('peer add writes the peer into the settings, beside what was there', code == 0 and conf.get('peers') == {'macbook': {'url': f'http://192.168.213.2:{llmshare.HUB_PORT}'}} and F in conf['families'], conf)
        t('...and makes the token: 64 hex characters, mode 0600', len(tok) == 64 and os.stat(peers.TOKEN_FILE).st_mode & 0o777 == 0o600, oct(os.stat(peers.TOKEN_FILE).st_mode))
        t('...and never prints it: the next steps copy it over SSH from its file', tok not in out and 'ssh <user>@192.168.213.2' in out and peers.TOKEN_FILE in out, out[-300:])
        code, out2 = run('add', 'macbook', '192.168.213.2')
        t('a second add keeps the token', open(peers.TOKEN_FILE).read().strip() == tok and 'existing' in out2)
        t('a peer name with a space is refused', run('add', 'mac book', '192.168.213.3')[0] == 2)
        t('an address that is not one is refused', run('add', 'other', 'ftp://x')[0] == 2)
        code, out = run('add', 'stray', '169.254.12.34')
        t('a link-local address is taken, with the warning', code == 0 and 'link-local' in out and any('link-local' in w for w in peers.problems()), out)
        run('remove', 'stray')
        real_iface, peers.interface_of = peers.interface_of, (lambda addr: None)   # whatever this Mac's own bridge holds
        try: code, out = run('serve', '192.168.213.2')
        finally: peers.interface_of = real_iface
        t('peer serve writes the address this Mac listens on, and says when no interface holds it yet',
          code == 0 and json.load(open(llmshare.MODELS)).get('serve') == {'address': '192.168.213.2'} and peers.serving() == {'address': '192.168.213.2', 'port': llmshare.HUB_PORT}
          and 'no interface' in out, out)
        t('peer serve with something that is not an address: refused', run('serve', 'bridge0')[0] == 2)
        code, out = run('serve', 'off')
        t('peer serve off: the door listens on this Mac only again', code == 0 and 'serve' not in json.load(open(llmshare.MODELS)) and peers.serving() is None)
        code, out = run('remove', 'macbook')
        t('peer remove takes it out', code == 0 and json.load(open(llmshare.MODELS)).get('peers') == {} and not peers.configured())
        t('the token is taken only with its exact value', peers.token_ok(tok) and not peers.token_ok(tok[:-1] + 'x') and not peers.token_ok('') and not peers.token_ok(None))
        os.chmod(peers.TOKEN_FILE, 0o644)
        t('a token file other users can read is not used, and the words say how to fix it',
          not peers.token_ok(tok) and 'chmod 600' in (peers.token_problem() or ''), peers.token_problem())
        os.chmod(peers.TOKEN_FILE, 0o600)

        import plistlib
        real_mem, real_daemon, real_launchctl = models.mem_gb, models.GPU_DAEMON, cli._launchctl
        models.mem_gb, models.GPU_DAEMON = (lambda: 48.0), os.path.join(d, 'system', 'com.local-models.gpu-limit.plist')
        code, out = run('gpu-limit')
        mine = os.path.join(os.path.dirname(peers.TOKEN_FILE), 'com.local-models.gpu-limit.plist')
        pl = plistlib.load(open(mine, 'rb')) if os.path.exists(mine) else {}
        t('peer gpu-limit writes the boot job for a 48 GB Mac: com.local-models.gpu-limit, sysctl iogpu.wired_limit_mb=40960 at load',
          code == 0 and pl == dict(Label='com.local-models.gpu-limit', ProgramArguments=['/usr/sbin/sysctl', 'iogpu.wired_limit_mb=40960'], RunAtLoad=True), pl)
        t('...and prints the sudo lines for the person, running none of them', 'sudo launchctl bootstrap system /Library/LaunchDaemons/com.local-models.gpu-limit.plist' in out and 'sudo cp' in out, out[-400:])
        os.makedirs(os.path.dirname(models.GPU_DAEMON)); shutil.copy(mine, models.GPU_DAEMON)
        code, out = run('gpu-limit')
        t('the same job already installed (by hand or from these lines): adopted, nothing to do', code == 0 and 'installed' in out and 'sudo' not in out, out)
        plistlib.dump(dict(Label='com.local-models.gpu-limit', ProgramArguments=['/usr/sbin/sysctl', 'iogpu.wired_limit_mb=32768'], RunAtLoad=True), open(models.GPU_DAEMON, 'wb'))
        code, out = run('gpu-limit')
        t('an installed job with another value: said, with the lines that replace it', code == 1 and '32768' in out and '40960' in out and 'sudo' in out, out[-300:])
        models.mem_gb, models.GPU_DAEMON = real_mem, real_daemon

        agent = os.path.join(d, 'com.local-models.load-at-login.plist'); open(agent, 'w').write('<plist/>')
        was_loader, launched = models.LOADER, []
        models.LOADER, cli._launchctl = agent, (lambda *a: (launched.append(a), subprocess.CompletedProcess(a, 0, '', ''))[1])
        conf = json.load(open(llmshare.MODELS))
        w = cli.retire_loader()
        t('with no family loaded at start, the login loader stays (it is the only loader)', os.path.exists(agent) and not launched and 'stays' in w, w)
        conf['families'][F]['load_at_start'] = True; json.dump(conf, open(llmshare.MODELS, 'w'))
        w = cli.retire_loader(dry=True)
        t('install --dry says it would retire it, and does not', os.path.exists(agent) and not launched and 'would' in w, w)
        w = cli.retire_loader()
        t("with a family loaded at start, install retires the login loader: the keeper loads it from now on (one loader)",
          not os.path.exists(agent) and launched and launched[0][0] == 'bootout' and 'com.local-models.load-at-login' in launched[0][1], (w, launched))
        models.LOADER, cli._launchctl = was_loader, real_launchctl
    finally: llmshare.MODELS, peers.TOKEN_FILE = was


def fake_lmstudio(root, catalog=True):
    """An ~/.lmstudio as LM Studio keeps a catalog model: its definition folder (manifest.json names its weights
    repositories), one downloaded weights folder, its two records in .internal/model-data.json (a superjson Map), and its
    default load settings. Another model's records stand before it, one with an undefined field."""
    defn = os.path.join(root, 'hub', 'models', 'test', 'family'); os.makedirs(defn)
    open(os.path.join(defn, 'model.yaml'), 'w').write('model: test/family\n')
    json.dump(dict(type='model', name='family', dependencies=[dict(type='model', purpose='baseModel', sources=[
        dict(type='huggingface', user='test-community', repo='Family-MLX-4bit'), dict(type='huggingface', user='test-community', repo='Family-MLX-8bit')])]),
        open(os.path.join(defn, 'manifest.json'), 'w'))
    w = os.path.join(root, 'models', 'test-community', 'Family-MLX-8bit'); os.makedirs(w)
    open(os.path.join(w, 'model-00001-of-00001.safetensors'), 'wb').write(b'\0' * 4096); open(os.path.join(w, 'config.json'), 'w').write('{}')
    json.dump(dict(downloadsFolder=os.path.join(root, 'models')), open(os.path.join(root, 'settings.json'), 'w'))
    os.makedirs(os.path.join(root, '.internal', 'user-concrete-model-default-config', 'test'))
    json.dump(dict(preset='', load=dict(fields=[dict(key='llm.load.contextLength', value=65536)])),
              open(os.path.join(root, '.internal', 'user-concrete-model-default-config', 'test', 'family.json'), 'w'))
    entries = [['other/model', dict(source=dict(type='huggingface', repo='model', file=None), transitive=True)]]
    if catalog:
        entries += [['test/family', dict(source=dict(type='hub', url='https://lmstudio.ai/models/test/family', name='family'),
                                         uiIdentifier='test/family', transitive=False, lastLoadedTimestamp=1)],
                    ['test-community/Family-MLX-8bit', dict(source=dict(type='huggingface', repo='Family-MLX-8bit', file=None), transitive=True)]]
    ann = {'0.1.source.file': ['undefined']}
    if catalog: ann['2.1.source.file'] = ['undefined']
    json.dump(dict(json=entries, meta=dict(values=['map', ann])), open(os.path.join(root, '.internal', 'model-data.json'), 'w'))
    os.makedirs(os.path.join(root, 'bin'))
    lms = os.path.join(root, 'bin', 'lms'); open(lms, 'w').write('#!/bin/sh\necho "LM Studio is not running" >&2\n'); os.chmod(lms, 0o755)


def test_copy_model(box):
    print("localmodels peer copy-model: a model the same on a peer as here (its definition, weights, records, default settings)")
    import contextlib, io
    from localmodels import cli
    here, there = os.path.join(box, 'cm-here', '.lmstudio'), os.path.join(box, 'cm-there')
    fake_lmstudio(here)
    os.makedirs(os.path.join(there, '.lmstudio', '.internal'))
    open(os.path.join(there, '.lmstudio', 'settings.json'), 'w').write('{"downloadsFolder": "' + os.path.join(there, '.lmstudio', 'models') + '"}')
    json.dump(dict(json=[['x/y', dict(source=dict(type='hub', name='y'), transitive=False)],
                         ['z/w', dict(source=dict(type='huggingface', repo='w', file=None), transitive=True)]],
                   meta=dict(values=['map', {'1.1.source.file': ['undefined']}])), open(os.path.join(there, '.lmstudio', '.internal', 'model-data.json'), 'w'))
    rbin = os.path.join(box, 'cm-bin'); os.makedirs(rbin)
    os.makedirs(os.path.join(there, '.lmstudio', 'bin'))
    lms_there = os.path.join(there, '.lmstudio', 'bin', 'lms')
    running = {'v': False}

    def set_running(v):
        running['v'] = v
        open(lms_there, 'w').write('#!/bin/sh\necho "' + ('LM Studio v0.4.28+2 is running (PID: 42)' if v else 'LM Studio is not running') + '" >&2\n')
        os.chmod(lms_there, 0o755)
        open(os.path.join(rbin, 'pgrep'), 'w').write('#!/bin/sh\nexit 1\n'); os.chmod(os.path.join(rbin, 'pgrep'), 0o755)
    set_running(False)
    was = peers.LMSTUDIO_HOME, peers.remote, peers.send, llmshare.settings
    peers.LMSTUDIO_HOME = here
    sent = []

    def remote(target, script, data=None, timeout=60):    # the peer, played by a folder: its HOME, its own pgrep
        r = subprocess.run(['/bin/sh', '-c', script], input=data, capture_output=True, env=dict(os.environ, HOME=there, PATH=rbin + ':/usr/bin:/bin'), timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).decode('utf-8', 'replace')

    def send(src, target, dest):
        sent.append((src, dest)); os.makedirs(dest, exist_ok=True)
        r = subprocess.run(['rsync', '-a', src.rstrip('/') + '/', dest.rstrip('/') + '/'], capture_output=True)
        return r.returncode, (r.stdout + r.stderr).decode()
    peers.remote, peers.send = remote, send
    llmshare.settings = lambda: dict(families={F: dict(key=F, copies=1, context=65536, parallel=4, variant='test/family@8bit')})

    def run(*a):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = cli.peer_cmd(list(a))
        return code, out.getvalue()
    try:
        plan = peers.copy_plan(F, 'test/family@8bit')
        t('the plan finds its definition folder, the downloaded weights of its variant, its two records and its default settings',
          plan['weights_key'] == 'test-community/Family-MLX-8bit' and plan['definition'].endswith('test/family') and [r[0] for r in plan['records']] == [F, 'test-community/Family-MLX-8bit']
          and plan['records'][1][2] == {'1.source.file': ['undefined']} and plan['config'].endswith('family.json') and plan['bytes'] >= 4096, plan)
        doc = json.load(open(os.path.join(there, '.lmstudio', '.internal', 'model-data.json')))
        merged = peers.merge_records(doc, plan['records'])
        keys = [e[0] for e in merged['json']]
        t("the merge keeps the peer's own records and their undefined fields, and adds the two with theirs at their new place",
          keys == ['x/y', 'z/w', F, 'test-community/Family-MLX-8bit'] and merged['meta']['values'][1] == {'1.1.source.file': ['undefined'], '3.1.source.file': ['undefined']}
          and 'lastLoadedTimestamp' not in merged['json'][2][1], merged)
        again = peers.merge_records(merged, plan['records'])
        t('merging twice changes nothing (no second entry)', again == merged, [e[0] for e in again['json']])
        t('merging into a peer with no model-data.json yet', [e[0] for e in peers.merge_records(None, plan['records'])['json']] == [F, 'test-community/Family-MLX-8bit'])
        bare = os.path.join(box, 'cm-bare', '.lmstudio'); fake_lmstudio(bare, catalog=False)
        peers.LMSTUDIO_HOME = bare
        try: peers.copy_plan(F, 'test/family@8bit'); said = None
        except ValueError as e: said = str(e)
        peers.LMSTUDIO_HOME = here
        t("a model LM Studio holds no records of here: not planned (its records are copied as LM Studio wrote them), and said", said and 'load' in said, said)

        set_running(True)
        code, out = run('copy-model', F, 'someone@peer')
        t("LM Studio running on the peer: refused, nothing copied, and the words say to quit it there first", code == 1 and not sent and 'quit' in out.lower(), out[-300:])
        set_running(False)
        code, out = run('copy-model', F, 'someone@peer', '--dry')
        t('--dry: the plan in words, nothing copied', code == 0 and not sent and 'would copy' in out and 'Family-MLX-8bit' in out, out[-400:])
        code, out = run('copy-model', F, 'someone@peer')
        got = json.load(open(os.path.join(there, '.lmstudio', '.internal', 'model-data.json')))
        t('copied: the definition, the weights, the two records (the old file kept as a backup) and the default settings',
          code == 0 and os.path.exists(os.path.join(there, '.lmstudio', 'hub', 'models', 'test', 'family', 'model.yaml'))
          and os.path.exists(os.path.join(there, '.lmstudio', 'models', 'test-community', 'Family-MLX-8bit', 'model-00001-of-00001.safetensors'))
          and [e[0] for e in got['json']][-2:] == [F, 'test-community/Family-MLX-8bit']
          and any(f.startswith('model-data.json.bak') for f in os.listdir(os.path.join(there, '.lmstudio', '.internal')))
          and os.path.exists(os.path.join(there, '.lmstudio', '.internal', 'user-concrete-model-default-config', 'test', 'family.json')), out[-500:])
        code, out = run('copy-model', F, 'someone@peer')
        got2 = json.load(open(os.path.join(there, '.lmstudio', '.internal', 'model-data.json')))
        t('run again: the same records, once each', code == 0 and [e[0] for e in got2['json']] == [e[0] for e in got['json']], [e[0] for e in got2['json']])
    finally: peers.LMSTUDIO_HOME, peers.remote, peers.send, llmshare.settings = was


def test_link_rules(box):
    print("the link: a peer that keeps failing requests is backed off; one that answers starts again")
    L = peers.Link()
    real = llmshare.settings
    llmshare.settings = lambda: dict(families={F: dict(key=F, copies=2)}, peers={'mac': dict(url='http://192.168.213.9:1235')})
    try:
        L.sync()
        p = L.peers['mac']; p.state, p.cap, p.match = 'up', {F: 2}, {F: (True, None)}
        c = peers.cid(F, 'mac')
        t('a peer up with its family matched and free slots is a copy', L.replicas(F) == [c] and L.take(c), L.replicas(F))
        L.give(c)
        L.failed(c, 503, 'no free slot'); L.failed(c, 503, 'no free slot')
        t('two requests it could not take: still up, but nothing more until the next look', p.state == 'up' and not L.take(c), (p.state, p.cap))
        L.answered(c); p.cap = {F: 2}
        L.failed(c, 503, 'no free slot'); L.failed(c, 503, 'no free slot')
        t('an answer in between starts the count again', p.state == 'up', p.state)
        L.failed(c, 502, "LM Studio's server is not answering")
        t('the third within a minute: backed off as if it were down, and said why', p.state == 'down' and '3 request failures' in p.why and not L.replicas(F), p.why)
        print("the link: a request goes to a peer only while it fits the context budget the peer reported")
        p.ctx = {F: (10000, 6000)}                                   # its budget, and what others than this Mac hold there
        t('it fits the room left: sent; one token more, or beside this Mac\'s own 3,000 there: not',
          L.ctx_fits(c, 0, 4000) and not L.ctx_fits(c, 0, 4001) and not L.ctx_fits(c, 3000, 2000))
        p.ctx = {F: (10000, 0)}
        t('a peer with nothing in flight takes any request; a peer that reports no budget (an older hub) too',
          L.ctx_fits(c, 0, 50000) and not L.ctx_fits(c, 9000, 2000) and (setattr(p, 'ctx', {}) or L.ctx_fits(c, 9000, 50000)))
    finally: llmshare.settings = real

    print('the link: the token goes only to a peer on a subnet of this Mac, and never follows a redirect')
    got = []

    class Catch(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            got.append(self.headers.get('X-Hub-Token')); self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
    catch = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Catch); threading.Thread(target=catch.serve_forever, daemon=True).start()

    class Move(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(302); self.send_header('Location', f'http://127.0.0.1:{catch.server_address[1]}/hub/peer'); self.end_headers()
    move = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Move); threading.Thread(target=move.serve_forever, daemon=True).start()
    tok_was, peers.token = peers.token, lambda: TOKEN
    real_conn = peers.LinkConnection.connect if hasattr(peers, 'LinkConnection') else None
    tried = []
    try:
        llmshare.settings = lambda: dict(families={F: dict(key=F, copies=2)}, peers={'off': dict(url='http://192.0.2.7:1235'),
                                                                                    'moved': dict(url=f'http://127.0.0.1:{move.server_address[1]}')})
        L = peers.Link()
        if real_conn: peers.LinkConnection.connect = lambda self: (tried.append(self.host), real_conn(self))[1]
        L.poll(force=True)
        off, moved = L.peers['off'], L.peers['moved']
        t('a peer address on no subnet of this Mac (the cable out, or a routed address): down, and no connection is even tried',
          off.state == 'down' and 'nothing is sent' in (off.why or '') and '192.0.2.7' not in tried, (off.state, off.why, tried))
        t("a redirect from a peer's address is not followed: the token reaches nothing else", moved.state == 'down' and '302' in (moved.why or '') and not got,
          (moved.state, moved.why, got))
    finally:
        llmshare.settings, peers.token = real, tok_was
        if real_conn: peers.LinkConnection.connect = real_conn
        catch.shutdown(); move.shutdown()

    print('the link: a peer away for days keeps its 60 s backoff, and one peer that fails to be read never stops the others')
    fetch_was, peers.token = peers.fetch, lambda: TOKEN
    asked = []

    def fetch(url, path, headers, timeout=peers.CONNECT):
        asked.append(url)
        if url.endswith(':1'): raise RuntimeError('a look that fails in an unforeseen way')
        raise ConnectionRefusedError(errno.ECONNREFUSED, 'refused')
    try:
        llmshare.settings = lambda: dict(families={F: dict(key=F, copies=2)}, peers={'first': dict(url='http://127.0.0.1:1'), 'second': dict(url='http://127.0.0.1:2')})
        L = peers.Link(); L.sync()
        p = L.peers['first']; p.fails = 2000
        try: L._down(p, 'away'); err = None
        except Exception as e: err = e
        t('after 2,000 failed looks (about 18 hours away) the next look is at most 10 s later: a cable plugged back in shows within seconds', err is None and 9 <= p.next_at - time.time() <= 11, repr(err) if err else p.next_at - time.time())
        peers.fetch = fetch
        try: L.poll(force=True); err = None
        except Exception as e: err = e
        t('a first peer whose look raises: recorded, marked down, and the second peer is still polled',
          err is None and asked == ['http://127.0.0.1:1', 'http://127.0.0.1:2'] and L.peers['first'].state == 'down' and L.peers['second'].state == 'down', (repr(err), asked))
    finally: llmshare.settings, peers.fetch, peers.token = real, fetch_was, tok_was

    print('the link: every change of a peer\'s state is recorded with its reason and time, and read back')
    answers = []

    def fetch2(url, path, headers, timeout=peers.CONNECT):
        a = answers.pop(0)
        if isinstance(a, Exception): raise a
        return 200, json.dumps(a).encode()
    peers.fetch, peers.token = fetch2, lambda: TOKEN
    events_was = peers.EVENTS; peers.EVENTS = os.path.join(box, 'link-events.jsonl')
    try:
        llmshare.settings = lambda: dict(families={F: dict(key=F, copies=2)}, peers={'m4': dict(url='http://127.0.0.1:3')})
        L = peers.Link()
        refused = ConnectionRefusedError(errno.ECONNREFUSED, 'refused')
        answers[:] = [refused, refused, dict(hub='local-models', families={})]
        L.poll(force=True); first = L.peers['m4'].since
        L.poll(force=True)
        L.poll(force=True)
        ev = peers.link_events(1)
        t('down, still down, then up: two records (a state that stays is not recorded again), each with its time and reason',
          [(e['peer'], e['was'], e['state']) for e in ev] == [('m4', 'new', 'down'), ('m4', 'down', 'up')] and 'ECONNREFUSED' in ev[0]['why'] and all(e.get('at') for e in ev), ev)
        t('the peer says since when it is in its state', L.peers['m4'].since >= first and L.view()[0].get('since'), (first, L.peers['m4'].since))
        t('the record holds names, states, reasons and times only', all(set(e) <= {'at', 'peer', 'was', 'state', 'why'} for e in ev), ev)
        import contextlib, io
        from localmodels import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = cli.peer_cmd(['log'])
        t('localmodels peer log reads them back, oldest first', code == 0 and 'm4' in out.getvalue() and out.getvalue().index('down') < out.getvalue().index('up'), out.getvalue())
    finally:
        llmshare.settings, peers.fetch, peers.token, peers.EVENTS = real, fetch_was, tok_was, events_was


# ---------------------------------------------------------------- two doors
def test_two_doors(box):
    print('two doors: this Mac and a peer, each with its own pretend LM Studio')
    front_dir, peer_dir = os.path.join(box, 'front'), os.path.join(box, 'peer')
    os.makedirs(front_dir); os.makedirs(peer_dir)
    A, B = fake('front'), fake('peer')
    up_a = http.server.ThreadingHTTPServer(('127.0.0.1', 0), A); threading.Thread(target=up_a.serve_forever, daemon=True).start()
    up_b = http.server.ThreadingHTTPServer(('127.0.0.1', 0), B); threading.Thread(target=up_b.serve_forever, daemon=True).start()

    # the peer: its own door, its own state, its own settings and token, its own pretend lms and model definition
    peer_port, link_port = free_port(), free_port()
    lms_b = lmstudio_home(os.path.join(peer_dir, 'lmstudio'), 'model: test/family\n')
    open(os.path.join(peer_dir, 'lmstudio', 'ps.json'), 'w').write(json.dumps([ps_row(F), ps_row('test/other', device='third-mac')]))
    json.dump(dict(name='peer', families={F: dict(key=F, copies=1, context=4096, parallel=2, recycle=False)},
                   serve=dict(address='127.0.0.1', port=link_port), batch_at_once=2, headroom_gb=1), open(os.path.join(peer_dir, 'models.json'), 'w'))
    tok = os.path.join(peer_dir, 'peer-token'); open(tok, 'w').write(TOKEN + '\n'); os.chmod(tok, 0o600)
    env = dict(os.environ, LOCALMODELS_STATE=peer_dir, LOCALMODELS_PORT=str(peer_port), LMSTUDIO_URL=f'http://127.0.0.1:{up_b.server_address[1]}',
               LMS=lms_b, LMSTUDIO_LOGS=os.path.join(peer_dir, 'logs'), LOCALMODELS_NO_KEEPER='1')
    log = open(os.path.join(box, 'peer-door.log'), 'w')
    proc = subprocess.Popen([sys.executable, os.path.join(HOME, 'bin', 'localmodels'), 'gateway'], env=env, stdout=log, stderr=log)

    def peer_up():
        for _ in range(100):
            try:
                req = urllib.request.Request(f'http://127.0.0.1:{link_port}/hub/health', headers={'X-Hub-Token': TOKEN})
                if json.load(urllib.request.urlopen(req, timeout=1)).get('ok'): return True
            except (OSError, ValueError): pass
            time.sleep(0.1)
        return False

    # this Mac: the door in this process, like test_gateway.py, with the peer in its settings
    for name in ('SLOTS', 'LEASES', 'INFLIGHT'): setattr(llmshare, name, os.path.join(front_dir, name.lower()))
    llmshare.REQUESTS, llmshare.WAITS = os.path.join(front_dir, 'requests.jsonl'), os.path.join(front_dir, 'waits.jsonl')
    crashes.STATE, crashes.RECORD, crashes.LOGS = os.path.join(front_dir, 'keeper.json'), os.path.join(front_dir, 'crashes.jsonl'), os.path.join(front_dir, 'logs')
    crashes._seen_state.update(at=0.0, st={})
    peers.TOKEN_FILE, peers.STATE = os.path.join(front_dir, 'peer-token'), os.path.join(front_dir, 'link.json')
    lmstudio_home(os.path.join(front_dir, 'lmstudio'), 'model: test/family\n'); peers.LMSTUDIO_HOME = os.path.join(front_dir, 'lmstudio')
    open(peers.TOKEN_FILE, 'w').write(TOKEN + '\n'); os.chmod(peers.TOKEN_FILE, 0o600)
    peers.route_of = lambda host: ('lo0', 'Loopback')
    row = lambda i: dict(id=i, key=F, state='IDLE', size='1 GB', context=4096, parallel=1, variant='test/family@8bit', bytes=1000, format='safetensors')
    rows = [row(F), row(F2)]
    llmshare.loaded, llmshare.parallel_of, llmshare.read_ok = (lambda fresh=False, force=False: list(rows)), (lambda m: 1), (lambda: True)
    llmshare.api_models = lambda timeout=3: {r['id']: 4096 for r in rows}
    conf = dict(name='front', families={F: dict(key=F, copies=2, context=4096, parallel=1, recycle=False)},
                peers={'peer': dict(url=f'http://127.0.0.1:{link_port}')}, batch_at_once=2, headroom_gb=1)
    llmshare.settings = lambda: conf
    door = gateway.Server(('127.0.0.1', 0), gateway.Door); threading.Thread(target=door.serve_forever, daemon=True).start()
    gateway.UP, gateway.PORT = f'http://127.0.0.1:{up_a.server_address[1]}', door.server_address[1]
    url = f'http://localhost:{gateway.PORT}'
    L = peers.LINK

    def ask(tag, kind='test', headers=None, stream=False, timeout=60, base=None, host=None):
        req = urllib.request.Request((base or url) + '/v1/chat/completions', data=json.dumps(dict(model=F, messages=[], tag=tag, stream=stream)).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Kind': kind, **({'X-Project': 'proj'} if base is None else {}), **(headers or {})})
        if host: req.add_header('Host', host)
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            return r.status, (r.read() if stream else json.load(r)), dict(r.headers)
        except urllib.error.HTTPError as e: return e.code, (e.read() or b'{}'), dict(e.headers)

    def link(path, token=TOKEN, method='GET', body=None, headers=None):
        h = dict(headers or {}, **({'X-Hub-Token': token} if token else {}))
        if body is not None: h['Content-Type'] = 'application/json'
        req = urllib.request.Request(f'http://127.0.0.1:{link_port}{path}', data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
        try: r = urllib.request.urlopen(req, timeout=10); return r.status, json.load(r)
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b'{}')

    def logged(path, n=1):
        time.sleep(0.4)
        try: return [json.loads(x) for x in open(path).read().strip().split('\n')[-n:]]
        except (OSError, ValueError): return []

    def plan(fake_, *what):
        with fake_.lock: fake_.plan[:] = list(what); fake_.seen.clear(); fake_.heads.clear(); fake_.most.clear()

    def together(*jobs, stagger=0.15):
        out, ths = {}, []
        for k, (tag, kw) in enumerate(jobs):
            th = threading.Thread(target=lambda tag=tag, kw=kw: out.__setitem__(tag, ask(tag, **kw))); th.start(); ths.append(th); time.sleep(stagger)
        for th in ths: th.join(60)
        return out

    try:
        t("the peer's door opens and answers its link with the token", peer_up(), open(os.path.join(box, 'peer-door.log')).read()[-400:])
        L.poll(force=True)
        v = {p['name']: p for p in peers.view()}.get('peer') or {}
        fam = (v.get('families') or {}).get(F) or {}
        t("this Mac's look at the peer: up, the family matched, its 2 slots counted", v.get('state') == 'up' and fam.get('match') and fam.get('slots') == 2, v)
        t('LM Link\'s row on the peer (another Mac\'s model) is not taken for a copy there', 'test/other' not in (v.get('families') or {}), list((v.get('families') or {})))
        t('the screens read the same look from the shared state (link.json)', os.path.exists(peers.STATE) and peers.view()[0]['name'] == 'peer')
        code, rep = link('/hub/peer')
        cx = ((rep.get('families') or {}).get(F) or {}).get('ctx') or {}
        t("the peer's report says the context its copies may hold in flight (one copy of 2 slots of 4,096: 8,192) and what they hold",
          code == 200 and cx.get('budget') == 8192 and cx.get('load') == 0 and cx.get('by') == {} and L.peers['peer'].ctx.get(F) == (8192, 0), (code, cx, L.peers['peer'].ctx))
        mem = v.get('memory') or {}
        t("the peer reports its memory, and this Mac's look carries it (for the memory pool)",
          isinstance(mem.get('total'), (int, float)) and mem['total'] > 0 and 0 <= mem.get('used', -1) <= mem['total'] * 1.05, mem)

        for r_ in rows: r_['variant'] = 'test/family@4bit'
        L.poll(force=True)
        fam = (({p['name']: p for p in peers.view()}.get('peer') or {}).get('families') or {}).get(F) or {}
        t("this Mac's copies another quantization: the peer's copy is not used, and the words say which",
          not fam.get('match') and '8bit' in (fam.get('why') or '') and '4bit' in (fam.get('why') or '') and not L.replicas(F), fam.get('why'))
        for r_ in rows: r_['variant'] = 'test/family@8bit'
        L.poll(force=True)
        t('the same again: used again', L.replicas(F) == [peers.cid(F, 'peer')], L.replicas(F))

        print('two doors: local slots first, then the peer\'s')
        plan(A); plan(B)
        code, r, h = ask('a1')
        t('one request with this Mac\'s slots free is answered here', code == 200 and [s[1] for s in A.seen] == ['a1'] and not B.seen, (code, A.seen, B.seen))
        plan(A, 'slow', 'slow'); plan(B, 'slow', 'slow'); A.slow = B.slow = 2.0
        out = together(('b1', {}), ('b2', {}), ('b3', {}), ('b4', {}))
        t("a batch, four at once: this Mac's first copy, then the peer's (its own GPU), then this Mac's second copy; all answered",
          all(out[k][0] == 200 for k in out) and [s[1] for s in A.seen] == ['b1', 'b4'] and sorted(s[1] for s in B.seen) == ['b2', 'b3'],
          ({k: v_[0] for k, v_ in out.items()}, A.seen, B.seen))
        t('the answer from the peer names the model asked for', out['b2'][1].get('model') == F, out['b2'][1])
        plan(A, 'slow', 'slow'); plan(B, 'slow'); A.slow = B.slow = 2.0
        out = together(('h1', dict(kind='batch')), ('h2', dict(kind='chat')), ('h3', dict(kind='batch')))
        t("a person waiting gets this Mac's second copy before the peer (the fastest free slot); the batch after it goes to the peer",
          [s[1] for s in A.seen] == ['h1', 'h2'] and [s[1] for s in B.seen] == ['h3'] and all(v_[0] == 200 for v_ in out.values()), (A.seen, B.seen))
        mine = [x for x in logged(llmshare.REQUESTS, 9) if x.get('peer') == 'peer']
        t('this Mac logs the peer\'s answers as "test/family on peer", never as one of its own copies',
          len(mine) == 3 and all(x.get('copy') == 'test/family on peer' for x in mine), [(x.get('copy'), x.get('peer')) for x in logged(llmshare.REQUESTS, 9)])
        L.poll(force=True)
        t("at_once counts the peer's free slots: 2 here and 2 there", llmshare.at_once(F) == 4, llmshare.at_once(F))

        print('two doors: who a request is from, forwarded')
        with crashes._state() as st: st['down'] = {F: dict(crashed=time.time(), models=[F, F2], state='loading again')}
        plan(A); plan(B)
        hd = {'X-Session': 'sess-1', 'X-Kind': 'batch', 'X-Step': 'extract 3 of 9', 'X-Run': 'nightly', 'X-Run-Total': '9', 'X-Run-Done': '2',
              'Authorization': 'Bearer sk-the-clients-own', 'x-api-key': 'the-clients-own', 'OpenAI-Organization': 'org-own'}
        code, r, h = ask('c1', kind='batch', headers=hd)
        prow = (logged(os.path.join(peer_dir, 'requests.jsonl')) or [{}])[0]
        t('this Mac\'s copies down after a crash: the peer answers at once, nothing held', code == 200 and B.seen and not A.seen, (code, A.seen, B.seen))
        t('the peer logs the project, session, kind, step and run this Mac sent',
          (prow.get('project'), prow.get('session'), prow.get('kind'), prow.get('step'), prow.get('run'), prow.get('run_total'), prow.get('run_done'))
          == ('proj', 'sess-1', 'batch', 'extract 3 of 9', 'nightly', 9, 2), prow)
        t('the peer logs which Mac sent it', prow.get('from') == 'front', prow)
        t("the token never reaches the peer's LM Studio", B.heads and not any(k.startswith('x-hub') for k in B.heads[-1]), B.heads[-1:] if B.heads else None)
        t("the client's own API credentials stay on this Mac: the peer gets the token, never Authorization, x-api-key or the organization",
          B.heads and not {'authorization', 'x-api-key', 'openai-organization'} & set(B.heads[-1]), B.heads[-1:] if B.heads else None)
        plan(B, 'sse')
        code, data, h = ask('s1', stream=True)
        frow = logged(llmshare.REQUESTS)[0]
        t('a stream from the peer passes through whole, its tokens counted here', code == 200 and data.rstrip().endswith(b'data: [DONE]') and b'"K"' in data
          and (frow.get('prompt_tokens'), frow.get('completion_tokens')) == (5, 2), (code, data[-80:], frow.get('prompt_tokens')))
        with crashes._state() as st: st['down'] = {}

        print('two doors: never more in flight to the peer than it says it has free')
        plan(A, 'slow', 'slow', 'slow', 'slow'); plan(B, 'slow', 'slow', 'slow', 'slow'); A.slow, B.slow = 2.0, 4.0
        mine_p = threading.Thread(target=lambda: ask('p1', base=f'http://localhost:{peer_port}', headers={'X-Project': 'peer-own'})); mine_p.start()
        time.sleep(0.8); L.poll(force=True)
        fam = (({p['name']: p for p in peers.view()}.get('peer') or {}).get('families') or {}).get(F) or {}
        t("the peer's own work counts: one of its 2 slots busy, so this Mac may send it 1", fam.get('busy') == 1 and fam.get('free_here') == 1, fam)
        out = together(('f1', {}), ('f2', {}), ('f3', {}), ('f4', {}))
        mine_p.join(30)
        t('four more at once: at most 1 at a time on the peer from this Mac, all answered', B.most['f'] <= 1 and all(v_[0] == 200 for v_ in out.values()),
          (dict(B.most), {k: v_[0] for k, v_ in out.items()}))

        print('two doors: the token, web pages, swaps and the dashboard over the link')
        t('the link without the token: 401', link('/hub/peer', token=None)[0] == 401)
        t('the link with a wrong token: 401', link('/hub/peer', token='b' * 64)[0] == 401)
        code, rep = link('/hub/peer')
        t('the link with the token: the peer\'s report', code == 200 and F in (rep.get('families') or {}), code)
        t('a web page over the link (an Origin): refused', link('/hub/peer', headers={'Origin': 'http://example.com'})[0] == 403)
        t('an unload over the link: refused', link('/api/v1/models/unload', method='POST', body=dict(instance_id=F))[0] == 403)
        t("the dashboard over the link: refused", link('/')[0] == 403)
        open(peers.TOKEN_FILE, 'w').write('c' * 64 + '\n'); os.chmod(peers.TOKEN_FILE, 0o600)
        L.poll(force=True)
        v = {p['name']: p for p in peers.view()}.get('peer') or {}
        t("this Mac with a token the peer does not take: refused, in words, and nothing routed there",
          v.get('state') == 'refused' and 'token' in (v.get('words') or '') and not L.replicas(F), v.get('words'))
        L.poll()
        t('a refused token is not tried again while nothing changed', L.peers['peer'].tries_refused == 1, L.peers['peer'].tries_refused)
        open(peers.TOKEN_FILE, 'w').write(TOKEN + '\n'); os.chmod(peers.TOKEN_FILE, 0o600)
        L.poll()
        t('the token file changed: looked at again, and up', ({p['name']: p for p in peers.view()}.get('peer') or {}).get('state') == 'up')

        print('two doors: the peer in maintenance, then gone')
        json.dump(dict(v=1, set_at=time.time(), minutes=30, why='updating LM Studio', start=time.time() - 1, after_batches=False), open(os.path.join(peer_dir, 'maintenance.json'), 'w'))
        plan(A, 'slow', 'slow', 'ok'); plan(B); A.slow = 2.0
        out = together(('m1', {}), ('m2', {}), ('m3', {}))
        frow = [x for x in logged(llmshare.REQUESTS, 3) if x.get('step') is None]
        t('a request this Mac sent before it knew: the peer refuses it (503), it is sent again here and answered, the peer\'s LM Studio never sees it',
          all(v_[0] == 200 for v_ in out.values()) and not B.seen and sorted(s[1] for s in A.seen) == ['m1', 'm2', 'm3'], ({k: v_[0] for k, v_ in out.items()}, A.seen, B.seen))
        v = {p['name']: p for p in peers.view()}.get('peer') or {}
        t('this Mac knows at once: the peer is in maintenance, skipped until it ends', v.get('state') == 'maintenance' and not L.replicas(F), (v.get('state'), v.get('words')))
        os.remove(os.path.join(peer_dir, 'maintenance.json'))
        L.poll(force=True)
        t('maintenance over: up again', ({p['name']: p for p in peers.view()}.get('peer') or {}).get('state') == 'up')
        proc.terminate(); proc.wait(10)
        plan(A, 'slow', 'slow', 'ok'); plan(B)
        out = together(('g1', {}), ('g2', {}), ('g3', {}))
        last = logged(llmshare.REQUESTS, 3)
        t('the peer gone between two looks: the request it could not take is sent again here and answered',
          all(v_[0] == 200 for v_ in out.values()) and sorted(s[1] for s in A.seen) == ['g1', 'g2', 'g3'] and any('requeued locally' in (x.get('note') or '') for x in last),
          ({k: v_[0] for k, v_ in out.items()}, [x.get('note') for x in last]))
        v = {p['name']: p for p in peers.view()}.get('peer') or {}
        t('the peer is marked down, in words, and skipped', v.get('state') == 'down' and 'ECONNREFUSED' in (v.get('words') or '') and not L.replicas(F), v.get('words'))
        L.peers['peer'].next_at = 0; L.poll()
        n1, gap1 = L.peers['peer'].fails, L.peers['peer'].next_at - time.time()
        L.poll()
        t('looked at again only after its pause (backoff)', L.peers['peer'].fails == n1 and gap1 > 1, (n1, L.peers['peer'].fails, gap1))
        L.peers['peer'].next_at = 0; L.poll()
        gap2 = L.peers['peer'].next_at - time.time()
        t('each failed look doubles the pause, up to a minute', L.peers['peer'].fails == n1 + 1 and 1.8 * gap1 < gap2 <= 61, (round(gap1, 1), round(gap2, 1)))
        t('nobody waits once the queue is empty', llmshare.waiting_here() == dict(people=0, others=0), llmshare.waiting_here())
    finally:
        try: proc.terminate(); proc.wait(5)
        except Exception: pass
        door.shutdown(); up_a.shutdown(); up_b.shutdown(); log.close()


def test_pool(box):
    print('the memory pool: this Mac and its peers that are up and report memory; the samples, old and new')
    from localmodels import dash, web
    m = dict(used=100.0, total=128.0)
    ps = [dict(name='a', state='up', memory=dict(used=40.0, total=48.0)), dict(name='b', state='down', memory=dict(used=10.0, total=64.0)),
          dict(name='c', state='up', memory=None), dict(name='d', state='maintenance', memory=dict(used=5.0, total=32.0))]
    p = dash.pool(m, ps)
    t('the pool: this Mac first, then the peers up (or in maintenance) that report memory; a down peer and an older peer left out',
      [h['name'] for h in p['hosts']][1:] == ['a', 'd'] and p['used'] == 145.0 and p['total'] == 208.0, p)
    t('no peers: one host, this Mac', len(dash.pool(m, [])['hosts']) == 1 and dash.pool(m, [])['total'] == 128.0)
    t('a memory sample from before peers reported memory still reads', web._mem_row('[1700000000, 101.5]\n') == (1700000000.0, 101.5, None))
    t('a sample with peers reads with each peer', web._mem_row('[1700000000, 101.5, {"a": [40.2, 48.0]}]') == (1700000000.0, 101.5, {'a': [40.2, 48.0]}))
    t('a broken line is skipped', web._mem_row('{"oops"') is None and web._mem_row('[1]') is None)
    now = time.time()
    web._mem.clear(); web._mem.extend([(now - 120, 100.0, None), (now - 60, 101.0, {'a': [40.0, 48.0]}), (now - 30, 102.0, {'a': [41.0, 48.0]})])
    M = web.history(60)['memory']
    t('the chart: each peer as a series (gaps where it was not sampled), and the top line follows the hosts present',
      M['hosts'] == {'a': [None, 40.0, 41.0]} and M['cap'][1:] == [M['total'] + 48] * 2 and M['cap'][0] == M['total'], M)
    web._mem.clear()


def main():
    box = tempfile.mkdtemp(prefix='localmodels-peerstest-')
    try:
        for name in ('SLOTS', 'LEASES', 'INFLIGHT'): setattr(llmshare, name, os.path.join(box, name.lower()))
        llmshare.REQUESTS, llmshare.WAITS = os.path.join(box, 'requests.jsonl'), os.path.join(box, 'waits.jsonl')
        from localmodels import names as _names                          # never this Mac's live settings or names
        llmshare.MODELS, _names.FILE = os.path.join(box, 'models.json'), os.path.join(box, 'names.json')
        crashes.STATE, crashes.RECORD, crashes.LOGS = os.path.join(box, 'keeper.json'), os.path.join(box, 'crashes.jsonl'), os.path.join(box, 'logs')
        os.makedirs(crashes.LOGS)
        crashes._seen_state.update(at=0.0, st={})
        from localmodels import maint
        maint.FILE = os.path.join(box, 'maintenance.json')
        peers.STATE, peers.TOKEN_FILE = os.path.join(box, 'link.json'), os.path.join(box, 'peer-token')
        peers.LMSTUDIO_HOME = os.path.join(box, 'lmstudio-home'); os.makedirs(peers.LMSTUDIO_HOME)     # never this Mac's own LM Studio settings
        test_units(box)
        test_keeper(box)
        test_commands(box)
        test_copy_model(box)
        test_link_rules(box)
        test_two_doors(box)
        test_pool(box)
    finally: shutil.rmtree(box, ignore_errors=True)
    t('no hub path points into the live state folder (tests/_sandbox.py)', not _sandbox.stray(), _sandbox.stray())
    print(f'{sum(ok)} of {len(ok)} ok')
    return 0 if all(ok) else 1


if __name__ == '__main__':
    sys.exit(main())
