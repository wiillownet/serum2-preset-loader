"""Codecs for the wrapping formats Serum 2 uses around its CBOR payloads."""
from __future__ import annotations

import base64

import json
import struct

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


def unwrap_xferjson(
    blob: bytes, *, expected_version: int | None = None
) -> tuple[dict, int, bytes]:
    """Return (metadata_dict, format_version, decompressed_cbor_bytes).

    If ``expected_version`` is set, raise ``ValueError`` when the wrapper's
    embedded version differs — useful so any caller (not just the preset
    converter) can opt into the same guard.
    """
    if len(blob) < 17:
        raise ValueError(f"XferJson blob too short ({len(blob)} bytes)")
    if blob[:9] != XFER_MAGIC:
        raise ValueError(f"not an XferJson blob: magic={blob[:9]!r}")
    json_len = struct.unpack("<Q", blob[9:17])[0]
    if len(blob) < 17 + json_len + 8:
        raise ValueError("XferJson blob truncated before CBOR header")
    meta = json.loads(blob[17:17 + json_len])
    if not isinstance(meta, dict):
        raise ValueError(
            f"XferJson metadata must be a JSON object, got {type(meta).__name__}"
        )
    after = blob[17 + json_len:]
    uncompressed_size, version = struct.unpack("<II", after[:8])
    if expected_version is not None and version != expected_version:
        raise ValueError(
            f"unsupported XferJson format version {version}; expected {expected_version}"
        )
    try:
        cbor = zstandard.ZstdDecompressor().decompress(
            after[8:], max_output_size=50_000_000
        )
    except zstandard.ZstdError as e:
        raise ValueError(f"XferJson zstd payload corrupt: {e}") from e
    if len(cbor) != uncompressed_size:
        raise ValueError(
            f"XferJson CBOR size mismatch: header={uncompressed_size}, decompressed={len(cbor)}"
        )
    return meta, version, cbor


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
