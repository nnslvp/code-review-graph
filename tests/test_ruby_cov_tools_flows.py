"""Coverage for the Ruby/Rails query / flows / enrich / embeddings surfacing.

Area: tools/query.py (query_graph patterns mixins_of / associations_of /
inheritors_of / children_of, confidence_tier surfacing in standard + minimal,
unresolved markers), tools/review.py (detect_changes inferred_edge_count),
flows.py (Rails entry-point detection), enrich.py (_format_node_context),
embeddings.py (_node_to_text).

All tests build a real graph from realistic Ruby fixtures via full_build (which
also runs the ruby cross-module resolver) and assert concrete nodes / edges /
extra fields. No mocks of graph internals.
"""

from pathlib import Path
from unittest.mock import patch

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    return tmp_path


def _build(tmp_path: Path) -> GraphStore:
    store = GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))
    full_build(tmp_path, store)
    return store


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _resolved_rails_app(tmp_path: Path) -> Path:
    """A small Rails app where mixin / association / inheritance targets all
    resolve to real nodes defined in the repo."""
    _init_repo(tmp_path)
    _write(tmp_path, "app/models/concerns/trackable.rb", "module Trackable\nend\n")
    _write(
        tmp_path,
        "app/models/post.rb",
        "class Post < ApplicationRecord\n"
        "  include Trackable\n"
        "  has_many :comments\n"
        "  belongs_to :author, class_name: 'User'\n"
        "  scope :recent, -> { order(created_at: :desc) }\n"
        "  validates :title, presence: true\n"
        "end\n",
    )
    _write(tmp_path, "app/models/comment.rb", "class Comment < ApplicationRecord\nend\n")
    _write(tmp_path, "app/models/user.rb", "class User < ApplicationRecord\nend\n")
    _write(tmp_path, "app/models/special_post.rb", "class SpecialPost < Post\nend\n")
    _write(
        tmp_path,
        "app/controllers/posts_controller.rb",
        "class PostsController < ApplicationController\n"
        "  def index\n  end\n"
        "  def show\n  end\n"
        "  private\n"
        "  def set_post\n  end\n"
        "end\n",
    )
    _write(
        tmp_path,
        "app/jobs/cleanup_job.rb",
        "class CleanupJob < ApplicationJob\n"
        "  def perform\n  end\n"
        "  private\n"
        "  def helper\n  end\n"
        "end\n",
    )
    return tmp_path


def _post_qn(tmp_path: Path) -> str:
    return f"{tmp_path}/app/models/post.rb::Post"


# ---------------------------------------------------------------------------
# query_graph: mixins_of (OUTGOING INCLUDES/EXTENDS/PREPENDS)
# ---------------------------------------------------------------------------


def test_mixins_of_returns_outgoing_includes_with_tier(tmp_path):
    from code_review_graph.tools.query import query_graph

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        res = query_graph(
            pattern="mixins_of", target=_post_qn(tmp_path), repo_root=str(tmp_path)
        )
    finally:
        store.close()

    assert res["status"] == "ok"
    # Non-zero "Found N result(s)" summary.
    assert "Found 1 result(s)" in res["summary"]
    names = {r.get("name") or r.get("qualified_name") for r in res["results"]}
    assert "Trackable" in names
    # The mixin edge is an outgoing INCLUDES edge from Post.
    mixin_edges = [e for e in res["edges"] if e["kind"] == "INCLUDES"]
    assert mixin_edges, f"expected an INCLUDES edge, got {res['edges']}"
    assert mixin_edges[0]["source"] == _post_qn(tmp_path)
    # Every result carries a confidence_tier.
    assert all("confidence_tier" in r for r in res["results"])


def test_mixins_of_covers_extend_and_prepend(tmp_path):
    from code_review_graph.tools.query import query_graph

    _init_repo(tmp_path)
    _write(tmp_path, "helpers.rb", "module Helper\nend\n")
    _write(tmp_path, "loggable.rb", "module Loggable\nend\n")
    _write(tmp_path, "trackable.rb", "module Trackable\nend\n")
    _write(
        tmp_path,
        "service.rb",
        "class Service\n"
        "  include Trackable\n"
        "  extend Helper\n"
        "  prepend Loggable\n"
        "end\n",
    )
    store = _build(tmp_path)
    try:
        res = query_graph(
            pattern="mixins_of",
            target=f"{tmp_path}/service.rb::Service",
            repo_root=str(tmp_path),
        )
    finally:
        store.close()

    kinds = {e["kind"] for e in res["edges"]}
    assert kinds == {"INCLUDES", "EXTENDS", "PREPENDS"}, kinds
    names = {r.get("name") or r.get("qualified_name") for r in res["results"]}
    assert {"Trackable", "Helper", "Loggable"} <= names


def test_mixins_of_module_side_is_empty_no_inversion(tmp_path):
    """Querying mixins_of on the included MODULE itself must return nothing —
    INCLUDES is directional (class -> module), not inverted."""
    from code_review_graph.tools.query import query_graph

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        res = query_graph(
            pattern="mixins_of",
            target=f"{tmp_path}/app/models/concerns/trackable.rb::Trackable",
            repo_root=str(tmp_path),
        )
    finally:
        store.close()

    assert res["status"] == "ok"
    assert res["results"] == []
    assert "Found 0 result(s)" in res["summary"]


def test_mixins_of_unresolved_bare_target_marker(tmp_path):
    """An include of a module not defined anywhere in the repo stays a bare
    constant target and is surfaced with unresolved=True."""
    from code_review_graph.tools.query import query_graph

    _init_repo(tmp_path)
    _write(
        tmp_path,
        "post.rb",
        "class Post\n  include ExternalConcern\nend\n",
    )
    store = _build(tmp_path)
    try:
        res = query_graph(
            pattern="mixins_of",
            target=f"{tmp_path}/post.rb::Post",
            repo_root=str(tmp_path),
        )
    finally:
        store.close()

    assert len(res["results"]) == 1
    r = res["results"][0]
    assert r["qualified_name"] == "ExternalConcern"
    assert r["mixin_kind"] == "INCLUDES"
    assert r["unresolved"] is True
    assert "confidence_tier" in r


# ---------------------------------------------------------------------------
# query_graph: associations_of (OUTGOING ASSOCIATES)
# ---------------------------------------------------------------------------


def test_associations_of_returns_outgoing_associates_resolved(tmp_path):
    from code_review_graph.tools.query import query_graph

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        res = query_graph(
            pattern="associations_of",
            target=_post_qn(tmp_path),
            repo_root=str(tmp_path),
        )
    finally:
        store.close()

    assert res["status"] == "ok"
    # has_many :comments -> Comment, belongs_to :author class_name 'User' -> User
    assert "Found 2 result(s)" in res["summary"]
    names = {r.get("name") or r.get("qualified_name") for r in res["results"]}
    assert "Comment" in names
    assert "User" in names  # single-quoted class_name honored + stripped
    # All ASSOCIATES edges are outgoing from Post.
    assoc_edges = [e for e in res["edges"] if e["kind"] == "ASSOCIATES"]
    assert len(assoc_edges) == 2
    assert all(e["source"] == _post_qn(tmp_path) for e in assoc_edges)
    assert all("confidence_tier" in r for r in res["results"])


def test_associations_of_unresolved_target_carries_association_kind(tmp_path):
    """When the associated class is not in the repo, the result is surfaced as
    an unresolved bare target tagged with the association macro name."""
    from code_review_graph.tools.query import query_graph

    _init_repo(tmp_path)
    _write(
        tmp_path,
        "app/models/post.rb",
        "class Post < ApplicationRecord\n"
        "  has_many :widgets, class_name: 'ExternalWidget'\n"
        "end\n",
    )
    store = _build(tmp_path)
    try:
        res = query_graph(
            pattern="associations_of",
            target=f"{tmp_path}/app/models/post.rb::Post",
            repo_root=str(tmp_path),
        )
    finally:
        store.close()

    assert len(res["results"]) == 1
    r = res["results"][0]
    assert r["qualified_name"] == "ExternalWidget"
    assert r["association"] == "has_many"
    assert r["unresolved"] is True
    assert r["confidence_tier"] == "INFERRED"


# ---------------------------------------------------------------------------
# query_graph: inheritors_of for a Ruby base
# ---------------------------------------------------------------------------


def test_inheritors_of_ruby_base(tmp_path):
    from code_review_graph.tools.query import query_graph

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        res = query_graph(
            pattern="inheritors_of",
            target=_post_qn(tmp_path),
            repo_root=str(tmp_path),
        )
    finally:
        store.close()

    assert res["status"] == "ok"
    names = {r.get("name") for r in res["results"]}
    assert "SpecialPost" in names
    inh_edges = [e for e in res["edges"] if e["kind"] == "INHERITS"]
    assert inh_edges
    # The edge target is the resolved Post node; the source is SpecialPost.
    assert any("SpecialPost" in e["source"] for e in inh_edges)


# ---------------------------------------------------------------------------
# query_graph: children_of for a brom model (synthesized accessors)
# ---------------------------------------------------------------------------


def test_children_of_brom_model_returns_getter_and_setter(tmp_path):
    from code_review_graph.tools.query import query_graph

    _init_repo(tmp_path)
    _write(
        tmp_path,
        "user.rb",
        "class User < ApplicationModel\n"
        "  attribute :id\n"
        "  attribute :name, :string\n"
        "end\n",
    )
    store = _build(tmp_path)
    try:
        res = query_graph(
            pattern="children_of",
            target=f"{tmp_path}/user.rb::User",
            repo_root=str(tmp_path),
        )
    finally:
        store.close()

    names = {r["name"] for r in res["results"]}
    # The synthesized accessor Functions (surfaced via ruby_owner_qn) include
    # both the getter and the setter for each attribute.
    assert {"id", "id=", "name", "name="} <= names


# ---------------------------------------------------------------------------
# confidence_tier present in standard AND minimal output
# ---------------------------------------------------------------------------


def test_confidence_tier_in_standard_and_minimal(tmp_path):
    from code_review_graph.tools.query import query_graph

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        standard = query_graph(
            pattern="mixins_of",
            target=_post_qn(tmp_path),
            repo_root=str(tmp_path),
            detail_level="standard",
        )
        minimal = query_graph(
            pattern="mixins_of",
            target=_post_qn(tmp_path),
            repo_root=str(tmp_path),
            detail_level="minimal",
        )
    finally:
        store.close()

    assert standard["results"]
    assert all("confidence_tier" in r for r in standard["results"])
    assert minimal["results"]
    assert all("confidence_tier" in r for r in minimal["results"])
    # Minimal output is a strict subset of keys.
    assert set(minimal["results"][0]) <= {
        "name", "kind", "file_path", "confidence_tier", "unresolved",
    }


def test_unresolved_marker_on_bare_call_target(tmp_path):
    """callees_of surfaces an ambiguous bare Ruby call target with
    unresolved=True (and the marker survives into minimal output)."""
    from code_review_graph.tools.query import query_graph

    _init_repo(tmp_path)
    # Two classes named Builder -> bare receiver is ambiguous -> unresolved.
    _write(tmp_path, "a.rb", "module A\n  class Builder\n    def self.go; end\n  end\nend\n")
    _write(tmp_path, "b.rb", "module B\n  class Builder\n    def self.go; end\n  end\nend\n")
    _write(tmp_path, "runner.rb", "class Runner\n  def run\n    Builder.go\n  end\nend\n")
    store = _build(tmp_path)
    try:
        standard = query_graph(
            pattern="callees_of",
            target=f"{tmp_path}/runner.rb::Runner.run",
            repo_root=str(tmp_path),
        )
        minimal = query_graph(
            pattern="callees_of",
            target=f"{tmp_path}/runner.rb::Runner.run",
            repo_root=str(tmp_path),
            detail_level="minimal",
        )
    finally:
        store.close()

    go = next((r for r in standard["results"] if r.get("name") == "go"), None)
    assert go is not None, standard["results"]
    assert go["unresolved"] is True
    assert "::" not in go["qualified_name"]
    assert "confidence_tier" in go

    go_min = next((r for r in minimal["results"] if r.get("name") == "go"), None)
    assert go_min is not None
    assert go_min["unresolved"] is True


# ---------------------------------------------------------------------------
# detect_changes: inferred_edge_count
# ---------------------------------------------------------------------------


def test_detect_changes_reports_inferred_edge_count(tmp_path):
    from code_review_graph.tools import detect_changes_func

    _init_repo(tmp_path)
    _write(
        tmp_path,
        "app/models/post.rb",
        "class Post < ApplicationRecord\n"
        "  has_many :comments\n"
        "end\n",
    )
    _write(tmp_path, "app/models/comment.rb", "class Comment < ApplicationRecord\nend\n")
    store = _build(tmp_path)

    rel = "app/models/post.rb"
    try:
        with (
            patch("code_review_graph.tools.review._get_store") as mock_gs,
            patch(
                "code_review_graph.tools.review.get_changed_files",
                return_value=[rel],
            ),
            patch(
                "code_review_graph.tools.review.parse_diff_ranges",
                return_value={},
            ),
        ):
            mock_gs.return_value = (store, tmp_path)
            store.close = lambda: None  # tool must not close our store
            res = detect_changes_func(
                base="HEAD~1", repo_root=str(tmp_path), changed_files=[rel]
            )
    finally:
        GraphStore.close(store)

    assert res["status"] == "ok"
    assert "inferred_edge_count" in res
    # The changed Post class has one INFERRED ASSOCIATES edge (has_many).
    assert res["inferred_edge_count"] >= 1


def test_detect_changes_inferred_edge_count_in_minimal(tmp_path):
    from code_review_graph.tools import detect_changes_func

    _init_repo(tmp_path)
    _write(
        tmp_path,
        "app/models/post.rb",
        "class Post < ApplicationRecord\n"
        "  belongs_to :author, class_name: 'User'\n"
        "end\n",
    )
    _write(tmp_path, "app/models/user.rb", "class User < ApplicationRecord\nend\n")
    store = _build(tmp_path)

    rel = "app/models/post.rb"
    try:
        with (
            patch("code_review_graph.tools.review._get_store") as mock_gs,
            patch(
                "code_review_graph.tools.review.get_changed_files",
                return_value=[rel],
            ),
            patch(
                "code_review_graph.tools.review.parse_diff_ranges",
                return_value={},
            ),
        ):
            mock_gs.return_value = (store, tmp_path)
            store.close = lambda: None
            res = detect_changes_func(
                base="HEAD~1",
                repo_root=str(tmp_path),
                changed_files=[rel],
                detail_level="minimal",
            )
    finally:
        GraphStore.close(store)

    assert res["status"] == "ok"
    assert res["inferred_edge_count"] >= 1


# ---------------------------------------------------------------------------
# flows: Rails entry points
# ---------------------------------------------------------------------------


def test_controller_public_action_is_entry_point(tmp_path):
    from code_review_graph.flows import detect_entry_points

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        eps = detect_entry_points(store)
        pairs = {(n.parent_name, n.name) for n in eps}
    finally:
        store.close()

    assert ("PostsController", "index") in pairs
    assert ("PostsController", "show") in pairs


def test_private_controller_method_is_not_entry_point(tmp_path):
    """set_post is private (in ruby_nonpublic_methods) and has no callers, so
    the ONLY reason it is excluded is the nonpublic filter."""
    from code_review_graph.flows import detect_entry_points

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        eps = detect_entry_points(store)
        names = {(n.parent_name, n.name) for n in eps}
        # Confirm the class did record set_post as nonpublic.
        ctrl = store.get_node(
            f"{tmp_path}/app/controllers/posts_controller.rb::PostsController"
        )
    finally:
        store.close()

    assert "set_post" in (ctrl.extra.get("ruby_nonpublic_methods") or [])
    assert ("PostsController", "set_post") not in names


def test_job_perform_is_entry_point_but_other_methods_are_not(tmp_path):
    from code_review_graph.flows import detect_entry_points

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        eps = detect_entry_points(store)
        pairs = {(n.parent_name, n.name) for n in eps}
    finally:
        store.close()

    assert ("CleanupJob", "perform") in pairs
    # A private job method is not an entry point.
    assert ("CleanupJob", "helper") not in pairs


def test_job_role_filter_only_perform_is_rails_entry(tmp_path):
    """For the `job` role, `_is_rails_entry` accepts only `perform`; any other
    public method is rejected by the role-specific filter (job -> perform only).
    """
    from code_review_graph.flows import _build_ruby_class_index, _is_rails_entry

    _init_repo(tmp_path)
    _write(
        tmp_path,
        "app/jobs/report_job.rb",
        "class ReportJob < ApplicationJob\n"
        "  def perform\n  end\n"
        "  def build\n  end\n"
        "end\n",
    )
    store = _build(tmp_path)
    try:
        idx = _build_ruby_class_index(store)
        perform = store.get_node(f"{tmp_path}/app/jobs/report_job.rb::ReportJob.perform")
        build = store.get_node(f"{tmp_path}/app/jobs/report_job.rb::ReportJob.build")
    finally:
        store.close()

    assert idx.get("ReportJob", {}).get("role") == "job"
    assert _is_rails_entry(perform, idx) is True
    # `build` is a public job method but not `perform` -> rejected by the
    # job-specific role filter.
    assert _is_rails_entry(build, idx) is False


# ---------------------------------------------------------------------------
# enrich: _format_node_context surfaces Rails metadata
# ---------------------------------------------------------------------------


def test_enrich_format_node_context_surfaces_rails_metadata(tmp_path):
    from code_review_graph.enrich import _format_node_context

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        post = store.get_node(_post_qn(tmp_path))
        lines = _format_node_context(post, store, store._conn, str(tmp_path))
    finally:
        store.close()

    text = "\n".join(lines)
    assert "Rails role: model" in text
    assert "Mixes in: Trackable" in text
    assert "Scopes: recent" in text
    # Associations are pulled from the ASSOCIATES edges (resolved targets).
    assert "Associations:" in text
    assert "has_many" in text


# ---------------------------------------------------------------------------
# embeddings: _node_to_text folds in Rails metadata
# ---------------------------------------------------------------------------


def test_embeddings_node_to_text_includes_rails_metadata(tmp_path):
    from code_review_graph.embeddings import _node_to_text

    _resolved_rails_app(tmp_path)
    store = _build(tmp_path)
    try:
        post = store.get_node(_post_qn(tmp_path))
    finally:
        store.close()

    text = _node_to_text(post)
    assert "rails model" in text  # rails_role
    assert "Trackable" in text  # mixins
    assert "has_many" in text  # associations
    assert "recent" in text  # rails_scopes
