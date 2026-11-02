# Pokémon Gen IV Chatter Confusion Patcher

`chatter.py` edits a Pokémon Diamond, Pearl, Platinum, HeartGold, or SoulSilver save so Chatot’s Chatter move confuses at the maximum 31% rate. The custom recording is kept, the script instead updates the single audio byte the game checks to calculate that chance, and the general-block checksum is rewritten to match. The original file is left in place; the result is `name_patched.sav`.

From this directory:

```bash
python chatter.py inspect platinum.sav
python chatter.py maximize platinum.sav
```

`inspect` prints the detected game, whether a custom recording is stored, the current confusion probability, and the recording byte. `maximize` writes the patched copy next to the save:

```text
Game: Pokémon Platinum
Chatter recording: Found
Current confusion probability: 1%
Maximizing confusion...
Recording byte: 0x08 → 0x1E
New confusion probability: 31%
Save checksums updated.
Output: platinum_patched.sav
```

The save must be a 512 KiB raw `.sav`. Diamond, Pearl, Platinum, HeartGold, and SoulSilver are supported.

## How the edit works

Chatot’s cry is shared save data: a 4-byte “recording exists” flag plus 1,000 bytes of compressed audio. Confusion is decided by audio index `0x0F`, read as a signed byte.

| Signed value | Confusion chance |
| --- | --- |
| Recording flag is not 1 | 1% |
| −128 through −31 | 11% |
| −30 through 29 | 1% |
| 30 through 127 | 31% |

`maximize` stores `0x1E` (signed 30) at that index in the current general-block copy. The backup partition is left alone, which is the same copy the game would fall back to. The general block’s CRC16-CCITT is stored in the footer’s last two bytes, little-endian. Diamond, Pearl, and Platinum exclude the last `0x14` bytes from that CRC. HeartGold and SoulSilver exclude the last `0x10` bytes. The tool reads the save-copy counters at `length - 0x14` for every supported Generation IV game.

Diamond and Pearl share one layout. Platinum uses another. HeartGold and SoulSilver share a third.

| Game | General block | Chatter offset | Trainer block | Checksum footer |
| --- | --- | --- | --- | --- |
| Diamond, Pearl | `0xC100` | `0x61CC` | `0x64` | `0x14` |
| Platinum | `0xCF2C` | `0x64EC` | `0x68` | `0x14` |
| HeartGold, SoulSilver | `0xF628` | `0x4E74` | `0x64` | `0x10` |

The current copy is the half of the 512 KiB file (at `0x00000` or `0x40000`) with the higher footer counter. Game detection uses the general-block footer: the size field must match the block, and the SDK magic must be `0x20060623` (Japan / international) or `0x20070903` (Korean).

Retail Diamond and Pearl never write the trainer version byte. Those saves are reported as **Pokémon Diamond/Pearl**. A version byte of 10 or 11 is reported as Diamond or Pearl. Platinum is identified by its block size; its trainer version byte is 12. HeartGold and SoulSilver store 7 or 8 in that byte and are named from it. A Johto save with any other value is reported as **Pokémon HeartGold/SoulSilver**.

A save with no custom recording still gets the byte set, and the reported chance stays 1%. The game ignores the clip until the flag is 1, and this tool does not invent a recording.

## Tests

```bash
python -m unittest test_chatter.py
```

The tests build synthetic saves with the footer and cover reading a clip, the probability table, patching Diamond/Pearl/Platinum, checksum repair, leaving unrelated bytes alone, and applying the patch twice.

