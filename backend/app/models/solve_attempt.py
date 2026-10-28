"""一轮独立作答的原始记录。

设计要点（都是踩过坑换来的）：

1. **只追加，不改**。同一道题的第 1/2/3 轮各一行，永不覆盖——「两轮一致」这件事必须
   能回放，否则无法判断「一致」是真收敛还是同一个错误犯两遍。
2. **`raw` 存模型原文**。入库走确定性解析（另一步），解析规则改了可以重放，不必重调模型。
3. **`steps` 是思维量的唯一来源**。`units_total` / `points_total` 是由 steps 算出来的
   派生值，不是人工填的；没解过的题是「未测」，不是 0。
4. 本表**不写** `questions.answer`。答案提升（promote）是另一件事：多轮一致 / 多模型投票
   通过后才写主表，并且要留 provenance。
"""
import enum

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.models.base import Base


class SolveAttemptStatus(str, enum.Enum):
    OK = "ok"
    PARSE_FAILED = "parse_failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class SolveAttempt(Base):
    __tablename__ = "solve_attempts"
    __table_args__ = (Index("ix_solve_attempts_qid_round", "question_id", "round"),)

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id", ondelete="CASCADE"), nullable=False, index=True)
    round = Column(Integer, nullable=False, default=1)
    model_name = Column(String(100), nullable=True)
    provider_name = Column(String(100), nullable=True)
    # 面向人的答案（Markdown / LaTeX 混排）与面向判等的纯文本形态，两者都留：
    # 判等器吃纯文本，页面渲染吃 LaTeX。
    answer_md = Column(Text, nullable=True)
    answer_plain = Column(Text, nullable=True)
    steps = Column(JSON, nullable=True)  # [{n, kind, line, units}]
    units_total = Column(Integer, nullable=True)
    points_total = Column(Integer, nullable=True)
    raw = Column(JSON, nullable=True)  # 模型原始输出，供重放
    status = Column(String(16), nullable=False, default="ok")
    error = Column(Text, nullable=True)
    agent_run_id = Column(String(36), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
