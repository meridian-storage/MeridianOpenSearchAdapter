# SPDX-License-Identifier: Apache-2.0
"""Deterministic, engine-private names and canonical values."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from meridian_storage.semantics import JsonValue, canonical_json_bytes, sha256_fingerprint

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_PREFIX_INVALID = re.compile(r"[^a-z0-9_-]+")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


def fingerprint(value: JsonValue) -> str:
    """Return Meridian's canonical SHA-256 form."""

    return sha256_fingerprint(value)


def require_fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"{name} must be a sha256 fingerprint")
    return value


def safe_token(value: object, name: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def contract_token(value: object, name: str) -> str:
    result = safe_token(value, name)
    if _SAFE_TOKEN.fullmatch(result) is None:
        raise ValueError(f"{name} must be a contract token")
    return result


def index_prefix(value: object) -> str:
    text = safe_token(value, "index prefix", maximum=128).casefold()
    text = _PREFIX_INVALID.sub("-", text).strip("-_")
    if not text:
        raise ValueError("index prefix must contain an ASCII letter or digit")
    return text[:80]


def resource_digest(resource: str) -> str:
    return hashlib.sha256(resource.encode("utf-8")).hexdigest()[:24]


def field_name(logical_name: str) -> str:
    return "f_" + hashlib.sha256(logical_name.encode("utf-8")).hexdigest()[:24]


def path_name(field: str, pointer: str) -> str:
    payload = f"{field}\x00{pointer}".encode()
    return "p_" + hashlib.sha256(payload).hexdigest()[:24]


def generation_name(prefix: str, resource: str, generation: int) -> str:
    if isinstance(generation, bool) or not 1 <= generation <= 999_999:
        raise ValueError("generation must be between 1 and 999999")
    return f"{index_prefix(prefix)}-i-{resource_digest(resource)}-g{generation:06d}"


def read_alias(prefix: str, resource: str) -> str:
    return f"{index_prefix(prefix)}-r-{resource_digest(resource)}"


def write_alias(prefix: str, resource: str) -> str:
    return f"{index_prefix(prefix)}-w-{resource_digest(resource)}"


def json_value(value: Any) -> JsonValue:
    """Normalize supported logical values without permitting non-finite JSON."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not valid Meridian values")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimals are not valid Meridian values")
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return int(value.total_seconds() * 1_000_000)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.urlsafe_b64encode(bytes(value)).rstrip(b"=").decode("ascii")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Meridian JSON object keys must be strings")
        return {str(key): json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_value(item) for item in value]
    raise TypeError(f"unsupported Meridian value type: {type(value).__name__}")


def canonical_record_id(value: object) -> str:
    normalized = json_value(value)
    if normalized is None or isinstance(normalized, Mapping):
        raise ValueError("record identity must be a scalar or ordered scalar tuple")
    if isinstance(normalized, list) and (
        not normalized
        or any(item is None or isinstance(item, (list, Mapping)) for item in normalized)
    ):
        raise ValueError("compound record identity must contain non-null scalar values")
    return canonical_json_bytes(normalized).decode("utf-8")


def document_id(scope: str, resource: str, record_id: object) -> str:
    payload: JsonValue = {
        "recordId": json.loads(canonical_record_id(record_id)),
        "resource": resource,
        "scope": require_fingerprint(scope, "scope fingerprint"),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def scope_fingerprint(tenant: str | None, scope: Mapping[str, str]) -> str:
    if tenant is None and not scope:
        raise ValueError("search projection access requires a tenant or explicit scope")
    normalized = {
        "scope": {
            safe_token(key, "scope key", maximum=64): safe_token(value, "scope value")
            for key, value in sorted(scope.items())
        },
        "tenant": None if tenant is None else safe_token(tenant, "tenant"),
    }
    return fingerprint(cast(JsonValue, normalized))


def canonical_mapping(value: Mapping[str, object]) -> JsonValue:
    normalized = json_value(value)
    if not isinstance(normalized, Mapping):
        raise TypeError("value must normalize to an object")
    return cast(JsonValue, normalized)


__all__ = [
    "canonical_mapping",
    "canonical_record_id",
    "contract_token",
    "document_id",
    "field_name",
    "fingerprint",
    "generation_name",
    "index_prefix",
    "json_value",
    "path_name",
    "read_alias",
    "require_fingerprint",
    "resource_digest",
    "safe_token",
    "scope_fingerprint",
    "write_alias",
]
