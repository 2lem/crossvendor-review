"""Bundled vendor, lens and prompt data."""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load(name, data_dir):
    with open(os.path.join(data_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


def load(data_dir=DATA_DIR):
    """Return {"vendors": ..., "lenses": ..., "prompts": ...} as shipped with the package."""
    return {"vendors": _load("vendors.json", data_dir), "lenses": _load("lenses.json", data_dir),
            "prompts": _load("prompts.json", data_dir)}


def lens_ids(catalog):
    return [lens["id"] for lens in catalog["lenses"]["lenses"]]
