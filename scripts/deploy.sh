#!/bin/bash
# Deploys both stacks. Run from AWS CloudShell after unzipping the project.
#   ALERT_EMAIL=you@example.com ./scripts/deploy.sh
set -e
export AWS_PAGER=""
: "${ALERT_EMAIL:?Set ALERT_EMAIL to the address that should receive the digest}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="job-alert-pipeline-artifacts-${ACCOUNT}"

# One-time: the bucket that holds the zipped Lambda code.
aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || aws s3 mb "s3://$BUCKET"

cd "$(dirname "$0")/../infrastructure"
aws cloudformation deploy --template-file base-stack.yaml --stack-name job-alert-pipeline-base \
  --capabilities CAPABILITY_NAMED_IAM --parameter-overrides AlertEmail="$ALERT_EMAIL"
aws cloudformation package --template-file functions-stack.yaml --s3-bucket "$BUCKET" --output-template-file packaged.yaml
aws cloudformation deploy --template-file packaged.yaml --stack-name job-alert-pipeline-functions --parameter-overrides MinScore=70
echo "Deployed."
