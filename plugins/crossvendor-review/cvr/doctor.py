"""Readiness check for the selected vendors: installed, logged in, key present. Never prints a credential."""

import os
import shutil
import subprocess
import tempfile

from . import adapters, config as config_mod


def check(vendor, settings, timeout=30):
    """Return {"vendor", "state", "detail", "fix"} with state READY, INSTALLED, NEEDS-LOGIN or MISSING."""
    row = {"vendor": vendor, "state": None, "detail": "", "fix": ""}
    if settings["transport"] == "http":
        problem = adapters.glm_key_problem(settings)
        if problem is None:
            row.update(state="READY", detail="%s is set" % settings["key_env"])
        else:
            row.update(state="MISSING" if problem[0] == "missing" else "NEEDS-LOGIN", detail=problem[1],
                       fix=settings["login_hint"])
        return row
    binary = shutil.which(settings["binary"])
    if not binary:
        row.update(state="MISSING", detail="%s not found on PATH" % settings["binary"],
                   fix="install the %s CLI so that `%s` is on PATH" % (settings["display"], settings["binary"]))
        return row
    version = _run([binary, "--version"], timeout)
    row["detail"] = (version[1].strip().splitlines() or [""])[0][:80]
    if not settings.get("login_check"):
        row.update(state="INSTALLED", fix="login is not checked offline; use --live to make one test call")
        return row
    argv = [binary] + list(settings["login_check"][1:])
    rc, _ = _run(argv, timeout)
    if rc == 0:
        row["state"] = "READY"
    else:
        row.update(state="NEEDS-LOGIN", fix=settings["login_hint"])
    return row


def _run(argv, timeout):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    return p.returncode, p.stdout or p.stderr


def live(vendor, settings, prompt, timeout):
    """One short call through the real adapter; costs one request."""
    with tempfile.TemporaryDirectory(prefix="cvr-doctor-") as tmp:
        r = adapters.call(vendor, settings, settings["review_model"], settings["review_effort"], prompt,
                          os.path.join(tmp, vendor), timeout)
    return {"vendor": vendor, "status": r["status"], "observed_model": r["observed_model"],
            "detail": r["detail"][:200]}


def run(cfg, catalog, with_live=False, timeout=300):
    rows = []
    for vendor in cfg["vendors"]:
        settings = config_mod.vendor_settings(cfg, catalog, vendor)
        row = check(vendor, settings)
        if with_live and row["state"] in ("READY", "INSTALLED"):
            result = live(vendor, settings, catalog["prompts"]["probe"], timeout)
            row["live"] = result
            if result["status"] == "ok":
                row.update(state="READY", fix="")
            elif result["status"] == "auth":
                row.update(state="NEEDS-LOGIN", fix=settings["login_hint"])
            else:
                row["state"] = "FAILED"
                row["fix"] = "test call status %s: %s" % (result["status"], result["detail"])
        rows.append(row)
    return rows


def render(rows):
    lines = ["| Vendor | State | Detail | Fix |", "|---|---|---|---|"]
    for r in rows:
        detail = r["detail"]
        if "live" in r:
            detail += " (test call: %s, model %s)" % (r["live"]["status"], r["live"]["observed_model"])
        lines.append("| %s | %s | %s | %s |" % (r["vendor"], r["state"], detail, r["fix"]))
    return "\n".join(lines)
