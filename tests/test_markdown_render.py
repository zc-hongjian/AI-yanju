"""前端 Markdown 渲染器回归测试。

背景：草稿里出现「以 | 开头、但不是完整表格」的行时，旧版 renderMarkdown 的段落分支
不会推进下标，导致浏览器主线程死循环（页面无响应）。这里用 node 直接跑渲染函数，
用带超时的子进程确认不会再卡死。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _extract_renderer() -> str:
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    assert script, "index.html 里找不到 <script>"
    code = script.group(1)
    start = code.index("function escapeHtml")
    end = code.index("function statusChip")
    return code[start:end]


@pytest.mark.skipif(NODE is None, reason="需要 node 来执行前端渲染器")
def test_frontend_script_syntax_is_valid(tmp_path):
    """整段前端脚本必须能通过 node 语法检查（防止 showFinal 等改动引入语法错误）。"""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    assert script, "index.html 里找不到 <script>"
    script_path = tmp_path / "frontend_script.js"
    script_path.write_text(script.group(1), encoding="utf-8")
    result = subprocess.run(
        [NODE, "--check", str(script_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, f"前端脚本语法错误：{result.stderr}"


@pytest.mark.skipif(NODE is None, reason="需要 node 来执行前端渲染器")
def test_markdown_renderer_does_not_hang_on_unusual_lines(tmp_path):
    code = _extract_renderer()
    harness = code + textwrap.dedent(
        """
        const samples = [
          "| a | b\\nnext line",
          "# 标题\\n\\n| 列1 | 列2\\n| --- | --- |\\n| 1 | 2 |\\n",
          "line with | pipe inside\\n\\n- a\\n- b\\n",
          "# 报告：第一行\\n第二行：会议内容\\n| 不是表格\\n\\n## 摘要\\ntext",
          "```\\ncode\\n```\\n| x |\\n",
          "#### 参考来源：\\n1. https://example.org/a\\n2. text",
        ];
        for (const sample of samples) {
          const out = renderMarkdown(sample);
          if (typeof out !== "string" || out.length === 0) {
            throw new Error("renderMarkdown 返回空结果");
          }
        }
        console.log("ALL_OK");
        """
    )
    script_path = tmp_path / "render_test.mjs"
    script_path.write_text(harness, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, f"node 执行失败：{result.stderr}"
    assert "ALL_OK" in result.stdout
