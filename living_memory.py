"""
living_memory.py — Ame's living memory: what happens in this story, per person.

Three layers, each with a different owner:
  · character.md  → who Ame is (you edit it).
  · canon.md      → her official past: game + anime eps 1–12. Fixed.
  · memories/     → what happened AFTERWARDS, in the app. One file per person
                    (michica.md, kache.md, us.md…). The app writes them on its
                    own; you can read and correct them as plain text.

How it grows (memories that get updated, not a history that is re-read in
full):
  1. Every CONSOLIDATE_EVERY new messages, in the background, a cheap model
     reads those messages plus the files of the people who appear in them.
  2. It returns JSON operations: add a fact, replace one that changed,
     delete one that is no longer true, or create a file for a new person.
  3. They are applied with validation. The canon is never touched: a fact
     that is already canon is not repeated here.
  4. A file longer than ENTRY_CAP characters is condensed (keeping the
     concrete facts).

How it is used every turn:
  · us.md (the relationship between Ame and the user) is always included.
  · Any other file is included only when someone names that person, right
    below their canon entry: the official version first, then what happened
    with them in this story.

Compatibility with installs from before the English rename: the old folder
memoria/ is moved to memories/, nosotros.md becomes us.md, and files or
model replies using the Spanish keywords are still understood.
"""

import json
import os
import re
import threading
import time

import canon
import database as db
from app_config import BASE_DIR, LANGUAGE_LABELS
from llm import call_llm

MEMORY_DIR = os.path.join(BASE_DIR, "memories")
STATE_PATH = os.path.join(MEMORY_DIR, ".state.json")
ALWAYS_ENTRY = "us"               # memories/us.md
CONSOLIDATE_EVERY = 8             # new messages before consolidating
ENTRY_CAP = 3500                  # characters; above this a file is condensed
INPUT_CAP = 24000                 # characters of messages per consolidation
TURN_BUDGET = 6000                # characters of living-memory files per turn
AUX_MODEL = {"grok": "grok-3-mini", "qwen": "qwen-turbo",
             "deepseek": "deepseek-v4-flash", "gpt": "gpt-5.6-luna"}

_OLD_DIR = os.path.join(BASE_DIR, "memoria")
_OLD_ALWAYS = "nosotros"
_KEY_PREFIXES = ("keys:", "claves:")
_US_NAMES = ("us", "nosotros", "ame and him", "ame and the user", "ame y el", "relationship", "relacion")

# Operation names and fields: English first, Spanish accepted for old replies.
_ACTIONS = {"add": "add", "agregar": "add", "replace": "replace", "reemplazar": "replace",
            "delete": "delete", "borrar": "delete", "create": "create", "crear": "create"}

_lock = threading.Lock()            # one consolidation at a time
_cache = {"signature": None, "entries": {}}


def _migrate_old_layout():
    """memoria/ → memories/, nosotros.md → us.md, .estado.json → .state.json."""
    try:
        if os.path.isdir(_OLD_DIR) and not os.path.exists(MEMORY_DIR):
            os.replace(_OLD_DIR, MEMORY_DIR)
        old_us = os.path.join(MEMORY_DIR, _OLD_ALWAYS + ".md")
        new_us = os.path.join(MEMORY_DIR, ALWAYS_ENTRY + ".md")
        if os.path.exists(old_us) and not os.path.exists(new_us):
            os.replace(old_us, new_us)
        old_state = os.path.join(MEMORY_DIR, ".estado.json")
        if os.path.exists(old_state) and not os.path.exists(STATE_PATH):
            with open(old_state, encoding="utf-8") as f:
                state = json.load(f)
            if "ultimo_id" in state:
                state["cursor_id"] = state.pop("ultimo_id")
            _save_state(state)
            os.remove(old_state)
    except (OSError, ValueError) as e:
        print(f"[living-memory] couldn't migrate the old memory folder: {e}")


# ── Files ──────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    s = canon._norm(name)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "unnamed"


def _entry_path(slug: str) -> str:
    return os.path.join(MEMORY_DIR, slug + ".md")


def _read_entry(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    name, keys, facts = None, [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# ") and name is None:
            name = line[2:].strip()
        elif line.lower().startswith(_KEY_PREFIXES):
            keys = [k.strip() for k in line.split(":", 1)[1].split(",") if k.strip()]
        elif line.startswith("- "):
            facts.append(line[2:].strip())
    if not name:
        return None
    slug = os.path.splitext(os.path.basename(path))[0]
    return {"slug": slug, "name": name, "keys": keys, "facts": facts,
            "pattern": canon.pattern_for([name] + keys)}


def _write_entry(entry: dict):
    """Atomic write: a power cut never leaves a half-written file."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    content = (f"# {entry['name']}\n"
               f"keys: {', '.join(entry['keys'])}\n"
               "<!-- Living memory: the app updates it on its own. You can correct it. -->\n\n"
               + "".join(f"- {fact}\n" for fact in entry["facts"]))
    tmp = _entry_path(entry["slug"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, _entry_path(entry["slug"]))


def entries() -> dict:
    """{slug: entry}. Re-read only when some file changed (yours or the app's)."""
    try:
        names = sorted(n for n in os.listdir(MEMORY_DIR) if n.endswith(".md"))
        signature = tuple((n, os.path.getmtime(os.path.join(MEMORY_DIR, n))) for n in names)
    except OSError:
        return {}
    if signature != _cache["signature"]:
        fresh = {}
        for n in names:
            entry = _read_entry(os.path.join(MEMORY_DIR, n))
            if entry:
                fresh[entry["slug"]] = entry
        _cache.update(signature=signature, entries=fresh)
    return _cache["entries"]


# ── Use in the prompt ──────────────────────────────────────────────────────

def _facts_text(entry: dict) -> str:
    return "\n".join(f"- {fact}" for fact in entry["facts"])


def always_block() -> str:
    entry = entries().get(ALWAYS_ENTRY)
    if not entry or not entry["facts"]:
        return ""
    return ("\n═══ YOUR STORY WITH THEM (living memory: what has already happened between you) ═══\n"
            + _facts_text(entry) + "\n")


def active_entries(current_message: str, recent: list[str]) -> list[dict]:
    candidates = [e for s, e in entries().items() if s != ALWAYS_ENTRY and e["facts"]]
    now = canon._norm(current_message or "")
    context = canon._norm("\n".join(recent[-canon.CONTEXT_MESSAGES:]))
    chosen = [e for e in candidates if e["pattern"].search(now)]
    chosen += [e for e in candidates if e not in chosen and e["pattern"].search(context)]
    result, used = [], 0
    for e in chosen:
        size = len(_facts_text(e))
        if used + size > TURN_BUDGET and result:
            break
        result.append(e)
        used += size
    return result


def entries_block(current_message: str, recent: list[str]) -> str:
    active = active_entries(current_message, recent)
    if not active:
        return ""
    print("[living-memory] entries this turn: " + ", ".join(e["name"] for e in active))
    return ("\n═══ WHAT HAS HAPPENED WITH THEM IN THIS STORY (living memory: newer than "
            "the canon; if something changed, this wins) ═══\n"
            + "\n\n".join(f"【{e['name']}】\n{_facts_text(e)}" for e in active) + "\n")


# ── Consolidation (background) ─────────────────────────────────────────────

def _read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def _messages_since(cursor_id: int) -> list:
    with db.db_ctx() as conn:
        return conn.execute(
            "SELECT id, role, content FROM messages WHERE id > ? ORDER BY id ASC",
            (cursor_id,)).fetchall()


def _max_id() -> int:
    with db.db_ctx() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) m FROM messages").fetchone()["m"]


_SYSTEM_PROMPT = """You are the memory system of a roleplay companion app. The character is Ame (from NEEDY GIRL OVERDOSE); {user} is the person she lives with (the user).
You maintain one memory file per person, plus "us" (Ame, {user} and their relationship). These files are what Ame will "remember" in future conversations, so keep what a close friend would remember.

You receive: (1) the current memory files that may be affected, (2) the names of all existing files, (3) the NEW conversation messages.
Return ONLY a JSON object: {"operations": [...]}. Each operation is one of:
  {"action":"add","entity":"<name>","fact":"<fact>"}
  {"action":"replace","entity":"<name>","old":"<exact existing fact>","new":"<updated fact>"}
  {"action":"delete","entity":"<name>","old":"<exact existing fact>"}
  {"action":"create","entity":"<new person name>","keys":["<name variants>"],"fact":"<first fact>"}

Rules:
- Only DURABLE facts that HAPPENED in these messages: events, decisions, agreements, changes in a relationship, promises, nicknames, open threads. Not moods of the moment, not her inner monologue, not speculation.
- Facts about Ame and {user} together, AND facts {user} shares about themselves (work, where they're from, tastes, family, plans), go to "us". Facts about another person (and what happened between that person and Ame or {user}) go to that person's file.
- Do NOT repeat official canon (her past from the game/anime); only what happened in THIS story.
- If something changed, use "replace" on the old fact instead of adding a contradiction. If something stopped being true, "delete" it.
- "create" only for a real, recurring person; never for objects or one-off strangers.
- One fact = one short, concrete sentence in {lang}, third person, using the names (Ame, {user}), with the concrete details (places, objects, words actually said). Intimate or sexual moments: mention them only briefly and without explicit detail.
- If nothing durable happened, return {"operations": []}."""


def _user_name() -> str:
    return (db.get_profile().get("user_name") or "").strip() or "the user"


def _call(user_prompt: str, language: str) -> str:
    settings = db.get_settings()
    model = AUX_MODEL.get(settings.get("llm_provider", "grok"))
    prompt = (_SYSTEM_PROMPT.replace("{lang}", LANGUAGE_LABELS.get(language, language))
              .replace("{user}", _user_name()))
    return call_llm([
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_prompt},
    ], max_tokens=1500, temperature=0.2, conv_id="ame-app-living-memory",
        model=model, reasoning=None)


def _extract_json(text: str) -> dict | None:
    """The reply's JSON, normalized to {"operations": [...]} (also accepts the
    old Spanish key "operaciones"). None if invalid or a refusal."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except ValueError:
        return None
    ops = parsed.get("operations", parsed.get("operaciones"))
    return {"operations": ops} if isinstance(ops, list) else None


def _field(op: dict, english: str, spanish: str):
    return op.get(english, op.get(spanish))


def apply_operations(ops: list) -> list[str]:
    """Apply validated operations and return a readable log. Malformed
    operations, or ones pointing at facts that don't exist, are ignored."""
    current = {s: dict(e, facts=list(e["facts"])) for s, e in entries().items()}
    by_name = {canon._norm(e["name"]): s for s, e in current.items()}
    touched, log = set(), []

    def find(name):
        n = canon._norm(name or "")
        if n in _US_NAMES:
            return ALWAYS_ENTRY
        return by_name.get(n)

    for op in ops:
        if not isinstance(op, dict):
            continue
        action = _ACTIONS.get(str(_field(op, "action", "accion") or "").lower())
        entity = str(_field(op, "entity", "entidad") or "").strip()
        fact = str(_field(op, "fact", "hecho") or "").strip()
        slug = find(entity)
        if action == "create":
            if slug or not entity or not fact:
                action = "add"                  # already exists: treat as add
            else:
                slug = _slug(entity)
                keys = _field(op, "keys", "claves") or []
                current[slug] = {"slug": slug, "name": entity,
                                 "keys": [k for k in keys if isinstance(k, str)], "facts": []}
                by_name[canon._norm(entity)] = slug
        if not slug:
            continue
        if slug not in current:                  # us.md doesn't exist yet
            current[slug] = {"slug": slug, "name": "Ame and the user", "keys": [], "facts": []}
        facts = current[slug]["facts"]
        name = current[slug]["name"]
        if action in ("add", "create"):
            if fact and fact not in facts:
                facts.append(fact)
                touched.add(slug)
                log.append(f"+ {name}: {fact}")
        elif action == "replace":
            old = str(_field(op, "old", "viejo") or "").strip()
            new = str(_field(op, "new", "nuevo") or "").strip()
            if old in facts and new:
                facts[facts.index(old)] = new
                touched.add(slug)
                log.append(f"~ {name}: {new}")
        elif action == "delete":
            old = str(_field(op, "old", "viejo") or "").strip()
            if old in facts:
                facts.remove(old)
                touched.add(slug)
                log.append(f"- {name}: {old}")
    for slug in touched:
        _write_entry(current[slug])
    return log


def _condense_if_needed(language: str):
    for entry in list(entries().values()):
        if len(_facts_text(entry)) <= ENTRY_CAP:
            continue
        try:
            reply = call_llm([
                {"role": "system", "content": (
                    f"Condense this memory list to under {ENTRY_CAP // 2} characters in "
                    f"{LANGUAGE_LABELS.get(language, language)}. Merge duplicates and outdated items, keep EVERY concrete "
                    "fact that still matters (names, places, objects, promises). Output only "
                    "the list, one fact per line starting with '- '.")},
                {"role": "user", "content": _facts_text(entry)},
            ], max_tokens=1500, temperature=0.2, conv_id="ame-app-living-memory",
                model=AUX_MODEL.get(db.get_settings().get("llm_provider", "grok")),
                reasoning=None)
        except Exception as e:
            print(f"[living-memory] couldn't condense {entry['name']}: {e}")
            continue
        facts = [l[2:].strip() for l in (reply or "").splitlines() if l.strip().startswith("- ")]
        if len(facts) >= 3:
            _write_entry(dict(entry, facts=facts))
            print(f"[living-memory] condensed: {entry['name']} ({len(facts)} facts)")


def init() -> None:
    """Called at startup. Without a cursor yet, it starts at the last
    existing message: 0 on a fresh install, so the story is processed from
    its first message; on a chat that existed before living memory, its past
    isn't re-read on its own (that would spend credit) — see
    tools/seed_memory.py."""
    _migrate_old_layout()
    if "cursor_id" not in _read_state():
        _save_state({"cursor_id": _max_id(), "ts": time.time()})


def consolidate(force: bool = False) -> list[str]:
    """Read the new messages and update the files. Safe to call from
    background_tasks: if a consolidation is already running, it returns."""
    if not _lock.acquire(blocking=False):
        return []
    try:
        state = _read_state()
        if "cursor_id" not in state:
            init()                      # normally app startup already did it
            state = _read_state()
        rows = _messages_since(state["cursor_id"])
        if not rows or (len(rows) < CONSOLIDATE_EVERY and not force):
            return []
        language = db.get_settings().get("language", "en")

        # Size-limited batch; the rest waits for next time.
        batch, total = [], 0
        for r in rows:
            c = r["content"]
            if total + len(c) > INPUT_CAP and batch:
                break
            batch.append(r)
            total += len(c)
        speaker = {"user": _user_name(), "assistant": "Ame"}
        messages_text = "\n\n".join(f"[{r['id']}] {speaker.get(r['role'], r['role'])}: {r['content']}"
                                    for r in batch)

        all_entries = entries()
        normalized = canon._norm(messages_text)
        relevant = [e for s, e in all_entries.items()
                    if s == ALWAYS_ENTRY or e["pattern"].search(normalized)]
        prompt = ("EXISTING FILES: " + ", ".join(e["name"] for e in all_entries.values())
                  + "\n\nCURRENT RELEVANT FILES:\n"
                  + ("\n\n".join(f"### {e['name']}\n{_facts_text(e)}" for e in relevant) or "(none)")
                  + "\n\nNEW MESSAGES:\n" + messages_text)
        print(f"[living-memory] reading {len(batch)} new messages "
              f"({len(relevant)} relevant files)…")
        try:
            parsed = _extract_json(_call(prompt, language))
        except Exception as e:
            print(f"[living-memory] consolidation failed, will retry later: {e}")
            return []
        if parsed is None:
            # Invalid reply or refusal: move on anyway so it never gets stuck
            # (the messages are still in the database, summaries and search).
            print("[living-memory] invalid reply or refusal — skipping this batch")
            log = []
        else:
            log = apply_operations(parsed["operations"])
            for line in log:
                print("[living-memory] " + line)
            if not log:
                print("[living-memory] nothing new worth remembering in this batch")
        _save_state({"cursor_id": batch[-1]["id"], "ts": time.time()})
        _condense_if_needed(language)
        return log
    finally:
        _lock.release()
