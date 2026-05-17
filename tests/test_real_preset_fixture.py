"""Regression tests against a real Serum-generated .SerumPreset file.

The fixture ``test_preset.SerumPreset`` is a preset captured from Serum 2.1.4
that exercises the full converter pipeline against actually-Serum-shaped CBOR
(~175 top-level keys, vs the synthetic minimal test's ~30). It locks in the
end-to-end shape so any drift in the preset → processor mapping fails loudly.

The tests here check structural invariants of the conversion output. They do
NOT compare against a captured getState ground-truth — that would require
also distributing the matching IComponent state blob.
"""
from __future__ import annotations

import hashlib
import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import cbor2
import pytest

from serum2_preset_loader import (
    PROCESSOR_FORMAT_VERSION,
    PROCESSOR_PRODUCT_VERSION,
    convert_preset_bytes,
    convert_preset_file,
    read_preset_metadata,
)
from serum2_preset_loader.converter import _PRESET_ONLY_TOPLEVEL_KEYS
from serum2_preset_loader.wrappers import (
    JUCE_VST3_MAGIC,
    XFER_MAGIC,
    juce_memoryblock_b64decode,
    unwrap_xferjson,
)


FIXTURE = Path(__file__).parent / "fixtures" / "test_preset.SerumPreset"


@pytest.fixture(scope="module")
def preset_bytes() -> bytes:
    if not FIXTURE.exists():
        pytest.skip(f"fixture missing: {FIXTURE}")
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def converted(preset_bytes) -> bytes:
    return convert_preset_bytes(preset_bytes)


def _extract_icomponent(state_blob: bytes) -> bytes:
    """Pull the IComponent payload out of a JUCE VST3 state envelope."""
    _magic, xml_len = struct.unpack("<II", state_blob[:8])
    xml = state_blob[8:8 + xml_len].decode("utf-8")
    return juce_memoryblock_b64decode(ET.fromstring(xml).find("IComponent").text)


# ─── End-to-end pipeline ──────────────────────────────────────────────────

def test_read_preset_metadata_against_real_preset(preset_bytes):
    """Real Serum metadata header should expose the documented fields."""
    meta = read_preset_metadata(str(FIXTURE))
    assert meta["fileType"] == "SerumPreset"
    assert "presetName" in meta
    assert "hash" in meta


def test_convert_produces_juce_envelope(converted):
    magic, xml_len = struct.unpack("<II", converted[:8])
    assert magic == JUCE_VST3_MAGIC
    assert xml_len > 0
    assert converted.endswith(b"\x00")


def test_convert_preset_file_matches_convert_preset_bytes(preset_bytes):
    """Disk-reading path produces the same blob as the in-memory one."""
    assert convert_preset_file(str(FIXTURE)) == convert_preset_bytes(preset_bytes)


def test_conversion_is_deterministic(preset_bytes):
    """Converting the same preset twice should byte-match.

    Two independent ZstdCompressor invocations producing identical output isn't
    guaranteed by the zstd API in general, but is true for the default
    parameters this converter uses. A regression here would mean the `hash`
    field becomes nondeterministic, which would be confusing.
    """
    a = convert_preset_bytes(preset_bytes)
    b = convert_preset_bytes(preset_bytes)
    assert a == b


# ─── Inner IComponent shape ───────────────────────────────────────────────

def test_inner_xferjson_has_processor_metadata(converted):
    icomp = _extract_icomponent(converted)
    meta, version, _cbor = unwrap_xferjson(icomp)
    assert version == 2
    assert meta["component"] == "processor"
    assert meta["product"] == "Serum2"
    assert meta["productVersion"] == PROCESSOR_PRODUCT_VERSION
    assert meta["version"] == PROCESSOR_FORMAT_VERSION


def test_inner_hash_is_md5_of_compressed_cbor(converted):
    """Same invariant as the captured state-blob test, but against bytes we
    generated ourselves — guards against the writer drifting from the reader.
    """
    icomp = _extract_icomponent(converted)
    assert icomp[:9] == XFER_MAGIC
    json_len = struct.unpack("<Q", icomp[9:17])[0]
    meta = json.loads(icomp[17:17 + json_len])
    compressed_cbor = icomp[17 + json_len + 8:]
    assert meta["hash"] == hashlib.md5(compressed_cbor, usedforsecurity=False).hexdigest()


# ─── Processor-state CBOR invariants ──────────────────────────────────────

def _converted_cbor(state_blob: bytes) -> dict:
    icomp = _extract_icomponent(state_blob)
    _meta, _version, cbor = unwrap_xferjson(icomp)
    return cbor2.loads(cbor)


def test_processor_only_toplevel_keys_present(converted):
    state = _converted_cbor(converted)
    assert state["component"] == "processor"
    assert state["killEnvsGracefullyCompat"] is True
    assert state["productVersion"] == PROCESSOR_PRODUCT_VERSION
    assert state["version"] == PROCESSOR_FORMAT_VERSION


def test_preset_only_toplevel_keys_stripped(converted):
    """None of the documented preset-only top-level keys should survive."""
    state = _converted_cbor(converted)
    for k in _PRESET_ONLY_TOPLEVEL_KEYS:
        assert k not in state, f"preset-only key {k!r} leaked into processor state"


def test_default_plainparams_sentinels_expanded(converted):
    """No ``plainParams: "default"`` strings should remain anywhere in the tree."""
    state = _converted_cbor(converted)

    def _no_default_sentinels(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "plainParams":
                    assert v != "default", "found unexpanded plainParams sentinel"
                _no_default_sentinels(v)
        elif isinstance(node, list):
            for item in node:
                _no_default_sentinels(item)

    _no_default_sentinels(state)


def test_arp_modules_have_active_clip(converted):
    """Every Arp<n> module should have ``activeClip`` set after conversion."""
    state = _converted_cbor(converted)
    arp_modules = [k for k, v in state.items()
                   if isinstance(v, dict) and k.startswith("Arp") and k[3:].isdigit()]
    assert arp_modules, "real preset should contain at least one Arp module"
    for k in arp_modules:
        assert "activeClip" in state[k], f"{k} missing activeClip after conversion"


def test_macro_name_subkey_stripped(converted):
    """If the preset has Macro<n> modules, their preset-only ``name`` field
    should be gone — this is the strip rule under real-preset conditions.
    """
    state = _converted_cbor(converted)
    macro_modules = [k for k, v in state.items()
                     if isinstance(v, dict) and k.startswith("Macro") and k[5:].isdigit()]
    if not macro_modules:
        pytest.skip("preset has no Macro modules to validate stripping against")
    for k in macro_modules:
        assert "name" not in state[k], f"{k} retained preset-only 'name' field"
