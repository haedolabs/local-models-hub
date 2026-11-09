"""gateway - the hub's door: LM Studio's own API on http://localhost:1235, with the sharing rules applied to every request.

  localmodels gateway          # in the foreground; at login, launchd runs it (localmodels install)

For every request that makes a model work (chat, completions, embeddings, responses, messages):
  1. who: the project, the Claude Code session and the program, from the header X-Project (or a /p/<project>/ path),
     or else from the program on the other end of the connection (whois). X-Kind says what kind of run it is; X-Step
     what this request is for (the dash shows it); X-Run, X-Run-Total and X-Run-Done, the whole run it belongs to (runs.py).
  2. a lease for that program on that model, kept two minutes after its last request, so no swap unloads it mid-run.
  3. a slot (llmshare.slot_any): all projects together never send LM Studio more than the model answers at once. A chat
     or ask request (a person is waiting) gets the next free slot before queued batch requests; otherwise first come,
     first served. A request whose program hung up while it waited is dropped, not sent. A program that says how long
     it will wait (X-Timeout: its own timeout in seconds; llmshare.post sends it) gets 503 with Retry-After at nine
     tenths of that, so the door's wait never outlives the program's.
  4. LM Studio answers; a streamed answer streams through as it comes. If the program hangs up during a long answer,
     the door hangs up on LM Studio too, which stops generating.
  5. one line in requests.jsonl: project, session, kind, model, seconds, waited, token counts. Never the content.
A model loaded as several copies (a family in models.json: two copies of 4 by default) is one model here: a request for it
goes to the first copy with a free slot, the first copy's slots filling before the second's, and the answer names the
model asked for. A copy that is down after a crash, or being drained for a recycle, gets nothing new while the others
answer; only when no copy is left to answer is the request held (up to 10 minutes) for the keeper to bring one back.
When LM Studio answers that the model crashed or is not loaded, or its server drops the connection, the door does not
pass that on: it waits for the keeper and sends the request once more; if that fails too, it answers 503 with
Retry-After. A genuine 400 (a bad request) passes through. The door's keeper keeps the copies loaded, loads a crashed
model again (crashes.py) and recycles copies before the crash band (recycle.py); while any program is using a model it
holds the Mac awake (caffeinate). Loading or unloading a model through the door is refused while another run is using
it (rule 3). Everything else passes straight through. If LM Studio's server is off, the door starts it (never when it is
already up: that restarts it) and tries once more.
Serves this Mac only (127.0.0.1 and ::1) and refuses web pages from other sites (Host, Origin and Sec-Fetch-Site are
checked: a browser's request from another site or port is refused, except a link that opens the dashboard page).

Peers (peers.py): a family a peer Mac has loaded with the same fingerprint is one more copy, after this Mac's own: the
next request goes to a peer only when every slot here is busy, never more than the peer said it has free for this Mac,
with the project, session, kind, step and run sent along. A peer that cannot take it (its door closed, its maintenance,
the token refused, no slot in time) answers before any byte reaches the program, so the request is sent again here. A Mac
that serves as a peer ("serve" in its settings) also listens on that address, bound to its interface, where it answers
only requests with the shared token, and only a model's work, the model listing and its own report (over_link).
"""
import http.client, http.server, itertools, json, os, re, resource, select, socket, subprocess, sys, threading, time, traceback, urllib.parse, urllib.request

from . import VERSION, admission, crashes, errors, icon, llmshare, maint, peers, recycle, runs, web, whois

llmshare.REMOTE = peers.LINK                            # slots on peers' copies are counted by this process's link
PORT = llmshare.HUB_PORT
UP = llmshare.LMSTUDIO
LINK_PASS = ('X-Project', 'X-Session', 'X-Kind', 'X-Step', 'X-Run', 'X-Run-Total', 'X-Run-Done')   # who a request is from, sent along to a peer
GEN = re.compile(r'/(chat/completions|completions|embeddings|responses|messages|chat)$')
SWAP = re.compile(r'/(load|unload)$')
PASS = ('content-type', 'authorization', 'accept', 'x-api-key', 'anthropic-version', 'openai-organization')
GONE = re.compile(r'crashed|no models? (?:are |is )?loaded|not loaded|model_not_found|has been unloaded|model unloaded', re.I)
# The engine's own failures (crashes.ENGINE, one list with the crash classifier's), which LM Studio answers with HTTP 400 while
# the instance may still be listed for a moment: "Channel Error" is what every request in flight gets when the generation
# thread dies; "[METAL] ... Insufficient Memory (...kIOGPUCommandBufferCallbackErrorOutOfMemory)" is the engine out of memory.
LOG_MAX, RETRY_AFTER, AWAKE_FOR = 20_000_000, 30, 600
# How each of LM Studio's APIs ends a stream that came out whole: OpenAI's chat and completions with `data: [DONE]`, the
# others with a last event of their own (Anthropic's messages, OpenAI's responses, LM Studio's own chat). An error event
# is an ending too: LM Studio said what went wrong, so nothing is cut short.
LAST_EVENTS = frozenset((b'message_stop', b'response.completed', b'response.incomplete', b'response.failed', b'chat.end', b'error'))
FAILED_EVENTS = frozenset((b'response.failed', b'error'))    # whole, but the keeper looks: a crash can end a stream this way
# What the door adds when a stream breaks off, in the stream's own format, so the program's own client reads it as an error.
CUT = {
    'openai': b'data: {"error": "LM Studio stream truncated (no terminal event)"}\n\ndata: [DONE]\n\n',
    'anthropic': b'event: error\ndata: {"type": "error", "error": {"type": "api_error", "message": "LM Studio stream truncated (no terminal event)"}}\n\n',
    'responses': b'event: error\ndata: {"type": "error", "code": "server_error", "message": "LM Studio stream truncated (no terminal event)"}\n\n',
    'native': b'event: error\ndata: {"type": "error", "error": {"type": "server_error", "message": "LM Studio stream truncated (no terminal event)"}}\n\n',
}
ICONS = {'/favicon.ico': 64, '/favicon.png': 64, '/apple-touch-icon.png': 180, '/apple-touch-icon-precomposed.png': 180}   # path -> pixels
ANTHROPIC_ERROR = {400: 'invalid_request_error', 403: 'permission_error', 404: 'not_found_error', 409: 'invalid_request_error', 503: 'overloaded_error'}
_n, _lock, _live = itertools.count(1), threading.Lock(), {}
_start = {'at': 0.0, 'since': time.time(), 'lock': threading.Lock()}


# ---------------------------------------------------------------- the hub's records
def log(rec):
    """One line per request in requests.jsonl (the older half rolls over to requests.jsonl.1 past 20 MB)."""
    llmshare._append(llmshare.REQUESTS, rec, roll_at=LOG_MAX)


def touch(pid, project, kind, model, who, delta, front=None):
    """The hub's lease for a program that sends it requests: written at each request, refreshed while one is in flight,
    and left to age out two minutes after the last. front: the peer Mac that sent it over the link (one lease per front
    and project, so a swap here is refused while a front's batch uses the model)."""
    path = os.path.join(llmshare.LEASES, f'{llmshare._safe(project)}-{pid}' + (f'-{llmshare._safe(front)}' if front else '') + '-hub.json')
    with _lock:
        e = _live.setdefault(path, dict(n=0, rec=dict(v=llmshare.VERSION, project=project, pid=pid, models=[], kind=kind,
                                                       since=time.time(), via='hub', app=who.get('app'), **({'from': front} if front else {}))))
        e['n'] = max(0, e['n'] + delta)
        rec = e['rec']
        if model and model not in rec['models']: rec['models'].append(model)
        for k in ('session', 'host_session'):
            if who.get(k): rec[k] = who[k]
        if kind != 'other': rec['kind'] = kind
        rec['beat'] = time.time()
        try:
            os.makedirs(llmshare.LEASES, exist_ok=True); llmshare._write(path, rec)
        except OSError: pass


def beats():
    while True:
        time.sleep(llmshare.BEAT)
        with _lock:
            for path, e in list(_live.items()):
                if e['n'] > 0:
                    e['rec']['beat'] = time.time()
                    try: llmshare._write(path, e['rec'])
                    except OSError: pass
                elif time.time() - e['rec'].get('beat', 0) > llmshare.STALE:
                    _live.pop(path, None)
                    try: os.remove(path)
                    except OSError: pass


def own_kind(pid):
    """The kind of run a program says it is doing, from its own lease (a batch, a chat...), if it holds one."""
    for f in glob_leases(pid):
        try: return json.load(open(f)).get('kind')
        except (OSError, ValueError): pass
    return None


def glob_leases(pid):
    import glob
    return [f for f in glob.glob(os.path.join(llmshare.LEASES, f'*-{pid}-*.json')) if not f.endswith('-hub.json')]


def server_up(timeout=3):
    """Whether LM Studio's server is up: only a refused connection means down. A slow answer, a reset or a cut answer
    means it is up and busy, and starting it then would restart it under every request in flight."""
    try: urllib.request.urlopen(UP + '/v1/models', timeout=timeout); return True
    except urllib.error.URLError as e: return not isinstance(getattr(e, 'reason', None), ConnectionRefusedError)
    except ConnectionRefusedError: return False
    except OSError: return True


def start_lmstudio(why='a request could not connect'):
    if maint.on(): return False                              # maintenance: the person is working on LM Studio
    """LM Studio's server, started only when it is down (starting a running one restarts it and cuts every request in
    flight). Threads that find it down at the same moment wait for the one start. -> whether it answers now."""
    if server_up(2): return True
    with _start['lock']:
        if server_up(2): return True
        if time.time() - _start['at'] < 60: return False
        _start['at'] = time.time()
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} LM Studio's server is down ({why}): lms server start", flush=True)
        try: subprocess.run([llmshare.LMS, 'server', 'start'], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError): return False
        for _ in range(20):
            if server_up(2): return True
            time.sleep(0.5)
        return False


def dialect(route):
    """Which of LM Studio's APIs a route is: 'anthropic' (/v1/messages), 'responses' (/v1/responses), 'native'
    (LM Studio's own /api/...), or 'openai' (chat, completions, embeddings)."""
    if route.endswith('/messages'): return 'anthropic'
    if route.endswith('/responses'): return 'responses'
    if route.startswith('/api/'): return 'native'
    return 'openai'


def counts(obj):
    """The token counts in an answer or in one stream event, wherever its API puts them: `usage` (OpenAI, Anthropic's
    message_delta), `message.usage` (Anthropic's message_start), `response.usage` (OpenAI's responses), `stats` or
    `result.stats` (LM Studio's own). -> {'prompt_tokens': n, 'completion_tokens': n}, holding only what it says."""
    if not isinstance(obj, dict): return {}
    inner = lambda k, sub: obj.get(k).get(sub) if isinstance(obj.get(k), dict) else None
    out = {}
    for u in (obj.get('usage'), inner('message', 'usage'), inner('response', 'usage'), obj.get('stats'), inner('result', 'stats')):
        if not isinstance(u, dict): continue
        p = u.get('prompt_tokens') or u.get('input_tokens')
        c = u.get('completion_tokens') or u.get('output_tokens') or u.get('total_output_tokens')
        if isinstance(p, int): out['prompt_tokens'] = max(p, out.get('prompt_tokens', 0))
        if isinstance(c, int): out['completion_tokens'] = max(c, out.get('completion_tokens', 0))
    return out


def merge(a, b):
    """Counts from two stream events together: each count is the largest said (streams repeat or grow them, never shrink)."""
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in set(a) | set(b)}


def _usage(u):
    u = u or {}
    return (u.get('prompt_tokens') or u.get('input_tokens') or 0), (u.get('completion_tokens') or u.get('output_tokens') or 0)


def client_gone(sock):
    """Whether the program on the other end has hung up (its socket reads as closed). It sent its whole request already,
    so anything readable now is the end."""
    try:
        r, _, _ = select.select([sock], [], [], 0)
        return bool(r) and sock.recv(1, socket.MSG_PEEK) == b''
    except (OSError, ValueError): return True


def known(model):
    """Whether the keeper knows this model (a family in the settings, or one it has seen loaded): then 'not loaded' from
    LM Studio means a crash, not a typo."""
    base = llmshare.family(model)
    if base in (llmshare.settings().get('families') or {}): return True
    return base in {llmshare.family(i) for i in (crashes._read().get('seen') or {})}


def _keepalive(sock):
    """TCP keepalive on a connection to a peer: a link that dies during a long answer is noticed in about 25 s (the
    peer's system answers the probes even while its model is busy), not after the hour a read may take."""
    for level, opt, val in ((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1), (socket.IPPROTO_TCP, getattr(socket, 'TCP_KEEPALIVE', 0x10), 10),
                            (socket.IPPROTO_TCP, getattr(socket, 'TCP_KEEPINTVL', 0x101), 5), (socket.IPPROTO_TCP, getattr(socket, 'TCP_KEEPCNT', 0x102), 3)):
        try: sock.setsockopt(level, opt, val)
        except OSError: pass


class Upstream:
    """One request to LM Studio (or to a peer's door: base) over our own connection, so a thread watching the program
    can cut it short."""
    def __init__(self, method, path, body, headers, base=None):
        u = urllib.parse.urlsplit(base or UP)
        if base:                                         # a peer: connect within seconds, then wait as long as the answer takes
            self.conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=peers.CONNECT)
            self.conn.connect(); _keepalive(self.conn.sock); self.conn.sock.settimeout(3600)
        else: self.conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=3600)
        self.conn.request(method, path, body=body, headers=headers)
        self.resp = None

    def wait(self):
        self.resp = self.conn.getresponse()
        return self.resp

    def abort(self):
        try: self.conn.sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError): pass
        self.close()

    def close(self):
        try: self.conn.close()
        except OSError: pass


# ---------------------------------------------------------------- the door
class Door(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = f'localmodels-hub/{VERSION}'
    _sent, _dialect, _link = False, 'openai', None      # _link: the front's name, for a request that came over the link

    def log_message(self, *a): pass

    def end_headers(self):
        try:                                                 # every answer says when maintenance is announced or on
            h = maint.header(maint.read())
            if h: self.send_header('X-Hub-Maintenance', h)
        except Exception as e: errors.record('maintenance header', e)
        super().end_headers()

    def _send(self, code, obj, retry_after=None):
        if self._dialect == 'anthropic' and code >= 400 and isinstance(obj, dict) and isinstance(obj.get('error'), str):
            obj = dict(type='error', error=dict(type=ANTHROPIC_ERROR.get(code, 'api_error'), message=obj['error']))   # as Anthropic's SDKs read it
        b = json.dumps(obj).encode()
        try:
            self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b)))
            if retry_after: self.send_header('Retry-After', str(int(retry_after)))
            self.end_headers(); self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError, OSError): pass
        self._sent = True
        return code, {}

    def _raw(self, code, data, ctype):
        try:
            self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError): pass
        self._sent = True
        return code, {}

    def _allowed(self):
        """A loopback Host; an Origin, if any, that is the gateway's own; and from a browser (Sec-Fetch-Site, which
        programs do not send) only the dashboard's own requests or an address typed in. A browser sends no Origin on a
        cross-site <img>, <script>, <iframe> or link, so the Origin alone lets those through; of them only a top-level
        navigation to the dashboard page is served, so a link to it from another site still opens it."""
        host = (self.headers.get('Host') or '').lower()
        if host not in (f'localhost:{PORT}', f'127.0.0.1:{PORT}', f'[::1]:{PORT}'): return False
        o = self.headers.get('Origin')
        if o and not re.match(rf'^https?://(localhost|127\.0\.0\.1|\[::1\]):{PORT}$', o): return False
        site = (self.headers.get('Sec-Fetch-Site') or '').lower()
        if site in ('', 'same-origin', 'none'): return True
        return (self.command == 'GET' and self.path.split('?')[0] == '/' and (self.headers.get('Sec-Fetch-Mode') or '').lower() == 'navigate'
                and (self.headers.get('Sec-Fetch-Dest') or '').lower() == 'document')

    def _body(self):
        """The request body: by Content-Length, or chunked (some HTTP libraries send it so)."""
        if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
            out = bytearray()
            while True:
                line = self.rfile.readline(65537)
                try: size = int(line.split(b';')[0].strip() or b'0', 16)
                except ValueError: break
                if size <= 0:
                    while self.rfile.readline(65537) not in (b'\r\n', b'\n', b''): pass
                    break
                out += self.rfile.read(size); self.rfile.readline(3)
                if len(out) > 64_000_000: break
            return bytes(out)
        try: n = int(self.headers.get('Content-Length') or 0)
        except ValueError: n = 0
        return self.rfile.read(n) if n > 0 else b''

    def do_GET(self): self.any('GET')
    def do_POST(self): self.any('POST')
    def do_PUT(self): self.any('PUT')
    def do_DELETE(self): self.any('DELETE')
    def do_OPTIONS(self): self._send(403, dict(error='forbidden: local clients only (Host must be localhost; cross-origin browser requests are rejected)'))

    def any(self, method):
        try: self._any(method)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as e:                               # never a bare reset: the client gets an answer, the error record the reason
            errors.record(f"{method} {self.path.split('?')[0]}", e)
            if not self._sent: self._send(500, dict(error=f'internal gateway error: {type(e).__name__}: {e}'))

    def _any(self, method):
        if getattr(self.server, 'link', False): return self.over_link(method)
        if not self._allowed(): return self._send(403, dict(error='forbidden: local clients only (Host must be localhost; cross-origin browser requests are rejected)'))
        path, project = self.path, self.headers.get('X-Project')
        if path.startswith('/hub/'): return self.hub(method, path)
        if method == 'GET' and path.split('?')[0] in ('/', '/dash', '/dash/'): return self._raw(200, web.page(), 'text/html; charset=utf-8')
        if method == 'GET' and path.split('?')[0] in ICONS: return self._raw(200, icon.png(ICONS[path.split('?')[0]]), 'image/png')
        m = re.match(r'^/p/([A-Za-z0-9._ -]{1,80})(/.*)$', path)
        if m: project, path = project or m.group(1), m.group(2)
        body = self._body()
        try: j = json.loads(body) if body else {}
        except ValueError: j = {}
        if not isinstance(j, dict): j = {}
        route = path.split('?')[0].rstrip('/')
        path = route + ('?' + path.split('?', 1)[1] if '?' in path else '')   # LM Studio's router knows no trailing slash
        route = route.lower()
        self._dialect = dialect(route)
        if method == 'POST' and SWAP.search(route):
            names = [str(j[k]) for k in ('model', 'identifier', 'instance_id') if j.get(k)]
            why = llmshare.swap_blockers(names or [r['id'] for r in llmshare.loaded(fresh=True)])
            if why: return self._send(409, dict(error='swap refused: ' + '; '.join(why) + '. Retry when it finishes.'))
        if method == 'POST' and GEN.search(route): return self.generate(path, body, j, project)
        return self.forward(method, path, body, False)

    def over_link(self, method):
        """A request from a peer's hub (this Mac serves as a peer): only with the shared token, never from a web page, and
        only what a front needs: a model's work, the model listing, and this Mac's report. Loads, unloads, recycles,
        maintenance and the dashboard stay this Mac's own business."""
        if self.headers.get('Origin'): return self._send(403, dict(error='forbidden: requests with an Origin header are not accepted on the peer link'))
        if not peers.token_ok(self.headers.get('X-Hub-Token')):
            return self._send(401, dict(error='unauthorized: missing or invalid X-Hub-Token (peer-token)'))
        if (self.headers.get('Host') or '').lower() not in self.server.hosts: return self._send(403, dict(error='forbidden: Host does not match the link listener address'))
        self._link = llmshare.clean(self.headers.get('X-Hub-From'), 40) or 'a peer'
        path = self.path
        route = path.split('?')[0].rstrip('/').lower()
        if method == 'GET' and route == '/hub/peer': return self._send(200, peers.report(self._link))
        if method == 'GET' and route == '/hub/health':
            return self._send(200, dict(ok=True, hub='local-models', version=VERSION, name=peers.me(), maintenance=maint.view()))
        if method == 'GET' and route in ('/v1/models', '/api/v0/models', '/api/v1/models'): return self.forward('GET', path, b'', False)
        if method == 'POST' and GEN.search(route) and not SWAP.search(route):
            body = self._body()
            try: j = json.loads(body) if body else {}
            except ValueError: j = {}
            if not isinstance(j, dict): j = {}
            self._dialect = dialect(route)
            return self.generate(path.split('?')[0].rstrip('/') + ('?' + path.split('?', 1)[1] if '?' in path else ''), body, j, self.headers.get('X-Project'))
        return self._send(403, dict(error='forbidden on the peer link: only inference endpoints, the model list, /hub/peer and /hub/health are served'))

    # ---- a request that makes a model work
    def _candidates(self, model, skip=(), kind=None):
        """The copies this request may go to right now, in the order it takes them: the model's loaded copies, less those
        down after a crash or being drained for a recycle, and the family's copies on peers (peers.LINK: those up, with
        the same model, and not skipped because this request failed there). A person waiting (chat, ask) takes this Mac's
        copies first, the fastest per request; anything else takes each Mac's first copy before a second copy on any one
        Mac, since a peer's GPU and memory are its own (measured: a second copy here added 23%, a peer 38%; GUIDE.md,
        Peers). A request that names one copy goes to that copy, or to its siblings when that copy is down or draining.
        [] when nothing can answer: the door then holds, or answers 503, never forwards the bare id. A request that came
        over the link is answered here only (no chains of Macs)."""
        out = [c for c in llmshare.copies(model) if c not in crashes.down_copies(model) and c not in recycle.draining()]
        if not out and llmshare.family(model) != model:
            out = [c for c in llmshare.copies(llmshare.family(model)) if c not in crashes.down_copies(model) and c not in recycle.draining()]
        if not self._link and (llmshare.family(model) == model or not out):
            there = peers.LINK.replicas(model, skip)
            if there and len(out) > 1 and kind not in llmshare.PEOPLE:
                first = [c for c in there if peers.LINK.first_free(c)]
                out = out[:1] + first + out[1:] + [c for c in there if c not in first]
            else: out += there
        return out

    def _hold(self, model, rec, mark, t0, until=None):
        """Wait while no copy can answer: the model crashed and is loading again, or the only copy is being recycled.
        Up to HOLD seconds in all, or until the program's own budget runs out. -> seconds held."""
        h0 = time.time()
        until = min(t0 + crashes.HOLD, until) if until else t0 + crashes.HOLD
        while not self._candidates(model) and time.time() < until:
            if crashes.reloading(model): state = 'held: reloading after a crash'
            elif recycle.draining() & set(llmshare.copies(llmshare.family(model))) or model in recycle.draining(): state = 'held: instance being recycled'
            else: break                                   # nothing loaded and nothing on the way: generate answers at once
            if rec.get('state') != state:
                rec.update(state=state)
                try: llmshare._write(mark, rec)
                except OSError: pass
            if client_gone(self.connection): break
            time.sleep(1)
        return time.time() - h0

    def generate(self, path, body, j, project):
        t0, host, front = time.time(), self.client_address[0], self._link
        if front:                                          # over the link: the front named the program; nothing here to look up
            pid, who = 0, dict(app=f'peer gateway {front}')
        else:
            pid = whois.peer_pid(self.client_address[1], PORT, '[::1]' if ':' in host else '127.0.0.1') or 0
            who = whois.identity(pid) if pid else {}
        project = llmshare.clean(project or who.get('project') or 'unknown', 80) or 'unknown'
        who = dict(who, session=llmshare.clean(self.headers.get('X-Session') or who.get('session'), 120) or None)
        kind = llmshare.clean(self.headers.get('X-Kind'), 20).lower()
        if kind not in llmshare.KINDS: kind = (own_kind(pid) if pid else None) or 'other'
        step = llmshare.clean(self.headers.get('X-Step'), 120) or None
        run = runs.from_headers(self.headers)                                  # X-Run, X-Run-Total, X-Run-Done: the whole run
        model = llmshare.clean(j.get('model') or next((llmshare.family(r['id']) for r in llmshare.loaded()), None) or 'unknown', 200)
        try: xt = float(self.headers.get('X-Timeout') or 0)
        except ValueError: xt = 0
        until = t0 + 0.9 * min(86400.0, max(5.0, xt)) if xt > 0 else None     # the program's own budget: never wait past it
        holder = pid or os.getpid()
        touch(holder, project, kind, model, who, +1, front)
        mark = os.path.join(llmshare.INFLIGHT, f'{os.getpid()}-{next(_n)}.json')
        rec = dict(hub_pid=os.getpid(), pid=pid, project=project, session=who.get('session'), host_session=who.get('host_session'),
                   kind=kind, model=model, app=who.get('app'), since=t0, state='queued', **({'step': step} if step else {}), **run,
                   **({'via': 'link', 'from': front} if front else {}))
        try: os.makedirs(llmshare.INFLIGHT, exist_ok=True); llmshare._write(mark, rec)
        except OSError: pass
        code, usage, waited, chosen, held, note, tries, peer = 0, {}, 0.0, model, 0.0, None, 0, None
        sent_along = {k: llmshare.clean(v, 120) for k, v in ((k, self.headers.get(k)) for k in LINK_PASS) if v}
        sent_along.update({'X-Project': project, 'X-Kind': kind, **({'X-Session': who['session']} if who.get('session') else {})})
        need, room = admission.estimate(body, j, admission.window(model), not path.split('?')[0].rstrip('/').endswith('embeddings')), [None]   # its context; why it last waited for room

        def waiting(unfit):                                # admission: the copies it waits on for room, not for a slot
            words = admission.why_waiting(unfit, llmshare.load_here(), need) if unfit else None
            if words: room[0] = words
            if rec.get('wait_for') != words:
                if words: rec['wait_for'] = words
                else: rec.pop('wait_for', None)
                try: llmshare._write(mark, rec)
                except OSError: pass
        try:
            base = llmshare.family(model)
            m = maint.on()
            if m:
                ra = maint.retry_after(m)
                code, _ = self._send(503, dict(error=f'service unavailable: {maint.words(m)}; retry in {ra} s'), retry_after=ra)
                note = 'maintenance'
            elif crashes.left_down(model) and not self._candidates(model):
                code, _ = self._send(503, dict(error=f'{base} unavailable: crash recovery stopped after repeated failures; the instance manager retries hourly, or load it now: localmodels use {base}'), retry_after=600)
                note = 'unloaded after crashes'
            else:
                attempt, skip = 1, set()                       # skip: the peers this request could not be sent to
                while True:
                    tries += 1
                    held += self._hold(model, rec, mark, t0, until)
                    if client_gone(self.connection): code, note = 499, 'client closed the connection while queued'; break
                    if not self._candidates(model, skip):  # the hold ran out, or no copy is loaded and none is on the way
                        why = (f'{base} is still reloading after a crash' if crashes.reloading(model) else
                               f'{base} is being recycled' if recycle.draining() else f'no instance of {base} is loaded (localmodels use {base})')
                        code, _ = self._send(503, dict(error=f'{why}; retry in {RETRY_AFTER} s'), retry_after=RETRY_AFTER); note = why; break
                    with llmshare.slot_any(lambda: self._candidates(model, skip, kind), wait=max(1.0, min(3600.0, until - time.time())) if until else 3600,
                                           kind=kind, need=need, fits=admission.fits, waiting=waiting) as (chosen, w):
                        waited += w
                        if client_gone(self.connection): code, note = 499, 'client closed the connection while queued'; break
                        m = maint.on()
                        if m:                                       # maintenance began while it waited for its turn
                            ra = maint.retry_after(m)
                            code, _ = self._send(503, dict(error=f'service unavailable: {maint.words(m)}; retry in {ra} s'), retry_after=ra)
                            note = 'maintenance'; break
                        rec.pop('wait_for', None)
                        rec.update(state='answering', answering_since=time.time(), copy=chosen, ctx=need)   # ctx: recycle.plan counts it
                        try: llmshare._write(mark, rec)
                        except OSError: pass
                        peer = peers.split(chosen)[1]
                        if peer:                                    # a copy on a peer: it is asked for the family, and names it back
                            sent = json.dumps(dict(j, model=base)).encode()
                            code, usage, why = self.forward('POST', path, sent, bool(j.get('stream')), (base, model) if base != model else None, model, chosen, sent_along)
                        else:
                            sent = json.dumps(dict(j, model=chosen)).encode() if chosen != j.get('model') else body
                            code, usage, why = self.forward('POST', path, sent, bool(j.get('stream')), (chosen, model) if chosen != model else None, model)
                    if not why:                                         # answered, or a genuine error passed through
                        if peer: peers.LINK.answered(chosen)
                        note = getattr(self, '_note', None) or note; break
                    if peer and not self._sent and code != 499 and len(skip) < 16:   # the peer could not take it, and nothing reached
                        skip.add(peer); note = f'requeued locally: {why}'             # the program: back in the queue, here or another peer
                        rec.update(state='queued', copy=None)
                        try: llmshare._write(mark, rec)
                        except OSError: pass
                        continue
                    note = why; crashes.nudge()
                    if attempt == 2 or code == 499: break
                    attempt += 1
                    rec.update(state='held: reloading after a crash', copy=None)
                    try: llmshare._write(mark, rec)
                    except OSError: pass
                    for _ in range(15):                                 # the keeper notices within seconds; _hold waits for the rest
                        if crashes.reloading(model) or not self._candidates(model, skip): break
                        time.sleep(1)
                if note and not self._sent and code != 499:
                    code, _ = self._send(503, dict(error=f'no response from the model ({note}); the instance manager is reloading it; retry in {RETRY_AFTER} s'), retry_after=RETRY_AFTER)
        except TimeoutError as e:
            waited = max(0.0, time.time() - t0 - held)                     # the row says what it waited, not that it answered
            code, _ = self._send(503, dict(error=str(e) + ('; the client X-Timeout budget expired' if until else '')), retry_after=60); note = 'slot wait timed out'
        finally:
            try: os.remove(mark)
            except OSError: pass
            touch(holder, project, kind, model, who, -1, front)
            if code >= 400 and code != 499 and note != 'maintenance' and not peers.split(chosen)[1]: crashes.nudge()   # maybe the model crashed: the keeper looks at once
            p, c = _usage(usage)
            log(dict(at=round(time.time(), 1), project=project, session=who.get('session'), host_session=who.get('host_session'),
                     kind=kind, model=model, copy=chosen, endpoint=path.split('?')[0].rstrip('/').rsplit('/', 1)[-1], status=code, stream=bool(j.get('stream')),
                     seconds=round(time.time() - t0, 2), waited=round(held + (waited or 0), 2), prompt_tokens=p, completion_tokens=c,
                     bytes=admission.prompt_bytes(body), ctx=need, **({'wait_for': room[0]} if room[0] else {}),   # sizes only, never content
                     **({'held': round(held, 2)} if held else {}), **({'note': note} if note else {}), **({'step': step} if step else {}),
                     **({'tries': tries} if tries > 1 else {}), **run, **({'peer': peers.split(chosen)[1]} if peers.split(chosen)[1] else {}),
                     **({'from': front} if front else {})))

    def forward(self, method, path, body, stream, rename=None, model=None, peer=None, along=None):
        """Pass one request to LM Studio and its answer back; rename=(copy, model): the answer names the model asked
        for, not the copy that answered. -> (status, usage, why): why is None when the program got an answer (or a genuine
        error), else words for a failure the program did not see: a crashed or unloaded model, a dropped connection.
        peer: a copy on a peer ('<family> on <peer>'): the request goes to that Mac's door with the token and along (who it
        is from), and why is also set when the peer could not take it (its door closed, its maintenance, the token, no
        slot in time): nothing reached the program, so the door sends it again here."""
        a, b = (f'"{rename[0]}"'.encode(), f'"{rename[1]}"'.encode()) if rename else (None, None)
        headers = {k: v for k, v in self.headers.items() if k.lower() in PASS}
        if method in ('POST', 'PUT'): headers['Content-Length'] = str(len(body))
        name, target = (peers.split(peer)[1], None) if peer else (None, None)
        if peer:
            target, extra = peers.LINK.target(peer)
            if not target: return 502, {}, f'peer {name}: removed from settings'
            headers.update(along or {}); headers.update(extra); headers['X-Timeout'] = str(peers.PEER_WAIT)
        who = f'peer {name}' if name else 'LM Studio'
        up = None
        for attempt in (1, 2):
            try: up = Upstream(method, path, body if method in ('POST', 'PUT') else None, headers, base=target); break
            except OSError as e:
                if peer:
                    w = peers.reach_words(e); peers.LINK.failed(peer, 0, w)
                    return 502, {}, f'peer {name}: {w}'
                if attempt == 1 and start_lmstudio(): continue
                if model: return 502, {}, f"LM Studio server unreachable at {UP} ({e})"
                code, _ = self._send(502, dict(error=f"LM Studio server unreachable at {UP} ({e}): start it in LM Studio's Developer tab, or run: lms server start"))
                return code, {}, None
        gone, done = [False], threading.Event()
        if model and not stream:
            def watch():                                             # the program may hang up during a long answer
                while not done.wait(1):
                    if client_gone(self.connection): gone[0] = True; up.abort(); return
            threading.Thread(target=watch, daemon=True).start()
        try: resp = up.wait()
        except (http.client.HTTPException, OSError) as e:
            done.set(); up.close()
            if gone[0]: return 499, {}, 'client closed the connection during the response'
            if peer:
                w = peers.reach_words(e) if isinstance(e, OSError) else f'connection dropped before the response ({type(e).__name__})'
                peers.LINK.failed(peer, 0, w)
                return 502, {}, f'peer {name}: {w}'
            if model: return 502, {}, f'LM Studio closed the connection before responding ({type(e).__name__})'
            code, _ = self._send(502, dict(error=f'LM Studio closed the connection before responding ({type(e).__name__})'))
            return code, {}, None
        finally: done.set()
        code, ctype, usage = resp.status, resp.getheader('Content-Type') or 'application/json', {}
        try:
            if code >= 400 and peer and (code in (401, 403, 404, 408, 409, 429) or code >= 500):   # the peer did not take it
                data = resp.read(200_000)
                try: said = json.loads(data).get('error')
                except (ValueError, AttributeError): said = None
                said = said.get('message') if isinstance(said, dict) else said
                peers.LINK.failed(peer, code, str(said or '')[:200], dict(resp.getheaders()))
                return code, {}, f'peer {name} returned HTTP {code}' + (f' ({str(said)[:160]})' if said else '')
            if code >= 400:
                data = resp.read(200_000)
                text = data.decode('utf-8', 'replace')
                failed = crashes.engine_failure(text)
                if model and not peer and known(model) and (code < 500 or failed):   # a 5xx in the engine's words is a crash too
                    if re.search(r'crashed', text, re.I): return code, {}, f'LM Studio returned HTTP {code}: model crashed'
                    if failed: return code, {}, f'LM Studio returned HTTP {code}: engine failure ({failed})'
                    if GONE.search(text): return code, {}, f'LM Studio returned HTTP {code}: model not loaded'
                    asked = rename[0] if rename else model                  # the words are new: is the copy that answered still there?
                    there = llmshare.api_models()
                    rows = {r['id'] for r in llmshare.loaded(fresh=True, force=True)}
                    if (there is not None and asked not in there) or (there is None and asked not in rows):
                        return code, {}, f'LM Studio returned HTTP {code}; the instance is no longer listed'
                self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data); self._sent = True
                return code, {}, None
            if stream and 'event-stream' in ctype:
                self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Cache-Control', 'no-cache')
                self.end_headers(); self._sent = True
                whole = failed = False
                while True:
                    try: line = resp.readline()
                    except (http.client.HTTPException, OSError): line = None   # LM Studio's end broke off
                    if not line: break
                    if a: line = line.replace(a, b)
                    self.wfile.write(line)
                    if line.startswith(b'data: [DONE]'): whole = True
                    elif line.startswith(b'event: '):
                        name = line[7:].strip()
                        if name in LAST_EVENTS: whole = True; failed = failed or name in FAILED_EVENTS
                    elif line.startswith(b'data: {') and (b'"usage"' in line or b'"stats"' in line
                                                          or (len(line) < 200 and b'"type"' in line and b'delta' not in line)):   # never a token chunk
                        try: ev = json.loads(line[6:])
                        except ValueError: ev = None
                        if isinstance(ev, dict):
                            usage = merge(usage, counts(ev))
                            name = str(ev.get('type', '')).encode()
                            if name in LAST_EVENTS: whole = True; failed = failed or name in FAILED_EVENTS   # a stream without event: lines
                    if line in (b'\n', b'\r\n'): self.wfile.flush()
                look = peers.WAKE.set if peer else crashes.nudge                # who looks at once: the link, or this Mac's keeper
                if whole and failed: self._note = f'{who}: stream ended with an error event' if peer else 'LM Studio stream ended with an error event'; look()
                if not whole:                                                # the stream ended without its last event: cut short
                    self._note = f'{who}: stream truncated (no terminal event)' if peer else 'LM Studio stream truncated (no terminal event)'; look()
                    self.wfile.write(CUT.get(self._dialect, CUT['openai']))
                self.wfile.flush()
                return code, usage, None
            data = resp.read()
            if a: data = data.replace(a, b)
            if len(data) < 20_000_000:                                    # the counts sit at the top level or one deep
                try: usage = counts(json.loads(data))
                except ValueError: pass
            self.send_response(code); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data); self._sent = True
            return code, usage, None
        except (BrokenPipeError, ConnectionResetError): return 499, usage, 'client closed the connection during the response'   # LM Studio stops when we do
        except (http.client.IncompleteRead, http.client.HTTPException, OSError) as e:
            if self._sent: return code, usage, None                   # a stream cut short: the program saw it end
            if peer: peers.LINK.failed(peer, 0, f'response truncated ({type(e).__name__})')
            return 502, {}, (f'{who}: response truncated' if peer else 'LM Studio response truncated') + f' ({type(e).__name__})'
        finally: up.close()

    # ---- the hub's own pages
    def hub(self, method, path):
        route = path.split('?')[0].rstrip('/')
        if route == '/hub/health':
            m = maint.read()
            return self._send(200, dict(ok=True, hub='local-models', version=VERSION, pid=os.getpid(), port=PORT,
                                        since=round(_start['since']), lmstudio=UP,
                                        maintenance=dict(phase=m['phase'], start=m.get('start'), end=m.get('end'), why=m.get('why'), words=maint.words(m)) if m else None))
        q = urllib.parse.parse_qs(path.partition('?')[2])
        try:
            if route == '/hub/snapshot': return self._send(200, web.snapshot())
            if route == '/hub/history': return self._send(200, web.history((q.get('minutes') or [60])[0]))
            if route == '/hub/recent': return self._send(200, web.recent((q.get('n') or [25])[0]))
            if route == '/hub/peer': return self._send(200, peers.report())          # what this Mac would tell a front
            if route == '/hub/link':                                                # this Mac's look at its peers (POST: look now)
                if method == 'POST': peers.LINK.poll(force=True)
                return self._send(200, dict(me=peers.me(), peers=peers.LINK.view(), serving=peers.serving(), listening=peers.LISTEN.get('words'),
                                            problems=peers.problems()))
            if route == '/hub/recycle' and method == 'POST':
                body = self._body()
                try: j = json.loads(body) if body else {}
                except ValueError: j = {}
                model = (j.get('model') if isinstance(j, dict) else None) or (q.get('model') or [None])[0] or next(iter(llmshare.settings().get('families') or {}), None)
                fam = recycle.fresh_start(model) if model else None
                crashes.nudge()
                return self._send(200, dict(ok=bool(fam), family=fam, note='an instance past half its recycle threshold is recycled before the batch starts' if fam else 'unknown family, or recycling is disabled for it'))
            if route == '/hub/errors':                                              # what the error record holds (localmodels errors)
                return self._send(200, dict(errors=errors.recent((q.get('minutes') or [1440])[0], 200)))
            if route == '/hub/client-error' and method == 'POST':                     # the dashboard's own render errors
                body = self._body()[:8000]
                try: j = json.loads(body) if body else {}
                except ValueError: j = {}
                if not isinstance(j, dict): j = {}
                errors.record(f"dashboard: {str(j.get('where') or '?')[:60]}", words=str(j.get('error') or 'error')[:300],
                              trace=str(j.get('stack') or '')[:800], source='dashboard')
                return self._send(200, dict(ok=True))
        except Exception as e:
            errors.record(f'{method} {route}', e)
            return self._send(500, dict(error=f'{type(e).__name__}: {e}'))
        if route == '/hub/status':
            return self._send(200, dict(inflight=llmshare.inflight(), leases=llmshare.leases(), loaded=llmshare.loaded(),
                                        last_hour=llmshare.by_project(llmshare.requests_log(1)), waiting=llmshare.waiting_here()))
        return self._send(404, dict(error='not found; hub endpoints: /hub/health, /hub/status, /hub/snapshot, /hub/history, /hub/recent, /hub/peer, /hub/link, /hub/errors, POST /hub/recycle, POST /hub/client-error'))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads, request_queue_size, link = True, 128, False


class Server6(Server):
    address_family = socket.AF_INET6


class LinkServer(Server):
    """The door's listener for its peers' hubs (this Mac serves as a peer): on one address, bound to its interface."""
    link, hosts, iface = True, frozenset(), None


class LinkServer6(LinkServer):
    address_family = socket.AF_INET6


def link_server(address, port):
    """A listener on this address only, bound to the interface that holds it (IP_BOUND_IF), so a packet for it that
    arrives on another interface is not answered. -> the server, not yet serving."""
    six = ':' in address
    srv = (LinkServer6 if six else LinkServer)((address, port), Door, bind_and_activate=False)
    srv.iface = peers.interface_of(address)
    if srv.iface:
        try:
            idx = socket.if_nametoindex(srv.iface)
            if six: srv.socket.setsockopt(socket.IPPROTO_IPV6, getattr(socket, 'IPV6_BOUND_IF', 125), idx)
            else: srv.socket.setsockopt(socket.IPPROTO_IP, getattr(socket, 'IP_BOUND_IF', 25), idx)
        except OSError: pass
    try: srv.server_bind(); srv.server_activate()
    except OSError: srv.server_close(); raise
    host = f'[{address}]' if six else address
    srv.hosts = frozenset({f'{host}:{port}'.lower()})
    return srv


def listen_link():
    """While the settings say this Mac serves as a peer ("serve": {"address": ...}), the door also listens there (checked
    every 5 s, so a change needs no restart; an address not on this Mac yet, the Thunderbolt Bridge still coming up, is
    tried again). The words say what it does, for the status line."""
    cur, srv, said = None, None, None
    while True:
        try:
            want = peers.serving()
            key = (want['address'], want['port']) if want else None
            if key != cur:
                if srv:
                    srv.shutdown(); srv.server_close(); srv = None
                    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} gateway: link listener on {cur[0]}:{cur[1]} stopped", flush=True)
                cur, peers.LISTEN['words'] = None, None
                if want:
                    problem = peers.token_problem()
                    if problem: words = f'link listener not started: {problem}'
                    else:
                        try:
                            srv = link_server(want['address'], want['port'])
                            threading.Thread(target=srv.serve_forever, daemon=True).start()
                            cur = key
                            port_name = peers.ports().get(srv.iface) if srv.iface else None
                            words = (f"listening on {want['address']}:{want['port']} for peers (token required)"
                                     + (f", interface {srv.iface}" + (f' ({port_name})' if port_name else '') if srv.iface else '')
                                     + ('' if not srv.iface or peers.bridge(srv.iface, {srv.iface: port_name}) or peers.loopback(want['address'])
                                        else '; warning: not the Thunderbolt Bridge') + (f'; {peers.address_words(want["address"])}' if peers.address_words(want['address']) else ''))
                        except OSError as e:
                            words = f"link listener not bound to {want['address']}:{want['port']} ({e.strerror or e}): address not configured on any interface (assign the Thunderbolt Bridge static address)"
                    peers.LISTEN['words'] = words
                    if words != said: print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} gateway: {words}", flush=True); said = words
                else: said = None
        except Exception as e: errors.record('link listener', e)
        time.sleep(5)


def awake(caf, idle_since):
    """Hold the Mac awake (caffeinate -i) while any program is using a model, and let go ten minutes after the last."""
    busy = bool(llmshare.inflight() or llmshare.leases())
    now = time.time()
    if busy:
        idle_since = now
        if caf is None or caf.poll() is not None:
            try: caf = subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError: caf = None
    elif caf is not None and now - idle_since > AWAKE_FOR:
        try: caf.terminate()
        except OSError: pass
        caf = None
    return caf, idle_since


def keeper():
    """Keep a family's copies loaded whenever its first copy is, load a crashed model again and recycle copies due
    (models.keep): at the start, every 20 s, and at once when a request through the door fails (crashes.nudge)."""
    from . import models
    last, caf, idle_since = None, None, time.time()
    while True:
        try:
            did = models.keep()
            if did and did != last: print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {'; '.join(did)}", flush=True)
            last = did or last
        except Exception as e: errors.record('instance manager', e)
        try: caf, idle_since = awake(caf, idle_since)
        except Exception as e: errors.record('keep-awake (caffeinate)', e)
        if crashes.WAKE.wait(20):
            crashes.WAKE.clear(); time.sleep(1)            # a request just failed: give LM Studio a moment to log why


def main(argv=()):
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, 8192), hard), hard))   # a thread, a socket and a lock per waiting request
    except (ValueError, OSError): pass
    try:
        import glob
        for f in glob.glob(os.path.join(llmshare.INFLIGHT, '*.json')): os.remove(f)     # one door at a time: what an earlier run left is gone
    except OSError: pass
    llmshare.leases()
    try: srv = Server(('127.0.0.1', PORT), Door)
    except OSError:
        print(f'port {PORT} in use: the gateway is probably already running ({llmshare.HUB}/hub/health)', flush=True); return 1
    try:                                                  # some programs try ::1 first for "localhost"
        srv6 = Server6(('::1', PORT), Door); threading.Thread(target=srv6.serve_forever, daemon=True).start()
    except OSError: pass
    threading.Thread(target=beats, daemon=True).start()
    threading.Thread(target=listen_link, daemon=True).start()          # this Mac as a peer, when the settings say so
    threading.Thread(target=peers.poller, daemon=True).start()         # this Mac's look at its peers, when it has any
    if not os.environ.get('LOCALMODELS_NO_KEEPER'): threading.Thread(target=keeper, daemon=True).start()
    threading.Thread(target=web.sampler, daemon=True).start()           # memory every 15 s, for the dashboard's chart
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} local-models gateway {VERSION} listening on {llmshare.HUB} -> LM Studio at {UP}", flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    return 0
