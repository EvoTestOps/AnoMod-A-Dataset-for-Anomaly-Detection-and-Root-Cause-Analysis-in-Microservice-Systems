#!/bin/bash

# Script to start chaos experiments for Train-ticket system
# Usage: ./start_chaos.sh <experiment-file>

set -e

EXPERIMENT_FILE="$1"

if [ -z "$EXPERIMENT_FILE" ]; then
    echo "Usage: $0 <experiment-file>"
    echo "Example: $0 01-cpu-stress.yaml"
    echo "Available experiments:"
    ls -1 *.yaml 2>/dev/null || echo "  No experiment files found"
    exit 1
fi

if [ ! -f "$EXPERIMENT_FILE" ]; then
    echo "Error: Experiment file '$EXPERIMENT_FILE' not found"
    exit 1
fi

# Extract experiment info from YAML for display
EXPERIMENT_TYPE=$(grep "kind:" "$EXPERIMENT_FILE" | head -1 | awk '{print $2}')
ANOMALY_LEVEL=$(grep "anomaly_level:" "$EXPERIMENT_FILE" | head -1 | sed 's/.*anomaly_level[=:] *"\?//' | sed 's/"\?.*//')
DURATION=$(grep "duration:" "$EXPERIMENT_FILE" | head -1 | sed 's/.*duration: *"\?//' | sed 's/"\?.*//')

echo "===========================================" 
echo "Starting Chaos Experiment"
echo "File: $EXPERIMENT_FILE"
echo "Type: $EXPERIMENT_TYPE"
echo "Level: $ANOMALY_LEVEL"
echo "Duration: $DURATION"
echo "Timestamp: $(date)"
echo "==========================================="

# Apply the chaos experiment
echo "Starting chaos experiment..."
kubectl apply -f "$EXPERIMENT_FILE"

# Get the experiment resource name(s)
EXPERIMENT_NAMES=$(kubectl get -f "$EXPERIMENT_FILE" -o name | tr '\n' ' ')
echo "Started: $EXPERIMENT_NAMES"

# Wait for experiments to be ready
echo "Waiting for chaos to become active..."
sleep 5

# Show experiment status
echo "Current status:"
kubectl get -f "$EXPERIMENT_FILE"

# Simple instructions
echo ""
echo "==========================================="
echo "🔥 CHAOS EXPERIMENT IS NOW RUNNING 🔥"
echo "==========================================="
echo "Duration: $DURATION"
echo ""
echo "Now run your data collection script:"
echo "  cd ../T-Dataset"
echo "  ./collect_all_modalities.sh --name chaos-$(basename $EXPERIMENT_FILE .yaml)-$(date +%Y%m%d-%H%M)"
echo ""
echo "Monitor experiment:"
echo "  kubectl get -f $EXPERIMENT_FILE"
echo ""
echo "Stop experiment:"
echo "  ./stop_chaos.sh $EXPERIMENT_FILE"
echo "==========================================="
