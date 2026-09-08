# SPDX-License-Identifier: Apache-2.0
ARG OPENSEARCH_IMAGE=opensearchproject/opensearch:2.19.1@sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4
FROM ${OPENSEARCH_IMAGE}

RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu
