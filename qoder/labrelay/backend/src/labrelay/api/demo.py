"""Local-only demo endpoints for the LabRelay showcase's live panel.

Three calls, one per thing the panel cannot fake on its own: which dependency set is actually
running, a project bootstrapped from the shipped corpus with a drafted Handoff, and a probe that
proves the *configured* model gateway answers JSON. Everything else the panel shows is an ordinary
project endpoint it calls itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from labrelay.api.deps import get_handoff_service, get_service
from labrelay.api.errors import DEPENDENCY_UNAVAILABLE, ApiError
from labrelay.application.handoff import HandoffDraft, HandoffService, HandoffStateItem
from labrelay.application.services import LabRelayService
from labrelay.domain.models import Asset, Citation

#: ``backend/src/labrelay/api/demo.py`` -> labrelay, which holds ``examples/``. Resolved from the
#: package rather than the process cwd, so the panel bootstraps the same corpus no matter where the
#: backend was started — the React workbench's relative default path carries that assumption.
DEMO_PROJECT_ROOT = Path(__file__).resolve().parents[4] / "examples" / "demo-project"

MAX_MODEL_PROMPT_CHARS = 2000

ServiceDep = Annotated[LabRelayService, Depends(get_service)]
HandoffDep = Annotated[HandoffService, Depends(get_handoff_service)]


class ModelCheckRequest(BaseModel):
    prompt: str | None = Field(default=None, max_length=MAX_MODEL_PROMPT_CHARS)


def build_demo_router() -> APIRouter:
    router = APIRouter(prefix="/api/demo")

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        descriptor: dict[str, str] = getattr(request.app.state, "demo", {})
        return {**descriptor, "demo_project_root": str(DEMO_PROJECT_ROOT)}

    @router.post("/bootstrap")
    async def bootstrap(service: ServiceDep, handoff: HandoffDep) -> dict[str, Any]:
        project = await service.create_project(
            "Aurora 时序异常检测",
            "深度学习 / 时序异常检测",
            "合成科研交接示例：baseline 复现、学习率消融与数据缓存问题。",
        )
        imported = dict(await service.import_project_files(project.id, DEMO_PROJECT_ROOT))
        assets = await service.repository.list_assets(project.id)
        citations = _citation_index(assets)
        draft = HandoffDraft(
            objective="复现 baseline，并推进学习率消融实验",
            state=[
                HandoffStateItem(
                    text="baseline 已完成，指标保存在 experiments/metrics.csv",
                    citations=_pick(citations, [("metrics.csv", {"run_key": "baseline"})]),
                ),
                HandoffStateItem(
                    text="旧数据缓存会沿用旧的数据划分，需要先验证再复现",
                    citations=_pick(citations, [("debugging.md", {"section": "数据缓存"})]),
                ),
            ],
            next_action="先确认 /data/aurora/cache 为空，再跑学习率消融",
            omissions=["尚未验证非默认随机种子 2027 与 2028。"],
        )
        preview = await handoff.prepare(project.id, draft)
        return {
            "project_id": project.id,
            "assets": imported["assets"],
            "chunks": imported["chunks"],
            "restricted": imported["restricted"],
            "restricted_reasons": imported["restricted_reasons"],
            "draft_id": preview.draft_id,
            "handoff": preview.model_dump(mode="json"),
            "citations": [
                citation.model_dump(mode="json")
                for item in draft.state
                for citation in item.citations
            ],
        }

    @router.post("/model")
    async def model_check(service: ServiceDep, payload: ModelCheckRequest | None = None):
        system_prompt = (
            "你是谨慎的科研交接助手。只返回 JSON，字段恰好是 headline 和 next_step。"
            "不要编造引用、命令或指标。"
        )
        prompt = (payload.prompt if payload else None) or (
            "接手人在发现数据缓存问题后要接手 Aurora baseline 复现，给出下一步。"
        )
        try:
            result = await service.model.generate_json(
                system_prompt, {"task": "labrelay_demo_model_check", "prompt": prompt}
            )
        except ValueError as exc:
            # The panel is a probe, so an unreachable or misconfigured channel has to read as
            # unavailable rather than as a server fault. The message stays out of the body: a
            # provider error can echo a URL or a key (spec 19.1).
            raise ApiError(
                503,
                DEPENDENCY_UNAVAILABLE,
                "The configured model channel did not answer. Check GENERATION_BASE_URL / "
                "GENERATION_API_KEY, or run with LABRELAY_USE_FAKE_DEPS=1.",
                {"dependency": "model"},
            ) from exc
        return {
            "model": getattr(service.model, "model", type(service.model).__name__),
            "result": result,
        }

    return router


def _citation_index(assets: list[Asset]) -> dict[str, Asset]:
    return {asset.title: asset for asset in assets}


def _pick(
    index: dict[str, Asset], wanted: list[tuple[str, dict[str, str | int]]]
) -> list[Citation]:
    """Resolve the panel's hand-written pointers onto imported assets by file name.

    The locators are prose (``run_key``, ``section``), not the ``line_start`` / ``page`` shapes the
    importer produces: they tell a reader where in the file to look, and ``asset_id`` is what the
    citation validator checks.
    """
    citations: list[Citation] = []
    for title, locator in wanted:
        asset = index.get(title)
        if asset is None:
            continue
        citations.append(
            Citation(
                asset_id=asset.id,
                kind=asset.kind,
                title=asset.title,
                uri=asset.uri,
                version=asset.version,
                locator=locator,
            )
        )
    return citations
