"""Read and write a measured set of Serum 2 parameters in a decoded preset tree.

Each entry maps one DAW-automatable parameter (the 0 to 1 "norm" a host sees) to
its field in the tree from :func:`decode_preset_file` and to the value Serum
stores there, which is in the parameter's own units (seconds, voices, a
wavetable position on a 1 to 256 scale, ...). The curves were measured on Serum 2.1.5 (``productVersion``
``"2.1.5"``, processor ``version`` 11.0) through DawDreamer: each parameter set
to 0, 0.25, 0.5, 0.75 and 1, state saved and decoded. The formulas were fit
to those five points and then re-checked at 21 (0, 0.05, ..., 1): every point
matched within float32 rounding (relative error under 1e-6).

Serum leaves a parameter that is at its default out of the file. :func:`get_param`
reads an absent field as the default, and :func:`set_param` removes the field
whenever the stored value equals the default's.

The wavetable position parameters (``osc_*_wt_pos``) only apply while that
oscillator is a wavetable oscillator (``kParamType`` absent or ``"kOsc_WT"``).
On a sample, multisample, granular or spectral oscillator the field exists but
does nothing, so both functions raise ``ValueError`` instead.
"""
from __future__ import annotations

import math
import numbers
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping, Tuple

__all__ = ["Param", "PARAMS", "get_param", "set_param"]


@dataclass(frozen=True)
class Param:
    """One parameter: where it lives in the tree and how norm maps to stored."""

    name: str
    daw_index: int
    daw_name: str
    path: Tuple[str, ...]   # module keys, then "plainParams", then the field
    default_norm: float     # the plugin's defaultValue, exactly
    discrete: bool
    _to_stored: Callable[[float], float] = field(repr=False, compare=False)
    _to_norm: Callable[[float], float] = field(repr=False, compare=False)
    wavetable_osc: str = ""  # oscillator module that must be wavetable, if any

    def to_stored(self, norm: float) -> float:
        """Stored value for a norm in [0, 1]."""
        return float(self._to_stored(_check_norm(self.name, norm)))

    def to_norm(self, stored: float) -> float:
        """Norm for a stored value; ``ValueError`` if Serum couldn't store it."""
        if isinstance(stored, bool) or not isinstance(stored, numbers.Real):
            raise ValueError(f"{self.name}: stored value {stored!r} is not a number")
        lo, hi = self._to_stored(0.0), self._to_stored(1.0)
        if not (lo - 1e-9 * abs(lo) <= stored <= hi + 1e-9 * abs(hi)):
            raise ValueError(f"{self.name}: stored value {stored!r} is outside {lo}..{hi}")
        return min(1.0, max(0.0, self._to_norm(float(stored))))

    @property
    def default_stored(self) -> float:
        return self.to_stored(self.default_norm)


def _check_norm(name: str, norm: float) -> float:
    if isinstance(norm, bool) or not isinstance(norm, numbers.Real):
        raise ValueError(f"{name}: norm {norm!r} is not a number")
    if not 0.0 <= norm <= 1.0:   # also false for NaN; exact for huge ints
        raise ValueError(f"{name}: norm {norm!r} is outside [0, 1]")
    return float(norm)


def _unison_to_norm(voices: float) -> float:
    # Middle of the voice count's step, so get -> set keeps the count.
    if voices != int(voices):
        raise ValueError(f"unison: stored value {voices!r} is not a whole voice count")
    return (voices - 1) / 16 + 1 / 32


def _time(norm: float) -> float:
    return 32.0 * norm ** 5     # seconds, 0 to 32 s


def _time_to_norm(seconds: float) -> float:
    return (seconds / 32.0) ** 0.2


_TABLE = []

# Oscillators A, B, C: (DAW indexes of Unison, Uni Detune, WT Pos).
for _i, (_osc, (_uni, _det, _wt)) in enumerate(
        {"a": (44, 46, 59), "b": (99, 101, 114), "c": (154, 156, 169)}.items()):
    _O, _mod = _osc.upper(), f"Oscillator{_i}"
    _TABLE += [
        Param(f"osc_{_osc}_unison", _uni, f"{_O} Unison", (_mod, "plainParams", "kParamUnison"),
              0.0, True, lambda n: min(16, math.floor(16 * n) + 1), _unison_to_norm),
        Param(f"osc_{_osc}_detune", _det, f"{_O} Uni Detune", (_mod, "plainParams", "kParamDetune"),
              0.5, False, lambda n: n * n, math.sqrt),
        Param(f"osc_{_osc}_wt_pos", _wt, f"{_O} WT Pos",
              (_mod, f"WTOsc{_i}", "plainParams", "kParamTablePos"),
              0.0, False, lambda n: 1 + 255 * n, lambda s: (s - 1) / 255, wavetable_osc=_mod),
    ]

# Filters 1, 2: (DAW indexes of Freq, Res).
for _i, (_freq, _res) in enumerate([(205, 206), (216, 217)]):
    _mod = f"VoiceFilter{_i}"
    _TABLE += [
        # Stored as the norm; Serum displays about 8.18 * 2697 ** norm Hz.
        Param(f"filter{_i + 1}_cutoff", _freq, f"Filter {_i + 1} Freq",
              (_mod, "plainParams", "kParamFreq"), 0.5, False, lambda n: n, lambda s: s),
        Param(f"filter{_i + 1}_reso", _res, f"Filter {_i + 1} Res",
              (_mod, "plainParams", "kParamReso"),
              0.10000000149011612, False, lambda n: 100 * n, lambda s: s / 100),
    ]

# Envelopes 1 to 4: (DAW indexes of Attack, Decay, Sustain, Release), attack default.
for _i, (_atk, _dec, _sus, _rel, _atk_default) in enumerate([
        (224, 226, 227, 228, 0.12559431791305542),   # Env 1, the amp envelope
        (232, 234, 235, 236, 0.10999999940395355),
        (242, 244, 245, 246, 0.10999999940395355),
        (252, 254, 255, 256, 0.10999999940395355)]):
    _mod, _n = f"Env{_i}", _i + 1
    _TABLE += [
        Param(f"env{_n}_attack", _atk, f"Env {_n} Attack", (_mod, "plainParams", "kParamAttack"),
              _atk_default, False, _time, _time_to_norm),
        Param(f"env{_n}_decay", _dec, f"Env {_n} Decay", (_mod, "plainParams", "kParamDecay"),
              0.5, False, _time, _time_to_norm),
        # Stored as the norm; Serum displays gain norm ** 2, in dB for Env 1 and
        # as a percent for Env 2 to 4.
        Param(f"env{_n}_sustain", _sus, f"Env {_n} Sustain", (_mod, "plainParams", "kParamSustain"),
              1.0, False, lambda n: n, lambda s: s),
        Param(f"env{_n}_release", _rel, f"Env {_n} Release", (_mod, "plainParams", "kParamRelease"),
              0.2150000035762787, False, _time, _time_to_norm),
    ]

PARAMS: Mapping[str, Param] = MappingProxyType({p.name: p for p in _TABLE})
del _TABLE, _i, _osc, _uni, _det, _wt, _O, _mod, _freq, _res, _atk, _dec, _sus, _rel, _atk_default, _n


def _lookup(name: str) -> Param:
    try:
        return PARAMS[name]
    except (KeyError, TypeError):
        raise KeyError(f"unknown parameter {name!r}; known: {', '.join(PARAMS)}") from None


def _module(tree: dict, keys: Tuple[str, ...], name: str) -> dict:
    node = tree
    for depth, key in enumerate(keys, 1):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f"{name}: preset has no module {'.'.join(keys[:depth])}")
        node = node[key]
    if not isinstance(node, dict):
        raise KeyError(f"{name}: {'.'.join(keys)} is not a module")
    return node


def _plain(module: dict, name: str) -> dict:
    """The module's plainParams as a map ("default" or missing reads as {})."""
    plain = module.get("plainParams", {})
    if plain == "default":
        return {}
    if not isinstance(plain, dict):
        raise ValueError(f"{name}: plainParams is {plain!r}, not a map")
    return plain


def _check_wavetable(tree: dict, param: Param) -> None:
    if param.wavetable_osc:
        osc = _module(tree, (param.wavetable_osc,), param.name)
        kind = _plain(osc, param.name).get("kParamType", "kOsc_WT")
        if kind != "kOsc_WT":
            raise ValueError(f"{param.name}: {param.wavetable_osc} is {kind!r}, not a "
                             "wavetable oscillator, so its wavetable position does nothing")


def get_param(tree: dict, name: str) -> float:
    """Return parameter ``name``'s norm; an absent field reads as the default."""
    param = _lookup(name)
    plain = _plain(_module(tree, param.path[:-2], param.name), param.name)
    _check_wavetable(tree, param)
    if param.path[-1] not in plain:
        return param.default_norm
    return param.to_norm(plain[param.path[-1]])


def set_param(tree: dict, name: str, norm: float) -> None:
    """Set parameter ``name`` to ``norm`` (0 to 1), in place.

    Writes the stored value as a float, or removes the field when it equals
    the default's stored value (absent means default). Raises ``ValueError``
    for a norm outside [0, 1] or NaN, a malformed ``plainParams`` or a
    wavetable position on a non-wavetable oscillator, and ``KeyError`` for an
    unknown name or a module missing from the tree (modules are never
    created). Nothing is changed when it raises.
    """
    param = _lookup(name)
    stored = param.to_stored(norm)
    module = _module(tree, param.path[:-2], param.name)
    plain = _plain(module, param.name)
    _check_wavetable(tree, param)
    if math.isclose(stored, param.default_stored, rel_tol=1e-9):
        plain.pop(param.path[-1], None)     # no-op on "default" or a missing map
    else:
        module["plainParams"] = plain
        plain[param.path[-1]] = stored
