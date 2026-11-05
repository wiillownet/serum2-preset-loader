# Changelog

Notable changes to serum2-preset-loader. Versions follow the policy in
`docs/decisions.md`. Releases before 0.5.1 are described in their GitHub
release notes.

## [0.6.1] - 2026-11-04

### Fixed

- `build_processor_state`, `convert_*` and `preset_cbor_to_processor_cbor` now
  leave out the file's `mpePitchBendRange` too, unless `keep_mpe=True`. Serum's
  own editor load keeps the instance's range, like `mpeEnabled` and `mpeConfig`,
  so a file with another value left Serum in a state its own load doesn't.
  Serum 2.1.5 reads a state without the key as range 48.

The bytes change for every preset that has the key. Over the 777 presets
installed here (626 Factory, 149 Splice, 2 User), all with range 48, the only
change is the key's removal: the output equals 0.6.0's on the tree without the
key, `keep_mpe=True` and controller states give the same bytes as 0.6.0, and a
sample of 30 loaded into Serum re-saved the same state with range 48.

## [0.6.0] - 2026-11-04

### Changed

- `build_processor_state`, `convert_*` and `preset_cbor_to_processor_cbor` no
  longer write the file's `mpeEnabled` and `mpeConfig` into the processor
  state, so output changes for every preset that has them. Serum 2.1.5 reads a
  state without them as MPE off and config 0. Pass the new keyword `keep_mpe=True`
  for the old output, byte for byte.

### Added

- `build_controller_state(tree, header=header)` carries `presetAuthorLocked`
  from the file's header into the controller state and its metadata, as Serum
  does. A packed preset has the flag only in the header, so callers need to
  pass `header=`. Without it, a flag at the tree's root (a preset saved over by
  Serum) is still carried. A preset with no flag gets the same bytes as before.

Over the 777 presets installed here (none has the flag), `keep_mpe=True` gives
the same bytes as 0.5.2, the default gives the bytes 0.5.2 gives with the two
keys removed from the tree, and controller states are unchanged.

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
