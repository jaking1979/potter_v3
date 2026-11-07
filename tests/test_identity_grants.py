"""C5 merge evidence (T11; T37 clause 5): a sub-agent or job never exceeds its
caller's scope, and the test domain cannot read Josh's records. The clinical
EHR root is unreachable by any Potter identity, even with an explicit grant.
Offline only: the EHR root is an opaque ID; nothing contacts Notion."""
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from identity import (CLINICAL_EHR_ROOT, AccessDecision, CapabilityBinding, Grant,  # noqa: E402
                      GrantError, GrantResolver, Hop, IdentityError, Principal,
                      PrincipalDirectory, PrincipalKind, Resource, Scope, ScopedPolicyEngine,
                      SecretRef, ServiceAccount, build_policy_engine, seed_directory)
from identity.seed import CHRISTIE, JOSH, SEED_SCOPES  # noqa: E402
from policy import (ALLOW, DENY, Caller, Capability, CapabilityRegistry,  # noqa: E402
                    DelegationHop, PolicyRequest, RiskClass)
from policy._contracts import approval as c0  # noqa: E402

EHR = CLINICAL_EHR_ROOT.root_id
EHR_HYPHEN = "2fba667c-6316-80c5-a852-c40b076e3c46"
R = RiskClass
NOW = 1_800_000_000
TRUSTED = ("principal_direct",)
TASK_ID = "00000000-0000-4000-8000-000000000001"   # canonical UUID (C1 invalid_task_id gate)

CAPS = (
    Capability("josh.mail.read", R.READ, False, False),
    Capability("josh.notes.read", R.READ, False, False),
    Capability("josh.files.read", R.READ, False, False),
    Capability("josh.notion.read", R.READ, False, False),
    Capability("household.calendar.read", R.READ, False, False),
    Capability("test.files.read", R.READ, False, False),
    Capability("test.notes.write", R.REVERSIBLE_WRITE, False, False),
    Capability("unbound.thing", R.READ, False, False),
)
BINDINGS = (
    CapabilityBinding("josh.mail.read", "josh/mail/*"),
    CapabilityBinding("josh.notes.read", "josh/notes/*"),
    CapabilityBinding("josh.files.read", "josh/files/*", target_is_resource=True),
    CapabilityBinding("josh.notion.read", "josh/notion/*", target_is_resource=True),
    CapabilityBinding("household.calendar.read", "household/calendar/*"),
    CapabilityBinding("test.files.read", "test/files/*", target_is_resource=True),
    CapabilityBinding("test.notes.write", "test/notes/*"),
)
ALL = frozenset(c.name for c in CAPS)
JOSH_CAPS = frozenset(c for c in ALL if c.startswith(("josh.", "household.")))
TEST_CAPS = frozenset(c for c in ALL if c.startswith("test."))


def grants(**extra):
    g = [Grant(pid, ALL, scopes) for pid, scopes in SEED_SCOPES.items()]
    g.append(Grant(CHRISTIE, ALL, ("household/*",)))
    return g + list(extra.get("more", ()))


def resolver(more=(), directory=None, deny_roots=(), ancestry=None):
    return GrantResolver(directory or seed_directory(), grants(more=more), BINDINGS,
                         deny_roots=deny_roots, ancestry=ancestry)


def engine(res=None, **kw):
    res = res or resolver()
    return build_policy_engine(res, registry=CapabilityRegistry(CAPS),
                               ledger=c0.MemoryApprovalLedger(), authenticator=None,
                               clock=lambda: NOW, **kw)


def request(cap, principal, chain=(), target="target:1"):
    return PolicyRequest(caller=Caller(principal, tuple(chain)), capability=cap,
                         action=c0.BoundAction(TASK_ID, cap, "read", target, {"k": "v"}),
                         provenance=TRUSTED)


JOSH_RESOURCES = ("josh/mail/msg-1", "josh/notes/n-1", "josh/files/a/b.txt",
                  "josh/calendar/evt-9", "josh/notion/abc123")
TEST_RESOURCES = ("test/files/f-1", "test/notes/n-2", "test/mail/m-3")


class Principals(unittest.TestCase):
    def test_seed_ids_are_stable_and_typed(self):
        d = seed_directory()
        self.assertEqual(d.get(JOSH).kind, PrincipalKind.HUMAN)
        self.assertFalse(d.get(CHRISTIE).enabled)
        self.assertEqual(d.service_account("gmail").principal_id, "svc:gmail")
        for p in d:
            self.assertTrue(p.principal_id.startswith(p.kind.value + ":"))

    def test_id_prefix_must_match_kind(self):
        with self.assertRaises(IdentityError):
            Principal("agent:josh", PrincipalKind.HUMAN)
        with self.assertRaises(IdentityError):
            Principal("Josh", PrincipalKind.HUMAN)

    def test_secret_ref_is_a_handle_only(self):
        SecretRef("secretref://keychain/potter-gmail-oauth")
        for bad in ("ya29.a0AfH6SMBx-real-token", "secretref://keychain/has space",
                    "sk-ant-api03-abcdef", "secretref://keychain/" + "x" * 200, b"bytes"):
            with self.assertRaises(IdentityError):
                SecretRef(bad)
        self.assertFalse(any(n in dir(SecretRef) for n in ("value", "resolve", "reveal")))

    def test_service_account_needs_secret_ref_not_value(self):
        with self.assertRaises(IdentityError):
            ServiceAccount("svc:gmail", "gmail", JOSH, "plaintext-password")

    def test_one_service_account_per_connector(self):
        ps = (Principal(JOSH, PrincipalKind.HUMAN), Principal("svc:a", PrincipalKind.SERVICE_ACCOUNT),
              Principal("svc:b", PrincipalKind.SERVICE_ACCOUNT))
        ref = SecretRef("secretref://keychain/x")
        with self.assertRaises(IdentityError):
            PrincipalDirectory(ps, (ServiceAccount("svc:a", "gmail", JOSH, ref),
                                    ServiceAccount("svc:b", "gmail", JOSH, ref)))

    def test_service_accounts_never_originate(self):
        res = resolver(more=[Grant("svc:gmail", ALL, ("josh/*",))])
        self.assertNotIn("svc:gmail", res.policy_grants())
        self.assertEqual(res.can_access("svc:gmail", (), "josh/mail/msg-1").reason_code,
                         "not_a_caller")


class Scopes(unittest.TestCase):
    def test_malformed_scopes_rejected(self):
        for bad in ("*", "*/josh", "", "josh/../test/*", "josh//x", "evil/*", "josh/*/x",
                    "josh/a b", 7):
            with self.assertRaises(Exception, msg=repr(bad)):
                Scope(bad)

    def test_coverage(self):
        self.assertTrue(Scope("josh/*").covers_scope(Scope("josh/mail/*")))
        self.assertTrue(Scope("josh/*").covers_scope(Scope("josh/mail/x")))
        self.assertFalse(Scope("josh/mail/*").covers_scope(Scope("josh/*")))
        self.assertFalse(Scope("josh/mail/x").covers_scope(Scope("josh/mail/*")))
        self.assertFalse(Scope("test/*").covers_scope(Scope("josh/mail/*")))
        self.assertFalse(Scope("josh/mail/*").covers_path(("josh", "mailbox", "x")))


class FakeAncestry:
    """Test double for the trusted, injected AncestryResolver (a connector's own
    page-tree lookup). Unknown resources -> None."""

    def __init__(self, tree):
        self.tree = dict(tree)
        self.calls = []

    def ancestors_of(self, resource):
        self.calls.append(resource.resource_id)
        return self.tree.get(resource.resource_id)


TREE = FakeAncestry({
    "josh/notion/abc123": ("ws",),
    "josh/notion/abc": ("ws",),
    # A descendant whose ID reveals nothing about its ancestry.
    "josh/notion/child-page-1": ("p2", EHR_HYPHEN, "ws"),
    f"josh/notion/{EHR}": ("ws",),
})


class DenyRoots(unittest.TestCase):
    def setUp(self):
        # Josh gets an *explicit* grant naming the EHR root and the whole notion tree.
        self.more = [Grant(JOSH, ALL, (f"josh/notion/{EHR}", "josh/notion/*"))]
        self.res = resolver(more=self.more, ancestry=TREE)

    def test_root_itself_denied_even_with_explicit_grant(self):
        for rid in (EHR, EHR_HYPHEN, EHR.upper()):
            d = self.res.can_access(JOSH, (), f"josh/notion/{rid}")
            self.assertEqual((d.allowed, d.reason_code), (False, "deny_root"), rid)

    def test_descendants_denied(self):
        d = self.res.can_access(JOSH, (), "josh/notion/child-page-1")
        self.assertEqual(d.reason_code, "deny_root")
        d = self.res.can_access(JOSH, (), Resource(f"josh/notion/{EHR}/sub/page"))
        self.assertEqual(d.reason_code, "deny_root")
        # A caller *claiming* the EHR root as ancestor is denied (claims can only deny).
        d = self.res.can_access(JOSH, (), Resource("josh/notion/abc", (EHR,)))
        self.assertEqual(d.reason_code, "deny_root")

    def test_known_outside_resource_allowed(self):
        self.assertTrue(self.res.can_access(JOSH, (), "josh/notion/abc123").allowed)

    def test_unknown_to_resolver_denied(self):
        self.assertEqual(self.res.can_access(JOSH, (), "josh/notion/never-seen").reason_code,
                         "ancestry_unverified")

    def test_no_resolver_fails_closed(self):
        res = resolver(more=self.more)       # no AncestryResolver injected
        for r in ("josh/notion/abc123", "josh/notion/abc", "josh/files/notion/x",
                  "josh/unknownstore/x", "josh/x"):
            self.assertEqual(res.can_access(JOSH, (), r).reason_code, "ancestry_unverified", r)
        # Positively-outside stores still work without a resolver.
        self.assertTrue(res.can_access(JOSH, (), "josh/mail/msg-1").allowed)

    def test_forged_ancestry_regression(self):
        # Dolores review: a descendant whose ID does not reveal ancestry, with a
        # caller-claimed "verified" flag and innocent-looking ancestors, is denied.
        with self.assertRaises(TypeError):
            Resource("josh/notion/child-page-1", ancestors=("ws",), ancestry_verified=True)
        forged = Resource("josh/notion/child-page-1", ancestors=("ws",))
        self.assertEqual(self.res.can_access(JOSH, (), forged).reason_code, "deny_root")
        no_resolver = resolver(more=self.more)
        self.assertEqual(no_resolver.can_access(JOSH, (), forged).reason_code,
                         "ancestry_unverified")
        scoped = ScopedPolicyEngine(engine(self.res), self.res)
        d = scoped.decide(request("josh.notion.read", JOSH, target="josh/notion/child-page-1"),
                          forged)
        self.assertEqual(d.outcome, DENY)
        scoped_t = ScopedPolicyEngine(engine(no_resolver), no_resolver, test_mode=True)
        d = scoped_t.decide(request("josh.notion.read", JOSH,
                                    target="josh/notion/child-page-1"), forged)
        self.assertEqual(d.outcome, DENY)

    def test_resolver_garbage_fails_closed(self):
        class Bad:
            def __init__(self, out):
                self.out = out

            def ancestors_of(self, r):
                if isinstance(self.out, Exception):
                    raise self.out
                return self.out
        for out, code in ((["ws"], "ancestry_unverified"), (("ws", 3), "ancestry_unverified"),
                          (("",), "ancestry_unverified"), (RuntimeError("x"), "engine_error")):
            res = resolver(more=self.more, ancestry=Bad(out))
            self.assertEqual(res.can_access(JOSH, (), "josh/notion/abc").reason_code, code)

    def test_every_identity_denied(self):
        res = Resource(f"josh/notion/{EHR}")
        for pid in seed_directory().ids() | {"human:nobody"}:
            self.assertFalse(self.res.can_access(pid, (), res).allowed, pid)

    def test_ehr_root_cannot_be_configured_away(self):
        r = GrantResolver(seed_directory(), grants(), BINDINGS, deny_roots=())
        self.assertIn(CLINICAL_EHR_ROOT, r.deny_roots)
        with self.assertRaises(GrantError):
            GrantResolver(seed_directory(), grants(), BINDINGS, outside_stores={"notion"})

    def test_scoped_engine_requires_resolver_or_explicit_test_mode(self):
        res = resolver()
        with self.assertRaises(ValueError):
            ScopedPolicyEngine(engine(res), res)
        with self.assertRaises(ValueError):
            ScopedPolicyEngine(engine(res), res, test_mode=1)
        ScopedPolicyEngine(engine(res), res, test_mode=True)
        ScopedPolicyEngine(engine(self.res), self.res)

    def test_scoped_engine_denies_ehr_target_before_c1(self):
        scoped = ScopedPolicyEngine(engine(self.res), self.res)
        d = scoped.decide(request("josh.notion.read", JOSH, target=f"josh/notion/{EHR}"))
        self.assertEqual((d.outcome, d.reason_code), (DENY, "not_granted"))
        ok = scoped.decide(request("josh.notion.read", JOSH, target="josh/notion/abc"))
        self.assertEqual(ok.outcome, ALLOW)


class GrantPairing(unittest.TestCase):
    """Dolores review: grants are (capability, scope) pairs end to end."""
    CROSS = (
        Capability("notes.write", R.REVERSIBLE_WRITE, False, False),
        Capability("mail.read", R.READ, False, False),
    )
    CROSS_BINDINGS = (CapabilityBinding("notes.write", "josh/notes/*", True),
                      CapabilityBinding("mail.read", "josh/mail/*", True))

    def make(self, grants_):
        res = GrantResolver(seed_directory(), grants_, self.CROSS_BINDINGS)
        e = build_policy_engine(res, registry=CapabilityRegistry(self.CROSS),
                                ledger=c0.MemoryApprovalLedger(), authenticator=None,
                                clock=lambda: NOW)
        return res, e

    def test_independent_grants_never_combine(self):
        res, _ = self.make([Grant(JOSH, {"notes.write"}, {"josh/notes/*"}),
                            Grant(JOSH, {"mail.read"}, {"josh/mail/*"})])
        self.assertTrue(res.can_access(JOSH, (), "josh/notes/n", "notes.write").allowed)
        self.assertTrue(res.can_access(JOSH, (), "josh/mail/m", "mail.read").allowed)
        self.assertFalse(res.can_access(JOSH, (), "josh/mail/m", "notes.write").allowed)
        self.assertFalse(res.can_access(JOSH, (), "josh/notes/n", "mail.read").allowed)

    def test_cross_paired_grants_confer_neither_capability(self):
        # notes.write only on mail, mail.read only on notes: neither covers its binding.
        res, e = self.make([Grant(JOSH, {"notes.write"}, {"josh/mail/*"}),
                            Grant(JOSH, {"mail.read"}, {"josh/notes/*"})])
        self.assertEqual(res.policy_grants()[JOSH], frozenset())
        for cap in ("notes.write", "mail.read"):
            self.assertEqual(e.decide(request(cap, JOSH)).reason_code, "not_granted")

    def test_hop_narrows_pairs_not_unions(self):
        # Origin: notes.write@notes + mail.read@mail. Hop: the cross-paired opposite.
        # A union-based intersection would let both through; pairwise, nothing does.
        res, e = self.make([
            Grant(JOSH, {"notes.write"}, {"josh/notes/*"}),
            Grant(JOSH, {"mail.read"}, {"josh/mail/*"}),
            Grant("agent:research", {"notes.write"}, {"josh/mail/*"}),
            Grant("agent:research", {"mail.read"}, {"josh/notes/*"})])
        chain = (Hop("agent:research", "subagent"),)
        for r in ("josh/notes/n", "josh/mail/m"):
            d = res.can_access(JOSH, chain, r)
            self.assertEqual((d.allowed, d.reason_code), (False, "out_of_scope"), r)
        hop = res.delegation_hop("agent:research", "subagent")
        self.assertEqual(hop.grants, frozenset())
        for cap in ("notes.write", "mail.read"):
            self.assertEqual(e.decide(request(cap, JOSH, (hop,))).reason_code, "not_granted")

    def test_hop_capability_narrowing(self):
        res, _ = self.make([Grant(JOSH, {"notes.write", "mail.read"}, {"josh/*"}),
                            Grant("agent:potter", {"notes.write", "mail.read"}, {"josh/*"})])
        chain = (Hop("agent:potter", "subagent", capabilities=frozenset({"mail.read"})),)
        self.assertTrue(res.can_access(JOSH, chain, "josh/mail/m", "mail.read").allowed)
        self.assertFalse(res.can_access(JOSH, chain, "josh/notes/n", "notes.write").allowed)
        self.assertFalse(res.can_access(JOSH, chain, "josh/notes/n").allowed)


class TestDomainIsolation(unittest.TestCase):
    def test_test_principals_cannot_read_josh(self):
        res = resolver()
        for pid in ("test:fixture-runner", "test:fixture-subagent"):
            for r in JOSH_RESOURCES + ("household/calendar/e",):
                self.assertFalse(res.can_access(pid, (), r).allowed, (pid, r))
            self.assertEqual(res.policy_grants()[pid], TEST_CAPS)

    def test_test_principal_with_josh_scope_grant_is_quarantined(self):
        res = resolver(more=[Grant("test:fixture-runner", ALL, ("josh/*",))])
        self.assertNotIn("test:fixture-runner", res.policy_grants())
        self.assertEqual(res.can_access("test:fixture-runner", (), "test/files/f").reason_code,
                         "principal_quarantined")

    def test_josh_chain_through_test_subagent_cannot_read_josh(self):
        res = resolver()
        chain = (Hop("test:fixture-subagent", "subagent"),)
        for r in JOSH_RESOURCES:
            self.assertFalse(res.can_access(JOSH, chain, r).allowed)


class Delegation(unittest.TestCase):
    def test_hop_cannot_widen(self):
        res = resolver()
        # research sub-agent only has josh/notes/*; under it, mail is out of scope.
        chain = (Hop("agent:research", "subagent"),)
        self.assertTrue(res.can_access(JOSH, chain, "josh/notes/n-1").allowed)
        self.assertEqual(res.can_access(JOSH, chain, "josh/mail/msg-1").reason_code,
                         "out_of_scope")
        # A hop that *claims* wider scopes than its caller still cannot widen.
        wide = (Hop("agent:potter", "subagent", frozenset({Scope("josh/*")})),)
        r2 = resolver(more=[Grant("agent:potter", ALL, ("josh/*",))])
        self.assertFalse(r2.can_access("job:nightly-digest", (Hop("agent:potter", "subagent",
                         frozenset({Scope("josh/*")})),), "josh/notes/n-1").allowed)
        self.assertTrue(r2.can_access(JOSH, wide, "josh/notes/n-1").allowed)

    def test_c1_hop_from_resolver_cannot_widen(self):
        res = resolver()
        hop = res.delegation_hop("agent:research", "subagent", narrow_to=ALL)
        self.assertEqual(hop.grants, frozenset({"josh.notes.read", "household.calendar.read"}))
        e = engine(res)
        self.assertEqual(e.decide(request("josh.mail.read", JOSH, (hop,))).reason_code,
                         "not_granted")
        self.assertEqual(e.decide(request("josh.notes.read", JOSH, (hop,))).outcome, ALLOW)

    def test_wrong_kind_hops_invalid(self):
        res = resolver()
        for hop in (Hop("job:nightly-digest", "subagent"), Hop("agent:potter", "cron"),
                    Hop(JOSH, "retry"), Hop("agent:nope", "subagent"), Hop("agent:potter", "fork"),
                    Hop("svc:gmail", "retry")):
            self.assertEqual(res.can_access(JOSH, (hop,), "josh/notes/n").reason_code,
                             "invalid_delegation", hop)
            self.assertEqual(res.delegation_hop(hop.agent_id, hop.kind).grants, frozenset())

    def test_depth_limit(self):
        res = resolver()
        chain = (Hop("agent:potter", "subagent"),) * 9
        self.assertEqual(res.can_access(JOSH, chain, "josh/notes/n").reason_code,
                         "invalid_delegation")

    def test_property_random_chains_never_exceed_caller(self):
        rng = random.Random(20261106)
        res = resolver()
        e = engine(res)
        origins = [JOSH, "agent:potter", "agent:research", "job:nightly-digest",
                   "test:fixture-runner"]
        hop_pool = [("agent:potter", "subagent"), ("agent:research", "subagent"),
                    ("job:nightly-digest", "cron"), ("job:retry-worker", "cron"),
                    ("job:retry-worker", "retry"), ("agent:potter", "retry"),
                    ("test:fixture-subagent", "subagent"), ("test:fixture-subagent", "cron")]
        narrows = [None, frozenset({Scope("josh/*")}), frozenset({Scope("josh/notes/*")}),
                   frozenset({Scope("test/*")}), frozenset({Scope("household/*")}), frozenset()]
        resources = JOSH_RESOURCES[:4] + TEST_RESOURCES + ("household/calendar/e",)
        pg = res.policy_grants(frozenset(ALL))
        for _ in range(1500):
            origin = rng.choice(origins)
            chain = []
            for _ in range(rng.randint(0, 6)):
                aid, kind = rng.choice(hop_pool)
                chain.append(Hop(aid, kind, rng.choice(narrows)))
            r = rng.choice(resources)
            allowed = res.can_access(origin, chain, r).allowed
            # Monotone: every prefix of the chain (incl. the bare caller) allows it too.
            if allowed:
                for k in range(len(chain)):
                    self.assertTrue(res.can_access(origin, chain[:k], r).allowed,
                                    (origin, chain, r, k))
            # Test domain never reaches josh/*, anywhere in any chain.
            if origin.startswith("test:") or any(h.agent_id.startswith("test:") for h in chain):
                if r.startswith(("josh/", "household/")):
                    self.assertFalse(allowed, (origin, chain, r))
            # C1 side: effective grants through resolver-built hops never exceed the caller.
            c1_chain = tuple(res.delegation_hop(h.agent_id, h.kind,
                                                rng.choice([None, ALL, JOSH_CAPS, TEST_CAPS]))
                             for h in chain)
            cap = rng.choice(sorted(ALL))
            d = e.decide(request(cap, origin, c1_chain))
            if d.outcome == ALLOW:
                self.assertIn(cap, pg[origin])
                for h in c1_chain:
                    self.assertIn(cap, h.grants)

    def test_forged_c1_hop_cannot_widen(self):
        # A request-supplied DelegationHop claiming everything is still intersected.
        e = engine()
        forged = DelegationHop("test:fixture-subagent", "subagent", ALL)
        d = e.decide(request("josh.mail.read", "test:fixture-runner", (forged,)))
        self.assertEqual((d.outcome, d.reason_code), (DENY, "not_granted"))


class FailClosed(unittest.TestCase):
    def test_unknown_principal(self):
        res = resolver()
        self.assertEqual(res.can_access("human:mallory", (), "josh/mail/m").reason_code,
                         "unknown_principal")
        self.assertEqual(res.can_access(None, (), "josh/mail/m").reason_code, "unknown_principal")
        d = engine(res).decide(request("josh.mail.read", "human:mallory"))
        self.assertEqual((d.outcome, d.reason_code), (DENY, "unknown_principal"))

    def test_disabled_placeholder(self):
        res = resolver()
        self.assertEqual(res.can_access(CHRISTIE, (), "household/calendar/e").reason_code,
                         "principal_disabled")
        self.assertNotIn(CHRISTIE, res.policy_grants())
        d = engine(res).decide(request("household.calendar.read", CHRISTIE))
        self.assertEqual(d.reason_code, "unknown_principal")

    def test_malformed_grant_constructors_raise(self):
        for bad in (dict(principal_id="josh", capabilities={"a"}, scopes={"josh/*"}),
                    dict(principal_id=JOSH, capabilities="josh.mail.read", scopes={"josh/*"}),
                    dict(principal_id=JOSH, capabilities={""}, scopes={"josh/*"}),
                    dict(principal_id=JOSH, capabilities={"a"}, scopes="josh/*"),
                    dict(principal_id=JOSH, capabilities={"a"}, scopes={"*"}),
                    dict(principal_id=JOSH, capabilities={"a"}, scopes={"nope/*"})):
            with self.assertRaises(GrantError, msg=bad):
                Grant(**bad)

    def test_malformed_grant_quarantines_principal(self):
        for raw in ({"principal": JOSH, "capabilities": ["josh.mail.read"], "scopes": ["*"]},
                    {"principal": JOSH, "capabilities": "josh.mail.read", "scopes": ["josh/*"]},
                    {"principal": JOSH, "capabilities": ["x"], "scopes": ["josh/*"], "extra": 1}):
            res = resolver(more=[raw])
            self.assertNotIn(JOSH, res.policy_grants())
            self.assertEqual(res.can_access(JOSH, (), "josh/mail/m").reason_code,
                             "principal_quarantined")
            d = engine(res).decide(request("josh.mail.read", JOSH))
            self.assertEqual(d.reason_code, "unknown_principal")
            # And a hop through the quarantined principal fails too.
            self.assertEqual(res.delegation_hop(JOSH, "subagent").grants, frozenset())

    def test_valid_config_grant(self):
        res = resolver(more=[{"principal": "agent:research", "capabilities": ["josh.mail.read"],
                              "scopes": ["josh/mail/*"]}])
        self.assertIn("josh.mail.read", res.policy_grants()["agent:research"])

    def test_unbound_or_unregistered_capability_never_granted(self):
        res = resolver(more=[Grant(JOSH, {"unbound.thing", "ghost.cap"}, ("josh/*",))])
        g = res.policy_grants(frozenset(ALL))[JOSH]
        self.assertNotIn("unbound.thing", g)
        self.assertNotIn("ghost.cap", g)

    def test_malformed_resource_and_chain(self):
        res = resolver()
        for bad in ("josh/../test/x", "/josh/x", "josh/mail/*", 42, None, "nope/x"):
            self.assertEqual(res.can_access(JOSH, (), bad).reason_code, "malformed_resource", bad)
        self.assertEqual(res.can_access(JOSH, ("not-a-hop",), "josh/mail/m").reason_code,
                         "invalid_delegation")
        self.assertEqual(res.can_access(JOSH, (Hop("agent:potter", "subagent", {"josh/*"}),),
                                        "josh/mail/m").reason_code, "invalid_delegation")
        self.assertEqual(res.can_access(JOSH, 5, "josh/mail/m").reason_code, "engine_error")

    def test_access_decision_reason_table_closed(self):
        with self.assertRaises(KeyError):
            AccessDecision(True, "made-up")

    def test_build_rejects_external_grants(self):
        with self.assertRaises(TypeError):
            build_policy_engine(resolver(), registry=CapabilityRegistry(CAPS), grants={},
                                ledger=c0.MemoryApprovalLedger(), authenticator=None,
                                clock=lambda: NOW)


class EndToEndWithC1(unittest.TestCase):
    def test_test_domain_subagent_denied_josh_capability(self):
        res = resolver()
        e = engine(res)
        hop = res.delegation_hop("test:fixture-subagent", "subagent", narrow_to=ALL)
        self.assertTrue(hop.grants <= TEST_CAPS)
        for cap in sorted(JOSH_CAPS):
            for origin in (JOSH, "test:fixture-runner"):
                d = e.decide(request(cap, origin, (hop,)))
                self.assertEqual((d.outcome, d.reason_code), (DENY, "not_granted"),
                                 (cap, origin))
        # Sanity: the same sub-agent may read its own domain.
        d = e.decide(request("test.files.read", "test:fixture-runner", (hop,),
                             target="test/files/f-1"))
        self.assertEqual(d.outcome, ALLOW)

    def test_scoped_engine_blocks_cross_domain_target(self):
        res = resolver()
        scoped = ScopedPolicyEngine(engine(res), res, test_mode=True)
        # test-scoped capability aimed at a Josh resource: denied before C1.
        d = scoped.decide(request("test.files.read", "test:fixture-runner",
                                  target="josh/files/a.txt"))
        self.assertEqual((d.outcome, d.reason_code), (DENY, "not_granted"))
        # Josh capability, Josh target, via a narrower sub-agent: denied.
        d = scoped.decide(request("josh.files.read", JOSH,
                                  (res.delegation_hop("agent:research", "subagent"),),
                                  target="josh/files/a.txt"))
        self.assertEqual(d.outcome, DENY)
        ok = scoped.decide(request("josh.files.read", JOSH, target="josh/files/a.txt"))
        self.assertEqual(ok.outcome, ALLOW)

    def test_scoped_engine_trace_has_no_target(self):
        res = resolver()
        seen = []
        scoped = ScopedPolicyEngine(engine(res), res, trace_hook=seen.append, test_mode=True)
        scoped.decide(request("test.files.read", "test:fixture-runner",
                              target="josh/files/secret-plan.txt"))
        self.assertEqual(seen[0]["identity_reason_code"], "out_of_scope")
        self.assertNotIn("secret-plan", repr(seen))


if __name__ == "__main__":
    unittest.main()
