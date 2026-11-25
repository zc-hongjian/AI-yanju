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
    (re.compile(r"主要挑战|主要原因|挑战与原因|困难与原因|风险与挑战"), "挑战 原因 风险"),
    (re.compile(r"权威来源|典型案例|案例|来源"), "案例 来源"),
    (re.compile(r"主要参与者|主要玩家|竞争格局|市场格局|竞争者|厂商|格局|玩家"), "参与者 竞争 格局"),
    (re.compile(r"驱动因素|增长驱动|驱动|推动"), "驱动 因素"),
    (re.compile(r"争议焦点|支持方观点|反对方观点|不同观点|争议|观点|立场"), "争议 观点 立场"),
    (re.compile(r"市场规模|市场份额|市场趋势|市场前景|规模|份额|营收|收入"), "市场规模 份额 数据"),
    (re.compile(r"在不同|差异|区别|对比"), "对比 差异"),
    (re.compile(r"实现原理|底层原理|工作原理|原理"), "原理 实现"),
    (re.compile(r"具体步骤|实现步骤|操作步骤|代码示例|示例代码|步骤"), "步骤 代码 示例"),
    (re.compile(r"前置条件|依赖"), "前置条件 依赖"),
    (re.compile(r"常见问题|最佳实践|注意事项"), "常见问题 最佳实践"),
    (re.compile(r"官方文档|官方说明|文档"), "官方文档"),
)

# ---------------------------------------------------------------- 覆盖度：按“方面”判定
# 旧实现只看子问题与证据的词面重叠：中文块只要命中 2 个字（如“市场”）就算覆盖，
# 于是 1 条“市场规模”证据也能把“玩家 / 驱动 / 风险”等子问题全判成已覆盖（实测 5/5 假阳性）。
# 现在把子问题拆成「主题（subquestion_core）」+「方面（aspect）」两层：
#   - 主题：英文实体必须命中，中文主题块要求至少 3 个连续字命中（2 字块要求整块命中）；
#   - 方面：问题侧识别词 + 证据侧信号词两边都命中，且同一子问题的方面必须全部满足。
_CoverageAspect = tuple[str, str, re.Pattern[str], re.Pattern[str]]
_COVERAGE_ASPECTS: tuple[_CoverageAspect, ...] = (
    (
        "data",
        "数据",
        re.compile(r"数据|数字|规模|体量|份额|营收|收入|增速|增长率|渗透率|指标|统计|金额|投融资|估值|用户数", re.I),
        re.compile(
            r"\d+(?:\.\d+)?\s*(?:%|％|亿|万|千|百万|十亿|元|美元|台|辆|人|家|个|倍|个百分点)"
            r"|同比增长|环比|达到.{0,8}\d",
            re.I,
        ),
    ),
    (
        "trend",
        "趋势",
        re.compile(r"趋势|进展|动向|最新|前景|预测|展望|变化|发展|走向", re.I),
        re.compile(r"增长|下降|上升|预计|预测|展望|最新|发布|推出|首次|20\d{2}\s*年|同比|环比", re.I),
    ),
    (
        "challenge",
        "挑战",
        re.compile(r"挑战|风险|瓶颈|难点|局限|障碍|不足|痛点|问题|困难|顾虑|隐患", re.I),
        re.compile(
            r"挑战|风险|瓶颈|难点|局限|障碍|不足|痛点|困难|顾虑|隐患|隐私|安全|合规|监管|成本高|难以",
            re.I,
        ),
    ),
    (
        "player",
        "参与者",
        re.compile(r"参与者|玩家|厂商|竞争者|竞争格局|格局|头部|公司|企业|客户|对手|替代|排名|领先|占据", re.I),
        re.compile(r"竞争|份额|格局|排名|领先|第一|头部|厂商|玩家|对手|替代|占据|企业|公司|客户", re.I),
    ),
    (
        "driver",
        "驱动因素",
        re.compile(r"驱动|推动|原因|因素|背后|为什么", re.I),
        re.compile(r"驱动|推动|带动|受益|原因|由于|因为|取决于|依赖于", re.I),
    ),
    (
        "official",
        "权威来源",
        re.compile(r"官方|文档|权威|来源|出处|规范|标准|白皮书|报告", re.I),
        re.compile(
            r"官方|文档|docs|documentation|github|规范|标准|白皮书|报告|研究院|gartner|idc|forrester",
            re.I,
        ),
    ),
    (
        "case",
        "案例",
        re.compile(r"案例|实例|实践|落地|试点|客户|采用", re.I),
        re.compile(r"案例|例如|比如|实践中|客户|采用|部署|落地|试点|某公司|一家", re.I),
    ),
    (
        "principle",
        "原理",
        re.compile(r"原理|机制|架构|工作方式|底层|内部|如何实现", re.I),
        re.compile(r"原理|机制|架构|通过|依赖|内部|流程|模块|组件|基于", re.I),
    ),
    (
        "steps",
        "步骤",
        re.compile(r"步骤|实现|用法|安装|配置|命令|代码|教程|API|接口|示例", re.I),
        re.compile(r"步骤|安装|配置|命令|代码|调用|使用|执行|import|pip|npm|curl|示例|如下", re.I),
    ),
    (
        "difference",
        "差异",
        re.compile(r"差异|区别|对比|优劣|优缺点|适合|不同|相比", re.I),
        re.compile(r"相比|对比|不同|区别|优于|不如|适合|优势|劣势", re.I),
    ),
    (
        "stance",
        "观点立场",
        re.compile(r"争议|观点|立场|支持|反对|正方|反方|争论|分歧", re.I),
        re.compile(r"认为|指出|主张|反对|支持|质疑|争议|分歧|观点|立场", re.I),
    ),
)
_ASPECT_EVIDENCE: dict[str, re.Pattern[str]] = {key: signal for key, _, _, signal in _COVERAGE_ASPECTS}

# 主题里过于宽泛的词：单独命中不能算“主题被覆盖”（“市场”“行业”等词几乎每篇证据都有）
_GENERIC_TOPIC_WORDS = frozenset(
    {
        "市场", "行业", "技术", "发展", "企业", "公司", "产品", "领域", "现状", "趋势",
        "数据", "挑战", "竞争", "格局", "参与", "玩家", "争议", "观点", "案例", "来源",
        "问题", "风险", "原因", "因素", "影响", "作用", "优点", "缺点", "优势", "劣势",
        "规模", "份额", "方法", "方式", "系统", "平台", "工具", "模型", "能力", "价值",
        "意义", "概念", "定义", "介绍", "概述", "内容", "信息", "应用", "场景", "需求",
        "用户", "客户", "方案", "策略", "背景", "情况", "方面",
        # 技术类子问题的模板词：靠“函数/代码/步骤”等词本身不能证明主题被覆盖
        "函数", "代码", "示例", "步骤", "原理", "机制", "架构", "依赖", "配置",
        "安装", "接口", "参数", "组件", "模块", "流程", "命令", "文档", "教程",
        # 市场/趋势类子问题的模板词
        "增长", "增速", "驱动", "参与者", "厂商", "头部", "机遇", "前景",
    }
)

# 主题里的年份只是限定范围，不是主题词：`2027年中国…` 不能要求证据里出现“年中国”。
_YEAR_RE = re.compile(r"(?:19|20)\d{2}\s*年?")

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
        r"未来发展趋势比较|未来趋势比较|发展趋势比较|正面观点分析|反面观点分析|"
        r"正面观点|反面观点|正方观点|反方观点|观点分析|定义及区别|定义与区别|案例研究|"
        r"案例|来源|实现原理|底层原理|工作原理|具体步骤|实现步骤|操作步骤|代码示例|"
        r"示例代码|前置条件|依赖|准备条件|常见问题|最佳实践|注意事项|官方文档|官方说明|文档|"
        r"市场规模|市场份额|市场趋势|市场格局|市场前景|主要参与者|主要玩家|典型玩家|头部玩家|"
        r"竞争者|竞争格局|驱动因素|增长驱动|主要风险|风险与挑战|趋势判断|争议焦点|"
        r"支持方观点|反对方观点|不同观点|证据强度|落地案例|商业模式|产业链|政策环境)"
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
    """证据正文：claim + quote + 结构化数字（numbers 是模型显式抽出的数值/年份/口径）。"""
    parts = [str(item.get("claim") or ""), str(item.get("quote") or "")]
    numbers = item.get("numbers")
    if isinstance(numbers, (list, tuple)):
        parts.extend(str(value) for value in numbers)
    elif isinstance(numbers, str):
        parts.append(numbers)
    return normalize(" ".join(parts)).lower()


def _coverage_units(text: str) -> tuple[list[str], list[str]]:
    """把子问题拆成“英文实体”和“中文语义块”。

    中文块不做 n-gram 全展开，而是在块内命中任意 2~4 字子串即算覆盖：
    这样“企业落地”被“企业”或“落地”覆盖都算有效，避免长词稀释分数。
    """
    cleaned = _STRUCTURAL_RE.sub(" ", normalize(text))
    cleaned = _YEAR_RE.sub(" ", cleaned)
    latin = [word.lower() for word in LATIN_RE.findall(cleaned)]
    latin = list(dict.fromkeys(word for word in latin if len(word) > 1))
    chunks: list[str] = []
    for chunk in CJK_CHUNK_RE.findall(cleaned):
        chunk = chunk.strip()
        if len(chunk) >= 2 and chunk not in chunks:
            chunks.append(chunk)
    return latin, chunks


def _chunk_matches(chunk: str, corpus: str) -> bool:
    """中文块匹配：要求命中 min(len(chunk), 3) 个连续字。

    旧实现只要 2 字重叠就算命中，于是“市场规模”“竞争格局”仅凭“市场”“竞争”
    就被判成已覆盖。现在 2 字块要求整块命中，更长的块要求 3 字连续命中。
    """
    if not chunk:
        return False
    if chunk in corpus:
        return True
    size = min(len(chunk), 3)
    return any(
        chunk[start : start + size] in corpus
        for start in range(0, len(chunk) - size + 1)
    )


def _aspects_for(sub_question: str) -> list[tuple[str, str]]:
    """子问题里显式要求的“方面”（数据/趋势/挑战/参与者/…）。

    “挑战与原因”这种模板里原因只是挑战的解释，合并成一个方面，避免要求
    证据同时出现“挑战”和“因为”两类词。
    """
    text = normalize(sub_question)
    found: list[tuple[str, str]] = []
    for key, label, question_pattern, _ in _COVERAGE_ASPECTS:
        if question_pattern.search(text):
            found.append((key, label))
    keys = {key for key, _ in found}
    if "challenge" in keys and "driver" in keys:
        found = [item for item in found if item[0] != "driver"]
    return found


def _aspect_matches_item(key: str, text: str, item: dict[str, Any]) -> bool:
    pattern = _ASPECT_EVIDENCE.get(key)
    if pattern is not None and pattern.search(text):
        return True
    # 结构化字段兜底：numbers 非空说明证据本身带数值；“支持/反对”是显式立场
    if key == "data":
        numbers = item.get("numbers")
        return isinstance(numbers, (list, tuple)) and bool(numbers)
    if key == "stance":
        return str(item.get("stance") or "").strip() in {"支持", "反对", "中立"}
    return False


def compute_coverage(
    sub_questions: Sequence[str],
    evidence: Iterable[dict[str, Any]],
    *,
    threshold: float = 0.6,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """计算每个子问题的证据覆盖度（主题 + 方面两层判定）。

    返回 (coverage_items, summary)：
    - coverage_items: 每个子问题的 score / covered / matched_terms / evidence_ids /
      aspects / missing_aspects；
    - summary: total / covered / rate / threshold / uncovered。
    """
    items = [item for item in evidence if isinstance(item, dict)]
    texts = [_evidence_text(item) for item in items]
    corpus = " ".join(texts)
    coverage: list[dict[str, Any]] = []
    for sub_question in sub_questions:
        core = subquestion_core(sub_question)
        latin, chunks = _coverage_units(core)
        # 通用词（市场/行业/企业…）不能单独作为主题覆盖依据
        topic_chunks = [chunk for chunk in chunks if chunk not in _GENERIC_TOPIC_WORDS]
        if not latin and not topic_chunks:
            # 主题被“的/与”拆成多个通用词时，退回用子问题的 3 字以上块判断
            _, fallback_chunks = _coverage_units(sub_question)
            topic_chunks = [chunk for chunk in fallback_chunks if len(chunk) >= 3][:3]
        aspects = _aspects_for(sub_question)
        matched_latin = [word for word in latin if word in corpus]
        matched_chunks = [chunk for chunk in topic_chunks if _chunk_matches(chunk, corpus)]
        missing_aspects = [
            (key, label)
            for key, label in aspects
            if not any(_aspect_matches_item(key, text, item) for text, item in zip(texts, items))
        ]
        subject_units = len(latin) + len(topic_chunks)
        subject_matched = len(matched_latin) + len(matched_chunks)
        all_aspects_matched = len(aspects) - len(missing_aspects)
        total_units = subject_units + len(aspects)
        score = (subject_matched + all_aspects_matched) / total_units if total_units else 0.0
        # 英文实体必须全部命中；中文主题块命中任意一个即可（剩下的模板词由方面判定兜底）
        subject_ok = (
            subject_units > 0
            and len(matched_latin) == len(latin)
            and (not topic_chunks or bool(matched_chunks))
        )
        # 关键英文实体（LangGraph / interrupt / RAG…）缺失时，不能算高覆盖
        if latin and len(matched_latin) < len(latin):
            score = min(score, 0.45)
        if missing_aspects:
            score = min(score, 0.5)
        if not subject_ok:
            score = min(score, 0.35)
        covered = bool(total_units) and subject_ok and not missing_aspects and score >= threshold
        matched_terms = [*matched_latin, *matched_chunks, *(label for _, label in aspects)]
        evidence_ids = [
            index + 1
            for index, (item, text) in enumerate(zip(items, texts))
            if any(word in text for word in matched_latin)
            or any(_chunk_matches(chunk, text) for chunk in matched_chunks)
            or any(
                _aspect_matches_item(key, text, item)
                for key, label in aspects
                if (key, label) not in missing_aspects
            )
        ]
        coverage.append(
            {
                "sub_question": sub_question,
                "core": core,
                "covered": covered,
                "score": round(score, 3),
                "matched_terms": matched_terms[:8],
                "evidence_ids": evidence_ids[:8],
                "aspects": [label for _, label in aspects],
                "missing_aspects": [label for _, label in missing_aspects],
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
