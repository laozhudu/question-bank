"""智能体任务队列。

一次 `AgentJob` = 一件「要做但很慢」的活儿：解一道题的第 N 轮、给一道题投一批盲票、
判一次等、拆一道题、产一道变式。与 `agent_runs` 的分工：

- `AgentJob` 是**排队语义**（可重试、有优先级、有死信），不关心模型细节；
- `AgentRun` 是**执行语义**（哪一轮、哪个模型、每一步调了什么、耗时多少）。

一个 job 可以产出多条 run（例如「给一道题投 2 个模型的票」= 2 条 run）。
任务本身不存业务结果，业务结果落各自的表（`solve_attempts` / `review_votes` …）。
"""
import enum
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.models.base import Base


class AgentJobKind(str, enum.Enum):
    SOLVE = "solve"
    REVIEW = "review"
    JUDGE = "judge"
    DECOMPOSE = "decompose"
    VARIANT = "variant"
    MEASURE = "measure"


class AgentJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"  # 超过 max_attempts，等人工处理


class AgentJob(Base):
    __tablename__ = "agent_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind = Column(String(24), nullable=False, index=True)
    status = Column(String(16), nullable=False, default=AgentJobStatus.QUEUED.value, index=True)
    # 入参（qid / round / models[] / 变式母题 …）。刻意不做强 schema：
    # 每种 kind 的入参由对应 handler 自己校验，避免改一个 kind 就要迁移表。
    payload = Column(JSON, nullable=False, default=dict)
    # 结果摘要（不是业务结果本体；业务结果落各自表）。
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    priority = Column(Integer, nullable=False, default=0)
    question_id = Column(Integer, ForeignKey("questions.id", ondelete="SET NULL"), nullable=True, index=True)
    subject_id = Column(Integer, ForeignKey("subjects.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
