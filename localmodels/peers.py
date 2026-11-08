"""peers - other Macs' hubs as more copies of a family: when this Mac's slots are full, its door sends a family's next
request to a peer Mac that has the same model loaded, over the Thunderbolt Bridge, and the answer comes back through it.

Every Mac runs the same hub. The Mac the projects run on is the front: its settings name each peer and its address
("peers": {"<name>": {"url": "http://<address>:1235"}}). A peer's settings say where it listens for the front
("serve": {"address": "<its Thunderbolt Bridge address>"}). Both hold the same token in peer-token (mode 0600, beside the
settings; never in the repo or in models.json). Over the link a peer's door answers only requests with that token, never a
web page, a load or unload, a recycle or the dashboard (gateway.Door.over_link).

  report()      what a peer tells a front (GET /hub/peer): each family loaded there with its copies, its slots, how many
                are busy and how many of those a front's requests hold, its fingerprint, its maintenance, its keeper's
                warnings. Never content.
  LINK          the front's side, in the door: it looks at each peer every 2 s (backoff from 2 s to a minute when one
                does not answer; a refused token is not tried again until the token or the settings change, or 5
                minutes pass; a peer in maintenance is skipped until its window ends). A family a peer has loaded with
                the same fingerprint counts as one more copy, with the slots the peer has free for this Mac: local slots
                first, then the peers', in the door's one queue (people first), and never more in flight to a peer than
                it said it had free. A request a peer could not take, before any byte reached the program, is sent
                again here (gateway.generate).
  view()        the front's look at its peers, from link.json (the door writes it), for every screen.

A model's identity across Macs (fingerprint, match): the family id in the settings, the variant LM Studio loaded
(`lms ps --json` selectedVariant, e.g. qwen/qwen3.6-35b-a3b@8bit), its size in bytes, its format, and a digest of its
definition in LM Studio (~/.lmstudio/hub/models/<key>/model.yaml and manifest.json, which carry its prompt template and
sampling). The same key can be another quantization on another Mac, and weights copied without their definition load
under another key with no template: neither is the same model, and the screens say why.

The network: fixed addresses on the Thunderbolt Bridge (169.254 addresses change at every restart, and with two link-local
interfaces on one cable the traffic may take the slow one); a peer's listener is bound to the interface that holds its
address; route_of() says which interface the route to a peer takes. macOS's Local Network permission can block a
LaunchAgent's connections to the local network with "no route to host" and no prompt: reach_words() says where to turn it
on. LM Link (LM Studio's own linking) is not used: llmshare leaves its rows (deviceIdentifier set) out of the listing.
"""
import collections, errno, hashlib, hmac, ipaddress, json, os, re, secrets, socket, subprocess, threading, time, urllib.error, urllib.parse, urllib.request

from . import VERSION, llmshare, maint

TOKEN_FILE = os.path.join(llmshare.SHARED, 'peer-token')      # the same on every Mac of the set; mode 0600
STATE = os.path.join(llmshare.SHARED, 'link.json')            # the front's look at its peers, written by the door
LMSTUDIO_HOME = (os.path.dirname(os.path.dirname(llmshare.LMS)) if llmshare.LMS.endswith(os.sep + os.path.join('bin', 'lms'))
                 else os.path.expanduser('~/.lmstudio'))
NAME = re.compile(r'^[A-Za-z0-9._-]{1,40}$')
ON = ' on '                    # a copy on a peer is "<family> on <peer>": model ids hold no spaces, so it is never a local id
POLL, BACKOFF_MAX, REFUSED_AGAIN, ROUTE_EVERY, WRITE_EVERY = 2.0, 60.0, 300.0, 300.0, 10.0
STRIKES, STRIKE_WINDOW = 3, 60   # requests a peer failed though its look was fine, within this many seconds: it is backed off
PEER_WAIT = 5                  # seconds a peer's door may keep this Mac's request waiting for a slot before it answers 503
CONNECT = 3                    # seconds to connect to a peer
WAKE = threading.Event()       # a request to a peer failed: look again at once
LISTEN = {'words': None}       # in the door: what its listener for peers does now, in words (gateway.listen_link)
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open    # never through a proxy from the environment
_cache = {}


def _sh(cmd, timeout=5):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError): return ''


def _every(key, seconds, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < seconds: return hit[1]
    val = fn(); _cache[key] = (time.time(), val)
    return val


# ---------------------------------------------------------------- the token
def token():
    """The token this Mac shares with its peers, or None when there is none (or it is too short to be one)."""
    try: s = open(TOKEN_FILE).read().strip()
    except OSError: return None
    return s if len(s) >= 32 and not re.search(r'\s', s) else None


def token_problem():
    """Why this Mac's token cannot be used, in words, or None."""
    try: st = os.stat(TOKEN_FILE)
    except OSError: return f'there is no token file ({TOKEN_FILE}): localmodels peer token makes one'
    if st.st_mode & 0o077: return f'the token file can be read by other users of this Mac: chmod 600 "{TOKEN_FILE}"'
    if not token(): return f'the token file holds no token (at least 32 characters, no spaces): {TOKEN_FILE}'
    return None


def make_token():
    """Make the token file (64 hex characters, mode 0600) if there is none. -> whether it was made now."""
    if token(): return False
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    tmp = f'{TOKEN_FILE}.{os.getpid()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f: f.write(secrets.token_hex(32) + '\n')
    os.replace(tmp, TOKEN_FILE)
    return True


def token_ok(given):
    """Whether a request carries this Mac's token (compared in constant time). False when this Mac has none, or when its
    file can be read by other users."""
    want = token()
    if not want or not given or token_problem(): return False
    return hmac.compare_digest(want.encode(), str(given).strip().encode())


# ---------------------------------------------------------------- the settings
def me():
    """This Mac's name as its peers show it: "name" in the settings, else its short host name."""
    n = llmshare.clean(llmshare.settings().get('name'), 40)
    if n and NAME.match(n): return n
    h = re.sub(r'[^A-Za-z0-9._-]+', '-', socket.gethostname().split('.')[0])[:40].strip('-')
    return h or 'this-mac'


def parse_url(u):
    """'http://192.168.213.2:1235' or '192.168.213.2' -> (url, host, port), or None when it is not an http address."""
    u = str(u or '').strip()
    if not u: return None
    if '://' not in u:
        if u.count(':') > 1 and not u.startswith('['): u = f'[{u}]'           # a bare IPv6 address
        u = 'http://' + u
    p = urllib.parse.urlsplit(u)
    if p.scheme != 'http' or not p.hostname: return None
    try: port = p.port or llmshare.HUB_PORT
    except ValueError: return None
    host = p.hostname
    return (f"http://{('[' + host + ']') if ':' in host else host}:{port}", host, port)


def configured():
    """The peers in this Mac's settings, in their order: {name: {url, host, port}}. An entry that is not a plain name with
    an http address is left out (problems() says so)."""
    raw = llmshare.settings().get('peers')
    out = {}
    if not isinstance(raw, dict): return out
    for name, p in raw.items():
        if not NAME.match(str(name)): continue
        u = parse_url(p.get('url') or p.get('address') if isinstance(p, dict) else p)
        if u: out[name] = dict(url=u[0], host=u[1], port=u[2])
    return out


def problems():
    """What is wrong with the peers in the settings, in words: [] when nothing is."""
    raw, out = llmshare.settings().get('peers'), []
    if raw is None: return out
    if not isinstance(raw, dict): return ['"peers" in the settings is not a list of names and addresses ({"<name>": {"url": "http://<address>:1235"}})']
    for name, p in raw.items():
        u = parse_url(p.get('url') or p.get('address') if isinstance(p, dict) else p)
        if not NAME.match(str(name)): out.append(f'the peer name {name!r} is not a plain word (letters, digits, . _ -)')
        elif not u: out.append(f'{name}: {p!r} is not an http address')
        elif address_words(u[1]): out.append(f'{name}: {address_words(u[1])}')
    return out


def serving():
    """This Mac as a peer: {address, port} from "serve" in its settings, or None (it listens on this Mac only)."""
    s = llmshare.settings().get('serve')
    if not isinstance(s, dict) or not s.get('address'): return None
    addr = str(s['address']).strip().strip('[]')
    try: port = int(s.get('port') or llmshare.HUB_PORT)
    except (TypeError, ValueError): port = llmshare.HUB_PORT
    return dict(address=addr, port=port)


def cid(model, peer):
    """The copy id the door gives a family on a peer: '<family> on <peer>'."""
    return f'{model}{ON}{peer}'


def split(c):
    """'<family> on <peer>' -> (family, peer); a local id -> (id, None)."""
    s = str(c or '')
    if ON in s:
        m, p = s.split(ON, 1)
        return m, p
    return s, None


def loopback(host):
    try: return ipaddress.ip_address(str(host).split('%')[0]).is_loopback
    except ValueError: return str(host).lower() == 'localhost'


# ---------------------------------------------------------------- the network
def interfaces(text=None):
    """{interface: [(IPv4 address, prefix length)]}, from `ifconfig` (interfaces with no IPv4 address left out)."""
    text = _sh(['ifconfig']) if text is None else text
    out, cur = {}, None
    for line in text.split('\n'):
        m = re.match(r'^([A-Za-z0-9]+): flags=', line)
        if m: cur = m.group(1); out.setdefault(cur, []); continue
        m = re.match(r'^\s+inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)', line)
        if m and cur: out[cur].append((m.group(1), bin(int(m.group(2), 16)).count('1')))
    return {k: v for k, v in out.items() if v}


def interface_of(address, text=None):
    """The interface that holds this address (an IPv6 one names it: fe80::1%bridge0), or None."""
    address = str(address)
    if '%' in address: return address.split('%', 1)[1]
    for name, addrs in interfaces(text).items():
        if any(a == address for a, _ in addrs): return name
    return None


def ports(text=None):
    """{device: its hardware port's name} (Wi-Fi, Thunderbolt Bridge, Ethernet Adapter (en7), ...), from networksetup."""
    text = _sh(['networksetup', '-listallhardwareports']) if text is None else text
    out, port = {}, None
    for line in text.split('\n'):
        if line.startswith('Hardware Port: '): port = line[15:].strip()
        elif line.startswith('Device: ') and port: out[line[8:].strip()] = port; port = None
    return out


def route_interface(text):
    """The interface in `route -n get <host>`'s answer, or None."""
    m = re.search(r'^\s*interface: (\S+)', text or '', re.M)
    return m.group(1) if m else None


def route_of(host):
    """(the interface the route to this host takes, its port's name), or (None, None) when there is no route."""
    if loopback(host): return 'lo0', 'Loopback'
    iface = route_interface(_sh(['route', '-n', 'get', str(host).split('%')[0]]))
    names = _every('ports', 600, ports)
    return iface, names.get(iface) if iface else None


def bridge(iface, names=None):
    return (names or {}).get(iface) == 'Thunderbolt Bridge' or (iface or '').startswith('bridge')


def route_words(iface, names):
    """Words when the route to a peer does not take the Thunderbolt Bridge, else None."""
    if not iface or bridge(iface, names) or iface.startswith('lo'): return None
    port = names.get(iface)
    over = f'{port}' if port and iface in port else f'{port} ({iface})' if port else iface
    return (f'the route to it goes over {over}, not the Thunderbolt Bridge: give the Thunderbolt Bridge a fixed address on '
            'both Macs, in a subnet no other network uses (GUIDE.md, Peers)')


def address_words(addr):
    """Words when an address will not stay put, else None."""
    a = str(addr or '')
    if a.startswith('169.254.'):
        return (f'{a} is a link-local address: it changes after a restart and may take the slow interface; give the '
                'Thunderbolt Bridge a fixed address on both Macs (GUIDE.md, Peers)')
    if a.lower().startswith('fe80:') and '%' not in a: return f'{a} is an IPv6 link-local address and needs its interface: {a}%bridge0'
    return None


def reach_words(e):
    """Why this Mac could not reach a peer, in plain words."""
    r = getattr(e, 'reason', None)
    e = r if isinstance(r, BaseException) else e
    n = getattr(e, 'errno', None)
    if isinstance(e, ConnectionRefusedError) or n == errno.ECONNREFUSED:
        return "its door is closed (connection refused): is its hub running, and serving the link? (localmodels peer serve, on that Mac)"
    if n == errno.EHOSTUNREACH:
        return ("no route to it: if the cable is in and `localmodels peer test` reaches it from Terminal, macOS's Local Network "
                "permission is blocking the hub: System Settings → Privacy & Security → Local Network, turn on python3")
    if n == errno.EHOSTDOWN: return 'it is not on the link (host is down): asleep, or the cable is out'
    if n == errno.ENETUNREACH: return 'this Mac has no route to its network: is the Thunderbolt Bridge up, with its fixed address?'
    if isinstance(e, (socket.timeout, TimeoutError)) or n == errno.ETIMEDOUT: return 'no answer in time: asleep, busy, or the cable is out'
    if isinstance(e, (ConnectionResetError, BrokenPipeError)): return 'it dropped the connection'
    return f'{type(e).__name__}: {e}'


# ---------------------------------------------------------------- a model's identity across Macs
def definition(key):
    """A digest of a model's definition in LM Studio (model.yaml and manifest.json under ~/.lmstudio/hub/models/<key>/),
    or None when it has none (weights alone, or a model that is not from LM Studio's catalog)."""
    key = str(key or '')
    if not key or '..' in key.split('/') or key.startswith('/'): return None
    d = os.path.join(LMSTUDIO_HOME, 'hub', 'models', *key.split('/'))
    stamp = []
    for name in ('model.yaml', 'manifest.json'):
        try: stamp.append((name, os.path.getmtime(os.path.join(d, name)), os.path.getsize(os.path.join(d, name))))
        except OSError: pass
    if not stamp: return None
    hit = _cache.get(('def', d))
    if hit and hit[0] == stamp: return hit[1]
    h = hashlib.sha1()
    for name, _, _ in stamp:
        h.update(name.encode())
        try: h.update(open(os.path.join(d, name), 'rb').read())
        except OSError: pass
    _cache[('def', d)] = (stamp, h.hexdigest()[:12])
    return h.hexdigest()[:12]


def fingerprint(row):
    """What names a loaded model, from its row in the listing: {variant, bytes, format, definition}."""
    return dict(variant=row.get('variant') or row.get('key'), bytes=row.get('bytes'), format=row.get('format'), definition=definition(row.get('key')))


def match(mine, theirs, pinned=None):
    """Whether a peer's copy of a family is the same model as this Mac's. mine: this Mac's fingerprint (live, or the last
    seen), None when this Mac has never seen the family loaded; pinned: the variant the settings name, if any.
    -> (True, None) or (False, words)."""
    if not isinstance(theirs, dict): return False, 'it did not say which model it has'
    if pinned and theirs.get('variant') != pinned: return False, f"it has {theirs.get('variant')}, the settings name {pinned}"
    if mine is None:
        if pinned: return True, None
        return False, ("this Mac has not seen the family loaded, so the peer's copy cannot be checked against it: load it here "
                       'once, or name the variant in the settings ("variant": "<key>@<quantization>")')
    if theirs.get('variant') != mine.get('variant'): return False, f"it has {theirs.get('variant')}, this Mac {mine.get('variant')}: another model"
    if theirs.get('format') != mine.get('format'): return False, f"its format is {theirs.get('format')}, here {mine.get('format')}: another model"
    if theirs.get('bytes') != mine.get('bytes'): return False, f"its weights are {theirs.get('bytes')} bytes, here {mine.get('bytes')}: another model"
    if theirs.get('definition') != mine.get('definition'):
        if not theirs.get('definition'):
            return False, ("it has the weights but not the model's definition (model.yaml): copy ~/.lmstudio/hub/models/"
                           f"{(mine.get('variant') or '').split('@')[0]}/ to it, then load the model there again")
        if not mine.get('definition'): return False, "this Mac has the weights but not the model's definition (model.yaml); the peer has one"
        return False, "its definition (model.yaml) differs from this Mac's: another template or sampling"
    return True, None


# ---------------------------------------------------------------- the peer's side: what it tells a front
def capacity_for(fam, front):
    """How many requests a front may have in flight on a peer for one family: its slots, less those busy with other work
    (its own programs', other fronts'). fam: the family as report() gives it."""
    try:
        slots, busy = int(fam.get('slots') or 0), int(fam.get('busy') or 0)
        mine = int((fam.get('answering') or {}).get(front) or 0)
    except (AttributeError, TypeError, ValueError): return 0
    return max(0, slots - max(0, busy - mine))


def report(front=None):
    """This Mac as a peer sees itself (GET /hub/peer): {name, version, time, maintenance, listing_ok, families: {id:
    {copies, slots, busy, answering: {front: n}, holders: {project: n}, fingerprint, in_settings}}, keeper}. Never content."""
    from . import crashes, models, recycle
    rows, busy, fams = llmshare.loaded(), llmshare.busy_slots(), {}
    draining, conf = recycle.draining(), llmshare.settings().get('families') or {}
    for r in rows:
        base = llmshare.family(r['id'])
        f = fams.setdefault(base, dict(copies=[], slots=0, busy=0, answering=collections.Counter(), holders=collections.Counter(),
                                       fingerprint=None, in_settings=base in conf))
        down = r['id'] in crashes.down_copies(base)
        n = busy.get(llmshare._safe(r['id']), 0)
        usable = r['state'] != 'LOADING' and not down and r['id'] not in draining
        f['copies'].append(dict(id=r['id'], parallel=r.get('parallel') or 1, state=r['state'], busy=n, down=down, draining=r['id'] in draining))
        if usable: f['slots'] += max(1, r.get('parallel') or 1); f['busy'] += n
        if f['fingerprint'] is None or r['id'] == base: f['fingerprint'] = fingerprint(r)
    for m in llmshare.inflight():
        if m.get('state') != 'answering': continue
        f = fams.get(llmshare.family(m.get('copy') or m.get('model')))
        if not f: continue
        if m.get('from'): f['answering'][m['from']] += 1
        f['holders'][m.get('project') or '?'] += 1
    keeper = dict(gpu=models.gpu(), warnings=[])
    for base, f in fams.items():
        size = (f['fingerprint'] or {}).get('bytes')
        w = models.gpu_words(keeper['gpu'], size / 2**30) if size else None
        if w: keeper['warnings'].append(f'{base}: {w}')
        b = crashes.load_blocked(base, models.conditions())
        if b: keeper['warnings'].append(f'{base}: {b}')
    for base, w in recycle.held_back().items(): keeper['warnings'].append(f'{base}: {w}')
    return dict(hub='local-models', version=VERSION, name=me(), time=round(time.time(), 1), maintenance=maint.view(),
                listing_ok=llmshare.read_ok(), families={b: dict(f, answering=dict(f['answering']), holders=dict(f['holders'])) for b, f in fams.items()},
                keeper=keeper)


# ---------------------------------------------------------------- the front's side: the link
class Peer:
    """One peer as this Mac's door sees it."""
    def __init__(self, name):
        self.name, self.url, self.host = name, None, None
        self.state, self.why = 'new', 'not looked at yet'
        self.fails, self.next_at, self.seen_at, self.rtt = 0, 0.0, 0.0, None
        self.refused_token, self.refused_at, self.tries_refused = None, 0.0, 0
        self.report, self.route, self.route_at = None, (None, None), 0.0
        self.strikes = []                               # when requests failed there though the peer looked up
        self.inflight = collections.Counter()           # family -> this Mac's requests on the peer now
        self.cap = {}                                   # family -> how many this Mac may have there, from the last look
        self.match = {}                                 # family -> (True, None) or (False, words)


class Link:
    """This Mac's door and its peers: who is up, which families match, how many each may take now."""
    def __init__(self):
        self.lock, self.peers, self.order, self.mine, self.wrote = threading.RLock(), {}, [], {}, 0.0
        try: self.mine = (json.load(open(STATE)).get('mine') or {}) if os.path.exists(STATE) else {}
        except (OSError, ValueError): self.mine = {}

    def sync(self):
        """Peers added to or taken out of the settings since the last look."""
        conf = configured()
        with self.lock:
            for name in [n for n in self.peers if n not in conf]: del self.peers[name]
            for name, c in conf.items():
                p = self.peers.setdefault(name, Peer(name))
                if p.url != c['url']: p.url, p.host, p.state, p.next_at, p.route_at = c['url'], c['host'], 'new', 0.0, 0.0
            self.order = list(conf)
        return conf

    def poll(self, force=False):
        """Look at every peer that is due (force: all of them now), then write link.json."""
        had = bool(self.peers)
        self.sync()
        self._mine()
        tok, looked = token(), False
        for name in list(self.order):
            p = self.peers.get(name)
            if not p: continue
            now = time.time()
            if not force:
                if p.state == 'refused' and tok == p.refused_token and now - p.refused_at < REFUSED_AGAIN: continue
                if p.state != 'refused' and now < p.next_at: continue
            self._look(p, tok); looked = True
        if (self.peers or had or os.path.exists(STATE)) and (looked or force or time.time() - self.wrote > WRITE_EVERY): self.write()

    def _look(self, p, tok):
        t0 = time.time()
        if not tok:
            with self.lock: p.state, p.why, p.refused_token, p.refused_at = 'refused', token_problem() or 'this Mac has no token', tok, t0
            p.tries_refused += 1; return
        req = urllib.request.Request(p.url + '/hub/peer', headers={'X-Hub-Token': tok, 'X-Hub-From': me()})
        try:
            with _open(req, timeout=CONNECT) as r: rep = json.load(r)
            if not isinstance(rep, dict) or rep.get('hub') != 'local-models': raise ValueError('not a local-models hub')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                with self.lock: p.state, p.why, p.refused_token, p.refused_at = 'refused', f'it refuses this Mac\'s token ({e.code}): the two Macs must hold the same peer-token', tok, t0
                p.tries_refused += 1; return
            return self._down(p, f'its door answered {e.code}')
        except (OSError, ValueError) as e: return self._down(p, reach_words(e) if isinstance(e, OSError) else f'it did not answer as a hub ({e})')
        with self.lock:
            p.rtt, p.report, p.seen_at, p.fails, p.next_at, p.tries_refused = time.time() - t0, rep, time.time(), 0, time.time() + POLL, 0
            m = rep.get('maintenance') or {}
            p.state, p.why = ('maintenance', m.get('words') or 'down for maintenance') if m.get('phase') == 'on' else ('up', None)
            fams, conf, front = rep.get('families') or {}, llmshare.settings().get('families') or {}, me()
            p.cap = {b: capacity_for(f, front) for b, f in fams.items() if isinstance(f, dict)}
            p.match = {}
            for b, f in fams.items():
                if b not in conf: p.match[b] = (False, "not a family in this Mac's settings"); continue
                p.match[b] = match(self.mine.get(b), (f or {}).get('fingerprint'), (conf.get(b) or {}).get('variant'))
        if p.state == 'up' and time.time() - p.route_at > ROUTE_EVERY:
            p.route, p.route_at = route_of(p.host), time.time()

    def _down(self, p, why):
        with self.lock:
            p.fails += 1
            p.state, p.why = 'down', why
            p.next_at = time.time() + min(BACKOFF_MAX, POLL * 2 ** (p.fails - 1))
            p.cap = {}

    def _mine(self):
        """This Mac's own fingerprint of each family it has loaded now (the last one seen stands while none is)."""
        for r in llmshare.loaded():
            b = llmshare.family(r['id'])
            if r['id'] == b or b not in self.mine: self.mine[b] = fingerprint(r)

    # ---- the door's queue asks these
    def replicas(self, model, skip=()):
        """The copies on peers a request for this family may go to now (each peer is one copy, with the slots it has
        free for this Mac): peers up, the family matched, not skipped for this request."""
        fam = llmshare.family(model)
        if not self.peers or fam not in (llmshare.settings().get('families') or {}): return []
        with self.lock:
            return [cid(fam, n) for n in self.order if n in self.peers and n not in skip and self.peers[n].state == 'up'
                    and self.peers[n].match.get(fam, (False,))[0] and self.peers[n].cap.get(fam, 0) > 0]

    def take(self, c):
        """Hold one of a peer's slots for a request (llmshare.slot_any calls it): never more than the peer said it had free."""
        fam, name = split(c)
        with self.lock:
            p = self.peers.get(name)
            if not p or p.state != 'up' or p.inflight[fam] >= p.cap.get(fam, 0): return False
            p.inflight[fam] += 1
            return True

    def give(self, c):
        fam, name = split(c)
        with self.lock:
            p = self.peers.get(name)
            if p and p.inflight[fam] > 0: p.inflight[fam] -= 1

    def target(self, c):
        """Where to send a request for a copy on a peer, and the headers that let it in: (url, {header: value})."""
        _, name = split(c)
        with self.lock: p = self.peers.get(name)
        return (p.url if p else None), {'X-Hub-Token': token() or '', 'X-Hub-From': me()}

    def failed(self, c, code, words, headers=None):
        """A request sent to a peer got no answer from it (nothing reached the program): mark the peer for what it said.
        code 0: no connection; 401/403: the token; 503 with X-Hub-Maintenance on: its maintenance; else it could not
        take it now (no more is sent there until the next look)."""
        fam, name = split(c)
        hd = {k.lower(): v for k, v in (headers or {}).items()}
        with self.lock:
            p = self.peers.get(name)
            if not p: return
            mh = hd.get('x-hub-maintenance') or ''
            if code in (401, 403): p.state, p.why, p.refused_token, p.refused_at, p.cap = 'refused', f'it refused this Mac\'s token ({code})', token(), time.time(), {}
            elif mh.startswith('on'):
                p.state, p.why, p.cap = 'maintenance', 'down for maintenance' + (f" ({mh.split('why=', 1)[1]})" if 'why=' in mh else ''), {}
                p.next_at = time.time() + POLL
            elif code == 0: self._down(p, words)
            else:                                        # it could not take it now: nothing more until the next look
                p.cap[fam] = p.inflight.get(fam, 0) - 1 if p.inflight.get(fam, 0) > 0 else 0
                p.next_at = 0.0
                p.strikes = [t for t in p.strikes if t > time.time() - STRIKE_WINDOW] + [time.time()]
                if len(p.strikes) >= STRIKES:            # its look is fine but its requests fail: back off as if it were down
                    p.strikes = []; self._down(p, f'it failed {STRIKES} requests within {STRIKE_WINDOW} s ({words or code})')
        self.write(); WAKE.set()

    def answered(self, c):
        """A request on a peer was answered: its failures in a row start again."""
        _, name = split(c)
        with self.lock:
            p = self.peers.get(name)
            if p: p.strikes = []

    # ---- the screens
    def view(self):
        out = []
        with self.lock:
            for name in self.order:
                p = self.peers.get(name)
                if not p: continue
                fams = {}
                for b, f in ((p.report or {}).get('families') or {}).items() if p.state in ('up', 'maintenance') else ():
                    good, why = p.match.get(b, (False, None))
                    cap = p.cap.get(b, 0)
                    fams[b] = dict(match=good, why=why, slots=f.get('slots'), busy=f.get('busy'), cap=cap, here=p.inflight.get(b, 0),
                                   free_here=max(0, cap - p.inflight.get(b, 0)), copies=f.get('copies') or [], holders=f.get('holders') or {},
                                   answering=f.get('answering') or {}, fingerprint=f.get('fingerprint'))
                rep = p.report or {}
                d = dict(name=name, url=p.url, host=p.host, state=p.state, why=p.why, seen=round(p.seen_at, 1) if p.seen_at else None,
                         rtt_ms=round(p.rtt * 1000, 1) if p.rtt is not None else None, route=dict(interface=p.route[0], port=p.route[1]),
                         route_words=route_words(p.route[0], {p.route[0]: p.route[1]} if p.route[1] else {}) if p.route[0] else None,
                         address_words=address_words(p.host), next_at=round(p.next_at, 1), fails=p.fails, families=fams,
                         maintenance=rep.get('maintenance'), keeper=rep.get('keeper'), version=rep.get('version'), their_name=rep.get('name'))
                d['words'] = words(d)
                out.append(d)
        return out

    def write(self):
        try:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            llmshare._write(STATE, dict(at=round(time.time(), 1), me=me(), peers=self.view(), mine=self.mine))
            self.wrote = time.time()
        except OSError: pass


LINK = Link()


def poller():
    """The door's look at its peers: every second it looks at those that are due (each every 2 s while up), and at once
    after a request to one failed."""
    while True:
        try:
            if LINK.peers or configured(): LINK.poll()
        except Exception as e: print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} the look at the peers stumbled: {type(e).__name__}: {e}", flush=True)
        WAKE.wait(1.0); WAKE.clear()


# ---------------------------------------------------------------- every screen reads the front's look from link.json
def view(max_age=30):
    """The front's look at its peers, as the door last wrote it: [peer dicts], or [] when there are none (or the door has
    not looked for max_age seconds: its look is then stale, and each peer says so)."""
    try: d = json.load(open(STATE))
    except (OSError, ValueError): return []
    names = set(configured())
    out = [p for p in d.get('peers') or [] if p.get('name') in names]
    for p in out:
        if time.time() - float(d.get('at') or 0) > max_age: p.update(state='unknown', words=f"{p['name']}: the door has not looked for a while (is it open?)")
        else: p['words'] = words(p)                      # said now ("looks again in 12 s" counts from now)
    return out


def capacity(model):
    """How many requests at once this Mac's peers take for a family now (llmshare.at_once adds it): the peers up with
    the family matched, each with the slots it has free for this Mac."""
    fam = llmshare.family(model)
    return sum(int((p.get('families') or {}).get(fam, {}).get('cap') or 0) for p in view()
               if p.get('state') == 'up' and (p.get('families') or {}).get(fam, {}).get('match'))


def words(p):
    """One peer in a line: its state, the link, and each family it serves for this Mac."""
    name, st = p.get('name'), p.get('state')
    if st == 'new': return f'{name}: not looked at yet'
    if st == 'refused': return f"{name} refuses this Mac's token: {p.get('why')}"
    if st == 'down':
        left = max(0, int((p.get('next_at') or 0) - time.time()))
        return f"{name}: no answer ({p.get('why')}); looks again in {left} s"
    if st == 'unknown': return p.get('words') or f'{name}: unknown'
    if st == 'maintenance': return f"{name}: {p.get('why') or 'down for maintenance'}; nothing is sent there until it is over"
    link = (f"{p['rtt_ms']:.1f} ms" if p.get('rtt_ms') is not None else '') + \
           (f" over {p['route']['port'] or p['route']['interface']}" if (p.get('route') or {}).get('interface') and p['route']['interface'] != 'lo0' else '')
    bits = []
    for b, f in (p.get('families') or {}).items():
        if not f.get('match'): bits.append(f"{b} not used: {f.get('why')}"); continue
        bits.append(f"{b} {f.get('slots')} at once, {f.get('busy')} busy, {f.get('here')} from here, {f.get('free_here')} free for this Mac")
    warn = [w for w in (p.get('route_words'), p.get('address_words')) if w] + list((p.get('keeper') or {}).get('warnings') or [])
    return f"{name}: up{' (' + link.strip() + ')' if link.strip() else ''} · " + ('; '.join(bits) or 'no model loaded there') + \
        (' · ' + '; '.join(warn) if warn else '')
