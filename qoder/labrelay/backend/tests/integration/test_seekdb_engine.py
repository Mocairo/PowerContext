import asyncio
import os

import pytest

from labrelay.config import LabRelayConfig
from labrelay.domain.models import Asset, Chunk
from labrelay.persistence.seekdb import SeekdbRepository
from labrelay.retrieval.seekdb import SeekdbSearchEngine

pytestmark = pytest.mark.skipif(
    not os.getenv("LABRELAY_SEEKDB_TEST"), reason="set LABRELAY_SEEKDB_TEST=1 with a live seekdb"
)


class FakeEmbedding:
    dimension = 2

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _config() -> LabRelayConfig:
    return LabRelayConfig(
        seekdb_host=os.getenv("SEEKDB_HOST", "127.0.0.1"),
        seekdb_port=int(os.getenv("SEEKDB_PORT", "2881")),
        seekdb_database=os.getenv("SEEKDB_DATABASE", "labrelay"),
        seekdb_user=os.getenv("SEEKDB_USER", "root"),
        seekdb_password=os.getenv("SEEKDB_ROOT_PASSWORD", ""),
        seekdb_tenant=os.getenv("SEEKDB_TENANT", "sys"),
    )


def _chunk(chunk_id: str, content: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        asset_id="asset-e2e",
        ordinal=0,
        content=content,
        locator={"line_start": 1, "line_end": 3},
        token_count=5,
    )


def test_upsert_and_hybrid_search() -> None:
    model = FakeEmbedding()
    config = _config()
    asyncio.run(SeekdbRepository.from_config(config).initialize())
    engine = SeekdbSearchEngine.from_config(config, model)
    asset = Asset(
        id="asset-e2e",
        project_id="project-e2e",
        kind="code",
        title="train.py",
        uri="/tmp/train.py",
        checksum="abc",
        status="active",
    )

    async def run():
        assert await engine.health_check() is True
        await engine.initialize()
        await engine.upsert_chunks([_chunk("asset-e2e:1", "run baseline experiment")], [asset])
        # a re-import that drops one chunk must keep the surviving one and lose the stale one
        await engine.upsert_chunks(
            [
                _chunk("asset-e2e:1", "run the baseline experiment again"),
                _chunk("asset-e2e:2", "sweep the learning rate"),
            ],
            [asset],
        )
        await engine.upsert_chunks([_chunk("asset-e2e:2", "sweep the learning rate")], [asset])
        hits = await engine.search("project-e2e", "baseline", ["code"], 5)
        other_project = await engine.search("project-somebody-else", "baseline", ["code"], 5)
        stale = await engine.search("project-e2e", "experiment", ["code"], 5)
        await engine.close()
        return hits, other_project, stale

    hits, other_project, stale = asyncio.run(run())

    assert hits
    assert {hit.citation.asset_id for hit in hits} == {"asset-e2e"}
    assert hits[0].citation.title == "train.py"
    assert hits[0].score >= hits[-1].score > 0
    # the project predicate holds even when a caller asks for another project's evidence
    assert other_project == []
    # the chunk the last upsert dropped is no longer retrievable evidence
    assert all(hit.citation.excerpt != "run baseline experiment" for hit in stale)
