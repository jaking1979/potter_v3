import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay/tools"))
import cron_switch  # noqa: E402


class Fake:
    def __init__(self, jobs, fail_ids=()):
        self.jobs, self.fail_ids, self.calls = jobs, set(fail_ids), []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[-3:] == ["list", "--all", "--json"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"jobs": self.jobs}), stderr="")
        ok = argv[-1] not in self.fail_ids
        return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="" if ok else "boom")


JOBS = [{"id": "id-1", "name": "potter-fetch-wine"}, {"id": "id-2", "name": "potter-x", "declarationKey": "potter-y"},
        {"id": "id-3", "name": "dup"}, {"id": "id-4", "name": "dup"}]


class Switch(unittest.TestCase):
    def go(self, argv, fake):
        out = io.StringIO()
        return cron_switch.main(argv, run=fake, out=out), out.getvalue()

    def test_names_are_turned_into_ids(self):
        f = Fake(JOBS)
        code, out = self.go(["on", "potter-fetch-wine", "potter-y", "--openclaw", "openclaw --profile t"], f)
        self.assertEqual(code, 0, out)
        self.assertEqual(f.calls[1], ["openclaw", "--profile", "t", "cron", "enable", "id-1"])
        self.assertEqual(f.calls[2][-2:], ["enable", "id-2"])                 # declaration key works too
        self.assertEqual(self.go(["off", "potter-x"], Fake(JOBS))[1].strip(), "potter-x: off")

    def test_unknown_ambiguous_and_failed_jobs_are_reported_and_fail(self):
        code, out = self.go(["on", "nope", "dup", "potter-fetch-wine"], Fake(JOBS, fail_ids={"id-1"}))
        self.assertEqual(code, 1)
        self.assertIn("nope: not found", out)
        self.assertIn("dup: ambiguous", out)
        self.assertIn("potter-fetch-wine: FAILED", out)

    def test_nothing_is_switched_when_the_list_cannot_be_read(self):
        def broken(argv, **kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="down")
        self.assertEqual(self.go(["on", "x"], broken)[0], 1)

    def test_nothing_in_the_repo_enables_a_job_by_name_with_the_raw_command(self):
        import re
        for p in list((ROOT / "install").glob("*.sh")) + list((ROOT / "overlay/workspace/skills").glob("*/SKILL.md")):
            self.assertIsNone(re.search(r"cron (enable|disable) (\"?\$|<name>|potter-)", p.read_text()), p)


if __name__ == "__main__":
    unittest.main()
