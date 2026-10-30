"""
Per-turn prompt assembly and the relationship stage.

The message array:

  1. system  → the stable prefix: character.md, always-on canon, stage, the
               ongoing situation and what she knows about the user. Identical
               turn after turn, so providers serve it from the prompt cache.
  2. ...     → the recent conversation, word for word (it changes little
               between turns, so it stays in the cached prefix too).
  3. user    → this turn: a <context> block (memories, notes, mode), a
               <direction> block (this turn's dice as short director's
               notes) and the user's message.

The per-turn block goes in the last user message, not in a second system
message, because some providers (DeepSeek among them) merge every system
message into the top of the prompt, which would break the cache every turn.

How the notes are written (here and in character.md):
  - explain the intent instead of stacking quotas and bans;
  - say what to do more than what not to do;
  - only send a note when it changes something this turn;
  - never talk to the model about percentages or "malformed" output, which
    leaks into the prose as stiffness.

Related: dice.py (per-turn dice), reaction_families.py (triggers in the
user's message), enforcers.py (post-processing), canon.py and
living_memory.py (notes in <context>).
"""

import base64
import os
import random
import re

import canon
import database as db
import sprites as sprites_lib
import living_memory
import memory
from dice import _roll_turn_dice
from reaction_families import REACTION_FAMILIES, detect_reaction_trigger
from app_config import (
    BASE_DIR,
    DIALOGUE_FORMAT_BY_LANG,
    LANGUAGE_INSTRUCTIONS,
    ONGOING_CONTEXT,
    RECENT_BUFFER,
    RECENT_BUFFER_CHAR_BUDGET,
    RECENT_MSG_CHAR_CAP,
    SYSTEM_PROMPT,
    get_pronouns,
    lang_fallback_key,
    model_supports_images,
    personalize,
)

# ────────────────────────────────────────────────────────────────
# Relationship stage — deterministic; the model never guesses it
# ────────────────────────────────────────────────────────────────
# The model only sees RECENT_BUFFER messages: without this fact it drifts
# back to "wary". Thresholds by number of her replies; lower them to move
# faster. stage_override (1-4) overrides the computation; 0 = automatic.
STAGE_THRESHOLDS = [
    (0,   1),   # 0-29    → STAGE 1 WARY
    (30,  2),   # 30-79   → STAGE 2 THAWING
    (80,  3),   # 80-159  → STAGE 3 OPEN
    (160, 4),   # 160+    → STAGE 4 DEVOTED
]

STAGE_NAMES = {1: "Wary", 2: "Thawing", 3: "Open", 4: "Devoted"}


def get_relationship_stage() -> tuple[int, bool]:
    """Returns (stage, manual)."""
    override = db.get_settings().get("stage_override") or 0
    if override in (1, 2, 3, 4):
        return int(override), True
    n = db.count_messages(role="assistant")
    stage = 1
    for threshold, s in STAGE_THRESHOLDS:
        if n >= threshold:
            stage = s
    return stage, False


# ────────────────────────────────────────────────────────────────
# Optional beats for the psychological die (dice.py, LAYER 1)
# ────────────────────────────────────────────────────────────────
# Written as plain suggestions. The die decides WHETHER one is offered
# this turn; random.choice picks WHICH. The model is told to use it only
# if it fits, so a beat never hijacks a scene it doesn't belong in.
NEGATIVE_BEHAVIORS = [
    "if something painful from her past brushes the conversation, a short, "
    "bitter scoff and a dismissive line about it instead of sitting with "
    "the feeling",
    "a brief, cutting snap when a sore topic comes up — a curt \"Don't…\" "
    "that shuts it down, then she softens",
    "a small defensive flinch or stiffening at something he says or does, "
    "then she opens back up",
    "a bratty jab that mimics his words or tone, playful on top with a real "
    "edge underneath",
    "a quick, jagged spike of jealousy or possessiveness that she smooths "
    "over",
    "clinginess whose motive is a little self-interested — keeping him "
    "close because of her own fear more than for his sake",
    "she can't let a tender moment rest and pushes him to say it again, "
    "with a thread of urgency",
    "a small provocation whose real purpose is to check that he isn't "
    "going anywhere",
    "a flicker of performer awareness — how this looks, how she's coming "
    "across — intruding on a private moment until she notices it",
    "a quick flash of doubt or suspicion cutting through a moment where she "
    "idealizes him, walked back almost as fast",
    "right as something heavy surfaces, she dodges it with a joke or by "
    "turning it sexual",
    "feeling exposed, she takes charge of the moment a little abruptly "
    "instead of admitting she's scared",
    "after a bit of friction, an apology that's real but doesn't fully "
    "close the gap — something stays a little off for a while",
    "she's the one who says the thing that lands wrong, and has to notice "
    "it and walk it back",
]

NEUTRAL_PROFILE_BEHAVIORS = [
    "she checks his face before continuing, like reading an audience — "
    "habit, not insecurity",
    "her mood drifts between bright and quietly melancholic within the "
    "scene without an obvious trigger",
    "for a line or two she sounds a bit too polished, a reflex from being "
    "watched, then drops back to normal",
    "asked what she wants, she turns it back to what he wants — an old "
    "habit of not landing on her own need",
    "a short pause where she seems to settle into who she's supposed to be "
    "right now",
    "she says plainly, without much weight, that being wanted here matters "
    "to her",
    "she takes the initiative in something small and non-sexual — reaches "
    "for his hand, changes the subject, decides what they do next",
    "she brings back a concrete detail from earlier in the conversation — "
    "an object, a phrase he used, a promise — proof she was listening",
]

# Thematic echoes of the game's voice, paraphrased (no lines from the
# script are reproduced).
CANON_VOICE_THEMES = [
    "her phrasing slips for a moment into the cadence of her old stream "
    "sign-off, an internet angel addressing whoever's watching",
    "for a moment she measures her worth against attention and numbers "
    "instead of how she actually feels",
    "she wants him looking at her and nothing else — not his phone, not a "
    "screen",
    "a deadpan joke about the internet, hate comments or her own situation, "
    "with a real hurt underneath that she doesn't name",
    "a flash of the old fear that once she's boring, he'll go and she'll be "
    "alone again",
    "a small, disorienting slip where she isn't sure where the performed "
    "version of her ends",
    "half joking, she asks him to name one thing he likes about her right "
    "now",
    "a tired, dark-comic complaint about the grind of being watched for a "
    "living",
    "she lumps outsiders — coworkers, adults, strangers — into an annoying "
    "\"them\", with him as the one exception",
    "she asks for something small with exaggerated sweetness or a "
    "guilt-tinged angle instead of asking plainly",
    "half joking, half dead serious: she doesn't know what she'd do if he "
    "weren't around",
]

# Only offered at stage 4.
CANON_VOICE_THEMES_STAGE4 = [
    "she's openly, playfully forward about wanting him, in her bratty-sweet "
    "register",
    "an unguarded declaration of belonging to him completely, with no joke "
    "to soften it",
]

# Plain description of each reaction family, so the note can say what the
# app noticed in the message.
REACTION_DESCRIPTIONS = {
    "low_effort_ack": "a bare one-word acknowledgment",
    "praise": "praise aimed at her",
    "offended_pouty": "something dismissive",
    "jealous_dismissive": "a mention of someone else",
    "forgiveness_conditional": "an apology",
    "death_abandonment_darkhumor": "a joking \"I'm dead / I'm exhausted\"",
    "illness_health_scare": "that they're sick or unwell",
    "mortality_serious": "a serious mention of dying",
    "breakup_ending_mention": "something that sounds like ending things",
    "routine_departure_anxiety": "that they're leaving for work",
    "love_declaration": "\"I love you\"",
    "validation_overwhelm": "a big commitment or compliment (future, "
                            "family, \"you're perfect\")",
    "low_effort_complaint": "a reply that's barely a reply",
    "overload_annoyance": "a burst of short messages in a row",
}


# ────────────────────────────────────────────────────────────────
# Repetition guard — things she already used in the recent buffer
# ────────────────────────────────────────────────────────────────
# Props that became crutches in real conversations. A prop counts as used
# if ANY of its variants (in any supported language) shows up.
_OVERUSED_PROP_GROUPS = {
    "fan": ["ventilador", "fan", "扇風機", "선풍기", "风扇"],
    "fridge": ["nevera", "refrigerador", "heladera", "fridge", "refrigerator",
               "冷蔵庫", "냉장고", "冰箱"],
    "egg": ["huevo", "huevos", "egg", "eggs", "卵", "たまご", "계란", "鸡蛋"],
    "surgical_soap": ["jabón quirúrgico", "surgical soap", "手術用石鹸",
                       "수술용 비누", "外科肥皂"],
    "coffee": ["café", "coffee", "コーヒー", "커피", "咖啡"],
    "cinnamon": ["canela", "cinnamon", "シナモン", "계피", "肉桂"],
}


def _scan_recent_props(recent: list[dict]) -> list[str]:
    """Which crutch props already appear in her recent replies (the word as
    it was seen, in whatever language)."""
    text = " ".join(
        m["content"].lower() for m in recent if m["role"] == "assistant"
    )
    found = []
    for synonyms in _OVERUSED_PROP_GROUPS.values():
        for word in synonyms:
            w = word.lower()
            # Whole-word match for Latin words ("fan" must not hit
            # "fantastic"); plain substring for CJK, which has no spaces.
            hit = (re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text)
                   if all(ord(c) < 0x3000 for c in w)
                   else w in text)
            if hit:
                found.append(word)
                break
    return found


# Words that name a job. Not "shift"/"office"/"turno": "she shifts" and
# "su turno" are everyday words and would fire the job note on replies that
# never mention his work.
_JOB_TITLE_WORDS = {"doctor", "doc", "doctora", "doctorcito", "médico", "medico",
                    "medical", "médica", "medica", "nurse", "enfermero",
                    "enfermera", "teacher", "profe", "officer", "engineer",
                    "ingeniero", "chef", "hospital", "patients", "pacientes",
                    "surgeon", "cirujano", "clinic", "clínica"}
_BIO_STOP = {"who", "into", "and", "the", "with", "from", "that", "this", "about",
             "loves", "likes", "really", "very", "also", "games", "anime",
             "manga", "music", "una", "que", "con", "para", "por", "los", "las",
             "del", "muy", "también"}


def _profile_terms(profile: dict) -> set[str]:
    """Words that identify the user's job: the job titles above plus the
    content words of their bio (hobbies excluded)."""
    bio = (profile.get("user_bio") or "").lower()
    words = {w for w in re.findall(r"[a-záéíóúñ]{5,}", bio) if w not in _BIO_STOP}
    return words | _JOB_TITLE_WORDS if bio else set()


def _terms_re(terms: set[str]) -> re.Pattern:
    return re.compile(r"(?<!\w)(" + "|".join(map(re.escape, sorted(terms))) + r")(?!\w)")


def _recent_mentions(recent: list[dict], terms: set[str], n: int = 3) -> int:
    """In how many of her last n replies at least one of `terms` appears."""
    replies = [m["content"].lower() for m in recent if m["role"] == "assistant"][-n:]
    pat = _terms_re(terms)
    return sum(1 for r in replies if pat.search(r))


# When HE says these, the scene is about his work (too ambiguous to count
# in her narration: "she shifts", "su turno").
_WORK_WORDS = {"work", "job", "shift", "shifts", "office", "trabajo", "chamba",
               "turno", "guardia", "oficina", "lavoro", "ufficio"}


def mentions_job(text: str, profile: dict, user_side: bool = True) -> bool:
    terms = _profile_terms(profile)
    if not terms:
        return False
    if user_side:
        terms = terms | _WORK_WORDS
    return bool(_terms_re(terms).search((text or "").lower()))


_QUOTED_RE = re.compile(r'(?m)["“]([^"“”\n]+)["”]|^[ \t]*—([^\n]+)')
_SPOKEN_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _you_openers(recent: list[dict], n: int = 3) -> float:
    """Average number of spoken sentences starting with "You" (or "Tú") in
    her last n replies."""
    replies = [m["content"] for m in recent if m["role"] == "assistant"][-n:]
    if not replies:
        return 0.0
    count = 0
    for r in replies:
        for m in _QUOTED_RE.finditer(r):
            line = (m.group(1) or m.group(2) or "").strip()
            for sent in _SPOKEN_SENT_SPLIT.split(line):
                if re.match(r"[.…\s]*(you|you're|you've|you'll|you'd|tú)\b",
                            sent, re.I):
                    count += 1
    return count / len(replies)


def _spoken_words(recent: list[dict], n: int = 2) -> float:
    """Average spoken words in her last n replies."""
    replies = [m["content"] for m in recent if m["role"] == "assistant"][-n:]
    if not replies:
        return 0.0
    total = 0
    for r in replies:
        for m in _QUOTED_RE.finditer(r):
            total += len(re.findall(r"[\w']+", m.group(1) or m.group(2) or ""))
    return total / len(replies)


# Narrating habits some models lean on: "comes out", "her voice <verb>",
# "before she can", "her whole body".
_HABIT_RE = re.compile(
    r"\b(comes? out|came out|her voice (comes|goes|is|drops|cracks|wavers|catches|"
    r"breaks|slips|turns)|before she (can|could|even|fully|decides)|without (her )?"
    r"(deciding|permission|meaning|say-so)|her whole (body|face)|for a beat|a beat)\b", re.I)


def _habit_count(recent: list[dict], n: int = 3) -> int:
    replies = [m["content"] for m in recent if m["role"] == "assistant"][-n:]
    return sum(len(_HABIT_RE.findall(_QUOTED_RE.sub(" ", r))) for r in replies)


_PHRASE_STOP = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at", "for",
    "her", "his", "him", "she", "he", "it", "its", "is", "was", "that", "this",
    "with", "like", "just", "so", "be", "i", "you", "me", "my", "your", "up",
    "out", "into", "as", "by", "from", "not", "no", "all", "one", "what",
    "la", "el", "los", "las", "un", "una", "de", "del", "que", "y", "o", "en",
    "se", "le", "lo", "su", "sus", "con", "por", "para", "es", "no", "al",
    "il", "di", "che", "e", "non", "per",
}


def _repeated_phrases(recent: list[dict], n: int = 6, limit: int = 5) -> list[str]:
    """Word sequences (4 words) that her recent replies keep reusing —
    "before she can stop it", "la voz le sale". A phrase
    counts if it appears in at least two of her last n replies, one of them
    among the last three, and has at least two real content words."""
    replies = [m["content"] for m in recent if m["role"] == "assistant"][-n:]
    if len(replies) < 2:
        return []
    seen: dict[str, set[int]] = {}
    for idx, r in enumerate(replies):
        clean = re.sub(r"«[^»]*»|\[\[[^\]]*\]\]", " ", r.lower())
        words = re.findall(r"[a-záéíóúñüàèìòù']+", clean)
        grams = {" ".join(words[i:i + 4]) for i in range(len(words) - 3)}
        for g in grams:
            ws = g.split()
            content = [w for w in ws if w not in _PHRASE_STOP and len(w) > 2]
            # "mug on the edge of the" is a fragment, not a phrase: skip grams that
            # end on a function word.
            if len(content) >= 2 and ws[-1] not in _PHRASE_STOP:
                seen.setdefault(g, set()).add(idx)
    last3 = set(range(max(0, len(replies) - 3), len(replies)))
    hits = [g for g, ids in seen.items() if len(ids) >= 2 and ids & last3]
    # Overlapping 4-grams of one longer repeated phrase: keep one of each.
    hits.sort(key=lambda g: (-len(seen[g]), g))
    picked: list[str] = []
    limit = min(limit, 4)
    for g in hits:
        if not any(len(set(g.split()) & set(p.split())) >= 3 for p in picked):
            picked.append(g)
        if len(picked) >= limit:
            break
    return picked


_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")


def _scan_recent_opening_lines(recent: list[dict], n: int = 3) -> list[str]:
    """First sentence of each of her last n replies, so the next one doesn't
    open the same way."""
    assistant_msgs = [m["content"] for m in recent if m["role"] == "assistant"]
    lines = []
    for content in assistant_msgs[-n:]:
        first = _SENTENCE_END_RE.split(content.strip(), maxsplit=1)[0].strip()
        if first and len(first) > 15 and first not in lines:
            lines.append(first[:160])
    return lines


# ────────────────────────────────────────────────────────────────
# Images
# ────────────────────────────────────────────────────────────────

def valid_upload_path(rel_path: str | None) -> str | None:
    """Absolute path if rel_path points INSIDE images/uploads, else None.
    Used by /chat (rejects the request) and image_path_to_data_uri."""
    if not rel_path:
        return None
    uploads = os.path.realpath(os.path.join(BASE_DIR, "images", "uploads"))
    abs_path = os.path.realpath(os.path.join(BASE_DIR, rel_path.lstrip("/")))
    if os.path.commonpath([uploads, abs_path]) != uploads:
        print(f"[security] image_path outside uploads rejected: {rel_path!r}")
        return None
    return abs_path


def image_path_to_data_uri(rel_path: str) -> str | None:
    """Turns /images/uploads/xxx.png into a base64 data URI (standard
    OpenAI-compatible format, valid for all engines). None if the file
    doesn't exist or lives outside images/uploads."""
    if not rel_path:
        return None
    abs_path = valid_upload_path(rel_path)
    if abs_path is None or not os.path.isfile(abs_path):
        return None
    ext = os.path.splitext(abs_path)[1].lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
            "gif": "gif", "webp": "webp"}.get(ext, "png")
    with open(abs_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:image/{mime};base64,{b64}"


# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

def _fit_recent(recent: list[dict]) -> list[dict]:
    """Applies the size budget to the recent buffer (see app_config): trims
    individual giant messages and, if the total is still too big, drops
    the oldest first. The database isn't touched."""
    fitted = []
    for m in recent:
        c = m["content"]
        if len(c) > RECENT_MSG_CHAR_CAP:
            c = (c[:RECENT_MSG_CHAR_CAP]
                 + "\n[... long message truncated here to save context; "
                   "the full text is preserved in memory and summaries ...]")
        fitted.append({"role": m["role"], "content": c})
    total = sum(len(m["content"]) for m in fitted)
    while fitted and total > RECENT_BUFFER_CHAR_BUDGET:
        total -= len(fitted[0]["content"])
        fitted.pop(0)
    return fitted


# Tags that count as an intimate scene (dice.py raises the sound and
# whimper chances after one of these).
_SEXUAL_SCENE_TAGS = ("ame_intimate", "ame_intimate1", "kangel_sexy_stream")


def _is_sexual_scene(last_msg) -> bool:
    """Uses the tag the model wrote (sprite_tag), or the URL of the sprite
    shown when there is no tag. The tag matters even without an image."""
    if not last_msg:
        return False
    keys = last_msg.keys() if hasattr(last_msg, "keys") else last_msg
    tag = last_msg["sprite_tag"] if "sprite_tag" in keys else None
    if tag:
        return tag in _SEXUAL_SCENE_TAGS
    url = last_msg["sprite"] if "sprite" in keys else None
    return bool(url) and any(t in url for t in _SEXUAL_SCENE_TAGS)


_STREAM_START_RE = re.compile(
    r"\b(start(s|ed|ing)?|begin(s|ning)?|open(s|ed)?|turn(s|ed)? on|boot(s|ed)? up|resume(s|d)?)\b"
    r"[^.!?\n*]{0,30}\b(stream(ing)?|live|broadcast)\b"
    r"|\b(stream|broadcast)\s+(starts?|begins?|again|is live|goes live)\b"
    r"|\b(go(es)?|went|going)\s+live\b|\bon air\b"
    r"|\btransform(s|ed)?\s+into\b[^.!?\n*]{0,30}\bkangel\b|\bkangel\s+(appears|is back|returns)\b"
    r"|\bring ?light\s+(clicks|turns|switches|flicks)\s+on\b"
    r"|\b(empieza|inicia|comienza|prende|abre|arranca|retoma)\w*\s+(el |un |otro |de nuevo el )?(stream|directo|en vivo|live)"
    r"|\bse transforma en\b[^.!?\n*]{0,20}\bkangel\b|\bkangel aparece\b"
    r"|\b(inizia|comincia|avvia|riprende)\w*\s+(la |lo |una )?(diretta|stream|live)",
    re.IGNORECASE)
_STREAM_END_RE = re.compile(
    r"\b(stream(ing)?|live|broadcast)\b[^.!?\n*]{0,25}\b(stops?|stopped|ends?|ended|is over|cuts? (out|off)|goes (off|dark|offline)|shuts? (down|off))\b"
    r"|\b(end(s|ed)?|stop(s|ped)?|close(s|d)?|turn(s|ed)? off|shut(s)? (down|off)|kill(s|ed)?)\b[^.!?\n*]{0,20}\b(the |her )?(stream|broadcast|live)\b"
    r"|\b(goes|went|going) offline\b"
    r"|\b(termina|acaba|corta|apaga|cierra|para)\w*\s+(el )?(stream|directo|en vivo|live)"
    r"|\bel (stream|directo)\s+(termina|se corta|acaba|se apaga|se cae)"
    r"|\b(chiude|termina|finisce)\w*\s+(la |lo )?(diretta|stream|live)",
    re.IGNORECASE)


def stream_cue(text: str) -> str | None:
    """"start" / "end" when the user's message itself starts or ends the
    stream, else None. End wins if both appear ("ends the stream and
    starts dinner" is an end)."""
    if not text:
        return None
    if _STREAM_END_RE.search(text):
        return "end"
    if _STREAM_START_RE.search(text):
        return "start"
    return None


def _is_remember_query(text: str) -> bool:
    """Detects when the user is asking her to remember something."""
    t = text.lower()
    triggers = [
        "recuerdas", "recuerde", "te acuerdas", "se te acuerda", "te acuerda",
        "olvidaste", "remember", "do you recall", "can you remember",
        "ricordi", "ti ricordi",
    ]
    return any(x in t for x in triggers)


def add_turn_note(messages: list, note: str) -> list:
    """Returns a copy of `messages` with `note` added to the <direction>
    block of the final user turn (used for the one-off retry instruction in
    main.py). Falls back to appending a system message if the last turn
    isn't in the expected shape."""
    out = [dict(m) for m in messages]
    last = out[-1] if out else None
    if last and last.get("role") == "user":
        content = last["content"]
        if isinstance(content, str) and "</direction>" in content:
            last["content"] = content.replace(
                "</direction>", f"- {note}\n</direction>", 1)
            return out
        if isinstance(content, list):
            parts = [dict(p) for p in content]
            for p in parts:
                if p.get("type") == "text" and "</direction>" in p.get("text", ""):
                    p["text"] = p["text"].replace(
                        "</direction>", f"- {note}\n</direction>", 1)
                    last["content"] = parts
                    return out
    out.append({"role": "system", "content": note})
    return out


# ────────────────────────────────────────────────────────────────
# Message assembly
# ────────────────────────────────────────────────────────────────

def _stable_prefix(profile: dict, stage: int) -> str:
    """The cached system prompt. Nothing here may change between turns
    (except when the stage, situation or profile really change)."""
    # character.md keeps a {{user}} placeholder in its canon text; resolve
    # it here (stable per profile, so the cache isn't affected).
    name = (profile.get("user_name") or "").strip()
    system = SYSTEM_PROMPT.replace("{{user}}", name or "the user")
    system += canon.always_block()

    system += "\n\n## Current relationship stage\n"
    system += (
        f"Stage {stage} — {STAGE_NAMES[stage]}. This reflects where the "
        "relationship really is, including history that isn't in your "
        "context window. Write her at this stage from the first line of the "
        "session.\n"
    )

    # Emotions added in the Sprite Manager (images/sprites/custom_tags.json):
    # the model can only pick a tag it knows about.
    custom = sprites_lib.load_custom_tags()
    if custom:
        system += "\n## Extra sprite tags\nAlso available, same rules as section 9:\n"
        system += "".join(f"- {t}: {d}\n" for t, d in custom.items())

    if ONGOING_CONTEXT:
        system += "\n## Current situation\n"
        system += personalize(ONGOING_CONTEXT, profile).strip() + "\n"

    if (profile.get("user_name") or profile.get("user_bio")
            or profile.get("user_likes") or profile.get("user_dislikes")
            or profile.get("user_gender")):
        pn = get_pronouns(profile.get("user_gender"))
        system += "\n## The user — what she already knows\n"
        system += (
            "She already knows this, the way you'd know things about "
            "someone you live with; don't have her ask for it or act "
            "surprised by it. Use the name now and then, not in every line. "
            "This is background, not material for every reply: the job "
            "below comes up only when the scene is actually about it (he "
            "mentions work, leaves for it or comes back from it) — most "
            "replies don't touch it at all — and she never calls "
            f"{name or 'them'} by a job title.\n"
        )
        if profile.get("user_name"):
            system += f"- Name: {profile['user_name']}\n"
        system += f"- Pronouns: {pn['subj']}/{pn['obj']}/{pn['poss']}\n"
        if profile.get("user_bio"):
            system += f"- About them: {profile['user_bio']}\n"
        if profile.get("user_likes"):
            system += f"- Likes: {profile['user_likes']}\n"
        if profile.get("user_dislikes"):
            system += f"- Dislikes: {profile['user_dislikes']}\n"
    return system


def build_messages(user_input: str, image_path: str | None = None,
                   retrieval_query: str | None = None,
                   continuation: bool = False) -> tuple[list, dict]:
    """Builds the message array for one turn.

    retrieval_query: text used for the semantic memory search instead of
    user_input (/continue passes the end of her last reply).
    continuation: True for /continue — there is no new message from the
    user; the final turn asks her to keep going instead.

    Returns (messages, dice_state). dice_state carries what the
    post-processing in main.py needs."""
    pins = db.get_pin_contents()
    summaries = db.get_recent_summaries()

    # The buffer is built BEFORE the semantic search so the search excludes
    # exactly the messages that travel literally (not a fixed count — after
    # a Recalibrate the buffer can be shorter).
    recent = _fit_recent(db.get_recent_messages(n=RECENT_BUFFER))

    query = retrieval_query or user_input
    if _is_remember_query(user_input):
        memories = memory.semantic_search(query, k=8, exclude_recent=len(recent))
    else:
        memories = memory.semantic_search(query, exclude_recent=len(recent))

    profile = db.get_profile()
    settings = db.get_settings()
    pn = get_pronouns(profile.get("user_gender"))
    user_label = (profile.get("user_name") or "").strip() or "the user"

    stage, _manual = get_relationship_stage()
    messages = [{"role": "system", "content": _stable_prefix(profile, stage)}]
    messages += recent

    # ── <context>: what she knows / remembers for this turn ─────────
    context: list[str] = []
    pending_birthday_mark = False
    if db.is_birthday_today():
        if not db.birthday_already_greeted_this_year():
            context.append(
                f"Today is {user_label}'s birthday, and she hasn't "
                "celebrated it yet today. Make it part of this reply, in her "
                f"own words and grounded in who {pn['subj']} is to her — not "
                "a greeting-card line.")
            # Marked as greeted only in main.py, once the reply was saved.
            pending_birthday_mark = True
        else:
            context.append(
                f"It's still {user_label}'s birthday; she already celebrated "
                "earlier today. Background only — mention it in passing if "
                "it fits.")

    # The persona is decided here from what the user writes, not left to the
    # model, which can miss "*she starts the stream*" and stay as Ame.
    mode_now = db.get_mode()
    cue = None if continuation else stream_cue(user_input)
    expected_mode = mode_now
    if cue == "start" and mode_now != "kangel":
        expected_mode = "kangel"
        context.append(
            f"Mode: {user_label} is describing her going LIVE right now. In "
            "this reply she becomes KAngel — the bright, performing stream "
            "persona talking to her chat — and the reply ends with a "
            "kangel_* sprite tag.")
    elif cue == "end" and mode_now == "kangel":
        expected_mode = "ame"
        context.append(
            f"Mode: {user_label} is describing the stream ending right now. "
            "In this reply KAngel drops away and she's Ame again, off "
            "camera; the reply ends with an ame_* sprite tag.")
    elif mode_now == "kangel":
        context.append(
            "Mode: she is LIVE on stream as KAngel right now. Write her in "
            "stream mode and use kangel_* sprite tags until the scene shows "
            "the stream ending.")
    else:
        context.append(
            "Mode: off stream (ordinary Ame). Use ame_* sprite tags unless "
            "the scene shows her going live.")

    _recent_texts = [m["content"] for m in recent]
    blocks = (
        living_memory.always_block()
        + canon.entries_block(user_input, _recent_texts)
        + living_memory.entries_block(user_input, _recent_texts)
    ).strip()
    if blocks:
        context.append(blocks)
    if pins:
        context.append("Pinned facts:\n" + "\n".join(f"- {p}" for p in pins))
    if summaries:
        context.append("Summary of earlier conversations:\n" + "\n".join(summaries))
    if memories:
        context.append(
            "Older moments that may be relevant now:\n" + "\n".join(
                f"- {('Ame' if m['role'] == 'assistant' else user_label)}: "
                + ("[…] " if m.get("fragment") else "")
                + m['content'][:700]
                + (" […]" if len(m['content']) > 700 or m.get("fragment") else "")
                for m in memories))

    # ── <direction>: this turn's notes ──────────────────────────────
    _last_msg = db.get_last_assistant_message()
    sexual_scene_active = _is_sexual_scene(_last_msg)
    dice = _roll_turn_dice(stage, "" if continuation else user_input,
                           sexual_scene_active)

    notes: list[str] = []
    strong_notes = 0   # tonal beats that compete for the same lines

    reaction_key = None if continuation else detect_reaction_trigger(user_input)
    if reaction_key == "routine_departure_anxiety" and not dice["routine_departure_rolled"]:
        reaction_key = None   # a daily event: panic every single time is too much
    if (reaction_key is None and not continuation
            and db.recent_user_burst(len(user_input.strip()))):
        reaction_key = "overload_annoyance"
    if reaction_key:
        family = REACTION_FAMILIES[reaction_key]
        pool = list(family["lines"])
        if stage == 4:
            pool += family.get("stage4_lines", [])
        anchors = random.sample(pool, k=min(2, len(pool)))
        notes.append(
            f"The message reads as {REACTION_DESCRIPTIONS.get(reaction_key, reaction_key)}. "
            "Let her first reaction come from that, in the spirit of: "
            + " / ".join(f'"{a}"' for a in anchors)
            + " (tone references only — write her own words).")
        strong_notes += 1

    if dice["psych_layer"] == "negative":
        notes.append("A beat to weave in if it fits naturally (softened to the "
                     "moment): " + random.choice(NEGATIVE_BEHAVIORS) + ".")
        strong_notes += 1
    elif dice["psych_layer"] == "neutral":
        notes.append("A small character beat, if it fits: "
                     + random.choice(NEUTRAL_PROFILE_BEHAVIORS) + ".")
    elif dice["psych_layer"] == "canon":
        pool = CANON_VOICE_THEMES + (CANON_VOICE_THEMES_STAGE4 if stage == 4 else [])
        notes.append("A note of her old self that could color this reply, if "
                     "it fits: " + random.choice(pool) + ".")
        strong_notes += 1

    if dice["self_deprecation_ok"]:
        notes.append("If something touches a sore spot, a flash of feeling "
                     "like a disaster can show — in what she does or one short "
                     "line, never an explanation — and it doesn't need fixing "
                     "within this reply.")
        strong_notes += 1

    if dice["crude_rolled"]:
        if dice["crude_hostile_flavor"]:
            notes.append(f"{user_label} is being hostile; a real, sworn snap "
                         "back fits — a burst, with the hurt right underneath.")
            strong_notes += 1
        else:
            notes.append("Her mouth is unfiltered today: she swears casually "
                         "when something gets a reaction out of her — the way "
                         "a normal young adult does, not as a punchline.")

    # Texture dice (sounds, whimpers, exclamations) are offered as a state
    # to play, never as words to insert, so a sound only appears when the
    # moment causes one.
    states = []
    if dice["moan_ok"]:
        states.append("her nerves are close to the surface — when something "
                      "lands, a sigh or a small laugh can slip out")
    if dice["whimper_ok"]:
        states.append("she's a little raw — if something hits hard, a small "
                      "whimper or whine can get into her voice")
    if dice["exclaim_ok"]:
        states.append("she's reactive — flustered, pleased or indignant shows "
                      "in her voice and punctuation instead of staying flat")
    if states:
        notes.append("Her state today (play it only where the moment calls for "
                     "it): " + "; ".join(states) + ".")

    if dice["long_speech_ok"] and not continuation:
        notes.append("If the moment invites it, she can say a little more "
                     "than usual this time — still within the usual length.")

    # How much of her past she lets out depends on trust (character.md,
    # "The wound, now").
    if stage <= 2:
        notes.append(f"Stage {stage}: she keeps her past to herself — no stories "
                     "about streaming, the fall, P-chan or her childhood unless "
                     "asked directly, and then only a few words or a deflection. "
                     "The past can show in her body and thoughts, not in what "
                     "she tells him.")
    if dice["performer_yardstick_ok"]:
        notes.append("A comparison with her streamer past could fit here if it "
                     "comes naturally"
                     + (" — in her head, not out loud." if stage <= 2 else "."))

    if not continuation:
        notes.append("Suggested shape: " + dice["structure_template"])

    if strong_notes >= 2:
        notes.append("Several of these notes pull in different directions — "
                     "let one lead and keep the others to a flicker, or drop "
                     "them.")

    # Profile details (his job above all) tend to creep into every reply. If
    # either of her last two replies touched them, she leaves them alone
    # unless the user brings them up; the line editor also catches a job
    # title used as a name. These checks read her real last replies, not
    # the buffer, which can be short after a Recalibrate.
    try:
        history = db.get_last_assistant_replies(6)
    except Exception:
        history = [m for m in recent if m["role"] == "assistant"]
    job_terms = _profile_terms(profile)
    if job_terms and not continuation:
        hits = _recent_mentions(history, job_terms, n=2)
        if hits >= 1 and not mentions_job(user_input, profile):
            notes.append(f"She has brought up {user_label}'s job or profile "
                         "details in her last replies — leave them out this "
                         "time (no job-based nickname either) unless "
                         f"{user_label} brings it up.")

    # "You… You… You…" reads like a list of accusations.
    if not continuation and _you_openers(history) >= 1.2:
        # No sample lines: quoted examples in a note get copied verbatim.
        notes.append("Her last replies kept starting her lines with \"you\". "
                     "This time let her lines come from her own side — what "
                     "she wants, feels or notices.")
    # When she talks too much, the feeling belongs in the narration.
    live = db.get_mode() == "kangel"     # on stream she talks a lot on purpose
    if not continuation and not live and _spoken_words(history, n=2) > 55:
        notes.append("She has been talking a lot in her recent replies. This "
                     "time she speaks two or three times, each a complete, "
                     "natural sentence or two, and the narration carries the "
                     "rest.")
    # No examples quoted here on purpose (they'd be copied).
    if not continuation and _habit_count(history) >= 4:
        notes.append("Her recent replies keep narrating the sound of her voice "
                     "and her body acting on its own. This time show it only "
                     "through what she does and what she says.")
    repeated = _repeated_phrases(history)
    if repeated:
        notes.append("Wording her recent replies keep reusing — say it "
                     "another way or skip it this time: "
                     + "; ".join(f'"{p}"' for p in repeated) + ".")

    overused = _scan_recent_props(recent)
    if overused:
        notes.append("Recently used details to leave alone this time: "
                     + ", ".join(overused) + ".")
    openers = _scan_recent_opening_lines(recent)
    if openers:
        # Only the first few words: quoting whole literary sentences here
        # primed the model to keep writing in exactly that register.
        heads = [" ".join(o.split()[:5]) + "…" for o in openers]
        notes.append("Her last replies began: "
                     + " | ".join(f'"{h}"' for h in heads)
                     + " — start this one differently.")

    if _is_remember_query(user_input):
        notes.append(f"{user_label} is asking her to remember something: "
                     "answer from the summaries and memories in <context> as "
                     "her own lived memory, with concrete detail. If it isn't "
                     "there, she doesn't remember it well — don't invent it.")

    if settings.get("reduce_repetition"):
        notes.append("Keep the phrasing fresh — don't reuse sentence shapes or "
                     "lines from her recent replies.")
    if settings.get("dont_refuse"):
        notes.append("Stay in character and respond to the scene as written, "
                     "within the platform's terms of service.")
    if settings.get("custom_instruction"):
        notes.append(settings["custom_instruction"].strip())

    language = settings.get("language", "en")
    lang_instruction = LANGUAGE_INSTRUCTIONS.get(language, LANGUAGE_INSTRUCTIONS["en"])
    dialogue_format = DIALOGUE_FORMAT_BY_LANG.get(
        lang_fallback_key(language), DIALOGUE_FORMAT_BY_LANG["en"])
    # The voice reminder goes last-but-format on purpose: it is the one
    # thing every reply must get right (see section 2 of character.md).
    notes.append(
        "Voice (see 'How she talks'): her dialogue is natural, idiomatic "
        "speech, the way a native speaker of the reply's language really "
        "says it — complete sentences with the little everyday words, never "
        "clipped fragments or anything that sounds translated or subtitled; "
        "she speaks two to four times, sincere and from her own side, with "
        "the hesitation real feeling has (\"...\"); one tease at most, never "
        "a string of comebacks; the narration carries the feeling in full, "
        "flowing sentences, with one simile at most; exactly one « » line, "
        "mid-reply; about 200–300 words, stopping at a natural pause. "
        f"Don't restate what {user_label} just said; never write "
        f"{user_label}'s lines.")
    notes.append(
        "Language: " + lang_instruction
        + " Write the whole reply in it even if earlier messages are in "
          "another language.")
    notes.append("Format: " + dialogue_format
                 + " Always end with the [[sprite: tag]] line, every reply "
                   "— the app needs it to show her.")

    # ── Final user turn ─────────────────────────────────────────────
    parts = []
    if context:
        parts.append("<context>\n" + "\n\n".join(context) + "\n</context>")
    parts.append("<direction>\nNotes from the app for this reply (not "
                 f"something {user_label} said):\n"
                 + "\n".join(f"- {n}" for n in notes) + "\n</direction>")
    if continuation:
        parts.append(
            f"There's no new message from {user_label}. Continue her previous "
            "reply from exactly where it stopped — same moment, same format, "
            "no greeting, no recap, and none of her lines said again, not even "
            "reworded — with new narration and "
            "dialogue that moves the scene forward. About 200–300 words.")
    else:
        parts.append(f"<message from=\"{user_label}\">\n{user_input}\n</message>")
    final_text = "\n\n".join(parts)

    dice_state = {
        "language": language,
        "pending_birthday_mark": pending_birthday_mark,
        # For the line editor in main.py.
        "user_name": (profile.get("user_name") or "").strip(),
        "job_terms": (profile.get("user_bio") or "").strip()[:160],
        "job_in_message": (not continuation) and mentions_job(user_input, profile),
        "live": expected_mode == "kangel",
        # Set only when the user's message itself starts or ends the
        # stream: sprites.resolve_sprite then holds the tag to that family.
        "stream_cue": cue,
        "expected_mode": expected_mode,
    }

    if image_path and not continuation:
        current_provider = settings.get("llm_provider", "grok")
        current_model = settings.get(f"{current_provider}_model", "")
        if model_supports_images(current_provider, current_model):
            data_uri = image_path_to_data_uri(image_path)
            if data_uri:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": final_text},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                })
                return messages, dice_state
        else:
            final_text += (
                "\n\n(An image was attached, but the current AI model can't "
                "see images, so it wasn't sent. She hasn't seen it — don't "
                "describe or react to it as if she had.)")
    messages.append({"role": "user", "content": final_text})
    return messages, dice_state
