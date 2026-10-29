"""Hybrid in-memory retrieval: fusion scoring, dedup, active-only and citations."""

import asyncio
import math
from pathlib import Path

import pytest
import yaml

from labrelay.domain.models import Asset, Chunk
from labrelay.domain.ports import SearchHit
from labrelay.retrieval import in_memory
from labrelay.retrieval.in_memory import InMemorySearchEngine

RETRIEVAL_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "retrieval.yaml"

TRAIN = Asset(
    id="asset-train",
    project_id="project-1",
    kind="code",
    title="train.py",
    uri="repo/train.py",
    checksum="x",
    status="active",
    version="commit-sha",
)
BASELINE_CHUNK = Chunk(
    id="asset-train:lines:1-2",
    asset_id=TRAIN.id,
    ordinal=0,
    content="def run_baseline():",
    locator={"line_start": 1, "line_end": 2},
    token_count=6,
)


class BaselineModel:
    """The plan's embedding stand-in: 1.0 for any text that mentions a baseline."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0 if "baseline" in text else 0.0] for text in texts]


class ScriptedModel:
    """A fixed vector per exact text; anything else embeds to the zero vector.

    Keying the vector on text the lexical channel ignores lets a test drive the two
    channels independently instead of both reading the same token.
    """

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self._vectors = vectors or {}
        self._dimension = max((len(v) for v in self._vectors.values()), default=1)
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [list(self._vectors.get(text, [0.0] * self._dimension)) for text in texts]


def make_data():
    """The asset/chunk pair the plan's retrieval tests are written against."""
    return TRAIN, [BASELINE_CHUNK]


def asset_chunk(*, asset: Asset, chunk_id: str, content: str, ordinal: int = 0) -> Chunk:
    return Chunk(
        id=chunk_id,
        asset_id=asset.id,
        ordinal=ordinal,
        content=content,
        locator={"line_start": ordinal + 1, "line_end": ordinal + 2},
        token_count=max(1, len(content) // 4),
    )


def run(coroutine):
    return asyncio.run(coroutine)


def search(engine: InMemorySearchEngine, project_id: str, query: str, kinds=(), limit: int = 5):
    async def go():
        return await engine.search(project_id, query, list(kinds), limit)

    return run(go())


# --- the plan's two retrieval tests -------------------------------------------


def test_search_returns_asset_metadata_and_citation() -> None:
    asset, chunks = make_data()
    engine = InMemorySearchEngine(model=BaselineModel())

    async def go():
        await engine.upsert_chunks(chunks, [asset])
        return await engine.search("project-1", "baseline", ["code"], 5)

    hits = run(go())
    assert hits[0].citation.asset_id == "asset-train"
    assert hits[0].citation.locator == {"line_start": 1, "line_end": 2}
    # FR-RET-002 wants the asset type, title and excerpt on every hit; FR-RET-004 and
    # the citation validator need a uri and a version to locate and open the source.
    assert hits[0].citation.kind == "code"
    assert hits[0].citation.title == "train.py"
    assert hits[0].citation.uri == "repo/train.py"
    assert hits[0].citation.version == "commit-sha"
    assert hits[0].citation.excerpt == "def run_baseline():"
    assert hits[0].score == pytest.approx(1.0)


def test_search_limits_each_asset() -> None:
    asset, chunks = make_data()
    duplicate = chunks[0].model_copy(
        update={"id": "asset-train:lines:3-4", "content": "run_baseline twice"}
    )
    engine = InMemorySearchEngine(model=BaselineModel())

    async def go():
        await engine.upsert_chunks([chunks[0], duplicate], [asset])
        return await engine.search("project-1", "baseline", [], 5)

    hits = run(go())
    assert len([hit for hit in hits if hit.citation.asset_id == "asset-train"]) <= 3


# --- fusion and deterministic scoring ----------------------------------------


def test_search_fuses_lexical_and_vector_scores() -> None:
    """A vector-only hit outranks a lexical-only one, at the configured weights."""
    notes = TRAIN.model_copy(update={"id": "asset-notes", "title": "notes.md", "kind": "note"})
    lexical_only = asset_chunk(asset=TRAIN, chunk_id="asset-train:lex", content="def run_train():")
    vector_only = asset_chunk(asset=notes, chunk_id="asset-notes:vec", content="gpu cluster setup")
    engine = InMemorySearchEngine(
        # "def run_train():" is left unmapped, so it scores on lexical overlap alone.
        model=ScriptedModel({"train": [1.0, 0.0], "gpu cluster setup": [1.0, 0.0]})
    )

    async def go():
        await engine.upsert_chunks([lexical_only, vector_only], [TRAIN, notes])
        return await engine.search("project-1", "train", [], 5)

    hits = run(go())
    assert [hit.citation.asset_id for hit in hits] == ["asset-notes", "asset-train"]
    assert hits[0].score == pytest.approx(0.55)
    assert hits[1].score == pytest.approx(0.45)


def test_vector_channel_is_normalised_cosine_not_dot_product() -> None:
    chunk = asset_chunk(asset=TRAIN, chunk_id="asset-train:cos", content="def fit():")
    model = ScriptedModel({"def fit():": [2.0, 0.0], "sweep": [1.0, 1.0]})
    engine = InMemorySearchEngine(model=model)

    async def go():
        await engine.upsert_chunks([chunk], [TRAIN])
        return await engine.search("project-1", "sweep", [], 5)

    hits = run(go())
    # cos([2,0],[1,1]) == 1/sqrt(2): the score must not grow with vector magnitude.
    assert hits[0].score == pytest.approx(0.55 / math.sqrt(2))


def test_zero_norm_vectors_contribute_nothing() -> None:
    chunk = asset_chunk(asset=TRAIN, chunk_id="asset-train:zero", content="cluster notes")
    engine = InMemorySearchEngine(model=ScriptedModel({"sweep": [1.0, 1.0]}))

    async def go():
        await engine.upsert_chunks([chunk], [TRAIN])
        return await engine.search("project-1", "sweep", [], 5)

    # No lexical overlap and a zero document vector: the hit must not appear at all.
    assert run(go()) == []


def test_scoring_is_deterministic_for_equal_scores() -> None:
    """Ties resolve on chunk identity, never on the order the chunks arrived."""
    asset, _ = make_data()
    chunks = [
        asset_chunk(asset=asset, chunk_id=f"asset-train:c{i}", content=f"run_baseline {i}")
        for i in range(1, 6)
    ]
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def index_in_reverse():
        await engine.upsert_chunks(list(reversed(chunks)), [asset])
        return await engine.search("project-1", "baseline", [], 5)

    async def index_in_order():
        await engine.upsert_chunks(chunks, [asset])
        return await engine.search("project-1", "baseline", [], 5)

    first = run(index_in_reverse())
    assert [hit.citation.excerpt for hit in first] == [f"run_baseline {i}" for i in (1, 2, 3)]
    assert len(first) == 3  # the per-asset cap bites, and it keeps the same three
    assert run(index_in_order()) == first


def test_lexical_matching_splits_code_identifiers() -> None:
    chunk = asset_chunk(asset=TRAIN, chunk_id="asset-train:tok", content="run_baseline()")
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([chunk], [TRAIN])
        return await engine.search("project-1", "baseline", [], 5)

    assert run(go())[0].score == pytest.approx(0.45)


# --- status filtering ---------------------------------------------------------


@pytest.mark.parametrize("status", ["restricted", "failed"])
def test_upsert_does_not_index_chunks_of_a_non_active_asset(status: str) -> None:
    asset = TRAIN.model_copy(update={"status": status})
    chunk = asset_chunk(asset=asset, chunk_id="asset-train:secret", content="api_key leaked")
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([chunk], [asset])
        return await engine.search("project-1", "api_key", [], 5)

    assert run(go()) == []


def test_an_asset_restricted_by_a_later_import_is_evicted() -> None:
    """Clean on the first import, credential-bearing on the second: no stale hits.

    The importer persists the restricted asset without re-chunking it, so the asset
    record that travels with the next upsert for that project is what tells the index
    to drop the chunks it wrote while the asset still looked active.
    """
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([BASELINE_CHUNK], [TRAIN])
        restricted = TRAIN.model_copy(update={"status": "restricted"})
        await engine.upsert_chunks([], [restricted])
        return await engine.search("project-1", "baseline", [], 5)

    assert run(go()) == []


def test_reindexing_a_restricted_asset_does_not_resurrect_its_chunks() -> None:
    engine = InMemorySearchEngine(model=ScriptedModel())
    fresh = asset_chunk(
        asset=TRAIN, chunk_id="asset-train:lines:3-4", content="def run_baseline_v2():"
    )

    async def go():
        await engine.upsert_chunks([BASELINE_CHUNK], [TRAIN])
        restricted = TRAIN.model_copy(update={"status": "restricted"})
        await engine.upsert_chunks([fresh], [restricted])
        return await engine.search("project-1", "baseline", [], 5)

    assert run(go()) == []


def test_upsert_does_not_index_chunks_without_an_asset_record() -> None:
    """Fail closed: a chunk that resolves to no asset is never searchable."""
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([BASELINE_CHUNK], [])
        return await engine.search("project-1", "baseline", [], 5)

    assert run(go()) == []


# --- indexing replaces stale chunks ------------------------------------------


def test_upsert_replaces_the_chunks_already_indexed_for_that_asset() -> None:
    """A re-import that shrinks the file must not leave the old chunks behind."""
    first = asset_chunk(asset=TRAIN, chunk_id="asset-train:lines:1-2", content="def run():")
    second = asset_chunk(
        asset=TRAIN, chunk_id="asset-train:lines:3-4", content="def run_baseline(): return 1"
    )
    shrunk = asset_chunk(asset=TRAIN, chunk_id="asset-train:lines:1-1", content="def run(): pass")
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([first, second], [TRAIN])
        await engine.upsert_chunks([shrunk], [TRAIN])
        return await engine.search("project-1", "run", [], 5)

    hits = run(go())
    assert [hit.citation.excerpt for hit in hits] == [shrunk.content]
    assert [hit.citation.locator for hit in hits] == [shrunk.locator]


def test_upsert_of_the_same_chunk_id_replaces_it_in_place() -> None:
    original = asset_chunk(asset=TRAIN, chunk_id="asset-train:1", content="def run_baseline():")
    edited = asset_chunk(asset=TRAIN, chunk_id="asset-train:1", content="def run_experiment():")
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([original], [TRAIN])
        await engine.upsert_chunks([edited], [TRAIN])
        baseline = await engine.search("project-1", "baseline", [], 5)
        experiment = await engine.search("project-1", "experiment", [], 5)
        return baseline, experiment

    baseline, experiment = run(go())
    assert baseline == []
    assert len(experiment) == 1


def test_upsert_deduplicates_repeated_chunk_ids_within_one_call() -> None:
    stale = asset_chunk(asset=TRAIN, chunk_id="asset-train:1", content="baseline draft text")
    current = asset_chunk(asset=TRAIN, chunk_id="asset-train:1", content="baseline final text")
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([stale, current], [TRAIN])
        final = await engine.search("project-1", "final", [], 5)
        draft = await engine.search("project-1", "draft", [], 5)
        return final, draft

    final, draft = run(go())
    assert len(final) == 1
    assert final[0].citation.excerpt == "baseline final text"
    assert draft == []


def test_upsert_embeds_each_chunk_once() -> None:
    model = ScriptedModel()
    engine = InMemorySearchEngine(model=model)
    other = asset_chunk(asset=TRAIN, chunk_id="asset-train:2", content="def evaluate():")

    run(engine.upsert_chunks([BASELINE_CHUNK, other], [TRAIN]))
    assert model.calls == [[BASELINE_CHUNK.content, other.content]]


# --- filtering, limit and empty results --------------------------------------


def test_search_filters_by_asset_kind() -> None:
    paper = TRAIN.model_copy(update={"id": "asset-paper", "kind": "paper", "title": "paper.pdf"})
    train_chunk = asset_chunk(asset=TRAIN, chunk_id="asset-train:1", content="run baseline")
    paper_chunk = asset_chunk(asset=paper, chunk_id="asset-paper:1", content="baseline results")
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([train_chunk, paper_chunk], [TRAIN, paper])
        code = await engine.search("project-1", "baseline", ["code"], 5)
        papers = await engine.search("project-1", "baseline", ["paper"], 5)
        everything = await engine.search("project-1", "baseline", [], 5)
        return code, papers, everything

    code, papers, everything = run(go())
    assert [hit.citation.asset_id for hit in code] == ["asset-train"]
    assert [hit.citation.asset_id for hit in papers] == ["asset-paper"]
    assert len(everything) == 2


def test_search_is_scoped_to_the_requested_project() -> None:
    other = TRAIN.model_copy(update={"id": "asset-other", "project_id": "project-2"})
    chunk = asset_chunk(asset=other, chunk_id="asset-other:1", content="run baseline")
    engine = InMemorySearchEngine(model=ScriptedModel())

    async def go():
        await engine.upsert_chunks([BASELINE_CHUNK, chunk], [TRAIN, other])
        return await engine.search("project-2", "baseline", [], 5)

    hits = run(go())
    assert [hit.citation.asset_id for hit in hits] == ["asset-other"]


def test_search_honours_the_limit_and_keeps_the_best_hits() -> None:
    notes = TRAIN.model_copy(update={"id": "asset-notes", "title": "notes.md", "kind": "note"})
    log = TRAIN.model_copy(update={"id": "asset-log", "title": "runs.md", "kind": "note"})
    chunks = [
        asset_chunk(asset=TRAIN, chunk_id="asset-train:1", content="run_baseline sweep"),
        asset_chunk(asset=notes, chunk_id="asset-notes:1", content="baseline notes"),
        asset_chunk(asset=log, chunk_id="asset-log:1", content="sweep results"),
    ]
    engine = InMemorySearchEngine(model=ScriptedModel())
    run(engine.upsert_chunks(chunks, [TRAIN, notes, log]))

    # Query "baseline sweep" matches train fully, notes and log on one token each.
    best = search(engine, "project-1", "baseline sweep", limit=1)
    assert [hit.citation.asset_id for hit in best] == ["asset-train"]
    ranked = search(engine, "project-1", "baseline sweep", limit=3)
    assert [hit.citation.asset_id for hit in ranked] == ["asset-train", "asset-log", "asset-notes"]
    assert [hit.score for hit in ranked] == pytest.approx([0.45, 0.225, 0.225])
    assert len(search(engine, "project-1", "baseline sweep", limit=2)) == 2


def test_search_preserves_asset_diversity() -> None:
    """One asset cannot take the whole result set while another asset still has a hit."""
    notes = TRAIN.model_copy(update={"id": "asset-notes", "title": "notes.md", "kind": "note"})
    chunks = [
        asset_chunk(asset=TRAIN, chunk_id=f"asset-train:c{i}", content=f"run_baseline {i}")
        for i in range(1, 6)
    ] + [asset_chunk(asset=notes, chunk_id="asset-notes:c1", content="baseline note")]
    engine = InMemorySearchEngine(model=ScriptedModel())
    run(engine.upsert_chunks(chunks, [TRAIN, notes]))

    hits = search(engine, "project-1", "baseline", limit=3)
    assert [(hit.citation.asset_id, hit.citation.excerpt) for hit in hits] == [
        ("asset-notes", "baseline note"),
        ("asset-train", "run_baseline 1"),
        ("asset-train", "run_baseline 2"),
    ]


def test_search_returns_everything_when_the_limit_is_not_the_binding_constraint() -> None:
    chunks = [
        asset_chunk(asset=TRAIN, chunk_id=f"asset-train:{i}", content=f"baseline {i}")
        for i in range(3)
    ]
    engine = InMemorySearchEngine(model=ScriptedModel())

    run(engine.upsert_chunks(chunks, [TRAIN]))
    assert len(search(engine, "project-1", "baseline")) == 3
    assert search(engine, "project-1", "missing") == []


def test_search_returns_no_hits_for_a_project_that_was_never_imported() -> None:
    model = ScriptedModel()
    engine = InMemorySearchEngine(model=model)

    assert search(engine, "project-empty", "baseline") == []
    # FR-RET-006 plus the "embedding unavailable" degradation row: an empty project
    # must answer empty without paying for an embedding call.
    assert model.calls == []


def test_search_returns_no_hits_for_an_empty_query_or_limit() -> None:
    engine = InMemorySearchEngine(model=BaselineModel())
    run(engine.upsert_chunks([BASELINE_CHUNK], [TRAIN]))

    assert search(engine, "project-1", "") == []
    assert search(engine, "project-1", "baseline", limit=0) == []


def test_search_hits_are_search_hit_models() -> None:
    engine = InMemorySearchEngine(model=BaselineModel())
    run(engine.upsert_chunks([BASELINE_CHUNK], [TRAIN]))

    hits = search(engine, "project-1", "baseline")

    assert all(isinstance(hit, SearchHit) for hit in hits)
    assert hits[0].model_dump()["citation"]["asset_id"] == "asset-train"


def test_excerpt_is_capped_at_the_configured_length() -> None:
    long_content = "baseline " + "x" * 900
    chunk = asset_chunk(asset=TRAIN, chunk_id="asset-train:long", content=long_content)
    engine = InMemorySearchEngine(model=ScriptedModel())

    run(engine.upsert_chunks([chunk], [TRAIN]))
    hits = search(engine, "project-1", "baseline")

    excerpt = hits[0].citation.excerpt
    assert len(excerpt) == in_memory.MAX_EXCERPT_CHARACTERS
    assert excerpt.startswith("baseline ")


def test_chinese_query_matches_cjk_bigrams_lexically() -> None:
    """A Chinese query has to rank on the lexical channel, not on the hash embedding alone."""
    note = TRAIN.model_copy(update={"id": "asset-note", "kind": "note", "title": "debugging.md"})
    config = TRAIN.model_copy(
        update={"id": "asset-config", "kind": "config", "title": "baseline.yaml"}
    )
    note_chunk = asset_chunk(
        asset=note, chunk_id="asset-note:cjk", content="旧数据缓存会污染数据划分"
    )
    config_chunk = asset_chunk(asset=config, chunk_id="asset-config:cjk", content="学习率消融配置")
    engine = InMemorySearchEngine(model=ScriptedModel())

    run(engine.upsert_chunks([note_chunk, config_chunk], [note, config]))
    hits = search(engine, "project-1", "数据缓存")

    assert hits[0].citation.title == "debugging.md"
    assert hits[0].score > 0


# --- configuration reconciliation --------------------------------------------


def test_retrieval_constants_match_retrieval_yaml() -> None:
    """The engine scores with the fusion settings the shipped config declares."""
    config = yaml.safe_load(RETRIEVAL_CONFIG.read_text(encoding="utf-8"))["retrieval"]
    fusion = config["fusion"]

    assert in_memory.FULL_TEXT_WEIGHT == pytest.approx(fusion["full_text_weight"])
    assert in_memory.VECTOR_WEIGHT == pytest.approx(fusion["vector_weight"])
    assert in_memory.FULL_TEXT_WEIGHT + in_memory.VECTOR_WEIGHT == pytest.approx(1.0)
    assert in_memory.MIN_SCORE == config["full_text"]["min_score"] == config["vector"]["min_score"]
    assert in_memory.MAX_CHUNKS_PER_ASSET == config["grouping"]["max_chunks_per_asset"]
    assert config["grouping"]["preserve_asset_diversity"] is True
    assert in_memory.MAX_EXCERPT_CHARACTERS == config["citation"]["max_excerpt_characters"]
    assert config["citation"]["validate_before_return"] is True
    assert config["filters"]["required_scope"] == "project"
