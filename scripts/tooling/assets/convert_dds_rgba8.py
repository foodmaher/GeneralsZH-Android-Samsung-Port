#!/usr/bin/env python3
"""Convert classic BC1/BC2/BC3 DDS assets to A8R8G8B8 DDS overlays.

The implementation intentionally uses only the Python standard library. The
repository's C++ DDS decoder is tied to engine surface abstractions and does
not implement BC2, so reusing it as a Linux/GitHub Actions host tool would be
larger and less auditable than this bounded decoder.

The input may be an extracted asset tree or a normal game-data directory. The
tool recursively discovers loose DDS files and DDS entries inside BIGF
archives; archives are read directly and are never extracted or modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


DDS_MAGIC = b"DDS "
DDS_HEADER_SIZE = 124
DDS_PIXEL_FORMAT_SIZE = 32
DDS_DATA_OFFSET = 4 + DDS_HEADER_SIZE

DDPF_ALPHAPIXELS = 0x00000001
DDPF_FOURCC = 0x00000004
DDPF_RGB = 0x00000040

DDSCAPS_COMPLEX = 0x00000008
DDSCAPS_TEXTURE = 0x00001000
DDSCAPS_MIPMAP = 0x00400000
DDSCAPS2_CUBEMAP = 0x00000200
DDSCAPS2_VOLUME = 0x00200000

DDSD_CAPS = 0x00000001
DDSD_HEIGHT = 0x00000002
DDSD_WIDTH = 0x00000004
DDSD_PITCH = 0x00000008
DDSD_PIXELFORMAT = 0x00001000
DDSD_MIPMAPCOUNT = 0x00020000

PROFILE_RELATIVE_ROOT = Path("TextureFallback") / "RGBA8"
MANIFEST_NAME = "texture-fallback-manifest.json"
PROFILE_NAME = "texture-fallback.cfg"
MANIFEST_VERSION = 1

BIG_MAGIC = b"BIGF"
BIG_HEADER_SIZE = 16
BIG_MAX_PATH_BYTES = 4096

FOURCC_FORMATS = {
    b"DXT1": ("BC1", 8),
    b"DXT3": ("BC2", 16),
    b"DXT5": ("BC3", 16),
}


class DdsError(ValueError):
    """Malformed DDS input."""


class UnsupportedDdsError(DdsError):
    """Well-formed DDS input outside the proof-of-concept format set."""


class BigArchiveError(ValueError):
    """Malformed or unsupported BIG archive."""


@dataclass(frozen=True)
class MipLevel:
    width: int
    height: int
    data: bytes


@dataclass(frozen=True)
class CompressedDds:
    format_name: str
    width: int
    height: int
    mip_levels: Tuple[MipLevel, ...]


@dataclass
class ConversionSummary:
    converted: int = 0
    skipped: int = 0
    unsupported: int = 0
    failed: int = 0
    archives_scanned: int = 0
    archive_dds_found: int = 0
    loose_dds_found: int = 0
    duplicate_dds: int = 0


@dataclass(frozen=True)
class DdsSource:
    """A loose file or a bounded byte range in a BIG archive."""

    relative_key: str
    container: Path
    offset: int = 0
    size: int | None = None

    @property
    def origin(self) -> str:
        if self.size is None:
            return str(self.container)
        return f"{self.container}:{self.offset}+{self.size}"

    def read_bytes(self) -> bytes:
        if self.size is None:
            return self.container.read_bytes()
        with self.container.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(self.size)
        if len(data) != self.size:
            raise BigArchiveError(
                f"short read for {self.relative_key!r} in {self.container}"
            )
        return data


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _maximum_mip_count(width: int, height: int) -> int:
    return max(width, height).bit_length()


def parse_compressed_dds(data: bytes) -> CompressedDds:
    """Parse a classic 2D DXT1/DXT3/DXT5 DDS with checked mip bounds."""

    if len(data) < DDS_DATA_OFFSET:
        raise DdsError("file is shorter than a classic DDS header")
    if data[:4] != DDS_MAGIC:
        raise DdsError("invalid DDS magic")

    header_size, flags, height, width, _pitch, depth, mip_count = struct.unpack_from(
        "<7I", data, 4
    )
    pixel_format_offset = 4 + 72
    pixel_size, pixel_flags, fourcc_value = struct.unpack_from(
        "<3I", data, pixel_format_offset
    )
    caps2 = struct.unpack_from("<I", data, 4 + 108)[0]

    if header_size != DDS_HEADER_SIZE:
        raise DdsError(f"invalid DDS header size {header_size}")
    if pixel_size != DDS_PIXEL_FORMAT_SIZE:
        raise DdsError(f"invalid DDS pixel-format size {pixel_size}")
    if width == 0 or height == 0:
        raise DdsError("zero texture dimension")
    if depth not in (0, 1) or caps2 & (DDSCAPS2_CUBEMAP | DDSCAPS2_VOLUME):
        raise UnsupportedDdsError("only classic 2D DDS textures are supported")
    if not pixel_flags & DDPF_FOURCC:
        raise UnsupportedDdsError("DDS is not block-compressed")

    fourcc = struct.pack("<I", fourcc_value)
    format_info = FOURCC_FORMATS.get(fourcc)
    if format_info is None:
        display = fourcc.decode("ascii", errors="replace")
        raise UnsupportedDdsError(f"unsupported DDS FourCC {display!r}")
    format_name, block_size = format_info

    mip_count = mip_count or 1
    maximum_mips = _maximum_mip_count(width, height)
    if mip_count > maximum_mips:
        raise DdsError(
            f"invalid mip count {mip_count} for {width}x{height} texture"
        )

    offset = DDS_DATA_OFFSET
    levels: List[MipLevel] = []
    for level in range(mip_count):
        mip_width = max(1, width >> level)
        mip_height = max(1, height >> level)
        block_width = max(1, (mip_width + 3) // 4)
        block_height = max(1, (mip_height + 3) // 4)
        level_size = block_width * block_height * block_size
        end = offset + level_size
        if end > len(data):
            raise DdsError(
                f"truncated mip {level}: need {level_size} bytes, "
                f"have {max(0, len(data) - offset)}"
            )
        levels.append(MipLevel(mip_width, mip_height, data[offset:end]))
        offset = end

    return CompressedDds(format_name, width, height, tuple(levels))


def _expand_565(value: int) -> Tuple[int, int, int]:
    red5 = (value >> 11) & 0x1F
    green6 = (value >> 5) & 0x3F
    blue5 = value & 0x1F
    return (
        (red5 << 3) | (red5 >> 2),
        (green6 << 2) | (green6 >> 4),
        (blue5 << 3) | (blue5 >> 2),
    )


def _colour_palette(block: bytes, bc1_alpha_mode: bool) -> Tuple[Tuple[int, int, int, int], ...]:
    colour0, colour1 = struct.unpack_from("<HH", block, 0)
    red0, green0, blue0 = _expand_565(colour0)
    red1, green1, blue1 = _expand_565(colour1)
    palette: List[Tuple[int, int, int, int]] = [
        (red0, green0, blue0, 255),
        (red1, green1, blue1, 255),
    ]
    if bc1_alpha_mode and colour0 <= colour1:
        palette.append(
            ((red0 + red1) // 2, (green0 + green1) // 2, (blue0 + blue1) // 2, 255)
        )
        palette.append((0, 0, 0, 0))
    else:
        palette.append(
            (
                (2 * red0 + red1) // 3,
                (2 * green0 + green1) // 3,
                (2 * blue0 + blue1) // 3,
                255,
            )
        )
        palette.append(
            (
                (red0 + 2 * red1) // 3,
                (green0 + 2 * green1) // 3,
                (blue0 + 2 * blue1) // 3,
                255,
            )
        )
    return tuple(palette)


def _decode_colour_indices(block: bytes) -> Tuple[int, ...]:
    indices = struct.unpack_from("<I", block, 4)[0]
    return tuple((indices >> (2 * pixel)) & 0x3 for pixel in range(16))


def _decode_bc1_block(block: bytes) -> Tuple[Tuple[int, int, int, int], ...]:
    palette = _colour_palette(block, True)
    return tuple(palette[index] for index in _decode_colour_indices(block))


def _decode_bc2_block(block: bytes) -> Tuple[Tuple[int, int, int, int], ...]:
    alpha_bits = int.from_bytes(block[:8], "little")
    palette = _colour_palette(block[8:], False)
    colours = _decode_colour_indices(block[8:])
    pixels = []
    for pixel, colour_index in enumerate(colours):
        red, green, blue, _alpha = palette[colour_index]
        alpha = ((alpha_bits >> (pixel * 4)) & 0xF) * 17
        pixels.append((red, green, blue, alpha))
    return tuple(pixels)


def _bc3_alpha_palette(alpha0: int, alpha1: int) -> Tuple[int, ...]:
    alphas = [alpha0, alpha1]
    if alpha0 > alpha1:
        alphas.extend(
            ((8 - index) * alpha0 + (index - 1) * alpha1) // 7
            for index in range(2, 8)
        )
    else:
        alphas.extend(
            ((6 - index) * alpha0 + (index - 1) * alpha1) // 5
            for index in range(2, 6)
        )
        alphas.extend((0, 255))
    return tuple(alphas)


def _decode_bc3_block(block: bytes) -> Tuple[Tuple[int, int, int, int], ...]:
    alpha0, alpha1 = block[0], block[1]
    alpha_indices = int.from_bytes(block[2:8], "little")
    alphas = _bc3_alpha_palette(alpha0, alpha1)
    palette = _colour_palette(block[8:], False)
    colours = _decode_colour_indices(block[8:])
    pixels = []
    for pixel, colour_index in enumerate(colours):
        red, green, blue, _alpha = palette[colour_index]
        alpha = alphas[(alpha_indices >> (pixel * 3)) & 0x7]
        pixels.append((red, green, blue, alpha))
    return tuple(pixels)


def decode_mip(format_name: str, mip: MipLevel) -> bytes:
    """Decode one BC mip to tightly packed little-endian BGRA bytes."""

    decoder = {
        "BC1": (_decode_bc1_block, 8),
        "BC2": (_decode_bc2_block, 16),
        "BC3": (_decode_bc3_block, 16),
    }[format_name]
    decode_block, block_size = decoder
    block_width = max(1, (mip.width + 3) // 4)
    block_height = max(1, (mip.height + 3) // 4)
    output = bytearray(mip.width * mip.height * 4)

    for block_y in range(block_height):
        for block_x in range(block_width):
            block_index = block_y * block_width + block_x
            start = block_index * block_size
            pixels = decode_block(mip.data[start : start + block_size])
            for local_y in range(4):
                target_y = block_y * 4 + local_y
                if target_y >= mip.height:
                    continue
                for local_x in range(4):
                    target_x = block_x * 4 + local_x
                    if target_x >= mip.width:
                        continue
                    red, green, blue, alpha = pixels[local_y * 4 + local_x]
                    destination = (target_y * mip.width + target_x) * 4
                    output[destination : destination + 4] = bytes(
                        (blue, green, red, alpha)
                    )
    return bytes(output)


def encode_a8r8g8b8_dds(image: CompressedDds) -> bytes:
    """Encode all source mips as classic A8R8G8B8 DDS."""

    mip_count = len(image.mip_levels)
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PITCH | DDSD_PIXELFORMAT
    caps = DDSCAPS_TEXTURE
    if mip_count > 1:
        flags |= DDSD_MIPMAPCOUNT
        caps |= DDSCAPS_COMPLEX | DDSCAPS_MIPMAP

    values = [
        DDS_HEADER_SIZE,
        flags,
        image.height,
        image.width,
        image.width * 4,
        0,
        mip_count,
        *([0] * 11),
        DDS_PIXEL_FORMAT_SIZE,
        DDPF_RGB | DDPF_ALPHAPIXELS,
        0,
        32,
        0x00FF0000,
        0x0000FF00,
        0x000000FF,
        0xFF000000,
        caps,
        0,
        0,
        0,
        0,
    ]
    header = struct.pack("<31I", *values)
    payload = b"".join(decode_mip(image.format_name, mip) for mip in image.mip_levels)
    return DDS_MAGIC + header + payload


def convert_dds_bytes(data: bytes) -> Tuple[CompressedDds, bytes]:
    image = parse_compressed_dds(data)
    return image, encode_a8r8g8b8_dds(image)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _load_manifest(path: Path) -> Mapping[str, Mapping[str, object]]:
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("format_version") != MANIFEST_VERSION:
            return {}
        return {
            str(entry["source"]): entry
            for entry in document.get("files", [])
            if isinstance(entry, dict) and "source" in entry
        }
    except (OSError, ValueError, TypeError):
        return {}


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def _normalise_archive_path(raw_path: bytes, archive_path: Path) -> str:
    try:
        decoded = raw_path.decode("cp1252")
    except UnicodeDecodeError as error:
        raise BigArchiveError(f"invalid filename encoding in {archive_path}") from error

    decoded = decoded.replace("\\", "/")
    path = PurePosixPath(decoded)
    if (
        not decoded
        or decoded.startswith("/")
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise BigArchiveError(
            f"unsafe entry path {decoded!r} in {archive_path}"
        )
    return path.as_posix()


def _big_dds_sources(archive_path: Path) -> Iterable[DdsSource]:
    """Yield checked DDS entries from one Generals BIGF archive."""

    archive_size = archive_path.stat().st_size
    with archive_path.open("rb") as stream:
        header = stream.read(BIG_HEADER_SIZE)
        if len(header) != BIG_HEADER_SIZE:
            raise BigArchiveError(f"archive header is truncated: {archive_path}")
        if header[:4] != BIG_MAGIC:
            raise BigArchiveError(f"unsupported BIG signature in {archive_path}")

        file_count, directory_end = struct.unpack_from(">II", header, 8)
        if directory_end < BIG_HEADER_SIZE or directory_end > archive_size:
            raise BigArchiveError(
                f"invalid BIG directory size {directory_end} in {archive_path}"
            )
        # Every entry needs at least offset, size, and a terminating NUL.
        if file_count > (directory_end - BIG_HEADER_SIZE) // 9:
            raise BigArchiveError(
                f"invalid BIG file count {file_count} in {archive_path}"
            )

        for entry_index in range(file_count):
            entry_header = stream.read(8)
            if len(entry_header) != 8:
                raise BigArchiveError(
                    f"truncated entry {entry_index} in {archive_path}"
                )
            offset, size = struct.unpack(">II", entry_header)

            name = bytearray()
            while True:
                if stream.tell() >= directory_end:
                    raise BigArchiveError(
                        f"unterminated entry name {entry_index} in {archive_path}"
                    )
                byte = stream.read(1)
                if byte == b"\0":
                    break
                name.extend(byte)
                if len(name) > BIG_MAX_PATH_BYTES:
                    raise BigArchiveError(
                        f"entry name {entry_index} is too long in {archive_path}"
                    )

            relative_key = _normalise_archive_path(bytes(name), archive_path)
            if offset < directory_end or size > archive_size - offset:
                raise BigArchiveError(
                    f"entry {relative_key!r} is outside {archive_path}"
                )
            if PurePosixPath(relative_key).suffix.lower() == ".dds":
                yield DdsSource(relative_key, archive_path, offset, size)


def _discover_sources(
    input_root: Path, summary: ConversionSummary
) -> List[DdsSource]:
    """Find effective loose and archived DDS sources recursively.

    Loose files win because the engine checks its local filesystem before BIG
    archives. For duplicate archive entries, the first archive in the engine's
    case-insensitive path order wins.
    """

    selected: Dict[str, DdsSource] = {}
    loose_files = sorted(
        (
            path
            for path in input_root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".dds"
        ),
        key=lambda path: path.relative_to(input_root).as_posix().casefold(),
    )
    for path in loose_files:
        relative_key = path.relative_to(input_root).as_posix()
        folded = relative_key.casefold()
        if folded in selected:
            summary.duplicate_dds += 1
            continue
        selected[folded] = DdsSource(relative_key, path)
        summary.loose_dds_found += 1

    archives = sorted(
        (
            path
            for path in input_root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".big"
        ),
        key=lambda path: path.relative_to(input_root).as_posix().casefold(),
    )
    for archive_path in archives:
        summary.archives_scanned += 1
        print(f"SCAN      {archive_path.relative_to(input_root).as_posix()}")
        try:
            archive_sources = list(_big_dds_sources(archive_path))
        except (BigArchiveError, OSError, OverflowError, struct.error) as error:
            summary.failed += 1
            print(f"FAILED    {archive_path}: {error}", file=sys.stderr)
            continue
        summary.archive_dds_found += len(archive_sources)
        for source in archive_sources:
            folded = source.relative_key.casefold()
            if folded in selected:
                summary.duplicate_dds += 1
                continue
            selected[folded] = source

    return sorted(selected.values(), key=lambda source: source.relative_key.casefold())


def convert_tree(input_root: Path, output_root: Path) -> ConversionSummary:
    """Convert a source DDS tree into TextureFallback/RGBA8 incrementally."""

    input_root = input_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not input_root.is_dir():
        raise ValueError(f"input directory does not exist: {input_root}")
    if input_root == output_root or _is_within(output_root, input_root):
        raise ValueError("output directory must not be the input directory or one of its children")

    profile_root = output_root / PROFILE_RELATIVE_ROOT
    manifest_path = profile_root / MANIFEST_NAME
    previous = _load_manifest(manifest_path)
    manifest_entries: List[Dict[str, object]] = []
    summary = ConversionSummary()

    for source in _discover_sources(input_root, summary):
        relative_key = source.relative_key
        relative = PurePosixPath(relative_key)
        output_path = profile_root.joinpath(*relative.parts)
        try:
            source_data = source.read_bytes()
            source_hash = _sha256_bytes(source_data)
            previous_entry = previous.get(relative_key)

            if (
                previous_entry is not None
                and previous_entry.get("source_sha256") == source_hash
                and output_path.is_file()
                and previous_entry.get("output_sha256") == _sha256_file(output_path)
            ):
                manifest_entries.append(dict(previous_entry))
                summary.skipped += 1
                print(f"SKIP      {relative_key}")
                continue

            image, output_data = convert_dds_bytes(source_data)
            _atomic_write(output_path, output_data)
        except UnsupportedDdsError as error:
            summary.unsupported += 1
            print(f"UNSUPPORTED {relative_key}: {error}", file=sys.stderr)
            continue
        except (DdsError, OSError, OverflowError, struct.error) as error:
            summary.failed += 1
            print(f"FAILED    {relative_key}: {error}", file=sys.stderr)
            continue

        output_hash = _sha256_bytes(output_data)
        manifest_entries.append(
            {
                "source": relative_key,
                "output": relative.as_posix(),
                "source_format": image.format_name,
                "output_format": "A8R8G8B8",
                "width": image.width,
                "height": image.height,
                "mip_levels": len(image.mip_levels),
                "source_size": len(source_data),
                "output_size": len(output_data),
                "source_sha256": source_hash,
                "output_sha256": output_hash,
            }
        )
        summary.converted += 1
        print(
            f"CONVERT   {relative_key} ({image.format_name} -> A8R8G8B8; "
            f"source={source.origin})"
        )

    manifest_entries.sort(key=lambda entry: str(entry["source"]).casefold())
    manifest = {
        "format_version": MANIFEST_VERSION,
        "profile": "rgba8",
        "files": manifest_entries,
    }
    _atomic_write(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    _atomic_write(profile_root / PROFILE_NAME, b"profile=rgba8\nformat_version=1\n")
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively convert loose and BIG-archived BC1/BC2/BC3 DDS files "
            "to an incremental RGBA8 Android overlay."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="game-data directory containing loose DDS and/or BIG files",
    )
    parser.add_argument("--output", required=True, type=Path, help="generated overlay root")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = convert_tree(args.input, args.output)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    print(
        "SUMMARY "
        f"converted={summary.converted} skipped={summary.skipped} "
        f"unsupported={summary.unsupported} failed={summary.failed} "
        f"archives_scanned={summary.archives_scanned} "
        f"archive_dds_found={summary.archive_dds_found} "
        f"loose_dds_found={summary.loose_dds_found} "
        f"duplicates={summary.duplicate_dds}"
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
