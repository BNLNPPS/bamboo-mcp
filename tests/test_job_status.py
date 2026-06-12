"""Tests for panda_job_status tool."""
import asyncio
import json

from bamboo.tools.job_status import panda_job_status_tool


def _unpack(result):
    """Deserialise the JSON-wrapped MCPContent returned by the tool.

    Args:
        result: Return value of tool.call().

    Returns:
        Deserialised dict with ``evidence`` and ``text`` keys.
    """
    return json.loads(result[0]["text"])


SAMPLE_JOB = {
    "pandaid": 6837798305,
    "jobstatus": "closed",
    "jobsubstatus": "toreassign",
    "jobname": "user.test/.6837798305",
    "produsername": "Test User",
    "computingsite": "ROMANIA07_HTCondor",
    "cloud": "FR",
    "atlasrelease": "Atlas-25.2.66",
    "transformation": "runGen-00-00-02",
    "jeditaskid": 46703290,
    "attemptnr": 1,
    "maxattempt": 3,
    "creationtime": "2025-10-09 16:49:45",
    "starttime": None,
    "endtime": "2025-10-10 14:10:23",
    "duration": "0:0:00:00",
    "commandtopilot": "tobekilled",
    "piloterrorcode": 0,
    "piloterrordiag": "",
    "taskbuffererrorcode": 100,
    "taskbuffererrordiag": "reassigned by JEDI",
    "file_summary_str": "input: 6, size: 9.48GB; output: 1; log: 1",
}

SAMPLE_FILES = [
    {"type": "input", "status": "ready", "lfn": "file1.root"},
    {"type": "output", "status": "failed", "lfn": "output.root"},
    {"type": "log", "status": "failed", "lfn": "log.tgz"},
]

# cached_fetch_jsonish returns (status_code, content_type, body_text, parsed_json)
_SAMPLE_PAYLOAD = {"job": SAMPLE_JOB, "files": SAMPLE_FILES, "dsfiles": []}
SAMPLE_FETCH_OK = (200, "application/json", json.dumps(_SAMPLE_PAYLOAD), _SAMPLE_PAYLOAD)

SAMPLE_FETCH_HTTP_ERROR = (503, "text/plain", "Service Unavailable", None)

SAMPLE_FETCH_NOT_FOUND = (
    200,
    "application/json",
    json.dumps({"files": [], "dsfiles": []}),
    {"files": [], "dsfiles": []},
)


def test_job_status_success(monkeypatch):
    """Test successful job metadata fetch and evidence extraction."""
    monkeypatch.setattr(
        "askpanda_atlas._cache.cached_fetch_jsonish",
        lambda url, **kw: SAMPLE_FETCH_OK,
    )
    result = asyncio.run(panda_job_status_tool.call({"job_id": 6837798305}))
    res = _unpack(result)
    ev = res["evidence"]
    assert ev["job_id"] == 6837798305
    assert ev["jobstatus"] == "closed"
    assert ev["computingsite"] == "ROMANIA07_HTCondor"
    assert ev["taskbuffererrordiag"] == "reassigned by JEDI"
    assert ev["jeditaskid"] == 46703290
    assert ev["error"] is None
    assert "reassigned by JEDI" in res["text"]


def test_job_status_files_summary(monkeypatch):
    """Test that files_summary correctly counts types and statuses."""
    monkeypatch.setattr(
        "askpanda_atlas._cache.cached_fetch_jsonish",
        lambda url, **kw: SAMPLE_FETCH_OK,
    )
    result = asyncio.run(panda_job_status_tool.call({"job_id": 6837798305}))
    res = _unpack(result)
    fs = res["evidence"]["files_summary"]
    assert fs["total"] == 3
    assert fs["by_type"]["input"] == 1
    assert fs["by_type"]["output"] == 1
    assert fs["by_status"]["failed"] == 2
    assert "output.root" in fs["failed_files"]


def test_job_status_http_error(monkeypatch):
    """Test graceful handling when BigPanDA returns a non-2xx status."""
    monkeypatch.setattr(
        "askpanda_atlas._cache.cached_fetch_jsonish",
        lambda url, **kw: SAMPLE_FETCH_HTTP_ERROR,
    )
    result = asyncio.run(panda_job_status_tool.call({"job_id": 9999}))
    res = _unpack(result)
    assert res["evidence"]["error"] is not None
    assert "503" in res["evidence"]["error"]


def test_job_status_missing_job_id():
    """Test validation when job_id is missing."""
    result = asyncio.run(panda_job_status_tool.call({}))
    assert _unpack(result)["evidence"]["error"] == "missing job_id"


def test_job_status_invalid_job_id():
    """Test validation when job_id is not an integer."""
    result = asyncio.run(panda_job_status_tool.call({"job_id": "notanint"}))
    assert "integer" in _unpack(result)["evidence"]["error"]


def test_job_status_not_found(monkeypatch):
    """Test handling when BigPanDA response has no 'job' key."""
    monkeypatch.setattr(
        "askpanda_atlas._cache.cached_fetch_jsonish",
        lambda url, **kw: SAMPLE_FETCH_NOT_FOUND,
    )
    result = asyncio.run(panda_job_status_tool.call({"job_id": 9999}))
    ev = _unpack(result)["evidence"]
    assert ev.get("not_found") is True
    assert ev["error"] is not None


def test_job_status_fetch_exception(monkeypatch):
    """Test graceful handling when the HTTP call itself raises."""
    def _raise(url, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr("askpanda_atlas._cache.cached_fetch_jsonish", _raise)
    result = asyncio.run(panda_job_status_tool.call({"job_id": 9999}))
    ev = _unpack(result)["evidence"]
    assert ev["error"] is not None
    assert "connection refused" in ev["error"]


def test_get_definition():
    """Test that get_definition returns required MCP fields."""
    d = panda_job_status_tool.get_definition()
    assert d["name"] == "panda_job_status"
    assert "job_id" in d["inputSchema"]["properties"]
    assert d["inputSchema"]["required"] == ["job_id"]
