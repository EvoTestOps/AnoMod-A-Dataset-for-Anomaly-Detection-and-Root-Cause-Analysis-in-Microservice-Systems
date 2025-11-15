# Train-Ticket Auto Query Suite

These Python utilities generate deterministic user journeys that complement the EvoMaster traffic when collecting the multi-modal dataset. Use them whenever you prefer readable, hand-crafted flows (e.g., to reproduce specific booking or admin sequences).

## Contents

```
train-ticket-auto-query/
├── atomic_queries.py    # Reusable primitives for login, ticket search, order mgmt, admin ops
└── test_all_services.py # High-level orchestrator covering every microservice
```

`atomic_queries.py` exposes helper functions such as `_login`, `_query_high_speed_ticket`, `_pay_one_order`, `_rebook_ticket`, `_query_admin_basic_price`, etc. `test_all_services.py` imports these helpers and coordinates end-to-end flows across core, auxiliary, admin, and extended services.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TT_BASE_URL` | `http://127.0.0.1:30467` (from `atomic_queries.py`) | Gateway URL |
| `TT_USERNAME` / `TT_PASSWORD` | `fdse_microservice` / `111111` | Credentials used by `_login` |
| `TT_AUTH_TOKEN` | None | Overrides bearer token in headers if already known |
| `TT_SESSION_COOKIE` | None | Injects session cookie into every request |
| `TT_USER_UUID` | `4d2a46c7-71cb-4cf1-b5bb-b68406d9da6f` | Used when canceling or rebooking orders |
| `API_RESP_OUT` | None | Path to store JSON Lines of every request/response (same format as EvoMaster captures) |

Export these variables before running the scripts to keep metadata consistent with the rest of the dataset.

## Running the Full Scenario

```bash
cd $(git rev-parse --show-toplevel)/TT_collection-scripts/train-ticket-auto-query
python3 -m venv .venv && source .venv/bin/activate
pip install requests

export TT_BASE_URL="http://<gateway>:<port>"
export TT_USERNAME="fdse_microservice"
export TT_PASSWORD="111111"
export API_RESP_OUT="../T-Dataset/api_responses/Lv_S_KILLPOD_order/$(date -u +%Y%m%d)"

python3 test_all_services.py --iterations 1
```

What the script covers:

- **Core business services**: login, high-speed and regular ticket search, travel plan queries, order creation, payment, cancellation, execution, and rebooking.
- **Auxiliary services**: contacts, assurances, food, consign, route, price, and user profile endpoints.
- **Admin services**: admin price/config queries, travel admin APIs, ticket/route batch operations.
- **Extended flows**: logistics, notification services, MQ/mail test endpoints, etc.

It logs every HTTP status to `stdout` and records failures so you can quickly spot regressions before starting multi-modal collection.

## Using Atomic Helpers Directly

When you need a targeted call (e.g., regenerating a single order event), import the module in an interactive shell:

```python
from atomic_queries import _login, _pay_one_order
uid, token = _login()
_pay_one_order(order_id="5f3b...", trip_id="D1345", headers={"Authorization": f"Bearer {token}"})
```

All helpers accept a `headers` dictionary and optional keyword arguments for IDs, dates, or query parameters. They return parsed responses (lists, tuples, or dictionaries) to ease chaining inside notebooks or troubleshooting scripts.

## Integration with `collect_all_modalities.sh`

Passing `--api py` to `../T-Dataset/collect_all_modalities.sh` executes `test_all_services.py` instead of the EvoMaster suite:

```bash
cd ../T-Dataset
./collect_all_modalities.sh --api py --name manual_flow_$(date -u +%Y%m%dT%H%M%SZ)
```

- `TEST_ITERATIONS` controls how many times the script runs inside a single experiment.
- Headers (`Authorization`, `Cookie`) are taken from the environment variables documented above, ensuring perfect alignment with EvoMaster traces when both drivers are combined.

Keep this README with the dataset so reviewers can replay the exact query mix without reverse-engineering the scripts.

