# BCPack

Some textures in the game's `.ttpf` packs are stored as BCPack chunks: a block-compressed texture (BC1, BC3, BC4,
BC5, BC6H or BC7) whose blocks are further compressed with a set of interleaved rANS (range asymmetric numeral
system) decoders. Lineup reads them with its own decoder, `engine/bcpack.py` (BC6H in `engine/bcpack_bc6h.py`),
which is pure Python with numpy and, when a C compiler is found, two small C libraries (`engine/native_build.py`).

## Where the decoder comes from

The decoder and its tables are this project's own clean-room work. They were worked out from the texture packs
themselves, the public rANS patent (US 11,234,023) and the public documentation of the BCn formats. No SDK or
other vendor material was used. The notes in the JSON files in `engine/bcpack_data/` refer to the author's private
research notes (`bcpack_research/README.md` and scripts such as `bc4degen.py`); those are not part of this
repository, so there is nothing to go looking for.

The files in `engine/bcpack_data/` are constants of the codec, the same for every pack. They do not hold image data.

| File | What it holds |
|---|---|
| `tables.json` | the rANS probability tables (one frequency list per symbol width and table number) |
| `bc1_index.json`, `bc3.json`, `bc4.json`, `bc5.json` | the fixed rank tables that map a stored symbol to a group of pixel indices |
| `bc1_presets.json`, `bc3.json`, `bc4.json`, `bc5.json`, `bc6h.json`, `bc7.json` | the codec's built-in histories: the 256 starting values that blocks near the start of a chunk may refer to |
| `bc7.json` | also the BC7 mode configuration, partition and anchor tables |

## Confirmed, provisional, last resort

A built-in history entry cannot be read from any file; it was recovered by fitting, so each entry carries an
evidence class, and the decoder keeps the distinction.

- **Confirmed**: pinned by exact evidence, for example blocks whose content is known exactly, or a value that
  several textures agree on. `decode()` uses only confirmed entries and refuses a chunk that needs another one.
- **Provisional**: the best-supported fitted value, for example a fit against the next larger mip of a texture.
  `decode_provisional()` uses it and reports which entries it relied on, so the result can be labelled. The
  pixels are usually right to within a small error, but they are not proven.
- **Last resort**: a provisional value that rests on weak evidence, such as a fit on only a few blocks or only on
  continuity across block edges. Blocks that depend on one can be visibly off.

The texture reader tries `decode()` first and falls back to `decode_provisional()` when a chunk needs an
unconfirmed entry, so a texture can contain pixels that came through provisional or last-resort entries. The viewer
does not yet mark such textures in the interface.

## How much is confirmed

These are the counts from the author's research notes (status of 2026-11-22), measured on version 1.0.2.1:

| Format | Chunks in all packs | Decoded from confirmed entries only | Remaining |
|---|---|---|---|
| BC1 | 100,020 | 99,559 | 461 through one of 3 provisional entries; none refused |
| BC3 | 35 | 0 | 35 provisional |
| BC4 | 77 | 2 | 75 provisional (24 entries are last resort) |
| BC5 | 325 | 3 | 322 provisional |
| BC6H | 268 | 0 | 268 provisional; 855 of 855 exactly known blocks reproduce bit for bit |
| BC7 | 5,120 | 585 | 4,535 provisional (over 2,200 endpoint slots are last resort) |

Texture packs that contain no BCPack chunks (for example most character textures at 2048 pixels, which are
zlib-coded) are unaffected.
