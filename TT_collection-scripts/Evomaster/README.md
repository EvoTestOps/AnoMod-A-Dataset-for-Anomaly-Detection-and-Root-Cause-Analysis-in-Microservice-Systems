# EvoMaster Traffic Replay

This folder stores the EvoMaster-generated regression suites that drive HTTP traffic during the Train-Ticket data collection campaign. By replaying these suites you can deterministically trigger all Train-Ticket microservices and capture the resulting logs, metrics, traces, API responses, and coverage data.

## Layout

```
Evomaster/
├── runs/
│   └── auth_fixed_10m/
│       ├── EvoMaster_successes_Test.py   # 256 success-path requests
│       └── em_test_utils.py              # helpers shared by generated suites
└── (optional) evomaster.jar              # used when regenerating suites
```

`EvoMaster_successes_Test.py` was generated on 2025-09-29 with a 10-minute search budget and currently covers 825 targets. The helper module contains URI validators used across tests.

## Prerequisites

- Python 3.8+ with `requests` and `timeout_decorator` installed (e.g., `pip install requests timeout-decorator`).
- Access to the Train-Ticket base URL (e.g., the Kubernetes ingress or NodePort).
- A bearer token with admin privileges so that protected endpoints succeed. Export the token through `TT_AUTH_TOKEN`.
- Optional: `TT_EM_RUN_ID` to tag calls and correlate them with downstream collectors (defaults to `em-placeholder`).

Environment variables consumed by the suite:

| Variable | Default | Purpose |
|----------|---------|---------|
| `TT_BASE_URL` | `http://localhost:30467` | Base URL of the Train-Ticket gateway or API |
| `TT_AUTH_TOKEN` | `REPLACE_ME` | JWT or session token inserted into the `Authorization` header |
| `TT_EM_RUN_ID` | `em-placeholder` | Label echoed in the `x-evomaster-run-id` header for trace correlation |

## Running the Test Suite

```bash
cd $(git rev-parse --show-toplevel)/TT_collection-scripts/Evomaster/runs/auth_fixed_10m
export TT_BASE_URL="http://<node-ip>:<node-port>"
export TT_AUTH_TOKEN="$(python3 - <<'PY'
from train_ticket_auto_query.atomic_queries import _login
uid, token = _login()
print(token)
PY
)"
python3 EvoMaster_successes_Test.py
```

Key notes:

- The suite contains 256 independent `unittest` cases named `test_<index>_<description>`. You can run a subset by passing the test name to Python’s `-m unittest`.
- Requests timeout after 60 seconds; if your cluster is slower, adjust `timeout_decorator.timeout` in the file or export `PYTHONFAULTHANDLER=1` for diagnostics.
- Failures usually mean the dataset traffic cannot reach the gateway (DNS/IP issue) or the token expired. Refresh tokens with `_login()` (see `train-ticket-auto-query/atomic_queries.py`).

## Capturing API Responses for the Dataset

`../T-Dataset/collect_all_modalities.sh` sets `API_RESP_OUT` and pipes the EvoMaster output into `api_responses/<exp>/<date>/api_responses.jsonl`. If you run the suite manually and still want JSONL logs:

```bash
export API_RESP_OUT=/tmp/api_recordings/Lv_S_KILLPOD_order
python3 EvoMaster_successes_Test.py | tee em.log
```

`em_test_utils.py` will automatically mirror request/response payloads into `API_RESP_OUT` when the variable is set.

## Integration with the Collectors

- When you call `collect_all_modalities.sh --api evomaster`, the script:
  1. Ensures `PYTHONPATH` includes `Evomaster/runs`.
  2. Starts `EvoMaster_successes_Test.py` (or every file passed through `--master-tests`) either in the background or sequentially.
  3. Waits for completion before finalizing the experiment folder.
- `run_experiment.sh` exposes additional knobs (`--evomaster-test`, `--test-iterations`) if you want to mix suites or loop the same file multiple times.

To add new suites, drop the generated `.py` files under `runs/<scenario>/` and point `collect_all_modalities.sh --evomaster-test` to the path. Always keep the helper module (`em_test_utils.py`) alongside the suite so imports resolve without editing the generated code.

## Re-generation (Optional)

If you wish to regenerate the suite with EvoMaster:

1. Build the Swagger/REST spec (`train-ticket/specs/evolved_merge_v4/.../combined-all-v3.5.json`).
2. Launch the SUT (Kubernetes or Docker).
3. Run EvoMaster in black-box mode:
   ```bash
   java -jar evomaster.jar \
     --bbSwaggerUrl http://<gateway>/swagger/swagger.json \
     --baseUrl http://<gateway> \
     --outputFolder ./runs/<new-name> \
     --maxTime 600
   ```
4. Export `TT_AUTH_TOKEN` before launching EvoMaster so the generated tests embed authenticated calls.

Version the new suite inside `runs/` and remember to capture the EvoMaster command line in your experiment log so reviewers can regenerate it.

