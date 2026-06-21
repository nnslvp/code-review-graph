"""Tests for SimpleCov coverage ingestion (coverage_ingest.py)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from code_review_graph.coverage_ingest import _compress_to_ranges, ingest_coverage
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal fake Ruby repo structure."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code-review-graph").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "coverage").mkdir()
    return tmp_path


def _make_store(tmp_path: Path) -> GraphStore:
    return GraphStore(str(tmp_path / ".code-review-graph" / "graph.db"))


# ---------------------------------------------------------------------------
# Unit tests for _compress_to_ranges helper
# ---------------------------------------------------------------------------


class TestCompressRanges:
    def test_empty(self):
        assert _compress_to_ranges([]) == []

    def test_single(self):
        assert _compress_to_ranges([5]) == [[5, 5]]

    def test_contiguous(self):
        assert _compress_to_ranges([3, 4, 5]) == [[3, 5]]

    def test_gap(self):
        assert _compress_to_ranges([3, 4, 5, 8]) == [[3, 5], [8, 8]]

    def test_multiple_gaps(self):
        assert _compress_to_ranges([1, 3, 5, 6]) == [[1, 1], [3, 3], [5, 6]]


# ---------------------------------------------------------------------------
# absent / absent_no_match cases
# ---------------------------------------------------------------------------


class TestAbsentCases:
    def test_absent_when_no_resultset(self, tmp_path):
        _make_repo(tmp_path)
        store = _make_store(tmp_path)
        result = ingest_coverage(store, str(tmp_path))
        assert result["status"] == "absent"
        assert result["files_matched"] == 0

    def test_absent_no_match_when_paths_dont_resolve(self, tmp_path):
        """Resultset with paths that match nothing in graph → absent_no_match."""
        _make_repo(tmp_path)
        src = tmp_path / "lib" / "calc.rb"
        src.write_text("class Calc\n  def add(a, b)\n    a + b\n  end\nend\n")
        store = _make_store(tmp_path)
        full_build(tmp_path, store)

        # Write resultset with WRONG absolute paths (point at /nonexistent/...)
        rs = {
            "RSpec": {
                "coverage": {
                    "/nonexistent/completely/wrong/path.rb": {
                        "lines": [None, 1, 5, None, None],
                    },
                },
                "timestamp": 9999999999,
            }
        }
        (tmp_path / "coverage" / ".resultset.json").write_text(json.dumps(rs))

        result = ingest_coverage(store, str(tmp_path))
        assert result["status"] == "absent_no_match"
        assert result["files_matched"] == 0

        # Crucially: no node should have line_coverage set
        for node in store.get_nodes_by_kind(["Function"]):
            assert "line_coverage" not in (node.extra or {})

    def test_absent_when_resultset_is_malformed(self, tmp_path):
        _make_repo(tmp_path)
        (tmp_path / "coverage" / ".resultset.json").write_text("not json {{{{")
        store = _make_store(tmp_path)
        result = ingest_coverage(store, str(tmp_path))
        assert result["status"] == "absent"


# ---------------------------------------------------------------------------
# Core normalization + ingestion test (mirrors task brief)
# ---------------------------------------------------------------------------


def test_simplecov_ingest_maps_and_normalizes(tmp_path):
    """Absolute resultset paths are normalized to match graph nodes."""
    _make_repo(tmp_path)
    src = tmp_path / "lib" / "calc.rb"
    # line 1: class Calc  (None - non-executable)
    # line 2: def add     (None - non-executable in SimpleCov)
    # line 3: a + b       (hit: 5 times)
    # line 4: end         (None)
    # line 5: end         (None)
    src.write_text("class Calc\n  def add(a, b)\n    a + b\n  end\nend\n")

    rs = {
        "RSpec": {
            "coverage": {
                str(src): {
                    "lines": [None, 1, 5, None, None],
                },
            },
            "timestamp": 9999999999,
        }
    }
    (tmp_path / "coverage" / ".resultset.json").write_text(json.dumps(rs))

    store = _make_store(tmp_path)
    full_build(tmp_path, store)

    stats = ingest_coverage(store, repo_root=str(tmp_path))

    assert stats["files_matched"] >= 1, (
        "Absolute→relative normalization failed: 0 paths matched graph nodes"
    )
    assert stats["nodes_updated"] >= 1

    func_nodes = store.get_nodes_by_kind(["Function"])
    add_nodes = [n for n in func_nodes if n.name == "add"]
    assert add_nodes, "Expected 'add' Function node in graph"
    add_node = add_nodes[0]

    extra = add_node.extra or {}
    assert "line_coverage" in extra, f"line_coverage missing from extra: {extra}"
    assert 0.0 <= extra["line_coverage"] <= 1.0
    assert "coverage_freshness" in extra


def test_line_coverage_computation_correctness(tmp_path):
    """Verify 1-indexed line mapping and covered/executable counting."""
    _make_repo(tmp_path)
    src = tmp_path / "lib" / "math.rb"
    # 5 lines; array indices 0-4 map to lines 1-5
    src.write_text("class Math\n  def sub(a, b)\n    a - b\n  end\nend\n")

    # lines array: [None, None, 0, None, None]
    # line 3 (index 2) is missed (0), lines 1,2,4,5 non-executable (None)
    # Function 'sub' spans lines 2-4; executable lines within: line 3 (index 2) = missed
    # → line_coverage = 0/1 = 0.0
    rs = {
        "RSpec": {
            "coverage": {
                str(src): {
                    "lines": [None, None, 0, None, None],
                },
            },
            "timestamp": 9999999999,
        }
    }
    (tmp_path / "coverage" / ".resultset.json").write_text(json.dumps(rs))

    store = _make_store(tmp_path)
    full_build(tmp_path, store)
    ingest_coverage(store, repo_root=str(tmp_path))

    func_nodes = [n for n in store.get_nodes_by_kind(["Function"]) if n.name == "sub"]
    assert func_nodes
    extra = func_nodes[0].extra or {}
    assert extra.get("line_coverage") == 0.0, (
        f"Expected 0% coverage for fully-missed function, got {extra.get('line_coverage')}"
    )
    assert extra.get("missed_lines") == [[3, 3]], (
        f"Expected missed_lines=[[3,3]], got {extra.get('missed_lines')}"
    )


def test_fully_covered_function(tmp_path):
    """All executable lines hit → line_coverage == 1.0."""
    _make_repo(tmp_path)
    src = tmp_path / "lib" / "service.rb"
    src.write_text("class Svc\n  def run\n    42\n  end\nend\n")

    # line 3 (index 2) hit 3 times
    rs = {
        "RSpec": {
            "coverage": {
                str(src): {
                    "lines": [None, None, 3, None, None],
                },
            },
            "timestamp": 9999999999,
        }
    }
    (tmp_path / "coverage" / ".resultset.json").write_text(json.dumps(rs))

    store = _make_store(tmp_path)
    full_build(tmp_path, store)
    ingest_coverage(store, repo_root=str(tmp_path))

    run_nodes = [n for n in store.get_nodes_by_kind(["Function"]) if n.name == "run"]
    assert run_nodes
    extra = run_nodes[0].extra or {}
    assert extra.get("line_coverage") == 1.0
    assert extra.get("missed_lines") == []


def test_staleness_detection(tmp_path):
    """Resultset older than source file → freshness = stale."""
    _make_repo(tmp_path)
    src = tmp_path / "lib" / "old.rb"
    src.write_text("class Old\n  def thing\n    1\n  end\nend\n")

    rs = {
        "RSpec": {
            "coverage": {
                str(src): {
                    "lines": [None, None, 1, None, None],
                },
            },
            "timestamp": 1000,  # very old
        }
    }
    (tmp_path / "coverage" / ".resultset.json").write_text(json.dumps(rs))

    # Set the resultset mtime to the past
    resultset_path = tmp_path / "coverage" / ".resultset.json"
    os.utime(resultset_path, (1000.0, 1000.0))

    store = _make_store(tmp_path)
    full_build(tmp_path, store)
    ingest_coverage(store, repo_root=str(tmp_path))

    thing_nodes = [n for n in store.get_nodes_by_kind(["Function"]) if n.name == "thing"]
    assert thing_nodes
    extra = thing_nodes[0].extra or {}
    assert extra.get("coverage_freshness") == "stale", (
        f"Expected stale, got {extra.get('coverage_freshness')}"
    )


def test_fresh_coverage_has_timestamp(tmp_path):
    """Ingested nodes carry coverage_timestamp from the resultset."""
    _make_repo(tmp_path)
    src = tmp_path / "lib" / "ts.rb"
    src.write_text("class Ts\n  def go\n    0\n  end\nend\n")

    ts = int(time.time()) + 3600  # future → fresh
    rs = {
        "RSpec": {
            "coverage": {
                str(src): {
                    "lines": [None, None, 2, None, None],
                },
            },
            "timestamp": ts,
        }
    }
    (tmp_path / "coverage" / ".resultset.json").write_text(json.dumps(rs))

    store = _make_store(tmp_path)
    full_build(tmp_path, store)
    ingest_coverage(store, repo_root=str(tmp_path))

    go_nodes = [n for n in store.get_nodes_by_kind(["Function"]) if n.name == "go"]
    assert go_nodes
    extra = go_nodes[0].extra or {}
    assert extra.get("coverage_timestamp") == ts


def test_idempotent_ingest(tmp_path):  # noqa: F811
    """Calling ingest_coverage twice does not corrupt node extra."""
    _make_repo(tmp_path)
    src = tmp_path / "lib" / "idem.rb"
    src.write_text("class Idem\n  def check\n    true\n  end\nend\n")

    ts = int(time.time()) + 3600
    rs = {
        "RSpec": {
            "coverage": {
                str(src): {
                    "lines": [None, None, 4, None, None],
                },
            },
            "timestamp": ts,
        }
    }
    (tmp_path / "coverage" / ".resultset.json").write_text(json.dumps(rs))

    store = _make_store(tmp_path)
    full_build(tmp_path, store)
    ingest_coverage(store, repo_root=str(tmp_path))
    ingest_coverage(store, repo_root=str(tmp_path))  # second call

    check_nodes = [n for n in store.get_nodes_by_kind(["Function"]) if n.name == "check"]
    assert check_nodes
    extra = check_nodes[0].extra or {}
    assert extra.get("line_coverage") == 1.0


def test_full_build_wires_coverage_ingest(tmp_path):  # noqa: F811
    """full_build calls ingest_coverage when .resultset.json exists."""
    _make_repo(tmp_path)
    src = tmp_path / "lib" / "wired.rb"
    src.write_text("class Wired\n  def go\n    1\n  end\nend\n")

    ts = int(time.time()) + 3600
    rs = {
        "RSpec": {
            "coverage": {
                str(src): {
                    "lines": [None, None, 1, None, None],
                },
            },
            "timestamp": ts,
        }
    }
    (tmp_path / "coverage" / ".resultset.json").write_text(json.dumps(rs))

    store = _make_store(tmp_path)
    result = full_build(tmp_path, store)

    assert "coverage_ingest" in result
    ci = result["coverage_ingest"]
    assert ci is not None
    assert ci["files_matched"] >= 1
