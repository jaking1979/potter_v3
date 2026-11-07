"""Offline T39 ancestry observations; no signing, gate or Git execution.

The caller must obtain the graph from an authenticated, complete Git source.
Validation proves internal closure only, not object contents or provenance.
"""
import argparse
import hashlib
import json
import re

SHA = re.compile(r"[0-9a-f]{40}")


class InventoryError(ValueError):
    pass


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise InventoryError("expected full lowercase SHA-1")
    return value


def inventory(tip, records):
    """Return one canonical full graph; reject omission, excess and cycles.

    All reachable commits, including merged side branches, are inventoried.
    The first-parent line is separately reported and is never an exemption.
    """
    _sha(tip)
    if type(records) is not list or not records or len(records) > 100000:
        raise InventoryError("expected bounded nonempty commit list")
    nodes = {}
    for row in records:
        if type(row) is not dict or set(row) != {"sha", "tree", "parents"}:
            raise InventoryError("unexpected commit fields")
        sha, tree = _sha(row["sha"]), _sha(row["tree"])
        parents = row["parents"]
        if type(parents) is not list or len(parents) > 64:
            raise InventoryError("invalid parent list")
        for parent in parents:
            _sha(parent)
        if len(set(parents)) != len(parents) or sha in nodes:
            raise InventoryError("duplicate commit or parent")
        nodes[sha] = {"sha": sha, "tree": tree, "parents": parents[:]}
    if tip not in nodes:
        raise InventoryError("missing tip")
    if any(parent not in nodes for node in nodes.values() for parent in node["parents"]):
        raise InventoryError("missing parent; truncated or incomplete graph")
    # Iterative traversal bounds stack use for long histories and rejects cycles.
    visited, active = set(), set()
    work = [(tip, False)]
    while work:
        sha, leaving = work.pop()
        if leaving:
            active.remove(sha)
            visited.add(sha)
            continue
        if sha in active:
            raise InventoryError("cyclic graph")
        if sha in visited:
            continue
        active.add(sha)
        work.append((sha, True))
        work.extend((p, False) for p in reversed(nodes[sha]["parents"]))
    if visited != nodes.keys():
        raise InventoryError("unreachable extra commit")
    first_parent, sha = [], tip
    while sha:
        first_parent.append(sha)
        sha = nodes[sha]["parents"][0] if nodes[sha]["parents"] else None
    ordered = [nodes[sha] for sha in sorted(nodes)]
    canonical = json.dumps({"tip": tip, "commits": ordered}, sort_keys=True,
                           separators=(",", ":")).encode()
    return {"schema": "potter.t39.ancestry-inventory/v0", "tip": tip,
            "commit_count": len(ordered), "first_parent": first_parent,
            "graph_sha256": hashlib.sha256(canonical).hexdigest(),
            "commits": ordered, "authority": "observation-only"}


def compare(expected, actual):
    """Compare two validated observations without granting receipt authority."""
    a = inventory(expected["tip"], expected["commits"])
    b = inventory(actual["tip"], actual["commits"])
    old = {c["sha"]: c for c in a["commits"]}
    new = {c["sha"]: c for c in b["commits"]}
    return {"tip_changed": a["tip"] != b["tip"],
            "added": sorted(new.keys() - old.keys()),
            "missing": sorted(old.keys() - new.keys()),
            "changed": sorted(sha for sha in old.keys() & new.keys() if old[sha] != new[sha])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observation")
    args = parser.parse_args()
    with open(args.observation, encoding="utf-8") as stream:
        observed = json.load(stream)
    print(json.dumps(inventory(observed["observed_tip"], observed["commits"]),
                     sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
