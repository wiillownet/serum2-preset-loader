"""Read, write and convert Serum 2 .SerumPreset files.

Converts presets into VST3 state blobs that DawDreamer can load, and decodes
and encodes the preset files themselves.
"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from .codec import (
    PRESET_FORMAT_VERSION,
    decode_preset_bytes,
    decode_preset_file,
    encode_cbor,
    encode_preset,
    write_preset_file,
)
from .converter import (
    PROCESSOR_FORMAT_VERSION,
    PROCESSOR_PRODUCT_VERSION,
    SUPPORTED_XFERJSON_VERSION,
    convert_preset_bytes,
    convert_preset_file,
    convert_preset_tree,
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
    "decode_preset_bytes",
    "decode_preset_file",
    "encode_preset",
    "write_preset_file",
    "encode_cbor",
    "PRESET_FORMAT_VERSION",
    "convert_preset_bytes",
    "convert_preset_file",
    "convert_preset_tree",
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
