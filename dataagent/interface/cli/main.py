# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
import argparse
import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.rule import Rule

    RICH_AVAILABLE = True
except ImportError:
    import builtins

    RICH_AVAILABLE = False

    class Console:  # type: ignore[no-redef]
        @classmethod
        def print(cls, *args: object, **kwargs: object) -> None:
            sep = kwargs.get("sep", " ")
            end = kwargs.get("end", "\n")
            builtins.print(sep.join(str(a) for a in args), end=end)

    class Markdown:  # type: ignore[no-redef]
        def __init__(self, text: str, **_: object) -> None:
            self._text = text

        def __str__(self) -> str:
            return self._text

    class Panel:  # type: ignore[no-redef]
        def __init__(self, content: object, **_: object) -> None:
            self._content = str(content)

        def __str__(self) -> str:
            return self._content

    class Prompt:  # type: ignore[no-redef]
        @staticmethod
        def ask(prompt_text: str, **_: object) -> str:
            return input(f"{prompt_text} ")

    class Rule:  # type: ignore[no-redef]
        def __init__(self, title: str = "", **_: object) -> None:
            pass

        def __str__(self) -> str:
            return "---"


from dataagent.config import ConfigManager
from dataagent.interface.sdk.agent import DataAgent
from dataagent.interface.sdk.loader import load_agent_from_config
from dataagent.utils.log import logger
from dataagent.utils.runtime_paths import dataagent_package_path

console = Console()


def resolve_config_path(config_path: str | Path) -> Path:
    """解析配置文件路径

    Args:
        config_path: 配置文件路径

    Returns:
        解析后的绝对路径
    """
    path = Path(config_path)

    # 如果是相对路径，从当前工作目录开始解析
    if not path.is_absolute():
        path = Path.cwd() / path

    # 检查文件是否存在
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    return path


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _flatten_dict(data: dict, prefix: str = "") -> dict[str, object]:
    items: dict[str, object] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            items.update(_flatten_dict(value, path))
        else:
            items[path] = value
    return items


def _resolve_env_refs(value: str, path: str) -> tuple[str, list[str]]:
    pattern = r"\$env\{([^}]+)\}"
    matches = re.findall(pattern, value)
    if not matches:
        return value, []

    resolved = value
    missing: list[str] = []
    for var_name in matches:
        env_value = os.getenv(var_name)
        if env_value is None:
            missing.append(var_name)
        else:
            resolved = resolved.replace(f"$env{{{var_name}}}", env_value)
    return resolved, missing


def run_config_check(config_path: str | Path, default_config_path: str | None = None) -> int:
    """诊断配置中的 env 引用与常见问题。"""
    try:
        config_file = resolve_config_path(config_path)
    except Exception as e:
        logger.error(f"配置文件加载失败: {e}")
        return 2

    try:
        config_data = _load_yaml(config_file)
        if default_config_path:
            default_file = resolve_config_path(default_config_path)
            default_data = _load_yaml(default_file)
            cm = ConfigManager()
            config_data = cm.merge_configs(default_data, config_data)
    except Exception as e:
        logger.error(f"读取 YAML 失败: {e}")
        return 2

    flat = _flatten_dict(config_data)
    exit_code = 0

    for path, raw_value in flat.items():
        if not isinstance(raw_value, str):
            continue
        if "$env{" in raw_value:
            resolved, missing = _resolve_env_refs(raw_value, path)
            if missing:
                exit_code = 1
                logger.error(f"[FAIL] {path} = {raw_value} -> 缺少环境变量: {', '.join(missing)}")
            else:
                logger.info(f"[OK] {path} = {raw_value} -> {resolved}")
        elif re.search(r"api_key\s*:|\bsk-[A-Za-z0-9_-]+", raw_value):
            logger.warning(f"[WARN] {path} 似乎包含硬编码密钥，请迁移到 .env")

    if exit_code == 0:
        logger.info("配置诊断通过。")
    return exit_code


def load_agent_from_config_path(config_path: str | Path) -> DataAgent:
    """解析配置路径、加载并返回 Agent，并输出加载日志。

    Args:
        config_path: 配置文件路径

    Returns:
        加载好的 DataAgent 实例
    """
    config_file = resolve_config_path(config_path)
    logger.debug(f"正在加载配置文件: {config_file}")
    agent = load_agent_from_config(str(config_file))
    logger.debug(f"Agent '{agent.name}' 加载成功！")
    return agent


def log_terminal_welcome(title: str = "DataAgent 交互模式", support_multiline: bool = False) -> None:
    """打印终端交互模式的欢迎与使用说明。"""
    lines = [
        "输入 `quit` / `exit` / `q` 退出程序",
        "输入 `help` 查看帮助信息",
    ]
    if support_multiline:
        lines.append("支持多行输入，Ctrl+J 换行，Enter 发送")
    console.print()
    console.print(
        Panel(
            Markdown("\n".join(f"- {line}" for line in lines)),
            title=f"[bold cyan]{title}[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()


async def _read_multiline_input() -> str:
    """读取多行用户输入。Alt+Enter 换行，Enter 提交。"""
    from dataagent.utils.cli.terminal_input import multiline_input

    return (await asyncio.to_thread(multiline_input, "> ")).strip()


def _cli_optional_str(value: str | None) -> str | None:
    """CLI 可选字符串：空串视为未提供。"""
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped if stripped else None


async def _run_terminal_chat_loop(
    agent: DataAgent,
    get_user_input: Callable[[], Awaitable[str]],
    *,
    enable_portrait: bool = False,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """终端交互循环：在进程内持续读入用户输入，直到 quit/help 退出或异常。

    Args:
        agent: DataAgent 实例
        get_user_input: 无参异步可调用对象，每次返回一行或多行用户输入
        enable_portrait: 是否开启 LLM 用户画像（snapshot/profile、Planner 注入）；``messages.json`` 在有
            ``user_id``/``session_id`` 时默认读写，不依赖本开关。
        user_id: 显式用户 ID；未传则与 Flex 一致由配置 ``USER_ID`` 兜底（默认 anonymous）
        session_id: 显式会话 ID；未传则本进程内生成 ``时间戳_uuid``，多轮共用。
    """
    import datetime
    import uuid
    from datetime import UTC

    uid = _cli_optional_str(user_id)
    fixed_session = _cli_optional_str(session_id)
    if fixed_session is not None:
        session_id_resolved = fixed_session
    else:
        # 同一次 CLI 进程内所有轮次共享同一 session；格式对齐 DataAgent.chat()
        session_id_resolved = datetime.datetime.now(UTC).strftime("%Y%m%d_%H%M%S_") + str(uuid.uuid4())

    run_id = 0
    stream_enabled = bool(getattr(getattr(agent, "_chat_agent", None), "debug", False))
    skip_display = stream_enabled and RICH_AVAILABLE
    while True:
        try:
            user_input = await get_user_input()

            if not user_input:
                continue

            if user_input.lower() in {"quit", "exit", "q"}:
                console.print("[dim]再见！[/dim]")
                break
            if user_input.lower() == "help":
                print_help()
                continue

            initial_state: dict = {
                "user_query": user_input,
                "run_id": run_id,
                "session_id": session_id_resolved,
                "enable_portrait": bool(enable_portrait),
            }
            if uid is not None:
                initial_state["user_id"] = uid
            response = await agent.chat(user_query=user_input, initial_state=initial_state)
            if isinstance(response, dict) and response.get("error"):
                console.print(
                    Panel(
                        str(response.get("final_answer") or response["error"]),
                        title="[bold red]❌ 错误[/bold red]",
                        border_style="red",
                        padding=(1, 2),
                    )
                )
            elif not skip_display and isinstance(response, dict) and response.get("messages"):
                final_message = response["messages"][-1]
                final_content = getattr(final_message, "content", str(final_message))
                if final_content:
                    console.print(
                        Panel(
                            Markdown(str(final_content)),
                            title="[bold cyan]🤖 Agent[/bold cyan]",
                            border_style="cyan",
                            padding=(1, 2),
                        )
                    )
                    console.print()
            run_id += 1

        except KeyboardInterrupt:
            logger.error("\n\n程序被用户中断")
            break
        except Exception as e:
            logger.exception(f"处理请求时出错: {e}")
            console.print(
                Panel(
                    str(e),
                    title="[bold red]❌ 请求异常[/bold red]",
                    border_style="red",
                    padding=(1, 2),
                )
            )
            continue


async def run_terminal_mode(
    config_path: str | Path,
    *,
    enable_portrait: bool = False,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """运行终端交互模式

    Args:
        config_path: 配置文件路径
        enable_portrait: 是否开启 LLM 用户画像（snapshot/profile）；消息持久化默认开启（需 user/session）
        user_id: 可选，用户 ID（默认 anonymous）
        session_id: 可选，固定会话 ID（默认每进程生成）
    """
    try:
        agent = load_agent_from_config_path(config_path)
        log_terminal_welcome("DataAgent 交互模式", support_multiline=True)
        await _run_terminal_chat_loop(
            agent,
            _read_multiline_input,
            enable_portrait=enable_portrait,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        logger.error(f"启动失败: {e}")
        raise


def mask_api_key(api_key: str) -> str:
    """脱敏 API Key，仅保留首尾各 2 位字符。"""
    value = api_key.strip()
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


async def run_quickstart() -> None:
    """运行 Quickstart 交互式配置并启动 Agent

    流程：
    1. 加载内置 example_agent 配置（或用户指定的 config）
    2. 通过命令行交互询问关键配置
    3. 使用独立 ConfigManager 实例覆盖内存中的配置（不污染模块级全局）
    4. 基于覆盖后的配置构建 DataAgent 并进入一次交互对话
    入口：
    uv run -m dataagent quickstart
    """
    try:
        quickstart_config = dataagent_package_path("core", "flex", "examples", "quickstart.yaml")
        config_file = resolve_config_path(str(quickstart_config))
        quickstart_config = dataagent_package_path("core", "flex", "flex_default_configs.yaml")
        default_config = resolve_config_path(str(quickstart_config))

        logger.trace(f"正在加载 Quickstart 示例配置文件: {config_file}")
        quickstart_cm = ConfigManager()
        quickstart_cm.reload(str(config_file), str(default_config))

        cur_chat_model = quickstart_cm.get("MODEL.chat_model.params.model") or ""
        new_chat_model = input(
            f"\n1) 请输入主对话模型 chat_model 的 model 名称 (当前: {cur_chat_model or '未配置'}): "
        ).strip()
        if new_chat_model:
            quickstart_cm.set("MODEL.chat_model.params.model", new_chat_model)
            console.print(f"[green]已更新[/green] MODEL.chat_model.params.model = {new_chat_model}")
        cur_chat_model_provider = quickstart_cm.get("MODEL.chat_model.provider") or ""
        if cur_chat_model_provider:
            cur_chat_base = os.getenv(f"{cur_chat_model_provider.upper()}_BASE_URL") or ""
            cur_chat_model_key = os.getenv(f"{cur_chat_model_provider.upper()}_API_KEY") or ""
        else:
            cur_chat_base = ""
            cur_chat_model_key = ""
        cur_chat_base = quickstart_cm.get("MODEL.chat_model.params.base_url") or cur_chat_base
        new_chat_base = input(
            f"\n2) 请输入主对话模型 chat_model 的 base_url (当前: {cur_chat_base or '未配置'}): "
        ).strip()
        if new_chat_base:
            quickstart_cm.set("MODEL.chat_model.params.base_url", new_chat_base)
            console.print(f"[green]已更新[/green] MODEL.chat_model.params.base_url = {new_chat_base}")

        # 2.2 主对话模型 API Key（存储在 MODEL.<chat_cfg_key>.params.api_key）
        cur_chat_model_key = quickstart_cm.get("MODEL.chat_model.params.api_key") or cur_chat_model_key
        cur_chat_model_key_label = mask_api_key(cur_chat_model_key) if cur_chat_model_key else "未配置"
        chat_model_key = Prompt.ask(
            f"\n3) 请输入 API Key (当前: {cur_chat_model_key_label})",
            password=True,
        ).strip()
        if chat_model_key:
            quickstart_cm.set("MODEL.chat_model.params.api_key", chat_model_key)
            console.print(f"[green]已更新[/green] chat_model 的 api_key = {mask_api_key(chat_model_key)}")

        console.print()
        console.print("[bold cyan]Quickstart 配置完成，正在构建 Agent ...[/bold cyan]")

        # 3. 基于覆盖后的配置构建 Agent（使用独立 ConfigManager，不写入全局单例）
        agent = DataAgent(config=quickstart_cm)
        logger.trace(f"[green]Agent '{agent.name}' 加载成功！[/green]")

        log_terminal_welcome("DataAgent Quickstart 交互模式", support_multiline=True)
        await _run_terminal_chat_loop(agent, _read_multiline_input)

    except Exception as e:
        logger.error(f"Quickstart 启动失败: {e}")
        raise


def print_help():
    """打印帮助信息"""
    help_text = """
- `help`: 显示此帮助信息
- `quit` / `exit` / `q`: 退出程序

直接输入您的数据分析需求，Agent 会自动理解并执行相应任务。

示例：
- `分析销售数据的趋势`
- `生成用户行为报告`
- `查询最近30天的订单统计`
"""
    console.print(
        Panel(
            Markdown(help_text.strip()),
            title="[bold cyan]DataAgent 帮助信息[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()


def run_serve_a2a_mode(
    config_path: str | Path,
    host: str = "",
    port: int = 9999,
    jsonrpc_path: str = "/a2a/jsonrpc",
    rest_path: str = "/a2a/rest",
    auth_token: str | None = None,
) -> None:
    """Run A2A 1.0 protocol server mode.

    Starts an independent A2A Server that exposes a DataAgent
    as an A2A 1.0-compliant service supporting JSON-RPC and REST transports.

    Args:
        config_path: Config file path.
        host: Server host.
        port: Server port.
        jsonrpc_path: JSON-RPC route path.
        rest_path: REST route path.
        auth_token: Bearer token for authentication. If None, no auth required.
    """
    try:
        from dataagent.a2a_server import DataAgentExecutor, build_agent_card, run_a2a_server
    except ImportError as e:
        logger.error(f"A2A server mode requires a2a-sdk>=1.0.0: {e}")
        logger.error("Install with: pip install a2a-sdk>=1.0.0")
        raise

    if not host:
        raise ValueError("Host must be specified")
    try:
        with open(config_path, encoding="utf-8") as f:
            raw_config = yaml.safe_load(f) or {}

        agent_config = raw_config.get("AGENT_CONFIG")
        if agent_config is None:
            raise ValueError(
                f"Missing required 'AGENT_CONFIG' field in config file: {config_path}\n"
                "Please ensure your YAML config contains an 'AGENT_CONFIG' section."
            )

        # Load agent after validation
        agent = load_agent_from_config_path(config_path)

        # Build A2A AgentCard
        agent_card = build_agent_card(agent, host=host, port=port, jsonrpc_path=jsonrpc_path, rest_path=rest_path)

        # Create AgentExecutor
        executor = DataAgentExecutor(agent)

        # Print service info
        console.print(Rule("[bold cyan]DataAgent A2A 1.0 Server[/bold cyan]"))
        console.print(f"[green]Server URL:[/green] http://{host}:{port}")
        console.print(f"[green]AgentCard:[/green] http://{host}:{port}/.well-known/agent.json")
        console.print(f"[green]JSON-RPC:[/green] http://{host}:{port}{jsonrpc_path}")
        console.print(f"[green]REST:[/green] http://{host}:{port}{rest_path}")
        if auth_token:
            console.print("[green]Auth:[/green] Bearer token enabled")
        console.print("[dim]Press Ctrl+C to stop\n")

        # Start A2A server
        run_a2a_server(
            agent_card=agent_card,
            executor=executor,
            host=host,
            port=port,
            jsonrpc_path=jsonrpc_path,
            rest_path=rest_path,
            auth_token=auth_token,
        )

    except Exception as e:
        logger.error(f"Failed to start A2A server: {e}")
        raise


def main():
    """主入口函数"""
    parser = argparse.ArgumentParser(
        description="DataAgent - 数据分析Agent框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 终端交互模式
  python -m dataagent --config dataagent/core/flex/examples/data_analyst_agent.yaml

  # 指定用户与会话 ID（messages.json 默认续接；LLM 画像加 --portrait）
  python -m dataagent --config path/to.yaml --user alice --session 20260101_my_session --portrait

  # Web服务模式
  python -m dataagent serve --config dataagent/core/flex/examples/data_analyst_agent.yaml

  # 指定服务地址和端口
  python -m dataagent serve --config dataagent/core/flex/examples/data_analyst_agent.yaml --host 0.0.0.0 --port 8080

  # A2A 1.0 协议服务模式
  python -m dataagent serve-a2a --config path/to/config.yaml
        """,
    )

    # 添加子命令
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # serve-a2a子命令
    serve_a2a_parser = subparsers.add_parser("serve-a2a", help="启动A2A 1.0协议服务模式")
    serve_a2a_parser.add_argument("--config", "-c", required=True, help="配置文件路径")
    serve_a2a_parser.add_argument("--host", default="0.0.0.0", help="服务主机地址")
    serve_a2a_parser.add_argument("--port", "-p", type=int, default=9999, help="服务端口 (默认: 9999)")
    serve_a2a_parser.add_argument("--jsonrpc-path", default="/a2a/jsonrpc", help="JSON-RPC路由路径")
    serve_a2a_parser.add_argument("--rest-path", default="/a2a/rest", help="REST路由路径")
    serve_a2a_parser.add_argument(
        "--auth-token", default=None, help="Bearer 鉴权 Token（设置后请求需携带 Authorization: Bearer <token>）"
    )

    # quickstart 子命令
    quickstart_parser = subparsers.add_parser("quickstart", help="交互式配置并启动 Agent（基于示例配置）")
    quickstart_parser.add_argument(
        "--config",
        "-c",
        help="示例配置文件路径 (默认使用 dataagent/core/flex/examples/quickstart.yaml)",
    )

    # config 子命令
    config_parser = subparsers.add_parser("config", help="配置相关工具")
    config_subparsers = config_parser.add_subparsers(dest="config_command", help="配置命令")
    config_check_parser = config_subparsers.add_parser("check", help="诊断配置并检查 $env{VAR} 依赖")
    config_check_parser.add_argument("config_path", help="配置文件路径")
    config_check_parser.add_argument(
        "--default",
        "-d",
        help="默认配置文件路径（可选，用于合并诊断）",
    )

    # 终端模式参数（默认模式）
    parser.add_argument("--config", "-c", help="配置文件路径")
    parser.add_argument(
        "--portrait",
        action="store_true",
        default=False,
        help="开启 LLM 用户画像（snapshot/profile 与 Planner 注入）；messages.json 在有 user/session 时默认读写",
    )
    parser.add_argument(
        "--user",
        "-u",
        default=None,
        metavar="USER_ID",
        help="用户 ID（默认与配置 USER_ID 一致，一般为 anonymous）",
    )
    parser.add_argument(
        "--session",
        "-s",
        default=None,
        metavar="SESSION_ID",
        help="会话 ID：默认本进程内生成 时间戳+uuid；指定则固定该会话 ID（messages.json 默认续接）",
    )
    parser.add_argument("--version", "-v", action="version", version="DataAgent 0.1.0")

    args = parser.parse_args()

    # 处理命令
    if args.command == "serve-a2a":
        # A2A 1.0协议服务模式
        run_serve_a2a_mode(args.config, args.host, args.port, args.jsonrpc_path, args.rest_path, args.auth_token)
    elif args.command == "quickstart":
        # Quickstart 交互模式
        asyncio.run(run_quickstart())
    elif args.command == "config" and args.config_command == "check":
        exit_code = run_config_check(args.config_path, args.default)
        sys.exit(exit_code)
    else:
        # 终端交互模式（兼容原有用法）
        if not args.config:
            parser.print_help()
            sys.exit(1)

        asyncio.run(
            run_terminal_mode(
                args.config,
                enable_portrait=args.portrait,
                user_id=args.user,
                session_id=args.session,
            )
        )


if __name__ == "__main__":
    main()
