"""Tests for the measured parameter table (params.py)."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from serum2_preset_loader import (
    PARAMS,
    convert_preset_tree,
    decode_preset_bytes,
    decode_preset_file,
    encode_preset,
    get_param,
    set_param,
)

FIXTURES = Path(__file__).parent / "fixtures"
MEASURED = json.loads((FIXTURES / "serum2_subset_map.json").read_text(encoding="utf-8"))
PRESET = FIXTURES / "test_preset.SerumPreset"
CONTINUOUS = [p for p in PARAMS.values() if not p.discrete]


def empty_tree() -> dict:
    """The modules the table touches, all at defaults."""
    return {
        "Oscillator0": {"plainParams": {}, "WTOsc0": {"plainParams": {}}},
        "VoiceFilter0": {"plainParams": {}},
        "Env0": {"plainParams": {}},
    }


def field(tree: dict, name: str) -> dict:
    node = tree
    for key in PARAMS[name].path[:-1]:
        node = node[key]
    return node


@pytest.fixture
def preset():
    return decode_preset_file(PRESET)


# ─── against the probe ─────────────────────────────────────────────────────

def test_table_matches_measured_params():
    by_index = {p.daw_index: p for p in PARAMS.values()}
    assert sorted(by_index) == sorted(m["index"] for m in MEASURED["params"])
    for m in MEASURED["params"]:
        p = by_index[m["index"]]
        assert p.daw_name == m["name"]
        assert ".".join(p.path) == m["path"]
        assert p.default_norm == m["default_norm"]
        assert p.discrete == m["discrete"]


@pytest.mark.parametrize("measured", MEASURED["params"], ids=lambda m: m["name"])
def test_to_stored_matches_every_sample(measured):
    p = next(p for p in PARAMS.values() if p.daw_index == measured["index"])
    for norm, stored, _text in measured["samples"]:
        # null: Serum omitted the field, so the value equalled the default.
        expected = p.default_stored if stored is None else stored
        assert math.isclose(p.to_stored(norm), expected, rel_tol=1e-6, abs_tol=1e-9), norm


def test_defaults_were_absent_in_baseline():
    assert not any(m["in_baseline"] for m in MEASURED["params"])


# ─── conversions ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("param", CONTINUOUS, ids=lambda p: p.name)
def test_continuous_round_trip(param):
    for i in range(101):
        n = i / 100
        assert math.isclose(param.to_norm(param.to_stored(n)), n, rel_tol=1e-9, abs_tol=1e-12)


@pytest.mark.parametrize("voices", range(1, 17))
def test_unison_get_set_get_is_stable(voices):
    tree = empty_tree()
    field(tree, "osc_a_unison")["kParamUnison"] = float(voices)
    p = PARAMS["osc_a_unison"]
    n = get_param(tree, "osc_a_unison")
    assert p.to_stored(n) == voices
    set_param(tree, "osc_a_unison", n)
    # 1 voice is the default, so the field is deleted and reads back as
    # default_norm (0) rather than 1/32: a different norm, the same count.
    assert p.to_stored(get_param(tree, "osc_a_unison")) == voices


def test_stored_values_serum_could_not_store_are_rejected():
    with pytest.raises(ValueError, match="outside"):
        PARAMS["env1_attack"].to_norm(-1.0)
    with pytest.raises(ValueError, match="whole voice"):
        PARAMS["osc_a_unison"].to_norm(2.5)
    with pytest.raises(ValueError, match="not a number"):
        PARAMS["filter1_cutoff"].to_norm("0.5")


# ─── get_param / set_param ─────────────────────────────────────────────────

@pytest.mark.parametrize("name", list(PARAMS))
def test_absent_field_reads_as_default(name):
    assert get_param(empty_tree(), name) == PARAMS[name].default_norm


@pytest.mark.parametrize("name", list(PARAMS))
def test_set_at_default_removes_field(name):
    tree = empty_tree()
    key = PARAMS[name].path[-1]
    field(tree, name)[key] = 123.0
    set_param(tree, name, PARAMS[name].default_norm)
    assert key not in field(tree, name)
    assert field(tree, name) == {}


@pytest.mark.parametrize("name", list(PARAMS))
def test_set_elsewhere_writes_a_float(name):
    tree = empty_tree()
    norm = 0.75 if PARAMS[name].default_norm < 0.75 else 0.25
    set_param(tree, name, norm)
    stored = field(tree, name)[PARAMS[name].path[-1]]
    assert type(stored) is float
    assert stored == PARAMS[name].to_stored(norm)


def test_default_plainparams_string_is_replaced():
    tree = empty_tree()
    tree["Env0"]["plainParams"] = "default"
    assert get_param(tree, "env1_decay") == 0.5
    set_param(tree, "env1_decay", 0.25)
    assert tree["Env0"]["plainParams"] == {"kParamDecay": 32 * 0.25 ** 5}


def test_set_on_real_preset_survives_encode_and_decode(preset):
    header, tree = preset
    values = {"osc_a_unison": 0.5, "osc_a_detune": 0.3, "osc_a_wt_pos": 0.4,
              "filter1_cutoff": 0.25, "filter1_reso": 0.6, "env1_attack": 0.25,
              "env1_decay": 0.35, "env1_sustain": 0.7, "env1_release": 0.45}
    for name, norm in values.items():
        set_param(tree, name, norm)
    _h, tree2 = decode_preset_bytes(encode_preset(header, tree))
    for name, norm in values.items():
        got = get_param(tree2, name)
        if PARAMS[name].discrete:
            assert PARAMS[name].to_stored(got) == PARAMS[name].to_stored(norm)
        else:
            # Floats that fit 32 bits are written as float32.
            assert math.isclose(got, norm, rel_tol=1e-6), name


def test_set_on_real_preset_converts(preset):
    _header, tree = preset
    set_param(tree, "filter1_cutoff", 0.25)
    set_param(tree, "env1_attack", 0.25)
    assert convert_preset_tree(tree) != convert_preset_tree(decode_preset_file(PRESET)[1])


def test_set_keeps_other_fields(preset):
    _header, tree = preset
    before = copy.deepcopy(tree["VoiceFilter0"]["plainParams"])
    set_param(tree, "filter1_reso", 0.5)
    after = tree["VoiceFilter0"]["plainParams"]
    assert after == {**before, "kParamReso": 50.0}


def test_readme_example_runs(tmp_path, monkeypatch):
    import re
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("## Parameters"):readme.index("## Targeting a different Serum build")]
    (block,) = re.findall(r"```python\n(.*?)```", section, re.S)
    (tmp_path / "My Preset.SerumPreset").write_bytes(PRESET.read_bytes())
    monkeypatch.chdir(tmp_path)
    exec(compile(block, "README", "exec"), {})
    _h, tree = decode_preset_file(tmp_path / "My Preset (edited).SerumPreset")
    assert math.isclose(get_param(tree, "filter1_cutoff"), 0.25)
    assert math.isclose(get_param(tree, "env1_attack"), 0.25, rel_tol=1e-6)


# ─── errors ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("norm", [-0.01, 1.01, math.nan, math.inf, "0.5", True])
def test_bad_norm_rejected(norm):
    tree = empty_tree()
    with pytest.raises(ValueError, match="filter1_cutoff"):
        set_param(tree, "filter1_cutoff", norm)
    assert tree == empty_tree()


def test_unknown_name():
    with pytest.raises(KeyError, match="no_such_param"):
        set_param(empty_tree(), "no_such_param", 0.5)
    with pytest.raises(KeyError, match="no_such_param"):
        get_param(empty_tree(), "no_such_param")


def test_missing_module_named_and_not_created():
    tree = empty_tree()
    del tree["Oscillator0"]["WTOsc0"]
    with pytest.raises(KeyError, match=r"Oscillator0\.WTOsc0"):
        set_param(tree, "osc_a_wt_pos", 0.5)
    with pytest.raises(KeyError, match=r"Oscillator0\.WTOsc0"):
        get_param(tree, "osc_a_wt_pos")
    assert "WTOsc0" not in tree["Oscillator0"]


def test_params_is_read_only():
    with pytest.raises(TypeError):
        PARAMS["x"] = PARAMS["env1_attack"]
