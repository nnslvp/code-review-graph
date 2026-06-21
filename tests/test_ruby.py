import tree_sitter_language_pack as tslp


def test_ruby_grammar_node_types_present():
    parser = tslp.get_parser("ruby")
    src = b'''
class Foo < Bar
  def self.build; end
  def call; end
  attribute :x, :decimal
  has_many :posts
  scope :active, -> { where(a: 1) }
  class << self
    def helper; end
  end
end
CONST = 1
'''
    root = parser.parse(src).root_node
    seen = set()
    def walk(n):
        seen.add(n.type)
        for c in n.named_children:
            walk(c)
    walk(root)
    required = {"class", "method", "singleton_method", "singleton_class",
               "call", "assignment", "simple_symbol", "lambda",
               "block", "superclass", "body_statement", "constant"}
    missing = required - seen
    assert not missing, f"tree-sitter-ruby node types changed; missing: {missing}"
