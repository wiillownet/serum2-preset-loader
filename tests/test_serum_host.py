"""Serum 2's own behaviour behind the controller-state checks, measured in a host.

Each case loads a state into Serum 2 through DawDreamer, in a fresh process,
then plays a short note and saves the state. A case fails when the process
dies or ``load_state`` raises. The states start from Serum's own init save, so
no preset file is needed. Opt-in: set SERUM2_PLUGIN_PATH and install
dawdreamer. Last run 2026-11-25 with Serum 2.1.5 and DawDreamer 0.9.0 on macOS.
"""
import importlib.util
import os
import subprocess
import sys
import warnings
from pathlib import Path

import cbor2
import pytest

from serum2_preset_loader.converter import _FX_PARAM_COUNTS, _serum_int, _valid_proxies

PLUGIN = os.environ.get("SERUM2_PLUGIN_PATH")
pytestmark = pytest.mark.skipif(
    not PLUGIN or importlib.util.find_spec("dawdreamer") is None,
    reason="set SERUM2_PLUGIN_PATH and install dawdreamer",
)
CASE = Path(__file__).with_name("serum_host_case.py")
SRC = str(Path(__file__).resolve().parents[1] / "src")


def host(**spec):
    """What Serum saves after loading the case's state, or None if the load failed."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [SRC, os.environ.get("PYTHONPATH")])))
    p = subprocess.run([sys.executable, str(CASE), PLUGIN, cbor2.dumps(spec).hex()],
                       capture_output=True, text=True, timeout=120, env=env)
    lines = p.stdout.splitlines()
    result = next((ln for ln in lines if ln.startswith("RESULT ")), None)
    if p.returncode == 0 and result:
        return cbor2.loads(bytes.fromhex(result[len("RESULT "):]))
    # A failure before the load is a broken setup, not Serum's answer.
    assert "LOADING" in lines, f"case failed before the load (exit {p.returncode}): {p.stderr[-800:]}"
    return None


CONTROLLER_FX = {"defaultPlainValues": {}, "kUIParamMixOrGain": 0.0}


def rack(fx, proxies=None, proc_proxies=None):
    """A spec whose FXRack0 holds ``fx`` (processor entries) and ``proxies``."""
    proc = {"FX": fx, "plainParams": {}}
    if proc_proxies is not None:
        proc["proxyParams"] = proc_proxies
    ctrl = {"displayName": ""}
    if fx:
        ctrl["FX"] = [CONTROLLER_FX] * len(fx)
    if proxies is not None:
        ctrl["proxyParams"] = proxies
    return {"proc": {"FXRack0": proc}, "ctrl": {"FXRack0": ctrl},
            "read_proc": ["FXRack0"], "read_ctrl": ["FXRack0"]}


def fx_of(*types):
    return [{"type": t} for t in types]


# ─── FX parameter counts ──────────────────────────────────────────────────────

def test_fx_param_counts_match_serums_own_save():
    # Serum re-saves one default per parameter for each FX it creates.
    spec = rack(fx_of(*range(8)))
    spec["proc"]["FXRack1"] = {"FX": fx_of(*range(8, 16)), "plainParams": {}}
    spec["ctrl"]["FXRack1"] = {"displayName": "", "FX": [CONTROLLER_FX] * 8}
    spec["read_ctrl"].append("FXRack1")
    saved = host(**spec)["ctrl"]
    counts = [len(fx["defaultPlainValues"]) for r in ("FXRack0", "FXRack1") for fx in saved[r]["FX"]]
    assert counts == list(_FX_PARAM_COUNTS)


@pytest.mark.parametrize("fx_type", range(16))
def test_fx_param_count_is_where_a_proxy_crashes(fx_type):
    last = _FX_PARAM_COUNTS[fx_type] - 1
    assert host(**rack(fx_of(fx_type), {"0": {"fxID": 0, "paramIndex": last}})) is not None
    assert host(**rack(fx_of(fx_type), {"0": {"fxID": 0, "paramIndex": last + 1}})) is None


# ─── proxyParams: the loader drops "crashes" entries exactly where Serum fails ─

def P(fx_id, index):
    return {"fxID": fx_id, "paramIndex": index}


BAD = P(99, 0)      # fails wherever Serum reads it
PROXY_CASES = {     # FXRack0 holds a Flanger (9 parameters) and a Phaser (12)
    "good": {"0": P(1, 3)},
    "not a map: list": [BAD],
    "not a map: str": "x",
    "slot 16": {"16": BAD},
    "slot -1": {"-1": BAD},
    "slot 00": {"00": BAD},
    "slot a": {"a": BAD},
    "slot 15": {"15": BAD},
    "entry int": {"0": 1},
    "entry list": {"0": [1, 3]},
    "fxID missing": {"0": {"paramIndex": 0}},
    "paramIndex missing": {"0": {"fxID": 0}},
    "fxID str": {"0": P("1", 0)},
    "fxID null": {"0": P(None, 0)},
    "paramIndex str": {"0": P(0, "1")},
    "fxID -1": {"0": P(-1, 0)},
    "fxID 2": {"0": P(2, 0)},
    "paramIndex -1": {"0": P(0, -1)},
    # How Serum reads a number: a float truncates toward zero, an integer keeps
    # its low 32 bits, a bool is 1 or 0.
    "fxID 1.9": {"0": P(1.9, 0)},
    "fxID 2.5": {"0": P(2.5, 0)},
    "fxID -0.5": {"0": P(-0.5, 0)},
    "fxID 2^32+1": {"0": P(2 ** 32 + 1, 0)},
    "fxID 2^32+2": {"0": P(2 ** 32 + 2, 0)},
    "fxID 2^32-1": {"0": P(2 ** 32 - 1, 0)},
    "fxID true": {"0": P(True, 0)},
    "fxID false": {"0": P(False, 0)},
    "paramIndex 8.9": {"0": P(0, 8.9)},
    "paramIndex 9.0": {"0": P(0, 9.0)},
    "paramIndex 2^32+8": {"0": P(0, 2 ** 32 + 8)},
    "paramIndex 2^32+9": {"0": P(0, 2 ** 32 + 9)},
}


def _loader_says_crash(proxies, fx_types):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _valid_proxies("FXRack0", proxies, fx_types)
    return any("crashes the host" in str(w.message) for w in caught)


@pytest.mark.parametrize("proxies", PROXY_CASES.values(), ids=PROXY_CASES.keys())
def test_proxy_dropped_as_a_crash_only_where_serum_fails(proxies):
    failed = host(**rack(fx_of(1, 2), proxies)) is None
    assert failed == _loader_says_crash(proxies, [1, 2])


def test_proxies_on_a_rack_without_fx_are_ignored():
    assert host(**rack([], {"0": BAD})) is not None
    assert not _loader_says_crash({"0": BAD}, [])


def test_proxies_in_the_processor_state_are_not_read():
    saved = host(**rack(fx_of(1), proc_proxies={"0": BAD}))
    assert saved is not None and "proxyParams" not in saved["ctrl"]["FXRack0"]


# ─── FX type ──────────────────────────────────────────────────────────────────

BAD_TYPES = {"missing": None, "16": 16, "-1": -1, "100": 100, "2^31": 2 ** 31}


def _fx(raw):
    return {} if raw is None else {"type": raw}


@pytest.mark.parametrize("raw", BAD_TYPES.values(), ids=BAD_TYPES.keys())
def test_bad_fx_type_at_the_end_of_a_rack_is_dropped(raw):
    saved = host(**rack([{"type": 1}, _fx(raw)]))
    assert [fx["type"] for fx in saved["proc"]["FXRack0"]["FX"]] == [1]


@pytest.mark.parametrize("raw", [None, 16, -1], ids=["missing", "16", "-1"])
def test_bad_fx_type_before_another_fx_crashes(raw):
    assert host(**rack([_fx(raw), {"type": 1}])) is None


@pytest.mark.parametrize("raw", ["1", None], ids=["str", "null"])
def test_fx_type_that_is_not_a_number_fails(raw):
    assert host(**rack([{"type": raw}])) is None


@pytest.mark.parametrize("raw", [15, 15.9, 2 ** 32 + 1, True], ids=["15", "15.9", "2^32+1", "true"])
def test_fx_type_read_like_a_proxy_number(raw):
    saved = host(**rack([{"type": raw}]))
    assert [fx["type"] for fx in saved["proc"]["FXRack0"]["FX"]] == [_serum_int(raw)]


# ─── midiMap ──────────────────────────────────────────────────────────────────

MACROS = [f"Macro{i}" for i in range(8)]
M0, M1 = 7000000, 7001000   # Macro0 and Macro1, as a CC mapped to them shows


def midi(where="ctrl", midi_map=None, cc=()):
    spec = {"cc": [list(c) for c in cc], "read_proc": MACROS, "read_ctrl": ["midiMap"]}
    if midi_map is not None:
        spec[where] = {"midiMap": midi_map}
    saved = host(**spec)
    moved = sorted(k for k in MACROS if (saved["proc"][k].get("plainParams") or {}).get("kParamValue") == 100.0)
    return moved, saved["ctrl"]["midiMap"]


def test_cc_moves_the_mapped_macro():
    assert midi(midi_map=[{"ccNum": 20, "paramIDs": [M0]}], cc=[(20, 127)])[0] == ["Macro0"]
    assert midi(midi_map=[{"ccNum": 20, "paramIDs": [M1]}], cc=[(20, 127)])[0] == ["Macro1"]


def test_midi_map_is_read_from_the_controller_state_only():
    assert midi("proc", [{"ccNum": 20, "paramIDs": [M0]}], cc=[(20, 127)]) == ([], "<absent>")


@pytest.mark.parametrize("first, second", [(M0, M1), (M1, M0)])
def test_later_entry_for_a_cc_wins(first, second):
    moved, saved = midi(midi_map=[{"ccNum": 20, "paramIDs": [first]}, {"ccNum": 20, "paramIDs": [second]}],
                        cc=[(20, 127)])
    assert moved == [f"Macro{(second - M0) // 1000}"]
    assert saved == [{"ccNum": 20, "paramIDs": [second]}]


@pytest.mark.parametrize("entry, kept", [
    ({"ccNum": 128, "paramIDs": [M0]}, False),
    ({"ccNum": -1, "paramIDs": [M0]}, False),
    ({"paramIDs": [M0]}, False),
    ({"ccNum": 0, "paramIDs": [M0]}, True),
    ({"ccNum": 127, "paramIDs": [M0]}, True),
], ids=["128", "-1", "missing", "0", "127"])
def test_cc_outside_0_to_127_is_skipped(entry, kept):
    other = {"ccNum": 21, "paramIDs": [M1]}
    _moved, saved = midi(midi_map=[entry, other])
    assert (entry in saved) == kept and other in saved


def test_empty_midi_map_is_not_saved():
    assert midi(midi_map=[])[1] == "<absent>"


# ─── author lock and MPE keys ─────────────────────────────────────────────────

def test_author_lock_kept_in_controller_state_and_metadata():
    saved = host(ctrl={"presetAuthorLocked": True}, ctrl_meta={"presetAuthorLocked": True},
                 read_ctrl=["presetAuthorLocked"], read_ctrl_meta=["presetAuthorLocked"])
    assert saved["ctrl"]["presetAuthorLocked"] is True
    assert saved["ctrl_meta"]["presetAuthorLocked"] is True


MPE = ["mpeEnabled", "mpeConfig", "mpePitchBendRange"]


def test_state_applies_the_mpe_keys():
    saved = host(proc={"mpeEnabled": True, "mpeConfig": 1, "mpePitchBendRange": 5}, read_proc=MPE)
    assert saved["proc"] == {"mpeEnabled": True, "mpeConfig": 1, "mpePitchBendRange": 5}


def test_state_without_the_mpe_keys_reads_as_a_fresh_instance():
    saved = host(proc_delete=MPE, read_proc=MPE)
    assert saved["proc"] == {"mpeEnabled": False, "mpeConfig": 0, "mpePitchBendRange": 48}


# ─── version migration ────────────────────────────────────────────────────────

COMPAT = ["noteOnExpressionCompat", "roundingModeCompat"]


@pytest.mark.parametrize("version, product, on", [
    (11.0, "2.1.5", False), (10.0, "2.1.4", True), (9.0, "2.0.17", True), (4.0, "2.0.12", True),
])
def test_state_older_than_11_turns_the_compat_flags_on(version, product, on):
    saved = host(proc={"version": version, "productVersion": product}, read_proc=COMPAT)
    assert saved["proc"] == ({k: True for k in COMPAT} if on else {k: "<absent>" for k in COMPAT})


def test_compat_flags_cannot_be_turned_off_from_an_old_state():
    saved = host(proc={"version": 10.0, "productVersion": "2.1.4", **{k: False for k in COMPAT}},
                 read_proc=COMPAT)
    assert saved["proc"] == {k: True for k in COMPAT}
