# SPDX-License-Identifier: GPL-3.0-or-later
"""RAGE 9 Character Effects, Dynamic Physiology, Tattoos, Blood Decals & Dismemberment Service.

Loads live configuration directly from the local GTA VI build files:
- common.rpf/xml/000000355000.xml, 000000361000.xml, 00000031f000.xml, 000000372000.xml (Drug/Status Timecycle FX)
- common.rpf/xml/000000226000.xml (CPedVariationBodyTypeManager: 16 body shapes, 27 morph channels, 11 heights)
- common.rpf/xml/0000000ab000.xml (CGraphicsDefinitionsData: Blood, Mud, Swamp, Beach, Dust, Urban dirt definitions)
- common.rpf/xml/000000046000.xml (sPedDamageModifiers: Heart, Throat, Brain, Spine vital organ hitboxes)
- data.rpf/00001ae00000.pso       (7,509 tattoos with 3x4 UV affine placement matrices -> decorations_0..3_3.ttpf)
- data.rpf/00001b1a4000.pso       (34 ped blood/wound decal sets -> ttdb_vfx.ttpf)
- data.rpf/00001b058000.pso       (Master VFX projected decals -> ttdb_vfx.ttpf)
"""
from __future__ import annotations

import math
import re
import sqlite3
import struct
import threading
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path
from paths import INDEX, LIBRARY

COMMON_XML = INDEX / "common.rpf" / "xml"
DATA_PSO = INDEX / "data.rpf"
TEXTURES_DB = LIBRARY / "textures.sqlite3"
ANIM_DB = LIBRARY / "anim_clips.sqlite3"

_LOCK = threading.RLock()
_CATALOG_CACHE: dict | None = None
_TATTOO_CACHE: list[dict] | None = None
_BLOOD_CACHE: list[dict] | None = None
_QUICK_ANIMS_CACHE: dict[str, list[dict]] = {}


def _ro_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _parse_float_list(text: str | None) -> list[float]:
    if not text:
        return []
    out = []
    for tok in text.replace("\n", " ").replace("\t", " ").split():
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------------------
# 1. Drug & Status Timecycle Modifiers (Mandy_*, Cocaine, Molly, Weed, Drunk, Trips, Combat)
# ---------------------------------------------------------------------------
_DRUG_PRESET_SPECS = [
    # 7-Stage Mandy (MDMA) progression from TC_mods_Mission.xml / TC_mods_postfx.xml
    ("Mandy_FirstTingle", "Mandy (MDMA) — 1. First Tingle", "Mandy (MDMA)", "000000355000.xml",
     "Beat-synced subtle shadow contrast & warm bloom lift on initial come-up.", "pills"),
    ("Mandy_FeelingIt", "Mandy (MDMA) — 2. Feeling It", "Mandy (MDMA)", "000000355000.xml",
     "Beat-synced radial/cubic lens warp & pulsing shadow contrast.", "dance"),
    ("Mandy_ComingUp", "Mandy (MDMA) — 3. Coming Up", "Mandy (MDMA)", "000000355000.xml",
     "Stronger cubic warp + neon blue/magenta chromatic aberration split on beat.", "dance"),
    ("Mandy_Spangled", "Mandy (MDMA) — 4. Spangled (Peak)", "Mandy (MDMA)", "000000355000.xml",
     "Peak euphoria: heavy bloom, neon blue/magenta/yellow CA, BRDF microfacet sparkle glints, motion blur & vignette pulse.", "dance"),
    ("Mandy_ComingDown", "Mandy (MDMA) — 5. Coming Down", "Mandy (MDMA)", "000000355000.xml",
     "Glints fade, highlight contrast drops, desaturation & vignette creep in on beat.", "drugged"),
    ("Mandy_LoosingTheFeeling", "Mandy (MDMA) — 6. Losing the Feeling", "Mandy (MDMA)", "000000355000.xml",
     "Flat contrast, harsh detail contrast (1.5), sickly amber chromatic split & heavy desaturation.", "drugged"),
    ("Mandy_ComeDown", "Mandy (MDMA) — 7. Come Down", "Mandy (MDMA)", "000000355000.xml",
     "Switches from music beat to heartbeat axis: heavy pulsing dark vignette (0.6) & amber CA.", "drugged"),
    # Molly (club ped rim lighting variant)
    ("Mission_MoneyEscort_Molly", "Molly — Extreme Ped Rim Light", "Club & Stimulants", "00000031f000.xml",
     "ped_light0_mult=100, ped_light_fresnel=1.0 extreme character rim highlight + lens warp (-0.25).", "dance"),
    # Cocaine
    ("POSTFX_DRUG_coke_chromaticGOOD", "Cocaine (Good) — Violet CA & Glare", "Club & Stimulants", "000000361000.xml",
     "Violet/blue chromatic split, negative cubic warp (-0.071), lens flares & boosted speculars.", "cocaine"),
    ("POSTFX_DRUG_coke_chromaticBAD", "Cocaine (Bad) — Toxic Green CA", "Club & Stimulants", "000000361000.xml",
     "Toxic yellow-green chromatic split (0.64, 1.0, 0.0), harsh warp & anxiety flicker.", "cocaine"),
    ("POSTFX_DRUG_coke_UV", "Cocaine — UV Party Mode", "Club & Stimulants", "000000361000.xml",
     "Blacklight indigo UV party lighting (uv_party_mode) with beat-synced fluorescence.", "dance"),
    ("Player_DrugsCocaineBump", "Cocaine Bump (Instant Surge)", "Club & Stimulants", "00000031f000.xml",
     "High-exposure (+2.0), high-saturation (2.0), bloom (1.0) & lens warp (-0.25) surge.", "cocaine"),
    # Weed & Alcohol
    ("Consume_Weed", "Weed — Warm Haze & Bloom", "Intoxicants", "00000031f000.xml",
     "Warm bloom (1.0), exposure lift (+1.0), gentle desat (0.9) & fish-eye warp (-0.25).", "weed"),
    ("Player_Drunk_Distort", "Drunk — Heavy Lens Sway", "Intoxicants", "00000031f000.xml",
     "Heavy drunken lens distortion (-0.75) paired with heartbeat light sway.", "drunk"),
    # Psychedelic / Mission Trips
    ("MKU_drugtrip", "MKU Drug Trip — Breath & Trails", "Psychedelic Trips", "000000361000.xml",
     "Breath/heartbeat-synced screen trails, vignette bloom, color-shift & light flicker.", "drugged"),
    ("MKU_drugtripRedroom", "MKU Drug Trip — Red Room", "Psychedelic Trips", "000000361000.xml",
     "Deep crimson vignette (0.36, 0, 0), red wash & breathing distortion.", "drugged"),
    ("LOGtrip", "LOG Trip — Orange CA & Grain", "Psychedelic Trips", "000000361000.xml",
     "Oscillating orange chromatic split (1.0, 0.5, 0.0), film grain (1.0) & heartbeat warp.", "drugged"),
    ("hellTrip", "Hell Trip — Red Shadow Grade", "Psychedelic Trips", "000000361000.xml",
     "Extreme red shadow gamma (-2.71), heavy warp (-1.0), grain & surreal saturation.", "drugged"),
    # Combat / Adrenaline / Status
    ("Player_Adrenaline", "Adrenaline — Focus Grain", "Combat & Status", "00000031f000.xml",
     "Fine crisp film grain (noise=0.5, size=0.1) for Adrenaline stamina/focus buff.", "adrenaline"),
    ("Player_HealthAdrenalinShot", "Adrenaline Shot — Surge", "Combat & Status", "00000031f000.xml",
     "High-contrast saturation surge (2.0) + radial tunnel warp (-0.5).", "adrenaline"),
    ("Weapon_Taser", "Taser / Electrocuted", "Combat & Status", "00000031f000.xml",
     "Electric stun warp (-0.1) + high-frequency flicker paired with electrocuted anim.", "taser"),
    ("Mission_BilHouseAtt_Concussion", "Concussion — Dazed & Blurred", "Combat & Status", "00000031f000.xml",
     "Heavy warp (-1.0), extreme detail contrast (3.0), bloom (0.75), exposure (+1.0) & wash.", "hit_react"),
    ("Player_HealthLowCritical", "Critical Health — Blood-Loss Desat", "Combat & Status", "00000031f000.xml",
     "Desaturated vision (0.5), harsh contrast (1.25) & heartbeat vignette.", "bleedout"),
    ("Player_Rampage", "Rampage — High-Contrast Ped Glow", "Combat & Status", "00000031f000.xml",
     "ped_light0_mult=4.0, exposure (+0.5), grain (1.0) & detail contrast (2.0).", "combat"),
]


def _extract_modifier_curves(xml_path: Path, wanted: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not xml_path.is_file():
        return out
    try:
        tree = ET.parse(xml_path)
    except Exception:
        return out
    root = tree.getroot()
    for mod in root.findall(".//modifier"):
        name = mod.attrib.get("name", "")
        if name not in wanted:
            continue
        axis_el = mod.find("axis")
        axis_ctrl = axis_el.attrib.get("controlValue", "static") if axis_el is not None else "static"
        times = [0.0]
        params: dict[str, list[float]] = {}
        containers = list(mod.findall("axis")) if axis_el is not None else [mod]
        # Prefer beat/heartbeat/breathRatio axis when multi-axis
        chosen = containers[0]
        for c in containers:
            cv = c.attrib.get("controlValue", "")
            if cv in ("beat", "heartbeat", "breathRatio", "musicPeakAmplitude"):
                chosen = c
                axis_ctrl = cv
                break
        for child in chosen:
            if child.tag == "keyframe_t_value":
                tv = _parse_float_list(child.text)
                if tv:
                    times = tv
                continue
            if child.tag in ("axis", "submodifiers"):
                continue
            vals = _parse_float_list(child.text)
            if vals:
                params[child.tag] = vals
        # Also pick up any top-level params directly under <modifier>
        if chosen is not mod:
            for child in mod:
                if child.tag in ("axis", "submodifiers", "keyframe_t_value"):
                    continue
                vals = _parse_float_list(child.text)
                if vals and child.tag not in params:
                    params[child.tag] = vals
        out[name] = {
            "axis": axis_ctrl,
            "times": times,
            "params": params,
        }
    return out


def _load_drug_presets() -> list[dict]:
    by_file: dict[str, set[str]] = {}
    for mod_name, _lbl, _grp, fn, _desc, _anim in _DRUG_PRESET_SPECS:
        by_file.setdefault(fn, set()).add(mod_name)
    parsed: dict[str, dict] = {}
    for fn, names in by_file.items():
        parsed.update(_extract_modifier_curves(COMMON_XML / fn, names))
    # Also fetch companion Coke sub-curves so Coke presets feel full-strength
    coke_extra = _extract_modifier_curves(COMMON_XML / "000000361000.xml", {
        "POSTFX_DRUG_coke_motionBlur", "POSTFX_DRUG_coke_gradingGOOD",
        "POSTFX_DRUG_coke_gradingBAD", "POSTFX_DRUG_vignette_good",
    })
    drunk_extra = _extract_modifier_curves(COMMON_XML / "000000372000.xml", {"Player_Drunk_Light_animations"})

    presets = []
    for mod_name, label, group, fn, desc, anim_key in _DRUG_PRESET_SPECS:
        raw = parsed.get(mod_name, {"axis": "static", "times": [0.0], "params": {}})
        params = dict(raw["params"])
        axis = raw["axis"]
        times = list(raw["times"])
        # Enrich compound modifiers with verified XML submodifier params
        if mod_name == "POSTFX_DRUG_coke_chromaticGOOD":
            params.setdefault("postfx_screen_trails_intensity", [0.25])
            params.setdefault("postfx_vignette_bloom_intensity", [0.65])
            params.setdefault("postfx_exposure_tweak", [0.85])
            if "POSTFX_DRUG_coke_motionBlur" in coke_extra:
                params.setdefault("postfx_motionblurlength", [0.5])
        elif mod_name == "POSTFX_DRUG_coke_chromaticBAD":
            params.setdefault("postfx_screen_trails_intensity", [0.30])
            params.setdefault("postfx_desaturation", [0.78])
            params.setdefault("postfx_noise", [0.35])
        elif mod_name == "POSTFX_DRUG_coke_UV":
            params.setdefault("uv_party_mode", [0.85])
            params.setdefault("postfx_intensity_bloom", [0.55])
            params.setdefault("postfx_vignette_intensity", [0.25])
        elif mod_name == "Player_Drunk_Distort" and "Player_Drunk_Light_animations" in drunk_extra:
            axis = "heartbeat"
            times = [0.0, 0.35, 0.70, 1.0]
            params.setdefault("light_intensity_mult", [0.85, 1.25, 1.15, 0.85])
            params.setdefault("postfx_intensity_bloom", [0.35, 0.55, 0.45, 0.35])
        elif mod_name == "MKU_drugtrip":
            params.setdefault("postfx_screen_trails_intensity", [0.45])
            params.setdefault("postfx_vignette_bloom_intensity", [0.8])
            params.setdefault("lens_dist_coeff", [-0.15])
            if axis == "static":
                axis, times = "breathRatio", [0.0, 0.5, 1.0]
        elif mod_name == "MKU_drugtripRedroom":
            params.setdefault("postfx_vignette_intensity", [0.65])
            params.setdefault("postfx_vignette_col_r", [0.45])
            params.setdefault("postfx_vignette_col_g", [0.02])
            params.setdefault("postfx_vignette_col_b", [0.02])
        elif mod_name == "LOGtrip":
            params.setdefault("chrom_aberration_coeff", [0.12])
            params.setdefault("chrom_aberration_split_col_r", [1.0])
            params.setdefault("chrom_aberration_split_col_g", [0.498])
            params.setdefault("chrom_aberration_split_col_b", [0.0])
            params.setdefault("postfx_noise", [0.65])
            params.setdefault("lens_dist_cube_coeff", [0.08])
            if axis == "static":
                axis, times = "breathRatio", [0.0, 0.5, 1.0]
        elif mod_name == "hellTrip":
            params.setdefault("postfx_color_grading_shadows_gamma_r", [-0.55])
            params.setdefault("postfx_vignette_intensity", [0.55])
            params.setdefault("postfx_vignette_col_r", [0.55])
            params.setdefault("postfx_vignette_col_g", [0.04])
            params.setdefault("postfx_vignette_col_b", [0.0])
            params.setdefault("lens_dist_cube_coeff", [-0.18])
            params.setdefault("postfx_noise", [0.35])
        elif mod_name == "Weapon_Taser":
            axis = "taser"
            times = [0.0, 0.5, 1.0]
            params.setdefault("chrom_aberration_coeff", [0.08, -0.08, 0.08])
            params.setdefault("chrom_aberration_split_col_r", [0.25, 0.45, 0.25])
            params.setdefault("chrom_aberration_split_col_g", [0.75, 0.92, 0.75])
            params.setdefault("chrom_aberration_split_col_b", [1.0, 1.0, 1.0])
            params.setdefault("postfx_noise", [0.45, 0.85, 0.45])

        presets.append({
            "id": mod_name,
            "label": label,
            "group": group,
            "source": f"common.rpf/xml/{fn}",
            "description": desc,
            "axis": axis,
            "times": times,
            "params": params,
            "animKey": anim_key,
            "isMandy": mod_name.startswith("Mandy_"),
        })
    return presets


# ---------------------------------------------------------------------------
# 2. Body Morph Presets (000000226000.xml)
# ---------------------------------------------------------------------------
def _load_body_shapes() -> dict:
    path = COMMON_XML / "000000226000.xml"
    heights = [1.52, 1.57, 1.62, 1.67, 1.72, 1.77, 1.82, 1.87, 1.92, 1.97, 2.02]
    presets = []
    if path.is_file():
        try:
            root = ET.parse(path).getroot()
            h_el = root.find(".//HeightPresets/Item[@key='HUMAN_HEIGHT_PRESETS']/HeightsInMeters")
            if h_el is not None:
                hv = _parse_float_list(h_el.text)
                if hv:
                    heights = hv
            for item in root.findall("./BodyShapePresets/Item"):
                key = item.attrib.get("key", "")
                if not key:
                    continue
                exprs: dict[str, float] = {}
                for ex in item.findall("./Expressions/Item"):
                    eid = (ex.findtext("Id") or "").strip()
                    vals = _parse_float_list(ex.findtext("Values"))
                    if eid and vals:
                        # Use median/mean when XML gives a random pool (e.g. [0.5, 0.75, 1.0])
                        exprs[eid] = round(sum(vals) / len(vals), 3)
                attrs = item.find("Attributes")
                stamina = [
                    float(attrs.find("StaminaScoreMin").attrib["value"]) if attrs is not None and attrs.find("StaminaScoreMin") is not None else 0.5,
                    float(attrs.find("StaminaScoreMax").attrib["value"]) if attrs is not None and attrs.find("StaminaScoreMax") is not None else 0.5,
                ]
                speed = [
                    float(attrs.find("SpeedScoreMin").attrib["value"]) if attrs is not None and attrs.find("SpeedScoreMin") is not None else 0.5,
                    float(attrs.find("SpeedScoreMax").attrib["value"]) if attrs is not None and attrs.find("SpeedScoreMax") is not None else 0.5,
                ]
                nice = key.replace("PED_BODYSHAPE_", "").replace("_", " ").title()
                presets.append({
                    "id": key,
                    "label": nice,
                    "expressions": exprs,
                    "stamina": stamina,
                    "speed": speed,
                })
        except Exception:
            pass
    return {
        "heights": heights,
        "maleDefaultHeight": 1.82,
        "femaleDefaultHeight": 1.72,
        "presets": presets,
    }


# ---------------------------------------------------------------------------
# 3. Dirt & Blood-Soak Definitions (0000000ab000.xml)
# ---------------------------------------------------------------------------
def _load_dirt_definitions() -> list[dict]:
    path = COMMON_XML / "0000000ab000.xml"
    out = []
    if not path.is_file():
        return out
    try:
        root = ET.parse(path).getroot()
        colors: dict[str, list[float]] = {}
        for c in root.findall("./CannedColors/Item"):
            nm = (c.findtext("Name") or "").strip()
            v = c.find("Color")
            if nm and v is not None:
                colors[nm] = [
                    round(float(v.attrib.get("x", "128")) / 255.0, 4),
                    round(float(v.attrib.get("y", "128")) / 255.0, 4),
                    round(float(v.attrib.get("z", "128")) / 255.0, 4),
                ]
        wanted_order = [
            ("AM_Dirtdefinition_BloodEffect", "Blood Soak (Wet)"),
            ("AM_Dirtsettings_Mud_Rural", "Rural Mud (Wet Glossy)"),
            ("AM_DirtSettings_Default_Swamp", "Swamp Grime"),
            ("AM_DirtSettings_Default_Beach", "Beach Sand"),
            ("AM_DirtSettings_Default_Dusty", "Dusty / Dry Road"),
            ("AM_DirtSettings_Default_Forest", "Forest Loam"),
            ("AM_DirtSettings_Default_Urban", "Urban Soot / Grime"),
            ("DIRTDEF_SNOW_DEFAULT", "Snow / Frost"),
        ]
        by_name = {}
        for d in root.findall("./DirtDefinitions/Item"):
            nm = (d.findtext("Name") or "").strip()
            if nm:
                by_name[nm] = d
        for nm, label in wanted_order:
            d = by_name.get(nm)
            if d is None:
                continue
            cname = (d.findtext("ColorLookup") or "").strip()
            rgb = colors.get(cname, [0.35, 0.30, 0.25])
            rl = float(d.find("RoughnessLow").attrib["value"]) if d.find("RoughnessLow") is not None else 0.5
            rh = float(d.find("RoughnessHigh").attrib["value"]) if d.find("RoughnessHigh") is not None else 0.85
            nc = float(d.find("NormalContribution").attrib["value"]) if d.find("NormalContribution") is not None else 0.2
            sk = d.find("SkinDirtModifiers")
            cl = d.find("ClothHideDirtModifiers")
            skin_boost = float(sk.find("DirtBoostStrength").attrib["value"]) if sk is not None and sk.find("DirtBoostStrength") is not None else 0.5
            cloth_boost = float(cl.find("DirtBoostStrength").attrib["value"]) if cl is not None and cl.find("DirtBoostStrength") is not None else 0.7
            out.append({
                "id": nm,
                "label": label,
                "colorName": cname,
                "rgb": rgb,
                "roughLow": round(rl, 4),
                "roughHigh": round(rh, 4),
                "normalContribution": round(nc, 4),
                "skinStrength": round(skin_boost, 3),
                "clothStrength": round(cloth_boost, 3),
            })
        # Also include Algae (Underwater) & Coal/Burn from CannedColors
        if "DIRT_ALGAE_GREEN" in colors:
            out.append({
                "id": "CANNED_ALGAE",
                "label": "Underwater Algae",
                "colorName": "DIRT_ALGAE_GREEN",
                "rgb": colors["DIRT_ALGAE_GREEN"],
                "roughLow": 0.28, "roughHigh": 0.55, "normalContribution": 0.2,
                "skinStrength": 0.45, "clothStrength": 0.7,
            })
        if "COAL_BLACK" in colors:
            out.append({
                "id": "CANNED_CHAR",
                "label": "Char / Burn Soot",
                "colorName": "COAL_BLACK",
                "rgb": colors["COAL_BLACK"],
                "roughLow": 0.82, "roughHigh": 0.96, "normalContribution": 0.35,
                "skinStrength": 0.85, "clothStrength": 0.95,
            })
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 4. Vital Organs (000000046000.xml)
# ---------------------------------------------------------------------------
def _load_vital_organs() -> list[dict]:
    path = COMMON_XML / "000000046000.xml"
    heart_r, throat_r, brain_r, brain_z = 0.055, 0.0325, 0.15, 0.12
    heart_m, throat_m, spine_m = 2.0, 4.0, 1.25
    if path.is_file():
        try:
            root = ET.parse(path).getroot()
            def _f(tag, default):
                el = root.find(f".//{tag}")
                return float(el.attrib["value"]) if el is not None and "value" in el.attrib else default
            heart_r = _f("fShotInTheHeartDist", heart_r)
            throat_r = _f("fShotInTheThroatDist", throat_r)
            brain_r = _f("fBrainRadius", brain_r)
            brain_z = _f("fBrainOffsetFromHeadZ", brain_z)
            heart_m = _f("fHeartDamageMultiplier", heart_m)
            throat_m = _f("fThroatDamageMultiplier", throat_m)
            spine_m = _f("fSpineDamageMultiplier", spine_m)
        except Exception:
            pass
    return [
        {"id": "heart", "label": "Heart", "bone": "SKEL_Spine3", "offsetRage": [0.035, -0.055, 0.055],
         "radius": round(heart_r, 4), "mult": heart_m, "color": [0.95, 0.16, 0.22]},
        {"id": "throat", "label": "Throat", "bone": "SKEL_Neck0", "offsetRage": [0.0, -0.042, 0.03],
         "radius": round(throat_r, 4), "mult": throat_m, "color": [1.0, 0.42, 0.18]},
        {"id": "brain", "label": "Brain", "bone": "SKEL_Head", "offsetRage": [0.0, 0.0, round(brain_z, 4)],
         "radius": round(min(brain_r, 0.095), 4), "xmlRadius": round(brain_r, 4), "mult": 1.5, "color": [0.96, 0.34, 0.72]},
        {"id": "spine", "label": "Spine", "bone": "SKEL_Spine2", "offsetRage": [0.0, 0.055, 0.03],
         "radius": 0.042, "mult": spine_m, "color": [0.35, 0.85, 0.96]},
    ]


# ---------------------------------------------------------------------------
# 5. Quick Character Animations (resolved from anim_clips.sqlite3)
# ---------------------------------------------------------------------------
_QUICK_ANIM_QUERIES = [
    ("idle", "Idle (Natural)", "Idle", "%idle%stand%"),
    ("walk", "Walk Cycle", "Locomotion", "%locomotion%@%walk%"),
    ("run", "Run / Jog", "Locomotion", "%locomotion%@%run%"),
    ("drunk", "Drunk Walk / Sway", "Drug & Status", "%drunk%standing%"),
    ("drugged", "Drugged / Unresponsive", "Drug & Status", "%drugged%"),
    ("pills", "Take Pills", "Drug & Status", "%take@pills%"),
    ("cocaine", "Cocaine Bump", "Drug & Status", "%cocaine_bump%"),
    ("weed", "Smoke Joint", "Drug & Status", "%smoke@joint%"),
    ("adrenaline", "Inject Adrenaline", "Drug & Status", "%inject@adrenaline%"),
    ("dance", "Club / Party Dance", "Club & Physiology", "%dance%"),
    ("workout", "Workout / Gym", "Club & Physiology", "%workout%"),
    ("suntan", "Apply Suntan Lotion", "Club & Physiology", "%applysuntanlotion%"),
    ("sweat", "Sweat Wipe", "Club & Physiology", "%sweat_wipe%"),
    ("vomit", "Vomit (Drunk)", "Drug & Status", "%vomit%"),
    ("taser", "Electrocuted / Taser", "Combat & Damage", "%electrocuted%"),
    ("burning", "On Fire / Burning", "Combat & Damage", "%behaviors@burning%"),
    ("hit_react", "Gunshot Hit React", "Combat & Damage", "%hit_react%"),
    ("bleedout", "Downed / Bleedout", "Combat & Damage", "%bleedout%"),
    ("combat", "Melee / Combat Stance", "Combat & Damage", "%melee%"),
]


def get_quick_anims(rig_kind: str = "female") -> list[dict]:
    rig = "male" if (rig_kind or "").lower() == "male" else "female"
    with _LOCK:
        if rig in _QUICK_ANIMS_CACHE:
            return _QUICK_ANIMS_CACHE[rig]
    out = []
    if not ANIM_DB.is_file():
        return out
    other = "female" if rig == "male" else "male"
    mcol = "match_m" if rig == "male" else "match_f"
    try:
        with closing(_ro_db(ANIM_DB)) as db:
            for key, label, group, pat in _QUICK_ANIM_QUERIES:
                row = db.execute(f"""
                    SELECT c.dict_id, c.hash AS clip_hash, c.short_name, c.name, d.name AS dict_name,
                           c.duration, c.frames, c.{mcol} AS matched_bones
                    FROM clip c JOIN dict d ON d.id = c.dict_id
                    WHERE (lower(d.name) LIKE ? OR lower(c.name) LIKE ?)
                      AND c.frames >= 10 AND c.{mcol} >= 20
                      AND lower(d.name) NOT LIKE '%animal%' AND lower(d.name) NOT LIKE '%creature%'
                    ORDER BY
                      (CASE WHEN lower(d.name) LIKE ? THEN 0 WHEN lower(d.name) LIKE ? THEN 2 ELSE 1 END),
                      c.{mcol} DESC,
                      ABS(c.duration - 3.5) ASC
                    LIMIT 1
                """, (pat, pat, f"%{rig}%", f"%{other}%")).fetchone()
                if row:
                    out.append({
                        "key": key,
                        "label": label,
                        "group": group,
                        "dictId": int(row["dict_id"]),
                        "clipHash": f"0x{int(row['clip_hash']) & 0xFFFFFFFF:08x}",
                        "name": row["short_name"] or row["name"],
                        "dictName": row["dict_name"],
                        "duration": round(float(row["duration"]), 2),
                        "frames": int(row["frames"]),
                        "matchedBones": int(row["matched_bones"]),
                    })
    except Exception:
        pass
    with _LOCK:
        _QUICK_ANIMS_CACHE[rig] = out
    return out


# ---------------------------------------------------------------------------
# 6. Tattoos (data.rpf/00001ae00000.pso -> decorations_0..3_3.ttpf)
# ---------------------------------------------------------------------------
def _swap_u64_hex(raw_hex: str, slot: int = 0) -> str:
    chunk = raw_hex[slot * 16:(slot + 1) * 16]
    if len(chunk) != 16:
        return ""
    val = struct.unpack("<Q", bytes.fromhex(chunk))[0]
    return f"{val:016x}" if val else ""


def _parse_mat34(raw_hex: str) -> list[float] | None:
    if not raw_hex or len(raw_hex) < 96:
        return None
    vals = struct.unpack("<12f", bytes.fromhex(raw_hex[:96]))
    return list(vals)


def _mul_affine2d(m1: list[float], m2: list[float]) -> tuple[float, float, float, float, float, float]:
    """Compose two RAGE 3x4 row-major UV affine matrices (x', y') = [a b tx; c d ty] * [x y 1]^T."""
    # RAGE stores row-vector affine: [u v 1] * [[m0, m1], [m4, m5], [m8, m9]]
    a1, b1, c1, d1, tx1, ty1 = m1[0], m1[1], m1[4], m1[5], m1[8], m1[9]
    a2, b2, c2, d2, tx2, ty2 = m2[0], m2[1], m2[4], m2[5], m2[8], m2[9]
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        tx1 * a2 + ty1 * c2 + tx2,
        tx1 * b2 + ty1 * d2 + ty2,
    )


def _invert_affine2d(a: float, b: float, c: float, d: float, tx: float, ty: float):
    """Invert UV_mesh -> UV_tex mapping into UV_tex -> UV_mesh placement (center, scale, angle)."""
    det = a * d - b * c
    if abs(det) < 1e-8:
        return (0.5, 0.5, 0.2, 0.2, 0.0)
    ia = d / det
    ib = -b / det
    ic = -c / det
    id_ = a / det
    # Center of tattoo texture (0.5, 0.5) in mesh UV space:
    # Solve [u v] * [[a,b],[c,d]] + [tx,ty] = [0.5, 0.5]
    rx, ry = 0.5 - tx, 0.5 - ty
    cu = rx * ia + ry * ic
    cv = rx * ib + ry * id_
    su = math.hypot(ia, ib)
    sv = math.hypot(ic, id_)
    ang = math.atan2(ib, ia)
    return (cu, cv, su, sv, ang)


def _load_tattoos() -> list[dict]:
    global _TATTOO_CACHE
    with _LOCK:
        if _TATTOO_CACHE is not None:
            return _TATTOO_CACHE
    pso_path = DATA_PSO / "00001ae00000.pso"
    if not pso_path.is_file() or not TEXTURES_DB.is_file():
        return []
    import decode_pso
    # Index available texture sizes/formats from textures.sqlite3
    tex_info: dict[str, tuple[int, int, str]] = {}
    with closing(_ro_db(TEXTURES_DB)) as db:
        for r in db.execute("""
            SELECT t.hash, t.width, t.height, t.format
            FROM texture t JOIN pack p ON p.id = t.pack
            WHERE p.path LIKE '%decorations%'
        """):
            tex_info[r["hash"]] = (int(r["width"]), int(r["height"]), r["format"])

    doc = decode_pso.decode(pso_path, {})
    layer_blocks: dict[int, list] = {}
    dec_recs = []
    for b in doc.get("blocks", []):
        if b["type"] == "0x77f0121d":
            layer_blocks[b["block"]] = b["records"]
        elif b["type"] == "0x40e79f52":
            dec_recs = b["records"]

    tattoos: list[dict] = []
    seen_albedo = set()
    for idx, rec in enumerate(dec_recs):
        dec_id = str(rec.get("0xacec22be") or f"0x{idx:08x}")
        base_m = _parse_mat34((rec.get("0x6e8249bf") or {}).get("raw_hex", "")) or [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
        arr_ref = rec.get("0xef888d7e")
        layers = []
        if isinstance(arr_ref, dict) and isinstance(arr_ref.get("target"), dict):
            tgt = arr_ref["target"]
            blk_idx = int(tgt.get("block", 0))
            elem_off = int(tgt.get("offset", 0))
            cnt = int(arr_ref.get("count", 1))
            if blk_idx in layer_blocks:
                blk = layer_blocks[blk_idx]
                start = elem_off // 320
                if 0 <= start < len(blk):
                    layers = blk[start:start + max(1, min(cnt, 4))]
        elif isinstance(arr_ref, list):
            layers = arr_ref

        for lrec in layers:
            if not isinstance(lrec, dict):
                continue
            slots_hex = (lrec.get("0x5adaff50") or {}).get("raw_hex", "")
            alb = _swap_u64_hex(slots_hex, 0)
            if not alb or alb not in tex_info or alb in seen_albedo:
                continue
            nrm = _swap_u64_hex(slots_hex, 1)
            if nrm and nrm not in tex_info:
                nrm = ""
            sub_m = _parse_mat34((lrec.get("0x8b424621") or {}).get("raw_hex", ""))
            if sub_m and any(abs(v) > 1e-6 for v in sub_m[:6]):
                aff = _mul_affine2d(base_m, sub_m)
            else:
                aff = (base_m[0], base_m[1], base_m[4], base_m[5], base_m[8], base_m[9])
            cu, cv, su, sv, ang = _invert_affine2d(*aff)
            # Filter out degenerate placements outside UV space
            if not (math.isfinite(cu) and math.isfinite(cv) and 0.01 <= su <= 2.5 and 0.01 <= sv <= 2.5):
                cu, cv, su, sv, ang = 0.5, 0.5, 0.22, 0.22, 0.0
            tints = lrec.get("0x0d23ee2d") or []
            tint_hex = "#ffffff"
            if isinstance(tints, list) and tints and isinstance(tints[0], int) and tints[0] > 0:
                c0 = tints[0]
                r0, g0, b0 = (c0 >> 16) & 255, (c0 >> 8) & 255, c0 & 255
                if r0 + g0 + b0 > 24:
                    tint_hex = f"#{r0:02x}{g0:02x}{b0:02x}"
            w, h, fmt = tex_info[alb]
            seen_albedo.add(alb)
            tattoos.append({
                "index": len(tattoos),
                "id": dec_id,
                "albedo": alb,
                "normal": nrm,
                "width": w,
                "height": h,
                "format": fmt,
                "tint": tint_hex,
                "u": round(max(0.02, min(0.98, cu)), 4),
                "v": round(max(0.02, min(0.98, cv)), 4),
                "su": round(max(0.03, min(1.0, su)), 4),
                "sv": round(max(0.03, min(1.0, sv)), 4),
                "angle": round(ang, 4),
            })
            break

    # Fallback: if array pointers didn't resolve all decoration blocks, also scan layer blocks directly
    if len(tattoos) < 200:
        for blk in layer_blocks.values():
            for lrec in blk:
                slots_hex = (lrec.get("0x5adaff50") or {}).get("raw_hex", "")
                alb = _swap_u64_hex(slots_hex, 0)
                if not alb or alb not in tex_info or alb in seen_albedo:
                    continue
                nrm = _swap_u64_hex(slots_hex, 1)
                if nrm and nrm not in tex_info:
                    nrm = ""
                w, h, fmt = tex_info[alb]
                seen_albedo.add(alb)
                tattoos.append({
                    "index": len(tattoos),
                    "id": f"0x{alb[:8]}",
                    "albedo": alb,
                    "normal": nrm,
                    "width": w,
                    "height": h,
                    "format": fmt,
                    "tint": "#ffffff",
                    "u": 0.5, "v": 0.5, "su": 0.22, "sv": 0.22, "angle": 0.0,
                })
                if len(tattoos) >= 2500:
                    break
            if len(tattoos) >= 2500:
                break

    with _LOCK:
        _TATTOO_CACHE = tattoos
    return tattoos


def search_tattoos(q: str = "", offset: int = 0, limit: int = 36) -> dict:
    all_t = _load_tattoos()
    ql = (q or "").strip().lower().removeprefix("0x")
    if ql:
        filt = [t for t in all_t if ql in t["albedo"] or ql in t["id"].lower() or ql in t["format"].lower()]
    else:
        filt = all_t
    off = max(0, offset)
    lim = max(1, min(120, limit))
    return {
        "total": len(filt),
        "offset": off,
        "limit": lim,
        "items": filt[off:off + lim],
    }


# ---------------------------------------------------------------------------
# 7. Blood & Wound Decals (data.rpf/00001b1a4000.pso + 00001b058000.pso -> ttdb_vfx.ttpf)
# ---------------------------------------------------------------------------
_CURATED_BLOOD_DECALS = [
    ("146c3559029639c3", "Blood Spatter (Heavy Crimson)", "Blood Spatter", False),
    ("b9cea1c1959db754", "Blood Spray / Gunshot Burst", "Blood Spatter", False),
    ("c8fd3d7c155eb889", "Blood Splat (Dense)", "Blood Spatter", False),
    ("2301d726eac1617c", "Blood Wound Pool", "Blood Spatter", False),
    ("81cc53f0b3abb383", "Blood Smear / Scrape", "Blood Spatter", False),
    ("420d664e979fc677", "Blood Drip Strip 01", "Drip Strip", False),
    ("313584a315b7ab70", "Blood Drip Strip 02", "Drip Strip", False),
    ("f3fe5d70dcdde3a5", "Blood Drip Strip 03", "Drip Strip", False),
    ("bd43d0dfcc58836d", "Blood Drip Strip 04", "Drip Strip", False),
    ("8999e43f94278ceb", "Wound / Bruise Alpha Mask 01", "Tinted Wound Mask", True),
    ("0da50fc15fcbaea0", "Wound / Bruise Alpha Mask 02", "Tinted Wound Mask", True),
    ("daf1eae2061fc56f", "Wound / Scrape Alpha Mask 03", "Tinted Wound Mask", True),
]


def get_blood_decals() -> list[dict]:
    global _BLOOD_CACHE
    with _LOCK:
        if _BLOOD_CACHE is not None:
            return _BLOOD_CACHE
    tex_info: dict[str, tuple[int, int, str]] = {}
    if TEXTURES_DB.is_file():
        with closing(_ro_db(TEXTURES_DB)) as db:
            for r in db.execute("""
                SELECT t.hash, t.width, t.height, t.format
                FROM texture t JOIN pack p ON p.id = t.pack
                WHERE p.path LIKE '%ttdb_vfx%'
            """):
                tex_info[r["hash"]] = (int(r["width"]), int(r["height"]), r["format"])

    out: list[dict] = []
    seen = set()
    for h, label, kind, tintable in _CURATED_BLOOD_DECALS:
        if h in tex_info:
            w, hi, fmt = tex_info[h]
            out.append({
                "hash": h, "normal": "", "label": label, "kind": kind,
                "tintable": tintable, "width": w, "height": hi, "format": fmt,
                "isStrip": hi >= w * 2,
            })
            seen.add(h)

    pso_path = DATA_PSO / "00001b1a4000.pso"
    if pso_path.is_file():
        import decode_pso
        doc = decode_pso.decode(pso_path, {})
        for b in doc.get("blocks", []):
            if b["block"] == 2:
                for idx, rec in enumerate(b["records"]):
                    sub = rec.get("0x063fa3f2") or {}
                    alb = _swap_u64_hex((sub.get("0xa255a1b8") or {}).get("raw_hex", ""), 0)
                    nrm = _swap_u64_hex((sub.get("0x9763cbdb") or {}).get("raw_hex", ""), 0)
                    if not alb or alb not in tex_info or alb in seen:
                        continue
                    w, hi, fmt = tex_info[alb]
                    is_strip = hi >= w * 2
                    out.append({
                        "hash": alb,
                        "normal": nrm if nrm in tex_info else "",
                        "label": f"Ped Blood Decal #{idx + 1:02d} ({w}×{hi})",
                        "kind": "Drip Strip" if is_strip else "Ped Blood Decal",
                        "tintable": not is_strip,
                        "width": w, "height": hi, "format": fmt,
                        "isStrip": is_strip,
                    })
                    seen.add(alb)

    with _LOCK:
        _BLOOD_CACHE = out
    return out


# ---------------------------------------------------------------------------
# 8. Rigged Dismemberment / Gore Stumps (inventory.sqlite3)
# ---------------------------------------------------------------------------
_LIMB_DEFS = [
    ("l_hand", "Left Hand", r"^gore_l_hand_", ["SKEL_L_Hand"]),
    ("r_hand", "Right Hand", r"^gore_r_hand_", ["SKEL_R_Hand"]),
    ("l_forearm", "Left Forearm", r"^gore_l_forearm_", ["SKEL_L_Forearm"]),
    ("r_forearm", "Right Forearm", r"^gore_r_forearm_", ["SKEL_R_Forearm"]),
    ("l_upperarm", "Left Upper Arm", r"^gore_l_upperarm_", ["SKEL_L_UpperArm"]),
    ("r_upperarm", "Right Upper Arm", r"^gore_r_upperarm_", ["SKEL_R_UpperArm"]),
    ("l_foot", "Left Foot", r"^gore_l_foot_", ["SKEL_L_Foot"]),
    ("r_foot", "Right Foot", r"^gore_r_foot_", ["SKEL_R_Foot"]),
    ("l_calf", "Left Calf / Shin", r"^gore_l_calf_", ["SKEL_L_Calf"]),
    ("r_calf", "Right Calf / Shin", r"^gore_r_calf_", ["SKEL_R_Calf"]),
    ("l_thigh", "Left Thigh", r"^gore_l_thigh_", ["SKEL_L_Thigh"]),
    ("r_thigh", "Right Thigh", r"^gore_r_thigh_", ["SKEL_R_Thigh"]),
    ("head", "Head (Decapitation)", r"^goreh_", ["SKEL_Head", "SKEL_Neck1", "SKEL_Neck2"]),
]


def get_gore_parts(model_lib, rig: str = "fr1") -> list[dict]:
    rig_l = (rig or "fr1").lower()
    if rig_l not in ("fr1", "mr1", "fb1", "mb1", "fo1", "mo1"):
        rig_l = "fr1" if rig_l.startswith("f") else "mr1"
    fallback_rig = "fr1" if rig_l.startswith("f") else "mr1"
    out = []
    try:
        with closing(model_lib.db()) as db:
            rows = db.execute("""
                SELECT id, name, archive, status
                FROM model
                WHERE status='indexed' AND is_rep=1
                  AND (name LIKE 'gore_%' OR name LIKE 'goreh_%')
                  AND name NOT LIKE '%_compliant'
                ORDER BY name
            """).fetchall()
    except Exception:
        return out

    for limb_id, label, rx, hide_roots in _LIMB_DEFS:
        pat = re.compile(rx, re.I)
        cands = [r for r in rows if pat.search(r["name"])]
        # Prefer exact rig token, then fallback rig, then un-rigged generic
        exact = [r for r in cands if f"_{rig_l}_" in r["name"].lower() or r["name"].lower().endswith(f"_{rig_l}")]
        if not exact and fallback_rig != rig_l:
            exact = [r for r in cands if f"_{fallback_rig}_" in r["name"].lower()]
        if not exact:
            exact = [r for r in cands if not re.search(r"_(fr1|mr1|fb1|mb1|fo1|mo1)_", r["name"].lower())]
        if exact:
            variants = [{"id": int(r["id"]), "name": r["name"]} for r in exact[:6]]
            out.append({
                "limb": limb_id,
                "label": label,
                "defaultId": variants[0]["id"],
                "variants": variants,
                "hideRootBones": hide_roots,
            })
    return out


# ---------------------------------------------------------------------------
# Master Catalog Assembly
# ---------------------------------------------------------------------------
def get_catalog(rig_kind: str = "female") -> dict:
    global _CATALOG_CACHE
    with _LOCK:
        if _CATALOG_CACHE is None:
            _CATALOG_CACHE = {
                "drugPresets": _load_drug_presets(),
                "bodyShapes": _load_body_shapes(),
                "dirtDefinitions": _load_dirt_definitions(),
                "vitalOrgans": _load_vital_organs(),
                "bloodDecals": get_blood_decals(),
            }
        base = dict(_CATALOG_CACHE)
    base["quickAnims"] = get_quick_anims(rig_kind)
    return base
