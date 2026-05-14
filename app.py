import sys
import os
import ctypes
import multiprocessing
multiprocessing.freeze_support()

# ── Make python_embed packages importable in the frozen app ──────────────────
# resemble-enhance (and future pip-installed packages) live in python_embed's
# site-packages. Append (not prepend) so PyInstaller-bundled packages keep
# priority and only packages NOT in the bundle are resolved from python_embed.
_embed_sp = os.path.join(
    os.environ.get("APPDATA", ""), "TTS Studio",
    "python_embed", "Lib", "site-packages",
)
if os.path.isdir(_embed_sp) and _embed_sp not in sys.path:
    sys.path.append(_embed_sp)
del _embed_sp

# ── Suppress console windows for ALL subprocesses (torch, resemble-enhance, etc.) ──
# Any library that calls subprocess.Popen without CREATE_NO_WINDOW would pop a
# visible CMD window on Windows. Patch it globally before any imports happen.
if sys.platform == "win32":
    import subprocess as _sp
    _popen_init_orig = _sp.Popen.__init__
    def _popen_no_window(self, *args, _orig=_popen_init_orig, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000
        _orig(self, *args, **kwargs)
    _sp.Popen.__init__ = _popen_no_window
    del _sp, _popen_init_orig, _popen_no_window

import customtkinter as ctk
from kokoro_onnx import Kokoro
import sounddevice as sd
import soundfile as sf
import threading
import numpy as np
import json
import re
import time
import subprocess
import tempfile
import webbrowser
from tkinter import filedialog, messagebox
from scipy.signal import butter, sosfilt
import tkinter as tk
from datetime import datetime
from text_cleaner import clean_text, preview_clean
from settings_window import open_settings_window, load_settings, save_settings, DEFAULT_SETTINGS
from pronunciation import open_pronunciation_window, apply_pronunciation
from tts_utils import (
    format_time, chunk_text, parse_dialogue,
    _srt_time, _wrap_for_subtitle, build_srt,
    fmt_err, estimate_audio_duration, GenerationCancelled,
    history_card_preview, history_card_voice_label,
)
from clone_library import (
    load_clone_library   as _lib_load,
    save_clone_library   as _lib_save,
    add_clone_to_library as _lib_add,
    rename_clone_in_library as _lib_rename,
)
from audio_utils import trim_silence, enhance_audio
import license as _lic

# ── Resource path helper (PyInstaller one-dir compatible) ─────────────────────
def _res(relative_path):
    """Return the absolute path to a bundled resource.
    Works both in development (relative to this file) and when frozen by
    PyInstaller one-dir builds (sys._MEIPASS == directory of the .exe).
    """
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)

# ── In-memory caches (eliminates repeated disk reads in hot paths) ────────────
_settings_cache     = None
_calibration_cache  = None
_clone_cache        = None

def _get_settings():
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = load_settings()
    return _settings_cache

def _save_settings(s):
    global _settings_cache
    _settings_cache = s
    save_settings(s)

def _get_calibration():
    global _calibration_cache
    if _calibration_cache is None:
        _calibration_cache = load_calibration()
    return _calibration_cache

def _invalidate_calibration():
    global _calibration_cache
    _calibration_cache = None

def _get_clone_library():
    global _clone_cache
    if _clone_cache is None:
        _clone_cache = _lib_load(CLONE_DIR, CLONE_INDEX)
    return _clone_cache

def _invalidate_clone_cache():
    global _clone_cache
    _clone_cache = None

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION          = "1.3.3"
GITHUB_REPO      = "tagee1/VoxWild"
MAX_HISTORY      = 10

# ── User data directory (%APPDATA%\TTS Studio) ────────────────────────────────
_USER_DIR        = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TTS Studio")
os.makedirs(_USER_DIR, exist_ok=True)

PROFILES_FILE    = os.path.join(_USER_DIR, "tts_profiles.json")
CALIBRATION_FILE = os.path.join(_USER_DIR, "calibration.json")
SETTINGS_FILE    = os.path.join(_USER_DIR, "settings.json")
CLONE_DIR        = os.path.join(_USER_DIR, "voice_clones")
CLONE_INDEX      = os.path.join(CLONE_DIR, "library.json")
HISTORY_JSON     = os.path.join(_USER_DIR, "history.json")
HISTORY_AUDIO    = os.path.join(_USER_DIR, "history_audio")
os.makedirs(HISTORY_AUDIO, exist_ok=True)

# ── One-time migration: copy existing user files from app dir to %APPDATA% ────
def _migrate_user_data():
    """Copy legacy files from the app directory to %APPDATA% on first launch."""
    if getattr(sys, "frozen", False):
        return  # frozen installs have no legacy files to migrate
    _app_dir = os.path.dirname(os.path.abspath(__file__))
    _migrations = [
        ("tts_profiles.json",       PROFILES_FILE),
        ("calibration.json",        CALIBRATION_FILE),
        ("settings.json",           SETTINGS_FILE),
    ]
    import shutil as _shutil
    for _src_name, _dst in _migrations:
        _src = os.path.join(_app_dir, _src_name)
        if os.path.exists(_src) and not os.path.exists(_dst):
            try:
                _shutil.copy2(_src, _dst)
            except OSError:
                pass
    # Migrate voice_clones directory
    _src_clones = os.path.join(_app_dir, "voice_clones")
    if os.path.isdir(_src_clones) and not os.path.exists(CLONE_INDEX):
        try:
            _shutil.copytree(_src_clones, CLONE_DIR, dirs_exist_ok=True)
        except OSError:
            pass

_migrate_user_data()

# ── Crash logger ──────────────────────────────────────────────────────────────
import traceback as _traceback
from datetime import datetime as _dt

_CRASH_LOG = os.path.join(_USER_DIR, "crashes.log")
_MAX_CRASH_LOG_BYTES = 512 * 1024  # 512 KB — rotate when exceeded

def _log_crash(e, tb_str=None):
    """Append a crash entry to crashes.log. Never raises."""
    try:
        # Rotate log if it's grown too large
        if os.path.exists(_CRASH_LOG) and os.path.getsize(_CRASH_LOG) > _MAX_CRASH_LOG_BYTES:
            _rotated = _CRASH_LOG + ".old"
            try:
                os.replace(_CRASH_LOG, _rotated)
            except OSError:
                pass

        if tb_str is None:
            tb_str = _traceback.format_exc()

        code = "E099"
        from tts_utils import fmt_err as _fe
        msg = _fe(e)
        import re as _re
        m = _re.search(r'\[(E\d{3})\]', msg)
        if m:
            code = m.group(1)

        timestamp = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"[{timestamp}] {code} — {type(e).__name__}: {str(e)[:200]}\n"
            f"{tb_str.strip()}\n"
            f"{'-' * 60}\n"
        )
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass  # logging must never crash the app

# ── Theme palette (Wild Emerald) ──────────────────────────────────────────────
C_BG        = "#0d0d0d"   # carbon black
C_SURFACE   = "#171717"   # header/footer bars
C_CARD      = "#1f1f1f"   # panel / card bg
C_ELEVATED  = "#2a2a2a"   # hover / elevated
C_BORDER    = "#383838"   # subtle neutral border
C_ACCENT    = "#00d98b"   # vivid emerald
C_ACCENT_H  = "#2ee5a0"   # emerald hover
C_ACCENT_D  = "#0a3d28"   # dark emerald / progress track
C_TXT       = "#f0ece4"   # warm near-white
C_TXT2      = "#9a9290"   # warm medium gray
C_TXT3      = "#4e4a48"   # dark warm gray
C_SUCCESS   = "#22c55e"   # green
C_WARN      = "#f97316"   # orange
C_DANGER    = "#f87171"   # red
C_REC       = "#dc3030"   # record button red

# Button presets
BTN_GHOST   = dict(fg_color="transparent", hover_color=C_ELEVATED,
                   border_width=1, border_color=C_BORDER, text_color=C_TXT2)

# ── Window helpers ───────────────────────────────────────────────────────────
def _center_window(win, w: int, h: int) -> None:
    """Set win to w×h and center it over the main app window.

    Centers relative to the app window rather than the screen so it works
    correctly on remote/cloud desktops (Shadow PC) and multi-monitor setups
    where GetSystemMetrics may return the wrong coordinate space.
    """
    win.update_idletasks()
    try:
        ax = app.winfo_x()
        ay = app.winfo_y()
        aw = app.winfo_width()
        ah = app.winfo_height()
        x = ax + (aw - w) // 2
        y = ay + (ah - h) // 2
    except Exception:
        try:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
        except Exception:
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
    win.geometry(f"{w}x{h}+{x}+{y}")


# ── Window animation helpers ──────────────────────────────────────────────────
def _fade_in(win, steps=10, delay=18):
    """Fade a CTkToplevel in from transparent to opaque."""
    win.attributes("-alpha", 0.0)
    def _step(i):
        if not win.winfo_exists():
            return
        if i > steps:
            win.attributes("-alpha", 1.0)
            return
        win.attributes("-alpha", i / steps)
        win.after(delay, lambda: _step(i + 1))
    win.after(delay, lambda: _step(1))

def _fade_out(win, on_done, steps=8, delay=15):
    """Fade a CTkToplevel out then call on_done (e.g. win.destroy)."""
    def _step(i):
        if not win.winfo_exists():
            on_done()
            return
        if i < 0:
            on_done()
            return
        win.attributes("-alpha", i / steps)
        win.after(delay, lambda: _step(i - 1))
    _step(steps - 1)
BTN_DARK    = dict(fg_color=C_CARD, hover_color=C_ELEVATED, text_color=C_TXT2)
BTN_DANGER  = dict(fg_color="#2a0f0f", hover_color="#3d1515", text_color=C_DANGER,
                   border_width=1, border_color="#3d1515")

# ── App Window ────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme(_res("theme.json"))

# Pin taskbar icon on Windows — must be set before the window is created
try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "CookieStudios.VoxWild.1"
    )
except Exception:
    pass

app = ctk.CTk()
app.title(f"VoxWild  v{VERSION}")
app.geometry("1380x860")
app.minsize(1100, 720)

# Route Tkinter callback exceptions through the crash logger so they're never silent.
def _on_tk_exception(exc_type, exc_val, exc_tb):
    import traceback as _tb
    _log_crash(exc_val, tb_str="".join(_tb.format_exception(exc_type, exc_val, exc_tb)))
app.report_callback_exception = _on_tk_exception
app.configure(fg_color=C_BG)
app.withdraw()   # hidden until splash finishes

# ── Windows taskbar identity ─────────────────────────────────────────────────
# Set AppUserModelID so Windows shows our icon in the taskbar instead of the
# generic Python icon. Must be called before the window is shown.
try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CookieStudios.VoxWild.1")
except Exception:
    pass

# Set app icon (deferred slightly so taskbar picks it up after window is shown)
_APP_DIR = _res(".")
def _set_icon():
    try:
        app.iconbitmap(_res("icon.ico"))
    except Exception:
        pass
    # Also set iconphoto for window managers that don't use iconbitmap
    try:
        from PIL import Image as _IconImg, ImageTk as _IconTk
        _icon_img = _IconImg.open(_res("icon.ico"))
        _icon_photo = _IconTk.PhotoImage(_icon_img)
        app.iconphoto(True, _icon_photo)
    except Exception:
        pass
app.after(100, _set_icon)

# ── Logo image (shared across UI) ────────────────────────────────────────────
from PIL import Image as _PILImage
_LOGO_PATH = os.path.join(_APP_DIR, "logo.png")
try:
    _logo_pil = _PILImage.open(_LOGO_PATH).convert("RGBA")
    LOGO_IMG_LG = ctk.CTkImage(_logo_pil, size=(120, 120))  # splash / about
    LOGO_IMG_SM = ctk.CTkImage(_logo_pil, size=(32, 32))    # header
except Exception:
    LOGO_IMG_LG = None
    LOGO_IMG_SM = None

# ── Splash Screen ─────────────────────────────────────────────────────────────
def _run_splash(on_done):
    """Show a splash window, animate a loading bar, then call on_done()."""
    import tkinter as _tk

    SW, SH = 520, 360

    # Use plain tk.Toplevel — ctk.CTkToplevel has deferred-init callbacks that
    # keep resetting geometry, making centering unreliable on Windows.
    splash = _tk.Toplevel(app)
    splash.overrideredirect(True)
    splash.configure(bg=C_BG)
    splash.attributes("-topmost", True)

    # Center on screen using logical-pixel screen dimensions.
    # winfo_screenwidth/height can return physical pixels on HiDPI Windows,
    # but window coordinates are always in logical pixels.  ctypes
    # GetSystemMetrics returns logical-pixel values that match window coords.
    try:
        import ctypes as _ctypes
        _u32 = _ctypes.windll.user32
        sw_screen = _u32.GetSystemMetrics(0)   # SM_CXSCREEN (logical px)
        sh_screen = _u32.GetSystemMetrics(1)   # SM_CYSCREEN (logical px)
    except Exception:
        sw_screen = app.winfo_screenwidth()
        sh_screen = app.winfo_screenheight()
    x = (sw_screen - SW) // 2
    y = (sh_screen - SH) // 2
    splash.geometry(f"{SW}x{SH}+{x}+{y}")
    splash.update_idletasks()

    # Amber border frame
    border = ctk.CTkFrame(splash, fg_color=C_ACCENT, corner_radius=16)
    border.place(relx=0, rely=0, relwidth=1, relheight=1)
    inner = ctk.CTkFrame(border, fg_color=C_BG, corner_radius=14)
    inner.place(relx=0, rely=0, relwidth=1, relheight=1, bordermode="outside")

    # Logo
    if LOGO_IMG_LG:
        ctk.CTkLabel(inner, image=LOGO_IMG_LG, text="").place(relx=0.5, rely=0.28, anchor="center")

    # App name
    ctk.CTkLabel(inner, text="VoxWild",
                 font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
                 text_color=C_TXT).place(relx=0.5, rely=0.60, anchor="center")
    ctk.CTkLabel(inner, text=f"v{VERSION}  ·  Kokoro  ·  Chatterbox",
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_ACCENT).place(relx=0.5, rely=0.70, anchor="center")

    # Progress bar
    bar_frame = ctk.CTkFrame(inner, fg_color="transparent", width=360)
    bar_frame.place(relx=0.5, rely=0.83, anchor="center")
    splash_bar = ctk.CTkProgressBar(bar_frame, height=6, corner_radius=3,
                                    progress_color=C_ACCENT, fg_color=C_ACCENT_D)
    splash_bar.set(0)
    splash_bar.pack(fill="x")
    splash_status = ctk.CTkLabel(inner, text="Loading...",
                                 font=ctk.CTkFont(family="Segoe UI", size=10),
                                 text_color=C_TXT3)
    splash_status.place(relx=0.5, rely=0.91, anchor="center")

    # Animate bar to ~0.4 quickly, then crawl while Kokoro loads
    _progress = [0.0]

    def _set_bar(val, msg=""):
        _progress[0] = val
        splash_bar.set(val)
        if msg:
            splash_status.configure(text=msg)
        splash.update_idletasks()

    def _animate_to(target, steps=18, delay=30):
        start = _progress[0]
        step_size = (target - start) / steps
        def _step(i=0):
            if i < steps:
                splash_bar.set(start + step_size * (i + 1))
                splash.after(delay, lambda: _step(i + 1))
        _step()

    _animate_to(0.15, steps=12, delay=40)
    splash.after(200, lambda: _set_bar(0.15, "Initializing..."))
    splash.after(400, lambda: _animate_to(0.35, steps=10, delay=50))
    splash.after(800, lambda: _set_bar(0.35, "Loading Kokoro TTS engine..."))

    def _finish_splash():
        _set_bar(1.0, "Ready!")
        splash.after(350, lambda: (_close_splash()))

    def _close_splash():
        splash.destroy()
        on_done()

    # Expose finish hook so main code can call it after Kokoro loads
    splash._finish = _finish_splash
    return splash

# ── Load Kokoro ───────────────────────────────────────────────────────────────
kokoro = Kokoro(_res("kokoro-v1.0.onnx"), _res("voices-v1.0.bin"))

_fmt_err = fmt_err  # local alias kept so existing call sites are unchanged

# ── Chatterbox Engine (persistent subprocess) ─────────────────────────────────
class ChatterboxEngine:
    """Manages a persistent chatterbox_worker.py subprocess."""

    WORKER = _res("chatterbox_worker.py")

    # Dev env (running from source) — sibling of app.py
    _PYTHON_DEV    = _res(os.path.join("chatterbox_env", "Scripts", "python.exe"))
    # Auto-installed env: embeddable Python extracted to APPDATA (no system Python needed)
    _CB_BASE_DIR   = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), "TTS Studio"
    )
    _CB_PYTHON_DIR = os.path.join(_CB_BASE_DIR, "python_embed")
    _PYTHON_USER   = os.path.join(_CB_PYTHON_DIR, "python.exe")

    def __init__(self):
        self._proc   = None
        self._sr     = 24000   # default; updated on ready
        self._lock   = threading.Lock()

    @property
    def PYTHON(self):
        """Dev env takes priority (running from source); falls back to APPDATA install."""
        if os.path.exists(self._PYTHON_DEV):
            return self._PYTHON_DEV
        return self._PYTHON_USER

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def start(self, status_cb=None):
        """Start worker and block until model is ready. Raises on failure."""
        with self._lock:
            if self.is_ready:
                return
            if not os.path.exists(self.PYTHON):
                raise FileNotFoundError(
                    "chatterbox_env not found.\n"
                    f"Expected: {self.PYTHON}"
                )

            # Prepend the Python directory (and Scripts/) to PATH so Windows
            # can find python3xx.dll and torch C-extension DLLs when the
            # worker subprocess loads them. Without this, the frozen app's
            # stripped PATH causes "Could not find module" OSErrors.
            python_dir  = os.path.dirname(self.PYTHON)
            scripts_dir = os.path.join(python_dir, "Scripts")
            env = os.environ.copy()
            env["PATH"] = (
                python_dir + os.pathsep +
                scripts_dir + os.pathsep +
                env.get("PATH", "")
            )
            # Force UTF-8 on the worker's stdin/stdout/stderr so the parent
            # (which reads with encoding="utf-8") never hits a codec mismatch.
            env["PYTHONIOENCODING"] = "utf-8"

            # Write stderr to a file (not a pipe) to avoid deadlocking on
            # the 4 GB machine. Pipe-based stderr blocks the worker when the
            # buffer fills with torch/diffusers warnings and the parent isn't
            # draining it.  A file avoids this entirely.
            _stderr_log = os.path.join(
                os.environ.get("APPDATA", ""), "TTS Studio",
                "worker_startup_crash.log",
            )
            _stderr_fh = open(_stderr_log, "w", encoding="utf-8")

            proc = subprocess.Popen(
                [self.PYTHON, self.WORKER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=_stderr_fh,
                # Binary mode — no text=True, no encoding.
                # The worker writes UTF-8 bytes directly to the pipe.
                # We read raw bytes here and decode ourselves.
                # This eliminates all TextIOWrapper/locale mismatches.
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=env,
            )
            for raw_bytes in proc.stdout:
                raw = raw_bytes.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # skip any non-JSON noise
                if msg["type"] == "status":
                    if status_cb:
                        status_cb(msg['msg'])
                elif msg["type"] == "ready":
                    self._sr   = msg.get("sr", 24000)
                    self._proc = proc
                    _stderr_fh.close()
                    return
                elif msg["type"] == "error":
                    proc.kill()
                    _stderr_fh.close()
                    raise RuntimeError(msg["msg"])
            proc.kill()
            _stderr_fh.close()
            # Read back the log for the UI error message.
            _stderr = ""
            try:
                with open(_stderr_log, encoding="utf-8") as _f:
                    _stderr = _f.read()
            except Exception:
                pass
            _tail = _stderr.strip().splitlines()[-3:] if _stderr.strip() else []
            _hint = "\n".join(_tail)
            raise RuntimeError(
                f"Chatterbox worker exited during startup.\n{_hint}".strip()
                + "  [E099]"
            )

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write((json.dumps({"cmd": "quit"}) + "\n").encode("utf-8"))
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

    @property
    def is_ready(self):
        return self._proc is not None and self._proc.poll() is None

    @property
    def sr(self):
        return self._sr

    # ── generation ─────────────────────────────────────────────────────────────
    def generate_chunk(self, text, audio_prompt_path=None,
                       exaggeration=0.5, cfg_weight=0.5, temperature=0.8,
                       status_cb=None):
        """Generate one chunk; returns (numpy_samples, sample_rate)."""
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        req = {
            "cmd": "generate",
            "text": text,
            "output_path": tmp.name,
            "audio_prompt_path": audio_prompt_path,
            "exaggeration": exaggeration,
            "cfg_weight": cfg_weight,
            "temperature": temperature,
        }
        self._proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
        self._proc.stdin.flush()
        for raw_bytes in self._proc.stdout:
            raw = raw_bytes.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue  # skip non-JSON noise
            if msg["type"] == "status":
                if status_cb:
                    status_cb(msg['msg'])
            elif msg["type"] == "done":
                samples, sr = sf.read(tmp.name)
                os.unlink(tmp.name)
                return samples, sr
            elif msg["type"] == "error":
                os.unlink(tmp.name)
                self.stop()  # mark engine as needing restart so next attempt recovers
                raise RuntimeError(msg["msg"])
        self.stop()
        raise RuntimeError("Chatterbox worker closed unexpectedly.")

chatterbox_engine = ChatterboxEngine()


class EnhanceEngine:
    """Manages a persistent enhance_worker.py subprocess."""

    WORKER = _res("enhance_worker.py")

    # Same Python paths as ChatterboxEngine — enhancement runs in python_embed
    _CB_BASE_DIR   = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), "TTS Studio"
    )
    _CB_PYTHON_DIR = os.path.join(_CB_BASE_DIR, "python_embed")
    _PYTHON_USER   = os.path.join(_CB_PYTHON_DIR, "python.exe")
    _PYTHON_DEV    = _res(os.path.join("chatterbox_env", "Scripts", "python.exe"))

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    @property
    def PYTHON(self):
        if os.path.exists(self._PYTHON_DEV):
            return self._PYTHON_DEV
        return self._PYTHON_USER

    def start(self, status_cb=None):
        """Start worker and block until ready. Raises on failure."""
        with self._lock:
            if self.is_ready:
                return
            if not os.path.exists(self.PYTHON):
                raise FileNotFoundError(
                    "python_embed not found — set up Natural mode first.\n"
                    f"Expected: {self.PYTHON}"
                )

            python_dir  = os.path.dirname(self.PYTHON)
            scripts_dir = os.path.join(python_dir, "Scripts")
            env = os.environ.copy()
            env["PATH"] = (
                python_dir + os.pathsep +
                scripts_dir + os.pathsep +
                env.get("PATH", "")
            )
            env["PYTHONIOENCODING"] = "utf-8"

            _stderr_log = os.path.join(
                os.environ.get("APPDATA", ""), "TTS Studio",
                "enhance_startup_crash.log",
            )
            _stderr_fh = open(_stderr_log, "w", encoding="utf-8")

            proc = subprocess.Popen(
                [self.PYTHON, self.WORKER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=_stderr_fh,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=env,
            )
            for raw_bytes in proc.stdout:
                raw = raw_bytes.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg["type"] == "status":
                    if status_cb:
                        status_cb(msg["msg"])
                elif msg["type"] == "ready":
                    self._proc = proc
                    _stderr_fh.close()
                    return
                elif msg["type"] == "error":
                    proc.kill()
                    _stderr_fh.close()
                    raise RuntimeError(msg["msg"])
            proc.kill()
            _stderr_fh.close()
            _stderr = ""
            try:
                with open(_stderr_log, encoding="utf-8") as _f:
                    _stderr = _f.read()
            except Exception:
                pass
            _tail = _stderr.strip().splitlines()[-3:] if _stderr.strip() else []
            _hint = "\n".join(_tail)
            raise RuntimeError(
                f"Enhance worker exited during startup.\n{_hint}".strip()
                + "  [E013]"
            )

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write(
                    (json.dumps({"cmd": "quit"}) + "\n").encode("utf-8")
                )
                self._proc.stdin.flush()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None

    @property
    def is_ready(self):
        return self._proc is not None and self._proc.poll() is None

    def enhance(self, input_path, output_path, device="cpu", status_cb=None):
        """Enhance one audio file; returns (sample_rate, rms_delta_db)."""
        req = {
            "cmd": "enhance",
            "input_path": input_path,
            "output_path": output_path,
            "device": device,
        }
        self._proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
        self._proc.stdin.flush()
        for raw_bytes in self._proc.stdout:
            raw = raw_bytes.decode("utf-8", errors="replace").strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg["type"] == "status":
                if status_cb:
                    status_cb(msg["msg"])
            elif msg["type"] == "done":
                return msg.get("sr", 44100), msg.get("rms_delta_db", 0.0)
            elif msg["type"] == "error":
                self.stop()
                raise RuntimeError(msg["msg"])
        self.stop()
        raise RuntimeError("Enhance worker closed unexpectedly.")


enhance_engine = EnhanceEngine()

# ── Chatterbox auto-setup helpers ─────────────────────────────────────────────

def _cb_env_exists():
    """Return True if Natural mode Python + chatterbox are fully installed.

    Just checking for python.exe isn't enough — the setup could have
    downloaded Python but failed to pip install chatterbox (network error,
    disk full, etc.). In that case we need to re-trigger the setup.
    """
    py = chatterbox_engine.PYTHON
    if not os.path.exists(py):
        return False
    # Verify chatterbox is actually installed in python_embed's site-packages.
    # Check both capitalization variants (Windows is case-insensitive but we
    # want to be explicit). Don't use sysconfig — it returns incorrect paths
    # when called from the frozen parent process for a different interpreter.
    py_dir = os.path.dirname(py)
    for sp_path in [
        os.path.join(py_dir, "Lib", "site-packages", "chatterbox"),
        os.path.join(py_dir, "lib", "site-packages", "chatterbox"),
    ]:
        if os.path.isdir(sp_path):
            return True
    return False


def _run_chatterbox_setup(update_status, on_success, on_failure):
    """
    Background thread: download embeddable Python → bootstrap pip → install packages.
    No system Python required — downloads and manages its own interpreter.
    Calls on_success() or on_failure(error_message) when done.
    """
    import urllib.request
    import urllib.error
    import zipfile
    import glob as _glob
    import tempfile

    python_dir = ChatterboxEngine._CB_PYTHON_DIR
    python_exe = ChatterboxEngine._PYTHON_USER

    # ── Step 1: download + extract Python embeddable ─────────────────────────
    if not os.path.exists(python_exe):
        update_status("Downloading Python 3.11 (~15 MB)…")
        py_url  = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
        zip_tmp = os.path.join(tempfile.gettempdir(), "tts_python_embed.zip")
        try:
            os.makedirs(python_dir, exist_ok=True)
            urllib.request.urlretrieve(py_url, zip_tmp)
        except urllib.error.URLError as e:
            on_failure(f"Could not download Python — check your internet connection.\n({e})")
            return
        except Exception as e:
            on_failure(f"Failed to download Python: {e}")
            return

        update_status("Extracting Python 3.11…")
        try:
            with zipfile.ZipFile(zip_tmp, "r") as zf:
                zf.extractall(python_dir)
            os.unlink(zip_tmp)
        except Exception as e:
            on_failure(f"Failed to extract Python: {e}")
            return

        # Enable site-packages so pip-installed libraries are importable.
        # The embeddable zip ships with '#import site' commented out — uncomment it.
        try:
            for pth in _glob.glob(os.path.join(python_dir, "python*._pth")):
                with open(pth, encoding="utf-8") as f:
                    content = f.read()
                content = content.replace("#import site", "import site")
                with open(pth, "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception as e:
            on_failure(f"Failed to configure Python environment: {e}")
            return

    # ── Step 2: bootstrap pip ─────────────────────────────────────────────────
    pip_exe = os.path.join(python_dir, "Scripts", "pip.exe")
    if not os.path.exists(pip_exe):
        update_status("Installing pip…")
        get_pip_url = "https://bootstrap.pypa.io/get-pip.py"
        get_pip_tmp = os.path.join(tempfile.gettempdir(), "tts_get_pip.py")
        try:
            urllib.request.urlretrieve(get_pip_url, get_pip_tmp)
        except urllib.error.URLError as e:
            on_failure(f"Could not download pip installer — check your internet connection.\n({e})")
            return
        except Exception as e:
            on_failure(f"Failed to download pip installer: {e}")
            return
        try:
            r = subprocess.run(
                [python_exe, get_pip_tmp, "--quiet"],
                capture_output=True, text=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            os.unlink(get_pip_tmp)
            if r.returncode != 0:
                on_failure(
                    "Failed to install pip:\n"
                    + (r.stderr or r.stdout or "Unknown error").strip()[-400:]
                )
                return
        except Exception as e:
            on_failure(f"Failed to install pip: {e}")
            return

    # ── Step 3: install PyTorch CPU (~800 MB) ─────────────────────────────────
    update_status("Downloading PyTorch (CPU) — ~800 MB, this may take several minutes…")
    try:
        r = subprocess.run(
            [python_exe, "-m", "pip", "install",
             "torch==2.6.0", "torchaudio==2.6.0",
             "--index-url", "https://download.pytorch.org/whl/cpu",
             "--quiet"],
            capture_output=True, text=True, timeout=1800,   # 30 min
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            on_failure(
                "Failed to install PyTorch:\n"
                + (r.stderr or r.stdout or "Unknown error").strip()[-400:]
            )
            return
    except subprocess.TimeoutExpired:
        on_failure("PyTorch download timed out. Check your internet connection and try again.")
        return
    except Exception as e:
        on_failure(f"Failed to install PyTorch: {e}")
        return

    # ── Step 4: install chatterbox-tts (~200 MB) ──────────────────────────────
    update_status("Installing Chatterbox TTS and dependencies…")
    try:
        r = subprocess.run(
            [python_exe, "-m", "pip", "install", "chatterbox-tts==0.1.7", "--quiet"],
            capture_output=True, text=True, timeout=600,    # 10 min
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            on_failure(
                "Failed to install Chatterbox:\n"
                + (r.stderr or r.stdout or "Unknown error").strip()[-400:]
            )
            return
    except subprocess.TimeoutExpired:
        on_failure("Chatterbox install timed out. Check your internet connection and try again.")
        return
    except Exception as e:
        on_failure(f"Failed to install Chatterbox: {e}")
        return

    # ── Step 5: copy VCOMP140.DLL into torchaudio\lib\ ───────────────────────────
    # torchaudio's libtorchaudio.pyd links to VCOMP140.DLL (the VC++ OpenMP runtime).
    # On machines without the Visual C++ Redistributable, this DLL is missing from
    # System32 and causes E099. We bundle vcomp140.dll with the installer and copy
    # it right next to libtorchaudio.pyd so Windows finds it automatically.
    try:
        import shutil as _shutil
        _vcomp_src = _res("vcomp140.dll")
        _taudio_lib_dir = os.path.join(python_dir, "Lib", "site-packages", "torchaudio", "lib")
        if os.path.exists(_vcomp_src) and os.path.isdir(_taudio_lib_dir):
            _shutil.copy2(_vcomp_src, os.path.join(_taudio_lib_dir, "vcomp140.dll"))
        del _shutil, _vcomp_src, _taudio_lib_dir
    except Exception:
        pass  # non-fatal — machines with VC++ Redistributable already have it in System32

    if not os.path.exists(python_exe):
        on_failure("Setup finished but Python not found — please try again.")
        return

    on_success()

# ── Voices ────────────────────────────────────────────────────────────────────
VOICES = {
    "🇺🇸 Female - Heart (Best)": "af_heart",
    "🇺🇸 Female - Bella":        "af_bella",
    "🇺🇸 Female - Sarah":        "af_sarah",
    "🇺🇸 Female - Nova":         "af_nova",
    "🇺🇸 Female - Sky":          "af_sky",
    "🇺🇸 Female - Nicole":       "af_nicole",
    "🇺🇸 Female - Jessica":      "af_jessica",
    "🇺🇸 Male - Adam":           "am_adam",
    "🇺🇸 Male - Michael":        "am_michael",
    "🇬🇧 Female - Emma":         "bf_emma",
    "🇬🇧 Female - Isabella":     "bf_isabella",
    "🇬🇧 Male - George (Best)":  "bm_george",
    "🇬🇧 Male - Lewis":          "bm_lewis",
}

# ── Settings helpers (load_settings / save_settings imported from settings_window) ─
def get_default_folder():
    return _get_settings().get("default_output_folder", "")

def set_default_folder(path):
    s = _get_settings()
    s["default_output_folder"] = path
    _save_settings(s)

_FX_DEFAULTS = {
    "fx_highpass": 20, "fx_lowpass": 18000, "fx_reverb": 0.0,
    "fx_compressor": True, "fx_compressor_ratio": 2.0, "fx_gain": 0,
    "fx_noise_gate": False, "fx_trim": True,
    "fx_enhance": False, "fx_enhance_mode": "Async",
}

def _save_fx_settings():
    """Persist current FX panel state into settings.json."""
    try:
        s = _get_settings()
        s["fx_highpass"]          = highpass_slider.get()
        s["fx_lowpass"]           = lowpass_slider.get()
        s["fx_reverb"]            = reverb_slider.get()
        s["fx_compressor"]        = compressor_var.get()
        s["fx_compressor_ratio"]  = compressor_slider.get()
        s["fx_gain"]              = gain_slider.get()
        s["fx_noise_gate"]        = noise_gate_var.get()
        s["fx_trim"]              = trim_var.get()
        s["fx_enhance"]           = False  # never persist — see _restore_fx_settings
        s["fx_enhance_mode"]      = enhance_mode.get()
        _save_settings(s)
    except Exception:
        pass

def _restore_fx_settings():
    """Load persisted FX panel state and apply to UI variables."""
    try:
        s = _get_settings()
        highpass_slider.set(s.get("fx_highpass",         _FX_DEFAULTS["fx_highpass"]))
        lowpass_slider.set(s.get("fx_lowpass",           _FX_DEFAULTS["fx_lowpass"]))
        reverb_slider.set(s.get("fx_reverb",             _FX_DEFAULTS["fx_reverb"]))
        compressor_var.set(s.get("fx_compressor",        _FX_DEFAULTS["fx_compressor"]))
        compressor_slider.set(s.get("fx_compressor_ratio", _FX_DEFAULTS["fx_compressor_ratio"]))
        gain_slider.set(s.get("fx_gain",                 _FX_DEFAULTS["fx_gain"]))
        noise_gate_var.set(s.get("fx_noise_gate",        _FX_DEFAULTS["fx_noise_gate"]))
        trim_var.set(s.get("fx_trim",                    _FX_DEFAULTS["fx_trim"]))
        enhance_mode.set(s.get("fx_enhance_mode",        _FX_DEFAULTS["fx_enhance_mode"]))
        # Never auto-restore the Enhancement checkbox. When checked, the trace
        # fires _install_resemble_enhance which runs pip against python_embed.
        # If the user switches to Natural mode while pip is still running, the
        # worker imports packages that pip is actively modifying → silent crash.
        # Enhancement must be explicitly opted-in each session.
        # enhance_var.set(s.get("fx_enhance", _FX_DEFAULTS["fx_enhance"]))
        update_all_labels()
    except Exception:
        pass

# ── Calibration ───────────────────────────────────────────────────────────────
def load_calibration():
    data = {"words_per_second": None, "samples": [],
            "cb_words_per_second": None, "cb_samples": []}
    if os.path.exists(CALIBRATION_FILE):
        try:
            with open(CALIBRATION_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    # One-time migration: remove outlier samples captured during model-load.
    # Values < 0.1 wps are physically impossible during normal generation.
    _changed = False
    for s_key, w_key in [("samples", "words_per_second"),
                          ("cb_samples", "cb_words_per_second")]:
        clean = [s for s in data.get(s_key, []) if s >= 0.1]
        if len(clean) != len(data.get(s_key, [])):
            data[s_key] = clean
            data[w_key] = round(sum(clean) / len(clean), 3) if clean else None
            _changed = True
    if _changed:
        save_calibration(data)

    return data

def save_calibration(data):
    _invalidate_calibration()
    try:
        with open(CALIBRATION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass  # calibration is non-critical; silently skip if disk write fails

def record_calibration(word_count, elapsed_seconds, use_cb=None):
    data = _get_calibration()
    if use_cb is None:
        use_cb = engine_var.get() == "Natural"
    wps = word_count / elapsed_seconds if elapsed_seconds > 0 else (0.5 if use_cb else 55)
    _EMA_ALPHA = 0.4
    if use_cb:
        data.setdefault("cb_samples", [])
        data["cb_samples"].append(round(wps, 3))
        data["cb_samples"] = data["cb_samples"][-5:]
        prior = data.get("cb_words_per_second") or 0.5
        data["cb_words_per_second"] = round(_EMA_ALPHA * wps + (1 - _EMA_ALPHA) * prior, 3)
    else:
        data["samples"].append(round(wps, 3))
        data["samples"] = data["samples"][-5:]
        prior = data.get("words_per_second") or 55
        data["words_per_second"] = round(_EMA_ALPHA * wps + (1 - _EMA_ALPHA) * prior, 3)
    save_calibration(data)

def get_words_per_second():
    data = _get_calibration()
    if engine_var.get() == "Natural":
        return data.get("cb_words_per_second") or 0.5
    return data.get("words_per_second") or 55

# ── Audio History ─────────────────────────────────────────────────────────────
audio_history = []   # list of dicts: {samples, sample_rate, text, duration, timestamp, voice}
_active_play_btn  = [None]   # currently playing card's Play button, or None
_active_pause_btn = [None]   # pause button paired with the active play button
_active_stop_btn  = [None]   # stop button paired with the active play button
_pause_pos        = [None]   # sample index to resume from (None = not paused)
_play_start_time  = [None]   # time.time() when sd.play() was last called
_play_id          = [0]      # incremented each session; threads check this to avoid stomping

def _save_history() -> None:
    """Persist audio_history to disk. Safe to call from any thread via app.after."""
    try:
        records = []
        for entry in audio_history:
            if entry.get("enhancing"):
                continue  # don't save mid-enhancement; called again in finally block
            audio_file = entry.get("_audio_file")
            if not audio_file:
                continue
            # Always write current samples (handles post-enhancement overwrite)
            try:
                sf.write(audio_file,
                         np.clip(np.nan_to_num(entry["samples"], nan=0.0), -1.0, 1.0),
                         entry["sample_rate"])
            except Exception as e:
                _log_crash(e)
                continue  # skip this entry if we can't write audio

            orig_file = entry.get("_orig_file")
            if entry.get("original_samples") is not None:
                if not orig_file:
                    # Derive orig filename from the main audio filename
                    base = os.path.splitext(os.path.basename(audio_file))[0]
                    orig_file = os.path.join(HISTORY_AUDIO, base + "_orig.wav")
                    entry["_orig_file"] = orig_file
                try:
                    sf.write(orig_file,
                             np.clip(np.nan_to_num(entry["original_samples"], nan=0.0), -1.0, 1.0),
                             entry["original_sr"])
                except Exception as e:
                    _log_crash(e)
                    orig_file = None

            records.append({
                "timestamp":   entry.get("timestamp", ""),
                "text":        entry.get("text", ""),
                "voice":       entry.get("voice", ""),
                "duration":    entry.get("duration", 0),
                "segments":    entry.get("segments"),
                "audio_file":  audio_file,
                "sample_rate": entry.get("sample_rate", 22050),
                "orig_file":   orig_file,
                "original_sr": entry.get("original_sr"),
            })

        with open(HISTORY_JSON, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        _log_crash(e)


def _load_history() -> None:
    """Load persisted history from disk on startup. Runs on the main thread."""
    global audio_history
    if not os.path.exists(HISTORY_JSON):
        return
    try:
        with open(HISTORY_JSON, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception as e:
        _log_crash(e)
        return

    loaded = []
    for rec in records:
        audio_file = rec.get("audio_file", "")
        if not audio_file or not os.path.exists(audio_file):
            continue  # audio missing — skip silently
        try:
            samples, sr = sf.read(audio_file, dtype="float32")
        except Exception as e:
            _log_crash(e)
            continue

        entry = {
            "samples":     samples,
            "sample_rate": sr,
            "text":        rec.get("text", ""),
            "duration":    rec.get("duration") or len(samples) / sr,
            "timestamp":   rec.get("timestamp", ""),
            "voice":       rec.get("voice", ""),
            "segments":    rec.get("segments"),  # lists after JSON; unpack as seq is fine
            "enhancing":   False,
            "_audio_file": audio_file,
            "_orig_file":  None,
        }

        orig_file = rec.get("orig_file", "")
        if orig_file and os.path.exists(orig_file):
            try:
                orig, orig_sr = sf.read(orig_file, dtype="float32")
                entry["original_samples"] = orig
                entry["original_sr"]      = orig_sr
                entry["_orig_file"]       = orig_file
            except Exception as e:
                _log_crash(e)

        loaded.append(entry)

    audio_history = loaded[:MAX_HISTORY]
    refresh_history_panel()


def _history_audio_path(ts_tag: str, suffix: str = "") -> str:
    """Return a unique WAV path in HISTORY_AUDIO for this entry."""
    base = f"hist_{ts_tag}{suffix}"
    path = os.path.join(HISTORY_AUDIO, base + ".wav")
    n = 0
    while os.path.exists(path):
        n += 1
        path = os.path.join(HISTORY_AUDIO, f"{base}_{n}.wav")
    return path


def _delete_history_audio(entry: dict) -> None:
    """Delete WAV files associated with a history entry, if they exist."""
    for key in ("_audio_file", "_orig_file"):
        fpath = entry.get(key)
        if fpath and os.path.exists(fpath):
            try:
                os.remove(fpath)
            except Exception:
                pass


def add_to_history(samples, sample_rate, text, voice_name, segments=None):
    duration = len(samples) / sample_rate
    ts = datetime.now()
    ts_tag = ts.strftime("%Y%m%d_%H%M%S")
    entry = {
        "samples":     samples,
        "sample_rate": sample_rate,
        "text":        text,
        "duration":    duration,
        "timestamp":   ts.strftime("%Y-%m-%d %H:%M:%S"),
        "voice":       voice_name,
        "segments":    segments,  # list of (start_sec, end_sec, text) for SRT export
        "enhancing":   False,
        "_audio_file": _history_audio_path(ts_tag),
        "_orig_file":  None,
    }
    audio_history.insert(0, entry)
    if len(audio_history) > MAX_HISTORY:
        _delete_history_audio(audio_history.pop())
    # Schedule save on main thread (add_to_history is called from bg threads)
    app.after(0, _save_history)

    if enhance_var.get():
        # ── Freemium gate: Resemble Enhance ───────────────────────────────────
        if not _lic.can_use_enhance():
            app.after(0, lambda: _show_upsell_modal("enhance"))
            app.after(0, lambda e=entry: _prepend_history_card(e))
            return

        # Always show card immediately then enhance in background.
        # CPU/GPU/Async controls which device the worker uses.
        mode = enhance_mode.get()
        if mode == "GPU":
            device = "cuda"
        elif mode == "CPU":
            device = "cpu"
        else:  # Async — worker auto-detects
            device = "cpu"
        entry["enhancing"] = True
        app.after(0, lambda e=entry: _prepend_history_card(e))
        threading.Thread(target=_enhance_async, args=(entry, device), daemon=True).start()
    else:
        app.after(0, lambda e=entry: _prepend_history_card(e))


def _enhance_async(entry, device="cpu"):
    """Background thread: enhance audio via subprocess worker, refresh the card."""
    app.after(0, lambda: status_label.configure(
        text="✨ Enhancing audio... (first run downloads ~450 MB of model weights)"))
    try:
        original_samples = entry["samples"].copy()
        original_sr      = entry["sample_rate"]

        # Write input audio to temp file for the worker
        input_tmp  = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        output_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        input_tmp.close()
        output_tmp.close()

        sf.write(input_tmp.name, original_samples, original_sr)

        # Start worker if not already running
        def _status(msg):
            app.after(0, lambda m=msg: status_label.configure(text=f"✨ {m}"))

        enhance_engine.start(status_cb=_status)

        # Send enhance request
        new_sr, rms_delta_db = enhance_engine.enhance(
            input_tmp.name, output_tmp.name, device=device, status_cb=_status,
        )

        # Read back enhanced audio
        enhanced, _ = sf.read(output_tmp.name, dtype="float32")

        # Clean up temp files
        try:
            os.unlink(input_tmp.name)
        except OSError:
            pass
        try:
            os.unlink(output_tmp.name)
        except OSError:
            pass

        entry["original_samples"] = original_samples
        entry["original_sr"]      = original_sr
        entry["samples"]          = enhanced
        entry["sample_rate"]      = new_sr
        entry["duration"]         = len(enhanced) / new_sr

        _lic.record_enhance_use()

        app.after(0, lambda db=rms_delta_db: status_label.configure(
            text=f"✅ Enhancement done  ({db:+.1f} dB RMS) — use Orig button to A/B compare"))

    except Exception as e:
        _log_crash(e)
        raw_msg = str(e)
        display_msg = raw_msg.replace("  [E013]", "").replace(" [E013]", "")
        app.after(0, lambda m=display_msg: status_label.configure(
            text=f"⚠️ Enhancement failed: {m}"))
        # Clean up temp files on error
        for _t in (input_tmp, output_tmp):
            try:
                os.unlink(_t.name)
            except Exception:
                pass
    finally:
        entry["enhancing"] = False
        app.after(0, refresh_history_panel)
        app.after(0, _save_history)

def _prepend_history_card(entry):
    """Add entry to the top of the history panel and rebuild."""
    # Full rebuild is fast (max 10 cards) and guarantees correct ordering.
    # audio_history already has the new entry at index 0.
    refresh_history_panel()

def refresh_history_panel():
    """Full rebuild — used after deletions."""
    for w in history_inner.winfo_children():
        w.destroy()
    if not audio_history:
        ctk.CTkLabel(history_inner, text="No audio yet.",
                     text_color=C_TXT3, font=ctk.CTkFont(family="Segoe UI", size=12)).pack(pady=16)
        return
    for i, entry in enumerate(audio_history):
        _make_history_card(history_inner, i, entry)

def _make_history_card(parent, idx, entry):
    def _delete(e=entry):
        if e in audio_history:
            audio_history.remove(e)
            _delete_history_audio(e)
        refresh_history_panel()
        _save_history()

    outer = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=6,
                         border_width=1, border_color=C_BORDER)
    outer.pack(fill="x", padx=6, pady=(0, 8))
    # NOTE: caller (_prepend_history_card) may call outer.lift() after this returns.

    # Animated enhancing bar — packed FIRST so it sits at the bottom of outer
    if entry.get("enhancing"):
        bar = ctk.CTkProgressBar(
            outer, mode="indeterminate", height=4, corner_radius=0,
            progress_color=C_ACCENT, fg_color=C_BORDER,
        )
        bar.pack(fill="x", side="bottom", padx=0, pady=0)
        bar.start()

    # Amber accent stripe (placed so it doesn't affect pack layout)
    ctk.CTkFrame(outer, fg_color=C_ACCENT, width=3, corner_radius=0).place(
        x=0, y=0, relheight=1)

    content = ctk.CTkFrame(outer, fg_color="transparent")
    content.pack(fill="both", expand=True, padx=(12, 10), pady=(9, 9))

    # ── Row 1: timestamp (left) · duration (right) ───────────────────────────
    row1 = ctk.CTkFrame(content, fg_color="transparent")
    row1.pack(fill="x")
    _ts_raw = entry.get("timestamp", "")
    try:
        _ts_dt  = datetime.strptime(_ts_raw, "%Y-%m-%d %H:%M:%S")
        _today  = datetime.now().date()
        if _ts_dt.date() == _today:
            _ts_str = _ts_dt.strftime("%-I:%M %p") if sys.platform != "win32" \
                      else _ts_dt.strftime("%#I:%M %p")
        else:
            _ts_str = _ts_dt.strftime("%b %-d · %-I:%M %p") if sys.platform != "win32" \
                      else _ts_dt.strftime("%b %#d · %#I:%M %p")
    except Exception:
        _ts_str = _ts_raw  # fallback: show raw string (handles old HH:MM:SS entries)
    ctk.CTkLabel(row1, text=_ts_str,
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color=C_TXT3).pack(side="left")
    try:
        dur_str = format_time(int(entry.get("duration", 0)))
    except Exception:
        dur_str = ""
    ctk.CTkLabel(row1, text=dur_str,
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color=C_TXT3).pack(side="right")

    # ── Row 2: voice label (left) · status badge (right) ─────────────────────
    row2 = ctk.CTkFrame(content, fg_color="transparent")
    row2.pack(fill="x", pady=(2, 0))

    voice_label = history_card_voice_label(entry.get("voice", "Unknown"))
    ctk.CTkLabel(row2, text=voice_label,
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color=C_ACCENT).pack(side="left")

    if entry.get("enhancing"):
        ctk.CTkLabel(row2, text="✦ enhancing",
                     font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                     text_color=C_ACCENT).pack(side="right")
    elif entry.get("original_samples") is not None:
        ctk.CTkLabel(row2, text="✨ enhanced",
                     font=ctk.CTkFont(family="Segoe UI", size=10),
                     text_color=C_ACCENT).pack(side="right")

    # ── Text preview ─────────────────────────────────────────────────────────
    preview = history_card_preview(entry.get("text", ""))
    ctk.CTkLabel(content, text=preview,
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT, anchor="w", justify="left",
                 wraplength=230).pack(fill="x", pady=(5, 0))

    # ── Play controls ─────────────────────────────────────────────────────────
    row_play = ctk.CTkFrame(content, fg_color="transparent")
    row_play.pack(fill="x", pady=(7, 0))

    play_btn = ctk.CTkButton(
        row_play, text="Play", width=72, height=26,
        font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
        fg_color=C_ACCENT_D, hover_color=C_ACCENT, text_color=C_TXT, corner_radius=5,
    )
    pause_btn = ctk.CTkButton(
        row_play, text="Pause", width=50, height=26,
        font=ctk.CTkFont(family="Segoe UI", size=11),
        state="disabled", **BTN_GHOST, corner_radius=5,
    )
    stop_btn = ctk.CTkButton(
        row_play, text="Stop", width=44, height=26,
        font=ctk.CTkFont(family="Segoe UI", size=11),
        state="disabled", **BTN_GHOST, corner_radius=5,
    )
    play_btn.configure(command=lambda e=entry, b=play_btn, p=pause_btn, s=stop_btn:
                       _toggle_history_playback(e, b, p, s))
    pause_btn.configure(command=lambda e=entry, b=play_btn, p=pause_btn:
                        _toggle_history_pause(e, b, p))
    stop_btn.configure(command=lambda b=play_btn: _history_stop(b))
    play_btn.pack(side="left", padx=(0, 4))
    pause_btn.pack(side="left", padx=(0, 4))
    stop_btn.pack(side="left", padx=(0, 4))

    # ── Action row ────────────────────────────────────────────────────────────
    # Delete is packed RIGHT first so it always reserves its space. Then
    # Save / SRT / Orig fill from the left. All widths are kept deliberately
    # tight (no CTk default 140px) so buttons never overflow the card.
    row_act = ctk.CTkFrame(content, fg_color="transparent")
    row_act.pack(fill="x", pady=(6, 0))

    ctk.CTkButton(
        row_act, text="Delete", width=46, height=22,
        font=ctk.CTkFont(family="Segoe UI", size=10),
        fg_color="transparent", hover_color="#3d1515",
        text_color=C_DANGER, border_width=1, border_color="#3d1515", corner_radius=5,
        command=_delete
    ).pack(side="right")

    ctk.CTkButton(
        row_act, text="Save", width=38, height=22,
        font=ctk.CTkFont(family="Segoe UI", size=10),
        **BTN_GHOST, corner_radius=5,
        command=lambda e=entry: download_history_entry(e)
    ).pack(side="left", padx=(0, 4))

    if entry.get("segments"):
        ctk.CTkButton(
            row_act, text="SRT", width=32, height=22,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            **BTN_GHOST, corner_radius=5,
            command=lambda e=entry: export_srt_from_entry(e)
        ).pack(side="left", padx=(0, 4))

    if entry.get("original_samples") is not None:
        # Build a synthetic entry so the original audio goes through the
        # full playback system — same pause / stop / error handling.
        _orig_entry = {
            "samples":     entry["original_samples"],
            "sample_rate": entry["original_sr"],
            "text":        f"[Original] {entry.get('text', '')}",
        }
        orig_play_btn  = ctk.CTkButton(
            row_act, text="Orig", width=46, height=22,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            **BTN_GHOST, corner_radius=5,
        )
        orig_pause_btn = ctk.CTkButton(
            row_act, text="Pause", width=46, height=22,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            state="disabled", **BTN_GHOST, corner_radius=5,
        )
        orig_stop_btn  = ctk.CTkButton(
            row_act, text="Stop", width=40, height=22,
            font=ctk.CTkFont(family="Segoe UI", size=10),
            state="disabled", **BTN_GHOST, corner_radius=5,
        )
        orig_play_btn.configure(
            command=lambda e=_orig_entry, b=orig_play_btn, p=orig_pause_btn, s=orig_stop_btn:
                _toggle_history_playback(e, b, p, s))
        orig_pause_btn.configure(
            command=lambda e=_orig_entry, b=orig_play_btn, p=orig_pause_btn:
                _toggle_history_pause(e, b, p))
        orig_stop_btn.configure(
            command=lambda b=orig_play_btn: _history_stop(b))
        orig_play_btn.pack(side="left",  padx=(0, 4))
        # Pause/Stop hidden until playback starts (they look like empty boxes when disabled)

    return outer

def _reset_play_btn():
    """Reset active play/pause/stop buttons to idle state. Call from main thread only."""
    play_btn = _active_play_btn[0]
    if play_btn:
        try:
            play_btn.configure(state="normal")
        except Exception:
            pass
    pause_btn = _active_pause_btn[0]
    if pause_btn:
        try:
            pause_btn.pack_forget()
            pause_btn.configure(text="Pause", state="disabled")
        except Exception:
            pass
    stop_btn = _active_stop_btn[0]
    if stop_btn:
        try:
            stop_btn.pack_forget()
            stop_btn.configure(state="disabled")
        except Exception:
            pass
    _active_play_btn[0]  = None
    _active_pause_btn[0] = None
    _active_stop_btn[0]  = None
    _pause_pos[0]        = None
    _play_start_time[0]  = None

def _toggle_history_playback(entry, btn, pause_btn=None, stop_btn=None):
    """Play a history entry. Stops any currently playing audio first."""
    try:
        sd.stop()
    except Exception as e:
        _log_crash(e)

    _reset_play_btn()

    raw = entry.get("samples")
    if raw is None or (hasattr(raw, "__len__") and len(raw) == 0):
        status_label.configure(text="❌ Audio data missing — try re-generating this entry.")
        return

    try:
        samples = np.clip(np.nan_to_num(raw, nan=0.0), -1.0, 1.0)
        sr      = entry["sample_rate"]
    except Exception as e:
        _log_crash(e)
        status_label.configure(text=f"❌ Could not read audio: {_fmt_err(e)}")
        return

    _play_id[0] += 1
    my_id = _play_id[0]

    _active_play_btn[0]  = btn
    _active_pause_btn[0] = pause_btn
    _active_stop_btn[0]  = stop_btn
    _pause_pos[0]        = None
    _play_start_time[0]  = time.time()

    if btn:
        try:
            btn.configure(state="disabled")
        except Exception:
            pass
    if pause_btn:
        try:
            pause_btn.pack(side="left", padx=(0, 2))
            pause_btn.configure(state="normal")
        except Exception:
            pass
    if stop_btn:
        try:
            stop_btn.pack(side="left", padx=(0, 4))
            stop_btn.configure(state="normal")
        except Exception:
            pass

    def run():
        try:
            app.after(0, lambda: status_label.configure(
                text=f"🔊 Playing: {entry['text'][:40]}..."))
            sd.play(samples, sr)
            sd.wait()
            if _play_id[0] == my_id:
                app.after(0, lambda: status_label.configure(text="✅ Playback done."))
                app.after(0, _reset_play_btn)
        except Exception as e:
            _log_crash(e)
            app.after(0, lambda m=_fmt_err(e): status_label.configure(text=f"❌ {m}"))
            if _play_id[0] == my_id:
                app.after(0, _reset_play_btn)

    threading.Thread(target=run, daemon=True).start()

def _history_stop(play_btn):
    """Stop playback if this card's play button is the active one."""
    if _active_play_btn[0] is not play_btn:
        return
    try:
        _play_id[0] += 1   # invalidate any bg thread before stopping
        sd.stop()
        _reset_play_btn()
        status_label.configure(text="Stopped.")
    except Exception as e:
        _log_crash(e)

def _toggle_history_pause(entry, play_btn, pause_btn):
    """Pause or resume the currently playing history entry."""
    if _active_play_btn[0] is not play_btn:
        return  # not this card's audio
    sr = entry["sample_rate"]
    samples = np.clip(np.nan_to_num(entry["samples"], nan=0.0), -1.0, 1.0)

    if _pause_pos[0] is None:
        # ── Pause ────────────────────────────────────────────────────────────
        elapsed = time.time() - (_play_start_time[0] or time.time())
        pos = int(elapsed * sr)
        _pause_pos[0] = min(pos, len(samples) - 1)
        _play_id[0] += 1   # invalidate bg thread so it won't call _reset_play_btn
        try:
            sd.stop()
        except Exception as e:
            _log_crash(e)
        if pause_btn:
            try:
                pause_btn.configure(text="Resume")
            except Exception:
                pass
        status_label.configure(text="Paused.")
    else:
        # ── Resume ───────────────────────────────────────────────────────────
        resume_pos = _pause_pos[0]
        _pause_pos[0] = None
        if pause_btn:
            try:
                pause_btn.configure(text="Pause")
            except Exception:
                pass

        _play_id[0] += 1
        my_id = _play_id[0]
        _active_play_btn[0] = play_btn   # re-assert ownership
        _play_start_time[0] = time.time() - (resume_pos / sr)

        def run():
            try:
                app.after(0, lambda: status_label.configure(
                    text=f"🔊 Playing: {entry['text'][:40]}..."))
                sd.play(samples[resume_pos:], sr)
                sd.wait()
                if _play_id[0] == my_id:
                    app.after(0, lambda: status_label.configure(text="✅ Playback done."))
                    app.after(0, _reset_play_btn)
            except Exception as e:
                _log_crash(e)
                app.after(0, lambda: status_label.configure(text=f"❌ {_fmt_err(e)}"))
                if _play_id[0] == my_id:
                    app.after(0, _reset_play_btn)

        threading.Thread(target=run, daemon=True).start()

def play_history_entry(entry):
    """Legacy wrapper — kept for any external callers."""
    _toggle_history_playback(entry, None)

def download_history_entry(entry):
    folder = get_default_folder()
    filepath = filedialog.asksaveasfilename(
        initialdir=folder or None,
        defaultextension=".wav",
        filetypes=[("MP3 files", "*.mp3"), ("WAV files", "*.wav")]
    )
    if not filepath:
        return
    set_default_folder(os.path.dirname(filepath))

    if filepath.lower().endswith(".mp3"):
        _save_as_mp3(entry, filepath)
    else:
        try:
            sf.write(filepath, entry["samples"], entry["sample_rate"])
            status_label.configure(text=f"✅ Saved: {os.path.basename(filepath)}")
        except Exception as e:
            _log_crash(e)
            status_label.configure(text=f"❌ Save failed: {_fmt_err(e)}")

def _write_id3v2(filepath, title="", artist="", album="", year="", comment=""):
    """Prepend a minimal ID3v2.3 tag block to an MP3 file (no external deps)."""
    import struct

    def _syncsafe(n):
        out = bytearray(4)
        for i in range(3, -1, -1):
            out[i] = n & 0x7F
            n >>= 7
        return bytes(out)

    def _text_frame(fid, text):
        if not text:
            return b""
        data = b"\x03" + text.encode("utf-8")   # encoding byte: UTF-8
        return fid.encode() + struct.pack(">I", len(data)) + b"\x00\x00" + data

    def _comm_frame(text, lang="eng"):
        data = b"\x03" + lang.encode() + b"\x00" + text.encode("utf-8")
        return b"COMM" + struct.pack(">I", len(data)) + b"\x00\x00" + data

    frames = (
        _text_frame("TIT2", title)
        + _text_frame("TPE1", artist)
        + _text_frame("TALB", album)
        + _text_frame("TYER", year)
        + (_comm_frame(comment) if comment else b"")
    )
    id3_tag = b"ID3\x03\x00\x00" + _syncsafe(len(frames)) + frames
    with open(filepath, "r+b") as f:
        mp3_data = f.read()
        f.seek(0)
        f.write(id3_tag + mp3_data)
        f.truncate()


def _save_as_mp3(entry, filepath):
    """Show MP3 metadata dialog then encode and save."""
    import lameenc

    win = ctk.CTkToplevel(app)
    win.title("Save as MP3")
    _center_window(win, 420, 460)
    win.resizable(False, False)
    win.grab_set()
    win.configure(fg_color=C_BG)
    _fade_in(win)

    # Header
    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=54)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    hdr_inner = ctk.CTkFrame(hdr, fg_color="transparent")
    hdr_inner.pack(side="left", padx=16, pady=10)
    ctk.CTkFrame(hdr_inner, fg_color=C_ACCENT, width=8, height=8,
                 corner_radius=4).pack(side="left", padx=(0, 10))
    ctk.CTkLabel(hdr_inner, text="MP3 Metadata  (optional)",
                 font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                 text_color=C_TXT).pack(side="left")
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    # ── Footer (packed BEFORE body so expand=True on body doesn't push it off-screen) ──
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(side="bottom", fill="x")
    foot = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=62)
    foot.pack(side="bottom", fill="x")
    foot.pack_propagate(False)
    foot_inner = ctk.CTkFrame(foot, fg_color="transparent")
    foot_inner.pack(expand=True, fill="both", padx=16)

    # ── Body (fills remaining space between header and footer) ────────────────
    body = ctk.CTkFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=20, pady=10)

    def _field(label, value="", placeholder=""):
        ctk.CTkLabel(body, text=label,
                     font=ctk.CTkFont(family="Segoe UI", size=11),
                     text_color=C_TXT2, anchor="w").pack(fill="x", pady=(4, 1))
        var = ctk.StringVar(value=value)
        ctk.CTkEntry(body, textvariable=var,
                     fg_color=C_CARD, border_color=C_BORDER, text_color=C_TXT,
                     placeholder_text=placeholder, placeholder_text_color=C_TXT3,
                     height=30).pack(fill="x")
        return var

    _auto_title = entry.get("text", "")[:60].replace("\n", " ").strip()
    _auto_voice = entry.get("voice", "")
    title_var  = _field("Title",          value=_auto_title)
    author_var = _field("Artist / Author", value=_auto_voice, placeholder="e.g. Cookie Studios")
    album_var  = _field("Album / Series",  placeholder="optional")
    year_var   = _field("Year",            value=datetime.now().strftime("%Y"))

    # Quality selector
    ctk.CTkLabel(body, text="Quality",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT2, anchor="w").pack(fill="x", pady=(8, 1))
    quality_var = ctk.StringVar(value="192 kbps")
    ctk.CTkSegmentedButton(body, values=["128 kbps", "192 kbps", "320 kbps"],
                            variable=quality_var,
                            font=ctk.CTkFont(family="Segoe UI", size=11),
                            width=380).pack(anchor="w")

    def _do_save():
        # Capture all StringVar values BEFORE destroying the window
        _title   = title_var.get().strip()
        _artist  = author_var.get().strip()
        _album   = album_var.get().strip()
        _year    = year_var.get().strip()
        _quality = quality_var.get()
        win.destroy()
        status_label.configure(text="Encoding MP3...")
        app.update_idletasks()

        try:
            bitrate_map = {"128 kbps": 128, "192 kbps": 192, "320 kbps": 320}
            bitrate = bitrate_map.get(_quality, 192)

            samples = entry["samples"]
            sr      = entry["sample_rate"]

            # Convert float32 → int16
            pcm = np.clip(samples, -1.0, 1.0)
            pcm = (pcm * 32767).astype(np.int16)
            # Mono only for now
            if pcm.ndim > 1:
                pcm = pcm.mean(axis=1).astype(np.int16)

            encoder = lameenc.Encoder()
            encoder.set_bit_rate(bitrate)
            encoder.set_in_sample_rate(sr)
            encoder.set_channels(1)
            encoder.set_quality(2)   # 2 = highest
            mp3_data = encoder.encode(pcm.tobytes()) + encoder.flush()

            with open(filepath, "wb") as f:
                f.write(mp3_data)

            # Write ID3 tags (pure Python, no mutagen)
            _write_id3v2(
                filepath,
                title=_title,
                artist=_artist,
                album=_album,
                year=_year,
                comment=f"Generated by VoxWild v{VERSION}",
            )

            status_label.configure(
                text=f"✅ Saved MP3: {os.path.basename(filepath)}  ({bitrate} kbps)")
        except Exception as e:
            _log_crash(e)
            status_label.configure(text=f"❌ MP3 encode failed: {_fmt_err(e)}")

    ctk.CTkButton(foot_inner, text="Save MP3", command=_do_save,
                  width=120, height=36,
                  font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold")
                  ).pack(side="left", padx=(0, 10), pady=13)
    ctk.CTkButton(foot_inner, text="Don't Save", command=win.destroy,
                  width=110, height=36,
                  font=ctk.CTkFont(family="Segoe UI", size=12),
                  **BTN_GHOST).pack(side="left", pady=13)

# ── SRT Export ────────────────────────────────────────────────────────────────
# _srt_time, _wrap_for_subtitle, build_srt imported from tts_utils

def export_srt_from_entry(entry):
    if not entry.get("segments"):
        status_label.configure(text="⚠️ No timing data available for SRT export.")
        return
    folder = get_default_folder()
    filepath = filedialog.asksaveasfilename(
        initialdir=folder or None,
        defaultextension=".srt",
        filetypes=[("SRT subtitles", "*.srt"), ("All files", "*.*")]
    )
    if not filepath:
        return
    try:
        srt_content = build_srt(entry["segments"])
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(srt_content)
        status_label.configure(text=f"✅ SRT saved: {os.path.basename(filepath)}")
    except Exception as e:
        _log_crash(e)
        status_label.configure(text=f"❌ SRT export failed: {_fmt_err(e)}")

# ── Smooth Progress ───────────────────────────────────────────────────────────
class SmoothProgress:
    def __init__(self, bar, time_label):
        self.bar         = bar
        self.time_label  = time_label
        self._current    = 0.0
        self._target     = 0.0
        self._running    = False
        self._start_time = None
        self._est_total  = None

    def start(self, estimated_seconds):
        self._current    = 0.0
        self._target     = 0.0
        self._running    = True
        self._start_time = time.time()
        self._est_total  = max(estimated_seconds, 1)
        self.bar.set(0)
        self.time_label.configure(text=f"⏱ Est. time: ~{format_time(estimated_seconds)}")
        self._tick()

    def set_target(self, value):
        self._target = min(value, 0.99)

    def finish(self):
        # Called from worker threads — schedule all UI work on the main thread
        elapsed = time.time() - self._start_time if self._start_time else 0
        self._running = False
        self.bar.after(0, lambda e=elapsed: self._finish_ui(e))

    def _finish_ui(self, elapsed):
        self._current = 1.0
        self.bar.set(1.0)
        self.time_label.configure(text=f"✅ Done in {format_time(elapsed)}")
        s = _get_settings()
        threshold = s.get("notify_threshold_seconds", 10)
        if s.get("notify_on_completion", True) and elapsed > threshold:
            try:
                from win10toast import ToastNotifier
                ToastNotifier().show_toast(
                    "VoxWild",
                    "Your audio is ready!",
                    duration=4,
                    threaded=True
                )
            except Exception:
                pass  # Notification is optional, never crash for it

    def _tick(self):
        if not self._running:
            return
        elapsed     = time.time() - self._start_time
        remaining   = self._est_total - elapsed
        if remaining > 0:
            time_driven = min(elapsed / self._est_total * 0.90, 0.90)
            ideal       = max(time_driven, self._target)
            self._current += (ideal - self._current) * 0.15
            self._current  = min(self._current, 0.90)
            self.bar.set(self._current)
            self.time_label.configure(text=f"⏱ Est. remaining: ~{format_time(remaining)}")
        else:
            # Estimate exceeded — breathe smoothly between 0.88 and 0.97
            import math
            pulse = 0.925 + 0.045 * math.sin(elapsed * 1.8)
            self.bar.set(pulse)
            self.time_label.configure(text="⏱ Processing…")
        app.after(100, self._tick)

# ── Helpers ───────────────────────────────────────────────────────────────────
# format_time imported from tts_utils

def estimate_processing_time(text):
    return len(text.split()) / get_words_per_second()

# estimate_audio_duration imported from tts_utils
# trim_silence imported from audio_utils
# chunk_text imported from tts_utils

# ── Voice Clone Library ────────────────────────────────────────────────────────
# CLONE_DIR / CLONE_INDEX defined near the top with other user-data paths.

# Thin wrappers bind the module-level paths so all existing call sites are unchanged.
# The in-memory _clone_cache is used for reads; writes invalidate it.
def load_clone_library():
    return _get_clone_library()

def save_clone_library(entries):
    _invalidate_clone_cache()
    _lib_save(entries, CLONE_DIR, CLONE_INDEX)

def add_clone_to_library(name, src_wav_path):
    _invalidate_clone_cache()
    return _lib_add(name, src_wav_path, CLONE_DIR, CLONE_INDEX)

def rename_clone_in_library(old_name, new_name):
    _invalidate_clone_cache()
    return _lib_rename(old_name, new_name, CLONE_DIR, CLONE_INDEX)

def apply_enhancements(samples, sample_rate):
    out = samples.astype(np.float32).copy()

    # Noise gate
    if noise_gate_var.get():
        threshold = 10 ** (-40 / 20)
        release_samples = int(sample_rate * 0.25)
        gate_open = np.abs(out) > threshold
        # Smooth the gate with a simple release envelope
        envelope = np.zeros(len(out), dtype=np.float32)
        level = 0.0
        for i in range(len(out)):
            if gate_open[i]:
                level = 1.0
            else:
                level = max(0.0, level - 1.0 / release_samples)
            envelope[i] = level
        out *= envelope

    # High-pass filter
    nyq = sample_rate / 2.0
    if highpass_slider.get() > 20:
        hp_freq = min(float(highpass_slider.get()), nyq - 1)
        sos = butter(4, hp_freq, btype="high",
                     fs=sample_rate, output="sos")
        out = sosfilt(sos, out).astype(np.float32)

    # Low-pass filter
    if lowpass_slider.get() < nyq:
        lp_freq = min(float(lowpass_slider.get()), nyq - 1)
        sos = butter(4, lp_freq, btype="low",
                     fs=sample_rate, output="sos")
        out = sosfilt(sos, out).astype(np.float32)

    # Compressor — vectorized via scipy IIR envelope follower (~100x faster than
    # the per-sample Python loop).  Uses the release coefficient for smoothing;
    # attack/release asymmetry is imperceptible on speech-only TTS output.
    if compressor_var.get():
        from scipy.signal import lfilter
        threshold_lin = 10 ** (-20 / 20)
        ratio        = float(compressor_slider.get())
        release_coef = np.exp(-1.0 / (sample_rate * 0.100))   # 100 ms
        # IIR one-pole lowpass: env[n] = r*env[n-1] + (1-r)*|x[n]|
        b = np.array([1.0 - release_coef])
        a = np.array([1.0, -release_coef])
        env = lfilter(b, a, np.abs(out).astype(np.float64)).astype(np.float32)
        env = np.maximum(env, 1e-8)
        gain = np.where(
            env > threshold_lin,
            (threshold_lin + (env - threshold_lin) / ratio) / env,
            1.0,
        ).astype(np.float32)
        out = out * gain

    # Reverb (Freeverb-style Schroeder reverb)
    if reverb_slider.get() > 0:
        rv       = reverb_slider.get()
        wet      = rv * 0.4
        dry      = 1.0 - rv * 0.2
        room     = rv * 0.3
        damping  = 0.7
        # Comb filter delays (in samples) tuned for speech
        comb_delays = [int(sample_rate * d) for d in
                       [0.0297, 0.0371, 0.0411, 0.0437]]
        feedback = 0.5 + room * 0.38
        allpass_delays = [int(sample_rate * d) for d in [0.005, 0.0017]]
        # Run comb filters in parallel
        comb_out = np.zeros_like(out)
        for delay in comb_delays:
            buf   = np.zeros(delay, dtype=np.float32)
            pos   = 0
            filt  = 0.0
            co    = np.empty_like(out)
            for i in range(len(out)):
                buf_out       = buf[pos]
                filt          = buf_out * (1 - damping) + filt * damping
                buf[pos]      = out[i] + filt * feedback
                pos           = (pos + 1) % delay
                co[i]         = buf_out
            comb_out += co
        comb_out /= len(comb_delays)
        # Allpass filters in series
        ap = comb_out.copy()
        for delay in allpass_delays:
            buf = np.zeros(delay, dtype=np.float32)
            pos = 0
            ao  = np.empty_like(ap)
            for i in range(len(ap)):
                buf_out  = buf[pos]
                buf[pos] = ap[i] + buf_out * 0.5
                pos      = (pos + 1) % delay
                ao[i]    = buf_out - ap[i] * 0.5
            ap = ao
        out = dry * out + wet * ap

    # Gain
    gain_db  = float(gain_slider.get())
    out     *= 10 ** (gain_db / 20)

    # Prevent clipping
    max_val = np.max(np.abs(out))
    if max_val > 1.0:
        out /= max_val

    return out

def generate_audio(text, voice, speed, status_cb=None, progress_range=(0.0, 0.95)):
    """Generate audio for text.
    progress_range: (lo, hi) — smooth bar target is scaled within this window.
    Single generation passes (0.0, 0.95); queue passes (i/n, (i+1)/n * 0.95).
    """
    lo, hi = progress_range
    text = apply_pronunciation(text)
    use_chatterbox = engine_var.get() == "Natural"

    if use_chatterbox:
        # ── Chatterbox path ────────────────────────────────────────────────────
        if not chatterbox_engine.is_ready:
            if status_cb: status_cb("Waiting for Natural mode to finish loading...")
            chatterbox_engine.start(status_cb=status_cb)
        chunks = chunk_text(text)
        all_samples, sample_rate = [], None
        _clone_path = cb_clone_path_var.get()
        if _clone_path and not os.path.exists(_clone_path):
            if status_cb: status_cb("⚠️ Voice clone file not found — using default voice.")
            _clone_path = ""
        prompt = _clone_path or None
        exag   = cb_exag_slider.get()
        cfg    = cb_cfg_slider.get()
        for i, chunk in enumerate(chunks):
            if _cancel_event.is_set():
                raise GenerationCancelled()
            if status_cb: status_cb(f"Generating chunk {i+1}/{len(chunks)}...")
            samples, sr = chatterbox_engine.generate_chunk(
                chunk,
                audio_prompt_path=prompt,
                exaggeration=exag,
                cfg_weight=cfg,
                status_cb=status_cb,
            )
            all_samples.append(samples)
            sample_rate = sr
            smooth.set_target(lo + (hi - lo) * (i + 1) / len(chunks))
    else:
        # ── Kokoro path ────────────────────────────────────────────────────────
        chunks = chunk_text(text)
        all_samples, sample_rate = [], None
        for i, chunk in enumerate(chunks):
            if _cancel_event.is_set():
                raise GenerationCancelled()
            if status_cb: status_cb(f"⏳ Generating chunk {i+1}/{len(chunks)}...")
            samples, sr = kokoro.create(chunk, voice=voice, speed=speed)
            all_samples.append(samples)
            sample_rate = sr
            smooth.set_target(lo + (hi - lo) * (i + 1) / len(chunks))

    # Build per-chunk timings (before trim/enhance) for SRT
    chunk_timings = []
    offset = 0.0
    for chunk, samp in zip(chunks, all_samples):
        dur = len(samp) / sample_rate
        chunk_timings.append((offset, offset + dur, chunk))
        offset += dur
    pre_total_dur = offset

    combined = np.concatenate(all_samples)

    # Sanitize — NaN/Inf in audio (from a bad voice clone prompt) will crash
    # PortAudio at the C level with no Python exception and no error message.
    if not np.isfinite(combined).all():
        combined = np.nan_to_num(combined, nan=0.0, posinf=1.0, neginf=-1.0)
    combined = np.clip(combined, -1.0, 1.0)

    if trim_var.get():
        if status_cb: status_cb("✂️ Trimming silence...")
        combined = trim_silence(combined, sample_rate)
    if status_cb: status_cb("🎛️ Applying enhancements...")
    enhanced = apply_enhancements(combined, sample_rate)

    # Final clip after enhancements (compressor/gain can push above 1.0)
    enhanced = np.clip(enhanced, -1.0, 1.0)

    # Scale timings to match final (post-trim/enhance) audio length
    post_total_dur = len(enhanced) / sample_rate
    scale = post_total_dur / pre_total_dur if pre_total_dur > 0 else 1.0
    segments = [(s * scale, e * scale, t) for s, e, t in chunk_timings]

    return enhanced, sample_rate, segments

# ── Dialogue ──────────────────────────────────────────────────────────────────
# parse_dialogue imported from tts_utils

def generate_dialogue_audio(dialogue_lines, speaker_voices, speed,
                            status_cb=None, cancel_event=None):
    """
    Generate multi-voice audio from parsed dialogue lines.
    dialogue_lines: list of (speaker, text)
    speaker_voices: dict of speaker_name -> voice display name (key in VOICES)
    cancel_event:   threading.Event — checked between lines; raises GenerationCancelled
    Returns: (audio, sample_rate, segments, failed_lines)
      failed_lines: list of (line_index, speaker, error_str) for skipped lines
    """
    PAUSE_SAME = 0.15   # s between consecutive lines from same speaker
    PAUSE_DIFF = 0.35   # s between different speakers

    parts       = []
    timings     = []
    failed_lines = []
    sample_rate = None
    offset      = 0.0
    voice_keys  = list(VOICES.keys())

    for i, (speaker, text) in enumerate(dialogue_lines):
        if cancel_event and cancel_event.is_set():
            raise GenerationCancelled()

        if status_cb:
            status_cb(f"⏳ Line {i+1}/{len(dialogue_lines)} — {speaker}...")

        try:
            proc_text  = apply_pronunciation(text)
            voice_name = speaker_voices.get(speaker, voice_keys[0])
            voice_id   = VOICES.get(voice_name, list(VOICES.values())[0])

            line_chunks  = chunk_text(proc_text)
            line_samples = []
            for chunk in line_chunks:
                if cancel_event and cancel_event.is_set():
                    raise GenerationCancelled()
                samp, sr = kokoro.create(chunk, voice=voice_id, speed=speed)
                line_samples.append(samp)
                sample_rate = sr

            seg_audio = np.concatenate(line_samples)
            dur = len(seg_audio) / sample_rate
            timings.append((offset, offset + dur, f"{speaker}: {text}"))
            parts.append(seg_audio)
            offset += dur

            # Pause between lines
            if i < len(dialogue_lines) - 1:
                next_speaker = dialogue_lines[i + 1][0]
                pause = PAUSE_DIFF if next_speaker != speaker else PAUSE_SAME
                parts.append(np.zeros(int(sample_rate * pause), dtype=np.float32))
                offset += pause

        except GenerationCancelled:
            raise
        except Exception as e:
            failed_lines.append((i + 1, speaker, str(e)))
            if status_cb:
                status_cb(f"⚠️ Skipped line {i+1} ({speaker}): {e}")
            continue

    if not parts:
        raise RuntimeError("All dialogue lines failed to generate. Check your script and voices.")

    combined = np.concatenate(parts)

    pre_dur = offset
    if trim_var.get():
        if status_cb: status_cb("✂️ Trimming silence...")
        combined = trim_silence(combined, sample_rate)
    if status_cb: status_cb("🎛️ Applying enhancements...")
    enhanced = apply_enhancements(combined, sample_rate)

    post_dur = len(enhanced) / sample_rate
    scale    = post_dur / pre_dur if pre_dur > 0 else 1.0
    segments = [(s * scale, e * scale, t) for s, e, t in timings]

    return enhanced, sample_rate, segments, failed_lines

# ── Profiles ──────────────────────────────────────────────────────────────────
def get_current_settings():
    return {
        "voice": voice_var.get(), "speed": speed_slider.get(),
        "highpass": highpass_slider.get(), "lowpass": lowpass_slider.get(),
        "reverb": reverb_slider.get(), "compressor": compressor_var.get(),
        "compressor_ratio": compressor_slider.get(), "gain": gain_slider.get(),
        "noise_gate": noise_gate_var.get(), "trim": trim_var.get(),
    }

def apply_settings(s):
    voice_var.set(s.get("voice", "🇬🇧 Male - George (Best)"))
    speed_slider.set(s.get("speed", 0.85))
    highpass_slider.set(s.get("highpass", 20))
    lowpass_slider.set(s.get("lowpass", 18000))
    reverb_slider.set(s.get("reverb", 0.0))
    compressor_var.set(s.get("compressor", True))
    compressor_slider.set(s.get("compressor_ratio", 2.0))
    gain_slider.set(s.get("gain", 0))
    noise_gate_var.set(s.get("noise_gate", False))
    trim_var.set(s.get("trim", True))
    update_all_labels()
    update_word_count()
    eq_preset_var.set("Custom")

EQ_PRESETS = {
    "Custom":             None,
    "🎚️ Flat":            {"highpass": 20,  "lowpass": 18000, "compressor": False, "compressor_ratio": 1.0, "reverb": 0.0,  "gain": 0, "noise_gate": False, "trim": False},
    "🎙️ Podcast":         {"highpass": 100, "lowpass": 14000, "compressor": True,  "compressor_ratio": 4.0, "reverb": 0.05, "gain": 3, "noise_gate": True,  "trim": True},
    "📖 Audiobook":       {"highpass": 60,  "lowpass": 16000, "compressor": True,  "compressor_ratio": 2.5, "reverb": 0.08, "gain": 1, "noise_gate": False, "trim": True},
    "📻 Broadcast":       {"highpass": 120, "lowpass": 12000, "compressor": True,  "compressor_ratio": 6.0, "reverb": 0.03, "gain": 4, "noise_gate": True,  "trim": True},
    "🎬 Cinematic":       {"highpass": 40,  "lowpass": 15000, "compressor": True,  "compressor_ratio": 2.0, "reverb": 0.30, "gain": 0, "noise_gate": False, "trim": True},
    "🌙 Warm & Intimate": {"highpass": 80,  "lowpass": 10000, "compressor": True,  "compressor_ratio": 3.0, "reverb": 0.15, "gain": 2, "noise_gate": True,  "trim": True},
}

def load_profiles():
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "🌙 Calm Narrator": {"voice":"🇬🇧 Male - George (Best)","speed":0.85,"highpass":80,"lowpass":12000,"reverb":0.15,"compressor":True,"compressor_ratio":3.0,"gain":2,"noise_gate":True,"trim":True},
        "🎙️ Podcast":       {"voice":"🇺🇸 Male - Michael","speed":1.0,"highpass":100,"lowpass":14000,"reverb":0.05,"compressor":True,"compressor_ratio":4.0,"gain":3,"noise_gate":True,"trim":True},
        "📖 Audiobook":     {"voice":"🇬🇧 Male - George (Best)","speed":0.9,"highpass":60,"lowpass":16000,"reverb":0.08,"compressor":True,"compressor_ratio":2.5,"gain":1,"noise_gate":False,"trim":True},
    }

def save_profiles(p):
    try:
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=2)
    except OSError:
        pass

def refresh_profile_menu():
    profile_menu.configure(values=list(load_profiles().keys()))

def save_profile():
    name = profile_name_entry.get().strip()
    if not name:
        status_label.configure(text="⚠️ Enter a profile name first.")
        return
    p = load_profiles()
    p[name] = get_current_settings()
    save_profiles(p)
    refresh_profile_menu()
    profile_var.set(name)
    status_label.configure(text=f"✅ Profile '{name}' saved.")

def load_profile():
    name = profile_var.get()
    p    = load_profiles()
    if name in p:
        apply_settings(p[name])
        status_label.configure(text=f"✅ Profile '{name}' loaded.")

def delete_profile():
    name = profile_var.get()
    p    = load_profiles()
    if name in p:
        if not messagebox.askyesno("Delete Profile", f"Delete '{name}'?"):
            return
        del p[name]
        save_profiles(p)
        refresh_profile_menu()
        remaining = list(load_profiles().keys())
        if remaining: profile_var.set(remaining[0])
        status_label.configure(text=f"🗑 Profile '{name}' deleted.")

_applying_eq_preset = False

def apply_eq_preset(name=None):
    global _applying_eq_preset
    name = name or eq_preset_var.get()
    p = EQ_PRESETS.get(name)
    if not p:
        return
    _applying_eq_preset = True
    highpass_slider.set(p["highpass"])
    lowpass_slider.set(p["lowpass"])
    compressor_var.set(p["compressor"])
    compressor_slider.set(p["compressor_ratio"])
    reverb_slider.set(p["reverb"])
    gain_slider.set(p["gain"])
    noise_gate_var.set(p["noise_gate"])
    trim_var.set(p["trim"])
    _applying_eq_preset = False
    update_all_labels()

# ── Cancellation ─────────────────────────────────────────────────────────────
# GenerationCancelled imported from tts_utils
_cancel_event = threading.Event()

def cancel_generation():
    """Signal the running generation thread to stop after the current chunk."""
    _cancel_event.set()

# ── Queue ─────────────────────────────────────────────────────────────────────
queue_items   = []
is_generating  = False
_queue_counter = 0     # monotonically increasing; not reset on remove/clear

def queue_add():
    global _queue_counter
    text = text_input.get("1.0", "end").strip()
    if not text:
        status_label.configure(text="⚠️ No text to add.")
        return

    _queue_counter += 1
    # Suggest first ~5 words of the text as the default name
    suggested = " ".join(text.split()[:5])
    if len(text.split()) > 5:
        suggested += "…"

    # ── Name popup ────────────────────────────────────────────────────────────
    dlg = ctk.CTkToplevel(app)
    dlg.title("Name this queue item")
    _center_window(dlg, 360, 140)
    dlg.resizable(False, False)
    dlg.configure(fg_color=C_BG)
    dlg.grab_set()
    dlg.transient(app)

    ctk.CTkLabel(dlg, text="Queue item name:",
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT2).pack(anchor="w", padx=20, pady=(18, 4))

    name_entry = ctk.CTkEntry(dlg, width=320, height=34,
                              font=ctk.CTkFont(family="Segoe UI", size=12))
    name_entry.insert(0, suggested)
    name_entry.select_range(0, "end")
    name_entry.pack(padx=20)
    name_entry.focus_set()

    btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
    btn_row.pack(fill="x", padx=20, pady=(12, 0))

    def _confirm(e=None):
        name = name_entry.get().strip() or suggested
        queue_items.append({"name": name, "text": text})
        refresh_queue_display()
        status_label.configure(text=f"✅ Added '{name}'. {len(queue_items)} item(s) in queue.")
        dlg.destroy()

    ctk.CTkButton(btn_row, text="Add to Queue", width=120, height=32,
                  font=ctk.CTkFont(family="Segoe UI", size=12),
                  fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#000000",
                  command=_confirm).pack(side="left", padx=(0, 8))
    ctk.CTkButton(btn_row, text="Cancel", width=80, height=32,
                  font=ctk.CTkFont(family="Segoe UI", size=12),
                  **BTN_GHOST, command=dlg.destroy).pack(side="left")

    name_entry.bind("<Return>", _confirm)
    dlg.bind("<Escape>", lambda _e: dlg.destroy())

def queue_remove():
    sel = queue_listbox.curselection()
    if not sel:
        status_label.configure(text="⚠️ Click an item first.")
        return
    queue_items.pop(sel[0])
    refresh_queue_display()

def queue_clear():
    queue_items.clear()
    refresh_queue_display()
    status_label.configure(text="🗑 Queue cleared.")

def refresh_queue_display():
    queue_listbox.delete(0, "end")
    for i, item in enumerate(queue_items):
        words = len(item["text"].split())
        proc  = format_time(estimate_processing_time(item["text"]))
        audio = format_time(estimate_audio_duration(item["text"], speed_slider.get()))
        queue_listbox.insert("end",
            f"  {i+1}. {item['name']}  —  {words:,} words  |  Process: ~{proc}  |  Audio: ~{audio}")
    update_queue_estimate()

def update_queue_estimate():
    if not queue_items:
        queue_estimate_label.configure(text="Queue is empty.")
        return
    total_proc  = sum(estimate_processing_time(i["text"]) for i in queue_items)
    total_audio = sum(estimate_audio_duration(i["text"], speed_slider.get()) for i in queue_items)
    total_words = sum(len(i["text"].split()) for i in queue_items)
    queue_estimate_label.configure(
        text=f"📊 {len(queue_items)} items  |  {total_words:,} words  |  "
             f"Processing: ~{format_time(total_proc)}  |  Total audio: ~{format_time(total_audio)}"
    )

def _encode_mp3_file(out_path, samples, sr, bitrate, title="", artist=""):
    """Encode samples to MP3 and write ID3 tags. Runs on background thread."""
    import lameenc
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16)
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1).astype(np.int16)
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(bitrate)
    encoder.set_in_sample_rate(sr)
    encoder.set_channels(1)
    encoder.set_quality(2)
    mp3_data = encoder.encode(pcm.tobytes()) + encoder.flush()
    with open(out_path, "wb") as f:
        f.write(mp3_data)
    _write_id3v2(out_path, title=title, artist=artist,
                 comment=f"Generated by VoxWild v{VERSION}")


def queue_generate_all():
    if not queue_items:
        status_label.configure(text="⚠️ Queue is empty.")
        return
    folder = get_default_folder()
    out_dir = filedialog.askdirectory(title="Choose output folder",
                                      initialdir=folder or None)
    if not out_dir: return
    set_default_folder(out_dir)

    use_mp3      = queue_fmt_var.get() == "MP3"
    bitrate      = {"128 kbps": 128, "192 kbps": 192, "320 kbps": 320}.get(
                       queue_mp3_quality_var.get(), 192)
    ext          = ".mp3" if use_mp3 else ".wav"
    queue_natural = engine_var.get() == "Natural"

    # ── Freemium gate: Natural mode — check before starting the batch ─────────
    if queue_natural and not _lic.can_use_natural():
        _show_upsell_modal("natural")
        return

    voice       = VOICES[voice_var.get()]
    if queue_natural:
        _sel = cb_clone_var.get()
        voice_name = _sel if _sel != _CLONE_DEFAULT else "Default"
    else:
        voice_name = voice_var.get()
    speed       = round(speed_slider.get(), 2)
    total_items = len(queue_items)
    total_words = sum(len(i["text"].split()) for i in queue_items)
    est_total   = estimate_processing_time(" ".join(i["text"] for i in queue_items))

    queue_gen_btn.configure(state="disabled")
    play_button.configure(state="disabled")
    smooth.start(est_total)
    _cancel_event.clear()

    def run():
        global is_generating
        is_generating = True
        words_done    = 0
        cancelled     = False
        i = -1
        for i, item in enumerate(queue_items):
            if _cancel_event.is_set():
                cancelled = True
                break
            # Per-item Natural gate (uses can run out mid-batch for free users)
            if queue_natural and not _lic.can_use_natural():
                app.after(0, lambda _i=i: status_label.configure(
                    text=f"⚠️ Natural mode limit reached after {_i} item(s). Upgrade to Pro for unlimited."))
                app.after(0, lambda: _show_upsell_modal("natural"))
                cancelled = True
                break
            def scb(msg, _i=i): app.after(0, lambda m=f"[{_i+1}/{total_items}] {msg}": status_label.configure(text=m))
            scb("Starting...")
            t0 = time.time()
            try:
                prog_lo = i / total_items
                prog_hi = (i + 1) / total_items
                samples, sr, segments = generate_audio(
                    item["text"], voice, speed,
                    status_cb=scb,
                    progress_range=(prog_lo, prog_hi * 0.95),
                )
                if queue_natural:
                    _lic.record_natural_use()
                words_done += len(item["text"].split())
                safe_name = item['name'].replace(' ', '_')
                out_path  = os.path.join(out_dir, f"{i+1:02d}_{safe_name}{ext}")
                # ── Resemble Enhance (if enabled and licensed) ────────────────
                if enhance_var.get() and _lic.can_use_enhance():
                    try:
                        scb("✨ Enhancing...")
                        mode = enhance_mode.get()
                        device = "cuda" if mode == "GPU" else "cpu"
                        input_tmp  = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                        output_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                        input_tmp.close()
                        output_tmp.close()
                        sf.write(input_tmp.name, samples, sr)
                        enhance_engine.start()
                        new_sr, _ = enhance_engine.enhance(
                            input_tmp.name, output_tmp.name, device=device)
                        samples, sr = sf.read(output_tmp.name, dtype="float32")
                        sr = new_sr
                        _lic.record_enhance_use()
                    except Exception as _ee:
                        scb(f"⚠️ Enhance failed, saving unenhanced: {_fmt_err(_ee)}")
                    finally:
                        for _t in (input_tmp.name, output_tmp.name):
                            try: os.unlink(_t)
                            except OSError: pass

                if use_mp3:
                    scb(f"Encoding MP3...")
                    _encode_mp3_file(out_path, samples, sr, bitrate,
                                     title=item["name"], artist=voice_name)
                else:
                    sf.write(out_path, samples, sr)
                record_calibration(len(item["text"].split()), time.time() - t0)
                add_to_history(samples, sr, item["text"], voice_name, segments=segments)
                scb(f"✅ Saved {item['name']}{ext}")
            except GenerationCancelled:
                cancelled = True
                break
            except Exception as e:
                _log_crash(e)
                scb(f"❌ {_fmt_err(e)}")
            time.sleep(0.05)
        smooth.finish()
        is_generating = False
        completed = i + 1
        fmt_str = "MP3" if use_mp3 else "WAV"
        if cancelled:
            app.after(0, lambda c=completed, t=total_items: status_label.configure(
                text=f"⏹ Queue cancelled. {c} of {t} items completed."))
        else:
            app.after(0, lambda t=total_items, d=out_dir, f=fmt_str: status_label.configure(
                text=f"✅ Queue complete! {t} {f} files saved to {d}"))
        app.after(0, lambda: queue_gen_btn.configure(state="normal"))
        app.after(0, lambda: play_button.configure(state="normal", text="Generate"))

    threading.Thread(target=run, daemon=True).start()

# ── Word Counter ──────────────────────────────────────────────────────────────
_wc_after_id = None

def update_word_count(*_):
    """Debounced: schedules the actual update 150 ms after the last call."""
    global _wc_after_id
    if _wc_after_id:
        app.after_cancel(_wc_after_id)
    _wc_after_id = app.after(150, _do_word_count)

def _do_word_count():
    global _wc_after_id
    _wc_after_id = None
    text  = text_input.get("1.0", "end").strip()
    words = len(text.split()) if text else 0
    chars = len(text)
    speed = speed_slider.get()
    audio = estimate_audio_duration(text, speed)
    proc  = estimate_processing_time(text)
    word_count_label.configure(
        text=f"Words: {words:,}  |  Chars: {chars:,}  |  "
             f"Audio: ~{format_time(audio)}  |  Processing: ~{format_time(proc)}"
    )

# ── Main Actions ──────────────────────────────────────────────────────────────
def generate_and_store():
    """Generate audio, store in history. Does NOT auto-play."""
    global is_generating
    text = text_input.get("1.0", "end").strip()
    if not text:
        status_label.configure(text="⚠️ Please enter some text.")
        return

    using_natural = engine_var.get() == "Natural"

    # ── Freemium gate: Natural mode ───────────────────────────────────────────
    if using_natural and not _lic.can_use_natural():
        _show_upsell_modal("natural")
        return

    voice      = VOICES[voice_var.get()]
    if using_natural:
        _sel = cb_clone_var.get()
        voice_name = _sel if _sel != _CLONE_DEFAULT else "Default"
    else:
        voice_name = voice_var.get()
    speed      = round(speed_slider.get(), 2)
    words      = len(text.split())
    est        = estimate_processing_time(text)

    _cancel_event.clear()
    play_button.configure(state="disabled", text="Working...")
    stop_button.configure(state="normal", text="Cancel",
                          fg_color="#2a0f0f", hover_color="#3d1515",
                          text_color=C_DANGER, border_width=1, border_color="#3d1515")
    queue_gen_btn.configure(state="disabled")
    smooth.start(est)
    is_generating = True

    def run():
        global is_generating
        t0 = time.time()
        try:
            samples, sr, segments = generate_audio(
                text, voice, speed,
                status_cb=lambda m: app.after(0, lambda m=m: status_label.configure(text=m))
            )
            elapsed = time.time() - t0
            record_calibration(words, elapsed)
            if using_natural:
                _lic.record_natural_use()
            smooth.finish()
            add_to_history(samples, sr, text, voice_name, segments=segments)
            app.after(0, lambda: status_label.configure(
                text="✅ Audio ready! Click ▶ Play in the history panel."))
            app.after(0, update_word_count)  # refresh calibration note
        except GenerationCancelled:
            smooth.finish()
            app.after(0, lambda: status_label.configure(text="Generation cancelled."))
        except Exception as e:
            _log_crash(e)
            smooth.finish()
            _msg = _fmt_err(e)
            app.after(0, lambda m=_msg: status_label.configure(text=f"❌ {m}"))
        finally:
            is_generating = False
            app.after(0, lambda: play_button.configure(state="normal", text="Generate"))
            app.after(0, lambda: stop_button.configure(
                state="disabled", text="Stop",
                fg_color="transparent", hover_color=C_ELEVATED,
                text_color=C_TXT2, border_width=1, border_color=C_BORDER))
            app.after(0, lambda: queue_gen_btn.configure(state="normal"))

    threading.Thread(target=run, daemon=True).start()


def stop_audio():
    if is_generating:
        cancel_generation()   # signals the generation thread to stop after current chunk
    else:
        sd.stop()
        status_label.configure(text="Stopped.")
        play_button.configure(state="normal", text="Generate")
        stop_button.configure(state="disabled", text="Stop",
                              fg_color="transparent", hover_color=C_ELEVATED,
                              text_color=C_TXT2, border_width=1, border_color=C_BORDER)

def preview_voice():
    voice = VOICES[voice_var.get()]
    speed = round(speed_slider.get(), 2)
    preview_button.configure(state="disabled")

    def run():
        try:
            samples, sr = kokoro.create(
                "Hello! This is a preview of the selected voice.", voice=voice, speed=speed)
            enhanced = apply_enhancements(samples, sr)
            sd.play(enhanced, sr)
            sd.wait()
            app.after(0, lambda: status_label.configure(text="✅ Preview done!"))
        except Exception as e:
            _log_crash(e)
            _msg = _fmt_err(e)
            app.after(0, lambda m=_msg: status_label.configure(text=f"❌ {m}"))
        finally:
            app.after(0, lambda: preview_button.configure(state="normal"))

    threading.Thread(target=run, daemon=True).start()

def import_file():
    folder = get_default_folder()
    fp = filedialog.askopenfilename(
        initialdir=folder or None,
        filetypes=[("Text files", "*.txt")]
    )
    if fp:
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                with open(fp, "r", encoding=enc) as f:
                    content = f.read()
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            status_label.configure(text="❌ Could not decode file — unknown encoding.")
            return
        if _get_settings().get("auto_clean_text", False):
            content, _ = clean_text(content)
        text_input.delete("1.0", "end")
        text_input.insert("1.0", content)
        update_word_count()
        status_label.configure(text=f"✅ Imported {len(content):,} characters.")

def clear_text():
    text_input.delete("1.0", "end")
    update_word_count()
    status_label.configure(text="Ready.")


def update_all_labels(*_):
    hp_label.configure(text=f"{int(highpass_slider.get())} Hz")
    lp_label.configure(text=f"{int(lowpass_slider.get())} Hz")
    reverb_label.configure(text=f"{reverb_slider.get():.2f}")
    comp_label.configure(text=f"{compressor_slider.get():.1f}x")
    gain_label.configure(text=f"{gain_slider.get():+.0f} dB")
    speed_label.configure(text=f"{speed_slider.get():.2f}x")
    try:
        cb_exag_label.configure(text=f"{cb_exag_slider.get():.2f}")
        cb_cfg_label.configure(text=f"{cb_cfg_slider.get():.2f}")
    except NameError:
        pass  # Chatterbox widgets not yet created
    # NOTE: update_word_count is intentionally NOT called here.
    # Sliders don't change the word count — only text changes do.
    # Calling it here caused a calibration disk read on every slider tick.

def reset_enhancements():
    highpass_slider.set(20);  lowpass_slider.set(18000)
    reverb_slider.set(0.0);   compressor_slider.set(2.0)
    gain_slider.set(0);       compressor_var.set(False)
    noise_gate_var.set(False); trim_var.set(True)
    update_all_labels()
    status_label.configure(text="🔄 Enhancements reset.")

# ── About Window ──────────────────────────────────────────────────────────────

# ── Text Cleaner Window ───────────────────────────────────────────────────────
def show_text_cleaner():
    text = text_input.get("1.0", "end").strip()
    if not text:
        status_label.configure(text="⚠️ No text to clean.")
        return

    win = ctk.CTkToplevel(app)
    win.title("Text Cleaner")
    _center_window(win, 620, 520)
    win.resizable(True, True)
    win.configure(fg_color=C_BG)
    win.grab_set()
    _fade_in(win)

    header = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=56)
    header.pack(fill="x")
    header.pack_propagate(False)
    ctk.CTkLabel(header, text="Text Cleaner",
                 font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
                 text_color=C_TXT).pack(side="left", padx=20)

    _, changes = clean_text(text)
    cleaned, _ = clean_text(text)

    if changes:
        summary = "Will fix:  " + "  ·  ".join(changes[:4]) + ("  ·  …" if len(changes) > 4 else "")
        s_color, s_bg = C_WARN, "#2a1f0a"
    else:
        summary = "Text looks clean — no changes needed."
        s_color, s_bg = C_SUCCESS, "#0a2010"

    info = ctk.CTkFrame(win, fg_color=s_bg, corner_radius=0, height=34)
    info.pack(fill="x")
    info.pack_propagate(False)
    ctk.CTkLabel(info, text=summary, text_color=s_color,
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 anchor="w").pack(side="left", padx=16, pady=8)

    preview_tabs = ctk.CTkTabview(win, fg_color=C_BG)
    preview_tabs.pack(fill="both", expand=True, padx=16, pady=(12, 8))
    preview_tabs.add("Before")
    preview_tabs.add("After")

    font_mono = ctk.CTkFont(family="Consolas", size=12)
    before_box = ctk.CTkTextbox(preview_tabs.tab("Before"), font=font_mono)
    before_box.pack(fill="both", expand=True)
    before_box.insert("1.0", text)
    before_box.configure(state="disabled")

    after_box = ctk.CTkTextbox(preview_tabs.tab("After"), font=font_mono)
    after_box.pack(fill="both", expand=True)
    after_box.insert("1.0", cleaned)
    after_box.configure(state="disabled")

    btn_frame = ctk.CTkFrame(win, fg_color="transparent")
    btn_frame.pack(fill="x", padx=16, pady=(0, 16))

    def apply_clean():
        text_input.delete("1.0", "end")
        text_input.insert("1.0", cleaned)
        update_word_count()
        status_label.configure(text=f"✅ Text cleaned.")
        win.destroy()

    ctk.CTkButton(btn_frame, text="Apply Changes", command=apply_clean,
                  width=150, height=36,
                  font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold")).pack(side="left", padx=(0, 8))
    ctk.CTkButton(btn_frame, text="Cancel", command=win.destroy,
                  width=90, height=36, **BTN_GHOST).pack(side="left")

# ── Settings ──────────────────────────────────────────────────────────────────
def show_settings():
    from settings_window import open_settings_window, load_settings
    profiles = list(load_profiles().keys())

    def on_saved(new_settings):
        global _settings_cache
        _settings_cache = new_settings  # keep cache in sync with what was just saved
        # Apply default voice if changed
        if new_settings.get("default_voice") in VOICES:
            voice_var.set(new_settings["default_voice"])
        # Apply default speed
        speed_slider.set(new_settings.get("default_speed", 0.85))
        update_all_labels()
        status_label.configure(text="✅ Settings saved.")

    open_settings_window(app, list(VOICES.keys()), profiles, on_save_callback=on_saved)


def show_about():
    win = ctk.CTkToplevel(app)
    win.title("About VoxWild")
    _center_window(win, 480, 640)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    _fade_in(win)

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=170)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    if LOGO_IMG_LG:
        ctk.CTkLabel(hdr, image=LOGO_IMG_LG, text="").pack(pady=(16, 6))
    ctk.CTkLabel(hdr, text="VoxWild",
                 font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
                 text_color=C_TXT).pack()
    ctk.CTkLabel(hdr, text=f"v{VERSION}",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_ACCENT).pack(pady=(2, 0))
    ctk.CTkLabel(hdr, text="Offline AI text-to-speech · Made by Cookie Studios",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT3).pack(pady=(2, 0))

    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    # ── Scrollable body ───────────────────────────────────────────────────────
    scroll = ctk.CTkScrollableFrame(win, fg_color="transparent",
                                    scrollbar_button_color=C_ELEVATED,
                                    scrollbar_button_hover_color=C_ACCENT_D)
    scroll.pack(fill="both", expand=True, padx=24, pady=(16, 0))

    def _section(text):
        ctk.CTkLabel(scroll, text=text,
                     font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                     text_color=C_TXT3, anchor="w").pack(fill="x", pady=(14, 6))

    def _link_row(label, url):
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text=label, width=110, anchor="w",
                     font=ctk.CTkFont(family="Segoe UI", size=11),
                     text_color=C_TXT3).pack(side="left")
        link = ctk.CTkLabel(row, text=url, anchor="w",
                            font=ctk.CTkFont(family="Segoe UI", size=11, underline=True),
                            text_color=C_ACCENT, cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda e, u=url: webbrowser.open(
            u if u.startswith("http") else f"mailto:{u}"))

    # ── Watermark disclosure ──────────────────────────────────────────────────
    notice = ctk.CTkFrame(scroll, fg_color=C_ELEVATED, corner_radius=8,
                          border_width=1, border_color=C_ACCENT_D)
    notice.pack(fill="x", pady=(0, 6))
    ctk.CTkLabel(notice,
                 text="Watermark Notice",
                 font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                 text_color=C_ACCENT, anchor="w").pack(anchor="w", padx=14, pady=(10, 4))
    ctk.CTkLabel(notice,
                 text="Natural mode audio contains an inaudible AI watermark "
                      "(Resemble Perth). Fast mode audio has no watermark.",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT2, anchor="w", justify="left",
                 wraplength=400).pack(anchor="w", padx=14, pady=(0, 12))

    # ── Shortcuts ─────────────────────────────────────────────────────────────
    _section("KEYBOARD SHORTCUTS")
    shortcuts = [
        ("Ctrl + Enter", "Generate audio"),
        ("Ctrl + P",     "Play latest"),
        ("Ctrl + S",     "Save latest"),
        ("Ctrl + I",     "Import text file"),
        ("Ctrl + Q",     "Add to queue"),
        ("Escape",       "Stop playback"),
        ("Ctrl + /",     "Open About"),
    ]
    for key, desc in shortcuts:
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=1)
        key_badge = ctk.CTkFrame(row, fg_color=C_CARD, corner_radius=4,
                                 border_width=1, border_color=C_BORDER)
        key_badge.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(key_badge, text=key,
                     font=ctk.CTkFont(family="Consolas", size=10),
                     text_color=C_ACCENT).pack(padx=8, pady=3)
        ctk.CTkLabel(row, text=desc, anchor="w",
                     font=ctk.CTkFont(family="Segoe UI", size=11),
                     text_color=C_TXT2).pack(side="left")

    # ── Links ─────────────────────────────────────────────────────────────────
    _section("SUPPORT & LINKS")
    _link_row("Email",    "cookiestudios.dev@gmail.com")
    _link_row("Website",  "https://voxwild.com")
    _link_row("Store",    "https://cookiestudios.gumroad.com")
    _link_row("Updates",  "https://github.com/tagee1/VoxWild/releases")

    # ── Credits ───────────────────────────────────────────────────────────────
    _section("BUILT WITH")
    ctk.CTkLabel(scroll,
                 text="Kokoro TTS · Chatterbox TTS · Resemble Perth · Resemble Enhance\n"
                      "PyTorch · ONNX Runtime · CustomTkinter · NumPy · SciPy",
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color=C_TXT3, anchor="w", justify="left").pack(anchor="w", pady=(0, 4))
    ctk.CTkLabel(scroll,
                 text="All open source. Full license list in CREDITS.txt.",
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color=C_TXT3, anchor="w").pack(anchor="w")

    ctk.CTkLabel(scroll, text=" ", text_color=C_BG).pack()  # bottom padding

    # ── Footer ────────────────────────────────────────────────────────────────
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")
    foot = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=52)
    foot.pack(fill="x")
    foot.pack_propagate(False)
    ctk.CTkLabel(foot, text=f"© 2026 Cookie Studios",
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color=C_TXT3).pack(side="left", padx=20, pady=18)
    ctk.CTkButton(foot, text="Close", command=win.destroy,
                  width=100, height=32, **BTN_GHOST).pack(side="right", padx=16, pady=10)

# ── Close Handler ─────────────────────────────────────────────────────────────
def on_close():
    if is_generating:
        if not messagebox.askyesno(
            "Generation in progress",
            "Audio is still being generated.\nAre you sure you want to quit? It will be cancelled."
        ):
            return
    _save_fx_settings()
    sd.stop()
    chatterbox_engine.stop()
    app.destroy()

app.protocol("WM_DELETE_WINDOW", on_close)

# ── Keyboard Shortcuts ────────────────────────────────────────────────────────
def _shortcut_generate(e=None): generate_and_store()
def _shortcut_save(e=None):
    if audio_history: download_history_entry(audio_history[0])
def _shortcut_play_latest(e=None):
    if audio_history: play_history_entry(audio_history[0])
def _shortcut_import(e=None): import_file()
def _shortcut_queue(e=None):  queue_add()
def _shortcut_stop(e=None):   stop_audio()
def _shortcut_about(e=None):  show_about()

app.bind("<Control-Return>",    _shortcut_generate)
app.bind("<Control-s>",         _shortcut_save)
app.bind("<Control-S>",         _shortcut_save)
app.bind("<Control-p>",         _shortcut_play_latest)
app.bind("<Control-P>",         _shortcut_play_latest)
app.bind("<Control-i>",         _shortcut_import)
app.bind("<Control-I>",         _shortcut_import)
app.bind("<Control-q>",         _shortcut_queue)
app.bind("<Control-Q>",         _shortcut_queue)
app.bind("<Escape>",            _shortcut_stop)
app.bind("<Control-slash>",     _shortcut_about)

# ══════════════════════════════════════════════════════════════════════════════
# UI — Wild Emerald Theme
# ══════════════════════════════════════════════════════════════════════════════

def _sep(parent, pady=0):
    """Thin horizontal divider line."""
    ctk.CTkFrame(parent, fg_color=C_BORDER, height=1,
                 corner_radius=0).pack(fill="x", pady=pady)

# ── Tooltip system ────────────────────────────────────────────────────────────
class _Tooltip:
    """Dark-themed hover tooltip. Appears 500 ms after mouse enters, hides on leave."""
    def __init__(self, widget, text):
        self._widget  = widget
        self._text    = text
        self._job     = None
        self._win     = None
        widget.bind("<Enter>",       self._on_enter, add="+")
        widget.bind("<Leave>",       self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _event):
        self._job = self._widget.after(500, self._show)

    def _on_leave(self, _event):
        if self._job:
            self._widget.after_cancel(self._job)
            self._job = None
        self._hide()

    def _show(self):
        if self._win:
            return
        try:
            wx = self._widget.winfo_rootx()
            wy = self._widget.winfo_rooty()
            wh = self._widget.winfo_height()
        except Exception:
            return
        self._win = ctk.CTkToplevel(self._widget)
        self._win.wm_overrideredirect(True)
        self._win.wm_attributes("-topmost", True)
        self._win.wm_geometry(f"+{wx + 4}+{wy + wh + 4}")
        frame = ctk.CTkFrame(self._win, fg_color=C_ELEVATED,
                             border_color=C_BORDER, border_width=1, corner_radius=7)
        frame.pack()
        ctk.CTkLabel(frame, text=self._text, wraplength=260, justify="left",
                     font=ctk.CTkFont(family="Segoe UI", size=12),
                     text_color=C_TXT2).pack(padx=12, pady=(8, 9))

    def _hide(self):
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None


def _info_btn(parent, tooltip_text):
    """Small ⓘ label with hover tooltip. Pack it after calling this."""
    lbl = ctk.CTkLabel(parent, text="ⓘ",
                       font=ctk.CTkFont(family="Segoe UI", size=11),
                       text_color=C_TXT3, cursor="question_arrow", width=16)
    _Tooltip(lbl, tooltip_text)
    return lbl


def _section_label(parent, text, padx=14, pady=(12, 6), tooltip=None):
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(anchor="w", padx=padx, pady=pady)
    ctk.CTkFrame(row, fg_color=C_ACCENT, width=3, height=10,
                 corner_radius=2).pack(side="left", padx=(0, 6))
    ctk.CTkLabel(row, text=text,
                 font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                 text_color=C_TXT2, anchor="w").pack(side="left")
    if tooltip:
        _info_btn(row, tooltip).pack(side="left", padx=(5, 0))

# ── Status Bar (packed first so it stays at bottom) ───────────────────────────
status_bar = ctk.CTkFrame(app, fg_color=C_SURFACE, corner_radius=0, height=32)
status_bar.pack(fill="x", side="bottom")
status_bar.pack_propagate(False)
_sep(status_bar)
status_label = ctk.CTkLabel(
    status_bar,
    text="Ready  ·  Ctrl+Enter to generate  ·  Ctrl+P to play  ·  Esc to stop",
    anchor="w", text_color=C_TXT3,
    font=ctk.CTkFont(family="Segoe UI", size=12))
status_label.pack(side="left", padx=16, pady=6)

def _copy_status():
    txt = status_label.cget("text")
    app.clipboard_clear()
    app.clipboard_append(txt)

_copy_btn = ctk.CTkButton(
    status_bar, text="⎘", width=24, height=20,
    font=ctk.CTkFont(family="Segoe UI", size=11),
    fg_color="transparent", hover_color=C_BORDER,
    text_color=C_TXT3, corner_radius=4,
    command=_copy_status)
_copy_btn.pack(side="right", padx=(0, 8), pady=6)

# ── Header ────────────────────────────────────────────────────────────────────
header = ctk.CTkFrame(app, fg_color=C_SURFACE, corner_radius=0, height=58)
header.pack(fill="x")
header.pack_propagate(False)

# Left: logo + title + version
title_left = ctk.CTkFrame(header, fg_color="transparent")
title_left.pack(side="left", padx=18, pady=10)
if LOGO_IMG_SM:
    ctk.CTkLabel(title_left, image=LOGO_IMG_SM, text="").pack(side="left", padx=(0, 10))
else:
    ctk.CTkFrame(title_left, fg_color=C_ACCENT, width=10, height=10,
                 corner_radius=5).pack(side="left", padx=(0, 10))
ctk.CTkLabel(title_left, text="VoxWild",
             font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
             text_color=C_TXT).pack(side="left")
ctk.CTkLabel(title_left, text=f" v{VERSION}",
             font=ctk.CTkFont(family="Segoe UI", size=11),
             text_color=C_TXT3).pack(side="left")

# Right: action buttons (packed right-to-left)
ctk.CTkButton(header, text="About", command=show_about,
              width=72, height=30,
              font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST).pack(side="right", padx=(0, 14), pady=14)
ctk.CTkButton(header, text="Settings", command=show_settings,
              width=84, height=30,
              font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST).pack(side="right", padx=(0, 6), pady=14)

_activate_btn = ctk.CTkButton(
    header, text="🔑 Activate", command=lambda: _show_activation_modal(can_skip=True),
    width=98, height=30,
    font=ctk.CTkFont(family="Segoe UI", size=12),
    **BTN_GHOST)
# Only show when not yet activated
if not _lic.load_license().get("activated"):
    _activate_btn.pack(side="right", padx=(0, 6), pady=14)

_sep(app)

# ── Update banner (hidden until _show_update_banner is called) ─────────────────
_update_banner = ctk.CTkFrame(app, fg_color=C_ACCENT_D, corner_radius=0, height=34)
_update_banner.pack_propagate(False)
# Not packed here — _show_update_banner inserts it before prog_row when needed
_update_banner_label = ctk.CTkLabel(
    _update_banner, text="", anchor="w",
    font=ctk.CTkFont(family="Segoe UI", size=12),
    text_color=C_TXT)
_update_banner_label.pack(side="left", padx=14)
_update_banner_link = ctk.CTkButton(
    _update_banner, text="Download ↗", width=94, height=22,
    font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
    fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#000000",
    corner_radius=4, command=lambda: None)
_update_banner_link.pack(side="left", padx=(6, 0))
ctk.CTkButton(
    _update_banner, text="✕", width=28, height=22,
    font=ctk.CTkFont(family="Segoe UI", size=11),
    fg_color="transparent", hover_color=C_BORDER,
    text_color=C_TXT3, corner_radius=4,
    command=lambda: _update_banner.pack_forget()).pack(side="right", padx=(0, 8))

# ── Progress row ──────────────────────────────────────────────────────────────
prog_row = ctk.CTkFrame(app, fg_color=C_SURFACE, corner_radius=0, height=34)
prog_row.pack(fill="x")
prog_row.pack_propagate(False)
progress_bar = ctk.CTkProgressBar(prog_row, height=6, corner_radius=3)
progress_bar.set(0)
progress_bar.pack(side="left", fill="x", expand=True, padx=(16, 10), pady=14)
progress_time_label = ctk.CTkLabel(
    prog_row, text="", width=260, anchor="w",
    font=ctk.CTkFont(family="Segoe UI", size=12), text_color=C_TXT3)
progress_time_label.pack(side="left", padx=(0, 16))

smooth = SmoothProgress(progress_bar, progress_time_label)

_sep(app)

# ── Tabs ──────────────────────────────────────────────────────────────────────
# ── Tab bar + Generate/Stop row ──────────────────────────────────────────────
_tab_row = ctk.CTkFrame(app, fg_color=C_BG, corner_radius=0, height=40)
_tab_row.pack(fill="x")
_tab_row.pack_propagate(False)

stop_button = ctk.CTkButton(
    _tab_row, text="Stop", command=stop_audio,
    width=60, height=28,
    font=ctk.CTkFont(family="Segoe UI", size=12),
    state="disabled", **BTN_GHOST, corner_radius=8)
stop_button.pack(side="right", padx=(0, 14), pady=6)

play_button = ctk.CTkButton(
    _tab_row, text="Generate", command=generate_and_store,
    width=100, height=28,
    font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
    corner_radius=8)
play_button.pack(side="right", padx=(0, 4), pady=6)

tabs = ctk.CTkTabview(app, fg_color=C_BG)
tabs.pack(fill="both", expand=True, padx=0, pady=0)
tabs.add("  Studio  ")
tabs.add("  Queue  ")
tabs.add("  Dialogue  ")
tabs.add("  Profiles  ")

# ══════════════════════════════════════════════════════════════════════════════
# STUDIO TAB
# ══════════════════════════════════════════════════════════════════════════════
studio = tabs.tab("  Studio  ")
studio.configure(fg_color=C_BG)
studio.grid_columnconfigure(0, weight=3)
studio.grid_columnconfigure(1, minsize=248)
studio.grid_columnconfigure(2, minsize=254)
studio.grid_columnconfigure(3, minsize=268)
studio.grid_rowconfigure(0, weight=1)

# helper: make a panel card
def _panel(parent, col, padright=8):
    f = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=12)
    f.grid(row=0, column=col, sticky="nsew", padx=(0, padright), pady=6)
    return f

# ── Text panel ────────────────────────────────────────────────────────────────
text_panel = _panel(studio, 0)

_section_label(text_panel, "TEXT INPUT",
    tooltip="Type or paste any text here. Use Ctrl+Enter to generate. "
            "Long texts are automatically split into chunks and joined seamlessly. "
            "Use the Dialogue tab for multi-speaker scripts.")

text_input = ctk.CTkTextbox(
    text_panel,
    font=ctk.CTkFont(family="Segoe UI", size=13),
    wrap="word",
    fg_color=C_ELEVATED, border_width=0, corner_radius=8,
    text_color=C_TXT,
    scrollbar_button_color=C_BORDER,
    scrollbar_button_hover_color=C_ACCENT_D)
text_input.pack(fill="both", expand=True, padx=14, pady=(0, 6))
text_input.bind("<KeyRelease>", update_word_count)

def _on_paste(e=None):
    app.after(10, update_word_count)  # after paste content lands
    if _get_settings().get("auto_clean_text", False):
        def _clean():
            raw = text_input.get("1.0", "end").strip()
            cleaned, changes = clean_text(raw)
            if changes:
                text_input.delete("1.0", "end")
                text_input.insert("1.0", cleaned)
                update_word_count()
                status_label.configure(text=f"✅ Auto-cleaned: {', '.join(changes[:3])}")
        app.after(20, _clean)

text_input.bind("<<Paste>>", _on_paste)

word_count_label = ctk.CTkLabel(
    text_panel,
    text="Words: 0  ·  Chars: 0  ·  Audio: ~0s  ·  Processing: ~0s",
    font=ctk.CTkFont(family="Segoe UI", size=11), text_color=C_TXT3)
word_count_label.pack(anchor="w", padx=14, pady=(2, 8))

_sep(text_panel, pady=0)

txt_btns = ctk.CTkFrame(text_panel, fg_color="transparent")
txt_btns.pack(fill="x", padx=10, pady=8)
ctk.CTkButton(txt_btns, text="Import", command=import_file,
              width=76, height=30, font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST).pack(side="left", padx=(0, 5))
ctk.CTkButton(txt_btns, text="+ Queue", command=queue_add,
              width=76, height=30, font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST).pack(side="left", padx=(0, 5))
ctk.CTkButton(txt_btns, text="Clean", command=show_text_cleaner,
              width=66, height=30, font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST).pack(side="left", padx=(0, 5))
ctk.CTkButton(txt_btns, text="Dict",
              command=lambda: open_pronunciation_window(app),
              width=54, height=30, font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST).pack(side="left", padx=(0, 5))
ctk.CTkButton(txt_btns, text="Clear", command=clear_text,
              width=54, height=30, font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_DARK).pack(side="left")

# ── Voice + Engine panel ──────────────────────────────────────────────────────
mid_panel = _panel(studio, 1)

_section_label(mid_panel, "ENGINE",
    tooltip="Choose between Fast mode (Kokoro — instant, offline) and Natural mode "
            "(Chatterbox — slower, more human-sounding, supports voice cloning).")
engine_var = ctk.StringVar(value="Fast")
engine_toggle = ctk.CTkSegmentedButton(
    mid_panel, values=["Fast", "Natural"],
    variable=engine_var,
    font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
    width=214)
engine_toggle.pack(padx=14, pady=(0, 12))

_sep(mid_panel)

# Container that swaps between Kokoro and Chatterbox settings
voice_container = ctk.CTkFrame(mid_panel, fg_color="transparent")
voice_container.pack(fill="x")

# ── Kokoro section ──
kokoro_frame = ctk.CTkFrame(voice_container, fg_color="transparent")
kokoro_frame.pack(fill="x")

_section_label(kokoro_frame, "VOICE",
    tooltip="Select a built-in voice for Fast mode. Voices marked (Best) sound the most natural. "
            "US and UK accents are available.")
voice_var = ctk.StringVar(value="🇬🇧 Male - George (Best)")
ctk.CTkOptionMenu(kokoro_frame, variable=voice_var, values=list(VOICES.keys()),
                  width=214, dynamic_resizing=False,
                  font=ctk.CTkFont(family="Segoe UI", size=12)).pack(padx=14, pady=(0, 6))
preview_button = ctk.CTkButton(
    kokoro_frame, text="Preview Voice", command=preview_voice,
    width=214, height=30,
    font=ctk.CTkFont(family="Segoe UI", size=12), **BTN_GHOST)
preview_button.pack(padx=14, pady=(0, 8))

# ── Chatterbox section (hidden initially) ──
cb_frame = ctk.CTkFrame(voice_container, fg_color="transparent")

_section_label(cb_frame, "VOICE CLONE",
    tooltip="Record or import a short audio sample (10–30 sec) to clone a voice. "
            "Natural mode will match its tone, pace, and character. "
            "Leave on Default for the built-in Chatterbox voice.")
cb_clone_path_var = ctk.StringVar(value="")

# ── Clone library dropdown ────────────────────────────────────────────────────
_CLONE_DEFAULT = "Default voice"

def _clone_display_names():
    return [_CLONE_DEFAULT] + [e["name"] for e in load_clone_library()]

cb_clone_var = ctk.StringVar(value=_CLONE_DEFAULT)
cb_clone_menu = ctk.CTkOptionMenu(
    cb_frame, variable=cb_clone_var,
    values=_clone_display_names(),
    font=ctk.CTkFont(family="Segoe UI", size=11),
    width=214, dynamic_resizing=False)
cb_clone_menu.pack(padx=14, pady=(0, 6))

def _apply_clone_selection(name=None):
    name = name or cb_clone_var.get()
    if name == _CLONE_DEFAULT:
        cb_clone_path_var.set("")
        status_label.configure(text="Voice clone: Default voice")
    else:
        lib = load_clone_library()
        entry = next((e for e in lib if e["name"] == name), None)
        if entry and os.path.exists(entry["file"]):
            cb_clone_path_var.set(entry["file"])
            status_label.configure(text=f"Voice clone: {name}")
        else:
            cb_clone_path_var.set("")
            status_label.configure(text="Clone file missing — using default voice.")
    s = _get_settings(); s["selected_clone"] = name; _save_settings(s)

cb_clone_var.trace_add("write", lambda *_: _apply_clone_selection())

def _refresh_clone_menu():
    names = _clone_display_names()
    cb_clone_menu.configure(values=names)
    if cb_clone_var.get() not in names:
        cb_clone_var.set(_CLONE_DEFAULT)

# ── Clone action buttons ──────────────────────────────────────────────────────
cb_clone_btns = ctk.CTkFrame(cb_frame, fg_color="transparent")
cb_clone_btns.pack(fill="x", padx=14, pady=(0, 6))

def _delete_clone():
    name = cb_clone_var.get()
    if name == _CLONE_DEFAULT:
        status_label.configure(text="⚠️ Default voice cannot be deleted.")
        return
    if not messagebox.askyesno("Delete Voice Clone", f"Delete '{name}'? This cannot be undone."):
        return
    lib = load_clone_library()
    entry = next((e for e in lib if e["name"] == name), None)
    if entry:
        try:
            os.remove(entry["file"])
        except Exception:
            pass
        lib = [e for e in lib if e["name"] != name]
        save_clone_library(lib)
    cb_clone_var.set(_CLONE_DEFAULT)
    _refresh_clone_menu()
    status_label.configure(text=f"Deleted clone: {name}")

def _rename_clone():
    old_name = cb_clone_var.get()
    if old_name == _CLONE_DEFAULT:
        status_label.configure(text="⚠️ Default voice cannot be renamed.")
        return

    win = ctk.CTkToplevel(app)
    win.title("Rename Voice Clone")
    _center_window(win, 340, 130)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    win.transient(app)
    _fade_in(win)

    ctk.CTkLabel(win, text="New name for this voice clone:",
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT).pack(padx=20, pady=(18, 6), anchor="w")

    entry = ctk.CTkEntry(win, font=ctk.CTkFont(family="Segoe UI", size=12),
                         fg_color=C_ELEVATED, border_color=C_BORDER,
                         text_color=C_TXT, height=32, width=300)
    entry.insert(0, old_name)
    entry.select_range(0, "end")
    entry.focus()
    entry.pack(padx=20)

    def _confirm():
        try:
            new_name = entry.get().strip()
            if not new_name:
                return
            if new_name == old_name:
                win.destroy()
                return
            # Deduplicate against existing names
            lib = load_clone_library()
            existing = {e["name"] for e in lib if e["name"] != old_name}
            base, n = new_name, 2
            while new_name in existing:
                new_name = f"{base} ({n})"; n += 1
            if not rename_clone_in_library(old_name, new_name):
                status_label.configure(text=f"⚠️ Could not rename '{old_name}' — entry not found.")
                win.destroy()
                return
            _refresh_clone_menu()
            cb_clone_var.set(new_name)  # set AFTER refresh so menu knows the name
            status_label.configure(text=f"Renamed to: {new_name}")
            win.destroy()
        except Exception as e:
            _log_crash(e)
            status_label.configure(text=f"❌ Rename failed: {_fmt_err(e)}")
            win.destroy()

    ctk.CTkButton(win, text="Rename", command=_confirm,
                  height=30, font=ctk.CTkFont(family="Segoe UI", size=12),
                  fg_color=C_ACCENT_D, hover_color=C_ACCENT,
                  text_color=C_TXT).pack(pady=10)
    win.bind("<Return>", lambda _: _confirm())

def show_voice_recorder():
    SAMPLE_RATE = 44100
    CLONE_SCRIPT = (
        "The warm light of the setting sun spilled across the old stone bridge, "
        "painting the river gold. A gentle breeze carried the scent of pine through "
        "the quiet valley below."
    )

    win = ctk.CTkToplevel(app)
    win.title("Record Voice Clone")
    _center_window(win, 560, 520)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    _fade_in(win)

    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=56)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    ctk.CTkLabel(hdr, text="Record Voice Clone",
                 font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
                 text_color=C_TXT).pack(side="left", padx=20)
    ctk.CTkLabel(hdr, text="Aim for 8–12 seconds",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT3).pack(side="right", padx=20)

    script_card = ctk.CTkFrame(win, fg_color=C_CARD, corner_radius=10)
    script_card.pack(fill="x", padx=20, pady=(16, 8))
    ctk.CTkLabel(script_card, text="Read this aloud",
                 font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                 text_color=C_ACCENT, anchor="w").pack(anchor="w", padx=14, pady=(12, 4))
    ctk.CTkLabel(script_card, text=CLONE_SCRIPT,
                 font=ctk.CTkFont(family="Segoe UI", size=13), wraplength=488,
                 text_color=C_TXT, justify="left", anchor="w").pack(fill="x", padx=14, pady=(0, 14))

    tips_frame = ctk.CTkFrame(win, fg_color="transparent")
    tips_frame.pack(fill="x", padx=20, pady=(0, 6))
    for tip in ("Speak naturally — same tone you want cloned",
                "Quiet room, no background noise",
                "15–30 cm from the microphone"):
        ctk.CTkLabel(tips_frame, text=f"  {tip}",
                     font=ctk.CTkFont(family="Segoe UI", size=10),
                     text_color=C_TXT3, anchor="w").pack(anchor="w", pady=1)

    # Name field
    name_row = ctk.CTkFrame(win, fg_color="transparent")
    name_row.pack(fill="x", padx=20, pady=(4, 6))
    ctk.CTkLabel(name_row, text="Name:",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT2, width=46, anchor="w").pack(side="left")
    name_entry = ctk.CTkEntry(name_row, placeholder_text="e.g. My Voice",
                               font=ctk.CTkFont(family="Segoe UI", size=11),
                               fg_color=C_ELEVATED, border_color=C_BORDER,
                               text_color=C_TXT, height=28)
    name_entry.pack(side="left", fill="x", expand=True)

    ctrl = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=10)
    ctrl.pack(fill="x", padx=20, pady=(0, 8))

    timer_lbl = ctk.CTkLabel(ctrl, text="0.0s", width=52,
                              font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                              text_color=C_TXT3)
    timer_lbl.pack(side="left", padx=(14, 8), pady=12)

    record_btn = ctk.CTkButton(ctrl, text="Record", width=120, height=36,
                                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                                fg_color=C_REC, hover_color="#a52020", corner_radius=8)
    record_btn.pack(side="left", padx=(0, 6), pady=12)

    play_btn = ctk.CTkButton(ctrl, text="Preview", width=100, height=36,
                              font=ctk.CTkFont(family="Segoe UI", size=12),
                              **BTN_GHOST, corner_radius=8, state="disabled")
    play_btn.pack(side="left", padx=(0, 6), pady=12)

    save_btn = ctk.CTkButton(ctrl, text="Save to Library", width=120, height=36,
                              font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                              corner_radius=8, state="disabled")
    save_btn.pack(side="left", pady=12)

    rec_status = ctk.CTkLabel(win, text="Press Record when you're ready.",
                               font=ctk.CTkFont(family="Segoe UI", size=11),
                               text_color=C_TXT3)
    rec_status.pack(pady=(4, 0))

    _recording   = threading.Event()
    _chunks      = []
    _chunks_lock = threading.Lock()
    _samples     = [None]
    _stream      = [None]
    _start       = [0.0]
    _tmp_path    = [None]

    def _tick():
        if _recording.is_set():
            t = time.time() - _start[0]
            timer_lbl.configure(text=f"{t:.1f}s",
                                 text_color=C_REC if t <= 15 else C_WARN)
            win.after(100, _tick)

    def _start_rec():
        with _chunks_lock:
            _chunks.clear()
        _recording.set()
        _start[0] = time.time()
        record_btn.configure(text="Stop")
        play_btn.configure(state="disabled")
        save_btn.configure(state="disabled")
        rec_status.configure(text="Recording...", text_color=C_REC)
        _tick()
        def _cb(indata, frames, time_info, st):
            if _recording.is_set():
                with _chunks_lock:
                    _chunks.append(indata.copy())
        s = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                           dtype="float32", callback=_cb)
        s.start()
        _stream[0] = s

    def _stop_rec():
        _recording.clear()
        if _stream[0]:
            _stream[0].stop(); _stream[0].close(); _stream[0] = None
        timer_lbl.configure(text_color=C_TXT3)
        record_btn.configure(text="Record Again")
        with _chunks_lock:
            chunks_snap = list(_chunks)
        if not chunks_snap:
            rec_status.configure(text="Nothing recorded.", text_color=C_WARN)
            return
        _samples[0] = np.concatenate(chunks_snap).flatten()
        dur = len(_samples[0]) / SAMPLE_RATE
        peak = float(np.abs(_samples[0]).max()) if len(_samples[0]) else 0.0
        if dur < 3:
            rec_status.configure(text=f"Only {dur:.1f}s — try for at least 5s.", text_color=C_WARN)
            play_btn.configure(state="normal")
        elif peak < 0.01:
            rec_status.configure(
                text=f"Recording is too quiet (level: {peak:.4f}) — check your mic gain in Windows Sound settings and re-record.",
                text_color=C_WARN)
            play_btn.configure(state="normal")
        else:
            # Write to a temp file so preview works before library save
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            try:
                sf.write(tmp.name, _samples[0], SAMPLE_RATE)
            except Exception as e:
                _log_crash(e)
                rec_status.configure(text=f"❌ Could not save recording: {_fmt_err(e)}",
                                     text_color=C_WARN)
                play_btn.configure(state="normal")
                return
            _tmp_path[0] = tmp.name
            rec_status.configure(text=f"{dur:.1f}s recorded — name it and save to library.",
                                  text_color=C_SUCCESS)
            play_btn.configure(state="normal")
            save_btn.configure(state="normal")

    def _toggle():
        if _recording.is_set(): _stop_rec()
        else: _start_rec()

    def _preview():
        if _samples[0] is not None:
            def _do():
                try:
                    sd.stop()
                    sd.play(_samples[0], SAMPLE_RATE)
                    sd.wait()
                except Exception as e:
                    app.after(0, lambda m=_fmt_err(e): rec_status.configure(
                        text=f"❌ Playback failed: {m}", text_color=C_WARN))
            threading.Thread(target=_do, daemon=True).start()

    def _save_to_library():
        if _tmp_path[0] is None or not os.path.exists(_tmp_path[0]):
            return
        name = name_entry.get().strip() or "Recorded Voice"
        # Ensure unique name
        lib = load_clone_library()
        existing = {e["name"] for e in lib}
        base, n = name, 2
        while name in existing:
            name = f"{base} ({n})"; n += 1
        add_clone_to_library(name, _tmp_path[0])
        try:
            os.remove(_tmp_path[0])
        except Exception:
            pass
        _tmp_path[0] = None
        _refresh_clone_menu()
        cb_clone_var.set(name)
        status_label.configure(text=f"Saved voice clone: {name}")
        win.destroy()

    def _on_close():
        _recording.clear()
        if _stream[0]:
            _stream[0].stop(); _stream[0].close()
        if _tmp_path[0]:
            try: os.remove(_tmp_path[0])
            except Exception: pass
        sd.stop()
        win.destroy()

    record_btn.configure(command=_toggle)
    play_btn.configure(command=_preview)
    save_btn.configure(command=_save_to_library)
    win.protocol("WM_DELETE_WINDOW", _on_close)

def _browse_and_add_clone():
    fp = filedialog.askopenfilename(
        title="Select voice sample (5–30s WAV/MP3)",
        filetypes=[("Audio files", "*.wav *.mp3")])
    if not fp:
        return
    # Ask for a name via a simple dialog
    name_win = ctk.CTkToplevel(app)
    name_win.title("Name this voice clone")
    _center_window(name_win, 340, 130)
    name_win.resizable(False, False)
    name_win.configure(fg_color=C_BG)
    name_win.grab_set()
    _fade_in(name_win)
    ctk.CTkLabel(name_win, text="Name for this voice clone:",
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT).pack(padx=20, pady=(18, 6), anchor="w")
    entry = ctk.CTkEntry(name_win, font=ctk.CTkFont(family="Segoe UI", size=12),
                         fg_color=C_ELEVATED, border_color=C_BORDER,
                         text_color=C_TXT, height=32, width=300)
    entry.insert(0, os.path.splitext(os.path.basename(fp))[0])
    entry.pack(padx=20)
    def _confirm():
        name = entry.get().strip() or os.path.splitext(os.path.basename(fp))[0]
        lib = load_clone_library()
        existing = {e["name"] for e in lib}
        base, n = name, 2
        while name in existing:
            name = f"{base} ({n})"; n += 1
        add_clone_to_library(name, fp)
        _refresh_clone_menu()
        cb_clone_var.set(name)
        status_label.configure(text=f"Added voice clone: {name}")
        name_win.destroy()
    ctk.CTkButton(name_win, text="Add to Library", command=_confirm,
                  height=30, font=ctk.CTkFont(family="Segoe UI", size=12)).pack(pady=10)
    name_win.bind("<Return>", lambda _: _confirm())

ctk.CTkButton(cb_clone_btns, text="Record New", command=show_voice_recorder,
              width=96, height=28, font=ctk.CTkFont(family="Segoe UI", size=11),
              fg_color=C_ACCENT_D, hover_color=C_ACCENT, text_color=C_TXT).pack(side="left", padx=(0, 5))
ctk.CTkButton(cb_clone_btns, text="Browse", command=_browse_and_add_clone,
              width=76, height=28, font=ctk.CTkFont(family="Segoe UI", size=11),
              **BTN_GHOST).pack(side="left", padx=(0, 5))
ctk.CTkButton(cb_clone_btns, text="Rename", command=_rename_clone,
              width=70, height=28, font=ctk.CTkFont(family="Segoe UI", size=11),
              **BTN_GHOST).pack(side="left", padx=(0, 5))
ctk.CTkButton(cb_clone_btns, text="Delete", command=_delete_clone,
              width=62, height=28, font=ctk.CTkFont(family="Segoe UI", size=11),
              fg_color="transparent", hover_color="#3d1515",
              text_color=C_DANGER, border_width=1, border_color="#3d1515").pack(side="left")

def make_slider(parent, label, from_, to, steps, default, width=214, tooltip=None):
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", padx=14, pady=(4, 2))
    ctk.CTkLabel(row, text=label,
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT2, anchor="w").pack(side="left")
    if tooltip:
        _info_btn(row, tooltip).pack(side="left", padx=(4, 0))
    val = ctk.CTkLabel(row, text="",
                       font=ctk.CTkFont(family="Segoe UI", size=11),
                       text_color=C_ACCENT, width=58, anchor="e")
    val.pack(side="right")
    s = ctk.CTkSlider(parent, from_=from_, to=to, number_of_steps=steps,
                      command=lambda _: update_all_labels(),
                      width=width, height=14)
    s.set(default)
    s.pack(padx=14, pady=(0, 8))
    return s, val

_section_label(cb_frame, "PARAMETERS",
    tooltip="Fine-tune how Natural mode generates audio. "
            "Default values work well for most voices.")
cb_exag_slider, cb_exag_label = make_slider(cb_frame, "Exaggeration", 0.0, 1.0, 20, 0.5,
    tooltip="Controls emotional intensity. Higher = more expressive and dramatic. "
            "Lower = flatter, more neutral. 0.5 is a good starting point.")
cb_cfg_slider,  cb_cfg_label  = make_slider(cb_frame, "CFG Weight",   0.0, 1.0, 20, 0.5,
    tooltip="Classifier-Free Guidance — how closely the output follows the voice clone sample. "
            "Higher values sound more like the clone but can reduce naturalness. "
            "0 uses the model's own judgment.")

# Speed (shared)
_sep(mid_panel)
_section_label(mid_panel, "SPEED",
    tooltip="Adjusts how fast the voice speaks. 1.0 is normal speed. "
            "0.85 sounds natural for most narration. Above 1.3 may sound rushed.")
speed_slider, speed_label = make_slider(mid_panel, "Playback Speed", 0.5, 2.0, 30, 0.85)

# Engine switch logic
def _on_engine_change(*_):
    if engine_var.get() == "Natural":
        kokoro_frame.pack_forget()
        cb_frame.pack(fill="x")
        if not chatterbox_engine.is_ready:
            def _start_load():
                try:
                    update_modal, close_modal = _make_chatterbox_loading_modal()
                except Exception:
                    # Modal failed to create — fall back to silent load with status bar only
                    def update_modal(msg): pass
                    def close_modal(): pass
                threading.Thread(
                    target=_load_chatterbox_bg,
                    args=(update_modal, close_modal),
                    daemon=True,
                ).start()

            def _revert_to_fast():
                engine_var.set("Fast")

            def _after_setup():
                """Called when auto-setup completes successfully — proceed with normal load."""
                free_gb = _get_free_ram_gb()
                if free_gb < 6.0:
                    app.after(0, lambda: _show_low_ram_warning(free_gb, _start_load, _revert_to_fast))
                else:
                    app.after(0, _start_load)

            if not _cb_env_exists():
                # First time — need to install chatterbox_env before loading
                app.after(0, lambda: _show_chatterbox_setup_modal(_after_setup, _revert_to_fast))
            else:
                free_gb = _get_free_ram_gb()
                if free_gb < 6.0:
                    app.after(0, lambda: _show_low_ram_warning(free_gb, _start_load, _revert_to_fast))
                else:
                    _start_load()
    else:
        cb_frame.pack_forget()
        kokoro_frame.pack(fill="x")
        if chatterbox_engine.is_ready:
            status_label.configure(text="Fast mode active — Natural mode unloaded.")
            threading.Thread(target=chatterbox_engine.stop, daemon=True).start()

engine_var.trace_add("write", _on_engine_change)

# Generate / Stop buttons are in the header bar

# ── Enhancement panel ─────────────────────────────────────────────────────────
# Outer card sits in the grid; inner scrollable frame holds all controls so
# the tall FX + AI Enhancement content is accessible at any window height.
_enh_card = ctk.CTkFrame(studio, fg_color=C_CARD, corner_radius=12)
_enh_card.grid(row=0, column=2, sticky="nsew", padx=(0, 8), pady=6)
enh_panel = ctk.CTkScrollableFrame(
    _enh_card, fg_color="transparent",
    scrollbar_button_color=C_BORDER,
    scrollbar_button_hover_color=C_ACCENT_D)
enh_panel.pack(fill="both", expand=True)

# ── AI Enhancement (above FX so it's visible without scrolling) ───────────────
_section_label(enh_panel, "AI ENHANCEMENT",
    tooltip="Uses Resemble AI's enhancement model to improve audio quality — removes noise, "
            "adds presence, and makes TTS sound more natural. "
            "First use downloads ~450 MB of model weights. "
            "Async mode runs in the background after generation.")

enhance_var  = ctk.BooleanVar(value=False)
enhance_mode = ctk.StringVar(value="Async")

_enhance_row = ctk.CTkFrame(enh_panel, fg_color="transparent")
_enhance_row.pack(fill="x", padx=14, pady=(0, 4))
_enhance_cb = ctk.CTkCheckBox(_enhance_row, text="Resemble Enhance", variable=enhance_var,
                              font=ctk.CTkFont(family="Segoe UI", size=11),
                              text_color=C_TXT2, checkmark_color=C_ACCENT,
                              fg_color=C_ACCENT_D, hover_color=C_ACCENT_D,
                              border_color=C_BORDER)
_enhance_cb.pack(side="left")

_enhance_mode_btn = ctk.CTkSegmentedButton(
    enh_panel,
    values=["CPU", "GPU", "Async"],
    variable=enhance_mode,
    font=ctk.CTkFont(family="Segoe UI", size=11),
    width=214,
)
_enhance_mode_btn.pack(padx=14, pady=(0, 4))

# GPU hint (shown if CUDA not available)
try:
    import torch as _torch_check
    if not _torch_check.cuda.is_available():
        ctk.CTkLabel(enh_panel,
                     text="GPU: no CUDA detected — use CPU or Async",
                     font=ctk.CTkFont(family="Segoe UI", size=9),
                     text_color=C_TXT3).pack(padx=14, anchor="w", pady=(0, 2))
    del _torch_check
except ImportError:
    pass

# ── Audio FX ──────────────────────────────────────────────────────────────────
_sep(enh_panel)
_section_label(enh_panel, "AUDIO FX",
    tooltip="Post-processing chain applied to every generated clip. "
            "Runs in order: Trim → High-pass → Low-pass → Noise Gate → Compressor → Reverb → Gain. "
            "All sliders save automatically with your profile.")

eq_preset_var = ctk.StringVar(value="Custom")
eq_preset_menu = ctk.CTkOptionMenu(
    enh_panel, variable=eq_preset_var,
    values=list(EQ_PRESETS.keys()),
    font=ctk.CTkFont(family="Segoe UI", size=11),
    width=214, dynamic_resizing=False)
eq_preset_menu.pack(padx=14, pady=(0, 8))
eq_preset_var.trace_add("write", lambda *_: apply_eq_preset())

def _checkbox_row(parent, text, var, tooltip):
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(anchor="w", fill="x", pady=5)
    ctk.CTkCheckBox(row, text=text, variable=var,
                    font=ctk.CTkFont(family="Segoe UI", size=12)).pack(side="left")
    _info_btn(row, tooltip).pack(side="left", padx=(6, 0))

checks = ctk.CTkFrame(enh_panel, fg_color="transparent")
checks.pack(fill="x", padx=14, pady=(0, 8))
noise_gate_var = ctk.BooleanVar(value=False)
_checkbox_row(checks, "Noise Gate", noise_gate_var,
    "Silences audio that falls below -40 dB. Removes breath noise and soft hiss "
    "between words without affecting speech.")
trim_var = ctk.BooleanVar(value=True)
_checkbox_row(checks, "Trim Silence", trim_var,
    "Cuts leading and trailing silence from the clip. Keeps a short tail so the "
    "audio doesn't feel clipped.")
compressor_var = ctk.BooleanVar(value=True)
_checkbox_row(checks, "Compressor", compressor_var,
    "Evens out volume differences between loud and quiet parts. "
    "Makes speech sound more consistent and professional.")

compressor_slider, comp_label   = make_slider(enh_panel, "Comp Ratio", 1.0, 8.0,  14, 2.0,
    tooltip="How aggressively to compress dynamics. 2:1 is gentle, 6:1+ is heavily compressed. "
            "2–4 is ideal for most TTS.")
highpass_slider,   hp_label     = make_slider(enh_panel, "High Pass",   20, 500,  48,  20,
    tooltip="Cuts frequencies below this value (Hz). Removes low-end rumble and mic noise. "
            "20 Hz = off. Try 80–120 Hz for cleaner speech.")
lowpass_slider,    lp_label     = make_slider(enh_panel, "Low Pass",  4000, 18000, 56, 18000,
    tooltip="Cuts frequencies above this value (Hz). Softens harsh highs. "
            "18000 Hz = off. Try 12000–15000 Hz for a warmer sound.")
reverb_slider,     reverb_label = make_slider(enh_panel, "Reverb",    0.0,  1.0,  20, 0.0,
    tooltip="Adds a room/space effect. 0 = dry (no reverb). Keep below 0.2 for narration — "
            "high values make speech hard to understand.")
gain_slider,       gain_label   = make_slider(enh_panel, "Gain",       -12,   12,  24,   0,
    tooltip="Boosts or reduces the overall volume in dB. "
            "0 = unchanged. Use +3 to +6 dB if the audio feels too quiet.")

def _on_eq_manual_change(_=None):
    if not _applying_eq_preset:
        eq_preset_var.set("Custom")
    update_all_labels()

for _eq_s in (compressor_slider, highpass_slider, lowpass_slider, reverb_slider, gain_slider):
    _eq_s.configure(command=_on_eq_manual_change)

noise_gate_var.trace_add("write",  lambda *_: eq_preset_var.set("Custom") if not _applying_eq_preset else None)
trim_var.trace_add("write",        lambda *_: eq_preset_var.set("Custom") if not _applying_eq_preset else None)
compressor_var.trace_add("write",  lambda *_: eq_preset_var.set("Custom") if not _applying_eq_preset else None)

ctk.CTkButton(enh_panel, text="Reset to defaults", command=reset_enhancements,
              width=214, height=30,
              font=ctk.CTkFont(family="Segoe UI", size=11),
              **BTN_GHOST).pack(padx=14, pady=(0, 8))


def _resemble_deps_without_deepspeed():
    """Return resemble-enhance's declared deps with deepspeed and training extras removed.

    Uses importlib.metadata (stdlib, no text parsing) so it works regardless
    of pip show line-wrapping or format changes across pip versions.
    Returns [] if the package isn't installed yet.
    """
    import re
    try:
        from importlib.metadata import requires as _meta_requires, PackageNotFoundError
        raw = _meta_requires("resemble-enhance") or []
    except Exception:
        return []

    result = []
    for req in raw:
        # Skip training-only / optional extras: 'pkg; extra == "train"'
        if "extra ==" in req or 'extra==' in req:
            continue
        # Extract bare package name (everything before whitespace or version specifier)
        name = re.split(r"[\s;><=!]", req.strip())[0]
        if not name or name.lower().startswith("deepspeed"):
            continue
        result.append(name)
    return result


def _install_resemble_enhance():
    """Background thread: install resemble-enhance into python_embed.

    Strategy
    --------
    1. pip install numpy<2 (resemble-enhance 0.0.1 incompatible with numpy 2.x)
    2. pip install resemble-enhance --no-deps  (avoids deepspeed build failure)
    3. Iteratively: try to import in python_embed subprocess, catch the missing
       module name, pip install just that package, repeat.
    4. Once the subprocess import succeeds, declare done.

    All imports run in python_embed (subprocess) — never in the frozen app.
    """
    _SKIP_PKGS = {"deepspeed", "gradio", "celluloid", "ptflops"}

    app.after(0, lambda: _enhance_cb.configure(state="disabled"))
    app.after(0, lambda: _enhance_mode_btn.configure(state="disabled"))

    _py = enhance_engine.PYTHON

    def _pip(*args):
        return subprocess.run(
            [_py, "-m", "pip", "install", *args],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def _pip_error(r):
        lines = (r.stderr or r.stdout or "pip failed").strip().splitlines()
        return lines[-1] if lines else "pip failed"

    def _try_import():
        """Try importing resemble-enhance via enhance_worker.py --check.

        Uses the real worker's deepspeed stub + PosixPath fix — no
        duplicated logic, tests the exact code path that runs at runtime.
        Returns (ok, error_msg).
        """
        r = subprocess.run(
            [_py, enhance_engine.WORKER, "--check"],
            capture_output=True, text=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "").strip()

    try:
        # ── Step 1: numpy pin + resemble-enhance ─────────────────────────────
        app.after(0, lambda: status_label.configure(
            text="📦 Installing Resemble Enhance..."))
        r = _pip("numpy<2", "--upgrade")
        if r.returncode != 0:
            raise RuntimeError(f"Could not pin numpy: {_pip_error(r)}")
        r = _pip("resemble-enhance", "--no-deps")
        if r.returncode != 0:
            raise RuntimeError(f"Could not install resemble-enhance: {_pip_error(r)}")

        # ── Step 2: iteratively resolve missing deps via subprocess ───────────
        skipped = []
        for attempt in range(20):
            ok, err = _try_import()
            if ok:
                break

            # Extract missing module: "No module named 'pandas'" → "pandas"
            missing = ""
            for line in err.splitlines():
                if "No module named" in line:
                    missing = line.split("No module named")[-1].strip(" '\"")
                    break
            if not missing:
                # ModuleNotFoundError isn't the only possible failure —
                # surface the last line of stderr to the user.
                last_line = err.splitlines()[-1] if err else "unknown error"
                raise RuntimeError(f"Import check failed: {last_line}")

            pkg = missing.split(".")[0].strip()
            if not pkg:
                raise RuntimeError(f"Unrecognised ImportError: {missing}")

            if pkg.lower() in _SKIP_PKGS:
                skipped.append(pkg)
                # enhance_worker.py --check stubs these automatically
                continue

            app.after(0, lambda p=pkg: status_label.configure(
                text=f"📦 Installing missing dep: {p}..."))
            r = _pip(pkg)
            if r.returncode != 0:
                raise RuntimeError(f"Failed to install '{pkg}': {_pip_error(r)}")
        else:
            raise RuntimeError("Dependency resolution did not converge after 20 attempts.")

        note = f" (skipped: {', '.join(skipped)})" if skipped else ""
        app.after(0, lambda n=note: status_label.configure(
            text=f"✅ Resemble Enhance ready{n}. First use downloads ~450 MB of model weights."))
        app.after(0, lambda: enhance_var.set(True))

    except Exception as e:
        app.after(0, lambda m=str(e): status_label.configure(
            text=f"❌ Install failed: {m}"))
        app.after(0, lambda: enhance_var.set(False))
    finally:
        app.after(0, lambda: _enhance_cb.configure(state="normal"))
        app.after(0, lambda: _enhance_mode_btn.configure(state="normal"))


def _enhance_deps_installed():
    """Check if resemble-enhance is importable in python_embed.

    Runs enhance_worker.py --check, which uses the same deepspeed stub
    and PosixPath fix as the real worker — no duplicated logic.
    """
    _py = enhance_engine.PYTHON
    if not os.path.exists(_py):
        return False
    try:
        r = subprocess.run(
            [_py, enhance_engine.WORKER, "--check"],
            capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def _on_enhance_toggle(*_):
    if not enhance_var.get():
        return
    if not os.path.exists(enhance_engine.PYTHON):
        enhance_var.set(False)
        app.after(0, lambda: status_label.configure(
            text="⚠️ Natural mode must be set up before Enhancement — switch to Natural mode first."))
        return
    # Check in a thread to avoid blocking UI (subprocess call)
    def _check():
        if _enhance_deps_installed():
            return  # deps ready, checkbox stays on
        app.after(0, lambda: enhance_var.set(False))
        app.after(0, lambda: status_label.configure(
            text="📦 Resemble Enhance not installed — auto-installing..."))
        _install_resemble_enhance()
    threading.Thread(target=_check, daemon=True).start()


enhance_var.trace_add("write", _on_enhance_toggle)

# Restore persisted FX settings (all FX vars are now defined)
app.after(50, _restore_fx_settings)

# ── Audio History panel ───────────────────────────────────────────────────────
hist_panel = _panel(studio, 3, padright=0)

hist_header = ctk.CTkFrame(hist_panel, fg_color="transparent")
hist_header.pack(fill="x", padx=14, pady=(10, 4))
_hist_left = ctk.CTkFrame(hist_header, fg_color="transparent")
_hist_left.pack(side="left")
ctk.CTkFrame(_hist_left, fg_color=C_ACCENT, width=3, height=10,
             corner_radius=2).pack(side="left", padx=(0, 6))
ctk.CTkLabel(_hist_left, text="HISTORY",
             font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
             text_color=C_TXT2).pack(side="left")
ctk.CTkLabel(hist_header, text=f"last {MAX_HISTORY}",
             font=ctk.CTkFont(family="Segoe UI", size=11),
             text_color=C_TXT3).pack(side="right")

_sep(hist_panel)

history_scroll = ctk.CTkScrollableFrame(
    hist_panel, fg_color=C_CARD,
    scrollbar_button_color=C_BORDER,
    scrollbar_button_hover_color=C_ACCENT_D)
history_scroll.pack(fill="both", expand=True, padx=8, pady=(6, 8))
history_inner = history_scroll

ctk.CTkLabel(history_inner, text="No audio yet.",
             text_color=C_TXT3, font=ctk.CTkFont(family="Segoe UI", size=12)).pack(pady=30)

# ══════════════════════════════════════════════════════════════════════════════
# QUEUE TAB
# ══════════════════════════════════════════════════════════════════════════════
queue_tab = tabs.tab("  Queue  ")
queue_tab.configure(fg_color=C_BG)

q_card = ctk.CTkFrame(queue_tab, fg_color=C_CARD, corner_radius=12)
q_card.pack(fill="both", expand=True, padx=6, pady=6)

q_top = ctk.CTkFrame(q_card, fg_color="transparent")
q_top.pack(fill="x", padx=14, pady=(12, 6))
_q_left = ctk.CTkFrame(q_top, fg_color="transparent")
_q_left.pack(side="left")
ctk.CTkFrame(_q_left, fg_color=C_ACCENT, width=3, height=10,
             corner_radius=2).pack(side="left", padx=(0, 6))
ctk.CTkLabel(_q_left, text="BATCH QUEUE",
             font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
             text_color=C_TXT2).pack(side="left")
queue_estimate_label = ctk.CTkLabel(q_top, text="Queue is empty",
                                     font=ctk.CTkFont(family="Segoe UI", size=11),
                                     text_color=C_TXT2)
queue_estimate_label.pack(side="right")

_sep(q_card)

queue_listbox = tk.Listbox(
    q_card,
    font=("Consolas", 11),
    bg=C_ELEVATED, fg=C_TXT,
    selectbackground=C_ACCENT_D, selectforeground=C_TXT,
    borderwidth=0, highlightthickness=0,
    activestyle="none",
    relief="flat")
queue_listbox.pack(fill="both", expand=True, padx=12, pady=(8, 4))

_sep(q_card)

# ── Format selector ───────────────────────────────────────────────────────────
q_fmt_row = ctk.CTkFrame(q_card, fg_color="transparent")
q_fmt_row.pack(fill="x", padx=14, pady=(8, 2))
ctk.CTkLabel(q_fmt_row, text="Output format:",
             font=ctk.CTkFont(family="Segoe UI", size=11),
             text_color=C_TXT2).pack(side="left", padx=(0, 8))
queue_fmt_var = ctk.StringVar(value="WAV")
_q_fmt_seg = ctk.CTkSegmentedButton(q_fmt_row, values=["WAV", "MP3"],
                                     variable=queue_fmt_var,
                                     font=ctk.CTkFont(family="Segoe UI", size=11),
                                     width=120)
_q_fmt_seg.pack(side="left", padx=(0, 16))
ctk.CTkLabel(q_fmt_row, text="Quality:",
             font=ctk.CTkFont(family="Segoe UI", size=11),
             text_color=C_TXT2).pack(side="left", padx=(0, 8))
queue_mp3_quality_var = ctk.StringVar(value="192 kbps")
_q_quality_seg = ctk.CTkSegmentedButton(q_fmt_row, values=["128 kbps", "192 kbps", "320 kbps"],
                                         variable=queue_mp3_quality_var,
                                         font=ctk.CTkFont(family="Segoe UI", size=11),
                                         width=240)
_q_quality_seg.pack(side="left")

def _q_fmt_toggle(val):
    _q_quality_seg.configure(state="normal" if val == "MP3" else "disabled")
_q_fmt_seg.configure(command=_q_fmt_toggle)
_q_fmt_toggle("WAV")  # start disabled

q_btns = ctk.CTkFrame(q_card, fg_color="transparent")
q_btns.pack(fill="x", padx=12, pady=(6, 10))
queue_gen_btn = ctk.CTkButton(
    q_btns, text="Generate All & Save",
    command=queue_generate_all, width=200, height=36,
    font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"))
queue_gen_btn.pack(side="left", padx=(0, 8))
ctk.CTkButton(q_btns, text="Remove Selected", command=queue_remove,
              width=140, height=36,
              font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST).pack(side="left", padx=(0, 6))
ctk.CTkButton(q_btns, text="Clear All", command=queue_clear,
              width=90, height=36,
              font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_DARK).pack(side="left")

# ══════════════════════════════════════════════════════════════════════════════
# PROFILES TAB
# ══════════════════════════════════════════════════════════════════════════════
prof_tab = tabs.tab("  Profiles  ")
prof_tab.configure(fg_color=C_BG)

p_outer = ctk.CTkFrame(prof_tab, fg_color="transparent")
p_outer.pack(fill="both", expand=True, padx=6, pady=6)

# Load card
load_card = ctk.CTkFrame(p_outer, fg_color=C_CARD, corner_radius=12)
load_card.pack(fill="x", pady=(0, 10))

_section_label(load_card, "LOAD PROFILE")
_sep(load_card)

load_row = ctk.CTkFrame(load_card, fg_color="transparent")
load_row.pack(fill="x", padx=14, pady=14)
profile_var = ctk.StringVar(value="🌙 Calm Narrator")
profile_menu = ctk.CTkOptionMenu(
    load_row, variable=profile_var,
    values=list(load_profiles().keys()), width=260,
    font=ctk.CTkFont(family="Segoe UI", size=12),
    dynamic_resizing=False)
profile_menu.pack(side="left", padx=(0, 8))
ctk.CTkButton(load_row, text="Load", command=load_profile,
              width=80, height=34,
              font=ctk.CTkFont(family="Segoe UI", size=12)).pack(side="left", padx=(0, 6))
ctk.CTkButton(load_row, text="Delete", command=delete_profile,
              width=80, height=34,
              font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_DANGER).pack(side="left")

# Save card
save_card = ctk.CTkFrame(p_outer, fg_color=C_CARD, corner_radius=12)
save_card.pack(fill="x", pady=(0, 10))

_section_label(save_card, "SAVE CURRENT SETTINGS")
_sep(save_card)

save_row = ctk.CTkFrame(save_card, fg_color="transparent")
save_row.pack(fill="x", padx=14, pady=14)
profile_name_entry = ctk.CTkEntry(
    save_row, width=260,
    placeholder_text="Profile name...",
    font=ctk.CTkFont(family="Segoe UI", size=12))
profile_name_entry.pack(side="left", padx=(0, 8))
profile_name_entry.bind("<Return>", lambda e: save_profile())
ctk.CTkButton(save_row, text="Save", command=save_profile,
              width=80, height=34,
              font=ctk.CTkFont(family="Segoe UI", size=12)).pack(side="left")

# Tips card
tip_card = ctk.CTkFrame(p_outer, fg_color=C_CARD, corner_radius=12)
tip_card.pack(fill="x")
ctk.CTkLabel(tip_card,
             text="The Calm Narrator preset is optimized for science/documentary narration.\n"
                  "George (Best) at 0.85× speed with subtle compression produces the cleanest results.",
             font=ctk.CTkFont(family="Segoe UI", size=11), text_color=C_TXT2,
             justify="left", anchor="w").pack(anchor="w", padx=14, pady=14)

# ══════════════════════════════════════════════════════════════════════════════
# DIALOGUE TAB
# ══════════════════════════════════════════════════════════════════════════════
dlg_tab = tabs.tab("  Dialogue  ")
dlg_tab.configure(fg_color=C_BG)
dlg_tab.grid_columnconfigure(0, weight=3)
dlg_tab.grid_columnconfigure(1, minsize=290)
dlg_tab.grid_rowconfigure(0, weight=1)

# ── Script panel ──────────────────────────────────────────────────────────────
dlg_script_panel = ctk.CTkFrame(dlg_tab, fg_color=C_CARD, corner_radius=12)
dlg_script_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=6)

_section_label(dlg_script_panel, "DIALOGUE SCRIPT", padx=14, pady=(12, 2))
ctk.CTkLabel(dlg_script_panel,
             text="One cue per line  ·  SPEAKER (all caps) followed by a colon, then the spoken text",
             font=ctk.CTkFont(family="Segoe UI", size=10),
             text_color=C_TXT3, anchor="w").pack(anchor="w", padx=14, pady=(0, 8))

dlg_text = ctk.CTkTextbox(
    dlg_script_panel,
    font=ctk.CTkFont(family="Consolas", size=12),
    wrap="word",
    fg_color=C_ELEVATED, border_width=0, corner_radius=8,
    text_color=C_TXT)
dlg_text.pack(fill="both", expand=True, padx=14, pady=(0, 6))
dlg_text.insert("1.0",
    "NARRATOR: In the beginning, there was silence.\n"
    "ALICE: But silence never lasts forever.\n"
    "NARRATOR: And so the story began.")

_sep(dlg_script_panel, pady=0)
dlg_txt_btns = ctk.CTkFrame(dlg_script_panel, fg_color="transparent")
dlg_txt_btns.pack(fill="x", padx=10, pady=8)

def dlg_import_file():
    folder = get_default_folder()
    fp = filedialog.askopenfilename(
        initialdir=folder or None,
        filetypes=[("Text files", "*.txt")]
    )
    if fp:
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                with open(fp, "r", encoding=enc) as f:
                    content = f.read()
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            status_label.configure(text="❌ Could not decode file — unknown encoding.")
            return
        dlg_text.delete("1.0", "end")
        dlg_text.insert("1.0", content)
        status_label.configure(text=f"✅ Imported {len(content):,} characters.")

ctk.CTkButton(dlg_txt_btns, text="Import", command=dlg_import_file,
              width=76, height=30, font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST).pack(side="left", padx=(0, 5))
ctk.CTkButton(dlg_txt_btns, text="Clear",
              command=lambda: (dlg_text.delete("1.0", "end")),
              width=60, height=30, font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_DARK).pack(side="left")

# ── Speakers panel ────────────────────────────────────────────────────────────
dlg_right = ctk.CTkFrame(dlg_tab, fg_color=C_CARD, corner_radius=12)
dlg_right.grid(row=0, column=1, sticky="nsew", padx=0, pady=6)

_section_label(dlg_right, "SPEAKERS")

dlg_detect_btn = ctk.CTkButton(dlg_right, text="Detect Speakers",
              width=256, height=30,
              font=ctk.CTkFont(family="Segoe UI", size=12),
              **BTN_GHOST)
dlg_detect_btn.pack(padx=16, pady=(0, 4))

dlg_stats_label = ctk.CTkLabel(dlg_right, text="",
    font=ctk.CTkFont(family="Segoe UI", size=10),
    text_color=C_TXT3)
dlg_stats_label.pack(pady=(0, 6))

_sep(dlg_right)

dlg_speakers_scroll = ctk.CTkScrollableFrame(
    dlg_right, fg_color="transparent",
    scrollbar_button_color=C_BORDER,
    scrollbar_button_hover_color=C_ACCENT_D)
dlg_speakers_scroll.pack(fill="both", expand=True, padx=6, pady=6)

_sep(dlg_right)

dlg_gen_frame = ctk.CTkFrame(dlg_right, fg_color="transparent")
dlg_gen_frame.pack(fill="x", padx=16, pady=10)

dlg_gen_btn = ctk.CTkButton(
    dlg_gen_frame, text="Generate Dialogue",
    width=256, height=46,
    font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
    corner_radius=10)
dlg_gen_btn.pack(pady=(0, 4))

dlg_cancel_btn = ctk.CTkButton(
    dlg_gen_frame, text="Cancel",
    width=256, height=30,
    font=ctk.CTkFont(family="Segoe UI", size=12),
    **BTN_DARK)
# shown only during generation (packed dynamically)

ctk.CTkLabel(dlg_right,
             text="Fast mode (Kokoro) · output appears in Studio history",
             font=ctk.CTkFont(family="Segoe UI", size=10),
             text_color=C_TXT3).pack(pady=(0, 10))

# ── Dialogue logic ────────────────────────────────────────────────────────────
dlg_speaker_vars = {}   # speaker_name → StringVar (voice display name)
_dlg_cancel_event = threading.Event()

def dlg_detect_speakers():
    text     = dlg_text.get("1.0", "end").strip()
    d_lines  = parse_dialogue(text)
    speakers = list(dict.fromkeys(sp for sp, _ in d_lines))  # ordered unique

    for w in dlg_speakers_scroll.winfo_children():
        w.destroy()
    dlg_speaker_vars.clear()

    if not speakers:
        ctk.CTkLabel(dlg_speakers_scroll,
                     text="No speakers found.\nFormat: SPEAKER: text",
                     text_color=C_TXT3,
                     font=ctk.CTkFont(family="Segoe UI", size=11),
                     justify="center").pack(pady=24)
        dlg_stats_label.configure(text="")
        return

    dlg_stats_label.configure(text=f"{len(d_lines)} line(s) · {len(speakers)} speaker(s)")

    voice_list   = list(VOICES.keys())
    # Smart defaults: alternate female/male pools so 2-speaker scripts sound distinct
    _female_keys = [v for v in voice_list if "Female" in v]
    _male_keys   = [v for v in voice_list if "Male" in v]
    def _default_voice(idx):
        if idx % 2 == 0:
            return _female_keys[idx // 2 % len(_female_keys)]
        else:
            return _male_keys[idx // 2 % len(_male_keys)]

    for i, speaker in enumerate(speakers):
        row = ctk.CTkFrame(dlg_speakers_scroll, fg_color=C_ELEVATED, corner_radius=8)
        row.pack(fill="x", padx=2, pady=(0, 6))

        # Editable speaker name
        name_var = ctk.StringVar(value=speaker)
        name_entry = ctk.CTkEntry(row, textvariable=name_var, width=90,
                                  font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                                  fg_color=C_BG, border_width=1, border_color=C_BORDER,
                                  text_color=C_TXT)
        name_entry.pack(side="left", padx=(10, 4), pady=8)

        # Voice dropdown — use explicit command to guarantee the selection is stored
        # (CTkOptionMenu variable binding is unreliable without a command callback)
        default_voice = _default_voice(i)
        dlg_speaker_vars[speaker] = default_voice  # store display-name string, not StringVar

        def _make_voice_cmd(sp):
            def _on_select(choice):
                dlg_speaker_vars[sp] = choice
            return _on_select

        menu = ctk.CTkOptionMenu(row, values=voice_list,
                                 command=_make_voice_cmd(speaker),
                                 width=140, dynamic_resizing=False,
                                 font=ctk.CTkFont(family="Segoe UI", size=11))
        menu.set(default_voice)
        menu.pack(side="left", padx=(0, 4), pady=8)

        # Rename-in-script button
        def _make_rename(old, nv):
            def _do():
                new = nv.get().strip().upper()
                if not new or new == old:
                    return
                content = dlg_text.get("1.0", "end")
                updated = re.sub(rf'^{re.escape(old)}\s*:', f'{new}:', content, flags=re.MULTILINE)
                dlg_text.delete("1.0", "end")
                dlg_text.insert("1.0", updated.rstrip('\n'))
                dlg_detect_speakers()
            return _do

        ctk.CTkButton(row, text="Reset", width=52, height=28,
                      font=ctk.CTkFont(family="Segoe UI", size=12),
                      command=_make_rename(speaker, name_var),
                      **BTN_GHOST).pack(side="left", padx=(0, 6), pady=8)

    status_label.configure(text=f"✅ {len(speakers)} detected — speaker 1 female, speaker 2 male by default.")

def dlg_generate():
    text    = dlg_text.get("1.0", "end").strip()
    if not text:
        status_label.configure(text="⚠️ Script is empty. Write some dialogue first.")
        return

    d_lines = parse_dialogue(text)
    if not d_lines:
        status_label.configure(text="⚠️ No dialogue detected. Format: SPEAKER: text  (SPEAKER must be ALL CAPS)")
        return

    # Auto-detect if speaker panel is empty
    if not dlg_speaker_vars:
        dlg_detect_speakers()

    # Warn if script has speakers that aren't in the panel
    script_speakers = set(sp for sp, _ in d_lines)
    panel_speakers  = set(dlg_speaker_vars.keys())
    missing = script_speakers - panel_speakers
    if missing:
        status_label.configure(text=f"⚠️ Speakers not in panel: {', '.join(sorted(missing))}. Re-detect speakers.")
        return

    speaker_voices = dict(dlg_speaker_vars)  # sp → voice display name (plain string)
    speed          = round(speed_slider.get(), 2)
    est            = estimate_processing_time(" ".join(t for _, t in d_lines))

    _dlg_cancel_event.clear()
    dlg_gen_btn.configure(state="disabled", text="Generating...")
    dlg_detect_btn.configure(state="disabled")
    play_button.configure(state="disabled")
    queue_gen_btn.configure(state="disabled")
    dlg_cancel_btn.configure(command=lambda: _dlg_cancel_event.set())
    dlg_cancel_btn.pack(pady=(0, 4))
    smooth.start(est)

    def run():
        global is_generating
        is_generating = True
        try:
            samples, sr, segments, failed = generate_dialogue_audio(
                d_lines, speaker_voices, speed,
                status_cb=lambda m: app.after(0, lambda m=m: status_label.configure(text=m)),
                cancel_event=_dlg_cancel_event,
            )
            smooth.finish()
            n_lines = len(d_lines)
            label   = f"Dialogue ({n_lines} lines): " + " · ".join(dlg_speaker_vars.keys())
            add_to_history(samples, sr, text, label, segments=segments)
            if failed:
                skipped = ", ".join(f"#{ln}" for ln, _, _ in failed)
                app.after(0, lambda: status_label.configure(
                    text=f"⚠️ Done with {len(failed)} skipped line(s): {skipped}. Check Studio history."))
            else:
                app.after(0, lambda: status_label.configure(
                    text="✅ Dialogue ready! View in Studio → History panel."))
        except GenerationCancelled:
            smooth.finish()
            app.after(0, lambda: status_label.configure(text="🚫 Dialogue generation cancelled."))
        except Exception as e:
            _log_crash(e)
            _msg = _fmt_err(e)
            app.after(0, lambda m=_msg: status_label.configure(text=f"❌ {m}"))
            smooth.finish()
        finally:
            is_generating = False
            app.after(0, lambda: dlg_gen_btn.configure(state="normal", text="Generate Dialogue"))
            app.after(0, lambda: dlg_detect_btn.configure(state="normal"))
            app.after(0, lambda: dlg_cancel_btn.pack_forget())
            app.after(0, lambda: play_button.configure(state="normal", text="Generate"))
            app.after(0, lambda: queue_gen_btn.configure(state="normal"))

    threading.Thread(target=run, daemon=True).start()

# Wire up buttons now that the functions exist
dlg_detect_btn.configure(command=dlg_detect_speakers)
dlg_gen_btn.configure(command=dlg_generate)

# ── Init ──────────────────────────────────────────────────────────────────────
_startup_settings = _get_settings()
ctk.set_appearance_mode(_startup_settings.get("theme", "dark"))

# Apply saved default profile (voice, speed, enhancements)
_default_profile = _startup_settings.get("default_profile", "")
if _default_profile and _default_profile in load_profiles():
    apply_settings(load_profiles()[_default_profile])

update_all_labels()

# Preload last-used voice clone
_saved_clone = _startup_settings.get("selected_clone", _CLONE_DEFAULT)
if _saved_clone and _saved_clone != _CLONE_DEFAULT:
    _lib = load_clone_library()
    _match = next((e for e in _lib if e["name"] == _saved_clone), None)
    if _match and os.path.exists(_match["file"]):
        cb_clone_var.set(_saved_clone)
        status_label.configure(text="Ready  ·  Ctrl+Enter to generate  ·  Ctrl+P to play  ·  Esc to stop")
    # if not found (deleted), dropdown stays at Default

def _show_chatterbox_setup_modal(on_complete, on_cancel):
    """
    Two-phase setup modal:
      Phase 1 — confirm: shows what will be downloaded, Set Up / Cancel buttons.
      Phase 2 — progress: downloads and installs in background; dismissible only on failure.
    Calls on_complete() on success, on_cancel() if user cancels or dismisses after failure.
    """
    win = ctk.CTkToplevel(app)
    win.title("Natural Mode Setup")
    _center_window(win, 500, 400)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.transient(app)
    win.protocol("WM_DELETE_WINDOW", lambda: None)
    win.attributes("-topmost", True)
    win.lift()

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=54)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    hdr_inner = ctk.CTkFrame(hdr, fg_color="transparent")
    hdr_inner.pack(side="left", padx=16, pady=10)
    ctk.CTkFrame(hdr_inner, fg_color=C_ACCENT, width=4, height=20,
                 corner_radius=2).pack(side="left", padx=(0, 10))
    ctk.CTkLabel(hdr_inner, text="Natural Mode Setup",
                 font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                 text_color=C_TXT).pack(side="left")
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    # ── Phase 1: confirmation body ────────────────────────────────────────────
    confirm_frame = ctk.CTkFrame(win, fg_color="transparent")
    confirm_frame.pack(fill="x", padx=20, pady=(16, 0))

    ctk.CTkLabel(confirm_frame,
                 text="Natural mode uses a separate AI engine that needs to be\n"
                      "downloaded once. The app will handle everything automatically.",
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT2, justify="left", anchor="w",
                 ).pack(anchor="w", pady=(0, 14))

    ctk.CTkLabel(confirm_frame, text="WHAT WILL BE DOWNLOADED",
                 font=ctk.CTkFont(family="Segoe UI", size=9, weight="bold"),
                 text_color=C_TXT3, anchor="w").pack(anchor="w", pady=(0, 6))

    items_frame = ctk.CTkFrame(confirm_frame, fg_color=C_ELEVATED, corner_radius=8)
    items_frame.pack(fill="x", pady=(0, 4))
    for label, size in (
        ("Python 3.11  (runtime environment)", "~15 MB"),
        ("PyTorch CPU  (AI inference library)", "~800 MB"),
        ("Chatterbox TTS  (voice model)",       "~200 MB"),
    ):
        row = ctk.CTkFrame(items_frame, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(row, text=label,
                     font=ctk.CTkFont(family="Segoe UI", size=11),
                     text_color=C_TXT2, anchor="w").pack(side="left")
        ctk.CTkLabel(row, text=size,
                     font=ctk.CTkFont(family="Segoe UI", size=11),
                     text_color=C_TXT3, anchor="e").pack(side="right")

    ctk.CTkLabel(confirm_frame,
                 text="The 3 GB voice model downloads on your first generation.",
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color=C_TXT3, anchor="w").pack(anchor="w", pady=(8, 0))

    # ── Phase 1: buttons ──────────────────────────────────────────────────────
    btn_frame = ctk.CTkFrame(win, fg_color="transparent")
    btn_frame.pack(fill="x", padx=20, pady=16)

    # ── Phase 2: progress elements (hidden until setup starts) ────────────────
    bar = ctk.CTkProgressBar(win, mode="indeterminate",
                             progress_color=C_ACCENT, fg_color=C_ACCENT_D)
    status_var = ctk.StringVar(value="")
    status_lbl = ctk.CTkLabel(win, textvariable=status_var,
                              font=ctk.CTkFont(family="Segoe UI", size=11),
                              text_color=C_TXT2, wraplength=460, anchor="w", justify="left")
    cancel_btn2 = ctk.CTkButton(win, text="Cancel — Use Fast Mode",
                                width=180, height=32,
                                font=ctk.CTkFont(family="Segoe UI", size=12),
                                fg_color=C_SURFACE, hover_color=C_ELEVATED,
                                border_width=1, border_color=C_BORDER, text_color=C_TXT2)
    # Not packed yet — appear only when needed

    win.update()
    _fade_in(win)

    # ── Callbacks ─────────────────────────────────────────────────────────────
    def update_status(msg):
        app.after(0, lambda m=msg: status_var.set(m))

    def _on_success():
        def _ui():
            bar.stop()
            _fade_out(win, win.destroy)
            on_complete()
        app.after(0, _ui)

    def _on_failure(msg):
        def _ui():
            bar.stop()
            status_var.set(f"Setup failed: {msg}")

            def _dismiss():
                _fade_out(win, win.destroy)
                on_cancel()

            cancel_btn2.configure(command=_dismiss)
            cancel_btn2.pack(padx=20, pady=(6, 12))
            win.protocol("WM_DELETE_WINDOW", _dismiss)
        app.after(0, _ui)

    def _start_setup():
        # Transition to phase 2
        confirm_frame.pack_forget()
        btn_frame.pack_forget()
        bar.pack(fill="x", padx=20, pady=(20, 6))
        bar.start()
        status_lbl.pack(padx=20, anchor="w")
        win.protocol("WM_DELETE_WINDOW", lambda: None)  # lock close during download
        threading.Thread(
            target=_run_chatterbox_setup,
            args=(update_status, _on_success, _on_failure),
            daemon=True,
        ).start()

    def _cancel():
        _fade_out(win, win.destroy)
        on_cancel()

    ctk.CTkButton(btn_frame, text="Set Up Natural Mode", command=_start_setup,
                  height=36, font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                  fg_color=C_ACCENT, hover_color=C_ACCENT_H,
                  text_color="#000000").pack(side="left", padx=(0, 10))
    ctk.CTkButton(btn_frame, text="Cancel", command=_cancel,
                  height=36, width=90,
                  font=ctk.CTkFont(family="Segoe UI", size=12),
                  **BTN_GHOST).pack(side="left")


def _make_chatterbox_loading_modal():
    """Create and return the Natural mode loading modal. Non-dismissible while loading."""
    win = ctk.CTkToplevel(app)
    win.title("Loading Natural Mode")
    _center_window(win, 500, 260)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.transient(app)
    win.protocol("WM_DELETE_WINDOW", lambda: None)  # block close button
    win.attributes("-topmost", True)               # stay above main window
    win.lift()                                      # bring to front

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=54)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    hdr_inner = ctk.CTkFrame(hdr, fg_color="transparent")
    hdr_inner.pack(side="left", padx=16, pady=10)
    ctk.CTkFrame(hdr_inner, fg_color=C_ACCENT, width=4, height=20,
                 corner_radius=2).pack(side="left", padx=(0, 10))
    ctk.CTkLabel(hdr_inner, text="Loading Natural Mode",
                 font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                 text_color=C_TXT).pack(side="left")
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    # ── Download warning callout ───────────────────────────────────────────────
    callout = ctk.CTkFrame(win, fg_color=C_ACCENT_D, corner_radius=8)
    callout.pack(fill="x", padx=20, pady=(16, 0))
    ctk.CTkLabel(
        callout,
        text="First launch downloads ~3 GB of model files (5–15 min).\n"
             "Subsequent loads take ~30 seconds. Do not close the app.",
        font=ctk.CTkFont(family="Segoe UI", size=11),
        text_color=C_WARN,
        justify="left",
    ).pack(anchor="w", padx=12, pady=8)

    # ── Progress bar (indeterminate) ───────────────────────────────────────────
    bar = ctk.CTkProgressBar(win, mode="indeterminate",
                             progress_color=C_ACCENT, fg_color=C_ACCENT_D)
    bar.pack(fill="x", padx=20, pady=(14, 6))
    bar.start()

    # ── Status text ────────────────────────────────────────────────────────────
    status_var = ctk.StringVar(value="Starting…")
    ctk.CTkLabel(win, textvariable=status_var,
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT2).pack(padx=20, anchor="w")

    # Force a render pass then fade in before the background thread starts
    win.update()
    _fade_in(win)

    def update_status(msg):
        app.after(0, lambda m=msg: status_var.set(m))

    def close_modal():
        def _do():
            bar.stop()
            _fade_out(win, win.destroy)
        app.after(0, _do)

    return update_status, close_modal


def _get_free_ram_gb() -> float:
    """Return available physical RAM in GB (Windows only)."""
    try:
        class _MEMSTATEX(ctypes.Structure):
            _fields_ = [
                ("dwLength",                ctypes.c_ulong),
                ("dwMemoryLoad",            ctypes.c_ulong),
                ("ullTotalPhys",            ctypes.c_ulonglong),
                ("ullAvailPhys",            ctypes.c_ulonglong),
                ("ullTotalPageFile",        ctypes.c_ulonglong),
                ("ullAvailPageFile",        ctypes.c_ulonglong),
                ("ullTotalVirtual",         ctypes.c_ulonglong),
                ("ullAvailVirtual",         ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual",ctypes.c_ulonglong),
            ]
        stat = _MEMSTATEX()
        stat.dwLength = ctypes.sizeof(stat)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullAvailPhys / (1024 ** 3)
    except Exception:
        return 999.0   # if we can't check, don't block the user


def _show_low_ram_warning(free_gb: float, on_proceed, on_cancel):
    """Warn the user that free RAM is below 6 GB before loading Natural mode."""
    win = ctk.CTkToplevel(app)
    win.title("Low Memory Warning")
    _center_window(win, 420, 310)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    win.transient(app)
    win.attributes("-topmost", True)
    win.lift()
    _fade_in(win)

    # Header
    top = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=72)
    top.pack(fill="x")
    top.pack_propagate(False)
    ctk.CTkLabel(top, text="⚠", font=ctk.CTkFont(family="Segoe UI", size=28),
                 text_color="#FFA500").pack(side="left", padx=(20, 10), pady=16)
    ctk.CTkLabel(top, text="Low memory detected",
                 font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                 text_color=C_TXT, anchor="w").pack(side="left", pady=16)

    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    body = ctk.CTkFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=16)

    ctk.CTkLabel(body,
                 text=f"Natural mode needs at least 6 GB of free RAM.\n"
                      f"You currently have {free_gb:.1f} GB available.\n\n"
                      "Loading may fail or cause your system to slow down significantly.",
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT2, wraplength=368, justify="left", anchor="w",
                 ).pack(anchor="w", pady=(0, 16))

    btns = ctk.CTkFrame(body, fg_color="transparent")
    btns.pack(anchor="w")

    def _proceed():
        win.destroy()
        on_proceed()

    def _cancel():
        win.destroy()
        on_cancel()

    ctk.CTkButton(btns, text="Load Anyway", width=140, command=_proceed,
                  fg_color=C_ACCENT, hover_color=C_ACCENT_H,
                  font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold")
                  ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(btns, text="Cancel", width=100, command=_cancel,
                  fg_color=C_SURFACE,
                  font=ctk.CTkFont(family="Segoe UI", size=12)
                  ).pack(side="left")


def _show_oom_modal():
    """Plain-English dialog shown when Natural mode fails to load due to low RAM."""
    win = ctk.CTkToplevel(app)
    win.title("Not Enough Memory")
    _center_window(win, 420, 300)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    win.transient(app)
    win.attributes("-topmost", True)
    win.lift()
    _fade_in(win)

    # Icon + title
    top = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=72)
    top.pack(fill="x")
    top.pack_propagate(False)
    ctk.CTkLabel(top, text="⚠", font=ctk.CTkFont(family="Segoe UI", size=28),
                 text_color=C_ACCENT).pack(side="left", padx=(20, 10), pady=16)
    ctk.CTkLabel(top, text="Natural mode requires more RAM",
                 font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                 text_color=C_TXT, anchor="w").pack(side="left", pady=16)

    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    body = ctk.CTkFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=18)

    ctk.CTkLabel(body,
                 text="The Chatterbox model needs at least 6 GB of free RAM to load.\n"
                      "Your system didn't have enough available memory.",
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT2, wraplength=368, justify="left", anchor="w"
                 ).pack(anchor="w", pady=(0, 12))

    ctk.CTkLabel(body, text="To free up memory:",
                 font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                 text_color=C_TXT, anchor="w").pack(anchor="w")
    for tip in ("• Close browser tabs and other open applications",
                "• Restart your PC to clear background processes",
                "• Use Fast mode — it runs with under 1 GB of RAM"):
        ctk.CTkLabel(body, text=tip,
                     font=ctk.CTkFont(family="Segoe UI", size=12),
                     text_color=C_TXT2, anchor="w").pack(anchor="w")

    ctk.CTkButton(win, text="OK — Use Fast Mode", width=160, height=34,
                  font=ctk.CTkFont(family="Segoe UI", size=12),
                  fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#000000",
                  corner_radius=6,
                  command=lambda: _fade_out(win, win.destroy)).pack(pady=(0, 20))


def _load_chatterbox_bg(update_modal, close_modal):
    """Load Chatterbox in a background thread; called when user switches to Natural mode."""
    def _st(msg):
        update_modal(msg)
        app.after(0, lambda m=msg: status_label.configure(text=m))

    app.after(0, lambda: play_button.configure(state="disabled"))
    app.after(0, lambda: engine_toggle.configure(state="disabled"))
    try:
        # Free RAM on low-memory machines — enhance worker holds torch in memory
        if enhance_engine.is_ready:
            enhance_engine.stop()
        chatterbox_engine.start(status_cb=_st)
        close_modal()
        app.after(0, lambda: engine_toggle.configure(state="normal"))
        app.after(0, lambda: play_button.configure(state="normal"))
        app.after(0, lambda: status_label.configure(
            text="✅ Natural mode ready  ·  Ctrl+Enter to generate"))
    except Exception as e:
        _log_crash(e)
        err = _fmt_err(e)
        close_modal()
        app.after(0, lambda: engine_var.set("Fast"))   # revert the toggle
        app.after(0, lambda: engine_toggle.configure(state="normal"))
        app.after(0, lambda: play_button.configure(state="normal"))
        _raw = str(e).lower()
        _is_oom = isinstance(e, MemoryError) or any(
            k in _raw for k in ("not enough ram", "not enough memory",
                                "out of memory", "paging file", "winerror 1455"))
        if _is_oom:
            app.after(0, _show_oom_modal)
        else:
            app.after(0, lambda m=err: status_label.configure(
                text=f"❌ Natural mode failed: {m}"))

# ── License activation modal ──────────────────────────────────────────────────
_activation_modal_open = [False]   # singleton guard — never open two at once


def _show_activation_modal(can_skip=True, remaining=0):
    """Show the license key activation dialog."""
    if _activation_modal_open[0]:
        return
    _activation_modal_open[0] = True

    win = ctk.CTkToplevel(app)
    win.title("Activate VoxWild")
    _center_window(win, 460, 290)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    win.transient(app)
    _fade_in(win)

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=54)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    hdr_inner = ctk.CTkFrame(hdr, fg_color="transparent")
    hdr_inner.pack(side="left", padx=16, pady=10)
    ctk.CTkFrame(hdr_inner, fg_color=C_ACCENT, width=4, height=20,
                 corner_radius=2).pack(side="left", padx=(0, 10))
    ctk.CTkLabel(hdr_inner, text="Activate Pro",
                 font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                 text_color=C_TXT).pack(side="left")
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    # ── Body ──────────────────────────────────────────────────────────────────
    body = ctk.CTkFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=(16, 8))

    ctk.CTkLabel(body, text="Enter your license key to unlock unlimited Natural mode and AI Enhancement.",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT2, wraplength=400, justify="left").pack(anchor="w", pady=(0, 10))

    ctk.CTkLabel(body, text="License Key",
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT2).pack(anchor="w")
    key_entry = ctk.CTkEntry(
        body, width=410, height=36,
        placeholder_text="XXXX-XXXX-XXXX-XXXX",
        font=ctk.CTkFont(family="Segoe UI", size=13))
    key_entry.pack(pady=(4, 8))

    # Pre-fill if there's a saved (unactivated) key
    saved_key = _lic.load_license().get("key") or ""
    if saved_key:
        key_entry.insert(0, saved_key)

    msg_label = ctk.CTkLabel(body, text="",
                             font=ctk.CTkFont(family="Segoe UI", size=11),
                             text_color=C_DANGER)
    msg_label.pack(anchor="w")

    # ── Buttons ───────────────────────────────────────────────────────────────
    btn_row = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=52)
    btn_row.pack(fill="x", side="bottom")
    btn_row.pack_propagate(False)
    ctk.CTkFrame(btn_row, fg_color=C_BORDER, height=1,
                 corner_radius=0).pack(fill="x", side="top")

    def _do_activate():
        activate_btn.configure(state="disabled", text="Activating…")
        msg_label.configure(text="", text_color=C_DANGER)
        key = key_entry.get().strip()

        def _run():
            ok, msg = _lic.activate_license(key)
            def _done():
                if ok:
                    msg_label.configure(text=f"✅ {msg}", text_color=C_SUCCESS)
                    activate_btn.configure(text="Activate")
                    _activate_btn.pack_forget()   # hide header button
                    app.after(900, _close_activation)
                else:
                    msg_label.configure(text=f"❌ {msg}", text_color=C_DANGER)
                    activate_btn.configure(state="normal", text="Activate")
            app.after(0, _done)
        threading.Thread(target=_run, daemon=True).start()

    activate_btn = ctk.CTkButton(
        btn_row, text="Activate", width=110, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#000000",
        command=_do_activate)
    activate_btn.pack(side="left", padx=14, pady=9)
    key_entry.bind("<Return>", lambda _e: _do_activate())

    def _open_store():
        import webbrowser
        webbrowser.open("https://voxwild.com")

    ctk.CTkButton(
        btn_row, text="Buy a License", width=110, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12),
        **BTN_GHOST, command=_open_store
    ).pack(side="left", padx=(0, 6), pady=9)

    def _close_activation():
        _activation_modal_open[0] = False
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _close_activation)

    ctk.CTkButton(
        btn_row, text="Later", width=72, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12),
        **BTN_GHOST, command=_close_activation
    ).pack(side="right", padx=14, pady=9)


_UPSELL_FEATURE_TEXT = {
    "natural": (
        "Natural Mode — free trial used up",
        "You've used your 3 free Natural mode generations.\n\n"
        "Upgrade to Pro for unlimited Natural mode, unlimited\n"
        "AI Enhancement, and priority support.",
    ),
    "enhance": (
        "✨ AI Enhancement — free trial used up",
        "You've used your 3 free Resemble Enhance sessions.\n\n"
        "Upgrade to Pro for unlimited AI Enhancement, unlimited\n"
        "Natural mode generations, and priority support.",
    ),
}

def _show_upsell_modal(feature):
    """
    Upsell modal shown when a freemium limit is hit.
    feature: "natural" | "enhance"
    """
    title_text, body_text = _UPSELL_FEATURE_TEXT.get(
        feature, ("Upgrade to Pro", "Upgrade to unlock this feature."))

    win = ctk.CTkToplevel(app)
    win.title("Upgrade to Pro")
    _center_window(win, 520, 270)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    win.transient(app)
    _fade_in(win)

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=54)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    hdr_inner = ctk.CTkFrame(hdr, fg_color="transparent")
    hdr_inner.pack(side="left", padx=16, pady=10)
    ctk.CTkFrame(hdr_inner, fg_color=C_ACCENT, width=4, height=20,
                 corner_radius=2).pack(side="left", padx=(0, 10))
    ctk.CTkLabel(hdr_inner, text=title_text,
                 font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                 text_color=C_TXT).pack(side="left")
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    # ── Body ──────────────────────────────────────────────────────────────────
    body = ctk.CTkFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=(18, 8))

    ctk.CTkLabel(body, text=body_text,
                 font=ctk.CTkFont(family="Segoe UI", size=12),
                 text_color=C_TXT2, justify="left").pack(anchor="w")

    # ── Buttons ───────────────────────────────────────────────────────────────
    btn_row = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=52)
    btn_row.pack(fill="x", side="bottom")
    btn_row.pack_propagate(False)
    ctk.CTkFrame(btn_row, fg_color=C_BORDER, height=1,
                 corner_radius=0).pack(fill="x", side="top")

    ctk.CTkButton(
        btn_row, text="Monthly — $12/mo", width=140, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#000000",
        command=lambda: webbrowser.open(_lic.STORE_URL_MONTHLY),
    ).pack(side="left", padx=(14, 4), pady=9)

    ctk.CTkButton(
        btn_row, text="Lifetime — $89", width=130, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#000000",
        command=lambda: webbrowser.open(_lic.STORE_URL_LIFETIME),
    ).pack(side="left", padx=(0, 4), pady=9)

    ctk.CTkButton(
        btn_row, text="Fast mode only", width=110, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12),
        **BTN_GHOST,
        command=win.destroy,
    ).pack(side="left", padx=(0, 4), pady=9)

    def _activate():
        win.destroy()
        _show_activation_modal(can_skip=True)

    ctk.CTkButton(
        btn_row, text="I have a key", width=100, height=34,
        font=ctk.CTkFont(family="Segoe UI", size=12),
        **BTN_GHOST,
        command=_activate,
    ).pack(side="right", padx=14, pady=9)

    win.protocol("WM_DELETE_WINDOW", win.destroy)


def _on_splash_done():
    app.deiconify()
    app.state("zoomed")   # maximize (fullscreen with taskbar visible)
    # Onboarding must fully complete before the license modal can appear.
    # Passing _handle_license_on_startup as on_done ensures they never overlap.
    _show_onboarding(on_done=_handle_license_on_startup)


def _handle_license_on_startup():
    """Check license state on startup.

    Freemium model: Fast mode is free forever — no blocking modal, no forced exit.
    Activated Pro users get silent background re-validation.
    Free users see the 🔑 Activate button in the toolbar (already packed at startup).
    """
    lic = _lic.load_license()

    if lic.get("activated"):
        # Pro user — silently re-validate in background once per session
        def _revalidate():
            valid = _lic.validate_license_silent(lic.get("key"))
            if not valid:
                # Suspend locally so freemium limits kick in, but keep the key
                # so next launch can re-validate (e.g. if access is restored).
                lic["activated"] = False
                _lic.save_license(lic)
                app.after(0, lambda: status_label.configure(
                    text="⚠️ License revoked or expired. Pro features are now limited."))
                app.after(0, lambda: _activate_btn.pack(side="right", padx=(0, 6), pady=14))
        threading.Thread(target=_revalidate, daemon=True).start()
    elif lic.get("key"):
        # Suspended license — key exists but not activated. Re-check silently.
        def _recheck():
            valid = _lic.validate_license_silent(lic.get("key"))
            if valid:
                lic["activated"] = True
                _lic.save_license(lic)
                app.after(0, lambda: status_label.configure(
                    text="✅ License re-activated. Welcome back!"))
        threading.Thread(target=_recheck, daemon=True).start()
    # Free users: Activate button already visible in header — nothing else to do.

# Seed Kokoro calibration on first-ever launch using a quick inference.
# Skipped if calibration data already exists. Takes ~150-250ms — imperceptible.
def _run_kokoro_benchmark():
    _BENCH_TEXT = "The quick brown fox jumps over the lazy dog today."
    try:
        if load_calibration().get("words_per_second"):
            return  # already calibrated
        _t0 = time.time()
        kokoro.create(_BENCH_TEXT, voice="af_heart", speed=1.0)
        record_calibration(len(_BENCH_TEXT.split()), time.time() - _t0, use_cb=False)
    except Exception:
        pass  # non-critical; never block startup

def _show_update_banner(latest_tag: str):
    """Show the amber update banner below the header. Called from main thread only."""
    _update_banner_label.configure(text=f"🔔 Update available: {latest_tag}")
    _update_banner_link.configure(
        text="Install ↻",
        command=lambda v=latest_tag: _start_in_app_update(v),
    )
    _update_banner.pack(fill="x", before=prog_row)


def _start_in_app_update(latest_tag: str):
    """Begin the in-app patch update flow with a polished progress modal."""
    target_version = latest_tag.lstrip("v")

    # ── Modal window ─────────────────────────────────────────────────────────
    win = ctk.CTkToplevel(app)
    win.title("VoxWild Update")
    _center_window(win, 440, 240)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    win.transient(app)
    win.protocol("WM_DELETE_WINDOW", lambda: None)

    # Header
    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=52)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    ctk.CTkLabel(hdr, text=f"Updating to {latest_tag}",
                 font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
                 text_color=C_TXT).pack(side="left", padx=20)

    # Body
    body = ctk.CTkFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=(20, 0))

    phase_lbl = ctk.CTkLabel(body, text="Preparing...",
                             font=ctk.CTkFont(family="Segoe UI", size=13),
                             text_color=C_TXT, anchor="w")
    phase_lbl.pack(fill="x")

    detail_lbl = ctk.CTkLabel(body, text="",
                              font=ctk.CTkFont(family="Consolas", size=11),
                              text_color=C_TXT3, anchor="w")
    detail_lbl.pack(fill="x", pady=(2, 10))

    # Progress bar — thicker, emerald, with percentage
    bar_row = ctk.CTkFrame(body, fg_color="transparent")
    bar_row.pack(fill="x")
    pb = ctk.CTkProgressBar(bar_row, height=10, corner_radius=5,
                            progress_color=C_ACCENT, fg_color=C_ELEVATED)
    pb.pack(side="left", fill="x", expand=True)
    pb.set(0)
    pct_lbl = ctk.CTkLabel(bar_row, text="0%", width=42,
                           font=ctk.CTkFont(family="Consolas", size=11),
                           text_color=C_ACCENT)
    pct_lbl.pack(side="right", padx=(8, 0))

    # Footer
    foot = ctk.CTkFrame(win, fg_color="transparent")
    foot.pack(fill="x", padx=24, pady=(10, 16))
    cancel_btn = ctk.CTkButton(foot, text="Cancel", width=80, height=28,
                               **BTN_GHOST, command=win.destroy)
    cancel_btn.pack(side="right")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _set_phase(msg):
        app.after(0, lambda m=msg: phase_lbl.configure(text=m))

    def _set_detail(msg):
        app.after(0, lambda m=msg: detail_lbl.configure(text=m))

    def _set_pct(frac):
        frac = max(0.0, min(1.0, frac))
        pct = int(frac * 100)
        app.after(0, lambda f=frac, p=pct: (pb.set(f), pct_lbl.configure(text=f"{p}%")))

    def _fail(msg, offer_github=True):
        app.after(0, lambda: phase_lbl.configure(text=msg, text_color=C_DANGER))
        app.after(0, lambda: pb.configure(progress_color=C_DANGER))
        app.after(0, lambda: pct_lbl.configure(text_color=C_DANGER))
        if offer_github:
            app.after(0, lambda: cancel_btn.configure(
                text="Open GitHub",
                command=lambda: (webbrowser.open(
                    f"https://github.com/{GITHUB_REPO}/releases/latest"), win.destroy())
            ))
        else:
            app.after(0, lambda: cancel_btn.configure(text="Close", command=win.destroy))

    # ── Update thread ─────────────────────────────────────────────────────────
    # Progress budget:
    #   0-5%    : checking for patch
    #   5-80%   : downloading (proportional to bytes)
    #   80-88%  : verifying
    #   88-98%  : copying files + staging exe
    #   98-100% : preparing restart

    def _run():
        try:
            import update_patcher
            from pathlib import Path
            update_patcher.cleanup_old_patches()

            # ── Check ────────────────────────────────────────────────────────
            _set_phase("Checking for update...")
            _set_pct(0.02)
            try:
                patch_url = update_patcher.fetch_patch_url(GITHUB_REPO, target_version)
            except Exception as e:
                _fail(f"Could not reach GitHub: {e}")
                return
            if not patch_url:
                _fail("No patch for this release. Use the full installer.")
                return
            _set_pct(0.05)

            # ── Download ─────────────────────────────────────────────────────
            import tempfile
            import os as _os
            tmp_zip = Path(tempfile.gettempdir()) / f"VoxWild-Patch-{target_version}.zip"

            _set_phase("Downloading...")
            def on_progress(read, total):
                if total > 0:
                    frac = 0.05 + (read / total) * 0.75
                    mb_r = read / 1024 / 1024
                    mb_t = total / 1024 / 1024
                    _set_pct(frac)
                    _set_detail(f"{mb_r:.1f} / {mb_t:.1f} MB")
                else:
                    _set_pct(0.4)
                    _set_detail(f"{read / 1024 / 1024:.1f} MB")

            try:
                update_patcher.download_patch(patch_url, tmp_zip, progress=on_progress)
            except Exception as e:
                _fail(f"Download failed: {e}")
                return
            _set_detail("")

            # ── Verify ───────────────────────────────────────────────────────
            _set_phase("Verifying integrity...")
            _set_pct(0.82)
            import time as _time
            _time.sleep(0.3)  # brief pause so "Verifying" is visible
            ok, msg = update_patcher.verify_patch(tmp_zip)
            if not ok:
                _fail(f"Verification failed: {msg}")
                try: _os.unlink(tmp_zip)
                except Exception: pass
                return
            _set_pct(0.88)

            # ── Install ──────────────────────────────────────────────────────
            app.after(0, lambda: cancel_btn.configure(state="disabled"))
            _set_phase("Stopping workers...")
            _set_detail("Releasing file locks")

            # Stop any running workers BEFORE copying — their loaded DLLs
            # (vcomp140.dll, torch libs) will block shutil.copy2.
            try: chatterbox_engine.stop()
            except Exception: pass
            try: enhance_engine.stop()
            except Exception: pass
            import time as _time
            _time.sleep(1)  # brief settle for file handles to release

            _set_phase("Installing files...")

            def _install_status(msg):
                _set_detail(msg)
                cur = pb.get() if hasattr(pb, 'get') else 0.88
                _set_pct(min(0.98, cur + 0.01))

            ok, msg = update_patcher.apply_patch(tmp_zip, status_cb=_install_status)
            if not ok:
                _fail(f"Install failed: {msg}", offer_github=False)
                return

            # ── Restart ──────────────────────────────────────────────────────
            _set_pct(1.0)
            _set_phase("Restarting VoxWild...")
            _set_detail("Update will apply on restart")

            def _quit():
                try: sd.stop()
                except Exception: pass
                try: chatterbox_engine.stop()
                except Exception: pass
                try: enhance_engine.stop()
                except Exception: pass
                def _final():
                    try: app.destroy()
                    except Exception: pass
                    os._exit(0)
                app.after(500, _final)

            app.after(1200, _quit)

        except Exception as e:
            import traceback as _tb
            _fail(f"Unexpected error: {e}")
            _log_crash(e)
            try:
                with open(os.path.join(_USER_DIR, "update_check.log"), "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] UPDATE EXCEPTION:\n{_tb.format_exc()}\n")
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


_update_check_done = [False]  # guard against double-fire

def _check_for_update():
    """Background thread: check GitHub releases API on every launch.
    Writes diagnostic log so silent failures can be debugged."""
    if _update_check_done[0]:
        return
    _update_check_done[0] = True
    _log_path = os.path.join(
        os.environ.get("APPDATA", ""), "TTS Studio", "update_check.log")
    def _log(msg):
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass
    try:
        _log(f"Starting update check — current VERSION={VERSION}")
        import urllib.request
        import json as _json
        import ssl as _ssl

        # Use certifi's CA bundle — frozen PyInstaller apps don't have access
        # to the system CA store, causing HTTPS requests to fail with
        # CERTIFICATE_VERIFY_FAILED.
        try:
            import certifi
            ctx = _ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = _ssl.create_default_context()

        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(
            api_url, headers={"User-Agent": f"VoxWild/{VERSION}"})
        _log(f"Fetching {api_url}")
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            data = _json.loads(resp.read())
        tag = data.get("tag_name", "").lstrip("v")
        _log(f"GitHub returned tag_name='{data.get('tag_name')}' (parsed tag='{tag}')")
        if not tag:
            _log("No tag returned — skipping")
            return
        current = tuple(int(x) for x in VERSION.split(".") if x.isdigit())
        latest  = tuple(int(x) for x in tag.split(".")  if x.isdigit())
        _log(f"current={current} latest={latest} newer={latest > current}")
        if latest > current:
            v = data["tag_name"]
            _log(f"Showing update banner for {v}")
            app.after(0, lambda: _show_update_banner(v))
    except Exception as e:
        import traceback
        _log(f"EXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()}")


def _show_onboarding(on_done=None):
    """Welcome card shown once on first launch. Saved to settings so it never repeats.

    on_done — optional callable invoked after the card is dismissed (or immediately
              if onboarding has already been seen). Use this to chain startup steps
              that must not overlap with the welcome modal (e.g. license check).
    """
    if _get_settings().get("seen_onboarding", False):
        if on_done:
            on_done()
        return

    win = ctk.CTkToplevel(app)
    win.title("Welcome to VoxWild")
    _center_window(win, 500, 430)
    win.resizable(False, False)
    win.configure(fg_color=C_BG)
    win.grab_set()
    win.transient(app)
    win.protocol("WM_DELETE_WINDOW", lambda: None)  # must use the button to dismiss
    win.attributes("-topmost", True)
    win.lift()
    _fade_in(win)

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=64)
    hdr.pack(fill="x")
    hdr.pack_propagate(False)
    hdr_inner = ctk.CTkFrame(hdr, fg_color="transparent")
    hdr_inner.pack(side="left", padx=20, pady=12)
    ctk.CTkFrame(hdr_inner, fg_color=C_ACCENT, width=4, height=28,
                 corner_radius=2).pack(side="left", padx=(0, 12))
    title_col = ctk.CTkFrame(hdr_inner, fg_color="transparent")
    title_col.pack(side="left")
    ctk.CTkLabel(title_col, text="Welcome to VoxWild",
                 font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
                 text_color=C_TXT).pack(anchor="w")
    ctk.CTkLabel(title_col, text="Here's what you need to know to get started",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT3).pack(anchor="w")
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")

    body = ctk.CTkFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=(18, 0))

    # ── Fast mode card ────────────────────────────────────────────────────────
    fast_card = ctk.CTkFrame(body, fg_color=C_CARD, corner_radius=8,
                             border_width=1, border_color=C_BORDER)
    fast_card.pack(fill="x", pady=(0, 10))
    fast_inner = ctk.CTkFrame(fast_card, fg_color="transparent")
    fast_inner.pack(fill="x", padx=14, pady=12)
    ctk.CTkLabel(fast_inner, text="Fast Mode",
                 font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                 text_color=C_ACCENT).pack(anchor="w")
    ctk.CTkLabel(fast_inner,
                 text="Uses the Kokoro ONNX engine — generates in seconds with no\n"
                      "internet required. Great for scripts, bulk work, and previews.\n"
                      "Choose from 13 voices in the Voice dropdown.",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT2, justify="left", wraplength=430).pack(anchor="w", pady=(4, 0))

    # ── Natural mode card ─────────────────────────────────────────────────────
    nat_card = ctk.CTkFrame(body, fg_color=C_CARD, corner_radius=8,
                            border_width=1, border_color=C_BORDER)
    nat_card.pack(fill="x", pady=(0, 10))
    nat_inner = ctk.CTkFrame(nat_card, fg_color="transparent")
    nat_inner.pack(fill="x", padx=14, pady=12)
    ctk.CTkLabel(nat_inner, text="Natural Mode",
                 font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                 text_color=C_TXT).pack(anchor="w")
    ctk.CTkLabel(nat_inner,
                 text="Uses the Chatterbox neural TTS engine — more expressive and\n"
                      "human-sounding. Downloads ~3 GB on first use (5–15 min).\n"
                      "Supports voice cloning from a short audio recording.",
                 font=ctk.CTkFont(family="Segoe UI", size=11),
                 text_color=C_TXT2, justify="left", wraplength=430).pack(anchor="w", pady=(4, 0))

    # ── Tip ───────────────────────────────────────────────────────────────────
    ctk.CTkLabel(body, text="Tip: press Ctrl+/ at any time to open Help & keyboard shortcuts.",
                 font=ctk.CTkFont(family="Segoe UI", size=10),
                 text_color=C_TXT3).pack(anchor="w", pady=(0, 14))

    # ── Footer: checkbox + button ─────────────────────────────────────────────
    ctk.CTkFrame(win, fg_color=C_BORDER, height=1, corner_radius=0).pack(fill="x")
    footer = ctk.CTkFrame(win, fg_color=C_SURFACE, corner_radius=0, height=54)
    footer.pack(fill="x", side="bottom")
    footer.pack_propagate(False)

    dont_show_var = ctk.BooleanVar(value=True)
    ctk.CTkCheckBox(footer, text="Don't show again", variable=dont_show_var,
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    text_color=C_TXT2, checkmark_color=C_ACCENT,
                    fg_color=C_ACCENT_D, hover_color=C_ACCENT_D,
                    border_color=C_BORDER).pack(side="left", padx=20)

    def _dismiss():
        if dont_show_var.get():
            s = _get_settings()
            s["seen_onboarding"] = True
            _save_settings(s)
        def _after_close():
            win.destroy()
            if on_done:
                on_done()
        _fade_out(win, _after_close)

    ctk.CTkButton(footer, text="Get Started  →", command=_dismiss,
                  width=130, height=34,
                  font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                  fg_color=C_ACCENT, hover_color=C_ACCENT_H,
                  text_color="#000000", corner_radius=8).pack(side="right", padx=20)
    win.bind("<Return>", lambda _: _dismiss())


_run_kokoro_benchmark()

# Load persisted history before splash so cards are ready when UI appears
app.after(0, _load_history)

_splash = _run_splash(_on_splash_done)
# Kokoro is already loaded at this point — complete the splash bar
app.after(100, _splash._finish)

# Clean up leftover .old files from a previous in-app update (best effort).
def _cleanup_stale_old_files():
    try:
        if getattr(sys, "frozen", False):
            install_dir = os.path.dirname(sys.executable)
            # Remove .old files from a previous update
            for name in os.listdir(install_dir):
                if name.lower().endswith(".old"):
                    try:
                        os.remove(os.path.join(install_dir, name))
                    except OSError:
                        pass
            # Check for interrupted update (Phase A completed, Phase B failed)
            try:
                import update_patcher
                if update_patcher.check_interrupted_update():
                    ok, msg = update_patcher.retry_exe_swap()
                    if ok:
                        app.after(0, lambda: status_label.configure(
                            text="Update installed. Restart VoxWild to use the new version."))
            except Exception:
                pass
        try:
            import update_patcher
            update_patcher.cleanup_old_patches()
        except Exception:
            pass
    except Exception:
        pass
app.after(3000, _cleanup_stale_old_files)

# Check for updates in the background — 2 s delay so the UI is fully settled first
app.after(2000, lambda: threading.Thread(target=_check_for_update, daemon=True).start())

app.mainloop()