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
