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
        if not stripped or stripped.startswith("#"):
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


def validate_draft(draft: str, evidence: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """引用与结构的硬校验：返回 issues；passed=False 时应重写或程序修复。"""
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
