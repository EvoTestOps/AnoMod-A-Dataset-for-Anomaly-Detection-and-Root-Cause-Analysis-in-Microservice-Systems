#!/bin/bash

# Script to stop chaos experiments
# Usage: ./stop_chaos.sh <experiment-file>

set -e

EXPERIMENT_FILE="$1"

if [ -z "$EXPERIMENT_FILE" ]; then
    echo "Usage: $0 <experiment-file>"
    echo "Example: $0 01-cpu-stress.yaml"
    exit 1
fi

if [ ! -f "$EXPERIMENT_FILE" ]; then
    echo "Error: Experiment file '$EXPERIMENT_FILE' not found"
    exit 1
fi

echo "===========================================" 
echo "Stopping Chaos Experiment"
echo "File: $EXPERIMENT_FILE"
echo "Timestamp: $(date)"
echo "==========================================="

# Show current status before cleanup
echo "Current status:"
kubectl get -f "$EXPERIMENT_FILE" 2>/dev/null || echo "No experiments found (already stopped?)"

# Delete the chaos experiments
echo "Stopping chaos experiments..."
kubectl delete -f "$EXPERIMENT_FILE" --ignore-not-found=true

echo "✅ Chaos experiment stopped successfully"
echo "Your collected data is in T-Dataset/ organized by your experiment name"
echo "==========================================="
