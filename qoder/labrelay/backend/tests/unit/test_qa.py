import asyncio

import httpx

from labrelay.api.main import create_app_with_service
from labrelay.application.qa import answer_project_question
from tests.helpers_planning import asset, make_service


def test_question_returns_cited_evidence() -> None:
    model_response = {
        "answer": "先检查历史缓存，再使用 baseline 配置。",
        "sufficient": True,
        "used_asset_ids": ["note-1"],
    }

    async def run():
        service, project = await make_service(
            assets=[asset("note-1", "note")], model_response=model_response
        )
        return await answer_project_question(service, project.id, "baseline 之前要注意什么？")

    response = asyncio.run(run())
    assert response.citations[0].asset_id == "note-1"
    assert "缓存" in response.answer


def test_question_without_evidence_declines_instead_of_inventing() -> None:
    async def run():
        service, project = await make_service(assets=[], model_response={"tasks": []})
        return await answer_project_question(service, project.id, "未导入的 GPU 编号是什么？")

    response = asyncio.run(run())
    assert response.citations == []
    assert "无法" in response.answer


def test_unknown_used_assets_are_not_returned_as_citations() -> None:
    model_response = {
        "answer": "项目资料中没有足够证据。",
        "sufficient": True,
        "used_asset_ids": ["missing-asset"],
    }

    async def run():
        service, project = await make_service(
            assets=[asset("note-1", "note")], model_response=model_response
        )
        return await answer_project_question(service, project.id, "解释不存在的资料")

    response = asyncio.run(run())
    assert response.citations == []
    assert "资料不足" in response.answer


async def test_question_route_refuses_unknown_project() -> None:
    service, _ = await make_service(assets=[], model_response={"tasks": []})
    app = create_app_with_service(service)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/projects/missing/questions", json={"question": "baseline 在哪？"}
            )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"
