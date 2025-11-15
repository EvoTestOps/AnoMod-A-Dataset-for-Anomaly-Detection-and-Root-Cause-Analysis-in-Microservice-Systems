#!/usr/bin/env bash
set -euo pipefail

# One-click coverage workflow:
# 1) Dump exec data from each ts-* Java service into /coverage without restarting pods
# 2) Pull the exec files to T-Dataset/coverage_data/<RUN_ID>
# 3) Copy the running JAR from the container and extract BOOT-INF/classes
# 4) Generate concise coverage reports for every Java service under T-Dataset/coverage_report/<RUN_ID>/<service>
#
# Configurable variables (all optional):
# - NS=default                Kubernetes namespace
# - RUN_ID=$(date +%Y%m%d_%H%M%S)  Batch identifier (used in directory names)
# - PORT=6300                 JaCoCo tcpserver port
# - TT_ROOT=/home/ubuntu/train-ticket   Source tree root (used to resolve file paths in reports)
# - OUT_ROOT=/home/ubuntu/T-Dataset
#
# Dependencies:
# - JaCoCo must already be injected in tcpserver mode (see inject_jacoco_k8s.py)
# - python3 must exist on the host, and each Java container must have java plus jacococli.jar (the injector copies it)

NS="${NS:-default}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
PORT="${PORT:-6300}"
TT_ROOT="${TT_ROOT:-/home/ubuntu/train-ticket}"
OUT_ROOT="${OUT_ROOT:-/home/ubuntu/T-Dataset}"
TIMEOUT="${TIMEOUT:-15}"  # Per-operation timeout in seconds

EXEC_DIR="${OUT_ROOT}/coverage_data/${RUN_ID}"
REPORT_ROOT="${OUT_ROOT}/coverage_report/${RUN_ID}"
CLASS_CACHE="${OUT_ROOT}/coverage_tools/class_cache"

mkdir -p "${EXEC_DIR}" "${REPORT_ROOT}" "${CLASS_CACHE}"

echo "[coverage] NS=${NS} RUN_ID=${RUN_ID} PORT=${PORT} TIMEOUT=${TIMEOUT}s"
echo "[coverage] EXEC_DIR=${EXEC_DIR}"

# Count how many services will be processed
total_services=$(kubectl -n "${NS}" get pods -l app -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | grep -c '^ts-' || echo 0)
echo "[coverage] Found ${total_services} Train-Ticket services to process"

# 1) Dump to /coverage inside each container without restarting, then copy locally
echo "[coverage] dumping exec from ts-* pods (tcpserver)..."
current_service=0
for pod in $(kubectl -n "${NS}" get pods -l app -o jsonpath='{.items[*].metadata.name}'); do
  case "${pod}" in
    ts-*) ;;
    *) continue;;
  esac
  
  current_service=$((current_service + 1))
  echo "[coverage] [$current_service/$total_services] Processing ${pod}..."
  
  # Only run if jacococli.jar exists; use env -u JAVA_TOOL_OPTIONS to avoid port clashes
  if timeout ${TIMEOUT} kubectl -n "${NS}" exec "${pod}" -- sh -c 'test -f /jacoco/jacococli.jar' >/dev/null 2>&1; then
    echo "  ✓ JaCoCo CLI found, dumping coverage..."
    if timeout ${TIMEOUT} kubectl -n "${NS}" exec "${pod}" -- sh -c "\
      mkdir -p /coverage && \
      env -u JAVA_TOOL_OPTIONS java -jar /jacoco/jacococli.jar dump --address localhost --port ${PORT} \
        --destfile /coverage/jacoco-${pod}.exec --reset" >/dev/null 2>&1; then
      echo "  ✓ Coverage dumped successfully"
    else
      echo "  ✗ Dump failed or timed out after ${TIMEOUT}s"
    fi
  else
    echo "  ✗ JaCoCo CLI not found or check timed out"
  fi
done

echo "[coverage] pulling exec files to ${EXEC_DIR}"
current_service=0
for pod in $(kubectl -n "${NS}" get pods -l app -o jsonpath='{.items[*].metadata.name}'); do
  case "${pod}" in
    ts-*) ;;
    *) continue;;
  esac
  
  current_service=$((current_service + 1))
  echo "[coverage] [$current_service/$total_services] Pulling from ${pod}..."
  
  if timeout ${TIMEOUT} kubectl -n "${NS}" exec "${pod}" -- sh -c 'ls /coverage 1>/dev/null 2>&1'; then
    cname=$(kubectl -n "${NS}" get pod "${pod}" -o jsonpath='{.spec.containers[0].name}' 2>/dev/null || echo "main")
    exec_files=$(kubectl -n "${NS}" exec "${pod}" -- sh -c 'ls -1 /coverage/*.exec 2>/dev/null || true')
    if [[ -n "${exec_files}" ]]; then
      for f in ${exec_files}; do
        base=$(basename "$f")
        dst="${EXEC_DIR}/${pod}__${base}"
        echo "  → Copying ${base}..."
        if timeout ${TIMEOUT} kubectl -n "${NS}" cp -c "${cname}" "${pod}:${f}" "${dst}" 2>/dev/null; then
          file_size=$(ls -lh "${dst}" 2>/dev/null | awk '{print $5}' || echo "?")
          echo "  ✓ Copied ${base} (${file_size})"
        else
          echo "  ✗ Copy failed or timed out"
        fi
      done
    else
      echo "  ✗ No exec files found"
    fi
  else
    echo "  ✗ No coverage directory or check timed out"
  fi
done

# 2) For every Java service: copy the JAR, extract classes, and call coverage_summary.py
echo "[coverage] generating reports for all Java services -> ${REPORT_ROOT}"
services=$(kubectl -n "${NS}" get pods -l app -o jsonpath='{range .items[*]}{.metadata.labels.app}{"\n"}{end}' | sort -u)
total_report_services=$(echo "$services" | grep -c '^ts-' || echo 0)
current_report=0

for svc in ${services}; do
  case "${svc}" in
    ts-*) ;;
    *) continue;;
  esac
  
  current_report=$((current_report + 1))
  echo "[coverage] [$current_report/$total_report_services] Generating report for ${svc}..."
  
  pod=$(timeout ${TIMEOUT} kubectl -n "${NS}" get pods -l app=${svc} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -z "${pod}" ]]; then
    echo "  ✗ No pod found"
    continue
  fi

  jar_path=$(timeout ${TIMEOUT} kubectl -n "${NS}" exec "${pod}" -- sh -c "ps -ef | grep 'java ' | grep ' -jar ' | grep -v grep | awk '{for(i=1;i<=NF;i++) if(\$i==\"-jar\") print \$(i+1)}'" | head -n1 2>/dev/null || true)
  if [[ -z "${jar_path}" ]]; then
    echo "  ✗ Non-Java service or no -jar found"
    continue
  fi

  svc_cache_dir="${CLASS_CACHE}/${svc}"
  classes_dir="${svc_cache_dir}/BOOT-INF/classes"
  mkdir -p "${svc_cache_dir}"
  cname=$(kubectl -n "${NS}" get pod "${pod}" -o jsonpath='{.spec.containers[0].name}' 2>/dev/null || echo "main")
  
  echo "  → Copying JAR: ${jar_path}"
  if timeout ${TIMEOUT} kubectl -n "${NS}" cp -c "${cname}" "${pod}:${jar_path}" "${svc_cache_dir}/${svc}.jar" >/dev/null 2>&1; then
    echo "  ✓ JAR copied"
  else
    echo "  ✗ JAR copy failed or timed out"
    continue
  fi

  python3 - << PY || true
import zipfile, os
jar_path = r"${svc_cache_dir}/${svc}.jar"
out_dir = r"${svc_cache_dir}"
try:
    with zipfile.ZipFile(jar_path, 'r') as z:
        members = [m for m in z.namelist() if m.startswith('BOOT-INF/classes/')]
        z.extractall(out_dir, members)
        print('EXTRACTED', len(members), 'files')
except Exception as e:
    print('EXTRACT ERROR', e)
PY

  if [[ ! -d "${classes_dir}" ]]; then
    echo "  ✗ No BOOT-INF/classes directory"
    continue
  fi

  out_dir="${REPORT_ROOT}/${svc}"
  echo "  → Generating coverage report..."
  if timeout 30 python3 /home/ubuntu/T-Dataset/coverage_tools/coverage_summary.py \
    --exec-dir "${EXEC_DIR}" \
    --service "${svc}" \
    --classes "${classes_dir}" \
    --sources "${TT_ROOT}/${svc}/src/main/java" \
    --out "${out_dir}" >/dev/null 2>&1; then
    echo "  ✓ Report generated: ${out_dir}"
  else
    echo "  ✗ Report generation failed or timed out"
  fi
done

# Print final statistics
exec_count=$(find "${EXEC_DIR}" -name "*.exec" 2>/dev/null | wc -l || echo 0)
report_count=$(find "${REPORT_ROOT}" -name "*.html" 2>/dev/null | wc -l || echo 0)
total_size=$(du -sh "${EXEC_DIR}" 2>/dev/null | cut -f1 || echo "0")

echo "[coverage] === SUMMARY ==="
echo "  Exec files collected: ${exec_count}"
echo "  Reports generated: ${report_count}"
echo "  Total data size: ${total_size}"
echo "  Exec location: ${EXEC_DIR}"
echo "  Reports location: ${REPORT_ROOT}"

if [[ $exec_count -gt 0 ]]; then
  echo "✅ Coverage collection completed successfully!"
else
  echo "❌ No coverage data collected!"
fi


