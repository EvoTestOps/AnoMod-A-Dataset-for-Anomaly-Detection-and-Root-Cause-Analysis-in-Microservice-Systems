#!/usr/bin/env bash

set -euo pipefail

# One-click multi-modal data collection for Train-Ticket
# Modalities: logs, metrics, traces, code coverage, API responses
#
# Output layout: each modality writes under its own base folder, with a shared
# experiment-named subfolder for this run:
#   - ${LOG_BASE}/<EXP_NAME>
#   - ${METRIC_BASE}/<EXP_NAME>
#   - ${TRACE_BASE}/<EXP_NAME>
#   - ${API_BASE}/<EXP_NAME>/YYYYMMDD/api_responses.jsonl
#   - coverage location is managed by coverage script, keyed by RUN_ID=<EXP_NAME>
#
# Usage examples:
#   bash ./collect_all_modalities.sh
#   bash ./collect_all_modalities.sh --name exp-foo --api evomaster
#   bash ./collect_all_modalities.sh --name perf-$(date -u +%Y%m%dT%H%M%SZ) --api py

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
LOG_BASE="${LOG_BASE:-${SCRIPT_DIR}/log_data}"
METRIC_BASE="${METRIC_BASE:-${SCRIPT_DIR}/metric_data}"
TRACE_BASE="${TRACE_BASE:-${SCRIPT_DIR}/trace_data}"
API_BASE="${API_BASE:-${SCRIPT_DIR}/api_responses}"
COVERAGE_SCRIPT="${COVERAGE_SCRIPT:-${SCRIPT_DIR}/coverage_tools/collect_coverage_reports.sh}"

EVOMASTER_HOME="${EVOMASTER_HOME:-${PROJECT_ROOT}/Evomaster}"
EVOMASTER_RUNS="${EVOMASTER_RUNS:-${EVOMASTER_HOME}/runs}"
EVOMASTER_TEST="${EVOMASTER_TEST:-${EVOMASTER_RUNS}/auth_fixed_10m/EvoMaster_successes_Test.py}"
PY_TEST="${PY_TEST:-${PROJECT_ROOT}/train-ticket-auto-query/test_all_services.py}"

EXP_NAME="exp-$(date -u +%Y%m%dT%H%M%SZ)"
API_MODE="evomaster"   # evomaster | py | none
RUN_COVERAGE="yes"      # yes | no
TRACE_SIZE=0            # 0 = collect every available trace
TRACE_HOURS=24          # Lookback window for trace collection
TRACE_WORKERS=24        # Worker threads for span detail retrieval
TEST_ITERATIONS=1       # Number of times to run EvoMaster test (to generate more traces)
EVOMASTER_TEST_PATH=""  # Path to EvoMaster test file (can be overridden by --evomaster-test or --test-file)
MASTER_TESTS=""  # Space-separated paths to master test files (overrides --evomaster-test)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      EXP_NAME="$2"; shift 2 ;;
    --api)
      API_MODE="$2"; shift 2 ;;
    --no-coverage)
      RUN_COVERAGE="no"; shift 1 ;;
    --trace-size)
      TRACE_SIZE="$2"; shift 2 ;;
    --trace-hours)
      TRACE_HOURS="$2"; shift 2 ;;
    --trace-workers)
      TRACE_WORKERS="$2"; shift 2 ;;
    --test-iterations)
      TEST_ITERATIONS="$2"; shift 2 ;;
    --evomaster-test)
      EVOMASTER_TEST_PATH="$2"; shift 2 ;;
    --test-file)
      EVOMASTER_TEST_PATH="$2"; shift 2 ;;
    --master-tests)
      MASTER_TESTS="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--name EXP_NAME] [--api evomaster|py|none] [--no-coverage] [--trace-size SIZE] [--test-iterations N] [--test-file PATH] [--evomaster-test PATH] [--master-tests PATHS]";
      echo "  --trace-size: Maximum number of traces to collect (0 = all, default: 0)";
      echo "  --trace-hours: Hour lookback window for traces (default: 24)";
      echo "  --trace-workers: Number of span detail worker threads (default: 24)";
      echo "  --test-iterations: Number of times to run EvoMaster test (default: 1)";
      echo "  --test-file: Path to EvoMaster test file (same as --evomaster-test)";
      echo "  --evomaster-test: Path to EvoMaster test file (default: uses EVOMASTER_TEST variable)";
      echo "  --master-tests: Space-separated paths to master test files (overrides --evomaster-test)";
      exit 0 ;;
    *)
      echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Determine which test to use: master tests > evomaster-test > default
if [[ -n "${MASTER_TESTS}" ]]; then
  # Using master test suite - will be handled specially in test execution
  echo "==> Using master test suite (5 test files)"
elif [[ -n "${EVOMASTER_TEST_PATH}" ]]; then
  EVOMASTER_TEST="${EVOMASTER_TEST_PATH}"
  echo "==> Using provided EvoMaster test: ${EVOMASTER_TEST}"
else
  echo "==> Using default EvoMaster test: ${EVOMASTER_TEST}"
fi

echo "==> Experiment name: ${EXP_NAME}"
echo "==> API collection mode: ${API_MODE}"
echo "==> Run coverage: ${RUN_COVERAGE}"
if [[ "${TRACE_SIZE}" -le 0 ]]; then
  echo "==> Trace size limit: all available traces"
else
  echo "==> Trace size limit: ${TRACE_SIZE}"
fi
echo "==> Trace lookback window (hours): ${TRACE_HOURS}"
echo "==> Trace span workers: ${TRACE_WORKERS}"
echo "==> Test iterations: ${TEST_ITERATIONS}"
if [[ -n "${EVOMASTER_TEST_PATH}" ]]; then
  echo "==> Using EvoMaster test: ${EVOMASTER_TEST_PATH}"
fi

# Ensure we run from the T-Dataset folder because collectors write to relative dirs
cd "${SCRIPT_DIR}"
echo "==> Working directory: $(pwd)"

mkdir -p "${LOG_BASE}" "${METRIC_BASE}" "${TRACE_BASE}" "${API_BASE}"

# Helper: move newest files (created after marker) into experiment folder
move_new_files_into_exp_dir() {
  local base_dir="$1"
  local marker="$2"
  local exp_dir="$3"
  mkdir -p "${exp_dir}"
  # Move only files directly under base_dir newer than marker
  find "${base_dir}" -maxdepth 1 -type f -newer "${marker}" -print0 | xargs -0 -I{} mv {} "${exp_dir}" || true
}

# 1) Start API response recording (optional, run multiple iterations for more traces)
API_PID=""
if [[ "${API_MODE}" != "none" ]]; then
  export API_RESP_OUT="${API_BASE}/${EXP_NAME}"
  export PYTHONPATH="${EVOMASTER_RUNS}"
  echo "==> Starting API-response run with API_RESP_OUT=${API_RESP_OUT}"
  echo "==> Running ${TEST_ITERATIONS} iteration(s) of tests to generate more traces..."
  
  case "${API_MODE}" in
    evomaster)
      if [[ -n "${MASTER_TESTS}" ]]; then
        # Using master test suite (5 test files) - run sequentially
        echo "==> Running master test suite (5 test files sequentially)..."
        file_count=0
        for test_file in ${MASTER_TESTS}; do
          file_count=$((file_count + 1))
          echo "==> Executing master test file ${file_count}/5: $(basename ${test_file})"
          if [[ ! -f "$test_file" ]]; then
            echo "ERROR: Test file not found: $test_file"
            exit 1
          fi
          ( set -x; python3 "${test_file}" | cat ) || echo "==> Test completed with failures (continuing data collection)"
          echo "==> Master test file ${file_count}/5 completed"
          if [[ $file_count -lt 5 ]]; then
            sleep 3  # Brief pause between test files
          fi
        done
        # Set API_PID to empty since we're running sequentially
        API_PID=""
      else
        # Run single EvoMaster test multiple times to generate more traces
        if [[ ${TEST_ITERATIONS} -eq 1 ]]; then
          # Single iteration: run in background (original behavior)
          ( set -x; python3 "${EVOMASTER_TEST}" | cat ) &
          API_PID=$!
        else
          # Multiple iterations: run sequentially to ensure each completes before next starts
          for ((i=1; i<=TEST_ITERATIONS; i++)); do
            echo "==> EvoMaster test iteration ${i}/${TEST_ITERATIONS}"
            ( set -x; python3 "${EVOMASTER_TEST}" | cat ) || echo "==> Test iteration ${i} completed with failures (continuing data collection)"
            echo "==> Iteration ${i} completed"
            if [[ $i -lt ${TEST_ITERATIONS} ]]; then
              sleep 2  # Brief pause between iterations
            fi
          done
          # Set API_PID to empty since we're running sequentially
          API_PID=""
        fi
      fi
      ;;
    py)
      # Run Python test multiple times
      if [[ ${TEST_ITERATIONS} -eq 1 ]]; then
        ( set -x; python3 "${PY_TEST}" | cat ) &
        API_PID=$!
      else
        for ((i=1; i<=TEST_ITERATIONS; i++)); do
          echo "==> Python test iteration ${i}/${TEST_ITERATIONS}"
          ( set -x; python3 "${PY_TEST}" | cat ) || echo "==> Test iteration ${i} completed with failures (continuing data collection)"
          echo "==> Iteration ${i} completed"
          if [[ $i -lt ${TEST_ITERATIONS} ]]; then
            sleep 2
          fi
        done
        API_PID=""
      fi
      ;;
    *)
      echo "Invalid --api value: ${API_MODE}"; exit 1 ;;
  esac
fi

# 2) Logs (will create log_data/<EXP_NAME>_<ts> then we rename to <EXP_NAME>)
echo "==> Collecting logs..."
(
  set -x
  python3 "${SCRIPT_DIR}/log_collector.py" --experiment-name "${EXP_NAME}"
)
# Normalize log folder name to remove timestamp suffix
created_log_dir=$(ls -1dt "${LOG_BASE}/${EXP_NAME}_"* 2>/dev/null | head -n1 || true)
if [[ -n "${created_log_dir}" && -d "${created_log_dir}" ]]; then
  target_log_dir="${LOG_BASE}/${EXP_NAME}"
  if [[ -d "${target_log_dir}" ]]; then rm -rf "${target_log_dir}"; fi
  mv "${created_log_dir}" "${target_log_dir}"
  echo "==> Logs placed in: ${target_log_dir}"
else
  echo "WARN: Did not find a created log folder matching ${LOG_BASE}/${EXP_NAME}_*"
fi

# 3) Metrics (default to experiment mode), then place into metric_data/<EXP_NAME>
echo "==> Collecting metrics (experiment mode)..."
before_metric_marker="${METRIC_BASE}/.${EXP_NAME}.start"
date -u +%s > "${before_metric_marker}"
(
  set -x
  python3 "${SCRIPT_DIR}/metric_collector.py" --mode experiment --experiment-name "${EXP_NAME}"
)
metric_exp_dir="${METRIC_BASE}/${EXP_NAME}"
mkdir -p "${metric_exp_dir}"
# Move any freshly created CSVs into the experiment folder
move_new_files_into_exp_dir "${METRIC_BASE}" "${before_metric_marker}" "${metric_exp_dir}"
rm -f "${before_metric_marker}"
echo "==> Metrics placed in: ${metric_exp_dir}"

# 4) Traces (no built-in exp folder) -> move recent files into trace_data/<EXP_NAME>
if [[ "${TRACE_SIZE}" -le 0 ]]; then
  trace_limit_label="all available traces"
else
  trace_limit_label="${TRACE_SIZE}"
fi
echo "==> Collecting traces with enhanced multimodal span relationships (size limit: ${trace_limit_label})..."
before_trace_marker="${TRACE_BASE}/.${EXP_NAME}.start"
date -u +%s > "${before_trace_marker}"
(
  set -x
  python3 "${SCRIPT_DIR}/trace_collector.py" --multimodal --size "${TRACE_SIZE}" --hours "${TRACE_HOURS}" --max-workers "${TRACE_WORKERS}" --output-dir "${TRACE_BASE}" --experiment-name "${EXP_NAME}"
)
trace_exp_dir="${TRACE_BASE}/${EXP_NAME}"
move_new_files_into_exp_dir "${TRACE_BASE}" "${before_trace_marker}" "${trace_exp_dir}"
rm -f "${before_trace_marker}"
echo "==> Traces placed in: ${trace_exp_dir}"

# 5) Code coverage (keyed by RUN_ID=EXP_NAME)
if [[ "${RUN_COVERAGE}" == "yes" ]]; then
  echo "==> Collecting code coverage..."
  (
    set -x
    timeout 300 bash -c "RUN_ID=\"${EXP_NAME}\" NS=\"default\" \"${COVERAGE_SCRIPT}\"" || echo "Coverage collection timed out after 5 minutes"
  )
else
  echo "==> Skipping coverage collection (--no-coverage given)"
fi

# 6) Wait for API task (if started)
if [[ -n "${API_PID}" ]]; then
  echo "==> Waiting for API-response process (PID ${API_PID}) to finish..."
  wait "${API_PID}" || true
  echo "==> API-response recording completed. Files under: ${API_BASE}/${EXP_NAME}/$(date -u +%Y%m%d)"
fi

echo "\nAll modalities collected for: ${EXP_NAME}"
echo "- Logs:     ${LOG_BASE}/${EXP_NAME}"
echo "- Metrics:  ${METRIC_BASE}/${EXP_NAME}"
echo "- Traces:   ${TRACE_BASE}/${EXP_NAME}"
echo "- Coverage: managed by RUN_ID=${EXP_NAME} (see coverage tool output)"
echo "- API resp: ${API_BASE}/${EXP_NAME}/$(date -u +%Y%m%d)/api_responses.jsonl (if API mode enabled)"
