"""Auto pop: use picked potions from the Inventory when a picked biome starts."""
import game

POTIONS = ('Heavenly Potion', 'Oblivion Potion', 'Godlike Potion', 'Transcendent Potion', 'Godly Potion',
           'Potion of Bound', 'Warp Potion', 'Jewelry Potion', 'Zombie Potion', 'Rage Potion', 'Diver Potion',
           'Fortune Potion I', 'Fortune Potion II', 'Fortune Potion III', 'Haste Potion I', 'Haste Potion II',
           'Haste Potion III', 'Lucky Potion', 'Speed Potion', 'Red Moon Potion I', 'Red Moon Potion II',
           'Potion of the Dune', "Pump King's Blood", 'Tear of the Goddess')
TIERS = {'Fortune Potion I': (105, 255, 122), 'Fortune Potion II': (52, 142, 64), 'Fortune Potion III': (44, 101, 29),
         'Haste Potion I': (128, 187, 219), 'Haste Potion II': (4, 175, 236), 'Haste Potion III': (9, 137, 207)}
GAP_S = 0.15             # between uses
MAX_USES = 300


def term(name):
    """Search term matching exactly `name` ('$' pins the end)."""
    return name.lower() + '$'


def looks_like(slot, name):
    """None if the name colour fits `name`, else the tier it looks like (or 'unreadable')."""
    if name not in TIERS:
        return None
    rgb = game.name_rgb(slot)
    if rgb is None:
        return 'unreadable'
    family = [n for n in TIERS if n.rsplit(' ', 1)[0] == name.rsplit(' ', 1)[0]]
    best = min(family, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb, TIERS[n])))
    return None if best == name else best


def use(name, want, halt, say):
    """Use up to `want` of potion `name` one at a time; returns how many were used."""
    if game.take_foreground() is None or not game.open_by_click('inventory', halt):
        say('ERROR inventory_open_failed potion="%s"' % name)
        return 0
    if halt() or not game.press_point('items', settle=0.3):
        return 0
    slot = None
    for _ in range(3):
        slot = game.search_item(term(name), say, halt, game.paced)
        other = None if slot is None else looks_like(slot, name)
        if other is None:
            break
        say('WARN potion_tier_mismatch potion="%s" seen="%s" attempt=%d/3' % (name, other, _ + 1))
        slot = None
    if slot is None:
        if not halt():
            say('potion_not_found potion="%s"' % name)
        return 0
    n = max(0, min(want, MAX_USES))
    used = 0
    while used < n and not halt():
        if not game.use_slot(slot, say, 0.3, halt, ours=used > 0):      # False once none are left
            break
        used += 1
        game.paced(GAP_S)
    say('potion_used potion="%s" n=%d asked=%d' % (name, used, n))
    return used
