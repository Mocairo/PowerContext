"""In-memory hybrid retrieval: lexical overlap fused with cosine vector similarity.

The engine is the ``SearchEngine`` port of :mod:`labrelay.domain.ports` and is the MVP
stand-in for the seekdb engine (Task 7). It owns no persistence: re-importing a project
re-answers the same asset and chunk ids, so the index converges on the last import
instead of growing.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass

from labrelay.domain.models import Asset, Chunk, Citation
from labrelay.domain.ports import ModelGateway, SearchHit

# Mirrors the ``retrieval`` section of configs/retrieval.yaml, so the in-memory engine
# and the seekdb engine rank the same evidence the same way;
# ``tests/unit/test_search.py::test_retrieval_constants_match_retrieval_yaml`` keeps
# both sides honest instead of injecting an unused config layer.
FULL_TEXT_WEIGHT = 0.45
VECTOR_WEIGHT = 0.55
MIN_SCORE = 0.0
MAX_CHUNKS_PER_ASSET = 3
MAX_EXCERPT_CHARACTERS = 700
# Only ``active`` evidence is searchable. An asset that was clean on one import and
# restricted on a later one is dropped from the index the moment its new status arrives,
# and a chunk whose asset is not active never enters it.
ACTIVE_STATUS = "active"

# Identifiers, paths and headings all reach the lexical channel, so ``run_baseline()``
# matches the query term ``baseline`` rather than the single token ``run_baseline():``.
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

logger = logging.getLogger("labrelay.retrieval.in_memory")


@dataclass(slots=True)
class IndexedChunk:
    chunk: Chunk
    asset: Asset
    vector: list[float]


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(text.lower()))


def _cosine(left: list[float], right: list[float]) -> float:
    # Ragged vectors are tolerated rather than fatal: a query embedded by a different
    # model than the indexed chunks still scores on the dimensions they share.
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _citation(item: IndexedChunk) -> Citation:
    """Resolve an indexed chunk into the citation the UI opens the source with."""
    return Citation(
        asset_id=item.asset.id,
        kind=item.asset.kind,
        title=item.asset.title,
        uri=item.asset.uri,
        version=item.asset.version,
        locator=item.chunk.locator,
        excerpt=item.chunk.content[:MAX_EXCERPT_CHARACTERS],
    )


def _diversify(
    scored: list[tuple[float, IndexedChunk]], limit: int
) -> list[tuple[float, IndexedChunk]]:
    """Cap each asset at ``MAX_CHUNKS_PER_ASSET`` and keep other assets in the result.

    ``scored`` is already in global score order, so taking the best chunk of every asset
    before any asset's second one is what ``grouping.preserve_asset_diversity`` asks for:
    one long file cannot push a shorter match out of a small result set. The selection is
    re-sorted afterwards so the caller still receives hits ranked by relevance.
    """
    per_asset: dict[str, list[tuple[float, IndexedChunk]]] = defaultdict(list)
    for entry in scored:
        per_asset[entry[1].asset.id].append(entry)

    selected: list[tuple[float, IndexedChunk]] = []
    for rank in range(MAX_CHUNKS_PER_ASSET):
        for entries in per_asset.values():
            if rank < len(entries):
                selected.append(entries[rank])
                if len(selected) >= limit:
                    break
        if len(selected) >= limit:
            break

    selected.sort(key=lambda entry: (-entry[0], entry[1].asset.id, entry[1].chunk.id))
    return selected[:limit]


class InMemorySearchEngine:
    """Project-scoped hybrid search over the chunks handed to :meth:`upsert_chunks`."""

    def __init__(self, model: ModelGateway) -> None:
        self._model = model
        self._documents: dict[str, list[IndexedChunk]] = {}

    async def upsert_chunks(self, chunks: list[Chunk], assets: list[Asset]) -> None:
        """Index ``chunks`` and replace what an earlier import left behind.

        Asset state travels with every call, so a chunk never gets indexed against a
        stale or missing :class:`Asset`, and every asset id in this call loses the chunks
        already indexed for it before the new ones are added — a file that shrinks on
        re-import drops the chunks it no longer produces instead of keeping retrievable
        evidence from an older version of itself.
        """
        assets_by_id = {asset.id: asset for asset in assets}
        for project_id, state_assets in _by_project(assets).items():
            self._adopt_asset_state(project_id, state_assets)

        # Last write wins when one call repeats a chunk id.
        pending: dict[str, tuple[Chunk, Asset]] = {}
        for chunk in chunks:
            asset = assets_by_id.get(chunk.asset_id)
            if asset is None:
                # The project is unknown here, which is exactly why the chunk cannot be
                # indexed: without its asset there is no status to trust.
                logger.warning(
                    "chunk not indexed operation=upsert_chunks project_id=- asset_id=%s "
                    "reason=unknown_asset",
                    chunk.asset_id,
                )
                continue
            if asset.status != ACTIVE_STATUS:
                logger.info(
                    "chunk not indexed operation=upsert_chunks project_id=%s asset_id=%s "
                    "reason=asset_not_active",
                    asset.project_id,
                    asset.id,
                )
                continue
            pending[chunk.id] = (chunk, asset)

        if not pending:
            return

        ordered = list(pending.values())
        vectors = self._model.embed([chunk.content for chunk, _ in ordered])
        additions: dict[str, list[IndexedChunk]] = defaultdict(list)
        for (chunk, asset), vector in zip(ordered, vectors, strict=True):
            additions[asset.project_id].append(
                IndexedChunk(chunk=chunk, asset=asset, vector=list(vector))
            )

        replaced_asset_ids = {asset.id for _, asset in ordered}
        for project_id, items in additions.items():
            kept = [
                indexed
                for indexed in self._documents.get(project_id, [])
                if indexed.asset.id not in replaced_asset_ids
            ]
            kept.extend(items)
            self._documents[project_id] = kept

    def _adopt_asset_state(self, project_id: str, assets: list[Asset]) -> None:
        """Refresh the asset snapshot behind the chunks already indexed for a project.

        This is the security reconciliation the in-memory index can express through the
        existing port: the importer persists a newly restricted asset without re-sending
        its chunks, so the asset record alone has to evict the evidence written while it
        still looked active.
        """
        documents = self._documents.get(project_id)
        if not documents:
            return
        freshest = {asset.id: asset for asset in assets}
        kept: list[IndexedChunk] = []
        evicted: set[str] = set()
        for item in documents:
            replacement = freshest.get(item.asset.id)
            if replacement is None:
                kept.append(item)
            elif replacement.status != ACTIVE_STATUS:
                evicted.add(item.asset.id)
            else:
                kept.append(IndexedChunk(chunk=item.chunk, asset=replacement, vector=item.vector))
        self._documents[project_id] = kept
        for asset_id in sorted(evicted):
            logger.info(
                "chunks evicted operation=upsert_chunks project_id=%s asset_id=%s "
                "reason=asset_not_active",
                project_id,
                asset_id,
            )

    async def search(
        self,
        project_id: str,
        query: str,
        asset_kinds: list[str],
        limit: int,
    ) -> list[SearchHit]:
        """Rank this project's active evidence, best citations first."""
        if limit <= 0:
            return []
        allowed_kinds = set(asset_kinds)
        candidates = [
            item
            for item in self._documents.get(project_id, [])
            if item.asset.status == ACTIVE_STATUS
            and (not allowed_kinds or item.asset.kind in allowed_kinds)
        ]
        if not candidates:
            # Nothing to score, so a project with no evidence never pays for an
            # embedding call and never invents a citation (FR-RET-006).
            return []

        query_tokens = _tokens(query)
        query_vector = self._model.embed([query])[0]
        scored: list[tuple[float, IndexedChunk]] = []
        for item in candidates:
            document_tokens = _tokens(item.chunk.content)
            lexical = (
                len(query_tokens & document_tokens) / len(query_tokens) if query_tokens else 0.0
            )
            vector = _cosine(query_vector, item.vector)
            score = FULL_TEXT_WEIGHT * lexical + VECTOR_WEIGHT * vector
            if score > MIN_SCORE:
                scored.append((score, item))

        # Score first, then asset and chunk identity, so equal scores resolve the same
        # way on every run rather than by the order the chunks happened to arrive.
        scored.sort(key=lambda entry: (-entry[0], entry[1].asset.id, entry[1].chunk.id))
        return [
            SearchHit(citation=_citation(item), score=score)
            for score, item in _diversify(scored, limit)
        ]


def _by_project(assets: list[Asset]) -> dict[str, list[Asset]]:
    grouped: dict[str, list[Asset]] = defaultdict(list)
    for asset in assets:
        grouped[asset.project_id].append(asset)
    return grouped
