from pathlib import Path
import tree_sitter_language_pack as tslp


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
