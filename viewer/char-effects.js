"use strict";
// RAGE 9 Character Effects & Simulation Studio for Lineup
// Covers: Drug/Status Timecycle Post-FX (Mandy_*, Cocaine, Molly, Weed, Drunk, Trips, Combat),
// Skin Weathering (WET_SKIN, WET_DUST_SKIN, WET_UNDERWATER_SKIN, DirtDefinitions, SunTan, Flush),
// Dynamic Physiology (16 BodyShapePresets, 27 BM_* morph channels, Height, SEC_* Soft-Tissue Physics),
// Body Damage (Blood Soak, 3D Click-to-Stamp Wounds, Vital Organs X-Ray, Rigged Gore Dismemberment),
// and Live UV-Baked Tattoos (00001ae00000.pso -> decorations_0..3_3.ttpf).
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const $ = (id) => document.getElementById(id);

  const _catalogCache = new Map(); // rigKind -> Promise<catalog>
  const _goreCache = new Map();    // rigToken -> Promise<items>

  const ALL_MORPH_CHANNELS = [
    ["GCBreasts", "Breasts (GCBreasts)", -0.5, 1.0],
    ["0xb0eae798", "Breast Volume / Fullness (0xb0eae798)", -1.0, 1.0],
    ["GCCleavage", "Cleavage (GCCleavage)", -0.75, 0.75],
    ["gcbreastheight", "Breast Height (gcbreastheight)", -1.0, 1.0],
    ["gcchestheight", "Chest Height (gcchestheight)", -1.0, 1.0],
    ["gcbreastspacing", "Breast Spacing (gcbreastspacing)", -1.0, 1.0],
    ["GCButt", "Glutes / Butt (GCButt)", -0.5, 1.0],
    ["gchipwidth", "Hip Width (gchipwidth)", -1.0, 1.0],
    ["gchipheight", "Hip Height (gchipheight)", -1.0, 1.0],
    ["gcbellyround", "Belly Roundness (gcbellyround)", -1.0, 1.0],
    ["gcweightbelly", "Weight: Belly", -1.0, 1.2],
    ["gcweightmuffin", "Weight: Waist / Love Handles", -1.0, 1.2],
    ["gcweighthip", "Weight: Hips", -1.0, 1.0],
    ["gcweightbutt", "Weight: Glutes", -1.0, 1.0],
    ["gcweightchest", "Weight: Chest", -1.0, 1.0],
    ["gcweightback", "Weight: Back", -1.0, 1.0],
    ["gcweightshoulder", "Weight: Shoulders", -1.0, 1.0],
    ["gcweightupperarm", "Weight: Upper Arms", -1.0, 1.0],
    ["gcweightforearm", "Weight: Forearms", -1.0, 1.0],
    ["gcweightthigh", "Weight: Thighs", -1.0, 1.0],
    ["gcweightcalf", "Weight: Calves", -1.0, 1.0],
    ["gcweightneck", "Weight: Neck / Jaw", -1.0, 1.0],
    ["gcmusclechest", "Muscle: Chest / Pecs", -0.4, 1.2],
    ["gcmuscleback", "Muscle: Back / Lats", -0.4, 1.2],
    ["gcmuscleshoulder", "Muscle: Shoulders / Delts", -0.4, 1.2],
    ["gcmuscleupperarm", "Muscle: Upper Arms", -0.4, 1.2],
    ["gcmusclebiceps", "Muscle: Biceps", -0.4, 1.2],
    ["gcmuscletriceps", "Muscle: Triceps", -0.4, 1.2],
    ["gcmuscleforearm", "Muscle: Forearms", -0.4, 1.2],
    ["gcmusclebelly", "Muscle: Core / Abs", -0.4, 1.2],
    ["gcmusclemuffin", "Muscle: Obliques", -0.4, 1.2],
    ["gcmusclehip", "Muscle: Hips", -0.4, 1.2],
    ["gcmusclebutt", "Muscle: Glutes", -0.4, 1.2],
    ["gcmusclethigh", "Muscle: Quads / Hamstrings", -0.4, 1.2],
    ["gcmusclecalf", "Muscle: Calves", -0.4, 1.2],
    ["gcmuscleneck", "Muscle: Neck / Traps", -0.4, 1.2],
  ];

  const QUICK_WOUND_ZONES = [
    { id: "heart", label: "Heart (2×)", pos: [0.035, 1.27, 0.115], radius: 0.068, drip: 0.24 },
    { id: "throat", label: "Throat (4×)", pos: [0.0, 1.445, 0.065], radius: 0.048, drip: 0.22 },
    { id: "head", label: "Headshot", pos: [0.02, 1.615, 0.095], radius: 0.052, drip: 0.18 },
    { id: "chest", label: "Chest", pos: [-0.04, 1.25, 0.12], radius: 0.075, drip: 0.22 },
    { id: "gut", label: "Abdomen", pos: [0.01, 1.06, 0.115], radius: 0.085, drip: 0.20 },
    { id: "larm", label: "Left Arm", pos: [0.27, 1.26, 0.02], radius: 0.065, drip: 0.16 },
    { id: "rarm", label: "Right Arm", pos: [-0.27, 1.26, 0.02], radius: 0.065, drip: 0.16 },
    { id: "lleg", label: "Left Thigh", pos: [0.105, 0.68, 0.08], radius: 0.08, drip: 0.22 },
    { id: "rleg", label: "Right Thigh", pos: [-0.105, 0.68, 0.08], radius: 0.08, drip: 0.22 },
  ];

  const ST = {
    catalog: null,
    goreItems: [],
    activeDrugId: "",
    activeDirtId: "AM_Dirtsettings_Mud_Rural",
    activeShapeId: "PED_BODYSHAPE_REGULAR",
    woundKind: 0,     // 0=gunshot/blood, 1=bruise, 2=scrape, 3=burn
    woundSize: 0.072,
    woundDrip: 0.20,
    activeBloodDecalHash: "",
    clickStamp: false,
    showVitalOrgans: false,
    severedLimbs: new Map(), // limb -> { partId, hideRootBones }
    // Tattoo & Facial Makeup Overlay state
    tattooQ: "",
    tattooOffset: 0,
    tattooTotal: 0,
    tattooItems: [],
    tattooTarget: "upper", // upper (hand_*), lower (feet_*), head (main)
    tattooOpacity: 0.88,
    tattooFade: 0.12,
    tattooScaleMult: 1.0,
    tattooOffU: 0.0,
    tattooOffV: 0.0,
    tattooRotDeg: 0.0,
    appliedTattoos: [], // [{ target, tattoo, opacity, fade, scaleMult, offU, offV, rotDeg, native }]
    appliedFaceOverlays: [], // [{ hash, tints, tintMode, opacity, uvTransform, label, kind, native }]
    nativeOverlays: null, // { pedName, tattoos, faceOverlays, makeupLooks, counts }
    autoNativeTattoos: true,
    autoNativeMakeup: true,
    activeMakeupLookId: "look_0",
    makeupKind: "",
    makeupQ: "",
    makeupOffset: 0,
    makeupTotal: 0,
    makeupItems: [],
    makeupKinds: [],
    _overlayReqTok: 0,
    _overlayTexCache: new Map(), // url -> Promise<ImageBitmap>
    _baseTexCache: new Map(), // layer:matIdx -> original ImageBitmap
    _lastCtx: null,
    _lastItem: null,
  };

  function isHumanCharacter(item) {
    if (!item) return false;
    if (item.category === "Protagonists" || item.category === "Characters") return true;
    if (item.characterAssembly && item.characterAssembly.kind && item.characterAssembly.kind !== "Animal") return true;
    return false;
  }

  function rigKindOf(item) {
    const rk = item?.rigInfo?.rigKind;
    if (rk === "male" || rk === "female") return rk;
    const body = (item?.characterAssembly?.body || item?.pedArchetype?.bodyRig || item?.name || "").toLowerCase();
    if (/(?:^|_)(mr1|mb1|mo1|mc1|msk1|mk1|mi1|male|jason)(?:_|$)/.test(body)) return "male";
    return "female";
  }

  function rigTokenOf(item) {
    const b = (item?.characterAssembly?.body || item?.pedArchetype?.bodyRig || "").toLowerCase();
    if (b) return b;
    return rigKindOf(item) === "male" ? "mr1" : "fr1";
  }

  function fetchCatalog(api, rigKind) {
    if (!_catalogCache.has(rigKind)) {
      _catalogCache.set(rigKind, api("char-effects-catalog", { rig: rigKind }).catch((e) => {
        _catalogCache.delete(rigKind);
        throw e;
      }));
    }
    return _catalogCache.get(rigKind);
  }

  function fetchGore(api, rigToken) {
    if (!_goreCache.has(rigToken)) {
      _goreCache.set(rigToken, api("char-gore", { rig: rigToken }).then((r) => r.items || []).catch(() => []));
    }
    return _goreCache.get(rigToken);
  }

  function applyCharacterNativeMorphs(item, view) {
    if (!view) return false;
    const isMale = rigKindOf(item) === "male";
    const defH = isMale ? 1.82 : 1.72;
    view.charMorph.defaultHeight = defH;
    view.resetCharMorph();
    const ca = item?.characterAssembly;
    const hs = ca?.heightScale;
    const targetH = (typeof hs === "number" && hs >= 0.6 && hs <= 1.5)
      ? Math.max(1.42, Math.min(2.15, +(defH * hs).toFixed(3)))
      : defH;
    const bm = ca?.bodyMorphs;
    if (bm && typeof bm === "object" && Object.keys(bm).length) {
      ST.activeShapeId = "CHARACTER_NATIVE";
      view.setCharMorph({ ...bm, height: targetH });
      return true;
    }
    ST.activeShapeId = "PED_BODYSHAPE_REGULAR";
    if (Math.abs(targetH - defH) > 0.001) {
      view.setCharMorph({ height: targetH });
    }
    return false;
  }

  function resetForModel(item, view) {
    ST.activeDrugId = "";
    ST.activeShapeId = "PED_BODYSHAPE_REGULAR";
    ST.clickStamp = false;
    ST.showVitalOrgans = false;
    ST.severedLimbs.clear();
    ST.appliedTattoos = [];
    ST.appliedFaceOverlays = [];
    ST.nativeOverlays = null;
    ST.activeMakeupLookId = "look_0";
    ST._overlayReqTok++;
    ST._baseTexCache.clear();
    if (!view) return;
    view.setPickMode(false, null);
    view.setPostFx({ active: false, preset: null, intensity: 1.0, phase: 0 });
    view.setCharFx({
      wetness: 0, soak: 0, waterHeight: 2.2, droplets: 0.55, eyeWetness: 0.2,
      dust: 0, dirtAmt: 0, underwater: 0, waterLine: 1.85, caustics: 0.75,
      sunTan: 0, sunBurn: 0, tanLines: 0, flush: 0, bloodSoak: 0,
      glints: 0, pedLightMult: 1.0, pedFresnel: 0, uvParty: 0, wounds: [],
    });
    applyCharacterNativeMorphs(item, view);
    view.setHiddenBoneRoots([]);
    view.setVitalOrgans([], false);
  }

  // --- Tattoo & Facial Overlay UV-Space Canvas2D Baker (00001ae00000.pso + decorations_0..3_3.ttpf) ---
  function overlayImage(hash, tints, tintMode, maxRes = 512) {
    const mode = tintMode || "mask";
    const tJson = JSON.stringify(tints || []);
    const key = `${hash}|${mode}|${maxRes}|${tJson}`;
    const hit = ST._overlayTexCache.get(key);
    if (hit) return hit;
    const url = `/api/models/overlay-texture?hash=${encodeURIComponent(hash)}&mode=${encodeURIComponent(mode)}&max=${maxRes}&opacity=1&tints=${encodeURIComponent(tJson)}`;
    const p = new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => resolve(img);
      img.onerror = () => {
        ST._overlayTexCache.delete(key);
        reject(new Error(`Overlay texture ${hash} failed`));
      };
      img.src = url;
    });
    ST._overlayTexCache.set(key, p);
    return p;
  }

  function findSkinTargets(ctx, targetKey) {
    const out = [];
    const S = ctx.state;
    const item = S.item;
    if (!item) return out;
    if (targetKey === "head") {
      for (const m of item.materials || []) {
        const key = "main:" + m.index;
        const vm = ctx.view.materials.get(key);
        if (vm && (vm.matMode === 3 || m.active)) {
          out.push({ layer: "main", matIndex: m.index, modelId: item.id, hash: m.active });
          break;
        }
      }
      return out;
    }
    const rx = targetKey === "lower" ? /^feet_/i : /^hand_/i;
    for (const [pid, meta] of S.partMeta.entries()) {
      if (!S.parts.has(pid) || !meta) continue;
      if (!rx.test(meta.name || "")) continue;
      for (const m of meta.materials || []) {
        const st = ctx.getPartAlbedoHash(pid, meta, m);
        out.push({ layer: "part" + pid, matIndex: m.index, modelId: pid, hash: st });
        break;
      }
    }
    return out;
  }

  function drawUvOverlay(g, img, w, h, tf, op, style) {
    const su = Math.max(0.01, tf.su ?? 0.22);
    const sv = Math.max(0.01, tf.sv ?? 0.22);
    const tw = Math.max(12, su * w);
    const th = Math.max(12, sv * h);
    const u = tf.u ?? 0.5;
    const v = tf.v ?? 0.5;
    const ang = tf.angle || 0;
    const flipU = !!tf.flipU;
    const drawOnce = (pu, pv, pAng, pFlip) => {
      g.save();
      g.translate(pu * w, pv * h);
      g.rotate(pAng);
      if (pFlip) g.scale(-1, 1);
      if (style === "tattoo") {
        g.globalAlpha = op * 0.72;
        g.globalCompositeOperation = "multiply";
        g.drawImage(img, -tw * 0.5, -th * 0.5, tw, th);
        g.globalAlpha = op * 0.52;
        g.globalCompositeOperation = "source-over";
        g.drawImage(img, -tw * 0.5, -th * 0.5, tw, th);
      } else if (style === "multiply") {
        g.globalAlpha = op;
        g.globalCompositeOperation = "multiply";
        g.drawImage(img, -tw * 0.5, -th * 0.5, tw, th);
      } else {
        g.globalAlpha = op;
        g.globalCompositeOperation = "source-over";
        g.drawImage(img, -tw * 0.5, -th * 0.5, tw, th);
      }
      g.restore();
    };
    drawOnce(u, v, ang, flipU);
    if (tf.mirrorU) {
      drawOnce(1.0 - u, v, -ang, !flipU);
    }
  }

  async function rebakeTattoos(ctx, targetKey, opts = {}) {
    if (!ctx) return;
    const targets = findSkinTargets(ctx, targetKey);
    if (!targets.length) {
      if (!opts.silent) {
        ctx.status(`No mounted ${targetKey} skin mesh found — assemble character body first.`);
      }
      return;
    }
    const tatList = ST.appliedTattoos.filter((t) => (t.target || "upper") === targetKey);
    const ovList = ST.appliedFaceOverlays.filter((o) => (o.target || "head") === targetKey);
    const reqRes = Math.min(2048, ctx.curTexRes ? ctx.curTexRes() : 1024);
    for (const tgt of targets) {
      if (!tgt.hash) continue;
      const cacheKey = `${tgt.layer}:${tgt.matIndex}:${tgt.hash}`;
      let baseImg = ST._baseTexCache.get(cacheKey);
      if (!baseImg) {
        try {
          baseImg = await ctx.textureImage(tgt.hash, reqRes, tgt.modelId);
          ST._baseTexCache.set(cacheKey, baseImg);
        } catch { continue; }
      }
      if (!tatList.length && !ovList.length) {
        ctx.view.setTexture(tgt.matIndex, baseImg, tgt.layer);
        continue;
      }
      const w = Math.min(2048, baseImg.width || 1024);
      const h = Math.min(2048, baseImg.height || 1024);
      const cv = document.createElement("canvas");
      cv.width = w; cv.height = h;
      const g = cv.getContext("2d");
      g.drawImage(baseImg, 0, 0, w, h);

      // 1) Makeup / facial / skin overlays (baked first so ink sits crisply on top if overlapping)
      for (const ov of ovList) {
        const hash = ov.hash || ov.albedo;
        if (!hash) continue;
        let ovImg;
        try {
          const tints = ov.tints || [];
          const mode = ov.tintMode || (tints.length ? "mask" : "direct");
          if (tints.length && mode === "mask") {
            ovImg = await overlayImage(hash, tints, mode, 512);
          } else {
            ovImg = await ctx.textureImage(hash, 1024, 0);
          }
        } catch { continue; }
        const tf = ov.uvTransform || { u: 0.5, v: 0.5, su: 1.0, sv: 1.0, angle: 0, flipU: false, mirrorU: false };
        const op = Math.max(0.05, Math.min(1.0, ov.opacity ?? 0.85));
        const style = ov.blendMode === "multiply" ? "multiply" : "source-over";
        drawUvOverlay(g, ovImg, w, h, tf, op, style);
      }

      // 2) Tattoos (native character tattoos + user-stamped decorations)
      for (const entry of tatList) {
        const tat = entry.tattoo || entry;
        const hash = tat.albedo || entry.hash;
        if (!hash) continue;
        let tatImg;
        try {
          const tints = entry.tints || tat.tintPalette || [];
          const mode = entry.tintMode || tat.tintMode || "direct";
          if (tints.length && mode === "mask") {
            tatImg = await overlayImage(hash, tints, mode, 512);
          } else {
            tatImg = await ctx.textureImage(hash, 1024, 0);
          }
        } catch { continue; }
        const baseTf = entry.uvTransform || tat;
        const scale = entry.scaleMult || 1.0;
        const tf = {
          u: (baseTf.u ?? 0.5) + (entry.offU || 0),
          v: (baseTf.v ?? 0.5) + (entry.offV || 0),
          su: (baseTf.su || 0.22) * scale,
          sv: (baseTf.sv || 0.22) * scale,
          angle: (baseTf.angle || 0) + ((entry.rotDeg || 0) * Math.PI / 180),
          flipU: !!baseTf.flipU,
          mirrorU: !!baseTf.mirrorU,
        };
        const op = Math.max(0.05, Math.min(1.0, (entry.opacity ?? 0.88) * (1.0 - (entry.fade || 0) * 0.55)));
        drawUvOverlay(g, tatImg, w, h, tf, op, "tattoo");
      }
      ctx.view.setTexture(tgt.matIndex, cv, tgt.layer);
    }
  }

  function applyNativeOverlaysFromState() {
    const ov = ST.nativeOverlays;
    ST.appliedTattoos = ST.appliedTattoos.filter((t) => !t._native);
    ST.appliedFaceOverlays = ST.appliedFaceOverlays.filter((o) => !o._native);
    if (!ov) return;
    if (ST.autoNativeTattoos && Array.isArray(ov.tattoos)) {
      for (const t of ov.tattoos) {
        ST.appliedTattoos.push({ ...t, _native: true });
      }
    }
    if (ST.autoNativeMakeup) {
      let layers = null;
      const looks = ov.makeupLooks || [];
      if (looks.length && ST.activeMakeupLookId) {
        const chosen = looks.find((l) => l.id === ST.activeMakeupLookId) || looks[0];
        if (chosen && Array.isArray(chosen.overlays)) layers = chosen.overlays;
      }
      if (!layers && Array.isArray(ov.faceOverlays)) {
        layers = ov.faceOverlays;
      }
      for (const l of layers || []) {
        ST.appliedFaceOverlays.push({ ...l, _native: true });
      }
    }
  }

  async function rebakeAllActiveOverlays(ctx, opts = { silent: true }) {
    if (!ctx) return;
    await rebakeTattoos(ctx, "head", opts);
    await rebakeTattoos(ctx, "upper", opts);
    await rebakeTattoos(ctx, "lower", opts);
  }

  function renderNativeOverlaysBox(ctx) {
    const box = $("cfxNativeOvBox");
    if (!box) return;
    const ov = ST.nativeOverlays;
    if (!ov) {
      box.innerHTML = `<p class="note">Scanning <code>0DRP</code> &amp; <code>00001ae00000.pso</code> for native character tattoos &amp; makeup…</p>`;
      return;
    }
    const nTats = (ov.tattoos || []).length;
    const nFace = (ov.faceOverlays || []).length;
    const looks = ov.makeupLooks || [];
    if (!nTats && !nFace && !looks.length) {
      box.innerHTML = `<p class="note">No character-native tattoos or facial makeup overrides baked in <code>0DRP</code>. Browse the libraries below to apply any tattoo or makeup look.</p>`;
      return;
    }
    const lookOpts = looks.map((l) =>
      `<option value="${esc(l.id)}" ${l.id === ST.activeMakeupLookId ? "selected" : ""}>${esc(l.label)} (${l.overlayCount} layers)</option>`
    ).join("");
    box.innerHTML = `
      <div class="cfx-native-ov" style="padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:rgba(255,255,255,0.03);margin-bottom:8px">
        <div class="cfx-subhead" style="margin-top:0">Character Native (<code>0DRP</code> + <code>00001ae00000.pso</code>)</div>
        <div class="cfx-row-inline" style="flex-wrap:wrap;gap:10px;margin-top:4px">
          ${nTats ? `<label class="proc-chk"><input type="checkbox" id="cfxNativeTatChk" ${ST.autoNativeTattoos ? "checked" : ""}> Native Tattoos (${nTats})</label>` : ""}
          ${(nFace || looks.length) ? `<label class="proc-chk"><input type="checkbox" id="cfxNativeMkChk" ${ST.autoNativeMakeup ? "checked" : ""}> Native Makeup (${nFace} layers)</label>` : ""}
        </div>
        ${looks.length ? `
          <div class="proc-grid" style="margin-top:6px">
            <label><span>Makeup Look</span><select id="cfxNativeLookSel">${lookOpts}</select></label>
          </div>
        ` : ""}
      </div>
    `;
    const refreshAndBake = async () => {
      applyNativeOverlaysFromState();
      if ($("cfxTatAppliedCnt")) $("cfxTatAppliedCnt").textContent = String(ST.appliedTattoos.length);
      if ($("cfxMkAppliedCnt")) $("cfxMkAppliedCnt").textContent = String(ST.appliedFaceOverlays.length);
      await rebakeAllActiveOverlays(ctx, { silent: true });
    };
    if ($("cfxNativeTatChk")) $("cfxNativeTatChk").onchange = async (e) => {
      ST.autoNativeTattoos = e.target.checked;
      await refreshAndBake();
    };
    if ($("cfxNativeMkChk")) $("cfxNativeMkChk").onchange = async (e) => {
      ST.autoNativeMakeup = e.target.checked;
      await refreshAndBake();
    };
    if ($("cfxNativeLookSel")) $("cfxNativeLookSel").onchange = async (e) => {
      ST.activeMakeupLookId = e.target.value;
      ST.autoNativeMakeup = true;
      if ($("cfxNativeMkChk")) $("cfxNativeMkChk").checked = true;
      await refreshAndBake();
    };
  }

  async function loadCharacterNativeOverlays(item, ctx) {
    if (!item || !ctx) return;
    const tok = ++ST._overlayReqTok;
    try {
      const ov = await ctx.api("char-overlays", { id: item.id });
      if (tok !== ST._overlayReqTok || ctx.state.item !== item) return;
      ST.nativeOverlays = ov;
      const looks = ov?.makeupLooks || [];
      if (looks.length) {
        const defLook = looks.find((l) => l.isDefault) || looks[0];
        ST.activeMakeupLookId = defLook.id;
      } else {
        ST.activeMakeupLookId = "";
      }
      applyNativeOverlaysFromState();
      renderNativeOverlaysBox(ctx);
      if ($("cfxTatAppliedCnt")) $("cfxTatAppliedCnt").textContent = String(ST.appliedTattoos.length);
      if ($("cfxMkAppliedCnt")) $("cfxMkAppliedCnt").textContent = String(ST.appliedFaceOverlays.length);
      if (ST.appliedTattoos.length || ST.appliedFaceOverlays.length) {
        await rebakeAllActiveOverlays(ctx, { silent: true });
      }
    } catch {
      if (tok !== ST._overlayReqTok) return;
      ST.nativeOverlays = { tattoos: [], faceOverlays: [], makeupLooks: [] };
      renderNativeOverlaysBox(ctx);
    }
  }

  async function onPartsReady(item) {
    const ctx = ST._lastCtx;
    if (!ctx || !item || ctx.state.item !== item) return;
    if (ST.appliedTattoos.length || ST.appliedFaceOverlays.length) {
      await rebakeAllActiveOverlays(ctx, { silent: true });
    }
  }

  // --- Helper: Slider HTML ---
  function sliderRow(id, label, min, max, step, val, fmt = (v) => Number(v).toFixed(2)) {
    return `<label><span>${esc(label)}</span><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${val}"><span class="mono" id="${id}Val">${esc(fmt(val))}</span></label>`;
  }

  // --- Main Render Entrypoint ---
  async function render(item, ctx) {
    const host = $("charFxBody");
    if (!host || !isHumanCharacter(item)) return;
    ST._lastItem = item;
    ST._lastCtx = ctx;
    const rk = rigKindOf(item);
    const rt = rigTokenOf(item);
    const view = ctx.view;
    const defH = rk === "male" ? 1.82 : 1.72;
    view.charMorph.defaultHeight = defH;
    if (!view.charMorph.height) {
      view.charMorph.height = defH;
    }

    host.innerHTML = `<p class="note">Loading RAGE 9 character effect catalogs…</p>`;
    let cat, gore;
    try {
      [cat, gore] = await Promise.all([fetchCatalog(ctx.api, rk), fetchGore(ctx.api, rt)]);
    } catch (e) {
      host.innerHTML = `<p class="note warn">Failed to load character effects catalog: ${esc(e.message)}</p>`;
      return;
    }
    if (ctx.state.item !== item) return;
    ST.catalog = cat;
    ST.goreItems = gore;

    const mandyStages = (cat.drugPresets || []).filter((p) => p.isMandy);
    const drugGroups = new Map();
    for (const p of cat.drugPresets || []) {
      if (!drugGroups.has(p.group)) drugGroups.set(p.group, []);
      drugGroups.get(p.group).push(p);
    }
    const drugOpts = [`<option value="">None (Sober / Neutral)</option>`];
    for (const [grp, arr] of drugGroups.entries()) {
      drugOpts.push(`<optgroup label="${esc(grp)}">${arr.map((p) =>
        `<option value="${esc(p.id)}" ${p.id === ST.activeDrugId ? "selected" : ""}>${esc(p.label)}</option>`
      ).join("")}</optgroup>`);
    }

    const dirtOpts = (cat.dirtDefinitions || []).map((d) =>
      `<option value="${esc(d.id)}" ${d.id === ST.activeDirtId ? "selected" : ""}>${esc(d.label)} (rough ${d.roughLow}–${d.roughHigh})</option>`
    ).join("");

    const nativeMorphs = item?.characterAssembly?.bodyMorphs;
    const hasNativeMorphs = !!(nativeMorphs && Object.keys(nativeMorphs).length);
    const nativeOpt = hasNativeMorphs
      ? `<option value="CHARACTER_NATIVE" ${ST.activeShapeId === "CHARACTER_NATIVE" ? "selected" : ""}>Character Native (0DRP · ${Object.keys(nativeMorphs).length} ch)</option>`
      : "";
    const shapeOpts = nativeOpt + (cat.bodyShapes?.presets || []).map((s) =>
      `<option value="${esc(s.id)}" ${s.id === ST.activeShapeId ? "selected" : ""}>${esc(s.label)}</option>`
    ).join("");

    const bloodDecalOpts = [`<option value="">Procedural Bullet &amp; Spatter</option>`]
      .concat((cat.bloodDecals || []).map((b) =>
        `<option value="${esc(b.hash)}" ${b.hash === ST.activeBloodDecalHash ? "selected" : ""}>${esc(b.label)} (${b.width}×${b.height})</option>`
      )).join("");

    const quickAnimBtns = (cat.quickAnims || []).map((a) =>
      `<button type="button" class="cfx-pill" data-qanim="${esc(a.key)}" title="${esc(a.dictName)} / ${esc(a.name)} (${a.duration}s, ${a.matchedBones} bones)">${esc(a.label)}</button>`
    ).join("");

    const cf = view.charFx;
    const cm = view.charMorph;
    const pf = view.postFx;

    host.innerHTML = `
      <div class="cfx-studio">
        <div class="cfx-top-bar">
          <span class="proc-tag">RAGE 9 Live</span>
          <button type="button" id="cfxResetAll" class="cfx-mini-btn">Reset All Effects</button>
        </div>

        <!-- 1. DRUG & STATUS EFFECTS (Mandy_*, Cocaine, Molly, Weed, Drunk, Trips, Combat) -->
        <details class="cfx-panel" open>
          <summary><span>Drug &amp; Status Effects</span><small>Mandy (MDMA) · Coke · Molly · Trips</small></summary>
          <div class="cfx-body">
            <p class="note">Exact keyframe curves from <code>TC_mods_Mission.xml</code>, <code>TC_mods_postfx.xml</code> &amp; <code>TC_mods_APFX.xml</code>. Drives GPU post-FX (warp, custom CA, bloom, trails, vignette), <code>brdf_glints</code>, <code>ped_light_fresnel</code> &amp; matching animations.</p>
            <div class="cfx-mandy-strip" id="cfxMandyStrip">
              <span class="cfx-strip-lbl">Mandy (MDMA) 7-Stage Progression:</span>
              <div class="cfx-btn-row">
                ${mandyStages.map((m, idx) => `<button type="button" class="cfx-pill" data-drug="${esc(m.id)}" aria-pressed="${ST.activeDrugId === m.id}" title="${esc(m.description)}">${idx + 1}. ${esc(m.id.replace(/^Mandy_/, ""))}</button>`).join("")}
                <button type="button" class="cfx-pill" data-drug="" aria-pressed="${!ST.activeDrugId}">Off</button>
              </div>
            </div>
            <div class="proc-grid" style="margin-top:6px">
              <label><span>Effect preset</span><select id="cfxDrugSel">${drugOpts.join("")}</select></label>
              ${sliderRow("cfxDrugInt", "Intensity", 0, 1.5, 0.02, pf.intensity ?? 1.0, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxDrugBpm", "Beat BPM", 60, 180, 1, pf.bpm || 126, (v) => Math.round(v) + " bpm")}
              ${sliderRow("cfxDrugPhase", "Axis phase", 0, 1, 0.01, pf.phase || 0, (v) => Number(v).toFixed(2))}
            </div>
            <div class="cfx-row-inline">
              <label class="proc-chk"><input type="checkbox" id="cfxDrugAnim" ${pf.animate ? "checked" : ""}> Live beat / heartbeat pulse</label>
              <button type="button" id="cfxDrugPlayAnim" class="cfx-mini-btn" disabled>▶ Play matching anim</button>
            </div>
            <div id="cfxDrugInfo" class="cfx-readout">Select a drug/status modifier above (e.g. <b>Mandy_Spangled</b>, <b>Molly</b>, <b>Cocaine UV</b>, <b>Drunk</b>).</div>
          </div>
        </details>

        <!-- 2. SKIN WEATHERING, WETNESS & SUNTAN -->
        <details class="cfx-panel" open>
          <summary><span>Skin Weathering, Wetness &amp; SunTan</span><small>WET_SKIN · DUST · UNDERWATER</small></summary>
          <div class="cfx-body">
            <div class="cfx-btn-row" id="cfxWetPresets">
              <button type="button" class="cfx-pill" data-wetpre="dry">Dry</button>
              <button type="button" class="cfx-pill" data-wetpre="rain">Rain / Wet</button>
              <button type="button" class="cfx-pill" data-wetpre="sweat">Sweaty / Club</button>
              <button type="button" class="cfx-pill" data-wetpre="wetdust">Wet + Dust</button>
              <button type="button" class="cfx-pill" data-wetpre="mud">Muddy</button>
              <button type="button" class="cfx-pill" data-wetpre="underwater">Underwater</button>
              <button type="button" class="cfx-pill" data-wetpre="suntan">Beach Tan</button>
              <button type="button" class="cfx-pill" data-wetpre="sunburn">Sunburn</button>
            </div>
            <div class="proc-grid" style="margin-top:6px">
              ${sliderRow("cfxWetness", "Wetness (WET_SKIN)", 0, 1, 0.01, cf.wetness, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxSoak", "Cloth Soak", 0, 1, 0.01, cf.soak, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxWaterH", "Wet Height Line", 0, 2.2, 0.02, cf.waterHeight, (v) => Number(v).toFixed(2) + " m")}
              ${sliderRow("cfxDroplets", "Droplets / Rivulets", 0, 1, 0.02, cf.droplets, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxEyeWet", "Eye Wetness", 0, 1, 0.02, cf.eyeWetness, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxDust", "Dust (WET_DUST)", 0, 1, 0.01, cf.dust, (v) => Math.round(v * 100) + "%")}
              <label><span>Dirt Definition</span><select id="cfxDirtSel">${dirtOpts}</select></label>
              ${sliderRow("cfxDirtAmt", "Dirt / Mud Amount", 0, 1, 0.01, cf.dirtAmt, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxUnderwater", "Underwater", 0, 1, 0.01, cf.underwater, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxWaterLine", "Water Surface Y", 0, 2.2, 0.02, cf.waterLine, (v) => Number(v).toFixed(2) + " m")}
              ${sliderRow("cfxCaustics", "Caustics", 0, 1, 0.02, cf.caustics, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxSunTan", "SunTan (Melanin)", -1, 1, 0.02, cf.sunTan, (v) => (v >= 0 ? "+" : "") + Math.round(v * 100) + "%")}
              ${sliderRow("cfxSunBurn", "Sunburn Redness", 0, 1, 0.02, cf.sunBurn, (v) => Math.round(v * 100) + "%")}
              <label><span>Tan-Line Mask</span><select id="cfxTanLines">
                <option value="0" ${cf.tanLines === 0 ? "selected" : ""}>None (Full-body exposure)</option>
                <option value="1" ${cf.tanLines === 1 ? "selected" : ""}>Bikini / Swimwear</option>
                <option value="2" ${cf.tanLines === 2 ? "selected" : ""}>Tank Top + Shorts</option>
                <option value="3" ${cf.tanLines === 3 ? "selected" : ""}>Swim Shorts Only</option>
                <option value="4" ${cf.tanLines === 4 ? "selected" : ""}>T-Shirt + Pants (Farmer's Tan)</option>
              </select></label>
              ${sliderRow("cfxFlush", "Vascular Flush", 0, 1, 0.02, cf.flush, (v) => Math.round(v * 100) + "%")}
            </div>
          </div>
        </details>

        <!-- 3. DYNAMIC PHYSIOLOGY & SOFT-TISSUE PHYSICS -->
        <details class="cfx-panel"${hasNativeMorphs ? " open" : ""}>
          <summary><span>Dynamic Physiology &amp; Soft-Tissue Physics</span><small>${hasNativeMorphs ? "Character Native Active · " : ""}131 BM_* · 4 SEC_*</small></summary>
          <div class="cfx-body">
            <p class="note">Drives the real <code>BM_*</code> morph bones &amp; <code>SEC_*</code> soft-tissue bones live on the GPU skeleton (works in bind pose &amp; during any <code>anim.rpf</code> animation). ${hasNativeMorphs ? "Auto-loaded from this character's <code>0DRP</code> + <code>000106153000.pso</code> BodyShapePreset." : "Presets parsed from <code>000000226000.xml</code>."}</p>
            <div class="proc-grid">
              <label><span>BodyShapePreset</span><select id="cfxShapeSel">${shapeOpts}</select></label>
              ${sliderRow("cfxHeight", "Height (m)", 1.48, 2.08, 0.01, cm.height, (v) => Number(v).toFixed(2) + " m")}
              ${sliderRow("cfxMasterW", "Master Weight", -1.0, 1.0, 0.02, cm.weight, (v) => (v >= 0 ? "+" : "") + Math.round(v * 100) + "%")}
              ${sliderRow("cfxMasterM", "Master Muscle", 0.0, 1.0, 0.02, cm.muscle, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxBreasts", "Chest / Breasts", -0.5, 1.0, 0.02, cm.GCBreasts, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2))}
              ${sliderRow("cfxCleavage", "Cleavage", -0.75, 0.75, 0.02, cm.GCCleavage, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2))}
              ${sliderRow("cfxButt", "Glutes / Butt", -0.5, 1.0, 0.02, cm.GCButt, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2))}
              ${sliderRow("cfxHipW", "Hip Width", -1.0, 1.0, 0.02, cm.gchipwidth || 0, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2))}
              ${sliderRow("cfxHips", "Hips / Waist", -1.0, 1.0, 0.02, cm.gcweighthip, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2))}
              ${sliderRow("cfxBelly", "Belly / Gut", -1.0, 1.2, 0.02, cm.gcweightbelly, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2))}
            </div>
            <div class="cfx-subhead">Soft-Tissue Secondary Motion (Spring-Damper)</div>
            <div class="proc-grid">
              ${sliderRow("cfxJigChest", "Chest Jiggle (SEC_*)", 0, 1.5, 0.05, cm.jiggleChest, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxJigButt", "Glute Jiggle (BM_*)", 0, 1.5, 0.05, cm.jiggleButt, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxJigBelly", "Belly Jiggle (BM_*)", 0, 1.5, 0.05, cm.jiggleBelly, (v) => Math.round(v * 100) + "%")}
              ${sliderRow("cfxJigStiff", "Spring Stiffness", 10, 65, 1, cm.jiggleStiff, (v) => Math.round(v))}
              ${sliderRow("cfxJigDamp", "Damping", 1.5, 10, 0.2, cm.jiggleDamp, (v) => Number(v).toFixed(1))}
            </div>
            <div class="row-actions" style="margin-top:6px">
              <button type="button" id="cfxBounceBtn">Bounce Impulse</button>
              <button type="button" id="cfxResetMorph">${hasNativeMorphs ? "Reset to Native" : "Reset Physiology"}</button>
              ${hasNativeMorphs ? `<button type="button" id="cfxZeroMorph">Neutral (Zero)</button>` : ""}
            </div>
            <details class="cfx-nested">
              <summary>All ${ALL_MORPH_CHANNELS.length} Native Morph Channels (<code>000106153000.pso</code>)</summary>
              <div class="proc-grid" id="cfxAllMorphs" style="margin-top:6px">
                ${ALL_MORPH_CHANNELS.map(([k, lbl, mn, mx]) =>
                  `<label><span>${esc(lbl)}</span><input type="range" data-mch="${esc(k)}" min="${mn}" max="${mx}" step="0.02" value="${cm[k] || 0}"><span class="mono" data-mchv="${esc(k)}">${Number(cm[k] || 0).toFixed(2)}</span></label>`
                ).join("")}
              </div>
            </details>
          </div>
        </details>

        <!-- 4. BODY DAMAGE, BLOOD, WOUNDS & DISMEMBERMENT -->
        <details class="cfx-panel">
          <summary><span>Body Damage, Blood, Wounds &amp; Gore</span><small>Organs · Decals · ${gore.length} Stumps</small></summary>
          <div class="cfx-body">
            <div class="proc-grid">
              ${sliderRow("cfxBloodSoak", "Blood Soak (000ab)", 0, 1, 0.01, cf.bloodSoak, (v) => Math.round(v * 100) + "%")}
              <label><span>Wound Type</span><select id="cfxWoundKind">
                <option value="0" ${ST.woundKind === 0 ? "selected" : ""}>Gunshot / Blood Spatter</option>
                <option value="1" ${ST.woundKind === 1 ? "selected" : ""}>Bruise / Contusion</option>
                <option value="2" ${ST.woundKind === 2 ? "selected" : ""}>Scrape / Road Rash</option>
                <option value="3" ${ST.woundKind === 3 ? "selected" : ""}>Burn / Scorch Mark</option>
              </select></label>
              <label><span>VFX Blood Decal</span><select id="cfxBloodDecal">${bloodDecalOpts}</select></label>
              ${sliderRow("cfxWoundSize", "Wound Radius", 0.025, 0.16, 0.005, ST.woundSize, (v) => Math.round(v * 100) + " cm")}
              ${sliderRow("cfxWoundDrip", "Blood Drip Length", 0.0, 0.45, 0.02, ST.woundDrip, (v) => Math.round(v * 100) + " cm")}
            </div>
            <div class="cfx-row-inline" style="margin-top:6px">
              <label class="proc-chk" title="Click anywhere on the 3D character to stamp a wound in bind space (moves with animation!)"><input type="checkbox" id="cfxClickStamp" ${ST.clickStamp ? "checked" : ""}> Click 3D model to stamp</label>
              <label class="proc-chk" title="Show 000000046000.xml Heart (5.5cm 2×), Throat (3.25cm 4×), Brain & Spine spheres"><input type="checkbox" id="cfxVitalOrgans" ${ST.showVitalOrgans ? "checked" : ""}> Vital organs X-Ray</label>
            </div>
            <div class="cfx-subhead">Quick Hit Zones (moves with animated rig · max 8 active)</div>
            <div class="cfx-btn-row" id="cfxQuickWounds">
              ${QUICK_WOUND_ZONES.map((z) => `<button type="button" class="cfx-pill" data-wzone="${esc(z.id)}">${esc(z.label)}</button>`).join("")}
              <button type="button" class="cfx-pill warn" id="cfxClearWounds">Clear Wounds (<span id="cfxWoundCnt">${(cf.wounds || []).length}</span>)</button>
            </div>
            ${gore.length ? `
              <div class="cfx-subhead">Dismemberment / Rigged Gore Stumps (<code>${esc(rt)}</code>)</div>
              <div class="cfx-gore-grid" id="cfxGoreGrid">
                ${gore.map((g) => `<label class="cfx-gore-item"><input type="checkbox" data-gore="${esc(g.limb)}" ${ST.severedLimbs.has(g.limb) ? "checked" : ""}><span>${esc(g.label)}</span></label>`).join("")}
              </div>
              <div class="row-actions" style="margin-top:4px"><button type="button" id="cfxClearGore">Restore All Limbs</button></div>
            ` : ""}
          </div>
        </details>

        <!-- 5. TATTOOS & FACIAL OVERLAYS (00001ae00000.pso -> decorations_0..3_3.ttpf) -->
        <details class="cfx-panel" open>
          <summary><span>Tattoos &amp; Facial Overlays</span><small>Native + Makeup + 4,775 Tattoos</small></summary>
          <div class="cfx-body">
            <div id="cfxNativeOvBox"></div>
            <p class="note">Composites real <code>decorations_0..3_3.ttpf</code> textures using exact <code>00001ae00000.pso</code> UV matrices &amp; RGB tint masks onto head, upper body, or lower body skin.</p>

            <details class="cfx-nested" id="cfxMkDetails">
              <summary>Makeup &amp; Skin Overlay Library (<span id="cfxMkAppliedCnt">${ST.appliedFaceOverlays.length}</span> active)</summary>
              <div style="margin-top:6px">
                <div class="cfx-row-inline" style="gap:6px">
                  <select id="cfxMkKind" style="max-width:140px"><option value="">All kinds</option></select>
                  <input type="search" id="cfxMkSearch" placeholder="Search lipstick, eyeliner, blush…" value="${esc(ST.makeupQ)}" spellcheck="false" style="flex:1">
                  <button type="button" id="cfxMkClear" class="cfx-mini-btn">Clear</button>
                </div>
                <div class="cfx-tat-grid" id="cfxMkGrid" style="margin-top:6px"><p class="note">Expand to browse makeup &amp; skin overlays…</p></div>
                <div class="anim-pager">
                  <button type="button" id="cfxMkPrev" disabled>‹ Prev</button>
                  <span id="cfxMkPage">–</span>
                  <button type="button" id="cfxMkNext" disabled>Next ›</button>
                </div>
              </div>
            </details>

            <details class="cfx-nested" id="cfxTatDetails" style="margin-top:6px">
              <summary>Tattoo Library (<span id="cfxTatAppliedCnt">${ST.appliedTattoos.length}</span> active)</summary>
              <div style="margin-top:6px">
                <div class="proc-grid">
                  <label><span>Target Region</span><select id="cfxTatTarget">
                    <option value="upper" ${ST.tattooTarget === "upper" ? "selected" : ""}>Upper Body (Torso / Arms)</option>
                    <option value="lower" ${ST.tattooTarget === "lower" ? "selected" : ""}>Lower Body (Hips / Legs)</option>
                    <option value="head" ${ST.tattooTarget === "head" ? "selected" : ""}>Head / Face / Neck</option>
                  </select></label>
                  ${sliderRow("cfxTatOp", "Ink Opacity", 0.1, 1.0, 0.02, ST.tattooOpacity, (v) => Math.round(v * 100) + "%")}
                  ${sliderRow("cfxTatFade", "Age / Fade", 0.0, 1.0, 0.02, ST.tattooFade, (v) => Math.round(v * 100) + "%")}
                  ${sliderRow("cfxTatScale", "Scale Mult", 0.3, 3.0, 0.05, ST.tattooScaleMult, (v) => Number(v).toFixed(2) + "×")}
                  ${sliderRow("cfxTatU", "UV Offset X", -0.45, 0.45, 0.01, ST.tattooOffU, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2))}
                  ${sliderRow("cfxTatV", "UV Offset Y", -0.45, 0.45, 0.01, ST.tattooOffV, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2))}
                  ${sliderRow("cfxTatRot", "Rotation", -180, 180, 5, ST.tattooRotDeg, (v) => Math.round(v) + "°")}
                </div>
                <div class="cfx-row-inline" style="margin-top:6px">
                  <input type="search" id="cfxTatSearch" placeholder="Search tattoo hash/ID…" value="${esc(ST.tattooQ)}" spellcheck="false" style="flex:1">
                  <button type="button" id="cfxTatClear" class="cfx-mini-btn">Clear Tattoos</button>
                </div>
                <div class="cfx-tat-grid" id="cfxTatGrid"><p class="note">Expand to browse tattoos…</p></div>
                <div class="anim-pager">
                  <button type="button" id="cfxTatPrev" disabled>‹ Prev</button>
                  <span id="cfxTatPage">–</span>
                  <button type="button" id="cfxTatNext" disabled>Next ›</button>
                </div>
              </div>
            </details>
          </div>
        </details>

        <!-- 6. QUICK CHARACTER ANIMATIONS -->
        ${quickAnimBtns ? `
        <details class="cfx-panel">
          <summary><span>Quick Effect &amp; State Animations</span><small>${cat.quickAnims.length} Curated Clips</small></summary>
          <div class="cfx-body">
            <p class="note">1-click verified <code>anim.rpf</code> clips matched to this character's skeleton (<code>${esc(rk)}</code>).</p>
            <div class="cfx-btn-row" id="cfxQuickAnims">${quickAnimBtns}</div>
          </div>
        </details>
        ` : ""}
      </div>
    `;

    // ---------------- Wiring Handlers ----------------
    const bindSlider = (id, fmt, onChange) => {
      const el = $(id), vl = $(id + "Val");
      if (!el) return;
      el.oninput = () => {
        const v = Number(el.value);
        if (vl) vl.textContent = fmt(v);
        onChange(v);
      };
    };

    const setSliderVal = (id, v, fmt) => {
      const el = $(id), vl = $(id + "Val");
      if (el) el.value = String(v);
      if (vl) vl.textContent = fmt(v);
    };

    // Quick anim playback helper
    const playQuickAnimByKey = (key) => {
      const qa = (cat.quickAnims || []).find((a) => a.key === key);
      if (!qa) { ctx.status(`No local animation clip found for "${key}".`); return; }
      ctx.loadAnimClip(qa);
    };

    // 1. Drug & Status Effects
    const applyDrugPreset = (drugId) => {
      ST.activeDrugId = drugId || "";
      const sel = $("cfxDrugSel");
      if (sel) sel.value = ST.activeDrugId;
      $("cfxMandyStrip")?.querySelectorAll("[data-drug]").forEach((b) =>
        b.setAttribute("aria-pressed", b.dataset.drug === ST.activeDrugId)
      );
      const pre = (cat.drugPresets || []).find((p) => p.id === ST.activeDrugId) || null;
      const playBtn = $("cfxDrugPlayAnim");
      const infoEl = $("cfxDrugInfo");
      if (!pre) {
        view.setPostFx({ active: false, preset: null });
        if (playBtn) playBtn.disabled = true;
        if (infoEl) infoEl.innerHTML = `Sober / neutral rendering (post-FX bypassed).`;
        return;
      }
      view.setPostFx({
        active: true,
        preset: pre,
        intensity: Number($("cfxDrugInt")?.value || 1.0),
        bpm: Number($("cfxDrugBpm")?.value || 126),
        animate: !!$("cfxDrugAnim")?.checked,
        phase: 0,
      });
      if (playBtn) {
        playBtn.disabled = !pre.animKey;
        playBtn.dataset.animkey = pre.animKey || "";
        playBtn.textContent = pre.animKey ? `▶ Play ${pre.animKey} anim` : "▶ Play matching anim";
      }
      if (infoEl) {
        infoEl.innerHTML = `<b>${esc(pre.id)}</b> · axis <code>${esc(pre.axis)}</code> · <code>${esc(pre.source)}</code><br>${esc(pre.description)}`;
      }
    };

    $("cfxMandyStrip")?.querySelectorAll("[data-drug]").forEach((btn) => {
      btn.onclick = () => applyDrugPreset(btn.dataset.drug);
    });
    if ($("cfxDrugSel")) $("cfxDrugSel").onchange = (e) => applyDrugPreset(e.target.value);
    bindSlider("cfxDrugInt", (v) => Math.round(v * 100) + "%", (v) => view.setPostFx({ intensity: v }));
    bindSlider("cfxDrugBpm", (v) => Math.round(v) + " bpm", (v) => view.setPostFx({ bpm: v }));
    bindSlider("cfxDrugPhase", (v) => v.toFixed(2), (v) => {
      if ($("cfxDrugAnim")) $("cfxDrugAnim").checked = false;
      view.setPostFx({ animate: false, phase: v });
    });
    if ($("cfxDrugAnim")) $("cfxDrugAnim").onchange = (e) => view.setPostFx({ animate: e.target.checked });
    if ($("cfxDrugPlayAnim")) $("cfxDrugPlayAnim").onclick = (e) => {
      const k = e.currentTarget.dataset.animkey;
      if (k) playQuickAnimByKey(k);
    };
    view.onPostFxTick((p) => {
      const ph = $("cfxDrugPhase"), pv = $("cfxDrugPhaseVal");
      if (ph && document.activeElement !== ph) ph.value = p.phase.toFixed(2);
      if (pv) pv.textContent = p.phase.toFixed(2);
    });
    if (ST.activeDrugId) applyDrugPreset(ST.activeDrugId);

    // 2. Weathering, Wetness & SunTan
    const applyDirtDef = (dirtId) => {
      ST.activeDirtId = dirtId;
      const d = (cat.dirtDefinitions || []).find((x) => x.id === dirtId);
      if (!d) return;
      view.setCharFx({
        dirtCol: d.rgb,
        dirtRough: [d.roughLow, d.roughHigh],
        dirtSkinStr: d.skinStrength,
        dirtClothStr: d.clothStrength,
      });
    };
    applyDirtDef(ST.activeDirtId);
    if ($("cfxDirtSel")) $("cfxDirtSel").onchange = (e) => applyDirtDef(e.target.value);

    const pct = (v) => Math.round(v * 100) + "%";
    const mtr = (v) => Number(v).toFixed(2) + " m";
    bindSlider("cfxWetness", pct, (v) => view.setCharFx({ wetness: v }));
    bindSlider("cfxSoak", pct, (v) => view.setCharFx({ soak: v }));
    bindSlider("cfxWaterH", mtr, (v) => view.setCharFx({ waterHeight: v }));
    bindSlider("cfxDroplets", pct, (v) => view.setCharFx({ droplets: v }));
    bindSlider("cfxEyeWet", pct, (v) => view.setCharFx({ eyeWetness: v }));
    bindSlider("cfxDust", pct, (v) => view.setCharFx({ dust: v }));
    bindSlider("cfxDirtAmt", pct, (v) => view.setCharFx({ dirtAmt: v }));
    bindSlider("cfxUnderwater", pct, (v) => view.setCharFx({ underwater: v }));
    bindSlider("cfxWaterLine", mtr, (v) => view.setCharFx({ waterLine: v }));
    bindSlider("cfxCaustics", pct, (v) => view.setCharFx({ caustics: v }));
    bindSlider("cfxSunTan", (v) => (v >= 0 ? "+" : "") + Math.round(v * 100) + "%", (v) => view.setCharFx({ sunTan: v }));
    bindSlider("cfxSunBurn", pct, (v) => view.setCharFx({ sunBurn: v }));
    if ($("cfxTanLines")) $("cfxTanLines").onchange = (e) => view.setCharFx({ tanLines: Number(e.target.value) | 0 });
    bindSlider("cfxFlush", pct, (v) => view.setCharFx({ flush: v }));

    const WET_PRESETS = {
      dry:        { wetness: 0, soak: 0, waterHeight: 2.2, droplets: 0.55, dust: 0, dirtAmt: 0, underwater: 0, sunTan: 0, sunBurn: 0, flush: 0 },
      rain:       { wetness: 0.92, soak: 0.85, waterHeight: 2.2, droplets: 0.88, dust: 0, underwater: 0, eyeWetness: 0.75 },
      sweat:      { wetness: 0.58, soak: 0.35, waterHeight: 2.2, droplets: 0.65, flush: 0.45, eyeWetness: 0.65, underwater: 0 },
      wetdust:    { wetness: 0.62, soak: 0.45, waterHeight: 1.5, droplets: 0.35, dust: 0.75, underwater: 0 },
      mud:        { wetness: 0.45, soak: 0.55, dirtAmt: 0.82, dust: 0.25, underwater: 0, _dirt: "AM_Dirtsettings_Mud_Rural" },
      underwater: { wetness: 1.0, soak: 1.0, underwater: 0.95, waterLine: 1.85, caustics: 0.88, droplets: 0.0, dust: 0 },
      suntan:     { wetness: 0.22, droplets: 0.1, sunTan: 0.72, sunBurn: 0.08, tanLines: 1, underwater: 0 },
      sunburn:    { wetness: 0.12, sunTan: 0.28, sunBurn: 0.82, tanLines: 1, flush: 0.35, underwater: 0 },
    };
    $("cfxWetPresets")?.querySelectorAll("[data-wetpre]").forEach((btn) => {
      btn.onclick = () => {
        const p = WET_PRESETS[btn.dataset.wetpre];
        if (!p) return;
        const patch = { ...p };
        if (patch._dirt) {
          if ($("cfxDirtSel")) $("cfxDirtSel").value = patch._dirt;
          applyDirtDef(patch._dirt);
          delete patch._dirt;
        }
        view.setCharFx(patch);
        const f = view.charFx;
        setSliderVal("cfxWetness", f.wetness, pct);
        setSliderVal("cfxSoak", f.soak, pct);
        setSliderVal("cfxWaterH", f.waterHeight, mtr);
        setSliderVal("cfxDroplets", f.droplets, pct);
        setSliderVal("cfxEyeWet", f.eyeWetness, pct);
        setSliderVal("cfxDust", f.dust, pct);
        setSliderVal("cfxDirtAmt", f.dirtAmt, pct);
        setSliderVal("cfxUnderwater", f.underwater, pct);
        setSliderVal("cfxWaterLine", f.waterLine, mtr);
        setSliderVal("cfxCaustics", f.caustics, pct);
        setSliderVal("cfxSunTan", f.sunTan, (v) => (v >= 0 ? "+" : "") + Math.round(v * 100) + "%");
        setSliderVal("cfxSunBurn", f.sunBurn, pct);
        setSliderVal("cfxFlush", f.flush, pct);
        if ($("cfxTanLines")) $("cfxTanLines").value = String(f.tanLines | 0);
      };
    });

    // 3. Dynamic Physiology & Soft-Tissue Physics
    const syncMorphUI = () => {
      const m = view.charMorph;
      setSliderVal("cfxHeight", m.height, mtr);
      setSliderVal("cfxMasterW", m.weight, (v) => (v >= 0 ? "+" : "") + Math.round(v * 100) + "%");
      setSliderVal("cfxMasterM", m.muscle, pct);
      setSliderVal("cfxBreasts", m.GCBreasts, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2));
      setSliderVal("cfxCleavage", m.GCCleavage, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2));
      setSliderVal("cfxButt", m.GCButt, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2));
      setSliderVal("cfxHipW", m.gchipwidth || 0, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2));
      setSliderVal("cfxBelly", m.gcweightbelly, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2));
      setSliderVal("cfxHips", m.gcweighthip, (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2));
      $("cfxAllMorphs")?.querySelectorAll("[data-mch]").forEach((sl) => {
        const k = sl.dataset.mch;
        const val = m[k] || 0;
        sl.value = String(val);
        const lbl = $("cfxAllMorphs").querySelector(`[data-mchv="${k}"]`);
        if (lbl) lbl.textContent = Number(val).toFixed(2);
      });
    };
    if ($("cfxShapeSel")) $("cfxShapeSel").onchange = (e) => {
      ST.activeShapeId = e.target.value;
      if (ST.activeShapeId === "CHARACTER_NATIVE") {
        applyCharacterNativeMorphs(item, view);
        syncMorphUI();
        return;
      }
      const sh = (cat.bodyShapes?.presets || []).find((x) => x.id === ST.activeShapeId);
      if (!sh) return;
      const patch = { weight: 0, muscle: 0 };
      for (const [k] of ALL_MORPH_CHANNELS) patch[k] = sh.expressions?.[k] ?? 0;
      view.setCharMorph(patch);
      syncMorphUI();
    };
    bindSlider("cfxHeight", mtr, (v) => view.setCharMorph({ height: v }));
    bindSlider("cfxMasterW", (v) => (v >= 0 ? "+" : "") + Math.round(v * 100) + "%", (v) => view.setCharMorph({ weight: v }));
    bindSlider("cfxMasterM", pct, (v) => view.setCharMorph({ muscle: v }));
    bindSlider("cfxBreasts", (v) => (v >= 0 ? "+" : "") + v.toFixed(2), (v) => { view.setCharMorph({ GCBreasts: v }); syncMorphUI(); });
    bindSlider("cfxCleavage", (v) => (v >= 0 ? "+" : "") + v.toFixed(2), (v) => { view.setCharMorph({ GCCleavage: v }); syncMorphUI(); });
    bindSlider("cfxButt", (v) => (v >= 0 ? "+" : "") + v.toFixed(2), (v) => { view.setCharMorph({ GCButt: v }); syncMorphUI(); });
    bindSlider("cfxHipW", (v) => (v >= 0 ? "+" : "") + v.toFixed(2), (v) => { view.setCharMorph({ gchipwidth: v }); syncMorphUI(); });
    bindSlider("cfxBelly", (v) => (v >= 0 ? "+" : "") + v.toFixed(2), (v) => { view.setCharMorph({ gcweightbelly: v }); syncMorphUI(); });
    bindSlider("cfxHips", (v) => (v >= 0 ? "+" : "") + v.toFixed(2), (v) => { view.setCharMorph({ gcweighthip: v, gcweightmuffin: v * 0.85 }); syncMorphUI(); });
    bindSlider("cfxJigChest", pct, (v) => view.setCharMorph({ jiggleChest: v }));
    bindSlider("cfxJigButt", pct, (v) => view.setCharMorph({ jiggleButt: v }));
    bindSlider("cfxJigBelly", pct, (v) => view.setCharMorph({ jiggleBelly: v }));
    bindSlider("cfxJigStiff", (v) => String(Math.round(v)), (v) => view.setCharMorph({ jiggleStiff: v }));
    bindSlider("cfxJigDamp", (v) => v.toFixed(1), (v) => view.setCharMorph({ jiggleDamp: v }));
    if ($("cfxBounceBtn")) $("cfxBounceBtn").onclick = () => {
      if ((view.charMorph.jiggleChest || 0) < 0.05 && (view.charMorph.jiggleButt || 0) < 0.05 && (view.charMorph.jiggleBelly || 0) < 0.05) {
        view.setCharMorph({ jiggleChest: 0.85, jiggleButt: 0.70, jiggleBelly: 0.45 });
        setSliderVal("cfxJigChest", 0.85, pct);
        setSliderVal("cfxJigButt", 0.70, pct);
        setSliderVal("cfxJigBelly", 0.45, pct);
      }
      view.impulseJiggle(1.5);
    };
    if ($("cfxResetMorph")) $("cfxResetMorph").onclick = () => {
      applyCharacterNativeMorphs(item, view);
      if ($("cfxShapeSel")) $("cfxShapeSel").value = ST.activeShapeId;
      syncMorphUI();
      setSliderVal("cfxJigChest", 0, pct);
      setSliderVal("cfxJigButt", 0, pct);
      setSliderVal("cfxJigBelly", 0, pct);
    };
    if ($("cfxZeroMorph")) $("cfxZeroMorph").onclick = () => {
      ST.activeShapeId = "PED_BODYSHAPE_REGULAR";
      if ($("cfxShapeSel")) $("cfxShapeSel").value = ST.activeShapeId;
      view.resetCharMorph();
      syncMorphUI();
      setSliderVal("cfxJigChest", 0, pct);
      setSliderVal("cfxJigButt", 0, pct);
      setSliderVal("cfxJigBelly", 0, pct);
    };
    $("cfxAllMorphs")?.querySelectorAll("[data-mch]").forEach((sl) => {
      sl.oninput = () => {
        const k = sl.dataset.mch, v = Number(sl.value);
        const lbl = $("cfxAllMorphs").querySelector(`[data-mchv="${k}"]`);
        if (lbl) lbl.textContent = v.toFixed(2);
        view.setCharMorph({ [k]: v });
      };
    });

    // 4. Body Damage, Blood, Wounds, Vital Organs & Gore
    bindSlider("cfxBloodSoak", pct, (v) => view.setCharFx({ bloodSoak: v }));
    if ($("cfxWoundKind")) $("cfxWoundKind").onchange = (e) => { ST.woundKind = Number(e.target.value) | 0; };
    bindSlider("cfxWoundSize", (v) => Math.round(v * 100) + " cm", (v) => { ST.woundSize = v; });
    bindSlider("cfxWoundDrip", (v) => Math.round(v * 100) + " cm", (v) => { ST.woundDrip = v; });
    if ($("cfxBloodDecal")) $("cfxBloodDecal").onchange = async (e) => {
      const h = e.target.value;
      ST.activeBloodDecalHash = h;
      if (!h) { view.setBloodDecalTexture(null, false); return; }
      const info = (cat.bloodDecals || []).find((b) => b.hash === h);
      try {
        const img = await ctx.textureImage(h, 512, 0);
        if (ctx.state.item === item) view.setBloodDecalTexture(img, !!info?.tintable);
      } catch (err) { ctx.status(`Blood decal: ${err.message}`); }
    };
    const addWound = (pos, radius, drip) => {
      const list = [...(view.charFx.wounds || [])];
      if (list.length >= 8) list.shift();
      list.push({
        pos,
        radius: radius || ST.woundSize,
        kind: ST.woundKind,
        intensity: 1.0,
        drip: ST.woundKind === 0 ? (drip ?? ST.woundDrip) : 0.0,
        seed: (list.length + 1) * 1.73 + (pos[0] * 13 + pos[1] * 7),
      });
      view.setWounds(list);
      if ($("cfxWoundCnt")) $("cfxWoundCnt").textContent = String(list.length);
    };
    if ($("cfxClickStamp")) $("cfxClickStamp").onchange = (e) => {
      ST.clickStamp = e.target.checked;
      view.setPickMode(ST.clickStamp, (hit) => {
        addWound(hit, ST.woundSize, ST.woundDrip);
        ctx.status(`Stamped wound at (${hit.map((x) => x.toFixed(2)).join(", ")})`);
      });
    };
    if ($("cfxVitalOrgans")) $("cfxVitalOrgans").onchange = (e) => {
      ST.showVitalOrgans = e.target.checked;
      view.setVitalOrgans(cat.vitalOrgans || [], ST.showVitalOrgans);
    };
    $("cfxQuickWounds")?.querySelectorAll("[data-wzone]").forEach((btn) => {
      btn.onclick = () => {
        const z = QUICK_WOUND_ZONES.find((x) => x.id === btn.dataset.wzone);
        if (z) addWound(z.pos, ST.woundSize || z.radius, ST.woundDrip ?? z.drip);
      };
    });
    if ($("cfxClearWounds")) $("cfxClearWounds").onclick = () => {
      view.setWounds([]);
      if ($("cfxWoundCnt")) $("cfxWoundCnt").textContent = "0";
    };
    const syncGoreBones = () => {
      const roots = [];
      for (const v of ST.severedLimbs.values()) roots.push(...(v.hideRootBones || []));
      view.setHiddenBoneRoots(roots);
    };
    $("cfxGoreGrid")?.querySelectorAll("[data-gore]").forEach((chk) => {
      chk.onchange = async () => {
        const limb = chk.dataset.gore;
        const g = gore.find((x) => x.limb === limb);
        if (!g) return;
        if (chk.checked) {
          ST.severedLimbs.set(limb, { partId: g.defaultId, hideRootBones: g.hideRootBones });
          syncGoreBones();
          if (!ctx.state.parts.has(g.defaultId)) await ctx.togglePart(g.defaultId, true);
        } else {
          const prev = ST.severedLimbs.get(limb);
          ST.severedLimbs.delete(limb);
          syncGoreBones();
          if (prev && ctx.state.parts.has(prev.partId)) await ctx.togglePart(prev.partId, false);
        }
      };
    });
    if ($("cfxClearGore")) $("cfxClearGore").onclick = async () => {
      for (const [, v] of [...ST.severedLimbs.entries()]) {
        if (ctx.state.parts.has(v.partId)) await ctx.togglePart(v.partId, false);
      }
      ST.severedLimbs.clear();
      syncGoreBones();
      $("cfxGoreGrid")?.querySelectorAll("[data-gore]").forEach((c) => { c.checked = false; });
    };

    // 5. Tattoos & Facial Overlays (00001ae00000.pso + decorations_0..3_3.ttpf)
    renderNativeOverlaysBox(ctx);
    if (!ST.nativeOverlays) {
      loadCharacterNativeOverlays(item, ctx);
    }

    const loadMakeupPage = async () => {
      const grid = $("cfxMkGrid");
      if (!grid) return;
      grid.innerHTML = `<p class="note">Loading makeup &amp; skin overlay presets…</p>`;
      try {
        const res = await ctx.api("char-overlays", {
          presets: 1,
          kind: ST.makeupKind,
          q: ST.makeupQ,
          offset: ST.makeupOffset,
          limit: 12,
        });
        if (ctx.state.item !== item) return;
        ST.makeupTotal = res.total || 0;
        ST.makeupItems = res.items || [];
        if (Array.isArray(res.kinds) && res.kinds.length) {
          ST.makeupKinds = res.kinds;
          const kSel = $("cfxMkKind");
          if (kSel && kSel.options.length <= 1) {
            kSel.innerHTML = `<option value="">All kinds (${res.kinds.reduce((a, k) => a + (k.count || 0), 0)})</option>` +
              res.kinds.map((k) => `<option value="${esc(k.kind)}" ${k.kind === ST.makeupKind ? "selected" : ""}>${esc(k.label)} (${k.count})</option>`).join("");
          }
        }
        if (!ST.makeupItems.length) {
          grid.innerHTML = `<p class="note">No matching makeup/skin overlays.</p>`;
        } else {
          grid.innerHTML = ST.makeupItems.map((p, idx) => {
            const l0 = p.layers?.[0];
            const tints = l0?.tints || [];
            const mode = l0?.tintMode || "direct";
            const thumb = (tints.length && mode === "mask")
              ? `/api/models/overlay-texture?hash=${encodeURIComponent(p.previewHash)}&mode=mask&max=128&opacity=1&tints=${encodeURIComponent(JSON.stringify(tints))}`
              : `/api/models/texture?hash=${encodeURIComponent(p.previewHash)}&max=128`;
            const sw = p.color ? `<i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${esc(p.color)};margin-right:3px;vertical-align:middle"></i>` : "";
            return `
              <button type="button" class="cfx-tat-card" data-midx="${idx}" title="Apply ${esc(p.label)} (${p.layerCount} layer${p.layerCount === 1 ? "" : "s"}, ${esc(p.target)})">
                <img src="${thumb}" loading="lazy" alt="">
                <span>${sw}${esc(p.label.slice(0, 16))}</span>
              </button>
            `;
          }).join("");
          grid.querySelectorAll("[data-midx]").forEach((btn) => {
            btn.onclick = async () => {
              const pre = ST.makeupItems[Number(btn.dataset.midx)];
              if (!pre || !Array.isArray(pre.layers)) return;
              const touched = new Set();
              for (const l of pre.layers) {
                ST.appliedFaceOverlays.push({ ...l, _native: false });
                touched.add(l.target || "head");
              }
              if ($("cfxMkAppliedCnt")) $("cfxMkAppliedCnt").textContent = String(ST.appliedFaceOverlays.length);
              ctx.status(`Baking ${pre.label} (${pre.layers.length} layer${pre.layers.length === 1 ? "" : "s"})…`);
              for (const tk of touched) await rebakeTattoos(ctx, tk);
              ctx.status(`Applied ${pre.label} (${ST.appliedFaceOverlays.length} active)`);
            };
          });
        }
        if ($("cfxMkPage")) $("cfxMkPage").textContent = ST.makeupTotal ? `${ST.makeupOffset + 1}–${Math.min(ST.makeupTotal, ST.makeupOffset + ST.makeupItems.length)} / ${ST.makeupTotal}` : "0";
        if ($("cfxMkPrev")) $("cfxMkPrev").disabled = ST.makeupOffset <= 0;
        if ($("cfxMkNext")) $("cfxMkNext").disabled = ST.makeupOffset + 12 >= ST.makeupTotal;
      } catch (e) {
        grid.innerHTML = `<p class="note warn">Makeup catalog error: ${esc(e.message)}</p>`;
      }
    };
    const mkDetails = $("cfxMkDetails");
    if (mkDetails) {
      let mkLoaded = false;
      mkDetails.addEventListener("toggle", () => {
        if (mkDetails.open && !mkLoaded) { mkLoaded = true; loadMakeupPage(); }
      });
    }
    let mkTimer = null;
    if ($("cfxMkKind")) $("cfxMkKind").onchange = (e) => {
      ST.makeupKind = e.target.value;
      ST.makeupOffset = 0;
      loadMakeupPage();
    };
    if ($("cfxMkSearch")) $("cfxMkSearch").oninput = (e) => {
      ST.makeupQ = e.target.value.trim();
      ST.makeupOffset = 0;
      clearTimeout(mkTimer);
      mkTimer = setTimeout(loadMakeupPage, 240);
    };
    if ($("cfxMkPrev")) $("cfxMkPrev").onclick = () => { ST.makeupOffset = Math.max(0, ST.makeupOffset - 12); loadMakeupPage(); };
    if ($("cfxMkNext")) $("cfxMkNext").onclick = () => { ST.makeupOffset += 12; loadMakeupPage(); };
    if ($("cfxMkClear")) $("cfxMkClear").onclick = async () => {
      const targets = new Set(ST.appliedFaceOverlays.map((x) => x.target || "head"));
      ST.appliedFaceOverlays = [];
      ST.autoNativeMakeup = false;
      if ($("cfxNativeMkChk")) $("cfxNativeMkChk").checked = false;
      if ($("cfxMkAppliedCnt")) $("cfxMkAppliedCnt").textContent = "0";
      for (const tk of targets) await rebakeTattoos(ctx, tk, { silent: true });
      ctx.status("Cleared all facial & skin overlays.");
    };

    if ($("cfxTatTarget")) $("cfxTatTarget").onchange = (e) => { ST.tattooTarget = e.target.value; };
    bindSlider("cfxTatOp", pct, (v) => { ST.tattooOpacity = v; });
    bindSlider("cfxTatFade", pct, (v) => { ST.tattooFade = v; });
    bindSlider("cfxTatScale", (v) => v.toFixed(2) + "×", (v) => { ST.tattooScaleMult = v; });
    bindSlider("cfxTatU", (v) => (v >= 0 ? "+" : "") + v.toFixed(2), (v) => { ST.tattooOffU = v; });
    bindSlider("cfxTatV", (v) => (v >= 0 ? "+" : "") + v.toFixed(2), (v) => { ST.tattooOffV = v; });
    bindSlider("cfxTatRot", (v) => Math.round(v) + "°", (v) => { ST.tattooRotDeg = v; });

    const loadTattooPage = async () => {
      const grid = $("cfxTatGrid");
      if (!grid) return;
      grid.innerHTML = `<p class="note">Loading decorations_0..3_3.ttpf tattoos…</p>`;
      try {
        const res = await ctx.api("char-tattoos", { q: ST.tattooQ, offset: ST.tattooOffset, limit: 12 });
        if (ctx.state.item !== item) return;
        ST.tattooTotal = res.total || 0;
        ST.tattooItems = res.items || [];
        if (!ST.tattooItems.length) {
          grid.innerHTML = `<p class="note">No matching tattoos.</p>`;
        } else {
          grid.innerHTML = ST.tattooItems.map((t, idx) => `
            <button type="button" class="cfx-tat-card" data-tidx="${idx}" title="Apply ${esc(t.id)} (${t.width}×${t.height}) at UV (${t.u}, ${t.v})">
              <img src="/api/models/texture?hash=${esc(t.albedo)}&max=128" loading="lazy" alt="">
              <span>${esc(t.id.slice(0, 10))}</span>
            </button>
          `).join("");
          grid.querySelectorAll("[data-tidx]").forEach((btn) => {
            btn.onclick = async () => {
              const tat = ST.tattooItems[Number(btn.dataset.tidx)];
              if (!tat) return;
              ST.appliedTattoos.push({
                target: ST.tattooTarget,
                tattoo: tat,
                opacity: ST.tattooOpacity,
                fade: ST.tattooFade,
                scaleMult: ST.tattooScaleMult,
                offU: ST.tattooOffU,
                offV: ST.tattooOffV,
                rotDeg: ST.tattooRotDeg,
              });
              if ($("cfxTatAppliedCnt")) $("cfxTatAppliedCnt").textContent = String(ST.appliedTattoos.length);
              ctx.status(`Baking tattoo ${tat.id} onto ${ST.tattooTarget} skin…`);
              await rebakeTattoos(ctx, ST.tattooTarget);
              ctx.status(`Applied tattoo ${tat.id} (${ST.appliedTattoos.length} active)`);
            };
          });
        }
        if ($("cfxTatPage")) $("cfxTatPage").textContent = ST.tattooTotal ? `${ST.tattooOffset + 1}–${Math.min(ST.tattooTotal, ST.tattooOffset + ST.tattooItems.length)} / ${ST.tattooTotal}` : "0";
        if ($("cfxTatPrev")) $("cfxTatPrev").disabled = ST.tattooOffset <= 0;
        if ($("cfxTatNext")) $("cfxTatNext").disabled = ST.tattooOffset + 12 >= ST.tattooTotal;
      } catch (e) {
        grid.innerHTML = `<p class="note warn">Tattoo catalog error: ${esc(e.message)}</p>`;
      }
    };
    const tatPanel = $("cfxTatDetails") || $("cfxTatGrid")?.closest("details");
    if (tatPanel) {
      let tatLoaded = false;
      tatPanel.addEventListener("toggle", () => {
        if (tatPanel.open && !tatLoaded) { tatLoaded = true; loadTattooPage(); }
      });
    }
    let tatTimer = null;
    if ($("cfxTatSearch")) $("cfxTatSearch").oninput = (e) => {
      ST.tattooQ = e.target.value.trim();
      ST.tattooOffset = 0;
      clearTimeout(tatTimer);
      tatTimer = setTimeout(loadTattooPage, 240);
    };
    if ($("cfxTatPrev")) $("cfxTatPrev").onclick = () => { ST.tattooOffset = Math.max(0, ST.tattooOffset - 12); loadTattooPage(); };
    if ($("cfxTatNext")) $("cfxTatNext").onclick = () => { ST.tattooOffset += 12; loadTattooPage(); };
    if ($("cfxTatClear")) $("cfxTatClear").onclick = async () => {
      const targets = new Set(ST.appliedTattoos.map((x) => x.target || "upper"));
      ST.appliedTattoos = [];
      ST.autoNativeTattoos = false;
      if ($("cfxNativeTatChk")) $("cfxNativeTatChk").checked = false;
      if ($("cfxTatAppliedCnt")) $("cfxTatAppliedCnt").textContent = "0";
      for (const tk of targets) await rebakeTattoos(ctx, tk, { silent: true });
      ctx.status("Cleared all tattoos.");
    };

    // 6. Quick Animations
    $("cfxQuickAnims")?.querySelectorAll("[data-qanim]").forEach((btn) => {
      btn.onclick = () => playQuickAnimByKey(btn.dataset.qanim);
    });

    // Global Reset
    if ($("cfxResetAll")) $("cfxResetAll").onclick = async () => {
      const hadGore = [...ST.severedLimbs.values()];
      const hadTargets = new Set([
        ...ST.appliedTattoos.map((x) => x.target || "upper"),
        ...ST.appliedFaceOverlays.map((x) => x.target || "head"),
      ]);
      ST.autoNativeTattoos = true;
      ST.autoNativeMakeup = true;
      resetForModel(item, view);
      for (const g of hadGore) { if (ctx.state.parts.has(g.partId)) await ctx.togglePart(g.partId, false); }
      for (const tk of hadTargets) await rebakeTattoos(ctx, tk, { silent: true });
      render(item, ctx);
    };
  }

  window.CharEffectsStudio = { render, resetForModel, isHumanCharacter, onPartsReady };
})();
