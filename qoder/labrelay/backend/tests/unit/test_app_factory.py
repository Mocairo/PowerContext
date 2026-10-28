import httpx

from labrelay.api.main import create_app
from labrelay.config import load_app_config

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]


async def test_health_ready_reports_backend_status() -> None:
    app = create_app(load_app_config(PROJECT_ROOT))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "config_ready"
