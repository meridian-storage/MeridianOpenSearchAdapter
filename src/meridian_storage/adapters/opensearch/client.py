# SPDX-License-Identifier: Apache-2.0
"""Credential-safe OpenSearch client construction behind the adapter boundary."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from meridian_storage.errors import ConfigurationError, ErrorCode
from meridian_storage.spi import AdapterCreateContext
from opensearchpy import OpenSearch


@runtime_checkable
class ServiceResolver(Protocol):
    def resolve(self, service_ref: str) -> str:
        """Resolve an IaC-owned service reference to one private endpoint."""


@runtime_checkable
class ClientProtocol(Protocol):
    indices: Any
    cluster: Any
    nodes: Any
    cat: Any
    transport: Any

    def info(self) -> Mapping[str, Any]: ...

    def ping(self, **kwargs: Any) -> bool: ...

    def search(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def index(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def bulk(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class ClientHandle:
    """Own a client and temporary TLS material for exactly one runtime."""

    def __init__(
        self,
        client: ClientProtocol,
        temporary_directory: tempfile.TemporaryDirectory[str] | None = None,
    ) -> None:
        self.client = client
        self._temporary_directory = temporary_directory
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.client.close()
        finally:
            if self._temporary_directory is not None:
                self._temporary_directory.cleanup()


def create_client_handle(
    context: AdapterCreateContext,
    *,
    service_resolver: ServiceResolver | None = None,
    client_builder: Callable[..., ClientProtocol] = OpenSearch,
) -> ClientHandle:
    binding = context.binding
    endpoint = binding.endpoint
    if endpoint is None:
        if binding.service_ref is None or service_resolver is None:
            raise ConfigurationError(
                ErrorCode.CONFIG_INVALID,
                "an OpenSearch serviceRef requires an injected IaC service resolver",
            )
        endpoint = service_resolver.resolve(binding.service_ref)
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            ErrorCode.CONFIG_INVALID,
            "OpenSearch endpoint must be an origin URL without credentials, "
            "path, query, or fragment",
        )
    if binding.tls.mode == "disabled" and parsed.scheme != "http":
        raise ConfigurationError(
            ErrorCode.CONFIG_INVALID, "disabled TLS requires an http OpenSearch endpoint"
        )
    if binding.tls.mode != "disabled" and parsed.scheme != "https":
        raise ConfigurationError(
            ErrorCode.CONFIG_INVALID, "authenticated TLS requires an https OpenSearch endpoint"
        )

    username = _utf8_secret(context.identity.reveal(), "identity")
    password = _utf8_secret(context.credential.reveal(), "credential")
    temporary: tempfile.TemporaryDirectory[str] | None = None
    ca_path: str | None = None
    certificate_path: str | None = None
    if binding.tls.mode != "disabled":
        if context.tls_ca is None:
            raise ConfigurationError(ErrorCode.CONFIG_INVALID, "TLS CA material is required")
        temporary = tempfile.TemporaryDirectory(prefix="meridian-opensearch-tls-")
        directory = Path(temporary.name)
        ca = directory / "ca.pem"
        ca.write_bytes(context.tls_ca.reveal())
        ca.chmod(0o600)
        ca_path = str(ca)
        if binding.tls.mode == "mutual":
            if context.tls_client_certificate is None:
                temporary.cleanup()
                raise ConfigurationError(
                    ErrorCode.CONFIG_INVALID, "mutual TLS client material is required"
                )
            certificate = directory / "client.pem"
            certificate.write_bytes(context.tls_client_certificate.reveal())
            certificate.chmod(0o600)
            certificate_path = str(certificate)

    options: dict[str, Any] = {
        "hosts": [
            {
                "host": parsed.hostname,
                "port": parsed.port or (443 if parsed.scheme == "https" else 80),
                "scheme": parsed.scheme,
            }
        ],
        "http_auth": (username, password),
        "use_ssl": parsed.scheme == "https",
        "verify_certs": binding.tls.mode != "disabled",
        "ssl_assert_hostname": binding.tls.server_name or True,
        "ssl_show_warn": False,
        "ca_certs": ca_path,
        "client_cert": certificate_path,
        "timeout": binding.client.operation_timeout_ms / 1_000,
        "max_retries": 0,
        "retry_on_timeout": False,
        "pool_maxsize": binding.client.max_size,
    }
    try:
        return ClientHandle(client_builder(**options), temporary)
    except Exception:
        if temporary is not None:
            temporary.cleanup()
        raise


def _utf8_secret(value: bytes, name: str) -> str:
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            ErrorCode.CONFIG_INVALID, f"OpenSearch {name} must be UTF-8 encoded"
        ) from exc
    if not decoded or "\x00" in decoded or len(value) > 4_096:
        raise ConfigurationError(
            ErrorCode.CONFIG_INVALID, f"OpenSearch {name} is invalid or exceeds 4096 bytes"
        )
    return decoded


__all__ = [
    "ClientHandle",
    "ClientProtocol",
    "ServiceResolver",
    "create_client_handle",
]
