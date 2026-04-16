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
from rosidl_runtime_py.utilities import get_message, get_service

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


def _as_name_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = [str(v) for v in value]
    else:
        return []
    out: list[str] = []
    for raw in raw_items:
        name = _normalize_name(str(raw).strip())
        if not name:
            continue
        if name.startswith("<dynamic:"):
            continue
        if name not in out:
            out.append(name)
    return out


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


@dataclass
class ServiceProbeMetric:
    client: Any | None = None
    request_cls: Any | None = None
    service_type: str | None = None
    pending_future: Any | None = None
    pending_since: float | None = None
    last_probe_start: float | None = None
    last_ok_time: float | None = None
    last_error_time: float | None = None
    last_error: str | None = None


class SystemHealthMonitor(Node):
    def __init__(self) -> None:
        super().__init__("system_health_monitor")

        self.declare_parameter("config_file", "")
        self.declare_parameter("check_period_sec", 1.0)
        self.declare_parameter("startup_grace_sec", 8.0)
        self.declare_parameter("diagnostics_ns", "/system_health")
        self.declare_parameter("service_probe_default_interval_sec", 5.0)

        self.config_file = str(self.get_parameter("config_file").value or "").strip()
        self.check_period_sec = float(self.get_parameter("check_period_sec").value)
        self.startup_grace_sec = float(self.get_parameter("startup_grace_sec").value)
        self.diagnostics_ns = str(self.get_parameter("diagnostics_ns").value or "/system_health").strip("/")
        self.service_probe_default_interval_sec = float(
            self.get_parameter("service_probe_default_interval_sec").value
        )

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
        self.service_probe_metrics: dict[str, ServiceProbeMetric] = {}

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
            expected_publishers = _as_name_list(spec.get("expected_publishers"))
            expected_subscribers = _as_name_list(spec.get("expected_subscribers"))

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
                        "expected_publishers": expected_publishers,
                        "expected_subscribers": expected_subscribers,
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
            expected_servers = _as_name_list(spec.get("expected_servers"))
            expected_clients = _as_name_list(spec.get("expected_clients"))
            probe_call = bool(spec.get("probe_call", False))
            probe_timeout_sec = _as_float(spec.get("probe_timeout_sec"))
            if probe_timeout_sec is None:
                probe_timeout_sec = _as_float(spec.get("timeout_sec"))
            if probe_timeout_sec is None:
                probe_timeout_sec = 1.0
            probe_interval_sec = _as_float(spec.get("probe_interval_sec"))
            if probe_interval_sec is None:
                probe_interval_sec = self.service_probe_default_interval_sec
            probe_request = spec.get("probe_request")
            if probe_request is None:
                probe_request = {}
            if not isinstance(probe_request, dict):
                probe_request = {}

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

            probe_details: dict[str, Any] = {
                "probe_call": probe_call,
                "probe_timeout_sec": probe_timeout_sec,
                "probe_interval_sec": probe_interval_sec,
            }
            if probe_call and exists and type_ok:
                probe_level, probe_msg, probe_extra = self._service_probe_status(
                    service_name=name,
                    actual_types=actual_types,
                    expected_types=expected_types,
                    request_data=probe_request,
                    timeout_sec=probe_timeout_sec,
                    interval_sec=probe_interval_sec,
                    critical=critical,
                )
                probe_details.update(probe_extra)
                if probe_level > level:
                    level = probe_level
                    msg = probe_msg

            results.append(
                CheckResult(
                    name=name,
                    level=level,
                    message=msg,
                    category="services",
                    critical=critical,
                    details={
                        "expected_types": expected_types,
                        "actual_types": actual_types,
                        "expected_servers": expected_servers,
                        "expected_clients": expected_clients,
                        **probe_details,
                    },
                )
            )
        return results

    def _ensure_service_probe_client(
        self,
        service_name: str,
        service_type: str,
    ) -> tuple[ServiceProbeMetric, str | None]:
        metric = self.service_probe_metrics.setdefault(service_name, ServiceProbeMetric())
        if (
            metric.client is not None
            and metric.request_cls is not None
            and metric.service_type == service_type
        ):
            return metric, None
        try:
            srv_cls = get_service(service_type)
        except Exception as exc:
            metric.last_error = f"cannot import service type {service_type}: {exc}"
            metric.last_error_time = _now_sec(self)
            return metric, metric.last_error
        try:
            metric.client = self.create_client(srv_cls, service_name)
            metric.request_cls = srv_cls.Request
            metric.service_type = service_type
            metric.pending_future = None
            metric.pending_since = None
            return metric, None
        except Exception as exc:
            metric.last_error = f"failed creating service client for {service_name}: {exc}"
            metric.last_error_time = _now_sec(self)
            return metric, metric.last_error

    def _build_probe_request(
        self,
        request_cls: Any,
        request_data: dict[str, Any],
    ) -> tuple[Any | None, str | None]:
        try:
            req = request_cls()
        except Exception as exc:
            return None, f"cannot create request object: {exc}"
        for key, value in request_data.items():
            if not hasattr(req, key):
                return None, f"request has no field '{key}'"
            try:
                setattr(req, key, value)
            except Exception as exc:
                return None, f"invalid request field '{key}': {exc}"
        return req, None

    def _on_service_probe_done(self, service_name: str, future: Any) -> None:
        metric = self.service_probe_metrics.get(service_name)
        if metric is None:
            return
        metric.pending_future = None
        metric.pending_since = None
        now = _now_sec(self)
        try:
            future.result()
            metric.last_ok_time = now
            metric.last_error = None
        except Exception as exc:
            metric.last_error = f"service probe call failed: {exc}"
            metric.last_error_time = now

    def _service_probe_status(
        self,
        service_name: str,
        actual_types: list[str],
        expected_types: list[str],
        request_data: dict[str, Any],
        timeout_sec: float,
        interval_sec: float,
        critical: bool,
    ) -> tuple[int, str, dict[str, Any]]:
        details: dict[str, Any] = {}
        now = _now_sec(self)
        service_type = actual_types[0] if actual_types else (expected_types[0] if expected_types else "")
        if not service_type:
            return (
                DiagnosticStatus.WARN,
                "service probe skipped: unknown service type",
                details,
            )

        metric, err = self._ensure_service_probe_client(service_name, service_type)
        details["probe_service_type"] = service_type
        if err is not None:
            return (
                DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN,
                f"service probe setup failed: {err}",
                details,
            )

        if metric.pending_future is not None and metric.pending_since is not None:
            age = max(0.0, now - metric.pending_since)
            details["probe_pending_age_sec"] = round(age, 3)
            if age > timeout_sec:
                try:
                    metric.pending_future.cancel()
                except Exception:
                    pass
                metric.pending_future = None
                metric.pending_since = None
                metric.last_error = f"service probe timeout: {age:.2f}s > {timeout_sec:.2f}s"
                metric.last_error_time = now
                return (
                    DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN,
                    metric.last_error,
                    details,
                )
            return (
                DiagnosticStatus.WARN,
                f"service probe pending ({age:.2f}s)",
                details,
            )

        due = metric.last_probe_start is None or (now - metric.last_probe_start) >= interval_sec
        if due:
            if not metric.client.service_is_ready():
                metric.last_error = "service probe: client is not ready yet"
                metric.last_error_time = now
                return (
                    DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN,
                    metric.last_error,
                    details,
                )
            req, req_err = self._build_probe_request(metric.request_cls, request_data)
            if req_err is not None:
                metric.last_error = f"service probe request invalid: {req_err}"
                metric.last_error_time = now
                return (
                    DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN,
                    metric.last_error,
                    details,
                )
            try:
                metric.last_probe_start = now
                metric.pending_future = metric.client.call_async(req)
                metric.pending_since = now
                metric.pending_future.add_done_callback(
                    lambda fut, svc=service_name: self._on_service_probe_done(svc, fut)
                )
            except Exception as exc:
                metric.last_error = f"service probe call setup failed: {exc}"
                metric.last_error_time = now
                return (
                    DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN,
                    metric.last_error,
                    details,
                )
            if metric.last_ok_time is None:
                return (
                    DiagnosticStatus.WARN,
                    "service probe started; waiting first response",
                    details,
                )
            age = max(0.0, now - metric.last_ok_time)
            details["probe_last_ok_age_sec"] = round(age, 3)
            return (
                DiagnosticStatus.OK,
                f"service is present; probe in progress (last ok {age:.2f}s ago)",
                details,
            )

        if metric.last_ok_time is not None:
            age = max(0.0, now - metric.last_ok_time)
            details["probe_last_ok_age_sec"] = round(age, 3)
            return (
                DiagnosticStatus.OK,
                "service is present; probe OK",
                details,
            )
        if metric.last_error is not None:
            return (
                DiagnosticStatus.ERROR if critical else DiagnosticStatus.WARN,
                metric.last_error,
                details,
            )
        return (
            DiagnosticStatus.WARN,
            "service probe waiting first cycle",
            details,
        )

    def _annotate_root_causes(self, checks: list[CheckResult]) -> None:
        failing_nodes = {
            c.name
            for c in checks
            if c.category == "nodes" and c.level >= DiagnosticStatus.WARN and "not found in graph" in c.message
        }
        for c in checks:
            if c.level == DiagnosticStatus.OK:
                continue
            if c.category == "nodes":
                c.details["root_cause"] = True
                c.details["caused_by"] = []
                continue
            if c.category == "topics":
                deps = _as_name_list(c.details.get("expected_publishers"))
            elif c.category == "services":
                deps = _as_name_list(c.details.get("expected_servers"))
            else:
                deps = []
            caused_by = sorted(n for n in deps if n in failing_nodes)
            if caused_by:
                c.details["root_cause"] = False
                c.details["caused_by"] = caused_by
                if "likely caused by missing node" not in c.message:
                    c.message = f"{c.message}; likely caused by missing node(s): {', '.join(caused_by)}"
            else:
                c.details["root_cause"] = True
                c.details["caused_by"] = []

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
            self._annotate_root_causes(checks)

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
