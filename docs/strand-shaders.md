# Strand hair and the optional shader listings

Hair, beards, brows, lashes and fur are stored as **guides** (a few thousand polylines on a scalp mesh) and
per-triangle strand counts. The individual strands are not stored: the game's compute shaders
(`smh_gen_1`, `smh_gen_2`, `smh_gen_4` and `smh_gen_16`, followed by a styling pass) scatter them over the scalp
triangles at run time. See [formats.md](formats.md) for the layout.

## What Lineup does without any listings (the normal case)

Lineup ships no shader code and no shader listing, and it does not read the game's shader archive. Strands are
produced by a Python model of the shaders, `smh_groom.strands()` in `engine/smh_groom.py`:

- the guides, the scalp triangles, the strand counts and the integer random sequences that pick each strand's
  triangle and position are read exactly from the asset;
- a strand is modelled in the rest pose as a blend of three guides, trimmed to the strand's length;
- the second strand class (clumps, curls) is approximate, and the style pass (curl, frizz) is not applied;
- inputs the game supplies at run time are not modelled: the length texture, skinning and simulation.

So hair looks like the right style, density and length, but individual strands are not the game's own. This is the
"strand hair is approximate" entry in the README. Cached hair files made this way have `.px` in their name
(`cache/grooms/`); files made with listings have `.sx`.

## What the optional listings are

If you put text listings of the game's own strand shaders in `WORK/strand_shaders/`, Lineup runs that GPU code in
a small interpreter (`engine/gcn_exec.py`) and applies the style pass (`engine/smh_style.py`). Strands then follow
the game's own arithmetic, with a few run-time inputs assumed (the density factor, an empty region mask, the rest
pose, a length texture of 1.0; each is named in the docstring of `engine/smh_gen.py`).

The files must be named like `*_smh_gen_<N>_CS_InterpolateStrands_*.c.s` for N = 1, 2, 4 and 16. The first line of
each names the shader image it was made from, and that image file must be in the same folder. Without all four
listings and their image Lineup keeps using the model above.

**Lineup does not ship these listings, does not include a tool that makes them, and does not explain how to
make them.** They are disassembly of a shader binary from the game, so they stay on your PC like everything else
Lineup builds, and they must never be shared or added to this repository (`tools/check_no_game_data.py` blocks the
file types). If you do not have them, nothing is missing that you need.
