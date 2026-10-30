#!/usr/bin/env python3
"""
ame_launcher.py — installer and launcher with a GUI (Tkinter).

Usage: double-click start.bat (Windows) or run ./start.sh (Linux/macOS).

Flow:
  1. A boot screen in the style of the game's BIOS (text only). While it
     types, it really checks Python, the virtual environment, the
     dependencies and the memory model.
  2. If anything is missing, it installs itself, with a per-step bar and an
     overall bar.
  3. A screen with the LAUNCH button: starts the server on 127.0.0.1:8000
     and opens the browser. Closing the window shuts the server down.

Standard library only: it runs on the system Python, BEFORE the virtual
environment exists. What gets installed lives in install.py (a single
source of truth; install.py still works from the console for whoever
prefers it). All user-facing text comes from installer_i18n.py.

Environment variables for testing:
  AME_LAUNCHER_SMOKE=1   runs through everything without clicks, launches,
                         opens and closes by itself.
  AME_NO_BROWSER=1       doesn't open the browser.
  AME_AUTOLAUNCH=1       presses LAUNCH by itself (without closing afterwards).
"""

import os
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

import tkinter as tk
from tkinter import ttk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
import install  # noqa: E402  (standard library only)
import installer_i18n as i18n  # noqa: E402


# ── Credit ────────────────────────────────────────────────────────
# Encoded on purpose (so it isn't a plain string to edit) and checked
# against a hash; if _sig.py is missing or altered, the copy below is used.
_SIG_H = "327a79612ba95b84ba1ded504e4bdc697aed23ba5f98ff0bf43ccec441b4862f"
_SIG_B = "j&Gt10FdaTQmnf`hDrO5do#*`<lxpCY4!nSuCS#=#Kwrf?|+3+!O~~{E@1sRsc)q;()4FFwF(4XRB0j"


def _credit() -> dict:
    import base64, hashlib, json, zlib

    def own():
        k = hashlib.sha256(b"internet-yamero").digest()
        raw = base64.b85decode(_SIG_B)
        return zlib.decompress(bytes(b ^ k[i % 32] for i, b in enumerate(raw)))

    tries = [own]
    try:
        import _sig
        tries.insert(0, _sig.blob)
    except Exception:
        pass
    for fn in tries:
        try:
            raw = fn()
            if hashlib.sha256(raw).hexdigest() == _SIG_H:
                return json.loads(raw)
        except Exception:
            continue
    return json.loads(own())


# Sources cited in the LEGAL window (titles stay in English; they're names).
LEGAL_SOURCES = [
    ("WSS playground — NEEDY GIRL OVERDOSE: video, streaming and fan-work guidelines (Steam, 2022)",
     "https://store.steampowered.com/news/app/1451940/view/3132817655051752608"),
    ("WSS playground on X — fan-work guidelines reminder (2023)",
     "https://x.com/infowssJP/status/1617022917497483264"),
    ("United States — 17 U.S.C. § 107, fair use",
     "https://www.law.cornell.edu/uscode/text/17/107"),
    ("United States — Campbell v. Acuff-Rose Music, 510 U.S. 569 (1994): parody as fair use",
     "https://supreme.justia.com/cases/federal/us/510/569/"),
    ("European Union — Directive 2001/29/EC, art. 5(3)(k): caricature, parody, pastiche",
     "https://eur-lex.europa.eu/eli/dir/2001/29/oj"),
    ("European Union — CJEU, Deckmyn, C-201/13 (2014): what counts as parody",
     "https://curia.europa.eu/juris/liste.jsf?num=C-201/13"),
]

HOST, PORT = "127.0.0.1", 8000
URL = f"http://{HOST}:{PORT}"
# Remote access (opt-in): other devices on your network (phone, another
# PC) can open Ame with an access key. The file holds the key; it is kept
# when you switch remote access off and on again, so devices that already
# logged in stay logged in. Without the switch the server only listens on
# this computer, because /settings returns the API keys. See
# remote_access.py (server side) and README "Running on a server".
REMOTE_FILE = os.path.join(APP_DIR, ".remote_access")
_KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i


def _remote_state() -> dict:
    try:
        with open(REMOTE_FILE, encoding="utf-8") as f:
            import json
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _remote_key() -> str | None:
    """The access key if remote access is on, else None."""
    st = _remote_state()
    return st.get("key") if st.get("on") and st.get("key") else None


def _set_remote(on: bool) -> None:
    import json
    st = _remote_state()
    if not st.get("key"):
        st["key"] = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(10))
    st["on"] = on
    with open(REMOTE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def _lan_ip() -> str | None:
    """This computer's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
VERSION = "Ame App 2026.10.28"
LOG_DIR = os.path.join(APP_DIR, "logs")
LOG_INSTALL = os.path.join(LOG_DIR, "install.log")
LOG_SERVER = os.path.join(LOG_DIR, "server.log")
SMOKE = os.environ.get("AME_LAUNCHER_SMOKE") == "1"
NO_BROWSER = os.environ.get("AME_NO_BROWSER") == "1" or SMOKE
AUTOLAUNCH = os.environ.get("AME_AUTOLAUNCH") == "1" or SMOKE

MEMORY_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ── BIOS look ─────────────────────────────────────────────────────────
BLACK, GRAY, WHITE = "#000000", "#b8b8b8", "#e8e8e8"
PINK, CYAN, RED, GREEN = "#ff8fc8", "#6fe3ff", "#ff5a6e", "#7dffa0"
DARK = "#1a1a1a"

WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if WIN else 0          # CREATE_NO_WINDOW


def _venv_python() -> str:
    if WIN:
        return os.path.join(install.VENV_DIR, "Scripts", "python.exe")
    return os.path.join(install.VENV_DIR, "bin", "python")


def _mono_font(root) -> str:
    available = set(root.tk.call("font", "families"))
    for f in ("Consolas", "Cascadia Mono", "Lucida Console", "Menlo",
              "DejaVu Sans Mono", "Liberation Mono", "Courier New"):
        if f in available:
            return f
    return "TkFixedFont"


# ── Child processes: one registry so they can all be killed on exit ────
_children = set()
_children_lock = threading.Lock()


def _popen(cmd, **kw):
    kw.setdefault("cwd", APP_DIR)
    if WIN:
        kw["creationflags"] = kw.get("creationflags", 0) | NO_WINDOW
    else:
        kw["start_new_session"] = True           # own process group → killpg
    p = subprocess.Popen(cmd, **kw)
    with _children_lock:
        _children.add(p)
    return p


def _kill_tree(p):
    """Kills the process AND its children (uvicorn --reload spawns one)."""
    if p is None or p.poll() is not None:
        return
    try:
        if WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=5)
    except Exception:
        try:
            if not WIN:
                os.killpg(p.pid, signal.SIGKILL)
            else:
                p.kill()
        except Exception:
            pass


def _kill_all():
    with _children_lock:
        children = list(_children)
    for p in children:
        _kill_tree(p)


# ── Checks ────────────────────────────────────────────────────────────
def _port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def _ame_is_running() -> bool:
    try:
        with urllib.request.urlopen(URL + "/onboarding_status", timeout=1.5) as r:
            return r.status == 200 and b"done" in r.read(200)
    except Exception:
        return False


_CHECK_DEPS = (
    "import importlib, sys\n"
    "missing = []\n"
    "for m in ('fastapi','uvicorn','watchfiles','numpy','requests',"
    "'torch','sentence_transformers','PIL'):\n"
    "    try: importlib.import_module(m)\n"
    "    except Exception: missing.append(m)\n"
    "try: import python_multipart\n"
    "except Exception:\n"
    "    try: import multipart\n"
    "    except Exception: missing.append('python-multipart')\n"
    "print('MISSING=' + ','.join(missing))\n"
)

_CHECK_MODEL = (
    "from huggingface_hub import try_to_load_from_cache as t\n"
    f"r = t('{MEMORY_MODEL}', 'config.json')\n"
    "print('MODEL=' + ('yes' if isinstance(r, str) else 'no'))\n"
)

_DOWNLOAD_MODEL = (
    "from sentence_transformers import SentenceTransformer\n"
    f"SentenceTransformer('{MEMORY_MODEL.split('/')[1]}')\n"
    "print('MODEL_OK')\n"
)


def _run_short(code: str, timeout=120) -> str:
    r = subprocess.run([_venv_python(), "-c", code], cwd=APP_DIR,
                       capture_output=True, text=True, timeout=timeout,
                       creationflags=NO_WINDOW if WIN else 0)
    return (r.stdout or "") + (r.stderr or "")


# ── Application ───────────────────────────────────────────────────────
class Launcher:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.server = None
        self.closing = False
        self._after_ids = []
        self.mono = _mono_font(root)

        root.title("Ame")
        root.configure(bg=BLACK)
        root.geometry("960x650")
        root.minsize(760, 590)
        root.protocol("WM_DELETE_WINDOW", self.quit_app)
        icon = os.path.join(APP_DIR, "images", "favicon.png")
        if os.path.exists(icon):
            try:
                root.iconphoto(True, tk.PhotoImage(file=icon))
            except Exception:
                pass

        style = ttk.Style(root)
        style.theme_use("clam")
        for name, color in (("Step", CYAN), ("Total", PINK)):
            style.configure(f"{name}.Horizontal.TProgressbar",
                            troughcolor=DARK, background=color,
                            bordercolor=GRAY, lightcolor=color, darkcolor=color,
                            thickness=14)

        self.lang = i18n.get_language()
        self.screen = None
        self._online = None             # None | True (our own server) | False (already running)

        # Fixed bottom bar: a note on the left, languages on the right.
        self.bottom_bar = tk.Frame(root, bg=BLACK)
        self.bottom_bar.pack(side="bottom", fill="x", padx=48, pady=(0, 14))
        self.footer_note = self._label(self.bottom_bar, "", "#606060", 9)
        self.footer_note.pack(side="left")
        self.lang_zone = tk.Frame(self.bottom_bar, bg=BLACK)
        self.lang_zone.pack(side="right")

        # Credit and fan-work notice, on every screen.
        self.legal_bar = tk.Frame(root, bg=BLACK)
        self.legal_bar.pack(side="bottom", fill="x", padx=48, pady=(0, 4))
        self._draw_legal_bar()

        self.frame = tk.Frame(root, bg=BLACK)
        self.frame.pack(fill="both", expand=True, padx=48, pady=(28, 6))
        self._screen_boot()
        self._after(100, self._drain)

    # ── language ──────────────────────────────────────────────────
    def T(self, key, **kw):
        return i18n.t(key, self.lang, **kw)

    def _draw_languages(self, visible=True):
        for w in self.lang_zone.winfo_children():
            w.destroy()
        if not visible:
            return
        self._label(self.lang_zone, self.T("language") + ":", "#606060", 9).pack(side="left", padx=(0, 6))
        for code, label in i18n.LANGUAGES.items():
            active = code == self.lang
            item = tk.Label(self.lang_zone, text=label, bg=BLACK,
                            fg=PINK if active else "#808080",
                            font=(self.mono, 10, "bold" if active else "normal"),
                            cursor="arrow" if active else "hand2", padx=4)
            item.pack(side="left")
            if not active:
                item.bind("<Button-1>", lambda e, c=code: self._set_language(c))

    def _draw_legal_bar(self):
        for w in self.legal_bar.winfo_children():
            w.destroy()
        c = _credit()
        by = tk.Label(self.legal_bar, text=f"{self.T('credit_by')} {c['by']} ({c['handle']})",
                      fg=PINK, bg=BLACK, font=(self.mono, 9, "bold"), cursor="hand2")
        by.pack(side="left")
        by.bind("<Button-1>", lambda e: webbrowser.open(c["url"]))
        self._label(self.legal_bar, "  ·  " + self.T("legal_short"), "#707070", 9).pack(side="left")
        b = tk.Label(self.legal_bar, text=f"[ {self.T('legal_btn')} ]", fg=CYAN, bg=BLACK,
                     font=(self.mono, 9, "bold"), cursor="hand2")
        b.pack(side="left", padx=(8, 0))
        b.bind("<Button-1>", lambda e: self._show_legal())

    def _show_legal(self):
        win = tk.Toplevel(self.root, bg=BLACK)
        win.title(self.T("legal_title"))
        win.geometry("760x560")
        win.transient(self.root)
        body = tk.Frame(win, bg=BLACK)
        body.pack(fill="both", expand=True, padx=28, pady=22)

        def para(text, color=WHITE, size=10, pad=(0, 10), weight="normal"):
            lab = tk.Label(body, text=text, fg=color, bg=BLACK, justify="left",
                           anchor="w", wraplength=690, font=(self.mono, size, weight))
            lab.pack(fill="x", pady=pad)
            return lab

        para(self.T("legal_title"), PINK, 14, (0, 12), "bold")
        para(self.T("legal_body"))
        para(self.T("legal_sources"), CYAN, 10, (6, 4), "bold")
        for title, url in LEGAL_SOURCES:
            link = para("• " + title, "#9fd8ff", 9, (0, 3))
            link.configure(cursor="hand2")
            link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
        para(self.T("legal_not_advice"), GRAY, 9, (12, 0))
        c = _credit()
        by = para(f"{self.T('credit_by')} {c['by']} ({c['handle']}) — {c['url']}", PINK, 10, (10, 0), "bold")
        by.configure(cursor="hand2")
        by.bind("<Button-1>", lambda e: webbrowser.open(c["url"]))

    def _set_language(self, code):
        self.lang = code
        i18n.save_language(code)
        self._draw_legal_bar()
        if self.screen == "boot":
            self._retry()                           # re-types the screen
        elif self.screen == "launch":
            self._screen_launch(restore=True)
        else:
            self._draw_languages()

    # ── UI helpers ────────────────────────────────────────────────
    def _after(self, ms, fn):
        if not self.closing:
            self._after_ids.append(self.root.after(ms, fn))

    def ui(self, fn, *a):
        """From any thread: asks for fn(*a) to run on the Tk thread."""
        self.q.put((fn, a))

    def _drain(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                try:
                    fn(*a)
                except tk.TclError:
                    pass
        except queue.Empty:
            pass
        self._after(80, self._drain)

    def _label(self, parent, text="", color=GRAY, size=13, **kw):
        return tk.Label(parent, text=text, fg=color, bg=BLACK,
                        font=(self.mono, size, kw.pop("weight", "normal")),
                        anchor="w", justify="left", **kw)

    def _button(self, parent, text, command, color=PINK, size=14):
        b = tk.Label(parent, text=f"[ {text} ]", fg=color, bg=BLACK,
                     font=(self.mono, size, "bold"), cursor="hand2", padx=6)
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.configure(fg=BLACK, bg=color))
        b.bind("<Leave>", lambda e: b.configure(fg=color, bg=BLACK))
        return b

    # ── 1. Boot screen ────────────────────────────────────────────
    def _screen_boot(self):
        self.screen = "boot"
        self.footer_note.configure(text="")
        self._draw_languages()
        top = tk.Frame(self.frame, bg=BLACK)
        top.pack(fill="x")
        left = tk.Frame(top, bg=BLACK)
        left.pack(side="left", anchor="n")
        right = tk.Frame(top, bg=BLACK)
        right.pack(side="right", anchor="n")

        self._label(right, "KAngel", PINK, 30, weight="italic").pack(anchor="e")
        self._label(right, "INTERNET YAMERO", CYAN, 10).pack(anchor="e")
        self._label(right, VERSION, GRAY, 9).pack(anchor="e", pady=(4, 0))

        self.lines = tk.Text(left, bg=BLACK, fg=GRAY, bd=0, highlightthickness=0,
                             font=(self.mono, 13), width=58, height=17,
                             cursor="arrow", wrap="none")
        self.lines.pack(anchor="w")
        for tag, color in (("ok", GREEN), ("bad", RED), ("info", CYAN), ("pink", PINK)):
            self.lines.tag_configure(tag, foreground=color)
        self.lines.configure(state="disabled")

        self.boot_footer = self._label(self.frame, "", WHITE, 13)
        self.boot_footer.pack(side="bottom", anchor="w")
        self._gen = getattr(self, "_gen", 0) + 1     # invalidates an earlier check
        self._dots = 0
        self._booting = True
        self._t0 = time.time()

        header = [
            ("Needy Streamer Overload", None),
            ("", None),
            ("NGO BIOS Rev1.0", None),
            ("", None),
            ("Main Processor : raincandy", None),
            ("Memory Testing : OK", None),
            ("", None),
        ]
        gen = self._gen
        self._type_lines(header, 0, lambda: threading.Thread(
            target=self._check, args=(gen,), daemon=True).start(), gen)
        self._animate_boot(gen)

    def _type_lines(self, lines, i, when_done, gen):
        if gen != self._gen:
            return
        if i >= len(lines):
            when_done()
            return
        text, tag = lines[i]
        self._append(text + "\n", tag)
        self._after(25 if SMOKE else 140,
                    lambda: self._type_lines(lines, i + 1, when_done, gen))

    def _append(self, text, tag=None):
        self.lines.configure(state="normal")
        self.lines.insert("end", text, tag or ())
        self.lines.see("end")
        self.lines.configure(state="disabled")

    def _report(self, label, value, tag, gen):
        self.ui(self._append_if_current, gen, f"{label:<20}: ", None)
        self.ui(self._append_if_current, gen, value + "\n", tag)

    def _append_if_current(self, gen, text, tag):
        """Only writes if the screen still belongs to that check (changing
        the language rebuilds it and silences the old check)."""
        if gen == self._gen and self.screen == "boot":
            self._append(text, tag)

    def _animate_boot(self, gen):
        if not self._booting or gen != self._gen:
            return
        self._dots = (self._dots + 1) % 26
        self.boot_footer.configure(text="Booting Windose20" + "." * self._dots)
        self._after(110, lambda: self._animate_boot(gen))

    # ── the real check (thread) ───────────────────────────────────
    def _check(self, gen):
        T = self.T
        current = lambda: gen == self._gen and not self.closing   # noqa: E731
        try:
            v = sys.version_info
            if v < (3, 10):
                self._report(T("chk_python"), T("val_need_python", v=f"{v.major}.{v.minor}"), "bad", gen)
                self.ui(self._error_if_current, gen, T("err_python"))
                return
            self._report(T("chk_python"), f"{v.major}.{v.minor}.{v.micro}  OK", "ok", gen)

            if not os.path.exists(os.path.join(APP_DIR, "main.py")):
                self._report(T("chk_files"), T("val_missing_main"), "bad", gen)
                self.ui(self._error_if_current, gen, T("err_folder"))
                return
            install.ensure_static_index()

            pending = []
            if not os.path.exists(_venv_python()):
                self._report(T("chk_venv"), T("val_not_installed"), "info", gen)
                pending = ["all"]
            else:
                self._report(T("chk_venv"), T("val_ok"), "ok", gen)
                output = _run_short(_CHECK_DEPS, timeout=180)
                m = re.search(r"MISSING=(.*)", output)
                missing = [x for x in (m.group(1).strip().split(",") if m else ["?"]) if x]
                if missing:
                    self._report(T("chk_deps"), T("val_missing", x=", ".join(missing)), "info", gen)
                    pending = ["deps"]
                elif not install.deps_ok(_venv_python())[0]:
                    self._report(T("chk_deps"), T("val_conflict"), "info", gen)
                    pending = ["deps"]
                else:
                    self._report(T("chk_deps"), T("val_ok"), "ok", gen)
                    output = _run_short(_CHECK_MODEL, timeout=60)
                    if "MODEL=yes" in output:
                        self._report(T("chk_model"), T("val_ok"), "ok", gen)
                    else:
                        self._report(T("chk_model"), T("val_to_download"), "info", gen)
                        pending = ["model"]

            if not current():
                return
            if pending:
                self.ui(self._append_if_current, gen, "\n" + T("first_time") + "\n", "pink")
            self._wait_boot()
            if not current():
                return                      # the language changed: another check carries on
            if pending:
                self.ui(self._screen_setup, pending[0])
            else:
                self.ui(self._screen_launch)
        except Exception:
            self._log_install(traceback.format_exc())
            self.ui(self._error_if_current, gen, T("err_check"))

    def _wait_boot(self):
        """Lets the boot screen be read (about 4 s)."""
        if not SMOKE:
            time.sleep(max(0.8, 4.0 - (time.time() - self._t0)))

    # ── 2. Setup ──────────────────────────────────────────────────
    def _clear(self):
        self._booting = False
        for w in self.frame.winfo_children():
            w.destroy()

    def _screen_setup(self, start_from):
        self._clear()
        self.screen = "setup"
        self._draw_languages(visible=False)     # no language change halfway through
        T = self.T
        self._label(self.frame, "NGO SETUP UTILITY", PINK, 18, weight="bold").pack(anchor="w")
        self._label(self.frame, T("setup_sub"), GRAY, 11).pack(anchor="w", pady=(2, 14))

        py = _venv_python()
        has_lock = os.path.exists(install.LOCK_FILE)
        self.steps = []
        if start_from == "all":
            self.steps.append((T("step_venv"), self._step_venv))
        if start_from in ("all", "deps"):
            self.steps.append((T("step_pip"), lambda: self._pip(["install", "--upgrade", "pip"])))
            if has_lock:
                self.steps.append((T("step_lock"), self._step_lock))
            self.steps += [
                (T("step_base"), lambda: self._pip(["install"] + install.DEPENDENCIES)),
                (T("step_torch"), lambda: self._pip(
                    ["install", "torch", "--index-url", install.TORCH_CPU_INDEX])),
                (T("step_st"), self._step_st),
            ]
        self.steps += [
            (T("step_model"), self._step_model),
            (T("step_final"), lambda: self._step_final(py)),
        ]

        self.rows = []
        listing = tk.Frame(self.frame, bg=BLACK)
        listing.pack(anchor="w", fill="x")
        for name, _ in self.steps:
            row = tk.Frame(listing, bg=BLACK)
            row.pack(anchor="w", fill="x")
            status = self._label(row, "[    ]", GRAY, 12)
            status.pack(side="left")
            self._label(row, " " + name, GRAY, 12).pack(side="left")
            self.rows.append(status)

        tk.Frame(self.frame, bg=BLACK, height=14).pack()
        self.txt_step = self._label(self.frame, "", CYAN, 11)
        self.txt_step.pack(anchor="w", fill="x")
        self.bar_step = ttk.Progressbar(self.frame, style="Step.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.bar_step.pack(fill="x", pady=(2, 10))
        self.txt_total = self._label(self.frame, "", PINK, 11)
        self.txt_total.pack(anchor="w")
        self.bar_total = ttk.Progressbar(self.frame, style="Total.Horizontal.TProgressbar",
                                         mode="determinate", maximum=len(self.steps) * 100)
        self.bar_total.pack(fill="x", pady=(2, 10))
        self.txt_log = self._label(self.frame, "", "#707070", 9)
        self.txt_log.pack(anchor="w", fill="x")
        self.button_zone = tk.Frame(self.frame, bg=BLACK)
        self.button_zone.pack(anchor="w", pady=(8, 0))

        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_INSTALL, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        self._i = 0
        for i, (name, fn) in enumerate(self.steps):
            if self.closing:
                return
            self._i = i
            self.ui(self.rows[i].configure, {"text": "[ .. ]", "fg": CYAN})
            self.ui(self.txt_step.configure, {"text": name})
            self._progress(0, indeterminate=True)
            try:
                ok = fn()
            except Exception:
                self._log_install(traceback.format_exc())
                ok = False
            if self.closing:
                return
            if not ok:
                self.ui(self.rows[i].configure, {"text": "[FAIL]", "fg": RED})
                self.ui(self._setup_failed, name)
                return
            self.ui(self.rows[i].configure, {"text": "[ OK ]", "fg": GREEN})
            self._progress(100)
        self.ui(self.txt_step.configure, {"text": self.T("done")})
        time.sleep(0.3 if SMOKE else 0.8)
        self.ui(self._screen_launch)

    def _progress(self, pct, indeterminate=False):
        def apply():
            if indeterminate:
                if str(self.bar_step["mode"]) != "indeterminate":
                    self.bar_step.configure(mode="indeterminate")
                    self.bar_step.start(12)
            else:
                self.bar_step.stop()
                self.bar_step.configure(mode="determinate", value=pct)
            total = self._i * 100 + (0 if indeterminate else pct)
            self.bar_total.configure(value=total)
            self.txt_total.configure(text=self.T("total", p=int(total / len(self.steps)),
                                                 i=self._i + 1, n=len(self.steps)))
        self.ui(apply)

    def _log_install(self, text):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_INSTALL, "a", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    def _run_with_progress(self, cmd) -> bool:
        """Runs cmd, writes everything to the log and turns the output into
        the progress bar:
        - pip --progress-bar raw:  'Progress 1234 of 5678'
        - tqdm bars (model download): '  45%|'
        - 'Downloading package (12 MB)' / 'Collecting x' → the step's text."""
        p = _popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONUNBUFFERED="1",
                                                      PYTHONIOENCODING="utf-8"))
        buf = b""
        while True:
            chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for line in parts:
                self._parse_line(line.decode("utf-8", "replace").strip())
            if self.closing:
                _kill_tree(p)
                return False
        if buf:
            self._parse_line(buf.decode("utf-8", "replace").strip())
        p.wait()
        with _children_lock:
            _children.discard(p)
        return p.returncode == 0

    def _parse_line(self, line):
        if not line:
            return
        m = re.match(r"Progress (\d+) of (\d+)", line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total:
                self._progress(min(100, done * 100 // total))
            return
        self._log_install(line)
        m = re.search(r"(\d{1,3})%\|", line)
        if m:
            self._progress(int(m.group(1)))
            return
        m = re.match(r"(Downloading|Collecting|Installing collected packages|Successfully installed)\s*(.*)", line)
        if m:
            verb = self.T({"Downloading": "pip_Downloading", "Collecting": "pip_Collecting",
                           "Installing collected packages": "pip_Installing",
                           "Successfully installed": "pip_Installed"}[m.group(1)])
            self.ui(self.txt_log.configure, {"text": (verb + " " + m.group(2))[:110]})
            if m.group(1).startswith("Installing"):
                self._progress(0, indeterminate=True)

    _raw = None      # does this pip support --progress-bar raw? (pip >= 24.1)

    def _pip(self, args) -> bool:
        cmd = [_venv_python(), "-m", "pip"] + args + ["--disable-pip-version-check"]
        if args[:1] == ["install"] and "--upgrade" not in args:
            if Launcher._raw is None:
                help_text = _run_short("import subprocess,sys;print(subprocess.run([sys.executable,'-m','pip',"
                                       "'install','--help'],capture_output=True,text=True).stdout)")
                Launcher._raw = "raw" in help_text
            if Launcher._raw:
                cmd += ["--progress-bar", "raw"]
        return self._run_with_progress(cmd)

    # steps
    def _step_venv(self) -> bool:
        import venv
        self.ui(self.txt_log.configure, {"text": install.VENV_DIR})
        venv.create(install.VENV_DIR, with_pip=True)
        return os.path.exists(_venv_python())

    def _step_lock(self) -> bool:
        ok = self._pip(["install", "-r", install.LOCK_FILE,
                        "--extra-index-url", install.TORCH_CPU_INDEX])
        if ok and install.deps_ok(_venv_python())[0]:
            # the lock already brings everything: the next steps are quick (already installed)
            return True
        if ok:
            # a lock with conflicts: set it aside and reinstall
            os.replace(install.LOCK_FILE, install.LOCK_FILE + ".broken")
        self._log_install(i18n.t("c_lock_bad", "en"))
        return True

    def _step_st(self) -> bool:
        # With its dependencies and torch pinned (see install.st_install_cmd).
        cmd = install.st_install_cmd(_venv_python())
        if not self._pip(cmd[3:]):
            return False
        ok, detail = install.deps_ok(_venv_python())
        if not ok:
            self._log_install("pip check:\n" + detail)
        return ok

    def _step_model(self) -> bool:
        if "MODEL=yes" in _run_short(_CHECK_MODEL, timeout=60):
            return True
        return self._run_with_progress([_venv_python(), "-c", _DOWNLOAD_MODEL])

    def _step_final(self, py) -> bool:
        if not os.path.exists(install.LOCK_FILE):
            install.write_lock(py)
        if not os.path.exists(os.path.join(APP_DIR, "images", "favicon.png")):
            install.generate_ribbon_icon(py)
        return True

    def _setup_failed(self, name):
        self.bar_step.stop()
        self.bar_step.configure(mode="determinate", value=0)
        self.txt_step.configure(text=self.T("failed", step=name), fg=RED)
        self.txt_log.configure(text=self.T("failed_hint"), fg=GRAY)
        self._draw_languages()
        for w in self.button_zone.winfo_children():
            w.destroy()
        self._button(self.button_zone, self.T("btn_retry"), self._retry, CYAN, 12).pack(side="left")
        self._button(self.button_zone, self.T("btn_log"), lambda: _open_file(LOG_INSTALL),
                     GRAY, 12).pack(side="left", padx=12)
        if SMOKE:
            self._after(300, self.quit_app)

    def _retry(self):
        self._clear()
        self._screen_boot()

    # ── 3. Launch ─────────────────────────────────────────────────
    def _screen_launch(self, restore=False):
        self._clear()
        self.screen = "launch"
        self._draw_languages()
        self.footer_note.configure(text=self.T("footer"))
        center = tk.Frame(self.frame, bg=BLACK)
        center.place(relx=0.5, rely=0.45, anchor="center")
        self._label(center, "KAngel", PINK, 44, weight="italic").pack()
        self._label(center, "INTERNET YAMERO", CYAN, 12).pack(pady=(0, 30))
        self.btn_launch = self._button(center, f"  {self.T('btn_launch')}  ", self.launch, PINK, 22)
        self.btn_launch.pack()
        self.txt_status = self._label(center, "", GRAY, 11)
        self.txt_status.configure(anchor="center", justify="center")
        self.txt_status.pack(pady=(22, 6))
        self.server_zone = tk.Frame(center, bg=BLACK)
        self.server_zone.pack()
        self.remote_toggle = self._label(center, "", GRAY, 11)
        self.remote_toggle.configure(cursor="hand2")
        self.remote_toggle.pack(pady=(18, 4))
        self.remote_toggle.bind("<Button-1>", lambda e: self._toggle_remote())
        self.remote_zone = tk.Frame(center, bg=BLACK)
        self.remote_zone.pack()
        self._draw_remote()
        if restore and self._online is not None:
            self._open_when_ready = False
            self._ready(own=self._online)
            return
        if AUTOLAUNCH:
            self._after(300, self.launch)

    # ── Remote access ─────────────────────────────────────────────
    def _draw_remote(self):
        key = _remote_key()
        self.remote_toggle.configure(text=self.T("remote_on" if key else "remote_off"),
                                    fg=CYAN if key else GRAY)
        for w in self.remote_zone.winfo_children():
            w.destroy()
        if not (key and self._online):
            return
        ip = _lan_ip()
        if not ip:
            self._label(self.remote_zone, self.T("remote_no_ip"), RED, 10).pack()
            return

        url = f"http://{ip}:{PORT}"
        row = tk.Frame(self.remote_zone, bg=BLACK)
        row.pack()
        left = tk.Frame(row, bg=BLACK)
        left.pack(side="left", padx=(0, 18))

        def field(title, text, color):
            """A read-only field; one click copies it to the clipboard."""
            head = self._label(left, f"{title.rstrip(':')} — {self.T('remote_click_copy')}", GRAY, 10)
            head.pack(pady=(6, 0))
            e = tk.Entry(left, width=34, justify="center", relief="flat",
                         bg=DARK, fg=color, readonlybackground=DARK,
                         font=(_mono_font(self.root), 12), cursor="hand2")
            e.insert(0, text)
            e.configure(state="readonly")
            e.pack(pady=2, ipady=3)

            def copy(_ev=None):
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                head.configure(text=f"{title.rstrip(':')} — {self.T('remote_copied')}", fg=GREEN)
                self._after(1500, lambda: head.winfo_exists() and head.configure(
                    text=f"{title.rstrip(':')} — {self.T('remote_click_copy')}", fg=GRAY))
            e.bind("<Button-1>", copy)

        field(self.T("remote_address"), url, GREEN)
        field(self.T("remote_key"), key, PINK)

        # QR with the login link: the server accepts /?key=… once, sets the
        # device cookie and cleans the address bar (main._remote_guard).
        right = tk.Frame(row, bg=BLACK)
        right.pack(side="left")
        canvas = self._qr_canvas(right, f"{url}/?key={key}", 150)
        if canvas is not None:
            canvas.pack()
            cap = self._label(right, self.T("remote_qr"), GRAY, 9)
            cap.configure(justify="center", anchor="center")
            cap.pack(pady=(4, 0))

        hint = self._label(self.remote_zone, self.T("remote_hint"), GRAY, 9)
        hint.configure(wraplength=560, justify="center")
        hint.pack(pady=(8, 0))

    def _qr_canvas(self, parent, data: str, size: int):
        """A QR code drawn on a Tk canvas (qrcodegen: pure Python, no
        dependencies, so the launcher still runs on a bare Python)."""
        try:
            from qrcodegen import QrCode
            qr = QrCode.encode_text(data, QrCode.Ecc.MEDIUM)
        except Exception as e:                    # never break the launcher
            print(f"[launcher] QR not available: {e}")
            return None
        quiet = 2                                 # white border, needed to scan
        n = qr.get_size() + 2 * quiet
        cell = max(2, size // n)
        side = cell * n
        c = tk.Canvas(parent, width=side, height=side, bg="#ffffff",
                      highlightthickness=0, bd=0)
        for y in range(qr.get_size()):
            for x in range(qr.get_size()):
                if qr.get_module(x, y):
                    x0, y0 = (x + quiet) * cell, (y + quiet) * cell
                    c.create_rectangle(x0, y0, x0 + cell, y0 + cell,
                                       fill="#000000", width=0)
        return c

    def _toggle_remote(self):
        _set_remote(not _remote_key())
        self._draw_remote()
        if self._online:            # our own server: restart so it binds again
            self.txt_status.configure(text=self.T("remote_applying"), fg=CYAN)
            self.restart()

    def launch(self):
        self.btn_launch.unbind("<Button-1>")
        self.btn_launch.configure(fg="#606060", bg=BLACK, cursor="arrow")
        if _port_in_use():
            if _ame_is_running():
                self.txt_status.configure(text=self.T("already", url=URL), fg=GREEN)
                self._ready(own=False)
            else:
                self.txt_status.configure(text=self.T("port_busy", port=PORT), fg=RED)
                self._rearm_button()
            return
        self.txt_status.configure(text=self.T("starting"), fg=CYAN)
        threading.Thread(target=self._start_server, daemon=True).start()

    def _rearm_button(self):
        self.btn_launch.configure(fg=PINK, cursor="hand2")
        self.btn_launch.bind("<Button-1>", lambda e: self.launch())

    def _start_server(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        log = open(LOG_SERVER, "a", encoding="utf-8")
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        log.flush()
        key = _remote_key()
        cmd = [_venv_python(), "-m", "uvicorn", "main:app",
               "--reload", "--reload-include", "character.md", "--reload-include", "config.md",
               "--host", "0.0.0.0" if key else HOST, "--port", str(PORT),
               "--log-level", "warning"]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", AME_LAUNCHER_PID=str(os.getpid()))
        env.pop("AME_ACCESS_KEY", None)
        if key:
            env["AME_ACCESS_KEY"] = key
        self.server = _popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             env=env)
        deadline = time.time() + 240        # the first time it loads the model: it can take a while
        while time.time() < deadline and not self.closing:
            if self.server.poll() is not None:
                self.ui(self._server_died)
                return
            if _ame_is_running():
                self.ui(self._ready, True)
                return
            time.sleep(0.5)
        if not self.closing:
            self.ui(self._server_died)

    _open_when_ready = True

    def _ready(self, own=True):
        self._online = own
        self.txt_status.configure(text=self.T("awake" if own else "already", url=URL), fg=GREEN)
        self.btn_launch.unbind("<Button-1>")
        self.btn_launch.unbind("<Enter>")
        self.btn_launch.unbind("<Leave>")
        self.btn_launch.configure(text=f"[  {self.T('btn_online')}  ]", fg=GREEN, bg=BLACK, cursor="arrow")
        for w in self.server_zone.winfo_children():
            w.destroy()
        self._button(self.server_zone, self.T("btn_open"), self.open_browser, CYAN, 12).pack(side="left", padx=8)
        self._button(self.server_zone, self.T("btn_sprites"),
                     lambda: None if NO_BROWSER else webbrowser.open(URL + "/sprites"),
                     PINK, 12).pack(side="left", padx=8)
        if own:
            self._button(self.server_zone, self.T("btn_restart"), self.restart, GRAY, 12).pack(side="left", padx=8)
            self._button(self.server_zone, self.T("btn_log"), lambda: _open_file(LOG_SERVER),
                         GRAY, 12).pack(side="left", padx=8)
        self._draw_remote()
        if self._open_when_ready:
            self.open_browser()
        self._open_when_ready = False           # after RESTART, no new tab
        if SMOKE:
            self._after(800, self.quit_app)

    def _server_died(self):
        self._online = None
        self._draw_remote()
        tail = ""
        try:
            with open(LOG_SERVER, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-6:])
        except OSError:
            pass
        self.txt_status.configure(text=self.T("start_failed") + "\n\n" + tail[-500:], fg=RED)
        self.btn_launch.configure(text=f"[  {self.T('btn_launch')}  ]")
        self._rearm_button()
        if SMOKE:
            self._after(300, self.quit_app)

    def open_browser(self):
        if not NO_BROWSER:
            webbrowser.open(URL)

    def restart(self):
        """For when you change something that doesn't reload on its own."""
        self._online = None
        self._draw_remote()
        self.txt_status.configure(text=self.T("restarting"), fg=CYAN)
        for w in self.server_zone.winfo_children():
            w.destroy()

        def worker():
            _kill_tree(self.server)
            t = time.time() + 10
            while _port_in_use() and time.time() < t:
                time.sleep(0.3)
            self._start_server()
        threading.Thread(target=worker, daemon=True).start()

    # ── 4. Errors and exit ────────────────────────────────────────
    def _error_if_current(self, gen, message):
        if gen == self._gen and self.screen == "boot":
            self._error(message)

    def _error(self, message):
        self._booting = False
        self.boot_footer.configure(text=message, fg=RED)
        if SMOKE:
            self._after(300, self.quit_app)

    def quit_app(self):
        if self.closing:
            return
        self.closing = True
        for a in self._after_ids:
            try:
                self.root.after_cancel(a)
            except Exception:
                pass
        _kill_all()
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass


def _open_file(path):
    if not os.path.exists(path):
        return
    if WIN:
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def main():
    app = None
    try:
        root = tk.Tk()
        app = Launcher(root)
        root.mainloop()
    except Exception:
        # With pythonw there's no console: without this, an error on startup would be invisible.
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "launcher_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{traceback.format_exc()}")
        raise
    finally:
        if app is not None:
            app.closing = True
        _kill_all()


if __name__ == "__main__":
    main()
