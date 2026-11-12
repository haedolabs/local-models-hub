"""icon - the hub's icon, a chip: a die with three pins on each side, as PNG (what browsers show in the tab, bookmarks and
favorites; Safari takes no icon drawn by a script or given as SVG data, and shows the domain's first letter instead) and as
SVG (the brand files).

Drawn in a 64-unit square: a rounded near-black tile, a sapphire die (a disc of radius 15.6 at the centre) and off-white
pins 3.25 wide with round ends, each from 19 units out from the centre to 26 (the square's 6..58), three per side at
-9, 0 and +9. Standard library only (zlib, struct): each pixel's coverage comes from its distance to each shape, so the
edges are smooth at any size. Made once per size and kept.

    python3 -m localmodels.icon      writes brand/ (logo.svg, logo-square.svg, mark.svg, PNGs at 1024, 400, 180 and 32)
                                     and docs/icon.png (180)
"""
import math, os, struct, zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TILE, DIE, PIN = (0x11, 0x11, 0x10), (0x0f, 0x52, 0xba), (0xec, 0xe7, 0xdf)    # near-black, sapphire, off-white
CORNER = 14                                               # the tile's corner radius
RADIUS = 15.6                                             # the die
WIDTH = 3.25                                              # the pins' width, round ends
PIN_IN, PIN_OUT = 19.0, 26 - WIDTH / 2                    # where each pin's centre line starts and ends, from the centre
OFFSETS = (-9, 0, 9)                                      # the pins along each side
_made = {}


def pins():
    """Each pin as a segment ((x1, y1), (x2, y2)) in the 64-unit square."""
    out = []
    for o in OFFSETS:
        out += [((32 + o, 32 - PIN_IN), (32 + o, 32 - PIN_OUT)), ((32 + o, 32 + PIN_IN), (32 + o, 32 + PIN_OUT)),
                ((32 - PIN_IN, 32 + o), (32 - PIN_OUT, 32 + o)), ((32 + PIN_IN, 32 + o), (32 + PIN_OUT, 32 + o))]
    return out


def hexc(c): return '#%02x%02x%02x' % c


def svg(tile=True, rounded=True):
    """The icon as SVG: on its tile (rounded or square), or the die and pins alone (tile=False), to lay on a page."""
    rx = f' rx="{CORNER}"' if rounded else ''
    back = f'<rect width="64" height="64"{rx} fill="{hexc(TILE)}"/>' if tile else ''
    lines = ''.join(f'<path d="M{a[0]:g} {a[1]:g}L{b[0]:g} {b[1]:g}"/>' for a, b in pins())
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">{back}'
            f'<circle cx="32" cy="32" r="{RADIUS}" fill="{hexc(DIE)}"/>'
            f'<g fill="none" stroke="{hexc(PIN)}" stroke-width="{WIDTH}" stroke-linecap="round">{lines}</g></svg>\n')


def _segment(x, y, a, b):
    """Distance from (x, y) to the segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - a[0] - t * dx, y - a[1] - t * dy)


def png(size=64, rounded=True):
    """The icon at size x size pixels, as PNG bytes (RGBA)."""
    if (size, rounded) in _made: return _made[(size, rounded)]
    k, segs = size / 64, pins()
    def cover(d): return min(1.0, max(0.0, 0.5 - d * k))  # d in units, negative inside
    rows = []
    for py in range(size):
        y = (py + 0.5) / k
        row = bytearray(b'\x00')                          # filter 0
        for px in range(size):
            x = (px + 0.5) / k
            if rounded:
                qx, qy = abs(x - 32) - (32 - CORNER), abs(y - 32) - (32 - CORNER)
                alpha = cover(math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - CORNER)
            else:
                alpha = 1.0
            col = list(TILE)
            for c, d in ((DIE, math.hypot(x - 32, y - 32) - RADIUS),
                         (PIN, min(_segment(x, y, a, b) for a, b in segs) - WIDTH / 2)):
                f = cover(d)
                col = [p + (q - p) * f for p, q in zip(col, c)]
            row += bytes(int(v + 0.5) for v in col) + bytes((int(255 * alpha + 0.5),))
        rows.append(bytes(row))

    def chunk(kind, data): return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    out = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(b''.join(rows), 9)) + chunk(b'IEND', b''))
    _made[(size, rounded)] = out
    return out


def write():
    """The brand files and the README's icon, from the measures above."""
    brand = os.path.join(ROOT, 'brand')
    os.makedirs(brand, exist_ok=True)
    files = {'logo.svg': svg(), 'logo-square.svg': svg(rounded=False), 'mark.svg': svg(tile=False)}
    for name, text in files.items():
        open(os.path.join(brand, name), 'w').write(text)
    for side in (1024, 400, 180, 32):
        open(os.path.join(brand, f'logo-{side}.png'), 'wb').write(png(side))
    for side in (1024, 400, 180):
        open(os.path.join(brand, f'logo-square-{side}.png'), 'wb').write(png(side, rounded=False))
    open(os.path.join(ROOT, 'docs', 'icon.png'), 'wb').write(png(180))
    return sorted(os.listdir(brand)) + ['../docs/icon.png']


if __name__ == '__main__':
    print('wrote', ', '.join(write()))
