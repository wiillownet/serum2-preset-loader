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
from typing import Any

import cbor2

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
_PROCESSOR_PRODUCT_VERSION = "2.1.4"
_PROCESSOR_FORMAT_VERSION = 10.0


def _expand_default_plainparams(obj: Any) -> Any:
    """Recursively replace ``plainParams: "default"`` with ``plainParams: {}``."""
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if k == "plainParams" and v == "default":
                out[k] = {}
            else:
                out[k] = _expand_default_plainparams(v)
        return out
    if isinstance(obj, list):
        return [_expand_default_plainparams(x) for x in obj]
    return obj


def _strip_preset_only_subkeys(state: dict) -> dict:
    """Remove sub-keys that exist in the preset shape but not the processor shape."""
    for i in range(8):
        macro = state.get(f"Macro{i}")
        if isinstance(macro, dict):
            macro.pop("name", None)
    for i in range(3):
        rack = state.get(f"FXRack{i}")
        if isinstance(rack, dict):
            rack.pop("displayName", None)
    for i in range(12):
        clip = state.get(f"MidiClip{i}")
        if isinstance(clip, dict):
            for k in list(clip.keys()):
                if k not in ("clip", "plainParams"):
                    clip.pop(k, None)
    pq = state.get("PitchQuantizer0")
    if isinstance(pq, dict):
        pq.pop("scaleName", None)
    return state


def preset_cbor_to_processor_cbor(preset_cbor: dict) -> dict:
    """Pure dict-to-dict transformation. Does not touch wrappers."""
    state = copy.deepcopy(preset_cbor)
    for k in _PRESET_ONLY_TOPLEVEL_KEYS:
        state.pop(k, None)
    state = _expand_default_plainparams(state)
    state = _strip_preset_only_subkeys(state)
    state.update(_PROCESSOR_EXTRA_TOPLEVEL)
    arp = state.get("Arp0")
    if isinstance(arp, dict):
        arp.setdefault("activeClip", 0)
    state["productVersion"] = _PROCESSOR_PRODUCT_VERSION
    state["version"] = _PROCESSOR_FORMAT_VERSION
    return state


def convert_preset_bytes(preset_bytes: bytes) -> bytes:
    """Convert raw .SerumPreset bytes to a DawDreamer-loadable VST3 state blob."""
    preset_meta, _format_ver, preset_cbor_bytes = unwrap_xferjson(preset_bytes)
    preset_dict = cbor2.loads(preset_cbor_bytes)
    proc_dict = preset_cbor_to_processor_cbor(preset_dict)
    proc_cbor = cbor2.dumps(proc_dict)

    proc_meta = {
        "component": "processor",
        "hash": preset_meta.get("hash", ""),
        "product": "Serum2",
        "productVersion": _PROCESSOR_PRODUCT_VERSION,
        "url": "https://xferrecords.com/",
        "vendor": "Xfer Records",
        "version": _PROCESSOR_FORMAT_VERSION,
    }
    icomponent = wrap_xferjson(proc_meta, proc_cbor)
    return build_juce_vst3_state(icomponent)


def convert_preset_file(preset_path: str) -> bytes:
    """Read a .SerumPreset from disk and return the VST3 state blob."""
    with open(preset_path, "rb") as f:
        return convert_preset_bytes(f.read())
