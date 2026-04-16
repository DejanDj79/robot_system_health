#!/usr/bin/env python3
"""RViz-friendly view for system health diagnostics.

Subscribes:
- /diagnostics (diagnostic_msgs/DiagnosticArray)

Publishes:
- /system_health/markers (visualization_msgs/MarkerArray)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import rclpy
from rclpy.node import Node

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from visualization_msgs.msg import Marker, MarkerArray


@dataclass
class ColorRGBA:
    r: float
    g: float
    b: float
    a: float = 1.0


def _status_color(level: int) -> ColorRGBA:
    if level == DiagnosticStatus.OK:
        return ColorRGBA(0.2, 0.85, 0.2, 1.0)
    if level == DiagnosticStatus.WARN:
        return ColorRGBA(1.0, 0.78, 0.1, 1.0)
    if level == DiagnosticStatus.ERROR:
        return ColorRGBA(0.92, 0.2, 0.2, 1.0)
    return ColorRGBA(0.8, 0.4, 0.1, 1.0)


def _status_label(level: int) -> str:
    if level == DiagnosticStatus.OK:
        return "OK"
    if level == DiagnosticStatus.WARN:
        return "WARN"
    if level == DiagnosticStatus.ERROR:
        return "ERROR"
    if level == DiagnosticStatus.STALE:
        return "STALE"
    return "UNKNOWN"


def _status_short(level: int) -> str:
    if level == DiagnosticStatus.OK:
        return "OK"
    if level == DiagnosticStatus.WARN:
        return "WARN"
    if level == DiagnosticStatus.ERROR:
        return "ERR"
    if level == DiagnosticStatus.STALE:
        return "STALE"
    return "UNK"


def _extract_category_and_target(full_name: str) -> tuple[str, str]:
    # Example names from monitor:
    # system_health/nodes/waypoint_follower
    # system_health/topics/load_waypoints
    # system_health/services/agar_waypoint_service
    name = (full_name or "").strip("/")
    parts = [p for p in name.split("/") if p]
    for cat in ("nodes", "topics", "services"):
        if cat in parts:
            idx = parts.index(cat)
            tail = parts[idx + 1 :]
            target = "/" + "/".join(tail) if tail else "/"
            return cat, target
    if parts:
        return "other", "/" + parts[-1]
    return "other", "/unknown"


class SystemHealthRviz(Node):
    def __init__(self) -> None:
        super().__init__("system_health_rviz")

        self.declare_parameter("diagnostics_topic", "/diagnostics")
        self.declare_parameter("marker_topic", "/system_health/markers")
        self.declare_parameter("frame_id", "map")
        self.declare_parameter("publish_rate_hz", 2.0)
        self.declare_parameter("max_items", 20)
        self.declare_parameter("anchor_x", 0.0)
        self.declare_parameter("anchor_y", 0.0)
        self.declare_parameter("anchor_z", 2.0)
        self.declare_parameter("line_spacing", 0.4)
        self.declare_parameter("text_height", 0.24)
        self.declare_parameter("show_ok_items", True)
        self.declare_parameter("show_messages", False)
        self.declare_parameter("use_colors", False)
        self.declare_parameter("health_status_prefix", "system_health")

        self.diagnostics_topic = str(self.get_parameter("diagnostics_topic").value)
        self.marker_topic = str(self.get_parameter("marker_topic").value)
        self.frame_id = str(self.get_parameter("frame_id").value)
        self.max_items = int(self.get_parameter("max_items").value)
        self.anchor_x = float(self.get_parameter("anchor_x").value)
        self.anchor_y = float(self.get_parameter("anchor_y").value)
        self.anchor_z = float(self.get_parameter("anchor_z").value)
        self.line_spacing = float(self.get_parameter("line_spacing").value)
        self.text_height = float(self.get_parameter("text_height").value)
        self.show_ok_items = bool(self.get_parameter("show_ok_items").value)
        self.show_messages = bool(self.get_parameter("show_messages").value)
        self.use_colors = bool(self.get_parameter("use_colors").value)
        self.health_status_prefix = str(self.get_parameter("health_status_prefix").value).strip("/")

        publish_rate = float(self.get_parameter("publish_rate_hz").value)
        publish_rate = publish_rate if publish_rate > 0.0 else 2.0

        self.latest: DiagnosticArray | None = None
        self.sub = self.create_subscription(
            DiagnosticArray,
            self.diagnostics_topic,
            self._on_diag,
            10,
        )
        self.pub = self.create_publisher(MarkerArray, self.marker_topic, 10)
        self.timer = self.create_timer(1.0 / publish_rate, self._tick)

        self.get_logger().info(
            f"system_health_rviz started: diagnostics={self.diagnostics_topic} "
            f"markers={self.marker_topic} frame={self.frame_id}"
        )

    def _on_diag(self, msg: DiagnosticArray) -> None:
        self.latest = msg

    def _mk_text(self, marker_id: int, x: float, y: float, z: float, text: str, color: ColorRGBA) -> Marker:
        m = Marker()
        m.header.frame_id = self.frame_id
        m.header.stamp = self.get_clock().now().to_msg()
        m.ns = "system_health"
        m.id = marker_id
        m.type = Marker.TEXT_VIEW_FACING
        m.action = Marker.ADD
        m.pose.position.x = x
        m.pose.position.y = y
        m.pose.position.z = z
        m.pose.orientation.w = 1.0
        m.scale.z = self.text_height
        m.color.r = color.r
        m.color.g = color.g
        m.color.b = color.b
        m.color.a = color.a
        m.text = text
        return m

    def _tick(self) -> None:
        out = MarkerArray()

        clear = Marker()
        clear.action = Marker.DELETEALL
        out.markers.append(clear)

        if self.latest is None or not self.latest.status:
            out.markers.append(
                self._mk_text(
                    1,
                    self.anchor_x,
                    self.anchor_y,
                    self.anchor_z,
                    "SYSTEM HEALTH: waiting for /diagnostics ...",
                    ColorRGBA(0.75, 0.75, 0.75, 1.0),
                )
            )
            self.pub.publish(out)
            return

        statuses = list(self.latest.status)
        prefix = f"{self.health_status_prefix}/"
        statuses = [s for s in statuses if (s.name or "").startswith(prefix)]

        if not statuses:
            out.markers.append(
                self._mk_text(
                    1,
                    self.anchor_x,
                    self.anchor_y,
                    self.anchor_z,
                    f"SYSTEM HEALTH: no '{prefix}' entries on /diagnostics yet",
                    ColorRGBA(0.75, 0.75, 0.75, 1.0),
                )
            )
            self.pub.publish(out)
            return

        summary = None
        details: list[DiagnosticStatus] = []
        for s in statuses:
            if s.name == f"{self.health_status_prefix}/summary" or s.name.endswith("/summary"):
                summary = s
            else:
                details.append(s)

        if summary is None:
            summary = statuses[0]

        kv = {item.key: item.value for item in summary.values}
        checks_total = kv.get("checks_total", "?")
        checks_ok = kv.get("ok", "?")
        checks_warn = kv.get("warn", "?")
        checks_critical_fail = kv.get("critical_failures", "?")

        header = (
            f"SYSTEM HEALTH [{_status_label(summary.level)}]  "
            f"ok={checks_ok}/{checks_total} warn={checks_warn} critical={checks_critical_fail}"
        )
        out.markers.append(
            self._mk_text(
                1,
                self.anchor_x,
                self.anchor_y,
                self.anchor_z,
                header[:180],
                _status_color(summary.level),
            )
        )

        grouped: dict[str, list[DiagnosticStatus]] = defaultdict(list)
        for status in details:
            cat, _target = _extract_category_and_target(status.name)
            grouped[cat].append(status)
        for cat in grouped:
            grouped[cat].sort(key=lambda s: _extract_category_and_target(s.name)[1])

        lines: list[tuple[str, ColorRGBA]] = []
        for cat in ("nodes", "topics", "services", "other"):
            items = grouped.get(cat, [])
            if not items:
                continue
            lines.append((f"{cat.upper()}:", ColorRGBA(0.75, 0.75, 0.75, 1.0)))
            for status in items:
                if not self.show_ok_items and status.level == DiagnosticStatus.OK:
                    continue
                _c, target = _extract_category_and_target(status.name)
                line = f"  [{_status_short(status.level)}] {target}"
                if self.show_messages:
                    line += f"  -  {status.message}"
                color = _status_color(status.level) if self.use_colors else ColorRGBA(1.0, 1.0, 1.0, 1.0)
                lines.append((line[:220], color))

        shown_lines = lines[: self.max_items]
        if len(lines) > len(shown_lines):
            shown_lines.append(
                (f"... +{len(lines) - len(shown_lines)} more checks", ColorRGBA(0.7, 0.7, 0.7, 1.0))
            )

        body_text = "\n".join(text for text, _ in shown_lines)
        out.markers.append(
            self._mk_text(
                2,
                self.anchor_x,
                self.anchor_y,
                self.anchor_z - self.line_spacing,
                body_text,
                ColorRGBA(1.0, 1.0, 1.0, 1.0),
            )
        )

        self.pub.publish(out)


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = SystemHealthRviz()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
