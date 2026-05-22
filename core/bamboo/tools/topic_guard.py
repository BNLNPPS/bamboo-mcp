"""Two-stage topic guardrail for the Bamboo MCP server.

Prevents the server from answering questions unrelated to PanDA, ATLAS, ePIC,
CGSim, and HEP/distributed-computing workflow systems, thus avoiding
unnecessary LLM costs.

Classification runs in two stages:

1. **Keyword fast-path** (free, synchronous) — matches known allow/deny
   terms against the lowercased question and returns immediately when a
   clear signal is found.

2. **LLM classification** (fast profile, ~50 tokens) — used only for
   questions that the keyword stage cannot classify confidently.  The model
   is instructed to be permissive: if there is any plausible connection to
   PanDA, ATLAS, ePIC, CGSim, SimGrid, or distributed HEP computing, the
   question is allowed.

On any LLM failure the guard **allows** the question through — a guardrail
failure must never silently block a legitimate user.  The failure is logged
to stderr.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

from bamboo.llm.runtime import get_llm_manager, get_llm_selector
from bamboo.llm.types import GenerateParams, Message

# ---------------------------------------------------------------------------
# Keyword lists
# ---------------------------------------------------------------------------

# Questions containing any of these terms are allowed immediately.
_ALLOW_TERMS: list[str] = [
    # Core PanDA concepts
    "panda", "bigpanda", "pandaid", "panda id",
    # ATLAS / HEP
    "atlas", "cern", "lhc", "hep", "high energy physics",
    "wlcg", "grid", "egi",
    # ePIC / EIC
    "epic", "eic", "bnl", "brookhaven", "electron-ion collider",
    "electron ion collider", "rhic",
    # CGSim / SimGrid
    "cgsim", "simgrid", "cgsim plugin", "simgrid plugin",
    "assignjob", "getresourceinformation", "onjobend", "onsimulationend",
    "netzone", "calibration", "job wall time", "job queue time",
    "simulation", "simulator", "computing grid", "distributed computing",
    # Workflow / job concepts
    "task", "job", "pilot", "brokerage", "workload", "workflow",
    "queue", "site", "nucleus", "harvester", "idds", "idd",
    "jedi", "deft", "prodsys", "pathena", "prun",
    # Data / storage
    "rucio", "dataset", "container", "did", "replica", "rse",
    # Status / operations
    "task status", "job status", "log analysis", "pilot error",
    "job failure", "error code", "exit code", "retries",
    # Infrastructure
    "condor", "arc", "cream", "htcondor", "slurm", "batch",
    "computing element", "worker node",
    # Social / conversational — greetings and acknowledgements must never be
    # refused.  bamboo_answer intercepts these before the guard runs, but
    # listing them here ensures they pass keyword_allow even if that
    # intercept is somehow bypassed (defence in depth).
    "hello", "hi", "hey", "thanks", "thank you", "cheers",
    "good morning", "good afternoon", "good evening",
    # Bamboo self-observability — prompt-log and session queries.
    # These questions are directed at Bamboo itself (e.g. "how many turns
    # today?", "which model am I using?", "replay my last session") and
    # must never be rejected as off-topic.
    "bamboo", "opensearch", "prompt log", "prompt logging",
    "session", "sessions", "turns", "turn number",
    "which model", "which tools", "tools used", "tool usage",
    "token count", "token counts", "input tokens", "output tokens",
    "replay session", "last session", "most recent",
    "how many turns", "how many sessions",
]

# Questions containing any of these terms (and none of the allow terms) are
# denied immediately — clearly off-topic.
_DENY_TERMS: list[str] = [
    # Food / lifestyle
    "recipe", "cook", "ingredient", "restaurant", "meal", "diet",
    "nutrition", "calorie",
    # Entertainment
    "movie", "film", "actor", "actress", "music", "song", "lyrics",
    "spotify", "netflix", "youtube",
    # Sports
    "football", "soccer", "basketball", "baseball", "cricket",
    "tennis", "golf", "nfl", "nba", "fifa", "premier league",
    # Finance / markets
    "stock price", "share price", "crypto", "bitcoin", "ethereum",
    "forex", "trading", "investment advice",
    # Weather / geography (non-CERN)
    "weather", "forecast", "temperature outside",
    # Creative writing
    "write a poem", "write me a poem", "write a story",
    "write a song", "creative writing",
    # Explicit personal / social
    "relationship advice", "dating", "horoscope", "zodiac",
    # Other AI assistants
    "chatgpt", "gemini", "copilot", "midjourney",
]

# Pre-compile as sets of lowercased strings for O(1) term lookup, and
# as a single regex for multi-word phrases.
_ALLOW_SET: set[str] = {t.lower() for t in _ALLOW_TERMS if " " not in t}
_ALLOW_PHRASES: list[re.Pattern[str]] = [
    re.compile(r"\b" + re.escape(t.lower()) + r"\b")
    for t in _ALLOW_TERMS
    if " " in t
]
_DENY_SET: set[str] = {t.lower() for t in _DENY_TERMS if " " not in t}
_DENY_PHRASES: list[re.Pattern[str]] = [
    re.compile(r"\b" + re.escape(t.lower()) + r"\b")
    for t in _DENY_TERMS
    if " " in t
]

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

_REJECTION_MESSAGE = (
    "I can only answer questions about PanDA, ATLAS, ePIC, AskCGSim, and related "
    "HEP workflow and distributed computing systems. Please ask me something "
    "about tasks, jobs, pilots, sites, grid computing, or simulation."
)


@dataclass
class GuardResult:
    """Result of a topic classification check.

    Attributes:
        allowed: True if the question is on-topic and should be processed.
        reason: Short code describing how the verdict was reached.
            One of ``"keyword_allow"``, ``"keyword_deny"``, ``"llm_allow"``,
            ``"llm_deny"``, or ``"llm_error_allow"``.
        llm_used: True if the LLM classifier was invoked for this check.
        rejection_message: Human-readable message to return to the user when
            ``allowed`` is False.  Empty string when ``allowed`` is True.
    """

    allowed: bool
    reason: str
    llm_used: bool
    rejection_message: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _keyword_verdict(question_lower: str) -> str | None:
    """Return ``'allow'``, ``'deny'``, or ``None`` (ambiguous).

    Args:
        question_lower: Lowercased question text.

    Returns:
        ``'allow'`` if an allow term is found, ``'deny'`` if a deny term is
        found (and no allow term), or ``None`` if the question is ambiguous.
    """
    tokens = set(re.findall(r"\b\w+\b", question_lower))

    # Check allow terms first — permissive bias.
    if tokens & _ALLOW_SET or any(p.search(question_lower) for p in _ALLOW_PHRASES):
        return "allow"

    if tokens & _DENY_SET or any(p.search(question_lower) for p in _DENY_PHRASES):
        return "deny"

    return None


_LLM_SYSTEM = (
    "You are a topic classifier for AskPanDA, an assistant specialised in "
    "the PanDA workload management system, ATLAS and ePIC experiment workflows, "
    "CGSim / SimGrid distributed computing simulation, and "
    "distributed high-energy physics (HEP) computing.\n\n"
    "Classify whether the user's question is relevant to this domain.\n\n"
    "Rules:\n"
    "- Reply with exactly one word: ALLOW or DENY.\n"
    "- Be PERMISSIVE: if the question could plausibly relate to PanDA, "
    "ATLAS, ePIC, the Electron-Ion Collider (EIC), CGSim, SimGrid, "
    "grid computing, distributed systems, HEP software, or any technology "
    "used in that context (Python, containers, databases, networking, etc.), "
    "reply ALLOW.\n"
    "- Reply DENY only when the question is clearly unrelated — e.g. "
    "cooking, sports, entertainment, personal advice, or unrelated finance.\n"
    "- No explanation. One word only."
)


async def _llm_classify(question: str) -> bool:
    """Ask the fast LLM profile to classify the question.

    Args:
        question: The user's question.

    Returns:
        True if the LLM considers the question on-topic, False otherwise.
        Returns True on any error (fail-open policy).
    """
    try:
        selector = get_llm_selector()
        manager = get_llm_manager()

        fast_profile: str = getattr(selector, "fast_profile", "fast")
        registry: Any = getattr(selector, "registry", None)
        if registry is None:
            return True  # No registry — fail open.

        model_spec = registry.get(fast_profile)
        client = await manager.get_client(model_spec)

        messages: list[Message] = [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": question},
        ]
        resp = await client.generate(
            messages=messages,
            params=GenerateParams(temperature=0.0, max_tokens=5),
        )
        verdict = resp.text.strip().upper()
        return not verdict.startswith("DENY")

    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(
            f"[topic_guard] LLM classifier failed, failing open: {exc}",
            file=sys.stderr,
        )
        return True  # Fail open — never block a user due to a guard error.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def check_topic(question: str) -> GuardResult:
    """Run the two-stage topic guard on a user question.

    Stage 1 is a free keyword check.  Stage 2 (LLM) is only invoked when
    Stage 1 cannot reach a confident verdict.

    Args:
        question: The raw user question string.

    Returns:
        GuardResult: Classification result.  Check ``allowed`` before
        proceeding; use ``rejection_message`` as the response text when
        ``allowed`` is False.
    """
    question_lower = question.lower()
    keyword_verdict = _keyword_verdict(question_lower)

    if keyword_verdict == "allow":
        return GuardResult(allowed=True, reason="keyword_allow", llm_used=False)

    if keyword_verdict == "deny":
        return GuardResult(
            allowed=False,
            reason="keyword_deny",
            llm_used=False,
            rejection_message=_REJECTION_MESSAGE,
        )

    # Ambiguous — escalate to LLM classifier.
    allowed = await _llm_classify(question)
    reason = "llm_allow" if allowed else "llm_deny"
    return GuardResult(
        allowed=allowed,
        reason=reason,
        llm_used=True,
        rejection_message="" if allowed else _REJECTION_MESSAGE,
    )


__all__ = ["GuardResult", "check_topic"]
