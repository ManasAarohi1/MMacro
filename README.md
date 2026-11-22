# MMacro

A Windows macro for Sol's RNG with auto fishing, Eden, merchant
detection and collection, cauldron crafting, Memory Match, Biome Selector and potion use,
clipping rare auras, auto reconnect and Discord webhook pings.

## Run from source

Windows 10 or 11, Python 3.11 or newer.

```
pip install -r requirements.txt
python app.py
```

Settings and `logs.txt` are kept in `%LOCALAPPDATA%\MMacro`.

## Files

- `app.py` - the window and the run loop
- `game.py`, `points.py` - the Roblox window, clicks, keys and where every button is (`gui_points.json`)
- `fishing.py`, `eden.py`, `merchant.py`, `merchant_cache.py`, `equip.py`, `clipper.py`, `crafter.py`,
  `craftpath.py`, `memmatch.py`, `selector.py`, `potions.py` - each feature
- `labels.py`, `labels_data.py` - reading on-screen text
- `memory_items/` - Memory Match reward pictures


## License

GPL-3.0
