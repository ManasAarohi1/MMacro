"""Button boxes from gui_points.json, scaled to the Roblox window, then found by sight once per window size."""
import base64
import bisect
import ctypes
import io
import json
import os
import sys
import time

import cv2
import numpy as np
from PIL import Image

import buttons_data
import game
import labels

_SIZES = None


def _sizes():
    global _SIZES
    if _SIZES is None:
        here = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, 'gui_points.json'), encoding='utf-8') as f:
            _SIZES = {tuple(int(v) for v in k.split('x')): boxes for k, boxes in json.load(f)['sizes'].items()}
    return _SIZES


NAMES = {(2560, 1440): '1440p fullscreen', (2560, 1369): '1440p windowed', (1920, 1080): '1080p fullscreen',
         (1920, 1009): '1080p windowed', (1366, 768): '768p fullscreen', (1366, 697): '768p windowed'}


WINDOWS = ('auraStorage', 'inventory', 'dailyRewards')     # buttons in a centred, width-scaled window
TOPBAR = 58                                                 # Roblox top bar, not scaled
FRAMES = {      # fixed-shape windows: (left, top, width) on a w x h client
    'merchantShop': lambda w, h: (0.95 * w - min(w / 2, h * 1.15), h / 2 - min(w / 2, h * 1.15) / 4.6,
                                  min(w / 2, h * 1.15)),
    'dialogue': lambda w, h: (w / 2 - min(w / 2, h * 0.27 * 3.031) / 2, 0.625 * h, min(w / 2, h * 0.27 * 3.031)),
}


def sizes():
    return list(_sizes())


def _nearest(cw, ch):
    """Closest stored size: same shape first, then nearest height."""
    return min(_sizes(), key=lambda s: (round(abs(s[0] / s[1] - cw / ch), 2), abs(s[1] - ch)))


def _lerp(xs, ys, x):
    i = min(max(bisect.bisect_left(xs, x), 1), len(xs) - 1)     # the stored sizes either side, else the end pair
    return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - xs[i - 1]) / (xs[i] - xs[i - 1])


def _bar_box(name, ow, oh):
    """Fishing bar box at an unstored size; it is placed by width."""
    full = sorted(s for s in _sizes() if abs(s[0] / s[1] - 16 / 9) < 0.01)
    rows = [(w, h) + tuple(_sizes()[(w, h)][name]) for w, h in full]
    ws = [r[0] for r in rows]
    bw = _lerp(ws, [r[4] for r in rows], ow)
    bh = _lerp(ws, [r[5] for r in rows], ow)
    cx = ow / 2 + _lerp(ws, [l + a / 2 - w / 2 for w, h, l, t, a, b in rows], ow)
    cy = 0.797 * oh - _lerp(ws, [0.797 * h - t - b / 2 for w, h, l, t, a, b in rows], ow)   # bottom at 0.797 of height
    return cx - bw / 2, cy - bh / 2, bw, bh


def _result_box(name, ow, oh, size):
    """The catch window at an unstored size: centred and not a plain stretch."""
    width = lambda w, h: min(0.185 * w + 250, 0.8 * (0.185 * h + 250))
    l, t, bw, bh = _sizes()[size][name]
    k = width(ow, oh) / width(*size)
    return ow / 2 + (l - size[0] / 2) * k, oh / 2 + (t - size[1] / 2) * k, bw * k, bh * k


def rule_box(name):
    """(left, top, right, bottom) on screen from the stored boxes and scaling rules, or None with no Roblox window."""
    w = game.roblox_window()
    if w is None:
        return None
    x0, y0, cw, ch = game.client_rect(w)
    if cw <= 0 or ch <= 0:
        return None
    s = game.window_scale(w)
    own = (round(cw / s), round(ch / s))        # Roblox layout size, before DPI stretch
    size = own if own in _sizes() else _nearest(*own)
    l, t, bw, bh = _sizes()[size][name]
    if name.startswith(WINDOWS) and own not in _sizes():
        # half the client wide, fixed shape: width sets the scale
        s = cw / size[0]
        y = lambda v: y0 + ch / 2 + (v - size[1] / 2) * s
        return round(x0 + l * s), round(y(t)), round(x0 + (l + bw) * s), round(y(t + bh))
    frame = next((f for k, f in FRAMES.items() if name.startswith(k)), None)
    if frame and own not in _sizes():
        (fl, ft, fw), (ol, ot, ow) = frame(*size), frame(cw, ch)
        k = ow / fw
        return (round(x0 + ol + (l - fl) * k), round(y0 + ot + (t - ft) * k),
                round(x0 + ol + (l + bw - fl) * k), round(y0 + ot + (t + bh - ft) * k))
    if name.startswith('fishingMinigame') and own not in _sizes():
        l, t, bw, bh = (_result_box(name, *own, size) if 'Result' in name else _bar_box(name, *own))
        size = own
    sx, sy = cw / size[0], ch / size[1]
    if name.startswith('collection') and own not in _sizes():
        # Collection sits below the fixed-height top bar
        bar = TOPBAR * s
        y = lambda v: round(y0 + bar + (v - TOPBAR) * (ch - bar) / (size[1] - TOPBAR))
        return round(x0 + l * sx), y(t), round(x0 + (l + bw) * sx), y(t + bh)
    return (round(x0 + l * sx), round(y0 + t * sy), round(x0 + (l + bw) * sx), round(y0 + (t + bh) * sy))


SURE = 0.7          # sight score (the lower of edges and brightness) that counts as found
REACH = 0.03        # how far from its rule spot a button is looked for, share of client height
REACH_EXACT = 0.01  # same at a size with exact stored boxes
SCALES = np.arange(0.8, 1.26, 0.03)     # size against the rule box, then 0.25% steps around the best
BUSY = 0.3          # a spot needs this share of the picture's edge detail (plain areas match anything)
RETRY_S = 30.0      # a window that was not up is looked for again after this
AGREE = 2           # px two looks may differ by (a window still sliding in moves more)
SAME_PX = 3         # sight this close to the rule spot just confirms it (scaled text wobbles a few px)
SEEN_BY = (     # window: the fixed-looking part found by sight; its other buttons keep their place in it
    ('mainInterface', 'mainInterfaceInventory'), ('inventory', 'inventoryItemsTab'),
    ('auraStorage', 'auraStorageAuraRegularTab'),
    ('fishingShop', 'fishingShopSellTab'), ('fishingMinigameResult', 'fishingMinigameResultHeaderLabel'),
    ('fishingMinigame', 'fishingMinigamePromptWindowRoot'),
)
KEEP_SHAPE = ('mainInterface',)    # the left buttons stay square when the rules stretch their boxes
_seen, _tried, _pictures = None, {}, {}


def seen_by(name):
    """The button whose look finds `name` (itself if it has a picture), or None."""
    if name in buttons_data.BUTTONS:
        return name
    return next((by for p, by in SEEN_BY if name.startswith(p)), None)


def _real(w):
    """A real window (stand-ins from tests aren't saved)."""
    try:
        return bool(ctypes.windll.user32.IsWindow(w.hwnd))
    except Exception:
        return False


def _spots():
    global _seen
    if _seen is None:
        stored = game.load_settings().get('seen') if _real(game.roblox_window()) else None
        _seen = dict(stored) if isinstance(stored, dict) else {}
    return _seen


def _save():
    if _real(game.roblox_window()):
        game.save_settings({'seen': _spots()})


def _key(w):
    return '%dx%d' % tuple(game.client_rect(w)[2:4])


def forget(name=None):
    """Find a button's window (every one with None) by sight again."""
    for spots in _spots().values():
        for k in [k for k in spots if name is None or k == seen_by(name)]:
            spots.pop(k)
    _tried.clear()
    _save()


def box(name):
    """(left, top, right, bottom) on screen, where sight found its window; None with no Roblox window."""
    b = rule_box(name)
    by, w = seen_by(name), game.roblox_window()
    at = _spots().get(_key(w), {}).get(by) if b is not None and by and w is not None else None
    if at is None:
        return b
    a = rule_box(by)
    x0, y0, cw, ch = game.client_rect(w)
    k = at[2]
    cx = x0 + at[0] * cw + ((b[0] + b[2]) - (a[0] + a[2])) / 2 * k
    cy = y0 + at[1] * ch + ((b[1] + b[3]) - (a[1] + a[3])) / 2 * k
    hw, hh = (b[2] - b[0]) / 2 * k, (b[3] - b[1]) / 2 * k
    return round(cx - hw), round(cy - hh), round(cx + hw), round(cy + hh)


def pictures(name):
    if name not in _pictures:
        _pictures[name] = [(l, t, w, h, np.asarray(Image.open(io.BytesIO(base64.b64decode(''.join(chunks)))).convert('L'),
                                                   np.float32)) for l, t, w, h, chunks in buttons_data.BUTTONS[name]]
    return _pictures[name]


def _match(e, grey, sums, pic, kx, ky):
    """Best (score, x, y) of one picture scaled by kx, ky, scored by edges and brightness."""
    l, t, w, h = pic[:4]
    tw, th = round(pic[4].shape[1] * kx), round(pic[4].shape[0] * ky)
    if min(tw, th) < 6 or tw > e.shape[1] or th > e.shape[0]:
        return None
    small = cv2.resize(pic[4], (tw, th), interpolation=cv2.INTER_AREA if kx < 1 else cv2.INTER_LINEAR)
    tpl = labels.edges(small, 1.0)
    r = cv2.matchTemplate(e, tpl, cv2.TM_CCOEFF_NORMED)
    rh, rw = r.shape
    s1, s2 = sums
    win = lambda s: s[th:th + rh, tw:tw + rw] - s[:rh, tw:tw + rw] - s[th:th + rh, :rw] + s[:rh, :rw]
    var = win(s2) / (tw * th) - (win(s1) / (tw * th)) ** 2
    r[var < BUSY * float(tpl.var())] = -1
    _, score, _, (x, y) = cv2.minMaxLoc(r)
    if score > -1:
        score = min(score, float(cv2.matchTemplate(grey[y:y + th, x:x + tw], small, cv2.TM_CCOEFF_NORMED)[0, 0]))
    return score, x + (l + w / 2) * kx, y + (t + h / 2) * ky


def _k(name, pic, b, exact):
    """Picture scale (x, y) that matches rule box b."""
    if exact or not name.startswith(KEEP_SHAPE):
        return (b[2] - b[0]) / pic[2], (b[3] - b[1]) / pic[3]
    k = ((b[2] - b[0]) + (b[3] - b[1])) / (pic[2] + pic[3])
    return k, k


def find(name, rgb, b, exact=False):
    """(x, y, score, size) of `name`'s middle in rgb near rule box b, or None if not sure."""
    grey = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2GRAY).astype(np.float32)
    e = labels.edges(grey, 1.0)
    sums = cv2.integral2(e, sdepth=cv2.CV_64F)
    best = None
    for pic in pictures(name):
        (kx, ky), mine = _k(name, pic, b, exact), None
        for steps in ((1.0,),) if exact else (SCALES, None):
            if steps is None:
                steps = np.clip(mine[3] + np.arange(-0.02, 0.0201, 0.0025), SCALES[0], SCALES[-1])
            for m in steps:
                got = _match(e, grey, sums, pic, kx * m, ky * m)
                if got is not None and (mine is None or got[0] > mine[2]):
                    mine = (got[1], got[2], got[0], m)
            if mine is None:
                break
        if mine is not None and (best is None or mine[2] > best[2]):
            best = mine
    return best if best is not None and best[2] >= SURE else None


def area(name, b, cw, ch, exact):
    """Screen box sight searches for `name`'s pictures when its rule box is b."""
    r = (REACH_EXACT if exact else REACH) * ch + 0.5 * min(b[2] - b[0], b[3] - b[1])
    grow = 1.0 if exact else SCALES[-1] + 0.03        # room for the biggest size tried
    out = [b[0] - r, b[1] - r, b[2] + r, b[3] + r]
    for l, t, w, h, grey in pictures(name):
        kx, ky = (v * grow for v in _k(name, (l, t, w, h), b, exact))
        out = [min(out[0], b[0] - l * kx - r), min(out[1], b[1] - t * ky - r),
               max(out[2], b[2] + (grey.shape[1] - l - w) * kx + r), max(out[3], b[3] + (grey.shape[0] - t - h) * ky + r)]
    return tuple(round(v) for v in out)


def look(name):
    """Find `name`'s window by sight, once per window size, and keep the spot. True when its spot is known."""
    by, w = seen_by(name), game.roblox_window()
    if by is None or w is None:
        return False
    key = _key(w)
    if by in _spots().get(key, {}):
        return True
    if time.monotonic() - _tried.get((key, by), -RETRY_S) < RETRY_S:
        return False
    _tried[(key, by)] = time.monotonic()
    b = rule_box(by)
    x0, y0, cw, ch = game.client_rect(w)
    s = game.window_scale(w)
    exact = (round(cw / s), round(ch / s)) in _sizes()
    a = area(by, b, cw, ch, exact)
    a = (max(a[0], x0), max(a[1], y0), min(a[2], x0 + cw), min(a[3], y0 + ch))
    rb = (b[0] - a[0], b[1] - a[1], b[2] - a[0], b[3] - a[1])
    first = find(by, game.grab(a), rb, exact)
    if first is None:
        return False
    time.sleep(0.1)
    again = find(by, game.grab(a), rb, exact)
    if again is None or max(abs(first[0] - again[0]), abs(first[1] - again[1])) > AGREE:
        return False
    x, y, m = a[0] + again[0], a[1] + again[1], again[3]
    if max(abs(x - (b[0] + b[2]) / 2), abs(y - (b[1] + b[3]) / 2)) <= SAME_PX and abs(m - 1) <= 0.02:
        x, y, m = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, 1.0       # sight agrees: keep the exact rule spot
    _spots().setdefault(key, {})[by] = [round(float(x - x0) / cw, 5), round(float(y - y0) / ch, 5), round(float(m), 3)]
    _save()
    print('button_seen name=%s size=%s moved=%d,%d scale=%.2f score=%.2f' % (
        by, key, round(x - (b[0] + b[2]) / 2), round(y - (b[1] + b[3]) / 2), m, again[2]))
    return True


def centre(name):
    """Middle of a button. A Calibrator point wins, then where sight found it, then the stored box."""
    stored = game.load_points()
    for key in [k for k, v in game.BUILT_IN.items() if v == name]:
        if key in stored:
            return game.to_screen(stored[key])
    try:
        look(name)
    except Exception as e:      # sight is a bonus: the rule spot still works
        print('WARN button_look_failed name=%s error="%s"' % (name, e))
    b = box(name)
    return None if b is None else ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)


def click(name, settle=0.3):
    """Click a button's middle. False if there's no window or Roblox won't come forward."""
    c = centre(name)
    if c is None or not game.click(*c, park=False):
        return False
    game.paced(settle)
    return True


def grab(name):
    b = box(name)
    return None if b is None else game.grab(b)


def choice(colour):
    """Where the 'green' or 'red' dialogue choice is, or None."""
    b = box('dialogueChoicesFrame')
    if b is None:
        return None
    rgb = game.grab(b).astype(int)
    r, g, bl = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    main, a, c = (g, r, bl) if colour == 'green' else (r, g, bl)
    mask = (main >= 120) & (main > a + 40) & (main > c + 40)
    if mask.sum() < 30:
        return None
    # biggest patch only: an aura glow can't drag it
    patches = game._group(mask, (b[0], b[1]), 6, 12, 30)
    return (int(patches[0][0]), int(patches[0][1])) if patches else None


def dialogue_up():
    """Both green and red choices showing."""
    return choice('green') is not None and choice('red') is not None


def leave_dialogue(tries=3, gap=0.3, skip=False):
    """Press Leave until the dialogue is gone; `skip` clicks "Click to skip" first (no shop open)."""
    pressed = False
    for n in range(tries):
        at = choice('red') if dialogue_up() else None       # red alone is an aura or biome, not Leave
        if at is None:
            if pressed:
                print('dialogue_gone presses=%d' % n)
            break
        if skip:
            click('dialogueSkipDialogue', 0.2)
            at = choice('red') if dialogue_up() else None
            if at is None:
                print('dialogue_gone via=skip')
                return True
        print('dialogue_leave_press x=%d y=%d attempt=%d/%d' % (at[0], at[1], n + 1, tries))
        game.click(*at, park=False)
        pressed = True
        time.sleep(gap)
    else:
        if pressed and dialogue_up():
            print('WARN dialogue_leave_stuck n=%d' % tries)
    return pressed
