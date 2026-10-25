"""Translate the CBOR inside a .SerumPreset into Serum 2's processor-state shape.

A .SerumPreset's CBOR is a "delta from defaults" structure: untouched modules
have either ``plainParams: "default"`` or an empty map (the 2.1.4 test preset
uses only empty maps), and the file carries extra UI / library / metadata
fields. Serum 2's runtime processor state CBOR (what Serum writes via
VST3 IComponent::getState) uses fully expanded ``plainParams: {}`` for unchanged
modules, omits the UI/metadata, and adds ``component: "processor"``.

Mapping the two shapes is a small, mostly mechanical transform.
"""
from __future__ import annotations

import hashlib
import math
import numbers
import os
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
})

# The state carries the preset's own `version` and `productVersion`, so
# Serum runs the same migration it runs when it opens the preset itself (FX
# Distortion Freq rescaling, behaviour-compat flags, ...). Stamping a newer
# version skipped that migration and changed how old presets sound.
#
# These are the fallback for a tree that has no version markers: Serum 2.1.5,
# which then loads it as current. `PROCESSOR_FORMAT_VERSION` is a float because
# Serum's getState writes a CBOR float there, not an integer.
PROCESSOR_PRODUCT_VERSION = "2.1.5"
PROCESSOR_FORMAT_VERSION = 11.0

# Highest `XferJson` envelope format version this converter knows how to
# read/write. Serum 2.1.4 ships v2; if a future Serum bumps the wrapper to v3
# we want to fail loudly instead of silently producing a malformed state.
SUPPORTED_XFERJSON_VERSION = 2

# A "prefix" here means the module-name family followed by an integer suffix
# (e.g. "Macro0", "MidiClip3"). Using a blacklist (rather than
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
    markers = _version_markers(preset_cbor)    # before the expensive copy
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
    state["productVersion"], state["version"] = markers
    return state


def _version_markers(preset_cbor: dict) -> tuple[str, float]:
    """The tree's ``(productVersion, version)``, or the fallback constants.

    Both or neither: one alone would pair an old version with the current
    product (or the reverse), and Serum migrates from the pair.
    """
    present = [k for k in ("productVersion", "version") if k in preset_cbor]
    if len(present) == 1:
        raise ValueError(f"preset has {present[0]} but not "
                         f"{({'productVersion', 'version'} - set(present)).pop()}; set both or neither")
    product = preset_cbor.get("productVersion", PROCESSOR_PRODUCT_VERSION)
    version = preset_cbor.get("version", PROCESSOR_FORMAT_VERSION)
    if not isinstance(product, str) or not product:
        raise ValueError(f"preset productVersion {product!r} is not a non-empty string")
    if isinstance(version, bool) or not isinstance(version, (int, float)) or not 0 < version < 1e6:
        raise ValueError(f"preset version {version!r} is not a positive number")
    return product, float(version)


def _build_processor_metadata(compressed_cbor: bytes, product_version: str, version: float) -> dict:
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
        "productVersion": product_version,
        "url": "https://xferrecords.com/",
        "vendor": "Xfer Records",
        "version": version,
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
    holder = [_loads_cbor_tree(preset_cbor_bytes)]
    del preset_cbor_bytes
    return _convert_owned_tree(holder)


def convert_preset_tree(preset_dict: dict) -> bytes:
    """Convert an already-decoded preset tree to a VST3 state blob.

    For edit-and-render loops: decode a preset once with
    :func:`decode_preset_file`, change fields in the tree, and convert each
    variant here without writing a ``.SerumPreset`` to disk. The tree is not
    modified. Output is identical to :func:`convert_preset_bytes` on a file
    holding the same tree.

    The caller still holds the tree, so unlike :func:`convert_preset_bytes`
    it stays alive through compression: peak memory is higher on
    sample-based presets.
    """
    if not isinstance(preset_dict, dict):
        raise TypeError(f"preset tree must be a dict, got {type(preset_dict).__name__}")
    return _convert_owned_tree([preset_dict])


def build_processor_state(preset_dict: dict) -> bytes:
    """The processor (VST3 IComponent) state for a decoded preset tree.

    These are the XferJson bytes a DAW stores as the plugin's processor state,
    the part :func:`convert_preset_tree` wraps for DawDreamer. Pair it with
    :func:`build_controller_state` when writing a DAW project. The tree is
    not modified.
    """
    if not isinstance(preset_dict, dict):
        raise TypeError(f"preset tree must be a dict, got {type(preset_dict).__name__}")
    return _processor_state([preset_dict])


def _convert_owned_tree(holder: list) -> bytes:
    return build_juce_vst3_state(_processor_state(holder))


def _processor_state(holder: list) -> bytes:
    """Shared body. ``holder`` is a one-element list so this frame holds the
    only reference to the decoded tree. The processor tree shares its sample
    lists, so once ``proc_dict`` is deleted below, nothing keeps the decoded
    sample arrays alive during compression."""
    proc_dict = preset_cbor_to_processor_cbor(holder.pop())
    markers = proc_dict["productVersion"], proc_dict["version"]
    proc_cbor = cbor2.dumps(proc_dict)
    # The decoded tree of a sample-based preset is ~2 GB of float objects;
    # drop it before the compress/base64 copies below stack on top of it.
    del proc_dict

    # Compress exactly once: the same compressed bytes are both md5'd for the
    # `hash` metadata field and embedded in the XferJson envelope. This avoids
    # the cross-call invariant that two independent ZstdCompressor invocations
    # produce byte-identical output.
    compressed_cbor = zstandard.ZstdCompressor().compress(proc_cbor)
    proc_meta = _build_processor_metadata(compressed_cbor, *markers)
    return wrap_xferjson_precompressed(
        proc_meta, compressed_cbor, uncompressed_size=len(proc_cbor)
    )


# Controller (VST3 IEditController) state: what Serum shows rather than plays.
# Measured against Live 12's saved state for a factory preset under Serum
# 2.1.5. Top-level keys copied from the preset as they are:
_CONTROLLER_TOPLEVEL_KEYS: tuple[str, ...] = (
    "ClipPlayer", "Filter", "SerumGUI",
    "GranularOsc", "MultiSampleOsc", "Osc", "SpectralOsc", "WTOsc",
    "arpBankDisplayName", "clipBankDisplayName",
    "presetName", "presetAuthor", "presetDescription",
    "product", "url", "vendor",
)


def build_controller_state(preset_dict: dict, preset_path: str | None = None) -> bytes:
    """The controller (VST3 IEditController) state for a decoded preset tree.

    Holds what a host shows rather than plays: the preset name, author and
    description, macro names and their preset values, clip names, UI panel
    settings, and each FX's preset values (what a double-click resets to).
    Without it, Serum in a DAW shows "Init" and default macro names. Text
    valued FX fields are left out of the reset values, which Serum then takes
    from its own defaults. ``preset_path``, if given, is stored as an absolute
    ``selectedPresetPath``; for a user preset that includes the home
    directory, so leave it out of a project that will be shared. Uses the tree's own version markers, as
    :func:`build_processor_state` does. The tree is not modified.
    """
    if not isinstance(preset_dict, dict):
        raise TypeError(f"preset tree must be a dict, got {type(preset_dict).__name__}")
    product, version = _version_markers(preset_dict)
    for key in ("presetName", "presetAuthor", "presetDescription", "presetComment"):
        if key in preset_dict and not isinstance(preset_dict[key], str):
            raise ValueError(f"preset {key} {preset_dict[key]!r:.60} is not a string")
    # Nothing below modifies these values; cbor2 only reads them.
    state: dict[str, Any] = {k: preset_dict[k] for k in _CONTROLLER_TOPLEVEL_KEYS if k in preset_dict}
    for key, module in preset_dict.items():
        if not isinstance(module, dict):
            continue
        if _matches_prefix_with_index(key, "Macro"):
            macro = _controller_macro(module)
            if macro:       # Serum leaves out a macro with no name or value
                state[key] = macro
        elif _matches_prefix_with_index(key, "MidiClip"):
            state[key] = {k: module[k] for k in _PRESET_ONLY_SUBKEYS_BY_PREFIX["MidiClip"] if k in module}
        elif _matches_prefix_with_index(key, "ArpClip"):
            state[key] = {}
        elif _matches_prefix_with_index(key, "FXRack"):
            state[key] = _controller_fx_rack(module)
        elif _matches_prefix_with_index(key, "LFO"):
            name = module.get("curveDisplayName")
            state[key] = None if name is None else {"curveDisplayName": name}
        elif _matches_prefix_with_index(key, "VoicePanel"):
            state[key] = {"displayName": module.get("displayName", "")}
    state.update(
        component="controller",
        presetComment=preset_dict.get("presetComment", ""),
        presetHasBeenEdited=False,
        productVersion=product,
        version=version,
    )
    if preset_path is not None:
        # Serum resolves it from the host's working directory, not the set's.
        state["selectedPresetPath"] = os.path.abspath(os.fspath(preset_path))
    raw = cbor2.dumps(state)
    compressed = zstandard.ZstdCompressor().compress(raw)
    meta = {
        "component": "controller",
        "hash": hashlib.md5(compressed, usedforsecurity=False).hexdigest(),
        "presetAuthor": state.get("presetAuthor", ""),
        "presetComment": state["presetComment"],
        "presetDescription": state.get("presetDescription", ""),
        "presetName": state.get("presetName", ""),
        "product": "Serum2",
        "productVersion": product,
        "url": "https://xferrecords.com/",
        "vendor": "Xfer Records",
        "version": version,
    }
    return wrap_xferjson_precompressed(meta, compressed, uncompressed_size=len(raw))


def _controller_macro(module: dict) -> dict:
    out: dict[str, Any] = {}
    if "name" in module:
        out["name"] = module["name"]
    plain = module.get("plainParams")
    value = plain.get("kParamValue") if isinstance(plain, dict) else None
    if _is_number(value):
        if not 0 <= value <= 100:
            raise ValueError(f"macro value {value!r:.60} is outside 0..100")
        out["default"] = value / 100    # the macro's preset value, 0 to 1
    return out


def _controller_fx_rack(module: dict) -> dict:
    out: dict[str, Any] = {"displayName": module.get("displayName", "")}
    fxs = module.get("FX")
    if isinstance(fxs, list) and fxs:
        # One entry per slot, even a malformed one, so later FX keep their index.
        out["FX"] = [_controller_fx(fx if isinstance(fx, dict) else {}) for fx in fxs]
    return out


def _controller_fx(fx: dict) -> dict:
    # Each FX entry holds one FX module (`FXDelay`, ...) next to `type` and
    # `kUIParamMixOrGain`. Serum rejects a string in defaultPlainValues;
    # booleans go in as 1.0 / 0.0.
    plain: Any = {}
    for name, value in fx.items():
        if name.startswith("FX") and isinstance(value, dict):
            plain = value.get("plainParams", {})
    values = {k: float(v) for k, v in plain.items()
              if isinstance(v, bool) or (_is_number(v) and math.isfinite(v))} if isinstance(plain, dict) else {}
    return {"defaultPlainValues": values, "kUIParamMixOrGain": fx.get("kUIParamMixOrGain", 0.0)}


def _is_number(value: Any) -> bool:
    # Same rule as params.py: any real number except a bool.
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


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
