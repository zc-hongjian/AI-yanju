"""研究质量闭环：子问题覆盖度 + 证据断言支持性校验。

设计成纯函数模块（不访问网络、不调用 LLM），这样：
1. 离线/mock 评测也能稳定衡量“证据是否覆盖子问题”“结论是否被原文支持”；
2. 单测可以直接构造证据，不依赖真实搜索；
3. 以后接 LLM verifier / reranker 时，这一层仍可作为降级与兜底。

注意：这里是“代理指标”，用于发现证据缺口与明显外推，不替代人工审阅。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+.\-]{1,}")
CJK_CHUNK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")

# 只过滤“问句脚手架/结构性词”，保留 aspect 词（挑战、数据、进展、案例、差异、原理、步骤…），
# 否则 coverage 会退化成“只要提到主题就算覆盖”，无法发现证据缺口。
_STRUCTURAL_WORDS = (
    "主要", "核心", "关键", "基本", "具体", "相关", "方面", "情况",
    "结论", "事实", "内容", "描述", "信息", "介绍", "概述", "说明",
    "解释", "包括", "以及", "进行", "一个",
    "研究", "哪些", "分别", "适合", "作用", "功能", "意义", "定义",
    "概念", "是什么", "有什么", "有什么用", "如何", "怎么", "为什么",
    "是否", "吗", "呢", "的", "与", "和", "及", "或",
)
_STRUCTURAL_RE = re.compile("|".join(re.escape(word) for word in _STRUCTURAL_WORDS))

# 子问题模板后缀：用于生成补检 query 的 aspect（不直接作为 coverage 术语）
_ASPECT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"关键数据|最新数据|最新进展|最新动态"), "最新 数据 进展"),
    (re.compile(r"主要挑战|主要原因|挑战与原因|困难与原因"), "挑战 原因"),
    (re.compile(r"权威来源|典型案例|案例|来源"), "案例 来源"),
    (re.compile(r"在不同|差异|区别|对比"), "对比 差异"),
    (re.compile(r"实现原理|底层原理|工作原理|原理"), "原理 实现"),
    (re.compile(r"具体步骤|实现步骤|操作步骤|代码示例|示例代码|步骤"), "步骤 代码 示例"),
    (re.compile(r"前置条件|依赖"), "前置条件 依赖"),
    (re.compile(r"常见问题|最佳实践|注意事项"), "常见问题 最佳实践"),
    (re.compile(r"官方文档|官方说明|文档"), "官方文档"),
)

# 判断“同一个结论”时使用的否定词；claim 与 quote 否定性不一致时降级为 partial。
_NEGATIONS = ("不", "没", "无", "未", "非", "无法", "不能", "不是", "not", "no", "never", "without", "cannot")


def normalize(text: Any) -> str:
    return " ".join(str(text or "").split()).strip()


def _bigrams(text: str) -> set[str]:
    """中文按字符 bigram、英文按词，用于宽松的语义重叠估计。"""
    lowered = normalize(text).lower()
    grams: set[str] = set()
    for word in LATIN_RE.findall(lowered):
        if len(word) > 1:
            grams.add(word)
    for chunk in CJK_CHUNK_RE.findall(lowered):
        for index in range(len(chunk) - 1):
            grams.add(chunk[index : index + 2])
    return grams


def coverage_terms(text: str) -> list[str]:
    """从子问题中抽出“需要被证据覆盖”的实词/技术词。

    - 去掉“主要/核心/是什么/如何/的/与”等脚手架；
    - 保留 aspect 词（数据、进展、挑战、案例、差异、原理、步骤…）；
    - 中文额外拆 2~4 字 n-gram，缓解中文无空格导致的匹配问题。
    """
    cleaned = _STRUCTURAL_RE.sub(" ", normalize(text))
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.strip().lower()
        if len(term) < 2 or term in seen:
            return
        seen.add(term)
        terms.append(term)

    for word in LATIN_RE.findall(cleaned):
        add(word)
    for chunk in CJK_CHUNK_RE.findall(cleaned):
        chunk = chunk.strip()
        if len(chunk) < 2:
            continue
        for size in (2, 3, 4):
            for start in range(0, len(chunk) - size + 1):
                add(chunk[start : start + size])
    return terms


def subquestion_core(sub_question: str) -> str:
    """去掉子问题尾部模板（“的关键数据与最新进展”等），留下主题短语。"""
    text = normalize(sub_question)
    text = re.sub(
        r"(?:的)?(?:核心结论|主要结论|主要事实|基本概念|基本定义|关键数据|最新数据|"
        r"最新进展|最新动态|主要挑战|主要原因|挑战与原因|困难与原因|权威来源|典型案例|"
        r"案例|来源|实现原理|底层原理|工作原理|具体步骤|实现步骤|操作步骤|代码示例|"
        r"示例代码|前置条件|依赖|准备条件|常见问题|最佳实践|注意事项|官方文档|官方说明|文档)"
        r"(?:与|和|及|或|、)?.*$",
        "",
        text,
    )
    text = re.sub(r"(?:是什么|有什么作用|有什么用|有哪些|如何实现|怎么实现|为什么|"
                  r"分别适合什么场景|分别适合做什么|怎么样|吗|呢)\s*[。？?]?$", "", text)
    text = text.strip(" ，。；;、：:？?！!")
    return text or normalize(sub_question)


def _aspect_for(sub_question: str) -> str:
    text = normalize(sub_question)
    for pattern, aspect in _ASPECT_RULES:
        if pattern.search(text):
            return aspect
    return ""


def _evidence_text(item: dict[str, Any]) -> str:
    return normalize(f"{item.get('claim') or ''} {item.get('quote') or ''}").lower()


def _coverage_units(text: str) -> tuple[list[str], list[str]]:
    """把子问题拆成“英文实体”和“中文语义块”。

    中文块不做 n-gram 全展开，而是在块内命中任意 2~4 字子串即算覆盖：
    这样“企业落地”被“企业”或“落地”覆盖都算有效，避免长词稀释分数。
    """
    cleaned = _STRUCTURAL_RE.sub(" ", normalize(text))
    latin = [word.lower() for word in LATIN_RE.findall(cleaned)]
    latin = list(dict.fromkeys(word for word in latin if len(word) > 1))
    chunks: list[str] = []
    for chunk in CJK_CHUNK_RE.findall(cleaned):
        chunk = chunk.strip()
        if len(chunk) >= 2 and chunk not in chunks:
            chunks.append(chunk)
    return latin, chunks


def _chunk_matches(chunk: str, corpus: str) -> bool:
    if chunk in corpus:
        return True
    for size in (2, 3, 4):
        if len(chunk) < size:
            continue
        for start in range(0, len(chunk) - size + 1):
            if chunk[start : start + size] in corpus:
                return True
    return False


def compute_coverage(
    sub_questions: Sequence[str],
    evidence: Iterable[dict[str, Any]],
    *,
    threshold: float = 0.6,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """计算每个子问题的证据覆盖度。

    返回 (coverage_items, summary)：
    - coverage_items: 每个子问题的 score / covered / matched_terms / evidence_ids；
    - summary: total / covered / rate / threshold / uncovered。
    """
    items = [item for item in evidence if isinstance(item, dict)]
    corpus = " ".join(_evidence_text(item) for item in items)
    coverage: list[dict[str, Any]] = []
    for sub_question in sub_questions:
        latin, chunks = _coverage_units(sub_question)
        matched_latin = [word for word in latin if word in corpus]
        matched_chunks = [chunk for chunk in chunks if _chunk_matches(chunk, corpus)]
        total_units = len(latin) + len(chunks)
        matched_units = len(matched_latin) + len(matched_chunks)
        score = matched_units / total_units if total_units else 0.0
        # 关键英文实体（LangGraph / interrupt / RAG…）缺失时，不能算高覆盖
        if latin and len(matched_latin) < len(latin):
            score = min(score, 0.45)
        covered = bool(total_units) and score >= threshold
        matched_terms = [*matched_latin, *matched_chunks]
        evidence_ids = [
            index + 1
            for index, item in enumerate(items)
            if any(
                word in _evidence_text(item)
                for word in matched_latin
            )
            or any(_chunk_matches(chunk, _evidence_text(item)) for chunk in chunks)
        ]
        coverage.append(
            {
                "sub_question": sub_question,
                "core": subquestion_core(sub_question),
                "covered": covered,
                "score": round(score, 3),
                "matched_terms": matched_terms[:8],
                "evidence_ids": evidence_ids[:8],
            }
        )
    total = len(coverage)
    covered_count = sum(1 for item in coverage if item["covered"])
    summary = {
        "total": total,
        "covered": covered_count,
        "rate": round(covered_count / total, 4) if total else 0.0,
        "threshold": threshold,
        "uncovered": [item["sub_question"] for item in coverage if not item["covered"]],
    }
    return coverage, summary


def gap_queries_for(
    coverage: Sequence[dict[str, Any]],
    *,
    question: str = "",
    existing_queries: Sequence[str] = (),
    limit: int = 2,
    max_results: int = 6,
) -> list[dict[str, Any]]:
    """为未覆盖子问题生成第二轮补检 query；跳过重复/过于宽泛的 query。"""
    existing_norm = {normalize(q).lower() for q in existing_queries if normalize(q)}
    question_norm = normalize(question).lower()
    queries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in coverage:
        if item.get("covered"):
            continue
        sub_question = str(item.get("sub_question") or "")
        core = str(item.get("core") or subquestion_core(sub_question)).strip()
        aspect = _aspect_for(sub_question)
        if aspect:
            query = f"{core} {aspect}".strip()
        else:
            query = re.sub(r"[。？?！!]+$", "", sub_question).strip()
        key = normalize(query).lower()
        if not key or key in seen:
            continue
        # 第一轮已经搜过同一 query，或只是原问题主题的重复，不再浪费一轮
        if key in existing_norm or (len(key) < 4 and key in question_norm):
            continue
        if question_norm and key == question_norm:
            continue
        seen.add(key)
        queries.append({"query": query[:120], "max_results": max_results})
        if len(queries) >= max(1, limit):
            break
    return queries


def claim_support(claim: Any, quote: Any) -> tuple[float, str]:
    """判断 claim 是否被 quote 支持，返回 (score, reason)。

    规则：
    - quote 整体包含 claim（离线抽取式证据）→ 1.0；
    - 中文 bigram / 英文词重叠率作为宽松代理；
    - claim 里的数字、英文实体在 quote 中缺失，或否定性不一致 → 降级；
    - 仅因同义改写导致的低重叠，仍保留为 partial（不误杀），只有明显无关才 unsupported。
    """
    claim_text = normalize(claim)
    quote_text = normalize(quote)
    if not claim_text:
        return 0.0, "空结论"
    if not quote_text:
        return 0.0, "空引文"
    if claim_text.lower() in quote_text.lower():
        return 1.0, "结论原文包含于引文"

    claim_grams = _bigrams(claim_text)
    quote_grams = _bigrams(quote_text)
    overlap = len(claim_grams & quote_grams) / len(claim_grams) if claim_grams else 0.0

    missing_numbers = set(NUMBER_RE.findall(claim_text)) - set(NUMBER_RE.findall(quote_text))
    claim_latin = {word.lower() for word in LATIN_RE.findall(claim_text)}
    quote_latin = {word.lower() for word in LATIN_RE.findall(quote_text)}
    missing_latin = claim_latin - quote_latin
    claim_neg = any(word in claim_text.lower() for word in _NEGATIONS)
    quote_neg = any(word in quote_text.lower() for word in _NEGATIONS)
    neg_mismatch = claim_neg != quote_neg

    score = overlap
    reasons: list[str] = [f"重叠率={overlap:.2f}"]
    if missing_numbers:
        score = min(score, 0.5)
        reasons.append(f"数字未在引文出现：{', '.join(sorted(missing_numbers))}")
    if missing_latin:
        score = min(score, 0.5)
        reasons.append(f"英文实体未在引文出现：{', '.join(sorted(missing_latin))}")
    if neg_mismatch:
        score = min(score, 0.4)
        reasons.append("否定性不一致")
    return round(score, 3), "；".join(reasons)


def _support_label(score: float, *, has_number_gap: bool, has_entity_gap: bool, neg_mismatch: bool) -> str:
    if has_number_gap or has_entity_gap or neg_mismatch:
        return "partial" if score >= 0.25 else "unsupported"
    if score >= 0.55:
        return "supported"
    if score >= 0.25:
        return "partial"
    return "unsupported"


def verify_evidence(evidence: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """逐条校验 claim 是否被 quote 支持；只丢弃明显无关（unsupported）的条目。

    返回 (kept, summary)。kept 里的每条 evidence 增加 support / support_score / support_reason 字段，
    供报告与 final_output 审计；summary 记录 supported / partial / unsupported 数量。
    """
    kept: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    counts = {"supported": 0, "partial": 0, "unsupported": 0}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        claim = item.get("claim") or item.get("evidence_text") or ""
        quote = item.get("quote") or ""
        score, reason = claim_support(claim, quote)
        claim_text = normalize(claim)
        quote_text = normalize(quote)
        missing_numbers = bool(set(NUMBER_RE.findall(claim_text)) - set(NUMBER_RE.findall(quote_text)))
        missing_latin = bool(
            {word.lower() for word in LATIN_RE.findall(claim_text)}
            - {word.lower() for word in LATIN_RE.findall(quote_text)}
        )
        neg_mismatch = any(word in claim_text.lower() for word in _NEGATIONS) != any(
            word in quote_text.lower() for word in _NEGATIONS
        )
        support = _support_label(
            score,
            has_number_gap=missing_numbers,
            has_entity_gap=missing_latin,
            neg_mismatch=neg_mismatch,
        )
        counts[support] += 1
        enriched = dict(item)
        enriched.update({"support": support, "support_score": score, "support_reason": reason})
        records.append(
            {
                "claim": claim_text[:160],
                "source_url": str(item.get("source_url") or ""),
                "support": support,
                "score": score,
                "reason": reason,
            }
        )
        if support != "unsupported":
            kept.append(enriched)

    total = len(records)
    return kept, {
        "total": total,
        "supported": counts["supported"],
        "partial": counts["partial"],
        "unsupported": counts["unsupported"],
        "dropped": counts["unsupported"],
        "support_rate": round(counts["supported"] / total, 4) if total else 0.0,
        "usable_rate": round((counts["supported"] + counts["partial"]) / total, 4) if total else 0.0,
        "records": records[:30],
    }
