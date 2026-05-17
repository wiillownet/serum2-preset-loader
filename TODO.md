# TODO

Open work-items, lowest-effort first. Strikethrough or delete entries as they ship.

## `_PROCESSOR_EXTRA_TOPLEVEL` mutation hardening
Latent only — current values are primitives, so `state.update(_PROCESSOR_EXTRA_TOPLEVEL)` is safe. If anyone ever adds a nested dict to that constant, callers would share state by reference. Cheapest fix: wrap the constant with `types.MappingProxyType` at module scope, or `dict(...)` it on update.

## CLI flags: `--sample-rate`, `--bit-depth`
`_cli.py` hardcodes 44.1 kHz and 16-bit PCM. Decide whether `serum2-render` is a quick demo (leave as-is) or a real tool (surface flags). Blocked on direction.

- [ ] [audit-validate] Decide whether `wrap_xferjson` is needed on the public surface
  - File: src/serum2_preset_loader/wrappers.py:65
  - Why deferred: Exported in `__all__` but has zero internal callers; whether external consumers rely on it (vs composing `zstd.compress` + `wrap_xferjson_precompressed` themselves) is author knowledge that can't be confirmed from inside the repo.
  - Source: audit run on 2026-05-17
