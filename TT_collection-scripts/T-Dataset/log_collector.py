#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Train-Ticket log collector.
Captures application, system, and container logs for each experiment.
"""

import os
import json
import datetime
import subprocess
import time
import logging
from pathlib import Path

class LogCollector:
    def __init__(self, output_dir="log_data"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        # Configure logger
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
    def get_train_ticket_pods(self):
        """Return pods that belong to the Train-Ticket stack (including infra)."""
        try:
            result = subprocess.run([
                'kubectl', 'get', 'pods', '-o', 'json'
            ], capture_output=True, text=True, check=True)
            
            pods_data = json.loads(result.stdout)
            train_ticket_pods = []
            
            for pod in pods_data['items']:
                pod_name = pod['metadata']['name']
                # Filter pods that belong to Train-Ticket or its dependencies
                if pod_name.startswith('ts-') or pod_name.startswith('nacos') or pod_name.startswith('rabbitmq'):
                    train_ticket_pods.append({
                        'name': pod_name,
                        'namespace': pod['metadata']['namespace'],
                        'labels': pod['metadata'].get('labels', {}),
                        'status': pod['status']['phase']
                    })
            
            return train_ticket_pods
        except Exception as e:
            self.logger.error(f"Failed to list pods: {e}")
            return []
    
    def collect_pod_logs(self, pod_name, namespace='default', lines=1000, run_dir: Path = None):
        """Collect logs for the current pod instance."""
        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            
            base_dir = run_dir if run_dir is not None else self.output_dir
            
            pod_dir = base_dir / pod_name
            pod_dir.mkdir(parents=True, exist_ok=True)
            
            log_file = pod_dir / f"{pod_name}_{timestamp}.log"
            
            result = subprocess.run([
                'kubectl', 'logs', pod_name, 
                '--namespace', namespace,
                '--tail', str(lines)
            ], capture_output=True, text=True)
            
            if result.returncode == 0:
                with open(log_file, 'w', encoding='utf-8') as f:
                    f.write(result.stdout)
                self.logger.info(f"Collected logs for {pod_name}: {log_file}")
                return str(log_file)
            else:
                self.logger.error(f"Failed to collect logs for {pod_name}: {result.stderr}")
                return None
                
        except Exception as e:
            self.logger.error(f"Unexpected error while collecting {pod_name} logs: {e}")
            return None
    
    def collect_pod_previous_logs(self, pod_name, namespace='default', run_dir: Path = None):
        """Collect logs from the previous pod run when available."""
        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base_dir = run_dir if run_dir is not None else self.output_dir
            pod_dir = base_dir / pod_name
            pod_dir.mkdir(parents=True, exist_ok=True)
            
            log_file = pod_dir / f"{pod_name}_previous_{timestamp}.log"
            
            result = subprocess.run([
                'kubectl', 'logs', pod_name,
                '--namespace', namespace,
                '--previous'
            ], capture_output=True, text=True)
            
            if result.returncode == 0 and result.stdout.strip():
                with open(log_file, 'w', encoding='utf-8') as f:
                    f.write(result.stdout)
                self.logger.info(f"Collected previous logs for {pod_name}: {log_file}")
                return str(log_file)
            else:
                self.logger.info(f"{pod_name} has no previous-run logs")
                return None
                
        except Exception as e:
            self.logger.error(f"Failed to collect previous logs for {pod_name}: {e}")
            return None
    
    def collect_events(self, run_dir: Path = None):
        """Collect Kubernetes events for the namespace."""
        try:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            base_dir = run_dir if run_dir is not None else self.output_dir
            events_file = base_dir / f"kubernetes_events_{timestamp}.json"
            
            result = subprocess.run([
                'kubectl', 'get', 'events', '-o', 'json'
            ], capture_output=True, text=True, check=True)
            
            with open(events_file, 'w', encoding='utf-8') as f:
                f.write(result.stdout)
            
            self.logger.info(f"Captured Kubernetes events: {events_file}")
            return str(events_file)
            
        except Exception as e:
            self.logger.error(f"Failed to capture Kubernetes events: {e}")
            return None
    
    def collect_all_logs(self, lines=1000, experiment_name: str = None):
        """Collect logs for every Train-Ticket pod into an optional experiment folder."""
        self.logger.info("Starting Train-Ticket log sweep...")
        
        # Root directory: log_data/<experiment>_<ts> or log_data/run_<ts>
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_folder_name = f"{experiment_name}_{ts}" if experiment_name else f"run_{ts}"
        run_dir = (self.output_dir / run_folder_name)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Writing logs to: {run_dir}")
        
        # Discover pods
        pods = self.get_train_ticket_pods()
        self.logger.info(f"Found {len(pods)} candidate pods")
        
        collected_logs = []
        
        # Collect logs for each pod
        for pod in pods:
            pod_name = pod['name']
            namespace = pod['namespace']
            
            current_log = self.collect_pod_logs(pod_name, namespace, lines, run_dir)
            if current_log:
                collected_logs.append(current_log)
            
            previous_log = self.collect_pod_previous_logs(pod_name, namespace, run_dir)
            if previous_log:
                collected_logs.append(previous_log)
            
            # Avoid sending excessive requests
            time.sleep(0.1)
        
        # Capture Kubernetes events
        events_log = self.collect_events(run_dir)
        if events_log:
            collected_logs.append(events_log)
        
        # Generate collection report
        self.generate_collection_report(collected_logs, run_dir)
        
        self.logger.info(f"Log collection finished with {len(collected_logs)} files")
        return collected_logs
    
    def generate_collection_report(self, collected_logs, run_dir: Path = None):
        """Create a JSON report summarizing collected logs."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base_dir = run_dir if run_dir is not None else self.output_dir
        report_file = base_dir / f"log_collection_report_{timestamp}.json"
        
        report = {
            "timestamp": timestamp,
            "collection_time": datetime.datetime.now().isoformat(),
            "output_dir": str(base_dir),
            "total_files": len(collected_logs),
            "collected_files": collected_logs,
            "summary": {
                "pod_logs": len([f for f in collected_logs if not f.endswith('events.json')]),
                "events": len([f for f in collected_logs if f.endswith('events.json')])
            }
        }
        
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        self.logger.info(f"Wrote log collection report: {report_file}")

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Train-Ticket log collector')
    parser.add_argument('--experiment-name', type=str, default=None, help='Experiment name used for log_data/<name>_<timestamp>/')
    parser.add_argument('--lines', type=int, default=1000, help='Log lines to capture per pod (default: 1000)')
    args = parser.parse_args()

    collector = LogCollector()
    logs = collector.collect_all_logs(lines=args.lines, experiment_name=args.experiment_name)
    
    print("\nLog collection finished!")
    print(f"Files collected: {len(logs)}")
    print(f"Artifacts stored in: {collector.output_dir}")

if __name__ == "__main__":
    main()
