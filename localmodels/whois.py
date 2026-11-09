"""whois - which project, Claude Code session and program is behind a connection to the hub or to LM Studio.

Nobody has to report: the hub looks at the program on the other end of each connection. Its folder names the project
(the first folder under ~/Documents, ~/Projects or ~/Developer, or the folder itself), and Claude Code puts the
session's id in every shell it runs, so a program Claude started carries it. Reads only process facts (folder,
program name, those two ids), never what a program sends. macOS has the calls for all of it; elsewhere the hub still
runs, naming programs only by what they say in their headers.
"""
import ctypes, os, re, subprocess, threading, time

HOME = os.path.expanduser('~')
ROOTS = [os.path.join(HOME, d) for d in ('Documents', 'Projects', 'Developer', 'Code', 'code', 'src', 'GitHub', 'repos')]
LSOF = '/usr/sbin/lsof' if os.path.exists('/usr/sbin/lsof') else 'lsof'
try:
    _libc = ctypes.CDLL(None)
    _libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                             ctypes.c_void_p, ctypes.c_size_t]
except (OSError, AttributeError): _libc = None
try:
    _proc = ctypes.CDLL('/usr/lib/libproc.dylib')
    _proc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
except (OSError, AttributeError): _proc = None
_cache, KEEP = {}, 600                                   # pid -> (when, identity); kept 10 minutes
_conns = {'at': 0.0, 'port': None, 'rows': [], 'lock': threading.Lock()}


def _sysctl(mib, size):
    if _libc is None: return None
    arr = (ctypes.c_int * len(mib))(*mib)
    buf, n = ctypes.create_string_buffer(size), ctypes.c_size_t(size)
    if _libc.sysctl(arr, len(mib), buf, ctypes.byref(n), None, 0) != 0: return None
    return buf.raw[:n.value]


def args_env(pid):
    """-> (argv, env dict) of a process of this user, from the kernel (KERN_PROCARGS2), or ([], {})."""
    raw = _sysctl([1, 8], 4)                              # CTL_KERN, KERN_ARGMAX
    argmax = int.from_bytes(raw, 'little') if raw else 1 << 20
    raw = _sysctl([1, 49, int(pid)], argmax)              # CTL_KERN, KERN_PROCARGS2, pid
    if not raw or len(raw) < 8: return [], {}
    argc, rest = int.from_bytes(raw[:4], 'little'), raw[4:]
    i = rest.find(b'\0')
    while 0 <= i < len(rest) and rest[i] == 0: i += 1
    parts = rest[i:].split(b'\0') if i >= 0 else []
    argv = [p.decode('utf-8', 'replace') for p in parts[:argc]]
    env = {}
    for p in parts[argc:]:
        if not p: break
        k, _, v = p.decode('utf-8', 'replace').partition('=')
        env[k] = v
    return argv, env


def parent_of(pid):
    """A process's parent (proc_pidinfo PROC_PIDTBSDINFO: pbi_ppid), or 0."""
    if _proc:
        buf = ctypes.create_string_buffer(136)
        if _proc.proc_pidinfo(int(pid), 3, 0, buf, 136) > 0: return int.from_bytes(buf.raw[16:20], 'little')
    try: return int(subprocess.run(['/bin/ps', '-o', 'ppid=', '-p', str(pid)], capture_output=True, text=True, timeout=5).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError): return 0


def cwd_of(pid):
    """A process's working folder (proc_pidinfo PROC_PIDVNODEPATHINFO), or None."""
    if _proc:
        buf = ctypes.create_string_buffer(2352)            # two vnode_info_path: 152 bytes of vnode_info + a 1024-byte path
        if _proc.proc_pidinfo(int(pid), 9, 0, buf, 2352) > 0:
            p = buf.raw[152:152 + 1024].split(b'\0')[0].decode('utf-8', 'replace')
            if p: return p
    try:
        out = subprocess.run([LSOF, '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], capture_output=True, text=True, timeout=5).stdout
        return next((l[1:] for l in out.split('\n') if l.startswith('n')), None)
    except (OSError, subprocess.SubprocessError): return None


def project_of(path):
    """A folder's project name, from the path alone: the first folder under ~/Documents (or ~/Projects, ...), never a
    worktree's own name; otherwise the folder's own name."""
    if not path or path in ('/', HOME): return None
    path = re.split(r'/\.claude/worktrees/|/\.worktrees/', path)[0]
    for r in ROOTS:
        if path.startswith(r + '/'): return path[len(r) + 1:].split('/')[0]
    return os.path.basename(path.rstrip('/')) or None


def _app(argv, exe=''):
    """The program in plain words: a script's file name, an app's name, or the program's own name."""
    first = argv[0] if argv else exe or ''
    name = os.path.basename(first)
    if re.match(r'(?i)(python[\d.]*|node|ruby|perl)$', name):
        if len(argv) > 2 and argv[1] == '-m': return argv[2][:80]
        inline = any(a in ('-c', '-e', '-p', '--eval', '--print') for a in argv[1:])   # code on the command line: never a name
        script = '' if inline else next((a for a in argv[1:] if not a.startswith('-')), '')
        if script: return os.path.basename(script)[:80]
        return 'python' if name.lower().startswith('python') else name
    if re.match(r'(ba|z|fi)?sh$', name): return name
    m = re.search(r'/([^/]+)\.app/', first)
    return m.group(1) if m else name or '?'


def identity(pid):
    """{pid, project, folder, app, session, host_session, claude} for a process; cached ten minutes."""
    pid = int(pid or 0)
    hit = _cache.get(pid)
    if hit and time.time() - hit[0] < KEEP: return hit[1]
    argv, env = args_env(pid) if pid else ([], {})
    folder = cwd_of(pid) if pid else None
    me = dict(pid=pid, folder=folder, project=project_of(folder), app=_app(argv), session=env.get('CLAUDE_CODE_SESSION_ID'),
              host_session=env.get('CLAUDE_CODE_HOST_SESSION_ID'),
              claude=env.get('CLAUDECODE') == '1' or bool(env.get('CLAUDE_CODE_HOST_SESSION_ID')))
    up = pid
    for _ in range(12):                                   # Apple's own programs (curl, zsh) hide their environment from
        if (me['session'] or me['host_session']) and me['project']: break     # others: ask their parents instead
        up = parent_of(up) if up else 0
        if up <= 1: break
        a2, e2 = args_env(up)
        me['session'] = me['session'] or e2.get('CLAUDE_CODE_SESSION_ID')
        me['host_session'] = me['host_session'] or e2.get('CLAUDE_CODE_HOST_SESSION_ID')
        me['claude'] = me['claude'] or bool(e2.get('CLAUDE_CODE_HOST_SESSION_ID')) or os.path.basename(a2[0] if a2 else '') == 'claude'
        if not me['project']: me['project'] = project_of(cwd_of(up))
    if not me['project']: me['project'] = me['app'] if me['app'] != '?' else None
    _cache[pid] = (time.time(), me)
    if len(_cache) > 500:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:250]: _cache.pop(k, None)
    return me


def _lsof_tcp(spec):
    """[(pid, local, remote)] for established TCP connections matching an lsof -i spec."""
    try:
        out = subprocess.run([LSOF, '-nP', f'-iTCP{spec}', '-sTCP:ESTABLISHED', '-Fpn'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError): return []
    rows, pid = [], None
    for line in out.split('\n'):
        if line.startswith('p'): pid = int(line[1:])
        elif line.startswith('n') and '->' in line and pid:
            local, _, remote = line[1:].partition('->')
            rows.append((pid, local, remote))
    return rows


def _connections(port):
    """Every established connection to or from this local port, from one lsof shared by the requests of the same second."""
    with _conns['lock']:
        if _conns['port'] != port or time.time() - _conns['at'] > 1:
            _conns.update(at=time.time(), port=port, rows=_lsof_tcp(f':{port}'))
        return list(_conns['rows'])


def peer_pid(peer_port, server_port, host='127.0.0.1'):
    """The process on the client end of a local connection: the one whose own end is peer_port. A burst of requests
    shares one lsof; a connection too new for it gets one more look."""
    for _ in range(2):
        for pid, local, remote in _connections(server_port):
            if local.endswith(f':{peer_port}') and remote.endswith(f':{server_port}') and pid != os.getpid(): return pid
        with _conns['lock']: _conns['at'] = 0.0                # not there yet: look once more, fresh
    return None


def clients_of(port, skip=()):
    """{pid: connections} of every program connected to this local port right now (the server itself left out)."""
    out = {}
    for pid, local, remote in _lsof_tcp(f':{port}'):
        if remote.endswith(f':{port}') and not local.endswith(f':{port}') and pid not in skip:
            out[pid] = out.get(pid, 0) + 1
    return out
