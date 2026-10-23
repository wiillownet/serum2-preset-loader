"""Read and write ``.SerumPreset`` files as plain Python data.

``decode_preset_*`` returns the JSON header and the decoded CBOR tree;
``encode_preset`` / ``write_preset_file`` go the other way. The tree is the
preset-file shape (UI and library fields present; untouched modules hold an
empty ``plainParams`` map, and the converter also accepts the string
``"default"`` there) — the same input :func:`convert_preset_tree` takes, so an
edited tree can be converted and loaded without touching disk.

What the encoder reproduces, as observed in the Serum 2.1.4 test preset:

- **Float widths.** Serum writes a float as 32-bit when the value round-trips
  through float32 and 64-bit otherwise, never 16-bit. ``cbor2.dumps`` writes
  finite floats as 64-bit by default, which changes every float in the file
  (the test preset grows from 7823 to 8151 bytes).
- **Key order.** Every map in the file, and the JSON header, is in
  lexicographic key order. :func:`encode_preset` sorts keys the same way, so
  a parameter added to an edited tree lands where Serum would put it.

With both, the unedited test preset re-encodes to identical CBOR. Files
written this way have not yet been loaded in Serum 2 itself.
"""
from __future__ import annotations

import hashlib
import os
import stat
import struct
import uuid
from typing import Any, Union

import zstandard

from . import converter
from .wrappers import _loads_cbor_tree, unwrap_xferjson, wrap_xferjson_precompressed

PathLike = Union[str, bytes, "os.PathLike[str]", "os.PathLike[bytes]"]

# Long all-float lists are encoded in chunks of this many values: a single
# value that needs 64 bits then only sends its own chunk down the slow path.
_FAST_PATH_MIN = 64
_FAST_PATH_CHUNK = 4096


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
    return bytes((major << 5 | 27,)) + struct.pack(">Q", n)


def _int(x: int) -> bytes:
    if not -(2**64) <= x < 2**64:
        raise ValueError(f"integer {x} does not fit in CBOR's 64-bit argument")
    return _head(0, x) if x >= 0 else _head(1, -1 - x)


def _float(x: float) -> bytes:
    try:
        f32 = struct.pack(">f", x)
    except OverflowError:          # beyond float32's range
        return b"\xfb" + struct.pack(">d", x)
    # Compare bit patterns, not values: NaN != NaN, and a NaN whose payload
    # float32 can't hold must stay 64-bit.
    if struct.pack(">d", struct.unpack(">f", f32)[0]) == struct.pack(">d", x):
        return b"\xfa" + f32
    return b"\xfb" + struct.pack(">d", x)


def _float_chunk(xs: list) -> bytes:
    """Encode a list of floats; packs the whole chunk as float32 in C when
    every value round-trips, which is what makes sample data fast."""
    n = len(xs)
    try:
        f32 = struct.pack(f">{n}f", *xs)
    except OverflowError:
        return b"".join(map(_float, xs))
    # == treats NaN as unequal, so a chunk holding one takes the per-value
    # path (which keeps its payload exact); ±0 compare equal but pack keeps
    # the sign.
    if list(struct.unpack(f">{n}f", f32)) != xs:
        return b"".join(map(_float, xs))
    out = bytearray(5 * n)
    out[0::5] = b"\xfa" * n
    for i in range(4):
        out[i + 1::5] = f32[i::4]
    return bytes(out)


def _map_items(x: dict, sort_keys: bool):
    if not sort_keys:
        return x.items()
    if not all(type(k) is str for k in x):
        bad = next(type(k).__name__ for k in x if type(k) is not str)
        # Serum's maps have string keys only, and decode_preset_* rejects
        # anything else, so a file written with one couldn't be read back.
        raise TypeError(f"preset map keys must be str, got {bad}")
    return sorted(x.items())


def _encode(x: Any, out: list, sort_keys: bool) -> None:
    # bool before int: bool is a subclass of int.
    if x is True:
        out.append(b"\xf5")
    elif x is False:
        out.append(b"\xf4")
    elif x is None:
        out.append(b"\xf6")
    elif isinstance(x, int):
        out.append(_int(x))
    elif isinstance(x, float):
        out.append(_float(x))
    elif isinstance(x, str):
        b = x.encode("utf-8")
        out.append(_head(3, len(b)))
        out.append(b)
    elif isinstance(x, (bytes, bytearray)):
        out.append(_head(2, len(x)))
        out.append(bytes(x))
    elif isinstance(x, (list, tuple)):
        out.append(_head(4, len(x)))
        # Exact type check: bool/int must not take the float path, and float
        # subclasses (numpy.float64) take the per-value path, which handles
        # them correctly.
        if len(x) >= _FAST_PATH_MIN and set(map(type, x)) == {float}:
            xs = list(x)
            for s in range(0, len(xs), _FAST_PATH_CHUNK):
                out.append(_float_chunk(xs[s:s + _FAST_PATH_CHUNK]))
        else:
            for v in x:
                _encode(v, out, sort_keys)
    elif isinstance(x, dict):
        out.append(_head(5, len(x)))
        for k, v in _map_items(x, sort_keys):
            _encode(k, out, sort_keys)
            _encode(v, out, sort_keys)
    else:
        raise TypeError(
            f"cannot encode {type(x).__name__} in a Serum preset "
            "(expected dict, list, tuple, str, bytes, int, float, bool or None)"
        )


def encode_cbor(obj: Any, *, sort_keys: bool = False) -> bytes:
    """Encode ``obj`` to CBOR with Serum 2's float widths (see module docstring).

    Maps keep the given key order unless ``sort_keys`` is true, which sorts
    every map's keys in lexicographic order, as Serum's files are, and
    requires them all to be strings. Tuples encode as arrays. Raises
    ``TypeError`` for other types (or a non-string key when sorting) and
    ``ValueError`` for integers outside CBOR's 64-bit range.
    """
    out: list[bytes] = []
    _encode(obj, out, sort_keys)
    return b"".join(out)


# ─── .SerumPreset files ────────────────────────────────────────────────────

def decode_preset_bytes(preset_bytes: bytes) -> tuple[dict, dict]:
    """Return ``(header, tree)`` for raw ``.SerumPreset`` bytes.

    Raises ``ValueError`` for anything that is not a well-formed
    ``.SerumPreset``, including a captured processor state (same envelope,
    different ``fileType``). The accepted envelope version is
    ``converter.SUPPORTED_XFERJSON_VERSION``, read at call time, so the
    override in the README applies here too.
    """
    header, _version, cbor = unwrap_xferjson(
        preset_bytes, expected_version=converter.SUPPORTED_XFERJSON_VERSION
    )
    converter._validate_preset_filetype(header)
    return header, _loads_cbor_tree(cbor)


def _fspath(path: PathLike) -> str:
    # open() accepts an int as a file descriptor (and True as fd 1) and
    # closes it afterwards; os.fspath rejects ints.
    return os.fsdecode(os.fspath(path))


def decode_preset_file(path: PathLike) -> tuple[dict, dict]:
    """Read a ``.SerumPreset`` from disk and return ``(header, tree)``.

    ``path`` is a ``str``, ``bytes`` or path-like object.
    """
    with open(_fspath(path), "rb") as f:
        return decode_preset_bytes(f.read())


# Keys the header and the tree's top level both carry (presetName, tags,
# presetAuthor ...). The header is what Serum's browser and
# read_preset_metadata see; the tree is what Serum loads.
_HEADER_ONLY_KEYS = frozenset({"hash"})


def _same(a: Any, b: Any) -> bool:
    """Equal and of the same types all the way down: ``10 == 10.0`` in Python
    but they are different CBOR."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        return len(a) == len(b) and all(map(_same, a, b))
    return a == b


def _shared_mismatches(header: dict, tree: dict) -> list:
    return sorted(k for k in header
                  if k in tree and k not in _HEADER_ONLY_KEYS
                  and not _same(header[k], tree[k]))


def set_preset_metadata(header: dict, tree: dict, **fields: Any) -> None:
    """Set metadata fields in both the header and the tree, in place.

    ``set_preset_metadata(header, tree, presetName="Dark Pad", tags=["Pad"])``.
    Each field is set in the header, and in the tree too when the tree has
    that key at its top level, which keeps :func:`encode_preset`'s
    consistency check satisfied.
    """
    for key, value in fields.items():
        if key in _HEADER_ONLY_KEYS:
            raise ValueError(f"{key!r} is computed on write and can't be set")
        header[key] = value
        if key in tree:
            tree[key] = value


def encode_preset(header: dict, tree: dict) -> bytes:
    """Build ``.SerumPreset`` bytes from a header and a preset-shaped tree.

    ``header`` is typically the one :func:`decode_preset_bytes` returned, with
    any edits. Metadata such as ``presetName`` and ``tags`` lives in both the
    header and the tree's top level; the two must agree (same values and
    types), or this raises ``ValueError`` naming the keys, rather than
    silently keeping one side. Use :func:`set_preset_metadata` to edit both.
    Neither argument is modified.

    ``hash`` is recomputed as the MD5 of the compressed CBOR, the rule
    observed in the Serum 2.1.4 test preset; whether Serum checks it on load
    is untested. The Zstd stream differs from Serum's (its compressor
    settings are unknown), so the file as a whole is not byte-identical.

    Raises ``TypeError`` if ``header`` or ``tree`` is not a dict, has a
    non-string key, or holds a value that can't be encoded; ``ValueError``
    if ``fileType`` is not ``"SerumPreset"``, header and tree disagree, or
    the header holds NaN or infinity.
    """
    if not isinstance(header, dict):
        raise TypeError(f"header must be a dict, got {type(header).__name__}")
    if not isinstance(tree, dict):
        raise TypeError(f"tree must be a dict, got {type(tree).__name__}")
    if not all(type(k) is str for k in header):
        raise TypeError("header keys must be str")
    if header.get("fileType") != "SerumPreset":
        raise ValueError(
            f"header fileType must be 'SerumPreset', got {header.get('fileType')!r}"
        )
    mismatched = _shared_mismatches(header, tree)
    if mismatched:
        raise ValueError(
            "header and tree disagree on " + ", ".join(map(repr, mismatched))
            + "; set both (set_preset_metadata does this)"
        )
    cbor = encode_cbor(tree, sort_keys=True)
    compressed = zstandard.ZstdCompressor().compress(cbor)
    header = dict(header)
    header["hash"] = hashlib.md5(compressed, usedforsecurity=False).hexdigest()
    header = dict(sorted(header.items()))   # Serum's header is sorted too
    return wrap_xferjson_precompressed(
        header, compressed,
        uncompressed_size=len(cbor),
        version=converter.SUPPORTED_XFERJSON_VERSION,
    )


def write_preset_file(path: PathLike, header: dict, tree: dict) -> None:
    """Write a ``.SerumPreset`` to disk (see :func:`encode_preset`).

    The preset is encoded in full, written to a uniquely named temporary file
    in the same folder, and moved into place, so a failure (or another thread
    writing the same path) never leaves a partial preset at ``path``. If
    ``path`` is a symlink, the file it points to is replaced. An existing
    file keeps its permission bits; a new one gets the usual umask-based
    mode. Moving into place replaces the file rather than rewriting it, so
    other hard links to it keep the old contents. On Windows the move fails
    with ``PermissionError`` while another program holds the file open.
    """
    data = encode_preset(header, tree)
    target = os.path.realpath(_fspath(path))
    folder, name = os.path.split(target)
    # Dot-prefixed with a non-preset suffix, so a preset browser scanning the
    # folder while it exists doesn't list it.
    tmp = os.path.join(folder, f".{name}.{uuid.uuid4().hex}.tmp")
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = None
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
