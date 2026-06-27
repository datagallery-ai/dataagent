# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""DataAgent CLI 入口。"""

import argparse
import asyncio
import datetime
import os
import re
import sys
import uuid
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
from dataagent.utils.cli.rich_renderer import StreamRenderer
from dataagent.utils.log import logger
from dataagent.utils.runtime_paths import dataagent_package_path

console = Console()


def resolve_config_path(config_path: str | Path) -> Path:
    path = Path(config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
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


def _resolve_env_refs(value: str) -> tuple[str, list[str]]:
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
            resolved, missing = _resolve_env_refs(raw_value)
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
    config_file = resolve_config_path(config_path)
    logger.debug(f"正在加载配置文件: {config_file}")
    agent = load_agent_from_config(str(config_file))
    logger.debug(f"Agent '{agent.name()}' 加载成功！")
    return agent


def log_terminal_welcome(title: str = "DataAgent 交互模式", support_multiline: bool = False) -> None:
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
    from dataagent.utils.cli.terminal_input import multiline_input

    return (await asyncio.to_thread(multiline_input, "> ")).strip()


async def _prompt_for_human_feedback(response: dict) -> dict:
    """Render Jiuwen HITL requests and return a batch resume payload."""
    interrupts = response.get("interrupts", [])
    if not isinstance(interrupts, list) or not interrupts:
        raise ValueError("HITL response does not contain any interrupt details")

    responses: list[dict] = []
    for interrupt in interrupts:
        if not isinstance(interrupt, dict):
            continue
        interrupt_id = str(interrupt.get("interrupt_id", "")).strip()
        if not interrupt_id:
            raise ValueError("HITL interrupt is missing interrupt_id")

        questions = interrupt.get("questions", [])
        if isinstance(questions, list) and questions:
            answers: dict[str, str] = {}
            for question in questions:
                if not isinstance(question, dict):
                    continue
                question_text = str(question.get("question") or interrupt.get("message") or "请提供反馈")
                header = str(question.get("header") or "需要您的输入")
                options = question.get("options", [])
                if isinstance(options, list) and options:
                    option_lines = []
                    for option in options:
                        if isinstance(option, dict):
                            label = str(option.get("label", "")).strip()
                            description = str(option.get("description", "")).strip()
                            option_lines.append(
                                f"{label}: {description}" if description else label
                            )
                    if option_lines:
                        console.print(
                            Panel(
                                "\n".join(option_lines),
                                title=f"[bold yellow]{header}[/bold yellow]",
                                border_style="yellow",
                            )
                        )
                answer = await asyncio.to_thread(Prompt.ask, question_text)
                answers[question_text] = answer
            responses.append({"interrupt_id": interrupt_id, "answers": answers})
            continue

        prompt_text = str(interrupt.get("message") or "请提供您的意见")
        answer = await asyncio.to_thread(Prompt.ask, prompt_text)
        responses.append({"interrupt_id": interrupt_id, "answer": answer})

    if not responses:
        raise ValueError("HITL response does not contain a supported interaction")
    return {"responses": responses}


def _cli_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped if stripped else None


def _split_stream_item(item: object) -> tuple[str, dict]:
    if not isinstance(item, tuple):
        return "custom", {"type": "output_msg", "content": str(item)}
    if len(item) >= 3:
        _, mode, data = item[:3]
    elif len(item) >= 2:
        mode, data = item[:2]
    else:
        return "custom", {"type": "output_msg", "content": str(item)}
    if not isinstance(data, dict):
        data = {"type": "output_msg", "content": str(data)}
    return str(mode), data


async def _stream_agent_response(
    agent: DataAgent,
    *,
    user_query: str,
    initial_state: dict,
    renderer: StreamRenderer,
    checkpoint_id: str | None = None,
    human_feedback: dict | None = None,
) -> dict:
    response: dict = {}
    stream_initial_state = dict(initial_state)
    stream_initial_state["user_query"] = user_query
    renderer.start()
    try:
        async for item in agent.astream(
            initial_state=stream_initial_state,
            checkpoint_id=checkpoint_id,
            human_feedback=human_feedback,
        ):
            mode, data = _split_stream_item(item)
            if mode == "updates":
                response = data
                continue
            if mode == "custom":
                renderer.handle_event(data)
    finally:
        renderer.stop()
    return response


async def _run_terminal_chat_loop(
    agent: DataAgent,
    get_user_input: Callable[[], Awaitable[str]],
    *,
    enable_portrait: bool = False,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    uid = _cli_optional_str(user_id)
    fixed_session = _cli_optional_str(session_id)
    if fixed_session is not None:
        session_id_resolved = fixed_session
    else:
        session_id_resolved = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d_%H%M%S_") + str(uuid.uuid4())

    run_id = 0
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
            renderer = StreamRenderer(console)
            response = await _stream_agent_response(
                agent,
                user_query=user_input,
                initial_state=initial_state,
                renderer=renderer,
            )
            while isinstance(response, dict) and response.get("interrupted"):
                feedback = await _prompt_for_human_feedback(response)
                renderer = StreamRenderer(console)
                response = await _stream_agent_response(
                    agent,
                    user_query="",
                    renderer=renderer,
                    checkpoint_id=session_id_resolved,
                    initial_state=initial_state,
                    human_feedback=feedback,
                )
            if isinstance(response, dict) and response.get("error"):
                renderer.render_error(str(response.get("final_answer") or response.get("error")))
            elif isinstance(response, dict) and response.get("messages"):
                renderer.render_final_result(response)
            run_id += 1

        except KeyboardInterrupt:
            logger.error("\n\n程序被用户中断")
            break
        except Exception as e:
            logger.exception(f"处理请求时出错: {e}")
            console.print(Panel(str(e), title="[bold red]❌ 请求异常[/bold red]", border_style="red", padding=(1, 2)))
            continue


async def run_terminal_mode(
    config_path: str | Path,
    *,
    enable_portrait: bool = False,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
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
    value = api_key.strip()
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


async def run_quickstart() -> None:
    try:
        quickstart_config = dataagent_package_path("examples", "quickstart.yaml")
        config_file = resolve_config_path(str(quickstart_config))
        quickstart_config = dataagent_package_path("examples", "default_configs.yaml")
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

        agent = DataAgent(config=quickstart_cm)
        agent._build_deep_agent()
        logger.trace(f"[green]Agent '{agent.name()}' 加载成功！[/green]")

        log_terminal_welcome("DataAgent Quickstart 交互模式", support_multiline=True)
        await _run_terminal_chat_loop(agent, _read_multiline_input)

    except Exception as e:
        logger.error(f"Quickstart 启动失败: {e}")
        raise


def print_help():
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
    try:
        from dataagent.a2a_server import DataAgentExecutor, build_agent_card, run_a2a_server
    except ImportError as e:
        logger.error(f"A2A server mode requires a2a-sdk>=1.0.0: {e}")
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

        agent = load_agent_from_config_path(config_path)
        agent_card = build_agent_card(agent, host=host, port=port, jsonrpc_path=jsonrpc_path, rest_path=rest_path)
        executor = DataAgentExecutor(agent)

        console.print(Rule("[bold cyan]DataAgent A2A 1.0 Server[/bold cyan]"))
        console.print(f"[green]Server URL:[/green] http://{host}:{port}")
        console.print(f"[green]AgentCard:[/green] http://{host}:{port}/.well-known/agent.json")
        console.print(f"[green]JSON-RPC:[/green] http://{host}:{port}{jsonrpc_path}")
        console.print(f"[green]REST:[/green] http://{host}:{port}{rest_path}")
        if auth_token:
            console.print("[green]Auth:[/green] Bearer token enabled")
        console.print("[dim]Press Ctrl+C to stop\n")

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
    parser = argparse.ArgumentParser(
        description="DataAgent - 数据分析Agent框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  # 终端交互模式
  python -m dataagent --config dataagent/examples/deep_analyze.yaml

  # 指定用户与会话 ID
  python -m dataagent --config path/to.yaml --user alice --session 20260101_my_session --portrait

  # A2A 1.0 协议服务模式
  python -m dataagent serve-a2a --config path/to/config.yaml
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    serve_a2a_parser = subparsers.add_parser("serve-a2a", help="启动A2A 1.0协议服务模式")
    serve_a2a_parser.add_argument("--config", "-c", required=True, help="配置文件路径")
    serve_a2a_parser.add_argument("--host", default="0.0.0.0", help="服务主机地址")
    serve_a2a_parser.add_argument("--port", "-p", type=int, default=9999, help="服务端口")
    serve_a2a_parser.add_argument("--jsonrpc-path", default="/a2a/jsonrpc", help="JSON-RPC路由路径")
    serve_a2a_parser.add_argument("--rest-path", default="/a2a/rest", help="REST路由路径")
    serve_a2a_parser.add_argument("--auth-token", default=None, help="Bearer 鉴权 Token")

    quickstart_parser = subparsers.add_parser("quickstart", help="交互式配置并启动 Agent")
    quickstart_parser.add_argument("--config", "-c", help="示例配置文件路径")

    config_parser = subparsers.add_parser("config", help="配置相关工具")
    config_subparsers = config_parser.add_subparsers(dest="config_command", help="配置命令")
    config_check_parser = config_subparsers.add_parser("check", help="诊断配置并检查 $env{VAR} 依赖")
    config_check_parser.add_argument("config_path", help="配置文件路径")
    config_check_parser.add_argument("--default", "-d", help="默认配置文件路径")

    parser.add_argument("--config", "-c", help="配置文件路径")
    parser.add_argument("--portrait", action="store_true", default=False, help="开启 LLM 用户画像")
    parser.add_argument("--user", "-u", default=None, metavar="USER_ID", help="用户 ID")
    parser.add_argument("--session", "-s", default=None, metavar="SESSION_ID", help="会话 ID")
    parser.add_argument("--version", "-v", action="version", version="DataAgent 0.1.0")

    args = parser.parse_args()

    if args.command == "serve-a2a":
        run_serve_a2a_mode(args.config, args.host, args.port, args.jsonrpc_path, args.rest_path, args.auth_token)
    elif args.command == "quickstart":
        asyncio.run(run_quickstart())
    elif args.command == "config" and args.config_command == "check":
        exit_code = run_config_check(args.config_path, args.default)
        sys.exit(exit_code)
    else:
        if not args.config:
            parser.print_help()
            sys.exit(1)
        asyncio.run(
            run_terminal_mode(args.config, enable_portrait=args.portrait, user_id=args.user, session_id=args.session)
        )


if __name__ == "__main__":
    main()
