# Ame Companion App

A self-hosted AI companion chat. It runs on your own computer, talks to the
LLM provider you choose (Grok, Qwen, DeepSeek or GPT, with your own API key)
and keeps everything (chat, memory, settings) in local files. No cloud
backend, no account, no telemetry.

The character is defined in plain-text files (`character.md`, `canon.md`)
that you can read and edit; none of her personality lives in code.

It is built for **one person**, not as a multi-user service.

---

## 1. Requirements

- Python 3.10+ (on Windows, `start.bat` installs it for you if it's missing)
- About 1 GB of disk space, mostly for the local embedding model and
  CPU-only PyTorch
- An API key for at least one of: [xAI/Grok](https://x.ai),
  [Alibaba/Qwen](https://dashscope.aliyun.com),
  [DeepSeek](https://platform.deepseek.com) or
  [OpenAI/GPT](https://platform.openai.com). Providers can be added or
  switched later from Settings, without restarting.

---

## 2. Install and run

**Windows:** double-click `start.bat`.
**macOS / Linux:** run `./start.sh`.

That opens the launcher (`ame_launcher.py`, plain Tkinter, nothing to
install first):

1. **Boot screen.** While it types, it checks Python, the virtual
   environment, the dependencies and the memory model.
2. **Setup**, only if something is missing: creates `venv/`, installs the
   dependencies and downloads the embedding model, with a progress bar per
   step. The first time is about 1 GB of downloads. If a step fails you get
   **RETRY** and **VIEW LOG** (`logs/install.log`).
3. **LAUNCH** starts the server and opens **http://127.0.0.1:8000** in your
   browser. **RESTART** reloads it, **VIEW LOG** opens `logs/server.log`,
   **SPRITES** opens the sprite manager. Closing the window stops Ame.

The launcher is in English, Spanish or Italian (switch in the bottom-right
corner; the choice is saved in `.installer_lang`).

On Windows, if `start.bat` can't find Python 3.10+, it installs Python 3.12
for your user only (no admin rights; winget, or the official installer) and
carries on. `start.bat console` runs the launcher with a console window, for
troubleshooting. On a Linux without Tkinter, `start.sh` falls back to
`install.py`, which also works on its own.

The first time you open the app, a welcome wizard asks for your language,
your name and bio, and the AI engine with its API key.

**Embedding model.** The first start downloads
`paraphrase-multilingual-MiniLM-L12-v2` (about 470 MB), used for memory
search. The server waits for it before accepting messages; after that it's
cached and startup is instant.

### Remote access (phone, another PC)

By default the server only listens on this computer (`127.0.0.1`), because
the app holds your API keys and every message spends your credit.

Turn on **REMOTE ACCESS** in the launcher and it also listens on your
network. The launcher shows the address (e.g. `http://192.168.1.8:8000`) and
an **access key**. On the other device, open the address and type the key
once (a cookie keeps it logged in for a year), or scan the QR code, which
carries the key in the link. API keys are never sent to other devices; they
show masked in Settings. The first time, Windows Firewall asks about Python:
allow it on **private** networks.

Outside your home network, use a tunnel such as Tailscale, or run Ame on a
server.

### Running on a server

```
export AME_HOST=0.0.0.0
export AME_ACCESS_KEY="a-long-random-key"
python install.py      # creates venv/, installs everything, starts the server
```

Afterwards `./start.sh` (with the same variables) or
`venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000` starts it directly;
`AME_PORT` changes the port. Without `AME_ACCESS_KEY`, remote requests get a
page saying remote access is off. Requests that come through a reverse proxy
are always treated as remote; `AME_TRUST_LOCALHOST=0` requires the key for
everyone. Over the internet, put HTTPS in front.

---

## 3. Using the app

- **Replies stream in** as they're written. When a reply is finished, the
  app checks its language and polishes a few lines, then swaps in the final
  text.
- **▶ Continue** extends Ame's last reply once; the reply and its
  continuation each get a sprite, and the two take turns in a loop.
- **↻ Regenerate** writes a new reply to your last message. Earlier versions
  are kept (up to 10) and **‹ 2 / 3 ›** switches between them; the version on
  screen is the one the next turn and the memory see. If a regeneration
  fails, the previous reply is put back.
- **🖥️ Console** shows the server's live log: every AI call (purpose, model,
  time, tokens, cache), the turn's dice, memory updates and errors. The same
  lines go to `logs/server.log`, with API keys hidden.
- **🌐** changes the language. Before the first message, the opening scene
  switches with it.
- **Pins** are facts she always keeps in mind. **Recalibrate** stops sending
  the recent conversation word for word (nothing is deleted) for when the
  chat has drifted. **Re-summarize** runs a summary pass now.
- **🎭 Sprites** opens the sprite manager (section 7).

DeepSeek's "thinking" mode is off by default because it makes replies much
slower. For even faster replies, pick `deepseek-v4-flash` as the model.

---

## 4. How a turn works

`prompting.build_messages()` builds the prompt in this order:

1. A cached system prompt: `character.md`, the always-on canon, the
   relationship stage (WARY → THAWING → OPEN → DEVOTED, from the number of
   turns or set by hand in Settings), the ongoing situation and your profile.
2. The recent conversation, word for word (the last 12 messages).
3. A final user turn with this turn's `<context>` (memories, pins,
   summaries, mode), `<direction>` (short notes from the dice) and your
   message. Per-turn notes go here because some providers move every system
   message to the top of the prompt.

The reply then goes through `enforcers.py`:

- **Language and dialect.** A regex looks for words from another language,
  or voseo in a tuteo dialect; only if it finds some is the model asked to
  rewrite.
- **Line editor** (Settings → "Polish her lines", on by default). The main
  model reads the reply as an editor and returns a few exact edits: spoken
  lines nobody would say out loud, narration that reads machine-written, a
  job title used as a name, too many lines starting with "You". When she
  talks too much it may cut one throwaway line (never on stream). Every edit
  is validated (single exact match, similar length, never the « » inner
  voice or the sprite tag), so a bad answer changes nothing.
- **Inner voice.** If the « » line is missing, a cheap call writes one for
  the scene; if there are several, one is kept.

Texture (sounds, swearing, whimpers, exclamations) only comes from the
per-turn notes; nothing is inserted afterwards.

`sprites.py` then picks the sprite from the tag the model wrote, or from a
small classifier call if it wrote none, and everything is saved.

The **dice** (`dice.py`, `reaction_families.py`) decide turn by turn which
expressive beats are due, with weighted chances and cooldowns, so the same
moment doesn't play out the same way every time.

**KAngel.** Going live is decided in code. When your message starts the
stream ("she starts the stream", "goes live"…) or ends it ("ends the
stream"…), the prompt says so and the sprite is held to the matching family:
a tag from the wrong family is mapped to its counterpart
(ame_happy → kangel_happy).

---

## 5. Memory

| Layer | Where | What it holds | Who writes it | When the model sees it |
|---|---|---|---|---|
| **Core** | `character.md` | Who Ame is: personality, voice, rules | You | Always (cached) |
| **Canon** | `canon.md` | Her official past: game + anime eps 1–12 (ep 13 doesn't exist; P-chan is a hallucination shaped like the Pien plush) | You, once | `## ALWAYS` every turn; each entry when that person is named |
| **Living memory** | `memories/*.md` | What happens in this story: one file per person + `us.md` (Ame and you) | The app (you can correct it) | `us.md` always; the others when that person is named |
| **Summaries** | `summaries` table | Condensed older stretches | The app, in the background | The latest ones, every turn |
| **History** | `messages` table | Every message, with an embedding | The app | The last 12 verbatim, plus semantic search |

**Living memory.** Every 8 messages, in the background, a cheap model reads
what's new and updates the files: adds a fact, replaces one that changed,
removes one that's no longer true, or creates a file for a new person. A
file that grows too long condenses itself. To correct a memory, edit the
file and save; the app re-reads it.

**Search.** Every message gets an embedding from the local model so it can
be found by meaning. Long messages are also indexed in overlapping chunks.
Embeddings are computed in the background after you've seen the reply.

If the model refuses to summarize a stretch, a neutral retry and then a
minimal summary are used, so memory never gets stuck.

**Moving a story to another PC:** copy `ame.db` and `memories/` together.

### Tools

Run these from the app folder with Ame stopped:

```
python tools/seed_memory.py --from 1 --dry-run
```
Builds living memory from history that existed before it (for example an
`ame.db` from an older version). `--dry-run` only counts the calls it would
make.

```
python tools/reembed_all.py
```
Recomputes every embedding. Only needed if you change the embedding model in
`memory.py`.

---

## 6. Configuration

Everything is editable without touching Python:

- **`character.md`**: personality, voice, rules, sprite tags,
  relationship stages. Reloaded automatically when it changes.
- **`canon.md`**: her official past, entry by entry.
- **`config.md`**: the opening scene (one section per language: English,
  Spanish with tú, Rioplatense Spanish with vos, Italian, Japanese, Korean,
  Chinese; a language without its own section gets the English one) and the
  ongoing situation.
- **Settings (in the app)**: provider, model, API key, base URL, language,
  thinking/reasoning mode, summary frequency and more. Applied on your next
  message.

| Provider | Image support |
|---|---|
| Grok (xAI) | `grok-4.3-latest` only |
| Qwen (Alibaba) | `-vl-` models only (e.g. `qwen-vl-max`) |
| DeepSeek | No (text-only API) |
| GPT (OpenAI) | Yes |

Any provider can point to a custom OpenAI-compatible base URL (a gateway or
router) instead of its official endpoint.

---

## 7. Sprites

`/sprites` (🎭 Sprites in the chat, **SPRITES** in the launcher) lists every
emotion, Ame's and KAngel's, with its images, how many replies used it and
whether it's missing. From there you can replace an image, add variants
(one is picked at random), remove images (moved to `images/sprites/_trash/`)
and create new emotions. Custom emotions are stored in
`images/sprites/custom_tags.json` and their descriptions go into the prompt
so the model can use them. Files are checked by content (GIF, PNG, WebP,
JPEG, up to 15 MB).

`SPRITE_LIST.md` lists the 31 built-in emotions (20 for Ame, 11 for KAngel)
and the file names, for adding images by hand.

---

## 8. API

Plain JSON over HTTP (interactive docs at `/docs` while the server runs).

| Endpoint | Method | Purpose |
|---|---|---|
| `/chat_stream` | POST | Send a message; the reply streams as server-sent events |
| `/chat` | POST | Send a message; the whole reply at once |
| `/continue` | POST | Extend Ame's last reply (once) |
| `/regenerate_stream` | POST | Write a new version of the last reply |
| `/select_version` | POST | Show another version of the last reply |
| `/opening` | GET | The opening scene, on a new chat |
| `/history` | GET | Past messages |
| `/upload_image` | POST | Attach an image to your next message (20 MB max) |
| `/pins`, `/pins/{id}` | GET / POST / PUT / DELETE | Pinned notes |
| `/summaries` | GET | Summaries |
| `/recalibrate` | POST | Stop sending the recent conversation word for word |
| `/resummarize` | POST | Run a summary pass now |
| `/profile` | GET / POST | Your profile |
| `/onboarding_status`, `/onboarding` | GET / POST | Welcome wizard |
| `/settings` | GET / POST | Settings, applied immediately |
| `/console` | GET | Live server log |
| `/cache_stats` | GET | Prompt-cache stats of the last call |
| `/sprite_status` | GET | Which sprite images are present or missing |
| `/sprites_api/*` | GET / POST | Sprite manager: catalog, upload, delete, emotions |

---

## 9. Project layout

```
ame_launcher.py         Launcher and installer window (Tkinter)
installer_i18n.py       Launcher texts (English, Spanish, Italian)
install.py              Installs the dependencies; also works from the console
start.sh / start.bat    Open the launcher
main.py                 FastAPI app: every HTTP endpoint and the turn pipeline
prompting.py            Builds the prompt each turn; relationship stage
enforcers.py            Post-processing: language, line editor, inner voice,
                        repetition check
dice.py                 Weighted dice for the expressive beats of each turn
reaction_families.py    Reactions to recognizable kinds of message
sprites.py              Sprite library, tags and fallback classification
memory.py               Embeddings, chunked search, summaries
canon.py                Canon entries (canon.md), injected by name
living_memory.py        Per-person living memory (memories/)
database.py             All SQLite access
llm.py                  Calls to the providers, with retries and clear errors
app_config.py           Loads config.md and character.md; constants
remote_access.py        Access key for other devices
console_log.py          The in-app console
qrcodegen.py            QR encoder (Project Nayuki, MIT)
character.md            Who Ame is
canon.md                Her official past
config.md               Opening scene and ongoing situation
static/                 The chat page and the sprite manager
images/                 Avatars and sprites
memories/               Living memory files (created by the app)
tools/                  Maintenance scripts (section 5)
tests/                  Tests (run_tests.bat / run_tests.sh)
logs/                   install.log, server.log
ame.db                  Your data, created on first run
```

---

## 10. Data and privacy

Everything lives on your computer: `ame.db` and `memories/`. Nothing is sent
anywhere except your messages to the provider you configured, with your own
key. No analytics, no accounts, no sync.

To back up, copy `ame.db` and `memories/`. To start over, delete both; they
are recreated empty on the next start.

## Credit and legal

Made by Kerveros ([@KerverosJim](https://x.com/KerverosJim)). Unofficial,
non-commercial fan work and parody; see `LEGAL.md` (also **LEGAL** in the
launcher) for the disclaimer.
