"""报告质量层：证据卡片化 + 大纲规划 + 草稿校验/修复。

设计成纯函数模块（不访问网络、不调用 LLM），原因和 quality.py 一样：

1. 离线/mock 评测也能稳定复现；单测可以直接构造证据，不用真实模型；
2. “摘要太水 / 逻辑割裂 / 引用堆叠 / 缺乏推演”这四个毛病必须变成**可判定的规则**，
   否则下一次改提示词又会退化；
3. LLM 只负责“写”和“重写”，判卷与兜底修复留在这一层，模型不稳定时报告不会崩。

对应关系（节点级）：
- summarize_sources → enrich_evidence：把“摘录句”变成“证据卡片”（结论/子问题/类型/去重/营销标记）
- plan_outline      → build_outline：把证据按子问题分节，明确每节目标与引用范围
- draft_report      → （提示词在 prompts.py）按节写作，摘要三段式，推演单独成节
- draft_review      → validate_draft / repair_draft：引用硬校验 + 超限剪枝 + 缺节补齐
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Sequence

from quality import LATIN_RE, normalize, subquestion_core

# ---------------------------------------------------------------- 基础正则/常量

CITATION_RE = re.compile(r"\[S(\d+)\]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.M)

INFER_TAG = "【推断】"
REQUIRED_SECTIONS = ("摘要", "关键发现", "局限与未证实")
ANALYSIS_TITLE = "分析与推演"
LIMITS_TITLE = "局限与未证实"
SUMMARY_TITLE = "摘要"
FINDINGS_TITLE = "关键发现"

# “摘要太水”的可判定特征：空判断开头（说了等于没说）
_VAGUE_SUMMARY_RE = re.compile(
    r"(存在差异|有所不同|各有优劣|各有特点|很重要|具有重要意义|值得关注|是一个重要|较为重要|备受关注)"
)

# 营销/证言话术：这类句子可以逐字验证，但对“回答研究问题”没有信息量，
# 实测会直接变成报告里的“引用堆叠”（同一营销页被反复引用）。
_PROMO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"受\s*[^，。；]{1,24}等[^，。；]{0,40}(信赖|信任|青睐)"),
    re.compile(r"(行业领先|业界领先|最佳选择|最强|第一名|顶级|革命性|颠覆性)"),
    re.compile(r"(立即体验|立即使用|免费试用|点击注册|扫码|预约演示|联系我们)"),
    re.compile(r"(我们的客户|深受.{0,12}喜爱|值得信赖)"),
    re.compile(
        r"(trusted by|get started|sign up|book a demo|free trial|world'?s leading|best-in-class)",
        re.I,
    ),
)
_TESTIMONIAL_RE = re.compile(r"^[\"“”'].{25,}[\"“”']$")

# 证据卡片类型（只用于组织报告与提示词，不影响“逐字可验证”这一硬约束）
_KIND_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("数据", re.compile(r"\d+(?:\.\d+)?\s*%|\b\d{2,}\b")),
    ("案例", re.compile(r"(案例|例如|比如|实践中|客户|团队|公司|生产环境|落地)")),
    ("定义", re.compile(r"(是指|指的是|定义为|是一种|是一个|概念|框架|平台|组件)")),
    ("机制", re.compile(r"(通过|机制|原理|流程|步骤|调用|实现|依赖|基于|触发|恢复)")),
    ("观点", re.compile(r"(认为|指出|表示|建议|应当|需要|风险|挑战|趋势)")),
)


# ---------------------------------------------------------------- 文本工具


def _units(text: str) -> set[str]:
    """中文 bigram + 英文词，作为“两句话说的是不是同一件事”的粗估。"""
    lowered = normalize(text).lower()
    units: set[str] = set()
    for word in LATIN_RE.findall(lowered):
        if len(word) > 1:
            units.add(word)
    for chunk in _CJK_RE.findall(lowered):
        for index in range(len(chunk) - 1):
            units.add(chunk[index : index + 2])
    return units


def _overlap(left: str, right: str) -> float:
    a, b = _units(left), _units(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _claim_key(text: str) -> str:
    return re.sub(r"[\s，,。.；;:：、!！?？\"'“”‘’（）()\[\]【】|·—\-]+", "", str(text or "")).lower()


def is_promo(text: str) -> bool:
    """是否为营销口号 / 客户证言（逐字可验证但没有信息量）。"""
    stripped = normalize(text)
    if not stripped:
        return False
    if len(stripped) >= 25 and _TESTIMONIAL_RE.match(stripped):
        return True
    return any(pattern.search(stripped) for pattern in _PROMO_PATTERNS)


def classify_kind(claim: str, quote: str = "") -> str:
    """证据卡片类型：数据 / 案例 / 定义 / 机制 / 观点 / 事实。"""
    text = normalize(f"{claim} {quote}")
    for kind, pattern in _KIND_RULES:
        if pattern.search(text):
            return kind
    return "事实"


# ---------------------------------------------------------------- 证据卡片化


def assign_sub_question(item: dict[str, Any], sub_questions: Sequence[str]) -> str:
    """把一条证据归到最能解释它的子问题；匹配不上返回空串。"""
    text = normalize(f"{item.get('claim') or ''} {item.get('quote') or ''}")
    if not text:
        return ""
    units = _units(text)
    best, best_score = "", 0.0
    for sub_question in sub_questions:
        if not str(sub_question).strip():
            continue
        score = len(units & _units(str(sub_question))) / max(1, len(_units(str(sub_question))))
        if score > best_score:
            best, best_score = str(sub_question), score
    return best if best_score > 0 else ""


def enrich_evidence(
    evidence: Iterable[dict[str, Any]],
    sub_questions: Sequence[str] = (),
    *,
    max_items: int = 24,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把“摘录句”升级成“证据卡片”。

    - 补充 ``sub_question``（归属子问题）、``kind``（定义/机制/数据/案例/观点）、``promo``（营销标记）；
    - 同一来源、同一结论的多条证据合并（``merged_count``），避免报告里同一句话反复出现；
    - 营销话术不直接删除（它仍是可验证证据），但标记 ``promo``，写作时降权。

    返回 (cards, stats)。cards 顺序即报告里的 S1、S2… 编号。
    """
    items = [item for item in evidence if isinstance(item, dict)]
    cards: list[dict[str, Any]] = []
    stats = {"total": len(items), "deduped": 0, "promo": 0, "assigned": 0}

    def _key(item: dict[str, Any]) -> str:
        return _claim_key(item.get("claim") or item.get("quote") or "")

    def _url(item: dict[str, Any]) -> str:
        return str(item.get("source_url") or "").rstrip("/").lower()

    for item in items:
        card = dict(item)
        card["sub_question"] = str(card.get("sub_question") or assign_sub_question(card, sub_questions))
        if card["sub_question"]:
            stats["assigned"] += 1
        card["kind"] = str(card.get("kind") or classify_kind(card.get("claim"), card.get("quote")))
        card["promo"] = bool(card.get("promo") or is_promo(card.get("claim")) or is_promo(card.get("quote")))
        if card["promo"]:
            stats["promo"] += 1

        duplicate = None
        for existing in cards:
            # 同一来源内“结论相同”或“表述高度重叠”视为重复；跨来源保留，用于交叉验证
            same_source = _key(card) and _url(card) == _url(existing)
            same_claim = _key(card) and _key(card) == _key(existing)
            if same_source and (same_claim or _overlap(card.get("claim", ""), existing.get("claim", "")) >= 0.8):
                duplicate = existing
                break
            if same_claim:
                duplicate = existing
                break
        if duplicate is not None:
            duplicate["merged_count"] = int(duplicate.get("merged_count") or 1) + 1
            merged_urls = list(duplicate.get("merged_urls") or [str(duplicate.get("source_url") or "")])
            url = str(card.get("source_url") or "")
            if url and url not in merged_urls:
                merged_urls.append(url)
            duplicate["merged_urls"] = [item for item in merged_urls if item]
            stats["deduped"] += 1
            continue
        card["merged_count"] = 1
        cards.append(card)

    if len(cards) > max_items:
        # 超出上限时优先保留非营销、非观点卡片
        cards.sort(key=lambda item: (bool(item.get("promo")), item.get("kind") == "观点"))
        stats["trimmed"] = len(cards) - max_items
        cards = cards[:max_items]
    return cards, stats


def find_bullets(evidence: Sequence[dict[str, Any]], ids: Iterable[int] | None = None, *, limit: int = 6) -> list[str]:
    """「关键发现」条目：按结论去重，每条一句 + 角标（程序生成，保证条条有引用）。"""
    items = list(evidence)
    chosen = [int(value) for value in (ids or range(1, len(items) + 1)) if 1 <= int(value) <= len(items)]
    merged: dict[str, list[int]] = {}
    for index in chosen:
        item = items[index - 1]
        key = _claim_key(item.get("claim") or item.get("quote") or "")
        if not key:
            continue
        merged.setdefault(key, []).append(index)
    lines: list[str] = []
    # 非营销证据优先
    ordered = sorted(
        merged.items(),
        key=lambda pair: bool(items[pair[1][0] - 1].get("promo")),
    )
    for _, indices in ordered:
        item = items[indices[0] - 1]
        claim = normalize(item.get("claim") or item.get("quote") or "").rstrip("。.；;，, ")
        if not claim:
            continue
        cites = "".join(f"[S{index}]" for index in sorted(indices)[:2])
        lines.append(f"- {claim} {cites}")
        if len(lines) >= limit:
            break
    return lines or ["- 未找到可靠来源，无法给出可引用的关键结论。"]


# ---------------------------------------------------------------- 大纲规划

DEFAULT_MAX_CONTENT_SECTIONS = 3


def _evidence_ids(evidence: Sequence[dict[str, Any]]) -> list[int]:
    return [index + 1 for index in range(len(evidence))]


def build_outline(
    question: str,
    sub_questions: Sequence[str],
    evidence: Sequence[dict[str, Any]],
    *,
    uncovered: Sequence[str] = (),
    max_content_sections: int = DEFAULT_MAX_CONTENT_SECTIONS,
) -> dict[str, Any]:
    """规则版大纲（LLM 不可用时的降级，也是 LLM 输出的兜底模板）。

    结构固定为：摘要 → 按子问题分的内容节 → 分析与推演 → 局限与未证实。
    内容节的证据按 ``sub_question`` 分组，保证“一节只引自己那组证据”，
    这是治“逻辑割裂/引用堆叠”的结构性手段。
    """
    sections: list[dict[str, Any]] = [
        {
            "key": "summary",
            "title": SUMMARY_TITLE,
            "goal": "用 2~3 句话直接回答研究问题：结论、关键依据（带引用）、结论的适用边界。",
            "evidence_ids": _evidence_ids(evidence)[:8],
        },
        {
            "key": "findings",
            "title": FINDINGS_TITLE,
            "goal": "跨来源归纳 3~6 条关键结论，每条一句并带引用角标。",
            "evidence_ids": _evidence_ids(evidence)[:8],
        },
    ]

    groups: list[tuple[str, list[int]]] = []
    for index, item in enumerate(evidence):
        key = str(item.get("sub_question") or "")
        for existing_key, ids in groups:
            if existing_key == key:
                ids.append(index + 1)
                break
        else:
            groups.append((key, [index + 1]))
    # 证据多的组优先，但保留子问题原始顺序
    ranked = sorted(groups, key=lambda pair: len(pair[1]), reverse=True)[:max_content_sections]
    order = {str(sub): pos for pos, sub in enumerate(sub_questions)}
    ranked.sort(key=lambda pair: order.get(pair[0], 999))

    used: set[int] = set()
    for key, ids in ranked:
        used.update(ids)
        title = subquestion_core(key)[:24] if key else "其他证据"
        sections.append(
            {
                "key": f"content-{len(sections)}",
                "title": title,
                "goal": (
                    f"回答子问题「{key}」：先给结论，再解释机制/依据，最后说明证据强度。"
                    if key
                    else "归纳无法归入具体子问题的证据，只写它们能支持的结论。"
                ),
                "evidence_ids": ids,
                "sub_question": key,
            }
        )

    leftover = [index + 1 for index in range(len(evidence)) if index + 1 not in used]
    if leftover and len(sections) < max_content_sections + 2:
        sections.append(
            {
                "key": "content-extra",
                "title": "补充证据",
                "goal": "补充前面小节未覆盖的证据，只写与主问题直接相关的结论。",
                "evidence_ids": leftover[:6],
            }
        )

    sections.append(
        {
            "key": "analysis",
            "title": ANALYSIS_TITLE,
            "goal": (
                "基于以上证据做因果/对比/条件推演：每条以【推断】开头，写清依据哪几条证据、"
                "在什么前提下成立、可能被什么证据推翻。"
            ),
            "evidence_ids": _evidence_ids(evidence),
        }
    )
    sections.append(
        {
            "key": "limits",
            "title": LIMITS_TITLE,
            "goal": "列出没有证据支撑的方面、证据本身的局限（单一来源/时效/立场），不要写套话。",
            "evidence_ids": [],
        }
    )
    return {
        "sections": sections,
        "uncovered": [str(item) for item in uncovered],
        "source": "rule",
        "question": normalize(question)[:200],
    }


def normalize_llm_outline(
    data: dict[str, Any] | None,
    evidence: Sequence[dict[str, Any]],
    *,
    uncovered: Sequence[str] = (),
    max_sections: int = 6,
) -> dict[str, Any] | None:
    """校验 LLM 给出的大纲：编号必须存在、标题非空、必须包含分析/局限节。"""
    if not isinstance(data, dict):
        return None
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list):
        return None
    total = len(evidence)
    sections: list[dict[str, Any]] = []
    for raw in raw_sections:
        if not isinstance(raw, dict):
            continue
        title = normalize(str(raw.get("title") or ""))[:40]
        if not title:
            continue
        ids: list[int] = []
        for value in raw.get("evidence_ids") or []:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= total and index not in ids:
                ids.append(index)
        sections.append(
            {
                "key": f"llm-{len(sections)}",
                "title": title,
                "goal": normalize(str(raw.get("goal") or ""))[:200] or f"写好「{title}」这一节。",
                "evidence_ids": ids,
                "sub_question": normalize(str(raw.get("sub_question") or ""))[:120],
            }
        )
        if len(sections) >= max_sections:
            break
    if not sections:
        return None

    titles = [section["title"] for section in sections]
    if FINDINGS_TITLE not in titles:
        insert_at = 1 if sections and sections[0]["title"] == SUMMARY_TITLE else 0
        sections.insert(
            insert_at,
            {
                "key": "findings",
                "title": FINDINGS_TITLE,
                "goal": "跨来源归纳 3~6 条关键结论，每条一句并带引用角标。",
                "evidence_ids": _evidence_ids(evidence)[:8],
            },
        )
    if ANALYSIS_TITLE not in titles:
        sections.append(
            {
                "key": "analysis",
                "title": ANALYSIS_TITLE,
                "goal": "基于以上证据做推演，每条以【推断】开头并挂引用。",
                "evidence_ids": _evidence_ids(evidence),
            }
        )
    if LIMITS_TITLE not in titles:
        sections.append(
            {
                "key": "limits",
                "title": LIMITS_TITLE,
                "goal": "列出没有证据支撑的方面与证据自身的局限。",
                "evidence_ids": [],
            }
        )
    return {
        "sections": sections,
        "uncovered": [str(item) for item in uncovered],
        "source": "llm",
    }


# ---------------------------------------------------------------- 草稿校验


def _section_text(draft: str, title: str) -> str:
    """取出 `## title` 到下一个二级标题之间的正文。"""
    pattern = re.compile(rf"^##\s+{re.escape(title)}\s*$", re.M)
    match = pattern.search(draft)
    if not match:
        return ""
    rest = draft[match.end() :]
    next_heading = _HEADING_RE.search(rest)
    return rest[: next_heading.start()] if next_heading else rest


def _sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for line in text.splitlines():
        stripped = _BULLET_RE.sub("", line).strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("|"):
            continue
        sentences.extend(part.strip() for part in _SENTENCE_SPLIT_RE.split(stripped) if part.strip())
    return sentences


def _citations(text: str) -> list[int]:
    return [int(value) for value in CITATION_RE.findall(text)]


def _citation_target(evidence: Sequence[dict[str, Any]], index: int) -> dict[str, Any] | None:
    if 1 <= index <= len(evidence):
        return evidence[index - 1]
    return None


def _is_relevant(sentence: str, item: dict[str, Any]) -> bool:
    text = normalize(f"{item.get('claim') or ''} {item.get('quote') or ''} {item.get('relevance_reason') or ''}")
    if not text:
        return False
    if _overlap(sentence, text) >= 0.08:
        return True
    # 专有名词/英文实体命中即算相关（中英混排时 bigram 会失真）
    latin = {word.lower() for word in LATIN_RE.findall(sentence)}
    return bool(latin & {word.lower() for word in LATIN_RE.findall(text)})


def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("|")
        and "-" in stripped
        and set(stripped) <= set("|-: ")
    )


def has_markdown_table(text: str) -> bool:
    """报告里是否有真正的 Markdown 表格（表头 + 分隔行 + 至少一行数据）。"""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    for index in range(len(lines) - 1):
        header, separator = lines[index].strip(), lines[index + 1].strip()
        if not header.startswith("|") or not _is_table_separator(separator):
            continue
        if index + 2 < len(lines) and lines[index + 2].strip().startswith("|"):
            return True
    return False


def scrub_year_number_mismatches(
    draft: str, evidence: Sequence[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """把“数字来自 2030 年证据、却被写成 2027 年”的句子年份改回证据年份。

    这是确定性纠偏：不删除关键数据，只修正年份；表格行有错配直接删除（表格无法安全改单格）。
    """
    text = str(draft or "")
    body, marker, source = text.partition("## 来源清单")
    year_by_number: dict[str, set[str]] = {}
    for item in evidence:
        ev_text = f"{item.get('claim') or ''} {item.get('quote') or ''}"
        ev_years = years_in(ev_text)
        for token in numeric_tokens(ev_text):
            year_by_number.setdefault(token, set()).update(ev_years)
    fixes: list[dict[str, Any]] = []
    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        if stripped.startswith("|"):
            s_years = years_in(stripped)
            bad = False
            for token in numeric_tokens(stripped):
                ev_years = year_by_number.get(token)
                if ev_years and s_years and not s_years & ev_years:
                    bad = True
                    fixes.append({"code": "dropped_year_mismatch_row", "detail": f"表格行年份与证据不符：{stripped[:40]}"})
                    break
            if not bad:
                out.append(line)
            continue
        pieces = [part for part in _SENTENCE_SPLIT_RE.split(line) if part.strip()]
        kept: list[str] = []
        for piece in pieces:
            s_years = years_in(piece)
            replaced = False
            if s_years:
                for token in numeric_tokens(piece):
                    ev_years = year_by_number.get(token)
                    if not ev_years or s_years & ev_years:
                        continue
                    target_year = sorted(ev_years)[0]
                    piece = re.sub(
                        r"(?<!\d)((?:19|20)\d{2})(?!\d)",
                        lambda m: target_year if m.group(1) not in ev_years else m.group(1),
                        piece,
                        count=1,
                    )
                    fixes.append(
                        {
                            "code": "fixed_year_mismatch",
                            "detail": f"数字 {token} 的年份改为证据年份 {target_year}",
                        }
                    )
                    replaced = True
                    break
            kept.append(piece)
        if kept:
            out.append("".join(kept).strip())
    result = "\n".join(out).strip()
    if marker:
        result = result.rstrip() + "\n\n" + marker.strip() + source
    return result.strip() + "\n", fixes


def _table_data_rows(text: str) -> list[str]:
    """返回所有 Markdown 表格的数据行（跳过表头与分隔行）。"""
    rows: list[str] = []
    lines = str(text or "").splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].strip().startswith("|"):
            index += 1
            continue
        block: list[str] = []
        while index < len(lines) and lines[index].strip().startswith("|"):
            block.append(lines[index])
            index += 1
        if len(block) >= 2 and _is_table_separator(block[1]):
            rows.extend(block[2:])
    return rows


def _report_body(text: str) -> str:
    """报告正文（去掉来源清单），用于字数统计。"""
    body = str(text or "").split("## 来源清单", 1)[0]
    return body.strip()


# ---------------------------------------------------------------- 数字 grounding
# 只认“带单位/量级”的数字：金额（亿/万/元）、百分比、数量（个/家/倍）、英文量级（B/M/K）。
# 裸数字容易和引用编号、年份混淆，单独由年份规则处理。
_NUM_UNIT_RE = re.compile(
    r"(?<![0-9A-Za-z_.])(\d[\d,]*(?:\.\d+)?)\s*"
    r"(%|％|亿元|万元|亿|万|千万|百万|万人|千人|个|家|倍|美元|元|B|M|K|billion|million|trillion)",
    re.I,
)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_MD_LINK_RE = re.compile(r"\((?:https?|ftp)://[^)\s]+\)")
_UNIT_CANON = {
    "％": "%",
    "%": "%",
    "亿元": "亿",
    "万元": "万",
    "千万": "千万",
    "百万": "百万",
    "万人": "万人",
    "千人": "千人",
    "个": "个",
    "家": "家",
    "倍": "倍",
    "美元": "美元",
    "元": "元",
    "b": "B",
    "m": "M",
    "k": "K",
    "billion": "B",
    "million": "M",
    "trillion": "T",
}


def _canon_number(raw_num: str, unit: str) -> str:
    num = str(raw_num).replace(",", "")
    unit_key = unit.lower() if unit.isascii() else unit
    canon = _UNIT_CANON.get(unit_key, unit_key)
    return f"{num}{canon}".lower()


def numeric_tokens(text: str) -> list[str]:
    """抽取文本里带单位的数字（规范化成 ``500亿`` / ``80%`` 这种形式）。"""
    clean = _MD_LINK_RE.sub("", str(text or ""))
    return [_canon_number(match.group(1), match.group(2)) for match in _NUM_UNIT_RE.finditer(clean)]


def years_in(text: str) -> set[str]:
    return set(_YEAR_RE.findall(_MD_LINK_RE.sub("", str(text or ""))))


def _allowed_numbers(evidence: Sequence[dict[str, Any]], question: str = "") -> set[str]:
    allowed = set(numeric_tokens(question))
    for item in evidence:
        allowed.update(numeric_tokens(item.get("claim")))
        allowed.update(numeric_tokens(item.get("quote")))
        allowed.update(numeric_tokens(item.get("numbers")))
    return allowed


def unsupported_numbers(
    text: str, evidence: Sequence[dict[str, Any]], question: str = ""
) -> list[str]:
    """报告里出现、但证据/研究问题里都找不到的带单位数字（去重、保序）。"""
    allowed = _allowed_numbers(evidence, question)
    bad: list[str] = []
    for token in numeric_tokens(text):
        if token not in allowed and token not in bad:
            bad.append(token)
    return bad


def number_issues(
    text: str, evidence: Sequence[dict[str, Any]], question: str = ""
) -> list[dict[str, Any]]:
    """数字问题：无证据数字 + 无证据年份 + 年份/数字错配。"""
    issues: list[dict[str, Any]] = []
    body = _report_body(text)
    for token in unsupported_numbers(body, evidence, question)[:8]:
        issues.append(
            {
                "code": "number_not_in_evidence",
                "detail": f"数字「{token}」在证据与问题里都找不到，疑似推算/编造",
            }
        )
    allowed_years = years_in(question)
    for item in evidence:
        allowed_years |= years_in(item.get("claim"))
        allowed_years |= years_in(item.get("quote"))
    for year in sorted(years_in(body) - allowed_years)[:5]:
        issues.append({"code": "year_not_in_evidence", "detail": f"年份「{year}」在证据里找不到"})
    # 年份错配：数字来自某条证据，但报告把它的年份写成了另一个年份
    year_by_number: dict[str, set[str]] = {}
    for item in evidence:
        ev_text = f"{item.get('claim') or ''} {item.get('quote') or ''}"
        ev_years = years_in(ev_text)
        for token in numeric_tokens(ev_text):
            year_by_number.setdefault(token, set()).update(ev_years)
    for sentence in _sentences(body):
        s_years = years_in(sentence)
        if not s_years:
            continue
        for token in numeric_tokens(sentence):
            ev_years = year_by_number.get(token)
            if not ev_years or s_years & ev_years:
                continue
            issues.append(
                {
                    "code": "year_number_mismatch",
                    "detail": (
                        f"数字「{token}」在证据中是 {','.join(sorted(ev_years))} 年，"
                        f"报告写成 {','.join(sorted(s_years))} 年"
                    ),
                }
            )
            break
    return issues


def _line_with_unsupported_numbers(line: str, evidence: Sequence[dict[str, Any]], question: str) -> tuple[bool, list[str]]:
    bad = unsupported_numbers(line, evidence, question)
    return bool(bad), bad


def scrub_unsupported_numbers(
    draft: str, evidence: Sequence[dict[str, Any]], question: str = ""
) -> tuple[str, list[dict[str, Any]]]:
    """确定性兜底：删掉含“证据中不存在数字”的句子/表格行，避免编造数据进入报告。"""
    text = str(draft or "")
    body, marker, source = text.partition("## 来源清单")
    out: list[str] = []
    fixes: list[dict[str, Any]] = []
    dropped_sentences = 0
    dropped_rows = 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        if stripped.startswith("|"):
            bad, tokens = _line_with_unsupported_numbers(line, evidence, question)
            if bad:
                dropped_rows += 1
                fixes.append(
                    {"code": "dropped_unsupported_number_row", "detail": f"表格行含无证据数字：{', '.join(tokens)}"}
                )
                continue
            out.append(line)
            continue
        pieces = [part for part in _SENTENCE_SPLIT_RE.split(line) if part.strip()]
        kept: list[str] = []
        for piece in pieces:
            bad, tokens = _line_with_unsupported_numbers(piece, evidence, question)
            if bad:
                dropped_sentences += 1
                fixes.append(
                    {"code": "dropped_unsupported_number", "detail": f"删除含无证据数字 {', '.join(tokens)} 的句子"}
                )
                continue
            kept.append(piece)
        if kept:
            out.append("".join(kept).strip())
    if dropped_sentences:
        fixes.append({"code": "scrubbed_numbers", "detail": f"共删除 {dropped_sentences} 句无证据数字表述"})
    if dropped_rows:
        fixes.append({"code": "scrubbed_number_rows", "detail": f"共删除 {dropped_rows} 行无证据数字表格"})
    result = "\n".join(out).strip()
    if marker:
        result = result.rstrip() + "\n\n" + marker.strip() + source
    return result.strip() + "\n", fixes


def scrub_uncited_table_rows(draft: str) -> tuple[str, list[dict[str, Any]]]:
    """删除没有任何 ``[S#]`` 引用的表格数据行（防止表格里凭常识补写结论）。"""
    text = str(draft or "")
    body, marker, source = text.partition("## 来源清单")
    lines = body.splitlines()
    out: list[str] = []
    fixes: list[dict[str, Any]] = []
    index = 0
    dropped = 0
    while index < len(lines):
        if lines[index].strip().startswith("|"):
            block: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            if len(block) >= 2 and _is_table_separator(block[1]):
                header, separator, rows = block[0], block[1], block[2:]
                kept = [row for row in rows if "[S" in row]
                dropped += len(rows) - len(kept)
                if kept:
                    out.extend([header, separator, *kept])
                continue
            out.extend(block)
            continue
        out.append(lines[index])
        index += 1
    if dropped:
        fixes.append({"code": "dropped_uncited_table_row", "detail": f"删除 {dropped} 行无引用表格数据"})
    result = "\n".join(out).strip()
    if marker:
        result = result.rstrip() + "\n\n" + marker.strip() + source
    return result.strip() + "\n", fixes


def scrub_uncited_claims(
    draft: str, *, market: bool = False, controversy: bool = False
) -> tuple[str, list[dict[str, Any]]]:
    """在“需要证据”的小节里删除没有 [S#] 的断言（玩家/驱动/观点/趋势等）。

    摘要/关键发现由其它校验管；这里只处理行业争议、市场分析类报告的数据/观点小节，
    避免“百度、阿里、腾讯是主要玩家”这种无来源常识被写成结论。
    """
    if not (market or controversy):
        return draft, []
    keywords = (
        "市场规模",
        "主要玩家",
        "竞争格局",
        "增长驱动",
        "风险",
        "争议",
        "支持方",
        "反对方",
        "正方",
        "反方",
        "观点",
        "趋势",
        "定义",
        "区别",
        "可能性",
    )
    text = str(draft or "")
    body, marker, source = text.partition("## 来源清单")
    sections = _split_sections(body)
    fixes: list[dict[str, Any]] = []
    out_sections: list[tuple[str | None, list[str]]] = []
    for title, lines in sections:
        if title is None or not any(word in title for word in keywords):
            out_sections.append((title, lines))
            continue
        kept_lines: list[str] = []
        dropped = 0
        placeholder = "- 公开证据不足，本节不作结论。"
        has_placeholder = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("|", "#")):
                kept_lines.append(line)
                continue
            if stripped == placeholder:
                if not has_placeholder:
                    kept_lines.append(line)
                    has_placeholder = True
                continue
            pieces = [part for part in _SENTENCE_SPLIT_RE.split(line) if part.strip()]
            kept = [piece for piece in pieces if "[S" in piece]
            dropped += len(pieces) - len(kept)
            if kept:
                kept_lines.append("".join(kept).strip())
            elif pieces and not has_placeholder:
                kept_lines.append(placeholder)
                has_placeholder = True
        if dropped:
            fixes.append(
                {"code": "dropped_uncited_claim", "detail": f"「{title}」删除 {dropped} 句无引用断言"}
            )
        out_sections.append((title, kept_lines))
    result = _render_sections(out_sections)
    if marker:
        result = result.rstrip() + "\n\n" + marker.strip() + source
    return result.strip() + "\n", fixes


def ensure_market_table(
    draft: str, evidence: Sequence[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """市场类报告没有数据表时，用证据里的数字生成一张最小数据表（带年份/引用）。"""
    text = str(draft or "")
    if has_markdown_table(text):
        return text, []
    rows: list[str] = []
    for index, item in enumerate(evidence, start=1):
        quote = str(item.get("quote") or "").strip()
        claim = str(item.get("claim") or "").strip()
        # 年份/口径从 quote 取（原文只讲一个年份/口径），claim 可能把问题年份也带进来
        meta_text = quote or claim
        tokens = numeric_tokens(meta_text)
        if not tokens:
            continue
        years = sorted(years_in(meta_text))
        year = years[0] if years else "未注明"
        scope = " ".join(meta_text.split())[:60]
        rows.append(f"| 市场数据 | {'、'.join(tokens[:2])} | {year} | {scope} | [S{index}] |")
    if not rows:
        return text, []
    table_lines = [
        "| 指标 | 数值 | 年份 | 口径/说明 | 来源 |",
        "| --- | --- | --- | --- | --- |",
        *rows[:4],
    ]
    body, marker, source = text.partition("## 来源清单")
    sections = _split_sections(body)
    target_index = next(
        (i for i, (title, _lines) in enumerate(sections) if title and "市场规模" in title),
        None,
    )
    if target_index is not None:
        sections[target_index] = (sections[target_index][0], table_lines)
    else:
        insert_at = next(
            (i for i, (title, _lines) in enumerate(sections) if title == ANALYSIS_TITLE),
            len(sections),
        )
        sections.insert(insert_at, ("市场规模与数据", table_lines))
    result = _render_sections(sections)
    if marker:
        result = result.rstrip() + "\n\n" + marker.strip() + source
    return result.strip() + "\n", [
        {"code": "inserted_market_table", "detail": f"用证据数字补充数据表 {min(len(rows), 4)} 行"}
    ]


def ensure_controversy_balance(draft: str) -> tuple[str, list[dict[str, Any]]]:
    """争议类报告只找到一方观点时，明确写入“未找到另一方公开观点”，不伪造平衡。"""
    text = str(draft or "")
    support = re.search(r"支持方|正方|支持者|赞成|看好", text)
    oppose = re.search(r"反对方|反方|反对者|质疑|批评|看衰|反对", text)
    if support and oppose:
        return text, []
    if re.search(r"未找到(另一方|反方|支持方)的?(公开)?观点", text):
        return text, []
    missing = "反对方" if support else "支持方"
    note = f"- 公开证据只找到{'支持方' if support else '反对方'}观点，未找到另一方（{missing}）公开观点；本报告不伪造平衡。"
    body, marker, source = text.partition("## 来源清单")
    sections = _split_sections(body)
    target = next(
        (i for i, (title, _lines) in enumerate(sections) if title and "局限" in title),
        None,
    )
    if target is not None:
        title, lines = sections[target]
        sections[target] = (title, [line for line in lines if line.strip()] + [note, ""])
    else:
        sections.append(("局限与未证实", [note, ""]))
    result = _render_sections(sections)
    if marker:
        result = result.rstrip() + "\n\n" + marker.strip() + source
    return result.strip() + "\n", [
        {"code": "inserted_controversy_gap", "detail": f"明确标注未找到{missing}公开观点"}
    ]


def repeated_sentence_groups(
    text: str, *, threshold: float = 0.9, min_chars: int = 14, max_groups: int = 3
) -> list[dict[str, Any]]:
    """找出“同一事实反复说”的句子组（≥3 句高度相似）。

    只做词面近似判断（中文 bigram 重叠），用于提示“车轱辘话”；不做语义理解。
    """
    groups: list[dict[str, Any]] = []
    for sentence in _sentences(_report_body(text)):
        plain = normalize(sentence)
        if len(plain) < min_chars:
            continue
        for group in groups:
            if _overlap(plain, group["sample"]) >= threshold:
                group["count"] += 1
                break
        else:
            groups.append({"sample": plain, "count": 1})
    repeated = [group for group in groups if group["count"] >= 3]
    repeated.sort(key=lambda group: -group["count"])
    return repeated[:max_groups]


def _report_char_limit() -> int:
    try:
        return max(200, int(os.getenv("AGENT_REPORT_MAX_CHARS", "1000")))
    except ValueError:
        return 1000


def report_char_limit() -> int:
    """报告正文字数上限（不含来源清单），默认 1000。"""
    return _report_char_limit()


def report_chars(text: str) -> int:
    """报告正文字数（去掉来源清单、引用链接 URL 与空白）。

    统计口径是“人读的字数”：`[S1](https://...)` 里的 URL 不算字数，
    否则渲染时自动加的可点击链接会把 800 字的报告算成 1400+。
    """
    body = _report_body(text)
    body = re.sub(r"\((?:https?|ftp)://[^)\s]+\)", "", body)
    body = re.sub(r"\[\^\d+\]", "", body)
    return len(re.sub(r"\s+", "", body))


def _split_sections(text: str) -> list[tuple[str | None, list[str]]]:
    """按 ``## 标题`` 切分报告；第一段（无标题）用 ``None`` 表示。"""
    sections: list[tuple[str | None, list[str]]] = []
    title: str | None = None
    lines: list[str] = []
    for line in str(text or "").splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            sections.append((title, lines))
            title = match.group(1).strip()
            lines = []
        else:
            lines.append(line)
    sections.append((title, lines))
    return sections


def _section_has_table(lines: Sequence[str]) -> bool:
    return has_markdown_table("\n".join(lines))


def _keep_first_sentences(lines: Sequence[str], count: int) -> list[str]:
    """保留前 count 句（按行内句子切分），丢掉多余解释。"""
    kept: list[str] = []
    used = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("|"):
            kept.append(line)
            continue
        sentences = [part for part in _SENTENCE_SPLIT_RE.split(stripped) if part.strip()]
        if used >= count:
            continue
        take = sentences[: count - used]
        if take:
            kept.append("".join(take).strip() if len(take) > 1 else take[0])
            used += len(take)
    return kept


def _keep_table_block(lines: Sequence[str]) -> list[str]:
    """只保留 Markdown 表格行（表头/分隔/数据），丢掉表格外的长解释。"""
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") or stripped.startswith("#"):
            kept.append(line)
        elif not kept and stripped:
            kept.append(line)  # 表格前的一句引导
    return kept


def _render_sections(items: Sequence[tuple[str | None, list[str]]]) -> str:
    chunks: list[str] = []
    for title, lines in items:
        block = "\n".join(lines).strip("\n").strip()
        if title is None:
            if block:
                chunks.append(block)
        elif block:
            chunks.append(f"## {title}\n\n{block}")
    return "\n\n".join(chunks).strip()


def _section_units(lines: Sequence[str]) -> list[str]:
    """把一个章节拆成可删除的最小单元：表格/列表/标题整行，普通段落按句。"""
    units: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("|", "#", "- ", "* ")):
            units.append(line)
            continue
        units.extend(part for part in _SENTENCE_SPLIT_RE.split(line) if part.strip())
    return units


def _fit_to_limit(rendered: str, target: int) -> tuple[str, list[dict[str, Any]]]:
    """最后一道硬约束：通过删可选小节、缩减必需小节、限表格行，把正文压到 target 内。"""
    if report_chars(rendered) <= target:
        return rendered, []
    sections = [(title, list(lines)) for title, lines in _split_sections(rendered)]
    required = {SUMMARY_TITLE, FINDINGS_TITLE, ANALYSIS_TITLE, LIMITS_TITLE}
    table_titles = {title for title, lines in sections if title and _section_has_table(lines)}
    fixes: list[dict[str, Any]] = []

    def render() -> str:
        return _render_sections(sections)

    def chars() -> int:
        return report_chars(render())

    # 1) 先删非必需、非表格小节
    while chars() > target:
        candidates = [
            index
            for index, (title, _lines) in enumerate(sections)
            if title is not None and title not in required and title not in table_titles
        ]
        if not candidates:
            break
        index = candidates[-1]
        fixes.append({"code": "dropped_section", "detail": f"删除非必需小节：{sections[index][0]}"})
        sections.pop(index)

    # 2) 必需小节逐句收缩（每个小节至少留 1 句/1 条）
    priority = (LIMITS_TITLE, ANALYSIS_TITLE, FINDINGS_TITLE, SUMMARY_TITLE)
    while chars() > target:
        changed = False
        for title in priority:
            index = next((i for i, (t, _l) in enumerate(sections) if t == title), None)
            if index is None:
                continue
            section_title, lines = sections[index]
            units = _section_units(lines)
            if len(units) > 1:
                sections[index] = (section_title, units[:-1])
                changed = True
                if chars() <= target:
                    break
        if chars() <= target:
            break
        # 3) 表格最多保留表头 + 分隔 + 1 行
        table_index = next(
            (i for i, (t, _l) in enumerate(sections) if t in table_titles), None
        )
        if table_index is not None:
            title, lines = sections[table_index]
            table_lines = [line for line in lines if line.strip().startswith("|")]
            other_lines = [line for line in lines if not line.strip().startswith("|")]
            if len(table_lines) > 3:
                sections[table_index] = (title, other_lines + table_lines[:3])
                fixes.append({"code": "trimmed_table_rows", "detail": "表格压到表头 + 1 行数据"})
                changed = True
        if not changed:
            break

    text = render()
    if report_chars(text) > target:
        # 极端兜底：按字符硬截断（保留可读的句末）
        while text and report_chars(text) > target:
            text = text[:-30]
            cut = max(text.rfind("。"), text.rfind("\n"))
            if cut > 0:
                text = text[: cut + 1]
        fixes.append({"code": "hard_truncated", "detail": f"正文硬截断到 {target} 字以内"})
        return text.strip() + "\n", fixes
    return text.strip() + "\n", fixes


def compact_report(
    draft: str,
    *,
    limit: int | None = None,
    max_summary_sentences: int = 3,
    max_findings: int = 5,
    max_inferences: int = 2,
    max_section_sentences: int = 2,
    strict: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    """把超长报告压到字数上限内（确定性后处理，不依赖模型配合）。

    策略（按保留优先级）：
    1. 摘要保留前 3 句、关键发现保留前 5 条、分析保留前 2 条推演、其他小节保留前 2 句；
    2. 对比类报告只保留**一个**表格小节，重复的表格小节整体丢弃；
    3. 全篇同一事实（近似句子）最多保留两次；
    4. 仍超限时，从尾部删除非必需小节（摘要/关键发现/分析/局限保留）。
    返回 (压缩后的文本, fixes)；不需要压缩时原样返回、fixes 为空。
    """
    target = limit or _report_char_limit()
    text = str(draft or "")
    if report_chars(text) <= target:
        return text, []
    body, marker, source = text.partition("## 来源清单")
    fixes: list[dict[str, Any]] = []
    required = {SUMMARY_TITLE, FINDINGS_TITLE, ANALYSIS_TITLE, LIMITS_TITLE}
    sections = _split_sections(body)
    kept_sections: list[tuple[str | None, list[str]]] = []
    table_seen = False
    table_titles: set[str] = set()
    for title, lines in sections:
        if title is None:
            cleaned = [line for line in lines if line.strip()]
            if cleaned:
                kept_sections.append((None, cleaned))
            continue
        if title == LIMITS_TITLE:
            kept_sections.append((title, lines))
            continue
        if title == SUMMARY_TITLE:
            kept_sections.append((title, _keep_first_sentences(lines, max_summary_sentences)))
            continue
        if title == FINDINGS_TITLE:
            bullets = [line for line in lines if line.strip().startswith(("- ", "* "))]
            if len(bullets) > max_findings:
                fixes.append({"code": "trimmed_findings", "detail": f"关键发现从 {len(bullets)} 条压到 {max_findings} 条"})
            kept_sections.append((title, bullets[:max_findings]))
            continue
        if title == ANALYSIS_TITLE:
            inferences = [line for line in lines if INFER_TAG in line or "推断" in line]
            if len(inferences) > max_inferences:
                fixes.append(
                    {"code": "trimmed_inference", "detail": f"推演从 {len(inferences)} 条压到 {max_inferences} 条"}
                )
            kept_sections.append((title, inferences[:max_inferences]))
            continue
        if _section_has_table(lines):
            if table_seen:
                fixes.append({"code": "dropped_duplicate_table_section", "detail": f"删除重复表格小节：{title}"})
                continue
            table_seen = True
            table_titles.add(title)
            kept_sections.append((title, _keep_table_block(lines)))
            continue
        kept_sections.append((title, _keep_first_sentences(lines, max_section_sentences)))

    # 同一事实全篇最多两次：按行做近似去重（表格与标题行不参与）
    deduped: list[tuple[str | None, list[str]]] = []
    history: list[str] = []
    dropped_sentences = 0
    for title, lines in kept_sections:
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("|"):
                new_lines.append(line)
                continue
            plain = normalize(CITATION_RE.sub("", stripped))
            if len(plain) >= 12 and sum(1 for item in history if _overlap(plain, item) >= 0.9) >= 2:
                dropped_sentences += 1
                continue
            if len(plain) >= 12:
                history.append(plain)
            new_lines.append(line)
        deduped.append((title, new_lines))
    if dropped_sentences:
        fixes.append({"code": "deduped_repeated_sentences", "detail": f"删除重复事实句 {dropped_sentences} 句"})

    def render(items: list[tuple[str | None, list[str]]]) -> str:
        chunks: list[str] = []
        for title, lines in items:
            block = "\n".join(lines).strip("\n")
            if title is None:
                if block.strip():
                    chunks.append(block.strip())
            elif block.strip():
                chunks.append(f"## {title}\n\n{block.strip()}")
        return "\n\n".join(chunks).strip()

    rendered = render(deduped)
    # 仍超限：从尾部丢非必需、且不是对比表格的小节
    while report_chars(rendered) > target:
        candidates = [
            index
            for index, (title, _lines) in enumerate(deduped)
            if title is not None and title not in required and title not in table_titles
        ]
        if not candidates:
            break
        index = candidates[-1]
        fixes.append({"code": "dropped_section", "detail": f"删除非必需小节：{deduped[index][0]}"})
        deduped.pop(index)
        rendered = render(deduped)
    # 第二轮更激进：摘要 2 句、发现 3 条、推演 1 条、其他小节 1 句
    if report_chars(rendered) > target and not strict:
        further, extra_fixes = compact_report(
            rendered,
            limit=target,
            strict=True,
            max_summary_sentences=2,
            max_findings=3,
            max_inferences=1,
            max_section_sentences=1,
        )
        fixes.extend(extra_fixes)
        rendered = further
    if report_chars(rendered) > target:
        rendered, fit_fixes = _fit_to_limit(rendered, target)
        fixes.extend(fit_fixes)
    if report_chars(rendered) > target:
        fixes.append(
            {
                "code": "still_over_limit",
                "detail": f"压缩后仍约 {report_chars(rendered)} 字（目标 {target}）",
            }
        )
    if marker:
        rendered = rendered.rstrip() + "\n\n" + marker.strip() + source
    return rendered.strip() + "\n", fixes


def validate_draft(
    draft: str,
    evidence: Sequence[dict[str, Any]],
    *,
    question: str = "",
    comparison: bool = False,
    controversy: bool = False,
    market: bool = False,
) -> dict[str, Any]:
    """引用与结构的硬校验：返回 issues；passed=False 时应重写或程序修复。

    ``question`` 非空时额外校验篇幅 / 重复事实；``comparison=True`` 时要求对比表格。
    （单测可以只传 draft/evidence，行为与旧版一致。）
    """
    issues: list[dict[str, Any]] = []
    text = draft or ""
    total = len(evidence)

    def add(code: str, detail: str, section: str = "") -> None:
        issues.append({"code": code, "detail": detail, "section": section})

    for title in REQUIRED_SECTIONS:
        if not _section_text(text, title):
            add("missing_section", f"缺少小节：{title}", title)
    if evidence and not _section_text(text, ANALYSIS_TITLE):
        add("missing_section", f"缺少小节：{ANALYSIS_TITLE}", ANALYSIS_TITLE)
    if evidence and not _citations(text):
        add("no_citations", "整篇报告没有任何引用角标——引用被写丢或被剪掉了")

    findings = _section_text(text, FINDINGS_TITLE)
    summary = _section_text(text, SUMMARY_TITLE)
    analysis = _section_text(text, ANALYSIS_TITLE)

    if summary:
        cites = _citations(summary)
        if not cites:
            add("summary_without_citation", "摘要没有任何引用角标", SUMMARY_TITLE)
        sentences = [item for item in _sentences(summary) if item]
        if len(sentences) < 2:
            add("summary_too_short", "摘要不足 2 句，无法给出结论+依据+边界", SUMMARY_TITLE)
        first = sentences[0] if sentences else ""
        if _VAGUE_SUMMARY_RE.search(first):
            add(
                "summary_vague",
                f"摘要第一句是空判断（{normalize(first)[:40]}），应直接点名机制或差异点",
                SUMMARY_TITLE,
            )
    if findings:
        total_cites = len(_citations(findings))
        if total_cites == 0:
            add("findings_without_citation", "关键发现整节没有任何引用", FINDINGS_TITLE)
        if not any(line.lstrip().startswith(("- ", "* ")) for line in findings.splitlines()):
            add("findings_not_bulleted", "关键发现不是分条列点，无法逐条校验引用", FINDINGS_TITLE)
        for line in findings.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not _citations(line):
                add(
                    "finding_without_citation",
                    f"关键发现有一条没有引用：{normalize(line)[:60]}",
                    FINDINGS_TITLE,
                )
    if analysis and INFER_TAG not in analysis:
        add("analysis_without_inference", "分析与推演小节没有任何【推断】标记", ANALYSIS_TITLE)

    for sentence in _sentences(text):
        cites = _citations(sentence)
        if len(cites) > 2:
            add("citation_stacking", f"同句引用 {len(cites)} 个：{normalize(sentence)[:60]}")
        # 推演句本来就是“基于证据的推理”，措辞不会逐字贴合原文，
        # 只校验编号合法性与数量，不做词面相关判定，否则会误杀真推演。
        inference = INFER_TAG in sentence or "推断" in sentence
        for index in cites:
            target = _citation_target(evidence, index)
            if target is None:
                add("citation_out_of_range", f"引用了不存在的证据编号 S{index}")
                continue
            if not inference and not _is_relevant(sentence, target):
                add(
                    "citation_mismatch",
                    f"S{index}「{normalize(target.get('claim') or target.get('quote') or '')[:40]}」"
                    f"与句子「{normalize(sentence)[:40]}」无词面关联",
                )

    # 对比报告必须有表格（否则模型很容易写成两段泛泛而谈的散文）
    if comparison and not has_markdown_table(text):
        add("missing_comparison_table", "对比类报告缺少 Markdown 对比表格（维度 × A × B，至少 3 个维度）")
    if controversy:
        support = re.search(r"支持方|正方|支持者|赞成|看好", text)
        oppose = re.search(r"反对方|反方|反对者|质疑|批评|看衰|反对", text)
        acknowledged_gap = re.search(r"未找到(另一方|反方|支持方)的?(公开)?观点", text)
        if not ((support and oppose) or acknowledged_gap):
            add("missing_opposing_view", "争议类报告缺少正反两方观点（至少各一个，且必须有引用）")
    if market:
        if not has_markdown_table(text):
            add("missing_market_table", "市场类报告缺少数据表（指标 | 数值 | 年份 | 口径 | 来源）")
        else:
            header = next(
                (line for line in text.splitlines() if line.strip().startswith("|")),
                "",
            )
            if "年份" not in header:
                add("market_table_missing_year", "市场数据表缺少「年份」列")
            if not ("口径" in header or "范围" in header):
                add("market_table_missing_scope", "市场数据表缺少「口径/范围」列，容易把细分市场当整体")
    if question:
        limit = _report_char_limit()
        chars = report_chars(text)
        if chars > limit:
            add("report_too_long", f"正文约 {chars} 字，超过 {limit} 字上限，需要压缩")
        for group in repeated_sentence_groups(text):
            add(
                "repeated_fact",
                f"同一事实出现 {group['count']} 次（「{group['sample'][:30]}」），全文最多两次",
            )
        for issue in number_issues(text, evidence, question)[:10]:
            add(issue["code"], issue["detail"])
        for row_index, row in enumerate(_table_data_rows(text), start=1):
            if "[S" not in row:
                add("table_row_without_citation", f"表格第 {row_index} 行没有任何引用角标")
    return {
        "issues": issues[:40],
        "issue_count": len(issues),
        "passed": not issues,
        "checked_citations": sum(len(_citations(sentence)) for sentence in _sentences(text)),
        "evidence_total": total,
    }


# ---------------------------------------------------------------- 程序修复


def analysis_lines(evidence: Sequence[dict[str, Any]], uncovered: Sequence[str] = ()) -> list[str]:
    """离线/降级时的“分析与推演”小节：只陈述证据之间的关系，不编造因果。"""
    lines = [f"## {ANALYSIS_TITLE}", ""]
    merged = [item for item in evidence if int(item.get("merged_count") or 1) >= 2]
    if merged:
        labels = "、".join(f"S{index + 1}" for index, item in enumerate(evidence) if int(item.get("merged_count") or 1) >= 2)
        lines.append(
            f"- {INFER_TAG} 多个来源对同一结论给出相近表述（{labels}），该结论的可靠性相对较高；"
            "但相同表述可能来自同一份原始资料，不构成独立验证。"
        )
    if len(evidence) >= 2:
        kinds = [str(item.get("kind") or "") for item in evidence]
        if "定义" in kinds and "机制" in kinds:
            first = kinds.index("定义") + 1
            mech = kinds.index("机制") + 1
            lines.append(
                f"- {INFER_TAG} 由 [S{first}] 的定义与 [S{mech}] 的实现机制可推知："
                "该能力不是单点函数，而是依赖状态持久化的一整套机制；若缺少持久化层，结论不成立。"
            )
    if uncovered:
        joined = "；".join(str(item) for item in uncovered[:3])
        lines.append(f"- {INFER_TAG} 以下子问题没有直接证据：{joined}，本报告不对其下结论。")
    if len(lines) == 3:  # 只有标题和空行：没有可写的推演
        lines.append(f"- {INFER_TAG} 当前证据只能支持事实陈述，尚不足以做跨来源推演。")
    lines.append("")
    return lines


def limits_lines(evidence: Sequence[dict[str, Any]], uncovered: Sequence[str] = ()) -> list[str]:
    """结构化的“局限与未证实”小节，替代“本报告仅供参考”式套话。"""
    lines = [f"## {LIMITS_TITLE}", ""]
    if uncovered:
        for item in list(uncovered)[:5]:
            lines.append(f"- 未覆盖：{str(item)[:80]}（没有可回溯到原文的证据，未下结论）。")
    else:
        lines.append("- 子问题均至少有一条证据，但“已覆盖”判定基于词面重叠，可能高估实际覆盖。")
    sources = {str(item.get("source_url") or "") for item in evidence if item.get("source_url")}
    kb = sum(1 for url in sources if url.startswith("kb://"))
    lines.append(
        f"- 证据共 {len(evidence)} 条，来自 {len(sources)} 个来源（其中本地知识库 {kb} 个）；"
        "网页内容可能过期或带立场，引用前应复核原文。"
    )
    promos = sum(1 for item in evidence if item.get("promo"))
    if promos:
        lines.append(f"- 有 {promos} 条证据属于营销/证言类表述，已降权，不作为主要结论依据。")
    snippet_count = sum(1 for item in evidence if item.get("snippet_only"))
    if snippet_count:
        lines.append(
            f"- 其中 {snippet_count} 条证据只来自搜索引擎摘要（本次未抓取到网页正文），"
            "只能作为线索，结论需打开原文复核。"
        )
    lines.append(f"- 标有{INFER_TAG}的内容是基于证据的推理，不是原文结论，也未做交叉验证。")
    lines.append("")
    return lines


def repair_draft(
    draft: str,
    evidence: Sequence[dict[str, Any]],
    *,
    uncovered: Sequence[str] = (),
) -> tuple[str, list[dict[str, Any]]]:
    """程序化修复：引用剪枝/去错配、补缺失小节。返回 (draft, fixes)。"""
    fixes: list[dict[str, Any]] = []
    text = draft or ""

    # 1) 逐句剪枝：>2 个引用时保留与句子最相关的 2 个；错配/越界引用直接移除
    def _fix_sentence(sentence: str) -> str:
        nonlocal fixes
        cites = _citations(sentence)
        if not cites:
            return sentence
        keep: list[int] = []
        inference = INFER_TAG in sentence or "推断" in sentence
        for index in cites:
            target = _citation_target(evidence, index)
            if target is None:
                fixes.append({"code": "removed_out_of_range", "detail": f"S{index}"})
                continue
            if not inference and not _is_relevant(sentence, target):
                fixes.append({"code": "removed_mismatch", "detail": f"S{index}"})
                continue
            keep.append(index)
        if len(keep) > 2:
            scored = sorted(keep, key=lambda i: _overlap(sentence, str(evidence[i - 1].get("quote") or "")), reverse=True)
            dropped = [i for i in keep if i not in scored[:2]]
            fixes.append({"code": "trimmed_stacking", "detail": "、".join(f"S{i}" for i in dropped)})
            keep = sorted(scored[:2])
        seen: list[int] = []
        for index in cites:
            if index in keep and index not in seen:
                seen.append(index)
        if not seen and keep:
            seen = sorted(keep)
        # 角标必须放回句末标点之前，不能把整行所有角标挪到行尾：
        # 否则一句话失去引用、另一句堆了 4 个角标，校验结果反而变差。
        bare = CITATION_RE.sub("", sentence).rstrip()
        tail = re.search(r"([。！？!?；;，,、]+)$", bare)
        cites_text = "".join(f"[S{index}]" for index in seen)
        if tail:
            head = bare[: tail.start()].rstrip()
            return f"{head}{cites_text}{tail.group(1)}"
        return bare + cites_text

    def _fix_line(line: str) -> str:
        # 表格行由 scrub_uncited_table_rows 单独处理；这里不要按句子重排角标，
        # 否则“| 指标 | 值 | [S1] |”里的引用会被判成错配并删掉。
        if line.lstrip().startswith("|"):
            return line
        bullet = _BULLET_RE.match(line)
        prefix = line[: bullet.end()] if bullet else line[: len(line) - len(line.lstrip())]
        body = line[len(prefix) :]
        if not _citations(body):
            return line
        tokens = re.split(r"(?<=[。！？!?；;])", body)
        return prefix + "".join(_fix_sentence(token) for token in tokens)

    replaced: list[str] = []
    for line in text.splitlines():
        replaced.append(_fix_line(line))
    text = "\n".join(replaced)
    # 去掉被剪掉的角标留下的“空格 + 标点”尾巴
    text = re.sub(r"[ \t]+([。，；：！？、])", r"\1", text)
    text = re.sub(r"([。，；：！？])\s*([。，；：！？])", r"\2", text)
    text = re.sub(r"（\s*）|\(\s*\)", "", text)

    # 2) 缺“关键发现”：用证据逐条生成（保证每条都有角标、条数可控）
    if evidence and not _section_text(text, FINDINGS_TITLE):
        block = "\n".join([f"## {FINDINGS_TITLE}", "", *find_bullets(evidence)])
        if _section_text(text, ANALYSIS_TITLE):
            head, _, tail = text.partition(f"## {ANALYSIS_TITLE}")
            text = f"{head.rstrip()}\n\n{block}\n\n## {ANALYSIS_TITLE}{tail}"
        elif _section_text(text, LIMITS_TITLE):
            head, _, tail = text.partition(f"## {LIMITS_TITLE}")
            text = f"{head.rstrip()}\n\n{block}\n\n## {LIMITS_TITLE}{tail}"
        else:
            text = f"{text.rstrip()}\n\n{block}"
        fixes.append({"code": "inserted_findings", "detail": FINDINGS_TITLE})

    # 3) 缺“分析与推演”：补一节仅陈述证据关系的推演（明确标注推断）
    if evidence and not _section_text(text, ANALYSIS_TITLE):
        block = "\n".join(analysis_lines(evidence, uncovered))
        if _section_text(text, LIMITS_TITLE):
            head, _, tail = text.partition(f"## {LIMITS_TITLE}")
            text = f"{head.rstrip()}\n\n{block}\n## {LIMITS_TITLE}{tail}"
        else:
            text = f"{text.rstrip()}\n\n{block}"
        fixes.append({"code": "inserted_analysis", "detail": ANALYSIS_TITLE})

    # 4) 缺“局限与未证实”：用结构化局限补上
    if not _section_text(text, LIMITS_TITLE):
        text = f"{text.rstrip()}\n\n" + "\n".join(limits_lines(evidence, uncovered))
        fixes.append({"code": "inserted_limits", "detail": LIMITS_TITLE})

    return text, fixes
