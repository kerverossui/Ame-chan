"""
Loads config.md and character.md (no other module reads them) and holds
the shared constants.
"""

import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ame.db")

# ────────────────────────────────────────────────────────────────
# config.md
# ────────────────────────────────────────────────────────────────

# Maps each config.md section title to its internal key
_SECTION_TO_KEY = {
    "opening scene": "OPENING_SCENARIO",
    "ongoing situation (always included)": "ONGOING_CONTEXT",
}

# Sections holding several `KEY = value` lines in one block (profile and
# AI engine). Spanish titles are accepted too.
_MULTI_KV_SECTION_PREFIXES = ("your profile", "ai engine", "reasoning effort",
                              "tu perfil", "motor de ia")


# Placeholder values in config.md ("your-key-here") count as empty.
_PLACEHOLDER_PREFIXES = ("your-", "tu-")


def load_config() -> dict:
    """Reads config.md. Associates each ``` ``` block with the ## heading
    before it. Supports single-line values (KEY = value) and multi-line
    blocks (opening scene, ongoing situation, or several KEY = value
    lines as in 'Your profile' / 'AI engine')."""
    config_path = os.path.join(BASE_DIR, "config.md")
    values = {}
    if not os.path.exists(config_path):
        return values

    with open(config_path, encoding="utf-8") as f:
        content = f.read()

    sections = re.split(r"(?m)^##\s+(.+)$", content)
    for i in range(1, len(sections), 2):
        title = sections[i].strip().lower()
        body = sections[i + 1] if i + 1 < len(sections) else ""
        block_match = re.search(r"```\s*\n(.*?)```", body, re.DOTALL)
        if not block_match:
            continue
        block = block_match.group(1).strip("\n")

        key = _SECTION_TO_KEY.get(title)
        lang_m = re.fullmatch(r"opening scene\s*\(([a-z]{2}(?:-[a-z]{2})?)\)", title)
        if lang_m:
            # One opening per language: "## Opening scene (es)", "(ja)"…
            # CJK text has no hard-wrapped lines to join.
            values["OPENING_SCENARIO_" + lang_m.group(1)] = re.sub(
                r"(?<!\n)\n(?!\n)", " ", block)
            continue
        if key == "OPENING_SCENARIO":
            # Shown as a chat message and rendered line by line: hard-wrapped
            # lines inside a paragraph are joined (blank lines still split
            # paragraphs), so a quote that wraps doesn't break the styling.
            values[key] = re.sub(r"(?<!\n)\n(?!\n)", " ", block)
        elif key == "ONGOING_CONTEXT":
            values[key] = block  # full text as-is, multi-line
        elif key:
            m = re.match(r"^\w+\s*=\s*(.+)$", block)
            val = m.group(1).strip() if m else block.strip()
            if val and not val.startswith(_PLACEHOLDER_PREFIXES):
                values[key] = val

        # Sections with several KEY = value lines (profile, AI engine)
        if title.startswith(_MULTI_KV_SECTION_PREFIXES):
            for line in block.split("\n"):
                m2 = re.match(r"^\s*(\w+)\s*=\s*(.+?)\s*$", line)
                if m2 and m2.group(2).strip() and not m2.group(2).strip().startswith(_PLACEHOLDER_PREFIXES):
                    values[m2.group(1)] = m2.group(2).strip()

    return values


_config = load_config()

# config.md values are only used to SEED the database the first time
# (see lifespan in main.py) — once the user completes onboarding or
# edits Settings, the database wins and config.md is no longer read
# until the next clean start.
DEFAULT_LLM_PROVIDER = _config.get("LLM_PROVIDER") or "grok"
GROK_API_KEY = _config.get("GROK_API_KEY") or os.environ.get("GROK_API_KEY", "")
GROK_MODEL = _config.get("GROK_MODEL") or os.environ.get("GROK_MODEL", "grok-4.3-latest")
QWEN_API_KEY = _config.get("QWEN_API_KEY") or os.environ.get("QWEN_API_KEY", "")
QWEN_MODEL = _config.get("QWEN_MODEL") or os.environ.get("QWEN_MODEL", "qwen-plus")
DEEPSEEK_API_KEY = _config.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = _config.get("DEEPSEEK_MODEL") or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
GPT_API_KEY = _config.get("GPT_API_KEY") or os.environ.get("GPT_API_KEY", "")
GPT_MODEL = _config.get("GPT_MODEL") or os.environ.get("GPT_MODEL", "gpt-5.6-sol")
REASONING_EFFORT = _config.get("REASONING_EFFORT") or os.environ.get("REASONING_EFFORT", "none")
OPENING_SCENARIO = _config.get("OPENING_SCENARIO", "")
# The same scene per language (config.md "## Opening scene (xx)").
OPENING_SCENARIOS = {"en": OPENING_SCENARIO}
OPENING_SCENARIOS.update({k[len("OPENING_SCENARIO_"):]: v for k, v in _config.items()
                          if k.startswith("OPENING_SCENARIO_")})


def opening_for(language: str | None) -> str:
    """The opening scene in the configured language: the exact code
    (es-ar), then its family (es), then English."""
    language = (language or "en").lower()
    for code in (language, language.split("-")[0], "en"):
        if OPENING_SCENARIOS.get(code):
            return OPENING_SCENARIOS[code]
    return ""
ONGOING_CONTEXT = _config.get("ONGOING_CONTEXT", "")
CONFIG_PROFILE_DEFAULTS = {
    "user_name": _config.get("USER_NAME", ""),
    "user_bio": _config.get("USER_BIO", ""),
}

# Values used to seed the settings table the first time the app starts
# with a new database (see lifespan in main.py). After that, everything
# is edited from the welcome wizard or Settings — this file is not read
# again until the next clean start.
DEFAULT_LLM_SETTINGS = {
    "llm_provider": DEFAULT_LLM_PROVIDER,
    "grok_api_key": GROK_API_KEY,
    "grok_model": GROK_MODEL,
    "qwen_api_key": QWEN_API_KEY,
    "qwen_model": QWEN_MODEL,
    "deepseek_api_key": DEEPSEEK_API_KEY,
    "deepseek_model": DEEPSEEK_MODEL,
    "gpt_api_key": GPT_API_KEY,
    "gpt_model": GPT_MODEL,
    "reasoning_effort": REASONING_EFFORT,
}

# ────────────────────────────────────────────────────────────────
# Supported AI engines
# ────────────────────────────────────────────────────────────────
# All four expose an OpenAI Chat Completions-compatible API, so a single
# adapter (see llm.py) serves all four — only the endpoint, key and
# model change.
PROVIDER_ENDPOINTS = {
    "grok": "https://api.x.ai/v1/chat/completions",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/chat/completions",
    "gpt": "https://api.openai.com/v1/chat/completions",
}

PROVIDER_LABELS = {
    "grok": "Grok (xAI)",
    "qwen": "Qwen (Alibaba)",
    "deepseek": "DeepSeek",
    "gpt": "GPT (OpenAI)",
}

# Image support:
#   - Grok: only grok-4.3-latest.
#   - Qwen: only the "-vl-" models (qwen-vl-max, qwen3-vl-plus…).
#   - DeepSeek: the public API is text-only.
#   - GPT: every GPT-5.6 model.
def model_supports_images(provider: str, model: str) -> bool:
    model = (model or "").lower()
    if provider == "grok":
        return model == "grok-4.3-latest"
    if provider == "qwen":
        return "-vl" in model or model.endswith("-vl")
    if provider == "gpt":
        return True
    return False  # deepseek: no image support in the public API


def provider_endpoint(provider: str, custom_base_url: str | None = None) -> str:
    """A custom base URL for this engine (a gateway such as OpenRouter) wins
    over the official endpoint. The /chat/completions suffix is normalized,
    since gateways document the base with or without /v1 and the suffix."""
    if custom_base_url:
        url = custom_base_url.strip().rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        return url
    return PROVIDER_ENDPOINTS.get(provider, PROVIDER_ENDPOINTS["grok"])

# ────────────────────────────────────────────────────────────────
# Memory / prompt parameters
# ────────────────────────────────────────────────────────────────
RECENT_BUFFER = 12   # last N literal messages in the prompt
RETRIEVE_K = 2       # semantic memories per turn (new, uncached text each turn)

# The recent buffer is also capped by size, so a giant pasted message isn't
# re-sent in full every turn. The database keeps the full text.
RECENT_MSG_CHAR_CAP = 4000       # cap per message (~1k tokens)
RECENT_BUFFER_CHAR_BUDGET = 28000  # total buffer cap (~7k tokens)

SUMMARY_LEVEL_TO_INTERVAL = {"off": None, "low": 50, "medium": 30, "high": 15}

# ────────────────────────────────────────────────────────────────
# character.md
# ────────────────────────────────────────────────────────────────

def load_character() -> str:
    """Ame's full character sheet lives in character.md — kept apart from
    the code so it can be edited without risking broken Python strings,
    and versioned separately."""
    with open(os.path.join(BASE_DIR, "character.md"), encoding="utf-8") as f:
        return f.read()


SYSTEM_PROMPT = load_character()

# ────────────────────────────────────────────────────────────────
# Languages
# ────────────────────────────────────────────────────────────────

def lang_family(language: str) -> str | None:
    """Collapses a language/dialect code to its family, for content banks
    shared between dialects (es-pe/es-mx/es-ar/es-es -> 'es'). None for
    ja/ko/zh — atomic codes without dialects, outside the documented
    collapse (see LANGUAGE_LEAK_MARKERS in enforcers.py)."""
    if language == "en":
        return "en"
    if language == "it":
        return "it"
    if language.startswith("es"):
        return "es"
    return None


def lang_fallback_key(language: str) -> str:
    """Like lang_family(), but returns ja/ko/zh as they are. Use it for any
    lookup in a language-keyed dictionary instead of the raw code."""
    return lang_family(language) or language


# Inner-voice marks per language (« » or 『 』).
INNER_VOICE_MARKS_BY_LANG = {
    "ja": ("『", "』"), "ko": ("『", "』"), "zh": ("『", "』"),
}
INNER_VOICE_DEFAULT_MARK = ("«", "»")


# How spoken dialogue is marked in each language (see "Output format" in
# character.md). English uses quotation marks; Spanish uses dashes. The
# frontend renders both.
DIALOGUE_FORMAT_BY_LANG = {
    "en": ("narration in plain paragraphs; spoken lines in double quotation "
           "marks, punctuated the normal English way; one line of her "
           "murmured inner voice in « », mid-reply — what she really feels and won't "
           "say; no asterisks."),
    "es": ("narración en párrafos sin marcas; lo que dice en voz alta va con "
           "raya (—) al inicio, y los incisos del narrador se cierran y abren "
           "con raya; una línea de su voz interior murmurada entre « », a mitad de la respuesta "
           "— lo que de verdad siente y no dice; sin asteriscos."),
    "it": ("narrazione in paragrafi semplici; il dialogo parlato si apre con "
           "il trattino lungo (—), con gli incisi del narratore chiusi e "
           "riaperti dal trattino; una riga di voce interiore sussurrata "
           "tra « », a metà risposta — ciò che sente davvero e non dice; niente asterischi."),
    "ja": "地の文は記号なし。声に出す台詞は「」、心の中のつぶやきは『』。アスタリスクは使わない。",
    "ko": "서술은 기호 없이, 소리 내어 하는 대사는 「」, 혼잣말 같은 속마음은 『』. 별표는 쓰지 않는다.",
    "zh": "叙述不加符号；说出口的台词用「」，喃喃自语的内心声音用『』；不使用星号。",
}


LANGUAGE_INSTRUCTIONS = {
    "en": (
        "Write entirely in English — both narration and dialogue. This is "
        "the default; no special dialect notes apply."
    ),
    "es-mx": (
        "Write entirely in Spanish, specifically Mexican Spanish (español "
        "latino de México) — both narration and dialogue. Use tú (not "
        "vos), natural Mexican vocabulary and rhythm where it fits "
        "organically (e.g. 'órale', 'qué onda', 'no manches' used "
        "sparingly and only where genuinely natural to her voice, not "
        "forced). Keep her established personality — the dialect shapes "
        "the words, not who she is."
    ),
    "es-pe": (
        "Write entirely in Spanish, natural Latin American Spanish as "
        "spoken in Peru — both narration and dialogue. Use tú (not vos). "
        "Peruvian naturalness lives in warm, neutral phrasing and rhythm, "
        "not in slang: avoid stereotypical jerga such as 'pata', 'jato', "
        "'al toque', 'chamba', 'causa' or 'bacán', which sound forced in "
        "her voice. The language shapes the words, not who she is."
    ),
    "es-ar": (
        "Write entirely in Spanish, specifically Argentine Spanish "
        "(español rioplatense) — both narration and dialogue. Use voseo "
        "(vos sos, vos tenés, vos querés — not tú eres/tienes/quieres) "
        "consistently, since that is the defining grammatical feature of "
        "this dialect. Natural Argentine vocabulary and rhythm where it "
        "fits organically (e.g. 'che', 'boludo' used sparingly and only "
        "where it genuinely matches the moment, not forced). Keep her "
        "established personality — the dialect shapes the words, not who "
        "she is."
    ),
    "es-es": (
        "Write entirely in Spanish, specifically Peninsular Spanish "
        "(español de España) — both narration and dialogue. Use vosotros "
        "for informal plural address where relevant, and natural "
        "Peninsular vocabulary and rhythm (e.g. 'vale', 'tío/tía', "
        "'guay' used sparingly and only where genuinely natural, not "
        "forced). Keep her established personality — the dialect shapes "
        "the words, not who she is."
    ),
    "it": (
        "Write entirely in Italian — both narration and dialogue. Use "
        "tu (not the formal Lei), since she's speaking with someone "
        "close to her. Natural Italian rhythm; light vocabulary like "
        "'amore', 'dai', 'non ci credo' only where it genuinely fits. "
        "The language shapes the words, not who she is."
    ),
    "ja": (
        "Write entirely in Japanese — both narration and dialogue. Her "
        "speech patterns (the hesitant trailing pauses, the guarded "
        "warmth) should come through in natural Japanese phrasing rather "
        "than a literal translation of English sentence structure — "
        "trailing off with 「……」 is her ellipsis. Keep her "
        "established personality intact — the language shapes the words, "
        "not who she is."
    ),
    "ko": (
        "Write entirely in Korean — both narration and dialogue. Her "
        "speech patterns (the hesitant trailing pauses, the guarded "
        "warmth) should come through in natural Korean phrasing rather "
        "than a literal translation of English sentence structure. Keep "
        "her established personality intact — the language shapes the "
        "words, not who she is."
    ),
    "zh": (
        "Write entirely in Chinese (simplified) — both narration and "
        "dialogue. Her speech patterns (the hesitant trailing pauses, the "
        "guarded warmth) should come through in natural Chinese phrasing "
        "rather than a literal translation of English sentence structure. "
        "Keep her established personality intact — the language shapes "
        "the words, not who she is."
    ),
}

LANGUAGE_LABELS = {
    "en": "English",
    "es-mx": "Español Latino (México)",
    "es-pe": "Español Latino (Perú)",
    "es-ar": "Español Latino (Argentina)",
    "es-es": "Español (España)",
    "it": "Italiano",
    "ja": "日本語 (Japanese)",
    "ko": "한국어 (Korean)",
    "zh": "中文 (Chinese)",
}

# ────────────────────────────────────────────────────────────────
# User pronouns (from onboarding) and template substitution
# ────────────────────────────────────────────────────────────────
# ONGOING_CONTEXT / OPENING_SCENARIO in config.md are fixed, generic text
# (they must not contain any hardcoded name or personal data). These
# tokens are resolved at runtime against the profile the user entered in
# the welcome wizard.
PRONOUNS = {
    "male":   {"subj": "he",   "obj": "him",  "poss": "his",   "poss_pron": "his"},
    "female": {"subj": "she",  "obj": "her",  "poss": "her",   "poss_pron": "hers"},
    "other":  {"subj": "they", "obj": "them", "poss": "their", "poss_pron": "theirs"},
}


def get_pronouns(gender: str | None) -> dict:
    return PRONOUNS.get((gender or "other").lower(), PRONOUNS["other"])


def personalize(text: str, profile: dict) -> str:
    """Replaces {{user_name}}, {{pronoun_subj}}, {{pronoun_obj}} and
    {{pronoun_poss}} in a config.md text using the user's real profile.
    If no name has been entered yet, the capitalized pronoun is used as
    the replacement subject so the sentence stays grammatical (and the
    possessive "{{user_name}}'s" is resolved separately, to avoid ending
    up with something like "She's ticket")."""
    if not text:
        return text
    p = get_pronouns(profile.get("user_gender"))
    name = (profile.get("user_name") or "").strip()
    if name:
        text = text.replace("{{user_name}}'s", f"{name}'s")
        text = text.replace("{{user_name}}", name)
    else:
        text = text.replace("{{user_name}}'s", p["poss"])
        text = text.replace("{{user_name}}", p["subj"].capitalize())
    return (
        text.replace("{{pronoun_subj}}", p["subj"])
        .replace("{{pronoun_obj}}", p["obj"])
        .replace("{{pronoun_poss}}", p["poss"])
    )
