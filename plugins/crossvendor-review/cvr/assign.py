"""Deterministic assignment of review lenses and advisors from the selected vendors.

Rules
  1. The host vendor (the one running the session, Claude inside Claude Code) does not review its own run: lenses
     go to the other selected vendors. With no other vendor the run is "single-vendor" and labelled advisory.
  2. Pinned lenses go to their pinned vendor. The other lenses are placed in a fixed order on the least-loaded
     eligible vendor (ties broken by vendor order), avoiding vendors whose flags a lens lists under "avoid" when
     another vendor is available; an avoid rule that cannot be honoured is shown as a warning.
  3. Every lens gets a fallback for a failed call: the first other eligible vendor (never the host) that the lens
     does not avoid. Without one the fallback is the host itself, labelled as such.
  4. Advisors are every advisor model of every selected vendor. A model equal to the host model is
     host-correlated. Diversity score: per vendor 1.0, or 0.5 when all its models are host-correlated; a score of
     2.0 or more means at least two independent vendors answer.
"""

import re

from . import config as config_mod

DIVERSITY_THRESHOLD = 2.0


def host_correlated(model, host_model):
    """Exact match, or the host id with a non-alphanumeric suffix such as 'x[1m]' ('x-5' does not match 'x-50')."""
    if model == host_model:
        return True
    return host_model.startswith(model) and re.match(r"[^A-Za-z0-9-]", host_model[len(model):]) is not None


def plan(cfg, catalog):
    order = catalog["vendors"]["order"]
    host, host_model = cfg["host"], cfg["host_model"]
    selected = [v for v in order if v in set(cfg["vendors"])]
    settings = {v: config_mod.vendor_settings(cfg, catalog, v) for v in selected}
    if host not in settings:
        settings[host] = config_mod.vendor_settings(cfg, catalog, host)

    eligible = [v for v in selected if v != host]
    mode = "cross-vendor" if eligible else "single-vendor"
    if not eligible:
        eligible = [host]

    pins = cfg.get("pins", {})
    load = {v: 0 for v in eligible}
    for lens, vendor in pins.items():
        if vendor in load:
            load[vendor] += 1
    lenses, warnings = [], []
    for lens in catalog["lenses"]["lenses"]:
        lens_id = lens["id"]
        avoid = set(lens.get("avoid", []))
        if lens_id in pins:
            chosen = pins[lens_id]
            if avoid & set(settings[chosen].get("flags", [])):
                warnings.append({"lens": lens_id, "kind": "pinned-to-avoided-vendor",
                                 "detail": sorted(avoid & set(settings[chosen].get("flags", [])))})
        else:
            preferred = [v for v in eligible if not (avoid & set(settings[v].get("flags", [])))]
            if not preferred:
                preferred = list(eligible)
                warnings.append({"lens": lens_id, "kind": "avoid-not-honoured", "detail": sorted(avoid)})
            chosen = min(preferred, key=lambda v: (load[v], order.index(v)))
            load[chosen] += 1
        targets =[t for t in eligible if t != chosen and t != host and not (avoid & set(settings[t].get("flags", [])))]
        lenses.append({"lens": lens_id, "vendor": chosen, "model": settings[chosen]["review_model"],
                       "effort": settings[chosen]["review_effort"], "pinned": lens_id in pins,
                       "fallback": targets[0] if targets else host})

    advisors = []
    for v in selected:
        for m in settings[v]["advisor_models"]:
            advisors.append({"vendor": v, "model": m, "effort": settings[v]["advisor_effort"],
                             "host_correlated": host_correlated(m, host_model)})

    return {"schema_version": 1, "host": host, "host_model": host_model, "selected": selected, "mode": mode,
            "lenses": lenses, "warnings": warnings, "advisors": advisors, "diversity": diversity(advisors),
            "settings": settings}


def diversity(legs):
    """Score over the legs given (callers pass the legs that answered to score a finished run)."""
    score = 0.0
    for v in sorted({leg["vendor"] for leg in legs}):
        vlegs = [leg for leg in legs if leg["vendor"] == v]
        score += 0.5 if all(leg["host_correlated"] for leg in vlegs) else 1.0
    return {"score": score, "threshold": DIVERSITY_THRESHOLD,
            "status": "full" if score >= DIVERSITY_THRESHOLD else "advisory (low diversity)"}


def render(result):
    lines = ["Host: %s (%s), mode: %s" % (result["host"], result["host_model"], result["mode"]), "",
             "| Lens | Vendor | Model | Effort | Fallback |", "|---|---|---|---|---|"]
    for row in result["lenses"]:
        fallback = row["fallback"] + (" (host)" if row["fallback"] == result["host"] else "")
        if row["fallback"] == row["vendor"]:
            fallback = "none"
        lines.append("| %s%s | %s | %s | %s | %s |" % (row["lens"], " (pinned)" if row["pinned"] else "",
                                                      row["vendor"], row["model"], row["effort"], fallback))
    for w in result["warnings"]:
        lines.append("Warning: lens %s: %s (%s)" % (w["lens"], w["kind"], ", ".join(w["detail"])))
    lines.append("")
    lines.append("Advisors: " + ", ".join("%s/%s%s" % (a["vendor"], a["model"], " (same model as host)"
                                                        if a["host_correlated"] else "") for a in result["advisors"]))
    d = result["diversity"]
    lines.append("Advisor diversity: %.1f (needs %.1f) -> %s" % (d["score"], d["threshold"], d["status"]))
    lines.append("Calls per run: review %d, advise %d" % (len(result["lenses"]), len(result["advisors"])))
    if result["mode"] == "single-vendor":
        lines.append("Note: only the host vendor is selected; results are single-vendor and advisory.")
    return "\n".join(lines)
