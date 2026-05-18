# TODO

Open work-items, lowest-effort first. Strikethrough or delete entries as they ship.

- [ ] [audit-validate] Decide whether `wrap_xferjson` is needed on the public surface
  - File: src/serum2_preset_loader/wrappers.py:65
  - Why deferred: Exported in `__all__` but has zero internal callers; whether external consumers rely on it (vs composing `zstd.compress` + `wrap_xferjson_precompressed` themselves) is author knowledge that can't be confirmed from inside the repo.
  - Source: audit run on 2026-05-17
