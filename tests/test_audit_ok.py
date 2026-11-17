import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "install"))
import audit_ok as a  # noqa: E402

FUNNEL = """Summary: 1 critical · 4 warn · 2 info
CRITICAL
gateway.tailscale_funnel Tailscale Funnel exposure enabled
  gateway.tailscale.mode="funnel" exposes the Gateway publicly; keep auth strict.
WARN
gateway.something Something minor
"""
TWO = FUNNEL.replace("Summary: 1 critical", "Summary: 2 critical").replace(
    "WARN\n", "plugins.allow No plugin trust list\n  details\nWARN\n")


class Audit(unittest.TestCase):
    def test_clean_audit_passes(self):
        self.assertEqual(a.ok("Summary: 0 critical · 1 warn", "serve"), (True, []))

    def test_funnel_passes_only_when_the_owner_chose_it(self):
        self.assertTrue(a.ok(FUNNEL, "funnel")[0])
        self.assertEqual(a.ok(FUNNEL, "serve"), (False, ["gateway.tailscale_funnel"]))

    def test_any_other_critical_finding_still_fails(self):
        self.assertEqual(a.ok(TWO, "funnel"), (False, ["plugins.allow"]))

    def test_unreadable_audit_fails(self):
        self.assertFalse(a.ok("", "funnel")[0])


if __name__ == "__main__":
    unittest.main()
