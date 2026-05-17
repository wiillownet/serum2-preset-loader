# Decisions

## [2026-05-17] Rejected pattern: removing exported/duplicate entry points whose intent is documented elsewhere in the repo
**Decision:** Chose not to remove publicly-exported symbols or duplicate entry-point files when their existence is justified by an explicit commit message, README section, or other in-repo author signal.
**Reason:** Two findings in this run proposed removing `juce_memoryblock_b64decode` (promoted to public API in commit 9fd668a) and `examples/render_preset.py` (documented in README as the no-install-required entry point). In both cases the "is this actually used?" question that drove the audit finding has already been answered deliberately by the author; re-flagging adds noise without surfacing new information.
**Source:** audit-validate run on 2026-05-17.
