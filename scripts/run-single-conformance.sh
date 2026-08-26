#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${repository_root}/tests/integration/docker/compose.single.yml"
python_command="${MERIDIAN_PYTHON:-python}"

cleanup_single() {
  docker compose -f "${compose_file}" down --volumes
}
trap cleanup_single EXIT

docker compose -f "${compose_file}" up --build --wait -d
OPENSEARCH_URL="http://localhost:19200" "${python_command}" -m pytest \
  -q "${repository_root}/tests/integration/test_real_opensearch.py"
