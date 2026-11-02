#!/usr/bin/env python3
"""Tests for the Generation IV Chatter patcher."""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import chatter

SCRIPT = ROOT / "chatter.py"


def _write_footer(
    buf: bytearray,
    base: int,
    size: int,
    major: int,
    minor: int,
    magic: int,
    footer_size: int,
) -> None:
    # Counters sit at size - 0x14 for every Gen IV game. The checksum excludes
    # footer_size bytes, which is 0x10 for HeartGold/SoulSilver and 0x14 otherwise.
    struct.pack_into("<II", buf, base + size - chatter.BLOCK_COUNTER_OFFSET, major, minor)
    struct.pack_into("<I", buf, base + size - 0x0C, size)
    struct.pack_into("<I", buf, base + size - 0x08, magic)
    prefix = buf[base : base + size - footer_size]
    struct.pack_into("<H", buf, base + size - 2, chatter.crc16_ccitt(prefix))


def make_save(
    layout: chatter.Layout,
    *,
    active_slot: int = 1,
    version: int = 0,
    initialized: bool = True,
    recording_byte: int = 0x08,
    audio_fill: int = 0x11,
    magic: int = chatter.MAGIC_JAPAN_INTL,
    primary_major: int | None = None,
    secondary_major: int | None = None,
) -> bytearray:
    """Build a 512 KiB save with a valid general-block footer in both partitions."""
    if active_slot not in (0, 1):
        raise ValueError("active_slot must be 0 or 1")
    if primary_major is None or secondary_major is None:
        # The higher major counter selects the active copy.
        primary_major = 2 if active_slot == 0 else 1
        secondary_major = 1 if active_slot == 0 else 2

    buf = bytearray(b"\xA5" * chatter.SAVE_SIZE)
    for slot, major in ((0, primary_major), (1, secondary_major)):
        base = slot * chatter.PARTITION_SIZE
        block = buf[base : base + layout.general_size]
        # Drop the canary inside the general block so the recording is explicit.
        block[:] = b"\x00" * layout.general_size
        chatter_at = layout.chatter_offset
        struct.pack_into("<I", block, chatter_at, 1 if initialized else 0)
        audio = chatter_at + 4
        for index in range(chatter.PCM_SIZE):
            block[audio + index] = audio_fill
        block[audio + chatter.CONFUSION_INDEX] = recording_byte
        block[layout.trainer_offset + chatter.VERSION_OFFSET] = version
        buf[base : base + layout.general_size] = block
        _write_footer(
            buf,
            base,
            layout.general_size,
            major,
            minor=0,
            magic=magic,
            footer_size=layout.footer_size,
        )
    return buf


def changed_offsets(left: bytes | bytearray, right: bytes | bytearray) -> list[int]:
    return [index for index, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]


class CrcTests(unittest.TestCase):
    def test_empty_and_single_byte_crc_values(self) -> None:
        self.assertEqual(chatter.crc16_ccitt(b""), 0xFFFF)
        # Expected CRC16-CCITT for the single byte 0x00.
        self.assertEqual(chatter.crc16_ccitt(b"\x00"), 0xE1F0)

    def test_general_block_checksum_round_trip(self) -> None:
        save = make_save(chatter.LAYOUT_PT, version=12)
        block = save[: chatter.PT_GENERAL_SIZE]
        stored, actual = chatter.block_checksum(block)
        self.assertEqual(stored, actual)


class ProbabilityTests(unittest.TestCase):
    def test_signed_recording_byte_boundaries(self) -> None:
        cases = [
            (False, 0x1E, 1),
            (True, 0x00, 1),
            (True, 29, 1),
            (True, 30, 31),
            (True, 0x1E, 31),
            (True, 127, 31),
            (True, 0xE2, 1),  # signed -30
            (True, 0xE1, 11),  # signed -31
            (True, 0x80, 11),  # signed -128
            (True, 0xFF, 1),  # signed -1
            (True, 0x08, 1),
        ]
        for initialized, byte, expected in cases:
            with self.subTest(initialized=initialized, byte=byte):
                self.assertEqual(chatter.confusion_probability(initialized, byte), expected)


class InspectTests(unittest.TestCase):
    def test_platinum_recording(self) -> None:
        save = make_save(chatter.LAYOUT_PT, version=12, recording_byte=0x08)
        info = chatter.inspect_save(save)
        self.assertEqual(info.game, "Pokémon Platinum")
        self.assertTrue(info.recording_found)
        self.assertEqual(info.probability, 1)
        self.assertEqual(info.recording_byte, 0x08)
        self.assertEqual(info.general_base, chatter.PARTITION_SIZE)

    def test_diamond_and_pearl_names(self) -> None:
        diamond = chatter.inspect_save(
            make_save(chatter.LAYOUT_DP, version=chatter.VERSION_DIAMOND, active_slot=0)
        )
        pearl = chatter.inspect_save(
            make_save(chatter.LAYOUT_DP, version=chatter.VERSION_PEARL, active_slot=0)
        )
        shared = chatter.inspect_save(make_save(chatter.LAYOUT_DP, version=0, active_slot=0))
        self.assertEqual(diamond.game, "Pokémon Diamond")
        self.assertEqual(pearl.game, "Pokémon Pearl")
        self.assertEqual(shared.game, "Pokémon Diamond/Pearl")
        self.assertEqual(diamond.general_base, 0)
        self.assertEqual(shared.layout.chatter_offset, chatter.DP_CHATTER_OFFSET)

    def test_missing_recording_still_reports_one_percent(self) -> None:
        save = make_save(chatter.LAYOUT_DP, version=10, initialized=False, recording_byte=0x1E)
        info = chatter.inspect_save(save)
        self.assertFalse(info.recording_found)
        self.assertEqual(info.probability, 1)
        self.assertEqual(info.recording_byte, 0x1E)

    def test_korean_magic_is_detected(self) -> None:
        save = make_save(chatter.LAYOUT_PT, version=12, magic=chatter.MAGIC_KOREAN)
        self.assertEqual(chatter.inspect_save(save).game, "Pokémon Platinum")

    def test_heartgold_and_soulsilver_names(self) -> None:
        heartgold = chatter.inspect_save(
            make_save(chatter.LAYOUT_HGSS, version=chatter.VERSION_HEARTGOLD, active_slot=0)
        )
        soulsilver = chatter.inspect_save(
            make_save(chatter.LAYOUT_HGSS, version=chatter.VERSION_SOULSILVER)
        )
        shared = chatter.inspect_save(make_save(chatter.LAYOUT_HGSS, version=0))
        self.assertEqual(heartgold.game, "Pokémon HeartGold")
        self.assertEqual(soulsilver.game, "Pokémon SoulSilver")
        self.assertEqual(shared.game, "Pokémon HeartGold/SoulSilver")
        self.assertEqual(heartgold.general_base, 0)
        self.assertEqual(soulsilver.general_base, chatter.PARTITION_SIZE)
        self.assertEqual(heartgold.layout.chatter_offset, chatter.HGSS_CHATTER_OFFSET)
        self.assertEqual(heartgold.layout.footer_size, chatter.HGSS_FOOTER_SIZE)

    def test_rejects_wrong_size_and_unknown_footer(self) -> None:
        with self.assertRaises(chatter.SaveError):
            chatter.inspect_save(b"\x00" * 100)
        unknown = bytearray(b"\x00" * chatter.SAVE_SIZE)
        struct.pack_into("<I", unknown, chatter.PARTITION_SIZE + 0x1111 - 0x0C, 0x1111)
        struct.pack_into("<I", unknown, chatter.PARTITION_SIZE + 0x1111 - 0x08, chatter.MAGIC_JAPAN_INTL)
        with self.assertRaises(chatter.SaveError):
            chatter.inspect_save(unknown)


class PatchTests(unittest.TestCase):
    def test_patches_active_slot_and_preserves_everything_else(self) -> None:
        for layout, version, slot in (
            (chatter.LAYOUT_PT, 12, 1),
            (chatter.LAYOUT_PT, 12, 0),
            (chatter.LAYOUT_DP, 10, 1),
            (chatter.LAYOUT_DP, 11, 0),
            (chatter.LAYOUT_HGSS, chatter.VERSION_HEARTGOLD, 1),
            (chatter.LAYOUT_HGSS, chatter.VERSION_SOULSILVER, 0),
        ):
            with self.subTest(layout=layout.key, slot=slot):
                original = make_save(
                    layout,
                    version=version,
                    active_slot=slot,
                    recording_byte=0x08,
                    audio_fill=0x5A,
                )
                patched, before, after = chatter.patch_save(bytes(original))
                self.assertEqual(before.probability, 1)
                self.assertEqual(after.recording_byte, chatter.MAX_CONFUSION_BYTE)
                self.assertEqual(after.probability, 31)
                self.assertTrue(after.recording_found)
                self.assertEqual(before.game, after.game)

                changed = set(changed_offsets(original, patched))
                allowed = {
                    before.recording_offset,
                    before.checksum_offset,
                    before.checksum_offset + 1,
                }
                self.assertIn(before.recording_offset, changed)
                self.assertTrue(changed <= allowed)

                base = before.general_base
                chatter_at = base + layout.chatter_offset
                audio = patched[chatter_at + 4 : chatter_at + 4 + chatter.PCM_SIZE]
                self.assertEqual(audio[chatter.CONFUSION_INDEX], 0x1E)
                self.assertTrue(all(byte == 0x5A for index, byte in enumerate(audio) if index != 0x0F))
                self.assertEqual(patched[chatter_at : chatter_at + 4], b"\x01\x00\x00\x00")

                inactive = 0 if base else chatter.PARTITION_SIZE
                inactive_slice = slice(inactive, inactive + layout.general_size)
                self.assertEqual(patched[inactive_slice], original[inactive_slice])

                block = patched[base : base + layout.general_size]
                stored, actual = chatter.block_checksum(block, layout.footer_size)
                self.assertEqual(stored, actual)
                if layout.footer_size != chatter.FOOTER_SIZE:
                    # HeartGold/SoulSilver must not be hashed with the Sinnoh footer length.
                    self.assertNotEqual(actual, chatter.crc16_ccitt(block[:-chatter.FOOTER_SIZE]))

    def test_repeat_patch_is_stable(self) -> None:
        original = bytes(make_save(chatter.LAYOUT_PT, version=12, recording_byte=0x03))
        once, _, after_once = chatter.patch_save(original)
        twice, before_twice, after_twice = chatter.patch_save(once)
        self.assertEqual(once, twice)
        self.assertEqual(before_twice.recording_byte, 0x1E)
        self.assertEqual(before_twice.probability, 31)
        self.assertEqual(after_twice.probability, 31)
        self.assertEqual(after_once.recording_byte, after_twice.recording_byte)

    def test_already_maximum_with_valid_checksum_is_unchanged(self) -> None:
        original = bytes(make_save(chatter.LAYOUT_DP, version=10, recording_byte=0x1E))
        patched, before, after = chatter.patch_save(original)
        self.assertEqual(before.probability, 31)
        self.assertEqual(after.probability, 31)
        self.assertEqual(original, patched)

    def test_repairs_general_checksum_without_touching_other_bytes(self) -> None:
        original = make_save(chatter.LAYOUT_PT, version=12, recording_byte=0x1E)
        info = chatter.inspect_save(original)
        original[info.checksum_offset] ^= 0xFF
        patched, _, _ = chatter.patch_save(bytes(original))
        # The clip byte was already 0x1E, so the only write is the restored checksum.
        self.assertEqual(changed_offsets(original, patched), [info.checksum_offset])
        block = patched[info.general_base : info.general_base + info.layout.general_size]
        stored, actual = chatter.block_checksum(block)
        self.assertEqual(stored, actual)
        again, _, _ = chatter.patch_save(patched)
        self.assertEqual(again, patched)

    def test_uninitialized_recording_keeps_one_percent(self) -> None:
        original = bytes(make_save(chatter.LAYOUT_DP, version=11, initialized=False, recording_byte=0x08))
        patched, before, after = chatter.patch_save(original)
        self.assertFalse(before.recording_found)
        self.assertEqual(before.probability, 1)
        self.assertEqual(after.recording_byte, 0x1E)
        self.assertEqual(after.probability, 1)
        self.assertNotEqual(original, patched)

    def test_newer_minor_counter_selects_the_patched_copy(self) -> None:
        save = make_save(
            chatter.LAYOUT_PT,
            version=12,
            primary_major=4,
            secondary_major=4,
            recording_byte=0x01,
        )
        # Equal majors: the higher minor counter wins. Bump the primary minor.
        footer = chatter.PT_GENERAL_SIZE - chatter.FOOTER_SIZE
        struct.pack_into("<I", save, footer + 4, 9)
        struct.pack_into("<I", save, footer + chatter.PARTITION_SIZE + 4, 3)
        # Distinct audio bytes so the patched copy is obvious.
        save[chatter.PT_CHATTER_OFFSET + 4 + 0x0F] = 0x01
        save[chatter.PARTITION_SIZE + chatter.PT_CHATTER_OFFSET + 4 + 0x0F] = 0x02
        _rewrite_checksum(save, 0, chatter.LAYOUT_PT)
        _rewrite_checksum(save, chatter.PARTITION_SIZE, chatter.LAYOUT_PT)

        patched, before, _ = chatter.patch_save(bytes(save))
        self.assertEqual(before.general_base, 0)
        self.assertEqual(before.recording_byte, 0x01)
        self.assertEqual(patched[before.recording_offset], 0x1E)
        other = chatter.PARTITION_SIZE + chatter.PT_CHATTER_OFFSET + 4 + 0x0F
        self.assertEqual(patched[other], 0x02)


def _rewrite_checksum(buf: bytearray, base: int, layout: chatter.Layout) -> None:
    prefix = buf[base : base + layout.general_size - layout.footer_size]
    struct.pack_into(
        "<H",
        buf,
        base + layout.general_size - 2,
        chatter.crc16_ccitt(prefix),
    )


class CliTests(unittest.TestCase):
    def test_maximize_matches_expected_output_and_keeps_original(self) -> None:
        save = bytes(make_save(chatter.LAYOUT_PT, version=12, recording_byte=0x08, active_slot=1))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "platinum.sav"
            source.write_bytes(save)
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "maximize", str(source)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, (
                "Game: Pokémon Platinum\n"
                "Chatter recording: Found\n"
                "Current confusion probability: 1%\n"
                "Maximizing confusion...\n"
                "Recording byte: 0x08 → 0x1E\n"
                "New confusion probability: 31%\n"
                "Save checksums updated.\n"
                "Output: platinum_patched.sav\n"
            ))
            self.assertEqual(source.read_bytes(), save)
            patched_path = Path(tmp) / "platinum_patched.sav"
            patched = patched_path.read_bytes()
            self.assertNotEqual(patched, save)
            again = subprocess.run(
                [sys.executable, str(SCRIPT), "maximize", str(patched_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            second = Path(tmp) / "platinum_patched_patched.sav"
            self.assertEqual(second.read_bytes(), chatter.patch_save(patched)[0])
            # Patching the already-patched save a second time does not move any other byte.
            third, _, _ = chatter.patch_save(second.read_bytes())
            self.assertEqual(third, second.read_bytes())

    def test_inspect_command(self) -> None:
        save = bytes(make_save(chatter.LAYOUT_DP, version=10, recording_byte=0x22, active_slot=0))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "diamond.sav"
            source.write_bytes(save)
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "inspect", str(source)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, (
            "Game: Pokémon Diamond\n"
            "Chatter recording: Found\n"
            "Current confusion probability: 31%\n"
            "Recording byte: 0x22\n"
        ))

    def test_missing_file_is_an_error(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "inspect", "no-such-save.sav"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(completed.stderr.startswith("error:"))


if __name__ == "__main__":
    unittest.main()
