# How Lineup works

## The three places

| Place | What it is | Written by Lineup? |
|---|---|---|
| **GAME** | Your copy of the game: the folder that holds `MicrosoftGame.Config`, `packs.rpf`, `data.rpf`, `common.rpf`, `anim.rpf`, `pack/` and `data/ttdb/` as plain files. | Never. Files there are opened for reading only. |
| **WORK** | Everything Lineup derives from the game: indexes, databases, decoded models and textures. Safe to delete; the next run rebuilds it. About 0.9 GB after a build, plus a cache that grows as you browse. | Yes. |
| **ENGINE** | The code in `engine/`, plus the codec tables in `engine/bcpack_data/` and `engine/script_symbol_names.json`. | No (it is the program). |

The two folders are chosen with `--game` and `--work`, or found and saved in a settings file in your user data
folder (`%LOCALAPPDATA%\Lineup` on Windows, `~/Library/Application Support/Lineup` on macOS,
`~/.local/share/lineup` on Linux). They reach worker processes through the environment variables `LINEUP_GAME` and
`LINEUP_WORK`. The default work folder is `work/` inside that user data folder.

Inside WORK:

| Folder or file | Content |
|---|---|
| `index/<archive>/` | the span index, the PSO members and XML members found in each archive; `index/drawables.csv` |
| `library/` | the SQLite databases and `names.json` that the viewer reads |
| `cache/` | decoded models (`glb/`), textures (`tex/`, `gtex/`), hair (`grooms/`, `char_grooms/`) |
| `native/` | the optional compiled texture helpers |
| `strand_shaders/` | optional, made by you (see [strand-shaders.md](strand-shaders.md)) |
| `build.json` | the receipt that says which stages are finished |

## Finding the game and checking it

`python lineup.py` first looks for a game folder: the one you gave, the saved one, or copies it finds in Downloads,
Desktop, Documents, Games, the home folder and the top of each drive (Windows), `/Volumes` (macOS) or `/mnt`,
`/media/<you>`, `/run/media/<you>` (Linux), two folders deep. It only asks whether `MicrosoftGame.Config` and
`packs.rpf` exist; it reads no file contents while searching.

It then checks that the copy is version 1.0.2.1: the `Version` of the `Identity` element in
`MicrosoftGame.Config`, and the sizes of `packs.rpf`, `data.rpf`, `common.rpf` and `anim.rpf`. Every missing or
different item is listed at once. Lineup's indexes rely on where data sits inside the archives of this version, so
it stops on a mismatch unless you pass `--force`.

## The build

The build is a list of stages. `WORK/build.json` records, for each finished stage, its version number, a signature
of its inputs (the sizes and modification times of the game files it reads) and the output signature of every stage
it reads from. A stage is skipped when all three still match. If a game file changed or a stage version was raised
by an update, that stage and every stage that reads from it run again. A stage that is interrupted keeps no receipt
and starts again; the `index` stage resumes at the archive it was in.

| Stage | Reads | Writes |
|---|---|---|
| `index` | the character archives (`packs.rpf`, `data.rpf`, `common.rpf`, `anim.rpf`, and `pack/*/*.rpf` except the collision, script and licence packs) | per archive in `index/`: `deflate-spans.bin` (every checksum-verified zlib stream), `members.json` and the PSIN metadata members (`*.pso`), `xml/*.xml` |
| `names` | the PSO and XML members | `library/names.json` (see [names.md](names.md)) |
| `drawables` | `packs.rpf` and the content packs | `index/drawables.csv`, one row per named model resource |
| `textures` | the `.ttpf` texture packs in `data/ttdb` and `pack/*/data/ttdb`; the model archives | `library/textures.sqlite3` (every texture by its 64-bit name hash); per archive the textures embedded in model resources |
| `inventory` | `drawables.csv`, the model resources | `library/inventory.build.sqlite3` (box, LODs, materials, texture references per model), then `library/inventory.sqlite3` (categories) |
| `characters` | the PSO catalogues, the 2,709 per-character appearance catalogues, the inventory, `names.json` | `ped_archetypes.sqlite3`, `face_presets.sqlite3`, `clothing_presets.sqlite3`; then the inventory again with the archetype rows added |
| `grooms` | the hair, beard and fur assets | `library/grooms.sqlite3` |
| `animations` | the animation archives | `library/anim_clips.sqlite3` (clips, blendspaces, motion matching databases) |

The slow work (`index`, `characters`) takes minutes; the rest take about a minute each. Lineup finishes by trying
to compile its two optional C helpers (`engine/native_build.py`).

Each step of the `inventory`, `characters`, `grooms` and `animations` stages runs in its own Python process, as
separate scripts would. Several modules cache what they read once, and a step that reads a file an earlier step
wrote must not share a process with it. The processes run with `PYTHONHASHSEED=0` so that two builds of the same
game give the same databases.

## The viewer

`python lineup.py` starts a small web server on `127.0.0.1` (the first free port from 8910 to 8919) and opens your
browser. If Lineup is already running on that port for the same game folder it just opens the page.

| Route | What it serves |
|---|---|
| `/` | the character viewer (`viewer/models.html`) |
| `/grooms` | the strand hair viewer |
| `/api/info` | `{"app": "Lineup", ...}` |
| `/api/models/*` | `engine/model_service.py`: the model list, models as GLB, textures as PNG, skeletons, skin weights, animations, outfits, overlays |
| `/api/grooms/*` | `engine/groom_service.py`: strand hair as GLB |

The page shows three categories: Protagonists, Characters, and Clothing & accessories. The other categories stay
in the database because character features use them (the body rigs, cloth proxies and gore pieces).

Safety properties, each covered by `tests/test_server.py`:

- it binds to `127.0.0.1` only; there is no option to listen on the network;
- only the files listed in `engine/server.py` are served from `viewer/`, with their MIME types and
  `X-Content-Type-Options: nosniff`; paths are matched exactly, so `..` and encoded variants find nothing;
- only `GET` is accepted; other methods get 405;
- a request whose `Host` header is not `127.0.0.1`, `localhost` or `[::1]` is refused, and so is an API request
  the browser marks as cross-site, so a web page on another site cannot read your data through your browser;
- the page makes no request outside `127.0.0.1`: there are no fonts, scripts or analytics from the internet.

Models and textures are decoded when you open them and cached in `cache/`; the first open of a character takes a
few seconds.

## Checks you can run

| Command | What it checks |
|---|---|
| `python lineup.py --check` | the game folder and its version, nothing built |
| `python lineup.py --status` | which stages are built and the size of the work folder |
| `python -m unittest discover -s tests -p "test_*.py"` | unit tests that need no game files |
| `python tests/api_smoke.py` | a running server answers every route the viewer uses (needs a built work folder) |
| `python tests/page_check.py` | loads the page in a headless Chromium browser and reports console errors |
| `python tools/verify_pso_types.py` | measures the PSO field type codes on your copy ([names.md](names.md)) |
| `python tools/check_no_game_data.py` | the repository holds no game data |
