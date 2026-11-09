"""Keep one aura equipped through Aura Storage.
The Equip/Unequip button wins over the log, which can keep naming the old aura."""
import difflib
import re
import time

import cv2
import numpy as np

import clipper
import game
import labels
import merchant
import points

TRIES = 3               # searches before giving up
EQUIP_WAIT_S = 10.0     # wait for Unequip (or the log) after Equip; never press twice
CELLS = ['auraStorageAuraCell[1,%d]' % c for c in range(1, 9)] + ['auraStorageAuraCell[2,%d]' % c for c in range(1, 9)]
REGULAR = 'auraStorageAuraRegularTab'
NAME_AREA = ('auraStorageAuraSelectedPreview', 'auraStorageAuraInfoPanel')

norm = clipper.norm


def resolve(text):
    """The aura `text` means: exact, the only one containing it, else the closest. None if nothing's close."""
    want = norm(text)
    if not want:
        return None
    names = clipper.names()
    exact = [n for n in names if norm(n) == want]
    if exact:
        return exact[0]
    inside = [n for n in names if want in norm(n)]
    if len(inside) == 1 and len(want) >= 3:
        return inside[0]
    close = difflib.get_close_matches(want, [norm(n) for n in names], n=1, cutoff=0.8)
    return next(n for n in names if norm(n) == close[0]) if close else None


def search_term(name):
    """What to type in search: the name without symbols if that still matches, else its longest word.
    Symbols like - [ ] + . go in as '.' (any one)."""
    name = name.replace('_', ' : ')
    for keep in (r'[^\x20-\x7e]+', r'[^A-Za-z0-9 ]+'):
        plain = re.sub(r'\s+', ' ', re.sub(keep, ' ', name)).strip()
        if len(plain) >= 3 and plain.lower() in name.lower():
            return re.sub(r'[\^$()%.\[\]*+?-]', '.', plain)
    words = re.findall(r'[A-Za-z0-9]+', name)
    return max(words, key=len) if words else ''


def read(rgb, small=40):
    """OCR lines, upscaled 3x when small."""
    return [t for t, _, _ in read_rows(rgb, small)]


def read_rows(rgb, small=40):
    """OCR lines as (text, top, bottom), in rgb's pixels."""
    if rgb is None or rgb.size == 0:
        return []
    k = 3 if rgb.shape[0] < small else 1
    if k > 1:
        rgb = cv2.resize(rgb, (rgb.shape[1] * k, rgb.shape[0] * k), interpolation=cv2.INTER_CUBIC)
    result, _ = merchant.reader()(np.ascontiguousarray(rgb[..., ::-1]), use_cls=False)
    return [(t, min(p[1] for p in box) / k, max(p[1] for p in box) / k) for box, t, _ in result or []]


def button():
    """'equip', 'unequip' or None if unsure. Never press on None: on a worn aura it takes it off."""
    rgb = points.grab('auraStorageAuraEquipButton')
    if rgb is None or rgb.size == 0:
        return None
    seen = labels.equip_button(rgb)                     # by its look first, OCR only if unsure
    if seen and lettering(rgb) in (seen, None):
        return seen
    words, ink = norm(' '.join(read(rgb))), lettering(rgb)
    if 'nequip' in words or 'unequ' in words:
        return 'unequip'
    if re.fullmatch(r'equip', words):
        return None if ink == 'unequip' else 'equip'     # a red "Equip" is a misread Unequip
    return ink


def lettering(rgb):
    """Green text 'equip', red 'unequip', else None."""
    px = rgb.reshape(-1, 3).astype(int)
    lit = px.max(-1)
    text = px[lit > np.median(lit) + 40]                # text is much brighter than the fill
    if len(text) < 0.02 * len(px):
        return None
    r, g, b = text.mean(0)
    if g > r + 40:
        return 'equip'
    if r > g + 40 and r > b + 20:
        return 'unequip'
    return None


def shown():
    """The aura the left panel names, or None."""
    boxes = [points.box(n) for n in NAME_AREA]
    if None in boxes:
        return None
    area = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    rows = read_rows(game.grab(area), small=200)
    lines = [t for t, _, _ in rows]
    keys = {fold(n): n for n in clipper.names() if fold(n)}
    p = boxes[0]
    mid = p[1] - area[1] + 0.4 * (p[3] - p[1])      # where a one-line name is centred
    # only the name line: "STARSCOURGE" + "[Information]" must not pass as STARSCOURGE : RADIANT
    rows = [r for r in rows if (r[1] + r[2]) / 2 < p[3] - area[1] and odds_of(r[0]) is None]
    # long names wrap, so try line pairs first, near misses too
    pairs = [(a[0] + b[0], a[1], b[2], 2) for i, a in enumerate(rows) for b in rows[i + 1:]]   # world text sits between
    for tries in (pairs, [r + (1,) for r in rows]):
        tries = [(fold(t), mid - (top + bot) / 2 > 0.25 * (bot - top) / n) for t, top, bot, n in tries if fold(t)]
        for line, high in tries:
            if line in keys:
                return odds_check(keys[line], lines, high)
        for line, high in tries:
            close = difflib.get_close_matches(line, list(keys), n=1, cutoff=0.8)
            if close:
                return odds_check(keys[close[0]], lines, high)
    for t, _, _ in rows:                        # world text behind the panel can join in
        inside = [k for k in keys if len(k) >= 4 and k in fold(t)]
        if inside:                              # the rest may be a misread longer name: odds decide
            return odds_check(keys[max(inside, key=len)], lines, True)
    return None


def odds_check(name, lines, high=False):
    """`name`, unless it may be the start of a longer name whose 2nd line OCR lost.
    The "1 in N" line tells them apart. Without it: None if the name sits `high`, else `name`."""
    table = clipper.table()
    longer = [k for k in table if k != norm(name) and k.startswith(norm(name))]
    if not longer:
        return name
    odds = next((odds_of(t) for t in lines if odds_of(t) is not None), None)
    if odds is None:
        return None if high else name
    if odds == table[norm(name)][0]:
        return name
    match = [k for k in longer if table[k][0] == odds]
    if len(match) == 1:
        return next(n for n in clipper.names() if norm(n) == match[0])
    return None if match else name             # an odd number that fits no longer name is a misread


def odds_of(line):
    """N from a "1 in N" line, else None."""
    hit = re.fullmatch(r'[1il|t]?(?:[il1|]n|m)([0-9o][0-9o,.]*)', re.sub(r'\s', '', line.lower()))
    return int(re.sub(r'[^0-9]', '', hit.group(1).replace('o', '0'))) if hit else None


def fold(text):
    """norm with o and a merged: OCR reads a as o."""
    return norm(text).replace('o', 'a')


def nap(seconds, halt):
    end = time.perf_counter() + seconds * game.PACE
    while time.perf_counter() < end:
        if halt():
            return False
        time.sleep(0.02)
    return not halt()


def find(aura, halt, say, skip=()):
    """Search the open Aura Storage and click cells (minus `skip`) until the panel names `aura`. The cell, or None."""
    if halt() or not points.click(REGULAR, 0.2) or not points.click('auraStorageAuraSearchContent', 0.15):
        return None
    game.type_term(search_term(aura))
    if not nap(0.5, halt):
        return None
    for cell in CELLS:                          # copies and variants share a name
        if cell in skip:
            continue
        if halt() or not points.click(cell, 0.2):
            return None
        now = shown()
        if now is not None and norm(now) == norm(aura):
            return cell
    return None


def shut():
    """Close Aura Storage, or click its X anyway."""
    if not game.close_inventory():
        points.click('auraStorageAuraCloseButton', 0.2)


def equip(aura, halt, say=print, logged=lambda since: False, other=lambda since: None):
    """Equip `aura` through Aura Storage. True once it's on.
    logged(since): the log says it went on. other(since): some other aura that did, or None."""
    if game.take_foreground() is None:
        return False
    wrong = set()                               # cells that turned out to be another aura
    for go in range(1, TRIES + 1):
        if halt():
            return False
        if not game.open_by_click('aura_storage', halt):
            if halt():
                return False
            shut()                              # the side button toggles, so it may be open anyway
            say('Aura Storage did not open (go %d of %d)' % (go, TRIES))
            continue
        cell = find(aura, halt, say, wrong)
        if cell is None:
            shut()
            if halt():
                return False
            say('%s not found in Aura Storage (go %d of %d)' % (aura, go, TRIES))
            continue
        state = button()
        now = shown()                           # a cell click can land late: the panel read may be old
        if now is not None and norm(now) != norm(aura):
            wrong.add(cell)
            shut()
            say('%s turned out to be %s (a cell click landed late) (go %d of %d)' % (cell, now, go, TRIES))
            continue
        if state == 'unequip':
            say('%s is equipped (its button says Unequip)' % aura)
            shut()
            return True
        if state is None:
            on = logged(0.0)
            say('%s: the Equip button could not be read, so it is not pressed (Roblox\'s log says it is %s; '
                'an odd window shape does this: press F11 for a window MMacro sizes)'
                % (aura, 'equipped' if on else 'not equipped'))
            shut()
            return on
        since = time.time()
        if not points.click('auraStorageAuraEquipButton', 0.1):
            shut()
            return False
        end, how = time.perf_counter() + EQUIP_WAIT_S * game.PACE, None
        while time.perf_counter() < end and not halt():
            if button() == 'unequip':
                how = 'the button now says Unequip'
            elif logged(since):
                how = 'Roblox\'s log says so'
            if how:
                break
            time.sleep(0.2)
        if how and nap(1.0, halt) and other(since):
            say('Roblox\'s log says %s went on instead (a cell read wrong): trying the next one (go %d of %d)'
                % (other(since), go, TRIES))
            wrong.add(cell)
            shut()
            continue
        now = shown() if how else None          # clicks land in order, so the panel now names what really went on
        if now is not None and norm(now) != norm(aura):
            say('%s went on instead (a cell click landed late): trying the next one (go %d of %d)' % (now, go, TRIES))
            wrong.add(cell)
            shut()
            continue
        if how:
            say('%s equipped (%s)' % (aura, how))
            shut()
            return True
        if halt():
            return False
        # never press again: the same button takes it off
        say('%s not equipped yet: its press may still land, so it is checked again later' % aura)
        shut()
        return False
    if not halt():
        say('%s could not be equipped after %d goes' % (aura, TRIES))
    return False
