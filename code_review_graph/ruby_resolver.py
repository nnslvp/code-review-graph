"""Post-build cross-module resolver for Ruby/Rails.

Resolves: require_relative raw strings -> real .rb file paths; Zeitwerk
CamelCase constants (INHERITS / INCLUDES / EXTENDS / PREPENDS / ASSOCIATES
targets) -> <file>::Const node qualified names. Idempotent via the
extra.ruby_resolved flag. No-op when the graph has no .rb files.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

_CONST_KINDS = ("INHERITS", "INCLUDES", "EXTENDS", "PREPENDS", "ASSOCIATES")

# Matches a Ruby constant: starts with an uppercase letter, no lowercase-then-uppercase
# camel case needed — just "starts with upper" is enough for a receiver constant check.
def _is_constant(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _camel_to_path(const_name: str) -> str:
    parts = const_name.split("::")
    out = []
    for p in parts:
        s: list[str] = []
        for i, ch in enumerate(p):
            if ch.isupper() and i > 0:
                s.append("_")
            s.append(ch.lower())
        out.append("".join(s))
    return "/".join(out)


def _update_edge(cur, target_qualified: str, extra: dict, edge_id: int) -> None:
    """Write a resolved target + INFERRED confidence back to an edge row."""
    extra["ruby_resolved"] = True
    cur.execute(
        "UPDATE edges SET target_qualified=?, extra=?,"
        " confidence=?, confidence_tier=? WHERE id=?",
        (target_qualified, json.dumps(extra), 0.7, "INFERRED", edge_id),
    )


def _extract_block_body_first_node(block_node):
    """Return the first named child of a block/do_block body, or None."""
    for btype in ("block_body", "body_statement"):
        body = None
        for child in block_node.children:
            if child.type == btype:
                body = child
                break
        if body is not None:
            for child in body.children:
                if child.is_named:
                    return child
            break
    return None


def _const_from_block(block_node) -> str | None:
    """Extract a static constant name from a register() block body.

    Handles:
      - Bare constant:  { ErrorNotifier }  or  { Logging::Logger }
      - .new call:      { Logging::Logger.new }  or  { Logging::Logger.new(...) }

    Returns the Ruby constant string (e.g. "Logging::Logger", "ErrorNotifier"),
    or None when the body is not a static constant expression (e.g. Rails.logger,
    Karafka.producer, resolve(...), conditionals, factories).
    """
    first = _extract_block_body_first_node(block_node)
    if first is None:
        return None

    if first.type == "constant":
        return first.text.decode("utf-8", errors="replace")

    if first.type == "scope_resolution":
        return first.text.decode("utf-8", errors="replace")

    if first.type == "call":
        receiver = first.child_by_field_name("receiver")
        method_node = first.child_by_field_name("method")
        if method_node is None:
            return None
        method = method_node.text.decode("utf-8", errors="replace") if method_node.text else ""
        if method != "new":
            return None
        if receiver is None:
            return None
        if receiver.type in ("constant", "scope_resolution"):
            return receiver.text.decode("utf-8", errors="replace")

    return None


def _find_register_calls(node, key_map: dict[str, str]) -> None:
    """Recursively walk *node* and populate *key_map* with ``register('k') { C }`` entries."""
    if node.type == "call":
        method = node.child_by_field_name("method")
        if method is not None and method.text == b"register":
            args = node.child_by_field_name("arguments")
            block = None
            for child in node.children:
                if child.type in ("block", "do_block"):
                    block = child
                    break
            if args is not None and block is not None:
                key_str: str | None = None
                for arg in args.children:
                    if arg.type == "string":
                        for sc in arg.children:
                            if sc.type == "string_content":
                                key_str = sc.text.decode("utf-8", errors="replace")
                                break
                        if key_str is not None:
                            break
                if key_str is not None:
                    const_name = _const_from_block(block)
                    if const_name is not None:
                        key_map[key_str] = const_name
            return
    for child in node.children:
        _find_register_calls(child, key_map)


def _build_container_key_map(files: list[str]) -> dict[str, str]:
    """Scan Ruby files that include Dry::Container::Mixin and build key -> constant map.

    Only files whose source contains the literal string 'Dry::Container::Mixin'
    are parsed. For each ``register('key') { Body }`` or ``register('key') do … end``
    call, extract the constant from the body. Bodies that are not a static constant
    expression are omitted (no edge emitted for ambiguous/dynamic registrations).

    Returns a dict mapping DI key strings to Ruby constant names
    (e.g. {'core.logger': 'Logging::Logger', 'core.notifier': 'ErrorNotifier'}).
    """
    try:
        import tree_sitter_language_pack as tslp
    except ImportError:
        logger.warning(
            "ruby_resolver: tree_sitter_language_pack not available; DI resolution skipped"
        )
        return {}

    parser = tslp.get_parser("ruby")
    key_map: dict[str, str] = {}

    for fp in files:
        try:
            source = Path(fp).read_bytes()
        except OSError:
            continue
        if b"Dry::Container::Mixin" not in source:
            continue

        tree = parser.parse(source)
        _find_register_calls(tree.root_node, key_map)

    return key_map


def _qn_to_ruby_full_path(qn: str) -> str:
    """Derive the full Ruby constant path from a node qualified_name.

    A node qn has the form ``<file_path>::A.B.Logger``.  The namespace
    portion after the first ``::`` uses ``.`` as separator; convert that
    to Ruby's ``::`` notation → ``A::B::Logger``.
    """
    if "::" not in qn:
        return ""
    ns_part = qn.split("::", 1)[1]
    return ns_part.replace(".", "::")


def _build_di_const_indexes(
    conn,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return (full_path_to_qn, bare_name_to_qns) for Ruby Class/Type nodes.

    full_path_to_qn maps e.g. ``Logging::Logger`` -> ``<file>::Logging.Logger``.
    bare_name_to_qns maps e.g. ``Logger`` -> [``<file>::Logging.Logger``, ...].
    """
    full_path_to_qn: dict[str, str] = {}
    bare_name_to_qns: dict[str, list[str]] = {}

    for row in conn.execute(
        "SELECT qualified_name, name FROM nodes"
        " WHERE language='ruby' AND kind IN ('Class', 'Type')"
    ).fetchall():
        qn: str = row["qualified_name"]
        bare: str = row["name"]
        full_path = _qn_to_ruby_full_path(qn)
        if full_path:
            full_path_to_qn[full_path] = qn
        bare_name_to_qns.setdefault(bare, []).append(qn)

    return full_path_to_qn, bare_name_to_qns


def _resolve_const_to_node(
    const_name: str,
    full_path_to_qn: dict[str, str],
    bare_name_to_qns: dict[str, list[str]],
) -> str | None:
    """Resolve a Ruby constant string to a node qualified_name.

    Resolution order:
    1. Exact full-path match: const_name == full_path key (e.g. ``Logging::Logger``).
    2. Suffix full-path match: any full_path ends with ``"::" + const_name``
       (handles partially-qualified references like ``Logger`` matching ``Logging::Logger``
       only when that suffix is unambiguous).
    3. Bare-name fallback: if const_name contains no ``::`` and exactly one class has
       that bare name, resolve to it.
    4. Otherwise omit (returns None — no false edges).
    """
    if exact := full_path_to_qn.get(const_name):
        return exact

    suffix = "::" + const_name
    suffix_matches = [qn for fp, qn in full_path_to_qn.items() if fp.endswith(suffix)]
    if len(suffix_matches) == 1:
        return suffix_matches[0]

    if "::" not in const_name:
        candidates = bare_name_to_qns.get(const_name, [])
        if len(candidates) == 1:
            return candidates[0]

    return None


def _resolve_di_imports(
    store: "GraphStore",
    key_map: dict[str, str],
    full_path_to_qn: dict[str, str],
    bare_name_to_qns: dict[str, list[str]],
) -> int:
    """Emit DEPENDS_ON edges for classes with extra['di_keys'] resolved via key_map.

    For each class node that has di_keys set (populated by the parser when it sees
    include Ns::Import['k1','k2',...]), look up each key in key_map to get a
    Ruby constant name, then resolve that constant to a qualified node name using
    the full-path index (exact or suffix match) with bare-name fallback only when
    the name is unique. Emit a DEPENDS_ON edge with INFERRED confidence tier. Keys
    not found in key_map, or constants not resolvable unambiguously, are silently
    omitted (never convention-guessed, never picked arbitrarily).

    Returns the number of DEPENDS_ON edges emitted.
    """
    if not key_map:
        return 0

    conn = store._conn
    cur = conn.cursor()
    emitted = 0

    for row in conn.execute(
        "SELECT qualified_name, file_path, extra FROM nodes"
        " WHERE language='ruby' AND kind IN ('Class', 'Type')"
    ).fetchall():
        extra = json.loads(row["extra"] or "{}")
        di_keys: list[str] = extra.get("di_keys", [])
        if not di_keys:
            continue
        src_qn: str = row["qualified_name"]
        fp: str = row["file_path"]
        for key in di_keys:
            const_name = key_map.get(key)
            if const_name is None:
                continue
            target_qn = _resolve_const_to_node(const_name, full_path_to_qn, bare_name_to_qns)
            if target_qn is None:
                continue
            edge_extra = json.dumps({"confidence_tier": "INFERRED", "di_key": key})
            existing = conn.execute(
                "SELECT id FROM edges"
                " WHERE kind='DEPENDS_ON' AND source_qualified=?"
                "   AND target_qualified=? AND file_path=?",
                (src_qn, target_qn, fp),
            ).fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO edges"
                    " (kind, source_qualified, target_qualified, file_path, line,"
                    "  confidence, confidence_tier, extra, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("DEPENDS_ON", src_qn, target_qn, fp, 0, 0.7, "INFERRED",
                     edge_extra, time.time()),
                )
                emitted += 1

    return emitted


def resolve_ruby_cross_module(store: "GraphStore") -> dict:
    """Resolve Ruby cross-module targets in the graph store.

    Safe to call multiple times: already-resolved edges (flagged via
    extra['ruby_resolved']) are skipped.

    Returns a dict with resolution counts for telemetry.
    """
    files = [f for f in store.get_all_files() if f.endswith(".rb")]
    if not files:
        return {}

    stats: dict[str, int] = {
        "files_indexed": len(files),
        "imports_resolved": 0,
        "consts_resolved": 0,
        "calls_resolved": 0,
        "calls_unresolved": 0,
        "di_edges_emitted": 0,
    }

    conn = store._conn

    # Build const_name -> qualified_name map from ruby Class/Type nodes.
    # Prefer app/ and lib/ over spec/test paths on collisions.
    const_to_qn: dict[str, str] = {}
    rank_map: dict[str, int] = {}
    for row in conn.execute(
        "SELECT qualified_name, name, file_path FROM nodes "
        "WHERE language='ruby' AND kind IN ('Class', 'Type')"
    ).fetchall():
        qn, nm, fp = row["qualified_name"], row["name"], row["file_path"]
        rank = (
            0
            if (
                "/app/" in fp
                or fp.startswith("app/")
                or "/lib/" in fp
                or fp.startswith("lib/")
            )
            else 1
        )
        if nm not in rank_map or rank < rank_map[nm]:
            const_to_qn[nm] = qn
            rank_map[nm] = rank

    # DI container key -> constant resolution (dry-auto_inject)
    try:
        container_key_map = _build_container_key_map(files)
        if container_key_map:
            full_path_to_qn, bare_name_to_qns = _build_di_const_indexes(conn)
            stats["di_edges_emitted"] = _resolve_di_imports(
                store, container_key_map, full_path_to_qn, bare_name_to_qns
            )
    except Exception:
        logger.exception("ruby_resolver: DI resolution failed")

    cur = conn.cursor()

    # 1) require_relative IMPORTS_FROM raw strings -> real .rb path
    for row in cur.execute(
        "SELECT id, target_qualified, extra FROM edges "
        "WHERE kind='IMPORTS_FROM'"
    ).fetchall():
        eid = row["id"]
        tgt = row["target_qualified"]
        extra = json.loads(row["extra"] or "{}")
        if extra.get("ruby_resolved"):
            continue
        if extra.get("require_kind") != "require_relative":
            continue
        # Skip if target is already an existing .rb file path
        if tgt in files:
            continue
        cand = tgt if tgt.endswith(".rb") else tgt + ".rb"
        match = next(
            (f for f in files if f.endswith("/" + cand) or f == cand), None
        )
        if match:
            _update_edge(cur, match, extra, eid)
            stats["imports_resolved"] += 1

    # 2) const-bearing edges (INHERITS/mixins/ASSOCIATES) -> node qn
    for row in cur.execute(
        "SELECT id, kind, target_qualified, extra FROM edges "
        f"WHERE kind IN {_CONST_KINDS}"
    ).fetchall():
        eid = row["id"]
        tgt = row["target_qualified"]
        extra = json.loads(row["extra"] or "{}")
        if extra.get("ruby_resolved"):
            continue
        qn = const_to_qn.get(tgt)
        if qn is None:
            # Zeitwerk path fallback: convert CamelCase -> snake_case path
            path = _camel_to_path(tgt) + ".rb"
            match = next(
                (f for f in files if f.endswith("/" + path) or f == path), None
            )
            if match is not None:
                # Resolve to the node in that file by the last const segment
                last = tgt.split("::")[-1]
                node_row = conn.execute(
                    "SELECT qualified_name FROM nodes "
                    "WHERE file_path=? AND name=? AND language='ruby'",
                    (match, last),
                ).fetchone()
                if node_row:
                    qn = node_row["qualified_name"]
        if qn is not None and qn != tgt:
            _update_edge(cur, qn, extra, eid)
            stats["consts_resolved"] += 1

    # 3) CALLS edges with bare targets -> qualified member node qualnames
    try:
        # Build member index keyed by QUALIFIED class name to avoid bare-name collisions.
        # Two classes with the same short name in different namespaces (e.g. A::Builder and
        # B::Builder) must NOT collide — they get separate entries in this dict.
        member_index: dict[str, dict[str, dict[str, str]]] = {}

        # Secondary index: bare class name -> list of qualified class names.
        # Used to resolve bare receivers: only resolve when exactly one class_qn matches.
        bare_to_class_qns: dict[str, list[str]] = {}

        # Collect member Function nodes via CONTAINS edges from Class nodes.
        # This covers both singleton (ruby_singleton=True) and instance methods,
        # even those without ruby_owner_qn (e.g. plain instance methods).
        for class_row in conn.execute(
            "SELECT qualified_name, name FROM nodes"
            " WHERE language='ruby' AND kind IN ('Class', 'Type')"
        ).fetchall():
            class_qn = class_row["qualified_name"]
            class_bare = class_row["name"]
            # Key by qualified name — no collision risk here
            bucket = member_index.setdefault(class_qn, {"instance": {}, "singleton": {}})
            bare_to_class_qns.setdefault(class_bare, []).append(class_qn)
            # The parser may emit CONTAINS edges from a bare-name source
            # (e.g. "<file>::Builder") even when the class node is namespace-qualified
            # (e.g. "<file>::A.Builder"). Build the bare-source qn as a fallback.
            file_prefix = class_qn.split("::", 1)[0] if "::" in class_qn else ""
            bare_source_qn = f"{file_prefix}::{class_bare}" if file_prefix else class_bare
            sources_to_check = {class_qn, bare_source_qn}
            for source_qn in sources_to_check:
                for member_row in conn.execute(
                    "SELECT n.qualified_name, n.name, n.extra"
                    " FROM edges e JOIN nodes n ON n.qualified_name = e.target_qualified"
                    " WHERE e.kind='CONTAINS' AND e.source_qualified=?"
                    "   AND n.kind='Function' AND n.language='ruby'",
                    (source_qn,),
                ).fetchall():
                    extra_m = json.loads(member_row["extra"] or "{}")
                    is_singleton = extra_m.get("ruby_singleton", False)
                    mname = member_row["name"]
                    mqn = member_row["qualified_name"]
                    tier = "singleton" if is_singleton else "instance"
                    if mname not in bucket[tier]:
                        bucket[tier][mname] = mqn

        # Tier-1 in-memory indexes (mirror the tier-2 member_index pattern):
        # replace the former two-queries-per-edge lookups with O(1) dict gets.
        # On a large repo the tier-1 arm previously ran ~2 SELECTs per no-receiver
        # CALLS edge (hundreds of thousands of queries); these maps make it one
        # scan each. Resolution reads only `nodes` (never the edges being
        # updated), so precomputing them changes no resolution decision.
        # src_to_file: caller qualified_name -> file_path
        #   (was: SELECT file_path FROM nodes WHERE qualified_name=?)
        src_to_file: dict[str, str] = {}
        for nrow in conn.execute(
            "SELECT qualified_name, file_path FROM nodes WHERE language='ruby'"
        ).fetchall():
            src_to_file[nrow["qualified_name"]] = nrow["file_path"]
        # file_fn_index: (file_path, bare_name) -> [qualified_name] for ruby Functions
        #   (was: SELECT qualified_name FROM nodes
        #         WHERE file_path=? AND name=? AND kind='Function' AND language='ruby')
        file_fn_index: dict[tuple[str, str], list[str]] = {}
        for nrow in conn.execute(
            "SELECT qualified_name, file_path, name FROM nodes"
            " WHERE language='ruby' AND kind='Function'"
        ).fetchall():
            file_fn_index.setdefault(
                (nrow["file_path"], nrow["name"]), []
            ).append(nrow["qualified_name"])

        # Accumulate edge writes and flush once via executemany. The unresolved
        # marking alone is one UPDATE per ruby CALLS edge, so per-edge writes were
        # the other half of the cost. Writes never feed back into resolution
        # (resolution reads `nodes` + the in-memory indexes), so deferring them to
        # a single bulk flush is semantically identical to per-edge UPDATEs.
        # resolved tuple: (target_qualified, extra_json, confidence, tier, edge_id)
        resolved_writes: list[tuple[str, str, float, str, int]] = []
        unresolved_writes: list[tuple[str, int]] = []

        # Process bare CALLS edges (no '::' in target_qualified).
        # Join through nodes to restrict to ruby-language callers only, so we
        # don't accidentally stamp extra["unresolved"]=True on non-Ruby edges.
        for row in cur.execute(
            "SELECT e.id, e.source_qualified, e.target_qualified, e.extra"
            " FROM edges e"
            " JOIN nodes n ON n.qualified_name = e.source_qualified"
            " WHERE e.kind='CALLS' AND n.language='ruby'"
        ).fetchall():
            eid = row["id"]
            src = row["source_qualified"]
            tgt = row["target_qualified"]
            extra = json.loads(row["extra"] or "{}")

            if extra.get("ruby_resolved"):
                continue

            # Only process bare targets (no '::' means not yet qualified)
            if "::" in tgt:
                continue

            receiver = extra.get("receiver", "")

            # Tier-1: no receiver — same-file def with matching name (O(1) lookups)
            if not receiver:
                src_file = src_to_file.get(src)
                if src_file is not None:
                    same_file_matches = file_fn_index.get((src_file, tgt), [])
                    if len(same_file_matches) == 1:
                        extra["ruby_resolved"] = True
                        resolved_writes.append(
                            (same_file_matches[0], json.dumps(extra), 1.0, "EXTRACTED", eid)
                        )
                        stats["calls_resolved"] += 1
                        continue
                    # 0 or >1 matches: ambiguous — fall through to unresolved

            # Tier-2: constant receiver (starts with uppercase letter)
            if receiver and _is_constant(receiver):
                # Resolve the receiver to a class_qn.
                # If the receiver is qualified (contains "::"), try to find a class whose
                # qualified_name ends with the receiver string (suffix match).
                # If bare (no "::"), look up bare_to_class_qns; resolve ONLY when unique.
                resolved_class_qn: str | None = None
                if "::" in receiver:
                    # Fully-qualified receiver: find a class_qn that ends with the receiver.
                    # The graph stores qualified names using "." as the namespace separator
                    # (e.g. "<file>::A.Builder"), while the parser records the receiver with
                    # Ruby's "::" separator (e.g. "A::Builder"). Normalize before comparing.
                    receiver_dot = receiver.replace("::", ".")
                    qualified_matches: list[str] = []
                    for cqn in member_index:
                        # Strip the file-path prefix to get the namespace tail, e.g. "A.Builder".
                        ns_part = cqn.split("::", 1)[-1] if "::" in cqn else cqn
                        if ns_part == receiver_dot or ns_part.endswith("." + receiver_dot):
                            qualified_matches.append(cqn)
                    # Resolve only when exactly one class matches — preserve uniqueness gate.
                    if len(qualified_matches) == 1:
                        resolved_class_qn = qualified_matches[0]
                else:
                    # Bare receiver: only resolve if exactly one class has this name
                    candidates = bare_to_class_qns.get(receiver, [])
                    if len(candidates) == 1:
                        resolved_class_qn = candidates[0]
                    # If 0 or >1 candidates: leave resolved_class_qn as None (ambiguous)

                if resolved_class_qn is not None:
                    members = member_index[resolved_class_qn]
                    resolved_qn: str | None = None
                    if tgt == "new":
                        # Builder.new -> initialize instance method (if it exists)
                        resolved_qn = members["instance"].get("initialize")
                    else:
                        # Builder.foo -> singleton method foo
                        resolved_qn = members["singleton"].get(tgt)

                    if resolved_qn is not None:
                        extra["ruby_resolved"] = True
                        resolved_writes.append(
                            (resolved_qn, json.dumps(extra), 0.7, "INFERRED", eid)
                        )
                        stats["calls_resolved"] += 1
                        continue

            # No resolution possible — mark unresolved
            extra["unresolved"] = True
            unresolved_writes.append((json.dumps(extra), eid))
            stats["calls_unresolved"] += 1

        # Flush accumulated writes in two bulk statements (one round-trip each
        # instead of one UPDATE per edge). Done before step 4 below, which reads
        # the now-resolved CALLS edges back to propagate TESTED_BY targets.
        if resolved_writes:
            cur.executemany(
                "UPDATE edges SET target_qualified=?, extra=?,"
                " confidence=?, confidence_tier=? WHERE id=?",
                resolved_writes,
            )
        if unresolved_writes:
            cur.executemany(
                "UPDATE edges SET extra=? WHERE id=?",
                unresolved_writes,
            )

    except Exception:
        logger.exception("ruby_resolver: CALLS arm failed")

    # 4) Propagate resolved CALLS targets to TESTED_BY edges.
    #    TESTED_BY edges are generated from CALLS edges (test->prod) with the
    #    same source/target.  When the CALLS target was bare at parse time, the
    #    corresponding TESTED_BY edge also has a bare target.  Now that CALLS
    #    edges have been resolved, build a map from (src, bare_name) ->
    #    qualified_name using the resolved CALLS edges, then update sibling
    #    TESTED_BY edges so that graph consumers (tests_for / get_transitive_tests)
    #    can locate tests by the fully-qualified prod node name.
    try:
        # Map (source_qn, bare_method_name) -> resolved_target_qn from CALLS edges.
        # A CALLS edge source_qn = spec::it adds, target_qn = lib::Calc.add
        # The bare method name is the last segment after "::" or ".".
        calls_map: dict[tuple[str, str], str] = {}
        for row in cur.execute(
            "SELECT source_qualified, target_qualified FROM edges WHERE kind='CALLS'"
        ).fetchall():
            src_qn = row["source_qualified"]
            tgt_qn = row["target_qualified"]
            if "::" not in tgt_qn:
                continue  # still unresolved — skip
            # Extract the bare method name from the resolved target
            bare = tgt_qn.rsplit("::", 1)[-1]  # e.g. "Calc.add" or "add"
            # Also index the last "."-segment for method names under a class
            method = bare.rsplit(".", 1)[-1]    # e.g. "add"
            for key_name in {bare, method}:
                key = (src_qn, key_name)
                if key not in calls_map:
                    calls_map[key] = tgt_qn

        # Apply resolutions to TESTED_BY edges with bare targets.
        tb_updated = 0
        for row in cur.execute(
            "SELECT id, source_qualified, target_qualified, extra FROM edges"
            " WHERE kind='TESTED_BY'"
        ).fetchall():
            tgt = row["target_qualified"]
            if "::" in tgt:
                continue  # already qualified
            src = row["source_qualified"]
            resolved = calls_map.get((src, tgt))
            if resolved:
                extra = json.loads(row["extra"] or "{}")
                extra["ruby_resolved"] = True
                cur.execute(
                    "UPDATE edges SET target_qualified=?, extra=? WHERE id=?",
                    (resolved, json.dumps(extra), row["id"]),
                )
                tb_updated += 1
        stats["tested_by_resolved"] = tb_updated
    except Exception:
        logger.exception("ruby_resolver: TESTED_BY propagation failed")

    # 4b) Resolve describe-based TESTED_BY edges (spec -> described constant).
    #     `RSpec.describe SomeClass` emits a TESTED_BY edge to the bare constant
    #     (extra.tested_via == "describe"); resolve it to the prod class node
    #     via the const index. Drop edges whose constant is not a repo class
    #     (e.g. a described gem class) so no dangling TESTED_BY remains.
    try:
        full_path_to_qn, bare_name_to_qns = _build_di_const_indexes(conn)
        describe_resolved = 0
        describe_dropped = 0
        for row in cur.execute(
            "SELECT id, target_qualified, extra FROM edges"
            " WHERE kind='TESTED_BY'"
        ).fetchall():
            extra = json.loads(row["extra"] or "{}")
            if extra.get("tested_via") != "describe" or extra.get("ruby_resolved"):
                continue
            resolved = _resolve_const_to_node(
                row["target_qualified"], full_path_to_qn, bare_name_to_qns
            )
            if resolved:
                extra["ruby_resolved"] = True
                cur.execute(
                    "UPDATE edges SET target_qualified=?, extra=? WHERE id=?",
                    (resolved, json.dumps(extra), row["id"]),
                )
                describe_resolved += 1
            else:
                cur.execute("DELETE FROM edges WHERE id=?", (row["id"],))
                describe_dropped += 1
        stats["tested_by_describe_resolved"] = describe_resolved
        stats["tested_by_describe_dropped"] = describe_dropped
    except Exception:
        logger.exception("ruby_resolver: describe TESTED_BY resolution failed")

    # 5) Propagate concern ASSOCIATES edges to includers.
    #    For each class that INCLUDES a concern node, copy the concern's
    #    ASSOCIATES edges onto the includer with extra["inherited_via"] set
    #    to the concern name.  Deduped: skip if an identical (kind, source,
    #    target, file_path) edge already exists.
    try:
        concern_assoc_emitted = 0

        # Build set of concern qualified_names (nodes with mixins containing
        # "ActiveSupport::Concern" or whose name appears as an INCLUDES target
        # where the concern node is known).
        concern_qns: set[str] = set()
        for row in conn.execute(
            "SELECT qualified_name, extra FROM nodes"
            " WHERE language='ruby' AND kind IN ('Class', 'Type')"
        ).fetchall():
            node_extra = json.loads(row["extra"] or "{}")
            mixins: list[str] = node_extra.get("mixins", [])
            if any("Concern" in m for m in mixins):
                concern_qns.add(row["qualified_name"])

        for concern_qn in concern_qns:
            concern_bare = concern_qn.split("::")[-1].split(".")[-1]

            # Find all ASSOCIATES edges sourced from this concern.
            assoc_rows = conn.execute(
                "SELECT target_qualified, file_path, line, extra FROM edges"
                " WHERE kind='ASSOCIATES' AND source_qualified=?",
                (concern_qn,),
            ).fetchall()
            if not assoc_rows:
                continue

            # Find all classes that INCLUDE this concern (by bare name or qn).
            includer_rows = conn.execute(
                "SELECT source_qualified FROM edges"
                " WHERE kind='INCLUDES'"
                "   AND (target_qualified=? OR target_qualified=?)",
                (concern_qn, concern_bare),
            ).fetchall()

            for inc_row in includer_rows:
                includer_qn: str = inc_row["source_qualified"]
                includer_fp_row = conn.execute(
                    "SELECT file_path FROM nodes WHERE qualified_name=?",
                    (includer_qn,),
                ).fetchone()
                includer_fp = includer_fp_row["file_path"] if includer_fp_row else ""

                for a_row in assoc_rows:
                    concern_tgt: str = a_row["target_qualified"]
                    a_extra = json.loads(a_row["extra"] or "{}")

                    # Skip if already exists (dedup).
                    existing = conn.execute(
                        "SELECT id FROM edges"
                        " WHERE kind='ASSOCIATES' AND source_qualified=?"
                        "   AND target_qualified=?",
                        (includer_qn, concern_tgt),
                    ).fetchone()
                    if existing is not None:
                        continue

                    derived_extra = dict(a_extra)
                    derived_extra["inherited_via"] = concern_bare
                    derived_extra["confidence_tier"] = "INFERRED"
                    cur.execute(
                        "INSERT INTO edges"
                        " (kind, source_qualified, target_qualified, file_path, line,"
                        "  confidence, confidence_tier, extra, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "ASSOCIATES",
                            includer_qn,
                            concern_tgt,
                            includer_fp,
                            a_row["line"],
                            0.7,
                            "INFERRED",
                            json.dumps(derived_extra),
                            time.time(),
                        ),
                    )
                    concern_assoc_emitted += 1

        stats["concern_assoc_emitted"] = concern_assoc_emitted
    except Exception:
        logger.exception("ruby_resolver: concern attribution failed")

    store.commit()
    store._invalidate_cache()
    logger.info("ruby_resolver: %s", stats)
    return stats
