#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repository_root}/tests/integration/docker/compose.cluster.yml"
python_command="${MERIDIAN_PYTHON:-python}"

cleanup_cluster() {
  docker compose -f "${compose_file}" down --volumes
}
trap cleanup_cluster EXIT

docker compose -f "${compose_file}" up --build --wait -d
OPENSEARCH_URL="http://localhost:19200" "${python_command}" -m pytest \
  -q "${repository_root}/tests/integration/test_cluster_conformance.py" -k three_node
docker compose -f "${compose_file}" stop opensearch-node3
OPENSEARCH_URL="http://localhost:19200" OPENSEARCH_NODE_LOSS=1 "${python_command}" -m pytest \
  -q "${repository_root}/tests/integration/test_cluster_conformance.py" -k node_loss
