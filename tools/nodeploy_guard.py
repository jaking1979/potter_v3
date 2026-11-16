"""Fail-closed "no deploy, no real secrets" guard for the v3-core integration branch.

Ratified bounded Rule 12 revision (Josh, 2026-11-16): v3-core is never deployed.
CI must fail if the tree contains deploy automation, a workflow with write or
secret access, secret-shaped material, or references to the real host,
credentials or Alpha Bot outside the documented allowlist.

Scope and limits: a text scan of tracked files. It cannot prove absence of a
deploy path, only refuse the common shapes of one. It never reads real
secrets, hosts or credentials: all patterns are generic shapes, and the
policy file lists only non-secret identifiers. A match is reported with file,
line and rule id, never with the matched text, so a real secret is not echoed
into CI logs.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

POLICY_FILE = Path(__file__).with_name("nodeploy_policy.json")
MAX_FILE_BYTES = 2_000_000

# --- rule patterns (generic shapes only) ---------------------------------------
SECRET_PATTERNS = {
    "secret.private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "secret.aws_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "secret.github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    "secret.slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "secret.telegram_bot_token": re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b"),
    "secret.provider_api_key": re.compile(r"\bsk-(?:ant-|or-v1-|proj-)?[A-Za-z0-9_-]{32,}\b"),
    "secret.assigned_credential": re.compile(
        r"""(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*['"][A-Za-z0-9/+_\-.=]{20,}['"]"""),
}
DEPLOY_WORDS = re.compile(
    r"(?i)\b(?:deploy|rsync|scp|sftp|kubectl|helm\s+(?:install|upgrade)|terraform\s+apply|"
    r"docker\s+push|twine\s+upload|npm\s+publish|gh\s+release|systemctl|ansible-playbook|"
    r"ssh\s+[-\w@.]+)\b")
RFC1918 = re.compile(
    r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")
WORKFLOW_DIR = ".github/workflows/"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self):
        return f"{self.path}:{self.line}: {self.rule}"


def load_policy(path: Path = POLICY_FILE) -> dict:
    policy = json.loads(path.read_text(encoding="utf-8"))
    for key in ("forbidden_literals", "doc_allow_globs", "workflow_allow_deploy_words"):
        if not isinstance(policy.get(key), list) or any(type(i) is not str for i in policy[key]):
            raise ValueError(f"policy field {key} must be a list of strings")
    return policy


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def _is_doc(path: str, policy: dict) -> bool:
    return any(Path(path).match(g) or path.startswith(g.rstrip("*")) for g in policy["doc_allow_globs"])


def scan_text(path: str, text: str, policy: dict) -> list[Finding]:
    findings: list[Finding] = []
    is_doc = _is_doc(path, policy)
    is_workflow = path.startswith(WORKFLOW_DIR) and path.endswith((".yml", ".yaml"))
    lowered_literals = [lit.lower() for lit in policy["forbidden_literals"]]
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                findings.append(Finding(path, number, rule))
        if not is_doc:
            low = line.lower()
            if any(lit in low for lit in lowered_literals):
                findings.append(Finding(path, number, "reference.forbidden_literal"))
            if RFC1918.search(line):
                findings.append(Finding(path, number, "reference.private_address"))
    if is_workflow:
        findings.extend(_scan_workflow(path, text, policy))
    return findings


def _scan_workflow(path: str, text: str, policy: dict) -> list[Finding]:
    findings: list[Finding] = []
    allowed = set(policy["workflow_allow_deploy_words"])
    has_permissions = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.split("#", 1)[0]
        if re.match(r"^\s*permissions\s*:", stripped):
            has_permissions = True
        if re.match(r"^\s*[\w-]+\s*:\s*write\s*$", stripped) or re.match(r"^\s*permissions\s*:\s*write-all\s*$", stripped):
            findings.append(Finding(path, number, "workflow.write_permission"))
        if re.search(r"\bsecrets\.(?!GITHUB_TOKEN\b)\w+", stripped):
            findings.append(Finding(path, number, "workflow.secret_reference"))
        if re.match(r"^\s*environment\s*:", stripped):
            findings.append(Finding(path, number, "workflow.deployment_environment"))
        for match in DEPLOY_WORDS.finditer(stripped):
            if match.group(0).lower() not in allowed:
                findings.append(Finding(path, number, "workflow.deploy_command"))
    if not has_permissions:
        findings.append(Finding(path, 1, "workflow.missing_permissions"))
    return findings


def scan_tree(root: Path, policy: dict | None = None, files: list[str] | None = None) -> list[Finding]:
    policy = policy or load_policy()
    findings: list[Finding] = []
    for rel in sorted(files if files is not None else tracked_files(root)):
        path = root / rel
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                if path.is_file() and path.stat().st_size > MAX_FILE_BYTES:
                    findings.append(Finding(rel, 0, "file.too_large_to_scan"))
                continue
            data = path.read_bytes()
        except OSError:
            findings.append(Finding(rel, 0, "file.unreadable"))
            continue
        if b"\0" in data[:8192]:
            continue  # binary: not scanned (tracked binaries are listed by the caller's policy)
        findings.extend(scan_text(rel, data.decode("utf-8", "replace"), policy))
        if rel in policy.get("forbidden_paths", []) or Path(rel).name == ".env" or Path(rel).name.startswith(".env."):
            findings.append(Finding(rel, 0, "path.forbidden_file"))
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    findings = scan_tree(root)
    for finding in findings:
        print(finding)
    if findings:
        print(f"nodeploy_guard: {len(findings)} finding(s); refusing", file=sys.stderr)
        return 1
    print("nodeploy_guard: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
