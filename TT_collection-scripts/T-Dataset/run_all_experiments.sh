#!/usr/bin/env bash

# =============================================================================
# Automated Multimodal Data Collection Script for Train-Ticket (Kubernetes)
# Purpose: Automate system reset, anomaly injection, testing, and data collection
# =============================================================================

set -eE  # Exit on error and inherit ERR trap in functions
set -o pipefail  # Pipe failure causes script to fail

# Error trap function
error_trap() {
    local exit_code=$?
    local line_number=$1
    
    set +e  # Temporarily disable error handling for cleanup
    
    log_error "Script failed at line $line_number with exit code $exit_code"
    log_error "Cleaning up before exit..."
    
    # Attempt to cleanup any active chaos experiments
    if [ -n "$CURRENT_CHAOS_EXP" ]; then
        log_info "Attempting to stop chaos experiment: $CURRENT_CHAOS_EXP"
        cleanup_chaos "$CURRENT_CHAOS_EXP" "$CURRENT_CHAOS_TYPE"
    fi
    
    exit $exit_code
}

trap 'error_trap $LINENO' ERR

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Logging functions (output to stderr so they don't interfere with command substitution)
log_info() {
    echo -e "${GREEN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1" >&2
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1" >&2
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1" >&2
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1" >&2
}

log_section() {
    echo -e "\n${CYAN}========================================${NC}" >&2
    echo -e "${CYAN}$1${NC}" >&2
    echo -e "${CYAN}========================================${NC}\n" >&2
}

# Configuration variables (override via environment variables when needed)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
TDATASET_DIR="${TDATASET_DIR:-${SCRIPT_DIR}}"
NAMESPACE="${NAMESPACE:-default}"
TRAIN_TICKET_DIR="${TRAIN_TICKET_DIR:-${PROJECT_ROOT}/train-ticket}"
CHAOS_EXP_DIR="${CHAOS_EXP_DIR:-${PROJECT_ROOT}/chaos-experiments}"
CHAOSBLADE_DIR="${CHAOSBLADE_DIR:-${PROJECT_ROOT}/chaosblade/chaosblade-1.7.5-linux_amd64}"
RUN_EXP_SCRIPT="${RUN_EXP_SCRIPT:-${TDATASET_DIR}/run_experiment.sh}"

# EvoMaster test configuration
EVOMASTER_HOME="${EVOMASTER_HOME:-${PROJECT_ROOT}/Evomaster}"
EVOMASTER_JAR="${EVOMASTER_JAR:-${EVOMASTER_HOME}/evomaster.jar}"
EVOMASTER_SPEC="${EVOMASTER_SPEC:-${TRAIN_TICKET_DIR}/specs/evolved_merge_v4/combined/v3.5/combined-all-v3.5.json}"
EVOMASTER_BASE_OUTPUT="${EVOMASTER_BASE_OUTPUT:-${EVOMASTER_HOME}/runs}"
EVOMASTER_TIME_LIMIT="10m"  # Time limit for test generation

# EvoMaster test file configuration (reuse existing test file multiple times to generate more traces)
EVOMASTER_TEST_FILE="${EVOMASTER_TEST_FILE:-${EVOMASTER_HOME}/runs/auth_fixed_10m/EvoMaster_successes_Test.py}"
EVOMASTER_TEST_ITERATIONS=10   # Default iterations per experiment (adjust for desired trace volume)
TRACE_SIZE_LIMIT=0             # 0 means collect every available trace for the experiment
TRACE_LOOKBACK_HOURS=24        # Trace query window
TRACE_WORKER_COUNT=32          # Worker threads for fetching span details
EVOMASTER_HEADER_AUTH="${EVOMASTER_HEADER_AUTH:-Authorization: Bearer {AUTH_TOKEN}}"

# Wait time configurations
POD_STARTUP_WAIT=180  # Initial wait after deployment
INTER_EXPERIMENT_WAIT=300  # Wait between experiments

# Tracking variables
CURRENT_CHAOS_EXP=""
CURRENT_CHAOS_TYPE=""  # mesh|blade

# =============================================================================
# Function: Check prerequisites
# =============================================================================
check_prerequisites() {
    log_section "Checking Prerequisites"
    
    local all_ok=true
    
    # Check directories
    if [ ! -d "$TRAIN_TICKET_DIR" ]; then
        log_error "Train-Ticket directory not found: $TRAIN_TICKET_DIR"
        all_ok=false
    else
        log_info "✓ Train-Ticket directory found"
    fi
    
    if [ ! -d "$CHAOS_EXP_DIR" ]; then
        log_error "Chaos experiments directory not found: $CHAOS_EXP_DIR"
        all_ok=false
    else
        log_info "✓ Chaos experiments directory found"
    fi
    
    if [ ! -d "$CHAOSBLADE_DIR" ]; then
        log_error "ChaosBlade directory not found: $CHAOSBLADE_DIR"
        all_ok=false
    else
        log_info "✓ ChaosBlade directory found"
    fi
    
    # Check run_experiment.sh script
    if [ ! -x "$RUN_EXP_SCRIPT" ]; then
        log_error "run_experiment.sh not found or not executable: $RUN_EXP_SCRIPT"
        all_ok=false
    else
        log_info "✓ run_experiment.sh found"
    fi
    
    # Check kubectl
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl command not found"
        all_ok=false
    else
        log_info "✓ kubectl available"
    fi
    
    # Check make
    if ! command -v make &> /dev/null; then
        log_error "make command not found"
        all_ok=false
    else
        log_info "✓ make available"
    fi
    
    # Check python3
    if ! command -v python3 &> /dev/null; then
        log_error "python3 command not found"
        all_ok=false
    else
        log_info "✓ python3 available"
    fi
    
    if [ "$all_ok" = false ]; then
        log_error "Prerequisites check failed. Please fix the above issues."
        exit 1
    fi
    
    log_info "All prerequisites satisfied!"
}

# =============================================================================
# Function: Cleanup any existing chaos experiments
# =============================================================================
cleanup_all_chaos() {
    (
        set +e
        set +o pipefail
        
        log_step "Cleaning up any existing chaos experiments..."
        
        # Clean up Chaos Mesh experiments
        log_info "Checking for active Chaos Mesh experiments..."
        local chaos_resources=$(kubectl get networkchaos,podchaos,stresschaos,iochaos -n chaos-mesh --no-headers 2>/dev/null | awk '{print $1}' || true)
        
        if [ -n "$chaos_resources" ]; then
            log_warn "Found active Chaos Mesh experiments, deleting them..."
            echo "$chaos_resources" | while IFS= read -r resource; do
                if [ -n "$resource" ]; then
                    log_info "Deleting: $resource"
                    kubectl delete "$resource" -n chaos-mesh 2>/dev/null || true
                fi
            done
            sleep 5
        else
            log_info "No active Chaos Mesh experiments found"
        fi
        
        # Clean up ChaosBlade experiments
        log_info "Checking for active ChaosBlade experiments..."
        cd "$CHAOSBLADE_DIR" || return 0
        
        local active_uids=$(./blade status --type create 2>/dev/null | grep -B 5 '"Status": "Success"' | grep '"Uid"' | grep -oP '"Uid":\s*"\K[^"]+' || true)
        
        if [ -n "$active_uids" ]; then
            log_warn "Found active ChaosBlade experiments, destroying them..."
            echo "$active_uids" | while IFS= read -r uid; do
                if [ -n "$uid" ]; then
                    log_info "Destroying ChaosBlade experiment: $uid"
                    ./blade destroy "$uid" 2>/dev/null || true
                fi
            done
            sleep 3
        else
            log_info "No active ChaosBlade experiments found"
        fi
        
        log_info "Chaos cleanup completed"
        return 0
    )
    
    return 0
}

# =============================================================================
# Function: Cleanup specific chaos experiment
# Parameters: $1 = experiment name, $2 = chaos type (mesh|blade)
# =============================================================================
cleanup_chaos() {
    local exp_name="$1"
    local chaos_type="$2"
    
    (
        set +e
        set +o pipefail
        
        log_step "Cleaning up chaos experiment: $exp_name (type: $chaos_type)"
        
        if [ "$chaos_type" = "mesh" ]; then
            # Stop Chaos Mesh experiment using stop_chaos.sh
            cd "$CHAOS_EXP_DIR" || return 0
            if [ -f "${exp_name}.yaml" ]; then
                ./stop_chaos.sh "${exp_name}.yaml" 2>/dev/null || true
                sleep 5
                log_info "Chaos Mesh experiment stopped"
            fi
        elif [ "$chaos_type" = "blade" ]; then
            # ChaosBlade cleanup is handled within run_experiment.sh
            log_info "ChaosBlade cleanup will be handled by run_experiment.sh"
        fi
        
        return 0
    )
    
    return 0
}

# =============================================================================
# Function: Cleanup residual files to free disk space
# =============================================================================
cleanup_residual_files() {
    (
        set +e
        set +o pipefail
        
        log_step "Cleaning up residual files to free disk space..."
        
        # Clean Docker system (containers, networks, build cache - but NOT images)
        log_info "Cleaning Docker containers and networks (preserving images)..."
        docker container prune -f 2>/dev/null || true
        docker network prune -f 2>/dev/null || true
        docker volume prune -f 2>/dev/null || true
        
        # Note: We do NOT prune images to avoid Docker Hub rate limiting
        # Images are cached and reused across experiments
        
        # Clean kubectl temporary files
        log_info "Cleaning kubectl cache..."
        rm -rf ~/.kube/cache/* 2>/dev/null || true
        rm -rf ~/.kube/http-cache/* 2>/dev/null || true
        
        # Clean old Kubernetes logs from terminated pods
        log_info "Cleaning old pod logs..."
        kubectl delete pods --field-selector status.phase=Failed -n default 2>/dev/null || true
        kubectl delete pods --field-selector status.phase=Succeeded -n default 2>/dev/null || true
        
        # Clean tmp files older than 1 day
        log_info "Cleaning old temporary files..."
        find /tmp -type f -mtime +1 -delete 2>/dev/null || true
        
        # Clean user cache
        log_info "Cleaning user cache..."
        rm -rf ~/.cache 2>/dev/null || true
        
        # Clean and vacuum system logs
        log_info "Vacuuming system journal logs..."
        sudo journalctl --rotate 2>/dev/null || true
        sudo journalctl --vacuum-time=1s 2>/dev/null || true
        
        # Clean JaCoCo dump files in the namespace (coverage dumps accumulate)
        log_info "Cleaning old coverage dump files..."
        for pod in $(kubectl get pods -n default -o name 2>/dev/null); do
            kubectl exec $pod -n default -- rm -f /jacoco.exec 2>/dev/null || true
        done
        
        # Clean orphaned PV data from local-path-provisioner to prevent disk exhaustion
        log_info "Cleaning orphaned PersistentVolume data from /opt/local-path-provisioner..."
        sudo rm -rf /opt/local-path-provisioner/* 2>/dev/null || true
        
        local space_freed=$(df -h "${PROJECT_ROOT}" | tail -1 | awk '{print $4}')
        log_info "Cleanup completed. Available space: $space_freed"
        
        return 0
    )
    
    return 0
}

# =============================================================================
# Function: Restart Prometheus to avoid OOM issues
# =============================================================================
restart_prometheus() {
    log_step "Restarting Prometheus to free up memory and ensure fresh metrics collection..."
    
    # Delete all prometheus pods (keep only Running ones to avoid deleting too many failed pods)
    local running_pods=$(kubectl get pods -n kube-system -l app=prometheus --field-selector=status.phase=Running --no-headers 2>/dev/null | awk '{print $1}' || true)
    
    if [[ -n "$running_pods" ]]; then
        log_info "Deleting running Prometheus pod(s)..."
        echo "$running_pods" | while IFS= read -r pod; do
            if [[ -n "$pod" ]]; then
                kubectl delete pod "$pod" -n kube-system --force --grace-period=0 2>/dev/null || true
            fi
        done
    fi
    
    # Also clean up failed prometheus pods to reduce clutter
    log_info "Cleaning up failed Prometheus pods..."
    kubectl delete pods -n kube-system -l app=prometheus --field-selector=status.phase!=Running --force --grace-period=0 2>/dev/null || true
    
    # Wait for new pod to be ready
    log_info "Waiting for Prometheus to restart and become ready (max 180s)..."
    local waited=0
    local max_wait=180
    while (( waited < max_wait )); do
        local ready_count=$(kubectl get pods -n kube-system -l app=prometheus --field-selector=status.phase=Running 2>/dev/null | grep -c "1/1" || echo "0")
        if (( ready_count > 0 )); then
            log_info "Prometheus is ready!"
            kubectl get pods -n kube-system -l app=prometheus | grep Running | cat
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
        if (( waited % 30 == 0 )); then
            log_info "Still waiting for Prometheus... (${waited}s/${max_wait}s)"
        fi
    done
    
    log_warn "Prometheus may not be fully ready yet, but continuing..."
    return 0
}

# =============================================================================
# Function: Generate EvoMaster tests using combined-all-v3.5.json
# Parameters: $1 = experiment name (already includes timestamp)
# Returns: Path to generated EvoMaster test file
# =============================================================================
generate_evomaster_tests() {
    local exp_name="$1"
    
    log_step "Generating new EvoMaster test suite for experiment: $exp_name"
    
    # Get node IP
    local node_ip=$(kubectl get nodes -o wide | awk 'NR==2{print $6}')
    if [[ -z "$node_ip" ]]; then
        log_error "Failed to get node IP"
        return 1
    fi
    
    log_info "Node IP: $node_ip"
    
    # Use the experiment name directly (which already includes timestamp from caller)
    local output_folder="${EVOMASTER_BASE_OUTPUT}/${exp_name}"
    
    log_info "Output folder: $output_folder"
    
    # Create output directory
    mkdir -p "$output_folder"
    
    # Generate unique run ID
    local run_id="em-$(date +%Y%m%d%H%M%S)"
    
    # Build EvoMaster command
    log_info "Running EvoMaster (this may take up to ${EVOMASTER_TIME_LIMIT})..."
    
    # Run EvoMaster command (don't fail on exit code, check output file instead)
    java -jar "$EVOMASTER_JAR" \
        --blackBox true \
        --bbTargetUrl "http://${node_ip}:30467" \
        --bbSwaggerUrl "file://${EVOMASTER_SPEC}" \
        --createTests true \
        --outputFolder "$output_folder" \
        --outputFormat PYTHON_UNITTEST \
        --advancedBlackBoxCoverage true \
        --maxTime "$EVOMASTER_TIME_LIMIT" \
        --seed -1 \
        --header0 "$EVOMASTER_HEADER_AUTH" \
        --header1 "x-evomaster-run-id: ${run_id}" \
        > "${output_folder}/evomaster_output.log" 2>&1 || true
    
    # Wait a moment for files to be written
    sleep 2
    
    # Find the generated test file
    local test_file="${output_folder}/EvoMaster_successes_Test.py"
    
    # Check if file exists (more reliable than exit code)
    if [[ ! -f "$test_file" ]]; then
        log_error "Generated test file not found: $test_file"
        log_error "Contents of output folder:"
        ls -la "$output_folder" || true
        log_error "Last 20 lines of EvoMaster log:"
        tail -20 "${output_folder}/evomaster_output.log" || true
        return 1
    fi
    
    # Verify EvoMaster completed successfully by checking log
    if ! grep -q "EvoMaster process has completed successfully" "${output_folder}/evomaster_output.log" 2>/dev/null; then
        log_warn "EvoMaster log doesn't show success message, but test file exists. Continuing..."
    fi
    
    log_info "EvoMaster test generation completed successfully!"
    log_info "Generated test file: $test_file"
    
    # Return the test file path
    echo "$test_file"
    return 0
}

# =============================================================================
# Function: Run normal case without anomaly injection
# =============================================================================
run_normal_case() {
    local exp_name="Normal_case"
    
    # Generate timestamp at the start of experiment to ensure all modalities use the same timestamp
    local timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    local exp_name_with_ts="${exp_name}_em_${timestamp}"
    
    log_section "Starting Normal Case Collection (No Anomaly)"
    log_info "This will collect baseline data without any chaos injection"
    log_info "Experiment folder name: ${exp_name_with_ts}"
    
    # Cleanup residual files before experiment
    cleanup_residual_files
    
    # Run collection without chaos injection
    # We use a special script call that skips chaos start/stop
    log_step "Running normal case data collection for: ${exp_name_with_ts}"
    
    # Deploy system
    log_step "Resetting and deploying Train-Ticket..."
    cd "$TRAIN_TICKET_DIR" || return 1
    make reset-deploy Namespace="$NAMESPACE" | cat
    
    # Force cleanup any remaining pods/replicasets to prevent duplicates
    log_info "Force cleaning any remaining resources..."
    kubectl delete pods --all -n "$NAMESPACE" --force --grace-period=0 --wait=false 2>/dev/null || true
    kubectl delete replicasets --all -n "$NAMESPACE" --force --grace-period=0 --wait=false 2>/dev/null || true
    
    # Wait for all resources to be fully deleted
    log_info "Waiting for resources to be fully deleted..."
    local cleanup_wait=0
    while [ $cleanup_wait -lt 60 ]; do
        local remaining_pods=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l || echo "0")
        if [ "$remaining_pods" -eq 0 ]; then
            log_info "All old pods deleted successfully"
            break
        fi
        sleep 2
        cleanup_wait=$((cleanup_wait + 2))
    done
    
    sleep 10
    
    # Restart Prometheus to ensure fresh start and avoid OOM issues
    restart_prometheus
    
    make deploy Namespace="$NAMESPACE" DeployArgs="--with-tracing --with-monitoring" | cat
    
    # Wait for pods
    log_step "Waiting for all pods to become Ready..."
    local max_wait=1200
    local elapsed=0
    while [ $elapsed -lt $max_wait ]; do
        # Check for crashed pods first
        local crashed_pods=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -v "Completed" | awk '{if ($3 == "CrashLoopBackOff" || $3 == "Error") print $1}' || echo "")
        
        if [ -n "$crashed_pods" ]; then
            log_warn "Found pods in crashed state, deleting them:"
            echo "$crashed_pods" | while read -r pod; do
                if [ -n "$pod" ]; then
                    log_info "  - Deleting crashed pod: $pod"
                    kubectl delete pod "$pod" -n "$NAMESPACE" --force --grace-period=0 2>/dev/null || true
                fi
            done
            sleep 10
            continue
        fi
        
        local not_ready=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | grep -v "Completed" | awk '{split($2, ready, "/"); if ($3 != "Running" || ready[1] != ready[2]) print $1}' | wc -l)
        if [ "$not_ready" -eq 0 ]; then
            log_info "All pods are Ready!"
            kubectl get pods -n "$NAMESPACE" | cat
            break
        fi
        if [ $((elapsed % 30)) -eq 0 ]; then
            log_info "Still waiting for $not_ready pod(s) to be ready..."
        fi
        sleep 10
        elapsed=$((elapsed + 10))
    done
    
    # Collect data without chaos
    log_step "Collecting multimodal data (no anomaly injected)..."
    
    # Check if test file exists
    if [[ ! -f "$EVOMASTER_TEST_FILE" ]]; then
        log_error "EvoMaster test file not found: $EVOMASTER_TEST_FILE"
        return 1
    fi
    
    log_info "Using test file: $EVOMASTER_TEST_FILE (will execute ${EVOMASTER_TEST_ITERATIONS} times)"
    
    cd "$TDATASET_DIR" || return 1
    bash "./collect_all_modalities.sh" \
        --name "${exp_name_with_ts}" \
        --api evomaster \
        --test-file "$EVOMASTER_TEST_FILE" \
        --test-iterations "$EVOMASTER_TEST_ITERATIONS" \
        --trace-size "$TRACE_SIZE_LIMIT" \
        --trace-hours "$TRACE_LOOKBACK_HOURS" \
        --trace-workers "$TRACE_WORKER_COUNT" | cat
    
    log_section "Normal Case Collection Completed"
    log_info "Baseline data saved for ${exp_name_with_ts}"
    
    return 0
}

# =============================================================================
# Function: Run single experiment (EvoMaster only)
# Parameters: $1 = experiment name (without suffix), $2 = chaos type (mesh|blade)
# =============================================================================
run_single_experiment() {
    local exp_name="$1"
    local chaos_type="$2"
    
    # Generate timestamp at the start of experiment to ensure all modalities use the same timestamp
    local timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    local exp_name_with_ts="${exp_name}_${timestamp}"
    
    log_section "Starting Experiment: $exp_name"
    log_info "Chaos Type: $chaos_type"
    log_info "Using EvoMaster for traffic generation"
    log_info "Experiment folder name: ${exp_name_with_ts}"
    
    # Set tracking variables for error trap (use original name without timestamp for chaos cleanup)
    CURRENT_CHAOS_EXP="$exp_name"
    CURRENT_CHAOS_TYPE="$chaos_type"
    
    # Cleanup residual files before experiment to ensure sufficient disk space
    cleanup_residual_files
    
    # Check if test file exists
    if [[ ! -f "$EVOMASTER_TEST_FILE" ]]; then
        log_error "EvoMaster test file not found: $EVOMASTER_TEST_FILE"
        CURRENT_CHAOS_EXP=""
        return 1
    fi
    
    log_info "Using test file: $EVOMASTER_TEST_FILE (will execute ${EVOMASTER_TEST_ITERATIONS} times)"
    
    # Run EvoMaster scenario (pass the timestamped name, test file and iterations)
    log_step "Running EvoMaster-driven collection for: ${exp_name_with_ts}"
    if ! bash "$RUN_EXP_SCRIPT" --name "$exp_name_with_ts" --api em --test-file "$EVOMASTER_TEST_FILE" --test-iterations "$EVOMASTER_TEST_ITERATIONS"; then
        log_error "EvoMaster collection failed for experiment: $exp_name_with_ts"
        CURRENT_CHAOS_EXP=""
        return 1
    fi
    
    log_info "EvoMaster collection completed successfully!"
    
    # Clear tracking variables
    CURRENT_CHAOS_EXP=""
    CURRENT_CHAOS_TYPE=""
    
    log_section "Experiment Completed: $exp_name_with_ts"
    log_info "Data saved for ${exp_name_with_ts}_em"
    
    return 0
}

# =============================================================================
# Function: Display menu
# =============================================================================
show_menu() {
    log_section "Train-Ticket Automated Data Collection Script"
    
    echo "Available options:"
    echo ""
    echo "Normal Case:"
    echo "  1. Normal Run - NO anomaly injection (baseline data)"
    echo ""
    echo "Performance Level (3 anomalies - Chaos Mesh):"
    echo "  2. CPU Contention on ts-preserve-service (Lv_P_CPU_preserve)"
    echo "  3. Disk I/O Stress on ts-preserve-service (Lv_P_DISKIO_preserve)"
    echo "  4. Network Packet Loss on ts-preserve-service (Lv_P_NETLOSS_preserve)"
    echo ""
    echo "Service Level (3 anomalies - Chaos Mesh):"
    echo "  5. DNS Failure - preserve service (Lv_S_DNSFAIL_preserve_no_order)"
    echo "  6. HTTP Abort on ts-preserve-service (Lv_S_HTTPABORT_preserve)"
    echo "  7. Kill Pod - ts-preserve-service (Lv_S_KILLPOD_preserve)"
    echo ""
    echo "Database Level (3 anomalies - Chaos Mesh):"
    echo "  8. Redis Cache Limit (Lv_D_cachelimit)"
    echo "  9. Connection Pool Exhaustion (Lv_D_CONNECTION_POOL_exhaustion)"
    echo " 10. Transaction Timeout on MySQL (Lv_D_TRANSACTION_timeout)"
    echo ""
    echo "Code Level (3 anomalies - ChaosBlade):"
    echo " 11. Security Check Failure (Lv_C_security_check)"
    echo " 12. Exception Injection (Lv_C_exception_injection)"
    echo " 13. Travel Detail Failure (Lv_C_travel_detail_failure)"
    echo ""
    echo "Batch Operations:"
    echo " 14. Run ALL anomalies sequentially (12 experiments + normal case)"
    echo " 15. Run all Performance Level anomalies (3 experiments)"
    echo " 16. Run all Service Level anomalies (3 experiments)"
    echo " 17. Run all Database Level anomalies (3 experiments)"
    echo " 18. Run all Code Level anomalies (3 experiments)"
    echo ""
    echo "  0. Exit"
    echo ""
}

# =============================================================================
# Main script execution
# =============================================================================
main() {
    log_section "Train-Ticket Automated Data Collection Script Starting"
    
    # Check prerequisites first
    check_prerequisites
    
    # Check if test file exists
    if [[ ! -f "$EVOMASTER_TEST_FILE" ]]; then
        log_error "EvoMaster test file not found: $EVOMASTER_TEST_FILE"
        log_error "Please ensure the test file exists before running experiments"
        exit 1
    fi
    
    log_info "Using test file: $EVOMASTER_TEST_FILE (will execute ${EVOMASTER_TEST_ITERATIONS} times per experiment)"
    
    # Define all anomaly configurations
    # Format: "experiment_name:chaos_type:display_name"
    declare -A ANOMALIES
    ANOMALIES[2]="Lv_P_CPU_preserve:mesh:Performance CPU Contention"
    ANOMALIES[3]="Lv_P_DISKIO_preserve:mesh:Performance Disk I/O Stress"
    ANOMALIES[4]="Lv_P_NETLOSS_preserve:mesh:Performance Network Loss"
    ANOMALIES[5]="Lv_S_DNSFAIL_preserve_no_order:mesh:Service DNS Failure"
    ANOMALIES[6]="Lv_S_HTTPABORT_preserve:mesh:Service HTTP Abort"
    ANOMALIES[7]="Lv_S_KILLPOD_preserve:mesh:Service Kill Preserve Pod"
    ANOMALIES[8]="Lv_D_cachelimit:mesh:Database Cache Limit"
    ANOMALIES[9]="Lv_D_CONNECTION_POOL_exhaustion:mesh:Database Connection Pool"
    ANOMALIES[10]="Lv_D_TRANSACTION_timeout:mesh:Database Transaction Timeout"
    ANOMALIES[11]="Lv_C_security_check:blade:Code Security Check Failure"
    ANOMALIES[12]="Lv_C_exception_injection:blade:Code Exception Injection"
    ANOMALIES[13]="Lv_C_travel_detail_failure:blade:Code Travel Detail Failure"
    
    # Clean up any existing chaos before starting
    cleanup_all_chaos
    
    while true; do
        show_menu
        
        echo -n "Please select an option [0-18]: "
        read choice
        
        case $choice in
            0)
                log_info "Exiting script. Goodbye!"
                exit 0
                ;;
            1)
                log_info "Selected: Normal Run (No Anomaly)"
                run_normal_case
                ;;
            2|3|4|5|6|7|8|9|10|11|12|13)
                IFS=':' read -r exp_name chaos_type display_name <<< "${ANOMALIES[$choice]}"
                log_info "Selected: $display_name"
                run_single_experiment "$exp_name" "$chaos_type"
                ;;
            14)
                log_info "Running Normal Case + ALL 12 anomaly experiments sequentially..."
                log_warn "This will take several hours to complete!"
                echo -n "Are you sure? (yes/no): "
                read confirm
                if [ "$confirm" != "yes" ]; then
                    log_info "Cancelled by user"
                    continue
                fi
                
                # First run normal case
                log_section "Batch Progress: Normal Case (Baseline)"
                run_normal_case
                sleep "$INTER_EXPERIMENT_WAIT"
                
                # Then run all anomalies
                for i in {2..13}; do
                    IFS=':' read -r exp_name chaos_type display_name <<< "${ANOMALIES[$i]}"
                    log_section "Batch Progress: Experiment $((i-1))/12 - $display_name"
                    run_single_experiment "$exp_name" "$chaos_type"
                    
                    if [ $i -lt 13 ]; then
                        log_info "Waiting ${INTER_EXPERIMENT_WAIT} seconds before next experiment..."
                        sleep "$INTER_EXPERIMENT_WAIT"
                    fi
                done
                log_section "All experiments completed (Normal + 12 anomalies)!"
                ;;
            15)
                log_info "Running all Performance Level anomalies (3 experiments)..."
                for i in {2..4}; do
                    IFS=':' read -r exp_name chaos_type display_name <<< "${ANOMALIES[$i]}"
                    run_single_experiment "$exp_name" "$chaos_type"
                    if [ $i -lt 4 ]; then sleep "$INTER_EXPERIMENT_WAIT"; fi
                done
                log_section "Performance Level anomalies completed!"
                ;;
            16)
                log_info "Running all Service Level anomalies (3 experiments)..."
                for i in {5..7}; do
                    IFS=':' read -r exp_name chaos_type display_name <<< "${ANOMALIES[$i]}"
                    run_single_experiment "$exp_name" "$chaos_type"
                    if [ $i -lt 7 ]; then sleep "$INTER_EXPERIMENT_WAIT"; fi
                done
                log_section "Service Level anomalies completed!"
                ;;
            17)
                log_info "Running all Database Level anomalies (3 experiments)..."
                for i in {8..10}; do
                    IFS=':' read -r exp_name chaos_type display_name <<< "${ANOMALIES[$i]}"
                    run_single_experiment "$exp_name" "$chaos_type"
                    if [ $i -lt 10 ]; then sleep "$INTER_EXPERIMENT_WAIT"; fi
                done
                log_section "Database Level anomalies completed!"
                ;;
            18)
                log_info "Running all Code Level anomalies (3 experiments)..."
                for i in {11..13}; do
                    IFS=':' read -r exp_name chaos_type display_name <<< "${ANOMALIES[$i]}"
                    run_single_experiment "$exp_name" "$chaos_type"
                    if [ $i -lt 13 ]; then sleep "$INTER_EXPERIMENT_WAIT"; fi
                done
                log_section "Code Level anomalies completed!"
                ;;
            *)
                log_error "Invalid option. Please try again."
                sleep 2
                ;;
        esac
        
        echo ""
        echo "Press Enter to continue..."
        read
    done
}

# Script entry point
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
