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

    for p in (repo_root, core_dir, atlas_pkg_dir, epic_pkg_dir, cgsim_pkg_dir):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
