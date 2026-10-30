"""
Ame Companion App: the FastAPI app (HTTP endpoints and the turn pipeline).

Run with  uvicorn main:app  (ame_launcher.py does it for you).

    app_config.py     config.md / character.md, constants
    database.py       SQLite storage
    llm.py            calls to the engines (Grok, DeepSeek, Qwen, GPT)
    memory.py         embeddings, semantic search, summaries
    canon.py          canon entries (canon.md), injected by name
    living_memory.py  per-person living memory (memories/)
    sprites.py        sprite library and tag handling
    prompting.py      relationship stage and per-turn prompt
    enforcers.py      post-processing of each reply
"""

import console_log
console_log.install()   # first, so every later print reaches the in-app console

import json
import re
import os
import queue
import urllib.parse
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import database as db
import living_memory
import memory
import prompting
import remote_access
import enforcers
import sprites
from app_config import (
    BASE_DIR,
    CONFIG_PROFILE_DEFAULTS,
    DEFAULT_LLM_SETTINGS,
    OPENING_SCENARIOS,
    opening_for,
    PROVIDER_LABELS,
    SUMMARY_LEVEL_TO_INTERVAL,
)
from llm import LAST_CACHE_STATS, call_llm, stream_llm

# Output ceiling. A long reply is about 1000 tokens; the margin keeps a
# length cut from eating the sprite tag at the end. Only what is generated
# is billed.
MAX_REPLY_TOKENS = 4000

# Sampling temperature for the main reply, per engine (1.0 if not listed).
# Higher values make deepseek-v4-pro drift into images that mean nothing
# and facts it invents about the user. With thinking on it is ignored.
CHAT_TEMPERATURE: dict[str, float] = {"deepseek": 0.85}


def _chat_temperature() -> float:
    return CHAT_TEMPERATURE.get(db.get_settings().get("llm_provider", "grok"), 1.0)


def _reply_token_budget() -> int:
    """With DeepSeek/Qwen thinking on, the reasoning shares max_tokens with
    the visible reply; a bigger ceiling keeps it from eating the reply
    (only what is generated is billed)."""
    s = db.get_settings()
    provider = s.get("llm_provider", "grok")
    if provider in ("deepseek", "qwen") and s.get(f"{provider}_thinking"):
        return MAX_REPLY_TOKENS * 2
    return MAX_REPLY_TOKENS


# ────────────────────────────────────────────────────────────────
# Lifecycle
# ────────────────────────────────────────────────────────────────

def _watch_launcher():
    """If the server was started by ame_launcher.py and the launcher dies
    without closing it (killed from Task Manager, hung), the server shuts
    itself down instead of staying orphaned and holding the port. With
    --reload this process is the child of the "reloader", which is the one
    holding the port: that one is closed too."""
    pid = int(os.environ.get("AME_LAUNCHER_PID", "0") or 0)
    if not pid:
        return

    def alive(p: int) -> bool:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x00100000, False, p)          # SYNCHRONIZE
            if not h:
                return False
            try:
                return k32.WaitForSingleObject(h, 0) == 0x00000102   # WAIT_TIMEOUT
            finally:
                k32.CloseHandle(h)
        try:
            os.kill(p, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def loop():
        import signal
        import time
        while alive(pid):
            time.sleep(2)
        parent_pid = os.getppid()
        if parent_pid not in (pid, 0, 1):
            try:
                os.kill(parent_pid, signal.SIGTERM)
            except Exception:
                pass
        os._exit(0)

    threading.Thread(target=loop, daemon=True, name="launcher-watchdog").start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    _watch_launcher()
    living_memory.init()   # living-memory cursor before the first message

    # The first time, the profile is seeded with whatever config.md has
    profile = db.get_profile()
    if not profile.get("user_name") and CONFIG_PROFILE_DEFAULTS["user_name"]:
        db.set_profile(user_name=CONFIG_PROFILE_DEFAULTS["user_name"])
    if not profile.get("user_bio") and CONFIG_PROFILE_DEFAULTS["user_bio"]:
        db.set_profile(user_bio=CONFIG_PROFILE_DEFAULTS["user_bio"])
    if not profile.get("ame_avatar"):
        db.set_profile(ame_avatar="/images/ame001.png")

    # The first time, settings are seeded with whatever config.md has
    # (after that, the database always wins).
    settings = db.get_settings()
    if not settings.get("onboarding_done"):
        seed = {k: v for k, v in DEFAULT_LLM_SETTINGS.items() if v}
        if seed:
            db.set_settings(**seed)
            settings = db.get_settings()

    # Load the embedding model in a thread so the first message doesn't
    # wait for it.
    def _preload():
        memory.get_embedder()
        memory.index_pending_chunks()

    if memory.embedder_is_cached():
        threading.Thread(target=_preload, daemon=True).start()
    else:
        # First run: the model has to be downloaded. Startup waits for it so
        # the download doesn't compete with the first message.
        print("\n" + "═" * 60)
        print("⏳ Downloading the embedding model for the first time (~470 MB).")
        print("   The server starts as soon as it finishes; next time it's instant.")
        print("═" * 60 + "\n")
        memory.get_embedder()
        print("✓ Embedding model ready.")
        threading.Thread(target=memory.index_pending_chunks, daemon=True).start()

    active_provider = settings.get("llm_provider", "grok")
    active_key = settings.get(f"{active_provider}_api_key", "")
    active_model = settings.get(f"{active_provider}_model", "")
    label = PROVIDER_LABELS.get(active_provider, active_provider)
    if not active_key:
        print("\n" + "═" * 60)
        print(f"⚠️  No API key configured for {label}.")
        print("   Fill it in in the welcome wizard or in Settings.")
        print("═" * 60 + "\n")
    else:
        print(f"→ Active engine: {label} — model: {active_model}")

    yield


app = FastAPI(title="Ame Companion", lifespan=lifespan)


# ────────────────────────────────────────────────────────────────
# Remote access (phone, another PC, or a server) — see remote_access.py
# ────────────────────────────────────────────────────────────────
_MASK = "••••••••"


def _is_local(request: Request) -> bool:
    return remote_access.is_local(request)


@app.middleware("http")
async def _remote_guard(request: Request, call_next):
    if remote_access.allowed(request):
        return await call_next(request)
    who = request.client.host if request.client else "?"
    if not remote_access.access_key():
        return HTMLResponse(remote_access.OFF_PAGE, status_code=403)
    link_key = request.query_params.get("key", "")
    if link_key and remote_access.key_ok(link_key):
        # A link with ?key=…: log the device in and clean the address bar.
        response = RedirectResponse(request.url.path or "/", status_code=303)
        _set_login_cookie(response, link_key)
        print(f"[remote] device logged in by link: {who}")
        return response
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(remote_access.login_page(), status_code=401)
    return JSONResponse({"detail": "Access key required."}, status_code=401)


def _set_login_cookie(response, key: str):
    response.set_cookie(remote_access.COOKIE, key.strip(), max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="lax")


@app.post("/login")
async def login(request: Request):
    if not remote_access.access_key():
        return HTMLResponse(remote_access.OFF_PAGE, status_code=403)
    form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
    key = (form.get("key") or [""])[0]
    who = request.client.host if request.client else "?"
    if remote_access.key_ok(key):
        response = RedirectResponse("/", status_code=303)
        _set_login_cookie(response, key)
        print(f"[remote] device logged in: {who}")
        return response
    print(f"[remote] wrong access key from {who}")
    return HTMLResponse(remote_access.login_page(error=True), status_code=401)


@app.exception_handler(HTTPException)
async def _log_http_errors(request, exc: HTTPException):
    """Errors shown to the user (API key rejected, no credits, empty reply…)
    also go to the console, so the in-app panel shows why a turn failed."""
    from fastapi.exception_handlers import http_exception_handler
    if exc.status_code >= 400 and request.url.path != "/console":
        print(f"[error] {request.url.path} → {exc.status_code}: {exc.detail}")
    return await http_exception_handler(request, exc)

_images_dir = os.path.join(BASE_DIR, "images")
_uploads_dir = os.path.join(_images_dir, "uploads")
os.makedirs(_uploads_dir, exist_ok=True)
app.mount("/images", StaticFiles(directory=_images_dir), name="images")


# ────────────────────────────────────────────────────────────────
# Input models
# ────────────────────────────────────────────────────────────────

class ChatIn(BaseModel):
    message: str
    image_path: str | None = None   # e.g. "/images/uploads/xxxx.png"


class PinIn(BaseModel):
    content: str
    priority: int = 0


class PinUpdateIn(BaseModel):
    content: str


class SettingsIn(BaseModel):
    summary_level: str | None = None       # off | low | medium | high
    reduce_repetition: bool | None = None
    dont_refuse: bool | None = None
    custom_instruction: str | None = None
    sprite_scale: int | None = None        # percentage: 100..300
    language: str | None = None            # en | es-mx | es-pe | es-ar | es-es | it | ja | ko | zh
    stage_override: int | None = None      # 0 = automatic; 1-4 = forced
    onboarding_done: bool | None = None    # set to true when the welcome wizard is completed
    llm_provider: str | None = None        # grok | qwen | deepseek | gpt
    grok_api_key: str | None = None
    grok_model: str | None = None
    qwen_api_key: str | None = None
    qwen_model: str | None = None
    deepseek_api_key: str | None = None
    deepseek_model: str | None = None
    gpt_api_key: str | None = None
    gpt_model: str | None = None
    grok_base_url: str | None = None        # optional override — a gateway/proxy such as AgentRouter, OpenRouter, etc.
    qwen_base_url: str | None = None
    deepseek_base_url: str | None = None
    gpt_base_url: str | None = None
    reasoning_effort: str | None = None     # only applies if llm_provider == grok or gpt
    qwen_thinking: bool | None = None       # only applies if llm_provider == qwen
    deepseek_thinking: bool | None = None   # only applies if llm_provider == deepseek
    polish_speech: bool | None = None       # rewrite unnatural spoken lines (on by default)


class ProfileIn(BaseModel):
    user_name: str | None = None
    user_bio: str | None = None
    user_gender: str | None = None         # male | female | other
    user_avatar: str | None = None
    ame_avatar: str | None = None
    user_likes: str | None = None
    user_dislikes: str | None = None
    user_birthday: str | None = None       # "MM-DD", never the year


class OnboardingIn(BaseModel):
    """Welcome-wizard payload: language + basic identity + AI engine, all
    in a single request so the state is never left half-saved."""
    language: str
    user_name: str
    user_gender: str    # male | female | other
    user_bio: str = ""
    llm_provider: str = "grok"    # grok | qwen | deepseek | gpt
    grok_api_key: str = ""
    grok_model: str = "grok-4.3-latest"
    reasoning_effort: str = "none"          # Grok/GPT: none | low | medium | high (+ xhigh/max on GPT)
    qwen_api_key: str = ""
    qwen_model: str = "qwen-plus"
    qwen_thinking: bool | None = None        # Qwen only (None = keep the default)
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_thinking: bool | None = None    # DeepSeek only (None = keep the default: off)
    gpt_api_key: str = ""
    gpt_model: str = "gpt-5.6-sol"
    grok_base_url: str = ""     # optional override — a gateway/proxy such as AgentRouter, OpenRouter, etc.
    qwen_base_url: str = ""
    deepseek_base_url: str = ""
    gpt_base_url: str = ""


# ────────────────────────────────────────────────────────────────
# Chat
# ────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────
# Generation + enforcer chain (shared by /chat and /continue)
# ────────────────────────────────────────────────────────────────

# One turn at a time. Two overlapping turns (a second tab, a double Enter,
# Regenerate pressed twice) would interleave their writes, so every endpoint
# that writes a turn takes this lock without waiting and answers 409 while
# Ame is busy.
_TURN_LOCK = threading.Lock()
BUSY_DETAIL = "Ame is still answering — wait for this reply to finish."


def _take_turn_lock():
    if not _TURN_LOCK.acquire(blocking=False):
        raise HTTPException(409, BUSY_DETAIL)


def _release_turn_lock():
    try:
        _TURN_LOCK.release()
    except RuntimeError:
        pass


def _generate_without_repeating(messages: list, previous: str | None) -> str:
    """Calls the model and, if the reply copies Ame's previous one, retries
    once with an explicit instruction. A second copy is delivered anyway
    and logged."""
    temperature = _chat_temperature()
    budget = _reply_token_budget()
    raw = call_llm(messages, max_tokens=budget, temperature=temperature)
    if not enforcers.is_repetition(raw, previous):
        return raw
    print("[enforce] repetition of the previous turn detected — retrying once")
    raw2 = call_llm(prompting.add_turn_note(messages, enforcers.REPETITION_INSTRUCTION),
                    max_tokens=budget, temperature=temperature)
    if enforcers.is_repetition(raw2, previous):
        print("[enforce] the retry also repeats — delivering it anyway")
    return raw2


def _enforcer_chain(raw: str, dice_state: dict, continuation: bool = False) -> str:
    """Post-processing of the model's reply, shared by every turn endpoint.

    1. Language/dialect check: a regex, and a rewrite only if text from
       another language or the wrong dialect leaked in.
    2. Line editor ("Polish her lines", on by default): the main model fixes
       the few lines that read translated or robotic, a job title used as a
       name, "You…" openers, narrator commentary. It never adds text of its
       own; texture comes only from the per-turn notes.
    3. Inner voice « »: written by a cheap call if the model left it out;
       exactly one is kept, never as the opener.
    4. Final dialect check, since the inner voice came from another call.
    """
    lang = dice_state["language"]
    raw = enforcers.enforce_language(raw, lang)
    settings = db.get_settings()
    if settings.get("polish_speech", 1):
        raw = enforcers.humanize_reply(
            raw, lang,
            user_name=dice_state.get("user_name", ""),
            job_terms=dice_state.get("job_terms", ""),
            job_in_message=dice_state.get("job_in_message", False),
            live=dice_state.get("live", False))
    # A continuation belongs to a reply that already has its « » line.
    if not continuation:
        raw = enforcers.enforce_inner_voice(raw, lang)
    raw = enforcers.tidy_inner_voice(raw)
    raw = enforcers.soften_cutoffs(raw, lang)
    return enforcers.enforce_dialect_final(raw, lang)


@app.post("/chat")
def chat(payload: ChatIn, background_tasks: BackgroundTasks):
    if payload.image_path and prompting.valid_upload_path(payload.image_path) is None:
        raise HTTPException(400, "Invalid image path")
    # The prompt is built before the user's message is saved, so the
    # message isn't sent twice (once from the history, once as the turn).
    t0 = time.time()
    _take_turn_lock()
    try:
        print(f"[turn] ── new message ({len(payload.message)} chars"
              + (", with image" if payload.image_path else "") + ") ──")
        messages, dice_state = prompting.build_messages(payload.message, image_path=payload.image_path)
        previous = db.get_last_assistant_message()
        raw_reply = _generate_without_repeating(messages, previous["content"] if previous else None)
        reply, sprite, _info = _finish_turn(payload, raw_reply, dice_state, background_tasks, t0)
    finally:
        _release_turn_lock()
    return {"reply": reply, "sprite": sprite, "auto_summary": False}


# Runaway guard: her reply ends at the first complete sprite tag, or where
# the model starts writing another turn.
_SPRITE_DONE_RE = re.compile(r"\[\[\s*sprite\s*[:=][^\]\n]*\]\]", re.IGNORECASE)
_ROLE_LEAK_RE = re.compile(r"\n\s*(assistant|user|system)\s*:?\s*(\n|(?=[A-Z¿¡—\"«]))")


# The app shows plain text: <br>/<p> become line breaks, other tags and
# markdown emphasis are removed.
_BR_RE = re.compile(r"\s*<\s*(br|/p|p)\s*/?\s*>\s*", re.IGNORECASE)
_TAG_RE = re.compile(r"</?\s*(b|i|u|em|strong|span|div|small|s|mark|font)\b[^>]*>", re.IGNORECASE)
_MD_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.S)


def clean_markup(text: str) -> str:
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _MD_BOLD_RE.sub(r"\2", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def trim_runaway(text: str) -> str:
    text = clean_markup(text)
    m = _SPRITE_DONE_RE.search(text)
    if m:
        text = text[:m.end()]
    m = _ROLE_LEAK_RE.search(text)
    if m:
        print("[enforce] the model started another turn — cut there")
        text = text[:m.start()].rstrip()
    return text


def _finish_turn(payload: ChatIn, raw_reply: str, dice_state: dict,
                 background_tasks: BackgroundTasks, t0: float,
                 earlier_versions: list[dict] | None = None) -> tuple[str, str | None, dict]:
    """Shared by /chat, /chat_stream and /regenerate_stream: enforcers,
    sprite, saving the turn and the background work. Returns (reply,
    sprite_url, versions info). `earlier_versions`: the replies this one
    regenerates, kept so the user can go back to them."""
    raw_reply = _enforcer_chain(trim_runaway(raw_reply), dice_state)
    reply, sprite, sprite_tag = sprites.resolve_sprite(
        raw_reply, stream_cue=dice_state.get("stream_cue"),
        expected_mode=dice_state.get("expected_mode"))
    # The user's turn is saved only once generation succeeded, so a failed
    # call that the user retries doesn't leave their message duplicated.
    # remember_fast stores the rows now; embeddings (CPU work only needed
    # by FUTURE searches) are computed in the background.
    user_id = memory.remember_fast("user", payload.message, image_path=payload.image_path)
    assistant_id = memory.remember_fast("assistant", reply, sprite=sprite,
                                        sprite_tag=sprite_tag)
    if dice_state.get("pending_birthday_mark"):
        db.mark_birthday_greeted()      # only once the turn really went through
    info = {"versions_n": 1, "version_idx": 0}
    if earlier_versions:
        versions = (earlier_versions + [db.snapshot_version(db.get_message_row(assistant_id))])[-10:]
        db.set_versions(assistant_id, versions, len(versions) - 1)
        info = {"versions_n": len(versions), "version_idx": len(versions) - 1}
    background_tasks.add_task(memory.embed_deferred, user_id, payload.message)
    background_tasks.add_task(memory.embed_deferred, assistant_id, reply)
    background_tasks.add_task(memory.maybe_summarize)
    background_tasks.add_task(living_memory.consolidate)   # per-person living memory
    print(f"[turn] reply ready in {time.time() - t0:.1f}s — {len(reply)} chars, "
          f"sprite tag: {sprite_tag or 'none'}; memory, summary and embeddings "
          "continue in the background")
    return reply, sprite, info


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/chat_stream")
def chat_stream(payload: ChatIn):
    """Same turn as /chat, streamed: the reply shows up as it's written.
    Server-sent events: `thinking` (the model is reasoning), `delta`
    {"t": text}, then `done` {"reply", "sprite"} with the final text after
    post-processing (the app swaps the streamed bubble for it), or `error`
    {"detail"}."""
    if payload.image_path and prompting.valid_upload_path(payload.image_path) is None:
        raise HTTPException(400, "Invalid image path")
    # The turn runs in its own thread and feeds the response through a
    # queue, so it still finishes and saves if the browser goes away
    # mid-reply; the app then picks the reply up from /history.
    _take_turn_lock()
    try:
        return _stream_turn(payload)
    except BaseException:
        _release_turn_lock()
        raise


REGENERATE_NOTE = (
    "This is a second take on this reply: answer the same message again "
    "with a fresh approach — a different way into the moment, different "
    "wording — while keeping the scene, her mood and the facts the same.")


def _stream_turn(payload: ChatIn, earlier_versions: list[dict] | None = None,
                 on_fail=None) -> StreamingResponse:
    """The streamed turn shared by /chat_stream and /regenerate_stream.
    The caller holds _TURN_LOCK; the worker releases it once the turn is
    saved (or put back), before the slow background work."""
    q: queue.Queue = queue.Queue()
    later = _Later()
    regenerating = earlier_versions is not None

    def work():
        t0 = time.time()
        held = [True]

        def release():           # exactly once: never release a lock someone else took
            if held[0]:
                held[0] = False
                _release_turn_lock()

        print(f"[turn] ── {'regenerate' if regenerating else 'new message'} "
              f"({len(payload.message)} chars"
              + (", with image" if payload.image_path else "") + ", streaming) ──")
        try:
            messages, dice_state = prompting.build_messages(
                payload.message, image_path=payload.image_path)
            if regenerating:
                messages = prompting.add_turn_note(messages, REGENERATE_NOTE)
            previous = db.get_last_assistant_message()
            parts = []
            stream = stream_llm(messages, max_tokens=_reply_token_budget(),
                                temperature=_chat_temperature())
            for kind, text in stream:
                if kind == "thinking":
                    q.put(("thinking", {}))
                    continue
                parts.append(text)
                q.put(("delta", {"t": text}))
                joined = "".join(parts)
                if _SPRITE_DONE_RE.search(joined) or _ROLE_LEAK_RE.search(joined):
                    stream.close()          # her reply is over: stop paying for more
                    break
            raw = "".join(parts)
            if enforcers.is_repetition(raw, previous["content"] if previous else None):
                print("[enforce] repetition of the previous turn detected — retrying once")
                q.put(("thinking", {}))
                raw = call_llm(prompting.add_turn_note(messages, enforcers.REPETITION_INSTRUCTION),
                               max_tokens=_reply_token_budget(),
                               temperature=_chat_temperature())
            reply, sprite, info = _finish_turn(payload, raw, dice_state, later, t0,
                                               earlier_versions=earlier_versions)
            release()
            q.put(("done", {"reply": reply, "sprite": sprite, **info}))
        except Exception as e:                       # never leave the bubble hanging
            detail = str(e.detail) if isinstance(e, HTTPException) else f"{type(e).__name__}: {e}"
            print(f"[error] {'/regenerate_stream' if regenerating else '/chat_stream'} → {detail}")
            if on_fail:
                try:
                    on_fail()
                except Exception as e2:
                    print(f"[error] couldn't put the previous reply back: {e2}")
            q.put(("error", {"detail": detail}))
        finally:
            release()
            q.put(None)
        later.run()                                   # memory, summary, embeddings

    threading.Thread(target=work, daemon=True, name="turn").start()

    def events():
        while True:
            item = q.get()
            if item is None:
                return
            yield _sse(*item)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/regenerate_stream")
def regenerate_stream():
    """Writes Ame's last reply again, for the same message. The old reply
    isn't lost: it becomes an earlier version of the new one (‹ 1/2 ›).
    The last user message and reply are taken out first, so the new turn
    is built exactly as the original one was; if generation fails, both
    are put back as they were."""
    _take_turn_lock()
    try:
        return _start_regenerate()
    except BaseException:
        _release_turn_lock()          # nothing started: free it
        raise


def _start_regenerate() -> StreamingResponse:
    turn = db.get_last_turn()
    if not turn:
        raise HTTPException(400, "The last message isn't a reply from Ame to regenerate")
    user_row, ame_row = turn
    versions = db._versions(ame_row.get("versions"))
    if versions:
        idx = ame_row.get("version_idx") or 0
        if 0 <= idx < len(versions):
            versions[idx] = db.snapshot_version(ame_row)   # keeps any Continue on it
    else:
        versions = [db.snapshot_version(ame_row)]
    saved_mode = db.get_mode()

    def put_back():
        db.restore_message(user_row)
        db.restore_message(ame_row)
        db.set_mode(saved_mode)
        print("[regenerate] failed — the previous reply was put back")

    db.delete_messages([user_row["id"], ame_row["id"]])
    try:
        # The persona as it was before that reply (it may have switched it).
        before = db.get_last_assistant_message()
        db.set_mode(sprites.mode_of_tag(before["sprite_tag"])
                    if before and before["sprite_tag"] else "ame")
        payload = ChatIn(message=user_row["content"], image_path=user_row.get("image_path"))
        return _stream_turn(payload, earlier_versions=versions, on_fail=put_back)
    except BaseException:
        put_back()
        raise


class VersionIn(BaseModel):
    index: int


@app.post("/select_version")
def select_version(payload: VersionIn):
    """Shows another version of Ame's last reply (the row takes its text
    and sprites, so the next turn's prompt sees the one on screen)."""
    _take_turn_lock()
    try:
        return _select_version(payload)
    finally:
        _release_turn_lock()


def _select_version(payload: VersionIn) -> dict:
    turn = db.get_last_turn()
    if not turn:
        raise HTTPException(400, "Only Ame's last reply has versions")
    _user_row, row = turn
    versions = db._versions(row.get("versions"))
    if not 0 <= payload.index < len(versions):
        raise HTTPException(400, "No such version")
    cur = row.get("version_idx") or 0
    if 0 <= cur < len(versions):
        versions[cur] = db.snapshot_version(row)         # keep its Continue, if any
    target = versions[payload.index]
    vec = memory.embed(target["content"]).astype("float32").tobytes()
    db.apply_version(row["id"], target, vec, payload.index, versions)
    memory.index_chunks(row["id"], target["content"])
    if target.get("sprite_tag"):
        db.set_mode(sprites.mode_of_tag(target["sprite_tag"]))
    return {"reply": target["content"],
            "sprites": db._sprite_list(target.get("sprite_seq"), target.get("sprite")),
            "versions_n": len(versions), "version_idx": payload.index,
            "continued": bool(target.get("continued"))}


class _Later:
    """Collects background work like FastAPI's BackgroundTasks, but runs it
    on demand — in the turn's own thread, so it also runs when the browser
    disconnected before the response finished."""

    def __init__(self):
        self._tasks = []

    def add_task(self, fn, *args, **kwargs):
        self._tasks.append((fn, args, kwargs))

    def run(self):
        for fn, args, kwargs in self._tasks:
            try:
                fn(*args, **kwargs)
            except Exception as e:
                print(f"[background] {getattr(fn, '__name__', fn)} failed: {e}")


@app.post("/continue")
def continue_last():
    """Extends Ame's last reply without a visible user turn."""
    _take_turn_lock()
    try:
        return _continue_last()
    finally:
        _release_turn_lock()


def _continue_last() -> dict:
    last = db.get_last_assistant_message()
    if not last:
        raise HTTPException(400, "There's no Ame message yet to continue")
    # Once per reply: the reply and its continuation, two sprites that the
    # app plays in a loop.
    if last["continued"]:
        raise HTTPException(400, "This reply was already continued")

    t0 = time.time()
    print("[turn] ── continue ──")
    # Semantic memories are searched with the real scene (the end of her
    # last reply), not with an instruction.
    messages, dice_state = prompting.build_messages(
        "", retrieval_query=last["content"][-600:], continuation=True)
    raw = _generate_without_repeating(messages, last["content"])
    raw = _enforcer_chain(trim_runaway(raw), dice_state, continuation=True)
    raw = enforcers.drop_repeated_lines(raw, last["content"])
    continuation, sprite, sprite_tag = sprites.resolve_sprite(raw)
    if not continuation.strip():
        # Nothing new survived (it only repeated the reply): don't spend the
        # reply's one Continue on an empty extension.
        raise HTTPException(502, "The continuation came back empty — try Continue again.")

    # The full text and its embedding are written together.
    memory.remember_continuation(last["id"], last["content"], continuation,
                                 sprite=sprite, sprite_tag=sprite_tag)
    sprite_list = db.get_sprites(last["id"])
    if dice_state.get("pending_birthday_mark"):
        db.mark_birthday_greeted()
    print(f"[turn] continuation ready in {time.time() - t0:.1f}s — {len(continuation)} chars")
    return {"continuation": continuation, "sprite": sprite, "sprites": sprite_list}


@app.get("/opening")
def opening():
    """When the app loads with no history: returns the opening scene and
    stores it as Ame's first message."""
    scenario = opening_for(db.get_settings().get("language"))
    # Two devices loading an empty chat at once must not both store it.
    with _OPENING_LOCK:
        if db.count_messages() > 0 or not scenario:
            return {"scenario": None}
        memory.remember("assistant", scenario)
    return {"scenario": scenario}


_OPENING_LOCK = threading.Lock()


def _swap_opening_language() -> bool:
    """If the chat holds nothing but the opening scene and the language
    changes (the wizard, or 🌐 before the first message), the scene is
    replaced by the same scene in the new language. Returns True if it did."""
    with _OPENING_LOCK:
        return _swap_opening_locked()


def _swap_opening_locked() -> bool:
    if db.count_messages() != 1:
        return False
    last = db.get_last_assistant_message()
    if not last or last["content"] not in OPENING_SCENARIOS.values():
        return False
    scenario = opening_for(db.get_settings().get("language"))
    if not scenario or scenario == last["content"]:
        return False
    vec = memory.embed(scenario).astype("float32").tobytes()
    db.update_message_content(last["id"], scenario, vec)
    memory.index_chunks(last["id"], scenario)
    print("[opening] language changed before the first message — opening scene swapped")
    return True


@app.get("/history")
def history(limit: int = 50):
    rows = db.get_history(limit)
    for r in rows:                      # images replaced in the Sprite Manager
        r["sprite"] = sprites.fix_url(r.get("sprite"))
        r["sprites"] = [u for u in (sprites.fix_url(x) for x in r.get("sprites") or []) if u]
    return rows


# ────────────────────────────────────────────────────────────────
# Images
# ────────────────────────────────────────────────────────────────

# Images are read into memory and later sent inside the prompt as base64;
# 20 MB covers any phone photo.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@app.post("/upload_image")
async def upload_image(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise HTTPException(400, "Unsupported image format")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"Image too large ({len(data) / 1024 / 1024:.1f}MB). "
            f"Maximum {MAX_UPLOAD_BYTES // 1024 // 1024}MB.",
        )
    filename = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_uploads_dir, filename), "wb") as f:
        f.write(data)
    return {"path": f"/images/uploads/{filename}"}


# ────────────────────────────────────────────────────────────────
# Pins
# ────────────────────────────────────────────────────────────────

@app.post("/pins")
def create_pin(payload: PinIn):
    return {"ok": True, "id": db.add_pin(payload.content, payload.priority)}


@app.get("/pins")
def list_pins():
    return {"pins": db.get_all_pins()}


@app.put("/pins/{pin_id}")
def edit_pin(pin_id: int, payload: PinUpdateIn):
    db.update_pin(pin_id, payload.content)
    return {"ok": True}


@app.delete("/pins/{pin_id}")
def remove_pin(pin_id: int):
    db.delete_pin(pin_id)
    return {"ok": True}


# ────────────────────────────────────────────────────────────────
# Summaries
# ────────────────────────────────────────────────────────────────

@app.get("/summaries")
def list_summaries():
    """The summaries exactly as they are sent to the model."""
    overview = db.get_summaries_overview()
    level = db.get_settings().get("summary_level", "medium")
    return {
        "summaries": overview["summaries"],
        "level": level,
        "unsummarized_count": overview["total_messages"] - overview["last_summarized_id"],
        "threshold": SUMMARY_LEVEL_TO_INTERVAL.get(level),
    }


@app.post("/recalibrate")
def recalibrate():
    """Recalibrate: condenses what's pending into a summary and stops
    sending the earlier conversation word for word. Useful to start a clean
    scene or after editing the prompt, since the model imitates the style of
    the messages it sees. Nothing is deleted."""
    summary = memory.force_summarize()   # nothing is lost: it's condensed before cutting
    result = db.recalibrate_buffer()
    return {"ok": True, "summarized": summary is not None, **result}


@app.post("/resummarize")
def resummarize():
    summary = memory.force_summarize()
    if summary is None:
        return {"ok": False, "message": "No new messages to summarize"}
    return {"ok": True, "summary": summary}


# ────────────────────────────────────────────────────────────────
# Profile, settings, diagnostics
# ────────────────────────────────────────────────────────────────

@app.get("/profile")
def read_profile():
    return db.get_profile()


@app.post("/profile")
def update_profile(payload: ProfileIn):
    db.set_profile(**payload.model_dump())
    return {"ok": True}


@app.get("/onboarding_status")
def onboarding_status():
    """The frontend checks this on every load to decide whether to show
    the welcome wizard or go straight to the chat."""
    return {"done": bool(db.get_settings().get("onboarding_done"))}


@app.post("/onboarding")
def complete_onboarding(payload: OnboardingIn):
    """Saves language + identity + AI engine from the welcome wizard in a
    single step, and sets onboarding_done so it doesn't show up again."""
    db.set_settings(
        language=payload.language,
        onboarding_done=True,
        llm_provider=payload.llm_provider,
        grok_api_key=payload.grok_api_key.strip(),
        grok_model=payload.grok_model,
        reasoning_effort=payload.reasoning_effort,
        qwen_api_key=payload.qwen_api_key.strip(),
        qwen_model=payload.qwen_model,
        qwen_thinking=payload.qwen_thinking,
        deepseek_api_key=payload.deepseek_api_key.strip(),
        deepseek_model=payload.deepseek_model,
        deepseek_thinking=payload.deepseek_thinking,
        gpt_api_key=payload.gpt_api_key.strip(),
        gpt_model=payload.gpt_model,
        grok_base_url=payload.grok_base_url.strip(),
        qwen_base_url=payload.qwen_base_url.strip(),
        deepseek_base_url=payload.deepseek_base_url.strip(),
        gpt_base_url=payload.gpt_base_url.strip(),
    )
    db.set_profile(
        user_name=payload.user_name.strip(),
        user_gender=payload.user_gender,
        user_bio=payload.user_bio.strip(),
    )
    _swap_opening_language()       # e.g. the wizard was run again
    return {"ok": True}


@app.get("/settings")
def read_settings(request: Request):
    s = db.get_settings()
    if not _is_local(request):
        # Remote device: never send API keys over the network.
        for k in list(s):
            if k.endswith("_api_key") and s[k]:
                s[k] = _MASK
    return s


@app.post("/settings")
def update_settings(payload: SettingsIn):
    data = payload.model_dump()
    for k, v in data.items():          # a masked key coming back from a remote device
        if k.endswith("_api_key") and isinstance(v, str) and v.startswith(_MASK):
            data[k] = None             # None = leave the saved key untouched
    db.set_settings(**data)
    swapped = bool(data.get("language")) and _swap_opening_language()
    return {"ok": True, "opening_swapped": swapped}


@app.get("/console")
def console(after: int = 0):
    """The server console for the in-app panel (see console_log.py)."""
    return console_log.since(after)


@app.get("/cache_stats")
def cache_stats():
    """Latest prompt-cache statistics. If cached_tokens stays at 0 turn after
    turn, something is breaking the stable prefix."""
    if not LAST_CACHE_STATS:
        return {"ok": False, "message": "No calls have been made yet"}
    return {"ok": True, **LAST_CACHE_STATS}


@app.get("/sprite_status")
def sprite_status():
    """Which emotions have an image, how many times each was shown, and the
    current mode."""
    with db.db_ctx() as conn:
        shown = {r["sprite_tag"]: r["c"] for r in conn.execute(
            "SELECT sprite_tag, COUNT(*) c FROM messages "
            "WHERE sprite_tag IS NOT NULL AND sprite IS NOT NULL GROUP BY sprite_tag")}
    report = [{"tag": tag, "file_exists": bool(sprites.files_for(tag)),
               "variants": len(sprites.files_for(tag)), "times_shown": shown.get(tag, 0),
               "strong": tag in sprites.STRONG_EMOTION_TAGS}
              for tag in sprites.SPRITE_CATALOG]
    return {
        "current_mode": db.get_mode(),
        "fallback_classifier": sprites.SPRITE_FALLBACK_ENABLED,
        "missing_files": [r["tag"] for r in report if not r["file_exists"]],
        "sprites": report,
    }


# ── Sprite manager (/sprites) ────────────────────────────────────────
# Removed images go to images/sprites/_trash/, never deleted outright.

@app.get("/sprites_api/catalog")
def sprites_catalog():
    with db.db_ctx() as conn:
        shown = {r["sprite_tag"]: r["c"] for r in conn.execute(
            "SELECT sprite_tag, COUNT(*) c FROM messages "
            "WHERE sprite_tag IS NOT NULL GROUP BY sprite_tag")}
    items = sprites.manager_catalog()
    for it in items:
        it["times_used"] = shown.get(it["tag"], 0)
    return {"emotions": items, "formats": list(sprites.SPRITE_EXTS),
            "max_mb": sprites.MAX_SPRITE_BYTES // 1024 // 1024}


@app.post("/sprites_api/upload")
async def sprites_upload(tag: str = Form(...), mode: str = Form("replace"),
                         file: UploadFile = File(...)):
    data = await file.read(sprites.MAX_SPRITE_BYTES + 1)
    try:
        name = await run_in_threadpool(sprites.save_sprite, tag, data,
                                       "add" if mode == "add" else "replace")
    except ValueError as e:
        raise HTTPException(400, str(e))
    print(f"[sprites] {tag}: {'added' if mode == 'add' else 'replaced with'} {name}")
    return {"ok": True, "name": name}


class SpriteFileIn(BaseModel):
    tag: str
    name: str


@app.post("/sprites_api/delete_file")
def sprites_delete_file(payload: SpriteFileIn):
    try:
        sprites.delete_sprite_file(payload.tag, payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    print(f"[sprites] {payload.tag}: {payload.name} moved to images/sprites/_trash/")
    return {"ok": True}


class SpriteTagIn(BaseModel):
    tag: str
    description: str = ""


@app.post("/sprites_api/add_emotion")
def sprites_add_emotion(payload: SpriteTagIn):
    try:
        sprites.add_custom_tag(payload.tag, payload.description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    print(f"[sprites] new emotion: {payload.tag.strip().lower()}")
    return {"ok": True}


@app.post("/sprites_api/remove_emotion")
def sprites_remove_emotion(payload: SpriteTagIn):
    try:
        sprites.remove_custom_tag(payload.tag)
    except ValueError as e:
        raise HTTPException(400, str(e))
    print(f"[sprites] emotion removed: {payload.tag}")
    return {"ok": True}


@app.get("/sprites", response_class=HTMLResponse)
def sprite_manager_page():
    with open(os.path.join(BASE_DIR, "static", "sprites.html"), encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), media_type="text/html; charset=utf-8")


# ────────────────────────────────────────────────────────────────
# Frontend
# ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), media_type="text/html; charset=utf-8")
