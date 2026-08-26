<!-- SPDX-License-Identifier: Apache-2.0 -->

# Failure and consistency model

The OpenSearch index is an eventually consistent derived projection. Search success proves only the
state observed by the selected read profile; it is not proof of an authoritative write, transaction,
audit event, or recovery point.

| Condition | Adapter behavior |
| --- | --- |
| Lower external source sequence | Classify as stale; do not overwrite newer projection state. |
| Bulk item `429`, timeout, or `5xx` | Return a retryable per-item outcome. |
| Bulk item mapping/client failure | Return a permanent per-item outcome. |
| Malformed or count-mismatched bulk response | Fail the complete adapter call closed. |
| Timed-out search or any failed shard | Reject partial results; never normalize them as success. |
| Invalid/expired/replayed cursor context | Reject before issuing a search. |
| Missing PIT id or changed pagination mode | Reject; do not silently fall back to live search. |
| Result, facet, highlight, query, filter, or bulk limit exceeded | Reject with a stable Meridian limit error. |
| Unsupported DSL, regex, wildcard, script, join, transaction, or strong consistency | Reject as an unsupported capability. |
| Authentication/authorization/rate/availability/engine error | Translate to a stable typed Meridian error with redacted safe cause. |
| Engine/plugin/topology/health/mapping/alias/replica drift | Startup or physical probe fails closed. |

Live keyset cursors bind the plan, registry, schema set, scope, and page size, but concurrent refreshes
can still change relevance and membership. PIT cursors additionally bind and reuse an engine snapshot
when that deployment option is enabled. PITs are closed when the final page is observed or explicitly
by the caller after abandoned pagination.
