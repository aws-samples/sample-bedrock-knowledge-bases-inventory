#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Forward localhost:<port> to the internal UI ALB via SSM, using the running Fargate task as the hop.
# Usage: ./scripts/tunnel.sh [stack-name] [local-port]   (honours AWS_PROFILE / AWS_REGION)
set -euo pipefail
STACK="${1:-BkbInventoryStack}"
LOCAL_PORT="${2:-8501}"
# The AWS CLI needs session-manager-plugin on PATH; it may be installed user-local in ~/bin.
command -v session-manager-plugin >/dev/null || export PATH="$HOME/bin:$PATH"

out() { aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
CLUSTER=$(out EcsClusterName)
SERVICE=$(out EcsServiceName)
ALB=$(out UiInternalAlbDns)

TASK_ARN=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" --desired-status RUNNING \
  --query 'taskArns[0]' --output text)
[[ "$TASK_ARN" == "None" || -z "$TASK_ARN" ]] && { echo "No running task in $SERVICE" >&2; exit 1; }
TASK_ID="${TASK_ARN##*/}"
# Pick the container running the ECS Exec agent; containers[0] can be an injected
# sidecar (e.g. the GuardDuty runtime-monitoring agent).
RUNTIME_ID=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query "tasks[0].containers[?managedAgents[?name=='ExecuteCommandAgent' && lastStatus=='RUNNING']].runtimeId | [0]" \
  --output text)
[[ "$RUNTIME_ID" == "None" || -z "$RUNTIME_ID" ]] && { echo "ECS Exec agent not running yet on $TASK_ID; retry shortly" >&2; exit 1; }

echo "Tunnel: http://localhost:${LOCAL_PORT}  ->  ${ALB}:80  (via task ${TASK_ID})"
aws ssm start-session \
  --target "ecs:${CLUSTER}_${TASK_ID}_${RUNTIME_ID}" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"${ALB}\"],\"portNumber\":[\"80\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}"
