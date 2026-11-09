# 研究助手评测口径与已知指标

## 两档评测

- **离线档**：离线启发式 LLM + 真实联网检索，或确定性 mock fixture。用于回归流水线、
  引用、去重、延迟，零 API 成本，可反复跑。
- **真实档**：真实大模型端到端，衡量生成质量，需要 LLM-as-judge。

两类数字不能混用。简历或 README 里写"任务成功率 85%"时必须注明是离线 LLM + 真实联网。

## 40 题测试集

`evaluation/dataset.jsonl` 共 40 题，四类各 10 题：简单事实、对比分析、时效性、多跳推理。
每题包含 `id / category / question / expected_facts`。

## 最近一次真实联网评测（real_p0）

- 任务成功率 85.0%（34/40）；
- 引用可溯源率 100%，关键结论可点击来源率 100%；
- 工具调用正确率 90.0%；
- 子问题证据覆盖率 51.2%（未达 75% 目标）；
- 证据断言支持率 99.4%（规则代理，不是语义蕴含）；
- 平均检索轮次 1.68；平均延迟 15.6s，P95 26.7s。

## 覆盖率为什么只有 51%

根因有两个：对比题只抓到单方证据；部分站点 403/521 或 JS 渲染空页导致抓取失败。
补检轮只能缓解，不能替代"按实体拆分检索 / 官方搜索 API / PDF 解析"。

## 可复现命令

- `python evaluation/run_eval.py --mode mock --tag mock_p0`
- `python evaluation/run_eval.py --mode real --tag real_p0`
- `python evaluation/make_report.py`
- `pytest -q`
