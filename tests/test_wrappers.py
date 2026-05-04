"""Tests for the XferJson and JUCE-VST3 envelope codecs."""
from __future__ import annotations

import struct

import pytest

from serum2_preset_loader.wrappers import (
    JUCE_VST3_MAGIC,
    XFER_MAGIC,
    build_juce_vst3_state,
    juce_memoryblock_b64decode,
    juce_memoryblock_b64encode,
    unwrap_xferjson,
    wrap_xferjson,
)


# ─── JUCE MemoryBlock base64 ──────────────────────────────────────────────

@pytest.mark.parametrize("data", [
    b"",
    b"\x00",
    b"\xff",
    b"hello",
    b"the quick brown fox jumps over the lazy dog",
    bytes(range(256)),
    b"\x00\x01\x02\x03\x04\x05\x06\x07",
])
def test_juce_b64_round_trip(data):
    assert juce_memoryblock_b64decode(juce_memoryblock_b64encode(data)) == data


def test_juce_b64_known_vector():
    """Verify against a hand-computed encoding so we catch alphabet drift."""
    # 1 byte = 8 bits = ceil(8/6) = 2 base64 chars after the "<len>." prefix.
    # Value 0x00 -> both chars are '.' (the value-0 char in JUCE's alphabet).
    assert juce_memoryblock_b64encode(b"\x00") == "1..."
    # 2 bytes = 16 bits = ceil(16/6) = 3 chars; 0x0000 -> "....".
    assert juce_memoryblock_b64encode(b"\x00\x00") == "2...."
    # First nonzero alphabet char is 'A' = value 1; LSB-first packing puts
    # data byte 0x01 into the low bit of char 0 -> 'A...'.
    assert juce_memoryblock_b64encode(b"\x01") == "1.A."


def test_juce_b64_decode_rejects_missing_separator():
    with pytest.raises(ValueError, match="missing '.' length separator"):
        juce_memoryblock_b64decode("XYZ")


def test_juce_b64_decode_rejects_bad_char():
    with pytest.raises(ValueError, match="bad character"):
        juce_memoryblock_b64decode("4.@@@@")


# ─── XferJson wrapper ─────────────────────────────────────────────────────

def test_xferjson_round_trip():
    metadata = {"component": "processor", "productVersion": "2.1.4"}
    payload = b"\xa1\x64test\x05"  # tiny CBOR map: {"test": 5}
    blob = wrap_xferjson(metadata, payload)
    assert blob.startswith(XFER_MAGIC)

    meta_out, version_out, cbor_out = unwrap_xferjson(blob)
    assert meta_out == metadata
    assert version_out == 2
    assert cbor_out == payload


def test_xferjson_rejects_short_blob():
    with pytest.raises(ValueError, match="too short"):
        unwrap_xferjson(b"\x00\x01")


def test_xferjson_rejects_bad_magic():
    with pytest.raises(ValueError, match="not an XferJson blob"):
        unwrap_xferjson(b"NOPE\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")


def test_xferjson_size_mismatch_caught():
    """If the uncompressed-size header lies, unwrap should reject the blob."""
    # Build a blob by hand with a wrong uncompressed_size in the header.
    import zstandard
    import json
    payload = b"hello world payload"
    compressed = zstandard.ZstdCompressor().compress(payload)
    meta_json = json.dumps({}).encode("utf-8")
    blob = (
        XFER_MAGIC
        + struct.pack("<Q", len(meta_json))
        + meta_json
        + struct.pack("<II", len(payload) + 100, 2)  # claim wrong size
        + compressed
    )
    with pytest.raises(ValueError, match="size mismatch"):
        unwrap_xferjson(blob)


# ─── JUCE VST3 state envelope ─────────────────────────────────────────────

def test_build_juce_vst3_state_starts_with_magic():
    blob = build_juce_vst3_state(b"icomponent payload")
    magic, _xml_len = struct.unpack("<II", blob[:8])
    assert magic == JUCE_VST3_MAGIC
    assert blob.endswith(b"\x00")


def test_build_juce_vst3_state_round_trip_via_xml():
    """The IComponent payload survives a round-trip through the XML envelope."""
    import xml.etree.ElementTree as ET

    payload = bytes(range(64)) + b"\xff" * 32
    blob = build_juce_vst3_state(payload)

    _magic, xml_len = struct.unpack("<II", blob[:8])
    xml = blob[8:8 + xml_len].decode("utf-8")
    root = ET.fromstring(xml)
    icomp = root.find("IComponent")
    assert icomp is not None and icomp.text is not None
    assert juce_memoryblock_b64decode(icomp.text) == payload
