"""
智能Web自动化Agent系统 — 程序入口
"""

import sys
from webagent.cli.commander import Commander


def cli_entry():
    """CLI 入口点（由 pyproject.toml [project.scripts] 调用）"""
    commander = Commander()
    commander.run()


def main():
    """直接运行入口"""
    cli_entry()


if __name__ == "__main__":
    main()
