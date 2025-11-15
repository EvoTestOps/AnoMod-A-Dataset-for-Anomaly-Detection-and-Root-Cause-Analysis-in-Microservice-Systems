#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Enhanced trace collector for the Train-Ticket system.
Designed to pull and organize tracing data directly from Elasticsearch.
"""

import os
import json
import datetime
import requests
import time
import logging
import subprocess
import csv
from pathlib import Path
from urllib.parse import urljoin

class EnhancedTraceCollector:
    def __init__(self, elasticsearch_url=None, output_dir="trace_data"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        # Configure logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        
        # Discover the Elasticsearch URL if no override is provided
        if elasticsearch_url is None:
            self.elasticsearch_url = self.get_elasticsearch_url()
        else:
            self.elasticsearch_url = elasticsearch_url
    
    def get_elasticsearch_url(self):
        """Discover the Elasticsearch service URL"""
        try:
            result = subprocess.run([
                'kubectl', 'get', 'svc', 'elasticsearch', '-o', 'json'
            ], capture_output=True, text=True, check=True)
            
            svc_data = json.loads(result.stdout)
            cluster_ip = svc_data['spec']['clusterIP']
            port = 9200
            
            elasticsearch_url = f"http://{cluster_ip}:{port}"
            self.logger.info(f"Discovered Elasticsearch URL: {elasticsearch_url}")
            return elasticsearch_url
            
        except Exception as e:
            self.logger.error(f"Failed to obtain Elasticsearch URL: {e}")
            return "http://10.108.217.220:9200"
    
    def query_elasticsearch_segments(self, size=1000, hours_back=24):
        """Query sw_segment indices for detailed trace segments"""
        try:
            # Determine time window
            end_time = datetime.datetime.now()
            start_time = end_time - datetime.timedelta(hours=hours_back)
            
            # Build the Elasticsearch query
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "range": {
                                    "start_time": {
                                        "gte": int(start_time.timestamp() * 1000),
                                        "lte": int(end_time.timestamp() * 1000)
                                    }
                                }
                            }
                        ]
                    }
                },
                "size": size,
                "sort": [
                    {
                        "start_time": {
                            "order": "desc"
                        }
                    }
                ]
            }
            
            url = f"{self.elasticsearch_url}/sw_segment-*/_search"
            response = requests.post(url, json=query, timeout=60)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"Elasticsearch query failed: {response.status_code}")
                return None
                
        except Exception as e:
            self.logger.error(f"Elasticsearch query error: {e}")
            return None
    
    def extract_trace_info(self, segment_data):
        """Extract trace metadata from segment documents"""
        traces = []
        
        if not segment_data or 'hits' not in segment_data:
            return traces
        
        for hit in segment_data['hits']['hits']:
            source = hit['_source']
            
            # Basic attributes
            trace_info = {
                'trace_id': source.get('trace_id', ''),
                'segment_id': source.get('segment_id', ''),
                'service_id': source.get('service_id', ''),
                'service_instance_id': source.get('service_instance_id', ''),
                'endpoint_name': source.get('endpoint_name', ''),
                'endpoint_id': source.get('endpoint_id', ''),
                'start_time': source.get('start_time', 0),
                'end_time': source.get('end_time', 0),
                'latency': source.get('latency', 0),
                'is_error': source.get('is_error', 0),
                'statement': source.get('statement', ''),
                'tags': source.get('tags', []),
                'time_bucket': source.get('time_bucket', 0),
                'version': source.get('version', 0)
            }
            
            # Decode service name
            if trace_info['service_id']:
                try:
                    # SkyWalking format: <base64 service name>.<version>, e.g., dHMtdHJhdmVsLXNlcnZpY2U=.1
                    import base64
                    service_id = trace_info['service_id']
                    
                    parts = service_id.split('.')
                    base64_part = parts[0]  # first part stores the base64 service name
                    
                    try:
                        decoded = base64.b64decode(base64_part).decode('utf-8')
                        trace_info['service_name'] = decoded
                    except:
                        # Already plain text
                        trace_info['service_name'] = base64_part
                except Exception as e:
                    # Fall back to the original identifier
                    trace_info['service_name'] = trace_info['service_id']
            else:
                trace_info['service_name'] = 'unknown'
            
            # Convert timestamps into ISO strings
            if trace_info['start_time']:
                trace_info['start_datetime'] = datetime.datetime.fromtimestamp(
                    trace_info['start_time'] / 1000
                ).isoformat()
            
            if trace_info['end_time']:
                trace_info['end_datetime'] = datetime.datetime.fromtimestamp(
                    trace_info['end_time'] / 1000
                ).isoformat()
            
            traces.append(trace_info)
        
        return traces
    
    def collect_detailed_traces(self, hours_back=24, size=10000):
        """Collect detailed trace data"""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        self.logger.info(f"Collecting detailed traces from the last {hours_back} hours...")
        
        # Query Elasticsearch
        segment_data = self.query_elasticsearch_segments(size, hours_back)
        
        if not segment_data:
            self.logger.error("Failed to retrieve data from Elasticsearch")
            return None
        
        # Extract traces
        traces = self.extract_trace_info(segment_data)
        
        self.logger.info(f"Extracted {len(traces)} trace records")
        
        # Write JSON
        json_file = self.output_dir / f"detailed_traces_{timestamp}.json"
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump({
                'timestamp': timestamp,
                'collection_time': datetime.datetime.now().isoformat(),
                'hours_back': hours_back,
                'total_traces': len(traces),
                'elasticsearch_url': self.elasticsearch_url,
                'traces': traces
            }, f, indent=2, ensure_ascii=False)
        
        # Write CSV
        csv_file = self.output_dir / f"detailed_traces_{timestamp}.csv"
        if traces:
            with open(csv_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=traces[0].keys())
                writer.writeheader()
                writer.writerows(traces)
        
        self.logger.info("Detailed trace collection finished:")
        self.logger.info(f"  JSON file: {json_file}")
        self.logger.info(f"  CSV file: {csv_file}")
        self.logger.info(f"  Total records: {len(traces)}")
        
        return {
            'json_file': str(json_file),
            'csv_file': str(csv_file),
            'total_traces': len(traces)
        }
    
    def analyze_trace_patterns(self, traces):
        """Analyze aggregated trace patterns"""
        if not traces:
            return {
                'total_traces': 0,
                'unique_services': [],
                'unique_endpoints': [],
                'error_traces': 0,
                'service_call_counts': {},
                'endpoint_call_counts': {},
                'latency_stats': None,
                'time_range': {'earliest': None, 'latest': None}
            }
        
        analysis = {
            'total_traces': len(traces),
            'unique_services': set(),
            'unique_endpoints': set(),
            'error_traces': 0,
            'service_call_counts': {},
            'endpoint_call_counts': {},
            'latency_stats': [],
            'time_range': {
                'earliest': None,
                'latest': None
            }
        }
        
        for trace in traces:
            # Service stats
            service_name = trace.get('service_name', 'unknown')
            analysis['unique_services'].add(service_name)
            analysis['service_call_counts'][service_name] = analysis['service_call_counts'].get(service_name, 0) + 1
            
            # Endpoint stats
            endpoint = trace.get('endpoint_name', 'unknown')
            analysis['unique_endpoints'].add(endpoint)
            analysis['endpoint_call_counts'][endpoint] = analysis['endpoint_call_counts'].get(endpoint, 0) + 1
            
            # Error stats
            if trace.get('is_error', 0) == 1:
                analysis['error_traces'] += 1
            
            # Latency stats
            latency = trace.get('latency', 0)
            if latency > 0:
                analysis['latency_stats'].append(latency)
            
            # Time window
            start_time = trace.get('start_time', 0)
            if start_time:
                if analysis['time_range']['earliest'] is None or start_time < analysis['time_range']['earliest']:
                    analysis['time_range']['earliest'] = start_time
                if analysis['time_range']['latest'] is None or start_time > analysis['time_range']['latest']:
                    analysis['time_range']['latest'] = start_time
        
        # Convert sets to lists for JSON serialization
        analysis['unique_services'] = list(analysis['unique_services'])
        analysis['unique_endpoints'] = list(analysis['unique_endpoints'])
        
        # Compute latency summary
        if analysis['latency_stats']:
            analysis['latency_stats'] = {
                'min': min(analysis['latency_stats']),
                'max': max(analysis['latency_stats']),
                'avg': sum(analysis['latency_stats']) / len(analysis['latency_stats']),
                'count': len(analysis['latency_stats'])
            }
        
        # Convert timestamps to readable strings
        if analysis['time_range']['earliest']:
            analysis['time_range']['earliest_datetime'] = datetime.datetime.fromtimestamp(
                analysis['time_range']['earliest'] / 1000
            ).isoformat()
        
        if analysis['time_range']['latest']:
            analysis['time_range']['latest_datetime'] = datetime.datetime.fromtimestamp(
                analysis['time_range']['latest'] / 1000
            ).isoformat()
        
        return analysis
    
    def collect_and_analyze_traces(self, hours_back=24, size=10000):
        """Collect traces and run the analysis pipeline"""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Collect traces
        result = self.collect_detailed_traces(hours_back, size)
        
        if not result:
            return None
        
        # Load collected traces
        with open(result['json_file'], 'r', encoding='utf-8') as f:
            data = json.load(f)
            traces = data['traces']
        
        # Analyze
        analysis = self.analyze_trace_patterns(traces)
        
        # Persist analysis results
        analysis_file = self.output_dir / f"trace_analysis_{timestamp}.json"
        with open(analysis_file, 'w', encoding='utf-8') as f:
            json.dump({
                'timestamp': timestamp,
                'collection_time': datetime.datetime.now().isoformat(),
                'analysis': analysis
            }, f, indent=2, ensure_ascii=False)
        
        # Print a summary
        print(f"\n{'='*80}")
        print("Train-Ticket Trace Analysis Report")
        print(f"{'='*80}")
        print(f"Collection time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Time window: last {hours_back} hours")
        print(f"Total traces: {analysis['total_traces']:,}")
        print(f"Distinct services: {len(analysis['unique_services'])}")
        print(f"Distinct endpoints: {len(analysis['unique_endpoints'])}")
        print(f"Error traces: {analysis['error_traces']}")
        if analysis['total_traces'] > 0:
            print(f"Error rate: {analysis['error_traces']/analysis['total_traces']*100:.2f}%")
        else:
            print("Error rate: N/A (no traces collected)")
        
        if analysis['latency_stats']:
            print("\nLatency statistics:")
            print(f"  Min latency: {analysis['latency_stats']['min']} ms")
            print(f"  Max latency: {analysis['latency_stats']['max']} ms")
            print(f"  Avg latency: {analysis['latency_stats']['avg']:.2f} ms")
        
        print("\nTop services (10):")
        sorted_services = sorted(analysis['service_call_counts'].items(), key=lambda x: x[1], reverse=True)
        for i, (service, count) in enumerate(sorted_services[:10], 1):
            print(f"  {i:2d}. {service}: {count:,} calls")
        
        print("\nTop endpoints (10):")
        sorted_endpoints = sorted(analysis['endpoint_call_counts'].items(), key=lambda x: x[1], reverse=True)
        for i, (endpoint, count) in enumerate(sorted_endpoints[:10], 1):
            print(f"  {i:2d}. {endpoint}: {count:,} calls")
        
        print("\nOutput files:")
        print(f"  Detailed JSON: {result['json_file']}")
        print(f"  CSV export: {result['csv_file']}")
        print(f"  Analysis report: {analysis_file}")
        print(f"{'='*80}")
        
        return {
            'json_file': result['json_file'],
            'csv_file': result['csv_file'],
            'analysis_file': str(analysis_file),
            'analysis': analysis
        }

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Train-Ticket enhanced trace collector')
    parser.add_argument('--hours', type=int, default=24,
                       help='How many hours of traces to collect (default: 24)')
    parser.add_argument('--size', type=int, default=10000,
                       help='Maximum number of traces to fetch (default: 10000)')
    
    args = parser.parse_args()
    
    collector = EnhancedTraceCollector()
    
    # Run collection + analysis
    result = collector.collect_and_analyze_traces(
        hours_back=args.hours,
        size=args.size
    )
    
    if result:
        print("\n✅ Trace collection and analysis completed!")
        print(f"📁 Data directory: {collector.output_dir}")
    else:
        print("\n❌ Trace collection failed!")

if __name__ == "__main__":
    main()
