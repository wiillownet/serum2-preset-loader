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


def test_large_scalar_lists_pass_the_tree_check():
    # A sample-sized list of floats is accepted by the tree check.
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


# ─── round 2: map keys, cbor2 version quirks, container count ──────────────

@pytest.mark.parametrize("cbor", [
    bytes.fromhex("a1820102a0"),        # {[1, 2]: {}} -> tuple key
    bytes.fromhex("a100a0"),            # {0: {}}      -> int key
    bytes.fromhex("a1a0a0"),            # {{}: {}}     -> frozendict key
    bytes.fromhex("a1616ba1f5a0"),      # nested map with a bool key
])
def test_non_string_map_keys_are_a_value_error(cbor):
    """These used to reach the converter and raise AttributeError from
    key.startswith(...)."""
    with pytest.raises(ValueError, match="non-string map key"):
        convert_preset_bytes(envelope(cbor))


@pytest.mark.parametrize("exc", [TypeError("unhashable"), MemoryError(), OverflowError()])
def test_any_cbor2_failure_becomes_value_error(monkeypatch, exc):
    """cbor2 5.4 raises TypeError for unhashable keys and MemoryError for a
    huge declared string length; neither is a ValueError."""
    def boom(_):
        raise exc
    monkeypatch.setattr(wrappers.cbor2, "loads", boom)
    with pytest.raises(ValueError, match=type(exc).__name__):
        _loads_cbor_tree(b"\xa0")


def test_huge_declared_string_length_is_rejected_before_decoding():
    cbor = b"\xa1\x61a\x5b" + (2**40).to_bytes(8, "big")
    with pytest.raises(ValueError, match="string length"):
        convert_preset_bytes(envelope(cbor))


def test_many_tiny_containers_are_rejected_before_decoding(monkeypatch):
    """4M empty lists: 201 bytes compressed, 566 MiB and 8.7 s decoded."""
    def must_not_decode(_):
        raise AssertionError("cbor2.loads reached despite the container cap")
    monkeypatch.setattr(wrappers.cbor2, "loads", must_not_decode)
    n = 4_000_000
    cbor = b"\xa1\x61x\x9a" + n.to_bytes(4, "big") + b"\x80" * n
    with pytest.raises(ValueError, match="more than 1000000 maps and arrays"):
        convert_preset_bytes(envelope(cbor))


def test_container_cap_boundary(monkeypatch):
    cbor = b"\xa1\x61x\x83\x80\xa0\x80"     # {'x': [[], {}, []]}: 5 containers
    monkeypatch.setattr(wrappers, "_MAX_CBOR_CONTAINERS", 5)
    _loads_cbor_tree(cbor)
    monkeypatch.setattr(wrappers, "_MAX_CBOR_CONTAINERS", 4)
    with pytest.raises(ValueError, match="more than 4 maps"):
        _loads_cbor_tree(cbor)


def test_indefinite_length_containers_are_counted(monkeypatch):
    cbor = b"\xbf\x61a\x9f\x01\xff\xff"     # {_ 'a': [_ 1]}
    monkeypatch.setattr(wrappers, "_MAX_CBOR_CONTAINERS", 1)
    with pytest.raises(ValueError, match="more than 1 maps"):
        _loads_cbor_tree(cbor)


def test_scan_step_cap(monkeypatch):
    monkeypatch.setattr(wrappers, "_MAX_CBOR_SCAN_STEPS", 100)
    alternating = b"\xfa\x3f\x00\x00\x00\xfb" + struct.pack(">d", 0.1)
    cbor = b"\xa1\x61x\x98\xc8" + alternating * 100
    with pytest.raises(ValueError, match="too many items to scan"):
        _loads_cbor_tree(cbor)


def test_float_runs_cost_one_scan_step_each(monkeypatch):
    """Sample data must not hit the step cap: 100k float32 values is one run
    per 4096 values (25 steps), plus a few for the map and key."""
    monkeypatch.setattr(wrappers, "_MAX_CBOR_SCAN_STEPS", 40)
    n = 100_000
    cbor = b"\xa1\x61x\x9a" + n.to_bytes(4, "big") + b"\xfa\x3f\x00\x00\x00" * n
    assert len(_loads_cbor_tree(cbor)["x"]) == n


def test_truncated_float_run_is_a_value_error():
    cbor = b"\xa1\x61x\x83" + b"\xfa\x3f\x00\x00\x00" * 2 + b"\xfa\x3f\x00"
    with pytest.raises(ValueError, match="truncated float"):
        wrappers._check_cbor_container_count(cbor)   # the scan itself, not cbor2


def test_scanner_accepts_every_valid_encoding():
    """The pre-scan must never reject CBOR that cbor2 itself decodes."""
    import math
    import random

    import cbor2

    rng = random.Random(1)
    scalars = [0, 23, 24, 256, 2**40, -1, -(2**40), 0.5, 0.1, math.inf, math.nan,
               -0.0, "", "ab" * 20, b"x" * 300, True, False, None, 1e300]

    def rnd(depth=0):
        r = rng.random()
        if depth > 3 or r < 0.4:
            return rng.choice(scalars)
        if r < 0.7:
            return [rnd(depth + 1) for _ in range(rng.randint(0, 8))]
        return {str(i): rnd(depth + 1) for i in range(rng.randint(0, 8))}

    for _ in range(500):
        tree = {"k": rnd()}
        for cbor in (cbor2.dumps(tree), cbor2.dumps(tree, canonical=True)):
            _loads_cbor_tree(cbor)


def test_fuzzed_fixture_only_raises_value_error():
    """Seeded bit flips and truncations of the real preset: every failure is
    a ValueError. (A 3000-iteration run in the audit found AttributeError,
    TypeError and MemoryError before these fixes.)"""
    import random

    rng = random.Random(7)
    good = FIXTURE.read_bytes()
    for _ in range(600):
        b = bytearray(good)
        if rng.random() < 0.2:
            b = b[:rng.randrange(len(b))]
        else:
            for _ in range(rng.randint(1, 8)):
                b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
        try:
            convert_preset_bytes(bytes(b))
        except ValueError:
            pass



# ─── round 3: cbor2 5 quirks, real cap values ──────────────────────────────

def test_stray_break_byte_is_a_value_error():
    """cbor2 5 decodes a lone 0xff as a break_marker value instead of
    raising; conversion then failed with cbor2's CBOREncodeTypeError."""
    with pytest.raises(ValueError):
        convert_preset_bytes(envelope(b"\xa1\x61x\xff"))


@pytest.mark.parametrize("value", [
    pytest.param(b"\xc1\x00", id="datetime tag"),
    pytest.param(b"\xd9\x03\xe8\x01", id="unknown tag"),
    pytest.param(b"\xf7", id="undefined"),
    pytest.param(b"\xf0", id="simple value"),
])
def test_non_plain_values_are_rejected(value):
    with pytest.raises(ValueError, match="Serum presets hold only"):
        _loads_cbor_tree(b"\xa1\x61x" + value)


def test_deep_nesting_is_rejected_on_any_cbor2():
    """cbor2 5 decodes 600 nested arrays; the converter's recursive copy
    then raised RecursionError. cbor2 6 rejects it itself ("nesting depth");
    either way it must be a ValueError. The boundary test below covers ours."""
    with pytest.raises(ValueError, match="nests deeper than 64|nesting depth"):
        convert_preset_bytes(envelope(b"\xa1\x61x" + b"\x81" * 600 + b"\x80"))


def test_depth_cap_boundary():
    # The top-level map is level 1, so 63 nested arrays reach level 64.
    _loads_cbor_tree(b"\xa1\x61x" + b"\x81" * 62 + b"\x80")
    with pytest.raises(ValueError, match="nests deeper"):
        _loads_cbor_tree(b"\xa1\x61x" + b"\x81" * 63 + b"\x80")


def test_fixture_is_well_inside_every_cap():
    import cbor2
    _meta, _v, cbor = unwrap_xferjson(FIXTURE.read_bytes())
    tree = cbor2.loads(cbor)

    def walk(o, d=1):
        if isinstance(o, dict):
            return 1 + sum(walk(v, d + 1)[0] for v in o.values()), max([d] + [walk(v, d + 1)[1] for v in o.values()])
        if isinstance(o, list):
            return 1 + sum(walk(v, d + 1)[0] for v in o), max([d] + [walk(v, d + 1)[1] for v in o])
        return 0, d - 1
    containers, depth = walk(tree)
    assert containers == 425 and depth <= 5       # documented in wrappers.py


def test_reserved_additional_info_is_rejected_by_the_scan():
    with pytest.raises(ValueError, match="reserved additional info 28"):
        wrappers._check_cbor_container_count(b"\x5c")


def test_real_cbor_size_cap():
    """Not monkeypatched: the shipped 1 GiB cap rejects one byte more."""
    with pytest.raises(ValueError, match="exceeds the 1073741824-byte limit"):
        unwrap_xferjson(envelope(b"\xa0", size=(1 << 30) + 1))


def test_real_header_size_cap():
    blob = XFER_MAGIC + struct.pack("<Q", (16 << 20) + 1) + b"{}"
    with pytest.raises(ValueError, match="exceeds the 16777216-byte limit"):
        unwrap_xferjson(blob)


def test_shipped_limits():
    """The README and decisions.md quote these; changing one is a decision."""
    assert wrappers._MAX_HEADER_JSON_SIZE == 16 << 20
    assert wrappers._MAX_CBOR_SIZE == 1 << 30
    assert wrappers._MAX_CBOR_CONTAINERS == 1_000_000
    assert wrappers._MAX_CBOR_SCAN_STEPS == 10_000_000
    assert wrappers._MAX_CBOR_DEPTH == 64
