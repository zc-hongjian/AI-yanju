"""接入真实大模型自检：读取 `.env`，解析配置，并发起一次真实 API 调用。

用法：
    python check_llm.py
    python check_llm.py --question "LangGraph 如何实现人工中断？"

退出码：
    0 = 接入成功；1 = 配置了 Key 但调用失败；2 = 仍是离线模式（没读到 Key）
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_FILE = ROOT / ".env"
EXAMPLE_FILE = ROOT / ".env.example"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def _ensure_dependencies() -> None:
    """如果当前 Python 缺 python-dotenv，自动切到项目 .venv（和 web.py 一致）。"""
    try:
        import dotenv  # noqa: F401
        return
    except ImportError:
        pass
    if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        print(f"[提示] 当前 Python 缺少 python-dotenv：{sys.executable}", flush=True)
        print(f"       已自动切换到项目虚拟环境：{VENV_PYTHON}", flush=True)
        raise SystemExit(subprocess.run([str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]]).returncode)
    print("[错误] 缺少 python-dotenv，无法读取 .env。")
    print(f'       请运行："{sys.executable}" -m pip install -r "{ROOT / "requirements.txt"}"')
    raise SystemExit(1)


def _write_env_values(values: dict[str, str]) -> None:
    """把值写进 .env（保留注释与其它配置；没有 .env 时从模板复制一份）。"""
    base = ENV_FILE if ENV_FILE.exists() else EXAMPLE_FILE
    lines = base.read_text(encoding="utf-8", errors="replace").splitlines() if base.exists() else []
    written: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        matched = None
        for key in values:
            if stripped.startswith(f"{key}="):
                matched = key
                break
        if matched is not None:
            output.append(f"{matched}={values[matched]}")
            written.add(matched)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in written:
            output.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(output) + "\n", encoding="utf-8")


def mask_secret(secret: str) -> str:
    secret = str(secret or "")
    if not secret:
        return "（空）"
    if len(secret) <= 8:
        return secret[:2] + "*" * max(1, len(secret) - 2)
    return secret[:6] + "…" + secret[-2:]


def _read_env_value(path: Path, key: str) -> str:
    """从 .env 风格的文本里读取一个值（只用于诊断，不做完整 dotenv 解析）。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def env_file_status() -> dict[str, Any]:
    return {
        "env_exists": ENV_FILE.exists(),
        "file_key": _read_env_value(ENV_FILE, "OPENAI_API_KEY"),
        "example_key": _read_env_value(EXAMPLE_FILE, "OPENAI_API_KEY"),
        "example_model": _read_env_value(EXAMPLE_FILE, "OPENAI_MODEL"),
    }


def resolve_config() -> dict[str, Any]:
    """加载 .env 并返回当前会生效的 LLM 配置（不发起请求）。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE)
    except Exception:  # noqa: BLE001 - 没有 .env / 没装 dotenv 时继续用系统环境变量
        pass

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    requested = os.getenv("AGENT_LLM_MODE", "auto").strip().lower() or "auto"
    if requested == "auto":
        mode = "openai" if api_key else "offline"
    else:
        mode = "openai" if requested == "openai" and api_key else "offline"
    return {
        "mode": mode,
        "requested_mode": requested,
        "api_key": api_key,
        "base_url": base_url or "https://api.openai.com/v1（默认）",
        "model": model,
    }


def _price_cny(usage: dict[str, int]) -> float:
    try:
        price_in = float(os.getenv("AGENT_PRICE_INPUT_PER_MTOK", "1.0"))
        price_out = float(os.getenv("AGENT_PRICE_OUTPUT_PER_MTOK", "4.0"))
    except ValueError:
        price_in, price_out = 1.0, 4.0
    return (
        usage.get("prompt_tokens", 0) / 1_000_000 * price_in
        + usage.get("completion_tokens", 0) / 1_000_000 * price_out
    )


def main(argv: list[str] | None = None) -> int:
    _ensure_dependencies()
    parser = argparse.ArgumentParser(description="真实大模型接入自检")
    parser.add_argument("--question", default="LangGraph 如何实现人工中断？")
    parser.add_argument("--dry-run", action="store_true", help="只检查配置，不发起真实请求")
    parser.add_argument("--force", action="store_true", help="Base URL 为空但模型非 OpenAI 官方时仍尝试请求")
    parser.add_argument("--set-key", action="store_true", help="交互式写入 OPENAI_API_KEY 到 .env（输入不回显）")
    parser.add_argument("--base-url", default=None, help="写入 OPENAI_BASE_URL，例如 https://api.deepseek.com/v1")
    parser.add_argument("--model", default=None, help="写入 OPENAI_MODEL，例如 deepseek-chat")
    args = parser.parse_args(argv)

    if args.set_key or args.base_url or args.model:
        values: dict[str, str] = {}
        if args.base_url:
            values["OPENAI_BASE_URL"] = args.base_url.strip()
        if args.model:
            values["OPENAI_MODEL"] = args.model.strip()
        if args.set_key:
            key = getpass.getpass("粘贴 API Key（输入不回显，回车确认）: ").strip()
            if not key or any(ch.isspace() for ch in key) or len(key) < 10:
                print("[错误] Key 为空、含空格或长度异常，未写入。")
                return 2
            values["OPENAI_API_KEY"] = key
        _write_env_values(values)
        print(f"\n[已写入] {ENV_FILE}")
        for key, value in values.items():
            shown = mask_secret(value) if "KEY" in key else value
            print(f"  {key}={shown}")
        print("\n下一步：python check_llm.py            # 真实调用自检")
        print("        python web.py                 # 重启网页版")
        return 0

    config = resolve_config()
    status = env_file_status()
    print("=" * 64)
    print("LLM 配置")
    print("=" * 64)
    print(f"  Python 解释器         : {sys.executable}")
    print(f"  .env 文件            : {'存在' if status['env_exists'] else '缺失（应用只读取 .env）'}")
    print(f"  模式（AGENT_LLM_MODE）: {config['requested_mode']} -> 实际 {config['mode']}")
    print(f"  API Key              : {mask_secret(config['api_key'])}")
    print(f"  Base URL             : {config['base_url']}")
    print(f"  模型                 : {config['model']}")
    if status["env_exists"] and status.get("file_key") and not config["api_key"]:
        print("\n[警告] .env 里有 Key，但当前进程环境变量为空/未加载，请检查是否用错 Python 解释器。")

    if config["mode"] != "openai":
        print("\n[离线模式] 没有读到 OPENAI_API_KEY，当前仍会走启发式（不是真实大模型）。")
        if status["example_key"]:
            print("\n[发现问题] 你的 Key 填在了 `.env.example`（模板文件）里，应用不会读取它。")
            print("  正确做法：复制模板生成 `.env`，把 Key 填进 `.env`，并清空 `.env.example` 里的 Key。")
            print(r"  Windows: copy .env.example .env     macOS/Linux: cp .env.example .env")
        print("请把 .env.example 复制成 .env，并至少填写：")
        print("  OPENAI_API_KEY=你的Key")
        print("  OPENAI_BASE_URL=兼容 OpenAI 的地址（OpenAI 官方可留空）")
        print("  OPENAI_MODEL=模型名")
        print("\n常见国内兼容地址：")
        print("  DeepSeek : OPENAI_BASE_URL=https://api.deepseek.com/v1   OPENAI_MODEL=deepseek-chat")
        print("  通义千问 : OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1")
        print("             OPENAI_MODEL=qwen-plus")
        print("  智谱 GLM : OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4  OPENAI_MODEL=glm-4-flash")
        print("  本地 Ollama: OPENAI_BASE_URL=http://127.0.0.1:11434/v1  OPENAI_MODEL=qwen2.5:7b")
        print("\n填好后重新运行本脚本；web 版需要重启 `python web.py`。")
        return 2

    base_raw = os.getenv("OPENAI_BASE_URL", "").strip()
    openai_like_model = bool(re.match(r"(gpt|o[134]|text-|dall-e|whisper|chatgpt)", config["model"], re.I))
    if not base_raw and not openai_like_model:
        print("\n[警告] Base URL 为空，但模型名看起来不是 OpenAI 官方模型。")
        print(f"  当前模型：{config['model']}，如果不填 OPENAI_BASE_URL，请求会发到 OpenAI 官方接口并失败。")
        print("  请填写服务商提供的兼容地址（如 DeepSeek：https://api.deepseek.com/v1），")
        print("  或改用官方模型名（如 gpt-4o-mini）。")
        if not args.force:
            return 1

    if args.dry_run:
        print("\n[配置检查] 未发起真实请求（--dry-run）。确认配置无误后去掉该参数即可做真实调用。")
        return 0

    from llm_client import LLMClient, LLMError
    from tools import TOOL_SCHEMAS

    llm = LLMClient(mode="openai")
    search_schema = [schema for schema in TOOL_SCHEMAS if schema["function"]["name"] == "search_web"]
    print("\n" + "=" * 64)
    print(f"发起真实请求（问题：{args.question}）")
    print("=" * 64)
    started = time.perf_counter()
    try:
        plan = llm.plan_research(args.question, tools=search_schema)
    except LLMError as exc:
        print(f"\n[失败] 模型调用失败：{exc}")
        print("\n排查建议：")
        print("  1. 401：Key 无效 / 复制时带了空格；")
        print("  2. 404：Base URL 通常要带 /v1（Ollama 也是 http://127.0.0.1:11434/v1）；")
        print("  3. 超时：调大 AGENT_LLM_TIMEOUT（短调用，默认 30 秒）或 AGENT_LLM_TIMEOUT_LONG（长输出，默认 75 秒）；")
        print("  4. 网关不支持 tools：代码会自动回退到 JSON 规划，仍应能拿到搜索词。")
        return 1

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    usage = llm.usage_snapshot()
    if usage.get("total_tokens", 0):
        cost = _price_cny(usage)
        usage_text = (
            f"prompt={usage['prompt_tokens']} completion={usage['completion_tokens']} "
            f"total={usage['total_tokens']}，估算成本 ¥{cost:.6f}"
        )
    else:
        usage_text = "网关未返回 usage（部分兼容服务不返回 token 统计）"

    print("\n[成功] 已连接到真实模型")
    print(f"  provider  : {plan.get('provider')}")
    print(f"  子问题    : {len(plan.get('sub_questions') or [])} 个")
    print(f"  搜索词    : {[item.get('query') for item in plan.get('queries') or []]}")
    print(f"  耗时      : {elapsed_ms} ms")
    print(f"  token     : {usage_text}")
    print("\n下一步：重启 web 版（python web.py），页面上再次提问即可走真实模型；")
    print("也可跑小样本评测：python evaluation/run_eval.py --mode real --llm openai --tag real_llm --limit 10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
