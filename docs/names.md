# Names

The game's data files hardly ever store a name. A structure type, a field, an enum value, a model or a character
archetype is stored as a 32-bit hash of its name (Bob Jenkins' one-at-a-time hash of the name as written, so
`Head` and `head` differ). Lineup builds a list of the names it can learn from your own copy.

## How names are learned (`engine/names.py`, the `names` stage)

1. **Wanted hashes.** Every structure type, field name, enum and enum-value hash in the PSO members, and every
   value of a hash-typed field in their data blocks.
2. **Candidate words.** Tags, attribute names, attribute values and text of the XML members; NUL-terminated
   identifier strings inside the PSO members; and `engine/script_symbol_names.json`, this project's own short list
   of script words.
3. **Matching.** Each word is hashed as written and in lower case. A word that lands on a wanted hash becomes that
   hash's name. A hash that two different words reach is left out.
4. **Evidence.** For each name `names.json` records whether the word also occurs as a string inside a PSO member
   that holds the hash (`same-member`) or only turned up somewhere else (`elsewhere`).

The result is `WORK/library/names.json`: `{"names": {"<8 hex>": "<name>"}, "evidence": {...}, "stats": {...}}`.
Nothing is downloaded and no name list ships with Lineup. If the file is missing everything still works and
labels stay hashes (`0x2ca58759`, `a_ped_mr1_cau_0e0e822d`).

Where the names are used: they are a vocabulary for cracking character archetype hashes (`ped_archetypes.py`, the
same token filters as its archive scan apply) and outfit slot hashes (`char_outfits.py`), and they label fields
when `decode_pso.py` is run on a file.

## How much is learned

Measured on one copy of version 1.0.2.1 (8,253 PSO members, 343,178 candidate words):

| Kind | Wanted | Named |
|---|---|---|
| structure types | 7,266 | 86 |
| fields | 20,851 | 487 |
| enums | 1,248 | 0 |
| enum values | 21,790 | 578 |
| hash field values | 583,529 | 11,103 |
| **total** | **634,684** | **12,254 (1.9%)** |

Most hashes stay unnamed: most hash-typed values are not names of anything that appears in plain text in these
files. 922 of the 12,254 names also occur in the member that holds their hash.

## The collision caveat

A 32-bit hash is sometimes hit by chance. Hashing the same candidate words with a deliberately different hash
function ("salted") lands about 100 spurious names among the 12,258 found with the real one, roughly 0.8%. You
cannot tell which names are accidents, but `same-member` names are far less likely to be than `elsewhere` ones. Treat
`elsewhere` names as likely, not certain.

A concrete case: the hash of the ped archetype `189b402c` is also the hash of `indirect_light_near_light_distance_min`,
a render-setting word found in the XML. Lineup therefore applies to the tokens from `names.json` the same filters
the archive scan in `ped_archetypes.py` uses (a token is tried when it is at most 36 characters long or looks like a
ped name), and that archetype keeps its hash-based name `s_ped_mr1_nat_189b402c`. A stricter evidence rule may still
be needed if another accident shows up.

## The PSO type codes

`tools/verify_pso_types.py` measures, on your copy, every field type code that occurs in the PSO schemas: how many
fields and files use it, its width (an upper bound from the gap to the next field, a lower bound from the highest
non-zero data byte), and checks that fit the reading (booleans are 0 or 1, floats are finite and plausible, hash
fields hit names from this stage, enum values are members of their enum). It compares the result with
`decode_pso.FORMATS` and `decode_pso.TYPE_NAMES` and exits with status 1 when a measurement contradicts them:

    python tools/verify_pso_types.py            # uses the saved work folder; --work PATH to choose one
    python tools/verify_pso_types.py --json codes.json

An XML cross-check compares values of classes that appear both in the XML members and as PSO structures; on
version 1.0.2.1 only 4 such classes exist, with 8 values, so that check is inconclusive.
