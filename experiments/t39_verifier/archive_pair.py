"""Synthetic archived-receipt verification experiment; no authorization.

The caller must authenticate the original issuance event and historical key,
revocation and review/check state from independently protected durable sources.
In particular, a timestamp merely asserted or signed by a reviewer does not
establish when a receipt was accepted. This module does not authenticate any
of those inputs and must not authorize build, deployment or rollback.
"""
from verify_pair import verify_pair


def verify_synthetic_archive(pair, *, issued_facts, issued_at, observed_now,
                             expected, agents, historical_agent_keys,
                             historical_revocations, required_checks,
                             historical_check_statuses, historical_open_requests,
                             max_lifetime):
    """Recheck a signed pair against a caller-supplied historical snapshot.

    All receipt binding fields must match; verifier version is audit metadata. The
    caller must independently establish issuance-time provenance and monotonic
    time. A pass is descriptive cryptographic consistency only.
    """
    if type(issued_at) is not int or type(observed_now) is not int or issued_at > observed_now:
        raise ValueError('invalid historical time')
    if type(pair) is not tuple or len(pair) != 4 or type(issued_facts) is not dict:
        raise ValueError('pair and issued facts required')
    facts = verify_pair(*pair, expected=expected, agents=agents,
                        agent_keys=historical_agent_keys,
                        revoked_fingerprints=historical_revocations,
                        required_checks=required_checks,
                        check_statuses=historical_check_statuses,
                        open_request_changes=historical_open_requests,
                        now=issued_at, max_lifetime=max_lifetime)
    if (set(facts) != set(issued_facts)
            or not isinstance(issued_facts.get('verifier_version'), str)
            or not issued_facts['verifier_version']):
        raise ValueError('invalid issued facts')
    def binding(record):
        result = {k: v for k, v in record.items() if k != 'verifier_version'}
        nonces = result.get('nonces')
        if type(nonces) not in (tuple, list) or len(nonces) != 2 or any(type(n) is not str for n in nonces):
            raise ValueError('invalid issued nonces')
        result['nonces'] = sorted(nonces)
        return result
    if binding(facts) != binding(issued_facts):
        raise ValueError('archived facts do not match signed pair')
    return facts

