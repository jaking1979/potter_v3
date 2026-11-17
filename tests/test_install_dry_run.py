"""The Proxmox dry run must finish (exit 0) and change nothing: every command that would create, start, push or
run something inside the container is printed, never executed. Fake pct/qm/pveam/pvesm stand in for the host."""
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE = {
    # status of any guest: "does not exist" (exit 2), like a host where 211 is free. Every other pct call is logged
    # and fails, so an unguarded real call shows up both in the log and as a failed run.
    "pct": 'echo "pct $*" >> "$FAKE_LOG"; [ "$1" = status ] && exit 2; '
           'echo "Configuration file does not exist" >&2; exit 2',
    "qm": 'echo "qm $*" >> "$FAKE_LOG"; exit 2',
    "pveam": 'echo "pveam $*" >> "$FAKE_LOG"; [ "$1" = list ] && echo "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst 1 1"; exit 0',
    "pvesm": 'echo "pvesm $*" >> "$FAKE_LOG"; echo "Name Type Status Total Used Available %"; '
             'echo "local-lvm lvmthin active 100000000 1000000 19818086 1%"; exit 0',
}


class ProxmoxDryRun(unittest.TestCase):
    def test_dry_run_finishes_and_executes_nothing_that_changes_the_host(self):
        with tempfile.TemporaryDirectory() as d:
            bindir, log = Path(d) / "bin", Path(d) / "calls.log"
            bindir.mkdir()
            for name, body in FAKE.items():
                f = bindir / name
                f.write_text("#!/bin/bash\n" + body + "\n")
                f.chmod(f.stat().st_mode | stat.S_IEXEC)
            env = {"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_LOG": str(log), "HOME": d,
                   **{k: v for k, v in (("POTTER_CTID", "211"), ("POTTER_CT_NAME", "potter-v3"),
                                        ("POTTER_CT_STORE", "local-lvm"), ("POTTER_CT_BRIDGE", "vmbr0"),
                                        ("POTTER_CT_DISK", "12"), ("POTTER_CT_MEM", "64"), ("POTTER_CT_CORES", "2"))}}
            r = subprocess.run(["bash", str(ROOT / "install/install.sh"), "--proxmox", "--dry-run"], env=env,
                               capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])
            calls = log.read_text().splitlines() if log.exists() else []
            for c in calls:
                self.assertRegex(c, r"^(pct status|qm status|pveam list|pveam available|pvesm status)\b", c)
            out = r.stdout
            for step in ("DRY: pct create 211", "DRY: pct push 211", "setup_form.py", "verify_host.sh",
                         "network address"):
                self.assertIn(step, out)
            self.assertIn("DRY: tar ", out)                                 # the source bundle is not written



if __name__ == "__main__":
    unittest.main()
