"""Read and write ``.SerumPreset`` files as plain Python data.

``decode_preset_*`` returns the JSON header and the decoded CBOR tree;
``encode_preset`` / ``write_preset_file`` go the other way. The tree is the
preset-file shape (``plainParams: "default"`` for untouched modules, UI and
library fields present) — the same input :func:`preset_cbor_to_processor_cbor`
takes, so an edited tree can be converted and loaded without touching disk.

Serum writes each float in the narrowest CBOR width that holds it exactly:
32-bit where the value round-trips through float32, 64-bit otherwise.
``cbor2.dumps`` always writes 64-bit, which changes every float in the file
(the test preset grows from 7823 to 8151 bytes). :func:`encode_cbor` keeps
Serum's widths, so decode → encode of an unedited preset reproduces its CBOR
byte for byte.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Any

import cbor2
import zstandard

from .wrappers import unwrap_xferjson, wrap_xferjson_precompressed

PRESET_FORMAT_VERSION = 2  # XferJson envelope version for .SerumPreset files


# ─── CBOR encoding with Serum's float widths ───────────────────────────────

def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes((major << 5 | n,))
    if n < 0x100:
        return bytes((major << 5 | 24, n))
    if n < 0x10000:
        return bytes((major << 5 | 25,)) + struct.pack(">H", n)
    if n < 0x100000000:
        return bytes((major << 5 | 26,)) + struct.pack(">I", n)
    if n < 0x10000000000000000:
        return bytes((major << 5 | 27,)) + struct.pack(">Q", n)
    raise ValueError(f"integer {n} does not fit in CBOR's 64-bit argument")


def _float(x: float) -> bytes:
    try:
        f32 = struct.pack(">f", x)
    except OverflowError:          # beyond float32's range
        return b"\xfb" + struct.pack(">d", x)
    # NaN != NaN, so compare bit patterns via a second pack.
    if struct.pack(">d", struct.unpack(">f", f32)[0]) == struct.pack(">d", x):
        return b"\xfa" + f32
    return b"\xfb" + struct.pack(">d", x)


def _float_list(xs: list) -> bytes | None:
    """Fast path for long all-float lists (wavetable and sample data).

    A sample-based preset carries tens of millions of floats; encoding them
    one ``_float`` call at a time takes minutes. Packing the whole list as
    float32 and back runs in C. Returns ``None`` when some value needs 64 bits,
    so the caller falls back to the per-element path.
    """
    n = len(xs)
    try:
        f32 = struct.pack(f">{n}f", *xs)
    except OverflowError:          # a value beyond float32's range
        return None
    if list(struct.unpack(f">{n}f", f32)) != xs:
        return None
    out = bytearray(5 * n)
    out[0::5] = b"\xfa" * n
    for i in range(4):
        out[i + 1::5] = f32[i::4]
    return bytes(out)


def _encode(x: Any, out: list) -> None:
    # bool before int: bool is a subclass of int.
    if x is True:
        out.append(b"\xf5")
    elif x is False:
        out.append(b"\xf4")
    elif x is None:
        out.append(b"\xf6")
    elif isinstance(x, int):
        out.append(_head(0, x) if x >= 0 else _head(1, -1 - x))
    elif isinstance(x, float):
        out.append(_float(x))
    elif isinstance(x, str):
        b = x.encode("utf-8")
        out.append(_head(3, len(b)))
        out.append(b)
    elif isinstance(x, (bytes, bytearray)):
        out.append(_head(2, len(x)))
        out.append(bytes(x))
    elif isinstance(x, list):
        out.append(_head(4, len(x)))
        fast = None
        if len(x) >= 64 and set(map(type, x)) == {float}:
            fast = _float_list(x)
        if fast is not None:
            out.append(fast)
        else:
            for v in x:
                _encode(v, out)
    elif isinstance(x, dict):
        out.append(_head(5, len(x)))
        for k, v in x.items():
            _encode(k, out)
            _encode(v, out)
    else:
        raise TypeError(
            f"cannot encode {type(x).__name__} in a Serum preset "
            "(expected dict, list, str, bytes, int, float, bool or None)"
        )


def encode_cbor(obj: Any) -> bytes:
    """Encode ``obj`` to CBOR the way Serum 2 does (see module docstring).

    Key order is preserved as given; Serum's files are not canonically sorted.
    """
    out: list[bytes] = []
    _encode(obj, out)
    return b"".join(out)


# ─── .SerumPreset files ────────────────────────────────────────────────────

def decode_preset_bytes(preset_bytes: bytes) -> tuple[dict, dict]:
    """Return ``(header, tree)`` for raw ``.SerumPreset`` bytes.

    Raises ``ValueError`` if the bytes are not a ``.SerumPreset`` (a captured
    processor state uses the same envelope but a different ``fileType``).
    """
    from .converter import _validate_preset_filetype

    header, _version, cbor = unwrap_xferjson(
        preset_bytes, expected_version=PRESET_FORMAT_VERSION
    )
    _validate_preset_filetype(header)
    return header, cbor2.loads(cbor)


def decode_preset_file(path: str | Path) -> tuple[dict, dict]:
    """Read a ``.SerumPreset`` from disk and return ``(header, tree)``."""
    return decode_preset_bytes(Path(path).read_bytes())


def encode_preset(header: dict, tree: dict) -> bytes:
    """Build ``.SerumPreset`` bytes from a header and a preset-shaped tree.

    ``header`` is typically the one :func:`decode_preset_bytes` returned, with
    any edits (``presetName``, ``tags`` …). Its ``hash`` is recomputed: it is
    the MD5 of the compressed CBOR payload, verified against Serum 2.1.4
    files. Other keys are written as given, in the given order.

    The CBOR is byte-identical to what Serum would write for the same tree;
    the Zstd stream is not (Serum's compressor settings are unknown), which
    is why the hash has to be recomputed rather than carried over.
    """
    if header.get("fileType") != "SerumPreset":
        raise ValueError(
            f"header fileType must be 'SerumPreset', got {header.get('fileType')!r}"
        )
    cbor = encode_cbor(tree)
    compressed = zstandard.ZstdCompressor().compress(cbor)
    header = dict(header)
    header["hash"] = hashlib.md5(compressed, usedforsecurity=False).hexdigest()
    return wrap_xferjson_precompressed(
        header, compressed,
        uncompressed_size=len(cbor),
        version=PRESET_FORMAT_VERSION,
    )


def write_preset_file(path: str | Path, header: dict, tree: dict) -> None:
    """Write a ``.SerumPreset`` to disk (see :func:`encode_preset`)."""
    Path(path).write_bytes(encode_preset(header, tree))
