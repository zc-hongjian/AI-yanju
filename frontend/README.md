# frontend/（前端代码索引）

> 本次整理遵循「不做任何代码改动」：前端代码**仍保持在仓库根目录**，未物理搬入本目录。
> 原因：`web.py` 用 `BASE_DIR / "index.html"` 直接返回页面，`launch.html` 也是独立静态文件；
> 把文件挪进 `frontend/` 必须同步改 `web.py` 的路径、`start.bat`、测试与文档链接，属于代码改动。
> 需要真正拆分时，按下面清单移动并同步修改引用即可。

## 前端文件清单（当前位置：仓库根目录）

| 文件 | 行数（约） | 作用 |
| --- | --- | --- |
| [`index.html`](../index.html) | 2284 | 单页主界面：研究输入、14 段执行进度、草稿/邮件/Notion 确认、历史抽屉、Trace 瀑布、模型设置、未完成/中断卡片 |
| [`launch.html`](../launch.html) | 247 | 一键启用启动页：探测 `/api/health` → 进入主界面并弹模型设置；`file://` 下降级静态演示 |
| [`start.bat`](../start.bat) | 32 | Windows 启动脚本（起 `web.py` + 4 秒后打开浏览器），严格说属于部署脚本 |

## 技术形态

- 无构建步骤：单文件 HTML + 内联 CSS/JS，双击即可演示；
- 由 `web.py` 托管时通过 NDJSON 流（`fetch` + `ReadableStream`）消费 `/api/research/stream`；
- 状态与交互全部在 `index.html` 的 IIFE 内：`NODE_ORDER` / `STAGE_FUN` / `NODE_LABELS` 必须与
  `graph._node_functions()` 顺序一致（有测试 `tests/test_web_config.py` 守护）；
- 页面原型截图统一在 [`../原型图/`](../原型图/)。

## 若要物理拆分到本目录

1. `git mv index.html launch.html frontend/`；
2. 改 `web.py`：`INDEX_FILE = BASE_DIR / "frontend" / "index.html"`、`LAUNCH_FILE` 同理；
3. 改 `start.bat` 提示文案（如涉及）；
4. 改 `tests/test_launch_page.py`、`tests/test_web_config.py` 中的路径；
5. 全量 `pytest -q`（当前 166 例）+ 实际打开页面回归。
