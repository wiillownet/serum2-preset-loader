<!-- Decisions log. Append new entries at the bottom.
     Records the *why* behind non-trivial choices (and what was rejected), so they
     aren't silently re-litigated or reversed. The *what/when* lives in git history;
     this file holds the reasoning git can't. -->

# Decisions

## [2026-09-06] Semantic Versioning

**Decision:** Patch releases carry fixes and performance work with no observable change (0.1.3: the base64 codec and deepcopy removal). Minor releases carry any API addition or changed default; before 1.0 a minor may also break. Same policy as serum-render.

**Reason:** serum-render pins this package by range; the version number alone has to say whether the converted bytes or the API can differ.

## [2026-09-06] Convert in memory, but drop each stage as soon as the next has it

**Decision:** `convert_preset_bytes` deletes the decompressed CBOR, the decoded tree and the processor tree as soon as they are consumed, and `unwrap_xferjson` slices through a memoryview. Peak RSS on a 160 MB sample-based preset: 4.4 GB to 2.9 GB. `read_preset_metadata` reads only the JSON header.

**Reason:** A sample-based preset decodes to ~41 million Python floats; every extra live reference to that tree, or extra copy of the 160 MB input, stacks another gigabyte on the worker converting it. What remains is the cost of decoding the tree at all.

**Alternatives considered:** Transforming the CBOR at the byte level without decoding the sample arrays, the only way below ~2 GB. It means a streaming CBOR rewriter for a change that is a handful of key edits; not worth it until a machine cannot run the current shape at all.

## [2026-10-22] Treat every preset as untrusted; one exception type for bad input

**Decision:** Corrupt or hostile input raises `ValueError` everywhere (`convert_preset_*`, `read_preset_metadata`, `unwrap_xferjson`). Declared sizes are checked before allocation: header JSON ≤ 16 MiB, decompressed CBOR ≤ 1 GiB, and the Zstd frame's own declared size must equal the header's. CBOR shared references (tags 28/29) are rejected. The limits are private constants, so this ships as 0.1.4. That is a patch even though which exceptions are raised changes, and files that used to be accepted (above the caps, or with shared references) are now rejected: no valid Serum preset is affected, and the old behaviour was a crash or a hang, so we treat it as a fix rather than an observable change.

**Reason:** serum-render runs this over folders of downloaded presets in worker pools. A 13 KB file declaring a 400 MiB frame allocated all of it before the size check (`decompress()` honours the frame's declared size and ignores `max_output_size`); a text file passed to `read_preset_metadata` raised `MemoryError`; a file of a few hundred bytes of shared references made conversion hang; and cbor2's errors are not `ValueError`s. Each of these is an OOM kill or an uncaught exception in a worker rather than one skipped preset.

**Alternatives considered:** Streaming decompression with a read cap (`stream_reader`), which bounds memory without trusting the frame but copies the output once more; checking the declared size first gets the same bound with the existing single allocation. Rejecting tags 28/29 at decode via `semantic_decoders`, which cbor2 5.x (still allowed by the `>=5.4` floor) does not support; a post-decode identity walk works on every version.

## [2026-10-22] Grow into a two-way codec instead of starting a separate package

**Decision:** The package now reads and writes `.SerumPreset` files (`decode_preset_*`, `encode_preset`, `write_preset_file`) and converts edited trees in memory (`convert_preset_tree`), alongside the existing preset-to-state converter. Released as 0.2.0, an API addition.

**Reason:** A preset generator needs to edit presets and render the variants. The envelope, hash and CBOR-shape knowledge already lives here and serum-render already depends on it; a second package would duplicate reverse-engineered format knowledge that has to change in step whenever Serum does.

**Alternatives considered:** A separate codec package, cleaner in name but two packages tracking one undocumented format. KennethWussmann's serum-preset-packager, which works through JSON and leaves the same field meanings unknown.

## [2026-10-22] Encode CBOR with Serum's float widths and key order; leave the converter on cbor2

**Decision:** `encode_cbor` writes each float as 32-bit when it round-trips exactly and 64-bit otherwise (never 16-bit, which the test preset never uses, even for 0.0 or 0.5), and `encode_preset` sorts map and header keys alphabetically, as every map in the test preset is. Together they reproduce the test preset's CBOR byte-for-byte. `convert_preset_*` keeps using `cbor2.dumps` (finite floats as 64-bit).

**Reason:** cbor2 writes finite floats as 64-bit by default, changing every float in a written preset (7823 → 8151 bytes on the fixture), and insertion order would put a newly added parameter where Serum wouldn't. Serum's `setState` already accepts the converter's 64-bit output, and switching would change converted bytes that serum-render users may have pinned against, for no gain.

**Alternatives considered:** Using `encode_cbor` in the converter too. Revisit if Serum ever rejects 64-bit floats in state.

## [2026-10-23] Count CBOR containers before decoding; require string keys

**Decision:** Before `cbor2.loads`, a linear scan of CBOR item heads counts maps and arrays and rejects more than 1M, skipping runs of same-width floats in one step and stopping after 10M steps. After decoding, every map must have string keys. Any exception from `cbor2.loads` becomes `ValueError`.

**Reason:** The second audit found that a 201-byte file of 4M empty lists took ~570 MiB and ~9 s to convert (about half decoding, about half the tree checks), and a 589-byte file of 16M reached ~2.2 GiB, all under the size caps. It also found that non-string map keys reached the converter and raised `AttributeError`, and that cbor2 5.4, still allowed by the dependency floor, raises `TypeError` and `MemoryError` on malformed input. The scan takes 0.13 s on 41M float32 values; its worst case (alternating float widths, single-byte items) stops at the step cap in about 3 s.

**Alternatives considered:** Skipping empty containers in the shared-reference walk, which makes the walk cheaper but leaves the decode itself unbounded. Counting during decoding, which cbor2's C decoder has no hook for (`object_hook` sees maps only). Documenting a per-worker memory limit instead; the README now recommends one as well, for honest files near the 1 GiB cap, which no count limits.

## [2026-10-23] Check value types and nesting depth after decoding

**Decision:** After decoding, every value in the tree must be a map, array, string, bytes, int, float, bool or null, and the tree may nest at most 64 levels. The README's "every corrupt input raises `ValueError`" now holds on cbor2 5 as well as 6.

**Reason:** The third audit found two inputs that still escaped on cbor2 5.4, which the `cbor2>=5.4` floor allows: a stray break byte decodes to a `break_marker` value and conversion failed with `CBOREncodeTypeError`, and 600 nested arrays decode fine but made the converter's recursive copy raise `RecursionError`. cbor2 6 rejects both itself. The test preset holds only those types and nests 4 deep. The type check reuses the walk's existing `set(map(type, ...))` pass, so it stays in C on sample data (0.57 s for 40M floats plus 3000 maps).

**Alternatives considered:** Raising the floor to `cbor2>=6`, which fixes both without code but drops support for environments pinned to cbor2 5. Catching `RecursionError` in the converter, which fixes only one of the two and leaves non-plain values reaching `cbor2.dumps`.

## [2026-10-23] Header and tree metadata must agree; writes are unique-temp and atomic

**Decision:** `encode_preset` raises `ValueError` naming any key the header and the tree's top level both hold with different values or types, replacing the earlier copy-header-into-tree behaviour; `set_preset_metadata` sets a field in both. `write_preset_file` writes through a temp file with a random name in the target's folder (`O_EXCL`), keeps an existing file's permission bits, follows symlinks, and accepts `bytes` paths; integer paths are rejected.

**Reason:** Copying the header over the tree silently discarded tree-only edits and could change a type Serum wrote (int `10` over float `10.0`); in the test preset every shared key already agrees, so the check costs nothing on unedited files. The per-process temp name let two threads share one temp file, and the second audit reproduced a "successful" write leaving a corrupt preset. `mkstemp` was not used because it creates files as 0600 and there is no thread-safe way to read the umask.

**Alternatives considered:** Header wins or tree wins (both silently drop an edit on the other side). Adding the thread id to the temp name, which fixes threads but not other processes reusing a pid.

## [2026-10-23] Publish to PyPI from GitHub releases with trusted publishing

**Decision:** Publishing a GitHub release runs `.github/workflows/publish.yml`, which runs the tests, checks the tag equals `v` + the pyproject version, builds, and uploads through PyPI trusted publishing. Same workflow as serum-render, plus the test and tag checks.

**Reason:** 0.1.4 was built and uploaded by hand, which needs build tools and a PyPI token on whichever machine does it, and nothing stops uploading a build whose version doesn't match its tag. Trusted publishing stores no token at all.

**Alternatives considered:** Continuing by hand with `twine` (works, but repeats every release and needs the token). A token stored as a repository secret (simpler PyPI setup, but a long-lived credential that can leak).

## [2026-10-23] A measured table for 9 parameters; absent means default

**Decision:** `params.py` maps 9 DAW parameters (oscillator A unison, detune and wavetable position; filter 1 cutoff and resonance; Env 1 attack, decay, sustain and release) to their tree paths, with a per-parameter conversion between the DAW's 0 to 1 norm and the stored value. `get_param` reads an absent field as the plugin's exact `defaultValue`; `set_param` always writes a float and deletes the field when the stored value equals the default's (relative tolerance 1e-9). Missing modules raise `KeyError` and are never created.

**Reason:** A DawDreamer probe of Serum 2.1.5 set each parameter to 0, 0.25, 0.5, 0.75 and 1 and decoded the saved state. Stored units differ per parameter (seconds as 32·n⁵, voices as min(16, floor(16·n) + 1), wavetable position as 1 + 255·n on a fixed 1 to 256 scale, percent, or the norm itself), so a generator can't write norms directly. Serum omitted every one of the 9 fields in the probe baseline, where all sat at their defaults, so absent means default. Serum does sometimes write a field equal to its default (unison at a norm just above 0 stores 1 voice), so removing it is equivalent rather than byte-identical. The curves were fit to those 5 points each, then re-checked by a second probe at 21 points (0, 0.05, ..., 1), all within float32 rounding; the test fixture holds the 21-point data. Creating a missing module would guess at a structure Serum builds itself.

**Alternatives considered:** Writing the default value explicitly instead of deleting it (Serum likely accepts it, but the file would differ from Serum's own). Probing all 2623 parameters into the table now: the full probe mapped 457 of them, but 9 with checked curves are enough to start a search loop, and FX parameters weren't mapped at all because the baseline had no FX loaded.

## [2026-10-23] Extend the parameter table to oscillators B and C, filter 2 and envelopes 2 to 4

**Decision:** `PARAMS` grows from 9 to 29 entries: unison, detune and wavetable position for oscillators B and C, cutoff and resonance for filter 2, and attack, decay, sustain and release for Env 2 to 4. They reuse the existing curves. The table is built in three loops (oscillators, filters, envelopes) with each DAW index listed explicitly.

**Reason:** In the full Serum 2.1.5 probe, all 20 sit at the matching path in their own module (`Oscillator1`, `WTOsc1`, `VoiceFilter1`, `Env1`, ...) and match the 9 measured curves at the 5 probe points, then at 21 points in a second probe, within float32 rounding. Two differences: the Env 2 to 4 attack default (`defaultValue` 0.10999999940395355, about 0.5 ms, against Env 1's 0.1256), kept per parameter, and Env 2 to 4 sustain, which Serum displays as a percent where Env 1 shows dB (same stored value). DAW indexes are listed rather than computed because their spacing is irregular (Env 1 to 4 attack: 224, 232, 242, 252).

**Alternatives considered:** Writing out all 29 rows by hand (clearer one at a time, but 20 near-copies to keep in sync). Computing indexes from offsets (shorter, but wrong for the envelopes).

## [2026-10-23] Wavetable position raises on a non-wavetable oscillator

**Decision:** `get_param` and `set_param` raise `ValueError` for `osc_*_wt_pos` unless that oscillator's `kParamType` is absent or `"kOsc_WT"`. A malformed `plainParams` (anything but a map or the string `"default"`) raises `ValueError` in both functions, and setting a default on a module without a `plainParams` map changes nothing.

**Reason:** An audit found that DAW slots 53 to 74 of each oscillator (wavetable position among them) control other fields on sample, multisample, granular and spectral oscillators. Every oscillator keeps a `WTOsc#` module whatever its type (all 2316 in the factory presets do), so writing `kParamTablePos` there succeeds but does nothing; 554 of those oscillators are not wavetable. An error is easier to act on than a silent no-op. The same audit found `get_param` raising `TypeError` or silently returning the default for a malformed `plainParams` where `set_param` raised `ValueError`.

**Alternatives considered:** Writing the field anyway and documenting that it has no effect (a search loop would waste evaluations on it). Mapping each oscillator type's own slot 59 parameter (timbre, density, spectral cutoff); that needs a probe per oscillator type and is left for later.

## [2026-10-25] Pass the preset's own version through to the state

**Decision:** The processor state carries the preset's own `version` and `productVersion` (an int version is written as a float, a non-number or non-string raises `ValueError`). The constants become the fallback for a tree without them, now 11.0 and `"2.1.5"`. The converter no longer adds `killEnvsGracefullyCompat: true` and no longer strips `PitchQuantizer{n}.scaleName`. 0.4.0, since rendered output changes.

**Reason:** Serum migrates a state from the version it declares. Stamping 10.0 on every preset made Serum skip the migration steps for presets saved before 2.1.4, which is nearly all factory presets (versions 3.0 to 9.0): FX Distortion Freq rescaling, ModSlot output, behaviour-compat flags such as `clampMasterTuningModulation` and `granularUnisonModOscCompat`, and a multisample loop mode. 63 of 64 sampled presets came out different, and on two kick presets the difference from Serum's own migration rendered at -12.7 and -17.1 dB relative to the signal, against a re-render noise floor of -24 to -41 dB. The added `killEnvsGracefullyCompat` was copied from a captured DAW session; Serum sets it itself for the presets that need it. Without `scaleName`, Serum loads "User Scale" and counts its scale index up on every load. Checked against Live 12's saved state for ARP - Aardvark under Serum 2.1.5: Serum's re-save of the converter output went from 10 differences to 3 (`mpeEnabled`, set by the host, and `noteOnExpressionCompat` and `roundingModeCompat`, which Serum forces on for any state below 11.0 even when the state sets them false).

**Alternatives considered:** Stamping 11.0 and writing the migrated values ourselves (matches Live exactly for Aardvark, but Serum's migration depends on each preset's content, so it would need reimplementing). Stamping 11.0 alone (avoids the two forced flags but skips every migration step).

## [2026-10-25] Document oscillator type fields instead of adding them to the table

**Decision:** The type-specific oscillator fields (granular, multisample, spectral and the extra wavetable ones) go into a README table of stored units and defaults, not into `PARAMS`. The norm API for them waits for a caller. When one appears, start with the most used fields (multisample envelope, granular density, grain length and random amounts, spectral shift and frequency range), not a slot range, and probe oscillators B and C rather than assume them.

**Reason:** Nothing calls `get_param`, `set_param` or `PARAMS` today (serum-render, serum-render-gui and alskit only convert). Three reviews agreed the fields can be measured (the curves are linear, n⁴, n⁵ and 10·2205ⁿ, confirmed on oscillator A at 30 norms each), but adding them to the table needs a type guard per entry, a mode guard for granular BPM mode, an `applies(tree, name)` helper so a search loop doesn't catch `ValueError` for discovery, tests keyed by name (one host slot maps to up to five fields), and a re-probe at each Serum update. The host `defaultValue` is wrong for most of these fields, so the defaults were found from what Serum omits; the multisample envelope's attack and release defaults never showed up in 400 probe values and are left undetermined.

**Alternatives considered:** Adding slots 59 to 62 for every type to `PARAMS` (45 entries, about half of them rarely used: grain reverse is set in none of the 66 factory granular oscillators). All slots 59 to 74 (about 100, including a string enum that doesn't fit the norm model). Only slot 59 (9 entries, the cheapest start once there is a caller).

## [2026-10-25] Build the controller state from the preset

**Decision:** Two new functions: `build_processor_state(tree)` returns the processor (IComponent) XferJson bytes that `convert_*` already wrapped for DawDreamer, and `build_controller_state(tree, preset_path=None)` returns the controller (IEditController) bytes. The controller state copies the preset's names and UI panels and keeps only the naming and display sub-keys of each module (macro names, clip names and grid, LFO curve names, FX rack names); it adds each macro's preset value as `default` and each FX's numeric preset values as `defaultPlainValues`, and uses the tree's own version markers. `convert_*` stays processor-only. 0.5.0.

**Reason:** alskit decided (its decisions.md, 2026-10-20) to place opaque processor and controller bytes into Live sets and leave building them to this library; without a controller state Serum in Live shows "Init" and default macro names. Measured against Live 12's saved state for ARP - Aardvark under Serum 2.1.5: after Serum loads processor plus built controller and re-saves, the controller differs from Live's only in one FX mode Serum stores as text in the preset (Serum rejects a string in `defaultPlainValues`, so text fields are left out and take Serum's default), against 39 differences with no controller. Across 757 installed presets, Serum's re-save keeps the name, author, preset path and macro names in all but SY - Tetris, where Serum drops the names of macros 5 and 7 whatever they are called (a name moved to another slot survives), which points at Serum's own handling of those macros. `convert_*` stays processor-only so rendering doesn't change.

**Alternatives considered:** Adding the controller to the DawDreamer blob from `convert_*` (no effect on audio, and it changes serum-render's input for nothing). Mapping text FX fields to their index for `defaultPlainValues` (needs an enum table per FX field; only affects double-click reset). A single function returning both blobs (two functions let a caller build one without the other).
