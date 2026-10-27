"""Independent wire checks and malformed-input boundaries for context packs."""

from __future__ import annotations

import base64
import hashlib
import json
import random
import subprocess
import sys

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)


def _json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("length", [0, 1, 600, 601, 602, 6000, 7300, 7301, 7302])
def test_independent_wire_oracle_includes_byte_count_own_digits(length):
    items = (b"x" * length, "海洋\n😀".encode())
    unsigned = {"schema": "powercontext.context-pack.v1", "items": [base64.b64encode(x).decode() for x in items]}
    digest = "sha256:" + hashlib.sha256(_json(unsigned)).hexdigest()
    expected = {**unsigned, "digest": digest, "byte_count": 0}
    for _ in range(10):
        if expected["byte_count"] == len(_json(expected)):
            break
        expected["byte_count"] = len(_json(expected))
    else:
        pytest.fail("independent oracle byte count did not converge")
    pack = ContextPack.build(items)
    assert pack.digest == digest
    assert pack.to_bytes() == _json(expected)
    assert pack.byte_count == len(pack.to_bytes())
    assert ContextPack.from_bytes(pack.to_bytes()).items == items


def test_random_utf8_boundaries_preserve_every_byte_and_are_deterministic():
    randomizer = random.Random(567)  # noqa: S311
    alphabet = "abc\r\n\té海😀\u2028\0"
    for _ in range(300):
        content = "".join(randomizer.choices(alphabet, k=randomizer.randrange(200)))
        ceiling = randomizer.randrange(1, 30)
        if any(len(character.encode()) > ceiling for character in content):
            with pytest.raises(ContextPackError):
                ContextPack.from_content(content, chunk_bytes=ceiling)
            continue
        pack = ContextPack.from_content(content, chunk_bytes=ceiling)
        assert pack == ContextPack.from_content(content, chunk_bytes=ceiling)
        assert b"".join(pack.items) == content.encode()
        assert all(len(part) <= ceiling and part.decode().encode() == part for part in pack.items)
    assert ContextPack.from_content("").items == ()


def test_random_targets_accept_arbitrary_valid_prefixes_and_shorter_results():
    randomizer = random.Random(918)  # noqa: S311
    values = [b"", b"a", b"repeat", "独立😀".encode()]
    for _ in range(300):
        base = ContextPack.build(tuple(randomizer.choices(values, k=randomizer.randrange(15))))
        retained = randomizer.randrange(len(base.items) + 1)
        suffix = tuple(randomizer.choices(values, k=randomizer.randrange(15)))
        target = ContextPack.build(base.items[:retained] + suffix)
        delta = ContextPackDelta(base.digest, target.digest, retained, suffix)
        decoded_base = ContextPack.from_bytes(base.to_bytes())
        decoded_delta = ContextPackDelta.from_bytes(delta.to_bytes())
        assert apply_context_pack_delta(decoded_base, decoded_delta) == target
        assert apply_context_pack_delta(base, ContextPackDelta.between(base, target)) == target
        assert delta.byte_count == len(delta.to_bytes())


@pytest.mark.parametrize("wire", [b"", b"\xff", b"{", b"[]", b"null", b"42", b'"text"', b"{}"])
def test_wire_requires_json_object_with_exact_schema(wire):
    for decoder in (ContextPack.from_bytes, ContextPackDelta.from_bytes):
        with pytest.raises(ContextPackError):
            decoder(wire)


@pytest.mark.parametrize("change", [{"extra": 1}, {"schema": "other"}, {"items": "YQ=="}, {"items": None}])
def test_pack_rejects_extra_fields_schema_and_nonarray_items(change):
    payload = {**json.loads(ContextPack.build((b"a",)).to_bytes()), **change}
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(_json(payload))


@pytest.mark.parametrize("item", ["!", "YQ", "YQ==\n", "YR==", "/w==", "-w==", "海洋", 0, None])
def test_base64_must_be_standard_canonical_and_decode_to_utf8(item):
    pack = ContextPack.build((b"a",))
    delta = ContextPackDelta.between(pack, pack)
    for original, field, decoder in (
        (pack, "items", ContextPack.from_bytes),
        (delta, "appended", ContextPackDelta.from_bytes),
    ):
        payload = json.loads(original.to_bytes())
        payload[field] = [item]
        with pytest.raises(ContextPackError):
            decoder(_json(payload))


@pytest.mark.parametrize("count", [True, False, 1.0, "999", -1, 0, None, float("nan")])
def test_pack_byte_count_is_strict_integer_and_matches_canonical_encoding(count):
    payload = json.loads(ContextPack.build((b"a",)).to_bytes())
    payload["byte_count"] = count
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(_json(payload))


@pytest.mark.parametrize("prefix", [True, False, 0.0, -1, "0", None, float("inf")])
def test_delta_prefix_is_strict_nonnegative_integer(prefix):
    pack = ContextPack.build((b"a",))
    with pytest.raises(ContextPackError):
        ContextPackDelta(pack.digest, pack.digest, prefix, ())


@pytest.mark.parametrize("digest", ["", "sha256:abc", "SHA256:" + "0" * 64, "sha256:" + "A" * 64, None, 0])
def test_delta_requires_sha256_digest_shape(digest):
    pack = ContextPack.build(())
    with pytest.raises(ContextPackError):
        ContextPackDelta(digest, pack.digest, 0, ())
    with pytest.raises(ContextPackError):
        ContextPackDelta(pack.digest, digest, 0, ())


def test_delta_rejects_each_missing_field_and_any_extra_field():
    pack = ContextPack.build((b"a",))
    payload = json.loads(ContextPackDelta.between(pack, pack).to_bytes())
    for field in payload:
        with pytest.raises(ContextPackError):
            ContextPackDelta.from_bytes(_json({key: value for key, value in payload.items() if key != field}))
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(_json({**payload, "byte_count": 123}))


def test_duplicate_json_fields_are_rejected_even_when_values_agree():
    pack = ContextPack.build((b"a",))
    delta = ContextPackDelta.between(pack, pack)
    for value, decoder in ((pack, ContextPack.from_bytes), (delta, ContextPackDelta.from_bytes)):
        wire = value.to_bytes()
        duplicate = b'{"schema":' + _json(json.loads(wire)["schema"]) + b"," + wire[1:]
        with pytest.raises(ContextPackError):
            decoder(duplicate)


def test_noncanonical_json_layout_normalizes_to_canonical_wire():
    pack = ContextPack.build((b"a", b""))
    delta = ContextPackDelta.between(pack, ContextPack.build((b"b",)))
    for value, decoder in ((pack, ContextPack.from_bytes), (delta, ContextPackDelta.from_bytes)):
        spaced = json.dumps(json.loads(value.to_bytes()), indent=2).encode()
        assert decoder(spaced).to_bytes() == value.to_bytes()


@pytest.mark.parametrize("items", [[b"a"], ("a",), (None,), (b"\xe6\xb5",), None])
def test_build_rejects_nonbyte_tuples_and_invalid_utf8(items):
    with pytest.raises(ContextPackError):
        ContextPack.build(items)


@pytest.mark.parametrize("ceiling", [0, -1, True, False, 1.0, "512", None])
def test_chunk_size_is_a_strict_positive_integer(ceiling):
    with pytest.raises(ContextPackError):
        ContextPack.from_content("", chunk_bytes=ceiling)


@pytest.mark.parametrize("content", [None, b"text", 1, "\ud800"])
def test_content_requires_encodable_text(content):
    with pytest.raises(ContextPackError):
        ContextPack.from_content(content)


def test_receiver_in_separate_process_needs_only_two_wire_messages():
    base = ContextPack.from_content("海洋\n" * 900)
    target = ContextPack.from_content("海洋\n" * 450 + "changed middle\n😀" + "海洋\n" * 450)
    delta = ContextPackDelta.between(base, target)
    receiver = """
import base64, sys
from powercontext.builtin.runtime.context_pack import ContextPack, ContextPackDelta, apply_context_pack_delta
base, delta = (base64.b64decode(line) for line in sys.stdin.buffer.read().splitlines())
result = apply_context_pack_delta(ContextPack.from_bytes(base), ContextPackDelta.from_bytes(delta))
sys.stdout.buffer.write(b''.join(result.items))
"""
    result = subprocess.run(
        [sys.executable, "-c", receiver],
        input=base64.b64encode(base.to_bytes()) + b"\n" + base64.b64encode(delta.to_bytes()),
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert result.stdout == b"".join(target.items)
