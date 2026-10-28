# Changelog

Notable changes to serum2-preset-loader. Versions follow the policy in
`docs/decisions.md`. Releases before 0.5.1 are described in their GitHub
release notes.

## [0.5.2] - 2026-10-28

### Fixed

- `build_controller_state` now checks each FX rack's `proxyParams` instead
  of copying them unchanged. An entry Serum 2.1.5 ignores (a slot key other
  than `"0"` to `"15"`, a rack with no FX, a `proxyParams` that isn't a map)
  or crashes the host on (an entry that isn't a map, an `fxID` or
  `paramIndex` that isn't a number, an `fxID` past the rack's FX, a
  `paramIndex` past that FX type's parameter count) is dropped with a
  warning. Valid entries are copied as stored.
- An FX entry with no `type`, or one outside 0 to 15, raises the new
  `InvalidFXTypeError` (a `ValueError`) in `build_controller_state`,
  `build_processor_state` and `convert_*`, since Serum crashes loading it.

Every installed factory preset gives the same bytes as 0.5.1.

## [0.5.1] - 2026-10-28

### Fixed

- `build_controller_state` now copies the preset's `midiMap` (MIDI CC map) and
  each FX rack's `proxyParams` (FX proxy links) into the controller state,
  unchanged. Before, a state built with the loader lost both. A preset without
  them gets neither key, which matches what Serum 2.1.5 writes in its own save.
  The processor state and `convert_*` output are unchanged.

### Changed

- CI now runs the tests on macOS and Windows as well as Linux.
