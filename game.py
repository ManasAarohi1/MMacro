"""Everything that touches Roblox: settings, its log, keys, mouse, screen, rejoining and menus."""
import atexit
import ctypes
import datetime
import glob
import json
import math
import os
import random
import re
from urllib.parse import parse_qs, urlsplit
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes
from time import sleep as _sleep

import numpy as np
import psutil
from mousekey import MouseKey
from PIL import Image, ImageGrab
from pynput.keyboard import Controller as KeyboardController, Key, KeyCode

ABORT = False            # set by Stop

# ================================================================ settings
APP_DIR = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "MMacro")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.jsonl")
DEFAULTS = {
    "webhook_url": "",
    "cooldown_seconds": 300,
    "auto_items": False,
    "anti_afk": True,
    "clicks": {},
    "private_server": "",
    "auto_reconnect": False,
    "low_end": False,
    "rejoin_browser": False,
    "eden_on": False,
    "eden_ping": "",          # Discord id to ping
    "eden_record": False,     # record 60 s when Eden shows
    "eden_path": True,        # walk to Eden, sign contract
    "merchant_on": False,     # via Roblox's cache
    "collect_on": False,
    "collect_merchants": ["Jester", "Mari", "Rin"],
    "buy_items": {},
    "buy_list": "",
    "fish_on": False,
    "fish_vip": True,
    "fish_sell": 0,
    "fish_sell_all": True,
    "fish_full_check": True,
    "fish_sell_amount": 10,
    "clip_on": False,
    "clip_min": "1,000,000,000",
    "clip_quality": "768p",
}


def _kind(value):
    return "bool" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else type(value).__name__


def load_settings():
    """Settings, with anything unreadable or the wrong type left at its default."""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k not in DEFAULTS or _kind(v) == _kind(DEFAULTS[k])})
    except (OSError, ValueError):
        pass
    return cfg


_settings_lock = threading.Lock()


def save_settings(cfg):
    """Merge into the stored file. Retries the swap since antivirus can lock it."""
    with _settings_lock:
        merged = load_settings()
        merged.update(cfg)
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        for attempt in range(10):
            try:
                os.replace(tmp, CONFIG_FILE)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
        return merged


def screenshot(name):
    """Save the Roblox client to the app folder. Never raises."""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        Image.fromarray(grab(client_box((0.0, 0.0, 1.0, 1.0)))).save(os.path.join(APP_DIR, name))
    except Exception:
        pass


# ================================================================ Roblox's log
LOG_DIR = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "Roblox", "logs")
RPC_MARKER = "[BloxstrapRPC]"
_decoder = json.JSONDecoder()


def _presence(line):
    """The `data` of a SetRichPresence RPC line, or None."""
    at = line.find(RPC_MARKER)
    if at < 0:
        return None
    try:
        payload, _ = _decoder.raw_decode(line[at + len(RPC_MARKER):].strip())
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("command") != "SetRichPresence":
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def biome_from_line(line):
    data = _presence(line)
    if data is None:
        return None
    name = (data.get("largeImage") or {}).get("hoverText")
    return name.strip() if isinstance(name, str) and name.strip() else None


def aura_from_line(line):
    """The equipped aura (`Equipped "X"` or bare `Equipped _None_`), or None."""
    data = _presence(line)
    state = data.get("state") if data else None
    m = re.match(r'\s*Equipped\s+(?:"(.*)"|(\S+))\s*$', state) if isinstance(state, str) else None
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def time_from_line(line):
    """Epoch seconds from the line's leading timestamp, or None."""
    try:
        stamp = datetime.datetime.strptime(line[:24], "%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, TypeError):
        return None
    return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()


TRAY_MARK = b"AppState/TrayMode"    # tray icon's own player log
GAME_MARK = b"! Joining game"       # tray games keep using it
_tray = {}                          # path -> False (game log) or bytes read


def tray_log(path):
    """Tray-only log with no game in it yet?"""
    seen = _tray.get(path)
    if seen is False:
        return False
    try:
        with open(path, "rb") as f:
            if seen is None:
                head = f.read(8192)
                if TRAY_MARK not in head:
                    if len(head) == 8192:
                        _tray[path] = False
                    return False                        # too short to tell yet
                seen = 0
            f.seek(max(0, seen - len(GAME_MARK)))
            data = f.read()
            _tray[path] = False if GAME_MARK in data else f.tell()
    except OSError:
        return False
    return _tray[path] is not False


def log_dirs(log_dir=LOG_DIR):
    """Log folders, plus the Microsoft Store Roblox's."""
    if log_dir != LOG_DIR:
        return [log_dir]
    base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    return [LOG_DIR] + glob.glob(os.path.join(base, "Packages", "ROBLOXCORPORATION.ROBLOX_*", "LocalState", "logs"))


def _stat(path, field):
    try:
        return getattr(os.stat(path), field)
    except OSError:
        return 0.0


def player_logs(log_dir=LOG_DIR):
    """Game client logs, newest first."""
    found = []
    for d in log_dirs(log_dir):
        try:
            # skip the crash handler's log
            with os.scandir(d) as entries:
                found += [(e.stat().st_mtime, e.path) for e in entries
                          if e.name.endswith(".log") and "_Player_" in e.name and "CrashHandler" not in e.name]
        except OSError:
            continue
    return [p for _, p in sorted(found, key=lambda f: f[0], reverse=True) if not tray_log(p)]


def newest_log(log_dir=LOG_DIR):
    names = player_logs(log_dir)
    return names[0] if names else None


STUB_QUIET_S = 10        # quiet this long = dead launch stub


_joined = {}             # path -> True or bytes read


def joined_log(path):
    """Has this log joined a game?"""
    seen = _joined.get(path, 0)
    if seen is True:
        return True
    try:
        with open(path, "rb") as f:
            f.seek(max(0, seen - 64))
            data = f.read()
            _joined[path] = any(k.encode() in data for k in JOINED) or f.tell()
    except OSError:
        return False
    return _joined[path] is True


def moved_on(latest, path, joined=False):
    """Switch to a later-created `latest` (once joined, if `path` is), or once an unjoined `path` has gone quiet."""
    if latest is None or latest == path:
        return False
    if path is None or not os.path.exists(path):
        return True
    joined = joined or joined_log(path)
    if _stat(latest, "st_ctime") > _stat(path, "st_ctime"):
        return not joined or joined_log(latest)
    return not joined and time.time() - _stat(path, "st_mtime") > STUB_QUIET_S


def follow(stop, log_dir=LOG_DIR, poll=1.0):
    """Yield new lines from the newest client log, None on an idle poll. Old content is skipped."""
    path, handle, looked, began, sizes = None, None, 0.0, time.time(), {}
    try:
        while not stop():
            if handle is None or time.monotonic() - looked > 2.0:
                looked = time.monotonic()
                logs = player_logs(log_dir)
                latest = logs[0] if logs else None
                if moved_on(latest, path):
                    try:
                        new = open(latest, "r", encoding="utf-8", errors="replace")
                    except OSError:
                        new = None                      # locked, retry next listing
                    if new is not None:
                        if handle:
                            handle.close()
                        path, handle = latest, new
                        if path in sizes:               # seen: resume
                            handle.seek(min(sizes[path], _stat(path, "st_size")))
                        else:                           # new: read all (merchant on join)
                            handle.seek(0, os.SEEK_END if _stat(path, "st_ctime") < began else os.SEEK_SET)
                sizes = {n: _stat(n, "st_size") for n in logs}
            line = None
            if handle:
                at = handle.tell()
                line = handle.readline()
                if line and not line.endswith("\n"):     # half written, retry
                    handle.seek(at)
                    line = None
            yield line or None
            if not line:
                time.sleep(poll)
    finally:
        if handle:
            handle.close()


# ================================================================ window, keys and mouse
STEP_DELAY = 0.06
SETTLE_S = 0.3
PACE = 1.0               # PC speed: 0.6 fast to 2 potato
MOUSE_PACE = 1.0         # mouse travel scale
WAIT_FLOOR = 0.6         # shorter waits miss late drawing
LIMIT = 1.0              # show-wait scale, never under 1

u32 = ctypes.WinDLL('user32', use_last_error=True)
KEYUP, SCANCODE = 0x0002, 0x0008
VK = {'w': 0x57, 'a': 0x41, 's': 0x53, 'd': 0x44, 'q': 0x51, 'e': 0x45, 'f': 0x46,
      ' ': 0x20, 'shift': 0xA0, 'ctrl': 0xA2, 'r': 0x52, 'enter': 0x0D, 'esc': 0x1B,
      '\\': 0xDC, 'oem5': 0xDC, 'o': 0x4F}
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
KEY_SC = {'w': 0x11, 'a': 0x1E, 's': 0x1F, 'd': 0x20, 'q': 0x10, 'e': 0x12, 'f': 0x21, 'r': 0x13,
          ' ': 0x39, 'shift': 0x2A, 'ctrl': 0x1D, 'enter': 0x1C, 'esc': 0x01, 'o': 0x18}


class KI(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MI(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class IN(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KI), ("mi", MI)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
u32.GetWindowThreadProcessId.restype = wintypes.DWORD
u32.GetForegroundWindow.restype = wintypes.HWND     # match mousekey's handles
u32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
u32.GetKeyboardLayout.restype = wintypes.HKL
u32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HKL]
u32.MapVirtualKeyExW.restype = wintypes.UINT
u32.VkKeyScanExW.argtypes = [wintypes.WCHAR, wintypes.HKL]
u32.VkKeyScanExW.restype = ctypes.c_short


class NotFocused(RuntimeError):
    pass


def paced(s):
    _sleep(s * PACE)


def held(s):
    """For the game's own animations: never shorter than Normal."""
    _sleep(s * LIMIT)


def set_pace(base):
    global PACE, LIMIT, MOUSE_PACE
    PACE, MOUSE_PACE = max(WAIT_FLOOR, base), base
    LIMIT = max(1.0, PACE)


def fresh_session():
    """Reset per-run state on Start."""
    global _window, _said_refused
    _window = (0.0, None)
    _said_refused = False


kb = KeyboardController()
mkey = MouseKey()


def _layout():
    """Keyboard layout of the front window."""
    try:
        return u32.GetKeyboardLayout(u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None))
    except Exception:
        return 0


def in_front():
    """Is Roblox in front? By handle: the Store version's window belongs to ApplicationFrameHost."""
    try:
        w = roblox_window()
        return w is not None and foreground_hwnd() == w.hwnd
    except Exception:
        return False


FRONT_EVERY = 2.0
_took_front = 0.0


def wait_for_front(grace=3.0, take=True):
    """Make sure Roblox is in front before a key goes out, pulling it forward if needed."""
    global _took_front
    end = time.monotonic() + grace
    while not in_front():
        if time.monotonic() > end:
            # don't fight the user for focus
            if take and time.monotonic() - _took_front > FRONT_EVERY:
                _took_front = time.monotonic()
                try:
                    focus_roblox(verify=False)
                except RuntimeError:
                    pass
                if in_front() or click_to_front():
                    return
            raise NotFocused('Roblox is not the window in front; key not sent')
        time.sleep(0.05)


INPUT_LOCK = threading.RLock()     # keeps the clip key combo whole


def _send(vk, flags, sc=None):
    if not flags & KEYUP:
        wait_for_front()
    sc = sc or u32.MapVirtualKeyExW(vk, 0, _layout()) or u32.MapVirtualKeyW(vk, 0)
    inp = IN(type=1, ki=KI(wVk=0, wScan=sc, dwFlags=flags | SCANCODE, time=0, dwExtraInfo=0))
    with INPUT_LOCK:
        sent = u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    if not sent:
        raise ctypes.WinError(ctypes.get_last_error())


_down = set()


def down(key):
    _send(VK[key], 0, KEY_SC.get(key))
    _down.add(key)


def up(key):
    _send(VK[key], KEYUP, KEY_SC.get(key))
    _down.discard(key)


STUCK_VK = (0x5B, 0x5C, 0x12, 0xA4, 0xA5, 0x79, 0x09, 0x25, 0x26, 0x27, 0x28, 0xA0, 0xA1, 0xA2, 0xA3)
# never sent, lifted if stuck (stuck Win opens Start)


WIN_VKS = (0x5B, 0x5C)
MASK_VK = 0xE8              # dead key before Win up keeps Start shut


def _key(vk, up):
    u32.SendInput(1, ctypes.byref(IN(type=1, ki=KI(wVk=vk, wScan=0, dwFlags=KEYUP if up else 0,
                                                   time=0, dwExtraInfo=0))), ctypes.sizeof(IN))


def mask_win():
    _key(MASK_VK, False)
    _key(MASK_VK, True)


def _lift_stuck():
    for vk in STUCK_VK:
        try:
            if u32.GetAsyncKeyState(vk) & 0x8000:
                if vk in WIN_VKS:
                    print('WARN win_key_stuck released=true masked=true')
                    mask_win()
                _key(vk, True)
        except OSError:
            pass


def release_all(focus=True):
    """All keys up, focusing Roblox first if any are held."""
    if focus and _down:
        try:
            focus_roblox(verify=False)
            time.sleep(0.15)
        except Exception:
            pass
    for key, vk in VK.items():
        try:
            _send(vk, KEYUP, KEY_SC.get(key))
        except OSError:
            pass
        time.sleep(0.005)
    _down.clear()
    _lift_stuck()


_armed = False


def arm():
    """Release every key however the program ends."""
    global _armed
    if _armed:
        return
    atexit.register(release_all)
    _armed = True


def disarm():
    global _armed
    _armed = False
    try:
        atexit.unregister(release_all)
    except Exception:
        pass


def by_key(key):
    """A letter as its key, whatever keyboard layout is on (else Ctrl+V types a lone 'v')."""
    if isinstance(key, str) and len(key) == 1 and key.isascii() and key.isalpha():
        return KeyCode.from_vk(ord(key.upper()))
    return key


def tap(key, hold=None, gap=None):
    key = by_key(key)
    wait_for_front()
    with INPUT_LOCK:
        kb.press(key)
        paced(0.03 if hold is None else hold)
        kb.release(key)
    paced(STEP_DELAY if gap is None else gap)


MOVE_ABS = 0x0001 | 0x8000 | 0x4000        # absolute, whole desktop
BTN_DOWN = {'left': 0x0002, 'right': 0x0008, 'middle': 0x0020}
BTN_UP = {'left': 0x0004, 'right': 0x0010, 'middle': 0x0040}
_said_refused = _said_locked = False


def _mouse(flags, data=0, dx=0, dy=0):
    """One mouse event. False if Windows refused it."""
    global _said_refused, _said_locked
    inp = IN(type=0, mi=MI(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0))
    with INPUT_LOCK:
        sent = u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    if sent:
        _said_locked = False
        return True
    if not u32.GetForegroundWindow():                       # locked or asleep: no input
        if not _said_locked:
            _said_locked = True
            print('WARN mouse_refused reason="screen locked or asleep"')
        return False
    if not _said_refused:
        err = ctypes.get_last_error()
        _said_refused = True
        print('WARN mouse_refused error=%d%s' % (
            err, ' roblox_admin=true' if err == 5 else ''))
    return False


def to_abs(x, y):
    """Screen pixel to SendInput's 0-65535 coords."""
    vx, vy, vw, vh = in_dpi(REAL, lambda: [u32.GetSystemMetrics(i) for i in (76, 77, 78, 79)])
    return round((x - vx) * 65535 / max(1, vw - 1)), round((y - vy) * 65535 / max(1, vh - 1))


def cursor():
    p = wintypes.POINT()
    in_dpi(REAL, u32.GetCursorPos, ctypes.byref(p))
    return p.x, p.y


MOVE_DELAY_S = 0.15


def move_to(x, y, settle=MOVE_DELAY_S):
    """Move the pointer, then wait `settle` so Roblox notices."""
    dx, dy = to_abs(x, y)
    ok = _mouse(MOVE_ABS, dx=dx, dy=dy)
    if settle:
        time.sleep(settle)
    return ok


def button(name, down):
    """Press or release a mouse button; presses carry the position, releases don't (camera drags)."""
    if u32.GetSystemMetrics(23):                   # left-handed mouse
        name = {'left': 'right', 'right': 'left'}.get(name, name)
    if not down:
        return _mouse(BTN_UP[name])
    dx, dy = to_abs(*cursor())
    return _mouse(MOVE_ABS | BTN_DOWN[name], dx=dx, dy=dy)


REAL = -4           # per-monitor v2: real pixels

try:
    for _f in (u32.SetThreadDpiAwarenessContext, u32.GetThreadDpiAwarenessContext, u32.GetWindowDpiAwarenessContext):
        _f.restype = ctypes.c_void_p
    u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    u32.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
    u32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    u32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
except AttributeError:      # Windows 10 before 1607
    pass


def in_dpi(context, f, *args):
    """Run f(*args) with this thread in a DPI context (REAL or a window's own)."""
    old = None
    try:
        old = u32.SetThreadDpiAwarenessContext(context) or (context == REAL and u32.SetThreadDpiAwarenessContext(-3))
    except (AttributeError, ctypes.ArgumentError):
        pass
    try:
        return f(*args)
    finally:
        if old:
            u32.SetThreadDpiAwarenessContext(old)


def window_context(hwnd):
    """The DPI context a window lays itself out in, or None."""
    try:
        return u32.GetWindowDpiAwarenessContext(hwnd)
    except (AttributeError, ctypes.ArgumentError):
        return None


def awareness(hwnd=None):
    """DPI awareness of a window (or this thread), for the log."""
    try:
        c = u32.GetWindowDpiAwarenessContext(hwnd) if hwnd else u32.GetThreadDpiAwarenessContext()
        if u32.AreDpiAwarenessContextsEqual(c, REAL):
            return 'per monitor v2'
        return {0: 'DPI unaware', 1: 'system DPI aware', 2: 'per monitor'}.get(
            u32.GetAwarenessFromDpiAwarenessContext(c), '?')
    except (AttributeError, ctypes.ArgumentError):
        return '?'


def _view(hwnd):
    """Client x, y, w, h on screen, then window left, top, width."""
    h, c, w, p = wintypes.HWND(hwnd), _RECT(), _RECT(), wintypes.POINT(0, 0)
    u32.GetClientRect(h, ctypes.byref(c))
    u32.GetWindowRect(h, ctypes.byref(w))
    u32.ClientToScreen(h, ctypes.byref(p))
    return p.x, p.y, c.right - c.left, c.bottom - c.top, w.left, w.top, w.right - w.left


def on_screen(own, real):
    """A _view in a window's own pixels to a real-pixel rect, plus the scale (not 1 if Windows stretches Roblox)."""
    x, y, cw, ch, left, top, ww = own
    s = real[6] / ww if ww > 0 and real[6] > 0 else 1.0
    return (round(real[4] + (x - left) * s), round(real[5] + (y - top) * s), round(cw * s), round(ch * s)), s


def _client(hwnd):
    return on_screen(in_dpi(window_context(hwnd), _view, hwnd), in_dpi(REAL, _view, hwnd))


def client_rect(window):
    """Client (left, top, width, height) in screen pixels."""
    return _client(window.hwnd)[0]


def window_scale(window):
    """Screen pixels per Roblox pixel."""
    return _client(window.hwnd)[1]


CLIENT_EXES = {'robloxplayerbeta.exe', 'robloxplayerlauncher.exe', 'robloxplayerinstaller.exe', 'robloxcrashhandler.exe'}


def mine(pid):
    """Is this pid in our Windows session? RDP and other accounts share the process list."""
    k, a, b = ctypes.windll.kernel32, wintypes.DWORD(), wintypes.DWORD()
    try:
        return (bool(k.ProcessIdToSessionId(os.getpid(), ctypes.byref(a)))
                and bool(k.ProcessIdToSessionId(int(pid), ctypes.byref(b))) and a.value == b.value)
    except (TypeError, ValueError):
        return False


def find_roblox_window():
    pids = set()
    for process in psutil.process_iter(['name']):
        try:
            if ((process.info['name'] or '').lower() in CLIENT_EXES and process.pid != os.getpid()
                    and mine(process.pid)):
                pids.add(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    windows = mkey.get_all_windows()
    big = lambda w: min(w.dim_win) >= 200 or _minimised(w)
    candidates = [w for w in windows if w.pid in pids and w.status == 'visible' and big(w)]
    if not candidates:     # Store Roblox: match window text
        candidates = [w for w in windows if w.status == 'visible' and (w.windowtext or '').strip() == 'Roblox' and big(w)]
    if not candidates:
        _why_no_window(pids, windows)
        return None
    exact = [w for w in candidates if w.class_name == 'WINDOWSCLIENT']
    return max(exact or candidates, key=lambda w: w.dim_win[0] * w.dim_win[1])


_told_no_window = 0.0


def _why_no_window(pids, windows):
    """Log why no Roblox window was found, at most once a minute."""
    global _told_no_window
    if time.monotonic() - _told_no_window < 60:
        return
    _told_no_window = time.monotonic()
    others = []
    for process in psutil.process_iter(['name']):
        try:
            if 'roblox' in (process.info['name'] or '').lower() and process.pid not in pids:
                others.append('%s %d' % (process.info['name'], process.pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    seen = ['%s %r %s %s' % (w.class_name, w.windowtext, w.status, w.dim_win) for w in windows if w.pid in pids]
    print('WARN window_missing client_pids=%s client_windows=%s other_roblox=%s'
          % (sorted(pids) or 'none', seen or 'none', others or 'none'))


def _minimised(w):
    try:
        return bool(ctypes.windll.user32.IsIconic(w.hwnd))
    except Exception:
        return False


def foreground_hwnd():
    return u32.GetForegroundWindow() or 0


def force_foreground(hwnd):
    """Bring hwnd to the front. Never AttachThreadInput: it can freeze both programs."""
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    if foreground_hwnd() == hwnd:
        return True
    user32.SwitchToThisWindow(hwnd, True)          # what alt-tab uses
    return foreground_hwnd() == hwnd


def focus_roblox(verify=True):
    global _window
    window = find_roblox_window()
    if window is None:
        raise RuntimeError('No Roblox game window. Is it running and not minimised?')
    _window = (time.monotonic(), window)
    if foreground_hwnd() == window.hwnd:
        return window
    force_foreground(window.hwnd)
    paced(SETTLE_S)
    if verify and foreground_hwnd() != window.hwnd:
        raise RuntimeError('Roblox would not come forward (foreground is %s, wanted %s)'
                           % (foreground_hwnd(), window.hwnd))
    return window


def take_foreground(tries=5, gap=0.35):
    """Get Roblox in front, retrying; the window, or None."""
    last = None
    for _ in range(max(1, tries)):
        try:
            return focus_roblox(verify=True)
        except RuntimeError as e:
            last = e
            paced(gap)
    if click_to_front():
        return roblox_window()
    print('WARN focus_failed tries=%d error="%s"' % (max(1, tries), last))
    return None


FRONT_CLICK = 'merchantShopCloseButton'     # dead spot, top right
FRONT_CLICK_EVERY = 5.0
_clicked_front = 0.0


def _window_at(x, y):
    """The top-level window under a screen pixel (0 if none)."""
    u = ctypes.windll.user32
    u.WindowFromPoint.restype = u.GetAncestor.restype = ctypes.c_void_p
    hit = in_dpi(REAL, u.WindowFromPoint, wintypes.POINT(x, y))
    return (u.GetAncestor(ctypes.c_void_p(hit), 2) or 0) if hit else 0     # GA_ROOT


def click_to_front():
    """Click a dead spot on Roblox to bring it forward, if nothing covers it. True if it worked."""
    global _clicked_front
    if time.monotonic() - _clicked_front < FRONT_CLICK_EVERY:
        return False
    _clicked_front = time.monotonic()
    w = find_roblox_window()
    if w is None:
        return False
    try:
        import points
        at = points.centre(FRONT_CLICK)
    except Exception:
        at = None
    if at is None:
        return False
    x, y = int(at[0]), int(at[1])
    if _window_at(x, y) != w.hwnd:
        print('WARN front_click_skipped reason="spot covered"')
        return False
    print('front_click spot="merchant shop X" x=%d y=%d n=2' % (x, y))
    glide(x, y)
    for _ in range(2):
        button('left', True)
        time.sleep(0.05)
        button('left', False)
        time.sleep(0.2)
    try:
        focus_roblox(verify=False)
    except RuntimeError:
        pass
    return in_front()


def steady_front(hold=0.6, tries=5):
    """Roblox in front and staying there for `hold` s. False if it never sticks."""
    for _ in range(max(1, tries)):
        if take_foreground(tries=2) is None:
            continue
        end = time.monotonic() + hold * LIMIT
        while in_front():
            if time.monotonic() > end:
                return True
            time.sleep(0.05)
    return False


# ================================================================ the screen
INVENTORY_TITLE = (0.46, 0.262, 0.54, 0.29)
INVENTORY_X = (0.7285, 0.2639, 0.7426, 0.2889)


_window = (0.0, None)


def roblox_window():
    """Cached Roblox window, looked up at most once a second (the search is slow)."""
    global _window
    if time.monotonic() - _window[0] > 1.0 or _window[1] is None:
        _window = (time.monotonic(), find_roblox_window())
    return _window[1]


def client_box(fractions):
    """A rectangle given as fractions of the Roblox client, in screen pixels."""
    try:
        w = roblox_window()
    except Exception:
        w = None
    if w is None:
        cw, ch, x0, y0 = 2560, 1440, 0, 0
    else:
        x0, y0, cw, ch = client_rect(w)
    l, t, r, b = fractions
    box = (int(x0 + l * cw), int(y0 + t * ch), int(x0 + r * cw), int(y0 + b * ch))
    return box[0], box[1], max(box[0] + 1, box[2]), max(box[1] + 1, box[3])


def area_scale():
    l, t, r, b = client_box((0.0, 0.0, 1.0, 1.0))
    return max(0.05, (r - l) * (b - t) / (2560.0 * 1440.0))


class _BMI(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('w', wintypes.LONG), ('h', wintypes.LONG), ('planes', wintypes.WORD),
                ('bits', wintypes.WORD), ('compression', wintypes.DWORD), ('rest', wintypes.DWORD * 5),
                ('colours', wintypes.DWORD * 3)]


def _grab_rect(box):
    """Grab just this rectangle; grabbing every monitor and cropping is much slower."""
    g, u = ctypes.windll.gdi32, ctypes.windll.user32
    w, h = box[2] - box[0], box[3] - box[1]
    for f, res, args in ((u.GetDC, wintypes.HDC, [wintypes.HWND]), (g.CreateCompatibleDC, wintypes.HDC, [wintypes.HDC]),
                         (g.CreateCompatibleBitmap, wintypes.HBITMAP, [wintypes.HDC, ctypes.c_int, ctypes.c_int]),
                         (g.SelectObject, wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ])):
        f.restype, f.argtypes = res, args
    g.BitBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT]
    g.DeleteObject.argtypes, g.DeleteDC.argtypes = [wintypes.HGDIOBJ], [wintypes.HDC]
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    screen = u.GetDC(None)
    mem = g.CreateCompatibleDC(screen)
    bmp = g.CreateCompatibleBitmap(screen, w, h)
    try:
        g.SelectObject(mem, bmp)
        if not g.BitBlt(mem, 0, 0, w, h, screen, box[0], box[1], 0x00CC0020):
            raise OSError('BitBlt failed')
        bmi = _BMI(size=40, w=w, h=-h, planes=1, bits=32)
        buf = np.empty((h, w, 4), np.uint8)
        if g.GetDIBits(mem, bmp, 0, h, buf.ctypes.data, ctypes.byref(bmi), 0) != h:
            raise OSError('GetDIBits failed')
        return buf[..., 2::-1]
    finally:
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None, screen)


def grab(box):
    """A screen rectangle as RGB; dark if the grab fails."""
    try:
        return in_dpi(REAL, _grab_rect, box)
    except Exception:
        pass
    try:
        return np.asarray(ImageGrab.grab(bbox=box, all_screens=True).convert('RGB'))
    except Exception:
        return np.zeros((max(1, box[3] - box[1]), max(1, box[2] - box[0]), 3), np.uint8)


def hsv(rgb):
    """H 0..179, S and V 0..255."""
    a = rgb.astype(np.int32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    v = a.max(axis=-1)
    c = v - a.min(axis=-1)
    safe = np.where(c == 0, 1, c)
    h = np.where(v == r, 30 * (g - b) / safe,
                 np.where(v == g, 60 + 30 * (b - r) / safe, 120 + 30 * (r - g) / safe))
    h = np.where(c == 0, 0, h) % 180
    s = np.where(v == 0, 0, 255 * c / np.where(v == 0, 1, v))
    return h, s, v


def window_part(box, w, h):
    """Inventory window part (16:9 fractions) in w x h client pixels; the window is centred, half the width."""
    y = lambda f: int(round(h / 2.0 + (f - 0.5) * w * 9.0 / 16.0))
    return int(round(box[0] * w)), y(box[1]), int(round(box[2] * w)), y(box[3])


def window_box(box):
    """Same, in screen pixels."""
    l, t, r, b = client_box((0.0, 0.0, 1.0, 1.0))
    x0, y0, x1, y1 = window_part(box, r - l, b - t)
    return l + x0, t + y0, l + x1, t + y1


WINDOW_TOP = 0.2556      # top edge on a 16:9 client


def inventory_open(rgb=None):
    """Is the Inventory / Aura Storage up? White title and X on darker, plus the window's top edge."""

    if rgb is None:
        rgb = grab(client_box((0.0, 0.0, 1.0, 1.0)))
    h, w = rgb.shape[:2]

    def lettering(box):
        x0, y0, x1, y1 = window_part(box, w, h)
        c = rgb[max(0, y0):y1, x0:x1].astype(np.int16)
        if not c.size:
            return 0.0, 255.0
        white = (c.min(-1) > 185) & (c.max(-1) - c.min(-1) < 40)
        rest = c.mean(-1)[~white]
        return float(white.mean()), float(rest.mean()) if rest.size else 255.0

    title, behind = lettering(INVENTORY_TITLE)
    cross, _ = lettering(INVENTORY_X)
    if not (0.06 <= title <= 0.5 and behind < 120 and 0.02 <= cross <= 0.5):
        return False
    top = window_part((0, WINDOW_TOP, 0, WINDOW_TOP), w, h)[1]
    g = rgb[:, int(0.27 * w):int(0.73 * w)].mean(-1)
    d = max(2, int(round(w / 512.0)))
    edge = max((float((g[y] - g[y + 3 * d] >= 20).mean()) for y in range(top - 3 * d, top + 3 * d + 1)
                if 0 <= y < h - 3 * d), default=0.0)
    return edge >= 0.7


def _group(mask, origin, row_gap, col_gap, min_area):
    out = []
    rows = np.flatnonzero(mask.any(axis=1))
    if not rows.size:
        return out
    for r0, r1 in runs(rows, row_gap):
        band = mask[r0:r1 + 1]
        for c0, c1 in runs(np.flatnonzero(band.any(axis=0)), col_gap):
            area = int(band[:, c0:c1 + 1].sum())
            if area >= min_area:
                ys, xs = np.nonzero(band[:, c0:c1 + 1])
                out.append((origin[0] + c0 + xs.mean(), origin[1] + r0 + ys.mean(), area))
    out.sort(key=lambda b: -b[2])
    return out


def runs(idx, gap):
    """Split sorted indices wherever they jump by more than gap."""
    cuts = np.flatnonzero(np.diff(idx) > gap)
    start = 0
    for c in cuts:
        yield int(idx[start]), int(idx[c])
        start = c + 1
    yield int(idx[start]), int(idx[-1])


def move_path(start, end, jitter=3.0, most=40):
    """Slightly bowed pointer path to near the target: speeds up, then about half the time goes on the last 20%."""
    sx, sy = start
    tx = end[0] + random.uniform(-jitter, jitter)
    ty = end[1] + random.uniform(-jitter, jitter)
    dist = math.hypot(tx - sx, ty - sy)
    steps = max(min(8, most), min(most, int(dist / 18)))
    bow = random.uniform(-1, 1) * dist * 0.06
    nx, ny = (-(ty - sy) / dist, (tx - sx) / dist) if dist else (0.0, 0.0)
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - (1 - t) ** 4 * (1 + 4 * t)          # fast start, long slow finish
        k = math.sin(math.pi * e) * bow
        out.append((sx + (tx - sx) * e + nx * k + random.uniform(-0.6, 0.6),
                    sy + (ty - sy) * e + ny * k + random.uniform(-0.6, 0.6)))
    out[-1] = (tx, ty)
    return out


GLIDE_S = 0.3


def glide(x, y, settle=MOVE_DELAY_S, travel=GLIDE_S):
    path = move_path(cursor(), (x, y), most=12)
    step = travel * min(PACE, MOUSE_PACE) / len(path)
    for i, (px, py) in enumerate(path):
        last = i == len(path) - 1
        move_to(int(round(px)), int(round(py)), settle=settle if last else step * random.uniform(0.8, 1.2))


def click(x, y, park=True, settle=MOVE_DELAY_S, travel=GLIDE_S):
    """Focus Roblox, glide there, click. Nothing is sent if Roblox won't come forward."""
    if take_foreground() is None:
        return False
    glide(x, y, settle, travel)                                   # button needs the hover
    if not button('left', True):
        return False
    time.sleep(0.03 * LIMIT)
    button('left', False)
    if park:
        time.sleep(0.1 * LIMIT)
        glide(int(x + random.uniform(-40, 40)), int(y + random.uniform(140, 200)))
    return True


def close_guis():
    """Shut the Inventory if it was left open."""
    if ABORT or not steady_front():
        return False
    shut_by_sight()
    return True


def shut_by_sight():
    """close_inventory that never raises."""
    try:
        close_inventory()
    except Exception:
        pass


def close_inventory():
    """Close the Inventory by its X if it's up. True if it was."""
    global _side_open
    _side_open = None
    if take_foreground() is None or not inventory_open():
        return False
    b = window_box(INVENTORY_X)
    print('inventory_close')
    for _ in range(3):
        click((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        time.sleep(0.5)
        if not inventory_open():
            return True
    print('WARN inventory_close_failed tries=3')
    return True


# ================================================================ staying connected
QUIET_S = 240.0          # no log line this long: frozen
JOIN_S = 150.0
JOIN_SLOW_S = 300.0      # some clients take minutes to join
START_S = 60.0           # no Roblox this long: retry
READY_S = 90.0
JOIN_SETTLE_S = 8.0
PLAY_POINT = (("play", "Play button on the title screen"),)
# built-in spots; a calibrated one wins
BUILT_IN = {'play': 'loadingPlayButton', 'collection': 'mainInterfaceCollection',
            'exit_collection': 'collectionLeftExitButton', 'inventory': 'mainInterfaceInventory',
            'items': 'inventoryItemsTab', 'search': 'inventorySearchContent', 'slot1': 'inventoryInvItem[1,1]',
            'slot2': 'inventoryInvItem[1,2]', 'use': 'inventoryUseButton', 'aura_storage': 'mainInterfaceAuraStorage'}
PLAY_PRESSES = 2
PLAY_SETTLE_S = 5.0
PLAY_AFTER_S = 45.0     # after opening the server
CLOSED_SETTLE_S = 2.0
PLAY_BOX = (0.0, 0.72, 0.4, 1.0)     # bottom left
PLAY_RGB = (0x7e, 0xff, 0x91)
PLAY_TOL = 30                        # red/blue drift
LOST = ('Lost connection with reason', 'Client has been disconnected with reason',
        'Disconnection Notification', 'ID_CONNECTION_LOST', 'leaveUGCGameInternal')
JOINED = ('Connection accepted from', 'Replicator created for player')
GAME_OUTPUT = '[FLog::Creator'
FROZEN = 'frozen'
REJOIN_GRACE_S = 30.0   # let the game rejoin first
def tidy_link(text):
    """Cut a pasted link's code where it stops ('...136155Roblox' has a word stuck on)."""
    text = re.sub(r'(privateServerLinkCode=\d+)[^\d&#\s]+', r'\1', (text or '').strip(), flags=re.I)
    return re.sub(r'(code=[a-fA-F0-9]{32})[^&#\s]+', r'\1', text, flags=re.I)


def deeplink(text):
    """roblox:// link for a game or private server URL, or None. A private link never falls back to public."""
    m = re.search(r'https?://\S+', text or '')
    if not m:
        return None
    parts = urlsplit(m.group(0))
    host = (parts.hostname or '').lower()
    if host != 'roblox.com' and not host.endswith('.roblox.com'):
        return None
    query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
    path = [seg for seg in parts.path.split('/') if seg]
    if path and path[0].lower() == 'share':
        code = query.get('code', '')
        if re.fullmatch(r'[a-fA-F0-9]{32}', code) and query.get('type', '').lower() == 'server':
            return 'roblox://navigation/share_links?code=%s&type=Server' % code
        return None
    for i, seg in enumerate(path[:2]):
        if seg.lower() in ('games', 'game-places') and i + 1 < len(path) and path[i + 1].isdigit():
            code = query.get('privateserverlinkcode')
            if code is None:
                return 'roblox://placeId=%s' % path[i + 1]
            return 'roblox://placeId=%s&linkCode=%s' % (path[i + 1], code) if re.fullmatch(r'[A-Za-z0-9]+', code) else None
    return None


def _lost_reason(line):
    m = re.search(r'reason\s*:?\s*(.+)$', line.strip(), re.I)
    return (m.group(1) if m else line.strip())[-120:]


class Watch:
    """Follows the newest client log. `state` is 'joined', 'lost' or None."""

    def __init__(self, log_dir=None):
        self.log_dir = log_dir or LOG_DIR
        self.path, self.pos, self.tail = None, 0, ''
        self.state, self.why = None, ''
        self.joined = False                   # joined a game
        self.aura = None
        self.in_menu = False                  # title screen
        self.menu_at = 0.0
        self.fresh = time.monotonic()
        self.dropped = None                   # sticky until trouble() asks
        self._lock = threading.RLock()        # polled from two threads

    def poll(self, now=None):
        with self._lock:
            return self._poll(now)

    def _poll(self, now=None):
        now = time.time() if now is None else now
        newest = newest_log(self.log_dir)
        if newest is None:
            return self.state
        old = False
        if moved_on(newest, self.path, self.joined):
            if self.path is not None:
                self.dropped = 'the client started again'
            old = self.path is None               # first log: old drops are history
            self.path, self.pos, self.tail, self.joined = newest, 0, '', False
            self.state, self.why = None, ''
            self.aura, self.in_menu = None, False
            self.fresh = time.monotonic()
        try:
            size = os.path.getsize(self.path)
            if size < self.pos:
                self.pos, self.tail = 0, ''
            if size > self.pos:
                with open(self.path, 'rb') as f:
                    f.seek(self.pos)
                    data = f.read()
                    self.pos = f.tell()
                self.fresh = time.monotonic()
                lines = (self.tail + data.decode('utf-8', 'replace')).split('\n')
                self.tail = lines.pop()
                for line in lines:
                    if RPC_MARKER in line:
                        aura = aura_from_line(line)
                        if aura is not None:            # _None_ = nothing equipped
                            self.in_menu = False
                        elif 'In Main Menu' in line:
                            self.in_menu = True
                            self.menu_at = time_from_line(line) or now
                        if aura is not None and aura != self.aura:
                            self.aura = aura
                    if GAME_OUTPUT in line:         # skip the game's own lines
                        continue
                    if any(k in line for k in LOST):
                        if self.state != 'lost':
                            self.state, self.why = 'lost', _lost_reason(line)
                        if not old:
                            self.dropped = self.why
                    elif any(k in line for k in JOINED):
                        self.state, self.why, self.joined = 'joined', '', True
        except OSError:
            pass
        return self.state

    def quiet_for(self):
        return time.monotonic() - self.fresh

    def trouble(self, quiet_s=None, check_process=True):
        """Why the client needs rejoining, or None."""
        quiet_s = QUIET_S if quiet_s is None else quiet_s
        if check_process and roblox_gone():
            return 'Roblox is not running'
        if check_process and roblox_hung():
            return '%s: Roblox not responding for %d s' % (FROZEN, HUNG_S)
        if self.poll() == 'lost':
            self.dropped = None
            return 'disconnected (%s)' % self.why
        if self.dropped:                          # a blip still counts
            why, self.dropped = self.dropped, None
            return 'disconnected (%s) and came back by itself' % why
        if self.path and self.quiet_for() > quiet_s:
            return '%s: no new Roblox log lines for %d minutes' % (FROZEN, quiet_s // 60)
        return None


GONE_S = 15.0
_window_seen = 0.0


def roblox_running():
    """Is a game window up? (The tray icon is the same exe.)"""
    global _window_seen
    try:
        up = roblox_window() is not None
    except Exception:
        up = False
    if up:
        _window_seen = time.monotonic()
    return up


HUNG_S = 60.0
_hung_since = None


def roblox_hung():
    """Not responding for HUNG_S in a row?"""
    global _hung_since
    try:
        w = roblox_window()
        hung = w is not None and bool(ctypes.windll.user32.IsHungAppWindow(w.hwnd))
    except Exception:
        hung = False
    if not hung:
        _hung_since = None
        return False
    if _hung_since is None:
        _hung_since = time.monotonic()
    return time.monotonic() - _hung_since >= HUNG_S


def roblox_gone():
    """Closed for sure. One look can miss the window while a rejoin redraws it."""
    return not roblox_running() and time.monotonic() - _window_seen > GONE_S


def play_mask(rgb):
    """Play-green (#7eff91) pixels, down to about 60% brightness."""
    a = rgb.astype(np.int32)
    g = a[..., 1]
    k = g / 255.0
    return ((g >= 150) & (np.abs(a[..., 0] - PLAY_RGB[0] * k) <= PLAY_TOL)
            & (np.abs(a[..., 2] - PLAY_RGB[2] * k) <= PLAY_TOL))


def find_play():
    """Play button centre on the title screen, or None; on Play's box with little green above (grass is green too)."""
    import points
    button = points.box('loadingPlayButton')
    if button is None:
        return None
    box = client_box(PLAY_BOX)
    mask = play_mask(grab(box))
    found = _group(mask, (box[0], box[1]), 8, 40, max(20, 150 * area_scale()))
    if not found:
        return None
    x, y, area = found[0]
    l, t, r, b = button
    h = b - t
    if not (l - h <= x <= r + h and t - h / 2 <= y <= b + h / 2) or area > 1.3 * (r - l) * h:
        return None
    inside = mask[max(0, t - box[1]):max(0, b - box[1]), max(0, l - box[0]):max(0, r - box[0])]
    above = mask[max(0, t - 3 * h - box[1]):max(0, t - h // 2 - box[1]), max(0, l - box[0]):max(0, r - box[0])]
    if not inside.size or (above.size and above.mean() * 3 > inside.mean()):
        return None
    return int(x), int(y)


MENU_S = 90.0            # some clients never log the aura after Play


def in_game(watch):
    """In game with a real aura? (_None_ shows on the title screen too.)"""
    on_menu = watch.in_menu and time.time() - getattr(watch, 'menu_at', time.time()) < MENU_S
    return watch.aura not in (None, '_None_') and not on_menu


def calibrated_play(watch, due, say=lambda m: None):
    """Press Play once `due` passes, unless already in game. Returns (next due or None, pressed)."""
    if due is None or time.monotonic() < due:
        return due, False
    if not in_game(watch):
        say('play_press spot=%s' % ('"Set Play"' if 'play' in load_points() else 'built_in'))
        return None, press_point('play', settle=1.0)
    return None, False


def press_play(watch, stop=lambda: False, say=lambda m: None, timeout=90.0, play_due=None, presses=0):
    """Click Play while it's up, at most PLAY_PRESSES times. True once the game started."""
    end, gone, saved = time.monotonic() + timeout, 0, False
    while time.monotonic() < end and not stop():
        watch.poll()
        play_due, pressed = calibrated_play(watch, play_due, say)
        presses += pressed
        at = find_play() if presses < PLAY_PRESSES else None
        if at is not None:
            gone = 0
            say('play_press spot=seen x=%d y=%d attempt=%d/%d' % (at[0], at[1], presses + 1, PLAY_PRESSES))
            click(*at)
            presses += 1
            time.sleep(PLAY_SETTLE_S)
            if find_play() is None:
                return True
            continue
        else:
            gone += 1
            if in_game(watch) or (watch.aura is not None and not watch.in_menu and gone >= 3):
                return True
            if watch.in_menu and not saved:
                saved = True
                screenshot('title_screen.png')
        if presses >= PLAY_PRESSES:
            say('WARN play_assumed_started presses=%d' % presses)
            return True
        time.sleep(3.0)
    return in_game(watch)


def roblox_processes():
    """Roblox client processes in this session, by exact exe name."""
    out = []
    for p in psutil.process_iter(['name']):
        try:
            if (p.info['name'] or '').lower() in CLIENT_EXES and p.pid != os.getpid() and mine(p.pid):
                out.append(p)
        except psutil.Error:
            continue
    return out


def roblox_starting():
    """Any Roblox exe of ours running: client, launcher or installer."""
    for p in psutil.process_iter(['name']):
        try:
            if (p.info['name'] or '').lower().startswith('roblox') and mine(p.pid):
                return True
        except psutil.Error:
            continue
    return False


def roblox_elevated():
    """Is Roblox running as admin while we aren't (Windows then drops our input)?"""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    for p in roblox_processes():
        h = k32.OpenProcess(0x1000, False, p.pid)               # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            token = wintypes.HANDLE()
            if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(token)):      # TOKEN_QUERY
                if ctypes.get_last_error() == 5:
                    return True
                continue
            try:
                elevated, size = wintypes.DWORD(), wintypes.DWORD()
                if adv.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size)) and elevated.value:
                    return True                                 # TokenElevation
            finally:
                k32.CloseHandle(token)
        finally:
            k32.CloseHandle(h)
    return False


def close_roblox(wait=15.0):
    """Kill Roblox clients until the game window is gone (the tray icon can respawn). True once it is."""
    end = time.monotonic() + wait
    while True:
        left = roblox_processes()
        if not left:
            return True
        for p in left:
            try:
                p.kill()
            except psutil.Error:
                pass
        time.sleep(0.5)
        if not roblox_running():
            return True
        if time.monotonic() >= end:
            return False


BROWSERS = {'chrome.exe', 'msedge.exe', 'brave.exe', 'opera.exe', 'firefox.exe', 'vivaldi.exe', 'arc.exe',
            'chromium.exe', 'librewolf.exe', 'waterfox.exe', 'floorp.exe', 'zen.exe', 'thorium.exe', 'browser.exe'}


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def browser_windows():
    """{hwnd: title} of every visible browser window."""
    pids = set()
    for p in psutil.process_iter(['name']):
        try:
            if (p.info['name'] or '').lower() in BROWSERS:
                pids.add(p.pid)
        except psutil.Error:
            continue
    return {w.hwnd: window_title(w.hwnd) for w in mkey.get_all_windows() if w.pid in pids and w.status == 'visible'}


TAB_WAIT_S = 6.0
MORE_TABS = 10           # earlier launches' copies to close


def opened_tab(before, wait=TAB_WAIT_S):
    """hwnd of the browser window showing our page, or None. Tabs share a title, so the front window goes first."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        front, wins = foreground_hwnd(), browser_windows()
        if front in wins and 'roblox' in wins[front].lower():
            return front
        time.sleep(0.25)
    for hwnd, title in browser_windows().items():
        if 'roblox' in title.lower() and before.get(hwnd) != title:
            return hwnd
    return None


def close_tab(hwnd):
    """Ctrl+W our page and any copies of it behind it. Returns how many tabs closed."""
    if not hwnd or hwnd not in browser_windows() or not force_foreground(hwnd):
        return 0
    time.sleep(0.3)
    closed, page = 0, None
    for _ in range(1 + MORE_TABS):
        title = window_title(hwnd)
        if foreground_hwnd() != hwnd or 'roblox' not in title.lower() or (page is not None and title != page):
            break
        page = title
        with kb.pressed(Key.ctrl):
            kb.press(by_key('w'))
            kb.release(by_key('w'))
        closed += 1
        time.sleep(0.4)
        if not ctypes.windll.user32.IsWindow(hwnd):     # last tab, window gone
            break
    return closed


def close_opened_tab(hwnd, say):
    try:
        n = close_tab(hwnd)
        if n:
            say('tabs_closed n=%d' % n)
            take_foreground(tries=2)
    except Exception as e:
        say('WARN tab_close_failed type=%s msg="%s"' % (type(e).__name__, e))


def came_back(log_dir, stop, wait):
    """True once the game rejoined by itself."""
    end = time.monotonic() + wait
    while time.monotonic() < end and not stop():
        watch = Watch(log_dir)
        watch.poll()
        if watch.state == 'joined' and in_game(watch):
            return True
        time.sleep(3.0)
    return False


def player_command(scheme):
    """Desktop Roblox's command for this scheme, else the newest RobloxPlayerBeta.exe; None if only the Store one."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\%s\shell\open\command' % scheme) as k:
            command = winreg.QueryValueEx(k, '')[0]
    except OSError:
        command = None
    if isinstance(command, str) and '%1' in command:
        exe = command.split('"')[1] if command.startswith('"') and command.count('"') > 1 else command.split(' ')[0]
        if os.path.isfile(exe):
            return command
    local = os.environ.get('LOCALAPPDATA') or os.path.expanduser(r'~\AppData\Local')
    found = glob.glob(os.path.join(glob.escape(local), 'Roblox', 'Versions', '*', 'RobloxPlayerBeta.exe'))
    try:
        return '"%s" %%1' % max(found, key=os.path.getmtime) if found else None
    except OSError:
        return None


def open_link(target, say=lambda m: None):
    """Open roblox:// links with desktop Roblox, anything else the normal Windows way."""
    scheme = target.split(':', 1)[0].lower()
    command = player_command(scheme) if scheme in ('roblox', 'roblox-player') else None
    if command:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('_PYI_', '_MEIPASS'))}
        try:
            subprocess.Popen(command.replace('%1', target), env=env, close_fds=True)
            say('link_opened exe=%s' % os.path.basename(command.split('"')[1] if command.startswith('"') else
                                                        command.split(' ')[0]))
            return
        except OSError as e:
            say('WARN desktop_start_failed fallback=windows error="%s"' % e)
    os.startfile(target)


def rejoin(link, say=lambda m: None, stop=lambda: False, log_dir=None, browser=False, frozen=False, join_s=JOIN_S):
    """Reopen the private server and wait till in game; a running Roblox is closed first unless it rejoins itself."""
    target = deeplink(link)
    if not target:
        say('ERROR bad_link reason="not a Roblox link"')
        return False
    if browser:
        target = link.strip()
    log_dir = log_dir or LOG_DIR
    if frozen:
        say('close_roblox reason="frozen or last try failed"')
        if not close_roblox():
            say('ERROR close_roblox_failed wait_s=15')
            return False
        time.sleep(CLOSED_SETTLE_S)
    elif roblox_running():
        say('self_rejoin_wait wait_s=%d' % REJOIN_GRACE_S)
        if came_back(log_dir, stop, REJOIN_GRACE_S):
            say('self_rejoined')
            return True
    if roblox_running():
        # a second client won't join beside a stuck one
        say('close_roblox reason="before reopen"')
        if not close_roblox():
            say('ERROR close_roblox_failed wait_s=15')
            return False
        time.sleep(CLOSED_SETTLE_S)
    watch = Watch(log_dir)
    watch.poll()        # skip history
    watch.state, watch.why, watch.aura, watch.in_menu = None, '', None, False
    if stop():
        return False
    tabs = browser_windows() if browser and target.startswith('http') else None
    say('rejoin_start browser=%s join_wait_s=%d' % ('true' if target.startswith('http') else 'false', join_s))
    open_link(target, say)
    tab = [opened_tab(tabs) if tabs is not None else None]
    try:
        return _join(watch, stop, say, tab, join_s)
    finally:
        close_opened_tab(tab[0], say)       # even if it never joined


def _join(watch, stop, say, tab, join_s=JOIN_S):
    """After launch: wait for the join, close the tab, press Play if needed."""
    t0, joined_at, started = time.monotonic(), None, False
    play_due, presses = t0 + PLAY_AFTER_S, 0
    while not stop():
        watch.poll()
        now = time.monotonic()
        play_due, pressed = calibrated_play(watch, play_due, say)
        presses += pressed
        # Roblox logs a leave before the join
        if watch.state == 'lost' and (joined_at is not None or 'leaveUGCGameInternal' not in watch.why):
            say('ERROR join_dropped reason="%s"' % watch.why)
            return False
        if joined_at is None and watch.state == 'joined':
            joined_at = now
            say('joined took_s=%.1f next=load_wait' % (now - t0))
            close_opened_tab(tab[0], say)
            tab[0] = None
        if joined_at is None and now - t0 > join_s:
            say('ERROR join_timeout wait_s=%d' % join_s)
            return False
        if joined_at is None and not started and now - t0 > START_S:
            started = roblox_starting()
            if not started:
                say('ERROR roblox_not_started wait_s=%d' % START_S)
                return False
        if find_play() is not None:
            if not press_play(watch, stop, say, play_due=play_due, presses=presses):
                say('ERROR game_not_started after=play_seen')
                return False
            return not stop()
        if joined_at is not None and (watch.in_menu or watch.aura is not None or now - joined_at > READY_S):
            if not in_game(watch):
                if not press_play(watch, stop, say, play_due=play_due, presses=presses):
                    say('ERROR game_not_started after=joined')
                    return False
                return not stop()
            end = time.monotonic() + JOIN_SETTLE_S
            while time.monotonic() < end and not stop():
                time.sleep(0.2)
            return not stop()
        time.sleep(1.0)
    return False


# ================================================================ item and aura menus
POINTS = (
    ('collection', 'Collection button'),
    ('exit_collection', 'Close Collections button'),
    ('inventory', 'Inventory button'),
    ('items', 'Items tab'),
    ('search', 'Search box'),
    ('slot1', 'First item slot'),
    ('slot2', 'Second item slot'),
    ('use', 'Use button'),
)
SC, BR = 'strange controller', 'biome randomizer'
CLEAR_TAPS = 40          # aura names run to 31 chars


NAME_COLOURS = {SC: 'grey', BR: 'grey', 'merchant teleporter': 'gold', 'portable crack': 'gold'}
NAME_LOOKS = {'grey': ('grey', 'white'), 'gold': ('gold',)}     # gold = One-time BR, never a BR
GOLD = (30, 60)          # hue; Teleporter and Crack read 44-46
NAME_INK = 0.02          # share of read area
NAME_W, NAME_H = 0.36, 0.2   # half size, share of slot pitch


INTERRUPT = None         # a merchant or pop stops a turn


def stopped():
    return ABORT or bool(INTERRUPT and INTERRUPT())


def type_char(ch, hold=0.03):
    """Type one character via Roblox's layout, or as a unicode event when no key makes it."""
    wait_for_front()
    r = u32.VkKeyScanExW(ch, _layout())
    vk, mods = r & 0xFF, (r >> 8) & 0xFF
    if r == -1 or mods & ~1:                    # no plain key: send unicode
        for flags in (0x0004, 0x0004 | KEYUP):  # KEYEVENTF_UNICODE
            inp = IN(type=1, ki=KI(wVk=0, wScan=ord(ch), dwFlags=flags, time=0, dwExtraInfo=0))
            if not u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)):
                raise ctypes.WinError(ctypes.get_last_error())
            paced(hold)
        return
    if mods & 1:
        _send(0x10, 0, 0x2A)
    try:
        _send(vk, 0)
        paced(hold)
        _send(vk, KEYUP)
    finally:
        if mods & 1:
            _send(0x10, KEYUP, 0x2A)


def type_term(term, clear=True):
    """Put `term` in the focused box: pasted (the user's clipboard put back), typed if the clipboard can't be had."""
    paste_term(term, clear)


def _type_keys(term, clear=True):
    if clear:                                   # leftover text can outlast the taps
        down('ctrl')
        try:
            tap('a', hold=0.01, gap=0.01)
        finally:
            up('ctrl')
        tap(Key.backspace, hold=0.01, gap=0.01)
    for key in (Key.backspace, Key.delete) if clear else ():     # caret may be mid-text
        for _ in range(CLEAR_TAPS):
            tap(key, hold=0.01, gap=0.01)
    for ch in term:
        type_char(ch)
        paced(STEP_DELAY)


k32 = ctypes.WinDLL('kernel32', use_last_error=True)
for _f, _a, _r in ((u32.GetClipboardData, [wintypes.UINT], ctypes.c_void_p),
                   (u32.SetClipboardData, [wintypes.UINT, ctypes.c_void_p], ctypes.c_void_p),
                   (u32.EnumClipboardFormats, [wintypes.UINT], wintypes.UINT),
                   (k32.GlobalAlloc, [wintypes.UINT, ctypes.c_size_t], ctypes.c_void_p),
                   (k32.GlobalLock, [ctypes.c_void_p], ctypes.c_void_p),
                   (k32.GlobalUnlock, [ctypes.c_void_p], wintypes.BOOL),
                   (k32.GlobalSize, [ctypes.c_void_p], ctypes.c_size_t)):
    _f.argtypes, _f.restype = _a, _r
CLIP_HANDLES = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E} | set(range(0x300, 0x400))   # GDI handles: skipped
CLIP_PRIVATE = u32.RegisterClipboardFormatW('ExcludeClipboardContentFromMonitorProcessing')  # kept out of Win+V


def _clip_open():
    for _ in range(10):                         # may be held briefly
        if u32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _clip_put(fmt, data):
    h = k32.GlobalAlloc(0x0002, max(1, len(data)))      # GMEM_MOVEABLE
    if not h:
        return
    p = k32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    k32.GlobalUnlock(h)
    u32.SetClipboardData(fmt, h)


def _clip_save():
    """Everything on the clipboard as [(format, bytes)], or None if it couldn't be opened."""
    if not _clip_open():
        return None
    saved, fmt = [], 0
    try:
        while True:
            fmt = u32.EnumClipboardFormats(fmt)
            if not fmt:
                return saved
            if fmt in CLIP_HANDLES:
                continue
            h = u32.GetClipboardData(fmt)
            p = k32.GlobalLock(h) if h else None
            if p:
                saved.append((fmt, ctypes.string_at(p, k32.GlobalSize(h))))
                k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def _clip_set(items):
    if not _clip_open():
        return False
    try:
        u32.EmptyClipboard()
        for fmt, data in items:
            _clip_put(fmt, data)
        return True
    finally:
        u32.CloseClipboard()


def paste_term(term, clear=True):
    """Clear the focused box and paste `term`, restoring the clipboard; typed if the clipboard can't be had."""
    saved = _clip_save()
    if saved is None or not _clip_set([(13, (term + '\0').encode('utf-16-le')), (CLIP_PRIVATE, b'\0')]):  # CF_UNICODETEXT
        return _type_keys(term, clear)
    try:
        if clear:
            down('ctrl')
            try:
                tap('a', hold=0.01, gap=0.01)
            finally:
                up('ctrl')
            for key in [Key.backspace] * (PASTE_CLEAR + 1) + [Key.delete] * PASTE_CLEAR:   # if Ctrl+A isn't taken
                tap(key, hold=0.005, gap=0.005)
        down('ctrl')
        try:
            tap('v', hold=0.01, gap=0.01)
        finally:
            up('ctrl')
        time.sleep(PASTE_READ_S)                # Roblox reads it with the keys
    finally:
        _clip_set(saved)


PASTE_READ_S = 0.1


def trim_memory():
    """Hand idle memory pages back to Windows (Task Manager's figure); they come back as they're used."""
    ps = ctypes.WinDLL('psapi')
    ps.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    return bool(ps.EmptyWorkingSet(k32.GetCurrentProcess()))
PASTE_CLEAR = 10          # about 0.25 s


DAILY_POINTS = (('daily_claim', 'Daily rewards: Claim button'), ('daily_close', 'Daily rewards: Close button'))
FISH_POINTS = (
    ('fishingMinigameFishButton', 'Fish button'),
    ('fishingMinigameResultCloseButton', 'Close the catch (X)'),
    ('dialogueSkipDialogue', 'Skip dialogue'),
    ('dialogueChoice[1 of 1]', "Captain Flarg's choice"),
    ('fishingShopSellTab', 'Fish shop: Sell tab'),
    ('fishingShopSellItem[1,1]', 'Fish shop: first fish'),
    ('fishingShopSellAllButton', 'Fish shop: Sell All'),
    ('fishingShopSellConfirmSellButton', 'Fish shop: Confirm'),
    ('fishingShopCloseButton', 'Fish shop: close (X)'),
)
MERCHANT_POINTS = (
    ('inventoryInvClose', 'Inventory: close (X)'),
    ('merchantShopSetToMaxButton', 'Merchant shop: Set to Max'),
    ('merchantShopPurchaseAmountBox', 'Merchant shop: amount box'),
    ('merchantShopPurchaseButton', 'Merchant shop: Purchase'),
    ('merchantShopCloseButton', 'Merchant shop: close (X)'),
)
BUILT_IN.update({name: name for name, _ in FISH_POINTS + MERCHANT_POINTS})
BUILT_IN.update({'daily_claim': 'dailyRewardsClaimButton', 'daily_close': 'dailyRewardsCloseButton'})
DAILY_WAIT_S = 2.0


DAILY_SHOW_S = 10.0


def daily_state(rgb=None):
    """'claim' (green Claim up), 'claimed' (Claim dimmed) or None (no window)."""
    if rgb is None:
        import points
        rgb = points.grab('dailyRewardsClaimButton')
    if rgb is None or rgb.size == 0:
        return None
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    if (rgb.max(axis=2) < 45).mean() < 0.5:     # Claim's dark cover leaves ~7% visible
        return None
    greenish = (g - r > 40) & (g - b > 30) & (b >= r - 5)      # grass has less blue than red
    if (greenish & (g > 200)).mean() > 0.03:
        return 'claim'
    if (greenish & (g > 90) & (g < 160)).mean() > 0.03:
        return 'claimed'
    return None


def daily_by_clicks(halt=stopped):
    """Claim if the window shows a live Claim, then close it. False if no window came up."""
    end = time.monotonic() + DAILY_SHOW_S
    state = daily_state()
    while state is None and time.monotonic() < end and not halt():
        paced(0.5)
        state = daily_state()
    if state is None:
        return False
    if state == 'claim':
        press_point('daily_claim', settle=1.5)
    for _ in range(3):
        press_point('daily_close', settle=0.6)
        if daily_state() is None:
            break
    return state


def shut_daily():
    """Daily Rewards left up: claim if Claim is lit, then close it. True if it was up."""
    return daily_state() is not None and bool(daily_by_clicks())


def item_pass(say=print, terms=(BR, SC)):
    """Use one of each item in `terms` in one Inventory visit. Returns what was used."""
    if stopped():
        return []
    if not steady_front():
        say('WARN item_pass_skipped reason="Roblox not staying in front" used=[]')
        return []
    paced(SETTLE_S)
    used = []
    if open_by_click('inventory', stopped) and not stopped() and press_point('items', settle=0.3):
        for term in terms:
            if stopped():
                break
            slot = search_item(term, say, stopped, paced)        # reopens Inventory if Use shut it
            if slot is not None and not stopped() and press_use(slot):
                say('item_used term="%s" slot=%d' % (term, slot + 1))
                used.append(term)
            elif not stopped():
                say('WARN item_not_used term="%s"' % term)
    if INTERRUPT and INTERRUPT():
        return used                             # left open for merchant or pop
    if not stopped() and (window_seen() or not sees_windows()):
        press_point('inventory', settle=0.6)
    close_inventory()
    return used


def raw_points():
    """Saved clicks: {name: [fx, fy, width, height]} (old ones have no size)."""
    try:
        return dict(load_settings().get('clicks') or {})
    except Exception:
        return {}


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long), ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', _RECT), ('rcWork', _RECT), ('dwFlags', wintypes.DWORD)]


u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
u32.MonitorFromWindow.restype = ctypes.c_void_p
u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]


def _rects(hwnd):
    """(client w, h), (window w, h)."""
    c, w = _RECT(), _RECT()
    u32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(c))
    u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(w))
    return (c.right - c.left, c.bottom - c.top), (w.right - w.left, w.bottom - w.top)


FIT_SIZES = ((1920, 1080), (1920, 1009), (1366, 768), (1366, 697))   # odd sizes: first that fits


def fit_window(say=print):
    """Resize an odd-sized Roblox window to the biggest known size that fits. True if at a known size."""
    w = find_roblox_window()
    return w is not None and in_dpi(window_context(w.hwnd), _fit, w.hwnd, say)


def _fit(hwnd, say):
    import points
    known = points.sizes()
    (cw, ch), _ = _rects(hwnd)
    if (cw, ch) in known:
        return True
    if cw <= 0 or ch <= 0:
        return False                                    # minimised
    s = _client(hwnd)[1]
    stretched = '' if abs(s - 1) < 0.01 else ' (Roblox is not DPI aware: Windows stretches it %d%%)' % round(s * 100)
    if not u32.GetWindowLongW(wintypes.HWND(hwnd), -16) & 0x00C00000:       # no title bar = fullscreen
        say('Roblox is fullscreen (or borderless) at %dx%d, a size the buttons are not made for: press F11 in Roblox '
            'for a normal window and MMacro sizes it to 1080p%s' % (cw, ch, stretched))
        return False
    if u32.IsZoomed(wintypes.HWND(hwnd)):
        u32.ShowWindowAsync(wintypes.HWND(hwnd), 9)     # SW_RESTORE: maximised can't be sized
        time.sleep(0.6)
    (cw, ch), (ww, wh) = _rects(hwnd)
    info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    u32.GetMonitorInfoW(u32.MonitorFromWindow(wintypes.HWND(hwnd), 2), ctypes.byref(info))
    work = info.rcWork
    room_w, room_h = work.right - work.left, work.bottom - work.top
    for tw, th in FIT_SIZES:
        ow, oh = tw + ww - cw, th + wh - ch
        if ow > room_w or oh > room_h:
            continue
        u32.SetWindowPos(wintypes.HWND(hwnd), None, work.left + (room_w - ow) // 2, work.top + (room_h - oh) // 2,
                         ow, oh, 0x0004 | 0x0010 | 0x4000)      # no z-order, no activate, async
        for _ in range(20):
            time.sleep(0.1)
            if _rects(hwnd)[0] == (tw, th):
                say('window_resized from=%dx%d to=%dx%d scale_pct=%d' % (cw, ch, tw, th, round(s * 100)))
                return True
        say('ERROR window_resize_failed from=%dx%d to=%dx%d wait_s=2' % (cw, ch, tw, th))
        return False
    say('WARN window_no_fit room=%dx%d size=%dx%d scale_pct=%d fallback=scaled' % (room_w, room_h, cw, ch, round(s * 100)))
    return False


def window_size():
    """Client size in Roblox's own pixels (what its buttons are laid out at)."""
    w = find_roblox_window()
    return in_dpi(window_context(w.hwnd), _view, w.hwnd)[2:4] if w is not None else None


def load_points():
    """Calibrated points as {name: (fx, fy)}. A built-in button's only counts at the size it was set at."""
    size = False
    out = {}
    for name, value in raw_points().items():
        try:
            if name in BUILT_IN:
                if size is False:
                    size = window_size()
                if size is None or tuple(int(v) for v in value[2:4]) != size:
                    continue
            out[name] = (float(value[0]), float(value[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def save_point(name, point):
    stored = raw_points()
    stored[name] = [round(point[0], 5), round(point[1], 5)] + list(window_size() or ())
    save_settings({'clicks': stored})
    return stored


def forget_point(name):
    """Back to the built-in spot."""
    stored = raw_points()
    stored.pop(name, None)
    save_settings({'clicks': stored})


def _window_rect():
    box = client_box((0.0, 0.0, 1.0, 1.0))
    return box[0], box[1], max(1, box[2] - box[0]), max(1, box[3] - box[1])


def to_fraction(x, y):
    left, top, w, h = _window_rect()
    return (x - left) / float(w), (y - top) / float(h)


def to_screen(point):
    left, top, w, h = _window_rect()
    return int(round(left + point[0] * w)), int(round(top + point[1] * h))


def press_point(name, settle=0.3, travel=GLIDE_S):
    """Click a button. False if it has no spot, there is no Roblox window or it won't come forward."""
    if find_roblox_window() is None:
        print('WARN click_skipped point=%s reason="no Roblox window"' % name)
        return False
    at = point_at(name)
    if at is None or not click(*at, park=False, travel=travel):
        return False
    paced(settle)
    return True


def point_at(name):
    """Screen spot of a button: calibrated, else built in."""
    stored = load_points()
    if name in stored:
        return to_screen(stored[name])
    if name in BUILT_IN:
        import points
        return points.centre(BUILT_IN[name])
    return None


def collection_open():
    """Is Collection up? Looks for the thin bright lines around its list buttons. None if its box is unknown."""
    import points
    b = points.box('collectionLeftScroll')
    if b is None:
        return None
    l, t, r, bottom = b
    w, h = r - l, bottom - t
    v = grab((l + w // 20, t + h * 2 // 5, r - w // 20, bottom)).astype(int).min(axis=2)
    line = (v[2:-2] >= v[:-4] + 30) & (v[2:-2] >= v[4:] + 30)
    return int((line.mean(axis=1) >= 0.8).sum()) >= 3


after_align = None       # once after next camera squaring (daily rewards)
align_fails = 0


def align_camera(stop=lambda: False):
    """Open and close Collection to square the camera. False if it never opened or shut."""
    global after_align, align_fails
    if stop() or take_foreground() is None:
        return False
    for go in range(1, 4):
        # slow: a fast glide may only hover
        if press_point('collection', settle=1.0, travel=0.6) and collection_open() is not False:
            break
        if stop():
            return False
        print('WARN collection_open_failed attempt=%d/3' % go)
        x, y = cursor()
        glide(x + 60, y + 120)                  # off it to hover fresh
    else:
        align_fails += 1
        return False
    press_point('exit_collection', settle=0.5)
    for go in range(3):
        if stop() or not collection_open():
            break
        # below 1440p chat covers Exit; hide it (last go shows it again)
        print('WARN collection_still_open attempt=%d/3 chat=%s' % (go + 1, 'true' if go else 'false'))
        if go:
            press_chat()
        press_point('exit_collection', settle=0.5)
    else:
        if collection_open():
            print('ERROR collection_close_failed tries=3')
            align_fails += 1                    # else it retries for ever
            return False
    align_fails = 0
    if after_align and not stop():
        go, after_align = after_align, None
        go()
    return not stop()


TILT_STEPS, TILT_STEP = 20, 10   # camera stops at straight down


def tilt_up(stop=stopped):
    """Hold right click and drag down so the camera looks down from above."""
    w = take_foreground()
    if w is None or stop():
        return False
    x0, y0, cw, ch = client_rect(w)
    move_to(x0 + cw // 2, y0 + ch // 2, 0.2)
    button('right', True)
    try:
        paced(0.2)
        for _ in range(TILT_STEPS):
            _mouse(0x0001, dy=TILT_STEP)
            time.sleep(0.03)
        paced(0.2)
    finally:
        button('right', False)
    paced(0.3)
    return not stop()


CHAT_BUTTON = (138, 34)  # from client top left, not scaled


def press_chat():
    w = find_roblox_window()
    if w is None:
        return
    x0, y0 = client_rect(w)[:2]
    s = window_scale(w)
    click(int(x0 + CHAT_BUTTON[0] * s), int(y0 + CHAT_BUTTON[1] * s), park=False)
    paced(0.3)


def inventory_shown():
    """False only when the Inventory is known to be off screen (a cutscene or another window shut it)."""
    return not sees_windows() or inventory_open()


def reopened(halt=stopped):
    """Bring the Inventory back after a cutscene or window shut it. Its search and pick survive."""
    end = time.monotonic() + BACK_S * LIMIT
    while not inventory_shown() and time.monotonic() < end:     # cutscenes give it back
        if halt():
            return False
        paced(0.3)
    return inventory_shown() or open_by_click('inventory', halt)


def amount_one():
    """Does the Use amount box read 1? A 1 is one narrow upright stroke; a caret is too thin to count."""
    import points
    rgb = points.grab('inventoryUseAmount')
    if rgb is None or not rgb.size:
        return False
    h, w = rgb.shape[:2]
    ink = rgb[h // 6:h * 5 // 6, w // 10:w * 9 // 10].min(-1) > 150
    rows = np.nonzero(ink.any(1))[0]
    if not rows.size:
        return False
    tall = rows[-1] - rows[0] + 1
    strokes = [e - s + 1 for s, e in runs(np.nonzero(ink.any(0))[0], 1)]
    strokes = [n for n in strokes if n > max(2, 0.12 * tall)]
    return len(strokes) == 1 and strokes[0] < 0.45 * tall


def use_one():
    """Set the Use amount to 1. False if it doesn't read 1 after, or the Inventory went away."""
    import points
    if not inventory_shown():
        return False
    if amount_one():
        return True
    if not points.click('inventoryUseAmount', 0.1):
        return False
    for key in (Key.backspace, Key.delete):
        for _ in range(8):
            tap(key, hold=0.01, gap=0.01)
    type_char('1')
    return until(amount_one, 0.1, 2.0, paced) and inventory_shown()


USE_WAIT_S = 5.0
USE_TRIES = 4            # counts presses a cutscene ate
BACK_S = 2.0
LOOK_STEADY_S = 1.5      # so a late frame can't fake a use
DOUBLE_USE = ('strange controller', 'biome randomizer', 'biome selector', 'merchant teleporter')     # Use twice so it lands
_searched = None
SEARCH_TRIES = 3
RESULTS_S = 2.0


def slot_look(slot):
    """The slot's 'xN' count and name colour (change when one is used); None if off screen or slots unknown."""
    a, b = point_at('slot1'), point_at('slot2')
    if a is None or b is None:
        return None
    x, y = (a, b)[slot]
    left, top, _, _ = _window_rect()
    rgb = _grab_window()
    if sees_windows() and not inventory_open(rgb):
        return None
    pitch = math.hypot(b[0] - a[0], b[1] - a[1])
    h = int(pitch / 2)
    cx, cy = int(x - left), int(y - top)
    ink = rgb[max(0, cy - h):cy + h, max(0, cx - h):cx + h].min(-1)
    levels = (ink > 130).astype(np.uint8) + (ink > 170) + (ink > 215)
    return levels.tobytes() + name_colour((cx, cy), rgb, pitch).encode()


def steady(look):
    """look() once it has held for LOOK_STEADY_S; None if the Inventory went away."""
    end = time.monotonic() + 3 * LOOK_STEADY_S * LIMIT
    last, since = look(), time.monotonic()
    while last is not None and time.monotonic() - since < LOOK_STEADY_S * LIMIT and time.monotonic() < end:
        paced(0.3)
        now = look()
        if now != last:
            last, since = now, time.monotonic()
    return last


def use_taken(look, before):
    """After a Use press: 'used' once the slot changes, 'gone' if the Inventory went off screen first, else 'same'."""
    end = time.monotonic() + USE_WAIT_S * LIMIT
    while True:
        now = look()
        if now is None:
            return 'gone'
        if now != before:
            return 'used'
        if time.monotonic() >= end:
            return 'same'
        paced(0.3)


def looks_used(look, before, halt):
    """The Inventory went away after Use: open it again and see if the slot changed. None if it can't be seen."""
    if not reopened(halt):
        return None
    paced(1.0)                                  # reply still coming
    a = look()
    paced(0.3)
    b = look()
    if a is None or b is None or a != b:
        return None
    return a != before


def use_lit():
    """Is Use live (an item picked)? Unpicked, its word is dimmed but its white corner marks stay bright."""
    import points
    rgb = points.grab('inventoryUseButton')
    if rgb is None or not rgb.size:
        return False
    h, w = rgb.shape[:2]
    return int((rgb[h // 5:h * 4 // 5, w // 5:w * 4 // 5].min(-1) > 215).sum()) > 4


def use_lit_after(want, wait_s=2.0):
    end = time.monotonic() + wait_s * LIMIT
    while use_lit() != want and time.monotonic() < end:
        paced(0.25)
    return use_lit() == want


def picked(click_slot, say, settle=0.25):
    """Pick a slot so Use is live, never leaving an older pick for Use to spend. False if it won't take."""
    old = use_lit()                             # old pick (lost Items click): Use would spend it
    for _ in range(2 if old else 0):
        press_point('items', settle=0.3)
        if use_lit_after(False):
            old = False
            break
    if old:
        say('WARN use_skipped reason="old pick still up"')
        return False
    for _ in range(3):
        if not click_slot(settle):
            return False
        if use_lit_after(True, 3.0):
            return True
    say('WARN use_skipped reason="slot not picked" tries=3')
    return False


def press_use(slot, settle=0.25, halt=stopped):
    """Pick search result `slot`, set the amount to 1, press Use. No waiting to see it taken."""
    if halt() or not press_point('slot%d' % (slot + 1), settle=settle):
        return False
    use_one()
    double = _searched in DOUBLE_USE
    if halt() or not press_point('use', settle=0.08 if double else 0.4):
        return False
    if double:
        press_point('use', settle=0.4, travel=0)
    return not halt()


def use_slot(slot, say, settle=0.25, halt=stopped, ours=False):
    """Pick search result `slot` (0 or 1), set the amount to 1, press Use. True only once the slot shows one fewer."""
    return use_picked(lambda s: press_point('slot%d' % (slot + 1), settle=s), lambda: slot_look(slot), say, settle, halt,
                      ours)


def use_picked(pick, look, say, settle=0.25, halt=stopped, ours=False):
    """Pick a slot, set the amount to 1, press Use; True once look() changes. ours: reuse our last pick."""
    for go in range(USE_TRIES):
        if halt() or not reopened(halt):
            return False
        lit = use_lit() or (ours and use_lit_after(True, 3.0))
        if ours and not lit:
            if inventory_shown():
                say('WARN use_stopped reason="nothing picked, none left?" attempt=%d/%d' % (go + 1, USE_TRIES))
                return False
            continue
        if ((go == 0 and not ours) or not lit) and not picked(pick, say, settle):     # later, a lit Use is ours
            if inventory_shown():
                return False
            continue
        if not use_one():
            continue
        before = steady(look)
        if before is None or not use_lit():
            continue
        if not press_point('use', settle=0.4):
            return False
        taken = use_taken(look, before)
        if taken == 'gone':
            seen = looks_used(look, before, halt)
            if seen is None:
                seen = looks_used(look, before, halt)       # cutscene, then closing window
            taken = {True: 'used', False: 'same', None: 'gone'}[seen]
            if taken == 'same':
                say('WARN use_retry reason="Inventory shut before Use took" attempt=%d/%d' % (go + 1, USE_TRIES))
                continue
        if taken == 'used':
            return True
        if taken == 'gone':
            say('ERROR use_failed reason="Inventory shut after Use, would not reopen" attempt=%d/%d' % (go + 1, USE_TRIES))
            return False
        say('WARN use_retry reason="Use did nothing" attempt=%d/%d' % (go + 1, USE_TRIES))
    say('ERROR use_failed tries=%d' % USE_TRIES)
    return False


def search_item(term, say, halt, wait, tries=SEARCH_TRIES):
    """Type `term` in the Inventory search and return the slot to use (read the same twice), or None."""
    global _searched
    _searched = None
    for go in range(tries):
        again = 'retry' if go < tries - 1 else 'give_up'
        if halt():
            return None
        if not inventory_shown() and not reopened(halt):
            say('ERROR item_search_failed term="%s" reason="Inventory gone, would not reopen"' % term)
            return None
        if go and not press_point('items', settle=0.3):        # a lost Items click leaves Gears up
            return None
        if not press_point('search', settle=0.08) or not press_point('search', settle=0.2, travel=0):
            return None
        if not inventory_shown():
            continue
        type_term(term)
        wait(0.6)
        slot, colours = None, []
        end = time.monotonic() + RESULTS_S * LIMIT
        while not halt():
            slot, colours = find_item(term)
            if slot is not None:
                wait(0.3)
                if find_item(term)[0] == slot:  # grid may still redraw
                    _searched = term
                    return slot
            if time.monotonic() >= end or colours == ['no Inventory']:
                break
            wait(0.4)
        if halt():
            return None
        say('WARN item_search term="%s" results=[%s]%s attempt=%d/%d next=%s'
            % (term, ','.join(colours), '' if colours else ' slot_spots=false', go + 1, tries, again))
    return None


def until(check, first, most, wait):
    """Wait `first`, then keep looking until check() holds or `most` seconds have gone."""
    wait(first)
    end = time.monotonic() + (most - first) * LIMIT
    while not check():
        if time.monotonic() >= end:
            return False
        wait(0.3)
    return True


OPEN_TRIES = 4           # cutscenes eat clicks up to 6 s
OPEN_WAIT = 1.5


_sees_windows = None     # Inventory misses count only at these sizes


def _size_key():
    size = window_size()
    return '%dx%d' % size if size else ''


def sees_windows():
    global _sees_windows
    if _sees_windows is None:
        try:
            saved = load_settings().get('sees_windows')
            _sees_windows = set(saved) if isinstance(saved, list) else set()
        except Exception:
            _sees_windows = set()
    return _size_key() in _sees_windows


def window_seen():
    if not inventory_open():
        return False
    key = _size_key()
    if key and not sees_windows():
        _sees_windows.add(key)
        try:
            save_settings({'sees_windows': sorted(_sees_windows)})
        except Exception:
            pass
    return True


_side_open = None


def open_by_click(name, halted=lambda: False):
    """Click a side button until its window shows; one click is trusted where windows aren't recognised."""
    global _side_open
    if _side_open == name and inventory_open():
        return True
    close_inventory()
    for _ in range(OPEN_TRIES):
        if halted() or not press_point(name, settle=0.4):
            return False
        if not window_seen():
            paced(OPEN_WAIT)
            if not (window_seen() or not sees_windows()):
                continue
        _side_open = name
        return True
    return False


def _grab_window():
    return grab(client_box((0.0, 0.0, 1.0, 1.0)))


def name_colour(centre, rgb, pitch):
    """Colour of the item name in a slot: 'grey', 'white', 'gold', 'hue N' or 'empty'."""
    x, y = int(centre[0]), int(centre[1])
    hx, hy = max(2, int(pitch * NAME_W)), max(2, int(pitch * NAME_H))
    crop = rgb[max(0, y - hy):y + hy + 1, max(0, x - hx):x + hx + 1].astype(np.int16)
    if crop.size == 0:
        return 'empty'
    val = crop.max(-1)
    spread = val - crop.min(-1)
    flat = spread <= 20
    tint = (spread >= 60) & (val >= 110)
    counts = {'grey': (flat & (val >= 120) & (val <= 215)).sum(), 'white': (flat & (val > 215)).sum(),
              'tint': tint.sum()}
    best = max(counts, key=counts.get)
    if counts[best] < NAME_INK * val.size:
        return 'empty'
    if best != 'tint':
        return best
    hue = int(np.median(hsv(crop)[0][tint])) * 2
    return 'gold' if GOLD[0] <= hue <= GOLD[1] else 'hue %d' % hue


def has_count(centre, rgb, pitch):
    """Is there an item in the slot? Its 'xN' count is white, bottom right; an empty slot shows the dimmed world."""
    x, y = int(centre[0]), int(centre[1])
    crop = rgb[max(0, y + int(0.12 * pitch)):y + int(0.42 * pitch), max(0, x):x + int(0.42 * pitch)]
    return int((crop.min(-1) > 215).sum()) >= 4 if crop.size else False


def name_rgb(slot):
    """Colour of the name in search result `slot` (its brightest tinted ink), or None."""
    a, b = point_at('slot1'), point_at('slot2')
    if a is None or b is None:
        return None
    left, top, _, _ = _window_rect()
    rgb = _grab_window()
    if sees_windows() and not inventory_open(rgb):
        return None
    pitch = math.hypot(b[0] - a[0], b[1] - a[1])
    x, y = int((a, b)[slot][0] - left), int((a, b)[slot][1] - top)
    hx, hy = max(2, int(pitch * NAME_W)), max(2, int(pitch * NAME_H))
    c = rgb[max(0, y - hy):y + hy + 1, max(0, x - hx):x + hx + 1].reshape(-1, 3).astype(int)
    c = c[c.max(1) - c.min(1) >= 40]
    if len(c) < 5:
        return None
    v = c.max(1)
    return tuple(np.median(c[v >= np.percentile(v, 80)], 0))


def find_item(term):
    """Which search result is `term` (0, 1 or None), and what the first three slots read as."""
    a, b = point_at('slot1'), point_at('slot2')
    if a is None or b is None:
        return None, []
    left, top, _, _ = _window_rect()
    rgb = _grab_window()
    if sees_windows() and not inventory_open(rgb):
        return None, ['no Inventory']           # a cutscene's backdrop can read as a name
    pitch = math.hypot(b[0] - a[0], b[1] - a[1])
    spots = [(x - left, y - top) for x, y in (a, b, (2 * b[0] - a[0], 2 * b[1] - a[1]))]
    want = NAME_COLOURS.get(term)
    if want is None:                            # colour unknown: lone result by its 'xN'
        colours = [name_colour(s, rgb, pitch) for s in spots]
        filled = [has_count(s, rgb, pitch) for s in spots]
        return (0 if filled == [True, False, False] else None), colours
    boxes = [has_box(s, rgb, pitch) for s in spots]
    names = [name_colour(s, rgb, pitch) for s in spots[:2]]
    colours = ['%s %s' % ('box' if x else 'no box', n) for x, n in zip(boxes, names + [''])]
    if not boxes[0] or boxes[2]:
        return None, colours                    # none, or search too broad
    found = [i for i in (0, 1) if boxes[i] and names[i] in NAME_LOOKS[want]]
    if not found and not boxes[1] and names[0] == 'empty':
        found = [0]                             # lone unread name; wrong colour never passes
    return (found[0] if found else None), colours


def has_box(centre, rgb, pitch):
    """Is there an item box in this slot? At least three of its four corner marks are bright."""
    x, y = int(centre[0]), int(centre[1])
    a, b = int(0.30 * pitch), int(0.47 * pitch)
    sq = rgb[max(0, y - b):y + b, max(0, x - b):x + b]
    if sq.shape[0] < 2 * b or sq.shape[1] < 2 * b:
        return False
    lit = sq.max(-1) > 90
    n = b - a
    corners = [lit[:n, :n], lit[:n, -n:], lit[-n:, :n], lit[-n:, -n:]]
    return sum(c.mean() >= 0.04 for c in corners) >= 3


# ================================================================ self check (python game.py)
def _sent(fn):
    """Every mouse event the call makes, as (flags, dx, dy, data)."""
    out, real = [], u32.SendInput

    def fake(n, inp, size):
        mi = ctypes.cast(inp, ctypes.POINTER(IN)).contents.mi
        out.append((mi.dwFlags, mi.dx, mi.dy, mi.mouseData))
        return 1
    u32.SendInput = fake
    try:
        fn()
    finally:
        u32.SendInput = real
    return out

def demo():
    me = sys.modules[__name__]

    events = _sent(lambda: button('left', True))
    assert events and events[0][0] == MOVE_ABS | BTN_DOWN['left'], events
    assert events[0][1] > 0 or events[0][2] > 0, 'the press went out with no position: %s' % (events,)
    print('  a press carries the absolute position it happens at')
    events = _sent(lambda: button('right', False))
    assert [e[0] for e in events] == [BTN_UP['right']], events
    print('  a release carries no position (it would undo a camera drag)')

    real_front, real_pace = me.take_foreground, PACE
    me.take_foreground, me.PACE = lambda *a, **k: True, 0.0
    try:
        events = _sent(lambda: click(300, 200, park=False))
        assert events[-1][0] == BTN_UP['left'], events[-1]
        assert events[-2][0] == MOVE_ABS | BTN_DOWN['left'], events[-2]
        assert len(events) > 3, 'no pointer move before the click: %d' % len(events)
        print('  a click moves, presses and releases: %d events' % len(events))

        real_send = u32.SendInput
        u32.SendInput = lambda *a: 0
        me._said_refused = False
        try:
            assert click(300, 200, park=False) is False, 'a blocked click reported success'
        finally:
            u32.SendInput = real_send
        print('  a click Windows refuses comes back False')
    finally:
        me.take_foreground, me.PACE = real_front, real_pace

    real_find = me.find_roblox_window
    me.find_roblox_window = lambda: None
    try:
        assert press_point('collection') is False, 'clicked a fraction with no window to measure it against'
    finally:
        me.find_roblox_window = real_find
    print('  no Roblox window: no calibrated click at all')

    me._said_refused = True
    fresh_session()
    assert _said_refused is False
    print('  Start clears what closing and reopening the program used to clear')

    ups, real_state, real_send = [], u32.GetAsyncKeyState, u32.SendInput
    u32.GetAsyncKeyState = lambda vk: 0x8000 if vk == 0x5B else 0
    u32.SendInput = lambda n, inp, size: (ups.append(ctypes.cast(inp, ctypes.POINTER(IN)).contents.ki.wVk), 1)[1]
    try:
        _lift_stuck()
    finally:
        u32.GetAsyncKeyState, u32.SendInput = real_state, real_send
    assert ups == [MASK_VK, MASK_VK, 0x5B], ups
    print('  a Windows key left down is lifted behind a mask key (no Start menu); keys already up are left alone')

    grabs, front = [], {'is': False}

    def _slow_came(verify=True):
        grabs.append(1)
        front['is'] = False            # next key mustn't retry at once

    kept2, real2 = me.in_front, me.focus_roblox
    me.in_front, me.focus_roblox, me._took_front = lambda: front['is'], _slow_came, 0.0
    try:
        for _ in range(5):
            try:
                wait_for_front(grace=0.0)
            except NotFocused:
                pass
        assert len(grabs) == 1, 'took the foreground %d times in a row instead of once' % len(grabs)
    finally:
        me.in_front, me.focus_roblox = kept2, real2
    print('  a window that will not come forward is not fought over every key')

    front, took = {'is': False}, []

    def _came(verify=True):
        took.append(1)
        front['is'] = True

    kept_front, real_focus = me.in_front, me.focus_roblox
    me.in_front, me.focus_roblox, me._took_front = lambda: front['is'], _came, 0.0
    try:
        wait_for_front(grace=0.0)
        assert took, 'gave up on a key instead of bringing Roblox forward'
        print('  a key with Roblox behind another window brings it forward first')
        front['is'] = False
        me.focus_roblox = lambda verify=True: None          # it will not come forward
        try:
            wait_for_front(grace=0.0)
            raise AssertionError('sent a key into whatever window was in front')
        except NotFocused:
            pass
        print('  and is refused, not sent elsewhere, when it will not')
    finally:
        me.in_front, me.focus_roblox = kept_front, real_focus


if __name__ == '__main__':
    print('game self check:')
    demo()
