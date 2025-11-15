# Train-Ticket Multi-Modal Dataset Toolkit

This folder contains the automation that was used to gather the five data modalities (logs, metrics, traces, coverage, API responses) for the Train-Ticket anomaly dataset. Follow this guide to reproduce the collection workflow end to end.

## Data Layout

All collectors agree on the same experiment identifier (`EXP_NAME`). Each run produces:

```
T-Dataset/
├── api_responses/<EXP_NAME>/<YYYYMMDD>/api_responses.jsonl
├── log_data/<EXP_NAME>/
├── metric_data/<EXP_NAME>/*.csv
├── trace_data/<EXP_NAME>/*.jsonl
├── coverage_data/<RUN_ID>/jacoco-*.exec
└── coverage_report/<RUN_ID>/<service>/index.html
```

Use `EXP_NAME` to encode the injected fault (e.g., `Lv_P_CPU_preserve_20251115T0900Z`) so logs, metrics, and traces remain correlated.

## Quick Start: Collect Everything at Once

```bash
cd $(git rev-parse --show-toplevel)/TT_collection-scripts/T-Dataset
chmod +x collect_all_modalities.sh
./collect_all_modalities.sh \
  --name Lv_P_CPU_preserve_$(date -u +%Y%m%dT%H%M%SZ) \
  --api evomaster \
  --trace-size 2000 \
  --test-iterations 2
```

What the script does:

1. Runs EvoMaster (or the Python scenario driver) to exercise the APIs. Set `--api py` to use `train-ticket-auto-query/test_all_services.py` instead, or `--api none` if you only need passive collection.
2. Calls `log_collector.py` to stream pod logs, previous container logs, and Kubernetes events.
3. Calls `metric_collector.py --mode experiment` to export Prometheus time-series between `start` and `end`.
4. Calls `trace_collector.py` (or `enhanced_trace_collector.py` when `--multimodal` is used) to download SkyWalking traces and span-level JSON.
5. Invokes `coverage_tools/collect_coverage_reports.sh` (unless `--no-coverage` is supplied) to dump JaCoCo exec files from every `ts-*` pod and render HTML reports.

Override the following environment variables when needed:

| Variable | Default | Meaning |
|----------|---------|---------|
| `LOG_BASE`, `METRIC_BASE`, `TRACE_BASE`, `API_BASE` | `./log_data` etc. | Destination folders |
| `EVOMASTER_RUNS`, `EVOMASTER_TEST` | `../Evomaster/runs/auth_fixed_10m/...` | Which test file to run |
| `PY_TEST` | `../train-ticket-auto-query/test_all_services.py` | Python scenario driver |
| `TRACE_SIZE`, `TRACE_HOURS`, `TRACE_WORKERS` | `0`, `24`, `24` | Scope of trace scraping |
| `RUN_COVERAGE` | `yes` | Toggle JaCoCo pull |

Use `--master-tests "<path1> <path2> ..."` if you want to execute several EvoMaster suites sequentially.

## Collectors in Detail

### `log_collector.py`
- Discovers Train-Ticket pods via `kubectl get pods -l app`.
- Saves `kubectl logs` (current) and `kubectl logs --previous` output per pod into `log_data/<EXP_NAME>/<pod>.log`.
- Captures cluster events and a summary report.
- Usage: `python3 log_collector.py --experiment-name EXP --namespace default --lines 2000`.

### `metric_collector.py`
- Talks to Prometheus (`PROMETHEUS_URL` env var or auto-discovery via Kubernetes service annotations).
- `--mode instant` dumps snapshot metrics, `--mode range` dumps aligned CSV windows, `--mode experiment` aligns with `--experiment-name`.
- Includes Train-Ticket specific query groups (`collect_train_ticket_specific_metrics`).
- Usage: `python3 metric_collector.py --mode experiment --experiment-name EXP --range-duration 90`.

### `trace_collector.py` and `enhanced_trace_collector.py`
- Query SkyWalking GraphQL endpoints discovered from the `skywalking-oap` service.
- `--multimodal` (default when invoked via `collect_all_modalities.sh`) enriches spans with tags, logs, and cross-modal linkage IDs (`x-evomaster-run-id`, `TT_EM_RUN_ID`).
- Usage: `python3 trace_collector.py --multimodal --size 5000 --hours 12 --max-workers 24 --experiment-name EXP`.

### Coverage Tooling
- `coverage_tools/inject_jacoco_k8s.py`: Patches Kubernetes manifests to mount JaCoCo TCP server agents ahead of time. Run this before deploying Train-Ticket:
  ```bash
  python3 coverage_tools/inject_jacoco_k8s.py \
    --manifests ../train-ticket/deployment/kubernetes-manifests/quickstart-k8s/yamls \
    --mode tcpserver --tcp-port 6300
  ```
- `coverage_tools/collect_coverage_reports.sh`: Dumps exec files, copies service JARs, extracts `BOOT-INF/classes`, and generates XML+HTML reports via `coverage_summary.py`. Set `RUN_ID` to the same label as `EXP_NAME` for easy correlation.

### Experiment Orchestration

`run_experiment.sh --name Lv_S_KILLPOD_order` encapsulates the complete workflow:

1. Deploys Train-Ticket via `../train-ticket/hack/deploy/deploy.sh`.
2. Waits for pods to become Ready (auto-retries CrashLoop pods).
3. Starts the correct chaos action (`CHAOS_KIND=mesh` for Lv_P/Lv_S/Lv_D, `CHAOS_KIND=blade` for Lv_C).
4. Calls `collect_all_modalities.sh --api evomaster`.
5. Stops chaos and writes a summary.

Automate multiple runs with `run_all_experiments.sh`, which iterates over a YAML list of experiment names.

## Integrating Test Drivers

- **EvoMaster traffic**: Provided via `../Evomaster/runs/auth_fixed_10m/EvoMaster_successes_Test.py`. Configure `TT_BASE_URL`, `TT_AUTH_TOKEN`, and optionally `TT_EM_RUN_ID` before collecting.
- **Python scenario driver**: `../train-ticket-auto-query/test_all_services.py` walks through booking, payment, cancel, rebook, logistics, and admin flows using the helper functions in `atomic_queries.py`. Export `TT_BASE_URL`, `TT_USERNAME`, `TT_PASSWORD`, and (optionally) static cookies before running `--api py`.

Both drivers respect `API_RESP_OUT`, which causes every request/response pair to be written as JSON Lines for the API-response modality.

## Prometheus and SkyWalking Endpoints

The collectors auto-discover service endpoints by:

- Inspecting services labeled `app=prometheus` or `component=skywalking`.
- Falling back to `PROMETHEUS_URL` and `SKYWALKING_URL` environment variables.

Override the URLs manually if your cluster uses custom hostnames:

```bash
export PROMETHEUS_URL=http://prometheus-k8s.monitoring:9090
export SKYWALKING_URL=http://skywalking-oap.skywalking:12800
```

## Coverage Post-processing

After `collect_coverage_reports.sh` completes, summarize coverage with:

```bash
python3 coverage_tools/coverage_summary.py \
  --exec-dir coverage_data/<RUN_ID> \
  --service ts-order-service \
  --classes coverage_tools/class_cache/ts-order-service/BOOT-INF/classes \
  --sources ../train-ticket/ts-order-service/src/main/java \
  --out coverage_report/<RUN_ID>/ts-order-service
```

The script emits `summary.txt` (line coverage %) plus JaCoCo XML/HTML artifacts.

## Recommended Reproduction Notes

- Record the git commit hashes of both `TT_collection-scripts` and the Train-Ticket source when you run collectors.
- Store the full invocation (command line, environment variables) in `experiment_manifest.json` next to the exported data folders.
- Mention the Prometheus and SkyWalking URLs used and whether `inject_jacoco_k8s.py` was applied to deployment manifests.
- When submitting updates to the artifact, include the README files from each subdirectory so reviewers can reproduce the workflow with minimal assumptions.

