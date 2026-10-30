"""
SQLite storage. Every query in the app lives here; this module only stores
and reads, it knows nothing about embeddings, prompts or models.
"""

import json
import sqlite3
import time
import datetime as _dt
from contextlib import contextmanager

from app_config import DB_PATH


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Summaries and embeddings are written from background threads, so two
    # connections can write at once: WAL plus a busy timeout makes the
    # second one wait instead of failing with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db_ctx():
    """Connection that commits on a clean exit and always closes."""
    conn = db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
# Schema and migrations
# ────────────────────────────────────────────────────────────────

def init_db():
    with db_ctx() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,          -- 'user' | 'assistant'
            content TEXT NOT NULL,
            ts REAL NOT NULL,
            embedding BLOB,               -- serialized float32 vector
            sprite TEXT,                  -- URL of the sprite shown, if any
            sprite_tag TEXT,              -- raw tag emitted by the model
                                          -- (ame_intimate, kangel_happy...),
                                          -- whether or not the image is shown
            image_path TEXT               -- image attached by the user, if any
        );
        -- Long messages are also indexed in overlapping chunks: the
        -- embedding model only reads the start of a text, so each chunk
        -- gets its own vector and search can land in the middle.
        CREATE TABLE IF NOT EXISTS message_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            start INTEGER NOT NULL,
            end INTEGER NOT NULL,
            embedding BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_message ON message_chunks(message_id);
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            range_start INTEGER,
            range_end INTEGER,
            content TEXT,
            ts REAL
        );
        CREATE TABLE IF NOT EXISTS pins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            priority INTEGER DEFAULT 0,
            ts REAL
        );
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            user_name TEXT DEFAULT '',
            user_bio TEXT DEFAULT '',
            user_gender TEXT DEFAULT '',
            user_avatar TEXT DEFAULT '',
            ame_avatar TEXT DEFAULT '/images/ame001.png',
            user_likes TEXT DEFAULT '',
            user_dislikes TEXT DEFAULT '',
            user_birthday TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            summary_level TEXT DEFAULT 'low',
            reduce_repetition INTEGER DEFAULT 1,
            dont_refuse INTEGER DEFAULT 0,
            custom_instruction TEXT DEFAULT '',
            sprite_scale INTEGER DEFAULT 300,
            assistant_turn_count INTEGER DEFAULT 0,
            last_sprite_turn INTEGER DEFAULT 0,
            buffer_reset_id INTEGER DEFAULT 0,
            language TEXT DEFAULT 'en',
            stage_override INTEGER DEFAULT 0,
            current_mode TEXT DEFAULT 'ame',
            onboarding_done INTEGER DEFAULT 0,
            llm_provider TEXT DEFAULT 'grok',
            grok_api_key TEXT DEFAULT '',
            grok_model TEXT DEFAULT 'grok-4.3-latest',
            qwen_api_key TEXT DEFAULT '',
            qwen_model TEXT DEFAULT 'qwen-plus',
            deepseek_api_key TEXT DEFAULT '',
            deepseek_model TEXT DEFAULT 'deepseek-v4-pro',
            gpt_api_key TEXT DEFAULT '',
            gpt_model TEXT DEFAULT 'gpt-5.6-sol',
            grok_base_url TEXT DEFAULT '',
            qwen_base_url TEXT DEFAULT '',
            deepseek_base_url TEXT DEFAULT '',
            gpt_base_url TEXT DEFAULT '',
            reasoning_effort TEXT DEFAULT 'none',
            qwen_thinking INTEGER DEFAULT 0,
            deepseek_thinking INTEGER DEFAULT 0,
            birthday_greeted_year INTEGER DEFAULT 0,
            defaults_rev INTEGER DEFAULT 2,
            polish_speech INTEGER DEFAULT 1
        );
        """)
        conn.execute("INSERT OR IGNORE INTO profile (id) VALUES (1)")
        conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")

        # Columns added after the first release.
        profile_cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile)")}
        if "user_gender" not in profile_cols:
            conn.execute("ALTER TABLE profile ADD COLUMN user_gender TEXT DEFAULT ''")
        for col, ddl in [
            ("user_likes", "ALTER TABLE profile ADD COLUMN user_likes TEXT DEFAULT ''"),
            ("user_dislikes", "ALTER TABLE profile ADD COLUMN user_dislikes TEXT DEFAULT ''"),
            ("user_birthday", "ALTER TABLE profile ADD COLUMN user_birthday TEXT DEFAULT ''"),
        ]:
            if col not in profile_cols:
                conn.execute(ddl)
        settings_cols_early = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
        if "onboarding_done" not in settings_cols_early:
            conn.execute("ALTER TABLE settings ADD COLUMN onboarding_done INTEGER DEFAULT 0")
        for col, ddl in [
            ("llm_provider", "ALTER TABLE settings ADD COLUMN llm_provider TEXT DEFAULT 'grok'"),
            ("grok_api_key", "ALTER TABLE settings ADD COLUMN grok_api_key TEXT DEFAULT ''"),
            ("grok_model", "ALTER TABLE settings ADD COLUMN grok_model TEXT DEFAULT 'grok-4.3-latest'"),
            ("qwen_api_key", "ALTER TABLE settings ADD COLUMN qwen_api_key TEXT DEFAULT ''"),
            ("qwen_model", "ALTER TABLE settings ADD COLUMN qwen_model TEXT DEFAULT 'qwen-plus'"),
            ("deepseek_api_key", "ALTER TABLE settings ADD COLUMN deepseek_api_key TEXT DEFAULT ''"),
            ("deepseek_model", "ALTER TABLE settings ADD COLUMN deepseek_model TEXT DEFAULT 'deepseek-v4-pro'"),
            ("gpt_api_key", "ALTER TABLE settings ADD COLUMN gpt_api_key TEXT DEFAULT ''"),
            ("gpt_model", "ALTER TABLE settings ADD COLUMN gpt_model TEXT DEFAULT 'gpt-5.6-sol'"),
            ("grok_base_url", "ALTER TABLE settings ADD COLUMN grok_base_url TEXT DEFAULT ''"),
            ("qwen_base_url", "ALTER TABLE settings ADD COLUMN qwen_base_url TEXT DEFAULT ''"),
            ("deepseek_base_url", "ALTER TABLE settings ADD COLUMN deepseek_base_url TEXT DEFAULT ''"),
            ("gpt_base_url", "ALTER TABLE settings ADD COLUMN gpt_base_url TEXT DEFAULT ''"),
            ("reasoning_effort", "ALTER TABLE settings ADD COLUMN reasoning_effort TEXT DEFAULT 'none'"),
            ("qwen_thinking", "ALTER TABLE settings ADD COLUMN qwen_thinking INTEGER DEFAULT 0"),
            ("deepseek_thinking", "ALTER TABLE settings ADD COLUMN deepseek_thinking INTEGER DEFAULT 0"),
        ]:
            if col not in settings_cols_early:
                conn.execute(ddl)

        msg_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        for col, ddl in [
            ("sprite", "ALTER TABLE messages ADD COLUMN sprite TEXT"),
            ("image_path", "ALTER TABLE messages ADD COLUMN image_path TEXT"),
            # `sprite` is the URL shown (NULL when there was no image);
            # `sprite_tag` is the tag the model wrote, image or not.
            ("sprite_tag", "ALTER TABLE messages ADD COLUMN sprite_tag TEXT"),
            # Every sprite a reply got, in order (JSON list of URLs).
            ("sprite_seq", "ALTER TABLE messages ADD COLUMN sprite_seq TEXT"),
            # Regenerate: every version of a reply and which one is shown.
            # The row itself always holds the shown version.
            ("versions", "ALTER TABLE messages ADD COLUMN versions TEXT"),
            ("version_idx", "ALTER TABLE messages ADD COLUMN version_idx INTEGER"),
            # Continue extends a reply once.
            ("continued", "ALTER TABLE messages ADD COLUMN continued INTEGER DEFAULT 0"),
        ]:
            if col not in msg_cols:
                conn.execute(ddl)

        # Replies with two sprites were already continued.
        conn.execute("UPDATE messages SET continued = 1 WHERE continued = 0 "
                     "AND sprite_seq LIKE '[%,%]'")

        set_cols = {r["name"] for r in conn.execute("PRAGMA table_info(settings)")}
        for col, ddl in [
            ("sprite_scale", "ALTER TABLE settings ADD COLUMN sprite_scale INTEGER DEFAULT 300"),
            ("assistant_turn_count", "ALTER TABLE settings ADD COLUMN assistant_turn_count INTEGER DEFAULT 0"),
            ("last_sprite_turn", "ALTER TABLE settings ADD COLUMN last_sprite_turn INTEGER DEFAULT 0"),
            ("buffer_reset_id", "ALTER TABLE settings ADD COLUMN buffer_reset_id INTEGER DEFAULT 0"),
            ("language", "ALTER TABLE settings ADD COLUMN language TEXT DEFAULT 'en'"),
            ("stage_override", "ALTER TABLE settings ADD COLUMN stage_override INTEGER DEFAULT 0"),
            ("current_mode", "ALTER TABLE settings ADD COLUMN current_mode TEXT DEFAULT 'ame'"),
            ("birthday_greeted_year", "ALTER TABLE settings ADD COLUMN birthday_greeted_year INTEGER DEFAULT 0"),
            ("defaults_rev", "ALTER TABLE settings ADD COLUMN defaults_rev INTEGER DEFAULT 0"),
            ("polish_speech", "ALTER TABLE settings ADD COLUMN polish_speech INTEGER DEFAULT 1"),
        ]:
            if col not in set_cols:
                conn.execute(ddl)

        # defaults_rev: a default that changed and should reach existing
        # installs once; after that the user's choice is kept.
        rev = conn.execute("SELECT COALESCE(defaults_rev, 0) r FROM settings WHERE id = 1").fetchone()["r"]
        if rev < 2:
            conn.execute("UPDATE settings SET deepseek_thinking = 0, defaults_rev = 2 WHERE id = 1")


# ────────────────────────────────────────────────────────────────
# Messages
# ────────────────────────────────────────────────────────────────

def save_message(role: str, content: str, embedding: bytes | None,
                 sprite: str | None = None, image_path: str | None = None,
                 sprite_tag: str | None = None) -> int:
    with db_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO messages (role, content, ts, embedding, sprite, "
            "image_path, sprite_tag, sprite_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (role, content, time.time(), embedding, sprite, image_path, sprite_tag,
             json.dumps([sprite]) if sprite else None),
        )
        return cur.lastrowid


def get_message_content(msg_id: int) -> str | None:
    """Current text of a message, or None if it no longer exists."""
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT content FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
    return row["content"] if row else None


def update_message_embedding(msg_id: int, embedding: bytes):
    """Writes only the embedding column (filled in the background by
    memory.embed_deferred)."""
    with db_ctx() as conn:
        conn.execute(
            "UPDATE messages SET embedding = ? WHERE id = ?", (embedding, msg_id)
        )


def get_recent_messages(n: int) -> list[dict]:
    """The last n messages after the last Recalibrate. Older messages are
    still in the history, the summaries and semantic search; they just stop
    being sent word for word."""
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT COALESCE(buffer_reset_id, 0) r FROM settings WHERE id = 1"
        ).fetchone()
        floor_id = row["r"] if row else 0
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE id > ? ORDER BY id DESC LIMIT ?",
            (floor_id, n),
        ).fetchall()
    return list(reversed([{"role": r["role"], "content": r["content"]} for r in rows]))


def get_last_assistant_replies(n: int) -> list[dict]:
    """Her last n replies, ignoring the Recalibrate floor, so the
    repetition checks see what she actually said lately."""
    with db_ctx() as conn:
        rows = conn.execute(
            "SELECT content FROM messages WHERE role = 'assistant' "
            "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [{"role": "assistant", "content": r["content"]} for r in reversed(rows)]


def recent_user_burst(current_len: int, n: int = 3, window_seconds: float = 90.0,
                       max_chars: int = 20) -> bool:
    """True if the current message (not saved yet) and the previous
    `n - 1` user messages are all short and arrived within
    `window_seconds`: a burst of short messages in a row."""
    if current_len > max_chars:
        return False
    need = n - 1
    if need <= 0:
        return True
    with db_ctx() as conn:
        rows = conn.execute(
            "SELECT content, ts FROM messages WHERE role = 'user' "
            "ORDER BY id DESC LIMIT ?",
            (need,),
        ).fetchall()
    if len(rows) < need:
        return False
    if any(len(r["content"].strip()) > max_chars for r in rows):
        return False
    oldest_ts = rows[-1]["ts"]
    return (time.time() - oldest_ts) <= window_seconds


def recalibrate_buffer() -> dict:
    """Cuts the literal buffer at the most recent message and resets the
    sprite counters. Returns how many messages left the window."""
    with db_ctx() as conn:
        last_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) m FROM messages"
        ).fetchone()["m"]
        prev = conn.execute(
            "SELECT COALESCE(buffer_reset_id, 0) r FROM settings WHERE id = 1"
        ).fetchone()["r"]
        conn.execute(
            "UPDATE settings SET buffer_reset_id = ?, assistant_turn_count = 0, "
            "last_sprite_turn = 0 WHERE id = 1",
            (last_id,),
        )
        cleared = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE id > ? AND id <= ?", (prev, last_id)
        ).fetchone()["c"]
    return {"buffer_reset_id": last_id, "cleared": cleared}


def get_messages_with_embeddings() -> list[sqlite3.Row]:
    """All messages, most recent first, embedding included."""
    with db_ctx() as conn:
        return conn.execute(
            "SELECT id, role, content, embedding FROM messages ORDER BY id DESC"
        ).fetchall()


def _sprite_list(seq_json: str | None, sprite: str | None) -> list[str]:
    try:
        seq = json.loads(seq_json) if seq_json else []
        seq = [u for u in seq if isinstance(u, str) and u]
    except (ValueError, TypeError):
        seq = []
    if not seq and sprite:
        seq = [sprite]
    return seq


def get_sprites(msg_id: int) -> list[str]:
    with db_ctx() as conn:
        row = conn.execute("SELECT sprite, sprite_seq FROM messages WHERE id = ?",
                           (msg_id,)).fetchone()
    return _sprite_list(row["sprite_seq"], row["sprite"]) if row else []


def get_history(limit: int) -> list[dict]:
    with db_ctx() as conn:
        rows = conn.execute(
            "SELECT id, role, content, ts, sprite, sprite_seq, image_path, versions, "
            "version_idx, continued FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["sprites"] = _sprite_list(d.pop("sprite_seq"), d["sprite"])
        versions = _versions(d.pop("versions"))
        d["versions_n"] = max(1, len(versions))
        d["version_idx"] = d["version_idx"] if versions and d["version_idx"] is not None else 0
        d["continued"] = bool(d["continued"])
        out.append(d)
    return out


# ── Regenerate / versions ────────────────────────────

def _versions(raw: str | None) -> list[dict]:
    try:
        v = json.loads(raw) if raw else []
        return [x for x in v if isinstance(x, dict) and isinstance(x.get("content"), str)]
    except (ValueError, TypeError):
        return []


def snapshot_version(row) -> dict:
    """What a version keeps: the text and its sprites."""
    return {"content": row["content"], "sprite": row["sprite"],
            "sprite_seq": row["sprite_seq"], "sprite_tag": row["sprite_tag"],
            "continued": bool(row["continued"]) if "continued" in row.keys() else False}


def get_last_turn() -> tuple[dict, dict] | None:
    """The last user message and Ame's reply to it — full rows, embedding
    included, so they can be put back exactly if a regeneration fails.
    None unless the chat ends in user → assistant."""
    with db_ctx() as conn:
        rows = conn.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 2").fetchall()
    if len(rows) < 2 or rows[0]["role"] != "assistant" or rows[1]["role"] != "user":
        return None
    return dict(rows[1]), dict(rows[0])


def delete_messages(ids: list[int]):
    with db_ctx() as conn:
        for i in ids:
            conn.execute("DELETE FROM message_chunks WHERE message_id = ?", (i,))
            conn.execute("DELETE FROM messages WHERE id = ?", (i,))


def restore_message(row: dict):
    """Puts a deleted row back with its original id and every column."""
    cols = [c for c in row.keys()]
    with db_ctx() as conn:
        conn.execute(f"INSERT INTO messages ({', '.join(cols)}) VALUES "
                     f"({', '.join('?' for _ in cols)})", [row[c] for c in cols])


def set_versions(msg_id: int, versions: list[dict], idx: int):
    with db_ctx() as conn:
        conn.execute("UPDATE messages SET versions = ?, version_idx = ? WHERE id = ?",
                     (json.dumps(versions[-10:]), min(idx, len(versions[-10:]) - 1), msg_id))


def get_message_row(msg_id: int) -> dict | None:
    with db_ctx() as conn:
        r = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    return dict(r) if r else None


def apply_version(msg_id: int, version: dict, embedding: bytes | None, idx: int,
                  versions: list[dict]):
    """Shows another version: the row takes its text and sprites."""
    with db_ctx() as conn:
        conn.execute(
            "UPDATE messages SET content = ?, embedding = ?, sprite = ?, sprite_seq = ?, "
            "sprite_tag = ?, versions = ?, version_idx = ?, continued = ? WHERE id = ?",
            (version["content"], embedding, version.get("sprite"), version.get("sprite_seq"),
             version.get("sprite_tag"), json.dumps(versions), idx,
             1 if version.get("continued") else 0, msg_id))


def get_last_assistant_message() -> sqlite3.Row | None:
    with db_ctx() as conn:
        return conn.execute(
            "SELECT id, content, sprite, sprite_tag, continued FROM messages WHERE role = 'assistant' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()


def update_message_content(msg_id: int, content: str, embedding: bytes | None,
                           sprite: str | None = None,
                           sprite_tag: str | None = None,
                           mark_continued: bool = False):
    """Writes a reply's new full text together with its embedding (used by
    Continue), so the vector always matches the text."""
    fields = ["content = ?", "embedding = ?"]
    values = [content, embedding]
    if mark_continued:
        fields.append("continued = 1")
    if sprite:
        fields.append("sprite = ?")
        values.append(sprite)
        # Keep every sprite the message has had (Continue adds one).
        with db_ctx() as conn:
            row = conn.execute("SELECT sprite, sprite_seq FROM messages WHERE id = ?",
                               (msg_id,)).fetchone()
        seq = _sprite_list(row["sprite_seq"], row["sprite"]) if row else []
        if not seq or seq[-1] != sprite:
            seq.append(sprite)
        fields.append("sprite_seq = ?")
        values.append(json.dumps(seq[-6:]))
    if sprite_tag:
        fields.append("sprite_tag = ?")
        values.append(sprite_tag)
    values.append(msg_id)
    with db_ctx() as conn:
        conn.execute(
            f"UPDATE messages SET {', '.join(fields)} WHERE id = ?", values
        )


def count_messages(role: str | None = None) -> int:
    with db_ctx() as conn:
        if role:
            return conn.execute(
                "SELECT COUNT(*) c FROM messages WHERE role = ?", (role,)
            ).fetchone()["c"]
        return conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]


# ────────────────────────────────────────────────────────────────
# Pins
# ────────────────────────────────────────────────────────────────

def get_all_pins() -> list[dict]:
    with db_ctx() as conn:
        rows = conn.execute(
            "SELECT id, content, priority, ts FROM pins ORDER BY priority DESC, id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_pin_contents() -> list[str]:
    """Text only, to inject into the prompt."""
    return [p["content"] for p in get_all_pins()]


def add_pin(content: str, priority: int = 0) -> int:
    with db_ctx() as conn:
        cur = conn.execute(
            "INSERT INTO pins (content, priority, ts) VALUES (?, ?, ?)",
            (content, priority, time.time()),
        )
        return cur.lastrowid


def update_pin(pin_id: int, content: str):
    with db_ctx() as conn:
        conn.execute("UPDATE pins SET content = ? WHERE id = ?", (content, pin_id))


def delete_pin(pin_id: int):
    with db_ctx() as conn:
        conn.execute("DELETE FROM pins WHERE id = ?", (pin_id,))


# ────────────────────────────────────────────────────────────────
# Summaries
# ────────────────────────────────────────────────────────────────

def get_recent_summaries(n: int = 2) -> list[str]:
    with db_ctx() as conn:
        rows = conn.execute(
            "SELECT content FROM summaries ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return list(reversed([r["content"] for r in rows]))


def save_summary(range_start: int, range_end: int, content: str):
    with db_ctx() as conn:
        conn.execute(
            "INSERT INTO summaries (range_start, range_end, content, ts) VALUES (?, ?, ?, ?)",
            (range_start, range_end, content, time.time()),
        )


def get_unsummarized_rows() -> list[sqlite3.Row]:
    with db_ctx() as conn:
        last_end = conn.execute(
            "SELECT COALESCE(MAX(range_end), 0) e FROM summaries"
        ).fetchone()["e"]
        return conn.execute(
            "SELECT id, role, content FROM messages WHERE id > ? ORDER BY id ASC",
            (last_end,),
        ).fetchall()


def get_summaries_overview() -> dict:
    with db_ctx() as conn:
        rows = conn.execute(
            "SELECT id, range_start, range_end, content, ts FROM summaries ORDER BY id DESC"
        ).fetchall()
        total_msgs = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        last_end = conn.execute(
            "SELECT COALESCE(MAX(range_end), 0) e FROM summaries"
        ).fetchone()["e"]
    return {
        "summaries": [dict(r) for r in rows],
        "total_messages": total_msgs,
        "last_summarized_id": last_end,
    }


# ────────────────────────────────────────────────────────────────
# Profile and settings
# ────────────────────────────────────────────────────────────────

def get_profile() -> dict:
    with db_ctx() as conn:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    return dict(row) if row else {}


def set_profile(**kwargs):
    allowed = ("user_name", "user_bio", "user_gender", "user_avatar", "ame_avatar",
               "user_likes", "user_dislikes", "user_birthday")
    fields, values = [], []
    for k in allowed:
        if kwargs.get(k) is not None:
            fields.append(f"{k} = ?")
            values.append(kwargs[k])
    if not fields:
        return
    with db_ctx() as conn:
        conn.execute(f"UPDATE profile SET {', '.join(fields)} WHERE id = 1", values)


# ────────────────────────────────────────────────────────────────
# Birthday, from the computer's local clock. Stored as "MM-DD", never with
# the year.
# ────────────────────────────────────────────────────────────────

def is_birthday_today() -> bool:
    birthday = get_profile().get("user_birthday", "")
    if not birthday:
        return False
    today = _dt.date.today().strftime("%m-%d")
    return birthday == today


def birthday_already_greeted_this_year() -> bool:
    year = get_settings().get("birthday_greeted_year", 0)
    return year == _dt.date.today().year


def mark_birthday_greeted():
    """Records that this year's birthday greeting happened; next year it
    turns itself back on."""
    with db_ctx() as conn:
        conn.execute(
            "UPDATE settings SET birthday_greeted_year = ? WHERE id = 1",
            (_dt.date.today().year,),
        )


def get_settings() -> dict:
    with db_ctx() as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return dict(row) if row else {}


def set_settings(**kwargs):
    # column -> cast; the custom instruction is capped at 300 characters
    allowed = {
        "summary_level": str,
        "reduce_repetition": int,
        "dont_refuse": int,
        "custom_instruction": lambda v: str(v)[:300],
        "sprite_scale": int,
        "language": str,
        "stage_override": int,
        "onboarding_done": int,
        "llm_provider": str,
        "grok_api_key": str,
        "grok_model": str,
        "qwen_api_key": str,
        "qwen_model": str,
        "deepseek_api_key": str,
        "deepseek_model": str,
        "gpt_api_key": str,
        "gpt_model": str,
        "grok_base_url": str,
        "qwen_base_url": str,
        "deepseek_base_url": str,
        "gpt_base_url": str,
        "reasoning_effort": str,
        "qwen_thinking": int,
        "deepseek_thinking": int,
        "polish_speech": int,
    }
    fields, values = [], []
    for k, cast in allowed.items():
        if kwargs.get(k) is not None:
            fields.append(f"{k} = ?")
            values.append(cast(kwargs[k]))
    if not fields:
        return
    with db_ctx() as conn:
        conn.execute(f"UPDATE settings SET {', '.join(fields)} WHERE id = 1", values)


def bump_assistant_turn(sprite_shown: bool) -> int:
    """Counts one more reply from Ame and, if it showed a sprite, records
    that turn. Returns the new turn number."""
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT assistant_turn_count FROM settings WHERE id = 1"
        ).fetchone()
        turn = row["assistant_turn_count"] + 1
        if sprite_shown:
            conn.execute(
                "UPDATE settings SET assistant_turn_count = ?, last_sprite_turn = ? WHERE id = 1",
                (turn, turn),
            )
        else:
            conn.execute(
                "UPDATE settings SET assistant_turn_count = ? WHERE id = 1", (turn,)
            )
    return turn


# ────────────────────────────────────────────────────────────────
# Ame ↔ KAngel mode
# ────────────────────────────────────────────────────────────────

def get_mode() -> str:
    """'ame' (default, off-stream) or 'kangel' (live, streaming)."""
    mode = get_settings().get("current_mode") or "ame"
    return mode if mode in ("ame", "kangel") else "ame"


def set_mode(mode: str):
    if mode not in ("ame", "kangel"):
        return
    with db_ctx() as conn:
        conn.execute("UPDATE settings SET current_mode = ? WHERE id = 1", (mode,))


# ────────────────────────────────────────────────────────────────
# Chunks of long messages
# ────────────────────────────────────────────────────────────────

def replace_message_chunks(msg_id: int, chunks: list[tuple[int, int, bytes]]):
    """Replaces a message's chunks (recomputed when its text changes, e.g.
    with /continue)."""
    with db_ctx() as conn:
        conn.execute("DELETE FROM message_chunks WHERE message_id = ?", (msg_id,))
        conn.executemany(
            "INSERT INTO message_chunks (message_id, start, end, embedding) "
            "VALUES (?, ?, ?, ?)", [(msg_id, a, b, e) for a, b, e in chunks])


def get_all_chunks() -> list[sqlite3.Row]:
    with db_ctx() as conn:
        return conn.execute(
            "SELECT message_id, start, end, embedding FROM message_chunks"
        ).fetchall()


def get_long_messages_without_chunks(min_chars: int) -> list[sqlite3.Row]:
    with db_ctx() as conn:
        return conn.execute(
            "SELECT id, content FROM messages WHERE length(content) > ? "
            "AND id NOT IN (SELECT DISTINCT message_id FROM message_chunks) "
            "ORDER BY id", (min_chars,)
        ).fetchall()
