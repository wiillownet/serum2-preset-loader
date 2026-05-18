"""Convert .SerumPreset files into VST3 state blobs that DawDreamer can load."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .converter import (
    PROCESSOR_FORMAT_VERSION,
    PROCESSOR_PRODUCT_VERSION,
    SUPPORTED_XFERJSON_VERSION,
    convert_preset_bytes,
    convert_preset_file,
    preset_cbor_to_processor_cbor,
    read_preset_metadata,
)
from .wrappers import (
    build_juce_vst3_state,
    juce_memoryblock_b64decode,
    juce_memoryblock_b64encode,
    unwrap_xferjson,
    wrap_xferjson_precompressed,
)

try:
    __version__ = _pkg_version("serum2-preset-loader")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = [
    "convert_preset_bytes",
    "convert_preset_file",
    "preset_cbor_to_processor_cbor",
    "read_preset_metadata",
    "build_juce_vst3_state",
    "juce_memoryblock_b64decode",
    "juce_memoryblock_b64encode",
    "unwrap_xferjson",
    "wrap_xferjson_precompressed",
    "PROCESSOR_FORMAT_VERSION",
    "PROCESSOR_PRODUCT_VERSION",
    "SUPPORTED_XFERJSON_VERSION",
]
