"""Literal evidence selection, byte accounting, and shared builder contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import ContextAssembly, PrepareContextRequest
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.prepared_context import PreparedContextBuilder, PreparedMemoryCandidates
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_REF = ArtifactRef(family="memory", artifact_id="memory", revision=7)


def _group(scope: str, text: str, *, entry: str = "entry") -> PreparedMemoryCandidates:
    return PreparedMemoryCandidates(
        scope_id=scope,
        memory_ref=_REF,
        hits=(
            MemoryHit(
                memory_ref=_REF, entry_id=entry, entry_version_id="version", text=text, score=1, matched_by=("fts",)
            ),
        ),
    )


def _build(groups, *, query="cobalt sensor", budget=1400, **options):
    return PreparedContextBuilder().build_scopes_result(
        current_scope_id="one",
        request=PrepareContextRequest(query=query, max_bytes=budget, evidence_selection=True, **options),
        memory_candidates=groups,
    )


def _check(result, groups, budget):
    content = result.context.content or ""
    assert result.context.content_bytes == len(content.encode()) <= budget
    if not content:
        assert result.fragments == result.origins == ()
        return
    assert content.startswith(TRUST_POLICY + "\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n")
    assert content.endswith("\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1")
    envelope = json.loads(content.splitlines()[-2])
    assert envelope["trust"] == "untrusted_history"
    assert len(envelope["items"]) == len(result.fragments) == len(result.origins)
    for item, fragment, origin in zip(envelope["items"], result.fragments, result.origins, strict=True):
        group = next(group for group in groups if group.scope_id == fragment.address.memory.scope_id)
        hit = next(hit for hit in group.hits if hit.entry_id == fragment.address.entry_id)
        assert type(fragment.start) is int and type(fragment.end) is int
        assert 0 <= fragment.start < fragment.end <= len(hit.text)
        assert fragment.text == hit.text[fragment.start : fragment.end]
        assert origin == fragment.address
        assert item == {
            "citation": {
                "scope_id": group.scope_id,
                "memory_ref": hit.memory_ref.model_dump(mode="json"),
                "entry_id": hit.entry_id,
                "entry_version_id": hit.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        }


def test_multiple_nonadjacent_fragments_keep_distinct_scopes_and_exact_origins():
    text = "Cobalt sensor pressure is 37. " + "Administrative metadata " * 200 + ". Cobalt sensor speed is 19."
    groups = (_group("one", text), _group("two", text))
    result = _build(groups, budget=1800)
    _check(result, groups, 1800)
    assert len(result.fragments) == 4
    assert {fragment.address.memory.scope_id for fragment in result.fragments} == {"one", "two"}
    assert all("Administrative" not in fragment.text for fragment in result.fragments)
    assert _build(groups, budget=1800) == result


def test_query_changes_selection_within_one_candidate():
    text = (
        "Amber valve opens at 19 degrees. "
        + "Unrelated administrative notes. " * 100
        + "Cobalt sensor calibrates at 37 degrees."
    )
    groups = (_group("one", text),)
    amber = _build(groups, query="amber valve", budget=700)
    cobalt = _build(groups, query="cobalt sensor", budget=700)
    _check(amber, groups, 700)
    _check(cobalt, groups, 700)
    assert "19" in amber.fragments[0].text
    assert "37" in cobalt.fragments[0].text
    assert cobalt.fragments[0].start > amber.fragments[0].end


@pytest.mark.parametrize("budget", [512, 600, 700, 1000, 1400, 1800])
def test_unicode_json_escapes_and_entire_envelope_are_charged(budget):
    text = '行政信息😀。\n海洋观测 station records "temperature" and \\pressure. ' * 30
    groups = (_group("one", text),)
    result = _build(groups, query="海洋观测 station temperature", budget=budget)
    _check(result, groups, budget)


def test_no_complete_sentence_fits_but_query_window_is_literal_and_bounded():
    text = "unrelated " * 300 + "cobalt sensor " + "reading 温度😀 " * 300
    groups = (_group("one", text),)
    result = _build(groups, budget=850)
    _check(result, groups, 850)
    assert result.fragments
    assert "cobalt sensor" in result.fragments[0].text
    assert result.fragments[0].start > 0


def test_empty_budget_and_no_candidate_results_are_legal():
    groups = (_group("scope" * 30, "Cobalt sensor is ready.", entry="entry" * 50),)
    result = _build(groups, budget=512)
    _check(result, groups, 512)
    assert result.context.status == "empty"
    assert _build(()).context.status == "empty"


def test_duplicate_candidate_identity_is_not_emitted_twice():
    group = _group("one", "Cobalt sensor is ready.")
    result = _build((group, group), budget=1800)
    assert len(result.fragments) == 1


def test_evidence_selection_uses_json_even_with_other_assembly_options():
    groups = (_group("one", "Cobalt sensor is ready."),)
    assembly = ContextAssembly.model_validate({"sections": [{"family": "experience", "limit": 1}]})
    result = _build(groups, assembly=assembly, include_code=True)
    _check(result, groups, 1400)
    assert result.fragments


def test_budget_probe_matches_actual_evidence_selection():
    builder = PreparedContextBuilder()
    groups = (_group("one", "Cobalt sensor is ready."), _group("two", "Very long unrelated text " * 100))
    request = PrepareContextRequest(query="cobalt sensor", evidence_selection=True, max_bytes=700)
    result = builder.build_scopes_result(request=request, current_scope_id="one", memory_candidates=groups)
    probe = builder.probe_budget(request=request, current_scope_id="one", memory_candidates=groups)
    assert probe.delivered_items == len(result.origins)
    assert probe.unused_bytes == request.max_bytes - result.context.content_bytes
    assert probe.dropped_items == result.omissions.dropped_items


def test_evidence_selection_preserves_candidate_revision_validation():
    group = _group("one", "Cobalt sensor is ready.")
    invalid = PreparedMemoryCandidates(
        scope_id="one", memory_ref=_REF.model_copy(update={"revision": 8}), hits=group.hits
    )
    with pytest.raises(PreparedContextInvariantError, match="memory-ref-mismatch"):
        _build((invalid,))


def test_default_and_explicitly_disabled_prepare_are_identical():
    builder = PreparedContextBuilder()
    group = _group("one", "Cobalt sensor is ready.")
    ordinary = builder.build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor"), current_scope_id="one", memory_candidates=(group,)
    )
    disabled = builder.build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor", evidence_selection=False),
        current_scope_id="one",
        memory_candidates=(group,),
    )
    assert ordinary == disabled
    assert ordinary.fragments == ()
    assert PrepareContextRequest(query="test").evidence_selection is False
    for invalid in (1, 0, "true", None):
        with pytest.raises(ValidationError):
            PrepareContextRequest.model_validate({"query": "test", "evidence_selection": invalid})
