"""pytest 公共配置：强制离线 LLM、隔离 outbox、提供假的检索/抓取工具。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试不依赖任何真实 API / 网络
os.environ["AGENT_LLM_MODE"] = "offline"
os.environ["AGENT_QUIET_LOG"] = "1"
os.environ["AGENT_RETRY_BASE_DELAY"] = "0.5"
os.environ["AGENT_TOOL_MAX_ATTEMPTS"] = "3"
for _key in ("OPENAI_API_KEY", "SMTP_HOST", "NOTION_TOKEN", "DEFAULT_EMAIL_TO"):
    os.environ.pop(_key, None)

# 持久化库隔离：web.py 在 import 时就建 checkpointer，必须在此之前指到临时目录，
# 否则测试会把 data/*.sqlite 写进仓库。放 .tmp/ 下（已被 .gitignore 忽略）。
_TMP_ROOT = ROOT / ".tmp" / "pytest-data"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP_ROOT)))
os.environ["AGENT_CHECKPOINT_DB"] = str(_TEST_DATA_DIR / "checkpoints.sqlite")
os.environ["AGENT_SESSIONS_DB"] = str(_TEST_DATA_DIR / "sessions.sqlite")

# 知识库隔离：默认指向空目录，避免仓库里的 knowledge/ 影响既有测试；
# RAG 用例会自己 setenv 到临时文档目录。
_EMPTY_KB = _TEST_DATA_DIR / "empty-kb"
_EMPTY_KB.mkdir(parents=True, exist_ok=True)
os.environ["AGENT_KB_DIR"] = str(_EMPTY_KB)
os.environ["AGENT_KB_INDEX"] = str(_TEST_DATA_DIR / "kb_index.json")

# 缓存默认关闭：既有用例断言的是“每次真的调用工具”，不能命中缓存被短路；
# 缓存/审计的库也都放临时目录，避免测试把 data/ 写脏。
os.environ["AGENT_CACHE_ENABLED"] = "0"
os.environ["AGENT_CACHE_DB"] = str(_TEST_DATA_DIR / "cache.sqlite")
os.environ["AGENT_WRITE_AUDIT_DB"] = str(_TEST_DATA_DIR / "write_audit.sqlite")

import llm_client  # noqa: E402
import tools  # noqa: E402

# 两个真实可抓取的“网页”，内容互不重叠，便于校验来源标注
PAGES: dict[str, dict[str, str]] = {
    "https://example.org/alpha": {
        "title": "Alpha：LangGraph 人工中断机制",
        "content": (
            "LangGraph 通过 checkpointer 持久化每一步的状态，从而支持人工中断。"
            "当节点调用 interrupt 函数时，图会暂停并把控制权交还给调用方。"
            "调用方随后使用 Command 的 resume 参数恢复执行，节点会从 interrupt 处继续。"
            "这种机制要求编译图时提供 checkpointer，并为每次运行指定 thread_id。"
            "如果缺少 checkpointer，interrupt 会直接报错，流程无法暂停。"
        ),
    },
    "https://example.org/beta": {
        "title": "Beta：Human-in-the-loop 的两种用法",
        "content": (
            "在 LangGraph 中，human-in-the-loop 通常用于写操作前的确认。"
            "interrupt_before 可以在节点执行前静态中断，适合固定的确认点。"
            "动态的人工确认则更适合在节点内部调用 interrupt，根据运行时数据决定是否暂停。"
            "两种方式都依赖 checkpoint 才能恢复，否则中断后的状态会丢失。"
        ),
    },
}


@pytest.fixture(autouse=True)
def offline_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试都用离线启发式 LLM，避免任何真实 API 调用。"""
    monkeypatch.setenv("AGENT_LLM_MODE", "offline")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm_client.reset_llm()
    yield
    llm_client.reset_llm()


@pytest.fixture
def fake_web(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """把 search_web / fetch_page 替换成本地假实现，记录调用参数。"""
    calls: dict[str, list[str]] = {"search": [], "fetch": []}

    def fake_search(query: str, max_results: int = 5) -> list[dict[str, object]]:
        calls["search"].append(query)
        hits = []
        for index, (url, page) in enumerate(PAGES.items()):
            hits.append(
                {
                    "title": page["title"],
                    "url": url,
                    "snippet": page["content"][:60],
                    "score": round(0.95 - index * 0.1, 2),
                    "query": query,
                }
            )
        return hits[: int(max_results)]

    def fake_fetch(url: str) -> dict[str, object]:
        calls["fetch"].append(url)
        if url not in PAGES:
            raise tools.ToolError(f"404: {url}")
        page = PAGES[url]
        return {
            "url": url,
            "title": page["title"],
            "content": page["content"],
            "chars": len(page["content"]),
        }

    monkeypatch.setitem(tools.TOOL_IMPL_REGISTRY, "search_web", fake_search)
    monkeypatch.setitem(tools.TOOL_IMPL_REGISTRY, "fetch_page", fake_fetch)
    return calls
