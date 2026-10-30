#!/usr/bin/env python3
"""
Installer for Ame Companion App (the launcher runs the same steps).

Usage:
    python3 install.py

1. Creates the virtual environment (venv/)
2. Installs the dependencies inside it
3. Makes sure static/index.html is where the backend expects it
4. Optionally adds a desktop shortcut and starts the server

Your name, the AI engine and its API key are set in the browser, in the
welcome wizard, the first time you open the app.
"""

import os
import subprocess
import sys
import venv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from installer_i18n import t  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(APP_DIR, "venv")
STATIC_DIR = os.path.join(APP_DIR, "static")
LOCK_FILE = os.path.join(APP_DIR, "requirements.lock")
AUTO = "--auto" in sys.argv   # no-questions mode (used by start.bat/start.sh)

DEPENDENCIES = [
    "fastapi",
    "uvicorn",
    # watchfiles lets uvicorn's --reload-include pick up changes to
    # character.md and config.md; without it only .py files are watched.
    "watchfiles",
    "numpy",
    "requests",
    "python-multipart",
]

# CPU-only torch: without this, pip pulls the full CUDA build by
# default, which is ~6GB of NVIDIA packages you don't need without a GPU.
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print("\n" + t("c_cmd_failed", cmd=" ".join(cmd)))
        sys.exit(1)


def ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    if AUTO:
        return False      # no questions: no shortcut and no server start here
    suffix = t("c_yes_no") if default_yes else t("c_no_yes")
    raw = input(f"{prompt} {suffix}: ").strip().lower()
    if not raw:
        return default_yes
    return raw.startswith(("s", "y"))


# ────────────────────────────────────────────────────────────────
# 1. Virtual environment + dependencies
# ────────────────────────────────────────────────────────────────

def setup_venv_and_deps():
    if not os.path.exists(VENV_DIR):
        print(t("c_creating_venv"))
        venv.create(VENV_DIR, with_pip=True)
    else:
        print(t("c_venv_exists"))

    if os.name == "nt":
        python_path = os.path.join(VENV_DIR, "Scripts", "python.exe")
    else:
        python_path = os.path.join(VENV_DIR, "bin", "python")

    run([python_path, "-m", "pip", "install", "--upgrade", "pip"])

    # Pinned versions: if requirements.lock exists, exactly what worked last
    # time is installed, so an update of a dependency can't break the app. If the
    # lock fails (a different Python version, say), it falls back to the normal
    # install with a warning.
    if os.path.exists(LOCK_FILE):
        print("\n" + t("c_lock") + "\n")
        r = subprocess.run([python_path, "-m", "pip", "install", "-r", LOCK_FILE,
                            "--extra-index-url", TORCH_CPU_INDEX])
        if r.returncode == 0 and deps_ok(python_path)[0]:
            return python_path
        if r.returncode == 0:
            # The lock holds an environment with conflicts: set it aside and
            # reinstall.
            os.replace(LOCK_FILE, LOCK_FILE + ".broken")
        print("\n⚠ requirements.lock could not be installed as-is; "
              "falling back to the latest versions.\n")

    print("\n" + t("c_base") + "\n")
    run([python_path, "-m", "pip", "install"] + DEPENDENCIES)

    print("\n" + t("c_torch") + "\n")
    run([python_path, "-m", "pip", "install", "torch", "--index-url", TORCH_CPU_INDEX])

    print("\n" + t("c_st") + "\n")
    run(st_install_cmd(python_path))
    ok, detail = deps_ok(python_path)
    if not ok:
        print("\n" + t("c_conflict") + "\n" + detail)
    write_lock(python_path)
    return python_path


def torch_pin(python_path: str) -> str | None:
    """Exact version of the installed torch (e.g. '2.14.1+cpu')."""
    r = subprocess.run([python_path, "-c", "import torch; print(torch.__version__)"],
                       capture_output=True, text=True)
    v = (r.stdout or "").strip()
    return f"torch=={v}" if r.returncode == 0 and v else None


def st_install_cmd(python_path: str) -> list:
    """sentence-transformers with its dependencies, keeping the installed
    CPU torch pinned so pip doesn't pull the CUDA build (about 6 GB)."""
    cmd = [python_path, "-m", "pip", "install", "sentence-transformers", "Pillow"]
    pin = torch_pin(python_path)
    if pin:
        cmd.append(pin)
    return cmd + ["--extra-index-url", TORCH_CPU_INDEX]


def deps_ok(python_path: str) -> tuple[bool, str]:
    """`pip check`: True if no packages have incompatible versions."""
    r = subprocess.run([python_path, "-m", "pip", "check"], capture_output=True, text=True)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()


def write_lock(python_path: str):
    """Saves the exact versions that ended up installed. The next install
    (another PC, deleted venv) reproduces this same environment."""
    ok, detail = deps_ok(python_path)
    if not ok:
        # Never pin a broken environment: the next install would copy it.
        print(t("c_lock_skip"))
        return
    r = subprocess.run([python_path, "-m", "pip", "freeze"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write("# Generated by install.py — versions that worked.\n"
                    "# Delete it to install the latest versions.\n" + r.stdout)
        print(t("c_lock_saved", f=os.path.basename(LOCK_FILE)))


# ────────────────────────────────────────────────────────────────
# 2. static/index.html in the right place
# ────────────────────────────────────────────────────────────────

def ensure_static_index():
    """The backend serves the UI from static/index.html; if it arrived at
    the project root, move it there."""
    os.makedirs(STATIC_DIR, exist_ok=True)
    root_index = os.path.join(APP_DIR, "index.html")
    static_index = os.path.join(STATIC_DIR, "index.html")

    if os.path.exists(static_index):
        print(t("c_static_ok"))
        return
    if os.path.exists(root_index):
        print(t("c_static_move"))
        os.replace(root_index, static_index)
        return
    print("\n" + t("c_static_missing"))


# ────────────────────────────────────────────────────────────────
# 3. Ribbon icon (drawn with Pillow, which lives in the venv, so it runs
#    with the venv's Python)
# ────────────────────────────────────────────────────────────────

_ICON_GEN_SCRIPT = r'''
import sys
from PIL import Image, ImageDraw

png_path, ico_path = sys.argv[1], sys.argv[2]
size = 256
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

pink = (232, 139, 171, 255)
dark_pink = (196, 84, 124, 255)
cx, cy = size // 2, size // 2
wing = size // 2 - 24

# Bow wings (two triangles meeting at the center knot)
d.polygon(
    [(cx - 8, cy), (cx - wing, cy - wing // 2), (cx - wing, cy + wing // 2)],
    fill=pink, outline=dark_pink, width=6,
)
d.polygon(
    [(cx + 8, cy), (cx + wing, cy - wing // 2), (cx + wing, cy + wing // 2)],
    fill=pink, outline=dark_pink, width=6,
)
# Trailing ribbon tails
d.polygon([(cx - 14, cy + 6), (cx - 30, cy + 60), (cx - 4, cy + 46)], fill=dark_pink)
d.polygon([(cx + 14, cy + 6), (cx + 30, cy + 60), (cx + 4, cy + 46)], fill=dark_pink)
# Center knot
knot_r = 26
d.ellipse([cx - knot_r, cy - knot_r, cx + knot_r, cy + knot_r], fill=dark_pink)

img.save(png_path)
img.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
'''


def generate_ribbon_icon(python_path: str) -> tuple[str, str] | None:
    """Draws a ribbon icon as .png (Linux shortcut, favicon) and .ico
    (Windows shortcut, favicon.ico) in images/."""
    images_dir = os.path.join(APP_DIR, "images")
    os.makedirs(images_dir, exist_ok=True)
    png_path = os.path.join(images_dir, "favicon.png")
    ico_path = os.path.join(images_dir, "favicon.ico")
    gen_script_path = os.path.join(APP_DIR, "_gen_icon_tmp.py")
    try:
        with open(gen_script_path, "w", encoding="utf-8") as f:
            f.write(_ICON_GEN_SCRIPT)
        result = subprocess.run(
            [python_path, gen_script_path, png_path, ico_path],
            capture_output=True, text=True,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            print(t("c_icon_fail"))
            print("   " + (result.stderr or "").strip()[:300])
            return None
        print(t("c_icon_saved", a=png_path, b=ico_path))
        return png_path, ico_path
    finally:
        if os.path.exists(gen_script_path):
            os.remove(gen_script_path)


# ────────────────────────────────────────────────────────────────
# 4. Desktop shortcut → http://localhost:8000
# ────────────────────────────────────────────────────────────────

APP_URL = "http://127.0.0.1:8000"


def _desktop_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Desktop")


def create_desktop_shortcut(icon_paths: tuple[str, str] | None):
    """A desktop shortcut to the running app (a plain internet-shortcut file,
    no extra dependencies)."""
    desktop = _desktop_dir()
    if not os.path.isdir(desktop):
        print(t("c_no_desktop", d=desktop))
        return

    png_path, ico_path = icon_paths if icon_paths else (None, None)

    if os.name == "nt":
        # Windows: .url internet shortcut, with the .ico as its icon.
        path = os.path.join(desktop, "Ame.url")
        lines = ["[InternetShortcut]", f"URL={APP_URL}"]
        if ico_path and os.path.exists(ico_path):
            lines += [f"IconFile={ico_path}", "IconIndex=0"]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(t("c_shortcut", p=path))

    elif sys.platform == "darwin":
        # macOS: .webloc web link (a custom icon would need a full .app).
        path = os.path.join(desktop, "Ame.webloc")
        content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n\t<key>URL</key>\n'
            f'\t<string>{APP_URL}</string>\n</dict>\n</plist>\n'
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(t("c_shortcut", p=path))

    else:
        # Linux: .desktop launcher, with the .png as its icon.
        path = os.path.join(desktop, "Ame.desktop")
        lines = [
            "[Desktop Entry]", "Version=1.0", "Type=Application",
            "Name=Ame", "Comment=Open Ame Companion App",
            f"Exec=xdg-open {APP_URL}", "Terminal=false", "Categories=Network;",
        ]
        if png_path and os.path.exists(png_path):
            lines.append(f"Icon={png_path}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(path, 0o755)
        print(t("c_shortcut", p=path))
        print(t("c_shortcut_linux"))


# ────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────

def main():
    print(t("c_title") + "\n")

    main_py = os.path.join(APP_DIR, "main.py")
    if not os.path.exists(main_py):
        print(t("c_no_main"))
        sys.exit(1)

    python_path = setup_venv_and_deps()
    ensure_static_index()

    if ask_yes_no("\n" + t("c_ask_shortcut"), default_yes=True):
        icon_paths = generate_ribbon_icon(python_path)
        create_desktop_shortcut(icon_paths)

    print("\n" + t("c_complete") + "\n")
    print(t("c_next") + "\n")

    if os.name == "nt":
        print(t("c_next_1", s="start.bat"))
    else:
        print(t("c_next_1", s="./start.sh"))

    print("\n" + t("c_next_2", url=APP_URL))
    print("\n" + t("c_next_3"))

    if ask_yes_no("\n" + t("c_ask_start"), default_yes=False):
        # Same flags as start.bat / start.sh.
        run([python_path, "-m", "uvicorn", "main:app", "--reload",
             "--reload-include", "character.md", "--reload-include", "config.md",
             # AME_HOST=0.0.0.0 (+ AME_ACCESS_KEY) for remote access / a server
             "--host", os.environ.get("AME_HOST", "127.0.0.1"),
             "--port", os.environ.get("AME_PORT", "8000")])


if __name__ == "__main__":
    main()
