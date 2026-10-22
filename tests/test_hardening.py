"""Malformed and hostile input: every failure is a ValueError, nothing is
allocated in proportion to what a file merely claims."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest
import zstandard

from serum2_preset_loader import convert_preset_bytes, read_preset_metadata
from serum2_preset_loader import wrappers
from serum2_preset_loader.wrappers import (
    XFER_MAGIC,
    _loads_cbor_tree,
    unwrap_xferjson,
    wrap_xferjson_precompressed,
)

FIXTURE = Path(__file__).parent / "fixtures" / "test_preset.SerumPreset"
HEADER = {"fileType": "SerumPreset"}


def envelope(cbor: bytes, *, size: int | None = None, compressed: bytes | None = None) -> bytes:
    if compressed is None:
        compressed = zstandard.ZstdCompressor().compress(cbor)
    return wrap_xferjson_precompressed(
        HEADER, compressed, uncompressed_size=len(cbor) if size is None else size
    )


# ─── decompression bounds ──────────────────────────────────────────────────

def test_frame_declaring_more_than_header_is_rejected_before_decompressing(monkeypatch):
    """The 13 KB → 400 MiB bomb. Proves the check runs before decompress()
    by making decompress() fail the test if it is ever reached."""
    bomb = zstandard.ZstdCompressor().compress(b"\x00" * (4 << 20))   # declares 4 MiB
    blob = envelope(b"", size=100, compressed=bomb)

    class MustNotDecompress:
        def decompress(self, *a, **k):
            raise AssertionError("decompress() reached despite a lying frame")
    # Patch the module attribute: older zstandard builds forbid setting
    # attributes on the extension type itself.
    monkeypatch.setattr(wrappers.zstandard, "ZstdDecompressor", MustNotDecompress)
    with pytest.raises(ValueError, match="frame declares 4194304"):
        unwrap_xferjson(blob)


def test_frame_without_declared_size_is_bounded_by_header():
    no_size = zstandard.ZstdCompressor(write_content_size=False).compress(b"\x00" * (1 << 20))
    with pytest.raises(ValueError):
        unwrap_xferjson(envelope(b"", size=100, compressed=no_size))


def test_header_size_beyond_cap_is_rejected(monkeypatch):
    monkeypatch.setattr(wrappers, "_MAX_CBOR_SIZE", 1000)
    with pytest.raises(ValueError, match="exceeds the 1000-byte limit"):
        unwrap_xferjson(envelope(b"\xa0" * 1, size=1001))


def test_honest_frame_still_decodes():
    assert _loads_cbor_tree(unwrap_xferjson(envelope(b"\xa1\x61a\x01"))[2]) == {"a": 1}


def test_fixture_still_converts():
    assert convert_preset_bytes(FIXTURE.read_bytes())[:4] == struct.pack("<I", 0x21324356)


# ─── header JSON ───────────────────────────────────────────────────────────

def test_json_length_beyond_cap_is_rejected_without_reading():
    blob = XFER_MAGIC + struct.pack("<Q", 2**40) + b"{}"
    with pytest.raises(ValueError, match="metadata length"):
        unwrap_xferjson(blob)


def test_deeply_nested_json_is_a_value_error():
    deep = b"[" * 200_000 + b"]" * 200_000
    blob = XFER_MAGIC + struct.pack("<Q", len(deep)) + deep + b"\x00" * 8
    with pytest.raises(ValueError):
        unwrap_xferjson(blob)


# ─── CBOR payload ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("cbor, match", [
    (b"\x9f", "corrupt"),                       # truncated indefinite array
    (b"\xa1\x61", "corrupt"),                   # truncated map
    (b"\x82\x01\x02", "must be a map"),         # top level is a list
    (b"\x05", "must be a map"),                 # top level is an int
    (b"\x61a", "must be a map"),                # top level is a str
])
def test_bad_cbor_is_a_value_error(cbor, match):
    with pytest.raises(ValueError, match=match):
        convert_preset_bytes(envelope(cbor))


def test_self_referencing_cbor_is_rejected():
    cycle = b"\xd8\x1c\xa1\x61k\xd8\x1d\x00"     # {'k': <itself>}
    with pytest.raises(ValueError, match="shared or self-referencing"):
        convert_preset_bytes(envelope(cycle))


def test_exponential_shared_references_are_rejected_quickly():
    """40 levels of [ref, ref] describe 2**40 nodes in a few hundred bytes;
    conversion used to hang. Rejection must not walk the expanded tree."""
    items = [b"\xd8\x1c\x80"]                               # shareable(0): []
    for i in range(40):
        ref = b"\xd8\x1d" + (bytes([0x18, i]) if i >= 24 else bytes([i]))
        items.append(b"\xd8\x1c\x82" + ref + ref)           # shareable([ref, ref])
    cbor = b"\xa1\x61x\x98" + bytes([len(items)]) + b"".join(items)
    with pytest.raises(ValueError, match="shared"):
        convert_preset_bytes(envelope(cbor))


def test_large_scalar_lists_are_not_walked_per_element():
    # A sample-sized list passes the shared-reference check without error.
    tree = {"SampleOsc0": {"data": [0.5] * 1_000_000}}
    import cbor2
    assert _loads_cbor_tree(cbor2.dumps(tree)) == tree


# ─── read_preset_metadata ──────────────────────────────────────────────────

def test_read_metadata_on_text_file_is_a_value_error(tmp_path):
    """Bytes 9..17 of a text file used to be read as a huge length
    (MemoryError)."""
    p = tmp_path / "notes.txt"
    p.write_text("hello world this is a text file\n" * 3)
    with pytest.raises(ValueError, match="not an XferJson blob"):
        read_preset_metadata(str(p))


@pytest.mark.parametrize("json_len", [2**40, 2**63, 2**64 - 1])
def test_read_metadata_with_huge_declared_length_is_a_value_error(tmp_path, json_len):
    p = tmp_path / "bad.SerumPreset"
    p.write_bytes(XFER_MAGIC + struct.pack("<Q", json_len) + b"{}")
    with pytest.raises(ValueError, match="metadata length"):
        read_preset_metadata(str(p))


def test_read_metadata_still_reads_fixture():
    assert read_preset_metadata(str(FIXTURE))["presetName"] == "test_preset"
