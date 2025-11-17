## SocialNetwork Multimodal Collection Toolkit

### 1. Project Overview
- **Dataset name**: *SocialNetwork Multimodal Anomaly & Baseline Collection* (SN-MAC).
- **Goal**: provide reproducible, multimodal telemetry (logs, metrics, traces, API payloads, coverage) for anomaly detection and root-cause analysis studies, enabling future open benchmarking campaigns.
- **Research focus**: benchmark anomaly classifiers, causal inference pipelines, and observability tooling against microservice faults spanning performance, service, database, and code-level failures.
- **Unique value**: pairs deterministic traffic replays (EvoMaster + wrk2) with scripted chaos injections and synchronized collectors, producing FAIR-aligned data ready for release (GitHub, Zenodo).

### 2. Data Collection Method
- **Source system**: DeathStarBench SocialNetwork (Docker Compose, coverage-enabled stack).
- **Traffic drivers**: EvoMaster regression suites (256 HTTP requests per run) and wrk2 Lua workload; selectable per experiment.
- **Anomaly injection**: ChaosBlade CLI and targeted container restarts simulate CPU, network, disk, Redis cache constraints, and service kills.
- **Process**:
  1. Reset SocialNetwork deployment (`docker-compose-gcov.yml`, `init_social_graph.py`).
  2. Execute selected chaos scenario (or baseline).
  3. Trigger workload(s) repeatedly.
  4. Run `collect_all_data.sh` to capture logs, Prometheus metrics, Jaeger traces, API responses, and coverage exec files.
  5. Archive outputs under `${DATA_ARCHIVE_ROOT}` with timestamped folder names.
- **Temporal scale**: defaults to 24h metric windows, 60-70s API monitoring, and 200 EvoMaster iterations per anomaly; configurable via environment variables and script prompts.

### 3. Data Pattern & Structure
- **Modalities**:
  - `log_data/<exp>_logs_<timestamp>/` - raw container logs + `summary.txt`.
  - `metric_data/<exp>_metrics_<timestamp>/` - Prometheus CSV exports (service, infra, storage KPIs) + `metadata.txt`.
  - `trace_data/<exp>_traces_<timestamp>/` - Jaeger JSON + CSV flattening + `available_services.json`.
  - `api_responses/<exp>_openapi_<timestamp>/` - JSONL response stream, latency reports, optional `.pcap` and tshark analysis.
  - `coverage_data/<exp>_coverage_<timestamp>/` - merged JaCoCo execs and HTML/XML reports per service.
- **File naming**: `${ExperimentName}_${ISO8601Timestamp}_${modality}` to guarantee lexical ordering and reproducibility.
- **Schemas**: all CSV/JSON outputs include headers with UTC timestamps, service identifiers, and measurement units documented in `metric_data/metadata.txt` and `api_responses/*.json`.
- **Canonical storage**: the curated dataset for this project resides under `AnoMod/SN_data`, mirroring the modality folders described above. Point `${DATA_ARCHIVE_ROOT}` to this location when reproducing the release layout.

### 4. Environment Configuration
- **System requirements**: Linux x86-64, 16+GB RAM, Docker 24+, Docker Compose, Python 3.9+, Node Exporter/Prometheus stack for metrics, ChaosBlade 1.7.4, wrk2, EvoMaster (Docker image `webfuzzing/evomaster`).
- **Core dependencies**:
  - Shell utilities: `jq`, `tcpdump`, `tshark`, `curl`.
  - Python packages: `requests`, `aiohttp`, `pandas`, `numpy`, `timeout_decorator`.
  - External services: Prometheus at `${PROMETHEUS_URL:-http://localhost:9090}`, Jaeger query at `http://localhost:16686`.
- **Required environment variables** (replace placeholders per `ANONYMIZATION_MAP.md`):
  ```bash
  export PROJECT_ROOT=/srv/socialnetwork-tooling
  export DATA_ARCHIVE_ROOT=/data/sn_multimodal
  export DATASET_STORAGE_DIR=$DATA_ARCHIVE_ROOT/raw
  export DATASET_SCRIPT_DIR=$PROJECT_ROOT/Dataset
  export SOCIAL_NETWORK_DIR=$PROJECT_ROOT/DeathStarBench/socialNetwork
  export CHAOSBLADE_DIR=/opt/chaosblade/chaosblade-1.7.4
  export EVOMASTER_BASE_DIR=$PROJECT_ROOT/BlackBox_tests
  export EVOMASTER_TEST_PATH=$EVOMASTER_BASE_DIR/Final_version_2m/EvoMaster_successes_Test.py
  export COLLECT_DATA_SCRIPT=$PROJECT_ROOT/collect_all_data.sh
  export WORKLOAD_WRK_BINARY=$PROJECT_ROOT/DeathStarBench/wrk2/wrk
  export WORKLOAD_SCRIPT_PATH=$SOCIAL_NETWORK_DIR/wrk2/scripts/social-network/mixed-workload.lua
  export API_SPEC_PATH=$PROJECT_ROOT/social-network-api.yaml
  ```
- **Optional knobs**: `EVOMASTER_RUN_COUNT`, `WORKLOAD_RUN_COUNT`, `PROMETHEUS_URL`, `OPENAPI_LOG_TO_FILE`.

### 5. Usage Guide
- **Quick start**:
  1. Export environment variables above and ensure Docker/ChaosBlade/wrk2 binaries are on `PATH`.
  2. Run `./automated_multimodal_collection.sh` and choose trigger mode + anomaly from the interactive menu.
  3. Collected data appears under `${DATA_ARCHIVE_ROOT}` with experiment-specific prefixes (the distributed dataset maps this path to `AnoMod/SN_data`).
- **Script entry points**:
  - `automated_multimodal_collection.sh`: full automation (reset -> anomaly -> trigger -> collect). Supports EvoMaster or wrk2 traffic.
  - `collect_all_data.sh`: manual data capture; mode `1` restarts services + chaos, mode `2` only runs collectors (used by automation).
  - Modal collectors under `Dataset/*`: can be invoked independently for debugging (e.g., `cd Dataset/log_data && CUSTOM_DIR=test_logs ./collect_log.sh 30m`).
- **Parameters**:
  - Chaos command prompt accepts any `blade` args (`create cpu load ...`).
  - `collect_all_data.sh` accepts experiment base name and optionally `CUSTOM_DIR` to override folder naming.
- **Example test case**:
  ```bash
  CUSTOM_DIR=baseline_logs ./Dataset/log_data/collect_log.sh 15m
  python3 Dataset/metric_data/fetch_prometheus_metrics.py \
      --query 'sum(rate(http_requests_total[5m]))' \
      --output /tmp/req_rate.csv --hours 6 --step 30s
  ```
- **Post-run verification**: use `summary.txt`, `metadata.txt`, and coverage HTML reports to ensure completeness.

### 6. Reproducibility
- **Deterministic steps**:
  1. `docker-compose -f docker-compose-gcov.yml down && up`.
  2. `python scripts/init_social_graph.py --graph socfb-Reed98`.
  3. ChaosBlade scenario logged in automation output.
  4. EvoMaster suite run count recorded (default 200).
  5. Modal collectors emit timestamps in UTC for alignment.
- **Required inputs**: running SocialNetwork stack, Prometheus/Jaege endpoints exposed, valid API spec for EvoMaster, and ChaosBlade accessible.
- **Expected outputs**: per-modality folders described in Section 3 plus aggregate log at console/stdout.
- **Quality controls**:
  - `collect_all_data.sh` verifies directories, warns if `blade`/API spec missing.
  - `collect_log.sh` validates that each log file is non-empty and counts INFO/WARN/ERROR lines.
  - Coverage collector waits for exec dumps and confirms move to `${DATASET_STORAGE_DIR}/coverage_data`.

### 7. Technical Architecture
- **Modules**:
  - *Trigger layer*: EvoMaster suites (`BlackBox_tests/...`) and wrk2 Lua workload.
  - *Chaos layer*: ChaosBlade CPU/network/disk/service/database primitives.
  - *Collector layer*: bash/Python helpers under `Dataset/` directories.
  - *Archive layer*: `${DATA_ARCHIVE_ROOT}` with FAIR-compliant naming.
- **Data flow**: experiment metadata propagates via `CUSTOM_DIR`, environment variables, and summary files to keep artifacts correlated.

### 8. Validation & Evaluation
- **Data quality checks**:
  - Prometheus queries are retried once by `fetch_prometheus_metrics.py`; warnings emitted when empty.
  - Jaeger trace collector merges by `traceID` to avoid duplicates and counts final rows.
  - API monitor logs success/error counts and captures latency percentiles.
- **Experimental validation**:
  - For each anomaly, compare baseline vs. anomaly metrics (e.g., CPU, request latency) using provided CSVs.
  - Coverage reports ensure instrumentation toggled; absence flagged with warnings.
  - Example KPI thresholds: CPU contention should drive `system_cpu_usage.csv` above 90%, Redis cache-limit anomalies should reduce `redis_memory_used.csv` plateaus.



