"""Convert .SerumPreset files into VST3 state blobs that DawDreamer can load."""
from .converter import (
    convert_preset_bytes,
    convert_preset_file,
    preset_cbor_to_processor_cbor,
)
from .wrappers import (
    build_juce_vst3_state,
    unwrap_xferjson,
    wrap_xferjson,
)

__all__ = [
    "convert_preset_bytes",
    "convert_preset_file",
    "preset_cbor_to_processor_cbor",
    "build_juce_vst3_state",
    "unwrap_xferjson",
    "wrap_xferjson",
]
__version__ = "0.1.0"
