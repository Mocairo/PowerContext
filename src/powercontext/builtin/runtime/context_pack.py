# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Self-contained UTF-8 context packs and verifiable prefix-reuse deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """A context pack, delta or requested UTF-8 boundary is invalid."""


def _encode(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContextPackError("Context pack JSON fields must not repeat")  # noqa: TRY003
        result[key] = value
    return result


def _decode(data: bytes, schema: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise ContextPackError("Context pack wire data must be bytes")  # noqa: TRY003
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object)
    except (ValueError, RecursionError) as error:
        raise ContextPackError("Context pack wire data must be UTF-8 JSON") from error  # noqa: TRY003
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != schema:
        raise ContextPackError("Context pack wire fields or schema do not match the protocol")  # noqa: TRY003
    return value


def _validate_items(items: tuple[bytes, ...]) -> None:
    if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
        raise ContextPackError("Context pack items must be a tuple of bytes")  # noqa: TRY003
    try:
        for item in items:
            item.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("Each context pack item must contain valid UTF-8") from error  # noqa: TRY003


def _wire_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _read_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("Context pack wire items must be a Base64 string array")  # noqa: TRY003
    items = []
    for item in value:
        if not isinstance(item, str):
            raise ContextPackError("Context pack wire items must be Base64 strings")  # noqa: TRY003
        try:
            decoded = base64.b64decode(item, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ContextPackError("Context pack item is not standard Base64") from error  # noqa: TRY003
        if base64.b64encode(decoded).decode("ascii") != item:
            raise ContextPackError("Context pack item is not canonical Base64")  # noqa: TRY003
        items.append(decoded)
    result = tuple(items)
    _validate_items(result)
    return result


def _validate_digest(value: object) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ContextPackError("Context pack digest must be sha256 followed by 64 lowercase hex digits")  # noqa: TRY003


def _pack_payload(items: tuple[bytes, ...]) -> dict[str, object]:
    unsigned = {"schema": _PACK_SCHEMA, "items": _wire_items(items)}
    digest = "sha256:" + hashlib.sha256(_encode(unsigned)).hexdigest()
    payload: dict[str, object] = {**unsigned, "digest": digest, "byte_count": 0}
    # The byte count includes the decimal digits of byte_count itself.
    while (size := len(_encode(payload))) != payload["byte_count"]:
        payload["byte_count"] = size
    return payload


@dataclass(frozen=True)
class ContextPack:
    """An immutable sequence of complete UTF-8 chunks with a canonical wire identity."""

    items: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_items(self.items)

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        return cls(items)

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> ContextPack:
        if not isinstance(content, str) or type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("Context content must be text and chunk_bytes a positive integer")  # noqa: TRY003
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContextPackError("Context content must be valid UTF-8") from error  # noqa: TRY003
        chunks = []
        start = 0
        while start < len(data):
            end = min(start + chunk_bytes, len(data))
            while end < len(data) and data[end] & 0xC0 == 0x80:
                end -= 1
            if end == start:
                raise ContextPackError("chunk_bytes is too small for a UTF-8 character")  # noqa: TRY003
            chunks.append(data[start:end])
            start = end
        return cls.build(tuple(chunks))

    @property
    def digest(self) -> str:
        unsigned = {"schema": _PACK_SCHEMA, "items": _wire_items(self.items)}
        return "sha256:" + hashlib.sha256(_encode(unsigned)).hexdigest()

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _encode(_pack_payload(self.items))

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _decode(data, _PACK_SCHEMA, {"schema", "items", "digest", "byte_count"})
        result = cls.build(_read_items(payload["items"]))
        if payload["digest"] != result.digest:
            raise ContextPackError("Context pack digest does not match its items")  # noqa: TRY003
        if type(payload["byte_count"]) is not int or payload["byte_count"] != result.byte_count:
            raise ContextPackError("Context pack byte_count does not match its canonical wire size")  # noqa: TRY003
        return result


@dataclass(frozen=True)
class ContextPackDelta:
    """Retain a prefix of one specific pack and append the complete target suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("Context pack prefix_items must be a nonnegative integer")  # noqa: TRY003
        _validate_items(self.appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("Context pack delta requires complete base and target packs")  # noqa: TRY003
        prefix = 0
        for previous, current in zip(base.items, target.items, strict=False):
            if previous != current:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _encode({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _wire_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _decode(data, _DELTA_SCHEMA, {"schema", "base_digest", "target_digest", "prefix_items", "appended"})
        return cls(
            base_digest=payload["base_digest"],
            target_digest=payload["target_digest"],
            prefix_items=payload["prefix_items"],
            appended=_read_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("Applying a context delta requires a complete base pack and delta")  # noqa: TRY003
    if base.digest != delta.base_digest:
        raise ContextPackError("Context pack delta does not match the base digest")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("Context pack delta prefix exceeds the base pack")  # noqa: TRY003
    target = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("Context pack delta does not reconstruct the target digest")  # noqa: TRY003
    return target


__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]
