"""Command line: setup, doctor, plan, review, advise.

Exit codes: 0 done; 2 usage or configuration error; 3 finished but incomplete: some lenses failed (review), fewer
than two independent advisors answered (advise), or a vendor is not ready (doctor).
"""

import argparse
import json
import os
import sys

from . import __version__, advise, assign, catalog as catalog_mod, config as config_mod, doctor, packet, review


def _pins(values):
    pins = {}
    for item in values or []:
        lens, sep, vendor = item.partition("=")
        if not sep or not lens or not vendor:
            raise config_mod.ConfigError("pin must look like lens=vendor: %r" % item)
        pins[lens.strip()] = vendor.strip()
    return pins


def cmd_setup(a, cat):
    vendors = [v.strip() for v in a.vendors.split(",") if v.strip()]
    path = config_mod.config_path()
    previous = None
    if os.path.exists(path):
        try:
            previous = config_mod.load(cat, path)
        except config_mod.ConfigError:
            previous = None
    # One effective host for the whole command: the flag, else the previous configuration, else the default. A new
    # host without --host-model takes that vendor's review model.
    prev = previous or {}
    host = a.host or prev.get("host") or config_mod.DEFAULTS["host"]
    if a.host_model:
        host_model = a.host_model
    elif host == prev.get("host") and prev.get("host_model"):
        host_model = prev["host_model"]
    else:
        host_model = cat["vendors"]["vendors"].get(host, {}).get("review_model") or config_mod.DEFAULTS["host_model"]
    pins = _pins(a.pin)
    if not a.pin and previous:
        # Keep earlier pins that are still valid: the vendor is still selected, and it is not the host while other
        # vendors are selected (a single-vendor setup may pin to the host; adding a vendor drops such pins).
        cross = any(v != host for v in vendors)
        pins = {lens: v for lens, v in prev.get("pins", {}).items()
                if v in vendors and not (cross and v == host)}
    cfg = config_mod.build(vendors, cat, host=host, host_model=host_model, pins=pins, overrides=prev.get("overrides"))
    if previous:
        for key in ("timeout_s", "max_parallel", "max_packet_chars", "guideline_files"):
            cfg[key] = previous.get(key, cfg[key])
    print("Saved %s" % config_mod.save(cfg, path))
    print("")
    print(assign.render(assign.plan(cfg, cat)))
    return 0


def cmd_doctor(a, cat):
    cfg = config_mod.load(cat)
    rows = doctor.run(cfg, cat, with_live=a.live)
    print(json.dumps(rows, indent=2) if a.json else doctor.render(rows))
    return 0 if all(r["state"] in ("READY", "INSTALLED") for r in rows) else 3


def cmd_plan(a, cat):
    cfg = config_mod.load(cat)
    result = assign.plan(cfg, cat)
    if a.json:
        print(json.dumps({k: v for k, v in result.items() if k != "settings"}, indent=2, sort_keys=True))
    else:
        print(assign.render(result))
    return 0


def positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer: %r" % text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def cmd_review(a, cat):
    cfg = config_mod.load(cat)
    if a.timeout is not None:
        cfg["timeout_s"] = a.timeout
    repo = packet.repo_root(a.repo)
    lenses = [x.strip() for x in a.lens.split(",") if x.strip()] if a.lens else None
    code, summary = review.run(cfg, cat, repo, base=a.base, paths=a.paths, lenses=lenses, out_dir=a.out,
                               dry_run=a.dry_run)
    print("Report: %s" % os.path.join(summary["run_dir"], "report.md"))
    print("Findings: %s" % os.path.join(summary["run_dir"], "findings.json"))
    for r in summary["lenses"]:
        print("  %-16s %-7s %-18s %s" % (r["lens"], r["vendor"], r["model"], r["status"]))
    if code == 3:
        print("Incomplete: some lenses did not answer; see the report.")
    return code


def cmd_advise(a, cat):
    cfg = config_mod.load(cat)
    if a.timeout is not None:
        cfg["timeout_s"] = a.timeout
    question = a.question
    if a.question_file:
        with open(a.question_file, encoding="utf-8") as fh:
            question = fh.read()
    code, summary = advise.run(cfg, cat, question, files=a.context or [], base_dir=a.repo, with_diff=a.diff,
                               base=a.base, out_dir=a.out, dry_run=a.dry_run)
    print("Answers: %s" % os.path.join(summary["run_dir"], "answers.md"))
    for r in summary["advisors"]:
        print("  %-7s %-18s %s" % (r["vendor"], r["model"], r["status"]))
    d = summary["diversity"]
    print("Diversity: %.1f (needs %.1f) -> %s" % (d["score"], d["threshold"], d["status"]))
    return code


def parser():
    p = argparse.ArgumentParser(prog="cvr", description="Code review and technical advice across AI vendors.")
    p.add_argument("--version", action="version", version="crossvendor-review " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="choose vendors and write the configuration")
    s.add_argument("--vendors", required=True, help="comma-separated: claude,codex,glm,grok,muse")
    s.add_argument("--host", default=None, help="vendor running the session (default claude)")
    s.add_argument("--host-model", default=None, help="model id of the session (default claude-opus-5-5)")
    s.add_argument("--pin", action="append", help="lens=vendor; repeat to pin several lenses")

    d = sub.add_parser("doctor", help="check that each selected vendor is installed and logged in")
    d.add_argument("--live", action="store_true", help="make one short test call per vendor")
    d.add_argument("--json", action="store_true")

    pl = sub.add_parser("plan", help="show which vendor runs which lens and who advises")
    pl.add_argument("--json", action="store_true")

    r = sub.add_parser("review", help="review the current change")
    r.add_argument("--repo", default=".")
    r.add_argument("--base", help="review everything since the merge base with this ref")
    r.add_argument("--paths", nargs="+", help="limit the review to these paths")
    r.add_argument("--lens", help="comma-separated subset of lenses")
    r.add_argument("--out", help="output directory (default: inside .git)")
    r.add_argument("--timeout", type=positive_int, help="seconds per call")
    r.add_argument("--dry-run", action="store_true", help="build the prompts but send nothing")

    v = sub.add_parser("advise", help="ask every advisor the same question")
    q = v.add_mutually_exclusive_group(required=True)
    q.add_argument("--question")
    q.add_argument("--question-file")
    v.add_argument("--context", nargs="+", help="files to include as context")
    v.add_argument("--diff", action="store_true", help="also include the current change")
    v.add_argument("--base", help="with --diff: changes since the merge base with this ref")
    v.add_argument("--repo", default=".")
    v.add_argument("--out", help="output directory")
    v.add_argument("--timeout", type=positive_int, help="seconds per call")
    v.add_argument("--dry-run", action="store_true")
    return p


COMMANDS = {"setup": cmd_setup, "doctor": cmd_doctor, "plan": cmd_plan, "review": cmd_review, "advise": cmd_advise}


def main(argv=None):
    a = parser().parse_args(argv)
    cat = catalog_mod.load()
    try:
        return COMMANDS[a.command](a, cat)
    except (config_mod.ConfigError, packet.PacketError, ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
