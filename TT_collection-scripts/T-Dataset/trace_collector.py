#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SkyWalking trace collector for the Train-Ticket microservice benchmark.

This script queries the SkyWalking GraphQL API directly to retrieve complete
trace executions (including every span and its detailed attributes) so they
can be aligned with other modalities in the dataset pipeline.

Key behaviours:
  * Auto-discovers the SkyWalking UI NodePort when no URL is supplied.
  * Pulls paginated trace summaries within a configurable lookback window.
  * Filters out ultra-short traces by default to avoid single-span noise.
  * Fetches full span graphs in parallel and annotates parent/child links.
  * Emits a single JSON artifact under trace_data/ with rich metadata.

Command-line usage is designed to stay compatible with run_all_experiments.sh
and collect_all_modalities.sh.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests


# --------------------------------------------------------------------------- #
# Data containers                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class TraceSummary:
    trace_id: str
    duration_ms: int
    start_ms: int
    is_error: bool
    endpoint_names: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "duration_ms": self.duration_ms,
            "start_time_utc": utc_iso(self.start_ms),
            "start_timestamp_ms": self.start_ms,
            "is_error": self.is_error,
            "endpoint_names": self.endpoint_names,
        }


@dataclass
class SpanRecord:
    node_id: str
    trace_id: str
    segment_id: str
    span_id: int
    parent_span_id: int
    parent_node_id: Optional[str]
    service_code: str
    service_instance: str
    start_ms: int
    end_ms: int
    endpoint_name: Optional[str]
    span_type: str
    peer: Optional[str]
    component: Optional[str]
    is_error: bool
    layer: Optional[str]
    tags: List[Dict[str, str]]
    logs: List[Dict[str, object]]
    refs: List[Dict[str, object]]
    children_ids: List[str]
    depth: int

    def to_dict(self) -> Dict[str, object]:
        duration = max(0, self.end_ms - self.start_ms)
        tags_map = {item["key"]: item.get("value") for item in self.tags}
        log_records = [
            {
                "time_utc": utc_iso(entry["time"]),
                "time_ms": entry["time"],
                "data": {kv["key"]: kv.get("value") for kv in entry.get("data", [])},
            }
            for entry in self.logs
        ]
        return {
            "node_id": self.node_id,
            "trace_id": self.trace_id,
            "segment_id": self.segment_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "parent_node_id": self.parent_node_id,
            "depth": self.depth,
            "children_node_ids": self.children_ids,
            "service_code": self.service_code,
            "service_instance": self.service_instance,
            "start_time_utc": utc_iso(self.start_ms),
            "end_time_utc": utc_iso(self.end_ms),
            "start_timestamp_ms": self.start_ms,
            "end_timestamp_ms": self.end_ms,
            "duration_ms": duration,
            "endpoint_name": self.endpoint_name,
            "type": self.span_type,
            "peer": self.peer,
            "component": self.component,
            "layer": self.layer,
            "is_error": self.is_error,
            "tags": self.tags,
            "tags_map": tags_map,
            "logs": log_records,
            "refs": self.refs,
        }


def utc_iso(epoch_millis: int) -> str:
    """Convert epoch milliseconds to an ISO-8601 UTC string."""
    try:
        return dt.datetime.utcfromtimestamp(epoch_millis / 1000.0).isoformat() + "Z"
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Collector implementation                                                    #
# --------------------------------------------------------------------------- #

class SkyWalkingTraceCollector:
    GRAPHQL_TRACE_LIST = """
    query queryBasicTraces($condition: TraceQueryCondition!) {
      data: queryBasicTraces(condition: $condition) {
        total
        traces {
          traceIds
          duration
          start
          isError
          endpointNames
        }
      }
    }
    """.strip()

    GRAPHQL_TRACE_DETAIL = """
    query queryTrace($traceId: ID!) {
      trace: queryTrace(traceId: $traceId) {
        spans {
          traceId
          segmentId
          spanId
          parentSpanId
          serviceCode
          serviceInstanceName
          startTime
          endTime
          endpointName
          type
          peer
          component
          isError
          layer
          tags { key value }
          logs { time data { key value } }
          refs { traceId parentSegmentId parentSpanId type }
        }
      }
    }
    """.strip()

    def __init__(
        self,
        skywalking_url: Optional[str] = None,
        output_dir: str = "trace_data",
        namespace: Optional[str] = None,
        http_timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.namespace = namespace or os.environ.get("TRAIN_TICKET_NAMESPACE", "default")
        self.skywalking_url = skywalking_url or self._discover_skywalking_url()
        self.graphql_endpoint = f"{self.skywalking_url.rstrip('/')}/graphql"
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.http_timeout = http_timeout
        self.max_retries = max_retries

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("Using SkyWalking endpoint: %s", self.graphql_endpoint)

    # ------------------------------------------------------------------ #
    # Discovery helpers                                                  #
    # ------------------------------------------------------------------ #
    def _discover_skywalking_url(self) -> str:
        """Locate the SkyWalking UI NodePort service via kubectl or env vars."""
        env_url = os.environ.get("SKYWALKING_UI_URL") or os.environ.get("SKYWALKING_BASE_URL")
        if env_url:
            return env_url.rstrip("/")

        service_name = os.environ.get("SKYWALKING_UI_SERVICE", "skywalking-ui")
        namespace = self.namespace

        try:
            svc_json = self._run_kubectl_json(
                ["get", "svc", service_name, "-n", namespace, "-o", "json"]
            )
            node_port = None
            for port in svc_json.get("spec", {}).get("ports", []):
                if port.get("nodePort"):
                    node_port = port["nodePort"]
                    break
            if node_port is None:
                raise RuntimeError("SkyWalking NodePort not found in service definition")

            node_info = self._run_kubectl_json(["get", "nodes", "-o", "json"])
            node_ip = None
            for item in node_info.get("items", []):
                for address in item.get("status", {}).get("addresses", []):
                    if address.get("type") == "InternalIP":
                        node_ip = address.get("address")
                        break
                if node_ip:
                    break
            if not node_ip:
                raise RuntimeError("Unable to determine Kubernetes node InternalIP")

            return f"http://{node_ip}:{node_port}"
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning(
                "Falling back to default SkyWalking URL due to discovery issue: %s", exc
            )
            return "http://127.0.0.1:30005"

    @staticmethod
    def _run_kubectl_json(args: List[str]) -> Dict[str, object]:
        """Execute kubectl and parse JSON output."""
        cmd = ["kubectl", *args]
        completed = subprocess.run(
            cmd,
            check=True,
            text=True,
            capture_output=True,
        )
        return json.loads(completed.stdout)

    # ------------------------------------------------------------------ #
    # GraphQL helpers                                                    #
    # ------------------------------------------------------------------ #
    def _post_graphql(self, query: str, variables: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        """Send a GraphQL request with simple retries."""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(
                    self.graphql_endpoint,
                    json=payload,
                    timeout=self.http_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if "errors" in data:
                    raise RuntimeError(data["errors"])
                return data.get("data") or {}
            except Exception as exc:  # pylint: disable=broad-except
                wait = min(3 * attempt, 10)
                if attempt == self.max_retries:
                    raise RuntimeError(f"GraphQL request failed after {attempt} attempts: {exc}") from exc
                self.logger.warning(
                    "GraphQL request failed (attempt %s/%s): %s; retrying in %ss",
                    attempt,
                    self.max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)
        return {}

    # ------------------------------------------------------------------ #
    # Trace retrieval                                                    #
    # ------------------------------------------------------------------ #
    def fetch_trace_summaries(
        self,
        limit: int,
        hours_back: float,
        min_duration_ms: int = 1,
        query_order: str = "BY_START_TIME",
        page_size: int = 200,
    ) -> Tuple[List[TraceSummary], int]:
        """Fetch paginated trace summaries within the lookback window."""
        page_size = max(1, min(page_size, limit if limit > 0 else page_size))
        lookback = max(hours_back, 0.1)

        end_dt = dt.datetime.utcnow()
        start_dt = end_dt - dt.timedelta(hours=lookback)
        step = "MINUTE" if lookback <= 12 else "HOUR"

        end_dt = end_dt.replace(second=0, microsecond=0)
        start_dt = start_dt.replace(second=0, microsecond=0)
        if step == "HOUR":
            end_dt = end_dt.replace(minute=0)
            start_dt = start_dt.replace(minute=0)
            time_format = "%Y-%m-%d %H"
        else:
            time_format = "%Y-%m-%d %H%M"

        summaries: List[TraceSummary] = []
        seen: set[str] = set()
        total_available = 0

        page = 1
        while True:
            if limit and len(summaries) >= limit:
                break

            condition = {
                "queryDuration": {
                    "start": start_dt.strftime(time_format),
                    "end": end_dt.strftime(time_format),
                    "step": step,
                },
                "traceState": "ALL",
                "queryOrder": query_order,
                "minTraceDuration": max(0, int(min_duration_ms)),
                "paging": {
                    "pageNum": page,
                    "pageSize": page_size,
                },
            }

            data = self._post_graphql(self.GRAPHQL_TRACE_LIST, {"condition": condition})
            result = data.get("data") if data else None
            traces = (result or {}).get("traces") or []
            total_available = (result or {}).get("total", 0)

            if not traces:
                break

            for entry in traces:
                trace_ids = entry.get("traceIds") or []
                if not trace_ids:
                    continue
                trace_id = trace_ids[0]
                if trace_id in seen:
                    continue
                seen.add(trace_id)

                try:
                    duration = int(entry.get("duration", 0))
                except (TypeError, ValueError):
                    duration = 0
                try:
                    start_ms = int(entry.get("start", 0))
                except (TypeError, ValueError):
                    start_ms = 0

                summaries.append(
                    TraceSummary(
                        trace_id=trace_id,
                        duration_ms=duration,
                        start_ms=start_ms,
                        is_error=bool(entry.get("isError", False)),
                        endpoint_names=list(entry.get("endpointNames") or []),
                    )
                )

                if limit and len(summaries) >= limit:
                    break

            if len(traces) < page_size:
                break
            page += 1

        return summaries[:limit] if limit else summaries, total_available

    def fetch_trace_spans(self, trace_id: str) -> List[Dict[str, object]]:
        """Retrieve the full span list for a single trace."""
        data = self._post_graphql(self.GRAPHQL_TRACE_DETAIL, {"traceId": trace_id})
        spans = (data.get("trace") or {}).get("spans") or []
        if not spans:
            self.logger.debug("Trace %s returned no spans", trace_id)
        return spans

    # ------------------------------------------------------------------ #
    # Span processing                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_span_records(spans: Iterable[Dict[str, object]]) -> Tuple[List[SpanRecord], List[str]]:
        """Annotate spans with node IDs, hierarchy, and depth."""
        nodes: Dict[str, Dict[str, object]] = {}
        parents: Dict[str, Optional[str]] = {}
        children: Dict[str, List[str]] = {}

        # Stage 1: normalise
        for span in spans:
            segment_id = span.get("segmentId")
            span_id = span.get("spanId")
            if segment_id is None or span_id is None:
                continue
            node_id = f"{segment_id}:{span_id}"
            span["_node_id"] = node_id
            nodes[node_id] = span
            children.setdefault(node_id, [])

        # Stage 2: relationships
        for span in spans:
            node_id = span.get("_node_id")
            if not node_id:
                continue
            parent_node: Optional[str] = None
            parent_span_id = span.get("parentSpanId", -1)
            segment_id = span.get("segmentId")
            if isinstance(parent_span_id, int) and parent_span_id >= 0:
                parent_node = f"{segment_id}:{parent_span_id}"
            else:
                refs = span.get("refs") or []
                if refs:
                    ref = refs[0]
                    parent_seg = ref.get("parentSegmentId")
                    parent_span = ref.get("parentSpanId")
                    if parent_seg is not None and parent_span is not None:
                        parent_node = f"{parent_seg}:{parent_span}"
            parents[node_id] = parent_node
            if parent_node and parent_node in children:
                children[parent_node].append(node_id)

        # Stage 3: compute depth via BFS
        depths: Dict[str, int] = {}
        roots = [node_id for node_id, parent in parents.items() if parent not in nodes]
        queue: List[Tuple[str, int]] = [(node_id, 0) for node_id in roots]
        while queue:
            current, depth = queue.pop(0)
            depths[current] = depth
            for child in children.get(current, []):
                queue.append((child, depth + 1))

        span_records: List[SpanRecord] = []
        for span in spans:
            node_id = span.get("_node_id")
            if not node_id:
                continue
            record = SpanRecord(
                node_id=node_id,
                trace_id=span.get("traceId"),
                segment_id=span.get("segmentId"),
                span_id=span.get("spanId"),
                parent_span_id=span.get("parentSpanId"),
                parent_node_id=parents.get(node_id),
                service_code=span.get("serviceCode"),
                service_instance=span.get("serviceInstanceName"),
                start_ms=span.get("startTime", 0),
                end_ms=span.get("endTime", 0),
                endpoint_name=span.get("endpointName"),
                span_type=span.get("type"),
                peer=span.get("peer"),
                component=span.get("component"),
                is_error=bool(span.get("isError", False)),
                layer=span.get("layer"),
                tags=list(span.get("tags") or []),
                logs=list(span.get("logs") or []),
                refs=list(span.get("refs") or []),
                children_ids=list(children.get(node_id, [])),
                depth=depths.get(node_id, 0),
            )
            span_records.append(record)

        return span_records, roots

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def collect_traces(
        self,
        size: int,
        hours: float,
        max_workers: int,
        min_duration_ms: int,
        experiment_name: Optional[str] = None,
        query_order: str = "BY_START_TIME",
    ) -> Path:
        """Coordinate the complete trace collection workflow."""
        requested_label = "all available" if size <= 0 else str(size)
        self.logger.info(
            "Collecting SkyWalking traces: target=%s hours=%s min_duration=%sms workers=%s",
            requested_label,
            hours,
            min_duration_ms,
            max_workers,
        )
        summaries, total_available = self.fetch_trace_summaries(
            limit=size,
            hours_back=hours,
            min_duration_ms=min_duration_ms,
            query_order=query_order,
        )

        if not summaries:
            raise RuntimeError("No trace summaries were returned by SkyWalking.")

        self.logger.info("Trace summaries obtained: %s (of total %s)", len(summaries), total_available)

        collected: List[Dict[str, object]] = []
        services_seen: set[str] = set()

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            future_map = {
                executor.submit(self.fetch_trace_spans, summary.trace_id): summary for summary in summaries
            }
            for future in concurrent.futures.as_completed(future_map):
                summary = future_map[future]
                try:
                    spans = future.result()
                except Exception as exc:  # pylint: disable=broad-except
                    self.logger.error("Failed to fetch trace %s: %s", summary.trace_id, exc)
                    continue

                span_records, roots = self._build_span_records(spans)
                if not span_records:
                    self.logger.debug("Trace %s had no usable span records, skipping", summary.trace_id)
                    continue

                services = sorted({rec.service_code for rec in span_records if rec.service_code})
                services_seen.update(services)

                collected.append(
                    {
                        "summary": summary.to_dict(),
                        "span_count": len(span_records),
                        "services_involved": services,
                        "root_span_node_ids": roots,
                        "spans": [rec.to_dict() for rec in span_records],
                    }
                )

        if not collected:
            raise RuntimeError("All trace detail requests failed; nothing collected.")

        timestamp = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_exp = ""
        if experiment_name:
            safe_exp = re.sub(r"[^A-Za-z0-9_.-]", "_", experiment_name.strip())

        filename = (
            f"{safe_exp}_skywalking_traces_{timestamp}.json"
            if safe_exp
            else f"skywalking_traces_{timestamp}.json"
        )
        output_path = self.output_dir / filename

        payload = {
            "metadata": {
                "generated_at": dt.datetime.utcnow().isoformat() + "Z",
                "lookback_hours": hours,
                "requested_trace_limit": size,
                "min_trace_duration_ms": min_duration_ms,
                "collected_traces": len(collected),
                "available_total": total_available,
                "services_discovered": sorted(services_seen),
                "experiment_name": experiment_name,
                "skywalking_base_url": self.skywalking_url,
                "skywalking_graphql": self.graphql_endpoint,
            },
            "traces": collected,
        }

        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)

        self.logger.info("Trace collection written to %s", output_path)
        return output_path


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect complete SkyWalking traces (with span detail) for multimodal datasets."
    )
    parser.add_argument(
        "--size",
        type=int,
        default=0,
        help="Maximum number of traces to collect (0 = no limit).",
    )
    parser.add_argument("--hours", type=float, default=24, help="Lookback window in hours.")
    parser.add_argument(
        "--min-duration",
        type=int,
        default=1,
        help="Minimum trace duration in milliseconds (filters out trivial spans).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Concurrent workers for span detail queries.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="trace_data",
        help="Directory to store the resulting JSON artifact.",
    )
    parser.add_argument(
        "--experiment-name",
        type=str,
        help="Optional experiment name for downstream alignment.",
    )
    parser.add_argument(
        "--skywalking-url",
        type=str,
        help="Override the auto-discovered SkyWalking UI base URL.",
    )
    parser.add_argument(
        "--multimodal",
        action="store_true",
        help="Alias flag kept for compatibility with orchestration scripts.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    collector = SkyWalkingTraceCollector(
        skywalking_url=args.skywalking_url,
        output_dir=args.output_dir,
    )

    try:
        collector.collect_traces(
            size=args.size,
            hours=max(0.1, float(args.hours)),
            max_workers=max(1, args.max_workers),
            min_duration_ms=max(0, args.min_duration),
            experiment_name=args.experiment_name,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logging.error("Trace collection failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
