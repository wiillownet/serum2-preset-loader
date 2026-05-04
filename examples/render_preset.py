"""Render one .SerumPreset to a WAV file using DawDreamer.

Usage:
    python examples/render_preset.py <Serum2.vst3> <preset.SerumPreset> <out.wav>

Requires the optional render extras: ``pip install dawdreamer scipy numpy``.
"""
import os
import sys
import tempfile

import dawdreamer as daw
import numpy as np
from scipy.io import wavfile

from serum2_preset_loader import convert_preset_file


SAMPLE_RATE = 44100
BUFFER_SIZE = 512
MIDI_NOTE = 60
MIDI_VELOCITY = 127
NOTE_DURATION = 2.0
RENDER_DURATION = 3.0


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    vst3_path, preset_path, wav_path = sys.argv[1:4]

    state_blob = convert_preset_file(preset_path)
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = os.path.join(tmp_dir, "state.bin")
        with open(state_path, "wb") as f:
            f.write(state_blob)

        engine = daw.RenderEngine(SAMPLE_RATE, BUFFER_SIZE)
        synth = engine.make_plugin_processor("serum", vst3_path)
        synth.load_state(state_path)
        synth.clear_midi()
        synth.add_midi_note(MIDI_NOTE, MIDI_VELOCITY, 0.0, NOTE_DURATION)
        engine.load_graph([(synth, [])])
        engine.render(RENDER_DURATION)

    audio = engine.get_audio().T
    audio_i16 = np.clip(audio * 32767.0, -32768.0, 32767.0).astype(np.int16)
    wavfile.write(wav_path, SAMPLE_RATE, audio_i16)
    print(f"wrote {wav_path}")


if __name__ == "__main__":
    main()
