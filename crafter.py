"""Stella's cauldron: find a recipe, add ingredients, Craft, and read the result, each step checked by sight."""
import difflib
import io
import re
import time

import numpy as np
from PIL import Image

import game
import merchant
import potions
import selector

OPEN_S = 3.0             # screen to show after E
STEP_S = 2.0             # click result to show
SAY_S = 3.0              # message under the window
COUNTDOWN_S = 8.0        # warning counts down 5 s first
TRIES = 3
CLICK_TRAVEL_S = 0.1
HOVER_S = 0.08           # so Roblox sees the hover
TWICE_GAP_S = 0.06
AUTO_TINT = 22           # Auto's tint lifts green over red by ~45
TOAST_S = 1.5
WARNINGS = 4             # Add Everything's own, then one per item that asks
LATE_WARN_S = 1.0        # a warning can show just after "completed"
TICK_DIFF = 12           # the tick changes the box this much
last_shot = None         # PNG of the last crafted notification
last_words = ''          # blue notification that isn't a crafted one
CRAFTED = re.compile(r'[ct][rf]?[aof]{1,2}ft')   # OCR reads: crofted, trafted, cfaft
BADGE = (111, 181, 255)  # notification text blue
BADGE_TOL = 14
BADGE_INK = 0.0005       # share of that blue before reading
OCR_EVERY_S = 0.5        # rate limit; else the last answer stands
CRAFTABLE = ('Heavenly Potion', 'Godlike Potion', 'Potion of Bound', 'Warp Potion', 'Godly Potion (Zeus)',
             'Godly Potion (Poseidon)', 'Godly Potion (Hades)', 'Fortune Potion I', 'Fortune Potion II',
             'Fortune Potion III', 'Haste Potion I', 'Haste Potion II', 'Haste Potion III', 'Jewelry Potion',
             'Zombie Potion', 'Diver Potion', 'Rage Potion', 'Forbidden Potion I', 'Forbidden Potion II',
             'Forbidden Potion III', 'Void Heart')
GODLY = {'Godly Potion (Zeus)': (251, 249, 6), 'Godly Potion (Poseidon)': (2, 239, 251),
         'Godly Potion (Hades)': (187, 12, 15)}     # same name: told apart by colour
COLOUR_TOL = 70

_seen = (0.0, False)


def _fit(w, h, ratio):
    """The biggest box of width/height `ratio` inside w x h."""
    return min(w, h * ratio), min(h, w / ratio)


def layout():
    """Screen boxes (l, t, r, b) and click points (x, y) of the crafting screen, from the client's size."""
    l, t, r, b = game.client_box((0.0, 0.0, 1.0, 1.0))
    W, H = r - l, b - t
    o = {}
    # recipe list, right
    fw, fh = _fit(0.25 * W, 0.7 * H, 0.635)
    px, py = l + 0.975 * W - fw, t + 0.5 * H - fh / 2
    th = 0.05 * fh
    o['close'] = (px + 0.994 * fw - th / 2, py + th / 2)
    lw, lh = 0.98 * fw, 0.885 * fh
    lx, ly = px + (fw - lw) / 2, py + 0.995 * fh - lh
    sw, sh = 0.95 * lw, 0.04 * lh
    sx, sy = lx + (lw - sw) / 2, ly + 0.01 * lh
    o['search'] = (sx + 0.535 * sw, sy + sh / 2)
    hw, hh = 0.98 * lw, 0.947 * lh
    o['list'] = (lx + (lw - hw) / 2, ly + lh - hh, lx + (lw + hw) / 2, ly + lh)
    # picked recipe, left
    gw, gh = _fit(0.25 * W, 0.6 * H, 0.726)
    gx, gy = l + 0.025 * W, t + 0.5 * H - gh / 2
    o['left'] = (gx, gy, gx + gw, gy + 0.415 * gh)
    cw, ch = 0.96 * gw, 0.46 * gh
    cx, cy = gx + (gw - cw) / 2, gy + 0.985 * gh - ch
    bh = 0.125 * ch
    by = cy + 0.95 * ch - bh / 2
    o['open'] = (cx + 0.345 * cw, by)
    o['auto'] = (cx + 0.69 * cw, by - bh / 2, cx + 0.97 * cw, by + bh / 2)
    o['bar'] = (cx, by - 0.75 * bh, cx + cw, by + 0.75 * bh)
    # Add Ingredients window, middle
    iw, ih = _fit(0.3 * W, 0.3 * H, 1.5)
    ix, iy = l + (W - iw) / 2, t + (H - ih) / 2
    mx = ix + iw / 2
    o['window'] = (ix, iy, ix + iw, iy + ih)
    tb = 0.075 * ih
    o['title'] = (ix, iy, ix + iw, iy + tb)
    o['shut'] = (ix + 0.994 * iw - tb / 2, iy + tb / 2)
    vw, vh = 0.99 * 0.99 * iw, 0.95 * 0.8 * ih
    vy = iy + 0.09 * ih + 0.025 * 0.8 * ih
    o['rows'] = (mx - vw / 2, vy, mx + vw / 2, vy + vh)
    rw = 0.98 * 0.99 * vw
    o['row'] = (mx - rw / 2, rw, 0.2 * vh)                  # left, width, row pitch
    bw, bb = 0.99 * iw, 0.085 * ih
    y = iy + 0.99 * ih - bb / 2
    o['everything'], o['craft'] = (mx - 0.25 * bw, y), (mx + 0.25 * bw, y)
    pt, ph, pw = iy + 0.09 * ih, 0.9 * ih, 0.99 * iw
    y, qw, qh = pt + 0.8375 * ph, 0.9 * pw, 0.125 * ph
    o['confirm'] = (mx + 0.2675 * qw, y)
    o['confirm_box'] = (mx + 0.0425 * qw, y - qh / 2, mx + 0.4925 * qw, y + qh / 2)
    kh = 0.075 * ih
    x, y = o['dont_show'] = (mx - 0.3625 * iw + kh / 2, iy + 0.7 * ih)
    o['dont_show_box'] = (x - 0.3 * kh, y - 0.3 * kh, x + 0.3 * kh, y + 0.3 * kh)
    o['lines'] = (cx, cy, cx + cw, by - 0.75 * bh)         # the Recipe panel's lines, above Open Recipe
    o['messages'] = (l + 0.275 * W, t + 0.7 * H, l + 0.725 * W, t + 0.87 * H)
    o['toasts'] = (l + 0.795 * W, t + 0.399 * H, l + 0.994 * W, t + 0.899 * H)
    return o


def _ocr(img):
    result, _ = merchant.reader()(np.ascontiguousarray(img[..., ::-1]), use_cls=False)
    return selector.rows(result or [])


def _read(name):
    """Text rows (text, x, y) on screen inside one of layout()'s boxes, or a given (l, t, r, b)."""
    box = [int(v) for v in (layout()[name] if isinstance(name, str) else name)]
    img = game.grab(box)
    if img is None or img.size == 0:
        return []
    l, t, r, b = game.client_box((0.0, 0.0, 1.0, 1.0))
    k = min(2.0, max(1.0, 1440.0 / max(1, b - t)))         # small windows: upscale first
    if k > 1:
        img = np.asarray(Image.fromarray(img).resize((int(img.shape[1] * k), int(img.shape[0] * k)), Image.LANCZOS))
    return [(text, box[0] + x / k, box[1] + y / k) for text, x, y in _ocr(img)]


def _text(name):
    return ' '.join(t for t, _, _ in _read(name)).lower()


def named(lines, name):
    """The row that reads as `name` (a tier mark may lead it), or None. A longer name (... II) doesn't count."""
    want = selector.squash(name)
    for line in lines:
        if selector.squash(line[0]).endswith(want):
            return line
    for line in lines:
        s = selector.squash(line[0])
        if len(s) <= len(want) + 1 and difflib.SequenceMatcher(None, want, s).ratio() >= 0.88:
            return line
    return None


def _until(check, seconds, halt):
    """check() until it gives something, about every 0.3 s for `seconds`. None if it never does or on halt."""
    for _ in range(max(1, round(seconds * game.LIMIT / (0.3 * max(game.PACE, 0.1))))):    # paced steps, same total
        t0 = time.monotonic()
        v = check()
        if v:
            return v
        if halt():
            return None
        game.paced(max(0.1, 0.3 - (time.monotonic() - t0) / max(game.PACE, 0.1)))   # a slow read was wait enough
    return None


def _click(at, twice=False):
    x, y = int(at[0]), int(at[1])
    ok = game.click(x, y, park=False, settle=HOVER_S, travel=CLICK_TRAVEL_S)
    if ok and twice:                                        # a lone click is often missed
        time.sleep(TWICE_GAP_S)
        ok = game.click(x, y, park=False, settle=0, travel=0)
    time.sleep(0.05)
    return ok


def screen_up():
    return 'recipe' in _text('bar')


def window_up():
    return 'ingredient' in _text('title')


def _messages():
    return [t for t, _, _ in _read('messages')]


def _said(before, seconds, halt):
    """The newest message under the window, lower-cased, once one shows that wasn't there `before`."""
    def new():
        now = _messages()
        if now and (not before or len(now) > len(before) or now[-1] != before[-1]):
            return now[-1].lower()
        return None
    return _until(new, seconds, halt)


def open_cauldron(halt):
    """Press E at the cauldron until its crafting screen shows."""
    if screen_up():
        return True
    for _ in range(TRIES):
        if halt() or game.take_foreground() is None:
            return False
        game.down('e')
        game.paced(0.1)
        game.up('e')
        if _until(screen_up, OPEN_S, halt):
            return True
    return False


def base(name):
    """The name the list shows: the three Godly Potions all read "Godly Potion"."""
    return 'Godly Potion' if name in GODLY else name


def _share(box, rgb):
    """Share of a box's pixels near a colour."""
    img = game.grab([int(v) for v in box])
    if img is None or img.size == 0:
        return 0.0
    return float((np.abs(img.astype(int) - np.array(rgb)).max(axis=2) <= COLOUR_TOL).mean())


def _ours(box, name):
    """Is this box the godly potion `name`: more of its colour than of either brother's?"""
    mine = _share(box, GODLY[name])
    return mine > 0.002 and all(mine > _share(box, c) for n, c in GODLY.items() if n != name)


def boxes(n=3):
    """The first n recipe boxes in the list, top down: full width, each 0.195 of the list's height, 0.02 apart."""
    l, t, r, b = layout()['list']
    w, h = r - l, b - t
    return [(l + 0.015 * w, t + k * 0.215 * h, r - 0.015 * w, t + k * 0.215 * h + 0.195 * h) for k in range(n)]


def _godly_row(lines, name):
    """(text, x, y) of a Godly Potion's box: by its god's name if read, else by its colour; None if unclear."""
    god = selector.squash(name.split('(')[1])
    hit = next((l for l in lines if 'godlypotion' in selector.squash(l[0]) and god in selector.squash(l[0])), None)
    if hit:
        return hit
    for box in boxes():
        if _ours(box, name):
            return name, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return None


def _godly_left(name):
    """Is the picked recipe on the left this Godly Potion (its god's name read, or its colour)?"""
    text = selector.squash(' '.join(t for t, _, _ in _read('left')))
    god = selector.squash(name.split('(')[1])
    return 'godlypotion' in text and (god in text or _ours(layout()['left'], name))


def _term(name):
    """The search for exactly `name`: the box takes ( ) - . and the like as wildcards unless they follow a %."""
    return re.sub(r'([^a-z0-9 ])', r'%\1', name.lower()) + '$'


def find_recipe(name, halt, say=print):
    """Search `name`, click it in the list, and wait for it to show on the left. True once picked."""
    godly, shown = name in GODLY, base(name)
    top = boxes(2)                                          # exact search: top rows only
    top = (top[0][0], top[0][1], top[1][2], top[1][3])
    find = (lambda: _godly_row(_read('list'), name)) if godly else (lambda: named(_read(top), shown))
    picked = (lambda: _godly_left(name)) if godly else (lambda: named(_read('left'), shown))
    for go in range(TRIES):
        if halt() or not _click(layout()['search'], twice=True):
            return False
        game.type_term(_term(shown)[:-1] if godly else _term(shown))     # brothers may carry the god's name
        game.tap(game.Key.enter)
        game.paced(0.4)
        row = _until(find, STEP_S, halt)
        if row is None:
            say('WARN recipe_missing item="%s" attempt=%d/%d' % (name, go + 1, TRIES))
            continue
        _click(row[1:], twice=True)
        if _until(picked, STEP_S, halt):
            return True
        say('WARN recipe_not_opened item="%s" attempt=%d/%d' % (name, go + 1, TRIES))
    return False


def _fill(box):
    """Middle colour of a button's see-through fill, its white words and border left out."""
    img = game.grab([int(v) for v in box])
    if img is None or img.size == 0:
        return None
    a = img.reshape(-1, 3).astype(int)
    a = a[a.min(axis=1) < 215]
    return np.median(a, axis=0) if len(a) > 20 else None


def auto_state():
    """'off', 'add' (green), 'craft' (teal-blue) or None: Auto's fill minus Open Recipe's, so the backdrop cancels."""
    o = layout()
    a, (ox, _) = o['auto'], o['open']
    half = (a[2] - a[0]) / 2
    auto, ref = _fill(a), _fill((ox - half, a[1], ox + half, a[3]))
    if auto is None or ref is None:
        return None
    r, g, b = auto - ref
    if g - r < AUTO_TINT:
        return 'off'
    return 'craft' if b - r > 0.4 * (g - r) else 'add'


def auto_on():
    """Is Auto teal-blue (auto add + auto craft)?"""
    return auto_state() == 'craft'


def auto_add_on():
    """Is Auto green (auto add only)? A press from there turns auto craft on too."""
    return auto_state() == 'add'


def turn_auto_on(halt):
    """Press Auto until blue (off -> green -> blue); never pressed when already blue."""
    a = layout()['auto']
    at = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
    game.paced(0.5)     # ponytail: guess - Auto may briefly show the last recipe's colour
    for _ in range(TRIES):
        if auto_on():
            return True
        if not auto_add_on():                               # off: first press is auto add
            if halt() or not _click(at):
                return False
            got = _until(lambda: auto_on() or auto_add_on() or (warning_up() and 'warn'), STEP_S, halt)
            if got == 'warn':                               # "add everything with Auto?" prompt
                _warned(halt)
                got = _until(lambda: auto_on() or auto_add_on(), STEP_S, halt)
            if not got:
                continue
            if auto_on():
                return True
        if halt() or not _click(at):                        # green: again for auto craft
            return False
        if _until(auto_on, STEP_S, halt):
            return True
    return False


def warning_up():
    return 'warning' in _text('window')


def open_recipe(halt):
    """Open Recipe until the Add Ingredients window shows."""
    for _ in range(TRIES):
        if window_up():
            return True
        if halt() or not _click(layout()['open']):
            return False
        if _until(window_up, STEP_S, halt):
            return True
    return False


def _warned(halt):
    """Answer the warning's countdown: wait for its right button to stop counting, then press it."""
    def counted():
        words = _text('confirm_box')
        return 'add' in words                               # countdown "( 5 )" ... "( 1 )" comes first

    return bool(_until(counted, COUNTDOWN_S, halt)) and _click(layout()['confirm'])


def craft(name, halt):
    """Press Craft: 'crafted', 'not enough', or None if nothing was said; the notification picture goes in last_shot."""
    global last_shot
    last_shot = None
    if halt() or not _click(layout()['craft']):
        return None

    def answer():
        if crafted_seen(name):
            return 'crafted'
        said = [t.lower() for t in _messages() if 'everything' not in t.lower()]    # Add Everything's is older
        return ('crafted' if any('crafted' in t for t in said) else 'not enough') if said else None
    got = _until(answer, SAY_S, halt)
    if got == 'crafted':
        _until(lambda: crafted_seen(name), TOAST_S, halt)
        last_shot = toast_png()
    return got


def toast_png():
    """PNG of the bottom-right notifications, or None."""
    img = game.grab([int(v) for v in layout()['toasts']])
    if img is None or img.size == 0:
        return None
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(img)).save(buf, 'PNG')
    return buf.getvalue()


def auto_start(halt, item='Heavenly Potion', say=print):
    """Pick `item`, turn its Auto on, Add Everything and Craft: 'crafted', 'added' (not done yet), or False."""
    took, t0 = [], time.monotonic()

    def step(name):
        nonlocal t0
        took.append('%s_s=%.1f' % (name, time.monotonic() - t0))
        t0 = time.monotonic()

    def done(msg):
        say('%s %s' % (msg, ' '.join(took)) if took else msg)

    if not find_recipe(item, halt, say):
        say('ERROR recipe_not_found item="%s"' % item)
        return False
    step('recipe')
    if not turn_auto_on(halt):
        done('ERROR auto_not_blue item="%s"' % item)
        return False
    step('auto')
    if not open_recipe(halt):
        done('ERROR recipe_open_failed item="%s"' % item)
        return False
    step('open')
    if not everything(halt, say):
        say('WARN add_all_unconfirmed item="%s"' % item)
    step('add_all')
    short = short_lines()
    if short:                                               # Add Everything by hand fills them: pressed again
        say('WARN recipe_short item="%s" lines="%s" retry=add_all' % (item, short))
        everything(halt, say)
        short = short_lines()
        if short:
            say('recipe_short item="%s" lines="%s"' % (item, short))
    got = craft(item, halt)
    step('craft')
    if got == 'crafted':
        done('auto_start_crafted item="%s"' % item)
        return 'crafted'
    done('auto_start_added item="%s" crafted=false' % item)
    return False if halt() else 'added'


def auto_back(halt, item='Heavenly Potion', say=print):
    """The cauldron keeps one recipe's Auto at a time: pick `item` again and turn its Auto back on."""
    if window_up():
        _click(layout()['shut'])
    if find_recipe(item, halt, say) and turn_auto_on(halt):
        say('auto_back_on item="%s"' % item)
        return True
    if not halt():
        say('ERROR auto_back_failed item="%s"' % item)
    return False


def _tick(halt):
    """Tick Don't show again, checked by sight: a click the game missed leaves the box as it was."""
    grab = lambda: game.grab([int(v) for v in layout()['dont_show_box']])
    before = grab()

    def changed():
        now = grab()
        return now is not None and now.shape == before.shape and \
            np.abs(now.astype(int) - before.astype(int)).mean() > TICK_DIFF
    for _ in range(2):
        if halt() or not _click(layout()['dont_show']):
            return False
        if before is None or before.size == 0:
            return True                                     # can't see it: trust the click
        if _until(changed, 0.6, halt):
            return True
    return False


def everything(halt, say=print):
    """Add Everything, answering each warning it brings: its own with Don't show again ticked, so it never asks
    again, and any "... will be consumed" one. True once it says it's done with no warning left up."""
    if halt() or not _click(layout()['everything']):
        return False
    done = lambda: any('completed' in t.lower() for t in _messages())
    for _ in range(WARNINGS):
        got = _until(lambda: (warning_up() and 'warn') or (done() and 'done'), SAY_S, halt)
        if got == 'done' and not _until(warning_up, LATE_WARN_S, halt):     # a warning can follow the message
            return True
        if not got:
            return False
        if got == 'warn':
            words = _text('window')
            say('add_all_warning text="%s"' % words[:100])
            if 'again' in words and not _tick(halt):
                say('WARN dont_show_again_not_ticked')
            if not _warned(halt):
                return False
    return False


COUNT = re.compile(r'\(?\s*(\d+)\s*/\s*(\d+)\s*\)?')


def recipe_lines(rows):
    """(name, have, need) per Recipe panel line; OCR gives the name and its ( have / need ) as rows at one height."""
    counts, names = [], []
    for text, _, y in rows:
        m = COUNT.search(text.replace(',', '').replace('.', ''))
        (counts.append((int(m.group(1)), int(m.group(2)), y)) if m else names.append((text, y)))
    out = []
    for have, need, y in counts:
        name = min(names, key=lambda n: abs(n[1] - y), default=None)
        out.append((name[0] if name and abs(name[1] - y) < 15 else '?', have, need))
    return out


def short_lines():
    """Recipe panel lines still short, as "Speed Potion 423/1000"; '' when all are full or none were read."""
    return ', '.join('%s %d/%d' % l for l in recipe_lines(_read('lines')) if l[1] < l[2])


def _blue_rows(img):
    """Rows (every 2nd pixel) holding the notification blue, or None if there's too little of it."""
    s = img[::2, ::2].astype(np.int16)
    hit = (np.abs(s - BADGE).max(axis=2) <= BADGE_TOL)
    if hit.mean() < BADGE_INK:
        return None
    return np.flatnonzero(hit.any(axis=1)) * 2


def crafted_name():
    """Potion named by a "crafted" notification, '' if unreadable, None if none (other blue words go in last_words)."""
    global last_words
    last_words = ''
    img = game.grab([int(v) for v in layout()['toasts']])
    rows = None if img is None or img.size == 0 else _blue_rows(img)
    if rows is None:
        return None
    crop = img[max(0, rows[0] - 20):min(img.shape[0], rows[-1] + 20)]
    if crop.shape[0] < 400:
        crop = np.asarray(Image.fromarray(crop).resize((crop.shape[1] * 2, crop.shape[0] * 2), Image.LANCZOS))
    text = selector.squash(' '.join(t for t, _, _ in _ocr(crop)))
    if not CRAFTED.search(text):
        last_words = text
        return None
    names = sorted({base(p) for p in CRAFTABLE} | set(potions.POTIONS), key=len, reverse=True)
    return next((p for p in names if selector.squash(p) in text), '')


def crafted_seen(name='Heavenly Potion'):
    """Cheap: is a bottom-right "Potion Crafted" notification for `name` up? OCR only when its blue is there."""
    global _seen
    box = [int(v) for v in layout()['toasts']]
    img = game.grab(box)
    if img is None or img.size == 0:
        return False
    rows = _blue_rows(img)
    if rows is None:
        _seen = (0.0, False)
        return False
    now = time.monotonic()
    if now - _seen[0] < OCR_EVERY_S:
        return _seen[1]
    top, bottom = max(0, rows[0] - 20), min(img.shape[0], rows[-1] + 20)
    crop = img[top:bottom]
    if crop.shape[0] < 400:                                 # small lower line: upscale
        crop = np.asarray(Image.fromarray(crop).resize((crop.shape[1] * 2, crop.shape[0] * 2), Image.LANCZOS))
    text = selector.squash(' '.join(t for t, _, _ in _ocr(crop)))
    want = selector.squash(base(name))
    # ponytail: lower line unreadable: a "crafted" naming no other potion is ours
    others = [p for p in potions.POTIONS if selector.squash(p) != want and selector.squash(p) in text]
    hit = bool(CRAFTED.search(text)) and (want in text or not others)
    _seen = (now, hit)
    return hit


def close(halt):
    """Shut the Add Ingredients window and the screen with their X; True once the screen's gone."""
    for _ in range(TRIES):
        if halt():
            return False
        if window_up():
            _click(layout()['shut'])
        if not screen_up():
            return True
        _click(layout()['close'])
        if _until(lambda: not screen_up(), STEP_S, halt):
            return True
    return False
