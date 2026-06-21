"""Coverage-fill tests for the implemented Rails DSL subset of the Ruby parser.

Scope: ONLY features already implemented in ``code_review_graph/parser.py``:
  * ActiveRecord associations (has_many / belongs_to / has_one /
    has_and_belongs_to_many) -> ASSOCIATES edges with extra.association,
    extra.name, extra.confidence_tier=INFERRED, extra.options, and a correctly
    resolved target (camelized singular for has_many/habtm; camelized as-is for
    has_one/belongs_to; class_name: override incl. single-quoted stripping).
  * validates / validate -> class node extra.rails_validations.
  * scope -> class node extra.rails_scopes.
  * before_*/after_*/around_* callbacks -> class node extra.rails_callbacks.
  * rails_role from superclass (model/controller/job/mailer) and via path
    convention (app/models|controllers|jobs|mailers) for plain classes.
  * no junk CALLS edges emitted for the DSL macro names.

These complement the existing TestRailsRole / TestRailsDSL cases in
tests/test_multilang.py; they target the gaps NOT exercised there
(mailer role, ActionMailer::Base, path-convention fallback for every role,
the ``validate`` macro, around_/after_ callbacks, the ASSOCIATES edge extra
payload, and the non-trivial pluralization/camelization paths).
"""

from code_review_graph.parser import CodeParser


def _classes(nodes):
    return {n.name: n for n in nodes if n.kind == "Class"}


def _assoc_edges(edges):
    return [e for e in edges if e.kind == "ASSOCIATES"]


# --------------------------------------------------------------------------
# rails_role via superclass
# --------------------------------------------------------------------------

def test_rails_role_mailer_via_application_mailer(tmp_path):
    f = tmp_path / "welcome_mailer.rb"
    f.write_text(
        "class WelcomeMailer < ApplicationMailer\n"
        "  def welcome\n"
        "  end\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["WelcomeMailer"].extra.get("rails_role") == "mailer"


def test_rails_role_mailer_via_actionmailer_base(tmp_path):
    f = tmp_path / "legacy_mailer.rb"
    f.write_text(
        "class LegacyMailer < ActionMailer::Base\n"
        "  def notify\n"
        "  end\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["LegacyMailer"].extra.get("rails_role") == "mailer"


def test_rails_role_model_via_active_record_base(tmp_path):
    f = tmp_path / "ledger.rb"
    f.write_text(
        "class Ledger < ActiveRecord::Base\n"
        "  def total\n"
        "  end\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["Ledger"].extra.get("rails_role") == "model"


# --------------------------------------------------------------------------
# rails_role via PATH convention (plain class, no Rails superclass)
# --------------------------------------------------------------------------

def _write_under(tmp_path, rel_dir, filename, body):
    d = tmp_path / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    f = d / filename
    f.write_text(body)
    return f


def test_rails_role_model_via_path_convention(tmp_path):
    f = _write_under(
        tmp_path, "app/models", "widget.rb",
        "class Widget\n  def label\n  end\nend\n",
    )
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["Widget"].extra.get("rails_role") == "model"


def test_rails_role_controller_via_path_convention(tmp_path):
    f = _write_under(
        tmp_path, "app/controllers", "things_controller.rb",
        "class ThingsController\n  def index\n  end\nend\n",
    )
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["ThingsController"].extra.get("rails_role") == "controller"


def test_rails_role_job_via_path_convention(tmp_path):
    f = _write_under(
        tmp_path, "app/jobs", "cleanup_job.rb",
        "class Cleanup\n  def perform\n  end\nend\n",
    )
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["Cleanup"].extra.get("rails_role") == "job"


def test_rails_role_mailer_via_path_convention(tmp_path):
    f = _write_under(
        tmp_path, "app/mailers", "news_mailer.rb",
        "class News\n  def digest\n  end\nend\n",
    )
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["News"].extra.get("rails_role") == "mailer"


def test_rails_role_superclass_wins_over_path(tmp_path):
    """A class under app/models but subclassing ApplicationController keeps the
    superclass-derived role (controller); the path fallback only runs when the
    superclass yields no role."""
    f = _write_under(
        tmp_path, "app/models", "odd.rb",
        "class Odd < ApplicationController\n  def index\n  end\nend\n",
    )
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["Odd"].extra.get("rails_role") == "controller"


def test_plain_class_outside_app_has_no_rails_role(tmp_path):
    f = tmp_path / "plain.rb"
    f.write_text("class Plain\n  def x\n  end\nend\n")
    nodes, _ = CodeParser().parse_file(f)
    assert _classes(nodes)["Plain"].extra.get("rails_role") is None


# --------------------------------------------------------------------------
# Associations: target resolution + edge extra payload
# --------------------------------------------------------------------------

def test_has_many_target_is_camelized_singular(tmp_path):
    """has_many singularizes then camelizes; covers the ies->y and snake paths."""
    f = tmp_path / "account.rb"
    f.write_text(
        "class Account < ApplicationRecord\n"
        "  has_many :categories\n"
        "  has_many :line_items\n"
        "end\n"
    )
    _, edges = CodeParser().parse_file(f)
    pairs = {(e.extra.get("association"), e.target) for e in _assoc_edges(edges)}
    assert ("has_many", "Category") in pairs
    assert ("has_many", "LineItem") in pairs


def test_belongs_to_and_has_one_camelize_without_singularizing(tmp_path):
    f = tmp_path / "membership.rb"
    f.write_text(
        "class Membership < ApplicationRecord\n"
        "  belongs_to :user_account\n"
        "  has_one :billing_profile\n"
        "end\n"
    )
    _, edges = CodeParser().parse_file(f)
    pairs = {(e.extra.get("association"), e.target) for e in _assoc_edges(edges)}
    assert ("belongs_to", "UserAccount") in pairs
    assert ("has_one", "BillingProfile") in pairs


def test_has_and_belongs_to_many_target_is_camelized_singular(tmp_path):
    f = tmp_path / "team.rb"
    f.write_text(
        "class Team < ApplicationRecord\n"
        "  has_and_belongs_to_many :players\n"
        "end\n"
    )
    _, edges = CodeParser().parse_file(f)
    pairs = {(e.extra.get("association"), e.target) for e in _assoc_edges(edges)}
    assert ("has_and_belongs_to_many", "Player") in pairs


def test_class_name_override_double_quoted(tmp_path):
    f = tmp_path / "invoice.rb"
    f.write_text(
        "class Invoice < ApplicationRecord\n"
        '  belongs_to :buyer, class_name: "Customer"\n'
        "end\n"
    )
    _, edges = CodeParser().parse_file(f)
    edge = next(e for e in _assoc_edges(edges) if e.extra.get("name") == "buyer")
    assert edge.target == "Customer"
    assert edge.extra.get("options", {}).get("class_name") == "Customer"


def test_class_name_override_single_quoted_is_stripped(tmp_path):
    f = tmp_path / "shipment.rb"
    f.write_text(
        "class Shipment < ApplicationRecord\n"
        "  belongs_to :carrier, class_name: 'Org'\n"
        "end\n"
    )
    _, edges = CodeParser().parse_file(f)
    edge = next(e for e in _assoc_edges(edges) if e.extra.get("name") == "carrier")
    # single quotes must be stripped: target is the bare class name, not "'Org'"
    assert edge.target == "Org"
    assert "'" not in edge.target


def test_class_name_override_on_has_many_overrides_singularize(tmp_path):
    """When class_name: is given, the literal value is used verbatim and the
    singularize/camelize pipeline is bypassed."""
    f = tmp_path / "blog.rb"
    f.write_text(
        "class Blog < ApplicationRecord\n"
        '  has_many :entries, class_name: "Article"\n'
        "end\n"
    )
    _, edges = CodeParser().parse_file(f)
    edge = next(e for e in _assoc_edges(edges) if e.extra.get("name") == "entries")
    assert edge.target == "Article"


def test_associates_edge_extra_payload(tmp_path):
    """ASSOCIATES edges carry association macro, the symbol name, the parsed
    options dict, and a confidence_tier of INFERRED."""
    f = tmp_path / "author.rb"
    f.write_text(
        "class Author < ApplicationRecord\n"
        "  has_many :posts, dependent: :destroy\n"
        "end\n"
    )
    src = f"{f}::Author"
    _, edges = CodeParser().parse_file(f)
    edge = next(e for e in _assoc_edges(edges) if e.extra.get("name") == "posts")
    assert edge.source == src
    assert edge.target == "Post"
    assert edge.extra.get("association") == "has_many"
    assert edge.extra.get("confidence_tier") == "INFERRED"
    assert edge.extra.get("options", {}).get("dependent") == "destroy"


def test_class_node_associations_summary_extra(tmp_path):
    """The class node accumulates a human-readable associations list in extra."""
    f = tmp_path / "store.rb"
    f.write_text(
        "class Store < ApplicationRecord\n"
        "  has_many :orders\n"
        "  belongs_to :region\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    summary = _classes(nodes)["Store"].extra.get("associations", [])
    assert "has_many Order" in summary
    assert "belongs_to Region" in summary


# --------------------------------------------------------------------------
# validations / validate
# --------------------------------------------------------------------------

def test_validates_macro_records_fields(tmp_path):
    f = tmp_path / "person.rb"
    f.write_text(
        "class Person < ApplicationRecord\n"
        "  validates :email, presence: true\n"
        "  validates :age, numericality: true\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    validations = _classes(nodes)["Person"].extra.get("rails_validations", [])
    assert "email" in validations
    assert "age" in validations


def test_validate_macro_without_s_records_method_name(tmp_path):
    """The bare ``validate :method`` macro is also captured in rails_validations."""
    f = tmp_path / "ticket.rb"
    f.write_text(
        "class Ticket < ApplicationRecord\n"
        "  validate :custom_rule\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    assert "custom_rule" in _classes(nodes)["Ticket"].extra.get("rails_validations", [])


def test_validates_presence_of_macro(tmp_path):
    f = tmp_path / "doc.rb"
    f.write_text(
        "class Doc < ApplicationRecord\n"
        "  validates_presence_of :title\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    assert "title" in _classes(nodes)["Doc"].extra.get("rails_validations", [])


# --------------------------------------------------------------------------
# scopes
# --------------------------------------------------------------------------

def test_scope_macro_records_name(tmp_path):
    f = tmp_path / "product.rb"
    f.write_text(
        "class Product < ApplicationRecord\n"
        "  scope :published, -> { where(published: true) }\n"
        "  scope :recent, -> { order(created_at: :desc) }\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    scopes = _classes(nodes)["Product"].extra.get("rails_scopes", [])
    assert "published" in scopes
    assert "recent" in scopes


# --------------------------------------------------------------------------
# callbacks: before_/after_/around_ families
# --------------------------------------------------------------------------

def test_model_lifecycle_callbacks(tmp_path):
    f = tmp_path / "subscriber.rb"
    f.write_text(
        "class Subscriber < ApplicationRecord\n"
        "  before_save :normalize\n"
        "  after_create :send_welcome\n"
        "  before_validation :sanitize\n"
        "  after_commit :reindex\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    callbacks = _classes(nodes)["Subscriber"].extra.get("rails_callbacks", [])
    assert "before_save:normalize" in callbacks
    assert "after_create:send_welcome" in callbacks
    assert "before_validation:sanitize" in callbacks
    assert "after_commit:reindex" in callbacks


def test_controller_action_callbacks_including_around(tmp_path):
    f = tmp_path / "admin_controller.rb"
    f.write_text(
        "class AdminController < ApplicationController\n"
        "  before_action :authenticate\n"
        "  around_action :with_timing\n"
        "  after_action :log\n"
        "  def index\n"
        "  end\n"
        "end\n"
    )
    nodes, _ = CodeParser().parse_file(f)
    callbacks = _classes(nodes)["AdminController"].extra.get("rails_callbacks", [])
    assert "before_action:authenticate" in callbacks
    assert "around_action:with_timing" in callbacks
    assert "after_action:log" in callbacks


# --------------------------------------------------------------------------
# no junk CALLS for DSL macro names
# --------------------------------------------------------------------------

def test_no_junk_calls_for_rails_dsl_macros(tmp_path):
    f = tmp_path / "kitchen_sink.rb"
    f.write_text(
        "class KitchenSink < ApplicationRecord\n"
        "  has_many :items\n"
        "  belongs_to :owner\n"
        "  has_one :config\n"
        "  has_and_belongs_to_many :tags\n"
        "  validates :name, presence: true\n"
        "  validate :extra\n"
        "  scope :live, -> { where(live: true) }\n"
        "  before_save :touch\n"
        "  after_create :ping\n"
        "  around_action :wrap\n"
        "end\n"
    )
    _, edges = CodeParser().parse_file(f)
    call_targets = {
        e.target.split("::")[-1].split(".")[-1]
        for e in edges if e.kind == "CALLS"
    }
    for macro in (
        "has_many", "belongs_to", "has_one", "has_and_belongs_to_many",
        "validates", "validate", "scope", "before_save", "after_create",
        "around_action",
    ):
        assert macro not in call_targets, f"DSL macro {macro!r} leaked a junk CALLS edge"
