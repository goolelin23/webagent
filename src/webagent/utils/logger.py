"""
统一日志模块
使用 rich 美化控制台输出，同时输出到日志文件
"""

import logging
import sys
from pathlib import Path
from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

# 自定义主题
THEME = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "agent.explorer": "bold magenta",
    "agent.planner": "bold blue",
    "agent.executor": "bold green",
    "pipeline": "bold cyan",
    "safety": "bold yellow",
    "skill": "bold white",
})

console = Console(theme=THEME)


def setup_logger(
    name: str = "webpilot",
    level: str = "INFO",
    log_file: str | Path | None = None,
) -> logging.Logger:
    """
    初始化并返回日志记录器

    Args:
        name: 日志记录器名称
        level: 日志级别
        log_file: 日志文件路径（可选）
    """
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Rich 控制台 Handler
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=True,
    )
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(rich_handler)

    # 文件 Handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)

    return logger


def get_logger(module_name: str = "webpilot") -> logging.Logger:
    """获取指定模块的日志记录器"""
    return logging.getLogger(module_name)


def print_banner():
    """打印系统启动横幅"""
    from webagent.utils.llm import get_provider_info
    provider_info = get_provider_info()

    banner = f"""
[bold cyan]╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   🤖  WebPilot AI (网页机长)                                 ║
║                                                              ║
║   基于视觉感知 + 网页自然语言自动化操作的强力 Agent             ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝[/bold cyan]
  [dim]模型: {provider_info}[/dim]
"""
    console.print(banner)


def print_step(step_num: int, total: int, description: str):
    """打印步骤进度"""
    console.print(
        f"  [bold cyan]▶ 步骤 [{step_num}/{total}][/bold cyan] {description}"
    )


def print_success(message: str):
    """打印成功消息"""
    console.print(f"  [success]✅ {message}[/success]")


def print_warning(message: str):
    """打印警告消息"""
    console.print(f"  [warning]⚠️  {message}[/warning]")


def print_error(message: str):
    """打印错误消息"""
    console.print(f"  [error]❌ {message}[/error]")


def print_agent(agent_type: str, message: str):
    """打印Agent消息"""
    style = f"agent.{agent_type}"
    icons = {
        "explorer": "🔍",
        "planner": "📋",
        "executor": "⚡",
    }
    icon = icons.get(agent_type, "🤖")
    console.print(f"  [{style}]{icon} [{agent_type.upper()}] {message}[/{style}]")
