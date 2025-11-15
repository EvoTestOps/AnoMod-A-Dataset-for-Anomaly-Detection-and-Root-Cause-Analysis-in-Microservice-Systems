#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Train-Ticket monitoring metric collector built on Prometheus.
Captures system, application, and infrastructure indicators for anomaly analysis.
"""

import os
import json
import datetime
import requests
import time
import logging
import subprocess
from pathlib import Path
from urllib.parse import urljoin

class MetricCollector:
    def __init__(self, prometheus_url=None, output_dir="metric_data"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        # Configure logger
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        # Auto-discover Prometheus URL if not provided
        if prometheus_url is None:
            self.prometheus_url = self.get_prometheus_url()
        else:
            self.prometheus_url = prometheus_url
        
        # Metric categories aligned with performance, service, and database anomaly levels
        self.metric_categories = {
            'performance': {
                'description': 'Performance-level metrics (CPU contention, packet loss, memory pressure, disk I/O)',
                'metrics': [
                    # CPU metrics
                    'node_cpu_seconds_total',
                    'container_cpu_usage_seconds_total',
                    'rate(node_cpu_seconds_total[5m])',
                    'node_load5',
                    
                    # Memory metrics
                    'node_memory_MemAvailable_bytes',
                    'node_memory_MemTotal_bytes',
                    'node_memory_MemFree_bytes',
                    'container_memory_usage_bytes',
                    'container_memory_working_set_bytes',
                    'container_spec_memory_limit_bytes',
                    
                    # Disk I/O metrics
                    'node_filesystem_avail_bytes',
                    'node_filesystem_size_bytes',
                    'rate(node_disk_read_bytes_total[5m])',
                    'rate(node_disk_written_bytes_total[5m])',
                    'node_disk_io_time_seconds_total',
                    
                    # Network metrics
                    'node_network_receive_bytes_total',
                    'node_network_transmit_bytes_total',
                    'node_network_receive_drop_total',
                    'node_network_transmit_drop_total',
                    'node_network_receive_errs_total',
                    'node_network_transmit_errs_total',
                    'container_network_receive_errors_total',
                    'container_network_transmit_errors_total',
                ]
            },
            'service': {
                'description': 'Service-level metrics (availability, timeouts, dependency failures)',
                'metrics': [
                    # Service availability and status
                    'up',
                    'http_requests_total',
                    
                    # Process health
                    'process_open_fds',
                    'process_cpu_seconds_total',
                    'process_resident_memory_bytes',
                    'container_processes',
                    
                    # Container state
                    'container_memory_failcnt',
                    'container_cpu_cfs_throttled_periods_total',
                ]
            },
            'database': {
                'description': 'Database-level metrics (storage pressure, I/O bottlenecks)',
                'metrics': [
                    # Storage utilization
                    'node_filesystem_avail_bytes',
                    'node_filesystem_size_bytes',
                    'volume_manager_total_volumes',
                    
                    # Process file descriptors (proxy for DB connections)
                    'process_open_fds',
                    'process_max_fds',
                ]
            }
        }
        
        # Maintain backwards compatibility
        self.key_metrics = []
        for category in self.metric_categories.values():
            self.key_metrics.extend(category['metrics'])
    
    def get_prometheus_url(self):
        """Automatically resolve the Prometheus service URL."""
        try:
            # Query Prometheus service definition
            result = subprocess.run([
                'kubectl', 'get', 'svc', 'prometheus', '-n', 'kube-system', '-o', 'json'
            ], capture_output=True, text=True, check=True)
            
            svc_data = json.loads(result.stdout)
            
            # Extract NodePort
            for port in svc_data['spec']['ports']:
                if port['port'] == 9090:
                    node_port = port['nodePort']
                    break
            else:
                raise Exception("Prometheus port not found")
            
            # Discover node IP
            result = subprocess.run([
                'kubectl', 'get', 'nodes', '-o', 'json'
            ], capture_output=True, text=True, check=True)
            
            nodes_data = json.loads(result.stdout)
            node_ip = None
            
            for node in nodes_data['items']:
                for address in node['status']['addresses']:
                    if address['type'] == 'InternalIP':
                        node_ip = address['address']
                        break
                if node_ip:
                    break
            
            if not node_ip:
                raise Exception("Node IP not found")
            
            prometheus_url = f"http://{node_ip}:{node_port}"
            self.logger.info(f"Auto-discovered Prometheus URL: {prometheus_url}")
            return prometheus_url
            
        except Exception as e:
            self.logger.error(f"Failed to auto-discover Prometheus URL: {e}")
            # Fall back to default
            return "http://localhost:30003"
    
    def query_prometheus(self, query, timestamp=None):
        """Query a single Prometheus metric."""
        try:
            url = urljoin(self.prometheus_url, '/api/v1/query')
            params = {'query': query}
            
            if timestamp:
                params['time'] = timestamp
            
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            return response.json()
            
        except Exception as e:
            self.logger.error(f"Prometheus query failed [{query}]: {e}")
            return None
    
    def query_prometheus_range(self, query, start_time, end_time, step='15s'):
        """Query Prometheus metrics across a time range."""
        try:
            url = urljoin(self.prometheus_url, '/api/v1/query_range')
            params = {
                'query': query,
                'start': start_time,
                'end': end_time,
                'step': step
            }
            
            response = requests.get(url, params=params, timeout=60)
            response.raise_for_status()
            
            return response.json()
            
        except Exception as e:
            self.logger.error(f"Prometheus range query failed [{query}]: {e}")
            return None
    
    def collect_instant_metrics(self):
        """Collect instant metrics snapshot."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        metrics_file = self.output_dir / f"instant_metrics_{timestamp}.json"
        
        collected_metrics = {}
        
        self.logger.info("Collecting instant metrics...")
        
        for metric in self.key_metrics:
            self.logger.info(f"Collecting metric: {metric}")
            result = self.query_prometheus(metric)
            
            if result and result.get('status') == 'success':
                collected_metrics[metric] = result['data']
            else:
                self.logger.warning(f"Metric {metric} failed or returned no data")
                collected_metrics[metric] = None
            
            # Avoid hammering the API
            time.sleep(0.1)
        
        # Persist results
        with open(metrics_file, 'w', encoding='utf-8') as f:
            json.dump({
                'timestamp': timestamp,
                'collection_time': datetime.datetime.now().isoformat(),
                'metrics': collected_metrics
            }, f, indent=2, ensure_ascii=False)
        
        self.logger.info(f"Instant metrics written to: {metrics_file}")
        return str(metrics_file)
    
    def collect_range_metrics(self, duration_minutes=60, step='15s'):
        """Collect metrics over a configurable window."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        metrics_file = self.output_dir / f"range_metrics_{duration_minutes}min_{timestamp}.json"
        
        # Compute time window
        end_time = datetime.datetime.now()
        start_time = end_time - datetime.timedelta(minutes=duration_minutes)
        
        # Convert to Unix timestamps
        start_timestamp = int(start_time.timestamp())
        end_timestamp = int(end_time.timestamp())
        
        collected_metrics = {}
        
        self.logger.info(f"Collecting metrics for the last {duration_minutes} minute(s)...")
        
        for metric in self.key_metrics:
            self.logger.info(f"Collecting range metric: {metric}")
            result = self.query_prometheus_range(
                metric, start_timestamp, end_timestamp, step
            )
            
            if result and result.get('status') == 'success':
                collected_metrics[metric] = result['data']
            else:
                self.logger.warning(f"Range metric {metric} failed or returned no data")
                collected_metrics[metric] = None
            
            # Avoid hammering the API
            time.sleep(0.2)
        
        # Persist results
        with open(metrics_file, 'w', encoding='utf-8') as f:
            json.dump({
                'timestamp': timestamp,
                'collection_time': datetime.datetime.now().isoformat(),
                'time_range': {
                    'start': start_time.isoformat(),
                    'end': end_time.isoformat(),
                    'duration_minutes': duration_minutes,
                    'step': step
                },
                'metrics': collected_metrics
            }, f, indent=2, ensure_ascii=False)
        
        self.logger.info(f"Range metrics written to: {metrics_file}")
        return str(metrics_file)
    
    def collect_train_ticket_specific_metrics(self):
        """Collect Train-Ticket specific signals."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        metrics_file = self.output_dir / f"trainticket_metrics_{timestamp}.json"
        
        # Train-Ticket specific queries
        train_ticket_queries = [
            # Pod phase
            'kube_pod_status_phase{namespace="default"}',
            
            # Container resource usage
            'rate(container_cpu_usage_seconds_total{namespace="default"}[5m])',
            'container_memory_usage_bytes{namespace="default"}',
            
            # Network throughput
            'rate(container_network_receive_bytes_total{namespace="default"}[5m])',
            'rate(container_network_transmit_bytes_total{namespace="default"}[5m])',
            
            # Pod restarts
            'kube_pod_container_status_restarts_total{namespace="default"}',
            
            # Volume usage
            'kubelet_volume_stats_used_bytes{namespace="default"}',
            
            # Add custom metrics here if needed
            'up{job="kubernetes-pods"}',
        ]
        
        collected_metrics = {}
        
        self.logger.info("Collecting Train-Ticket specific metrics...")
        
        for query in train_ticket_queries:
            self.logger.info(f"Executing query: {query}")
            result = self.query_prometheus(query)
            
            if result and result.get('status') == 'success':
                collected_metrics[query] = result['data']
            else:
                self.logger.warning(f"Query failed or returned no data: {query}")
                collected_metrics[query] = None
            
            time.sleep(0.1)
        
        # Persist results
        with open(metrics_file, 'w', encoding='utf-8') as f:
            json.dump({
                'timestamp': timestamp,
                'collection_time': datetime.datetime.now().isoformat(),
                'queries': collected_metrics
            }, f, indent=2, ensure_ascii=False)
        
        self.logger.info(f"Train-Ticket specific metrics written to: {metrics_file}")
        return str(metrics_file)
    
    def collect_all_metrics(self, range_duration=60):
        """Collect every supported metric type."""
        self.logger.info("Collecting all monitoring metrics...")
        
        collected_files = []
        
        # Instant metrics
        instant_file = self.collect_instant_metrics()
        collected_files.append(instant_file)
        
        # Range metrics
        range_file = self.collect_range_metrics(range_duration)
        collected_files.append(range_file)
        
        # Train-Ticket specific metrics
        specific_file = self.collect_train_ticket_specific_metrics()
        collected_files.append(specific_file)
        
        # Build summary report
        self.generate_collection_report(collected_files)
        
        self.logger.info(f"Monitoring metric collection produced {len(collected_files)} file(s)")
        return collected_files
    
    def generate_collection_report(self, collected_files):
        """Generate a summary report for collected metrics."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = self.output_dir / f"metric_collection_report_{timestamp}.json"
        
        report = {
            "timestamp": timestamp,
            "collection_time": datetime.datetime.now().isoformat(),
            "prometheus_url": self.prometheus_url,
            "total_files": len(collected_files),
            "collected_files": collected_files,
            "metrics_collected": len(self.key_metrics)
        }
        
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        self.logger.info(f"Wrote metric collection report: {report_file}")
    
    def get_system_startup_time(self):
        """Fetch system boot time from Prometheus."""
        try:
            response = requests.get(
                f"{self.prometheus_url}/api/v1/query",
                params={'query': 'node_boot_time_seconds'}
            )
            
            if response.status_code == 200:
                data = response.json()
                if data['status'] == 'success' and data['data']['result']:
                    boot_timestamp = float(data['data']['result'][0]['value'][1])
                    startup_time = datetime.datetime.fromtimestamp(boot_timestamp)
                    self.logger.info(f"System boot time: {startup_time}")
                return startup_time
        
            fallback_time = datetime.datetime.now() - datetime.timedelta(hours=1)
            self.logger.warning(f"Unable to obtain boot time, using fallback: {fallback_time}")
            return fallback_time
            
        except Exception as e:
            self.logger.error(f"Failed to determine system boot time: {e}")
            fallback_time = datetime.datetime.now() - datetime.timedelta(hours=1)
            return fallback_time
    
    def collect_experiment_metrics_csv(self, experiment_name="experiment", step='15s'):
        """Collect metrics from startup to now and emit a CSV."""
        import csv
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_file = self.output_dir / f"{experiment_name}_metrics_{timestamp}.csv"
        
        startup_time = self.get_system_startup_time()
        end_time = datetime.datetime.now()
        
        start_timestamp = int(startup_time.timestamp())
        end_timestamp = int(end_time.timestamp())
        
        duration_minutes = int((end_time - startup_time).total_seconds() / 60)
        
        self.logger.info(f"Collecting experiment metrics from {startup_time} to {end_time} ({duration_minutes} minutes)")
        
        csv_rows = []
        
        # Use all critical metrics (covering every anomaly type)
        core_metrics = self.key_metrics
        self.logger.info(f"Collecting {len(core_metrics)} metrics")
        
        for metric in core_metrics:
            self.logger.info(f"Collecting metric: {metric}")
            result = self.query_prometheus_range(metric, start_timestamp, end_timestamp, step)
            
            if result and result.get('status') == 'success' and result.get('data', {}).get('result'):
                for item in result['data']['result']:
                    if 'values' in item:
                        for ts, value in item['values']:
                            row = {
                                'metric_name': metric,
                                'timestamp': ts,
                                'datetime': datetime.datetime.fromtimestamp(ts).isoformat(),
                                'value': float(value) if value != 'NaN' else None
                            }
                            
                            if 'metric' in item:
                                for key, label_value in item['metric'].items():
                                    if key != '__name__':
                                        row[key] = label_value
                            
                            csv_rows.append(row)
            else:
                self.logger.warning(f"Metric {metric} failed or returned no data")
            
            time.sleep(0.2)
        
        if not csv_rows:
            self.logger.error("No valid data returned")
            return None
        
        all_columns = set()
        for row in csv_rows:
            all_columns.update(row.keys())
        
        fixed_columns = ['metric_name', 'timestamp', 'datetime', 'value']
        label_columns = sorted([col for col in all_columns if col not in fixed_columns])
        column_order = fixed_columns + label_columns
        
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=column_order)
            writer.writeheader()
            
            for row in csv_rows:
                complete_row = {col: row.get(col, '') for col in column_order}
                writer.writerow(complete_row)
        
        print(f"\n{'='*60}")
        print("Experiment metric collection finished")
        print(f"{'='*60}")
        print(f"Experiment name: {experiment_name}")
        print(f"Duration: {duration_minutes} minutes")
        print(f"Data points: {len(csv_rows):,}")
        print(f"CSV file: {csv_file}")
        print(f"{'='*60}")
        
        return str(csv_file)

    def get_system_startup_time(self):
        """Determine when the Train-Ticket application most recently started."""
        try:
            # Approach 1: inspect the earliest Train-Ticket pod start time
            self.logger.info("Attempting to determine Train-Ticket application start time...")
            
            # Query kube_pod_start_time for pods in the default namespace
            response = requests.get(
                f"{self.prometheus_url}/api/v1/query",
                params={'query': 'kube_pod_start_time{namespace="default"}'}
            )
            
            if response.status_code == 200:
                data = response.json()
                if data['status'] == 'success' and data['data']['result']:
                    # Track the earliest pod start timestamp
                    earliest_time = None
                    
                    for item in data['data']['result']:
                        pod_start_timestamp = float(item['value'][1])
                        pod_start_time = datetime.datetime.fromtimestamp(pod_start_timestamp)
                        
                        if earliest_time is None or pod_start_time < earliest_time:
                            earliest_time = pod_start_time
                    
                    if earliest_time:
                        # Limit the window to the last 24 hours
                        max_time = datetime.datetime.now() - datetime.timedelta(hours=24)
                        if earliest_time < max_time:
                            earliest_time = max_time
                            self.logger.warning(f"App start time exceeds 24h window; clamping to {earliest_time}")
                        
                        self.logger.info(f"Train-Ticket application start time: {earliest_time}")
                        return earliest_time
            
            # Approach 2: fall back to a 2-hour window when discovery fails
            self.logger.warning("Unable to retrieve pod start times, using safe window")
            safe_time = datetime.datetime.now() - datetime.timedelta(hours=2)
            self.logger.info(f"Safe time window: {safe_time} (last 2 hours)")
            return safe_time
            
        except Exception as e:
            self.logger.error(f"Failed to fetch application start time: {e}")
            fallback_time = datetime.datetime.now() - datetime.timedelta(hours=1)
            self.logger.warning(f"Using fallback time: {fallback_time}")
            return fallback_time

    def collect_experiment_metrics_by_metric(self, experiment_name="experiment", step='15s'):
        """Export each metric into its own CSV file."""
        import csv
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Create experiment-specific folder
        experiment_dir = self.output_dir / experiment_name
        experiment_dir.mkdir(exist_ok=True)
        
        startup_time = self.get_system_startup_time()
        end_time = datetime.datetime.now()
        
        start_timestamp = int(startup_time.timestamp())
        end_timestamp = int(end_time.timestamp())
        duration_minutes = int((end_time - startup_time).total_seconds() / 60)
        
        self.logger.info(f"Collecting per-metric data into: {experiment_dir}")
        self.logger.info(f"Time window: {startup_time} to {end_time} ({duration_minutes} minutes)")
        
        collected_files = []
        total_data_points = 0
        
        # Iterate through curated metrics
        for metric in self.key_metrics:
            self.logger.info(f"Collecting metric: {metric}")
            
            # Sanitize metric name for filenames
            safe_metric_name = metric.replace('(', '').replace(')', '').replace('[', '').replace(']', '').replace('/', '_')
            csv_file = experiment_dir / f"{safe_metric_name}_{timestamp}.csv"
            
            result = self.query_prometheus_range(metric, start_timestamp, end_timestamp, step)
            
            if result and result.get('status') == 'success' and result.get('data', {}).get('result'):
                csv_rows = []
                
                for item in result['data']['result']:
                    if 'values' in item:
                        for ts, value in item['values']:
                            row = {
                                'timestamp': ts,
                                'datetime': datetime.datetime.fromtimestamp(ts).isoformat(),
                                'value': float(value) if value != 'NaN' else None
                            }
                            
                            # Include labels as columns
                            if 'metric' in item:
                                for key, label_value in item['metric'].items():
                                    if key != '__name__':
                                        row[key] = label_value
                            
                            csv_rows.append(row)
                
                if csv_rows:
                    # Determine column order
                    all_columns = set()
                    for row in csv_rows:
                        all_columns.update(row.keys())
                    
                    fixed_columns = ['timestamp', 'datetime', 'value']
                    label_columns = sorted([col for col in all_columns if col not in fixed_columns])
                    column_order = fixed_columns + label_columns
                    
                    # Write CSV file
                    with open(csv_file, 'w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=column_order)
                        writer.writeheader()
                        
                        for row in csv_rows:
                            complete_row = {col: row.get(col, '') for col in column_order}
                            writer.writerow(complete_row)
                    
                    collected_files.append(str(csv_file))
                    total_data_points += len(csv_rows)
                    self.logger.info(f"[OK] {metric} collected: {len(csv_rows):,} data points")
                else:
                    self.logger.warning(f"[WARN] {metric} produced data but with unexpected format")
            else:
                self.logger.warning(f"[FAIL] {metric} failed or returned no data")
            
            time.sleep(0.2)
        
        # Emit summary
        print(f"\n{'='*80}")
        print("Per-metric CSV export finished")
        print(f"{'='*80}")
        print(f"Experiment name: {experiment_name}")
        print(f"Duration: {duration_minutes} minutes")
        print(f"Total data points: {total_data_points:,}")
        print(f"Metrics with data: {len(collected_files)}")
        print("File list:")
        for file_path in collected_files:
            file_size = os.path.getsize(file_path) / 1024 / 1024  # MB
            metric_name = os.path.basename(file_path).split('_')[0]
            print(f"  - {os.path.basename(file_path)} ({file_size:.1f} MB)")
        print(f"{'='*80}")
        
        return collected_files

    def show_metric_categories(self):
        """Display metric category information."""
        print(f"\n{'='*80}")
        print("Anomaly-detection metric categories")
        print(f"{'='*80}")
        
        for category_name, category_info in self.metric_categories.items():
            print(f"\nLevel {category_name.upper()}:")
            print(f"   {category_info['description']}")
            print(f"   Metrics: {len(category_info['metrics'])}")
            print("   Metric list:")
            for i, metric in enumerate(category_info['metrics'], 1):
                print(f"     {i:2d}. {metric}")
        
        total_metrics = sum(len(cat['metrics']) for cat in self.metric_categories.values())
        print(f"\nSummary: {len(self.metric_categories)} category(ies), {total_metrics} metric(s)")
        print(f"{'='*80}")

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Train-Ticket monitoring metric collector')
    parser.add_argument('--mode', choices=['normal', 'experiment', 'experiment-split', 'show-metrics'], default='normal',
                       help='Collection mode: normal, experiment (single CSV), experiment-split (per metric), show-metrics (list categories)')
    parser.add_argument('--experiment-name', type=str, default='experiment',
                       help='Experiment name used as folder prefix (experiment modes only)')
    parser.add_argument('--step', type=str, default='15s',
                       help='Data sampling interval (experiment modes only)')
    
    args = parser.parse_args()
    
    collector = MetricCollector()
    
    if args.mode == 'experiment':
        # Experiment mode: collect from startup and output one CSV
        csv_file = collector.collect_experiment_metrics_csv(
            experiment_name=args.experiment_name,
            step=args.step
        )
        
        if csv_file:
            print(f"[OK] Experiment metrics saved to: {csv_file}")
        else:
            print("[FAIL] Experiment metric collection failed")
    elif args.mode == 'experiment-split':
        # Experiment split mode: one CSV per metric
        csv_files = collector.collect_experiment_metrics_by_metric(
            experiment_name=args.experiment_name,
            step=args.step
        )
        
        if csv_files:
            print(f"[OK] Per-metric export produced {len(csv_files)} file(s)")
        else:
            print("[FAIL] Per-metric export failed")
    elif args.mode == 'show-metrics':
        # Show metric categories
        collector.show_metric_categories()
    else:
        # Normal mode: collect three categories and emit JSON
        metrics = collector.collect_all_metrics(range_duration=60)
        
        print("\nMonitoring metric collection completed!")
        print(f"Files generated: {len(metrics)}")
        print(f"Output directory: {collector.output_dir}")
        print(f"Prometheus URL: {collector.prometheus_url}")

if __name__ == "__main__":
    main()
