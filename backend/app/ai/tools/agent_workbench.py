"""做题工作台的 AI 工具。

暴露给模型的是「动作用例」，不是裸 SQL：模型说「把这道题排进解题队列，两轮，两个模型投票」，
服务端负责鉴权、去重、排队。真正跑模型的是 worker，模型在这里只拿到 job_id 与状态。

两个工具刻意都**不返回题目内容**：取题走 `get_question`（已有），避免 packet 从两条路漏出去
（盲复核要求「模型看到的」与「记录的 packet_hash」完全一致）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import select

from app import capabilities
from app.ai.contracts import ToolResult, ToolSpec
from app.ai.tools.registry import register
from app.capabilities.agent_workbench import QueueAgentJobInput
from app.capabilities.context import ExecutionContext
from app.models.agent_job import AgentJob, AgentJobKind

logger = logging.getLogger(__name__)

_KINDS = sorted(k.value for k in AgentJobKind)


async def _queue_agent_job(ctx: ExecutionContext, args: Dict[str, Any]) -> ToolResult:
    payload: Dict[str, Any] = dict(args.get("payload") or {})
    # 模型常把 round / models 写在顶层，这里收敛进 payload（不强求它记住嵌套结构）。
    if args.get("round") is not None:
        payload["round"] = args["round"]
    if args.get("models"):
        payload["models"] = args["models"]

    inp = QueueAgentJobInput(
        kind=AgentJobKind(args["kind"]),
        payload=payload,
        question_id=args.get("question_id"),
        subject_id=args.get("subject_id"),
        priority=int(args.get("priority") or 0),
        dedupe=bool(args.get("dedupe", True)),
    )
    result = await capabilities.run("agent.queue_job", ctx, inp)
    verb = "复用已在队列中的" if result.get("deduped") else "已排队"
    return ToolResult(
        content=f"{verb}任务：{inp.kind.value} job_id={result['id']} status={result['status']}",
        data=result,
    )


async def _get_agent_job(ctx: ExecutionContext, args: Dict[str, Any]) -> ToolResult:
    job_id = str(args.get("id") or "").strip()
    if not job_id:
        return ToolResult.text("Error: 需要 id（job_id）")
    job = (
        await ctx.db.execute(select(AgentJob).where(AgentJob.id == job_id))
    ).scalars().first()
    if job is None:
        return ToolResult.text(f"Error: 找不到 job {job_id}")
    return ToolResult(
        content=(
            f"job {job.id} kind={job.kind} status={job.status} "
            f"attempts={job.attempts}/{job.max_attempts}"
            + (f" error={job.error}" if job.error else "")
        ),
        data={
            "id": job.id,
            "kind": job.kind,
            "status": job.status,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "result": job.result,
            "error": job.error,
            "question_id": job.question_id,
        },
    )


register(
    ToolSpec(
        name="queue_agent_job",
        description=(
            "把一件做题工作排进后台队列（解一道题的第 N 轮 / 给一道题投多模型盲票 / 判等 / 拆题 / 产变式 / 量思维量）。"
            "返回 job_id，执行进度用 get_agent_job 查。不要用它直接写答案——答案由 worker 落到 solve_attempts，"
            "多轮一致或投票通过后才提升为题目答案。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": _KINDS,
                    "description": "任务类型",
                },
                "question_id": {
                    "type": "integer",
                    "description": "题目 id（做题类任务必填）",
                },
                "round": {
                    "type": "integer",
                    "description": "第几轮独立作答（solve 用，>=1）",
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "参与投票/判等的模型名（review/judge 必填，且判等不得与投票同家族）",
                },
                "payload": {
                    "type": "object",
                    "description": "其他入参（如 variant 的母题、decompose 的 max_slots）",
                },
                "priority": {"type": "integer", "description": "越大越先跑，默认 0"},
                "dedupe": {
                    "type": "boolean",
                    "description": "同样的活儿已在队列里时复用（默认 true）",
                },
            },
            "required": ["kind"],
        },
        handler=_queue_agent_job,
        capability="agent.queue_job",
        mutating=True,
    )
)

register(
    ToolSpec(
        name="get_agent_job",
        description="查后台任务状态与结果摘要（queued/running/done/failed/dead）。",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "string", "description": "job_id"}},
            "required": ["id"],
        },
        handler=_get_agent_job,
    )
)
