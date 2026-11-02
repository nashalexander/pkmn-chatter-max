#!/usr/bin/env python3
"""Maximize Chatot Chatter confusion in Pokémon Diamond, Pearl, Platinum, HeartGold, and SoulSilver saves.

The recording lives in the save's general block, not on the Pokémon.
The confusion byte is the 16th byte of the 1,000-byte audio clip, at audio
index 0x0F. HeartGold and SoulSilver use the same confusion formula at
their own offset.

Diamond and Pearl use one save layout. Retail Diamond and Pearl leave the
trainer version byte at 0, so those saves are reported as
Pokémon Diamond/Pearl unless the byte is 10 or 11. Platinum is identified
by its general-block size. HeartGold and SoulSilver store the cartridge
version (7 or 8) in that same trainer byte.
"""

from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

SAVE_SIZE = 0x80000
PARTITION_SIZE = 0x40000
# Sinnoh checksum footer. HeartGold/SoulSilver exclude only the last 0x10 bytes.
FOOTER_SIZE = 0x14
# The save counters are read here for every supported Gen IV game,
# including HeartGold/SoulSilver, whose checksum footer is shorter.
BLOCK_COUNTER_OFFSET = 0x14
PCM_SIZE = 1000
CHATTER_SIZE = 4 + PCM_SIZE
CONFUSION_INDEX = 0x0F
MAX_CONFUSION_BYTE = 0x1E

MAGIC_JAPAN_INTL = 0x20060623
MAGIC_KOREAN = 0x20070903
MAGICS = (MAGIC_JAPAN_INTL, MAGIC_KOREAN)

VERSION_HEARTGOLD = 7
VERSION_SOULSILVER = 8
VERSION_DIAMOND = 10
VERSION_PEARL = 11
VERSION_OFFSET = 0x1C

DP_GENERAL_SIZE = 0xC100
DP_CHATTER_OFFSET = 0x61CC
DP_TRAINER_OFFSET = 0x64

PT_GENERAL_SIZE = 0xCF2C
PT_CHATTER_OFFSET = 0x64EC
PT_TRAINER_OFFSET = 0x68

HGSS_GENERAL_SIZE = 0xF628
HGSS_CHATTER_OFFSET = 0x4E74
HGSS_TRAINER_OFFSET = 0x64
HGSS_FOOTER_SIZE = 0x10


class SaveError(Exception):
    """The file is not a usable Generation IV Sinnoh or Johto save."""


@dataclass(frozen=True)
class Layout:
    key: str
    general_size: int
    chatter_offset: int
    trainer_offset: int
    footer_size: int

    def game_name(self, version: int) -> str:
        if self.key == "pt":
            return "Pokémon Platinum"
        if self.key == "hgss":
            if version == VERSION_HEARTGOLD:
                return "Pokémon HeartGold"
            if version == VERSION_SOULSILVER:
                return "Pokémon SoulSilver"
            return "Pokémon HeartGold/SoulSilver"
        if version == VERSION_DIAMOND:
            return "Pokémon Diamond"
        if version == VERSION_PEARL:
            return "Pokémon Pearl"
        return "Pokémon Diamond/Pearl"


LAYOUT_DP = Layout("dp", DP_GENERAL_SIZE, DP_CHATTER_OFFSET, DP_TRAINER_OFFSET, FOOTER_SIZE)
LAYOUT_PT = Layout("pt", PT_GENERAL_SIZE, PT_CHATTER_OFFSET, PT_TRAINER_OFFSET, FOOTER_SIZE)
LAYOUT_HGSS = Layout(
    "hgss",
    HGSS_GENERAL_SIZE,
    HGSS_CHATTER_OFFSET,
    HGSS_TRAINER_OFFSET,
    HGSS_FOOTER_SIZE,
)


@dataclass(frozen=True)
class ChatterInfo:
    game: str
    layout: Layout
    general_base: int
    recording_found: bool
    probability: int
    recording_byte: int

    @property
    def recording_offset(self) -> int:
        """Absolute offset of audio byte 0x0F in the save."""
        return self.general_base + self.layout.chatter_offset + 4 + CONFUSION_INDEX

    @property
    def checksum_offset(self) -> int:
        return self.general_base + self.layout.general_size - 2


def crc16_ccitt(data: bytes | bytearray | memoryview) -> int:
    """CRC16-CCITT with initial value 0xFFFF for Gen IV saves."""
    top = 0xFF
    bot = 0xFF
    for byte in data:
        mixed = byte ^ top
        mixed ^= mixed >> 4
        top = (bot ^ (mixed >> 3) ^ (mixed << 4)) & 0xFF
        bot = (mixed ^ (mixed << 5)) & 0xFF
    return (top << 8) | bot


def confusion_probability(initialized: bool, recording_byte: int) -> int:
    """Chatter confusion chance, in percent.

    The signed recording byte selects 1%, 11%, or 31%.
    An uninitialized clip stays at 1%.
    """
    if not initialized:
        return 1
    signed = recording_byte - 256 if recording_byte >= 128 else recording_byte
    if signed < -30:
        return 11
    if signed >= 30:
        return 31
    return 1


def _u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _compare_counters(counter1: int, counter2: int) -> int:
    """Compare save counters. 0=first, 1=second, 2=same."""
    if counter1 == 0xFFFFFFFF and counter2 != 0xFFFFFFFE:
        return 1
    if counter2 == 0xFFFFFFFF and counter1 != 0xFFFFFFFE:
        return 0
    if counter1 > counter2:
        return 0
    if counter1 < counter2:
        return 1
    return 2


def _compare_footers(data: bytes | bytearray, offset1: int, offset2: int) -> int:
    """Compare footer counters. 0=primary, 1=secondary."""
    result = _compare_counters(_u32(data, offset1), _u32(data, offset2))
    if result != 2:
        return result
    result = _compare_counters(_u32(data, offset1 + 4), _u32(data, offset2 + 4))
    return 1 if result == 1 else 0


def active_general_base(data: bytes | bytearray, general_size: int) -> int:
    """Offset of the newer general block."""
    footer = general_size - BLOCK_COUNTER_OFFSET
    slot = _compare_footers(data, footer, footer + PARTITION_SIZE)
    return 0 if slot == 0 else PARTITION_SIZE


def _footer_matches(data: bytes | bytearray, base: int, general_size: int) -> bool:
    """Check the general-block size and magic for one partition."""
    if base < 0 or base + general_size > len(data):
        return False
    size = _u32(data, base + general_size - 0x0C)
    magic = _u32(data, base + general_size - 0x08)
    return size == general_size and magic in MAGICS


def detect_layout(data: bytes | bytearray) -> Layout:
    """Diamond/Pearl, Platinum, or HeartGold/SoulSilver, from the general-block footer.

    Check the copy at 0x40000 first. A save whose current copy is the only
    initialized half is accepted as well. The three general-block sizes do not overlap.
    """
    if len(data) != SAVE_SIZE:
        raise SaveError(
            f"expected a {SAVE_SIZE}-byte raw save, got {len(data)} bytes"
        )
    for base in (PARTITION_SIZE, 0):
        for layout in (LAYOUT_HGSS, LAYOUT_PT, LAYOUT_DP):
            if _footer_matches(data, base, layout.general_size):
                return layout
    raise SaveError(
        "not a Pokémon Diamond, Pearl, Platinum, HeartGold, or SoulSilver save "
        "(general-block footer did not match)"
    )


def _read_chatter(data: bytes | bytearray, info_base: int, layout: Layout) -> ChatterInfo:
    chatter = info_base + layout.chatter_offset
    if chatter + CHATTER_SIZE > info_base + layout.general_size - layout.footer_size:
        raise SaveError("Chatter recording does not fit in the general block")
    initialized = _u32(data, chatter) == 1
    recording_byte = data[chatter + 4 + CONFUSION_INDEX]
    version = data[info_base + layout.trainer_offset + VERSION_OFFSET]
    return ChatterInfo(
        game=layout.game_name(version),
        layout=layout,
        general_base=info_base,
        recording_found=initialized,
        probability=confusion_probability(initialized, recording_byte),
        recording_byte=recording_byte,
    )


def inspect_save(data: bytes | bytearray) -> ChatterInfo:
    layout = detect_layout(data)
    base = active_general_base(data, layout.general_size)
    return _read_chatter(data, base, layout)


def patch_save(data: bytes) -> tuple[bytes, ChatterInfo, ChatterInfo]:
    """Return (patched save, before, after). The original buffer is unchanged.

    Writes audio[0x0F] = 0x1E in the active general block and refreshes that
    block's CRC16. Every other byte, including the rest of the recording and
    the backup partition, stays as it was.
    """
    before = inspect_save(data)
    patched = bytearray(data)
    patched[before.recording_offset] = MAX_CONFUSION_BYTE

    block_end = before.general_base + before.layout.general_size
    prefix = patched[before.general_base : block_end - before.layout.footer_size]
    struct.pack_into("<H", patched, before.checksum_offset, crc16_ccitt(prefix))

    after = inspect_save(patched)
    return bytes(patched), before, after


def block_checksum(block: bytes | bytearray, footer_size: int = FOOTER_SIZE) -> tuple[int, int]:
    """Return (stored general-block checksum, CRC of the block without its footer)."""
    if footer_size < 2 or len(block) < footer_size:
        raise SaveError("general block is shorter than its footer")
    stored = struct.unpack_from("<H", block, len(block) - 2)[0]
    actual = crc16_ccitt(block[:-footer_size])
    return stored, actual


def format_inspect(info: ChatterInfo) -> str:
    state = "Found" if info.recording_found else "Not found"
    return (
        f"Game: {info.game}\n"
        f"Chatter recording: {state}\n"
        f"Current confusion probability: {info.probability}%\n"
        f"Recording byte: 0x{info.recording_byte:02X}\n"
    )


def format_maximize(before: ChatterInfo, after: ChatterInfo, output: Path) -> str:
    state = "Found" if before.recording_found else "Not found"
    lines = [
        f"Game: {before.game}",
        f"Chatter recording: {state}",
        f"Current confusion probability: {before.probability}%",
        "Maximizing confusion...",
        f"Recording byte: 0x{before.recording_byte:02X} → 0x{after.recording_byte:02X}",
        f"New confusion probability: {after.probability}%",
        "Save checksums updated.",
        f"Output: {output.name}",
    ]
    if not before.recording_found:
        lines.append(
            "No custom recording is stored, so the game keeps the 1% chance "
            "until a recording exists."
        )
    return "\n".join(lines) + "\n"


def patched_output_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}_patched{source.suffix}")


def _read_save(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SaveError(str(exc)) from exc


def _command_inspect(path: Path) -> int:
    info = inspect_save(_read_save(path))
    sys.stdout.write(format_inspect(info))
    return 0


def _command_maximize(path: Path) -> int:
    original = _read_save(path)
    patched, before, after = patch_save(original)
    destination = patched_output_path(path)
    if destination.resolve() == path.resolve():
        raise SaveError("refusing to overwrite the original save")
    destination.write_bytes(patched)
    sys.stdout.write(format_maximize(before, after, destination))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read or maximize Chatot's Chatter confusion chance "
            "in a Pokémon Diamond, Pearl, Platinum, HeartGold, or SoulSilver save."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="show the current Chatter recording")
    inspect.add_argument("save", type=Path, help="512 KiB raw .sav")

    maximize = sub.add_parser(
        "maximize",
        help="write a copy whose Chatter confusion chance is 31%%",
    )
    maximize.add_argument("save", type=Path, help="512 KiB raw .sav")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            return _command_inspect(args.save)
        if args.command == "maximize":
            return _command_maximize(args.save)
    except SaveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
