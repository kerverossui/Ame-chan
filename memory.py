"""
Conversation memory:

1. Recent buffer: database.get_recent_messages (word for word).
2. Rolling summaries: maybe_summarize / force_summarize.
3. Semantic retrieval: semantic_search, with local embeddings
   (sentence-transformers, no external service).

Per-person living memory is in living_memory.py; the canon in canon.py.
"""

import os
import re

import threading

import numpy as np

import database as db
from app_config import RECENT_BUFFER, RETRIEVE_K, SUMMARY_LEVEL_TO_INTERVAL, LANGUAGE_LABELS
from llm import call_llm

# ────────────────────────────────────────────────────────────────
# Local embeddings
# ────────────────────────────────────────────────────────────────
# A multilingual model, since most messages are not in English. Vectors from
# different models are not comparable even at the same size: after changing
# it, run tools/reembed_all.py once with the app stopped.
_EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

# Keep Hugging Face's notices and progress bars out of the console; errors
# still show.
for _k, _v in (("HF_HUB_VERBOSITY", "error"), ("TRANSFORMERS_VERBOSITY", "error"),
               ("HF_HUB_DISABLE_PROGRESS_BARS", "1"), ("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1"),
               ("TOKENIZERS_PARALLELISM", "false")):
    os.environ.setdefault(_k, _v)

_embedder = None


def embedder_is_cached() -> bool:
    """True if the embedding model is already in the local Hugging Face
    cache. Only looks at disk. main.py uses it to wait for the first
    download before accepting messages."""
    repo = f"sentence-transformers/{_EMBEDDING_MODEL_NAME}"
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return True     # without huggingface_hub we can't tell: don't block
    try:
        for file_name in ("config.json", "modules.json"):
            r = try_to_load_from_cache(repo, file_name)
            if not isinstance(r, str) or not os.path.exists(r):
                return False
        return True
    except Exception:
        return True


def get_embedder():
    global _embedder
    if _embedder is None:
        # Already downloaded → no internet requests (faster startup and no
        # English "unauthenticated requests" warning).
        local = embedder_is_cached()
        from sentence_transformers import SentenceTransformer
        try:
            from transformers.utils import logging as _tl
            _tl.disable_progress_bar()
        except Exception:
            pass
        try:
            _embedder = SentenceTransformer(_EMBEDDING_MODEL_NAME, local_files_only=local)
        except Exception:
            # incomplete cache or another version of the library: normal mode
            _embedder = SentenceTransformer(_EMBEDDING_MODEL_NAME)
    return _embedder


def embed(text: str) -> np.ndarray:
    return get_embedder().encode(text, normalize_embeddings=True)


def remember(role: str, content: str, sprite: str | None = None,
             image_path: str | None = None, sprite_tag: str | None = None) -> int:
    """Stores a message and computes its embedding right away (used for the
    opening scene). Turns use remember_fast + embed_deferred instead."""
    vec = embed(content).astype(np.float32).tobytes()
    msg_id = db.save_message(role, content, vec, sprite, image_path, sprite_tag)
    index_chunks(msg_id, content)
    return msg_id


def remember_fast(role: str, content: str, sprite: str | None = None,
                  image_path: str | None = None,
                  sprite_tag: str | None = None) -> int:
    """Stores the message without its embedding and returns its id; the
    caller queues embed_deferred(id, content) for after the reply. A new
    message is in the recent buffer for several turns, and semantic search
    skips the buffer, so nothing needs its vector sooner."""
    return db.save_message(role, content, None, sprite, image_path, sprite_tag)


def embed_deferred(msg_id: int, content: str):
    """Fills in the embedding remember_fast left pending. Writes only if the
    message still has the same text, so it never overwrites the vector of a
    reply Continue has extended in the meantime."""
    current = db.get_message_content(msg_id)
    if current != content:
        print(f"[embed-deferred] message {msg_id} changed in the meantime "
              "(probably /continue) — its already-updated embedding is not overwritten.")
        return
    vec = embed(content).astype(np.float32).tobytes()
    db.update_message_embedding(msg_id, vec)
    index_chunks(msg_id, content)


def remember_continuation(msg_id: int, previous_content: str, extra: str,
                          sprite: str | None = None,
                          sprite_tag: str | None = None) -> str:
    """Continue: appends the extension to the reply, recomputes the
    embedding over the full text and writes both together. Returns the full
    text."""
    full_content = previous_content + "\n\n" + extra
    vec = embed(full_content).astype(np.float32).tobytes()
    db.update_message_content(msg_id, full_content, vec, sprite=sprite,
                              sprite_tag=sprite_tag, mark_continued=True)
    index_chunks(msg_id, full_content)
    return full_content


def semantic_search(query: str, k: int = RETRIEVE_K,
                    exclude_recent: int = RECENT_BUFFER) -> list[dict]:
    """The k most semantically similar messages, excluding the ones already
    travelling literally in the recent buffer (to avoid duplicates).

    Long messages are also searched by chunks (see index_chunks): a
    message scores the best of its full vector and its chunks, and for a
    long message the matching chunk is returned (with "fragment": True)."""
    rows = db.get_messages_with_embeddings()
    if len(rows) <= exclude_recent:
        return []

    qvec = embed(query)
    expected_bytes = qvec.astype(np.float32).nbytes
    # Rows without an embedding or with a different dimension are skipped
    # (e.g. if the embedding model ever changes, old rows don't break the
    # search — they just don't take part).
    candidates = [
        r for r in rows[exclude_recent:]
        if r["embedding"] and len(r["embedding"]) == expected_bytes
    ]
    if not candidates:
        return []
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in candidates])
    sims = mat @ qvec

    # Best chunk per candidate message
    pos = {r["id"]: i for i, r in enumerate(candidates)}
    chunk_list = [c for c in db.get_all_chunks()
              if c["message_id"] in pos and len(c["embedding"]) == expected_bytes]
    best = {}                                   # candidate index → (sim, start, end)
    if chunk_list:
        tmat = np.stack([np.frombuffer(c["embedding"], dtype=np.float32) for c in chunk_list])
        tsims = tmat @ qvec
        for c, sim in zip(chunk_list, tsims):
            i = pos[c["message_id"]]
            if i not in best or sim > best[i][0]:
                best[i] = (float(sim), c["start"], c["end"])
    scores = np.array([max(float(sims[i]), best.get(i, (-1.0,))[0])
                       for i in range(len(candidates))])

    top_idx = np.argsort(scores)[::-1][:k]
    output = []
    for i in top_idx:
        r = candidates[i]
        content_text, fragment = r["content"], False
        if i in best and len(content_text) > FRAGMENT_IF_LONGER_THAN:
            _, a, b = best[i]
            content_text, fragment = content_text[a:b].strip(), True
        output.append({"role": r["role"], "content": content_text,
                       "score": round(float(scores[i]), 3), "fragment": fragment})
    return output


# ── Chunks of long messages ────────────────────────────────────────────
#   The embedding model only reads the start of each text (about 128
#   tokens), so long messages are split into overlapping chunks, each with
#   its own vector.
CHUNK_MIN_MESSAGE = 700      # shorter messages are not chunked
CHUNK_SIZE = 600
CHUNK_OVERLAP = 120
FRAGMENT_IF_LONGER_THAN = 900    # longer messages return only the chunk


def _chunk_spans(text_value: str) -> list[tuple[int, int]]:
    """(start, end) cuts of ~CHUNK_SIZE characters, extended to the nearest
    end of sentence/line, with overlap."""
    cuts, start_pos, n = [], 0, len(text_value)
    while start_pos < n:
        end_pos = min(start_pos + CHUNK_SIZE, n)
        if end_pos < n:
            window_text = text_value[end_pos:end_pos + 150]
            m = re.search(r"[.!?…»\n]", window_text)
            if m:
                end_pos += m.end()
        cuts.append((start_pos, end_pos))
        if end_pos >= n:
            break
        next_item = max(end_pos - CHUNK_OVERLAP, start_pos + 1)
        space = text_value.find(" ", next_item)
        start_pos = space + 1 if 0 <= space < end_pos else next_item
    return cuts


def index_chunks(msg_id: int, content: str):
    """Computes and stores the chunk vectors of a long message.
    Idempotent: replaces the message's previous chunks."""
    if len(content) <= CHUNK_MIN_MESSAGE:
        return
    cuts = _chunk_spans(content)
    vecs = get_embedder().encode([content[a:b] for a, b in cuts],
                                 normalize_embeddings=True)
    db.replace_message_chunks(msg_id, [
        (a, b, np.asarray(v, dtype=np.float32).tobytes()) for (a, b), v in zip(cuts, vecs)])


def index_pending_chunks():
    """Fills in the chunks of long messages that don't have them yet. Runs
    in the background at startup and can be interrupted safely."""
    pending = db.get_long_messages_without_chunks(CHUNK_MIN_MESSAGE)
    if not pending:
        return
    print(f"[memory] indexing {len(pending)} long messages by chunks (one time only)...")
    for r in pending:
        try:
            index_chunks(r["id"], r["content"])
        except Exception as e:
            print(f"[memory] couldn't chunk message {r['id']}: {e}")
            return
    print("[memory] chunk indexing done.")


# ────────────────────────────────────────────────────────────────
# Rolling summaries
# ────────────────────────────────────────────────────────────────
SUMMARY_CONV_ID = "ame-app-local-summarize"


# ── Refusals ─────────────────────────────────────────────────────────────
#   Some engines refuse to summarize intimate scenes. A refusal is never
#   stored and never blocks the cursor:
#     1. The normal prompt already asks for intimate content to be described
#        briefly and non-explicitly.
#     2. If it refuses: one retry with a neutral-summary framing.
#     3. If it refuses again: a minimal summary without AI (the range and
#        that it was an intimate moment), so the cursor moves on.
#   A network error is not a refusal: that batch is retried later.

_SUMMARY_SYSTEM = (
    "You write the running memory of an ongoing roleplay between Ame and "
    "{user}. Later turns will only see this summary, not the messages, so "
    "keep what continuity needs: what happened and where, things {user} "
    "told her about themselves, promises and plans, names, objects and "
    "phrases that became meaningful, running jokes, and how the "
    "relationship shifted. Write 3-6 plain sentences in third person, in "
    "{lang}. Only what is in the text; nothing invented. Intimate or "
    "sexual moments: say so briefly, without explicit detail (e.g. 'they "
    "were intimate')."
)
_SUMMARY_SYSTEM_NEUTRAL = (
    "You are writing a private memory note for a fictional roleplay app "
    "between consenting adults (Ame and {user}). Write a NEUTRAL, non-explicit summary "
    "(3-5 sentences, third person, in {lang}) of what happened "
    "emotionally and practically. Replace any sexual content with a "
    "neutral phrase such as 'they shared an intimate moment'. Do not "
    "quote the text and do not refuse: a neutral summary is always possible."
)
_SUMMARY_FALLBACK = {
    "es": "Mensajes {a}–{b}: Ame y {user} compartieron un momento íntimo y "
          "conversaron sobre su relación (resumen mínimo: el modelo "
          "actual no quiso resumir este tramo).",
    "it": "Messaggi {a}–{b}: Ame e {user} hanno condiviso un momento intimo "
          "e parlato della loro relazione (riassunto minimo).",
    "en": "Messages {a}–{b}: Ame and {user} shared an intimate moment and "
          "talked about their relationship (minimal summary: the current "
          "model declined to summarize this stretch).",
}


def summarize_rows(rows, language: str = "en") -> tuple[str | None, str]:
    """Returns (text, state). state: 'ok' | 'neutral' | 'minimal' | 'error'.
    Only 'error' returns text None (network/API failure)."""
    user = (db.get_profile().get("user_name") or "").strip() or "the user"
    speaker = {"user": user, "assistant": "Ame"}
    block_text = "\n\n".join(f"{speaker.get(r['role'], r['role'])}: {r['content']}"
                             for r in rows)
    lang_label = LANGUAGE_LABELS.get(language, LANGUAGE_LABELS["en"])
    for state, template in (("ok", _SUMMARY_SYSTEM),
                              ("neutral", _SUMMARY_SYSTEM_NEUTRAL)):
        try:
            summary = call_llm([
                {"role": "system", "content": template.format(lang=lang_label, user=user)},
                {"role": "user", "content": block_text},
            ], max_tokens=800, conv_id=SUMMARY_CONV_ID, reasoning=None)
        except Exception as e:
            print(f"[summarize] failed, will retry next turn "
                  f"(the messages are NOT lost, they stay unsummarized): {e}")
            return None, "error"
        if summary and summary.strip() and not _looks_like_refusal(summary):
            if state == "neutral":
                print("[summarize] the first attempt was a refusal; "
                      "summarized with the neutral framing")
            return summary.strip(), state
        print(f"[summarize] refusal/empty reply from the model ({(summary or '').strip()[:80]!r})")
    fam_name = "es" if language.startswith("es") else (language if language in _SUMMARY_FALLBACK else "en")
    minimum = _SUMMARY_FALLBACK[fam_name].format(a=rows[0]["id"], b=rows[-1]["id"], user=user)
    print("[summarize] two refusals in a row — storing a minimal summary "
          "so memory doesn't get stuck")
    return minimum, "minimal"


def _run_summarize(rows, language: str = "en") -> str | None:
    """Summarizes a batch in the configured language and stores it. Returns
    None on a network/API error, so the batch is retried later."""
    summary, state = summarize_rows(rows, language)
    if summary is None:
        return None
    db.save_summary(rows[0]["id"], rows[-1]["id"], summary)
    print(f"[summarize] summary saved for messages {rows[0]['id']}–{rows[-1]['id']} "
          f"({len(summary)} chars)")
    return summary


# Refusal phrases in the languages the summarizer answers in. The text must
# also be short: a real summary that happens to contain "I can't" is far
# longer than a refusal.
_REFUSAL_MARKERS = (
    "i can't provide", "i cannot provide", "i can't summarize",
    "i cannot summarize", "i'm not able to", "i am not able to",
    "i won't be able to", "as an ai", "i'm sorry, but",
    "no puedo proporcionar", "no puedo resumir", "no puedo ayudar",
    "no me es posible", "lo siento, pero", "como ia",
    "non posso fornire", "non posso riassumere", "mi dispiace, ma",
)
_REFUSAL_MAX_CHARS = 320


def _looks_like_refusal(summary: str) -> bool:
    """True if the text looks like a refusal rather than a summary. It needs
    both signals (a refusal phrase and brevity): a false positive loses a
    summary, a false negative puts a refusal in every turn's prompt."""
    if not summary:
        return True
    text = summary.strip()
    if len(text) > _REFUSAL_MAX_CHARS:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


# One summary at a time: two quick turns would otherwise summarize the same
# rows twice.
_summary_lock = threading.Lock()


def maybe_summarize() -> str | None:
    """Called after every turn. Summarizes only if the chosen level isn't
    'off' and there are already enough unsummarized messages. If another
    summary is already running, it returns — the next turn catches up."""
    if not _summary_lock.acquire(blocking=False):
        return None
    try:
        return _maybe_summarize()
    finally:
        _summary_lock.release()


def _maybe_summarize() -> str | None:
    settings = db.get_settings()
    level = settings.get("summary_level", "medium")
    interval = SUMMARY_LEVEL_TO_INTERVAL.get(level)
    if interval is None:  # 'off'
        return None
    rows = db.get_unsummarized_rows()
    if len(rows) < interval:
        return None
    return _run_summarize(rows, language=settings.get("language", "en"))


def force_summarize() -> str | None:
    """Manual summary ('Re-Summarize' button), regardless of the threshold.
    Waits (up to 2 min) for a background summary that's already running."""
    if not _summary_lock.acquire(timeout=120):
        return None
    try:
        rows = db.get_unsummarized_rows()
        if not rows:
            return None
        language = db.get_settings().get("language", "en")
        return _run_summarize(rows, language=language)
    finally:
        _summary_lock.release()
