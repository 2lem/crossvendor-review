import os
import unittest

from support import CATALOG, Env
from cvr import assign, config as config_mod


def cfg_for(vendors, **kw):
    return config_mod.build(vendors, CATALOG, **kw)


class AssignTests(unittest.TestCase):
    def test_all_vendors_spread_lenses_and_keep_host_out(self):
        result = assign.plan(cfg_for(["claude", "codex", "glm", "grok", "muse"]), CATALOG)
        self.assertEqual(result["mode"], "cross-vendor")
        vendors = [r["vendor"] for r in result["lenses"]]
        self.assertNotIn("claude", vendors)
        self.assertEqual(sorted(vendors.count(v) for v in set(vendors)), [2, 2, 2, 3])
        privacy = next(r for r in result["lenses"] if r["lens"] == "data-privacy")
        self.assertNotEqual(privacy["vendor"], "glm")  # avoids a vendor that may train on input
        self.assertEqual(result, assign.plan(cfg_for(["muse", "grok", "glm", "codex", "claude"]), CATALOG))

    def test_single_vendor_is_labelled_advisory(self):
        result = assign.plan(cfg_for(["claude"]), CATALOG)
        self.assertEqual(result["mode"], "single-vendor")
        self.assertEqual({r["vendor"] for r in result["lenses"]}, {"claude"})
        self.assertEqual(result["diversity"]["status"], "advisory (low diversity)")
        self.assertIn("single-vendor and advisory", assign.render(result))

    def test_two_vendors_fall_back_to_the_host(self):
        result = assign.plan(cfg_for(["claude", "codex"]), CATALOG)
        self.assertEqual({r["vendor"] for r in result["lenses"]}, {"codex"})
        self.assertEqual({r["fallback"] for r in result["lenses"]}, {"claude"})
        self.assertIn("| codex | gpt-6.1-sol | high | claude (host) |", assign.render(result))

    def test_fallback_honours_the_lens_avoid_rule(self):
        result = assign.plan(cfg_for(["claude", "codex", "glm", "grok"]), CATALOG)
        privacy = next(r for r in result["lenses"] if r["lens"] == "data-privacy")
        self.assertNotIn("glm", (privacy["vendor"], privacy["fallback"]))
        others = [r["fallback"] for r in result["lenses"] if r["lens"] != "data-privacy" and r["vendor"] != "codex"]
        self.assertIn("codex", others)

    def test_pin_to_an_avoided_vendor_is_a_warning(self):
        result = assign.plan(cfg_for(["claude", "codex", "glm"], pins={"data-privacy": "glm"}), CATALOG)
        self.assertIn({"lens": "data-privacy", "kind": "pinned-to-avoided-vendor", "detail": ["may_train_on_input"]},
                      result["warnings"])
        self.assertIn("pinned-to-avoided-vendor", assign.render(result))

    def test_single_vendor_has_no_fallback(self):
        result = assign.plan(cfg_for(["claude"]), CATALOG)
        self.assertEqual({r["fallback"] for r in result["lenses"]}, {"claude"})
        self.assertIn("| none |", assign.render(result))

    def test_avoid_rule_that_cannot_be_honoured_is_a_warning(self):
        result = assign.plan(cfg_for(["claude", "glm"]), CATALOG)
        self.assertEqual(result["warnings"], [{"lens": "data-privacy", "kind": "avoid-not-honoured",
                                               "detail": ["may_train_on_input"]}])

    def test_pins_are_honoured_and_count_toward_load(self):
        result = assign.plan(cfg_for(["claude", "codex", "grok"], pins={"security": "grok", "tests": "grok"}),
                             CATALOG)
        by_lens = {r["lens"]: r for r in result["lenses"]}
        self.assertEqual((by_lens["security"]["vendor"], by_lens["security"]["pinned"]), ("grok", True))
        counts = [r["vendor"] for r in result["lenses"]]
        self.assertLessEqual(abs(counts.count("codex") - counts.count("grok")), 1)

    def test_diversity_counts_host_correlated_vendor_as_half(self):
        legs = [{"vendor": "claude", "model": "claude-opus-5-5", "host_correlated": True}]
        self.assertEqual(assign.diversity(legs)["score"], 0.5)
        legs.append({"vendor": "claude", "model": "claude-fable-5-1", "host_correlated": False})
        self.assertEqual(assign.diversity(legs)["score"], 1.0)
        legs.append({"vendor": "codex", "model": "gpt-6-astra", "host_correlated": False})
        self.assertEqual(assign.diversity(legs)["status"], "full")

    def test_host_correlation_matching(self):
        self.assertTrue(assign.host_correlated("m-5", "m-5"))
        self.assertTrue(assign.host_correlated("m-5", "m-5[1m]"))
        self.assertFalse(assign.host_correlated("m-5", "m-50"))

    def test_overrides_change_models(self):
        cfg = cfg_for(["claude", "codex"], overrides={"codex": {"review_model": "gpt-x", "advisor_models": ["a"]}})
        result = assign.plan(cfg, CATALOG)
        self.assertEqual({r["model"] for r in result["lenses"]}, {"gpt-x"})
        self.assertIn({"vendor": "codex", "model": "a", "effort": "max", "host_correlated": False},
                      result["advisors"])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def problems(self, **changes):
        cfg = cfg_for(["claude", "codex"])
        cfg.update(changes)
        return config_mod.validate(cfg, CATALOG)

    def test_validation_rejects_bad_input(self):
        self.assertTrue(any("unknown vendor" in p for p in self.problems(vendors=["claude", "nope"])))
        self.assertTrue(any("not selected" in p for p in self.problems(pins={"security": "grok"})))
        self.assertTrue(any("host vendor" in p for p in self.problems(pins={"security": "claude"})))
        self.assertTrue(any("unknown lens" in p for p in self.problems(pins={"style": "codex"})))
        self.assertTrue(any("unknown key" in p for p in self.problems(overrides={"codex": {"model": "x"}})))
        self.assertTrue(any("non-empty list" in p for p in self.problems(overrides={"codex": {"advisor_models": []}})))
        self.assertTrue(any("repository-relative" in p for p in self.problems(guideline_files=["../x"])))
        self.assertTrue(any("positive integer" in p for p in self.problems(timeout_s=True)))
        self.assertTrue(any("twice" in p for p in self.problems(vendors=["claude", "codex", "codex"])))
        self.assertEqual(self.problems(), [])

    def test_single_vendor_may_pin_to_host(self):
        cfg = cfg_for(["claude"], pins={"security": "claude"})
        self.assertEqual(cfg["pins"], {"security": "claude"})

    def test_save_load_round_trip_and_missing_file(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(CATALOG)
        cfg = cfg_for(["claude", "grok"])
        path = config_mod.save(cfg)
        self.assertEqual(path, os.environ["CVR_CONFIG"])
        self.assertEqual(config_mod.load(CATALOG), cfg)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(CATALOG)

    def test_config_path_order(self):
        del os.environ["CVR_CONFIG"]
        os.environ["XDG_CONFIG_HOME"] = "/x"
        self.assertEqual(config_mod.config_path(), "/x/crossvendor-review/config.json")


if __name__ == "__main__":
    unittest.main()
