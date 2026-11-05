"""编码探测与乱码清洗测试。

覆盖三类问题：
1. 服务器 charset 声明缺失 / 写错时，必须从原始字节正确解码（UTF-8 / GBK / UTF-16）；
2. 页面混入乱码段落时，入库前必须整段丢弃；
3. 整页都是乱码时，抓取应确定性失败，而不是把脏数据喂给证据提取。
"""

from __future__ import annotations

import pytest

import text_clean
import tools

CLEAN_1 = "LangGraph 通过 checkpointer 持久化每一步状态，从而支持人工中断。"
CLEAN_2 = "当节点调用 interrupt 函数时，图会暂停并把控制权交还给调用方，等待恢复指令。"
CLEAN_3 = "调用方随后使用 Command 的 resume 参数恢复执行，节点会从 interrupt 处继续。"
GARBLED = "æµ‹è¯•æ–‡æœ¬å†…å®¹ï¼Œè¿™æ˜¯ä¸€æ®µä¹±ç "


def _html(title: str, paragraphs: list[str], head: str = "") -> str:
    body = "".join(f"<p>{text}</p>" for text in paragraphs)
    return (
        f"<html><head>{head}<title>{title}</title></head>"
        f"<body><article>{body}</article></body></html>"
    )


class _FakeResponse:
    """假响应：只需要 fetch 实现用到的几个属性。"""

    status_code = 200

    def __init__(self, raw: bytes, content_type: str = "text/html") -> None:
        self.content = raw
        self.headers = {"content-type": content_type}

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]

    def close(self) -> None:
        pass


@pytest.fixture
def fetch_with(monkeypatch: pytest.MonkeyPatch):
    """把真实网络替换成本地假响应，返回 _fetch_page_impl 的结果。"""

    def _run(raw: bytes, content_type: str = "text/html") -> dict[str, object]:
        monkeypatch.setattr(tools, "_assert_safe_url", lambda url: None)
        monkeypatch.setattr(
            tools,
            "_fetch_with_safe_redirects",
            lambda url, **kwargs: _FakeResponse(raw, content_type),
        )
        return tools._fetch_page_impl("https://example.org/page")

    return _run


# --------------------------------------------------------------------------- 编码探测
def test_decode_utf8_without_any_charset_declaration():
    raw = _html("UTF-8 页面", [CLEAN_1]).encode("utf-8")
    text, encoding = text_clean.decode_body(raw, "")
    assert "人工中断" in text
    assert encoding.startswith("utf-8")


def test_decode_gbk_from_meta_charset():
    raw = _html("GBK 页面", [CLEAN_1], head='<meta charset="gbk">').encode("gbk")
    text, encoding = text_clean.decode_body(raw, "text/html")
    assert "人工中断" in text
    assert encoding in {"gb18030", "gbk", "gb2312"}


def test_decode_gbk_without_any_declaration():
    """没有任何 charset 声明时，需要通过字节探测识别出 GBK。"""
    raw = _html("无声明页面", [CLEAN_1, CLEAN_2, CLEAN_3]).encode("gbk")
    text, _ = text_clean.decode_body(raw, "text/html")
    assert "人工中断" in text
    assert "æµ" not in text


def test_decode_when_declared_utf8_but_actually_gbk():
    """声明写错（UTF-8）但实际是 GBK：不要盲信声明，回退到正确编码。"""
    raw = _html("声明错误的页面", [CLEAN_1]).encode("gbk")
    text, encoding = text_clean.decode_body(raw, "text/html; charset=utf-8")
    assert "人工中断" in text
    assert encoding != "utf-8"


def test_decode_utf16_with_bom():
    raw = _html("UTF-16 页面", [CLEAN_1]).encode("utf-16")
    text, encoding = text_clean.decode_body(raw, "")
    assert "人工中断" in text
    assert "utf-16" in encoding


def test_decode_empty_body():
    text, encoding = text_clean.decode_body(b"", "text/html")
    assert text == ""
    assert encoding == "utf-8"


# --------------------------------------------------------------------------- 乱码判断
def test_garbled_markers_are_detected():
    # 典型 mojibake：UTF-8 中文被按 Latin-1/GBK 解码后出现的字符
    assert text_clean.mojibake_ratio(GARBLED) > 0.3
    assert text_clean.is_garbled(GARBLED)


def test_clean_texts_are_not_flagged():
    assert text_clean.mojibake_ratio(CLEAN_1) == 0.0
    assert not text_clean.is_garbled(CLEAN_1)
    french = "Le café est très bon, mais la crème brûlée est meilleure."
    assert not text_clean.is_garbled(french)


def test_short_fragment_is_never_flagged():
    """太短的片段不做判断，避免误伤标题、代码片段。"""
    assert not text_clean.is_garbled("å□")


def test_threshold_can_be_overridden(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENT_MOJIBAKE_RATIO", "0.95")
    assert not text_clean.is_garbled(GARBLED)


def test_clean_text_blocks_drops_garbled_and_invisible():
    blocks = [
        CLEAN_1,
        GARBLED,
        "\u200b" + CLEAN_2 + "\ufeff",
        "",
    ]
    kept, dropped = text_clean.clean_text_blocks(blocks)
    assert kept == [CLEAN_1, CLEAN_2], "保留干净段落，并去掉零宽字符/BOM"
    assert dropped == 2


def test_clean_text_filters_line_by_line():
    text = "\n".join([CLEAN_1, GARBLED, CLEAN_3])
    assert text_clean.clean_text(text) == "\n".join([CLEAN_1, CLEAN_3])


# --------------------------------------------------------------------------- 抓取集成
def test_fetch_decodes_gbk_page(fetch_with):
    raw = _html("GBK 编码页面", [CLEAN_1, CLEAN_2, CLEAN_3], head='<meta charset="gbk">').encode("gbk")
    page = fetch_with(raw, "text/html; charset=gbk")
    assert "checkpointer" in page["content"]
    assert page["chars"] >= 120
    assert page["encoding"] in {"gb18030", "gbk", "gb2312"}
    assert page["dropped_blocks"] == 0


def test_fetch_drops_garbled_paragraphs(fetch_with):
    raw = _html("正常标题", [CLEAN_1, CLEAN_2, CLEAN_3, GARBLED]).encode("utf-8")
    page = fetch_with(raw)
    assert "checkpointer" in page["content"]
    assert "æµ" not in page["content"], "乱码段落不能进入正文"
    assert page["dropped_blocks"] == 1


def test_fetch_uses_url_when_title_is_garbled(fetch_with):
    raw = _html(GARBLED, [CLEAN_1, CLEAN_2, CLEAN_3]).encode("utf-8")
    page = fetch_with(raw)
    assert page["title"] == "https://example.org/page"


def test_fetch_fails_when_page_is_all_garbled(fetch_with):
    raw = _html("乱码页面", [GARBLED, GARBLED + GARBLED]).encode("utf-8")
    with pytest.raises(tools.NonRetryableToolError) as excinfo:
        fetch_with(raw)
    assert "乱码" in str(excinfo.value)
