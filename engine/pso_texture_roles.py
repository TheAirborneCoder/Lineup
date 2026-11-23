# SPDX-License-Identifier: GPL-3.0-or-later
"""Recovered names for native material texture parameters in the SGD5 shader dictionaries.

The resource table stores a primary hash and a second hash. For aliased
parameters, two distinct, semantically consistent names reproduce the pair.
Equal hashes provide a single check and the spelling is marked as inferred.
These describe the binding's use, independent of pixel format or channel packing.
"""

from script_tree import _name_hash


def _layer_parameter(layer, role, suffix=None, effect="standard_4lyr"):
    spelling = f"lyr{layer}{suffix or role}tex"
    return f"Layer {layer} {role}", spelling, spelling, effect


def _uber_parameter(layer, role, prefix):
    spelling = f"{prefix}_layer{layer}"
    return f"Uber layer {layer} {role}", spelling, spelling, "standard_uber_vto"


# (label, material/secondary spelling, primary spelling, observed effect)
PARAMETERS = (
    # Paired SGD5 hashes (primary != secondary)
    ("Base color", "diffusetex", "diffusetexture", "cable"),
    ("Bump / normal", "bumptex", "bumptexture", "cable"),
    ("Specular", "speculartex", "spectexture", "cloth_normal_spec"),
    ("Secondary specular", "speculartex2", "spectexture2", "horse_standard_hair"),
    ("Emissive", "emissivetex", "emissivetexture", "ped_standard_emissive_layer"),
    ("Clearcoat", "clearcoattex", "clearcoattexture", "standard_clearcoat"),
    ("Anisotropy", "anisotex", "anisotexture", "standard_anisotropy"),
    ("Iridescence", "iridescencetex", "iridescencetexture", "standard_iridescence"),
    ("Diffraction", "diffractiontex", "diffractiontexture", "standard_diffraction"),
    ("Noise / fur noise", "noisetexture", "furnoisetexture", "standard_fur"),
    ("Translucency", "translucencytex", "translucencytexture", "standard_translucency"),
    ("Specular color", "specularcolortex", "specularcolortexture", "standard_specw"),
    # Single-hash SGD5 parameters (primary == secondary)
    ("Secondary diffuse", "diffusetex2", "diffusetex2", "dirt_normal_tnt"),
    ("Tint palette", "tintpalettetex", "tintpalettetex", "decal_tnt"),
    ("Height", "heighttexture", "heighttexture", "normal_decal_pxm"),
    ("Retroreflection", "retroreflectiontexture", "retroreflectiontexture", "standard_retroreflection"),
    ("Retroreflection (short)", "retroreflectiontex", "retroreflectiontex", "standard_2lyr_roadsign"),
    ("Flow", "flowtexture", "flowtexture", "water_river"),
    ("Roughness", "roughnessmap", "roughnessmap", "cable"),
    # Standard 2/3/4-layer
    *(_layer_parameter(i, role) for i in range(4) for role in ("diffuse", "normal")),
    *(_layer_parameter(i, "material A", "materiala") for i in range(4)),
    *(_layer_parameter(i, "material B", "materialb") for i in range(4)),
    *(_layer_parameter(i, "control") for i in (2, 3)),
    ("Dirt diffuse", "dirtdiffusetex", "dirtdiffusetex", "standard_2lyr"),
    ("Layer blend control", "controltexturetex", "controltexturetex", "standard_2lyr"),
    # Terrain uber 4-layer / 2-layer
    ("Uber terrain lookup", "lookuptexture", "lookuptexture", "standard_uber_vto"),
    ("Uber terrain grass mask", "grassmasktexture", "grassmasktexture", "standard_uber_vto"),
    ("Uber terrain tint", "tinttexture", "tinttexture", "standard_uber_vto"),
    ("Uber terrain tint bump", "tintbumptexture", "tintbumptexture", "standard_uber_vto"),
    *(_uber_parameter(i, "diffuse", "diffusetexture") for i in range(4)),
    *(_uber_parameter(i, "bump", "bumptexture") for i in range(4)),
    *(_uber_parameter(i, "material A", "materialmapatexture") for i in range(4)),
    # Terrain LOD / SLOD / dirt normal
    ("Bump / normal (unaliased)", "bumptexture", "bumptexture", "terrain_slod"),
    ("Specular (unaliased)", "speculartexture", "speculartexture", "terrain_slod"),
    ("Material B", "materialbtexture", "materialbtexture", "terrain_slod"),
    ("Secondary bump / normal", "bumptexture2", "bumptexture2", "standard_plus_normal"),
    ("Secondary specular (unaliased)", "speculartexture2", "speculartexture2", "terrain_lod_2lyr_grass_wetness"),
    ("Secondary material B", "materialbtexture2", "materialbtexture2", "terrain_lod_2lyr_grass_wetness"),
    # Vehicle weathering & glass alt set
    ("Dirt noise", "dirtnoisetex", "dirtnoisetex", "standard_vehicle_2lyr"),
    ("Dust", "dusttexture", "dusttexture", "standard_vehicle_2lyr"),
    ("Scratch", "scratchtexture", "scratchtexture", "standard_vehicle_alpha"),
    ("Alt base color", "altdiffusetex", "altdiffusetex", "standard_vehicle_glass"),
    ("Alt bump / normal", "altbumptexture", "altbumptexture", "standard_vehicle_glass"),
    ("Alt specular", "altspectexture", "altspectexture", "standard_vehicle_glass"),
    ("Alt secondary specular", "altspectexture2", "altspectexture2", "standard_vehicle_glass"),
    # Interior
    ("Interior diffuse", "interiordiffusemap", "interiordiffusemap", "standard_interior"),
    ("Interior layer diffuse", "interiorlayerdiffusemap", "interiorlayerdiffusemap", "standard_interior"),
    ("Interior diffuse mask", "interiordiffusemaskmap", "interiordiffusemaskmap", "standard_interior"),
    # Alpha / glass / refraction / liquid
    ("Refraction bump", "refractionbumptexture", "refractionbumptexture", "standard_alpha"),
    ("Extinction LUT", "extinctionlut", "extinctionlut", "standard_alpha"),
    ("Looking glass blur", "lookingglassblurtexture", "lookingglassblurtexture", "standard_alpha_looking_glass"),
    ("Liquid extinction LUT", "liquidextinctionlut", "liquidextinctionlut", "standard_alpha_liquid"),
    ("Liquid surface tint mask", "liquidsurfacetintmask", "liquidsurfacetintmask", "standard_alpha_liquid_opaque_liquid"),
    ("Liquid tint mask", "liquidtintmask", "liquidtintmask", "standard_alpha_liquid_opaque_liquid"),
    ("Liquid tint palette", "liquidtintpalette", "liquidtintpalette", "standard_alpha_liquid_opaque_liquid"),
    ("Liquid surface bump", "liquidsurfacebumptex", "liquidsurfacebumptex", "standard_alpha_liquid_opaque_liquid"),
    # Water micro geom
    ("Water surface detail", "watersurfacedetailtexture", "watersurfacedetailtexture", "water_micro_geom"),
    ("Water surface map", "watersurfacemaptexture", "watersurfacemaptexture", "water_micro_geom"),
    ("Water height", "waterheighttexture", "waterheighttexture", "water_micro_geom"),
    *((f"Surface detail {i}", f"surfacedetailtexture{i}", f"surfacedetailtexture{i}", "water_micro_geom") for i in range(3)),
    *((f"Surface detail normal {i}", f"surfacedetailnormal{i}", f"surfacedetailnormal{i}", "water_micro_geom") for i in range(3)),
    # Ped / weapon / wrinkle
    ("Material ID", "materialid", "materialid", "weapon"),
    ("Material C", "materialctexture", "materialctexture", "ped_standard_skin"),
    ("Blood flow mask", "bloodflowmask", "bloodflowmask", "ped_standard_skin"),
    ("Wrinkle occlusion mask", "wrinkleocclusionmask", "wrinkleocclusionmask", "ped_standard_animal"),
    ("Standard wrinkle", "standardwrinkletexture", "standardwrinkletexture", "standard_wrinkle"),
    ("Standard wrinkle mask", "standardwrinklemasktexture", "standardwrinklemasktexture", "standard_wrinkle"),
    # Decal text / signs / emissive / lightmap
    ("Font diffuse", "fontdiffusemap", "fontdiffusemap", "standard_decal_blend_text"),
    ("Font normal", "fontnormalmap", "fontnormalmap", "standard_decal_blend_text"),
    ("Distance map", "distancemap", "distancemap", "distance_map"),
    ("Sign icon", "signicon", "signicon", "signs"),
    ("Animated emissive", "emissiveanimatedtexture", "emissiveanimatedtexture", "standard_emissive_animated"),
    ("Light vector", "lightvectortexture", "lightvectortexture", "standard_emissive_lightmap"),
    ("Scrolling emissive", "emissivescrollingtexture", "emissivescrollingtexture", "standard_emissive_animated_3lyr"),
    ("Emissive distortion", "emissivedistortiontexture", "emissivedistortiontexture", "standard_emissive_animated_distortion"),
)

ROLES = {
    _name_hash(spelling): {
        "role": role,
        "spelling": spelling,
        "effect": effect,
        "primaryHash": f"{_name_hash(primary):08x}",
        "primarySpelling": primary,
        "evidence": "paired SGD5 hashes" if primary != spelling else "inferred from one SGD5 hash",
    }
    for role, spelling, primary, effect in PARAMETERS
}
