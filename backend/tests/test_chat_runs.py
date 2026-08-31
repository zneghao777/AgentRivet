"""ASYNC_CONVERSATION_RUN_SPEC 的状态机、幂等、事件和 Run API 验证。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.api import router_runs
from app.harness import events
from app.harness.app_runtime import harness_app
from app.harness.run_coordinator import RunCoordinator, run_coordinator
from app.storage import chat_runs
from app.storage.db import AsyncSessionLocal
from app.storage.models import ChatRun, ChatRunEvent, ChatRunOutbox, Message, Session

AUTH = {"X-API-Key": "test-key-1"}


def test_chat_run_api_is_enabled_by_default():
    from app.config import Settings

    assert Settings.model_fields["CHAT_RUN_ENABLED"].default is True


async def _new_session(app_client) -> str:
    response = await app_client.post("/api/v1/sessions", headers=AUTH, json={})
    assert response.status_code == 200
    return response.json()["data"]["session_id"]


@pytest.mark.asyncio
async def test_default_config_accepts_run_api_request(app_client, monkeypatch):
    async def no_start():
        return None

    async def no_enqueue(_run_id):
        return None

    monkeypatch.setattr(run_coordinator, "start", no_start)
    monkeypatch.setattr(run_coordinator, "enqueue", no_enqueue)
    monkeypatch.setattr(router_runs.settings, "CHAT_RUN_ENABLED", True)
    session_id = await _new_session(app_client)
    response = await app_client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
        json={"message": "默认配置可发送", "approval_mode": "auto"},
    )

    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    canceled = await app_client.post(
        f"/api/v1/runs/{run_id}/cancel",
        headers=AUTH,
    )
    assert canceled.status_code == 200
    assert canceled.json()["data"]["status"] == "canceled"


@pytest.fixture
def enabled_chat_runs(monkeypatch):
    monkeypatch.setattr(router_runs.settings, "CHAT_RUN_ENABLED", True)
    monkeypatch.setattr(router_runs.settings, "CHAT_RUN_DURABLE_ENABLED", False)
    monkeypatch.setattr(router_runs.settings, "MULTI_AGENT_ENABLED", False)
    monkeypatch.setattr(router_runs.settings, "CHAT_RUN_MAX_QUEUED_PER_USER", 10_000)

    async def no_start():
        return None

    async def no_enqueue(_run_id):
        return None

    monkeypatch.setattr(run_coordinator, "start", no_start)
    monkeypatch.setattr(run_coordinator, "enqueue", no_enqueue)


@pytest.mark.asyncio
async def test_create_run_is_idempotent_and_blocks_second_active_run(
    app_client,
    enabled_chat_runs,
):
    session_id = await _new_session(app_client)
    headers = {**AUTH, "Idempotency-Key": str(uuid.uuid4())}
    payload = {"message": "异步分析", "approval_mode": "auto"}

    first = await app_client.post(
        f"/api/v1/sessions/{session_id}/runs", headers=headers, json=payload
    )
    duplicate = await app_client.post(
        f"/api/v1/sessions/{session_id}/runs", headers=headers, json=payload
    )
    conflict = await app_client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
        json={"message": "第二条消息"},
    )

    assert first.status_code == 202
    assert first.json()["data"]["status"] == "queued"
    assert duplicate.status_code == 202
    assert duplicate.json()["data"]["run_id"] == first.json()["data"]["run_id"]
    assert conflict.status_code == 409
    assert conflict.json()["code"] == 40901


@pytest.mark.asyncio
async def test_idempotency_key_rejects_different_payload(app_client, enabled_chat_runs):
    session_id = await _new_session(app_client)
    key = str(uuid.uuid4())
    first = await app_client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers={**AUTH, "Idempotency-Key": key},
        json={"message": "原始请求"},
    )
    conflict = await app_client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers={**AUTH, "Idempotency-Key": key},
        json={"message": "不同请求"},
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["code"] == 40903


@pytest.mark.asyncio
async def test_run_is_not_visible_to_another_api_key(app_client, enabled_chat_runs):
    session_id = await _new_session(app_client)
    created = await app_client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
        json={"message": "租户内用户隔离"},
    )
    response = await app_client.get(
        f"/api/v1/runs/{created.json()['data']['run_id']}",
        headers={"X-API-Key": "test-key-2"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == 40410


@pytest.mark.asyncio
async def test_different_sessions_accept_runs_for_same_user(app_client, enabled_chat_runs):
    session_a = await _new_session(app_client)
    session_b = await _new_session(app_client)
    first, second = await asyncio.gather(
        app_client.post(
            f"/api/v1/sessions/{session_a}/runs",
            headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
            json={"message": "A"},
        ),
        app_client.post(
            f"/api/v1/sessions/{session_b}/runs",
            headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
            json={"message": "B"},
        ),
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["run_id"] != second.json()["data"]["run_id"]


@pytest.mark.asyncio
async def test_cancel_queued_run_is_idempotent_and_events_are_replayable(
    app_client,
    enabled_chat_runs,
):
    session_id = await _new_session(app_client)
    created = await app_client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
        json={"message": "等待取消"},
    )
    run_id = created.json()["data"]["run_id"]

    first = await app_client.post(f"/api/v1/runs/{run_id}/cancel", headers=AUTH)
    second = await app_client.post(f"/api/v1/runs/{run_id}/cancel", headers=AUTH)
    replay = await app_client.get(
        f"/api/v1/runs/{run_id}/events?after_sequence=1", headers=AUTH
    )

    assert first.json()["data"]["status"] == "canceled"
    assert second.json()["data"]["status"] == "canceled"
    assert "event: run_snapshot" in replay.text
    assert "event: run_status" in replay.text
    assert "event: run_end" in replay.text
    ids = [int(line[4:]) for line in replay.text.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids)


def test_state_machine_rejects_illegal_transitions():
    chat_runs.validate_transition("queued", "running")
    chat_runs.validate_transition("running", "pending_approval")
    chat_runs.validate_transition("pending_approval", "queued")
    with pytest.raises(chat_runs.ChatRunConflict) as error:
        chat_runs.validate_transition("succeeded", "running")
    assert error.value.code == 40902


@pytest.mark.asyncio
async def test_success_terminal_commit_creates_one_assistant_message(db_session):
    from app.storage import repositories as repo

    session = await repo.create_session(
        db_session,
        thread_id=f"chat-run-test-{uuid.uuid4().hex}",
        tenant_id="chat-run-test",
        user_id="user-a",
    )
    session_id = session.id
    await db_session.rollback()
    async with db_session.begin():
        session = await db_session.get(Session, session_id)
        assert session is not None
        run, _ = await chat_runs.create_run(
            db_session,
            session=session,
            tenant_id="chat-run-test",
            user_id="user-a",
            request_id=f"request-{uuid.uuid4().hex}",
            client_request_id=str(uuid.uuid4()),
            request_payload={"message": "生成最终答案"},
            max_queued_per_user=10,
            write_outbox=False,
        )
    claimed = await chat_runs.claim_run(
        db_session,
        run_id=run.id,
        worker_id="worker-a",
        lease_seconds=60,
        max_active_per_user=3,
        max_active_per_tenant=100,
        max_attempts=3,
    )
    assert claimed is not None
    run_id = run.id
    await chat_runs.finish_succeeded(
        db_session,
        run_id=run_id,
        worker_id="worker-a",
        content="唯一答案",
        trace_id="trace-a",
        token_usage={"total": 1},
        agent_id=None,
        duration_ms=10,
        tool_calls=0,
    )
    await chat_runs.finish_succeeded(
        db_session,
        run_id=run_id,
        worker_id="worker-a",
        content="不应重复写入",
        trace_id="trace-a",
        token_usage={"total": 1},
        agent_id=None,
        duration_ms=10,
        tool_calls=0,
    )

    assistant_count = (
        await db_session.execute(
            select(func.count(Message.id)).where(
                Message.run_id == run_id,
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    run_end_count = (
        await db_session.execute(
            select(func.count(ChatRunEvent.id)).where(
                ChatRunEvent.run_id == run_id,
                ChatRunEvent.event_type == "run_end",
            )
        )
    ).scalar_one()
    stored = await db_session.get(ChatRun, run_id)
    assert assistant_count == 1
    assert run_end_count == 1
    assert stored.status == "succeeded"


async def _create_coordinator_run(*, message: str = "后台运行") -> uuid.UUID:
    from app.storage import repositories as repo

    async with AsyncSessionLocal() as db:
        session = await repo.create_session(
            db,
            thread_id=f"coordinator-{uuid.uuid4().hex}",
            tenant_id="coordinator-test",
            user_id=f"user-{uuid.uuid4().hex}",
        )
        session_id = session.id
        user_id = session.user_id
        await db.rollback()
        async with db.begin():
            session = await db.get(Session, session_id)
            assert session is not None
            run, _ = await chat_runs.create_run(
                db,
                session=session,
                tenant_id="coordinator-test",
                user_id=user_id,
                request_id=f"request-{uuid.uuid4().hex}",
                client_request_id=str(uuid.uuid4()),
                request_payload={
                    "message": message,
                    "model": "fake-model",
                    "skills": [],
                    "knowledge_base_ids": [],
                    "mcp_capability_ids": [],
                    "attachment_ids": [],
                    "approval_mode": "manual",
                    "agent_snapshot": {"memory_enabled": False},
                    "orchestration_agents": [],
                    "degrade_level": 0,
                },
                max_queued_per_user=10,
                write_outbox=False,
            )
        return run.id


@pytest.mark.asyncio
async def test_coordinator_completes_without_any_subscriber(monkeypatch):
    run_id = await _create_coordinator_run()

    class FakeRuntime:
        async def astream(self, *_args, **_kwargs):
            yield events.trace_start(
                trace_id="trace-background",
                request_id="request-background",
                session_id=None,
            )
            yield events.message_delta("msg-1", "后台完成")
            yield events.message_end({"total": 2})
            yield events.run_end(status="success", duration_ms=5, tool_calls=0)

    monkeypatch.setattr(harness_app, "runtime", FakeRuntime())
    coordinator = RunCoordinator()
    await coordinator.execute(run_id)

    async with AsyncSessionLocal() as db:
        stored = await db.get(ChatRun, run_id)
        assistant_count = (
            await db.execute(
                select(func.count(Message.id)).where(
                    Message.run_id == run_id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one()
    assert stored is not None
    assert stored.status == "succeeded"
    assert stored.trace_id == "trace-background"
    assert assistant_count == 1


@pytest.mark.asyncio
async def test_pending_approval_resumes_same_run_id(monkeypatch):
    run_id = await _create_coordinator_run(message="审批工具")

    class FakeRuntime:
        async def astream(self, *_args, **_kwargs):
            yield events.interrupt_requested(
                "需要批准",
                {
                    "type": "tool_approval",
                    "reason": "需要批准",
                    "tool_calls": [],
                },
            )
            yield events.run_end(status="interrupted", duration_ms=1, tool_calls=0)

        async def resume(self, *_args, **_kwargs):
            yield events.trace_start(
                trace_id="trace-resume",
                request_id="request-resume",
                session_id=None,
            )
            yield events.message_delta("msg-resume", "批准后完成")
            yield events.message_end({"total": 1})
            yield events.run_end(status="success", duration_ms=2, tool_calls=0)

    monkeypatch.setattr(harness_app, "runtime", FakeRuntime())
    coordinator = RunCoordinator()
    await coordinator.execute(run_id)
    async with AsyncSessionLocal() as db:
        pending = await db.get(ChatRun, run_id)
        assert pending is not None
        assert pending.status == "pending_approval"
        await chat_runs.queue_resume(
            db,
            run_id=run_id,
            command={"decision": "approve", "input": "批准执行"},
            write_outbox=False,
        )
    await coordinator.execute(run_id)

    async with AsyncSessionLocal() as db:
        completed = await db.get(ChatRun, run_id)
        assistant_count = (
            await db.execute(
                select(func.count(Message.id)).where(
                    Message.run_id == run_id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one()
    assert completed is not None
    assert completed.id == run_id
    assert completed.status == "succeeded"
    assert assistant_count == 1


@pytest.mark.asyncio
async def test_user_capacity_keeps_fourth_run_queued():
    from app.storage import repositories as repo

    tenant_id = f"capacity-{uuid.uuid4().hex}"
    user_id = "same-user"
    run_ids: list[uuid.UUID] = []
    async with AsyncSessionLocal() as db:
        for index in range(4):
            session = await repo.create_session(
                db,
                thread_id=f"capacity-{index}-{uuid.uuid4().hex}",
                tenant_id=tenant_id,
                user_id=user_id,
            )
            session_id = session.id
            await db.rollback()
            async with db.begin():
                session = await db.get(Session, session_id)
                assert session is not None
                run, _ = await chat_runs.create_run(
                    db,
                    session=session,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    request_id=f"request-{uuid.uuid4().hex}",
                    client_request_id=str(uuid.uuid4()),
                    request_payload={"message": f"run-{index}"},
                    max_queued_per_user=10,
                    write_outbox=False,
                )
            run_ids.append(run.id)
        for run_id in run_ids[:3]:
            claimed = await chat_runs.claim_run(
                db,
                run_id=run_id,
                worker_id=f"worker-{run_id}",
                lease_seconds=60,
                max_active_per_user=3,
                max_active_per_tenant=100,
                max_attempts=3,
            )
            assert claimed is not None
        fourth = await chat_runs.claim_run(
            db,
            run_id=run_ids[3],
            worker_id="worker-four",
            lease_seconds=60,
            max_active_per_user=3,
            max_active_per_tenant=100,
            max_attempts=3,
        )
        stored = await db.get(ChatRun, run_ids[3])
    assert fourth is None
    assert stored is not None and stored.status == "queued"


@pytest.mark.asyncio
async def test_expired_worker_lease_requeues_with_outbox():
    run_id = await _create_coordinator_run(message="恢复租约")
    async with AsyncSessionLocal() as db:
        claimed = await chat_runs.claim_run(
            db,
            run_id=run_id,
            worker_id="crashed-worker",
            lease_seconds=60,
            max_active_per_user=3,
            max_active_per_tenant=100,
            max_attempts=3,
        )
        assert claimed is not None
        await db.execute(
            update(ChatRun)
            .where(ChatRun.id == run_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
        requeued, failed = await chat_runs.recover_expired_leases(
            db,
            max_attempts=3,
            write_outbox=True,
        )
        stored = await db.get(ChatRun, run_id)
        outbox_count = (
            await db.execute(
                select(func.count(ChatRunOutbox.id)).where(
                    ChatRunOutbox.run_id == run_id,
                    ChatRunOutbox.published_at.is_(None),
                )
            )
        ).scalar_one()
    # 恢复扫描是全库任务；共享开发测试库可能还包含其他过期租约。
    assert requeued >= 1
    assert failed == 0
    assert stored is not None and stored.status == "queued"
    assert outbox_count == 1
