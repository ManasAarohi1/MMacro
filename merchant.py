"""Auto merchant: teleport to it when it arrives, read its shop with OCR, buy what's on the list."""
import difflib
import re
import time

import cv2
import numpy as np

import game
import points

KNOWN_ITEMS = [
    "Void Coin", "Lucky Penny", "Gear A", "Gear B", "Mixed Potion",
    "Lucky Potion", "Lucky Potion L", "Lucky Potion XL",
    "Speed Potion", "Speed Potion L", "Speed Potion XL",
    "Fortune Spoid I", "Fortune Spoid II", "Fortune Spoid III",
    "Random Potion Sack", "Stella's Star", "Stella's Candle",
    "Rune of Wind", "Rune of Frost", "Rune of Rainstorm", "Rune of Hell",
    "Rune of Galaxy", "Rune of Corruption", "Rune of Nothing", "Rune of Everything",
    "Strange Potion I", "Strange Potion II", "Oblivion Potion",
    "Potion of Bound", "Merchant Tracker", "Heavenly Potion",
]
SLOTS = 5
TELEPORT_S = 3.0         # after Use, until in talk range
SHOP_WAIT_S = 8.0        # dialogue slides off, fade out and back
MESSAGE = (0.275, 0.835, 0.725, 0.865)    # answer line: green bought, red errors
TIER = re.compile(r'(?i)(?:un)?c[o0]mm[o0]n|r[ao]re|ep[il1]c|legend[ao]ry|myth[il1]c[ao]l')
OCR_THREADS = 2          # as accurate as 4, less CPU

_reader = None
_reader_used = 0.0


def wanted_from_text(text):
    """'Void Coin x5' per line -> {name: amount}, None means buy them all."""
    out = {}
    for line in (text or '').splitlines():
        line = line.strip()
        m = re.match(r'^(.*?)\s*(?:[x×]\s*(\d+)|\s(\d+))?$', line, re.I)
        if not line or not m or not m.group(1).strip():
            continue
        n = m.group(2) or m.group(3)
        out[m.group(1).strip()] = int(n) if n else None
    return out


def clean(text):
    text = text.lower().replace('1', 'i').replace('a', 'o')     # shop font: I reads as 1, a as o
    for r in ('common', 'uncommon', 'rore', 'epic', 'legendory', 'mythicol'):
        text = text.replace(r, '')
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z ]', '', text)).strip()


def numeral(raw, base):
    """How many I's follow `base` (OCR reads II as 11, ll or Il), or None."""
    t = raw.lower()
    i = t.rfind(base)
    m = re.match(r'[\s.:-]*([1il|]{1,3})(?![a-z]{2})', t[i + len(base):]) if i >= 0 else None     # 'lle' is II + junk
    return len(m.group(1)) if m else None


def identify(text, names):
    """The item a slot's text names, or the closest one (0.8+). I, II and III get counted separately."""
    raw, text = text, clean(text)
    if not text:
        return None
    name = _identify(text, names)
    m = re.match(r'^(.*\S)\s+(I{1,3})$', name or '')
    if m:
        base = m.group(1).split()[-1].lower()
        n = numeral(raw, base)
        if not n and base in raw.lower():           # numeral lost: I, II, III look alike
            return None
        same = [x for x in ('%s %s' % (m.group(1), 'I' * k) for k in (n or 0, (n or 0) - 1) if k) if x in names]
        if same:                                    # title's | read as one more I
            name = same[0]
    return name


def _identify(text, names):
    best, score = None, 0.0
    for name in sorted(names, key=len, reverse=True):
        c = clean(name)
        if c and c.replace(' ', '') in text.replace(' ', ''):
            return name
        c, t = c.replace(' ', ''), text.replace(' ', '')
        # title may run into a misread rarity: match the start too
        r = max(difflib.SequenceMatcher(None, c, t).ratio(), difflib.SequenceMatcher(None, c, t[:len(c)]).ratio())
        if r > score:
            best, score = name, r
    return best if score >= 0.8 else None


def reader():
    global _reader, _reader_used
    _reader_used = time.monotonic()
    if _reader is None:
        from rapidocr_onnxruntime import RapidOCR
        try:
            _reader = RapidOCR(intra_op_num_threads=OCR_THREADS, det_intra_op_num_threads=OCR_THREADS,
                               rec_intra_op_num_threads=OCR_THREADS)
        except Exception:
            _reader = RapidOCR()
    return _reader


def drop_reader(idle_s):
    """Let the OCR models go (about 30 MB) once unused this long; the next read loads them again (0.3 s)."""
    global _reader
    if _reader is not None and time.monotonic() - _reader_used > idle_s:
        _reader = None
        return True
    return False


def read_lines(img):
    if img is None or img.size == 0:
        return []
    result, _ = reader()(np.ascontiguousarray(img[..., ::-1]), use_cls=False)
    return [line[1] for line in result] if result else []


def identify_lines(lines, names):
    """Each line on its own first, then all together."""
    for line in lines:
        item = identify(line, names)
        if item:
            return item
    return identify(' '.join(lines), names) if len(lines) > 1 else None


def read_slot(i, scale=2):
    """Twice the size by default: OCR loses numerals far less often then."""
    img = points.grab('merchantShopItem[%d]' % i)
    if img is None or img.size == 0:
        return []
    return read_lines(cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale > 1 else img)


def slot_item(i, names):
    """Read a slot's name whole, at 2x then 1x."""
    for scale in (2, 1):
        lines = read_slot(i, scale)
        item = identify(' '.join(lines), names) or identify_lines(lines, names)
        if item or not lines:
            return item
    return None


why = ''


def failed(reason, say):
    global why
    why = reason
    say('WARN step_failed reason="%s"' % reason)
    return False


def nap(seconds, halt):
    end = time.perf_counter() + seconds * game.PACE
    while time.perf_counter() < end:
        if halt():
            return False
        time.sleep(0.02)
    return not halt()


def shut_inventory():
    if not game.close_inventory() and not game.sees_windows():
        points.click('inventoryInvClose', 0.2)              # unrecognised size: click its X anyway


def use_item(term, halt, say):
    """Search the Inventory for `term` and Use it, only if a result has the item's name colour."""
    what = term.title()
    if game.take_foreground() is None:
        return failed('Roblox would not come to the front', say)
    if not game.open_by_click('inventory', halt):
        return not halt() and failed('The Inventory did not open', say)
    if halt() or not points.click('inventoryItemsTab', 0.4):
        return not halt() and failed('The Items tab could not be clicked', say)
    slot = game.search_item(term, say, halt, lambda s: nap(s, halt))
    if slot is None:
        seen = game.inventory_shown()
        shut_inventory()
        return not halt() and failed('No %s found in the Inventory (none left, or the search failed)' % what if seen
                                     else 'The Inventory went off screen (a cutscene?)', say)
    if halt() or not game.press_use(slot, 0.4, halt):
        shut_inventory()
        return not halt() and failed('The %s was found but Use did not take' % what, say)
    shut_inventory()
    return True


def teleport(halt, say):
    """Use a Merchant Teleporter, then close the Inventory again (it can pop back up at the merchant)."""
    if not (use_item('merchant teleporter', halt, say) and nap(TELEPORT_S, halt)):
        return False
    points.click('inventoryInvClose', 0.3)
    return not halt()


def open_shop(halt, say=print):
    """Talk, skip the text, click the green Open Shop choice. Slow and steady, four tries."""
    lost = 0
    for go in range(1, 5):
        if halt():
            return False
        if not points.dialogue_up():                    # green and red together: a glow fakes only one
            game.down('e')
            time.sleep(0.1)
            game.up('e')
            if not nap(1.0, halt):
                return False
        for _ in range(3):
            if points.dialogue_up():
                break                                   # choices up: a click could pick one
            points.click('dialogueSkipDialogue', 0.3)
        if not nap(0.8, halt):
            return False
        at = points.choice('green') if points.dialogue_up() else None
        if at is None:
            say('WARN open_shop_choice_missing attempt=%d/4' % go)
            continue
        game.click(*at, park=False)
        if not nap(1.5, halt):
            return False
        if points.dialogue_up():                        # the click was lost
            lost += 1
            say('WARN open_shop_click_lost attempt=%d/4' % go)
            continue
        points.leave_dialogue()
        return nap(0.5, halt)
    if lost:
        return failed('Open Shop was clicked %d times but the shop never opened' % lost, say)
    return failed('Pressed E 4 times but the Open Shop choice never came up (the teleport missed, '
                  'the merchant left, or it was too far away)', say)


def read_shop(names, halt):
    """{slot: item} once the shop has faded in, with a second look at the rest."""
    end, k = time.perf_counter() + SHOP_WAIT_S * game.LIMIT, 0
    while not slot_item(k % SLOTS + 1, names):                  # shows 1-3 s after Shop
        k += 1
        if time.perf_counter() > end or not nap(0.1, halt):
            break
    if not nap(0.6, halt):                                      # rest of the fade from black
        return {}
    found = {}
    for attempt in range(2):
        for i in range(1, SLOTS + 1):
            if i in found or halt():
                continue
            try:
                item = slot_item(i, names)
            except Exception:
                item = None
            if item:
                found[i] = item
        if len(found) == SLOTS or not nap(0.3, halt):
            break
    return found


def title(lines):
    """The name in the panel's 'Name | Tier'. OCR can glue the | onto the name as I, l, 1 or T."""
    text = ' '.join(lines)
    m = TIER.search(text)
    if not m:
        return text
    name = re.sub(r'[^\w\s|]', '', text[:m.start()]).rstrip()          # OCR adds - and . too
    return name[:-1] if name[-1:] in '|I1T' else name


def selected(names, scale=1):
    """What the detail panel says is picked, i.e. what Purchase would buy."""
    img = points.grab('merchantShopproductDetailPanel')
    if img is None or img.size == 0:
        return None
    img = img[:max(1, img.shape[0] * 3 // 10)]
    lines = read_lines(cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale > 1 else img)
    return identify(title(lines), names) or identify_lines(lines, names)


def lit(name):
    """A shop button's white text is showing, with no dark veil over it."""
    img = points.grab(name)
    return img is not None and img.size > 0 and float((img.min(-1) > 200).mean()) >= 0.01


def answer_band():
    """The shop's answer line and its green and red text pixels. The newest line sits in the middle of MESSAGE."""
    rgb = game.grab(game.client_box(MESSAGE))
    c = rgb.astype(int)
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    return rgb, (g > 200) & (r < 120) & (b < 120), (r > 200) & (g < 120) & (b < 120)


def wait(test, seconds, halt):
    end = time.perf_counter() + seconds * game.LIMIT
    while not test():
        if halt() or time.perf_counter() > end:
            return False
        time.sleep(0.1)
    return True


def answer(before, halt):
    """Wait for a new answer line: 'good', 'sold', 'poor', 'limit', 'error' or None."""
    _, g0, r0 = before
    start, idle = time.perf_counter(), 0
    while True:
        _, g, r = answer_band()
        if int((g & ~g0).sum() + (r & ~r0).sum()) > 20 + 0.05 * int(g0.sum() + r0.sum()):
            break
        late = time.perf_counter() - start
        idle = idle + 1 if late > 1.0 * game.LIMIT and lit('merchantShopPurchaseButton') else 0
        if idle >= 2 or late > 6.0 * game.LIMIT or halt():      # no veil either: click lost
            return None
        time.sleep(0.1)
    if not nap(0.35, halt):
        return None
    rgb, g, r = answer_band()
    h = rgb.shape[0]
    text = re.sub(r'[^a-z]', '', ''.join(read_lines(rgb[h // 10:h * 9 // 10])).lower()).replace('a', 'o')
    for word, kind in (('error', 'error'), ('internol', 'error'), ('ldout', 'sold'), ('enou', 'poor'), ('dont', 'poor'),
                       ('lreody', 'limit'), ('purch', 'good')):
        if word in text:
            return kind
    g, r = int((g & ~g0)[h // 10:h * 9 // 10].sum()), int((r & ~r0)[h // 10:h * 9 // 10].sum())
    return 'good' if g > r else 'error'


def purchase(amount, halt):
    """Set the amount (None = Set to Max), Purchase, read the answer."""
    if not wait(lambda: lit('merchantShopPurchaseButton'), 8.0, halt):       # still loading, or buying the last one
        return None
    if amount is None:
        points.click('merchantShopSetToMaxButton', 0.15)
    else:
        points.click('merchantShopPurchaseAmountBox', 0.15)
        for key in (game.Key.backspace, game.Key.delete):      # clears on focus; few digits at most
            for _ in range(4):
                game.tap(key, hold=0.01, gap=0.01)
        game.type_term(str(amount), clear=False)
    before = answer_band()
    if halt() or not points.click('merchantShopPurchaseButton', 0.3):
        return None
    return answer(before, halt)


def box_number():
    """The amount box after Set to Max: the stock left. Its 1 can read as 7: the smaller wins."""
    img = points.grab('merchantShopPurchaseAmountBox')
    if img is None or img.size == 0:
        return 0
    reads = []
    for im in (img, cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)):
        digits = re.sub(r'\D', '', ''.join(read_lines(im)).replace('O', '0'))
        if digits:
            reads.append(int(digits))
    return min(reads) if reads else 0


def buy(slot, item, amount, names, halt, say=print):
    """Pick the slot, set the amount, Purchase. Checks the panel first: a lost click would buy the last pick."""
    if halt():
        return False
    if not points.click('merchantShopItem[%d]' % slot, 0.2):
        say('WARN buy_skipped item="%s" slot=%d reason=slot_click_failed' % (item, slot))
        return False
    shown = None
    for k in range(3):
        try:
            shown = selected(names, 1 + k % 2)          # 1x and 2x misread different numerals
        except Exception:
            shown = None
        if shown == item or halt():
            break
        points.click('merchantShopItem[%d]' % slot, 0.3)
    if shown != item:
        say('WARN buy_skipped item="%s" slot=%d reason=wrong_pick shown="%s"' % (item, slot, shown or 'unreadable'))
        return False
    n, left, retries, bought = amount, amount, 1, False     # left: to buy, None = all
    while not halt():
        got = purchase(n, halt)
        say('purchase_result item="%s" amount=%s result=%s' % (item, 'max' if n is None else n, got or 'none'))
        if got == 'good':
            bought = True
            if n == left:
                break
            left = None if left is None else left - n
            n = min(n // 2, 10 ** 6 if left is None else left)    # too dear: keep halving
            if n < 1:
                break
        elif got == 'poor':
            n = min(box_number(), n or 10 ** 6) // 2      # count must drop, whatever the box reads
            if n < 1:
                break
        elif got in ('sold', 'limit') or retries < 1:   # one retry for a lost click or error
            break
        else:
            retries -= 1
    if not bought:
        say('WARN buy_failed item="%s"' % item)
    return bought


def collect(merchant, wanted, halt, say=print):
    """Teleport, read the shop, buy: ({slot: name}, [bought]), None if no shop, False if no teleport."""
    global why
    why = ''
    say('merchant_arrived merchant="%s" action=teleport' % merchant)
    if not teleport(halt, say):
        return False
    if not open_shop(halt, say):
        say('WARN shop_open_failed merchant="%s"' % merchant)
        return None
    wanted = {(identify(n, KNOWN_ITEMS) or n): a for n, a in wanted.items()}
    names = KNOWN_ITEMS + [n for n in wanted if n not in KNOWN_ITEMS]
    found = read_shop(names, halt)
    say('shop_read merchant="%s" n=%d items=[%s]'
        % (merchant, len(found), ','.join('%d="%s"' % kv for kv in sorted(found.items()))))
    if not any(item in wanted for item in found.values()):
        say('buy_list_absent wanted=[%s]' % ','.join(wanted))
    bought = []
    for slot, item in sorted(found.items()):
        if item in wanted and buy(slot, item, wanted[item], names, halt, say):     # all names: slots can be misread
            bought.append(item if wanted[item] is None else '%s x%d' % (item, wanted[item]))
    return found, bought


def close_shop():
    for _ in range(3):                                      # a missed click on the X leaves the shop up
        points.click('merchantShopCloseButton', 0.2)
        time.sleep(0.6)
        if not any(slot_item(i, KNOWN_ITEMS) for i in (1, SLOTS)):
            break
    points.leave_dialogue(skip=True)                        # the dialogue can outlive the shop
