"""
All of a turn's dice, rolled in one place and in a fixed order. The results
become short notes in the prompt's <direction> block (prompting.py).

  Context (not a die): stage, mode, whether the last reply was intimate.
  Passed in by build_messages; this module never reads the database.

  1. Psychological beat: negative (5%) / neutral (5%) / canon echo (5%) /
     nothing (85%).
  2. Crude register: chance by stage (80/50/25/10%). Her swearing is a trait
     of her voice under strong emotion, not only retaliation; a hostile
     message only changes the flavor (a snap at him vs. an intensifier).
  3. Longer speech: 1 in 6.
  4. Texture: a sound (20%, 40% in an intimate scene), a comparison with
     her streamer past (30%), a whimper (25%, 50% in an intimate scene), an
     exclamation (28%).
  5. Shape: one of the STRUCTURE_TEMPLATES, every turn.

  Also by stage: anxiety when he leaves for work (70/45/25/15%) and a floor
  of raw self-deprecation (55/40/25/15%). Both soften with trust and never
  reach 0.

The dice are independent of each other. Which specific line a beat uses is
picked later in prompting.py; this module only decides whether it fires.
"""

import random
import re

# A message that is hostile toward her (only colors the crude note).
HOSTILITY_TRIGGER_RE = re.compile(
    r"\b(c[aá]llate( la boca)?|puta|perra|zorra|idiota|est[uú]pid[oa]|"
    r"in[uú]til|no sirves( para nada)?|te odio|asco de|eres (una |un )?"
    r"(mierda|basura|desastre)|vete a la mierda|jodete|c[oó]jete|"
    r"shut (the fuck )?up|fuck you|screw you|f[*u]ck off|"
    r"(you'?re|ur) (such a )?(bitch|whore|stupid|useless|pathetic|worthless)|"
    r"i hate you|nobody (likes|wants) you|no vales nada)\b",
    re.IGNORECASE,
)

# Chances by relationship stage (1-4). They soften as trust grows and never
# reach 0: the trait stays, it just shows less.
HOSTILITY_CRUDE_CHANCE_BY_STAGE = {1: 0.80, 2: 0.50, 3: 0.25, 4: 0.10}
ROUTINE_DEPARTURE_CHANCE_BY_STAGE = {1: 0.70, 2: 0.45, 3: 0.25, 4: 0.15}
# Raw, halting unworthiness that doesn't shake off quickly. Separate from the
# articulated insight, which character.md caps on its own.
SELF_DEPRECATION_FLOOR_BY_STAGE = {1: 0.55, 2: 0.40, 3: 0.25, 4: 0.15}


# Suggested shapes for the reply, so they don't all open the same way. None
# of them opens on the inner voice or asks for back-and-forth dialogue.
STRUCTURE_TEMPLATES = [
    # None of these opens on the inner voice or asks for back-and-forth
    # dialogue: measured 2026-10-20, "open inside her head" made 39% of
    # replies start with the « » line, and "interleave short spoken lines"
    # pushed her to 8 spoken lines a reply (Crushon: 3).
    "open on what she's doing when his message lands, then her reaction.",
    "open with something she says, then stay in the narration.",
    "open on something she notices about him, then what it does to her.",
    "open mid-feeling — one sentence of what's going on inside her — then "
    "what she does about it.",
    "keep the room to a passing clause and stay on the two of them.",
    "what she does, then what she feels about it, then what she finally "
    "says.",
]


def _roll_turn_dice(stage: int, user_input: str, sexual_scene_active: bool) -> dict:
    """Rolls this turn's dice and logs them. `sexual_scene_active`: the
    last reply was intimate, which doubles the sound and whimper chances."""
    dice: dict = {"stage": stage, "sexual_scene_active": sexual_scene_active}

    psych_roll = random.random()
    if psych_roll < 0.05:
        dice["psych_layer"] = "negative"
    elif psych_roll < 0.10:
        dice["psych_layer"] = "neutral"
    elif psych_roll < 0.15:
        dice["psych_layer"] = "canon"
    else:
        dice["psych_layer"] = None

    crude_chance = HOSTILITY_CRUDE_CHANCE_BY_STAGE[stage]
    dice["crude_chance"] = crude_chance
    dice["crude_rolled"] = random.random() < crude_chance
    dice["crude_hostile_flavor"] = bool(HOSTILITY_TRIGGER_RE.search(user_input))

    routine_departure_chance = ROUTINE_DEPARTURE_CHANCE_BY_STAGE[stage]
    dice["routine_departure_chance"] = routine_departure_chance
    dice["routine_departure_rolled"] = random.random() < routine_departure_chance

    self_dep_chance = SELF_DEPRECATION_FLOOR_BY_STAGE[stage]
    dice["self_deprecation_chance"] = self_dep_chance
    dice["self_deprecation_ok"] = random.random() < self_dep_chance

    dice["long_speech_ok"] = random.random() < (1 / 6)

    vocal_chance = 0.40 if sexual_scene_active else 0.20
    whimper_chance = 0.50 if sexual_scene_active else 0.25
    dice["vocal_chance"] = vocal_chance
    dice["whimper_chance"] = whimper_chance
    dice["moan_ok"] = random.random() < vocal_chance
    dice["performer_yardstick_ok"] = random.random() < 0.30
    dice["whimper_ok"] = random.random() < whimper_chance
    dice["exclaim_ok"] = random.random() < 0.28

    dice["structure_template"] = random.choice(STRUCTURE_TEMPLATES)

    print(
        "[dice] stage={stage} sexual_scene={sexual_scene_active} "
        "psych={psych_layer} "
        "crude={crude_rolled}(chance={crude_chance:.0%}, "
        "hostile_flavor={crude_hostile_flavor}) "
        "long_speech={long_speech_ok} "
        "vocal={moan_ok}(chance={vocal_chance:.0%}) "
        "performer_yardstick={performer_yardstick_ok} "
        "whimper={whimper_ok}(chance={whimper_chance:.0%}) "
        "exclaim={exclaim_ok} "
        "routine_departure={routine_departure_rolled}"
        "(chance={routine_departure_chance:.0%}) "
        "self_deprecation={self_deprecation_ok}"
        "(chance={self_deprecation_chance:.0%})".format(**dice)
    )
    return dice

