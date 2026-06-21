"""Post-build cross-module resolver for Ruby/Rails.

Resolves: require_relative raw strings -> real .rb file paths; Zeitwerk
CamelCase constants (INHERITS / INCLUDES / EXTENDS / PREPENDS / ASSOCIATES
targets) -> <file>::Const node qualified names. Idempotent via the
extra.ruby_resolved flag. No-op when the graph has no .rb files.
"""

from __future__ import annotations

import json
import logging
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


def _update_edge_extracted(cur, target_qualified: str, extra: dict, edge_id: int) -> None:
    """Write a resolved target + EXTRACTED confidence back to an edge row."""
    extra["ruby_resolved"] = True
    cur.execute(
        "UPDATE edges SET target_qualified=?, extra=?,"
        " confidence=?, confidence_tier=? WHERE id=?",
        (target_qualified, json.dumps(extra), 1.0, "EXTRACTED", edge_id),
    )


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
        # Build member index: {class_name -> {"instance": {mname: qn}, "singleton": {mname: qn}}}
        # Index by bare class name (last segment after ::) for fast lookup.
        # On bare-name collisions (two classes with same name), the first class found wins.
        member_index: dict[str, dict[str, dict[str, str]]] = {}

        # Collect member Function nodes via CONTAINS edges from Class nodes.
        # This covers both singleton (ruby_singleton=True) and instance methods,
        # even those without ruby_owner_qn (e.g. plain instance methods).
        for class_row in conn.execute(
            "SELECT qualified_name, name FROM nodes"
            " WHERE language='ruby' AND kind IN ('Class', 'Type')"
        ).fetchall():
            class_qn = class_row["qualified_name"]
            class_bare = class_row["name"]
            bucket = member_index.setdefault(class_bare, {"instance": {}, "singleton": {}})
            for member_row in conn.execute(
                "SELECT n.qualified_name, n.name, n.extra"
                " FROM edges e JOIN nodes n ON n.qualified_name = e.target_qualified"
                " WHERE e.kind='CONTAINS' AND e.source_qualified=?"
                "   AND n.kind='Function' AND n.language='ruby'",
                (class_qn,),
            ).fetchall():
                extra_m = json.loads(member_row["extra"] or "{}")
                is_singleton = extra_m.get("ruby_singleton", False)
                mname = member_row["name"]
                mqn = member_row["qualified_name"]
                tier = "singleton" if is_singleton else "instance"
                if mname not in bucket[tier]:
                    bucket[tier][mname] = mqn

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

            # Tier-1: no receiver — look for a same-file def with matching name
            if not receiver:
                src_file_row = conn.execute(
                    "SELECT file_path FROM nodes WHERE qualified_name=?", (src,)
                ).fetchone()
                if src_file_row is not None:
                    src_file = src_file_row["file_path"]
                    same_file_matches = conn.execute(
                        "SELECT qualified_name FROM nodes"
                        " WHERE file_path=? AND name=? AND kind='Function' AND language='ruby'",
                        (src_file, tgt),
                    ).fetchall()
                    if len(same_file_matches) == 1:
                        matched_qn = same_file_matches[0]["qualified_name"]
                        _update_edge_extracted(cur, matched_qn, extra, eid)
                        stats["calls_resolved"] += 1
                        continue
                    # Multiple matches: ambiguous — fall through to unresolved

            # Tier-2: constant receiver (starts with uppercase letter)
            if receiver and _is_constant(receiver):
                # receiver may be "Foo" or "Foo::Bar" — use last segment as class name
                class_bare = receiver.rsplit("::", 1)[-1]
                members = member_index.get(class_bare)

                if members is not None:
                    resolved_qn: str | None = None
                    if tgt == "new":
                        # Builder.new -> initialize instance method (if it exists)
                        resolved_qn = members["instance"].get("initialize")
                    else:
                        # Builder.foo -> singleton method foo
                        resolved_qn = members["singleton"].get(tgt)

                    if resolved_qn is not None:
                        _update_edge(cur, resolved_qn, extra, eid)
                        stats["calls_resolved"] += 1
                        continue

            # No resolution possible — mark unresolved
            extra["unresolved"] = True
            cur.execute(
                "UPDATE edges SET extra=? WHERE id=?",
                (json.dumps(extra), eid),
            )
            stats["calls_unresolved"] += 1

    except Exception:
        logger.exception("ruby_resolver: CALLS arm failed")

    store.commit()
    store._invalidate_cache()
    logger.info("ruby_resolver: %s", stats)
    return stats
