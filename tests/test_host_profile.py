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
        self.assertEqual(profile.diff(facts, observed), {"ac_power_recovery": ("off", "power-on")})
        self.assertIn("guest_onboot", profile.unobserved(facts, observed))

    def test_malformed_profiles_refused(self):
        bad = [{"schema": "x", "facts": {}},
               {"schema": "potter.assumed-host/v0", "facts": {"a": {"value": 1, "status": "guess", "source": "s"}}},
               {"schema": "potter.assumed-host/v0", "facts": {"a": {"value": 1, "status": "assumed", "source": ""}}},
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
