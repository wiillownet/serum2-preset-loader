"""Tests for reading and writing .SerumPreset files (codec.py)."""
from __future__ import annotations

import copy
import os
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
    set_preset_metadata,
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
    -1, -24, -25, -256, -(2**63), -(2**64), 2**64 - 1,
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


def test_header_bytes_preserved_apart_from_hash(preset_bytes):
    """The rewritten JSON header is Serum's, with only the hash value
    replaced in place: same keys, order, number formats and position."""
    header, tree = decode_preset_bytes(preset_bytes)
    out = encode_preset(header, tree)

    def raw_header(blob):
        n = struct.unpack("<Q", blob[9:17])[0]
        return blob[17:17 + n]
    new_hash = decode_preset_bytes(out)[0]["hash"]
    assert raw_header(out).replace(new_hash.encode(), header["hash"].encode()) == \
        raw_header(preset_bytes)


def test_hash_is_md5_of_compressed_payload(preset_bytes):
    # Verified against Serum's own file first, then against ours.
    for blob in (preset_bytes, encode_preset(*decode_preset_bytes(preset_bytes))):
        n = struct.unpack("<Q", blob[9:17])[0]
        header = decode_preset_bytes(blob)[0]
        assert header["hash"] == hashlib.md5(blob[17 + n + 8:]).hexdigest()


def test_edit_is_written_and_read_back(preset_bytes, tmp_path):
    header, tree = decode_preset_bytes(preset_bytes)
    tree["VoiceFilter0"]["plainParams"]["kParamFreq"] = 0.25
    set_preset_metadata(header, tree, presetName="edited")
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


# ─── gaps found by mutation testing in the audit ───────────────────────────

def test_long_int_and_bool_lists_stay_ints():
    """The fast path must only take lists of exact floats. [1]*100 == [1.0]*100
    in Python, so compare bytes, not values."""
    assert encode_cbor([1] * 100) == cbor2.dumps([1] * 100)
    assert encode_cbor([True] * 100) == cbor2.dumps([True] * 100)


def test_nan_is_written_as_32_bit():
    assert encode_cbor(math.nan) == b"\xfa\x7f\xc0\x00\x00"


def test_nan_with_64_bit_payload_stays_64_bit():
    x = struct.unpack(">d", bytes.fromhex("7ff8000000000001"))[0]
    enc = encode_cbor(x)
    assert enc == b"\xfb" + bytes.fromhex("7ff8000000000001")


@pytest.mark.parametrize("x", [2**64, -(2**64) - 1])
def test_out_of_range_int_raises_with_original_value(x):
    with pytest.raises(ValueError, match=str(x)):
        encode_cbor(x)


def test_bytearray_and_tuple_encode_like_bytes_and_list():
    assert encode_cbor(bytearray(b"ab")) == encode_cbor(b"ab")
    assert encode_cbor(("a", 1)) == encode_cbor(["a", 1])
    assert encode_cbor(tuple([0.5] * 100)) == encode_cbor([0.5] * 100)


def test_chunked_fast_path_matches_per_value_path():
    xs = [i / 8 for i in range(10_000)]
    xs[7000] = 0.1                       # one 64-bit value, mid-list
    xs[5000] = math.nan                  # one NaN in another chunk
    slow = b"".join(encode_cbor(x) for x in xs)
    assert encode_cbor(xs) == b"\x99" + struct.pack(">H", len(xs)) + slow


def test_sort_keys_orders_maps_alphabetically_at_every_level():
    tree = {"b": {"z": 1, "a": 2}, "a": [{"y": 0, "x": 0}]}
    assert cbor2.loads(encode_cbor(tree, sort_keys=True)) == tree
    assert list(cbor2.loads(encode_cbor(tree, sort_keys=True))) == ["a", "b"]
    assert encode_cbor(tree, sort_keys=True) == encode_cbor(
        {"a": [{"x": 0, "y": 0}], "b": {"a": 2, "z": 1}})


def test_added_parameter_lands_in_serum_key_order(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    params = tree["VoiceFilter0"]["plainParams"]
    params["kParamAaaa"] = 1.0           # inserted last, sorts first
    out = decode_preset_bytes(encode_preset(header, tree))[1]
    keys = list(out["VoiceFilter0"]["plainParams"])
    assert keys == sorted(keys) and keys[0] == "kParamAaaa"


def test_decode_respects_version_override(preset_bytes, monkeypatch):
    from serum2_preset_loader import converter
    raw = bytearray(preset_bytes)
    n = struct.unpack("<Q", raw[9:17])[0]
    raw[17 + n + 4:17 + n + 8] = struct.pack("<I", 3)
    with pytest.raises(ValueError, match="version 3"):
        decode_preset_bytes(bytes(raw))
    monkeypatch.setattr(converter, "SUPPORTED_XFERJSON_VERSION", 3)
    header, tree = decode_preset_bytes(bytes(raw))
    out = encode_preset(header, tree)
    assert struct.unpack("<I", out[17 + struct.unpack("<Q", out[9:17])[0] + 4:][:4])[0] == 3


@pytest.mark.parametrize("side", ["header", "tree"])
def test_header_tree_mismatch_raises_naming_the_key(preset_bytes, side):
    """Editing only one side used to be silently overwritten by the header."""
    header, tree = decode_preset_bytes(preset_bytes)
    (header if side == "header" else tree)["presetName"] = "renamed"
    with pytest.raises(ValueError, match="disagree on 'presetName'"):
        encode_preset(header, tree)


def test_mismatch_check_compares_types(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    header["version"] = 10            # tree holds float 10.0; 10 == 10.0
    with pytest.raises(ValueError, match="'version'"):
        encode_preset(header, tree)


def test_set_preset_metadata_sets_both_sides(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    set_preset_metadata(header, tree, presetName="renamed", tags=["Bass"], extra="x")
    assert header["presetName"] == tree["presetName"] == "renamed"
    assert header["tags"] == tree["tags"] == ["Bass"]
    assert header["extra"] == "x" and "extra" not in tree   # header-only key
    out_header, out_tree = decode_preset_bytes(encode_preset(header, tree))
    assert out_header["presetName"] == out_tree["presetName"] == "renamed"
    assert "hash" not in out_tree


def test_set_preset_metadata_refuses_hash(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    with pytest.raises(ValueError, match="hash"):
        set_preset_metadata(header, tree, hash="0" * 32)


@pytest.mark.parametrize("make, match", [
    (lambda h, t: ({**h, 1: "x"}, t), "header keys must be str"),
    # Maps holding only non-str keys, so sorted() itself can't fail first.
    (lambda h, t: (h, {**t, "Extra": {1: {}}}), "map keys must be str"),
    (lambda h, t: (h, {**t, "Extra": {(1, 2): 0.5}}), "map keys must be str"),
])
def test_non_string_keys_are_rejected_on_write(preset_bytes, make, match):
    """The decoder rejects them, so a file written with one couldn't be read."""
    header, tree = make(*decode_preset_bytes(preset_bytes))
    with pytest.raises(TypeError, match=match):
        encode_preset(header, tree)


@pytest.mark.parametrize("header, tree", [([], {}), (None, {}), ({"fileType": "SerumPreset"}, [1])])
def test_encode_preset_rejects_non_dicts(header, tree):
    with pytest.raises(TypeError, match="must be a dict"):
        encode_preset(header, tree)


def test_convert_tree_rejects_non_dict():
    with pytest.raises(TypeError, match="preset tree must be a dict"):
        convert_preset_tree([1])


def test_nan_in_header_is_rejected(preset_bytes):
    header, tree = decode_preset_bytes(preset_bytes)
    with pytest.raises(ValueError):
        encode_preset({**header, "x": math.nan}, tree)


def test_failed_write_leaves_existing_file_untouched(preset_bytes, tmp_path):
    path = tmp_path / "keep.SerumPreset"
    path.write_bytes(b"ORIGINAL")
    header, tree = decode_preset_bytes(preset_bytes)
    with pytest.raises(TypeError):
        write_preset_file(path, header, {**tree, "bad": {1, 2}})
    assert path.read_bytes() == b"ORIGINAL"
    assert list(tmp_path.iterdir()) == [path]          # no temp file left


def test_decode_rejects_shared_references():
    import zstandard
    from serum2_preset_loader.wrappers import wrap_xferjson_precompressed
    cycle = b"\xd8\x1c\xa1\x61k\xd8\x1d\x00"
    blob = wrap_xferjson_precompressed(
        {"fileType": "SerumPreset"}, zstandard.ZstdCompressor().compress(cycle),
        uncompressed_size=len(cycle))
    with pytest.raises(ValueError, match="shared"):
        decode_preset_bytes(blob)


def test_bytes_path_frees_decoded_tree_before_compression(preset_bytes, monkeypatch):
    """Guards decisions.md's peak-memory rule: convert_preset_bytes must not
    keep its own reference to the decoded tree while compressing."""
    import weakref
    import zstandard
    from serum2_preset_loader import converter

    class Tree(dict):
        pass

    seen = {}
    real_loads, real_compressor = converter._loads_cbor_tree, zstandard.ZstdCompressor

    def tracked_loads(cbor):
        tree = Tree(real_loads(cbor))
        seen["ref"] = weakref.ref(tree)
        return tree

    class Compressor:
        def compress(self, data):
            seen["alive_at_compress"] = seen["ref"]() is not None
            return real_compressor().compress(data)

    monkeypatch.setattr(converter, "_loads_cbor_tree", tracked_loads)
    monkeypatch.setattr(converter.zstandard, "ZstdCompressor", Compressor)
    convert_preset_bytes(preset_bytes)
    assert seen["alive_at_compress"] is False


def test_readme_examples_run(tmp_path):
    """The README's two code examples, run against the fixture."""
    import re
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    section = readme[readme.index("## Reading and writing presets"):readme.index("## Parameters")]
    blocks = re.findall(r"```python\n(.*?)```", section, re.S)
    assert len(blocks) == 2
    src = FIXTURE.read_bytes()
    (tmp_path / "My Preset.SerumPreset").write_bytes(src)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        for code in blocks:
            exec(compile(code, "README", "exec"), {})
    finally:
        os.chdir(cwd)
    h, t = decode_preset_file(tmp_path / "My Preset (darker).SerumPreset")
    assert t["VoiceFilter0"]["plainParams"]["kParamFreq"] == 0.25
    assert h["presetName"] == t["presetName"] == "My Preset (darker)"



# ─── write_preset_file edge cases (round-2 audit) ──────────────────────────

def test_concurrent_writers_never_leave_a_corrupt_file(preset_bytes, tmp_path):
    """Writers used to share one temp name per process; a slow writer could
    keep writing into the file another thread had already moved into place."""
    import threading

    header, tree = decode_preset_bytes(preset_bytes)
    path = tmp_path / "shared.SerumPreset"
    errors = []

    def writer(i):
        h, t = dict(header), dict(tree)
        set_preset_metadata(h, t, presetName=f"w{i}")
        try:
            for _ in range(20):
                write_preset_file(path, h, t)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    h, t = decode_preset_file(path)        # a complete, valid preset
    assert h["presetName"] == t["presetName"] and h["presetName"].startswith("w")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shared.SerumPreset"]


def test_bytes_path_round_trips(preset_bytes, tmp_path):
    header, tree = decode_preset_bytes(preset_bytes)
    path = os.fsencode(str(tmp_path / "b.SerumPreset"))
    write_preset_file(path, header, tree)
    assert decode_preset_file(path)[0]["presetName"] == "test_preset"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.SerumPreset"]


@pytest.mark.parametrize("bad", [0, 1, True])
def test_int_paths_are_rejected_not_treated_as_file_descriptors(preset_bytes, bad):
    header, tree = decode_preset_bytes(preset_bytes)
    with pytest.raises(TypeError):
        decode_preset_file(bad)
    with pytest.raises(TypeError):
        write_preset_file(bad, header, tree)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_existing_file_keeps_its_mode(preset_bytes, tmp_path):
    header, tree = decode_preset_bytes(preset_bytes)
    path = tmp_path / "private.SerumPreset"
    path.write_bytes(b"old")
    # Pin the umask so a newly created file would be 0o644: then 0o640 can
    # only come from copying the old file's mode, whatever the runner's umask.
    os.chmod(path, 0o640)
    old_umask = os.umask(0o022)
    try:
        write_preset_file(path, header, tree)
    finally:
        os.umask(old_umask)
    assert os.stat(path).st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name != "posix", reason="POSIX umask")
def test_new_file_gets_umask_mode_not_mkstemp_0600(preset_bytes, tmp_path):
    header, tree = decode_preset_bytes(preset_bytes)
    path = tmp_path / "new.SerumPreset"
    old = os.umask(0o022)
    try:
        write_preset_file(path, header, tree)
    finally:
        os.umask(old)
    assert os.stat(path).st_mode & 0o777 == 0o644


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlink_target_is_replaced_and_link_kept(preset_bytes, tmp_path):
    header, tree = decode_preset_bytes(preset_bytes)
    real = tmp_path / "real.SerumPreset"
    real.write_bytes(b"old")
    link = tmp_path / "link.SerumPreset"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlinks not permitted here")
    write_preset_file(link, header, tree)
    assert link.is_symlink()
    assert decode_preset_file(real)[0]["presetName"] == "test_preset"


def test_failed_move_raises_and_cleans_up(preset_bytes, tmp_path):
    header, tree = decode_preset_bytes(preset_bytes)
    folder = tmp_path / "a folder.SerumPreset"
    folder.mkdir()
    with pytest.raises(OSError):
        write_preset_file(folder, header, tree)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a folder.SerumPreset"]



# ─── round 3: gaps found by the third audit ────────────────────────────────

def test_written_header_keys_are_sorted(preset_bytes):
    """The fixture header is already sorted, so only a new key shows whether
    encode_preset sorts it."""
    header, tree = decode_preset_bytes(preset_bytes)
    set_preset_metadata(header, tree, aaaFirst="x", zzzLast="y")
    blob = encode_preset(header, tree)
    n = struct.unpack("<Q", blob[9:17])[0]
    import json
    keys = list(json.loads(blob[17:17 + n]))
    assert keys == sorted(keys) and keys[0] == "aaaFirst" and keys[-1] == "zzzLast"


def test_interrupted_write_cleans_up_temp_file(preset_bytes, tmp_path, monkeypatch):
    """KeyboardInterrupt is not an Exception; the cleanup must still run."""
    from serum2_preset_loader import codec

    header, tree = decode_preset_bytes(preset_bytes)
    path = tmp_path / "x.SerumPreset"

    def interrupted(src, dst):
        raise KeyboardInterrupt
    monkeypatch.setattr(codec.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        write_preset_file(path, header, tree)
    assert list(tmp_path.iterdir()) == []


def test_public_api():
    import serum2_preset_loader as pkg
    expected = {
        "decode_preset_bytes", "decode_preset_file", "encode_preset",
        "write_preset_file", "set_preset_metadata", "encode_cbor",
        "convert_preset_bytes", "convert_preset_file", "convert_preset_tree",
        "preset_cbor_to_processor_cbor", "read_preset_metadata",
    }
    assert expected <= set(pkg.__all__)
    assert all(hasattr(pkg, name) for name in pkg.__all__)
