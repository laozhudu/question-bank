"""`solve` 处理器：只产原始件、两轮不覆盖、坏回包不许记成答案。

用假 provider 跑，不打真模型——真模型的验证放在 `QB_LIVE_MODEL=1` 的冒烟里。
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.capabilities.errors import Invalid
from app.models.agent import AgentRun
from app.models.agent_job import AgentJob, AgentJobKind, AgentJobStatus
from app.models.question import Question, QuestionType
from app.models.solve_attempt import SolveAttempt, SolveAttemptStatus
from app.models.subject import Subject
from app.models.user import User
from app.services import agent_jobs, solve_handler

_DOC = json.dumps(
    {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": r"已知 \(y=x^2-4x\)，求最小值。"}]}
        ],
    },
    ensure_ascii=False,
)


class _FakeProvider:
    def __init__(self, reply: str):
        self.reply = reply

    async def chat_stream(self, messages, config):
        yield self.reply


def _resolved():
    return SimpleNamespace(
        model=SimpleNamespace(id=None, name="glm-5.3"),  # 测试库里没有 ai_models 行，不设 FK
        provider=SimpleNamespace(name="dsh"),
        config={"api_key": "test", "base_url": "http://127.0.0.1:1/v1"},
        interface_type="openai",
    )


@pytest.fixture
async def seeded(db_session):
    subject = Subject(name="数学", slug="math")
    db_session.add(subject)
    await db_session.commit()
    await db_session.refresh(subject)

    user = User(username="u", full_name="u", hashed_password="x", is_active=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    question = Question(
        content=_DOC, q_type=QuestionType.FREE_RESPONSE, subject_id=subject.id,
        created_by=user.id, visibility="public",
    )
    db_session.add(question)
    await db_session.commit()
    await db_session.refresh(question)

    return {"subject": subject, "user": user, "question": question}


def _job(seeded, round_no=1):
    return AgentJob(
        kind=AgentJobKind.SOLVE.value,
        status=AgentJobStatus.RUNNING.value,
        payload={"qid": seeded["question"].id, "round": round_no},
        question_id=seeded["question"].id,
        subject_id=seeded["subject"].id,
        created_by=seeded["user"].id,
    )


_GOOD = json.dumps(
    {
        "answer": r"\(y_{\min}=-4\)",
        "steps": [
            {"n": 1, "kind": "识别", "line": "配方", "units": 2},
            {"n": 2, "kind": "计算", "line": r"\(y=(x-2)^2-4\)", "units": 3},
            {"n": 3, "kind": "结论", "line": "最小值 -4", "units": 1},
        ],
    },
    ensure_ascii=False,
)


def test_packet_is_blind():
    q = SimpleNamespace(content=_DOC, q_type=QuestionType.FREE_RESPONSE)
    text = solve_handler.packet_text(q)
    assert "求最小值" in text
    assert "free_response" in text
    for leaked in ("答案", "agree", "provenance", "第 1 轮"):
        assert leaked not in text


async def test_solve_writes_attempt_and_run(db_session, seeded, monkeypatch):
    job = _job(seeded)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    monkeypatch.setattr(solve_handler, "resolve_model", lambda *a, **k: _awaited(_resolved()))
    monkeypatch.setattr(solve_handler, "get_ai_provider", lambda it: _FakeProvider(_GOOD))

    result = await solve_handler.handle_solve(db_session, job)

    attempt = (
        await db_session.execute(select(SolveAttempt))
    ).scalars().first()
    assert attempt.round == 1
    assert attempt.status == SolveAttemptStatus.OK.value
    assert attempt.units_total == 6
    assert attempt.points_total == 3  # 三步的 units 都 > 0
    assert attempt.answer_md == r"\(y_{\min}=-4\)"
    assert attempt.model_name == "glm-5.3"
    assert [s["kind"] for s in attempt.steps] == ["识别", "计算", "结论"]

    run = (await db_session.execute(select(AgentRun))).scalars().first()
    assert run.role == "solver"
    assert run.question_id == seeded["question"].id
    assert len(run.packet_hash) == 64
    assert result["attempt_id"] == attempt.id


async def test_second_round_is_a_new_row(db_session, seeded, monkeypatch):
    monkeypatch.setattr(solve_handler, "resolve_model", lambda *a, **k: _awaited(_resolved()))
    monkeypatch.setattr(solve_handler, "get_ai_provider", lambda it: _FakeProvider(_GOOD))

    for round_no in (1, 2):
        job = _job(seeded, round_no)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        await solve_handler.handle_solve(db_session, job)

    rows = (await db_session.execute(select(SolveAttempt).order_by(SolveAttempt.round))).scalars().all()
    assert [r.round for r in rows] == [1, 2]  # 不覆盖


async def test_same_round_twice_is_rejected(db_session, seeded, monkeypatch):
    monkeypatch.setattr(solve_handler, "resolve_model", lambda *a, **k: _awaited(_resolved()))
    monkeypatch.setattr(solve_handler, "get_ai_provider", lambda it: _FakeProvider(_GOOD))

    job = _job(seeded)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    await solve_handler.handle_solve(db_session, job)

    with pytest.raises(Invalid):
        await solve_handler.handle_solve(db_session, job)


async def test_garbage_reply_does_not_become_an_attempt(db_session, seeded, monkeypatch):
    job = _job(seeded)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    monkeypatch.setattr(solve_handler, "resolve_model", lambda *a, **k: _awaited(_resolved()))
    monkeypatch.setattr(
        solve_handler, "get_ai_provider", lambda it: _FakeProvider("我先想想……（没有 JSON）")
    )

    with pytest.raises(Invalid):
        await solve_handler.handle_solve(db_session, job)
    assert (await db_session.execute(select(SolveAttempt))).scalars().all() == []


async def test_no_model_means_clear_error(db_session, seeded, monkeypatch):
    job = _job(seeded)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    monkeypatch.setattr(solve_handler, "resolve_model", lambda *a, **k: _awaited(None))

    with pytest.raises(Invalid) as exc:
        await solve_handler.handle_solve(db_session, job)
    assert "模型" in str(exc.value)


def _awaited(value):
    async def _coro():
        return value

    return _coro()


async def test_handler_is_registered():
    assert AgentJobKind.SOLVE.value in agent_jobs._HANDLERS
