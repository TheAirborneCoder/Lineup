# SPDX-License-Identifier: GPL-3.0-or-later
"""Categorize the model inventory and publish LIBRARY/inventory.sqlite3.

Evidence, strongest first:
  1. vehicle model names from the game's vehicle tint recipes (common.rpf CPaletteIndexedRecipeTintsArray)
  2. the texture-pack area (weapons, vehicles, peds, props, interiors, maps, ...) that a model's
     material texture hashes resolve to (LIBRARY/textures.sqlite3)
  3. native name families (cr*_ creatures, cs_/hc_/u_ characters, clothing type tokens, prop_ ...)
Map geometry is kept and tagged "World map geometry" so totals reconcile; the viewer hides it.
"""
import gzip
import json
import re
import sqlite3
import struct
from collections import Counter, defaultdict
from contextlib import closing
from paths import INDEX, LIBRARY, CACHE

SRC = LIBRARY / "inventory.build.sqlite3"
OUT = LIBRARY / "inventory.sqlite3"
TTDB_MANIFEST_CACHE = CACHE / "ttdb_manifest.json"

CLOTHING = re.compile(
    r"(?:^|_)(?:tshirt\w*|shirt\w*|sweatshirt\w*|tanktop\w*|hoodie\w*|jacket\w*|gilet\w*|coat\w*|blazer\w*|"
    r"sweater\w*|cardigan\w*|vest\w*|bodice\w*|dress\w*|skirt\w*|pants\w*|jeans|shorts\w*|leggings\w*|"
    r"undies\w*|bra(?!nd|ce|ss|nch|ke|in|wl)\w*|tapepasties\w*|swim\w*|bikini\w*|shoes\w*|boots\w*|"
    r"sandals\w*|sneakers\w*|socks\w*|hat\w*|cap\w*|helmet\w*|beanie\w*|mask\w*|glasses\w*|sunglasses\w*|"
    r"specs\w*|goggles\w*|gloves\w*|necklace\w*|bracelet\w*|anklets\w*|ring\w*|earring\w*|piercing\w*|"
    r"watch(?!er|tower)\w*|belt\w*|bag\w*|apron\w*|armor\w*|basebottoms\w*|basetops\w*|bodysuit\w*|"
    r"overalls\w*|fullbody\w*|costume\w*|scarf\w*|tie\w*|jumpsuit\w*|uniform\w*|accs\w*|scupperaccs\w*|"
    r"scloweraccs\w*|teethaccs\w*|medicaldressing\w*|holster\w*|backpack\w*|bandana\w*|wrapbandana\w*|"
    r"wrapbalaclava\w*|wrapscuba\w*|wraphood\w*|shades\w*|headphones\w*|wrapneckgaiter\w*|neckwear\w*|"
    r"gunstraps\w*|headband\w*|earmuffs\w*|slod_(?:lowr|uppr)\w*)(?:_|$)"
)
HAIR = re.compile(
    r"(?:^|_)(?:hair\w*|beard\w*|moustache\w*|eyebrow\w*|brows\w*|lashes\w*|stubble\w*|"
    r"bodyhair\w*|leghair\w*|armpithair\w*|chesthair\w*|genitalhair\w*)(?:_|$)"
)
PED_RIG_NAME = re.compile(r"_(?:fr|mr|fb|mb|fo|mo|fc|mc)\d_")
VARIANT = re.compile(r"_(hi|mid|lod\d*|slod\d*|batchlod|batch_lod|batch_slod\d*|batchslod\d*|cull_\d+|alt_\d+|full)$")
LOW_DETAIL = re.compile(r"^(?:mid|lod\d*|slod\d*|batchlod|batch_lod|batch_slod\d*|batchslod\d*)$")
PROP_GROUPS = [
    ("Food & drink", r"food|drink|can_|_can|soda|beer|bottle|cup|mug|glass_|burger|pizza|fruit|snack|cereal|coffee|"
                     r"juice|wine|liquor|plate|cutlery|bowl|caddy_condiment|menu|coke|weed|drug|pill|bong|"
                     r"cigarette|cigar|ashtray|vape|cake|bread|meat|fish_|steak|icecream"),
    ("Furniture", r"chair|table|sofa|couch|bed_|_bed|desk|shelf|shelving|cabinet|stool|bench|wardrobe|dresser|"
                  r"drawer|bookcase|ottoman|armchair|counter|parasol|rack_"),
    ("Appliances & electronics", r"fridge|freezer|microwave|oven|stove|washer|dryer|tv_|_tv|monitor|computer|laptop|"
                                 r"phone|radio|speaker|console|printer|fan_|aircon|heater|vending|atm_|arcade|"
                                 r"electric|scanner|camera|register|cctv|router|server|battery|solar"),
    ("Containers & storage", r"box|crate|container|barrel|bin_|_bin|bag_|_bag|basket|case_|tank_|jerrycan|keg|pallet|"
                             r"bucket|cooler|trunk|luggage|suitcase|duffle|chest|safe_"),
    ("Signs & displays", r"sign|billboard|poster|banner|display|flag|board|calendar|2d_|neon|marquee|placard"),
    ("Decor, art & print", r"picture|photo|frame|mirror|_art_|^art_|_orn_|rug|carpet|curtain|pillow|cushion|vase|"
                           r"statue|trophy|clock|book|magazine|newspaper|card|paper|decal|painting|tapestry|candle"),
    ("Lighting", r"light|lamp|lantern|bulb|chandelier|sconce|spotlight|floodlight"),
    ("Tools & equipment", r"tool|ladder|drill|hammer|wrench|generator|pump|hose|cone|barrier|scaffold|"
                          r"wheelbarrow|toolbox|extinguisher|compressor|welder|saw_|jack_|toolbox|medical|medkit"),
    ("Street & urban infrastructure", r"utilitypole|gantry|bridge|crane|cables|pylon|hydrant|manhole|bollard|traffic|"
                                      r"mailbox|busstop|parking|meter|pole_|_pole|post_|curb|antenna|satellite|"
                                      r"transformer|junction|powerline|rail_"),
    ("Trash, debris & clutter", r"rubbish|_rub_|^rub_|litter|debris|dumpster|trash|garbage|junk|clutter|rubble|scrap"),
    ("Sports, toys & leisure", r"ball|toy|sport|golf|tennis|surf|bike|skate|kayak|fishing|gym|pool_|beach|umbrella|"
                               r"guitar|piano|drum_|instrument|casino|poker|dart|arcade|plush|trophy|racing"),
    ("Vehicles & parked models", r"veh_|boat|wreck|car_|tyre|tire|wheel|trailer|cart|ship|dock|pier|anchor|buoy|cargo"),
    ("Clothing & retail items", r"widget_inv|cloth|shoe|sneaker|tshirt|shirt|pants|dress|hat_|wallet|purse|towel|"
                                r"backpack|jewel|gold|silver|bronze|cash|money|coin|mannequin|hanger|watch|ring"),
    ("Weapons & tactical props", r"_gun|gun_|ammo|target|holster|knife|rifle|pistol|bullet|shell|grenade|sword"),
    ("Plants & nature", r"plant|tree|bush|flower|pot_|rock|log|driftwood|grass|leaf|leaves|coral|cactus|palm|hedge"),
    ("Architecture & fixtures", r"door|window|gate|fence|railing|pipe|vent|grate|shutter|blind|wall|ceiling|floor|"
                                r"stair|pillar|column|roof|fakeint|balcony|beam|arch|awning|canopy|rope|chain"),
]
WEAPON_KIND = {"ar": "Assault rifles", "smg": "Submachine guns", "pi": "Pistols", "rv": "Revolvers",
               "sg": "Shotguns", "sr": "Sniper rifles", "tr": "Rifles (tr family)", "mg": "Machine guns",
               "lr": "Launchers & flare guns", "me": "Melee weapons", "ex": "Explosives & throwables",
               "ammo": "Ammunition", "at": "Attachments (shared)", "kit": "Equipment", "sports": "Sports items"}
WEAPON_FAMILY = re.compile(r"(w_(?:ar|smg|pi|rv|sg|sr|tr|mg|lr|me|ex|kit|sports)_[a-z0-9]+_\d+)")
SENTINEL_TEX = "5d7a3a87cc19967d"  # RAGE unbound/null placeholder texture hash

ANIMAL_ALIASES = {
    "a_c_alligator_01": "crr_alligator_american_m_00",
    "a_c_bat_01": "crm_bat_brownbig_m_00",
    "a_c_crawfish_01": "crc_crawfish_louisiana_m_00",
    "a_c_goosecanada_01": "crb_goose_canada_m_00",
    "a_c_parrot_01": "crb_parrot_macawscarlet_m_00",
    "a_c_turkeywild_01": "crb_turkey_wildflorida_m_00",
    "crm_dolphin_shortbeakedcommon_m_00": "crm_dolphin_shortbeaked_m_00",
}
ANIMAL_PART_STOP = {
    "body", "head", "eye", "eyes", "teeth", "tongue", "fur", "facialfur", "feather", "feathers",
    "wing", "wings", "tail", "upper", "lower", "antler", "antlerb", "horn", "horns", "shell",
    "gill", "fin", "legs", "collar", "harness", "generic", "hat", "shirt", "vest", "goggles",
    "costume", "corpse", "roadkill", "default", "short", "long", "collarharness",
}
ANIMAL_SLOTS = [
    ("antlerb", ["antlerb", "antler", "horn"]),
    ("antler", ["antler", "antlerb", "horn"]),
    ("horn", ["horn", "antler"]),
    ("teeth", ["teeth"]),
    ("tongue", ["tongue"]),
    ("eyes", ["eye"]),
    ("eye", ["eye"]),
    ("head", ["head"]),
    ("feather", ["feather"]),
    ("wings", ["wing"]),
    ("wing", ["wing"]),
    ("tail", ["tail"]),
    ("shell", ["shell", "body"]),
    ("gill", ["gill"]),
    ("fin", ["fin"]),
    ("legs", ["legs", "body"]),
    ("body", ["body"]),
]


def animal_part_keys(name):
    """Return (primary_keys, lookup_keys) of (tier, scope, sex, slot) tuples for species-shared texture derivation."""
    base, _ = split_variant(name.lower())
    for k, v in ANIMAL_ALIASES.items():
        if base == k or base.startswith(k + "_"):
            base = v + base[len(k):]
            break
    toks = base.split("_")
    if len(toks) < 3 or not re.match(r"^cr[abcfimr]$", toks[0]):
        return [], []
    genus = f"{toks[0]}_{toks[1]}"
    genus_species = f"{toks[0]}_{toks[1]}_{toks[2]}"
    sex = next((t for t in toks[2:5] if t in ("m", "f")), "")
    rest = set(toks[2:])
    if "head" in rest and "feather" in rest:
        slots = ["head_feather", "feather"]
    elif "facialfur" in rest:
        slots = ["facialfur", "fur"]
    elif "fur" in rest:
        slots = ["fur"]
    else:
        slots = next((cand for tok, cand in ANIMAL_SLOTS if tok in rest), ["body"])

    def expand(slot):
        keys = []
        if sex:
            keys.append(("gs_sex", genus_species, sex, slot))
        keys.append(("gs", genus_species, "", slot))
        if sex:
            keys.append(("g_sex", genus, sex, slot))
        keys.append(("g", genus, "", slot))
        return keys

    primary = expand(slots[0])
    lookup = [k for s in slots for k in expand(s)]
    return primary, lookup


def animal_family(name, frag_roots):
    """Group all parts of a creature (body, head, eyes, teeth, feathers, harness, groom) under one family."""
    base, _ = split_variant(name.lower())
    for k, v in ANIMAL_ALIASES.items():
        if base == k or base.startswith(k + "_"):
            base = v + base[len(k):]
            break
    if base.startswith("crm_cattle_holstein_f_fur_"):
        return "crm_cattle_holstein_f_00"
    if base.startswith("crm_dog_poodlestandard_m_fur_"):
        return "crm_dog_poodlestandard_m_00"
    if base.startswith("crm_cat_shorthairamerican_m_01_") or base.startswith("crm_cat_shorthairamerican_m_eye"):
        return "crm_cat_shorthairamerican_m_00"
    if base.startswith("crm_generic_dog_"):
        return "crm_generic_dog_collar"
    if base.startswith("slod_"):
        return ""
    hit = next((f for f in frag_roots if base == f or base.startswith(f + "_")), None)
    if hit:
        return hit
    toks = base.split("_")
    if len(toks) >= 2 and re.match(r"^cr[abcfimr]$", toks[0]):
        out = toks[:2]
        i = 2
        if i < len(toks) and toks[i] not in ANIMAL_PART_STOP and toks[i] not in ("m", "f") and not toks[i].isdigit():
            out.append(toks[i])
            i += 1
        if i < len(toks) and toks[i] in ("m", "f", "standard"):
            out.append(toks[i])
            i += 1
        if i < len(toks) and toks[i].isdigit():
            out.append(toks[i])
        return "_".join(out)
    return ""


HELICOPTER_CLASS = "0x196dda40"   # the aircraft branch whose members are buzzard, maverick, cargobob ...
VEHICLE_PSO = INDEX / "data.rpf/00001c35b000.pso"
CLASS_LABELS = [("aircraft", None), ("boat", "Boats & watercraft"), ("bicycle", "Bicycles"), ("bike", "Motorcycles"),
                ("trailer", "Trailers"), ("bus", "Buses"), ("tractor", "Tractors"), ("truck", "Trucks"),
                ("van", "Vans & box trucks"), ("utility", "Utility vehicles"), ("car", "Cars")]


def vehicle_classes(vehicles):
    """Vehicle subtype from the game's vehicle hierarchy (data.rpf PSO: name, parent, children records)."""
    from decode_pso import decode_bytes
    from script_tree import _name_hash
    parent = {}

    def walk(o):
        if isinstance(o, dict):
            if "0x93db5464" in o and "0x49a01a86" in o:
                parent[o["0x93db5464"]] = o["0x49a01a86"]
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    try:
        walk(decode_bytes(VEHICLE_PSO.read_bytes(), {}, VEHICLE_PSO.name)["blocks"])
    except (OSError, ValueError):
        pass        # no vehicle hierarchy in this copy: every vehicle falls back to its name below
    label = {f"0x{_name_hash(w):08x}": (w, text) for w, text in CLASS_LABELS}
    out, evidence = {}, {}
    for v in vehicles:
        chain, node = [], parent.get(f"0x{_name_hash(v):08x}")
        while node and node != "0x00000000" and len(chain) < 12:
            chain.append(node)
            node = parent.get(node)
        kind = None
        for node in chain:
            if node in label:
                word, text = label[node]
                kind = ("Helicopters" if HELICOPTER_CLASS in chain else "Planes") if word == "aircraft" else text
                evidence[v] = "vehicle hierarchy: " + word
                break
        if kind is None:
            # Not in the hierarchy (rail cars, baggage and boat trailers, damaged bus variants, utility/heavy).
            kind = ("Trains & rail" if re.search(r"train|freight|tanker|metro", v) else
                    "Trailers" if "trailer" in v else
                    "Buses" if v.startswith("bus") else
                    "Trucks" if v in ("dump", "guardiantx2") else
                    "Utility vehicles" if v in ("airmule", "spreader") else
                    "Other vehicles")
            evidence[v] = "name (not in the vehicle hierarchy)"
        out[v] = kind
    return out, evidence


def vehicle_names():
    try:
        x = (INDEX / "common.rpf/xml/000000449000.xml").read_text("utf-8", "replace")
    except OSError:
        return set()        # the vehicle catalogue is not indexed: no vehicle names are known
    return {n.lower() for n in re.findall(r"<vehicleName>([^<]+)</vehicleName>", x)}


def texture_areas():
    t = sqlite3.connect(LIBRARY / "textures.sqlite3")
    areas = defaultdict(set)
    for h, area, package in t.execute("select t.hash, p.area, p.package from texture t join pack p on p.id=t.pack"):
        areas[h].add(area)
    return areas


def split_variant(name):
    m = VARIANT.search(name)
    return (name[:m.start()], m[1]) if m else (name, "")


DATA_EMITTERS = {"amb_lights", "core", "ent_core", "ent_core_amb", "exp_core", "scr_car_wash", "veh_core", "weap_core"}
DATA_SKY = {"horizon", "skydome"}


def classify(r, vehicles, vfamily, areas_hist):
    n = r["name"].lower()
    arc = r["archive"]
    top = areas_hist.most_common(1)[0][0] if areas_hist else ""
    # Physics & bounds proxies
    if "bipedbounds" in n or "birdbounds" in n or "boundprims" in n or "batarchetype" in n or \
            re.match(r"char_.*(?:bounds|archtype|archetype)", n):
        return "Physics & bounds proxies", "Character, animal & cloth bounds"
    if n.endswith("_bounds"):
        return "Physics & bounds proxies", ("Map & interior bounds" if arc == "levels.rpf" else "Character, animal & cloth bounds")
    # Sky, horizon & far LODs in data.rpf
    if arc == "data.rpf" and (n in DATA_SKY or n.startswith("farlod_")):
        return "World map geometry", "Sky, horizon & far LOD"
    # Effects (particles, gore, unit-cube emitters)
    if n.startswith("ptfx"):
        return "Effects", "Particle effect meshes"
    if n.startswith("gore") or "gorecap" in n or "gorechunk" in n:
        return "Effects", "Gore & damage pieces"
    if n.startswith("anm_") or (arc == "data.rpf" and n in DATA_EMITTERS):
        return "Effects", "Effect & animation emitters"
    # Protagonists (ped archives only — props/buildings mentioning lucia/jason belong in Props/Map)
    is_ped_source = arc == "packs.rpf" or top == "peds" or (
        arc == "pack/core_restofworld/core_restofworld.rpf" and bool(PED_RIG_NAME.search(n)))
    if is_ped_source and not re.match(r"prop_|mint_|prefab_|cvar_", n):
        if re.search(r"(?:^|_)(?:lucia|jason)_(?:dad|mom|mum|father|mother|brother|sister|uncle|aunt)(?:_|$)", n):
            return "Characters", "Story characters"
        if re.search(r"(?:^|_)lucia(?:_|$)", n) or n.startswith("player_story000_00_head"):
            return "Protagonists", "Lucia"
        if re.search(r"(?:^|_)jason(?:_|$)", n) or n.startswith("player_story000_01_head"):
            return "Protagonists", "Jason"
        if n.startswith("player_"):
            return "Protagonists", "Player wardrobe"
    # Animals (all creature families + ambient creatures)
    if re.match(r"crm_|slod_mammal|a_c_bat", n):
        return "Animals", "Mammals"
    if re.match(r"crb_|slod_bird|a_c_(?:goose|parrot|turkey)", n):
        return "Animals", "Birds"
    if re.match(r"crf_|slod_fish", n):
        return "Animals", "Fish & sea life"
    if re.match(r"crr_|slod_reptile|a_c_alligator", n):
        return "Animals", "Reptiles"
    if re.match(r"cra_", n):
        return "Animals", "Amphibians"
    if re.match(r"crc_|a_c_crawfish", n):
        return "Animals", "Crustaceans"
    if re.match(r"cri_", n):
        return "Animals", "Insects & arachnids"
    if re.match(r"slod_animal|a_c_", n):
        return "Animals", "Other animals"
    # Vehicles
    if n in vfamily:
        return "Vehicles", vfamily[n]
    fam = next((v for v in sorted(vehicles, key=len, reverse=True) if n.startswith(v + "_")), None)
    if fam and arc == "levels.rpf" and r["kind"] == "fragment":
        return "Vehicles", vfamily[fam]
    if re.match(r"(?:cmn|truckbeds)_", n) and r["kind"] == "fragment":
        return "Vehicles", "Shared vehicle parts"
    if arc == "levels.rpf" and re.match(r"(?:rim|tyre)_", n):
        return "Vehicles", "Shared vehicle parts"
    # Weapons
    if arc == "data.rpf" and re.match(r"[ad]?w_", n):
        kind = n.split("_")[1] if n.startswith("w_") else ""
        return "Weapons", WEAPON_KIND.get(kind, "Other weapon models")
    if re.match(r"w_preset_|wpn_", n):
        return "Weapons", "Weapon presets & proxies"
    if top == "weapons" and arc == "data.rpf":
        return "Weapons", "Other weapon models"
    # Vegetation
    if re.match(r"veg_|prefab_bush|prefab_tree", n):
        return "Vegetation", "Plants & trees"
    # Props (explicit native prop prefixes)
    if re.match(r"prop_|prefab_|cvar_|mint_|ib_", n):
        for label, pat in PROP_GROUPS:
            if re.search(pat, n):
                return "Props", label
        return "Props", "Other props"
    if top == "vehicles" and arc in ("packs.rpf", "data.rpf"):
        return "Vehicles", "Other vehicle parts"
    # Characters & clothing (ped sources only — prevents map buildings in pack/* leaking in)
    if re.match(r"cs_", n):
        return "Characters", "Story characters"
    if re.match(r"u_", n):
        return "Characters", "Unique characters"
    if re.match(r"sc_", n) and is_ped_source:
        return "Characters", "Special & creature-like characters"
    if re.match(r"hc_|head_|a_head_|slod_head_|teeth(?!accs)", n) and is_ped_source:
        return "Characters", "Heads & faces"
    if re.match(r"ambient_|legacyped", n) and is_ped_source:
        return "Characters", "Ambient & legacy bodies"
    if is_ped_source:
        if HAIR.search(n):
            return "Characters", "Hair & facial hair"
        if CLOTHING.search(n):
            return "Clothing & accessories", clothing_kind(n)
        return "Characters", "Body parts & other character pieces"
    # World map geometry
    if re.match(r"[ns]\d+_[ew]\d+", n):
        return "World map geometry", "Terrain grid tiles"
    if arc.startswith("pack/"):
        return "World map geometry", "Content-pack map pieces (" + arc.split("/")[-1] + ")"
    if arc != "levels.rpf":
        return "Other models", arc
    return "World map geometry", "Map blocks, buildings, terrain and roads"


def clothing_kind(n):
    for label, pat in [("Tops", r"tshirt|shirt|sweatshirt|tanktop|hoodie|sweater|cardigan|basetops|bodysuit|bodice|top|slod_uppr"),
                       ("Outerwear", r"jacket|gilet|coat|blazer|vest|armor|apron"),
                       ("Bottoms", r"pants|jeans|shorts|skirt|leggings|basebottoms|overalls|slod_lowr"),
                       ("Dresses & one-pieces", r"dress|jumpsuit|swim|bikini|uniform|fullbody|costume"),
                       ("Underwear", r"undies|bra(?!nd|ce|ss|nch|ke|in|wl)|tapepasties"),
                       ("Footwear", r"shoes|boots|sandals|sneakers|socks|foot"),
                       ("Headwear & masks", r"hat|cap|helmet|beanie|mask|wrapbalaclava|wrapscuba|wraphood"),
                       ("Jewelry & accessories", r"necklace|bracelet|anklets|ring|earring|piercing|watch|belt|bag|glasses|"
                                                 r"specs|goggles|scarf|tie|gloves|accs|holster|backpack|bandana|shades|"
                                                 r"headphones|gaiter|neckwear|gunstraps|headband|earmuffs|medicaldressing")]:
        if re.search(pat, n):
            return label
    return "Other clothing"


def _is_animal_ragdoll_proxy(r, found):
    return r["kind"] == "fragment" and (r.get("vbytes") or 0) <= 1024 and (r.get("ibytes") or 0) <= 512 and found == 0


def _is_animal_groom_proxy(r, found):
    return "_fur_" in r["name"].lower() and (r.get("vbytes") or 0) <= 256 and found == 0


def _is_ped_proxy(r, found):
    n = r["name"].lower()
    return found == 0 and ((r.get("vbytes") or 0) <= 256 or n.startswith("proxycap_") or "singlepoly" in n)


PED_GARMENT_CLASSES = [
    ("gore", r"^gore|gorecap|gorechunk"),
    ("piercing", r"piercing"),
    ("earring", r"earring"),
    ("necklace", r"necklace"),
    ("bracelet", r"bracelet|anklet"),
    ("ring", r"(?:^|_)ring"),
    ("watch", r"watch"),
    ("shades", r"shades|sunglasses"),
    ("specs", r"specs|glasses"),
    ("goggles", r"goggles"),
    ("helmet", r"helmet"),
    ("mask", r"mask|balaclava|scuba"),
    ("hat", r"hat|cap(?!e)|beanie|visor|headband|earmuffs|bandana"),
    ("headphones", r"headphones"),
    ("gloves", r"gloves"),
    ("boots", r"boots"),
    ("shoes", r"shoes|sneakers|sandals|socks"),
    ("jeans", r"jeans"),
    ("pants", r"pants|leggings|basebottoms|slod_lowr"),
    ("shorts", r"shorts"),
    ("skirt", r"skirt"),
    ("overalls", r"overalls|jumpsuit|fullbody|bodysuit|uniform|costume"),
    ("dress", r"dress"),
    ("swim", r"swim|bikini"),
    ("bra", r"(?:^|_)bra(?!ce|nd|ss)|tapepasties"),
    ("undies", r"undies"),
    ("jacket", r"jacket|coat|blazer|gilet"),
    ("armor", r"armor|holster|gunstraps"),
    ("vest", r"vest|apron"),
    ("hoodie", r"hoodie|sweatshirt|sweater|cardigan"),
    ("tshirt", r"tshirt|tanktop|basetops|bodice|slod_uppr"),
    ("shirt", r"shirt"),
    ("bag", r"bag|backpack"),
    ("belt", r"belt"),
    ("tagname", r"tagname|badge|medicaldressing"),
    ("legacyhead", r"legacyhead"),
    ("legacyped", r"legacyped"),
    ("hand", r"(?:^|_)hand"),
    ("feet", r"(?:^|_)(?:feet|foot)"),
    ("teeth", r"teeth"),
    ("eye", r"(?:^|_)eye"),
    ("hair", r"hair|beard|brows|lashes|stubble|moustache"),
    ("body", r"(?:^|_)body"),
]
PED_PREFIX_STRIP = re.compile(
    r"^(?:player_story\d+_\d+_|hc_cs_[a-z0-9_]+?_(?=head_)|hc_[a-z0-9_]+?_(?=head_)|"
    r"cs_[a-z0-9_]+?_(?=(?:old_head_|head_|legacy|tshirt|shirt|sweatshirt|tanktop|hoodie|jacket|gilet|coat|blazer|"
    r"sweater|cardigan|vest|bodice|dress|skirt|pants|jeans|shorts|leggings|undies|bra|swim|bikini|shoes|boots|"
    r"sandals|sneakers|socks|hat|cap|helmet|beanie|mask|glasses|sunglasses|specs|goggles|gloves|necklace|bracelet|"
    r"anklets|ring|earring|piercing|watch|belt|bag|apron|armor|basebottoms|basetops|bodysuit|overalls|fullbody|"
    r"costume|scarf|tie|jumpsuit|uniform|accs|holster|backpack|bandana|wrap|shades|headphones|neckwear|gunstraps|"
    r"headband|earmuffs|hand_|feet_|body|hair|beard|brows|lashes))|"
    r"u_[mf]_[a-z0-9_]+?_(?=head_)|sc_[a-z0-9]+_|ambient_[a-z0-9]+_)"
)


def ped_family(name, cat):
    """Group character heads with their companion _eyes, _eyecap, _teeth, and _tattooprojection meshes."""
    if cat not in ("Protagonists", "Characters"):
        return ""
    base, _ = split_variant(name.lower())
    if base.startswith("slod_"):
        return ""
    stem = re.sub(r"_(?:eyes|eyecap|teeth|tattooprojection)$", "", base)
    if stem != base or re.search(r"(?:^|_)head_(?:fr|mr|fb|mb|fo|mo|fc|mc)\d", base):
        return stem
    return ""


def head_eth_keys(name):
    """Return ordered lookup keys (rig_eth, rig, 'any') for ped_standard_head normal-map derivation."""
    n = name.lower()
    m = re.search(r"((?:fr|mr|fb|mb|fo|mo|fc|mc)\d)_(cau|lat|afr|asi|gen|base)", n)
    if m:
        return [("eth", f"{m[1]}_{m[2]}"), ("rig", m[1]), ("any", "head")]
    m2 = re.search(r"((?:fr|mr|fb|mb|fo|mo|fc|mc)\d)", n)
    if m2:
        return [("rig", m2[1]), ("any", "head")]
    return [("any", "head")]


def head_skin_tint(name):
    """Natural base skin tint keyed by character ethnicity token when runtime head-blend albedo is unbound."""
    n = name.lower()
    if "_afr_" in n:
        return [0.42, 0.29, 0.22]
    if "_lat_" in n:
        return [0.79, 0.60, 0.49]
    if "_asi_" in n:
        return [0.82, 0.66, 0.55]
    if "albino" in n:
        return [0.90, 0.82, 0.78]
    if "alien" in n:
        return [0.52, 0.62, 0.56]
    return [0.83, 0.66, 0.57]


def garment_default_tint(name):
    """Sensible fabric base tint for procedural MUV garments whose color palette is bound at runtime."""
    n = name.lower()
    if "jeans" in n or "denim" in n:
        return [0.24, 0.34, 0.48]
    if "cowboy" in n or "hat" in n or "straw" in n:
        return [0.45, 0.33, 0.23]
    if "tactical" in n or "military" in n or "army" in n or "cargo" in n:
        return [0.32, 0.35, 0.28]
    if "leather" in n or "biker" in n or "boots" in n or "belt" in n or "holster" in n:
        return [0.23, 0.19, 0.17]
    if "braceletchain" in n or "necklace" in n or "gold" in n or "earring" in n or "ring" in n:
        return [0.78, 0.65, 0.36]
    if "gore" in n:
        return [0.52, 0.14, 0.13]
    return [0.64, 0.62, 0.59]


BAD_PED_DONOR_RE = re.compile(
    r"_(?:fsk|msk)\d|_(?:mc|fc)1_001_|corpse|skeleton|zombie|gore|alien|demon|sexrobot|formaldeh|mannequin"
)


def ped_garment_keys(name):
    """Return (primary_keys, lookup_keys) for sibling garment/accessory/gore texture derivation."""
    base, _ = split_variant(name.lower())
    if base.startswith("ptfx_model_"):
        return [("ptfx", "ptfx_gore")], [("ptfx", "ptfx_gore")]
    clean = PED_PREFIX_STRIP.sub("", base)
    if clean.startswith("old_head_"):
        clean = clean[4:]
    primary, lookup = [], []
    m = re.match(r"^([a-z0-9]+)_((?:fr|mr|fb|mb|fo|mo|fc|mc|mk|fk|mi|fi)\d)_(\d+)", clean)
    is_skin_part = False
    if m:
        gtype, rig, num = m[1], m[2], m[3]
        if gtype in ("hand", "feet"):
            is_skin_part = True
            if clean.endswith("_fp") or "_fp_" in clean:
                gtype = f"{gtype}_fp"
            keys = [
                ("exact", f"{gtype}_{rig}_{num}"),
                ("grig", f"{gtype}_{rig}"),
                ("gsex", f"{gtype}_{rig[0]}"),
                ("gtype", gtype),
            ]
        elif gtype == "bodygenitalsgen":
            is_skin_part = True
            keys = [("exact", f"{gtype}_{rig}_{num}"), ("grig", f"{gtype}_{rig}")]
            if rig.startswith("f"):
                # Female bodygenitalsgen shares feet_fr1's 512x1024 lower-body UV layout & normal map.
                keys.extend([("gsex", "feet_f"), ("gtype", "feet")])
        elif gtype == "bodynipplesgen":
            is_skin_part = True
            # Female bodynipplesgen shares hand_fr1's 512x1024 upper-body UV layout & normal map.
            keys = [
                ("exact", f"{gtype}_{rig}_{num}"),
                ("grig", f"{gtype}_{rig}"),
                ("gsex", f"hand_{rig[0]}"),
                ("gtype", "hand"),
            ]
        else:
            keys = [("exact", f"{gtype}_{rig}_{num}"), ("grig", f"{gtype}_{rig}"), ("gtype", gtype)]
        primary.extend(keys)
        lookup.extend(keys)
    else:
        tok = clean.split("_")[0]
        if tok:
            primary.append(("gtype", tok))
            lookup.append(("gtype", tok))
    if not is_skin_part:
        for cls_name, pat in PED_GARMENT_CLASSES:
            if re.search(pat, clean) or re.search(pat, base):
                k = ("gclass", cls_name)
                primary.append(k)
                lookup.append(k)
                break
    return primary, lookup


def load_ttdb_manifest(ttpf_hashes=None, rebuild=False):
    """Load or build virtual->physical .ttpf redirections and baked RGB tints from RAGE 0x93c495af PSOs."""
    if not rebuild and TTDB_MANIFEST_CACHE.is_file():
        try:
            return json.loads(TTDB_MANIFEST_CACHE.read_text("utf-8"))
        except Exception:
            pass
    from decode_pso import decode_bytes
    if ttpf_hashes is None:
        t = sqlite3.connect(LIBRARY / "textures.sqlite3")
        ttpf_hashes = {r[0] for r in t.execute("select hash from texture")}
        t.close()
    redirects, colors = {}, {}
    for p in sorted(INDEX.rglob("*.pso")):
        if p.stat().st_size > 25_000_000:
            continue
        blob = p.read_bytes()
        if b"\x93\xc4\x95\xaf" not in blob and b"\xaf\x95\xc4\x93" not in blob:
            continue
        try:
            d = decode_bytes(blob, {}, p.name)
        except Exception:
            continue
        blocks = {b["block"]: b for b in d.get("blocks", ())}
        schemas = d.get("schemas", {})
        for b in d.get("blocks", ()):
            for rec in b.get("records", ()):
                if not isinstance(rec, dict) or not isinstance(rec.get("0x93c495af"), list):
                    continue
                for entry in rec["0x93c495af"]:
                    if not isinstance(entry, dict):
                        continue
                    kv = entry.get("0x6098a50e")
                    if isinstance(kv, dict) and len(kv.get("raw_hex", "")) == 16:
                        vhash = bytes.fromhex(kv["raw_hex"])[::-1].hex()
                    elif isinstance(kv, int):
                        vhash = f"{kv & ((1 << 64) - 1):016x}"
                    elif isinstance(kv, str) and kv.startswith("0x"):
                        vhash = f"{int(kv, 16) & ((1 << 64) - 1):016x}"
                    else:
                        continue
                    val = entry.get("0x063fa3f2")
                    if not isinstance(val, dict):
                        continue
                    col = val.get("0x8e9261bb")
                    if isinstance(col, str) and col.startswith("0x"):
                        col = int(col, 16)
                    if isinstance(col, int) and col != 0:
                        rgb = [round((col & 0xff) / 255.0, 4), round(((col >> 8) & 0xff) / 255.0, 4),
                               round(((col >> 16) & 0xff) / 255.0, 4)]
                        if any(x > 0 for x in rgb):
                            colors[vhash] = rgb
                    ptr_v = val.get("0xe6657b17")
                    if isinstance(ptr_v, dict) and len(ptr_v.get("raw_hex", "")) == 48:
                        raw = bytes.fromhex(ptr_v["raw_hex"])
                        pw = struct.unpack_from("<Q", raw, 8)[0]
                        cnt = struct.unpack_from("<H", raw, 16)[0]
                        if pw and cnt:
                            tb = blocks.get((pw & 0xfffff) + 1)
                            if tb and tb.get("records"):
                                stride = schemas.get(tb["type"], {}).get("stride") or (tb["size"] // len(tb["records"]))
                                idx = (pw >> 20) // stride
                                for k in range(cnt):
                                    if 0 <= idx + k < len(tb["records"]):
                                        tr = tb["records"][idx + k]
                                        tv = tr.get("0x063fa3f2") if isinstance(tr, dict) else None
                                        if isinstance(tv, dict) and len(tv.get("raw_hex", "")) == 16:
                                            phash = bytes.fromhex(tv["raw_hex"])[::-1].hex()
                                            if phash in ttpf_hashes:
                                                redirects[vhash] = [phash, p.name]
                                                break
    TTDB_MANIFEST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    data = {"redirects": redirects, "colors": colors}
    TTDB_MANIFEST_CACHE.write_text(json.dumps(data), "utf-8")
    return data


def load_native_texture_hashes():
    """Return {hash16: area_label} for GPU-backed native RPF dictionary textures."""
    out = {}
    for gz in sorted(INDEX.rglob("studio-native-textures-v4.json.gz")):
        try:
            with gzip.open(gz, "rt", encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            continue
        pkg = doc.get("package", gz.parent.name)
        area = "native:" + pkg.split("/")[-1].replace(".rpf", "")
        for rec in doc.get("records", ()):
            h = str(rec.get("nameHash") or "").lower().removeprefix("0x")
            if len(h) == 16 and rec.get("gpuStreams"):
                out.setdefault(h, area)
    return out


def main():
    vehicles = vehicle_names()
    src = sqlite3.connect(SRC)
    src.row_factory = sqlite3.Row
    rows = [dict(r) for r in src.execute("select * from model")]
    vfamily, vevidence = vehicle_classes(vehicles)
    ordered = sorted(vehicles, key=len, reverse=True)
    areas = texture_areas()
    ttdb_manifest = load_ttdb_manifest(set(areas))
    redirects = ttdb_manifest["redirects"]
    native_tex = load_native_texture_hashes()

    def hash_areas(h):
        if h in areas:
            return areas[h]
        if h in redirects and redirects[h][0] in areas:
            return areas[redirects[h][0]]
        if h in native_tex:
            return {native_tex[h]}
        return ()

    def is_resolved(h):
        return h in areas or (h in redirects and redirects[h][0] in areas) or h in native_tex

    # World placements come from the map archives, which Lineup does not read.
    placements = Counter()
    arch = {}

    # Creature fragment stems, animal donors, and ped/clothing/head/gore donors.
    frag_roots_set = set()
    animal_donors = {}
    ped_donors = {}
    head_norm_donors = {}
    tdb = sqlite3.connect(LIBRARY / "textures.sqlite3")
    tex_dims = {
        r[0]: (r[1] * r[2], max(r[1], r[2]), r[3] or "")
        for r in tdb.execute("select hash, width, height, format from texture")
    }
    bc5_256 = {h for h, (_, mx, fmt) in tex_dims.items() if "BC5" in fmt and mx >= 256}
    tdb.close()

    for r in rows:
        n = r["name"].lower()
        base, _ = split_variant(n)
        mapped = ANIMAL_ALIASES.get(base, base)
        if r["kind"] == "fragment" and re.match(r"^cr[abcfimr]_", mapped):
            frag_roots_set.add(mapped)
        mats = json.loads(r["materials"] or "[]")
        if re.match(r"^(?:cr[abcfimr]_|a_c_)", n):
            primary_keys, _ = animal_part_keys(n)
            if primary_keys:
                rank = (1 if any(w in base for w in ("_corpse", "_law", "_sheriff", "_agh", "_mutated", "_css")) else 0,
                        len(base), base)
                for m in mats:
                    for t in m["textures"]:
                        th, param = t["textureHash"], t["parameter"]
                        eff = redirects[th][0] if th not in areas and th in redirects else th
                        if th != SENTINEL_TEX and eff in areas:
                            for pk in primary_keys:
                                cur = animal_donors.get((*pk, param))
                                if cur is None or rank < cur[0]:
                                    animal_donors[(*pk, param)] = (rank, eff, r["name"])
        # Collect ped/clothing/head/gore donors from shipped .ttpf bindings (highest resolution first).
        if not BAD_PED_DONOR_RE.search(n):
            for m in mats:
                if m["shaderHash"] == "379efcef":
                    for t in m["textures"]:
                        th = t["textureHash"]
                        eff = redirects[th][0] if th not in areas and th in redirects else th
                        if t["parameter"] == "1e77bf4c" and eff in bc5_256:
                            px = tex_dims.get(eff, (0, 0, ""))[0]
                            hrank = (-px, 0 if n.startswith("hc_") or n.startswith("head_") else 1, len(base), base)
                            for hk in head_eth_keys(n):
                                cur = head_norm_donors.get(hk)
                                if cur is None or hrank < cur[0]:
                                    head_norm_donors[hk] = (hrank, eff, r["name"])
        pkeys, _ = ped_garment_keys(n)
        if pkeys and not re.search(r"(?:^|_)head_(?:fr|mr|fb|mb|fo|mo|fc|mc)\d", base) and not BAD_PED_DONOR_RE.search(n):
            for m in mats:
                for t in m["textures"]:
                    th, param = t["textureHash"], t["parameter"]
                    eff = redirects[th][0] if th not in areas and th in redirects else th
                    if th != SENTINEL_TEX and eff in areas:
                        px, mx, _ = tex_dims.get(eff, (0, 0, ""))
                        if param in ("c8e8c282", "1e77bf4c") and mx < 64:
                            continue
                        prank = (1 if "_cull" in n or "_slod" in n or "gore" in n else 0, -px, len(base), base)
                        for pk in pkeys:
                            cur = ped_donors.get((*pk, param))
                            if cur is None or prank < cur[0]:
                                ped_donors[(*pk, param)] = (prank, eff, r["name"])
    # Seed clean body-skin presets (hand_*, feet_*) and high-res 1024x1024 head normals from the game's
    # appearance catalogues so sibling meshes always inherit highest-res clean human textures.
    from runtime_faces import _load as _load_face_presets
    for pname, ptex in _load_face_presets().items():
        if BAD_PED_DONOR_RE.search(pname):
            continue
        if pname.startswith(("hand_", "feet_")):
            pkeys, _ = ped_garment_keys(pname)
            if not pkeys:
                continue
            is_adult_reg = bool(re.search(r"_(?:fr1|mr1)_", pname))
            for param, fh in ptex.items():
                if fh in areas:
                    px = tex_dims.get(fh, (0, 0, ""))[0]
                    prank = (-2 if is_adult_reg else -1, -px, len(pname), pname)
                    for pk in pkeys:
                        cur = ped_donors.get((*pk, param))
                        if cur is None or prank < cur[0]:
                            ped_donors[(*pk, param)] = (prank, fh, f"{pname} (skin preset)")
        elif re.search(r"(?:^|_)head_(?:fr|mr|fb|mb|fo|mo|fc|mc)\d", pname):
            nfh = ptex.get("1e77bf4c")
            if nfh and nfh in bc5_256:
                px = tex_dims.get(nfh, (0, 0, ""))[0]
                hrank = (-px, 0 if pname.startswith(("player_", "head_", "hc_")) else 1, len(pname), pname)
                for hk in head_eth_keys(pname):
                    cur = head_norm_donors.get(hk)
                    if cur is None or hrank < cur[0]:
                        head_norm_donors[hk] = (hrank, nfh, pname)
    frag_roots = sorted(frag_roots_set, key=len, reverse=True)

    # First pass: classify and identify which animal & character families have real non-proxy mesh geometry.
    staged = []
    real_animal_families = set()
    real_ped_families = set()
    for r in rows:
        mats = json.loads(r["materials"] or "[]")
        hashes = sorted({t["textureHash"] for m in mats for t in m["textures"] if t["textureHash"] != SENTINEL_TEX})
        hist = Counter(a for h in hashes for a in hash_areas(h))
        found = sum(1 for h in hashes if is_resolved(h))
        cat, sub = classify(r, vehicles, vfamily, hist)
        n = r["name"].lower()
        fam = (animal_family(n, frag_roots) if cat == "Animals" else
               ped_family(n, cat) if cat in ("Protagonists", "Characters") else "")
        if cat == "Animals" and fam and not _is_animal_ragdoll_proxy(r, found) and not _is_animal_groom_proxy(r, found):
            real_animal_families.add(fam)
        if cat in ("Protagonists", "Characters") and fam and not _is_ped_proxy(r, found):
            real_ped_families.add(fam)
        staged.append((r, mats, hashes, hist, found, cat, sub, fam))

    out = []
    for r, mats, hashes, hist, found, cat, sub, fam in staged:
        base, variant = split_variant(r["name"].lower())
        n = r["name"].lower()
        family = None
        low_detail = bool(LOW_DETAIL.match(variant))
        tex_refs = len(hashes)
        if cat == "Animals":
            ragdoll = _is_animal_ragdoll_proxy(r, found)
            groom = _is_animal_groom_proxy(r, found)
            if (ragdoll or groom) and (not fam or fam not in real_animal_families):
                cat, sub = "Physics & bounds proxies", "Character, animal & cloth bounds"
                family = ""
            else:
                family = fam or None
                if ragdoll or groom:
                    low_detail = True
                if hashes and found < len(hashes):
                    _, lookup_keys = animal_part_keys(n)
                    if lookup_keys:
                        resolved = {h for h in hashes if is_resolved(h)}
                        for m in mats:
                            for t in m["textures"]:
                                th, param = t["textureHash"], t["parameter"]
                                if th != SENTINEL_TEX and th not in resolved:
                                    donor = next((animal_donors[(*lk, param)] for lk in lookup_keys
                                                  if (*lk, param) in animal_donors), None)
                                    if donor:
                                        resolved.add(th)
                                        for a_name in areas.get(donor[1], ()):
                                            hist[a_name] += 1
                        found = len(resolved)
        elif cat in ("Protagonists", "Characters", "Clothing & accessories", "Effects"):
            if cat in ("Protagonists", "Characters", "Clothing & accessories") and _is_ped_proxy(r, found):
                if fam and fam in real_ped_families:
                    family = fam
                    low_detail = True
                else:
                    cat, sub = "Physics & bounds proxies", "Character, animal & cloth bounds"
                    family = ""
            else:
                if cat in ("Protagonists", "Characters"):
                    family = fam or None
                _, lookup_keys = ped_garment_keys(n)
                resolved = {h for h in hashes if is_resolved(h)}
                is_head_mesh = bool(re.search(r"(?:^|_)head_(?:fr|mr|fb|mb|fo|mo|fc|mc|mi|fi|mk|fk)\d", base)) and "gore" not in base
                if hashes and found < len(hashes) and lookup_keys and not is_head_mesh:
                    for m in mats:
                        for t in m["textures"]:
                            th, param = t["textureHash"], t["parameter"]
                            if th != SENTINEL_TEX and th not in resolved:
                                donor = next((ped_donors[(*lk, param)] for lk in lookup_keys
                                              if (*lk, param) in ped_donors), None)
                                if donor:
                                    resolved.add(th)
                                    for a_name in areas.get(donor[1], ()):
                                        hist[a_name] += 1
                    found = len(resolved)
                # Real face, body-skin, eye, teeth, and clothing textures from the game's appearance catalogues.
                if cat in ("Protagonists", "Characters", "Clothing & accessories") and "gore" not in base:
                    from runtime_faces import faces_for
                    face = faces_for(r["name"])
                    if face:
                        has_head_mat = any(m["shaderHash"] == "379efcef" for m in mats)
                        present_params = set()
                        for m in mats:
                            is_muv = m["shaderHash"] in ("d5ed50f4", "39ddb9ca", "7574b09b")
                            has_c8 = any(t["parameter"] == "c8e8c282" for t in m["textures"])
                            for t in m["textures"]:
                                th, param = t["textureHash"], t["parameter"]
                                if th == SENTINEL_TEX:
                                    continue
                                present_params.add(param)
                                fh = face.get(param)
                                if not fh and is_muv and not has_c8 and param == "c6f46e79":
                                    cand = face.get("c8e8c282")
                                    if cand and "SRGB" not in tex_dims.get(cand, (0, 0, ""))[2]:
                                        fh = cand
                                if fh and fh in areas and th not in resolved:
                                    resolved.add(th)
                                    for a_name in areas.get(fh, ()):
                                        hist[a_name] += 1
                        found = len(resolved)
                        if has_head_mat:
                            added_face = 0
                            for param in ("c8e8c282", "1e77bf4c"):
                                fh = face.get(param)
                                if fh and fh in areas and param not in present_params:
                                    added_face += 1
                                    for a_name in areas.get(fh, ()):
                                        hist[a_name] += 1
                            if added_face:
                                tex_refs += added_face
                                found += added_face
                # Heads (379efcef) and MUV garments with runtime-only slots derive normal/detail textures.
                if found == 0:
                    has_head = any(m["shaderHash"] == "379efcef" for m in mats)
                    has_muv = any(m["shaderHash"] in ("d5ed50f4", "39ddb9ca", "7574b09b") for m in mats)
                    if has_head and any(hk in head_norm_donors for hk in head_eth_keys(n)):
                        donor = next(head_norm_donors[hk] for hk in head_eth_keys(n) if hk in head_norm_donors)
                        tex_refs = max(tex_refs, 1)
                        found = max(found, 1)
                        for a_name in areas.get(donor[1], ()):
                            hist[a_name] += 1
                    elif has_muv and lookup_keys:
                        added = 0
                        for param in ("c6f46e79", "1e77bf4c"):
                            donor = next((ped_donors[(*lk, param)] for lk in lookup_keys
                                          if (*lk, param) in ped_donors), None)
                            if donor:
                                added += 1
                                for a_name in areas.get(donor[1], ()):
                                    hist[a_name] += 1
                        if added:
                            tex_refs = max(tex_refs, added)
                            found = max(found, added)
        elif cat == "Vehicles":
            family = next((v for v in ordered if n == v or n.startswith(v + "_")), None)
        elif cat == "Weapons" and WEAPON_FAMILY.match(n):
            family = WEAPON_FAMILY.match(n)[1]
        a = arch.get(r["hash"])
        out.append((r["id"], r["name"], r["hash"], r["archive"], r["member"], r["root"], r["kind"], r["source"],
                    r["status"], r["error"], r["box"], r["lods"], r["models"], r["geometries"], r["materials"],
                    cat, sub, family or "", base, variant, int(low_detail),
                    tex_refs, found, json.dumps(dict(hist)), placements.get(r["hash"], 0),
                    a[0] if a else None, a[1] if a else None, json.dumps(a[2]) if a else None,
                    r.get("ibytes"), r.get("vbytes")))
    tmp = OUT.with_suffix(".building.sqlite3")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("""
        create table model(id integer primary key, name text, hash integer, archive text, member integer, root integer,
            kind text, source text, status text, error text, box text, lods text, models integer, geometries integer,
            materials text, category text, subcategory text, family text, base_name text, variant text,
            low_detail integer, texture_refs integer, textures_found integer, texture_areas text, placements integer,
            archetype_type integer, asset_type integer, archetype_box text, ibytes integer, vbytes integer,
            group_key text, group_size integer, is_rep integer);
    """)
    # One browsable item per group: a vehicle/weapon/animal/head with its parts, or a model with its detail variants.
    groups = defaultdict(list)
    for row in out:
        cat, family, base = row[15], row[17], row[18]
        groups[f"{cat}|{family}" if family else f"{cat}|{base}"].append(row)
    final = []
    for key, members in groups.items():
        def score(row):
            n, fam, base_n = row[1].lower(), row[17], row[18]
            if row[15] == "Animals" and fam:
                is_body = (n in (fam, fam + "_hi", fam + "_body_00", fam + "_body", fam + "_body_00_hi", fam + "_00")
                           or bool(re.match(rf"^{re.escape(fam)}_body_0\d+$", n)))
                return (row[8] == "indexed", not row[20], is_body, row[22] > 0, row[28] or 0, -row[0])
            is_root = bool(fam) and (n == fam or n == fam + "_hi" or base_n == fam)
            return (row[8] == "indexed", is_root, not row[20], row[22] > 0, row[28] or 0, -row[0])
        rep = max(members, key=score)[0]
        final.extend((*row, key, len(members), int(row[0] == rep)) for row in members)

    # Faithfully ingest all renderable human ped archetypes from packs.rpf/00011d1ff000.pso
    # (ped_archetypes.sqlite3) so every story & world ped archetype appears in Characters.
    pa_db_path = LIBRARY / "ped_archetypes.sqlite3"
    if pa_db_path.is_file():
        by_name_lower = {}
        for row in final:
            if row[8] != "indexed":
                continue
            nl = row[1].lower()
            cur = by_name_lower.get(nl)
            rank = (row[32], 0 if not row[20] else 1, row[28] or 0, -row[0])
            if cur is None or rank > cur[0]:
                by_name_lower[nl] = (rank, row)

        with closing(sqlite3.connect(pa_db_path.as_uri() + "?mode=ro", uri=True)) as padb:
            padb.row_factory = sqlite3.Row
            pa_rows = padb.execute("select * from ped_archetype order by idx").fetchall()

        story_covered_heads = set()
        pa_inserts = []
        for pa in pa_rows:
            if pa["category"] != "Characters":
                continue
            pa_idx = int(pa["idx"])
            pa_name = pa["name"]
            pa_hash = int(pa["arch_hash"])
            pa_hex = pa["arch_hex"]
            head_nm = (pa["head_drawable"] or "").strip()
            body_list = json.loads(pa["body_drawables"] or "[]")
            cloth_list = json.loads(pa["clothing_drawables"] or "[]")
            target_nm = head_nm
            if not target_nm:
                # Fallback for full-body legacy peds (e.g. legacypedcs / legacypedworld) without separate head mesh.
                for cand in body_list + cloth_list:
                    cl = cand.lower()
                    if "legacyped" in cl or "fullbody" in cl:
                        if cl in by_name_lower:
                            target_nm = cand
                            break
            if not target_nm:
                continue
            hit = by_name_lower.get(target_nm.lower())
            if not hit:
                continue
            src_row = hit[1]

            if pa["subcategory"] == "Story characters":
                subcat = "Story characters"
                if head_nm and "_head_" in head_nm.lower():
                    story_covered_heads.add(head_nm.lower())
            elif pa_idx < 614:
                subcat = "Ped archetypes — Ambient (a_*)"
            elif 1380 <= pa_idx <= 1900:
                subcat = "Ped archetypes — Encounters & gang"
            else:
                subcat = "Ped archetypes — Mission, service & unique"

            # Texture coverage reflects bespoke 0DRP face albedo/normal when resolved locally.
            t_refs = src_row[21]
            t_found = src_row[22]
            t_areas = json.loads(src_row[23] or "{}")
            extra_found = 0
            for fh in (pa["face_albedo"], pa["face_normal"]):
                if fh and fh in areas:
                    extra_found += 1
                    for a_name in areas.get(fh, ()):
                        t_areas[a_name] = t_areas.get(a_name, 0) + 1
            if extra_found:
                t_refs = max(t_refs, extra_found)
                t_found = max(t_found, extra_found)

            pa_inserts.append((
                1_000_000 + pa_idx,
                pa_name,
                pa_hash,
                src_row[3],          # archive
                src_row[4],          # member
                src_row[5],          # root
                src_row[6],          # kind
                "ped_archetype",     # source
                src_row[8],          # status
                src_row[9],          # error
                src_row[10],         # box
                src_row[11],         # lods
                src_row[12],         # models
                src_row[13],         # geometries
                src_row[14],         # materials
                "Characters",        # category
                subcat,              # subcategory
                pa_name,             # family
                src_row[1],          # base_name = exact underlying RPF drawable name for decoding
                "",                  # variant
                0,                   # low_detail
                t_refs,
                t_found,
                json.dumps(t_areas),
                src_row[24],         # placements
                src_row[25],         # archetype_type
                src_row[26],         # asset_type
                src_row[27],         # archetype_box
                src_row[28],         # ibytes
                src_row[29],         # vbytes
                f"ped_arch|{pa_hex}",
                1,                   # group_size
                1,                   # is_rep
            ))

        # Avoid duplicating raw cs_*_head_* drawables inside Story characters when their exact cs_*
        # ped archetype is now represented there (raw head drawables remain browsable in Heads & faces).
        if story_covered_heads:
            covered_gkeys = {
                row[30] for row in final
                if row[15] == "Characters" and row[16] == "Story characters" and row[1].lower() in story_covered_heads
            }
            if covered_gkeys:
                final = [
                    (row[:16] + ("Heads & faces",) + row[17:]) if (row[30] in covered_gkeys and row[16] == "Story characters") else row
                    for row in final
                ]
        final.extend(pa_inserts)

    out = final
    db.executemany(f"insert into model values ({','.join('?' * 33)})", out)
    db.executescript("""
        create index model_cat on model(category, subcategory, base_name);
        create index model_name on model(name);
        create index model_hash on model(hash);
        create index model_base on model(base_name);
        create index model_group on model(group_key);
        create index model_rep on model(is_rep, category, subcategory);
    """)
    summary = {f"{c} / {s}": n for c, s, n in db.execute(
        "select category, subcategory, count(*) from model group by 1,2 order by 1,2")}
    db.commit()
    db.close()
    tmp.replace(OUT)
    (LIBRARY / "inventory_summary.json").write_text(json.dumps(dict(
        models=len(out), vehicles=len(vehicles), vehicleKinds=Counter(vfamily.values()),
        vehicleClassEvidence=Counter(v.split(":")[0] for v in vevidence.values()), categories=summary), indent=2))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
