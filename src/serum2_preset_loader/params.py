"""Read and write a measured set of Serum 2 parameters in a decoded preset tree.

Each entry maps one DAW-automatable parameter (the 0 to 1 "norm" a host sees) to
its field in the tree from :func:`decode_preset_file` and to the value Serum
stores there, which is in the parameter's own units (seconds, voices, frame
index, ...). The curves were measured on Serum 2.1.5 (``productVersion``
``"2.1.5"``, processor ``version`` 11.0) through DawDreamer: each parameter set
to 0, 0.25, 0.5, 0.75 and 1, state saved and decoded. The formulas were fit
to those five points and then re-checked at 21 (0, 0.05, ..., 1): every point
matched within float32 rounding (relative error under 1e-6).

Serum omits a parameter that is at its default: the field is absent, not
written with the default value. :func:`get_param` reads an absent field as the
default and :func:`set_param` deletes the field when given the default.
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
    if math.isnan(norm) or not 0.0 <= norm <= 1.0:
        raise ValueError(f"{name}: norm {norm!r} is outside [0, 1]")
    return float(norm)


def _unison_to_norm(voices: float) -> float:
    # Middle of the voice count's step, so get -> set keeps the count.
    if voices != int(voices):
        raise ValueError(f"osc_a_unison: stored value {voices!r} is not a whole voice count")
    return (voices - 1) / 16 + 1 / 32


def _time(norm: float) -> float:
    return 32.0 * norm ** 5     # seconds, 0 to 32 s


def _time_to_norm(seconds: float) -> float:
    return (seconds / 32.0) ** 0.2


_TABLE = [
    Param("osc_a_unison", 44, "A Unison", ("Oscillator0", "plainParams", "kParamUnison"),
          0.0, True, lambda n: min(16, math.floor(16 * n) + 1), _unison_to_norm),
    Param("osc_a_detune", 46, "A Uni Detune", ("Oscillator0", "plainParams", "kParamDetune"),
          0.5, False, lambda n: n * n, math.sqrt),
    Param("osc_a_wt_pos", 59, "A WT Pos", ("Oscillator0", "WTOsc0", "plainParams", "kParamTablePos"),
          0.0, False, lambda n: 1 + 255 * n, lambda s: (s - 1) / 255),
    # Stored as the norm; Serum displays about 8.18 * 2697 ** norm Hz.
    Param("filter1_cutoff", 205, "Filter 1 Freq", ("VoiceFilter0", "plainParams", "kParamFreq"),
          0.5, False, lambda n: n, lambda s: s),
    Param("filter1_reso", 206, "Filter 1 Res", ("VoiceFilter0", "plainParams", "kParamReso"),
          0.10000000149011612, False, lambda n: 100 * n, lambda s: s / 100),
    Param("env1_attack", 224, "Env 1 Attack", ("Env0", "plainParams", "kParamAttack"),
          0.12559431791305542, False, _time, _time_to_norm),
    Param("env1_decay", 226, "Env 1 Decay", ("Env0", "plainParams", "kParamDecay"),
          0.5, False, _time, _time_to_norm),
    # Stored as the norm; Serum displays the level as gain norm ** 2 in dB.
    Param("env1_sustain", 227, "Env 1 Sustain", ("Env0", "plainParams", "kParamSustain"),
          1.0, False, lambda n: n, lambda s: s),
    Param("env1_release", 228, "Env 1 Release", ("Env0", "plainParams", "kParamRelease"),
          0.2150000035762787, False, _time, _time_to_norm),
]

PARAMS: Mapping[str, Param] = MappingProxyType({p.name: p for p in _TABLE})


def _lookup(name: str) -> Param:
    try:
        return PARAMS[name]
    except KeyError:
        raise KeyError(f"unknown parameter {name!r}; known: {', '.join(PARAMS)}") from None


def _module(tree: dict, param: Param) -> dict:
    node = tree
    for depth, key in enumerate(param.path[:-2], 1):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f"{param.name}: preset has no module {'.'.join(param.path[:depth])}")
        node = node[key]
    if not isinstance(node, dict):
        raise KeyError(f"{param.name}: {'.'.join(param.path[:-2])} is not a module")
    return node


def get_param(tree: dict, name: str) -> float:
    """Return parameter ``name``'s norm; an absent field reads as the default."""
    param = _lookup(name)
    plain = _module(tree, param).get("plainParams", {})
    if plain == "default" or param.path[-1] not in plain:
        return param.default_norm
    return param.to_norm(plain[param.path[-1]])


def set_param(tree: dict, name: str, norm: float) -> None:
    """Set parameter ``name`` to ``norm`` (0 to 1), in place.

    Writes the stored value as a float, or deletes the field when it equals
    the default's stored value, as Serum does. Raises ``ValueError`` for a
    norm outside [0, 1] or NaN, and ``KeyError`` for an unknown name or a
    module missing from the tree (modules are never created).
    """
    param = _lookup(name)
    stored = param.to_stored(norm)
    module = _module(tree, param)
    plain = module.get("plainParams", {})
    if plain == "default":
        plain = {}
    if not isinstance(plain, dict):
        raise ValueError(f"{param.name}: plainParams is {plain!r}, not a map")
    module["plainParams"] = plain
    if math.isclose(stored, param.default_stored, rel_tol=1e-9):
        plain.pop(param.path[-1], None)
    else:
        plain[param.path[-1]] = stored
