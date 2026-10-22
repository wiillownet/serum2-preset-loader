"""Tests for reading and writing .SerumPreset files (codec.py)."""
from __future__ import annotations

import copy
import hashlib
import math
import struct
from pathlib import Path

import cbor2
import pytest

from serum2_preset_loader import (
    convert_preset_bytes,
    convert_preset_tree,
    decode_preset_bytes,
    decode_preset_file,
    encode_cbor,
    encode_preset,
    write_preset_file,
)
from serum2_preset_loader.wrappers import unwrap_xferjson, wrap_xferjson

FIXTURE = Path(__file__).parent / "fixtures" / "test_preset.SerumPreset"


@pytest.fixture(scope="module")
def preset_bytes() -> bytes:
    return FIXTURE.read_bytes()


# ─── encode_cbor ───────────────────────────────────────────────────────────

def test_unedited_fixture_cbor_roundtrips_byte_for_byte(preset_bytes):
    _meta, _v, cbor = unwrap_xferjson(preset_bytes)
    assert encode_cbor(cbor2.loads(cbor)) == cbor


def test_plain_cbor2_does_not_roundtrip(preset_bytes):
    """Guards the reason encode_cbor exists: if cbor2 ever matches Serum's
    float widths on its own, this module can shrink to a cbor2 call."""
    _meta, _v, cbor = unwrap_xferjson(preset_bytes)
    assert cbor2.dumps(cbor2.loads(cbor)) != cbor


@pytest.mark.parametrize("value", [
    0, 1, 23, 24, 255, 256, 65535, 65536, 2**32 - 1, 2**32, 2**64 - 1,
    -1, -24, -25, -256, -(2**63),
    True, False, None,
    "", "kParamFreq", "naïve ✓", b"", b"\x00\xff",
    [], [1, "a", None], {}, {"b": 1, "a": ["x", {"c": False}]},
])
def test_encode_cbor_matches_cbor2_for_non_floats(value):
    # cbor2 and Serum agree on everything except float width.
    assert encode_cbor(value) == cbor2.dumps(value)


@pytest.mark.parametrize("value", [0.0, -0.0, 1.0, 0.5, 66.5, 0.6702131032943726, math.inf, -math.inf])
def test_float32_exact_values_use_four_bytes(value):
    enc = encode_cbor(value)
    assert enc[0] == 0xFA and len(enc) == 5
    assert struct.unpack(">d", struct.pack(">d", cbor2.loads(enc))) == \
        struct.unpack(">d", struct.pack(">d", value))   # keeps -0.0's sign


@pytest.mark.parametrize("value", [0.1, 66.60000000000001, 1e30, 1e300])
def test_other_floats_use_eight_bytes_and_survive(value):
    enc = encode_cbor(value)
    assert enc[0] == 0xFB and len(enc) == 9
    assert cbor2.loads(enc) == value


def test_nan_survives():
    assert math.isnan(cbor2.loads(encode_cbor(math.nan)))


def test_long_float_list_fast_path_matches_slow_path():
    exact = [i / 8 for i in range(1000)]           # all float32-exact
    mixed = exact[:500] + [0.1] + exact[500:]      # one needs 64 bits
    for xs in (exact, mixed):
        slow = b"".join(encode_cbor(x) for x in xs)
        head = b"\x99" + struct.pack(">H", len(xs))   # array, 2-byte length
        assert encode_cbor(xs) == head + slow
        assert cbor2.loads(encode_cbor(xs)) == xs


def test_float_list_beyond_float32_range_falls_back():
    xs = [1.0] * 100 + [1e300]
    assert cbor2.loads(encode_cbor(xs)) == xs


def test_key_order_is_preserved():
    enc = encode_cbor({"b": 1, "a": 2})
    assert enc == b"\xa2\x61b\x01\x61a\x02"


def test_unsupported_type_raises():
    with pytest.raises(TypeError, match="set"):
        encode_cbor({"x": {1, 2}})


# ─── decode / encode preset files ──────────────────────────────────────────

def test_decode_returns_header_and_tree(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    assert header["fileType"] == "SerumPreset"
    assert header["presetName"] == "test_preset"
    assert tree["VoiceFilter0"]["plainParams"]["kParamType"] == "MgL6"


def test_decode_rejects_processor_state():
    blob = wrap_xferjson({"component": "processor"}, cbor2.dumps({}))
    with pytest.raises(ValueError, match="processor-state"):
        decode_preset_bytes(blob)


def test_unedited_preset_roundtrips(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    out = encode_preset(header, tree)
    header2, tree2 = decode_preset_bytes(out)
    assert tree2 == tree
    assert {k: v for k, v in header2.items() if k != "hash"} == \
        {k: v for k, v in header.items() if k != "hash"}
    # Same CBOR as the original; only the Zstd stream (and so the hash) differs.
    assert unwrap_xferjson(out)[2] == unwrap_xferjson(preset_bytes)[2]


def test_header_key_order_and_format_match_serum(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    n = struct.unpack("<Q", preset_bytes[9:17])[0]
    original = preset_bytes[17:17 + n]
    out = encode_preset(header, tree)
    m = struct.unpack("<Q", out[9:17])[0]
    rewritten = out[17:17 + m]
    # Identical apart from the 32 hex digits of the hash.
    assert len(rewritten) == len(original)
    strip = lambda b: b.replace(header["hash"].encode(), b"").replace(  # noqa: E731
        decode_preset_bytes(out)[0]["hash"].encode(), b"")
    assert strip(rewritten) == strip(original)


def test_hash_is_md5_of_compressed_payload(preset_bytes):
    # Verified against Serum's own file first, then against ours.
    for blob in (preset_bytes, encode_preset(*decode_preset_bytes(preset_bytes))):
        n = struct.unpack("<Q", blob[9:17])[0]
        header = decode_preset_bytes(blob)[0]
        assert header["hash"] == hashlib.md5(blob[17 + n + 8:]).hexdigest()


def test_edit_is_written_and_read_back(preset_bytes, tmp_path):
    header, tree = decode_preset_bytes(preset_bytes)
    tree["VoiceFilter0"]["plainParams"]["kParamFreq"] = 0.25
    header = {**header, "presetName": "edited"}
    path = tmp_path / "edited.SerumPreset"
    write_preset_file(path, header, tree)
    header2, tree2 = decode_preset_file(path)
    assert header2["presetName"] == "edited"
    assert tree2["VoiceFilter0"]["plainParams"]["kParamFreq"] == 0.25


def test_encode_preset_rejects_wrong_filetype(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    with pytest.raises(ValueError, match="fileType"):
        encode_preset({**header, "fileType": "XferArpBank"}, tree)


def test_encode_preset_does_not_mutate_header(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    before = dict(header)
    encode_preset(header, tree)
    assert header == before


# ─── convert_preset_tree ───────────────────────────────────────────────────

def test_convert_tree_matches_convert_bytes(preset_bytes):
    _header, tree = decode_preset_bytes(preset_bytes)
    assert convert_preset_tree(tree) == convert_preset_bytes(preset_bytes)


def test_convert_tree_does_not_mutate_input(preset_bytes):
    _header, tree = decode_preset_bytes(preset_bytes)
    before = copy.deepcopy(tree)
    convert_preset_tree(tree)
    assert tree == before


def test_written_preset_converts(preset_bytes):
    """A file we wrote goes through the existing converter unchanged."""
    header, tree = decode_preset_bytes(preset_bytes)
    assert convert_preset_bytes(encode_preset(header, tree)) == \
        convert_preset_bytes(preset_bytes)
