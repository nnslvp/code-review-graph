"""Best-effort SimpleCov line-coverage ingestion for Ruby/Rails graphs.

Reads ``coverage/.resultset.json`` (SimpleCov 0.18+ format), normalises
absolute file paths to repo-relative graph ``file_path`` keys, computes
per-Function/Type ``line_coverage`` and ``missed_lines``, and stamps
``coverage_freshness`` so consumers can prefer real coverage over the
static TESTED_BY signal when the data is fresh.

This module is intentionally best-effort: it never raises an exception
out of the caller and is idempotent (safe to call repeatedly).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_coverage(store: "GraphStore", repo_root: str) -> dict[str, Any]:
    """Ingest SimpleCov coverage data into the graph (best-effort).

    Args:
        store: The graph store to update.
        repo_root: Absolute path to the repository root (used for path
            normalisation and file mtime comparisons).

    Returns:
        A stats dict with at least:
          - ``status``: one of ``"ok"``, ``"absent"``, ``"absent_no_match"``
          - ``files_matched``: number of resultset file paths that matched a
            graph node's ``file_path``
          - ``nodes_updated``: number of nodes whose ``extra`` was written
    """
    root = Path(repo_root)
    resultset_path = root / "coverage" / ".resultset.json"

    if not resultset_path.exists():
        return {"status": "absent", "files_matched": 0, "nodes_updated": 0}

    try:
        raw = resultset_path.read_text(encoding="utf-8")
        resultset: dict = json.loads(raw)
    except Exception as exc:
        logger.warning("coverage_ingest: failed to parse .resultset.json: %s", exc)
        return {"status": "absent", "files_matched": 0, "nodes_updated": 0}

    # -----------------------------------------------------------------------
    # Extract per-file coverage arrays from SimpleCov 0.18+ structure:
    #   { "<suite_name>": { "coverage": { "<abs_path>": { "lines": [...] } },
    #                       "timestamp": <unix_int> } }
    # -----------------------------------------------------------------------
    suite_timestamp: int | None = None
    raw_file_coverage: dict[str, list[int | None]] = {}

    for suite_data in resultset.values():
        if not isinstance(suite_data, dict):
            continue
        ts = suite_data.get("timestamp")
        if isinstance(ts, (int, float)) and suite_timestamp is None:
            suite_timestamp = int(ts)
        coverage_block = suite_data.get("coverage", {})
        for abs_path, file_data in coverage_block.items():
            if not isinstance(file_data, dict):
                continue
            lines = file_data.get("lines")
            if isinstance(lines, list):
                raw_file_coverage[abs_path] = lines

    if not raw_file_coverage:
        logger.debug("coverage_ingest: no file coverage found in resultset")
        return {"status": "absent", "files_matched": 0, "nodes_updated": 0}

    # -----------------------------------------------------------------------
    # Normalise absolute paths → repo-relative (strip repo_root prefix).
    # The graph stores absolute file_path values, so we build two maps:
    #   norm_coverage: relative_path  -> lines_array
    #   abs_coverage:  absolute_path  -> lines_array  (for graph lookups)
    # Then we resolve which graph file_paths match.
    # -----------------------------------------------------------------------
    # Map: absolute_graph_path -> lines_array
    abs_coverage: dict[str, list[int | None]] = {}
    for abs_path, lines in raw_file_coverage.items():
        # Ensure canonical form
        canonical = str(Path(abs_path))
        abs_coverage[canonical] = lines

    # -----------------------------------------------------------------------
    # Assert ≥1 normalised path matches a graph File/Function node file_path
    # -----------------------------------------------------------------------
    # Collect the set of file_paths stored in the graph
    graph_file_paths: set[str] = set()
    try:
        conn = store._conn
        rows = conn.execute("SELECT DISTINCT file_path FROM nodes").fetchall()
        graph_file_paths = {r[0] for r in rows}
    except Exception as exc:
        logger.warning("coverage_ingest: could not query graph file_paths: %s", exc)
        return {"status": "absent", "files_matched": 0, "nodes_updated": 0}

    # Intersect: which absolute resultset paths appear in the graph?
    matched_paths: set[str] = abs_coverage.keys() & graph_file_paths
    if not matched_paths:
        logger.debug(
            "coverage_ingest: 0 resultset paths matched graph nodes — treating as absent"
        )
        return {"status": "absent_no_match", "files_matched": 0, "nodes_updated": 0}

    files_matched = len(matched_paths)
    logger.debug("coverage_ingest: %d file(s) matched graph nodes", files_matched)

    # -----------------------------------------------------------------------
    # Compute per-node line_coverage + missed_lines for Function/Type nodes.
    # The lines array is 1-indexed (index 0 = line 1):
    #   None  → non-executable (comment, blank, def keyword)
    #   0     → missed (executable, not hit)
    #   N > 0 → hit N times
    # -----------------------------------------------------------------------
    nodes_updated = 0

    try:
        node_rows = conn.execute(
            "SELECT id, file_path, line_start, line_end, extra FROM nodes "
            "WHERE kind IN ('Function', 'Type') "
            "AND file_path IN (%s)"
            % ",".join("?" * len(matched_paths)),
            list(matched_paths),
        ).fetchall()
    except Exception as exc:
        logger.warning("coverage_ingest: node query failed: %s", exc)
        return {
            "status": "ok",
            "files_matched": files_matched,
            "nodes_updated": 0,
        }

    resultset_mtime: float | None = None
    try:
        resultset_mtime = resultset_path.stat().st_mtime
    except OSError:
        pass

    updates: list[tuple[str, int]] = []

    for row in node_rows:
        node_id: int = row[0]
        file_path: str = row[1]
        line_start: int | None = row[2]
        line_end: int | None = row[3]
        extra_raw: str = row[4] or "{}"

        lines_array = abs_coverage.get(file_path)
        if lines_array is None or line_start is None or line_end is None:
            continue

        # Compute staleness: compare resultset mtime to source file mtime.
        freshness = "unknown"
        try:
            src_mtime = Path(file_path).stat().st_mtime
            if resultset_mtime is not None:
                if resultset_mtime >= src_mtime:
                    freshness = "fresh"
                else:
                    freshness = "stale"
        except OSError:
            pass

        # Slice the relevant lines (1-indexed array, Python 0-indexed slice).
        # line_start=2, line_end=4 → indices 1,2,3 → array[1:4]
        start_idx = max(line_start - 1, 0)
        end_idx = min(line_end, len(lines_array))  # exclusive upper bound

        executable_count = 0
        covered_count = 0
        missed_line_numbers: list[int] = []

        for array_idx in range(start_idx, end_idx):
            val = lines_array[array_idx]
            if val is None:
                continue  # non-executable
            executable_count += 1
            line_num = array_idx + 1  # convert back to 1-indexed
            if val > 0:
                covered_count += 1
            else:
                missed_line_numbers.append(line_num)

        if executable_count == 0:
            line_coverage = 1.0
        else:
            line_coverage = covered_count / executable_count

        missed_ranges = _compress_to_ranges(missed_line_numbers)

        try:
            extra_dict: dict = json.loads(extra_raw)
        except (json.JSONDecodeError, ValueError):
            extra_dict = {}

        extra_dict["line_coverage"] = round(line_coverage, 4)
        extra_dict["missed_lines"] = missed_ranges
        extra_dict["coverage_freshness"] = freshness
        if suite_timestamp is not None:
            extra_dict["coverage_timestamp"] = suite_timestamp

        updates.append((json.dumps(extra_dict), node_id))

    if updates:
        try:
            conn.executemany(
                "UPDATE nodes SET extra = ? WHERE id = ?",
                updates,
            )
            conn.commit()
            nodes_updated = len(updates)
            logger.debug("coverage_ingest: updated %d node(s)", nodes_updated)
        except Exception as exc:
            logger.warning("coverage_ingest: DB write failed: %s", exc)

    return {
        "status": "ok",
        "files_matched": files_matched,
        "nodes_updated": nodes_updated,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compress_to_ranges(line_numbers: list[int]) -> list[list[int]]:
    """Compress a sorted list of line numbers into [start, end] ranges.

    Example: [3, 4, 5, 8] → [[3, 5], [8, 8]]
    """
    if not line_numbers:
        return []
    ranges: list[list[int]] = []
    start = line_numbers[0]
    end = line_numbers[0]
    for n in line_numbers[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append([start, end])
            start = n
            end = n
    ranges.append([start, end])
    return ranges
