"""Offline counterexample: resolved path is not a pinned git directory."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import candidate

class GitDirectorySwap(unittest.TestCase):
    def test_swap_after_discovery_follows_different_repository(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'root'
            other = Path(td) / 'other'
            root.mkdir()
            def run(*args):
                return subprocess.check_output(['git', '-C', str(root), *args]).decode().strip()
            run('init', '-q')
            run('config', 'user.name', 'Test')
            run('config', 'user.email', 'test@example.invalid')
            p = root / 'governance/decisions/manifest.json'
            p.parent.mkdir(parents=True)
            p.write_text('{"v":1}\n')
            run('add', '.')
            run('commit', '-qm', 'initial')
            commit = run('rev-parse', 'HEAD')
            subprocess.run(['git', 'clone', '-q', str(root), str(other)], check=True)
            old_dir = root / '.git'
            saved = root / '.git-original'
            original_run = subprocess.run
            swapped = []
            def hooked(cmd, **kwargs):
                if 'cat-file' in cmd and not swapped:
                    old_dir.rename(saved)
                    old_dir.symlink_to(other / '.git', target_is_directory=True)
                    swapped.append(True)
                return original_run(cmd, **kwargs)
            with patch.object(candidate.subprocess, 'run', side_effect=hooked):
                facts = candidate.candidate_facts(root, commit, b'test')
            self.assertTrue(swapped)
            self.assertTrue(old_dir.is_symlink())
            self.assertEqual(facts['commit'], commit)

if __name__ == '__main__':
    unittest.main()
