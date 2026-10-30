#!/usr/bin/env bash
# Opens the Ame launcher (installs what's missing and starts Ame).
cd "$(dirname "${BASH_SOURCE[0]}")" || { echo "Can't enter the script folder."; exit 1; }

AL="en"; [ -f .installer_lang ] && AL="$(head -c 2 .installer_lang)"
msg() {  # msg "english" "español" "italiano"
    case "$AL" in es) echo "$2" ;; it) echo "$3" ;; *) echo "$1" ;; esac
}

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ] || ! "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    msg "You need Python 3.10 or newer: https://www.python.org/downloads/" \
        "Necesitas Python 3.10 o más nuevo: https://www.python.org/downloads/" \
        "Serve Python 3.10 o più recente: https://www.python.org/downloads/"
    exit 1
fi

if "$PY" -c "import tkinter" 2>/dev/null; then
    exec "$PY" ame_launcher.py
fi

# No Tkinter (minimal Linux): plain console mode.
msg "Your Python has no Tkinter (Ubuntu/Debian: sudo apt install python3-tk). Using console mode." \
    "Tu Python no trae Tkinter (Ubuntu/Debian: sudo apt install python3-tk). Sigo en modo consola." \
    "Il tuo Python non ha Tkinter (Ubuntu/Debian: sudo apt install python3-tk). Uso la modalità console."
[ -f venv/bin/activate ] || "$PY" install.py --auto || exit 1
source venv/bin/activate
msg "Open http://127.0.0.1:8000 in your browser." \
    "Abre http://127.0.0.1:8000 en tu navegador." \
    "Apri http://127.0.0.1:8000 nel browser."
exec uvicorn main:app --reload --reload-include character.md --reload-include config.md --host "${AME_HOST:-127.0.0.1}" --port "${AME_PORT:-8000}" --log-level warning
