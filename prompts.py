"""所有节点的提示词。

设计原则：提示词本身也承担“不许编造来源”的约束，节点里再做一次程序化校验，
不能只依赖模型自觉。
"""

from __future__ import annotations

from typing import Any, Iterable

PLAN_SYSTEM = """你是一个专业的技术搜索专家。用户提出一个研究问题，你要先拆解问题，再转化为最适合搜索引擎的关键词组合。

规则：
1. 先把研究问题拆解成 3~5 个可独立检索的子问题（覆盖：定义/现状、关键机制或原因、方法或应对、案例或数据）。
   用一行 JSON 输出子问题：{"sub_questions": ["子问题1", "子问题2", "子问题3"]}
2. 如果问的是「如何实现 / 原理 / 教程（How）」，请在关键词中强制加入对应的英文或中文技术词，如「代码示例」「tutorial」「documentation」「implementation」。
3. 如果涉及特定框架（如 LangGraph），请加上官方文档的限定词，例如 "site:langchain.com" 或 "site:github.com"。
4. 去除所有口语化表达（如「我想知道」「怎么弄」「请问」「帮我」）。
5. 每个搜索词要能独立检索，不要编造 URL；不要输出解释。

提交方式：先输出子问题 JSON，再调用 search_web 工具 2~3 次，每次 query 填一个关键词组合；不要只在正文里写搜索词。"""


def plan_user(question: str, context: str = "") -> str:
    prior = f"""

上一轮研究上下文（用户正在追问，只用于避免重复检索，不要逐条复述）：
{context.strip()}""" if context.strip() else ""
    return f"""用户问题：{question}{prior}

请先拆解出 3~5 个子问题（JSON），再按规则生成 2~3 个搜索关键词组合，并通过 search_web 工具提交。"""


PLAN_JSON_SYSTEM = """你是一个专业的技术搜索专家。用户提出一个研究问题，你要把它转化为 2~3 个最适合在搜索引擎中使用的关键词组合。

规则：
1. 如果问的是「如何实现 / 原理 / 教程（How）」，请在关键词中强制加入对应的英文或中文技术词，如「代码示例」「tutorial」「documentation」「implementation」。
2. 如果涉及特定框架（如 LangGraph），请加上官方文档的限定词，例如 "site:langchain.com" 或 "site:github.com"。
3. 去除所有口语化表达（如「我想知道」「怎么弄」「请问」「帮我」）。

只输出一个 JSON 字符串数组，例如 ["搜索词1", "搜索词2"]，不要输出任何其他内容。"""


def plan_json_user(question: str, context: str = "") -> str:
    prior = f"""

上一轮研究上下文（用户正在追问，仅用于避免重复检索）：
{context.strip()}""" if context.strip() else ""
    return f"""用户问题：{question}{prior}

请直接输出 2~3 个搜索词的 JSON 数组。"""


SUMMARIZE_SYSTEM = """你是一个严谨的研究助理。你的任务是从给定的网页正文中，提取出能回答【用户问题】的核心事实证据。

【提取规则】（必须严格遵守）：
1. 绝不提取废话：不要提取广告、欢迎语、博主个人感慨（如“无脑跟着学就行了”）、导航栏文字。
2. 必须是完整句子：严禁输出半截句子（如结尾是 “and”、“the” 或者逗号）。提取的片段必须包含完整的语法结构。
3. 宁缺毋滥：如果网页内容只是在介绍基本概念，完全没有提到【用户问题】所问的具体“实现步骤/方法/细节”，就不要返回该条证据。绝对不允许用泛泛而谈的介绍来凑数；全部不相关时 evidence 返回空数组。
4. 保留代码或 API 名：如果涉及具体函数（如 `interrupt`），必须在 quote 中完整保留。
5. 必须带出处：每条证据都要给出 source_url（必须原样来自 sources）和 source_snippet（网页中的小标题或段落定位，如“小标题二 / 第 3 段”）。
6. 只能使用 sources 提供的正文，禁止引入外部知识；quote 必须逐字摘录，不得改写、拼接或翻译。

只输出严格 JSON，用 evidence 数组包一层（便于逐条校验来源）：
{"evidence": [{"evidence_text": "完整的原文句子", "quote": "逐字原文", "source_url": "https://...", "source_snippet": "小标题或段落定位", "relevance_reason": "为什么这段能回答用户问题", "confidence": 0.0}], "note": "没有证据时说明原因"}
confidence 取 0~1，表示该证据对研究问题的支持强度。"""


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def sources_block(sources: Iterable[dict[str, Any]], per_source: int = 1800) -> str:
    parts: list[str] = []
    for idx, src in enumerate(sources, start=1):
        parts.append(
            f"[S{idx}] url: {src.get('url', '')}\n"
            f"标题: {src.get('title', '')}\n"
            f"正文:\n{_clip(src.get('content', ''), per_source)}"
        )
    return "\n\n".join(parts)


def summarize_user(question: str, sources: Iterable[dict[str, Any]]) -> str:
    return f"""【用户问题】：{question}

【网页正文】（多个来源）：
{sources_block(sources)}

请按提取规则输出 JSON；如果所有来源都只是在介绍基本概念、回答不了用户问的步骤/方法/细节，直接返回空 evidence 数组。"""


DRAFT_SYSTEM = """你是一个资深的技术研究员。请根据给定的【证据列表】为用户撰写一份结构化的研究报告。

【撰写要求】
1. 先总结，后分点：报告第一段“摘要”必须用通顺的 2~3 句话，综合所有证据直接回答用户的问题
   （例如“根据官方文档，LangGraph 主要通过实现 X 和 Y 来达到 Z 目的”），不要复述问题，也不要写“本文将……”。
2. 消除重复：若多条证据表达同一个意思，合并为一条，并在引用时写成 [S1][S2] 这种连续角标。
3. 强制引用：每一个关键结论的末尾都必须跟上方括号引用，如 [S1]；引用编号只能来自证据列表，
   不得引用不存在的编号，也不得用常识补写证据之外的内容。
4. 结构要求：使用 Markdown，且只包含以下三个小节（顺序固定）：
   - ## 摘要：直接回答问题的 2~3 句总结。
   - ## 关键发现：分条列点，每条必须包含 1~2 个引用角标。
   - ## 局限与未证实：证据不足以回答的方面写“当前资料未覆盖 XX 方面”。
5. 语气专业：使用客观、书面化的语言，禁止“今天给大家”“我们来看看”等口语表达。
6. 只允许使用证据列表中的内容：不得补写证据之外的事实、数字、结论或 URL；正文里不要写裸 URL，
   来源由程序统一在文末列出。
7. 不要输出“来源清单 / References”，程序会按证据自动追加。"""


def evidence_block(evidence: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for idx, item in enumerate(evidence, start=1):
        parts.append(
            f"[S{idx}] claim: {item.get('claim', '')}\n"
            f"    来源: {item.get('source_url', '')}\n"
            f"    定位: {item.get('source_snippet', '')}\n"
            f"    原文: {item.get('quote', '')}\n"
            f"    相关性: {item.get('relevance_reason', '')}\n"
            f"    置信度: {item.get('confidence', 0)}"
        )
    return "\n\n".join(parts) if parts else "（无可用证据）"


def draft_user(
    question: str,
    evidence: Iterable[dict[str, Any]],
    sources: Iterable[dict[str, Any]],
    feedback: str = "",
) -> str:
    revision = f"\n人工修改意见（上一版草稿）：{feedback}\n" if feedback else ""
    return f"""【用户问题】：{question}
{revision}
【证据列表】（编号 S1、S2…，引用时必须使用这些编号）：
{evidence_block(evidence)}

【来源类型】source_url 以 `kb://` 开头的是本地知识库片段（定义/背景/项目内结论可优先引用）；
`https://` 开头的是本次联网抓取的网页，时效性内容优先引用网页。

请按撰写要求输出 Markdown 研究报告，只输出「摘要 / 关键发现 / 局限与未证实」三个小节。"""


def _norm_url(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def source_list_lines(
    evidence: Iterable[dict[str, Any]],
    sources: Iterable[dict[str, Any]],
) -> list[str]:
    """生成文末来源清单：按证据编号 S1、S2… 列出，同 URL 的多条证据合并为一行。"""
    evidence = list(evidence)
    sources = list(sources)
    title_by_key = {
        _norm_url(src.get("url", "")): str(src.get("title") or src.get("url") or "")
        for src in sources
    }
    url_by_key = {
        _norm_url(src.get("url", "")): str(src.get("url") or "").strip() for src in sources
    }

    if not evidence:
        return [
            f"{idx}. [{src.get('title') or src.get('url') or ''}]({src.get('url') or ''})"
            for idx, src in enumerate(sources, start=1)
        ]

    labels_by_key: dict[str, list[str]] = {}
    order: list[str] = []
    for idx, item in enumerate(evidence, start=1):
        url = str(item.get("source_url") or "").strip()
        if not url:
            continue
        key = _norm_url(url)
        if key not in labels_by_key:
            labels_by_key[key] = []
            order.append(key)
            url_by_key.setdefault(key, url)
        labels_by_key[key].append(f"S{idx}")

    lines: list[str] = []
    for key in order:
        labels = "".join(f"[{label}]" for label in labels_by_key[key])
        title = title_by_key.get(key) or url_by_key.get(key) or key
        if key.startswith("kb://"):
            lines.append(f"- 📚 知识库 · {title}（{labels}）")
        else:
            lines.append(f"- [{title}]({url_by_key[key]})（{labels}）")

    # 抓取到、但没有被引用的页面也列出来，保证“来源清单”完整，且不伪造编号
    for src in sources:
        key = _norm_url(src.get("url", ""))
        if not key or key in labels_by_key:
            continue
        title = str(src.get("title") or src.get("url") or "")
        if key.startswith("kb://"):
            lines.append(f"- 📚 知识库 · {title}")
        else:
            lines.append(f"- [{title}]({str(src.get('url') or '').strip()})")
    return lines

