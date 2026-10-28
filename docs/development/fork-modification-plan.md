# 修改规划：把「做题工作台」长进 QuestionBank

> 对象：本 fork（`laozhudu/question-bank`，基线 `b58f8d0`，AGPL-3.0-or-later）
> 目的：让**智能体独立做题之后的各个动作**（求解 / 盲复核 / 判等 / 拆题 / 图形语义 / 命题 / 闸门）
> 在这个平台里有地方落、有账可查、有人能签字；平台自带的「导入 / 组稿 / 权限 / 知识点 / 查重」
> 反过来喂我们的题库流水线。
> 结论先说：**能做，而且不需要改它已有的语义**——加表、加 capability、加工具、加队列，
> 已有的 `questions` / `compositions` / `import_tasks` / `agent_runs` 一个字段都不用重定义。

---

## 0. 一页结论

| 问题 | 答案 |
|---|---|
| 能不能把我们的动作搬进去 | 能。它的 `capability`（三段式 load→authorize→execute）+ `agent_runs/agent_steps`（工具循环审计）+ MCP server 正好是我们要的地基 |
| 要不要动它的核心表 | 不要。`questions` 的 `content/answer/thinking/analysis/summary` 全是 RichDoc JSON 字符串，够放题干与解答；**语义字段一律进新表** |
| 谁是真源 | 建议：**平台 DB = 运行真源**（题、票、任务、账），**git = 交付快照**（每批导出一份，可 diff、可回滚）。理由见 §8 |
| 最大风险 | ① AGPL ② 「同一事实两处存」必然漂 ③ 绕过 `dedup_fingerprint` / `content_revision` / 软删除写库会埋雷 |
| 分期 | S1 队列+作答记录（**已完成**）→ S2 盲票/判等/争议 → S3 图形语义+拆题 → S4 命题+闸门+思维量 → S5 导入桥 |

---

## 1. 三方特点对照（决定「谁干什么」）

| 维度 | 我们自建题库（qb-corpus） | QuestionBank（本 fork） | DSH |
|---|---|---|---|
| 定位 | **真相层**：机器可读的题目语义 + 确定性闸门 | **交付层**：导入、审核、组稿、导出、权限、多学科 | **执行层**：智能体干活、多模型、MCP、子代理 |
| 数据形态 | 一题一 JSON 文件，git 可 diff、可回滚 | SQLite/MySQL，RichDoc + 判别联合 AnswerSpec | 会话/文件/工具调用 |
| 强项 | 图形语义、拆题层（conditions/asks/**slots 旋钮**）、思维量（每步 units）、盲复核多模型投票、变式盲解验证、**55 关闸门 + 元闸门 + 地基指纹** | 智能导入（Word/MD/图片/zip）、组稿排版导出、多学科/多用户/权限、知识点树 + RAG、MinHash 查重、材料题 | 多模型（含视觉）、子代理、技能、上下文压缩、shell |
| 缺的 | 界面、权限、导入、组稿、多用户 | 上述语义与审计能力 | 长期账本、题目真源 |
| 结论 | 不搬界面 | **不搬语义** | 不搬真相 |

**分工一句话**：平台负责「把题弄进来 + 让人看见 + 导出成卷」，DSH 负责「让模型把题做透」，
我们的语义与闸门以**表 + 队列 + 审计**的形式嵌进平台，而 git 侧继续保留可 diff 的交付快照。

---

## 2. 缺口矩阵（我们 10 项能力 × 平台现状）

| # | 我们的能力 | 平台现状 | 落点 | 类型 |
|---|---|---|---|---|
| 1 | 一题一文件 + 逐字段 diff | 单表 `questions` | 保留 git 导出（S5），平台做运行态 | 桥 |
| 2 | 图形语义 `figure`（lite 四问 / entities / relations / necessity） | 无 | **新表** `question_figure` | 加表 |
| 3 | 拆题层 `conditions / asks / slots(旋钮)` | 只有 `question_relations.decomposed_from` 一种边 | **新表** `question_decompose` + `decompose_conditions` + `decompose_asks` + `decompose_slots` | 加表 |
| 4 | 解答多轮 + 每步思维量 | `questions.thinking/analysis` 是展示文本 | **新表** `solve_attempts`（**S1 已建**） | 已做 |
| 5 | 盲复核多模型投票 | 无（`review_count` 只是个计数） | **新表** `review_votes` | 加表 |
| 6 | 第三方判等 + 争议池 | 无 | **新表** `review_verdicts` + `disputes` | 加表 |
| 7 | 变式（母题 → 变式，含盲解验证） | `question_relations` 只有 `decomposed_from` | 扩枚举 `variant_of` + **新表** `variant_verifications` | 扩 + 加表 |
| 8 | 55 关闸门 + 元闸门 + 地基指纹 | 只有 pytest（开发期） | **新表** `gate_runs` / `gate_findings` + `foundation_stamps` | 加表 |
| 9 | 思维量计量（thinking_score / density / kind_variety） | `difficulty` 是 1–5 整数 | **新表** `question_metrics`（不要塞进 `difficulty`） | 加表 |
| 10 | 验收签字（谁看过、过没过） | `status` 状态机 + `review_count` | **新表** `acceptances`（或复用 `activity_log`？不建议） | 加表 |

> **为什么不塞进现有列**：`questions` 的五个 RichDoc 列是「给人看的富文本」，写进去的语义
> 没法查、没法建索引、没法做等值校验——正好违反我们「同一事实不许两处存」的底线。
> 派生/事实一律新表，且**一字段一写者**（写者名沿用我们 `schema/fields.json` 的 writer 口径）。

---

## 3. 数据模型规划

### 3.1 已有（S1 落地，迁移 `f7b1c2d3e4a5`）
```
agent_jobs(id, kind, status, payload, result, error, attempts, max_attempts,
           priority, question_id, subject_id, created_by, created_at, started_at, finished_at)
solve_attempts(id, question_id, round, model_name, provider_name, answer_md, answer_plain,
               steps, units_total, points_total, raw, status, error, agent_run_id)
agent_runs += role / packet_hash / question_id
```
`(question_id, round)` 复合索引；**同轮重复提交直接拒**——两轮各一行，这就是「独立求解」的证据。

### 3.2 待加（S2–S4）

| 表 | 关键字段 | 谁写 | 为什么这么设计 |
|---|---|---|---|
| `question_figure` | `question_id`(uq), `status`(none/placeholder/filled), `mode`(lite/full), `entities` JSON, `relations` JSON, `necessity`(required/confirmatory/none), `figure_only` text, `not_to_scale` bool, `ocr_page`, `writer`, `stamped_at` | annotator | 一题一图语义；`status` 用我们口径，别复用 `questions.status` |
| `question_decompose` | `question_id`(uq), `version` int, `chapter`, `topic`, `topic_id`, `summary`, `writer` | decomposer | 拆题是**一层**，条件/问/旋钮是它的子表 |
| `decompose_conditions` | `decompose_id`, `cid`(c1..), `text`, `kind`(given/implicit/from_figure/derived), `role`(defines/constrains/quantifies/constructs/qualifies), `depends_on` JSON, `removable_effect` text, `source`(stem/figure/both/notation), `seq` | decomposer | `(decompose_id, cid)` 唯一；`role` 五值是我们闸门的 enum |
| `decompose_asks` | `decompose_id`, `aid`(a1..), `text`, `answer_type`(number/expression/range/point/value/proof/set/other), `depends_on` JSON(只能指条件), `seq` | decomposer | 依赖指向 ask 是历史高频错，DB 层加 CHECK 挡一半 |
| `decompose_slots` | `decompose_id`, `sid`(s1..), `name`, `type`(text/number/...), `current`, `effect`, `provides`, `range`, `seq` | composer | **旋钮**：换一个条件，答案该变成什么。这是我们命题能力的核心 |
| `review_votes` | `question_id`, `role`(reviewer), `model_name`, `provider_name`, `packet_hash`, `verdict`(same/diff/undecided), `answer_md`, `confidence`, `agent_run_id`, `created_at` | reviewer | 一票一行；`packet_hash` 保证「票与它看到的题对得上」 |
| `review_verdicts` | `question_id`, `round`, `decision`(consistent/suspect/undecided/adjudicated), `judge_model`, `judge_provider`, `canonical_answer`, `rationale`, `agent_run_id` | judge | 第三家族判等；**判不了**要能落库（我们吃过「判不了当一致」的亏） |
| `disputes` | `question_id`, `topic`(answer/figure/decompose/…), `status`(open/resolved/wontfix), `evidence` JSON, `resolved_by`, `resolution`, `resolved_at` | human | 关闭必须写证据；否则等于凭印象销案 |
| `variant_verifications` | `variant_id`, `round`, `model_name`, `answer_md`, `verdict`, `agent_run_id` | verifier | 变式自带答案**必须盲解验证**才算数 |
| `question_metrics` | `question_id`(uq), `units_total`, `points_total`, `density`, `kind_variety`, `thinking_score`, `measured`(bool), `computed_at` | rebuild | 计量值，不是档位；`difficulty` 那列留给人 |
| `gate_runs` | `run_id`, `git_ref`, `started_at`, `finished_at`, `total`, `failed`, `meta_ok` | gate | 一次 CI 一行 |
| `gate_findings` | `run_id`, `gate`, `level`(red/yellow/na), `subject_ref`(qid/路径), `message`, `fix_hint` | gate | 红在哪、怎么修，要能查历史 |
| `foundation_stamps` | `scope`(figure/decompose/answer/…), `fingerprint`, `dirty_count`, `note`, `stamped_at` | gate | 地基指纹：改了地基要能算出「哪些产物过期」 |
| `acceptances` | `question_id`, `by_user_id`, `passed` bool, `note`, `accepted_at` | human | 逐题验收；与 `status` 状态机分开（审核 ≠ 验收） |

**迁移顺序**（每步独立可回滚，`batch_alter_table` 兼容 SQLite）：
`S2: review_votes → review_verdicts → disputes` ·
`S3: question_figure → question_decompose(+3 子表)` ·
`S4: question_metrics → variant_verifications → gate_runs/gate_findings/foundation_stamps → acceptances` ·
`S5: 只读导出视图（不建表）`。

---

## 4. 能力（capability）与工具规划

它的规矩（读代码确认过）：`Capability` 三段式 `load → authorize → execute`；`capabilities.run()` 是
API / AI 工具 / worker 的**唯一入口**；MCP 侧每个 capability 必须在
`app/mcp_server/tools.py::CAPABILITY_OUTPUT_TYPES` 登记输出类型，否则 MCP 直接 `KeyError`。

| capability | 权限 | 作用域 | mutating | 说明 |
|---|---|---|---|---|
| `agent.queue_job` ✅S1 | `EDIT_QUESTION` | subject | ✔ | 入队（solve/review/judge/decompose/variant/measure） |
| `agent.get_job`（S2） | `VIEW_QUESTION` | subject | ✘ | 查任务与结果摘要（MCP 轮询用） |
| `review.submit_vote`（S2） | `EDIT_QUESTION` | subject | ✔ | 盲票落库（带 `packet_hash`） |
| `review.record_verdict`（S2） | `EDIT_QUESTION` | subject | ✔ | 第三方判等结论 |
| `dispute.open` / `dispute.resolve`（S2） | `EDIT_QUESTION` / `MANAGE_SUBJECT` | subject | ✔ | 关闭必须带证据 |
| `figure.upsert`（S3） | `EDIT_QUESTION` | subject | ✔ | 图形语义写一题 |
| `decompose.upsert`（S3） | `EDIT_QUESTION` | subject | ✔ | 拆题（含条件/问/旋钮整体替换） |
| `variant.create` / `variant.verify`（S4） | `EDIT_QUESTION` | subject | ✔ | 变式入库 / 盲解验证结论 |
| `metrics.recompute`（S4） | `MANAGE_SUBJECT` | subject | ✔ | 重算思维量 |
| `gate.report`（S4） | `MANAGE_SUBJECT` | subject | ✔ | CI 结果回灌 |

**权限模型不扩**：平台只有 6 个 `Permission`（`VIEW_QUESTION / EDIT_QUESTION / EDIT_TAXONOMY /
MANAGE_SUBJECT / MANAGE_MEMBERS / VIEW_PRIVATE_ANY`）。我们的 solver/reviewer/judge 是**角色语义**，
不是权限——落 `agent_runs.role` + `agent_jobs.kind`，不新增 Permission。这条必须守住：
权限是「能不能动」，角色是「这一轮在扮谁」，混在一起以后没法做盲眼约束。

**MCP 工具增量**（给 DSH 用，全部走 capability，不直连 DB）：
```
queue_agent_job(kind, question_id, round?, models?, payload?, priority?, dedupe?)   ✔S1
get_agent_job(job_id)                                                                ✔S1
submit_blind_vote(question_id, model, answer_md, packet_hash?)                       S2
list_pending_jobs(kind?, limit?)                                                     S2
get_question_semantics(question_id)   # figure + decompose + metrics 一次取全          S3
upsert_figure(question_id, payload)                                                  S3
upsert_decompose(question_id, payload)                                               S3
open_dispute(question_id, topic, evidence) / resolve_dispute(id, resolution)          S2
```
`get_question_semantics` 是给 DSH 的**关键一个**：它把一题的全部语义（图/拆题/计量）打成一份
packet，避免模型为了拼上下文连打五六个工具。

---

## 5. 流水线编排（七条流水线在平台里怎么跑）

平台侧统一形态：**MCP/API 入队 → worker 领取 → handler 干活 → 落表 → 人签字**。

| 流水线 | 入队 kind | handler 干什么 | 产物表 | 谁验收 |
|---|---|---|---|---|
| 导入 | 平台自带 `import_tasks` | 平台：Word/MD/图片 → draft `questions` | `questions` | 人（平台 UI） |
| 求解 | `solve` ✅S1 | 盲 packet → 模型 → 解析 → 写一轮 | `solve_attempts` | 多轮一致 + 判等 |
| 复核 | `review` | 盲 packet → 多家族模型各出一票 | `review_votes` | judge |
| 判等 | `judge` | 第三家族比对票与库内答案 | `review_verdicts` | 人（争议） |
| 拆题 | `decompose` | 模型产 conditions/asks/slots → schema 校验 | `question_decompose`+子表 | 闸门 |
| 图形语义 | `variant`/`figure`（新增 kind `figure`） | 视觉模型盲读四问 | `question_figure` | 闸门 + 人抽检 |
| 命题 | `variant` | 母题 + 旋钮 → 变式 → 盲解验证 | `questions`(草稿) + `variant_verifications` | 盲解一致 |
| 计量/闸门 | `measure` / `gate` | 重算思维量；回灌 CI | `question_metrics` / `gate_*` | 无（确定性） |

**状态机**（`agent_jobs`）：`queued → running → done | failed(→queued 重试) | dead(超 attempts)`。
死信必须留 `error` 文本；「没有 handler」也要死信，不许记 done（S1 已用测试钉住）。

---

## 6. DSH 对接（三条通道，不选「直连 DB」）

**通道 A：DSH 当 MCP 客户端挂进来（主力）**
- 平台已有 `/api/mcp`（Streamable HTTP、无状态、API Key 鉴权、按 key 的权限执行）。
- 在 DSH 侧把这个 server 注册成 MCP，模型就能直接 `queue_agent_job` / `get_agent_job` / `get_question_semantics`。
- 好处：权限沿用平台的 API Key，DSH 侧不需要另建一套账号；所有写操作仍走 capability（有 `activity_log`）。

**通道 B：把 DSH 网关配成平台的 `AIProvider`（让平台自己会做题）**
- DSH 暴露 OpenAI 兼容端点（本机实测 `http://192.168.99.223:7864/v1`，`/v1/models` 可用）。
- 平台 `ai_config` 支持一个 provider 多 model → 正好满足我们的**家族隔离**（solver 用 glm、reviewer 用 kimi/deepseek、judge 用 minimax）。
- 配置要点：`interface_type=openai`、`base_url` 指网关、`api_key` 存网关 key、模型名照抄（如 `cn:glm-5.3`、视觉 `cn:glm-5v-turbo`）。

**通道 C：半自动回灌（S5）**
- 平台导出 JSON → 我们 `qb-corpus` 侧的 `import` 脚本落成 git 文件 → 跑 55 关 → 差异回写平台。
- 只在需要「可 diff 交付物」时用；平时不做双向同步（**双向必漂**）。

**明确不做**：不经 capability、不触发 `dedup_fingerprint` flush 钩子、不动 `content_revision` 的
直连 SQL 写题。那会同时绕过查重、版本过期检测和审计。

---

## 7. 分期与验收

| 期 | 内容 | 验收标准 | 状态 |
|---|---|---|---|
| **S1** | 任务队列 `agent_jobs` + 作答记录 `solve_attempts` + `agent_runs` 角色列 + `agent.queue_job` + 两个 AI 工具 + worker 领取 + 迁移 | 全量测试绿（986 passed）；入队不写业务结果；无 handler 死信；两轮不覆盖；迁移在空库跑通 | **已完成**（`b81fae1`、`7c78b35`） |
| **S1b** | `solve` handler 真跑一道题（DSH 网关） | 一道题两轮 = 两条 `solve_attempts` + 两条 `agent_runs`，`packet_hash` 可复现；`questions.answer` 不变 | 代码已就位（假 provider 测过），待真网关冒烟 |
| **S2** | 盲票 / 判等 / 争议池 | 24 题盲复核可全流程在平台跑；「判不了」能落库；争议关闭必须带证据 | 规划 |
| **S3** | 图形语义 + 拆题（含旋钮） | 一题的 figure+decompose 能一次取全（`get_question_semantics`）；schema 校验与我们在 git 侧的 8 硬 3 软对齐 | 规划 |
| **S4** | 命题 + 盲解验证 + 思维量 + 闸门回灌 | 变式必须过盲解才 `verified`；`question_metrics` 与我们 `difficulty.py` 同值；`gate_runs` 能查历史红灯 | 规划 |
| **S5** | 导入桥 + git 导出 | 平台导入的题能一键落成 `data/questions/qNNNN.json` 并过 55 关 | 规划 |

---

## 8. 红线与风险

1. **AGPL-3.0-or-later**：fork 可以，**对外提供网络服务要公开修改**。个人用无碍；一旦当服务跑给别人用，必须评估。这条要先拍板。
2. **同一事实两处存**：`questions.answer` 与 `solve_attempts.answer_md` 是两处。规矩：`solve_attempts` 是**过程**（只追加），`questions.answer` 是**结论**（只有人/裁定能改）。任何「把第 N 轮答案直接写成库内答案」的代码都不许写。
3. **查重钩子**：`dedup_fingerprint` / `question_dedup_bands` 由 flush 钩子维护，绕过去写题 = 查重失效。
4. **`content_revision`**：组稿靠它判断「题被改过了」。写题必须走 `question.update` capability 才会 bump。
5. **软删除**：`questions.deleted_at`；删除不是物理删，别在自建 SQL 里 `DELETE`。
6. **SQLite 兼容**：桌面版是 SQLite。迁移一律 `batch_alter_table`；测试 `test_no_model_migration_drift` 会抓「模型与迁移不一致」（S1 就抓到 `ix_solve_attempts_id`）。
7. **MCP 输出类型**：新增 capability 必须同时登记 `CAPABILITY_OUTPUT_TYPES`，否则所有 MCP 测试连锁 `KeyError`（S1 踩过）。
8. **工具集合断言**：`tests/test_ai_tools.py` 断言精确工具集合，加工具要同步改（S1 踩过）。

---

## 9. 需要你拍板的 5 件事

1. **真相在哪**：平台 DB 当运行真源（推荐）／git 当唯一真源、平台只读投影？
2. **AGPL**：个人自用可以；将来会不会对外提供服务？
3. **多学科**：只放初中数学这一本，还是把「学科」当多本书用（一章=一册）？
4. **前端要不要动**：S1–S4 我建议**先只做 API/MCP**（模型和人用 MCP 就够），前端等流程稳定再补页面。你同意吗？
5. **验收签字落在哪**：平台里点（新表 `acceptances`）还是继续在 `qb-corpus` 的验收页里点（`review.py --accept`）？

---

## 10. 已落地的代码（本分支）

- `docs/development/agent-workbench.md`：设计草案（S1–S4 分期 + DSH 三向对接）
- `backend/app/models/agent_job.py`、`backend/app/models/solve_attempt.py`、`agent_runs` 加三列
- `backend/app/capabilities/agent_workbench.py`（`agent.queue_job`）
- `backend/app/services/agent_jobs.py`（register_handler / claim_next / run_once / run_forever）
- `backend/app/services/solve_handler.py`（盲 packet → 模型 → 一轮作答）
- `backend/app/ai/tools/agent_workbench.py`（`queue_agent_job` / `get_agent_job`）
- `backend/alembic/versions/f7b1c2d3e4a5_agent_workbench_s1.py`
- `backend/tests/test_agent_workbench.py`（13）+ `tests/test_solve_handler.py`（7）
