"""网页编码探测与乱码清洗。

抓取链路里有两个坑：

1. **编码**：`requests` 的 `resp.text` 依赖服务器声明的 charset。很多国内站点不声明编码、
   或者把 GBK 页面错误声明成 UTF-8 / ISO-8859-1，于是拿到的就是一屏乱码。
   这里统一从 `resp.content` 拿原始字节，自己探测编码再 decode：
   BOM → 严格 UTF-8 → HTTP 头 / meta 声明 → charset_normalizer（缺失时退回 chardet）
   → gb18030 → 最后按用户要求用 `gbk` + `errors="ignore"` 兜底。

2. **乱码块**：即使编码猜对了，页面里也可能混着乱码段落（模板占位、错误的评论、被截断的
   多字节字符等）。入库前按“可疑字符比例”做一次过滤：一段文本中 Latin-1 扩展字符、控制符、
   □/▪ 之类的符号占比超过阈值（默认 30%），就整段丢弃，避免脏数据进证据库。
"""

from __future__ import annotations

import codecs
import os
import re

# 默认丢弃阈值：可疑字符 / 非空白字符 超过 30% 视为乱码
DEFAULT_MOJIBAKE_RATIO = 0.30
# 太短的片段不做乱码判断（避免误伤代码片段、标题等）
MIN_SUSPECT_LEN = 12
# decode_body 内部用来跳过“能解码但明显是乱码”的候选编码
_CANDIDATE_SCREEN_RATIO = 0.25

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?\s*([\w.:\-]+)", re.IGNORECASE)
_XML_ENCODING_RE = re.compile(r"<\?xml[^>]+encoding\s*=\s*[\"']([\w.:\-]+)", re.IGNORECASE)

# 乱码可疑字符：控制符、Latin-1 补充区、拉丁扩展、零宽/双向控制符、制表/几何符号、替换符、私用区
_BAD_CHARS = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f"
    r"\u0080-\u00ff"
    r"\u0100-\u024f"
    r"\u200b-\u200f\u2028-\u202e\u2060"
    r"\u2500-\u25ff"
    r"\ufffd"
    r"\ue000-\uf8ff]"
)
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\u202a-\u202e\ufeff]")

# 编码声明常见别名 → Python 解码器名（gb18030 是 gbk/gb2312 的超集，解码结果一致）
_ALIASES = {
    "gbk": "gb18030",
    "gb2312": "gb18030",
    "gb_2312-80": "gb18030",
    "x-gbk": "gb18030",
    "cp936": "gb18030",
    "ms936": "gb18030",
    "iso-8859-1": "cp1252",
    "iso8859-1": "cp1252",
    "latin-1": "cp1252",
    "latin1": "cp1252",
}


# --------------------------------------------------------------------------- 编码探测
def _bom_encoding(raw: bytes) -> str:
    """带 BOM 的页面，BOM 是最高优先级的事实。"""
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        return "utf-32"
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    return ""


def _normalize_encoding(name: str) -> str:
    name = (name or "").strip().lower()
    # charset_normalizer 返回 utf_8 这种下划线命名，Python 解码器也认；只处理常见中文别名
    return _ALIASES.get(name, name)


def _declared_encodings(raw: bytes, content_type: str = "") -> list[str]:
    """HTTP 头 + HTML meta + XML 声明里声明的编码，按可信度排序。"""
    found: list[str] = []
    header_match = _CHARSET_RE.search(content_type or "")
    if header_match:
        found.append(header_match.group(1))
    head = raw[:8192].decode("latin-1", errors="ignore")
    meta_match = _CHARSET_RE.search(head)
    if meta_match:
        found.append(meta_match.group(1))
    xml_match = _XML_ENCODING_RE.search(head)
    if xml_match:
        found.append(xml_match.group(1))
    return found


def _detected_encoding(raw: bytes) -> str:
    """用 charset_normalizer 探测；没装就退回 chardet；都不可用返回空串。"""
    sample = raw[:200_000]
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(sample).best()
        if best is not None and best.encoding:
            return best.encoding
    except Exception:  # noqa: BLE001 - 探测失败就交给后面的候选编码
        pass
    try:
        import chardet

        guess = chardet.detect(sample) or {}
        return str(guess.get("encoding") or "")
    except Exception:  # noqa: BLE001
        return ""


def _encoding_candidates(raw: bytes, content_type: str = "") -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def add(name: str) -> None:
        encoding = _normalize_encoding(name)
        if not encoding or encoding in seen:
            return
        seen.add(encoding)
        ordered.append(encoding)

    # 严格 UTF-8 放最前面：现代网页绝大多数是 UTF-8，而“声明写错”比“真的不是 UTF-8”更常见
    add("utf-8")
    for declared in _declared_encodings(raw, content_type):
        add(declared)
    add(_detected_encoding(raw))
    add("gb18030")
    add("big5hkscs")
    add("utf-16")
    return ordered


def decode_body(raw: bytes, content_type: str = "") -> tuple[str, str]:
    """把响应体的原始字节解码成文本。

    返回 ``(text, encoding)``：encoding 是最终实际使用的编码，便于写日志排查乱码问题。
    """
    if not raw:
        return "", "utf-8"

    bom = _bom_encoding(raw)
    if bom:
        try:
            return raw.decode(bom), bom
        except (UnicodeDecodeError, LookupError):
            pass

    # 严格 UTF-8 能解码就等于坐实了编码：UTF-8 的多字节结构很严格，中文 GBK 页面
    # 几乎不可能整体恰好是合法 UTF-8。这里直接采信，也避免文本本身已是乱码时被误判。
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    attempts: list[tuple[float, str, str]] = []
    for encoding in _encoding_candidates(raw, content_type):
        if encoding == "utf-8":
            continue
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError, ValueError):
            continue
        ratio = mojibake_ratio(text)
        attempts.append((ratio, encoding, text))
        # 能正常解码、且乱码比例很低，直接采用
        if ratio <= _CANDIDATE_SCREEN_RATIO:
            return text, encoding

    if attempts:
        # 所有候选都能解码但都有乱码迹象：挑可疑字符最少的一个
        attempts.sort(key=lambda item: item[0])
        return attempts[0][2], attempts[0][1]

    # 兜底：按用户要求用 gbk 忽略错误字节，至少保住能读的部分
    try:
        return raw.decode("gbk", errors="ignore"), "gbk"
    except LookupError:  # pragma: no cover - 正常 Python 都有 gbk
        return raw.decode("utf-8", errors="ignore"), "utf-8"


# --------------------------------------------------------------------------- 乱码清洗
def sanitize_invisible(text: str) -> str:
    """去掉零宽字符、BOM、双向控制符等不可见字符。"""
    return _INVISIBLE_RE.sub("", str(text or ""))


def mojibake_ratio(text: str) -> float:
    """可疑字符在非空白字符中的占比，范围 0~1。"""
    compact = "".join(str(text or "").split())
    if not compact:
        return 0.0
    return len(_BAD_CHARS.findall(compact)) / len(compact)


def _threshold(value: float | None = None) -> float:
    if value is not None:
        return value
    raw = os.getenv("AGENT_MOJIBAKE_RATIO", "")
    try:
        return min(0.95, max(0.05, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_MOJIBAKE_RATIO


def is_garbled(text: str, threshold: float | None = None) -> bool:
    """判断一段文本是否乱码：太短的片段一律不算（避免误伤代码、标题）。"""
    cleaned = sanitize_invisible(text)
    compact = "".join(cleaned.split())
    if len(compact) < MIN_SUSPECT_LEN:
        return False
    return mojibake_ratio(cleaned) > _threshold(threshold)


def clean_text_blocks(
    blocks: list[str] | tuple[str, ...],
    threshold: float | None = None,
) -> tuple[list[str], int]:
    """逐段清洗：去掉不可见字符，丢弃乱码段落。

    返回 ``(保留的段落, 丢弃数量)``，丢弃数量用于日志和排障。
    """
    threshold = _threshold(threshold)
    kept: list[str] = []
    dropped = 0
    for block in blocks:
        text = sanitize_invisible(str(block or "")).strip()
        if not text or is_garbled(text, threshold):
            dropped += 1
            continue
        kept.append(text)
    return kept, dropped


def clean_text(text: str, threshold: float | None = None) -> str:
    """按行清洗正文，丢弃乱码行。"""
    blocks = str(text or "").splitlines()
    kept, _ = clean_text_blocks(blocks, threshold)
    return "\n".join(kept)
