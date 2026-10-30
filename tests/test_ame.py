"""
Ame App tests:

    python -m unittest discover tests        (from the app folder)
    or double-click run_tests.bat

The model is always mocked and the tests run on a temporary database, so
they never call an API or touch your chat.

Much of the test data is Spanish on purpose: it exercises the Spanish
dialect checks and the dialogue-dash formatter.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
INDEX = next((p for p in (os.path.join(APP, "static", "index.html"),
                          os.path.join(APP, "index.html")) if os.path.exists(p)), None)

import database  # noqa: E402
import enforcers  # noqa: E402
import prompting  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="ame_tests_")
database.DB_PATH = os.path.join(_TMP, "ame.db")
database.init_db()


def _read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Dialect(unittest.TestCase):
    def test_detects_real_voseo(self):
        for phrase in ("Benditos vos y tus funciones cognitivas.",
                       "Hasta para lo boludo.", "Vos me lo decís a mí."):
            self.assertTrue(enforcers._detect_dialect_leak(phrase, "es-pe"), phrase)
        self.assertFalse(enforcers._detect_dialect_leak("Tú me lo dices a mí.", "es-pe"))
        self.assertFalse(enforcers._detect_dialect_leak("Vos sabés.", "es-ar"))

    def test_empty_rewrite_leaves_trace(self):
        with mock.patch.object(enforcers, "_rewrite_in_language", return_value=""), \
             mock.patch("builtins.print") as p:
            out = enforcers.enforce_language("Sin vos no puedo.", "es-pe")
        self.assertEqual(out, "Sin vos no puedo.")
        self.assertTrue(any("EMPTY" in str(c) for c in p.call_args_list))

    def test_final_check_catches_text_generated_later(self):
        """Voseo in text added after enforce_language (the inner voice) is
        still corrected by the final dialect check."""
        import main
        dice = {"language": "es-pe"}
        generated = "Ella sonríe.\n\n«Sin vos no sé qué haría.»\n[[sprite: ame_happy]]"
        corrected = "Ella sonríe.\n\n«Sin ti no sé qué haría.»\n[[sprite: ame_happy]]"
        with mock.patch.object(main.db, "get_settings",
                               return_value={"polish_speech": 0}), \
             mock.patch.object(enforcers, "enforce_inner_voice", return_value=generated), \
             mock.patch.object(enforcers, "_rewrite_in_language",
                               return_value=corrected) as rw:
            out = main._enforcer_chain("Ella sonríe.\n[[sprite: ame_happy]]", dice)
        self.assertEqual(out, corrected)
        self.assertEqual(rw.call_count, 1)

    def test_polish_off_changes_nothing(self):
        """With the line editor off, a reply that already has its inner
        voice goes out exactly as written."""
        import main
        reply = 'She smiles. "Okay."\n\n«he stayed»\n[[sprite: ame_happy]]'
        with mock.patch.object(main.db, "get_settings", return_value={"polish_speech": 0}), \
             mock.patch.object(enforcers, "call_llm") as cl:
            out = main._enforcer_chain(reply, {"language": "en"})
        self.assertEqual(out, reply)
        cl.assert_not_called()

    def test_inner_voice_always_guaranteed(self):
        """Every reply has an inner-voice line; a reply without one gets one
        written for the scene, even with the line editor off."""
        import main
        dice = {"language": "en"}
        with mock.patch.object(main.db, "get_settings",
                               return_value={"polish_speech": 0}), \
             mock.patch.object(enforcers, "_generate_inner_voice_line", return_value="«stay»"):
            out = main._enforcer_chain('She smiles.\n[[sprite: ame_happy]]', dice)
        self.assertIn("«stay»", out)
        self.assertTrue(out.rstrip().endswith("[[sprite: ame_happy]]"))

    def test_no_texture_is_inserted(self):
        """Post-processing only fixes lines; it never adds sounds or
        interjections of its own."""
        import main
        dice = {"language": "en", "user_name": "Sam", "job_terms": "nurse",
                "job_in_message": False}
        reply = 'She smiles. "Okay."\n\n«he stayed»\n[[sprite: ame_happy]]'
        with mock.patch.object(main.db, "get_settings",
                               return_value={"polish_speech": 1}), \
             mock.patch.object(enforcers, "humanize_reply", side_effect=lambda t, *a, **k: t) as hz, \
             mock.patch.object(enforcers, "enforce_inner_voice", side_effect=lambda t, *a, **k: t):
            out = main._enforcer_chain(reply, dice)
        self.assertEqual(out, reply)
        self.assertNotIn("wants", hz.call_args.kwargs)
        self.assertEqual(hz.call_args.kwargs["user_name"], "Sam")

    def test_editor_uses_the_main_model(self):
        with mock.patch.object(enforcers, "call_llm", return_value='{"edits": []}') as cl, \
             mock.patch.object(enforcers.db, "get_settings", return_value={"llm_provider": "deepseek"}):
            enforcers.humanize_reply('She smiles. "Okay."', "en")
        self.assertIsNone(cl.call_args.kwargs["model"])

    def test_fresh_install_defaults(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        with mock.patch.object(database, "DB_PATH", path):
            database.init_db()
            s = database.get_settings()
            self.assertEqual(s["polish_speech"], 1)
            self.assertEqual(s["deepseek_thinking"], 0)
            database.set_settings(deepseek_thinking=1)
            database.init_db()                      # restart keeps the choice
            self.assertEqual(database.get_settings()["deepseek_thinking"], 1)

    def test_deepseek_temperature_not_raised(self):
        import main
        self.assertLessEqual(main.CHAT_TEMPERATURE.get("deepseek", 1.0), 1.0)


class Repetition(unittest.TestCase):
    BASE = ("She sets the cup down and looks at him for a long moment, the steam "
            "curling between them. \"You came back,\" she says, quieter than she "
            "meant to. The room hums with the fridge and the rain on the window, and "
            "for once she doesn't reach for her phone. \"I thought you'd be mad.\" "
            "She pulls her knees up onto the chair. \"I would be, if I were you.\"\n\n«stay»")

    def test_near_copy_is_repetition(self):
        again = self.BASE.replace("long moment", "long while").replace("quieter", "softer")
        self.assertTrue(enforcers.is_repetition(again, self.BASE))

    def test_different_reply_is_not(self):
        other = ("The rain gets louder. She laughs at something on her screen and turns "
                 "it toward him, a cat stuck in a cardboard box, and for a minute neither "
                 "of them talks about anything that matters. \"Look at his face.\" She "
                 "scrolls to the next one. \"Okay, this one's worse.\" The tea goes cold "
                 "on the desk while they argue about which cat would win a fight.\n\n«nice»")
        self.assertFalse(enforcers.is_repetition(other, self.BASE))

    def test_short_replies_are_never_repetition(self):
        self.assertFalse(enforcers.is_repetition("Okay.", "Okay."))

    def test_retries_once(self):
        import main
        previous = "x" * 400
        with mock.patch.object(main, "call_llm", side_effect=[previous, "respuesta nueva " * 30]) as cl:
            out = main._generate_without_repeating([{"role": "user", "content": "hola"}], previous)
        self.assertEqual(cl.call_count, 2)
        self.assertTrue(out.startswith("respuesta nueva"))
        self.assertIn(enforcers.REPETITION_INSTRUCTION, cl.call_args_list[1][0][0][-1]["content"])


class Summaries(unittest.TestCase):
    """A model that refuses to summarize must not block the memory."""
    ROWS_FIXTURE = [{"id": 10, "role": "user", "content": "hola"},
             {"id": 11, "role": "assistant", "content": "—Hola, amor."}]
    REFUSAL = "I can't provide a summary of this content."

    def test_refusal_then_neutral(self):
        import memory
        with mock.patch.object(memory, "call_llm",
                               side_effect=[self.REFUSAL, "Ame y él se saludan con cariño."]):
            text, state = memory.summarize_rows(self.ROWS_FIXTURE, "es-pe")
        self.assertEqual(state, "neutral")
        self.assertIn("cariño", text)

    def test_two_refusals_store_minimal_summary(self):
        import memory
        with mock.patch.object(memory, "call_llm", return_value=self.REFUSAL):
            text, state = memory.summarize_rows(self.ROWS_FIXTURE, "es-pe")
        self.assertEqual(state, "minimal")
        self.assertIn("10–11", text)
        self.assertFalse(memory._looks_like_refusal(text))

    def test_network_error_does_not_advance(self):
        import memory
        with mock.patch.object(memory, "call_llm", side_effect=RuntimeError("timeout")):
            text, state = memory.summarize_rows(self.ROWS_FIXTURE, "es-pe")
        self.assertIsNone(text)
        self.assertEqual(state, "error")


class Canon(unittest.TestCase):
    """Memory from before the app (canon.md): exact entry by name."""

    def setUp(self):
        import canon
        self.canon = canon
        self._orig = canon.CANON_PATH
        self.tmp = os.path.join(_TMP, "canon_test.md")
        with open(self.tmp, "w", encoding="utf-8") as f:
            f.write("# Canon\n<!-- comment -->\n## ALWAYS\nThe four of them.\n\n"
                    "## Nechika\nkeys: nechika, nechica, nechika-sama\n- Supports her siblings.\n\n"
                    "## Kache\n- Nails.\n")
        canon.CANON_PATH = self.tmp
        canon._cache["mtime"] = None

    def tearDown(self):
        self.canon.CANON_PATH = self._orig
        self.canon._cache["mtime"] = None

    def test_entry_by_name_ignoring_accents_and_case(self):
        names = [e["name"] for e in self.canon.active_entries("¿Qué hay de NECHICA?", [])]
        self.assertEqual(names, ["Nechika"])

    def test_no_mention_costs_nothing(self):
        self.assertEqual(self.canon.active_entries("good morning love", ["hi"]), [])
        self.assertEqual(self.canon.entries_block("good morning love", []), "")

    def test_recent_context_and_priority(self):
        names = [e["name"] for e in self.canon.active_entries("and Kache?", ["Nechika isn't answering"])]
        self.assertEqual(names, ["Kache", "Nechika"])

    def test_no_match_inside_another_word(self):
        self.assertEqual(self.canon.active_entries("kacheteo", []), [])

    def test_always_and_comments(self):
        b = self.canon.always_block()
        self.assertIn("The four of them.", b)
        self.assertNotIn("comment", b)

    def test_reload_without_restart(self):
        import time
        self.canon.always_block()
        time.sleep(0.05)
        with open(self.tmp, "a", encoding="utf-8") as f:
            f.write("\n## Michica\n- Gothic.\n")
        os.utime(self.tmp, None)
        self.canon._cache["mtime"] = -1          # force change detection
        self.assertEqual([e["name"] for e in self.canon.active_entries("michica", [])], ["Michica"])

    def test_old_spanish_format_still_works(self):
        with open(self.tmp, "w", encoding="utf-8") as f:
            f.write("## SIEMPRE\nLas cuatro.\n\n## Nechika\nclaves: nechica\n- Hermanos.\n")
        self.canon._cache["mtime"] = -1
        self.assertIn("Las cuatro.", self.canon.always_block())
        self.assertEqual([e["name"] for e in self.canon.active_entries("nechica", [])], ["Nechika"])

    def test_real_canon_if_present(self):
        real = os.path.join(APP, "canon.md")
        if not os.path.exists(real):
            self.skipTest("canon.md not found")
        self.canon.CANON_PATH = real
        self.canon._cache["mtime"] = None
        for n in ("Michica", "Kache", "Purple Lollipop", "Nechika"):
            self.assertIn(n, [e["name"] for e in self.canon.active_entries(n.lower(), [])])


class LivingMemory(unittest.TestCase):
    """Per-person memories that get updated (not a history that is re-read)."""

    def setUp(self):
        import living_memory
        self.m = living_memory
        self._orig = (living_memory.MEMORY_DIR, living_memory.STATE_PATH, living_memory._OLD_DIR)
        self.dir = tempfile.mkdtemp(prefix="memories_", dir=_TMP)
        living_memory.MEMORY_DIR = self.dir
        living_memory.STATE_PATH = os.path.join(self.dir, ".state.json")
        living_memory._OLD_DIR = os.path.join(_TMP, "no_old_dir")
        living_memory._cache["signature"] = None
        with open(os.path.join(self.dir, "michica.md"), "w", encoding="utf-8") as f:
            f.write("# Michica\nkeys: michica, michika\n\n- Drinks rose tea.\n")

    def tearDown(self):
        self.m.MEMORY_DIR, self.m.STATE_PATH, self.m._OLD_DIR = self._orig
        self.m._cache["signature"] = None

    def test_add_replace_delete_create(self):
        log = self.m.apply_operations([
            {"action": "add", "entity": "Michica", "fact": "Always arrives late."},
            {"action": "replace", "entity": "michica", "old": "Drinks rose tea.",
             "new": "Drinks rose and jasmine tea."},
            {"action": "create", "entity": "Yuki", "keys": ["yuki"], "fact": "Kache's neighbor."},
            {"action": "add", "entity": "us", "fact": "They adopted a cat."},
            {"action": "delete", "entity": "Michica", "old": "does not exist"},   # ignored
            {"action": "add", "entity": "Unknown", "fact": "x"},                  # ignored
            "garbage",                                                            # ignored
        ])
        self.assertEqual(len(log), 4)
        e = self.m.entries()
        self.assertEqual(e["michica"]["facts"],
                         ["Drinks rose and jasmine tea.", "Always arrives late."])
        self.assertIn("yuki", e)
        self.assertIn("They adopted a cat.", e["us"]["facts"])

    def test_spanish_operations_still_accepted(self):
        log = self.m.apply_operations([{"accion": "agregar", "entidad": "Michica",
                                        "hecho": "Llega tarde."}])
        self.assertEqual(log, ["+ Michica: Llega tarde."])

    def test_injection_by_name_and_us_always(self):
        self.m.apply_operations([{"action": "add", "entity": "us",
                                  "fact": "They went for a walk in the park."}])
        self.assertIn("park", self.m.always_block())
        self.assertEqual([e["name"] for e in self.m.active_entries("and MICHIKA?", [])], ["Michica"])
        self.assertEqual(self.m.active_entries("good morning", []), [])

    def test_old_folder_is_migrated(self):
        old = tempfile.mkdtemp(prefix="memoria_old_", dir=_TMP)
        with open(os.path.join(old, "nosotros.md"), "w", encoding="utf-8") as f:
            f.write("# Ame y él\nclaves:\n\n- Se comprometieron.\n")
        with open(os.path.join(old, ".estado.json"), "w", encoding="utf-8") as f:
            json.dump({"ultimo_id": 42}, f)
        new = os.path.join(_TMP, "memories_migrated")
        self.m._OLD_DIR, self.m.MEMORY_DIR = old, new
        self.m.STATE_PATH = os.path.join(new, ".state.json")
        self.m._migrate_old_layout()
        self.assertTrue(os.path.exists(os.path.join(new, "us.md")))
        self.assertEqual(self.m._read_state()["cursor_id"], 42)
        self.assertIn("Se comprometieron.", self.m.always_block())

    def test_fresh_install_processes_from_first_message(self):
        # Empty database at startup: cursor 0, so the first message is
        # included.
        with mock.patch.object(self.m, "_max_id", return_value=0):
            self.m.init()
        self.assertEqual(self.m._read_state()["cursor_id"], 0)
        self.m.init()                                  # doesn't overwrite an existing cursor
        self.assertEqual(self.m._read_state()["cursor_id"], 0)

    def test_first_run_spends_no_api(self):
        with mock.patch.object(self.m, "call_llm") as cl:
            self.assertEqual(self.m.consolidate(force=True), [])
        cl.assert_not_called()
        self.assertIn("cursor_id", self.m._read_state())

    def test_consolidates_and_advances(self):
        mid = database.save_message("user", "Michica told me she's moving to Osaka.", None, None, None, None)
        self.m._save_state({"cursor_id": mid - 1})
        resp = json.dumps({"operations": [{"action": "add", "entity": "Michica",
                                           "fact": "Is moving to Osaka."}]})
        with mock.patch.object(self.m, "call_llm", return_value="```json\n" + resp + "\n```"):
            log = self.m.consolidate(force=True)
        self.assertEqual(log, ["+ Michica: Is moving to Osaka."])
        self.assertEqual(self.m._read_state()["cursor_id"], mid)

    def test_invalid_response_does_not_block(self):
        mid = database.save_message("user", "hi", None, None, None, None)
        self.m._save_state({"cursor_id": mid - 1})
        with mock.patch.object(self.m, "call_llm", return_value="I can't help with that."):
            self.m.consolidate(force=True)
        self.assertEqual(self.m._read_state()["cursor_id"], mid)

    def test_network_error_retries_later(self):
        mid = database.save_message("user", "hi again", None, None, None, None)
        self.m._save_state({"cursor_id": mid - 1})
        with mock.patch.object(self.m, "call_llm", side_effect=RuntimeError("timeout")):
            self.m.consolidate(force=True)
        self.assertEqual(self.m._read_state()["cursor_id"], mid - 1)

    def test_waits_to_collect_messages(self):
        mid = database.save_message("user", "one", None, None, None, None)
        self.m._save_state({"cursor_id": mid - 1})
        with mock.patch.object(self.m, "call_llm") as cl:
            self.m.consolidate()             # 1 message < CONSOLIDATE_EVERY
        cl.assert_not_called()


class Chunks(unittest.TestCase):
    def test_cover_whole_text_with_overlap(self):
        import memory
        text = ("Test sentence number %d. " * 300) % tuple(range(300))
        spans = memory._chunk_spans(text)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(text))
        for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
            self.assertLess(a2, b1, "chunks must overlap")
            self.assertGreater(a2, a1)

    def test_search_returns_matching_chunk(self):
        import numpy as np
        import memory
        needle = "funeral in the rain"

        def vec(t):
            v = np.zeros(8, np.float32)
            v[0 if needle in t else 1] = 1
            return v

        class Fake:
            def encode(self, x, normalize_embeddings=True):
                return np.stack([vec(t[:600]) for t in x])
        long_text = "filler with nothing. " * 250 + "She went to the " + needle + ". " + "more filler. " * 50
        with mock.patch.object(memory, "get_embedder", return_value=Fake()), \
             mock.patch.object(memory, "embed", side_effect=lambda t: vec(t[:600])):
            mid = database.save_message("user", long_text, vec(long_text[:600]).tobytes(), None, None, None)
            memory.index_chunks(mid, long_text)
            res = memory.semantic_search(needle, k=1, exclude_recent=0)
        self.assertTrue(res and res[0]["fragment"])
        self.assertIn(needle, res[0]["content"])
        self.assertLess(len(res[0]["content"]), 900)


class PromptShape(unittest.TestCase):
    """The per-turn notes travel inside the final user turn (some providers
    fold every system message into the top system prompt), and the prompt
    talks to the model as a director, not as an audit log."""

    def _build(self, text="good morning", **kw):
        import memory
        with mock.patch.object(memory, "semantic_search", return_value=[]):
            return prompting.build_messages(text, **kw)

    def test_single_system_message_and_final_user_turn(self):
        msgs, dice = self._build()
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual([m for m in msgs[1:] if m["role"] == "system"], [])
        last = msgs[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("<direction>", last["content"])
        self.assertIn("good morning", last["content"])
        self.assertIn("language", dice)

    def test_continue_has_no_fake_user_message(self):
        msgs, _ = self._build("", retrieval_query="x", continuation=True)
        self.assertNotIn("<message", msgs[-1]["content"])
        self.assertIn("Continue her previous reply", msgs[-1]["content"])

    def test_no_audit_language(self):
        for _ in range(15):     # several dice rolls
            msgs, _ = self._build()
            text = msgs[0]["content"] + msgs[-1]["content"]
            for word in ("malformed", "measured in", "ROLLED", "MANDATORY", "BANNED", "%"):
                self.assertNotIn(word, text)

    def test_job_mentions_are_rationed(self):
        terms = prompting._profile_terms({"user_bio": "Night-shift doctor who likes anime"})
        self.assertIn("doctor", terms)
        self.assertNotIn("anime", terms)
        recent = [{"role": "assistant", "content": "Hey, doctor."},
                  {"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "She shifts closer."},
                  {"role": "assistant", "content": "Long day at the hospital?"},
                  {"role": "assistant", "content": "I don't care."}]
        # "she shifts" is not a job mention; "hospital" is.
        self.assertEqual(prompting._recent_mentions(recent, terms, n=3), 1)
        self.assertEqual(prompting._recent_mentions(recent, terms, n=4), 2)
        prof = {"user_bio": "Pediatric doctor"}
        self.assertTrue(prompting.mentions_job("rough night at the hospital", prof))
        self.assertTrue(prompting.mentions_job("long day at work", prof))
        self.assertFalse(prompting.mentions_job("she shifts on the couch", prof, user_side=False))

    def test_you_openers_counted(self):
        recent = [{"role": "assistant", "content":
                   '"You\'re warm." She yawns. "You look stupid. ...You\'re staying."'},
                  {"role": "assistant", "content": '"I\'m not moving. Gross."'}]
        self.assertEqual(prompting._you_openers(recent, n=2), 1.5)

    def test_repeated_phrases_found(self):
        recent = [{"role": "assistant", "content": "A laugh escapes before she can stop it, again."},
                  {"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "She grins at the wall."},
                  {"role": "assistant", "content": "Her breath hitches before she can stop it."}]
        found = prompting._repeated_phrases(recent)
        self.assertTrue(any("before she can stop" in f or "she can stop it" in f for f in found), found)
        self.assertFalse(any("grins" in f for f in found))

    def test_retry_note_goes_into_direction(self):
        msgs, _ = self._build()
        out = prompting.add_turn_note(msgs, "NOTE-XYZ")
        self.assertEqual(len(out), len(msgs))
        self.assertIn("- NOTE-XYZ\n</direction>", out[-1]["content"])
        self.assertNotIn("NOTE-XYZ", msgs[-1]["content"])   # original untouched


class RemoteQr(unittest.TestCase):
    def test_login_link_encodes(self):
        from qrcodegen import QrCode
        qr = QrCode.encode_text("http://192.168.1.8:8000/?key=abcdefghij", QrCode.Ecc.MEDIUM)
        self.assertGreaterEqual(qr.get_size(), 21)


class NaturalWording(unittest.TestCase):
    def test_cutoff_dashes_become_trail_offs_in_english_speech(self):
        t = 'She looks away — slowly. "I— you can\'t just— stop."'
        self.assertEqual(enforcers.soften_cutoffs(t, "en"),
                         'She looks away — slowly. "I... you can\'t just... stop."')
        es = '—No sé —baja la mirada—. Olvídalo.'
        self.assertEqual(enforcers.soften_cutoffs(es, "es-pe"), es)

    def test_narrator_commentary_can_be_cut(self):
        r = 'She sits up. "Okay." It isn\'t a no. She smiles at him.\n\n«hm»'
        self.assertTrue(enforcers._validate_deletion(r, "It isn't a no."))
        self.assertFalse(enforcers._validate_deletion('"Hi," she says. He sits.', "she says."))

    def test_garbled_inner_voice_fixable_from_inside_only(self):
        t = 'A. «people like me. likes me» B.'
        self.assertTrue(enforcers._validate_edit(t, "likes me", "he likes me"))
        self.assertFalse(enforcers._validate_edit(t, "«people like me. likes me»", "x"))

    def test_buried_inner_voice_gets_its_own_paragraph(self):
        p = ("She talks " + "and talks " * 25 + "«ugh.» " + '"Fine," she says.')
        out = enforcers.tidy_inner_voice(p)
        self.assertIn("\n\n«ugh.»\n\n", out)

    def test_habits_counted_outside_quotes(self):
        recent = [{"role": "assistant", "content":
                   'Her voice comes out small. "It comes out wrong," she says. Her whole body shakes.'}]
        self.assertEqual(prompting._habit_count(recent), 2)   # the quoted line doesn't count


class ContinueRepeats(unittest.TestCase):
    PREV = ('"Maybe tomorrow." She presses a kiss to his throat. "For now just... stay. '
            'Keep talking. I like your voice."')

    def test_repeated_lines_dropped(self):
        new = ('"Maybe tomorrow. Right now I just want pancakes." She laughs.\n\n'
               '"Keep talking. I like your voice."\n\nShe yawns.')
        out = enforcers.drop_repeated_lines(new, self.PREV)
        self.assertNotIn("Maybe tomorrow", out)
        self.assertIn("Right now I just want pancakes.", out)
        self.assertNotIn("I like your voice", out)
        self.assertIn("She yawns.", out)

    def test_new_lines_untouched(self):
        new = '"Something new entirely." She sits up.'
        self.assertEqual(enforcers.drop_repeated_lines(new, self.PREV), new)

    def test_curly_quotes_in_editor_answers(self):
        reply = 'She grins. “It’s fine, I’m fine.”'
        with mock.patch.object(enforcers, "call_llm", return_value=
                               '{"edits": [{"old": "It\'s fine, I\'m fine.", "new": "I\'m okay, really."}]}'), \
             mock.patch.object(enforcers.db, "get_settings", return_value={}):
            out = enforcers.humanize_reply(reply, "en")
        self.assertIn("really", out)


class StreamMode(unittest.TestCase):
    """KAngel must show up when the user's message starts the stream, and
    leave when it ends — even if the model skips or mislabels the tag."""

    def test_cues(self):
        cases = {
            "*the next morning she starts the stream again in better spirits*": "start",
            "*she transforms into something different, kangel*": "start",
            "*kangel appears and starts talking*": "start",
            "*empieza el stream*": "start",
            "*the stream stops suddenly* are you okay": "end",
            "*ends the stream* You were amazing.": "end",
            "Did you stream? I didn't know.": None,
            "lets play a game while streaming someday": None,
            "you look so alive today": None,
        }
        for text, want in cases.items():
            self.assertEqual(prompting.stream_cue(text), want, text)

    def test_wrong_family_is_mapped(self):
        import sprites
        with mock.patch.object(sprites, "tag_to_url", return_value=None), \
             mock.patch.object(sprites.db, "set_mode") as sm, \
             mock.patch.object(sprites.db, "bump_assistant_turn"):
            _, _, tag = sprites.resolve_sprite("She waves.\n[[sprite: ame_happy]]",
                                               stream_cue="start", expected_mode="kangel")
        self.assertEqual(tag, "kangel_happy")
        sm.assert_called_with("kangel")

    def test_missing_tag_classified_inside_the_family(self):
        import sprites
        with mock.patch.object(sprites, "call_llm", return_value="kangel_waving") as cl, \
             mock.patch.object(sprites, "tag_to_url", return_value=None), \
             mock.patch.object(sprites.db, "set_mode"), \
             mock.patch.object(sprites.db, "bump_assistant_turn"), \
             mock.patch.object(sprites.db, "get_settings", return_value={}):
            _, _, tag = sprites.resolve_sprite("She waves at chat.", stream_cue="start",
                                               expected_mode="kangel")
        self.assertEqual(tag, "kangel_waving")
        system = cl.call_args.args[0][0]["content"]
        self.assertNotIn("ame_happy", system)        # only kangel_* offered

    def test_no_cue_keeps_model_choice(self):
        import sprites
        with mock.patch.object(sprites, "tag_to_url", return_value=None), \
             mock.patch.object(sprites.db, "set_mode"), \
             mock.patch.object(sprites.db, "bump_assistant_turn"):
            _, _, tag = sprites.resolve_sprite("She sighs.\n[[sprite: ame_stressed]]",
                                               stream_cue=None, expected_mode="kangel")
        self.assertEqual(tag, "ame_stressed")


class SpriteSequence(unittest.TestCase):
    def test_continue_keeps_every_sprite(self):
        mid = database.save_message("assistant", "Hi.", None, sprite="/a.gif")
        database.update_message_content(mid, "Hi. More.", None, sprite="/b.gif")
        database.update_message_content(mid, "Hi. More. Again.", None, sprite="/b.gif")
        self.assertEqual(database.get_sprites(mid), ["/a.gif", "/b.gif"])
        hist = [h for h in database.get_history(5) if h["content"] == "Hi. More. Again."]
        self.assertEqual(hist[-1]["sprites"], ["/a.gif", "/b.gif"])

    def test_old_rows_fall_back_to_sprite(self):
        self.assertEqual(database._sprite_list(None, "/x.gif"), ["/x.gif"])
        self.assertEqual(database._sprite_list("not json", None), [])


class OpeningLanguage(unittest.TestCase):
    def test_every_language_has_its_opening(self):
        import app_config as ac
        self.assertIn("vos sos", ac.opening_for("es-ar"))
        for lang in ("es-pe", "es-mx", "es-es"):
            self.assertIn("tú eres", ac.opening_for(lang))
        self.assertIn("Dinder", ac.opening_for("it"))
        for lang in ("ja", "ko", "zh"):
            self.assertIn("「", ac.opening_for(lang))
        self.assertEqual(ac.opening_for("fr"), ac.opening_for("en"))
        self.assertNotIn("\n\n\n", ac.opening_for("es"))

    def test_opening_follows_language_until_the_first_message(self):
        import main
        import numpy as np
        from fastapi.testclient import TestClient
        tmp = os.path.join(tempfile.mkdtemp(), "o.db")
        with mock.patch.object(database, "DB_PATH", tmp), \
             mock.patch.object(main.memory, "embed", return_value=np.zeros(4)), \
             mock.patch.object(main.memory, "index_chunks"), \
             mock.patch.object(main.memory, "remember",
                               side_effect=lambda role, text: database.save_message(role, text, None)):
            database.init_db()
            database.set_settings(language="es-pe")
            c = TestClient(main.app)
            first = c.get("/opening").json()["scenario"]
            self.assertIn("tú eres", first)
            r = c.post("/settings", json={"language": "it"}).json()
            self.assertTrue(r["opening_swapped"])
            self.assertIn("Quindi sei tu", database.get_history(5)[-1]["content"])
            database.save_message("user", "ciao", None)        # the chat has started
            r = c.post("/settings", json={"language": "en"}).json()
            self.assertFalse(r["opening_swapped"])


class TalkGuards(unittest.TestCase):
    def test_spoken_words_measured(self):
        recent = [{"role": "assistant", "content": 'She sits. "One two three." She waits.'},
                  {"role": "assistant", "content": '"Four five." Then "six."'}]
        self.assertEqual(prompting._spoken_words(recent, n=2), 3.0)

    def test_structure_never_opens_on_inner_voice(self):
        import dice
        for t in dice.STRUCTURE_TEMPLATES:
            self.assertNotIn("inside her head", t)
            self.assertNotIn("interleave", t)

    def test_no_cutting_on_stream(self):
        p = enforcers._polish_prompt("en", "Sam", "", False, live=True)
        self.assertIn("live on stream", p)
        self.assertNotIn('giving "new": ""', p)


class InnerVoiceTidy(unittest.TestCase):
    def test_extra_lines_dropped_opener_moved(self):
        t = ('«first»\n\nShe smiles.\n\n"Hi."\n\nShe sits.\n\n«second»\n\n'
             '[[sprite: ame_happy]]')
        out = enforcers.tidy_inner_voice(t)
        self.assertEqual(out.count("«"), 1)
        self.assertFalse(out.startswith("«"))
        t2 = '«only»\n\nShe smiles.\n\n"Hi."\n\nShe sits.\n\n[[sprite: ame_happy]]'
        out2 = enforcers.tidy_inner_voice(t2)
        self.assertFalse(out2.startswith("«"))
        self.assertIn("«only»", out2)
        self.assertTrue(out2.endswith("[[sprite: ame_happy]]"))

    def test_inline_inner_voice_is_left_alone(self):
        t = 'She smiles. «stay» She sits.\n\n"Hi."'
        self.assertEqual(enforcers.tidy_inner_voice(t), t)


class SpeechPolish(unittest.TestCase):
    """Only spoken lines may change, only with exact matches, and a bad
    answer from the model changes nothing."""

    REPLY = ('She grins at him. "You\'re smug and goner all at once, Dummkopf." '
             'She looks away.\n\n«he stayed»\n[[sprite: ame_happy]]')

    def _run(self, answer):
        with mock.patch.object(enforcers, "call_llm", return_value=answer), \
             mock.patch.object(enforcers.db, "get_settings", return_value={"llm_provider": "deepseek"}):
            return enforcers.humanize_reply(self.REPLY, "en")

    def test_valid_edit_applied(self):
        out = self._run('{"edits": [{"old": "You\'re smug and goner all at once, Dummkopf.", '
                        '"new": "You\'re so smug right now, idiot."}]}')
        self.assertIn("You're so smug right now, idiot.", out)
        self.assertIn("«he stayed»", out)

    def test_inner_voice_and_sprite_untouchable(self):
        out = self._run('{"edits": [{"old": "«he stayed»", "new": "he left"}, '
                        '{"old": "ame_happy", "new": "ame_sad"}, '
                        '{"old": "She grins at him. \\"You", "new": "She grins.\\n\\"You"}]}')
        self.assertEqual(out, self.REPLY)

    def test_odd_narration_can_be_fixed(self):
        out = self._run('{"edits": [{"old": "She looks away.", "new": "She looks at the floor."}]}')
        self.assertIn("She looks at the floor.", out)

    def test_prompt_carries_job_rule_and_wants(self):
        p = enforcers._polish_prompt("en", "Sam", "ICU nurse", False)
        self.assertIn("Sam", p); self.assertIn("ICU nurse", p)
        p2 = enforcers._polish_prompt("en", "Sam", "ICU nurse", True)
        self.assertIn("brought up the job", p2); self.assertNotIn("6. This reply", p2)

    def test_one_throwaway_line_can_be_cut(self):
        reply = ('She grins. "Okay. Fine. Whatever. You win. Your hair is a crime '
                 'scene. Come here."\n\n«ugh»\n[[sprite: ame_happy]]')
        with mock.patch.object(enforcers, "call_llm", return_value=
                               '{"edits": [{"old": "Your hair is a crime scene.", "new": ""}, '
                               '{"old": "Come here.", "new": ""}]}'), \
             mock.patch.object(enforcers.db, "get_settings", return_value={}):
            out = enforcers.humanize_reply(reply, "en")
        self.assertNotIn("crime scene", out)
        self.assertIn('You win. Come here."', out)      # only ONE deletion

    def test_no_cut_when_she_barely_talks(self):
        reply = 'She grins. "Okay. Come here."\n\n«ugh»'
        self.assertFalse(enforcers._validate_deletion(reply, "Come here."))
        self.assertFalse(enforcers._validate_deletion(reply, '"Okay. Come here."'))

    def test_garbage_answer_changes_nothing(self):
        self.assertEqual(self._run("I can't help with that."), self.REPLY)


class Streaming(unittest.TestCase):
    """POST /chat_stream: deltas as they arrive, then the final turn saved
    exactly like /chat; a provider that can't stream falls back."""

    def _sse_response(self, pieces):
        lines = []
        for p in pieces:
            lines.append("data: " + json.dumps({"choices": [{"delta": {"content": p}}]}))
            lines.append("")
        lines.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                            "usage": {"completion_tokens": 9}}))
        lines.append("data: [DONE]")
        r = mock.MagicMock(status_code=200)
        r.iter_lines.return_value = iter(lines)
        return r

    def _events(self, body):
        out = []
        for block in body.split("\n\n"):
            m = re.search(r"^event: (.*)$", block, re.M)
            d = re.search(r"^data: (.*)$", block, re.M)
            if m:
                out.append((m.group(1), json.loads(d.group(1)) if d else {}))
        return out

    def test_stream_then_done(self):
        import main, memory, living_memory, llm
        from fastapi.testclient import TestClient
        pieces = ['She looks up. ', '"Hey."', '\n\n«he came back»\n', '[[sprite: ame_happy]]']
        settings = dict(database.get_settings(), llm_provider="deepseek",
                        deepseek_api_key="k", text_enforcers=0)
        before = database.count_messages()
        with mock.patch.object(llm.db, "get_settings", return_value=settings), \
             mock.patch.object(main.db, "get_settings", return_value=settings), \
             mock.patch.object(llm.requests, "post", return_value=self._sse_response(pieces)), \
             mock.patch.object(memory, "semantic_search", return_value=[]), \
             mock.patch.object(memory, "embed_deferred"), \
             mock.patch.object(memory, "maybe_summarize"), \
             mock.patch.object(living_memory, "consolidate"):
            r = TestClient(main.app).post("/chat_stream", json={"message": "hi"})
        ev = self._events(r.text)
        self.assertEqual([e for e, _ in ev].count("delta"), 4)
        self.assertEqual(ev[-1][0], "done")
        self.assertNotIn("[[sprite", ev[-1][1]["reply"])
        self.assertIn("«he came back»", ev[-1][1]["reply"])
        self.assertEqual(database.count_messages(), before + 2)

    def test_runaway_reply_is_cut(self):
        """Several replies glued together with a stray 'assistant' between
        them: only the first one is kept."""
        import main
        glued = 'Hola.\n\n«x»\n\n\n\nassistantAme no responde. Otra respuesta entera.'
        self.assertEqual(main.trim_runaway(glued), 'Hola.\n\n«x»')
        tagged = 'Hola. [[sprite: ame_idle]]\n\nY sigue hablando.'
        self.assertEqual(main.trim_runaway(tagged), 'Hola. [[sprite: ame_idle]]')
        normal = 'She tells the user about the user manual.\n«fine»'
        self.assertEqual(main.trim_runaway(normal), normal)

    def test_fallback_when_streaming_refused(self):
        import llm
        refused = mock.MagicMock(status_code=400)
        settings = {"llm_provider": "deepseek", "deepseek_api_key": "k", "deepseek_model": "m"}
        with mock.patch.object(llm.db, "get_settings", return_value=settings), \
             mock.patch.object(llm.requests, "post", return_value=refused), \
             mock.patch.object(llm, "call_llm", return_value="whole reply") as cl:
            out = list(llm.stream_llm([{"role": "user", "content": "x"}]))
        self.assertEqual(out, [("delta", "whole reply")])
        cl.assert_called_once()


class Regenerate(unittest.TestCase):
    """↻ Regenerate writes the last reply again; the old one stays as an
    earlier version, and a failure puts everything back."""

    def _client_ctx(self, reply_pieces=None, fail=False):
        import main, memory, living_memory
        def fake_stream(*a, **k):
            if fail:
                raise RuntimeError("provider down")
            for p in reply_pieces:
                yield ("delta", p)
        return [mock.patch.object(main, "stream_llm", side_effect=fake_stream),
                mock.patch.object(main.enforcers, "humanize_reply", side_effect=lambda t, *a, **k: t),
                mock.patch.object(main.enforcers, "enforce_inner_voice", side_effect=lambda t, *a, **k: t),
                mock.patch.object(memory, "semantic_search", return_value=[]),
                mock.patch.object(memory, "embed_deferred"),
                mock.patch.object(memory, "maybe_summarize"),
                mock.patch.object(memory, "embed", return_value=__import__("numpy").zeros(4)),
                mock.patch.object(memory, "index_chunks"),
                mock.patch.object(living_memory, "consolidate")]

    def _run(self, path, patches, body=None):
        import main
        from fastapi.testclient import TestClient
        for p in patches:
            p.start()
        try:
            return TestClient(main.app).post(path, json=body or {})
        finally:
            for p in reversed(patches):
                p.stop()

    def _seed(self):
        database.save_message("user", "regen-test: hi", None)
        return database.save_message("assistant", 'She waves. "Hi."\n\n«ok»', None,
                                     sprite="/a.gif", sprite_tag="ame_happy")

    def test_regenerate_keeps_old_version(self):
        self._seed()
        before = database.count_messages()
        r = self._run("/regenerate_stream", self._client_ctx(
            ['She grins. "Hey, you."', "\n\n«again»\n", "[[sprite: ame_happy]]"]))
        done = [b for b in r.text.split("\n\n") if b.startswith("event: done")]
        self.assertTrue(done, r.text)
        data = json.loads(done[0].split("data: ", 1)[1])
        self.assertEqual((data["versions_n"], data["version_idx"]), (2, 1))
        self.assertEqual(database.count_messages(), before)
        hist = database.get_history(2)
        self.assertEqual(hist[0]["content"], "regen-test: hi")
        self.assertIn("Hey, you.", hist[1]["content"])
        self.assertEqual(hist[1]["versions_n"], 2)
        # back to the first version
        r = self._run("/select_version", self._client_ctx([]), {"index": 0})
        self.assertIn('"Hi."', r.json()["reply"])
        self.assertIn('"Hi."', database.get_history(1)[0]["content"])

    def test_failure_puts_the_reply_back(self):
        aid = self._seed()
        r = self._run("/regenerate_stream", self._client_ctx(fail=True))
        self.assertIn("event: error", r.text)
        last = database.get_last_assistant_message()
        self.assertEqual(last["id"], aid)
        self.assertIn('"Hi."', last["content"])

    def test_nothing_to_regenerate(self):
        database.save_message("user", "regen-test: lonely", None)
        r = self._run("/regenerate_stream", self._client_ctx([]))
        self.assertEqual(r.status_code, 400)


class TurnLock(unittest.TestCase):
    """Only one turn at a time: a second request while Ame is answering
    gets 409, and the lock is always given back (success or failure)."""

    def test_second_turn_waits_its_turn(self):
        import main, memory, living_memory, threading as th
        from fastapi.testclient import TestClient
        gate = th.Event()
        def slow_stream(*a, **k):
            gate.wait(5)
            yield ("delta", '"Hi."\n\n«x»\n[[sprite: ame_happy]]')
        patches = [mock.patch.object(main, "stream_llm", side_effect=slow_stream),
                   mock.patch.object(main.enforcers, "humanize_reply", side_effect=lambda t, *a, **k: t),
                   mock.patch.object(memory, "semantic_search", return_value=[]),
                   mock.patch.object(memory, "embed_deferred"),
                   mock.patch.object(memory, "maybe_summarize"),
                   mock.patch.object(living_memory, "consolidate")]
        for p in patches: p.start()
        try:
            c = TestClient(main.app)
            out = {}
            t = th.Thread(target=lambda: out.setdefault("first", c.post("/chat_stream", json={"message": "lock-test 1"})))
            t.start()
            for _ in range(50):
                if main._TURN_LOCK.locked(): break
                __import__("time").sleep(0.02)
            self.assertEqual(c.post("/chat_stream", json={"message": "lock-test 2"}).status_code, 409)
            self.assertEqual(c.post("/regenerate_stream").status_code, 409)
            self.assertEqual(c.post("/continue").status_code, 409)
            gate.set(); t.join(10)
            self.assertIn("event: done", out["first"].text)
            self.assertFalse(main._TURN_LOCK.locked())
        finally:
            for p in reversed(patches): p.stop()

    def test_lock_released_after_failure(self):
        import main
        from fastapi.testclient import TestClient
        with mock.patch.object(main.prompting, "build_messages", side_effect=RuntimeError("boom")):
            r = TestClient(main.app).post("/chat_stream", json={"message": "lock-test fail"})
        self.assertIn("event: error", r.text)
        self.assertFalse(main._TURN_LOCK.locked())

    def test_continuation_not_forced_a_second_inner_voice(self):
        import main
        dice = {"language": "en"}
        with mock.patch.object(main.db, "get_settings", return_value={"polish_speech": 0}), \
             mock.patch.object(main.enforcers, "enforce_inner_voice") as iv:
            main._enforcer_chain('"More."', dice, continuation=True)
        iv.assert_not_called()


class ContinueOnce(unittest.TestCase):
    def test_second_continue_refused(self):
        import main, memory
        import numpy as np
        from fastapi.testclient import TestClient
        database.save_message("user", "once-test", None)
        aid = database.save_message("assistant", '"Hi."\n\n«x»', None, sprite="/a.gif")
        with mock.patch.object(memory, "embed", return_value=np.zeros(4)), \
             mock.patch.object(memory, "index_chunks"):
            memory.remember_continuation(aid, '"Hi."\n\n«x»', '"More."', sprite="/b.gif")
        self.assertTrue(database.get_history(1)[0]["continued"])
        self.assertEqual(database.get_sprites(aid), ["/a.gif", "/b.gif"])
        r = TestClient(main.app).post("/continue")
        self.assertEqual(r.status_code, 400)
        self.assertIn("already continued", r.json()["detail"])


class SpriteManager(unittest.TestCase):
    GIF = b"GIF89a" + b"\x00" * 40
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40

    def setUp(self):
        import sprites
        self.sp = sprites
        self.tmp = tempfile.mkdtemp()
        self.patches = [mock.patch.object(sprites, "SPRITES_DIR", self.tmp),
                        mock.patch.object(sprites, "CUSTOM_TAGS_FILE", os.path.join(self.tmp, "custom_tags.json"))]
        for p in self.patches: p.start()
        sprites.reload_catalog()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.sp.reload_catalog()

    def test_replace_add_variant_and_trash(self):
        sp = self.sp
        self.assertIsNone(sp.tag_to_url("ame_happy"))
        self.assertEqual(sp.save_sprite("ame_happy", self.GIF), "ame_happy.gif")
        self.assertEqual(sp.save_sprite("ame_happy", self.PNG, "add"), "ame_happy--v2.png")
        self.assertEqual(sp.files_for("ame_happy"), ["ame_happy.gif", "ame_happy--v2.png"])
        urls = {sp.tag_to_url("ame_happy").split("?")[0] for _ in range(40)}
        self.assertEqual(len(urls), 2)                      # variants picked at random
        sp.save_sprite("ame_happy", self.PNG, "replace")
        self.assertEqual(sp.files_for("ame_happy"), ["ame_happy.png"])
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "_trash"))), 2)
        # an old message's .gif URL now points to the current image
        self.assertIn("ame_happy.png", sp.fix_url("/images/sprites/ame/ame_happy.gif"))
        self.assertNotIn("ame_intimate1", sp.files_for("ame_intimate"))

    def test_rejects_non_images(self):
        with self.assertRaises(ValueError):
            self.sp.save_sprite("ame_happy", b"<html>not an image</html>")
        with self.assertRaises(ValueError):
            self.sp.save_sprite("ame_nonexistent", self.GIF)

    def test_custom_emotion_reaches_the_prompt(self):
        sp = self.sp
        sp.add_custom_tag("ame_pouting", "Sulking with puffed cheeks")
        self.assertIn("ame_pouting", sp.SPRITE_CATALOG)
        with self.assertRaises(ValueError):
            sp.add_custom_tag("ame_happy", "dup")
        with self.assertRaises(ValueError):
            sp.add_custom_tag("Bad Name!", "x")
        system = prompting._stable_prefix({"user_name": "Sam"}, 2)
        self.assertIn("ame_pouting: Sulking with puffed cheeks", system)
        sp.save_sprite("ame_pouting", self.GIF)
        sp.remove_custom_tag("ame_pouting")
        self.assertNotIn("ame_pouting", sp.SPRITE_CATALOG)
        self.assertEqual(sp.files_for("ame_pouting"), [])
        with self.assertRaises(ValueError):
            sp.remove_custom_tag("ame_happy")               # built-in emotions stay

    def test_api(self):
        import main
        from fastapi.testclient import TestClient
        c = TestClient(main.app)
        r = c.post("/sprites_api/upload", data={"tag": "kangel_idle", "mode": "replace"},
                   files={"file": ("x.gif", self.GIF, "image/gif")})
        self.assertEqual(r.status_code, 200, r.text)
        cat = {e["tag"]: e for e in c.get("/sprites_api/catalog").json()["emotions"]}
        self.assertEqual(cat["kangel_idle"]["files"][0]["name"], "kangel_idle.gif")
        self.assertEqual(cat["kangel_idle"]["family"], "kangel")
        bad = c.post("/sprites_api/upload", data={"tag": "kangel_idle"},
                     files={"file": ("x.gif", b"nope", "image/gif")})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(c.post("/sprites_api/delete_file",
                                json={"tag": "kangel_idle", "name": "kangel_idle.gif"}).status_code, 200)
        self.assertEqual(c.get("/sprites").status_code, 200)


class Console(unittest.TestCase):
    """The in-app console keeps what the server prints and hides API keys."""

    def test_capture_and_secrets(self):
        import console_log
        start = console_log.since(0)["last"]
        console_log._add("[llm] → Ame's reply: key sk-abcdef1234567890 Bearer xyz", "out")
        lines = console_log.since(start)["lines"]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("sk-abcdef", lines[0]["text"])
        self.assertNotIn("xyz", lines[0]["text"])

    def test_texture_is_a_state_not_an_insertion(self):
        import memory
        with mock.patch.object(memory, "semantic_search", return_value=[]), \
             mock.patch.object(prompting, "_roll_turn_dice", return_value={
                 "stage": 1, "sexual_scene_active": False, "psych_layer": None,
                 "crude_rolled": False, "crude_hostile_flavor": False, "crude_chance": .8,
                 "routine_departure_rolled": False, "self_deprecation_ok": False,
                 "long_speech_ok": False, "moan_ok": True, "performer_yardstick_ok": False,
                 "whimper_ok": True, "exclaim_ok": True, "vocal_chance": .2,
                 "whimper_chance": .25, "structure_template": "x."}):
            msgs, _ = prompting.build_messages("hola")
        self.assertIn("Her state today", msgs[-1]["content"])


class Security(unittest.TestCase):
    def test_remote_access_needs_key_and_hides_api_keys(self):
        import main
        from fastapi.testclient import TestClient
        remote = lambda: TestClient(main.app, client=("192.168.1.20", 5000))
        with mock.patch.dict(os.environ, {"AME_ACCESS_KEY": "k3y"}), \
             mock.patch.object(main.db, "get_settings", return_value={"deepseek_api_key": "sk-x"}):
            c = remote()
            self.assertEqual(c.get("/settings").status_code, 401)
            self.assertIn("access key", c.get("/", headers={"accept": "text/html"}).text)
            self.assertEqual(c.post("/login", data={"key": "nope"},
                                    follow_redirects=False).status_code, 401)
            r = c.post("/login", data={"key": "k3y"}, follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            self.assertNotEqual(c.get("/settings").json()["deepseek_api_key"], "sk-x")
            c2 = remote()                                     # link with ?key=
            self.assertEqual(c2.get("/?key=k3y", follow_redirects=False).status_code, 303)
            self.assertEqual(c2.get("/settings").status_code, 200)
            # behind a reverse proxy, "localhost" with proxy headers is remote
            proxied = TestClient(main.app, client=("127.0.0.1", 5000))
            self.assertEqual(proxied.get("/settings", headers={"x-forwarded-for": "8.8.8.8"})
                             .status_code, 401)
        with mock.patch.dict(os.environ, {"AME_ACCESS_KEY": ""}):
            self.assertEqual(remote().get("/").status_code, 403)   # remote access off
            local = TestClient(main.app, client=("127.0.0.1", 5000))
            with mock.patch.object(main.db, "get_settings", return_value={}):
                self.assertEqual(local.get("/settings").status_code, 200)

    def test_image_path_outside_uploads(self):
        for path in ("../../etc/passwd", "/images/uploads/../../main.py",
                     "/images/../ame.db", "/main.py"):
            self.assertIsNone(prompting.image_path_to_data_uri(path), path)

    def test_chat_rejects_outside_path(self):
        import main
        from fastapi import HTTPException
        payload = main.ChatIn(message="hi", image_path="../../main.py")
        with self.assertRaises(HTTPException) as ctx:
            main.chat(payload, None)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_image_path_valid(self):
        up = os.path.join(APP, "images", "uploads")
        os.makedirs(up, exist_ok=True)
        f = os.path.join(up, "_test_.png")
        with open(f, "wb") as fh:
            fh.write(b"\x89PNG\r\n")
        try:
            uri = prompting.image_path_to_data_uri("/images/uploads/_test_.png")
            self.assertTrue(uri and uri.startswith("data:image/png;base64,"))
        finally:
            os.remove(f)


class CoreFunctionsExist(unittest.TestCase):
    """Functions other modules and the frontend depend on."""

    def test_backend(self):
        import memory
        for mod, name in ((memory, "embedder_is_cached"), (memory, "remember_continuation"),
                          (memory, "_looks_like_refusal"), (enforcers, "enforce_dialect_final"),
                          (enforcers, "is_repetition"), (enforcers, "_log_swallowed"),
                          (memory, "summarize_rows"), (memory, "index_chunks"),
                          (memory, "index_pending_chunks")):
            self.assertTrue(hasattr(mod, name), f"{mod.__name__}.{name}")
        import living_memory
        for name in ("consolidate", "apply_operations", "always_block", "entries_block", "init"):
            self.assertTrue(hasattr(living_memory, name), f"living_memory.{name}")

    def test_frontend(self):
        if not INDEX:
            self.skipTest("index.html not found")
        html = _read_file(INDEX)
        for name in ("function splitDashLine", "function splitInnerVoice"):
            self.assertIn(name, html)


class Formatter(unittest.TestCase):
    """Runs the formatter from index.html with Node (if installed) on known
    cases. Most are Spanish because the formatter implements the Spanish
    dialogue-dash convention."""

    CASES = [
        ("—Hola —dijo ella—. ¿Vienes?", "D:Hola|N:dijo ella|D:. ¿Vienes?"),
        ("Deja escapar un quejido. —Pan con palta, por favor.",
         "N:Deja escapar un quejido.|D:Pan con palta, por favor."),
        ("Lo mira con un miedo — uno que no pide ser resuelto.",
         "N:Lo mira con un miedo — uno que no pide ser resuelto."),
        ("—Yo— yo no sé qué decir.", "D:Yo— yo no sé qué decir."),
        ("—Fueron 3–4 horas —susurra.", "D:Fueron 3–4 horas|N:susurra."),
        ("—Te quiero «no lo digas así» tanto.", "D:Te quiero|I:«no lo digas así»|D:tanto."),
        ("Sonríe: —Ven aquí.", "N:Sonríe:|D:Ven aquí."),
        # *gestures* inside a spoken line are narration
        ("—Es que... *respira hondo* ...no sé.", "D:Es que...|N:respira hondo|D:...no sé."),
        ("—Eres *tan* tonta.", "D:Eres tan tonta."),          # one starred word = emphasis
        # English dialogue with quotation marks, dashes as punctuation
        ('She looks up — tired. "You\'re early."', 'N:She looks up — tired.|D:"You\'re early."'),
        ('"Hey— look at me." Her hands find his face.', 'D:"Hey— look at me."|N:Her hands find his face.'),
    ]

    def test_formatter(self):
        node = shutil.which("node")
        if not (node and INDEX):
            self.skipTest("Node.js or index.html not available")
        html = _read_file(INDEX)
        js = re.search(r"const DIALOGUE_LINE_RE.*?\nfunction formatUserHTML", html, re.S
                       ).group(0).rsplit("function formatUserHTML", 1)[0]
        esc = re.search(r"function escapeHTML\(.*?\n}\n", html, re.S).group(0)
        corpus = [inp for inp, _ in self.CASES] + [
            "Ella lo mira — cansada — y sonríe.\n—Ya, ya. Ven.",
            'She shrugs — then laughs. "Fine."']
        script = esc + js + """
const strip=h=>h.replace(/<[^>]+>/g,'').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&#39;/g,"'");
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const res={cases:[], lost_punctuation:0};
for(const [inp] of input.cases){
  const out=formatAssistantHTML(inp);
  const got=[...out.matchAll(/<span class="msg-(\\w+)">([^<]*)<\\/span>/g)].filter(m=>m[2].trim())
     .map(m=>[({dialogue:'D',narration:'N',innervoice:'I'})[m[1]], strip(m[2]).trim()]);
  const mg=[]; for(const [c,t] of got){ if(mg.length&&mg[mg.length-1][0]===c) mg[mg.length-1][1]+=' '+t; else mg.push([c,t]); }
  res.cases.push(mg.map(x=>x[0]+':'+x[1].replace(/ ([.,;:!?])/g,'$1')).join('|'));
}
for(const t of input.corpus) for(const raw of t.split('\\n')){
  const line=raw.replace(/\\*/g,'').trim(); if(!line) continue;
  // a punctuation dash (between lowercase words, with spaces) must stay visible
  const n=(line.match(/[a-záéíóúñ] — [a-záéíóúñ]/g)||[]).length;
  const v=(strip(formatAssistantHTML(line)).match(/[a-záéíóúñ] — [a-záéíóúñ]/g)||[]).length;
  res.lost_punctuation+=Math.max(0,n-v);
}
console.log(JSON.stringify(res));
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
        try:
            r = subprocess.run([node, f.name], input=json.dumps(
                {"cases": self.CASES, "corpus": corpus}), capture_output=True,
                text=True, encoding="utf-8", timeout=60)
        finally:
            os.remove(f.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        for (inp, expected), obtained in zip(self.CASES, res["cases"]):
            self.assertEqual(obtained, expected.replace(" .", "."), inp)
        self.assertEqual(res["lost_punctuation"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
