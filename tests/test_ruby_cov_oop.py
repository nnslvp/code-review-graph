"""OOP coverage for the Ruby/Rails first-class integration.

Behavioral tests for already-implemented language-OOP cases that are not (or
only partially) covered by tests/test_ruby.py and tests/test_multilang.py:

- inheritance (simple / namespaced class / scope_resolution superclass)
- mixins include / extend / prepend -> edge kind + extra.mixins
- attr_accessor / attr_reader / attr_writer exclusivity + ruby_kind
- ``attribute`` macro: ``attribute :x`` and ``attribute :x, :type`` (the 2nd
  symbol is the TYPE, never a field; extra.attr_type on BOTH getter and setter)
- constants top-level (no owner) + class-level (ruby_owner_qn + CONTAINS owner)
- singleton_method ``def self.x`` -> ruby_singleton + distinct qualname
- ``class << self`` methods -> ruby_singleton + CONTAINS from class + owner_qn
- ``def x=`` setter naming
- method visibility (private/protected collected, public excluded)
- children_of(class) returns synthesized accessors

Every test parses a small realistic Ruby snippet in tmp_path (or builds a tmp
graph) and asserts on concrete nodes/edges/extra — no mocks.
"""

from code_review_graph.parser import CodeParser


def _parse(tmp_path, body: str, fname: str = "x.rb"):
    f = tmp_path / fname
    f.write_text(body)
    nodes, edges = CodeParser().parse_file(f)
    return f, nodes, edges


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------

def test_simple_inheritance_edge(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class Dog < Animal\nend\n")
    inh = [e for e in edges if e.kind == "INHERITS"]
    assert len(inh) == 1
    assert inh[0].source == f"{f}::Dog"
    assert inh[0].target == "Animal"


def test_namespaced_class_name_not_dropped(tmp_path):
    # `class A::B` keeps the full scope_resolution name as the class node name.
    f, nodes, edges = _parse(tmp_path, "class Reports::Daily\nend\n")
    classes = {n.name for n in nodes if n.kind == "Class"}
    assert "Reports::Daily" in classes


def test_scope_resolution_superclass_activerecord_base(tmp_path):
    # `class Foo < ActiveRecord::Base` -> INHERITS target is the full scoped name.
    f, nodes, edges = _parse(tmp_path, "class Account < ActiveRecord::Base\nend\n")
    inh = {e.target for e in edges if e.kind == "INHERITS"}
    assert "ActiveRecord::Base" in inh
    src = {e.source for e in edges if e.kind == "INHERITS"}
    assert f"{f}::Account" in src


def test_namespaced_class_with_scoped_base(tmp_path):
    # Both the class name and the base are scope_resolution nodes.
    f, nodes, edges = _parse(tmp_path, "class Admin::Audit < Auth::Admin\nend\n")
    classes = {n.name for n in nodes if n.kind == "Class"}
    assert "Admin::Audit" in classes
    inh = {(e.source, e.target) for e in edges if e.kind == "INHERITS"}
    assert (f"{f}::Admin::Audit", "Auth::Admin") in inh


# ---------------------------------------------------------------------------
# Mixins: include / extend / prepend
# ---------------------------------------------------------------------------

def test_mixin_include_edge_and_extra(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  include Comparable\nend\n")
    inc = [e for e in edges if e.kind == "INCLUDES"]
    assert len(inc) == 1
    assert inc[0].source == f"{f}::A"
    assert inc[0].target == "Comparable"
    assert inc[0].extra.get("confidence_tier") == "EXTRACTED"
    cls = next(n for n in nodes if n.kind == "Class" and n.name == "A")
    assert "Comparable" in cls.extra.get("mixins", [])


def test_mixin_extend_edge_and_extra(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  extend ClassMethods\nend\n")
    ext = [e for e in edges if e.kind == "EXTENDS"]
    assert len(ext) == 1
    assert ext[0].target == "ClassMethods"
    cls = next(n for n in nodes if n.kind == "Class" and n.name == "A")
    assert "ClassMethods" in cls.extra.get("mixins", [])


def test_mixin_prepend_edge_and_extra(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  prepend Logging\nend\n")
    pre = [e for e in edges if e.kind == "PREPENDS"]
    assert len(pre) == 1
    assert pre[0].target == "Logging"
    cls = next(n for n in nodes if n.kind == "Class" and n.name == "A")
    assert "Logging" in cls.extra.get("mixins", [])


def test_mixin_macros_not_emitted_as_calls(tmp_path):
    f, nodes, edges = _parse(
        tmp_path,
        "class A\n  include Comparable\n  extend ClassMethods\n  prepend Logging\nend\n",
    )
    call_targets = {
        e.target.split("::")[-1].split(".")[-1] for e in edges if e.kind == "CALLS"
    }
    assert "include" not in call_targets
    assert "extend" not in call_targets
    assert "prepend" not in call_targets


def test_mixin_scoped_module_target(tmp_path):
    # include of a namespaced module keeps the full scope_resolution text.
    f, nodes, edges = _parse(tmp_path, "class A\n  include Auth::Helpers\nend\n")
    inc = [e for e in edges if e.kind == "INCLUDES"]
    assert any(e.target == "Auth::Helpers" for e in inc)


# ---------------------------------------------------------------------------
# attr_accessor / attr_reader / attr_writer
# ---------------------------------------------------------------------------

def test_attr_accessor_creates_getter_and_setter(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  attr_accessor :name\nend\n")
    fns = {n.name: n.extra.get("ruby_kind")
           for n in nodes if n.kind == "Function"}
    assert fns.get("name") == "attr_accessor"
    assert fns.get("name=") == "attr_accessor"


def test_attr_reader_getter_only(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  attr_reader :id\nend\n")
    fns = {n.name for n in nodes if n.kind == "Function"}
    assert "id" in fns
    assert "id=" not in fns, "attr_reader must NOT emit a setter"
    reader = next(n for n in nodes if n.kind == "Function" and n.name == "id")
    assert reader.extra.get("ruby_kind") == "attr_reader"


def test_attr_writer_setter_only(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  attr_writer :token\nend\n")
    fns = {n.name for n in nodes if n.kind == "Function"}
    assert "token=" in fns
    assert "token" not in fns, "attr_writer must NOT emit a getter"
    writer = next(n for n in nodes if n.kind == "Function" and n.name == "token=")
    assert writer.extra.get("ruby_kind") == "attr_writer"


def test_attr_owner_qn_and_contains(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  attr_accessor :name\nend\n")
    getter = next(n for n in nodes if n.kind == "Function" and n.name == "name")
    assert getter.extra.get("ruby_owner_qn") == f"{f}::A"
    contains = {e.target.split(".")[-1]
                for e in edges if e.kind == "CONTAINS" and e.source == f"{f}::A"}
    assert "name" in contains
    assert "name=" in contains


# ---------------------------------------------------------------------------
# `attribute` macro (ActiveModel / brom)
# ---------------------------------------------------------------------------

def test_attribute_single_symbol_no_type(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  attribute :id\nend\n")
    fns = {n.name: n for n in nodes if n.kind == "Function"}
    assert "id" in fns and "id=" in fns
    assert fns["id"].extra.get("ruby_kind") == "attribute"
    assert "attr_type" not in fns["id"].extra
    assert "attr_type" not in fns["id="].extra


def test_attribute_second_symbol_is_type_not_field(tmp_path):
    # `attribute :name, :string` -> the 2nd symbol is the TYPE; there must be
    # NO function node named 'string', and attr_type must be set on BOTH the
    # getter and the setter.
    f, nodes, edges = _parse(tmp_path, "class A\n  attribute :name, :string\nend\n")
    fn_names = {n.name for n in nodes if n.kind == "Function"}
    assert "name" in fn_names
    assert "name=" in fn_names
    assert "string" not in fn_names, "the type symbol must NOT become a field/function"
    getter = next(n for n in nodes if n.kind == "Function" and n.name == "name")
    setter = next(n for n in nodes if n.kind == "Function" and n.name == "name=")
    assert getter.extra.get("attr_type") == "string"
    assert setter.extra.get("attr_type") == "string"


def test_attribute_macro_not_emitted_as_call(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  attribute :name, :string\nend\n")
    call_targets = {
        e.target.split("::")[-1].split(".")[-1] for e in edges if e.kind == "CALLS"
    }
    assert "attribute" not in call_targets


# ---------------------------------------------------------------------------
# Constants -> Type nodes
# ---------------------------------------------------------------------------

def test_top_level_constant_is_type_no_owner(tmp_path):
    f, nodes, edges = _parse(tmp_path, "MAX = 5\n")
    const = next(n for n in nodes if n.kind == "Type" and n.name == "MAX")
    assert const.extra.get("ruby_kind") == "constant"
    assert "ruby_owner_qn" not in const.extra
    assert const.parent_name is None
    # CONTAINS sourced from the file when there is no enclosing class.
    contains = [e for e in edges if e.kind == "CONTAINS" and e.target == f"{f}::MAX"]
    assert len(contains) == 1
    assert contains[0].source == str(f)


def test_class_level_constant_owner_and_contains(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class Box\n  LIMIT = 10\nend\n")
    const = next(n for n in nodes if n.kind == "Type" and n.name == "LIMIT")
    assert const.extra.get("ruby_kind") == "constant"
    assert const.extra.get("ruby_owner_qn") == f"{f}::Box"
    assert const.parent_name == "Box"
    contains = [e for e in edges
                if e.kind == "CONTAINS" and e.target == f"{f}::Box.LIMIT"]
    assert len(contains) == 1
    assert contains[0].source == f"{f}::Box"


# ---------------------------------------------------------------------------
# Singleton methods
# ---------------------------------------------------------------------------

def test_singleton_method_def_self_distinct_qn(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class Foo\n  def self.build; end\n  def build; end\nend\n")
    builds = [n for n in nodes if n.kind == "Function" and n.name == "build"]
    assert len(builds) == 2
    singleton = [n for n in builds if n.extra.get("ruby_singleton")]
    instance = [n for n in builds if not n.extra.get("ruby_singleton")]
    assert len(singleton) == 1
    assert len(instance) == 1
    # ruby_owner_qn bridges children_of for the singleton method.
    assert singleton[0].extra.get("ruby_owner_qn") == f"{f}::Foo"
    # Distinct qualnames: the singleton CONTAINS target carries `self.build`.
    contains = {e.target for e in edges if e.kind == "CONTAINS"}
    assert f"{f}::Foo.self.build" in contains
    assert f"{f}::Foo.build" in contains


def test_class_self_block_methods_owned_by_class(tmp_path):
    f, nodes, edges = _parse(
        tmp_path,
        "class Bar\n  class << self\n    def helper; end\n  end\nend\n",
    )
    helpers = [n for n in nodes if n.kind == "Function" and n.name == "helper"]
    assert len(helpers) == 1
    helper = helpers[0]
    assert helper.extra.get("ruby_singleton") is True
    assert helper.extra.get("ruby_owner_qn") == f"{f}::Bar"
    helper_qn = f"{f}::Bar.self.helper"
    contains = [e for e in edges if e.kind == "CONTAINS" and e.target == helper_qn]
    assert len(contains) == 1
    # CONTAINS for class<<self method is sourced from the class, not the file.
    assert contains[0].source == f"{f}::Bar"


# ---------------------------------------------------------------------------
# Setter naming
# ---------------------------------------------------------------------------

def test_def_setter_named_with_equals(tmp_path):
    f, nodes, edges = _parse(tmp_path, "class A\n  def value=(v)\n    @value = v\n  end\nend\n")
    fns = {n.name for n in nodes if n.kind == "Function"}
    assert "value=" in fns
    assert "value" not in fns


# ---------------------------------------------------------------------------
# Method visibility
# ---------------------------------------------------------------------------

def test_visibility_private_and_protected_collected_public_excluded(tmp_path):
    f, nodes, edges = _parse(
        tmp_path,
        "class A\n"
        "  def pub; end\n"
        "  protected\n"
        "  def prot; end\n"
        "  private\n"
        "  def priv; end\n"
        "end\n",
    )
    cls = next(n for n in nodes if n.kind == "Class" and n.name == "A")
    nonpublic = cls.extra.get("ruby_nonpublic_methods", [])
    assert "prot" in nonpublic
    assert "priv" in nonpublic
    assert "pub" not in nonpublic


def test_visibility_setter_recorded_with_equals(tmp_path):
    # A private setter (def x=) is recorded by its canonical "x=" name.
    f, nodes, edges = _parse(
        tmp_path,
        "class A\n  private\n  def secret=(v)\n    @secret = v\n  end\nend\n",
    )
    cls = next(n for n in nodes if n.kind == "Class" and n.name == "A")
    nonpublic = cls.extra.get("ruby_nonpublic_methods", [])
    assert "secret=" in nonpublic


# ---------------------------------------------------------------------------
# children_of(class) -> synthesized accessors
# ---------------------------------------------------------------------------

def _build_graph(repo):
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build

    (repo / ".git").mkdir()
    (repo / ".code-review-graph").mkdir()
    store = GraphStore(str(repo / ".code-review-graph" / "graph.db"))
    full_build(repo, store)
    return store


def test_children_of_returns_attr_accessor_synthesized_members(tmp_path):
    from code_review_graph.tools.query import query_graph

    (tmp_path / "model.rb").write_text(
        "class User\n"
        "  attr_accessor :name\n"
        "  attr_reader :id\n"
        "  attr_writer :token\n"
        "end\n"
    )
    _build_graph(tmp_path)
    res = query_graph(
        pattern="children_of",
        target=f"{tmp_path}/model.rb::User",
        repo_root=str(tmp_path),
    )
    names = {r["name"] for r in res["results"]}
    assert "name" in names
    assert "name=" in names
    assert "id" in names
    assert "token=" in names
    # attr_reader emits no setter; attr_writer emits no getter.
    assert "id=" not in names
    assert "token" not in names


def test_children_of_returns_attribute_macro_members(tmp_path):
    from code_review_graph.tools.query import query_graph

    (tmp_path / "rec.rb").write_text(
        "class Record\n"
        "  attribute :id\n"
        "  attribute :name, :string\n"
        "end\n"
    )
    _build_graph(tmp_path)
    res = query_graph(
        pattern="children_of",
        target=f"{tmp_path}/rec.rb::Record",
        repo_root=str(tmp_path),
    )
    names = {r["name"] for r in res["results"]}
    assert {"id", "id=", "name", "name="} <= names
    # The type symbol must never appear as a synthesized member.
    assert "string" not in names
