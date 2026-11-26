"""Run one case against Serum 2 in this fresh process, for test_serum_host.py.

Usage: python serum_host_case.py PLUGIN SPEC_HEX. SPEC_HEX is a CBOR map:

- ``proc`` / ``ctrl``: top-level keys to set in Serum's own init processor /
  controller state before loading it back; ``proc_delete`` lists keys to remove
- ``ctrl_meta``: keys to set in the controller state's JSON metadata
- ``cc``: ``[[number, value], ...]`` MIDI CCs on channel 1, 25 ms apart
- ``read_proc`` / ``read_ctrl`` / ``read_ctrl_meta``: keys to report from the
  state Serum saves after the load and a short note

Prints ``LOADING`` just before the load and ``RESULT <cbor hex>`` at the end.
A load or render that fails exits without ``RESULT``.
"""
import dawdreamer as daw  # DawDreamer must be imported before the other packages

import hashlib
import os
import re
import struct
import sys
import tempfile

import cbor2
import zstandard

from serum2_preset_loader.wrappers import (
    build_juce_vst3_state, juce_memoryblock_b64decode, unwrap_xferjson, wrap_xferjson_precompressed,
)


def read_state(path):
    """(processor meta, tree), (controller meta, tree) from a DawDreamer save."""
    xml = open(path, "rb").read()[8:].rstrip(b"\0").decode()
    out = []
    for tag in ("IComponent", "IEditController"):
        blob = juce_memoryblock_b64decode(re.search(f"<{tag}>(.*?)</{tag}>", xml, re.S).group(1))
        meta, _version, cbor = unwrap_xferjson(blob)
        out.append((meta, cbor2.loads(cbor)))
    return out


def wrap(meta, tree):
    raw = cbor2.dumps(tree)
    compressed = zstandard.ZstdCompressor().compress(raw)
    meta = dict(meta, hash=hashlib.md5(compressed).hexdigest())
    return wrap_xferjson_precompressed(meta, compressed, uncompressed_size=len(raw))


def cc_file(path, ccs):
    """A one-track MIDI file of control changes, 24 ticks (25 ms at 120 bpm) apart."""
    events = b"".join(b"\x18" + bytes([0xB0, num, value]) for num, value in ccs) + b"\x00\xff\x2f\x00"
    with open(path, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480) + b"MTrk" + struct.pack(">I", len(events)) + events)


def main():
    plugin, spec = sys.argv[1], cbor2.loads(bytes.fromhex(sys.argv[2]))
    tmp = tempfile.mkdtemp(prefix="serum_host_case_")
    init, state, saved = (os.path.join(tmp, n) for n in ("init.bin", "state.bin", "saved.bin"))

    engine = daw.RenderEngine(44100, 512)
    synth = engine.make_plugin_processor("serum", plugin)
    engine.load_graph([(synth, [])])
    engine.render(0.05)
    synth.save_state(init)
    (pmeta, proc), (cmeta, ctrl) = read_state(init)
    proc.update(spec.get("proc", {}))
    for key in spec.get("proc_delete", []):
        proc.pop(key, None)
    ctrl.update(spec.get("ctrl", {}))
    cmeta = dict(cmeta, **spec.get("ctrl_meta", {}))
    with open(state, "wb") as f:
        f.write(build_juce_vst3_state(wrap(pmeta, proc), wrap(cmeta, ctrl)))

    print("LOADING", flush=True)
    synth.load_state(state)
    if spec.get("cc"):
        midi = os.path.join(tmp, "cc.mid")
        cc_file(midi, spec["cc"])
        synth.load_midi(midi, clear_previous=True, beats=False, all_events=True)
    synth.add_midi_note(60, 100, 0.2, 0.05)
    engine.render(0.3)
    synth.save_state(saved)

    (_pm, proc2), (cmeta2, ctrl2) = read_state(saved)
    missing = "<absent>"
    result = {
        "proc": {k: proc2.get(k, missing) for k in spec.get("read_proc", [])},
        "ctrl": {k: ctrl2.get(k, missing) for k in spec.get("read_ctrl", [])},
        "ctrl_meta": {k: cmeta2.get(k, missing) for k in spec.get("read_ctrl_meta", [])},
    }
    print("RESULT " + cbor2.dumps(result).hex(), flush=True)


if __name__ == "__main__":
    main()
