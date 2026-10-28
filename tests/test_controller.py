"""Tests for build_processor_state and build_controller_state."""
from __future__ import annotations

import copy
import hashlib
import os
import struct
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path

import cbor2
import pytest

from serum2_preset_loader import (
    build_controller_state,
    InvalidFXTypeError,
    build_processor_state,
    convert_preset_tree,
    decode_preset_file,
    juce_memoryblock_b64decode,
    unwrap_xferjson,
)

PRESET = Path(__file__).parent / "fixtures" / "test_preset.SerumPreset"


def _tree() -> dict:
    return {
        "presetName": "Tiny", "presetAuthor": "Tester", "presetDescription": "desc",
        "product": "Serum2", "url": "https://xferrecords.com/", "vendor": "Xfer Records",
        "productVersion": "2.0.12", "version": 4.0, "fileType": "SerumPreset",
        "SerumGUI": {"kUIParamShowKeyboard": 1.0}, "ClipPlayer": {}, "Filter": {"kUIParamMixOrGain": 0.0},
        "WTOsc": [{"kUIParamWTOverviewMouseTag": 0.0}],
        "arpBankDisplayName": "", "clipBankDisplayName": "",
        "Macro0": {"name": "OPEN", "plainParams": "default"},
        "Macro1": {"name": "BODY", "plainParams": {"kParamValue": 25.0}},
        "Macro2": {"plainParams": {}},
        "MidiClip0": {"clip": {"notes": []}, "plainParams": {}, "name": "Clip", "gridWidth_Beats": 4.0},
        "ArpClip0": {"clip": {}, "plainParams": {}},
        "LFO0": {"curveDisplayName": "Custom", "curveData": {}, "plainParams": {}},
        "LFO1": {"curveData": {}, "plainParams": {}},
        "LFOPointModBus0": {"plainParams": {}},
        "VoicePanel0": {"displayName": "", "plainParams": {}},
        "FXRack0": {"displayName": "RACK", "plainParams": {}, "FX": [
            {"FXDelay": {"plainParams": {"kParamWet": 22.0, "kParamMode": "Normal", "kParamHQ": True}},
             "type": 1, "kUIParamMixOrGain": 0.5},
            {"FXReverb": {"plainParams": "default"}, "type": 2},
        ]},
        "FXRack1": {"displayName": "", "plainParams": {}, "FX": []},
        "Env0": {"plainParams": {"kParamAttack": 1.0}},
    }


def _decode(blob: bytes) -> tuple[dict, dict]:
    meta, version, cbor = unwrap_xferjson(blob)
    assert version == 2
    return meta, cbor2.loads(cbor)


def test_controller_holds_names_and_ui_not_sound():
    meta, state = _decode(build_controller_state(_tree()))
    assert state["component"] == meta["component"] == "controller"
    assert (state["presetName"], state["presetAuthor"], state["presetDescription"]) == ("Tiny", "Tester", "desc")
    assert state["SerumGUI"] == {"kUIParamShowKeyboard": 1.0}
    assert state["presetHasBeenEdited"] is False and state["presetComment"] == ""
    for gone in ("Env0", "fileType", "LFOPointModBus0", "selectedPresetPath"):
        assert gone not in state


def test_controller_module_shapes():
    _meta, state = _decode(build_controller_state(_tree()))
    assert state["Macro0"] == {"name": "OPEN"}
    assert state["Macro1"] == {"name": "BODY", "default": 0.25}
    assert "Macro2" not in state                      # no name, no value
    assert state["MidiClip0"] == {"name": "Clip", "gridWidth_Beats": 4.0}
    assert state["ArpClip0"] == {}
    assert state["LFO0"] == {"curveDisplayName": "Custom"}
    assert state["LFO1"] is None
    assert state["VoicePanel0"] == {"displayName": ""}
    assert state["FXRack1"] == {"displayName": ""}
    assert state["FXRack0"] == {"displayName": "RACK", "FX": [
        # text fields are left out (Serum rejects a string here); booleans become floats
        {"defaultPlainValues": {"kParamWet": 22.0, "kParamHQ": 1.0}, "kUIParamMixOrGain": 0.5},
        {"defaultPlainValues": {}, "kUIParamMixOrGain": 0.0},
    ]}


def test_controller_metadata_and_versions():
    blob = build_controller_state(_tree(), "/some/Tiny.SerumPreset")
    meta, state = _decode(blob)
    assert state["selectedPresetPath"] == os.path.abspath("/some/Tiny.SerumPreset")
    _m, rel = _decode(build_controller_state(_tree(), Path("rel") / "Tiny.SerumPreset"))
    assert rel["selectedPresetPath"] == str(Path.cwd() / "rel" / "Tiny.SerumPreset")
    _m, home = _decode(build_controller_state(_tree(), "~/Tiny.SerumPreset"))
    assert home["selectedPresetPath"] == str(Path.home() / "Tiny.SerumPreset")
    assert (meta["productVersion"], meta["version"]) == (state["productVersion"], state["version"]) == ("2.0.12", 4.0)
    assert type(state["version"]) is float
    json_len = struct.unpack("<Q", blob[9:17])[0]
    assert meta["hash"] == hashlib.md5(blob[17 + json_len + 8:], usedforsecurity=False).hexdigest()
    assert (meta["presetName"], meta["product"]) == ("Tiny", "Serum2")


def test_controller_does_not_mutate_tree():
    tree = _tree()
    before = copy.deepcopy(tree)
    build_controller_state(tree, "/x")
    assert tree == before


@pytest.mark.parametrize("key, value", [
    ("presetName", b"Tiny"), ("presetAuthor", 5), ("presetDescription", None), ("presetComment", ["x"]),
])
def test_controller_rejects_non_string_text(key, value):
    tree = _tree()
    tree[key] = value
    with pytest.raises(ValueError, match=key):
        build_controller_state(tree)


@pytest.mark.parametrize("value", [10**400, -1.0, 100.5, float("nan")])
def test_controller_rejects_bad_macro_value(value):
    tree = _tree()
    tree["Macro1"]["plainParams"]["kParamValue"] = value
    with pytest.raises(ValueError, match="macro value"):
        build_controller_state(tree)


def test_malformed_fx_entry_keeps_later_fx_in_place():
    tree = _tree()
    tree["FXRack0"]["FX"].insert(0, {"type": 0})     # no FX module
    _meta, state = _decode(build_controller_state(tree))
    fx = state["FXRack0"]["FX"]
    assert fx[0] == {"defaultPlainValues": {}, "kUIParamMixOrGain": 0.0}
    assert fx[1]["defaultPlainValues"]["kParamWet"] == 22.0


def test_controller_rejects_bad_input():
    with pytest.raises(TypeError):
        build_controller_state([])
    tree = _tree()
    del tree["version"]
    with pytest.raises(ValueError, match="set both or neither"):
        build_controller_state(tree)


def test_processor_state_is_what_convert_wraps():
    _h, tree = decode_preset_file(PRESET)
    blob = convert_preset_tree(tree)
    xml = ET.fromstring(blob[8:8 + struct.unpack("<II", blob[:8])[1]])
    icomponent = juce_memoryblock_b64decode(xml.find("IComponent").text)
    assert xml.find("IEditController") is None
    processor = build_processor_state(tree)
    assert _decode(processor)[1] == _decode(icomponent)[1]
    assert _decode(processor)[0]["component"] == "processor"
    with pytest.raises(TypeError):
        build_processor_state("not a tree")


def test_controller_on_real_preset():
    _h, tree = decode_preset_file(PRESET)
    meta, state = _decode(build_controller_state(tree))
    assert meta["presetName"] == state["presetName"] == "test_preset"
    assert state["LFO0"] == {"curveDisplayName": tree["LFO0"]["curveDisplayName"]}
    assert not any(k.startswith("Macro") for k in state)   # all eight are unnamed


def test_readme_example_runs(tmp_path, monkeypatch):
    import re
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("## State for a DAW project"):readme.index("## Untrusted files")]
    (block,) = re.findall(r"```python\n(.*?)```", section, re.S)
    (tmp_path / "My Preset.SerumPreset").write_bytes(PRESET.read_bytes())
    monkeypatch.chdir(tmp_path)
    scope: dict = {}
    exec(compile(block, "README", "exec"), scope)
    assert _decode(scope["processor"])[0]["component"] == "processor"
    assert "selectedPresetPath" not in _decode(scope["controller"])[1]


# Serum's own saves: one entry per CC, a CC can drive several parameters; a
# later duplicate ccNum wins and an out-of-range one is skipped, so pass both
# through for Serum to resolve.
MIDI_MAP = [
    {"ccNum": 15, "paramIDs": [7001000]},
    {"ccNum": 14, "paramIDs": [7000000, 7002000, 1004006]},
    {"ccNum": 15, "paramIDs": [7003000]},
    {"ccNum": 200, "paramIDs": [7004000]},
]
PROXIES = {"0": {"fxID": 1, "paramIndex": 10}, "15": {"fxID": 0, "paramIndex": 2}}


def test_midimap_copied():
    tree = _tree()
    tree["midiMap"] = copy.deepcopy(MIDI_MAP)
    _meta, state = _decode(build_controller_state(tree))
    assert state["midiMap"] == MIDI_MAP
    assert "proxyParams" not in state["FXRack0"]


def test_proxies_copied():
    tree = _tree()
    tree["FXRack0"]["proxyParams"] = copy.deepcopy(PROXIES)
    tree["FXRack1"]["FX"] = [{"type": 0}]
    tree["FXRack1"]["proxyParams"] = {"3": {"fxID": 0, "paramIndex": 0}}
    _meta, state = _decode(build_controller_state(tree))
    assert state["FXRack0"]["proxyParams"] == PROXIES
    assert list(state["FXRack0"]["proxyParams"]) == ["0", "15"]
    assert state["FXRack1"]["proxyParams"] == {"3": {"fxID": 0, "paramIndex": 0}}
    assert "midiMap" not in state


def test_neither_left_out():
    # Serum's own save leaves both keys out when nothing is mapped
    _meta, state = _decode(build_controller_state(_tree()))
    assert "midiMap" not in state
    assert not any("proxyParams" in v for k, v in state.items() if k.startswith("FXRack"))


def test_roundtrip():
    from serum2_preset_loader import decode_preset_bytes, encode_preset
    header, tree = decode_preset_file(PRESET)
    tree["midiMap"] = copy.deepcopy(MIDI_MAP)
    tree["FXRack0"]["FX"] = _tree()["FXRack0"]["FX"]
    tree["FXRack0"]["proxyParams"] = copy.deepcopy(PROXIES)
    _h, again = decode_preset_bytes(encode_preset(header, tree))
    _meta, state = _decode(build_controller_state(again))
    assert state["midiMap"] == again["midiMap"] == MIDI_MAP
    assert state["FXRack0"]["proxyParams"] == again["FXRack0"]["proxyParams"] == PROXIES


# proxyParams validity (serum-spec 10 section 16.9). _tree()'s FXRack0 holds
# two FX: type 1 (Flanger, 9 parameters) and type 2 (Phaser, 12).
FX_COUNTS = [11, 9, 12, 12, 14, 35, 16, 10, 13, 10, 15, 12, 11, 7, 10, 6]


def _proxies(proxies, fx=None):
    """FXRack0's proxyParams in the built controller state (None if left out)."""
    tree = _tree()
    if fx is not None:
        tree["FXRack0"]["FX"] = fx
    tree["FXRack0"]["proxyParams"] = proxies
    _meta, state = _decode(build_controller_state(tree))
    return state["FXRack0"].get("proxyParams")


def _kept(proxies, fx=None):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        return _proxies(proxies, fx)


def _dropped(proxies, match, fx=None):
    with pytest.warns(UserWarning, match=match):
        return _proxies(proxies, fx)


GOOD = {"fxID": 1, "paramIndex": 3}


@pytest.mark.parametrize("proxies", [[GOOD], "x", 1, None], ids=["list", "str", "int", "null"])
def test_proxies_not_a_map_ignored(proxies):
    assert _dropped(proxies, "FXRack0 proxyParams dropped: .*not a map, which Serum ignores") is None


@pytest.mark.parametrize("slot", ["16", "-1", "00", " 1", "1.0", "a"])
def test_proxy_slot_not_0_to_15_ignored(slot):
    assert _dropped({slot: GOOD, "4": GOOD}, f"slot '{slot}' dropped: not one of") == {"4": GOOD}


def test_proxies_on_empty_rack_ignored():
    assert _dropped({"0": GOOD}, "slot '0' dropped: the rack has no FX", fx=[]) == {}


@pytest.mark.parametrize("entry", [[1, 3], 1, "1", None], ids=["list", "int", "str", "null"])
def test_proxy_entry_not_a_map_crashes(entry):
    assert _dropped({"0": entry, "1": GOOD}, "slot '0' dropped: .*not a map, which crashes") == {"1": GOOD}


@pytest.mark.parametrize("field", ["fxID", "paramIndex"])
@pytest.mark.parametrize("value", ["missing", None, "1", {}, [], float("nan"), float("inf"), 2 ** 64],
                         ids=["missing", "null", "str", "map", "list", "nan", "inf", "bignum"])
def test_proxy_value_not_a_number_crashes(field, value):
    entry = dict(GOOD)
    if value == "missing":
        del entry[field]
    else:
        entry[field] = value
    assert _dropped({"0": entry}, f"slot '0' dropped: {field} .*, which crashes") == {}


@pytest.mark.parametrize("fx_id", [-1, 2, 1002, 2 ** 31, 1e300], ids=["neg", "len", "big", "2^31", "1e300"])
def test_proxy_fxid_out_of_range_crashes(fx_id):
    assert _dropped({"0": {"fxID": fx_id, "paramIndex": 0}}, "fxID .* is outside 0 to 1") == {}


@pytest.mark.parametrize("fx_type", range(16))
def test_proxy_paramindex_limit_per_type(fx_type):
    count = FX_COUNTS[fx_type]
    fx = [{"type": fx_type}]
    last = {"0": {"fxID": 0, "paramIndex": count - 1}}
    assert _kept(last, fx) == last
    for index in (count, count + 1, -1, 1002):
        assert _dropped({"0": {"fxID": 0, "paramIndex": index}}, "paramIndex .* is outside", fx) == {}


@pytest.mark.parametrize("fx_id,index", [
    (2 ** 32, 0), (2 ** 32 + 1, 8), (1.9, 11), (-0.5, 0), (True, 11), (False, 8), (1, 11.99),
], ids=["2^32", "2^32+1", "float", "neg-float", "true", "false", "float-index"])
def test_proxy_numbers_read_as_serum_does(fx_id, index):
    proxies = {"0": {"fxID": fx_id, "paramIndex": index}}
    kept = _kept(copy.deepcopy(proxies))
    assert kept == proxies
    # Stored as they were, not normalized: 1.9 stays a float, True a bool.
    assert [type(v) for v in kept["0"].values()] == [type(fx_id), type(index)]


def test_proxy_slots_may_share_a_target():
    proxies = {"0": GOOD, "7": GOOD, "15": GOOD}
    assert _kept(copy.deepcopy(proxies)) == proxies


def test_proxies_mixed_keep_valid_in_order():
    proxies = {"9": GOOD, "16": GOOD, "2": {"fxID": 5, "paramIndex": 0}, "1": {"fxID": 0, "paramIndex": 8}}
    with pytest.warns(UserWarning) as caught:
        kept = _proxies(proxies)
    assert list(kept) == ["9", "1"]
    assert len(caught) == 2


@pytest.mark.parametrize("fx", [{"FXDelay": {}}, {"type": None}, {"type": 16}, {"type": -1},
                                {"type": "2"}, None, 3],
                         ids=["missing", "null", "16", "neg", "str", "entry-null", "entry-int"])
@pytest.mark.parametrize("build", [build_controller_state, build_processor_state, convert_preset_tree],
                         ids=["controller", "processor", "convert"])
def test_fx_without_valid_type_raises(fx, build):
    tree = _tree()
    tree["FXRack0"]["FX"].insert(1, fx)    # with or without proxyParams
    with pytest.raises(InvalidFXTypeError, match=r"FXRack0 FX\[1\] has "):
        build(tree)


def test_fx_type_bounds_accepted():
    tree = _tree()
    tree["FXRack0"]["FX"] = [{"type": 0}, {"type": 15}]
    build_processor_state(tree)
    _meta, state = _decode(build_controller_state(tree))
    assert len(state["FXRack0"]["FX"]) == 2


def test_invalid_fx_type_is_a_value_error():
    assert issubclass(InvalidFXTypeError, ValueError)
