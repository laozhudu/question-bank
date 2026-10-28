"""`solve` 任务的执行体：把一道题交给模型独立解一遍，只留原始件。

三条硬规矩（对应设计文档 docs/development/agent-workbench.md 的底线）：

1. **packet 剥干净**：模型只看到题干 + 题型 + 图形语义说明，看不到库里的答案、
   看不到上一轮谁怎么答的、看不到置信度——否则「独立求解」是假的。
2. **只产原始件**：这里只写 `solve_attempts`，不碰 `questions.answer`。
   答案要不要提升进主库，是另一条流水线（人工/多轮一致 + 判等）的事。
3. **两轮不许覆盖**：`(question_id, round)` 唯一，第二轮写一行新的。

解析失败/模型不可用都抛异常，让队列按 `max_attempts` 重试、超限进死信——
不许「解析不出来就记个空答案」。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capabilities.errors import Invalid
from app.core.api_errors import ErrorCode
from app.models.agent import AgentRun, AgentStep
from app.models.agent_job import AgentJob, AgentJobKind
from app.models.question import Question
from app.models.solve_attempt import SolveAttempt, SolveAttemptStatus
from app.services import agent_jobs
from app.services.ai_provider import get_ai_provider
from app.services.ai_resolver import resolve_model

logger = logging.getLogger(__name__)

STEP_KINDS = ("识别", "转化", "计算", "分类", "检验", "结论")

_SYSTEM = """你是初中数学解题员。给你一道题，请独立解出它，只输出一行 JSON（不要 Markdown 代码块、不要解释）：

{"answer": "最终答案（公式用 \\\\( ... \\\\) 包起来，分式用 \\\\dfrac{}{}）",
 "steps": [{"n": 1, "kind": "识别", "line": "这一步做了什么（含式子）", "units": 1}]}

要求：
- steps 按解题顺序，kind 只能取：识别、转化、计算、分类、检验、结论；
- units 是这一步的思维量（1 很轻、3 中等、5 很重），据实估；
- 有多个小问时，answer 里用 (1)(2) 分别作答；
- 看不清、条件不足就直说，不要编。"""


def _plain(node: Any) -> str:
    """把 RichDoc 结构拍成纯文本（够模型读懂题干即可）。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_plain(x) for x in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text") or "")
        if node.get("type") == "math":
            latex = node.get("latex") or node.get("text") or ""
            return f"\\({latex}\\)" if latex else ""
        parts = [_plain(v) for k, v in node.items() if k in ("content", "title", "caption")]
        text = "\n".join(p for p in parts if p)
        return text
    return str(node)


def packet_text(question: Question) -> str:
    """盲 packet：只有题干与题型。答案/解法/上一轮结果一律不进。"""
    body = _plain(question.content)
    q_type = getattr(question.q_type, "value", question.q_type)
    lines = [f"【题型】{q_type}", "【题干】", body.strip()]
    return "\n".join(line for line in lines if line is not None)


def packet_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_reply(raw: str) -> Dict[str, Any]:
    """模型回包里抠出那一行 JSON（容忍 ``` 包裹与前后废话）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.lstrip("`")
        text = text.removeprefix("json").strip()
    start = text.find("{")
    if start < 0:
        raise Invalid("模型没有给出 JSON", ErrorCode.VALIDATION_FAILED)
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise Invalid(f"模型回包不是合法 JSON：{exc}", ErrorCode.VALIDATION_FAILED) from exc
    if not isinstance(data, dict) or not str(data.get("answer") or "").strip():
        raise Invalid("模型回包缺 answer", ErrorCode.VALIDATION_FAILED)
    return data


def _steps(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, step in enumerate(data.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        line = str(step.get("line") or "").strip()
        if not line:
            continue
        kind = str(step.get("kind") or "计算").strip()
        try:
            units = int(step.get("units") or 1)
        except (TypeError, ValueError):
            units = 1
        out.append(
            {
                "n": i,
                "kind": kind if kind in STEP_KINDS else "计算",
                "line": line,
                "units": max(units, 0),
            }
        )
    if not out:
        raise Invalid("模型回包 steps 为空", ErrorCode.VALIDATION_FAILED)
    return out


async def _collect(provider, messages, config) -> str:
    chunks: List[str] = []
    async for piece in provider.chat_stream(messages, config):
        if isinstance(piece, str):
            chunks.append(piece)
    return "".join(chunks)


@agent_jobs.register_handler(AgentJobKind.SOLVE)
async def handle_solve(db: AsyncSession, job: AgentJob) -> Dict[str, Any]:
    payload = job.payload or {}
    qid = payload.get("qid") or job.question_id
    round_no = payload.get("round")
    if not qid or not isinstance(round_no, int) or round_no < 1:
        raise Invalid("solve 任务缺 qid 或合法 round", ErrorCode.VALIDATION_FAILED)

    question = await db.get(Question, qid)
    if question is None:
        raise Invalid(f"题目 {qid} 不存在", ErrorCode.QUESTION_NOT_FOUND)

    resolved = await resolve_model(
        db, user_id=job.created_by, purpose="text", model_id=payload.get("model_id")
    )
    if resolved is None:
        raise Invalid(
            "没有可用的文本模型：请先在 AI 设置里激活一个模型",
            ErrorCode.AI_CONFIG_MODEL_UNUSABLE,
        )

    packet = packet_text(question)
    provider = get_ai_provider(resolved.interface_type)
    raw = await _collect(
        provider,
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": packet},
        ],
        resolved.config,
    )

    data = _parse_reply(raw)
    steps = _steps(data)
    units_total = sum(s["units"] for s in steps)
    points_total = sum(1 for s in steps if s["units"] > 0)

    run = AgentRun(
        user_id=job.created_by,
        subject_id=job.subject_id,
        surface="worker",
        status="done",
        role="solver",
        packet_hash=packet_hash(packet),
        question_id=qid,
        model_id=resolved.model.id,
        model_name=resolved.model.name,
        provider_name=resolved.provider.name,
    )
    db.add(run)
    await db.flush()

    db.add(
        AgentStep(
            run_id=run.id,
            idx=0,
            type="assistant",
            result={"text": raw[:4000]},
        )
    )

    existing = (
        await db.execute(
            select(SolveAttempt).where(
                SolveAttempt.question_id == qid, SolveAttempt.round == round_no
            )
        )
    ).scalars().first()
    if existing is not None:
        raise Invalid(
            f"第 {round_no} 轮已经记过了（attempt #{existing.id}），换一轮或先删旧记录",
            ErrorCode.VALIDATION_FAILED,
        )

    attempt = SolveAttempt(
        question_id=qid,
        round=round_no,
        model_name=resolved.model.name,
        provider_name=resolved.provider.name,
        answer_md=str(data["answer"]).strip(),
        answer_plain=str(data.get("answer_plain") or "").strip() or None,
        steps=steps,
        units_total=units_total,
        points_total=points_total,
        raw={"text": raw},
        status=SolveAttemptStatus.OK.value,
        agent_run_id=run.id,
    )
    db.add(attempt)
    await db.flush()

    return {
        "attempt_id": attempt.id,
        "round": round_no,
        "units_total": units_total,
        "points_total": points_total,
        "model": resolved.model.name,
        "answer": attempt.answer_md,
    }


def model_choices(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给 AI 工具的描述用：告诉模型这个任务支持哪些 payload 键。"""
    return {
        "round": "正整数，第几轮独立求解（两轮不许覆盖）",
        "model_id": "可选，显式指定模型 id；不填则用当前用户/全局默认文本模型",
    }
