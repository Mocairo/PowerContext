"""OpenAI-compatible model gateway behind the ``ModelGateway`` port.

Generation and embedding may live on different base URLs with different keys, so each
channel keeps its own endpoint/credential; the embedding channel optionally fixes the
vector width via ``dimensions`` so Task 7 can align stored vectors with the service.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from labrelay.config import LabRelayConfig


class OpenAICompatModelGateway:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        generation_model: str,
        embedding_model: str,
        embedding_base_url: str | None = None,
        embedding_api_key: str | None = None,
        embedding_dimensions: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.generation_model = generation_model
        self.embedding_model = embedding_model
        self.embedding_base_url = (embedding_base_url or base_url).rstrip("/")
        self.embedding_api_key = embedding_api_key or api_key
        self.dimension = embedding_dimensions
        self._transport = transport

    @classmethod
    def from_config(cls, config: LabRelayConfig) -> OpenAICompatModelGateway:
        generation_base_url = config.generation_base_url or config.embedding_base_url
        if not generation_base_url:
            raise ValueError(
                "No model endpoint configured: set GENERATION_BASE_URL "
                "(or EMBEDDING_BASE_URL as a fallback)."
            )
        return cls(
            base_url=generation_base_url,
            api_key=config.generation_api_key or config.embedding_api_key or "",
            generation_model=config.generation_model or "",
            embedding_model=config.embedding_model or "",
            embedding_base_url=config.embedding_base_url,
            embedding_api_key=config.embedding_api_key,
            embedding_dimensions=config.embedding.dimensions,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {"model": self.embedding_model, "input": texts}
        if self.dimension is not None:
            payload["dimensions"] = self.dimension
        with httpx.Client(transport=self._transport) as client:
            response = client.post(
                f"{self.embedding_base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.embedding_api_key}"},
                json=payload,
                timeout=60,
            )
        response.raise_for_status()
        return [item["embedding"] for item in response.json()["data"]]

    async def generate_json(
        self, system_prompt: str, user_payload: dict[str, object]
    ) -> dict[str, object]:
        async with httpx.AsyncClient(transport=self._transport) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.generation_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": json.dumps(user_payload, ensure_ascii=False),
                        },
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
                timeout=120,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
