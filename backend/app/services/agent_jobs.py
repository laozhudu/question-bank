"""智能体任务的领取与执行。

分工：`capabilities/agent_workbench.py` 只负责入队；本模块负责「领出来、跑掉、记账」。

三条硬规矩：

1. **失败不吞**：异常一律写 `error`，`attempts+1`；到 `max_attempts` 转 `dead`（死信），
   绝不无限重试。死信是给人看的，不是给代码重试的。
2. **一次只领一件**：MySQL 用 `SKIP LOCKED`；SQLite 单写者场景不需要行锁。
3. **handler 未实现 = 立刻死信**，不要伪装成「跑过了」。宁可红灯，不要假绿。

handler 由 `register_handler(kind)` 注册，签名 `async def (db, job) -> dict`，返回结果摘要
（写 `job.result`）。业务结果本体请落各自的表（`solve_attempts` / `review_votes` …）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_job import AgentJob, AgentJobKind, AgentJobStatus

logger = logging.getLogger(__name__)

Handler = Callable[[AsyncSession, AgentJob], Awaitable[Optional[Dict[str, Any]]]]
_HANDLERS: Dict[str, Handler] = {}


def register_handler(kind: AgentJobKind):
    def deco(fn: Handler) -> Handler:
        if kind.value in _HANDLERS:
            raise RuntimeError(f"Duplicate agent job handler: {kind.value}")
        _HANDLERS[kind.value] = fn
        return fn

    return deco


def handlers() -> Dict[str, Handler]:
    return dict(_HANDLERS)


async def claim_next(db: AsyncSession, *, use_row_lock: bool = False) -> Optional[AgentJob]:
    stmt = (
        select(AgentJob)
        .where(AgentJob.status == AgentJobStatus.QUEUED.value)
        .order_by(AgentJob.priority.desc(), AgentJob.created_at)
        .limit(1)
    )
    if use_row_lock:
        stmt = stmt.with_for_update(skip_locked=True)
    job = (await db.execute(stmt)).scalars().first()
    if job is None:
        return None
    job.status = AgentJobStatus.RUNNING.value
    job.attempts = (job.attempts or 0) + 1
    job.started_at = datetime.now(timezone.utc)
    await db.commit()
    return job


async def _finish(
    db: AsyncSession,
    job: AgentJob,
    *,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    retryable: bool = True,
) -> None:
    job.finished_at = datetime.now(timezone.utc)
    if error is None:
        job.status = AgentJobStatus.DONE.value
        job.result = result or {}
        job.error = None
    else:
        job.error = error[:4000]
        if retryable and (job.attempts or 0) < (job.max_attempts or 1):
            job.status = AgentJobStatus.QUEUED.value
            job.started_at = None
        else:
            job.status = AgentJobStatus.DEAD.value
    await db.commit()


async def execute(db: AsyncSession, job: AgentJob) -> None:
    handler = _HANDLERS.get(job.kind)
    if handler is None:
        await _finish(
            db,
            job,
            error=f"没有为 kind={job.kind} 注册 handler（不要假装跑过了）",
            retryable=False,
        )
        return
    try:
        result = await handler(db, job)
    except Exception as exc:  # noqa: BLE001 - 失败要记账，不能吞
        logger.exception("Agent job failed: %s", job.id)
        await _finish(db, job, error=f"{type(exc).__name__}: {exc}")
        return
    await _finish(db, job, result=result)


async def run_once(db: AsyncSession, *, use_row_lock: bool = False) -> bool:
    """领一件并跑掉。返回是否真的处理了一件（供循环决定要不要 sleep）。"""
    job = await claim_next(db, use_row_lock=use_row_lock)
    if job is None:
        return False
    await execute(db, job)
    return True


async def run_forever(poll_seconds: float = 5.0, *, use_row_lock: bool = False) -> None:
    while True:
        processed = False
        try:
            async with _session() as db:
                processed = await run_once(db, use_row_lock=use_row_lock)
        except Exception as exc:  # noqa: BLE001 - 循环不能因为一次异常就退出
            logger.error("Agent job loop error: %s", exc)
            await asyncio.sleep(poll_seconds)
            continue
        if not processed:
            await asyncio.sleep(poll_seconds)


def _session():
    # 延迟导入：worker 进程与 API 进程的 session 工厂来自同一处配置。
    from app.db.session import SessionLocal

    return SessionLocal()
