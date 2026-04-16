# system_health_tools

Generic ROS2 tools for extracting expected system contracts and checking live runtime health.

## What This Package Provides

- Static extraction from launch + node code:
  - `extract_expected_system`
- Runtime snapshot merge to final health config:
  - `build_system_health_from_runtime`
  - `build_health` (alias)
- Online health publisher:
  - `system_health_monitor`
- Operator terminal checklist:
  - `system_health_cli`
- RViz text marker output:
  - `system_health_rviz`

## Build

```bash
cd /path/to/ws
colcon build --packages-select system_health_tools
source install/setup.bash
```

## Quick Start

1. Extract expected contract from your robot launch:

```bash
ros2 run system_health_tools extract_expected_system \
  --workspace /path/to/ws \
  --launch src/my_robot_bringup/launch/robot.launch.py \
  --output ~/system_health/expected.yaml
```

2. Build runtime health YAML from live graph:

```bash
ros2 run system_health_tools build_health \
  --capture \
  --expected ~/system_health/expected.yaml \
  --output ~/system_health/system_health_real_robot.yaml \
  --scenario real_robot
```

3. Start monitor:

```bash
ros2 run system_health_tools system_health_monitor --ros-args \
  -p config_file:=~/system_health/system_health_real_robot.yaml
```

4. Show checklist in terminal:

```bash
ros2 run system_health_tools system_health_cli
```

Useful CLI modes:

```bash
ros2 run system_health_tools system_health_cli --watch --clear
ros2 run system_health_tools system_health_cli --watch --clear --only-problems
ros2 run system_health_tools system_health_cli --no-color
```

Include ROS system interfaces (optional):

```bash
ros2 run system_health_tools build_health \
  --capture \
  --expected ~/system_health/expected.yaml \
  --include-system-interfaces
```

## What Is Checked

Checks are driven by entries in `health_template.nodes/topics/services` from the generated runtime YAML.

- Nodes:
  - present in ROS graph (`node is active`)
  - missing node (`node not found in graph`)
- Topics:
  - topic exists in graph (`topic is present`)
  - message type match (`topic type mismatch`)
  - optional freshness (`max_age_sec`) -> stale detection
  - optional rate (`min_rate_hz`) -> low-rate detection
- Services:
  - service exists in graph (`service is present`)
  - service type match (`service type mismatch`)

Severity rule:

- `critical: true` -> failures are `ERROR`
- `critical: false` -> failures are `WARN`

## Failure Reason In Output

Yes. In CLI output, non-OK rows include a reason in parentheses, for example:

- `(... node not found in graph)`
- `(... topic type mismatch)`
- `(... stale topic data: age=... > max_age_sec=...)`
- `(... topic rate too low: ... < min_rate_hz=...)`
- `(... service not found in graph)`

## One-Command Operator Report

If you use the helper script from workspace root:

- `scripts/system_health.sh`

then one-shot full report is:

```bash
~/system_health.sh report
```

This starts a temporary monitor, prints full checklist (`OK/WARN/ERROR`), then stops it.

At the end of CLI report you also get runtime counts summary:

- `nodes:user=...,system=...,total=...`
- `topics:user=...,system=...,total=...`
- `services:user=...,system=...,total=...`

CLI also prints:

- `health-score X/Y OK (Z%)` at top and bottom of the report
- grouped problem sections:
  - `root-causes:` primary failing checks
  - `dependent-issues:` checks likely failing as a consequence (for example missing topic/service because producer/server node is missing)

## ROS1 + ROS2 Bridge Notes

- This package observes only the ROS2 graph.
- If ROS1 data is bridged correctly, bridged topics/services are visible in ROS2 and can be checked.
- Missing bridge mappings appear as missing ROS2 runtime entities (reported by monitor/CLI).
