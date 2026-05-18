## 2026-05-17 — audit-validate

### Applied (0) — no changes applied
(none)

### Deferred (1)
See `TODO.md` for individual entries.

**Resolution (2026-05-17):** The deferred `wrap_xferjson` question was resolved
by checking the only known external consumer (`vst-render`), which imports
`convert_preset_file` exclusively. Dropped `wrap_xferjson` from the top-level
package's `__all__` and `__init__` imports; the function itself remains in
`wrappers.py` and can be re-exported if a real consumer surfaces.

### Rejected (2)
- `src/serum2_preset_loader/wrappers.py:127` (`juce_memoryblock_b64decode` demotion) — recent commit 9fd668a explicitly promoted this to public API; rejection records the author's standing decision.
- `examples/render_preset.py` (delete as duplicate) — at the time of this audit, README documented this as the no-install-required entry point; intentional duplication. (File has since moved to the vst-render repo in commit fdda99e.)

### Stale (0)
