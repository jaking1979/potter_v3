import json
import tempfile
import unittest
from pathlib import Path

from host import profile


class ProfileTests(unittest.TestCase):
    def test_committed_profile_loads_and_lists_assumptions(self):
        facts = profile.load()
        self.assertIn("broker_ack_shapes", profile.assumptions(facts))
        self.assertNotIn("proxmox_version", profile.assumptions(facts))
        self.assertTrue(all(f.source for f in facts.values()))

    def test_no_fact_is_verified_before_the_host_check(self):
        self.assertFalse([f for f in profile.load().values() if f.status == "verified"])

    def test_diff_and_unobserved(self):
        facts = profile.load()
        observed = {"proxmox_version": "pve-manager/9.2.3", "ac_power_recovery": "power-on"}
        self.assertEqual(profile.diff(facts, observed), {"ac_power_recovery": ("unknown", "power-on")})
        self.assertIn("guest_onboot", profile.unobserved(facts, observed))

    def test_observations_are_strictly_typed(self):
        facts = profile.load()
        for bad in ({"catskills_ha_reachable": 1}, {"bay_ridge_ha_online": 0},
                    {"proxmox_version": 9}, {"not_a_fact": "x"}, {"broker_ack_shapes": "order_id"}):
            with self.subTest(bad=bad):
                self.assertTrue(profile.check_observed(facts, bad))
                with self.assertRaises(profile.ProfileError):
                    profile.diff(facts, bad)
        self.assertEqual(profile.diff(facts, {"catskills_ha_reachable": True}), {})
        self.assertEqual(profile.diff(facts, {"catskills_ha_reachable": False}),
                         {"catskills_ha_reachable": (True, False)})

    def test_observation_and_setting_are_separate(self):
        facts = profile.load()
        self.assertEqual(facts["restarted_after_outage"].status, "reported")
        self.assertEqual(facts["ac_power_recovery"].status, "assumed")
        self.assertIn("other_external_alert_paths", profile.assumptions(facts))

    def test_malformed_profiles_refused(self):
        bad = [{"schema": "x", "facts": {}},
               {"schema": "potter.assumed-host/v0", "facts": {"a": {"value": 1, "status": "guess", "source": "s", "type": "int"}}},
               {"schema": "potter.assumed-host/v0", "facts": {"a": {"value": 1, "status": "assumed", "source": "", "type": "int"}}},
               {"schema": "potter.assumed-host/v0", "facts": {"a": {"value": 1, "status": "assumed"}}}]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.json"
            for b in bad:
                p.write_text(json.dumps(b))
                with self.subTest(b=b), self.assertRaises(profile.ProfileError):
                    profile.load(p)
            p.write_text("{not json")
            with self.assertRaises(profile.ProfileError):
                profile.load(p)


if __name__ == "__main__":
    unittest.main()
