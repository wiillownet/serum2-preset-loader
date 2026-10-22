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

## [2026-10-23] Count CBOR containers before decoding; require string keys

**Decision:** Before `cbor2.loads`, a linear scan of CBOR item heads counts maps and arrays and rejects more than 1M, skipping runs of same-width floats in one step and stopping after 10M steps. After decoding, every map must have string keys. Any exception from `cbor2.loads` becomes `ValueError`.

**Reason:** The second audit found that a 201-byte file of 4M empty lists took ~570 MiB and ~9 s to convert (about half decoding, about half the tree checks), and a 589-byte file of 16M reached ~2.2 GiB, all under the size caps. It also found that non-string map keys reached the converter and raised `AttributeError`, and that cbor2 5.4, still allowed by the dependency floor, raises `TypeError` and `MemoryError` on malformed input. The scan takes 0.13 s on 41M float32 values; its worst case (alternating float widths, single-byte items) stops at the step cap in about 3 s.

**Alternatives considered:** Skipping empty containers in the shared-reference walk, which makes the walk cheaper but leaves the decode itself unbounded. Counting during decoding, which cbor2's C decoder has no hook for (`object_hook` sees maps only). Documenting a per-worker memory limit instead; the README now recommends one as well, for honest files near the 1 GiB cap, which no count limits.

## [2026-10-23] Check value types and nesting depth after decoding

**Decision:** After decoding, every value in the tree must be a map, array, string, bytes, int, float, bool or null, and the tree may nest at most 64 levels. The README's "every corrupt input raises `ValueError`" now holds on cbor2 5 as well as 6.

**Reason:** The third audit found two inputs that still escaped on cbor2 5.4, which the `cbor2>=5.4` floor allows: a stray break byte decodes to a `break_marker` value and conversion failed with `CBOREncodeTypeError`, and 600 nested arrays decode fine but made the converter's recursive copy raise `RecursionError`. cbor2 6 rejects both itself. The test preset holds only those types and nests 4 deep. The type check reuses the walk's existing `set(map(type, ...))` pass, so it stays in C on sample data (0.57 s for 40M floats plus 3000 maps).

**Alternatives considered:** Raising the floor to `cbor2>=6`, which fixes both without code but drops support for environments pinned to cbor2 5. Catching `RecursionError` in the converter, which fixes only one of the two and leaves non-plain values reaching `cbor2.dumps`.

## [2026-10-22] Grow into a two-way codec instead of starting a separate package

**Decision:** The package now reads and writes `.SerumPreset` files (`decode_preset_*`, `encode_preset`, `write_preset_file`) and converts edited trees in memory (`convert_preset_tree`), alongside the existing preset-to-state converter. Released as 0.2.0, an API addition.

**Reason:** A preset generator needs to edit presets and render the variants. The envelope, hash and CBOR-shape knowledge already lives here and serum-render already depends on it; a second package would duplicate reverse-engineered format knowledge that has to change in step whenever Serum does.

**Alternatives considered:** A separate codec package, cleaner in name but two packages tracking one undocumented format. KennethWussmann's serum-preset-packager, which works through JSON and leaves the same field meanings unknown.

## [2026-10-22] Encode CBOR with Serum's float widths; leave the converter on cbor2

**Decision:** `encode_cbor` writes each float as 32-bit when it round-trips exactly and 64-bit otherwise, which reproduces Serum's CBOR byte-for-byte on the test preset. Preset writing uses it. `convert_preset_*` keeps using `cbor2.dumps` (all 64-bit).

**Reason:** cbor2 always writes 64-bit floats, changing every float in a written preset (7823 → 8151 bytes on the fixture). Serum's `setState` already accepts the converter's 64-bit output, and switching would change converted bytes that serum-render users may have pinned against, for no gain.

**Alternatives considered:** Using `encode_cbor` in the converter too. Revisit if Serum ever rejects 64-bit floats in state.

