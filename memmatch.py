"""Memory Match at its stand: open it with E, play every pair from memory, or read how long until it's back."""
import difflib
import os
import re
import sys
import time

import cv2
import numpy as np
from PIL import Image

import crafter
import game
import selector

OPEN_S = 4.0             # for the window after E
START_WAIT_S = 2.0       # Start can't be pressed for 1 s after the window shows: 1 s spare on any PC speed
START_S = 10.0           # Start -> every card face down
STILL_S = 1.0            # backs held this long = the curtain has slid away
FLIP_S = 3.0             # a clicked card starts turning by then, else the click was lost
SETTLE_S = 2.5           # a turning card holds still by then
SHOWN_S = 0.6            # its turn-over takes 0.6 s; a face up that long was whole
TURN_S = 8.0             # a wrong pair turns back, or a right one goes green, by then
END_S = 10.0             # the rest turn over and Close shows by then
GAME_S = 300.0           # a whole game; a good one takes about 90 s
TRIES = 3
FLIPS = 5                # clicks at a card before leaving it
STEP = 0.15
CARDS = 20
CHANCES = 10             # the game's chances; past this many pairs turned the board may have ended
OVER_S = 2.0             # ... every card turns face up by then if it did
last_shot = None         # PNG of the end screen (what was won) from the last game
SIZE = 64                # cards are compared shrunk to this, then softened
INK = 40                 # a pixel changed if a colour moved more than this
BACK = 0.03              # share of changed pixels under which a card still shows its back
SAME = 0.015             # ... two faces are the same item and amount (picture and words checked apart)
HELD = 0.01              # ... a card held still between two looks
PICTURE = 0.1            # ... one reward's picture at another screen size (about 0.05; others 0.2 and up)
TINT = 10                # middle colour: one item at any size within 6, the nearest other potion 16+
GREEN = 50               # green this far over red and blue = the pair matched
LABEL = 0.79             # the amount sits below this share of the card
WAIT = re.compile(r'(\d+)\s*([hms])')
UNIT = {'h': 3600, 'm': 60, 's': 1}


def _fit(w, h, ratio):
    return min(w, h * ratio), min(h, w / ratio)


def layout():
    """Boxes (l, t, r, b) of the cards, Close and Start, and the warning's X, from the client size."""
    l, t, r, b = game.client_box((0.0, 0.0, 1.0, 1.0))
    W, H = r - l, b - t
    o = {}
    mw, mh = _fit(0.5 * W, 0.5 * H, 1.4)                    # the board, centred
    mx, my = l + (W - mw) / 2, t + (H - mh) / 2
    gx, gy, gw, gh = mx + 0.245 * mw, my + 0.09 * mh, 0.75 * mw, 0.9 * mh     # its cards, 5 across and 4 down
    cw, ch = 0.199 * gw, 0.25 * gh
    w, h = round(0.76 * cw), round(0.76 * ch)              # inside each card's edge, amount included; one size
    o['cards'] = [(x, y, x + w, y + h) for x, y in ((round(gx + (c + 0.12) * cw), round(gy + (k + 0.12) * ch))
                                                     for c in range(5) for k in range(4))]
    o['close'] = tuple(round(v) for v in (mx + 0.35 * mw, my + 1.02 * mh, mx + 0.65 * mw, my + 1.09 * mh))
    ww, wh = _fit(0.3 * W, 0.3 * H, 3.0)                    # the "Start Memory Match?" window, centred
    wx, wy = l + (W - ww) / 2, t + (H - wh) / 2
    o['start'] = tuple(round(v) for v in (wx + 0.02 * ww, wy + 0.8 * wh, wx + 0.98 * ww, wy + 0.95 * wh))
    o['shut'] = (round(wx + 0.994 * ww - 0.06 * wh), round(wy + 0.06 * wh))
    return o


def mid(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def wait_left(text):
    """Seconds in 'Available after 05h 12m Left', or None. OCR reads 0 as O in this font."""
    text = re.sub(r'(?<=\d)o|o(?=\d)', '0', text.lower())
    return sum(int(n) * UNIT[u] for n, u in WAIT.findall(text)) or None


def _like(text, words):
    return difflib.SequenceMatcher(None, text, words).ratio() >= 0.7


def screen():
    """'ready', seconds left, 'wait' (time unread), or None when the window isn't up."""
    text = crafter._text(layout()['start'])
    s = selector.squash(text)
    if _like(s, 'startmemorymatch'):
        return 'ready'
    if _like(s[:14], 'availableafter') and _like(s[-5:], 'ready'):   # timer ran out: still playable
        return 'ready'
    if _like(s[:14], 'availableafter') or s.endswith('left'):
        return wait_left(text) or 'wait'
    return None


def _small(img):
    img = cv2.resize(np.ascontiguousarray(img), (SIZE, SIZE), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(img, (5, 5), 0)                         # cards sit at fractions of a pixel


def _moved(a, b):
    """Share of pixels where some colour moved more than INK."""
    d = cv2.absdiff(a, b)
    return np.count_nonzero(np.maximum(np.maximum(d[..., 0], d[..., 1]), d[..., 2]) > INK) / (d.shape[0] * d.shape[1])


def _off(a, b):
    """Like _moved, best of a pixel's slide either way (cards sit at fractions of a pixel)."""
    h, w = a.shape[:2]
    return min(_moved(a[max(0, y):h + min(0, y), max(0, x):w + min(0, x)],
                      b[max(0, -y):h + min(0, -y), max(0, -x):w + min(0, -x)]) for y in (-1, 0, 1) for x in (-1, 0, 1))


def _near(a, b, limit):
    rough = _moved(a, b)
    return rough < limit or (rough < 5 * limit and _off(a, b) < limit)


def _same(a, b):
    if cv2.absdiff(a[4::8, 4::8], b[4::8, 4::8]).mean() > 20:      # plainly different: skip the close look
        return False
    cut = int(SIZE * LABEL)
    return _off(a[:cut], b[:cut]) < SAME and _off(a[cut:], b[cut:]) < SAME


def _same_picture(a, b):
    """Same reward picture, whatever the amount; the top band is left out (the matched cover's corners)."""
    top, cut = SIZE // 10, int(SIZE * LABEL)
    a, b = a[top:cut], b[top:cut]
    return cv2.absdiff(a[4::8, 4::8], b[4::8, 4::8]).mean() <= 40 and _off(a, b) < PICTURE


def _tint(raw):
    """Middle colour of a card, unblurred: tells apart potions that only differ in tint."""
    s = cv2.resize(np.ascontiguousarray(raw), (50, 50), interpolation=cv2.INTER_AREA)
    px = s[21:31, 19:31].reshape(-1, 3).astype(float)
    mid = np.median(px, axis=0)
    if (np.abs(px - mid).max(1) > 30).mean() < 0.1:              # potions: one even colour
        return mid
    bright = px.sum(1)                                           # an edge runs through the middle, so it moves
    return np.stack([mid, px[bright >= np.percentile(bright, 75)].mean(axis=0)])   # with the card: also its brightest quarter


def _same_tint(a, b, most=TINT):
    if a is None or b is None:
        return True
    rows = 2 if a.ndim == b.ndim == 2 else 1                         # brightest quarters only when both have edges
    return float(np.abs(a.reshape(-1, 3)[:rows] - b.reshape(-1, 3)[:rows]).max(axis=-1).min()) <= most


def amount(raw):
    """The 'x200' under a card, read; None if unreadable. Small cards need the black edge and the blow-up."""
    h = raw.shape[0]
    for tall, top in ((40, 0.74), (72, 0.76)):
        s = np.pad(np.ascontiguousarray(raw[int(top * h):]), ((8, 8), (8, 8), (0, 0)))
        k = tall / s.shape[0]
        img = np.asarray(Image.fromarray(s).resize((max(1, int(s.shape[1] * k)), tall), Image.LANCZOS))
        try:
            text = ' '.join(t for t, _, _ in crafter._ocr(img))
        except Exception:
            return None
        m = re.search(r'\d[\d,]*', text.translate(str.maketrans('oOlI', '0011')))
        if m:
            return int(m.group().replace(',', ''))
    return None


def _same_amount(a, b):
    return a is None or b is None or a == b


def _kind(look):
    """The known reward whose picture (colour left out) a card is closest to, or None if none is close."""
    top, cut = SIZE // 10, int(SIZE * LABEL)
    best, score = None, PICTURE
    for n in rewards():
        k = look_of(n)
        if k is not None:
            d = _off(look[top:cut], k[0][top:cut])
            if d < score:
                best, score = n, d
    return best


def _apart(a, b):
    """(look, tint) pairs whose closest known rewards are clearly different pictures."""
    ka, kb = _kind(a[0]), _kind(b[0])
    if ka is None or kb is None or ka == kb:
        return False
    top, cut = SIZE // 10, int(SIZE * LABEL)
    return _off(look_of(ka)[0][top:cut], look_of(kb)[0][top:cut]) > PICTURE / 2


def _same_item(a, b):
    """(look, tint) pairs: same reward picture and colour, whatever the amount."""
    return _same_picture(a[0], b[0]) and _same_tint(a[1], b[1])


ITEMS = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'memory_items')  # known
_looks = {}


def _pngs(folder):
    try:
        return [n for n in os.listdir(folder) if n.endswith('.png')]
    except OSError:
        return []


def rewards():
    """Picture file names of the known rewards, sorted."""
    return sorted(_pngs(ITEMS))


def path_of(name):
    return os.path.join(ITEMS, name)


def look_of(name):
    """(look, tint) of a reward picture, or None."""
    if name not in _looks:
        try:
            raw = np.asarray(Image.open(path_of(name)).convert('RGB'))
            _looks[name] = _small(raw), _tint(raw)
        except (OSError, ValueError):
            _looks[name] = None
    return _looks[name]


def face_of(raw, face):
    """(look, tint) of a face up card just grabbed, or None if it's mid-turn, green or changed since `face`."""
    look = _small(raw)
    if _green(look) or not _same_picture(look, face):                # turned green or moved since it was read
        return None
    if (look.min(axis=2) > 230).mean() > 0.5:                       # the white flash of a turn
        return None
    return look, _tint(raw)


def _green(look):
    """The matched cover: green over the card's top corners, where no picture reaches."""
    k = SIZE // 10
    c = np.concatenate([look[:k, :k], look[:k, -k:]]).reshape(-1, 3).mean(axis=0)
    return c[1] - max(c[0], c[2]) > GREEN


def _red(img):
    r, g, b = (img[..., k].astype(int) for k in range(3))
    return float(((r > 180) & (g < 120) & (b < 120)).mean()) > 0.02


def shut_board():
    """Press Close on a Memory Match board left up (red, and it reads Close). True if there was one."""
    box = layout()['close']
    img = game.grab(box)
    if img is None or img.size == 0 or not _red(img):
        return False
    if 'close' not in ' '.join(t for t, _, _ in crafter._ocr(img)).lower():
        return False
    for _ in range(3):
        game.click(*mid(box), park=False)
        game.paced(0.5)
        if not _red(game.grab(box)):
            break
    return True


class Board:
    """One game, played from memory."""

    def __init__(self, halt, say, skip=()):
        self.halt, self.say = halt, say
        self.skip = [look_of(n) for n in skip if look_of(n) is not None]     # rewards not worth a chance
        o = layout()
        self.boxes, self.close = o['cards'], o['close']
        self.back = None
        self.faces = {}          # card -> its face, for cards seen and turned back
        self.done = set()        # matched cards
        self.apart = set()       # pairs that looked alike but didn't match
        self.dead = set()        # cards that wouldn't turn; left alone
        self.memo = {}           # (i, j) -> faces alike
        self.retried = False
        self.turns = 0           # pairs turned; each one uses a chance
        self.tints = {}          # card -> its middle colour at full size
        self.amounts = {}        # card -> its amount, read

    def look(self, i):
        return _small(game.grab(self.boxes[i]))

    def down(self, look):
        return _near(look, self.back, BACK)

    def wait(self, s=STEP):
        game.paced(s)
        return not self.halt()

    def ready(self):
        """Wait for every card face down and still; the back's look, or None."""
        end = time.monotonic() + START_S * game.LIMIT
        since, last = None, None
        while time.monotonic() < end and self.wait(0.5):
            looks = [self.look(i) for i in range(CARDS)]
            back = np.median(np.array(looks), axis=0).astype(np.uint8)
            if all(_near(x, back, BACK) for x in looks) and last is not None and _moved(back, last) < HELD:
                since = since or time.monotonic()
                if time.monotonic() - since >= STILL_S:
                    self.back = back
                    return back
            else:
                since = None
            last = back
        return None

    def over(self):
        """Every card face up: all matched, or the chances ran out and the rest turned over."""
        return not any(self.down(self.look(i)) for i in range(CARDS))

    def flip(self, i):
        """Click a face-down card and read its face once still. None if it never turned or didn't hold."""
        for go in range(FLIPS):
            x, y = mid(self.boxes[i])
            game.click(x, y, park=False)
            end = time.monotonic() + FLIP_S * game.LIMIT
            while self.down(self.look(i)):
                if time.monotonic() > end or not self.wait():
                    break
            else:
                face = self.settle(i)
                if face is not None and not self.down(face):
                    self.read(i, face)
                return face
            if self.halt():
                return None
            self.say('WARN card_no_turn card=%d attempt=%d/%d' % (i + 1, go + 1, FLIPS))
        return None

    def read(self, i, face):
        """Colour and amount of a face up card, kept only if two looks 0.1 s apart agree (a fading flash doesn't)."""
        items = []
        for _ in range(2):
            raw = game.grab(self.boxes[i])
            items.append(None if self.down(_small(raw)) else face_of(raw, face))
            if items[-1] is None or len(items) == 2 or not self.wait(0.1):
                break
        if len(items) < 2 or items[1] is None or not _same_tint(items[0][1], items[1][1], TINT / 3):
            return
        self.tints[i] = items[1][1]
        self.amounts[i] = amount(raw)

    def settle(self, i):
        start = time.monotonic()
        prev = self.look(i)
        while time.monotonic() < start + SETTLE_S * game.LIMIT and self.wait(0.1):
            now = self.look(i)
            if self.down(now):                              # a wrong pair turning back already
                return prev if time.monotonic() - start >= SHOWN_S else None
            if _moved(prev, now) < HELD:
                return now
            prev = now
        return None

    def verdict(self, a, fa, b, fb):
        """True if a and b matched (they go green), False if they turned back, None if neither in time."""
        end = time.monotonic() + TURN_S * game.LIMIT
        while time.monotonic() < end and self.wait():
            la, lb = self.look(a), self.look(b)
            if self.down(la) and self.down(lb):
                return False
            if _green(la) and not _green(fa) or fb is not None and _green(lb) and not _green(fb):
                return True                                 # green that wasn't in its picture
        return None

    def same(self, fa, a, fb, b):
        """Same reward: same item (picture and colour) and amount (as numbers if both read, else as pictures)."""
        na, nb = self.amounts.get(a), self.amounts.get(b)
        if _apart((fa, self.tints.get(a)), (fb, self.tints.get(b))):
            return False
        if na is not None and nb is not None:
            return na == nb and _same_item((fa, self.tints.get(a)), (fb, self.tints.get(b)))
        return _same(fa, fb) and _same_tint(self.tints.get(a), self.tints.get(b))

    def twin(self, face, skip):
        for k, f in self.faces.items():
            if (k != skip and k not in self.done | self.dead and (k, skip) not in self.apart
                    and self.same(f, k, face, skip)):
                return k
        return None

    def useless(self, face, card):
        """A skipped reward: never paired on purpose, last chance or not."""
        return any(_same_item((face, self.tints.get(card)), s) for s in self.skip)

    def shown(self, k):
        n = self.amounts.get(k)
        return 'card %d x%s' % (k + 1, '?' if n is None else n)

    def alike(self, i, j):
        if (i, j) not in self.memo:
            self.memo[i, j] = (i, j) not in self.apart and self.same(self.faces[i], i, self.faces[j], j)
        return self.memo[i, j]

    def turn(self):
        """One pair. False when there's nothing left to try."""
        known = [k for k in self.faces if k not in self.done | self.dead]
        pairs = [(i, j) for i in known for j in known
                 if i < j and self.alike(i, j) and not self.useless(self.faces[i], i)]
        # biggest amount first, unread amounts last: a wrong pair can end the game before the rest are taken
        pair = max(pairs, key=lambda p: self.amounts.get(p[0]) or self.amounts.get(p[1]) or 0, default=None)
        fresh = [k for k in range(CARDS) if k not in self.faces and k not in self.done | self.dead]
        if pair:
            a, b = pair
            fa = self.flip(a)
            if fa is None:
                self.dead.add(a)
                return not self.halt()
            fb = self.flip(b)
        else:
            if not fresh and not known:
                if not self.dead or self.retried:
                    return False
                self.dead, self.retried = set(), True       # one more go at cards that wouldn't turn
                return True
            a = fresh[0] if fresh else known[0]
            fa = self.flip(a)
            if fa is None:
                self.dead.add(a)
                return not self.halt()
            b = None if self.useless(fa, a) else self.twin(fa, a)
            if b is None:                                   # a skipped reward's twin is left alone
                rest = [k for k in fresh if k != a] or [k for k in known if k != a and not self.same(self.faces[k], k, fa, a)]
                b = rest[0] if rest else self.twin(fa, a)   # nothing else left: take it
                if not rest and b is not None:
                    self.say('pair_skipped_last a="%s" b="%s"' % (self.shown(a), self.shown(b)))
                if b is None:                               # only look-alikes that didn't match: still play on
                    b = next((k for k in known if k != a), None)
                if b is None:
                    return False
            fb = self.flip(b)
        self.turns += 1
        matched = self.verdict(a, fa, b, fb)
        if matched is None:                                 # neither: the game may be over
            if self.over():
                return False
            matched = fa is not None and fb is not None and self.same(fa, a, fb, b)
        if matched:
            self.done |= {a, b}
            self.say('pair_matched n=%d a="%s" b="%s"' % (len(self.done) // 2, self.shown(a), self.shown(b)))
            return True
        for k, f in ((a, fa), (b, fb)):
            if f is not None:
                self.faces[k] = f
                self.memo = {p: v for p, v in self.memo.items() if k not in p}
        if fa is not None and fb is not None and self.same(fa, a, fb, b):
            self.apart |= {(a, b), (b, a)}                  # alike to us, not to the game
            self.say('WARN pair_mismatch a="%s" b="%s"' % (self.shown(a), self.shown(b)))
        return True

    def play(self):
        """-> pairs matched, or None if the cards never showed."""
        if self.ready() is None:
            return None
        end = time.monotonic() + GAME_S * game.LIMIT
        for _ in range(4 * CARDS):                          # failsafe
            if (len(self.done) == CARDS or self.halt() or time.monotonic() > end or self.spent()
                    or not self.turn()):
                break
        return len(self.done) // 2

    def spent(self):
        """Ended after its chances? Past CHANCES pairs, wait a moment for every card to show face up."""
        if self.turns < CHANCES:
            return False
        stop = time.monotonic() + OVER_S * game.LIMIT
        while not self.over():
            if time.monotonic() > stop or not self.wait(0.2):
                return False
        return True

    def finish(self):
        """Picture the face-up board, then press Close once it shows. True if it closed."""
        global last_shot
        end, shot = time.monotonic() + END_S * game.LIMIT, False
        while time.monotonic() < end and self.wait():
            if not _red(game.grab(self.close)):
                continue
            if not shot:
                if not self.over() and time.monotonic() < end - 1:
                    continue                                # the rest are still turning over
                game.paced(SHOWN_S)                         # the last ones finish turning
                ok, png = cv2.imencode('.png', cv2.cvtColor(game._grab_window(), cv2.COLOR_RGB2BGR))
                last_shot, shot = (png.tobytes() if ok else None), True
            x, y = mid(self.close)
            game.click(x, y, park=False)
            game.paced(0.5)
            if not _red(game.grab(self.close)):
                return True
        return False


def play(halt, say=print, skip=()):
    """At the stand: E and play, avoiding `skip` rewards: ('played', pairs), ('wait', seconds or None) or (None, why)."""
    got = None
    for _ in range(TRIES):
        game.tap('e')
        got = crafter._until(screen, OPEN_S, halt)
        if got or halt():
            break
    if halt():
        return None, 'stopped'
    if not got:
        return None, 'the Memory Match window did not open'
    o = layout()
    if got != 'ready':
        game.click(*o['shut'], park=False)
        return 'wait', (got if isinstance(got, int) else None)
    global last_shot
    last_shot = None
    board = Board(halt, say, skip)
    for _ in range(TRIES):
        game.held(START_WAIT_S)
        game.click(*mid(o['start']), park=False)
        pairs = board.play()
        if halt():
            return None, 'stopped'                          # mid-game: the stand's timer is read next time
        if pairs is not None:
            if not board.finish():
                say('WARN close_missing')
            return 'played', pairs
        if screen() != 'ready':
            break
    game.click(*o['shut'], park=False)
    return None, 'Start did not bring up the cards'
