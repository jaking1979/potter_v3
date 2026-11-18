"""Potter Core (v3.1 control, v3.2 brain): its node tests pass, the manifest agrees, the installer wires it."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "overlay/plugins/potter-core"


class PotterCore(unittest.TestCase):
    def test_node_tests_pass(self):
        r = subprocess.run(["node", "--test", "core.test.mjs"], cwd=D, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-800:])
        self.assertIn("fail 0", r.stdout)
        self.assertNotIn("pass 0", r.stdout)

    def test_manifest_package_and_schema(self):
        m = json.loads((D / "openclaw.plugin.json").read_text())
        p = json.loads((D / "package.json").read_text())
        self.assertEqual((m["id"], p["name"], m["version"]), ("potter-core", "potter-core", p["version"]))
        props = m["configSchema"]["properties"]
        for k in ("mode", "ownerIds", "standingGrants", "governor", "fastPath", "jevEnvFile", "policyPath"):
            self.assertIn(k, props)

    def test_node_tests_for_v33_to_v36_pass(self):
        r = subprocess.run(["node", "--test", "core35.test.mjs", "core36.test.mjs"], cwd=D, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-800:])
        self.assertIn("fail 0", r.stdout)

    def test_every_registered_tool_is_declared_in_the_manifest(self):
        m = json.loads((D / "openclaw.plugin.json").read_text())
        src = (D / "lib/tools.js").read_text()
        import re
        names = re.findall(r'\{ name: "([a-z_]+)"', src)
        self.assertEqual(sorted(m["contracts"]["tools"]), sorted(names))
        self.assertTrue(names)

    def test_new_jobs_are_added_once_the_gateway_is_back(self):
        u = (ROOT / "install/update.sh").read_text()
        self.assertLess(u.index("systemctl start potter-gateway"), u.rindex("add_new_jobs"))
        self.assertIn("--only-missing", u)
        jobs = json.loads((ROOT / "install/jobs.json").read_text())["jobs"]
        self.assertTrue({"potter-memory-review", "potter-notion-outbox"} <= {j["name"] for j in jobs})

    def test_the_jev_client_copy_has_not_drifted(self):
        self.assertEqual((D / "lib/jev-client.js").read_text(), (ROOT / "overlay/jev/client.js").read_text())

    def test_nothing_personal_is_hard_coded(self):
        src = "".join(p.read_text() for p in [D / "index.js", *sorted((D / "lib").glob("*.js"))])
        self.assertNotIn("/home/", src)
        self.assertNotRegex(src, r"[0-9a-f]{32}")
        self.assertNotRegex(src, r"\b\d{9,10}\b")                         # no Telegram ids
        self.assertNotRegex(src, r"(?i)\b(josh|christie|molly|edie)\b")      # no family names in code

    def test_installer_wires_it_with_conversation_access_and_merrick_uses_jev(self):
        boot = (ROOT / "install/bootstrap.sh").read_text()
        self.assertIn("install_plugin \"$cdir\" potter-core --force", boot)
        self.assertIn('"allowConversationAccess": True', boot)
        self.assertIn('MERRICK_DECIDE_CMD="node $POTTER_HOME/jev/merrick-decide.mjs"', boot)
        self.assertLess(boot.index("potter-core --force"), boot.index("Pinning the plugin trust list"))


if __name__ == "__main__":
    unittest.main()
