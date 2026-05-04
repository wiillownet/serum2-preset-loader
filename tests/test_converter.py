"""Tests for the preset → processor CBOR transformation."""
from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import xml.etree.ElementTree as ET

import cbor2
import pytest
import zstandard

from serum2_preset_loader import (
    convert_preset_bytes,
    preset_cbor_to_processor_cbor,
    read_preset_metadata,
)
from serum2_preset_loader.wrappers import (
    XFER_MAGIC,
    juce_memoryblock_b64decode,
    unwrap_xferjson,
)


# ─── preset_cbor_to_processor_cbor ────────────────────────────────────────

def _minimal_preset_cbor() -> dict:
    """The smallest-ish preset CBOR exercising every transformation rule."""
    return {
        # modules with default-sentinel plainParams (rule: rewrite to {})
        "Arp0":   {"plainParams": "default"},
        "Env0":   {"plainParams": "default"},
        # nested default sentinel under an oscillator
        "Oscillator0": {
            "WTOsc0": {"plainParams": "default", "relativePathToWT": "X.wav"},
            "plainParams": "default",
        },
        # macro with preset-only `name` field (rule: drop it)
        "Macro0": {"plainParams": "default", "name": "OPEN"},
        # FX rack with preset-only `displayName` (rule: drop it)
        "FXRack0": {"plainParams": "default", "displayName": "RACK"},
        # MidiClip with preset-only UI fields (rule: drop them)
        "MidiClip1": {
            "clip": {},
            "plainParams": "default",
            "laneTabs": [1, 2, 3],
            "gridWidth_Beats": 4.0,
            "name": "Clip 1",
        },
        # PitchQuantizer with `scaleName` (rule: drop)
        "PitchQuantizer0": {"plainParams": "default", "scale": [], "scaleName": "C major"},
        # module with real plainParams (rule: pass through)
        "LFO0": {
            "plainParams": {"kParamRate": 3.276, "kParamMode": "Free"},
            "curveData": {},
            "pathData": {},
        },
        # preset-only top-level keys (rule: drop)
        "fileType": "SerumPreset",
        "presetName": "Tiny Test",
        "presetAuthor": "Tester",
        "presetDescription": "",
        "ClipPlayer": {},
        "Filter": {},
        "SerumGUI": {},
        "WTOsc": [],
        "GranularOsc": [],
        "MultiSampleOsc": [],
        "Osc": [],
        "SpectralOsc": [],
        "arpBankDisplayName": "",
        "clipBankDisplayName": "",
        # shared metadata that should get overridden
        "productVersion": "2.0.12",
        "version": 4.0,
    }


def test_default_sentinels_become_empty_dicts():
    out = preset_cbor_to_processor_cbor(_minimal_preset_cbor())
    assert out["Arp0"]["plainParams"] == {}
    assert out["Env0"]["plainParams"] == {}
    # nested expansion under Oscillator0 / WTOsc0
    assert out["Oscillator0"]["plainParams"] == {}
    assert out["Oscillator0"]["WTOsc0"]["plainParams"] == {}


def test_real_plainparams_pass_through_unchanged():
    out = preset_cbor_to_processor_cbor(_minimal_preset_cbor())
    assert out["LFO0"]["plainParams"] == {"kParamRate": 3.276, "kParamMode": "Free"}


def test_preset_only_toplevel_keys_dropped():
    out = preset_cbor_to_processor_cbor(_minimal_preset_cbor())
    for k in [
        "fileType", "presetName", "presetAuthor", "presetDescription",
        "ClipPlayer", "Filter", "SerumGUI",
        "WTOsc", "GranularOsc", "MultiSampleOsc", "Osc", "SpectralOsc",
        "arpBankDisplayName", "clipBankDisplayName",
    ]:
        assert k not in out, f"{k} should be stripped"


def test_preset_only_subkeys_dropped():
    out = preset_cbor_to_processor_cbor(_minimal_preset_cbor())
    assert "name" not in out["Macro0"]
    assert "displayName" not in out["FXRack0"]
    for k in ("laneTabs", "gridWidth_Beats", "name"):
        assert k not in out["MidiClip1"]
    assert "scaleName" not in out["PitchQuantizer0"]
    # should keep the legitimate processor-side fields
    assert "clip" in out["MidiClip1"]
    assert "scale" in out["PitchQuantizer0"]


def test_processor_only_keys_added():
    out = preset_cbor_to_processor_cbor(_minimal_preset_cbor())
    assert out["component"] == "processor"
    assert out["killEnvsGracefullyCompat"] is True
    assert out["Arp0"]["activeClip"] == 0


def test_version_markers_overwritten():
    out = preset_cbor_to_processor_cbor(_minimal_preset_cbor())
    assert out["productVersion"] == "2.1.4"
    assert out["version"] == 10.0


def test_version_is_serialized_as_cbor_float():
    """Regression guard: Serum writes `version` as a CBOR float, not int.

    cbor2 preserves the int/float distinction on encode, so emitting `10`
    instead of `10.0` would produce a byte-different CBOR document.
    """
    out = preset_cbor_to_processor_cbor(_minimal_preset_cbor())
    assert isinstance(out["version"], float)
    encoded = cbor2.dumps({"version": out["version"]})
    # CBOR float64 tag = 0xfb; this would be 0x18 (uint8) for an int
    assert b"\xfb" in encoded


def test_input_is_not_mutated():
    inp = _minimal_preset_cbor()
    snapshot = json.dumps(inp, sort_keys=True, default=str)
    _ = preset_cbor_to_processor_cbor(inp)
    assert json.dumps(inp, sort_keys=True, default=str) == snapshot


def test_unknown_processor_subkey_passes_through():
    """Blacklist-style subkey stripping should not eat keys we don't know about."""
    inp = _minimal_preset_cbor()
    inp["MidiClip1"]["someFutureProcessorField"] = 42
    out = preset_cbor_to_processor_cbor(inp)
    assert out["MidiClip1"]["someFutureProcessorField"] == 42


def test_prefix_match_requires_digit_suffix():
    """Hypothetical 'MacroBank' top-level should NOT match the 'Macro' prefix
    rule and lose its 'name' field."""
    inp = _minimal_preset_cbor()
    inp["MacroBank"] = {"plainParams": {}, "name": "should-not-be-stripped"}
    out = preset_cbor_to_processor_cbor(inp)
    assert out["MacroBank"]["name"] == "should-not-be-stripped"


def test_pitchquantizer_prefix_handles_arbitrary_index():
    """PitchQuantizer is on the prefix table; a hypothetical PitchQuantizer1
    should be stripped the same way as PitchQuantizer0."""
    inp = _minimal_preset_cbor()
    inp["PitchQuantizer1"] = {
        "plainParams": "default", "scale": [], "scaleName": "D minor",
    }
    out = preset_cbor_to_processor_cbor(inp)
    assert "scaleName" not in out["PitchQuantizer1"]
    assert "scale" in out["PitchQuantizer1"]


# ─── convert_preset_bytes (full pipeline) ─────────────────────────────────

def _make_minimal_preset_file() -> bytes:
    """Wrap _minimal_preset_cbor in the XferJson + JUCE envelope for testing."""
    cbor_bytes = cbor2.dumps(_minimal_preset_cbor())
    compressed = zstandard.ZstdCompressor().compress(cbor_bytes)
    meta = {"fileType": "SerumPreset", "presetName": "Tiny Test", "hash": "deadbeef"}
    meta_json = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return (
        XFER_MAGIC
        + struct.pack("<Q", len(meta_json))
        + meta_json
        + struct.pack("<II", len(cbor_bytes), 2)
        + compressed
    )


def test_convert_preset_bytes_produces_juce_envelope():
    out = convert_preset_bytes(_make_minimal_preset_file())
    magic, _xml_len = struct.unpack("<II", out[:8])
    assert magic == 0x21324356  # JUCE VST3 magic


def test_convert_preset_bytes_hash_is_md5_of_compressed_cbor():
    """Verified against a captured Serum 2.1.4 state: hash = md5(compressed_cbor)."""
    out = convert_preset_bytes(_make_minimal_preset_file())
    _magic, xml_len = struct.unpack("<II", out[:8])
    xml = out[8:8 + xml_len].decode("utf-8")
    icomponent_b64 = ET.fromstring(xml).find("IComponent").text
    icomponent = juce_memoryblock_b64decode(icomponent_b64)

    # IComponent is itself an XferJson blob; pull out the metadata + compressed payload.
    assert icomponent[:9] == XFER_MAGIC
    json_len = struct.unpack("<Q", icomponent[9:17])[0]
    meta = json.loads(icomponent[17:17 + json_len])
    after = icomponent[17 + json_len:]
    compressed_cbor = after[8:]

    assert meta["hash"] == hashlib.md5(compressed_cbor).hexdigest()
    assert meta["component"] == "processor"
    assert meta["productVersion"] == "2.1.4"


# ─── Input shape validation ───────────────────────────────────────────────

def _make_envelope(meta: dict, version: int = 2) -> bytes:
    cbor_bytes = cbor2.dumps({"some": "cbor"})
    compressed = zstandard.ZstdCompressor().compress(cbor_bytes)
    meta_json = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return (
        XFER_MAGIC
        + struct.pack("<Q", len(meta_json))
        + meta_json
        + struct.pack("<II", len(cbor_bytes), version)
        + compressed
    )


def test_convert_rejects_processor_state_blob():
    """Feeding in a captured IComponent processor state is the most likely
    user mistake; we want a precise error, not garbage out."""
    blob = _make_envelope({"component": "processor", "product": "Serum2"})
    with pytest.raises(ValueError, match="processor-state blob"):
        convert_preset_bytes(blob)


def test_convert_rejects_unknown_filetype():
    blob = _make_envelope({"fileType": "WhateverElse"})
    with pytest.raises(ValueError, match="not a .SerumPreset"):
        convert_preset_bytes(blob)


def test_convert_rejects_unsupported_xferjson_version():
    blob = _make_envelope({"fileType": "SerumPreset"}, version=99)
    with pytest.raises(ValueError, match="unsupported XferJson format version 99"):
        convert_preset_bytes(blob)


# ─── read_preset_metadata ─────────────────────────────────────────────────

def test_read_preset_metadata_returns_header_dict():
    blob = _make_minimal_preset_file()
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = os.path.join(tmp_dir, "preset.SerumPreset")
        with open(path, "wb") as f:
            f.write(blob)
        meta = read_preset_metadata(path)
    assert meta["fileType"] == "SerumPreset"
    assert meta["presetName"] == "Tiny Test"
    assert "hash" in meta
