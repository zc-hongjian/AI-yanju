"""LLM 客户端：OpenAI / 兼容 OpenAI 的 Function Calling，附带离线启发式回退。

- openai 模式：使用 chat.completions + tools 真正走 Function Calling；
- offline 模式：没有 Key 或显式关闭时使用确定性启发式（抽取式摘要 + 模板报告），
  保证没有 Key 也能跑通完整流程，而且不会编造来源。

get_llm() 返回进程级单例，测试里可用 set_llm()/reset_llm() 替换。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

import prompts
import reporting


class LLMError(RuntimeError):
    """LLM 调用或输出解析失败。节点捕获后进入降级路径。"""


STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are",
    "was", "were", "be", "been", "with", "about", "how", "what", "why", "which",
    "does", "do", "did", "can", "could", "should", "would", "this", "that",
    "研究", "如何", "什么", "怎么", "为什么", "是否", "以及", "进行", "一个",
    # 中文通用问句词：不携带主题信息，用来召回会引入「统计公报」「百科词条」等噪声
    "企业", "主要", "挑战", "问题", "哪些", "实现", "步骤", "方法", "场景",
    "差异", "区别", "影响", "进展", "趋势", "最新", "情况", "优点", "缺点",
    "作用", "角色", "原理", "机制", "方式", "对比", "分析", "应用", "标准",
    "评估", "方案", "产品", "能力", "需要", "解决", "做到", "达到",
}


def _clip(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def keywords(text: str) -> list[str]:
    """轻量关键词抽取：英文词 + 中文片段及其 2~3 字子串，用于离线相关性打分。

    中文句子没有空格，整段当关键词会匹配不到（例如「人工中断需要什么」和
    「人工中断需要 checkpointer…」），所以额外拆出短子串提高召回。
    """
    latin = re.findall(r"[A-Za-z][A-Za-z0-9_+\-.]{1,}", (text or "").lower())
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", text or "")
    words: list[str] = []
    for word in latin:
        if word not in STOPWORDS and len(word) > 1 and word not in words:
            words.append(word)
    for chunk in cjk:
        chunk = chunk.strip()
        if not chunk:
            continue
        candidates = [chunk[:12]]
        for size in (2, 3):
            for start in range(max(0, len(chunk) - size + 1)):
                candidates.append(chunk[start : start + size])
        for word in candidates:
            if len(word) < 2 or word in STOPWORDS or word in words:
                continue
            words.append(word)
    return words


_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")

# 常见技术框架 → 官方文档/代码站点的 site: 限定词（技术搜索规则 2）
FRAMEWORK_SITES: dict[str, str] = {
    "langgraph": "langchain.com",
    "langchain": "langchain.com",
    "openai": "openai.com",
    "llamaindex": "docs.llamaindex.ai",
    "crewai": "github.com",
    "autogen": "github.com",
    "dify": "github.com",
    "fastapi": "fastapi.tiangolo.com",
    "django": "docs.djangoproject.com",
    "react": "react.dev",
    "vue": "vuejs.org",
}

# 技术搜索规则 3：去掉口语化表达
FILLER_PHRASES = (
    "我想知道",
    "我想了解",
    "我想问",
    "请问",
    "请帮我",
    "帮我",
    "麻烦",
    "能不能",
    "可以吗",
    "怎么弄",
    "怎么搞",
)

# 技术搜索规则 1：「如何实现 / 原理 / 教程」类问题
HOW_PATTERN = re.compile(
    r"(如何|怎么|怎样|原理|实现|教程|步骤|入门|how\b|how to|tutorial|implement|implementation|guide)",
    re.I,
)


def clean_question(text: str) -> str:
    """去掉口语化表达与多余空白，保留技术名词。"""
    cleaned = " ".join(str(text or "").split())
    for phrase in FILLER_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    cleaned = " ".join(cleaned.split()).strip(" ，。；;、")
    return cleaned


def is_how_question(text: str) -> bool:
    return bool(HOW_PATTERN.search(text or ""))


def framework_site(text: str) -> str | None:
    lowered = (text or "").lower()
    for framework, site in FRAMEWORK_SITES.items():
        if framework in lowered:
            return site
    return None


def split_sentences(text: str, min_len: int = 25) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s and len(s.strip()) >= min_len]


_TOPIC_TAIL_RE = re.compile(
    r"(?:是什么|有什么作用|有什么优缺点|各有什么优缺点|有哪些|有什么区别|的主要挑战是什么|"
    r"的典型场景有哪些|如何实现|怎么实现|为什么|分别适合什么场景|吗|呢)\s*[。？?]?$"
)


def _topic_phrase(question: str) -> str:
    """把问句压成主题短语，供子问题模板拼接（避免“XXX 是什么 的核心结论”这种病句）。"""
    base = clean_question(question)
    base = re.split(r"[？?]", base)[0].strip()
    base = _TOPIC_TAIL_RE.sub("", base).strip(" ，。；;、")
    # 去掉句中的“如何/怎么”，但保留被问的技术对象
    base = re.sub(r"(?:如何|怎么)(?:实现|使用|配置|评估|做|才能|来)?\s*", " ", base)
    base = " ".join(base.split()).strip(" ，。；;、")
    return base or clean_question(question) or "研究问题"


def heuristic_sub_questions(question: str) -> list[str]:
    """离线模式的问题拆解：生成 4 个可独立检索的子问题（3~5 个区间内）。"""
    base = _topic_phrase(question)
    if is_how_question(base):
        candidates = [
            f"{base}的实现原理是什么",
            f"{base}的具体步骤和代码示例",
            f"{base}需要哪些前置条件与依赖",
            f"{base}有哪些常见问题与最佳实践",
            f"{base}官方文档如何说明",
        ]
    else:
        candidates = [
            f"{base}的核心结论与主要事实",
            f"{base}的关键数据与最新进展",
            f"{base}的主要挑战与原因",
            f"{base}有哪些权威来源与案例",
            f"{base}在不同对象或场景下的差异",
        ]
    return candidates[:4]


_SUB_QUESTIONS_RE = re.compile(r"\{[^{}]*\"sub_questions\"[^{}]*\}", re.S)


def extract_sub_questions(text: str) -> list[str]:
    """从模型输出里解析 {"sub_questions": [...]}，失败返回空列表。"""
    match = _SUB_QUESTIONS_RE.search(text or "")
    payload = match.group(0) if match else (text or "")
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("sub_questions")
    if not isinstance(raw, list):
        return []
    items: list[str] = []
    for item in raw:
        value = str(item or "").strip()
        if value and value not in items:
            items.append(value)
    return items[:5]


def _sub_questions_enabled() -> bool:
    """评测消融开关：AGENT_PLAN_SUB_QUESTIONS=0 时模拟“不做问题拆解”的旧行为。"""
    return os.getenv("AGENT_PLAN_SUB_QUESTIONS", "1").strip().lower() not in {"0", "false", "no", "off"}



def _normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    return url.rstrip("/").lower()


# ------------------------------------------------------------------ 错误分类
# 只做「按类型判断」，不直接 import openai：离线模式/测试环境可能没有安装 SDK。
def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "timeout" in name or "timed out" in text or "timeout" in text


def _is_format_unsupported(exc: BaseException) -> bool:
    """400/422 通常意味着网关不认 response_format 之类的参数，去掉再试一次值得。"""
    return _status_code(exc) in {400, 422}


def _is_transient_error(exc: BaseException) -> bool:
    """限流 / 网关 5xx / 连接类抖动：短退避重试一次，不放大超时预算。"""
    if _status_code(exc) in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    name = type(exc).__name__.lower()
    return any(token in name for token in ("connection", "remoteprotocol", "readerror", "connecterror"))


def _env_positive_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _draft_concurrency() -> int:
    """分节写作并发度：默认 3；设 1 可退回逐节串行（便于对比/排障）。"""
    try:
        value = int(os.getenv("AGENT_DRAFT_CONCURRENCY", "3"))
    except ValueError:
        value = 3
    return max(1, min(value, 4))


class LLMClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "")).strip() or None
        self.model = (model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip() or "gpt-4o-mini"
        self.timeout = float(timeout if timeout is not None else os.getenv("AGENT_LLM_TIMEOUT", "30"))
        requested = (mode or os.getenv("AGENT_LLM_MODE", "auto")).strip().lower()
        if requested not in {"auto", "openai", "offline"}:
            requested = "auto"
        if requested == "auto":
            requested = "openai" if self.api_key else "offline"
        if requested == "openai" and not self.api_key:
            requested = "offline"
        self.mode = requested
        self._client: Any = None
        # 长输出调用（证据提取 / 大纲 / 报告分节）要用更宽的预算：
        # 实测 glm-4-flash 输出 750 token 就要 27~33s，30s 的单次超时必然频繁触发；
        # 超时后再重试只会把一次 30s 放大成 4×30s（见 _chat 的注释），所以单独分档。
        self.long_timeout = float(os.getenv("AGENT_LLM_TIMEOUT_LONG", "75"))
        self._usage_lock = threading.Lock()
        # token 用量累计，供日志/成本核算使用；离线模式恒为 0
        self.usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        }

    def _record_usage(self, resp: Any) -> None:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", prompt + completion) or (prompt + completion))
        # 分节写作会并发调用，累计器必须加锁，否则 token 统计会丢
        with self._usage_lock:
            self.usage["prompt_tokens"] += prompt
            self.usage["completion_tokens"] += completion
            self.usage["total_tokens"] += total
            self.usage["calls"] += 1

    def usage_snapshot(self) -> dict[str, int]:
        return dict(self.usage)

    def ping(self) -> dict[str, Any]:
        """做一次最小真实调用，验证 Key / Base URL / 模型是否可用。

        只用于前端「测试连接」；成功返回延迟与模型名，不返回任何密钥。
        """
        if self.mode != "openai":
            raise LLMError("当前为离线模式，无法测试连接")
        started = time.perf_counter()
        reply = self._chat(
            "你是连通性检查器。只输出 OK 两个字母，不要输出其他内容。",
            "ping",
            temperature=0.0,
            max_tokens=8,
        )
        return {
            "ok": True,
            "model": self.model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "reply": reply[:40],
        }

    # ------------------------------------------------------------------ SDK
    def _sdk(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
                raise LLMError("缺少 openai SDK，请先 pip install openai") from exc
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                # 不交给 SDK 隐式重试：超时被内部重试一次，再叠加 _chat 的格式回退，
                # 一次 30s 超时就会变成 4×30s=120s（实测 121s），而结果没有任何改善。
                max_retries=0,
            )
        return self._client

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]] | None:
        """OpenAI 兼容 /embeddings；失败返回 None（调用方降级到 BM25）。"""
        if self.mode != "openai" or not texts:
            return None
        model = (model or os.getenv("AGENT_EMBEDDING_MODEL", "text-embedding-3-small")).strip()
        try:
            resp = self._sdk().embeddings.create(model=model, input=texts)
        except Exception as exc:  # noqa: BLE001 - 向量服务不可用不应中断检索
            print(f"[研究助手] embedding 调用失败，降级 BM25：{exc}", flush=True)
            return None
        vectors = [list(item.embedding) for item in getattr(resp, "data", []) or []]
        if len(vectors) != len(texts):
            return None
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.usage["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.usage["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)
            self.usage["calls"] += 1
        return vectors

    def _chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 2200,
        response_format: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> str:
        if self.mode != "openai":
            raise LLMError("当前为 offline 模式，自由文本调用不可用")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        budget = float(timeout if timeout is not None else self.timeout)

        def _create(extra: dict[str, Any]) -> Any:
            client = self._sdk()
            if budget != self.timeout:
                client = client.with_options(timeout=budget)
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **extra,
            )

        # 重试策略（性能修复：一次 30s 超时曾被放大成 4×30s=120s）：
        # 1) 超时只发一次请求，绝不再叠加「去掉 response_format 重试」——超时与格式无关；
        # 2) 只有 400/422（网关不支持 response_format 的常见形态）才回退成纯文本再试一次；
        # 3) 429/5xx/连接类错误快速重试一次（短退避），保住偶发抖动时的可用性。
        try:
            resp = _create({"response_format": response_format} if response_format else {})
        except Exception as exc:  # noqa: BLE001 - 统一转换成 LLMError 由上层降级
            if _is_timeout_error(exc):
                raise LLMError(f"LLM 请求超时（{budget:.0f}s）：{exc}") from exc
            if response_format and _is_format_unsupported(exc):
                try:
                    resp = _create({})
                except Exception as retry_exc:  # noqa: BLE001
                    if _is_timeout_error(retry_exc):
                        raise LLMError(f"LLM 请求超时（{budget:.0f}s）：{retry_exc}") from retry_exc
                    raise LLMError(f"LLM 请求失败：{retry_exc}") from retry_exc
            elif _is_transient_error(exc):
                time.sleep(0.8)
                try:
                    resp = _create({"response_format": response_format} if response_format else {})
                except Exception as retry_exc:  # noqa: BLE001
                    raise LLMError(f"LLM 请求失败：{retry_exc}") from retry_exc
            else:
                raise LLMError(f"LLM 请求失败：{exc}") from exc
        self._record_usage(resp)
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise LLMError("LLM 返回空内容")
        return content

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # JSON 模式下要求网关返回合法 JSON（智谱/OpenAI 都支持），解析失败再退回纯文本解析
        try:
            return parse_json_object(
                self._chat(
                    system,
                    user,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    timeout=timeout,
                )
            )
        except LLMError as exc:
            if "JSON" not in str(exc):
                raise
            return parse_json_object(self._chat(system, user, temperature=temperature, timeout=timeout))

    # ---------------------------------------------- 检索计划（技术搜索专家）
    def plan_research(
        self,
        question: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        context: str = "",
    ) -> dict[str, Any]:
        """技术搜索专家规划：先拆解 3~5 个子问题，再产出 2~3 个搜索词。

        提示词见 prompts.PLAN_SYSTEM / PLAN_JSON_SYSTEM：
        How 问题加技术词、框架加 site: 限定、去口语化、输出 2~3 个搜索词。

        ``context`` 是上一轮研究结论摘要（多轮追问时由 web 层注入）；
        OpenAI 模式下拼进规划提示词，离线启发式模式忽略它（保证离线评测可复现）。
        """
        if self.mode != "openai":
            return self._heuristic_plan(question)
        errors: list[str] = []
        queries: list[dict[str, Any]] = []
        rationale = ""
        used_fc = False
        try:
            resp = self._sdk().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompts.PLAN_SYSTEM},
                    {"role": "user", "content": prompts.plan_user(question, context)},
                ],
                tools=tools or None,
                tool_choice="auto" if tools else "none",
                temperature=0.1,
                max_tokens=800,
            )
            self._record_usage(resp)
            message = resp.choices[0].message
            rationale = (message.content or "").strip()
            used_fc = True
            queries = self._queries_from_tool_calls(message)
        except Exception as exc:  # noqa: BLE001 - 服务不支持 tools 时走 JSON 回退
            errors.append(f"Function Calling 规划失败：{exc}")
        if not queries:
            try:
                queries = self._json_plan(question, context)
                if not queries:
                    errors.append("JSON 规划没有返回搜索词")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"JSON 规划失败：{exc}")
        if not queries:
            raise LLMError("规划失败：" + "；".join(errors or ["模型没有返回可用搜索词"]))
        sub_questions = extract_sub_questions(rationale)
        if not 3 <= len(sub_questions) <= 5:
            sub_questions = heuristic_sub_questions(question)
        return {
            "queries": queries[:4],
            "sub_questions": sub_questions if _sub_questions_enabled() else [],
            "focus": question,
            "rationale": rationale or "JSON 规划（技术搜索规则）",
            "provider": "openai-function-calling" if used_fc else "openai-json",
        }

    @staticmethod
    def _queries_from_tool_calls(message: Any) -> list[dict[str, Any]]:
        queries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for call in getattr(message, "tool_calls", None) or []:
            if getattr(call.function, "name", "") != "search_web":
                continue
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                continue
            query = str(args.get("query") or "").strip()
            if not query or query.lower() in seen:
                continue
            seen.add(query.lower())
            queries.append({"query": query, "max_results": _clamp_int(args.get("max_results"), 5, 1, 10)})
        return queries

    def _json_plan(self, question: str, context: str = "") -> list[dict[str, Any]]:
        """纯 JSON 输出模式的备用路径（兼容不支持 tools 的 OpenAI 网关）。"""
        text = self._chat(
            prompts.PLAN_JSON_SYSTEM,
            prompts.plan_json_user(question, context),
            temperature=0.1,
            max_tokens=400,
        )
        terms = parse_json_list(text)
        queries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for term in terms[:4]:
            key = term.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            queries.append({"query": term, "max_results": 5})
        return queries

    def _heuristic_plan(self, question: str) -> dict[str, Any]:
        cleaned = clean_question(question)
        base = cleaned.rstrip("？?。.").strip() or "研究问题"
        queries: list[dict[str, Any]] = [{"query": base, "max_results": 8}]
        tech_terms = "教程 实现 代码示例" if _has_cjk(base) else "tutorial implementation code example"
        if is_how_question(cleaned):
            queries.append({"query": f"{base} {tech_terms}", "max_results": 8})
        site = framework_site(cleaned)
        if site:
            queries.append({"query": f"{base} site:{site}", "max_results": 8})
        if len(queries) < 2:
            extra = f"{base} 研究 综述" if _has_cjk(base) else f"{base} review"
            queries.append({"query": extra, "max_results": 8})
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in queries:
            key = re.sub(r"\s+", " ", item["query"].strip().lower())
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return {
            "queries": deduped[:3],
            "sub_questions": heuristic_sub_questions(question) if _sub_questions_enabled() else [],
            "focus": question,
            "rationale": "离线启发式规划：技术搜索规则（How 加技术词 / 框架加 site 限定 / 去口语）",
            "provider": "offline-heuristic",
        }

    # ------------------------------------------------------------ 证据总结
    def summarize_evidence(
        self,
        question: str,
        sources: Iterable[dict[str, Any]],
        *,
        llm_failed: bool = False,
    ) -> dict[str, Any]:
        usable = [s for s in sources if str(s.get("content") or "").strip()]
        if not usable:
            return {"evidence": [], "note": "未找到可靠来源（没有可用的网页正文）", "mode": "none"}
        if self.mode != "openai" or llm_failed:
            return {
                "evidence": heuristic_evidence(question, usable),
                "note": "离线启发式证据提取：逐句摘录原文，未做改写",
                "mode": "offline",
            }
        try:
            data = self.chat_json(
                prompts.SUMMARIZE_SYSTEM,
                prompts.summarize_user(question, usable),
                # 证据提取是长输出（实测 ~750 token / 27~33s），用长超时分档；
                # AGENT_SUMMARIZE_TIMEOUT 与工具层预算是同一个值，避免“预算 25s、实际 121s”的假配置。
                timeout=_env_positive_float("AGENT_SUMMARIZE_TIMEOUT", self.long_timeout),
            )
        except LLMError as exc:
            return {
                "evidence": heuristic_evidence(question, usable),
                "note": f"LLM 证据提取失败，已降级为逐句摘录：{exc}",
                "mode": "offline_fallback",
            }
        evidence, dropped = filter_evidence(usable, data.get("evidence"))
        note = str(data.get("note") or "").strip()
        if dropped:
            note = (note + f"；已丢弃 {dropped} 条无法对应到来源原文的引用").lstrip("；")
        if not evidence:
            return {
                "evidence": heuristic_evidence(question, usable),
                "note": note or "LLM 未给出可验证的证据，已降级为逐句摘录",
                "mode": "heuristic_fallback",
            }
        return {"evidence": evidence, "note": note, "mode": "openai"}

    # ------------------------------------------------------------ 大纲规划
    def plan_outline(
        self,
        question: str,
        sub_questions: list[str],
        evidence: list[dict[str, Any]],
        *,
        uncovered: list[str] | None = None,
        max_sections: int = 4,
    ) -> dict[str, Any] | None:
        """让模型设计报告大纲；offline 模式或失败返回 None（由规则大纲兜底）。"""
        evidence = list(evidence)
        if self.mode != "openai" or not evidence:
            return None
        last_error = ""
        # 模型偶尔输出不合法 JSON（实测一次），重试一次比直接降级回规则大纲更划算；
        # 但超时重试只会让整条链路更慢，这里不重试。
        for attempt in range(2):
            try:
                data = self.chat_json(
                    prompts.OUTLINE_SYSTEM,
                    prompts.outline_user(question, sub_questions, evidence, uncovered or ()),
                    timeout=self.long_timeout,
                )
            except LLMError as exc:
                last_error = str(exc)
                if "JSON" not in last_error:
                    break
                continue
            outline = reporting.normalize_llm_outline(
                data, evidence, uncovered=uncovered or (), max_sections=max_sections + 2
            )
            if outline:
                return outline
            last_error = "大纲 JSON 结构不符合预期"
        print(f"[研究助手] 大纲生成失败，改用规则大纲：{last_error}", flush=True)
        return None

    # ------------------------------------------------------------ 报告草稿
    def draft_report(
        self,
        question: str,
        evidence: Iterable[dict[str, Any]],
        sources: Iterable[dict[str, Any]],
        *,
        feedback: str = "",
        outline: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """返回 (markdown, mode)。LLM 失败时降级为模板，不抛异常。

        ``outline`` 存在时按节生成（摘要单独写、每节只喂本节证据），
        这是“逻辑割裂 / 摘要水 / 引用堆叠”的结构性修法；没有大纲时退回单次生成。
        """
        evidence = list(evidence)
        sources = list(sources)
        sections = [item for item in ((outline or {}).get("sections") or []) if isinstance(item, dict)]
        uncovered = list((outline or {}).get("uncovered") or [])
        if self.mode != "openai":
            return (
                heuristic_draft(question, evidence, sources, feedback, uncovered=uncovered),
                "offline",
            )
        if not sections:
            try:
                text = self._chat(
                    prompts.DRAFT_SYSTEM,
                    prompts.draft_user(question, evidence, sources, feedback),
                    temperature=0.3,
                    max_tokens=2600,
                    timeout=self.long_timeout,
                )
            except LLMError:
                return heuristic_draft(question, evidence, sources, feedback, uncovered=uncovered), "offline_fallback"
            return text, "openai"
        try:
            text = self._draft_by_sections(
                question, evidence, sections, uncovered=uncovered, feedback=feedback
            )
        except LLMError as exc:
            print(f"[研究助手] 分节写作失败，降级模板草稿：{exc}", flush=True)
            return heuristic_draft(question, evidence, sources, feedback, uncovered=uncovered), "offline_fallback"
        return text, "openai"

    def _draft_by_sections(
        self,
        question: str,
        evidence: list[dict[str, Any]],
        sections: list[dict[str, Any]],
        *,
        uncovered: list[str],
        feedback: str,
    ) -> str:
        """按大纲逐节生成正文。

        各节之间没有正文依赖（只依赖“前文出现过哪些小节标题”），所以把 LLM 调用
        并发执行、再按大纲顺序拼装：实测 6 次串行调用 80s，并发后约 20~30s。
        ``AGENT_DRAFT_CONCURRENCY=1`` 可退回逐节串行（对比/排障用，输出完全一致）。
        """
        all_ids = list(range(1, len(evidence) + 1))
        parts: list[str | None] = [None] * len(sections)
        titles: list[str] = []
        summary_index: int | None = None
        jobs: list[tuple[int, str, dict[str, Any]]] = []
        for index, section in enumerate(sections):
            title = str(section.get("title") or "").strip()
            key = str(section.get("key") or "")
            if not title:
                continue
            ids = [int(value) for value in (section.get("evidence_ids") or all_ids)]
            ids = [value for value in ids if 1 <= value <= len(evidence)] or all_ids
            previous_titles = list(titles)
            if title == reporting.LIMITS_TITLE or key == "limits":
                # 「局限与未证实」由程序按证据生成，不调模型。
                parts[index] = "\n".join(reporting.limits_lines(evidence, uncovered))
            elif title == reporting.FINDINGS_TITLE or key == "findings":
                # 「关键发现」由程序按证据生成：条条有角标、不重复、不会被模型写成大段散文
                parts[index] = (
                    f"## {reporting.FINDINGS_TITLE}\n\n"
                    + "\n".join(reporting.find_bullets(evidence, ids))
                )
            elif title == reporting.SUMMARY_TITLE or key == "summary":
                summary_index = index
                jobs.append((index, "summary", {"ids": ids}))
            elif title == reporting.ANALYSIS_TITLE or key == "analysis":
                jobs.append((index, "analysis", {"ids": ids}))
            else:
                jobs.append((index, "section", {"section": section, "previous": previous_titles}))
            titles.append(title)

        workers = _draft_concurrency()
        if workers <= 1 or len(jobs) <= 1:
            for job in jobs:
                index, text = self._run_section_job(question, evidence, uncovered, feedback, job)
                parts[index] = text
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
                futures = [
                    pool.submit(self._run_section_job, question, evidence, uncovered, feedback, job)
                    for job in jobs
                ]
                for future in futures:
                    index, text = future.result()
                    parts[index] = text

        if summary_index is None:
            parts.insert(
                0,
                f"## {reporting.SUMMARY_TITLE}\n\n{_fallback_summary(question, evidence)}",
            )
        return "\n\n".join(text for text in parts if text)

    def _run_section_job(
        self,
        question: str,
        evidence: list[dict[str, Any]],
        uncovered: list[str],
        feedback: str,
        job: tuple[int, str, dict[str, Any]],
    ) -> tuple[int, str]:
        """执行一个写作任务；返回值可在线程池中乱序完成，由调用方按大纲重排。"""
        index, kind, payload = job
        if kind == "summary":
            body = self._chat(
                prompts.DRAFT_SUMMARY_SYSTEM,
                prompts.draft_summary_user(
                    question, evidence, ids=payload["ids"], feedback=feedback
                ),
                temperature=0.2,
                max_tokens=500,
                timeout=self.long_timeout,
            )
            return index, f"## {reporting.SUMMARY_TITLE}\n\n{_strip_heading(body)}"
        if kind == "analysis":
            body = self._chat(
                prompts.DRAFT_ANALYSIS_SYSTEM,
                prompts.draft_analysis_user(
                    question, evidence, ids=payload["ids"], uncovered=uncovered, feedback=feedback
                ),
                temperature=0.4,
                max_tokens=900,
                timeout=self.long_timeout,
            )
            return index, f"## {reporting.ANALYSIS_TITLE}\n\n{_strip_heading(body)}"
        section = payload["section"]
        title = str(section.get("title") or "").strip()
        body = self._chat(
            prompts.DRAFT_SECTION_SYSTEM,
            prompts.draft_section_user(
                question, section, evidence, previous_titles=payload["previous"], feedback=feedback
            ),
            temperature=0.3,
            max_tokens=900,
            timeout=self.long_timeout,
        )
        return index, f"## {title}\n\n{_strip_heading(body)}"

    def rewrite_draft(
        self,
        question: str,
        draft: str,
        issues: list[dict[str, Any]],
    ) -> str | None:
        """按问题清单做一次最小改动重写；offline / 失败返回 None。"""
        if self.mode != "openai" or not issues:
            return None
        try:
            return self._chat(
                prompts.REWRITE_SYSTEM,
                prompts.rewrite_user(question, draft, issues),
                temperature=0.2,
                max_tokens=3000,
                timeout=self.long_timeout,
            )
        except LLMError as exc:
            print(f"[研究助手] 草稿重写失败，改为程序修复：{exc}", flush=True)
            return None


# --------------------------------------------------------------------- 工具函数
def parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象，兼容 ```json 代码块。"""
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("模型输出不是合法 JSON")
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型输出 JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("模型输出的 JSON 顶层不是对象")
    return data


def parse_json_list(text: str) -> list[str]:
    """从模型输出里解析 JSON 字符串数组（兼容 ```json 代码块）。"""
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            raise LLMError("模型输出不是合法 JSON 数组") from None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型输出 JSON 解析失败：{exc}") from exc
    if not isinstance(data, list):
        raise LLMError("模型输出的 JSON 顶层不是数组")
    terms: list[str] = []
    for item in data:
        term = str(item or "").strip()
        if term and term not in terms:
            terms.append(term)
    return terms


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, num))


_SENTENCE_END_BAD = re.compile(
    r"(?:[,，、;；:]|\b(?:and|the|of|to|with|for|in|on|at|by|is|are|was|were|a|an)\b)\s*$",
    re.I,
)


def _is_complete_sentence(text: str) -> bool:
    """规则 2：拒绝半截句子（以逗号、连接词等结尾）。"""
    text = (text or "").strip()
    if len(text) < 12:
        return False
    return not _SENTENCE_END_BAD.search(text)


def filter_evidence(
    sources: Iterable[dict[str, Any]],
    raw_items: Any,
) -> tuple[list[dict[str, Any]], int]:
    """只保留能对应到抓取正文的完整句证据。

    - URL 必须在来源里、quote 必须逐字命中（防编造）；
    - 拒绝半截句子（规则 2）；
    - 保留 relevance_reason / source_snippet（规则 5，带出处）。
    """
    by_url = {_normalize_url(str(s.get("url", ""))): s for s in sources}
    items: list[dict[str, Any]] = []
    dropped = 0
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        url = _normalize_url(str(raw.get("source_url") or raw.get("url") or ""))
        source = by_url.get(url)
        quote = _normalize_ws(str(raw.get("quote") or raw.get("evidence_text") or ""))
        claim = _normalize_ws(str(raw.get("claim") or raw.get("evidence_text") or ""))
        if not source or not quote or not claim:
            dropped += 1
            continue
        if not _is_complete_sentence(quote):
            dropped += 1
            continue
        content = _normalize_ws(str(source.get("content") or ""))
        if quote not in content:
            dropped += 1
            continue
        try:
            confidence = float(raw.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        source_snippet = _normalize_ws(str(raw.get("source_snippet") or ""))[:120]
        relevance_reason = _normalize_ws(str(raw.get("relevance_reason") or ""))[:200]
        items.append(
            {
                "claim": claim[:300],
                "evidence_text": claim[:300],
                "source_url": str(source.get("url", "")),
                "quote": quote[:400],
                "source_snippet": source_snippet or _normalize_ws(str(source.get("title") or "正文摘录"))[:120],
                "relevance_reason": relevance_reason,
                "confidence": round(max(0.0, min(1.0, confidence)), 2),
            }
        )
    return items, dropped


_NOISE_SEPARATOR_RE = re.compile(r"\s/\s")
_NOISE_SYMBOL_RE = re.compile(r"[0-9%+\-—.,:：/|]")

# 导航/目录/表格类片段：即使逐字命中关键词，也不是可用的证据（评测中发现的主要噪声源）
_NOISE_WORDS = ("登录", "注册", "首页 产品", "联系我们", "版权所有", "广告", "导航")
_MENU_MARKERS = ("教程", "文档", "下载", "登录", "注册", "首页", "产品", "价格")


def _looks_like_noise(text: str) -> bool:
    """过滤表格数据、导航菜单、目录式片段。"""
    stripped = str(text or "").strip()
    if len(stripped) > 350:
        return True
    if len(_NOISE_SEPARATOR_RE.findall(stripped)) >= 2 or stripped.count("|") >= 2:
        return True
    compact = "".join(stripped.split())
    if not compact:
        return True
    if len(_NOISE_SYMBOL_RE.findall(compact)) / len(compact) > 0.30:
        return True
    if _has_cjk(stripped) and stripped.count(" ") / max(1, len(stripped)) > 0.22:
        return True
    if sum(stripped.count(marker) for marker in _MENU_MARKERS) >= 3:
        return True
    lowered = stripped.lower()
    return any(word in lowered for word in _NOISE_WORDS)


def _noise_filter_enabled() -> bool:
    """评测消融开关：AGENT_EVIDENCE_NOISE_FILTER=0 时关闭导航/表格噪声过滤。"""
    return os.getenv("AGENT_EVIDENCE_NOISE_FILTER", "1").strip().lower() not in {"0", "false", "no", "off"}


def _evidence_total_cap() -> int:
    """离线抽取式证据的总条数上限；补检轮次需要更多证据来覆盖子问题。"""
    try:
        return max(4, min(30, int(os.getenv("AGENT_EVIDENCE_MAX", "16"))))
    except ValueError:
        return 16


def _evidence_per_source_cap() -> int:
    try:
        return max(1, min(6, int(os.getenv("AGENT_EVIDENCE_PER_SOURCE", "3"))))
    except ValueError:
        return 3


def heuristic_evidence(question: str, sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """抽取式证据：句子原文照抄，置信度按关键词命中估算。

    规则 3（宁缺毋滥）：与问题毫无关键词关联的句子直接丢弃；一条都没有就返回 []。
    每个来源取前 N 句、总数有上限，避免补检后报告被低相关句子灌水。
    """
    kws = keywords(question)
    if not kws:
        return []
    total_cap = _evidence_total_cap()
    per_source_cap = _evidence_per_source_cap()
    items: list[dict[str, Any]] = []
    for source in sources:
        sentences = split_sentences(str(source.get("content") or ""))
        scored: list[tuple[int, str]] = []
        for sentence in sentences:
            if _noise_filter_enabled() and _looks_like_noise(sentence):
                continue
            lowered = sentence.lower()
            score = sum(1 for kw in kws if kw in lowered)
            scored.append((score, sentence))
        scored.sort(key=lambda pair: (pair[0], len(pair[1])), reverse=True)
        seen: set[str] = set()
        picked = 0
        for score, sentence in scored:
            if score <= 0:
                continue  # 宁缺毋滥
            key = sentence[:60]
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "claim": sentence[:300],
                    "evidence_text": sentence[:300],
                    "source_url": str(source.get("url", "")),
                    "quote": sentence[:400],
                    "source_snippet": _normalize_ws(str(source.get("title") or "正文摘录"))[:120],
                    "relevance_reason": f"原句命中问题关键词 {score} 个，直接来自网页正文",
                    "confidence": round(min(0.9, 0.35 + 0.08 * score), 2),
                }
            )
            picked += 1
            if picked >= per_source_cap:
                break
        if len(items) >= total_cap:
            break
    return items[:total_cap]


def _claim_key(text: str) -> str:
    """归一化结论文本，用于识别“多条证据表达同一意思”。"""
    return re.sub(r"[\s，,。.；;:：、!！?？\"'“”‘’（）()\[\]【】]+", "", str(text or "")).lower()


def _merge_claims(evidence: list[dict[str, Any]]) -> list[tuple[str, list[int]]]:
    """合并同义证据：返回 [(结论, [S 编号...]), ...]，保住第一条的原始表述。"""
    merged: list[tuple[str, list[int]]] = []
    for idx, item in enumerate(evidence, start=1):
        claim = str(item.get("claim") or item.get("quote") or "").strip()
        if not claim:
            continue
        key = _claim_key(claim)
        for existing in merged:
            if key and key == _claim_key(existing[0]):
                existing[1].append(idx)
                break
        else:
            merged.append((claim, [idx]))
    return merged


def _cite(indices: list[int]) -> str:
    return "".join(f"[S{index}]" for index in indices)


def _strip_claim_end(text: str) -> str:
    return str(text or "").strip().rstrip("。.；;，, ")


def _strip_heading(text: str) -> str:
    """去掉模型回显的小节标题（我们自己在拼接时加标题）。"""
    lines = str(text or "").strip().splitlines()
    while lines and lines[0].lstrip().startswith("#"):
        lines.pop(0)
    return "\n".join(lines).strip()


def _fallback_summary(question: str, evidence: list[dict[str, Any]]) -> str:
    """分节写作失败时的摘要兜底：结论 + 依据 + 边界，全部来自证据原句。"""
    picked: list[int] = []
    for index, item in enumerate(evidence, start=1):
        if item.get("promo"):
            continue
        picked.append(index)
        if len(picked) >= 2:
            break
    if not picked:
        picked = list(range(1, min(len(evidence), 2) + 1))
    sentences: list[str] = []
    for index in picked:
        item = evidence[index - 1]
        text = _strip_claim_end(item.get("claim") or item.get("quote") or "")
        if text:
            sentences.append(f"{text}[S{index}]。")
    sentences.append("以上判断仅基于可回溯到原文的证据，未覆盖的方面不作结论。")
    return "".join(sentences[:3])


def heuristic_draft(
    question: str,
    evidence: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    feedback: str = "",
    uncovered: Iterable[str] = (),
) -> str:
    # 研究问题可能是多行（比如直接粘贴会议纪要/表格），标题与摘要折叠成一行并去掉表格竖线，
    # 避免把 "| 议题 | 负责人 |" 这类行原样插进 Markdown 报告。
    subject = " ".join(str(question).replace("|", " ").split()) or "研究问题"
    lines = [f"# 研究报告：{subject}", ""]
    if feedback:
        lines += [f"> 修订说明：根据人工意见「{_clip(feedback, 120)}」重新生成。", ""]
    merged = _merge_claims(evidence)
    if not merged:
        lines += [
            "## 摘要",
            "",
            "本次流程没有获得可用的网页正文，因此**未找到可靠来源**，无法给出有来源支持的结论。",
            "",
            "## 关键发现",
            "",
            "- 未找到可靠来源（检索无结果、抓取失败或内容无法解析）。",
            "",
            "## 局限与未证实",
            "",
            "- 当前资料未覆盖该问题的任何方面：没有成功抓取的网页正文。",
            "- 报告不包含事实性结论，不应据此做决策。",
        ]
        return "\n".join(lines)

    # 摘要：2~3 句，用前两条证据直接回答用户问题，并标注来源角标
    sentences: list[str] = []
    for claim, indices in merged[:2]:
        sentences.append(f"{_strip_claim_end(claim)}{_cite(indices)}。")
    if len(sentences) == 1:
        sentences.append("以上结论均可回溯到网页原文，具体出处见文末来源清单。")
    lines += ["## 摘要", "", "".join(sentences), ""]

    # 关键发现：同义证据合并后逐条列出，引用写成 [S1][S2]
    lines += ["## 关键发现", ""]
    for claim, indices in merged:
        lines.append(f"- {_strip_claim_end(claim)} {_cite(indices)}")

    # 分析与推演：离线模式只陈述证据之间可以确定的关系，并明确标注这是推断
    lines.append("")
    lines += reporting.analysis_lines(evidence, list(uncovered))

    # 局限与未证实：结构化列出未覆盖子问题与证据局限，不写套话
    lines += reporting.limits_lines(evidence, list(uncovered))
    lines += ["## 来源清单", ""]
    lines += prompts.source_list_lines(evidence, sources)
    return "\n".join(lines)


_LLM: LLMClient | None = None


def get_llm() -> LLMClient:
    global _LLM
    if _LLM is None:
        _LLM = LLMClient()
    return _LLM


def set_llm(client: LLMClient | None) -> None:
    global _LLM
    _LLM = client


def reset_llm() -> None:
    set_llm(None)
