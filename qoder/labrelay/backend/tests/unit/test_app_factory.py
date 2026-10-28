from pathlib import Path

import httpx

from labrelay.api.main import create_app
from labrelay.config import load_app_config

PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def test_health_ready_reports_backend_status() -> None:
    config = load_app_config(PROJECT_ROOT)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        assert app.state.config is config
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "config_ready"
