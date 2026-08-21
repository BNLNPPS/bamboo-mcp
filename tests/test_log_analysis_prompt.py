"""Guards tying ``_SYSTEM_LOG_ANALYSIS`` to the evidence it is given.

The core-dump probe added ``core_dump_probe_state`` and friends to the log
analysis evidence, but the synthesis prompt was never updated to describe them
— and it carried a rule forbidding root-cause inference from directory
listings and file sizes, which is exactly the shape of what the probe emits.
The model was therefore free to ignore the probe, and did: the same job was
answered once with a section headed "Root Cause — Core Dump" and, thirteen
minutes later, with "No core dump is visible in the pilot log evidence".

A prompt cannot be unit-tested for whether a model obeys it.  What can be
tested is that every evidence key and every state value the probe can produce
is at least *named* in the prompt, so adding a state without documenting it
fails here rather than silently degrading answers in production.
"""
from __future__ import annotations

import pytest

from bamboo.tools.bamboo_executor import _PRESENTATION_KEYS, _SYSTEM_LOG_ANALYSIS

impl = pytest.importorskip(
    "askpanda_atlas.log_analysis_impl",
    reason="ATLAS plugin not installed; the probe constants live there",
)


class TestProbeEvidenceIsDocumented:
    """Everything the probe can emit is described to the synthesising model."""

    def test_every_probe_state_is_named_in_the_prompt(self) -> None:
        """All five states appear, including the negative ones.

        The distinction between them is the whole point of the probe.
        ``truncated`` (zero-length core) and ``timed_out`` (no core, pilot
        killed a looping job) are different facts about what went wrong, and
        ``not_probed`` means the listing was unavailable — reporting it as "no
        core dump" states as fact something that was never checked.
        """
        states = [
            impl.CORE_DUMP_PRESENT,
            impl.CORE_DUMP_TRUNCATED,
            impl.CORE_DUMP_TIMED_OUT,
            impl.CORE_DUMP_ABSENT,
            impl.CORE_DUMP_NOT_PROBED,
        ]
        for state in states:
            assert f"'{state}'" in _SYSTEM_LOG_ANALYSIS, (
                f"probe state {state!r} is not described in _SYSTEM_LOG_ANALYSIS, "
                "so the model has no basis for reporting it correctly"
            )

    def test_every_probe_key_reaching_the_model_is_documented(self) -> None:
        """Keys the model receives are named; presentation keys are excluded.

        ``core_dump_offer_md`` is deliberately absent from the prompt: it is
        stripped by ``_strip_presentation_keys`` before synthesis and appended
        to the rendered answer afterwards.  Documenting it would invite the
        model to reproduce an offer it cannot see — which is what a build
        lacking that strip did, by copying the ready-made string verbatim.
        """
        emitted = impl._build_core_dump_evidence(None, 0)
        model_visible = set(emitted) - set(_PRESENTATION_KEYS)

        assert "core_dump_offer_md" not in model_visible, (
            "core_dump_offer_md should be a presentation key"
        )
        for key in sorted(model_visible):
            assert key in _SYSTEM_LOG_ANALYSIS, (
                f"evidence key {key!r} is sent to the model but not described "
                "in _SYSTEM_LOG_ANALYSIS"
            )

    def test_the_listing_prohibition_is_scoped_to_the_raw_excerpt(self) -> None:
        """The no-inference-from-listings rule must not swallow the probe.

        The rule previously read "never infer a root cause from a directory
        listing or from file sizes in the log excerpt", which reads as an
        instruction to discard ``core_dump_candidates`` — records carrying
        exactly a name and a ``size_bytes``.
        """
        assert "read out of log_excerpt" in _SYSTEM_LOG_ANALYSIS
        assert "structured probe" in _SYSTEM_LOG_ANALYSIS

    def test_the_model_is_told_not_to_write_its_own_offer(self) -> None:
        """The core-dump offer is appended, as the pilot-source offer is.

        Without this the model invents its own "shall I analyse it?" prompt,
        and an affirmative reply then has nothing to match: the follow-up
        intercept keys on the stored offer, not on the answer text.
        """
        assert "Do not offer to fetch or analyse the core dump" in _SYSTEM_LOG_ANALYSIS
