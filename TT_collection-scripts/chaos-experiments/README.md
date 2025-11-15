# Chaos Experiments (Train-Ticket Dataset)

This directory defines repeatable Chaos Mesh scenarios that were used to inject the four anomaly levels (performance, service, database, and code) required by the Train-Ticket multi-modal dataset. Every run must be paired with the collectors under `../T-Dataset` so that logs, metrics, traces, API responses, and coverage stay time-aligned.

## Directory Overview

```
chaos-experiments/
├── Lv_P_CPU_preserve.yaml          # CPU contention on ts-preserve-service
├── Lv_P_DISKIO_preserve.yaml       # Disk I/O pressure on ts-preserve-service
├── Lv_P_NETLOSS_preserve.yaml      # Network packet loss between preserve services
├── Lv_S_KILLPOD_*.yaml             # Service-level pod kill scenarios
├── Lv_S_HTTPABORT_preserve.yaml    # HTTP abort fault injection
├── Lv_S_DNSFAIL_preserve_no_order.yaml
├── Lv_D_CACHELIMIT.yaml            # Cache and DB pressure
├── Lv_D_CONNECTION_POOL_exhaustion.yaml
├── Lv_D_TRANSACTION_timeout.yaml
├── README.md
├── start_chaos.sh                  # Wrapper for kubectl apply -f <fault>.yaml
└── stop_chaos.sh                   # Wrapper for kubectl delete -f <fault>.yaml
```

| YAML file | Anomaly level | Impacted component |
|-----------|---------------|--------------------|
| `Lv_P_CPU_preserve.yaml` | Performance | CPU contention on `ts-preserve-service` |
| `Lv_P_DISKIO_preserve.yaml` | Performance | Disk I/O bottleneck on preserve pods |
| `Lv_P_NETLOSS_preserve.yaml` | Performance | Network packet loss between preserve and downstream services |
| `Lv_S_KILLPOD_gateway.yaml` | Service | Kills `ts-gateway-service` pods to emulate outages |
| `Lv_S_KILLPOD_order.yaml` | Service | Kills `ts-order-service` pods (order unavailability) |
| `Lv_S_KILLPOD_preserve.yaml` | Service | Removes preserve pods to trigger retries |
| `Lv_S_HTTPABORT_preserve.yaml` | Service | Forces HTTP aborts before entering business logic |
| `Lv_S_DNSFAIL_preserve_no_order.yaml` | Service | Corrupts DNS resolve path from preserve -> order |
| `Lv_D_CACHELIMIT.yaml` | Database | Eliminates cache entries to cause repeated DB hits |
| `Lv_D_CONNECTION_POOL_exhaustion.yaml` | Database | Exhausts JDBC pools to mimic DB outage |
| `Lv_D_TRANSACTION_timeout.yaml` | Database | Forces long-running SQL transactions to time out |

Code-level anomalies are orchestrated by ChaosBlade inside `run_experiment.sh`; see the `T-Dataset/README.md` for those instructions.

## Prerequisites

- Kubernetes cluster with Train-Ticket deployed in the namespace specified by `NAMESPACE` (defaults to `default`).
- Chaos Mesh installed (`stresschaos`, `networkchaos`, `iochaos`, `podchaos` CRDs ready).
- `kubectl` configured for the cluster.
- Access to `../T-Dataset/collect_all_modalities.sh` for synchronized data capture.
- Optional: ChaosBlade CLI for Lv_C* experiments triggered by `run_experiment.sh`.

## Running a Single Chaos Experiment

1. Change into this directory:
   ```bash
   cd $(git rev-parse --show-toplevel)/TT_collection-scripts/chaos-experiments
   ```
2. Export the namespace if you do not use `default`:
   ```bash
   export NAMESPACE=tt-chaos
   ```
3. Start the desired scenario (scripts wrap `kubectl apply` and automatically point to the `chaos-mesh` namespace if needed):
   ```bash
   ./start_chaos.sh Lv_P_CPU_preserve.yaml
   ```
4. In another shell, trigger multi-modal collection while the fault is active:
   ```bash
   cd ../T-Dataset
   ./collect_all_modalities.sh --name Lv_P_CPU_preserve_$(date -u +%Y%m%dT%H%M%SZ) --api evomaster
   ```
   This runs EvoMaster traffic (or Python flows), metrics/log collectors, trace scrapers, and coverage pullers. Use `--trace-size`, `--trace-hours`, or `--no-coverage` to match your experiment budget.
5. After the capture finishes, stop the chaos action:
   ```bash
   cd ../chaos-experiments
   ./stop_chaos.sh Lv_P_CPU_preserve.yaml
   ```
6. Record the experiment metadata (fault file, timestamps, collector arguments) beside the generated data folders so that other researchers can cross-reference runs.

### Monitoring Tips

```bash
kubectl -n chaos-mesh get stresschaos,networkchaos,podchaos,iochaos
kubectl -n chaos-mesh describe stresschaos cpu-preserve
kubectl -n ${NAMESPACE:-default} get pods | grep ts-
```

Delete stuck resources with `kubectl delete <kind> <name> -n chaos-mesh` if the helper scripts fail.

## Automated Campaigns

`../T-Dataset/run_experiment.sh --name Lv_D_TRANSACTION_timeout` performs the following for you:

1. Rebuilds or validates the Train-Ticket deployment (`train-ticket/hack/deploy`).
2. Waits for pods to reach Ready state.
3. Calls `start_chaos.sh` or ChaosBlade depending on the `Lv_*` prefix.
4. Runs EvoMaster-generated traffic (`Evomaster/runs/.../EvoMaster_successes_Test.py`) once or multiple times.
5. Invokes `collect_all_modalities.sh` to capture all five data modalities.
6. Calls `stop_chaos.sh` or `blade destroy` for cleanup.

Use `run_all_experiments.sh` to iterate over a curated list of YAML files and automatically label each run.

## Reproducibility Checklist

- Keep `git rev-parse HEAD` of both the Train-Ticket source and this repository for the submission metadata.
- Store the executed YAML file plus any manual parameter edits next to the exported data (`T-Dataset/experiment_manifest.json` is a common place).
- For database-level faults, also log current MySQL resource usage from `kubectl top pods` to contextualize the anomaly.
- Mention the Chaos Mesh commit or image tags in your final artifact so reviewers can replay the same conditions.

Following these steps allows anyone to re-run the same chaos profiles and regenerate the multimodal dataset.
