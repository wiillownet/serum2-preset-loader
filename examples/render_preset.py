"""Render one .SerumPreset to a WAV file using DawDreamer.

Usage:
    python examples/render_preset.py <Serum2.vst3> <preset.SerumPreset> <out.wav>

If the package is installed, prefer the ``serum2-render`` console script
instead — this script is a thin wrapper that just calls into it.

Requires the optional render extras: ``pip install dawdreamer scipy numpy``.
"""
import sys

from serum2_preset_loader._cli import main


if __name__ == "__main__":
    sys.exit(main())
