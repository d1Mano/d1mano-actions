import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from catalog import triage

STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
mode = os.environ.get("STUB_MODE", "good")
model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else ""
pairs = json.load(open(sys.argv[sys.argv.index("-f") + 1]))["pairs"]
if mode == "hang": time.sleep(30)
if mode == "bad-first-model" and model == "m1":
    print("Error: free tier unavailable"); sys.exit(1)
ids = [p["pair_id"] for p in pairs]
if mode == "poison" and "PRV_A-001__PRV_B-001" in ids:
    print("I could not process this batch."); sys.exit(0)
if mode == "pretty":
    print("Here is the answer:\n")
    print(json.dumps({"verdicts": [{"pair_id": i, "verdict": "same_product", "evidence": "ficha {igual}"} for i in ids]}, indent=2))
    print("\nHope that helps."); sys.exit(0)
if mode == "inject": ids = ids + ["PRV_X-001__PRV_Y-001"]
if mode == "partial": ids = ids[:-1]
verdict = "banana" if mode == "vocab" else "same_product"
print("\x1b[0mpreamble the model felt like writing")
print(json.dumps({"verdicts": [{"pair_id": i, "verdict": verdict, "evidence": "misma ficha"} for i in ids]}))
'''


def queue(n):
    return [{"pair_id": f"PRV_A-{i:03d}__PRV_B-{i:03d}", "left": {}, "right": {}} for i in range(1, n + 1)]


class Triage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = self.tmp / "stub.py"
        self.stub.write_text(STUB)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)
        (self.tmp / "prompt.md").write_text("judge the pairs")
        os.environ.pop("STUB_MODE", None)

    def go(self, n=5, mode="good", models=("m1",), decisions=(), **kw):
        os.environ["STUB_MODE"] = mode
        return triage.run({"triage_queue": queue(n), "decisions": list(decisions)},
                          self.tmp / "prompt.md", self.tmp / "work", self.tmp / "out.json",
                          size=2, cli=[sys.executable, str(self.stub)], models=list(models), **kw)

    def test_answers_are_accepted_and_checkpointed(self):
        result = self.go()
        self.assertEqual(len(result.verdicts), 5)
        self.assertEqual(result.unresolved, [])
        self.assertTrue(json.loads((self.tmp / "out.json").read_text())["complete"])

    def test_an_answer_about_a_pair_that_was_not_asked_is_rejected(self):
        result = self.go(mode="inject")
        self.assertEqual(result.verdicts, [])
        self.assertTrue(all(b.status == "rejected" for b in result.batches))

    def test_a_missing_pair_or_a_free_text_verdict_rejects_the_batch(self):
        self.assertEqual(self.go(mode="partial", n=2).verdicts, [])
        self.assertEqual(self.go(mode="vocab", n=2).verdicts, [])

    def test_the_model_cannot_merge_what_code_separated(self):
        refused = [{"pair_id": "PRV_A-001__PRV_B-001", "verdict": "DIFFERENT", "rule": "ADDON_ASYMMETRY"}]
        result = self.go(n=2, decisions=refused)
        self.assertEqual(result.verdicts, [])

    def test_next_model_in_the_chain_is_tried(self):
        result = self.go(n=2, mode="bad-first-model", models=("m1", "m2"))
        self.assertEqual(len(result.verdicts), 2)
        self.assertEqual(result.batches[0].attempts, 2)
        self.assertEqual(result.verdicts[0]["model"], "m2")

    def test_a_hung_call_costs_one_attempt(self):
        original = triage.CALL_TIMEOUT_SECONDS
        triage.CALL_TIMEOUT_SECONDS = 0.5
        try:
            result = self.go(n=1, mode="hang")
        finally:
            triage.CALL_TIMEOUT_SECONDS = original
        self.assertEqual(result.batches[0].cli_exit, 124)
        self.assertEqual(result.verdicts, [])

    def test_past_the_deadline_nothing_is_started(self):
        result = self.go(deadline=0.0, clock=lambda: 1.0)
        self.assertEqual({b.status for b in result.batches}, {"deferred"})

    def test_resume_does_not_ask_again(self):
        self.go(n=4)
        asked = []
        real = triage.invoke
        triage.invoke = lambda *a, **k: asked.append(1) or real(*a, **k)
        try:
            result = self.go(n=4, resume=True)
        finally:
            triage.invoke = real
        self.assertEqual((len(result.verdicts), asked), (4, []))

    def test_a_batch_the_model_cannot_digest_does_not_starve_the_rest(self):
        result = self.go(n=6, mode="poison")
        answered = {v["pair_id"] for v in result.verdicts}
        self.assertEqual(len(answered), 4)                       # batches 1 and 2
        self.assertNotIn("PRV_A-001__PRV_B-001", answered)
        self.assertEqual(result.batches[0].status, "rejected")
        self.assertEqual(result.batches[0].attempts, triage.MAX_PASSES)

    def test_a_pretty_printed_answer_wrapped_in_prose_is_found(self):
        result = self.go(n=2, mode="pretty")
        self.assertEqual(len(result.verdicts), 2)
        self.assertEqual(result.verdicts[0]["evidence"], "ficha {igual}")

    def test_refuses_to_start_holding_a_database_variable(self):
        with self.assertRaises(triage.CredentialsPresent):
            triage.assert_no_credentials({"SUPABASE_URL": "x"})
        triage.assert_no_credentials({"PATH": "/bin"})

    def test_transcripts_are_not_printed(self):
        import contextlib, io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            self.go(n=2)
        self.assertNotIn("preamble", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
