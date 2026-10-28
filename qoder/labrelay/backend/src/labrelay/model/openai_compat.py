"""OpenAI-compatible model gateway behind the ``ModelGateway`` port.

Generation and embedding may live on different base URLs with different keys, so each
channel keeps its own endpoint/credential; the embedding channel optionally fixes the
vector width via ``dimensions`` so Task 7 can align stored vectors with the service.

Two rules shape the code below. A channel checks its own configuration the moment it is
used, so a half-configured deployment fails with the env var it is missing instead of
POSTing an empty model. And a 2xx reply that does not match the OpenAI shape becomes one
concise ``ValueError`` naming the channel and the endpoint — never the response body,
which can carry the prompt, the completion or a credential.
"""

from __future__ import annotations

import json
from numbers import Real
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from labrelay.config import LabRelayConfig

#: Appended to each channel's base URL; named in every error so a broken reply says
#: which channel produced it.
GENERATION_ENDPOINT = "/chat/completions"
EMBEDDING_ENDPOINT = "/embeddings"


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
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.generation_model = generation_model
        self.embedding_model = embedding_model
        self.embedding_base_url = (embedding_base_url or base_url).rstrip("/")
        self.embedding_api_key = embedding_api_key or api_key
        self.dimension = embedding_dimensions
        # ``httpx.Client`` needs a sync transport and ``httpx.AsyncClient`` an async one;
        # ``MockTransport`` implements both, but a real async transport does not, so each
        # channel checks the shape before opening a client rather than after.
        self._transport = transport

    @classmethod
    def from_config(cls, config: LabRelayConfig) -> OpenAICompatModelGateway:
        generation_base_url = config.generation_base_url or config.embedding_base_url
        if not generation_base_url:
            raise ValueError(
                "No model endpoint configured: set GENERATION_BASE_URL "
                "(or EMBEDDING_BASE_URL as a fallback)."
            )
        # A deployment can legitimately wire only one channel, so construction stays
        # permissive; the channel that is actually called reports what it misses.
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
        self._require_embedding_channel()
        payload: dict[str, Any] = {"model": self.embedding_model, "input": texts}
        if self.dimension is not None:
            payload["dimensions"] = self.dimension
        with httpx.Client(transport=self._sync_transport()) as client:
            response = client.post(
                f"{self.embedding_base_url}{EMBEDDING_ENDPOINT}",
                headers={"Authorization": f"Bearer {self.embedding_api_key}"},
                json=payload,
                timeout=60,
            )
        response.raise_for_status()
        return self._parse_embedding_response(response, expected=len(texts))

    async def generate_json(
        self, system_prompt: str, user_payload: dict[str, object]
    ) -> dict[str, object]:
        self._require_generation_channel()
        async with httpx.AsyncClient(transport=self._async_transport()) as client:
            response = await client.post(
                f"{self.base_url}{GENERATION_ENDPOINT}",
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
        return self._parse_generation_response(response)

    # --- response parsing ---------------------------------------------------------

    def _parse_generation_response(self, response: httpx.Response) -> dict[str, object]:
        body = self._decode_json(response, "generation", GENERATION_ENDPOINT)
        if not isinstance(body, dict):
            raise self._malformed(
                "generation", GENERATION_ENDPOINT, _wrong_shape("the response body", body)
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise self._malformed(
                "generation", GENERATION_ENDPOINT, "'choices' is missing or empty"
            )
        first = choices[0]
        if not isinstance(first, dict):
            raise self._malformed(
                "generation", GENERATION_ENDPOINT, _wrong_shape("choice 0", first)
            )
        message = first.get("message")
        if not isinstance(message, dict):
            raise self._malformed(
                "generation", GENERATION_ENDPOINT, "choice 0 has no 'message' object"
            )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise self._malformed(
                "generation", GENERATION_ENDPOINT, "message 'content' is missing or empty"
            )
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise self._malformed(
                "generation", GENERATION_ENDPOINT, "message 'content' is not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise self._malformed(
                "generation", GENERATION_ENDPOINT, _wrong_shape("message 'content'", parsed)
            )
        return parsed

    def _parse_embedding_response(
        self, response: httpx.Response, expected: int
    ) -> list[list[float]]:
        body = self._decode_json(response, "embedding", EMBEDDING_ENDPOINT)
        if not isinstance(body, dict):
            raise self._malformed(
                "embedding", EMBEDDING_ENDPOINT, _wrong_shape("the response body", body)
            )
        items = body.get("data")
        if not isinstance(items, list):
            raise self._malformed(
                "embedding", EMBEDDING_ENDPOINT, "'data' is missing or not a list"
            )
        if len(items) != expected:
            raise self._malformed(
                "embedding",
                EMBEDDING_ENDPOINT,
                f"returned a batch of {len(items)} vectors for {expected} input texts",
            )

        vectors: list[list[float]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise self._malformed(
                    "embedding", EMBEDDING_ENDPOINT, _wrong_shape(f"item {index}", item)
                )
            vector = item.get("embedding")
            if vector is None:
                raise self._malformed(
                    "embedding", EMBEDDING_ENDPOINT, f"item {index} has no 'embedding' list"
                )
            if not isinstance(vector, list):
                raise self._malformed(
                    "embedding",
                    EMBEDDING_ENDPOINT,
                    f"item {index} 'embedding' is a {type(vector).__name__}, "
                    "expected a list of numbers",
                )
            if not all(_is_number(value) for value in vector):
                raise self._malformed(
                    "embedding",
                    EMBEDDING_ENDPOINT,
                    f"item {index} 'embedding' must be a list of numbers",
                )
            if self.dimension is not None and len(vector) != self.dimension:
                raise self._malformed(
                    "embedding",
                    EMBEDDING_ENDPOINT,
                    f"item {index} has vector width {len(vector)} but "
                    f"EMBEDDING_DIMENSIONS says {self.dimension}",
                )
            vectors.append([float(value) for value in vector])
        return vectors

    @staticmethod
    def _decode_json(response: httpx.Response, channel: str, endpoint: str) -> Any:
        """Parse a 2xx body, reporting a broken one without echoing a byte of it."""
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OpenAICompatModelGateway._malformed(
                channel,
                endpoint,
                f"HTTP {response.status_code} body is not valid JSON",
            ) from exc

    @staticmethod
    def _malformed(channel: str, endpoint: str, problem: str) -> ValueError:
        # Deliberately carries no body, no request payload and no credential: providers
        # echo prompts in errors and these messages end up in logs.
        return ValueError(
            f"the {channel} channel got a malformed response from {endpoint}: {problem}"
        )

    # --- configuration ------------------------------------------------------------

    def _require_generation_channel(self) -> None:
        self._require_configured(
            "generation",
            (
                ("GENERATION_BASE_URL", self.base_url),
                ("GENERATION_API_KEY", self.api_key),
                ("GENERATION_MODEL", self.generation_model),
            ),
        )

    def _require_embedding_channel(self) -> None:
        self._require_configured(
            "embedding",
            (
                ("EMBEDDING_BASE_URL", self.embedding_base_url),
                ("EMBEDDING_API_KEY", self.embedding_api_key),
                ("EMBEDDING_MODEL", self.embedding_model),
            ),
        )
        if self.dimension is not None and self.dimension <= 0:
            raise ValueError(
                "embedding channel is not configured: set EMBEDDING_DIMENSIONS to a "
                f"positive integer, got {self.dimension}."
            )

    @staticmethod
    def _require_configured(channel: str, required: tuple[tuple[str, Any], ...]) -> None:
        missing = [name for name, value in required if not str(value or "").strip()]
        if missing:
            raise ValueError(
                f"{channel} channel is not configured: set {' and '.join(missing)} "
                f"before calling {channel}."
            )

    # --- transport shape ----------------------------------------------------------

    def _sync_transport(self) -> httpx.BaseTransport | None:
        if self._transport is not None and not isinstance(self._transport, httpx.BaseTransport):
            raise TypeError(
                f"the embedding channel runs synchronously and needs a httpx.BaseTransport: "
                f"the configured {type(self._transport).__name__} has no handle_request method."
            )
        return self._transport

    def _async_transport(self) -> httpx.AsyncBaseTransport | None:
        if self._transport is not None and not isinstance(
            self._transport, httpx.AsyncBaseTransport
        ):
            raise TypeError(
                f"the generation channel runs asynchronously and needs a "
                f"httpx.AsyncBaseTransport: the configured "
                f"{type(self._transport).__name__} has no handle_async_request method."
            )
        return self._transport


def _wrong_shape(label: str, value: object) -> str:
    return f"expected a JSON object, but {label} is a {type(value).__name__}"


def _is_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)
