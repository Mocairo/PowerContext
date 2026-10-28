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
    chunk = Chunk(
        id="asset-e2e:1",
        asset_id=asset.id,
        ordinal=0,
        content="run baseline experiment",
        locator={"line_start": 1, "line_end": 3},
        token_count=5,
    )

    async def run():
        await engine.upsert_chunks([chunk], [asset])
        return await engine.search("project-e2e", "baseline", ["code"], 5)

    hits = asyncio.run(run())
    assert hits[0].citation.asset_id == "asset-e2e"
