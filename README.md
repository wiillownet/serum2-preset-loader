# serum2-preset-loader

[![PyPI](https://img.shields.io/pypi/v/serum2-preset-loader)](https://pypi.org/project/serum2-preset-loader/)
[![Python versions](https://img.shields.io/pypi/pyversions/serum2-preset-loader)](https://pypi.org/project/serum2-preset-loader/)
[![tests](https://github.com/wiillownet/serum2-preset-loader/actions/workflows/tests.yml/badge.svg)](https://github.com/wiillownet/serum2-preset-loader/actions/workflows/tests.yml)
[![license](https://img.shields.io/pypi/l/serum2-preset-loader)](LICENSE)

Read, write and convert Serum 2 `.SerumPreset` files. Convert presets into
VST3 state blobs that [DawDreamer](https://github.com/DBraun/DawDreamer) can
load into Serum 2 — so you can render presets to audio without opening the
Serum GUI — and decode, edit and re-encode the preset files themselves.

Tested against Serum 2.1.5 on macOS, through DawDreamer: every installed
preset under 20 MB loads, and Serum ends up in the same state as when Live
opens the preset itself, apart from three host or compat fields (see Caveats).
The parameter table below was measured there too.

For a ready-made renderer built on top of this library, see
[serum-render](https://github.com/wiillownet/serum-render).

## Why

Serum 2 ships its presets as `.SerumPreset` files (a small Zstd-compressed
CBOR document). DawDreamer's `synth.load_state(...)` expects the VST3
`IComponent` state shape that Serum writes via `getState`, which is structurally
similar but not identical. This package translates one to the other.

## Install

```sh
pip install serum2-preset-loader
```

For local development:

```sh
git clone https://github.com/wiillownet/serum2-preset-loader.git
cd serum2-preset-loader
pip install -e ".[test]"
```

## Usage as a library

```python
import dawdreamer as daw
from serum2_preset_loader import convert_preset_file

state_blob = convert_preset_file("My Preset.SerumPreset")
with open("/tmp/state.bin", "wb") as f:
    f.write(state_blob)

engine = daw.RenderEngine(44100, 512)
synth = engine.make_plugin_processor("serum", "/path/to/Serum2.vst3")
synth.load_state("/tmp/state.bin")
# … add MIDI, render as usual
```

If you'd rather skip the temp file, you can stream the bytes via your own
NamedTemporaryFile — DawDreamer's `load_state` takes a file path.

## Reading preset metadata (no decode required)

```python
from serum2_preset_loader import read_preset_metadata
meta = read_preset_metadata("My Preset.SerumPreset")
print(meta["presetName"], meta["presetAuthor"], meta["tags"])
```

## Reading and writing presets

```python
from serum2_preset_loader import (
    decode_preset_file, set_preset_metadata, write_preset_file,
)

header, tree = decode_preset_file("My Preset.SerumPreset")
tree["VoiceFilter0"]["plainParams"]["kParamFreq"] = 0.25
set_preset_metadata(header, tree, presetName="My Preset (darker)")
write_preset_file("My Preset (darker).SerumPreset", header, tree)
```

`header` is the JSON metadata; `tree` is the decoded CBOR, keyed by module
(`Oscillator0`, `Env0`, `VoiceFilter0`, `ModSlot0`, …). Edited modules hold a
`plainParams` map of parameter name to value; untouched ones hold an empty map.
Values are not all the 0–1 range a DAW sees (envelope curves are stored as
`50.0`, filter types as names like `"MgL6"`). Which fields exist and what they
mean is still being mapped; the format is reverse-engineered.

Metadata (`presetName`, `tags`, `presetAuthor`, …) lives in both the header and
the tree's top level. `write_preset_file` raises `ValueError` if the two
disagree rather than silently keeping one; `set_preset_metadata` sets a field
in both. The header's `hash` is recomputed on write, and the file is written
to a temporary file and moved into place, so a failed or concurrent write
never leaves a partial preset. The encoder keeps Serum's float widths (32-bit
where the value fits, 64-bit otherwise) and its alphabetical key order, so the
unedited test preset round-trips to identical CBOR. The Zstd stream around it
differs from Serum's, so files are not byte-identical overall.

**Verified in Serum 2.1.5:** a factory preset decoded, edited (name and
filter cutoff) and written this way loads with both edits applied. A
non-ASCII name (`Café ñ 日本 ü`) shows correctly in the browser and the
preset display. This package writes it as `\u` escapes, while Serum itself
writes raw UTF-8 in decomposed (NFD) form; both load. An `int` where Serum
writes a float (`kParamUnison = 4`, `kParamFreq = 1`) loads as the same
value.

### Rendering an edited preset without writing a file

```python
from serum2_preset_loader import decode_preset_file, convert_preset_tree

_header, tree = decode_preset_file("My Preset.SerumPreset")
tree["Oscillator0"]["WTOsc0"]["plainParams"]["kParamTablePos"] = 20.0
state_blob = convert_preset_tree(tree)   # same output as convert_preset_bytes
```

## Parameters

`set_param` and `get_param` work in the 0 to 1 range a DAW knob uses and convert
to the value Serum stores, which is in each parameter's own units (seconds,
voices, a wavetable position on a 1 to 256 scale, ...):

```python
from serum2_preset_loader import decode_preset_file, set_param, write_preset_file

header, tree = decode_preset_file("My Preset.SerumPreset")
set_param(tree, "filter1_cutoff", 0.25)   # about 59 Hz
set_param(tree, "env1_attack", 0.25)      # 31 ms
write_preset_file("My Preset (edited).SerumPreset", header, tree)
```

Only 29 parameters are covered so far (`PARAMS` lists them): unison, detune
and wavetable position for oscillators A, B and C (`osc_a_unison`, ...),
cutoff and resonance for filters 1 and 2 (`filter1_cutoff`, ...), and attack,
decay, sustain and release for Env 1 (the amp envelope) through Env 4
(`env1_attack`, ...). Each curve was measured on Serum 2.1.5 at 21 points (0,
0.05, ..., 1); the tests check every one. Serum leaves a parameter out of the
file when it's at its default, so `get_param` reads a missing field as the
default and `set_param` removes the field when you set the default. Wavetable
position only applies to a wavetable oscillator: on a sample, multisample,
granular or spectral oscillator both functions raise `ValueError`.

### Oscillator type fields

Fields that only exist for one oscillator type aren't in `PARAMS`; edit them
in `plainParams` directly, in stored units. Each oscillator `OscillatorN`
(A is 0, B is 1, C is 2) keeps a submodule for every type, and only the one
matching its `kParamType` (absent means `kOsc_WT`) has any effect:
`WTOsc{N}`, `MultiSampleOsc{N}`, `GranularOsc{N}` or `SpectralOsc{N}`. The
sample type has no fields of its own beyond warp. Measured on oscillator A in
Serum 2.1.5, 30 norms per field (`n` is the host knob, 0 to 1):

| Submodule | Field | Stored value | Default |
|---|---|---|---|
| `WTOsc` | `kParamUnisonWTPos` | -100 + 200n | 0 |
| `WTOsc` | `kParamInitialPhase` | 360n (degrees) | 180 |
| `WTOsc` | `kParamRandomPhase` | 100n | 100 |
| `MultiSampleOsc` | `kParamTimbreShift` | -64 + 128n | 0 |
| `MultiSampleOsc` | `kParamRandomPhase` | 100n | 0 |
| `MultiSampleOsc` | `kParamEnvOverride` | 1.0 from n = 0.5, else 0.0 | 0.0 |
| `MultiSampleOsc` | `kParamEnvDelay` | 8n⁵ seconds | 0 |
| `MultiSampleOsc` | `kParamEnvAttack`, `kParamEnvDecay`, `kParamEnvRelease` | 32n⁵ seconds | decay 1.0; attack and release not determined |
| `MultiSampleOsc` | `kParamEnvSustain` | n | 1.0 |
| `GranularOsc` | `kParamDensity` | 1000n⁴ (shown as 30n grains) | 3.90625 |
| `GranularOsc` | `kParamGrainLength` | 10n⁴ seconds | 0.625 |
| `GranularOsc` | `kParamGrainReverse` | 1.0 from n = 0.5, else 0.0 | 0.0 |
| `GranularOsc` | `kParamWindowParam` | 100n | 100 |
| `SpectralOsc` | `kParamSpecFltShift` | -100 + 200n | 0 |
| `SpectralOsc` | `kParamSpecFltWetDry` | 100n | 100 |
| `SpectralOsc` | `kParamFreqLo`, `kParamFreqHi` | 10 · 2205ⁿ Hz | 10 and 22050 |

These defaults are the values Serum leaves out of the file; the host's
`defaultValue` for the same slot describes the wavetable meaning and is wrong
for most of them. Granular density and grain length were measured in their
default (non-BPM) mode, and oscillators B and C are assumed to match A.

## Version markers

The state keeps the preset's own `version` and `productVersion`, so Serum
migrates an older preset exactly as it does when it opens the file itself.
A tree without them gets `converter.PROCESSOR_FORMAT_VERSION` (11.0) and
`converter.PROCESSOR_PRODUCT_VERSION` (`"2.1.5"`), which Serum loads as
current. If Serum bumps the XferJson wrapper:

```python
from serum2_preset_loader import converter
converter.SUPPORTED_XFERJSON_VERSION = 3
```

## State for a DAW project

A DAW saves two blobs per VST3 plugin: the processor state, which sets the
sound, and the controller state, which holds what the plugin shows. `convert_*`
gives DawDreamer the processor state only, which is all rendering needs. To
write a preset into a DAW project (for example with
[alskit](https://github.com/wiillownet/alskit)), build both:

```python
from serum2_preset_loader import (
    build_controller_state, build_processor_state, decode_preset_file,
)

_header, tree = decode_preset_file("My Preset.SerumPreset")
processor = build_processor_state(tree)    # XferJson bytes
controller = build_controller_state(tree)  # XferJson bytes
```

Without the controller state, Serum in the DAW plays the preset but shows
"Init" and default macro names. With it, Serum shows the preset's name,
author, description, macro names and clip names, and a double-click on an FX
knob resets to the preset's value. The controller state also carries the
preset's MIDI CC map (`midiMap`), copied unchanged, and each FX rack's
links from the 48 "FX Main/Bus Param" host parameters to FX knobs
(`proxyParams`). A preset with neither gets neither key, as in Serum's own
save. Proxy links are checked first, because Serum 2.1.5 crashes the host on
some malformed ones: an entry is kept, exactly as stored, only if its slot is
`"0"` to `"15"`, its `fxID` names an FX in that rack and its `paramIndex` is
below that FX type's parameter count. Every other entry is dropped with a
Python warning naming the rack, slot and reason. An FX entry with no `type`,
or one outside 0 to 15, also crashes Serum, so `build_controller_state`,
`build_processor_state` and `convert_*` raise `InvalidFXTypeError` (a
`ValueError`) for it rather than drop it and shift every later FX's index.
One caveat: Serum drops a
host value sent to a proxy parameter in the first audio block after a state
load, so set it after one block has been processed. Checked against Live 12's saved state
under Serum 2.1.5: after Serum loads and re-saves it, the controller state
matches Live's except for FX fields stored as text (an FX mode such as
`kParamMode: "Normal"`), whose double-click target falls back to Serum's
default. An optional second argument, the preset's path, is made absolute
and stored as `selectedPresetPath`. Leave it out of a project you share: for
a user preset it holds your home directory, inside compressed data a text
search won't find.

## Untrusted files

Every corrupt, truncated or hostile input raises `ValueError` (or `OSError`
if the file can't be read), so a batch loop can catch one exception type.
Sizes the file declares are checked before anything is allocated:

- the JSON header is capped at 16 MiB and the decompressed CBOR at 1 GiB;
- a Zstd frame whose declared size disagrees with the header is rejected
  without being decompressed;
- the CBOR may hold at most 1M maps and arrays, counted before decoding
  (the test preset holds 425).

After decoding, the tree may nest at most 64 levels (the test preset nests
4), every map must have string keys, and every value must be a map, array,
string, bytes, number, boolean or null. CBOR shared references (tags 28/29),
which Serum does not write, are rejected: they can describe cyclic or
exponentially large trees in a few hundred bytes. These checks are what make
the guarantee hold on cbor2 5 as well as 6: cbor2 5 accepts some malformed
input (a stray break byte, unlimited nesting) that cbor2 6 rejects itself.

**What this does not bound:** an honest file near the 1 GiB cap. Floats
decode to about 32 bytes of Python memory each, so 1 GiB of float data is
roughly 7 GB in one process. If you run conversions in a worker pool over
files you don't trust, also set a per-worker memory limit (for example
`resource.setrlimit(resource.RLIMIT_AS, ...)` in the worker initializer on
Linux).

## How it works

There are three layers between a `.SerumPreset` on disk and the parameter
values living in Serum's audio thread. The converter has to peel back two of
them, transform the third, then re-stack.

### Layer 1 — JUCE's VST3 state envelope

DawDreamer's `synth.save_state(path)` and `synth.load_state(path)` use JUCE's
`VST3PluginState` format, not raw VST3 bytes. The on-disk layout is:

```
4   bytes   uint32_le  magic = 0x21324356  ("VC2!")
4   bytes   uint32_le  xml length
N   bytes   UTF-8 XML  (see below)
1   byte    NUL terminator
```

The XML wraps two base64-encoded blobs:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<VST3PluginState>
    <IComponent>4436.XYVYxozbu4F.2B........</IComponent>
    <IEditController>2233.4VKzqr+...</IEditController>
</VST3PluginState>
```

**Gotcha:** the encoding is not standard base64. JUCE's
`MemoryBlock::toBase64Encoding()` uses a custom 64-character alphabet starting
with `.` (so a `.` in the encoded string is the value 0, *not* padding) and
prefixes the output with `<decimal length>.`. A standard `base64.b64decode`
will choke on it. See `serum2_preset_loader.wrappers.juce_memoryblock_b64decode`
for the implementation.

### Layer 2 — Serum's `XferJson` wrapper

The decoded `IComponent` bytes (and, separately, every `.SerumPreset` file)
share the same outer container:

```
9   bytes   b"XferJson\x00"
8   bytes   uint64_le  json metadata length
N   bytes   UTF-8 JSON metadata
4   bytes   uint32_le  uncompressed CBOR payload size
4   bytes   uint32_le  format version  (currently 2)
M   bytes   Zstandard-compressed CBOR
```

The JSON metadata differs by context:

- **Preset file**: `{"fileType": "SerumPreset", "presetName": "…", "presetAuthor": "…", "tags": [...], …}`
- **IComponent state**: `{"component": "processor", "product": "Serum2", "productVersion": "2.1.4", "hash": "…", …}`

The `hash` field is `md5(compressed_cbor)` — verified by md5'ing the
compressed payload of a captured Serum 2.1.4 state and matching it byte-for-
byte against the metadata header. The converter recomputes it for the
processor-state CBOR it generates.

### Layer 3 — the CBOR payload

Both payloads decode to a CBOR map keyed by module name (`Arp0`, `Env0`,
`Oscillator0`, `Macro0`, `ModSlot0`, …, ~160 modules). Each module is itself a
map with a `plainParams` field plus module-specific extras (`curveData`,
`pathData`, `clip`, etc.).

The two CBORs are mostly identical, but they differ in three places:

#### a) `plainParams` shape

A preset marks a module whose params are all at their factory defaults either
with a `"default"` *string* sentinel or with an empty map (the 2.1.4 test
preset uses only empty maps); the processor always uses an empty map, so the
converter expands the sentinel.

```
preset:    "Arp0":         {"plainParams": "default"}
processor: "Arp0":         {"plainParams": {}, "activeClip": 0}
```

When `plainParams` is a real dict (the module *has* been edited), the contents
are byte-identical between formats:

```
preset:    "Env0": {"plainParams": {"kParamAttack": 0.0005153631860372513,
                                    "kParamDecay":  0.9660339322275069,  …}}
processor: "Env0": {"plainParams": {"kParamAttack": 0.0005153631860372509,
                                    "kParamDecay":  0.9660339322275069,  …}}
```

(The trailing-digit drift on `kParamAttack` is float roundtrip noise from
Serum re-serializing.)

#### b) Preset-only top-level keys (dropped)

| key | role |
|---|---|
| `fileType`, `presetName`, `presetAuthor`, `presetDescription` | preset metadata (also in the JSON header) |
| `arpBankDisplayName`, `clipBankDisplayName` | UI strings for the preset browser |
| `ClipPlayer`, `Filter`, `SerumGUI` | UI panel state |
| `GranularOsc`, `MultiSampleOsc`, `Osc`, `SpectralOsc`, `WTOsc` | library template lists (not the *chosen* osc, which lives inside `Oscillator0..4`) |

#### c) Processor-only top-level keys (added)

| key | value |
|---|---|
| `component` | `"processor"` |
| `Arp0.activeClip` | `0` (defaults to first clip; not present in preset) |
| `productVersion` | the preset's own (fallback `"2.1.5"`) |
| `version` | the preset's own, as a CBOR float (fallback `11.0`); Serum migrates from it |

There are also a few preset-only **sub-keys** the converter strips:
`Macro{0..7}.name`, `FXRack{0..2}.displayName`, extra UI fields on
`MidiClip{0..11}` (`laneTabs`, `gridWidth_Beats`, `name`, …).
`PitchQuantizer{n}.scaleName` is kept: without it Serum picks another scale.

### Putting it back together

```
preset bytes
  └─ unwrap_xferjson      → preset CBOR
       └─ preset_cbor_to_processor_cbor  → processor CBOR
            └─ wrap_xferjson with processor metadata → IComponent bytes
                 └─ build_juce_vst3_state          → VST3 state blob (load_state'able)
```

For a deeper account of how this mapping was figured out — including dead-end
attempts and the probes used to diff the two formats — see
[docs/DERIVATION.md](docs/DERIVATION.md).

## Caveats

- Serum loads a state older than version 11.0 (any preset saved before
  2.1.5) with `noteOnExpressionCompat` and `roundingModeCompat` switched on,
  which opening the preset in Serum's browser doesn't do. They can't be
  turned off from the state, and stamping 11.0 instead would skip Serum's
  migration. `mpeEnabled` comes from the host.
- If a future Serum version changes the schema, the converter may need new
  mappings; `docs/DERIVATION.md` describes how to re-derive them.
- Per-instance audio randomization (unison detune phase, sample start, etc.)
  means rendered audio is not bit-identical to a manual GUI load — but it's
  audibly the same patch.
- Direct injection of `.SerumPreset` bytes via `setState` does **not** work —
  Serum silently falls back to the init patch. The CBOR translation in this
  package is required.

## License

MIT.
