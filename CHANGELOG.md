# Changelog

Notable changes to serum2-preset-loader. Versions follow the policy in
`docs/decisions.md`. Releases before 0.5.1 are described in their GitHub
release notes.

## [0.5.1] - 2026-10-28

### Fixed

- `build_controller_state` now copies the preset's `midiMap` (MIDI CC map) and
  each FX rack's `proxyParams` (FX proxy links) into the controller state,
  unchanged. Before, a state built with the loader lost both. A preset without
  them gets neither key, which matches what Serum 2.1.5 writes in its own save.
  The processor state and `convert_*` output are unchanged.

### Changed

- CI now runs the tests on macOS and Windows as well as Linux.
