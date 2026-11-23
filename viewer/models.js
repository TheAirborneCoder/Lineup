"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmtN = (n) => (n == null ? "–" : Number(n).toLocaleString());
const S = { summary: null, category: "", subcategory: "", start: 0, limit: 150, total: 0, item: null, lod: 0,
  texRes: 8192, partMeta: new Map(), muvPresets: new Map(), partOverrides: new Map(), groomState: null, charOutfits: null,
  skinHarmonize: true, view: null, textures: new Map(), request: 0, rowIds: [], rig: null };
const _skelCache = new Map(); // skeletonId -> Promise<skeletonData | null>
const _groomCatalogCache = new Map(); // rig -> Promise<catalog>
const _groomStrandCache = new Map(); // url -> Promise<ArrayBuffer>
const curTexRes = () => S.texRes || 8192;

async function api(path, params = {}) {
  const r = await fetch("/api/models/" + path + "?" + new URLSearchParams(params));
  const body = r.headers.get("Content-Type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw Error(body?.error || `Request failed (${r.status})`);
  return body;
}
function status(text) { $("status").textContent = text; }
function loading(text, error = false) {
  const l = $("load");
  l.hidden = !text;
  l.textContent = text || "";
  l.classList.toggle("error", error);
}
function palette(i) {
  const h = (i * 137.508) % 360, s = 0.42, l = 0.62;
  const k = (n) => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
  return [0, 8, 4].map((n) => l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1)));
}
const css = (c) => `rgb(${c.map((x) => Math.round(x * 255)).join(",")})`;

// Categories --------------------------------------------------------------------------------------
function renderTree() {
  const tree = $("tree");
  tree.replaceChildren();
  const all = document.createElement("button");
  const items = S.summary.categories.reduce((n, c) => n + c.items, 0);
  all.innerHTML = `<span class="caret"></span>All models<span class="n">${fmtN(items)}</span>`;
  all.setAttribute("aria-current", !S.category);
  all.onclick = () => selectCategory("", "");
  tree.append(all);
  for (const c of S.summary.categories) {
    const b = document.createElement("button");
    const open = S.category === c.category;
    b.innerHTML = `<span class="caret">${open ? "▾" : "▸"}</span><span class="${c.hidden ? "hidden-cat" : ""}">${esc(c.category)}</span><span class="n">${fmtN(c.items)}</span>`;
    b.title = `${fmtN(c.items)} items from ${fmtN(c.resources)} model resources`;
    b.setAttribute("aria-current", open && !S.subcategory);
    b.onclick = () => selectCategory(c.category, "");
    tree.append(b);
    if (!open) continue;
    for (const s of c.subcategories) {
      const sb = document.createElement("button");
      sb.className = "sub";
      sb.innerHTML = `${esc(s.name)}<span class="n">${fmtN(s.items)}</span>`;
      sb.title = `${fmtN(s.items)} items from ${fmtN(s.resources)} model resources`;
      sb.setAttribute("aria-current", S.subcategory === s.name);
      sb.onclick = () => selectCategory(c.category, s.name);
      tree.append(sb);
    }
  }
}
function selectCategory(category, subcategory) {
  S.category = category;
  S.subcategory = subcategory;
  S.start = 0;
  renderTree();
  saveUrl();
  loadList();
}

// List ---------------------------------------------------------------------------------------------
let listTimer;
async function loadList() {
  const pending = ++S.request;
  const params = { start: S.start, limit: S.limit, q: $("q").value.trim(), sort: $("sort").value,
    archive: $("archive").value, category: S.category, subcategory: S.subcategory,
    textured: $("textured").checked ? 1 : "" };
  try {
    const data = await api("list", params);
    if (pending !== S.request) return;
    S.total = data.total;
    if ($("archive").options.length === 1)
      for (const a of data.archives) $("archive").append(new Option(a, a));
    const col = Object.fromEntries(data.columns.map((c, i) => [c, i]));
    const list = $("list");
    list.replaceChildren();
    S.rowIds = data.rows.map((r) => r[col.id]);
    for (const r of data.rows) {
      const b = document.createElement("button");
      b.className = "model-row" + (r[col.status] !== "indexed" ? " failed" : "");
      b.dataset.id = r[col.id];
      b.setAttribute("aria-current", S.item && S.item.id === r[col.id]);
      const extra = r[col.group_size] > 1 ? (r[col.family] ? `${r[col.group_size]} parts` : `${r[col.group_size]} variants`) : "";
      b.innerHTML = `<strong title="${esc(r[col.name])}">${esc(r[col.name])}</strong><span class="badge">${esc(extra)}</span>
        <small>${esc(r[col.subcategory])} · ${esc(r[col.archive])} · ${r[col.texture_refs] ? r[col.textures_found] + "/" + r[col.texture_refs] + " textures" : "no texture slots"}</small>`;
      b.onclick = () => openModel(r[col.id]);
      list.append(b);
    }
    $("count").textContent = fmtN(data.total);
    const page = Math.floor(S.start / S.limit) + 1, pages = Math.max(1, Math.ceil(data.total / S.limit));
    $("pageLabel").textContent = `${page} / ${fmtN(pages)}`;
    $("prev").disabled = S.start === 0;
    $("next").disabled = S.start + S.limit >= data.total;
    status(`${fmtN(data.total)} items${S.category ? " in " + S.category + (S.subcategory ? " / " + S.subcategory : "") : ""}`);
  } catch (e) {
    status(e.message);
  }
}

// Model -------------------------------------------------------------------------------------------
function renderLodButtons(item) {
  const host = $("lods");
  const lods = item?.lods || [];
  if (lods.length <= 1) { host.hidden = true; host.replaceChildren(); return; }
  host.hidden = false;
  host.replaceChildren();
  for (const lv of lods) {
    const b = document.createElement("button");
    b.textContent = `LOD ${lv}`;
    b.title = `Switch to level of detail ${lv}`;
    b.setAttribute("aria-pressed", lv === S.lod);
    b.onclick = () => { if (S.lod !== lv) switchLod(lv); };
    host.append(b);
  }
}

const rgbToHex = (c) => "#" + (c || [0.7, 0.7, 0.7]).map((x) => Math.max(0, Math.min(255, Math.round(x * 255))).toString(16).padStart(2, "0")).join("");
const hexToRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);

function updateDownloadLink(item, lod) {
  const a = $("downloadGlb"), b = $("exportTexturedGlb");
  if (!item) { a.hidden = true; if (b) b.hidden = true; return; }
  const suffix = lod ? `_lod${lod}` : "";
  a.href = `/api/models/glb?id=${item.id}&lod=${lod}`;
  a.download = `${item.name}${suffix}.glb`;
  a.hidden = false;
  if (b) {
    b.hidden = false;
    if (S.rig?.activeAnim) {
      b.textContent = "Export Animated GLB";
      b.title = "Export Blender-ready glTF 2.0 (.glb) with embedded textures, full skeleton, 8-influence skinning, active parts, and the playing animation clip";
    } else if (S.rig?.info) {
      b.textContent = "Export Rigged GLB";
      b.title = "Export Blender-ready glTF 2.0 (.glb) with embedded textures, full skeleton, 8-influence skinning, and active parts";
    } else {
      b.textContent = "Export Textured GLB";
      b.title = "Export Blender-ready glTF 2.0 (.glb) with embedded PNG textures, normal maps, active parts, wheels, and live tint/palette edits";
    }
  }
}

async function exportTexturedGlb() {
  const item = S.item;
  if (!item) return;
  const btn = $("exportTexturedGlb");
  const prev = btn.textContent;
  btn.disabled = true;
  const hasRig = !!S.rig?.info;
  const activeAnim = S.rig?.activeAnim || null;
  btn.textContent = activeAnim ? "Building Animated GLB…" : (hasRig ? "Building Rigged GLB…" : "Building GLB…");
  status(`Building ${activeAnim ? "animated + rigged + textured" : (hasRig ? "rigged + textured" : "textured")} glTF 2.0 (.glb) for ${item.name}…`);
  try {
    const params = new URLSearchParams({ id: item.id, lod: S.lod, textured: "1", maxTex: String(curTexRes()) });
    if (S.parts && S.parts.size) params.set("parts", [...S.parts.keys()].join(","));
    if (S.wheels?.enabled && item.wheelSetup) params.set("wheels", `${S.wheels.rimId}:${S.wheels.tyreId}`);
    if (hasRig) {
      params.set("rig", "1");
      if (activeAnim) {
        params.set("animDictId", String(activeAnim.dictId));
        params.set("animClipHash", String(activeAnim.clipHash));
        if (S.view?._animState?.rootMotion) params.set("rootMotion", "1");
      }
    }
    const ov = {};
    for (const m of item.materials || []) {
      const entry = { visible: m.visible !== false };
      if (m.activeMuvPreset) {
        entry.muvPreset = m.activeMuvPreset;
      } else {
        if (m.active !== undefined) entry.tex = m.active;
        if (m.texMode !== undefined) entry.texMode = m.texMode;
        if (m.tintColor && (m.tintOverride || m.texMode === 1 || m.texMode === 3 || !m.active)) entry.tint = m.tintColor;
        if (m.palette && m.texMode === 2) entry.pal = m.palette;
      }
      if (m.activeNormal !== undefined) entry.norm = m.activeNormal;
      if (m.tintPalette) entry.tintRow = m.tintRow || 0;
      ov[m.index] = entry;
    }
    params.set("overrides", JSON.stringify(ov));
    if (S.anatomyTex && Object.keys(S.anatomyTex).length) {
      const atOut = { ...S.anatomyTex };
      if (S.skinHarmonize === false) {
        delete atOut.upperGain;
        delete atOut.lowerGain;
      }
      params.set("anatTex", JSON.stringify(atOut));
    }
    if (S.muvPresets && S.muvPresets.size) {
      const mpObj = {};
      for (const [k, v] of S.muvPresets.entries()) if (v) mpObj[String(k)] = v;
      if (Object.keys(mpObj).length) params.set("muvPresets", JSON.stringify(mpObj));
    }
    if (S.partOverrides && S.partOverrides.size) {
      const poObj = {};
      for (const [k, v] of S.partOverrides.entries()) if (v) poObj[String(k)] = v;
      if (Object.keys(poObj).length) params.set("partOverrides", JSON.stringify(poObj));
    }
    const cm = S.view?.charMorph;
    if (cm) {
      const bmOut = {};
      for (const [k, v] of Object.entries(cm)) {
        if (k === "height" || k === "defaultHeight" || k.startsWith("jiggle")) continue;
        if (typeof v === "number" && Number.isFinite(v) && Math.abs(v) > 1e-4) bmOut[k] = Number(v.toFixed(4));
      }
      if (Object.keys(bmOut).length) params.set("bodyMorphs", JSON.stringify(bmOut));
      if (cm.defaultHeight > 0 && cm.height > 0) {
        const hs = cm.height / cm.defaultHeight;
        if (Math.abs(hs - 1.0) > 1e-3) params.set("heightScale", hs.toFixed(4));
      }
    }
    const r = await fetch("/api/models/glb?" + params);
    if (!r.ok) {
      let msg = `Export failed (${r.status})`;
      try { msg = (await r.json()).error || msg; } catch (_) {}
      throw Error(msg);
    }
    const blob = await r.blob();
    const suffix = S.lod ? `_lod${S.lod}` : "";
    const tag = activeAnim ? "_animated" : (hasRig ? "_rigged" : "_textured");
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${item.name}${suffix}${tag}.glb`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    status(`Exported ${link.download} (${(blob.size / 1024).toFixed(1)} KB, Blender-ready glTF 2.0)`);
  } catch (e) {
    status(`Export error: ${e.message}`);
  } finally {
    btn.disabled = false;
    updateDownloadLink(S.item, S.lod);
    if (!S.item) btn.textContent = prev;
  }
}

async function switchLod(lod) {
  const item = S.item;
  if (!item) return;
  const pending = (S.open = (S.open || 0) + 1);
  S.lod = lod;
  renderLodButtons(item);
  loading(`Decoding LOD ${lod}…`);
  try {
    const r = await fetch(`/api/models/glb?id=${item.id}&lod=${lod}`);
    if (!r.ok) throw Error((await r.json()).error || "Decode failed");
    const info = S.view.load(await r.arrayBuffer(), "main", false, true);
    if (pending !== S.open || S.item !== item) return;
    loading("");
    item.view = info;
    if (S.rig?.info) attachPartSkin(item.id, "main", lod);
    const fresh = await api("item", { id: item.id, lod });
    if (pending !== S.open || S.item !== item) return;
    item.decode = fresh.decode;
    updateDownloadLink(item, lod);
    renderInspector(item, info);
    if (S.wheels?.enabled && item.wheelSetup) applyWheels(item, false);
    updatePartStats();
  } catch (e) {
    if (pending !== S.open) return;
    loading(e.message, true);
  }
}

async function openModel(id) {
  const pending = (S.open = (S.open || 0) + 1);
  for (const b of document.querySelectorAll(".model-row")) {
    const active = Number(b.dataset.id) === id;
    b.setAttribute("aria-current", active);
    if (active) b.scrollIntoView({ block: "nearest" });
  }
  loading("Reading model metadata…");
  $("empty").hidden = true;
  $("downloadGlb").hidden = true;
  if ($("exportTexturedGlb")) $("exportTexturedGlb").hidden = true;
  let item;
  try {
    item = await api("item", { id });
  } catch (e) {
    loading(e.message, true);
    return;
  }
  if (pending !== S.open) return;
  S.item = item;
  S.lod = (item.lods && item.lods.length) ? item.lods[0] : 0;
  S.parts = new Map();
  S.partMeta = new Map();
  S.muvPresets = new Map();
  S.partOverrides = new Map();
  S.groomState = null;
  S.charOutfits = null;
  S.skinHarmonize = true;
  S.anatomyTex = {};
  const _ds = item.characterAssembly?.defaultSkin;
  if (_ds) {
    for (const k of ["upperAlbedo", "upperNormal", "lowerAlbedo", "lowerNormal", "genitalAlbedo", "genitalNormal", "eyeAlbedo", "upperGain", "lowerGain"]) {
      if (_ds[k]) S.anatomyTex[k] = _ds[k];
    }
  }
  S.rig = null;
  S.view.clearSkeleton();
  S.view.clearGroomStrands?.();
  window.CharEffectsStudio?.resetForModel(item, S.view);
  S.wheels = item.wheelSetup ? { enabled: true, rimId: item.wheelSetup.defaultRimId, tyreId: item.wheelSetup.defaultTyreId, infoRim: null, infoTyre: null } : null;
  S.wardrobeSlot = "top";
  saveUrl();
  $("crumb").innerHTML = `${esc(item.category)} <span>/</span> ${esc(item.subcategory)}`;
  $("title").textContent = item.name;
  $("subtitle").textContent = `${item.archive} · ${item.kind} · ${item.member}${item.root !== "0x0" ? " root " + item.root : ""}`;
  renderLodButtons(item);
  S.view.resetMaterials();
  renderInspector(item, null);
  loading("Decoding geometry…");
  try {
    const r = await fetch(`/api/models/glb?id=${id}&lod=${S.lod}`);
    if (!r.ok) throw Error((await r.json()).error || "Decode failed");
    const info = S.view.load(await r.arrayBuffer());
    if (pending !== S.open) return;
    loading("");
    item.view = info;
    if (item.rigInfo) initRigForItem(item, pending);
    updateDownloadLink(item, S.lod);
    // Refresh decode facts recorded by the server.
    const fresh = await api("item", { id, lod: S.lod });
    if (pending !== S.open) return;
    item.decode = fresh.decode;
    renderInspector(item, info);
    applyMaterials(item);
    // A weapon's root drawable is a small mount piece; show it with one variant of each attachment slot.
    if (item.category === "Weapons" && item.family && item.name.replace(/_(hi|mid)$/, "") === item.family) {
      if (info.triangles <= 200) {
        for (const m of item.materials) { m.visible = false; S.view.setMaterial(m.index, { visible: false }); }
        renderMaterials(item);
        const note = document.createElement("p");
        note.className = "note warn";
        note.textContent = `The root model is a ${info.triangles}-triangle proxy and is hidden. Tick its material to show it.`;
        $("materialList").prepend(note);
      }
      assembleDefaults(item);
    } else if (item.characterAssembly && item.characterAssembly.parts?.length > 1 &&
               (item.characterAssembly.parts[0].id === item.id || item.is_rep)) {
      // Human characters default to nude (Head + Eyes + Teeth + Skin, no clothing mounted by default) + default hair grooming!
      const isHuman = item.characterAssembly.kind !== "Animal";
      assembleCharacter(item, { nudeOnly: isHuman }).then(() => {
        if (pending === S.open && S.item === item) {
          window.CharEffectsStudio?.onPartsReady?.(item);
        }
      });
      if (isHuman) {
        initCharacterGroom(item, pending);
        loadCharacterOutfits(item, pending);
      }
    } else if ((item.category === "Animals" || item.category === "Protagonists" || item.category === "Characters") &&
               item.family && item.is_rep && item.group.length > 1) {
      assembleDefaults(item);
    }
    if (S.wheels?.enabled && item.wheelSetup) applyWheels(item, true);
    updatePartStats();
  } catch (e) {
    if (pending !== S.open) return;
    S.view.clear();
    $("stats").textContent = "";
    loading(e.message, true);
    renderInspector(item, null, e.message);
  }
}

// Recovered native shader parameter roles from pso_texture_roles.py, with sRGB/BC5/detail fallbacks.
const BASE_COLOR = "c8e8c282", OVERLAY = "0df47048", DIRT = "7485dafb", BUMP = "1e77bf4c", SPECULAR = "c6f46e79", TINT_PALETTE = "2c8d7182";
const SPECULAR2 = "739e3d0d", MAT_A0 = "2bb2ede8", MAT_A1 = "4d56d7fe", DUST = "fb03ae36", DIRT_NOISE = "cc155f4b", VEH_LUT_PARAM = "19df25d7", DIST_MAP = "0dbc5a4e";
const HEAD_SHADER = "379efcef", SKIN_SHADERS = new Set(["379efcef", "210fbc6a"]), EYE_SHADER = "89a1cf72", WATER_STENCIL_SHADER = "fc34f08b";
const WATER_SHADERS = new Set(["7f3a6aa9", "97596162", "735d2de6"]); // water_micro_geom, standard_alpha_water, standard_alpha_waterfall
const DIST_MAP_SHADER = "e05f1a42"; // distance_map (SDF road signs & plaques)
const PROCEDURAL_MUV_SHADERS = new Set([
  "d5ed50f4", "39ddb9ca", "7574b09b", "ea8d09d0",
  "73055d06", "c78a3e26", "9880a0c9", "1c43a65b",
  "6f3c7994", "1df84fef",
]); // ped_standard_muv*
const WEAPON_SHADERS = new Set(["e68a4a98", "348b6795"]); // weapon, weapon_muv_alpha
const VEHICLE_GLASS_SHADERS = new Set(["fa9cc069", "bd71bdbf", "389206d1", "31212453"]); // standard_vehicle_glass, windshield_wipermask, standard_vehicle_alpha*
const VEHICLE_PAINT_SHADERS = new Set(["4aa38282", "476ac73a", "03b27476", "469ebe36", "4d2b05e7", "921b19e5", "4dc0ac6a", "e0166a30", "f25d4644", "4f1964cc", "f3ad0561", "0b11a70e", "4ea57893", "87493dca", "e810a19b"]); // standard_vehicle_2lyr* (vehicles, props, world)
const VEHICLE_WHEEL_SHADERS = new Set(["c5cdbdd2", "570fcace"]); // standard_vehicle_2lyr_wheel*
const VEHICLE_TYRE_SHADERS = new Set(["99ad6fc9", "b994d670", "42ac0460"]); // standard_vehicle_2lyr_tyre*
const VEHICLE_LUT_SHADERS = new Set([...VEHICLE_PAINT_SHADERS, ...VEHICLE_WHEEL_SHADERS, ...VEHICLE_TYRE_SHADERS]);
const DIFFUSE_ROLES = new Set(["Base color", "Layer 0 diffuse", "Interior diffuse", "Alt base color",
  "Font diffuse", "Uber layer 0 diffuse"]);
const NORMAL_ROLES = new Set(["Bump / normal", "Bump / normal (unaliased)", "Layer 0 normal", "Layer 1 normal",
  "Alt bump / normal", "Font normal", "Uber layer 0 bump"]);
const CAR_PAINTS = [
  [0.16, 0.27, 0.45], [0.58, 0.13, 0.14], [0.85, 0.86, 0.88], [0.18, 0.20, 0.23],
  [0.14, 0.34, 0.24], [0.68, 0.42, 0.13], [0.46, 0.49, 0.53], [0.26, 0.22, 0.38],
];
// Real GTA VI LUT (fbc2b8d31c2f4684) Slice 1/2 paint rows + matching analytic fallbacks.
const VEH_PAINT_PRESETS = [
  { name: "Jet Black",       row1: 129, row2: 100, slice: 2, twoTone: 0, primary: [0.09, 0.09, 0.10], secondary: [0.90, 0.90, 0.91], pearl: [0.35, 0.42, 0.52] },
  { name: "Sheriff Cruiser", row1: 250, row2: 100, slice: 2, twoTone: 1, primary: [0.04, 0.04, 0.05], secondary: [0.91, 0.91, 0.91], pearl: [0.22, 0.28, 0.36] },
  { name: "Glacier White",   row1: 100, row2: 129, slice: 2, twoTone: 0, primary: [0.91, 0.91, 0.91], secondary: [0.12, 0.12, 0.13], pearl: [0.85, 0.90, 0.98] },
  { name: "Vice Blue",       row1: 179, row2: 100, slice: 1, twoTone: 0, primary: [0.18, 0.34, 0.62], secondary: [0.88, 0.88, 0.89], pearl: [0.45, 0.78, 1.00] },
  { name: "Crimson Red",     row1: 19,  row2: 129, slice: 2, twoTone: 0, primary: [0.58, 0.12, 0.16], secondary: [0.12, 0.12, 0.13], pearl: [0.95, 0.35, 0.28] },
  { name: "Sunset Copper",   row1: 23,  row2: 129, slice: 2, twoTone: 0, primary: [0.66, 0.32, 0.14], secondary: [0.14, 0.14, 0.15], pearl: [1.00, 0.72, 0.36] },
  { name: "Graphite Steel",  row1: 7,   row2: 100, slice: 1, twoTone: 0, primary: [0.28, 0.30, 0.33], secondary: [0.88, 0.88, 0.89], pearl: [0.62, 0.70, 0.82] },
  { name: "Emerald",         row1: 203, row2: 129, slice: 1, twoTone: 0, primary: [0.16, 0.36, 0.24], secondary: [0.14, 0.14, 0.15], pearl: [0.44, 0.88, 0.62] },
  { name: "Silver",          row1: 93,  row2: 129, slice: 1, twoTone: 0, primary: [0.72, 0.72, 0.74], secondary: [0.14, 0.14, 0.15], pearl: [0.85, 0.90, 0.96] },
];
const WPN_FINISH_PRESETS = [
  { id: 0, name: "Factory Tactical", dot: "#2d3238" },
  { id: 1, name: "Blued & Walnut",   dot: "#5c341d" },
  { id: 2, name: "Coyote Tan / FDE", dot: "#8d7556" },
  { id: 3, name: "Olive Drab",       dot: "#48543d" },
  { id: 4, name: "Stainless",        dot: "#bcc2c8" },
  { id: 5, name: "Vice Gold",        dot: "#d9aa46" },
  { id: 6, name: "Arctic Two-Tone",  dot: "#cfd5dc" },
];
const texHash = (t) => t?.effectiveHash || t?.textureHash || null;
const isUsable2D = (t) => t.sources.length && t.sources[0].dimension === "2D" && t.sources[0].height > 1 && !t.sources[0].isTintPalette && t.parameter !== TINT_PALETTE;
const _lutArrayCache = new Map(); // hash -> Promise<{ width, height, slices, pixels } | null>

function ensureLutArray(hash) {
  if (!hash) return Promise.resolve(null);
  if (_lutArrayCache.has(hash)) return _lutArrayCache.get(hash);
  const p = (async () => {
    try {
      const r = await fetch(`/api/models/tex-array?hash=${encodeURIComponent(hash)}`);
      if (r.ok) {
        const buf = await r.arrayBuffer();
        const entry = S.view ? S.view.setSharedLutArray(hash, buf) : null;
        if (entry) return entry;
      }
    } catch {}
    // Fallback before server restart: load Slice 0 PNG via existing /api/models/texture endpoint.
    try {
      const img = await textureImage(hash, 256, 0);
      if (img && S.view) return S.view.setSharedLutImage(hash, img);
    } catch {}
    return null;
  })();
  _lutArrayCache.set(hash, p);
  return p;
}

function sampleLutColor(hash, slice, row) {
  const entry = S.view?._lutArrays?.get(hash);
  if (!entry || !entry.pixels) return null;
  const s = Math.max(0, Math.min(entry.slices - 1, Math.round(slice)));
  const y = Math.max(0, Math.min(entry.height - 1, Math.round(row)));
  const x = Math.floor(entry.width * 0.5);
  const off = ((s * entry.height + y) * entry.width + x) * 4;
  return [entry.pixels[off] / 255, entry.pixels[off + 1] / 255, entry.pixels[off + 2] / 255];
}

function vehiclePaintColor(name) {
  const s = (name || "").toLowerCase();
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619) >>> 0;
  return CAR_PAINTS[h % CAR_PAINTS.length];
}

function vehicleDefaultPreset(name) {
  const s = (name || "").toLowerCase();
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619) >>> 0;
  return VEH_PAINT_PRESETS[h % VEH_PAINT_PRESETS.length];
}

function animalMaskPalette(name) {
  const n = (name || "").toLowerCase();
  if (/antler|horn/.test(n)) return [[0.52, 0.44, 0.33], [0.34, 0.26, 0.18], [0.85, 0.81, 0.73]];
  if (n.includes("deer_agh")) return [[0.34, 0.35, 0.24], [0.44, 0.38, 0.24], [0.74, 0.75, 0.66]];
  if (n.includes("deer")) return [[0.48, 0.33, 0.20], [0.64, 0.38, 0.19], [0.92, 0.90, 0.86]];
  if (n.includes("cattle_holstein")) return [[0.14, 0.13, 0.13], [0.95, 0.94, 0.91], [0.16, 0.15, 0.15]];
  if (n.includes("horse")) return [[0.44, 0.26, 0.15], [0.18, 0.14, 0.12], [0.90, 0.88, 0.84]];
  if (n.includes("cat_")) return [[0.48, 0.43, 0.38], [0.22, 0.20, 0.18], [0.90, 0.88, 0.85]];
  if (n.includes("goat")) return [[0.46, 0.35, 0.25], [0.20, 0.17, 0.15], [0.88, 0.85, 0.80]];
  if (n.includes("sheep")) return [[0.78, 0.74, 0.67], [0.56, 0.48, 0.40], [0.88, 0.85, 0.79]];
  if (n.includes("mouse")) return [[0.45, 0.41, 0.37], [0.33, 0.29, 0.26], [0.80, 0.72, 0.70]];
  if (n.startsWith("crf_")) return [[0.36, 0.43, 0.41], [0.58, 0.49, 0.35], [0.86, 0.89, 0.91]];
  return [[0.46, 0.36, 0.26], [0.60, 0.44, 0.30], [0.90, 0.88, 0.84]];
}

function terrainLayers(material) {
  const sh = material.shaderHash;
  if (VEHICLE_GLASS_SHADERS.has(sh) || VEHICLE_LUT_SHADERS.has(sh) || WATER_SHADERS.has(sh) || !!defaultLutHash(material)) return null;
  const texs = material.textures || [];
  const usable = isUsable2D;
  const usableMask = (t) => usable(t) && t.sources[0].width >= 16;

  const uberParams = ["ce7b968e", "d883aa9e", "32b9df09", "3d10f3b7"];
  const uberRoles = ["Uber layer 0 diffuse", "Uber layer 1 diffuse", "Uber layer 2 diffuse", "Uber layer 3 diffuse"];
  const uLayers = uberParams.map((p, i) => texs.find((t) => usable(t) && (t.parameter === p || t.role === uberRoles[i])) || null);
  const uCount = uLayers.filter(Boolean).length;
  const firstU = uLayers.find(Boolean);
  if (firstU && uCount >= 2) {
    if (!uLayers[0]) uLayers[0] = firstU;
    const maskSlot = texs.find((t) => usableMask(t) && (t.parameter === "c075f9b6" || t.role === "Uber terrain lookup")) || null;
    const tintSlot = texs.find((t) => usableMask(t) && /SRGB/.test(t.sources[0].format) && (t.parameter === "3fb7964f" || t.role === "Uber terrain tint")) || null;
    return { slot: uLayers[0], texMode: 5, layers: uLayers, maskSlot, tintSlot, layerCount: uCount };
  }

  const stdParams = ["346394ea", "7d4f12e2", "7d072b80", ""];
  const stdRoles = ["Layer 0 diffuse", "Layer 1 diffuse", "Layer 2 diffuse", "Layer 3 diffuse"];
  const sLayers = stdRoles.map((r, i) => texs.find((t) => usable(t) && (t.role === r || (stdParams[i] && t.parameter === stdParams[i]))) || null);
  const sCount = sLayers.filter(Boolean).length;
  const firstS = sLayers.find(Boolean);
  if (firstS && sCount >= 2) {
    if (!sLayers[0]) sLayers[0] = firstS;
    const maskSlot = texs.find((t) => usableMask(t) && (t.parameter === "38ebcdb3" || t.role === "Layer blend control")) || null;
    return { slot: sLayers[0], texMode: 5, layers: sLayers, maskSlot, tintSlot: null, layerCount: sCount };
  }
  return null;
}

function defaultLutHash(material) {
  if (material.lutArray?.hash) return material.lutArray.hash;
  const slot = (material.textures || []).find((t) =>
    t.sources.length && (t.parameter === VEH_LUT_PARAM || (t.sources[0].dimension === "2D array" && t.sources[0].width <= 256)));
  return slot ? texHash(slot) : null;
}

function defaultSpecSlot(material) {
  const usable = (t) => isUsable2D(t) && t.sources[0].width >= 16;
  if (VEHICLE_LUT_SHADERS.has(material.shaderHash) || !!defaultLutHash(material)) {
    return material.textures.find((t) => (t.parameter === MAT_A0 || t.parameter === MAT_A1) && usable(t)) ||
      material.textures.find((t) => t.parameter === SPECULAR && usable(t)) || null;
  }
  return material.textures.find((t) => t.parameter === SPECULAR && usable(t)) || null;
}

function defaultSpec2Slot(material) {
  const usable = (t) => isUsable2D(t) && t.sources[0].width >= 16;
  return material.textures.find((t) => t.parameter === SPECULAR2 && usable(t)) || null;
}

function defaultDustSlot(material) {
  const usable = (t) => isUsable2D(t) && t.sources[0].width >= 16;
  return material.textures.find((t) => (t.parameter === DUST || t.parameter === DIRT_NOISE) && usable(t)) || null;
}

function defaultSlot(material, modelName = "", category = "") {
  if (VEHICLE_GLASS_SHADERS.has(material.shaderHash) || WATER_SHADERS.has(material.shaderHash)) return null;
  const usable = isUsable2D;
  if (material.shaderHash === DIST_MAP_SHADER) {
    const dm = material.textures.find((t) => t.parameter === DIST_MAP && usable(t));
    if (dm) return { slot: dm, texMode: 8 };
  }
  const terr = terrainLayers(material);
  if (terr) return terr;
  const lutHash = defaultLutHash(material);
  const isVehLut = VEHICLE_LUT_SHADERS.has(material.shaderHash) || !!lutHash;
  const base = material.textures.find((t) => t.parameter === BASE_COLOR && usable(t));
  if (base) {
    if (material.shaderHash === EYE_SHADER) return { slot: base, texMode: 4 };
    if (material.shaderHash === HEAD_SHADER && !/SRGB/.test(base.sources[0].format)) {
      return { slot: base, texMode: 3 };
    }
    if (category === "Animals" && !/SRGB/.test(base.sources[0].format)) {
      return { slot: base, texMode: 2, palette: animalMaskPalette(modelName) };
    }
    if (isVehLut && !/SRGB/.test(base.sources[0].format)) {
      return { slot: base, texMode: lutHash ? 6 : 1, lutHash };
    }
    return { slot: base, texMode: 0 };
  }
  // GTA VI weapon / weapon_muv_alpha procedural PBR shader (no baked sRGB albedo).
  if (WEAPON_SHADERS.has(material.shaderHash)) {
    const spec = material.textures.find((t) => t.parameter === SPECULAR && usable(t));
    return { slot: spec || null, texMode: 7 };
  }
  // GTA VI standard_vehicle_2lyr* procedural LUT shader (19df25d7 8-slice 128x256 LUT).
  const named = material.textures.find((t) => usable(t) && DIFFUSE_ROLES.has(t.role));
  if (named) {
    if (isVehLut && !/SRGB/.test(named.sources[0].format)) {
      return { slot: named, texMode: lutHash ? 6 : 1, lutHash };
    }
    return { slot: named, texMode: 0 };
  }
  const isVehPaint = VEHICLE_PAINT_SHADERS.has(material.shaderHash) || isVehLut;
  const color = material.textures.filter((t) => usable(t) && t.parameter !== OVERLAY && t.parameter !== DIRT &&
    /SRGB/.test(t.sources[0].format) && (!isVehPaint || (t.sources[0].width > 4 && t.sources[0].height > 4)));
  if (color.length) {
    return { slot: color.sort((a, b) => b.sources[0].width * b.sources[0].height - a.sources[0].width * a.sources[0].height)[0], texMode: 0 };
  }
  if (isVehLut && lutHash) {
    return { slot: null, texMode: 6, lutHash };
  }
  // Last-resort sRGB Secondary diffuse (0df47048) when Base color is absent from local packs.
  const overlay = material.textures.find((t) => t.parameter === OVERLAY && usable(t) &&
    /SRGB/.test(t.sources[0].format) && t.sources[0].width > 4 && t.sources[0].height > 4);
  if (overlay && !isVehPaint) {
    return { slot: overlay, texMode: 0 };
  }
  const spec = material.textures.find((t) => t.parameter === SPECULAR && usable(t) && t.sources[0].width >= 32);
  if (spec && material.shaderHash !== HEAD_SHADER && !isVehPaint &&
      (PROCEDURAL_MUV_SHADERS.has(material.shaderHash) || !material.textures.some((t) => t.parameter === BASE_COLOR && usable(t)))) {
    return { slot: spec, texMode: 1 };
  }
  return null;
}

function defaultNormalSlot(material) {
  if (material.shaderHash === EYE_SHADER) return null;
  const usable = (t) => isUsable2D(t) && t.sources[0].width >= 8;
  const bump = material.textures.find((t) => t.parameter === BUMP && usable(t));
  if (bump) return bump;
  const named = material.textures.find((t) => usable(t) && NORMAL_ROLES.has(t.role));
  if (named) return named;
  return material.textures.find((t) => usable(t) && /BC5/.test(t.sources[0].format)) || null;
}

const PASS_TYPE_MAP = { opaque: 0, cutout: 1, decal: 2, alpha: 3, water: 4, stencil: 0 };

function materialState(m, modelName, category) {
  const rpass = WATER_SHADERS.has(m.shaderHash) ? "water" : (m.renderPass || (m.shaderHash === WATER_STENCIL_SHADER ? "stencil" : "opaque"));
  const pick = defaultSlot(m, modelName, category);
  const visible = m.shaderHash !== WATER_STENCIL_SHADER && rpass !== "stencil" &&
    !(rpass === "decal" && !pick && !m.tintColor);
  const nslot = defaultNormalSlot(m);
  const specSlot = defaultSpecSlot(m);
  const spec2Slot = defaultSpec2Slot(m);
  const isVehLut = VEHICLE_LUT_SHADERS.has(m.shaderHash) || pick?.texMode === 6;
  const dustSlot = isVehLut ? defaultDustSlot(m) : null;
  const nm = (modelName || "").toLowerCase();
  const isRim = VEHICLE_WHEEL_SHADERS.has(m.shaderHash) || nm.startsWith("rim_");
  const isTyre = VEHICLE_TYRE_SHADERS.has(m.shaderHash) || nm.startsWith("tyre_");
  const isHumanSkin = SKIN_SHADERS.has(m.shaderHash) && !/(?:fsk|msk)\d|gore|corpse|skeleton/.test(nm);
  const matMode = VEHICLE_GLASS_SHADERS.has(m.shaderHash) ? 2 :
    (((VEHICLE_PAINT_SHADERS.has(m.shaderHash) || pick?.texMode === 6) && (!pick || pick.texMode === 0 || pick.texMode === 6)) || isRim ? 1 :
      (isHumanSkin ? 3 : 0));
  const passType = PASS_TYPE_MAP[rpass] ?? 0;
  const fmt = pick?.slot?.sources?.[0]?.format || "";
  const lumaAlpha = (rpass === "alpha" && /BC1|BC6H|R11G11B10/.test(fmt))
    || ((rpass === "cutout" || rpass === "alpha" || rpass === "decal") && /^(?:A8|R8|BC4)_/.test(fmt));
  let tintColor = m.tintColor || null;
  if (!tintColor) {
    if (isTyre && (!pick || pick.texMode === 1 || pick.texMode === 6)) tintColor = [0.15, 0.15, 0.16];
    else if (isRim && (!pick || pick.texMode === 1 || pick.texMode === 6)) tintColor = [0.76, 0.78, 0.82];
    else if ((category === "Vehicles" && VEHICLE_PAINT_SHADERS.has(m.shaderHash)) || pick?.texMode === 6) {
      if (!pick || pick.texMode === 6) tintColor = vehiclePaintColor(modelName);
      else if (pick.texMode === 1) tintColor = [0.44, 0.46, 0.50];
    }
  }
  return { visible, pick, nslot, specSlot, spec2Slot, dustSlot, matMode, passType, rpass, lumaAlpha, tintColor };
}

function loadTerrainLayers(materialIndex, spec, layer = "main", modelId = 0, guardFn = null) {
  if (!spec || !spec.layers) return;
  const sz = curTexRes();
  const jobs = [
    ["layer1Tex", texHash(spec.layers[1]), sz],
    ["layer2Tex", texHash(spec.layers[2]), sz],
    ["layer3Tex", texHash(spec.layers[3]), sz],
    ["maskTex", texHash(spec.maskSlot), sz],
    ["tintTex", texHash(spec.tintSlot), sz],
  ];
  for (const [field, h, lim] of jobs) {
    if (!h) continue;
    textureImage(h, lim, modelId).then((img) => {
      if (!guardFn || guardFn()) S.view.setExtraTexture(materialIndex, field, img, layer);
    }).catch(() => {});
  }
}

function loadPbrAuxTextures(materialIndex, st, layer, modelId, guardFn) {
  const sz = curTexRes();
  const jobs = [
    ["specTex", st.specSlot ? texHash(st.specSlot) : null, sz],
    ["spec2Tex", st.spec2Slot ? texHash(st.spec2Slot) : null, sz],
    ["dustTex", st.dustSlot ? texHash(st.dustSlot) : null, sz],
  ];
  for (const [field, h, lim] of jobs) {
    if (!h) continue;
    textureImage(h, lim, modelId).then((img) => {
      if (!guardFn || guardFn()) S.view.setExtraTexture(materialIndex, field, img, layer);
    }).catch(() => {});
  }
}

function applyMaterials(item) {
  let hasVehLut = false, lutHashFound = null, hasWeapon = false;
  for (const m of item.materials) {
    const st = materialState(m, item.name, item.category);
    const useMuv = (m.isMuv || m.muvPreset) && (!st.pick || st.pick.texMode === 1);
    const muvPre = useMuv ? (S.muvPresets.get(item.id) || item.clothingPresets?.defaultPreset || m.muvPreset || null) : null;
    m.color = palette(m.index);
    m.visible = st.visible;
    m.renderPass = st.rpass;
    m.passType = st.passType;
    m.lumaAlpha = st.lumaAlpha;
    m.terrainSpec = st.pick?.texMode === 5 ? st.pick : null;
    m.activeMuvPreset = muvPre;
    m.active = muvPre ? null : (st.pick?.slot ? texHash(st.pick.slot) : null);
    m.texMode = muvPre ? 0 : (st.pick ? st.pick.texMode : 0);
    m.lutHash = st.pick?.lutHash || null;
    m.matMode = st.matMode;
    m.palette = st.pick?.palette ? st.pick.palette.map((z) => [...z]) : null;
    m.defaultPalette = m.palette ? m.palette.map((z) => [...z]) : null;
    m.tintColor = muvPre ? null : (st.tintColor ? [...st.tintColor] : null);
    m.defaultTint = m.tintColor ? [...m.tintColor] : null;
    m.tintOverride = false;
    m.tintRow = 0;
    m.activeNormal = st.nslot ? texHash(st.nslot) : null;
    if (m.texMode === 6) { hasVehLut = true; if (m.lutHash) lutHashFound = m.lutHash; }
    if (m.texMode === 7) hasWeapon = true;
    S.view.setMaterial(m.index, {
      visible: m.visible, color: m.color, texMode: m.texMode, matMode: m.matMode,
      passType: m.passType, lumaAlpha: m.lumaAlpha, lutHash: m.lutHash,
      palette: m.palette, tintColor: m.tintColor, tintOverride: false,
      tintRow: 0, tintRowLen: m.tintPalette?.rowLength || 1,
    });
    if (muvPre) {
      S.muvPresets.set(item.id, muvPre);
      loadMuvTexture(m.index, item.id, muvPre, "main");
    } else if (m.active) {
      loadTexture(m.index, m.active, item.id);
    }
    if (m.texMode === 5 && m.terrainSpec) {
      loadTerrainLayers(m.index, m.terrainSpec, "main", item.id, () => S.item === item && m.texMode === 5);
    }
    if (m.activeNormal) loadNormalTexture(m.index, m.activeNormal, item.id);
    loadPbrAuxTextures(m.index, st, "main", item.id, () => S.item === item);
    if (m.tintPalette?.hash) loadTintPalette(m, item);
  }
  if (hasVehLut) {
    const pre = vehicleDefaultPreset(item.name);
    S.view.setProcedural({
      vehPrimary: pre.primary, vehSecondary: pre.secondary, vehPearlCol: pre.pearl,
      vehPaintRow1: pre.row1, vehPaintRow2: pre.row2, vehPaintSlice: pre.slice,
      vehTwoTone: pre.twoTone || 0,
    });
    if (lutHashFound) {
      ensureLutArray(lutHashFound).then(() => {
        if (S.item === item) renderMaterials(item);
      });
    }
  }
  if (!hasWeapon && S.activeLivery) {
    S.activeLivery = null;
    S.view.setSharedLivery(null);
  }
  renderMaterials(item);
}

async function loadTintPalette(m, item) {
  try {
    const img = await textureImage(m.tintPalette.hash, 4096, item.id);
    if (S.item !== item) return;
    m._tintPalBmp = img;
    S.view.setExtraTexture(m.index, "tintPalTex", img, "main");
    drawTintStrip(m);
  } catch (_) {}
}

function drawTintStrip(m) {
  const cv = document.querySelector(`canvas[data-tstrip="${m.index}"]`);
  const bmp = m._tintPalBmp;
  if (!cv || !bmp || !m.tintPalette) return;
  const rowLen = Math.max(1, m.tintPalette.rowLength || bmp.width);
  const startX = Math.min(bmp.width - 1, (m.tintRow || 0) * rowLen);
  const drawW = Math.max(1, Math.min(rowLen, bmp.width - startX));
  const ctx = cv.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.drawImage(bmp, startX, 0, drawW, 1, 0, 0, cv.width, cv.height);
}

async function muvTextureImage(modelId, presetHex, maxRes, matIdx = 0) {
  const res = Math.min(1024, Math.max(256, maxRes || 512));
  const key = `muv:${modelId}:${matIdx}:${presetHex}@${res}`;
  if (!S.textures.has(key)) {
    S.textures.set(key, (async () => {
      const url = `/api/models/muv-texture?id=${modelId}&preset=${encodeURIComponent(presetHex)}&max=${res}&mat=${matIdx}`;
      const r = await fetch(url);
      if (!r.ok) {
        S.textures.delete(key);
        throw Error((await r.json()).error || "MUV bake failed");
      }
      return createImageBitmap(await r.blob());
    })());
  }
  return S.textures.get(key);
}

async function loadMuvTexture(materialIndex, modelId, presetHex, layer = "main") {
  const item = S.item;
  const reqRes = curTexRes();
  try {
    const image = await muvTextureImage(modelId, presetHex, reqRes, materialIndex);
    if (S.item !== item || curTexRes() !== reqRes) return;
    if (layer === "main") {
      const m = item.materials.find((x) => x.index === materialIndex);
      if (!m || m.activeMuvPreset !== presetHex) return;
    } else if (!S.parts.has(modelId) || S.muvPresets.get(modelId) !== presetHex) {
      return;
    }
    S.view.setTexture(materialIndex, image, layer);
  } catch (e) {
    status(`Clothing preset ${presetHex}: ${e.message}`);
  }
}

function resolveMuvPresetForPart(part) {
  if (!part) return null;
  if (S.muvPresets?.has(part.id)) return S.muvPresets.get(part.id);
  const pnm = (part.name || "").toLowerCase();
  const charPre = S.item?.characterAssembly?.clothingPresets?.[pnm];
  if (charPre) return charPre;
  return part.clothingPresets?.defaultPreset || part.materials?.find((m) => m.muvPreset)?.muvPreset || null;
}

function swatchDotsHtml(swatches, size = 10) {
  const list = (swatches || []).slice(0, 4);
  if (!list.length) return "";
  return `<span class="muv-dots">${list.map((c) =>
    `<span class="muv-dot" style="width:${size}px;height:${size}px;background:${esc(c)}"></span>`
  ).join("")}</span>`;
}

async function textureImage(hash, max, modelId = 0) {
  const mid = modelId || S.item?.id || 0;
  const key = hash + "@" + max;
  if (!S.textures.has(key)) {
    S.textures.set(key, (async () => {
      const url = `/api/models/texture?hash=${hash}&max=${max}` + (mid ? `&model=${mid}` : "");
      const r = await fetch(url);
      if (!r.ok) {
        S.textures.delete(key);
        throw Error((await r.json()).error || "Texture unavailable");
      }
      return createImageBitmap(await r.blob());
    })());
  }
  return S.textures.get(key);
}
function applyImageHeuristics(m, image, layer = "main", manual = false) {
  if (!image || !window.ModelView?.analyzeImage) return true;
  const info = ModelView.analyzeImage(image);
  if (info.isMagenta && !manual) {
    if (m.renderPass === "decal") {
      m.visible = false;
      m.magentaHidden = true;
      S.view.setMaterial(m.index, { visible: false }, layer);
      return false;
    }
    m.magentaStripped = true;
    S.view.setTexture(m.index, null, layer);
    return false;
  }
  if (info.isCutout && (m.renderPass === "alpha" || m.renderPass === "decal") && !m.lumaAlpha && (m.texMode || 0) === 0) {
    m.passType = 1;
    m.promotedCutout = true;
    S.view.setMaterial(m.index, { passType: 1 }, layer);
  }
  return true;
}

async function loadTexture(materialIndex, hash, modelId = 0, manual = false) {
  const item = S.item;
  const reqRes = curTexRes();
  try {
    const image = await textureImage(hash, reqRes, modelId || item?.id || 0);
    const m = item.materials.find((x) => x.index === materialIndex);
    if (S.item !== item || !m || m.active !== hash || curTexRes() !== reqRes) return;
    const prevVis = m.visible, prevCut = m.promotedCutout, prevStrip = m.magentaStripped;
    if (applyImageHeuristics(m, image, "main", manual)) {
      S.view.setTexture(materialIndex, image);
    }
    if (m.visible !== prevVis || m.promotedCutout !== prevCut || m.magentaStripped !== prevStrip) {
      renderMaterials(item);
    }
  } catch (e) {
    status(`Texture ${hash}: ${e.message}`);
  }
}
async function loadNormalTexture(materialIndex, hash, modelId = 0) {
  const item = S.item;
  const reqRes = curTexRes();
  try {
    const image = await textureImage(hash, reqRes, modelId || item?.id || 0);
    const m = item.materials.find((x) => x.index === materialIndex);
    if (S.item !== item || !m || m.activeNormal !== hash || curTexRes() !== reqRes) return;
    S.view.setNormalTexture(materialIndex, image);
  } catch (_) {}
}

function loadPartMaterials(id, part, layer) {
  let partHasVehLut = false, partHasWeapon = false;
  const reqRes = curTexRes();
  const pnm = (part.name || "").toLowerCase();
  const at = S.anatomyTex || {};
  const pov = S.partOverrides?.get(id) || S.partOverrides?.get(String(id)) || S.partOverrides?.get(pnm) || null;
  let ovAlbedo = null, ovNormal = null, ovSkinGain = null, isEyePart = false;
  if (pnm.startsWith("hand_")) {
    ovAlbedo = at.upperAlbedo || null;
    ovNormal = at.upperNormal || null;
    if (S.skinHarmonize !== false && at.upperGain) ovSkinGain = at.upperGain;
  } else if (pnm.startsWith("bodynipplesgen_")) {
    ovAlbedo = at.upperAlbedo || null;
    if (S.skinHarmonize !== false && at.upperGain) ovSkinGain = at.upperGain;
  } else if (pnm.startsWith("feet_")) {
    ovAlbedo = at.lowerAlbedo || null;
    ovNormal = at.lowerNormal || null;
    if (S.skinHarmonize !== false && (at.lowerGain || at.upperGain)) ovSkinGain = at.lowerGain || at.upperGain;
  } else if (pnm.startsWith("bodygenitalsgen_")) {
    ovAlbedo = at.genitalAlbedo || (pnm.includes("_fr1_") ? at.lowerAlbedo : null) || null;
    ovNormal = at.genitalNormal || (pnm.includes("_fr1_") ? at.lowerNormal : null) || null;
    if (S.skinHarmonize !== false && (at.lowerGain || at.upperGain)) ovSkinGain = at.lowerGain || at.upperGain;
  } else if (/_eyes(?:_|$)/i.test(pnm)) {
    isEyePart = true;
    if (at.eyeAlbedo) ovAlbedo = at.eyeAlbedo;
  } else if (pov) {
    if (pov.albedo) ovAlbedo = pov.albedo;
    if (pov.normal) ovNormal = pov.normal;
  }
  const partMuvPre = S.muvPresets?.get(id) || pov?.muvPreset || resolveMuvPresetForPart(part);
  const povTintRgb = (pov?.tintColor && typeof pov.tintColor === "string" && pov.tintColor.startsWith("#") && pov.tintColor.length >= 7)
    ? hexToRgb(pov.tintColor) : null;
  for (const m of part.materials) {
    const st = materialState(m, part.name, part.category);
    const useMuv = !ovAlbedo && !!partMuvPre && (m.isMuv || m.muvPreset) && (!st.pick || st.pick.texMode === 1);
    const tmode = isEyePart && ovAlbedo ? 4 : ((ovAlbedo || useMuv) ? 0 : (st.pick ? st.pick.texMode : 0));
    const lutHash = st.pick?.lutHash || null;
    const pm = { index: m.index, renderPass: st.rpass, passType: st.passType, lumaAlpha: st.lumaAlpha, texMode: tmode, visible: st.visible };
    if (tmode === 6) partHasVehLut = true;
    if (tmode === 7) partHasWeapon = true;
    const finalTint = useMuv ? null : (povTintRgb && (!ovAlbedo || st.pick?.texMode === 1) ? povTintRgb : st.tintColor);
    S.view.setMaterial(m.index, {
      visible: st.visible, color: palette(m.index), texMode: tmode,
      matMode: st.matMode, passType: st.passType, lumaAlpha: st.lumaAlpha,
      lutHash, palette: st.pick?.palette || null, tintColor: finalTint,
      tintRow: pov?.tintRow != null ? (Number(pov.tintRow) || 0) : 0,
      tintRowLen: m.tintPalette?.rowLength || 1,
      skinGain: ovSkinGain || [1, 1, 1],
    }, layer);
    if (useMuv) {
      S.muvPresets.set(id, partMuvPre);
      loadMuvTexture(m.index, id, partMuvPre, layer);
    } else {
      const h = ovAlbedo || (st.pick ? texHash(st.pick.slot) : null);
      if (h) textureImage(h, reqRes, id).then((img) => {
        if (S.parts.has(id) && curTexRes() === reqRes && applyImageHeuristics(pm, img, layer, false)) S.view.setTexture(m.index, img, layer);
      }).catch(() => {});
    }
    const nh = ovNormal || (st.nslot ? texHash(st.nslot) : null);
    if (tmode === 5) loadTerrainLayers(m.index, st.pick, layer, id, () => S.parts.has(id) && curTexRes() === reqRes);
    if (nh) textureImage(nh, reqRes, id).then((img) => { if (S.parts.has(id) && curTexRes() === reqRes) S.view.setNormalTexture(m.index, img, layer); }).catch(() => {});
    loadPbrAuxTextures(m.index, st, layer, id, () => S.parts.has(id) && curTexRes() === reqRes);
    if (lutHash) ensureLutArray(lutHash);
    if (m.tintPalette?.hash) textureImage(m.tintPalette.hash, 4096, id).then((img) => {
      if (S.parts.has(id)) S.view.setExtraTexture(m.index, "tintPalTex", img, layer);
    }).catch(() => {});
  }
  return { partHasVehLut, partHasWeapon };
}

function reloadAllTextures() {
  const item = S.item;
  if (!item) return;
  const reqRes = curTexRes();
  for (const m of item.materials || []) {
    const st = materialState(m, item.name, item.category);
    if (m.activeMuvPreset) loadMuvTexture(m.index, item.id, m.activeMuvPreset, "main");
    else if (m.active) loadTexture(m.index, m.active, item.id);
    if (m.texMode === 5 && m.terrainSpec) {
      loadTerrainLayers(m.index, m.terrainSpec, "main", item.id, () => S.item === item && m.texMode === 5 && curTexRes() === reqRes);
    }
    if (m.activeNormal) loadNormalTexture(m.index, m.activeNormal, item.id);
    loadPbrAuxTextures(m.index, st, "main", item.id, () => S.item === item && curTexRes() === reqRes);
  }
  for (const [id, part] of S.partMeta.entries()) {
    if (S.parts.has(id) && part) loadPartMaterials(id, part, "part" + id);
  }
  if (S.wheels?.enabled && item.wheelSetup) applyWheels(item, false);
  if (S.activeLivery) {
    textureImage(S.activeLivery, reqRes, 0).then((img) => {
      if (S.item === item && curTexRes() === reqRes) S.view.setSharedLivery(img);
    }).catch(() => {});
  }
}

// Assembly ----------------------------------------------------------------------------------------
function syncBodyCullSeams() {
  if (!S.view?.cullOverlap) return;
  const active = [];
  if (S.item) active.push({ layer: "main", name: (S.item.base_name || S.item.name || "").toLowerCase() });
  for (const [pid, meta] of S.partMeta.entries()) {
    if (S.parts.has(pid) && meta) active.push({ layer: "part" + pid, name: (meta.base_name || meta.name || "").toLowerCase() });
  }
  const hands = active.filter((x) => /^hand_[a-z0-9]+_000_nude_cull$/.test(x.name));
  const feet = active.filter((x) => /^feet_[a-z0-9]+_000_nude_cull$/.test(x.name));
  const nipples = active.filter((x) => x.name.startsWith("bodynipplesgen_"));
  const fr1Genitals = active.filter((x) => x.name.startsWith("bodygenitalsgen_fr1_"));
  for (const h of hands) {
    const refs = [];
    // Keep hand's upper-waist triangles (v >= 0.85, bottom of upper-torso UV atlas) and cull only
    // its lower-waist cross-seam triangles (v <= 0.15, which carry feet's pelvis UV coordinates).
    for (const f of feet) refs.push({ layer: f.layer, tol: 0.015, vMax: 0.15 });
    for (const n of nipples) refs.push({ layer: n.layer, tol: 0.0025 });
    S.view.cullOverlap(h.layer, refs);
  }
  for (const f of feet) {
    const refs = [];
    // Keep feet's lower-waist triangles (v <= 0.15, top of lower-pelvis UV atlas) and cull its
    // upper-waist cross-seam triangles (v >= 0.85, which would sample feet/toes across the waist).
    for (const h of hands) refs.push({ layer: h.layer, tol: 0.015, vMin: 0.85 });
    for (const g of fr1Genitals) refs.push({ layer: g.layer, tol: 0.0016 });
    S.view.cullOverlap(f.layer, refs);
  }
}

async function togglePart(id, on, opts = {}) {
  const item = S.item, layer = "part" + id;
  const setBoxes = (v) => {
    $("inspect")?.querySelectorAll(`[data-part="${id}"]`).forEach((b) => { b.checked = v; });
    const gSel = $("anatGenital");
    const gens = item?.characterAssembly?.anatomy?.genitals;
    if (gSel && gens?.length) {
      const cur = gens.find((g) => S.parts?.has(g.id));
      gSel.value = cur ? String(cur.id) : "0";
    }
  };
  if (!on) {
    S.parts.delete(id);
    S.partMeta.delete(id);
    S.view.removeLayer(layer);
    setBoxes(false);
    syncBodyCullSeams();
    renderEquippedClothingStyles();
    updatePartStats();
    return;
  }
  S.parts.set(id, null);
  setBoxes(true);
  try {
    const [part, r] = await Promise.all([api("item", { id }), fetch("/api/models/glb?id=" + id)]);
    if (!r.ok) throw Error((await r.json()).error || "Decode failed");
    if (S.item !== item || !S.parts.has(id)) return;
    const info = S.view.load(await r.arrayBuffer(), layer, true);
    if (opts.skipStub && info.atRootStub) {
      S.parts.delete(id);
      S.partMeta.delete(id);
      S.view.removeLayer(layer);
      setBoxes(false);
      updatePartStats();
      return;
    }
    S.parts.set(id, info);
    S.partMeta.set(id, part);
    if (S.rig?.info) attachPartSkin(id, layer, 0);
    const { partHasVehLut, partHasWeapon } = loadPartMaterials(id, part, layer);
    syncBodyCullSeams();
    renderEquippedClothingStyles();
    if (partHasVehLut && !item._hasVehLutPart) {
      item._hasVehLutPart = true;
      renderMaterials(item);
    }
    if (partHasWeapon && !item._hasWeaponPart) {
      item._hasWeaponPart = true;
      renderMaterials(item);
    }
    updatePartStats();
  } catch (e) {
    S.parts.delete(id);
    setBoxes(false);
    status(`Part ${id}: ${e.message}`);
  }
}

async function assembleCharacter(item, opts = {}) {
  const ca = item?.characterAssembly;
  if (!ca?.parts?.length) return;
  const nudeOnly = Boolean(opts.nudeOnly);
  const tasks = [];
  for (const p of ca.parts) {
    if (p.id === item.id) continue;
    if (nudeOnly && p.slot === "Clothing") {
      if (S.parts.has(p.id)) togglePart(p.id, false);
      continue;
    }
    if (!S.parts.has(p.id)) tasks.push(togglePart(p.id, true, { skipStub: true }));
  }
  if (tasks.length) {
    await Promise.all(tasks);
    if (S.item === item) S.view.fit();
  }
}

// Hair & Grooming (Strands + Poly Fallbacks + 48 GTA VI RAGE 9 Colors) ----------------------------
function fetchGroomCatalogCached(rig) {
  const rk = (rig || "fr1").toLowerCase();
  if (!_groomCatalogCache.has(rk)) {
    _groomCatalogCache.set(rk, api("groom-catalog", { rig: rk }).catch((e) => {
      _groomCatalogCache.delete(rk);
      throw e;
    }));
  }
  return _groomCatalogCache.get(rk);
}

async function fetchGroomStrandsCached(url) {
  if (_groomStrandCache.has(url)) return _groomStrandCache.get(url);
  const p = fetch(url).then((r) => {
    if (!r.ok) throw Error(`Groom strands failed (${r.status})`);
    return r.arrayBuffer();
  }).catch((e) => {
    _groomStrandCache.delete(url);
    throw e;
  });
  _groomStrandCache.set(url, p);
  if (_groomStrandCache.size > 64) {
    const first = _groomStrandCache.keys().next().value;
    _groomStrandCache.delete(first);
  }
  return p;
}

const GROOM_SLOTS_UI = [
  ["hair", "Scalp Hair", 4000],
  ["brows", "Eyebrows", 1200],
  ["lashes", "Eyelashes", 800],
  ["beard", "Beard / Facial Hair", 3000],
  ["bodyhair", "Body Hair", 2500],
];

async function mountGroomSlot(slot, entry, opts = {}) {
  const item = S.item;
  const gs = S.groomState;
  if (!item || !gs || gs.itemId !== item.id) return;
  const tok = (gs.slotTok[slot] = (gs.slotTok[slot] || 0) + 1);
  // Remove previous poly mesh for this slot if one was mounted
  const prevPoly = gs.polyPartIds[slot];
  if (prevPoly && S.parts?.has(prevPoly)) {
    delete gs.polyPartIds[slot];
    togglePart(prevPoly, false);
  }
  S.view.removeGroomStrands?.(slot);
  gs.active[slot] = entry || null;
  if (!entry) return;
  const colorSpec = gs.color || { tipHex: "#4a3322", rootHex: "#2d2016" };
  const gids = Array.isArray(entry.groomIds) ? entry.groomIds.filter((n) => Number.isFinite(Number(n))) : [];
  if (gids.length) {
    const slotDef = GROOM_SLOTS_UI.find((x) => x[0] === slot);
    const baseMax = slot === "hair" ? (gs.maxStrands || 4000) : (slotDef ? slotDef[2] : 2500);
    const params = new URLSearchParams({
      ids: gids.join(","),
      slot,
      max: String(opts.maxStrands || baseMax),
      lod: "0",
    });
    if (entry.rig) params.set("groomRig", entry.rig);
    if (gs.rig) params.set("targetRig", gs.rig);
    const url = "/api/models/groom-strands?" + params;
    try {
      const buf = await fetchGroomStrandsCached(url);
      if (S.item !== item || S.groomState !== gs || gs.slotTok[slot] !== tok) return;
      S.view.setGroomStrands?.(slot, buf, colorSpec);
    } catch (e) {
      if (S.item === item && entry.polyMeshModelId && !S.parts?.has(entry.polyMeshModelId)) {
        gs.polyPartIds[slot] = entry.polyMeshModelId;
        togglePart(entry.polyMeshModelId, true);
      }
    }
  } else if (entry.polyMeshModelId) {
    gs.polyPartIds[slot] = entry.polyMeshModelId;
    await togglePart(entry.polyMeshModelId, true);
  }
}

async function initCharacterGroom(item, openTok) {
  const ca = item?.characterAssembly;
  if (!ca || ca.kind === "Animal") return;
  const rig = (ca.body || item.pedArchetype?.bodyRig || "fr1").toLowerCase();
  const dg = ca.defaultGroom || null;
  const defCol = dg?.defaultColor || { id: "rich_brunette", label: "Rich Brunette", tipHex: "#553b2a", rootHex: "#34271f", tipRgb: [85, 59, 42], rootRgb: [52, 39, 31] };
  const defaults = dg?.defaults || {};
  const variants = dg?.variants || {};
  const gs = {
    itemId: item.id,
    rig,
    color: { ...defCol },
    defaultColor: { ...defCol },
    defaults,
    variants,
    active: {
      hair: defaults.hair || null,
      brows: defaults.brows || null,
      lashes: defaults.lashes || null,
      beard: defaults.beard || null,
      bodyhair: Array.isArray(defaults.bodyhair) ? (defaults.bodyhair[0] || null) : (defaults.bodyhair || null),
    },
    catalog: null,
    maxStrands: 4000,
    slotTok: {},
    polyPartIds: {},
  };
  S.groomState = gs;
  renderGroomSection();

  // Auto-mount default scalp hair, eyebrows, eyelashes, and beard immediately in parallel!
  const mountJobs = [];
  for (const slot of ["hair", "brows", "lashes", "beard"]) {
    if (gs.active[slot]) mountJobs.push(mountGroomSlot(slot, gs.active[slot]));
  }
  const catJob = fetchGroomCatalogCached(rig).then((cat) => {
    if (openTok !== S.open || S.item !== item || S.groomState !== gs) return;
    gs.catalog = cat;
    renderGroomSection();
  }).catch(() => {});

  await Promise.all([...mountJobs, catJob]);
  if (openTok === S.open && S.item === item && S.groomState === gs) {
    renderGroomSection();
  }
}

function renderGroomSection() {
  const host = $("groomBody");
  const gs = S.groomState;
  if (!host || !gs || !S.item || gs.itemId !== S.item.id) return;
  const cat = gs.catalog;
  const colors = cat?.colors || [];
  const curCol = gs.color || {};
  const tipHex = curCol.tipHex || "#553b2a";
  const rootHex = curCol.rootHex || "#34271f";
  const isMaleRig = gs.rig.startsWith("m");

  const slotHtml = [];
  for (const [slot, label] of GROOM_SLOTS_UI) {
    const natList = Array.isArray(gs.variants?.[slot]) ? gs.variants[slot] : [];
    const catList = Array.isArray(cat?.[slot]) ? cat[slot] : [];
    if (slot === "beard" && !isMaleRig && !natList.length && !catList.length) continue;
    const cur = gs.active[slot];
    const curKey = cur ? (cur.drawable || (cur.groomIds || []).join(",")) : "";
    const seen = new Set();
    const entryMap = new Map();
    const makeKey = (e) => e.drawable || (e.groomIds || []).join(",");
    const opts = [`<option value="" ${!curKey ? "selected" : ""}>None (Bald / Hidden)</option>`];
    if (cur && curKey && !natList.some((e) => makeKey(e) === curKey) && !catList.some((e) => makeKey(e) === curKey)) {
      seen.add(curKey);
      entryMap.set(curKey, cur);
      opts.push(`<option value="${esc(curKey)}" selected>★ Default: ${esc(cur.label || cur.drawable)}</option>`);
    }
    if (natList.length) {
      const natOpts = [];
      for (const e of natList) {
        const k = makeKey(e);
        if (!k || seen.has(k)) continue;
        seen.add(k);
        entryMap.set(k, e);
        const strands = e.strandCount ? ` · ${fmtN(e.strandCount)} str` : (e.hasPolyMesh ? " · poly" : "");
        natOpts.push(`<option value="${esc(k)}" ${k === curKey ? "selected" : ""}>★ ${esc(e.label || e.drawable)}${strands}</option>`);
      }
      if (natOpts.length) opts.push(`<optgroup label="Character Native Variants (${natOpts.length})">${natOpts.join("")}</optgroup>`);
    }
    if (catList.length) {
      const catOpts = [];
      for (const e of catList) {
        const k = makeKey(e);
        if (!k || seen.has(k)) continue;
        seen.add(k);
        entryMap.set(k, e);
        const strands = e.strandCount ? ` · ${fmtN(e.strandCount)}` : "";
        catOpts.push(`<option value="${esc(k)}" ${k === curKey ? "selected" : ""}>${esc(e.label || e.drawable)}${strands}</option>`);
      }
      if (catOpts.length) opts.push(`<optgroup label="All ${esc(gs.rig)} Rig Styles (${catOpts.length})">${catOpts.join("")}</optgroup>`);
    }
    gs._slotEntryMaps = gs._slotEntryMaps || {};
    gs._slotEntryMaps[slot] = entryMap;
    const totalAvail = seen.size;
    slotHtml.push(`<label><span>${esc(label)} (${totalAvail}${cat ? "" : "…"})</span><select data-groom-slot="${esc(slot)}">${opts.join("")}</select></label>`);
  }

  // Group 48 GTA VI colors by family
  const colorGroups = new Map();
  for (const c of colors) {
    const g = c.group || "Other";
    if (!colorGroups.has(g)) colorGroups.set(g, []);
    colorGroups.get(g).push(c);
  }
  let swatchGridHtml = "";
  if (colorGroups.size) {
    const rows = [];
    for (const [grp, list] of colorGroups.entries()) {
      const btns = list.map((c) => {
        const active = curCol.id === c.id;
        return `<button type="button" data-gcol="${esc(c.id)}" title="${esc(c.label)} (Root ${esc(c.rootHex)} → Tip ${esc(c.tipHex)})" style="width:22px;height:22px;border-radius:50%;padding:0;border:${active ? "2px solid #fff;box-shadow:0 0 0 1px #58a6ff" : "1px solid rgba(255,255,255,0.28)"};background:linear-gradient(135deg,${esc(c.rootHex)} 0%,${esc(c.tipHex)} 100%);cursor:pointer"></button>`;
      }).join("");
      rows.push(`<div style="margin-top:4px"><div style="font-size:10px;opacity:.7;margin-bottom:3px">${esc(grp)}</div><div style="display:flex;flex-wrap:wrap;gap:4px">${btns}</div></div>`);
    }
    swatchGridHtml = rows.join("");
  } else {
    swatchGridHtml = `<p class="note">Loading 48 RAGE 9 hair colors…</p>`;
  }

  host.innerHTML = `
    <p class="note">RAGE 9 <code>.grm</code> strand grooming &amp; paired root→tip palette (<code>00011dab3000.pso</code>) for rig <code>${esc(gs.rig)}</code>. Active color: <span style="display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:-1px;background:linear-gradient(135deg,${esc(rootHex)},${esc(tipHex)});border:1px solid rgba(255,255,255,.4)"></span> <b>${esc(curCol.label || "Custom")}</b>.</p>
    <div class="anat-grid">${slotHtml.join("")}</div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px">
      <label style="font-size:11px;display:inline-flex;align-items:center;gap:4px">Root <input type="color" id="groomRootCol" value="${esc(rootHex)}"></label>
      <label style="font-size:11px;display:inline-flex;align-items:center;gap:4px">Tip <input type="color" id="groomTipCol" value="${esc(tipHex)}"></label>
      <label style="font-size:11px;display:inline-flex;align-items:center;gap:4px;margin-left:auto">Density
        <select id="groomDensity">
          <option value="1500" ${gs.maxStrands === 1500 ? "selected" : ""}>1.5k (Fast)</option>
          <option value="4000" ${gs.maxStrands === 4000 ? "selected" : ""}>4k (Default)</option>
          <option value="8000" ${gs.maxStrands === 8000 ? "selected" : ""}>8k (Ultra)</option>
        </select>
      </label>
      <button type="button" id="groomResetDef" class="cfx-mini-btn">Reset Default</button>
    </div>
    <details style="margin-top:8px" open>
      <summary style="cursor:pointer;font-size:11px;font-weight:600">Hair color palette (${colors.length || 48} colors)</summary>
      ${swatchGridHtml}
    </details>
  `;

  host.querySelectorAll("[data-groom-slot]").forEach((sel) => {
    sel.onchange = () => {
      const slot = sel.dataset.groomSlot;
      const entry = sel.value ? (gs._slotEntryMaps?.[slot]?.get(sel.value) || null) : null;
      mountGroomSlot(slot, entry);
    };
  });
  host.querySelectorAll("[data-gcol]").forEach((btn) => {
    btn.onclick = () => {
      const c = colors.find((x) => x.id === btn.dataset.gcol);
      if (!c) return;
      gs.color = { ...c };
      S.view.setGroomColor?.(gs.color);
      renderGroomSection();
    };
  });
  const applyCustomCol = () => {
    const rHex = $("groomRootCol")?.value || rootHex;
    const tHex = $("groomTipCol")?.value || tipHex;
    gs.color = { id: "custom", label: "Custom", rootHex: rHex, tipHex: tHex };
    S.view.setGroomColor?.(gs.color);
  };
  if ($("groomRootCol")) $("groomRootCol").oninput = applyCustomCol;
  if ($("groomTipCol")) $("groomTipCol").oninput = applyCustomCol;
  if ($("groomDensity")) $("groomDensity").onchange = (e) => {
    gs.maxStrands = Number(e.target.value) || 4000;
    if (gs.active.hair) mountGroomSlot("hair", gs.active.hair);
  };
  if ($("groomResetDef")) $("groomResetDef").onclick = async () => {
    gs.color = { ...gs.defaultColor };
    S.view.setGroomColor?.(gs.color);
    await Promise.all(["hair", "brows", "lashes", "beard", "bodyhair"].map((slot) => {
      const defE = slot === "bodyhair" ? null : (gs.defaults?.[slot] || null);
      return mountGroomSlot(slot, defE);
    }));
    renderGroomSection();
  };
}

// Named In-Game Outfits (00011cfbf000.pso + 0DRP Per-Garment Overrides) ---------------------------
async function loadCharacterOutfits(item, openTok) {
  const ca = item?.characterAssembly;
  if (!ca || ca.kind === "Animal") return;
  try {
    const res = await api("char-outfits", { id: item.id });
    if (openTok !== S.open || S.item !== item) return;
    S.charOutfits = res;
    // Seed default per-part overrides from character's 0DRP catalogue
    if (res.partOverrides) {
      for (const [k, v] of Object.entries(res.partOverrides)) {
        if (!S.partOverrides.has(k)) S.partOverrides.set(k, v);
      }
    }
    renderCharOutfitsSection();
  } catch (_) {}
}

async function equipNamedOutfit(outfit) {
  const item = S.item;
  if (!item || !outfit) return;
  // Remove existing clothing parts (keep base Head/Eyes/Teeth/Skin/Genitals/Nipples)
  const baseKeep = new Set([item.id]);
  for (const p of item.characterAssembly?.parts || []) {
    if (p.slot !== "Clothing") baseKeep.add(p.id);
  }
  for (const g of item.characterAssembly?.anatomy?.genitals || []) baseKeep.add(g.id);
  const polyKeep = new Set(Object.values(S.groomState?.polyPartIds || {}));
  for (const pid of [...S.parts.keys()]) {
    if (!baseKeep.has(pid) && !polyKeep.has(pid)) {
      await togglePart(pid, false);
    }
  }
  const toMount = [];
  for (const p of outfit.parts || []) {
    if (!p.id || p.id === item.id) continue;
    const ov = {
      muvPreset: p.muvPreset || "",
      albedo: p.albedo || "",
      normal: p.normal || "",
      tintColor: p.tintColor || "",
      tintRow: p.tintRow ?? null,
    };
    S.partOverrides.set(p.id, ov);
    S.partOverrides.set((p.drawable || "").toLowerCase(), ov);
    if (p.muvPreset) S.muvPresets.set(p.id, p.muvPreset);
    else S.muvPresets.delete(p.id);
    if (S.parts.has(p.id)) {
      const meta = S.partMeta.get(p.id);
      if (meta) loadPartMaterials(p.id, meta, "part" + p.id);
    } else {
      toMount.push(togglePart(p.id, true, { skipStub: true }));
    }
  }
  if (toMount.length) {
    await Promise.all(toMount);
    if (S.item === item) S.view.fit();
  }
  status(`Equipped outfit: ${outfit.label || outfit.name} (${(outfit.parts || []).filter((x) => x.id).length} pieces)`);
}

function renderCharOutfitsSection() {
  const host = $("charOutfitsBox");
  const co = S.charOutfits;
  if (!host || !co || !co.outfits?.length) return;
  const outfits = co.outfits;
  host.innerHTML = `<details class="muv-native-box" open style="margin-top:8px">
    <summary>In-Game Outfits (${outfits.length} from <code>00011cfbf000.pso</code>)</summary>
    <div style="display:flex;gap:6px;margin:6px 0">
      <select id="charOutfitSel" style="flex:1;min-width:0">${outfits.map((o, i) => {
        const cnt = (o.parts || []).filter((p) => p.id).length;
        return `<option value="${i}">#${i + 1} · ${esc(o.label || o.name)} (${cnt} pcs)</option>`;
      }).join("")}</select>
      <button type="button" id="charOutfitEquip">Equip</button>
    </div>
    <div id="charOutfitPreview" style="font-size:11px;opacity:.85"></div>
  </details>`;
  const updatePreview = () => {
    const idx = Number($("charOutfitSel")?.value) || 0;
    const o = outfits[idx];
    const prev = $("charOutfitPreview");
    if (!prev || !o) return;
    prev.innerHTML = (o.parts || []).filter((p) => p.id).map((p) => {
      const sw = p.swatches?.length ? swatchDotsHtml(p.swatches, 8) : (p.tintColor ? swatchDotsHtml([p.tintColor], 8) : "");
      return `<div style="display:flex;align-items:center;gap:5px;padding:1px 0">${sw}<span class="asm-slot">${esc(p.slot || "Part")}</span><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(p.drawable)}">${esc(p.drawable)}</span></div>`;
    }).join("");
  };
  updatePreview();
  if ($("charOutfitSel")) $("charOutfitSel").onchange = updatePreview;
  if ($("charOutfitEquip")) $("charOutfitEquip").onclick = () => {
    const idx = Number($("charOutfitSel")?.value) || 0;
    if (outfits[idx]) equipNamedOutfit(outfits[idx]);
  };
}

async function applyWheels(item, fitAfter = false) {
  const ws = item?.wheelSetup;
  if (!ws || !S.wheels) return;
  if (!S.wheels.enabled) {
    S.wheels.infoRim = null;
    S.wheels.infoTyre = null;
    S.view.removeLayer("wheel_rim");
    S.view.removeLayer("wheel_tyre");
    updatePartStats();
    return;
  }
  const { rimId, tyreId } = S.wheels;
  const tok = (S.wheelTok = (S.wheelTok || 0) + 1);
  try {
    const [rimMeta, rimRes, tyreMeta, tyreRes] = await Promise.all([
      api("item", { id: rimId }), fetch("/api/models/glb?id=" + rimId),
      api("item", { id: tyreId }), fetch("/api/models/glb?id=" + tyreId),
    ]);
    if (S.item !== item || tok !== S.wheelTok || !S.wheels?.enabled) return;
    if (!rimRes.ok || !tyreRes.ok) throw Error("Wheel mesh decode failed");
    const [rimBuf, tyreBuf] = await Promise.all([rimRes.arrayBuffer(), tyreRes.arrayBuffer()]);
    if (S.item !== item || tok !== S.wheelTok || !S.wheels?.enabled) return;
    const tBox = tyreMeta.box;
    const tyreUnitR = tBox ? Math.max(tBox[1][1] - tBox[0][1], tBox[1][2] - tBox[0][2]) * 0.5 : 0.7886;
    const tyreUnitW = tBox ? (tBox[1][0] - tBox[0][0]) : 0.5458;
    const rBox = rimMeta.box;
    const rimUnitR = rBox ? Math.max(rBox[1][1] - rBox[0][1], rBox[1][2] - rBox[0][2]) * 0.5 : 0.5;
    const rimFix = 0.5 / Math.max(rimUnitR, 0.1);
    const rimMats = [], tyreMats = [];
    for (const ax of ws.axles) {
      const sr = ax.radius / Math.max(tyreUnitR, 0.1);
      const sw = ax.width / Math.max(tyreUnitW, 0.05);
      const sgn = ax.right ? -1 : 1;
      const [px, py, pz] = ax.position;
      rimMats.push(new Float32Array([sgn * sw, 0, 0, 0, 0, sr * rimFix, 0, 0, 0, 0, sgn * sr * rimFix, 0, px, py, pz, 1]));
      tyreMats.push(new Float32Array([sgn * sw, 0, 0, 0, 0, sr, 0, 0, 0, 0, sgn * sr, 0, px, py, pz, 1]));
    }
    S.wheels.infoRim = S.view.load(rimBuf, "wheel_rim", true, true, rimMats);
    S.wheels.infoTyre = S.view.load(tyreBuf, "wheel_tyre", true, true, tyreMats);
    for (const [meta, layer] of [[rimMeta, "wheel_rim"], [tyreMeta, "wheel_tyre"]]) {
      for (const m of meta.materials) {
        const st = materialState(m, meta.name, meta.category);
        const tmode = st.pick ? st.pick.texMode : 0;
        const lutHash = st.pick?.lutHash || null;
        const fallbackTint = !st.pick && !st.tintColor ? (layer === "wheel_rim" ? [0.76, 0.78, 0.80] : [0.16, 0.16, 0.17]) : st.tintColor;
        S.view.setMaterial(m.index, {
          visible: st.visible, color: palette(m.index), texMode: tmode,
          matMode: st.matMode, passType: st.passType, lumaAlpha: st.lumaAlpha,
          lutHash, palette: st.pick?.palette || null, tintColor: fallbackTint,
        }, layer);
        const h = st.pick ? texHash(st.pick.slot) : null;
        const nh = st.nslot ? texHash(st.nslot) : null;
        const reqRes = curTexRes();
        if (h) textureImage(h, reqRes, meta.id).then((img) => { if (S.item === item && S.wheels?.enabled && curTexRes() === reqRes) S.view.setTexture(m.index, img, layer); }).catch(() => {});
        if (nh) textureImage(nh, reqRes, meta.id).then((img) => { if (S.item === item && S.wheels?.enabled && curTexRes() === reqRes) S.view.setNormalTexture(m.index, img, layer); }).catch(() => {});
        loadPbrAuxTextures(m.index, st, layer, meta.id, () => S.item === item && S.wheels?.enabled);
        if (lutHash) ensureLutArray(lutHash);
      }
    }
    if (fitAfter) S.view.fit();
    updatePartStats();
  } catch (e) {
    status(`Wheels: ${e.message}`);
  }
}

function updatePartStats() {
  const parts = [...S.parts.values()].filter(Boolean);
  const wList = S.wheels?.enabled ? [S.wheels.infoRim, S.wheels.infoTyre].filter(Boolean) : [];
  const base = S.item?.view;
  if (!base) return;
  const all = [...parts, ...wList];
  const tris = base.triangles + all.reduce((n, p) => n + p.triangles, 0);
  const verts = base.vertices + all.reduce((n, p) => n + p.vertices, 0);
  const lodTag = S.lod ? ` · LOD ${S.lod}` : "";
  const extra = [];
  if (parts.length) extra.push(`${parts.length} parts`);
  if (wList.length && S.item?.wheelSetup) extra.push(`${S.item.wheelSetup.axles.length} wheels`);
  $("stats").textContent = `${fmtN(verts)} vertices · ${fmtN(tris)} triangles` +
    (extra.length ? ` · main model + ${extra.join(" + ")}` : ` · ${base.primitives} primitives`) + lodTag;
}
async function assembleDefaults(item) {
  if (item.category === "Animals" && item.family) {
    const fam = item.family.toLowerCase();
    const mainHasBody = item.name.toLowerCase().includes("_body");
    const slots = new Map();
    for (const g of item.group) {
      if (g.id === item.id || g.status !== "indexed" || g.low_detail || g.kind === "fragment") continue;
      const clean = g.name.toLowerCase().replace(/_(hi|mid|lod\d*)$/, "");
      if (!clean.startsWith(fam + "_")) continue;
      const suf = clean.slice(fam.length + 1);
      if (/(?:^|_)(?:fur|antlerb|harness|collar|collarharness|hat|shirt|costume|goggles|cape|flower|lace|ribbon|rope|tranquilizer|crown|jewel|vest|badge|knife|corpse|roadkill|law|sheriff|css)(?:_|$)/.test(suf)) continue;
      const nums = suf.match(/\d+/g);
      if (nums && nums.some((n) => n !== "00")) continue;
      const slot = suf.replace(/_00$/, "");
      if (slot === "body" && mainHasBody) continue;
      const rank = (g.name.endsWith("_hi") ? 0 : 2) + (g.archive === "packs.rpf" ? 0 : 1);
      if (!slots.has(slot) || rank < slots.get(slot).rank) slots.set(slot, { id: g.id, rank });
    }
    const tasks = [];
    for (const { id } of slots.values()) if (!S.parts.has(id)) tasks.push(togglePart(id, true));
    if (tasks.length) {
      await Promise.all(tasks);
      if (S.item === item) S.view.fit();
    }
    return;
  }
  if ((item.category === "Protagonists" || item.category === "Characters") && item.family) {
    const fam = item.family.toLowerCase();
    const slots = new Map();
    for (const g of item.group) {
      if (g.id === item.id || g.status !== "indexed" || g.low_detail) continue;
      const clean = g.name.toLowerCase().replace(/_(hi|mid|lod\d*|full)$/, "");
      if (!clean.startsWith(fam + "_")) continue;
      const suf = clean.slice(fam.length + 1);
      if (!/^(?:eyes|teeth)$/.test(suf)) continue;
      const rank = (g.name.endsWith("_full") || g.name.endsWith("_hi") ? 0 : 2) + (g.archive === "packs.rpf" ? 0 : 1);
      if (!slots.has(suf) || rank < slots.get(suf).rank) slots.set(suf, { id: g.id, rank });
    }
    const tasks = [];
    for (const { id } of slots.values()) if (!S.parts.has(id)) tasks.push(togglePart(id, true));
    if (tasks.length) {
      await Promise.all(tasks);
      if (S.item === item) S.view.fit();
    }
    return;
  }
  const slots = new Map();
  for (const g of item.group) {
    if (g.id === item.id || g.status !== "indexed" || g.low_detail) continue;
    const base = g.name.replace(/_(hi|mid)$/, "");
    if (base === item.family) continue;
    const slot = base.replace(/_\d+$/, "");
    const rank = (/_01$/.test(base) ? 0 : 2) + (g.name.endsWith("_hi") ? 0 : 1);
    if (!slots.has(slot) || rank < slots.get(slot).rank) slots.set(slot, { id: g.id, rank });
  }
  for (const { id } of slots.values()) if (!S.parts.has(id)) togglePart(id, true);
}

// Skeleton, GPU Skinning & Animation --------------------------------------------------------------
function fetchSkeletonCached(skeletonId) {
  const key = Number(skeletonId);
  if (_skelCache.has(key)) return _skelCache.get(key);
  const p = api("skeleton", { skeletonId: key }).catch((e) => {
    _skelCache.delete(key);
    throw e;
  });
  _skelCache.set(key, p);
  return p;
}

const _animClipBufCache = new Map();

async function fetchAnimClipBuffer(dictId, clipHash, skeletonId) {
  const key = `${dictId}:${clipHash}:${skeletonId}`;
  if (_animClipBufCache.has(key)) return _animClipBufCache.get(key);
  const r = await fetch(`/api/models/anim-clip?dictId=${dictId}&clipHash=${encodeURIComponent(clipHash)}&skeleton=${skeletonId}`);
  if (!r.ok) {
    let msg = `Clip decode failed (${r.status})`;
    try { msg = (await r.json()).error || msg; } catch (_) {}
    throw Error(msg);
  }
  const buf = await r.arrayBuffer();
  _animClipBufCache.set(key, buf);
  if (_animClipBufCache.size > 96) {
    const first = _animClipBufCache.keys().next().value;
    _animClipBufCache.delete(first);
  }
  return buf;
}

async function initRigForItem(item, openTok) {
  const info = item?.rigInfo;
  if (!info || !info.skeletonId) {
    S.rig = null;
    S.view.clearSkeleton();
    updateDownloadLink(item, S.lod);
    return;
  }
  const isAnimal = info.rigKind === "animal";
  const rigState = {
    info,
    skel: null,
    skelPromise: null,
    extraJoints: 0,
    maxInfluences: 4,
    localBones: 0,
    skinnedParts: 0,
    rigTab: "clips",
    activeAnim: null,
    animCat: isAnimal ? "All" : "Locomotion",
    animArchive: "All",
    animQ: "",
    animOffset: 0,
    animTotal: 0,
    animItems: null,
    animCategories: null,
    animArchives: null,
    animTok: 0,
    // Blendspaces tab
    bsQ: "",
    bsDims: "All",
    bsDomain: isAnimal ? "animal/creature" : "All",
    bsOffset: 0,
    bsTotal: 0,
    bsItems: null,
    bsDomains: null,
    bsTok: 0,
    activeBs: null,
    bsSubIdx: 0,
    bsParams: [],
    bsWeights: new Map(),
    bsLoadTok: 0,
    bsLiveKey: null,
    // Motion Matching tab
    mmQ: "",
    mmCat: isAnimal ? "animal" : "All",
    mmMode: "",
    mmItems: null,
    mmTotal: 0,
    mmCategories: null,
    mmTok: 0,
    activeMm: null,
    mmLoadTok: 0,
    boneQ: "",
  };
  S.rig = rigState;
  updateDownloadLink(item, S.lod);

  rigState.skelPromise = fetchSkeletonCached(info.skeletonId).then((skel) => {
    if (S.item !== item || S.rig !== rigState) return null;
    rigState.skel = skel;
    S.view.setSkeleton(skel);
    syncRigSummary();
    renderBoneList();
    return skel;
  }).catch((e) => {
    if (S.item === item && S.rig === rigState) status(`Skeleton: ${e.message}`);
    return null;
  });

  const skel = await rigState.skelPromise;
  if (!skel || S.item !== item || S.rig !== rigState) return;
  attachPartSkin(item.id, "main", S.lod);
  // Also skin any already-mounted parts (in case assembleCharacter finished before skeleton arrived).
  for (const [pid, pinfo] of S.parts?.entries() || []) if (pinfo) attachPartSkin(pid, "part" + pid, 0);
  if (info.canAnimate && !rigState.animItems) refreshAnimSearch(item);
}

async function attachPartSkin(modelId, layer, lod = 0) {
  const item = S.item;
  const rig = S.rig;
  if (!item || !rig?.info) return null;
  if (!rig.skel && rig.skelPromise) await rig.skelPromise;
  if (S.item !== item || S.rig !== rig || !rig.skel) return null;
  try {
    const r = await fetch(`/api/models/skin?id=${modelId}&lod=${lod}&skeleton=${rig.info.skeletonId}&v=3`, { cache: "no-store" });
    if (!r.ok) return null;
    const buf = await r.arrayBuffer();
    if (S.item !== item || S.rig !== rig) return null;
    const res = S.view.attachSkin(layer, buf);
    if (res && res.skinnedPrims > 0) {
      rig.extraJoints = res.extraJointsTotal || 0;
      rig.maxInfluences = Math.max(rig.maxInfluences || 4, res.maxInfluences || 4);
      rig.localBones = Math.max(rig.localBones || 0, res.localBones || 0);
      rig.skinnedParts = (rig.skinnedParts || 0) + 1;
      syncRigSummary();
      if (res.extraAdded > 0) renderBoneList();
    }
    return res;
  } catch {
    return null;
  }
}

function syncRigSummary() {
  const host = $("rigSummary");
  const rig = S.rig;
  if (!host || !rig) return;
  const skel = rig.skel;
  if (!skel) {
    host.innerHTML = `<span class="rig-badge">Loading <code>${esc(rig.info.skeletonName)}</code>…</span>`;
    return;
  }
  const masterN = skel.boneCount || skel.joints?.length || 0;
  const extraN = rig.extraJoints || 0;
  const errTxt = skel.bindError != null ? skel.bindError.toExponential(1) : "0";
  const badges = [
    `<span class="rig-badge cyan" title="Master crSkeletonData">${esc(skel.name || rig.info.skeletonName)}</span>`,
    `<span class="rig-badge">${fmtN(masterN)} bones${extraN ? ` + ${extraN} GM` : ""}</span>`,
    `<span class="rig-badge green" title="GPU Linear Blend Skinning influences per vertex">${rig.maxInfluences || 4}-inf LBS</span>`,
    `<span class="rig-badge" title="Max inverse-bind matrix verification residual ||W*IBM - I||">bind err ${esc(errTxt)}</span>`,
  ];
  if (rig.info.rigKind === "animal") badges.push(`<span class="rig-badge amber">Animal rig</span>`);
  if (extraN > 0) badges.push(`<span class="rig-badge amber" title="Garment helper bones auto-parented to SKEL_* anatomy">+${extraN} garment helpers</span>`);
  host.innerHTML = badges.join("");
}

function highlightBoneIn3D(boneName, boneHashHex) {
  const joints = S.view?.getSkeletonJoints() || [];
  if (!joints.length) return false;
  const targetH = boneHashHex ? (parseInt(String(boneHashHex), 16) >>> 0) : null;
  const targetN = boneName ? String(boneName).toLowerCase() : "";
  let idx = -1;
  if (targetH != null) idx = joints.findIndex((j) => (j.hash >>> 0) === targetH);
  if (idx < 0 && targetN) idx = joints.findIndex((j) => (j.name || "").toLowerCase() === targetN);
  if (idx < 0) return false;
  if (!S.view.opts.skeleton) {
    S.view.set("skeleton", true);
    if ($("skeleton")) $("skeleton").checked = true;
    if ($("rigSkelOn")) $("rigSkelOn").checked = true;
  }
  S.view.set("selectedBone", idx);
  $("boneList")?.querySelectorAll("[data-bidx]").forEach((b) => b.setAttribute("aria-selected", Number(b.dataset.bidx) === idx));
  status(`Highlighted bone #${idx}: ${joints[idx].name}`);
  return true;
}

function renderBoneList() {
  const host = $("boneList");
  const cnt = $("boneCountLbl");
  if (!host) return;
  const joints = S.view?.getSkeletonJoints() || [];
  if (cnt) cnt.textContent = String(joints.length);
  if (!joints.length) {
    host.innerHTML = `<p class="note" style="padding:6px">Loading bone hierarchy…</p>`;
    return;
  }
  const q = (S.rig?.boneQ || "").trim().toLowerCase();
  const filt = S.view?.opts?.skelFilter || "core";
  const sel = S.view?.opts?.selectedBone ?? -1;
  const shown = [];
  for (let i = 0; i < joints.length; i++) {
    const j = joints[i];
    if (q) {
      if (!j.name.toLowerCase().includes(q) && !(j.group || "").toLowerCase().includes(q)) continue;
    } else if (filt === "core" && j.group !== "SKEL" && j.group !== "GM" && i !== sel) {
      continue;
    } else if (filt === "face" && j.group !== "SKEL" && j.group !== "FB" && j.group !== "GM" && i !== sel) {
      continue;
    }
    shown.push(j);
    if (shown.length >= 250) break;
  }
  if (!shown.length) {
    host.innerHTML = `<p class="note" style="padding:6px">No matching bones.</p>`;
    return;
  }
  host.innerHTML = shown.map((j) => {
    const parName = j.parent >= 0 && joints[j.parent] ? joints[j.parent].name : "root";
    const grp = esc(j.group || "OTHER");
    return `<button type="button" class="bone-row" data-bidx="${j.index}" aria-selected="${j.index === sel}" title="${esc(j.name)} (parent: ${esc(parName)}, hash: 0x${(j.hash >>> 0).toString(16).padStart(8, "0")})"><span class="bidx">${j.index}</span><span class="bgrp ${grp}">${grp}</span><span class="bname">${esc(j.name)}</span></button>`;
  }).join("");
  host.querySelectorAll("[data-bidx]").forEach((btn) => {
    btn.onclick = () => {
      const idx = Number(btn.dataset.bidx);
      const next = S.view.opts.selectedBone === idx ? -1 : idx;
      if (next >= 0 && !S.view.opts.skeleton) {
        S.view.set("skeleton", true);
        if ($("skeleton")) $("skeleton").checked = true;
        if ($("rigSkelOn")) $("rigSkelOn").checked = true;
      }
      S.view.set("selectedBone", next);
      host.querySelectorAll("[data-bidx]").forEach((b) => b.setAttribute("aria-selected", Number(b.dataset.bidx) === next));
    };
  });
}

function syncAnimTransport(state, anim) {
  const playBtn = $("animPlay");
  const resetBtn = $("animReset");
  const scrub = $("animScrub");
  const timeEl = $("animTime");
  const nowEl = $("animNow");
  if (!playBtn) return;
  if (!anim) {
    playBtn.textContent = "▶ Play";
    playBtn.disabled = true;
    if (resetBtn) resetBtn.disabled = true;
    if (scrub) { scrub.disabled = true; scrub.value = "0"; }
    if (timeEl) timeEl.textContent = "Bind pose";
    if (nowEl) nowEl.innerHTML = `<span>Bind pose (rest)</span><small>Pick a clip, blendspace, or MM DB</small>`;
    return;
  }
  playBtn.disabled = false;
  playBtn.textContent = state.playing ? "❚❚ Pause" : "▶ Play";
  if (resetBtn) resetBtn.disabled = false;
  const dur = Math.max(0.001, anim.duration || 0.001);
  const t = Math.max(0, Math.min(dur, state.time || 0));
  if (scrub) {
    scrub.disabled = false;
    if (document.activeElement !== scrub) scrub.value = String(Math.round((t / dur) * 1000));
  }
  const frames = Math.max(1, anim.frames || 1);
  const f = Math.min(frames - 1, Math.round((t / dur) * Math.max(0, frames - 1)));
  if (timeEl) timeEl.textContent = `${t.toFixed(2)}s / ${dur.toFixed(2)}s · f${f}/${frames}`;
  if (nowEl) {
    if (anim.isBlendspace) {
      const nActive = (anim.weights || []).reduce((n, w) => n + (w > 0.005 ? 1 : 0), 0);
      nowEl.innerHTML = `<span title="${esc(anim.name)}">◈ ${esc(anim.name)}</span><small>${nActive}/${anim.clips?.length || 0} blended clips</small>`;
    } else {
      nowEl.innerHTML = `<span title="${esc(anim.dictName)} / ${esc(anim.name)}">${esc(anim.name)}</span><small>${anim.matchedBones} bones · ${Math.round(anim.fps || 30)} fps</small>`;
    }
  }
}

async function loadAnimClip(clip, opts = {}) {
  const item = S.item;
  const rig = S.rig;
  if (!item || !rig?.info) return;
  if (!rig.skel && rig.skelPromise) await rig.skelPromise;
  if (S.item !== item || S.rig !== rig || !rig.skel) return;
  status(`Decoding animation ${clip.name}…`);
  try {
    const buf = await fetchAnimClipBuffer(clip.dictId, clip.clipHash, rig.info.skeletonId);
    if (S.item !== item || S.rig !== rig) return;
    const hdr = S.view.setAnimation(buf);
    rig.bsLiveKey = null;
    rig.activeAnim = { dictId: clip.dictId, clipHash: clip.clipHash, name: hdr?.name || clip.name, dictName: clip.dictName || "" };
    if (typeof opts.seekTime === "number" && Number.isFinite(opts.seekTime)) {
      S.view.setAnimState({ time: Math.max(0, Math.min(hdr?.duration || 0, opts.seekTime)), playing: opts.playing !== false });
    }
    updateDownloadLink(item, S.lod);
    $("animList")?.querySelectorAll(".anim-row").forEach((b) => {
      b.setAttribute("aria-current", b.dataset.dict === String(clip.dictId) && b.dataset.hash === String(clip.clipHash));
    });
    const seekNote = typeof opts.seekTime === "number" ? ` @ ${opts.seekTime.toFixed(2)}s` : "";
    status(`Playing ${clip.name}${seekNote} (${hdr?.frames || clip.frames || "?"} frames, ${hdr?.matchedBones || clip.matchedBones || 0} bones matched)`);
  } catch (e) {
    status(`Animation error: ${e.message}`);
  }
}

async function refreshAnimSearch(item) {
  const rig = S.rig;
  if (!rig || S.item !== item) return;
  const tok = ++rig.animTok;
  const listEl = $("animList");
  if (listEl) listEl.innerHTML = `<p class="note" style="padding:6px">Searching animation archives…</p>`;
  try {
    const res = await api("anims", {
      q: rig.animQ || "",
      category: rig.animCat === "All" ? "" : (rig.animCat || ""),
      archive: rig.animArchive === "All" ? "" : (rig.animArchive || ""),
      rig: rig.info.rigKind || "female",
      skeletonId: rig.info.skeletonId || "",
      limit: 40,
      offset: rig.animOffset || 0,
    });
    if (S.item !== item || S.rig !== rig || tok !== rig.animTok) return;
    rig.animTotal = res.total || 0;
    rig.animItems = res.items || [];
    rig.animCategories = res.categories || [];
    rig.animArchives = res.archives || [];
    renderAnimList();
  } catch (e) {
    if (S.item !== item || S.rig !== rig || tok !== rig.animTok) return;
    if (listEl) listEl.innerHTML = `<p class="note warn" style="padding:6px">Animation library unavailable: ${esc(e.message)}</p>`;
  }
}

function renderAnimList() {
  const rig = S.rig;
  const archEl = $("animArchives");
  const catsEl = $("animCats");
  const listEl = $("animList");
  const pageEl = $("animPage");
  const prevBtn = $("animPrev");
  const nextBtn = $("animNext");
  if (!rig || !listEl) return;

  if (archEl && rig.animArchives && rig.animArchives.length > 1) {
    const totalAll = rig.animArchives.reduce((n, a) => n + a.count, 0);
    const pills = [{ name: "All", label: `All (${fmtN(totalAll)})` }].concat(
      rig.animArchives.map((a) => ({ name: a.name, label: `${a.label} (${fmtN(a.count)})` }))
    );
    archEl.innerHTML = pills.map((p) =>
      `<button type="button" data-aarch="${esc(p.name)}" aria-pressed="${rig.animArchive === p.name}">${esc(p.label)}</button>`
    ).join("");
    archEl.querySelectorAll("[data-aarch]").forEach((btn) => {
      btn.onclick = () => {
        rig.animArchive = btn.dataset.aarch;
        rig.animOffset = 0;
        refreshAnimSearch(S.item);
      };
    });
  } else if (archEl) {
    archEl.innerHTML = "";
  }

  if (catsEl && rig.animCategories) {
    const preferred = ["All", "Locomotion", "Idle", "Gestures", "Combat", "Sit & Props", "Vehicles", "Cutscene", "Facial", "Animals"];
    const byName = new Map(rig.animCategories.map((c) => [c.name, c.count]));
    const totalAll = rig.animCategories.reduce((n, c) => n + c.count, 0);
    const pills = preferred.filter((p) => p === "All" || byName.has(p));
    catsEl.innerHTML = pills.map((p) => {
      const cnt = p === "All" ? totalAll : (byName.get(p) || 0);
      return `<button type="button" data-acat="${esc(p)}" aria-pressed="${rig.animCat === p}" title="${fmtN(cnt)} clips">${esc(p)}</button>`;
    }).join("");
    catsEl.querySelectorAll("[data-acat]").forEach((btn) => {
      btn.onclick = () => {
        rig.animCat = btn.dataset.acat;
        rig.animOffset = 0;
        refreshAnimSearch(S.item);
      };
    });
  }
  const items = rig.animItems || [];
  if (!items.length) {
    listEl.innerHTML = `<p class="note" style="padding:6px">No matching clips for this skeleton.</p>`;
  } else {
    const cur = rig.activeAnim;
    listEl.innerHTML = items.map((c, idx) => {
      const active = cur && cur.dictId === c.dictId && cur.clipHash === c.clipHash;
      const archBadge = c.archive && c.archive !== "anim.rpf" ? `<span class="anim-tag">${esc(c.archiveLabel || c.archive)}</span>` : "";
      const spBadge = c.speciesMatch ? `<span class="anim-tag sp" title="Species-matched dictionary">★</span>` : "";
      return `<button type="button" class="anim-row" data-aidx="${idx}" data-dict="${c.dictId}" data-hash="${esc(c.clipHash)}" aria-current="${!!active}" title="${esc(c.dictName)} / ${esc(c.name)}"><strong>${esc(c.name)}${spBadge}${archBadge}</strong><small>${esc(c.dictName)} · ${Number(c.duration).toFixed(2)}s · ${c.matchedBones} bones</small></button>`;
    }).join("");
    listEl.querySelectorAll("[data-aidx]").forEach((btn) => {
      btn.onclick = () => loadAnimClip(items[Number(btn.dataset.aidx)]);
    });
  }
  const off = rig.animOffset || 0;
  const tot = rig.animTotal || 0;
  if (pageEl) pageEl.textContent = tot ? `${fmtN(off + 1)}–${fmtN(Math.min(tot, off + items.length))} / ${fmtN(tot)}` : "0 clips";
  if (prevBtn) prevBtn.disabled = off <= 0;
  if (nextBtn) nextBtn.disabled = off + 40 >= tot;
}

// -------------------------------------------------------------------------------------------------
// Parametric Blendspaces (.motion): Search, 1D/2D/N-D Evaluator, Canvas Visualizer & Live Blending
// -------------------------------------------------------------------------------------------------
function getActiveBsNode(rig) {
  const bs = rig?.activeBs;
  if (!bs) return null;
  if (bs.variantType === 2 && Array.isArray(bs.subMotions) && bs.subMotions.length) {
    const idx = Math.max(0, Math.min(bs.subMotions.length - 1, rig.bsSubIdx || 0));
    return bs.subMotions[idx];
  }
  return bs;
}

function getBsAxisBounds(node, axisIdx) {
  const ax = (node.axes || [])[axisIdx];
  let lo = ax && ax.min != null && Number.isFinite(ax.min) ? ax.min : Infinity;
  let hi = ax && ax.max != null && Number.isFinite(ax.max) ? ax.max : -Infinity;
  for (const s of node.samples || []) {
    const v = (s.coords || [])[axisIdx];
    if (Number.isFinite(v)) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [-1, 1];
  if (Math.abs(hi - lo) < 1e-5) return [lo - 1, hi + 1];
  return [lo, hi];
}

function evalBlendspaceNode(node, params) {
  const samples = node?.samples || [];
  const dim = node?.dimensions || 0;
  const weights = new Map();
  let activeTri = null;
  let activeSeg = null;
  if (!samples.length) return { weights, activeTri, activeSeg };
  if (dim === 0 || samples.length === 1) {
    weights.set(samples[0].sampleIndex, 1.0);
    return { weights, activeTri, activeSeg };
  }
  if (dim === 1) {
    const x = Number(params[0] ?? 0);
    const pts = samples
      .filter((s) => s.coords && s.coords.length >= 1)
      .map((s) => ({ x: s.coords[0], si: s.sampleIndex }))
      .sort((a, b) => a.x - b.x);
    if (!pts.length) return { weights, activeTri, activeSeg };
    if (x <= pts[0].x) { weights.set(pts[0].si, 1.0); return { weights, activeTri, activeSeg }; }
    if (x >= pts[pts.length - 1].x) { weights.set(pts[pts.length - 1].si, 1.0); return { weights, activeTri, activeSeg }; }
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i], b = pts[i + 1];
      if (x >= a.x && x <= b.x) {
        const span = b.x - a.x;
        const t = span > 1e-9 ? (x - a.x) / span : 0;
        weights.set(a.si, 1 - t);
        weights.set(b.si, t);
        activeSeg = [a.si, b.si];
        return { weights, activeTri, activeSeg };
      }
    }
    weights.set(pts[pts.length - 1].si, 1.0);
    return { weights, activeTri, activeSeg };
  }
  if (dim === 2) {
    const px = Number(params[0] ?? 0), py = Number(params[1] ?? 0);
    const cmap = new Map();
    for (const s of samples) {
      if (s.coords && s.coords.length >= 2) cmap.set(s.sampleIndex, s.coords);
    }
    for (const [si, c] of cmap) {
      if ((c[0] - px) ** 2 + (c[1] - py) ** 2 < 1e-10) {
        weights.set(si, 1.0);
        return { weights, activeTri, activeSeg };
      }
    }
    const tris = node.triangles || [];
    let bestTri = null, bestBary = null, bestNeg = -1e30;
    for (const tri of tris) {
      const a = cmap.get(tri[0]), b = cmap.get(tri[1]), c = cmap.get(tri[2]);
      if (!a || !b || !c) continue;
      const det = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1]);
      if (Math.abs(det) < 1e-12) continue;
      const l0 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / det;
      const l1 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / det;
      const l2 = 1 - l0 - l1;
      const mn = Math.min(l0, l1, l2);
      if (mn >= -1e-5) {
        const ws = [Math.max(0, l0), Math.max(0, l1), Math.max(0, l2)];
        const s = ws[0] + ws[1] + ws[2] || 1;
        for (let i = 0; i < 3; i++) if (ws[i] > 1e-5) weights.set(tri[i], ws[i] / s);
        activeTri = tri;
        return { weights, activeTri, activeSeg };
      }
      if (mn > bestNeg) { bestNeg = mn; bestTri = tri; bestBary = [l0, l1, l2]; }
    }
    if (bestTri && bestBary) {
      const ws = bestBary.map((v) => Math.max(0, v));
      const s = ws[0] + ws[1] + ws[2];
      if (s > 1e-9) {
        for (let i = 0; i < 3; i++) if (ws[i] > 1e-5) weights.set(bestTri[i], ws[i] / s);
        activeTri = bestTri;
        return { weights, activeTri, activeSeg };
      }
    }
  }
  // 3D-5D or triangle-free fallback: normalized inverse-distance top-K blending
  const spans = [];
  for (let d = 0; d < dim; d++) {
    const [lo, hi] = getBsAxisBounds(node, d);
    spans.push(Math.max(1e-4, hi - lo));
  }
  const scored = [];
  for (const s of samples) {
    const c = s.coords || [];
    let d2 = 0;
    for (let d = 0; d < Math.min(dim, c.length); d++) {
      const diff = ((params[d] ?? 0) - c[d]) / spans[d];
      d2 += diff * diff;
    }
    scored.push({ si: s.sampleIndex, dist: Math.sqrt(d2) });
  }
  scored.sort((a, b) => a.dist - b.dist);
  if (scored[0].dist < 1e-5) {
    weights.set(scored[0].si, 1.0);
    return { weights, activeTri, activeSeg };
  }
  const top = scored.slice(0, Math.min(4, scored.length));
  let sum = 0;
  for (const t of top) { t.w = 1 / (t.dist * t.dist + 1e-4); sum += t.w; }
  for (const t of top) weights.set(t.si, t.w / sum);
  return { weights, activeTri, activeSeg };
}

function drawBlendspaceCanvas(canvas, node, params, evalRes) {
  if (!canvas || !node) return;
  const dim = node.dimensions || 0;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const cssW = Math.max(220, Math.floor(canvas.clientWidth || 280));
  const cssH = dim <= 1 ? 84 : 196;
  if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.height = `${cssH}px`;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = "#0a1113";
  ctx.fillRect(0, 0, cssW, cssH);

  const samples = node.samples || [];
  const wMap = evalRes?.weights || new Map();

  if (dim <= 1) {
    const padL = 22, padR = 22, yMid = 44;
    const [xmin, xmax] = getBsAxisBounds(node, 0);
    const toX = (v) => padL + ((v - xmin) / Math.max(1e-6, xmax - xmin)) * (cssW - padL - padR);
    const axName = (node.axes?.[0]?.name || node.axes?.[0]?.hash || "param");
    // Rail
    ctx.strokeStyle = "#2b3e42";
    ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(padL, yMid); ctx.lineTo(cssW - padR, yMid); ctx.stroke();
    // Active segment
    if (evalRes?.activeSeg) {
      const s0 = samples.find((s) => s.sampleIndex === evalRes.activeSeg[0]);
      const s1 = samples.find((s) => s.sampleIndex === evalRes.activeSeg[1]);
      if (s0 && s1) {
        ctx.strokeStyle = "#5ee3b2";
        ctx.lineWidth = 4;
        ctx.beginPath(); ctx.moveTo(toX(s0.coords[0]), yMid); ctx.lineTo(toX(s1.coords[0]), yMid); ctx.stroke();
      }
    }
    // Labels
    ctx.font = "10px monospace";
    ctx.fillStyle = "#8aa5ab";
    ctx.textAlign = "left"; ctx.fillText(xmin.toFixed(2), padL, 74);
    ctx.textAlign = "center"; ctx.fillText(axName, cssW / 2, 16);
    ctx.textAlign = "right"; ctx.fillText(xmax.toFixed(2), cssW - padR, 74);
    // Samples
    for (const s of samples) {
      const vx = dim === 0 ? 0.5 * (xmin + xmax) : (s.coords?.[0] ?? 0);
      const sx = toX(vx);
      const w = wMap.get(s.sampleIndex) || 0;
      const r = 4 + w * 5.5;
      ctx.beginPath(); ctx.arc(sx, yMid, r, 0, Math.PI * 2);
      ctx.fillStyle = w > 0.01 ? "#6fe8b8" : (s.resolved ? "#3c6f78" : "#444c4f");
      ctx.fill();
      ctx.strokeStyle = "#0a1113"; ctx.lineWidth = 1.5; ctx.stroke();
      if (w > 0.04) {
        ctx.fillStyle = "#d9fced"; ctx.textAlign = "center";
        ctx.fillText(`${Math.round(w * 100)}%`, sx, yMid - 12);
      }
    }
    // Cursor
    if (dim === 1) {
      const cx = toX(Number(params[0] ?? 0));
      ctx.strokeStyle = "#ffd977"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(cx, yMid - 14); ctx.lineTo(cx, yMid + 14); ctx.stroke();
      ctx.beginPath(); ctx.arc(cx, yMid, 5, 0, Math.PI * 2);
      ctx.fillStyle = "#ffd977"; ctx.fill();
    }
    canvas._bsPlot = { dim, padL, padR, xmin, xmax, cssW, cssH };
    return;
  }

  // 2D (or XY projection of 3D-5D)
  const padL = 34, padR = 14, padT = 16, padB = 24;
  const plotW = cssW - padL - padR, plotH = cssH - padT - padB;
  const [xmin, xmax] = getBsAxisBounds(node, 0);
  const [ymin, ymax] = getBsAxisBounds(node, 1);
  const toX = (v) => padL + ((v - xmin) / Math.max(1e-6, xmax - xmin)) * plotW;
  const toY = (v) => padT + (1 - (v - ymin) / Math.max(1e-6, ymax - ymin)) * plotH;

  // Grid
  ctx.strokeStyle = "#172428"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const gx = padL + (plotW * i) / 4;
    const gy = padT + (plotH * i) / 4;
    ctx.beginPath(); ctx.moveTo(gx, padT); ctx.lineTo(gx, padT + plotH); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(padL + plotW, gy); ctx.stroke();
  }
  // Zero axes
  ctx.strokeStyle = "#2a4147"; ctx.lineWidth = 1;
  if (xmin < 0 && xmax > 0) { const zx = toX(0); ctx.beginPath(); ctx.moveTo(zx, padT); ctx.lineTo(zx, padT + plotH); ctx.stroke(); }
  if (ymin < 0 && ymax > 0) { const zy = toY(0); ctx.beginPath(); ctx.moveTo(padL, zy); ctx.lineTo(padL + plotW, zy); ctx.stroke(); }

  const cmap = new Map();
  for (const s of samples) if (s.coords && s.coords.length >= 2) cmap.set(s.sampleIndex, s.coords);

  // Triangulation mesh
  const tris = node.triangles || [];
  ctx.strokeStyle = "rgba(92, 188, 175, 0.28)";
  ctx.lineWidth = 1;
  for (const tri of tris) {
    const a = cmap.get(tri[0]), b = cmap.get(tri[1]), c = cmap.get(tri[2]);
    if (!a || !b || !c) continue;
    ctx.beginPath();
    ctx.moveTo(toX(a[0]), toY(a[1]));
    ctx.lineTo(toX(b[0]), toY(b[1]));
    ctx.lineTo(toX(c[0]), toY(c[1]));
    ctx.closePath();
    ctx.stroke();
  }
  // Highlight active triangle
  if (evalRes?.activeTri) {
    const a = cmap.get(evalRes.activeTri[0]), b = cmap.get(evalRes.activeTri[1]), c = cmap.get(evalRes.activeTri[2]);
    if (a && b && c) {
      ctx.fillStyle = "rgba(84, 224, 172, 0.17)";
      ctx.strokeStyle = "#6de8ba";
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.moveTo(toX(a[0]), toY(a[1]));
      ctx.lineTo(toX(b[0]), toY(b[1]));
      ctx.lineTo(toX(c[0]), toY(c[1]));
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    }
  }

  // Sample nodes
  ctx.font = "9px monospace";
  for (const s of samples) {
    const c = cmap.get(s.sampleIndex);
    if (!c) continue;
    const sx = toX(c[0]), sy = toY(c[1]);
    const w = wMap.get(s.sampleIndex) || 0;
    const r = 3.5 + w * 6;
    if (w > 0.02) {
      ctx.beginPath(); ctx.arc(sx, sy, r + 3, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(110, 232, 184, ${(0.18 + 0.3 * w).toFixed(2)})`;
      ctx.fill();
    }
    ctx.beginPath(); ctx.arc(sx, sy, r, 0, Math.PI * 2);
    ctx.fillStyle = w > 0.01 ? "#6ee8b8" : (s.resolved ? "#407a85" : "#495256");
    ctx.fill();
    ctx.strokeStyle = "#081012"; ctx.lineWidth = 1.2; ctx.stroke();
    if (w > 0.08) {
      ctx.fillStyle = "#dcfef0";
      ctx.textAlign = sx > cssW * 0.72 ? "right" : "left";
      const lbl = (s.clipShortName || `#${s.sampleIndex}`).split("/").pop().slice(0, 14);
      ctx.fillText(`${lbl} ${Math.round(w * 100)}%`, sx + (sx > cssW * 0.72 ? -8 : 8), sy - 3);
    }
  }

  // Draggable cursor crosshair
  const cx = toX(Number(params[0] ?? 0)), cy = toY(Number(params[1] ?? 0));
  ctx.strokeStyle = "rgba(255, 217, 119, 0.75)";
  ctx.lineWidth = 1.2;
  ctx.beginPath();
  ctx.moveTo(cx - 9, cy); ctx.lineTo(cx + 9, cy);
  ctx.moveTo(cx, cy - 9); ctx.lineTo(cx, cy + 9);
  ctx.stroke();
  ctx.beginPath(); ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
  ctx.fillStyle = "#ffd977"; ctx.fill();
  ctx.strokeStyle = "#081012"; ctx.lineWidth = 1.2; ctx.stroke();

  // Axis labels
  const axX = node.axes?.[0]?.name || node.axes?.[0]?.hash || "X";
  const axY = node.axes?.[1]?.name || node.axes?.[1]?.hash || "Y";
  ctx.fillStyle = "#8aa5ab"; ctx.font = "9px monospace";
  ctx.textAlign = "center"; ctx.fillText(`${axX} [${xmin.toFixed(1)} .. ${xmax.toFixed(1)}]`, padL + plotW / 2, cssH - 6);
  ctx.save();
  ctx.translate(11, padT + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(`${axY} [${ymin.toFixed(1)} .. ${ymax.toFixed(1)}]`, 0, 0);
  ctx.restore();

  canvas._bsPlot = { dim, padL, padR, padT, padB, plotW, plotH, xmin, xmax, ymin, ymax, cssW, cssH };
}

function sampleWeightsToChildWeights(node, sampleWeights) {
  const byChild = {};
  for (const s of node.samples || []) {
    const sw = sampleWeights.get(s.sampleIndex) || 0;
    if (sw <= 1e-6) continue;
    const ci = s.childIndex ?? s.sampleIndex;
    byChild[ci] = (byChild[ci] || 0) + sw * (s.weight ?? 1.0);
    if (Array.isArray(s.extraChildren)) {
      for (const ec of s.extraChildren) {
        const eci = ec.childIndex;
        if (eci != null) byChild[eci] = (byChild[eci] || 0) + sw * (ec.weight ?? 0);
      }
    }
  }
  return byChild;
}

function updateBlendspaceEvaluation(rig) {
  const node = getActiveBsNode(rig);
  if (!node) return;
  const evalRes = evalBlendspaceNode(node, rig.bsParams);
  rig.bsWeights = evalRes.weights;
  const canvas = $("bsCanvas");
  if (canvas) drawBlendspaceCanvas(canvas, node, rig.bsParams, evalRes);
  // Sync sliders & outputs
  (node.axes || []).forEach((_ax, di) => {
    const sl = document.querySelector(`[data-bs-axis="${di}"]`);
    const out = document.querySelector(`[data-bs-axis-out="${di}"]`);
    const val = Number(rig.bsParams[di] ?? 0);
    if (sl && document.activeElement !== sl) sl.value = String(val);
    if (out) out.textContent = val.toFixed(2);
  });
  // Sync sample weight bars
  document.querySelectorAll("[data-bs-samp]").forEach((row) => {
    const si = Number(row.dataset.bsSamp);
    const w = evalRes.weights.get(si) || 0;
    row.classList.toggle("active", w > 0.01);
    const bar = row.querySelector(".bs-bar i");
    const pct = row.querySelector(".bs-pct");
    if (bar) bar.style.width = `${Math.round(w * 100)}%`;
    if (pct) pct.textContent = `${Math.round(w * 100)}%`;
  });
  // Push live weights into WebGL2 multi-clip mixer
  if (S.view?._blend && rig.bsLiveKey) {
    S.view.setBlendWeights(sampleWeightsToChildWeights(node, evalRes.weights));
  }
}

async function activateBlendspaceLive(item, rig, bs, node) {
  if (!rig?.skel || !bs || !node) return;
  const liveKey = `${bs.id}:${rig.bsSubIdx}:${rig.info.skeletonId}`;
  const tok = ++rig.bsLoadTok;
  // Collect unique resolved child clips
  const unique = new Map();
  for (const s of node.samples || []) {
    if (!s.resolved || !s.dictId || !s.clipHash) continue;
    const ci = s.childIndex ?? s.sampleIndex;
    if (!unique.has(ci)) {
      unique.set(ci, {
        childIndex: ci,
        dictId: s.dictId,
        clipHash: s.clipHash,
        childDuration: s.childDuration || s.clipDurationResolved || 1.0,
        blendDuration: s.blendDuration || s.clipDurationResolved || 1.0,
        timeCurve: s.timeCurve || null,
      });
    }
  }
  const list = Array.from(unique.values());
  if (!list.length) {
    status(`Blendspace ${bs.name}: no locally resolved child clips to stream`);
    return;
  }
  status(`Loading ${list.length} blendspace clips for ${bs.name}…`);
  try {
    const fetched = await Promise.all(list.map(async (c) => {
      try {
        const buffer = await fetchAnimClipBuffer(c.dictId, c.clipHash, rig.info.skeletonId);
        return { ...c, buffer };
      } catch {
        return null;
      }
    }));
    if (S.item !== item || S.rig !== rig || tok !== rig.bsLoadTok) return;
    const valid = fetched.filter(Boolean);
    if (!valid.length) {
      status(`Blendspace ${bs.name}: child clips failed to decode for this skeleton`);
      return;
    }
    const cw = sampleWeightsToChildWeights(node, rig.bsWeights);
    const entries = valid.map((c) => ({ ...c, weight: cw[c.childIndex] || 0 }));
    S.view.setBlendspace(entries, { name: bs.name });
    rig.bsLiveKey = liveKey;
    rig.activeAnim = null;
    updateBlendspaceEvaluation(rig);
    status(`Blending ${valid.length} clips live in ${bs.name} (${node.dimensions}D)`);
  } catch (e) {
    status(`Blendspace load error: ${e.message}`);
  }
}

async function refreshBlendspaceSearch(item) {
  const rig = S.rig;
  if (!rig || S.item !== item) return;
  const tok = ++rig.bsTok;
  const listEl = $("bsList");
  if (listEl) listEl.innerHTML = `<p class="note" style="padding:6px">Searching 17k parametric blendspaces…</p>`;
  try {
    const res = await api("blendspaces", {
      q: rig.bsQ || "",
      dims: rig.bsDims === "All" ? "" : rig.bsDims,
      domain: rig.bsDomain === "All" ? "" : rig.bsDomain,
      rig: rig.info.rigKind || "",
      skeletonId: rig.info.skeletonId || "",
      limit: 30,
      offset: rig.bsOffset || 0,
    });
    if (S.item !== item || S.rig !== rig || tok !== rig.bsTok) return;
    rig.bsTotal = res.total || 0;
    rig.bsItems = res.items || [];
    rig.bsDomains = res.domains || [];
    renderBlendspaceTab();
  } catch (e) {
    if (S.item !== item || S.rig !== rig || tok !== rig.bsTok) return;
    if (listEl) listEl.innerHTML = `<p class="note warn" style="padding:6px">Blendspaces unavailable: ${esc(e.message)}</p>`;
  }
}

async function openBlendspaceById(bsId) {
  const item = S.item, rig = S.rig;
  if (!item || !rig) return;
  const detailEl = $("bsDetail");
  if (detailEl) detailEl.innerHTML = `<div class="bs-panel"><p class="note">Parsing .motion blendspace…</p></div>`;
  try {
    const bs = await api("blendspace", { id: bsId });
    if (S.item !== item || S.rig !== rig) return;
    rig.activeBs = bs;
    rig.bsSubIdx = 0;
    if (bs.variantType === 2 && Array.isArray(bs.subMotions) && bs.subMotions.length) {
      let best = 0;
      for (let i = 1; i < bs.subMotions.length; i++) {
        if ((bs.subMotions[i].dimensions || 0) > (bs.subMotions[best].dimensions || 0)) best = i;
      }
      rig.bsSubIdx = best;
    }
    initBsParamsFromNode(rig);
    renderBlendspaceDetail();
    const node = getActiveBsNode(rig);
    if (node) activateBlendspaceLive(item, rig, bs, node);
  } catch (e) {
    if (detailEl) detailEl.innerHTML = `<div class="bs-panel"><p class="note warn">${esc(e.message)}</p></div>`;
  }
}

function initBsParamsFromNode(rig) {
  const node = getActiveBsNode(rig);
  if (!node) { rig.bsParams = []; return; }
  rig.bsParams = [];
  for (let d = 0; d < (node.dimensions || 0); d++) {
    const [lo, hi] = getBsAxisBounds(node, d);
    rig.bsParams.push(lo < 0 && hi > 0 ? 0 : 0.5 * (lo + hi));
  }
}

function renderBlendspaceDetail() {
  const rig = S.rig;
  const host = $("bsDetail");
  if (!host || !rig?.activeBs) { if (host) host.innerHTML = ""; return; }
  const bs = rig.activeBs;
  const node = getActiveBsNode(rig);
  if (!node) return;
  const dim = node.dimensions || 0;
  const resolvedN = node.resolvedSamples ?? (node.samples || []).filter((s) => s.resolved).length;
  const totalS = (node.samples || []).length;
  const subSel = (bs.variantType === 2 && bs.subMotions?.length > 1)
    ? `<div class="bs-sub-row"><span>Variation (${bs.subMotions.length})</span><select id="bsSubSelect">${bs.subMotions.map((sm, i) =>
        `<option value="${i}" ${i === rig.bsSubIdx ? "selected" : ""}>#${i} · ${sm.dimensions}D (${sm.samples?.length || 0} samples · ${(sm.axes || []).map((a) => a.name || a.hash).join(", ")})</option>`
      ).join("")}</select></div>`
    : "";
  const slidersHtml = (node.axes || []).map((ax, di) => {
    const [lo, hi] = getBsAxisBounds(node, di);
    const val = Number(rig.bsParams[di] ?? 0);
    const step = Math.max(0.001, (hi - lo) / 200);
    const lbl = ax.name || ax.hash || `axis_${di}`;
    return `<div class="bs-slider-row"><span title="${esc(lbl)} (${esc(ax.hash || "")})">${esc(lbl)}</span><input type="range" data-bs-axis="${di}" min="${lo}" max="${hi}" step="${step}" value="${val}"><output data-bs-axis-out="${di}">${val.toFixed(2)}</output></div>`;
  }).join("");

  const samplesHtml = (node.samples || []).map((s) => {
    const cStr = (s.coords || []).map((v) => Number(v).toFixed(2)).join(", ");
    const shortN = (s.clipShortName || s.clipHash || `#${s.sampleIndex}`).split("/").pop();
    const rateTxt = Math.abs((s.rate ?? 1) - 1) > 0.01 ? ` · ×${Number(s.rate).toFixed(2)}` : "";
    return `<button type="button" class="bs-samp ${s.resolved ? "" : "unres"}" data-bs-samp="${s.sampleIndex}" title="${esc(s.dictName || "")} / ${esc(s.clipShortName || "")} (${cStr})${rateTxt}"><span class="sname">${esc(shortN)} <small style="color:var(--dim)">(${esc(cStr)})</small></span><span class="bs-bar"><i style="width:0%"></i></span><span class="bs-pct">0%</span></button>`;
  }).join("");

  host.innerHTML = `
    <div class="bs-panel">
      <div class="bs-head">
        <strong>${esc(bs.name)}</strong>
        <span class="anim-tag dim">${dim}D</span>
        <span class="anim-tag">${resolvedN}/${totalS} clips</span>
        ${node.triangles?.length ? `<span class="anim-tag">${node.triangles.length} tris</span>` : ""}
      </div>
      ${subSel}
      <div class="bs-canvas-wrap"><canvas id="bsCanvas"></canvas></div>
      ${slidersHtml ? `<div class="bs-sliders">${slidersHtml}</div>` : ""}
      <div class="bs-samples">${samplesHtml}</div>
    </div>
  `;

  if ($("bsSubSelect")) {
    $("bsSubSelect").onchange = (e) => {
      rig.bsSubIdx = Number(e.target.value) || 0;
      initBsParamsFromNode(rig);
      renderBlendspaceDetail();
      const n2 = getActiveBsNode(rig);
      if (n2) activateBlendspaceLive(S.item, rig, bs, n2);
    };
  }

  host.querySelectorAll("[data-bs-axis]").forEach((sl) => {
    sl.oninput = () => {
      rig.bsParams[Number(sl.dataset.bsAxis)] = Number(sl.value);
      updateBlendspaceEvaluation(rig);
    };
  });

  host.querySelectorAll("[data-bs-samp]").forEach((btn) => {
    btn.onclick = () => {
      const si = Number(btn.dataset.bsSamp);
      const samp = (node.samples || []).find((s) => s.sampleIndex === si);
      if (samp && samp.coords?.length) {
        for (let d = 0; d < Math.min(dim, samp.coords.length); d++) rig.bsParams[d] = samp.coords[d];
        updateBlendspaceEvaluation(rig);
      }
    };
  });

  const canvas = $("bsCanvas");
  if (canvas) {
    const applyPtr = (ev) => {
      const p = canvas._bsPlot;
      if (!p || p.dim === 0) return;
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      if (p.dim === 1) {
        const u = Math.max(0, Math.min(1, (x - p.padL) / Math.max(1, p.cssW - p.padL - p.padR)));
        rig.bsParams[0] = p.xmin + u * (p.xmax - p.xmin);
      } else {
        const u = Math.max(0, Math.min(1, (x - p.padL) / Math.max(1, p.plotW)));
        const v = Math.max(0, Math.min(1, 1 - (y - p.padT) / Math.max(1, p.plotH)));
        rig.bsParams[0] = p.xmin + u * (p.xmax - p.xmin);
        rig.bsParams[1] = p.ymin + v * (p.ymax - p.ymin);
      }
      updateBlendspaceEvaluation(rig);
    };
    let dragging = false;
    canvas.onpointerdown = (ev) => { dragging = true; canvas.setPointerCapture(ev.pointerId); applyPtr(ev); };
    canvas.onpointermove = (ev) => { if (dragging) applyPtr(ev); };
    canvas.onpointerup = (ev) => { dragging = false; try { canvas.releasePointerCapture(ev.pointerId); } catch (_) {} };
  }

  updateBlendspaceEvaluation(rig);
}

function renderBlendspaceTab() {
  const rig = S.rig;
  const listEl = $("bsList");
  const pageEl = $("bsPage");
  const prevBtn = $("bsPrev");
  const nextBtn = $("bsNext");
  if (!rig || !listEl) return;

  $("bsDimPills")?.querySelectorAll("[data-bsdim]").forEach((b) => b.setAttribute("aria-pressed", rig.bsDims === b.dataset.bsdim));
  $("bsDomPills")?.querySelectorAll("[data-bsdom]").forEach((b) => b.setAttribute("aria-pressed", rig.bsDomain === b.dataset.bsdom));

  const items = rig.bsItems || [];
  if (!items.length) {
    listEl.innerHTML = `<p class="note" style="padding:6px">No matching .motion blendspaces.</p>`;
  } else {
    const activeId = rig.activeBs?.id;
    listEl.innerHTML = items.map((b) => {
      const axesTxt = (b.axes || []).slice(0, 3).join(", ");
      return `<button type="button" class="anim-row" data-bsid="${b.id}" aria-current="${b.id === activeId}" title="${esc(b.dictName)} / ${esc(b.name)}"><strong>${esc(b.name)}<span class="anim-tag dim">${b.dimensions}D</span>${b.subCount > 1 ? `<span class="anim-tag">×${b.subCount}</span>` : ""}</strong><small>${esc(axesTxt || b.dictName)} · ${b.sampleCount} samples · ${esc(b.domain)}</small></button>`;
    }).join("");
    listEl.querySelectorAll("[data-bsid]").forEach((btn) => {
      btn.onclick = () => {
        listEl.querySelectorAll("[data-bsid]").forEach((x) => x.setAttribute("aria-current", x === btn));
        openBlendspaceById(Number(btn.dataset.bsid));
      };
    });
  }
  const off = rig.bsOffset || 0;
  const tot = rig.bsTotal || 0;
  if (pageEl) pageEl.textContent = tot ? `${fmtN(off + 1)}–${fmtN(Math.min(tot, off + items.length))} / ${fmtN(tot)}` : "0 blendspaces";
  if (prevBtn) prevBtn.disabled = off <= 0;
  if (nextBtn) nextBtn.disabled = off + 30 >= tot;
  if (rig.activeBs) renderBlendspaceDetail();
}

// -------------------------------------------------------------------------------------------------
// Motion Matching (.motionmatcher): Search, Architecture Inspector & Candidate / Interval Playback
// -------------------------------------------------------------------------------------------------
async function refreshMotionMatcherSearch(item) {
  const rig = S.rig;
  if (!rig || S.item !== item) return;
  const tok = ++rig.mmTok;
  const listEl = $("mmList");
  if (listEl) listEl.innerHTML = `<p class="note" style="padding:6px">Loading Motion Matching databases…</p>`;
  try {
    const res = await api("motion-matchers", {
      q: rig.mmQ || "",
      rigCategory: rig.mmCat === "All" ? "" : rig.mmCat,
      mode: rig.mmMode || "",
      skeletonId: rig.info.skeletonId || "",
      limit: 100,
    });
    if (S.item !== item || S.rig !== rig || tok !== rig.mmTok) return;
    rig.mmTotal = res.total || 0;
    rig.mmItems = res.items || [];
    rig.mmCategories = res.categories || [];
    renderMotionMatcherTab();
  } catch (e) {
    if (S.item !== item || S.rig !== rig || tok !== rig.mmTok) return;
    if (listEl) listEl.innerHTML = `<p class="note warn" style="padding:6px">Motion matchers unavailable: ${esc(e.message)}</p>`;
  }
}

async function openMotionMatcherById(mmId) {
  const item = S.item, rig = S.rig;
  if (!item || !rig) return;
  const tok = ++rig.mmLoadTok;
  const detailEl = $("mmDetail");
  if (detailEl) detailEl.innerHTML = `<div class="mm-panel"><p class="note">Decoding .motionmatcher database…</p></div>`;
  try {
    const mm = await api("motion-matcher", { id: mmId });
    if (S.item !== item || S.rig !== rig || tok !== rig.mmLoadTok) return;
    rig.activeMm = mm;
    renderMotionMatcherDetail();
  } catch (e) {
    if (detailEl) detailEl.innerHTML = `<div class="mm-panel"><p class="note warn">${esc(e.message)}</p></div>`;
  }
}

function renderMotionMatcherDetail() {
  const rig = S.rig;
  const host = $("mmDetail");
  if (!host || !rig?.activeMm) { if (host) host.innerHTML = ""; return; }
  const mm = rig.activeMm;
  const st = mm.stats || {};
  const rf = mm.rigFamily || {};
  const si = mm.searchIndex || {};
  const align = mm.alignmentSpace || {};
  const feats = mm.features || {};
  const channels = feats.channels || [];
  const isM3 = mm.mode === 3;

  const aabbSummary = isM3 && si.aabbLevels?.length
    ? si.aabbLevels.map((l) => `L${l.level}:${l.simdBoxes8}×8`).join(" → ")
    : `AoS fp16 (${si.blocksPerPose || 0} blk/pose)`;

  const alignTxt = align.rootBoneName
    ? `${align.rootBoneName}${align.alignmentBoneName ? ` / ${align.alignmentBoneName}` : ""}`
    : (align.mode != null ? `mode ${align.mode}` : "root");

  const chanHtml = channels.map((ch) => {
    if (ch.type === 1) {
      const subs = (ch.subFeatures || []).join("+");
      const wMax = ch.rawWeights?.length ? Math.max(...ch.rawWeights).toFixed(2) : "–";
      return `<button type="button" class="mm-chan" data-mm-bone="${esc(ch.boneName || "")}" data-mm-bhash="${esc(ch.boneHash || "")}" title="Click to highlight ${esc(ch.boneName || ch.boneHash)} in 3D"><span>${esc(ch.boneName || ch.boneHash)} <small>[${esc(subs)}]</small></span><small>${ch.dim}D · w=${wMax}</small></button>`;
    }
    const wMax = ch.rawWeights?.length ? Math.max(...ch.rawWeights).toFixed(2) : "–";
    return `<div class="mm-chan" style="cursor:default"><span>↗ ${esc(ch.semantic || ch.typeName)} <small>Δt=${ch.sampleDeltaSec ?? 0}s</small></span><small>${ch.dim}D · w=${wMax}</small></div>`;
  }).join("");

  const cands = (mm.candidates || []).slice().sort((a, b) => (b.resolved ? 1 : 0) - (a.resolved ? 1 : 0) || b.poseCount - a.poseCount);
  const candHtml = cands.slice(0, 80).map((c, ci) => {
    const title = c.clipShortName || c.clipHash;
    const ivsHtml = (c.resolved && c.intervals?.length)
      ? `<div class="mm-ivs">${c.intervals.slice(0, 8).map((iv) =>
          `<button type="button" class="mm-iv" data-mm-play="${ci}" data-mm-seek="${iv.startTime}" title="Jump to matched pose interval ${iv.startTime.toFixed(2)}s–${iv.endTime.toFixed(2)}s (${iv.poseCount} poses @ ${Math.round(iv.sampleRateHz || 30)}Hz)">${iv.startTime.toFixed(2)}s · ${iv.poseCount}p</button>`
        ).join("")}</div>`
      : "";
    return `<div class="mm-cand">
      <div class="mm-cand-top">
        <span class="mm-cand-name" title="${esc(c.dictName || "")} / ${esc(title)}">${esc(title)}</span>
        <button type="button" data-mm-play="${ci}" ${c.resolved && c.dictId ? "" : "disabled"}>${c.resolved && c.dictId ? "▶ Play" : "Unresolved"}</button>
      </div>
      <div class="mm-cand-sub">${esc(c.dictName || c.clipSetHash)} · ${c.poseCount} poses${c.clipDuration ? ` · ${c.clipDuration.toFixed(2)}s` : ""}${c.isLooping ? " · loop" : ""}</div>
      ${ivsHtml}
    </div>`;
  }).join("");

  host.innerHTML = `
    <div class="mm-panel">
      <div class="mm-head">
        <strong>${esc(mm.shortName || mm.name)}</strong>
        <span class="anim-tag ${isM3 ? "m3" : "m1"}">${isM3 ? "Mode 3 · SoA+AABB" : "Mode 1 · AoS"}</span>
        <span class="anim-tag">${esc(rf.clade || rf.category || "")}</span>
      </div>
      <div class="mm-stats">
        <div class="mm-stat"><small>Poses / Feature Dim</small><b>${fmtN(st.poseCount || 0)} × ${st.featureDim || 0}D fp16</b></div>
        <div class="mm-stat"><small>Playable Candidates</small><b>${st.resolvedCandidateCount || 0} / ${st.candidateCount || 0} clips</b></div>
        <div class="mm-stat"><small>Search Index</small><b>${esc(aabbSummary)}</b></div>
        <div class="mm-stat"><small>Alignment / Bones</small><b>${esc(alignTxt)} (${st.boneCount || 0}b)</b></div>
      </div>
      <details class="mm-sec" open>
        <summary>Feature channels (${channels.length} · click bone to highlight in 3D)</summary>
        <div class="mm-sec-body">${chanHtml || `<p class="note">No channels.</p>`}</div>
      </details>
      <details class="mm-sec" open>
        <summary>Candidate clips &amp; matched pose intervals (${cands.length})</summary>
        <div class="mm-sec-body" style="max-height:230px">${candHtml || `<p class="note">No candidates.</p>`}</div>
      </details>
    </div>
  `;

  host.querySelectorAll("[data-mm-bone]").forEach((btn) => {
    btn.onclick = () => highlightBoneIn3D(btn.dataset.mmBone, btn.dataset.mmBhash);
  });
  host.querySelectorAll("[data-mm-play]").forEach((btn) => {
    btn.onclick = () => {
      const c = cands[Number(btn.dataset.mmPlay)];
      if (!c || !c.resolved || !c.dictId) return;
      const seek = btn.dataset.mmSeek != null ? Number(btn.dataset.mmSeek) : undefined;
      loadAnimClip({
        dictId: c.dictId,
        clipHash: c.clipHash,
        name: c.clipShortName || c.clipHash,
        dictName: c.dictName || "",
        frames: c.clipFrames || 0,
      }, seek != null ? { seekTime: seek, playing: true } : {});
    };
  });
}

function renderMotionMatcherTab() {
  const rig = S.rig;
  const listEl = $("mmList");
  const countEl = $("mmCount");
  if (!rig || !listEl) return;

  $("mmCatPills")?.querySelectorAll("[data-mmcat]").forEach((b) => b.setAttribute("aria-pressed", rig.mmCat === b.dataset.mmcat));
  $("mmModePills")?.querySelectorAll("[data-mmmode]").forEach((b) => b.setAttribute("aria-pressed", rig.mmMode === b.dataset.mmmode));

  const items = rig.mmItems || [];
  if (countEl) countEl.textContent = `${fmtN(rig.mmTotal || items.length)} databases`;
  if (!items.length) {
    listEl.innerHTML = `<p class="note" style="padding:6px">No matching Motion Matching databases.</p>`;
  } else {
    const activeId = rig.activeMm?.id;
    listEl.innerHTML = items.map((m) => {
      const ovPct = Math.round((m.boneOverlap || 0) * 100);
      const ovTag = m.matchedBones > 0 ? `<span class="anim-tag ${m.speciesMatch ? "sp" : ""}">${ovPct}% rig</span>` : "";
      const mTag = `<span class="anim-tag ${m.mode === 3 ? "m3" : "m1"}">M${m.mode}</span>`;
      return `<button type="button" class="anim-row" data-mmid="${m.id}" aria-current="${m.id === activeId}" title="${esc(m.dictName)} / ${esc(m.name)}"><strong>${esc(m.name)}${mTag}${ovTag}</strong><small>${esc(m.rigSpecies || m.rigCategory)} · ${fmtN(m.poseCount)} poses · ${m.featureDim}D · ${m.resolvedCount}/${m.candidateCount} playable</small></button>`;
    }).join("");
    listEl.querySelectorAll("[data-mmid]").forEach((btn) => {
      btn.onclick = () => {
        listEl.querySelectorAll("[data-mmid]").forEach((x) => x.setAttribute("aria-current", x === btn));
        openMotionMatcherById(Number(btn.dataset.mmid));
      };
    });
  }
  if (rig.activeMm) renderMotionMatcherDetail();
}

function renderRigSection(item) {
  const host = $("rigBody");
  if (!host || !item?.rigInfo) return;
  const info = item.rigInfo;
  const rig = S.rig;
  const skelOn = !!S.view?.opts?.skeleton;
  const xrayOn = S.view?.opts?.skelXray !== false;
  const filt = S.view?.opts?.skelFilter || "core";
  const st = S.view?._animState || { playing: false, time: 0, speed: 1, loop: true, rootMotion: false };
  const tab = rig?.rigTab || "clips";

  host.innerHTML = `
    <div class="rig-summary" id="rigSummary"></div>
    <div class="skel-controls">
      <label><input type="checkbox" id="rigSkelOn" ${skelOn ? "checked" : ""}> Show skeleton</label>
      <label><input type="checkbox" id="rigSkelXray" ${xrayOn ? "checked" : ""}> X-Ray</label>
      <select id="rigSkelFilter" title="Bone groups drawn in 3D overlay and listed below">
        <option value="core" ${filt === "core" ? "selected" : ""}>Core (SKEL + GM)</option>
        <option value="face" ${filt === "face" ? "selected" : ""}>Core + Facial (FB)</option>
        <option value="all" ${filt === "all" ? "selected" : ""}>All bones (SKEL/BM/MH/PH/FB/GM)</option>
      </select>
    </div>
    <details class="bone-details">
      <summary>Bone hierarchy (<span id="boneCountLbl">0</span> joints · click to highlight in 3D)</summary>
      <div class="bone-details-body">
        <input type="search" id="boneSearch" placeholder="Filter bones (e.g. SKEL_Head, GM_, Spine)…" value="${esc(rig?.boneQ || "")}" spellcheck="false">
        <div class="bone-list" id="boneList"></div>
      </div>
    </details>
    ${info.canAnimate ? `
      <div class="anim-player" id="animPlayer">
        <div class="anim-now" id="animNow"><span>Bind pose (rest)</span><small>Pick a clip, blendspace, or MM DB</small></div>
        <div class="anim-transport">
          <button type="button" id="animPlay" disabled>▶ Play</button>
          <input type="range" id="animScrub" min="0" max="1000" step="1" value="0" disabled aria-label="Animation timeline">
          <span class="anim-time" id="animTime">Bind pose</span>
        </div>
        <div class="anim-opts">
          <label>Speed <select id="animSpeed">
            <option value="0.25" ${st.speed === 0.25 ? "selected" : ""}>0.25×</option>
            <option value="0.5" ${st.speed === 0.5 ? "selected" : ""}>0.5×</option>
            <option value="1" ${st.speed === 1 ? "selected" : ""}>1.0×</option>
            <option value="1.5" ${st.speed === 1.5 ? "selected" : ""}>1.5×</option>
            <option value="2" ${st.speed === 2 ? "selected" : ""}>2.0×</option>
          </select></label>
          <label><input type="checkbox" id="animLoop" ${st.loop ? "checked" : ""}> Loop</label>
          <label title="Apply native mover root translation & yaw delta"><input type="checkbox" id="animRootMotion" ${st.rootMotion ? "checked" : ""}> Root motion</label>
          <button type="button" id="animReset" style="margin-left:auto" disabled>Bind pose</button>
        </div>
      </div>
      <div class="rig-tabs" id="rigTabs" role="tablist">
        <button type="button" role="tab" data-rtab="clips" aria-selected="${tab === "clips"}">Clips</button>
        <button type="button" role="tab" data-rtab="blendspaces" aria-selected="${tab === "blendspaces"}">Blendspaces</button>
        <button type="button" role="tab" data-rtab="motionmatchers" aria-selected="${tab === "motionmatchers"}">Motion Match</button>
      </div>
      <div id="rigPaneClips" ${tab === "clips" ? "" : "hidden"}>
        <div class="anim-archives" id="animArchives"></div>
        <div class="anim-cats" id="animCats"></div>
        <div class="anim-search-bar">
          <input type="search" id="animSearch" placeholder="Search 418k clips (walk, run, story, animal…)" value="${esc(rig?.animQ || "")}" spellcheck="false">
        </div>
        <div class="anim-list" id="animList"></div>
        <div class="anim-pager">
          <button type="button" id="animPrev" disabled>‹ Prev</button>
          <span id="animPage">–</span>
          <button type="button" id="animNext" disabled>Next ›</button>
        </div>
      </div>
      <div id="rigPaneBs" ${tab === "blendspaces" ? "" : "hidden"}>
        <div class="anim-cats" id="bsDimPills">
          <button type="button" data-bsdim="All" aria-pressed="true">All Dims</button>
          <button type="button" data-bsdim="2" aria-pressed="false">2D</button>
          <button type="button" data-bsdim="1" aria-pressed="false">1D</button>
          <button type="button" data-bsdim="3+" aria-pressed="false">3D–5D</button>
        </div>
        <div class="anim-archives" id="bsDomPills">
          <button type="button" data-bsdom="All" aria-pressed="true">All</button>
          <button type="button" data-bsdom="human/gameplay" aria-pressed="false">Human</button>
          <button type="button" data-bsdom="animal/creature" aria-pressed="false">Animal</button>
          <button type="button" data-bsdom="vehicle/mount" aria-pressed="false">Vehicle</button>
        </div>
        <div class="anim-search-bar">
          <input type="search" id="bsSearch" placeholder="Search 17k blendspaces or axes (velocity, incline, yaw…)" value="${esc(rig?.bsQ || "")}" spellcheck="false">
        </div>
        <div class="anim-list" id="bsList" style="max-height:155px"></div>
        <div class="anim-pager">
          <button type="button" id="bsPrev" disabled>‹ Prev</button>
          <span id="bsPage">–</span>
          <button type="button" id="bsNext" disabled>Next ›</button>
        </div>
        <div id="bsDetail"></div>
      </div>
      <div id="rigPaneMm" ${tab === "motionmatchers" ? "" : "hidden"}>
        <div class="anim-cats" id="mmCatPills">
          <button type="button" data-mmcat="All" aria-pressed="true">All (180)</button>
          <button type="button" data-mmcat="human" aria-pressed="false">Human (90)</button>
          <button type="button" data-mmcat="animal" aria-pressed="false">Animal (90)</button>
        </div>
        <div class="anim-archives" id="mmModePills">
          <button type="button" data-mmmode="" aria-pressed="true">All Modes</button>
          <button type="button" data-mmmode="3" aria-pressed="false">Mode 3 · SoA+AABB</button>
          <button type="button" data-mmmode="1" aria-pressed="false">Mode 1 · AoS</button>
        </div>
        <div class="anim-search-bar">
          <input type="search" id="mmSearch" placeholder="Search human & animal Motion Matching DBs…" value="${esc(rig?.mmQ || "")}" spellcheck="false">
        </div>
        <div class="anim-list" id="mmList" style="max-height:155px"></div>
        <div class="anim-pager"><span id="mmCount">180 databases</span><span>Ranked by bone overlap</span></div>
        <div id="mmDetail"></div>
      </div>
    ` : `<p class="note">The animation index is not built. Run <code>python lineup.py --build</code> to enable real-time animations.</p>`}
  `;

  syncRigSummary();
  renderBoneList();
  syncAnimTransport(S.view._animState, S.view._activePlayback ? S.view._activePlayback() : S.view._anim);

  $("rigSkelOn").onchange = (e) => {
    S.view.set("skeleton", e.target.checked);
    if ($("skeleton")) $("skeleton").checked = e.target.checked;
  };
  $("rigSkelXray").onchange = (e) => S.view.set("skelXray", e.target.checked);
  $("rigSkelFilter").onchange = (e) => {
    S.view.set("skelFilter", e.target.value);
    renderBoneList();
  };
  $("boneSearch").oninput = (e) => {
    if (S.rig) S.rig.boneQ = e.target.value;
    renderBoneList();
  };

  if (info.canAnimate) {
    $("animPlay").onclick = () => {
      const act = S.view._activePlayback ? S.view._activePlayback() : S.view._anim;
      if (!act) return;
      const playing = !S.view._animState.playing;
      const patch = { playing };
      if (playing && S.view._animState.time >= (act.duration || 0) - 0.005) patch.time = 0;
      S.view.setAnimState(patch);
    };
    $("animReset").onclick = () => {
      S.view.resetBindPose();
      if (S.rig) { S.rig.activeAnim = null; S.rig.bsLiveKey = null; }
      updateDownloadLink(S.item, S.lod);
      $("animList")?.querySelectorAll(".anim-row").forEach((b) => b.setAttribute("aria-current", "false"));
    };
    $("animScrub").oninput = (e) => {
      const act = S.view._activePlayback ? S.view._activePlayback() : S.view._anim;
      if (!act) return;
      const t = (Number(e.target.value) / 1000) * (act.duration || 0);
      S.view.setAnimState({ time: t });
    };
    $("animSpeed").onchange = (e) => S.view.setAnimState({ speed: Number(e.target.value) || 1 });
    $("animLoop").onchange = (e) => S.view.setAnimState({ loop: e.target.checked });
    $("animRootMotion").onchange = (e) => S.view.setAnimState({ rootMotion: e.target.checked });

    // Tab switcher
    $("rigTabs").querySelectorAll("[data-rtab]").forEach((btn) => {
      btn.onclick = () => {
        if (!S.rig) return;
        const next = btn.dataset.rtab;
        S.rig.rigTab = next;
        $("rigTabs").querySelectorAll("[data-rtab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.rtab === next));
        $("rigPaneClips").hidden = next !== "clips";
        $("rigPaneBs").hidden = next !== "blendspaces";
        $("rigPaneMm").hidden = next !== "motionmatchers";
        if (next === "blendspaces") {
          if (!S.rig.bsItems) refreshBlendspaceSearch(item);
          else renderBlendspaceTab();
        } else if (next === "motionmatchers") {
          if (!S.rig.mmItems) refreshMotionMatcherSearch(item);
          else renderMotionMatcherTab();
        }
      };
    });

    // Clips tab wiring
    let aTimer = null;
    $("animSearch").oninput = (e) => {
      if (!S.rig) return;
      S.rig.animQ = e.target.value.trim();
      S.rig.animOffset = 0;
      clearTimeout(aTimer);
      aTimer = setTimeout(() => refreshAnimSearch(item), 220);
    };
    $("animPrev").onclick = () => {
      if (!S.rig) return;
      S.rig.animOffset = Math.max(0, (S.rig.animOffset || 0) - 40);
      refreshAnimSearch(item);
    };
    $("animNext").onclick = () => {
      if (!S.rig) return;
      S.rig.animOffset = (S.rig.animOffset || 0) + 40;
      refreshAnimSearch(item);
    };

    // Blendspaces tab wiring
    $("bsDimPills").querySelectorAll("[data-bsdim]").forEach((btn) => {
      btn.onclick = () => {
        if (!S.rig) return;
        S.rig.bsDims = btn.dataset.bsdim;
        S.rig.bsOffset = 0;
        refreshBlendspaceSearch(item);
      };
    });
    $("bsDomPills").querySelectorAll("[data-bsdom]").forEach((btn) => {
      btn.onclick = () => {
        if (!S.rig) return;
        S.rig.bsDomain = btn.dataset.bsdom;
        S.rig.bsOffset = 0;
        refreshBlendspaceSearch(item);
      };
    });
    let bsTimer = null;
    $("bsSearch").oninput = (e) => {
      if (!S.rig) return;
      S.rig.bsQ = e.target.value.trim();
      S.rig.bsOffset = 0;
      clearTimeout(bsTimer);
      bsTimer = setTimeout(() => refreshBlendspaceSearch(item), 220);
    };
    $("bsPrev").onclick = () => {
      if (!S.rig) return;
      S.rig.bsOffset = Math.max(0, (S.rig.bsOffset || 0) - 30);
      refreshBlendspaceSearch(item);
    };
    $("bsNext").onclick = () => {
      if (!S.rig) return;
      S.rig.bsOffset = (S.rig.bsOffset || 0) + 30;
      refreshBlendspaceSearch(item);
    };

    // Motion Matching tab wiring
    $("mmCatPills").querySelectorAll("[data-mmcat]").forEach((btn) => {
      btn.onclick = () => {
        if (!S.rig) return;
        S.rig.mmCat = btn.dataset.mmcat;
        refreshMotionMatcherSearch(item);
      };
    });
    $("mmModePills").querySelectorAll("[data-mmmode]").forEach((btn) => {
      btn.onclick = () => {
        if (!S.rig) return;
        S.rig.mmMode = btn.dataset.mmmode;
        refreshMotionMatcherSearch(item);
      };
    });
    let mmTimer = null;
    $("mmSearch").oninput = (e) => {
      if (!S.rig) return;
      S.rig.mmQ = e.target.value.trim();
      clearTimeout(mmTimer);
      mmTimer = setTimeout(() => refreshMotionMatcherSearch(item), 220);
    };

    if (rig?.animItems) renderAnimList();
    else if (rig) refreshAnimSearch(item);
  }
}

// Inspector ---------------------------------------------------------------------------------------
function kv(rows) {
  return `<dl class="kv">${rows.filter((r) => r && r[1] !== undefined).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}
const vec = (v) => v.map((x) => Number(x).toFixed(3)).join(", ");
function renderInspector(item, view, error) {
  const box = item.box, size = box ? box[1].map((x, i) => x - box[0][i]) : null;
  const d = item.decode, info = d?.info || {};
  const famKind = { Weapons: "Weapon", Animals: "Creature", Protagonists: "Character", Characters: "Character" }[item.category] || "Vehicle";
  const html = [];
  const pa = item.pedArchetype;
  html.push(`<section><h3>Identity</h3>${kv([
    ["Name", `<b>${esc(item.name)}</b>`], ["Name hash", esc(item.hash)], ["Category", esc(item.category)],
    ["Subcategory", esc(item.subcategory)], item.family ? [famKind, esc(item.family)] : null,
    [pa ? "Head drawable" : "Base name", esc(item.base_name)], item.variant ? ["Detail variant", esc(item.variant)] : null,
    pa ? ["Ped archetype", `#${pa.index} · <code>0x${esc(pa.archHex)}</code>${pa.shortName ? ` · short <code>${esc(pa.shortName)}</code>` : ""} · ${fmtN(pa.outfitCount)} outfits${pa.bodyRig ? ` · rig <code>${esc(pa.bodyRig)}</code>` : ""}`] : null,
  ])}</section>`);
  html.push(`<section><h3>Source</h3>${kv([
    ["Archive", esc(item.archive)], ["Resource kind", esc(item.kind)], ["Member offset", esc(item.member)],
    ["Drawable root", esc(item.root)], ["Indexed by", esc(item.source)],
    ["Index status", item.status === "indexed" ? "indexed" : `<span class="bad">${esc(item.status)}</span>`],
    item.error ? ["Index error", `<span class="bad">${esc(item.error)}</span>`] : null,
  ])}</section>`);
  const geo = [["Models / geometries", `${fmtN(item.models)} / ${fmtN(item.geometries)}`],
    ["LOD levels present", esc((item.lods || []).join(", ") || "–")]];
  if (view) geo.push([`Decoded (LOD ${S.lod})`, `${fmtN(view.vertices)} vertices · ${fmtN(view.triangles)} triangles`],
    ["Primitives", `${view.primitives}${view.withoutUV ? ` <span class="warn">(${view.withoutUV} without UVs)</span>` : ""}${view.withUV1 ? ` <small>(${view.withUV1} dual-UV)</small>` : ""}${view.withColor ? ` <small>(${view.withColor} vertex-colored)</small>` : ""}`]);
  if (d?.status === "decoded") geo.push(["Decode path", esc(info.decodePath || "direct")]);
  if (info.boundsOffset) geo.push(["Skeleton-root offset", `${vec(info.boundsOffset)} m <small>(matches drawable box to ${(info.boundsDimensionsErrorM * 100).toFixed(1)} cm)</small>`]);
  if (view?.atRootStub || info.atRootStub) geo.push(["Mesh note", `<span class="warn">At-root clothing proxy stub (auto-skipped in assemblies)</span>`]);
  if (error) geo.push(["Decode", `<span class="bad">${esc(error)}</span>`]);
  html.push(`<section><h3>Geometry</h3>${kv(geo)}</section>`);
  if (box) html.push(`<section><h3>Bounds (native, m)</h3>${kv([
    ["Min", vec(box[0])], ["Max", vec(box[1])], ["Size", vec(size)],
    item.archetype_box ? ["Archetype box", `${vec(item.archetype_box.slice(0, 3))} → ${vec(item.archetype_box.slice(3))}`] : null,
  ])}</section>`);
  html.push(`<section><h3>Source</h3>${kv([
    item.archetype_type != null ? ["Archetype class", "0x" + (item.archetype_type >>> 0).toString(16).padStart(8, "0")] : null,
    item.asset_type != null ? ["Asset type", esc(item.asset_type)] : null,
    ["Texture areas", esc(Object.entries(item.texture_areas || {}).map(([a, n]) => `${a} ${n}`).join(", ") || "–")],
    item.embeddedTextures?.length ? ["Embedded TXD", `${item.embeddedTextures.length} texture${item.embeddedTextures.length > 1 ? "s" : ""}`] : null,
  ])}</section>`);
  if (item.rigInfo) {
    html.push(`<section id="rigSec"><h3>Skeleton &amp; Animation</h3><div id="rigBody"></div></section>`);
  }
  if (window.CharEffectsStudio?.isHumanCharacter(item)) {
    html.push(`<section id="charFxSec"><h3>Character Effects &amp; Simulation Studio</h3><div id="charFxBody"></div></section>`);
  }
  if (item.characterAssembly && item.characterAssembly.parts?.length > 1) {
    const ca = item.characterAssembly;
    const isAnimal = ca.kind === "Animal";
    if (!isAnimal) {
      html.push(`<section id="groomSec"><h3>Hair &amp; Grooming</h3><div id="groomBody"><p class="note">Loading default hair grooming…</p></div></section>`);
    }
    const hasClothing = ca.parts.some((p) => p.slot === "Clothing");
    const rigNote = ca.body ? ` · rig <code>${esc(ca.body)}</code>` : "";
    const asmBtns = isAnimal
      ? `<button id="assembleChar">Assemble full animal</button>`
      : `<button id="assembleNude">Assemble nude body</button>${hasClothing ? `<button id="assembleChar">Equip character outfit</button>` : ""}`;
    const asmDesc = isAnimal
      ? `Structural assembly for <code>${esc(ca.character)}</code>.`
      : `Structural assembly for <code>${esc(ca.character)}</code>${rigNote}. Defaults to nude body (clothing unchecked until enabled).`;
    const nw = ca.nativeWardrobe || [];
    const nativeWbHtml = nw.length
      ? `<details class="muv-native-box" open><summary>${esc(ca.character)} 0DRP wardrobe (${nw.length} garments · character-native styles)</summary><div class="variant-list">${nw.map((g) =>
          `<div class="part-row"><input type="checkbox" data-part="${g.id}" title="Equip with ${esc(ca.character)} native MUV style (${esc(g.label)})" ${S.parts?.has(g.id) ? "checked" : ""}><button data-open="${g.id}" title="${esc(g.label)} (0x${esc(g.preset)})">${swatchDotsHtml(g.swatches, 9)}<span class="muv-gname">${esc(g.name)}</span><small>${esc(g.label)}</small></button></div>`
        ).join("")}</div></details>`
      : "";
    html.push(`<section><h3>${esc(ca.kind)} Assembly (${ca.parts.length} parts)</h3><p class="note">${asmDesc}</p><div class="row-actions">${asmBtns}<button id="clearChar">Clear assembly</button></div><div id="charOutfitsBox"></div><div class="variant-list">${ca.parts.map((p) =>
      `<div class="part-row" data-self="${p.id === item.id ? 1 : 0}">${p.id === item.id ? `<span class="part-self" title="Shown">●</span>` : `<input type="checkbox" data-part="${p.id}" title="Toggle part in viewport" ${S.parts?.has(p.id) ? "checked" : ""}>`}<button data-open="${p.id}" aria-current="${p.id === item.id}"><span class="asm-slot">${esc(p.slot)}</span>${esc(p.name)}</button></div>`
    ).join("")}</div>${nativeWbHtml}</section>`);
    if (ca.anatomy) {
      const an = ca.anatomy;
      const gens = an.genitals || [];
      const dets = an.details || [];
      const sp = an.skinPresets || {};
      const at = S.anatomyTex || {};
      const activeGen = gens.find((g) => S.parts?.has(g.id))?.id || (gens.find((g) => g.default)?.id && !S.parts?.size ? gens.find((g) => g.default).id : 0);
      const controls = [];
      if (gens.length) {
        const gOpts = [`<option value="0" ${activeGen === 0 ? "selected" : ""}>None (hidden)</option>`]
          .concat(gens.map((g) => `<option value="${g.id}" ${g.id === activeGen ? "selected" : ""}>${esc(g.label)}</option>`)).join("");
        controls.push(`<label><span>Genital geometry (${gens.length})</span><select id="anatGenital">${gOpts}</select></label>`);
      }
      const skinMenus = [
        ["eyeAlbedo", "Eye iris color"],
        ["upperAlbedo", "Upper body skin tone"],
        ["upperNormal", "Upper body normal / physique"],
        ["lowerAlbedo", "Lower body skin tone"],
        ["lowerNormal", "Lower body normal / physique"],
        ["genitalAlbedo", "Genital skin texture"],
        ["genitalNormal", "Genital normal map"],
      ];
      for (const [key, lbl] of skinMenus) {
        const list = sp[key];
        const cur = at[key] || "";
        if (!list?.length && !cur) continue;
        const rows = list || [];
        const hasCur = !cur || rows.some((v) => v.hash === cur);
        const defEntry = rows[0];
        const defText = defEntry
          ? `Rig default (${defEntry.label ? esc(defEntry.label) + " · " : ""}${esc(defEntry.hash.slice(0, 8))})`
          : "Rig default";
        const optParts = [`<option value="" ${!cur ? "selected" : ""}>${defText}</option>`];
        if (cur && !hasCur) {
          optParts.push(`<option value="${esc(cur)}" selected>Character Native · ${esc(cur.slice(0, 8))}</option>`);
        }
        for (let idx = 0; idx < rows.length; idx++) {
          const v = rows[idx];
          const tag = v.label ? `${v.label} · ${v.hash.slice(0, 8)}` : `#${idx + 1} · ${v.hash.slice(0, 8)}`;
          optParts.push(`<option value="${esc(v.hash)}" ${cur === v.hash ? "selected" : ""}>${esc(tag)} (${v.width}×${v.height} ${esc(v.format.replace(/_UNORM/, ""))})</option>`);
        }
        controls.push(`<label><span>${esc(lbl)} (${rows.length || 1})</span><select data-anattex="${esc(key)}">${optParts.join("")}</select></label>`);
      }
      const detHtml = dets.length
        ? `<div class="variant-list" style="margin-top:8px">${dets.map((d) =>
            `<div class="part-row"><input type="checkbox" data-part="${d.id}" title="Toggle anatomical detail in viewport" ${S.parts?.has(d.id) ? "checked" : ""}><button data-open="${d.id}"><span class="asm-slot">Detail</span>${esc(d.label)} <small>${esc(d.name)}</small></button></div>`
          ).join("")}</div>`
        : "";
      const ds = ca.defaultSkin;
      const swatch = ds?.skinRgb ? `<span style="display:inline-block;width:11px;height:11px;border-radius:50%;vertical-align:-1px;margin-right:5px;border:1px solid rgba(255,255,255,.35);background:rgb(${esc(ds.skinRgb)})"></span>` : "";
      const deInfo = ds?.neckDeltaE ? ` · Neck seam ΔE ${ds.neckDeltaE.before} → <b>${ds.neckDeltaE.after}</b>` : "";
      const nativeSkinNote = ds
        ? `<p class="note" style="margin-top:2px">${swatch}Character-native <code>0DRP</code> body skin, eyes &amp; physique normals auto-applied for <code>${esc(ca.character)}</code>${deInfo}.</p>`
        : "";
      const harmToggle = (ds?.upperGain || ds?.lowerGain)
        ? `<label class="proc-chk" style="margin:4px 0 6px;display:inline-flex;align-items:center;gap:6px;font-size:11px"><input type="checkbox" id="anatHarmonize" ${S.skinHarmonize !== false ? "checked" : ""}> Harmonize collar &amp; waist skin color seam</label>`
        : "";
      html.push(`<section><h3>Anatomy &amp; Skin Variants</h3><p class="note">Swap eye iris color, genital morphs, body skin tones, physique normals, nails, and body piercings for rig <code>${esc(ca.body)}</code>.</p>${nativeSkinNote}${harmToggle}${controls.length ? `<div class="anat-grid">${controls.join("")}</div>` : ""}${detHtml}</section>`);
    }
  }
  if (item.wheelSetup && S.wheels) {
    const ws = item.wheelSetup;
    const rimOpts = ws.rims.map((r) => `<option value="${r.id}" ${r.id === S.wheels.rimId ? "selected" : ""}>${esc(r.name)}</option>`).join("");
    const tyreOpts = ws.tyres.map((t) => `<option value="${t.id}" ${t.id === S.wheels.tyreId ? "selected" : ""}>${esc(t.name)} (r=${(t.unitRadius ?? 0).toFixed(3)}m)</option>`).join("");
    const axleTags = ws.axles.map((a) => a.bone.replace(/^wheel_/, "")).join(", ");
    html.push(`<section><h3>Wheels (${ws.axles.length})</h3><p class="note">Rims and tires are instanced onto native skeleton axles (${esc(axleTags)}) with physics-bound radii.</p><div class="parts-filter"><label><input type="checkbox" id="wheelToggle" ${S.wheels.enabled ? "checked" : ""}> Attach 3D wheels</label></div><div class="wheel-grid"><label>Rim style<select id="wheelRim">${rimOpts}</select></label><label>Tire profile<select id="wheelTyre">${tyreOpts}</select></label></div></section>`);
  }
  if (item.wardrobe) {
    const wb = item.wardrobe;
    const slotLabels = [
      ["top", "Tops & outerwear"], ["bottom", "Bottoms"], ["underwear", "Underwear & swimwear"],
      ["body", "Dresses & full-body"], ["shoes", "Shoes & boots"], ["headwear", "Accessories, hats & eyewear"],
      ["hands_feet", "Hands & feet"], ["hair", "Hair"], ["head", "Heads"],
    ];
    if (!S.wardrobeSlot || !(wb.counts?.[S.wardrobeSlot] > 0)) {
      S.wardrobeSlot = slotLabels.find(([k]) => wb.counts?.[k] > 0)?.[0] || "top";
    }
    const slotOpts = slotLabels.filter(([k]) => (wb.counts?.[k] || 0) > 0)
      .map(([k, lbl]) => `<option value="${k}" ${k === S.wardrobeSlot ? "selected" : ""}>${lbl} (${fmtN(wb.counts[k])})</option>`).join("");
    const actions = [];
    if (wb.starterOutfit?.length) actions.push(`<button id="equipOutfit" title="Overlay full standing-space starter outfit">Equip starter outfit</button>`);
    actions.push(`<button id="clearOutfit">Remove outfit pieces</button>`);
    html.push(`<section><h3>Wardrobe &amp; outfit (${esc(wb.rig)})</h3><p class="note">Compatible standing-space (<code>+1.0 m</code>) garments for body rig <code>${esc(wb.rig)}</code>. Equipped garments auto-apply character-native or default MUV textures.</p><div id="equippedStyles"></div><div class="row-actions">${actions.join("")}</div><div class="wardrobe-bar"><select id="wardrobeSlot">${slotOpts}</select><input type="search" id="wardrobeSearch" placeholder="Search ${esc(wb.rig)} garments…" spellcheck="false"></div><div class="variant-list" id="wardrobeList"></div><div class="wardrobe-pager" id="wardrobePager" hidden></div></section>`);
  }
  if (item.group.length > 1) {
    const label = item.family ? `${famKind} parts (${item.group.length})` : `Variants and copies (${item.group.length})`;
    const actions = [];
    if (["Weapons", "Animals", "Protagonists", "Characters"].includes(item.category) && item.family)
      actions.push(`<button id="assemble" title="Overlay default parts for this ${famKind.toLowerCase()}">Assemble default parts</button>`);
    actions.push(`<button id="clearParts">Remove added parts</button>`);
    const partNote = item.category === "Weapons" && item.family
      ? `<p class="note">Parts are overlaid at their stored origins. The game attaches them to weapon bones at runtime, so placement can differ.</p>`
      : (["Animals", "Protagonists", "Characters"].includes(item.category) && item.family
        ? `<p class="note">Anatomical parts align in standing skeleton space. Toggle optional layers or detail copies below.</p>` : "");
    const filterBar = item.group.length > 10
      ? `<div class="parts-filter"><input type="search" id="partSearch" placeholder="Filter ${item.group.length} parts…" spellcheck="false"><label><input type="checkbox" id="hideLow" checked> Hide low-detail</label></div>` : "";
    html.push(`<section><h3>${label}</h3><div class="row-actions">${actions.join("")}</div>${partNote}${filterBar}<div class="variant-list" id="partList">${item.group.map((g) =>
      `<div class="part-row" data-pname="${esc(g.name.toLowerCase())}" data-low="${g.low_detail ? 1 : 0}" data-self="${g.id === item.id ? 1 : 0}">${g.id === item.id ? `<span class="part-self" title="Shown">●</span>` : `<input type="checkbox" data-part="${g.id}" title="Add to the viewport" ${S.parts?.has(g.id) ? "checked" : ""}>`}<button data-open="${g.id}" aria-current="${g.id === item.id}">${esc(g.name)} <small>${esc(g.archive)} ${esc(g.member)}${g.low_detail ? " · low detail" : ""}${g.status !== "indexed" ? " · " + esc(g.status) : ""}</small></button></div>`).join("")}</div></section>`);
  }
  html.push(`<section id="materials"><h3>Materials (${item.materials.length})</h3><div id="materialList"></div></section>`);
  $("inspect").innerHTML = html.join("");
  if (S.groomState && S.groomState.itemId === item.id) renderGroomSection();
  if (S.charOutfits) renderCharOutfitsSection();
  for (const b of $("inspect").querySelectorAll("[data-open]")) b.onclick = () => openModel(Number(b.dataset.open));
  for (const c of $("inspect").querySelectorAll("[data-part]")) c.onchange = () => togglePart(Number(c.dataset.part), c.checked);
  if ($("assembleNude")) $("assembleNude").onclick = () => assembleCharacter(item, { nudeOnly: true });
  if ($("assembleChar")) $("assembleChar").onclick = () => assembleCharacter(item, { nudeOnly: false });
  if ($("clearChar")) $("clearChar").onclick = () => {
    for (const p of item.characterAssembly?.parts || []) {
      if (p.id !== item.id && S.parts.has(p.id)) togglePart(p.id, false);
    }
    for (const g of item.characterAssembly?.anatomy?.genitals || []) {
      if (g.id !== item.id && S.parts.has(g.id)) togglePart(g.id, false);
    }
    for (const d of item.characterAssembly?.anatomy?.details || []) {
      if (d.id !== item.id && S.parts.has(d.id)) togglePart(d.id, false);
    }
    if ($("anatGenital")) $("anatGenital").value = "0";
    if (S.item === item) S.view.fit();
  };
  if ($("anatGenital") && item.characterAssembly?.anatomy?.genitals?.length) {
    const gens = item.characterAssembly.anatomy.genitals;
    $("anatGenital").onchange = async (e) => {
      const nextId = Number(e.target.value) || 0;
      for (const g of gens) {
        if (g.id !== nextId && S.parts.has(g.id)) await togglePart(g.id, false);
      }
      if (nextId && !S.parts.has(nextId)) await togglePart(nextId, true);
    };
  }
  if ($("anatHarmonize")) {
    $("anatHarmonize").onchange = (e) => {
      S.skinHarmonize = !!e.target.checked;
      for (const [pid, part] of S.partMeta.entries()) {
        if (S.parts.has(pid) && part) loadPartMaterials(pid, part, "part" + pid);
      }
    };
  }
  for (const sel of $("inspect").querySelectorAll("[data-anattex]")) {
    sel.onchange = (e) => {
      const key = sel.dataset.anattex;
      S.anatomyTex = S.anatomyTex || {};
      if (e.target.value) S.anatomyTex[key] = e.target.value;
      else delete S.anatomyTex[key];
      for (const [pid, part] of S.partMeta.entries()) {
        if (S.parts.has(pid) && part) loadPartMaterials(pid, part, "part" + pid);
      }
    };
  }
  if ($("assemble")) $("assemble").onclick = () => assembleDefaults(item);
  if ($("clearParts")) $("clearParts").onclick = () => { for (const id of [...S.parts.keys()]) togglePart(id, false); };
  if ($("wheelToggle") && item.wheelSetup && S.wheels) {
    $("wheelToggle").onchange = (e) => { S.wheels.enabled = e.target.checked; applyWheels(item, false); };
    $("wheelRim").onchange = (e) => { S.wheels.rimId = Number(e.target.value); if (S.wheels.enabled) applyWheels(item, false); };
    $("wheelTyre").onchange = (e) => { S.wheels.tyreId = Number(e.target.value); if (S.wheels.enabled) applyWheels(item, false); };
  }
  if ($("wardrobeList") && item.wardrobe) {
    const wb = item.wardrobe;
    let wTok = 0, wTimer = null, wRows = [], wTotal = 0;
    const WB_PAGE = 250;
    const drawRows = (rows, total) => {
      const host = $("wardrobeList");
      const pager = $("wardrobePager");
      if (!host) return;
      if (!rows.length) {
        host.innerHTML = `<p class="note" style="padding:6px">No matching garments.</p>`;
        if (pager) pager.hidden = true;
        return;
      }
      host.innerHTML = rows.map((g) => `<div class="part-row" data-self="${g.id === item.id ? 1 : 0}">${g.id === item.id ? `<span class="part-self" title="Shown">●</span>` : `<input type="checkbox" data-part="${g.id}" title="Overlay garment in viewport" ${S.parts?.has(g.id) ? "checked" : ""}>`}<button data-open="${g.id}" aria-current="${g.id === item.id}">${esc(g.name)} <small>${esc(g.subcategory || "")}</small></button></div>`).join("");
      for (const b of host.querySelectorAll("[data-open]")) b.onclick = () => openModel(Number(b.dataset.open));
      for (const c of host.querySelectorAll("[data-part]")) c.onchange = () => togglePart(Number(c.dataset.part), c.checked);
      if (pager) {
        if (rows.length < total) {
          const rem = total - rows.length;
          pager.hidden = false;
          pager.innerHTML = `<span>Showing ${fmtN(rows.length)} of ${fmtN(total)}</span><button type="button" id="wbMore">Load +${Math.min(WB_PAGE, rem)}</button><button type="button" id="wbAll">Show all (${fmtN(total)})</button>`;
          $("wbMore").onclick = () => fetchWardrobe(rows.length, WB_PAGE, true);
          $("wbAll").onclick = () => fetchWardrobe(rows.length, Math.min(2000, rem), true);
        } else {
          pager.hidden = false;
          pager.innerHTML = `<span>Showing all ${fmtN(total)} items</span>`;
        }
      }
    };
    const fetchWardrobe = async (offset = 0, limit = WB_PAGE, append = false) => {
      const slot = $("wardrobeSlot").value;
      const q = $("wardrobeSearch").value.trim();
      S.wardrobeSlot = slot;
      const myTok = ++wTok;
      try {
        const res = await api("wardrobe", { rig: wb.rig, slot, q, limit, offset });
        if (myTok !== wTok || S.item !== item) return;
        const batch = res.items || [];
        wTotal = res.total ?? batch.length;
        wRows = append ? wRows.concat(batch) : batch;
        drawRows(wRows, wTotal);
      } catch {
        // Fallback to embedded initial slice if server hasn't been restarted yet
        if (!append && !q && wb.slots?.[slot]) {
          wRows = wb.slots[slot];
          wTotal = wb.counts?.[slot] || wRows.length;
          drawRows(wRows, wTotal);
        }
      }
    };
    const refreshWardrobe = () => fetchWardrobe(0, WB_PAGE, false);
    $("wardrobeSlot").onchange = refreshWardrobe;
    $("wardrobeSearch").oninput = () => { clearTimeout(wTimer); wTimer = setTimeout(refreshWardrobe, 200); };
    if ($("equipOutfit")) {
      $("equipOutfit").onclick = async () => {
        const toAdd = (wb.starterOutfit || []).filter((id) => id !== item.id && !S.parts.has(id));
        await Promise.all(toAdd.map((id) => togglePart(id, true)));
        if (S.item === item) S.view.fit();
      };
    }
    if ($("clearOutfit")) {
      $("clearOutfit").onclick = () => {
        const groupIds = new Set((item.group || []).map((g) => g.id));
        for (const id of [...S.parts.keys()]) {
          if (!groupIds.has(id)) togglePart(id, false);
        }
        if (S.item === item) S.view.fit();
      };
    }
    refreshWardrobe();
  }
  if ($("partSearch")) {
    const applyFilter = () => {
      const q = $("partSearch").value.trim().toLowerCase();
      const hideLow = $("hideLow").checked;
      for (const row of $("partList").children) {
        const isSelf = row.dataset.self === "1";
        const matchQ = !q || row.dataset.pname.includes(q);
        const matchLow = !hideLow || row.dataset.low !== "1" || isSelf;
        row.hidden = !(matchQ && matchLow);
      }
    };
    $("partSearch").oninput = applyFilter;
    $("hideLow").onchange = applyFilter;
    applyFilter();
  }
  if (item.rigInfo) renderRigSection(item);
  if (window.CharEffectsStudio?.isHumanCharacter(item)) {
    window.CharEffectsStudio.render(item, {
      api,
      view: S.view,
      state: S,
      togglePart,
      loadAnimClip,
      textureImage,
      curTexRes,
      status,
      getPartAlbedoHash: (pid, meta, m) => {
        const pnm = (meta.name || "").toLowerCase();
        const at = S.anatomyTex || {};
        if (pnm.startsWith("hand_") && at.upperAlbedo) return at.upperAlbedo;
        if (pnm.startsWith("feet_") && at.lowerAlbedo) return at.lowerAlbedo;
        const st = materialState(m, meta.name, meta.category);
        return st.pick ? texHash(st.pick.slot) : null;
      },
    });
  }
  renderMaterials(item);
  renderEquippedClothingStyles();
}

function renderEquippedClothingStyles() {
  const host = $("equippedStyles");
  if (!host) return;
  const item = S.item;
  const charName = item?.characterAssembly?.character || "";
  const charPreMap = item?.characterAssembly?.clothingPresets || {};
  const equipped = [];
  for (const [pid, meta] of (S.partMeta?.entries() || [])) {
    if (!S.parts?.has(pid) || !meta) continue;
    const cp = meta.clothingPresets;
    const tv = meta.textureVariants;
    if ((cp && cp.variants?.length) || (tv && (tv.albedos?.length || 0) > 1)) {
      equipped.push({ id: pid, meta, cp, tv });
    }
  }
  if (!equipped.length) {
    host.innerHTML = "";
    return;
  }
  host.innerHTML = `<div class="muv-equipped-box"><div class="proc-head"><h4>Equipped Clothing Styles (${equipped.length})</h4><span class="pass-badge pass-tint">MUV live</span></div>${equipped.map(({ id, meta, cp, tv }) => {
    const pnm = (meta.name || "").toLowerCase();
    const charPre = charPreMap[pnm] || "";
    const curPre = S.muvPresets.get(id) || charPre || cp?.defaultPreset || "";
    if (cp && cp.variants?.length) {
      // Pin character-native preset first if present
      const vars = cp.variants.slice();
      const natIdx = charPre ? vars.findIndex((v) => v.id === charPre) : -1;
      if (natIdx > 0) {
        const [nv] = vars.splice(natIdx, 1);
        vars.unshift(nv);
      } else if (charPre && natIdx < 0) {
        const nwEntry = (item?.characterAssembly?.nativeWardrobe || []).find((x) => x.name.toLowerCase() === pnm);
        vars.unshift({
          id: charPre,
          label: nwEntry?.label || `0x${charPre}`,
          swatches: nwEntry?.swatches || ["#808080"],
          hasPattern: !!nwEntry?.hasPattern,
        });
      }
      const activeVar = vars.find((v) => v.id === curPre) || vars[0];
      const isNative = charPre && curPre === charPre;
      const badge = isNative ? `<span class="muv-nat-badge" title="Character's own 0DRP preset">${esc(charName)} native</span>` : "";
      const opts = vars.map((v, idx) => {
        const prefix = (charPre && v.id === charPre) ? `★ ${charName} · ` : `#${idx + 1} · `;
        return `<option value="${esc(v.id)}" ${v.id === curPre ? "selected" : ""}>${esc(prefix + v.label)} (${esc(v.id)})</option>`;
      }).join("");
      const quickChips = vars.slice(0, 12).map((v) =>
        `<button type="button" class="muv-chip${v.id === curPre ? " active" : ""}" data-eqchip="${id}" data-pre="${esc(v.id)}" title="${esc(v.label)} (0x${esc(v.id)})">${swatchDotsHtml(v.swatches, 9)}${v.hasPattern ? `<span class="muv-pat">✦</span>` : ""}</button>`
      ).join("");
      return `<div class="muv-eq-row"><div class="muv-eq-head">${swatchDotsHtml(activeVar?.swatches, 10)}<strong title="${esc(meta.name)}">${esc(meta.name)}</strong>${badge}<span class="mono">${vars.length}</span></div><select data-eqpre="${id}">${opts}</select><div class="muv-chip-bar">${quickChips}</div></div>`;
    }
    const albs = tv?.albedos || [];
    return `<div class="muv-eq-row"><div class="muv-eq-head"><strong>${esc(meta.name)}</strong><span class="mono">${albs.length} tex</span></div><select data-eqalb="${id}">${albs.map((v, i) =>
      `<option value="${esc(v.hash)}">#${i + 1} · ${esc(v.hash.slice(0, 8))} (${v.width}×${v.height})</option>`
    ).join("")}</select></div>`;
  }).join("")}</div>`;

  const applyPartPreset = (pid, pre) => {
    const part = S.partMeta.get(pid);
    if (!part || !S.parts.has(pid)) return;
    S.muvPresets.set(pid, pre);
    loadPartMaterials(pid, part, "part" + pid);
    renderEquippedClothingStyles();
  };
  host.querySelectorAll("[data-eqpre]").forEach((sel) => {
    sel.onchange = () => applyPartPreset(Number(sel.dataset.eqpre), sel.value);
  });
  host.querySelectorAll("[data-eqchip]").forEach((btn) => {
    btn.onclick = () => applyPartPreset(Number(btn.dataset.eqchip), btn.dataset.pre);
  });
  host.querySelectorAll("[data-eqalb]").forEach((sel) => {
    sel.onchange = () => {
      const pid = Number(sel.dataset.eqalb);
      const part = S.partMeta.get(pid);
      const h = sel.value;
      if (!part || !S.parts.has(pid) || !h) return;
      for (const m of part.materials || []) {
        S.view.setMaterial(m.index, { texMode: 0 }, "part" + pid);
        textureImage(h, curTexRes(), pid).then((img) => {
          if (S.parts.has(pid)) S.view.setTexture(m.index, img, "part" + pid);
        }).catch(() => {});
      }
    };
  });
}

function roleBadge(t) {
  let out = "";
  if (t.role) {
    const cls = DIFFUSE_ROLES.has(t.role) || /layer \d diffuse/i.test(t.role) ? "role" : (NORMAL_ROLES.has(t.role) || /layer \d (normal|bump)/i.test(t.role) ? "role norm" : "role other");
    out += ` · <span class="${cls}" title="Native SGD5 parameter: ${esc(t.spelling || "")}">${esc(t.role)}</span>`;
  }
  if (t.derivedFrom) {
    out += ` · <span class="role other" title="Resolved from ${esc(t.derivedFrom)} (${esc(t.effectiveHash)})">derived: ${esc(t.derivedFrom)}</span>`;
  }
  return out;
}

function fmtParamVal(v) {
  if (Array.isArray(v)) return v.map((x) => Number(x).toFixed(Math.abs(x) >= 100 ? 1 : 3)).join(", ");
  return typeof v === "number" ? Number(v).toFixed(Math.abs(v) >= 100 ? 1 : 3) : String(v);
}

function renderProceduralStudio(item, host) {
  const proc = S.view?.procedural;
  if (!proc) return;
  const hasVehLut = item.materials.some((m) => m.texMode === 6) || !!item._hasVehLutPart;
  const hasWeapon = item.category === "Weapons" || item.materials.some((m) => m.texMode === 7) || !!item._hasWeaponPart;

  if (hasVehLut) {
    const lutHash = item.materials.find((m) => m.lutHash)?.lutHash || "fbc2b8d31c2f4684";
    const lutEntry = S.view._lutArrays?.get(lutHash) || null;
    const slices = lutEntry?.slices || 0;
    const box = document.createElement("div");
    box.className = "proc-studio";
    const lutBadge = slices >= 6
      ? `<span class="pass-badge pass-tint" title="Native 128×256×${slices} vehicle LUT (${esc(lutHash)})">${slices}-slice LUT</span>`
      : (slices === 1
        ? `<span class="pass-badge pass-decal" title="Slice 0 loaded; restart Lineup for full 8-slice LUT">slice 0 fallback</span>`
        : `<span class="pass-badge pass-tint">loading LUT…</span>`);

    const swatch1 = sampleLutColor(lutHash, proc.vehPaintSlice, proc.vehPaintRow1) || proc.vehPrimary;
    const swatch2 = sampleLutColor(lutHash, proc.vehPaintSlice, proc.vehPaintRow2) || proc.vehSecondary;

    box.innerHTML = `<div class="proc-head"><h4>Vehicle Procedural Studio</h4>${lutBadge}</div>
      <div class="proc-presets">${VEH_PAINT_PRESETS.map((p, idx) =>
        `<button type="button" data-vpre="${idx}" aria-pressed="${proc.vehPaintRow1 === p.row1 && proc.vehPaintSlice === p.slice && (proc.vehTwoTone | 0) === (p.twoTone | 0)}"><span class="swatch" style="background:${rgbToHex(p.primary)}"></span>${esc(p.name)}</button>`
      ).join("")}</div>
      <div class="proc-grid">
        <label><span>Primary row</span><input type="range" data-v="vehPaintRow1" min="0" max="255" step="1" value="${proc.vehPaintRow1}"><span class="proc-swatch-val"><span class="swatch" data-sw1 style="background:${rgbToHex(swatch1)}"></span><span class="mono" data-lbl="vehPaintRow1">${proc.vehPaintRow1}</span></span></label>
        <label><span>Secondary row</span><input type="range" data-v="vehPaintRow2" min="0" max="255" step="1" value="${proc.vehPaintRow2}"><span class="proc-swatch-val"><span class="swatch" data-sw2 style="background:${rgbToHex(swatch2)}"></span><span class="mono" data-lbl="vehPaintRow2">${proc.vehPaintRow2}</span></span></label>
        <label><span>Paint style</span><select data-vi="vehTwoTone">
          <option value="0" ${(proc.vehTwoTone | 0) === 0 ? "selected" : ""}>Uniform Body</option>
          <option value="1" ${(proc.vehTwoTone | 0) === 1 ? "selected" : ""}>Two-Tone · Cruiser / Sheriff</option>
          <option value="2" ${(proc.vehTwoTone | 0) === 2 ? "selected" : ""}>Two-Tone · Roof &amp; Trim</option>
        </select></label>
        <label><span>Paint finish</span><select data-vsel="vehPaintSlice">
          <option value="1" ${proc.vehPaintSlice === 1 ? "selected" : ""}>Slice 1 · Metallic</option>
          <option value="2" ${proc.vehPaintSlice === 2 ? "selected" : ""}>Slice 2 · Deep Clearcoat</option>
          <option value="5" ${proc.vehPaintSlice === 5 ? "selected" : ""}>Slice 5 · Satin / Matte</option>
        </select></label>
        <label><span>Interior trim</span><select data-vi="vehInterior">
          <option value="0" ${(proc.vehInterior | 0) === 0 ? "selected" : ""}>Charcoal Black</option>
          <option value="1" ${(proc.vehInterior | 0) === 1 ? "selected" : ""}>Slate Grey</option>
          <option value="2" ${(proc.vehInterior | 0) === 2 ? "selected" : ""}>Saddle Tan</option>
          <option value="3" ${(proc.vehInterior | 0) === 3 ? "selected" : ""}>Cream Leather</option>
          <option value="4" ${(proc.vehInterior | 0) === 4 ? "selected" : ""}>Oxblood Red</option>
        </select></label>
        <label><span>Pearlescent</span><input type="range" data-vf="vehPearl" min="0" max="1" step="0.05" value="${proc.vehPearl}"><span class="mono" data-lbl="vehPearl">${Math.round(proc.vehPearl * 100)}%</span></label>
        <label><span>Clearcoat</span><input type="range" data-vf="vehClearcoat" min="0" max="1" step="0.05" value="${proc.vehClearcoat}"><span class="mono" data-lbl="vehClearcoat">${Math.round(proc.vehClearcoat * 100)}%</span></label>
        <label><span>Dirt / Dust</span><input type="range" data-vf="vehDirt" min="0" max="1" step="0.05" value="${proc.vehDirt}"><span class="mono" data-lbl="vehDirt">${Math.round(proc.vehDirt * 100)}%</span></label>
        <label><span>Sun wear</span><input type="range" data-vf="vehWear" min="0" max="1" step="0.05" value="${proc.vehWear}"><span class="mono" data-lbl="vehWear">${Math.round(proc.vehWear * 100)}%</span></label>
        <label class="proc-chk"><input type="checkbox" data-vchk="vehLutExact" ${proc.vehLutExact ? "checked" : ""}> Raw LUT row (debug: no panel/interior remap)</label>
      </div>
      ${slices === 1 ? `<p class="note" style="margin:6px 0 0">Restart Lineup to unlock the full 8-slice 2D array LUT (<code>/api/models/tex-array</code>) &amp; shader constant buffers.</p>` : ""}`;

    const syncSwatches = () => {
      const c1 = sampleLutColor(lutHash, proc.vehPaintSlice, proc.vehPaintRow1) || proc.vehPrimary;
      const c2 = sampleLutColor(lutHash, proc.vehPaintSlice, proc.vehPaintRow2) || proc.vehSecondary;
      const sw1 = box.querySelector("[data-sw1]"), sw2 = box.querySelector("[data-sw2]");
      if (sw1) sw1.style.background = rgbToHex(c1);
      if (sw2) sw2.style.background = rgbToHex(c2);
    };

    box.querySelectorAll("[data-vpre]").forEach((btn) => {
      btn.onclick = () => {
        const p = VEH_PAINT_PRESETS[Number(btn.dataset.vpre)];
        S.view.setProcedural({
          vehPrimary: p.primary, vehSecondary: p.secondary, vehPearlCol: p.pearl,
          vehPaintRow1: p.row1, vehPaintRow2: p.row2, vehPaintSlice: p.slice,
          vehTwoTone: p.twoTone || 0,
        });
        box.querySelectorAll("[data-vpre]").forEach((b) => b.setAttribute("aria-pressed", b === btn));
        const r1 = box.querySelector('[data-v="vehPaintRow1"]'), r2 = box.querySelector('[data-v="vehPaintRow2"]');
        const sel = box.querySelector('[data-vsel="vehPaintSlice"]');
        const tt = box.querySelector('[data-vi="vehTwoTone"]');
        if (r1) { r1.value = String(p.row1); box.querySelector('[data-lbl="vehPaintRow1"]').textContent = String(p.row1); }
        if (r2) { r2.value = String(p.row2); box.querySelector('[data-lbl="vehPaintRow2"]').textContent = String(p.row2); }
        if (sel) sel.value = String(p.slice);
        if (tt) tt.value = String(p.twoTone || 0);
        syncSwatches();
      };
    });
    box.querySelectorAll("[data-v]").forEach((inp) => {
      inp.oninput = () => {
        const k = inp.dataset.v, val = Number(inp.value);
        const patch = { [k]: val };
        const sampled = sampleLutColor(lutHash, proc.vehPaintSlice, val);
        if (sampled) patch[k === "vehPaintRow1" ? "vehPrimary" : "vehSecondary"] = sampled;
        S.view.setProcedural(patch);
        const lbl = box.querySelector(`[data-lbl="${k}"]`);
        if (lbl) lbl.textContent = String(val);
        box.querySelectorAll("[data-vpre]").forEach((b) => b.setAttribute("aria-pressed", "false"));
        syncSwatches();
      };
    });
    const sliceSel = box.querySelector('[data-vsel="vehPaintSlice"]');
    if (sliceSel) {
      sliceSel.onchange = () => {
        const sl = Number(sliceSel.value);
        const patch = { vehPaintSlice: sl };
        const s1 = sampleLutColor(lutHash, sl, proc.vehPaintRow1);
        const s2 = sampleLutColor(lutHash, sl, proc.vehPaintRow2);
        if (s1) patch.vehPrimary = s1;
        if (s2) patch.vehSecondary = s2;
        S.view.setProcedural(patch);
        syncSwatches();
      };
    }
    box.querySelectorAll("[data-vi]").forEach((sel) => {
      sel.onchange = () => {
        const k = sel.dataset.vi;
        S.view.setProcedural({ [k]: Number(sel.value) });
        if (k === "vehTwoTone") box.querySelectorAll("[data-vpre]").forEach((b) => b.setAttribute("aria-pressed", "false"));
      };
    });
    box.querySelectorAll("[data-vf]").forEach((inp) => {
      inp.oninput = () => {
        const k = inp.dataset.vf, val = Number(inp.value);
        S.view.setProcedural({ [k]: val });
        const lbl = box.querySelector(`[data-lbl="${k}"]`);
        if (lbl) lbl.textContent = `${Math.round(val * 100)}%`;
      };
    });
    const exactChk = box.querySelector('[data-vchk="vehLutExact"]');
    if (exactChk) exactChk.onchange = () => S.view.setProcedural({ vehLutExact: exactChk.checked });
    host.append(box);
  }

  if (hasWeapon) {
    const box = document.createElement("div");
    box.className = "proc-studio";
    box.innerHTML = `<div class="proc-head"><h4>Weapon Procedural Studio</h4><span class="pass-badge pass-tint">zone PBR + liveries</span></div>
      <div class="proc-presets">${WPN_FINISH_PRESETS.map((p) =>
        `<button type="button" data-wpre="${p.id}" aria-pressed="${proc.wpnFinish === p.id}"><span class="swatch" style="background:${p.dot}"></span>${esc(p.name)}</button>`
      ).join("")}</div>
      <div class="proc-grid">
        <label><span>Edge wear</span><input type="range" data-wf="wpnWear" min="0" max="1" step="0.05" value="${proc.wpnWear}"><span class="mono" data-lbl="wpnWear">${Math.round(proc.wpnWear * 100)}%</span></label>
        <label><span>Livery scale</span><input type="range" data-wf="liveryScale" min="0.5" max="4" step="0.1" value="${proc.liveryScale}"><span class="mono" data-lbl="liveryScale">${Number(proc.liveryScale).toFixed(1)}×</span></label>
      </div>
      <details class="livery-box"${S.activeLivery ? " open" : ""}>
        <summary>Weapon liveries (<code>weapon_liveries.ttpf</code>)${S.activeLivery ? ` · <span class="mono">${esc(S.activeLivery.slice(0, 8))}</span>` : ""}</summary>
        <div class="livery-bar">
          <input type="search" data-livq placeholder="Filter by hash or #index…" spellcheck="false">
          <button type="button" data-livclear ${S.activeLivery ? "" : "disabled"}>Clear</button>
        </div>
        <div class="livery-grid" data-livgrid><p class="note">Expand to load liveries…</p></div>
        <div class="livery-pager"><button type="button" data-livprev disabled>Prev</button><span class="mono" data-livinfo>–</span><button type="button" data-livnext disabled>Next</button></div>
      </details>`;

    box.querySelectorAll("[data-wpre]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.wpre);
        S.view.setProcedural({ wpnFinish: id });
        box.querySelectorAll("[data-wpre]").forEach((b) => b.setAttribute("aria-pressed", b === btn));
      };
    });
    box.querySelectorAll("[data-wf]").forEach((inp) => {
      inp.oninput = () => {
        const k = inp.dataset.wf, val = Number(inp.value);
        S.view.setProcedural({ [k]: val });
        const lbl = box.querySelector(`[data-lbl="${k}"]`);
        if (lbl) lbl.textContent = k === "liveryScale" ? `${val.toFixed(1)}×` : `${Math.round(val * 100)}%`;
      };
    });

    const livDetails = box.querySelector(".livery-box");
    const livGrid = box.querySelector("[data-livgrid]");
    const livQ = box.querySelector("[data-livq]");
    const livClear = box.querySelector("[data-livclear]");
    const livPrev = box.querySelector("[data-livprev]");
    const livNext = box.querySelector("[data-livnext]");
    const livInfo = box.querySelector("[data-livinfo]");
    let livOffset = 0, livTotal = 0, livLoaded = false, livTimer = null;
    const LIV_LIMIT = 24;

    const loadLiveries = async () => {
      livLoaded = true;
      const q = livQ.value.trim();
      livGrid.innerHTML = `<p class="note">Loading weapon liveries…</p>`;
      try {
        const res = await api("weapon-liveries", { q, offset: livOffset, limit: LIV_LIMIT });
        livTotal = res.total || 0;
        const rows = res.items || [];
        if (!rows.length) {
          livGrid.innerHTML = `<p class="note">No matching liveries.</p>`;
          livInfo.textContent = "0";
          livPrev.disabled = true;
          livNext.disabled = true;
          return;
        }
        livGrid.replaceChildren();
        for (const lv of rows) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "livery-thumb" + (S.activeLivery === lv.hash ? " active" : "");
          btn.title = `#${lv.index} · ${lv.hash} · ${lv.width}×${lv.height} ${lv.format}`;
          btn.innerHTML = `<img alt="" loading="lazy" src="/api/models/texture?hash=${encodeURIComponent(lv.hash)}&max=96"><span>#${lv.index}</span>`;
          btn.onclick = async () => {
            try {
              const img = await textureImage(lv.hash, curTexRes(), 0);
              if (S.item !== item) return;
              S.activeLivery = lv.hash;
              S.view.setSharedLivery(img);
              livClear.disabled = false;
              livGrid.querySelectorAll(".livery-thumb").forEach((b) => b.classList.toggle("active", b === btn));
            } catch (e) {
              status(`Livery ${lv.hash}: ${e.message}`);
            }
          };
          livGrid.append(btn);
        }
        const from = livOffset + 1, to = Math.min(livTotal, livOffset + rows.length);
        livInfo.textContent = `${from}–${to} / ${livTotal}`;
        livPrev.disabled = livOffset <= 0;
        livNext.disabled = livOffset + LIV_LIMIT >= livTotal;
      } catch (e) {
        livGrid.innerHTML = `<p class="note">Restart Lineup to enable the weapon liveries browser (${esc(e.message)}).</p>`;
        livInfo.textContent = "–";
        livPrev.disabled = true;
        livNext.disabled = true;
      }
    };

    livDetails.ontoggle = () => { if (livDetails.open && !livLoaded) loadLiveries(); };
    if (livDetails.open) loadLiveries();
    livQ.oninput = () => { clearTimeout(livTimer); livTimer = setTimeout(() => { livOffset = 0; loadLiveries(); }, 220); };
    livPrev.onclick = () => { livOffset = Math.max(0, livOffset - LIV_LIMIT); loadLiveries(); };
    livNext.onclick = () => { livOffset += LIV_LIMIT; loadLiveries(); };
    livClear.onclick = () => {
      S.activeLivery = null;
      S.view.setSharedLivery(null);
      livClear.disabled = true;
      livGrid.querySelectorAll(".livery-thumb.active").forEach((b) => b.classList.remove("active"));
    };

    host.append(box);
  }
}

function renderMaterials(item) {
  const host = $("materialList");
  if (!host) return;
  host.replaceChildren();
  if (!item.materials.length) {
    renderProceduralStudio(item, host);
    if (!host.children.length) host.innerHTML = `<p class="note">No material records.</p>`;
    return;
  }
  const refs = item.materials.reduce((n, m) => n + m.textures.length, 0);
  const found = item.materials.reduce((n, m) => n + m.textures.filter((t) => t.sources.length).length, 0);
  const hasStencil = item.materials.some((m) => m.shaderHash === WATER_STENCIL_SHADER || m.renderPass === "stencil");
  const hasHiddenDecal = item.materials.some((m) => m.renderPass === "decal" && !m.active && !m.tintColor);
  const note = document.createElement("p");
  note.className = "note";
  note.innerHTML = `${refs ? `${found} of ${refs} texture references resolved in local packs.` : "No texture slots."}${hasStencil ? " Stencil cutout mesh hidden by default (tick to inspect)." : ""}${hasHiddenDecal ? " Untextured decal proxy hidden by default." : ""} Hover a material to highlight it in the viewport.`;
  host.append(note);
  renderProceduralStudio(item, host);
  if (item.clothingPresets?.variants?.length) {
    const cp = item.clothingPresets;
    const curPre = S.muvPresets.get(item.id) || item.materials.find((m) => m.activeMuvPreset)?.activeMuvPreset || cp.defaultPreset;
    const activeVar = cp.variants.find((v) => v.id === curPre) || cp.variants[0];
    const box = document.createElement("div");
    box.className = "proc-studio muv-studio";
    const srcLbl = { catalog: "00011ccb", "0drp": "0DRP", stem: "stem match", type: "garment type", fallback: "MUV library" }[cp.source] || cp.source;
    const opts = cp.variants.map((v, i) =>
      `<option value="${esc(v.id)}" ${v.id === curPre ? "selected" : ""}>#${i + 1} · ${esc(v.label)}${v.hasPattern ? " ✦" : ""} (${esc(v.id)})</option>`
    ).join("");
    const chips = cp.variants.map((v, i) =>
      `<button type="button" class="muv-chip${v.id === curPre ? " active" : ""}" data-mpre="${esc(v.id)}" title="#${i + 1} · ${esc(v.label)} (0x${esc(v.id)})">${swatchDotsHtml(v.swatches, 10)}<span class="muv-chip-lbl">${esc(v.label)}</span>${v.hasPattern ? `<span class="muv-pat">✦</span>` : ""}</button>`
    ).join("");
    box.innerHTML = `<div class="proc-head"><h4>Clothing Material Variants</h4><span class="pass-badge pass-tint">${cp.total} MUV · ${esc(srcLbl)}</span></div>
      <div class="muv-active-bar">${swatchDotsHtml(activeVar?.swatches, 12)}<strong>${esc(activeVar?.label || curPre)}</strong><span class="mono">0x${esc(curPre)}</span></div>
      <div class="anat-grid" style="margin-bottom:6px"><label><span>Select style (${cp.total})</span><select data-muvsel>${opts}</select></label></div>
      <div class="muv-chip-grid">${chips}</div>`;
    const applyMainPreset = (pre) => {
      S.muvPresets.set(item.id, pre);
      for (const m of item.materials) {
        if (m.isMuv || m.muvPreset || m.activeMuvPreset) {
          m.activeMuvPreset = pre;
          m.active = null;
          m.texMode = 0;
          m.tintColor = null;
          S.view.setMaterial(m.index, { texMode: 0, tintColor: null, tintOverride: false });
          loadMuvTexture(m.index, item.id, pre, "main");
        }
      }
      renderMaterials(item);
    };
    const sel = box.querySelector("[data-muvsel]");
    if (sel) sel.onchange = () => applyMainPreset(sel.value);
    box.querySelectorAll("[data-mpre]").forEach((btn) => {
      btn.onclick = () => applyMainPreset(btn.dataset.mpre);
    });
    host.append(box);
  }
  if (item.textureVariants && ((item.textureVariants.albedos?.length || 0) > 1 || (item.textureVariants.normals?.length || 0) > 1)) {
    const tv = item.textureVariants;
    const box = document.createElement("div");
    box.className = "proc-studio";
    const rows = [];
    if ((tv.albedos?.length || 0) > 1) {
      const curA = item.materials[0]?.active || "";
      rows.push(`<label><span>Albedo preset (${tv.albedos.length})</span><select data-tvalb>${tv.albedos.map((v, i) =>
        `<option value="${esc(v.hash)}" ${curA === v.hash ? "selected" : ""}>#${i + 1} · ${esc(v.hash.slice(0, 8))} (${v.width}×${v.height} ${esc(v.format.replace(/_UNORM/, ""))})</option>`
      ).join("")}</select></label>`);
    }
    if ((tv.normals?.length || 0) > 1) {
      const curN = item.materials[0]?.activeNormal || "";
      rows.push(`<label><span>Normal preset (${tv.normals.length})</span><select data-tvnrm>${tv.normals.map((v, i) =>
        `<option value="${esc(v.hash)}" ${curN === v.hash ? "selected" : ""}>#${i + 1} · ${esc(v.hash.slice(0, 8))} (${v.width}×${v.height} ${esc(v.format.replace(/_UNORM/, ""))})</option>`
      ).join("")}</select></label>`);
    }
    box.innerHTML = `<div class="proc-head"><h4>Catalogue Texture Variants</h4><span class="pass-badge pass-tint">0DRP presets</span></div><div class="anat-grid">${rows.join("")}</div>`;
    const aSel = box.querySelector("[data-tvalb]");
    if (aSel) aSel.onchange = () => {
      const h = aSel.value;
      for (const m of item.materials) {
        m.active = h;
        m.texMode = 0;
        S.view.setMaterial(m.index, { texMode: 0 });
        loadTexture(m.index, h, item.id, true);
      }
    };
    const nSel = box.querySelector("[data-tvnrm]");
    if (nSel) nSel.onchange = () => {
      const h = nSel.value;
      for (const m of item.materials) {
        m.activeNormal = h;
        loadNormalTexture(m.index, h, item.id);
      }
    };
    host.append(box);
  }
  if (item.materials.length > 1) {
    const bar = document.createElement("div");
    bar.className = "row-actions";
    bar.innerHTML = `<button data-all="1">Show all</button><button data-all="0">Hide all</button>`;
    bar.querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        const on = b.dataset.all === "1";
        for (const m of item.materials) { m.visible = on; S.view.setMaterial(m.index, { visible: on }); }
        renderMaterials(item);
      };
    });
    host.append(bar);
  }
  for (const m of item.materials) {
    const card = document.createElement("div");
    card.className = "material";
    card.onmouseenter = () => S.view.setHighlight("main", m.index);
    card.onmouseleave = () => S.view.setHighlight("main", null);
    const color = m.color || palette(m.index);
    const effects = (m.effects || []).map((e) => esc(e.name)).join(" / ");
    const shaderLink = `<span title="Shader hash">shader ${esc(m.shaderHash)}</span>`;
    const rpass = m.renderPass || "opaque";
    const effPass = m.promotedCutout ? "cutout" : rpass;
    const passBadge = effPass !== "opaque"
      ? `<span class="pass-badge pass-${esc(effPass)}" title="${m.promotedCutout ? `Promoted from ${rpass} via binary-alpha analysis` : `Native grmShader pass mask 0x${(m.passMask ?? 1).toString(16)}`}">${esc(effPass)}${m.promotedCutout ? " (auto)" : ""}</span>`
      : "";
    const magBadge = m.magentaHidden || m.magentaStripped
      ? `<span class="pass-badge pass-stencil" title="Magenta placeholder texture filtered automatically">${m.magentaHidden ? "placeholder hidden" : "placeholder stripped"}</span>`
      : "";
    const terrBadge = m.terrainSpec
      ? `<span class="pass-badge pass-terrain" title="Multi-layer blended shader (${m.terrainSpec.layerCount} diffuse layers)">${m.terrainSpec.layerCount}-layer</span>`
      : "";
    const tintPalBadge = m.tintPalette
      ? `<span class="pass-badge pass-tint" title="Embedded 1D tint palette (${m.tintPalette.width}×1, row length ${m.tintPalette.rowLength}, ${m.tintPalette.rows} row${m.tintPalette.rows > 1 ? "s" : ""})">tint${m.tintPalette.rows > 1 ? ` ×${m.tintPalette.rows}` : ""}</span>`
      : "";
    const procBadge = m.texMode === 6
      ? `<span class="pass-badge pass-tint" title="standard_vehicle_2lyr procedural 8-slice LUT + clearcoat PBR">vehicle LUT</span>`
      : m.texMode === 7
        ? `<span class="pass-badge pass-tint" title="weapon procedural zone PBR (COLOR_0.g zones + speculartex/speculartex2 + livery)">weapon PBR</span>`
        : (m.texMode === 1 || (VEHICLE_PAINT_SHADERS.has(m.shaderHash) && (!m.active || m.texMode === 1)))
          ? `<span class="pass-badge pass-tint" title="Runtime procedural material — no baked RGB diffuse texture; final colors come from in-game paint/material tables">procedural</span>`
          : "";
    card.innerHTML = `<div class="mhead"><label><input type="checkbox" ${m.visible !== false ? "checked" : ""} data-vis> <span class="swatch" style="background:${css(color)}"></span>Material ${m.index}</label>${passBadge}${magBadge}${terrBadge}${tintPalBadge}${procBadge}${item.materials.length > 1 ? `<button class="solo" title="Show only this material">Solo</button>` : ""}<span class="meta">${effects ? effects + " · " : ""}${shaderLink} · ${m.textures.length} tex</span></div>`;
    card.querySelector("[data-vis]").onchange = (e) => { m.visible = e.target.checked; S.view.setMaterial(m.index, { visible: m.visible }); };
    const solo = card.querySelector(".solo");
    if (solo) {
      solo.onclick = () => {
        for (const other of item.materials) {
          other.visible = other.index === m.index;
          S.view.setMaterial(other.index, { visible: other.visible });
        }
        renderMaterials(item);
      };
    }
    if (m.tintPalette) {
      const tp = m.tintPalette;
      const rows = Math.max(1, tp.rows || 1);
      const curRow = Math.max(0, Math.min(rows - 1, m.tintRow || 0));
      m.tintRow = curRow;
      const pbar = document.createElement("div");
      pbar.className = "tint-pal-bar";
      const controls = rows > 1
        ? `<button type="button" data-tprev title="Previous tint row">‹</button><input type="range" data-trange min="0" max="${rows - 1}" step="1" value="${curRow}"><button type="button" data-tnext title="Next tint row">›</button><span class="mono" data-tlabel>${curRow + 1}/${rows}</span>`
        : `<span class="mono">${tp.width} px</span>`;
      pbar.innerHTML = `<span>Tint palette</span>${controls}<canvas class="tint-strip" data-tstrip="${m.index}" width="160" height="12" title="Active tint palette row colors"></canvas>`;
      if (rows > 1) {
        const range = pbar.querySelector("[data-trange]");
        const lbl = pbar.querySelector("[data-tlabel]");
        const setRow = (next) => {
          const r = ((next % rows) + rows) % rows;
          m.tintRow = r;
          range.value = String(r);
          lbl.textContent = `${r + 1}/${rows}`;
          S.view.setMaterial(m.index, { tintRow: r });
          drawTintStrip(m);
        };
        range.oninput = (e) => setRow(Number(e.target.value));
        pbar.querySelector("[data-tprev]").onclick = () => setRow((m.tintRow || 0) - 1);
        pbar.querySelector("[data-tnext]").onclick = () => setRow((m.tintRow || 0) + 1);
      }
      card.append(pbar);
    }
    if (m.matMode !== 2) {
      const tbar = document.createElement("div");
      tbar.className = "tint-bar";
      if (m.texMode === 2) {
        const pal = (m.palette || animalMaskPalette(item.name)).map((z) => [...z]);
        m.palette = pal;
        tbar.innerHTML = [0, 1, 2].map((k) => `<label>Zone ${k + 1} <input type="color" data-pal="${k}" value="${rgbToHex(pal[k])}"></label>`).join("") +
          `<button type="button" class="reset-tint" title="Restore species coat palette">Reset</button>`;
        for (const inp of tbar.querySelectorAll("[data-pal]")) {
          inp.oninput = (e) => {
            const k = Number(e.target.dataset.pal);
            m.palette[k] = hexToRgb(e.target.value);
            S.view.setMaterial(m.index, { palette: m.palette });
          };
        }
        tbar.querySelector(".reset-tint").onclick = () => {
          m.palette = (m.defaultPalette || animalMaskPalette(item.name)).map((z) => [...z]);
          tbar.querySelectorAll("[data-pal]").forEach((inp, k) => { inp.value = rgbToHex(m.palette[k]); });
          S.view.setMaterial(m.index, { palette: m.palette });
        };
      } else {
        const label = m.texMode === 6 ? "Vehicle tint" : m.texMode === 7 ? "Weapon tint" : m.matMode === 1 ? "Paint" : m.texMode === 3 ? "Skin tone" : m.texMode === 1 ? "Fabric tint" : m.texMode === 5 ? "Terrain tint" : "Surface tint";
        const defDisp = m.defaultTint || (m.active ? [1, 1, 1] : [0.72, 0.73, 0.70]);
        const curDisp = m.tintColor || defDisp;
        const blendBtn = m.terrainSpec && m.texMode !== 5
          ? `<button type="button" class="blend-btn" title="Restore multi-layer terrain blending">Blend ${m.terrainSpec.layerCount} layers</button>`
          : "";
        tbar.innerHTML = `<label>${label} <input type="color" data-tint value="${rgbToHex(curDisp)}"></label>${blendBtn}<button type="button" class="reset-tint" title="Restore default material tint">Reset</button>`;
        const inp = tbar.querySelector("[data-tint]");
        inp.oninput = (e) => {
          m.tintColor = hexToRgb(e.target.value);
          m.tintOverride = true;
          S.view.setMaterial(m.index, { tintColor: m.tintColor, tintOverride: true });
        };
        const bb = tbar.querySelector(".blend-btn");
        if (bb) {
          bb.onclick = () => {
            m.active = texHash(m.terrainSpec.slot);
            m.texMode = 5;
            S.view.setMaterial(m.index, { texMode: 5 });
            if (m.active) loadTexture(m.index, m.active, item.id);
            loadTerrainLayers(m.index, m.terrainSpec, "main", item.id, () => S.item === item && m.texMode === 5);
            renderMaterials(item);
          };
        }
        tbar.querySelector(".reset-tint").onclick = () => {
          m.tintColor = m.defaultTint ? [...m.defaultTint] : null;
          m.tintOverride = false;
          inp.value = rgbToHex(m.defaultTint || (m.active ? [1, 1, 1] : [0.72, 0.73, 0.70]));
          S.view.setMaterial(m.index, { tintColor: m.tintColor, tintOverride: false });
        };
      }
      card.append(tbar);
    }
    const blendedHashes = m.texMode === 5 && m.terrainSpec
      ? new Set(m.terrainSpec.layers.filter(Boolean).map((s) => texHash(s)))
      : null;
    for (const t of m.textures) {
      const src = t.sources[0];
      const effHash = texHash(t);
      const slot = document.createElement("div");
      slot.className = "slot" + (src ? "" : " missing");
      const pngUrl = src ? `/api/models/texture?hash=${effHash}&max=4096` + (src.area === "embedded" ? `&model=${item.id}` : "") : "";
      const ttdbHint = !src && t.ttdbColor ? ` · <span class="swatch" style="background:${css(t.ttdbColor)};vertical-align:middle"></span> TTDB baked tint` : "";
      const srcLabel = src ? (src.area === "embedded"
        ? `${src.width}×${src.height} ${esc(src.format)} · embedded TXD (${esc(src.embeddedName)}, ${esc(src.embeddedLayout)})`
        : (src.nativeId
          ? `${src.width}×${src.height} ${esc(src.format)} · native RPF ${esc(src.path)} (${esc(src.nativeName)})`
          : `${src.width}×${src.height} ${esc(src.format)} · ${esc(src.area)}/${esc(src.path.split("/").pop())} #${src.idx}${t.sources.length > 1 ? ` (+${t.sources.length - 1} packs)` : ""}`)) : "";
      const isChecked = m.active === effHash || (blendedHashes && blendedHashes.has(effHash));
      slot.innerHTML = `${src ? `<a class="thumb-link" href="${esc(pngUrl)}" target="_blank" rel="noopener" title="Open the texture as PNG"><img alt="" loading="lazy"></a>` : `<div class="noimg">not in local packs</div>`}
        <div class="info"><div><code>${esc(t.spelling || t.parameter)}</code> · slot ${t.slot}${roleBadge(t)}</div>
        <div><code>${esc(t.textureHash)}</code>${t.effectiveHash ? ` → <code>${esc(t.effectiveHash)}</code>` : ""}${t.spelling ? ` · param ${esc(t.parameter)}` : ""}</div>
        <div>${src ? srcLabel : `Hash not found in any .ttpf pack${ttdbHint}`}</div>
        <div class="controls">${src
          ? `<label title="Use as surface color/detail in Textured mode"><input type="checkbox" data-apply ${isChecked ? "checked" : ""}> Color</label><label title="Use as tangent-space normal map"><input type="checkbox" data-norm ${m.activeNormal === effHash ? "checked" : ""}> Normal</label><a href="${esc(pngUrl)}" target="_blank" rel="noopener">PNG</a>`
          : ""}</div></div>`;
      if (src) {
        const img = slot.querySelector("img");
        textureImage(effHash, 128, item.id).then((bmp) => {
          const c = document.createElement("canvas"); c.width = bmp.width; c.height = bmp.height;
          c.getContext("2d").drawImage(bmp, 0, 0); img.src = c.toDataURL();
        }).catch((e) => { img.replaceWith(Object.assign(document.createElement("div"), { className: "noimg", textContent: "not decodable", title: e.message })); });
        slot.querySelector("[data-apply]").onchange = (e) => {
          const prevMode = m.texMode;
          m.activeMuvPreset = null;
          m.active = e.target.checked ? effHash : null;
          if (!e.target.checked) m.texMode = 0;
          else if (m.shaderHash === EYE_SHADER && t.parameter === BASE_COLOR) m.texMode = 4;
          else if (m.shaderHash === HEAD_SHADER && t.parameter === BASE_COLOR && !/SRGB/.test(src.format)) m.texMode = 3;
          else if (t.parameter === SPECULAR && !/SRGB/.test(src.format)) m.texMode = 1;
          else if (t.parameter === BASE_COLOR && item.category === "Animals" && !/SRGB/.test(src.format)) {
            m.texMode = 2;
            m.palette = m.palette || animalMaskPalette(item.name);
          } else m.texMode = 0;
          if (m.texMode !== 5) S.view.clearExtraTextures(m.index);
          m.lumaAlpha = (m.renderPass === "alpha") && /BC1|BC6H|R11G11B10/.test(src.format);
          S.view.setMaterial(m.index, { texMode: m.texMode, palette: m.palette || null, lumaAlpha: m.lumaAlpha });
          for (const other of card.querySelectorAll("[data-apply]")) if (other !== e.target) other.checked = false;
          if (m.active) loadTexture(m.index, m.active, item.id, true); else S.view.setTexture(m.index, null);
          if ((prevMode === 2) !== (m.texMode === 2) || prevMode === 5 || prevMode === 6 || prevMode === 7) renderMaterials(item);
        };
        slot.querySelector("[data-norm]").onchange = (e) => {
          m.activeNormal = e.target.checked ? effHash : null;
          for (const other of card.querySelectorAll("[data-norm]")) if (other !== e.target) other.checked = false;
          if (m.activeNormal) loadNormalTexture(m.index, m.activeNormal, item.id); else S.view.setNormalTexture(m.index, null);
        };
      }
      card.append(slot);
    }
    const pEntries = m.params ? Object.entries(m.params) : [];
    if (pEntries.length) {
      const det = document.createElement("details");
      det.className = "cbuf-details";
      det.innerHTML = `<summary>Shader constant buffer (${pEntries.length} params)</summary><dl class="cbuf-list">${pEntries.map(([k, v]) => `<dt><code>${esc(k)}</code></dt><dd><code>${esc(fmtParamVal(v))}</code></dd>`).join("")}</dl>`;
      card.append(det);
    }
    host.append(card);
    if (m._tintPalBmp) drawTintStrip(m);
  }
}

// Page --------------------------------------------------------------------------------------------
function saveUrl() {
  const u = new URL(location.href);
  for (const [k, v] of [["category", S.category], ["sub", S.subcategory], ["id", S.item?.id || ""], ["q", $("q").value.trim()]])
    v ? u.searchParams.set(k, v) : u.searchParams.delete(k);
  history.replaceState(null, "", u);
}
function setMode(mode) {
  for (const b of $("modes").querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.mode === mode);
  S.view.set("mode", mode);
  $("legend").textContent = { wire: "Wireframe: edges colored by material", solid: "Solid: neutral shading (normal maps active if enabled)",
    material: "Material: one color per material", textured: "Textured: base color + normal maps (untextured materials render neutral grey)" }[mode];
}
function stepList(delta) {
  if (!S.rowIds.length) return;
  const idx = S.item ? S.rowIds.indexOf(S.item.id) : -1;
  const next = idx < 0 ? 0 : Math.max(0, Math.min(S.rowIds.length - 1, idx + delta));
  if (S.rowIds[next] !== S.item?.id) openModel(S.rowIds[next]);
}

const STUDIO_STORAGE_KEY = "pso_studio_lighting_v1";

function initStudioControls() {
  if (!S.view || typeof ModelView === "undefined") return;
  const lightPresets = ModelView.LIGHT_PRESETS || [];
  const bgPresets = ModelView.BG_PRESETS || [];
  const quickSel = $("studioLightQuick");
  const toggleBtn = $("studioToggle");
  const panel = $("studioPanel");
  if (!panel) return;

  // Restore saved studio preferences (keep autoRotate off on fresh page load so GPU stays quiet until requested)
  try {
    const raw = localStorage.getItem(STUDIO_STORAGE_KEY);
    if (raw) {
      const saved = JSON.parse(raw);
      if (saved && typeof saved === "object") {
        const clean = {};
        if (lightPresets.some((p) => p.id === saved.lightPreset)) clean.lightPreset = saved.lightPreset;
        if (bgPresets.some((p) => p.id === saved.bgPreset)) clean.bgPreset = saved.bgPreset;
        if (typeof saved.bgCustomHex === "string" && /^#[0-9a-f]{6}$/i.test(saved.bgCustomHex)) clean.bgCustomHex = saved.bgCustomHex;
        for (const k of ["rotation", "exposure", "keyIntensity", "ambientIntensity", "shadowStrength", "bgBrightness"]) {
          if (typeof saved[k] === "number" && Number.isFinite(saved[k])) clean[k] = saved[k];
        }
        S.view.setStudio(clean);
      }
    }
  } catch (_) {}

  const persist = () => {
    try {
      const st = S.view.getStudio();
      localStorage.setItem(STUDIO_STORAGE_KEY, JSON.stringify({
        lightPreset: st.lightPreset,
        bgPreset: st.bgPreset,
        bgCustomHex: st.bgCustomHex,
        rotation: Math.round(st.rotation),
        exposure: st.exposure,
        keyIntensity: st.keyIntensity,
        ambientIntensity: st.ambientIntensity,
        shadowStrength: st.shadowStrength,
        bgBrightness: st.bgBrightness,
      }));
    } catch (_) {}
  };

  // Populate quick toolbar dropdown
  if (quickSel) {
    quickSel.innerHTML = "";
    for (const p of lightPresets) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label;
      quickSel.appendChild(opt);
    }
    quickSel.onchange = () => {
      S.view.setStudio({ lightPreset: quickSel.value });
      syncUI();
      persist();
    };
  }

  // Build floating Studio panel
  const lightSwatchesHtml = lightPresets.map((p) =>
    `<button type="button" class="studio-swatch-btn" data-light="${p.id}" title="${p.label}" aria-pressed="false">` +
    `<span class="studio-swatch-chip" style="background:${p.swatch}"></span>` +
    `<span class="studio-swatch-lbl">${p.short || p.label}</span>` +
    `</button>`
  ).join("");
  const bgSwatchesHtml = bgPresets.map((p) =>
    `<button type="button" class="studio-swatch-btn" data-bg="${p.id}" title="${p.label}" aria-pressed="false">` +
    `<span class="studio-swatch-chip" style="background:${p.swatch}"></span>` +
    `<span class="studio-swatch-lbl">${p.label.replace(" (18%)", "").replace("3D Environment Dome", "3D Sky Dome").replace("Custom Solid Color", "Custom Solid").replace("Chroma Key Green", "Chroma Green")}</span>` +
    `</button>`
  ).join("");

  panel.innerHTML =
    `<div class="studio-head">` +
      `<strong>Studio Lighting &amp; Backdrop</strong>` +
      `<button type="button" id="studioReset" title="Reset lighting and backdrop to defaults">Reset</button>` +
      `<button type="button" id="studioClose" title="Close panel (L)">×</button>` +
    `</div>` +
    `<div class="studio-sec">` +
      `<div class="studio-sec-title"><span>Lighting Scenario</span><span class="studio-hint">Shift+L cycle</span></div>` +
      `<div class="studio-swatches" id="studioLightGrid">${lightSwatchesHtml}</div>` +
    `</div>` +
    `<div class="studio-sec">` +
      `<div class="studio-sec-title"><span>Viewport Background</span></div>` +
      `<div class="studio-swatches" id="studioBgGrid">${bgSwatchesHtml}</div>` +
      `<div class="studio-custom-row" id="studioCustomRow">` +
        `<span>Solid Color</span>` +
        `<input type="color" id="studioBgColor" value="#1c252b" aria-label="Custom solid background color" />` +
        `<span class="studio-hex" id="studioBgHex">#1C252B</span>` +
      `</div>` +
    `</div>` +
    `<div class="studio-sec">` +
      `<div class="studio-sec-title"><span>Light &amp; Exposure Controls</span></div>` +
      `<div class="studio-sliders">` +
        `<div class="studio-slider-row"><label for="studioRot">Light Angle</label><input id="studioRot" type="range" min="0" max="360" step="1" /><output id="studioRotVal"></output></div>` +
        `<div class="studio-slider-row"><label for="studioExp">Exposure</label><input id="studioExp" type="range" min="0.40" max="2.20" step="0.02" /><output id="studioExpVal"></output></div>` +
        `<div class="studio-slider-row"><label for="studioKey">Key Light</label><input id="studioKey" type="range" min="0.00" max="2.50" step="0.05" /><output id="studioKeyVal"></output></div>` +
        `<div class="studio-slider-row"><label for="studioAmb">Ambient / Fill</label><input id="studioAmb" type="range" min="0.00" max="2.50" step="0.05" /><output id="studioAmbVal"></output></div>` +
        `<div class="studio-slider-row"><label for="studioShd">Shadows</label><input id="studioShd" type="range" min="0.00" max="1.00" step="0.02" /><output id="studioShdVal"></output></div>` +
        `<div class="studio-slider-row"><label for="studioBgB">BG Brightness</label><input id="studioBgB" type="range" min="0.20" max="1.80" step="0.05" /><output id="studioBgBVal"></output></div>` +
      `</div>` +
      `<div class="studio-check-row">` +
        `<label><input type="checkbox" id="studioAutoRot" /> Auto-rotate lights</label>` +
        `<span class="studio-hint">Key: L</span>` +
      `</div>` +
    `</div>`;

  const syncUI = () => {
    const st = S.view.getStudio();
    if (quickSel) quickSel.value = st.lightPreset;
    for (const b of panel.querySelectorAll("[data-light]")) {
      b.setAttribute("aria-pressed", String(b.dataset.light === st.lightPreset));
    }
    for (const b of panel.querySelectorAll("[data-bg]")) {
      b.setAttribute("aria-pressed", String(b.dataset.bg === st.bgPreset));
    }
    const hex = (st.bgCustomHex || "#1c252b").toLowerCase();
    const colorEl = $("studioBgColor");
    if (colorEl && colorEl.value.toLowerCase() !== hex) colorEl.value = hex;
    if ($("studioBgHex")) $("studioBgHex").textContent = hex.toUpperCase();
    const rot = Math.round(((st.rotation || 0) % 360 + 360) % 360);
    if ($("studioRot")) $("studioRot").value = String(rot);
    if ($("studioRotVal")) $("studioRotVal").textContent = `${rot}°`;
    if ($("studioExp")) $("studioExp").value = String(st.exposure.toFixed(2));
    if ($("studioExpVal")) $("studioExpVal").textContent = `${st.exposure.toFixed(2)}×`;
    if ($("studioKey")) $("studioKey").value = String(st.keyIntensity.toFixed(2));
    if ($("studioKeyVal")) $("studioKeyVal").textContent = `${st.keyIntensity.toFixed(2)}×`;
    if ($("studioAmb")) $("studioAmb").value = String(st.ambientIntensity.toFixed(2));
    if ($("studioAmbVal")) $("studioAmbVal").textContent = `${st.ambientIntensity.toFixed(2)}×`;
    if ($("studioShd")) $("studioShd").value = String(st.shadowStrength.toFixed(2));
    if ($("studioShdVal")) $("studioShdVal").textContent = `${Math.round(st.shadowStrength * 100)}%`;
    if ($("studioBgB")) $("studioBgB").value = String(st.bgBrightness.toFixed(2));
    if ($("studioBgBVal")) $("studioBgBVal").textContent = `${Math.round(st.bgBrightness * 100)}%`;
    if ($("studioAutoRot")) $("studioAutoRot").checked = !!st.autoRotate;
  };

  for (const b of panel.querySelectorAll("[data-light]")) {
    b.onclick = () => {
      S.view.setStudio({ lightPreset: b.dataset.light });
      syncUI();
      persist();
    };
  }
  for (const b of panel.querySelectorAll("[data-bg]")) {
    b.onclick = () => {
      S.view.setStudio({ bgPreset: b.dataset.bg });
      syncUI();
      persist();
    };
  }
  if ($("studioBgColor")) {
    $("studioBgColor").oninput = (e) => {
      S.view.setStudio({ bgPreset: "solid", bgCustomHex: e.target.value });
      syncUI();
      persist();
    };
  }
  const bindSlider = (id, key, parse = Number) => {
    const el = $(id);
    if (!el) return;
    el.oninput = () => {
      S.view.setStudio({ [key]: parse(el.value) });
      syncUI();
    };
    el.onchange = persist;
  };
  bindSlider("studioRot", "rotation");
  bindSlider("studioExp", "exposure");
  bindSlider("studioKey", "keyIntensity");
  bindSlider("studioAmb", "ambientIntensity");
  bindSlider("studioShd", "shadowStrength");
  bindSlider("studioBgB", "bgBrightness");
  if ($("studioAutoRot")) {
    $("studioAutoRot").onchange = (e) => {
      S.view.setStudio({ autoRotate: e.target.checked });
      syncUI();
    };
  }
  if ($("studioReset")) {
    $("studioReset").onclick = () => {
      S.view.resetStudio();
      syncUI();
      persist();
    };
  }

  const setOpen = (open) => {
    panel.hidden = !open;
    if (toggleBtn) toggleBtn.setAttribute("aria-expanded", String(open));
  };
  if (toggleBtn) toggleBtn.onclick = () => setOpen(panel.hidden);
  if ($("studioClose")) $("studioClose").onclick = () => setOpen(false);

  S.view.onStudioTick((st) => {
    if (panel.hidden) return;
    const rot = Math.round(((st.rotation || 0) % 360 + 360) % 360);
    if ($("studioRot")) $("studioRot").value = String(rot);
    if ($("studioRotVal")) $("studioRotVal").textContent = `${rot}°`;
  });

  S.toggleStudioPanel = () => setOpen(panel.hidden);
  S.cycleStudioLight = () => {
    if (!lightPresets.length) return;
    const cur = S.view.getStudio().lightPreset;
    const idx = lightPresets.findIndex((p) => p.id === cur);
    const next = lightPresets[(idx + 1) % lightPresets.length];
    S.view.setStudio({ lightPreset: next.id });
    syncUI();
    persist();
  };

  syncUI();
}

async function start() {
  try {
    S.view = new ModelView($("view"));
    S.view.onAnimTick(syncAnimTransport);
    initStudioControls();
  } catch (e) {
    loading(e.message, true);
  }
  for (const b of $("modes").querySelectorAll("button")) b.onclick = () => setMode(b.dataset.mode);
  for (const id of ["overlay", "normals", "grid", "box", "skeleton", "backfaces", "alpha", "flipv", "spin"]) {
    if (!$(id)) continue;
    $(id).onchange = (e) => {
      S.view.set(id, e.target.checked);
      if (id === "skeleton" && $("rigSkelOn")) $("rigSkelOn").checked = e.target.checked;
    };
  }
  if ($("texRes")) {
    $("texRes").value = String(S.texRes);
    $("texRes").onchange = (e) => {
      S.texRes = Number(e.target.value) || 8192;
      reloadAllTextures();
    };
  }
  for (const b of $("views").querySelectorAll("[data-view]")) b.onclick = () => S.view.setView(b.dataset.view);
  $("fit").onclick = () => S.view.fit();
  if ($("exportTexturedGlb")) $("exportTexturedGlb").onclick = exportTexturedGlb;
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea")) return;
    const mode = { 1: "wire", 2: "solid", 3: "material", 4: "textured" }[e.key];
    if (mode) setMode(mode);
    else if (e.key === "f" || e.key === "F") S.view.fit();
    else if (e.key === "r" || e.key === "R") S.view.setView("iso");
    else if (e.key === "l" && !e.shiftKey) S.toggleStudioPanel?.();
    else if (e.key === "L" && e.shiftKey) S.cycleStudioLight?.();
    else if (e.key === "g" || e.key === "G") { $("grid").checked = !$("grid").checked; S.view.set("grid", $("grid").checked); }
    else if (e.key === "b" || e.key === "B") { $("box").checked = !$("box").checked; S.view.set("box", $("box").checked); }
    else if (e.key === "s" || e.key === "S") {
      if ($("skeleton")) {
        $("skeleton").checked = !$("skeleton").checked;
        S.view.set("skeleton", $("skeleton").checked);
        if ($("rigSkelOn")) $("rigSkelOn").checked = $("skeleton").checked;
      }
    }
    else if (e.key === " " && S.view?._anim) {
      e.preventDefault();
      const playing = !S.view._animState.playing;
      const patch = { playing };
      if (playing && S.view._animState.time >= (S.view._anim.duration || 0) - 0.005) patch.time = 0;
      S.view.setAnimState(patch);
    }
    else if (e.key === "w" || e.key === "W") { $("overlay").checked = !$("overlay").checked; S.view.set("overlay", $("overlay").checked); }
    else if (e.key === "t" || e.key === "T") { $("spin").checked = !$("spin").checked; S.view.set("spin", $("spin").checked); }
    else if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); stepList(1); }
    else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); stepList(-1); }
  });
  $("q").oninput = () => { clearTimeout(listTimer); listTimer = setTimeout(() => { S.start = 0; saveUrl(); loadList(); }, 250); };
  for (const id of ["archive", "sort", "textured"]) $(id).onchange = () => { S.start = 0; loadList(); };
  $("prev").onclick = () => { S.start = Math.max(0, S.start - S.limit); loadList(); };
  $("next").onclick = () => { S.start += S.limit; loadList(); };
  const q = new URLSearchParams(location.search);
  $("q").value = q.get("q") || "";
  S.category = q.get("category") || "";
  S.subcategory = q.get("sub") || "";
  setMode("textured");
  try {
    S.summary = await api("summary");
  } catch (e) {
    status(e.message);
    loading(e.message, true);
    return;
  }
  renderTree();
  await loadList();
  status(`${fmtN(S.summary.totals.items)} items from ${fmtN(S.summary.totals.resources)} model resources · ${S.total ? fmtN(S.total) + " listed" : ""}`);
  if (q.get("id")) openModel(Number(q.get("id")));
}
start();
