"""Stats for everything MMacro does, counted from its log events and kept in stats.jsonl for the views."""
import collections
import datetime
import json
import os
import re
import time

import cv2

import game
import merchant

NUMBER = re.compile(r'\d{1,3}(?:[,.]\d{3})+|\d+')
FIELD = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
HISTORY = os.path.join(game.APP_DIR, 'stats.jsonl')
VIEWS = ('Last run', 'Last hour', 'Today', 'Last 7 days', 'Last 30 days', 'All time', 'Custom')
BOSS_NAMES = {'Boss': 'Sinister S.'}
BIOMES = ()                     # shown biome names, set by the app
MERCHANTS = ('Mari', 'Jester', 'Rin', 'Sinister S.', 'Sinister P.')
ITEMS = ('Biome Randomizer', 'Strange Controller', 'Cursed Rune Fragment')


def read_points():
    """The fish points counter (blue, bottom left), or None. Its size follows the window's height."""
    w = game.roblox_window()
    if w is None:
        return None
    x, y, _, h = game.client_rect(w)
    img = game.grab((x, int(y + h * 0.88), int(x + h * 0.21), int(y + h * 0.935)))
    return parse(merchant.read_lines(cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)))


def parse(lines):
    found = NUMBER.findall(' '.join(lines))
    return int(re.sub(r'\D', '', max(found, key=len))) if found else None


def read_points_sure(gap=0.5):
    """Two reads that agree, or None."""
    a = read_points()
    if a is None:
        return None
    time.sleep(gap)
    return a if read_points() == a else None


def _int(v, default=1):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def counts_for(tag, msg):
    """{stat: n} a log line adds."""
    event, _, rest = str(msg).partition(' ')
    if event in ('WARN', 'ERROR', 'DEBUG'):
        event, _, rest = rest.partition(' ')
    f = {m.group(1): m.group(2) if m.group(2) is not None else m.group(3) for m in FIELD.finditer(rest)}
    if event == 'catch':
        return {'Fish caught': 1}
    if event == 'catch_lost':
        return {'Fish lost': 1}
    if event == 'sell_done':
        return {'Sells': 1}
    if event == 'biome_start' and f.get('biome'):
        return {'Biome: ' + f['biome']: 1}
    if event == 'merchant_arrived' and tag == 'merchant' and f.get('name'):
        return {'Seen: ' + BOSS_NAMES.get(f['name'], f['name']): 1}
    if event == 'purchase_result' and f.get('result') == 'good':
        return {'Bought: ' + f.get('item', '?'): _int(f.get('amount'))}
    if event == 'bought':
        return {'Bought: ' + f.get('item', 'Lucky Potion'): _int(f.get('n'))}
    if event == 'crafted_notification' or (event == 'craft_result' and f.get('result') == 'crafted'):
        return {'Crafted: ' + (f.get('item') or 'a potion'): 1}
    if event == 'memory_done':
        return {'Memory Match games': 1, 'Memory Match pairs': _int(f.get('pairs', '0').split('/')[0], 0)}
    if event == 'eden_seen':
        return {'Eden found': 1}
    if event == 'clip_start':
        return {'Clips': 1}
    if event == 'item_used' and f.get('term'):
        return {'Used: ' + f['term'].title(): 1}
    if event == 'potion_used' and f.get('potion'):
        return {'Potion: ' + f['potion']: _int(f.get('n'))}
    if event == 'selector_used':
        return {'Biome Selector uses': 1}
    if event == 'reconnect_start':
        return {'Reconnects': 1}
    if event == 'daily_claimed':
        return {'Daily rewards claimed': 1}
    if event == 'run_start':
        return {'Runs': 1}
    return {}


class Stats:
    """One run. Points spent between two reads (Lucky Potions) don't lower the rate."""
    # ponytail: a sell and buy in one read gap cancel out; read more often if needed

    def __init__(self, now):
        self.start, self.counts = now, collections.Counter()
        self.run = round(time.time())                   # names the run in the history
        self.end = None
        self.last = None
        self.reads = 0
        self.saved = (now, collections.Counter())       # time and counts already in the history

    def event(self, tag, msg):
        self.counts.update(counts_for(tag, msg))

    def points(self, value):
        """A new fish points reading; returns its change since the last one."""
        change = 0 if self.last is None else value - self.last
        if change > 0:
            self.counts['Fish points gained'] += change
        elif change < 0:
            self.counts['Fish points spent'] -= change
        self.last, self.reads = value, self.reads + 1
        return change

    def pending(self, now, wall=None):
        """What isn't in the history yet, as a history record."""
        t, old = self.saved
        c = {k: v for k, v in (self.counts - old).items() if v}
        return {'t': round(time.time() if wall is None else wall), 's': round(now - t), 'c': c, 'run': self.run}

    def save(self, now, path=HISTORY):
        rec = self.pending(now)
        if rec['s'] <= 0:
            return None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec) + '\n')
        except OSError:
            return None
        self.saved = (now, collections.Counter(self.counts))
        return rec

    def summary(self, now):
        now = self.end if self.end is not None else now
        return {'s': int(now - self.start), 'c': dict(self.counts), 'reading': self.reads < 2}


def load(path=HISTORY):
    out = []
    try:
        with open(path, encoding='utf-8') as f:
            for row in f:
                try:
                    out.append(json.loads(row))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def last_run(records):
    """The records of the newest run in the history."""
    runs = [r['run'] for r in records if 'run' in r]
    return [r for r in records if runs and r.get('run') == max(runs)]


def span(view, now=None, custom=None):
    """(from, to) epoch seconds for a view; None for 'Last run'."""
    now = time.time() if now is None else now
    day = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {'Last hour': (now - 3600, now), 'Today': (day, now), 'Last 7 days': (now - 7 * 86400, now),
            'Last 30 days': (now - 30 * 86400, now), 'All time': (0, now), 'Custom': custom}.get(view)


def total(records, since, until):
    """Counts and running time of the records ending in [since, until]."""
    c = collections.Counter()
    secs = 0
    for r in records:
        if since <= r.get('t', 0) <= until:
            secs += r.get('s', 0)
            c.update(r.get('c', {}))
    return {'s': secs, 'c': dict(c), 'reading': False}


def custom_span(a, b):
    """'YYYY-MM-DD' to 'YYYY-MM-DD' (both days whole) as epoch seconds, or None."""
    try:
        start = datetime.datetime.strptime(a.strip(), '%Y-%m-%d')
        end = datetime.datetime.strptime(b.strip(), '%Y-%m-%d') + datetime.timedelta(days=1)
    except ValueError:
        return None
    return (start.timestamp(), end.timestamp()) if end > start else None


def line(d):
    """The stats as one log line."""
    c = d['c']
    hours = max(d['s'], 1) / 3600
    keys = ' '.join('%s=%d' % (re.sub(r'\W+', '_', k).strip('_').lower(), v) for k, v in sorted(c.items()))
    return 'run_s=%d fish_per_h=%.1f points_per_h=%d %s' % (
        d['s'], c.get('Fish caught', 0) / hours, c.get('Fish points gained', 0) / hours, keys)


def _rows(c, prefix, fixed=()):
    """(name, count) for every fixed name (0 if none yet), then any other name seen, most first."""
    rows = [(k, c.get(prefix + k, 0)) for k in fixed]
    extra = sorted(((k[len(prefix):], v) for k, v in c.items() if k.startswith(prefix) and k[len(prefix):] not in fixed),
                   key=lambda kv: -kv[1])
    return rows + extra


def layout(d):
    """(tiles, sections) for the window: big numbers on top, then (title, rows, wide) sections. Every biome, merchant
    and item is listed, at 0 until it happens; lists that depend on the run (potions, buys, crafts) say None."""
    c, secs = d['c'], d['s']
    hours = max(secs, 1) / 3600
    n = lambda v: format(int(round(v)), ',')
    points = 'reading...' if d.get('reading') and not c.get('Fish points gained') else n(c.get('Fish points gained', 0) / hours)
    tiles = [('Fish caught', n(c.get('Fish caught', 0))), ('Fish / hour', '%.1f' % (c.get('Fish caught', 0) / hours)),
             ('Fish points / hour', points), ('Running', '%dh %02dm' % (secs // 3600, secs % 3600 // 60))]
    shown = lambda rows: [(k, n(v)) for k, v in rows] or [('None', '')]
    fishing = [(k, c.get(k, 0)) for k in ('Fish lost', 'Sells', 'Fish points gained', 'Fish points spent')]
    items = _rows(c, 'Used: ', ITEMS) + [('Biome Selector', c.get('Biome Selector uses', 0))]
    other = [(label, c.get(k, 0)) for label, k in (('Eden found', 'Eden found'), ('Clips', 'Clips'),
                                                   ('Daily rewards', 'Daily rewards claimed'),
                                                   ('Reconnects', 'Reconnects'), ('Runs', 'Runs'))]
    sections = [('Fishing', shown(fishing), False),
                ('Merchants and bosses', shown(_rows(c, 'Seen: ', MERCHANTS)), False),
                ('Biomes', shown(_rows(c, 'Biome: ', BIOMES)), True),
                ('Items used', shown(items), False),
                ('Potions used', shown(_rows(c, 'Potion: ')), False),
                ('Bought', shown(_rows(c, 'Bought: ')), False),
                ('Crafted', shown(_rows(c, 'Crafted: ')), False),
                ('Memory Match', shown([('Played', c.get('Memory Match games', 0)),
                                        ('Pairs', c.get('Memory Match pairs', 0))]), False),
                ('Other', shown(other), False)]
    return tiles, sections


def demo():
    s = Stats(0.0)
    for tag, msg in [('fishing', 'catch n=1 reel_clicks=20 bag=1/10'), ('fishing', 'catch n=2 late=true'),
                     ('fishing', 'WARN catch_lost reel_clicks=53'), ('biome', 'biome_start biome="Glitched"'),
                     ('merchant', 'merchant_arrived name="Boss" via="cache"'), ('merchant', 'merchant_arrived merchant="Mari" action=teleport'),
                     ('merchant', 'purchase_result item="Void Coin" amount=2 result=good'),
                     ('merchant', 'purchase_result item="Void Coin" amount=2 result=none'),
                     ('memory', 'memory_done pairs=6/10'), ('craft', 'craft_result item="Warp Potion" result=crafted next_min=5'),
                     ('craft', 'craft_result item="Warp Potion" result=added next_min=5'),
                     ('potions', 'potion_used potion="Haste Potion II" n=9 asked=9')]:
        s.event(tag, msg)
    c = s.counts
    assert c['Fish caught'] == 2 and c['Fish lost'] == 1 and c['Biome: Glitched'] == 1, c
    assert c['Seen: Sinister S.'] == 1 and 'Seen: Mari' not in c and c['Bought: Void Coin'] == 2, c
    assert c['Memory Match pairs'] == 6 and c['Crafted: Warp Potion'] == 1 and c['Potion: Haste Potion II'] == 9, c
    s.event('items', 'item_used term="biome randomizer" slot=1')
    assert c['Used: Biome Randomizer'] == 1, c
    assert s.points(1000) == 0 and s.points(1500) == 500 and s.points(1200) == -300
    assert c['Fish points gained'] == 500 and c['Fish points spent'] == 300
    rec = s.pending(1800.0, wall=1_800_000_000)
    assert rec['s'] == 1800 and rec['c']['Fish caught'] == 2
    s.saved = (1800.0, collections.Counter(s.counts))
    s.event('fishing', 'catch n=3')
    assert s.pending(1900.0)['c'] == {'Fish caught': 1}
    assert parse(['309,033']) == 309033 and parse(['F', '1.204.500']) == 1204500 and parse(['F']) is None
    now = 1_800_000_000
    recs = [{'t': now - 10, 's': 600, 'c': {'Fish caught': 20, 'Fish points gained': 1000}},
            {'t': now - 8 * 86400, 's': 3600, 'c': {'Fish caught': 100}}]
    week = total(recs, *span('Last 7 days', now))
    assert week['c']['Fish caught'] == 20 and week['s'] == 600, week
    tiles, sections = layout(week)
    assert tiles[0] == ('Fish caught', '20') and tiles[1] == ('Fish / hour', '120.0') and tiles[2][1] == '6,000', tiles
    got = {title: rows for title, rows, _ in sections}
    assert ('Fish points gained', '1,000') in got['Fishing'] and ('Sells', '0') in got['Fishing'], got['Fishing']
    assert got['Items used'][:2] == [('Biome Randomizer', '0'), ('Strange Controller', '0')], got['Items used']
    assert got['Merchants and bosses'][-1] == ('Sinister P.', '0') and got['Potions used'] == [('None', '')]
    assert total(recs, *span('All time', now))['c']['Fish caught'] == 120
    assert 'fish_caught=3' in line(s.summary(1900.0))
    old = [{'t': 5, 's': 60, 'c': {'Fish caught': 1}, 'run': 1}, {'t': 9, 's': 60, 'c': {'Fish caught': 2}, 'run': 7},
           {'t': 10, 's': 30, 'c': {'Fish caught': 3}, 'run': 7}]
    assert total(last_run(old), 0, 99)['c']['Fish caught'] == 5 and last_run([]) == []
    a, b = custom_span('2026-11-01', '2026-11-01')
    assert b - a == 86400 and custom_span('2026-11-02', '2026-11-01') is None and custom_span('x', 'y') is None
    print('stats ok')


if __name__ == '__main__':
    demo()
