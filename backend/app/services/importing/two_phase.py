"""两阶段抽取：AI 只判答案，题干由原文确定性切分。

动机（实测依据）
----------------
1.6.0 的 `AIExtractor` 把整篇原文丢给模型，要求它回写完整题干。实测 442K 字符
/786 题的书，模型只出 15 题（`finish=stop`，不是截断）；把 `max_tokens` 提到
131072 仍是 16 题；用强约束提示词要求"必须 786 题"，模型直接拒绝并回
"原文过长，请分批"。

根因不是上下文，是**输出体积**：题干逐字照抄等于把整个文件再输出一遍。同一份
原文、同一次调用，只把输出改成 `{ord, answer, q_type}`（不回写题干），784→786 题
100% 穷举，78 秒完成。

而且题干由 AI 回写本来就有风险：实测它会改写 `\\(...\\)`→`$...$`、
`\\left( {{30}+{2m}}\\right)`→`(30+2m)`、`,若`→`，若`。改成从原文切片后，
题干在结构上不可能被改写。

设计
----
阶段 1（AI，整篇一次调用）：输出 `{"items":[{"ord":1,"answer":"A"}]}`，
`ord` 是**出现顺序**（1..N），不是原文题号——教辅书每章从 1 重新编号，题号会重复
786 次，不可用于对齐。

阶段 2（纯脚本）：
- 按行首题号切段
- 剥选项块 → `options`
- 剥内联答案 `(B)` / `( c )` / `故选B` → 脚本答案（比 AI 可靠）
- 脚本答案优先，缺失时用 AI 的
- `q_type` 由结构推断（AI 判题型实测不可靠：786 题里只判出 16 个选择题，
  而原文有 148 个带选项块的题）
- 图片引用原样保留

阶段 1 有两种模式（提示词不同，其余共用）：

- `extract`：AI 只提取原文**已有**答案，没有就留空。绝不自己解题——自己算出来的
  答案一旦有错就会被当成原文答案入库。
- `solve`：AI 逐题求解，产出 answer/thinking/analysis/summary/difficulty。
  **分批**（每批 `SOLVE_BATCH_SIZE` 题）：实测整篇一次调用给 139 张图、56 个页码
  的教辅只出 16 题，内容停在第一章；单题「答案+思路+详解+小结」约 4,000 字符，
  模型写完十几题就 stop，后面整章静默消失，而任务状态仍是 completed。

两种模式的共同点：**题干永远从原文切**，所以题干不可能被改写，原文里的图片占位符
也不会像 AI 回写时那样被整批丢掉（实测一次丢 139/139）。

降级：题数少于 `MIN_Q`、或 AI 调用失败、或 AI 返回条目数与切段数不符时，
退回原始 `AIExtractor`，行为与未打补丁时一致。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai_provider import get_ai_provider
from app.services.importing.contracts import ExtractionResult
from app.services.importing.enrich import KnowledgePointEnricher

logger = logging.getLogger(__name__)

# 少于这个题数不值得走两阶段（小文件用原路径更稳）
MIN_Q = 20

# 答案阶段重试退避（秒）。整本请求体积大，上游偶发 502。
RETRY_DELAYS = (0, 20, 60)

# 答案阶段每批题数。实测单次 227K token 的整本请求上游会概率性 502，
# 拆成每批 150 题（约 40K token）后稳定；对外仍是一次导入，不需要用户干预。
BATCH_SIZE = 100000  # 不分批：整本一次调用（用户要求，上游限制靠切 cn: 前缀解决）

# solve 阶段每批题数。单题「答案+思路+详解+小结」实测约 4,000 字符；一次调用只给
# 6 题（约 24K 字符输出）留足余量。整篇一次调用实测只出 16 题：不是上下文不够，
# 是输出体积撞上限，且模型是把前几题写满后 stop，后面整章静默消失。
SOLVE_BATCH_SIZE = 6

# 行首题号：1. / 1． / 1、 / 1) / 1）
# 必须排除小数（3.14 不是第 3 题）：点号后不能紧跟数字。
# 必须排除解析里的 1） 这种：题号后要么是空白，要么紧接中文/字母（不能是"先化简"这类动词短语的判断交给
# 上层的 _split_stem_analysis 截断，这里只保证不把小数当成题号）。
NUM_RE = re.compile(r"^\s*(\d{1,3})\s*(?:[.．](?!\d)|、|[)）])\s*")

# 出现这些标签即认为题目正文已结束（避免把解析里的 1）2）当成新题）
SOLUTION_TAG_RE = re.compile(r"^\s*(?:解\s*[:：]|【\s*(?:解析|分析|详解|点睛|答案|考点|点评|小结|思路|解答)\s*】)")

# 选项块起点：A. / A、 / A) / A） / （A） / (A)
OPT_LABEL = r"[.．、)）]"
OPT_START_RE = re.compile(r"(?:^|\n)[ \t]*(?:[（(]\s*[A-D]\s*[)）]|[A-D]\s*" + OPT_LABEL + r")")
OPT_LETTER_RE = re.compile(r"(?:^|\s|\n)(?:[（(]\s*([A-D])\s*[)）]|([A-D])\s*" + OPT_LABEL + r")")

def _letters_in(text: str) -> List[str]:
    """从文本里抽出选项字母，兼容 A. / A、 / A) / A） / （A） 五种写法。"""
    out = []
    for m in OPT_LETTER_RE.finditer(text):
        lab = m.group(1) or m.group(2)
        if lab:
            out.append(lab.upper())
    return out


# 题干末尾内联答案：(B) / （B） / ( c ) / （ABD） 多选
INLINE_ANS_RE = re.compile(r"[\s，,、]?[（(]\s*([A-Da-d]{1,4})\s*[)）]\s*[。．.]?\s*$")
# 故选 B / 答案：B
TAIL_ANS_RE = re.compile(r"[\s，,、]?(?:故选|答案是|答案[：:])\s*([A-Da-d])\s*[。．.]?\s*$")
# 填空占位
BLANK_RE = re.compile(r"_{2,}|＿{2,}|（\s*）|\(\s*\)")

SYS_PROMPT = (
    "你是题目答案判定器。只输出 JSON，不要思考过程，不要 code fence，"
    "不要输出题干或解析。"
)

USER_PROMPT = """## 关键前提
原文是教辅书，每条题目前导是"数字."，共 {n} 条。注意原文题号每章从 1 重新开始，
所以题号会重复，**必须按出现顺序编号**，而不是按原文数字。

## 任务
按出现顺序（第1条、第2条……第 {n} 条）逐条**提取原文已经给出的答案**。

## 硬性要求
1. 输出必须正好 {n} 项，一项不漏，顺序与原文完全一致。
2. 不要回写题干，不要输出解析——题干由系统另取。
3. **只提取原文明确写出的答案，不要自己解题。** 原文没给答案就写空字符串 ""。
   不要推算、不要猜测、不要顺手做出来 —— 你产出的答案会被当成原文答案入库，
   自己算出来的答案一旦有错就会污染数据，比留空严重得多。
4. 答案来源：题干末尾或选项后有 `(B)` / `（B）` / `（ABD）` 就取该字母；填空看原文是否
   给出结果；解答看原文是否有"答案：/故/所以…=…"这类明确结论。
5. 禁止以"原文过长"为由拒绝或省略——你只输出答案，输出体积很小。

## 输出格式
{{"items":[{{"ord":1,"answer":"A"}},{{"ord":2,"answer":""}}]}}

## 原文
"""

SOLVE_SYS_PROMPT = (
    "你是资深数学教师，负责逐题求解。只输出 JSON，不要 code fence，"
    "不要输出题干原文。"
)

SOLVE_USER_PROMPT = r"""## 关键前提
下面是从教辅书里按出现顺序切出的 {n} 道题，每题以 `### [i]` 开头。
题干、选项、图片占位符都由系统另行从原文取，**你不需要回写题干**。

## 任务
逐题求解，产出：答案、思路、详解、小结、难度。

## 硬性要求
1. 输出正好 {n} 项，`ord` 必须与 `### [i]` 的编号一致；不许重排、合并或跳过。
2. **不要输出题干**，也不要重复题目原文里的句子。
3. 答案只写最终结果：选择题写字母（多选如 `ABD`），填空题每个空一个表达式，
   解答题写完整结论。
4. 公式一律 LaTeX：行内 `$...$`，分式优先 `\dfrac`。
   反斜杠只写一个，例如 `$\sqrt{2}$`、`$\dfrac{1}{2}$`。
5. 原文若自带解析，可以参考但必须自己验算；与原文冲突时以验算结果为准。

## 输出格式
{"items":[{"ord":1,"answer":"A","thinking":"...","analysis":"...","summary":"..."}]}

## 题目
"""


# 整卷/材料题的信号：题组标签、材料标签、大分值、交卷标记
PAPER_SIGNAL_RE = re.compile(
    r"【\s*(?:题目材料|题组|题组结束)\s*】|"
    r"^\s*(?:第[一二三四五六七八九十]+[、.．]|[一二三四五六七八九十]+、)\s*$|"
    # 不能加 \b：中文词内全是 word 字符，"本卷满分" 里 卷/满 之间不存在边界，
    # 加了 \b 会漏掉前面带字的写法。
    r"(?:满分|共\s*\d+\s*分|考试时间)",
    re.M,
)


def _looks_like_paper(markdown: str) -> bool:
    """粗判是不是整卷/含材料题。命中就走原路径，保住 paper/stimuli/question_groups。"""
    return bool(PAPER_SIGNAL_RE.search(markdown))


# 括号型题号（1） / 1)）：在解析区里通常是步骤编号，不当成新题
PAREN_NUM_RE = re.compile(r"^\s*\d{1,3}\s*[)）]\s*")

# --------------------------------------------------------------------------- #
# 表格内题目展开
#
# 教辅的"方法归纳"表格常把配套练习塞进单元格，用 <br> 分隔：
#   | 类型一 完全平方式 1. … 2. … | 1. 若关于 x 的多项式… 2. 若…求… 的值 |
# 这些题号不是行首，NUM_RE 永远匹配不到，于是整张表跟着上一个题号走，
# 变成一道几千字符的"题"。实测一份 415 段的材料里，4 道题的题干内藏了
# 54 道独立题目（最长题干 6,675 字符，89% 是表格）。
#
# 处理方式：把命中的单元格片段从表格里摘出来，按原位置紧跟表格输出为独立
# 段落；表格剩余部分（方法、图、公式）留在原处。不改变原文措辞。
# --------------------------------------------------------------------------- #
# 折行的表格行不以 `|` 结尾（OCR/导出常见），只要求以 `|` 开头
TABLE_ROW_RE = re.compile(r"^\s*\|")

# 片段起点：题号 / 教材母题 / 教材变式 / 练透 / 类型X / 【标签】
ITEM_START_RE = re.compile(
    r"^\s*(?:\d{1,3}\s*[.．、]|【[^】]{1,12}】|教材母题|教材变式|练透\s*\d+|类型[一二三四五六七八九十]+)"
)

# 片段必须像一道题：有空位、有提问动词，且不是纯标题
ITEM_ASK_RE = re.compile(
    r"\\_\\_\\_|___|＿{2,}|（\s*）|\(\s*\)|求证|求|解方程|解不等式|取值范围|大小关系|画出|证明|说明理由|填写"
)


def _looks_like_table_item(fragment: str) -> bool:
    """单元格里的一段文字是否构成一道独立题目。"""
    text = fragment.strip()
    if len(text) < 20:
        return False
    if not ITEM_START_RE.match(text):
        return False
    # "类型一 完全平方式" 这类纯标题不算题
    if not ITEM_ASK_RE.search(text):
        return False
    return True


def expand_table_items(markdown: str) -> Tuple[str, int]:
    """把表格单元格里被 <br> 分隔的题目摘出来，按原位置输出为独立段。

    返回 (改写后的 markdown, 摘出的题目数)。

    设计要点（都是踩过的坑）：
    1. 单元格里的分类标签（"类型一 完全平方式"、"类型四 线段和最值——将军饮马"）
       必须跟着它下面的题目一起走，否则会像以前那样被静默丢掉。
    2. 摘走题目后变空的行要整行删除，否则会在题干里留下 `|  |  |` 噪声，而且
       残留的空表会让 `_split_trailing_table` 把真正的题目数据表误判成"尾表"
       剥进解析（实测导致题干里的表格字符全变成 0，题目写着"观察表格"却没有表）。
    3. 数据表（点数表、函数值表）里没有 `<br>` 分隔的题目，原样保留。
    """
    lines = markdown.split("\n")
    out: List[str] = []
    pending: List[Tuple[str, str]] = []   # (分类标签, 题目文本)
    in_table = False
    extracted_total = 0

    def flush_items() -> None:
        nonlocal pending, extracted_total
        if not pending:
            return
        extracted_total += len(pending)
        out.append("")
        for idx, (label, item) in enumerate(pending, start=1):
            text = item.strip()
            if label:
                text = "%s（%s）" % (text, label)
            if not re.match(r"^\s*\d{1,3}\s*[.．、]", text):
                text = "%d. %s" % (idx, text)
            out.append(text)
        out.append("")
        pending = []

    for line in lines:
        if not TABLE_ROW_RE.match(line):
            if in_table:
                in_table = False
                flush_items()
            out.append(line)
            continue

        in_table = True
        # 分隔行（| --- | --- |）必须原样保留：下游用"有没有分隔行"来判断一个表
        # 是完整表格还是被切断的残块，删掉它会让完整表格被当成残块剥进解析。
        if re.match(r"^\s*\|[\s:\-|]*\|\s*$", line):
            out.append(line)
            continue

        cells = line.split("|")
        new_cells: List[str] = []
        row_label = ""
        row_items: List[str] = []
        for cell in cells:
            parts = cell.split("<br>")
            keep: List[str] = []
            for part in parts:
                text = part.strip()
                if _looks_like_table_item(text):
                    row_items.append(text)
                elif text and ITEM_START_RE.match(text) and len(text) <= 30:
                    # 分类标题，挂到本行的题目上
                    row_label = (row_label + " " + text).strip() if row_label else text
                else:
                    keep.append(part)
            new_cells.append("<br>".join(keep))

        if row_items:
            for it in row_items:
                pending.append((row_label, it))
            # 剩下的若不是空壳就保留（可能是例题讲解、图）
            joined = "|".join(new_cells)
            rest = joined.replace("|", "").replace("-", "").replace("<br>", "").strip()
            if rest:
                out.append(joined)
        else:
            out.append(line)

    if in_table:
        flush_items()

    # 收尾：题目被摘走后可能只剩分隔行/空壳的表格块，整块丢掉。
    # 留着会在题干里变成一行 `| --- | --- |`，然后被下游当成"表格残块"剥走。
    cleaned: List[str] = []
    block: List[str] = []

    def flush_block() -> None:
        if not block:
            return
        has_content = any(
            re.sub(r"[\s|:\-]", "", ln) for ln in block
        )
        if has_content:
            cleaned.extend(block)
        block.clear()

    for line in out:
        if TABLE_ROW_RE.match(line):
            block.append(line)
        else:
            flush_block()
            cleaned.append(line)
    flush_block()

    return "\n".join(cleaned), extracted_total


def split_segments(markdown: str) -> List[List[str]]:
    """按行首题号把整篇原文切成若干题的原始行块（保留原行，便于后续逐字取用）。

    解析区内的 `1）` `2）` 是步骤编号，不是新题——进入解析后只有 `数字.` / `数字．` /
    `数字、` 才可能是新题。
    """
    lines = markdown.split("\n")
    starts: List[int] = []
    in_solution = False
    for i, line in enumerate(lines):
        if SOLUTION_TAG_RE.match(line):
            in_solution = True
            continue
        if not NUM_RE.match(line):
            continue
        if in_solution and PAREN_NUM_RE.match(line):
            continue          # 解析里的步骤编号
        starts.append(i)
        in_solution = False   # 出现真正的新题，退出解析区
    if not starts:
        return []
    segments: List[List[str]] = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        block = list(lines[start:end])
        block[0] = NUM_RE.sub("", block[0], count=1)
        segments.append(block)
    return segments


def _split_options(body: str) -> Tuple[str, str]:
    """把题块拆成 (题干, 选项块)。选项块必须含至少两个不同字母，否则不算。"""
    m = OPT_START_RE.search(body)
    if not m:
        return body, ""
    candidate = body[m.start():].lstrip("\n")
    # 选项块到解析标签为止：否则【解析】里的 "A. 错误 B. 正确" 会被当成新选项
    lines = candidate.split("\n")
    keep = []
    for ln in lines:
        if SOLUTION_TAG_RE.match(ln):
            break
        keep.append(ln)
    candidate = "\n".join(keep).strip()
    if len(set(_letters_in(candidate))) >= 2:
        return body[: m.start()].rstrip(), candidate
    return body, ""


def _options_to_list(options_block: str) -> List[str]:
    """把 'A. … B. … C. …' 拆成 ['A. …', 'B. …']，兼容 A) （A） 等写法。"""
    if not options_block:
        return []
    parts = re.split(r"(?:^|\s|\n)(?=(?:[（(]\s*[A-D]\s*[)）]|[A-D]\s*" + OPT_LABEL + r"))", options_block)
    out: List[str] = []
    seen = set()
    for p in parts:
        p = p.strip()
        if not (p and re.match(r"^(?:[（(]\s*[A-D]\s*[)）]|[A-D]\s*" + OPT_LABEL + r")", p)):
            continue
        lab = (_letters_in(p[:4]) or [""])[0]
        if not lab or lab in seen:
            continue
        # 同一段里若还嵌着下一个选项（"A. 甲 B. 乙"），在此截断
        inner = re.search(r"\s(?:[（(]\s*[A-D]\s*[)）]|[A-D]\s*" + OPT_LABEL + r")", p[1:])
        if inner:
            p = p[: inner.start() + 1].rstrip()
        out.append(p)
        seen.add(lab)
    letters = [(_letters_in(o[:4]) or [""])[0] for o in out]
    expect = [chr(65 + i) for i in range(len(letters))]
    return out if letters == expect else []


def _strip_inline_answer(stem: str) -> Tuple[str, str]:
    """从题干末尾剥出内联答案，返回 (干净题干, 答案字母)。"""
    for pat in (INLINE_ANS_RE, TAIL_ANS_RE):
        m = pat.search(stem)
        if m:
            return stem[: m.start()].rstrip(), m.group(1).upper()
    return stem, ""


def _sanitize_answer(q_type: str, answer: Any, options: List[str]) -> Any:
    """答案自检：解析不了就清空，绝不因为答案格式丢掉整道题。

    实测 786 题里有 7 道栽在这里，官方 `convert_answer` 会直接抛
    `LegacyQuestionError`，整题被 skip：
    - 选择题答案给了数字或结论文本（`3`、`y1<y3<y2`），抽不到选项字母
    - 填空答案含 `>`，被 markdown 当成引用块解析成空
    """
    if not answer:
        return answer

    if q_type in ("single_choice", "multiple_choice", "option_matching"):
        n = len(options)
        # 只接受落在选项范围内的字母
        letters = [c.upper() for c in re.findall(r"[A-Da-d]", str(answer))]
        letters = [c for c in letters if ord(c) - 65 < n]
        if not letters:
            # 退一步：用答案文本去匹配选项正文
            for i, opt in enumerate(options):
                text = re.sub(r"^\s*[A-Za-z]\s*[.．、)）]\s*", "", opt).strip()
                if len(text) >= 2 and text in str(answer):
                    letters.append(chr(65 + i))
        if not letters:
            return ""
        uniq = sorted(set(letters))
        return uniq[0] if q_type == "single_choice" else "".join(uniq)

    if q_type == "fill_in_the_blank":
        # 允许字符串或二维数组；元素做 markdown 安全转义，避免 `>` `<` `*` 被解析成结构
        def safe(v: Any) -> str:
            s = "" if v is None else str(v).strip()
            return re.sub(r"([<>*_`#~|])", r"\\\1", s) if s else ""

        if isinstance(answer, list):
            groups = []
            for g in answer:
                items = g if isinstance(g, list) else [g]
                cleaned = [safe(x) for x in items if safe(x)]
                groups.append(cleaned)
            return [g for g in groups if g] or ""
        return [[safe(answer)]] if safe(answer) else ""

    return answer


def _infer_q_type(stem: str, options: List[str], answer: str) -> str:
    """由结构推断题型。实测 AI 判题型不可靠，这里确定性判断。"""
    if options:
        letters = re.sub(r"[^A-Za-z]", "", answer or "")
        return "multiple_choice" if len(letters) > 1 else "single_choice"
    if BLANK_RE.search(stem):
        return "fill_in_the_blank"
    if re.search(r"_{2,}|＿{2,}", stem):
        return "fill_in_the_blank"
    return "free_response"


# 题干与「非题干内容」的分界：命中即认为题干到此结束
# 实测 442K 字符的教辅里，两题号之间混着 564 个 ## 标题、344 段 `解:` 解析、33 个【点睛】栏目
BOUNDARY_PATTERNS = (
    re.compile(r"^\s*解\s*[:：]"),                              # 解:(1)…
    re.compile(r"^\s*【\s*(?:解析|分析|详解|点睛|答案|考点|点评|小结|思路|解答)\s*】"),
    re.compile(r"^\s*(?:解析|分析|解答|点评|点睛|答案)\s*[:：]"),
    re.compile(r"^\s*#{1,6}\s*"),                               # 章节标题
    re.compile(r"^\s*\d{1,2}\s*[.．]\s*\d{1,2}\s+\S"),          # 25.2 降次——…（OCR 丢了 ### 的残句）
)

# 纯章节标题行（整行丢弃，不属于任何题目）
HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s*")
SECTION_TAIL_RE = re.compile(r"^\s*\d{1,2}\s*[.．]\s*\d{1,2}\s+\S.{0,40}$")


TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}")


def _split_trailing_table(text: str) -> Tuple[str, str]:
    """把题干末尾「不完整的表格残块」剥出去。

    实测 id=158 之类的题，末尾挂着 `|抛物线|开口方向|对称轴|` —— 那是上一节归纳
    表被 OCR 切断后粘到题干上的，既不属于这道题，也会让检索命中率变差。
    判据：从第一条 `|` 行到结尾，如果整块里没有 `| --- |` 分隔行，就是残块。
    """
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.strip().startswith("|")), None)
    if first is None:
        return text, ""
    block = lines[first:]
    if any(TABLE_SEP_RE.match(ln) for ln in block):
        return text, ""            # 是完整表格，属于题干，保留
    return "\n".join(lines[:first]).rstrip(), "\n".join(block).strip()


def _split_stem_analysis(body: str) -> Tuple[str, str]:
    """把题块拆成 (题干, 解析)。

    原文一道题后面常跟着完整解答（`解:` 起）与栏目（`【点睛】`），以前这些全被
    当成题干吞进去，实测最长一题被撑到 75,084 字符。
    """
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if any(p.match(line) for p in BOUNDARY_PATTERNS):
            stem = "\n".join(lines[:i]).rstrip()
            analysis = "\n".join(lines[i:]).strip()
            return stem, analysis
    # 末尾残留的章节残句（如 "25.2 降次——解一元二次方"）也剥掉
    tail = len(lines) - 1
    while tail >= 0 and not lines[tail].strip():
        tail -= 1
    if tail > 0 and SECTION_TAIL_RE.match(lines[tail]):
        return "\n".join(lines[:tail]).rstrip(), lines[tail].strip()
    return body, ""


def _strip_headings(markdown: str) -> str:
    """删掉整行章节标题。标题是排版结构，不属于任何一道题的题干。"""
    kept = [ln for ln in markdown.split("\n") if not HEADING_LINE_RE.match(ln)]
    return "\n".join(kept)


def _upgrade_frac(text: str) -> str:
    r"""`\frac` → `\dfrac`，但上下标内的分式保留 `\frac`。

    用户要求：行内公式不缩小，分式按教材满尺寸排版；只有指数/下标里的分式才用
    `\frac`，否则 `\dfrac` 会撑坏行距。
    """
    if "\\frac" not in text:
        return text

    # 找出所有 ^{...} / _{...} 的字符区间，落在里面的 \frac 不动
    protected: List[Tuple[int, int]] = []
    for m in re.finditer(r"[\^_]\s*\{", text):
        depth = 0
        for i in range(m.end() - 1, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    protected.append((m.end() - 1, i))
                    break

    def in_script(pos: int) -> bool:
        return any(a <= pos <= b for a, b in protected)

    # \dfrac 不会命中：正则要求反斜杠后紧跟 frac
    hits = [(m.start(), m.end()) for m in re.finditer(r"\\frac(?![A-Za-z])", text)
            if not in_script(m.start())]
    if not hits:
        return text
    out = text
    for start, end in reversed(hits):
        out = out[:start] + "\\dfrac" + out[end:]
    return out


def _escape_blanks(stem: str) -> str:
    """转义连续下划线。

    Markdown 会把 `___4___` 解析成 bold+italic，下划线本身丢失（实测 786 题里 7 题
    因此失真）。填空横线是题目内容的一部分，必须用 `\\_` 转义保留为字面字符。
    """
    return re.sub(r"_{2,}", lambda m: "\\_" * len(m.group(0)), stem)


def _infer_difficulty(stem: str, options: List[str], q_type: str) -> int:
    """按文本特征估难度 1–5。

    官方 difficulty 只有 1–5 的档位，且接口不传会比全写成 1 更糟（筛选失效）。
    这里只用可从文本确定性算出的量，不调用 AI：
    - 字数、行内公式数、是否多小问、是否含证明/探究类动词。
    精确难度仍需人工或另跑模型，此值作为可用下限。
    """
    plain = re.sub(r"\s+", "", stem)
    n_chars = len(plain)
    n_math = stem.count("$") // 2
    score = 1
    if n_chars > 120:
        score += 1
    if n_chars > 260:
        score += 1
    if n_math > 4:
        score += 1
    if re.search(r"证明|求证|探究|综合|分类讨论|是否存在", stem):
        score += 1
    if re.search(r"[（(]\s*1\s*[)）][\s\S]*[（(]\s*2\s*[)）]", stem):
        score += 1
    return max(1, min(5, score))


def _clamp_difficulty(value: Any, fallback: int) -> int:
    """难度只接受 1–5：前端星级与筛选按 1–5 硬编码，越界值会显示异常或筛不出来。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return fallback
    if v < 1 or v > 5:
        logger.warning("difficulty out of range (%s), clamped", v)
        return min(max(v, 1), 5)
    return v


def build_questions(
    segments: List[List[str]],
    ai_items: List[Dict[str, Any]],
    *,
    solve: bool = False,
) -> List[Dict[str, Any]]:
    """把切段 + AI 结果组装成 legacy 形态的题目 dict（喂给 adapt_legacy_question）。

    solve=False：AI 只给 answer，解析从小节原文里切。
    solve=True：AI 给 answer + thinking/analysis/summary/difficulty，解析优先用 AI 的
    （原文解析作为兜底），题干仍然逐字来自原文，因此不可能被改写、图片占位符也不会丢。
    """
    by_ord: Dict[int, Dict[str, Any]] = {}
    for item in ai_items:
        try:
            by_ord[int(item.get("ord"))] = item
        except (TypeError, ValueError):
            continue

    questions: List[Dict[str, Any]] = []
    for idx, block in enumerate(segments, start=1):
        body = _strip_headings("\n".join(block)).strip()
        if not body:
            continue
        stem, opt_block = _split_options(body)
        options = _options_to_list(opt_block)
        # 题干到此为止：后面是 解: / 【点睛】 / 章节标题，归入 analysis
        stem, table_tail = _split_trailing_table(stem)
        stem, analysis = _split_stem_analysis(stem)
        if table_tail:
            analysis = (analysis + "\n\n" + table_tail).strip() if analysis else table_tail
        stem, script_answer = _strip_inline_answer(stem)

        ai_item = by_ord.get(idx) or {}
        ai_answer = (ai_item.get("answer") or "").strip()
        # 脚本剥出的答案优先（确定性），AI 只补脚本剥不到的；
        # solve 模式下原文没有答案，AI 的答案就是唯一来源。
        answer = script_answer or ai_answer
        q_type = _infer_q_type(stem, options, answer)
        difficulty = _infer_difficulty(stem, options, q_type)
        stem = _escape_blanks(_upgrade_frac(stem))

        # 填空题先按分隔符拆成多解，再统一自检：
        # 官方对填空题要求二维数组（外层=空，内层=可接受答案），给字符串会抛
        # LegacyQuestionError；而含 `>` 之类的答案会被 markdown 解析成空。
        if q_type == "fill_in_the_blank" and answer:
            answer = [
                a.strip() for a in re.split(r"[；;]|或|,|，|、", str(answer)) if a.strip()
            ]
        answer_value = _sanitize_answer(q_type, answer, options)

        thinking = ""
        summary = ""
        if solve:
            thinking = _upgrade_frac((ai_item.get("thinking") or "").strip())
            summary = _upgrade_frac((ai_item.get("summary") or "").strip())
            ai_analysis = _upgrade_frac((ai_item.get("analysis") or "").strip())
            # 原文解析兜底，AI 解析优先：AI 是真正算过的那份
            analysis = ai_analysis or analysis
            # 难度**不**采用模型给的值：1.6.0 的两套提示词都让模型给 1–5，却没有给任何
            # 判据。实测 12 题全部落在 1–2（[2,1,1,2,1,2,1,2,2,2,2,2]），等于没有区分度，
            # 而越界值（实测出现过 6/7/8）会让前端星级和筛选失效。
            # 改用脚本按题干特征估计，并强制夹到 1–5。
            difficulty = _clamp_difficulty(difficulty, difficulty)

        questions.append(
            {
                "content": stem,
                "options": options,
                "answer": answer_value,
                "thinking": thinking,
                "analysis": _upgrade_frac(analysis),
                "summary": summary,
                "difficulty": difficulty,
                "q_type": q_type,
            }
        )
    return questions


class TwoPhaseExtractor:
    """AI 只负责答案/解析，题干由脚本从原文切。见模块 docstring 的实测依据。"""

    def __init__(self, enricher: Optional[KnowledgePointEnricher] = None) -> None:
        self.enricher = enricher or KnowledgePointEnricher()
        self._fallback = None  # 延迟构造，避免循环导入

    async def _ask_answers_batched(
        self, segments: List[List[str]], config: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """把题目分批问答案，返回带全局 ord 的结果。

        整本塞进一次请求（227K token）实测会被上游概率性 502；分批体积小、稳，
        而且对调用方仍然是一次导入，用户不需要干预。
        """
        collected: List[Dict[str, Any]] = []
        total = len(segments)
        for start in range(0, total, BATCH_SIZE):
            batch = segments[start : start + BATCH_SIZE]
            text = "\n".join("\n".join(block) for block in batch)
            items = await self._ask_answers(text, config, len(batch))
            for item in items:
                try:
                    item["ord"] = int(item.get("ord")) + start
                except (TypeError, ValueError):
                    continue
            collected.extend(items)
            logger.info(
                "two-phase batch %d-%d/%d answered (%d items)",
                start + 1, min(start + BATCH_SIZE, total), total, len(items),
            )
        return collected

    async def _ask_solutions_batched(
        self, segments: List[List[str]], config: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """分小批逐题求解，返回带全局 ord 的结果。

        为什么必须分批：整篇一次调用实测 139 张图 / 56 个页码的教辅只出 16 题，
        内容停在第一章——单题输出（答案+思路+详解+小结）约 4,000 字符，模型写完
        十几题就 stop，后面整章静默消失，而调用方看到的是 completed。
        """
        collected: List[Dict[str, Any]] = []
        total = len(segments)
        for start in range(0, total, SOLVE_BATCH_SIZE):
            batch = segments[start : start + SOLVE_BATCH_SIZE]
            text = "\n\n".join(
                "### [%d]\n%s" % (i + 1, "\n".join(block).strip())
                for i, block in enumerate(batch)
            )
            items = await self._ask_solutions(text, config, len(batch))
            for item in items:
                try:
                    item["ord"] = int(item.get("ord")) + start
                except (TypeError, ValueError):
                    continue
            collected.extend(items)
            logger.info(
                "two-phase solve batch %d-%d/%d solved (%d items)",
                start + 1, min(start + SOLVE_BATCH_SIZE, total), total, len(items),
            )
        return collected

    async def _ask_solutions(
        self, content: str, config: Dict[str, Any], n: int
    ) -> List[Dict[str, Any]]:
        """单批求解。批次小，重试代价低，但仍保留退避以对付上游偶发 502。"""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=config.get("API_KEY"), base_url=config.get("BASE_URL")
        )
        messages = [
            {"role": "system", "content": SOLVE_SYS_PROMPT},
            # 不能用 .format()：提示词里的 LaTeX（\dfrac{1}{2}）会被当成位置占位符。
            {"role": "user", "content": SOLVE_USER_PROMPT.replace("{n}", str(n)) + content},
        ]

        last_error: Exception | None = None
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            try:
                response = await client.chat.completions.create(
                    model=config.get("MODEL_NAME", "gpt-4o"),
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=65536,
                )
                choice = response.choices[0]
                if choice.finish_reason == "length":
                    raise RuntimeError("solve phase truncated (finish_reason=length)")
                payload = json.loads(choice.message.content or "")
                items = payload.get("items") or []
                if not items:
                    raise RuntimeError("solve phase returned empty items")
                return items
            except Exception as exc:  # noqa: BLE001 - 重试后再决定是否失败
                last_error = exc
                logger.warning(
                    "two-phase solve attempt %d/%d failed: %s",
                    attempt, len(RETRY_DELAYS), exc,
                )
                if attempt < len(RETRY_DELAYS):
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"solve phase failed after {len(RETRY_DELAYS)} attempts: {last_error}"
        )

    async def _ask_answers(
        self, content: str, config: Dict[str, Any], n: int
    ) -> List[Dict[str, Any]]:
        """整篇一次调用，只拿答案。带重试：大请求在上游容易偶发 502。"""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=config.get("API_KEY"), base_url=config.get("BASE_URL")
        )
        messages = [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": USER_PROMPT.format(n=n) + content},
        ]

        last_error: Exception | None = None
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            try:
                response = await client.chat.completions.create(
                    model=config.get("MODEL_NAME", "gpt-4o"),
                    messages=messages,
                    response_format={"type": "json_object"},
                    # 关键：不传 max_tokens 时上游默认额度会截断长输出；这里给足。
                    max_tokens=131072,
                )
                choice = response.choices[0]
                if choice.finish_reason == "length":
                    raise RuntimeError("answer phase truncated (finish_reason=length)")
                raw = choice.message.content or ""
                payload = json.loads(raw)
                items = payload.get("items") or payload.get("answers") or []
                if isinstance(items, list) and items and isinstance(items[0], str):
                    items = [{"ord": i + 1, "answer": a} for i, a in enumerate(items)]
                if not items:
                    raise RuntimeError("answer phase returned empty items")
                return items
            except Exception as exc:  # noqa: BLE001 - 重试后再决定是否失败
                last_error = exc
                logger.warning(
                    "two-phase answer attempt %d/%d failed: %s",
                    attempt, len(RETRY_DELAYS), exc,
                )
                if attempt < len(RETRY_DELAYS):
                    await asyncio.sleep(delay)
        raise RuntimeError(f"answer phase failed after {len(RETRY_DELAYS)} attempts: {last_error}")

    async def extract(
        self,
        content: str,
        db: AsyncSession,
        *,
        image_data: bytes | None = None,
        filename: str | None = None,
        mode: str = "extract",
        subject_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> ExtractionResult:
        # 图片整页 OCR 路径不走两阶段（没有可切分的 markdown）
        if image_data:
            return await self._fallback_extract(
                content, db, image_data=image_data, filename=filename,
                mode=mode, subject_id=subject_id, user_id=user_id,
            )

        # 两种模式都走两阶段，但提示词不同：
        #   extract → AI 只提取原文已有答案，没有就留空（绝不自己解题）
        #   solve   → AI 逐题求解，产出 answer/thinking/analysis/summary/difficulty
        # 共同点是题干永远从原文切，所以不可能被改写、图片占位符也不会丢。
        if mode not in ("extract", "solve"):
            logger.info("two-phase skipped: unsupported mode=%s", mode)
            return await self._fallback_extract(
                content, db, filename=filename, mode=mode,
                subject_id=subject_id, user_id=user_id,
            )

        # 整卷/材料题文档不走两阶段：paper/stimuli/question_groups 会恒为空。
        if _looks_like_paper(content):
            logger.info("two-phase skipped: document looks like a whole paper")
            return await self._fallback_extract(
                content, db, filename=filename, mode=mode,
                subject_id=subject_id, user_id=user_id,
            )

        # 表格单元格里常塞着成套练习（"方法归纳"表），题号不在行首，切段器看不到。
        # 先摘出来变成独立段，否则它们会整体并进上一题的题干（实测 4 道题吞掉 54 道）。
        expanded, extracted_n = expand_table_items(content)
        if extracted_n:
            logger.info("two-phase: expanded %d questions embedded in tables", extracted_n)

        segments = split_segments(expanded)
        if len(segments) < MIN_Q:
            logger.info(
                "two-phase skipped: %d segments < MIN_Q(%d), falling back",
                len(segments), MIN_Q,
            )
            return await self._fallback_extract(
                content, db, filename=filename, mode=mode,
                subject_id=subject_id, user_id=user_id,
            )

        from app.services.importing.provider_config import resolve_active_provider

        provider_name, config = await resolve_active_provider(
            db, is_vision=False, user_id=user_id
        )
        if provider_name is None:
            logger.warning("two-phase: no active AI provider, falling back")
            return await self._fallback_extract(
                content, db, filename=filename, mode=mode,
                subject_id=subject_id, user_id=user_id,
            )

        # 注意：这里刻意不降级到 AIExtractor。实测整本 227K token 的请求失败后，
        # 单次全量抽取只会再失败一次（模型只吐十几题就 stop），产出的残缺数据
        # 更难察觉。让异常抛出去，任务显式失败，重试一次就好。
        solve_mode = mode == "solve"
        if solve_mode:
            ai_items = await self._ask_solutions_batched(segments, config)
        else:
            ai_items = await self._ask_answers_batched(segments, config)

        # 校验：条数必须与切段数一致，且 ord 必须是 1..N 的连续排列。
        # 漏一条会让后面所有答案按 ord 对齐时整体错位，比降级更危险。
        try:
            ords = sorted(int(i.get("ord")) for i in ai_items if i.get("ord") is not None)
        except (TypeError, ValueError):
            ords = []
        expected = list(range(1, len(segments) + 1))
        if ords != expected:
            missing = sorted(set(expected) - set(ords))[:8]
            logger.warning(
                "two-phase: ord mismatch (%d answered vs %d segments, missing=%s), falling back",
                len(ords), len(segments), missing,
            )
            return await self._fallback_extract(
                content, db, filename=filename, mode=mode,
                subject_id=subject_id, user_id=user_id,
            )

        logger.info(
            "two-phase: %d segments, AI %s all in order",
            len(segments), "solved" if solve_mode else "answered",
        )

        questions = build_questions(segments, ai_items, solve=solve_mode)
        if not questions:
            return ExtractionResult()

        # 知识点富化沿用官方实现（向量召回 + AI rerank）
        provider = get_ai_provider(provider_name)
        try:
            await self.enricher.enrich(
                [_EnrichShim(q) for q in questions],
                subject_id=subject_id, provider=provider, config=config,
            )
        except Exception as exc:  # noqa: BLE001 - 富化失败不影响入库
            logger.warning("two-phase: knowledge point enrich failed: %s", exc)

        for q in questions:
            q["id"] = str(uuid.uuid4())

        return ExtractionResult(questions=questions, stimuli=[], question_groups=[], paper=None)

    async def _fallback_extract(self, content: str, db: AsyncSession, **kwargs: Any) -> ExtractionResult:
        if self._fallback is None:
            from app.services.importing.extract import AIExtractor
            self._fallback = AIExtractor()
        return await self._fallback.extract(content, db, **kwargs)


class _EnrichShim:
    """官方 KnowledgePointEnricher 读 .content / .knowledge_points 属性，
    这里把 dict 包一层，避免为了富化把题目转成 AIQuestion。"""

    __slots__ = ("_d", "content", "knowledge_points", "knowledge_point_ids")

    def __init__(self, d: Dict[str, Any]) -> None:
        self._d = d
        self.content = d.get("content", "")
        self.knowledge_points: List[str] = []
        self.knowledge_point_ids: List[int] = []

    def __getattr__(self, name: str) -> Any:
        try:
            return self._d[name]
        except KeyError:
            raise AttributeError(name) from None
