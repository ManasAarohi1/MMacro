"""Fixed game labels found by their look instead of OCR.
Each label is a mask (labels_data.py), scaled to the label's spot and matched on edges.
Every answer is yes, no, or None (unsure): on None the caller uses OCR."""
import base64
import io

import cv2
import numpy as np
from PIL import Image

import labels_data

YES = 0.6                # this good or better: it's there
NO = 0.4                 # below this everywhere: it's not there
MARGIN = 0.1             # and this much better than other words that could be in the same spot
BIG = 32                 # bigger text is shrunk to about this first
LOW, HIGH = 0.62, 1.45   # font size / box height: text fills ~0.6 to all of its box
STEP = 1.1               # sizes tried first, then the best is refined in 2% steps
SHARP = 1.0              # blur of the edges the letters are scored on
SLACK = (2, 0.02)        # px, and share of the word's width, each letter run may sit off
ROUGH = 14               # px the text is shrunk to for the first, rough look
COVER = 0.5              # a match counts in full only if this share of its outline holds the colour, or its letters
                         # stand out this much from the gaps between them
NEAR = 40                # a pixel this far off a blend of fill and outline colour isn't text

_masks, _cache = {}, {}


def mask(name):
    if name not in _masks:
        png = base64.b64decode(''.join(labels_data.LABELS[name]))
        _masks[name] = np.asarray(Image.open(io.BytesIO(png)).convert('L'), np.float32) / 255
    return _masks[name]


def edges(g, blur):
    """Edge strength of a grey picture, blurred a little."""
    dx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    dy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.GaussianBlur(cv2.magnitude(dx, dy), (0, 0), blur)


def resize(g, f):
    if f == 1.0:
        return g
    size = (max(2, round(g.shape[1] * f)), max(2, round(g.shape[0] * f)))
    return cv2.resize(g, size, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_LINEAR)


def template(name, px, f, blur, ring=False):
    """Edges of `name` at font size px, scaled by f. ring: only its 1 px outline."""
    key = (name, round(px * 8), round(f * 200), blur, ring)
    if key not in _cache:
        m = np.pad(resize(mask(name), px / labels_data.REF), 3)
        if ring:
            m = cv2.dilate(m, np.ones((3, 3), np.uint8)) - m
        _cache[key] = edges(resize(m, f), blur)
    return _cache[key]


def unlined(g, wide, tall):
    """g without straight runs longer than a letter stroke (frame lines, plain areas)."""
    lines = np.maximum(cv2.morphologyEx(g, cv2.MORPH_OPEN, np.ones((1, max(3, int(wide))), np.uint8)),
                       cv2.morphologyEx(g, cv2.MORPH_OPEN, np.ones((max(3, int(tall)), 1), np.uint8)))
    return np.maximum(g - lines, 0)


def keyed(img, colour, base):
    """How much of `colour` each pixel holds (255 = all), on the line from `base` to it.
    Pixels off that line count for nothing."""
    return lines(img, [(colour, base)])[0]


def lines(img, pairs):
    """keyed() for several (colour, base) pairs, each pixel counted only for its nearest line."""
    d = [np.asarray(img[..., i], np.float32) for i in range(3)]
    out, offs = [], []
    for colour, base in pairs:
        v = [float(c) - float(o) for c, o in zip(colour, base)]
        e = [d[i] - float(base[i]) for i in range(3)]
        t = np.clip((e[0] * v[0] + e[1] * v[1] + e[2] * v[2]) * (1.0 / sum(x * x for x in v)), 0, 1)
        off = np.maximum(np.maximum(np.abs(e[0] - t * v[0]), np.abs(e[1] - t * v[1])), np.abs(e[2] - t * v[2]))
        out.append((255 * t) * np.clip(1 - off * (1.0 / NEAR), 0, 1))
        offs.append(off)
    if len(pairs) > 1:
        nearest = np.argmin(np.stack(offs), axis=0)
        for i, g in enumerate(out):
            g[nearest != i] = 0
    return out


def size(name, px):
    """(width, height) of `name`'s template at font size px."""
    h, w = mask(name).shape
    return w * px / labels_data.REF, h * px / labels_data.REF


def sizes(name, box_w, box_h, lo=LOW, hi=HIGH, extra=0.0):
    """Font sizes `name` can show at in a box: height in [lo, hi], never wider than the box.
    extra: more em of text on the same line."""
    out, r = [], lo
    while r <= hi * 1.001:
        out.append(round(min(r * box_h, box_w / (labels_data.ADV[name] + extra)), 1))
        r *= STEP
    return sorted(set(out))


def flat(e, shape):
    """Spots where the edges under the template hardly vary (a plain fill)."""
    th, tw = shape[:2]
    s1, s2 = cv2.integral2(e)[:2]
    box = lambda s: s[th:, tw:] - s[:-th, tw:] - s[th:, :-tw] + s[:-th, :-tw]
    return box(s2) / (th * tw) - (box(s1) / (th * tw)) ** 2 < 4.0


def scan(e, t):
    """Best score of template t anywhere in e, and where: (score, (x, y))."""
    if t.shape[0] > e.shape[0] or t.shape[1] > e.shape[1] or t.size == 0:
        return -1.0, None
    r = cv2.patchNaNs(cv2.matchTemplate(e, t, cv2.TM_CCOEFF_NORMED), 0.0)
    r[flat(e, t.shape)] = 0.0                       # no edges there: the score is noise over nothing
    _, v, _, at = cv2.minMaxLoc(r)
    return float(v), at


def best(e, name, pxs, f, blur, ring, keep=1):
    """The `keep` best (score, (x0, y0, x1, y1), px), one per size."""
    out = []
    for px in pxs:
        t = template(name, px, f, blur, ring)
        v, at = scan(e, t)
        if at is not None:
            out.append((v, (at[0], at[1], at[0] + t.shape[1], at[1] + t.shape[0]), px))
    return sorted(out, key=lambda q: q[0], reverse=True)[:keep]


def chunks(name, n=4):
    """Each word cut into even runs of at most n letters, as (first, end) indexes."""
    out, i = [], 0
    for word in name.split(' '):
        k = -(-len(word) // n)
        cut = [i + round(j * len(word) / k) for j in range(k + 1)]
        out += list(zip(cut, cut[1:]))
        i += len(word) + 1
    return out


def pieces(e, name, px, f, at, ring):
    """Mean score of the word's letter runs, each looked for within a few px of where `at` puts it."""
    t = template(name, px, f, SHARP, ring)
    k = px / labels_data.REF
    xs = [(x * k + 3) * f for x in labels_data.CHARS[name]]          # the template has 3 px of padding
    slack = SLACK[0] + int(SLACK[1] * t.shape[1])
    total = weight = 0.0
    for a, b in chunks(name):
        x0, x1 = max(0, int(xs[a]) - 1), min(t.shape[1], int(np.ceil(xs[b])) + 1)
        part = t[:, x0:x1]
        l, top = max(0, at[0] + x0 - slack), max(0, at[1] - slack)
        area = e[top:at[1] + t.shape[0] + slack, l:at[0] + x1 + slack]
        v = scan(area, part)[0]
        total += max(0.0, v) * (x1 - x0)
        weight += x1 - x0
    return total / weight if weight else -1.0


def peaks(e, name, pxs, f, ring, n=3):
    """The n best spots for `name` over all sizes, at least half a word apart."""
    out = []
    for px in pxs:
        t = template(name, px, f, 1.5, ring)
        if t.shape[0] > e.shape[0] or t.shape[1] > e.shape[1]:
            continue
        r = cv2.patchNaNs(cv2.matchTemplate(e, t, cv2.TM_CCOEFF_NORMED), 0.0)
        r[flat(e, t.shape)] = 0.0
        for _ in range(n):
            _, v, _, (x, y) = cv2.minMaxLoc(r)
            if v <= 0:
                break
            out.append((float(v), (x, y, x + t.shape[1], y + t.shape[0]), px))
            r[max(0, y - t.shape[0] // 2):y + t.shape[0] // 2 + 1, max(0, x - t.shape[1] // 2):x + t.shape[1] // 2 + 1] = 0
    out.sort(key=lambda q: q[0], reverse=True)
    kept = []
    for q in out:
        if all(abs(q[1][1] - k[1][1]) > (k[1][3] - k[1][1]) / 2 or abs(q[1][0] - k[1][0]) > (k[1][2] - k[1][0]) / 2
               for k in kept):
            kept.append(q)
    return kept[:n]


def find(img, name, pxs, box=None, plane=None, ring=False):
    """(score, (x0, y0, x1, y1), px) of the best spot for `name` inside box, in img pixels.
    plane: what to match on (brightness if None). ring: match the text's 1 px outline."""
    x0 = y0 = 0
    if box is not None:
        x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
        img = img[y0:max(y0, int(np.ceil(box[3]))), x0:max(x0, int(np.ceil(box[2])))]
    top = (-1.0, None, None)
    if img.size == 0:
        return top
    f = min(1.0, BIG / min(pxs))
    g = plane(img) if plane else np.ascontiguousarray(img.max(axis=2), np.float32)
    g = resize(g, f)
    g = unlined(g, 0.8 * max(pxs) * f + 4, 0.9 * max(pxs) * f + 4)       # letters: 0.6 em wide, 0.75 em tall
    if g.std() < 1.0:
        return top
    if ring and (g > 64).sum() < 0.2 * outline(name, min(pxs)) * f:
        return top                                          # hardly any of its colour here: -1
    fr = min(1.0, ROUGH / (min(pxs) * f))                  # the rough look is done at about ROUGH px
    for v, at, px in peaks(edges(resize(g, fr), 1.5), name, pxs, f * fr, ring):
        at = tuple(int(q / fr) for q in at)
        s = int(0.05 * (at[2] - at[0])) + int(2 / fr) + 6
        l, t = max(0, at[0] - s), max(0, at[1] - s)
        part = g[t:at[3] + s, l:at[2] + s]
        got = best(edges(part, 2.0), name, [px * (1 + 0.02 * k) for k in range(-4, 5)], f, 2.0, ring)
        if not got:
            continue
        v, at2, px = got[0]
        v = pieces(edges(part, SHARP), name, px, f, at2, ring)
        v *= min(1.0, (lit(part, name, px, f, at2) if ring else contrast(part, name, px, f, at2)) / COVER)
        if v > top[0]:
            at = (l + at2[0], t + at2[1], l + at2[2], t + at2[3])
            top = (v, (x0 + at[0] / f, y0 + at[1] / f, x0 + at[2] / f, y0 + at[3] / f), px)
    return top


def picture(name, px, f):
    """The 1 px outline of `name` at font size px, scaled by f."""
    key = (name, round(px * 8), round(f * 200), 'outline')
    if key not in _cache:
        m = np.pad(resize(mask(name), px / labels_data.REF), 3)
        _cache[key] = resize(cv2.dilate(m, np.ones((3, 3), np.uint8)) - m, f) > 0.4
    return _cache[key]


def outline(name, px):
    """How many pixels the outline of `name` covers at font size px."""
    return int(picture(name, px, 1.0).sum())


def lit(g, name, px, f, at):
    """Share of the outline's pixels that hold the colour where `at` puts the word."""
    m = picture(name, px, f)
    win = g[at[1]:at[1] + m.shape[0], at[0]:at[0] + m.shape[1]]
    if win.shape != m.shape:
        return 0.0
    return float((cv2.dilate(win, np.ones((3, 3), np.uint8))[m] > 64).mean())     # within 1 px


def contrast(g, name, px, f, at):
    """How much brighter the letters are than the gaps between them: 1 for text on a plain fill, near 0 for a flat patch."""
    key = (name, round(px * 8), round(f * 200), 'ink')
    if key not in _cache:
        ink = resize(np.pad(resize(mask(name), px / labels_data.REF), 3), f)
        _cache[key] = (ink > 0.5, cv2.dilate(ink, np.ones((5, 5), np.uint8)) < 0.1)
    ink, gap = _cache[key]
    win = g[at[1]:at[1] + ink.shape[0], at[0]:at[0] + ink.shape[1]]
    if win.shape != ink.shape or not ink.any() or not gap.any():
        return 0.0
    on, off = float(win[ink].mean()), float(win[gap].mean())
    return max(0.0, on - off) / max(on, 20.0)


def around(name, pxs, cx, cy, dx, dy):
    """The box the whole word lies in when centred within (dx, dy) of (cx, cy)."""
    w, h = size(name, max(pxs))
    return cx - w / 2 - dx, cy - h / 2 - dy, cx + w / 2 + dx, cy + h / 2 + dy


def call(v):
    """A score as sure yes (True), sure no (False) or unsure (None)."""
    return True if v >= YES else (False if v < NO else None)


# ---------------------------------------------------------------- Aura Storage: Equip / Unequip
def green(img):
    """How green and bright: Equip's text runs from aaff9d to 6bff60."""
    c = img.astype(np.float32)
    return np.clip(c[..., 1] - np.maximum(c[..., 0], c[..., 2]), 0, None) * (c[..., 1] / 255)


def red(img):
    """How red and bright: Unequip's text runs from ffacdf to ff5356."""
    c = img.astype(np.float32)
    return np.clip(c[..., 0] - c[..., 1], 0, None) * (c[..., 0] / 255)


def equip_button(rgb):
    """'equip' or 'unequip' from a grab of the Equip button, or None if unsure."""
    if rgb is None or rgb.size == 0 or min(rgb.shape[:2]) < 6:
        return None
    h, w = rgb.shape[:2]
    pad = max(3, h // 3)
    rgb = np.pad(rgb, ((pad, pad), (pad, pad), (0, 0)), mode='edge')
    q = find(rgb, 'Equip', sizes('Equip', w, h, 0.45, 1.3), plane=green)[0]
    u = find(rgb, 'Unequip', sizes('Unequip', w, h, 0.45, 1.3), plane=red)[0]
    if u >= YES and q < NO:
        return 'unequip'
    if q >= YES and u < NO:
        return 'equip'
    return None


# ---------------------------------------------------------------- fishing: the red "Cannot Fish" note
BODY = 'You do not have enough inventory space.'


def cannot_fish(crop, W, H):
    """True if the crop shows the red "Cannot Fish" note, False if surely not, None if unsure."""
    if crop is None or crop.size == 0:
        return None
    bw, bh = 0.9 * 0.199 * W, 0.03 * H
    pad = int(bh)
    crop = np.pad(crop, ((pad, pad), (pad, pad), (0, 0)), mode='edge')
    title = find(crop, 'Cannot Fish', sizes('Cannot Fish', bw, bh), plane=red)[0]
    if call(title) is not True:
        return call(title)
    body = find(crop, BODY, sizes(BODY, bw, 14, 0.5, 1.2), plane=red)[0]
    return True if body >= YES else None


# ---------------------------------------------------------------- Biome Selector
DRIVES = {'Radiant Star Drive': (28, 115, 255), 'Infernal Flame Drive': (255, 55, 55),   # each row's text outline
          'Unknown Void Drive': (255, 255, 255), 'Corrupt Ruin Drive': (151, 46, 255),
          'Golden Dune Drive': (255, 188, 134), 'Chilling Frost Drive': (158, 236, 255),
          'Abyss Curse Drive': (88, 155, 255), 'Raging Gale Drive': (138, 255, 189),
          'Angelic Hymn Drive': (255, 206, 128)}
PRIMARY, SECONDARY, WARN = (13, 36, 89), (134, 188, 255), (255, 131, 131)   # text fill, outline,
                                                                            # warning outline


def tinted(colour, base):
    """plane for find(): text outlined in `colour` over a `base` fill."""
    return lambda img: keyed(img, colour, base)


def fill(colour):
    """A Drive's fill colour: a quarter of its colour."""
    return tuple(v * 0.25 for v in colour)


def selector(img, W, H, want=None, picked=None):
    """The Biome Selector's fixed text in `img` (centred on the screen) as [(text, (x0, y0, x1, y1))].
    [] if surely not up, None if unsure or if `want` can't be called either way.
    picked: the Drive just clicked; the window takes on its colours."""
    if img is None or img.size == 0:
        return None
    h = min(0.38 * H, 0.38 * W / 1.75)
    w = 1.75 * h
    sx, sy = 0.012 * W + 3, 0.025 * H + 3                   # shake, bob
    cx, cy = img.shape[1] / 2, img.shape[0] / 2
    pxs = sizes('BIOME SELECTOR', 0.95 * w, 0.075 * h)
    box = around('BIOME SELECTOR', pxs, cx - 0.025 * w, cy - 0.4625 * h, sx, sy)
    tries = [picked] if picked in DRIVES else []
    tries = tries + [None] + [d for d in DRIVES if d not in tries]   # picking a Drive turns the window its colours
    seen, v = -1.0, -1.0
    for d in tries:
        colour = (DRIVES[d], fill(DRIVES[d])) if d else (SECONDARY, PRIMARY)
        v, at, px = find(img, 'BIOME SELECTOR', pxs, box, tinted(*colour), True)
        seen = max(seen, v)
        if v >= YES:
            break
    if v < YES:                     # surely not up only when none of its colours is there; else let OCR look
        return [] if seen < 0 else None
    r = px / (0.075 * h)                                    # this window's font size / box height

    def near(name, bw, bh, extra=0.0):
        return [min(k * r * bh, bw / (labels_data.ADV[name] + extra)) for k in (0.92, 1.0, 1.08)]

    found = [('BIOME SELECTOR', at)]
    mx, my = (at[0] + at[2]) / 2 + 0.025 * w, (at[1] + at[3]) / 2 + 0.4625 * h      # the window's middle now
    lx, ly, lw, lh = mx - 0.49 * w, my - 0.5 * h + 0.087 * h + 0.02 * 0.913 * h, 0.72 * w, 0.86 * 0.913 * h
    s = 0.01 * h + 3
    status_y = my + 0.5 * h - 0.02 * 0.913 * h - 0.0375 * 0.913 * h
    calls = {}                    # the status line shows one of these; warnings are drawn in their own red
    for name, extra, c in (('RECHARGING...', 2.4, WARN), ('CONNECTION UNSTABLE', 0.0, WARN),     # 2.4 em: ' 75%'
                           ('BIOME SELECTOR IS READY TO BE USED', 0.0, colour[0]),
                           ('SENDING REQUEST...', 0.0, colour[0]), ('REQUEST SENT', 0.0, colour[0])):
        p = near(name, lw, 0.075 * 0.913 * h, extra)
        x = lx + lw / 2 - extra * max(p) / 2           # the line is centred, so RECHARGING... sits left of the middle
        calls[name] = find(img, name, p, around(name, p, x, status_y, s + 0.3 * max(p) * (extra > 0), s),
                           tinted(c, colour[1]), True)
    w1, w2 = calls['RECHARGING...'][0], calls['CONNECTION UNSTABLE'][0]
    warn = max(w1, w2)
    other = max(calls[n][0] for n in calls if n not in ('RECHARGING...', 'CONNECTION UNSTABLE'))
    if warn >= YES and warn - other >= MARGIN and abs(w1 - w2) >= MARGIN:
        name = 'RECHARGING...' if w1 > w2 else 'CONNECTION UNSTABLE'
        found.append((name, calls[name][1]))
    elif warn >= NO and not (other >= YES and other - warn >= MARGIN):
        return None
    pxs = {name: near(name, 0.98 * lw, 0.1 * lh) + [min(r * 0.13 * lh, 0.98 * lw / (labels_data.ADV[name] + 1.2))]
           for name in DRIVES}      # Drive row height, bigger when hovered
    boxes = [around(name, p, lx + lw / 2, ly + lh / 2, s, lh / 2 + s) for name, p in pxs.items()]
    box = tuple(int(pick(b[i] for b in boxes)) for i, pick in enumerate((min, min, max, max)))
    x0, y0 = max(0, box[0]), max(0, box[1])
    maps = dict(zip(DRIVES, lines(img[y0:box[3], x0:box[2]], [(c, fill(c)) for c in DRIVES.values()])))
    hits = []
    for name in DRIVES:
        v, at, _ = find(img, name, pxs[name], box, lambda part, m=maps[name]: m, True)
        if name == want and call(v) is None:
            return None
        hits.append((v, name, at and (at[1] + at[3]) / 2, at))
    rows = []
    for v, name, y, at in sorted(hits, key=lambda q: q[0], reverse=True):
        if v < YES or any(abs(y - y2) < 0.06 * lh for y2 in rows):
            continue
        rows.append(y)                                      # one Drive per row, and clearly that one
        close = [n for v2, n, y2, _ in hits if n != name and y2 is not None and abs(y2 - y) < 0.06 * lh
                 and v - v2 < MARGIN]
        if not close:
            found.append((name, at))
        elif want in close + [name]:
            return None
    rw = 0.7 * lw                                           # Confirm: left of a 0.7 x 0.1 pair, 0.7 down the list
    p = near('Confirm', 0.48 * rw, 0.1 * lh)
    v, at, _ = find(img, 'Confirm', p, around('Confirm', p, lx + lw / 2 - 0.26 * rw, ly + 0.7 * lh, s, s),
                    tinted(colour[0], (0, 0, 0)), True)    # Confirm's text is black
    if v >= YES:
        found.append(('Confirm', at))
    elif want == 'Confirm' and call(v) is None:
        return None
    return found
