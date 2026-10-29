#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
set -e

# Deploy BkbInventoryStack. The Streamlit image is built in CodeBuild (native ARM)
# during the deploy, so no local Docker or container runtime is required.

# Explicit on purpose: an AWS_REGION inherited from the shell must not silently redirect the deploy.
export AWS_REGION="${DEPLOY_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export CDK_DEFAULT_REGION="$AWS_REGION"
CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT
echo "Deploying to account $CDK_DEFAULT_ACCOUNT (profile ${AWS_PROFILE:-default}, region $AWS_REGION)"

cd "$(dirname "$0")/.."

# Document ACLs: comma-separated emails allowed (ACL_ALLOW) / denied (ACL_DENY) on docs/.
# Documents with no ALLOW entry are not ingested, so set ACL_ALLOW.
ACL_ARGS=()
[ -n "${ACL_ALLOW:-}" ] && ACL_ARGS+=(-c "aclAllow=$ACL_ALLOW")
[ -n "${ACL_DENY:-}" ] && ACL_ARGS+=(-c "aclDeny=$ACL_DENY")
[ -n "${ACL_ALLOW:-}" ] || echo "WARNING: ACL_ALLOW is not set; no documents will be ingested."

npx cdk deploy BkbInventoryStack --require-approval never "${ACL_ARGS[@]}" "$@"

echo ""
echo "Connect: AWS_REGION=$AWS_REGION ./scripts/tunnel.sh  ->  http://localhost:8501"
echo "Tear down: AWS_REGION=$AWS_REGION npx cdk destroy BkbInventoryStack"
