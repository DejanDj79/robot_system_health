#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_WS_DEFAULT="$(cd "${SCRIPT_DIR}/.." && pwd)"

HOST=""
REMOTE_WS=""
LOCAL_WS="${LOCAL_WS_DEFAULT}"
ROS_SETUP="/opt/ros/humble/setup.bash"
SCENARIO="real_robot"
REMOTE_EXPECTED="~/system_health/expected.yaml"
REMOTE_SCRIPT_PATH="~/system_health.sh"
ROBOT_LAUNCH_REL="src/agar_bringup/launch/agar_jetson_launch.py"
EXPECTED_LOCAL=""

SKIP_BUILD=0
SKIP_GENERATE=0
SKIP_COPY_EXPECTED=1
SKIP_REMOTE_EXTRACT=0
SKIP_INSTALL_REMOTE_SCRIPT=0
DRY_RUN=0

usage() {
  cat <<EOF
Usage:
  $(basename "$0") --host <user@ip> --remote-ws <remote_ws_path> [options]

Required:
  --host                SSH target (example: nvidia@192.168.1.50)
  --remote-ws           Remote ROS2 workspace path (example: /home/nvidia/workspace/agar2_ws)

Options:
  --local-ws PATH       Local workspace path (default: ${LOCAL_WS_DEFAULT})
  --expected-local PATH Local *_expected.yaml to copy (optional; default: <local-ws>/files/system_health/agar_jetson_launch_expected.yaml)
  --remote-expected P   Remote expected yaml path (default: ${REMOTE_EXPECTED})
  --remote-script P     Remote helper script path (default: ${REMOTE_SCRIPT_PATH})
  --launch-rel PATH     Launch path relative to remote workspace (default: ${ROBOT_LAUNCH_REL})
  --scenario NAME       Scenario label for generated runtime yaml (default: ${SCENARIO})
  --ros-setup PATH      Remote ROS setup script (default: ${ROS_SETUP})
  --copy-expected       Copy expected yaml from local machine to robot (disabled by default)
  --skip-copy-expected  Do not copy expected yaml to robot (default behavior)
  --skip-remote-extract Skip generating expected yaml on robot from robot source
  --skip-install-remote-script  Do not create helper script in robot home
  --skip-build          Skip remote colcon build
  --skip-generate       Skip runtime yaml generation on robot
  --dry-run             Print commands without executing
  -h, --help            Show this help

What this script does:
  1) rsync src/system_health_tools to remote <remote-ws>/src/
  2) builds package on remote (colcon build --packages-select system_health_tools)
  3) generates expected yaml on robot from robot source (unless skipped)
  4) generates runtime yaml on robot
  5) creates remote helper script in robot home (~/system_health.sh)
EOF
}

run_cmd() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "[dry-run] $*"
  else
    # Use a single shell evaluation pass to avoid double-expansion bugs.
    bash -lc "$1"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:-}"
      shift 2
      ;;
    --remote-ws)
      REMOTE_WS="${2:-}"
      shift 2
      ;;
    --local-ws)
      LOCAL_WS="${2:-}"
      shift 2
      ;;
    --expected-local)
      EXPECTED_LOCAL="${2:-}"
      shift 2
      ;;
    --remote-expected)
      REMOTE_EXPECTED="${2:-}"
      shift 2
      ;;
    --remote-script)
      REMOTE_SCRIPT_PATH="${2:-}"
      shift 2
      ;;
    --launch-rel)
      ROBOT_LAUNCH_REL="${2:-}"
      shift 2
      ;;
    --scenario)
      SCENARIO="${2:-}"
      shift 2
      ;;
    --ros-setup)
      ROS_SETUP="${2:-}"
      shift 2
      ;;
    --copy-expected)
      SKIP_COPY_EXPECTED=0
      shift
      ;;
    --skip-copy-expected)
      SKIP_COPY_EXPECTED=1
      shift
      ;;
    --skip-remote-extract)
      SKIP_REMOTE_EXTRACT=1
      shift
      ;;
    --skip-install-remote-script)
      SKIP_INSTALL_REMOTE_SCRIPT=1
      shift
      ;;
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --skip-generate)
      SKIP_GENERATE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${HOST}" || -z "${REMOTE_WS}" ]]; then
  echo "Error: --host and --remote-ws are required."
  usage
  exit 1
fi

if [[ -z "${EXPECTED_LOCAL}" ]]; then
  EXPECTED_LOCAL="${LOCAL_WS}/files/system_health/agar_jetson_launch_expected.yaml"
fi

LOCAL_PACKAGE_DIR="${LOCAL_WS}/src/system_health_tools"
if [[ ! -d "${LOCAL_PACKAGE_DIR}" ]]; then
  echo "Error: local package dir not found: ${LOCAL_PACKAGE_DIR}"
  exit 1
fi

echo "[1/6] Sync system_health_tools package to robot..."
run_cmd "rsync -av --delete \"${LOCAL_PACKAGE_DIR}\" \"${HOST}:${REMOTE_WS}/src/\""

HAVE_EXPECTED=0
if [[ "${SKIP_COPY_EXPECTED}" -eq 0 ]]; then
  if [[ -n "${EXPECTED_LOCAL}" && -f "${EXPECTED_LOCAL}" ]]; then
    echo "[2/6] Copy expected yaml to robot..."
    run_cmd "ssh \"${HOST}\" \"mkdir -p \\\"\$(dirname '${REMOTE_EXPECTED}')\\\"\""
    run_cmd "scp \"${EXPECTED_LOCAL}\" \"${HOST}:${REMOTE_EXPECTED}\""
    HAVE_EXPECTED=1
  else
    echo "[warn] Expected yaml not found locally: ${EXPECTED_LOCAL}"
    echo "[warn] Continuing without copy."
  fi
else
  echo "[2/6] Skip copy expected yaml (--skip-copy-expected)."
fi

if [[ "${SKIP_BUILD}" -eq 0 ]]; then
  echo "[3/6] Build package on robot..."
  run_cmd "ssh -t \"${HOST}\" \"bash -lc '
    set -e
    unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION ROS_MASTER_URI ROS_IP ROS_HOSTNAME ROS_PACKAGE_PATH ROS_ROOT || true
    ROS_SETUP_EXPANDED=\$(eval echo \"${ROS_SETUP}\")
    REMOTE_WS_EXPANDED=\$(eval echo \"${REMOTE_WS}\")
    if [ -z \"\$ROS_SETUP_EXPANDED\" ]; then
      echo \"[error] ROS setup not found on robot: \$ROS_SETUP_EXPANDED\"
      exit 2
    fi
    if [ ! -f \"\$ROS_SETUP_EXPANDED\" ]; then
      echo \"[error] ROS setup not found on robot: \$ROS_SETUP_EXPANDED\"
      exit 2
    fi
    if [ -z \"\$REMOTE_WS_EXPANDED\" ]; then
      echo \"[error] Remote workspace not found on robot: \$REMOTE_WS_EXPANDED\"
      exit 2
    fi
    if [ ! -d \"\$REMOTE_WS_EXPANDED\" ]; then
      echo \"[error] Remote workspace not found on robot: \$REMOTE_WS_EXPANDED\"
      exit 2
    fi
    source \"\$ROS_SETUP_EXPANDED\"
    cd \"\$REMOTE_WS_EXPANDED\"
    colcon build --packages-select system_health_tools
  '\""
else
  echo "[3/6] Skip build on robot (--skip-build)."
fi

if [[ "${SKIP_REMOTE_EXTRACT}" -eq 0 ]]; then
  echo "[4/6] Generate expected yaml on robot from robot source..."
  run_cmd "ssh -t \"${HOST}\" \"bash -lc '
    set -e
    unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION ROS_MASTER_URI ROS_IP ROS_HOSTNAME ROS_PACKAGE_PATH ROS_ROOT || true
    ROS_SETUP_EXPANDED=\$(eval echo \"${ROS_SETUP}\")
    REMOTE_WS_EXPANDED=\$(eval echo \"${REMOTE_WS}\")
    if [ -z \"\$ROS_SETUP_EXPANDED\" ]; then
      echo \"[error] ROS setup not found on robot: \$ROS_SETUP_EXPANDED\"
      exit 2
    fi
    if [ ! -f \"\$ROS_SETUP_EXPANDED\" ]; then
      echo \"[error] ROS setup not found on robot: \$ROS_SETUP_EXPANDED\"
      exit 2
    fi
    if [ -z \"\$REMOTE_WS_EXPANDED\" ]; then
      echo \"[error] Remote workspace not found on robot: \$REMOTE_WS_EXPANDED\"
      exit 2
    fi
    if [ ! -d \"\$REMOTE_WS_EXPANDED\" ]; then
      echo \"[error] Remote workspace not found on robot: \$REMOTE_WS_EXPANDED\"
      exit 2
    fi
    source \"\$ROS_SETUP_EXPANDED\"
    REMOTE_EXPECTED_EXPANDED=\$(eval echo \"${REMOTE_EXPECTED}\")
    mkdir -p \\\"\$(dirname \\\"\$REMOTE_EXPECTED_EXPANDED\\\")\\\"
    cd \"\$REMOTE_WS_EXPANDED\"
    source install/setup.bash
    ros2 run system_health_tools extract_expected_system \\
      --workspace \"\$REMOTE_WS_EXPANDED\" \\
      --launch \"${ROBOT_LAUNCH_REL}\" \\
      --output \\\"\$REMOTE_EXPECTED_EXPANDED\\\"
  '\""
  HAVE_EXPECTED=1
else
  echo "[4/6] Skip remote expected generation (--skip-remote-extract)."
fi

if [[ "${SKIP_GENERATE}" -eq 0 ]]; then
  echo "[5/6] Generate runtime health yaml on robot..."
  if [[ "${HAVE_EXPECTED}" -eq 1 ]]; then
    run_cmd "ssh -t \"${HOST}\" \"bash -lc '
      set -e
      unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION ROS_MASTER_URI ROS_IP ROS_HOSTNAME ROS_PACKAGE_PATH ROS_ROOT || true
      ROS_SETUP_EXPANDED=\$(eval echo \"${ROS_SETUP}\")
      REMOTE_WS_EXPANDED=\$(eval echo \"${REMOTE_WS}\")
      if [ -z \"\$ROS_SETUP_EXPANDED\" ]; then
        echo \"[error] ROS setup not found on robot: \$ROS_SETUP_EXPANDED\"
        exit 2
      fi
      if [ ! -f \"\$ROS_SETUP_EXPANDED\" ]; then
        echo \"[error] ROS setup not found on robot: \$ROS_SETUP_EXPANDED\"
        exit 2
      fi
      if [ -z \"\$REMOTE_WS_EXPANDED\" ]; then
        echo \"[error] Remote workspace not found on robot: \$REMOTE_WS_EXPANDED\"
        exit 2
      fi
      if [ ! -d \"\$REMOTE_WS_EXPANDED\" ]; then
        echo \"[error] Remote workspace not found on robot: \$REMOTE_WS_EXPANDED\"
        exit 2
      fi
      source \"\$ROS_SETUP_EXPANDED\"
      REMOTE_EXPECTED_EXPANDED=\$(eval echo \"${REMOTE_EXPECTED}\")
      cd \"\$REMOTE_WS_EXPANDED\"
      source install/setup.bash
      ros2 run system_health_tools build_health --capture --scenario \"${SCENARIO}\" --expected \"\$REMOTE_EXPECTED_EXPANDED\"
    '\""
  else
    run_cmd "ssh -t \"${HOST}\" \"bash -lc '
      set -e
      unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION ROS_MASTER_URI ROS_IP ROS_HOSTNAME ROS_PACKAGE_PATH ROS_ROOT || true
      ROS_SETUP_EXPANDED=\$(eval echo \"${ROS_SETUP}\")
      REMOTE_WS_EXPANDED=\$(eval echo \"${REMOTE_WS}\")
      if [ -z \"\$ROS_SETUP_EXPANDED\" ]; then
        echo \"[error] ROS setup not found on robot: \$ROS_SETUP_EXPANDED\"
        exit 2
      fi
      if [ ! -f \"\$ROS_SETUP_EXPANDED\" ]; then
        echo \"[error] ROS setup not found on robot: \$ROS_SETUP_EXPANDED\"
        exit 2
      fi
      if [ -z \"\$REMOTE_WS_EXPANDED\" ]; then
        echo \"[error] Remote workspace not found on robot: \$REMOTE_WS_EXPANDED\"
        exit 2
      fi
      if [ ! -d \"\$REMOTE_WS_EXPANDED\" ]; then
        echo \"[error] Remote workspace not found on robot: \$REMOTE_WS_EXPANDED\"
        exit 2
      fi
      source \"\$ROS_SETUP_EXPANDED\"
      cd \"\$REMOTE_WS_EXPANDED\"
      source install/setup.bash
      ros2 run system_health_tools build_health --capture --scenario \"${SCENARIO}\"
    '\""
  fi
else
  echo "[5/6] Skip runtime yaml generation (--skip-generate)."
fi

if [[ "${SKIP_INSTALL_REMOTE_SCRIPT}" -eq 0 ]]; then
  echo "[6/6] Install helper script in robot home..."
  TMP_HELPER="$(mktemp)"
  cat > "${TMP_HELPER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail

ROS_SETUP="${ROS_SETUP}"
WS="${REMOTE_WS}"
LAUNCH_REL="${ROBOT_LAUNCH_REL}"
EXPECTED="\$HOME/system_health/expected.yaml"
RUNTIME="\$HOME/system_health/system_health_real_robot.yaml"
RUNTIME_ALL="\$HOME/system_health/system_health_real_robot_all.yaml"
SCENARIO_DEFAULT="${SCENARIO}"

ensure_env() {
  unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION ROS_MASTER_URI ROS_IP ROS_HOSTNAME ROS_PACKAGE_PATH ROS_ROOT || true
  set +u
  source "\$ROS_SETUP"
  set -u
  WS_EXPANDED=\$(eval echo "\$WS")
  cd "\$WS_EXPANDED"
  set +u
  source install/setup.bash
  set -u
  mkdir -p "\$HOME/system_health"
}

cmd="\${1:-help}"
arg="\${2:-}"

case "\$cmd" in
  build)
    ensure_env
    colcon build --packages-select system_health_tools
    ;;
  expected)
    ensure_env
    launch_path="\${arg:-\$LAUNCH_REL}"
    ros2 run system_health_tools extract_expected_system \\
      --workspace "\$WS_EXPANDED" \\
      --launch "\$launch_path" \\
      --output "\$EXPECTED"
    ;;
  runtime)
    ensure_env
    scenario="\${arg:-\$SCENARIO_DEFAULT}"
    ros2 run system_health_tools build_health --capture --expected "\$EXPECTED" --scenario "\$scenario"
    ;;
  runtime-all)
    ensure_env
    scenario="\${arg:-\$SCENARIO_DEFAULT}"
    ros2 run system_health_tools build_health --capture --expected "\$EXPECTED" --output "\$RUNTIME_ALL" --scenario "\$scenario" --include-system-interfaces
    ;;
  runtime-both)
    ensure_env
    scenario="\${arg:-\$SCENARIO_DEFAULT}"
    ros2 run system_health_tools build_health --capture --expected "\$EXPECTED" --output "\$RUNTIME" --scenario "\$scenario"
    ros2 run system_health_tools build_health --capture --expected "\$EXPECTED" --output "\$RUNTIME_ALL" --scenario "\$scenario" --include-system-interfaces
    ;;
  monitor)
    ensure_env
    ros2 run system_health_tools system_health_monitor --ros-args -p config_file:=\$RUNTIME
    ;;
  monitor-all)
    ensure_env
    ros2 run system_health_tools system_health_monitor --ros-args -p config_file:=\$RUNTIME_ALL
    ;;
  watch)
    ensure_env
    ros2 run system_health_tools system_health_cli --watch --clear --only-problems
    ;;
  report)
    ensure_env
    MON_LOG="/tmp/system_health_monitor_\$\$.log"
    ros2 run system_health_tools system_health_monitor --ros-args \\
      -p config_file:=\$RUNTIME \\
      -p diagnostics_ns:=/system_health_user \\
      -r __node:=system_health_monitor_report_user > "\$MON_LOG" 2>&1 &
    MON_PID=\$!
    sleep 2
    set +e
    ros2 run system_health_tools system_health_cli --prefix system_health_user
    CLI_RC=\$?
    kill "\$MON_PID" 2>/dev/null || true
    wait "\$MON_PID" 2>/dev/null || true
    set -e
    exit "\$CLI_RC"
    ;;
  report-all)
    ensure_env
    MON_LOG="/tmp/system_health_monitor_all_\$\$.log"
    ros2 run system_health_tools system_health_monitor --ros-args \\
      -p config_file:=\$RUNTIME_ALL \\
      -p diagnostics_ns:=/system_health_all \\
      -r __node:=system_health_monitor_report_all > "\$MON_LOG" 2>&1 &
    MON_PID=\$!
    sleep 2
    set +e
    ros2 run system_health_tools system_health_cli --prefix system_health_all
    CLI_RC=\$?
    kill "\$MON_PID" 2>/dev/null || true
    wait "\$MON_PID" 2>/dev/null || true
    set -e
    exit "\$CLI_RC"
    ;;
  all)
    "\$0" build
    "\$0" expected
    "\$0" runtime "\${arg:-\$SCENARIO_DEFAULT}"
    "\$0" runtime-all "\${arg:-\$SCENARIO_DEFAULT}"
    ;;
  help|*)
    cat <<'HLP'
Usage: ~/system_health.sh <command> [arg]

Commands:
  build                 Build system_health_tools package in workspace
  expected [launch_rel] Generate ~/system_health/expected.yaml from robot source launch
  runtime [scenario]    Generate ~/system_health/system_health_real_robot.yaml
  runtime-all [scenario] Generate ~/system_health/system_health_real_robot_all.yaml
  runtime-both [scenario] Generate both runtime yaml files
  monitor               Run system_health_monitor using runtime yaml
  monitor-all           Run system_health_monitor using runtime-all yaml
  watch                 Run terminal checklist watcher
  report                One-shot full checklist for runtime yaml
  report-all            One-shot full checklist for runtime-all yaml
  all [scenario]        build + expected + runtime + runtime-all
HLP
    ;;
esac
EOF
  run_cmd "scp \"${TMP_HELPER}\" \"${HOST}:${REMOTE_SCRIPT_PATH}\""
  run_cmd "ssh \"${HOST}\" \"chmod +x \\\"\$(eval echo '${REMOTE_SCRIPT_PATH}')\\\"\""
  rm -f "${TMP_HELPER}"
else
  echo "[6/6] Skip remote helper script install (--skip-install-remote-script)."
fi

cat <<EOF

Done.
Remote helper script:
  ${REMOTE_SCRIPT_PATH}

Useful SSH commands:
  ssh -t ${HOST} "~/system_health.sh help"
  ssh -t ${HOST} "~/system_health.sh all ${SCENARIO}"
  ssh -t ${HOST} "~/system_health.sh monitor"
  ssh -t ${HOST} "~/system_health.sh watch"

EOF
