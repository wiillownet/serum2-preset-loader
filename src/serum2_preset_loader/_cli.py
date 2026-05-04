"""Console-script entry point: render one .SerumPreset to a WAV file.

Installed as ``serum2-render`` when the ``[render]`` extra is present.
Imports of dawdreamer/scipy/numpy are deferred so just ``--help`` works
without the heavy deps installed.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

from .converter import convert_preset_file


SAMPLE_RATE = 44100
BUFFER_SIZE = 512


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="serum2-render",
        description="Render a .SerumPreset to a WAV file via DawDreamer.",
    )
    p.add_argument("vst3", help="path to Serum2.vst3")
    p.add_argument("preset", help="path to a .SerumPreset file")
    p.add_argument("out_wav", help="path to write the rendered WAV")
    p.add_argument("--midi-note", type=int, default=60,
                   help="MIDI note number to play (default: 60 / middle C)")
    p.add_argument("--velocity", type=int, default=127,
                   help="MIDI note velocity (default: 127)")
    p.add_argument("--note-duration", type=float, default=2.0,
                   help="seconds the note is held (default: 2.0)")
    p.add_argument("--render-duration", type=float, default=3.0,
                   help="total render length in seconds, including release tail "
                        "(default: 3.0)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        import dawdreamer as daw
        import numpy as np
        from scipy.io import wavfile
    except ImportError as e:
        print(
            f"error: missing render dependency ({e.name}). "
            f"install with: pip install 'serum2-preset-loader[render]'",
            file=sys.stderr,
        )
        return 2

    state_blob = convert_preset_file(args.preset)
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = os.path.join(tmp_dir, "state.bin")
        with open(state_path, "wb") as f:
            f.write(state_blob)

        engine = daw.RenderEngine(SAMPLE_RATE, BUFFER_SIZE)
        synth = engine.make_plugin_processor("serum", args.vst3)
        synth.load_state(state_path)
        synth.clear_midi()
        synth.add_midi_note(args.midi_note, args.velocity, 0.0, args.note_duration)
        engine.load_graph([(synth, [])])
        engine.render(args.render_duration)
        audio = engine.get_audio().T
        audio_i16 = np.clip(audio * 32767.0, -32768.0, 32767.0).astype(np.int16)

    wavfile.write(args.out_wav, SAMPLE_RATE, audio_i16)
    print(f"wrote {args.out_wav}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
