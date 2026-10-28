"""seekdb-backed hybrid retrieval for LabRelay chunks."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

import anyio
import pyseekdb
from pyseekdb import (
    EmbeddingFunction,
    FulltextIndexConfig,
    HNSWConfiguration,
    Schema,
    VectorIndexConfig,
)

from labrelay.config import LabRelayConfig
from labrelay.domain.models import Asset, Chunk, Citation
from labrelay.domain.ports import ModelGateway, SearchHit

COLLECTION_NAME = "labrelay_chunks"
ACTIVE_STATUS = "active"
MAX_EXCERPT_CHARACTERS = 700
MAX_CHUNKS_PER_ASSET = 3

logger = logging.getLogger("labrelay.retrieval.seekdb")


class SeekdbEmbeddingFunction(EmbeddingFunction):
    """Bridge :class:`ModelGateway` embeddings into pyseekdb collections.

    The model object is not serializable, so the function reports no persistent
    configuration and is rebound on each engine construction.
    """

    def __init__(self, model: ModelGateway, dimensions: int | None = None) -> None:
        self._model = model
        self._dimension = (
            dimensions if dimensions is not None else getattr(model, "dimension", None)
        )

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            raise ValueError("Embedding dimension must be configured")
        return int(self._dimension)

    def __call__(self, input: str | list[str]) -> list[list[float]]:
        texts = [input] if isinstance(input, str) else list(input)
        return self._model.embed(texts)

    @staticmethod
    def name() -> str:
        return "labrelay"

    def get_config(self) -> Any:
        # pyseekdb treats NotImplemented as "bind this function at runtime".
        return NotImplemented

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> SeekdbEmbeddingFunction:
        raise NotImplementedError("The LabRelay embedding function is bound at runtime")


def _collection_schema(embedding_function: SeekdbEmbeddingFunction) -> Schema:
    return Schema(
        vector_index=VectorIndexConfig(
            hnsw=HNSWConfiguration(
                dimension=embedding_function.dimension,
                distance="cosine",
            ),
            embedding_function=embedding_function,
        ),
        # The SDK's default analyzer is ``ik``; ``ngram`` keeps code identifiers and
        # mixed Chinese/English demo material searchable without a custom dictionary.
        fulltext_index=FulltextIndexConfig(analyzer="ngram"),
    )


class SeekdbSearchEngine:
    """Project-scoped hybrid search backed by a seekdb collection."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        collection_name: str,
        model: ModelGateway,
        dimensions: int | None = None,
    ) -> None:
        self._client = pyseekdb.Client(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
        )
        self._embedding_function = SeekdbEmbeddingFunction(model, dimensions)
        if self._client.has_collection(collection_name):
            self._collection = self._client.get_collection(
                collection_name,
                embedding_function=self._embedding_function,
            )
        else:
            self._collection = self._client.create_collection(
                name=collection_name,
                schema=_collection_schema(self._embedding_function),
            )

    @classmethod
    def from_config(cls, config: LabRelayConfig, model: ModelGateway) -> SeekdbSearchEngine:
        return cls(
            host=config.seekdb_host,
            port=config.seekdb_port,
            database=config.seekdb_database,
            user=config.seekdb_user,
            password=config.seekdb_password,
            collection_name=COLLECTION_NAME,
            model=model,
            dimensions=config.embedding.dimensions,
        )

    async def upsert_chunks(self, chunks: list[Chunk], assets: list[Asset]) -> None:
        await anyio.to_thread.run_sync(self._upsert_sync, chunks, assets)

    def _upsert_sync(self, chunks: list[Chunk], assets: list[Asset]) -> None:
        assets_by_id = {asset.id: asset for asset in assets}
        pending: dict[str, tuple[Chunk, Asset]] = {}
        for chunk in chunks:
            asset = assets_by_id.get(chunk.asset_id)
            if asset is None:
                logger.warning(
                    "chunk not indexed operation=upsert_chunks asset_id=%s reason=unknown_asset",
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
        active = list(pending.values())
        active_asset_ids = {asset.id for _, asset in active}

        # Evict assets that need no new embedding first: restricted/failed evidence must
        # disappear immediately, and an active file with no chunks has nothing to replace.
        for asset in assets_by_id.values():
            if asset.status != ACTIVE_STATUS or asset.id not in active_asset_ids:
                self._collection.delete(where={"asset_id": asset.id})

        if not active:
            return

        vectors = self._embedding_function([chunk.content for chunk, _ in active])

        # Only replace an active asset's old chunks after its embeddings exist, so an
        # embedding outage cannot erase evidence that was searchable a moment ago.
        for asset_id in active_asset_ids:
            self._collection.delete(where={"asset_id": asset_id})

        self._collection.upsert(
            ids=[chunk.id for chunk, _ in active],
            embeddings=vectors,
            documents=[chunk.content for chunk, _ in active],
            metadatas=[
                {
                    "project_id": asset.project_id,
                    "asset_id": asset.id,
                    "kind": asset.kind,
                    "title": asset.title,
                    "uri": asset.uri,
                    "version": asset.version or "",
                    "status": asset.status,
                    "locator_json": json.dumps(chunk.locator, ensure_ascii=False),
                    "excerpt": chunk.content[:MAX_EXCERPT_CHARACTERS],
                }
                for chunk, asset in active
            ],
        )

    async def search(
        self,
        project_id: str,
        query: str,
        asset_kinds: list[str],
        limit: int,
    ) -> list[SearchHit]:
        if limit <= 0 or not query.strip():
            return []
        return await anyio.to_thread.run_sync(
            self._search_sync, project_id, query, asset_kinds, limit
        )

    def _search_sync(
        self,
        project_id: str,
        query: str,
        asset_kinds: list[str],
        limit: int,
    ) -> list[SearchHit]:
        filters: dict[str, Any] = {
            "project_id": project_id,
            "status": ACTIVE_STATUS,
        }
        if asset_kinds:
            filters["kind"] = {"$in": asset_kinds}
        candidate_limit = max(limit, min(50, limit * 4))
        result = self._collection.hybrid_search(
            query={
                "where": filters,
                "where_document": {"$contains": query},
                "n_results": candidate_limit,
            },
            knn={
                "query_texts": [query],
                "where": filters,
                "n_results": candidate_limit,
            },
            rank={"rrf": {}},
            n_results=candidate_limit,
            include=["documents", "metadatas", "distances"],
        )
        return _diversify(self._convert(result))[:limit]

    @staticmethod
    def _convert(result: dict[str, Any]) -> list[SearchHit]:
        ids = (result.get("ids") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits: list[SearchHit] = []

        for index, _chunk_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) else {}
            asset_id = metadata.get("asset_id")
            kind = metadata.get("kind")
            if not asset_id or not kind:
                logger.warning(
                    "search hit skipped operation=convert asset_id=%s reason=missing_metadata",
                    asset_id or "-",
                )
                continue
            document = (documents[index] if index < len(documents) else "") or metadata.get(
                "excerpt", ""
            )
            relevance = distances[index] if index < len(distances) else 0.0
            hits.append(
                SearchHit(
                    citation=Citation(
                        asset_id=asset_id,
                        kind=kind,
                        title=metadata.get("title", ""),
                        uri=metadata.get("uri", ""),
                        version=metadata.get("version") or None,
                        locator=_parse_locator(metadata.get("locator_json")),
                        excerpt=document[:MAX_EXCERPT_CHARACTERS],
                    ),
                    # Under ``rank={"rrf": {}}`` pyseekdb returns the fused relevance in
                    # ``distances``; it is higher-is-better, not a distance.
                    score=float(relevance or 0.0),
                )
            )
        return hits


def _parse_locator(raw: Any) -> dict[str, str | int]:
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    else:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        key: value
        for key, value in parsed.items()
        if isinstance(key, str) and isinstance(value, (str, int))
    }


def _diversify(hits: list[SearchHit]) -> list[SearchHit]:
    counts: defaultdict[str, int] = defaultdict(int)
    kept: list[SearchHit] = []
    for hit in hits:
        asset_id = hit.citation.asset_id
        if counts[asset_id] >= MAX_CHUNKS_PER_ASSET:
            continue
        counts[asset_id] += 1
        kept.append(hit)
    return kept
