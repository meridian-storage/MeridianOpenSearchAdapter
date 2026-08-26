# SPDX-License-Identifier: Apache-2.0
"""Stable, redacted OpenSearch failure normalization."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Never

from meridian_storage.errors import (
    AuthenticationError,
    AuthorizationError,
    CompatibilityError,
    ConflictError,
    ErrorCode,
    InternalError,
    MeridianTimeoutError,
    NotFoundError,
    RateLimitError,
    SafeCause,
    TransientError,
    UnavailableError,
    ValidationError,
)


class OpenSearchErrorCode(StrEnum):
    AUTHENTICATION = "MERIDIAN_OPENSEARCH_AUTHENTICATION"
    AUTHORIZATION = "MERIDIAN_OPENSEARCH_AUTHORIZATION"
    BULK_REJECTED = "MERIDIAN_OPENSEARCH_BULK_REJECTED"
    INCOMPATIBLE_VERSION = "MERIDIAN_OPENSEARCH_INCOMPATIBLE_VERSION"
    MAPPING_CONFLICT = "MERIDIAN_OPENSEARCH_MAPPING_CONFLICT"
    NOT_FOUND = "MERIDIAN_OPENSEARCH_NOT_FOUND"
    PARTIAL_SHARD_FAILURE = "MERIDIAN_OPENSEARCH_PARTIAL_SHARD_FAILURE"
    RATE_LIMIT = "MERIDIAN_OPENSEARCH_RATE_LIMIT"
    SHARD_UNAVAILABLE = "MERIDIAN_OPENSEARCH_SHARD_UNAVAILABLE"
    TIMEOUT = "MERIDIAN_OPENSEARCH_TIMEOUT"
    VERSION_CONFLICT = "MERIDIAN_OPENSEARCH_VERSION_CONFLICT"


def engine_error_type(exc: BaseException) -> str | None:
    info = getattr(exc, "info", None)
    if not isinstance(info, Mapping):
        return None
    error = info.get("error")
    if isinstance(error, str):
        return error[:128]
    if isinstance(error, Mapping):
        value = error.get("type")
        return value[:128] if isinstance(value, str) else None
    return None


def translate_engine_error(
    exc: BaseException,
    *,
    operation_contract: str | None = None,
    resource_ref: str | None = None,
    request_id: str | None = None,
    execution_id: str | None = None,
) -> Never:
    """Raise one credential-free Meridian error for an OpenSearch client failure."""

    status = getattr(exc, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    error_type = engine_error_type(exc)
    details = {
        "operation_contract": operation_contract,
        "resource_ref": resource_ref,
        "request_id": request_id,
        "execution_id": execution_id,
        "cause": SafeCause(type=type(exc).__name__, code=error_type),
        "adapter_provenance": ({"engineErrorType": error_type} if error_type else {}),
    }
    if status == 401:
        raise AuthenticationError(
            OpenSearchErrorCode.AUTHENTICATION,
            "OpenSearch rejected the configured identity",
            **details,
        ) from exc
    if status == 403:
        raise AuthorizationError(
            OpenSearchErrorCode.AUTHORIZATION,
            "OpenSearch denied the required adapter operation",
            **details,
        ) from exc
    if status == 404:
        raise NotFoundError(
            OpenSearchErrorCode.NOT_FOUND,
            "a required OpenSearch resource is absent",
            **details,
        ) from exc
    if status == 409:
        raise ConflictError(
            OpenSearchErrorCode.VERSION_CONFLICT,
            "OpenSearch rejected a conflicting version or generation change",
            **details,
        ) from exc
    if status == 429:
        raise RateLimitError(
            OpenSearchErrorCode.RATE_LIMIT,
            "OpenSearch rejected work because an engine limit was reached",
            retryable=True,
            **details,
        ) from exc
    if status in {408, 504}:
        raise MeridianTimeoutError(
            OpenSearchErrorCode.TIMEOUT,
            "OpenSearch did not complete the operation before its deadline",
            retryable=True,
            **details,
        ) from exc
    if status in {502, 503}:
        raise UnavailableError(
            OpenSearchErrorCode.SHARD_UNAVAILABLE,
            "OpenSearch is temporarily unavailable",
            retryable=True,
            **details,
        ) from exc
    if status == 400:
        code = (
            OpenSearchErrorCode.MAPPING_CONFLICT
            if error_type in {"mapper_parsing_exception", "strict_dynamic_mapping_exception"}
            else ErrorCode.OPERATION_INVALID
        )
        raise ValidationError(
            code, "OpenSearch rejected the bounded adapter request", **details
        ) from exc
    if status is not None and status >= 500:
        raise TransientError(
            ErrorCode.ADAPTER_FAILURE,
            "OpenSearch failed while executing the adapter operation",
            **details,
        ) from exc
    raise InternalError(
        ErrorCode.ADAPTER_FAILURE,
        "the OpenSearch client failed without a recognized safe status",
        **details,
    ) from exc


def incompatible(message: str) -> Never:
    raise CompatibilityError(OpenSearchErrorCode.INCOMPATIBLE_VERSION, message)


__all__ = [
    "OpenSearchErrorCode",
    "engine_error_type",
    "incompatible",
    "translate_engine_error",
]
