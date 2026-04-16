#!/usr/bin/env python3
"""Runtime health monitor driven by a YAML system contract.

Publishes:
- /diagnostics (diagnostic_msgs/DiagnosticArray)
- /system_ready (std_msgs/Bool)
- /system_health_summary (std_msgs/String)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import rclpy
from rclpy.node import Node
from rosidl_runtime_py.utilities import get_message

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from std_msgs.msg import Bool, String


SYSTEM_TOPICS = {
    "/parameter_events",
    "/rosout",
}

SYSTEM_SERVICE_SUFFIXES = (
    "/describe_parameters",
    "/get_parameter_types",
    "/get_parameters",
    "/list_parameters",
    "/set_parameters",
    "/set_parameters_atomically",
)

SYSTEM_NODE_PREFIXES = (
    "/_",
    "/system_health_cli",
)


def _is_system_node(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in SYSTEM_NODE_PREFIXES)


def _is_system_topic(name: str) -> bool:
    return name in SYSTEM_TOPICS


def _is_system_service(name: str) -> bool:
    if name.startswith("/_"):
        return True
    return any(name.endswith(suffix) for suffix in SYSTEM_SERVICE_SUFFIXES)


def _now_sec(node: Node) -> float:
    return node.get_clock().now().nanoseconds / 1e9


def _normalize_name(name: str) -> str:
    if not name:
        return name
    if name.startswith("<dynamic:"):
        return name
    if not name.startswith("/"):
        return "/" + name
    return name


def _type_basename(type_name: str) -> str:
    if not type_name:
        return type_name
    if "/" in type_name:
        return type_name.split("/")[-1]
    return type_name


def _type_match(expected: str, actual: str) -> bool:
    if not expected or expected == "UNKNOWN":
        return True
    if expected == actual:
        return True
    return _type_basename(expected) == _type_basename(actual)


def _as_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except Exception:
        return None


@dataclass
class TopicMetric:
    stamps: deque[float] = field(default_factory=lambda: deque(maxlen=64))
    last_seen: float | None = None
    callback_errors: int = 0
    subscribed_type: str | None = None
    subscription_ready: bool = False
    subscription_error: str | None = None


@dataclass
class CheckResult:
    name: str
    level: int
    message: str
    category: str
    critical: bool
    details: dict[str, Any] = field(default_factory=dict)


class SystemHealthMonitor(Node):
    def __init__(self) -> None:
        super().__init__("system_health_monitor")

        self.declare_parameter("config_file", "")
        self.declare_parameter("check_period_sec", 1.0)
        self.declare_parameter("startup_grace_sec", 8.0)
        self.declare_parameter("diagnostics_ns", "/system_health")

        self.config_file = str(self.get_parameter("config_file").value or "").strip()
        self.check_period_sec = float(self.get_parameter("check_period_sec").value)
        self.startup_grace_sec = float(self.get_parameter("startup_grace_sec").value)
        self.diagnostics_ns = str(self.get_parameter("diagnostics_ns").value or "/system_health").strip("/")

        if not self.config_file:
            self.get_logger().fatal("Missing required parameter 'config_file'.")
            raise RuntimeError("system_health_monitor requires config_file parameter")

        self.config_path = Path(self.config_file).expanduser()
        if not self.config_path.exists():
            self.get_logger().fatal(f"Config file not found: {self.config_path}")
            raise RuntimeError(f"Config file not found: {self.config_path}")

        self.spec = self._load_spec(self.config_path)
        self.startup_time = _now_sec(self)

        self.diagnostics_pub = self.create_publisher(DiagnosticArray, "/diagnostics", 10)
        self.ready_pub = self.create_publisher(Bool, "/system_ready", 10)
        self.summary_pub = self.create_publisher(String, "/system_health_summary", 10)

        self.topic_metrics: dict[str, TopicMetric] = {}
        self.topic_subscriptions: dict[str, Any] = {}

        self.timer = self.create_timer(self.check_period_sec, self._tick)
        self.get_logger().info(f"System health monitor started with config: {self.config_path}")

    def _load_spec(self, path: Path) -> dict[str, Any]:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if "health_template" in data and isinstance(data["health_template"], dict):
            scope = data["health_template"]
        else:
            scope = data

        nodes = scope.get("nodes", []) or []
        topics = scope.get("topics", []) or []
        services = scope.get("services", []) or []

        # Normalize names upfront.
        for n in nodes:
            if isinstance(n, dict) and "name" in n:
                n["name"] = _normalize_name(str(n["name"]))
        for t in topics:
            if isinstance(t, dict) and "name" in t:
                t["name"] = _normalize_name(str(t["name"]))
        for s in services:
            if isinstance(s, dict) and "name" in s:
                s["name"] = _normalize_name(str(s["name"]))

        return {"nodes": nodes, "topics": topics, "services": services}

    def _active_nodes(self) -> set[str]:
        out = set()
        for name, ns in self.get_node_names_and_namespaces():
            ns = ns if ns else "/"
            if not ns.startswith("/"):
                ns = "/" + ns
            if ns.endswith("/"):
                full = ns + name
            elif ns == "/":
                full = "/" + name
            else:
                full = ns + "/" + name
            out.add(full)
        return out

    def _topic_map(self) -> dict[str, list[str]]:
        return {name: types for name, types in self.get_topic_names_and_types()}

    def _service_map(self) -> dict[str, list[str]]:
        return {name: types for name, types in self.get_service_names_and_types()}

    def _ensure_topic_subscription(self, topic_name: str, topic_types: list[str]) -> None:
        metric = self.topic_metrics.setdefault(topic_name, TopicMetric())
        if metric.subscription_ready or metric.subscription_error:
            return
        if not topic_types:
            return
        ros_type = topic_types[0]
        try:
            msg_type = get_message(ros_type)
        except Exception as exc:
            metric.subscription_error = f"cannot import message type {ros_type}: {exc}"
            return

        def _cb(_msg: Any, topic: str = topic_name) -> None:
            now = _now_sec(self)
            m = self.topic_metrics.setdefault(topic, TopicMetric())
            m.last_seen = now
            m.stamps.append(now)

        try:
            sub = self.create_subscription(msg_type, topic_name, _cb, 10)
            self.topic_subscriptions[topic_name] = sub
            metric.subscription_ready = True
            metric.subscribed_type = ros_type
        except Exception as exc:
            metric.subscription_error = f"failed creating subscription: {exc}"

    def _topic_rate(self, metric: TopicMetric) -> float | None:
        if len(metric.stamps) < 2:
            return None
        dt = metric.stamps[-1] - metric.stamps[0]
        if dt <= 0:
            return None
        return (len(metric.stamps) - 1) / dt

    def _is_grace_period(self) -> bool:
        return (_now_sec(self) - self.startup_time) < self.startup_grace_sec

    def _node_checks(self, active_nodes: set[str]) -> list[CheckResult]:
        results: list[CheckResult] = []
        for spec in self.spec["nodes"]:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name", "")).strip()
            if not name or name.startswith("<dynamic:"):
                continue
            critical = bool(spec.get("critical", False))
            condition = str(spec.get("condition", "always"))
            exists = name in active_nodes

            if exists:
                level = DiagnosticStatus.OK
                msg = "node is active"
            else:
                level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                msg = "node not found in graph"

            results.append(
                CheckResult(
                    name=name,
                    level=level,
                    message=msg,
                    category="nodes",
                    critical=critical,
                    details={"condition": condition},
                )
            )
        return results

    def _topic_checks(self, topic_map: dict[str, list[str]]) -> list[CheckResult]:
        results: list[CheckResult] = []
        in_grace = self._is_grace_period()

        for spec in self.spec["topics"]:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name", "")).strip()
            if not name or name.startswith("<dynamic:"):
                continue
            critical = bool(spec.get("critical", False))
            expected_types = spec.get("types") or []
            if isinstance(expected_types, str):
                expected_types = [expected_types]
            expected_types = [str(t) for t in expected_types if str(t)]

            exists = name in topic_map
            actual_types = topic_map.get(name, [])
            type_ok = True
            if expected_types and actual_types:
                type_ok = any(_type_match(exp, act) for exp in expected_types for act in actual_types)

            min_rate_hz = _as_float(spec.get("min_rate_hz"))
            max_age_sec = _as_float(spec.get("max_age_sec"))

            base_level = DiagnosticStatus.OK
            base_msg = "topic is present"
            if not exists:
                base_level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                base_msg = "topic not found in graph"
            elif not type_ok:
                base_level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                base_msg = "topic type mismatch"

            metric = self.topic_metrics.setdefault(name, TopicMetric())
            if exists and (min_rate_hz is not None or max_age_sec is not None):
                self._ensure_topic_subscription(name, actual_types)
                if metric.subscription_error:
                    level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                    msg = metric.subscription_error
                elif metric.last_seen is None:
                    if in_grace:
                        level = DiagnosticStatus.WARN
                        msg = "waiting for first message (grace period)"
                    else:
                        level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                        msg = "no messages received"
                else:
                    level = base_level
                    msg = base_msg
                    age = _now_sec(self) - metric.last_seen
                    if max_age_sec is not None and age > max_age_sec:
                        level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                        msg = f"stale topic data: age={age:.2f}s > max_age_sec={max_age_sec:.2f}s"

                    rate = self._topic_rate(metric)
                    if min_rate_hz is not None:
                        if rate is None and not in_grace:
                            level = max(level, DiagnosticStatus.WARN)
                            msg = "insufficient samples for rate calculation"
                        elif rate is not None and rate < min_rate_hz:
                            level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                            msg = f"topic rate too low: {rate:.2f}Hz < min_rate_hz={min_rate_hz:.2f}Hz"
            else:
                level = base_level
                msg = base_msg

            results.append(
                CheckResult(
                    name=name,
                    level=level,
                    message=msg,
                    category="topics",
                    critical=critical,
                    details={
                        "expected_types": expected_types,
                        "actual_types": actual_types,
                        "min_rate_hz": min_rate_hz,
                        "max_age_sec": max_age_sec,
                    },
                )
            )
        return results

    def _service_checks(self, service_map: dict[str, list[str]]) -> list[CheckResult]:
        results: list[CheckResult] = []
        for spec in self.spec["services"]:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name", "")).strip()
            if not name or name.startswith("<dynamic:"):
                continue
            critical = bool(spec.get("critical", False))

            expected_types = spec.get("types") or []
            if isinstance(expected_types, str):
                expected_types = [expected_types]
            expected_types = [str(t) for t in expected_types if str(t)]

            exists = name in service_map
            actual_types = service_map.get(name, [])
            type_ok = True
            if expected_types and actual_types:
                type_ok = any(_type_match(exp, act) for exp in expected_types for act in actual_types)

            if not exists:
                level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                msg = "service not found in graph"
            elif not type_ok:
                level = DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN
                msg = "service type mismatch"
            else:
                level = DiagnosticStatus.OK
                msg = "service is present"

            results.append(
                CheckResult(
                    name=name,
                    level=level,
                    message=msg,
                    category="services",
                    critical=critical,
                    details={"expected_types": expected_types, "actual_types": actual_types},
                )
            )
        return results

    def _diag_status(self, result: CheckResult) -> DiagnosticStatus:
        status = DiagnosticStatus()
        status.level = result.level
        status.name = f"{self.diagnostics_ns}/{result.category}{result.name}"
        status.message = result.message
        status.hardware_id = "agar_system"
        values = {
            "critical": result.critical,
            **result.details,
        }
        for key, value in values.items():
            kv = KeyValue()
            kv.key = str(key)
            kv.value = str(value)
            status.values.append(kv)
        return status

    def _runtime_interface_counts(
        self,
        active_nodes: set[str],
        topic_map: dict[str, list[str]],
        service_map: dict[str, list[str]],
    ) -> dict[str, int]:
        node_system = sum(1 for n in active_nodes if _is_system_node(n))
        topic_system = sum(1 for n in topic_map if _is_system_topic(n))
        service_system = sum(1 for n in service_map if _is_system_service(n))

        node_total = len(active_nodes)
        topic_total = len(topic_map)
        service_total = len(service_map)

        return {
            "runtime_nodes_total": node_total,
            "runtime_nodes_system": node_system,
            "runtime_nodes_user": max(0, node_total - node_system),
            "runtime_topics_total": topic_total,
            "runtime_topics_system": topic_system,
            "runtime_topics_user": max(0, topic_total - topic_system),
            "runtime_services_total": service_total,
            "runtime_services_system": service_system,
            "runtime_services_user": max(0, service_total - service_system),
        }

    def _tick(self) -> None:
        try:
            active_nodes = self._active_nodes()
            topic_map = self._topic_map()
            service_map = self._service_map()
            runtime_counts = self._runtime_interface_counts(active_nodes, topic_map, service_map)

            checks: list[CheckResult] = []
            checks.extend(self._node_checks(active_nodes))
            checks.extend(self._topic_checks(topic_map))
            checks.extend(self._service_checks(service_map))

            critical_failures = [
                c for c in checks if c.critical and c.level >= DiagnosticStatus.ERROR
            ]
            warns = [c for c in checks if c.level == DiagnosticStatus.WARN]
            oks = [c for c in checks if c.level == DiagnosticStatus.OK]

            ready = len(critical_failures) == 0

            diag = DiagnosticArray()
            diag.header.stamp = self.get_clock().now().to_msg()

            summary = DiagnosticStatus()
            summary.level = (
                DiagnosticStatus.ERROR
                if critical_failures
                else (DiagnosticStatus.WARN if warns else DiagnosticStatus.OK)
            )
            summary.name = f"{self.diagnostics_ns}/summary"
            summary.message = (
                "system READY" if ready else f"critical failures: {len(critical_failures)}"
            )
            summary.hardware_id = "agar_system"
            summary_values = {
                "checks_total": len(checks),
                "ok": len(oks),
                "warn": len(warns),
                "critical_failures": len(critical_failures),
            }
            summary_values.update(runtime_counts)
            for key, value in summary_values.items():
                kv = KeyValue()
                kv.key = str(key)
                kv.value = str(value)
                summary.values.append(kv)
            diag.status.append(summary)

            for check in checks:
                diag.status.append(self._diag_status(check))
            self.diagnostics_pub.publish(diag)

            ready_msg = Bool()
            ready_msg.data = ready
            self.ready_pub.publish(ready_msg)

            summary_msg = String()
            if ready:
                summary_msg.data = (
                    f"READY: {len(oks)}/{len(checks)} checks OK, {len(warns)} WARN."
                )
            else:
                top = "; ".join(f"{c.category}:{c.name} -> {c.message}" for c in critical_failures[:5])
                summary_msg.data = (
                    f"NOT READY: {len(critical_failures)} critical failure(s). {top}"
                )
            self.summary_pub.publish(summary_msg)
        except Exception as exc:
            self.get_logger().error(f"Health monitor tick failed: {exc}")


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = SystemHealthMonitor()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
