#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Inject JaCoCo runtime instrumentation into the Train-Ticket Kubernetes manifests.

Usage:
  python3 inject_jacoco_k8s.py -i /path/to/sw_deploy.yaml -o /path/to/sw_deploy.with-coverage.yaml \
    [--mode file|tcpserver] [--tcp-port 6300]

Design notes:
- Avoid rebuilding images or changing app code by adding an initContainer that downloads
  jacocoagent.jar (and jacococli.jar) plus JAVA_TOOL_OPTIONS injection inside the main container.
- mode=file creates /coverage/jacoco-<pod>.exec; mode=tcpserver enables remote dumps without restart.
- Always emit a new YAML file instead of editing the original one in place for easier review/rollback.
"""

import argparse
import copy
import sys
from pathlib import Path
from typing import Dict, Optional

import yaml


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _has_volume(volumes, name):
    return any(v.get('name') == name for v in volumes or [])


def _has_init_container(init_containers, name):
    return any(c.get('name') == name for c in init_containers or [])


def _get_or_create_env(container):
    env = container.get('env')
    if env is None:
        env = []
        container['env'] = env
    return env


def _has_env(env_list, name):
    return any(e.get('name') == name for e in env_list)


def _get_or_create_volume_mounts(container):
    vms = container.get('volumeMounts')
    if vms is None:
        vms = []
        container['volumeMounts'] = vms
    return vms


def _add_unique_volume_mount(volume_mounts, name, mount_path):
    if any(vm.get('name') == name for vm in volume_mounts):
        return
    volume_mounts.append({'name': name, 'mountPath': mount_path})


def _inject_into_pod_spec(
    pod_spec: dict,
    *,
    mode: str,
    tcp_port: int,
    includes: Optional[str],
    excludes: Optional[str],
) -> bool:
    """Inject volumes/initContainers/env blocks into the pod spec; return whether anything changed."""
    changed = False

    # volumes: jacoco-vol, coverage-vol
    volumes = pod_spec.get('volumes') or []
    if not _has_volume(volumes, 'jacoco-vol'):
        volumes.append({'name': 'jacoco-vol', 'emptyDir': {}})
        changed = True
    if not _has_volume(volumes, 'coverage-vol'):
        volumes.append({'name': 'coverage-vol', 'emptyDir': {}})
        changed = True
    pod_spec['volumes'] = volumes

    # initContainers: init-jacoco downloads the agent bits
    init_containers = pod_spec.get('initContainers') or []
    if not _has_init_container(init_containers, 'init-jacoco'):
        init_containers.append({
            'name': 'init-jacoco',
            'image': 'curlimages/curl:7.88.1',
            'command': ['sh', '-c'],
            'args': [
                'set -e; mkdir -p /jacoco && '
                'curl -sSL -o /jacoco/jacocoagent.jar '
                'https://repo1.maven.org/maven2/org/jacoco/org.jacoco.agent/0.8.10/org.jacoco.agent-0.8.10-runtime.jar && '
                'curl -sSL -o /jacoco/jacococli.jar '
                'https://repo1.maven.org/maven2/org/jacoco/org.jacoco.cli/0.8.10/org.jacoco.cli-0.8.10-nodeps.jar'
            ],
            'volumeMounts': [
                {'name': 'jacoco-vol', 'mountPath': '/jacoco'}
            ],
            'imagePullPolicy': 'IfNotPresent'
        })
        changed = True
    pod_spec['initContainers'] = init_containers

    # containers: inject/append JAVA_TOOL_OPTIONS and required mounts for each container
    for container in _ensure_list(pod_spec.get('containers')):
        env = _get_or_create_env(container)
        if mode == 'file':
            jacoco_agent = (
                '-javaagent:/jacoco/jacocoagent.jar='
                'output=file,destfile=/coverage/jacoco-$(HOSTNAME).exec,'
                'append=true'
            )
        else:
            jacoco_agent = (
                '-javaagent:/jacoco/jacocoagent.jar='
                f'output=tcpserver,address=*,port={tcp_port},sessionid=$(HOSTNAME),'
                'append=true'
            )
        if includes:
            jacoco_agent = jacoco_agent + f',includes={includes}'
        if excludes:
            jacoco_agent = jacoco_agent + f',excludes={excludes}'
        # Keep existing JAVA_TOOL_OPTIONS (e.g., SkyWalking) and append JaCoCo after it
        existing = None
        for e in env:
            if e.get('name') == 'JAVA_TOOL_OPTIONS':
                existing = e
                break
        if existing is None:
            env.append({'name': 'JAVA_TOOL_OPTIONS', 'value': jacoco_agent})
            changed = True
        else:
            val = existing.get('value', '') or ''
            if jacoco_agent not in val:
                existing['value'] = (val + ' ' + jacoco_agent).strip()
                changed = True

        volume_mounts = _get_or_create_volume_mounts(container)
        before_len = len(volume_mounts)
        _add_unique_volume_mount(volume_mounts, 'jacoco-vol', '/jacoco')
        _add_unique_volume_mount(volume_mounts, 'coverage-vol', '/coverage')
        if len(volume_mounts) != before_len:
            changed = True

    return changed


def inject_for_documents(docs, *, mode: str, tcp_port: int, svc_to_includes: Dict[str, str], excludes: Optional[str]):
    changed_any = False
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get('kind')
        if kind not in {'Deployment', 'StatefulSet', 'DaemonSet'}:
            continue
        template = (
            doc.get('spec', {})
               .get('template', {})
        )
        pod_spec = template.get('spec')
        if not isinstance(pod_spec, dict):
            continue
        # Use metadata.name as the service key
        svc_name = doc.get('metadata', {}).get('name') or ''
        includes = svc_to_includes.get(svc_name)
        if _inject_into_pod_spec(
            pod_spec,
            mode=mode,
            tcp_port=tcp_port,
            includes=includes,
            excludes=excludes,
        ):
            changed_any = True
    return changed_any


def infer_service_includes(src_root: Path, service_name: str) -> Optional[str]:
    """Guess the dominant package prefix (e.g., user.*) for a service by scanning its sources."""
    module_dir = src_root / service_name / 'src' / 'main' / 'java'
    if not module_dir.is_dir():
        return None
    packages: Dict[str, int] = {}
    # Scan at most 500 Java files and extract the package line
    count = 0
    for path in module_dir.rglob('*.java'):
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        for line in text.splitlines()[:5]:
            line_strip = line.strip()
            if line_strip.startswith('package '):
                # e.g., package user.controller;
                pkg = line_strip[len('package '):].rstrip(' ;')
                top = pkg.split('.')[0]
                if top:
                    packages[top] = packages.get(top, 0) + 1
                break
        count += 1
        if count >= 500:
            break
    if not packages:
        return None
    # Pick the most frequent top-level package
    top_pkg = max(packages.items(), key=lambda kv: kv[1])[0]
    return f'{top_pkg}.*'


def main():
    parser = argparse.ArgumentParser(description='Inject JaCoCo runtime into Kubernetes manifests')
    parser.add_argument('-i', '--input', required=True, help='Input YAML (multi-document). Use sw_deploy.yaml for with-tracing setup')
    parser.add_argument('-o', '--output', required=True, help='Output YAML (the original file is left untouched)')
    parser.add_argument('--mode', choices=['file', 'tcpserver'], default='file', help='JaCoCo output mode')
    parser.add_argument('--tcp-port', type=int, default=6300, help='tcpserver listening port')
    parser.add_argument('--tt-root', type=str, default='/home/ubuntu/train-ticket', help='Train-Ticket source root used to infer package prefixes')
    parser.add_argument('--excludes', type=str, default='org.springframework.*;ch.qos.logback.*;org.apache.*;com.alibaba.*;javax.*;lombok.*;sun.*', help='Semicolon separated list of excluded package prefixes')
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    with input_path.open('r', encoding='utf-8') as f:
        docs = list(yaml.safe_load_all(f))

    docs_copy = copy.deepcopy(docs)

    # Derive includes from metadata.name (ts-xxx-service aligns with the module directory)
    svc_to_includes: Dict[str, str] = {}
    src_root = Path(args.tt_root)
    for doc in docs_copy:
        if not isinstance(doc, dict):
            continue
        if doc.get('kind') != 'Deployment':
            continue
        svc = doc.get('metadata', {}).get('name')
        if not svc:
            continue
        includes = infer_service_includes(src_root, svc)
        if includes:
            svc_to_includes[svc] = includes

    changed = inject_for_documents(
        docs_copy,
        mode=args.mode,
        tcp_port=args.tcp_port,
        svc_to_includes=svc_to_includes,
        excludes=args.excludes,
    )

    if not changed:
        print('No injectable workloads were found (or they already include JaCoCo). YAML will still be written.', file=sys.stderr)

    with output_path.open('w', encoding='utf-8') as f:
        yaml.safe_dump_all(docs_copy, f, sort_keys=False, allow_unicode=True)

    print(f'Generated YAML with JaCoCo instrumentation: {output_path}')


if __name__ == '__main__':
    main()


