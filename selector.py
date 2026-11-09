"""Biome Selector: use it from the Inventory, click the picked biome's Drive, then Confirm.
Only unlocked Drives are listed, so the Drive is found by name."""
import difflib
import re
import time

import numpy as np
from PIL import Image

import game
import labels
import merchant
import points

TERM = 'biome selector'
DRIVES = {'STARFALL': 'Radiant Star Drive', 'HELL': 'Infernal Flame Drive', 'NULL': 'Unknown Void Drive',
          'CORRUPTION': 'Corrupt Ruin Drive', 'SAND STORM': 'Golden Dune Drive', 'SNOWY': 'Chilling Frost Drive',
          'RAINY': 'Abyss Curse Drive', 'WINDY': 'Raging Gale Drive', 'HEAVEN': 'Angelic Hymn Drive'}   # list order
COLOURS = {'Radiant Star Drive': (28, 115, 255), 'Infernal Flame Drive': (255, 55, 55),     # each Drive's row border
           'Unknown Void Drive': (255, 255, 255), 'Corrupt Ruin Drive': (151, 46, 255),    # is drawn in it, and so is
           'Golden Dune Drive': (255, 188, 134), 'Chilling Frost Drive': (158, 236, 255),  # Confirm's once it's picked
           'Abyss Curse Drive': (88, 155, 255), 'Raging Gale Drive': (138, 255, 189),
           'Angelic Hymn Drive': (255, 206, 128)}
COLOUR_TOL = 12          # Golden Dune and Angelic Hymn are only 18 apart
EVERY_S = 60 * 60
RECHARGE_S = 3600        # its "RECHARGING... n%" counts down from an hour
AREA = (0.27, 0.26, 0.73, 0.74)     # the window's spot on the client, with room for its bobbing
OPEN_S = 3.5             # it types its title in and adds the Drives one by one
CONFIRM_S = 1.5          # "Are you sure?" types in, then Confirm grows in
APPLY_S = 9.0            # request animation, then the window closes itself
SCROLLS = 3              # the last Drives sit past the bottom of the list
OCR_ROWS = 700           # small screens are scaled up to this many rows first: Drive names get tiny
REACH = 0.025            # largest bob + shake, as a share of the screen height

_ref = None              # (screen box, grey title picture, how far it can move) from the last read


def squash(text):
    return re.sub(r'[^a-z]', '', text.lower())


def edges(img):
    """Row-to-row change of the brightest channel: the title's letters and borders stand out over the see-through bar."""
    g = img.max(axis=2).astype(np.float32)
    return np.abs(np.diff(g, axis=0, prepend=g[:1]))


def read(want=None, picked=None):
    """Text rows in the window's area as (text, x, y) on screen. Keeps the title's picture to follow the bob.
    Fixed words are found by look; OCR reads it all only when unsure."""
    global _ref
    l, t, r, b = game.client_box(AREA)
    img = game.grab((l, t, r, b))
    if img is None or img.size == 0:
        return []
    lines = seen(img, l, t, want, picked)
    if lines is None:
        k = max(1.0, OCR_ROWS / img.shape[0])
        big = img if k == 1.0 else np.asarray(Image.fromarray(img).resize((int(img.shape[1] * k),
                                                                           int(img.shape[0] * k)), Image.LANCZOS))
        result, _ = merchant.reader()(big[..., ::-1].copy(), use_cls=False)
        result = [([[px / k, py / k] for px, py in box], text, score) for box, text, score in result or []]
        lines = rows(result, l, t)
    _ref = None
    title = [q for q in lines if squash(q[0]) == 'biomeselector']
    if title:
        x, y, W, H = title[0][1] - l, title[0][2] - t, (r - l) / 0.46, (b - t) / 0.48
        hy, hx = int(0.02 * H) + 2, int(0.45 * 1.75 * min(0.38 * H, 0.38 * W / 1.75))   # the whole title bar
        hx = min(hx, x, img.shape[1] - x)
        if hy <= y < img.shape[0] - hy and hx > 20:
            _ref = ((l + x - hx, t + y - hy, l + x + hx, t + y + hy), edges(img[y - hy:y + hy, x - hx:x + hx]),
                    int(REACH * H) + 1)
    return lines


def seen(img, left, top, want=None, picked=None):
    """The window's fixed words as read() gives them, with RECHARGING's number from OCR. None if unsure."""
    cl, ct, cr, cb = game.client_box((0.0, 0.0, 1.0, 1.0))
    found = labels.selector(img, cr - cl, cb - ct, want, picked)
    if found is None:
        return None
    lines = []
    for text, (x0, y0, x1, y1) in found:
        if text == 'RECHARGING...':
            text = percent(img, x0, y0, x1, y1)
            if text is None:
                return None
        lines.append((text, left + int((x0 + x1) / 2), top + int((y0 + y1) / 2)))
    return lines


def percent(img, x0, y0, x1, y1):
    """'RECHARGING... n%' from OCR of the number. None if OCR missed the '%'."""
    adv, h = (x1 - x0) / 13.5, y1 - y0                       # 13 letters of RobotoMono, all as wide
    crop = img[max(0, int(y0 - 0.3 * h)):int(y1 + 0.3 * h), max(0, int(x1 - 0.5 * adv)):int(x1 + 5.5 * adv)]
    if crop.size == 0:
        return None
    k = 48 / crop.shape[0]                                  # OCR reads digits best about this tall
    crop = np.asarray(Image.fromarray(crop).resize((max(1, int(crop.shape[1] * k)), 48), Image.LANCZOS))
    crop = np.pad(crop, ((10, 10), (10, 10), (0, 0)), mode='edge')
    result, _ = merchant.reader()(np.ascontiguousarray(crop[..., ::-1]), use_cls=False)
    m = re.match(r'\D*(\d{1,3})\s*%', ' '.join(q[1] for q in result or []))
    return 'RECHARGING... %s%%' % m.group(1) if m else None


def drift():
    """Pixels the window has moved since the last read: where the title's picture is now."""
    if _ref is None:
        return 0
    (l, t, r, b), want, reach = _ref
    img = game.grab((l, t - reach, r, b + reach))
    if img is None or img.shape[0] != b - t + 2 * reach:
        return 0
    now, n = edges(img), want.shape[0]
    a = want - want.mean()
    best, score = 0, -2.0
    for s in range(2 * reach + 1):
        c = now[s:s + n] - now[s:s + n].mean()
        d = np.sqrt((a * a).sum() * (c * c).sum())
        v = (a * c).sum() / d if d else -1.0
        if v > score:
            best, score = s - reach, v
    return best if score > 0.4 else 0


def list_box():
    """The Drive list's screen box at rest, and how far the window can bob."""
    l, t, r, b = game.client_box((0.0, 0.0, 1.0, 1.0))
    W, H = r - l, b - t
    h = min(0.38 * H, 0.38 * W / 1.75)
    w = 1.75 * h
    x0, y0 = l + (W - w) / 2, t + (H - h) / 2 + 0.087 * h + 0.02 * 0.913 * h
    return (x0 + 0.01 * w, y0, 0.72 * w, 0.86 * 0.913 * h), int(REACH * H) + 1


def locate(colour, near=None, cols=None):
    """Screen y of the middle of the box bordered in `colour`, from a fresh grab. None if not seen."""
    (lx, ly, lw, lh), reach = list_box()
    x0, x1 = cols or (lx, lx + lw)
    top = int(ly - reach)
    img = game.grab((int(x0), top, int(x1), int(ly + lh + reach)))
    if img is None or img.size == 0:
        return None
    hit = np.abs(img.astype(int) - colour).max(axis=2) < COLOUR_TOL
    full = np.flatnonzero(hit.mean(axis=1) > 0.6)
    lines = [y for i, y in enumerate(full) if i == 0 or y - full[i - 1] > 1]
    pairs = [(a, c) for a in lines for c in lines if 0.06 * lh <= c - a <= 0.16 * lh]
    if not pairs:
        return None
    want = (near - top) if near is not None else (pairs[0][0] + pairs[0][1]) / 2
    a, c = min(pairs, key=lambda q: abs((q[0] + q[1]) / 2 - want))
    return top + (a + c) / 2


def press(x, y, colour, cols=None):
    """Click the `colour`-bordered box nearest y, found again on arrival. Without the colour, follow the title."""
    if locate(colour, y, cols) is None:
        return aim(x, y)
    if game.take_foreground() is None:
        return False
    game.glide(x, int(locate(colour, y, cols) or y), settle=0.05)
    game.move_to(x, int(locate(colour, y, cols) or y), settle=0.02)
    if not game.button('left', True):
        return False
    time.sleep(0.03)
    return game.button('left', False)


def aim(x, y):
    """Click what was at (x, y) in the last read. The window bobs, so: measure drift, glide, measure, nudge, press."""
    if game.take_foreground() is None:
        return False
    game.glide(x, y + drift(), settle=0.05)
    game.move_to(x, y + drift(), settle=0.02)
    if not game.button('left', True):
        return False
    time.sleep(0.03)
    return game.button('left', False)


def rows(result, left=0, top=0):
    """OCR pieces joined into rows: it reads 'Raging Gale Drive' as 'Raging' and 'GaleDrive'."""
    pieces = sorted(((min(p[0] for p in b), max(p[0] for p in b), min(p[1] for p in b), max(p[1] for p in b), text)
                     for b, text, _ in result), key=lambda q: (q[2] + q[3]) / 2)
    out = []
    for x0, x1, y0, y1, text in pieces:
        row = out[-1] if out else None
        if (row and abs((y0 + y1) / 2 - (row[2] + row[3]) / 2) < (y1 - y0) / 2
                and min(abs(x0 - row[1]), abs(row[0] - x1)) < y1 - y0):        # Confirm and Cancel stay apart
            row[0], row[1], row[4] = min(row[0], x0), max(row[1], x1), row[4] + [(x0, text)]
        else:
            out.append([x0, x1, y0, y1, [(x0, text)]])
    return [(' '.join(t for _, t in sorted(r[4])), left + int((r[0] + r[1]) / 2), top + int((r[2] + r[3]) / 2))
            for r in out]


def find(lines, words):
    """The row that reads as `words` (hover adds [ ]), or None."""
    want = squash(words)
    best, score = None, 0.0
    for line in lines:
        r = difflib.SequenceMatcher(None, want, squash(line[0])).ratio()
        if r > score:
            best, score = line, r
    return best if score >= 0.8 else None


def status(lines):
    """('recharging', seconds left), ('unstable', None), ('ready', None) or (None, None) if the window isn't up."""
    text = ' '.join(t for t, _, _ in lines).upper()
    m = re.search(r'RECHARG\w*\W*(\d{1,3})\s*%', text)
    if m:
        return 'recharging', max(60, (1 - min(100, int(m.group(1))) / 100.0) * RECHARGE_S)
    if 'UNSTABLE' in text:
        return 'unstable', None
    if 'READY' in text or 'SELECTOR' in text.replace(' ', '') or 'DRIVE' in text:
        return 'ready', None
    return None, None


def close(lines=()):
    """Click its X, moved by how far the title sits from its usual spot."""
    b = points.box('biomeSelectorCloseButton')
    if b is None:
        return
    x, y = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
    title = [l for l in lines if squash(l[0]) == 'biomeselector']
    if title and abs(title[0][2] - y) < (b[3] - b[1]) * 1.5:
        aim(x, title[0][2])                             # where the window sat in that read, plus its drift since
    else:
        game.click(x, y, park=False)
    game.paced(0.3)


def shut():
    """Close the window if it's up. Left up, it covers the Inventory and every later item use fails."""
    lines = read()
    if status(lines)[0]:
        close(lines)


def scroll(at, notches):
    game.move_to(*at)
    for _ in range(abs(notches)):                       # one notch at a time, so the end of the list stays in view
        game._mouse(0x0800, data=-120 if notches > 0 else 120)
        game.paced(0.05)


def use(biome, halt, say):
    """Select `biome` (a DRIVES key). Returns (outcome, wait): 'used', 'recharging' (wait = seconds left),
    'unstable', 'refused', 'no drive', 'no selector' or 'stopped'."""
    drive = DRIVES[biome]
    if not merchant.use_item(TERM, halt, say):
        return ('stopped' if halt() else 'no selector'), None
    lines, state, wait = [], None, None
    for _ in range(3):
        if not merchant.nap(OPEN_S, halt):
            shut()
            return 'stopped', None
        lines = read(drive)
        state, wait = status(lines)
        if state:
            break
    if state is None:
        say('the Biome Selector window did not show (read: %s)' % (' | '.join(t for t, _, _ in lines)[:200] or 'nothing'))
        return 'no selector', None
    if state != 'ready':
        say('Biome Selector %s%s: closing it' % (state, ' (%d min left)' % (wait // 60) if wait else ''))
        close(lines)
        return state, wait
    colour = COLOURS[drive]
    (lx, ly, lw, lh), _ = list_box()
    hit = find(lines, drive)
    if hit is None and locate(colour) is not None:          # OCR missed it but its colour is in view
        hit = (drive, int(lx + lw / 2), int(locate(colour)))
    at = None
    for step in [1] * SCROLLS + [-3 * SCROLLS]:         # down a notch at a time, then back to the top for a last look
        if hit is not None:
            break
        named = [q for q in lines if 'drive' in q[0].lower() and not q[0].lstrip('[').lower().startswith('sys')]
        at = (named[len(named) // 2][1], named[len(named) // 2][2]) if named else at
        if at is None:
            break
        scroll(at, step)
        if not merchant.nap(0.6, halt):
            shut()
            return 'stopped', None
        lines = read(drive)
        hit = find(lines, drive)
        if hit is None and locate(colour) is not None:
            hit = (drive, int(lx + lw / 2), int(locate(colour)))
    if hit is None:
        say('no %s in the Biome Selector (read: %s)' % (drive, ', '.join(t for t, _, _ in lines if 'rive' in t) or 'no Drives'))
        close(lines)
        return 'no drive', None
    if halt() or not press(hit[1], hit[2], colour):
        shut()
        return 'stopped', None
    if not merchant.nap(CONFIRM_S, halt):
        shut()
        return 'stopped', None
    lines = read('Confirm', drive)
    ok = find(lines, 'Confirm')
    left = (lx + lw * 0.15, lx + lw * 0.49)                 # Confirm is the left one of the pair
    if ok is None and locate(colour, ly + 0.7 * lh, left) is not None:
        ok = ('Confirm', int(lx + lw * 0.32), int(ly + 0.7 * lh))
    if ok is None:
        say('picked %s but no Confirm showed' % drive)
        close(lines)
        return 'refused', None
    press(ok[1], ok[2], colour, left)
    if not merchant.nap(APPLY_S, halt):
        shut()                                          # a refusal leaves it up
        return 'stopped', None
    lines = read(picked=drive)
    if status(lines)[0]:                                # a rejected use leaves the window up
        say("the server refused it (its 20 min cooldown, or no %s left?): closing the Biome Selector" % drive)
        close(lines)
        return 'refused', None
    say('selected %s (%s)' % (biome.title(), drive))
    return 'used', None
