# 智能体做题工作台（Agent Workbench）设计草案

> 状态：草案（fork 后第一批开发的设计依据）
> 适用范围：`backend/app/{ai,capabilities,worker.py,models}` 的扩展
> 许可证：本项目为 AGPL-3.0-or-later。个人自用不触发「网络服务须公开修改」义务；一旦对外提供服务，修改必须公开。

## 0. 这份文档要解决什么

现状：本项目的 AI 只用在一处半——**导入抽取**（`services/importing/`）与**组稿助手**（`ai/runtime.py` 的工具循环）。`agent_runs` / `agent_steps` 只是聊天循环的审计记录，做题这条主线完全没被建模。

我们要的是：把「智能体独立做题 → 多模型盲复核 → 第三方判等 → 拆题 → 变式 → 思维量计量」做成**一等公民**——可批量、可审计、可复现、可回溯到「哪个模型、第几轮、看了什么、得出什么」。

## 1. 五条不可让的底线（来自题库项目的血账，别在本项目重犯）

1. **一字段一写者**。同一字段只允许一条写路径；多写者必然漂。
2. **同一事实不许两处存储**。曾出现「解法里的答案」与「题目最终答案」并存，34 条漂移且四道闸门全绿。
3. **盲复核的 packet 不许含上游自证**：`provenance`、`agree_rounds`、`confidence`、`method`、`notes` 一律剥掉。看见了就不是复核数学，是复核共识。
4. **判等必须第三方家族**。产答案的模型不许给自己判分；判等模型与投票模型不同家族。
5. **模型只产原始件，入库由确定性代码做**。模型输出先落盘（`data/tmp` 语义的原始 JSON），再由可重放的解析器入库；闸门（确定性检查）**绝不调用模型**。

## 2. 我们已有的 7 条流水线 → 本项目的构件映射

| 流水线 | 现在（题库项目） | 在本项目落成 |
|---|---|---|
| 录入 | OCR → 切章 → 裁图 → `import_md.py` | **已有**：`import_tasks` + 智能导入（Word/MD/图片/zip/文件夹） |
| 图形语义 | `figure_lite_packet.py` → VLM 盲读 → `figure_apply_lite.py` | 新表 `question_figure` + capability `figure.annotate` |
| 拆题 | `decompose_packet/ingest` + `decompose_check` | 新表 `question_decompose`(+conditions/asks/slots) + capability `decompose.run` |
| 解题 | `solve_packet` → 多轮 → `solve_ingest` → `growth.promote` | 新表 `solve_attempts` + capability `solve.run`（多轮独立作答） |
| 盲复核 | `blind_review.py`（多模型投票）+ `blind_judge.py`（第三方判等）+ `blind_apply.py` | 新表 `review_votes` / `review_verdicts` / `disputes` + 复用 `agent_runs` 记每票 |
| 变式 | `variant.py packet/check/ingest/verify-packet/verify-apply/judge-apply` | `question_relations` 加 `variant_of` + 新表 `variant_verify` |
| 思维量 | `difficulty.py measure()`（`units_total`/`points_total`/`density`/`kind_variety`/`thinking_score`） | 新列（**不要复用 `difficulty` 的 1–5**） |
| 闸门 | 55 关 + 元闸门 + 地基指纹 | 新表 `gate_runs` + `backend/scripts/gates/`（纯确定性，不进模型调用） |

## 3. 数据模型（Alembic 增量，不动既有表语义）

### 3.1 扩展 `agent_runs`（不新建 run 表）

`agent_runs` 现在只有 `surface`（api/chat/worker/mcp）。做题需要再加三列：

```python
role = Column(String(24), nullable=True)      # solver | reviewer | judge | decomposer | composer | gate
packet_hash = Column(String(64), nullable=True)  # 这一轮「模型看到了什么」的指纹，盲复核可复现
question_id = Column(Integer, ForeignKey("questions.id", ondelete="SET NULL"), nullable=True)
```

理由：一次「解题第 2 轮」「kimi 的第一张票」「minimax 的判等」都是**一次 agent_run**，`agent_steps` 里天然能装「取 packet → 调模型 → 解析 → 写库」。UI 侧直接复用现有 run 列表。

### 3.2 新表

```python
class QuestionFigure(Base):            # 1:1
    question_id (PK, FK questions.id, cascade)
    status            # none | placeholder | filled
    mode              # lite | svg（先只做 lite）
    kind              # coordinate_system | triangle | circle | composite | quadrilateral
    necessity         # required | confirmatory | decorative
    not_to_scale      bool
    answer_branches   int
    figure_only       Text            # 图上独有的信息（数字/字母/等长标记）
    branches_note     Text
    entities / relations  JSON
    by_model / packet_hash / updated_at

class QuestionDecompose(Base):         # 1:1
    question_id (PK)
    version int
    conditions / asks / slots  JSON    # slots 是「旋钮」：name/current/range/effect
    by_model / packet_hash / updated_at

class SolveAttempt(Base):              # 1:N
    id (PK), question_id (FK, index)
    round int, model_id / model_name / provider_name
    answer_md Text, answer_plain Text
    steps JSON                         # 每步 kind/units/line（思维量的来源）
    units_total int, points_total int
    raw JSON                           # 模型原始输出（可重放）
    status                             # ok | parse_failed | timeout
    created_at

class ReviewVote(Base):                # 1:N
    id (PK), question_id (FK, index)
    model_name / provider_name
    packet_hash                        # 必须与 agent_runs.packet_hash 对得上
    answer_md Text
    verdict                            # same | differ | undecidable
    raw JSON
    created_at

class ReviewVerdict(Base):             # 1:N（第三方判等结论）
    id (PK), question_id (FK)
    judge_model / judge_provider
    votes_hash                         # 参与判等的票集合指纹
    result                             # pass | incorrect | undecidable
    canonical_answer Text              # 判等给出的「标准答案」
    evidence Text                      # 判据（写给人看）
    created_at

class Dispute(Base):                   # 争议池
    id (PK), question_id (FK)
    kind                               # answer_conflict | stem_ambiguous | figure_ambiguous
    status                             # open | resolved | wontfix
    note Text, evidence Text
    resolved_by / resolved_at

class VariantVerify(Base):
    id (PK), question_id (FK)          # 变式题
    parent_id (FK questions.id)
    blind_answer Text
    judge_result                       # same | differ | undecidable
    canonical_answer Text
    created_at

class GateRun(Base):
    id (PK), gate_name, ok bool, detail JSON, duration_ms, git_rev, created_at
```

### 3.3 关系枚举扩展

`question_relations.kind` 现只有 `decomposed_from`。加：`variant_of`（变式）、`generalizes`（一般化）。

## 4. 角色与 packet（盲眼）

```python
# app/services/agent_packet.py
def build_solve_packet(q) -> dict:
    """解题 packet：题干 + 图形语义 + 题面问数。不含任何既有答案/解法/置信度。"""

def build_review_packet(q) -> dict:
    """复核 packet：与 solve packet 同构，但只给题干与图；不给人给的答案。"""

def packet_hash(pkt: dict) -> str:
    """规范化 JSON 的 sha256，落 agent_runs.packet_hash 与 review_votes.packet_hash。"""
```

判等（`judge`）输入是「票 + 库内答案」，输出是 `pass | incorrect | undecidable`；**判等员由 `ai_config` 里单独指定**，代码层禁止「判等模型 == 任一投票模型」。

## 5. 模型可见的工具（`app/ai/tools/agent_workbench.py`）

只暴露「动作用例」，不暴露裸 SQL。每个都落 `agent_run`：

| 工具 | 作用 | 落表 |
|---|---|---|
| `get_question_packet(qid, purpose)` | 取盲 packet（purpose=solve/review/decompose） | — |
| `submit_solution(qid, round, answer_md, steps)` | 记一轮作答 | `solve_attempts` |
| `request_blind_review(qid, models[])` | 入队多模型盲票 | `agent_jobs` → `review_votes` |
| `judge_answers(qid)` | 第三方判等 | `review_verdicts` / `disputes` |
| `decompose_question(qid, ...)` | 写拆题层 | `question_decompose` |
| `propose_variant(parent_qid, ...)` | 产变式（进 `status=draft`，验收通过才发布） | `question_relations` + `variant_verify` |
| `measure_thinking(qid)` | 由 `solve_attempts.steps` 算思维量 | `questions` 新列 |
| `run_gates(scope)` | 跑确定性闸门 | `gate_runs` |

## 6. 后台执行：新增 `agent_jobs`

现有 `worker.py` 只消费 `import_tasks`（语义是「文件导入」）。做题是**长任务 + 可重试 + 有死信**，另起一张表更干净：

```python
class AgentJob(Base):
    id, kind            # solve | review | judge | decompose | variant | measure
    payload JSON        # {qid, models[], round}
    status              # queued | running | done | failed | dead
    attempts int, max_attempts int
    error Text, result JSON
    priority int, created_at, started_at, finished_at
```

worker 加一个 `process_agent_job()` 循环；**限流**：同一 `provider_name` 的并发不超过 N（现在是题间 sleep 3s 硬编码在脚本里，进服务端要换成令牌桶）。

## 7. 与 DSH 对接（双向，这是「发挥全部能力」的关键）

### 方向 A：DSH 作为客户端（已具备，需补写工具）

`app/mcp_server/` 已有 MCP server，现有工具以只读 + 组稿写为主。补三个：

- `queue_agent_job(kind, payload)` → 返回 job_id
- `get_job_status(job_id)` → 状态 + 结果摘要
- `submit_blind_vote(qid, model_name, answer_md, packet_hash)`

这样 DSH 侧一条命令就能「把这一章 38 道题排进解题队列，每道 2 轮，两个模型投票」，而重活在服务端跑，DSH 只轮询。

### 方向 B：把 DSH 的模型通道当成 provider

`app/services/ai_provider.py` 是 `AIProvider` 接口，`ai_config` 热配置。DSH 侧暴露的 OpenAI 兼容端点（`/v1/chat/completions`，`cn:glm-5.3`/`cn:kimi-k3-1`/`cn:minimax-m3`…）可以直接配成一个 provider —— **一个 provider、多个 model**，正好满足「多模型盲投票 + 第三方判等」的家族隔离。

### 方向 C（不建议先做）：DSH 直连数据库

绕过 `dedup` flush 钩子、`content_revision`、状态机，必出脏数据。

## 8. 分期与验收

- **S1（1–2 天）**：`agent_jobs` + `solve_attempts` 两张表 + `AgentJob`/`SolveAttempt` 模型 + `solve.run` capability + `queue_agent_job`/`get_job_status` 两个 MCP 工具 + worker 循环。**验收**：从 MCP 排一道题 2 轮，`agent_runs` 里能看到两条 run、`solve_attempts` 里两条记录、`questions.answer` 未被改动（只落 attempt）。
- **S2（3–5 天）**：`review_votes` / `review_verdicts` / `disputes` + 盲 packet + 判等家族隔离 + `promote`（两票一致才写 `questions.answer`，否则进争议池）。**验收**：故意注入一个错答案，能抓到并进争议池。
- **S3（1 周）**：`question_figure` / `question_decompose`（含 slots）+ 对应 packet/ingest + 图形语义 lite。
- **S4（1 周）**：`variant_of` + `variant_verify` + `gate_runs` + 思维量列；前端加两个面板（复核票、拆题旋钮）。

## 9. 已知风险

1. **同步漂移**：题库项目的 JSON 与本项目 DB 若双向可写，必漂（我们吃过 34 条漂移的亏）。规矩：`stem/options/answer` 只由导入写；派生字段（figure/decompose/thinking/verdict）只由工作台写。
2. **依赖重量**：`chromadb`、`weasyprint`（导出）在无图形库的环境装不上；做题工作台不依赖它们，测试可裁剪。
3. **成本**：多模型投票 = 3 倍调用。`ai_config` 里要给「每轮预算上限」与「每题最多票数」，否则批量跑会把额度烧光。
