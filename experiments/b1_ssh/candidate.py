"""Offline candidate binding experiment. Not a production authorization gate.

Repository, evidence bytes and candidate selection must be independently trusted.
Does not protect against repository replacement or concurrent object modification.
"""
import hashlib
import os
import re
import subprocess

_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_MANIFEST = "governance/decisions/manifest.json"
_MAX_BYTES = 1024 * 1024


def candidate_facts(repo, commit, evidence):
    if not isinstance(commit, str) or not _SHA1.fullmatch(commit):
        raise ValueError("full lowercase SHA-1 commit required")
    if not isinstance(evidence, bytes):
        raise ValueError("evidence must be immutable bytes")
    if len(evidence) > _MAX_BYTES:
        raise ValueError("evidence exceeds experiment limit")
    # Avoid inherited Git environment variables redirecting repository discovery.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    env['GIT_CONFIG_GLOBAL'] = os.devnull
    env['GIT_NO_REPLACE_OBJECTS'] = '1'
    def git(*args):
        try:
            return subprocess.run(['git', '-C', os.fspath(repo), *args], env=env,
                                  check=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=10).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise ValueError('git object unavailable') from exc
    if git('cat-file', '-t', commit).strip() != b'commit':
        raise ValueError('candidate is not a commit')
    tree = git('rev-parse', '--verify', commit + '^{tree}').strip().decode('ascii')
    if not _SHA1.fullmatch(tree):
        raise ValueError('unsupported tree identity')
    # Read the manifest from the immutable commit tree, never the worktree.
    obj = commit + ':' + _MANIFEST
    if git('cat-file', '-t', obj).strip() != b'blob':
        raise ValueError('manifest must be a blob')
    try:
        size = int(git('cat-file', '-s', obj).strip())
    except ValueError as exc:
        raise ValueError('invalid manifest size') from exc
    if size < 0 or size > _MAX_BYTES:
        raise ValueError('manifest exceeds experiment limit')
    manifest = git('cat-file', 'blob', obj)
    if len(manifest) != size:
        raise ValueError('manifest changed during read')
    return {'commit': commit, 'tree': tree,
            'manifest': hashlib.sha256(manifest).hexdigest(),
            'evidence': hashlib.sha256(evidence).hexdigest()}

