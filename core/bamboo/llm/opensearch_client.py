"""Shared OpenSearch client factory for Bamboo MCP.

All modules that talk to the CERN OpenSearch cluster (prompt logging, harvester
timeseries queries, general index reads) should obtain their client from
:func:`create_os_client` rather than duplicating the connection logic.  This
guarantees that TLS settings, certificate paths, and environment-variable names
stay consistent across the codebase.

Environment variables
---------------------
``ASKPANDA_OPENSEARCH_HOST``
    Base URL of the OpenSearch cluster.
    Default: ``https://os-atlas.cern.ch/os``

``ASKPANDA_OPENSEARCH_USER``
    HTTP Basic-auth username.
    Default: ``pilot-monitor-agent``

``ASKPANDA_OPENSEARCH_CA``
    Path to the CA certificate bundle.
    Default: ``/etc/pki/tls/certs/CERN-bundle.pem``

``ASKPANDA_OPENSEARCH_VERIFY_CERTS``
    Set to ``"false"`` to disable TLS certificate verification.
    Intended for local development without the CERN CA bundle only.

The **password** is passed explicitly by each caller so that different
features (read vs. write) can use different credentials while sharing the
same connection plumbing.
"""
from __future__ import annotations

import os
from typing import Any

# ---------------------------------------------------------------------------
# Shared defaults (also imported by prompt_log.py and harvester_timeseries_impl.py)
# ---------------------------------------------------------------------------

DEFAULT_HOST: str = "https://os-atlas.cern.ch/os"
DEFAULT_USER: str = "pilot-monitor-agent"
DEFAULT_CA: str = "/etc/pki/tls/certs/CERN-bundle.pem"


def create_os_client(password: str) -> Any:
    """Create an authenticated OpenSearch client from environment variables.

    Builds a synchronous :class:`opensearchpy.OpenSearch` instance using the
    shared connection parameters.  TLS certificate verification is enabled by
    default; set ``ASKPANDA_OPENSEARCH_VERIFY_CERTS=false`` to skip it (local
    development without the CERN CA bundle).

    A fresh client is created on every call.  Callers that make repeated
    queries should cache the returned client themselves rather than calling
    this function in a tight loop.

    Args:
        password: HTTP Basic-auth password for the OpenSearch cluster.

    Returns:
        An authenticated :class:`opensearchpy.OpenSearch` client instance.

    Raises:
        ValueError: If *password* is empty.
        ImportError: If ``opensearch-py`` is not installed.
    """
    if not password:
        raise ValueError(
            "OpenSearch password must be non-empty.  "
            "Pass the value of the relevant environment variable explicitly."
        )

    from opensearchpy import OpenSearch  # type: ignore[import]  # optional dep

    host = os.environ.get("ASKPANDA_OPENSEARCH_HOST", DEFAULT_HOST)
    user = os.environ.get("ASKPANDA_OPENSEARCH_USER", DEFAULT_USER)
    ca = os.environ.get("ASKPANDA_OPENSEARCH_CA", DEFAULT_CA)
    verify_raw = os.environ.get("ASKPANDA_OPENSEARCH_VERIFY_CERTS", "true").lower()
    verify: bool = verify_raw != "false"

    client_kwargs: dict[str, Any] = {
        "hosts": [host],
        "http_auth": (user, password),
        "use_ssl": True,
        "verify_certs": verify,
    }
    if verify and os.path.exists(ca):
        client_kwargs["ca_certs"] = ca

    return OpenSearch(**client_kwargs)


__all__ = [
    "create_os_client",
    "DEFAULT_HOST",
    "DEFAULT_USER",
    "DEFAULT_CA",
]
