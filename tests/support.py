"""Test helpers: stand-in vendor programs, a local GLM server and temporary repositories. No network."""

import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "crossvendor-review")
sys.path.insert(0, PLUGIN)

from cvr import catalog as catalog_mod  # noqa: E402
from cvr import config as config_mod  # noqa: E402

CATALOG = catalog_mod.load()

# One stand-in program serves every CLI vendor. Its behaviour comes from FAKE_MODE_<VENDOR> (ok, fail, quota, empty,
# empty-findings, invalid-findings, odd-severity, mismatch, garbage); FAKE_LOGGED_OUT=<vendor> makes its login check
# fail. Every call is appended to FAKE_LOG.
FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys
vendor = os.path.basename(sys.argv[0])
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE_" + vendor.upper(), "ok")
def arg(flag):
    return args[args.index(flag) + 1] if flag in args else None
model = arg("--model") or arg("-m")
effort = arg("--effort") or arg("--reasoning-effort")
for a in args:
    if a.startswith("model_reasoning_effort="):
        effort = a.split("=", 1)[1]
prompt_file = arg("--prompt-file")
prompt = open(prompt_file, encoding="utf-8").read() if prompt_file else sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"vendor": vendor, "model": model, "effort": effort, "args": args, "cwd": os.getcwd(),
                         "cwd_entries": os.listdir(".")}) + "\n")
if args[:1] == ["--version"]:
    print(vendor + " 9.9.9"); sys.exit(0)
if args[:2] in (["auth", "status"], ["login", "status"]):
    sys.exit(0 if os.environ.get("FAKE_LOGGED_OUT") != vendor else 1)
if mode == "fail":
    sys.stderr.write("internal error\n"); sys.exit(1)
if mode == "quota":
    sys.stderr.write("Error: rate limit reached, try again later\n"); sys.exit(1)
m = re.search(r"--- FILE (\S+) ", prompt)
if "Lens:" in prompt:
    lens = re.search(r"Lens: ([a-z -]+)\.", prompt).group(1)
    answer = json.dumps({"findings": [] if mode == "empty-findings" else [
        {"file": m.group(1) if m else "x.py", "line": 0 if mode == "invalid-findings" else 2,
         "severity": ["odd"] if mode == "odd-severity" else "important",
         "title": "%s says %s" % (vendor, lens), "reasoning": "stand-in", "confidence": 70}]})
else:
    answer = "Recommendation: %s answer" % vendor
if mode == "empty":
    answer = ""
if mode == "garbage":
    answer = "I could not decide."
reported = "other-model" if mode == "mismatch" else model
if vendor == "claude":
    print(json.dumps({"result": answer, "modelUsage": {reported: {}}, "is_error": False}))
elif vendor == "codex":
    sys.stderr.write("model: %s\nreasoning effort: %s\n" % (reported, effort))
    print(answer)
elif vendor == "grok":
    print(json.dumps({"text": answer, "modelUsage": {reported: {}}}))
elif vendor == "muse":
    for ev in ({"payload_type": "run.model.configured", "payload": {"model_id": reported}},
               {"payload_type": "run.output.delta", "payload": {"text": answer}},
               {"payload_type": "run.terminal.completed", "payload": {}}):
        print(json.dumps(ev))
'''


class GlmHandler(http.server.BaseHTTPRequestHandler):
    mode = "ok"
    calls = []
    redirect_to = ""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])).decode("utf-8"))
        GlmHandler.calls.append({"auth": self.headers.get("authorization"), "body": body})
        prompt = body["messages"][0]["content"]
        if GlmHandler.mode == "unauthorized":
            return self._send(401, {"error": "bad key"})
        if GlmHandler.mode == "quota":
            return self._send(429, {"error": "slow down"})
        if GlmHandler.mode in ("trickle", "stalled-error"):
            import time
            self.send_response(200 if GlmHandler.mode == "trickle" else 429)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", "100000")
            self.end_headers()
            for _ in range(100):  # one byte at a time, well inside any per-read timeout
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except OSError:
                    return None
                time.sleep(0.2)
            return None
        if GlmHandler.mode == "redirect":
            self.send_response(302)  # a POST is followed after 302 by the default handler, headers included
            self.send_header("location", GlmHandler.redirect_to)
            self.send_header("content-length", "0")
            self.end_headers()
            return None
        if "Lens:" in prompt:
            import re
            m = re.search(r"--- FILE (\S+) ", prompt)
            lens = re.search(r"Lens: ([a-z -]+)\.", prompt).group(1)
            text = json.dumps({"findings": [{"file": m.group(1) if m else "x.py", "line": 2,
                                             "severity": "minor", "title": "glm says " + lens}]})
        else:
            text = "Recommendation: glm answer"
        content = [{"type": "thinking", "thinking": "..."}]
        if GlmHandler.mode != "thinking-only":
            content.append({"type": "text", "text": text})
        stop = "max_tokens" if GlmHandler.mode == "truncated" else "end_turn"
        self._send(200, {"model": body["model"], "content": content, "stop_reason": stop})

    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Env:
    """Temporary home for one test: fake vendor binaries on PATH, a config file, a GLM server, a git repo."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="cvr-test-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name in ("claude", "codex", "grok", "muse"):
            path = os.path.join(self.bin, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(FAKE)
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.log = os.path.join(self.tmp, "calls.jsonl")
        self.saved = dict(os.environ)
        os.environ["PATH"] = self.bin + os.pathsep + os.environ.get("PATH", "")
        os.environ["FAKE_LOG"] = self.log
        os.environ["CVR_CONFIG"] = os.path.join(self.tmp, "config.json")
        os.environ["CVR_CODEX_ISOLATION"] = "off"
        os.environ["ZAI_API_KEY"] = "test-key-123"
        for key in list(os.environ):
            if key.startswith("FAKE_MODE_") or key == "FAKE_LOGGED_OUT":
                del os.environ[key]
        GlmHandler.mode = "ok"
        GlmHandler.calls = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GlmHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["CVR_GLM_ENDPOINT"] = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.clear()
        os.environ.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, vendors, **extra):
        cfg = config_mod.build(vendors, CATALOG, pins=extra.pop("pins", None), overrides=extra.pop("overrides", None))
        cfg.update(extra)
        config_mod.save(cfg)
        return cfg

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def repo(self, files=None, commit=True):
        path = os.path.join(self.tmp, "repo-%d" % len(os.listdir(self.tmp)))
        os.makedirs(path)
        run_git(path, "init", "-q", "-b", "main")
        for name, text in (files or {"app.py": "def add(a, b):\n    return a + b\n"}).items():
            write(os.path.join(path, name), text)
        if commit:
            run_git(path, "add", "-A")
            run_git(path, "commit", "-q", "-m", "init")
        return path


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(text, bytes) else "w"
    with open(path, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as fh:
        fh.write(text)


def run_git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git"] + list(args), cwd=repo, env=env, check=True, capture_output=True,
                          text=True).stdout
