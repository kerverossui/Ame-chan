"""
canon.py — Ame's memory from before the app (the game/anime and its people).

Kept apart from character.md (who Ame is) and from the living memory of the
story (living_memory.py). It lives in canon.md, which you edit.

Proper names need exact lookup: similarity search can miss a person buried
deep in a long text, and the model fills the gaps with whatever it vaguely
remembers of the anime.

How it works:
  · "## ALWAYS"  → every turn, inside the cached prompt prefix (cheap).
  · "## <Name>"  → the full entry ONLY when the name or one of its "keys:"
                   appears in the current message or the last few messages.
                   For proper names this is exact, unlike similarity search.
  · Re-read automatically when canon.md changes (no server restart).

Older files written in Spanish ("## SIEMPRE", "claves:") are still accepted.
"""

import os
import re
import threading
import unicodedata

from app_config import BASE_DIR

CANON_PATH = os.path.join(BASE_DIR, "canon.md")
ENTRY_BUDGET = 9000      # characters of canon entries per turn (~2.3k tokens)
CONTEXT_MESSAGES = 4     # how many recent messages can also activate an entry

_ALWAYS_HEADINGS = ("always", "siempre")
_KEY_PREFIXES = ("keys:", "claves:")

_lock = threading.Lock()
_cache = {"mtime": None, "always": "", "entries": []}


def _norm(text: str) -> str:
    """Lowercase and strip accents, so "Nechíka" matches "nechika"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def pattern_for(keys) -> re.Pattern:
    """Regex matching any of the keys as a whole word, on text already
    normalized with _norm (no accents, lowercase)."""
    keys = sorted({_norm(k).strip() for k in keys if k and k.strip()}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)")


def _parse(text: str) -> tuple[str, list[dict]]:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    sections = re.split(r"(?m)^##\s+", text)
    always, entries = "", []
    for section in sections[1:]:                  # sections[0] is the title
        name, _, body = section.partition("\n")
        name = name.strip()
        lines = body.strip().splitlines()
        keys = {_norm(name)}
        if lines and lines[0].lower().startswith(_KEY_PREFIXES):
            keys |= {_norm(k.strip()) for k in lines[0].split(":", 1)[1].split(",") if k.strip()}
            lines = lines[1:]
        body = "\n".join(lines).strip()
        if not body:
            continue
        if _norm(name) in _ALWAYS_HEADINGS:
            always = body
            continue
        entries.append({"name": name, "body": body, "pattern": pattern_for(keys)})
    return always, entries


def _load() -> dict:
    try:
        mtime = os.path.getmtime(CANON_PATH)
    except OSError:
        return {"mtime": None, "always": "", "entries": []}
    with _lock:
        if _cache["mtime"] != mtime:
            try:
                with open(CANON_PATH, encoding="utf-8") as f:
                    always, entries = _parse(f.read())
                _cache.update(mtime=mtime, always=always, entries=entries)
                print(f"[canon] canon.md loaded: {len(entries)} entries")
            except Exception as e:                  # a broken canon must not break the app
                print(f"[canon] couldn't read canon.md ({e}); keeping the previous version")
        return dict(_cache)


def always_block() -> str:
    """Fixed text for the cached prefix ('' when there is no canon.md)."""
    text = _load()["always"]
    if not text:
        return ""
    return "\n\n═══ HER PEOPLE — CANON FROM BEFORE THE APP ═══\n" + text + "\n"


def active_entries(current_message: str, recent: list[str]) -> list[dict]:
    """Entries whose name/key appears in the current message (first) or in
    the last few messages. Respects ENTRY_BUDGET."""
    entries = _load()["entries"]
    if not entries:
        return []
    now = _norm(current_message or "")
    context = _norm("\n".join(recent[-CONTEXT_MESSAGES:]))
    chosen = [e for e in entries if e["pattern"].search(now)]
    chosen += [e for e in entries if e not in chosen and e["pattern"].search(context)]
    result, used = [], 0
    for e in chosen:
        if used + len(e["body"]) > ENTRY_BUDGET and result:
            break
        result.append(e)
        used += len(e["body"])
    return result


def entries_block(current_message: str, recent: list[str]) -> str:
    entries = active_entries(current_message, recent)
    if not entries:
        return ""
    print("[canon] entries this turn: " + ", ".join(e["name"] for e in entries))
    return ("\n═══ WHAT YOU REMEMBER ABOUT THEM (canon: use it as written, do not invent "
            "details that are not here) ═══\n"
            + "\n\n".join(f"【{e['name']}】\n{e['body']}" for e in entries) + "\n")
