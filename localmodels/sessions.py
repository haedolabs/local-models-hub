"""sessions - every Claude Code session on this Mac, by project: working or idle, its title, its subagents and workflows.

From Claude Code's own records: the desktop app's session files (title, folder) and each session's transcript file
(when it last wrote; a session that wrote in the last minute is working). Reads names, times and labels only, never
what was said.
"""
import glob, json, os, time

from . import whois

CLAUDE = os.path.expanduser('~/.claude/projects')
DESKTOP = os.path.expanduser('~/Library/Application Support/Claude/claude-code-sessions')
WORKING = 60                                              # seconds: a transcript written this recently is a working session
_files = {}                                               # path -> (mtime, parsed), so a refresh rereads only what changed
_index = {'at': 0.0, 'by_cli': {}, 'by_host': {}}


def _cached(path, parse):
    try: m = os.path.getmtime(path)
    except OSError: return None
    hit = _files.get(path)
    if hit and hit[0] == m: return hit[1]
    try: val = parse(path)
    except (OSError, ValueError, UnicodeDecodeError): val = None
    _files[path] = (m, val)
    return val


def _desktop_file(path):
    d = json.load(open(path))
    return dict(host=d.get('sessionId'), id=d.get('cliSessionId'), title=d.get('title') or '', archived=bool(d.get('isArchived')),
                folder=d.get('cwd') or d.get('originCwd'), model=d.get('model'), last=(d.get('lastActivityAt') or 0) / 1000)


def desktop(fresh=False):
    """-> ({transcript id: info}, {desktop id: info}) for the desktop app's sessions (cached 10 s)."""
    if fresh or time.time() - _index['at'] > 10:
        by_cli, by_host = {}, {}
        for f in glob.glob(os.path.join(DESKTOP, '*', '*', 'local_*.json')):
            d = _cached(f, _desktop_file)
            if not d: continue
            if d.get('id'): by_cli[d['id']] = d
            if d.get('host'): by_host[d['host']] = d
        _index.update(at=time.time(), by_cli=by_cli, by_host=by_host)
    return _index['by_cli'], _index['by_host']


def _folder_from_transcript(path):
    """A terminal session's folder, from the first lines of its transcript that carry one."""
    with open(path, errors='replace') as f:
        for _, line in zip(range(20), f):
            if '"cwd"' in line:
                try: return json.loads(line).get('cwd')
                except ValueError: continue
    return None


def _workflows(sdir):
    out = []
    for j in glob.glob(os.path.join(sdir, 'subagents', 'workflows', '*', 'journal.jsonl')):
        try: age = time.time() - os.path.getmtime(j)
        except OSError: continue
        if age > 3 * 3600: continue
        try: ev = [json.loads(l) for l in open(j) if l.strip()]
        except (OSError, ValueError): continue
        started = {e.get('agentId') or e.get('key'): e.get('label') for e in ev if e.get('type') == 'started'}
        done = {e.get('agentId') or e.get('key') for e in ev if e.get('type') == 'result'}
        live = age < 900
        out.append(dict(name=os.path.basename(os.path.dirname(j)), agents=len(started), finished=len(done & set(started)),
                        running=[v for k, v in started.items() if k not in done] if live else [], live=live))
    return out


def _subagents(sdir):
    out = []
    for meta in glob.glob(os.path.join(sdir, 'subagents', 'agent-*.meta.json')):
        log = meta.replace('.meta.json', '.jsonl')
        try:
            if time.time() - os.path.getmtime(log) > 90: continue
        except OSError: continue
        d = _cached(meta, lambda p: json.load(open(p))) or {}
        out.append(d.get('description') or 'a subagent')
    return out


def recent(hours=3.0):
    """Every Claude Code session that wrote in the last `hours` hours, newest first:
    [{id, host, project, title, working, last, model, subagents, workflows}]."""
    by_cli, _ = desktop()
    out, since = [], time.time() - hours * 3600
    for t in glob.glob(os.path.join(CLAUDE, '*', '*.jsonl')):
        try: m = os.path.getmtime(t)
        except OSError: continue
        if m < since: continue
        sid = os.path.basename(t)[:-6]
        d = by_cli.get(sid) or {}
        if d.get('archived') and time.time() - m > WORKING: continue
        folder = d.get('folder') or _cached(t, _folder_from_transcript)
        sdir = t[:-6]
        out.append(dict(id=sid, host=d.get('host'), project=whois.project_of(folder) or '?', title=d.get('title') or 'a terminal session',
                        working=time.time() - m < WORKING, last=m, model=d.get('model'),
                        subagents=_subagents(sdir) if os.path.isdir(sdir) else [],
                        workflows=_workflows(sdir) if os.path.isdir(sdir) else []))
    return sorted(out, key=lambda s: -s['last'])


def title_of(session=None, host=None):
    """A session's title (the desktop app's), by its transcript id or its desktop id."""
    by_cli, by_host = desktop()
    d = by_cli.get(session) if session else None
    d = d or (by_host.get(host) if host else None)
    return (d or {}).get('title') or None
