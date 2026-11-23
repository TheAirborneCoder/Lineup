# What the decoders understand

Short notes on the layouts Lineup reads, written from the docstrings in `engine/`. They describe structure only;
no game data is reproduced here. Offsets are for game version 1.0.2.1. Everything was worked out from the files
themselves; where a field is unexplained the code keeps its bytes raw. Nothing here decrypts or unprotects
anything: Lineup only reads archives that are already readable on disk.

## Archives (`.rpf`)

An archive starts with the 4 bytes `9FPR` (`RPF9` read as a little-endian word). Lineup does not use the archive's
own table of contents. Instead the `index` stage (`engine/archive_index.py`) scans the file for **zlib streams that
start on a 16-byte boundary** and keeps those whose Adler-32 checksum verifies. Each one becomes a 32-byte row of
`deflate-spans.bin`: packed offset, packed length, inflated length, Adler-32, first four bytes of the inflated data
(`<QQQII`). Members are then recognised by what they contain:

| Member kind | How it is recognised | Used for |
|---|---|---|
| PSIN metadata (`*.pso`) | starts with `PSIN` after inflating; checksums verify (see below) | ped archetypes, outfits, wardrobe, decoration and tint catalogues |
| XML | starts with an XML declaration (or well-formed XML) | body shapes, status effects, damage tables, vehicle names |
| native resource | 16 zero bytes, then zlib-compressed CPU pages | models, embedded textures |

## PSO metadata files (`engine/decode_pso.py`, `engine/rpf9_psin.py`)

A PSIN member is a sequence of sections, each a 4-byte tag and a big-endian size, in this order: `PSIN` (data
blocks), `LSCH` (structure schemas), `LENM` (enums), `LSNS`, `LGID`, `LRSC` (a root descriptor of 80 bytes) and
`CHKS` (total length and a checksum of the whole file). In the archive the file is stored as consecutive
deflate-compressed blocks that each expand to at most 64 KiB.

- A **schema** has a 32-bit type hash, a stride and a list of 16-byte field descriptors: name hash, offset, a type
  word (the low byte is the type code) and a reference (the hash of the structure or enum the field points at).
  Fields that share an offset are one field; a field named `0x100` declares the element type of an array and takes
  no space.
- **Names are not stored**, only 32-bit hashes (Bob Jenkins' one-at-a-time hash of the name as written). See
  [names.md](names.md).
- A pointer is a 64-bit value: the block number is `bits & 0xfffff`, the byte offset inside the block is
  `bits >> 20`.

Type codes that occur in the 8,253 members of version 1.0.2.1, with the field size measured from the schema layout
and the data (`python tools/verify_pso_types.py` reproduces the table on your copy):

| Code | Meaning | Size in bytes |
|---|---|---|
| `0x01` | boolean (0 or 1) | 1 |
| `0x10`, `0x11` | 8-bit integer | 1 |
| `0x12`, `0x13` | 16-bit integer | 2 |
| `0x14`, `0x15` | 32-bit integer | 4 |
| `0x21` | float32 | 4 |
| `0x33`, `0x34` | vector of 3 or 4 floats; the 3-vector sits in a 16-byte slot and its fourth lane is not read | 16 |
| `0x4a` | 32-bit name hash | 4 |
| `0x60`, `0x62` | enum (8 or 32 bit); values are members of the referenced enum | 1, 4 |
| `0x63`, `0x64`, `0x65` | flags (8, 16, 32 bit) | 1, 2, 4 |
| `0x05` | a structure stored inline (size from its schema) | stride |
| `0x07` | pointer | 8 |
| `0x40`, `0x44` | fixed-length string; string (up to 15 characters inline, else pointer and length) | n, 16 |
| `0x50` | fixed-size array | n x element |
| `0x52`, `0x5c` | array: pointer, 16-bit count, 16-bit capacity (12 bytes); pointer with 32-bit count and capacity (16 bytes) | 12, 16 |

Other codes occur in the files (64-bit integers, wider vectors, composite types); the decoder returns their bytes
raw.

## Native resources, drawables and fragments (`engine/resource_pages.py`, `drawables.py`, `model_decode.py`)

A resource is 16 zero bytes followed by zlib-compressed CPU pages (the "system" section) and then GPU data.
Pointers in the CPU pages are virtual addresses based at `0x50000000`; GPU buffers are based at `0x60000000`.
Three layouts carry model names: a **dictionary** (a table of name hashes next to a table of pointers), a
**standalone drawable**, and a **fragment** whose drawable sits behind the pointer at `+0x18`. The model name is a
string ending in `.#dr` or `.#ft`.

A drawable holds up to four LODs; each model has geometries made of meshlets, a vertex buffer and an index buffer
in the GPU data, and a material per geometry (a shader hash and 64-bit texture name hashes). Skinned characters
store vertices relative to the skeleton root, about 1 m above the floor, while the drawable's box is in standing
space; Lineup places them with a node translation when the two agree to within 3 cm.

Textures can also live inside a model resource: a texture descriptor holds the GPU address, width, height, layer
count, dimension, DXGI format and mip count, and a name or name hash.

## Texture packs (`engine/ttpf_format.py`)

`data/ttdb/**/*.ttpf` files start with `RDS5`. A chunk table (one 64-bit entry per chunk: physical offset in
256-byte units, a 6-bit codec, virtual offset bits) maps the physical file onto a virtual stream. The codec is
zlib, stored, or BCPack for BC1, BC3, BC4, BC5, BC6H or BC7 (see [bcpack.md](bcpack.md)). The start of the virtual
stream is an `ETPF` index: a count, then per texture a descriptor (width, height, declared levels, DXGI format,
slice count, dimension, tile mode) and a 4 KiB-page location, then the 64-bit name hashes in ascending order.
Surfaces use a GFX10-style swizzle in 64 KiB blocks; mips that fit half a block share a mip-tail block and the
larger mips follow from smallest to largest.

## Skeletons and skin (`engine/character_rig.py`)

The skeleton of a drawable is behind the pointer at root `+0x18`: a bone count (16-bit at `+0x6C`), bone records of
0x50 bytes (rotation quaternion, translation, scale, a name hash, an **attach hash**, a name pointer), inverse bind
matrices of 64 bytes each and 16-bit parent indices. Skin weights come from the vertex streams (4 or 8 influences:
four unsigned bytes, packed 32-bit words holding 12-bit indices, or 16-bit values; rigid parts use one bone) and
each model carries a palette of bone name hashes that its local indices refer to.

Characters, clothing and accessories all skin to one of a few **master skeletons** (the `ambient_*body` fragments:
female, male, kid, infant, large). A part may carry a small skeleton of its own for helper bones (hat bands, glasses,
watch straps, garment helpers). A helper bone is attached, in this order, to its parent in the part's own skeleton;
to the master joint named by its attach hash (its bind pose is then stored in that bone's local space); or, when
the target master does not have that bone (the kid and infant masters lack many accessory placeholders), to the
first joint that the target master has when going up from the attach bone in the item's own default master
skeleton (`attach_through_default_master`), or the skeleton root when there is none.

## Appearance catalogues (`engine/ped_archetypes.py`, `char_outfits.py`, `clothing_presets.py`, ...)

These PSO members (found by their offsets in version 1.0.2.1) list what a character wears:

| Member | Holds |
|---|---|
| `packs.rpf` `00011d1ff000` | the ped archetype hashes |
| `packs.rpf` `00011cfbf000` | per archetype, an ordered list of outfits: slot hash, outfit hash, random-spawn flag, weight |
| `packs.rpf` `00011ccb2000` | the wardrobe catalogue: outfit definitions, part presets (88-byte records), per-state drawable and texture overrides (184-byte records) |
| `packs.rpf` `00011dab3000` | 22 colour palettes (RGBA8), including the 256-entry paired hair palette |
| `data.rpf` `00001b95b000` | clothing material presets (8-slot garment presets) |
| `data.rpf` `00001ae00000` | the decoration (tattoo and make-up) catalogue with UV placement matrices |

In addition `packs.rpf` holds 2,709 compiled per-character catalogues: resources with the magic `0DRP` at `+0x10`
whose internal pointers use the same block/offset encoding as PSO files.

## Animation (`engine/anim_service.py`, `blendspace.py`, `motion_matcher.py`)

Clips live in clip dictionaries in `anim.rpf` and the content packs. A clip has tracks (bone rotation, translation,
scale, and others, identified by the one-at-a-time hash of a track name) with channel types 0 to 5, 7, 8 and 10;
the float channels are second-order delta-coded in chunks of 64 frames with 8-lane bitstreams, and several
animation layers can be merged. A **blendspace** (`.motion`) is a parametric node tree of 0 to 5 dimensions with
sample grids, child clips and time-warp curves. A **motion matching** database (`.motionmatcher`) comes in two
modes (array-of-structures and structure-of-arrays with bounding boxes). Clips are matched to a skeleton by the
overlap between their bone hashes and the skeleton's.

## Strand hair (`engine/smh_groom.py`, `smh_style.py`, `smh_gen.py`)

A hair, beard, brow, lash or fur item's drawable is a one-triangle stub; the hair is a separate "groom LOD
resource": a small system page and one GPU buffer that the strand compute shaders read through a header of byte
offsets. The buffer holds **guides** (the vertices of a scalp triangle mesh, each with a polyline of nodes, skin
weights and attributes) and per-triangle strand counts. Strands themselves are not stored: the game's compute
shaders scatter them over the scalp triangles and blend three guides. What Lineup does about that is described in
[strand-shaders.md](strand-shaders.md).
