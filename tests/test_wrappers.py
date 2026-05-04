"""Tests for the XferJson and JUCE-VST3 envelope codecs."""
from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET

import pytest
import zstandard

from serum2_preset_loader.wrappers import (
    JUCE_VST3_MAGIC,
    XFER_MAGIC,
    build_juce_vst3_state,
    juce_memoryblock_b64decode,
    juce_memoryblock_b64encode,
    unwrap_xferjson,
    wrap_xferjson,
    wrap_xferjson_precompressed,
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


def test_juce_b64_decode_empty_payload():
    """`<len 0>.` with no body should decode to empty bytes."""
    assert juce_memoryblock_b64decode("0.") == b""


def test_juce_b64_decode_rejects_non_numeric_length_prefix():
    with pytest.raises(ValueError, match="malformed length prefix"):
        juce_memoryblock_b64decode("abc.AAAA")


def test_juce_b64_decode_rejects_empty_length_prefix():
    with pytest.raises(ValueError, match="malformed length prefix"):
        juce_memoryblock_b64decode(".AAAA")


def test_juce_b64_decode_rejects_negative_length_prefix():
    with pytest.raises(ValueError, match="negative length prefix"):
        juce_memoryblock_b64decode("-1.A")


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


def test_xferjson_corrupt_zstd_caught_as_value_error():
    """A corrupt zstd payload should surface as ValueError, not ZstdError."""
    meta_json = json.dumps({}).encode("utf-8")
    blob = (
        XFER_MAGIC
        + struct.pack("<Q", len(meta_json))
        + meta_json
        + struct.pack("<II", 100, 2)
        + b"this is definitely not a zstd frame"
    )
    with pytest.raises(ValueError, match="zstd payload corrupt"):
        unwrap_xferjson(blob)


def test_xferjson_precompressed_round_trip():
    """wrap_xferjson_precompressed lets the caller reuse the compressed bytes."""
    payload = b"\xa1\x64test\x05" * 20  # repeating tiny CBOR
    compressed = zstandard.ZstdCompressor().compress(payload)
    blob = wrap_xferjson_precompressed(
        {"k": "v"}, compressed, uncompressed_size=len(payload)
    )
    meta, version, cbor = unwrap_xferjson(blob)
    assert meta == {"k": "v"}
    assert version == 2
    assert cbor == payload
    # The wrapped envelope should contain the *exact* compressed bytes we passed in.
    assert compressed in blob


# ─── JUCE VST3 state envelope ─────────────────────────────────────────────

def test_build_juce_vst3_state_starts_with_magic():
    blob = build_juce_vst3_state(b"icomponent payload")
    magic, _xml_len = struct.unpack("<II", blob[:8])
    assert magic == JUCE_VST3_MAGIC
    assert blob.endswith(b"\x00")


def test_build_juce_vst3_state_round_trip_via_xml():
    """The IComponent payload survives a round-trip through the XML envelope."""
    payload = bytes(range(64)) + b"\xff" * 32
    blob = build_juce_vst3_state(payload)

    _magic, xml_len = struct.unpack("<II", blob[:8])
    xml = blob[8:8 + xml_len].decode("utf-8")
    root = ET.fromstring(xml)
    icomp = root.find("IComponent")
    assert icomp is not None and icomp.text is not None
    assert juce_memoryblock_b64decode(icomp.text) == payload


def test_build_juce_vst3_state_includes_ieditcontroller():
    """When ieditcontroller is non-empty it appears as its own XML element."""
    icomponent = b"icomp data"
    iedit = b"editcontroller data \x00\x01\x02"
    blob = build_juce_vst3_state(icomponent, iedit)

    _magic, xml_len = struct.unpack("<II", blob[:8])
    xml = blob[8:8 + xml_len].decode("utf-8")
    root = ET.fromstring(xml)
    ic_el = root.find("IComponent")
    ec_el = root.find("IEditController")
    assert ic_el is not None and juce_memoryblock_b64decode(ic_el.text) == icomponent
    assert ec_el is not None and juce_memoryblock_b64decode(ec_el.text) == iedit
