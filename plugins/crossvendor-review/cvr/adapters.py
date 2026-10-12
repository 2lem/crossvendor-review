"""One call to one vendor: send exactly the prompt given, with no tools, and return a receipt.

Every adapter runs the vendor without tools, web access or the user's own instruction files where the CLI allows
it, in an empty temporary working directory outside the repository, and records which model actually answered:
  claude  `claude -p --safe-mode`, no tools, no MCP servers; the model is read from the result envelope.
  codex   `codex exec` in its read-only sandbox without the user config file, with shell, web, plugins and similar
          features off; on macOS the global instruction files are also made unreadable with sandbox-exec. The model
          and effort are read from the CLI banner.
  grok    `grok` with no tools, one turn and a neutral system prompt.
  muse    `muse exec` with web, shell and write tools off.
  glm     HTTPS request to the Z.ai Anthropic-compatible endpoint with your own API key (never logged, never sent
          to a redirect target).
Receipt status: ok, empty, error, timeout, model-mismatch, refused, truncated, auth, quota, missing. Only "ok"
carries an answer; a call that returned nothing usable is never treated as a clean result.
"""

import json
import os
import re
import shutil
import socket
import sys
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import proc

CLAUDE_DISALLOWED = "Bash,WebFetch,WebSearch,Read,Write,Edit,NotebookEdit,Task,Glob,Grep"
CODEX_FEATURES_OFF = (
    "shell_tool", "unified_exec", "code_mode", "multi_agent", "memories", "apps", "plugins", "hooks",
    "computer_use", "browser_use", "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
    "in_app_local_automation", "remote_plugin",
)
CODEX_INSTRUCTION_FILES = ("AGENTS.md", "AGENTS.override.md")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
GROK_DENIED_TOOLS = ("run_terminal_command,read_file,search_replace,list_dir,grep,write,todo_write,spawn_subagent,"
                     "kill_command_or_subagent,get_command_or_subagent_output,scheduler_create,scheduler_delete,"
                     "scheduler_list,monitor,search_tool,use_tool,workflow,enter_plan_mode,exit_plan_mode,"
                     "ask_user_question,image_gen,image_edit,image_to_video,reference_to_video,x_user_search,"
                     "x_semantic_search,x_keyword_search,x_thread_fetch")
GROK_SYSTEM = ("You have no tools, no filesystem and no repository access: the only material you will ever see is the "
               "text of the user message. Never attempt to call a tool or read a file. Respond in a single turn.")
GLM_MAX_TOKENS = 65536
UNDOUBLE_MIN_HALF = 200
QUOTA_RE = re.compile(r"rate[ _-]?limit|too many requests|usage limit|quota|(?<![\w-])429(?![\w-])", re.I)
AUTH_RE = re.compile(r"not logged in|unauthori[sz]ed|authenticat|please log ?in|(?<![\w-])401(?![\w-])", re.I)


def receipt(vendor, model, effort, **fields):
    r = {"vendor": vendor, "model": model, "effort": effort, "observed_model": None, "status": None, "rc": None,
         "timed_out": False, "wall_s": None, "detail": "", "text": ""}
    r.update(fields)
    return r


def classify_failure(stderr_text):
    if QUOTA_RE.search(stderr_text):
        return "quota"
    if AUTH_RE.search(stderr_text):
        return "auth"
    return "error"


def _write_prompt(leg_dir, prompt):
    pin = os.path.join(leg_dir, "prompt.txt")
    with open(pin, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    return pin


def _tail(text, n=400):
    return text.strip()[-n:]


def _settle(r, stderr_text):
    """Fill status from rc/timeout when the parser did not decide."""
    if r["timed_out"]:
        r["status"] = "timeout"
    elif r["rc"] is None and r["status"] is None:
        r["status"] = "missing"
    elif r["status"] is None and r["rc"] != 0:
        r["status"] = classify_failure(stderr_text)
    if r["status"] not in (None, "ok") and not r["detail"]:
        r["detail"] = _tail(stderr_text)
    if r["status"] is None:
        r["status"] = "ok" if r["text"].strip() else "empty"
    if r["status"] != "ok":
        r["text"] = ""
    return r


# ------------------------------------------------------------------ claude

def claude_argv(binary, model, effort):
    # --setting-sources project,local keeps the user's own settings (such as an answer language) out of the call;
    # the working directory is an empty temporary directory, so project and local sources load nothing.
    return [binary, "-p", "--safe-mode", "--setting-sources", "project,local", "--tools", "",
            "--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence", "--model", model,
            "--effort", effort, "--disallowed-tools", CLAUDE_DISALLOWED, "--output-format", "json"]


def claude_env(model):
    env = dict(os.environ)
    env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] = "1"  # a refusal must surface, not be answered by another model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model       # helper calls on the same model as the call itself
    env["ANTHROPIC_SMALL_FAST_MODEL"] = model
    return env


def parse_claude(raw, model):
    """(status or None, text, observed_model) from the --output-format json envelope."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return "error", "", None
    if not isinstance(obj, dict):
        return "error", "", None
    usage = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}
    observed = ",".join(sorted(usage)) or None
    if str(obj.get("stop_reason") or "") == "refusal":
        return "refused", "", observed
    if obj.get("is_error"):
        return "error", "", observed
    if sorted(usage) != [model]:
        return "model-mismatch", "", observed
    text = obj.get("result")
    return None, text if isinstance(text, str) else "", observed


def run_claude(settings, model, effort, prompt, leg_dir, work, timeout):
    pin = _write_prompt(leg_dir, prompt)
    out, err = os.path.join(leg_dir, "stdout.json"), os.path.join(leg_dir, "stderr.txt")
    binary = shutil.which(settings["binary"])
    r = receipt("claude", model, effort)
    if not binary:
        r.update(status="missing", detail="%s not found on PATH" % settings["binary"])
        return r
    rc, timed_out, wall = proc.run(claude_argv(binary, model, effort), work, pin, out, err, timeout,
                                   env=claude_env(model))
    r.update(rc=rc, timed_out=timed_out, wall_s=round(wall, 2))
    raw = proc.read(out)
    if not timed_out and rc == 0:
        status, text, observed = parse_claude(raw, model)
        r.update(status=status, text=text, observed_model=observed)
    return _settle(r, proc.read(err) + "\n" + raw[-2000:])


# ------------------------------------------------------------------ codex

def codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def codex_auth_store(home):
    """The credential store named in config.toml, so that skipping the user config keeps the same login."""
    path = os.path.join(home, "config.toml")
    if not os.path.exists(path):
        return "file"
    try:
        import tomllib
    except ImportError:  # Python < 3.11: fall back to a plain line match
        tomllib = None
    try:
        if tomllib:
            with open(path, "rb") as fh:
                value = tomllib.load(fh).get("cli_auth_credentials_store", "file")
        else:
            m = re.search(r'(?m)^\s*cli_auth_credentials_store\s*=\s*"([a-z]+)"', proc.read(path))
            value = m.group(1) if m else "file"
    except (OSError, ValueError):
        return "file"
    return value if value in ("file", "keyring", "auto") else "file"


def codex_deny_profile(home):
    paths = set()
    for name in CODEX_INSTRUCTION_FILES:
        p = os.path.join(home, name)
        paths.add(os.path.abspath(p))
        paths.add(os.path.realpath(p))
    rules = " ".join("(literal %s)" % json.dumps(p) for p in sorted(paths))
    return "(version 1)(allow default)(deny file-read* %s)" % rules


def codex_argv(binary, model, effort, workdir, auth_store, home, isolate):
    opts = ["--ignore-user-config", "-c", "project_doc_max_bytes=0", "-c", 'developer_instructions=""',
            "-c", "cli_auth_credentials_store=" + auth_store, "-c", "skills.include_instructions=false",
            "-c", "skills.bundled.enabled=false", "-c", 'web_search="disabled"']
    for feature in CODEX_FEATURES_OFF:
        opts += ["-c", "features.%s=false" % feature]
    argv = [binary, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--cd", workdir] + opts + [
        "--model", model, "-c", "model_reasoning_effort=%s" % effort, "-"]
    if isolate:
        argv = [SANDBOX_EXEC, "-p", codex_deny_profile(home)] + argv
    return argv


BANNER_MODEL = re.compile(r"^model:\s*(\S+)\s*$", re.M)
BANNER_EFFORT = re.compile(r"^reasoning effort:\s*(\S+)\s*$", re.M)


def codex_banner(stderr_text):
    model = BANNER_MODEL.findall(stderr_text)
    effort = BANNER_EFFORT.findall(stderr_text)
    return (model[0] if len(model) == 1 else None), (effort[0] if len(effort) == 1 else None)


def run_codex(settings, model, effort, prompt, leg_dir, work, timeout):
    pin = _write_prompt(leg_dir, prompt)
    out, err = os.path.join(leg_dir, "stdout.txt"), os.path.join(leg_dir, "stderr.txt")
    binary = shutil.which(settings["binary"])
    r = receipt("codex", model, effort, isolation="none")
    if not binary:
        r.update(status="missing", detail="%s not found on PATH" % settings["binary"])
        return r
    home = codex_home()
    isolate = sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC) and os.environ.get("CVR_CODEX_ISOLATION",
                                                                                         "auto") != "off"
    argv = codex_argv(binary, model, effort, work, codex_auth_store(home), home, isolate)
    rc, timed_out, wall = proc.run(argv, work, pin, out, err, timeout)
    if isolate and rc not in (0, None) and "sandbox_apply" in proc.read(err):
        # sandbox-exec cannot nest inside another sandbox (for example a sandboxed agent shell): run without it
        isolate = False
        argv = codex_argv(binary, model, effort, work, codex_auth_store(home), home, False)
        rc, timed_out, wall = proc.run(argv, work, pin, out, err, timeout)
    r.update(rc=rc, timed_out=timed_out, wall_s=round(wall, 2),
             isolation="global-instructions-blocked" if isolate else "none")
    stderr_text = proc.read(err)
    if not timed_out and rc == 0:
        observed_model, observed_effort = codex_banner(stderr_text)
        r["observed_model"] = observed_model
        if observed_model != model or observed_effort != effort:
            r.update(status="model-mismatch", detail="banner model=%s effort=%s" % (observed_model, observed_effort))
        else:
            r["text"] = proc.read(out)
    return _settle(r, stderr_text)


# ------------------------------------------------------------------ grok

def grok_argv(binary, prompt_path, model, effort):
    # An empty built-in tool list and the deny list together; with the deny list alone the model still asked for a
    # tool when one was in reach.
    return [binary, "--prompt-file", prompt_path, "--verbatim", "-m", model, "--output-format", "json",
            "--reasoning-effort", effort, "--system-prompt-override", GROK_SYSTEM, "--max-turns", "1",
            "--tools", "", "--disallowed-tools", GROK_DENIED_TOOLS, "--no-subagents", "--disable-web-search",
            "--no-plan"]


def parse_grok(raw, model):
    """(status or None, text, observed_model). The model key may carry a -build suffix."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return "error", "", None
    if not isinstance(obj, dict):
        return "error", "", None
    usage = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}
    keys = sorted(usage)
    observed = ",".join(keys) or None
    if len(keys) != 1 or keys[0] not in (model, model + "-build"):
        return "model-mismatch", "", observed
    if str(obj.get("stopReason") or "").lower() == "cancelled":
        return "error", "", observed
    text = obj.get("text") if isinstance(obj.get("text"), str) else ""
    return None, text, observed


def run_grok(settings, model, effort, prompt, leg_dir, work, timeout):
    pin = _write_prompt(leg_dir, prompt)
    out, err = os.path.join(leg_dir, "stdout.json"), os.path.join(leg_dir, "stderr.txt")
    binary = shutil.which(settings["binary"])
    r = receipt("grok", model, effort)
    if not binary:
        r.update(status="missing", detail="%s not found on PATH" % settings["binary"])
        return r
    rc, timed_out, wall = proc.run(grok_argv(binary, pin, model, effort), work, None, out, err, timeout)
    r.update(rc=rc, timed_out=timed_out, wall_s=round(wall, 2))
    if not timed_out and rc == 0:
        status, text, observed = parse_grok(proc.read(out), model)
        r.update(status=status, text=text, observed_model=observed)
    return _settle(r, proc.read(err))


# ------------------------------------------------------------------ muse

def muse_argv(binary, model, effort, prompt_path, workspace):
    return [binary, "exec", "--provider", "meta", "--model", model, "--reasoning-effort", effort, "--json",
            "--no-session-log", "--prompt-file", prompt_path, "--workspace", workspace, "--worktree", "off",
            "--approval-mode", "never", "--disable-web-tools", "--no-foreign-personal-context",
            "--disable-write", "--disable-shell"]


def parse_muse(raw):
    """(text, model_ids, completed, failed) from the muse --json event stream."""
    body, models, completed, failed = [], [], 0, 0
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("payload_type") or ""
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if kind == "run.output.delta" and isinstance(payload.get("text"), str):
            body.append(payload["text"])
        elif kind == "run.model.configured":
            models.append(payload.get("model_id") if isinstance(payload.get("model_id"), str) else "")
        elif kind == "run.terminal.completed":
            completed += 1
        elif kind == "run.terminal.failed":
            failed += 1
    return undouble("".join(body)), models, completed, failed


def undouble(text):
    """The stream has been seen to carry the whole answer twice, byte for byte; keep one copy. Short texts are left
    alone, since a short answer can legitimately repeat itself."""
    n = len(text)
    if n >= 2 * UNDOUBLE_MIN_HALF and n % 2 == 0 and text[:n // 2] == text[n // 2:]:
        return text[:n // 2]
    return text


def run_muse(settings, model, effort, prompt, leg_dir, work, timeout):
    pin = _write_prompt(leg_dir, prompt)
    out, err = os.path.join(leg_dir, "stdout.jsonl"), os.path.join(leg_dir, "stderr.txt")
    binary = shutil.which(settings["binary"])
    r = receipt("muse", model, effort)
    if not binary:
        r.update(status="missing", detail="%s not found on PATH" % settings["binary"])
        return r
    env = dict(os.environ)
    env["MUSE_NO_AUTO_UPDATE"] = "1"
    rc, timed_out, wall = proc.run(muse_argv(binary, model, effort, pin, work), work, None, out, err, timeout,
                                   env=env)
    r.update(rc=rc, timed_out=timed_out, wall_s=round(wall, 2))
    if not timed_out and rc == 0:
        text, models, completed, failed = parse_muse(proc.read(out))
        r["observed_model"] = ",".join(models) or None
        if failed or not completed:
            r["status"] = "error"
        elif models != [model]:
            r["status"] = "model-mismatch"
        else:
            r["text"] = text
    return _settle(r, proc.read(err))


# ------------------------------------------------------------------ glm

class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the default handler would resend the Authorization header to the new location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def glm_endpoint(settings):
    endpoint = os.environ.get("CVR_GLM_ENDPOINT") or settings["endpoint"]
    m = re.match(r"^(https?)://([^/:]+)", endpoint)
    if not m:
        raise ValueError("bad GLM endpoint")
    if m.group(1) != "https" and m.group(2) not in ("127.0.0.1", "localhost"):
        raise ValueError("the GLM endpoint must use https")
    return endpoint.rstrip("/") + "/v1/messages"


def parse_glm(raw, model):
    """(status or None, text, observed_model) from an Anthropic-style messages response."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return "error", "", None
    if not isinstance(obj, dict):
        return "error", "", None
    observed = obj.get("model") if isinstance(obj.get("model"), str) else None
    if obj.get("stop_reason") == "max_tokens":
        return "truncated", "", observed
    if observed != model:
        return "model-mismatch", "", observed
    blocks = obj.get("content") if isinstance(obj.get("content"), list) else []
    text = "".join(b.get("text", "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return None, text, observed


def glm_key_problem(settings):
    """None when the key may be sent, else (status, detail)."""
    key = os.environ.get(settings["key_env"], "").strip()
    if not key:
        return "missing", "%s is not set" % settings["key_env"]
    if key.startswith("sk-ant-"):
        return "auth", "%s holds an Anthropic key; it is never sent to another vendor" % settings["key_env"]
    return None


def _http_post(opener, req, timeout):
    """(code, body) for any HTTP answer, error statuses included; network failures raise."""
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                body = exc.read() if exc.fp else b""
            except OSError:
                body = b""
            return exc.code, body


def run_glm(settings, model, effort, prompt, leg_dir, work, timeout):
    _write_prompt(leg_dir, prompt)
    r = receipt("glm", model, effort)
    problem = glm_key_problem(settings)
    if problem:
        r.update(status=problem[0], detail=problem[1])
        return r
    try:
        url = glm_endpoint(settings)
    except ValueError as exc:
        r.update(status="error", detail=str(exc))
        return r
    key = os.environ[settings["key_env"]].strip()
    body = json.dumps({"model": model, "max_tokens": GLM_MAX_TOKENS,
                       "messages": [{"role": "user", "content": prompt}],
                       "output_config": {"effort": effort}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "content-type": "application/json", "anthropic-version": "2023-06-01", "authorization": "Bearer " + key})
    opener = urllib.request.build_opener(_RefuseRedirect)
    # The socket timeout only bounds each read; the whole exchange runs in a worker so the call as a whole stops
    # at `timeout` even when a response trickles in. A worker still running at the deadline is abandoned.
    box = {}

    def worker():
        try:
            box["result"] = _http_post(opener, req, timeout)
        except Exception as exc:  # reported in the receipt below
            box["error"] = exc

    start = time.monotonic()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    wall = round(time.monotonic() - start, 2)
    if thread.is_alive():
        r.update(status="timeout", timed_out=True, detail="no complete answer within %d s" % timeout, wall_s=wall)
        return r
    if "error" in box:
        exc = box["error"]
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower()
        r.update(status="timeout" if timed_out else "error", timed_out=timed_out, detail=str(exc)[:300], wall_s=wall)
        return r
    code, resp_body = box["result"]
    raw = resp_body.decode("utf-8", "replace")
    r.update(rc=code, wall_s=wall)
    with open(os.path.join(leg_dir, "response.json"), "w", encoding="utf-8") as fh:
        fh.write(raw)
    if code in (401, 403):
        r.update(status="auth", detail="HTTP %d" % code)
    elif code == 429:
        r.update(status="quota", detail="HTTP 429")
    elif code in (301, 302, 303, 307, 308):
        r.update(status="error", detail="HTTP %d redirect refused (the key is never sent to another location)" %
                 code)
    elif code != 200:
        r.update(status="error", detail="HTTP %s %s" % (code, raw[:300]))
    else:
        status, text, observed = parse_glm(raw, model)
        r.update(status=status, text=text, observed_model=observed)
    if r["status"] is None:
        r["status"] = "ok" if r["text"].strip() else "empty"
    if r["status"] != "ok":
        r["text"] = ""
    return r


RUNNERS = {"claude": run_claude, "codex": run_codex, "grok": run_grok, "muse": run_muse, "glm": run_glm}


def _inside_work_tree(path):
    try:
        p = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, capture_output=True, text=True)
    except OSError:  # no git on PATH: nothing to detect, and no work tree for it to find either
        return False
    return p.returncode == 0 and p.stdout.strip() == "true"


FALLBACK_TMP_DIRS = ("/tmp", "/var/tmp")


def work_dir():
    """A fresh empty directory that is not inside any git work tree (TMPDIR may point into a repository)."""
    for parent in (None,) + FALLBACK_TMP_DIRS:
        try:
            work = tempfile.mkdtemp(prefix="cvr-work-", dir=parent)
        except OSError:
            continue
        if not _inside_work_tree(work):
            return work
        shutil.rmtree(work, ignore_errors=True)
    raise OSError("no temporary directory outside a git work tree is available")


def call(vendor, settings, model, effort, prompt, leg_dir, timeout):
    """One call; the receipt (with the answer in r["text"]) is also written to leg_dir/receipt.json.

    The vendor runs in a fresh empty directory outside the repository, so no project file or setting of the code
    under review is in reach; it is removed afterwards. Artifacts stay in leg_dir.
    """
    leg_dir = os.path.abspath(leg_dir)
    os.makedirs(leg_dir, exist_ok=True)
    work = work_dir()
    try:
        r = RUNNERS[vendor](settings, model, effort, prompt, leg_dir, work, timeout)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    with open(os.path.join(leg_dir, "answer.txt"), "w", encoding="utf-8") as fh:
        fh.write(r["text"])
    with open(os.path.join(leg_dir, "receipt.json"), "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in r.items() if k != "text"}, fh, indent=2, sort_keys=True)
    return r
