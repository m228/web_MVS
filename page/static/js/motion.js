/* Страница «Движение»: камеры, зоны, группы и вывод в ПЛК сервиса движения (motion_service). Всё через /api/motion/*.
   Настройки правятся локально (S.cfg) и уходят в сервис одним PUT по кнопке «Сохранить»; живые цифры идут отдельным опросом. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const THR = { flow: [0.9, 0.6], diff: [4.0, 2.0] };

  const S = { cfg: null, dirty: false, cam: null, state: {}, mark: null, fw: 0, fh: 0, saved: [], frameBusy: false, editGrp: null, online: false };

  async function api(path, opt) {
    try {
      const r = await fetch(path, opt);
      const ct = r.headers.get("content-type") || "";
      if (!ct.includes("json")) return r.ok ? {} : { error: "HTTP " + r.status };
      return await r.json();
    } catch (e) {
      return { error: String(e) };
    }
  }
  const putJson = (path, body, method) => api(path, { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  const cams = () => (S.cfg ? S.cfg.cameras : []);
  const curCam = () => cams().find((c) => c.id === S.cam) || null;
  const curState = () => (S.state[S.cam] || {}).result || null;

  function dirty(on) {
    S.dirty = on !== false;
    $("saveInfo").textContent = S.dirty ? "есть несохранённые изменения" : "";
    $("saveInfo").className = "mo-note" + (S.dirty ? " mo-warn" : "");
  }

  // ───────────── загрузка ─────────────
  async function init() {
    const h = await api("/api/motion/health");
    $("svcUrl").textContent = h.service_url || "";
    S.online = !!h.online;
    paintBadge(h);
    $("offline").classList.toggle("hidden", S.online);
    $("app").classList.toggle("hidden", !S.online);
    if (!S.online) { setTimeout(init, 5000); return; }
    const cfg = await api("/api/motion/config");
    if (cfg.error) { $("svcBadge").textContent = cfg.error; return; }
    S.cfg = cfg;
    const saved = await api("/api/rtsp/saved");
    S.saved = saved.items || [];
    S.cam = cams().length ? cams()[0].id : null;
    renderAll();
    loopState();
    loopFrame();
    loopCharts();
  }

  function paintBadge(h) {
    const b = $("svcBadge");
    if (!h || !h.online) { b.textContent = "не запущен"; b.className = "mo-bad"; return; }
    const s = h.service || {};
    b.textContent = "работает · v" + (s.version || "?") + " · камер " + (s.alive || 0) + "/" + (s.cameras || 0) + " живы";
    b.className = (s.cameras && s.alive < s.cameras) ? "mo-warn" : "mo-ok";
  }

  function renderAll() {
    const sel = $("camSel");
    sel.innerHTML = cams().map((c) => '<option value="' + esc(c.id) + '">' + esc(c.name) + "</option>").join("") || '<option value="">— камер нет —</option>';
    if (S.cam) sel.value = S.cam;
    renderZones(); renderGroups(); renderCamForm(); renderPlc();
    $("btnMark").disabled = !S.cam;
    $("btnDelCam").disabled = !S.cam;
    $("btnRec").disabled = !S.cam;
    $("hint").textContent = S.cam ? "" : "Добавь камеру (кнопка «+ Камера»): нужен адрес потока RTSP.";
    rebuildCharts();
    S.frameKey = null;
  }

  // ───────────── камеры ─────────────
  $("camSel").addEventListener("change", (e) => { S.cam = e.target.value; cancelMark(); renderAll(); refreshFrame(true); });

  $("btnAddCam").addEventListener("click", () => {
    const opts = S.saved.map((s) => '<option value="' + esc(s.url) + '">' + esc(s.label || s.ip || s.url) + "</option>").join("");
    const box = $("newZone");
    box.classList.remove("hidden");
    box.dataset.mode = "cam";
    box.innerHTML = '<div class="mo-form"><label>Название</label><input type="text" id="ncName" placeholder="Ленты, шахта…" />' +
      (opts ? '<label>Из сохранённых RTSP</label><select id="ncSaved"><option value="">— ввести вручную —</option>' + opts + "</select>" : "") +
      '<label>Адрес потока</label><input type="text" id="ncUrl" placeholder="rtsp://user:pass@192.168.1.10:554/…" /></div>' +
      '<p class="mo-note">Можно указать и путь к видеофайлу — для проверки без камеры (крутится по кругу).</p>' +
      '<div style="margin-top:8px;display:flex;gap:8px"><button type="button" class="btn btn-xs" id="ncOk">Добавить камеру</button>' +
      '<button type="button" class="btn btn-xs btn-secondary" id="ncCancel">Отмена</button></div>';
    const savedSel = $("ncSaved");
    if (savedSel) savedSel.addEventListener("change", () => { if (savedSel.value) { $("ncUrl").value = savedSel.value; const f = S.saved.find((s) => s.url === savedSel.value); if (f && f.label && !$("ncName").value) $("ncName").value = f.label; } });
    $("ncCancel").onclick = () => box.classList.add("hidden");
    $("ncOk").onclick = () => {
      const url = $("ncUrl").value.trim();
      if (!url) { alert("Нужен адрес потока"); return; }
      let n = 1; while (cams().some((c) => c.id === "cam" + n)) n++;
      const cam = { id: "cam" + n, name: $("ncName").value.trim() || "Камера " + n, enabled: true, url: url, transport: "tcp", proc_fps: 3, frame_gap: 1,
        freeze_timeout_s: 10, freeze_eps: 1.0, loop: true, frame_size: null, status_out: null, zones: [], groups: [] };
      S.cfg.cameras.push(cam);
      S.cam = cam.id;
      box.classList.add("hidden");
      dirty(); renderAll();
      $("hint").textContent = "Камера добавлена. Нажми «Сохранить настройки» — сервис подключится и покажет кадр.";
    };
  });

  $("btnDelCam").addEventListener("click", () => {
    const c = curCam();
    if (!c || !confirm("Удалить камеру «" + c.name + "» вместе с зонами и группами? (Применится после «Сохранить»)")) return;
    S.cfg.cameras = S.cfg.cameras.filter((x) => x.id !== c.id);
    S.cam = cams().length ? cams()[0].id : null;
    dirty(); renderAll();
  });

  // ───────────── кадр и оверлей ─────────────
  function loopFrame() { refreshFrame(); setTimeout(loopFrame, 1000); }

  function refreshFrame(force) {
    if (!S.online || !S.cam || S.frameBusy) return;
    if (!force && (!$("chkLive").checked || S.mark)) return;
    S.frameBusy = true;
    const cam = S.cam;
    const im = new Image();
    im.onload = async () => {
      S.frameBusy = false;
      if (cam !== S.cam) return;
      $("frame").src = im.src;
      drawOverlay();
    };
    im.onerror = () => { S.frameBusy = false; };
    // размер оригинала приходит заголовком — берём его отдельным HEAD-подобным запросом только пока не знаем
    im.src = "/api/motion/snapshot?camera=" + encodeURIComponent(cam) + "&w=1100&t=" + Date.now();
    if (!S.fw || S.fwCam !== cam) {
      fetch("/api/motion/snapshot?camera=" + encodeURIComponent(cam) + "&w=64").then((r) => {
        S.fw = +r.headers.get("X-Frame-W") || 0; S.fh = +r.headers.get("X-Frame-H") || 0; S.fwCam = cam;
      }).catch(() => {});
    }
  }
  $("chkLive").addEventListener("change", () => refreshFrame());
  $("frame").addEventListener("load", drawOverlay);
  window.addEventListener("resize", drawOverlay);

  // координаты зон — в пикселях кадра, на котором размечали (frame_size), либо оригинала камеры
  function refSize() {
    const c = curCam();
    if (c && c.frame_size) return c.frame_size;
    return [S.fw || $("frame").naturalWidth || 1, S.fh || $("frame").naturalHeight || 1];
  }

  function drawOverlay() {
    const cv = $("ov"), img = $("frame");
    const w = img.clientWidth, h = img.clientHeight;
    if (!w || !h) return;
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    const g = cv.getContext("2d");
    g.clearRect(0, 0, w, h);
    const c = curCam(); if (!c) return;
    const [rw, rh] = S.mark ? [S.fw || img.naturalWidth, S.fh || img.naturalHeight] : refSize();
    const sx = w / rw, sy = h / rh;
    const st = curState();
    const byId = {}; ((st && st.zones) || []).forEach((z) => { byId[z.id] = z; });
    g.lineWidth = 2; g.font = "bold 14px sans-serif";
    c.zones.forEach((z) => {
      if (!z.enabled) return;
      const live = byId[z.id], on = live && live.bit === 1;
      g.strokeStyle = on ? "#22c55e" : "#f59e0b";
      g.fillStyle = on ? "rgba(34,197,94,.22)" : "rgba(245,158,11,.12)";
      g.beginPath();
      z.points.forEach((p, i) => (i ? g.lineTo(p[0] * sx, p[1] * sy) : g.moveTo(p[0] * sx, p[1] * sy)));
      g.closePath(); g.fill(); g.stroke();
      const p0 = z.points[0];
      g.fillStyle = "#fff"; g.strokeStyle = "#000"; g.lineWidth = 3;
      const label = z.name + (live ? "  " + live.value.toFixed(2) + (on ? "  ●" : "") : "");
      g.strokeText(label, p0[0] * sx + 4, p0[1] * sy + 16); g.fillText(label, p0[0] * sx + 4, p0[1] * sy + 16);
      g.lineWidth = 2;
    });
    if (S.mark) {
      g.strokeStyle = "#38bdf8"; g.fillStyle = "#38bdf8";
      g.beginPath();
      S.mark.pts.forEach((p, i) => (i ? g.lineTo(p[0] * sx, p[1] * sy) : g.moveTo(p[0] * sx, p[1] * sy)));
      if (S.mark.cursor) g.lineTo(S.mark.cursor[0] * sx, S.mark.cursor[1] * sy);
      g.stroke();
      S.mark.pts.forEach((p) => { g.beginPath(); g.arc(p[0] * sx, p[1] * sy, 4, 0, 7); g.fill(); });
    }
  }

  // ───────────── разметка зоны ─────────────
  function setMarkUi() {
    const m = !!S.mark;
    $("stage").classList.toggle("marking", m);
    $("btnClose").disabled = !m || S.mark.pts.length < 3;
    $("btnUndo").disabled = !m || !S.mark.pts.length;
    $("btnCancel").disabled = !m;
    $("btnMark").disabled = m || !S.cam;
  }
  function cancelMark() { S.mark = null; $("newZone").classList.add("hidden"); setMarkUi(); drawOverlay(); }

  $("btnMark").addEventListener("click", async () => {
    if (!S.fw) { $("hint").textContent = "Кадра ещё нет — подожди подключения камеры или сохрани настройки."; return; }
    S.mark = { pts: [], cursor: null };
    $("hint").textContent = "Кликай по углам ленты. ПКМ или «Убрать точку» — шаг назад. «Замкнуть» / Enter — готово (минимум 3 точки). Кадр замер — это нормально.";
    setMarkUi(); drawOverlay();
  });

  function canvasPt(ev) {
    const cv = $("ov"), r = cv.getBoundingClientRect();
    const W = S.fw || $("frame").naturalWidth, H = S.fh || $("frame").naturalHeight;
    return [Math.round((ev.clientX - r.left) / r.width * W), Math.round((ev.clientY - r.top) / r.height * H)];
  }
  $("ov").addEventListener("click", (e) => { if (!S.mark) return; S.mark.pts.push(canvasPt(e)); setMarkUi(); drawOverlay(); });
  $("ov").addEventListener("mousemove", (e) => { if (!S.mark) return; S.mark.cursor = canvasPt(e); drawOverlay(); });
  $("ov").addEventListener("contextmenu", (e) => { if (!S.mark) return; e.preventDefault(); S.mark.pts.pop(); setMarkUi(); drawOverlay(); });
  $("btnUndo").addEventListener("click", () => { if (S.mark) { S.mark.pts.pop(); setMarkUi(); drawOverlay(); } });
  $("btnCancel").addEventListener("click", cancelMark);
  $("btnClose").addEventListener("click", closePolygon);
  document.addEventListener("keydown", (e) => {
    if (!S.mark || /INPUT|SELECT|TEXTAREA/.test((e.target || {}).tagName || "")) return;
    if (e.key === "Enter") closePolygon(); else if (e.key === "Escape") cancelMark();
  });

  function closePolygon() {
    if (!S.mark || S.mark.pts.length < 3) return;
    S.mark.closed = true; S.mark.cursor = null;
    const box = $("newZone");
    box.classList.remove("hidden"); box.dataset.mode = "zone";
    box.innerHTML = '<div class="mo-form"><label>Название зоны</label><input type="text" id="nzName" placeholder="Лента 1" />' +
      '<label>Тип</label><select id="nzType"><option value="motion">motion — движется (датчик вращения)</option><option value="presence">presence — жом идёт (низкий поток)</option></select>' +
      '<label>Метрика</label><select id="nzMetric"><option value="diff">diff — разность кадров</option><option value="flow">flow — оптический поток</option></select></div>' +
      '<div style="margin-top:8px;display:flex;gap:8px"><button type="button" class="btn btn-xs" id="nzOk">Добавить зону</button>' +
      '<button type="button" class="btn btn-xs btn-secondary" id="nzCancel">Отмена</button></div>';
    $("nzName").focus();
    $("nzCancel").onclick = cancelMark;
    $("nzOk").onclick = () => {
      const c = curCam(); if (!c) return;
      let n = 1; while (c.zones.some((z) => z.id === "z" + n)) n++;
      const metric = $("nzMetric").value, type = $("nzType").value;
      c.frame_size = [S.fw || $("frame").naturalWidth, S.fh || $("frame").naturalHeight];
      c.zones.push({ id: "z" + n, name: $("nzName").value.trim() || "Зона " + n, enabled: true, type: type, metric: metric, points: S.mark.pts.slice(),
        on: THR[metric][0], off: THR[metric][1], invert: type === "presence", smooth: 5, frames_to_on: 3, frames_to_off: 4, blur: 5, flicker_comp: true, out: null });
      S.mark = null; box.classList.add("hidden");
      dirty(); setMarkUi(); renderZones(); renderGroups(); rebuildCharts(); drawOverlay();
      $("hint").textContent = "Зона добавлена. Выставь вывод в ПЛК и нажми «Сохранить настройки» — тогда пойдут цифры.";
    };
  }

  // ───────────── таблица зон ─────────────
  function outCell(o, idp) {
    const kind = o ? o.kind : "", addr = o ? o.address : 0;
    return '<select data-k="okind" data-i="' + idp + '"><option value="">нет</option><option value="holding"' + (kind === "holding" ? " selected" : "") +
      '>регистр</option><option value="coil"' + (kind === "coil" ? " selected" : "") + '>coil</option></select> <input type="number" min="0" max="65535" data-k="oaddr" data-i="' +
      idp + '" value="' + addr + '" style="width:64px" />';
  }

  function renderZones() {
    const c = curCam(), tb = document.querySelector("#zonesTbl tbody");
    tb.innerHTML = "";
    $("zonesEmpty").classList.toggle("hidden", !!(c && c.zones.length));
    if (!c) return;
    c.zones.forEach((z, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td><input type="text" class="mo-name" data-k="name" data-i="' + i + '" value="' + esc(z.name) + '" title="' + esc(z.id) + '" /></td>' +
        '<td><span class="mo-bit" id="zb-' + esc(z.id) + '">–</span></td><td class="mo-num" id="zf-' + esc(z.id) + '">–</td><td class="mo-num" id="zd-' + esc(z.id) + '">–</td>' +
        '<td><select data-k="type" data-i="' + i + '"><option value="motion"' + (z.type === "motion" ? " selected" : "") + ">motion</option><option value=\"presence\"" + (z.type === "presence" ? " selected" : "") + ">presence</option></select></td>" +
        '<td><select data-k="metric" data-i="' + i + '"><option value="diff"' + (z.metric === "diff" ? " selected" : "") + ">diff</option><option value=\"flow\"" + (z.metric === "flow" ? " selected" : "") + ">flow</option></select></td>" +
        '<td><input type="number" step="0.05" min="0" data-k="on" data-i="' + i + '" value="' + z.on + '" title="включить при значении ' + (z.invert ? "НИЖЕ off" : "ВЫШЕ on") + '" /></td>' +
        '<td><input type="number" step="0.05" min="0" data-k="off" data-i="' + i + '" value="' + z.off + '" /></td>' +
        "<td>" + outCell(z.out, i) + "</td>" +
        '<td><button type="button" class="btn btn-xs btn-secondary" data-k="del" data-i="' + i + '" title="Удалить зону">✕</button></td>';
      tb.appendChild(tr);
    });
    paintLive();
  }

  document.querySelector("#zonesTbl").addEventListener("change", (e) => onZoneEdit(e));
  document.querySelector("#zonesTbl").addEventListener("click", (e) => { if (e.target.dataset && e.target.dataset.k === "del") onZoneEdit(e); });

  function onZoneEdit(e) {
    const t = e.target, k = t.dataset && t.dataset.k, c = curCam();
    if (!k || !c) return;
    const z = c.zones[+t.dataset.i]; if (!z) return;
    if (k === "del") {
      if (!confirm("Удалить зону «" + z.name + "»?")) return;
      c.zones.splice(+t.dataset.i, 1);
      c.groups.forEach((g) => { g.zones = g.zones.filter((x) => x !== z.id); if (g.order) g.order = g.order.filter((x) => x !== z.id); });
      c.groups = c.groups.filter((g) => g.zones.length);
      renderZones(); renderGroups(); rebuildCharts(); drawOverlay();
    } else if (k === "name") z.name = t.value;
    else if (k === "type") { z.type = t.value; z.invert = t.value === "presence"; renderZones(); }
    else if (k === "metric") { z.metric = t.value; z.on = THR[z.metric][0]; z.off = THR[z.metric][1]; renderZones(); rebuildCharts(); }
    else if (k === "on" || k === "off") z[k] = parseFloat(t.value) || 0;
    else if (k === "okind") { z.out = t.value ? { kind: t.value, address: (z.out && z.out.address) || 0 } : null; }
    else if (k === "oaddr") { if (z.out) z.out.address = parseInt(t.value, 10) || 0; else { /* адрес без вида — ждём вид */ } }
    dirty();
  }

  function paintLive() {
    const st = curState();
    ((st && st.zones) || []).forEach((z) => {
      const b = $("zb-" + z.id); if (!b) return;
      b.textContent = z.bit; b.classList.toggle("on", z.bit === 1);
      $("zf-" + z.id).textContent = z.flow.toFixed(2);
      $("zd-" + z.id).textContent = z.diff.toFixed(1);
    });
  }

  // ───────────── группы ─────────────
  function renderGroups() {
    const c = curCam(), box = $("groupsList");
    $("grpEd").classList.add("hidden");
    $("btnAddGrp").disabled = !c || !c.zones.length;
    if (!c || !c.groups.length) { box.innerHTML = '<p class="mo-note">Групп нет — каждая зона пишется в ПЛК сама (колонка «Вывод»).</p>'; return; }
    const st = curState();
    box.innerHTML = c.groups.map((g, i) => {
      const live = st && st.groups.find((x) => x.id === g.id);
      const out = g.out ? ({ holding: "регистр", bits: "биты в регистр", coils: "coil на зону" }[g.out.kind] + " @" + g.out.address) : "без вывода";
      return '<div style="display:flex;gap:8px;align-items:center;margin:4px 0"><b>' + esc(g.name) + "</b><span class=\"mo-note\" style=\"margin:0\">" + g.type + " · " +
        g.zones.map((id) => { const z = c.zones.find((q) => q.id === id); return esc(z ? z.name : id); }).join(", ") + " · " + out + "</span>" +
        '<span style="flex:1"></span><span class="mo-bit' + (live && live.value ? " on" : "") + '" id="gv-' + esc(g.id) + '">' + (live ? live.value : "–") + "</span>" +
        '<button type="button" class="btn btn-xs btn-secondary" data-g="edit" data-i="' + i + '">Изменить</button>' +
        '<button type="button" class="btn btn-xs btn-secondary" data-g="del" data-i="' + i + '">✕</button></div>';
    }).join("");
  }
  $("groupsList").addEventListener("click", (e) => {
    const t = e.target, c = curCam(); if (!c || !t.dataset || !t.dataset.g) return;
    const i = +t.dataset.i;
    if (t.dataset.g === "del") { if (confirm("Удалить группу «" + c.groups[i].name + "»?")) { c.groups.splice(i, 1); dirty(); renderGroups(); } }
    else openGroupEditor(i);
  });
  $("btnAddGrp").addEventListener("click", () => openGroupEditor(-1));

  function openGroupEditor(i) {
    const c = curCam(); if (!c) return;
    const g = i >= 0 ? JSON.parse(JSON.stringify(c.groups[i])) : { id: "", name: "", type: "sum", zones: [], out: { kind: "holding", address: 0 }, window_s: 60, min_active: 1, agg: "any", scale: 100 };
    const ed = $("grpEd"); ed.classList.remove("hidden");
    const zs = c.zones.map((z) => '<label class="mo-chk"><input type="checkbox" value="' + esc(z.id) + '"' + (g.zones.includes(z.id) ? " checked" : "") + " />" + esc(z.name) + "</label>").join("");
    ed.innerHTML = '<div class="mo-form"><label>Название</label><input type="text" id="geName" value="' + esc(g.name) + '" placeholder="Уровень шахты / Наработка" />' +
      '<label>Тип</label><select id="geType"><option value="sum">sum — сколько зон активно</option><option value="monotonic">monotonic — уровень снизу вверх</option><option value="worktimer">worktimer — наработка за окно</option></select>' +
      "<label>Зоны</label><div>" + zs + "</div>" +
      '<label>Вывод</label><div class="row"><select id="geKind"><option value="">нет</option><option value="holding">значение в регистр</option><option value="bits">биты зон в один регистр</option><option value="coils">coil на каждую зону</option></select>' +
      '<span>адрес</span><input type="number" id="geAddr" min="0" max="65535" value="' + ((g.out && g.out.address) || 0) + '" /></div>' +
      '<div id="geMono" class="hidden" style="grid-column:1/3"><span class="mo-note">Порядок «снизу вверх» = порядок зон в списке выше (первая зона — низ шахты).</span></div>' +
      '<label class="ge-wt">Окно, с</label><input type="number" id="geWin" min="1" value="' + (g.window_s || 60) + '" class="ge-wt" />' +
      '<label class="ge-wt">Мин. активных зон</label><input type="number" id="geMin" min="1" value="' + (g.min_active || 1) + '" class="ge-wt" />' +
      '<label class="ge-wt">Секунда рабочая, если</label><select id="geAgg" class="ge-wt"><option value="any">хоть один кадр активен</option><option value="majority">не меньше половины кадров</option><option value="all">все кадры активны</option></select>' +
      '<label class="ge-wt">Шкала</label><input type="number" id="geScale" min="1" value="' + (g.scale || 100) + '" class="ge-wt" title="100 = проценты 0..100; 1000 или 4095 — для аналогового выхода" /></div>' +
      '<div style="margin-top:8px;display:flex;gap:8px"><button type="button" class="btn btn-xs" id="geOk">Готово</button><button type="button" class="btn btn-xs btn-secondary" id="geCancel">Отмена</button></div>';
    $("geType").value = g.type; $("geKind").value = g.out ? g.out.kind : ""; $("geAgg").value = g.agg || "any";
    const sync = () => { const t = $("geType").value; ed.querySelectorAll(".ge-wt").forEach((n) => n.classList.toggle("hidden", t !== "worktimer")); $("geMono").classList.toggle("hidden", t !== "monotonic"); };
    $("geType").onchange = sync; sync();
    $("geCancel").onclick = () => ed.classList.add("hidden");
    $("geOk").onclick = () => {
      const ids = [...ed.querySelectorAll('input[type=checkbox]:checked')].map((x) => x.value);
      if (!ids.length) { alert("Выбери хотя бы одну зону"); return; }
      const type = $("geType").value;
      const ng = { id: g.id, name: $("geName").value.trim() || "Группа", type: type, zones: ids, out: $("geKind").value ? { kind: $("geKind").value, address: parseInt($("geAddr").value, 10) || 0 } : null };
      if (!ng.id) { let n = 1; while (c.groups.some((q) => q.id === "g" + n)) n++; ng.id = "g" + n; }
      if (type === "monotonic") ng.order = c.zones.map((z) => z.id).filter((id) => ids.includes(id));
      if (type === "worktimer") Object.assign(ng, { window_s: parseInt($("geWin").value, 10) || 60, min_active: Math.min(ids.length, parseInt($("geMin").value, 10) || 1), agg: $("geAgg").value, scale: parseInt($("geScale").value, 10) || 100 });
      if (i >= 0) c.groups[i] = ng; else c.groups.push(ng);
      dirty(); renderGroups();
    };
  }

  // ───────────── камера и ПЛК ─────────────
  function numRow(id, v, extra) { return '<input type="number" id="' + id + '" value="' + v + '" ' + (extra || "") + " />"; }

  function renderCamForm() {
    const c = curCam(), f = $("camForm");
    if (!c) { f.innerHTML = '<span class="mo-note" style="grid-column:1/3">Камеры нет.</span>'; return; }
    const so = c.status_out;
    f.innerHTML = "<label>Название</label><input type=\"text\" id=\"cfName\" value=\"" + esc(c.name) + "\" />" +
      "<label>Адрес потока</label><input type=\"text\" id=\"cfUrl\" value=\"" + esc(c.url) + "\" />" +
      "<label>Кадров/с в анализ</label>" + numRow("cfFps", c.proc_fps, 'min="0.2" max="30" step="0.5"') +
      "<label title=\"Сравнивать кадр не с соседним, а через N кадров: медленное движение виднее\">Шаг сравнения, кадров</label>" + numRow("cfGap", c.frame_gap, 'min="1" max="10"') +
      "<label title=\"Картинка не меняется дольше — камера зависла: в ПЛК нули и статус 0\">Камера зависла через, с</label>" + numRow("cfFrz", c.freeze_timeout_s, 'min="1"') +
      "<label>Статус «камера жива»</label><div class=\"row\"><select id=\"cfSKind\"><option value=\"\">нет</option><option value=\"holding\">регистр</option><option value=\"coil\">coil</option></select><span>адрес</span>" +
      numRow("cfSAddr", so ? so.address : 0, 'min="0" max="65535"') + "</div>" +
      "<label>Включена</label><input type=\"checkbox\" id=\"cfOn\"" + (c.enabled ? " checked" : "") + " />";
    $("cfSKind").value = so ? so.kind : "";
    const upd = () => {
      c.name = $("cfName").value; c.url = $("cfUrl").value.trim(); c.proc_fps = parseFloat($("cfFps").value) || 3; c.frame_gap = parseInt($("cfGap").value, 10) || 1;
      c.freeze_timeout_s = parseFloat($("cfFrz").value) || 10; c.enabled = $("cfOn").checked;
      c.status_out = $("cfSKind").value ? { kind: $("cfSKind").value, address: parseInt($("cfSAddr").value, 10) || 0 } : null;
      dirty();
    };
    f.querySelectorAll("input,select").forEach((n) => n.addEventListener("change", upd));
  }

  function renderPlc() {
    const m = S.cfg ? S.cfg.modbus : null, f = $("plcForm");
    if (!m) { f.innerHTML = ""; return; }
    f.innerHTML = "<label>Писать в ПЛК</label><input type=\"checkbox\" id=\"pcOn\"" + (m.enable ? " checked" : "") + " />" +
      "<label>IP контроллера</label><input type=\"text\" id=\"pcHost\" value=\"" + esc(m.host) + "\" />" +
      "<label>Порт / Unit ID</label><div class=\"row\">" + numRow("pcPort", m.port, 'min="1" max="65535"') + numRow("pcUid", m.device_id, 'min="0" max="255"') + "</div>" +
      "<label title=\"Значения пишутся сразу при изменении и ещё раз с этим периодом\">Обновлять не реже, с</label>" + numRow("pcPer", m.write_period_s, 'min="0.1" step="0.1"') +
      "<label>Проверка связи</label><div class=\"row\"><span>регистр</span>" + numRow("pcTest", 0, 'min="0" max="65535"') + "<button type=\"button\" class=\"btn btn-xs btn-secondary\" id=\"pcPing\">Прочитать</button></div>";
    const upd = () => {
      m.enable = $("pcOn").checked; m.host = $("pcHost").value.trim(); m.port = parseInt($("pcPort").value, 10) || 502;
      m.device_id = parseInt($("pcUid").value, 10) || 0; m.write_period_s = parseFloat($("pcPer").value) || 1; dirty();
    };
    f.querySelectorAll("input").forEach((n) => { if (n.id !== "pcTest") n.addEventListener("change", upd); });
    $("pcPing").onclick = async () => {
      $("plcInfo").textContent = "читаю…"; $("plcInfo").className = "mo-note";
      const r = await putJson("/api/motion/modbus_ping", { host: $("pcHost").value.trim(), port: parseInt($("pcPort").value, 10) || 502, device_id: parseInt($("pcUid").value, 10) || 0, address: parseInt($("pcTest").value, 10) || 0 });
      $("plcInfo").textContent = r.error ? r.error : (r.ok ? "связь есть, регистр = " + r.info + " (только чтение, в ПЛК ничего не записано)" : "нет связи: " + r.info);
      $("plcInfo").className = "mo-note " + (r.ok ? "mo-ok" : "mo-bad");
    };
  }

  // ───────────── сохранение ─────────────
  $("btnSave").addEventListener("click", async () => {
    $("saveInfo").textContent = "сохраняю…"; $("saveInfo").className = "mo-note";
    const r = await putJson("/api/motion/config", S.cfg, "PUT");
    if (r.error) { $("saveInfo").textContent = "Не сохранено: " + r.error; $("saveInfo").className = "mo-note mo-bad"; return; }
    S.cfg = r; dirty(false);
    if (!cams().some((c) => c.id === S.cam)) S.cam = cams().length ? cams()[0].id : null;
    renderAll();
    $("saveInfo").textContent = "сохранено " + new Date().toLocaleTimeString(); $("saveInfo").className = "mo-note mo-ok";
    S.fw = 0; setTimeout(() => refreshFrame(true), 1500);
  });
  window.addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });

  // ───────────── запись CSV ─────────────
  $("btnRec").addEventListener("click", async () => {
    const st = S.state[S.cam], on = !(st && st.recording);
    const r = await putJson("/api/motion/record", { camera: S.cam, on: on });
    $("hint").textContent = r.error ? r.error : (r.recording ? "Пишу метрики в motion_service\\data\\records\\" + r.recording : "Запись остановлена.");
  });

  // ───────────── живые данные ─────────────
  async function loopState() {
    if (S.online) {
      const r = await api("/api/motion/state");
      if (!r.error) {
        (r.cameras || []).forEach((c) => { S.state[c.id] = c; });
        paintLive(); paintGroupsLive(); paintCamStatus(); drawOverlay();
      }
      if (Math.random() < 0.15) api("/api/motion/health").then((h) => { if (h) { S.online = !!h.online; paintBadge(h); } });
    }
    setTimeout(loopState, 1000);
  }

  function paintGroupsLive() {
    const st = curState(); if (!st) return;
    st.groups.forEach((g) => { const b = $("gv-" + g.id); if (b) { b.textContent = g.value; b.classList.toggle("on", g.value > 0); } });
  }

  function paintCamStatus() {
    const s = S.state[S.cam], el = $("camStatus");
    if (!s) { el.textContent = ""; $("btnRec").textContent = "● запись CSV"; return; }
    let t, cls;
    if (!s.enabled) { t = "выключена"; cls = "mo-note"; }
    else if (!s.connected) { t = "нет связи с источником"; cls = "mo-note mo-bad"; }
    else if (s.result && !s.result.alive) { t = s.result.reason || "нет данных"; cls = "mo-note mo-bad"; }
    else { t = "жива · " + s.fps + " к/с"; cls = "mo-note mo-ok"; }
    const p = s.plc || {};
    if (p.enabled) t += " · ПЛК: " + (p.ok === null ? "…" : p.ok ? "ок" : "ошибка (" + (p.error || "") + ")");
    el.textContent = t; el.className = cls;
    $("btnRec").textContent = s.recording ? "■ остановить запись (" + s.recording + ")" : "● запись CSV";
  }

  // ───────────── графики калибровки ─────────────
  function rebuildCharts() {
    const c = curCam(), box = $("charts");
    box.innerHTML = "";
    if (!c) return;
    c.zones.forEach((z) => {
      const row = document.createElement("div"); row.className = "mo-chart";
      row.innerHTML = '<span class="lbl" title="' + esc(z.name) + '">' + esc(z.name) + " (" + z.metric + ')</span><canvas id="ch-' + esc(z.id) + '" height="56"></canvas>';
      box.appendChild(row);
    });
  }

  async function loopCharts() {
    const c = curCam();
    if (S.online && c && c.zones.length && !S.mark) {
      const h = await api("/api/motion/history?camera=" + encodeURIComponent(c.id) + "&n=300");
      if (!h.error && h.zones) c.zones.forEach((z) => drawChart(z, h.zones[z.id]));
    }
    setTimeout(loopCharts, 2000);
  }

  function drawChart(z, d) {
    const cv = $("ch-" + z.id); if (!cv || !d) return;
    const w = cv.clientWidth, h = 56;
    if (cv.width !== w) cv.width = w;
    const g = cv.getContext("2d"); g.clearRect(0, 0, w, h);
    const ys = z.metric === "flow" ? d.flow : d.diff, n = ys.length; if (n < 2) return;
    const top = Math.max(z.on * 1.6, ...ys, 0.0001);
    const X = (i) => (i / (n - 1)) * (w - 2) + 1, Y = (v) => h - 3 - (v / top) * (h - 8);
    g.fillStyle = "rgba(34,197,94,.22)";
    for (let i = 0; i < n; i++) if (d.bit[i]) g.fillRect(X(i) - w / n / 2, 0, w / n + 1, h);
    g.setLineDash([4, 3]); g.strokeStyle = "#ef4444"; g.beginPath(); g.moveTo(0, Y(z.on)); g.lineTo(w, Y(z.on)); g.stroke();
    g.strokeStyle = "#f59e0b"; g.beginPath(); g.moveTo(0, Y(z.off)); g.lineTo(w, Y(z.off)); g.stroke(); g.setLineDash([]);
    g.strokeStyle = "#38bdf8"; g.lineWidth = 1.6; g.beginPath();
    ys.forEach((v, i) => (i ? g.lineTo(X(i), Y(v)) : g.moveTo(X(i), Y(v)))); g.stroke();
    g.fillStyle = "#94a3b8"; g.font = "10px sans-serif"; g.fillText(top.toFixed(top < 10 ? 2 : 0), 3, 10);
  }

  init();
})();
