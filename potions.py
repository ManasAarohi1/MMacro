"""Auto pop: use picked potions from the Inventory as soon as a picked biome starts.
The Use box is set to 1 and Use pressed once per potion, each one seen to take.
Names' colours are checked so a brother potion isn't used."""
import game

POTIONS = ('Heavenly Potion', 'Oblivion Potion', 'Godlike Potion', 'Transcendent Potion', 'Godly Potion',
           'Potion of Bound', 'Warp Potion', 'Jewelry Potion', 'Zombie Potion', 'Rage Potion', 'Diver Potion',
           'Fortune Potion I', 'Fortune Potion II', 'Fortune Potion III', 'Haste Potion I', 'Haste Potion II',
           'Haste Potion III', 'Lucky Potion', 'Speed Potion')
TIERS = {'Fortune Potion I': (105, 255, 122), 'Fortune Potion II': (52, 142, 64), 'Fortune Potion III': (44, 101, 29),
         'Haste Potion I': (128, 187, 219), 'Haste Potion II': (4, 175, 236), 'Haste Potion III': (9, 137, 207)}
GAP_S = 0.15             # between uses
MAX_USES = 300


def term(name):
    """The search for exactly `name`: '$' pins the end, so a shorter name never lists the longer ones."""
    return name.lower() + '$'


def looks_like(slot, name):
    """None if the result's name colour fits `name`, else the brother potion it looks like (or 'unreadable')."""
    if name not in TIERS:
        return None
    rgb = game.name_rgb(slot)
    if rgb is None:
        return 'unreadable'
    family = [n for n in TIERS if n.rsplit(' ', 1)[0] == name.rsplit(' ', 1)[0]]
    best = min(family, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb, TIERS[n])))
    return None if best == name else best


def use(name, want, halt, say):
    """Use `want` of potion `name`, one at a time, stopping when they run out. Returns how many were used."""
    if game.take_foreground() is None or not game.open_by_click('inventory', halt):
        say('the Inventory did not open')
        return 0
    if halt() or not game.press_point('items', settle=0.3):
        return 0
    slot = None
    for _ in range(3):
        slot = game.search_item(term(name), say, halt, game.paced)
        other = None if slot is None else looks_like(slot, name)
        if other is None:
            break
        say('  the result looks like %s, not %s: searching again' % (other, name))
        slot = None
    if slot is None:
        if not halt():
            say('no %s in the Inventory' % name)
        return 0
    n = max(0, min(want, MAX_USES))
    used = 0
    while used < n and not halt():
        if not game.use_slot(slot, say, 0.3, halt, ours=used > 0):      # stops once none are left to pick
            break
        used += 1
        game.paced(GAP_S)
    say('used %d %s%s' % (used, name, '' if used == n else ' (%d asked)' % n))
    return used
