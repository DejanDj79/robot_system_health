#!/usr/bin/env python3
"""Static extractor for expected ROS2 system components by launch scenario.

This script inspects:
1) Launch files (Node / IncludeLaunchDescription / DeclareLaunchArgument)
2) Python node modules mapped from setup.py entry_points
3) create_publisher/create_subscription/create_service/create_client calls

It generates a YAML template you can use as a starting point for health checks.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def safe_unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return repr(node)


def const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def keyword_arg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def pos_or_kw_str(call: ast.Call, pos_index: int, kw_name: str) -> str | None:
    kw = keyword_arg(call, kw_name)
    if kw is not None:
        return const_str(kw)
    if len(call.args) > pos_index:
        return const_str(call.args[pos_index])
    return None


def merge_conditions(parent: str, local: str | None) -> str:
    local_norm = (local or "always").strip()
    if not local_norm or local_norm == "always":
        return parent
    if parent == "always":
        return local_norm
    return f"({parent}) AND ({local_norm})"


def extract_condition_expr(call: ast.Call) -> str:
    cond_node = keyword_arg(call, "condition")
    if cond_node is None:
        return "always"
    return safe_unparse(cond_node) or "always"


def string_literals_in(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    vals: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            vals.append(child.value)
    return vals


def package_share_calls_in(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    pkgs: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = call_name(child)
        if name not in {"get_package_share_directory", "get_package_share_path"}:
            continue
        if child.args:
            pkg = const_str(child.args[0])
            if pkg:
                pkgs.append(pkg)
    return pkgs


def scalar_yaml(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return "''"
    safe_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:-")
    if all(ch in safe_chars for ch in text):
        return text
    return "'" + text.replace("'", "''") + "'"


def to_yaml(value: Any, indent: int = 0) -> list[str]:
    sp = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        if not value:
            return [sp + "{}"]
        for k, v in value.items():
            key = str(k)
            if isinstance(v, (dict, list)):
                lines.append(f"{sp}{key}:")
                lines.extend(to_yaml(v, indent + 2))
            else:
                lines.append(f"{sp}{key}: {scalar_yaml(v)}")
        return lines
    if isinstance(value, list):
        lines = []
        if not value:
            return [sp + "[]"]
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{sp}-")
                lines.extend(to_yaml(item, indent + 2))
            else:
                lines.append(f"{sp}- {scalar_yaml(item)}")
        return lines
    return [sp + scalar_yaml(value)]


def path_label(path: Path, workspace: Path) -> str:
    try:
        return str(path.relative_to(workspace))
    except Exception:
        return str(path)


@dataclass(frozen=True)
class NodeKey:
    source_launch: str
    package: str
    executable: str
    condition: str
    launch_name: str | None


class StaticSystemExtractor:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.src_root = self.workspace / "src"
        self.console_scripts: dict[tuple[str, str], str] = {}
        self.module_cache: dict[Path, dict[str, Any]] = {}
        self.seen_launches: set[Path] = set()

        self.launch_arguments: dict[str, dict[str, Any]] = {}
        self.includes: list[dict[str, Any]] = []
        self.unresolved_includes: list[dict[str, Any]] = []
        self.nodes: dict[NodeKey, dict[str, Any]] = {}

        self._load_console_scripts()

    def _load_console_scripts(self) -> None:
        for setup_py in self.src_root.glob("*/setup.py"):
            try:
                tree = ast.parse(setup_py.read_text(encoding="utf-8"))
            except Exception:
                continue
            constants: dict[str, str] = {}
            for stmt in tree.body:
                if not isinstance(stmt, ast.Assign) or not stmt.targets:
                    continue
                target = stmt.targets[0]
                if isinstance(target, ast.Name):
                    value = const_str(stmt.value)
                    if value is not None:
                        constants[target.id] = value

            def resolve_str(node: ast.AST | None) -> str | None:
                if node is None:
                    return None
                value = const_str(node)
                if value is not None:
                    return value
                if isinstance(node, ast.Name):
                    return constants.get(node.id)
                return None

            package_name: str | None = None
            entries: list[str] = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or call_name(node) != "setup":
                    continue
                name_kw = keyword_arg(node, "name")
                if package_name is None:
                    package_name = resolve_str(name_kw)
                ep_kw = keyword_arg(node, "entry_points")
                if not isinstance(ep_kw, ast.Dict):
                    continue
                for k, v in zip(ep_kw.keys, ep_kw.values):
                    if resolve_str(k) != "console_scripts":
                        continue
                    if isinstance(v, (ast.List, ast.Tuple)):
                        for item in v.elts:
                            val = resolve_str(item)
                            if val:
                                entries.append(val)
            if not package_name:
                continue
            for entry in entries:
                if "=" not in entry:
                    continue
                executable, target = [part.strip() for part in entry.split("=", 1)]
                if ":" in target:
                    module = target.split(":", 1)[0].strip()
                else:
                    module = target
                self.console_scripts[(package_name, executable)] = module

    def _resolve_include_path(self, include_call: ast.Call, assigns: dict[str, ast.AST]) -> tuple[Path | None, str | None, str | None]:
        source_expr: ast.AST | None = None
        if include_call.args:
            source_expr = include_call.args[0]
        else:
            source_expr = keyword_arg(include_call, "launch_description_source")
        if isinstance(source_expr, ast.Name) and source_expr.id in assigns:
            source_expr = assigns[source_expr.id]

        if source_expr is None:
            return None, None, None

        literals = string_literals_in(source_expr)
        py_names = [Path(s).name for s in literals if s.endswith(".py")]
        filename = py_names[-1] if py_names else None
        packages = package_share_calls_in(source_expr)
        package = packages[-1] if packages else None

        if not filename:
            return None, package, None
        if package:
            candidate = self.src_root / package / "launch" / filename
            if candidate.exists():
                return candidate, package, filename
        for candidate in self.src_root.rglob(filename):
            if "launch" in candidate.parts:
                return candidate, package, filename
        return None, package, filename

    def _parse_module_interfaces(self, py_file: Path) -> dict[str, Any]:
        cached = self.module_cache.get(py_file)
        if cached is not None:
            return cached

        data = {
            "node_names": [],
            "publishes": [],
            "subscribes": [],
            "provides_services": [],
            "uses_services": [],
        }
        if not py_file.exists():
            self.module_cache[py_file] = data
            return data

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except Exception:
            self.module_cache[py_file] = data
            return data

        node_names: set[str] = set()
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            # super().__init__('node_name')
            if isinstance(call.func, ast.Attribute) and call.func.attr == "__init__":
                if isinstance(call.func.value, ast.Call) and call_name(call.func.value) == "super":
                    if call.args:
                        node_name = const_str(call.args[0])
                        if node_name:
                            node_names.add(node_name)

        def add_item(bucket: str, name: str, msg_type: str) -> None:
            items = data[bucket]
            if not any(i["name"] == name and i["type"] == msg_type for i in items):
                items.append({"name": name, "type": msg_type})

        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            if not isinstance(call.func, ast.Attribute):
                continue
            func = call.func.attr
            if func not in {"create_publisher", "create_subscription", "create_service", "create_client"}:
                continue

            if func == "create_publisher":
                type_index, name_index, bucket = 0, 1, "publishes"
                kw_name = "topic"
            elif func == "create_subscription":
                type_index, name_index, bucket = 0, 1, "subscribes"
                kw_name = "topic"
            elif func == "create_service":
                type_index, name_index, bucket = 0, 1, "provides_services"
                kw_name = "srv_name"
            else:
                type_index, name_index, bucket = 0, 1, "uses_services"
                kw_name = "srv_name"

            msg_type = ""
            if len(call.args) > type_index:
                msg_type = safe_unparse(call.args[type_index])
            elif keyword_arg(call, "msg_type"):
                msg_type = safe_unparse(keyword_arg(call, "msg_type"))

            target_node: ast.AST | None = None
            if len(call.args) > name_index:
                target_node = call.args[name_index]
            else:
                target_node = keyword_arg(call, kw_name)

            target = const_str(target_node)
            if target is None and target_node is not None:
                target = f"<dynamic:{safe_unparse(target_node)}>"
            if target is None:
                target = "<unknown>"

            add_item(bucket, target, msg_type or "UNKNOWN")

        data["node_names"] = sorted(node_names)
        for key in ("publishes", "subscribes", "provides_services", "uses_services"):
            data[key] = sorted(data[key], key=lambda x: (x["name"], x["type"]))

        self.module_cache[py_file] = data
        return data

    def _module_file_for(self, package: str, module: str) -> Path | None:
        if module.startswith(package + "."):
            rel = module[len(package) + 1 :]
        else:
            rel = module
        candidate = self.src_root / package / package / (rel.replace(".", "/") + ".py")
        if candidate.exists():
            return candidate
        file_name = rel.split(".")[-1] + ".py"
        for c in self.src_root.rglob(file_name):
            if package in c.parts:
                return c
        return None

    def _node_record(self, node_call: ast.Call, source_launch: Path, parent_condition: str) -> dict[str, Any] | None:
        package = pos_or_kw_str(node_call, 0, "package")
        executable = pos_or_kw_str(node_call, 1, "executable")
        if not package or not executable:
            return None

        launch_name = pos_or_kw_str(node_call, 2, "name")
        local_cond = extract_condition_expr(node_call)
        effective_cond = merge_conditions(parent_condition, local_cond)

        module = self.console_scripts.get((package, executable))
        module_file: Path | None = None
        interfaces: dict[str, Any] = {
            "node_names": [],
            "publishes": [],
            "subscribes": [],
            "provides_services": [],
            "uses_services": [],
        }
        if module:
            module_file = self._module_file_for(package, module)
            if module_file:
                interfaces = self._parse_module_interfaces(module_file)

        key = NodeKey(
            source_launch=path_label(source_launch, self.workspace),
            package=package,
            executable=executable,
            condition=effective_cond,
            launch_name=launch_name,
        )

        rec = {
            "source_launch": path_label(source_launch, self.workspace),
            "package": package,
            "executable": executable,
            "launch_name": launch_name,
            "condition": effective_cond,
            "module": module,
            "module_file": path_label(module_file, self.workspace) if module_file else None,
            "runtime_node_names": interfaces["node_names"],
            "publishes": interfaces["publishes"],
            "subscribes": interfaces["subscribes"],
            "provides_services": interfaces["provides_services"],
            "uses_services": interfaces["uses_services"],
        }
        self.nodes[key] = rec
        return rec

    def _record_launch_arg(self, arg_call: ast.Call, source_launch: Path, parent_condition: str) -> None:
        if not arg_call.args:
            return
        name = const_str(arg_call.args[0]) or pos_or_kw_str(arg_call, 0, "name")
        if not name:
            return
        default_node = keyword_arg(arg_call, "default_value")
        description_node = keyword_arg(arg_call, "description")
        default = const_str(default_node) if default_node is not None else None
        if default is None and default_node is not None:
            default = safe_unparse(default_node)
        description = const_str(description_node) if description_node is not None else None
        if description is None and description_node is not None:
            description = safe_unparse(description_node)

        self.launch_arguments.setdefault(
            name,
            {
                "default": default,
                "description": description,
                "conditions": [],
                "declared_in": [],
            },
        )
        self.launch_arguments[name]["conditions"].append(parent_condition)
        self.launch_arguments[name]["declared_in"].append(path_label(source_launch, self.workspace))

    def _process_action_expr(
        self,
        expr: ast.AST,
        source_launch: Path,
        parent_condition: str,
        assigns: dict[str, ast.AST],
    ) -> None:
        if isinstance(expr, ast.Name) and expr.id in assigns:
            self._process_action_expr(assigns[expr.id], source_launch, parent_condition, assigns)
            return
        if not isinstance(expr, ast.Call):
            return

        name = call_name(expr)
        if name == "Node":
            self._node_record(expr, source_launch, parent_condition)
            return
        if name == "DeclareLaunchArgument":
            self._record_launch_arg(expr, source_launch, parent_condition)
            return
        if name == "IncludeLaunchDescription":
            include_cond = merge_conditions(parent_condition, extract_condition_expr(expr))
            resolved, package, filename = self._resolve_include_path(expr, assigns)
            include_rec = {
                "source_launch": path_label(source_launch, self.workspace),
                "condition": include_cond,
                "package_hint": package,
                "filename_hint": filename,
                "resolved_path": path_label(resolved, self.workspace) if resolved else None,
            }
            self.includes.append(include_rec)
            if resolved and resolved.exists():
                self._parse_launch_file(resolved, include_cond)
            else:
                self.unresolved_includes.append(include_rec)
            return
        if name == "LaunchDescription" and expr.args:
            first = expr.args[0]
            if isinstance(first, (ast.List, ast.Tuple)):
                for item in first.elts:
                    self._process_action_expr(item, source_launch, parent_condition, assigns)

    def _parse_launch_file(self, launch_file: Path, parent_condition: str) -> None:
        if launch_file in self.seen_launches:
            return
        self.seen_launches.add(launch_file)

        try:
            tree = ast.parse(launch_file.read_text(encoding="utf-8"))
        except Exception:
            return

        gen_fn = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "generate_launch_description":
                gen_fn = node
                break
        if gen_fn is None:
            return

        assigns: dict[str, ast.AST] = {}
        launch_desc_actions: dict[str, list[ast.AST]] = defaultdict(list)

        for stmt in gen_fn.body:
            if isinstance(stmt, ast.Assign) and stmt.targets:
                if isinstance(stmt.targets[0], ast.Name):
                    var = stmt.targets[0].id
                    assigns[var] = stmt.value
                    if isinstance(stmt.value, ast.Call) and call_name(stmt.value) == "LaunchDescription":
                        if stmt.value.args and isinstance(stmt.value.args[0], (ast.List, ast.Tuple)):
                            launch_desc_actions[var].extend(stmt.value.args[0].elts)

            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "add_action":
                    base = call.func.value
                    if isinstance(base, ast.Name) and call.args:
                        launch_desc_actions[base.id].append(call.args[0])

        action_exprs: list[ast.AST] = []
        for stmt in gen_fn.body:
            if isinstance(stmt, ast.Return):
                ret = stmt.value
                if isinstance(ret, ast.Call) and call_name(ret) == "LaunchDescription":
                    if ret.args and isinstance(ret.args[0], (ast.List, ast.Tuple)):
                        action_exprs.extend(ret.args[0].elts)
                elif isinstance(ret, ast.Name):
                    action_exprs.extend(launch_desc_actions.get(ret.id, []))

        for action in action_exprs:
            self._process_action_expr(action, launch_file, parent_condition, assigns)

    def analyze(self, launch_entrypoint: Path) -> dict[str, Any]:
        self._parse_launch_file(launch_entrypoint, "always")

        node_records = sorted(
            self.nodes.values(),
            key=lambda n: (n["source_launch"], n["package"], n["executable"], n["condition"]),
        )
        includes = sorted(
            self.includes,
            key=lambda i: (i["source_launch"], i["resolved_path"] or "", i["filename_hint"] or ""),
        )
        unresolved = sorted(
            self.unresolved_includes,
            key=lambda i: (i["source_launch"], i["filename_hint"] or ""),
        )
        launch_args = []
        for name, meta in sorted(self.launch_arguments.items()):
            launch_args.append(
                {
                    "name": name,
                    "default": meta["default"],
                    "description": meta["description"],
                    "conditions": sorted(set(meta["conditions"])),
                    "declared_in": sorted(set(meta["declared_in"])),
                }
            )

        topic_publishers: dict[str, set[str]] = defaultdict(set)
        topic_subscribers: dict[str, set[str]] = defaultdict(set)
        topic_types: dict[str, set[str]] = defaultdict(set)
        service_servers: dict[str, set[str]] = defaultdict(set)
        service_clients: dict[str, set[str]] = defaultdict(set)
        service_types: dict[str, set[str]] = defaultdict(set)

        for node in node_records:
            display_name = node["launch_name"] or node["executable"]
            for pub in node["publishes"]:
                topic_publishers[pub["name"]].add(display_name)
                topic_types[pub["name"]].add(pub["type"])
            for sub in node["subscribes"]:
                topic_subscribers[sub["name"]].add(display_name)
                topic_types[sub["name"]].add(sub["type"])
            for srv in node["provides_services"]:
                service_servers[srv["name"]].add(display_name)
                service_types[srv["name"]].add(srv["type"])
            for cli in node["uses_services"]:
                service_clients[cli["name"]].add(display_name)
                service_types[cli["name"]].add(cli["type"])

        health_nodes = []
        for node in node_records:
            runtime_name = node["runtime_node_names"][0] if node["runtime_node_names"] else (node["launch_name"] or node["executable"])
            health_nodes.append(
                {
                    "name": f"/{runtime_name}" if not str(runtime_name).startswith("/") else runtime_name,
                    "critical": True if node["condition"] == "always" else False,
                    "condition": node["condition"],
                    "source_launch": node["source_launch"],
                    "package": node["package"],
                    "executable": node["executable"],
                    "note": "critical=true is a default guess; adjust per scenario",
                }
            )

        health_topics = []
        all_topics = sorted(set(topic_publishers) | set(topic_subscribers))
        for topic in all_topics:
            health_topics.append(
                {
                    "name": topic,
                    "critical": False,
                    "min_rate_hz": None,
                    "max_age_sec": None,
                    "types": sorted(topic_types.get(topic, set())),
                    "expected_publishers": sorted(topic_publishers.get(topic, set())),
                    "expected_subscribers": sorted(topic_subscribers.get(topic, set())),
                    "externally_provided": bool(topic_subscribers.get(topic)) and not bool(topic_publishers.get(topic)),
                }
            )

        health_services = []
        all_services = sorted(set(service_servers) | set(service_clients))
        for srv in all_services:
            health_services.append(
                {
                    "name": srv,
                    "critical": False,
                    "timeout_sec": 1.0,
                    "types": sorted(service_types.get(srv, set())),
                    "expected_servers": sorted(service_servers.get(srv, set())),
                    "expected_clients": sorted(service_clients.get(srv, set())),
                    "externally_provided": bool(service_clients.get(srv)) and not bool(service_servers.get(srv)),
                }
            )

        return {
            "meta": {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "workspace": str(self.workspace),
                "entry_launch": path_label(launch_entrypoint, self.workspace),
                "analysis_type": "static",
                "notes": [
                    "Static extraction cannot prove runtime success, only expected wiring from code.",
                    "Conditional actions keep condition expressions; enable flags to decide scenario.",
                    "Dynamic topic/service names are marked as <dynamic:...> and need manual review.",
                ],
            },
            "launch_arguments": launch_args,
            "includes": includes,
            "unresolved_includes": unresolved,
            "nodes": node_records,
            "health_template": {
                "nodes": health_nodes,
                "topics": health_topics,
                "services": health_services,
            },
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract expected ROS2 system contracts from launch and node code.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace root containing src/, install/, build/ (default: current directory).",
    )
    parser.add_argument(
        "--launch",
        type=Path,
        required=True,
        help="Launch file path (relative to workspace or absolute).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output YAML file path. Default: files/system_health/<launch_stem>_expected.yaml",
    )
    return parser.parse_args()


def resolve_path(workspace: Path, p: Path) -> Path:
    return p if p.is_absolute() else (workspace / p)


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    launch_file = resolve_path(workspace, args.launch).resolve()
    if not launch_file.exists():
        raise SystemExit(f"Launch file not found: {launch_file}")

    output = args.output
    if output is None:
        out_dir = workspace / "files" / "system_health"
        output = out_dir / f"{launch_file.stem}_expected.yaml"
    output = resolve_path(workspace, output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    extractor = StaticSystemExtractor(workspace)
    result = extractor.analyze(launch_file)

    yaml_text = "\n".join(to_yaml(result)) + "\n"
    output.write_text(yaml_text, encoding="utf-8")
    print(f"[ok] generated: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
