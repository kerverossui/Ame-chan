# Configuration

Edit the values below. The app reads this file every time it starts.

## AI engine (LLM provider) — optional

You don't need to touch this anymore: the first time you open the app in
the browser, the welcome wizard lets you pick the engine (Grok, Qwen,
DeepSeek or GPT), the model, and paste your API key right in the
interface — that's enough to get started, and you can change it later
from Settings without touching this file or restarting the app.

This is only an optional fallback, in case you prefer to have it
pre-filled before opening the browser for the first time (for example,
for automated installs).

`LLM_PROVIDER` decides which of the four keys/models below is used by
default: `grok`, `qwen`, `deepseek` or `gpt`.

Image recognition (verified July/August 2026):
- **Grok**: only `grok-4.3-latest` recognizes images; the rest of the Grok
  family doesn't.
- **Qwen**: only models with `-vl-` in the name (`qwen-vl-max`,
  `qwen-vl-plus`, `qwen3-vl-plus`, `qwen3-vl-flash`, etc.) recognize
  images; the regular text models (`qwen-plus`, `qwen-turbo`, `qwen-max`)
  don't.
- **DeepSeek**: its public API is text-only — it doesn't recognize
  images, whatever the model.
- **GPT**: all three GPT-5.6 models (`sol`/`terra`/`luna`) recognize
  images out of the box, no exceptions.

Available text models (check which ones your credits cover in each
provider's dashboard — exact names may vary):

- Grok: `grok-4.3-latest`, `grok-4.5`, `grok-4`, `grok-4-fast`,
  `grok-3`, `grok-3-mini`
- Qwen: `qwen-max`, `qwen-plus`, `qwen-turbo`, `qwen-vl-max`,
  `qwen-vl-plus`, `qwen3-vl-plus`, `qwen3-vl-flash`
- DeepSeek: `deepseek-v4-pro`, `deepseek-v4-flash`
- GPT: `gpt-5.6-sol` (most capable), `gpt-5.6-terra` (balanced),
  `gpt-5.6-luna` (fastest/cheapest)

```
LLM_PROVIDER = grok

GROK_API_KEY = your-key-here
GROK_MODEL = grok-4.3-latest

QWEN_API_KEY = your-key-here
QWEN_MODEL = qwen-plus

DEEPSEEK_API_KEY = your-key-here
DEEPSEEK_MODEL = deepseek-v4-pro

GPT_API_KEY = your-key-here
GPT_MODEL = gpt-5.6-sol
```

## Reasoning effort

The model's reasoning effort level (affects quality and cost). It's a
Grok and GPT parameter — if the active engine is Qwen or DeepSeek this
value is ignored (those two use a simple on/off "extended thinking"
switch, set in the welcome wizard or in Settings, not here).

Options for Grok: `none`, `low`, `medium`, `high`.
Options for GPT (more levels): `none`, `low`, `medium`, `high`, `xhigh`,
`max`.

`none` is the most cost-effective and is enough for normal conversation:
Ame's quality comes from character.md, not from extra reasoning. Raise
it occasionally if you're doing something that warrants it.

```
REASONING_EFFORT = none
```

## Opening scene

This text is sent as Ame's very first message the first time you open
the chat (when there's no history yet), in the language chosen in the
welcome wizard. The sections below hold one version per language; a
language without its own section uses this English one. Leave a block
empty between the ``` ``` if you don't want a fixed opening scene.

```
The apartment door is heavier than she expected it to be — a strange
choice for someone who claims to want company. When it finally swings
open, Ame is standing there in an oversized hoodie, hair still damp,
looking at you like she's already regretting the ad she never actually
posted.

"So you're the one from Dinder." She steps back to let you in, not quite
meeting your eyes. "No strings, no questions — that's still the deal,
right? Tokyo's smaller than it looks online. Don't make this weird."
```

## Opening scene (es)

```
La puerta del departamento pesa más de lo que ella esperaba, una elección
rara para alguien que dice querer compañía. Cuando por fin se abre, ahí
está Ame, con una sudadera que le queda enorme y el pelo todavía húmedo,
mirándote como si ya se arrepintiera del anuncio que nunca llegó a
publicar.

—Así que tú eres quien me escribió por Dinder. —Se hace a un lado para
dejarte pasar, sin mirarte del todo a los ojos—. Nada de compromisos ni
preguntas, ese sigue siendo el trato, ¿no? Tokio es más chico de lo que
parece en internet. No lo hagas raro.
```

## Opening scene (es-ar)

```
La puerta del departamento pesa más de lo que ella esperaba, una elección
rara para alguien que dice querer compañía. Cuando por fin se abre, ahí
está Ame, con un buzo que le queda enorme y el pelo todavía húmedo,
mirándote como si ya se arrepintiera del anuncio que nunca llegó a
publicar.

—Así que vos sos quien me escribió por Dinder. —Se corre a un costado
para dejarte pasar, sin mirarte del todo a los ojos—. Nada de compromisos
ni preguntas, ese sigue siendo el trato, ¿no? Tokio es más chico de lo
que parece en internet. No lo hagas raro.
```

## Opening scene (it)

```
La porta dell'appartamento è più pesante di quanto si aspettasse, una
scelta strana per una che dice di volere compagnia. Quando finalmente si
apre, Ame è lì, con una felpa enorme e i capelli ancora umidi, e ti
guarda come se si stesse già pentendo dell'annuncio che in realtà non ha
mai pubblicato.

—Quindi sei tu, quello di Dinder. —Si fa da parte per farti entrare,
senza guardarti davvero negli occhi—. Niente impegni, niente domande:
l'accordo è sempre quello, giusto? Tokyo è più piccola di quanto sembri
online. Non rendere le cose strane.
```

## Opening scene (ja)

```
アパートのドアは、彼女が思っていたより重い。誰かと一緒にいたいと言う人にしては、妙な選び方だ。ようやくドアが開くと、ぶかぶかのパーカーを着て、髪もまだ濡れたままのアメが立っている。結局投稿すらしなかった募集を、もう後悔しているような目であなたを見ている。

「……で、あんたがDinderの人ね」一歩下がってあなたを中に通すけれど、目はちゃんと合わせない。「しがらみなし、詮索なし。その約束、まだ有効だよね？　東京ってネットで見るより狭いから。変な空気にしないでよ」
```

## Opening scene (ko)

```
아파트 문은 그녀가 생각했던 것보다 무겁다. 누군가와 함께 있고 싶다는 사람치고는 이상한 선택이다. 마침내 문이 열리자, 헐렁한 후드티를 입고 머리도 아직 덜 마른 아메가 서 있다. 정작 올리지도 않은 구인 글을 벌써 후회하는 듯한 눈빛으로 당신을 보고 있다.

「그러니까, 네가 Dinder에서 연락한 사람이구나.」 그녀가 한 발 물러서 당신을 들여보내지만, 눈은 제대로 마주치지 않는다. 「서로 얽매이지 않고, 캐묻지 않기. 그 약속 아직 유효한 거지? 도쿄는 인터넷에서 보는 것보다 좁아. 이상하게 만들지 마.」
```

## Opening scene (zh)

```
公寓的门比她想象的还要重——对一个声称想要有人陪的人来说，真是个奇怪的选择。门终于打开时，糖糖就站在那里，穿着一件宽大的连帽衫，头发还没干，看你的眼神像是已经在后悔那条她其实从没发出去的招募帖。

「所以，你就是在Dinder上找我的那个人吧。」她往后退了一步让你进来，却没怎么正眼看你。「不牵扯、不过问——这个约定还算数吧？东京比网上看起来小多了。别搞得怪怪的。」
```

## Ongoing situation (always included)

Unlike the opening scene above (which only appears once, as the first
message), this block is sent to Ame on EVERY turn — use it for facts
about the current setup that should never drift or get forgotten as the
conversation goes on.

```
{{user_name}} and Ame live together in the same Tokyo apartment. They
matched on Dinder; Ame offered the arrangement "no strings, no
questions," bought {{user_name}}'s ticket to Tokyo, and {{pronoun_subj}}
moved in. Living together is a settled, ongoing fact, not a visit:
everyday domestic moments (being in the apartment, chores, talking in the
morning) are normal and not worth narrating as surprising.

This says nothing about what they are to each other emotionally — that
is free to develop, stay platonic, shift or deepen as the conversation
actually unfolds. Being roommates is not a ceiling on intimacy, and not a
reason to reset warmth that has already grown between them.
```

## Your profile (optional)

You don't need to touch this anymore: the first time you open the app in
the browser, a welcome wizard asks for language, name, gender and bio
right in the interface, and that's enough to get started.

These values are only an optional fallback (in case you prefer to have
them pre-filled before opening the browser for the first time, or for
automated install scripts) — they can be left empty.

```
USER_NAME =
USER_BIO =
```

## Notes

- If you leave any API key empty here, the app falls back to the matching
  environment variable (`GROK_API_KEY`, `QWEN_API_KEY`,
  `DEEPSEEK_API_KEY`, `GPT_API_KEY`) — in case you prefer not to keep them
  as plain text in a file.
- Save this file before starting the app (`start.bat` / `start.sh`).
- Don't upload this file to any public repository once it holds a real
  key.
