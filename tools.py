"""5 个 Function Calling 工具。

- search_web / fetch_page / summarize_sources 是读操作；
- send_email / save_to_notion 是写操作，必须 human_approved=True 才会执行。

每个工具都有：
1) JSON Schema 描述（TOOL_SCHEMAS，可直接喂给 OpenAI tools 参数）；
2) 参数校验（ToolValidationError）；
3) 超时（requests timeout / smtplib timeout）；
4) 指数退避重试（最多 3 次，可用 AGENT_TOOL_MAX_ATTEMPTS 覆盖）；
5) 失败后返回结构化 ToolResult，由节点记录日志并进入降级/人工处理，不静默失败。
"""

from __future__ import annotations

import ipaddress
import os
import re
import smtplib
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import cache
from llm_client import LLMError, get_llm
from text_clean import clean_text, clean_text_blocks, decode_body, is_garbled, sanitize_invisible

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
HTTP_TIMEOUT = float(os.getenv("AGENT_HTTP_TIMEOUT", "10"))
SEARCH_TIMEOUT = (4.0, 8.0)  # 搜索提供方：连接 4s / 读取 8s，避免一个站点拖住整条链路
MAX_BYTES = 2_000_000
MAX_PAGE_CHARS = int(os.getenv("AGENT_MAX_PAGE_CHARS", "12000"))
USER_AGENT = "ResearchAssistantMVP/1.0 (educational demo; +https://example.invalid)"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
WRITE_TOOLS = {"send_email", "save_to_notion"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEFAULT_PROVIDER_FAILURE_LIMIT = 2
# 广告/弹窗/订阅等噪声容器：按 class/id/role 等属性识别后整块删除
_AD_ATTR_RE = re.compile(
    r"(?:^|[\s_\-])(?:ad|ads|advert|advertisement|banner|promo|sponsor|cookie|popup|"
    r"modal|subscribe|newsletter|share|social|comment|广告|推广)(?:$|[\s_\-])",
    re.IGNORECASE,
)


def _is_ad_node(tag: Any) -> bool:
    attrs = " ".join(
        str(tag.get(attr) or "") for attr in ("class", "id", "role", "data-testid", "aria-label")
    )
    return bool(_AD_ATTR_RE.search(attrs))


# --------------------------------------------------------------------------- 异常
class ToolError(RuntimeError):
    """工具层的所有错误基类。"""


class ToolValidationError(ToolError):
    """参数不合法，重试没有意义。"""


class NonRetryableToolError(ToolError):
    """确定性失败（404、证书错误、JS 空页面等），重试没有意义，直接降级。"""


class WriteActionNotApproved(ToolError):
    """写操作未经人工确认。"""

    def __init__(self, tool: str) -> None:
        super().__init__(f"{tool} 是写操作，必须先获得人工确认（human_approved=True）")
        self.tool = tool


class ToolExecutionError(ToolError):
    """重试耗尽后的执行失败。"""

    def __init__(self, message: str, *, attempts: int = 0, last_error: str = "") -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: Any = None
    error: str | None = None
    attempts: int = 0
    latency_ms: int = 0
    rejected: bool = False
    retry_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if self.ok:
            return f"成功（attempts={self.attempts}, {self.latency_ms}ms）"
        if self.rejected:
            return "已拒绝：需要人工确认"
        return f"失败（attempts={self.attempts}）：{self.error}"


# --------------------------------------------------------------------------- 重试
def _max_attempts() -> int:
    try:
        return max(1, int(os.getenv("AGENT_TOOL_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))))
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def _base_delay() -> float:
    try:
        return max(0.0, float(os.getenv("AGENT_RETRY_BASE_DELAY", str(DEFAULT_BASE_DELAY))))
    except ValueError:
        return DEFAULT_BASE_DELAY


def _tool_timeout(name: str) -> float | None:
    """单次工具调用的总时间预算（秒）。可用环境变量覆盖。"""
    raw = {
        "search_web": os.getenv("AGENT_SEARCH_TIMEOUT", "20"),
        "fetch_page": os.getenv("AGENT_FETCH_TIMEOUT", "15"),
        # 证据提取是一次长输出 LLM 调用（实测 glm-4-flash ~750 token 需 27~33s），
        # 预算给 25s 只会必然超时；这里与 llm_client 的 summarize 超时共用同一个变量。
        "summarize_sources": os.getenv("AGENT_SUMMARIZE_TIMEOUT", "75"),
    }.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def retry_call(
    fn: Callable[[], Any],
    *,
    attempts: int | None = None,
    base_delay: float | None = None,
    sleep: Callable[[float], None] | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
    label: str | None = None,
    deadline: float | None = None,
) -> tuple[Any, int]:
    """执行 fn，异常时按指数退避重试。返回 (结果, 实际尝试次数)。

    参数错误、写操作拒绝、确定性失败不重试。退避时间：base, base*2, base*4 ...
    deadline 是 time.monotonic() 时间戳；超过预算就停止重试并抛 ToolExecutionError。
    """
    max_attempts = attempts or _max_attempts()
    delay_base = _base_delay() if base_delay is None else base_delay
    sleeper = sleep or time.sleep
    last_error: Exception | None = None
    deadline_hit = False
    attempt = 0
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(), attempt
        except (ToolValidationError, WriteActionNotApproved, NonRetryableToolError):
            raise
        except Exception as exc:  # noqa: BLE001 - 工具层统一兜底后重试
            last_error = exc
            if attempt >= max_attempts:
                break
            if deadline is not None and time.monotonic() >= deadline:
                deadline_hit = True
                break
            delay = delay_base * (2 ** (attempt - 1))
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleeper(delay)
    name = label or getattr(fn, "__name__", "tool")
    if deadline_hit:
        raise ToolExecutionError(
            f"{name} 超过时间预算，已停止重试：{last_error}",
            attempts=attempt,
            last_error=str(last_error),
        ) from last_error
    raise ToolExecutionError(
        f"{name} 重试 {max_attempts} 次后仍失败：{last_error}",
        attempts=max_attempts,
        last_error=str(last_error),
    ) from last_error


# --------------------------------------------------------------------------- 校验
def _require_text(value: Any, field_name: str, *, max_len: int = 10_000, min_len: int = 1) -> str:
    if not isinstance(value, str):
        raise ToolValidationError(f"{field_name} 必须是字符串")
    text = value.strip()
    if len(text) < min_len:
        raise ToolValidationError(f"{field_name} 不能为空")
    if len(text) > max_len:
        text = text[:max_len]
    return text


def _coerce_int(value: Any, field_name: str, *, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    try:
        num = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolValidationError(f"{field_name} 必须是整数") from exc
    return max(lo, min(hi, num))


def _assert_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ToolValidationError(f"拒绝访问内网/保留地址：{ip}")


def _resolve_ips(host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolError(f"域名解析失败：{host}") from exc
    ips: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for info in infos:
        try:
            ips.add(ipaddress.ip_address(str(info[4][0])))
        except ValueError:
            continue
    return ips


def _assert_safe_url(url: str) -> None:
    """阻止抓取内网/保留地址（字面 IP + DNS 解析结果 + 重定向目标都检查）。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ToolValidationError("url 只支持 http/https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ToolValidationError("url 缺少主机名")
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise ToolValidationError("拒绝访问内网主机")
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        resolved = _resolve_ips(host)
        if not resolved:
            raise ToolValidationError(f"无法解析主机：{host}") from None
        for ip in resolved:
            _assert_public_ip(ip)
        return
    _assert_public_ip(literal_ip)


def _elapsed_ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))


# ============================================================================ 读取类工具
_PROVIDER_FAILURES: dict[str, int] = {}
_PROVIDER_LOCK = threading.Lock()


def _provider_failure_limit() -> int:
    try:
        return max(1, int(os.getenv("AGENT_PROVIDER_FAILURE_LIMIT", str(DEFAULT_PROVIDER_FAILURE_LIMIT))))
    except ValueError:
        return DEFAULT_PROVIDER_FAILURE_LIMIT


def _provider_tripped(name: str) -> bool:
    with _PROVIDER_LOCK:
        return _PROVIDER_FAILURES.get(name, 0) >= _provider_failure_limit()


def _mark_provider_failure(name: str) -> None:
    with _PROVIDER_LOCK:
        _PROVIDER_FAILURES[name] = _PROVIDER_FAILURES.get(name, 0) + 1


def _reset_provider_failure(name: str) -> None:
    with _PROVIDER_LOCK:
        _PROVIDER_FAILURES.pop(name, None)


def reset_provider_breakers() -> None:
    """清空搜索提供方熔断状态（批量评测时每个任务重新开始，避免前一个任务的失败拖累后续）。"""
    with _PROVIDER_LOCK:
        _PROVIDER_FAILURES.clear()


def _search_web_impl(query: Any, max_results: Any = 5) -> list[dict[str, Any]]:
    query = _require_text(query, "query", max_len=300)
    limit = _coerce_int(max_results, "max_results", default=5, lo=1, hi=10)
    cache_key = f"{limit}|{' '.join(query.split())}"
    cached = cache.get_value("search", cache_key)
    if isinstance(cached, list) and cached:
        return cached
    budget = float(os.getenv("AGENT_SEARCH_TIMEOUT", "20"))
    deadline = time.monotonic() + budget
    failures: list[str] = []
    for provider in _provider_order(query):
        name = provider.__name__
        if _provider_tripped(name):
            failures.append(f"{name}: 连续失败，已跳过")
            continue
        if time.monotonic() >= deadline:
            failures.append(f"搜索时间预算 {budget}s 已用完")
            break
        try:
            hits = provider(query, limit)
        except Exception as exc:  # noqa: BLE001 - 换下一个提供方
            _mark_provider_failure(name)
            failures.append(f"{name}: {exc}")
            continue
        if hits:
            _reset_provider_failure(name)
            for idx, hit in enumerate(hits):
                hit.setdefault("score", round(max(0.5, 0.95 - idx * 0.05), 2))
            results = hits[:limit]
            cache.put_value("search", cache_key, results)
            return results
        failures.append(f"{name}: 无结果")
    raise ToolExecutionError("所有搜索提供方均失败：" + "；".join(failures))


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SPECIFIC_TECH_RE = re.compile(
    r"langgraph|langchain|llamaindex|crewai|autogen|dify|fastapi|django|react|vue|bm25|milvus|"
    r"interrupt|checkpointer|mcp|gpt|claude|llama|qwen|deepseek",
    re.IGNORECASE,
)


def _provider_order(query: str = "") -> list[Callable[[str, int], list[dict[str, Any]]]]:
    """搜索提供方顺序，可用 AGENT_SEARCH_PROVIDERS 覆盖。

    中文宽泛问题（如「2025 年 AI Agent 企业落地挑战」）Bing 常返回不相关结果，优先 360；
    命中具体框架/技术词的查询仍优先 Bing（官方文档、GitHub 更全）。
    """
    raw = os.getenv("AGENT_SEARCH_PROVIDERS", "bing,so360,duckduckgo,wikipedia")
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if (
        _CJK_RE.search(query or "")
        and not _SPECIFIC_TECH_RE.search(query or "")
        and "so360" in names
    ):
        names.remove("so360")
        names.insert(0, "so360")
    ordered: list[Callable[[str, int], list[dict[str, Any]]]] = []
    for name in names:
        provider = SEARCH_PROVIDERS.get(name)
        if provider is not None and provider not in ordered:
            ordered.append(provider)
    return ordered or list(SEARCH_PROVIDERS.values())


SEARCH_PROVIDERS: dict[str, Callable[[str, int], list[dict[str, Any]]]] = {}


def _response_html(resp: requests.Response) -> str:
    """搜索页也用原始字节 + 探测编码解析，避免 resp.text 依赖服务器错误声明。"""
    text, _ = decode_body(resp.content, resp.headers.get("content-type", ""))
    return text


def _clean_hit_text(text: str) -> str:
    """标题/摘要先去掉不可见字符；乱码摘要直接置空，乱码标题由调用方丢弃整条结果。"""
    text = sanitize_invisible(text or "").strip()
    return "" if is_garbled(text) else text


def _search_bing(query: str, limit: int) -> list[dict[str, Any]]:
    """Bing 网页搜索（HTML 解析）。在国内网络通常可用，作为首选提供方。"""
    resp = requests.get(
        "https://www.bing.com/search",
        params={"q": query, "setlang": "en"},
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        timeout=SEARCH_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ToolError(f"Bing HTTP {resp.status_code}")
    soup = BeautifulSoup(_response_html(resp), "html.parser")
    hits: list[dict[str, Any]] = []
    for block in soup.select("li.b_algo"):
        link = block.select_one("h2 a")
        if link is None:
            continue
        url = str(link.get("href") or "").strip()
        if not url.startswith("http"):
            continue
        title = _clean_hit_text(link.get_text(" ", strip=True))
        if not title:
            continue
        snippet_el = block.select_one(".b_caption p") or block.select_one("p")
        snippet = _clean_hit_text(snippet_el.get_text(" ", strip=True)) if snippet_el else ""
        hits.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "query": query,
            }
        )
        if len(hits) >= limit:
            break
    return hits


def _search_so360(query: str, limit: int) -> list[dict[str, Any]]:
    """360 搜索（so.com）。中文宽泛问题的结果通常比 Bing 更贴近主题。

    结果链接是 360 的跳转地址，但 <a data-mdurl="..."> 里保存了真实 URL，直接取用，
    避免每个结果都再发一次跳转请求。
    """
    resp = requests.get(
        "https://www.so.com/s",
        params={"q": query},
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        timeout=SEARCH_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ToolError(f"360 HTTP {resp.status_code}")
    soup = BeautifulSoup(_response_html(resp), "html.parser")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in soup.select("li.res-list"):
        link = block.select_one("h3 a")
        if link is None:
            continue
        url = str(link.get("data-mdurl") or "").strip()
        if not url.startswith("http"):
            url = str(link.get("href") or "").strip()
            if not url.startswith("http") or "so.com/link" in url:
                continue
        if url in seen:
            continue
        title = _clean_hit_text(link.get_text(" ", strip=True))
        if not title:
            continue
        snippet_el = block.select_one(".res-desc") or block.select_one(".res-list-summary") or block.select_one("p")
        snippet = _clean_hit_text(snippet_el.get_text(" ", strip=True)) if snippet_el else ""
        if not snippet:
            snippet = _clean_hit_text(block.get_text(" ", strip=True))[:180]
        seen.add(url)
        hits.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "query": query,
            }
        )
        if len(hits) >= limit:
            break
    return hits


def _search_duckduckgo(query: str, limit: int) -> list[dict[str, Any]]:
    resp = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=SEARCH_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ToolError(f"DuckDuckGo HTTP {resp.status_code}")
    soup = BeautifulSoup(_response_html(resp), "html.parser")
    hits: list[dict[str, Any]] = []
    for block in soup.select("div.result"):
        link = block.select_one("a.result__a")
        if link is None:
            continue
        url = _unwrap_ddg_url(link.get("href", ""))
        if not url:
            continue
        title = _clean_hit_text(link.get_text(" ", strip=True))
        if not title:
            continue
        snippet_el = block.select_one(".result__snippet")
        snippet = _clean_hit_text(snippet_el.get_text(" ", strip=True)) if snippet_el else ""
        hits.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "query": query,
            }
        )
        if len(hits) >= limit:
            break
    return hits


def _unwrap_ddg_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target.startswith("http") else ""
    return href if href.startswith("http") else ""


def _search_wikipedia(query: str, limit: int) -> list[dict[str, Any]]:
    """备用搜索源：Wikipedia API，不需要 Key，结果 URL 真实可抓取。"""
    lang = "zh" if re.search(r"[\u4e00-\u9fff]", query) else "en"
    resp = requests.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": limit,
        },
        headers={"User-Agent": USER_AGENT},
        timeout=SEARCH_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ToolError(f"Wikipedia HTTP {resp.status_code}")
    payload = resp.json()
    hits: list[dict[str, Any]] = []
    for item in payload.get("query", {}).get("search", []):
        title = str(item.get("title") or "")
        if not title:
            continue
        snippet = BeautifulSoup(str(item.get("snippet") or ""), "html.parser").get_text(" ", strip=True)
        hits.append(
            {
                "title": title,
                "url": f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                "snippet": snippet,
                "query": query,
            }
        )
    return hits


SEARCH_PROVIDERS.update(
    {
        "bing": _search_bing,
        "so360": _search_so360,
        "duckduckgo": _search_duckduckgo,
        "wikipedia": _search_wikipedia,
    }
)


def _fetch_with_safe_redirects(url: str, *, max_hops: int = 4) -> requests.Response:
    """手动跟随重定向：每一跳都先做 SSRF 校验再发起请求，避免请求先打到内网。"""
    current = url
    for _ in range(max_hops + 1):
        _assert_safe_url(current)
        resp = requests.get(
            current,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=(5, HTTP_TIMEOUT),
            stream=True,
            allow_redirects=False,
        )
        location = resp.headers.get("location")
        if resp.is_redirect and location:
            resp.close()
            current = urljoin(current, location)
            continue
        return resp
    raise NonRetryableToolError(f"重定向次数过多（>{max_hops} 次）")


def _fetch_page_impl(url: Any) -> dict[str, Any]:
    url = _require_text(url, "url", max_len=2000)
    _assert_safe_url(url)
    cache_key = cache.normalize_url(url)
    cached = cache.get_value("fetch", cache_key)
    if isinstance(cached, dict) and cached.get("content"):
        hit = dict(cached)
        hit["cached"] = True
        return hit
    try:
        resp = _fetch_with_safe_redirects(url)
    except requests.exceptions.SSLError as exc:
        raise NonRetryableToolError(f"SSL 证书校验失败：{exc}") from exc
    try:
        if resp.status_code >= 400:
            if resp.status_code in {408, 429} or resp.status_code >= 500:
                raise ToolError(f"HTTP {resp.status_code}")
            raise NonRetryableToolError(f"HTTP {resp.status_code}（页面不可用）")
        ctype = (resp.headers.get("content-type") or "").lower()
        content_type_header = str(resp.headers.get("content-type") or "")
        if ctype and not any(token in ctype for token in ("text/html", "application/xhtml", "text/plain")):
            raise NonRetryableToolError(f"不支持的内容类型：{ctype}")
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_BYTES:
                break
        raw = b"".join(chunks)
    finally:
        resp.close()
    # 不用 resp.text：很多站点 charset 声明缺失或写错，统一从原始字节探测编码
    html, encoding = decode_body(raw, content_type_header)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"]):
        tag.decompose()
    for tag in soup.find_all(_is_ad_node):
        tag.decompose()
    title = sanitize_invisible(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if not title or is_garbled(title):
        title = url
    root = soup.find("article") or soup.find("main") or soup.body or soup
    parts: list[str] = []
    seen: set[str] = set()
    for node in root.find_all(["h1", "h2", "h3", "p", "li"]):
        text = sanitize_invisible(node.get_text(" ", strip=True))
        if len(text) < 25:
            continue
        key = text[:80]
        if key in seen:
            continue
        seen.add(key)
        parts.append(text)
    # 入库前清洗：丢弃乱码段落，避免脏数据进入证据提取
    parts, dropped_blocks = clean_text_blocks(parts)
    content = "\n".join(parts)[:MAX_PAGE_CHARS].strip()
    if not parts and dropped_blocks:
        raise NonRetryableToolError(f"正文疑似乱码（已丢弃 {dropped_blocks} 个段落）")
    if len(content) < 120:
        raise NonRetryableToolError("正文提取失败（内容过短，可能是 JS 渲染页面或编码无法识别）")
    payload = {
        "url": url,
        "title": title[:200],
        "content": content,
        "chars": len(content),
        "encoding": encoding,
        "dropped_blocks": dropped_blocks,
    }
    cache.put_value("fetch", cache_key, payload)
    return payload


def _summarize_sources_impl(sources: Any, research_question: Any) -> dict[str, Any]:
    question = _require_text(research_question, "research_question", max_len=500)
    if not isinstance(sources, list) or not sources:
        raise ToolValidationError("sources 必须是非空数组")
    normalized: list[dict[str, str]] = []
    for idx, raw in enumerate(sources):
        if isinstance(raw, str):
            raw = {"url": raw}
        if not isinstance(raw, dict):
            raise ToolValidationError(f"sources[{idx}] 必须是对象")
        url = str(raw.get("url") or "").strip()
        if not url:
            raise ToolValidationError(f"sources[{idx}].url 不能为空")
        normalized.append(
            {
                "url": url,
                # 二次防线：source 也可能来自调用方直接传入，入库前再清洗一次乱码
                "title": sanitize_invisible(str(raw.get("title") or ""))[:300],
                "content": clean_text(str(raw.get("content") or ""))[:MAX_PAGE_CHARS],
            }
        )
    result = get_llm().summarize_evidence(question, normalized)
    evidence = result.get("evidence") or []
    allowed = {item["url"].rstrip("/").lower() for item in normalized}
    verified: list[dict[str, Any]] = []
    for item in evidence:
        if str(item.get("source_url", "")).rstrip("/").lower() not in allowed:
            continue
        verified.append(item)
    note = str(result.get("note") or "")
    if not verified:
        note = (note + "；未找到可靠来源").strip("；")
    return {"evidence": verified, "note": note, "mode": result.get("mode", "")}


# ============================================================================ 写入类工具
def _send_email_impl(to: Any, subject: Any, body: Any, *, human_approved: bool = False) -> dict[str, Any]:
    if not human_approved:
        raise WriteActionNotApproved("send_email")
    to = _require_text(to, "to", max_len=320)
    subject = _require_text(subject, "subject", max_len=200)
    body = _require_text(body, "body", max_len=40_000)
    if not EMAIL_RE.match(to):
        raise ToolValidationError("收件人邮箱格式不合法")
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if smtp_host:
        return _send_via_smtp(smtp_host, to, subject, body)
    return _save_outbox_email(to, subject, body)


def _send_via_smtp(smtp_host: str, to: str, subject: str, body: str) -> dict[str, Any]:
    port = _coerce_int(os.getenv("SMTP_PORT"), "SMTP_PORT", default=465, lo=1, hi=65535)
    use_ssl = os.getenv("SMTP_USE_SSL", "true").strip().lower() in {"1", "true", "yes", "on"}
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", "").strip() or user or "research-assistant@localhost"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="research-assistant.local")
    message.set_content(body)
    try:
        if use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(smtp_host, port, timeout=HTTP_TIMEOUT)
        else:
            server = smtplib.SMTP(smtp_host, port, timeout=HTTP_TIMEOUT)
            server.starttls()
        with server:
            if user:
                server.login(user, password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise ToolError(f"SMTP 发送失败：{exc}") from exc
    return {"mode": "smtp", "to": to, "subject": subject, "message_id": message["Message-ID"]}


def _save_outbox_email(to: str, subject: str, body: str) -> dict[str, Any]:
    outbox = _outbox_dir() / "emails"
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_to = re.sub(r"[^A-Za-z0-9_.@+-]", "_", to)
    path = outbox / f"{stamp}-{uuid.uuid4().hex[:6]}-{safe_to}.eml"
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="research-assistant.local")
    message["X-Delivery-Mode"] = "dry-run (SMTP_HOST 未配置)"
    message.set_content(body)
    path.write_text(message.as_string(), encoding="utf-8")
    return {
        "mode": "dry_run",
        "to": to,
        "subject": subject,
        "message_id": message["Message-ID"],
        "path": str(path),
        "note": "未配置 SMTP_HOST，邮件已写入 outbox，未真正发送",
    }


def _normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw_tags = [part.strip() for part in tags.split(",")]
    elif isinstance(tags, list):
        raw_tags = [str(part).strip() for part in tags]
    else:
        raise ToolValidationError("tags 必须是字符串数组")
    cleaned: list[str] = []
    for tag in raw_tags:
        if not tag:
            continue
        tag = tag[:50]
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned[:10]


def _save_to_notion_impl(
    title: Any,
    content: Any,
    tags: Any = None,
    *,
    human_approved: bool = False,
) -> dict[str, Any]:
    if not human_approved:
        raise WriteActionNotApproved("save_to_notion")
    title = _require_text(title, "title", max_len=200)
    content = _require_text(content, "content", max_len=100_000)
    normalized_tags = _normalize_tags(tags)
    token = os.getenv("NOTION_TOKEN", "").strip()
    page_id = os.getenv("NOTION_PAGE_ID", "").strip()
    database_id = os.getenv("NOTION_DATABASE_ID", "").strip()
    if token and (page_id or database_id):
        return _save_via_notion_api(token, page_id, database_id, title, content, normalized_tags)
    return _save_outbox_notion(title, content, normalized_tags)


def _save_via_notion_api(
    token: str,
    page_id: str,
    database_id: str,
    title: str,
    content: str,
    tags: list[str],
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": os.getenv("NOTION_VERSION", "2022-06-28"),
        "Content-Type": "application/json",
    }
    parent = {"database_id": database_id} if database_id else {"page_id": page_id}
    properties: dict[str, Any] = {}
    title_payload = [{"type": "text", "text": {"content": title}}]
    if database_id:
        properties[os.getenv("NOTION_TITLE_PROPERTY", "Name")] = {"title": title_payload}
    else:
        properties["title"] = {"title": title_payload}
    children = _markdown_to_blocks(content)
    if tags:
        children.insert(
            0,
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {"content": "标签：" + "、".join(tags)}}],
                    "icon": {"emoji": "🏷️"},
                },
            },
        )
    payload = {"parent": parent, "properties": properties, "children": children[:100]}
    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=payload,
        timeout=(5, HTTP_TIMEOUT),
    )
    if resp.status_code >= 400:
        error_body, _ = decode_body(resp.content, resp.headers.get("content-type", ""))
        raise ToolError(f"Notion API {resp.status_code}: {error_body[:300]}")
    data = resp.json()
    return {"mode": "notion_api", "page_id": data.get("id"), "url": data.get("url"), "title": title}


def _markdown_to_blocks(content: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for line in content.splitlines():
        text = line.strip()
        if not text or text.startswith("```"):
            continue
        block_type = "paragraph"
        value = text
        if text.startswith("### "):
            block_type, value = "heading_3", text[4:]
        elif text.startswith("## "):
            block_type, value = "heading_2", text[3:]
        elif text.startswith("# "):
            block_type, value = "heading_1", text[2:]
        elif text.startswith(("- ", "* ")):
            block_type, value = "bulleted_list_item", text[2:]
        value = value.replace("**", "").replace("`", "")
        for start in range(0, max(1, len(value)), 1800):
            piece = value[start : start + 1800]
            if not piece:
                continue
            blocks.append(
                {
                    "object": "block",
                    "type": block_type,
                    block_type: {"rich_text": [{"type": "text", "text": {"content": piece}}]},
                }
            )
            if block_type.startswith("heading"):
                break
        if len(blocks) >= 95:
            break
    return blocks


def _save_outbox_notion(title: str, content: str, tags: list[str]) -> dict[str, Any]:
    outbox = _outbox_dir() / "notion"
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r'[\\/:*?"<>|\s]+', "-", title)[:60].strip("-") or "report"
    path = outbox / f"{stamp}-{uuid.uuid4().hex[:6]}-{slug}.md"
    header = (
        "---\n"
        f"title: {title}\n"
        f"tags: {tags}\n"
        f"saved_at: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        "mode: dry-run (NOTION_TOKEN 未配置)\n"
        "---\n\n"
    )
    path.write_text(header + content, encoding="utf-8")
    return {
        "mode": "dry_run",
        "title": title,
        "tags": tags,
        "path": str(path),
        "note": "未配置 NOTION_TOKEN，内容已写入 outbox/notion，未真正同步",
    }


def _outbox_dir() -> Path:
    return Path(os.getenv("AGENT_OUTBOX_DIR", "outbox"))


# ============================================================================ 调度层
TOOL_IMPL_REGISTRY: dict[str, Callable[..., Any]] = {
    "search_web": _search_web_impl,
    "fetch_page": _fetch_page_impl,
    "summarize_sources": _summarize_sources_impl,
    "send_email": _send_email_impl,
    "save_to_notion": _save_to_notion_impl,
}


def call_tool(name: str, args: dict[str, Any] | None = None, *, human_approved: bool = False) -> ToolResult:
    """统一入口：校验 → 写操作门禁 → 重试 → 结构化结果。"""
    payload = dict(args or {})
    payload.pop("human_approved", None)
    impl = TOOL_IMPL_REGISTRY.get(name)
    if impl is None:
        return ToolResult(tool=name, ok=False, error=f"未知工具：{name}", attempts=0)
    if name in WRITE_TOOLS:
        if not human_approved:
            return ToolResult(
                tool=name,
                ok=False,
                rejected=True,
                error=f"{name} 是写操作，未获得人工确认，已拒绝执行",
                attempts=0,
            )
        payload["human_approved"] = True
    start = time.perf_counter()
    retry_events: list[dict[str, Any]] = []
    timeout = _tool_timeout(name)
    deadline = time.monotonic() + timeout if timeout else None
    try:
        data, attempts = retry_call(
            lambda: impl(**payload),
            label=name,
            deadline=deadline,
            on_retry=lambda attempt, delay, exc: retry_events.append(
                {"attempt": attempt, "delay": round(delay, 3), "error": str(exc)}
            ),
        )
    except WriteActionNotApproved as exc:
        return ToolResult(
            tool=name, ok=False, rejected=True, error=str(exc), attempts=0,
            latency_ms=_elapsed_ms(start),
        )
    except ToolValidationError as exc:
        return ToolResult(
            tool=name, ok=False, error=f"参数校验失败：{exc}", attempts=0,
            latency_ms=_elapsed_ms(start),
        )
    except NonRetryableToolError as exc:
        return ToolResult(
            tool=name, ok=False, error=f"不可重试失败：{exc}", attempts=1,
            latency_ms=_elapsed_ms(start), retry_events=retry_events,
        )
    except ToolExecutionError as exc:
        return ToolResult(
            tool=name, ok=False, error=str(exc), attempts=exc.attempts,
            latency_ms=_elapsed_ms(start), retry_events=retry_events,
        )
    except Exception as exc:  # noqa: BLE001 - 兜底，避免整个图崩掉
        return ToolResult(
            tool=name, ok=False, error=f"未预期错误：{exc}", attempts=1,
            latency_ms=_elapsed_ms(start), retry_events=retry_events,
        )
    return ToolResult(
        tool=name, ok=True, data=data, attempts=attempts,
        latency_ms=_elapsed_ms(start), retry_events=retry_events,
    )


# --------------------------------------------------------------- 供人直接调用的函数
def search_web(query: str, max_results: int = 5) -> ToolResult:
    return call_tool("search_web", {"query": query, "max_results": max_results})


def fetch_page(url: str) -> ToolResult:
    return call_tool("fetch_page", {"url": url})


def summarize_sources(sources: list[dict[str, Any]], research_question: str) -> ToolResult:
    return call_tool("summarize_sources", {"sources": sources, "research_question": research_question})


def send_email(to: str, subject: str, body: str, *, human_approved: bool = False) -> ToolResult:
    if not human_approved:
        raise WriteActionNotApproved("send_email")
    return call_tool("send_email", {"to": to, "subject": subject, "body": body}, human_approved=True)


def save_to_notion(
    title: str,
    content: str,
    tags: list[str] | None = None,
    *,
    human_approved: bool = False,
) -> ToolResult:
    if not human_approved:
        raise WriteActionNotApproved("save_to_notion")
    return call_tool(
        "save_to_notion",
        {"title": title, "content": content, "tags": tags or []},
        human_approved=True,
    )


# --------------------------------------------------------------------------- Schema
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "搜索公开网页，返回标题、URL 和摘要。用于研究问题的初步检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索查询词，尽量具体，不要包含 URL。",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回结果数量，1~10，默认 5。",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "抓取指定 URL 的网页正文并清洗为纯文本。只能抓取公开的 http/https 页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的网页 URL，必须是搜索结果中出现的真实 URL。",
                    }
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_sources",
            "description": "从已抓取的网页正文中提取带来源标注的证据，返回证据列表和置信度。禁止引入来源之外的信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "description": "已抓取的来源列表。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "来源 URL"},
                                "title": {"type": "string", "description": "来源标题"},
                                "content": {"type": "string", "description": "来源正文"},
                            },
                            "required": ["url", "content"],
                            "additionalProperties": False,
                        },
                    },
                    "research_question": {
                        "type": "string",
                        "description": "要回答的研究问题。",
                    },
                },
                "required": ["sources", "research_question"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "把研究报告发送到指定邮箱。写操作，必须由人工确认后才能执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "收件人邮箱地址"},
                    "subject": {"type": "string", "description": "邮件主题"},
                    "body": {"type": "string", "description": "邮件正文，通常是研究报告全文"},
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_to_notion",
            "description": "把研究报告保存到 Notion 页面或数据库。写操作，必须由人工确认后才能执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "页面标题"},
                    "content": {"type": "string", "description": "页面正文，Markdown 格式"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表，最多 10 个",
                    },
                },
                "required": ["title", "content"],
                "additionalProperties": False,
            },
        },
    },
]


def tool_names() -> list[str]:
    return [schema["function"]["name"] for schema in TOOL_SCHEMAS]
