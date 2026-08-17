# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# Authors
# - Paul Nilsson, paul.nilsson@cern.ch, 2026

"""Pytest configuration.

These tests are designed to work both when Bamboo is installed (editable or
wheel) and when running directly from a source checkout.

In a clean checkout, the `bamboo` (core), `askpanda_atlas`, and
`askpanda_epic` packages live under `core/`, `packages/askpanda_atlas/`,
and `packages/askpanda_epic/` respectively. Add these directories to
`sys.path` so `pytest` can import them without requiring an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path


def pytest_configure() -> None:
    """Configure sys.path for local-source test runs."""
    repo_root = Path(__file__).resolve().parents[1]

    core_dir = repo_root / "core"
    atlas_pkg_dir = repo_root / "packages" / "askpanda_atlas"
    epic_pkg_dir = repo_root / "packages" / "askpanda_epic"
    cgsim_pkg_dir = repo_root / "packages" / "askcgsim"

    # core/, plugin packages, and the cgsim package are inserted at the front so
    # they take priority over any installed wheels.  repo_root is appended at the
    # end so that interfaces/ is importable without shadowing core/bamboo/ (the
    # repo root also contains a top-level bamboo/__init__.py that would win if
    # placed first).
    for p in (core_dir, atlas_pkg_dir, epic_pkg_dir, cgsim_pkg_dir):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)

    repo_root_s = str(repo_root)
    if repo_root_s not in sys.path:
        sys.path.append(repo_root_s)

    # Install minimal mcp sub-module stubs so that interfaces/agent/agent.py
    # and interfaces/shared/mcp_client.py can be imported in environments where
    # the real mcp SDK is not installed.
    #
    # CAUTION: these stubs shadow a genuine mcp installation too. sys.modules
    # .setdefault only no-ops when the module has already been *imported*, not
    # merely installed, and in a fresh session nothing has imported mcp yet — so
    # the empty ModuleType wins and Server below becomes a MagicMock regardless
    # of which mcp version is present. Consequently no test in this suite
    # exercises the real mcp API, which is how the mcp 2.0.0 removal of the
    # low-level Server decorator API stayed invisible behind 1132 passing tests.
    # tests/test_mcp_server_api_compat.py covers that gap by checking
    # distribution metadata and a clean subprocess instead of importing mcp.
    _mcp_sub_modules = (
        "mcp",
        "mcp.client",
        "mcp.client.session",
        "mcp.client.stdio",
        "mcp.client.streamable_http",
        "mcp.server",
        "mcp.types",
    )
    import types as _types
    for _mod_name in _mcp_sub_modules:
        sys.modules.setdefault(_mod_name, _types.ModuleType(_mod_name))

    from unittest.mock import MagicMock as _MagicMock
    _session_mod = sys.modules["mcp.client.session"]
    if not hasattr(_session_mod, "ClientSession"):
        _session_mod.ClientSession = _MagicMock  # type: ignore[attr-defined]
    _stdio_mod = sys.modules["mcp.client.stdio"]
    if not hasattr(_stdio_mod, "StdioServerParameters"):
        _stdio_mod.StdioServerParameters = _MagicMock  # type: ignore[attr-defined]
    if not hasattr(_stdio_mod, "stdio_client"):
        _stdio_mod.stdio_client = _MagicMock  # type: ignore[attr-defined]
    # bamboo.core imports Server from mcp.server and ListToolsResult/Tool from
    # mcp.types; provide stubs so patch.dict("bamboo.core.TOOLS", ...) works.
    _server_mod = sys.modules["mcp.server"]
    if not hasattr(_server_mod, "Server"):
        _server_mod.Server = _MagicMock  # type: ignore[attr-defined]
    _types_mod = sys.modules["mcp.types"]
    if not hasattr(_types_mod, "ListToolsResult"):
        _types_mod.ListToolsResult = _MagicMock  # type: ignore[attr-defined]
    if not hasattr(_types_mod, "Tool"):
        _types_mod.Tool = _MagicMock  # type: ignore[attr-defined]
