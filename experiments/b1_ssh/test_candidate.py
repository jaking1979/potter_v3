import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from candidate import candidate_facts

class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.manifest = self.repo / 'governance/decisions/manifest.json'
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('{"v":1}\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'one')
        self.commit = self.git('rev-parse', 'HEAD').strip()
    def tearDown(self): self.tmp.cleanup()
    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL).decode()
    def test_tree_and_manifest_are_resolved_from_commit(self):
        facts = candidate_facts(self.repo, self.commit, b'evidence')
        self.assertEqual(facts['tree'], self.git('rev-parse', 'HEAD^{tree}').strip())
        self.assertEqual(facts['manifest'], hashlib.sha256(b'{"v":1}\n').hexdigest())
        self.assertEqual(facts['evidence'], hashlib.sha256(b'evidence').hexdigest())
        self.manifest.write_text('uncommitted change')
        self.assertEqual(facts, candidate_facts(self.repo, self.commit, b'evidence'))
    def test_subdirectory_cannot_bind_enclosing_repository(self):
        subdir = self.repo / 'nested'
        subdir.mkdir()
        with self.assertRaises(ValueError):
            candidate_facts(subdir, self.commit, b'evidence')
    def test_new_commit_changes_binding(self):
        before = candidate_facts(self.repo, self.commit, b'evidence')
        self.manifest.write_text('new')
        self.git('add', '.')
        self.git('commit', '-qm', 'two')
        after = candidate_facts(self.repo, self.git('rev-parse', 'HEAD').strip(), b'evidence')
        self.assertNotEqual(before['tree'], after['tree'])
        self.assertNotEqual(before['manifest'], after['manifest'])
    def test_invalid_or_missing_objects_fail(self):
        for commit in ('HEAD', '-'*40, 'a'*40, self.commit.upper()):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                candidate_facts(self.repo, commit, b'')
        blob = self.git('rev-parse', 'HEAD:governance/decisions/manifest.json').strip()
        with self.assertRaises(ValueError): candidate_facts(self.repo, blob, b'')
        with self.assertRaises(ValueError): candidate_facts(self.repo, self.commit, bytearray())
    def test_missing_manifest_fails_and_git_env_cannot_redirect(self):
        with patch.dict(os.environ, {'GIT_DIR': '/does-not-exist'}):
            candidate_facts(self.repo, self.commit, b'')
        self.git('rm', '-q', str(self.manifest))
        self.git('commit', '-qm', 'remove')
        with self.assertRaises(ValueError):
            candidate_facts(self.repo, self.git('rev-parse', 'HEAD').strip(), b'')

    def test_oversized_manifest_and_evidence_refused(self):
        with self.assertRaises(ValueError):
            candidate_facts(self.repo, self.commit, b'x' * (1024 * 1024 + 1))
        self.manifest.write_bytes(b'x' * (1024 * 1024 + 1))
        self.git('add', '.')
        self.git('commit', '-qm', 'oversized')
        with self.assertRaises(ValueError):
            candidate_facts(self.repo, self.git('rev-parse', 'HEAD').strip(), b'')

    def test_manifest_directory_refused(self):
        self.manifest.unlink()
        self.manifest.mkdir()
        (self.manifest / 'entry').write_text('x')
        self.git('add', '-A')
        self.git('commit', '-qm', 'directory')
        with self.assertRaises(ValueError):
            candidate_facts(self.repo, self.git('rev-parse', 'HEAD').strip(), b'')

if __name__ == '__main__': unittest.main()

