"""做题工作台 S1：入队 → 领取 → 执行 → 记账。

覆盖的行为（每条都对应一个踩过的坑）：

- 入队只做入队：不调模型、不写业务结果；
- 轮次必须 >= 1（第 0 轮没有意义）；
- 投票类任务必须显式给 models（不许「默认模型」偷偷决定谁投票）；
- 同样的活儿在路上不重复排（dedupe），但显式 dedupe=false 可以再排；
- 没有 handler 的 kind **立刻死信**，不许假装跑过；
- handler 抛异常 → 重试到 max_attempts 转 dead，异常文本落 error；
- `solve_attempts` 只追加；`agent_runs` 能记 role / packet_hash / question_id。
"""
import json

import pytest
from sqlalchemy import select

from app import capabilities
from app.capabilities.agent_workbench import QueueAgentJobInput
from app.capabilities.context import ExecutionContext, Surface
from app.capabilities.errors import Forbidden, Invalid
from app.crud.crud_user import user as crud_user
from app.models.agent import AgentRun
from app.models.agent_job import AgentJob, AgentJobKind, AgentJobStatus
from app.models.question import Question, QuestionType
from app.models.solve_attempt import SolveAttempt
from app.models.subject import Subject
from app.models.subject_member import SubjectMember
from app.models.user import User
from app.services import agent_jobs

_DOC = json.dumps({"type": "doc", "content": [{"type": "paragraph"}]}, ensure_ascii=False)


@pytest.fixture
async def seed(db_session):
    subject = Subject(name="数学", slug="math")
    db_session.add(subject)
    await db_session.commit()
    await db_session.refresh(subject)

    editor = User(username="e", full_name="e", hashed_password="x", is_active=True)
    viewer = User(username="v", full_name="v", hashed_password="x", is_active=True)
    db_session.add_all([editor, viewer])
    await db_session.commit()
    await db_session.refresh(editor)
    await db_session.refresh(viewer)

    db_session.add_all([
        SubjectMember(user_id=editor.id, subject_id=subject.id, role="editor"),
        SubjectMember(user_id=viewer.id, subject_id=subject.id, role="viewer"),
    ])
    await db_session.commit()

    question = Question(
        content=_DOC, q_type=QuestionType.FREE_RESPONSE, subject_id=subject.id,
        created_by=editor.id, visibility="public",
    )
    db_session.add(question)
    await db_session.commit()
    await db_session.refresh(question)

    return {"subject": subject, "editor": editor, "viewer": viewer, "question": question}


async def _ctx(db_session, user, subject_id):
    actor = await crud_user.get_with_memberships(db_session, id=user.id)
    return ExecutionContext(db=db_session, actor=actor, surface=Surface.MCP, subject_id=subject_id)


async def _queue(db_session, seed, **kw):
    ctx = await _ctx(db_session, seed["editor"], seed["subject"].id)
    inp = QueueAgentJobInput(
        kind=kw.pop("kind", AgentJobKind.SOLVE),
        question_id=kw.pop("question_id", seed["question"].id),
        payload=kw.pop("payload", {}),
        **kw,
    )
    return await capabilities.run("agent.queue_job", ctx, inp)


async def _jobs(db_session):
    return (await db_session.execute(select(AgentJob))).scalars().all()


# --------------------------------------------------------------------------- #
# 入队
# --------------------------------------------------------------------------- #
async def test_queue_solve_job_only_enqueues(db_session, seed):
    result = await _queue(db_session, seed, payload={"round": 2})

    assert result["deduped"] is False
    jobs = await _jobs(db_session)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.kind == AgentJobKind.SOLVE.value
    assert job.status == AgentJobStatus.QUEUED.value
    assert job.payload["round"] == 2
    assert job.payload["qid"] == seed["question"].id
    assert job.question_id == seed["question"].id
    assert job.subject_id == seed["subject"].id
    # 入队不写业务结果
    assert job.result is None
    assert (await db_session.execute(select(SolveAttempt))).scalars().all() == []


async def test_round_must_be_positive(db_session, seed):
    with pytest.raises(Invalid):
        await _queue(db_session, seed, payload={"round": 0})
    with pytest.raises(Invalid):
        await _queue(db_session, seed, payload={"round": "第一轮"})


async def test_payload_qid_must_match_question_id(db_session, seed):
    with pytest.raises(Invalid):
        await _queue(db_session, seed, payload={"qid": seed["question"].id + 999})


async def test_review_requires_explicit_models(db_session, seed):
    with pytest.raises(Invalid):
        await _queue(db_session, seed, kind=AgentJobKind.REVIEW, payload={})
    with pytest.raises(Invalid):
        await _queue(db_session, seed, kind=AgentJobKind.REVIEW, payload={"models": []})

    result = await _queue(
        db_session, seed, kind=AgentJobKind.REVIEW, payload={"models": ["kimi-k3-1"]}
    )
    assert result["status"] == AgentJobStatus.QUEUED.value


async def test_solve_job_needs_question(db_session, seed):
    ctx = await _ctx(db_session, seed["editor"], seed["subject"].id)
    with pytest.raises(Invalid):
        await capabilities.run(
            "agent.queue_job",
            ctx,
            QueueAgentJobInput(kind=AgentJobKind.SOLVE, payload={"round": 1}),
        )


async def test_dedupe_reuses_active_job_but_can_be_forced(db_session, seed):
    first = await _queue(db_session, seed, payload={"round": 1})
    second = await _queue(db_session, seed, payload={"round": 1})
    assert second["deduped"] is True
    assert second["id"] == first["id"]
    assert len(await _jobs(db_session)) == 1

    forced = await _queue(db_session, seed, payload={"round": 1}, dedupe=False)
    assert forced["deduped"] is False
    assert len(await _jobs(db_session)) == 2


async def test_viewer_cannot_queue(db_session, seed):
    ctx = await _ctx(db_session, seed["viewer"], seed["subject"].id)
    with pytest.raises(Forbidden):
        await capabilities.run(
            "agent.queue_job",
            ctx,
            QueueAgentJobInput(
                kind=AgentJobKind.SOLVE,
                question_id=seed["question"].id,
                payload={"round": 1},
            ),
        )


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
async def test_missing_handler_goes_dead_not_silently_done(db_session, seed):
    # solve 已经有真处理器了（services/solve_handler.py）；这里临时摘掉，
    # 验的是「没有处理器时必须死信，而不是记成 done」。
    saved = agent_jobs._HANDLERS.pop(AgentJobKind.SOLVE.value, None)
    try:
        await _queue(db_session, seed, payload={"round": 1})
        assert await agent_jobs.run_once(db_session) is True
    finally:
        if saved is not None:
            agent_jobs._HANDLERS[AgentJobKind.SOLVE.value] = saved

    job = (await _jobs(db_session))[0]
    assert job.status == AgentJobStatus.DEAD.value
    assert "handler" in (job.error or "")
    assert job.result is None


async def test_handler_result_is_recorded(db_session, seed):
    seen = {}

    @agent_jobs.register_handler(AgentJobKind.MEASURE)
    async def _measure(db, job):  # noqa: ANN001
        seen["payload"] = job.payload
        return {"thinking_score": 6.4}

    try:
        await _queue(db_session, seed, kind=AgentJobKind.MEASURE, payload={})
        assert await agent_jobs.run_once(db_session) is True
    finally:
        agent_jobs._HANDLERS.pop(AgentJobKind.MEASURE.value, None)

    job = (await _jobs(db_session))[0]
    assert job.status == AgentJobStatus.DONE.value
    assert job.result == {"thinking_score": 6.4}
    assert job.error is None
    assert seen["payload"]["qid"] == seed["question"].id


async def test_handler_failure_retries_then_dies(db_session, seed):
    @agent_jobs.register_handler(AgentJobKind.DECOMPOSE)
    async def _boom(db, job):  # noqa: ANN001
        raise RuntimeError("模型返回不是 JSON")

    try:
        await _queue(
            db_session, seed, kind=AgentJobKind.DECOMPOSE, payload={}, max_attempts=2
        )
        assert await agent_jobs.run_once(db_session) is True
        job = (await _jobs(db_session))[0]
        assert job.status == AgentJobStatus.QUEUED.value  # 还有一次机会
        assert "不是 JSON" in (job.error or "")

        assert await agent_jobs.run_once(db_session) is True
        job = (await _jobs(db_session))[0]
        assert job.status == AgentJobStatus.DEAD.value
        assert job.attempts == 2
    finally:
        agent_jobs._HANDLERS.pop(AgentJobKind.DECOMPOSE.value, None)


async def test_empty_queue_is_not_an_error(db_session, seed):
    assert await agent_jobs.run_once(db_session) is False


# --------------------------------------------------------------------------- #
# 记录表
# --------------------------------------------------------------------------- #
async def test_solve_attempt_is_append_only_and_agent_run_has_role(db_session, seed):
    run = AgentRun(
        user_id=seed["editor"].id,
        subject_id=seed["subject"].id,
        surface="mcp",
        status="running",
        role="solver",
        packet_hash="deadbeef" * 8,
        question_id=seed["question"].id,
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    for round_no in (1, 2):
        db_session.add(
            SolveAttempt(
                question_id=seed["question"].id,
                round=round_no,
                model_name="glm-5.3",
                provider_name="dsh",
                answer_md=f"\\(x={round_no}\\)",
                answer_plain=f"x={round_no}",
                steps=[{"n": 1, "kind": "计算", "line": "…", "units": 2}],
                units_total=2,
                points_total=1,
                raw={"text": "…"},
                agent_run_id=run.id,
            )
        )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(SolveAttempt).order_by(SolveAttempt.round)
        )
    ).scalars().all()
    assert [r.round for r in rows] == [1, 2]  # 两轮各一行，不覆盖
    assert all(r.agent_run_id == run.id for r in rows)

    stored = (await db_session.execute(select(AgentRun))).scalars().first()
    assert stored.role == "solver"
    assert stored.question_id == seed["question"].id
    assert len(stored.packet_hash or "") == 64
