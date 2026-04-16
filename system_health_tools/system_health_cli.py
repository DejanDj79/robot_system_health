#!/usr/bin/env python3
"""Terminal checklist view for system health diagnostics."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import rclpy
from rclpy.node import Node
from rclpy.utilities import remove_ros_args

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus


@dataclass
class HealthRow:
    category: str
    target: str
    level: int
    message: str


def _level_str(level: int) -> str:
    if level == DiagnosticStatus.OK:
        return "OK"
    if level == DiagnosticStatus.WARN:
        return "WARN"
    if level == DiagnosticStatus.ERROR:
        return "ERR"
    if level == DiagnosticStatus.STALE:
        return "STALE"
    return "UNK"


def _dot_line(left: str, right: str, width: int = 68) -> str:
    dots = "." * max(3, width - len(left) - len(right))
    return f"{left}{dots}{right}"


def _parse_statuses(diag: DiagnosticArray, prefix: str) -> tuple[DiagnosticStatus | None, list[HealthRow]]:
    summary: DiagnosticStatus | None = None
    rows: list[HealthRow] = []
    marker = f"{prefix}/"
    for status in diag.status:
        name = (status.name or "").strip("/")
        if not name.startswith(marker):
            continue
        rel = name[len(marker) :]
        if rel == "summary":
            summary = status
            continue

        cat = "other"
        target = "/" + rel
        for group in ("nodes", "topics", "services"):
            if rel == group or rel.startswith(group + "/"):
                cat = group[:-1]  # nodes -> node
                tail = rel[len(group) :].lstrip("/")
                target = "/" + tail if tail else "/"
                break

        rows.append(HealthRow(category=cat, target=target, level=status.level, message=status.message))

    order = {"node": 0, "topic": 1, "service": 2, "other": 3}
    rows.sort(key=lambda r: (order.get(r.category, 99), r.target))
    return summary, rows


def _summary_int(summary: DiagnosticStatus | None, key: str) -> int | None:
    if summary is None:
        return None
    for kv in summary.values:
        if kv.key == key:
            try:
                return int(str(kv.value))
            except Exception:
                return None
    return None


def _print_runtime_counts(summary: DiagnosticStatus | None) -> None:
    n_user = _summary_int(summary, "runtime_nodes_user")
    n_sys = _summary_int(summary, "runtime_nodes_system")
    n_total = _summary_int(summary, "runtime_nodes_total")
    t_user = _summary_int(summary, "runtime_topics_user")
    t_sys = _summary_int(summary, "runtime_topics_system")
    t_total = _summary_int(summary, "runtime_topics_total")
    s_user = _summary_int(summary, "runtime_services_user")
    s_sys = _summary_int(summary, "runtime_services_system")
    s_total = _summary_int(summary, "runtime_services_total")

    if None in (n_user, n_sys, n_total, t_user, t_sys, t_total, s_user, s_sys, s_total):
        return

    print(
        "counts "
        f"nodes:user={n_user},system={n_sys},total={n_total}  "
        f"topics:user={t_user},system={t_sys},total={t_total}  "
        f"services:user={s_user},system={s_sys},total={s_total}"
    )


def _print_check_counts(rows: list[HealthRow], shown: int, only_problems: bool) -> None:
    node_count = sum(1 for r in rows if r.category == "node")
    topic_count = sum(1 for r in rows if r.category == "topic")
    service_count = sum(1 for r in rows if r.category == "service")
    total = len(rows)
    print(f"checks nodes={node_count} topics={topic_count} services={service_count} total={total}")
    if only_problems:
        print(f"shown problems={shown}")


class SystemHealthCli(Node):
    def __init__(self, diagnostics_topic: str):
        super().__init__("system_health_cli")
        self.latest: DiagnosticArray | None = None
        self.msg_count = 0
        self.sub = self.create_subscription(
            DiagnosticArray,
            diagnostics_topic,
            self._on_diagnostics,
            10,
        )

    def _on_diagnostics(self, msg: DiagnosticArray) -> None:
        self.latest = msg
        self.msg_count += 1


def _print_report(diag: DiagnosticArray, prefix: str, only_problems: bool) -> None:
    summary, rows = _parse_statuses(diag, prefix)
    if summary is None and not rows:
        print(f"No '{prefix}/...' statuses found on /diagnostics yet.")
        return

    shown = 0
    for row in rows:
        if only_problems and row.level == DiagnosticStatus.OK:
            continue
        left = f"{row.category} {row.target}"
        right = _level_str(row.level)
        line = _dot_line(left, right)
        if row.level != DiagnosticStatus.OK:
            line += f"  ({row.message})"
        print(line)
        shown += 1

    if shown == 0:
        print("No problematic items.")

    _print_check_counts(rows, shown, only_problems)
    _print_runtime_counts(summary)
    if summary is not None:
        print(f"summary {_level_str(summary.level)}: {summary.message}")
    else:
        print("summary: (not found)")


def _parse_cli(argv: list[str]) -> argparse.Namespace:
    cleaned = remove_ros_args(argv)
    parser = argparse.ArgumentParser(description="Print system health checklist from /diagnostics.")
    parser.add_argument("--diagnostics-topic", default="/diagnostics")
    parser.add_argument("--prefix", default="system_health", help="Diagnostic name prefix, default: system_health")
    parser.add_argument("--timeout", type=float, default=8.0, help="Wait timeout in seconds (default: 8)")
    parser.add_argument("--watch", action="store_true", help="Keep printing on every new diagnostics update")
    parser.add_argument("--only-problems", action="store_true", help="Show only WARN/ERR/STALE items")
    parser.add_argument("--clear", action="store_true", help="Clear terminal before each watch refresh")
    return parser.parse_args(cleaned[1:])


def main(args: list[str] | None = None) -> int:
    argv = list(sys.argv if args is None else args)
    cli = _parse_cli(argv)

    rclpy.init(args=argv)
    node = SystemHealthCli(cli.diagnostics_topic)

    deadline = time.monotonic() + max(0.1, cli.timeout)
    last_count = -1
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.2)

            if node.latest is None:
                if not cli.watch and time.monotonic() > deadline:
                    print(f"Timeout: no messages received on {cli.diagnostics_topic}.")
                    return 2
                continue

            summary, rows = _parse_statuses(node.latest, cli.prefix.strip("/"))
            if summary is None and not rows:
                if not cli.watch and time.monotonic() > deadline:
                    print(
                        f"Timeout: received diagnostics, but no '{cli.prefix.strip('/')}/...' entries found."
                    )
                    return 2
                continue

            if cli.watch:
                if node.msg_count != last_count:
                    if cli.clear:
                        print("\033[2J\033[H", end="")
                    _print_report(node.latest, cli.prefix.strip("/"), cli.only_problems)
                    print("")
                    last_count = node.msg_count
                continue

            _print_report(node.latest, cli.prefix.strip("/"), cli.only_problems)
            return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
