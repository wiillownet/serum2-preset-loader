# serum-2-preset-loader

Convert `.SerumPreset` files into VST3 state blobs that
[DawDreamer](https://github.com/DBraun/DawDreamer) can load into Serum 2 — so
you can render presets to audio without opening the Serum GUI.

Tested against Serum 2.1.4 on macOS.

## Why

Serum 2 ships its presets as `.SerumPreset` files (a small Zstd-compressed
CBOR document). DawDreamer's `synth.load_state(...)` expects the VST3
`IComponent` state shape that Serum writes via `getState`, which is structurally
similar but not identical. This package translates one to the other.

## Install

```sh
pip install serum2-preset-loader            # core converter
pip install serum2-preset-loader[render]    # + dawdreamer/scipy/numpy
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

## Usage as a CLI

The included example renders one preset to a WAV file:

```sh
python examples/render_preset.py /path/to/Serum2.vst3 "My Preset.SerumPreset" out.wav
```

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
- **IComponent state**: `{"component": "processor", "product": "Serum2", "productVersion": "2.1.4", …}`

### Layer 3 — the CBOR payload

Both payloads decode to a CBOR map keyed by module name (`Arp0`, `Env0`,
`Oscillator0`, `Macro0`, `ModSlot0`, …, ~160 modules). Each module is itself a
map with a `plainParams` field plus module-specific extras (`curveData`,
`pathData`, `clip`, etc.).

The two CBORs are mostly identical, but they differ in three places:

#### a) `plainParams` shape

The preset uses a `"default"` *string* sentinel for any module whose params are
all at their factory defaults; the processor uses an empty map.

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
| `version` | `10.0` |

There are also a few preset-only **sub-keys** the converter strips:
`Macro{0..7}.name`, `FXRack{0..2}.displayName`, extra UI fields on
`MidiClip{0..11}` (`laneTabs`, `gridWidth_Beats`, `name`, …),
`PitchQuantizer0.scaleName`.

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
- Core conversion is dawdreamer-free; only the example renderer needs it.
- Direct injection of `.SerumPreset` bytes via `setState` does **not** work —
  Serum silently falls back to the init patch. The CBOR translation in this
  package is required.

## License

MIT.
