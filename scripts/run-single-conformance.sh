#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
export OPENSEARCH_VERSION="${OPENSEARCH_VERSION:-2.19.1}"
if [[ -n "${OPENSEARCH_IMAGE:-}" && ! "${OPENSEARCH_IMAGE}" =~ @sha256:[a-f0-9]{64}$ ]]; then
  echo "OPENSEARCH_IMAGE must be locked by sha256 digest" >&2
  exit 1
fi

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repository_root}/tests/integration/docker/compose.single.yml"
python_command="${MERIDIAN_PYTHON:-python}"
compose_project="meridian-opensearch-single-${OPENSEARCH_VERSION//./-}"

cleanup_single() {
  docker compose -p "${compose_project}" -f "${compose_file}" down --volumes
}
trap cleanup_single EXIT

docker compose -p "${compose_project}" -f "${compose_file}" up --build --wait -d
OPENSEARCH_URL="http://localhost:19200" "${python_command}" -m pytest \
  -q "${repository_root}/tests/integration/test_real_opensearch.py"
