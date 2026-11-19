"""
DiscCompressor GUI - drag videos onto the window and they're compressed below the target size.
Run with:  pythonw gui.pyw   (or double-click DiscCompressor.bat, or the packaged DiscCompressor.exe)
"""

import base64
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

FROZEN = getattr(sys, "frozen", False)          # running as a PyInstaller-built .exe
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import compress as engine  # noqa: E402

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAVE_DND = True
except ImportError:
    HAVE_DND = False

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv",
              ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".gif"}
SPEEDS = {"Fast (lower quality)": "veryfast",
          "Balanced": "medium",
          "Best quality (slow)": "slow"}
CODEC_CHOICES = {"H.264 (plays everywhere)": "h264",
                 "AV1 (better quality)": "av1"}
AUDIO_CHOICES = {"Keep audio": "keep",
                 "Mono audio": "mono",
                 "No audio": "none"}
SIZE_PRESETS = {"Discord (10 MB)": "10",
                "20 MB": "20",
                "Nitro Basic (50 MB)": "50",
                "Nitro (500 MB)": "500"}
CUSTOM = "Custom"
PRESET_SIZES = sorted(float(v) for v in SIZE_PRESETS.values())

SETTINGS_FILE = Path(os.environ.get("APPDATA", Path.home())) / "DiscCompressor" / "settings.json"
DEFAULTS = {"size": "10", "codec": "H.264 (plays everywhere)", "speed": "Balanced",
            "audio": "Keep audio", "gpu": False, "clip": True, "force": False, "autostart": True,
            "notify": True, "out_dir": "", "geometry": "900x680"}
SENDTO_LINK = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "SendTo" / "DiscCompressor.lnk"
INSTANCE_PORT = 47653   # localhost port used to pass files to an already-open window
# Toasts need a registered app ID; borrowing PowerShell's works without installing anything.
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
THUMB_BOX = (240, 150)
README = APP_DIR / "README.md"   # shown in the Help window; build_exe.py copies it next to the .exe

BG = "#1e1f22"
PANEL = "#2b2d31"
ACCENT = "#5865f2"
ACCENT_HOVER = "#6d78f5"
TEXT = "#f2f3f5"
MUTED = "#a3a6ad"
OK = "#3ba55d"
WARN = "#faa61a"
ERR = "#ed4245"

FINISHED = ("done", "skipped", "failed", "cancelled")
PENDING = ("waiting", "queued")
COLS = ("file", "size", "length", "trim", "plan", "status", "result")


# ---------- Windows helpers ----------

def copy_files_to_clipboard(paths):
    """Put files on the Windows clipboard (like Explorer's Copy) so Ctrl+V pastes them into Discord."""
    if os.name != "nt" or not paths:
        return False
    import ctypes
    import struct
    from ctypes import wintypes

    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE

    # DROPFILES header (offset to file list, drop point, fNC, fWide=1) + double-null-terminated UTF-16 list
    files = ("\0".join(str(Path(p).resolve()) for p in paths) + "\0\0").encode("utf-16-le")
    data = struct.pack("<IiiII", 20, 0, 0, 0, 1) + files

    h = k32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
    if not h:
        return False
    ctypes.memmove(k32.GlobalLock(h), data, len(data))
    k32.GlobalUnlock(h)

    for _ in range(10):                     # another app may briefly hold the clipboard
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        k32.GlobalFree(h)
        return False
    try:
        u32.EmptyClipboard()
        if not u32.SetClipboardData(15, h):  # CF_HDROP; clipboard owns h on success
            k32.GlobalFree(h)
            return False
        return True
    finally:
        u32.CloseClipboard()


def toplevel_hwnd(root):
    """Tk's winfo_id is an inner window; the taskbar button belongs to its parent frame."""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    u32.GetParent.argtypes = [wintypes.HWND]
    u32.GetParent.restype = wintypes.HWND
    return u32.GetParent(root.winfo_id()) or root.winfo_id()


def flash_window(root):
    """Flash the taskbar button until the window is brought to the front."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]

        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), toplevel_hwnd(root),
                          0x3 | 0xC, 0, 0)   # FLASHW_ALL | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def show_toast(title, body):
    """Windows notification (runs PowerShell in the background; silently does nothing on failure)."""
    if os.name != "nt":
        return
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType=WindowsRuntime] | Out-Null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        f"$x.Item(0).AppendChild($t.CreateTextNode({_ps_quote(title)})) | Out-Null;"
        f"$x.Item(1).AppendChild($t.CreateTextNode({_ps_quote(body)})) | Out-Null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        f"{_ps_quote(TOAST_APP_ID)}).Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    threading.Thread(target=lambda: subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, creationflags=engine.NO_WINDOW), daemon=True).start()


class Taskbar:
    """Progress bar on the app's taskbar button (ITaskbarList3), via raw COM calls."""
    NOPROGRESS, NORMAL = 0, 2

    def __init__(self, root):
        self.root = root
        self.ok = False
        if os.name != "nt":
            return
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            def guid(s):
                return ctypes.create_string_buffer(uuid.UUID(s).bytes_le, 16)

            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)
            # CLSID_TaskbarList, asking for ITaskbarList4 (extends ITaskbarList3: same vtable slots).
            self.ptr = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                guid("56FDF344-FD6D-11d0-958A-006097C9A090"), None, 1,
                guid("c43dc798-95d1-4bea-9030-bb99e2983a1a"), ctypes.byref(self.ptr))
            if hr != 0 or not self.ptr.value:
                return
            vtbl = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]

            def method(index, *argtypes):
                proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
                return proto(vtbl[index])

            method(3)(self.ptr)                                               # HrInit
            self._value = method(9, wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong)
            self._state = method(10, wintypes.HWND, ctypes.c_int)
            self.ok = True
        except Exception:
            self.ok = False

    def progress(self, fraction):
        if self.ok:
            hwnd = toplevel_hwnd(self.root)
            self._state(self.ptr, hwnd, self.NORMAL)
            self._value(self.ptr, hwnd, int(max(0.0, min(fraction, 1.0)) * 1000), 1000)

    def clear(self):
        if self.ok:
            self._state(self.ptr, toplevel_hwnd(self.root), self.NOPROGRESS)


# ---------- settings / integration ----------

def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    for key, choices in (("codec", CODEC_CHOICES), ("speed", SPEEDS), ("audio", AUDIO_CHOICES)):
        if s[key] not in choices:
            s[key] = DEFAULTS[key]
    return s


def save_settings(s):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except OSError:
        pass


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def set_sendto(enabled):
    """Add or remove the Explorer 'Send to → DiscCompressor' shortcut."""
    if not enabled:
        SENDTO_LINK.unlink(missing_ok=True)
        return True
    if FROZEN:
        target, args = Path(sys.executable), ""
    else:
        target = Path(sys.executable).with_name("pythonw.exe")
        if not target.exists():
            target = Path(sys.executable)
        args = f'"{APP_DIR / "gui.pyw"}"'
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(SENDTO_LINK)});"
        f"$s.TargetPath = {_ps_quote(target)};"
        f"$s.Arguments = {_ps_quote(args)};"
        f"$s.WorkingDirectory = {_ps_quote(APP_DIR)};"
        f"$s.Description = 'Compress videos for Discord';"
        f"$s.Save()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, creationflags=engine.NO_WINDOW)
    return r.returncode == 0 and SENDTO_LINK.exists()


def send_to_running_instance(paths):
    """If a DiscCompressor window is already open, hand it the files. Returns True if it took them."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as s:
            s.sendall(json.dumps(paths).encode("utf-8"))
        return True
    except OSError:
        return False


def path_key(p):
    """Comparable form of a path, for spotting the same file added twice."""
    return os.path.normcase(str(Path(p).resolve()))


def short_path(p, limit=48):
    s = str(p)
    return s if len(s) <= limit else "…" + s[-(limit - 1):]


def render_markdown(text, md):
    """Very small Markdown renderer for the help window: #/##/### headings, - and 1. lists
    (nested by indent), **bold**, `code` and ``` blocks. Enough for README.md."""
    def inline(s, base):
        for part in re.split(r"(\*\*.+?\*\*|`[^`]+`)", s):
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                text.insert("end", part[2:-2], (base, "bold"))
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                text.insert("end", part[1:-1], (base, "code"))
            elif part:
                text.insert("end", part, base)
        text.insert("end", "\n", base)

    in_block = False
    for line in md.splitlines():
        if line.startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            text.insert("end", line + "\n", "codeblock")
            continue
        heading = re.match(r"(#{1,3}) (.*)", line)
        item = re.match(r"(\s*)([-*]|\d+\.) (.*)", line)
        if heading:
            inline(heading.group(2), f"h{len(heading.group(1))}")
        elif item:
            level = "li2" if len(item.group(1)) >= 2 else "li"
            marker = "•" if item.group(2) in "-*" else item.group(2)
            text.insert("end", f"{marker}\t", level)
            inline(item.group(3), level)
        elif line.strip():
            inline(line.strip(), "p")
        else:
            text.insert("end", "\n", "gap")


# ---------- app ----------

class App:
    def __init__(self, root, server=None):
        self.root = root
        self.ui_queue = queue.Queue()     # worker/listener/prober -> UI messages
        self.jobs = queue.Queue()          # queued job ids, for the encoder thread
        self.probe_q = queue.Queue()       # job ids to read length/resolution for
        self.job_info = {}                 # id -> job dict
        self.current = None
        self.last_copied = None
        self.gpu_found = None              # {codec: (encoder, vendor)} once detected
        self.batch = {"done": 0, "skipped": 0, "failed": 0, "cancelled": 0}
        self.cfg = load_settings()
        self.snapshot = {}                 # current settings, read by the worker when a job starts

        root.title("DiscCompressor")
        root.geometry(self.cfg["geometry"])
        root.minsize(800, 600)
        root.configure(bg=BG)

        self._style()
        self._build()
        self._sync_settings()
        self.taskbar = Taskbar(root)

        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()
        threading.Thread(target=self._probe_loop, daemon=True).start()
        threading.Thread(target=self._detect_gpu, daemon=True).start()
        if server is not None:
            threading.Thread(target=self._listen, args=(server,), daemon=True).start()
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        try:
            engine.tools()
        except RuntimeError as e:
            messagebox.showerror("ffmpeg missing", str(e))

    # ---------- layout ----------

    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL,
                    bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL,
                    insertcolor=TEXT, font=("Segoe UI", 10))
        s.configure("Vertical.TScrollbar", background="#3a3c42", troughcolor=PANEL,
                    arrowcolor=MUTED, borderwidth=0, gripcount=0)
        s.map("Vertical.TScrollbar", background=[("active", "#4e5058")])
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Error.TLabel", foreground=ERR)
        s.configure("Thumb.TLabel", background=PANEL, foreground=MUTED, anchor="center")
        s.configure("TButton", background=PANEL, foreground=TEXT, borderwidth=0, padding=(12, 6))
        s.map("TButton", background=[("active", "#3a3c42"), ("disabled", PANEL)],
              foreground=[("disabled", "#5c5f66")])
        s.configure("Small.TButton", padding=(8, 2))
        s.configure("Accent.TButton", background=ACCENT)
        s.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", PANEL)])
        s.configure("TEntry", foreground=TEXT, padding=4)
        s.configure("TCombobox", arrowcolor=TEXT, background=PANEL, foreground=TEXT)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", TEXT)])
        s.configure("TSpinbox", arrowcolor=TEXT, foreground=TEXT, background=PANEL)
        s.map("TSpinbox", foreground=[("disabled", MUTED)], fieldbackground=[("disabled", BG)],
              arrowcolor=[("disabled", "#5c5f66")])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    rowheight=28, borderwidth=0)
        s.configure("Treeview.Heading", background=BG, foreground=MUTED, borderwidth=0,
                    font=("Segoe UI", 9, "bold"))
        s.map("Treeview", background=[("selected", "#404249")])
        s.map("Treeview.Heading", background=[("active", BG)])
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL,
                    borderwidth=0, thickness=8)
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def _check(self, parent, text, var, **kw):
        return tk.Checkbutton(parent, text=text, variable=var, bg=BG, fg=TEXT, selectcolor=PANEL,
                              activebackground=BG, activeforeground=TEXT,
                              disabledforeground="#5c5f66", bd=0, highlightthickness=0, **kw)

    def _build(self):
        pad = {"padx": 16}
        cfg = self.cfg

        # Drop zone
        self.drop = tk.Canvas(self.root, height=110, bg=PANEL, highlightthickness=0, cursor="hand2")
        self.drop.pack(fill="x", pady=(16, 12), **pad)
        self.drop.bind("<Configure>", lambda e: self._draw_drop())
        self.drop.bind("<Button-1>", lambda e: self._browse())
        self.drop.bind("<Enter>", lambda e: self._draw_drop(hover=True))
        self.drop.bind("<Leave>", lambda e: self._draw_drop())
        if HAVE_DND:
            for w in (self.drop, self.root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)
            self.drop.dnd_bind("<<DropEnter>>", lambda e: (self._draw_drop(hover=True), e.action)[1])
            self.drop.dnd_bind("<<DropLeave>>", lambda e: (self._draw_drop(), e.action)[1])

        # Row 1: size / codec / speed
        row = ttk.Frame(self.root)
        row.pack(fill="x", pady=(0, 8), **pad)
        ttk.Label(row, text="Target size").pack(side="left")
        self.size_var = tk.StringVar(value=cfg["size"])
        preset = next((k for k, v in SIZE_PRESETS.items() if v == cfg["size"].strip()), CUSTOM)
        self.preset_var = tk.StringVar(value=preset)
        preset_box = ttk.Combobox(row, textvariable=self.preset_var, state="readonly", width=18,
                                  values=[*SIZE_PRESETS, CUSTOM])
        preset_box.pack(side="left", padx=(8, 6))
        preset_box.bind("<<ComboboxSelected>>", lambda e: self._apply_preset(focus=True))
        self.size_spin = ttk.Spinbox(row, from_=1, to=2000, increment=1, width=6,
                                     textvariable=self.size_var)
        self.size_spin.pack(side="left", padx=(0, 4))
        ttk.Label(row, text="MB", style="Muted.TLabel").pack(side="left", padx=(0, 20))
        self._apply_preset()
        ttk.Label(row, text="Codec").pack(side="left")
        self.codec_var = tk.StringVar(value=cfg["codec"])
        ttk.Combobox(row, textvariable=self.codec_var, values=list(CODEC_CHOICES),
                     state="readonly", width=22).pack(side="left", padx=(8, 20))
        ttk.Label(row, text="Speed").pack(side="left")
        self.speed_var = tk.StringVar(value=cfg["speed"])
        ttk.Combobox(row, textvariable=self.speed_var, values=list(SPEEDS), state="readonly",
                     width=18).pack(side="left", padx=8)

        # Row 2: audio / output folder
        row2 = ttk.Frame(self.root)
        row2.pack(fill="x", pady=(0, 8), **pad)
        ttk.Label(row2, text="Audio").pack(side="left")
        self.audio_var = tk.StringVar(value=cfg["audio"])
        ttk.Combobox(row2, textvariable=self.audio_var, values=list(AUDIO_CHOICES),
                     state="readonly", width=12).pack(side="left", padx=(8, 20))
        ttk.Label(row2, text="Save to").pack(side="left")
        self.out_dir = cfg["out_dir"] or ""
        self.out_label = ttk.Label(row2, style="Muted.TLabel")
        self.out_label.pack(side="left", padx=8)
        ttk.Button(row2, text="Change…", style="Small.TButton",
                   command=self._choose_out_dir).pack(side="left")
        self.out_reset = ttk.Button(row2, text="Next to original", style="Small.TButton",
                                    command=lambda: self._set_out_dir(""))
        ttk.Button(row2, text="Help  (F1)", style="Small.TButton",
                   command=self._show_help).pack(side="right")
        self.root.bind("<F1>", lambda e: self._show_help())
        self.help_win = None
        self._show_out_dir()

        # Row 3: encoding toggles
        row3 = ttk.Frame(self.root)
        row3.pack(fill="x", pady=(0, 4), **pad)
        self.gpu_var = tk.BooleanVar(value=cfg["gpu"])
        self.gpu_check = self._check(row3, "Use graphics card (checking…)", self.gpu_var,
                                     state="disabled")
        self.gpu_check.pack(side="left", padx=(0, 20))
        self.force_var = tk.BooleanVar(value=cfg["force"])
        self._check(row3, "Re-encode files already under the size", self.force_var).pack(side="left")

        # Row 4: workflow toggles
        row4 = ttk.Frame(self.root)
        row4.pack(fill="x", pady=(0, 12), **pad)
        self.clip_var = tk.BooleanVar(value=cfg["clip"])
        self._check(row4, "Copy to clipboard when done", self.clip_var).pack(side="left", padx=(0, 16))
        self.notify_var = tk.BooleanVar(value=cfg["notify"])
        self._check(row4, "Notify when done", self.notify_var).pack(side="left", padx=(0, 16))
        self.auto_var = tk.BooleanVar(value=cfg["autostart"])
        self._check(row4, "Start automatically", self.auto_var,
                    command=self._refresh_buttons).pack(side="left", padx=(0, 16))
        self.sendto_var = tk.BooleanVar(value=SENDTO_LINK.exists())
        self._check(row4, "Add to \"Send to\" menu", self.sendto_var,
                    command=self._toggle_sendto).pack(side="left")

        for var in (self.size_var, self.codec_var, self.speed_var, self.audio_var, self.gpu_var,
                    self.force_var, self.clip_var, self.auto_var, self.notify_var):
            var.trace_add("write", lambda *a: self._sync_settings())

        # Progress + buttons (packed before the queue so they never get squeezed off-screen)
        bottom = ttk.Frame(self.root)
        bottom.pack(side="bottom", fill="x", pady=(12, 16), **pad)
        self.status = ttk.Label(bottom, text="Drop videos above to start. Right-click a file to "
                                             "trim or retry it. Press F1 for help.", style="Muted.TLabel")
        self.status.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=1.0)
        self.progress.pack(fill="x", pady=(6, 10))
        btns = ttk.Frame(bottom)
        btns.pack(fill="x")
        ttk.Button(btns, text="Add files…", command=self._browse).pack(side="left")
        self.start_btn = ttk.Button(btns, text="Start", style="Accent.TButton",
                                    command=self._start_waiting)
        self.start_btn.pack(side="left", padx=(8, 0))
        self.cancel_btn = ttk.Button(btns, text="Cancel current", command=self._cancel_current,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="Clear finished", command=self._clear_finished).pack(side="left")
        ttk.Button(btns, text="Show in folder", command=self._reveal).pack(side="right")
        ttk.Button(btns, text="Copy file", command=self._copy_selected).pack(side="right", padx=8)
        ttk.Button(btns, text="Trim…", command=self._trim_dialog).pack(side="right")

        # Queue
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(frame, columns=COLS, show="headings", selectmode="extended",
                                 height=5)
        for c, label, w, anchor, stretch in [
            ("file", "FILE", 190, "w", True), ("size", "ORIGINAL", 72, "e", False),
            ("length", "LENGTH", 62, "e", False), ("trim", "TRIM", 100, "center", False),
            ("plan", "ESTIMATE", 120, "w", False), ("status", "STATUS", 88, "w", False),
            ("result", "RESULT", 200, "w", True),
        ]:
            self.tree.heading(c, text=label, anchor=anchor)
            self.tree.column(c, width=w, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("done", foreground=OK)
        self.tree.tag_configure("failed", foreground=ERR)
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.tag_configure("muted", foreground=MUTED)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._reveal())
        self.tree.bind("<Delete>", lambda e: self._remove_selected())
        self.tree.bind("<Control-c>", lambda e: self._copy_selected())
        self.tree.bind("<Button-3>", self._context_menu)

        self._refresh_buttons()

    def _draw_drop(self, hover=False):
        c = self.drop
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        color = ACCENT if hover else "#4e5058"
        c.create_rectangle(6, 6, w - 6, h - 6, outline=color, width=2, dash=(8, 6))
        c.create_text(w / 2, h / 2 - 12, text="Drop videos here",
                      fill=TEXT, font=("Segoe UI", 15, "bold"))
        sub = "or click to browse" if HAVE_DND else \
              "click to browse  (drag & drop needs: pip install tkinterdnd2)"
        c.create_text(w / 2, h / 2 + 16, text=sub, fill=MUTED, font=("Segoe UI", 10))

    # ---------- help ----------

    def _show_help(self):
        """Show README.md in a scrollable window (one window; F1 again just brings it forward)."""
        if self.help_win is not None and self.help_win.winfo_exists():
            self.help_win.deiconify()
            self.help_win.lift()
            self.help_win.focus_force()
            return
        win = self.help_win = tk.Toplevel(self.root, bg=BG)
        win.title("DiscCompressor help")
        win.geometry(f"700x640+{self.root.winfo_rootx() + 40}+{self.root.winfo_rooty() + 20}")
        win.minsize(420, 300)
        win.bind("<Escape>", lambda e: win.destroy())

        sb = ttk.Scrollbar(win, orient="vertical")
        text = tk.Text(win, bg=BG, fg=TEXT, wrap="word", relief="flat", bd=0, highlightthickness=0,
                       padx=24, pady=16, font=("Segoe UI", 10), cursor="arrow",
                       selectbackground=ACCENT, yscrollcommand=sb.set, spacing1=1, spacing3=1,
                       tabs=("18p",))
        sb.config(command=text.yview)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)

        text.tag_configure("h1", font=("Segoe UI", 18, "bold"), spacing3=6)
        text.tag_configure("h2", font=("Segoe UI", 13, "bold"), foreground=ACCENT_HOVER,
                           spacing1=14, spacing3=4)
        text.tag_configure("h3", font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2)
        text.tag_configure("p", spacing3=2)
        text.tag_configure("li", lmargin1=8, lmargin2=26, spacing3=2)
        text.tag_configure("li2", lmargin1=28, lmargin2=46, spacing3=1)
        text.tag_configure("gap", font=("Segoe UI", 4))
        text.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        text.tag_configure("code", font=("Consolas", 10), background=PANEL)
        text.tag_configure("codeblock", font=("Consolas", 10), background=PANEL,
                           lmargin1=12, lmargin2=12)
        text.tag_raise("bold")   # bold inside headings keeps the heading size via the h* tags
        for h in ("h1", "h2", "h3"):
            text.tag_raise(h)

        try:
            render_markdown(text, README.read_text(encoding="utf-8"))
        except OSError:
            text.insert("end", "README.md wasn't found next to the app.\n\n", "h3")
            text.insert("end", "Drop videos on the window, wait for Done, then press Ctrl+V in "
                               "Discord. Right-click a file to trim or retry it.", "p")
        text.config(state="disabled")
        text.focus_set()

    # ---------- settings ----------

    def _apply_preset(self, focus=False):
        """A preset fills in its size and locks the box; Custom unlocks it for typing."""
        value = SIZE_PRESETS.get(self.preset_var.get())
        if value is None:
            self.size_spin.config(state="normal")
            if focus:
                self.size_spin.focus_set()
        else:
            self.size_var.set(value)
            self.size_spin.config(state="disabled")

    def _show_out_dir(self):
        if self.out_dir:
            self.out_label.config(text=short_path(self.out_dir), style="TLabel")
            self.out_reset.pack(side="left", padx=(8, 0))
        else:
            self.out_label.config(text="next to the original", style="Muted.TLabel")
            self.out_reset.pack_forget()

    def _choose_out_dir(self):
        folder = filedialog.askdirectory(title="Save compressed videos to",
                                         initialdir=self.out_dir or str(Path.home()))
        if folder:
            self._set_out_dir(str(Path(folder)))

    def _set_out_dir(self, folder):
        self.out_dir = folder
        self._show_out_dir()
        self._sync_settings()

    def _sync_settings(self):
        """Save settings, publish a snapshot the worker reads per job, and refresh estimates."""
        try:
            size = float(self.size_var.get())
            size = size if size > 0 else None
        except ValueError:
            size = None
        self.snapshot = {
            "size": size,
            "codec": CODEC_CHOICES.get(self.codec_var.get(), "h264"),
            "preset": SPEEDS.get(self.speed_var.get(), "medium"),
            "audio": AUDIO_CHOICES.get(self.audio_var.get(), "keep"),
            "gpu": self.gpu_var.get(),
            "force": self.force_var.get(),
            "out_dir": self.out_dir,
        }
        self.cfg.update(size=self.size_var.get(), codec=self.codec_var.get(),
                        speed=self.speed_var.get(), audio=self.audio_var.get(),
                        gpu=self.gpu_var.get(), force=self.force_var.get(),
                        clip=self.clip_var.get(), autostart=self.auto_var.get(),
                        notify=self.notify_var.get(), out_dir=self.out_dir)
        save_settings(self.cfg)
        self._update_gpu_label()
        self._refresh_estimates()

    def _detect_gpu(self):
        try:
            found = engine.detect_gpu_encoders()
        except Exception:
            found = {}
        self.ui_queue.put(("gpu", None, found))

    def _update_gpu_label(self):
        if self.gpu_found is None:
            return
        if not self.gpu_found:
            self.gpu_check.config(text="Use graphics card (none found)", state="disabled")
            return
        codec = CODEC_CHOICES.get(self.codec_var.get(), "h264")
        vendor = next(iter(self.gpu_found.values()))[1]
        if codec in self.gpu_found:
            text = f"Use graphics card ({self.gpu_found[codec][1]}, much faster)"
        else:
            text = f"Use graphics card ({vendor} can't encode {engine.CODECS[codec][0]}, will use CPU)"
        self.gpu_check.config(text=text, state="normal")

    def _toggle_sendto(self):
        want = self.sendto_var.get()
        if set_sendto(want):
            self.status.config(text="Added to Explorer: right-click videos → Send to → DiscCompressor."
                               if want else "Removed from the Send to menu.")
        else:
            self.sendto_var.set(SENDTO_LINK.exists())
            self.status.config(text="Couldn't change the Send to menu.")

    # ---------- estimates ----------

    def _estimate(self, jid, states=PENDING):
        """Fill the ESTIMATE column for a job that hasn't started yet."""
        job = self.job_info.get(jid)
        if job is None or job["state"] not in states or not job["info"]:
            return
        s = self.snapshot
        size = job["size"] or s["size"]
        if size is None:
            self._set(jid, plan="")
            return
        gpu = bool(s["gpu"] and self.gpu_found and s["codec"] in self.gpu_found)
        fits, text = engine.estimate(job["info"], size, s["codec"], gpu, job["start"], job["end"],
                                     s["audio"], s["force"])
        if job["size"]:
            text += f" @ {job['size']:g} MB"
        pending = job["state"] in PENDING        # don't recolour a row that's already encoding
        self._set(jid, plan=text, tag=("muted" if fits else "warn") if pending else None)

    def _refresh_estimates(self):
        if hasattr(self, "tree"):
            for jid in list(self.job_info):
                self._estimate(jid)

    def _probe_loop(self):
        """Read each added file's length/resolution in the background."""
        while True:
            jid = self.probe_q.get()
            if jid is None:
                return
            job = self.job_info.get(jid)
            if job is None:
                continue
            try:
                info, err = engine.probe(job["src"]), None
            except Exception as e:
                info, err = None, str(e)
            self.ui_queue.put(("info", jid, info, err))

    # ---------- adding files ----------

    def _on_drop(self, event):
        self._draw_drop()
        self.add_paths(self.root.tk.splitlist(event.data))
        return event.action

    def _browse(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        paths = filedialog.askopenfilenames(title="Choose videos",
                                            filetypes=[("Videos", exts), ("All files", "*.*")])
        self.add_paths(paths)

    def add_paths(self, paths):
        files = []
        for p in map(Path, paths):
            if p.is_dir():
                files += sorted(f for f in p.rglob("*")
                                if f.suffix.lower() in VIDEO_EXTS and "_compressed" not in f.stem)
            elif p.is_file():
                files.append(p)

        # Skip files that are already waiting or in progress (dropped twice, Send to twice...).
        active = {path_key(j["src"]) for j in self.job_info.values() if j["state"] not in FINISHED}
        dupes = 0
        for f in files:
            key = path_key(f)
            if key in active:
                dupes += 1
                continue
            active.add(key)
            jid = self.tree.insert("", "end", values=(f.name, f"{f.stat().st_size / engine.MB:.1f} MB",
                                                       "", "", "…", "", ""), tags=("muted",))
            # Output name and settings are decided when the job starts, not now.
            self.job_info[jid] = {"src": f, "dst": None, "start": None, "end": None,
                                  "info": None, "size": None, "used_size": None,
                                  "cancel": threading.Event(), "state": "waiting", "tag": "muted"}
            self.probe_q.put(jid)
            if self.auto_var.get():
                self._enqueue(jid)
            else:
                self._set(jid, "Waiting", "", "muted")
        if dupes:
            self.status.config(text=f"Skipped {dupes} file{'s' if dupes > 1 else ''} already in the list.")
        self._refresh_buttons()

    def _bring_to_front(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)   # Windows ignores plain lift() for background apps
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _enqueue(self, jid):
        job = self.job_info[jid]
        job["state"] = "queued"
        job["cancel"] = threading.Event()        # fresh flag: a stale cancel can't hit this run
        self._set(jid, "Queued", "", "muted")
        self._estimate(jid)
        self.jobs.put(jid)

    def _start_waiting(self):
        for jid, job in self.job_info.items():
            if job["state"] == "waiting":
                self._enqueue(jid)
        self._refresh_buttons()

    def _refresh_buttons(self):
        waiting = any(j["state"] == "waiting" for j in self.job_info.values())
        self.start_btn.config(state="normal" if waiting else "disabled")

    def _listen(self, server):
        """Accept file lists from later launches (Send to, dropping on the .bat)."""
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                data = b""
                conn.settimeout(2)
                try:
                    while chunk := conn.recv(65536):
                        data += chunk
                    paths = json.loads(data.decode("utf-8"))
                except (OSError, ValueError):
                    continue
            self.ui_queue.put(("add", None, paths))

    # ---------- worker thread ----------

    def _work(self):
        while True:
            jid = self.jobs.get()
            if jid is None:
                return
            job = self.job_info.get(jid)
            if job is None or job["state"] != "queued":   # removed or re-trimmed meanwhile
                continue
            s = dict(self.snapshot)                       # settings as they are *now*
            size = job["size"] or s["size"]
            job["used_size"] = size
            post = lambda kind, *a: self.ui_queue.put((kind, jid, *a))
            self.ui_queue.put(("start", jid))
            if size is None:
                post("finish", "failed", "target size must be a positive number")
                continue
            try:
                if s["out_dir"]:
                    Path(s["out_dir"]).mkdir(parents=True, exist_ok=True)
                job["dst"] = engine.default_output(job["src"], s["out_dir"] or None)
                status, msg = engine.compress(
                    job["src"], job["dst"], size, s["preset"], s["force"], s["codec"],
                    gpu=s["gpu"], start=job["start"], end=job["end"], audio=s["audio"],
                    info=job["info"],
                    log=lambda m: post("log", m),
                    on_progress=lambda f: post("progress", f),
                    cancel=job["cancel"])
                post("finish", status, msg)
            except engine.Cancelled:
                post("finish", "cancelled", "")
            except Exception as e:  # keep the worker alive whatever happens
                post("finish", "failed", str(e))

    # ---------- UI updates (main thread) ----------

    def _poll(self):
        try:
            while True:
                self._handle(*self.ui_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _set(self, jid, status=None, result=None, tag=None, plan=None):
        """Update a row. Columns not passed keep their value; length/trim come from the job."""
        if not self.tree.exists(jid):
            return
        vals = list(self.tree.item(jid, "values"))
        job = self.job_info.get(jid)
        for col, val in (("status", status), ("result", result), ("plan", plan)):
            if val is not None:
                vals[COLS.index(col)] = val
        if job:
            vals[COLS.index("trim")] = self._trim_text(job)
            if job["info"]:
                vals[COLS.index("length")] = engine.format_time(job["info"]["duration"])
            if tag is not None:
                job["tag"] = tag
            tag = job["tag"]
        self.tree.item(jid, values=vals, tags=(tag,) if tag else ())

    @staticmethod
    def _trim_text(job):
        if job["start"] is None and job["end"] is None:
            return ""
        a = engine.format_time(job["start"] or 0)
        b = engine.format_time(job["end"]) if job["end"] is not None else "end"
        return f"{a}–{b}"

    def _pending_count(self):
        return sum(1 for j in self.job_info.values() if j["state"] == "queued")

    def _handle(self, kind, jid, *args):
        if kind == "add":
            self._bring_to_front()
            self.add_paths(args[0])
            return
        if kind == "gpu":
            self.gpu_found = args[0]
            self._update_gpu_label()
            self._refresh_estimates()
            return
        job = self.job_info.get(jid)
        if job is None:
            return
        name = job["src"].name
        if kind == "info":
            info, err = args
            job["info"] = info
            if info:
                self._set(jid)
                self._estimate(jid, (*PENDING, "running"))   # it may have started already
            elif job["state"] in PENDING:
                self._set(jid, plan="can't read", tag="warn")
                self.status.config(text=f"{name}: {err}")
        elif kind == "start":
            self.current = jid
            job["state"] = "running"
            self._set(jid, "Compressing", "", "")
            self.status.config(text=f"Compressing {name}…")
            self.progress["value"] = 0
            self.cancel_btn.config(state="normal")
        elif kind == "log":
            self.status.config(text=f"{name}: {args[0]}")
        elif kind == "progress":
            self.progress["value"] = args[0]
            self._set(jid, f"{args[0] * 100:.0f}%")
            finished = sum(self.batch.values())
            self.taskbar.progress((finished + args[0]) / (finished + self._pending_count() + 1))
        elif kind == "finish":
            self._finish(jid, job, *args)

    def _finish(self, jid, job, state, msg):
        job["state"] = state
        self.batch[state] = self.batch.get(state, 0) + 1
        copied = None
        if state == "done":
            self._set(jid, "Done", msg, "done")
            if self.clip_var.get() and copy_files_to_clipboard([job["dst"]]):
                copied = job["dst"]
        elif state == "skipped":
            # Already small enough: the original is what you'd send, so copy that instead.
            if self.clip_var.get() and copy_files_to_clipboard([job["src"]]):
                copied = job["src"]
            self._set(jid, "Skipped", msg + (", original copied" if copied else ""), "muted")
        elif state == "cancelled":
            self._set(jid, "Cancelled", "right-click → Retry", "muted")
        else:
            bigger = self._next_size(job)
            hint = f" (right-click → Retry at {bigger:g} MB)" if bigger and "too long" in msg else ""
            self._set(jid, "Failed", msg + hint, "failed")
        if copied:
            self.last_copied = copied.name
        self.current = None
        self.cancel_btn.config(state="disabled")
        self.progress["value"] = 0

        remaining = self._pending_count()
        text = f"{remaining} remaining…" if remaining else "All done."
        if self.last_copied:
            text += f"  {self.last_copied} copied, press Ctrl+V in Discord to send it."
        self.status.config(text=text)
        if not remaining:
            self._batch_finished()

    def _batch_finished(self):
        """Queue is empty: clear taskbar progress and let the user know if they're elsewhere."""
        b, self.batch = self.batch, {"done": 0, "skipped": 0, "failed": 0, "cancelled": 0}
        self.taskbar.clear()
        if b["done"] + b["skipped"] + b["failed"] == 0:      # only cancellations
            return
        if self.root.focus_displayof() is not None:          # user is looking at the window
            return
        flash_window(self.root)
        if self.notify_var.get():
            parts = [f"{n} {word}" for n, word in ((b["done"], "compressed"),
                                                   (b["skipped"], "already small"),
                                                   (b["failed"], "failed")) if n]
            body = ", ".join(parts) + "."
            if b["done"] + b["skipped"] and self.clip_var.get():
                body += " Copied, ready to paste into Discord."
            show_toast("DiscCompressor finished", body)

    # ---------- retry ----------

    def _next_size(self, job):
        """Smallest preset above the size last tried that's big enough for the clip's length."""
        used = job["used_size"] or self.snapshot["size"] or 0
        need = 0
        if job["info"]:
            dur = (job["end"] or job["info"]["duration"]) - (job["start"] or 0)
            need = engine.min_size_mb(job["info"], dur, self.snapshot["audio"])
        return next((s for s in PRESET_SIZES if s > used and s >= need), None)

    def _retryable(self):
        return [jid for jid in self.tree.selection()
                if self.job_info.get(jid, {}).get("state") in ("failed", "cancelled")]

    def _retry(self, bigger=False):
        for jid in self._retryable():
            job = self.job_info[jid]
            if bigger:
                job["size"] = self._next_size(job)
            self._enqueue(jid)
        self._refresh_buttons()

    # ---------- trimming ----------

    def _trim_dialog(self):
        sel = self.tree.selection()
        if not sel:
            self.status.config(text="Select a file in the list to trim it.")
            return
        jid = sel[0]
        job = self.job_info.get(jid)
        if job is None:
            return
        if job["state"] == "running":
            self.status.config(text="Can't trim a file while it's compressing. Cancel it first.")
            return
        if job["info"] is None:
            try:
                job["info"] = engine.probe(job["src"])
            except Exception as e:
                messagebox.showerror("Can't read video", str(e))
                return
        info = job["info"]
        dur = info["duration"]

        win = tk.Toplevel(self.root, bg=BG, padx=20, pady=16)
        win.title("Trim")
        win.resizable(False, False)
        win.transient(self.root)
        ttk.Label(win, text=job["src"].name, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(win, text=f"Length {engine.format_time(dur)}", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))

        # Start / end columns, each with a frame preview above its time box.
        start_var = tk.StringVar(value=engine.format_time(job["start"] or 0))
        end_var = tk.StringVar(value=engine.format_time(job["end"] if job["end"] is not None else dur))
        blank = tk.PhotoImage(width=THUMB_BOX[0], height=THUMB_BOX[1])
        thumbs, entries = {}, {}
        for col, (key, label, var) in enumerate((("start", "Start", start_var), ("end", "End", end_var))):
            cell = ttk.Frame(win)
            cell.grid(row=2, column=col, padx=(0, 12) if col == 0 else 0, sticky="n")
            thumbs[key] = ttk.Label(cell, image=blank, style="Thumb.TLabel", compound="center",
                                    text="loading…")
            thumbs[key].image = blank
            thumbs[key].pack()
            line = ttk.Frame(cell)
            line.pack(fill="x", pady=(6, 0))
            ttk.Label(line, text=label).pack(side="left")
            entries[key] = ttk.Entry(line, textvariable=var, width=12)
            entries[key].pack(side="left", padx=8)
        ttk.Label(win, text="Times like 90, 1:30 or 1:02:03.5", style="Muted.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        length = ttk.Label(win, style="Muted.TLabel")
        length.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def parsed():
            a = engine.parse_time(start_var.get()) or 0.0
            b = engine.parse_time(end_var.get())
            b = dur if b is None else b
            if not (0 <= a < b) or b > dur + 0.05:
                raise ValueError
            return a, min(b, dur)

        # Thumbnails are grabbed by ffmpeg in a thread; only the latest request per side is shown.
        results = queue.Queue()
        cache, wanted, pending = {}, {}, {"after": None}

        def show(key, png):
            if png is None:
                thumbs[key].config(image=blank, text="no preview")
                thumbs[key].image = blank
                return
            img = tk.PhotoImage(data=base64.b64encode(png))
            thumbs[key].config(image=img, text="")
            thumbs[key].image = img

        def fetch(key, t):
            wanted[key] = t
            if t in cache:
                show(key, cache[t])
                return
            thumbs[key].config(text="loading…")

            def run():
                try:
                    png = engine.grab_frame(job["src"], t, THUMB_BOX, info["hdr"])
                except Exception:
                    png = None
                results.put((key, t, png))
            threading.Thread(target=run, daemon=True).start()

        def collect():
            if not win.winfo_exists():
                return
            try:
                while True:
                    key, t, png = results.get_nowait()
                    cache[t] = png
                    if wanted.get(key) == t:
                        show(key, png)
            except queue.Empty:
                pass
            win.after(100, collect)

        def refresh_thumbs():
            pending["after"] = None
            if not win.winfo_exists():
                return
            try:
                a, b = parsed()
            except ValueError:
                return
            fetch("start", round(a, 1))
            fetch("end", round(max(0.0, b - 0.05), 1))   # last frame before the cut

        def update_length(*_):
            try:
                a, b = parsed()
                length.config(text=f"Clip length {engine.format_time(b - a)}", style="Muted.TLabel")
            except ValueError:
                length.config(text="Start must be before end, and within the video.",
                              style="Error.TLabel")
                return
            if pending["after"]:
                win.after_cancel(pending["after"])
            pending["after"] = win.after(400, refresh_thumbs)   # wait until typing pauses

        start_var.trace_add("write", update_length)
        end_var.trace_add("write", update_length)
        update_length()
        collect()

        def apply(clear=False):
            if clear:
                a, b = 0.0, dur
            else:
                try:
                    a, b = parsed()
                except ValueError:
                    return
            job["start"] = a if a > 0.05 else None
            job["end"] = b if b < dur - 0.05 else None
            win.destroy()
            # Finished files get redone with the new trim; queued/waiting ones just update.
            if job["state"] in FINISHED:
                if self.auto_var.get():
                    self._enqueue(jid)
                else:
                    job["state"] = "waiting"
                    self._set(jid, "Waiting", "", "muted")
            self._set(jid)
            self._estimate(jid)
            self._refresh_buttons()

        btns = ttk.Frame(win)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Remove trim", command=lambda: apply(clear=True)).pack(side="left")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=8)
        ttk.Button(btns, text="Apply", style="Accent.TButton", command=apply).pack(side="left")
        win.bind("<Return>", lambda e: apply())
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        entries["start"].focus_set()
        entries["start"].select_range(0, "end")

    # ---------- actions ----------

    def _context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        menu = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT,
                       activeforeground=TEXT, bd=0)
        retryable = self._retryable()
        if retryable:
            menu.add_command(label="Retry", command=self._retry)
            sizes = {self._next_size(self.job_info[j]) for j in retryable}
            if None not in sizes:
                label = f"Retry at {next(iter(sizes)):g} MB" if len(sizes) == 1 else "Retry at next size up"
                menu.add_command(label=label, command=lambda: self._retry(bigger=True))
            menu.add_separator()
        menu.add_command(label="Trim…", command=self._trim_dialog)
        menu.add_command(label="Copy file", command=self._copy_selected)
        menu.add_command(label="Show in folder", command=self._reveal)
        menu.add_separator()
        menu.add_command(label="Remove", command=self._remove_selected)
        menu.tk_popup(event.x_root, event.y_root)

    def _cancel_current(self):
        if self.current and self.current in self.job_info:
            self.job_info[self.current]["cancel"].set()
            self.status.config(text="Cancelling…")

    def _remove_selected(self):
        for jid in self.tree.selection():
            job = self.job_info.get(jid)
            if job and job["state"] == "running":
                continue
            self.job_info.pop(jid, None)
            self.tree.delete(jid)
        self._refresh_buttons()
        if not self._pending_count() and self.current is None:
            self.taskbar.clear()

    def _clear_finished(self):
        for jid, job in list(self.job_info.items()):
            if job["state"] in FINISHED and self.tree.exists(jid):
                self.tree.delete(jid)
                del self.job_info[jid]

    def _output_of(self, jid):
        """The compressed file for a row, or the original if it was skipped / not done yet."""
        job = self.job_info.get(jid)
        if job is None:
            return None
        return job["dst"] if job["dst"] and job["dst"].exists() else job["src"]

    def _copy_selected(self):
        sel = [j for j in self.tree.selection()
               if self.job_info.get(j, {}).get("state") not in ("failed", "cancelled")]
        paths = [p for p in map(self._output_of, sel) if p]
        if not paths:
            self.status.config(text="Select a compressed file in the list to copy it.")
            return
        if copy_files_to_clipboard(paths):
            what = paths[0].name if len(paths) == 1 else f"{len(paths)} files"
            self.status.config(text=f"{what} copied, press Ctrl+V in Discord to send.")
        else:
            self.status.config(text="Couldn't access the clipboard, try again.")

    def _reveal(self):
        sel = self.tree.selection()
        target = self._output_of(sel[0]) if sel else None
        if target is None:
            return
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])

    def _on_close(self):
        if self.current and not messagebox.askyesno(
                "Quit?", "A video is still compressing. Stop it and quit?"):
            return
        self.cfg["geometry"] = self.root.geometry()
        save_settings(self.cfg)
        for job in self.job_info.values():
            job["cancel"].set()
            if job["state"] == "queued":
                job["state"] = "removed"
        self.jobs.put(None)
        self.probe_q.put(None)
        self.worker.join(timeout=5)   # lets the engine kill ffmpeg and delete the partial file
        self.taskbar.clear()
        self.root.destroy()


def main():
    paths = [str(Path(p).resolve()) for p in sys.argv[1:]]
    # One window: if DiscCompressor is already open, give it the files and exit.
    if paths and send_to_running_instance(paths):
        return
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", INSTANCE_PORT))
        server.listen(5)
    except OSError:
        if send_to_running_instance(paths):   # open window without files: just focus it
            return
        server = None                         # port taken by something else; run standalone

    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    app = App(root, server)
    if paths:
        root.after(200, lambda: app.add_paths(paths))
    root.mainloop()


if __name__ == "__main__":
    main()
