"""icon - the gateway's icon, a chip on a board, as PNG: what browsers show in the tab, bookmarks and favorites (Safari
takes no icon drawn by a script or given as SVG data, and shows the domain's first letter instead).

Drawn here with the standard library (zlib, struct): a rounded board in --p1, a --silicon chip with three --gold pins on
each side and a --gold die, in the 32-unit design the page itself used, read from the page's own dark-theme tokens (one
source of truth: a change of colors in dash.html changes the icon). Each pixel averages 4x4 samples, so the edges are
smooth at any size. Made once per size and kept.
"""
import os, re, struct, zlib

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dash.html')
SAMPLES = 4                                               # samples per pixel along each axis
_made = {}


def tokens(names=('--p1', '--silicon', '--gold')):
    """The dark theme's colors for these tokens, from dash.html's :root[data-theme="dark"] block: {name: (r, g, b)}."""
    css = open(PAGE).read()
    at = css.index(':root[data-theme="dark"]')
    block = css[at:css.index('}', at)]
    out = {}
    for n in names:
        m = re.search(re.escape(n) + r':\s*#([0-9a-fA-F]{6})\b', block)
        if not m: raise ValueError(f'{n}: no colour in the dark theme block of {PAGE}')
        out[n] = tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    return out


def shapes():
    """The design, back to front: [(x, y, w, h, corner radius, colour)] in a 32-unit square."""
    c = tokens()
    board, chip, gold = c['--p1'], c['--silicon'], c['--gold']
    out = [(0, 0, 32, 32, 7, board)]
    for p in (11, 15, 19):
        out += [(p, 6, 2, 3, 0, gold), (p, 23, 2, 3, 0, gold), (6, p, 3, 2, 0, gold), (23, p, 3, 2, 0, gold)]
    return out + [(9, 9, 14, 14, 2, chip), (13, 13, 6, 6, 1, gold)]


def _inside(px, py, x, y, w, h, r):
    if px < x or py < y or px > x + w or py > y + h: return False
    if not r: return True
    cx = x + r if px < x + r else x + w - r if px > x + w - r else px          # the nearest point of the inner rectangle
    cy = y + r if py < y + r else y + h - r if py > y + h - r else py
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def png(size=64):
    """The icon at size x size pixels, as PNG bytes (RGBA)."""
    if size in _made: return _made[size]
    sh, k, unit = shapes(), SAMPLES, 32.0 / size
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0.0
            for j in range(k):
                for i in range(k):
                    sx, sy = (px + (i + 0.5) / k) * unit, (py + (j + 0.5) / k) * unit
                    for (x, y, w, h, rad, col) in reversed(sh):             # the frontmost shape that holds the sample
                        if _inside(sx, sy, x, y, w, h, rad):
                            r += col[0]; g += col[1]; b += col[2]; a += 1; break
            n = k * k
            row += bytes((int(r / a + 0.5), int(g / a + 0.5), int(b / a + 0.5), int(255 * a / n + 0.5)) if a else (0, 0, 0, 0))
        rows.append(bytes(row))

    def chunk(kind, data): return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    raw = b''.join(b'\x00' + row for row in rows)                               # filter 0 on every row
    out = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))
    _made[size] = out
    return out
