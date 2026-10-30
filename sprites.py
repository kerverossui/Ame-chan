"""
The emotion sprite of each reply, and the sprite library behind the
Sprite Manager.

Every reply ends with a [[sprite: tag]] tag. Extraction is tolerant
([sprite: x], (sprite: x), a bare "sprite: x" at the end, any case). If the
model skipped the tag, a small call to a cheap model labels the reply, so a
reply always gets an emotion when one fits.
"""

import json
import os
import random
import re
import shutil
import threading
import time

import database as db
from app_config import BASE_DIR
from llm import call_llm

# What each emotion is for (shown in the Sprite Manager; custom emotions
# also reach the prompt, see prompting._stable_prefix).
BUILTIN_SPRITES = {
    # ── Ame (default mode, off-stream) ──
    "ame_idle": "Neutral, everyday conversation",
    "ame_happy": "Genuinely pleased, warm, light-hearted",
    "ame_sad": "Sad, hurt, down",
    "ame_devastated": "Severe emotional collapse, crying hard",
    "ame_surprised": "Caught off guard, shocked, startled",
    "ame_stressed": "Anxious, overwhelmed, on edge",
    "ame_angry": "Annoyed, irritated, sharp",
    "ame_sleepy": "Tired, just woke up, low energy",
    "ame_thinking": "Reflective, quietly processing",
    "ame_crying": "Crying (softer than devastated)",
    "ame_selfie": "Playful, showing herself off a little",
    "ame_affectionate": "Tender, caring, not sexual",
    "ame_intimate": "Sexual or deeply intimate scene",
    "ame_intimate1": "Visual variant of ame_intimate (the app picks it at random)",
    "ame_out_happy": "Happy while outdoors, away from the apartment (plays once)",
    "ame_eating": "Eating or sharing a meal, any mood",
    "ame_nod": "A wordless yes",
    "ame_texting": "Ordinary phone moment",
    "ame_texting_mad": "On her phone and visibly annoyed",
    "ame_tv": "Relaxed downtime watching something",
    # ── KAngel (ONLY in stream mode) ──
    "kangel_idle": "Neutral streaming moment",
    "kangel_happy": "Bright, cheerful on-stream energy",
    "kangel_waving": "Greeting viewers, saying hi or bye",
    "kangel_flirty": "Flirting with viewers",
    "kangel_crying": "Crying on stream, still performing",
    "kangel_scared": "Startled or unsettled on stream",
    "kangel_angry": "Sharp with hecklers, still KAngel",
    "kangel_breakdown": "Full on-stream breakdown",
    "kangel_praying": "Thanking viewers, grateful 'bless' moment",
    "kangel_explaining": "Explaining something to chat",
    "kangel_sexy_stream": "Explicitly sexy stream content",
}

SPRITES_DIR = os.path.join(BASE_DIR, "images", "sprites")
CUSTOM_TAGS_FILE = os.path.join(SPRITES_DIR, "custom_tags.json")
SPRITE_EXTS = (".gif", ".png", ".webp", ".jpg", ".jpeg")
TAG_RE = re.compile(r"^(ame|kangel)_[a-z0-9_]{1,30}$")


def family_of(tag: str) -> str:
    return "kangel" if tag.startswith("kangel_") else "ame"


def load_custom_tags() -> dict:
    """Emotions added from the Sprite Manager: {tag: description}."""
    try:
        with open(CUSTOM_TAGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {t: str(d) for t, d in data.items() if TAG_RE.match(t) and t not in BUILTIN_SPRITES}
    except (OSError, ValueError, AttributeError):
        return {}


# tag → default relative path, built-in and custom. Replaced as a whole when
# the Sprite Manager changes it, so a turn reading it never sees it half
# built; read it as sprites.SPRITE_CATALOG.
SPRITE_CATALOG: dict[str, str] = {}

# Sprite Manager changes run one at a time.
_EDIT_LOCK = threading.Lock()


def reload_catalog() -> None:
    global SPRITE_CATALOG
    SPRITE_CATALOG = {
        tag: os.path.join("images", "sprites", family_of(tag), tag + ".gif")
        for tag in list(BUILTIN_SPRITES) + list(load_custom_tags())}


reload_catalog()

STRONG_EMOTION_TAGS = {
    "ame_devastated", "ame_crying", "ame_angry", "ame_surprised", "ame_intimate",
    "ame_intimate1", "ame_texting_mad",
    "kangel_breakdown", "kangel_crying", "kangel_angry", "kangel_scared",
    "kangel_sexy_stream",
}


# ── Fallback classifier ──
SPRITE_FALLBACK_ENABLED = True
# Cheap model for the fallback classifier, from the active engine.
SPRITE_FALLBACK_MODEL_BY_PROVIDER = {
    "grok": "grok-3-mini",
    "qwen": "qwen-turbo",
    "deepseek": "deepseek-v4-flash",
    "gpt": "gpt-5.6-luna",
}
SPRITE_FALLBACK_CONV_ID = "ame-app-local-sprite-classifier"

# ────────────────────────────────────────────────────────────────
# Tolerant extraction
# ────────────────────────────────────────────────────────────────

# Canonical format anywhere in the text: [[sprite: tag]]
_TAG_ANYWHERE_RE = re.compile(
    r"[\[\(\{]{1,2}\s*sprite\s*[:=]\s*([A-Za-z_]+)\s*[\]\)\}]{1,2}", re.IGNORECASE
)
# Loose variant at the end of the text: "sprite: tag" with or without
# brackets, tolerating trailing punctuation/spaces.
_TAG_AT_END_RE = re.compile(
    r"[\[\(\{]{0,2}\s*sprite\s*[:=]\s*([A-Za-z_]+)\s*[\]\)\}]{0,2}[\s.\-*_`]*$",
    re.IGNORECASE,
)


def _normalize_tag(raw: str) -> str | None:
    tag = raw.strip().lower()
    return tag if tag in SPRITE_CATALOG else None


def _variant_re(tag: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(tag)}(?:--v(\d+))?(\.gif|\.png|\.webp|\.jpe?g)$", re.I)


def files_for(tag: str) -> list[str]:
    """Image files for an emotion: `tag.ext` plus variants `tag--v2.ext`…
    (file names, in the family folder)."""
    folder = os.path.join(SPRITES_DIR, family_of(tag))
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    rx = _variant_re(tag)
    found = [n for n in names if rx.match(n)]
    return sorted(found, key=lambda n: int(rx.match(n).group(1) or 1))


def file_url(tag: str, name: str) -> str:
    path = os.path.join(SPRITES_DIR, family_of(tag), name)
    try:
        v = int(os.path.getmtime(path))
    except OSError:
        v = 0
    # ?v= changes when a file is replaced, so the browser doesn't keep
    # showing the old image from its cache.
    return f"/images/sprites/{family_of(tag)}/{name}?v={v}"


def tag_to_url(tag: str | None) -> str | None:
    """A servable URL for the emotion — one of its variants at random — or
    None if no image has been added yet (nothing breaks: no sprite)."""
    if not tag or tag not in SPRITE_CATALOG:
        return None
    files = files_for(tag)
    return file_url(tag, random.choice(files)) if files else None


_URL_NAME_RE = re.compile(r"/images/sprites/(ame|kangel)/([a-z0-9_]+?)(?:--v\d+)?\.[a-z]+(?:\?.*)?$", re.I)


def fix_url(url: str | None) -> str | None:
    """Old messages keep the URL of the sprite they showed. If that file
    was replaced (another format) or removed in the Sprite Manager, point
    them to the emotion's current image instead of a broken one."""
    if not url:
        return url
    m = _URL_NAME_RE.search(url)
    if not m:
        return url
    path = os.path.join(SPRITES_DIR, m.group(1), url.split("/")[-1].split("?")[0])
    if os.path.exists(path):
        return url
    files = files_for(m.group(2))
    return file_url(m.group(2), files[0]) if files else None


# ── Sprite Manager operations ──────────────────────────────────────

MAX_SPRITE_BYTES = 15 * 1024 * 1024
_MAGIC = {b"GIF87a": ".gif", b"GIF89a": ".gif", b"\x89PNG\r\n\x1a\n": ".png",
          b"\xff\xd8\xff": ".jpg"}


def image_ext(data: bytes) -> str | None:
    """The real format from the file's first bytes (not its name)."""
    for magic, ext in _MAGIC.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _trash(tag: str, name: str) -> None:
    """Removed images go to images/sprites/_trash/, never deleted outright."""
    src = os.path.join(SPRITES_DIR, family_of(tag), name)
    dst_dir = os.path.join(SPRITES_DIR, "_trash")
    os.makedirs(dst_dir, exist_ok=True)
    if not os.path.exists(src):
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst, n = os.path.join(dst_dir, f"{stamp}_{name}"), 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(dst_dir, f"{stamp}-{n}_{name}")
    shutil.move(src, dst)


def _save_sprite_locked(tag: str, data: bytes, mode: str) -> str:
    if tag not in SPRITE_CATALOG:
        raise ValueError("Unknown emotion")
    if len(data) > MAX_SPRITE_BYTES:
        raise ValueError(f"Image too large (max {MAX_SPRITE_BYTES // 1024 // 1024} MB)")
    ext = image_ext(data)
    if not ext:
        raise ValueError("Not a GIF, PNG, WebP or JPEG image")
    folder = os.path.join(SPRITES_DIR, family_of(tag))
    os.makedirs(folder, exist_ok=True)
    existing = files_for(tag)
    if mode == "replace" or not existing:
        for name in existing:
            _trash(tag, name)
        name = tag + ext
    else:
        rx = _variant_re(tag)
        n = max(int(rx.match(x).group(1) or 1) for x in existing) + 1
        name = f"{tag}--v{n}{ext}"
    tmp = os.path.join(folder, "." + name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(folder, name))
    return name


def _delete_sprite_file_locked(tag: str, name: str) -> None:
    if name not in files_for(tag):
        raise ValueError("No such image for this emotion")
    _trash(tag, name)


def _write_custom(tags: dict) -> None:
    os.makedirs(SPRITES_DIR, exist_ok=True)
    tmp = CUSTOM_TAGS_FILE + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tags, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CUSTOM_TAGS_FILE)


def _add_custom_tag_locked(tag: str, description: str) -> None:
    tag = tag.strip().lower()
    if not TAG_RE.match(tag):
        raise ValueError("The name must start with ame_ or kangel_ and use only a-z, 0-9 and _")
    if tag in SPRITE_CATALOG:
        raise ValueError("That emotion already exists")
    description = " ".join(description.split())[:160]
    if not description:
        raise ValueError("Describe when she should use it")
    tags = load_custom_tags()
    tags[tag] = description
    _write_custom(tags)
    reload_catalog()


def _remove_custom_tag_locked(tag: str) -> None:
    tags = load_custom_tags()
    if tag not in tags:
        raise ValueError("Only emotions you added can be removed")
    for name in files_for(tag):
        _trash(tag, name)
    del tags[tag]
    _write_custom(tags)
    reload_catalog()


def save_sprite(tag: str, data: bytes, mode: str = "replace") -> str:
    """mode "replace": this image becomes the emotion's only one (the old
    ones go to _trash). mode "add": one more variant, picked at random."""
    with _EDIT_LOCK:
        return _save_sprite_locked(tag, data, mode)


def delete_sprite_file(tag: str, name: str) -> None:
    with _EDIT_LOCK:
        _delete_sprite_file_locked(tag, name)


def add_custom_tag(tag: str, description: str) -> None:
    with _EDIT_LOCK:
        _add_custom_tag_locked(tag, description)


def remove_custom_tag(tag: str) -> None:
    with _EDIT_LOCK:
        _remove_custom_tag_locked(tag)


def manager_catalog() -> list[dict]:
    custom = load_custom_tags()
    out = []
    for tag in SPRITE_CATALOG:
        files = files_for(tag)
        out.append({
            "tag": tag,
            "family": family_of(tag),
            "description": BUILTIN_SPRITES.get(tag) or custom.get(tag, ""),
            "custom": tag in custom,
            "files": [{"name": n, "url": file_url(tag, n),
                       "bytes": os.path.getsize(os.path.join(SPRITES_DIR, family_of(tag), n))}
                      for n in files],
        })
    return out


def extract_sprite(reply: str) -> tuple[str, str | None]:
    """Finds the sprite tag in the raw reply, removes it from the visible
    text and returns (clean_text, tag_or_None)."""
    text = reply.rstrip()

    # 1) Bracketed format, in any position (the model sometimes
    #    writes narration after the tag).
    matches = list(_TAG_ANYWHERE_RE.finditer(text))
    if matches:
        tag = _normalize_tag(matches[0].group(1))
        for m in reversed(matches):          # remove every occurrence
            text = text[:m.start()] + text[m.end():]
        return text.strip(), tag

    # 2) Loose variant stuck at the end ("sprite: ame_happy").
    m = _TAG_AT_END_RE.search(text)
    if m and (len(text) - m.start()) <= 60:  # only if it really is the tail
        tag = _normalize_tag(m.group(1))
        return text[:m.start()].rstrip(), tag

    return text, None


# ────────────────────────────────────────────────────────────────
# Fallback classifier
# ────────────────────────────────────────────────────────────────

def mode_of_tag(tag: str) -> str:
    return "kangel" if tag.startswith("kangel_") else "ame"


# When the user's own message starts or ends the stream, the tag must be
# in the matching family; an emotion from the wrong family is mapped to its
# closest counterpart instead of being thrown away.
AME_TO_KANGEL = {
    "ame_idle": "kangel_idle", "ame_happy": "kangel_happy", "ame_sad": "kangel_crying",
    "ame_devastated": "kangel_breakdown", "ame_surprised": "kangel_scared",
    "ame_stressed": "kangel_scared", "ame_angry": "kangel_angry",
    "ame_sleepy": "kangel_idle", "ame_thinking": "kangel_explaining",
    "ame_crying": "kangel_crying", "ame_selfie": "kangel_waving",
    "ame_affectionate": "kangel_flirty", "ame_intimate": "kangel_sexy_stream",
    "ame_intimate1": "kangel_sexy_stream", "ame_out_happy": "kangel_happy",
    "ame_eating": "kangel_happy", "ame_nod": "kangel_happy",
    "ame_texting": "kangel_idle", "ame_texting_mad": "kangel_angry", "ame_tv": "kangel_idle",
}
KANGEL_TO_AME = {
    "kangel_idle": "ame_idle", "kangel_happy": "ame_happy", "kangel_waving": "ame_happy",
    "kangel_flirty": "ame_affectionate", "kangel_crying": "ame_crying",
    "kangel_scared": "ame_stressed", "kangel_angry": "ame_angry",
    "kangel_breakdown": "ame_devastated", "kangel_praying": "ame_thinking",
    "kangel_explaining": "ame_thinking", "kangel_sexy_stream": "ame_intimate",
}


def to_family(tag: str, mode: str) -> str:
    if mode == "kangel" and not tag.startswith("kangel_"):
        return AME_TO_KANGEL.get(tag, "kangel_happy")
    if mode == "ame" and tag.startswith("kangel_"):
        return KANGEL_TO_AME.get(tag, "ame_idle")
    return tag


def classify_sprite_fallback(reply_text: str, current_mode: str = "ame",
                             strict: bool = False) -> str | None:
    """Labels the reply's emotion with a cheap model when the main model
    skipped the tag. The current mode (ame/kangel) is a prior: it only
    crosses family if the text shows a stream starting or ending. Errors
    are swallowed; a reply without a sprite beats no reply."""
    if not SPRITE_FALLBACK_ENABLED:
        return None
    try:
        family = "kangel_" if current_mode == "kangel" else "ame_"
        tags = ", ".join(t for t in SPRITE_CATALOG if not strict or t.startswith(family))
        if strict:
            mode_hint = ("She is " + ("LIVE ON STREAM as KAngel" if current_mode == "kangel"
                                      else "off stream as Ame") + " in this reply.")
        elif current_mode == "kangel":
            mode_hint = (
                "She is currently LIVE ON STREAM as KAngel, so default to "
                "kangel_* tags. Use an ame_* tag ONLY if the text clearly "
                "shows the stream ending and her dropping the persona."
            )
        else:
            mode_hint = (
                "She is currently OFF-STREAM as Ame, so default to ame_* "
                "tags. Use a kangel_* tag ONLY if the text clearly shows "
                "her going live / streaming as KAngel."
            )
        current_provider = db.get_settings().get("llm_provider", "grok")
        fallback_model = SPRITE_FALLBACK_MODEL_BY_PROVIDER.get(current_provider, "grok-3-mini")
        raw = call_llm(
            [
                {"role": "system", "content": (
                    "You label the emotional beat of a roleplay reply written "
                    "by the character Ame. Answer with exactly ONE tag from "
                    "this closed list and nothing else: " + tags + ". "
                    + mode_hint + " Base the label on the FINAL emotional "
                    "beat of the text."
                )},
                {"role": "user", "content": reply_text[-1500:]},
            ],
            # Room for models that spend part of the budget before answering.
            max_tokens=500,
            temperature=0.0,
            conv_id=SPRITE_FALLBACK_CONV_ID,
            model=fallback_model,
            reasoning=None,                # no reasoning_effort for the classifier
        )
        # Accept the output even if it comes with text around it
        for tag in SPRITE_CATALOG:
            if re.search(rf"\b{tag}\b", raw, re.IGNORECASE):
                print(f"[sprite-fallback] main model skipped the tag; "
                      f"classifier picked: {tag}")
                return tag
    except Exception as e:
        print(f"[sprite-fallback] failed harmlessly: {e}")
    return None


# ────────────────────────────────────────────────────────────────
# Visibility
# ────────────────────────────────────────────────────────────────

def decide_sprite_visibility(tag: str | None, url: str | None) -> str | None:
    """Counts the turn and returns the URL to show: whenever there is a tag
    and an image for it."""
    show = bool(tag and url)
    db.bump_assistant_turn(sprite_shown=show)
    return url if show else None


def _maybe_swap_intimate_variant(tag: str | None) -> str | None:
    """ame_intimate1 is a visual variant of ame_intimate: the coin is flipped
    here, since a model can't be random on request."""
    if tag == "ame_intimate":
        return random.choice(["ame_intimate", "ame_intimate1"])
    return tag


def resolve_sprite(raw_reply: str, stream_cue: str | None = None,
                   expected_mode: str | None = None) -> tuple[str, str | None, str | None]:
    """Full pipeline for a reply: extract → fallback if missing → variant →
    mode → URL. Returns (clean_text, sprite_url_or_None, tag_or_None).

    The tag is returned even without an image: it carries the persona and
    the intimate-scene signal for the next turn. The mode follows every tag
    for the same reason."""
    clean_text, tag = extract_sprite(raw_reply)
    # stream_cue: the user's message itself started/ended the stream
    # (prompting.stream_cue) — then the family is not up for debate.
    forced = expected_mode if stream_cue else None
    if tag is None:
        tag = classify_sprite_fallback(clean_text,
                                       current_mode=forced or expected_mode or db.get_mode(),
                                       strict=bool(forced))
    if tag and forced and mode_of_tag(tag) != forced:
        fixed = to_family(tag, forced)
        print(f"[sprite] the stream {stream_cue}s this turn: {tag} → {fixed}")
        tag = fixed
    tag = _maybe_swap_intimate_variant(tag)
    if tag:
        db.set_mode(mode_of_tag(tag))
    url = tag_to_url(tag)
    sprite = decide_sprite_visibility(tag, url)
    return clean_text, sprite, tag
