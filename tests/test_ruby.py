from pathlib import Path
import tree_sitter_language_pack as tslp
from code_review_graph.parser import CodeParser


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
