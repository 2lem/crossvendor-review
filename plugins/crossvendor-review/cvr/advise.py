"""Ask the same question to every advisor model of the selected vendors in parallel and collect the answers.

The answers are hypotheses for the caller to weigh, not a vote: agreement between vendors does not make an answer
right. The run reports which advisors answered and the diversity score of those that did.
"""

import concurrent.futures
import datetime
import json
import os

from . import adapters, assign, packet as packet_mod


def advisor_prompt(prompts, context_text):
    return "\n".join([prompts["advisor_preamble"], prompts["advisor_contract"], "", context_text])


def run_dir(base_dir, out_dir=None):
    if out_dir:
        path = out_dir
    else:
        stamp = "%s-%d" % (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"), os.getpid())
        try:
            root = packet_mod.git(packet_mod.repo_root(base_dir), ["rev-parse", "--absolute-git-dir"]).strip()
            path = os.path.join(root, "crossvendor-review", "advise-" + stamp)
        except packet_mod.PacketError:
            path = os.path.join(base_dir, ".crossvendor-review", "advise-" + stamp)
    os.makedirs(path, exist_ok=False)
    return path


def run(cfg, catalog, question, files=(), base_dir=".", with_diff=False, base=None, out_dir=None, dry_run=False,
        call=adapters.call):
    """Return (exit_code, summary). Exit 0: answers from at least two independent vendors; 3: fewer."""
    if not question or not question.strip():
        raise ValueError("the question is empty")
    plan = assign.plan(cfg, catalog)
    diff_text = ""
    if with_diff:
        repo = packet_mod.repo_root(base_dir)
        try:
            diff_text, _, _ = packet_mod.review_packet(repo, base, None, cfg["max_packet_chars"])
        except packet_mod.NothingToReview:
            diff_text = "(There is no change to show: the working tree matches %s.)" % (
                "the merge base with " + base if base else "the last commit")
    text, stats = packet_mod.context_packet(question, list(files), base_dir, diff_text, cfg["max_packet_chars"])
    out = run_dir(base_dir, out_dir)
    with open(os.path.join(out, "context.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    prompt = advisor_prompt(catalog["prompts"], text)
    legs = plan["advisors"]
    summary = {"run_dir": out, "context": stats, "advisors": [], "dry_run": dry_run}
    if dry_run:
        summary["advisors"] = [dict(a, status="not-run") for a in legs]
        summary["diversity"] = plan["diversity"]
        _write(out, summary, [])
        return 0, summary
    settings = plan["settings"]

    def one(index, leg):
        leg_dir = os.path.join(out, "legs", "%02d-%s-%s" % (index, leg["vendor"], leg["model"].replace("/", "_")))
        return call(leg["vendor"], settings[leg["vendor"]], leg["model"], leg["effort"], prompt, leg_dir,
                    cfg["timeout_s"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(cfg["max_parallel"], len(legs)))) as pool:
        receipts = list(pool.map(lambda pair: one(*pair), enumerate(legs, 1)))
    answers = []
    for leg, r in zip(legs, receipts):
        entry = dict(leg, status=r["status"], detail=r["detail"], observed_model=r["observed_model"])
        summary["advisors"].append(entry)
        if r["status"] == "ok":
            answers.append(dict(entry, text=r["text"]))
    summary["diversity"] = assign.diversity([a for a in summary["advisors"] if a["status"] == "ok"])
    _write(out, summary, answers)
    return (0 if summary["diversity"]["status"] == "full" else 3), summary


def _write(out, summary, answers):
    with open(os.path.join(out, "answers.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "answers": answers}, fh, indent=2, sort_keys=True)
    lines = ["# Cross-vendor advice", ""]
    lines += ["| Vendor | Model | Status |", "|---|---|---|"]
    for a in summary["advisors"]:
        lines.append("| %s | %s | %s |" % (a["vendor"], a["model"], a["status"]))
    d = summary["diversity"]
    lines += ["", "Diversity of the answers received: %.1f (needs %.1f) -> %s" % (d["score"], d["threshold"],
                                                                                 d["status"])]
    if summary.get("dry_run"):
        lines.append("Dry run: nothing was sent.")
    for a in answers:
        lines += ["", "## %s / %s%s" % (a["vendor"], a["model"], " (same model as host)" if a["host_correlated"]
                                         else ""), "", a["text"].strip()]
    with open(os.path.join(out, "answers.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
