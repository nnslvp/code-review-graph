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
