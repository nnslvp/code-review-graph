"""Coverage tests for ruby_resolver.py (post-build cross-module resolver).

Every test drives the resolver through the real build pipeline (``full_build``,
which calls ``resolve_ruby_cross_module``) on a small Ruby/Rails repo laid out
in ``tmp_path``, then asserts the concrete resolved edge target / extra /
confidence. The single exception is the tier-1 same-file arm, which the parser
already qualifies for ordinary same-file calls; there we build the real nodes
via ``full_build`` and then inject the one bare CALLS edge that the arm is meant
to catch (a bare target that slipped through, e.g. an include-injected method),
which is the realistic scenario for that defensive arm.

Authoritative behaviour comes from
``code_review_graph/ruby_resolver.py::resolve_ruby_cross_module``.
"""

import json
import sqlite3

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.ruby_resolver import resolve_ruby_cross_module


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    return tmp_path


def _build(tmp_path):
    store = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    result = full_build(tmp_path, store)
    return store, result


def _conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _edges(conn, where, params=()):
    return conn.execute(
        "SELECT source_qualified, target_qualified, extra, confidence, confidence_tier"
        f" FROM edges WHERE {where}",
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# 1) require_relative -> real .rb node, resolved POST-BUILD
# ---------------------------------------------------------------------------
def test_require_relative_resolved_to_real_rb_node(tmp_path):
    """``require_relative 'helper'`` where helper.rb lives in another directory
    cannot be resolved at parse time (parse-time resolution is strictly relative
    to the requiring file's dir). The post-build resolver matches it to the real
    ``lib/helper.rb`` node by basename suffix and stamps ruby_resolved + INFERRED.
    """
    _repo(tmp_path)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "helper.rb").write_text("class Helper\nend\n")
    # In repo root, NOT next to helper.rb -> parse-time resolution fails.
    (tmp_path / "main.rb").write_text(
        "require_relative 'helper'\n"
        "class Main\nend\n"
    )

    store, result = _build(tmp_path)
    conn = _conn(tmp_path)

    rows = _edges(conn, "kind='IMPORTS_FROM'")
    assert len(rows) == 1, f"expected one IMPORTS_FROM edge, got {[dict(r) for r in rows]}"
    edge = rows[0]
    helper_path = str(tmp_path / "lib" / "helper.rb")
    assert edge["target_qualified"] == helper_path, (
        f"require_relative must resolve to the real helper.rb node, got "
        f"{edge['target_qualified']}"
    )
    extra = json.loads(edge["extra"] or "{}")
    assert extra.get("require_kind") == "require_relative"
    assert extra.get("ruby_resolved") is True, "resolved import must be ruby_resolved"
    assert edge["confidence_tier"] == "INFERRED"

    # The resolved target is a real File node in the graph.
    file_node = conn.execute(
        "SELECT qualified_name FROM nodes WHERE qualified_name=? AND kind='File'",
        (helper_path,),
    ).fetchone()
    assert file_node is not None, "resolved target must be an existing .rb file node"

    assert result["ruby_resolution"]["imports_resolved"] >= 1


def test_require_relative_already_local_not_double_resolved(tmp_path):
    """When helper.rb sits next to the requiring file, parse-time resolution
    already produced the real path; the post-build import arm skips it
    (``if tgt in files: continue``) and does not re-touch the edge.
    """
    _repo(tmp_path)
    (tmp_path / "helper.rb").write_text("class Helper\nend\n")
    (tmp_path / "main.rb").write_text(
        "require_relative 'helper'\n"
        "class Main\nend\n"
    )

    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    rows = _edges(conn, "kind='IMPORTS_FROM'")
    assert len(rows) == 1
    edge = rows[0]
    assert edge["target_qualified"] == str(tmp_path / "helper.rb")
    # Parse-time resolution => EXTRACTED, and the post-build arm left it alone.
    assert edge["confidence_tier"] == "EXTRACTED"


# ---------------------------------------------------------------------------
# 2) Zeitwerk CamelCase -> snake_case file resolution (path fallback branch)
# ---------------------------------------------------------------------------
def test_zeitwerk_namespaced_const_resolves_via_snake_case_path(tmp_path):
    """A fully-qualified const ``Payment::Gateway`` has no node whose *name* is
    ``Payment::Gateway`` (nodes are named ``Payment`` and ``Gateway``), so the
    direct const_to_qn lookup misses. The resolver falls back to converting the
    CamelCase const to ``payment/gateway.rb``, finds that file, and resolves to
    the node in it by last segment (``Gateway``).
    """
    _repo(tmp_path)
    (tmp_path / "app" / "services" / "payment").mkdir(parents=True)
    (tmp_path / "app" / "services" / "payment" / "gateway.rb").write_text(
        "module Payment\n  class Gateway\n  end\nend\n"
    )
    (tmp_path / "app" / "services" / "stripe_gateway.rb").write_text(
        "class StripeGateway < Payment::Gateway\nend\n"
    )

    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)

    rows = _edges(conn, "kind='INHERITS' AND source_qualified LIKE '%StripeGateway'")
    assert len(rows) == 1, f"expected one INHERITS edge, got {[dict(r) for r in rows]}"
    edge = rows[0]
    gateway_node_qn = conn.execute(
        "SELECT qualified_name FROM nodes WHERE name='Gateway' AND language='ruby'"
    ).fetchone()["qualified_name"]
    assert edge["target_qualified"] == gateway_node_qn, (
        f"Payment::Gateway must resolve to the Gateway node {gateway_node_qn}, "
        f"got {edge['target_qualified']}"
    )
    assert "payment/gateway.rb::" in edge["target_qualified"]
    extra = json.loads(edge["extra"] or "{}")
    assert extra.get("ruby_resolved") is True
    assert edge["confidence_tier"] == "INFERRED"


def test_zeitwerk_direct_const_name_resolution(tmp_path):
    """The simple const arm: a subclass inheriting from a top-level const whose
    node exists by name (``ApplicationRecord``) resolves directly via the
    const_to_qn map to that node's qualified name.
    """
    _repo(tmp_path)
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "app" / "models" / "application_record.rb").write_text(
        "class ApplicationRecord\nend\n"
    )
    (tmp_path / "app" / "models" / "user.rb").write_text(
        "class User < ApplicationRecord\nend\n"
    )

    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    edge = _edges(conn, "kind='INHERITS' AND source_qualified LIKE '%::User'")[0]
    ar_qn = str(tmp_path / "app" / "models" / "application_record.rb") + "::ApplicationRecord"
    assert edge["target_qualified"] == ar_qn
    assert json.loads(edge["extra"] or "{}").get("ruby_resolved") is True


# ---------------------------------------------------------------------------
# 3) association target (has_many :posts) -> Post model node
# ---------------------------------------------------------------------------
def test_association_target_resolves_to_model_node(tmp_path):
    """``has_many :posts`` emits an ASSOCIATES edge with bare target ``Post``
    (singularized + camelized). When a Post model node exists, the resolver
    rewrites the target to that node's qualified name.
    """
    _repo(tmp_path)
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "app" / "models" / "user.rb").write_text(
        "class User < ApplicationRecord\n  has_many :posts\nend\n"
    )
    (tmp_path / "app" / "models" / "post.rb").write_text(
        "class Post < ApplicationRecord\nend\n"
    )

    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)

    rows = _edges(conn, "kind='ASSOCIATES'")
    assert len(rows) == 1, f"expected one ASSOCIATES edge, got {[dict(r) for r in rows]}"
    edge = rows[0]
    post_qn = str(tmp_path / "app" / "models" / "post.rb") + "::Post"
    assert edge["target_qualified"] == post_qn, (
        f"has_many :posts must resolve to the Post node {post_qn}, got "
        f"{edge['target_qualified']}"
    )
    extra = json.loads(edge["extra"] or "{}")
    assert extra.get("association") == "has_many"
    assert extra.get("name") == "posts"
    assert extra.get("ruby_resolved") is True


def test_association_target_unresolved_when_model_absent(tmp_path):
    """When no Post model node exists, the ASSOCIATES target stays the bare
    ``Post`` const string and is NOT marked ruby_resolved (no node to point at).
    """
    _repo(tmp_path)
    (tmp_path / "app" / "models").mkdir(parents=True)
    (tmp_path / "app" / "models" / "user.rb").write_text(
        "class User < ApplicationRecord\n  has_many :posts\nend\n"
    )

    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    edge = _edges(conn, "kind='ASSOCIATES'")[0]
    assert edge["target_qualified"] == "Post", (
        f"with no Post model, target stays bare 'Post', got {edge['target_qualified']}"
    )
    assert json.loads(edge["extra"] or "{}").get("ruby_resolved") is not True


# ---------------------------------------------------------------------------
# 4) CALLS tier-1: same-file def -> class-qualified node, EXTRACTED, confidence 1.0
# ---------------------------------------------------------------------------
def test_calls_tier1_same_file_extracted_high_confidence(tmp_path):
    """Tier-1 arm: a bare CALLS edge (no receiver) whose target name matches a
    Function in the same file is resolved to that Function's qualified name with
    EXTRACTED tier and confidence 1.0.

    The parser already qualifies ordinary same-file calls, so this defensive arm
    only fires on a bare edge that slipped through (e.g. a method injected via an
    include from another file). We build the real graph with full_build, then
    inject exactly that one bare edge and run the resolver.
    """
    _repo(tmp_path)
    (tmp_path / "greeter.rb").write_text(
        "class Greeter\n"
        "  def run\n"
        "  end\n"
        "  def greet\n"
        "  end\nend\n"
    )
    store, _ = _build(tmp_path)

    rb_file = str(tmp_path / "greeter.rb")
    run_qn = f"{rb_file}::Greeter.run"
    # Inject a bare CALLS edge run -> 'greet' (no receiver), unresolved.
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source=run_qn, target="greet",
        file_path=rb_file, line=2, extra={},
    ))
    store.commit()

    stats = resolve_ruby_cross_module(store)
    assert stats["calls_resolved"] >= 1

    rows = store._conn.execute(
        "SELECT target_qualified, extra, confidence, confidence_tier"
        " FROM edges WHERE kind='CALLS' AND source_qualified=?",
        (run_qn,),
    ).fetchall()
    assert len(rows) == 1
    tgt, extra_raw, conf, tier = rows[0]
    assert tgt == f"{rb_file}::Greeter.greet", (
        f"bare 'greet' must resolve to class-qualified node, got {tgt}"
    )
    assert tier == "EXTRACTED", f"tier-1 must be EXTRACTED, got {tier}"
    assert conf == 1.0, f"tier-1 confidence must be 1.0, got {conf}"
    assert json.loads(extra_raw or "{}").get("ruby_resolved") is True


def test_calls_tier1_ambiguous_same_file_stays_unresolved(tmp_path):
    """If two Functions in the same file share the target name, the tier-1 arm
    is ambiguous (len != 1) and the bare edge is marked unresolved instead.
    """
    _repo(tmp_path)
    (tmp_path / "dup.rb").write_text(
        "class A\n  def handle\n  end\nend\n"
        "class B\n  def handle\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    rb_file = str(tmp_path / "dup.rb")
    caller_qn = f"{rb_file}::A.run"
    store.upsert_node(NodeInfo(
        kind="Function", name="run", file_path=rb_file,
        line_start=1, line_end=1, language="ruby", parent_name="A", extra={},
    ))
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source=caller_qn, target="handle",
        file_path=rb_file, line=1, extra={},
    ))
    store.commit()

    resolve_ruby_cross_module(store)
    row = store._conn.execute(
        "SELECT target_qualified, extra FROM edges"
        " WHERE kind='CALLS' AND source_qualified=?",
        (caller_qn,),
    ).fetchone()
    assert row["target_qualified"] == "handle", "ambiguous tier-1 target stays bare"
    assert json.loads(row["extra"] or "{}").get("unresolved") is True
    assert not json.loads(row["extra"] or "{}").get("ruby_resolved")


# ---------------------------------------------------------------------------
# 5) CALLS tier-2: const receiver Foo.bar -> singleton; Foo.new -> initialize
# ---------------------------------------------------------------------------
def test_calls_tier2_const_receiver_singleton_method(tmp_path):
    """``Builder.build`` (const receiver, non-new method) resolves to the
    singleton method of Builder (INFERRED, confidence 0.7).
    """
    _repo(tmp_path)
    (tmp_path / "builder.rb").write_text(
        "class Builder\n  def self.build; end\nend\n"
    )
    (tmp_path / "runner.rb").write_text(
        "class Runner\n  def run\n    Builder.build\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    rows = _edges(conn, "kind='CALLS' AND source_qualified LIKE '%Runner.run'")
    build_row = next((r for r in rows if "self.build" in r["target_qualified"]), None)
    assert build_row is not None, (
        f"Builder.build must resolve to singleton qualname containing 'self.build'; "
        f"got {[r['target_qualified'] for r in rows]}"
    )
    assert build_row["confidence_tier"] == "INFERRED"
    assert build_row["confidence"] == 0.7
    assert json.loads(build_row["extra"] or "{}").get("ruby_resolved") is True


def test_calls_tier2_new_resolves_to_initialize(tmp_path):
    """``Builder.new`` resolves to Builder#initialize when an initialize
    instance method exists (INFERRED, confidence 0.7)."""
    _repo(tmp_path)
    (tmp_path / "builder.rb").write_text(
        "class Builder\n  def initialize; end\nend\n"
    )
    (tmp_path / "runner.rb").write_text(
        "class Runner\n  def run\n    Builder.new\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    rows = _edges(conn, "kind='CALLS' AND source_qualified LIKE '%Runner.run'")
    new_row = next((r for r in rows if "initialize" in r["target_qualified"]), None)
    assert new_row is not None, (
        f"Builder.new must resolve to initialize qualname; got "
        f"{[r['target_qualified'] for r in rows]}"
    )
    assert new_row["confidence_tier"] == "INFERRED"
    assert new_row["confidence"] == 0.7
    assert json.loads(new_row["extra"] or "{}").get("ruby_resolved") is True


def test_calls_tier2_new_unresolved_when_no_initialize(tmp_path):
    """``Thing.new`` where Thing has NO initialize method must NOT resolve: the
    edge keeps its bare 'new' target and is marked unresolved (not ruby_resolved).
    """
    _repo(tmp_path)
    (tmp_path / "thing.rb").write_text(
        "class Thing\n  def self.go; end\nend\n"
    )
    (tmp_path / "user.rb").write_text(
        "class User\n  def run\n    Thing.new\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    rows = _edges(conn, "kind='CALLS' AND source_qualified LIKE '%User.run'")
    new_row = next(
        (r for r in rows if json.loads(r["extra"] or "{}").get("receiver") == "Thing"),
        None,
    )
    assert new_row is not None, f"expected a CALLS edge with receiver Thing; got {[dict(r) for r in rows]}"
    assert new_row["target_qualified"] == "new", (
        f"Thing.new with no initialize must keep bare 'new' target, got "
        f"{new_row['target_qualified']}"
    )
    extra = json.loads(new_row["extra"] or "{}")
    assert extra.get("unresolved") is True
    assert not extra.get("ruby_resolved")


def test_calls_tier2_method_unresolved_when_singleton_absent(tmp_path):
    """``Thing.missing`` (const receiver, no matching singleton method) stays
    unresolved — bare target, unresolved=True, not ruby_resolved.
    """
    _repo(tmp_path)
    (tmp_path / "thing.rb").write_text(
        "class Thing\n  def self.go; end\nend\n"
    )
    (tmp_path / "user.rb").write_text(
        "class User\n  def run\n    Thing.missing\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    rows = _edges(conn, "kind='CALLS' AND source_qualified LIKE '%User.run'")
    miss_row = next(
        (r for r in rows
         if json.loads(r["extra"] or "{}").get("receiver") == "Thing"
         and r["target_qualified"] in ("missing",)),
        None,
    )
    assert miss_row is not None, f"expected bare Thing.missing edge; got {[dict(r) for r in rows]}"
    extra = json.loads(miss_row["extra"] or "{}")
    assert extra.get("unresolved") is True
    assert not extra.get("ruby_resolved")


# ---------------------------------------------------------------------------
# 6) bare receiver: unique class name resolves; duplicate stays unresolved
# ---------------------------------------------------------------------------
def test_bare_receiver_unique_class_resolves(tmp_path):
    """A bare const receiver (``Widget.create``) resolves when exactly one class
    named Widget exists in the graph."""
    _repo(tmp_path)
    (tmp_path / "widget.rb").write_text(
        "class Widget\n  def self.create; end\nend\n"
    )
    (tmp_path / "factory.rb").write_text(
        "class Factory\n  def make\n    Widget.create\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    rows = _edges(conn, "kind='CALLS' AND source_qualified LIKE '%Factory.make'")
    widget_row = next((r for r in rows if "self.create" in r["target_qualified"]), None)
    assert widget_row is not None, (
        f"Widget.create must resolve; got {[r['target_qualified'] for r in rows]}"
    )
    assert widget_row["confidence_tier"] == "INFERRED"
    assert json.loads(widget_row["extra"] or "{}").get("ruby_resolved") is True


def test_bare_receiver_duplicate_class_stays_unresolved(tmp_path):
    """Two classes named ``Builder`` in different namespaces (A::Builder,
    B::Builder) make a bare receiver ambiguous: the resolver must NOT resolve it
    and must mark the edge unresolved without ruby_resolved.
    """
    _repo(tmp_path)
    (tmp_path / "a.rb").write_text(
        "module A\n  class Builder\n    def self.build; end\n  end\nend\n"
    )
    (tmp_path / "b.rb").write_text(
        "module B\n  class Builder\n    def self.build; end\n  end\nend\n"
    )
    (tmp_path / "other.rb").write_text(
        "class Other\n  def run\n    Builder.build\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    rows = _edges(conn, "kind='CALLS' AND source_qualified LIKE '%Other.run'")
    build_row = next(
        (r for r in rows if json.loads(r["extra"] or "{}").get("receiver") == "Builder"),
        None,
    )
    assert build_row is not None, f"expected receiver=Builder edge; got {[dict(r) for r in rows]}"
    extra = json.loads(build_row["extra"] or "{}")
    assert extra.get("unresolved") is True, "ambiguous bare Builder must be unresolved"
    assert not extra.get("ruby_resolved"), "ambiguous bare Builder must not be ruby_resolved"
    # And the target must remain bare (no '::' qualification leaked in).
    assert "::" not in build_row["target_qualified"]


def test_qualified_receiver_does_not_mis_resolve_to_wrong_namespace(tmp_path):
    """A fully-qualified receiver ``A::Builder.build`` (with a same-short-name
    ``B::Builder`` also present) must NEVER be silently mis-resolved to the wrong
    namespace's class. In this build the class qualified-name uses ``.`` as its
    namespace separator (``...a.rb::A.Builder``) while the receiver string uses
    ``::`` (``A::Builder``); the tier-2 suffix-match compares them and finds no
    match, so the edge is left unresolved rather than wrongly bound to B.

    This pins the safe-failure contract: ambiguity-prone qualified receivers
    either resolve to the *correct* namespace or stay unresolved — never the
    wrong one.
    """
    _repo(tmp_path)
    (tmp_path / "a.rb").write_text(
        "module A\n  class Builder\n    def self.build; end\n  end\nend\n"
    )
    (tmp_path / "b.rb").write_text(
        "module B\n  class Builder\n    def self.build; end\n  end\nend\n"
    )
    (tmp_path / "other.rb").write_text(
        "class Other\n  def run\n    A::Builder.build\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    conn = _conn(tmp_path)
    rows = _edges(conn, "kind='CALLS' AND source_qualified LIKE '%Other.run'")
    edge = next(
        (r for r in rows
         if json.loads(r["extra"] or "{}").get("receiver") == "A::Builder"),
        None,
    )
    assert edge is not None, f"expected receiver='A::Builder' edge; got {[dict(r) for r in rows]}"
    extra = json.loads(edge["extra"] or "{}")
    # Must NOT have been bound to B's namespace.
    assert str(tmp_path / "b.rb") not in edge["target_qualified"], (
        f"A::Builder.build must never resolve to B's Builder; got {edge['target_qualified']}"
    )
    # In this build format the safe outcome is: left unresolved, not ruby_resolved.
    assert extra.get("unresolved") is True
    assert not extra.get("ruby_resolved")


# ---------------------------------------------------------------------------
# 7) idempotency: resolved edges unchanged on re-run; unresolved retry
# ---------------------------------------------------------------------------
def test_resolver_idempotent_on_resolved_edges(tmp_path):
    """Running the resolver a second time finds nothing new and leaves all
    resolved edges byte-identical (resolved targets/extra/tier unchanged).
    """
    _repo(tmp_path)
    (tmp_path / "builder.rb").write_text(
        "class Builder\n  def self.build; end\n  def initialize; end\nend\n"
    )
    (tmp_path / "runner.rb").write_text(
        "class Runner\n  def run\n    Builder.new\n    Builder.build\n  end\nend\n"
    )
    store, _ = _build(tmp_path)
    conn = store._conn
    conn.row_factory = sqlite3.Row

    def snapshot():
        return {
            r["id"]: (r["target_qualified"], r["extra"], r["confidence_tier"])
            for r in conn.execute(
                "SELECT id, target_qualified, extra, confidence_tier FROM edges"
                " WHERE kind='CALLS'"
            )
        }

    before = snapshot()
    second = resolve_ruby_cross_module(store)
    after = snapshot()

    assert second["calls_resolved"] == 0, "no new CALLS should resolve on re-run"
    assert second["consts_resolved"] == 0
    assert second["imports_resolved"] == 0
    assert before == after, "resolved CALLS edges must be unchanged on re-run"


def test_resolver_unresolved_edges_retry_on_rerun(tmp_path):
    """Unresolved CALLS edges are NOT marked ruby_resolved, so a re-run RE-counts
    them (they are retried rather than skipped). This keeps an edge eligible to
    resolve later once the missing target appears.
    """
    _repo(tmp_path)
    # Thing.new with no initialize -> permanently unresolvable here.
    (tmp_path / "thing.rb").write_text(
        "class Thing\n  def self.go; end\nend\n"
    )
    (tmp_path / "user.rb").write_text(
        "class User\n  def run\n    Thing.new\n  end\nend\n"
    )
    store, first_build = _build(tmp_path)

    # The build's resolver already counted one unresolved call.
    assert first_build["ruby_resolution"]["calls_unresolved"] >= 1

    second = resolve_ruby_cross_module(store)
    assert second["calls_unresolved"] >= 1, (
        "unresolved CALLS must be retried (re-counted) on every run, not skipped"
    )
    assert second["calls_resolved"] == 0


def test_resolver_noop_on_repo_without_ruby(tmp_path):
    """With no .rb files, the resolver returns an empty dict and does nothing."""
    _repo(tmp_path)
    (tmp_path / "app.py").write_text("def main():\n    pass\n")
    store = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, store)
    assert resolve_ruby_cross_module(store) == {}
