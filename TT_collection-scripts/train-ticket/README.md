# Train-Ticket Deployment Assets

This folder mirrors the Train-Ticket microservice system we used to generate the multi-modal anomaly dataset. Re-deploying the system with the instructions below ensures your environment matches the one used during data collection.

## Structure

```
train-ticket/
├── deployment/
│   └── kubernetes-manifests/quickstart-k8s/yamls/
│       ├── sw_deploy.tcpserver.includes.yaml   # SkyWalking + JaCoCo includes
│       └── sw_deploy.with-jacoco.yaml          # Pre-patched manifest
├── hack/deploy/
│   ├── deploy.sh            # High-level orchestration script
│   ├── gen-mysql-secret.sh  # Generates the DB credentials secret
│   └── utils.sh             # Helper functions (deploy_* targets)
└── specs/
    └── evolved_merge_v4/combined/v3.5/combined-all-v3.5.json
```

The Swagger specification under `specs/` was used by EvoMaster to generate traffic, so keep it in sync with the running cluster.

## Prerequisites

- Kubernetes 1.22+ cluster with `kubectl` admin access.
- A container registry/mirror reachable from the cluster to pull all `ts-*` service images.
- SkyWalking, Prometheus, and Chaos Mesh namespaces ready if you plan to collect traces, metrics, or inject faults.
- Optional: apply `coverage_tools/inject_jacoco_k8s.py` (from `../T-Dataset`) to the manifests before deployment so every service exposes JaCoCo TCP ports for coverage dumps.

## Quick Deployment (Recommended Path)

1. Generate the MySQL secret once:
   ```bash
   cd $(git rev-parse --show-toplevel)/TT_collection-scripts/train-ticket/hack/deploy
   ./gen-mysql-secret.sh default  # change namespace if needed
   ```
2. Deploy the full stack (infrastructure, services, tracing, monitoring):
   ```bash
   ./deploy.sh default "--all --with-monitoring --with-tracing"
   ```
   - `deploy_infrastructures` sets up Zookeeper, Kafka, RabbitMQ, Redis, etc.
   - `deploy_tt_mysql_each_service` provisions dedicated MySQL instances per microservice when `--independent-db` is used; otherwise a shared DB is deployed.
   - `deploy_tt_dp_sw` wires the SkyWalking agent sidecars and JaCoCo agent includes defined in `sw_deploy.tcpserver.includes.yaml`.
   - `deploy_monitoring` installs Prometheus/Grafana for the metric collectors.

3. Confirm readiness:
   ```bash
   kubectl -n default get pods -l app | grep ts-
   ```
   Wait until every pod shows `Running` with `1/1` containers Ready before launching collectors or chaos experiments.

## Manifests

- `sw_deploy.with-jacoco.yaml`: Pre-integrated SkyWalking deployment plus JaCoCo TCP server agent configuration. Apply it after injecting any custom includes:
  ```bash
  kubectl apply -f deployment/kubernetes-manifests/quickstart-k8s/yamls/sw_deploy.with-jacoco.yaml
  ```
- `sw_deploy.tcpserver.includes.yaml`: Snippet referenced by `inject_jacoco_k8s.py` to mount `/jacoco` volumes, TCP server agents, and `JAVA_TOOL_OPTIONS`. Keep this file untouched so the injector can match and merge blocks idempotently.

If you need to tweak resource requests/limits, duplicate the YAML files and note the changes in your experiment log so reviewers can replicate them.

## Maintaining Alignment with the Dataset

- Always capture the `git rev-parse HEAD` of this directory whenever you redeploy; the manifest revisions are part of the dataset artifact metadata.
- Document the namespace and ingress/Nginx configuration you use because the collectors (metrics, logs, traces) rely on label selectors such as `app=ts-user-service`.
- When enabling JaCoCo, ensure the TCP port (`6300` by default) is open inside the cluster and matches the `collect_coverage_reports.sh` configuration.
- If you regenerate the Swagger spec (e.g., after modifying APIs), update `specs/evolved_merge_v4/.../combined-all-v3.5.json` and re-run EvoMaster so the traffic matches the deployed surface.

By following these steps you can redeploy Train-Ticket exactly as it was configured for the dataset, enabling reproducible multi-modal data collection.

