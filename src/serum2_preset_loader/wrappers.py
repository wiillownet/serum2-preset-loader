"""Codecs for the wrapping formats Serum 2 uses around its CBOR payloads."""
from __future__ import annotations

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


def unwrap_xferjson(blob: bytes) -> tuple[dict, int, bytes]:
    """Return (metadata_dict, format_version, decompressed_cbor_bytes)."""
    if len(blob) < 17:
        raise ValueError(f"XferJson blob too short ({len(blob)} bytes)")
    if blob[:9] != XFER_MAGIC:
        raise ValueError(f"not an XferJson blob: magic={blob[:9]!r}")
    json_len = struct.unpack("<Q", blob[9:17])[0]
    if len(blob) < 17 + json_len + 8:
        raise ValueError("XferJson blob truncated before CBOR header")
    meta = json.loads(blob[17:17 + json_len])
    after = blob[17 + json_len:]
    uncompressed_size, version = struct.unpack("<II", after[:8])
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
    meta_json = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    compressed = zstandard.ZstdCompressor().compress(cbor_bytes)
    return wrap_xferjson_precompressed(
        meta_json, compressed,
        uncompressed_size=len(cbor_bytes),
        version=version,
    )


def wrap_xferjson_precompressed(
    metadata: dict | bytes,
    compressed_cbor: bytes,
    *,
    uncompressed_size: int,
    version: int = 2,
) -> bytes:
    """Emit the XferJson envelope from an already-Zstd-compressed CBOR payload.

    ``metadata`` may be a dict (will be JSON-encoded) or pre-encoded JSON bytes.
    """
    if isinstance(metadata, dict):
        meta_json = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
    else:
        meta_json = metadata
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


def juce_memoryblock_b64encode(data: bytes) -> str:
    num_chars = ((len(data) * 8) + 5) // 6
    out = [str(len(data)), "."]
    for i in range(num_chars):
        bit = i * 6
        v = 0
        for b in range(6):
            byte_i = (bit + b) >> 3
            if byte_i < len(data) and (data[byte_i] >> ((bit + b) & 7)) & 1:
                v |= 1 << b
        out.append(_JUCE_B64_ALPHABET[v])
    return "".join(out)


def juce_memoryblock_b64decode(s: str) -> bytes:
    dot = s.find(".")
    if dot < 0:
        raise ValueError("JUCE base64: missing '.' length separator")
    num_bytes = int(s[:dot])
    out = bytearray(num_bytes)
    bit_pos = 0
    for c in s[dot + 1:]:
        idx = _JUCE_B64_INDEX.get(c)
        if idx is None:
            raise ValueError(f"JUCE base64: bad character {c!r}")
        for b in range(6):
            byte_i = (bit_pos + b) >> 3
            if byte_i >= num_bytes:
                break
            if (idx >> b) & 1:
                out[byte_i] |= 1 << ((bit_pos + b) & 7)
        bit_pos += 6
    return bytes(out)


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
