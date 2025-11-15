#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate a concise coverage summary from collected jacoco.exec files plus the
service-specific classes directory. The output mirrors the socialnetwork
example (overall line coverage plus totals per file if you extend it later).

Rationale: the multi-modal dataset needs an easy-to-read coverage snapshot for
side-by-side comparison across services.

Notes:
- Install jacococli once:
  curl -sSL -o jacococli.jar \
    https://repo1.maven.org/maven2/org/jacoco/org.jacoco.cli/0.8.10/org.jacoco.cli-0.8.10-nodeps.jar
- Provide both compiled classes and source directories for the target service.

Example:
  python3 coverage_summary.py \
    --exec-dir /home/ubuntu/Dataset/coverage_data/2025xxxx_xxxxxx \
    --service ts-user-service \
    --classes /home/ubuntu/train-ticket/ts-user-service/target/classes \
    --sources /home/ubuntu/train-ticket/ts-user-service/src/main/java \
    --out /home/ubuntu/Dataset/coverage_report/2025xxxx_xxxxxx/ts-user-service
"""

import argparse
import json
import subprocess
from pathlib import Path


def run(cmd):
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def merge_execs(exec_dir: Path, service_name: str, merged_exec: Path):
    # Merge every exec file whose name contains the service identifier
    exec_files = sorted([p for p in exec_dir.glob('*.exec') if service_name in p.name])
    if not exec_files:
        raise FileNotFoundError(f'No jacoco.exec file found for {service_name}')

    # Locate jacococli.jar
    jacoco_cli = None
    possible_paths = [
        '/home/ubuntu/jacococli.jar',
        '/home/ubuntu/T-Dataset/coverage_tools/jacococli.jar',
        './jacococli.jar',
        'jacococli.jar'
    ]
    for path in possible_paths:
        if Path(path).exists():
            jacoco_cli = path
            break
    
    if not jacoco_cli:
        raise FileNotFoundError('jacococli.jar not found')
    
    cmd = [
        'java', '-jar', jacoco_cli, 'merge',
    ] + [str(p) for p in exec_files] + ['--destfile', str(merged_exec)]
    run(cmd)


def generate_xml_html(merged_exec: Path, classes: Path, sources: Path, out_dir: Path):
    ensure_dir(out_dir)
    
    # Locate jacococli.jar
    jacoco_cli = None
    possible_paths = [
        '/home/ubuntu/jacococli.jar',
        '/home/ubuntu/T-Dataset/coverage_tools/jacococli.jar',
        './jacococli.jar',
        'jacococli.jar'
    ]
    for path in possible_paths:
        if Path(path).exists():
            jacoco_cli = path
            break
    
    if not jacoco_cli:
        raise FileNotFoundError('jacococli.jar not found')
    
    cmd = [
        'java', '-jar', jacoco_cli, 'report', str(merged_exec),
        '--classfiles', str(classes),
        '--sourcefiles', str(sources),
        '--xml', str(out_dir / 'coverage.xml'),
        '--html', str(out_dir / 'html')
    ]
    run(cmd)


def parse_total_from_xml(xml_path: Path):
    # Parse the top-level LINE counter from the JaCoCo XML
    import xml.etree.ElementTree as ET
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    total = {'covered': 0, 'missed': 0}
    for c in root.findall('counter'):
        if c.get('type') == 'LINE':
            total['covered'] = int(c.get('covered'))
            total['missed'] = int(c.get('missed'))
            break
    return total


def build_summary_text(service: str, xml_path: Path) -> str:
    total = parse_total_from_xml(xml_path)
    lines_total = total['covered'] + total['missed']
    cover_pct = 0 if lines_total == 0 else int(round(100 * total['covered'] / lines_total))

    # Only emit an overview for now; extend this section for per-file stats
    text = []
    text.append('=' * 66)
    text.append('  Simple Code Coverage Report')
    text.append('-' * 66)
    text.append(f'Service: {service}')
    text.append('-' * 66)
    text.append('TOTAL'.ljust(20) + f"Lines {lines_total:6d}  Cover {cover_pct:3d}%")
    text.append('-' * 66)
    return '\n'.join(text) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exec-dir', required=True, help='Directory produced by collect_coverage.sh')
    ap.add_argument('--service', required=True, help='Service name, e.g., ts-user-service')
    ap.add_argument('--classes', required=True, help='Path to target/classes for that service')
    ap.add_argument('--sources', required=True, help='Path to service sources, usually src/main/java')
    ap.add_argument('--out', required=True, help='Output directory')
    args = ap.parse_args()

    exec_dir = Path(args.exec_dir)
    out_dir = Path(args.out)
    ensure_dir(out_dir)

    merged_exec = out_dir / 'merged.exec'
    merge_execs(exec_dir, args.service, merged_exec)
    generate_xml_html(merged_exec, Path(args.classes), Path(args.sources), out_dir)

    summary_text = build_summary_text(args.service, out_dir / 'coverage.xml')
    (out_dir / 'coverage-summary.txt').write_text(summary_text, encoding='utf-8')
    print(out_dir / 'coverage-summary.txt')


if __name__ == '__main__':
    main()


