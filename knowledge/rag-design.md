# 研究助手的混合检索（RAG）设计

## 为什么是混合检索

只用向量检索有两个问题：中文短查询的召回不稳定；部分模型服务（例如 DeepSeek）不提供
embedding 接口，换模型就断。混合方案让 BM25 始终可用，向量只是加分项。

## 分块策略

按 Markdown 标题切段，再按 800 字滑窗、120 字重叠；每块保留 `heading` 路径。
800 字约等于中文 500~600 token，top-6 约 4k token，给多来源摘要留出预算；
重叠用于避免定义句被切断。

## 召回与融合

- BM25：中文按字符 bigram、英文按词，k1=1.5、b=0.75；
- 向量：OpenAI 兼容 `/embeddings`，默认 `text-embedding-3-small`，可用
  `AGENT_EMBEDDING_MODEL` 覆盖（智谱可用 `embedding-3`）；
- 融合：RRF（Reciprocal Rank Fusion），`score = Σ 1/(60 + rank)`，两路排名都参与。

## 降级路径

| 情况 | 行为 |
| --- | --- |
| 没有 embedding Key | 纯 BM25，检索仍可用 |
| embedding 调用失败 | 打印提示并降级 BM25，不中断研究 |
| 知识库为空 | `retrieve_knowledge` 节点直接跳过 |

## 引用怎么处理

知识库片段被转成 `kb://<doc_id>#<chunk_id>` 伪 URL，追加进 `fetched_pages`，
复用现有的证据抽取、`[S#]` 编号、URL 白名单与支持性校验。来源清单里知识库条目
显示为「📚 知识库」而不是可点击链接。

## 索引缓存

索引缓存在 `data/kb_index.json`，记录文档 mtime/size 与 embedding 模型；
文档变化或换模型时自动重建。规模上限大约一万个 chunk：向量用纯 Python 余弦计算，
超过这个量级应换 FAISS/Chroma 或专用向量库。
