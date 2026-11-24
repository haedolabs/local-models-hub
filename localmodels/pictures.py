"""pictures - the README's pictures of the dashboard, made the same way every time: `localmodels pictures [--out docs] [--dry]`.

Headless Chrome, with a temporary profile of its own (never the person's), draws the dashboard at 1512 x 900 (the MacBook's
screen) and 1728 x 1117, at 2x, dark and light; the close-ups are cut from the dark ones with sips, along boxes the page itself
measures (dash.html with ?rects writes each card's box into the page), so a change of layout moves the crops with it.

Only in screenshot mode (`localmodels present on`, with at least 5 minutes left): pictures made for the public never show a
project, batch or session name that is not the demo's. Run `localmodels demo` first for a hub at work.

Headless Chrome lays a page out shorter than its window (by the window's frame) and resizes it to the window just before the
shot; the dashboard draws again at once on that resize. The crop boxes are measured at the size the shot ends at: the frame
is measured on each run, not assumed.
"""
import json, os, re, shutil, subprocess, tempfile, time

from . import HOME, llmshare, names

SCALE, MARGIN = 2, 6           # device pixels per CSS pixel; CSS pixels around a close-up's boxes
BUDGET = 8000                  # ms of virtual time Chrome gives the page (fonts, first snapshot, history, a redraw)
SHOTS = (('dashboard-dark', 1512, 900, 'dark'), ('dashboard-light', 1512, 900, 'light'),
         ('dashboard-large-dark', 1728, 1117, 'dark'), ('dashboard-large-light', 1728, 1117, 'light'))
CROPS = (('tiles-1-dark', 'dashboard-dark', ('tile0', 'tile3')), ('tiles-2-dark', 'dashboard-dark', ('tile4', 'tile6')),
         ('models-dark', 'dashboard-large-dark', ('models',)), ('concurrency-dark', 'dashboard-large-dark', ('ans',)),
         ('clients-dark', 'dashboard-large-dark', ('users',)))
CHROMES = ('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Chromium.app/Contents/MacOS/Chromium',
           os.path.expanduser('~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))


def chrome():
    """Chrome or Chromium on this Mac, or None."""
    return next((c for c in CHROMES if os.access(c, os.X_OK)), None)


def chrome_args(ch, profile, width, height, scheme, shot=None, url=None):
    """One headless Chrome run: a screenshot to `shot`, else the page's DOM on stdout."""
    return [ch, '--headless=new', f'--user-data-dir={profile}', '--hide-scrollbars', '--no-first-run', f'--window-size={width},{height}',
            f'--force-device-scale-factor={SCALE}', f'--virtual-time-budget={BUDGET}',
            '--force-dark-mode' if scheme == 'dark' else '--blink-settings=preferredColorScheme=1',
            f'--screenshot={shot}' if shot else '--dump-dom', url or llmshare.HUB + '/']


def crop_box(rects, keys, size, scale=SCALE, margin=MARGIN):
    """The device-pixel box (x, y, width, height) around the page's boxes `keys` ([left, top, width, height] in CSS pixels),
    with a margin, inside a picture of `size`; its offsets at least 1 (sips centres a crop whose offset is 0). None when the
    page did not measure one of them."""
    boxes = [rects.get(k) for k in keys]
    if not all(isinstance(b, list) and len(b) == 4 for b in boxes): return None
    x0 = min(b[0] for b in boxes) - margin; y0 = min(b[1] for b in boxes) - margin
    x1 = max(b[0] + b[2] for b in boxes) + margin; y1 = max(b[1] + b[3] for b in boxes) + margin
    x0, y0 = max(1, round(x0 * scale)), max(1, round(y0 * scale))
    x1, y1 = min(size[0], round(x1 * scale)), min(size[1], round(y1 * scale))
    return (x0, y0, x1 - x0, y1 - y0)


def _run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def measure(ch, width, height, say=print):
    """The page's boxes at width x height, as the shot ends: the window's frame measured first (a page laid out in a window
    of width x height gets a shorter viewport), then the page measured in a window that much taller. -> dict or None."""
    def dump(h):
        prof = tempfile.mkdtemp(prefix='localmodels-chrome-')
        try: p = _run(chrome_args(ch, prof, width, h, 'dark', url=llmshare.HUB + '/?rects'))
        finally: shutil.rmtree(prof, ignore_errors=True)
        m = re.search(r'<pre id="lm-rects"[^>]*>(.*?)</pre>', p.stdout, re.S)
        try: return json.loads(m.group(1).replace('&quot;', '"').replace('&amp;', '&')) if m else None
        except ValueError: return None
    first = dump(height)
    if not first: say(f'  the page wrote no boxes at {width} x {height} (dash.html without ?rects: localmodels install)'); return None
    frame = height - int(first.get('vh') or height)
    rects = dump(height + frame) if frame else first
    if not rects or int(rects.get('vh') or 0) != height:
        say(f"  the page measured {rects and rects.get('vw')} x {rects and rects.get('vh')}, not {width} x {height}"); return None
    return rects


def take(out=None, say=print, dry=False):
    """Make every picture into `out` (the repo's docs/ by default). -> exit code."""
    out = out or os.path.join(HOME, 'docs')
    p = names.presenting()
    if not p or float(p.get('until') or 0) - time.time() < 300:
        say('pictures are made only in screenshot mode with at least 5 minutes left, so no project but the demo\'s shows by name: '
            'localmodels present on for 1h (and localmodels demo for a hub at work)')
        return 2
    ch = chrome()
    if not ch: say(f"no Chrome or Chromium found ({', '.join(CHROMES[:2])})"); return 2
    if not llmshare.hub_up(fresh=True): say(f'gateway down at {llmshare.HUB}: localmodels start'); return 1
    if dry:
        say(f'would draw {llmshare.HUB}/ with {ch} into {out}:')
        for name, w, h, scheme in SHOTS: say(f'  {name}.png  {w} x {h} at {SCALE}x, {scheme}')
        for name, src, keys in CROPS: say(f"  {name}.png  cut from {src}.png around the page's {' and '.join(keys)}")
        return 0
    stage = tempfile.mkdtemp(prefix='localmodels-pictures-')
    try:
        boxes = {}
        for w, h in sorted({(s[1], s[2]) for s in SHOTS}):
            r = measure(ch, w, h, say)
            if not r: return 1
            boxes[(w, h)] = r
        sizes = {}
        for name, w, h, scheme in SHOTS:
            prof = tempfile.mkdtemp(prefix='localmodels-chrome-')
            try: _run(chrome_args(ch, prof, w, h, scheme, shot=os.path.join(stage, name + '.png')))
            finally: shutil.rmtree(prof, ignore_errors=True)
            if not os.path.exists(os.path.join(stage, name + '.png')): say(f'  {name}: Chrome made no picture'); return 1
            sizes[name] = (w * SCALE, h * SCALE, (w, h))
        for name, src, keys in CROPS:
            box = crop_box(boxes[sizes[src][2]], keys, sizes[src][:2])
            if not box: say(f"  {name}: the page did not measure {' and '.join(keys)}"); return 1
            x, y, bw, bh = box
            shutil.copy(os.path.join(stage, src + '.png'), os.path.join(stage, name + '.png'))
            c = _run(['sips', '--cropToHeightWidth', str(bh), str(bw), '--cropOffset', str(y), str(x), os.path.join(stage, name + '.png')], 60)
            if c.returncode: say(f'  {name}: sips failed: {c.stderr.strip()[-200:]}'); return 1
        os.makedirs(out, exist_ok=True)
        for f in sorted(os.listdir(stage)):
            shutil.move(os.path.join(stage, f), os.path.join(out, f))
            dims = re.findall(r'pixel(?:Width|Height): (\d+)', _run(['sips', '-g', 'pixelWidth', '-g', 'pixelHeight', os.path.join(out, f)], 30).stdout)
            say(f"  {f}  {' x '.join(dims)}")
    finally: shutil.rmtree(stage, ignore_errors=True)
    say(f'pictures written to {out}: look at each at full size before they ship')
    return 0
