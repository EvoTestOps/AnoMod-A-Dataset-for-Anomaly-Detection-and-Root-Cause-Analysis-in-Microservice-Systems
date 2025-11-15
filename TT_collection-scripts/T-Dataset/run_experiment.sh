#!/usr/bin/env bash

# Color definitions
RED='\\033[0;31m'
GREEN='\\033[0;32m'
YELLOW='\\033[1;33m'
BLUE='\\033[0;34m'
CYAN='\\033[0;36m'
NC='\\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

# This script is intended to be called by run_all_experiments.sh or manually for a single run.
# It orchestrates the setup, chaos injection, test execution, and data collection for one experiment.

set -o pipefail

# Train-Ticket end-to-end experiment orchestrator
# - Resets and deploys the system
# - Waits for pods to be ready
# - Starts chaos (Chaos Mesh for Lv_S/Lv_D/Lv_P, ChaosBlade for Lv_C)
# - Runs one collection, chosen by --api (py or evomaster)
# - Stops chaos and performs cleanup

########################################
# Config
########################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
NAMESPACE="${NAMESPACE:-default}"
TRAIN_TICKET_DIR="${TRAIN_TICKET_DIR:-${PROJECT_ROOT}/train-ticket}"
CHAOS_EXP_DIR="${CHAOS_EXP_DIR:-${PROJECT_ROOT}/chaos-experiments}"
CHAOSBLADE_DIR="${CHAOSBLADE_DIR:-${PROJECT_ROOT}/chaosblade/chaosblade-1.7.5-linux_amd64}"
TDATASET_DIR="${TDATASET_DIR:-${SCRIPT_DIR}}"
COLLECT_SCRIPT="${COLLECT_SCRIPT:-${TDATASET_DIR}/collect_all_modalities.sh}"
TRACE_SIZE_LIMIT="${TRACE_SIZE_LIMIT:-5000}"
TRACE_LOOKBACK_HOURS="${TRACE_LOOKBACK_HOURS:-24}"
TRACE_WORKER_COUNT="${TRACE_WORKER_COUNT:-32}"
EVOMASTER_HOME="${EVOMASTER_HOME:-${PROJECT_ROOT}/Evomaster}"
EVOMASTER_RUNS_DIR="${EVOMASTER_RUNS_DIR:-${EVOMASTER_HOME}/runs}"
EVOMASTER_SPEC="${EVOMASTER_SPEC:-${TRAIN_TICKET_DIR}/specs/evolved_merge_v4/combined/v3.5/combined-all-v3.5.json}"
EVOMASTER_JAR="${EVOMASTER_JAR:-${EVOMASTER_HOME}/evomaster.jar}"
EVOMASTER_HEADER_AUTH="${EVOMASTER_HEADER_AUTH:-Authorization: Bearer {AUTH_TOKEN}}"

########################################
# Helpers
########################################
log() { printf "[%s] %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || { echo "ERROR: '$cmd' is required but not found in PATH" >&2; exit 1; }
}

usage() {
  cat <<EOF
Usage: $0 --name EXPERIMENT_NAME [--api em]

Required:
  --name   Experiment name (e.g., Lv_D_TRANSACTION_timeout)

Optional:
  --api    Trigger source (default: em). Currently only 'em' (EvoMaster) is supported.

Notes:
  - Lv_C* experiment names use ChaosBlade (code-level injections)
  - Lv_S*/Lv_D*/Lv_P* use Chaos Mesh with <name>.yaml in ${CHAOS_EXP_DIR}
  - Uses EvoMaster for traffic generation
EOF
}

EXP_NAME=""
API_MODE="em"  # Default to EvoMaster only
EVOMASTER_TEST_PATH=""  # Path to EvoMaster test file (optional)
TEST_ITERATIONS=1  # Number of times to execute the test file (default: 1)
MASTER_TESTS=""  # Space-separated paths to master test files (optional, deprecated)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      EXP_NAME="$2"; shift 2 ;;
    --api)
      API_MODE="$2"; shift 2 ;;
    --evomaster-test)
      EVOMASTER_TEST_PATH="$2"; shift 2 ;;
    --test-file)
      EVOMASTER_TEST_PATH="$2"; shift 2 ;;
    --test-iterations)
      TEST_ITERATIONS="$2"; shift 2 ;;
    --master-tests)
      MASTER_TESTS="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "${EXP_NAME}" ]]; then
  echo "ERROR: --name is required" >&2
  usage
  exit 1
fi

# Only support EvoMaster now
if [[ "${API_MODE}" != "em" && "${API_MODE}" != "EM" ]]; then
  echo "ERROR: Only --api em is currently supported" >&2
  usage
  exit 1
fi
API_MODE="em"

########################################
# Pre-flight checks
########################################
require_cmd kubectl
require_cmd make
require_cmd python3

if [[ ! -x "${COLLECT_SCRIPT}" ]]; then
  echo "ERROR: Collector script not found or not executable at ${COLLECT_SCRIPT}" >&2
  exit 1
fi

if [[ ! -d "${TRAIN_TICKET_DIR}" ]]; then
  echo "ERROR: Train-Ticket dir not found at ${TRAIN_TICKET_DIR}" >&2
  exit 1
fi

########################################
# K8s readiness
########################################
wait_for_pods_ready() {
  local ns="$1"
  log "Waiting for pods in namespace '$ns' to be Ready..."
  
  # First, show current pod status
  log "Current pod status:"
  kubectl get pods -n "$ns" 2>/dev/null | cat || true
  
  # Primary: kubectl wait (covers Deployments/StatefulSets pods)
  if ! kubectl wait --for=condition=Ready pods --all -n "$ns" --timeout=900s 2>/dev/null; then
    log "kubectl wait did not fully succeed, falling back to polling..."
  fi

  # Fallback: poll for all pods to be Running with READY column showing N/N
  local start_ts=$(date +%s)
  local timeout_s=3600
  local check_count=0
  declare -A not_ready_since=()
  
  while true; do
    # Get all pods (excluding Completed jobs)
    local pod_status=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -v "Completed" || echo "")
    
    if [[ -z "$pod_status" ]]; then
      log "No pods found yet, waiting..."
      sleep 10
      continue
    fi
    
    # Check for pods in CrashLoopBackOff or Error state for extended time
    local crashed_pods=$(echo "$pod_status" | awk '{
      if ($3 == "CrashLoopBackOff" || $3 == "Error") print $1
    }' || echo "")
    
    if [[ -n "$crashed_pods" ]]; then
      log "WARNING: Found pods in crashed state:"
      echo "$crashed_pods" | while read -r pod; do
        if [[ -n "$pod" ]]; then
          log "  - $pod is in crashed state, attempting to delete..."
          kubectl delete pod "$pod" -n "$ns" --force --grace-period=0 2>/dev/null || true
        fi
      done
      sleep 10
      continue
    fi
    
    # Count pods not in Running state or not fully ready (READY column N/M where N!=M)
    local not_ready=$(echo "$pod_status" | awk '{
      split($2, ready, "/");
      if ($3 != "Running" || ready[1] != ready[2]) print $1
    }' | wc -l)
    
    if [[ "$not_ready" -eq 0 ]]; then
      log "All pods are Running and Ready!"
      kubectl get pods -n "$ns" | cat
      break
    fi
    
    # Log status every 30 seconds
    if (( check_count % 3 == 0 )); then
      log "Still waiting for $not_ready pod(s) to be ready..."
      echo "$pod_status" | awk '{if ($3 != "Running" || $2 !~ /^([0-9]+)\/\1$/) print "  - " $1 ": " $3 " " $2}'
    fi
    
    # Track pods that have been running but not ready for too long
    while read -r pod_line; do
      [[ -z "$pod_line" ]] && continue
      local pod_name ready_col status_col
      pod_name=$(awk '{print $1}' <<< "$pod_line")
      ready_col=$(awk '{print $2}' <<< "$pod_line")
      status_col=$(awk '{print $3}' <<< "$pod_line")
      
      # Skip pods that are already ready
      if [[ "$status_col" == "Running" && "$ready_col" =~ ^([0-9]+)/\1$ ]]; then
        unset 'not_ready_since[$pod_name]'
        continue
      fi
      
      # Only force-restart pods that are stuck in Running state with readiness failures
      if [[ "$status_col" == "Running" && "$ready_col" =~ ^([0-9]+)/([0-9]+)$ ]]; then
        local ready_now=${BASH_REMATCH[1]}
        local ready_total=${BASH_REMATCH[2]}
        if (( ready_total > 0 && ready_now < ready_total )); then
          local first_seen=${not_ready_since[$pod_name]:-0}
          if (( first_seen == 0 )); then
            not_ready_since[$pod_name]=$(date +%s)
          else
            local waited=$(( $(date +%s) - first_seen ))
            if (( waited > 180 )); then
              log "Pod $pod_name has been Running but not Ready for ${waited}s, deleting to trigger restart..."
              kubectl delete pod "$pod_name" -n "$ns" --force --grace-period=0 2>/dev/null || true
              unset 'not_ready_since[$pod_name]'
              sleep 5
              continue 2
            fi
          fi
        fi
      else
        unset 'not_ready_since[$pod_name]'
      fi
    done <<< "$pod_status"
    
    if (( $(date +%s) - start_ts > timeout_s )); then
      echo "ERROR: Timed out waiting for pods to be Ready in namespace '$ns'" >&2
      kubectl get pods -n "$ns" -o wide | cat
      exit 1
    fi
    
    check_count=$((check_count + 1))
    sleep 10
  done
}

########################################
# Chaos control
########################################
CHAOS_KIND=""        # mesh|blade|none
CHAOS_RESOURCE=""    # yaml path or blade UID

infer_blade_action_from_name() {
  # Heuristic mapping based on EXP_NAME keywords
  local name_lc
  name_lc="$(echo -n "$EXP_NAME" | tr 'A-Z' 'a-z')"
  if [[ "$name_lc" == *"security"* ]]; then
    echo "security"
  elif [[ "$name_lc" == *"exception"* || "$name_lc" == *"throw"* ]]; then
    echo "exception"
  elif [[ "$name_lc" == *"travel"* || "$name_lc" == *"trip"* || "$name_lc" == *"detail"* ]]; then
    echo "travel"
  else
    echo "exception"  # default
  fi
}

start_chaos() {
    local exp_name="$1"
    local chaos_type="$2"
    
    log "Starting chaos experiment: $exp_name ($chaos_type)"

    # No chaos for normal run
    if [ "$chaos_type" = "none" ]; then
        log "Skipping chaos injection for normal run"
        return 0
    fi

    if [ "$chaos_type" = "blade" ]; then
        cd "$CHAOSBLADE_DIR" || { log_error "Cannot cd to ChaosBlade dir"; return 1; }
        
        local output uid
        
        # Infer action from experiment name
        if [[ $exp_name == *"security_check"* ]]; then
            # This fault makes the security check method return a failure response immediately.
            # We target the pod, then the java process, then the specific class and method.
            local pod_name=$(get_pod_name ts-security-service)
            log "Injecting JVM fault into ts-security-service pod: $pod_name"
            
            output=$(./blade create k8s container-jvm return \
                --classname "security.service.SecurityServiceImpl" \
                --methodname "check" \
                --value "new edu.fudan.common.util.Response(0, 'CHAOS_SECURITY_CHECK_FAILURE', null)" \
                --names "$pod_name" \
                --container-names "ts-security-service" \
                --process "java" \
                --namespace "$NAMESPACE" \
                --kubeconfig ~/.kube/config)

        elif [[ $exp_name == *"exception_injection"* ]]; then
            # This fault throws a generic exception when trying to create an order.
            # We target the 'create' method in the 'OrderServiceImpl' class of the 'ts-order-service'.
            local pod_name=$(get_pod_name ts-order-service)
            log "Injecting JVM fault into ts-order-service pod: $pod_name"

            output=$(./blade create k8s container-jvm throwCustomException \
                --classname "order.service.OrderServiceImpl" \
                --methodname "create" \
                --exception "java.lang.RuntimeException" \
                --exception-message "CHAOS_EXCEPTION_INJECTION" \
                --names "$pod_name" \
                --container-names "ts-order-service" \
                --process "java" \
                --namespace "$NAMESPACE" \
                --kubeconfig ~/.kube/config)

        elif [[ $exp_name == *"travel_detail"* ]]; then
            # This fault makes the travel detail lookup return null, simulating a failure.
            # We target the 'getTripAllDetailInfo' method in the 'TravelServiceImpl' class of the 'ts-travel-service'.
            local pod_name=$(get_pod_name ts-travel-service)
            log "Injecting JVM fault into ts-travel-service pod: $pod_name"

            output=$(./blade create k8s container-jvm return \
                --classname "travel.service.TravelServiceImpl" \
                --methodname "getTripAllDetailInfo" \
                --value "null" \
                --names "$pod_name" \
                --container-names "ts-travel-service" \
                --process "java" \
                --namespace "$NAMESPACE" \
                --kubeconfig ~/.kube/config)
                
        else
            log_error "Unknown ChaosBlade action for experiment name: $exp_name"
            return 1
        fi
        
        log "ChaosBlade command output: $output"

        # Try to extract UID from output (supports multiple JSON formats)
        # Format 1: {"code":200,"success":true,"result":"<uid>"}
        uid=$(echo "$output" | grep -Eo '"result"\\s*:\\s*"[^"]+"' | sed -E 's/.*"result"\\s*:\\s*"([^"]+)".*/\\1/' | head -n1 || true)
        # Format 2: {"Uid":"<uid>",...}
        if [[ -z "$uid" ]]; then
          uid=$(echo "$output" | grep -Eo '"Uid"\\s*:\\s*"[^"]+"' | sed -E 's/.*"Uid"\\s*:\\s*"([^"]+)".*/\\1/' | head -n1 || true)
        fi
        # Format 3: uid: <uid> (old text format)
        if [[ -z "$uid" ]]; then
            uid=$(echo "$output" | awk -F":" '/uid/ {gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2}' | head -n1)
        fi

        if [ -z "$uid" ]; then
            log_error "Failed to get UID from chaos experiment: $exp_name"
            log_error "ChaosBlade Output: $output"
            return 1
        fi
        
        # Save UID for cleanup
        CHAOS_UID="$uid"
        log "Chaos experiment started successfully with UID: $CHAOS_UID"

    elif [ "$chaos_type" = "mesh" ]; then
        cd "$CHAOS_EXP_DIR" || { log_error "Cannot cd to chaos exp dir"; return 1; }
        log "Starting Chaos Mesh experiment from: ${exp_name}.yaml"
        if [ -f "${exp_name}.yaml" ]; then
            ./start_chaos.sh "${exp_name}.yaml"
            log "Chaos Mesh experiment started."
        else
            log_error "Chaos Mesh yaml not found for: $exp_name"
            return 1
        fi
    else
        log_error "Unsupported chaos type: $chaos_type"
        return 1
    fi
}

stop_chaos() {
  if [[ "$CHAOS_KIND" == "blade" && -n "${CHAOS_RESOURCE}" ]]; then
    log "Stopping ChaosBlade experiment UID=${CHAOS_RESOURCE}"
    cd "${CHAOSBLADE_DIR}"
    ./blade destroy "${CHAOS_RESOURCE}" | cat || true
  elif [[ "$CHAOS_KIND" == "mesh" && -n "${CHAOS_RESOURCE}" ]]; then
    log "Stopping Chaos Mesh experiment defined in ${CHAOS_RESOURCE}"
    cd "${CHAOS_EXP_DIR}"
    ./stop_chaos.sh "$(basename "${CHAOS_RESOURCE}")" | cat || true
  else
    log "No chaos to stop or already cleaned."
  fi
}

cleanup() {
  # Always attempt to stop chaos when exiting
  stop_chaos || true
}
trap cleanup EXIT

########################################
# Helper: Restart Prometheus
########################################
restart_prometheus() {
  log "Restarting Prometheus to free up memory and ensure fresh metrics collection..."
  
  # Delete all prometheus pods (keep only Running ones to avoid deleting too many failed pods)
  local running_pods=$(kubectl get pods -n kube-system -l app=prometheus --field-selector=status.phase=Running --no-headers 2>/dev/null | awk '{print $1}' || true)
  
  if [[ -n "$running_pods" ]]; then
    log "Deleting running Prometheus pod(s)..."
    echo "$running_pods" | while IFS= read -r pod; do
      if [[ -n "$pod" ]]; then
        kubectl delete pod "$pod" -n kube-system --force --grace-period=0 2>/dev/null || true
      fi
    done
  fi
  
  # Also clean up failed prometheus pods to reduce clutter
  log "Cleaning up failed Prometheus pods..."
  kubectl delete pods -n kube-system -l app=prometheus --field-selector=status.phase!=Running --force --grace-period=0 2>/dev/null || true
  
  # Wait for new pod to be ready
  log "Waiting for Prometheus to restart and become ready (max 180s)..."
  local waited=0
  local max_wait=180
  while (( waited < max_wait )); do
    local ready_count=$(kubectl get pods -n kube-system -l app=prometheus --field-selector=status.phase=Running 2>/dev/null | grep -c "1/1" || echo "0")
    if (( ready_count > 0 )); then
      log "Prometheus is ready!"
      kubectl get pods -n kube-system -l app=prometheus | grep Running | cat
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
    if (( waited % 30 == 0 )); then
      log "Still waiting for Prometheus... (${waited}s/${max_wait}s)"
    fi
  done
  
  log_warn "Prometheus may not be fully ready yet, but continuing..."
  return 0
}

########################################
# 1) Restart system for new experiment
########################################
log "Resetting Train-Ticket deployment..."
cd "${TRAIN_TICKET_DIR}"
make reset-deploy Namespace="${NAMESPACE}" | cat

# Force cleanup any remaining pods/replicasets to prevent duplicates
log "Force cleaning any remaining resources..."
kubectl delete pods --all -n "${NAMESPACE}" --force --grace-period=0 --wait=false 2>/dev/null || true
kubectl delete replicasets --all -n "${NAMESPACE}" --force --grace-period=0 --wait=false 2>/dev/null || true

# Wait for all resources to be fully deleted
log "Waiting for resources to be fully deleted..."
local cleanup_wait=0
while [ $cleanup_wait -lt 60 ]; do
    local remaining_pods=$(kubectl get pods -n "${NAMESPACE}" --no-headers 2>/dev/null | wc -l || echo "0")
    if [ "$remaining_pods" -eq 0 ]; then
        log "All old pods deleted successfully"
        break
    fi
    sleep 2
    cleanup_wait=$((cleanup_wait + 2))
done

sleep 10

# Restart Prometheus to ensure fresh start and avoid OOM issues
restart_prometheus

log "Deploying Train-Ticket with tracing and monitoring..."
make deploy Namespace="${NAMESPACE}" DeployArgs="--with-tracing --with-monitoring" | cat

log "Waiting for all pods to become Ready..."
wait_for_pods_ready "${NAMESPACE}" 3600

# Add a delay before chaos injection to allow agents (like ChaosBlade) to initialize
log "Waiting 180 seconds for chaos agents and slow application startup..."
sleep 180

########################################
# 1.5) Generate EvoMaster tests if not provided (skip if using master tests)
########################################
if [[ -z "${EVOMASTER_TEST_PATH}" && -z "${MASTER_TESTS}" ]]; then
    log "EvoMaster test path not provided, generating new tests..."
    
    # Get node IP
    NODE_IP=$(kubectl get nodes -o wide | awk 'NR==2{print $6}')
    if [[ -z "$NODE_IP" ]]; then
        log "ERROR: Failed to get node IP"
        exit 1
    fi
    
    # Generate unique output folder
    TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
    OUTPUT_FOLDER="${EVOMASTER_RUNS_DIR}/${EXP_NAME}_${TIMESTAMP}"
    mkdir -p "$OUTPUT_FOLDER"
    
    RUN_ID="em-$(date +%Y%m%d%H%M%S)"
    EVOMASTER_SPEC="${EVOMASTER_SPEC}"
    EVOMASTER_JAR="${EVOMASTER_JAR}"
    
    log "Generating EvoMaster tests (this may take up to 10 minutes)..."
    log "Output folder: $OUTPUT_FOLDER"
    
    # Run EvoMaster command (don't fail on exit code, check output file instead)
    java -jar "$EVOMASTER_JAR" \
        --blackBox true \
        --bbTargetUrl "http://${NODE_IP}:30467" \
        --bbSwaggerUrl "file://${EVOMASTER_SPEC}" \
        --createTests true \
        --outputFolder "$OUTPUT_FOLDER" \
        --outputFormat PYTHON_UNITTEST \
        --advancedBlackBoxCoverage true \
        --maxTime 10m \
        --seed -1 \
        --header0 "$EVOMASTER_HEADER_AUTH" \
        --header1 "x-evomaster-run-id: ${RUN_ID}" \
        > "${OUTPUT_FOLDER}/evomaster_output.log" 2>&1 || true
    
    sleep 2
    
    EVOMASTER_TEST_PATH="${OUTPUT_FOLDER}/EvoMaster_successes_Test.py"
    if [[ ! -f "$EVOMASTER_TEST_PATH" ]]; then
        log "ERROR: Generated test file not found: $EVOMASTER_TEST_PATH"
        log "Contents of output folder:"
        ls -la "$OUTPUT_FOLDER" || true
        log "Last 20 lines of EvoMaster log:"
        tail -20 "${OUTPUT_FOLDER}/evomaster_output.log" || true
        exit 1
    fi
    
    # Verify EvoMaster completed successfully by checking log
    if ! grep -q "EvoMaster process has completed successfully" "${OUTPUT_FOLDER}/evomaster_output.log" 2>/dev/null; then
        log "WARNING: EvoMaster log doesn't show success message, but test file exists. Continuing..."
    fi
    
    log "EvoMaster test generation completed: $EVOMASTER_TEST_PATH"
fi

########################################
# 2) Start chaos experiment
########################################
start_chaos "${EXP_NAME}" "${API_MODE}"

########################################
# 3 & 4) Collect data using EvoMaster
########################################
log "Starting collection for EvoMaster-driven scenario: ${EXP_NAME}_em"

# Build collect command with optional EvoMaster test path or master tests
COLLECT_CMD="bash \"${COLLECT_SCRIPT}\" --name \"${EXP_NAME}_em\" --api evomaster --trace-size ${TRACE_SIZE_LIMIT} --trace-hours ${TRACE_LOOKBACK_HOURS} --trace-workers ${TRACE_WORKER_COUNT}"

if [[ -n "${MASTER_TESTS}" ]]; then
  # Using master test suite (5 test files) - deprecated, kept for compatibility
  COLLECT_CMD="${COLLECT_CMD} --master-tests \"${MASTER_TESTS}\""
  log "Using master test suite (5 test files)"
elif [[ -n "${EVOMASTER_TEST_PATH}" ]]; then
  # Using single EvoMaster test file with iterations
  COLLECT_CMD="${COLLECT_CMD} --test-file \"${EVOMASTER_TEST_PATH}\" --test-iterations ${TEST_ITERATIONS}"
  log "Using EvoMaster test: ${EVOMASTER_TEST_PATH} (${TEST_ITERATIONS} iterations)"
fi

eval "$COLLECT_CMD" | cat

########################################
# 5) Stop chaos (handled by trap as well)
########################################
stop_chaos

log "Experiment completed: ${EXP_NAME}_em"
