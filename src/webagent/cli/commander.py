"""
CLI 命令行交互模块
提供交互模式、单次指令模式和扫描模式
"""

from __future__ import annotations
import argparse
import asyncio

from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.markdown import Markdown

from webagent.utils.config import get_config
from webagent.utils.logger import print_banner, console, setup_logger
from webagent.utils.llm import get_provider_info

logger = None


def create_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        prog="webpilot",
        description="🤖 WebPilot AI (网页机长) — 基于视觉感知 + 网页自然语言自动化操作的强力 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  webpilot                                            # 进入交互模式
  webpilot run "创建一个采购订单"                       # 直接执行指令
  webpilot run "创建采购订单" --url https://example.com  # 指定目标系统
  webpilot scan --url https://example.com              # 扫描系统并生成知识库
  webpilot scan --url https://example.com --depth 3    # 扫描3层深度
  webpilot kb list                                     # 查看所有知识库
  webpilot kb show example.com                         # 查看特定站点知识
  webpilot skills                                      # 查看可用技能插件
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

    # ── openclaw: OpenClaw 兼容 ──
    export_claw_parser = subparsers.add_parser("export-claw", help="导出知识库为 OpenClaw(龙虾) 技能包")
    export_claw_parser.add_argument("domain", type=str, help="站点域名")

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
        elif parsed.command == "export-claw":
            self._export_claw_command(parsed.domain)
        else:
            parser.print_help()

    def _interactive_mode(self):
        """交互模式"""
        print_banner()
        console.print("  [dim]输入自然语言指令，或使用以下命令:[/dim]")
        console.print("  [dim]  /scan <url>     — 扫描Web系统（自动深度分析）[/dim]")
        console.print("  [dim]  /scan-deep <url> — 👁️ 视觉驱动深度扫描（截图理解+自验证+自愈回退）[/dim]")
        console.print("  [dim]  /explore <url>   — 🌲 全量自主探索（穷举所有交互元素 + DFS 回溯）[/dim]")
        console.print("  [dim]  /resolve <域名>  — 人工接管解决扫描中的阻碍点[/dim]")
        console.print("  [dim]  /replay <域名>   — 回放已学习的操作路径[/dim]")
        console.print("  [dim]  /dream <域名>    — 💤 梦境模式 (知识库自我整理清理)[/dim]")
        console.print("  [dim]  /analyze <域名>  — 手动触发深度分析[/dim]")
        console.print("  [dim]  /pageskills <域名> — 查看页面技能[/dim]")
        console.print("  [dim]  /export-claw <域名> — 导出为OpenClaw(龙虾)技能包[/dim]")
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
                user_input = Prompt.ask("[bold cyan]🤖 WebPilot[/bold cyan]").strip()

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
                    elif cmd == "/scan-deep":
                        if not arg:
                            arg = Prompt.ask("  请输入深度扫描目标URL")
                        console.print(f"  [cyan]目标URL设为: {arg}[/cyan]")
                        asyncio.run(self._async_scan_deep(arg))
                    elif cmd == "/explore":
                        if not arg:
                            arg = Prompt.ask("  请输入要探索的目标页面URL")
                        asyncio.run(self._async_explore(arg))
                    elif cmd == "/resolve":
                        if not arg:
                            arg = Prompt.ask("  请输入受阻站点域名")
                        asyncio.run(self._async_resolve(arg))
                    elif cmd == "/replay":
                        if not arg:
                            arg = Prompt.ask("  请输入站点域名")
                        asyncio.run(self._async_replay(arg))
                    elif cmd == "/dream":
                        asyncio.run(self._async_dream(arg))
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
                    elif cmd == "/analyze":
                        if not arg:
                            arg = Prompt.ask("  请输入站点域名")
                        asyncio.run(self._async_analyze(arg))
                    elif cmd == "/pageskills":
                        if not arg:
                            arg = Prompt.ask("  请输入站点域名")
                        self._page_skills_command(arg)
                    elif cmd == "/export-claw":
                        if not arg:
                            arg = Prompt.ask("  请输入站点域名")
                        self._export_claw_command(arg)
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
            console.print("  使用: webpilot kb [list|show|delete|search]")

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
        """异步扫描（含自动深度分析）"""
        orchestrator = self._get_orchestrator()
        try:
            await orchestrator.scan(url, depth, max_pages)
        except Exception as e:
            console.print(f"  [bold red]扫描失败: {e}[/bold red]")

    async def _async_scan_deep(self, url: str):
        """异步执行视觉驱动深度扫描"""
        orchestrator = self._get_orchestrator()
        try:
            await orchestrator.scan_deep(url)
        except Exception as e:
            console.print(f"  [bold red]深度扫描失败: {e}[/bold red]")

    async def _async_explore(self, url: str):
        """🌲 区域感知全量自主探索 — 穷举所有交互元素，DFS 回溯遍历所有路径，形成系统知识图谱"""
        from playwright.async_api import async_playwright
        from webagent.agents.page_explorer import PageExplorer
        import time as _time

        config = get_config()
        console.print(f"\n  [bold cyan]🌲 区域感知全量自主探索启动[/bold cyan]")
        console.print(f"  [dim]目标: {url}[/dim]")
        console.print(f"  [dim]策略: 区域划分 → 区域内元素枚举 → BFS(区域) + DFS(跨页) → 持久化知识图谱[/dim]\n")

        max_depth_str    = Prompt.ask("  最大递归深度（建议2-4）", default="3")
        max_elems_str    = Prompt.ask("  每个区域最多探索元素数", default="8")
        max_nodes_str    = Prompt.ask("  全局最多节点数（页面状态上限）", default="60")
        output_dir       = Prompt.ask("  探索结果输出目录", default="exploration_output")
        resume_path      = Prompt.ask("  断点续探路径（留空则全新开始）", default="")

        try:
            max_depth  = int(max_depth_str)
            max_elems  = int(max_elems_str)
            max_nodes  = int(max_nodes_str)
        except ValueError:
            max_depth, max_elems, max_nodes = 3, 8, 60

        resume_graph_path = resume_path if resume_path else None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=config.browser.headless)
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()

                console.print(f"  [dim]正在导航到目标页面...[/dim]")
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)

                explorer = PageExplorer(output_dir=output_dir)
                await explorer.explore(
                    page=page,
                    max_depth=max_depth,
                    max_elements_per_region=max_elems,
                    max_total_nodes=max_nodes,
                    resume_graph_path=resume_graph_path,
                )

                console.print(f"\n  [green]✅ 探索完成！结果已保存到目录: {output_dir}/[/green]")
                console.print(f"  [dim]  - exploration_graph.json （知识图谱，可断点续探）[/dim]")
                console.print(f"  [dim]  - exploration_report.md  （可读探索报告）[/dim]")

                await browser.close()

        except Exception as e:
            console.print(f"  [bold red]全量探索失败: {e}[/bold red]")
            if logger:
                logger.exception("全量探索异常")


    async def _async_resolve(self, domain: str):
        """异步执行人工解决阻碍"""
        orchestrator = self._get_orchestrator()
        try:
            await orchestrator.resolve_blocked_paths(domain)
        except Exception as e:
            console.print(f"  [bold red]解决阻碍失败: {e}[/bold red]")

    async def _async_replay(self, domain: str):
        """异步回放已学习的操作路径"""
        orchestrator = self._get_orchestrator()
        try:
            site = orchestrator.knowledge_store.load(domain)
            if not site:
                console.print(f"  [yellow]未找到站点: {domain}[/yellow]")
                return

            actions = getattr(site, 'learned_actions', [])
            if not actions:
                console.print(f"  [yellow]站点 {domain} 没有已学习的操作记录[/yellow]")
                return

            from rich.table import Table
            table = Table(title=f"📚 已学习操作 — {domain} ({len(actions)} 条)")
            table.add_column("#", style="dim")
            table.add_column("操作", style="cyan")
            table.add_column("描述")
            table.add_column("坐标")
            table.add_column("置信度", style="green")
            table.add_column("URL")

            for i, a in enumerate(actions):
                coords = a.get("coordinates", {})
                coord_str = f"({coords.get('x',0)}, {coords.get('y',0)})"
                conf = f"{a.get('confidence', 0):.0%}"
                table.add_row(
                    str(i+1),
                    a.get("action_type", "?"),
                    a.get("description", ""),
                    coord_str,
                    conf,
                    a.get("page_url_pattern", ""),
                )
            console.print(table)
        except Exception as e:
            console.print(f"  [bold red]回放失败: {e}[/bold red]")

    async def _async_dream(self, domain: str = ""):
        """异步执行梦境知识整理"""
        orchestrator = self._get_orchestrator()
        from webagent.agents.dreamer import Dreamer
        dreamer = Dreamer(orchestrator.knowledge_store)
        
        try:
            if domain:
                console.print(f"\n  [magenta]💤 正在进入梦境整理 [{domain}] 的知识库...[/magenta]")
                await dreamer.dream(domain)
            else:
                console.print(f"\n  [magenta]💤 正在进入深层梦境，整理全部站点的知识库...[/magenta]")
                await dreamer.dream_all()
        except Exception as e:
            console.print(f"  [bold red]梦境整理失败: {e}[/bold red]")

    async def _async_analyze(self, domain: str):
        """手动触发深度分析"""
        orchestrator = self._get_orchestrator()
        try:
            console.print(f"  [cyan]正在对 [{domain}] 进行深度分析...[/cyan]")
            summary = await orchestrator.analyze(domain)
            console.print(Panel(summary, title=f"🧠 深度分析结果 — {domain}", border_style="magenta"))
        except Exception as e:
            console.print(f"  [bold red]深度分析失败: {e}[/bold red]")

    def _page_skills_command(self, domain: str):
        """显示页面技能列表"""
        orchestrator = self._get_orchestrator()
        summary = orchestrator.get_page_skills_summary(domain)
        console.print(Panel(summary, title=f"📦 页面技能 — {domain}", border_style="cyan"))

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
        table.add_column("说明")

        provider_info = {
            "openai":    ("gpt-4o",          "OpenAI GPT 系列"),
            "anthropic": ("claude-sonnet-4-20250514", "Anthropic Claude 系列"),
            "gemini":    ("gemini-2.0-flash", "Google Gemini 系列"),
            "qwen":      ("qwen-max",        "阿里云千问/通义 (DashScope)"),
        }

        for name, (default_model, desc) in provider_info.items():
            table.add_row(
                name,
                default_model,
                desc,
            )

        console.print(table)
        console.print()
        console.print("  [dim]切换方法: 修改 .env 中的 LLM_PROVIDER 和对应 API Key[/dim]")

    def _export_claw_command(self, domain: str):
        """导出为 OpenClaw 技能包"""
        try:
            from webagent.openclaw import export_for_openclaw
            export_dir = export_for_openclaw(domain)
            console.print(Panel(
                f"🦞 OpenClaw 技能包已导出到:\n\n"
                f"  {export_dir.resolve()}\n\n"
                f"使用方法:\n"
                f"  1. 复制到 OpenClaw 的 skills 目录:\n"
                f"     cp -r {export_dir.resolve()} ~/.openclaw/skills/\n\n"
                f"  2. 重启 OpenClaw 即可使用\n\n"
                f"  3. 或手动加载:\n"
                f"     在 OpenClaw 设置中添加 skills 路径指向导出目录",
                title="🦞 OpenClaw 导出完成",
                border_style="red",
            ))
        except Exception as e:
            console.print(f"  [bold red]导出失败: {e}[/bold red]")

    def _print_interactive_help(self):
        """打印交互模式帮助"""
        help_text = """
## 交互模式命令

| 命令 | 说明 |
|------|------|
| `<指令>` | 直接输入自然语言指令执行自动化任务 |
| `/scan <url>` | 扫描Web系统（自动深度分析+技能生成） |
| `/scan-deep <url>` | 🤖 主动交互扫描（AI造数填表，打通深层页面链路） |
| `/resolve <域名>` | 🛠️ 人工接管：解决扫描期间遇到的阻塞（如验证码） |
| `/analyze <域名>` | 对已扫描站点手动触发深度分析 |
| `/pageskills <域名>` | 查看自动生成的页面技能 |
| `/export-claw <域名>` | 🦞 导出为OpenClaw(龙虾)技能包 |
| `/plan <指令>` | 仅生成执行计划（不执行） |
| `/kb list` | 查看所有知识库 |
| `/kb show <domain>` | 查看特定站点知识 |
| `/skills` | 查看所有可用技能插件 |
| `/model` | 查看当前模型配置和支持的提供商 |
| `/domain <name>` | 设置业务领域 (supply_chain/hr/ecommerce) |
| `/url <url>` | 设置目标系统URL |
| `/help` | 显示此帮助 |
| `/quit` | 退出 |
"""
        console.print(Markdown(help_text))
