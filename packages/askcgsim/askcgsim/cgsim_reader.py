#!/usr/bin/env python
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

"""CGSim SQLite database reader.

Reads an events database produced by the CGSim simulation framework and
provides structured access to its contents.  Running this module directly
dumps the full contents of a database file to stdout.

Usage::

    python cgsim_reader.py <path/to/cgsim.db>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# Metadata dataclasses — one per (EVENT, STATE) combination
# ---------------------------------------------------------------------------


@dataclass
class JobAllocationStarted:
    """Metadata for a JobAllocation / Started event."""

    site: str
    host: str


@dataclass
class JobAllocationFinished:
    """Metadata for a JobAllocation / Finished event."""

    site: str
    host: str
    site_storage_util: float
    grid_storage_util: float
    site_cpu_util: float
    grid_cpu_util: float


@dataclass
class JobExecutionStarted:
    """Metadata for a JobExecution / Started event."""

    flops: float
    site: str
    host: str
    cores: int
    speed: float
    site_cpu_util: float
    grid_cpu_util: float


@dataclass
class JobExecutionFinished:
    """Metadata for a JobExecution / Finished event."""

    flops: float
    cores: int
    site: str
    host: str
    speed: float
    cost: float
    site_cpu_util: float
    grid_cpu_util: float
    duration: float
    retries: int
    total_io_read_time: float
    file_transfer_queue_time: float
    resource_waiting_queue_time: float
    total_queue_time: float


@dataclass
class FileTransferStarted:
    """Metadata for a FileTransfer / Started event."""

    file: str
    size: int
    source_site: str
    destination_site: str
    bandwidth: float
    latency: float
    link_load: float
    site_storage_util: float
    grid_storage_util: float


@dataclass
class FileTransferFinished:
    """Metadata for a FileTransfer / Finished event."""

    file: str
    size: int
    source_site: str
    destination_site: str
    duration: float
    bandwidth: float
    latency: float
    link_load: float
    site_storage_util: float
    grid_storage_util: float


@dataclass
class FileReadStarted:
    """Metadata for a FileRead / Started event."""

    file: str
    size: int
    site: str
    host: str
    disk: str
    disk_read_bw: float


@dataclass
class FileReadFinished:
    """Metadata for a FileRead / Finished event."""

    file: str
    size: int
    site: str
    host: str
    disk: str
    disk_read_bw: float
    duration: float


@dataclass
class FileWriteStarted:
    """Metadata for a FileWrite / Started event."""

    file: str
    size: int
    site: str
    host: str
    disk: str
    disk_write_bw: float
    site_storage_util: float
    grid_storage_util: float


@dataclass
class FileWriteFinished:
    """Metadata for a FileWrite / Finished event."""

    file: str
    size: int
    site: str
    host: str
    disk: str
    duration: float
    disk_write_bw: float
    site_storage_util: float
    grid_storage_util: float


# Union type for all possible metadata objects.
MetadataType = (
    JobAllocationStarted
    | JobAllocationFinished
    | JobExecutionStarted
    | JobExecutionFinished
    | FileTransferStarted
    | FileTransferFinished
    | FileReadStarted
    | FileReadFinished
    | FileWriteStarted
    | FileWriteFinished
)

# Dispatch table: (EVENT, STATE) -> dataclass constructor.
_METADATA_DISPATCH: dict[tuple[str, str], type] = {
    ("JobAllocation", "Started"): JobAllocationStarted,
    ("JobAllocation", "Finished"): JobAllocationFinished,
    ("JobExecution", "Started"): JobExecutionStarted,
    ("JobExecution", "Finished"): JobExecutionFinished,
    ("FileTransfer", "Started"): FileTransferStarted,
    ("FileTransfer", "Finished"): FileTransferFinished,
    ("FileRead", "Started"): FileReadStarted,
    ("FileRead", "Finished"): FileReadFinished,
    ("FileWrite", "Started"): FileWriteStarted,
    ("FileWrite", "Finished"): FileWriteFinished,
}


def parse_metadata(event: str, state: str, raw: str) -> MetadataType:
    """Deserialise a raw JSON metadata string into the matching dataclass.

    Unknown (event, state) pairs are returned as a plain :class:`dict` so
    that forward-compatibility is not broken when new event types are added
    to the simulator.

    Args:
        event: The EVENT column value (e.g. ``"JobExecution"``).
        state: The STATE column value (``"Started"`` or ``"Finished"``).
        raw:   The raw JSON string stored in the METADATA column.

    Returns:
        A typed dataclass instance, or a plain ``dict`` when the (event,
        state) combination is not recognised.

    Raises:
        json.JSONDecodeError: If *raw* is not valid JSON.
    """
    data: dict[str, Any] = json.loads(raw)
    cls = _METADATA_DISPATCH.get((event, state))
    if cls is None:
        return data  # type: ignore[return-value]
    # Pass only the keys the dataclass knows about so that future additions to
    # the JSON blob do not cause unexpected TypeError exceptions.
    known_fields = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in known_fields})


# ---------------------------------------------------------------------------
# Top-level event row
# ---------------------------------------------------------------------------


@dataclass
class EventRow:
    """A single row from the EVENTS table with a parsed metadata object.

    Attributes:
        id:       Internal SQLite row identifier (_ID column).
        event:    Event type string (e.g. ``"JobExecution"``).
        state:    Lifecycle stage (``"Started"`` or ``"Finished"``).
        status:   Job status at the time of the event.
        job_id:   Identifier of the associated job.
        time:     Simulation clock timestamp.
        metadata: Parsed metadata object; type depends on (event, state).
    """

    id: int
    event: str
    state: str
    status: str
    job_id: str
    time: float
    metadata: MetadataType


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class CGSimReader:
    """Read-only accessor for a CGSim SQLite events database.

    Can be used as a context manager::

        with CGSimReader("cgsim.db") as reader:
            for row in reader.iter_events():
                print(row)

    Args:
        db_path:    Path to the SQLite database file.
        connection: Optional pre-existing :class:`sqlite3.Connection` to
                    reuse (e.g. an in-memory database in tests). When
                    supplied, *db_path* is ignored and the connection is
                    **not** closed on :meth:`close`.
    """

    def __init__(
        self,
        db_path: str = "",
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        """Open a connection to the CGSim SQLite database.

        Args:
            db_path:    Path to the SQLite database file.  Ignored when
                        *connection* is supplied.
            connection: Optional pre-existing :class:`sqlite3.Connection` to
                        reuse (e.g. an in-memory database in tests).  When
                        supplied, the connection is **not** closed on
                        :meth:`close`.
        """
        self._db_path = db_path
        self._external_conn = connection is not None
        self._conn: sqlite3.Connection = connection or sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "CGSimReader":
        """Return *self* for use inside a ``with`` block."""
        return self

    def __exit__(self, *_: Any) -> None:
        """Close the connection unless it was supplied externally."""
        self.close()

    def close(self) -> None:
        """Close the underlying database connection.

        No-op when the connection was supplied by the caller.
        """
        if not self._external_conn:
            self._conn.close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def iter_events(
        self,
        *,
        event: Optional[str] = None,
        state: Optional[str] = None,
        job_id: Optional[str] = None,
        time_range: Optional[tuple[float, float]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[EventRow]:
        """Iterate over rows in the EVENTS table, optionally filtered.

        All filter arguments are optional and combinable.

        Args:
            event:      Keep only rows whose EVENT column equals this value.
            state:      Keep only rows whose STATE column equals this value.
            job_id:     Keep only rows for this job identifier.
            time_range: A ``(min_time, max_time)`` tuple; keeps rows where
                        ``min_time <= TIME <= max_time``.
            limit:      Maximum number of rows to return.

        Yields:
            :class:`EventRow` instances in ascending TIME order.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if event is not None:
            clauses.append("EVENT = ?")
            params.append(event)
        if state is not None:
            clauses.append("STATE = ?")
            params.append(state)
        if job_id is not None:
            clauses.append("JOB_ID = ?")
            params.append(job_id)
        if time_range is not None:
            clauses.append("TIME BETWEEN ? AND ?")
            params.extend(time_range)

        sql = "SELECT _ID, EVENT, STATE, STATUS, JOB_ID, TIME, METADATA FROM EVENTS"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY TIME ASC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        cursor = self._conn.execute(sql, params)
        for raw in cursor:
            yield EventRow(
                id=raw["_ID"],
                event=raw["EVENT"],
                state=raw["STATE"],
                status=raw["STATUS"],
                job_id=raw["JOB_ID"],
                time=raw["TIME"],
                metadata=parse_metadata(raw["EVENT"], raw["STATE"], raw["METADATA"]),
            )

    def get_job_timeline(self, job_id: str) -> list[EventRow]:
        """Return all events for a single job, ordered by simulation time.

        Args:
            job_id: The job identifier to look up.

        Returns:
            A list of :class:`EventRow` objects sorted by TIME ascending.
        """
        return list(self.iter_events(job_id=job_id))

    def row_count(self) -> int:
        """Return the total number of rows in the EVENTS table.

        Returns:
            Integer row count.
        """
        (count,) = self._conn.execute("SELECT COUNT(*) FROM EVENTS").fetchone()
        return count


# ---------------------------------------------------------------------------
# stdout dump
# ---------------------------------------------------------------------------


def dump_to_stdout(db_path: str) -> None:
    """Print every event row in a CGSim database to stdout.

    Output format — one line per row::

        [<id>] t=<time> | <event>/<state> | job=<job_id> | status=<status>
            <field>=<value> ...

    Args:
        db_path: Path to the SQLite database file to read.
    """
    with CGSimReader(db_path) as reader:
        total = reader.row_count()
        print(f"Database : {db_path}")
        print(f"Rows     : {total}")
        print("=" * 72)

        for row in reader.iter_events():
            print(
                f"[{row.id:>6}] t={row.time:<14.4f} | "
                f"{row.event}/{row.state:<8} | "
                f"job={row.job_id} | status={row.status}"
            )
            # Pretty-print metadata fields indented below the header line.
            if isinstance(row.metadata, dict):
                # Unrecognised event type — fall back to raw key/value dump.
                for k, v in row.metadata.items():
                    print(f"           {k}={v}")
            else:
                for fname, fval in row.metadata.__dict__.items():
                    print(f"           {fname}={fval}")
            print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point: ``python cgsim_reader.py <db_path>``.

    Exits with a non-zero status and a usage message when called without
    the required argument.
    """
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <path/to/cgsim.db>", file=sys.stderr)
        sys.exit(1)

    dump_to_stdout(sys.argv[1])


if __name__ == "__main__":
    main()
