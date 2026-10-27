import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vntrans import VnTransError, validate, workspace
from vntrans.translation import glossary, prompts, providers, quality, runner
from vntrans.translation.glossary import Rules, Term
from vntrans.translation.providers import Gemini, OpenRouter, Profile, ProviderError, Reply

SCRIPT = ("*start|Opening\n[name name=Alice]Hello[l]world\n[name name=Bob]How are you?\n"
          "[name name=Alice]Fine.\n*next|Second\n[name name=Bob]Bye.\n")


def records(user):
    return [json.loads(line) for line in user.splitlines() if line.startswith('{"i"')]


class FakeProvider:
    """Translates by prefixing "T:"; `rules(record, call)` may override a record's text."""

    def __init__(self, rules=None, errors=(), reply=None):
        self.calls = []
        self.rules = rules or (lambda record, call: None)
        self.errors = list(errors)
        self.reply = reply

    def complete(self, profile, key, system, user, schema=providers.SCHEMA):
        self.calls.append(records(user))
        if self.errors:
            raise self.errors.pop(0)
        if self.reply:
            return Reply(json.dumps(self.reply(records(user), user)), "stop")
        out = []
        for record in records(user):
            text = self.rules(record, len(self.calls))
            out.append(dict(i=record["i"], t=f"T:{record['text']}" if text is None else text))
        return Reply(json.dumps(dict(translations=out)), "stop", 10, 5)


class GlossaryPromptTests(unittest.TestCase):
    def test_match_is_selective_and_ordered(self):
        terms = [Term("Alice", "Arisu", "character"), Term("alice", "x", case_sensitive=True),
                 Term("Ali", "Al"), Term("Bob", ""), Term("Hello", "Halo", approved=False)]
        found = glossary.match(terms, ["Hello ALICE"])
        self.assertEqual([t.target for t in found], ["Arisu", "Al"])

    def test_json_round_trip_and_foreign_keys(self):
        terms = glossary.loads('[{"src": "魔法", "dst": "magic", "info": "noun"}]')
        self.assertEqual((terms[0].source, terms[0].target, terms[0].notes), ("魔法", "magic", "noun"))
        self.assertEqual(glossary.loads(glossary.dumps(terms))[0].target, "magic")
        with self.assertRaises(VnTransError):
            glossary.loads('[{"source": ""}]')

    def test_rules_touch_only_visible_text(self):
        rules = Rules.parse("# note\npre ♡ => ~\npost /(\\w+)!/ => \\1.\npost ⟦0⟧ =>\n")
        self.assertEqual(Rules.apply(rules.pre, "a♡⟦0⟧♡"), "a~⟦0⟧~")
        self.assertEqual(Rules.apply(rules.post, "Hi! ⟦0⟧ yo!"), "Hi. ⟦0⟧ yo.")
        for bad in ("mid x => y", "pre /(/ => y", "post /a/ => \\5"):
            with self.assertRaises(VnTransError):
                Rules.parse(bad)

    def test_chain_keeps_order_and_contract_last(self):
        chain = prompts.chain({"sections": [{"key": "context", "enabled": False}, {"key": "contract"},
                                            {"key": "role", "text": "Mine {target}"}, {"key": "bogus"}]})
        self.assertEqual([s["key"] for s in chain],
                         ["context", "role", "style", "instructions", "characters", "glossary", "contract"])
        self.assertFalse(chain[0]["enabled"])


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "game").mkdir()
        (self.root / "game/a.ks").write_text(SCRIPT)
        workspace.create(self.root / "game", self.root / "work", name="T")
        self.ws = workspace.Workspace(self.root / "work")
        self.addCleanup(self.ws.close)
        self.rows = self.ws.rows()

    def find(self, source):
        return next(i for i, row in enumerate(self.rows) if row.source == source)


class PromptBuildTests(WorkspaceCase):
    def test_build_is_deterministic_with_scene_context(self):
        chain = prompts.chain({})
        batch = [self.find("How are you?")]
        args = (chain, self.ws.manifest["languages"], self.rows, batch)
        first = prompts.build(*args, terms=[Term("Bob", "Bobu", "character")], before=5, after=5)
        self.assertEqual(first, prompts.build(*args, terms=[Term("Bob", "Bobu", "character")], before=5, after=5))
        self.assertIn("Characters:\n- Bob → Bobu", first.user)
        self.assertIn("- Alice: Hello⟦0⟧world", first.user)
        self.assertNotIn("Bye.", first.user)
        self.assertEqual(records(first.user), [dict(i=1, kind="dialogue", speaker="Bob", text="How are you?")])
        self.assertTrue(first.system.endswith(prompts.CONTRACT))
        self.assertIn("into natural, fluent English", first.system)
        quiet = prompts.build(*args, before=0, after=0)
        self.assertNotIn("reference only", quiet.user)
        self.assertEqual(quiet.version, first.version)

    def test_plan_splits_on_scene_and_size(self):
        lines = [i for i, row in enumerate(self.rows) if row.kind != "speaker"]
        self.assertEqual(len(prompts.plan(self.rows, lines)), 2)
        self.assertEqual([len(b) for b in prompts.plan(self.rows, lines, lines=2)], [2, 2, 2])


class DecodeTests(unittest.TestCase):
    def test_accepted_shapes(self):
        expected = ({1: "a", 2: "b"}, set())
        self.assertEqual(runner.decode('{"translations": [{"i": 1, "t": "a"}, {"i": 2, "t": "b"}]}'), expected)
        self.assertEqual(runner.decode('```json\n[{"i": 1, "t": "a"}, {"i": "2", "t": "b"}]\n```'), expected)
        self.assertEqual(runner.decode('Sure!\n{"1": "a", "2": "b"} done'), expected)

    def test_duplicates_and_garbage(self):
        self.assertEqual(runner.decode('[{"i": 1, "t": "a"}, {"i": 1, "t": "b"}, {"i": true, "t": "c"}, {"x": 1}]')[1], {1})
        for bad in ("no json here", "[1, 2", "42"):
            with self.assertRaises(ValueError):
                runner.decode(bad)


class ProviderTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(providers.classify(401, "{}", None).kind, "auth")
        self.assertEqual(providers.classify(400, '{"error": {"details": [{"reason": "API_KEY_INVALID"}]}}', None).kind, "auth")
        rate = providers.classify(429, '{"error": {"message": "slow", "details": [{"retryDelay": "21s"}]}}', None)
        self.assertEqual((rate.kind, rate.retry_after, str(rate)), ("rate", 21.0, "HTTP 429: slow"))
        self.assertEqual(providers.classify(429, "", "7").retry_after, 7.0)
        self.assertEqual(providers.classify(503, "", None).kind, "transient")
        self.assertEqual(providers.classify(404, "", None).kind, "request")

    def test_gemini_mapping(self):
        gemini = Gemini()
        body = gemini.payload(Profile("p", model="gemini-2.5-flash", reasoning="minimal"), "S", "U")
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0, "includeThoughts": True})
        body = gemini.payload(Profile("p", model="gemini-2.0-flash"), "S", "U")
        self.assertNotIn("thinkingConfig", body["generationConfig"])
        self.assertEqual(body["generationConfig"]["responseSchema"]["type"], "OBJECT")
        self.assertNotIn("additionalProperties", json.dumps(body))
        body = gemini.payload(Profile("p", model="gemini-3-flash", reasoning="low", show_thinking=False), "S", "U")
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "LOW"})
        reply = gemini.parse({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [
            {"text": "think", "thought": True}, {"text": "{}"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "thoughtsTokenCount": 5}})
        self.assertEqual(reply, Reply("{}", "length", 3, 9, "think"))
        with self.assertRaises(ProviderError) as caught:
            gemini.parse({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
        self.assertEqual(caught.exception.kind, "blocked")

    def test_openrouter_mapping(self):
        router = OpenRouter()
        body = router.payload(Profile("p", provider="openrouter", model="x/y", reasoning="high"), "S", "U")
        self.assertEqual(body["reasoning"], {"effort": "high", "exclude": False})
        body = router.payload(Profile("p", provider="openrouter", model="x/y", show_thinking=False), "S", "U")
        self.assertEqual(body["reasoning"], {"exclude": True})
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(router.parse({"choices": [{"message": {"content": "ok", "reasoning": "hmm"},
                                                    "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 1, "completion_tokens": 2}}),
                         Reply("ok", "stop", 1, 2, "hmm"))
        with self.assertRaises(ProviderError) as caught:
            router.parse({"error": {"code": 402, "message": "no credits"}})
        self.assertEqual(caught.exception.kind, "auth")

    def test_env_key_override(self):
        profile = Profile("My Gemini", keys=["stored"])
        self.assertEqual(profile.api_keys(), ["stored"])
        with patch.dict("os.environ", {"VNTRANS_MY_GEMINI_KEY": "a, b"}):
            self.assertEqual(profile.api_keys(), ["a", "b"])


class RunnerTests(WorkspaceCase):
    def run_with(self, provider, indices=None, terms=(), **settings):
        indices = [i for i, row in enumerate(self.rows) if row.status == "new"] if indices is None else indices
        run = runner.Run(self.ws, self.rows, indices, profile=Profile("p", model="m", keys=["k"]),
                         provider=provider, sections=prompts.chain({}), terms=list(terms),
                         settings=dict(concurrency=2) | settings)
        events = []
        run.on_event = lambda kind, text: events.append((kind, text))
        asyncio.run(run.run())
        return run, events

    def test_translates_everything_and_exports_valid_json(self):
        fake = FakeProvider()
        run, events = self.run_with(fake, terms=[Term("Alice", "Arisu", "character")])
        self.assertEqual(run.stats.done, len(self.rows))
        self.assertTrue(all(row.status == "machine" for row in self.rows))
        self.assertEqual({r.translation for r in self.rows if r.source == "Alice"}, {"Arisu"})
        self.assertEqual({r.translation for r in self.rows if r.source == "Bob"}, {"T:Bob"})
        name_requests = [call for call in fake.calls if call and call[0]["kind"] == "speaker"]
        self.assertEqual(name_requests, [[dict(i=1, kind="speaker", text="Bob")]])
        self.assertEqual(events[-1][0], "done")
        target, _ = self.ws.build()
        validate(self.root / "work/source", translations=target / "json")
        attempts = self.ws.db.execute("SELECT count(*), sum(tokens_in) FROM attempts WHERE outcome = 'ok'").fetchone()
        self.assertEqual(attempts, (len(fake.calls), 10 * len(fake.calls)))

    def test_bad_placeholder_is_split_and_retried(self):
        hello = self.find("Hello⟦0⟧world")
        fake = FakeProvider(lambda record, call: "broken" if record["text"] == "Hello⟦0⟧world" and len(fake.calls) < 3 else None)
        run, _ = self.run_with(fake)
        self.assertEqual(self.rows[hello].translation, "T:Hello⟦0⟧world")
        self.assertEqual(run.stats.failed, 0)

    def test_permanent_failure_is_marked_not_saved(self):
        hello = self.find("Hello⟦0⟧world")
        fake = FakeProvider(lambda record, call: "broken" if record["text"] == "Hello⟦0⟧world" else None)
        run, _ = self.run_with(fake)
        row = self.rows[hello]
        self.assertEqual((row.status, row.translation), ("new", None))
        self.assertIn("placeholders", row.error)
        self.assertEqual(run.stats.failed, 1)
        self.assertEqual(run.stats.done, len(self.rows) - 1)
        single_tries = [call for call in fake.calls if call == [dict(i=1, kind="dialogue", speaker="Alice", text="Hello⟦0⟧world")]]
        self.assertEqual(len(single_tries), 2)
        rerun, _ = self.run_with(FakeProvider())
        self.assertEqual((rerun.stats.total, row.status, row.error), (1, "machine", None))

    def test_transient_retried_auth_stops(self):
        fake = FakeProvider(errors=[ProviderError("transient", "flaky", retry_after=0.01)])
        run, _ = self.run_with(fake, concurrency=1)
        self.assertEqual(run.stats.done, len(self.rows))
        for row in self.rows:
            self.ws.save(row, None)
        fake = FakeProvider(errors=[ProviderError("auth", "bad key")])
        run, events = self.run_with(fake, concurrency=1)
        self.assertEqual((run.error, run.stats.done, run.stats.failed), ("bad key", 0, 0))
        self.assertEqual(events[-1], ("error", "Stopped: bad key"))
        self.assertTrue(all(row.error is None for row in self.rows))

    def test_manual_edit_during_run_wins(self):
        bye = self.find("Bye.")

        def rules(record, call):
            if record["text"] == "Bye.":
                self.ws.save(self.rows[bye], "Mine")
            return None

        async def inline(function, *args):
            # The user edits on the loop thread while a request is in flight.
            return function(*args)

        with patch("vntrans.translation.runner.asyncio.to_thread", inline):
            self.run_with(FakeProvider(rules))
        self.assertEqual((self.rows[bye].translation, self.rows[bye].status), ("Mine", "edited"))
        stored = self.ws.db.execute("SELECT translation FROM units WHERE id = ?", (self.rows[bye].id,)).fetchone()
        self.assertEqual(stored, ("Mine",))

    def test_rules_apply_to_prompt_and_reply(self):
        fake = FakeProvider(lambda record, call: record["text"] + "!!")
        rules = Rules.parse("pre Bob => Robert\npost !! => .")
        run = runner.Run(self.ws, self.rows, [self.find("How are you?")], profile=Profile("p", model="m", keys=["k"]),
                         provider=fake, sections=prompts.chain({}), terms=[], settings={}, rules=rules)
        asyncio.run(run.run())
        self.assertEqual(fake.calls[0][0]["speaker"], "Bob")
        self.assertEqual(self.rows[self.find("How are you?")].translation, "How are you?.")

    def test_review_suggests_without_touching_translations(self):
        self.run_with(FakeProvider())
        reviewed = [i for i, row in enumerate(self.rows) if row.kind == "dialogue"]
        hello = self.find("Hello⟦0⟧world")

        def reply(recs, user):
            out = [dict(i=r["i"], t="Better⟦0⟧one" if r["text"] == "Hello⟦0⟧world" else "no placeholder", r="tone")
                   for r in recs if r["text"] in ("Hello⟦0⟧world", "Fine.")]
            return dict(translations=out + [dict(i=99, t="x", r="bogus")])

        fake = FakeProvider(reply=reply)
        run = runner.Run(self.ws, self.rows, reviewed, profile=Profile("p", model="m", keys=["k"]), provider=fake,
                         sections=prompts.chain({}), terms=[], settings={}, task="review")
        asyncio.run(run.run())
        self.assertIn('"translation": "T:Hello⟦0⟧world"', json.dumps(fake.calls, ensure_ascii=False))
        suggestions = self.ws.suggestions()
        self.assertEqual(suggestions, {("a.ks", self.rows[hello].id): ("Better⟦0⟧one", "tone"),
                                       ("a.ks", self.rows[self.find("Fine.")].id): ("no placeholder", "tone")})
        self.assertEqual(self.rows[hello].translation, "T:Hello⟦0⟧world")
        self.assertEqual(run.found, 2)

    def test_discovery_adds_grounded_drafts(self):
        self.ws.save_term(Term("Alice", "Arisu", "character"))
        terms = self.ws.terms()

        def reply(recs, user):
            return dict(terms=[dict(source="Bob", target="Bobby", kind="character", notes="friend"),
                               dict(source="Alice", target="A", kind="character", notes=""),
                               dict(source="Dragon", target="Naga", kind="term", notes="not in text")])

        run = runner.Run(self.ws, self.rows, list(range(len(self.rows))), profile=Profile("p", model="m", keys=["k"]),
                         provider=FakeProvider(reply=reply), sections=[], terms=terms, settings={}, task="discover")
        asyncio.run(run.run())
        drafts = [t for t in self.ws.terms() if not t.approved]
        self.assertEqual([(t.source, t.target, t.kind, t.notes) for t in drafts], [("Bob", "Bobby", "character", "friend")])
        self.assertIn("Bob", drafts[0].evidence)

    def test_quality_signals(self):
        rows = {row.source: row for row in self.rows}
        self.ws.save_many([(rows["How are you?"], "お元気?"), (rows["Fine."], "Fine.\nOK"),
                           (rows["Hello⟦0⟧world"], "Hi⟦0⟧Alicia")])
        terms = [Term("Alice", "Arisu", "character"), Term("world", "dunia")]
        checker = quality.Checker(self.ws.check, "English", terms)
        found = {row.source: checker.issues(row) for row in self.rows}
        self.assertEqual(found["How are you?"], ["residue"])
        self.assertEqual(found["Fine."], ["lines"])
        self.assertEqual(found["Hello⟦0⟧world"], ["glossary"])
        self.assertEqual(found["Bye."], [])
        with self.assertRaises(VnTransError):
            self.ws.save_many([(rows["Bye."], "ok"), (rows["Hello⟦0⟧world"], "broken")])
        self.assertIsNone(rows["Bye."].translation)

    def test_batches_and_live_counters(self):
        fake = FakeProvider(lambda record, call: "broken" if record["text"] == "Hello⟦0⟧world" else None)
        batches = []
        run = runner.Run(self.ws, self.rows, [i for i, r in enumerate(self.rows) if r.kind != "speaker"],
                         profile=Profile("p", model="m", keys=["k"]), provider=fake, sections=prompts.chain({}),
                         terms=[Term("Alice", "Arisu", "character")], settings=dict(concurrency=2),
                         on_batch=batches.append)
        asyncio.run(run.run())
        self.assertEqual([b.number for b in batches], list(range(1, len(batches) + 1)))
        first = batches[0]
        self.assertEqual((first.task, first.file, first.tokens_in, first.tokens_out), ("translate", "a.ks", 10, 5))
        self.assertIn("Alice → Arisu", first.terms)
        outcomes = {source: (output, outcome, note) for _, source, output, outcome, note in first.records}
        self.assertEqual(outcomes["How are you?"], ("T:How are you?", "ok", ""))
        self.assertEqual(outcomes["Hello⟦0⟧world"][:2], ("broken", "failed"))
        self.assertIn("placeholders", outcomes["Hello⟦0⟧world"][2])
        self.assertTrue(any(b.attempt > 0 for b in batches))
        self.assertEqual((run.stats.active, run.stats.queued, run.stats.cooling), (0, 0, 0))
        self.assertEqual(run.stats.replies, len(fake.calls))
        self.assertGreater(run.stats.rate(), 0)

    def test_missing_key_is_rejected(self):
        with self.assertRaisesRegex(VnTransError, "no API key"):
            runner.Run(self.ws, self.rows, [], profile=Profile("p", model="m"), provider=FakeProvider(),
                       sections=[], terms=[], settings={})


if __name__ == "__main__":
    unittest.main()
