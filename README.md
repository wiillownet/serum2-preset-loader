# serum2-preset-loader

[![PyPI](https://img.shields.io/pypi/v/serum2-preset-loader)](https://pypi.org/project/serum2-preset-loader/)
[![Python versions](https://img.shields.io/pypi/pyversions/serum2-preset-loader)](https://pypi.org/project/serum2-preset-loader/)
[![tests](https://github.com/wiillownet/serum2-preset-loader/actions/workflows/tests.yml/badge.svg)](https://github.com/wiillownet/serum2-preset-loader/actions/workflows/tests.yml)
[![license](https://img.shields.io/pypi/l/serum2-preset-loader)](LICENSE)

Read, write and convert Serum 2 `.SerumPreset` files. Convert presets into
VST3 state blobs that [DawDreamer](https://github.com/DBraun/DawDreamer) can
load into Serum 2 — so you can render presets to audio without opening the
Serum GUI — and decode, edit and re-encode the preset files themselves.

Tested against Serum 2.1.4 on macOS.

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

**Verified in Serum 2:** a factory preset decoded, edited (name and filter
cutoff) and written this way loads with both edits applied. Still open: whether
Serum writes non-ASCII preset names as raw UTF-8 (this writes `\u` escapes),
and whether it accepts an `int` where it wrote a float (write `1.0`, not `1`,
when editing).

### Rendering an edited preset without writing a file

```python
from serum2_preset_loader import decode_preset_file, convert_preset_tree

_header, tree = decode_preset_file("My Preset.SerumPreset")
tree["Oscillator0"]["WTOsc0"]["plainParams"]["kParamTablePos"] = 20.0
state_blob = convert_preset_tree(tree)   # same output as convert_preset_bytes
```

## Targeting a different Serum build

The version markers Serum embeds in the processor-state CBOR are exposed as
module attributes you can override before calling the converter:

```python
from serum2_preset_loader import converter
converter.PROCESSOR_PRODUCT_VERSION = "2.2.0"
converter.PROCESSOR_FORMAT_VERSION = 11.0
# converter.SUPPORTED_XFERJSON_VERSION = 3   # if Serum bumps the wrapper
```

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
| `killEnvsGracefullyCompat` | `true` |
| `Arp0.activeClip` | `0` (defaults to first clip; not present in preset) |
| `productVersion` | `"2.1.4"` |
| `version` | `10.0` (CBOR float; not int — Serum's `getState` writes a major-7 float here) |

There are also a few preset-only **sub-keys** the converter strips:
`Macro{0..7}.name`, `FXRack{0..2}.displayName`, extra UI fields on
`MidiClip{0..11}` (`laneTabs`, `gridWidth_Beats`, `name`, …),
`PitchQuantizer{n}.scaleName`.

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

- Targets Serum 2.1.4. If a future Serum version changes the schema, the
  converter may need new mappings; `docs/DERIVATION.md` describes how to
  re-derive them.
- Per-instance audio randomization (unison detune phase, sample start, etc.)
  means rendered audio is not bit-identical to a manual GUI load — but it's
  audibly the same patch.
- Direct injection of `.SerumPreset` bytes via `setState` does **not** work —
  Serum silently falls back to the init patch. The CBOR translation in this
  package is required.

## License

MIT.
