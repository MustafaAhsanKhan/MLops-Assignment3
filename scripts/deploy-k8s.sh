#!/bin/zsh
# Export a registered model version, build its image and roll it out to Kubernetes.
#   scripts/deploy-k8s.sh [version | alias]     (default: alias "candidate")
set -euo pipefail
cd "${0:A:h}/.."
set -a; source .env; set +a
export MLFLOW_DISABLE_AGENT_HINT=1
V=$(.venv/bin/python serving/export_model.py "${1:-candidate}" 2>/dev/null | tail -1)
TAG="model-v$V"
echo "Deploying breast-cancer-classifier v$V as prediction-api:$TAG"
docker build -q -t "prediction-api:$TAG" .
sed -i '' -E "s/model-v[0-9]+/$TAG/g" kubernetes/deployment.yaml
kubectl apply -f kubernetes/namespace.yaml
kubectl apply -f kubernetes/
kubectl -n "$K8S_NAMESPACE" rollout status deployment/prediction-api --timeout=180s
