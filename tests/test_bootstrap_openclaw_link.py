"""npm 11.19 with --allow-scripts installs OpenClaw but skips linking its command (seen on a real install).
The installer must link it from the package's declared entry point, and must not depend on the PATH it is given."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOT = (ROOT / "install/bootstrap.sh").read_text()

FAKE_NPM = """#!/bin/bash
# stand-in for npm 11.19: installs the package files, links no command
prefix=""; while [ $# -gt 0 ]; do [ "$1" = --prefix ] && prefix="$2"; shift; done
pkg="$prefix/lib/node_modules/openclaw"; mkdir -p "$pkg" "$prefix/bin"
printf '{"name":"openclaw","version":"9.9.9","bin":{"openclaw":"openclaw.mjs"}}' > "$pkg/package.json"
printf '#!/bin/sh\\necho "OpenClaw 9.9.9"\\n' > "$pkg/openclaw.mjs"; chmod 755 "$pkg/openclaw.mjs"
"""


class OpenClawLink(unittest.TestCase):
    def test_the_command_is_linked_when_npm_skips_it(self):
        start = BOOT.index('say "Installing OpenClaw')
        end = BOOT.index("\n", BOOT.index('openclaw --version || die'))
        block = BOOT[start:end]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "bin").mkdir()
            npm = d / "bin/npm"
            npm.write_text(FAKE_NPM)
            npm.chmod(0o755)
            link = d / "usr-local-bin-openclaw"
            script = ("set -euo pipefail\nsay(){ :; }\ndie(){ echo \"DIE $*\"; exit 1; }\n"
                      f"POTTER_HOME={d}/home\nexport PATH={d}/bin:$POTTER_HOME/openclaw/bin:/usr/bin:/bin\n"
                      + block.replace("/usr/local/bin/openclaw", str(link)))
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("OpenClaw 9.9.9", r.stdout)
            self.assertTrue((d / "home/openclaw/bin/openclaw").exists())

    def test_installer_and_health_check_set_their_own_path(self):
        for name in ("bootstrap.sh", "verify_host.sh"):
            text = (ROOT / "install" / name).read_text()
            self.assertIn('export PATH="$POTTER_HOME/openclaw/bin:$POTTER_HOME/node/bin:/usr/local/sbin:/usr/local/bin', text, name)
        self.assertLess(BOOT.index('export PATH="$POTTER_HOME/openclaw/bin'), BOOT.index("collect_inputs"))


if __name__ == "__main__":
    unittest.main()
