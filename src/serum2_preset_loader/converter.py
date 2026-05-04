"""Translate the CBOR inside a .SerumPreset into Serum 2's processor-state shape.

A .SerumPreset's CBOR is a "delta from defaults" structure: untouched modules
have ``plainParams: "default"`` and the file carries extra UI / library /
metadata fields. Serum 2's runtime processor state CBOR (what Serum writes via
VST3 IComponent::getState) uses fully expanded ``plainParams: {}`` for unchanged
modules, omits the UI/metadata, and adds a couple of processor-only fields.

Mapping the two shapes is a small, mostly mechanical transform.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any

import cbor2
import zstandard

from .wrappers import build_juce_vst3_state, unwrap_xferjson, wrap_xferjson


# Top-level keys that exist only in the preset shape.
_PRESET_ONLY_TOPLEVEL_KEYS: frozenset[str] = frozenset({
    # UI panels and library scratch state
    "ClipPlayer", "Filter", "SerumGUI",
    "arpBankDisplayName", "clipBankDisplayName",
    # Library template lists (alternative oscillator types)
    "GranularOsc", "MultiSampleOsc", "Osc", "SpectralOsc", "WTOsc",
    # Preset-file metadata (also lives in the JSON header)
    "fileType", "presetName", "presetAuthor", "presetDescription",
})

# Top-level keys the processor adds that the preset doesn't carry.
_PROCESSOR_EXTRA_TOPLEVEL: dict[str, Any] = {
    "component": "processor",
    "killEnvsGracefullyCompat": True,
}

# Current Serum 2 processor-state version markers (observed in 2.1.4).
# `version` is intentionally a Python float — Serum's getState writes a
# CBOR major-7 (float) value here, not an integer. cbor2 preserves the
# distinction on encode, so emitting an int would produce a CBOR document
# that doesn't byte-match what Serum produces.
_PROCESSOR_PRODUCT_VERSION = "2.1.4"
_PROCESSOR_FORMAT_VERSION = 10.0

# Sub-keys that exist only in the preset shape, indexed by module-name prefix.
# Using a blacklist (rather than whitelisting "clip"/"plainParams") so that any
# new processor-side fields a future Serum version adds will pass through
# instead of being silently dropped.
_PRESET_ONLY_SUBKEYS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "Macro":   ("name",),
    "FXRack":  ("displayName",),
    "MidiClip": (
        "displayLength_Beats", "gridWidth_Beats", "gridYOffset_Rows",
        "laneTabs", "name",
    ),
}

_PRESET_ONLY_SUBKEYS_BY_MODULE: dict[str, tuple[str, ...]] = {
    "PitchQuantizer0": ("scaleName",),
}


def _expand_default_plainparams_inplace(obj: Any) -> None:
    """Recursively replace ``plainParams: "default"`` with ``plainParams: {}``.

    Mutates dicts and lists in place. Caller is responsible for working on a
    copy if the input must not be modified.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "plainParams" and v == "default":
                obj[k] = {}
            else:
                _expand_default_plainparams_inplace(v)
    elif isinstance(obj, list):
        for item in obj:
            _expand_default_plainparams_inplace(item)


def _strip_preset_only_subkeys(state: dict) -> None:
    """Remove sub-keys that exist in the preset shape but not the processor shape."""
    for key, value in state.items():
        if not isinstance(value, dict):
            continue
        for prefix, drop_keys in _PRESET_ONLY_SUBKEYS_BY_PREFIX.items():
            if key.startswith(prefix):
                for dk in drop_keys:
                    value.pop(dk, None)
        for dk in _PRESET_ONLY_SUBKEYS_BY_MODULE.get(key, ()):
            value.pop(dk, None)


def preset_cbor_to_processor_cbor(preset_cbor: dict) -> dict:
    """Pure dict-to-dict transformation. Does not touch wrappers."""
    state = copy.deepcopy(preset_cbor)
    for k in _PRESET_ONLY_TOPLEVEL_KEYS:
        state.pop(k, None)
    _expand_default_plainparams_inplace(state)
    _strip_preset_only_subkeys(state)
    state.update(_PROCESSOR_EXTRA_TOPLEVEL)
    arp = state.get("Arp0")
    if isinstance(arp, dict):
        arp.setdefault("activeClip", 0)
    state["productVersion"] = _PROCESSOR_PRODUCT_VERSION
    state["version"] = _PROCESSOR_FORMAT_VERSION
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
        "hash": hashlib.md5(compressed_cbor).hexdigest(),
        "product": "Serum2",
        "productVersion": _PROCESSOR_PRODUCT_VERSION,
        "url": "https://xferrecords.com/",
        "vendor": "Xfer Records",
        "version": _PROCESSOR_FORMAT_VERSION,
    }


def convert_preset_bytes(preset_bytes: bytes) -> bytes:
    """Convert raw .SerumPreset bytes to a DawDreamer-loadable VST3 state blob."""
    _preset_meta, _format_ver, preset_cbor_bytes = unwrap_xferjson(preset_bytes)
    preset_dict = cbor2.loads(preset_cbor_bytes)
    proc_dict = preset_cbor_to_processor_cbor(preset_dict)
    proc_cbor = cbor2.dumps(proc_dict)

    # `wrap_xferjson` re-compresses, so to compute the hash we have to compress
    # once here too. Done with the same default Zstd settings used by
    # `wrap_xferjson`, so the bytes match.
    compressed_cbor = zstandard.ZstdCompressor().compress(proc_cbor)
    proc_meta = _build_processor_metadata(compressed_cbor)
    icomponent = wrap_xferjson(proc_meta, proc_cbor)
    return build_juce_vst3_state(icomponent)


def convert_preset_file(preset_path: str) -> bytes:
    """Read a .SerumPreset from disk and return the VST3 state blob."""
    with open(preset_path, "rb") as f:
        return convert_preset_bytes(f.read())
