"""Run every lens on its assigned vendor in parallel, collect findings, write a report.

A lens whose call fails is retried once on its fallback vendor. A lens that still has no answer is listed as
missing: the report never calls a run clean while lenses are missing.
"""

import concurrent.futures
import datetime
import json
import math
import os

from . import adapters, assign, packet as packet_mod

NEAR_LINES = 3
SEVERITY_ORDER = {"critical": 0, "important": 1, "minor": 2}


def extract_findings(text):
    """(findings, invalid_count) from the last JSON object in text that has a 'findings' list, or (None, 0)."""
    decoder = json.JSONDecoder()
    found, i = None, 0
    while True:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
            found = obj
        i = end
    if found is None:
        return None, 0
    findings, invalid = [], 0
    for item in found["findings"]:
        line = item.get("line") if isinstance(item, dict) else None
        if isinstance(line, bool) or not isinstance(line, (int, float)) or not math.isfinite(line) \
                or line != int(line) or line < 1 or not isinstance(item.get("file"), str) or not item["file"].strip():
            invalid += 1
            continue
        severity = item.get("severity")
        clean = {"file": item["file"].strip(), "line": int(line),
                 "severity": severity if isinstance(severity, str) and severity in SEVERITY_ORDER else "minor",
                 "title": str(item.get("title") or "").strip()[:300],
                 "reasoning": str(item.get("reasoning") or "").strip()[:4000]}
        conf = item.get("confidence")
        clean["confidence"] = int(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) \
            and 0 <= conf <= 100 else None
        findings.append(clean)
    return findings, invalid


def lens_prompt(prompts, lens_id, packet_text, guidelines=""):
    parts = [prompts["review_preamble"], prompts["lenses"][lens_id], prompts["review_contract"], "", packet_text]
    if guidelines and lens_id == "maintainability":
        parts.append(guidelines)
    return "\n".join(parts)


def group_findings(findings):
    """Assign a group number to findings on the same file within NEAR_LINES lines of each other."""
    ordered = sorted(findings, key=lambda f: (f["file"], f["line"]))
    group, last = 0, None
    for f in ordered:
        if last is None or f["file"] != last["file"] or f["line"] - last["line"] > NEAR_LINES:
            group += 1
        f["group"] = group
        last = f
    return ordered


def run_dir(repo, out_dir=None):
    if out_dir:
        path = out_dir
    else:
        git_dir = packet_mod.git(repo, ["rev-parse", "--absolute-git-dir"]).strip()
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = os.path.join(git_dir, "crossvendor-review", "review-%s-%d" % (stamp, os.getpid()))
    os.makedirs(path, exist_ok=False)
    return path


def _judge(result):
    """Turn an adapter receipt into (receipt, findings, invalid). An answer without a findings object, or whose
    findings were all rejected, is not a usable review."""
    if result["status"] != "ok":
        return result, [], 0
    findings, invalid = extract_findings(result["text"])
    if findings is None:
        return dict(result, status="empty", detail="no findings object in the answer"), [], 0
    if not findings and invalid:
        return dict(result, status="invalid", detail="all %d findings were malformed" % invalid), [], invalid
    return result, findings, invalid


def _leg(row, settings_by_vendor, prompt, out, timeout, call=adapters.call):
    first, findings, invalid = _judge(call(row["vendor"], settings_by_vendor[row["vendor"]], row["model"],
                                           row["effort"], prompt, os.path.join(out, "legs", row["lens"]), timeout))
    attempts = [first]
    result = first
    target = row.get("fallback")
    if first["status"] != "ok" and target and target != row["vendor"]:
        s = settings_by_vendor[target]
        result, findings, invalid = _judge(call(target, s, s["review_model"], s["review_effort"], prompt,
                                                os.path.join(out, "legs", row["lens"] + ".fallback"), timeout))
        attempts.append(result)
    return {"lens": row["lens"], "vendor": result["vendor"], "model": result["model"], "status": result["status"],
            "fallback_used": len(attempts) > 1, "findings": findings, "invalid_findings": invalid,
            "attempts": [{k: v for k, v in a.items() if k != "text"} for a in attempts]}


def run(cfg, catalog, repo, base=None, paths=None, lenses=None, out_dir=None, dry_run=False, call=adapters.call):
    """Return (exit_code, summary dict). Exit 0: every lens answered; 3: some lenses missing."""
    plan = assign.plan(cfg, catalog)
    rows = plan["lenses"]
    if lenses:
        unknown = sorted(set(lenses) - {r["lens"] for r in rows})
        if unknown:
            raise ValueError("unknown lens(es): %s" % ", ".join(unknown))
        rows = [r for r in rows if r["lens"] in set(lenses)]
    text, guidelines, stats = packet_mod.review_packet(repo, base, paths, cfg["max_packet_chars"],
                                                       cfg.get("guideline_files", []))
    out = run_dir(repo, out_dir)
    prompts = catalog["prompts"]
    with open(os.path.join(out, "packet.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + guidelines)
    with open(os.path.join(out, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in plan.items() if k != "settings"}, fh, indent=2, sort_keys=True)
    summary = {"run_dir": out, "mode": plan["mode"], "packet": stats, "lenses": [], "dry_run": dry_run}
    if dry_run:
        for r in rows:
            summary["lenses"].append({"lens": r["lens"], "vendor": r["vendor"], "model": r["model"],
                                      "status": "not-run"})
        _write(out, summary, [])
        return 0, summary
    settings = plan["settings"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(cfg["max_parallel"], len(rows)))) as pool:
        futures = [pool.submit(_leg, r, settings, lens_prompt(prompts, r["lens"], text, guidelines), out,
                               cfg["timeout_s"], call) for r in rows]
        results = [f.result() for f in futures]
    findings = []
    for res in results:
        for f in res["findings"]:
            findings.append(dict(f, lens=res["lens"], vendor=res["vendor"], model=res["model"]))
        summary["lenses"].append({k: v for k, v in res.items() if k != "findings"})
    findings = group_findings(findings)
    _write(out, summary, findings)
    missing = [r for r in summary["lenses"] if r["status"] != "ok"]
    return (3 if missing else 0), summary


def _write(out, summary, findings):
    with open(os.path.join(out, "findings.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "findings": findings}, fh, indent=2, sort_keys=True)
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(render(summary, findings))


def render(summary, findings):
    lines = ["# Cross-vendor review", ""]
    p = summary["packet"]
    lines.append("Files reviewed: %d. Characters sent per lens: %d. Redacted values: %d." % (
        len(p["files"]), p["chars"] + p.get("guideline_chars", 0), p["redactions"]))
    if p["omitted"]:
        lines.append("Not sent: " + "; ".join("%s (%s)" % tuple(o) for o in p["omitted"]))
    lines += ["", "| Lens | Vendor | Model | Status | Findings | Malformed |", "|---|---|---|---|---|---|"]
    counts = {}
    for f in findings:
        counts[f["lens"]] = counts.get(f["lens"], 0) + 1
    for r in summary["lenses"]:
        status = r["status"] + (" (after fallback)" if r.get("fallback_used") else "")
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            r["lens"], r["vendor"], r["model"], status, counts.get(r["lens"], 0) if r["status"] == "ok" else "-",
            r.get("invalid_findings", 0) or ""))
    missing = [r["lens"] for r in summary["lenses"] if r["status"] != "ok"]
    lines.append("")
    if summary.get("dry_run"):
        lines.append("Dry run: nothing was sent.")
    elif missing:
        lines.append("Incomplete: no answer from lens(es) %s. Missing lenses are not a clean result." %
                     ", ".join(missing))
    if findings:
        lines += ["", "## Findings (grouped by location; a group may hold the same problem seen by several lenses)",
                  ""]
        for f in sorted(findings, key=lambda f: (f["group"], SEVERITY_ORDER[f["severity"]])):
            lines.append("- [group %d] %s:%d %s - %s (%s via %s/%s%s)" % (
                f["group"], f["file"], f["line"], f["severity"], f["title"] or "(no title)", f["lens"], f["vendor"],
                f["model"], ", confidence %d" % f["confidence"] if f["confidence"] is not None else ""))
            if f["reasoning"]:
                lines.append("  " + f["reasoning"].replace("\n", " "))
    elif not summary.get("dry_run") and not missing:
        lines += ["", "No lens reported a problem."]
    return "\n".join(lines) + "\n"
