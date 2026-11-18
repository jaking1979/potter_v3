"""iMessage: safe channel defaults (only the owner directs Potter; groups need a mention); setup script is installed."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "install"))
import imessage_config  # noqa: E402


class IMessage(unittest.TestCase):
    def test_only_the_owner_directs_potter_and_groups_need_a_mention(self):
        c = imessage_config.build("192.0.2.10", "potter", ["+15555550100", "me@example.com"], "/etc/potter/imsg-ssh")["channels"]["imessage"]
        self.assertEqual(c["dmPolicy"], "allowlist")
        self.assertEqual(c["allowFrom"], ["+15555550100", "me@example.com"])
        self.assertEqual(c["groupAllowFrom"], c["allowFrom"])
        self.assertEqual(c["groups"], {"*": {"requireMention": True}})
        self.assertEqual(c["dbPath"], "/Users/potter/Library/Messages/chat.db")
        self.assertEqual(c["remoteHost"], "potter@192.0.2.10")
        self.assertFalse(c["includeAttachments"])

    def test_bad_handles_or_users_are_refused(self):
        with self.assertRaises(ValueError):
            imessage_config.build("h", "potter", ["555-0100"], "/x")
        with self.assertRaises(ValueError):
            imessage_config.build("h", "potter", [], "/x")
        with self.assertRaises(ValueError):
            imessage_config.build("h", "Potter User", ["+15555550100"], "/x")

    def test_setup_is_installed_and_uses_a_pinned_known_host(self):
        s = (ROOT / "install/imessage_setup.sh").read_text()
        self.assertIn("StrictHostKeyChecking yes", s)
        self.assertIn("BatchMode yes", s)
        for f in ("install/update.sh", "install/bootstrap.sh"):
            self.assertIn("/usr/local/bin/potter-imessage-setup", (ROOT / f).read_text())


if __name__ == "__main__":
    unittest.main()
