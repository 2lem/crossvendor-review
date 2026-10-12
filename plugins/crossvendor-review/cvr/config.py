"""User configuration: which vendors to use, optional pins and model overrides.

The file lives at $CVR_CONFIG, else $XDG_CONFIG_HOME/crossvendor-review/config.json, else
~/.config/crossvendor-review/config.json. It never holds credentials.
"""

import json
import os
import tempfile

from . import catalog as catalog_mod

SCHEMA_VERSION = 1
OVERRIDE_KEYS = {"review_model", "review_effort", "advisor_models", "advisor_effort"}
DEFAULTS = {"host": "claude", "host_model": "claude-opus-5-5", "timeout_s": 1200, "max_parallel": 9,
            "max_packet_chars": 400000, "guideline_files": []}


class ConfigError(Exception):
    pass


def config_path():
    if os.environ.get("CVR_CONFIG"):
        return os.environ["CVR_CONFIG"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "crossvendor-review", "config.json")


def build(vendors, catalog, host=None, host_model=None, pins=None, overrides=None):
    cfg = dict(DEFAULTS)
    cfg.update({"schema_version": SCHEMA_VERSION, "vendors": list(vendors), "pins": dict(pins or {}),
                "overrides": dict(overrides or {})})
    if host:
        cfg["host"] = host
    if host_model:
        cfg["host_model"] = host_model
    problems = validate(cfg, catalog)
    if problems:
        raise ConfigError("; ".join(problems))
    return cfg


def validate(cfg, catalog):
    """Return a list of problems; an empty list means the configuration is usable."""
    problems = []
    known = catalog["vendors"]["vendors"]
    if cfg.get("schema_version") != SCHEMA_VERSION:
        problems.append("unsupported schema_version %r (expected %d)" % (cfg.get("schema_version"), SCHEMA_VERSION))
    vendors = cfg.get("vendors")
    if not isinstance(vendors, list) or not vendors:
        problems.append("select at least one vendor")
        vendors = []
    unknown = sorted({v for v in vendors if v not in known})
    if unknown:
        problems.append("unknown vendor(s): %s (known: %s)" % (", ".join(unknown), ", ".join(known)))
    if len(set(vendors)) != len(vendors):
        problems.append("a vendor is listed twice")
    host = cfg.get("host")
    if host not in known:
        problems.append("unknown host vendor: %r" % host)
    if not isinstance(cfg.get("host_model"), str) or not cfg.get("host_model"):
        problems.append("host_model must be a model id")
    lenses = set(catalog_mod.lens_ids(catalog))
    cross = any(v != host for v in vendors)
    pins = cfg.get("pins", {})
    if not isinstance(pins, dict):
        problems.append("pins must be an object of lens -> vendor")
        pins = {}
    for lens, vendor in sorted(pins.items()):
        if lens not in lenses:
            problems.append("pin for unknown lens %r" % lens)
        elif vendor not in vendors:
            problems.append("lens %r is pinned to %r, which is not selected" % (lens, vendor))
        elif cross and vendor == host:
            problems.append("lens %r is pinned to the host vendor %r; the host does not review its own run" % (
                lens, vendor))
    overrides = cfg.get("overrides", {})
    if not isinstance(overrides, dict):
        problems.append("overrides must be an object of vendor -> settings")
        overrides = {}
    for vendor, values in sorted(overrides.items()):
        if vendor not in known:
            problems.append("override for unknown vendor %r" % vendor)
            continue
        if not isinstance(values, dict):
            problems.append("override for %r must be an object" % vendor)
            continue
        extra = sorted(set(values) - OVERRIDE_KEYS)
        if extra:
            problems.append("override for %r has unknown key(s): %s" % (vendor, ", ".join(extra)))
        models = values.get("advisor_models")
        if models is not None and (not isinstance(models, list) or not models
                                   or any(not isinstance(m, str) or not m.strip() for m in models)):
            problems.append("advisor_models for %r must be a non-empty list of model ids" % vendor)
        for key in ("review_model", "review_effort", "advisor_effort"):
            if key in values and (not isinstance(values[key], str) or not values[key].strip()):
                problems.append("%s for %r must be a non-empty string" % (key, vendor))
    for key in ("timeout_s", "max_parallel", "max_packet_chars"):
        value = cfg.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            problems.append("%s must be a positive integer" % key)
    files = cfg.get("guideline_files", [])
    if not isinstance(files, list) or any(not isinstance(f, str) or os.path.isabs(f) or ".." in f.split("/")
                                          for f in files):
        problems.append("guideline_files must be repository-relative paths")
    return problems


def load(catalog, path=None):
    path = path or config_path()
    if not os.path.exists(path):
        raise ConfigError("no configuration at %s; run setup first" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    if not isinstance(cfg, dict):
        raise ConfigError("%s is not a JSON object" % path)
    merged = dict(DEFAULTS)
    merged.update(cfg)
    problems = validate(merged, catalog)
    if problems:
        raise ConfigError("%s: %s" % (path, "; ".join(problems)))
    return merged


def save(cfg, path=None):
    path = path or config_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def vendor_settings(cfg, catalog, vendor):
    """Catalog entry for a vendor with the user's overrides applied."""
    entry = dict(catalog["vendors"]["vendors"][vendor])
    entry.update(cfg.get("overrides", {}).get(vendor, {}))
    return entry
