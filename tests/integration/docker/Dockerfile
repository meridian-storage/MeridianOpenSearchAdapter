# SPDX-License-Identifier: Apache-2.0
ARG OPENSEARCH_VERSION=2.19.1
FROM opensearchproject/opensearch:${OPENSEARCH_VERSION}@sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4

RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu
