"""知识库检索验收（P0-3）：只给本地 knowledge/，问只能靠知识库回答的问题。

不联网、不调用付费 API：搜索/抓取工具被替换成"失败"实现，唯一证据来源是本地知识库。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["AGENT_LLM_MODE"] = "offline"
os.environ["AGENT_QUIET_LOG"] = "1"
os.environ.pop("OPENAI_API_KEY", None)

import tools  # noqa: E402
from graph import build_graph  # noqa: E402
from state import new_state  # noqa: E402

KB_DIR = ROOT / "knowledge"
QUESTIONS = [
    {
        "id": "kb1",
        "question": "研究助手评测里子问题覆盖率的根因是什么？",
        "expect": ["对比题", "403"],
    },
    {
        "id": "kb2",
        "question": "为什么人工中断不能把副作用放在 interrupt 之前？",
        "expect": ["重新执行", "副作用"],
    },
    {
        "id": "kb3",
        "question": "混合检索里 BM25 和向量是怎么融合的？",
        "expect": ["RRF", "排名"],
    },
    {
        "id": "kb4",
        "question": "没有 embedding Key 时知识库还能用吗？",
        "expect": ["BM25"],
    },
    {
        "id": "kb5",
        "question": "研究助手真实联网评测的证据断言支持率是多少？",
        "expect": ["99.4"],
    },
    {
        "id": "kb6",
        "question": "知识库分块的大小和重叠是多少？",
        "expect": ["800", "120"],
    },
]


def _broken_search(**_kwargs):
    raise tools.ToolError("kb-only 验收：禁止联网检索")


def _broken_fetch(url: str, **_kwargs):
    raise tools.ToolError(f"kb-only 验收：禁止抓取 {url}")


def main() -> int:
    tools.TOOL_IMPL_REGISTRY["search_web"] = _broken_search
    tools.TOOL_IMPL_REGISTRY["fetch_page"] = _broken_fetch
    graph = build_graph()
    rows: list[dict[str, object]] = []
    passed = 0
    for item in QUESTIONS:
        state = new_state(item["question"], trace_id=f"kb-{item['id']}")
        state["research_question"] = item["question"]
        result = graph.invoke(state, {"configurable": {"thread_id": f"kb-{item['id']}"}})
        payload = result["__interrupt__"][0].value if "__interrupt__" in result else {}
        report = str(payload.get("draft_report") or "")
        pages = result.get("fetched_pages") or []
        kb_pages = [page for page in pages if page.get("is_kb")]
        cited = "📚 知识库" in report and "[S" in report
        expect_hit = all(term in report for term in item["expect"])
        ok = bool(kb_pages) and cited and expect_hit
        passed += 1 if ok else 0
        rows.append(
            {
                "id": item["id"],
                "ok": ok,
                "kb_pages": len(kb_pages),
                "cited": cited,
                "expect_hit": expect_hit,
                "question": item["question"],
            }
        )

    for row in rows:
        flag = "PASS" if row["ok"] else "FAIL"
        print(
            f"{flag} {row['id']}  kb_pages={row['kb_pages']}  cited={row['cited']}  "
            f"expect={row['expect_hit']}  | {row['question']}"
        )
    print(f"\n{passed}/{len(QUESTIONS)} passed  |  KB dir: {KB_DIR}")
    summary_path = ROOT / "evaluation" / "results" / "kb_check_result.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(QUESTIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
