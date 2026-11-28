"""Auto fishing: cast, wait for the bite, reel by the bar colour, close the catch, repeat."""
import difflib
import re
import time

import numpy as np

import game
import labels
import memmatch
import merchant
import points

# VIP: spawn -> Flarg, then shop -> fishing spot
TO_SHOP = [(0.0, 'down', 'w'), (0.7, 'down', 'a'), (5.2, 'up', 'w'), (5.2, 'up', 'a'), (5.3, 'down', 'w'),
           (6.4, 'up', 'w'), (6.7, 'down', 's'), (6.9, 'up', 's'), (7.1, 'down', ' '), (7.125, 'down', 'w'),
           (8.325, 'up', ' '), (8.525, 'up', 'w'), (8.825, 'down', 'e'), (9.125, 'up', 'e')]
TO_SPOT = [(0.0, 'down', 'a'), (3.0, 'up', 'a'), (3.0, 'down', 'w'), (5.3, 'up', 'w')]
# same two walks without VIP
TO_SHOP_FREE = [(0.0, 'down', 'w'), (0.875, 'down', 'a'), (6.725, 'up', 'w'), (6.725, 'up', 'a'), (6.825, 'down', 'w'),
                (8.2, 'up', 'w'), (8.5, 'down', 's'), (8.75, 'up', 's'), (8.95, 'down', ' '), (8.975, 'down', 'w'),
                (10.175, 'up', ' '), (10.725, 'up', 'w'), (11.025, 'down', 'e'), (11.325, 'up', 'e')]
TO_SPOT_FREE = [(0.0, 'down', 'a'), (3.75, 'up', 'a'), (3.75, 'down', 'w'), (6.625, 'up', 'w')]
ZOOM_OUT_S = 1.5         # zoomed in, the bobber is lost
ZOOM_TOPUP_S = 0.3
BITE_WHITE = 200         # lit ~245 (~230 over RDP), dark under 40
BITE_CAP = 244           # so a tinted bite still counts
BITE_POLL_S = 0.05       # stays lit all catch
DRY_CASTS = 2            # casts doing nothing = full bag
REEL_S = 9.0
THIN = 20 / 405          # 20 px of a 405 px bar
RUN_TOL = 20             # zone drifts ~15, fill is ~100 off
LOST_S = 1.5             # bar colour gone: click slow
NEXT_PX = 6
FILL_MIN = 130           # fill >= ~166, empty bar = 40% of the world <= ~102; ponytail: a very bright world under HDR can pass it
NO_BITE_S = 30.0
REEL_MIN_S = 1.0
BAR_SHOWS_S = 1.5
GONE_LOOKS = 3          # misses in a row = reel over
CLOSE_WAIT_S = 0.2
X_SHOWS_S = 2.5          # X shows ~1.9 s after the reel
SEE_CAST_S = 2.0
LOST_RUN = 5            # "got away" streak = catch window unseen
SEE_CATCH_S = 5.0
SEE_SHUT_S = 1.5
TRIES = 3
TALK_WAIT_S = 2.0
SHOP_TRIPS = 3           # then fish without selling
ALIGN_REJOIN = 3
SELL_CLOSE_S = 1.0
SELL_MAX = 100           # each Sell All clears one kind of fish
SLOW_STEP = 0.15         # extra wait when Sell Alls get dropped
SLOW_MAX = 0.6
SALE_LOOKS = 15          # ~4 s for a sale to land
# shop buttons and read areas (points.py boxes)
BUY_TAB = 'fishingShopBuyTab'
BUY_GRID = 'fishingShopBuyPageFrame'
PRODUCT_NAME = 'fishingShopBuyProductName'      # the same spot on the Sell page
SELLS_FOR = 'sellsFor'                          # "Sells for N": under the name, over Sell All
PURCHASE = 'fishingShopBuyPurchaseButton'
HEADER = 'fishingShopBuyConfirmHeader'          # "Confirm Purchase"
SELL_YES = 'fishingShopSellConfirmSellButton'   # Sell Confirm's "Sell" button
AMOUNT_BOX = 'fishingShopBuyConfirmAmountBox'
AMOUNT_TEXT = 'fishingShopBuyConfirmAmountLabel'   # "250 / 1,000" or "[ Sold Out ]"
CONFIRM = 'fishingShopBuyConfirmButton'
PRICE = CONFIRM                                 # the price is written on Confirm
POPUP_X = 'fishingShopBuyConfirmCloseButton'
FISH_POINTS = 'fishingShopCurrencyText'
SLOT = 'fishingShopBuyItem[1,1]'                # one Buy grid item
BUY_WAIT_S = 5.0


def press(key, hold=0.05):
    game.down(key)
    time.sleep(hold)
    game.up(key)


def nap(seconds, halt):
    end = time.perf_counter() + seconds * game.PACE
    while time.perf_counter() < end:
        if halt():
            return False
        time.sleep(0.01)
    return not halt()


def walk(route, halt):
    held, last = set(), 0.0
    try:
        for t, how, key in route:
            end = time.perf_counter() + t - last
            while time.perf_counter() < end:
                if halt():
                    return False
                time.sleep(0.005)
            if how == 'down':
                game.down(key)
                held.add(key)
            else:
                game.up(key)
                held.discard(key)
            last = t
        return not halt()
    finally:
        for key in held:
            game.up(key)


RESPAWNED_S = 3.0           # clicks ignored a bit after respawn


def respawn(halt):
    if game.take_foreground() is None:
        return False
    for key, wait in (('esc', 1.25), ('r', 0.75), ('enter', RESPAWNED_S)):
        press(key)
        if not nap(wait, halt):
            return False
    return True


def pixel(xy):
    return tuple(int(v) for v in game.grab((xy[0], xy[1], xy[0] + 1, xy[1] + 1))[0, 0])


def bad_colour(c):
    """White bite flash, dark backdrop, or the grey fade-in: not a real bar colour."""
    return min(c) >= 245 or max(c) < 100 or max(c) - min(c) < 30


def majority(mask):
    """3x3 majority vote: the see-through bar gets speckled, straight edges survive."""
    h, w = mask.shape
    p = np.pad(mask, 1, mode='edge').astype(np.uint8)
    return sum(p[y:y + h, x:x + w] for y in range(3) for x in range(3)) >= 5


def run_span(rgb, colour):
    """(width, left x) of the longest run of the bar colour on any row, or None if it's not there."""
    diff = np.abs(rgb.astype(int) - np.array(colour))
    run = majority(np.all(diff <= RUN_TOL, axis=2))
    if run.sum() < 6:
        return None
    best = (0, 0)
    for row in run:
        idx = np.flatnonzero(row)
        if idx.size:
            gaps = np.flatnonzero(np.diff(idx) > 1)
            starts, ends = np.r_[0, gaps + 1], np.r_[gaps, idx.size - 1]
            k = int((idx[ends] - idx[starts]).argmax())
            best = max(best, (int(idx[ends[k]] - idx[starts[k]]) + 1, int(idx[starts[k]])))
    return best


def run_width(rgb, colour):
    span = run_span(rgb, colour)
    return None if span is None else span[0]


def left_of(rgb, x):
    """Just left of a run: the fill (bright) or the empty bar (dark)? None at the bar's left end."""
    if x < NEXT_PX + 2:
        return None
    side = rgb[:, x - NEXT_PX - 2:x - 2].reshape(-1, 3).astype(int)
    return 'fill' if side.max(axis=1).mean() >= FILL_MIN else 'empty'


def bar_rows(bar):
    """The bar between its two light outline lines, looked for a few px around the box; the box itself if unseen."""
    pad = max(4, (bar[3] - bar[1]) // 2)
    img = game.grab((bar[0], bar[1] - pad, bar[2], bar[3] + pad))
    if img is None or img.size == 0:
        return bar
    on = np.flatnonzero((img.min(axis=2) >= 110).mean(axis=1) >= 0.9)
    if on.size < 2 or on[-1] - on[0] < 0.6 * (bar[3] - bar[1]) or on[-1] - on[0] > 1.5 * (bar[3] - bar[1]):
        return bar
    top = bar[1] - pad
    return bar[0], top + int(on[0]) + 1, bar[2], top + int(on[-1])


def sample(bar, halt):
    """The zone colour: most common colour in a full-height block where the zone starts, once two looks agree."""
    w = bar[2] - bar[0]
    x0, x1 = bar[0] + w * 186 // 405, bar[0] + w * 206 // 405
    last = None
    for _ in range(60):
        px = game.grab((x0, bar[1], x1, bar[3])).reshape(-1, 3).astype(int)
        hi, lo = px.max(axis=1), px.min(axis=1)
        px = px[(lo < 200) & (hi >= 100) & (hi - lo >= 30)]          # no text, backdrop or grey fade-in
        c = None
        if len(px) >= 20:
            _, which, count = np.unique(px // 12, axis=0, return_inverse=True, return_counts=True)
            c = tuple(int(v) for v in px[which.ravel() == count.argmax()].mean(axis=0))
        if c is not None and last is not None and max(abs(a - b) for a, b in zip(c, last)) <= 8:
            return c
        if halt():
            break
        last = c
        time.sleep(0.02)
    return last


def fast_click():
    game.button('left', True)
    time.sleep(0.01)
    game.button('left', False)


def cast(halt, zoom=ZOOM_OUT_S, full_check=True):
    if game.take_foreground() is None:
        return False
    game.down('o')
    try:
        if not nap(zoom, halt):
            return False
    finally:
        game.up('o')
    points.leave_dialogue(tries=2, skip=True)               # a leftover dialogue covers Fish
    for go in range(1, TRIES + 1):
        if window_state() in ('catch', 'failed'):          # a late catch window covers Fish
            fclick('fishingMinigameResultCloseButton')
        if not fclick('fishingMinigameFishButton', 0.1):
            return False
        st = wait_state(lambda st: st not in ('fish', 'catch', 'failed'), SEE_CAST_S, halt)
        if st not in ('fish', 'catch', 'failed'):
            return not halt()
        if go == 1 and st == 'fish' and full_check and bag_full():
            return 'full'
        print('WARN cast_not_taken attempt=%d/%d' % (go, TRIES))
        fclick('fishingMinigameResultCloseButton')  # a centred window can leave Fish dead unseen
    return not halt()


def bag_full():
    """The red 'Cannot Fish / not enough inventory space' note, bottom right."""
    w = game.roblox_window()
    if w is None:
        return False
    x0, y0, cw, ch = game.client_rect(w)
    img = game.grab((x0 + cw // 2, y0 + ch // 2, x0 + cw, y0 + ch))
    a = img.astype(int)
    red = (a[..., 0] >= 200) & (a[..., 0] > a[..., 1] + 90) & (a[..., 0] > a[..., 2] + 90)
    if red.sum() < 40:
        return False
    ys, xs = np.nonzero(red)
    crop = img[max(0, ys.min() - 10):ys.max() + 10, max(0, xs.min() - 10):xs.max() + 10]
    seen = labels.cannot_fish(crop, cw, ch)             # look first, OCR if unsure
    if seen is not None:
        return seen
    try:
        text = ''.join(merchant.read_lines(crop)).lower().replace(' ', '')
    except Exception:
        return False
    return 'inventory' in text or 'cannotfish' in text


def icon_level():
    icon = fcentre('fishingMinigameIcon')
    return 0 if icon is None else min(pixel(icon))


def cutoff(idle):
    return min(BITE_CAP, max(BITE_WHITE, idle + 30))


def bitten(within, halt, idle=0):
    """True once the icon lights up after being dark since the cast, or the catch bar shows."""
    icon = fcentre('fishingMinigameIcon')
    if icon is None:
        return False
    global peak, dipped
    white = cutoff(idle)
    end = time.perf_counter() + within
    while time.perf_counter() < end and not halt():
        level = min(pixel(icon))
        peak = max(peak, level)
        if level < white:
            dipped = True
        elif dipped:
            return True
        if catch_bar_up(True):      # some sizes only flash the icon
            return True
        time.sleep(BITE_POLL_S)
    return False


peak = 0
dipped = False


def _share(rgb, colour):
    a = rgb.astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    if colour == 'blue':
        hit = (b >= 140) & (b > r + 30) & (b > g + 30)
    elif colour == 'red':
        hit = (r >= 140) & (r > g + 50) & (r > b + 20)
    elif colour == 'text':          # white or any bright colour
        hi, lo = a.max(axis=2), a.min(axis=2)
        hit = (lo >= 225) | ((hi >= 180) & (hi - lo >= 90))
    elif colour == 'dark':
        hit = a.max(axis=2) <= 120
    else:
        hit = a.min(axis=2) >= 225
    return float(hit.mean())


bugged = False      # window sometimes centred, buttons higher


def _shift(on):
    """Vertical offset of the bugged (centred) window, or 0."""
    if not on:
        return 0
    w, root = game.roblox_window(), points.box('fishingMinigamePromptWindowRoot')
    if w is None or root is None:
        return 0
    _, y0, _, ch = game.client_rect(w)
    return round(y0 + ch / 2 - (root[1] + root[3]) / 2)


def fbox(name, on=None):
    b, dy = points.box(name), _shift(bugged if on is None else on)
    return None if b is None else (b[0], b[1] + dy, b[2], b[3] + dy)


def fcentre(name):
    c = points.centre(name)
    dy = 0 if name.startswith('fishingMinigameResult') else _shift(bugged)     # the catch window never moves
    return None if c is None else (c[0], c[1] + dy)


def fclick(name, settle=0.3):
    """Click where the button is now. The catch X also clicks the bugged window's X."""
    if name != 'fishingMinigameResultCloseButton':
        c = fcentre(name)
        if c is None or not game.click(*c, park=False):
            return False
        game.paced(settle)
        return True
    if not points.click(name, 0):
        return False
    root, w = fbox('fishingMinigamePromptWindowRoot', True), game.roblox_window()
    if root is not None and w is not None:
        k = game.client_rect(w)[3] / 1080
        game.click(round(root[2] - 8 * k), round(root[1] + 9 * k), park=False, settle=0)
    game.paced(settle)
    return True


def _button_state(on):
    btn = fbox('fishingMinigameFishButton', on)
    if btn is None:
        return None
    rgb = game.grab(btn)
    blue, red = _share(rgb, 'blue'), _share(rgb, 'red')
    if blue >= 0.02 and red < 0.005:
        return 'fish'
    if red >= 0.02 and blue < 0.005:
        return 'line'
    return None


def _centred_edge():
    """The centred window has a white top edge over a dark body; a blue aura or sea has neither."""
    root = fbox('fishingMinigamePromptWindowRoot', True)
    if root is None:
        return False
    cut = (root[2] - root[0]) // 10
    rows = game.grab((root[0] + cut, root[1] - 4, root[2] - cut, root[1] + 8))
    light = (rows.min(axis=2) >= 120).mean(axis=1)
    return len(light) == 12 and light[:7].max() >= 0.9 and light[7:].max() <= 0.3


def window_state():
    """'catch', 'failed', 'fish', 'line' or None if unsure; the button seen in the centred spot sets `bugged`."""
    global bugged
    head, x = points.box('fishingMinigameResultHeaderLabel'), points.box('fishingMinigameResultCloseIcon')
    shot = game.grab(x) if head and x else None             # X on the dark body,
    if shot is not None and _share(shot, 'white') >= 0.03 and _share(shot, 'dark') >= 0.5:   # not a white sky
        top = game.grab(head)
        if _share(top, 'red') >= 0.05:
            return 'failed'
        if _share(top, 'text') >= 0.03:         # title colour varies by fish
            return 'catch'
    for on in (False, True):                      # normal spot first, odd reads don't stick
        st = _button_state(on)
        if st is not None and (not on or _centred_edge()):
            if on != bugged:
                bugged = on
                print('window_moved centred=%s' % ('true' if on else 'false'))
            return st
    return None


def wait_state(want, seconds, halt):
    """Wait for a state (or a test on one); returns the last seen. None counts only after 3 looks in a row."""
    test = want if callable(want) else (lambda st: st == want)
    end, st, blank = time.perf_counter() + seconds * game.LIMIT, None, 0
    while time.perf_counter() < end and not halt():
        st = window_state()
        blank = blank + 1 if st is None else 0
        if test(st) and (st is not None or blank >= 3):
            return st
        time.sleep(0.1)
    return st


def catch_bar_up(new=False):
    """Catch bar showing (a long light outline line)? None if unknown. `new` also wants it still empty inside."""
    b = fbox('fishingMinigameCatchBarOuter')
    if b is None:
        return None
    pad = max(3, b[3] - b[1])                                # windowed Roblox draws it a few px off
    v = game.grab((b[0], b[1] - pad, b[2], b[3] + pad)).min(axis=2).astype(int)
    on = np.flatnonzero((v >= 120).mean(axis=1) >= 0.9)
    if not on.size:
        return False
    rows = v[on[0]:on[-1] + 1].mean(axis=1)
    return not new or bool(rows.min() < rows.max() - 40)     # darker inside than its lines


def reel(halt):
    """Click while the bar colour is thin or gone, until the catch bar goes. Returns the click count."""
    bar, park = fbox('fishingMinigameBarOuter'), fcentre('fishingMinigameFishButton')
    if bar is None or park is None:
        return 0
    game.glide(*park)
    end = time.perf_counter() + BAR_SHOWS_S
    up = catch_bar_up(True)
    while not up and time.perf_counter() < end and not halt():
        time.sleep(0.02)
        up = catch_bar_up(True)
    if not up and wait_state(lambda st: st is not None, 1.0, halt) in ('line', 'fish'):
        return None                              # no bar: a click would hit Exit
    bar = bar_rows(bar)
    colour = sample(bar, halt)
    bar = bar_rows(bar)
    thin = max(3, round(THIN * (bar[2] - bar[0])))
    start = time.perf_counter()
    clicks, end, seen, gone = 0, start + REEL_S, False, 0
    missing, plain = None, False
    while time.perf_counter() < end and not halt():
        up = catch_bar_up()
        seen = seen or bool(up)
        gone = gone + 1 if seen and up is False else 0
        if gone >= GONE_LOOKS and time.perf_counter() - start >= REEL_MIN_S:
            break                                # don't click the catch window
        if colour is None or bad_colour(colour):
            fast_click()                         # no bar colour: click slow
            clicks += 1
            time.sleep(0.15)
            continue
        img = game.grab(bar)
        span = run_span(img, colour)
        width = None if span is None else span[0]
        missing = None if width is not None else missing or time.perf_counter()
        if missing and time.perf_counter() - missing >= LOST_S:
            fast_click()
            clicks += 1
            time.sleep(0.15)
            continue
        side = None if width is None or width < thin else left_of(img, span[1])     # a stray few px: clicked anyway
        if side == 'empty':
            plain = True                         # a late look took the zone without the fill under it
        elif side == 'fill' and plain:
            c = np.median(img[:, span[1] - NEXT_PX - 2:span[1] - 2].reshape(-1, 3), axis=0)
            if not bad_colour(c):
                colour, plain = tuple(int(v) for v in c), False      # the fill is in the zone now: its colour
        if width is None or width < thin or side == 'empty':
            fast_click()
            clicks += 1
        time.sleep(0.01)
    return clicks


def close_catch(halt):
    """Shut the catch window once its X shows: 'caught', 'lost', 'failed', or False if stopped."""
    if not nap(CLOSE_WAIT_S, halt):
        return False
    x = points.centre('fishingMinigameResultCloseButton')
    if x is not None:
        game.glide(*x)                            # hover the X, click as it shows
    start, st = time.perf_counter(), None
    while not halt():
        st = window_state()                       # only 'catch' once the X is drawn
        waited = time.perf_counter() - start
        if st in ('catch', 'failed') or waited > SEE_CATCH_S * game.LIMIT:
            break                                 # slow PCs draw it late
        if st is None and waited > X_SHOWS_S * game.LIMIT:
            break                                 # unreadable: assume the X is up
        time.sleep(0.05)
    if halt():
        return False
    if st in ('fish', 'line'):
        return 'lost'
    seen = x is not None and st in ('catch', 'failed')
    if seen and not game.click(*x, park=False, settle=0.05, travel=0):
        return False
    if st == 'failed':
        return (seen or fclick('fishingMinigameResultCloseButton', 0.1)) and not halt() and 'failed'
    if seen:
        return not halt() and 'caught'          # still up: the cast shuts it
    for go in range(1, TRIES + 1):
        if not fclick('fishingMinigameResultCloseButton', 0.1):
            return False
        if wait_state(lambda st: st != 'catch', SEE_SHUT_S, halt) != 'catch':
            return not halt() and 'caught'
        print('WARN catch_window_still_up attempt=%d/%d' % (go, TRIES))
    return not halt() and 'caught'


def talked(halt):
    """Wait for Flarg's choices to show, pressing E once more if not. False if he never answers."""
    for go in range(2):
        if go:
            press('e')
        for _ in range(2):
            if halt():
                return False
            points.click('dialogueSkipDialogue', 0.05)
        end = time.perf_counter() + TALK_WAIT_S * game.LIMIT
        while time.perf_counter() < end:
            if halt():
                return False
            if points.choice('green') or points.choice('red'):
                return True
            time.sleep(0.1)
    return False


def sells_for():
    """N from "Sells for N" (0 with nothing picked), or None unread."""
    t = _text(SELLS_FOR)
    nums = re.findall(r'\d[\d,.]*', t)
    return int(re.sub(r'\D', '', nums[-1])) if nums and game.fuzzy_has(t, 'sells for') else None


def to_sell(halt, sold=False):
    """The first slot clicked: fish there if it sells for more than 0 (unread: if the panel names one). An empty
    bag shows 0 every try, as a click right after a sale can be ignored (or lost). None on Stop or a failed click."""
    for _ in range(SALE_LOOKS if sold else 0):              # the sold fish stays picked till the sale is done
        if not sells_for():
            break
        if not nap(0.2 + slow, halt):
            return None
    for _ in range(TRIES):
        if halt() or not points.click('fishingShopSellItem[1,1]', 0.3 + slow):
            return None
        n = sells_for()
        if n or (n is None and re.search('[a-z0-9]', _text(PRODUCT_NAME))):     # a name read as "00" still counts
            return True
    return False


slow = 0.0


def slower(say):
    global slow
    if slow < SLOW_MAX:
        slow = round(min(SLOW_MAX, slow + SLOW_STEP), 2)
        say('WARN sell_slowed extra_s=%.2f' % slow)


def answer(halt, say):
    """Press Sell again while the Sell Confirm window is still up. False only on Stop or a failed click."""
    for _ in range(3):
        if 'sell' not in _text(SELL_YES):
            break
        say('WARN sell_confirm_again')
        slower(say)
        if not points.click('fishingShopSellConfirmSellButton', 0.1 + slow) or not nap(SELL_CLOSE_S + slow, halt):
            return False
    return True


def confirm_up(halt):
    """Sell Confirm's Sell button on screen, given a moment to draw."""
    for _ in range(10):
        if 'sell' in _text(SELL_YES):
            return True
        if not nap(0.2 + slow, halt):
            return False
    return False


def sell_all(halt, say=print):
    """True once sold, None when Sell Confirm never shows (stop selling), False on Stop or a failed click."""
    for _ in range(2):                                      # no Sell Confirm: Sell All once more
        for name, settle in (('fishingShopSellTab', 0.1), ('fishingShopSellItem[1,1]', 0.1),
                             ('fishingShopSellAllButton', 0.3 + slow)):
            if halt() or not points.click(name, settle):
                return False
        if confirm_up(halt):
            break
        if halt():
            return False
    else:
        say('WARN sell_confirm_missing tries=2 next=stop_selling')
        return None
    if halt() or not points.click(SELL_YES, 0.1 + slow):
        return False
    return nap(SELL_CLOSE_S + slow, halt) and answer(halt, say)


def sell(halt, times=1, say=print, every=True):
    """Sells the fish at Captain Flarg, a Sell All per kind till the bag is empty (Sell all off: `times` at most).
    Returns the Sell Alls done, or False."""
    if not points.click('dialogueChoice[1 of 1]', 0.4):      # middle choice is Sell Fish
        return False
    if points.leave_dialogue() and not nap(0.3, halt):     # dialogue covers the shop until Leave
        return False
    if _until(lambda: _points() is not None, 3.0, halt) is None:     # still drawing: an empty-looking bag
        return False
    if halt() or not points.click('fishingShopSellTab', 0.1):
        return False
    done = 0
    while done < (SELL_MAX if every else max(1, times)):
        left = to_sell(halt, done > 0)
        if left is None:
            return False
        if not left:
            break
        sold = sell_all(halt, say)
        if sold is None:
            break
        if not sold:
            return False
        done += 1
    answer(halt, say)
    for _ in range(3):                                      # a lost Close leaves the shop over Fish
        closed = points.click('fishingShopCloseButton', 0.2)
        if not closed or not nap(0.4, halt):
            return done if closed else False
        if _points() is None:
            break
        say('WARN shop_close_again')
    points.leave_dialogue(skip=True)                         # dialogue outlives the shop
    return done


def _tap(name, settle=0.3):
    return points.click(name, settle)


def _box(area):
    if area != SELLS_FOR:
        return points.box(area)
    a, b = points.box(PRODUCT_NAME), points.box('fishingShopSellAllButton')
    return None if a is None or b is None else (a[0], a[3], b[2], b[1])


def _lines(area):
    """OCR of part of the shop as (squashed text, x, y) on screen."""
    box = _box(area)
    if box is None:
        return []
    l, t, r, b = box
    img = game.grab(box)
    try:
        result, _ = merchant.reader()(np.ascontiguousarray(img[..., ::-1]), use_cls=False)
    except Exception:
        return []
    return [(re.sub(r'[^a-z0-9/.,]', '', q[1].lower()), l + (q[0][0][0] + q[0][2][0]) / 2,
             t + (q[0][0][1] + q[0][2][1]) / 2) for q in result or []]


def _text(area):
    return ''.join(q[0] for q in _lines(area))


def _like(seen, want):
    return want in seen or difflib.SequenceMatcher(None, seen, want).ratio() >= 0.8


def _points(area=FISH_POINTS):
    """Fish points from the shop's top bar (or a price), or None."""
    m = re.search(r'(\d[\d,.]*)([kmb]?)', _text(area))
    try:
        return float(m.group(1).replace(',', '')) * {'': 1, 'k': 1e3, 'm': 1e6, 'b': 1e9}[m.group(2)]
    except (AttributeError, ValueError):
        return None


def _steady_points(halt):
    """Fish points read the same twice in a row (a digit is sometimes lost on one read), or None."""
    last = None
    for _ in range(4):
        p = _points()
        if p is not None and p == last:
            return p
        last = p
        if not nap(0.15, halt):
            return None
    return None


def _until(seen, seconds, halt):
    end = time.perf_counter() + seconds * game.LIMIT
    while time.perf_counter() < end:
        if seen():
            return True
        if not nap(0.2, halt):
            return None
    return seen()


def _popup():
    """Is the Confirm Purchase window up? The screen reader often reads its title as "Confirm Purchose"."""
    return 'confirm' in _text(HEADER)


def _shut(halt, popup=False):
    """Shut the Confirm Purchase window (if asked or seen), the shop, then Flarg's dialogue."""
    if popup or _popup():
        _tap(POPUP_X, 0.3)
    closed = points.click('fishingShopCloseButton', 0.2)
    if nap(0.4, halt):
        points.leave_dialogue(skip=True)
    return closed


def buy(halt, item='Lucky Potion', amount=250, say=print):
    """Buy `amount` of `item` at Flarg, his choices showing: how many, 0 if sold out or unaffordable, None if
    pressed but the fish points didn't go down, or False."""
    want = re.sub(r'[^a-z0-9]', '', item.lower())
    if not points.click('dialogueChoice[1 of 1]', 0.4):
        return False
    if points.leave_dialogue() and not nap(0.3, halt):
        return False
    if _until(lambda: _points() is not None, 3.0, halt) is None:     # shop drawn, or the Buy click is lost
        return False
    grid, hits = [], []
    for go in range(3):                                     # retry a Buy click that came too early
        if halt() or not _tap(BUY_TAB, 0.5):
            return False
        if _popup():                                        # left up earlier, covers the grid
            _tap(POPUP_X, 0.3)
        grid = _lines(BUY_GRID)
        hits = [q for q in grid if _like(q[0], want)]
        if hits:
            break
        say('buy_tab_retry go=%d' % (go + 1))
    if not hits:
        say('WARN buy_item_missing item="%s" read=[%s]' % (item, ','.join(q[0] for q in grid)))
        _shut(halt)
        return False
    _, x, y = hits[0]
    w, h = (lambda b: (b[2] - b[0], b[3] - b[1]) if b else (0, 0))(points.box(SLOT))
    if any('soldout' in q[0] and abs(q[1] - x) < w / 2 and -h / 2 < q[2] - y < h / 4
           for q in grid):
        say('buy_sold_out item="%s" at=grid' % item)
        return 0 if _shut(halt) else False
    if halt() or not game.click(int(x), int(y), park=False):
        return False
    game.paced(0.4)
    if not _like(_text(PRODUCT_NAME), want):
        say('WARN buy_pick_failed item="%s"' % item)
        _shut(halt)
        return False
    for go in range(2):                                     # retry a lost click
        if halt() or not _tap(PURCHASE, 0.5):
            return False
        opened = _until(_popup, 1.5, halt)
        if opened is None:
            return False
        if opened:
            break
    else:
        say('WARN buy_no_confirm item="%s" maybe=sold_out' % item)
        return 0 if _shut(halt) else False
    if 'soldout' in _text(AMOUNT_TEXT):
        say('buy_sold_out item="%s" at=confirm' % item)
        return 0 if _shut(halt, True) else False
    for go in range(2):                                     # retry a lost click or key
        if halt() or not _tap(AMOUNT_BOX, 0.2):
            return False
        game.type_term(str(int(amount)))
        _tap(HEADER, 0.3)                                   # click off the box so it takes the amount
        m = re.match(r'(\d[\d,]*)/(\d[\d,]*)?', _text(AMOUNT_TEXT))       # "250 / 1,000": typed / most
        n, most = [int(g.replace(',', '')) if g else None for g in m.groups()] if m else (0, None)
        if most is not None and n > most:                   # "2501/1,000": the last digit
            n //= 10                                        # is the misread /
        if n == amount or most is not None and n == min(amount, most):
            break
    if n < 1:
        say('WARN buy_amount_unread item="%s"' % item)
        _shut(halt, True)
        return False
    if n != amount and (most is None or n != min(amount, most)):
        say('WARN buy_amount_wrong shown=%d want=%d' % (n, amount))
        _shut(halt, True)
        return False
    if n != amount:
        say('WARN buy_amount_capped allowed=%d want=%d' % (n, amount))
    before, cost = _steady_points(halt), _points(PRICE)
    if cost is None or cost % n:
        cost = None
    need = cost or n                                        # every item costs 1+ points each
    if before is not None and need > before:
        say('WARN buy_no_points n=%d item="%s" cost=%d points=%d' % (n, item, need, before))
        return 0 if _shut(halt, True) else False            # pressing it now leaves the button dead
    if halt() or not _tap(CONFIRM, 0.3):
        return False
    stop = lambda: game.ABORT                               # pressed: wait for the answer, or the buy repeats
    # ponytail: half the price, the points bar rounds big numbers (12.3k)
    paid = lambda: before is not None and before - (_points() or before) >= need / 2
    if _until(lambda: paid() or not _popup(), BUY_WAIT_S, stop) is None:
        return False
    done = _until(paid, 1.5, stop) if before is not None else not _popup()     # points redraw a bit after
    if done is None or stop():
        return False
    after = _steady_points(stop) if not done else _points()
    if not done and before is not None and (after is None or after > before + need / 2):   # can't go up: misread
        say('WARN buy_points_unsure item="%s" points_before=%d points_after=%s' % (
            item, before, '?' if after is None else '%d' % after))
        done = True
    if not done:                                            # None: the points didn't go down, not bought
        say('WARN buy_unconfirmed item="%s" points_before=%s points_after=%s' % (
            item, '?' if before is None else '%d' % before, '?' if after is None else '%d' % after))
        _shut(halt)
        return None
    say('bought n=%d item="%s" points_before=%s points_after=%s' % (
        n, item, '?' if before is None else '%d' % before, '?' if after is None else '%d' % after))
    _shut(halt)
    return n


class Fisher:
    """One fishing session: get to the spot, catch, and step through a failsafe when nothing bites."""

    def __init__(self, vip=True, sell_every=0, sell_amount=10, sell_all=True, full_check=True, say=print, report=None):
        self.shop_route, self.spot_route = (TO_SHOP, TO_SPOT) if vip else (TO_SHOP_FREE, TO_SPOT_FREE)
        self.sell_every = max(0, int(sell_every or 0))
        self.sell_amount = max(1, int(sell_amount or 1))
        self.sell_all = sell_all
        self.full_check = full_check
        global slow, bugged
        slow, bugged = 0.0, False
        self.say = say
        self.report = report        # failsafe steps go to the webhook
        self.at_spot = False
        self.cast_at = None
        self.idle = 0
        self.stage = 0
        self.caught = self.since_sell = 0
        self.lost_run = 0
        self.zoomed = False
        self.hold = False           # merchant: land the fish, then stop casting
        self.pause = False          # SC/BR: land the fish, then wait uncast
        self.line_out = False
        self.recast = False         # screen used elsewhere: recast
        self.dry = 0
        self.after_sell = False
        self.rejoin = False
        self.to_buy = None          # (item, amount)
        self.bought = None
        self.cut = lambda: ''       # why to stop the walk ('' = keep walking)
        self.unbarred = None        # cast already logged as lit with no bar

    def cast(self, halt, zoom=None):
        global peak, dipped
        self.recast = False         # a second cast would hit Exit and cancel this one
        if zoom is None:
            zoom = ZOOM_TOPUP_S if self.zoomed else ZOOM_OUT_S
        done = cast(halt, zoom, self.full_check)
        if done == 'full':
            return self.full_bag()
        self.after_sell = False
        ok = done and nap(0.5, halt)
        self.zoomed = self.zoomed or bool(ok)
        self.line_out = bool(ok)
        self.cast_at, self.idle, peak, dipped = time.monotonic(), icon_level(), 0, False
        return ok

    def full_bag(self):
        """Sell, or rejoin if it's still full on the first cast after selling."""
        self.line_out = self.at_spot = False
        if self.after_sell:
            self.rejoin = 'inventory still full right after selling'
            self.failsafe('%s: rejoining' % self.rejoin)
        else:
            self.failsafe('inventory full: selling')
        self.since_sell = 0
        return True

    def failsafe(self, what):
        self.say('WARN failsafe msg="%s"' % what)
        if self.report:
            self.report(what[:1].upper() + what[1:])
        points.click('fishingShopCloseButton', 0.2)          # a leftover shop covers all
        for name, shut in (('Memory Match', memmatch.shut_board), ('Daily Rewards', game.shut_daily)):
            try:                                            # so do these, left up
                if shut():
                    self.say('WARN left_open_closed what="%s"' % name)
            except Exception:
                pass

    def lost(self):
        """We got moved (rejoin, merchant teleport): walk back before casting."""
        self.at_spot = self.line_out = self.zoomed = False
        self.hold = False

    def reel_in(self, halt):
        """Press Exit, only if it's showing (the same spot is Fish otherwise)."""
        if window_state() == 'line':
            fclick('fishingMinigameFishButton', 0.2)
        self.line_out = False
        return not halt()

    def to_spot(self, halt, selling=True):
        """Spawn -> Flarg (buy what's asked for, then sell unless `selling` is False) -> fishing spot, then cast."""
        self.say('walk_to_spot flarg=%s' % ('sell' if selling else 'buy'))
        self.zoomed = False                     # respawn resets the camera
        for trip in range(1, SHOP_TRIPS + 1):
            if not respawn(halt) or not game.align_camera(halt):
                if game.align_fails >= ALIGN_REJOIN and not halt():
                    self.rejoin = 'Collection would not open or shut %d times in a row' % game.align_fails
                    self.failsafe('%s: rejoining' % self.rejoin)
                return False
            if not walk(self.shop_route, lambda: halt() or bool(self.cut())) or not nap(0.3, halt):
                if self.cut() and not halt():
                    self.say('walk_cut why="%s"' % self.cut())
                return False
            if talked(halt):
                if self.to_buy:
                    if not self.buy_now(halt):
                        return False
                    if selling:
                        press('e')
                        if not nap(0.3, halt):
                            return False
                        if not talked(halt):
                            self.say('WARN flarg_no_choices for=sell retry=next_trip')
                            selling = False
                if selling:
                    sold = sell(halt, self.sell_amount, self.say, self.sell_all)
                    if sold is not False:          # 0 = bag was already empty
                        self.say('sell_done sell_alls=%d caught_since=%d' % (sold, self.since_sell))
                        self.since_sell = 0
                    elif halt():
                        return False
                    else:
                        self.say('WARN sell_failed retry_after=%d' % self.sell_every if self.sell_every
                                 else 'WARN sell_failed')
                        self.since_sell = 0
                break
            if halt():
                return False
            self.say('WARN flarg_no_dialogue attempt=%d/%d next=%s' % (
                trip, SHOP_TRIPS, 'walk_again' if trip < SHOP_TRIPS else 'fish_without_selling'))
            if trip == SHOP_TRIPS and selling:
                self.since_sell = 0             # retry after the next batch, not every fish
        if not nap(0.2, halt) or not walk(self.spot_route, halt):
            return False
        self.at_spot = self.after_sell = True
        self.nudged = False
        if not self.cast(halt):
            self.at_spot = False
            return False
        return True

    def buy_here(self, halt):
        """Talk to Flarg again (we're beside him) and buy. False only on Stop."""
        press('e')
        if not nap(0.3, halt) or not talked(halt):
            self.say('WARN flarg_no_choices for=buy retry=next_trip')
            return not halt()
        return self.buy_now(halt)

    def buy_now(self, halt):
        """Buy what's asked for, his choices showing. False only on Stop."""
        item, amount = self.to_buy
        got = buy(halt, item, amount, self.say)
        if got is None and not halt():                      # points didn't go down: once more
            press('e')
            got = buy(halt, item, amount, self.say) if nap(0.3, halt) and talked(halt) else None
        if halt():
            return False
        self.to_buy = None              # one go per ask, bought or not
        if got is None:
            got, self.rejoin = False, 'fish points did not go down after buying %s twice' % item
            self.failsafe('%s: rejoining' % self.rejoin)
        self.bought = got
        if got is False:
            self.say('ERROR buy_failed item="%s"' % item)
        return not self.rejoin

    def buy_trip(self, halt, item='Lucky Potion', amount=250):
        """Reset, walk to Flarg, buy (no selling, the fish count carries on), back to the spot and cast."""
        self.to_buy = (item, amount)
        self.at_spot = self.line_out = False
        try:
            return self.to_spot(halt, selling=False)
        finally:
            self.to_buy = None

    def foiled(self, halt, how):
        self.say('WARN catch_failed %s' % how)
        self.line_out = False
        return True if self.pause or self.hold else self.cast(halt)

    def step(self, halt, wait=8.0):
        """Wait up to `wait` s for a bite and land it. False if stopped."""
        if self.rejoin:                 # the app rejoins first
            return not halt()
        if not self.at_spot:
            return self.to_spot(halt)
        if (self.pause or self.hold) and not self.line_out:
            return not halt()
        if self.recast:
            self.recast = False
            fclick('fishingMinigameResultCloseButton', 0.1)
            return self.cast(halt)
        if bitten(wait, halt, self.idle):
            clicks = reel(halt)
            if clicks is None:
                if time.monotonic() - self.cast_at < NO_BITE_S:
                    if self.cast_at != self.unbarred:       # once per cast, not every look
                        self.unbarred = self.cast_at
                        self.say('WARN bite_no_bar peak=%d needs=%d' % (peak, cutoff(self.idle)))
                    return not halt()
                self.say('WARN bite_no_bar_timeout wait_s=%d' % NO_BITE_S)
                return self.no_bite(halt)
            landed = close_catch(halt)
            if halt() or not landed:
                return False
            self.stage = self.dry = 0
            if landed == 'failed':
                return self.foiled(halt, 'reel_clicks=%d' % clicks)
            self.lost_run = self.lost_run + 1 if landed == 'lost' else 0
            if landed == 'lost' and self.lost_run >= LOST_RUN:
                if self.lost_run == LOST_RUN:
                    self.say('WARN catch_window_unseen lost_run=%d reels_count_as_catch=true' % LOST_RUN)
                fclick('fishingMinigameResultCloseButton', 0.1)
                landed = 'caught'
            if landed == 'lost' and window_state() == 'line':
                self.say('WARN catch_window_missing reel_clicks=%d line_out=true' % clicks)
                return True                         # casting now would press Exit
            if landed == 'lost':
                self.say('WARN catch_lost reel_clicks=%d' % clicks)
                self.line_out = False
                return True if self.pause or self.hold else self.cast(halt)
            self.caught += 1
            self.since_sell += 1
            self.say('catch n=%d reel_clicks=%d%s' % (
                self.caught, clicks, ' bag=%d/%d' % (self.since_sell, self.sell_every) if self.sell_every else ''))
            if self.hold or (self.sell_every and self.since_sell >= self.sell_every):
                self.at_spot = self.line_out = False
                return True
            if self.pause:
                self.line_out = False
                self.say('cast_paused until=sc_br')
                return True
            return self.cast(halt)
        if halt():
            return False
        st = window_state()
        if st == 'failed':
            fclick('fishingMinigameResultCloseButton', 0.1)
            return self.foiled(halt, 'when=before_reel')
        if st == 'catch':                       # catch window came late
            fclick('fishingMinigameResultCloseButton', 0.1)
            self.caught += 1
            self.since_sell += 1
            self.say('catch n=%d late=true' % self.caught)
            self.line_out = False
            return True if self.pause or self.hold else self.cast(halt)
        if st == 'fish' and nap(0.3, halt) and window_state() == 'fish':
            if self.pause or self.hold:
                self.line_out = False
                return True
            self.dry += 1
            if self.dry > DRY_CASTS:
                self.dry = 0
                return self.full_bag()
            self.say('WARN line_not_out dry=%d/%d' % (self.dry, DRY_CASTS))
            return self.cast(halt)
        if st is None and not self.line_out and self.no_menu(halt):     # line out: wait for the fish, no step or reset
            if getattr(self, 'nudged', False):          # once per walk: the second time, walk again
                self.say('WARN fish_menu_missing then=walk_to_spot')
                self.at_spot = self.line_out = False
                return not halt()
            self.nudged = True
            self.say('WARN fish_menu_missing then=w_200ms')
            press('w', 0.2)                             # nudged off the pad: a step brings the menu back
            if not nap(0.5, halt):
                return False
            if window_state() == 'fish':
                return self.cast(halt)
            return not halt()
        if time.monotonic() - self.cast_at < NO_BITE_S:
            return not halt()
        return self.no_bite(halt)

    def no_menu(self, halt):
        """No fishing window at all on three looks in a row (a moment's blank is just a redraw)."""
        for _ in range(3):
            if window_state() is not None or not nap(0.15, halt):
                return False
        return True

    def no_bite(self, halt):
        """Nothing bit for NO_BITE_S: recast, walk back to the spot or sell, in turn."""
        if self.pause or self.hold:
            self.say('WARN no_bite wait_s=%d waiting_for=%s then=reel_in' % (NO_BITE_S, 'sc_br' if self.pause else 'merchant'))
            return self.reel_in(halt)
        self.stage = (self.stage + 1) % 4
        self.say('no_bite_icon stage=%d idle=%d peak=%d needs=%d' % (self.stage, self.idle, peak, cutoff(self.idle)))
        self.failsafe({1: 'no bite in %d s: casting again' % NO_BITE_S, 3: 'no bite in %d s: casting again' % NO_BITE_S,
                       2: 'still no bite: walking back to the spot',
                       0: 'still no bite: a full bag looks like this - selling'}[self.stage])
        fclick('fishingMinigameResultCloseButton', 0.1)
        if self.stage in (1, 3):
            if not nap(1.0, halt):
                return False
            return self.cast(halt, 0.0 if self.zoomed else None)
        self.at_spot = False
        return True
