"""所有节点的提示词。

设计原则：提示词本身也承担“不许编造来源”的约束，节点里再做一次程序化校验，
不能只依赖模型自觉。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# ---------------------------------------------------------------- 问题类型识别
COMPARISON_PATTERN = re.compile(
    r"对比|比较|区别|差异|优劣|优缺点|哪个更|哪家|横向|选型|versus|\bvs\b|\bVS\b", re.I
)


def is_comparison_question(question: str) -> bool:
    """是否是对比/选型类问题（A vs B、对比、区别、哪个更适合…）。"""
    return bool(COMPARISON_PATTERN.search(str(question or "")))


# 本地知识库证据的作用域约束：项目内文档不能当外部产品结论
KB_SCOPE_RULE = """【本地知识库使用规则】
- 来源以 `kb://` 开头、或证据里标注「本地项目文档」的，是本项目自己的设计/实现记录；
- 它只能说明「本项目当时怎么设计、为什么这样取舍」，**不能当作外部产品/框架/库的通用能力、性能参数或行业事实**；
- 如果问题在问外部产品对比或通用技术选型，禁止用本地项目文档支撑产品结论（最多在「局限」里说明本项目已知的约束）。"""


def comparison_instructions(question: str) -> str:
    """对比类问题专用的写作指令；非对比问题返回空串。"""
    if not is_comparison_question(question):
        return ""
    return """
【对比报告专属指令】（本次问题要求对比，必须严格遵守）
1. 强制表格：正文必须包含一个 Markdown 表格（表头形如「维度 | A | B | 结论/适用场景」，至少 3 个维度，
   如数据规模、部署难度、性能、成本、适用团队）；表格里的结论必须来自证据并带 [S#] 角标。
2. 严禁身份混淆：A 的属性/数字只能写在 A 列，绝不能张冠李戴给 B；每条对比都要能追溯到对应来源，
   做不到就写「证据不足，未比较」，不要用常识补。
3. 冲突消解：不同来源对同一指标说法矛盾时（如官方称十亿级、第三方称轻量部署下受限），
   必须在表格旁或正文里两句都写清，并说明适用条件（部署规模/版本/团队能力），不得只选一边。
4. 拒绝废话：不要写「A 是一个好用的数据库」这类空评价，直接给参数、限制、适用场景和代价。
5. 若某一维度只有一方有证据，明确标注「另一方缺证据」，不要替缺失方编造。"""


PLAN_SYSTEM = """你是一个专业的技术搜索专家。用户提出一个研究问题，你要先拆解问题，再转化为最适合搜索引擎的关键词组合。

规则：
1. 先把研究问题拆解成 3~5 个可独立检索的子问题（覆盖：定义/现状、关键机制或原因、方法或应对、案例或数据）。
   用一行 JSON 输出子问题：{"sub_questions": ["子问题1", "子问题2", "子问题3"]}
2. 如果问的是「如何实现 / 原理 / 教程（How）」，请在关键词中强制加入对应的英文或中文技术词，如「代码示例」「tutorial」「documentation」「implementation」。
3. 如果涉及特定框架（如 LangGraph），请加上官方文档的限定词，例如 "site:langchain.com" 或 "site:github.com"。
4. 去除所有口语化表达（如「我想知道」「怎么弄」「请问」「帮我」）。
5. 每个搜索词要能独立检索，不要编造 URL；不要输出解释。
6. 如果问题是 How / 教程 / 实现类：必须至少有 1 条查询带「代码示例」或「code example」，
   以及 1 条带 `site:` 官方文档的查询（程序会兜底补齐，但模型应尽量自己产出）。

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
4. 如果问题是 How / 教程 / 实现类：至少 1 条查询带「代码示例」或「code example」，
   以及 1 条带 `site:` 官方文档的查询；程序会对缺失的查询兜底补齐。

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
1. 先总结，后分点：报告第一段“摘要”必须用通顺的 2~3 句话，综合所有证据直接回答用户的问题。
   写法固定为「结论 → 关键依据 → 适用边界」：第一句给判断，第二句给支撑它的依据，第三句说明
   这个判断在什么前提下成立。不要复述问题，不要写“本文将……”，不要用“综上所述”凑句。
2. 消除重复：若多条证据表达同一个意思，合并为一条，并在引用时写成 [S1][S2] 这种连续角标。
   禁止连续两句表达同一意思，禁止把一条证据改写成两条并列的“发现”。
3. 强制引用：每一个关键结论的末尾都必须跟上方括号引用，如 [S1]；引用编号只能来自证据列表，
   不得引用不存在的编号，也不得用常识补写证据之外的内容。
4. 引用最小集：一条结论最多跟 2 个角标；同一句话不要堆 3 个以上来源，不同结论不要共用一个角标；
   引用必须真的支撑它前面那句话，不要为了凑数把来源挂在无关的句尾。
5. 结构要求：使用 Markdown，且必须包含以下四个小节（顺序固定）：
   - ## 摘要：2~3 句，按「结论 → 依据 → 边界」写。
   - ## 关键发现：分条列点，每条先给结论再给一句解释，结尾带 1~2 个引用角标。
   - ## 分析与推演：每条以【推断】开头，写清依据哪几条证据、在什么前提下成立、什么情况会推翻它；
     推演是基于证据的推理，不是原文结论。
   - ## 局限与未证实：写清没有证据支撑的子问题、证据本身的局限（单一来源/时效/立场），不要写套话。
6. 语气专业：使用客观、书面化的语言，禁止“今天给大家”“我们来看看”等口语表达。
7. 只允许使用证据列表中的内容：不得补写证据之外的事实、数字、结论或 URL；正文里不要写裸 URL，
   来源由程序统一在文末列出。
8. 不要输出“来源清单 / References”，程序会按证据自动追加。
9. """ + KB_SCOPE_RULE + """
10. 篇幅硬约束：整篇报告正文 **≤1000 字**（不含来源清单）；同一个事实全文最多出现两次
    （摘要一次 + 正文一次），严禁车轱辘话；超长时优先删解释和重复，不要删引用。
11. 若用户问题是对比/选型类（A vs B、区别、哪个更好），必须遵守用户消息里的【对比报告专属指令】：
    给出 Markdown 对比表格、禁止张冠李戴、冲突要写清、不写空评价。"""


def _scope_label(item: dict[str, Any]) -> str:
    """证据来源标注：知识库片段要让人（和模型）一眼看出是项目内文档。"""
    url = str(item.get("source_url") or "")
    if url.startswith("kb://") or item.get("source_scope") == "project":
        return "（本地项目文档，仅代表本项目做法）"
    return ""


def evidence_block(evidence: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for idx, item in enumerate(evidence, start=1):
        parts.append(
            f"[S{idx}] claim: {item.get('claim', '')}\n"
            f"    来源: {item.get('source_url', '')}{_scope_label(item)}\n"
            f"    定位: {item.get('source_snippet', '')}\n"
            f"    原文: {item.get('quote', '')}\n"
            f"    相关性: {item.get('relevance_reason', '')}\n"
            f"    置信度: {item.get('confidence', 0)}"
        )
    return "\n\n".join(parts) if parts else "（无可用证据）"


def numbered_evidence_block(
    evidence: Iterable[dict[str, Any]],
    ids: Iterable[int] | None = None,
) -> str:
    """按**全局**证据编号（S1、S2…）渲染证据块。

    分节写作时每节只喂一部分证据，但报告里的角标必须是全局编号，
    否则模型写的 [S1] 会指向错误的来源——这是“引用堆叠错误”的直接成因。
    """
    items = list(evidence)
    if ids is None:
        chosen = list(range(1, len(items) + 1))
    else:
        chosen = [int(value) for value in ids if 1 <= int(value) <= len(items)]
    parts: list[str] = []
    for index in chosen:
        item = items[index - 1]
        parts.append(
            f"[S{index}] claim: {item.get('claim', '')}\n"
            f"    类型: {item.get('kind', '事实')}\n"
            f"    来源: {item.get('source_url', '')}{_scope_label(item)}\n"
            f"    原文: {item.get('quote', '')}\n"
            f"    置信度: {item.get('confidence', 0)}"
        )
    return "\n\n".join(parts) if parts else "（本节没有可用证据）"


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

【来源类型】source_url 以 `kb://` 开头的是**本地项目文档**（只说明本项目怎么设计/取舍，
不能当作外部产品/框架/库的通用能力或行业事实）；`https://` 开头的是本次联网抓取的网页，
外部产品特性/时效性内容优先引用网页。
{comparison_instructions(question)}
请按撰写要求输出 Markdown 研究报告（正文 ≤1000 字，同一事实全文最多出现两次），
只输出「摘要 / 关键发现 / 局限与未证实」三个小节。"""


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


# ================================================================ 大纲规划
OUTLINE_SYSTEM = """你是一个资深的技术研究报告主编。给定研究问题、子问题和已提取的证据卡片，
你要设计报告大纲：决定分几节、每节回答什么、每节用哪几条证据。

规则：
1. 第一必须是「摘要」，最后必须是「分析与推演」和「局限与未证实」，中间按子问题分节（2~4 节）。
2. 每节必须显式列出使用的证据编号（S1、S2…），只能使用给定的编号，不得虚构编号。
3. 分节按逻辑递进排列：是什么 → 怎么做 → 有什么代价/差异 → 推演 → 局限。
   不要按来源排序，不要一个来源一节。
4. 若多个子问题证据不足，可以合并成一节；某个子问题完全没有证据时，不要为它单开一节，
   而是留给「局限与未证实」。
5. goal 用一句话写清“这一节要回答什么、写到什么程度”，不要写成标题的重复。
6. 若问题是对比/选型类（A vs B、区别、哪个更好），必须包含一节「对比矩阵」：
   该节 goal 写明“输出 Markdown 表格（维度 × A × B，至少 3 个维度）”，并排在「关键发现」之后。
7. """ + KB_SCOPE_RULE + """

只输出严格 JSON：
{"sections": [{"title": "节标题", "goal": "这一节的写作目标", "evidence_ids": [1, 3], "sub_question": "对应的子问题或空串"}]}"""


def outline_user(
    question: str,
    sub_questions: Iterable[str],
    evidence: Iterable[dict[str, Any]],
    uncovered: Iterable[str] = (),
) -> str:
    sub_lines = "\n".join(f"- {item}" for item in sub_questions) or "（无）"
    cards = []
    for idx, item in enumerate(evidence, start=1):
        claim = _clip(item.get("claim") or item.get("quote") or "", 160)
        kind = item.get("kind") or "事实"
        origin = "知识库" if str(item.get("source_url") or "").startswith("kb://") else "网页"
        cards.append(f"[S{idx}]（{kind}/{origin}）{claim}")
    missing = "\n".join(f"- {item}" for item in uncovered) or "（无）"
    return f"""【研究问题】：{question}

【子问题】：
{sub_lines}

【没有证据覆盖的子问题】：
{missing}

【证据卡片】：
{chr(10).join(cards) if cards else "（无证据）"}
{comparison_instructions(question)}
请设计大纲并输出 JSON。"""


# ================================================================ 分节写作
DRAFT_SECTION_SYSTEM = """你是一个资深的技术研究员，正在撰写研究报告的【某一个章节】。

写作规则：
1. 只写这一节，不要写标题、不要写其他小节、不要写来源清单。
2. 结论先行：每段第一句就是判断，后面 1~2 句解释机制/依据，不要铺垫背景。
3. 承接上下文：用“在此基础上”“相比之下”“其代价是”“但在……条件下”等过渡句连接上一节，
   禁止与上一节重复同一结论。
4. 引用最小集：每个结论结尾跟 1~2 个角标（如 [S1][S2]），一条结论最多 2 个；
   引用必须真的支撑该句，禁止把来源挂在无关句尾，禁止使用未给出的编号。
5. 只用证据卡片里的内容，不补写证据之外的事实、数字、URL；不要输出裸 URL。
6. 禁止连续两句表达同一意思；不要用“综上所述”“总而言之”凑句。
7. 篇幅：本节 ≤250 字；同一个事实全报告最多出现两次（前面小节写过的不要再写）。
8. 若用户问题是对比类，按用户消息里的【对比报告专属指令】执行；若本节标题是「对比矩阵」，
   必须输出 Markdown 表格（维度 × A × B），单元格结论带 [S#] 角标。
9. """ + KB_SCOPE_RULE


def draft_section_user(
    question: str,
    section: dict[str, Any],
    evidence: Iterable[dict[str, Any]],
    *,
    previous_titles: Iterable[str] = (),
    feedback: str = "",
) -> str:
    cards = numbered_evidence_block(evidence, section.get("evidence_ids") or None)
    prior = "、".join(previous_titles) or "（这是第一节）"
    revision = f"\n【人工修改意见】：{feedback}\n" if feedback else ""
    return f"""【研究问题】：{question}{revision}

【本节标题】：{section.get('title', '')}
【本节写作目标】：{section.get('goal', '')}
【已写过的前面小节】：{prior}

【本节可用证据】（编号即引用角标，只能使用这些编号）：
{cards}
{comparison_instructions(question)}
请只输出本节正文（Markdown，不要标题行；本节 ≤250 字，不重复前面小节已写事实）。"""


DRAFT_SUMMARY_SYSTEM = """你是一个资深的技术研究员，正在写研究报告的「摘要」。

规则：
1. 恰好 3 句话，按「结论 → 关键依据 → 适用边界」排列，每句都要短（不超过 45 字）。
2. 第一句必须是对研究问题的具体判断，点名关键机制或差异点
   （例如“LangGraph 用 interrupt 在节点内部暂停，AutoGen 靠检查点回放状态”）。
   禁止用“存在差异”“很重要”“值得关注”“各有优劣”这类空判断开头，也不要复述问题、不要写“本文”。
3. 第二句给支撑判断的关键依据，必须带 1~2 个引用角标（如 [S1][S3]）。
4. 第三句说明这个判断在什么前提下成立、或还缺什么（例如“该结论以官方文档为准，第三方实践未覆盖”）。
5. 只能使用证据内容，不补写事实；不要输出标题、不要输出“摘要”二字、不要输出其他小节。
6. 同一个事实全文最多出现两次（摘要一次、正文一次）；摘要里点到为止，不要提前复述正文细节。
7. """ + KB_SCOPE_RULE


def draft_summary_user(
    question: str,
    evidence: Iterable[dict[str, Any]],
    *,
    ids: Iterable[int] | None = None,
    feedback: str = "",
) -> str:
    revision = f"\n【人工修改意见】：{feedback}\n" if feedback else ""
    return f"""【研究问题】：{question}{revision}

【证据】：
{numbered_evidence_block(evidence, ids)}

请只输出 3 句话的摘要正文。"""


DRAFT_ANALYSIS_SYSTEM = """你是一个资深的技术研究员，正在写研究报告的「分析与推演」小节。

规则：
1. 输出 2~4 条推演，每条以【推断】开头，一条一行（Markdown 列表）。
2. 每条必须写清三件事：依据哪几条证据（带角标）、在什么前提下成立、什么情况会推翻它。
3. 允许做因果、对比、条件、代价推演，但必须标明这是推断而非原文结论，不得把推测写成事实。
4. 只能用给定证据，不引入外部知识、不补写数字。
5. 不要写“综上所述”“未来可期”之类空话；没有可推演的内容时就只输出一条，说明证据不足以推演。
6. 若是 A/B 对比问题：推演要直接比较两者的差异与适用条件，不要泛泛而谈；表格里已写过的参数不再复述。
7. """ + KB_SCOPE_RULE


def draft_analysis_user(
    question: str,
    evidence: Iterable[dict[str, Any]],
    *,
    ids: Iterable[int] | None = None,
    uncovered: Iterable[str] = (),
    feedback: str = "",
) -> str:
    missing = "；".join(str(item) for item in uncovered) or "（无）"
    revision = f"\n【人工修改意见】：{feedback}\n" if feedback else ""
    return f"""【研究问题】：{question}{revision}

【没有证据覆盖的子问题】：{missing}

【全部证据】：
{numbered_evidence_block(evidence, ids)}

请只输出「分析与推演」小节的正文（不要标题行）。"""


# ================================================================ 草稿重写（引用硬校验不过时）
REWRITE_SYSTEM = """你是一个资深的技术报告编辑。给定一份研究报告和一批“引用/结构问题”，
你要做**最小改动**的修订，不要重写全文。

修订规则：
1. 保留原来的小节标题与顺序，保留绝大多数原句；禁止新增、删除或重排小节。
2. 禁止整体删掉引用角标：宁可修正一个角标，也不要让一条结论失去引用。
   只有确认错配时才换编号（必须换成证据列表里存在的编号），一句话最多保留 2 个角标。
3. 列表仍然是列表：不要把「关键发现」的条目改写成大段段落。
4. 摘要问题：按「结论 → 关键依据 → 适用边界」改成 3 句，第一句直接点名机制或差异点，
   并至少带 1 个角标。
5. 结构问题：缺「分析与推演」就补 2~3 条以【推断】开头、带角标的推演；缺「局限与未证实」就补上。
6. 不得新增证据之外的事实、数字或 URL；不要输出来源清单。
7. 直接输出修订后的完整 Markdown 报告，不要解释改了什么。
8. 若问题是 A/B 对比而报告缺少 Markdown 表格：必须补一个「维度 | A | B」、至少 3 行的对比表格，
   单元格结论带 [S#] 角标；同时保留原有小节结构。
9. 若正文超过 1000 字、或同一个事实出现超过 2 次：压缩解释、删掉重复句（摘要一次 + 正文一次即可），
   不得靠删引用或删整节来降字数。
10. """ + KB_SCOPE_RULE


def rewrite_user(question: str, draft: str, issues: Iterable[dict[str, Any]]) -> str:
    lines = []
    for issue in issues:
        code = issue.get("code") or "issue"
        detail = issue.get("detail") or ""
        lines.append(f"- [{code}] {detail}")
    return f"""【研究问题】：{question}

【待修订报告】：
{draft}

【必须修复的问题】：
{chr(10).join(lines) if lines else "- （无）"}

请输出修订后的完整报告。"""

