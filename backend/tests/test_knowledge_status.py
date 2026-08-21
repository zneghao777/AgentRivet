"""KBX 聚合状态优先级测试。"""

import pytest

from app.knowledge.status import aggregate_graph_status, aggregate_index_status


@pytest.mark.parametrize(
    ("mode", "statuses", "expected"),
    [
        ("standard", ["ready"], "disabled"),
        ("graph_enhanced", [], "none"),
        ("graph_enhanced", ["excluded"], "none"),
        ("graph_enhanced", ["ready", "building"], "building"),
        ("graph_enhanced", ["ready", "pending"], "pending"),
        ("graph_enhanced", ["ready", "ready"], "ready"),
        ("graph_enhanced", ["ready", "failed"], "partial"),
        ("graph_enhanced", ["failed", "failed"], "failed"),
        ("graph_enhanced", ["stale", "stale"], "stale"),
    ],
)
def test_aggregate_graph_status(mode, statuses, expected):
    assert aggregate_graph_status(mode, statuses) == expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], "empty"),
        (["pending"], "indexing"),
        (["ready", "building"], "indexing"),
        (["ready", "ready"], "ready"),
        (["ready", "failed"], "partial"),
        (["failed"], "failed"),
        (["stale"], "stale"),
    ],
)
def test_aggregate_index_status(statuses, expected):
    assert aggregate_index_status(statuses) == expected
