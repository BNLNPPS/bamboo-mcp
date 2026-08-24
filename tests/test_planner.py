import asyncio
import json

import pytest

from bamboo.tools import planner as planner_mod


def test_extract_first_json_object_from_code_fence():
    text = """Here you go:

```json
{"route":"FAST_PATH","confidence":0.9,"tool_calls":[{"tool":"panda_task_status","arguments":{}}],"retrieval_query":null,"reuse_policy":{"allow_final_answer_reuse":false,"allow_pattern_reuse":true,"requires_fresh_evidence":true},"explain":"ok"}
```
"""
    extracted = planner_mod.extract_first_json_object(text)
    parsed = json.loads(extracted)
    assert parsed["route"] == "FAST_PATH"


def test_plan_schema_contains_expected_fields():
    schema = planner_mod.get_plan_json_schema()
    assert schema.get("title") == "Plan"
    props = schema.get("properties", {})
    for key in ("route", "confidence", "tool_calls", "retrieval_query", "reuse_policy", "explain"):
        assert key in props


def test_plan_semantic_validation_requires_tool_calls_for_plan_route():
    bad = {
        "route": "PLAN",
        "confidence": 0.5,
        "tool_calls": [],
        "retrieval_query": None,
        "reuse_policy": {
            "allow_final_answer_reuse": False,
            "allow_pattern_reuse": True,
            "requires_fresh_evidence": True,
        },
        "explain": "",
    }
    with pytest.raises(Exception):
        planner_mod.Plan.model_validate(bad)


def test_planner_tool_repairs_invalid_first_response(monkeypatch):
    # Avoid entry-point scanning in unit tests.
    monkeypatch.setattr(
        planner_mod,
        "_collect_tool_catalog",
        lambda namespaces=None: [
            {
                "name": "panda_task_status",
                "description": "Get task metadata",
                "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer"}}},
            }
        ],
    )

    calls = {"n": 0}

    async def fake_call_default_llm(messages, temperature, max_tokens):  # pylint: disable=unused-argument
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json at all"
        return json.dumps(
            {
                "route": "FAST_PATH",
                "confidence": 0.91,
                "tool_calls": [{"tool": "panda_task_status", "arguments": {"task_id": 123}}],
                "retrieval_query": None,
                "reuse_policy": {
                    "allow_final_answer_reuse": False,
                    "allow_pattern_reuse": True,
                    "requires_fresh_evidence": True,
                },
                "explain": "Task ID detected.",
            }
        )

    monkeypatch.setattr(planner_mod, "_call_default_llm", fake_call_default_llm)

    tool = planner_mod.bamboo_plan_tool
    res = asyncio.run(tool.call({"question": "task 123 status?"}))
    assert isinstance(res, list)
    assert res and res[0]["type"] == "text"
    plan = json.loads(res[0]["text"])
    assert plan["route"] == "FAST_PATH"
    assert calls["n"] == 2


def test_planner_tool_execute_true_passes_plugin_id_to_execute_plan(monkeypatch):
    """When execute=True, PlannerTool.call must thread plugin_id through to
    execute_plan.

    Regression test: this was previously omitted, so execute_plan always
    ran with its "atlas" default regardless of the active plugin — a latent
    bug that matters more now that unmatched (no fast-path signal)
    questions defer to the LLM planner for every plugin, not just atlas.
    """
    monkeypatch.setattr(
        planner_mod,
        "_collect_tool_catalog",
        lambda namespaces=None: [
            {
                "name": "cgsim.sim_query",
                "description": "Query the simulation database",
                "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}},
            }
        ],
    )

    async def fake_call_default_llm(messages, temperature, max_tokens):  # pylint: disable=unused-argument
        return json.dumps(
            {
                "route": "FAST_PATH",
                "confidence": 0.9,
                "tool_calls": [{"tool": "cgsim.sim_query", "arguments": {"question": "how many jobs?"}}],
                "retrieval_query": None,
                "reuse_policy": {
                    "allow_final_answer_reuse": False,
                    "allow_pattern_reuse": True,
                    "requires_fresh_evidence": True,
                },
                "explain": "CGSim query.",
            }
        )

    monkeypatch.setattr(planner_mod, "_call_default_llm", fake_call_default_llm)

    captured: dict = {}

    async def fake_execute_plan(plan, question, history, plugin_id="atlas"):  # pylint: disable=unused-argument
        captured["plugin_id"] = plugin_id
        return [{"type": "text", "text": "42 jobs."}]

    monkeypatch.setattr(
        "bamboo.tools.bamboo_executor.execute_plan", fake_execute_plan
    )

    tool = planner_mod.bamboo_plan_tool
    res = asyncio.run(tool.call({
        "question": "how many jobs?",
        "execute": True,
        "plugin_id": "cgsim",
    }))
    assert res[0]["text"] == "42 jobs."
    assert captured["plugin_id"] == "cgsim"


def test_planner_tool_execute_true_defaults_plugin_id_when_missing(monkeypatch):
    """When execute=True and plugin_id is omitted entirely, execute_plan
    still receives 'atlas' rather than an empty string."""
    monkeypatch.setattr(
        planner_mod,
        "_collect_tool_catalog",
        lambda namespaces=None: [
            {
                "name": "panda_doc_search",
                "description": "Search docs",
                "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ],
    )

    async def fake_call_default_llm(messages, temperature, max_tokens):  # pylint: disable=unused-argument
        return json.dumps(
            {
                "route": "RETRIEVE",
                "confidence": 0.8,
                "tool_calls": [{"tool": "panda_doc_search", "arguments": {"query": "what is PanDA?"}}],
                "retrieval_query": None,
                "reuse_policy": {
                    "allow_final_answer_reuse": False,
                    "allow_pattern_reuse": True,
                    "requires_fresh_evidence": True,
                },
                "explain": "Doc search.",
            }
        )

    monkeypatch.setattr(planner_mod, "_call_default_llm", fake_call_default_llm)

    captured: dict = {}

    async def fake_execute_plan(plan, question, history, plugin_id="atlas"):  # pylint: disable=unused-argument
        captured["plugin_id"] = plugin_id
        return [{"type": "text", "text": "PanDA is a workload manager."}]

    monkeypatch.setattr(
        "bamboo.tools.bamboo_executor.execute_plan", fake_execute_plan
    )

    tool = planner_mod.bamboo_plan_tool
    res = asyncio.run(tool.call({"question": "what is PanDA?", "execute": True}))
    assert res[0]["text"] == "PanDA is a workload manager."
    assert captured["plugin_id"] == "atlas"


# ---------------------------------------------------------------------------
# ATLAS routing prompt content — job_stats field coverage
# ---------------------------------------------------------------------------
#
# Regression tests for the "which python versions are used" incident: the
# jobmetrics-derived fields (python_version, os_version, lsetup_time,
# leak_slope/leak_intersect/leak_chi2) were added to job_stats_schema.py and
# bamboo_answer.py's fast-path signals, but the LLM planner's own
# atlas.job_stats routing description was never updated to mention them —
# the third file in the "three-file edit problem" that got missed.


def test_atlas_prompt_mentions_python_version():
    """The ATLAS planner prompt tells the LLM that python version questions
    route to atlas.job_stats."""
    schema = planner_mod.get_plan_json_schema()
    prompt = planner_mod.build_planner_system_prompt(schema, plugin_id="atlas")
    assert "python version" in prompt.lower()


def test_atlas_prompt_mentions_os_version():
    """The ATLAS planner prompt tells the LLM that OS version questions
    route to atlas.job_stats."""
    schema = planner_mod.get_plan_json_schema()
    prompt = planner_mod.build_planner_system_prompt(schema, plugin_id="atlas")
    assert "os version" in prompt.lower()


def test_atlas_prompt_mentions_memory_leak():
    """The ATLAS planner prompt tells the LLM that memory-leak questions
    route to atlas.job_stats."""
    schema = planner_mod.get_plan_json_schema()
    prompt = planner_mod.build_planner_system_prompt(schema, plugin_id="atlas")
    assert "memory leak" in prompt.lower() or "memory-leak" in prompt.lower()


def test_atlas_prompt_mentions_lsetup_time():
    """The ATLAS planner prompt tells the LLM that lsetup time questions
    route to atlas.job_stats."""
    schema = planner_mod.get_plan_json_schema()
    prompt = planner_mod.build_planner_system_prompt(schema, plugin_id="atlas")
    assert "lsetup time" in prompt.lower()


def test_atlas_prompt_python_version_example_routes_to_job_stats():
    """The 'which python versions are used' example phrase appears in the
    same routing rule as atlas.job_stats, not just anywhere in the prompt."""
    schema = planner_mod.get_plan_json_schema()
    prompt = planner_mod.build_planner_system_prompt(schema, plugin_id="atlas")
    # Find the job_stats routing bullet and confirm both the example phrase
    # and the tool name appear within it, rather than merely somewhere in
    # the overall prompt (which would not prove they're linked).
    idx = prompt.lower().find("historical opensearch index")
    assert idx != -1, "job_stats routing bullet not found in prompt"
    bullet = prompt[idx:idx + 1200]
    assert "python versions are used" in bullet.lower()
    assert "atlas.job_stats" in bullet
