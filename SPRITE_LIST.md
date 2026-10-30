# Sprite list — upload your images with these exact filenames

**Easiest way: the Sprite Manager.** Open it from the chat (🎭 Sprites) or
the launcher ([ SPRITES ]), or go to `http://127.0.0.1:8000/sprites`. It lists
every emotion with its image, lets you replace an image, add variants (the
app picks one of them at random each time), remove images (they go to
`images/sprites/_trash/`) and create new emotions — the model learns a new
emotion from the description you give it. GIF, PNG, WebP or JPEG.

The rest of this file is the manual way: drop files into the folders with
these names (variants: `ame_happy--v2.gif`, `ame_happy--v3.png`…).

The app detects Ame's emotional state at the end of every response and
shows a matching sprite automatically. Just drop your images into the
right folder with the exact filename below — no code changes needed,
no restart needed, just refresh the browser.

If a file doesn't exist yet, the app simply shows no sprite that turn
(nothing breaks).

## images/sprites/ame/  (default mode, off-stream)

| Filename              | Trigger / when it's used                                    |
|------------------------|---------------------------------------------------------------|
| ame_idle.gif           | Neutral, everyday conversation                                |
| ame_happy.gif          | Genuinely pleased, warm, light-hearted moment                 |
| ame_affectionate.gif   | Tender/caring moment, not sexual (hugs, soft words)            |
| ame_intimate.gif       | A sexual/intimate scene is starting or happening               |
| ame_sad.gif            | Sad, hurt, down                                                |
| ame_devastated.gif     | Severe emotional collapse, crying hard, breakdown              |
| ame_crying.gif         | Crying (softer than devastated)                                |
| ame_surprised.gif      | Caught off guard, shocked, startled                            |
| ame_stressed.gif       | Anxious, overwhelmed, on edge                                  |
| ame_angry.gif          | Annoyed, irritated, sharp                                      |
| ame_sleepy.gif         | Tired, just woke up, low energy                                |
| ame_thinking.gif       | Reflective, considering something, quiet processing            |
| ame_selfie.gif         | Playful/casual moment, showing herself off a little            |
| ame_intimate1.gif      | Random visual variant of ame_intimate — backend picks this at random, no trigger needed beyond having the file |
| ame_out_happy.gif      | Happy/light mood while genuinely outdoors (street, mall, anywhere away from the apartment) |
| ame_eating.gif         | Actively eating / sharing a meal, any mood — action tag, not an emotion tag |
| ame_nod.gif            | Wordless affirmative nod (agreeing/confirming without a spoken line) |
| ame_texting.gif        | Neutral/casual moment using her phone |
| ame_texting_mad.gif    | On her phone AND visibly annoyed/irritated (use instead of ame_texting) |
| ame_tv.gif             | Relaxed downtime, watching something, low-key |

## images/sprites/kangel/  (ONLY while explicitly in stream mode)

| Filename                  | Trigger / when it's used                                  |
|-----------------------------|---------------------------------------------------------------|
| kangel_idle.gif             | Neutral streaming moment                                     |
| kangel_happy.gif            | Bright, cheerful on-stream energy                             |
| kangel_waving.gif           | Greeting viewers, saying hi/goodbye on stream                 |
| kangel_flirty.gif           | Flirting with viewers/chat                                    |
| kangel_explaining.gif       | Explaining something to chat, hands up gesturing              |
| kangel_praying.gif          | Thanking viewers, "bless" moment, grateful gesture             |
| kangel_crying.gif           | Crying on stream (still performing, more contained)            |
| kangel_scared.gif           | Startled or unsettled on stream                                |
| kangel_angry.gif            | Annoyed/sharp with hecklers, still in KAngel mode              |
| kangel_breakdown.gif        | Full on-stream mental break/crisis moment                      |
| kangel_sexy_stream.gif      | Explicitly flirtatious/sexy-stream content                     |

Total: 20 Ame sprites + 11 KAngel sprites = 31 files.

You can start with just a handful (e.g. ame_idle, ame_happy, ame_sad,
kangel_idle) and add the rest over time — nothing requires the full set
to work.
