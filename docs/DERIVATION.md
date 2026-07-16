# How the converter was derived

This document records how the `.SerumPreset` → processor-state mapping was
reverse-engineered. Read this if you need to update the converter for a future
Serum 2 version that changes the on-disk schema, or if you just want to
understand *why* the converter does what it does.

The investigation went through seven phases (0–6). Each phase produced a small
probe script; you can re-run the same playbook on a new Serum build.

---

## Phase 0 — Working hypothesis

Initial guess: a `.SerumPreset` is "just" the bytes Serum's VST3 IComponent
returns from `getState()`, possibly with a small wrapper around it. If true,
the converter is a wrapper rewrite — no schema work needed.

This turned out to be wrong, but the probe to disprove it set up everything
that followed.

---

## Phase 1 — Capture a reference state

**Probe:** open Serum 2 in DawDreamer, manually load a preset via the GUI,
close the editor, and dump the state.

```python
engine = daw.RenderEngine(44100, 512)
synth = engine.make_plugin_processor("serum", VST3_PATH)
synth.open_editor()        # blocks until window is closed
synth.save_state(out_path) # writes JUCE VST3 state blob
```

**What we found:** The dump starts with magic bytes `56 43 32 21` ("VC2!") —
the JUCE `VST3PluginState` header. The next four bytes are the length of an
embedded XML document. The XML wraps two `<IComponent>` and `<IEditController>`
elements containing what *looks* like base64.

**Gotcha that cost the most time:** the contents are not standard base64.
Strings like `4436.XYVYxozbu4F.2B........` contain `.` characters everywhere
and the standard library's `base64.b64decode` raises `binascii.Error: Incorrect
padding`. JUCE's `MemoryBlock::toBase64Encoding()` uses a custom alphabet:

```
".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
```

with `.` as the value-0 character (not padding), and prefixes the encoded data
with `<decimal byte length>.`. The decoder packs 6-bit groups into bytes
**LSB-first**, mirroring `MemoryBlock::setBitRange`.

If you hit a base64 decode error in the future, suspect this first.

---

## Phase 2 — Parse the inner blob

**Probe:** decode the IComponent bytes with the JUCE MemoryBlock decoder and
inspect them.

**What we found:** the IComponent itself starts with `XferJson\x00` — Serum's
own wrapper, identical in structure to a `.SerumPreset` file:

```
9 bytes  "XferJson\x00"
8 bytes  uint64_le json length
N bytes  JSON metadata
4 bytes  uint32_le uncompressed CBOR size
4 bytes  uint32_le format version (2)
M bytes  Zstandard-compressed CBOR
```

So the wrapping is *symmetric*: a preset file and an IComponent state blob both
unwrap to a JSON metadata header + a Zstd-compressed CBOR payload. The
difference must be inside the CBOR (or in the metadata).

The metadata is the first hint that they're not the same thing:

```
preset:    {"fileType": "SerumPreset",  "presetName": "KY - The Magic Keys", ...}
processor: {"component": "processor",   "productVersion": "2.1.4",          ...}
```

---

## Phase 3 — Try the obvious thing first

Before doing schema work, test the cheap hypothesis: does Serum's `setState`
accept the preset bytes verbatim?

**Probe:** wrap the raw `.SerumPreset` bytes as the IComponent payload of a
JUCE state blob, write to a tempfile, call `synth.load_state(path)`, then
render audio and compare against:
- the **default-init audio** (no preset loaded, just close the editor)
- the **GUI-loaded audio** (manually drag the preset into the editor)

Three RMS values to look at:

| run | RMS | what it tells you |
|---|---|---|
| no-load default | `~0.094` | Serum's init patch tone |
| GUI-loaded preset | `~0.043` | ground truth — preset is loaded |
| inject preset bytes via `load_state` | matches **no-load** | Serum silently fell back to init |

**What we found:** direct injection silently fails. Serum's `setState` rejects
the preset CBOR shape, doesn't error, and reverts to the default patch. So
schema translation is required.

**Sanity check:** before concluding "translation needed," also verify that
`load_state` of a *captured* loaded-preset state reproduces the audio. If it
doesn't, the bug is somewhere else (DawDreamer, Serum's editor not pushing to
the processor, state caching). For us, round-trip worked — RMS matched the
GUI-loaded ground truth within 3% — so the format mapping was the only thing
left to do.

---

## Phase 4 — Look for a backdoor

Before committing to schema work, enumerate every parameter Serum exposes via
VST3 and grep for anything that could load a preset by index, path, or slot:

```python
params = synth.get_parameters_description()
hits = [p for p in params if any(k in p["name"].lower()
        for k in ("preset", "program", "patch", "load", "bank", "file", "path", "name"))]
```

**What we found:** one hit, parameter index 540 named `Bank` with 128 steps
labeled "Prog 1"–"Prog 128". This is the legacy VST2-style program parameter
JUCE auto-exposes. Sweeping it produced byte-identical state and audio at every
value — Serum doesn't wire the slots to anything in DawDreamer's setup.

No backdoor exists. Onward.

---

## Phase 5 — Diff the two CBORs

This is the actual derivation. With two known-good captures in hand
(`state_loaded.bin` from a GUI-loaded preset, and the corresponding
`.SerumPreset` file), decode both CBORs and structurally diff them.

```python
import cbor2

# Both blobs end as zstd-compressed CBOR; the preset is reachable directly
# off disk, the IComponent is one JUCE-envelope hop further in (u32 magic +
# u32 xml_len + xml containing a juce-base64 <IComponent>). See
# `_extract_icomponent` in tests/test_real_preset_fixture.py for the layout.
preset_cbor    = cbor2.loads(unwrap_xferjson(open("preset.SerumPreset","rb").read())[2])
processor_cbor = cbor2.loads(unwrap_xferjson(_extract_icomponent(open("state_loaded.bin","rb").read()))[2])
```

**Top-level diff (175 vs 163 keys):**

- **161 keys overlap**, including all the synthesis modules (`Arp0`,
  `Env0..3`, `LFO0..9`, `ModSlot0..63`, `Oscillator0..4`, …).
- **14 keys preset-only** — UI panel state (`SerumGUI`, `Filter`,
  `ClipPlayer`), library template lists (`WTOsc`, `Osc`, `GranularOsc`,
  `MultiSampleOsc`, `SpectralOsc`), preset metadata (`presetName`,
  `presetAuthor`, …).
- **2 keys processor-only** — `component: "processor"` and
  `killEnvsGracefullyCompat: true`.

**Within-key diff (the important one):**

For modules where the preset has an *unedited* `plainParams`, the preset
stores the **string** `"default"` and the processor stores an **empty map**.
Concretely:

```
preset:    "Arp0":  {"plainParams": "default"}
processor: "Arp0":  {"plainParams": {}, "activeClip": 0}
```

For modules where the preset has *real* `plainParams` values, both formats
store the same dict, byte-for-byte (modulo float-roundtrip drift in the last
few decimal places). Compare `Env0` between the two — if the values match,
then the rule is just "rewrite the sentinel; pass everything else through."

**Counts in the test preset (KY - The Magic Keys):**

- 103 modules with `plainParams: "default"` → all need the sentinel rewrite.
- 46 modules with real overrides → pass-through.

A few module types have UI sub-keys to strip: `Macro{i}.name`,
`FXRack{i}.displayName`, and extra fields on `MidiClip{i}` (`laneTabs`,
`gridWidth_Beats`, etc. — observed on `MidiClip1` in the test preset; the
converter strips them from every `MidiClip` index).

---

### Aside: the `hash` field in the JSON metadata

The IComponent metadata header contains a 32-hex-char `hash` field that
*does* vary between captures (so it isn't a constant schema/build identifier).
By md5'ing every plausible candidate from a captured state file, we found:

```
hash == md5(compressed_cbor_payload)
```

i.e. md5 of the Zstd-compressed CBOR bytes that follow the size+version
header. The converter recomputes this. Empirically Serum's `setState` does not
seem to validate the hash (it accepts blobs where we copied the wrong value),
but computing it correctly costs nothing and matches the round-trip shape.

---

## Phase 6 — Validate

Convert the preset, render through DawDreamer, compare audio against the
GUI-loaded ground truth.

| run | peak | rms | sha256 |
|---|---|---|---|
| no-load default | 0.28 | **0.094** | `1e37d93d…` |
| GUI-loaded ground truth | 0.31 | **0.043** | `5426663a…` |
| `load_state` of saved state (round-trip) | 0.32 | **0.044** | `35a97647…` |
| converter output | 0.32 | **0.044** | `a195bd95…` |

The converter's RMS matches the ground truth and round-trip within 3%. The
SHA-256 differs only because Serum has per-instance randomness (unison detune
phases, sample start, etc.) — the captured state is the *patch*, not the
*sample-accurate audio*. RMS/peak similarity is the correct success criterion.

We also ran three other factory presets through the converter to check that
the transform isn't accidentally hardcoded for one preset:

| preset | rms | clearly distinct from no-load? |
|---|---|---|
| KY - Zero Feels | 0.084 | yes |
| KY - Additive | 0.073 | yes |
| KY - Color Chords | 0.017 | yes |

All converted cleanly.

---

## Re-deriving the mapping for a future Serum version

If Serum 2.x changes the on-disk schema and the converter starts producing the
init patch (RMS matches the no-load baseline), follow this playbook:

1. **Capture default state.** Open Serum in DawDreamer, close the window
   without doing anything, save the state. This is the "all defaults"
   reference.
2. **Capture a loaded state.** Reopen Serum, load a single known preset via
   the GUI, save the state. This is the ground truth.
3. **Render both.** Render the same MIDI through both states. Confirm audio
   differs significantly (RMS or peak). If audio is identical, the GUI load
   isn't propagating to the processor — the bug is in DawDreamer or Serum, not
   the format.
4. **Decode both states' IComponent CBOR.** Compare with `cbor2.loads`.
5. **Diff top-level keys.** What's only in preset? What's only in processor?
   Update `_PRESET_ONLY_TOPLEVEL_KEYS` / `_PROCESSOR_EXTRA_TOPLEVEL` in
   `converter.py`.
6. **Diff a few specific modules.** Look at `Arp0`, `Env0`, `Oscillator0`,
   `Macro0`, `ModSlot0`. For each: are `plainParams` the same shape? Is the
   `"default"` sentinel still in use? Are there new sub-keys to strip or pass
   through?
7. **Update version markers.** `PROCESSOR_PRODUCT_VERSION` and
   `PROCESSOR_FORMAT_VERSION` in `converter.py`.
8. **Re-run the validation table from Phase 6.** Converter RMS within ~5% of
   GUI-loaded RMS, both clearly distinct from no-load RMS = pass.
