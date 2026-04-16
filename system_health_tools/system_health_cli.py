#!/usr/bin/env python3
"""Terminal checklist view for system health diagnostics."""

from __future__ import annotations

import argparse
import ast
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
    details: dict[str, str]


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_COLORS = {
    "node": "\033[36m",      # cyan
    "topic": "\033[33m",     # yellow
    "service": "\033[32m",   # green
    "other": "\033[37m",     # light gray
    "OK": "\033[32m",        # green
    "WARN": "\033[33m",      # yellow
    "ERR": "\033[31m",       # red
    "STALE": "\033[35m",     # magenta
    "UNK": "\033[37m",       # light gray
}


def _paint(text: str, color_key: str, enabled: bool, bold: bool = False) -> str:
    if not enabled:
        return text
    color = ANSI_COLORS.get(color_key, "")
    if not color:
        return text
    style = ANSI_BOLD if bold else ""
    return f"{style}{color}{text}{ANSI_RESET}"


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

        details = {str(kv.key): str(kv.value) for kv in status.values}
        rows.append(HealthRow(category=cat, target=target, level=status.level, message=status.message, details=details))

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


def _parse_bool(text: str | None) -> bool | None:
    if text is None:
        return None
    val = str(text).strip().lower()
    if val in ("true", "1", "yes", "y"):
        return True
    if val in ("false", "0", "no", "n"):
        return False
    return None


def _parse_list(text: str | None) -> list[str]:
    if text is None:
        return []
    raw = str(text).strip()
    if not raw:
        return []
    try:
        value = ast.literal_eval(raw)
        if isinstance(value, (list, tuple, set)):
            return [str(x) for x in value if str(x).strip()]
        return [str(value)]
    except Exception:
        pass
    # Fallback for plain comma-separated text.
    trimmed = raw.strip("[]")
    return [x.strip().strip("'\"") for x in trimmed.split(",") if x.strip()]


def _health_score_text(summary: DiagnosticStatus | None) -> str | None:
    if summary is None:
        return None
    total = _summary_int(summary, "checks_total")
    ok = _summary_int(summary, "ok")
    if total is None or ok is None or total <= 0:
        return None
    pct = (ok * 100.0) / float(total)
    return f"health-score {ok}/{total} OK ({pct:.1f}%)"


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


def _print_root_cause_groups(rows: list[HealthRow], only_problems: bool, use_color: bool) -> None:
    relevant = [r for r in rows if r.level != DiagnosticStatus.OK]
    if not relevant:
        return

    roots: list[HealthRow] = []
    deps: list[tuple[HealthRow, list[str]]] = []
    for row in relevant:
        root_cause = _parse_bool(row.details.get("root_cause"))
        caused_by = _parse_list(row.details.get("caused_by"))
        if root_cause is False and caused_by:
            deps.append((row, caused_by))
        else:
            roots.append(row)

    if roots:
        print(_paint("root-causes:", "ERR", use_color, bold=True))
        for row in roots:
            print(f"  - {row.category} {row.target}: {row.message}")

    if deps:
        print(_paint("dependent-issues:", "WARN", use_color, bold=True))
        for row, caused_by in deps:
            print(
                f"  - {row.category} {row.target}: {row.message} "
                f"[caused_by: {', '.join(caused_by)}]"
            )

    if only_problems and not roots and not deps:
        print("No root-cause/dependent groups found.")


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


def _print_report(diag: DiagnosticArray, prefix: str, only_problems: bool, use_color: bool) -> None:
    summary, rows = _parse_statuses(diag, prefix)
    if summary is None and not rows:
        print(f"No '{prefix}/...' statuses found on /diagnostics yet.")
        return

    score_text = _health_score_text(summary)
    if score_text:
        print(_paint(score_text, "OK", use_color, bold=True))

    shown_rows: list[HealthRow] = []
    shown = 0
    for row in rows:
        if only_problems and row.level == DiagnosticStatus.OK:
            continue
        left_plain = f"{row.category} {row.target}"
        right_plain = _level_str(row.level)
        line = _dot_line(left_plain, right_plain)
        cat_colored = _paint(row.category, row.category, use_color, bold=True)
        right_colored = _paint(right_plain, right_plain, use_color, bold=True)
        line = line.replace(row.category, cat_colored, 1)
        line = line[: -len(right_plain)] + right_colored
        if row.level != DiagnosticStatus.OK:
            line += f"  ({row.message})"
        print(line)
        shown_rows.append(row)
        shown += 1

    if shown == 0:
        print("No problematic items.")

    _print_root_cause_groups(shown_rows, only_problems, use_color)
    _print_check_counts(rows, shown, only_problems)
    _print_runtime_counts(summary)
    if score_text:
        print(_paint(score_text, "OK", use_color, bold=True))
    if summary is not None:
        summary_level = _level_str(summary.level)
        print(f"summary {_paint(summary_level, summary_level, use_color, bold=True)}: {summary.message}")
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
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors in output")
    return parser.parse_args(cleaned[1:])


def main(args: list[str] | None = None) -> int:
    argv = list(sys.argv if args is None else args)
    cli = _parse_cli(argv)
    use_color = (not cli.no_color) and sys.stdout.isatty()

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
                    _print_report(node.latest, cli.prefix.strip("/"), cli.only_problems, use_color)
                    print("")
                    last_count = node.msg_count
                continue

            _print_report(node.latest, cli.prefix.strip("/"), cli.only_problems, use_color)
            return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
