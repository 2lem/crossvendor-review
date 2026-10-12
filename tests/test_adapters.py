import json
import os
import sys
import unittest

from support import CATALOG, Env, GlmHandler
from cvr import adapters, config as config_mod


def settings(vendor, cfg=None):
    cfg = cfg or config_mod.build([vendor], CATALOG)
    return config_mod.vendor_settings(cfg, CATALOG, vendor)


class ParserTests(unittest.TestCase):
    def test_claude_envelope(self):
        ok = json.dumps({"result": "hi", "modelUsage": {"m": {}}})
        self.assertEqual(adapters.parse_claude(ok, "m"), (None, "hi", "m"))
        self.assertEqual(adapters.parse_claude(ok, "n")[0], "model-mismatch")
        two = json.dumps({"result": "hi", "modelUsage": {"m": {}, "small": {}}})
        self.assertEqual(adapters.parse_claude(two, "m")[0], "model-mismatch")
        self.assertEqual(adapters.parse_claude(json.dumps({"stop_reason": "refusal"}), "m")[0], "refused")
        self.assertEqual(adapters.parse_claude("not json", "m")[0], "error")

    def test_codex_banner(self):
        self.assertEqual(adapters.codex_banner("x\nmodel: a\nreasoning effort: high\n"), ("a", "high"))
        self.assertEqual(adapters.codex_banner("model: a\nmodel: b\n"), (None, None))

    def test_grok_result(self):
        raw = json.dumps({"text": "t", "modelUsage": {"g-1-build": {}}})
        self.assertEqual(adapters.parse_grok(raw, "g-1"), (None, "t", "g-1-build"))
        self.assertEqual(adapters.parse_grok(json.dumps({"text": "t", "modelUsage": {}}), "g-1")[0],
                         "model-mismatch")

    def test_muse_stream_and_doubled_answer(self):
        long = "x" * adapters.UNDOUBLE_MIN_HALF
        events = [{"payload_type": "run.model.configured", "payload": {"model_id": "m"}},
                  {"payload_type": "run.output.delta", "payload": {"text": long}},
                  {"payload_type": "run.output.delta", "payload": {"text": long}},
                  {"payload_type": "run.terminal.completed", "payload": {}}]
        raw = "\n".join(json.dumps(e) for e in events)
        self.assertEqual(adapters.parse_muse(raw), (long, ["m"], 1, 0))
        self.assertEqual(adapters.undouble("abcabc"), "abcabc")  # a short answer may repeat itself

    def test_glm_response(self):
        body = {"model": "g", "stop_reason": "end_turn",
                "content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "ans"}]}
        self.assertEqual(adapters.parse_glm(json.dumps(body), "g"), (None, "ans", "g"))
        body["stop_reason"] = "max_tokens"
        self.assertEqual(adapters.parse_glm(json.dumps(body), "g")[0], "truncated")

    def test_failure_classes(self):
        self.assertEqual(adapters.classify_failure("HTTP 429 Too Many Requests"), "quota")
        self.assertEqual(adapters.classify_failure("usage limit reached"), "quota")
        self.assertEqual(adapters.classify_failure("request id req-429e-77 failed"), "error")
        self.assertEqual(adapters.classify_failure("Not logged in"), "auth")
        self.assertEqual(adapters.classify_failure("boom"), "error")

    def test_glm_endpoint_must_be_https_unless_loopback(self):
        s = settings("glm")
        os.environ.pop("CVR_GLM_ENDPOINT", None)
        self.assertEqual(adapters.glm_endpoint(s), "https://api.z.ai/api/anthropic/v1/messages")
        os.environ["CVR_GLM_ENDPOINT"] = "http://example.com"
        try:
            with self.assertRaises(ValueError):
                adapters.glm_endpoint(s)
        finally:
            del os.environ["CVR_GLM_ENDPOINT"]


class StandInTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.out = os.path.join(self.env.tmp, "legs")

    def tearDown(self):
        self.env.close()

    def call(self, vendor, prompt="Lens: security. --- FILE a.py (new) ---", model=None, effort=None):
        s = settings(vendor)
        return adapters.call(vendor, s, model or s["review_model"], effort or s["review_effort"], prompt,
                             os.path.join(self.out, vendor + str(len(self.env.calls()))), 60)

    def test_every_vendor_answers(self):
        for vendor in ("claude", "codex", "grok", "muse", "glm"):
            r = self.call(vendor)
            self.assertEqual(r["status"], "ok", (vendor, r))
            self.assertIn('"findings"', r["text"])
            self.assertEqual(r["observed_model"].replace("-build", ""), r["model"])
        logged = {c["vendor"]: c for c in self.env.calls()}
        self.assertIn("--safe-mode", logged["claude"]["args"])
        self.assertEqual(logged["codex"]["args"][-1], "-")  # prompt comes from stdin, not argv
        self.assertIn("--disable-shell", logged["muse"]["args"])
        self.assertIn("--disable-reminders", logged["muse"]["args"])
        self.assertEqual(GlmHandler.calls[0]["auth"], "Bearer test-key-123")
        self.assertEqual(GlmHandler.calls[0]["body"]["max_tokens"], adapters.GLM_MAX_TOKENS)

    def test_receipt_files_hold_no_answer_or_key(self):
        r = self.call("glm")
        leg = os.path.join(self.out, [d for d in os.listdir(self.out)][0])
        with open(os.path.join(leg, "receipt.json"), encoding="utf-8") as fh:
            receipt = json.load(fh)
        self.assertNotIn("text", receipt)
        for name in os.listdir(leg):
            path = os.path.join(leg, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    self.assertNotIn("test-key-123", fh.read())
        self.assertEqual(r["status"], "ok")

    def test_failure_modes(self):
        os.environ["FAKE_MODE_CODEX"] = "quota"
        self.assertEqual(self.call("codex")["status"], "quota")
        os.environ["FAKE_MODE_GROK"] = "mismatch"
        self.assertEqual(self.call("grok")["status"], "model-mismatch")
        os.environ["FAKE_MODE_CLAUDE"] = "empty"
        self.assertEqual(self.call("claude")["status"], "empty")
        os.environ["FAKE_MODE_MUSE"] = "fail"
        r = self.call("muse")
        self.assertEqual((r["status"], r["text"]), ("error", ""))
        for mode, status in (("unauthorized", "auth"), ("quota", "quota"), ("truncated", "truncated"),
                             ("thinking-only", "empty")):
            GlmHandler.mode = mode
            self.assertEqual(self.call("glm")["status"], status, mode)

    def test_missing_binary_and_key(self):
        os.environ["PATH"] = os.path.join(self.env.tmp, "empty")
        self.assertEqual(self.call("codex")["status"], "missing")
        del os.environ["ZAI_API_KEY"]
        self.assertEqual(self.call("glm")["status"], "missing")
        os.environ["ZAI_API_KEY"] = "sk-ant-" + "z" * 30
        r = self.call("glm")
        self.assertEqual(r["status"], "auth")
        self.assertEqual(GlmHandler.calls, [])  # nothing was sent
        from cvr import doctor
        row = doctor.check("glm", settings("glm"))
        self.assertEqual(row["state"], "NEEDS-LOGIN")  # setup's offline check does not call it ready

    def test_glm_redirect_is_refused_and_the_key_stays_home(self):
        import http.server
        import threading
        seen = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(self.headers.get("authorization"))
                self.send_response(200)
                self.end_headers()
            do_POST = do_GET

        sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        threading.Thread(target=sink.serve_forever, daemon=True).start()
        try:
            GlmHandler.mode = "redirect"
            GlmHandler.redirect_to = "http://127.0.0.1:%d/elsewhere" % sink.server_address[1]
            r = self.call("glm")
        finally:
            sink.shutdown()
            sink.server_close()
        self.assertEqual(r["status"], "error")
        self.assertIn("redirect refused", r["detail"])
        self.assertEqual(seen, [])

    def test_glm_call_stops_at_the_total_deadline(self):
        for mode in ("trickle", "stalled-error"):
            GlmHandler.mode = mode
            s = settings("glm")
            r = adapters.call("glm", s, s["review_model"], s["review_effort"], "x",
                              os.path.join(self.out, "slow-" + mode), 2)
            self.assertEqual(r["status"], "timeout", mode)
            self.assertLess(r["wall_s"], 6, mode)

    def test_work_directory_is_never_inside_a_repository(self):
        import tempfile
        repo = self.env.repo()
        inside = os.path.join(repo, "tmp")
        os.makedirs(inside)
        outside = os.path.join(self.env.tmp, "outside")
        os.makedirs(outside)
        old_tempdir, old_fallback = tempfile.tempdir, adapters.FALLBACK_TMP_DIRS
        tempfile.tempdir, adapters.FALLBACK_TMP_DIRS = inside, (outside,)
        try:
            work = adapters.work_dir()
        finally:
            tempfile.tempdir, adapters.FALLBACK_TMP_DIRS = old_tempdir, old_fallback
        self.assertTrue(os.path.realpath(work).startswith(os.path.realpath(outside)))
        self.assertEqual(os.listdir(inside), [])  # the attempt inside the repository was removed

    def test_vendor_runs_outside_the_repository_with_relative_paths(self):
        repo = self.env.repo()
        old = os.getcwd()
        os.chdir(repo)
        try:
            s = settings("grok")
            r = adapters.call("grok", s, s["review_model"], s["review_effort"], "hello", "rel/legs/one", 60)
        finally:
            os.chdir(old)
        self.assertEqual(r["status"], "ok")
        logged = self.env.calls()[-1]
        self.assertFalse(os.path.realpath(logged["cwd"]).startswith(os.path.realpath(repo)))
        self.assertEqual(logged["cwd_entries"], [])
        self.assertFalse(os.path.exists(logged["cwd"]))  # removed after the call
        self.assertTrue(os.path.isfile(os.path.join(repo, "rel", "legs", "one", "receipt.json")))

    def test_timeout_kills_the_call(self):
        slow = os.path.join(self.env.bin, "grok")
        with open(slow, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nsleep 30\n")
        s = settings("grok")
        r = adapters.call("grok", s, s["review_model"], s["review_effort"], "x", os.path.join(self.out, "slow"), 1)
        self.assertEqual(r["status"], "timeout")
        self.assertLess(r["wall_s"], 15)

    @unittest.skipUnless(sys.platform == "darwin", "instruction isolation is macOS only")
    def test_codex_runs_without_isolation_when_it_cannot_nest(self):
        fake = os.path.join(self.env.bin, "fake-sandbox-exec")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho 'sandbox_apply: Operation not permitted' >&2\nexit 71\n")
        os.chmod(fake, 0o755)
        old = adapters.SANDBOX_EXEC
        adapters.SANDBOX_EXEC = fake
        os.environ["CVR_CODEX_ISOLATION"] = "auto"
        try:
            r = self.call("codex")
        finally:
            adapters.SANDBOX_EXEC = old
        self.assertEqual((r["status"], r["isolation"]), ("ok", "none"))


if __name__ == "__main__":
    unittest.main()
