"""Offline grade realignment for synthetic blinded-pair experiments.

The caller must independently authenticate both mappings and restrict their
visibility from graders. This helper checks consistency, not provenance,
blinding, exercise validity or permission to run an evaluation.
"""
import copy
import hashlib
import re
import secrets
from blind_grading import validate_grades

_IDENTITY = re.compile(r'[0-9a-f]{64}\Z')
_MAX_OUTPUT_BYTES = 1_048_576


def prepare_synthetic_occurrences(first_bytes, second_bytes):
    """Create two ephemeral occurrence records and two shuffled presentations.

    Returned mappings are sensitive to evaluator identity and must not be sent
    to graders. This in-memory experiment does not enforce that separation or
    establish trusted provenance, secure storage, or experimental blinding.
    """
    if any(type(data) is not bytes or len(data) > _MAX_OUTPUT_BYTES
           for data in (first_bytes, second_bytes)):
        raise ValueError('bounded output bytes required')
    ids = [secrets.token_hex(32), secrets.token_hex(32)]
    if ids[0] == ids[1]:
        raise ValueError('occurrence ID collision')
    snapshots = dict(zip(ids, (first_bytes, second_bytes)))
    def presentation():
        order = ids[:] if secrets.randbelow(2) == 0 else ids[::-1]
        visible = {label: snapshots[identity] for label, identity in zip(('A', 'B'), order)}
        hidden = {label: {'id': identity, 'sha256': hashlib.sha256(snapshots[identity]).hexdigest()}
                  for label, identity in zip(('A', 'B'), order)}
        return visible, hidden
    return snapshots, presentation(), presentation()


def realign_pair(first, second, first_map, second_map):
    """Align second-pass A/B to first-pass A/B using supplied output hashes.

    The mappings must name the same two distinct hashes. No mapping or identity
    is returned with grader records. Outputs are copied rather than aliased.
    """
    validate_grades(first)
    validate_grades(second)
    if first['case_id'] != second['case_id'] or first['responsibility'] != second['responsibility']:
        raise ValueError('case or responsibility mismatch')
    for mapping in (first_map, second_map):
        if type(mapping) is not dict or set(mapping) != {'A', 'B'}:
            raise ValueError('two-label mapping required')
        if any(not isinstance(v, str) or not _IDENTITY.fullmatch(v) for v in mapping.values()):
            raise ValueError('invalid output identity')
        if mapping['A'] == mapping['B']:
            raise ValueError('ambiguous duplicate outputs')
    if set(first_map.values()) != set(second_map.values()):
        raise ValueError('different output pair')
    reverse = {output_id: label for label, output_id in second_map.items()}
    aligned_first = copy.deepcopy(first)
    aligned_second = {'case_id': second['case_id'], 'responsibility': second['responsibility'],
                      'outputs': {label: copy.deepcopy(second['outputs'][reverse[first_map[label]]])
                                  for label in ('A', 'B')}}
    return aligned_first, aligned_second


def realign_verified_bytes(first, second, first_map, second_map, outputs):
    """Align only when a supplied byte snapshot matches both output hashes.

    Hashing proves consistency with these supplied bytes, not that they are
    the authentic, complete outputs actually presented to graders. Identical
    byte outputs remain unsupported because their identities collide.
    """
    aligned = realign_pair(first, second, first_map, second_map)
    if type(outputs) is not dict or len(outputs) != 2:
        raise ValueError('two output byte snapshots required')
    for identity, data in outputs.items():
        if (not isinstance(identity, str) or not _IDENTITY.fullmatch(identity)
                or type(data) is not bytes or len(data) > _MAX_OUTPUT_BYTES
                or hashlib.sha256(data).hexdigest() != identity):
            raise ValueError('output bytes do not match identity')
    if set(first_map.values()) != set(outputs):
        raise ValueError('output snapshots do not match mapping')
    return aligned


def realign_occurrences(first, second, first_map, second_map, outputs):
    """Align separate output occurrences, including identical byte strings.

    Each label maps to {'id': opaque stable occurrence ID, 'sha256': content
    digest}. Snapshots are keyed by occurrence ID, not content digest. This
    verifies consistency only; IDs, mappings and snapshots still require a
    separately authenticated provenance and protected grader access.
    """
    mappings = []
    for mapping in (first_map, second_map):
        if type(mapping) is not dict or set(mapping) != {'A', 'B'}:
            raise ValueError('two-label occurrence mapping required')
        clean = {}
        for label, item in mapping.items():
            if (type(item) is not dict or set(item) != {'id', 'sha256'}
                    or any(not isinstance(item[k], str) or not _IDENTITY.fullmatch(item[k])
                           for k in ('id', 'sha256'))):
                raise ValueError('invalid occurrence')
            clean[label] = (item['id'], item['sha256'])
        if clean['A'][0] == clean['B'][0]:
            raise ValueError('duplicate occurrence ID')
        mappings.append(clean)
    first_by_id = {identity: digest for identity, digest in mappings[0].values()}
    second_by_id = {identity: digest for identity, digest in mappings[1].values()}
    if first_by_id != second_by_id:
        raise ValueError('occurrence or digest changed')
    if type(outputs) is not dict or set(outputs) != set(first_by_id):
        raise ValueError('two occurrence snapshots required')
    for identity, data in outputs.items():
        if (type(data) is not bytes or len(data) > _MAX_OUTPUT_BYTES
                or hashlib.sha256(data).hexdigest() != first_by_id[identity]):
            raise ValueError('occurrence bytes do not match digest')
    return realign_pair(first, second,
                        {label: value[0] for label, value in mappings[0].items()},
                        {label: value[0] for label, value in mappings[1].items()})


def synthetic_aligned_report(pairs, expected_cases):
    """Compose occurrence alignment and descriptive scoring for complete pairs.

    Every planned case must be present exactly once and independently supplied
    mappings must validate. This deliberately refuses incomplete exercises;
    it is not the separate L17 missing-case scoring policy. Neither it nor its
    result authenticates inputs, enforces blindness, or authorizes evaluation.
    """
    from report import synthetic_report
    if type(pairs) is not list or type(expected_cases) is not dict or not expected_cases:
        raise ValueError('pair list and case plan required')
    if any(not isinstance(k, str) or not k.strip() or type(v) is not bool
           for k, v in expected_cases.items()):
        raise ValueError('invalid case plan')
    first, second, seen = [], [], set()
    required = {'first', 'second', 'first_map', 'second_map', 'outputs'}
    for pair in pairs:
        if type(pair) is not dict or set(pair) != required:
            raise ValueError('invalid pair')
        a, b = realign_occurrences(pair['first'], pair['second'], pair['first_map'],
                                   pair['second_map'], pair['outputs'])
        case = a['case_id']
        if case not in expected_cases or case in seen or a['responsibility'] != expected_cases[case]:
            raise ValueError('unexpected, duplicate or mistyped case')
        seen.add(case)
        first.append(a)
        second.append(b)
    if seen != set(expected_cases):
        raise ValueError('incomplete case plan')
    return synthetic_report(first, second, expected_cases)
