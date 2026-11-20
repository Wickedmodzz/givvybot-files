"""
GivvyBot - a simple window for whatnot_giveaway_bot.py

  * Lists the emulators your PC can see (MuMu Player etc.) and lets you pick which to run
  * Start / Stop buttons, with every emulator's log shown in the window
  * Each emulator runs in its own background process, so they don't interfere with each other

Run from source:   python whatnot_bot_gui.py
Build a program:   double-click setup_exe.bat   (see README_BUILD.txt)

NOTE: automating the app may violate Whatnot's Terms of Service and can get accounts restricted.
"""

import glob
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

FROZEN = getattr(sys, "frozen", False)
IS_WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

# Common MuMu Player ADB ports (MuMu 12 uses 16384, 16416, 16448, ... ; older versions 7555)
COMMON_PORTS = [16384 + 32 * i for i in range(8)] + [7555, 5555]
AUTO_SCAN_SECONDS = 10      # how often to look for new emulators (a full port scan every 3rd time)

APP_NAME = "GivvyBot"

# Black + green theme
C_BG = "#050706"        # window background
C_PANEL = "#0c1210"     # lists, entry boxes, buttons
C_LOG = "#020403"       # log background
C_GREEN = "#00e676"     # main accent
C_BRIGHT = "#69ffaa"    # hover / highlights
C_DIM = "#1d6b45"       # borders
C_TEXT = "#c9f7de"      # normal text
C_MUTED = "#5f8f76"     # small notes
C_OFF = "#35574a"       # disabled text


# ----------------------------------------------------------------------------------------
# Worker mode: this same program is started once per emulator with "--worker --device X"
# ----------------------------------------------------------------------------------------
def worker_main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass
    device = None
    if "--device" in sys.argv:
        device = sys.argv[sys.argv.index("--device") + 1]
    if not device:
        print("No device given.")
        sys.exit(2)
    import whatnot_giveaway_bot as bot

    try:
        bot.run(device)
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as e:  # keep the reason visible in the window
        print("Worker crashed:", repr(e))
        sys.exit(1)
    sys.exit(0)


# ----------------------------------------------------------------------------------------
# Helpers for the window
# ----------------------------------------------------------------------------------------
def find_adb():
    """Find an adb.exe (on PATH, or inside a MuMu Player install) and tell adbutils about it."""
    exe = shutil.which("adb")
    if exe:
        return exe
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), "C:\\", "D:\\"]
    patterns = [
        "*MuMu*\\shell\\adb.exe",
        "*MuMu*\\*\\shell\\adb.exe",
        "Netease\\*\\shell\\adb.exe",
        "Netease\\*\\*\\shell\\adb.exe",
        "*MuMu*\\adb.exe",
    ]
    for root in roots:
        if not root:
            continue
        for pat in patterns:
            hits = glob.glob(os.path.join(root, pat))
            if hits:
                os.environ["ADBUTILS_ADB_PATH"] = hits[0]
                return hits[0]
    return None


def worker_command(device):
    if FROZEN:
        return [sys.executable, "--worker", "--device", device]
    return [sys.executable, os.path.abspath(__file__), "--worker", "--device", device]


def kill_tree(proc):
    """Stop a worker and anything it started."""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW, timeout=10)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def hide_console_if_packaged():
    """The packaged program is a console build; hide its console window behind the GUI."""
    if FROZEN and IS_WINDOWS:
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        except Exception:
            pass


# ----------------------------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------------------------
def gui_main():
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    adb_path = find_adb()

    import whatnot_giveaway_bot as bot

    root = tk.Tk()
    version = os.environ.get("GIVVY_VERSION", "").strip()
    root.title(f"{APP_NAME} {version}" if version else APP_NAME)
    root.geometry("680x540")
    root.minsize(560, 440)
    root.configure(bg=C_BG)

    if IS_WINDOWS:      # lets the taskbar show our icon instead of Python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("givvy.givvybot")
        except Exception:
            pass
    logo_img = None
    try:
        from givvy_assets import ICON_PNG_B64, LOGO_PNG_B64
        logo_img = tk.PhotoImage(data=LOGO_PNG_B64)
        icon_img = tk.PhotoImage(data=ICON_PNG_B64)
        root.iconphoto(True, icon_img)
        root._keep_images = (logo_img, icon_img)       # tkinter drops images that nothing references
    except Exception:
        logo_img = None

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=C_BG, foreground=C_TEXT, fieldbackground=C_PANEL,
                    bordercolor=C_DIM, lightcolor=C_BG, darkcolor=C_BG, troughcolor=C_PANEL,
                    focuscolor=C_BG, font=("Segoe UI", 9))
    style.configure("TFrame", background=C_BG)
    style.configure("TLabel", background=C_BG, foreground=C_TEXT)
    style.configure("Muted.TLabel", foreground=C_MUTED)
    style.configure("Heading.TLabel", foreground=C_GREEN, font=("Segoe UI", 9, "bold"))
    style.configure("Status.TLabel", foreground=C_BRIGHT)
    style.configure("Line.TFrame", background=C_GREEN)
    style.configure("TButton", background=C_PANEL, foreground=C_GREEN, bordercolor=C_DIM,
                    padding=(10, 4), relief="flat")
    style.map("TButton",
              background=[("disabled", C_BG), ("pressed", C_DIM), ("active", "#12261c")],
              foreground=[("disabled", C_OFF), ("active", C_BRIGHT)],
              bordercolor=[("disabled", "#13241c"), ("active", C_GREEN)])
    style.configure("Accent.TButton", background=C_GREEN, foreground="#000000",
                    bordercolor=C_GREEN, padding=(16, 5), font=("Segoe UI", 9, "bold"))
    style.map("Accent.TButton",
              background=[("disabled", "#10281c"), ("pressed", C_DIM), ("active", C_BRIGHT)],
              foreground=[("disabled", C_OFF), ("active", "#000000")],
              bordercolor=[("disabled", "#10281c"), ("active", C_BRIGHT)])
    style.configure("TCheckbutton", background=C_BG, foreground=C_TEXT, indicatorcolor=C_PANEL,
                    bordercolor=C_DIM)
    style.map("TCheckbutton",
              background=[("active", C_BG)],
              foreground=[("active", C_BRIGHT)],
              indicatorcolor=[("selected", C_GREEN), ("pressed", C_DIM)])
    style.configure("TEntry", fieldbackground=C_PANEL, foreground=C_TEXT, insertcolor=C_GREEN,
                    bordercolor=C_DIM)

    log_queue = queue.Queue()
    ui_calls = queue.Queue()     # work that background threads want done on the window thread
    procs = {}          # device -> Popen
    state = {"running": False, "launching": 0, "active": False, "auto": True, "autostart": False}
    state_lock = threading.Lock()

    def launching(delta):
        """Count of workers currently being started (so "none running yet" is not mistaken for "all stopped")."""
        with state_lock:
            state["launching"] = max(0, state["launching"] + delta)

    # ---- top: emulator list ----
    header = ttk.Frame(root, padding=(12, 10, 12, 6))
    header.pack(fill="x")
    if logo_img is not None:
        ttk.Label(header, image=logo_img).pack(side="left")
    else:
        ttk.Label(header, text=APP_NAME, style="Heading.TLabel", font=("Segoe UI", 16, "bold")).pack(side="left")
    ttk.Label(header, text="auto-joins Whatnot giveaways", style="Muted.TLabel").pack(
        side="left", padx=(12, 0), pady=(10, 0))
    ttk.Frame(root, style="Line.TFrame", height=2).pack(fill="x", padx=12)

    top = ttk.Frame(root, padding=(12, 8, 12, 4))
    top.pack(fill="x")

    ttk.Label(top, text="EMULATORS  (select the ones to run)", style="Heading.TLabel").grid(
        row=0, column=0, sticky="w", pady=(0, 3))
    listbox = tk.Listbox(top, selectmode="extended", height=5, exportselection=False,
                         bg=C_PANEL, fg=C_TEXT, selectbackground=C_GREEN, selectforeground="#000000",
                         highlightthickness=1, highlightbackground=C_DIM, highlightcolor=C_GREEN,
                         relief="flat", borderwidth=0, activestyle="none", font=("Consolas", 10))
    listbox.grid(row=1, column=0, rowspan=4, sticky="nsew", padx=(0, 8))
    top.columnconfigure(0, weight=1)

    status = tk.StringVar(value="Ready.")
    adb_note = tk.StringVar(
        value=("adb: " + adb_path) if adb_path else "adb: using the copy bundled with the program")

    def log(msg):
        log_queue.put(msg if msg.endswith("\n") else msg + "\n")

    def set_devices(devices):
        keep = set(listbox.get(0, "end"))
        for d in devices:
            if d not in keep:
                listbox.insert("end", d)
        listbox.selection_set(0, "end")

    def refresh():
        def job():
            try:
                devs = bot.unique_devices(bot.adb_devices(), log=log)
            except Exception as e:
                log(f"Could not list devices: {e!r}")
                devs = []
            ui_calls.put(lambda: (set_devices(devs),
                                   status.set(f"Found {len(devs)} emulator(s).")))
        status.set("Looking for emulators...")
        threading.Thread(target=job, daemon=True).start()

    def scan():
        def job():
            log("Scanning common MuMu ports...")
            for port in COMMON_PORTS:
                try:
                    bot.adb_connect(f"127.0.0.1:{port}")
                except Exception:
                    pass
            try:
                devs = bot.unique_devices(bot.adb_devices(), log=log)
            except Exception as e:
                log(f"Could not list devices: {e!r}")
                devs = []
            log(f"Scan finished: {len(devs)} emulator(s) found.")
            ui_calls.put(lambda: (set_devices(devs),
                                   status.set(f"Found {len(devs)} emulator(s).")))
        status.set("Scanning...")
        threading.Thread(target=job, daemon=True).start()

    add_var = tk.StringVar()

    def add_manual():
        addr = add_var.get().strip()
        if not addr:
            return
        if ":" not in addr:
            addr = "127.0.0.1:" + addr
        if addr not in listbox.get(0, "end"):
            listbox.insert("end", addr)
        listbox.selection_set(listbox.size() - 1)
        add_var.set("")

    ttk.Button(top, text="Refresh", command=refresh).grid(row=1, column=1, sticky="ew", pady=1)
    ttk.Button(top, text="Scan for MuMu", command=scan).grid(row=2, column=1, sticky="ew", pady=1)
    add_row = ttk.Frame(top)
    add_row.grid(row=3, column=1, sticky="ew", pady=1)
    ttk.Entry(add_row, textvariable=add_var, width=14).pack(side="left")
    ttk.Button(add_row, text="Add", width=5, command=add_manual).pack(side="left", padx=(4, 0))
    auto_var = tk.BooleanVar(value=True)
    autostart_var = tk.BooleanVar(value=False)

    def on_auto():
        state["auto"] = bool(auto_var.get())

    def on_autostart():
        state["autostart"] = bool(autostart_var.get())

    ttk.Checkbutton(top, text="Auto-detect new emulators",
                    variable=auto_var, command=on_auto).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
    ttk.Checkbutton(top, text="Also start the bot on new emulators while running (needs a live stream open)",
                    variable=autostart_var, command=on_autostart).grid(row=6, column=0, columnspan=2, sticky="w")
    ttk.Label(top, textvariable=adb_note, style="Muted.TLabel").grid(
        row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ---- middle: controls ----
    mid = ttk.Frame(root, padding=(12, 6))
    mid.pack(fill="x")
    start_btn = ttk.Button(mid, text="Start", style="Accent.TButton")
    stop_btn = ttk.Button(mid, text="Stop", state="disabled")
    start_btn.pack(side="left")
    stop_btn.pack(side="left", padx=6)
    ttk.Button(mid, text="Clear log", command=lambda: logbox.delete("1.0", "end")).pack(side="left")
    ttk.Label(mid, textvariable=status, style="Status.TLabel").pack(side="left", padx=12)

    # ---- bottom: log ----
    logbox = scrolledtext.ScrolledText(
        root, height=12, font=("Consolas", 9), wrap="word",
        bg=C_LOG, fg="#7dffb0", insertbackground=C_GREEN, selectbackground=C_DIM,
        relief="flat", borderwidth=0, highlightthickness=1,
        highlightbackground=C_DIM, highlightcolor=C_GREEN)
    logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def scroll_to_end(_event=None):
        """Always show the newest line at the bottom of the log."""
        try:
            logbox.see("end")
            logbox.yview_moveto(1.0)
        except Exception:
            pass

    logbox.bind("<Configure>", lambda e: scroll_to_end())      # keep following when the window is resized

    def handle_found(devs):
        """Runs on the window thread: add any emulators we haven't seen yet."""
        known = set(listbox.get(0, "end"))
        for d in devs:
            if d in known:
                continue
            listbox.insert("end", d)
            listbox.selection_set(listbox.size() - 1)
            log(f"New emulator detected: {d}")
            if state["autostart"] and state["running"]:
                log(f"Auto-starting the bot on {d}")
                state["active"] = True
                launching(+1)

                def auto_spawn(dev=d):
                    try:
                        spawn_worker(dev)
                    finally:
                        launching(-1)

                threading.Thread(target=auto_spawn, daemon=True).start()
        status.set(f"{listbox.size()} emulator(s) in the list.")

    def auto_scan_loop():
        cycle = 0
        while True:
            time.sleep(AUTO_SCAN_SECONDS)
            if not state["auto"]:
                continue
            devs = []
            try:
                if cycle % 3 == 0:                 # try the usual MuMu ports every ~30 s
                    for port in COMMON_PORTS:
                        bot.adb_connect(f"127.0.0.1:{port}")
                devs = bot.unique_devices(bot.adb_devices(), log=log)
            except Exception:
                pass
            cycle += 1
            if devs:
                ui_calls.put(lambda devs=devs: handle_found(devs))

    def pump_logs():
        try:
            while True:
                ui_calls.get_nowait()()
        except queue.Empty:
            pass
        added = False
        try:
            for _ in range(300):
                line = log_queue.get_nowait()
                logbox.insert("end", line)
                added = True
        except queue.Empty:
            pass
        if added:
            if int(logbox.index("end-1c").split(".")[0]) > 5000:
                logbox.delete("1.0", "1000.0")
            scroll_to_end()
        # tidy up finished workers
        for dev, p in list(procs.items()):
            if p.poll() is not None:
                procs.pop(dev, None)
        if state["active"] and not state["launching"] and not procs:
            state["active"] = False
            state["running"] = False
            start_btn.config(state="normal")
            stop_btn.config(state="disabled")
            status.set("All emulators stopped.")
        root.after(150, pump_logs)

    def reader(device, proc):
        try:
            for raw in proc.stdout:
                log(raw.rstrip("\r\n"))
        except Exception as e:
            log(f"[{device}] log reader stopped: {e!r}")
        rc = proc.wait()
        log(f"[{device}] worker finished (exit code {rc})")

    def spawn_worker(dev):
        """Start the bot for one emulator (safe to call from a background thread)."""
        if dev in procs and procs[dev].poll() is None:
            return False
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        try:
            bot.adb_connect(dev)
        except Exception:
            pass
        log(f"Starting worker for {dev}")
        try:
            p = subprocess.Popen(
                worker_command(dev), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, creationflags=NO_WINDOW)
        except Exception as e:
            log(f"Could not start worker for {dev}: {e!r}")
            return False
        procs[dev] = p
        threading.Thread(target=reader, args=(dev, p), daemon=True).start()
        return True

    def start():
        devices = [listbox.get(i) for i in listbox.curselection()]
        if not devices:
            messagebox.showinfo(APP_NAME,
                                "Pick at least one emulator first.\n\n"
                                "Click 'Scan for MuMu' (or type an address like 127.0.0.1:16384 and press Add).")
            return
        state["running"] = True
        state["active"] = True         # buttons stay in "running" mode until every worker has ended
        launching(+1)                  # workers are still being started
        start_btn.config(state="disabled")
        stop_btn.config(state="normal")
        status.set(f"Starting {len(devices)} emulator(s)...")

        def launcher():
            try:
                launch_workers(devices)
            finally:
                launching(-1)

        def launch_workers(devices):
            for i, dev in enumerate(devices):
                if not state["running"]:
                    break
                if i:
                    time.sleep(bot.STAGGER_SECONDS)
                spawn_worker(dev)
            ui_calls.put(lambda: status.set(f"Running on {len(procs)} emulator(s)."))
            if not procs:
                state["running"] = False
                ui_calls.put(lambda: (start_btn.config(state="normal"), stop_btn.config(state="disabled")))

        threading.Thread(target=launcher, daemon=True).start()

    def stop():
        state["running"] = False
        stop_btn.config(state="disabled")
        status.set("Stopping...")
        for p in list(procs.values()):
            kill_tree(p)
        log("Stopped.")

    def on_close():
        state["running"] = False
        for p in list(procs.values()):
            kill_tree(p)
        root.destroy()

    start_btn.config(command=start)
    stop_btn.config(command=stop)
    root.protocol("WM_DELETE_WINDOW", on_close)

    threading.Thread(target=auto_scan_loop, daemon=True).start()
    update_note = os.environ.get("GIVVY_UPDATE_NOTE", "").strip()
    if update_note:
        log(update_note)
    log("Open a live stream in each emulator first, then press Start.")
    log("The bot stays on the stream you have open and joins its giveaways.")
    pump_logs()
    refresh()
    hide_console_if_packaged()     # window is up: now the black console can go
    root.mainloop()


def crash_report(exc_text):
    """Write the error next to the program, show it in a box, and keep the console readable."""
    base = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "givvybot_error.log")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(exc_text)
    except Exception:
        path = "(could not write the log file)"
    if "ModuleNotFoundError" in exc_text and "whatnot_giveaway_bot" in exc_text:
        exc_text = ("The file whatnot_giveaway_bot.py is missing.\n"
                    "Put it in the same folder as whatnot_bot_gui.py, then run again.\n\n") + exc_text
    print(exc_text)
    if IS_WINDOWS:
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 5)      # make the console visible again
            ctypes.windll.user32.MessageBoxW(
                0, "GivvyBot could not start.\n\n" + exc_text[-900:] + "\n\nThe full error was saved to:\n" + path,
                "GivvyBot", 0x10)
        except Exception:
            pass
    try:
        input("\nPress Enter to close...")
    except Exception:
        pass


def main():
    """Entry point (used by launcher.py and when run directly)."""
    if "--worker" in sys.argv:
        worker_main()
    else:
        try:
            gui_main()
        except SystemExit:
            raise
        except BaseException:
            import traceback
            crash_report(traceback.format_exc())
            sys.exit(1)


if __name__ == "__main__":
    main()
