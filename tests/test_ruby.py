import json
from pathlib import Path

import tree_sitter_language_pack as tslp

from code_review_graph.parser import CodeParser


def test_children_of_brom_model_returns_attribute_accessors(tmp_path):
    from code_review_graph.graph import GraphStore
    from code_review_graph.tools.query import query_graph
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / ".code-review-graph").mkdir()
    (repo / "user.rb").write_text(
        "class User < ApplicationModel\n"
        "  attribute :id\n"
        "  attribute :name, :string\n"
        "end\n"
    )
    from code_review_graph.incremental import full_build
    store = GraphStore(str(repo / ".code-review-graph" / "graph.db"))
    full_build(repo, store)
    res = query_graph(pattern="children_of", target=f"{repo}/user.rb::User", repo_root=str(repo))
    names = {r["name"] for r in res["results"]}
    assert "id" in names and "name" in names


def test_member_call_targets_method_not_receiver(tmp_path):
    f = tmp_path / "m.rb"
    f.write_text("class A\n  def run(user)\n    user.save\n    Foo.bar\n  end\nend\n")
    nodes, edges = CodeParser().parse_file(f)
    targets = {e.target.split("::")[-1].split(".")[-1] for e in edges if e.kind == "CALLS"}
    assert "save" in targets
    assert "bar" in targets
    assert "user" not in targets  # receiver/local must not be a call target


def test_singleton_and_instance_call_have_distinct_qns(tmp_path):
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    f = tmp_path / "s.rb"
    f.write_text("class Foo\n  def self.call; end\n  def call; end\nend\n")

    nodes, edges = CodeParser().parse_file(f)
    call_nodes = [n for n in nodes if n.kind == "Function" and n.name == "call"]
    assert len(call_nodes) == 2, f"Expected 2 'call' nodes, got {len(call_nodes)}"
    singleton = [n for n in call_nodes if n.extra.get("ruby_singleton")]
    instance = [n for n in call_nodes if not n.extra.get("ruby_singleton")]
    assert len(singleton) == 1, "Expected exactly one singleton call node"
    assert len(instance) == 1, "Expected exactly one instance call node"

    store = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, store)
    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    rows = conn.execute(
        "SELECT qualified_name, name, extra FROM nodes WHERE name='call' AND kind='Function'"
    ).fetchall()
    assert len(rows) == 2, f"Both call methods must survive in store; got {rows}"
    qns = {r[0] for r in rows}
    assert any("self.call" in qn for qn in qns), f"Singleton qualname missing 'self.call': {qns}"


def test_singleton_class_methods_owned_by_class(tmp_path):
    f = tmp_path / "sc.rb"
    f.write_text("class Bar\n  class << self\n    def helper\n    end\n  end\nend\n")

    nodes, edges = CodeParser().parse_file(f)
    helper_nodes = [n for n in nodes if n.kind == "Function" and n.name == "helper"]
    assert len(helper_nodes) == 1, f"Expected 1 'helper' node, got {len(helper_nodes)}"

    helper = helper_nodes[0]
    assert helper.extra.get("ruby_singleton") is True, "helper should be marked as singleton"
    assert helper.extra.get("ruby_owner_qn"), "helper should have ruby_owner_qn"
    assert "Bar" in helper.extra["ruby_owner_qn"], f"ruby_owner_qn should contain Bar: {helper.extra['ruby_owner_qn']}"

    helper_qn = f"{tmp_path / 'sc.rb'}::Bar.self.helper"
    contains_edges = [e for e in edges if e.kind == "CONTAINS" and e.target == helper_qn]
    assert len(contains_edges) == 1, f"Expected 1 CONTAINS edge to helper, got {len(contains_edges)}"

    contains_edge = contains_edges[0]
    assert contains_edge.source == helper.extra["ruby_owner_qn"], \
        f"CONTAINS edge should be sourced from class qn, got {contains_edge.source} vs {helper.extra['ruby_owner_qn']}"


def test_ruby_calls_resolve_const_receiver_and_new(tmp_path):
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build
    from code_review_graph.ruby_resolver import resolve_ruby_cross_module

    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "builder.rb").write_text(
        "class Builder\n"
        "  def self.build; end\n"
        "  def initialize; end\n"
        "end\n"
    )
    (tmp_path / "runner.rb").write_text(
        "class Runner\n"
        "  def run\n"
        "    Builder.new\n"
        "    Builder.build\n"
        "  end\n"
        "end\n"
    )

    store = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, store)
    resolve_ruby_cross_module(store)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT target_qualified, extra, confidence_tier FROM edges"
        " WHERE kind='CALLS' AND source_qualified LIKE '%Runner.run'"
    ).fetchall()

    assert len(rows) == 2, f"Expected 2 CALLS from Runner.run, got {len(rows)}: {[dict(r) for r in rows]}"

    builder_build_row = next(
        (r for r in rows if "self.build" in r["target_qualified"]), None
    )
    assert builder_build_row is not None, (
        f"Builder.build should resolve to singleton qualname containing 'self.build'; "
        f"got targets: {[r['target_qualified'] for r in rows]}"
    )
    build_extra = json.loads(builder_build_row["extra"] or "{}")
    assert build_extra.get("ruby_resolved") is True, "Builder.build edge must be ruby_resolved"
    assert builder_build_row["confidence_tier"] == "INFERRED", (
        f"Builder.build must have confidence_tier=INFERRED, got {builder_build_row['confidence_tier']}"
    )

    builder_new_row = next(
        (r for r in rows if "initialize" in r["target_qualified"]), None
    )
    assert builder_new_row is not None, (
        f"Builder.new should resolve to initialize qualname; "
        f"got targets: {[r['target_qualified'] for r in rows]}"
    )
    new_extra = json.loads(builder_new_row["extra"] or "{}")
    assert new_extra.get("ruby_resolved") is True, "Builder.new edge must be ruby_resolved"
    assert builder_new_row["confidence_tier"] == "INFERRED", (
        f"Builder.new must have confidence_tier=INFERRED, got {builder_new_row['confidence_tier']}"
    )


def test_ruby_calls_resolve_tier1_same_file(tmp_path):
    """Tier-1: a bare CALLS edge (no receiver) whose target name matches a Function
    in the same file as the caller is resolved to its qualified name with EXTRACTED
    confidence by the ruby resolver.

    We insert the nodes and the unresolved CALLS edge manually to simulate a scenario
    where the parser emitted a bare call target (e.g. a cross-class call to a method
    not in the caller file's parse-time symbol table) and the same method happens to
    exist in the same file.
    """
    from code_review_graph.graph import GraphStore
    from code_review_graph.ruby_resolver import resolve_ruby_cross_module
    from code_review_graph.parser import NodeInfo, EdgeInfo

    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    rb_file = str(tmp_path / "a.rb")
    # File must exist on disk so get_all_files() picks it up
    (tmp_path / "a.rb").write_text("# placeholder\n")

    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))

    # File node (required so get_all_files() returns the .rb path)
    s.upsert_node(NodeInfo(
        kind="File", name=rb_file, file_path=rb_file,
        line_start=1, line_end=1, language="ruby",
        parent_name=None, extra={},
    ))
    # Class node
    s.upsert_node(NodeInfo(
        kind="Class", name="Greeter", file_path=rb_file,
        line_start=1, line_end=6, language="ruby",
        parent_name=None, extra={},
    ))
    # Instance method 'greet' (callee)
    s.upsert_node(NodeInfo(
        kind="Function", name="greet", file_path=rb_file,
        line_start=2, line_end=2, language="ruby",
        parent_name="Greeter", extra={},
    ))
    # Instance method 'run' (caller)
    run_qn = f"{rb_file}::Greeter.run"
    s.upsert_node(NodeInfo(
        kind="Function", name="run", file_path=rb_file,
        line_start=3, line_end=5, language="ruby",
        parent_name="Greeter", extra={},
    ))
    # Bare CALLS edge: run -> 'greet' (bare, no receiver) — not yet resolved.
    # This simulates a call to a method that was not in the caller file's
    # parse-time symbol table (e.g. injected via include from another file).
    s.upsert_edge(EdgeInfo(
        kind="CALLS",
        source=run_qn,
        target="greet",
        file_path=rb_file,
        line=4,
        extra={},
    ))
    s.commit()

    resolve_ruby_cross_module(s)

    rows = s._conn.execute(
        "SELECT target_qualified, confidence_tier FROM edges WHERE kind='CALLS'"
    ).fetchall()
    by_tgt = {r[0]: r[1] for r in rows}
    assert any("greet" in tgt and "::" in tgt for tgt in by_tgt), (
        f"greet not resolved to qualified name: {by_tgt}"
    )
    resolved_tgt = next(t for t in by_tgt if "greet" in t and "::" in t)
    assert by_tgt[resolved_tgt] == "EXTRACTED", (
        f"Expected EXTRACTED confidence tier, got {by_tgt[resolved_tgt]}"
    )


def test_di_import_resolves_via_container(tmp_path):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "lib" / "container.rb").write_text(
        "class Container\n"
        "  include Dry::Container::Mixin\n"
        "  register('core.logger') { Logging::Logger.new }\n"
        "  register('core.notifier') { ErrorNotifier }\n"
        "end\n"
    )
    (tmp_path / "app" / "logger.rb").write_text(
        "module Logging\n  class Logger; end\nend\n"
    )
    (tmp_path / "app" / "svc.rb").write_text(
        "class Svc\n  include App::Import['core.logger']\nend\n"
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)
    rows = s._conn.execute(
        "SELECT target_qualified FROM edges WHERE kind='DEPENDS_ON'"
    ).fetchall()
    assert any(
        "Logging" in r[0] and "Logger" in r[0] for r in rows
    ), f"Expected DEPENDS_ON edge to Logging::Logger; got: {[r[0] for r in rows]}"


def test_ruby_calls_ambiguous_bare_receiver_not_resolved(tmp_path):
    """Two classes with the same bare name in different namespaces must NOT be
    resolved when the caller uses a bare (unqualified) receiver — the edge must
    be left unresolved (extra['unresolved']=True, ruby_resolved not set).
    """
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build
    from code_review_graph.ruby_resolver import resolve_ruby_cross_module

    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "a.rb").write_text(
        "module A\n"
        "  class Builder\n"
        "    def self.build; end\n"
        "  end\n"
        "end\n"
    )
    (tmp_path / "b.rb").write_text(
        "module B\n"
        "  class Builder\n"
        "    def self.build; end\n"
        "  end\n"
        "end\n"
    )
    (tmp_path / "other.rb").write_text(
        "class Other\n"
        "  def run\n"
        "    Builder.build\n"
        "  end\n"
        "end\n"
    )

    store = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, store)
    resolve_ruby_cross_module(store)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT target_qualified, extra FROM edges"
        " WHERE kind='CALLS' AND source_qualified LIKE '%Other.run'"
    ).fetchall()

    assert len(rows) >= 1, "Expected at least one CALLS edge from Other.run"
    build_row = next(
        (r for r in rows if json.loads(r["extra"] or "{}").get("receiver") == "Builder"),
        None,
    )
    assert build_row is not None, (
        f"Expected a CALLS edge with receiver='Builder'; got: "
        f"{[dict(r) for r in rows]}"
    )
    build_extra = json.loads(build_row["extra"] or "{}")
    assert build_extra.get("unresolved") is True, (
        "Ambiguous bare Builder.build must be marked unresolved"
    )
    assert not build_extra.get("ruby_resolved"), (
        "Ambiguous bare Builder.build must NOT be marked ruby_resolved"
    )


def test_ruby_calls_unique_bare_receiver_is_resolved(tmp_path):
    """When there is exactly one class with a given bare name, a bare receiver
    call to it must still resolve correctly (regression of the working case).
    """
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build
    from code_review_graph.ruby_resolver import resolve_ruby_cross_module

    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "widget.rb").write_text(
        "class Widget\n"
        "  def self.create; end\n"
        "end\n"
    )
    (tmp_path / "factory.rb").write_text(
        "class Factory\n"
        "  def make\n"
        "    Widget.create\n"
        "  end\n"
        "end\n"
    )

    store = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, store)
    resolve_ruby_cross_module(store)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT target_qualified, extra, confidence_tier FROM edges"
        " WHERE kind='CALLS' AND source_qualified LIKE '%Factory.make'"
    ).fetchall()

    assert len(rows) >= 1, "Expected at least one CALLS edge from Factory.make"
    widget_row = next(
        (r for r in rows if "self.create" in r["target_qualified"]),
        None,
    )
    assert widget_row is not None, (
        f"Widget.create should resolve to singleton qualname containing 'self.create'; "
        f"got: {[r['target_qualified'] for r in rows]}"
    )
    extra = json.loads(widget_row["extra"] or "{}")
    assert extra.get("ruby_resolved") is True, "Widget.create edge must be ruby_resolved"
    assert widget_row["confidence_tier"] == "INFERRED", (
        f"Widget.create must have confidence_tier=INFERRED, got {widget_row['confidence_tier']}"
    )


def test_ruby_grammar_node_types_present():
    src = (Path(__file__).parent / "fixtures" / "ruby_golden.rb").read_bytes()
    root = tslp.get_parser("ruby").parse(src).root_node
    seen = set()
    def walk(n):
        seen.add(n.type)
        for c in n.named_children:
            walk(c)
    walk(root)
    required = {"class", "method", "singleton_method", "singleton_class",
               "call", "assignment", "simple_symbol", "scope_resolution",
               "do_block", "superclass", "body_statement", "constant"}
    missing = required - seen
    assert not missing, f"tree-sitter-ruby node types changed; missing: {missing}"


def test_tested_by_direction_and_tests_for(tmp_path):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build
    from code_review_graph.tools.query import query_graph
    (tmp_path / "lib").mkdir()
    (tmp_path / "spec").mkdir()
    (tmp_path / "lib" / "calc.rb").write_text(
        "class Calc\n  def add(a,b)\n    a+b\n  end\nend\n"
    )
    (tmp_path / "spec" / "calc_spec.rb").write_text(
        "RSpec.describe Calc do\n  it 'adds' do\n    Calc.new.add(1,2)\n  end\nend\n"
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)
    res = query_graph(
        pattern="tests_for",
        target=f"{tmp_path}/lib/calc.rb::Calc.add",
        repo_root=str(tmp_path),
    )
    assert len(res["results"]) >= 1, (
        f"tests_for should find the spec via TESTED_BY (direction must be test->prod); "
        f"got: {res['results']}"
    )


def test_rspec_test_node_gating_to_spec_files(tmp_path):
    lib_file = tmp_path / "calc.rb"
    lib_file.write_text(
        "describe 'something' do\n  context 'when' do\n    it 'works' do end\n  end\nend\n"
    )
    nodes, _ = CodeParser().parse_file(lib_file)
    test_nodes = [n for n in nodes if n.kind == "Test"]
    assert len(test_nodes) == 0, (
        f"Non-spec .rb file should not create Test nodes from describe/context; "
        f"got: {[n.name for n in test_nodes]}"
    )


def test_rspec_test_nodes_created_in_spec_files(tmp_path):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    spec_file = spec_dir / "calc_spec.rb"
    spec_file.write_text(
        "RSpec.describe Calc do\n  it 'adds' do end\nend\n"
    )
    nodes, _ = CodeParser().parse_file(spec_file)
    test_nodes = [n for n in nodes if n.kind == "Test"]
    assert len(test_nodes) >= 1, (
        f"Spec file should create Test nodes; got: {[n.name for n in nodes]}"
    )


def _describe_setup(tmp_path):
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "spec").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    return sqlite3, s, full_build


def test_describe_based_tested_by_resolves_to_class(tmp_path):
    """`RSpec.describe Calc` emits a describe-based TESTED_BY edge from the spec
    to the Calc class, resolved post-build to the prod class node. Both
    tests_for(Calc) and tests_for(Calc.add) (via method->class rollup) find it.
    """
    from code_review_graph.tools.query import query_graph

    sqlite3, s, full_build = _describe_setup(tmp_path)
    (tmp_path / "lib" / "calc.rb").write_text(
        "class Calc\n  def add(a, b)\n    a + b\n  end\nend\n"
    )
    # Use a non-constant call inside the example so the only TESTED_BY signal is
    # the describe-based one (not an incidental resolved member call).
    (tmp_path / "spec" / "calc_spec.rb").write_text(
        "RSpec.describe Calc do\n"
        "  it 'adds' do\n"
        "    subject.add(1, 2)\n"
        "  end\nend\n"
    )
    full_build(tmp_path, s)

    calc_qn = f"{tmp_path}/lib/calc.rb::Calc"
    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    tb = conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges"
        " WHERE kind='TESTED_BY' AND json_extract(extra,'$.tested_via')='describe'"
    ).fetchall()
    assert len(tb) == 1, f"expected one describe-based TESTED_BY, got {[dict(r) for r in tb]}"
    assert tb[0]["target_qualified"] == calc_qn, (
        "describe-based TESTED_BY must resolve to the Calc class node, got "
        f"{tb[0]['target_qualified']}"
    )

    by_class = query_graph(pattern="tests_for", target=calc_qn, repo_root=str(tmp_path))
    assert len(by_class["results"]) >= 1, f"tests_for(Calc) found nothing: {by_class['results']}"

    by_method = query_graph(
        pattern="tests_for", target=f"{calc_qn}.add", repo_root=str(tmp_path)
    )
    assert len(by_method["results"]) >= 1, (
        f"tests_for(Calc.add) must find the spec via method->class rollup; "
        f"got: {by_method['results']}"
    )


def test_rspec_dsl_calls_not_emitted_as_tested_by(tmp_path):
    """RSpec DSL / mock calls (let, expect, before, eq) and member calls on
    locals must NOT produce TESTED_BY edges. After the build the only TESTED_BY
    edges in a spec file are describe-based (and resolve to a real node).
    """
    sqlite3, s, full_build = _describe_setup(tmp_path)
    (tmp_path / "lib" / "calc.rb").write_text("class Calc\n  def add; end\nend\n")
    (tmp_path / "spec" / "calc_spec.rb").write_text(
        "RSpec.describe Calc do\n"
        "  let(:thing) { build_stubbed(:thing) }\n"
        "  before { allow(thing).to receive(:run) }\n"
        "  it 'adds' do\n"
        "    expect(thing.call).to eq(1)\n"
        "  end\nend\n"
    )
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    noise = conn.execute(
        "SELECT target_qualified FROM edges WHERE kind='TESTED_BY'"
        " AND instr(target_qualified, '::') = 0"
    ).fetchall()
    assert noise == [], (
        "no bare-target (DSL-noise) TESTED_BY edges expected, got "
        f"{[r['target_qualified'] for r in noise]}"
    )
    dsl_targets = {"let", "expect", "to", "eq", "before", "allow", "receive",
                   "build_stubbed", "call"}
    all_tb = conn.execute(
        "SELECT target_qualified FROM edges WHERE kind='TESTED_BY'"
    ).fetchall()
    for r in all_tb:
        bare = r["target_qualified"].rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        assert bare not in dsl_targets, f"DSL call leaked into TESTED_BY: {r['target_qualified']}"


def test_describe_string_arg_no_describe_tested_by(tmp_path):
    """`describe 'a string'` (no constant subject) emits no describe-based
    TESTED_BY edge — there is no class to anchor coverage to.
    """
    sqlite3, s, full_build = _describe_setup(tmp_path)
    (tmp_path / "lib" / "calc.rb").write_text("class Calc\n  def add; end\nend\n")
    (tmp_path / "spec" / "calc_spec.rb").write_text(
        "RSpec.describe 'some behaviour' do\n  it 'works' do\n    Calc\n  end\nend\n"
    )
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    tb = conn.execute(
        "SELECT * FROM edges WHERE kind='TESTED_BY'"
        " AND json_extract(extra,'$.tested_via')='describe'"
    ).fetchall()
    assert tb == [], (
        f"string-described spec must not emit describe TESTED_BY, got {[dict(r) for r in tb]}"
    )


def test_callers_of_ruby_skips_bare_name_false_callers(tmp_path):
    """callers_of(Thing.call) must NOT report `dep.call` (a member call on a
    different receiver that shares the method name) as a caller. After the CALLS
    fix such calls are stored bare with extra.receiver; the query-layer
    bare-name fallback is skipped for Ruby so it does not re-introduce the false
    caller edge.
    """
    from code_review_graph.tools.query import query_graph

    sqlite3, s, full_build = _describe_setup(tmp_path)
    (tmp_path / "lib" / "thing.rb").write_text(
        "class Thing\n  def call\n  end\nend\n"
    )
    (tmp_path / "lib" / "user.rb").write_text(
        "class User\n  def run(dep)\n    dep.call\n  end\nend\n"
    )
    full_build(tmp_path, s)

    res = query_graph(
        pattern="callers_of",
        target=f"{tmp_path}/lib/thing.rb::Thing.call",
        repo_root=str(tmp_path),
    )
    qns = [r.get("qualified_name") or "" for r in res["results"]]
    assert all("User.run" not in q for q in qns), (
        f"dep.call must not be reported as a caller of Thing.call; got {qns}"
    )


def test_describe_first_const_arg_only_leading_constant(tmp_path):
    """`_ruby_first_const_arg` anchors only on a LEADING constant subject. A
    non-constant first positional arg (string / helper call / identifier) yields
    no describe-based class edge, even if a later arg is a constant.
    """
    from code_review_graph.parser import CodeParser

    spec = tmp_path / "x_spec.rb"
    spec.write_text("RSpec.describe foo_helper, SomeConstant do\n  it 'x' do end\nend\n")
    _nodes, edges = CodeParser().parse_file(spec)
    describe_tb = [
        e for e in edges
        if e.kind == "TESTED_BY" and (e.extra or {}).get("tested_via") == "describe"
    ]
    assert describe_tb == [], (
        f"string-subject describe must not anchor to a trailing constant; got "
        f"{[e.target for e in describe_tb]}"
    )


def test_describe_reopened_module_dropped_unique_class_resolves(tmp_path):
    """`describe SomeModule` where the module is reopened across files must NOT
    resolve to an arbitrary file (ambiguous full-path → dropped). A uniquely
    defined class in the same graph still resolves.
    """
    sqlite3, s, full_build = _describe_setup(tmp_path)
    # `module Shared` reopened across two files -> full-path "Shared" is ambiguous
    (tmp_path / "lib" / "shared_a.rb").write_text("module Shared\n  class A; end\nend\n")
    (tmp_path / "lib" / "shared_b.rb").write_text("module Shared\n  class B; end\nend\n")
    # a uniquely-defined class
    (tmp_path / "lib" / "uniq.rb").write_text("class Uniq\n  def go; end\nend\n")
    (tmp_path / "spec" / "shared_spec.rb").write_text(
        "RSpec.describe Shared do\n  it 'x' do end\nend\n"
    )
    (tmp_path / "spec" / "uniq_spec.rb").write_text(
        "RSpec.describe Uniq do\n  it 'y' do end\nend\n"
    )
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    targets = [
        r["target_qualified"] for r in conn.execute(
            "SELECT target_qualified FROM edges WHERE kind='TESTED_BY'"
            " AND json_extract(extra,'$.tested_via')='describe'"
        ).fetchall()
    ]
    # Only the unique class resolves; the reopened module is dropped (no arbitrary edge).
    assert len(targets) == 1, f"expected only the unique describe to resolve, got {targets}"
    assert targets[0].endswith("::Uniq"), f"unique class Uniq must resolve, got {targets[0]}"


def test_di_namespaced_constant_resolves_to_correct_node(tmp_path):
    """Namespaced positive: container body 'Logging::Logger.new' resolves to the
    Logging::Logger class node, not any other Logger class, tier == INFERRED.
    """
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "lib" / "container.rb").write_text(
        "class Container\n"
        "  include Dry::Container::Mixin\n"
        "  register('core.logger') { Logging::Logger.new }\n"
        "end\n"
    )
    (tmp_path / "app" / "logging_logger.rb").write_text(
        "module Logging\n  class Logger; end\nend\n"
    )
    (tmp_path / "app" / "svc.rb").write_text(
        "class Svc\n  include App::Import['core.logger']\nend\n"
    )

    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT target_qualified, confidence_tier FROM edges WHERE kind='DEPENDS_ON'"
    ).fetchall()

    assert any(
        "Logging" in r["target_qualified"] and "Logger" in r["target_qualified"]
        for r in rows
    ), f"Expected DEPENDS_ON to Logging::Logger node; got: {[r['target_qualified'] for r in rows]}"

    for r in rows:
        if "Logging" in r["target_qualified"] and "Logger" in r["target_qualified"]:
            assert r["confidence_tier"] == "INFERRED", (
                f"Expected INFERRED tier, got {r['confidence_tier']}"
            )


def test_di_bare_constant_body_resolves(tmp_path):
    """Bare constant body: register('core.notifier') { ErrorNotifier } resolves to ErrorNotifier."""
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "lib" / "container.rb").write_text(
        "class Container\n"
        "  include Dry::Container::Mixin\n"
        "  register('core.notifier') { ErrorNotifier }\n"
        "end\n"
    )
    (tmp_path / "app" / "error_notifier.rb").write_text(
        "class ErrorNotifier; end\n"
    )
    (tmp_path / "app" / "svc.rb").write_text(
        "class Svc\n  include App::Import['core.notifier']\nend\n"
    )

    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT target_qualified FROM edges WHERE kind='DEPENDS_ON'"
    ).fetchall()

    assert any(
        "ErrorNotifier" in r["target_qualified"] for r in rows
    ), f"Expected DEPENDS_ON to ErrorNotifier; got: {[r['target_qualified'] for r in rows]}"


def test_di_collision_bare_name_not_resolved(tmp_path):
    """Collision: two classes named Logger in different namespaces (A::Logger, B::Logger).
    A container key whose body is bare 'Logger' must NOT create a DEPENDS_ON edge.
    """
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "lib" / "container.rb").write_text(
        "class Container\n"
        "  include Dry::Container::Mixin\n"
        "  register('core.logger') { Logger }\n"
        "end\n"
    )
    (tmp_path / "app" / "a_logger.rb").write_text(
        "module A\n  class Logger; end\nend\n"
    )
    (tmp_path / "app" / "b_logger.rb").write_text(
        "module B\n  class Logger; end\nend\n"
    )
    (tmp_path / "app" / "svc.rb").write_text(
        "class Svc\n  include App::Import['core.logger']\nend\n"
    )

    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT target_qualified FROM edges WHERE kind='DEPENDS_ON'"
    ).fetchall()

    assert len(rows) == 0, (
        f"Ambiguous bare Logger must NOT create any DEPENDS_ON edge; "
        f"got: {[r['target_qualified'] for r in rows]}"
    )


def test_di_unmapped_key_no_edge(tmp_path):
    """Unmapped key: include App::Import['nope.missing'] with no matching register -> NO DEPENDS_ON."""
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "lib" / "container.rb").write_text(
        "class Container\n"
        "  include Dry::Container::Mixin\n"
        "  register('core.logger') { SomeLogger }\n"
        "end\n"
    )
    (tmp_path / "app" / "some_logger.rb").write_text(
        "class SomeLogger; end\n"
    )
    (tmp_path / "app" / "svc.rb").write_text(
        "class Svc\n  include App::Import['nope.missing']\nend\n"
    )

    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    rows = conn.execute(
        "SELECT target_qualified FROM edges WHERE kind='DEPENDS_ON'"
    ).fetchall()

    assert len(rows) == 0, (
        f"Unmapped DI key must not create DEPENDS_ON edge; got: {[r[0] for r in rows]}"
    )


def test_di_idempotency_no_duplicate_edges(tmp_path):
    """Idempotency: running full_build twice must not create duplicate DEPENDS_ON edges."""
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "lib" / "container.rb").write_text(
        "class Container\n"
        "  include Dry::Container::Mixin\n"
        "  register('core.logger') { Logging::Logger.new }\n"
        "end\n"
    )
    (tmp_path / "app" / "logging_logger.rb").write_text(
        "module Logging\n  class Logger; end\nend\n"
    )
    (tmp_path / "app" / "svc.rb").write_text(
        "class Svc\n  include App::Import['core.logger']\nend\n"
    )

    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    rows = conn.execute(
        "SELECT target_qualified FROM edges WHERE kind='DEPENDS_ON'"
    ).fetchall()

    logger_edges = [r[0] for r in rows if "Logger" in r[0]]
    assert len(logger_edges) == 1, (
        f"Expected exactly 1 DEPENDS_ON edge to Logger after two builds; "
        f"got {len(logger_edges)}: {logger_edges}"
    )


def test_di_static_const_emits_edge_dynamic_bodies_dropped(tmp_path):
    """Positive+negative in ONE graph (so the negatives are not tautological):
    a container registers a resolvable static const body together with two
    dynamic / non-constant bodies. The static body emits a DEPENDS_ON edge to the
    const's node; the dynamic bodies (`Rails.logger` — a method call, not `.new`;
    `resolve(:y)` — a receiver-less call) emit nothing, proving
    `_const_from_block` drops non-constant bodies while emission demonstrably
    works in the same graph.
    """
    import json
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "lib" / "container.rb").write_text(
        "class Container\n"
        "  include Dry::Container::Mixin\n"
        "  register('repo') { Foo::Repo.new }\n"
        # Non-`.new` method on a RESOLVABLE but DIFFERENT const: if the
        # `method == 'new'` gate in _const_from_block regressed, this would
        # wrongly emit an edge to Bar::Other (distinct from the positive's
        # Foo::Repo, so it survives edge dedup) — making the negative below
        # mutation-sensitive, not tautological.
        "  register('instance') { Bar::Other.instance }\n"
        # Receiver-less call: exercises the no-receiver drop branch.
        "  register('x') { resolve(:y) }\n"
        "end\n"
    )
    (tmp_path / "app" / "repo.rb").write_text(
        "module Foo\n  class Repo; end\nend\n"
        "module Bar\n  class Other; end\nend\n"
    )
    (tmp_path / "app" / "svc.rb").write_text(
        "class Svc\n  include App::Import['repo', 'instance', 'x']\nend\n"
    )

    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT target_qualified, extra FROM edges WHERE kind='DEPENDS_ON'"
    ).fetchall()
    di_keys = [json.loads(r["extra"] or "{}").get("di_key") for r in rows]

    # Positive: the static const body resolves and emits exactly one edge,
    # targeting the real Foo::Repo class node.
    repo_edges = [
        r for r in rows if json.loads(r["extra"] or "{}").get("di_key") == "repo"
    ]
    assert len(repo_edges) == 1, (
        f"static const body must emit exactly one DEPENDS_ON edge; got di_keys={di_keys}"
    )
    target = repo_edges[0]["target_qualified"]
    node = conn.execute(
        "SELECT name, kind FROM nodes WHERE qualified_name=?", (target,)
    ).fetchone()
    assert node is not None and node["name"] == "Repo", (
        f"DEPENDS_ON('repo') must target the real Foo::Repo node; got {target!r}"
    )

    # Negative (meaningful: the positive above proves emission works in this
    # exact graph) — dynamic / non-constant bodies are dropped.
    assert "instance" not in di_keys, (
        f"Bar::Other.instance (method call, not .new) must not emit an edge; got {di_keys}"
    )
    assert "x" not in di_keys, (
        f"resolve(:y) (receiver-less call) must not emit an edge; got {di_keys}"
    )
    # Belt-and-suspenders: no edge points at the dynamic body's would-be target.
    targets = [r["target_qualified"] for r in rows]
    assert not any("Other" in t for t in targets), (
        f"no DEPENDS_ON edge should target Bar::Other; got {targets}"
    )


def test_di_register_outside_container_mixin_file_not_parsed(tmp_path):
    """`register('k') { Const.new }` in a file that does NOT contain
    `Dry::Container::Mixin` is ignored by `_build_container_key_map`. Paired with
    a real container in the same graph (positive control) so the negative is not
    tautological: the proper container's key emits an edge, the non-container
    file's identically-shaped register does not — even though its const and the
    importer both exist.
    """
    import json
    import sqlite3

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (tmp_path / "lib").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "lib" / "container.rb").write_text(
        "class Container\n"
        "  include Dry::Container::Mixin\n"
        "  register('good') { Foo::Repo.new }\n"
        "end\n"
    )
    (tmp_path / "lib" / "plain.rb").write_text(
        "class Plain\n"
        "  register('bad') { Bar::Thing.new }\n"
        "end\n"
    )
    (tmp_path / "app" / "models.rb").write_text(
        "module Foo\n  class Repo; end\nend\n"
        "module Bar\n  class Thing; end\nend\n"
    )
    (tmp_path / "app" / "svc.rb").write_text(
        "class Svc\n  include App::Import['good', 'bad']\nend\n"
    )

    s = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, s)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT extra FROM edges WHERE kind='DEPENDS_ON'"
    ).fetchall()
    di_keys = [json.loads(r["extra"] or "{}").get("di_key") for r in rows]

    assert "good" in di_keys, (
        f"register in a Dry::Container::Mixin file must emit an edge; got {di_keys}"
    )
    assert "bad" not in di_keys, (
        f"register outside a Dry::Container::Mixin file must be ignored; got {di_keys}"
    )


def test_delegate_synthesizes_function_node_and_delegates_edge(tmp_path):
    f = tmp_path / "m.rb"
    f.write_text(
        "class User < ApplicationRecord\n"
        "  delegate :currency, to: :account\n"
        "end\n"
    )
    nodes, edges = CodeParser().parse_file(f)
    fn_names = {n.name for n in nodes if n.kind == "Function"}
    assert "currency" in fn_names, f"expected 'currency' Function node; got {fn_names}"
    delegate_fn = next(n for n in nodes if n.kind == "Function" and n.name == "currency")
    assert delegate_fn.extra.get("ruby_kind") == "delegate"
    assert any(e.kind == "DELEGATES" for e in edges), "expected a DELEGATES edge"
    delegates_edge = next(e for e in edges if e.kind == "DELEGATES")
    assert delegates_edge.target == "account" or "account" in delegates_edge.target
    calls_targets = {e.target.split("::")[-1].split(".")[-1] for e in edges if e.kind == "CALLS"}
    assert "delegate" not in calls_targets, "delegate must not emit a junk CALLS edge"


def test_enum_suppressed_no_calls_edge(tmp_path):
    f = tmp_path / "m.rb"
    f.write_text(
        "class Order < ApplicationRecord\n"
        "  enum :status, [:pending, :shipped]\n"
        "end\n"
    )
    nodes, edges = CodeParser().parse_file(f)
    calls_targets = {e.target.split("::")[-1].split(".")[-1] for e in edges if e.kind == "CALLS"}
    assert "enum" not in calls_targets, "enum must not emit a junk CALLS edge"


def test_has_many_through_sets_extra(tmp_path):
    f = tmp_path / "m.rb"
    f.write_text(
        "class User < ApplicationRecord\n"
        "  has_many :logs, through: :sessions\n"
        "end\n"
    )
    nodes, edges = CodeParser().parse_file(f)
    assoc_edges = [e for e in edges if e.kind == "ASSOCIATES"]
    assert assoc_edges, "expected an ASSOCIATES edge"
    e = assoc_edges[0]
    assert e.extra.get("through") == "sessions", (
        f"expected through=='sessions'; got extra={e.extra}"
    )


def test_belongs_to_polymorphic_marks_edge_no_concrete_target(tmp_path):
    f = tmp_path / "m.rb"
    f.write_text(
        "class Comment < ApplicationRecord\n"
        "  belongs_to :subject, polymorphic: true\n"
        "end\n"
    )
    nodes, edges = CodeParser().parse_file(f)
    assoc_edges = [e for e in edges if e.kind == "ASSOCIATES"]
    assert assoc_edges, "expected an ASSOCIATES edge"
    e = assoc_edges[0]
    assert e.extra.get("polymorphic") is True, (
        f"expected polymorphic==True; got extra={e.extra}"
    )
    assert e.target != "Subject", (
        f"polymorphic belongs_to must not camelize to a concrete target; got target={e.target!r}"
    )


def test_delegate_and_enum_and_assoc_accuracy(tmp_path):
    f = tmp_path / "m.rb"
    f.write_text(
        "class User < ApplicationRecord\n"
        "  delegate :currency, to: :account\n"
        "  enum :status, [:active, :blocked]\n"
        "  belongs_to :owner, class_name: 'Account'\n"
        "  has_many :logs, through: :sessions\n"
        "  belongs_to :subject, polymorphic: true\n"
        "end\n"
    )
    nodes, edges = CodeParser().parse_file(f)
    fn = {n.name for n in nodes if n.kind == "Function"}
    assert "currency" in fn
    assert any(e.kind == "DELEGATES" for e in edges)
    calls = {e.target.split("::")[-1].split(".")[-1] for e in edges if e.kind == "CALLS"}
    assert "delegate" not in calls and "enum" not in calls
    assoc_targets = {e.target for e in edges if e.kind == "ASSOCIATES"}
    assert "Account" in assoc_targets
    poly = [e for e in edges if e.kind == "ASSOCIATES" and e.extra.get("polymorphic")]
    assert poly and poly[0].extra.get("polymorphic") is True
    through_edges = [e for e in edges if e.kind == "ASSOCIATES" and e.extra.get("through")]
    assert through_edges and through_edges[0].extra.get("through") == "sessions"


def test_concern_included_do_dsl_and_class_methods(tmp_path):
    from pathlib import Path
    FIX = Path(__file__).parent / "fixtures" / "concern_with_included.rb"
    nodes, edges = CodeParser().parse_file(FIX)
    # has_many/scope/before_save inside included do are captured on the concern
    assoc = {(e.kind, e.extra.get("association")) for e in edges if e.kind == "ASSOCIATES"}
    assert ("ASSOCIATES", "has_many") in assoc
    # class_methods do -> tracked? is a method node
    fn = {n.name for n in nodes if n.kind == "Function"}
    assert "tracked?" in fn
    # included do macros are NOT junk CALLS
    calls = {e.target.split("::")[-1].split(".")[-1] for e in edges if e.kind == "CALLS"}
    assert "has_many" not in calls and "included" not in calls
    # tracked? has ruby_singleton=True
    tracked_nodes = [n for n in nodes if n.name == "tracked?" and n.kind == "Function"]
    assert len(tracked_nodes) == 1, f"Expected exactly one tracked? node, got {len(tracked_nodes)}"
    assert tracked_nodes[0].extra.get("ruby_singleton") is True
    # scope/callback recorded in class extra
    concern_node = next(n for n in nodes if n.name == "Trackable")
    assert "recent" in concern_node.extra.get("rails_scopes", [])
    assert any("before_save" in cb for cb in concern_node.extra.get("rails_callbacks", []))


def test_concern_includer_inherits_associates(tmp_path):
    import sqlite3
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build
    from code_review_graph.ruby_resolver import resolve_ruby_cross_module

    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()

    (tmp_path / "trackable.rb").write_text(
        "module Trackable\n"
        "  extend ActiveSupport::Concern\n"
        "  included do\n"
        "    has_many :events\n"
        "  end\n"
        "end\n"
    )
    (tmp_path / "post.rb").write_text(
        "class Post < ApplicationRecord\n"
        "  include Trackable\n"
        "end\n"
    )

    store = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, store)
    resolve_ruby_cross_module(store)

    conn = sqlite3.connect(str(tmp_path / ".code-review-graph" / "graph.db"))
    conn.row_factory = sqlite3.Row

    post_assoc = conn.execute(
        "SELECT target_qualified, extra FROM edges"
        " WHERE kind='ASSOCIATES' AND source_qualified LIKE '%Post'",
    ).fetchall()

    assert post_assoc, "Post should have inherited ASSOCIATES edges from Trackable"
    row = post_assoc[0]
    row_extra = json.loads(row["extra"] or "{}")
    assert row_extra.get("inherited_via") == "Trackable", (
        f"Expected inherited_via='Trackable', got extra={row_extra}"
    )
    assert row_extra.get("confidence_tier") == "INFERRED"


def test_concern_not_emitted_as_extends(tmp_path):
    f = tmp_path / "c.rb"
    f.write_text("module M\n  extend ActiveSupport::Concern\nend\n")
    nodes, edges = CodeParser().parse_file(f)
    bad = [e for e in edges if e.kind in ("EXTENDS", "INCLUDES") and "ActiveSupport::Concern" in e.target]
    assert not bad, f"Expected no EXTENDS/INCLUDES edge to ActiveSupport::Concern, got: {bad}"
    module_node = next((n for n in nodes if n.name == "M"), None)
    assert module_node is not None
    assert module_node.extra.get("rails_role") is None, (
        f"Plain Concern module should not be stamped with rails_role, got: {module_node.extra}"
    )
