"""
审计日志
记录所有Agent操作的完整轨迹
"""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any

from webagent.utils.logger import get_logger

logger = get_logger("webpilot.safety.audit")


@dataclass
class AuditEntry:
    """审计日志条目"""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    event_type: str = ""            # action, safety_check, approval, error, scan
    agent: str = ""                 # explorer, planner, executor
    action: str = ""                # 具体操作
    target: str = ""                # 操作目标
    value: str = ""                 # 操作值
    result: str = ""                # success, failed, blocked
    risk_level: str = ""            # LOW, MEDIUM, HIGH, CRITICAL
    error_message: str = ""
    page_url: str = ""
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class AuditLogger:
    """审计日志记录器"""

    def __init__(self, log_dir: str | Path = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._entries: list[AuditEntry] = []
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    @property
    def log_file(self) -> Path:
        return self.log_dir / f"audit_{self._session_id}.json"

    def log(self, entry: AuditEntry):
        """记录审计条目"""
        self._entries.append(entry)
        logger.debug(
            f"[审计] {entry.event_type} | {entry.agent} | "
            f"{entry.action} → {entry.target} | {entry.result}"
        )

    def log_action(
        self,
        agent: str,
        action: str,
        target: str,
        result: str,
        value: str = "",
        page_url: str = "",
        duration_ms: int = 0,
        **kwargs,
    ):
        """快捷方式：记录操作"""
        self.log(AuditEntry(
            event_type="action",
            agent=agent,
            action=action,
            target=target,
            value=value,
            result=result,
            page_url=page_url,
            duration_ms=duration_ms,
            metadata=kwargs,
        ))

    def log_safety_event(
        self,
        action: str,
        risk_level: str,
        result: str,
        reason: str = "",
    ):
        """快捷方式：记录安全事件"""
        self.log(AuditEntry(
            event_type="safety_check",
            action=action,
            risk_level=risk_level,
            result=result,
            error_message=reason,
        ))

    def log_error(
        self,
        agent: str,
        action: str,
        error_message: str,
        page_url: str = "",
    ):
        """快捷方式：记录错误"""
        self.log(AuditEntry(
            event_type="error",
            agent=agent,
            action=action,
            result="failed",
            error_message=error_message,
            page_url=page_url,
        ))

    def save(self):
        """保存审计日志到文件"""
        if not self._entries:
            return

        data = {
            "session_id": self._session_id,
            "total_entries": len(self._entries),
            "start_time": self._entries[0].timestamp if self._entries else "",
            "end_time": self._entries[-1].timestamp if self._entries else "",
            "entries": [asdict(e) for e in self._entries],
        }

        with open(self.log_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"审计日志已保存: {self.log_file} ({len(self._entries)} 条记录)")

    def get_summary(self) -> dict:
        """获取审计摘要"""
        total = len(self._entries)
        success = sum(1 for e in self._entries if e.result == "success")
        failed = sum(1 for e in self._entries if e.result == "failed")
        blocked = sum(1 for e in self._entries if e.result == "blocked")

        return {
            "total_actions": total,
            "success": success,
            "failed": failed,
            "blocked": blocked,
            "success_rate": f"{success / total * 100:.1f}%" if total > 0 else "N/A",
            "session_id": self._session_id,
        }

    def clear(self):
        """清空内存中的审计条目"""
        self._entries.clear()
