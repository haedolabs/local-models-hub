"""names - what every screen calls a project, its batches and its jobs, set by the person on this Mac.

A project is seen under the name a program gives it (X-Project), or else its folder (a script's working folder, a
Claude Code session's folder). names.json, in the hub's settings folder, maps what is seen to what is shown:

  {"projects": {
     "<as seen>": "<shown>",
     "<shown or as seen>": {"name": "<shown>", "private": true,
                            "labels": {"<batch, job or script as seen>": "<shown>"}}}}

- name: what every screen calls the project. Several names seen can show as one (a folder and a header, say).
- labels: names for its batches (X-Run), its jobs (the phase in X-Step: "extract files 8 of 360") and its scripts.
- private: the screens show only names set here for the project: no Claude Code session titles, and no script names,
  free-text steps or batch names without a label (a count such as "8 of 360" stays).

The names apply when a screen reads, never in the records, so a new name applies to all past history at once.
`localmodels name` sets them.

Screenshot mode (`localmodels present on for 2h`, present.json beside names.json, off by itself at its end): every screen
shows the projects it keeps by their names (the demo's and local-models by default) and every other project as "other",
private; no Claude Code session title shows anywhere; error details are left out. For pictures of the dashboard that
show the hub at work and nothing personal.
"""
import json, os, time

from . import llmshare, runs

FILE = os.path.join(llmshare.SHARED, 'names.json')
_cache = {'mtime': None, 'data': {}}
_present = {'checked': 0.0, 'mtime': None, 'data': None}
OTHER = 'other'                                          # what a project not kept is called in screenshot mode


def _present_file(): return os.path.join(os.path.dirname(FILE), 'present.json')


def presenting():
    """Screenshot mode while it is on: {until, keep: [project names shown as they are]}, else None (looked at once a second)."""
    now = time.time()
    if now - _present['checked'] >= 1:
        _present['checked'] = now
        try: m = os.path.getmtime(_present_file())
        except OSError: m = None
        if m != _present['mtime']:
            try: d = json.load(open(_present_file())) if m else None
            except (OSError, ValueError): d = None
            _present.update(mtime=m, data=d if isinstance(d, dict) else None)
    d = _present['data']
    return d if d and float(d.get('until') or 0) > now else None


def set_present(minutes, keep):
    """Screenshot mode on for `minutes`, showing the projects in `keep` by name. -> the state."""
    d = dict(until=round(time.time() + float(minutes) * 60), keep=sorted({str(k) for k in keep if k}), set_at=round(time.time()))
    os.makedirs(os.path.dirname(_present_file()), exist_ok=True)
    tmp = f'{_present_file()}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f: json.dump(d, f); f.write('\n')
    os.replace(tmp, _present_file())
    _present['checked'] = 0.0
    return d


def present_off():
    try: os.remove(_present_file())
    except OSError: pass
    _present['checked'] = 0.0


def _load():
    try: m = os.path.getmtime(FILE)
    except OSError: return {}
    if m != _cache['mtime']:
        try: d = json.load(open(FILE))
        except (OSError, ValueError): d = None
        _cache.update(mtime=m, data=d if isinstance(d, dict) else {})
    return _cache['data']


def _entry(key):
    v = (_load().get('projects') or {}).get(key)
    if isinstance(v, str): return dict(name=v, private=False, labels={})
    if isinstance(v, dict):
        return dict(name=v.get('name') if isinstance(v.get('name'), str) else None, private=bool(v.get('private')),
                    labels={k: x for k, x in (v.get('labels') or {}).items() if isinstance(x, str)})
    return dict(name=None, private=False, labels={})


def _resolve(raw):
    """-> (shown name, private, labels): the entry for what is seen, then the one for the name it shows as."""
    raw = raw or '?'
    a = _entry(raw)
    shown = a['name'] or raw
    b = _entry(shown) if shown != raw else dict(name=None, private=False, labels={})
    pm = presenting()
    if pm and shown not in pm.get('keep', ()) and raw not in pm.get('keep', ()): return OTHER, True, {}   # screenshot mode
    return shown, a['private'] or b['private'], {**b['labels'], **a['labels']}


def project(raw):
    """What every screen calls this project."""
    return _resolve(raw)[0]


def private(raw):
    return _resolve(raw)[1]


def label(raw_project, as_seen):
    """A batch, job or script name as the screens show it: its label, else as seen, else None when the project is private."""
    if not as_seen: return as_seen
    _, priv, labels = _resolve(raw_project)
    return labels.get(as_seen) or (None if priv else as_seen)


def step(raw_project, words):
    """A request's step as the screens show it: "<job> N of M" with the job's label; for a private project, without a
    label, only "N of M"; free words only when the project is not private (or they have a label)."""
    if not words: return words
    p = runs.parse_step(words)
    if p:
        name = label(raw_project, p[0])
        return f'{name} {p[1]} of {p[2]}' if name else f'{p[1]} of {p[2]}'
    return label(raw_project, words)


def request(rec):
    """A request record for a screen: the project as shown, the step as shown."""
    raw = rec.get('project') or '?'
    return dict(rec, project=project(raw), step=step(raw, rec.get('step')))


def session(c):
    """A Claude Code session for a screen: the project as shown; its title only while the project is not private and
    screenshot mode is off (titles are free text), else the same plain words, so every screen has a string to print."""
    raw = c.get('project') or '?'
    return dict(c, project=project(raw), title='a Claude Code session' if presenting() or private(raw) else c.get('title'))


def user(r):
    """A program using the models, for a screen: the project, script, step, batch and job as shown."""
    raw = r.get('project') or '?'
    out = dict(r, project=project(raw), app=label(raw, r.get('app')), step=step(raw, r.get('step')))
    if private(raw) or presenting(): out['title'] = None
    if isinstance(r.get('batch'), dict):
        b = dict(r['batch'], name=label(raw, r['batch'].get('name')), phase=label(raw, r['batch'].get('phase')))
        if isinstance(b.get('job'), dict): b['job'] = dict(b['job'], name=label(raw, b['job'].get('name')))
        if b.get('now'): b['now'] = [dict(j, step=step(raw, j.get('step'))) for j in b['now']]
        out['batch'] = b
    if isinstance(r.get('job'), dict): out['job'] = dict(r['job'], name=label(raw, r['job'].get('name')))
    return out


# ---------------------------------------------------------------- setting them (localmodels name)
def _save(d):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = f'{FILE}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f: json.dump(d, f, indent=1, sort_keys=True); f.write('\n')
    os.replace(tmp, FILE)
    _cache['mtime'] = None


def _edit(key, change):
    d = json.loads(json.dumps(_load() or {}))
    ps = d.setdefault('projects', {})
    v = ps.get(key)
    e = dict(name=v) if isinstance(v, str) else dict(v) if isinstance(v, dict) else {}
    change(e)
    e = {k: x for k, x in e.items() if x not in (None, False, {}, '')}
    if not e: ps.pop(key, None)
    elif list(e) == ['name']: ps[key] = e['name']                   # a plain rename stays a plain string
    else: ps[key] = e
    _save(d)


def set_project(as_seen, shown):
    _edit(llmshare.clean(as_seen, 120), lambda e: e.__setitem__('name', llmshare.clean(shown, 80) or None))


def set_private(project_name, on):
    _edit(llmshare.clean(project_name, 120), lambda e: e.__setitem__('private', bool(on)))


def set_label(project_name, as_seen, shown):
    def change(e):
        labels = dict(e.get('labels') or {})
        labels[llmshare.clean(as_seen, 120)] = llmshare.clean(shown, 80)
        e['labels'] = labels
    _edit(llmshare.clean(project_name, 120), change)


def forget(project_name, as_seen=None):
    """Drop a label (as_seen), or the project's whole entry."""
    key = llmshare.clean(project_name, 120)
    if as_seen is None:
        d = json.loads(json.dumps(_load() or {}))
        (d.get('projects') or {}).pop(key, None); _save(d); return

    def change(e):
        labels = dict(e.get('labels') or {})
        labels.pop(as_seen, None)
        e['labels'] = labels
    _edit(key, change)
