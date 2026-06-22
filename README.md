<h1 align="center">code-review-graph · Ruby/Rails edition</h1>

<p align="center">
  <strong>A persistent, incremental code knowledge graph for token-efficient, context-aware code review — with first-class Ruby & Rails support.</strong>
</p>

<p align="center">
  Fork of <a href="https://github.com/tirth8205/code-review-graph">tirth8205/code-review-graph</a> that brings Ruby and Rails to parity with the other supported languages.
</p>

---

## What it is

`code-review-graph` parses a codebase with Tree-sitter, builds a structural
knowledge graph (definitions, calls, imports, inheritance, tests…), and exposes
it to AI agents and humans over an **MCP server** and a **CLI**. Instead of
re-reading whole files, an agent can ask the graph precise questions —
*who calls this?*, *what tests cover it?*, *what breaks if I change it?* — and
get token-cheap, structural answers.

This fork makes **Ruby a first-class language**: the graph understands Rails and
common Ruby idioms, not just bare method definitions.

## What this fork adds for Ruby

**Structure & object model**
- Classes, modules, methods, singleton methods and `class << self`, constants.
- Inheritance → `INHERITS`; mixins `include` / `extend` / `prepend` →
  `INCLUDES` / `EXTENDS` / `PREPENDS`, resolved to the real target module.

**Rails-aware relationships**
- ActiveRecord associations (`has_many`, `belongs_to`, `has_one`,
  `has_and_belongs_to_many`) → `ASSOCIATES`, resolved to the model node and
  honoring `class_name:` and `through:`.
- `delegate … to:` → `DELEGATES`.
- `ActiveModel::Attributes` (`attribute :name`) → typed accessor nodes.
- `ActiveSupport::Concern` blocks (`included do`, `class_methods do`): macros and
  method definitions inside a concern propagate to the classes that include it.
- Rails roles & flow entry points — controllers, jobs, background consumers and
  service objects are recognized as the start of execution flows.

**Dependency injection (dry-rb)**
- `register('key') { SomeClass.new }` containers plus `include Import['key']`
  resolve to `DEPENDS_ON` edges — read from the **explicit container registry**,
  never guessed from naming conventions, so no false dependencies.

**Tests & coverage**
- RSpec `describe SomeClass` is the coverage anchor: one precise `TESTED_BY`
  edge from the spec to the class under test (direction: test → production),
  instead of one noisy edge per `let`/`expect`/`before` call. `tests_for` rolls
  a method-level query up to its class, so "what tests this method?" finds the
  spec that describes its class.
- SimpleCov `.resultset.json` line coverage is ingested per node, so review
  tooling can see real, per-line coverage — not just whether a test file exists.

**Call resolution that under-claims instead of over-claiming**
- Receiver-aware: constant receivers resolve to the right singleton method
  (and `.new` → `initialize`), same-object/`self` calls resolve within the
  class, and calls on unknown locals/injected dependencies are left
  **unresolved rather than mis-attributed**. The guiding rule is that a missing
  edge is better than a false one.
- Every inferred edge carries a confidence tier — `EXTRACTED`, `INFERRED`, or
  `AMBIGUOUS` — and unresolved targets are marked, so consumers can trust or
  discount each edge.

**Cross-repo**
- Search and relationship queries work across many registered repositories at
  once — useful when Ruby services span more than one repo.

### Compatibility
All other languages and the MCP / CLI contract are unchanged, and there is no
database migration — the Ruby work is additive and gated so existing graphs keep
working exactly as before.

## Install & wire to an MCP client

Run the server straight from this fork with [`uv`](https://docs.astral.sh/uv/):

```jsonc
// .mcp.json (or your client's MCP config)
{
  "mcpServers": {
    "code-review-graph": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/nnslvp/code-review-graph@ruby-rails-firstclass",
        "code-review-graph",
        "serve"
      ],
      "type": "stdio"
    }
  }
}
```

The agent then has the graph tools available (build, query, impact radius,
review context, cross-repo search, and more).

## CLI quickstart

```bash
# Run any command from this fork without installing it globally:
alias crg='uvx --from git+https://github.com/nnslvp/code-review-graph@ruby-rails-firstclass code-review-graph'

crg build            # build the graph for the current repo
crg status           # node / edge / language stats
crg update           # incremental update after changes
crg register .       # add the repo to the multi-repo registry
crg serve            # start the MCP server (stdio)
```

The graph is stored locally under `.code-review-graph/` and updates
incrementally as files change.

## Credits

Built on top of [tirth8205/code-review-graph](https://github.com/tirth8205/code-review-graph).
This fork focuses on bringing Ruby and Rails support up to first-class quality.
Licensed under MIT (see [LICENSE](LICENSE)).
