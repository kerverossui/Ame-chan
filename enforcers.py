"""
enforcers.py — post-processing of Ame's replies.

Everything here runs after the model has written a reply and fixes what a
prompt alone can't guarantee:

  enforce_language / enforce_dialect_final  text in another language, or
                                            voseo in a tuteo dialect
  humanize_reply                            a line editor (the main model)
                                            returns small validated edits
  enforce_inner_voice / tidy_inner_voice    exactly one « » line per reply
  soften_cutoffs                            "I— you" → "I... you" in English
  drop_repeated_lines                       a continuation doesn't repeat
                                            the reply it extends
  is_repetition                             a reply that copies the last one

Helper calls never break a turn: if one fails, the text goes out as it
came, and the failure is logged.
"""

import json
import random
import re
import traceback
import difflib

import database as db
from llm import call_llm
from app_config import (
    LANGUAGE_INSTRUCTIONS,
    lang_family as _lang_family,
    lang_fallback_key,
    INNER_VOICE_MARKS_BY_LANG,
    INNER_VOICE_DEFAULT_MARK,
)

# Code errors are logged loudly so they can't pass for a network failure.
_PROGRAMMING_ERRORS = (NameError, AttributeError, TypeError, ImportError,
                       IndexError, KeyError, ValueError)


def _log_swallowed(where: str, exc: Exception, consequence: str) -> None:
    if isinstance(exc, _PROGRAMMING_ERRORS):
        print(f"\n{'!' * 62}")
        print(f"[BUG] {where}: {type(exc).__name__}: {exc}")
        print(f"  Consequence: {consequence}")
        print(traceback.format_exc())
        print(f"{'!' * 62}\n")
    else:
        print(f"[{where}] failed ({type(exc).__name__}: {exc}) — {consequence}")


# Cheap model per engine for the small helper calls.
HELPER_MODEL_BY_PROVIDER = {
    "grok": "grok-3-mini",
    "qwen": "qwen-turbo",
    "deepseek": "deepseek-v4-flash",
    "gpt": "gpt-5.6-luna",
}


def _helper_model() -> str:
    provider = db.get_settings().get("llm_provider", "grok")
    return HELPER_MODEL_BY_PROVIDER.get(provider, "grok-3-mini")


# ────────────────────────────────────────────────────────────────
# Inner voice
# ────────────────────────────────────────────────────────────────
# Every reply carries one « » line. If the model left it out, a cheap call
# writes one for this exact moment; a generic line is only the fallback
# when that call fails.

INNER_VOICE_MARK_DETECT_RE = re.compile(r"[«»『』]")

INNER_VOICE_FALLBACK_BANK = {
    "es": ["no dejes que se note.", "otra vez no.", "respira.",
           "esto está bien... todo está bien.", "cuidado, cuidado."],
    "ja": ["顔に出すな。", "落ち着け、落ち着け。", "大丈夫、大丈夫。"],
    "ko": ["티내지 마.", "괜찮아, 괜찮아.", "진정해."],
    "zh": ["别表现出来。", "没事,没事的。", "冷静点。"],
}
INNER_VOICE_FALLBACK_DEFAULT = ["don't let it show.", "not again.", "breathe.",
                                "this is fine. everything's fine.",
                                "careful, careful."]

_TRAILING_SPRITE_TAG_RE = re.compile(
    r"([\[\(\{]{1,2}\s*sprite\s*[:=]\s*[A-Za-z_]+\s*[\]\)\}]{1,2}\s*)$",
    re.IGNORECASE,
)


def _generate_inner_voice_line(reply_text: str, marks: tuple[str, str]) -> str | None:
    """A line written for this reply's moment, or None if the call fails."""
    open_mark, close_mark = marks
    try:
        raw = call_llm(
            [
                {"role": "system", "content": (
                    "Below is a reply from a roleplay. Ame is a former "
                    "streamer: dry and guarded on the outside, raw and "
                    "needy underneath. Write the ONE thing she murmurs to "
                    "herself at the end of this moment — what she really "
                    "feels and would never say to his face. A few words to "
                    "one short sentence, lowercase unless a name, in the "
                    "same language as the reply, about THIS exact moment "
                    "(not a mood summary, not advice to herself in general). "
                    f"Wrap it in {open_mark} {close_mark} and output only "
                    "that, nothing else.\n\nExamples of the register, "
                    "never to reuse: «he remembered. he actually "
                    "remembered.» / «don't get used to this, idiot» / "
                    "«why does that make me want to cry»"
                )},
                {"role": "user", "content": reply_text[-1200:]},
            ],
            # Room for models that spend a few tokens before the answer.
            max_tokens=220,
            temperature=1.0,
            conv_id="ame-app-local-innervoice",
            model=_helper_model(),
            reasoning=None,
        )
    except Exception as e:
        _log_swallowed("inner-voice", e, "a generic line is used instead")
        return None
    line = (raw or "").strip()
    if not line:
        return None
    if open_mark not in line:
        line = f"{open_mark}{line.strip(open_mark + close_mark)}{close_mark}"
    return line


def enforce_inner_voice(text: str, language: str = "en") -> str:
    """Adds an inner-voice line if the reply has none (before the sprite tag)."""
    if INNER_VOICE_MARK_DETECT_RE.search(text):
        return text
    key = lang_fallback_key(language)
    marks = INNER_VOICE_MARKS_BY_LANG.get(key, INNER_VOICE_DEFAULT_MARK)
    line = _generate_inner_voice_line(text, marks)
    if not line:
        phrase = random.choice(INNER_VOICE_FALLBACK_BANK.get(key, INNER_VOICE_FALLBACK_DEFAULT))
        line = f"{marks[0]}{phrase}{marks[1]}"
    m = _TRAILING_SPRITE_TAG_RE.search(text)
    if m:
        return f"{text[:m.start()].rstrip()}\n\n{line}\n\n{m.group(1)}"
    return f"{text.rstrip()}\n\n{line}"


_INNER_RE = re.compile(r"«[^»]*»|『[^』]*』")
_SPRITE_RE = re.compile(r"\[\[[^\]]*\]\]")
_IV_PARA_RE = re.compile(r"^\s*(«[^»\n]*»|『[^』\n]*』)\s*$")


def _split_inline_inner_voice(text: str) -> str:
    """A « » line buried in the middle of a long paragraph becomes its own
    paragraph."""
    out = []
    for p in text.split("\n\n"):
        m = _INNER_RE.search(p)
        if m and len(p.split()) > 40 and p[:m.start()].strip() and p[m.end():].strip():
            out += [p[:m.start()].rstrip(), m.group(0), p[m.end():].lstrip()]
        else:
            out.append(p)
    return "\n\n".join(out)


def tidy_inner_voice(text: str) -> str:
    """Exactly one inner-voice line, and not as the reply's first line.
    Only stand-alone « » paragraphs are moved or dropped; one inside a
    short paragraph is left alone and counts as the one."""
    text = _split_inline_inner_voice(text)
    paras = text.split("\n\n")
    iv_idx = [i for i, p in enumerate(paras) if _IV_PARA_RE.match(p)]
    if not iv_idx:
        return text
    inline = any(_INNER_RE.search(p) for i, p in enumerate(paras) if i not in iv_idx)
    keep = None
    if not inline:
        later = [i for i in iv_idx if i != 0]
        keep = later[0] if later else iv_idx[0]
    drop = [i for i in iv_idx if i != keep]
    if drop:
        print(f"[inner-voice] {len(drop)} extra « » line(s) removed")
    out = [p for i, p in enumerate(paras) if i not in drop]
    if keep == 0:
        body = [p for p in out if p.strip() and not _SPRITE_RE.fullmatch(p.strip())]
        if len(body) >= 3:
            out.insert(2, out.pop(0))
            print("[inner-voice] moved the « » line out of the opening")
    return "\n\n".join(out)


# ────────────────────────────────────────────────────────────────
# Language and dialect
# ────────────────────────────────────────────────────────────────
# A clear marker of another language (or voseo where the user picked a tuteo
# dialect) gets the whole reply rewritten by a cheap call. Covers English,
# Spanish and Italian; ja/ko/zh would need word lists checked by native
# speakers.

LANGUAGE_LEAK_MARKERS = {
    "es": re.compile(
        r"\b(dios( mío| mía)?|te amo|con todo lo que soy|mi amor|"
        r"corazón|cariño|abrázame|bésame|preciosa|preciosos|"
        r"así es|no puedo creerlo|todo lo que tengo)\b|[¿¡]",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"\b(i love you|oh my god|sweetheart|honey|darling|"
        r"my everything|i can'?t believe it)\b",
        re.IGNORECASE,
    ),
    # No "cara"/"caro": too common in Spanish.
    "it": re.compile(
        r"\b(amore mio|tesoro mio|ti amo|non ci credo|cavolo|"
        r"madonna mia|dio mio)\b",
        re.IGNORECASE,
    ),
}

VOSEO_LEAK_RE = re.compile(
    r"\b(vos|sos\b|tenés|querés|decís|podés|sabés|"
    r"andá\b|vení\b|escuchás|"
    r"boludo|boluda|che\b|quilombo|bo[ .]?ludeces)\b",
    re.IGNORECASE,
)
_TUTEO_DIALECTS = {"es-mx", "es-pe", "es-es"}


def _detect_dialect_leak(text: str, language: str) -> bool:
    return language in _TUTEO_DIALECTS and bool(VOSEO_LEAK_RE.search(text))


def _detect_language_leak(text: str, language: str) -> bool:
    family = _lang_family(language)
    if family is None:
        return False
    return any(pattern.search(text) for other, pattern in LANGUAGE_LEAK_MARKERS.items()
               if other != family)


def _rewrite_in_language(text: str, language: str, leak_type: str = "language") -> str | None:
    """The whole reply rewritten in the right language (or dialect), or None."""
    problem = (
        "accidentally used the wrong Spanish dialect (voseo — vos/sos/tenés/"
        "decís — and/or Argentine slang like 'boludo'/'che') where the target "
        "dialect uses tuteo instead"
        if leak_type == "dialect" else
        "accidentally mixed in words or a phrase from a different language"
    )
    target = LANGUAGE_INSTRUCTIONS.get(language, LANGUAGE_INSTRUCTIONS["en"])
    try:
        raw = call_llm(
            [
                {"role": "system", "content": (
                    f"The text below is a reply from the character Ame that {problem}. "
                    "Rewrite it ENTIRELY in the correct language, changing as little "
                    "else as possible — same content, same tone, same formatting "
                    "(narration, «»/『』 inner voice, real dialogue marks, any "
                    "[[sprite: x]] tag at the end all stay exactly where they are). "
                    f"Target language: {target} Output ONLY the corrected text, "
                    "nothing else."
                )},
                {"role": "user", "content": text},
            ],
            max_tokens=len(text) // 2 + 400,
            temperature=0.3,
            conv_id="ame-app-local-language-fix",
            model=_helper_model(),
            reasoning=None,
        )
        return (raw or "").strip() or None
    except Exception as e:
        _log_swallowed("language-fix", e, "the reply goes out uncorrected")
        return None


def _apply_rewrite(text: str, language: str, leak_type: str) -> str:
    fixed = _rewrite_in_language(text, language, leak_type=leak_type)
    if not fixed:
        print(f"[enforce] language-fix ({leak_type}) returned EMPTY — "
              "the reply goes out uncorrected")
        return text
    still = (_detect_dialect_leak(fixed, language) if leak_type == "dialect"
             else _detect_language_leak(fixed, language))
    print(f"[enforce] language-fix ({leak_type}) applied"
          + (" — the leak is still there after rewriting" if still else ""))
    return fixed


def enforce_language(text: str, language: str = "en") -> str:
    """Rewrites the reply if it leaks another language or the wrong dialect.
    Detection is a regex, so the model is only called when needed."""
    if _detect_dialect_leak(text, language):
        return _apply_rewrite(text, language, "dialect")
    if _detect_language_leak(text, language):
        return _apply_rewrite(text, language, "language")
    return text


def enforce_dialect_final(text: str, language: str = "en") -> str:
    """The same check at the end of the chain: the inner voice was written
    by another call after the first check."""
    return enforce_language(text, language)


# ────────────────────────────────────────────────────────────────
# Repeated reply
# ────────────────────────────────────────────────────────────────
# Long pasted user messages occasionally make the model echo its previous
# reply. Two replies sharing 85% of their first 1,500 characters count as a
# repeat; the caller then retries once with REPETITION_INSTRUCTION.

REPETITION_THRESHOLD = 0.85
REPETITION_WINDOW = 1500

REPETITION_INSTRUCTION = (
    "[Your previous draft repeated your last reply almost word for word. "
    "Do NOT reuse your previous message: answer what he just said, with "
    "new narration and new dialogue, same format and language.]"
)


def is_repetition(new_value: str, previous: str | None) -> bool:
    if not new_value or not previous:
        return False
    a, b = new_value[:REPETITION_WINDOW], previous[:REPETITION_WINDOW]
    if len(a) < 300 or len(b) < 300:
        return False
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sm.quick_ratio() >= REPETITION_THRESHOLD and sm.ratio() >= REPETITION_THRESHOLD


# ────────────────────────────────────────────────────────────────
# Line editor
# ────────────────────────────────────────────────────────────────
# The main model re-reads the reply and returns a few exact-match edits:
# lines a native speaker wouldn't say that way, narration that reads
# machine-written, the narrator explaining the moment, a job title used as a
# name. Every edit is validated before it's applied (exact single match,
# never the sprite tag, the inner voice only from inside its marks, similar
# length), so a bad answer changes nothing.

POLISH_MAX_EDITS = 6

_POLISH_SYSTEM = """You are the line editor for a roleplay app. The character is Ame: a woman in her early twenties, online a lot, blunt, sometimes crude, easily flustered, and — when she's with someone she's falling for — more sincere than she'd ever admit. She lives with {name}. Below is one of her finished replies. Make it read like a person wrote it by fixing only what is clearly wrong. Most replies need one to three edits; some need none.

Fix, in this order of importance:
1. SPOKEN lines (inside quotation marks, or after a dash in Spanish or Italian) that a native speaker wouldn't say exactly that way. This matters most. Look for:
   - lines that sound translated: clipped constructions that skip words a person would say, bent idioms, a word used slightly wrong, odd tense or word order;
   - anime-subtitle noises and words at the start of a line ("Oi", "Nn", "Hmph", "Tch") — drop them or use what an English speaker would say;
   - lines that don't follow from what was just said, or contradict the line before;
   - stiff or written-sounding phrasing, greeting-card lines, and polished comebacks — especially a joke used to swat away something tender.
   Rewrite each as the natural, complete sentence she'd really say, in fluent everyday {lang}, with contractions and the little words people use. Don't shorten lines to fix them; natural speech is often a full sentence.
2. {talk_rule}
3. NARRATION that reads machine-written. These are the most common, in order:
   a. images that mean nothing when you try to picture them ("quiet and unforgiven", "a knot in the static", "some kind of broken little prophet") — say the plain thing;
   b. narrating habits: describing how her voice "comes out" (small, thin, wet, at the edge of a laugh…), things she does "before she can…" or "without deciding to", "her whole body…", "for a beat", a feeling "behind her ribs" — say what she does instead;
   c. the narrator explaining the moment ("It isn't a no." "It isn't a performance." "It's almost a warning, almost a question.") or a closing sentence about what the moment means — delete it with "new": "";
   d. a throwaway punchline tacked onto the end of her last line after she already joked earlier in the reply ("Deal with it." "Your loss." "Case closed.") — delete it, so the reply ends on something real;
   e. wrong word choices, piled-up adjectives, clever tech or game metaphors for ordinary moments, a second simile, a run of choppy fragments that should be one sentence.
4. {job_rule}
5. If three or more of her spoken sentences start with "You", rewrite one or two so they start from her own side.

Rules for every edit:
- Copy "old" EXACTLY from the reply — one sentence or a piece of one, never across a line break. To delete a whole spoken line, copy it with its quotation marks.
- Keep the language ({lang}), the meaning, the mood, the swearing and roughly the length (except deletions).
- Never touch the [[sprite]] tag. Leave the « » / 『 』 line alone unless it's garbled (broken grammar, a repeated or wrong word); then fix only the words inside the marks, keeping it short, raw and lowercase.
- At most two deletions in total. Never add lines for {name}.
- Don't "improve" lines that already sound like a real person. Plain is good.

Examples of good edits (from other replies — don't reuse their words):
{{"old": "Cute later. Now sleep.", "new": "You can be cute tomorrow. Right now you're going to sleep."}}
{{"old": "Oi—quit it!", "new": "Hey! Quit it!"}}
{{"old": "You touch it and I end you.", "new": "If you touch it, I swear I'll kill you."}}
{{"old": "Your eloquence is truly breathtaking, Doc.", "new": "Wow. Okay. Very romantic."}}
{{"old": "Is this a date or a hostage situation? Asking for my dignity.", "new": "Wait... is this a date?"}}
{{"old": "The window glows like a loading screen.", "new": "Light is coming in around the curtain."}}
{{"old": "Warm. Solid. Here.", "new": "He's warm, and solid, and still here."}}
{{"old": "Her voice comes out thin and wobbly, caught somewhere between a laugh and a plea.", "new": "She's half laughing."}}
{{"old": "It isn't a refusal.", "new": ""}}  (narrator explaining the moment, deleted)
{{"old": "\\"Also your hair is a crime scene.\\"", "new": ""}}  (a sixth spoken line, deleted)

Answer ONLY with JSON: {{"edits": [{{"old": "...", "new": "..."}}]}} — or {{"edits": []}}."""

_JOB_RULE_DEFAULT = ("If she calls {name} by a job title (\"doctor\", \"doc\", "
                     "\"officer\"…) or brings up {name}'s work (profile: \"{job}\") when the "
                     "moment isn't about it, use the name, a casual nickname "
                     "or nothing instead, or drop the job reference.")
_JOB_RULE_TOPIC = ("{name} brought up the job this time, so mentions "
                   "of it are fine — just don't let her use a job title as a name.")
_JOB_RULE_NONE = "(nothing to check here)"

_TALK_RULE = ('TOO MUCH TALKING: if she has more than five spoken sentences, '
              'delete the one that adds least (usually a throwaway quip) by '
              'giving "new": "" — at most one deletion, and never her only line.')
_TALK_RULE_LIVE = ("(She's live on stream as KAngel, where she talks a lot on "
                   "purpose — don't cut anything for length.)")


def _polish_prompt(language: str, user_name: str, job_terms: str,
                   job_in_message: bool, live: bool = False) -> str:
    name = user_name or "the user"
    if not job_terms:
        job_rule = _JOB_RULE_NONE
    elif job_in_message:
        job_rule = _JOB_RULE_TOPIC.format(name=name)
    else:
        job_rule = _JOB_RULE_DEFAULT.format(name=name, job=job_terms)
    lang_name = {"en": "English", "it": "Italian"}.get(_lang_family(language) or "en", "Spanish")
    return _POLISH_SYSTEM.format(name=name, job_rule=job_rule, lang=lang_name,
                                 talk_rule=_TALK_RULE_LIVE if live else _TALK_RULE)


def _speech_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges of spoken dialogue: "…" / “…” / 「…」, and the text
    after a dash on a dialogue line (Spanish/Italian convention)."""
    spans = [m.span(1) for m in re.finditer(r'["“]([^"“”\n]+)["”]', text)]
    spans += [m.span(1) for m in re.finditer(r'「([^」\n]+)」', text)]
    spans += [m.span(1) for m in re.finditer(r'(?m)^[ \t]*—([^\n]+)', text)]
    return spans


def _protected_spans(text: str) -> list[tuple[int, int]]:
    return ([m.span() for m in _INNER_RE.finditer(text)]
            + [m.span() for m in _SPRITE_RE.finditer(text)])


def _spoken_sentence_count(text: str) -> int:
    return sum(len([x for x in re.split(r"(?<=[.!?…])\s+", text[a:b]) if x.strip()])
               for a, b in _speech_spans(text))


def _validate_deletion(text: str, old: str) -> bool:
    """A deletion ("new": "") may remove one spoken sentence or quoted line
    when she talks a lot and something she says survives, or one short,
    whole narration sentence."""
    if not old or text.count(old) != 1 or "\n" in old or len(old) > 200:
        return False
    start = text.index(old)
    end = start + len(old)
    if any(start < b and a < end for a, b in _protected_spans(text)):
        return False
    stripped = old.strip()
    in_speech = any(a - 1 <= start and end <= b + 1 for a, b in _speech_spans(text))
    if not in_speech:
        before = text[:start].rstrip(" \t")
        starts_ok = not before or before[-1] in ".!?…\n" or (
            before[-1] in '"”' and len(before) > 1 and before[-2] in ".!?…")
        if not (starts_ok and stripped[-1:] in ".!?…" and len(stripped.split()) <= 25):
            return False
        para_start = text.rfind("\n", 0, start) + 1
        para_end = text.find("\n", end)
        para_end = len(text) if para_end == -1 else para_end
        return bool((text[para_start:start] + text[end:para_end]).strip())
    if _spoken_sentence_count(text) <= 5:
        return False
    if stripped[:1] in '"“' and stripped[-1:] in '"”':
        spans = _speech_spans(text)
        return len(spans) > 1 and any(a - 1 == start and b + 1 == end for a, b in spans)
    return stripped[-1:] in ".!?…" and _validate_deletion_inside_line(text, old)


def _validate_deletion_inside_line(text: str, old: str) -> bool:
    """`old` sits inside one spoken line that keeps other words."""
    if text.count(old) != 1:
        return False
    start = text.index(old)
    end = start + len(old)
    for a, b in _speech_spans(text):
        if a <= start and end <= b:
            return bool(text[a:start].strip() or text[end:b].strip())
    return False


def _apply_deletion(text: str, old: str) -> str:
    out = text.replace(old, "", 1)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r' +([”"])(?=\s|$)', r"\1", out)      # 'Okay. "' → 'Okay."'
    out = re.sub(r'(^|\s)(["“]) +', r"\1\2", out)        # '" Okay' → '"Okay'
    return re.sub(r"(?m)^[ \t]+|[ \t]+$", "", out)


def _validate_edit(text: str, old: str, new: str) -> bool:
    if not old or not new or old == new or text.count(old) != 1:
        return False
    if "\n" in old or "\n" in new or len(old) > 300:
        return False
    if any(ch in new for ch in "«»『』[]<>*"):
        return False
    ratio = len(new) / max(len(old), 1)
    if not (0.3 <= ratio <= 1.8) and not (len(old) < 30 and len(new) <= len(old) + 30):
        return False
    start = text.index(old)
    end = start + len(old)
    if any(start < b and a < end for a, b in (m.span() for m in _SPRITE_RE.finditer(text))):
        return False
    for m in _INNER_RE.finditer(text):          # inner voice: only from inside
        a, b = m.span()
        if start < b and a < end and not (a < start and end < b):
            return False
    return True


def humanize_reply(text: str, language: str = "en", *, user_name: str = "",
                   job_terms: str = "", job_in_message: bool = False,
                   live: bool = False) -> str:
    """The reply with the editor's validated edits applied (unchanged if
    the call fails, finds nothing, or proposes invalid edits)."""
    if not text.strip():
        return text
    try:
        raw = call_llm(
            [{"role": "system", "content": _polish_prompt(
                language, user_name, job_terms, job_in_message, live)},
             {"role": "user", "content": text}],
            max_tokens=900, temperature=0.3,
            conv_id="ame-app-local-speech-polish",
            model=None,          # the main model: it judges natural speech best
            reasoning=None,
        )
        m = re.search(r"\{.*\}", raw or "", re.S)
        edits = json.loads(m.group(0)).get("edits", []) if m else []
    except Exception as e:
        _log_swallowed("humanize", e, "the reply goes out unedited")
        return text
    applied = deleted = 0
    for e in edits[:POLISH_MAX_EDITS] if isinstance(edits, list) else []:
        if not isinstance(e, dict):
            continue
        old, new = str(e.get("old", "")).strip(), str(e.get("new", "")).strip()
        if old and old not in text:
            # Straight vs curly quotes/apostrophes: try the reply's own.
            for a, b in (("'", "’"), ("’", "'"), ('"', "“"), ('"', "”")):
                if old.replace(a, b) in text:
                    old = old.replace(a, b)
                    break
        if not new and deleted < 2 and not live and _validate_deletion(text, old):
            text = _apply_deletion(text, old)
            deleted += 1
            applied += 1
            print(f"[humanize] cut {old!r}")
        elif _validate_edit(text, old, new):
            text = text.replace(old, new, 1)
            applied += 1
            print(f"[humanize] {old!r} → {new!r}")
        else:
            print(f"[humanize] rejected edit (not an exact, safe match): {old[:60]!r}")
    if not applied:
        print("[humanize] no changes")
    return text


# ────────────────────────────────────────────────────────────────
# Continue and dialogue punctuation
# ────────────────────────────────────────────────────────────────

def _norm_line(t: str) -> str:
    return re.sub(r"[^\w' ]+", "", t.lower().replace("’", "'")).strip()


def drop_repeated_lines(new: str, previous: str) -> str:
    """A continuation must not say again what she already said in the reply
    it extends: a paragraph whose spoken sentences are mostly repeats is
    dropped, and a repeated sentence inside a longer line is cut."""
    def sentences(text):
        out = []
        for a, b in _speech_spans(text):
            out += [x for x in re.split(r"(?<=[.!?…])\s+", text[a:b]) if x.strip()]
        return out
    seen = {_norm_line(x) for x in sentences(previous) if len(_norm_line(x).split()) >= 2}
    if not seen:
        return new
    kept = []
    for para in new.split("\n\n"):
        sents = sentences(para)
        reps = [x for x in sents if _norm_line(x) in seen]
        if sents and len(reps) * 2 > len(sents):
            print(f"[continue] dropped a paragraph that repeated: {reps[0][:50]!r}")
            continue
        for x in reps:
            x = x.strip()
            if x[-1:] in ".!?…" and _validate_deletion_inside_line(para, x):
                para = _apply_deletion(para, x)
                print(f"[continue] dropped a repeated line: {x[:50]!r}")
        kept.append(para)
    return "\n\n".join(kept)


_CUTOFF_RE = re.compile(r"(\w)—(?=\s|[\"”]|$)")


def soften_cutoffs(text: str, language: str = "en") -> str:
    """English dialogue: a word cut off with an em dash ("I— you can't")
    becomes a trail-off ("I... you can't"). Narration keeps its dashes;
    Spanish and Italian use the dash to mark dialogue and are left alone."""
    if (_lang_family(language) or "en") != "en":
        return text
    out, last = [], 0
    for a, b in _speech_spans(text):
        out.append(text[last:a])
        out.append(_CUTOFF_RE.sub(r"\1...", text[a:b]))
        last = b
    out.append(text[last:])
    return "".join(out)
