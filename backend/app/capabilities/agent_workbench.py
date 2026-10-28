"""智能体任务队列的能力 —— 唯一写入口。

为什么要有这张队列表（而不是直接在聊天里跑）：解题一轮要 30 秒到 2 分钟，多模型投票
是 3 倍成本，批一章是 38×N 次调用。这些活儿必须能排队、能重试、能限流、能留死信，
否则一次超时就把整批断在半路（题库项目里靠 `nohup` + 数落盘文件续跑，代价很高）。

本能力只做「入队」：不做模型调用，也不写业务结果。执行在 `app/worker.py` 的
`process_agent_job()`，业务结果落各自的表（`solve_attempts` / `review_votes` …）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select

from app import crud
from app.core.api_errors import ErrorCode
from app.core.permissions import Permission
from app.models.agent_job import AgentJob, AgentJobKind, AgentJobStatus
from app.models.question import Question
from app.services.activity_logger import log_activity

from .base import Authz, Capability, Scope
from .context import ExecutionContext
from .errors import Invalid, NotFound
from .registry import register

# 需要「针对某一道题」的 kind。measure/variant 也以题为中心，一并对齐。
QUESTION_BOUND_KINDS = frozenset(
    {
        AgentJobKind.SOLVE,
        AgentJobKind.REVIEW,
        AgentJobKind.JUDGE,
        AgentJobKind.DECOMPOSE,
        AgentJobKind.VARIANT,
        AgentJobKind.MEASURE,
    }
)

# 入队时去重的状态：这两种状态代表「同样的活儿已经在路上」。
_ACTIVE_STATUSES = (AgentJobStatus.QUEUED.value, AgentJobStatus.RUNNING.value)


class QueueAgentJobInput(BaseModel):
    kind: AgentJobKind
    payload: Dict[str, Any] = Field(default_factory=dict)
    subject_id: Optional[int] = None
    question_id: Optional[int] = None
    priority: int = 0
    max_attempts: int = 3
    # 同样的 (kind, question_id, payload) 已在队列里时：True 直接复用旧 job（默认），
    # False 强制再排一个（例如「同一道题再投一次票」）。
    dedupe: bool = True


@register
class QueueAgentJob(Capability[QueueAgentJobInput, Dict[str, Any]]):
    name = "agent.queue_job"
    description = (
        "Queue a background agent job for one question "
        "(kind: solve | review | judge | decompose | variant | measure)."
    )
    input_model = QueueAgentJobInput
    authz = Authz.PERMISSION
    permission = Permission.EDIT_QUESTION
    scope = Scope.SUBJECT
    mutating = True

    async def load(self, ctx: ExecutionContext, inp: QueueAgentJobInput) -> Optional[Question]:
        if inp.question_id is None:
            return None
        question = await crud.question.get(db=ctx.db, id=inp.question_id)
        if question is None:
            raise NotFound("Question not found", ErrorCode.QUESTION_NOT_FOUND)
        return question

    async def execute(
        self,
        ctx: ExecutionContext,
        inp: QueueAgentJobInput,
        target: Optional[Question],
    ) -> Dict[str, Any]:
        payload = dict(inp.payload or {})

        # 1) payload 里的 qid 与 question_id 必须一致（模型很容易两处写得不一样）
        if target is not None:
            p_qid = payload.get("qid") or payload.get("question_id")
            if p_qid is not None and int(p_qid) != target.id:
                raise Invalid(
                    f"payload.qid={p_qid} 与 question_id={target.id} 不一致",
                    ErrorCode.VALIDATION_FAILED,
                )
            payload.setdefault("qid", target.id)
        elif inp.kind in QUESTION_BOUND_KINDS:
            raise Invalid(f"kind={inp.kind.value} 必须给 question_id", ErrorCode.VALIDATION_FAILED)

        # 2) 轮次只能是正整数（第 0 轮 = 没跑过，没有意义）
        if "round" in payload:
            try:
                round_no = int(payload["round"])
            except (TypeError, ValueError):
                raise Invalid("payload.round 必须是整数", ErrorCode.VALIDATION_FAILED) from None
            if round_no < 1:
                raise Invalid("payload.round 必须 >= 1", ErrorCode.VALIDATION_FAILED)
            payload["round"] = round_no

        # 3) 投票类任务必须显式给模型列表：不许「默认模型」偷偷决定谁投票
        if inp.kind in (AgentJobKind.REVIEW, AgentJobKind.JUDGE):
            models = payload.get("models")
            if not isinstance(models, list) or not models:
                raise Invalid(
                    f"kind={inp.kind.value} 需要 payload.models（非空字符串列表）",
                    ErrorCode.VALIDATION_FAILED,
                )
            payload["models"] = [str(m) for m in models]

        # 4) 去重：同样的活儿在路上就不要重复排
        if inp.dedupe:
            stmt = select(AgentJob).where(
                AgentJob.kind == inp.kind.value,
                AgentJob.status.in_(_ACTIVE_STATUSES),
            )
            if inp.question_id is not None:
                stmt = stmt.where(AgentJob.question_id == inp.question_id)
            existing = (await ctx.db.execute(stmt)).scalars().first()
            if existing is not None and (existing.payload or {}) == payload:
                return {"id": existing.id, "status": existing.status, "deduped": True}

        job = AgentJob(
            kind=inp.kind.value,
            status=AgentJobStatus.QUEUED.value,
            payload=payload,
            priority=int(inp.priority),
            max_attempts=max(1, int(inp.max_attempts)),
            question_id=inp.question_id,
            subject_id=target.subject_id if target is not None else inp.subject_id,
            created_by=ctx.actor.id,
        )
        ctx.db.add(job)
        await ctx.db.flush()

        await log_activity(
            ctx.db,
            user_id=ctx.actor.id,
            action="agent.queue_job",
            resource_type="question" if inp.question_id else "agent_job",
            resource_id=inp.question_id or job.id,
            details={"kind": inp.kind.value, "job_id": job.id, "payload": payload},
        )
        await ctx.db.commit()
        return {"id": job.id, "status": job.status, "deduped": False}
