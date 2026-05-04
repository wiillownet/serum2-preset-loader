"""Convert .SerumPreset files into VST3 state blobs that DawDreamer can load."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

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

try:
    __version__ = _pkg_version("serum2-preset-loader")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = [
    "convert_preset_bytes",
    "convert_preset_file",
    "preset_cbor_to_processor_cbor",
    "build_juce_vst3_state",
    "unwrap_xferjson",
    "wrap_xferjson",
]
