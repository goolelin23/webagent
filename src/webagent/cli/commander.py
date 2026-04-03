"""
CLI 命令行交互模块
提供交互模式、单次指令模式和扫描模式
"""

from __future__ import annotations
import argparse
import asyncio
import sys
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.markdown import Markdown

from webagent.utils.logger import print_banner, console, setup_logger
from webagent.utils.config import get_config, get_provider_info, PROVIDER_DEFAULTS

logger = None


def create_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="webagent",
        description="🤖 智能Web自动化Agent系统 — 基于 Browser Use + AI + Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  webagent                                            # 进入交互模式
  webagent run "创建一个采购订单"                       # 直接执行指令
  webagent run "创建采购订单" --url https://example.com  # 指定目标系统
  webagent scan --url https://example.com              # 扫描系统并生成知识库
  webagent scan --url https://example.com --depth 3    # 扫描3层深度
  webagent kb list                                     # 查看所有知识库
  webagent kb show example.com                         # 查看特定站点知识
  webagent skills                                      # 查看可用技能插件
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # ── run: 执行自然语言指令 ──
    run_parser = subparsers.add_parser("run", help="执行自然语言指令")
    run_parser.add_argument("instruction", type=str, help="自然语言指令")
    run_parser.add_argument("--url", type=str, default="", help="目标系统URL")
    run_parser.add_argument("--domain", type=str, default="", help="业务领域 (supply_chain/hr/ecommerce)")
    run_parser.add_argument("--auto-scan", action="store_true", help="执行前先扫描目标系统")
    run_parser.add_argument("--plan-only", action="store_true", help="仅生成执行计划，不实际执行")

    # ── scan: 扫描系统 ──
    scan_parser = subparsers.add_parser("scan", help="扫描Web系统并生成知识库")
    scan_parser.add_argument("--url", type=str, required=True, help="目标系统URL")
    scan_parser.add_argument("--depth", type=int, default=2, help="扫描深度 (默认2)")
    scan_parser.add_argument("--max-pages", type=int, default=50, help="最大页面数 (默认50)")

    # ── kb: 知识库管理 ──
    kb_parser = subparsers.add_parser("kb", help="知识库管理")
    kb_sub = kb_parser.add_subparsers(dest="kb_command")

    kb_sub.add_parser("list", help="列出所有知识库")

    kb_show = kb_sub.add_parser("show", help="查看特定站点知识")
    kb_show.add_argument("domain", type=str, help="站点域名")

    kb_delete = kb_sub.add_parser("delete", help="删除知识库")
    kb_delete.add_argument("domain", type=str, help="站点域名")

    kb_search = kb_sub.add_parser("search", help="搜索知识库")
    kb_search.add_argument("domain", type=str, help="站点域名")
    kb_search.add_argument("keyword", type=str, help="搜索关键词")

    # ── skills: 技能管理 ──
    subparsers.add_parser("skills", help="查看可用技能插件")

    return parser


class Commander:
    """CLI 命令行交互控制器"""

    def __init__(self):
        self._orchestrator = None

    def _get_orchestrator(self):
        """延迟初始化调度器"""
        if self._orchestrator is None:
            from webagent.agents.orchestrator import AgentOrchestrator
            self._orchestrator = AgentOrchestrator()
        return self._orchestrator

    def run(self, args: list[str] | None = None):
        """主入口"""
        parser = create_parser()
        parsed = parser.parse_args(args)

        # 初始化日志
        config = get_config()
        global logger
        logger = setup_logger(
            level=config.log_level,
            log_file=config.log_file_path,
        )

        if parsed.command is None:
            # 无子命令 → 进入交互模式
            self._interactive_mode()
        elif parsed.command == "run":
            self._run_command(parsed)
        elif parsed.command == "scan":
            self._scan_command(parsed)
        elif parsed.command == "kb":
            self._kb_command(parsed)
        elif parsed.command == "skills":
            self._skills_command()
        else:
            parser.print_help()

    def _interactive_mode(self):
        """交互模式"""
        print_banner()
        console.print("  [dim]输入自然语言指令，或使用以下命令:[/dim]")
        console.print("  [dim]  /scan <url>     — 扫描Web系统[/dim]")
        console.print("  [dim]  /kb list        — 查看知识库[/dim]")
        console.print("  [dim]  /skills         — 查看可用技能[/dim]")
        console.print("  [dim]  /model          — 查看当前模型配置[/dim]")
        console.print("  [dim]  /domain <name>  — 设置业务领域[/dim]")
        console.print("  [dim]  /url <url>      — 设置目标URL[/dim]")
        console.print("  [dim]  /help           — 显示帮助[/dim]")
        console.print("  [dim]  /quit           — 退出[/dim]")
        console.print()

        target_url = ""

        while True:
            try:
                user_input = Prompt.ask("[bold cyan]🤖 WebAgent[/bold cyan]").strip()

                if not user_input:
                    continue

                if user_input.startswith("/"):
                    # 处理内置命令
                    parts = user_input.split(maxsplit=1)
                    cmd = parts[0].lower()
                    arg = parts[1] if len(parts) > 1 else ""

                    if cmd in ("/quit", "/exit", "/q"):
                        console.print("  [dim]再见! 👋[/dim]")
                        break
                    elif cmd == "/help":
                        self._print_interactive_help()
                    elif cmd == "/scan":
                        if not arg:
                            arg = Prompt.ask("  请输入目标URL")
                        asyncio.run(self._async_scan(arg))
                    elif cmd == "/kb":
                        self._handle_kb_interactive(arg)
                    elif cmd == "/skills":
                        self._skills_command()
                    elif cmd == "/domain":
                        if arg:
                            orchestrator = self._get_orchestrator()
                            orchestrator.set_business_domain(arg)
                        else:
                            console.print("  可用领域: supply_chain, hr, ecommerce")
                    elif cmd == "/url":
                        target_url = arg
                        console.print(f"  [cyan]目标URL已设置: {target_url}[/cyan]")
                    elif cmd == "/model":
                        self._model_command(arg)
                    elif cmd == "/plan":
                        if not arg:
                            arg = Prompt.ask("  请输入任务指令")
                        asyncio.run(self._async_plan_only(arg, target_url))
                    else:
                        console.print(f"  [yellow]未知命令: {cmd}[/yellow]")
                else:
                    # 作为自然语言指令执行
                    asyncio.run(self._async_run_task(user_input, target_url))

            except KeyboardInterrupt:
                console.print("\n  [dim]使用 /quit 退出[/dim]")
            except EOFError:
                break

    def _run_command(self, args):
        """处理 run 子命令"""
        print_banner()

        orchestrator = self._get_orchestrator()

        if args.domain:
            orchestrator.set_business_domain(args.domain)

        if args.plan_only:
            asyncio.run(self._async_plan_only(args.instruction, args.url))
        else:
            asyncio.run(self._async_run_task(
                args.instruction,
                args.url,
                auto_scan=args.auto_scan,
            ))

    def _scan_command(self, args):
        """处理 scan 子命令"""
        print_banner()
        asyncio.run(self._async_scan(args.url, args.depth, args.max_pages))

    def _kb_command(self, args):
        """处理 kb 子命令"""
        orchestrator = self._get_orchestrator()

        if args.kb_command == "list":
            sites = orchestrator.list_knowledge_bases()
            if not sites:
                console.print("  [dim]暂无知识库数据。使用 scan 命令扫描Web系统。[/dim]")
                return
            table = Table(title="📚 知识库列表")
            table.add_column("域名", style="cyan")
            for site in sites:
                table.add_row(site)
            console.print(table)

        elif args.kb_command == "show":
            summary = orchestrator.get_knowledge_summary(args.domain)
            if summary:
                console.print(Panel(summary, title=f"📋 {args.domain}"))
            else:
                console.print(f"  [yellow]未找到站点: {args.domain}[/yellow]")

        elif args.kb_command == "delete":
            if Confirm.ask(f"  确认删除知识库 [{args.domain}]?"):
                store = orchestrator.knowledge_store
                if store.delete(args.domain):
                    console.print(f"  [green]已删除: {args.domain}[/green]")
                else:
                    console.print(f"  [yellow]未找到: {args.domain}[/yellow]")

        elif args.kb_command == "search":
            store = orchestrator.knowledge_store
            pages = store.search_pages(args.domain, args.keyword)
            if pages:
                table = Table(title=f"🔍 搜索结果: {args.keyword}")
                table.add_column("URL", style="cyan")
                table.add_column("标题")
                table.add_column("类型")
                for page in pages:
                    table.add_row(page.url, page.title, page.page_type)
                console.print(table)
            else:
                console.print(f"  [dim]未找到相关页面[/dim]")

        else:
            console.print("  使用: webagent kb [list|show|delete|search]")

    def _skills_command(self):
        """显示可用技能列表"""
        orchestrator = self._get_orchestrator()
        skills = orchestrator.list_skills()

        if not skills:
            console.print("  [dim]暂无可用技能插件[/dim]")
            return

        table = Table(title="🔧 可用技能插件")
        table.add_column("名称", style="cyan")
        table.add_column("描述")
        table.add_column("版本", style="dim")

        for skill in skills:
            table.add_row(skill["name"], skill["description"], skill["version"])

        console.print(table)

    # ── 异步操作方法 ──

    async def _async_run_task(
        self,
        instruction: str,
        target_url: str = "",
        auto_scan: bool = False,
    ):
        """异步执行任务"""
        orchestrator = self._get_orchestrator()
        try:
            report = await orchestrator.run_task(
                instruction=instruction,
                target_url=target_url,
                auto_scan=auto_scan,
            )
        except Exception as e:
            console.print(f"  [bold red]任务执行异常: {e}[/bold red]")

    async def _async_scan(
        self,
        url: str,
        depth: int = 2,
        max_pages: int = 50,
    ):
        """异步扫描"""
        orchestrator = self._get_orchestrator()
        try:
            await orchestrator.scan(url, depth, max_pages)
        except Exception as e:
            console.print(f"  [bold red]扫描失败: {e}[/bold red]")

    async def _async_plan_only(self, instruction: str, target_url: str = ""):
        """仅生成执行计划"""
        orchestrator = self._get_orchestrator()
        try:
            plan = await orchestrator.plan_only(instruction, target_url)
            console.print(Panel(
                plan.to_json(indent=2),
                title="📋 执行计划",
                border_style="cyan",
            ))
        except Exception as e:
            console.print(f"  [bold red]规划失败: {e}[/bold red]")

    # ── 辅助方法 ──

    def _handle_kb_interactive(self, arg: str):
        """交互模式下处理知识库命令"""
        orchestrator = self._get_orchestrator()
        parts = arg.split(maxsplit=1)
        subcmd = parts[0] if parts else "list"

        if subcmd == "list":
            sites = orchestrator.list_knowledge_bases()
            if not sites:
                console.print("  [dim]暂无知识库数据[/dim]")
            else:
                for site in sites:
                    console.print(f"  • {site}")
        elif subcmd == "show" and len(parts) > 1:
            summary = orchestrator.get_knowledge_summary(parts[1])
            if summary:
                console.print(summary)
            else:
                console.print(f"  [yellow]未找到: {parts[1]}[/yellow]")
        else:
            console.print("  用法: /kb [list|show <domain>]")

    def _model_command(self, arg: str = ""):
        """查看或切换模型配置"""
        info = get_provider_info()
        console.print(f"  [cyan]当前模型: {info}[/cyan]")
        console.print()

        table = Table(title="📡 支持的 LLM 提供商")
        table.add_column("提供商", style="cyan")
        table.add_column("默认模型")
        table.add_column(".env Key")
        table.add_column("说明")

        provider_desc = {
            "openai": "OpenAI GPT 系列",
            "anthropic": "Anthropic Claude 系列",
            "gemini": "Google Gemini 系列",
            "qwen": "阿里云千问/通义 (DashScope)",
        }

        for name, (default_model, key_env, _) in PROVIDER_DEFAULTS.items():
            table.add_row(
                name,
                default_model,
                key_env,
                provider_desc.get(name, ""),
            )

        console.print(table)
        console.print()
        console.print("  [dim]切换方法: 修改 .env 中的 LLM_PROVIDER 和对应 API Key[/dim]")

    def _print_interactive_help(self):
        """打印交互模式帮助"""
        help_text = """
## 交互模式命令

| 命令 | 说明 |
|------|------|
| `<指令>` | 直接输入自然语言指令执行自动化任务 |
| `/scan <url>` | 扫描Web系统并生成知识库 |
| `/plan <指令>` | 仅生成执行计划（不执行） |
| `/kb list` | 查看所有知识库 |
| `/kb show <domain>` | 查看特定站点知识 |
| `/skills` | 查看可用技能插件 |
| `/model` | 查看当前模型配置和支持的提供商 |
| `/domain <name>` | 设置业务领域 (supply_chain/hr/ecommerce) |
| `/url <url>` | 设置目标系统URL |
| `/help` | 显示此帮助 |
| `/quit` | 退出 |
"""
        console.print(Markdown(help_text))
