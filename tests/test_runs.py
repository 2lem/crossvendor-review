import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from support import CATALOG, Env, GlmHandler, write
from cvr import advise, cli, review


class ReviewRunTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.repo = self.env.repo()
        write(os.path.join(self.repo, "app.py"), "def add(a, b):\n    return a - b\n")

    def tearDown(self):
        self.env.close()

    def run_review(self, vendors, **kw):
        cfg = self.env.config(vendors)
        return review.run(cfg, CATALOG, self.repo, out_dir=os.path.join(self.env.tmp, "out-%d" % len(
            os.listdir(self.env.tmp))), **kw)

    def test_all_lenses_answer(self):
        code, summary = self.run_review(["claude", "codex", "glm", "grok", "muse"])
        self.assertEqual(code, 0)
        self.assertEqual({r["status"] for r in summary["lenses"]}, {"ok"})
        self.assertNotIn("claude", {c["vendor"] for c in self.env.calls()})  # the host does not review
        with open(os.path.join(summary["run_dir"], "findings.json"), encoding="utf-8") as fh:
            findings = json.load(fh)["findings"]
        self.assertEqual(len(findings), 9)
        self.assertEqual({f["file"] for f in findings}, {"app.py"})
        self.assertEqual({f["group"] for f in findings}, {1})  # same file and line: one group
        with open(os.path.join(summary["run_dir"], "report.md"), encoding="utf-8") as fh:
            self.assertIn("app.py:2", fh.read())

    def test_failed_lens_uses_fallback(self):
        os.environ["FAKE_MODE_CODEX"] = "quota"
        code, summary = self.run_review(["claude", "codex", "grok"])
        self.assertEqual(code, 0)
        codex_lenses = [r for r in summary["lenses"] if r["attempts"][0]["vendor"] == "codex"]
        self.assertTrue(codex_lenses)
        for r in codex_lenses:
            self.assertEqual((r["vendor"], r["fallback_used"], r["attempts"][0]["status"]), ("grok", True, "quota"))

    def test_missing_lenses_are_never_clean(self):
        os.environ["FAKE_MODE_CODEX"] = "fail"
        os.environ["FAKE_MODE_CLAUDE"] = "fail"
        code, summary = self.run_review(["claude", "codex"])
        self.assertEqual(code, 3)
        with open(os.path.join(summary["run_dir"], "report.md"), encoding="utf-8") as fh:
            report = fh.read()
        self.assertIn("Incomplete", report)
        self.assertNotIn("No lens reported a problem", report)

    def test_answer_without_findings_object_is_empty_not_clean(self):
        os.environ["FAKE_MODE_GROK"] = "garbage"
        os.environ["FAKE_MODE_CLAUDE"] = "garbage"
        code, summary = self.run_review(["claude", "grok"])
        self.assertEqual(code, 3)
        self.assertEqual({r["status"] for r in summary["lenses"]}, {"empty"})

    def test_unusable_answer_goes_to_the_fallback(self):
        os.environ["FAKE_MODE_CODEX"] = "garbage"
        code, summary = self.run_review(["claude", "codex", "grok"])
        self.assertEqual(code, 0)
        moved = [r for r in summary["lenses"] if r["attempts"][0]["vendor"] == "codex"]
        self.assertTrue(moved)
        for r in moved:
            self.assertEqual((r["vendor"], r["status"], r["fallback_used"]), ("grok", "ok", True))
            self.assertEqual(r["attempts"][0]["status"], "empty")

    def test_all_malformed_findings_are_not_a_clean_answer(self):
        os.environ["FAKE_MODE_CODEX"] = "invalid-findings"
        os.environ["FAKE_MODE_CLAUDE"] = "invalid-findings"
        code, summary = self.run_review(["claude", "codex"])
        self.assertEqual(code, 3)
        self.assertEqual({r["status"] for r in summary["lenses"]}, {"invalid"})
        with open(os.path.join(summary["run_dir"], "report.md"), encoding="utf-8") as fh:
            report = fh.read()
        self.assertNotIn("No lens reported a problem", report)
        self.assertIn("| 1 |", report)  # the malformed count is shown

    def test_odd_severity_does_not_break_the_run(self):
        os.environ["FAKE_MODE_CODEX"] = "odd-severity"
        code, summary = self.run_review(["claude", "codex"], lenses=["security"])
        self.assertEqual(code, 0)
        with open(os.path.join(summary["run_dir"], "findings.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["findings"][0]["severity"], "minor")

    def test_empty_findings_list_is_a_clean_answer(self):
        os.environ["FAKE_MODE_CODEX"] = "empty-findings"
        code, summary = self.run_review(["claude", "codex"])
        self.assertEqual(code, 0)
        with open(os.path.join(summary["run_dir"], "report.md"), encoding="utf-8") as fh:
            self.assertIn("No lens reported a problem", fh.read())

    def test_dry_run_sends_nothing(self):
        code, summary = self.run_review(["claude", "codex", "glm"], dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.env.calls(), [])
        self.assertEqual(GlmHandler.calls, [])
        self.assertTrue(os.path.exists(os.path.join(summary["run_dir"], "packet.txt")))

    def test_lens_subset_and_unknown_lens(self):
        code, summary = self.run_review(["claude", "codex"], lenses=["security", "tests"])
        self.assertEqual([r["lens"] for r in summary["lenses"]], ["security", "tests"])
        with self.assertRaises(ValueError):
            self.run_review(["claude", "codex"], lenses=["style"])

    def test_default_output_lives_in_the_git_directory(self):
        cfg = self.env.config(["claude", "codex"])
        code, summary = review.run(cfg, CATALOG, self.repo, lenses=["security"])
        self.assertIn(os.path.join(".git", "crossvendor-review"), summary["run_dir"])
        _, again = review.run(cfg, CATALOG, self.repo, lenses=["security"], dry_run=True)
        self.assertNotEqual(again["run_dir"], summary["run_dir"])  # back-to-back runs do not collide


class AdviseRunTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_answers_and_diversity(self):
        cfg = self.env.config(["claude", "codex", "grok"])
        write(os.path.join(self.env.tmp, "design.md"), "Queue in API or worker?\n")
        code, summary = advise.run(cfg, CATALOG, "Where should the queue live?", files=["design.md"],
                                   base_dir=self.env.tmp, out_dir=os.path.join(self.env.tmp, "adv"))
        self.assertEqual(code, 0)
        self.assertEqual(len(summary["advisors"]), 4)  # two Claude models, Codex, Grok
        self.assertEqual(summary["diversity"]["status"], "full")
        with open(os.path.join(summary["run_dir"], "answers.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Recommendation: codex answer", text)
        self.assertIn("(same model as host)", text)

    def test_low_diversity_when_only_host_answers(self):
        os.environ["FAKE_MODE_CODEX"] = "fail"
        cfg = self.env.config(["claude", "codex"], overrides={"claude": {"advisor_models": ["claude-opus-5-5"]}})
        code, summary = advise.run(cfg, CATALOG, "Q?", base_dir=self.env.tmp,
                                   out_dir=os.path.join(self.env.tmp, "adv"))
        self.assertEqual(code, 3)
        self.assertEqual(summary["diversity"]["score"], 0.5)

    def test_diff_in_a_clean_repository_still_asks(self):
        repo = self.env.repo()
        cfg = self.env.config(["claude", "codex"])
        code, summary = advise.run(cfg, CATALOG, "Q?", base_dir=repo, with_diff=True)
        self.assertEqual(code, 0)
        with open(os.path.join(summary["run_dir"], "context.txt"), encoding="utf-8") as fh:
            self.assertIn("no change to show: the working tree matches the last commit", fh.read())
        code, summary = advise.run(cfg, CATALOG, "Q?", base_dir=repo, with_diff=True, base="main")
        with open(os.path.join(summary["run_dir"], "context.txt"), encoding="utf-8") as fh:
            self.assertIn("matches the merge base with main", fh.read())

    def test_empty_question_is_rejected(self):
        cfg = self.env.config(["claude", "codex"])
        with self.assertRaises(ValueError):
            advise.run(cfg, CATALOG, "  ", base_dir=self.env.tmp)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_setup_plan_doctor(self):
        code, out, err = self.cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("run setup first", err)
        code, out, _ = self.cli("setup", "--vendors", "claude,codex,glm,grok", "--pin", "security=grok")
        self.assertEqual(code, 0)
        self.assertIn("| security (pinned) | grok |", out)
        code, out, _ = self.cli("plan", "--json")
        self.assertEqual(json.loads(out)["selected"], ["claude", "codex", "glm", "grok"])
        code, out, _ = self.cli("doctor", "--json")
        states = {r["vendor"]: r["state"] for r in json.loads(out)}
        self.assertEqual(states, {"claude": "READY", "codex": "READY", "glm": "READY", "grok": "INSTALLED"})
        self.assertEqual(code, 0)
        os.environ["FAKE_LOGGED_OUT"] = "codex"
        code, out, _ = self.cli("doctor")
        self.assertEqual(code, 3)
        self.assertIn("codex login", out)
        code, out, _ = self.cli("doctor", "--live", "--json")
        grok = {r["vendor"]: r for r in json.loads(out)}["grok"]
        self.assertEqual((grok["state"], grok["fix"]), ("READY", ""))

    def test_setup_rejects_bad_pin_and_keeps_overrides(self):
        code, _, err = self.cli("setup", "--vendors", "claude,codex", "--pin", "security")
        self.assertEqual(code, 2)
        self.assertIn("lens=vendor", err)
        self.env.config(["claude", "codex"], overrides={"codex": {"review_model": "gpt-x"}}, timeout_s=77)
        code, out, _ = self.cli("setup", "--vendors", "claude,codex,grok")
        self.assertEqual(code, 0)
        with open(os.environ["CVR_CONFIG"], encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["overrides"], saved["timeout_s"]), ({"codex": {"review_model": "gpt-x"}}, 77))
        self.cli("setup", "--vendors", "claude,codex,grok", "--pin", "security=grok", "--pin", "tests=codex")
        self.cli("setup", "--vendors", "claude,codex")  # no --pin: earlier pins stay unless their vendor is gone
        with open(os.environ["CVR_CONFIG"], encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["pins"], {"tests": "codex"})
        self.cli("setup", "--vendors", "claude", "--pin", "security=claude")  # valid while single-vendor
        code, _, err = self.cli("setup", "--vendors", "claude,codex")  # adding a vendor drops the host pin
        self.assertEqual(code, 0, err)
        with open(os.environ["CVR_CONFIG"], encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["pins"], {})

    def test_setup_keeps_a_non_default_host(self):
        self.cli("setup", "--vendors", "claude,codex", "--host", "codex", "--host-model", "gpt-6.1-sol",
                 "--pin", "security=claude")
        code, _, err = self.cli("setup", "--vendors", "claude,codex,grok")  # no --host: codex stays the host
        self.assertEqual(code, 0, err)
        with open(os.environ["CVR_CONFIG"], encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["host"], saved["host_model"], saved["pins"]),
                         ("codex", "gpt-6.1-sol", {"security": "claude"}))
        self.cli("setup", "--vendors", "claude,codex", "--host", "grok")
        with open(os.environ["CVR_CONFIG"], encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["host"], saved["host_model"]), ("grok", "grok-4.7"))  # a new host takes its model

    def test_timeout_must_be_positive(self):
        self.cli("setup", "--vendors", "claude,codex")
        with self.assertRaises(SystemExit) as ctx:
            self.cli("review", "--timeout", "0")
        self.assertEqual(ctx.exception.code, 2)

    def test_review_and_advise_commands(self):
        repo = self.env.repo()
        write(os.path.join(repo, "app.py"), "x = 1\n")
        self.cli("setup", "--vendors", "claude,codex")
        code, out, _ = self.cli("review", "--repo", repo, "--lens", "security")
        self.assertEqual(code, 0)
        self.assertIn("report.md", out)
        qfile = os.path.join(self.env.tmp, "q.txt")
        write(qfile, "What now?\n")
        code, out, _ = self.cli("advise", "--question-file", qfile, "--repo", self.env.tmp)
        self.assertEqual(code, 0)
        self.assertIn("Diversity: 2.0", out)
        code, _, err = self.cli("review", "--repo", self.env.tmp)
        self.assertEqual(code, 2)
        self.assertIn("not inside a git repository", err)


if __name__ == "__main__":
    unittest.main()
