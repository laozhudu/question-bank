"""agent workbench s1: 任务队列 + 作答记录 + agent_runs 角色列

Revision ID: f7b1c2d3e4a5
Revises: d4f6b8a0c2e3
Create Date: 2026-10-30

设计说明见 docs/development/agent-workbench.md。本迁移只做三件事：

1. 新建 `agent_jobs`（排队语义：可重试 / 优先级 / 死信）；
2. 新建 `solve_attempts`（只追加的作答记录，思维量的唯一来源）；
3. 给 `agent_runs` 加 `role` / `packet_hash` / `question_id`（执行语义：这一轮扮演谁、
   模型看到了什么）。三列都可空，存量数据零影响。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7b1c2d3e4a5"
down_revision: Union[str, Sequence[str], None] = "d4f6b8a0c2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("questions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("subject_id", sa.Integer(), sa.ForeignKey("subjects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_jobs_kind", "agent_jobs", ["kind"])
    op.create_index("ix_agent_jobs_status", "agent_jobs", ["status"])
    op.create_index("ix_agent_jobs_question_id", "agent_jobs", ["question_id"])
    op.create_index("ix_agent_jobs_subject_id", "agent_jobs", ["subject_id"])

    op.create_table(
        "solve_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "question_id",
            sa.Integer(),
            sa.ForeignKey("questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("provider_name", sa.String(length=100), nullable=True),
        sa.Column("answer_md", sa.Text(), nullable=True),
        sa.Column("answer_plain", sa.Text(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=True),
        sa.Column("units_total", sa.Integer(), nullable=True),
        sa.Column("points_total", sa.Integer(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "agent_run_id",
            sa.String(length=36),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_solve_attempts_id", "solve_attempts", ["id"])
    op.create_index("ix_solve_attempts_question_id", "solve_attempts", ["question_id"])
    op.create_index("ix_solve_attempts_qid_round", "solve_attempts", ["question_id", "round"])

    # agent_runs 扩三列：可空，存量行不受影响
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("role", sa.String(length=24), nullable=True))
        batch.add_column(sa.Column("packet_hash", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("question_id", sa.Integer(), sa.ForeignKey("questions.id", ondelete="SET NULL"), nullable=True)
        )
    op.create_index("ix_agent_runs_role", "agent_runs", ["role"])
    op.create_index("ix_agent_runs_packet_hash", "agent_runs", ["packet_hash"])
    op.create_index("ix_agent_runs_question_id", "agent_runs", ["question_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_question_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_packet_hash", table_name="agent_runs")
    op.drop_index("ix_agent_runs_role", table_name="agent_runs")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("question_id")
        batch.drop_column("packet_hash")
        batch.drop_column("role")

    op.drop_index("ix_solve_attempts_qid_round", table_name="solve_attempts")
    op.drop_index("ix_solve_attempts_question_id", table_name="solve_attempts")
    op.drop_index("ix_solve_attempts_id", table_name="solve_attempts")
    op.drop_table("solve_attempts")

    op.drop_index("ix_agent_jobs_subject_id", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_question_id", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_status", table_name="agent_jobs")
    op.drop_index("ix_agent_jobs_kind", table_name="agent_jobs")
    op.drop_table("agent_jobs")
