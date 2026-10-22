"""Codecs for the wrapping formats Serum 2 uses around its CBOR payloads."""
from __future__ import annotations

import base64

import json
import struct

import cbor2
import zstandard


# ─── Serum's "XferJson" wrapper ────────────────────────────────────────────
#
# Both .SerumPreset files and Serum 2's VST3 IComponent state use the same
# wrapping:
#
#   9   bytes  b"XferJson\x00"
#   8   bytes  uint64_le  json metadata length
#   N   bytes  UTF-8 JSON metadata
#   4   bytes  uint32_le  uncompressed CBOR size
#   4   bytes  uint32_le  format version (2 in current Serum 2 builds)
#   M   bytes  Zstandard-compressed CBOR payload

XFER_MAGIC = b"XferJson\x00"

# Upper bounds on sizes an untrusted file can make us allocate. Every size in
# the envelope is attacker-controlled, so these are checked before any read or
# decompression. Real values are far below both: headers are a few hundred
# bytes, and the largest sample-based presets seen decompress to a few
# hundred MB.
_MAX_HEADER_JSON_SIZE = 16 << 20       # 16 MiB
_MAX_CBOR_SIZE = 1 << 30               # 1 GiB


def _parse_header(blob: bytes) -> tuple[dict, int]:
    """Return (metadata_dict, offset_of_cbor_header). Validates magic and JSON only."""
    if len(blob) < 17:
        raise ValueError(f"XferJson blob too short ({len(blob)} bytes)")
    if bytes(blob[:9]) != XFER_MAGIC:
        raise ValueError(f"not an XferJson blob: magic={bytes(blob[:9])!r}")
    json_len = struct.unpack("<Q", blob[9:17])[0]
    _check_header_len(json_len)
    if len(blob) < 17 + json_len + 8:
        raise ValueError("XferJson blob truncated before CBOR header")
    meta = _loads_header_json(bytes(blob[17:17 + json_len]))
    if not isinstance(meta, dict):
        raise ValueError(
            f"XferJson metadata must be a JSON object, got {type(meta).__name__}"
        )
    return meta, 17 + json_len


def _check_header_len(json_len: int) -> None:
    if json_len > _MAX_HEADER_JSON_SIZE:
        raise ValueError(
            f"XferJson metadata length {json_len} exceeds the "
            f"{_MAX_HEADER_JSON_SIZE}-byte limit (corrupt or not a Serum file)"
        )


def _loads_header_json(raw: bytes):
    try:
        return json.loads(raw)
    except RecursionError as e:  # pathologically nested JSON
        raise ValueError("XferJson metadata is nested too deeply") from e
    # JSONDecodeError and UnicodeDecodeError are already ValueErrors.


def unwrap_xferjson(
    blob: bytes, *, expected_version: int | None = None
) -> tuple[dict, int, bytes]:
    """Return (metadata_dict, format_version, decompressed_cbor_bytes).

    If ``expected_version`` is set, raise ``ValueError`` when the wrapper's
    embedded version differs — useful so any caller (not just the preset
    converter) can opt into the same guard.
    """
    meta, off = _parse_header(blob)
    # memoryview: slicing a 160 MB sample-based preset as bytes copied it
    # twice before decompression started.
    after = memoryview(blob)[off:]
    uncompressed_size, version = struct.unpack("<II", after[:8])
    if expected_version is not None and version != expected_version:
        raise ValueError(
            f"unsupported XferJson format version {version}; expected {expected_version}"
        )
    if uncompressed_size > _MAX_CBOR_SIZE:
        raise ValueError(
            f"XferJson CBOR size {uncompressed_size} exceeds the "
            f"{_MAX_CBOR_SIZE}-byte limit"
        )
    payload = after[8:]
    try:
        # decompress() allocates whatever size the frame itself declares and
        # ignores max_output_size when one is present (Serum's frames always
        # carry one), so a 13 KB file declaring 400 MiB used to allocate
        # 400 MiB before the size check below rejected it. Check the
        # declaration first. A frame without one is bounded by
        # max_output_size, i.e. by the header size checked above.
        declared = zstandard.frame_content_size(payload)
        if declared != -1 and declared != uncompressed_size:
            raise ValueError(
                f"XferJson CBOR size mismatch: header={uncompressed_size}, "
                f"zstd frame declares {declared}"
            )
        cbor = zstandard.ZstdDecompressor().decompress(
            payload, max_output_size=uncompressed_size
        )
    except zstandard.ZstdError as e:
        raise ValueError(f"XferJson zstd payload corrupt: {e}") from e
    if len(cbor) != uncompressed_size:
        raise ValueError(
            f"XferJson CBOR size mismatch: header={uncompressed_size}, decompressed={len(cbor)}"
        )
    return meta, version, cbor


_CBOR_CONTAINERS = (dict, list)


def _loads_cbor_tree(cbor_bytes: bytes) -> dict:
    """Decode a Serum CBOR payload, raising ``ValueError`` on anything corrupt.

    Wraps ``cbor2.loads`` so that every failure an untrusted file can cause
    surfaces as ``ValueError``: cbor2's own errors are not ``ValueError``
    subclasses, and deeply nested input can raise ``RecursionError`` on older
    cbor2. Also rejects a top level that is not a map, and any container that
    appears more than once in the tree. cbor2 decodes CBOR shared references
    (tags 28/29) into aliased objects, which lets a few hundred bytes describe
    a cyclic tree or one with 2**40 nodes; Serum does not write these.
    """
    try:
        tree = cbor2.loads(cbor_bytes)
    except (cbor2.CBORError, RecursionError) as e:
        raise ValueError(f"CBOR payload corrupt: {e}") from e
    if not isinstance(tree, dict):
        raise ValueError(
            f"CBOR payload must be a map, got {type(tree).__name__}"
        )
    _reject_shared_containers(tree)
    return tree


def _reject_shared_containers(tree: dict) -> None:
    seen: set[int] = set()
    stack: list = [tree]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            raise ValueError(
                "CBOR payload contains a shared or self-referencing value "
                "(CBOR tags 28/29); Serum presets do not use these"
            )
        seen.add(id(node))
        children = node.values() if isinstance(node, dict) else node
        # set(map(type, ...)) runs in C; lists of scalars (sample data, tens
        # of millions of floats) are skipped without a Python-level loop.
        if isinstance(node, list) and set(map(type, node)).isdisjoint(_CBOR_CONTAINERS):
            continue
        stack.extend(c for c in children if isinstance(c, _CBOR_CONTAINERS))


def wrap_xferjson(metadata: dict, cbor_bytes: bytes, *, version: int = 2) -> bytes:
    """Compress ``cbor_bytes`` and emit the full XferJson envelope.

    For pipelines that need the compressed bytes for *other* purposes too
    (computing a hash, for instance), use :func:`wrap_xferjson_precompressed`
    so the same compressed payload ends up both inside the envelope and in the
    caller's hand — avoiding the ``compress() == compress()`` cross-call
    invariant.
    """
    compressed = zstandard.ZstdCompressor().compress(cbor_bytes)
    return wrap_xferjson_precompressed(
        metadata, compressed,
        uncompressed_size=len(cbor_bytes),
        version=version,
    )


def wrap_xferjson_precompressed(
    metadata: dict,
    compressed_cbor: bytes,
    *,
    uncompressed_size: int,
    version: int = 2,
) -> bytes:
    """Emit the XferJson envelope from an already-Zstd-compressed CBOR payload."""
    meta_json = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    return (
        XFER_MAGIC
        + struct.pack("<Q", len(meta_json))
        + meta_json
        + struct.pack("<II", uncompressed_size, version)
        + compressed_cbor
    )


# ─── JUCE's VST3 state wrapper ─────────────────────────────────────────────
#
# DawDreamer's load_state / save_state read/write JUCE's VST3PluginState
# format: a 4-byte magic, the XML length, then a small XML document with
# IComponent and (optionally) IEditController as MemoryBlock-base64 elements.
# JUCE's MemoryBlock base64 is NOT standard base64: it uses a custom 64-char
# alphabet (with '.' = 0) and prefixes the encoded data with "<decimal length>.".

JUCE_VST3_MAGIC = 0x21324356  # b"VC2!" in little-endian
_JUCE_B64_ALPHABET = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_JUCE_B64_INDEX = {c: i for i, c in enumerate(_JUCE_B64_ALPHABET)}


# The alphabet constant has 65 symbols; only indices 0..63 are values.
# JUCE reads the byte stream LSB-first and puts the first stream bit in the
# LOW bit of each 6-bit char; standard base64 reads MSB-first and puts it in
# the HIGH bit. So JUCE(data) == std_b64(bitrev8(data)) with each char's 6-bit
# value reversed, minus the '=' padding. Both reversals are table lookups, so
# the whole codec runs in C. The previous per-bit Python loop took ~21 minutes
# on a 160 MB sample-based preset; this takes well under a second.
_BITREV8 = bytes.maketrans(
    bytes(range(256)),
    bytes(int(f"{i:08b}"[::-1], 2) for i in range(256)),
)
_STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _rev6(v: int) -> int:
    return int(f"{v:06b}"[::-1], 2)


# Standard char at index j carries value j MSB-first; JUCE's char for the same
# six stream bits has value rev6(j).
_STD_TO_JUCE = str.maketrans(_STD_ALPHABET, "".join(_JUCE_B64_ALPHABET[_rev6(j)] for j in range(64)))
_JUCE_TO_STD = str.maketrans(_JUCE_B64_ALPHABET[:64], "".join(_STD_ALPHABET[_rev6(j)] for j in range(64)))


def juce_memoryblock_b64encode(data: bytes) -> str:
    body = base64.b64encode(bytes(data).translate(_BITREV8)).decode("ascii").rstrip("=")
    return f"{len(data)}.{body.translate(_STD_TO_JUCE)}"


def juce_memoryblock_b64decode(s: str) -> bytes:
    dot = s.find(".")
    if dot < 0:
        raise ValueError("JUCE base64: missing '.' length separator")
    length_str = s[:dot]
    try:
        num_bytes = int(length_str)
    except ValueError as e:
        raise ValueError(
            f"JUCE base64: malformed length prefix {length_str!r}"
        ) from e
    if num_bytes < 0:
        raise ValueError(f"JUCE base64: negative length prefix {num_bytes}")
    body = s[dot + 1:]
    expected_chars = ((num_bytes * 8) + 5) // 6
    if len(body) != expected_chars:
        raise ValueError(
            f"JUCE base64: body length {len(body)} doesn't match length prefix "
            f"(expected {expected_chars} chars for {num_bytes} bytes)"
        )
    bad = set(body) - set(_JUCE_B64_ALPHABET[:64])
    if bad:
        raise ValueError(f"JUCE base64: bad character {sorted(bad)[0]!r}")
    std = body.translate(_JUCE_TO_STD) + "=" * (-len(body) % 4)
    # A final partial char can carry junk in its padding bits; standard
    # base64 would reject that, JUCE ignores it. validate=False plus the
    # length slice reproduces JUCE.
    raw = base64.b64decode(std, validate=False)[:num_bytes]
    return raw.translate(_BITREV8)


def build_juce_vst3_state(icomponent: bytes, ieditcontroller: bytes = b"") -> bytes:
    """Wrap raw IComponent (and optionally IEditController) bytes for load_state.

    String-concat XML is safe here because the only interpolated content is
    JUCE-base64 output, whose alphabet contains no XML metacharacters
    (``< > & " '``). Do not extend this to interpolate user-supplied text
    without proper escaping.
    """
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<VST3PluginState>'
    xml += f"<IComponent>{juce_memoryblock_b64encode(icomponent)}</IComponent>"
    if ieditcontroller:
        xml += f"<IEditController>{juce_memoryblock_b64encode(ieditcontroller)}</IEditController>"
    xml += "</VST3PluginState>"
    xml_bytes = xml.encode("utf-8")
    return struct.pack("<II", JUCE_VST3_MAGIC, len(xml_bytes)) + xml_bytes + b"\x00"
