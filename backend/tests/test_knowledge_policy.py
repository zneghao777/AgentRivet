"""KBX 索引策略纯函数测试。"""

import pytest

from app.knowledge.policy import IndexPolicyError, resolve_graph_action


@pytest.mark.parametrize(
    ("enabled", "mode", "policy", "override", "expected"),
    [
        (True, "standard", "manual", "inherit", "excluded"),
        (False, "standard", "manual", "exclude", "excluded"),
        (True, "graph_enhanced", "auto", "inherit", "enqueue"),
        (True, "graph_enhanced", "manual", "inherit", "pending"),
        (True, "graph_enhanced", "auto", "exclude", "excluded"),
        (False, "graph_enhanced", "auto", "include", "pending"),
        (True, "graph_enhanced", "manual", "include", "pending"),
    ],
)
def test_resolve_graph_action_all_product_branches(
    enabled, mode, policy, override, expected
):
    assert resolve_graph_action(
        platform_enabled=enabled,
        index_mode=mode,
        build_policy=policy,
        graph_override=override,
    ) == expected


def test_standard_include_is_explicit_conflict():
    with pytest.raises(IndexPolicyError) as error:
        resolve_graph_action(
            platform_enabled=True,
            index_mode="standard",
            build_policy="manual",
            graph_override="include",
        )
    assert error.value.code == "INDEX_MODE_CONFLICT"


def test_standard_auto_policy_is_invalid():
    with pytest.raises(IndexPolicyError):
        resolve_graph_action(
            platform_enabled=True,
            index_mode="standard",
            build_policy="auto",
            graph_override="inherit",
        )
