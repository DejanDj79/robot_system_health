#!/usr/bin/env bash
set -euo pipefail

WS="${WS:-$HOME/workspace/agar2_ws}"
LAUNCH_REL="${LAUNCH_REL:-src/agar_bringup/launch/agar_jetson_launch.py}"
EXPECTED="${EXPECTED:-$HOME/system_health/expected.yaml}"
RUNTIME="${RUNTIME:-$HOME/system_health/system_health_real_robot.yaml}"
RUNTIME_ALL="${RUNTIME_ALL:-$HOME/system_health/system_health_real_robot_all.yaml}"
SCENARIO_DEFAULT="${SCENARIO_DEFAULT:-real_robot}"
ROS_SETUP="${ROS_SETUP:-}"

detect_ros_setup() {
  if [ -n "$ROS_SETUP" ] && [ -f "$ROS_SETUP" ]; then
    echo "$ROS_SETUP"
    return
  fi
  if [ -f "$HOME/workspace/ros2_humble/install/setup.bash" ]; then
    echo "$HOME/workspace/ros2_humble/install/setup.bash"
    return
  fi
  if [ -f "/opt/ros/humble/setup.bash" ]; then
    echo "/opt/ros/humble/setup.bash"
    return
  fi
  echo ""
}

ensure_env() {
  unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION ROS_MASTER_URI ROS_IP ROS_HOSTNAME ROS_PACKAGE_PATH ROS_ROOT || true

  ROS_SETUP_PATH="$(detect_ros_setup)"
  if [ -z "$ROS_SETUP_PATH" ]; then
    echo "[error] Ne mogu da nadjem ROS2 setup.bash"
    exit 2
  fi

  set +u
  source "$ROS_SETUP_PATH"
  set -u

  WS_EXPANDED="$(eval echo "$WS")"
  if [ ! -d "$WS_EXPANDED" ]; then
    echo "[error] Workspace ne postoji: $WS_EXPANDED"
    exit 2
  fi
  cd "$WS_EXPANDED"

  if [ ! -f "install/setup.bash" ]; then
    echo "[error] Nedostaje install/setup.bash. Prvo build paketa."
    exit 2
  fi

  set +u
  source install/setup.bash
  set -u

  mkdir -p "$HOME/system_health"
}

run_monitor_bg() {
  local monitor_log="$1"
  local config_file="$2"
  local diag_ns="$3"
  local node_name="$4"
  ros2 run system_health_tools system_health_monitor --ros-args \
    -p config_file:="$config_file" \
    -p diagnostics_ns:="$diag_ns" \
    -r __node:="$node_name" > "$monitor_log" 2>&1 &
  echo $!
}

run_report() {
  local config_file="$1"
  local cli_prefix="$2"
  local diag_ns="$3"
  local node_name="$4"
  MON_LOG="/tmp/system_health_monitor_$$.log"
  MON_PID="$(run_monitor_bg "$MON_LOG" "$config_file" "$diag_ns" "$node_name")"
  sleep 2
  set +e
  ros2 run system_health_tools system_health_cli --prefix "$cli_prefix"
  CLI_RC=$?
  kill "$MON_PID" 2>/dev/null || true
  wait "$MON_PID" 2>/dev/null || true
  set -e
  exit "$CLI_RC"
}

cmd="${1:-help}"
arg="${2:-}"

case "$cmd" in
  build)
    ensure_env
    colcon build --packages-select system_health_tools
    ;;
  expected)
    ensure_env
    launch_path="${arg:-$LAUNCH_REL}"
    ros2 run system_health_tools extract_expected_system \
      --workspace "$WS_EXPANDED" \
      --launch "$launch_path" \
      --output "$EXPECTED"
    ;;
  runtime)
    ensure_env
    scenario="${arg:-$SCENARIO_DEFAULT}"
    ros2 run system_health_tools build_health \
      --capture \
      --expected "$EXPECTED" \
      --output "$RUNTIME" \
      --scenario "$scenario"
    ;;
  runtime-all)
    ensure_env
    scenario="${arg:-$SCENARIO_DEFAULT}"
    ros2 run system_health_tools build_health \
      --capture \
      --expected "$EXPECTED" \
      --output "$RUNTIME_ALL" \
      --scenario "$scenario" \
      --include-system-interfaces
    ;;
  runtime-both)
    ensure_env
    scenario="${arg:-$SCENARIO_DEFAULT}"
    ros2 run system_health_tools build_health \
      --capture \
      --expected "$EXPECTED" \
      --output "$RUNTIME" \
      --scenario "$scenario"
    ros2 run system_health_tools build_health \
      --capture \
      --expected "$EXPECTED" \
      --output "$RUNTIME_ALL" \
      --scenario "$scenario" \
      --include-system-interfaces
    ;;
  monitor)
    ensure_env
    ros2 run system_health_tools system_health_monitor --ros-args -p config_file:="$RUNTIME"
    ;;
  monitor-all)
    ensure_env
    ros2 run system_health_tools system_health_monitor --ros-args -p config_file:="$RUNTIME_ALL"
    ;;
  watch)
    ensure_env
    ros2 run system_health_tools system_health_cli --watch --clear --only-problems
    ;;
  watch-all)
    ensure_env
    ros2 run system_health_tools system_health_cli --watch --clear
    ;;
  once)
    ensure_env
    ros2 run system_health_tools system_health_cli --only-problems
    ;;
  once-all)
    ensure_env
    ros2 run system_health_tools system_health_cli
    ;;
  report)
    ensure_env
    run_report "$RUNTIME" "system_health_user" "/system_health_user" "system_health_monitor_report_user"
    ;;
  report-all)
    ensure_env
    run_report "$RUNTIME_ALL" "system_health_all" "/system_health_all" "system_health_monitor_report_all"
    ;;
  all)
    "$0" build
    "$0" expected
    "$0" runtime "${arg:-$SCENARIO_DEFAULT}"
    "$0" runtime-all "${arg:-$SCENARIO_DEFAULT}"
    ;;
  help|*)
    cat <<'HLP'
Usage: ~/system_health.sh <command> [arg]

Commands:
  build                 Build system_health_tools
  expected [launch_rel] Generate ~/system_health/expected.yaml from launch
  runtime [scenario]    Generate ~/system_health/system_health_real_robot.yaml
  runtime-all [scenario] Generate ~/system_health/system_health_real_robot_all.yaml
  runtime-both [scenario] Generate both runtime yaml files
  monitor               Run system_health_monitor in foreground
  monitor-all           Run system_health_monitor with runtime-all yaml
  watch                 Live terminal checklist (only WARN/ERR)
  watch-all             Live terminal checklist (all items)
  once                  One-shot checklist (only WARN/ERR)
  once-all              One-shot checklist (all items)
  report                One command report for runtime (your interfaces)
  report-all            One command report for runtime-all (with system interfaces)
  all [scenario]        build + expected + runtime + runtime-all
HLP
    ;;
esac
