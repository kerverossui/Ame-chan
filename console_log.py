"""
console_log.py — keeps the server's console output for the 🖥️ Console panel
(GET /console).

The launcher runs the server without a console window, so this tees stdout
and stderr: everything still goes where it went (the log file or the
terminal), and the last MAX_LINES lines stay in memory for the panel. API
keys are masked. Remote devices only see it with the access key.
"""

import collections
import io
import re
import sys
import threading
import time

MAX_LINES = 2000

_lines: collections.deque = collections.deque(maxlen=MAX_LINES)
_lock = threading.Lock()
_next_id = 1
_installed = False

# Strip API keys if any message ever echoes one (an error body, a URL…).
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|xai-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)")


def _add(text: str, stream: str) -> None:
    global _next_id
    text = _SECRET_RE.sub("[hidden]", text.rstrip("\r"))
    if not text.strip():
        return
    with _lock:
        _lines.append({"id": _next_id, "ts": time.time(), "stream": stream, "text": text})
        _next_id += 1


class _Tee(io.TextIOBase):
    def __init__(self, original, stream_name: str):
        self._original = original
        self._name = stream_name
        self._partial = ""

    def write(self, s):
        try:
            if self._original is not None:
                self._original.write(s)
        except Exception:
            pass
        self._partial += s
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            _add(line, self._name)
        return len(s)

    def flush(self):
        try:
            if self._original is not None:
                self._original.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")


def install() -> None:
    """Idempotent: safe to call more than once (uvicorn --reload re-imports)."""
    global _installed
    if _installed or isinstance(sys.stdout, _Tee):
        _installed = True
        return
    sys.stdout = _Tee(sys.stdout, "out")
    sys.stderr = _Tee(sys.stderr, "err")
    _installed = True


def since(after: int = 0, limit: int = 500) -> dict:
    """Lines with id > after (at most `limit`, the newest ones), plus the
    id to ask from next time."""
    with _lock:
        items = [l for l in _lines if l["id"] > after]
        last = _next_id - 1
    return {"lines": items[-limit:], "last": last}
