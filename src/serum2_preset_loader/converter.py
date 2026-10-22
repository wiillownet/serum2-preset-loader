"""Translate the CBOR inside a .SerumPreset into Serum 2's processor-state shape.

A .SerumPreset's CBOR is a "delta from defaults" structure: untouched modules
have ``plainParams: "default"`` and the file carries extra UI / library /
metadata fields. Serum 2's runtime processor state CBOR (what Serum writes via
VST3 IComponent::getState) uses fully expanded ``plainParams: {}`` for unchanged
modules, omits the UI/metadata, and adds a couple of processor-only fields.

Mapping the two shapes is a small, mostly mechanical transform.
"""
from __future__ import annotations

import hashlib
import struct
from types import MappingProxyType
from typing import Any, Mapping

import cbor2
import zstandard

from .wrappers import (
    XFER_MAGIC,
    _check_header_len,
    _parse_header,
    _loads_cbor_tree,
    build_juce_vst3_state,
    unwrap_xferjson,
    wrap_xferjson_precompressed,
)


_PRESET_ONLY_TOPLEVEL_KEYS: frozenset[str] = frozenset({
    # UI panels and library scratch state
    "ClipPlayer", "Filter", "SerumGUI",
    "arpBankDisplayName", "clipBankDisplayName",
    # Library template lists (alternative oscillator types)
    "GranularOsc", "MultiSampleOsc", "Osc", "SpectralOsc", "WTOsc",
    # Preset-file metadata (also lives in the JSON header)
    "fileType", "presetName", "presetAuthor", "presetDescription",
})

_PROCESSOR_EXTRA_TOPLEVEL: Mapping[str, Any] = MappingProxyType({
    "component": "processor",
    "killEnvsGracefullyCompat": True,
})

# Current Serum 2 processor-state version markers (observed in 2.1.4).
# `PROCESSOR_FORMAT_VERSION` is intentionally a Python float — Serum's getState
# writes a CBOR major-7 (float) value here, not an integer. cbor2 preserves the
# int/float distinction on encode, so emitting an int would produce a CBOR
# document that doesn't byte-match what Serum produces.
#
# These are public so callers targeting a different Serum build can override
# them without monkey-patching:
#
#     from serum2_preset_loader import converter
#     converter.PROCESSOR_PRODUCT_VERSION = "2.2.0"
#     converter.PROCESSOR_FORMAT_VERSION = 11.0
PROCESSOR_PRODUCT_VERSION = "2.1.4"
PROCESSOR_FORMAT_VERSION = 10.0

# Highest `XferJson` envelope format version this converter knows how to
# read/write. Serum 2.1.4 ships v2; if a future Serum bumps the wrapper to v3
# we want to fail loudly instead of silently producing a malformed state.
SUPPORTED_XFERJSON_VERSION = 2

# A "prefix" here means the module-name family followed by an integer suffix
# (e.g. "Macro0", "PitchQuantizer3"). Using a blacklist (rather than
# whitelisting "clip"/"plainParams") so that any new processor-side fields a
# future Serum version adds will pass through instead of being silently
# dropped.
_PRESET_ONLY_SUBKEYS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "Macro":           ("name",),
    "FXRack":          ("displayName",),
    "MidiClip": (
        "displayLength_Beats", "gridWidth_Beats", "gridYOffset_Rows",
        "laneTabs", "name",
    ),
    "PitchQuantizer":  ("scaleName",),
}


def _matches_prefix_with_index(key: str, prefix: str) -> bool:
    """Match `<prefix><digits>` exactly (e.g. 'Macro0', not 'MacroBank')."""
    return key.startswith(prefix) and key[len(prefix):].isdigit()


_CONTAINERS = frozenset({dict, list})


def _copy_expanding_plainparams(obj: Any) -> Any:
    """Copy-on-write walk: a new dict for every dict, ``plainParams: "default"``
    replaced with ``{}`` on the way, and lists rebuilt only when they hold a
    container. A list of scalars (a sample-based preset carries tens of
    millions of floats) is returned as-is, which is safe because scalars are
    immutable and nothing downstream mutates lists.

    Replaces ``copy.deepcopy`` plus an in-place walk: on a 160 MB preset those
    two cost ~100 s; this pass costs about a second.
    """
    if isinstance(obj, dict):
        return {
            k: ({} if k == "plainParams" and v == "default" else _copy_expanding_plainparams(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        # set(map(type, ...)) runs in C; a genexpr over 41M floats does not.
        if not set(map(type, obj)).isdisjoint(_CONTAINERS):
            return [_copy_expanding_plainparams(x) for x in obj]
        return obj
    return obj


def _strip_preset_only_subkeys(state: dict) -> None:
    """Remove sub-keys that exist in the preset shape but not the processor shape."""
    for key, value in state.items():
        if not isinstance(value, dict):
            continue
        for prefix, drop_keys in _PRESET_ONLY_SUBKEYS_BY_PREFIX.items():
            if _matches_prefix_with_index(key, prefix):
                for dk in drop_keys:
                    value.pop(dk, None)


def preset_cbor_to_processor_cbor(preset_cbor: dict) -> dict:
    """Pure dict-to-dict transformation. Does not touch wrappers."""
    state = _copy_expanding_plainparams(preset_cbor)
    for k in _PRESET_ONLY_TOPLEVEL_KEYS:
        state.pop(k, None)
    _strip_preset_only_subkeys(state)
    state.update(_PROCESSOR_EXTRA_TOPLEVEL)
    # Every Arp<n> module gets a default `activeClip: 0` if the preset doesn't
    # carry one. Hardcoding `Arp0` would miss future Arp1+ modules.
    for k, v in state.items():
        if isinstance(v, dict) and _matches_prefix_with_index(k, "Arp"):
            v.setdefault("activeClip", 0)
    state["productVersion"] = PROCESSOR_PRODUCT_VERSION
    state["version"] = PROCESSOR_FORMAT_VERSION
    return state


def _build_processor_metadata(compressed_cbor: bytes) -> dict:
    """Build the JSON metadata header Serum writes alongside its IComponent state.

    The ``hash`` field is md5 of the *compressed* CBOR payload — verified by
    md5'ing the compressed CBOR from a captured Serum 2.1.4 state and matching
    it against the metadata header byte-for-byte. We don't know whether Serum
    validates this on setState (it accepts mismatched values in our tests), but
    computing it correctly costs nothing and matches the round-trip shape.
    """
    return {
        "component": "processor",
        # `usedforsecurity=False` so this works on FIPS-restricted Python builds.
        # The hash is integrity, not security.
        "hash": hashlib.md5(compressed_cbor, usedforsecurity=False).hexdigest(),
        "product": "Serum2",
        "productVersion": PROCESSOR_PRODUCT_VERSION,
        "url": "https://xferrecords.com/",
        "vendor": "Xfer Records",
        "version": PROCESSOR_FORMAT_VERSION,
    }


def _validate_preset_filetype(meta: dict) -> None:
    """Reject blobs that aren't ``.SerumPreset`` files."""
    file_type = meta.get("fileType")
    if file_type != "SerumPreset":
        # Most common mistake: feeding in a captured IComponent processor state,
        # which uses the same XferJson envelope but has component='processor'.
        component = meta.get("component")
        hint = (
            " (looks like a processor-state blob; this converter takes "
            ".SerumPreset files only)"
            if component == "processor" else ""
        )
        raise ValueError(
            f"input is not a .SerumPreset (fileType={file_type!r}){hint}"
        )


def convert_preset_bytes(preset_bytes: bytes) -> bytes:
    """Convert raw .SerumPreset bytes to a DawDreamer-loadable VST3 state blob."""
    preset_meta, _format_version, preset_cbor_bytes = unwrap_xferjson(
        preset_bytes, expected_version=SUPPORTED_XFERJSON_VERSION
    )
    _validate_preset_filetype(preset_meta)
    preset_dict = _loads_cbor_tree(preset_cbor_bytes)
    del preset_cbor_bytes
    proc_dict = preset_cbor_to_processor_cbor(preset_dict)
    del preset_dict
    proc_cbor = cbor2.dumps(proc_dict)
    # The decoded tree of a sample-based preset is ~2 GB of float objects;
    # drop it before the compress/base64 copies below stack on top of it.
    del proc_dict

    # Compress exactly once: the same compressed bytes are both md5'd for the
    # `hash` metadata field and embedded in the XferJson envelope. This avoids
    # the cross-call invariant that two independent ZstdCompressor invocations
    # produce byte-identical output.
    compressed_cbor = zstandard.ZstdCompressor().compress(proc_cbor)
    proc_meta = _build_processor_metadata(compressed_cbor)
    icomponent = wrap_xferjson_precompressed(
        proc_meta, compressed_cbor, uncompressed_size=len(proc_cbor)
    )
    return build_juce_vst3_state(icomponent)


def convert_preset_file(preset_path: str) -> bytes:
    """Read a .SerumPreset from disk and return the VST3 state blob."""
    with open(preset_path, "rb") as f:
        return convert_preset_bytes(f.read())


def read_preset_metadata(preset_path: str) -> dict:
    """Return the JSON metadata header from a ``.SerumPreset`` file.

    Useful for batch-renaming, filtering by tag, or seeding an output
    filename with the preset's display name.

    Keys typically present: ``fileType``, ``presetName``, ``presetAuthor``,
    ``presetDescription``, ``tags``, ``product``, ``productVersion``,
    ``hash``, ``vendor``, ``url``, ``version``.

    Raises ``ValueError`` for a file that is not an XferJson blob or whose
    header is truncated or corrupt, and ``OSError`` if the file can't be read.
    """
    # Header only: no reason to decompress a 200 MB sample payload for it.
    with open(preset_path, "rb") as f:
        head = f.read(17)
        if len(head) < 17:
            raise ValueError(f"XferJson blob too short ({len(head)} bytes)")
        # Check magic and the declared length before reading that many bytes:
        # in any other file, bytes 9..17 are an arbitrary 64-bit number.
        if head[:9] != XFER_MAGIC:
            raise ValueError(f"not an XferJson blob: magic={head[:9]!r}")
        json_len = struct.unpack("<Q", head[9:17])[0]
        _check_header_len(json_len)
        blob = head + f.read(json_len + 8)
    meta, _off = _parse_header(blob)
    return meta
