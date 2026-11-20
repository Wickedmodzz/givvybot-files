"""
Whatnot giveaway auto-joiner for MuMu Player (Android emulator).

Flow per stream:
  1. Wait a few seconds
  2. Tap the "Giveaway" tile (if the stream has one)
  3. Tap "Follow Host & Enter Giveaway" (if present)
  4. Close the panel
  5. Wait, then repeat on the SAME stream (the bot never scrolls)

Usage
-----
   python whatnot_giveaway_bot.py --dump    # print on-screen elements
   python whatnot_giveaway_bot.py           # run the loop (Ctrl+C to stop)

NOTE: Entering via this button FOLLOWS the host, so you will follow every
host whose giveaway you enter. Automation may also violate Whatnot's ToS.
"""

import argparse
import os
import random
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import uiautomator2 as u2

# Every log line gets a [device] prefix once a worker knows its emulator.
_TAG = ""
_builtin_print = print


def print(*args, **kwargs):  # noqa: A001  (intentionally shadows the builtin)
    if _TAG:
        _builtin_print(f"[{_TAG}]", *args, **kwargs)
    else:
        _builtin_print(*args, **kwargs)

# Folder for debug captures. When packaged as an .exe this is the folder the .exe sits in
# (not the temporary folder the program unpacks into).
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- CONFIG ----------------
DEVICE = "127.0.0.1:16384"
PACKAGE = "com.whatnot_mobile"

GIVEAWAY_TILE_TEXT = "Giveaway"                    # tile on the stream screen
PANEL_OPEN_TEXT = "Terms & Conditions"             # only visible when panel is open
ENTER_BUTTON_REGEX = r"(?i)(.*(enter|join).*giveaway.*|enter|join)"  # e.g. "Follow Host & Enter Giveaway"
CLOSE_DESC = "Close"
ON_STREAM_REGEX = r"(?i)say something.*"           # chat box = we are on a live stream
# Words that mean a piece of text is NOT the giveaway tile (panel buttons, banners)
TILE_EXCLUDE_REGEX = r"(?i).*(enter|winner|in the giveaway|follow|terms|selecting|won).*"
ENTERED_REGEX = r"(?i).*in the giveaway.*"        # "You're in the Giveaway" (already entered)
# Popups to dismiss automatically (exact button text, case-insensitive).
# e.g. the "Become a Verified Buyer" popup -> tap "Another time" (never "Get Verified").
DISMISS_BUTTON_REGEXES = [r"(?i)^another time$"]

PANEL_LOAD_SECONDS = (1.0, 2.0)
AFTER_ENTER_SECONDS = (1.5, 3.0)
MAX_RUNTIME_MIN = 0               # 0 = no time limit: run until you press Stop
# Giveaways whose panel text matches any of these are skipped (buyers-only etc.).
# Matched against each piece of text inside the open panel, whole-string, case-insensitive.
# Tune these once you see the real wording in the "Panel text:" log lines.
SKIP_PANEL_REGEXES = [
    r"(?i).*buyers?[ -]*only.*",
    r"(?i).*only (for )?(verified )?buyers.*",
    r"(?i).*purchase (is )?required.*",
    r"(?i).*(must|need to) (have )?(make|made|purchase|purchased|buy|bought).*",
    r"(?i).*(make|complete) a purchase to (enter|join).*",
    r"(?i).*(buyers|purchasers) (can|may) (enter|join).*",
]
STAY_POLL_SECONDS = (3, 5)      # how often to check for a new giveaway while waiting
STAY_COOLDOWN_SECONDS = (20, 30)  # pause after entering / finding you're already in, so the
                                  # same giveaway isn't re-opened over and over
STAY_HEARTBEAT_SECONDS = 60     # print a "still watching" line this often while idle
STAGGER_SECONDS = 6             # delay between launching each emulator worker

VERBOSE = True                  # narrate every step in the log

# Debug captures: screenshot + screen text saved to a "debug" folder next to the script
DEBUG_DIR = os.path.join(BASE_DIR, "debug")
MAX_DEBUG_FILES = 40            # stop saving after this many (protects disk space)
DEBUG_FIRST_ENTERED = 3         # also capture the first N successful entries to verify them
# ----------------------------------------


def rnd(rng):
    return random.uniform(*rng)


def adb_connect(device):
    """For host:port serials (emulators), run the equivalent of `adb connect`."""
    if ":" not in device:
        return
    try:
        from adbutils import adb
        adb.connect(device, timeout=5)
    except Exception:
        try:
            subprocess.run(["adb", "connect", device], capture_output=True, timeout=10)
        except Exception:
            pass


def connect(device=DEVICE):
    global _TAG
    _TAG = device
    adb_connect(device)
    d = u2.connect(device)
    print("Connected:", d.info.get("productName"), d.window_size())
    return d


def ensure_app_foreground(d):
    if d.app_current().get("package") != PACKAGE:
        print("Bringing Whatnot to foreground...")
        d.app_start(PACKAGE)
        time.sleep(4)


_last_left_report = 0.0


def check_whatnot_on_screen(d):
    """True if Whatnot is the app in front. If it isn't (closed, crashed, or the emulator went to
    its home screen), say so once in a while with the last bot tap for context, save a capture,
    and bring Whatnot back. It may reopen on its own home feed, not your stream."""
    global _last_left_report
    try:
        pkg = d.app_current().get("package")
    except Exception:
        return True                      # can't tell: carry on as before
    if not pkg or pkg == PACKAGE:
        return True
    if time.time() - _last_left_report >= 30:
        _last_left_report = time.time()
        what, x, y, t = _last_action
        print(f"  WHATNOT IS NOT ON SCREEN (the emulator is showing '{pkg}'). "
              f"Last bot tap: '{what}' at ({x},{y}), {time.time() - t:.0f}s ago.")
        print("  Bringing Whatnot back. If it opens on its home feed, open your live stream again.")
        save_debug(d, "left_whatnot", note=f"foreground package: {pkg}; last tap: {what} ({x},{y})")
    try:
        d.app_start(PACKAGE)
        time.sleep(4)
    except Exception as e:
        print("  Could not reopen Whatnot:", e)
    return False


def dump(d):
    root = ET.fromstring(d.dump_hierarchy())
    seen = set()
    count = 0
    for node in root.iter("node"):
        text = node.get("text", "")
        desc = node.get("content-desc", "")
        rid = node.get("resource-id", "")
        key = (text, desc, rid)
        if (text or desc) and key not in seen:
            seen.add(key)
            count += 1
            print(f"text={text!r:40} desc={desc!r:30} clickable={node.get('clickable','')} id={rid}")
    print(f"\n{count} labelled elements found. Current app: {d.app_current()}")


_last_read_error = None


def read_texts(d):
    """All text / content-desc strings on screen, from a single hierarchy dump.
    (One call is more reliable than many element lookups on a busy, animating screen.)"""
    global _last_read_error
    try:
        root = ET.fromstring(d.dump_hierarchy())
        out = []
        for node in root.iter("node"):
            t, ds = node.get("text", ""), node.get("content-desc", "")
            if t:
                out.append(t)
            if ds:
                out.append(ds)
        return out
    except Exception as e:
        msg = repr(e)[:160]
        if msg != _last_read_error:          # print each distinct error once
            print("  (screen read error:", msg, ")")
            _last_read_error = msg
        return []


def any_match(texts, regex):
    return any(re.fullmatch(regex, t) for t in texts)


def panel_texts(d):
    """Texts/descriptions inside the giveaway panel. Normally everything after the 'Close'
    button; if the panel has no 'Close' label (newer design), everything in the top half of
    the screen below the status bar."""
    root = ET.fromstring(d.dump_hierarchy())
    out, inside, found_close = [], False, False
    for node in root.iter("node"):
        if node.get("content-desc", "") == CLOSE_DESC:
            found_close = True
    if found_close:
        for node in root.iter("node"):
            text, desc = node.get("text", ""), node.get("content-desc", "")
            if desc == CLOSE_DESC:
                inside = True
            if inside and (text or desc):
                out.append(text or f"[{desc}]")
        return out
    try:
        _, h = d.window_size()
    except Exception:
        h = 1600
    for node in root.iter("node"):
        text, desc = node.get("text", ""), node.get("content-desc", "")
        if not (text or desc):
            continue
        try:
            l, t, r, b = parse_bounds(node.get("bounds"))
        except Exception:
            continue
        if 60 < t < 0.5 * h:
            out.append(text or f"[{desc}]")
    return out


def get_center(d, retries=4, **sel):
    """Look the element up fresh each time (the live UI changes constantly, so
    held references go stale). Returns (cx, cy, bounds) or None."""
    for _ in range(retries):
        try:
            el = d(**sel)
            if el.exists(timeout=0.5):
                b = el.info["bounds"]
                return (b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2, b
        except Exception:
            pass
        time.sleep(0.3)
    return None


_last_action = ("start", 0, 0, time.time())


def press_tap(d, retries=4, **sel):
    """Tap the centre of an element with a short press (quick taps get ignored)."""
    global _last_action
    c = get_center(d, retries=retries, **sel)
    if not c:
        return False
    cx, cy, _ = c
    what = next(iter(sel.values()), "?")
    _last_action = (str(what)[:30], cx, cy, time.time())
    if VERBOSE:
        print(f"  tap '{str(what)[:30]}' at ({cx},{cy})")
    d.touch.down(cx, cy)
    time.sleep(0.15)
    d.touch.up(cx, cy)
    return True


def screen_texts(d, limit=45):
    """Snapshot of labelled elements on screen, for debugging."""
    root = ET.fromstring(d.dump_hierarchy())
    out = []
    for node in root.iter("node"):
        text, desc = node.get("text", ""), node.get("content-desc", "")
        if text or desc:
            out.append(text or f"[{desc}]")
    return out[:limit]


_debug_count = 0
_entered_shots = 0


def save_debug(d, label, note=""):
    """Save a screenshot and text snapshot so the failure can be inspected later."""
    global _debug_count
    if _debug_count >= MAX_DEBUG_FILES:
        return
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        name = time.strftime("%H%M%S") + "_" + label
        d.screenshot().save(os.path.join(DEBUG_DIR, name + ".png"))
        with open(os.path.join(DEBUG_DIR, name + ".txt"), "w", encoding="utf-8") as f:
            f.write(f"{label}\n{note}\n\n")
            f.write("\n".join(screen_texts(d, limit=80)))
        _debug_count += 1
        print(f"  Saved debug capture: debug\\{name}.png")
    except Exception as e:
        print("  (could not save debug capture:", e, ")")


def wait_for_panel(d, timeout=6.0):
    """Wait until the giveaway panel is showing (Terms link or the enter / entered text)."""
    end = time.time() + timeout
    while time.time() < end:
        texts = read_texts(d)
        if (any_match(texts, re.escape(PANEL_OPEN_TEXT))
                or any_match(texts, ENTER_BUTTON_REGEX)
                or any_match(texts, ENTERED_REGEX)):
            return True
        time.sleep(0.3)
    return False


def dismiss_popups(d):
    """Tap any known dismiss button (e.g. 'Another time'). Returns True if one was tapped."""
    tapped = False
    for rx in DISMISS_BUTTON_REGEXES:
        try:
            if any_match(read_texts(d), rx):
                if press_tap(d, retries=2, textMatches=rx):
                    print("  Dismissed popup")
                    time.sleep(1.0)
                    tapped = True
        except Exception:
            pass
    return tapped


def parse_bounds(b):
    l, t, r, bt = map(int, re.findall(r"\d+", b))
    return l, t, r, bt


def where(d):
    """With the Giveaway panel open: print bounds of everything in it and save a
    screenshot with the planned tap spot marked (red = enter button, green = others)."""
    from PIL import ImageDraw

    root = ET.fromstring(d.dump_hierarchy())
    parent = {c: p for p in root.iter() for c in p}
    items, inside = [], False
    for node in root.iter("node"):
        text, desc = node.get("text", ""), node.get("content-desc", "")
        if desc == CLOSE_DESC:
            inside = True
        if inside and (text or desc):
            items.append((text or f"[{desc}]", parse_bounds(node.get("bounds")),
                          node.get("clickable"), node))

    print(f"Screen size reported: {d.window_size()}")
    for label, b, c, node in items:
        print(f"{label[:45]!r:48} bounds={b} clickable={c}")
        if re.match(ENTER_BUTTON_REGEX, label):
            print("  Ancestors of the enter button (inner -> outer):")
            n = parent.get(node)
            for _ in range(6):
                if n is None or n.tag != "node":
                    break
                print(f"    {n.get('class')} bounds={parse_bounds(n.get('bounds'))} "
                      f"clickable={n.get('clickable')}")
                n = parent.get(n)

    img = d.screenshot()
    scale = img.width / d.window_size()[0]
    dr = ImageDraw.Draw(img)
    for label, b, c, node in items:
        is_btn = bool(re.match(ENTER_BUTTON_REGEX, label))
        l, t, r, bt = [v * scale for v in b]
        dr.rectangle([l, t, r, bt], outline="red" if is_btn else "lime", width=4)
        if is_btn:
            cx, cy = (l + r) / 2, (t + bt) / 2
            dr.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], outline="red", width=6)
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, time.strftime("%H%M%S") + "_where.png")
    img.save(path)
    print("\nSaved marked screenshot:", path)


def panel_open(d):
    return any_match(read_texts(d), re.escape(PANEL_OPEN_TEXT))


_last_close_hint = 0.0
_icon_close_disabled = False


def find_close_button(d):
    """Locate the panel's close / collapse button. Returns (cx, cy, how) or None.
    1) an element labelled close/collapse/minimize/dismiss/hide
    2) otherwise a small clickable icon INSIDE the giveaway panel card, top-right corner.
       The card is found from its 'Terms & Conditions' text. Icons outside the card (such as the
       stream's own down-arrow in the header, which LEAVES the stream) are never considered."""
    try:
        root = ET.fromstring(d.dump_hierarchy())
        w, h = d.window_size()
    except Exception:
        return None
    for node in root.iter("node"):
        label = (node.get("content-desc", "") or node.get("text", "")).strip()
        if re.fullmatch(r"(?i)(close|collapse|minimi[sz]e|dismiss|hide|shrink)", label):
            try:
                l, t, r, b = parse_bounds(node.get("bounds"))
            except Exception:
                continue
            if r > l and b > t:
                return (l + r) // 2, (t + b) // 2, f"label '{label}'"

    if _icon_close_disabled:
        return None
    parent = {c: p for p in root.iter() for c in p}
    anchor = None
    for node in root.iter("node"):
        if node.get("text", "").strip() == PANEL_OPEN_TEXT:
            anchor = node
            break
    if anchor is None:
        return None
    card = None
    n = parent.get(anchor)
    while n is not None and n.tag == "node":
        try:
            l, t, r, b = parse_bounds(n.get("bounds"))
        except Exception:
            break
        if (r - l) >= 0.8 * w and 0.1 * h <= (b - t) < 0.6 * h:
            card = (n, l, t, r, b)
            break
        n = parent.get(n)
    if card is None:
        return None
    cnode, cl, ct, cr, cb = card
    cands = []
    for node in cnode.iter("node"):
        cls = node.get("class", "")
        if not (node.get("clickable") == "true" or "ImageView" in cls or "ImageButton" in cls):
            continue
        try:
            l, t, r, b = parse_bounds(node.get("bounds"))
        except Exception:
            continue
        bw, bh = r - l, b - t
        cx, cy = (l + r) // 2, (t + b) // 2
        if (20 <= bw <= 200 and 20 <= bh <= 200
                and cx > cl + 0.75 * (cr - cl) and ct <= cy < ct + 0.35 * (cb - ct)):
            cands.append((cy, -cx, cx, cy))
    if cands:
        cands.sort()
        return cands[0][2], cands[0][3], "icon inside panel, top-right"
    return None


def close_panel(d):
    """Close the giveaway panel. NEVER presses Back: on a stream screen Back leaves the
    stream. If no close button can be found, the panel is left open and the problem is logged."""
    global _last_close_hint, _last_action
    if not check_whatnot_on_screen(d):       # nothing to close if Whatnot isn't even open
        return False
    found = find_close_button(d)
    if found:
        cx, cy, how = found
        _last_action = ("close panel", cx, cy, time.time())
        if VERBOSE:
            print(f"  tap close ({how}) at ({cx},{cy})")
        d.touch.down(cx, cy)
        time.sleep(0.15)
        d.touch.up(cx, cy)
        time.sleep(1.2)
        if how.startswith("icon") and not any_match(read_texts(d), ON_STREAM_REGEX):
            # That tap took us off the stream. Never tap this kind of icon again.
            global _icon_close_disabled
            _icon_close_disabled = True
            print("  WARNING: that close tap left the stream. Icon-based closing is now turned off;"
                  " the panel will be left open instead.")
            save_debug(d, "close_left_stream")
        return True
    if time.time() - _last_close_hint >= 60:
        _last_close_hint = time.time()
        print("  Could not find the panel's close button; leaving the panel open (not pressing Back).")
        try:
            root = ET.fromstring(d.dump_hierarchy())
            w, h = d.window_size()
            rows = []
            for node in root.iter("node"):
                try:
                    l, t, r, b = parse_bounds(node.get("bounds"))
                except Exception:
                    continue
                if t < 0.3 * h and (node.get("clickable") == "true" or "Image" in node.get("class", "")
                                    or node.get("content-desc") or node.get("text")):
                    rows.append((node.get("class", "").split(".")[-1], (l, t, r, b),
                                 node.get("clickable"), node.get("content-desc", ""), node.get("text", "")[:25]))
            print("  Top-of-screen elements:", rows[:14])
        except Exception:
            pass
        save_debug(d, "close_not_found")
    time.sleep(0.5)
    return False


def entered_confirmed(d, timeout=3.0):
    """True once the panel shows the 'You're in the Giveaway' confirmation."""
    end = time.time() + timeout
    while time.time() < end:
        if any_match(read_texts(d), ENTERED_REGEX):
            return True
        time.sleep(0.3)
    return False


def wait_entry_result(d, timeout=3.0):
    """After tapping enter: 'confirmed' if the confirmation text shows, 'button gone'
    if the enter button disappears (the panel often auto-collapses after entering)."""
    end = time.time() + timeout
    misses = 0
    while time.time() < end:
        texts = read_texts(d)
        if texts:                                  # ignore failed reads
            if any_match(texts, ENTERED_REGEX):
                return "confirmed"
            if any_match(texts, ENTER_BUTTON_REGEX):
                misses = 0
            else:
                misses += 1
                if misses >= 2:                    # gone twice in a row, not a flicker
                    return "button gone"
        time.sleep(0.25)
    return None


def verify_entered(d):
    """Re-open the panel and check it says 'You're in the Giveaway'."""
    if not panel_open(d):
        if not tap_giveaway_tile(d, retries=2):
            return False
        time.sleep(rnd(PANEL_LOAD_SECONDS))
        wait_for_panel(d)
    return entered_confirmed(d, 4.0)


def wait_until_stable(d, timeout=3.0):
    """Wait until the enter button stops moving (the panel animates in)."""
    last = None
    end = time.time() + timeout
    while time.time() < end:
        c = get_center(d, retries=1, textMatches=ENTER_BUTTON_REGEX)
        if c and last and abs(c[0] - last[0]) <= 3 and abs(c[1] - last[1]) <= 3:
            return True
        last = c
        time.sleep(0.4)
    return False


def click_enter(d, verbose=False):
    """Try several tap methods until the enter button changes/disappears.
    Returns the name of the method that worked, or None."""

    def button():
        el = d(textMatches=ENTER_BUTTON_REGEX)
        return el if el.exists(timeout=0.5) else None

    c = get_center(d, textMatches=ENTER_BUTTON_REGEX)
    if not c:
        if verbose:
            print("Enter button not found. Open the Giveaway panel first.")
        return None
    cx, cy, b = c
    if verbose:
        print(f"Button bounds={b} center=({cx},{cy})")

    def pos():
        c2 = get_center(d, retries=2, textMatches=ENTER_BUTTON_REGEX)
        return (c2[0], c2[1]) if c2 else (cx, cy)

    def s_element():
        button().click()

    def s_coords():
        x, y = pos()
        d.click(x, y)

    def s_parent():
        el = d.xpath('//*[contains(@text, "Enter Giveaway")]/ancestor::*[@clickable="true"][1]')
        if not el.exists:
            raise RuntimeError("no clickable parent found")
        el.click()

    def s_press():
        x, y = pos()
        d.touch.down(x, y)
        time.sleep(0.15)
        d.touch.up(x, y)

    def s_shell():
        x, y = pos()
        d.shell(f"input tap {x} {y}")

    strategies = [("press", s_press), ("element", s_element), ("coords", s_coords),
                  ("parent", s_parent), ("shell", s_shell)]
    for name, fn in strategies:
        try:
            fn()
        except Exception as e:
            if verbose:
                print(f"  {name}: error {e}")
            continue
        res = wait_entry_result(d, 3.0)
        if res:
            if verbose:
                print(f"  {name}: WORKED ({res})")
            return name
        if verbose:
            print(f"  {name}: no change after tap, trying next")
    return None


_last_hint = 0.0


def find_tile_center(d):
    """Find the giveaway tile by what it is and where it sits, so variants such as
    '$5 / Giveaway / 72 Entries' still match. Looks for text or content-desc containing
    'giveaway' in the upper-right part of the screen, ignoring panel buttons/banners."""
    try:
        root = ET.fromstring(d.dump_hierarchy())
        w, h = d.window_size()
    except Exception:
        return None
    for node in root.iter("node"):
        label = (node.get("text", "") or node.get("content-desc", "")).strip()
        if not label or not re.fullmatch(r"(?is).*giveaway.*", label):
            continue
        if re.fullmatch(TILE_EXCLUDE_REGEX, label.replace("\n", " ")):
            continue
        try:
            l, t, r, b = parse_bounds(node.get("bounds"))
        except Exception:
            continue
        cx, cy = (l + r) // 2, (t + b) // 2
        if cx > 0.5 * w and cy < 0.25 * h and r > l and b > t:
            return cx, cy, label
    return None


def tap_giveaway_tile(d, retries=2):
    """Tap the giveaway tile: exact 'Giveaway' text first, then the position/text search."""
    global _last_action
    if press_tap(d, retries=retries, text=GIVEAWAY_TILE_TEXT):
        return True
    found = find_tile_center(d)
    if not found:
        return False
    cx, cy, label = found
    _last_action = (label[:30], cx, cy, time.time())
    if VERBOSE:
        print(f"  tap tile variant '{label[:30]}' at ({cx},{cy})")
    d.touch.down(cx, cy)
    time.sleep(0.15)
    d.touch.up(cx, cy)
    return True


def hint_if_giveaway_text_missed(d):
    """If 'giveaway' text is on screen but no tile was tapped, say so (at most once a minute)
    and save a capture, so the exact wording can be added."""
    global _last_hint
    if time.time() - _last_hint < 60:
        return
    texts = [t for t in read_texts(d) if re.search("giveaway", t, re.I)]
    if texts:
        _last_hint = time.time()
        print("  Saw giveaway text but could not tap a tile:", texts[:6])
        save_debug(d, "giveaway_text_not_tapped", note=str(texts[:10]))


def skip_reason(d):
    """Return the panel text that marks this giveaway as one to skip (buyers-only etc.),
    or None. Also returns all panel text so it can be logged."""
    try:
        texts = panel_texts(d)
    except Exception:
        return None, []
    for t in texts:
        for rx in SKIP_PANEL_REGEXES:
            if re.fullmatch(rx, t.strip()):
                return t, texts
    return None, texts


def enter_giveaway_on_stream(d):
    """Returns 'entered', 'already_entered', 'skipped_restricted', 'no_giveaway',
    'panel_not_opened', 'nothing_to_enter' or 'click_failed'."""
    opened = False
    for attempt in range(2):
        dismiss_popups(d)          # a popup on top would swallow the tile tap
        if not tap_giveaway_tile(d, retries=2):
            # Tile not visible: either no giveaway here, or the panel is already open
            # (it covers the tile) and we were just slow to notice.
            if wait_for_panel(d, 2.0):
                opened = True
                break
            if attempt == 0:
                hint_if_giveaway_text_missed(d)
            return "no_giveaway" if attempt == 0 else "panel_not_opened"
        time.sleep(rnd(PANEL_LOAD_SECONDS))
        if wait_for_panel(d, 6.0):
            opened = True
            break
    if not opened:
        print("  Screen after tap:", screen_texts(d))
        save_debug(d, "panel_not_opened")
        if d(description=CLOSE_DESC).exists(timeout=0.3):
            close_panel(d)
        return "panel_not_opened"
    if VERBOSE:
        print("  Panel opened")

    state = None
    end = time.time() + 6
    while time.time() < end:
        texts = read_texts(d)
        if any_match(texts, ENTERED_REGEX):
            state = "entered"
            break
        if any_match(texts, ENTER_BUTTON_REGEX):
            state = "button"
            break
        time.sleep(0.3)

    if state == "entered":
        if VERBOSE:
            print("  Already in this giveaway")
        if panel_open(d):
            close_panel(d)
        return "already_entered"

    if state is None:
        # Panel opened but neither the enter button nor the confirmation appeared.
        print("  Panel contents:", panel_texts(d))
        save_debug(d, "nothing_to_enter", note=str(panel_texts(d)))
        if panel_open(d):
            close_panel(d)
        return "nothing_to_enter"

    reason, panel_all = skip_reason(d)
    if VERBOSE:
        print("  Panel text:", panel_all)
    if reason:
        print(f"  Skipping this giveaway (matched: {reason!r})")
        if panel_open(d):
            close_panel(d)
        return "skipped_restricted"

    if VERBOSE:
        print("  Enter button found, waiting for it to settle...")
    wait_until_stable(d)
    time.sleep(0.5)
    how = click_enter(d, verbose=VERBOSE)
    if VERBOSE:
        print(f"  Tap result: {how}")
    global _entered_shots
    if not how:
        save_debug(d, "click_failed")
    elif _entered_shots < DEBUG_FIRST_ENTERED:
        _entered_shots += 1
        time.sleep(1.0)
        verified = verify_entered(d)
        print(f"  Verified entry (re-opened panel): {verified}")
        save_debug(d, "entered_check", note=f"tap method: {how}, verified: {verified}")
    time.sleep(rnd(AFTER_ENTER_SECONDS))
    if panel_open(d):
        close_panel(d)
    return "entered" if how else "click_failed"


def set_debug_dir(device):
    global DEBUG_DIR
    safe_device = re.sub(r"[^A-Za-z0-9_.-]+", "_", device)
    DEBUG_DIR = os.path.join(BASE_DIR, "debug", safe_device)


def current_host(d):
    """Host name shown at the top of the stream (None if not readable). Diagnostics only."""
    try:
        root = ET.fromstring(d.dump_hierarchy())
        last = None
        for node in root.iter("node"):
            text, desc = node.get("text", ""), node.get("content-desc", "")
            if desc == "Rating":
                return last
            if text:
                last = text
    except Exception:
        pass
    return None


def run(device=DEVICE):
    global DEVICE
    DEVICE = device
    set_debug_dir(device)
    d = connect(device)
    print("Staying on this stream and joining every giveaway (no scrolling).")
    start = time.time()
    stats = {"entered": 0, "no_giveaway": 0, "nothing_to_enter": 0, "click_failed": 0}
    last_heartbeat = time.time()
    last_host = current_host(d)
    print("Current stream host:", last_host)
    while MAX_RUNTIME_MIN <= 0 or time.time() - start < MAX_RUNTIME_MIN * 60:
        try:
            host = current_host(d)
            if host and last_host and host != last_host:
                what, x, y, t = _last_action
                age = time.time() - t
                print(f"  STREAM CHANGED: {last_host} -> {host}  "
                      f"(last bot tap: '{what}' at ({x},{y}), {age:.0f}s ago)")
            if host:
                last_host = host
            if not check_whatnot_on_screen(d):
                time.sleep(rnd(STAY_POLL_SECONDS))
                continue
            dismiss_popups(d)
            if panel_open(d):
                close_panel(d)
            result = enter_giveaway_on_stream(d)
            stats[result] = stats.get(result, 0) + 1
            dismiss_popups(d)

            if result == "no_giveaway":
                if time.time() - last_heartbeat >= STAY_HEARTBEAT_SECONDS:
                    if any_match(read_texts(d), ON_STREAM_REGEX):
                        print(f"[{time.strftime('%H:%M:%S')}] watching, no giveaway right now  | totals: {stats}")
                    else:
                        print(f"[{time.strftime('%H:%M:%S')}] NOT ON A LIVE STREAM - open a stream in this emulator")
                    last_heartbeat = time.time()
                time.sleep(rnd(STAY_POLL_SECONDS))
            else:
                print(f"[{time.strftime('%H:%M:%S')}] {result}  | totals: {stats}")
                last_heartbeat = time.time()
                if result in ("entered", "already_entered", "skipped_restricted"):
                    time.sleep(rnd(STAY_COOLDOWN_SECONDS))
                else:
                    time.sleep(rnd(STAY_POLL_SECONDS))
        except Exception as e:
            print("Error:", e)
            save_debug(d, "error", note=str(e)[:500])
            time.sleep(3)
    print("Done.", stats)


def adb_devices():
    """Return ADB device serials whose state is 'device'. Uses adbutils (installed with
    uiautomator2) so adb does not need to be on PATH; falls back to the adb command."""
    try:
        from adbutils import adb
        return [d.serial for d in adb.device_list() if getattr(d, "state", "device") == "device"]
    except Exception:
        pass
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=True)
    devices = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append(fields[0])
    return devices


_merge_logged = set()


def device_identity(serial):
    """A value that is identical for every ADB address of ONE running emulator, or None if it
    can't be read. Uses the kernel's boot id: two ports that reach the same emulator return the
    same value, while separate emulators (even clones of each other) return different ones."""
    cmd = "cat /proc/sys/kernel/random/boot_id"
    out = ""
    try:
        from adbutils import adb
        out = adb.device(serial).shell(cmd)
    except Exception:
        try:
            out = subprocess.run(["adb", "-s", serial, "shell", cmd], capture_output=True,
                                 text=True, timeout=8).stdout
        except Exception:
            return None
    out = (out or "").strip()
    return out if 8 <= len(out) <= 64 and " " not in out else None


def _port_rank(serial):
    """Prefer MuMu 12's own ports (16384, 16416, ...) when one emulator has several addresses."""
    try:
        port = int(serial.rsplit(":", 1)[1])
    except Exception:
        return (2, 0)
    if port >= 16384 and (port - 16384) % 32 == 0:
        return (0, port)
    return (1, port)


def unique_devices(serials, log=None):
    """Drop addresses that lead to an emulator already in the list (for example MuMu's extra
    7555 / 5555 ports), so the bot never runs twice on the same emulator."""
    seen, keep = {}, []
    for serial in sorted(serials, key=_port_rank):
        ident = device_identity(serial)
        if ident is None:
            keep.append(serial)
        elif ident in seen:
            key = (serial, seen[ident])
            if log and key not in _merge_logged:
                _merge_logged.add(key)
                log(f"{serial} is the same emulator as {seen[ident]}; hiding it.")
        else:
            seen[ident] = serial
            keep.append(serial)
    return keep


def run_many(devices):
    """Start an isolated copy of this script for each device."""
    children = []
    script = os.path.abspath(__file__)
    try:
        for i, device in enumerate(devices):
            if i:
                time.sleep(STAGGER_SECONDS)
            adb_connect(device)
            print(f"Starting worker for {device}")
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--device", device]
            else:
                cmd = [sys.executable, script, "--device", device]
            children.append(subprocess.Popen(cmd))
        while children:
            children = [child for child in children if child.poll() is None]
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping emulator workers...")
        for child in children:
            child.terminate()
        for child in children:
            child.wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=DEVICE,
                    help="ADB serial for one emulator (default: %(default)s)")
    ap.add_argument("--devices", help="comma-separated ADB serials to run concurrently")
    ap.add_argument("--all", action="store_true",
                    help="run concurrently on every ADB device in state 'device'")
    ap.add_argument("--list-devices", action="store_true", help="list available ADB devices and exit")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--where", action="store_true",
                    help="with the Giveaway panel open, mark where the enter tap would land")
    ap.add_argument("--shot", action="store_true",
                    help="save one debug screenshot right now to confirm saving works")
    ap.add_argument("--test-enter", action="store_true",
                    help="with the Giveaway panel open, try each tap method and report")
    args = ap.parse_args()
    try:
        if args.list_devices:
            for device in adb_devices():
                print(device)
        elif args.all or args.devices:
            devices = unique_devices(adb_devices(), log=print) if args.all else [x.strip() for x in args.devices.split(",") if x.strip()]
            if not devices:
                ap.error("No usable devices found. Start the emulators and check `adb devices`.")
            run_many(devices)
        elif args.dump:
            dump(connect(args.device))
        elif args.where:
            set_debug_dir(args.device)
            where(connect(args.device))
        elif args.shot:
            set_debug_dir(args.device)
            save_debug(connect(args.device), "manual_test")
        elif args.test_enter:
            click_enter(connect(args.device), verbose=True)
        else:
            run(args.device)
    except KeyboardInterrupt:
        print("Stopped.")
        sys.exit(0)
