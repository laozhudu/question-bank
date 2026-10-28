# 实施定位表：每处改动落在哪个文件的哪一行

> 配套文档：`docs/development/fork-modification-plan.md`（做什么、为什么）
> 本文件只回答一件事：**改哪个文件的哪个符号**，以及**我们的字段到底塞进哪一列**。
> 基线：`b58f8d0` + 本分支已落地的 S1（迁移 head = `f7b1c2d3e4a5`）。
> 所有行号以当前分支实测为准，改前请再 `grep -n` 复核一次。

---

## A. 字段级映射（`data/questions/qNNNN.json` → 平台落点）

我们一题一 JSON（实测 q0109 的真实键），平台是 SQL 表。下表逐字段给出**落点**与**转换器**。
原则：**能派生的不存**、**语义进新表**、**富文本只进 `questions` 那五列**。

### A1. 题目主体

| 我们的字段 | 平台落点 | 转换 | writer | 坑 |
|---|---|---|---|---|
| `id`（`"q0109"`） | `questions.id`(int) + **新列 `external_ref`**(varchar 16, 与 `subject_id` 唯一) | 保留我们的 qid 做对账键 | ingest | 平台主键是自增 int，**不能直接当 qid 用**；没有 `external_ref` 就没法双向对账 |
| `stem_md` | `questions.content`（RichDoc JSON 字符串） | 先 `\(…\)`→`$…$`、`\[…\]`→`$$…$$`，再 `markdown_to_rich_doc()` | ingest | 它的 dollarmath 插件只认 `$`，**我们全库是 `\( \)`**——必须先转，否则整段变纯文本（数学全丢） |
| `stem_plain` / `stem_text` | **不存** | 需要时 `rich_doc_to_plain_text(content)` 现算 | — | 存了就是「同一事实两处存」，必漂 |
| `qtype`（solve/fill/choice/proof） | `questions.q_type` | `solve→free_response`、`fill→fill_in_the_blank`、`choice→single_choice`、`proof→free_response` | ingest | `proof` 与 `solve` 都落 `free_response`，区分靠 `decompose.asks[].answer_type=proof`（别另加列） |
| `kind`（example/practice） | 标签 `tags`（例题/练习） | 建两个 tag | ingest | 平台的 `q_type` 是**作答形态**，不是题型分类，别混 |
| `chapter` / `topic` / `topic_id` | `knowledge_points`（两级树）+ 多对多表 `question_knowledge_points`（`Question.knowledge_points` 关系，`app/models/question.py:26,141`） | 章→`parent_id=null` 的 kp，`slug=ch04`；专题→子 kp，`slug=ch04-t02` | ingest | 它的 `slug` 是学科内唯一；我们的 `topic_id` 正好当 slug |
| `answer.final_md` | `questions.answer`（AnswerSpec 判别联合 JSON） | 自由作答 → `FreeResponseAnswer.reference`；填空 → `FillBlankAnswer.blanks[].accept[]` | 裁定 | **只有人/裁定能写**；`solve_attempts` 永远不写这里 |
| `answer.final_plain` | **不存** | 需要时 `strip_latex(final_md)` 现算 | — | 我们在 git 侧吃过「纯文本字段里塞 LaTeX」的亏 |
| `answer.provenance`（solved/adjudicated/needs_review） | **新表 `answer_states`**.`provenance` | 枚举直存 | 裁定 | 不要塞进 `questions.status`（那是 draft/pending/published/archived 的**发布**状态机） |
| `answer.agree_rounds` / `answers_by_round` / `rounds` | `answer_states`.`agree_rounds`(JSON) + `solve_attempts` 逐轮 | 逐轮答案本来就在 `solve_attempts.answer_md` | 裁定 | `answers_by_round` 是**派生**：由 attempts 重算，别单存一份 |
| `answer.review_note` | `answer_states`.`note` | 直存 | 裁定 | 与 `disputes.resolution` 别重复：有争议才进 disputes |
| `review{by,date,passed,note}` | **新表 `acceptances`** | `by`→`by_user_id`（人名字符串无法直连用户表时留 `by_name`） | 人 | 验收 ≠ 审核：`status` 走平台状态机，签字走这张表 |
| `images[]`（`images/p43_….jpg`） | `media_asset`（`sha256` 去重）+ `content` 里的 `image` 节点 | `media_service.content_url(asset_id)` 拼进 markdown 再转 RichDoc | ingest | 平台已有 MCP 工具 `create_media_upload_url`，**别绕过它直接写文件** |
| `schema_version`(4) | `questions.content_schema_version` | 我们 4 ↔ 它 1，映射表放常量 | ingest | 语义不同，别直接赋值 |
| `curriculum` / `prerequisites`（全 placeholder） | **不迁** | — | — | 空壳搬过去只会让「有值」变成假象 |

### A2. 图形语义 → `question_figure`（一题一行）

| 我们的字段 | 列 | 说明 |
|---|---|---|
| `figure.status` | `status` | `none/placeholder/filled`，**枚举与 git 侧一致** |
| `figure.mode` | `mode` | `lite/full` |
| `figure.kind` | `kind` | `triangle/circle/composite/quadrilateral/coordinate_system/…`（**自由串 + 校验在闸门**，别做 DB ENUM，否则新形状要迁移） |
| `figure.necessity` | `necessity` | `required/confirmatory/none` |
| `figure.necessity_note` | `necessity_note` | text |
| `figure.figure_only` | `figure_only` | JSON 数组（「图上才有、题干没有」的信息） |
| `figure.not_to_scale` | `not_to_scale` | bool |
| `figure.answer_branches` | `answer_branches` | int |
| `figure.branches_note` | `branches_note` | text |
| `figure.review{who,at,confidence,uncertain}` | `review_who` / `reviewed_at` / `confidence` / `uncertain` | 摊平四列，别存 JSON（要能按 confidence 筛） |
| （缺）`entities` / `relations` | `entities` / `relations` | **预留 JSON 列**：lite 模式不填，S3 后期补 full 模式时不用迁移 |

### A3. 拆题层 → `question_decompose` + 三张子表

`decompose` 是一棵树：一层壳 + 条件/问/旋钮/实体/方法。**实体与方法也各给一张子表**（不然又是 JSON 黑洞）。

| 我们的字段 | 落点 | 说明 |
|---|---|---|
| `decompose.status` / `version` | `question_decompose.status` / `version`(int) | `version` 我们实测**必须是整数 1**，别写 `"v1"` |
| `decompose.conditions[]` | `decompose_conditions` | 每项字段见下 |
| `conditions[].id`（`c1`） | `cid` | 与 `decompose_id` 组成唯一键 |
| `conditions[].text` | `text` | |
| `conditions[].kind` | `kind` | `given/implicit/from_figure/derived`（CHECK 约束） |
| `conditions[].role` | `role` | `defines/constrains/quantifies/constructs/qualifies`（CHECK 约束；我们模型产出的 `constraint/confirms/…` 必须**先映射再入库**） |
| `conditions[].source` | `source` | `stem/figure/both/notation`（CHECK） |
| `conditions[].depends_on[]` | `depends_on` JSON | 指向其它 `cid`；跨表外键做不了，靠闸门校验 |
| `conditions[].provides[]` | `provides` JSON | 它「提供了什么结论」 |
| `conditions[].essential` | `essential` bool | 我们数据里有，平台没有对应概念 |
| `conditions[].note` / `removable_effect` | `note` / `removable_effect` | |
| `asks[].id`/`text`/`answer_type`/`depends_on`/`figure_dependent`/`note` | `decompose_asks` 同名列 | `answer_type` 七值 CHECK（`number/expression/range/point/value/proof/set/other`） |
| `slots[].id/name/type/current/range/effect` | `decompose_slots` 同名列 | `effect` 是「换掉这个条件，答案变成什么」——**长文本，别截断** |
| `decompose.entities[]`（字符串数组） | `decompose_entities(name, seq)` | 单独成表才能按实体检索 |
| `decompose.methods[]`（`id/name/uses/yields`） | `decompose_methods(mid, name, uses JSON, yields JSON, seq)` | |

### A4. 解答 → `solve_attempts`（S1 已建）

| 我们的字段 | 列 | 说明 |
|---|---|---|
| `solutions[].round` | `round` | 与 `question_id` 唯一；**重复提交要拒**（S1 已实现） |
| `solutions[].model` | `model_name` | 再加 `provider_name`（我们原来没有，平台有） |
| `solutions[].answer` | `answer_md` | |
| `solutions[].steps[]`（`n/kind/line/units`） | `steps` JSON | 平台不拆表：步是**一份快照**，不是可独立查询的实体 |
| `solutions[].draft` / `rank` / `confidence` / `method` / `notes` / `answer_text` / `answer_branches` / `used_figure_only` | `raw` JSON（保留原样） | 这些是**过程元数据**，进 `raw` 够用；要筛的（`units_total`/`points_total`）才提列 |
| （派生）`difficulty.units_total` / `points_total` | `units_total` / `points_total` | 由 `steps` 现算，S1 已算 |

### A5. 思维量 / 变式 / 闸门

| 我们的字段 | 落点 |
|---|---|
| `difficulty.units_total/points_total/thinking_score/thinking_density/thinking_kind_variety` | `question_metrics`（一题一行，`computed_at` + `measured`） |
| `difficulty.level`（人字段，168/168 空） | `question_metrics.level`（`易/中/难`），**writer 只有人** |
| `data/variants/*.json`（14 键：`family/parent/change/basis/why_worth/difficulty/verify/status/stem_fp`…） | 变式本体 → `questions`（草稿）+ `question_relations.relation_type='variant_of'`；`verify` → `variant_verifications`；`stem_fp` → `questions.dedup_fingerprint` 复用 |
| `data/log/disputes.jsonl` | `disputes` 表（`topic/status/evidence/resolved_by/resolution`） |
| 55 关结果 | `gate_runs` + `gate_findings`（`level=red/yellow/na`，与闸门输出一一对应） |
| `data/log/foundation.jsonl`（bump/stamp 事件） | `foundation_stamps`（`scope/fingerprint/dirty_count/note`） |
| `data/log/reviews.jsonl` | 拆两处：**票**进 `review_votes`，**结论**进 `review_verdicts` |

---

## B. 代码锚点（改哪个文件哪一行）

> 行号是本分支实测；括号里是符号名，比行号稳。

### B1. 加能力（capability）

| 目标 | 文件:行 | 现有内容 | 要做的事 |
|---|---|---|---|
| 能力基类 | `backend/app/capabilities/base.py:47`（`class Capability`） | `input_model:51`、`authz:52`、`permission:53`、`scope:54`、`mutating:55`、`authorize():62-66` | **不用改**；新能力继承它即可 |
| 新域模块 | 新建 `backend/app/capabilities/review.py`、`figure.py`、`decompose.py`、`gates.py` | — | 每个文件一个 `@register class …` |
| **注册入口** | `backend/app/capabilities/__init__.py:18` | `from . import agent_workbench, compositions, library, paper_imports, questions` | **新域必须加进这一行**，否则注册表里没有（PyInstaller 不扫目录） |
| 未授权白名单 | `backend/app/capabilities/registry.py:19`（`UNGATED_ALLOWLIST`） | `frozenset()` | 新能力一律带 `permission`，**不许进白名单** |
| 错误码 | `backend/app/core/api_errors.py` | `ErrorCode.QUESTION_NOT_FOUND:100`、`AI_CONFIG_MODEL_UNUSABLE:189` | 新错误码加在这个枚举里（已有 `VALIDATION_FAILED`，**没有 `VALIDATION_ERROR`**） |

### B2. 加 MCP 工具

| 目标 | 文件:行 | 说明 |
|---|---|---|
| 输出类型表 | `backend/app/mcp_server/tools.py:61`（`CAPABILITY_OUTPUT_TYPES`） | **每个新 capability 必须登记**；取用处是 `:245 output_type = CAPABILITY_OUTPUT_TYPES[cap.name]`，漏了就是全站 MCP `KeyError`（S1 踩过） |
| 工具名与描述 | `backend/app/mcp_server/tools.py:INSTRUCTIONS`（文件头） | 面向模型的英文说明；新增「做题工作台」段（怎么入队、怎么轮询、盲 packet 不含答案） |
| 认证与作用域 | `backend/app/mcp_server/auth.py`（`SCOPE_KEY`/`McpAuth`） | 不改：API Key 即身份，权限按 key 所有者算 |

### B3. 加 AI 工具（对话里能用的）

| 目标 | 文件:行 | 说明 |
|---|---|---|
| 工具注册 | `backend/app/ai/tools/registry.py:20`（`register`） | 重名导入期就炸 |
| 导入触发 | `backend/app/ai/tools/__init__.py:11-18` | 新域模块加进这个 import 列表 |
| **工具集合断言** | `backend/tests/test_ai_tools.py:60-62`（`test_registry_exposes_the_expected_tools`） | 精确集合断言，**加工具必须同步改这一处**（S1 踩过） |

### B4. 加队列 handler

| 目标 | 文件:行 | 说明 |
|---|---|---|
| handler 注册 | `backend/app/services/agent_jobs.py`（`register_handler`/`_HANDLERS`） | 新 kind 一个文件：`review_handler.py`/`judge_handler.py`/`figure_handler.py`/`decompose_handler.py`/`variant_handler.py` |
| **注册触发** | `backend/app/worker.py:18` | 现在是 `from app.services import agent_jobs, data_migrations, solve_handler`；新 handler 加进这一行（不加就永远「没有 handler」→ 死信） |
| 领取循环 | `backend/app/worker.py:191`（`if await agent_jobs.run_once(...)`）与 `:193 sleep(10)` | 队列优先级/并发度要调就改这里 |
| SQLite 兼容 | `backend/app/worker.py:168`（`use_row_lock = not get_db_url().startswith("sqlite")`） | 新 handler 若自己 select，要沿用这个开关 |

### B5. 加 API 端点（可选，给前端/脚本用）

| 目标 | 文件:行 | 说明 |
|---|---|---|
| 路由挂载 | `backend/app/api/v1/api.py:1`（import 行）+ 末尾 `include_router(...)` | 新 `endpoints/agent_jobs.py`、`endpoints/reviews.py` 在这里挂 |
| 鉴权依赖 | `backend/app/api/deps.py`（`get_current_active_user`） | 与现有端点一致 |

### B6. 迁移

| 目标 | 位置 | 说明 |
|---|---|---|
| 目录 | `backend/alembic/versions/`（head = `f7b1c2d3e4a5_agent_workbench_s1.py`） | 新文件名 `<rev>_<slug>.py`，`down_revision` 接 head |
| SQLite 兼容 | 现有 S1 迁移里用的是 `op.batch_alter_table` | **改表一律 batch**，桌面版是 SQLite |
| 索引漂移 | `backend/tests/test_migrations.py::test_no_model_migration_drift` | 模型里 `index=True` 的列，迁移必须显式建索引（S1 被 `ix_solve_attempts_id` 抓过） |
| 降级 | 每个迁移都要写对称 `downgrade()` | 有测试 `test_branch_migrations_downgrade_and_reupgrade_on_sqlite` |

---

## C. 迁移清单（DDL 级别）

```sql
-- S2（review）  down_revision = f7b1c2d3e4a5
CREATE TABLE review_votes(
  id INTEGER PK, question_id INTEGER FK questions ON DELETE CASCADE,
  model_name VARCHAR(100) NOT NULL, provider_name VARCHAR(100),
  packet_hash CHAR(64) NOT NULL,           -- 票必须与它看到的题对得上
  verdict VARCHAR(16) NOT NULL,            -- same|diff|undecided
  answer_md TEXT, confidence FLOAT, agent_run_id VARCHAR(36) FK agent_runs,
  created_at DATETIME, created_by INTEGER FK user);
CREATE INDEX ix_review_votes_qid ON review_votes(question_id);
CREATE INDEX ix_review_votes_hash ON review_votes(packet_hash);

CREATE TABLE review_verdicts(
  id INTEGER PK, question_id INTEGER FK, round INTEGER NOT NULL,
  decision VARCHAR(16) NOT NULL,           -- consistent|suspect|undecided|adjudicated
  judge_model VARCHAR(100), judge_provider VARCHAR(100),
  canonical_answer TEXT, rationale TEXT, agent_run_id VARCHAR(36),
  created_at DATETIME,
  UNIQUE(question_id, round));
CREATE INDEX ix_review_verdicts_qid ON review_verdicts(question_id);

CREATE TABLE disputes(
  id INTEGER PK, question_id INTEGER FK, topic VARCHAR(24) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'open',   -- open|resolved|wontfix
  evidence JSON, resolution TEXT,
  resolved_by INTEGER FK user, resolved_at DATETIME, created_at DATETIME);
CREATE INDEX ix_disputes_status ON disputes(status);

-- S3（figure + decompose）
CREATE TABLE question_figure(
  question_id INTEGER PK FK questions ON DELETE CASCADE,
  status VARCHAR(16) NOT NULL,             -- none|placeholder|filled
  mode VARCHAR(8) NOT NULL,                -- lite|full
  kind VARCHAR(32), necessity VARCHAR(16), necessity_note TEXT,
  figure_only JSON, not_to_scale BOOLEAN, answer_branches INTEGER, branches_note TEXT,
  entities JSON, relations JSON,           -- full 模式预留，省一次迁移
  review_who VARCHAR(64), reviewed_at DATE, confidence FLOAT, uncertain TEXT,
  writer VARCHAR(32), stamped_at DATETIME);

CREATE TABLE question_decompose(
  id INTEGER PK, question_id INTEGER UNIQUE FK questions ON DELETE CASCADE,
  status VARCHAR(16), version INTEGER NOT NULL DEFAULT 1,
  chapter VARCHAR(64), topic VARCHAR(255), topic_id VARCHAR(32),
  writer VARCHAR(32), stamped_at DATETIME);

CREATE TABLE decompose_conditions(
  id INTEGER PK, decompose_id INTEGER FK question_decompose ON DELETE CASCADE,
  cid VARCHAR(8) NOT NULL, text TEXT NOT NULL,
  kind VARCHAR(16) NOT NULL, role VARCHAR(16) NOT NULL, source VARCHAR(16) NOT NULL,
  depends_on JSON, provides JSON, essential BOOLEAN, note TEXT,
  removable_effect TEXT, seq INTEGER NOT NULL,
  UNIQUE(decompose_id, cid),
  CHECK(kind IN ('given','implicit','from_figure','derived')),
  CHECK(role IN ('defines','constrains','quantifies','constructs','qualifies')),
  CHECK(source IN ('stem','figure','both','notation')));

CREATE TABLE decompose_asks(
  id INTEGER PK, decompose_id INTEGER FK ON DELETE CASCADE,
  aid VARCHAR(8) NOT NULL, text TEXT NOT NULL, answer_type VARCHAR(16) NOT NULL,
  depends_on JSON, figure_dependent BOOLEAN, note TEXT, seq INTEGER NOT NULL,
  UNIQUE(decompose_id, aid),
  CHECK(answer_type IN ('number','expression','range','point','value','proof','set','other')));

CREATE TABLE decompose_slots(
  id INTEGER PK, decompose_id INTEGER FK ON DELETE CASCADE,
  sid VARCHAR(8) NOT NULL, name VARCHAR(255), type VARCHAR(16) NOT NULL DEFAULT 'text',
  current TEXT, "range" TEXT, effect TEXT, provides JSON, seq INTEGER NOT NULL,
  UNIQUE(decompose_id, sid));

CREATE TABLE decompose_entities(id INTEGER PK, decompose_id INTEGER FK ON DELETE CASCADE,
  name VARCHAR(255) NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE decompose_methods(id INTEGER PK, decompose_id INTEGER FK ON DELETE CASCADE,
  mid VARCHAR(8), name VARCHAR(255), uses JSON, yields JSON, seq INTEGER NOT NULL);

-- S4（metrics / variant / gates / acceptance）
CREATE TABLE question_metrics(
  question_id INTEGER PK FK questions ON DELETE CASCADE,
  units_total INTEGER, points_total INTEGER, density FLOAT, kind_variety INTEGER,
  thinking_score FLOAT, level VARCHAR(8),   -- 易|中|难，writer 只有人
  measured BOOLEAN NOT NULL DEFAULT 0, computed_at DATETIME);

CREATE TABLE variant_verifications(
  id INTEGER PK, variant_id INTEGER FK questions ON DELETE CASCADE,
  round INTEGER NOT NULL, model_name VARCHAR(100), answer_md TEXT,
  verdict VARCHAR(16), agent_run_id VARCHAR(36), created_at DATETIME,
  UNIQUE(variant_id, round));

CREATE TABLE gate_runs(id INTEGER PK, git_ref VARCHAR(64), started_at DATETIME,
  finished_at DATETIME, total INTEGER, failed INTEGER, meta_ok BOOLEAN);
CREATE TABLE gate_findings(id INTEGER PK, run_id INTEGER FK gate_runs ON DELETE CASCADE,
  gate VARCHAR(64), level VARCHAR(8), subject_ref VARCHAR(64), message TEXT, fix_hint TEXT);

CREATE TABLE foundation_stamps(id INTEGER PK, scope VARCHAR(32) NOT NULL,
  fingerprint CHAR(16), dirty_count INTEGER, note TEXT, stamped_at DATETIME);
CREATE INDEX ix_foundation_stamps_scope ON foundation_stamps(scope);

CREATE TABLE acceptances(id INTEGER PK, question_id INTEGER FK ON DELETE CASCADE,
  by_user_id INTEGER FK user, by_name VARCHAR(64), passed BOOLEAN NOT NULL,
  note TEXT, accepted_at DATETIME);
CREATE INDEX ix_acceptances_qid ON acceptances(question_id);
```

> 另需一条 **additive** 迁移给 `questions` 加 `external_ref VARCHAR(16)` + `UNIQUE(subject_id, external_ref)`：
> 没有它，平台的 int 主键和我们的 `q0109` 没法对账。**这是唯一一处改 `questions` 表**，且只加列。

---

## D. 工具签名（MCP，字段级）

已落地（S1）：

```jsonc
// queue_agent_job
{ "kind": "solve|review|judge|decompose|variant|measure",   // 必填
  "question_id": 123,                                        // solve 必填
  "round": 1,                                                // solve 必填，>=1
  "models": ["cn:kimi-k3-1"],                                // review/judge 必填
  "payload": {},                                             // 透传给 handler
  "priority": 0, "dedupe": true }

// get_agent_job
{ "job_id": 456 }
```

待加（S2/S3），签名先定死，免得以后改协议：

```jsonc
// submit_blind_vote：一票一行，packet_hash 用来证明「票与它看到的题对得上」
{ "question_id": 123, "model": "cn:kimi-k3-1", "verdict": "same|diff|undecided",
  "answer_md": "…", "confidence": 0.9, "packet_hash": "…64 hex…" }

// get_question_semantics：一次取全 figure + decompose + metrics（模型不必连打五个工具）
{ "question_id": 123, "include": ["figure","decompose","metrics","answer_state"] }

// upsert_figure / upsert_decompose：整块替换，带 expected_version 做乐观锁
{ "question_id": 123, "payload": { … }, "expected_version": 1 }

// open_dispute / resolve_dispute：关闭必须带证据
{ "question_id": 123, "topic": "answer", "evidence": {…} }
{ "dispute_id": 9, "resolution": "…", "evidence": {…} }

// list_pending_jobs：给 DSH 轮询
{ "kind": "review", "limit": 20 }
```

---

## E. 会红的既有测试 / 要同步改的点

| 触发条件 | 会红在哪 | 怎么改 |
|---|---|---|
| 加 AI 工具 | `tests/test_ai_tools.py:60` 精确集合 | 把新工具名加进那个 `assert names == {…}` |
| 加 capability | `tests/test_mcp.py` 全量（`KeyError: '<cap>'`） | 在 `CAPABILITY_OUTPUT_TYPES` 登记输出类型 |
| 加模型字段 | `tests/test_migrations.py::test_no_model_migration_drift` | 迁移里补对应列/索引（`index=True` 要显式建） |
| 改 `questions` 表 | 同上 + `test_branch_migrations_downgrade_and_reupgrade_on_sqlite` | 用 `batch_alter_table`，写对称 `downgrade` |
| 新迁移 head | `alembic heads` 出现多个 head 就报错 | `down_revision` 指向旧 head，别并行开两条链 |
| 加 capability 但漏权限 | `tests/test_capability_registry.py` | 必须带 `permission`（不许进 `UNGATED_ALLOWLIST`） |

---

## F. 三处还需实测确认（写代码前先跑一遍）

1. **`\( \)` → `$ $` 的转换是否 100% 无损**：`markdown_to_rich_doc` 用 `dollarmath(double_inline=True)`；我们全库是 `\(`。抽 20 题转一遍，比对 `rich_doc_to_plain_text` 与原 `stem_text` 的相似度，再决定「先转还是直写 inlineMath 节点」。
2. **`knowledge_points.slug` 没有唯一约束**（实测 `app/models/knowledge_points` 只有 `index=True`，无 `UniqueConstraint`）→ 我们按 `ch04-t02` 这种**全局唯一**写法入库，但要在 `knowledge_point.create` 能力里自己查重，否则同一 slug 会重复插入。
3. **图片迁移路径**：`media_service.content_url(asset_id)` 生成 `/api/v1/media/{id}/content`，而我们的 `stem_md` 里是 `images/p43_….jpg`。要确认「上传→拿 asset_id→改写 md→转 RichDoc」这条链在导入时能一次走完（它的 `import_tasks` 已经做类似事，可直接复用而不是另写）。
