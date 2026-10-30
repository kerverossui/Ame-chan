"""
One adapter for every engine (Grok, Qwen, DeepSeek, GPT), all speaking
OpenAI's Chat Completions schema: retries, readable errors, cache
statistics and truncation detection. The engine, model and key are read from
the settings on every call, so a change in Settings applies immediately.
"""

import time

import requests
from fastapi import HTTPException

import database as db
from app_config import PROVIDER_LABELS, provider_endpoint

# Stable conversation ID. Grok (xAI) caches repeated prefixes on its own;
# the x-grok-conv-id header raises the hit rate, and the other engines ignore
# it. Each kind of call passes its own id so they don't evict each other.
CONV_ID = "ame-app-local"

# Latest cache statistics, for the /cache_stats endpoint.
LAST_CACHE_STATS: dict = {}

# What each call is for, by its conv_id — shown in the console log.
CALL_PURPOSE = {
    None: "Ame's reply",
    "ame-app-local-innervoice-classifier": "inner-voice line",
    "ame-app-local-shortline-classifier": "short spoken line",
    "ame-app-local-language-fix": "language/dialect fix",
    "ame-app-local-speech-polish": "speech polish",
    "ame-app-living-memory": "living memory",
}


def _purpose(conv_id: str | None) -> str:
    if conv_id in CALL_PURPOSE:
        return CALL_PURPOSE[conv_id]
    if conv_id and "summ" in conv_id:
        return "conversation summary"
    if conv_id and "sprite" in conv_id:
        return "sprite classifier"
    return conv_id or "Ame's reply"


def _resolve_provider_config(provider: str | None, model: str | None) -> tuple[str, str, str, str]:
    """Returns (provider, api_key, model, base_url_override) from the
    settings unless given. base_url_override is empty unless the user set
    their own gateway for that engine."""
    settings = db.get_settings()
    provider = provider or settings.get("llm_provider", "grok")
    api_key = settings.get(f"{provider}_api_key", "")
    resolved_model = model or settings.get(f"{provider}_model", "")
    base_url_override = settings.get(f"{provider}_base_url", "")
    return provider, api_key, resolved_model, base_url_override


def _build_request(messages: list, max_tokens: int, temperature: float,
                   conv_id: str | None, provider: str | None, model: str | None,
                   reasoning: str | None):
    """Everything both call_llm and stream_llm send: returns
    (provider, label, model, url, headers, payload)."""
    provider, api_key, model, base_url_override = _resolve_provider_config(provider, model)
    label = PROVIDER_LABELS.get(provider, provider)

    if not api_key:
        raise HTTPException(500, f"No API key configured for {label}. "
                             "Set it in the welcome wizard or in Settings.")

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if provider in ("grok", "gpt"):
        # GPT also takes reasoning_effort as a top-level field, with more levels
        # than Grok (xhigh/max); the frontend only offers the valid ones.
        settings = db.get_settings()
        effort = settings.get("reasoning_effort", "none") if reasoning == "default" else reasoning
        if effort:
            payload["reasoning_effort"] = effort
    elif provider in ("qwen", "deepseek"):
        # On/off switch, always sent explicitly.
        if reasoning is None:
            thinking_on = False
        else:
            settings = db.get_settings()
            thinking_on = bool(settings.get(f"{provider}_thinking"))
        if provider == "qwen":
            # DashScope's name for it; non-streaming calls to reasoning
            # models fail if the field is missing entirely.
            payload["enable_thinking"] = thinking_on
        else:
            # DeepSeek's own field. Thinking is on by default for the V4 models,
            # so it is always sent, on or off.
            payload["thinking"] = {"type": "enabled" if thinking_on else "disabled"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "grok":
        headers["x-grok-conv-id"] = conv_id or CONV_ID

    url = provider_endpoint(provider, base_url_override)
    return provider, label, model, url, headers, payload


def call_llm(messages: list, max_tokens: int = 800, temperature: float = 1.0,
             conv_id: str | None = None, provider: str | None = None,
             model: str | None = None, reasoning: str | None = "default",
             _is_retry: bool = False) -> str:
    """Calls the configured engine. provider/model pick a one-off
    combination (e.g. a cheaper model of the same engine) without touching
    the settings. reasoning='default' uses the saved value; None leaves it
    out. reasoning_effort is only sent to the engines that support it
    (Grok, GPT).

    _is_retry is internal: it stops the empty-reply retry from looping."""
    provider, label, model, url, headers, payload = _build_request(
        messages, max_tokens, temperature, conv_id, provider, model, reasoning)
    purpose = _purpose(conv_id)
    thinking_label = ""
    if "thinking" in payload:
        thinking_label = f", thinking {payload['thinking']['type']}"
    elif "enable_thinking" in payload:
        thinking_label = f", thinking {'on' if payload['enable_thinking'] else 'off'}"
    elif payload.get("reasoning_effort"):
        thinking_label = f", reasoning {payload['reasoning_effort']}"
    print(f"[llm] → {purpose}: {label} {model} (temp {temperature}{thinking_label}, "
          f"max {max_tokens} tokens)")
    t_start = time.time()

    # Retries with backoff for 429 and 5xx
    for attempt in range(4):
        try:
            # A short connect timeout fails fast when the API can't be reached; the
            # read timeout is long so a long reply isn't cut.
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=(10, 180))
        except requests.exceptions.RequestException:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise HTTPException(503, f"Couldn't reach the {label} API. Is there an internet connection?")

        if resp.status_code == 429:
            # Most of these APIs use 429 both for rate limits and for
            # exhausted credits.
            body_text = (resp.text or "").lower()
            if "credit" in body_text or "quota" in body_text or "billing" in body_text or "insufficient" in body_text:
                raise HTTPException(429,
                    f"No credits left on the {label} account. Top up in the provider's "
                    "dashboard to continue. (The API returns 429 both for rate "
                    "limits and for exhausted credits; this message says credits.)")
            if attempt < 3:
                wait_s = int(resp.headers.get("retry-after", 2 ** attempt))
                print(f"[rate limit] 429 ({label}) — retry {attempt+1}/3 in {wait_s}s")
                time.sleep(wait_s)
                continue
            raise HTTPException(429,
                f"{label} request limit reached. Wait a moment and "
                "try again — your message hasn't been sent yet.")

        if resp.status_code == 401:
            detail = (resp.text or "")[:300]
            raise HTTPException(401,
                f"The {label} API key was rejected{f' — {detail}' if detail else ''}. "
                "Check it in Settings or in the welcome wizard. If you're using a "
                "gateway/proxy (AgentRouter, OpenRouter, etc.), also make sure the "
                "custom base URL is set — without it, the gateway's key is "
                "sent to the provider's official API, which will reject it anyway.")

        if resp.status_code == 400:
            # Request rejected by the provider (invalid payload, filtered content…):
            # pass the real reason on to the app.
            detail = (resp.text or "")[:300]
            raise HTTPException(502,
                f"{label} rejected the request (400): {detail or 'no details'}")

        if resp.status_code >= 500:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise HTTPException(502, f"{label} returned {resp.status_code}. Try again in a moment.")

        # Any other status code becomes a readable error too.
        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            detail = (resp.text or "")[:300]
            raise HTTPException(502,
                f"{label} returned an unexpected {resp.status_code}: "
                f"{detail or 'no details'}")
        break

    data = resp.json()
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError):
        raise HTTPException(502,
            f"{label} returned a reply without choices. Try again in a moment.")

    # Truncation: if the model ran out of tokens, the end of the reply (where
    # the sprite tag goes) is lost. Logged so it can be matched with missing
    # sprites.
    finish = choice.get("finish_reason")
    if finish == "length":
        print("[truncated] finish_reason=length — the reply was cut by "
              "max_tokens; the sprite tag was probably lost this turn.")

    # Cache statistics: 0 cached turn after turn means something is breaking
    # the stable prefix. Engines that don't report it just show 0%.
    try:
        usage = data.get("usage", {}) or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        # OpenAI reports cached tokens under prompt_tokens_details; DeepSeek and
        # others use top-level fields. Both are read; cache_source says which one
        # answered. Field names change without notice, so a miss degrades to 0.
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        cache_source = "prompt_tokens_details.cached_tokens"
        if not cached:
            for alt in ("prompt_cache_hit_tokens", "cached_tokens",
                        "cache_read_input_tokens"):
                if usage.get(alt):
                    cached = usage[alt]
                    cache_source = alt
                    break
        cached = cached or 0
        completion = usage.get("completion_tokens", 0)
        pct = (cached / prompt_tokens * 100) if prompt_tokens else 0.0
        LAST_CACHE_STATS.update({
            "provider": provider,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached,
            "uncached_tokens": prompt_tokens - cached,
            "completion_tokens": completion,
            "cache_hit_pct": round(pct, 1),
            "cache_source": cache_source if cached else "not reported by the provider",
            "model": payload["model"],
            "finish_reason": finish,
        })
        print(f"[llm] ← {purpose}: {time.time() - t_start:.1f}s, "
              f"finish={finish or '?'}")
        print(
            f"[cache:{provider}] input {prompt_tokens} | cached {cached} "
            f"({pct:.0f}%) | new {prompt_tokens - cached} | "
            f"output {completion}"
            + ("" if cached else " | (the provider reported no cache in "
                                "any known field)")
        )
    except Exception as e:
        print(f"[cache] couldn't read usage: {type(e).__name__}: {e}")

    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip():
        # Empty reply: reasoning can eat the whole budget, however large. Retry
        # once with reasoning off; a reply without it is better than no reply.
        thinking_was_on = (
            payload.get("enable_thinking") is True
            or (payload.get("thinking") or {}).get("type") == "enabled"
            or (payload.get("reasoning_effort") not in (None, "none"))
        )
        if thinking_was_on and not _is_retry:
            print(f"[reasoning-empty] {label} returned empty with reasoning "
                  f"on (finish_reason: {finish or 'unknown'}) — "
                  "retrying ONCE without reasoning instead of breaking "
                  "the turn.")
            return call_llm(messages, max_tokens=max_tokens, temperature=temperature,
                            conv_id=conv_id, provider=provider, model=model,
                            reasoning=None, _is_retry=True)
        # Engines also return empty content now and then with reasoning off
        # (the output tokens are still billed). One plain retry saves the turn.
        if not _is_retry:
            print(f"[empty-content] {label} returned empty content "
                  f"(finish_reason: {finish or 'unknown'}, "
                  f"output tokens billed: "
                  f"{LAST_CACHE_STATS.get('completion_tokens', '?')}) — "
                  "retrying ONCE before breaking the turn.")
            return call_llm(messages, max_tokens=max_tokens, temperature=temperature,
                            conv_id=conv_id, provider=provider, model=model,
                            reasoning=reasoning, _is_retry=True)
        # Still empty: an explicit error. The user's message isn't lost, since
        # it is only saved after a successful reply.
        raise HTTPException(502,
            "The model returned an empty reply "
            f"(finish_reason: {finish or 'unknown'}). This usually happens when "
            "internal reasoning used up the token limit — try again; "
            "if it keeps happening, lower the reasoning effort in Settings.")
    return content


# ────────────────────────────────────────────────────────────────
# Streaming: same request as call_llm plus "stream": true; every engine
# here speaks the OpenAI SSE format.
# ────────────────────────────────────────────────────────────────

def stream_llm(messages: list, max_tokens: int = 800, temperature: float = 1.0,
               conv_id: str | None = None, provider: str | None = None,
               model: str | None = None, reasoning: str | None = "default"):
    """Generator of events: ("thinking", None) while the model reasons,
    ("delta", text) for each piece of the visible reply. Anything that
    goes wrong BEFORE the first piece of text (rate limit, 5xx, a provider
    that refuses streaming, an empty reply) falls back to call_llm, which
    has all the retries and readable errors; its full text then arrives as
    a single delta. Errors after text started are raised as HTTPException."""
    import json
    provider, label, model_name, url, headers, payload = _build_request(
        messages, max_tokens, temperature, conv_id, provider, model, reasoning)
    payload = dict(payload, stream=True, stream_options={"include_usage": True})
    purpose = _purpose(conv_id)

    def fallback(reason: str):
        print(f"[llm] streaming unavailable ({reason}) — using a normal request")
        text = call_llm(messages, max_tokens=max_tokens, temperature=temperature,
                        conv_id=conv_id, provider=provider, model=model,
                        reasoning=reasoning)
        yield ("delta", text)

    print(f"[llm] → {purpose} (streaming): {label} {model_name} "
          f"(temp {temperature}, max {max_tokens} tokens)")
    t_start = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=(10, 180), stream=True)
    except requests.exceptions.RequestException as e:
        yield from fallback(type(e).__name__)
        return
    if resp.status_code != 200:
        resp.close()
        yield from fallback(f"HTTP {resp.status_code}")
        return

    got_text = False
    said_thinking = False
    finish = None
    usage = {}
    try:
        # chunk_size=1 hands each line over as soon as it arrives; larger chunks
        # hold the reply back until the end. A reply is a few KB.
        for raw in resp.iter_lines(chunk_size=1, decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            finish = choices[0].get("finish_reason") or finish
            if delta.get("reasoning_content") and not said_thinking:
                said_thinking = True
                yield ("thinking", None)
            piece = delta.get("content")
            if piece:
                if not got_text:
                    print(f"[llm] first words after {time.time() - t_start:.1f}s")
                got_text = True
                yield ("delta", piece)
    except requests.exceptions.RequestException as e:
        if not got_text:
            yield from fallback(type(e).__name__)
            return
        raise HTTPException(502, f"The connection to {label} dropped mid-reply. Try again.")
    finally:
        resp.close()

    if not got_text:
        yield from fallback(f"empty stream, finish={finish}")
        return
    print(f"[llm] ← {purpose}: {time.time() - t_start:.1f}s, finish={finish or '?'}"
          + (f", output {usage.get('completion_tokens')} tokens" if usage else ""))
    if finish == "length":
        print("[truncated] finish_reason=length — the reply was cut by max_tokens.")
