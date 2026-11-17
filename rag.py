"""本地知识库检索（RAG）：BM25 + 可选向量 + RRF 融合。

设计目标：**没有 embedding Key 也能用**。

- 文档：``knowledge/`` 下的 ``.md`` / ``.txt``（可用 ``AGENT_KB_DIR`` 覆盖）；
- 分块：按标题切分，再按 800 字滑窗、120 字重叠，保留 heading 路径；
- 召回：BM25（中文按字符 bigram、英文按词）始终可用；
  配了 OpenAI 兼容 embedding 时再叠加向量召回，用 RRF 融合两路排名；
- 缓存：``data/kb_index.json``，记录文档 mtime/size 与 embedding 模型，变更时自动重建；
- 降级：embedding 调用失败 → 只用 BM25，并打印一行提示，不影响主流程。

检索结果会转成 ``kb://<doc_id>#<chunk_id>`` 的伪 URL，由 ``retrieve_knowledge`` 节点
追加进 ``fetched_pages``，从而复用现有的证据抽取 / 引用 / 校验链路。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_KB_DIR = BASE_DIR / "knowledge"
DEFAULT_INDEX_PATH = BASE_DIR / "data" / "kb_index.json"

CHUNK_SIZE = int(os.getenv("AGENT_KB_CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("AGENT_KB_OVERLAP", "120"))
TOP_K = int(os.getenv("AGENT_KB_TOP_K", "6"))
BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60

_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[a-z0-9_]+")
_SENTENCE_BREAK = re.compile(r"[。！？；.!?;\n]")


# ------------------------------------------------------------------ 路径/环境
def kb_dir() -> Path:
    raw = (os.getenv("AGENT_KB_DIR") or "").strip()
    return Path(raw) if raw else DEFAULT_KB_DIR


def index_path() -> Path:
    raw = (os.getenv("AGENT_KB_INDEX") or "").strip()
    return Path(raw) if raw else DEFAULT_INDEX_PATH


def _embedding_model() -> str:
    return (os.getenv("AGENT_EMBEDDING_MODEL") or "text-embedding-3-small").strip()


# ------------------------------------------------------------------ 分块
@dataclass
class Chunk:
    doc_id: str
    title: str
    heading: str
    chunk_id: int
    text: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def tokenize(text: str) -> list[str]:
    """中文按字符 bigram、英文按词；用于 BM25（不引入分词依赖）。"""
    lowered = str(text or "").lower()
    tokens: list[str] = [match.group(0) for match in _LATIN_RE.finditer(lowered)]
    cjk = "".join(_CJK_RE.findall(lowered))
    tokens.extend(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    if len(cjk) == 1:
        tokens.append(cjk)
    return tokens


def split_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切段；没有标题时整篇一段。返回 [(heading, body)]。"""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    body: list[str] = []
    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            if heading or any(item.strip() for item in body):
                sections.append((heading, body))
            heading = match.group(2).strip()
            body = []
        else:
            body.append(line)
    if heading or any(item.strip() for item in body):
        sections.append((heading, body))
    return [(title, "\n".join(lines).strip()) for title, lines in sections if "\n".join(lines).strip()]


def chunk_section(body: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """定长滑窗，优先在句末断开；与下一块保留 overlap 字重叠。"""
    text = re.sub(r"[ \t]+", " ", str(body or "")).strip()
    if not text:
        return []
    size = max(120, size)
    overlap = max(0, min(overlap, size // 2))
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            cut = None
            for match in _SENTENCE_BREAK.finditer(window):
                if match.end() >= int(size * 0.55):
                    cut = start + match.end()
            if cut:
                end = cut
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _doc_title(text: str, fallback: str) -> str:
    for line in str(text or "").splitlines():
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
        if line.strip():
            break
    return fallback


def load_chunks(root: Path | None = None) -> list[Chunk]:
    root = root or kb_dir()
    if not root.exists():
        return []
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        doc_id = relative
        title = _doc_title(text, path.stem)
        chunk_id = 0
        for heading, body in split_sections(text):
            for piece in chunk_section(body):
                chunk_id += 1
                chunks.append(
                    Chunk(
                        doc_id=doc_id,
                        title=title,
                        heading=heading,
                        chunk_id=chunk_id,
                        text=piece,
                        url=f"kb://{doc_id}#{chunk_id}",
                    )
                )
    return chunks


# ------------------------------------------------------------------ 召回
class BM25Index:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.tokens = [tokenize(f"{chunk.title} {chunk.heading} {chunk.text}") for chunk in chunks]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        document_frequency: Counter[str] = Counter()
        for tokens in self.tokens:
            document_frequency.update(set(tokens))
        total = len(self.chunks)
        self.idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in document_frequency.items()
        }

    def scores(self, query_tokens: list[str]) -> list[float]:
        if not self.chunks or not query_tokens:
            return [0.0] * len(self.chunks)
        query = set(query_tokens)
        results: list[float] = []
        for index, tokens in enumerate(self.tokens):
            counts = Counter(tokens)
            length = self.lengths[index] or 1
            score = 0.0
            for term in query:
                frequency = counts.get(term)
                if not frequency:
                    continue
                denominator = frequency + BM25_K1 * (1 - BM25_B + BM25_B * length / (self.avg_len or 1))
                score += self.idf.get(term, 0.0) * frequency * (BM25_K1 + 1) / denominator
            results.append(score)
        return results


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def _embedder_available() -> bool:
    try:
        from llm_client import get_llm

        return getattr(get_llm(), "mode", "") == "openai"
    except Exception:  # noqa: BLE001
        return False


def _embed(texts: list[str]) -> list[list[float]] | None:
    if not texts:
        return None
    try:
        from llm_client import get_llm

        llm = get_llm()
        embed = getattr(llm, "embed", None)
        if not callable(embed):
            return None
        return embed(texts, model=_embedding_model())
    except Exception as exc:  # noqa: BLE001 - 向量不可用时静默降级
        print(f"[研究助手] 知识库 embedding 不可用（BM25 降级）：{exc}", flush=True)
        return None


def _signature(root: Path) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    if not root.exists():
        return signature
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature.append(
                {"path": path.relative_to(root).as_posix(), "mtime": int(stat.st_mtime), "size": stat.st_size}
            )
    return signature


class KnowledgeBase:
    """知识库索引：惰性构建 + 文件签名缓存；线程安全。"""

    def __init__(self, root: Path | None = None, cache_path: Path | None = None) -> None:
        self.root = Path(root) if root else kb_dir()
        self.cache_path = Path(cache_path) if cache_path else index_path()
        self._lock = threading.Lock()
        self.chunks: list[Chunk] = []
        self.vectors: list[list[float]] | None = None
        self.model = ""
        self._loaded = False

    # -------------------------------------------------- 构建/缓存
    def _load_cache(self, signature: list[dict[str, Any]], model: str) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if data.get("signature") != signature or data.get("model", "") != model:
            return False
        try:
            self.chunks = [Chunk(**item) for item in data.get("chunks") or []]
        except (TypeError, KeyError):
            return False
        vectors = data.get("vectors")
        self.vectors = [list(map(float, vector)) for vector in vectors] if vectors else None
        if self.vectors is not None and len(self.vectors) != len(self.chunks):
            self.vectors = None
        self.model = model
        return True

    def _save_cache(self, signature: list[dict[str, Any]]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "model": self.model,
                "signature": signature,
                "chunks": [chunk.to_dict() for chunk in self.chunks],
                "vectors": self.vectors,
            }
            self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            print(f"[研究助手] 知识库索引写入失败（不影响检索）：{exc}", flush=True)

    def refresh(self, force: bool = False) -> int:
        """重建索引（文档变更/首次加载时），返回 chunk 数。"""
        with self._lock:
            signature = _signature(self.root)
            model = _embedding_model() if _embedder_available() else ""
            if not force and self._loaded and signature == getattr(self, "_signature", None):
                return len(self.chunks)
            if not force and self._load_cache(signature, model):
                self._signature = signature
                self._loaded = True
                return len(self.chunks)
            self.chunks = load_chunks(self.root)
            self.vectors = None
            self.model = ""
            if self.chunks and model:
                vectors = _embed([f"{chunk.heading}\n{chunk.text}" for chunk in self.chunks])
                if vectors is not None:
                    self.vectors = vectors
                    self.model = model
            self._signature = signature
            self._loaded = True
            self._save_cache(signature)
            return len(self.chunks)

    # -------------------------------------------------- 检索
    def search(self, queries: list[str], k: int = TOP_K) -> list[dict[str, Any]]:
        queries = [str(query).strip() for query in queries if str(query or "").strip()]
        if not queries:
            return []
        if not self._loaded:
            self.refresh()
        if not self.chunks:
            return []
        bm25 = BM25Index(self.chunks)
        query_vectors: list[list[float]] | None = None
        if self.vectors:
            query_vectors = _embed(queries)
        fused: dict[int, float] = {}
        hits: dict[int, dict[str, Any]] = {}
        for query_index, query in enumerate(queries):
            lexical = bm25.scores(tokenize(query))
            lexical_rank = sorted(range(len(lexical)), key=lambda index: lexical[index], reverse=True)
            for rank, index in enumerate(lexical_rank):
                if lexical[index] <= 0:
                    break
                fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank + 1)
            if query_vectors and query_index < len(query_vectors):
                semantic = [_cosine(query_vectors[query_index], vector) for vector in self.vectors]
                semantic_rank = sorted(range(len(semantic)), key=lambda index: semantic[index], reverse=True)
                for rank, index in enumerate(semantic_rank[: max(k * 4, 20)]):
                    fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank + 1)
        ranked = sorted(fused.items(), key=lambda item: item[1], reverse=True)[: max(1, k)]
        results: list[dict[str, Any]] = []
        for index, score in ranked:
            chunk = self.chunks[index]
            results.append(
                {
                    "doc_id": chunk.doc_id,
                    "title": chunk.title,
                    "heading": chunk.heading,
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "url": chunk.url,
                    "score": round(score, 6),
                    "retriever": "hybrid" if self.vectors else "bm25",
                }
            )
        return results

    def stats(self) -> dict[str, Any]:
        if not self._loaded:
            self.refresh()
        docs = {chunk.doc_id for chunk in self.chunks}
        return {
            "documents": len(docs),
            "chunks": len(self.chunks),
            "vector": bool(self.vectors),
            "embedding_model": self.model,
            "dir": str(self.root),
        }


# ------------------------------------------------------------------ 单例
_KB: KnowledgeBase | None = None
_KB_LOCK = threading.Lock()


def get_knowledge_base() -> KnowledgeBase:
    global _KB
    with _KB_LOCK:
        if _KB is None or _KB.root != kb_dir():
            _KB = KnowledgeBase()
        return _KB


def reset_knowledge_base() -> None:
    """测试用：清掉单例与缓存（不删磁盘文件）。"""
    global _KB
    with _KB_LOCK:
        _KB = None


def search_knowledge(queries: list[str], k: int = TOP_K) -> list[dict[str, Any]]:
    return get_knowledge_base().search(queries, k)


def knowledge_stats() -> dict[str, Any]:
    return get_knowledge_base().stats()


def knowledge_documents(root: Path | None = None) -> list[dict[str, Any]]:
    """列出知识库文档（路径/标题/分块数/大小/修改时间），供网页管理面板展示。

    这里直接读盘而不是复用 KnowledgeBase 单例：面板要的是“当前目录的真实内容”，
    即使索引缓存是旧的、或 embedding 不可用，也必须能如实列出文件。
    """
    base = Path(root) if root else kb_dir()
    if not base.exists():
        return []
    chunks_by_doc: dict[str, int] = {}
    titles: dict[str, str] = {}
    for chunk in load_chunks(base):
        chunks_by_doc[chunk.doc_id] = chunks_by_doc.get(chunk.doc_id, 0) + 1
        titles.setdefault(chunk.doc_id, chunk.title)
    documents: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".txt"}:
            continue
        relative = path.relative_to(base).as_posix()
        try:
            stat = path.stat()
            size, mtime = stat.st_size, int(stat.st_mtime)
        except OSError:
            size, mtime = 0, 0
        documents.append(
            {
                "path": relative,
                "title": titles.get(relative) or path.stem,
                "chunks": chunks_by_doc.get(relative, 0),
                "size": size,
                "mtime": mtime,
            }
        )
    return documents


def doc_fingerprint(path: Path) -> str:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return ""
