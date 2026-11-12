#!/usr/bin/env python3
"""pcb.py - the dashboard's background: a circuit board in sapphire, drawn as one square tile that repeats without a seam.

Two copper layers: the top layer's buses run left to right, the bottom layer's (fainter) top to bottom; each bus leaves a
tile edge where it entered the opposite one, so the tiles join exactly. Buses jog by 45 degrees, branch to vias (rings),
and fan out from chip footprints (rows of pads). The page lays it at low strength (--lace in dash.html).

    python3 tools/pcb.py        writes localmodels/pcb.svg
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = 640                                   # the tile's side
SAPPHIRE = '#0f52ba'
PITCH = 9                                 # between parallel traces
W_TOP, W_BOT = 1.6, 1.3


def bus_h(y, n, jogs):
    """n traces from x=0 to x=T, starting at y, each jog (x, dy) a 45-degree step of dy at x, run, and the step back at x2."""
    out = []
    for i in range(n):
        yy, x, d = y + i * PITCH, 0, f'M0 {y + i * PITCH}'
        for (x1, dy, x2) in jogs:
            # inner traces turn later on the way down and earlier on the way back, so the group stays parallel
            k = i if dy > 0 else n - 1 - i
            a = x1 + k * PITCH * 0.414
            b = x2 - k * PITCH * 0.414
            d += f'H{a:.1f}l{abs(dy)} {dy}H{b:.1f}l{abs(dy)} {-dy}'
        d += f'H{T}'
        out.append(d)
    return out


def bus_v(x, n, jogs):
    out = []
    for i in range(n):
        d = f'M{x + i * PITCH} 0'
        for (y1, dx, y2) in jogs:
            k = i if dx > 0 else n - 1 - i
            a = y1 + k * PITCH * 0.414
            b = y2 - k * PITCH * 0.414
            d += f'V{a:.1f}l{dx} {abs(dx)}V{b:.1f}l{-dx} {abs(dx)}'
        d += f'V{T}'
        out.append(d)
    return out


def via(x, y, r=3.4): return f'<circle cx="{x}" cy="{y}" r="{r}"/>'


def footprint(x, y, cols, rows=2, pitch=9, w=4, h=7, gap=26):
    """A chip's pads: `cols` pads on its top and bottom edges (a two-row package), `gap` apart; -> (svg, pad ends)."""
    s, ends = '', []
    for c in range(cols):
        px = x + c * pitch
        s += f'<rect x="{px - w / 2}" y="{y}" width="{w}" height="{h}" rx="1"/>'
        s += f'<rect x="{px - w / 2}" y="{y + h + gap}" width="{w}" height="{h}" rx="1"/>'
        ends.append(((px, y), (px, y + 2 * h + gap)))
    outline = f'<rect class="o" x="{x - 7}" y="{y + h + 3}" width="{(cols - 1) * pitch + 14}" height="{gap - 6}" rx="2"/>'
    return s, outline, ends


def build():
    top, bot, vias, pads, outlines = [], [], [], [], []
    # top layer: three buses across
    top += bus_h(46, 4, [(120, 36, 300), (430, -24, 560)])
    top += bus_h(262, 3, [(60, -30, 210), (350, 40, 470)])
    top += bus_h(478, 5, [(190, 28, 380)])
    # bottom layer: two buses down
    bot += bus_v(92, 3, [(150, 30, 330)])
    bot += bus_v(548, 4, [(60, -26, 240), (380, 34, 520)])
    # chip footprints with traces fanning out to vias
    s, o, ends = footprint(250, 150, 6); pads.append(s); outlines.append(o)
    for j, ((tx, ty), (bx, by)) in enumerate(ends):
        top.append(f'M{bx} {by}V{by + 10 + j * 3}l{12 + j * 2} {12 + j * 2}'); vias.append(via(bx + 12 + j * 2, by + 22 + j * 5))
    for j, ((tx, ty), _) in enumerate(ends[:3]):
        top.append(f'M{tx} {ty}V{ty - 14 - j * 4}'); vias.append(via(tx, ty - 18 - j * 4))
    s, o, ends = footprint(400, 360, 4); pads.append(s); outlines.append(o)
    for j, ((tx, ty), (bx, by)) in enumerate(ends):
        top.append(f'M{tx} {ty}V{ty - 12}l{-14 + j * 2} -14'); vias.append(via(tx - 14 + j * 2, ty - 30))
    s, o, ends = footprint(40, 380, 5); pads.append(s); outlines.append(o)
    for j, (_, (bx, by)) in enumerate(ends):
        top.append(f'M{bx} {by}V{by + 16 + (4 - j) * 3}'); vias.append(via(bx, by + 20 + (4 - j) * 3))
    # stubs from the buses to vias (where a trace changes layer)
    for (x, y, dx, dy) in ((160, 82, 18, 18), (520, 22, -16, -16), (300, 280, 20, 20), (90, 232, -14, -14), (600, 514, -18, 18)):   # each starts on a bus
        top.append(f'M{x} {y}l{dx} {dy}'); vias.append(via(x + dx + (3 if dx > 0 else -3), y + dy + (3 if dy > 0 else -3)))
    for (x, y) in ((92, 410), (566, 150), (557, 600), (101, 600)):
        vias.append(via(x, y))
    P = lambda ds, cls: ''.join(f'<path class="{cls}" d="{d}"/>' for d in ds)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {T} {T}" width="{T}" height="{T}">'
            f'<style>path{{fill:none;stroke-linecap:round;stroke-linejoin:round}}.t{{stroke:{SAPPHIRE};stroke-width:{W_TOP}}}'
            f'.b{{stroke:{SAPPHIRE};stroke-width:{W_BOT};opacity:.5}}circle{{fill:none;stroke:{SAPPHIRE};stroke-width:1.4}}'
            f'rect{{fill:{SAPPHIRE}}}rect.o{{fill:none;stroke:{SAPPHIRE};stroke-width:1;opacity:.6}}</style>'
            f'{P(bot, "b")}{P(top, "t")}{"".join(pads)}{"".join(outlines)}{"".join(vias)}</svg>\n')


if __name__ == '__main__':
    out = os.path.join(ROOT, 'localmodels', 'pcb.svg')
    open(out, 'w').write(build())
    print('wrote', os.path.relpath(out, ROOT), os.path.getsize(out), 'bytes')
