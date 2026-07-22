# Android Non-Adreno RGBA8 Texture Fallback

## Overview

The Android renderer normally sends the game's DDS textures through Direct3D
8, DXVK, and Vulkan. Most retail textures use BC1/DXT1, BC2/DXT3, or BC3/DXT5.
Qualcomm Adreno devices can use their stock driver or the optional Mesa Turnip
driver through libadrenotools. Turnip is an Adreno driver and cannot run on
Samsung Xclipse or ARM Mali GPUs.

Some non-Adreno Vulkan drivers do not expose `textureCompressionBC`. On those
drivers DXVK cannot create or sample the retail BC texture formats. The opt-in
`rgba8` profile converts DDS files offline and installs them as higher-priority
loose files. The original `.big` archives and the normal Adreno path are not
modified.

This is a compatibility proof, not a performance format. ASTC and ETC2 are
possible future optimisations after RGBA8 is proven on real devices.

## Requirements

- A build containing the RGBA8 fallback changes.
- Python 3.10 or newer on Linux, macOS, or Termux.
- `adb` from Android platform-tools when pushing from a host.
- DDS files extracted from game data you legally own.

The proof-of-concept converter reads a directory tree. It does not extract or
repack `.big` archives. Do not commit extracted or converted commercial assets.

The converter uses only the Python standard library. The engine already has a
BC decoder, but it is coupled to W3D texture/surface classes and is not a
portable host tool. Keeping this bounded BC1/BC2/BC3 decoder in the asset tool
avoids a new third-party dependency and licence while remaining auditable in
Linux and GitHub Actions.

## Build the Android proof

With the Android NDK, vcpkg, CMake, Ninja, Meson, pkg-config, and glslang
installed, initialise DXVK and run the existing build/package path:

```bash
git submodule update --init references/fbraz3-dxvk
git -C references/fbraz3-dxvk submodule update --init --depth 1
./scripts/build/android/build-android-zh.sh
./scripts/build/android/package-android-zh.sh --install
```

The `Build Android` GitHub Actions workflow performs the same ARM64 configure,
compile, verification, and APK packaging path. It also runs the synthetic DDS
suite before configuring Android; it never downloads commercial game data.

## Prepare the overlay

Run from the repository root. Paths may contain spaces.

```bash
GX_ANDROID_TEXTURE_FALLBACK=rgba8 \
GX_ANDROID_TEXTURE_INPUT="/path/to/legally extracted GameData" \
GX_ANDROID_TEXTURE_OUTPUT="/path/to/generated Android overlay" \
./scripts/build/android/push-assets-android.sh --prepare-only
```

The generated tree is isolated from the source:

```text
generated Android overlay/
└── TextureFallback/
    └── RGBA8/
        ├── Art/Textures/...
        ├── Data/<language>/Art/Textures/...
        ├── texture-fallback.cfg
        └── texture-fallback-manifest.json
```

The converter:

- supports DXT1, DXT3, and DXT5;
- preserves relative paths, filenames, dimensions, alpha, and every source mip;
- handles non-square and sub-4×4 textures;
- skips unchanged outputs after checking source and output SHA-256 hashes;
- rejects malformed files without writing partial output;
- reports converted, skipped, unsupported, and failed counts.

Unsupported DDS formats are reported and skipped. A malformed supported DDS is
a conversion failure and gives the command a non-zero exit status.

## Push to Android

In the Setup app, note the game folder selected with **Select Game Folder**.
Pass that exact device path:

```bash
GX_ANDROID_TEXTURE_FALLBACK=rgba8 \
GX_ANDROID_TEXTURE_INPUT="/path/to/legally extracted GameData" \
GX_ANDROID_TEXTURE_OUTPUT="/path/to/generated Android overlay" \
GX_ANDROID_GAME_DATA="/sdcard/Download/GeneralsZH" \
./scripts/build/android/push-assets-android.sh
```

For multiple attached devices, add `--serial SERIAL` or set `ANDROID_SERIAL`.
The script pushes only `TextureFallback/RGBA8`; it does not overwrite `.big`
archives or normal loose files.

## Verify the active path

Completely stop and relaunch the game, then open **GeneralsZH Setup → View
Logs**. A successful activation contains lines similar to:

```text
[GX_ANDROID_GRAPHICS] texture_profile=rgba8 asset_directory='/sdcard/Download/GeneralsZH' fallback_directory='/sdcard/Download/GeneralsZH/TextureFallback/RGBA8'
[GX_ANDROID_GRAPHICS] libadrenotools_active=0 turnip_auto_selected=0
[GX_ANDROID_GRAPHICS] d3d_texture_formats bc1=0 bc2=0 bc3=0 rgba8=1 texture_profile=rgba8
[GX_TEXTURE_FALLBACK] source='Art/Textures/example.dds' replacement='/sdcard/Download/GeneralsZH/TextureFallback/RGBA8/Art/Textures/example.dds'
[GX_DDS] source='Art/Textures/example.dds' format=A8R8G8B8 size=... mips=... reduction=...
```

DXVK's INFO report also lists the physical device name, vendor/device IDs,
Vulkan API version, driver name/version, and `textureCompressionBC` value. The
Xclipse proof is expected to show `textureCompressionBC : 0`.

If BC is unavailable and the marker is missing, the engine logs:

```text
ERROR: [GX_ANDROID_GRAPHICS] BC texture sampling is unavailable and no RGBA8 fallback profile is installed. Prepare and push the RGBA8 texture fallback before launching.
```

## Galaxy A55 proof procedure

1. Install the proof APK and select a valid Zero Hour game-data folder in
   GeneralsZH Setup.
2. Launch once without the marker. Confirm the stock Xclipse device is logged,
   `textureCompressionBC` is `0`, the profile is `default`, and the explicit
   missing-fallback error appears.
3. Stop the app, prepare and push the legally extracted RGBA8 overlay, then
   relaunch.
4. Confirm the profile and replacement lines shown above. Inspect menus, text,
   terrain, units, effects, and alpha-cutout textures in a skirmish.
5. Keep the log and note any missing replacement path; an unconverted DDS can
   still reach the unsupported BC path.

## Adreno/Turnip regression procedure

On a Tab S9 or another supported Adreno device, disable the RGBA8 marker and
select the normal Turnip option in GeneralsZH Setup. The log should show the
`default` texture profile, no fallback replacement lines, and
`libadrenotools_active=1`. Verify menus and a skirmish against the previous APK.
This confirms that the fallback remains opt-in and the original archives and
Turnip renderer path are still used.

## Disable the fallback

Rename or remove only the profile marker and relaunch:

```bash
adb shell mv \
  "/sdcard/Download/GeneralsZH/TextureFallback/RGBA8/texture-fallback.cfg" \
  "/sdcard/Download/GeneralsZH/TextureFallback/RGBA8/texture-fallback.cfg.disabled"
```

The next log must show `texture_profile=default` and
`fallback_directory='disabled'`. The generated DDS files may remain present;
without the marker they are not searched.

## Storage and memory cost

RGBA8 stores four bytes per pixel:

- BC1 to RGBA8 is approximately an 8× expansion.
- BC2/BC3 to RGBA8 is approximately a 4× expansion.
- A full mip chain adds roughly one third over the base level.

A 1024×1024 texture with a full mip chain is approximately 5.33 MiB as RGBA8,
compared with 0.67 MiB as BC1 or 1.33 MiB as BC2/BC3. Originals remain in the
archives, so device storage is additive. Uploading also temporarily holds a
system-memory texture and a GPU texture.

Use a separate game-data copy for the non-Adreno test where possible. Keep the
fallback disabled on Adreno unless testing it deliberately.

## Current limitations

- Vulkan 1.3 and the other DXVK-required features are still required.
- The converter does not read `.big` archives directly.
- Only classic 2D DXT1, DXT3, and DXT5 DDS inputs are accepted.
- DX10 DDS, texture arrays, cubemaps, volumes, DXT2, and DXT4 are rejected.
- RGBA8 can be too large for a full production asset set on memory-constrained devices.
- ASTC and ETC2 conversion are not implemented.
