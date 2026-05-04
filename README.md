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

Both `.SerumPreset` files and Serum 2's IComponent state share the same outer
wrapping (`XferJson` magic, JSON metadata header, Zstd-compressed CBOR). The
inner CBORs differ in two ways:

1. **`plainParams` shape.** Presets store `plainParams: "default"` for any
   module that hasn't been edited; the processor stores `plainParams: {}`. The
   converter rewrites this recursively.
2. **Top-level keys.** Presets carry UI/library/metadata fields (`SerumGUI`,
   `WTOsc`, `presetName`, …) that the processor doesn't keep, and the
   processor adds a couple of fields (`component: "processor"`,
   `killEnvsGracefullyCompat`) that the preset doesn't have.

Module-level `plainParams` *values* (e.g. on `Env0`, `LFO0`, `ModSlot0`) are
already in the same shape in both formats and are passed through unchanged.

## Caveats

- Targets Serum 2.1.4. If a future Serum version changes the schema, the
  converter may need new mappings.
- Per-instance audio randomization (unison detune phase, sample start, etc.)
  means rendered audio is not bit-identical to a manual GUI load — but it's
  audibly the same patch.
- Core conversion is dawdreamer-free; only the example renderer needs it.

## License

MIT.
