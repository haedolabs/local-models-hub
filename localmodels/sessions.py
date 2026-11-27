"""sessions - recent Claude Code and OpenCode sessions on this Mac, by project.

From Claude Code's own records: the desktop app's session files (title, folder) and each session's transcript file
(when it last wrote; a session that wrote in the last minute is working). Reads names, times and labels only, never
what was said. OpenCode's local SQLite database supplies the session title, directory, busy and idle times, and
subagent count. Only session and project metadata are queried.
"""
import glob, json, os, sqlite3, time
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from . import names, whois

CLAUDE = os.path.expanduser('~/.claude/projects')
DESKTOP = os.path.expanduser('~/Library/Application Support/Claude/claude-code-sessions')
SOURCES = {'Claude Code': 'claude', 'OpenCode': 'opencode'}
WORKING = 60                                              # seconds: a transcript written this recently is a working session
_files = {}                                               # path -> (mtime, parsed), so a refresh rereads only what changed
_index = {'at': 0.0, 'by_cli': {}, 'by_host': {}}


def source_of(session=None, host_session=None):
    if session and session.startswith('ses_'): return 'OpenCode'
    return 'Claude Code' if session or host_session else None


def by_source(rows):
    """Keep both clients in summaries, including one with no recent sessions. Older rows are Claude Code's."""
    return {source: [r for r in rows if r.get('source', 'Claude Code') == source] for source in SOURCES}


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
                        running=len([k for k in started if k not in done]) if live else 0, live=live))   # a count: labels are free text
    return out


def _subagents(sdir):
    """How many subagents of this session wrote in the last 90 s (their descriptions are free text: never read out)."""
    out = 0
    for meta in glob.glob(os.path.join(sdir, 'subagents', 'agent-*.meta.json')):
        log = meta.replace('.meta.json', '.jsonl')
        try:
            if time.time() - os.path.getmtime(log) > 90: continue
        except OSError: continue
        out += 1
    return out


def _opencode_uri():
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'opencode'
    path = data / (os.environ.get('OPENCODE_DB') or 'opencode.db')
    return path.absolute().as_uri() + '?mode=ro'


def _opencode_read(uri, query, params):
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as db:
            return db.execute(query, params).fetchall()
    except sqlite3.Error: return []                        # optional source: absent, locked, or an unsupported schema


def _opencode_recent(since):
    """Viewing an idle session updates time_updated. Its last activity is still when it went idle."""
    query = """SELECT s.id, s.directory, p.worktree, s.title, s.model, s.time_busy > coalesce(s.time_idle, 0),
                      iif(s.time_idle IS NULL OR s.time_busy > s.time_idle,
                          max(s.time_updated, coalesce(s.time_busy, 0)), s.time_idle) AS last_activity,
                      (SELECT count(*) FROM session_v2 AS child WHERE child.parent_id = s.id
                       AND child.time_archived IS NULL AND child.time_busy > coalesce(child.time_idle, 0))
               FROM session_v2 AS s LEFT JOIN project AS p ON p.id = s.project_id
               WHERE s.parent_id IS NULL AND s.time_archived IS NULL AND last_activity >= ?"""
    for sid, directory, root, title, model, working, last, children in _opencode_read(_opencode_uri(), query, (since * 1000,)):
        try: ref = json.loads(model or 'null')
        except (ValueError, TypeError): ref = None
        yield dict(id=sid, host=None, source='OpenCode', project=whois.project_of(root) or whois.project_of(directory) or '?',
                   title=title or 'an OpenCode session', working=bool(working), last=last / 1000,
                   model=ref.get('id') if isinstance(ref, dict) else None, subagents=children, workflows=[])


@lru_cache(maxsize=512)
def _opencode_root(uri, sid, tick):
    """Resolve a request ID, even outside the dashboard window. Only its root supplies a title.
    The time tick shares lookups across recent requests without keeping a renamed session's title."""
    query = """WITH RECURSIVE ancestors(id, parent_id) AS (
                   SELECT id, parent_id FROM session_v2 WHERE id = ?
                   UNION
                   SELECT s.id, s.parent_id FROM session_v2 AS s JOIN ancestors AS a ON s.id = a.parent_id)
               SELECT s.id, p.worktree, s.directory, s.title FROM ancestors AS a
               JOIN session_v2 AS s ON s.id = a.id LEFT JOIN project AS p ON p.id = s.project_id
               WHERE a.parent_id IS NULL"""
    rows = _opencode_read(uri, query, (sid,))
    return rows[0] if rows else None


def recent(hours=3.0):
    """Sessions active in this time window, newest first. Child agents are counts on their parent, not extra sessions."""
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
        out.append(dict(id=sid, host=d.get('host'), source='Claude Code', project=whois.project_of(folder) or '?', title=d.get('title') or 'a terminal session',
                        working=time.time() - m < WORKING, last=m, model=d.get('model'),
                        subagents=_subagents(sdir) if os.path.isdir(sdir) else 0,
                        workflows=_workflows(sdir) if os.path.isdir(sdir) else []))
    out.extend(_opencode_recent(since))
    return sorted(out, key=lambda s: -s['last'])


def root_title(session=None, host=None):
    """The session row a request belongs to, and its title when that session's project allows it."""
    if not (session or host): return None, None
    if source_of(session, host) == 'OpenCode':
        row = _opencode_root(_opencode_uri(), session, int(time.monotonic() // 3))
        if not row: return session, None
        sid, root, folder, title = row
        project = whois.project_of(root) or whois.project_of(folder)
    else:
        by_cli, by_host = desktop()
        d = by_cli.get(session) or by_host.get(host)
        if not d: return session or host, None
        sid, project, title = d.get('id') or host, whois.project_of(d.get('folder')), d.get('title')
    if not project or names.presenting() or names.private(project): title = None
    return sid, title or None


def title_of(session=None, host=None):
    return root_title(session, host)[1]
