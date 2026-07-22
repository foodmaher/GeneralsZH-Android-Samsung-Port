#!/usr/bin/env python3
"""Synthetic tests for the Android BCn-to-RGBA8 DDS converter."""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts" / "tooling" / "assets"))

import convert_dds_rgba8 as converter  # noqa: E402


def _colour_block(colour0: int, colour1: int, indices: list[int]) -> bytes:
    packed_indices = sum((index & 0x3) << (pixel * 2) for pixel, index in enumerate(indices))
    return struct.pack("<HHI", colour0, colour1, packed_indices)


def _compressed_dds(width: int, height: int, fourcc: bytes, mips: list[bytes]) -> bytes:
    flags = (
        converter.DDSD_CAPS
        | converter.DDSD_HEIGHT
        | converter.DDSD_WIDTH
        | converter.DDSD_PIXELFORMAT
    )
    caps = converter.DDSCAPS_TEXTURE
    if len(mips) > 1:
        flags |= converter.DDSD_MIPMAPCOUNT
        caps |= converter.DDSCAPS_COMPLEX | converter.DDSCAPS_MIPMAP
    values = [
        converter.DDS_HEADER_SIZE,
        flags,
        height,
        width,
        len(mips[0]),
        0,
        len(mips),
        *([0] * 11),
        converter.DDS_PIXEL_FORMAT_SIZE,
        converter.DDPF_FOURCC,
        struct.unpack("<I", fourcc)[0],
        0,
        0,
        0,
        0,
        0,
        caps,
        0,
        0,
        0,
        0,
    ]
    return converter.DDS_MAGIC + struct.pack("<31I", *values) + b"".join(mips)


def _rgba_pixels(output: bytes) -> list[tuple[int, int, int, int]]:
    pixels = []
    for offset in range(converter.DDS_DATA_OFFSET, len(output), 4):
        blue, green, red, alpha = output[offset : offset + 4]
        pixels.append((red, green, blue, alpha))
    return pixels


def _big_archive(entries: list[tuple[str, bytes]]) -> bytes:
    encoded_entries = [(name.encode("cp1252"), data) for name, data in entries]
    directory_size = converter.BIG_HEADER_SIZE + sum(
        8 + len(name) + 1 for name, _data in encoded_entries
    )
    data_offset = directory_size
    directory = bytearray()
    payload = bytearray()
    for name, data in encoded_entries:
        directory.extend(struct.pack(">II", data_offset, len(data)))
        directory.extend(name)
        directory.append(0)
        payload.extend(data)
        data_offset += len(data)
    archive_size = converter.BIG_HEADER_SIZE + len(directory) + len(payload)
    return (
        converter.BIG_MAGIC
        + struct.pack("<I", archive_size)
        + struct.pack(">II", len(entries), directory_size)
        + directory
        + payload
    )


class ConvertDdsRgba8Tests(unittest.TestCase):
    def test_bc1_block_palette(self) -> None:
        indices = [0, 1, 2, 3] * 4
        source = _compressed_dds(
            4,
            4,
            b"DXT1",
            [_colour_block(0xF800, 0x07E0, indices)],
        )
        image, output = converter.convert_dds_bytes(source)
        pixels = _rgba_pixels(output)
        self.assertEqual(image.format_name, "BC1")
        self.assertEqual(pixels[0], (255, 0, 0, 255))
        self.assertEqual(pixels[1], (0, 255, 0, 255))
        self.assertEqual(pixels[2], (170, 85, 0, 255))
        self.assertEqual(pixels[3], (85, 170, 0, 255))

        pixel_format = struct.unpack_from("<8I", output, 4 + 72)
        self.assertEqual(
            pixel_format,
            (
                converter.DDS_PIXEL_FORMAT_SIZE,
                converter.DDPF_RGB | converter.DDPF_ALPHAPIXELS,
                0,
                32,
                0x00FF0000,
                0x0000FF00,
                0x000000FF,
                0xFF000000,
            ),
        )

    def test_bc1_one_bit_alpha_and_sub_block_dimensions(self) -> None:
        indices = [3] + [0] * 15
        source = _compressed_dds(
            2,
            1,
            b"DXT1",
            [_colour_block(0x0000, 0xFFFF, indices)],
        )
        _image, output = converter.convert_dds_bytes(source)
        self.assertEqual(_rgba_pixels(output), [(0, 0, 0, 0), (0, 0, 0, 255)])

    def test_bc2_explicit_alpha(self) -> None:
        alpha_bits = sum(pixel << (pixel * 4) for pixel in range(16))
        block = alpha_bits.to_bytes(8, "little") + _colour_block(
            0xFFFF, 0x0000, [0] * 16
        )
        source = _compressed_dds(4, 4, b"DXT3", [block])
        image, output = converter.convert_dds_bytes(source)
        self.assertEqual(image.format_name, "BC2")
        self.assertEqual([pixel[3] for pixel in _rgba_pixels(output)], [value * 17 for value in range(16)])

    def test_bc3_interpolated_alpha(self) -> None:
        alpha_indices = sum((pixel % 8) << (pixel * 3) for pixel in range(16))
        block = bytes((255, 0)) + alpha_indices.to_bytes(6, "little") + _colour_block(
            0xFFFF, 0x0000, [0] * 16
        )
        source = _compressed_dds(4, 4, b"DXT5", [block])
        image, output = converter.convert_dds_bytes(source)
        self.assertEqual(image.format_name, "BC3")
        self.assertEqual(
            [pixel[3] for pixel in _rgba_pixels(output)[:8]],
            [255, 0, 218, 182, 145, 109, 72, 36],
        )

    def test_bc3_six_step_alpha_mode(self) -> None:
        alpha_indices = sum((pixel % 8) << (pixel * 3) for pixel in range(16))
        block = bytes((0, 100)) + alpha_indices.to_bytes(6, "little") + _colour_block(
            0xFFFF, 0x0000, [0] * 16
        )
        source = _compressed_dds(4, 4, b"DXT5", [block])
        _image, output = converter.convert_dds_bytes(source)
        self.assertEqual(
            [pixel[3] for pixel in _rgba_pixels(output)[:8]],
            [0, 100, 20, 40, 60, 80, 0, 255],
        )

    def test_non_multiple_of_four_and_all_mips(self) -> None:
        red = _colour_block(0xF800, 0x07E0, [0] * 16)
        green = _colour_block(0x07E0, 0x001F, [0] * 16)
        source = _compressed_dds(5, 3, b"DXT1", [red + red, green])
        image, output = converter.convert_dds_bytes(source)
        self.assertEqual([(mip.width, mip.height) for mip in image.mip_levels], [(5, 3), (2, 1)])
        self.assertEqual(len(output), converter.DDS_DATA_OFFSET + (5 * 3 + 2 * 1) * 4)
        self.assertEqual(struct.unpack_from("<I", output, 4 + 24)[0], 2)

    def test_corrupt_and_unsupported_headers(self) -> None:
        with self.assertRaises(converter.DdsError):
            converter.parse_compressed_dds(b"not a DDS")

        unsupported = _compressed_dds(
            4, 4, b"DXT2", [b"\0" * 16]
        )
        with self.assertRaises(converter.UnsupportedDdsError):
            converter.parse_compressed_dds(unsupported)

        corrupt = bytearray(
            _compressed_dds(4, 4, b"DXT1", [b"\0" * 8])
        )
        struct.pack_into("<I", corrupt, 4, 120)
        with self.assertRaises(converter.DdsError):
            converter.parse_compressed_dds(bytes(corrupt))

    def test_tree_reports_unsupported_and_failed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            input_root = temporary / "Input"
            output_root = temporary / "Output"
            input_root.mkdir()
            (input_root / "Unsupported.dds").write_bytes(
                _compressed_dds(4, 4, b"DXT2", [b"\0" * 16])
            )
            (input_root / "Malformed.dds").write_bytes(b"DDS ")

            summary = converter.convert_tree(input_root, output_root)

            self.assertEqual(summary.unsupported, 1)
            self.assertEqual(summary.failed, 1)
            manifest = json.loads(
                (
                    output_root
                    / converter.PROFILE_RELATIVE_ROOT
                    / converter.MANIFEST_NAME
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["files"], [])

    def test_tree_preserves_paths_and_is_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            input_root = temporary / "Input Assets"
            output_root = temporary / "Generated Assets"
            source_path = input_root / "Art" / "Textures" / "Synthetic BC1.DDS"
            source_path.parent.mkdir(parents=True)
            source_path.write_bytes(
                _compressed_dds(
                    4,
                    4,
                    b"DXT1",
                    [_colour_block(0xF800, 0x07E0, [0] * 16)],
                )
            )

            first = converter.convert_tree(input_root, output_root)
            profile_root = output_root / converter.PROFILE_RELATIVE_ROOT
            generated = profile_root / "Art" / "Textures" / "Synthetic BC1.DDS"
            manifest_path = profile_root / converter.MANIFEST_NAME
            first_manifest = manifest_path.read_bytes()

            self.assertEqual(first.converted, 1)
            self.assertTrue(generated.is_file())
            self.assertEqual(
                json.loads(first_manifest)["files"][0]["source"],
                "Art/Textures/Synthetic BC1.DDS",
            )

            second = converter.convert_tree(input_root, output_root)
            self.assertEqual(second.skipped, 1)
            self.assertEqual(first_manifest, manifest_path.read_bytes())

            generated.write_bytes(b"tampered")
            third = converter.convert_tree(input_root, output_root)
            self.assertEqual(third.converted, 1)
            self.assertNotEqual(generated.read_bytes(), b"tampered")

    def test_tree_finds_dds_inside_nested_big_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            input_root = temporary / "Normal Game Folder"
            output_root = temporary / "Generated"
            archive_path = input_root / "Data" / "TexturesZH.big"
            archive_path.parent.mkdir(parents=True)
            source = _compressed_dds(
                4,
                4,
                b"DXT1",
                [_colour_block(0xF800, 0x07E0, [0] * 16)],
            )
            archive_path.write_bytes(
                _big_archive(
                    [
                        ("Art\\Textures\\Synthetic.DDS", source),
                        ("Data\\INI\\Object.ini", b"not a texture"),
                    ]
                )
            )

            summary = converter.convert_tree(input_root, output_root)
            generated = (
                output_root
                / converter.PROFILE_RELATIVE_ROOT
                / "Art"
                / "Textures"
                / "Synthetic.DDS"
            )

            self.assertEqual(summary.archives_scanned, 1)
            self.assertEqual(summary.archive_dds_found, 1)
            self.assertEqual(summary.converted, 1)
            self.assertTrue(generated.is_file())
            self.assertEqual(_rgba_pixels(generated.read_bytes())[0], (255, 0, 0, 255))

    def test_loose_dds_overrides_same_path_inside_big(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            input_root = temporary / "Game"
            output_root = temporary / "Generated"
            relative = Path("Art") / "Textures" / "Priority.dds"
            loose = input_root / relative
            loose.parent.mkdir(parents=True)
            loose.write_bytes(
                _compressed_dds(
                    4, 4, b"DXT1", [_colour_block(0xF800, 0x07E0, [0] * 16)]
                )
            )
            (input_root / "Textures.big").write_bytes(
                _big_archive(
                    [
                        (
                            "Art\\Textures\\Priority.dds",
                            _compressed_dds(
                                4,
                                4,
                                b"DXT1",
                                [_colour_block(0x07E0, 0x001F, [0] * 16)],
                            ),
                        )
                    ]
                )
            )

            summary = converter.convert_tree(input_root, output_root)
            generated = output_root / converter.PROFILE_RELATIVE_ROOT / relative

            self.assertEqual(summary.converted, 1)
            self.assertEqual(summary.duplicate_dds, 1)
            self.assertEqual(_rgba_pixels(generated.read_bytes())[0], (255, 0, 0, 255))

    def test_big_archive_rejects_unsafe_entry_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            input_root = temporary / "Game"
            output_root = temporary / "Generated"
            input_root.mkdir()
            (input_root / "Unsafe.big").write_bytes(
                _big_archive(
                    [
                        (
                            "..\\Outside.dds",
                            _compressed_dds(
                                4,
                                4,
                                b"DXT1",
                                [_colour_block(0xF800, 0x07E0, [0] * 16)],
                            ),
                        )
                    ]
                )
            )

            summary = converter.convert_tree(input_root, output_root)

            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.converted, 0)
            self.assertFalse((temporary / "Outside.dds").exists())


if __name__ == "__main__":
    unittest.main()
