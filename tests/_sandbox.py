"""Imported by every suite before localmodels: the hub's state folder, its port, LM Studio's address and logs, and the
lms command all point into a fresh temporary folder or at nothing, so a suite never reads or writes this Mac's live state
(~/Library/Application Support/LocalModels), never reaches the installed gateway, and never runs the real lms, whatever
path or call a test forgets to redirect by hand. stray() names any hub path left pointing into the live state folder."""
import atexit, os, shutil, socket, sys, tempfile

LIVE = os.path.expanduser('~/Library/Application Support/LocalModels')
BOX = tempfile.mkdtemp(prefix='localmodels-state-')
atexit.register(shutil.rmtree, BOX, True)


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


os.environ.update(LOCALMODELS_STATE=BOX, LOCALMODELS_PORT=str(_free_port()), LMSTUDIO_URL=f'http://127.0.0.1:{_free_port()}',
                  LMSTUDIO_LOGS=os.path.join(BOX, 'lmstudio-logs'), LMS='/usr/bin/false')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stray():
    """The hub's module-level paths that point into the live state folder: [module.NAME]."""
    out = []
    for name, mod in sorted(sys.modules.items()):
        if not name.startswith('localmodels'): continue
        for k, v in vars(mod).items():
            vs = v if isinstance(v, (tuple, list)) else (v,)
            if any(isinstance(x, str) and x.startswith(LIVE) for x in vs): out.append(f'{name}.{k}')
    return out
