"""Fencing epoch and node promotion (package C4, decision T35 item 10).

C2 checks leases and in-flight windows inside the journal DB only, so a
dispatcher that stalls past its window (GC pause, VM freeze, partition) and then
resumes can still land an effect after another node has taken over. C4 closes
that with a monotonic **fencing epoch**:

* :class:`EpochAuthority` is a small SQLite file that stands in for the shared
  authority (in Phase F, whatever survives the loss of either container). It
  holds one row ``(epoch, holder)``. :meth:`EpochAuthority.promote` is a
  compare-and-set: it increments the epoch only if the caller saw the current
  one, so concurrent promotions have exactly one winner and epochs never repeat.
* :class:`Node` is one runtime instance. Every activation takes a fresh epoch,
  so an older incarnation of the same node is fenced like any other node.
  Lifecycle::

      start ──▶ FENCED ──rejoin() (authority still names us)──┐
                  │                                           ▼
                  └──promote() (takeover, CAS)────────────▶ PAUSED ──unpause(reconcile)──▶ ACTIVE
      any state ──authority epoch moved past ours──▶ FENCED

  A node always starts FENCED: a returning node does not assume it is still
  primary, and a node restored from a replica (:class:`FileCopyReplicator`) can
  leave FENCED only by promoting. A promoted node starts PAUSED: it may not
  dispatch until :meth:`Node.unpause` has reconciled every ``unknown`` effect in
  its journal (C2 ``unresolved()``).
* :class:`FencedEffects` wraps :class:`~runtime.effects.EffectJournal`. Every
  dispatch carries the node's :class:`FencingToken`; it is checked before the
  journal is touched, inside the journal's claim transaction, and just before
  the external call. The token is passed to ``perform(effect, token)`` so the
  connector can check it too.
* :class:`StubConnector` is the external-side check ("fencing token at the
  boundary"). Its state lives in the authority's DB, and the check plus the
  apply run inside :meth:`EpochAuthority.boundary`, the same ``BEGIN
  IMMEDIATE`` lock that promotion takes. It refuses any token below the
  highest epoch it has seen, or not equal to the committed epoch. Promotion and
  apply are therefore strictly ordered: once a promotion commits, no older
  token can land, and the new primary's reconcile sees everything that did.

Limits (documented, not hidden): third-party APIs cannot check our tokens, so
the boundary in production is an egress gateway with this check, and a window
remains between the gateway's check and the third party applying the call. The
deterministic C2 idempotency key, passed to the third party, is the backstop
for that window. Staging-only (T22); nothing here performs a real effect.
"""
from __future__ import annotations

import abc
import ast
import builtins
import contextlib
import dataclasses
import multiprocessing.synchronize
import threading
import hashlib
import hmac
import inspect
import json
import os
import re
import reprlib
import sqlite3
import sys
import time
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol

from .db import Database, as_database
from .effects import (DEFAULT_DISPATCH_TIMEOUT_S, UNKNOWN, Effect, EffectConflict, EffectError,
                      adapter_name,
                      EffectFailed, EffectInFlight, EffectJournal)

FENCED, PAUSED, ACTIVE = "fenced", "paused", "active"
NODE_STATES = (FENCED, PAUSED, ACTIVE)

_META_EPOCH = "fencing.epoch"
_META_NODE = "fencing.node_id"
_META_RESTORED = "fencing.restored_from"
_OWNER_RE = re.compile(r"@epoch:(\d+)$")


# --------------------------------------------------------------------------- errors

class FencingError(EffectError):
    pass


class StaleEpoch(EffectFailed, FencingError):
    """Refused because the dispatching epoch is not the authority's current one.
    Subclasses EffectFailed: a refused dispatch definitely did not apply."""


class StaleEpochRefused(StaleEpoch):
    """Refused at the external boundary (connector side)."""


class NodeNotActive(FencingError):
    """The node is FENCED or PAUSED; it may not dispatch effects."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


class PromotionConflict(FencingError):
    """Another node promoted first (compare-and-set lost)."""


class UnresolvedEffects(FencingError):
    """Unpause refused: these effects are still ``unknown`` after reconciliation."""

    def __init__(self, keys: list[str]):
        super().__init__(f"{len(keys)} effect(s) still unresolved; node stays paused")
        self.keys = keys


class AuthorityUnavailable(FencingError):
    """The epoch authority cannot be read; callers fail closed."""


# --------------------------------------------------------------------------- authority

@dataclass(frozen=True)
class Epoch:
    epoch: int
    holder: str | None


@dataclass(frozen=True)
class FencingToken:
    node_id: str
    epoch: int

    @property
    def owner(self) -> str:
        """The C2 in-flight owner string; records the epoch with the dispatch."""
        return f"{self.node_id}@epoch:{self.epoch}"


def owner_epoch(owner: str | None) -> int:
    """Epoch encoded in a C2 in-flight owner; 0 for pre-C4 (untagged) owners."""
    m = _OWNER_RE.search(owner or "")
    return int(m.group(1)) if m else 0


class _AuthorityTx:
    """``BEGIN IMMEDIATE`` on the authority DB. A plain class rather than a
    ``contextmanager`` generator, so the code that runs is exactly the code
    the boundary verifier checks (no decorator closure in between)."""

    def __init__(self, authority: "EpochAuthority"):
        self._authority = authority
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        c = self._authority._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
        except BaseException:
            c.close()
            raise
        self._conn = c
        return c

    def __exit__(self, exc_type, exc, tb) -> bool:
        c, self._conn = self._conn, None
        try:
            c.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            c.close()
        return False


class _BoundaryTx:
    """The authority's boundary transaction: ``_AuthorityTx`` plus the
    committed epoch read inside it; sqlite errors surface as
    ``AuthorityUnavailable``."""

    def __init__(self, authority: "EpochAuthority"):
        self._tx = _AuthorityTx(authority)

    def __enter__(self) -> tuple[sqlite3.Connection, "Epoch"]:
        try:
            c = self._tx.__enter__()
        except sqlite3.Error as exc:
            raise AuthorityUnavailable(f"epoch authority boundary failed: {exc}") from exc
        try:
            row = c.execute("SELECT epoch, holder FROM authority WHERE id = 1").fetchone()
            if row is None:
                raise AuthorityUnavailable("epoch authority has no row")
            return c, Epoch(int(row[0]), row[1])
        except BaseException as exc:
            self._tx.__exit__(type(exc), exc, exc.__traceback__)
            if isinstance(exc, sqlite3.Error):
                raise AuthorityUnavailable(f"epoch authority boundary failed: {exc}") from exc
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._tx.__exit__(exc_type, exc, tb)
        except sqlite3.Error as err:
            raise AuthorityUnavailable(f"epoch authority boundary failed: {err}") from err
        if exc_type is not None and issubclass(exc_type, sqlite3.Error):
            raise AuthorityUnavailable(f"epoch authority boundary failed: {exc}") from exc
        return False


class EpochAuthority:
    """Monotonic epoch with compare-and-set promotion, in one SQLite file."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time,
                 busy_timeout_s: float = 30.0):
        self.path = str(path)
        self._clock = clock
        self._timeout = busy_timeout_s
        try:
            with self._tx() as c:
                c.execute("CREATE TABLE IF NOT EXISTS authority ("
                          " id INTEGER PRIMARY KEY CHECK (id = 1),"
                          " epoch INTEGER NOT NULL CHECK (epoch >= 0),"
                          " holder TEXT, updated_at REAL NOT NULL)")
                c.execute("CREATE TABLE IF NOT EXISTS promotions ("
                          " epoch INTEGER PRIMARY KEY, holder TEXT NOT NULL,"
                          " promoted_at REAL NOT NULL)")
                c.execute("INSERT OR IGNORE INTO authority (id, epoch, holder, updated_at)"
                          " VALUES (1, 0, NULL, ?)", (self._clock(),))
        except sqlite3.Error as exc:
            raise AuthorityUnavailable(f"epoch authority unusable: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        return c

    def _tx(self) -> "_AuthorityTx":
        """One ``BEGIN IMMEDIATE`` write transaction (committed only if the
        block completes)."""
        return _AuthorityTx(self)

    def boundary(self) -> "_BoundaryTx":
        """One ``BEGIN IMMEDIATE`` transaction on the authority DB, with the
        committed epoch read inside it. :meth:`promote` takes the same write
        lock, so whatever runs in this block linearizes with promotion: no
        promotion can commit between the epoch read and the block's commit.
        The external boundary (:class:`StubConnector`) keeps its state in this
        DB and checks + applies here. Yields ``(connection, Epoch)``."""
        return _BoundaryTx(self)

    def current(self) -> Epoch:
        try:
            c = self._connect()
            try:
                row = c.execute("SELECT epoch, holder FROM authority WHERE id = 1").fetchone()
            finally:
                c.close()
        except sqlite3.Error as exc:
            raise AuthorityUnavailable(f"epoch authority unreadable: {exc}") from exc
        if row is None:
            raise AuthorityUnavailable("epoch authority has no row")
        return Epoch(int(row[0]), row[1])

    def promote(self, node_id: str, expected_epoch: int) -> int:
        """CAS: move to ``expected_epoch + 1`` held by ``node_id`` only if the
        authority is still at ``expected_epoch``. Returns the new epoch."""
        if not node_id or not isinstance(node_id, str):
            raise ValueError("node_id required")
        if not isinstance(expected_epoch, int) or isinstance(expected_epoch, bool) \
                or expected_epoch < 0:
            raise ValueError("expected_epoch must be a non-negative int")
        now = self._clock()
        try:
            with self._tx() as c:
                cur = c.execute("UPDATE authority SET epoch = epoch + 1, holder = ?,"
                                " updated_at = ? WHERE id = 1 AND epoch = ?",
                                (node_id, now, expected_epoch))
                if cur.rowcount != 1:
                    raise PromotionConflict(
                        f"authority moved past epoch {expected_epoch}; promotion lost")
                c.execute("INSERT INTO promotions (epoch, holder, promoted_at) VALUES (?,?,?)",
                          (expected_epoch + 1, node_id, now))
        except sqlite3.Error as exc:
            raise AuthorityUnavailable(f"promotion failed: {exc}") from exc
        return expected_epoch + 1

    def history(self) -> list[Epoch]:
        c = self._connect()
        try:
            rows = c.execute("SELECT epoch, holder FROM promotions ORDER BY epoch").fetchall()
        finally:
            c.close()
        return [Epoch(int(e), h) for e, h in rows]


def authority_scope_id(authority: "EpochAuthority") -> str:
    """Stable, non-secret identifier of an epoch authority for a FencingScope."""
    return "sqlite:" + os.path.realpath(authority.path)


# --------------------------------------------------------------------------- boundary registry

DEFAULT_FENCING_SEMANTICS = (
    "refuses any token below the epoch authority's committed epoch; the check and"
    " the external apply are one transaction linearized with promotion")
_HEX64 = frozenset("0123456789abcdef")


class BoundaryVerificationError(ValueError):
    """A fenced boundary's adapter or bound scope cannot be verified against its
    declared contract. Mirrors C2's ``AdapterVerificationError``."""


# Artifact verification mirrors C2's ``adapter_artifact_digest`` /
# ``_require_loaded_matches_source`` (claude/c2-task-effect-journal@fdd3137),
# which is not yet on claude/c-integration. Same algorithm and semantics; when
# that C2 change is integrated, these two helpers become imports from effects.
def boundary_artifact_digest(adapter: Any) -> str:
    """sha256 of the adapter's implementation: the bytes of the source file
    defining its function, plus (if different) the file defining the class it
    is bound to. Raises BoundaryVerificationError if a source file cannot be
    read (builtins, C extensions, REPL/exec code), or if the LOADED function is
    not structurally what that file compiles to (rewritten after import, or
    patched in memory)."""
    owner = getattr(adapter, "__self__", None)
    func = getattr(adapter, "__func__", adapter)
    objs = [func if hasattr(func, "__code__") else type(func)]
    if owner is not None:
        objs.append(owner if isinstance(owner, type) else type(owner))
    files: list[str] = []
    for obj in objs:
        try:
            path = inspect.getsourcefile(obj)
        except (TypeError, OSError):
            path = None
        if not path or not os.path.isfile(path):
            raise BoundaryVerificationError(f"no readable source for adapter {adapter_name(adapter)!r}")
        path = os.path.realpath(path)
        if path not in files:
            files.append(path)
    parts, sources = [], {}
    for path in files:
        try:
            with open(path, "rb") as f:
                sources[path] = f.read()
        except OSError as exc:
            raise BoundaryVerificationError(f"cannot read adapter source {path}: {exc}") from exc
        parts.append(hashlib.sha256(sources[path]).hexdigest())
    code = getattr(objs[0], "__code__", None)
    if code is not None:
        _require_loaded_matches_source(code, sources[files[0]], adapter)
    return parts[0] if len(parts) == 1 else hashlib.sha256(":".join(parts).encode()).hexdigest()


def _require_loaded_matches_source(code: Any, source: bytes, adapter: Any) -> None:
    try:
        module_code = compile(source, code.co_filename, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        raise BoundaryVerificationError(f"adapter source does not compile: {exc}") from exc
    # Structural code equality (bytecode, consts incl. nested code, names,
    # flags, first line, line and exception tables), as in C2.
    stack = [module_code]
    while stack:
        c = stack.pop()
        if c.co_qualname == code.co_qualname and c.co_filename == code.co_filename \
                and c.co_firstlineno == code.co_firstlineno and c == code:
            return
        stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
    raise BoundaryVerificationError(
        f"loaded adapter {adapter_name(adapter)!r} differs from its source file")


_DATACLASS_GENERATED = frozenset({
    "__init__", "__repr__", "__eq__", "__hash__", "__setattr__", "__delattr__", "__lt__",
    "__le__", "__gt__", "__ge__", "__replace__", "__getstate__", "__setstate__"})
_PROTOCOL_GENERATED = frozenset({"__init__", "__subclasshook__"})
_CONTEXTMANAGER_HELPER = contextlib.contextmanager(lambda: iter(())).__code__
_DATACLASS_ORIGINS = frozenset(os.path.realpath(m.__file__) for m in (dataclasses, reprlib))


def _foreign_ok(cls: type, name: str, code: types.CodeType) -> bool:
    """Methods the class machinery generates (there is no source to compare):
    dataclass-generated dunders on dataclasses, Protocol's __init__ /
    __subclasshook__ from typing. Residual, documented: these are checked by
    origin and name, not by content."""
    origin = code.co_filename
    if dataclasses.is_dataclass(cls) and name in _DATACLASS_GENERATED:
        return origin == "<string>" or os.path.realpath(origin) in _DATACLASS_ORIGINS
    if getattr(cls, "_is_protocol", False) and name in _PROTOCOL_GENERATED:
        return os.path.realpath(origin) == os.path.realpath(typing.__file__)
    return False


def verify_boundary_implementation(adapter: Any) -> None:
    """Every loaded callable that can supply the boundary must still be what
    the reviewed artifact compiles to, not just ``perform``.

    For each module defining the adapter's function or its bound class, the
    module source is compiled and compared, structurally (as in C2), with:
    every module-level function and class the source defines (same object at
    the same name, nothing removed); every method of those classes (plain,
    static, class, property parts; ``contextmanager``-wrapped functions are
    unwrapped and the wrapper must be contextlib's own helper); and no
    function or method the source does not define. For a bound adapter, the
    instance's class must be the module's own class at that name, and no
    instance attribute may shadow a class attribute or hold a callable (an
    instance-level callable injects behavior). Raises
    BoundaryVerificationError. Imported modules are NOT covered here."""
    owner = getattr(adapter, "__self__", None)
    func = getattr(adapter, "__func__", adapter)
    modnames = {getattr(func, "__module__", None)}
    cls_of_owner = None
    if owner is not None:
        cls_of_owner = owner if isinstance(owner, type) else type(owner)
        modnames.add(cls_of_owner.__module__)
    for modname in modnames:
        _verify_module(modname, adapter)
    if cls_of_owner is not None:
        home = sys.modules.get(cls_of_owner.__module__)
        if home is None or getattr(home, cls_of_owner.__qualname__, None) is not cls_of_owner:
            raise BoundaryVerificationError(
                f"adapter class {cls_of_owner.__qualname__!r} is not its module's reviewed class")
        if not isinstance(owner, type):
            for k, v in vars(owner).items():
                if any(k in vars(c) for c in cls_of_owner.__mro__):
                    raise BoundaryVerificationError(f"instance attribute {k!r} shadows a class attribute")
                if callable(v):
                    raise BoundaryVerificationError(f"instance attribute {k!r} holds a callable")
                _verify_collaborator(k, v, modnames)


def _verify_collaborator(name: str, obj: Any, modnames: set) -> None:
    """An instance attribute whose class comes from a verified module (e.g. the
    stub's EpochAuthority) must be exactly that module's class, with no
    instance attribute shadowing its methods. (Its own injected callables,
    like a clock, are allowed: they are not on the check-and-apply path.)"""
    cls = type(obj)
    if not any(c.__module__ in modnames for c in cls.__mro__):
        return                              # not a boundary collaborator class
    home = sys.modules.get(cls.__module__)
    if cls.__module__ not in modnames:
        raise BoundaryVerificationError(f"collaborator {name!r} is a subclass from an unverified module")
    if home is None or getattr(home, cls.__qualname__, None) is not cls:
        raise BoundaryVerificationError(f"collaborator {name!r} is not its module's reviewed class")
    for k in getattr(obj, "__dict__", {}):
        if any(k in vars(c) for c in cls.__mro__):
            raise BoundaryVerificationError(f"collaborator {name!r}: attribute {k!r} shadows a class attribute")


def _verify_module(modname: str | None, adapter: Any) -> None:
    mod = sys.modules.get(modname) if modname else None
    path = getattr(mod, "__file__", None) if mod is not None else None
    if not path or not os.path.isfile(path):
        raise BoundaryVerificationError(f"no readable module source for adapter {adapter_name(adapter)!r}")
    modfile = os.path.realpath(path)
    try:
        with open(modfile, "rb") as f:
            source = f.read()
        tree = compile(source, modfile, "exec", dont_inherit=True)
    except (OSError, SyntaxError, ValueError) as exc:
        raise BoundaryVerificationError(f"cannot compile adapter module source: {exc}") from exc
    funcs: dict[str, list[types.CodeType]] = {}
    classes: set[str] = set()
    stack = list(k for k in tree.co_consts if isinstance(k, types.CodeType))
    while stack:
        c = stack.pop()
        stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
        if "<" in c.co_qualname:
            continue                        # lambdas, comprehensions, locals
        if c.co_flags & inspect.CO_NEWLOCALS:
            funcs.setdefault(c.co_qualname, []).append(c)
        else:
            classes.add(c.co_qualname)      # class body

    def same(code: types.CodeType, qualname: str) -> bool:
        return (os.path.realpath(code.co_filename) == modfile
                and any(code == e and code.co_firstlineno == e.co_firstlineno
                        for e in funcs.get(qualname, ())))

    def check_func(f: Any, qualname: str, cls: type | None, name: str) -> None:
        outer = getattr(f, "__code__", None)
        if cls is not None and qualname not in funcs and outer is not None \
                and _foreign_ok(cls, name, outer):
            return                          # generated by the class machinery
        inner = getattr(f, "__wrapped__", None)
        if inner is not None:
            if getattr(f, "__code__", None) != _CONTEXTMANAGER_HELPER:
                raise BoundaryVerificationError(f"{qualname} is wrapped by an unreviewed decorator")
            f = inner
        code = getattr(f, "__code__", None)
        if code is None:
            raise BoundaryVerificationError(f"{qualname} has no code to verify")
        if same(code, qualname):
            return
        raise BoundaryVerificationError(f"loaded {qualname} differs from the reviewed source")

    def check_class(cls: Any, qualname: str) -> None:
        if not (isinstance(cls, type) and cls.__qualname__ == qualname and cls.__module__ == modname):
            raise BoundaryVerificationError(f"class {qualname} is not the reviewed class")
        present = set()
        for name, v in vars(cls).items():
            parts: list[Any] = []
            if isinstance(v, types.FunctionType):
                parts = [v]
            elif isinstance(v, (staticmethod, classmethod)):
                parts = [v.__func__]
            elif isinstance(v, property):
                parts = [x for x in (v.fget, v.fset, v.fdel) if x is not None]
            elif isinstance(v, type) and v.__qualname__ == f"{qualname}.{name}":
                check_class(v, v.__qualname__)
                continue
            for part in parts:
                check_func(part, f"{qualname}.{name}", cls, name)
                present.add(name)
        for q in funcs:
            if q.startswith(qualname + ".") and "." not in q[len(qualname) + 1:]:
                if q[len(qualname) + 1:] not in present:
                    raise BoundaryVerificationError(f"reviewed method {q} is missing")

    for q in funcs:
        if "." not in q:
            f = vars(mod).get(q)
            if not isinstance(f, types.FunctionType):
                raise BoundaryVerificationError(f"reviewed function {q} is missing or replaced")
            check_func(f, q, None, q)
    for q in classes:
        if "." not in q:
            check_class(vars(mod).get(q), q)
    for name, v in vars(mod).items():
        if isinstance(v, types.FunctionType) and v.__module__ == modname \
                and "<" not in v.__qualname__ and name not in funcs:
            raise BoundaryVerificationError(f"unreviewed function {name} added to the module")
    _verify_manifest(mod, modname, source)


# ----- manifest: nothing in the module or its classes beyond what the source binds

_MODULE_MACHINERY = frozenset({"__name__", "__doc__", "__package__", "__loader__", "__spec__",
                               "__file__", "__cached__", "__builtins__", "__annotations__",
                               "__annotate__", "__conditional_annotations__"})
_CLASS_MACHINERY = frozenset({"__module__", "__qualname__", "__doc__", "__dict__", "__weakref__",
                              "__firstlineno__", "__static_attributes__", "__annotations__",
                              "__annotate__", "__annotate_func__", "__annotations_cache__",
                              "__type_params__", "__orig_bases__", "__parameters__"})
_DATACLASS_MACHINERY = frozenset({"__dataclass_fields__", "__dataclass_params__", "__match_args__"})
_ABC_MACHINERY = frozenset({"_abc_impl", "__abstractmethods__"})
_PROTOCOL_MACHINERY = frozenset({"_is_protocol", "_is_runtime_protocol", "__protocol_attrs__",
                                 "__non_callable_proto_members__",
                                 "__callable_proto_members_only__"})
# Names that change attribute lookup / construction / identity: never accepted
# unless the reviewed class body itself defines them (then they are verified).
_LOOKUP_SENSITIVE = frozenset({"__getattribute__", "__getattr__", "__setattr__", "__delattr__",
                               "__get__", "__set__", "__delete__", "__set_name__",
                               "__init_subclass__", "__class_getitem__", "__slots__", "__new__",
                               "__call__", "__instancecheck__", "__subclasscheck__",
                               "__mro_entries__", "__prepare__", "__class__", "__dir__"})


class _Unverifiable(Exception):
    pass


@dataclass(frozen=True)
class _Unpacked:
    """Element ``index`` of a tuple-unpacking assignment."""
    node: ast.Assign
    index: int


def _assigned_value(src: Any, mod: types.ModuleType, trusted: dict[str, Any]) -> Any:
    if isinstance(src, _Unpacked):
        seq = _static_value(src.node.value, mod, trusted)
        if not isinstance(seq, (tuple, list)):
            raise _Unverifiable("unpacking a non-sequence")
        return seq[src.index]
    return _static_value(src.value, mod, trusted)


def _bound_names(body: list[ast.stmt]) -> tuple[dict[str, ast.AST], dict[str, ast.AST]]:
    """(names bound by def/class/assign in ``body`` -> defining node,
        names bound by import -> import node)"""
    defs: dict[str, ast.AST] = {}
    imports: dict[str, ast.AST] = {}
    for n in body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[n.name] = n
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    defs[t.id] = n
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for i, e in enumerate(t.elts):     # a, b = x, y  ->  element i
                        if isinstance(e, ast.Name):
                            defs[e.id] = _Unpacked(n, i)
        elif isinstance(n, ast.AnnAssign) and n.value is not None and isinstance(n.target, ast.Name):
            defs[n.target.id] = n
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                imports[a.asname or a.name.split(".")[0]] = n
    return defs, imports


def _import_target(node: ast.AST, alias: ast.alias, mod: types.ModuleType) -> Any:
    if isinstance(node, ast.Import):
        if alias.asname:
            return sys.modules[alias.name]
        return sys.modules[alias.name.split(".")[0]]
    base = node.module or ""
    if node.level:
        pkg = (mod.__package__ or "").split(".")
        pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
        base = ".".join([*pkg, base] if base else pkg)
    src = sys.modules[base]
    return vars(src)[alias.name] if alias.name in vars(src) else sys.modules[f"{base}.{alias.name}"]


def _static_value(node: ast.AST, mod: types.ModuleType, trusted: dict[str, Any]) -> Any:
    """Evaluate a module-level expression made only of literals, verified
    names, attribute access on verified modules, tuples/lists/sets, and
    ``frozenset(<static>)`` / ``re.compile(<literal>)``. Anything else is
    _Unverifiable."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = [_static_value(e, mod, trusted) for e in node.elts]
        return tuple(items) if isinstance(node, ast.Tuple) else (
            items if isinstance(node, ast.List) else set(items))
    if isinstance(node, ast.Name):
        if node.id in trusted:
            return trusted[node.id]
        if node.id not in vars(mod) and node.id in vars(builtins):
            return vars(builtins)[node.id]        # unshadowed builtin
        raise _Unverifiable(node.id)
    if isinstance(node, ast.Attribute):
        base = _static_value(node.value, mod, trusted)
        if isinstance(base, types.ModuleType) and node.attr in vars(base):
            return vars(base)[node.attr]
        raise _Unverifiable(ast.unparse(node))
    if isinstance(node, ast.Call) and not node.keywords:
        f = node.func
        if isinstance(f, ast.Name) and f.id == "frozenset" and "frozenset" not in vars(mod) \
                and len(node.args) == 1:
            return frozenset(_static_value(node.args[0], mod, trusted))
        if isinstance(f, ast.Attribute) and f.attr == "compile" and isinstance(f.value, ast.Name) \
                and trusted.get(f.value.id) is sys.modules.get("re") \
                and all(isinstance(a, ast.Constant) for a in node.args):
            return re.compile(*[a.value for a in node.args])
    raise _Unverifiable(ast.unparse(node))


def _same_value(live: Any, expected: Any) -> bool:
    if isinstance(expected, (type, types.ModuleType, types.FunctionType, types.CodeType)):
        return live is expected
    if isinstance(expected, re.Pattern):
        return type(live) is re.Pattern and (live.pattern, live.flags) == (expected.pattern, expected.flags)
    if isinstance(expected, (tuple, list)):
        return type(live) is type(expected) and len(live) == len(expected) and all(
            _same_value(a, b) for a, b in zip(live, expected))
    if isinstance(expected, (set, frozenset)):
        if type(live) is not type(expected) or len(live) != len(expected):
            return False
        if all(isinstance(x, (type, types.ModuleType)) for x in expected):
            return {id(x) for x in live} == {id(x) for x in expected}
        return live == expected
    return type(live) is type(expected) and live == expected


def _verify_manifest(mod: types.ModuleType, modname: str, source: bytes) -> None:
    tree = ast.parse(source)
    defs, imports = _bound_names(tree.body)
    live = vars(mod)
    unknown = set(live) - set(defs) - set(imports) - _MODULE_MACHINERY
    if unknown:
        raise BoundaryVerificationError(f"unreviewed module attribute(s) {sorted(unknown)}")
    b = live.get("__builtins__")
    if b is not builtins and b is not vars(builtins):
        raise BoundaryVerificationError("module __builtins__ was replaced")
    trusted: dict[str, Any] = {}
    for name, node in imports.items():
        alias = next(a for a in node.names if (a.asname or a.name.split(".")[0]) == name)
        try:
            expected = _import_target(node, alias, mod)
        except (KeyError, ValueError) as exc:
            raise BoundaryVerificationError(f"import binding {name!r} cannot be resolved") from exc
        if live.get(name) is not expected:
            raise BoundaryVerificationError(f"import binding {name!r} was rebound")
        trusted[name] = expected
    for name, node in defs.items():
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            trusted[name] = live.get(name)       # verified structurally elsewhere
    for name, node in defs.items():              # module-level assigned values
        if not isinstance(node, (ast.Assign, ast.AnnAssign, _Unpacked)):
            continue
        try:
            expected = _assigned_value(node, mod, trusted)
        except _Unverifiable:
            continue                             # snapshotted at registry build instead
        if name not in live or not _same_value(live[name], expected):
            raise BoundaryVerificationError(f"module value {name!r} differs from the reviewed source")
        trusted[name] = live[name]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            _verify_class_manifest(live.get(node.name), node, mod, trusted)
    _verify_function_objects(mod, tree, trusted)


# ----- function objects: what actually runs is code + globals + closure + defaults

def _first_line(node: ast.AST) -> int:
    return min([node.lineno, *[d.lineno for d in getattr(node, "decorator_list", [])]])


def _expected_defaults(node: ast.FunctionDef, mod: types.ModuleType,
                       scope: dict[str, Any]) -> tuple[tuple, dict]:
    try:
        pos = tuple(_static_value(d, mod, scope) for d in node.args.defaults)
        kw = {a.arg: _static_value(d, mod, scope)
              for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d is not None}
    except _Unverifiable as exc:
        raise BoundaryVerificationError(f"default of {node.name} cannot be verified: {exc}") from exc
    return pos, kw


def _same_defaults(f: Any, pos: tuple, kw: dict) -> bool:
    live_pos = f.__defaults__ or ()
    live_kw = f.__kwdefaults__ or {}
    return (len(live_pos) == len(pos) and all(_same_value(a, b) for a, b in zip(live_pos, pos))
            and set(live_kw) == set(kw) and all(_same_value(live_kw[k], kw[k]) for k in kw))


def _cell_values(f: Any, what: str) -> list[tuple[str, Any]]:
    cells = f.__closure__ or ()
    if len(cells) != len(f.__code__.co_freevars):
        raise BoundaryVerificationError(f"{what}: closure does not match its code")
    out = []
    for name, cell in zip(f.__code__.co_freevars, cells):
        try:
            out.append((name, cell.cell_contents))
        except ValueError:
            raise BoundaryVerificationError(f"{what}: empty closure cell {name!r}") from None
    return out


def _check_source_function(f: Any, node: ast.FunctionDef, cls: type | None,
                           mod: types.ModuleType, scope: dict[str, Any], what: str) -> None:
    """A source-defined function: globals are the module's, the only closure
    cell is the ``__class__`` cell holding the owning class, and the defaults
    equal the source's default expressions."""
    if f.__globals__ is not vars(mod):
        raise BoundaryVerificationError(f"{what}: globals are not the module's")
    for name, value in _cell_values(f, what):
        if not (name == "__class__" and cls is not None and value is cls):
            raise BoundaryVerificationError(f"{what}: unexpected closure cell {name!r}")
    if not _same_defaults(f, *_expected_defaults(node, mod, scope)):
        raise BoundaryVerificationError(f"{what}: defaults differ from the reviewed source")


def _unwrap_reviewed(f: Any, what: str) -> Any:
    """contextlib.contextmanager wrappers run the function in their single
    ``func`` closure cell, NOT ``__wrapped__``: that cell must hold exactly the
    wrapped (and separately verified) function."""
    inner = getattr(f, "__wrapped__", None)
    if inner is None:
        return f
    if f.__code__ != _CONTEXTMANAGER_HELPER or f.__globals__ is not vars(contextlib) \
            or f.__defaults__ or f.__kwdefaults__:
        raise BoundaryVerificationError(f"{what}: wrapped by an unreviewed decorator")
    cells = _cell_values(f, what)
    if len(cells) != 1 or cells[0][0] != "func" or cells[0][1] is not inner:
        raise BoundaryVerificationError(f"{what}: the wrapper runs something other than __wrapped__")
    return inner


def _generated_globals_ok(f: Any, mod: types.ModuleType) -> bool:
    origin = f.__code__.co_filename
    if origin == "<string>":
        return f.__globals__ is vars(mod)
    for m in (dataclasses, reprlib, typing):
        if os.path.realpath(origin) == os.path.realpath(m.__file__):
            return f.__globals__ is vars(m)
    return False


def _generated_cell_ok(name: str, value: Any, cls: type) -> bool:
    if name in ("__class__", "cls"):
        return value is cls
    if name == "__dataclass_builtins_object__":
        return value is builtins.object
    if name == "FrozenInstanceError":
        return value is dataclasses.FrozenInstanceError
    if name == "fillvalue":
        return type(value) is str and value == "..."
    if name == "repr_running":
        return type(value) is set
    return False


def _check_generated(f: Any, cls: type, name: str, mod: types.ModuleType,
                     class_defaults: list[Any], what: str) -> None:
    """A method the class machinery generated: expected globals for its origin,
    only the known cells (with their expected values), and no defaults except
    a dataclass ``__init__``'s, which must be the class's source defaults."""
    if not _generated_globals_ok(f, mod):
        raise BoundaryVerificationError(f"{what}: generated method has unexpected globals")
    for cell, value in _cell_values(f, what):
        if cell == "user_function":
            if not (isinstance(value, types.FunctionType) and value.__code__.co_filename == "<string>"):
                raise BoundaryVerificationError(f"{what}: unexpected repr target")
            _check_generated(value, cls, name, mod, [], what + " [inner]")
        elif not _generated_cell_ok(cell, value, cls):
            raise BoundaryVerificationError(f"{what}: unexpected closure cell {cell!r}")
    values = list(f.__defaults__ or ()) + list((f.__kwdefaults__ or {}).values())
    if name == "__init__" and dataclasses.is_dataclass(cls):
        sentinel = getattr(dataclasses, "_HAS_DEFAULT_FACTORY", None)
        for v in values:
            if not (v is sentinel or any(_same_value(v, d) for d in class_defaults)):
                raise BoundaryVerificationError(f"{what}: generated default differs from the source")
    elif values:
        raise BoundaryVerificationError(f"{what}: generated method has unexpected defaults")


def _verify_function_objects(mod: types.ModuleType, tree: ast.Module,
                             trusted: dict[str, Any]) -> None:
    live = vars(mod)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            f = live.get(node.name)
            if not isinstance(f, types.FunctionType):
                raise BoundaryVerificationError(f"function {node.name} is missing")
            f = _unwrap_reviewed(f, node.name)
            _check_source_function(f, node, None, mod, trusted, node.name)
        elif isinstance(node, ast.ClassDef):
            cls = live.get(node.name)
            scope = dict(trusted)                 # class-body scope for method defaults
            class_defaults: list[Any] = []
            for b in node.body:
                if isinstance(b, (ast.Assign, ast.AnnAssign)) and b.value is not None:
                    try:
                        val = _static_value(b.value, mod, scope)
                    except _Unverifiable:
                        continue
                    class_defaults.append(val)
                    targets = b.targets if isinstance(b, ast.Assign) else [b.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            scope[t.id] = val
            defined = set()
            for b in node.body:
                if not isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                defined.add(b.name)
                v = vars(cls).get(b.name)
                parts = ([v] if isinstance(v, types.FunctionType)
                         else [v.__func__] if isinstance(v, (staticmethod, classmethod))
                         else [x for x in (v.fget, v.fset, v.fdel) if x is not None]
                         if isinstance(v, property) else [])
                what = f"{node.name}.{b.name}"
                matched = False
                for part in parts:
                    part = _unwrap_reviewed(part, what)
                    if part.__code__.co_firstlineno == _first_line(b):
                        _check_source_function(part, b, cls, mod, scope, what)
                        matched = True
                if not matched:
                    raise BoundaryVerificationError(f"{what}: reviewed definition not found")
            for key, v in vars(cls).items():
                if key in defined:
                    continue
                if isinstance(v, types.FunctionType):
                    _check_generated(v, cls, key, mod, class_defaults, f"{node.name}.{key}")


def _verify_class_manifest(cls: Any, node: ast.ClassDef, mod: types.ModuleType,
                           trusted: dict[str, Any]) -> None:
    name = node.name
    if not isinstance(cls, type):
        raise BoundaryVerificationError(f"class {name} is missing")
    # 1. type identity: bases, metaclass, MRO
    if node.keywords:
        raise BoundaryVerificationError(f"class {name} uses class keywords: unverifiable")
    try:
        bases = types.resolve_bases(tuple(_static_value(b, mod, trusted)
                                          for b in node.bases)) or (object,)
    except _Unverifiable as exc:
        raise BoundaryVerificationError(f"class {name} has unverifiable bases") from exc
    if len(cls.__bases__) != len(bases) or any(a is not b for a, b in zip(cls.__bases__, bases)):
        raise BoundaryVerificationError(f"class {name} bases were swapped")
    meta = type
    for b in bases:
        if issubclass(type(b), meta):
            meta = type(b)
    if type(cls) is not meta:
        raise BoundaryVerificationError(f"class {name} metaclass was swapped")
    if cls.__mro__ != tuple(type.mro(cls)):
        raise BoundaryVerificationError(f"class {name} has a non-standard MRO")
    # 2. manifest of the class namespace
    defs, _ = _bound_names(node.body)
    allowed = set(defs) | _CLASS_MACHINERY
    if dataclasses.is_dataclass(cls):
        allowed |= _DATACLASS_MACHINERY | _DATACLASS_GENERATED
    if isinstance(cls, abc.ABCMeta):
        allowed |= _ABC_MACHINERY
    if getattr(cls, "_is_protocol", False):
        allowed |= _PROTOCOL_MACHINERY | _PROTOCOL_GENERATED
    for key, v in vars(cls).items():
        if key not in allowed:
            raise BoundaryVerificationError(f"unreviewed class attribute {name}.{key}")
        if key in _LOOKUP_SENSITIVE and key not in defs and not (
                dataclasses.is_dataclass(cls) and key in _DATACLASS_GENERATED
                and isinstance(v, types.FunctionType)):
            raise BoundaryVerificationError(f"lookup-affecting attribute {name}.{key}")
        if isinstance(v, (types.FunctionType, staticmethod, classmethod, property)):
            continue                              # verified structurally by check_class
        if isinstance(v, type) and v.__qualname__ == f"{name}.{key}":
            continue                              # nested class, verified by check_class
        if key in ("__dict__", "__weakref__"):
            if type(v).__name__ != "getset_descriptor" or v.__objclass__ is not cls:
                raise BoundaryVerificationError(f"{name}.{key} is not the class's own descriptor")
            continue
        if key == "__hash__" and v is None:
            continue
        if callable(v) or hasattr(type(v), "__get__") or hasattr(type(v), "__set__"):
            raise BoundaryVerificationError(f"class attribute {name}.{key} is a callable or descriptor")
        src = defs.get(key)
        if isinstance(src, (ast.Assign, ast.AnnAssign, _Unpacked)):
            try:
                expected = _assigned_value(src, mod, trusted)
            except _Unverifiable:
                continue
            if not _same_value(v, expected):
                raise BoundaryVerificationError(f"class value {name}.{key} differs from the reviewed source")


def unverifiable_module_values(adapter: Any) -> dict[str, Any]:
    """Module-level values the static check cannot evaluate (verifier
    internals such as the contextmanager helper's code). The registry
    snapshots their identity at build; later rebinding is caught live."""
    out: dict[str, Any] = {}
    owner = getattr(adapter, "__self__", None)
    func = getattr(adapter, "__func__", adapter)
    names = {getattr(func, "__module__", None)}
    if owner is not None:
        names.add((owner if isinstance(owner, type) else type(owner)).__module__)
    for modname in sorted(n for n in names if n):
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        with open(os.path.realpath(mod.__file__), "rb") as f:
            tree = ast.parse(f.read())
        defs, imports = _bound_names(tree.body)
        trusted = {k: vars(mod).get(k) for k in [*imports, *defs]}
        for name, node in defs.items():
            if isinstance(node, (ast.Assign, ast.AnnAssign, _Unpacked)):
                try:
                    _assigned_value(node, mod, trusted)
                except _Unverifiable:
                    out[f"{modname}.{name}"] = vars(mod).get(name)
    return out


@dataclass(frozen=True)
class FencingScope:
    """What a fenced boundary is bound to, mirroring C2's ``IdempotencyScope``.
    Non-secret identifiers and a config hash, never credentials. The fence
    guarantee holds only within this scope: the boundary must enforce tokens
    from THIS authority, in THIS store, under THIS check version and config."""
    boundary: str          # boundary kind, e.g. "potter.stub-epoch-gateway"
    authority: str         # the epoch authority whose committed epoch it checks
    store: str             # where the check + apply are made atomic
    check_version: str     # version of the token-check contract
    config_hash: str       # sha256 of the boundary's fencing-relevant config

    def __post_init__(self):
        for name in ("boundary", "authority", "store", "check_version"):
            v = getattr(self, name)
            if not (isinstance(v, str) and v):
                raise ValueError(f"fencing scope {name} required")
        h = self.config_hash
        if not (isinstance(h, str) and len(h) == 64 and set(h) <= _HEX64):
            raise ValueError("config_hash must be 64 lowercase hex characters")

    def canonical(self) -> list:
        return [self.boundary, self.authority, self.store, self.check_version, self.config_hash]


def current_fencing_scope(adapter: Any) -> FencingScope:
    """Ask the adapter what it is bound to RIGHT NOW: ``fencing_scope()`` on the
    object a bound adapter belongs to, or a module-level ``fencing_scope()``
    beside a plain function. Raises BoundaryVerificationError otherwise."""
    owner = getattr(adapter, "__self__", None)
    report = (getattr(owner, "fencing_scope", None) if owner is not None
              else getattr(adapter, "__globals__", {}).get("fencing_scope"))
    if not callable(report):
        raise BoundaryVerificationError(f"adapter {adapter_name(adapter)!r} cannot report its fencing scope")
    try:
        scope = report()
    except Exception as exc:
        raise BoundaryVerificationError(f"adapter {adapter_name(adapter)!r} failed to report its"
                                        f" fencing scope: {exc!r}") from exc
    if not isinstance(scope, FencingScope):
        raise BoundaryVerificationError(f"adapter {adapter_name(adapter)!r} reported a non-FencingScope")
    return scope


@dataclass(frozen=True)
class FencedBoundaryContract:
    """A reviewed connector boundary that PROVABLY refuses stale fencing tokens.

    Mirrors C2's ``ConnectorContract``:
    * ``adapter``: the exact ``perform(effect, token)`` callable reviewed;
    * ``contract_ref``: where the evidence is;
    * ``adapter_artifact_digest``: sha256 of the reviewed implementation,
      supplied by trusted configuration (see :func:`boundary_artifact_digest`);
    * ``fencing_scope``: the authority / store / config it is bound to;
    * ``fencing_semantics``: the stated guarantee.

    ``contract_digest`` is sha256 over the canonical JSON of all of them plus
    the adapter's qualified name. It is recorded on each effect when dispatched
    through :class:`FencedEffects`. A changed body under the same name, a
    memory patch, or a re-pointed scope is a different (or unverifiable)
    contract and stops vouching."""
    identity: str
    adapter: Callable[[Effect, "FencingToken"], Any]
    contract_ref: str
    adapter_artifact_digest: str
    fencing_scope: FencingScope
    fencing_semantics: str = DEFAULT_FENCING_SEMANTICS
    contract_digest: str = field(init=False)

    def __post_init__(self):
        if not (isinstance(self.identity, str) and self.identity):
            raise ValueError("identity required")
        if not (isinstance(self.contract_ref, str) and self.contract_ref):
            raise ValueError("contract_ref required")
        if not (isinstance(self.fencing_semantics, str) and self.fencing_semantics):
            raise ValueError("fencing_semantics required")
        if not callable(self.adapter):
            raise TypeError("adapter must be callable")
        d = self.adapter_artifact_digest
        if not (isinstance(d, str) and len(d) == 64 and set(d) <= _HEX64):
            raise ValueError("adapter_artifact_digest must be 64 lowercase hex characters")
        if not isinstance(self.fencing_scope, FencingScope):
            raise TypeError("fencing_scope must be a FencingScope")
        canon = json.dumps(["potter.c4.fenced_boundary/v1", self.identity, self.contract_ref,
                            adapter_name(self.adapter), self.adapter_artifact_digest,
                            self.fencing_scope.canonical(), self.fencing_semantics],
                           separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        object.__setattr__(self, "contract_digest", hashlib.sha256(canon).hexdigest())

    def verify_live(self) -> None:
        """Re-check the RUNNING adapter against this contract: its implementation
        artifact, ALL loaded callables of its module(s) (see
        :func:`verify_boundary_implementation`), and its current scope.
        Raises BoundaryVerificationError."""
        actual = boundary_artifact_digest(self.adapter)
        if not hmac.compare_digest(actual, self.adapter_artifact_digest):
            raise BoundaryVerificationError(
                f"adapter for {self.identity!r} does not match its declared artifact digest")
        verify_boundary_implementation(self.adapter)
        if current_fencing_scope(self.adapter) != self.fencing_scope:
            raise BoundaryVerificationError(
                f"adapter for {self.identity!r} is bound to a different fencing scope")


class FencedBoundaryRegistry(Mapping):
    """Frozen identity -> FencedBoundaryContract mapping, given to a
    :class:`Node` at construction. Empty by default: then no effect is ever
    cleared by the fence, and C2 keeps ambiguous effects unresolved.

    Every entry is verified when the registry is built (artifact digest against
    the adapter's actual source, scope against the adapter's live report);
    otherwise construction raises BoundaryVerificationError. Nodes verify again
    before every dispatch and before vouching for a retry."""

    def __init__(self, contracts: Iterable[FencedBoundaryContract] = ()):
        entries: dict[str, FencedBoundaryContract] = {}
        baselines: dict[str, Mapping[str, Any]] = {}
        for k in contracts:
            if not isinstance(k, FencedBoundaryContract):
                raise TypeError("registry entries must be FencedBoundaryContract")
            if k.identity in entries:
                raise ValueError(f"duplicate boundary identity {k.identity!r}")
            k.verify_live()
            entries[k.identity] = k
            baselines[k.identity] = MappingProxyType(unverifiable_module_values(k.adapter))
        self._entries = MappingProxyType(entries)
        self._baselines = MappingProxyType(baselines)

    def baseline_holds(self, identity: str) -> bool:
        """The module values the static check cannot evaluate are still the very
        objects they were when this registry was built (late rebinding)."""
        base = self._baselines.get(identity)
        if base is None:
            return False
        now = unverifiable_module_values(self._entries[identity].adapter)
        return set(now) == set(base) and all(now[k] is base[k] for k in base)

    def __getitem__(self, identity: str) -> FencedBoundaryContract:
        return self._entries[identity]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __setattr__(self, name, value):
        if name not in ("_entries", "_baselines") or name in self.__dict__:
            raise AttributeError("FencedBoundaryRegistry is frozen")
        object.__setattr__(self, name, value)


_BINDINGS_DDL = ("CREATE TABLE IF NOT EXISTS fenced_bindings ("
                 " idempotency_key TEXT PRIMARY KEY, connector TEXT NOT NULL,"
                 " boundary_digest TEXT, bound_at REAL NOT NULL)")


# --------------------------------------------------------------------------- node

def _meta_get(db: Database, key: str) -> str | None:
    with db.read() as c:
        row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(c: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        c.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                  " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


@dataclass(frozen=True)
class Observation:
    current: bool              # authority names this node at the epoch it holds
    authority: Epoch
    held_epoch: int | None
    restored_from: str | None


class Node:
    """One runtime instance. Always starts FENCED (see module docstring).

    The epoch a node last held is persisted in its own journal DB (``meta``), so
    a returning node knows which epoch it had, and a replica restored from that
    DB carries the marker that keeps it fenced."""

    def __init__(self, node_id: str, authority: EpochAuthority, db: Database | str | Path,
                 *, clock: Callable[[], float] = time.time,
                 dispatch_timeout_s: float = DEFAULT_DISPATCH_TIMEOUT_S,
                 boundaries: FencedBoundaryRegistry | None = None):
        if not node_id or not isinstance(node_id, str) or "@" in node_id:
            raise ValueError("node_id required (no '@')")
        if boundaries is None:
            boundaries = FencedBoundaryRegistry()
        if not isinstance(boundaries, FencedBoundaryRegistry):
            raise TypeError("boundaries must be a FencedBoundaryRegistry")
        self.boundaries = boundaries   # fixed for this node's lifetime
        self.node_id = node_id
        self.authority = authority
        self.db = as_database(db)
        with self.db.tx() as c:   # C4-owned table in the journal DB (replicated with it)
            c.execute(_BINDINGS_DDL)
        self.journal = EffectJournal(self.db, clock=clock, dispatch_timeout_s=dispatch_timeout_s,
                                     fence=self.c2_fence)
        self._clock = clock
        self._reconciling = False
        # (idempotency_key, owner judged, decision) for every C2 fence call: audit trail
        self.fence_log: list[tuple[str, str | None, bool]] = []
        held_node = _meta_get(self.db, _META_NODE)
        held = _meta_get(self.db, _META_EPOCH)
        self._epoch: int | None = int(held) if held is not None and held_node == node_id else None
        self._restored = _meta_get(self.db, _META_RESTORED)
        self._state = FENCED
        self.fence_reason = ("restored from replica" if self._restored
                             else "returning node" if self._epoch is not None else "new node")

    # ---- state --------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def epoch(self) -> int | None:
        return self._epoch

    def token(self) -> FencingToken:
        if self._epoch is None:
            raise NodeNotActive(FENCED, "node holds no epoch")
        return FencingToken(self.node_id, self._epoch)

    def _fence(self, reason: str) -> None:
        self._state, self.fence_reason = FENCED, reason

    def observe(self) -> Observation:
        """Read the authority. Demotes to FENCED if the epoch moved past ours."""
        auth = self.authority.current()
        current = (self._epoch is not None and auth.epoch == self._epoch
                   and auth.holder == self.node_id and not self._restored)
        if not current and self._state != FENCED:
            self._fence(f"authority at epoch {auth.epoch}, node held {self._epoch}")
        return Observation(current, auth, self._epoch, self._restored)

    def is_current(self, token: FencingToken | None = None) -> bool:
        """True if ``token`` (default: ours) is the authority's current epoch."""
        token = token or (FencingToken(self.node_id, self._epoch)
                          if self._epoch is not None else None)
        if token is None:
            return False
        auth = self.authority.current()
        return auth.epoch == token.epoch and auth.holder == token.node_id

    # ---- transitions --------------------------------------------------------

    def rejoin(self) -> bool:
        """A returning node leaves FENCED only if the authority still names it at
        the epoch it last held, and then it takes a FRESH epoch (CAS), so any
        stalled earlier incarnation of itself is fenced too. Never steals
        leadership: a superseded or restored node stays FENCED (returns False)."""
        obs = self.observe()
        if not obs.current:
            self._fence("superseded" if not self._restored else "restored from replica")
            return False
        self._take_epoch(obs.authority.epoch)
        return True

    def promote(self) -> int:
        """Takeover: CAS the authority from the epoch we observe to the next one.
        The node starts PAUSED. Raises PromotionConflict if another node won."""
        auth = self.authority.current()
        return self._take_epoch(auth.epoch)

    def _take_epoch(self, expected: int) -> int:
        try:
            new = self.authority.promote(self.node_id, expected)
        except PromotionConflict:
            self._fence("promotion lost")
            raise
        with self.db.tx() as c:
            _meta_set(c, _META_NODE, self.node_id)
            _meta_set(c, _META_EPOCH, str(new))
            _meta_set(c, _META_RESTORED, None)
        self._epoch, self._restored = new, None
        self._state, self.fence_reason = PAUSED, ""
        return new

    def pause(self) -> None:
        if self._state == ACTIVE:
            self._state = PAUSED

    def unpause(self, reconcile: Callable[[Effect], str], *,
                now: float | None = None) -> dict[str, Any]:
        """Reconcile every ``unknown`` effect, then go ACTIVE.

        Live in-flight windows owned by a LOWER epoch, on a proven fenced
        boundary, are ended first (the owner stays on the row): the promotion is committed, so the boundary already
        refuses that dispatcher. C2 then accepts a ``failed`` verdict only
        because :meth:`c2_fence` confirms the earlier dispatcher's epoch is
        older than ours. Windows of our own epoch are respected. If anything is still unknown, the node
        stays PAUSED and :class:`UnresolvedEffects` is raised. ``pending``
        effects (never dispatched) need no reconciliation."""
        if self._state != PAUSED:
            raise NodeNotActive(self._state, f"unpause needs PAUSED, node is {self._state}")
        token = self.token()
        if not self.is_current(token):
            self._fence("superseded before unpause")
            raise NodeNotActive(FENCED, "authority moved past this node's epoch")
        released, resolved, blocked = [], [], []
        self._reconciling = True
        try:
            for eff in self.journal.unresolved():
                if eff.status != UNKNOWN:
                    continue
                if eff.in_flight_owner and owner_epoch(eff.in_flight_owner) < token.epoch \
                        and self._boundary_proven(eff):   # unproven: keep the window
                    if self.journal.expire_in_flight(eff.idempotency_key, eff.in_flight_owner,
                                                     now=now):
                        released.append(eff.idempotency_key)
                try:
                    after = self.journal.reconcile(eff.idempotency_key, reconcile, now=now)
                except EffectInFlight:
                    blocked.append(eff.idempotency_key)
                    continue
                (blocked if after.status == UNKNOWN else resolved).append(eff.idempotency_key)
        finally:
            self._reconciling = False
        if blocked:
            raise UnresolvedEffects(blocked)
        if not self.is_current(token):
            self._fence("superseded during unpause")
            raise NodeNotActive(FENCED, "authority moved past this node's epoch")
        self._state = ACTIVE
        return {"epoch": token.epoch, "released": released, "resolved": resolved}

    # ---- gates --------------------------------------------------------------

    def require_active(self, token: FencingToken | None = None) -> FencingToken:
        """Raise unless ACTIVE and (``token`` or our epoch) is current."""
        if self._state != ACTIVE:
            raise NodeNotActive(self._state, f"node is {self._state}")
        token = token or self.token()
        if token.epoch != self._epoch or not self.is_current(token):
            self._fence(f"epoch {token.epoch} is stale")
            raise StaleEpoch(f"epoch {token.epoch} of {token.node_id} is no longer current")
        return token

    def require_current(self, token: FencingToken) -> None:
        """Epoch-only check (no state check): raises StaleEpoch, which is a
        definite "not applied", so the journal settles the effect FAILED."""
        if not self.is_current(token):
            self._fence(f"epoch {token.epoch} is stale")
            raise StaleEpoch(f"epoch {token.epoch} of {token.node_id} is no longer current")

    def binding(self, key: str) -> tuple[str, str | None] | None:
        """(connector, boundary_digest) recorded for ``key`` by FencedEffects."""
        with self.db.read() as c:
            row = c.execute("SELECT connector, boundary_digest FROM fenced_bindings"
                            " WHERE idempotency_key = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def bind(self, key: str, connector: str, perform: Callable[..., Any]) -> str | None:
        """Record which boundary dispatches ``key``, before any dispatch.

        A registered identity requires ``perform`` to be that contract's
        reviewed adapter, re-verified live (implementation artifact and bound
        scope, which must name this node's authority), and records its digest. An unregistered identity is
        recorded with no digest: it may dispatch, but the fence will never
        clear a re-dispatch for it. A different binding for the same key is
        refused (``EffectConflict``)."""
        contract = self.boundaries.get(connector)
        if contract is not None:
            if perform != contract.adapter:
                raise EffectConflict(f"perform is not the registered fenced adapter for {connector!r}")
            problem = self._contract_problem(contract)
            if problem:
                raise EffectConflict(f"fenced boundary {connector!r} failed live verification: {problem}")
        digest = contract.contract_digest if contract is not None else None
        with self.db.tx() as c:
            row = c.execute("SELECT connector, boundary_digest FROM fenced_bindings"
                            " WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                c.execute("INSERT INTO fenced_bindings (idempotency_key, connector,"
                          " boundary_digest, bound_at) VALUES (?,?,?,?)",
                          (key, connector, digest, self._clock()))
            elif row[0] != connector or row[1] != digest:
                raise EffectConflict(f"effect {key} is bound to boundary {row[0]!r}"
                                     f" (digest {row[1]!r}); refusing {connector!r}")
        return digest

    def _contract_problem(self, contract: FencedBoundaryContract) -> str | None:
        """Why ``contract`` cannot vouch right now (None if it can): the running
        adapter must still match its reviewed artifact, report the same scope,
        and that scope must name THIS node's epoch authority."""
        try:
            contract.verify_live()
        except BoundaryVerificationError as exc:
            return str(exc)
        if contract.fencing_scope.authority != authority_scope_id(self.authority):
            return "scope names a different epoch authority"
        if self.boundaries.get(contract.identity) is not contract \
                or not self.boundaries.baseline_holds(contract.identity):
            return "module values changed since the registry was verified"
        return None

    def _boundary_proven(self, effect: Effect) -> bool:
        """The effect's recorded connector is a registered fenced boundary whose
        contract digest matches the one recorded when it was dispatched, and
        whose adapter and scope still verify live."""
        recorded = getattr(effect, "connector", None)
        bound = self.binding(effect.idempotency_key)
        if recorded is None or bound is None or bound[0] != recorded or bound[1] is None:
            return False
        contract = self.boundaries.get(recorded)
        return (contract is not None
                and hmac.compare_digest(contract.contract_digest, bound[1])
                and self._contract_problem(contract) is None)

    def c2_fence(self, effect: Effect) -> bool:
        """C2 ``EffectJournal(fence=...)`` seam. True only if a late landing of
        the earlier dispatch of ``effect`` is ruled out by the epoch authority:

        * this node is not FENCED, and is ACTIVE (or PAUSED but inside
          :meth:`unpause`, which reconciles and never dispatches);
        * the authority's current epoch is this node's epoch; and
        * the row names the earlier dispatcher (``node@epoch:N``, or a pre-C4
          untagged owner, read as epoch 0), and that epoch is older than ours;
          and
        * the effect's RECORDED connector (C2 ``effect.connector``) is bound,
          with a matching contract digest, to a boundary in this node's
          :class:`FencedBoundaryRegistry`: one that provably refuses tokens
          below the committed epoch, linearized with promotion. Only then can
          that older dispatcher no longer land the effect.

        Anything else returns False and C2 keeps the effect unresolved. That
        includes: an unknown dispatcher (no owner on the row), a dispatcher at
        our own epoch (a timed-out call of ours may still be on the wire), and
        an unproven, unregistered, rotated or unbound connector (an adapter
        that ignores its token is not fenced by any epoch)."""
        owner = getattr(effect, "in_flight_owner", None)
        decision = False
        try:
            if (self._state == ACTIVE or (self._state == PAUSED and self._reconciling)) \
                    and self._epoch is not None and owner \
                    and owner_epoch(owner) < self._epoch \
                    and self._boundary_proven(effect):
                decision = self.is_current()
                if not decision:
                    self._fence("superseded (seen by fence)")
        except Exception:
            decision = False
        self.fence_log.append((getattr(effect, "idempotency_key", "?"), owner, decision))
        return decision

    def policy_state(self) -> str:
        """C1 seam (``PolicyEngine(node_state=...)``): the live node state,
        re-checked against the authority. Fails closed to FENCED."""
        try:
            if self._state != FENCED:
                self.observe()
        except Exception:
            self._fence("epoch authority unreadable")
        return self._state


# --------------------------------------------------------------------------- dispatch

class FencedEffects:
    """Epoch-checked front door to the C2 effect journal.

    ``perform(effect, token)`` receives the node's fencing token and must pass
    it, with ``effect.idempotency_key``, to the external boundary."""

    def __init__(self, node: Node):
        self.node = node
        self.journal = node.journal

    def execute(self, key: str, perform: Callable[[Effect, FencingToken], Any],
                reconcile: Callable[[Effect], str], *, connector: str,
                now: float | None = None) -> Effect:
        """``connector`` is the C2 connector identity (recorded on the effect;
        every dispatch must match it). Before anything is claimed, the key is
        bound to that boundary (:meth:`Node.bind`): for an identity in the
        node's :class:`FencedBoundaryRegistry`, ``perform`` must be the
        registered adapter and the contract digest is recorded. Only such
        effects can later be cleared by :meth:`Node.c2_fence`. Fencing does not
        use C2's trusted idempotent registry or its digest.

        Because ``perform`` is wrapped (to pass the token and re-check the
        epoch), a connector in C2's trusted idempotent registry cannot be
        routed through here: C2 requires ``perform`` to be its own adapter."""
        token = self.node.require_active()
        if not (isinstance(connector, str) and connector):
            raise ValueError("connector identity required")
        self.node.bind(key, connector, perform)
        refused: list[StaleEpoch] = []

        def fence() -> None:
            self.node.require_active(token)

        def fenced_perform(eff: Effect) -> Any:
            try:
                self.node.require_current(token)   # last check before the boundary
                return perform(eff, token)
            except StaleEpoch as exc:
                refused.append(exc)
                raise

        eff = self.journal.execute(key, fenced_perform, reconcile, owner=token.owner,
                                   connector=connector, now=now, guard=fence)
        if refused:   # the journal settled it FAILED (not applied); surface the refusal
            self.node._fence(f"dispatch refused: {refused[0]}")
            raise refused[0]
        return eff


# --------------------------------------------------------------------------- boundary

_GATE_TYPES = (threading.Event, multiprocessing.synchronize.Event)
GATE_TIMEOUT_S = 60.0


def _pass_gate(gate: Any) -> None:
    """Test seam: signal ``reached`` and park until ``resume`` (Events only)."""
    if not (isinstance(gate, tuple) and len(gate) == 2
            and all(type(e) in _GATE_TYPES for e in gate)):
        raise TypeError("a gate is a (reached, resume) pair of Events")
    reached, resume = gate
    reached.set()
    if not resume.wait(GATE_TIMEOUT_S):
        raise ConnectionError("gate never resumed")


class StubConnector:
    """Staging stand-in for the external system behind an epoch-checking egress
    boundary. It does NOT deduplicate on the idempotency key, so any double
    execution is visible.

    Fencing-token pattern, linearized with promotion: the boundary's state
    (highest epoch seen, applied and refused rows) lives in the AUTHORITY's
    SQLite DB, and :meth:`apply` runs entirely inside
    :meth:`EpochAuthority.boundary`, one ``BEGIN IMMEDIATE`` transaction that
    :meth:`EpochAuthority.promote` also needs. In that one transaction it
    refuses a token below the highest epoch seen, or not equal to the
    authority's committed epoch, and otherwise records the apply. So an apply
    and a promotion are strictly ordered: once a promotion has committed, no
    older token lands, and an apply in progress delays the promotion until it
    has committed. ``reconcile`` reads under the same lock.

    Test seams are DATA, not code (a callable seam in the boundary path would be
    a token bypass, and verification refuses any instance-level callable):
    ``gate_before_apply`` / ``gate_in_check`` are ``(reached, resume)`` Event
    pairs (exactly ``threading.Event`` or a multiprocessing Event) that park the
    caller before the boundary transaction (holding no lock) or inside it after
    the token passed; ``fail_before_apply`` is a message that makes
    :meth:`perform` raise ``ConnectionError`` before the boundary (an
    ambiguous transport failure)."""

    IDENTITY = "stub.epoch-gateway"

    def __init__(self, authority: EpochAuthority, *, identity: str = IDENTITY):
        if not isinstance(authority, EpochAuthority):
            raise TypeError("StubConnector needs the EpochAuthority whose DB it shares")
        if not (isinstance(identity, str) and identity):
            raise ValueError("identity required")
        self.identity = identity   # C2 connector identity; NOT trusted-idempotent
        self.authority = authority
        self.gate_before_apply: tuple[Any, Any] | None = None
        self.gate_in_check: tuple[Any, Any] | None = None
        self.fail_before_apply: str | None = None
        with authority.boundary() as (c, _):
            c.execute("CREATE TABLE IF NOT EXISTS ext_applied ("
                      " seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,"
                      " node_id TEXT NOT NULL, epoch INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS ext_fence ("
                      " id INTEGER PRIMARY KEY CHECK (id = 1), max_epoch INTEGER NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS ext_refused ("
                      " seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,"
                      " node_id TEXT NOT NULL, epoch INTEGER NOT NULL, reason TEXT NOT NULL)")
            c.execute("INSERT OR IGNORE INTO ext_fence (id, max_epoch) VALUES (1, 0)")

    def apply(self, key: str, token: FencingToken) -> dict[str, Any]:
        if not isinstance(token, FencingToken):
            raise StaleEpochRefused("no fencing token")
        reason = None
        try:
            with self.authority.boundary() as (c, auth):
                max_seen = c.execute("SELECT max_epoch FROM ext_fence WHERE id = 1").fetchone()[0]
                if token.epoch < max_seen:
                    reason = f"epoch {token.epoch} < highest seen {max_seen}"
                elif token.epoch != auth.epoch or token.node_id != auth.holder:
                    reason = f"epoch {token.epoch} is not the committed epoch {auth.epoch}"
                if reason:
                    c.execute("INSERT INTO ext_refused (key, node_id, epoch, reason)"
                              " VALUES (?,?,?,?)", (key, token.node_id, token.epoch, reason))
                else:
                    if self.gate_in_check is not None:
                        _pass_gate(self.gate_in_check)
                    c.execute("UPDATE ext_fence SET max_epoch = ? WHERE id = 1", (token.epoch,))
                    c.execute("INSERT INTO ext_applied (key, node_id, epoch) VALUES (?,?,?)",
                              (key, token.node_id, token.epoch))
        except AuthorityUnavailable as exc:   # fail closed: nothing was applied
            raise StaleEpochRefused(f"connector refused: {exc}") from exc
        if reason:   # the refusal row is committed before we raise
            raise StaleEpochRefused(f"connector refused: {reason}")
        return {"applied": key, "epoch": token.epoch}

    CHECK_VERSION = "c4.boundary.v1"

    def fencing_scope(self) -> FencingScope:
        """What this boundary is bound to, reported live (read-only): the
        authority whose committed epoch it checks, the store where check and
        apply are atomic (the same SQLite DB), and its fencing config."""
        auth = authority_scope_id(self.authority)
        config = json.dumps({"identity": self.identity, "tables": ["ext_fence", "ext_applied",
                             "ext_refused"], "lock": "BEGIN IMMEDIATE on authority DB",
                             "refuse": ["epoch < max_seen", "epoch != committed",
                                        "node != holder"]}, sort_keys=True)
        return FencingScope("potter.stub-epoch-gateway", auth, auth, self.CHECK_VERSION,
                            hashlib.sha256(config.encode("utf-8")).hexdigest())

    def contract(self, adapter_artifact_digest: str) -> FencedBoundaryContract:
        """This boundary's reviewed fencing contract, for a node's
        :class:`FencedBoundaryRegistry`. ``adapter_artifact_digest`` must come
        from trusted configuration (the digest of the REVIEWED implementation);
        the registry checks it against the running code, so the stub cannot
        vouch for itself. Evidence: tests/test_fencing.py BoundaryLinearization."""
        return FencedBoundaryContract(
            self.identity, self.perform,
            "runtime/fencing.py:StubConnector + tests/test_fencing.py:BoundaryLinearization",
            adapter_artifact_digest, self.fencing_scope())

    # C2 / FencedEffects-compatible callables
    def perform(self, effect: Effect, token: FencingToken) -> dict[str, Any]:
        effect.action()                    # executors run only the journaled action
        if self.fail_before_apply is not None:
            raise ConnectionError(self.fail_before_apply)
        if self.gate_before_apply is not None:
            _pass_gate(self.gate_before_apply)
        return self.apply(effect.idempotency_key, token)

    def perform_unfenced(self, effect: Effect) -> dict[str, Any]:
        """A C2-shaped ``perform(effect)`` with no token: always refused here."""
        return self.apply(effect.idempotency_key, None)  # type: ignore[arg-type]

    def reconcile(self, effect: Effect) -> str:
        return "succeeded" if self.count(effect.idempotency_key) else "failed"

    def count(self, key: str | None = None) -> int:
        with self.authority.boundary() as (c, _):   # serialises with applies and promotions
            if key is None:
                return c.execute("SELECT COUNT(*) FROM ext_applied").fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM ext_applied WHERE key = ?",
                             (key,)).fetchone()[0]

    def applied(self) -> list[tuple[str, str, int]]:
        with self.authority.boundary() as (c, _):
            return c.execute("SELECT key, node_id, epoch FROM ext_applied ORDER BY seq").fetchall()

    def refusals(self) -> list[tuple[str, str, int, str]]:
        with self.authority.boundary() as (c, _):
            return c.execute("SELECT key, node_id, epoch, reason FROM ext_refused"
                             " ORDER BY seq").fetchall()


# --------------------------------------------------------------------------- replication

class Replicator(Protocol):
    """Seam for journal replication (Litestream in Phase F)."""

    def replicate(self, db_path: str | Path) -> str: ...
    def latest(self) -> str | None: ...
    def restore(self, target_path: str | Path, generation: str | None = None) -> Path: ...


class FileCopyReplicator:
    """In-process simulation of Litestream: each ``replicate`` writes a
    consistent snapshot (SQLite online backup, so WAL content is included) to
    ``replica_dir`` as a numbered generation, published by atomic rename.
    ``restore`` materialises a generation at a NEW path and marks it as
    restored, so a :class:`Node` opened on it starts FENCED and cannot
    :meth:`Node.rejoin` (it must promote).

    Like Litestream, a replica can lag: effects journaled after the last
    snapshot are absent from a restore. The C2 idempotency key, passed to the
    external system, is the backstop for re-recorded intents."""

    _PREFIX, _SUFFIX = "gen-", ".sqlite"

    def __init__(self, replica_dir: str | Path):
        self.dir = Path(replica_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _generations(self) -> list[str]:
        return sorted(p.name[len(self._PREFIX):-len(self._SUFFIX)]
                      for p in self.dir.glob(f"{self._PREFIX}*{self._SUFFIX}"))

    def _path(self, generation: str) -> Path:
        return self.dir / f"{self._PREFIX}{generation}{self._SUFFIX}"

    def latest(self) -> str | None:
        gens = self._generations()
        return gens[-1] if gens else None

    def replicate(self, db_path: str | Path) -> str:
        last = self.latest()
        gen = f"{(int(last) if last else 0) + 1:06d}"
        tmp = self.dir / f".{gen}.tmp"
        src = sqlite3.connect(str(db_path), timeout=30)
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")   # self-contained single file
        finally:
            dst.close()
            src.close()
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self._path(gen))
        return gen

    def restore(self, target_path: str | Path, generation: str | None = None) -> Path:
        gen = generation or self.latest()
        if gen is None or not self._path(gen).exists():
            raise FileNotFoundError("no replica generation to restore")
        target = Path(target_path)
        if target.exists():
            raise FileExistsError(f"restore target {target} exists; refusing to overwrite")
        src = sqlite3.connect(str(self._path(gen)))
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        db = Database(target)      # validates schema, switches to WAL
        with db.tx() as c:
            _meta_set(c, _META_RESTORED, gen)
        return target
