"""Agent 运行记录。

一次 `AgentRun` = 一轮用户消息触发的完整工具循环;`AgentStep` 是循环里的每一步
(模型输出 / 工具调用 / 工具结果),用于排障、审计与用量统计。

会话上下文本身仍存在 `chat_messages`(带 run_id 回指),这里不重复保存消息正文。
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import Base


class AgentRunStatus(str):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("chat_sessions.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=True)
    surface = Column(String(16), nullable=False)
    status = Column(String(24), nullable=False, default=AgentRunStatus.RUNNING)
    # 做题工作台的角色：solver | reviewer | judge | decomposer | composer | gate。
    # 与 surface（从哪个入口进来）正交：surface 是审计语义，role 是「这一轮在扮演谁」。
    role = Column(String(24), nullable=True, index=True)
    # 这一轮「模型看到了什么」的指纹。盲复核要靠它复现：票与 packet 必须对得上。
    packet_hash = Column(String(64), nullable=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id", ondelete="SET NULL"), nullable=True, index=True)
    model_id = Column(
        Integer, ForeignKey("ai_models.id", ondelete="SET NULL"), nullable=True
    )
    model_name = Column(String(100), nullable=True)
    provider_name = Column(String(100), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    steps = relationship(
        "AgentStep", back_populates="run", cascade="all, delete-orphan", order_by="AgentStep.idx"
    )


class AgentStep(Base):
    __tablename__ = "agent_steps"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    idx = Column(Integer, nullable=False)
    type = Column(String(16), nullable=False)  # assistant | tool_call | tool_result
    tool_name = Column(String(64), nullable=True)
    args = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    run = relationship("AgentRun", back_populates="steps")
