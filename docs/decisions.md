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
