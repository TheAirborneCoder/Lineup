// Grooms page: browse and view the smh strand-hair groom assets decoded by smh_groom.py.
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtN = (n) => Number(n || 0).toLocaleString("en-US");
  const vec = (v) => v.map((x) => Number(x).toFixed(3)).join(", ");
  const kv = (rows) => `<dl class="kv">${rows.filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
  const PAGE = 80;
  const COLORS = [[0.55, 0.56, 0.58], [0.86, 0.24, 0.18], [0.36, 0.24, 0.15], [0.78, 0.62, 0.36]];
  // visible: scalp and guides (guides start hidden). Both strand classes start on: the game draws them together
  // as one groom. pick holds what the user ticked for the groom on screen (index 2 = class 0, 3 = class 1;
  // null = on) and is forgotten when another groom is opened.
  // whole: show every groom of the item's group (default) or this groom only. extra: neighbouring grooms the user
  // added with + previous / + next, kept until another item is opened.
  const S = { cls: "", page: 0, count: 0, item: null, lod: 0, view: null, visible: [true, false], pick: { 2: null, 3: null },
    totals: {}, token: 0, whole: true, extra: [], class1: "", style: true };
  const TINT = [[0.55, 0.56, 0.58], [0.55, 0.3, 0.3], [0.33, 0.37, 0.45], [0.55, 0.6, 0.7]];   // the item's other grooms

  async function api(path) {
    const r = await fetch("/api/grooms/" + path, { cache: "no-store" });   // the index changes when names are relinked
    const body = await r.json();
    if (!r.ok) throw Error(body.error || r.statusText);
    return body;
  }
  const status = (text) => { $("status").textContent = text; };

  function query() {
    const p = new URLSearchParams({ sort: $("sort").value, limit: PAGE, offset: S.page * PAGE });
    if (S.cls === "@named") p.set("named", "1");
    else if (S.cls === "@likely") p.set("likely", "1");
    else if (S.cls) p.set("class", S.cls);
    if ($("archive").value) p.set("archive", $("archive").value);
    if ($("q").value.trim()) p.set("q", $("q").value.trim());
    return p;
  }

  async function loadList() {
    let data;
    try { data = await api("list?" + query()); } catch (e) { status(e.message); return; }
    S.count = data.count;
    $("count").textContent = fmtN(data.count);
    const list = $("list");
    list.replaceChildren();
    for (const r of data.rows) {
      const b = document.createElement("button");
      b.className = "model-row";
      b.dataset.id = r.id;
      if (S.item && S.item.id === r.id) b.setAttribute("aria-current", "true");
      const strands = r.strands0 + r.strands1;
      const guess = !r.name && r.likely_p >= 0.9 ? `<small>likely ${esc(r.likely)} (${likelyShare(r.likely_p)})</small>` : "";
      b.innerHTML = `<strong>${esc(r.name || "Groom " + r.offset)}</strong>${guess}<small>${esc(r.class)} · ${fmtN(r.guides)} guides × ${r.nodes_per_guide} · ${fmtN(strands)} strands · ${r.lods} LOD${r.lods > 1 ? "s" : ""}</small>`;
      b.addEventListener("click", () => select(r.id));
      list.append(b);
    }
    const pages = Math.max(1, Math.ceil(data.count / PAGE));
    $("pageLabel").textContent = `${S.page + 1} / ${pages}`;
    $("prev").disabled = S.page === 0;
    $("next").disabled = S.page + 1 >= pages;
    status(data.count ? `${fmtN(data.count)} grooms.` : "No groom matches.");
  }

  function buildTree(summary) {
    const tree = $("tree");
    tree.replaceChildren();
    const add = (name, label, n) => {
      const b = document.createElement("button");
      b.innerHTML = `${esc(label)}<span class="n">${fmtN(n)}</span>`;
      if (S.cls === name) b.setAttribute("aria-current", "true");
      b.addEventListener("click", () => { S.cls = name; S.page = 0; buildTree(summary); loadList(); });
      tree.append(b);
    };
    add("", "All grooms", summary.assets);
    add("@named", "Named items", summary.named);
    add("@likely", "Likely items (90%+)", summary.likely || 0);
    for (const c of summary.classes) add(c.name, c.name, c.assets);
  }

  const likelyShare = (p) => (p >= 0.995 ? "over 99%" : Math.round(p * 100) + "%");

  function applyParts() {
    if (!S.view) return;
    const mine = "g" + (S.item && S.item.id);
    const tint = (S.layers || []).length > 1 && $("tint") && $("tint").checked;
    for (const layer of S.layers || ["main"])
      for (let i = 0; i < 4; i++)
        S.view.setMaterial(i, { visible: partOn(i, S.totals[layer]), color: tint && layer !== mine ? TINT[i] : COLORS[i] }, layer);
  }
  // Whether part i is drawn for a groom with these strand totals ([class 0, class 1]).
  function partOn(i, totals) {
    if (i < 2) return S.visible[i];
    return S.pick[i] !== null ? S.pick[i] : true;
  }

  // The grooms on screen: the item's whole group (plus neighbours the user added), or this groom only.
  function onScreen() {
    const item = S.item;
    if (!S.whole || item.group.length < 2 && !S.extra.length) return [{ id: item.id, offset: item.offset, lods: item.lods.length }];
    return [...S.extra.filter((e) => e.order < item.group[0].order), ...item.group,
      ...S.extra.filter((e) => e.order > item.group[item.group.length - 1].order)];
  }

  // One viewer layer per groom. Each groom is drawn at the chosen LOD, or at its last LOD if it has fewer.
  async function show() {
    const item = S.item, token = ++S.token, limit = $("strands").value, list = onScreen();
    $("empty").hidden = true;
    $("load").hidden = false;
    $("load").className = "";
    try {
      const bufs = [];
      for (const [k, g] of list.entries()) {
        $("load").textContent = list.length > 1 ? `Generating strands, groom ${k + 1} of ${list.length}…` : "Generating strands…";
        const lod = Math.min(S.lod, g.lods - 1);
        const r = await fetch(`/api/grooms/glb?id=${g.id}&lod=${lod}&strands=${limit}${S.class1 ? "&class1=" + S.class1 : ""}${S.style ? "" : "&style=0"}`, { cache: "no-store" });   // the server keeps its own GLB cache
        if (!r.ok) throw Error((await r.json()).error || r.statusText);
        bufs.push(await r.arrayBuffer());
        if (token !== S.token) return;
      }
      S.view.clear();
      S.view.resetMaterials();
      S.layers = [];
      S.totals = {};
      let tris = 0;
      const shown = {}, total = {}, shaders = {};
      let styled = false;
      for (const [k, g] of list.entries()) {
        const layer = "g" + g.id;
        const info = S.view.load(bufs[k], layer, true, k > 0);
        tris += info.triangles;
        S.totals[layer] = info.extras.strandTotals;
        S.layers.push(layer);
        for (const [c, n] of Object.entries(info.extras.strandTotals || {})) total[c] = (total[c] || 0) + n;
        for (const [c, n] of Object.entries(info.extras.strandsShown || {})) shown[c] = (shown[c] || 0) + n;
        for (const [c, v] of Object.entries(info.extras.shaders || {})) shaders[c + ":" + k] = v;
        if (info.extras.style) styled = true;
      }
      applyParts();
      S.view.fit();
      $("load").hidden = true;
      $("stats").textContent = `${fmtN(tris)} display triangles`;
      const counts = Object.keys(total).map((c) => `Class ${c}: ${fmtN(shown[c] || 0)} of ${fmtN(total[c])} strands drawn`).join(" · ");
      const ran = [...new Set(Object.values(shaders).filter(Boolean))].sort((a, b) => a - b).map((v) => "smh_gen_" + v).join(", ");
      const what = list.length < 2 ? "" : item.groupKind === "named" ? `All ${list.length} grooms of ${item.name} · `
        : `${list.length} grooms, ${item.groupKind === "likely" ? `likely ${item.likely} (an estimate)` : "this groom and its neighbours"} · `;
      $("legend").textContent = what + counts + (ran ? ` · shaders run: ${ran}` : "") + (styled ? " · curl from the style profile (amount taken as full)" : "");
      const url = `/api/grooms/glb?id=${item.id}&lod=${S.lod}&strands=${limit}${S.class1 ? "&class1=" + S.class1 : ""}${S.style ? "" : "&style=0"}`;
      $("downloadGlb").href = url;
      $("downloadGlb").download = `groom_${item.offset}_lod${S.lod}.glb`;
      $("downloadCurves").href = url + "&curves=1";
      $("downloadCurves").download = `groom_${item.offset}_lod${S.lod}_curves.glb`;
      $("downloadGlb").hidden = $("downloadCurves").hidden = false;
    } catch (e) {
      if (token !== S.token) return;
      $("load").className = "error";
      $("load").textContent = e.message;
    }
  }

  // The item's grooms, the edges of the group, and the controls for what is on screen.
  function groupSection(item) {
    const named = item.groupKind === "named", likely = item.groupKind === "likely";
    const title = named ? "Grooms of this item" : likely ? "Grooms of the likely item" : "Neighbouring grooms";
    const note = named ? "Proven by archive order. They open together."
      : likely ? `An estimate: the grooms the best alignment gives to ${esc(item.likely)}. They open together, with the item's other grooms tinted. Each row shows the groom's own probability for this item and, where it is uncertain, the chance that another item starts there. A part may sit just outside the group: add the neighbouring groom to see it.`
      : "This groom is not in a scored group (story characters, beards, body hair, animals and wigs are not scored). Add its neighbours to look for the item's other parts.";
    return `<section><h3>${title}</h3><p class="note">${note}</p><div id="siblings"></div>
      <div class="row-actions"><button id="addBefore" ${item.neighbours.before ? "" : "disabled"}>+ previous groom</button><button id="addAfter" ${item.neighbours.after ? "" : "disabled"}>+ next groom</button></div>
      <label class="check"><input type="checkbox" id="whole" ${S.whole ? "checked" : ""}/> Show the whole item</label>
      <label class="check"><input type="checkbox" id="tint" checked/> Tint the other grooms</label></section>`;
  }

  function groupRows(item) {
    const box = $("siblings");
    const rows = [...S.extra.filter((e) => e.order < item.group[0].order), ...item.group,
      ...S.extra.filter((e) => e.order > item.group[item.group.length - 1].order)];
    const inGroup = new Set(item.group.map((g) => g.id));
    for (const g of rows) {
      const b = document.createElement("button");
      b.className = "model-row";
      if (g.id === item.id) b.setAttribute("aria-current", "true");
      const rated = item.groupKind === "likely" && inGroup.has(g.id) && g.p != null ? ` · ${likelyShare(g.p)}` : "";
      const edge = item.groupKind === "likely" && g.startP != null && g.startP >= 0.1 && g.startP < 0.9 ? ` · new item starts here: ${Math.round(g.startP * 100)}%` : "";
      const added = inGroup.has(g.id) ? "" : " · added";
      b.innerHTML = `<strong>Groom ${esc(g.offset)}</strong><small>${fmtN(g.guides)} guides × ${g.nodesPerGuide} · ${fmtN(g.strands)} strands${rated}${edge}${added}</small>`;
      b.addEventListener("click", () => select(g.id));
      box.append(b);
    }
    const add = async (side) => {
      const ends = onScreen(), edge = side < 0 ? ends[0] : ends[ends.length - 1];
      let next;
      try { next = await api(`item?id=${edge.id}&lod=0`); } catch (e) { status(e.message); return; }
      const g = side < 0 ? next.adjacent.before : next.adjacent.after;      // the groom right next to it in the archive
      if (!g || ends.some((e) => e.id === g.id)) { status("No further groom on that side."); return; }
      S.extra.push(g);
      S.whole = true;
      inspect();
      show();
    };
    $("addBefore").addEventListener("click", () => add(-1));
    $("addAfter").addEventListener("click", () => add(1));
    $("whole").addEventListener("change", (e) => { S.whole = e.target.checked; show(); });
    $("tint").addEventListener("change", applyParts);
  }

  function inspect() {
    const item = S.item, g = item.groom;
    const size = g.box[1].map((x, i) => x - g.box[0][i]);
    const html = [];
    html.push(`<section><h3>Groom</h3>${kv([
      ["Placement class", `${esc(item.class)} <small>(from the stored box)</small>`],
      ["Item name", item.name ? `<b>${esc(item.name)}</b>` : `<span class="warn">not resolved</span>`],
      item.name ? ["Name evidence", esc(item.nameBasis || "")] : null,
      !item.name && item.likely ? ["Most likely item", `${esc(item.likely)} <small>(${likelyShare(item.likelyP)}, an estimate from the length code, style word and fade code in the item names; not a name)</small>`] : null,
      !item.name && item.hint ? ["Candidates", esc(item.hint.replace(/^most likely [^;]*; /, ""))] : null,
      ["Archive", esc(item.archive)], ["LOD 0 offset", esc(item.offset)], ["Position in archive", `#${fmtN(item.order + 1)}`],
    ])}${item.name ? "" : `<p class="note">Groom resources carry no name and the archive's file tables are encrypted. A groom is named only where archive order proves the link (one groom per item, checked by count, rig and body region).</p>`}</section>`);
    html.push(groupSection(item));
    html.push(`<section><h3>LOD ${item.lod}</h3>${kv([
      ["Guides", `${fmtN(g.guides)} × ${g.nodesPerGuide} nodes`], ["Scalp triangles", fmtN(g.triangles)],
      ["Strands, class 0", fmtN(g.strandTotals[0])], ["Strands, class 1", fmtN(g.strandTotals[1])],
      ["Guide length", `${(g.guideLengthM[0] * 100).toFixed(1)} – ${(g.guideLengthM[2] * 100).toFixed(1)} cm <small>(mean ${(g.guideLengthM[1] * 100).toFixed(1)})</small>`],
      ["Stored length check", `${(g.storedLengthMaxErrorM * 1000).toFixed(2)} mm worst difference`],
      ["Length variation", `${(g.lengthRandom[0] * 100).toFixed(0)}% of strands' length, down to ×${g.lengthRandom[1].toFixed(2)}`],
      ["Skin bones", esc(item.skin.bones.join(", "))], ["Flags", esc(g.flags)], ["Buffer", `${fmtN(g.bufferBytes)} bytes`],
      g.optionalArrays.length ? ["Undecoded arrays", `<span class="warn">${esc(g.optionalArrays.join(", "))}${g.tailBytes ? `, tail ${fmtN(g.tailBytes)} bytes` : ""}</span>`] : null,
    ])}</section>`);
    html.push(`<section><h3>Parts</h3><div id="parts"></div><label class="check" title="Which game shader generates the class 1 strands. The asset selects one with system page byte +0x12d; the mapping from that byte to a shader is inferred from which counts each shader samples, not proven.">Class 1 shader <select id="class1"><option value="">${item.class1Shader ? "As the asset selects: smh_gen_" + item.class1Shader : "None (no class 1)"}</option><option value="4">smh_gen_4 (triangle density)</option><option value="16">smh_gen_16 (guide density)</option><option value="2">smh_gen_2 (triangle slots)</option></select></label><label class="check" title="Apply the asset's curl and wave profile (the game's InterpolateStrandStyle pass) using the frame and flip the strand shader wrote. The run-time amount is taken as full."><input type="checkbox" id="style" ${S.style ? "checked" : ""}/> Curl from the asset's style profile</label><p class="note">Strands come from running the game's own strand shaders (smh_gen_1 for class 0, smh_gen_4, _16 or _2 for class 1) on this asset, instruction by instruction, in the rest pose. What the game supplies per character at run time is assumed: the density factor, an empty region mask, rest-pose skinning and a length texture of 1. Curl is the game's style pass with the asset's own profile and the shader's frame and flip; its run-time amount is taken as full, so curl is at the most the profile allows. Tube thickness is a display size.</p></section>`);
    html.push(`<section><h3>Bounds (m, Z up)</h3>${kv([["Min", vec(g.box[0])], ["Max", vec(g.box[1])], ["Size", vec(size)]])}</section>`);
    html.push(`<section><h3>LOD chain</h3>${kv(item.lods.map((l) => [`LOD ${l.lod}`, `${fmtN(l.guides)} guides · ${fmtN(l.triangles)} triangles · ${fmtN(l.strands[0] + l.strands[1])} strands · ${esc(l.offset)}`]))}<p class="note">A groom LOD lowers the guides and scalp triangles only. Every LOD stores the same strand totals and the same points per strand, so the hair looks almost the same at each LOD: turn on Guides to see the difference. The guides are what the game simulates and skins, so the asset LODs save simulation work. Fewer strands at a distance would be set at run time in each instance's record, by a rule that is not in the asset.</p></section>`);
    if (g.regions.length) html.push(`<section><h3>Regions</h3>${kv(g.regions.map((r) => [`Region ${r.id}`, `${fmtN(r.guides)} guides · ${fmtN(r.triangles)} triangles · ${fmtN(r.strands[0])} + ${fmtN(r.strands[1])} strands`]))}</section>`);
    $("inspect").innerHTML = html.join("");
    groupRows(item);
    const c1 = $("class1");
    c1.value = S.class1 || "";
    c1.addEventListener("change", () => { S.class1 = c1.value; show(); });
    $("style").addEventListener("change", (e) => { S.style = e.target.checked; show(); });
    const parts = $("parts");
    item.parts.forEach((p) => {
      const label = document.createElement("label");
      label.className = "check";
      const on = partOn(p.index, item.groom.strandTotals);
      label.innerHTML = `<input type="checkbox" ${on ? "checked" : ""} /> <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:rgb(${COLORS[p.index].map((c) => Math.round(c * 255)).join(",")})"></span> ${esc(p.name)}`;
      label.querySelector("input").addEventListener("change", (e) => {
        if (p.index < 2) S.visible[p.index] = e.target.checked; else S.pick[p.index] = e.target.checked;
        applyParts();
      });
      parts.append(label);
    });
  }

  function lodButtons() {
    const seg = $("lods");
    seg.replaceChildren();
    seg.hidden = S.item.lods.length < 2;
    for (const l of S.item.lods) {
      const b = document.createElement("button");
      b.textContent = "LOD " + l.lod;
      b.setAttribute("aria-pressed", String(l.lod === S.lod));
      b.addEventListener("click", () => select(S.item.id, l.lod));
      seg.append(b);
    }
  }

  async function select(id, lod = 0) {
    const before = S.item;
    try { S.item = await api(`item?id=${id}&lod=${lod}`); } catch (e) { status(e.message); return; }
    if (!before || S.item.id !== before.id) S.pick = { 2: null, 3: null };      // another groom: both strand classes on again
    // Another item: drop the added neighbours and show the whole item again. Moving within the same group keeps them.
    const sameGroup = before && before.group.some((g) => g.id === S.item.id) || S.extra.some((g) => g.id === S.item.id);
    if (!sameGroup) { S.extra = []; S.whole = true; }
    S.lod = lod;
    for (const b of $("list").children) b.toggleAttribute("aria-current", Number(b.dataset.id) === id);
    for (const b of $("list").querySelectorAll("[aria-current]")) b.setAttribute("aria-current", "true");
    $("title").textContent = S.item.name || "Groom " + S.item.offset;
    $("title").title = S.item.name ? "Groom " + S.item.offset : "";
    $("crumb").textContent = "Grooms › " + S.item.class;
    $("subtitle").textContent = `${S.item.archive} · ${S.item.lods.length} LOD${S.item.lods.length > 1 ? "s" : ""}`;
    history.replaceState(null, "", `#id=${id}&lod=${lod}`);
    lodButtons();
    inspect();
    show();
  }

  async function start() {
    let summary;
    try { summary = await api("summary"); } catch (e) { status(e.message); return; }
    for (const a of summary.archives) $("archive").add(new Option(`${a.name} (${fmtN(a.assets)})`, a.name));
    for (const n of summary.strandLimits) $("strands").add(new Option(n ? fmtN(n) : "All", n, n === 6000, n === 6000));
    buildTree(summary);
    S.view = new ModelView($("view"));
    // The viewer leaves its depth-compare shadow texture on unit 0. A groom never binds a color texture,
    // so give every 2D color unit a plain texture; otherwise each draw fails with a sampler type mismatch.
    const gl = S.view.gl, white = gl.createTexture();
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, white);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([255, 255, 255, 255]));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    for (let unit = 1; unit < 12; unit++) { gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, white); }
    gl.activeTexture(gl.TEXTURE0);
    S.view.set("mode", "material");
    S.view.set("backfaces", true);
    for (const b of $("modes").children) b.addEventListener("click", () => {
      for (const o of $("modes").children) o.setAttribute("aria-pressed", String(o === b));
      S.view.set("mode", b.dataset.mode);
    });
    for (const id of ["grid", "box", "spin"]) {
      S.view.set(id, $(id).checked);
      $(id).addEventListener("change", (e) => S.view.set(id, e.target.checked));
    }
    for (const b of $("views").querySelectorAll("[data-view]")) b.addEventListener("click", () => S.view.setView(b.dataset.view));
    $("fit").addEventListener("click", () => S.view.fit());
    $("strands").addEventListener("change", () => { if (S.item) show(); });
    let timer;
    $("q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { S.page = 0; loadList(); }, 250); });
    for (const id of ["archive", "sort"]) $(id).addEventListener("change", () => { S.page = 0; loadList(); });
    $("prev").addEventListener("click", () => { S.page = Math.max(0, S.page - 1); loadList(); });
    $("next").addEventListener("click", () => { S.page += 1; loadList(); });
    await loadList();
    const fromHash = () => {
      const m = /id=(\d+)(?:&lod=(\d+))?/.exec(location.hash);
      if (m && !(S.item && S.item.id === Number(m[1]) && S.lod === Number(m[2] || 0))) select(Number(m[1]), Number(m[2] || 0));
    };
    window.addEventListener("hashchange", fromHash);
    fromHash();
  }
  window.addEventListener("DOMContentLoaded", start);
})();
