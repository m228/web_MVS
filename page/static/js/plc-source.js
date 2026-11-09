// static/js/plc-source.js — «Вакуум-аппарат (ПЛК)»: источник данных аппарата, Modbus или OPC UA. ТОЛЬКО чтение.
// Один компонент на две страницы: микроскоп (вкладка «ПЛК» в нижней полосе) и /multi (карточка внизу).
// PlcSource.mount(узел) — рисует панель; настройки берёт из /api/plc/source, живые значения опрашивает, пока панель видна.
// Данные с сервера OPC UA (имена тегов, значения) — чужой текст: только textContent, никакого innerHTML.
(function () {
  const LABELS = {
    sv: "СВ (Brix)", temp_app: "Температура, °C", level: "Уровень, %", current: "Ток циркулятора, А",
    press_top: "Разрежение сверху", press_bot: "Давление снизу", vacuum: "Разрежение (резерв)",
    stage: "Стадия варки", substage: "Подстадия", cook_time: "Время варки, с",
    v_syrup: "Клапан сиропа, %", v_water: "Клапан воды, %", v_product: "Клапан продукта, %",
  };
  const POLICY_LABEL = { None: "без защиты", Basic256Sha256: "Basic256Sha256", Aes128_Sha256_RsaOaep: "Aes128_Sha256_RsaOaep", Aes256_Sha256_RsaPss: "Aes256_Sha256_RsaPss" };
  const MODE_LABEL = { Sign: "подпись", SignAndEncrypt: "подпись + шифрование" };

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v == null || v === false) return;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    });
    kids.flat().forEach((c) => { if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c))); });
    return n;
  }
  const fmt = (v) => (v == null ? "(—)" : typeof v === "number" ? String(Math.round(v * 1000) / 1000) : String(v));

  async function post(url, body, timeout = 20000) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeout);
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctl.signal, cache: "no-store" });
      return await r.json();
    } catch (e) {
      return { ok: false, status: "error", error: e.name === "AbortError" ? "сервер не ответил за " + timeout / 1000 + " с" : e.message };
    } finally { clearTimeout(t); }
  }
  async function get(url, timeout = 20000) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeout);
    try { return await (await fetch(url, { cache: "no-store", signal: ctl.signal })).json(); }
    catch (e) { return { ok: false, error: e.name === "AbortError" ? "сервер не ответил за " + timeout / 1000 + " с" : e.message }; }
    finally { clearTimeout(t); }
  }

  function mount(root) {
    if (!root || root.dataset.plcMounted) return;
    root.dataset.plcMounted = "1";
    root.classList.add("plc-src");
    const S = { cfg: null, proto: "modbus", active: "sv", test: {}, status: null, running: false, eps: null };
    const inp = {};                                   // поля ввода по ключу
    const rowVal = { modbus: {}, opcua: {} };         // ячейки «значение» по полю
    const rowNode = {}, rowScaleOp = {}, rowOp = {};
    const rowReg = {}, rowScaleMb = {}, rowSigned = {};

    // ── шапка ──
    const segBtns = {
      modbus: el("button", { type: "button", class: "plc-seg__btn", text: "Modbus TCP", onclick: () => setProto("modbus") }),
      opcua: el("button", { type: "button", class: "plc-seg__btn", text: "OPC UA", onclick: () => setProto("opcua") }),
    };
    inp.enabled = el("input", { type: "checkbox" });
    const badge = el("span", { class: "micro-plc-badge", text: "—" });
    const msg = el("p", { class: "plc-msg", "aria-live": "polite" });
    const btnTest = el("button", { type: "button", class: "btn-secondary plc-btn", text: "Проверить", title: "Прочитать назначенные поля один раз — с теми настройками, что сейчас на экране (ещё не сохранёнными)", onclick: test });
    const btnSave = el("button", { type: "button", class: "btn-primary plc-btn", text: "Сохранить", title: "Сохранить и перезапустить чтение аппарата (плата и автомат не перезапускаются)", onclick: save });
    const head = el("div", { class: "plc-src__head" },
      el("div", { class: "plc-seg", role: "tablist", "aria-label": "Протокол" }, segBtns.modbus, segBtns.opcua),
      el("label", { class: "plc-chk", title: "Читать данные аппарата из контроллера. Выключено — на странице «(—)», СВ и стадия вручную" }, inp.enabled, el("span", { text: "читать" })),
      badge, el("span", { class: "plc-src__sp" }), btnTest, btnSave);

    // ── Modbus ──
    const num = (key, w, title) => (inp[key] = el("input", { type: "number", class: "plc-in plc-in--" + w, title }));
    const txt = (key, w, ph, title) => (inp[key] = el("input", { type: "text", class: "plc-in plc-in--" + w, placeholder: ph || "", title, spellcheck: "false", autocomplete: "off" }));
    const lab = (text, ...c) => el("label", { class: "plc-lab" }, el("span", { text }), ...c);
    const mbTable = el("table", { class: "plc-fields" },
      el("thead", {}, el("tr", {}, el("th", { text: "Поле" }), el("th", { text: "Регистр" }), el("th", { text: "Масштаб", title: "значение = регистр / масштаб" }), el("th", { text: "±", title: "знаковый int16 (может быть меньше нуля)" }), el("th", { text: "Значение" }))),
      el("tbody"));
    const paneMb = el("div", { class: "plc-pane", "data-p": "modbus" },
      el("div", { class: "plc-row" },
        lab("IP контроллера", txt("mb_host", "ip", "10.20.2.180")), lab("Порт", num("mb_port", "s")), lab("Unit", num("mb_unit", "s", "unit (slave id) Modbus-сервера ПЛК")),
        lab("Опрос, с", num("period_mb", "s"))),
      mbTable);

    // ── OPC UA ──
    inp.op_security = el("select", { class: "plc-in plc-in--m", onchange: syncSecurity });
    inp.op_mode = el("select", { class: "plc-in plc-in--m" });
    inp.op_auth = el("select", { class: "plc-in plc-in--s", onchange: syncSecurity }, el("option", { value: "anonymous", text: "анонимно" }), el("option", { value: "user", text: "логин" }));
    inp.op_password = el("input", { type: "password", class: "plc-in plc-in--m", autocomplete: "new-password", title: "Хранится на этом ПК в data\\opcua_secret.json; на страницу не возвращается. Пусто — оставить прежний" });
    const userBox = el("span", { class: "plc-inline" }, lab("Логин", txt("op_username", "m", "", "")), lab("Пароль", inp.op_password));
    const epNote = el("div", { class: "plc-eps" });
    const plainWarn = el("p", { class: "plc-warn", hidden: true, text: "Вход по логину без защиты: пароль уходит по сети открытым текстом. Если сервер умеет — выбери Basic256Sha256." });
    const scanBox = el("div", { class: "plc-scan", hidden: true });
    const tree = el("div", { class: "plc-tree", role: "tree", "aria-label": "Теги сервера" }, el("p", { class: "plc-hint", text: "Нажми «Подключиться» — здесь появятся теги сервера." }));
    const opTable = el("table", { class: "plc-fields" },
      el("thead", {}, el("tr", {}, el("th", { text: "Поле" }), el("th", { text: "Тег (NodeId)" }), el("th", { text: "Масштаб", title: "значение = тег / масштаб (обычно 1)" }), el("th", { text: "Значение" }), el("th"))),
      el("tbody"));
    const paneOp = el("div", { class: "plc-pane", "data-p": "opcua" },
      el("div", { class: "plc-row" },
        lab("Адрес сервера", txt("op_endpoint", "url", "opc.tcp://10.20.2.180:4840", "Endpoint сервера OPC UA (уточнить у наладчиков SCADA/ПЛК)")),
        el("button", { type: "button", class: "btn-primary plc-btn", text: "Подключиться", title: "Спросить у сервера режимы защиты и открыть дерево тегов", onclick: connect }),
        el("button", { type: "button", class: "btn-secondary plc-btn", text: "Найти в сети", title: "Поиск серверов OPC UA: у кого в подсети открыт порт", onclick: toggleScan })),
      scanBox,
      el("div", { class: "plc-row" },
        lab("Защита", inp.op_security), lab("Режим", inp.op_mode), lab("Вход", inp.op_auth), userBox,
        lab("Опрос, с", num("period_op", "s")), lab("Таймаут, с", num("op_timeout", "s")),
        el("a", { class: "plc-link", href: "/api/opcua/client_cert", download: "web_mvs_client.der", title: "Сертификат этого ПК для шифрованного подключения: его один раз добавляют в доверенные на сервере OPC UA", text: "сертификат ⤓" })),
      plainWarn, epNote,
      el("div", { class: "plc-two" }, tree,
        el("div", { class: "plc-map" }, el("p", { class: "plc-hint", text: "Выбери поле в таблице, потом тег в дереве слева — он назначится на поле." }), opTable)));

    root.replaceChildren(head, msg, paneMb, paneOp);

    function say(text, kind) { msg.textContent = text || ""; msg.dataset.kind = kind || ""; }

    // ── таблицы полей ──
    function buildTables(fields) {
      const tbMb = mbTable.tBodies[0], tbOp = opTable.tBodies[0];
      tbMb.replaceChildren(); tbOp.replaceChildren();
      fields.forEach((f) => {
        rowReg[f] = el("input", { type: "number", class: "plc-in plc-in--s", min: "0", max: "65535" });
        rowScaleMb[f] = el("input", { type: "number", class: "plc-in plc-in--s", step: "any" });
        rowSigned[f] = el("input", { type: "checkbox" });
        rowVal.modbus[f] = el("td", { class: "plc-val" });
        tbMb.append(el("tr", {}, el("td", { text: LABELS[f] || f }), el("td", {}, rowReg[f]), el("td", {}, rowScaleMb[f]), el("td", { class: "plc-c" }, rowSigned[f]), rowVal.modbus[f]));

        rowNode[f] = el("input", { type: "text", class: "plc-in plc-in--node", spellcheck: "false", autocomplete: "off", placeholder: "не читается", onfocus: () => setActive(f) });
        rowScaleOp[f] = el("input", { type: "number", class: "plc-in plc-in--s", step: "any" });
        rowVal.opcua[f] = el("td", { class: "plc-val" });
        rowOp[f] = el("tr", { onclick: () => setActive(f) }, el("td", { text: LABELS[f] || f }), el("td", {}, rowNode[f]), el("td", {}, rowScaleOp[f]), rowVal.opcua[f],
          el("td", {}, el("button", { type: "button", class: "plc-x", title: "Не читать это поле", text: "×", onclick: (e) => { e.stopPropagation(); rowNode[f].value = ""; delete S.test[f]; paintValues(); } })));
        tbOp.append(rowOp[f]);
      });
      setActive(S.active);
    }
    function setActive(f) {
      S.active = f;
      Object.entries(rowOp).forEach(([k, tr]) => tr.classList.toggle("is-active", k === f));
    }

    function fill(cfg) {
      S.cfg = cfg;
      const mb = cfg.modbus || {}, op = cfg.opcua || {};
      buildTables(cfg.fields || Object.keys(LABELS));
      inp.enabled.checked = !!cfg.enabled;
      inp.mb_host.value = mb.host || ""; inp.mb_port.value = mb.port ?? 502; inp.mb_unit.value = mb.unit ?? 255;
      inp.period_mb.value = inp.period_op.value = cfg.period_s ?? 1;
      (cfg.fields || []).forEach((f) => {
        const a = (mb.fields || {})[f] || {}, b = (op.fields || {})[f] || {};
        rowReg[f].value = a.reg || 0; rowScaleMb[f].value = a.scale ?? 1; rowSigned[f].checked = !!a.signed;
        rowNode[f].value = b.node || ""; rowScaleOp[f].value = b.scale ?? 1;
      });
      inp.op_security.replaceChildren(...(cfg.policies || ["None"]).map((p) => el("option", { value: p, text: POLICY_LABEL[p] || p })));
      inp.op_mode.replaceChildren(...(cfg.modes || ["SignAndEncrypt"]).map((m) => el("option", { value: m, text: MODE_LABEL[m] || m })));
      inp.op_endpoint.value = op.endpoint || ""; inp.op_security.value = op.security || "None"; inp.op_mode.value = op.mode || "SignAndEncrypt";
      inp.op_auth.value = op.auth || "anonymous"; inp.op_username.value = op.username || ""; inp.op_timeout.value = op.timeout_s ?? 3;
      inp.op_password.value = "";
      inp.op_password.placeholder = op.has_password ? "задан — пусто = прежний" : "";
      S.test = {};
      setProto(cfg.protocol || "modbus");
      syncSecurity();
    }
    function setProto(p) {
      S.proto = p;
      Object.entries(segBtns).forEach(([k, b]) => b.classList.toggle("is-active", k === p));
      paneMb.hidden = p !== "modbus"; paneOp.hidden = p !== "opcua";
      // период опроса один на источник — переносим между вкладками
      if (p === "opcua") inp.period_op.value = inp.period_mb.value; else inp.period_mb.value = inp.period_op.value;
      S.test = {};
      paintValues();
    }
    function syncSecurity() {
      inp.op_mode.disabled = inp.op_security.value === "None";
      userBox.hidden = inp.op_auth.value !== "user";
      plainWarn.hidden = !(inp.op_auth.value === "user" && inp.op_security.value === "None");
    }

    // ── что отправляем ──
    function connBody() {
      return { endpoint: inp.op_endpoint.value.trim(), security: inp.op_security.value, mode: inp.op_mode.value, auth: inp.op_auth.value,
        timeout_s: Number(inp.op_timeout.value) || 3, username: inp.op_username.value.trim(), password: inp.op_password.value };
    }
    function collect() {
      const fields = (S.cfg && S.cfg.fields) || [];
      const period = Number(S.proto === "opcua" ? inp.period_op.value : inp.period_mb.value) || 1;
      const mbFields = {}, opFields = {};
      fields.forEach((f) => {
        const sc = (x) => (x.value.trim() === "" ? 1 : Number(x.value));      // пусто = 1; 0 уйдёт как есть — сервер скажет, что нельзя
        mbFields[f] = { reg: Number(rowReg[f].value) || 0, scale: sc(rowScaleMb[f]), signed: rowSigned[f].checked };
        opFields[f] = { node: rowNode[f].value.trim(), scale: sc(rowScaleOp[f]) };
      });
      const op = { ...connBody(), fields: opFields };
      if (!op.password) delete op.password;                  // пусто — пароль на ПК остаётся прежним
      return { enabled: inp.enabled.checked, protocol: S.proto, period_s: period,
        modbus: { host: inp.mb_host.value.trim(), port: Number(inp.mb_port.value) || 502, unit: Number(inp.mb_unit.value) || 0, fields: mbFields },
        opcua: op };
    }

    // ── значения: свежие из «Проверить», иначе живые из работающего источника (если он того же протокола) ──
    function paintValues() {
      const fields = (S.cfg && S.cfg.fields) || [];
      const st = S.status || {};
      const live = st.connected && (st.protocol || "modbus") === S.proto ? (st.values || {}) : {};
      fields.forEach((f) => {
        const cell = rowVal[S.proto] && rowVal[S.proto][f];
        if (!cell) return;
        const has = Object.prototype.hasOwnProperty.call(S.test, f);
        cell.textContent = fmt(has ? S.test[f] : live[f]);
        cell.classList.toggle("is-test", has);
        cell.title = has ? "результат «Проверить»" : live[f] != null ? "читается сейчас" : "";
      });
      if (!st.enabled) { badge.textContent = S.running ? "выключено" : "микроскоп выключен"; badge.className = "micro-plc-badge"; }
      else if (st.connected) { badge.textContent = "есть связь · " + (st.protocol === "opcua" ? "OPC UA" : "Modbus"); badge.className = "micro-plc-badge is-on"; }
      else { badge.textContent = "нет связи · " + (st.protocol === "opcua" ? "OPC UA" : "Modbus"); badge.className = "micro-plc-badge is-off"; }
      badge.title = st.error ? "Ошибка: " + st.error : !S.running ? "Источник ПЛК работает вместе с микроскопом; настройки сохранятся и применятся при его включении" : "";
    }

    // ── кнопки ──
    async function test() {
      const body = collect();
      say("Читаю…");
      if (S.proto === "modbus") {
        const f = body.modbus.fields, regs = Object.values(f).map((x) => x.reg).filter((r) => r > 0);
        const r = await post("/api/plc/modbus_test", { host: body.modbus.host, port: body.modbus.port, unit: body.modbus.unit, regs });
        if (!r || !r.ok) return say("Modbus: " + ((r && r.error) || "нет ответа"), "err");
        S.test = {};
        Object.entries(f).forEach(([k, x]) => {
          if (!(x.reg > 0)) return;
          let raw = r.values[String(x.reg)];
          if (raw == null) { S.test[k] = null; return; }
          if (x.signed && raw >= 0x8000) raw -= 0x10000;
          S.test[k] = x.scale === 1 ? raw : raw / x.scale;
        });
      } else {
        const f = body.opcua.fields, nodes = Object.values(f).map((x) => x.node).filter(Boolean);
        if (!nodes.length) return say("Не назначено ни одного тега.", "err");
        const r = await post("/api/opcua/read", { ...connBody(), nodes }, 30000);
        if (!r || !r.ok) return say("OPC UA: " + ((r && r.error) || "нет ответа"), "err");
        S.test = {};
        Object.entries(f).forEach(([k, x]) => {
          if (!x.node) return;
          const v = r.values[x.node];
          S.test[k] = v == null ? null : x.scale === 1 ? v : v / x.scale;
        });
      }
      const bad = Object.values(S.test).filter((v) => v == null).length;
      say(bad ? "Прочитано, но " + bad + " пол. без значения — (—): нет такого адреса/тега или плохой статус." : "Прочитано, все поля со значением. Не забудь «Сохранить».", bad ? "warn" : "ok");
      paintValues();
    }
    async function save() {
      say("Сохраняю…");
      const r = await post("/api/plc/source", collect(), 30000);
      if (!r || r.status !== "ok") return say("Не сохранено: " + ((r && r.error) || "нет ответа"), "err");
      S.status = r.live; S.running = r.running;
      fill(r.config);
      say(r.running ? "Сохранено, чтение аппарата перезапущено." : "Сохранено. Источник заработает, когда включат микроскоп.", "ok");
    }

    // ── OPC UA: подключение, режимы защиты, дерево ──
    async function connect() {
      const cb = connBody();
      if (!cb.endpoint) return say("Впиши адрес сервера: opc.tcp://IP:4840", "err");
      say("Спрашиваю сервер…");
      const e = await post("/api/opcua/endpoints", { endpoint: cb.endpoint, timeout_s: cb.timeout_s }, 30000);
      if (!e || !e.ok) { epNote.replaceChildren(); return say("Сервер не ответил: " + ((e && e.error) || "нет ответа"), "err"); }
      S.eps = e.endpoints;
      epNote.replaceChildren(el("span", { class: "plc-hint", text: "Сервер предлагает: " }),
        ...e.endpoints.map((x) => el("button", {
          type: "button", class: "plc-chip" + (x.supported ? "" : " is-off"), disabled: !x.supported,
          title: x.supported ? "Взять этот режим" : "Этот режим клиент не поддерживает",
          text: (POLICY_LABEL[x.security] || x.security) + (x.mode !== "None" ? " · " + (MODE_LABEL[x.mode] || x.mode) : "") + (x.tokens.includes("UserName") ? " · логин" : ""),
          onclick: () => { inp.op_security.value = x.security; if (x.mode !== "None") inp.op_mode.value = x.mode; syncSecurity(); connect(); },
        })));
      const cur = e.endpoints.some((x) => x.security === cb.security && (cb.security === "None" || x.mode === cb.mode));
      if (!cur) return say("Такой защиты (" + (POLICY_LABEL[cb.security] || cb.security) + ") у сервера нет — выбери режим из предложенных.", "warn");
      tree.replaceChildren();
      const ok = await expand(null, tree, 0);
      if (ok) say("Подключено. Раскрой папки и назначь теги на поля.", "ok");
    }
    async function expand(node, box, depth) {
      const r = await post("/api/opcua/browse", { ...connBody(), node }, 30000);
      if (!r || !r.ok) { say("Дерево: " + ((r && r.error) || "нет ответа"), "err"); return false; }
      if (!r.items.length) box.append(el("p", { class: "plc-hint plc-tree__empty", text: "пусто" }));
      r.items.forEach((it) => box.append(treeRow(it, depth)));
      return true;
    }
    function treeRow(it, depth) {
      const kids = el("div", { class: "plc-tree__kids", hidden: true, role: "group" });
      const isVar = it.cls === "Variable";
      const caret = el("button", { type: "button", class: "plc-tree__caret", "aria-label": "Раскрыть", text: it.has_children ? "▸" : "", disabled: !it.has_children });
      let loaded = false;
      caret.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!it.has_children) return;
        const open = kids.hidden;
        kids.hidden = !open; caret.textContent = open ? "▾" : "▸";
        if (open && !loaded) { loaded = true; kids.textContent = ""; await expand(it.node, kids, depth + 1); }
      });
      const line = el("div", { class: "plc-tree__line" + (isVar ? " is-var" : ""), role: "treeitem", title: it.node + (it.type ? " · " + it.type : "") + (it.status ? " · " + it.status : ""),
        style: "padding-left:" + (depth * 14 + 2) + "px" },
        caret, el("span", { class: "plc-tree__name", text: it.name }),
        isVar ? el("span", { class: "plc-tree__val" + (it.status ? " is-bad" : ""), text: it.status ? "нет значения" : fmt(it.value) }) : null,
        isVar && it.type ? el("span", { class: "plc-tree__type", text: it.type }) : null);
      if (isVar) line.addEventListener("click", () => assign(it));
      else line.addEventListener("click", () => caret.click());
      return el("div", { class: "plc-tree__node" }, line, kids);
    }
    function assign(it) {
      const f = S.active;
      if (!f || !rowNode[f]) return;
      rowNode[f].value = it.node;
      S.test[f] = typeof it.value === "number" ? it.value / (Number(rowScaleOp[f].value) || 1) : null;
      paintValues();
      say("«" + it.name + "» → " + (LABELS[f] || f) + ". Не забудь «Сохранить».", "ok");
      const fields = (S.cfg && S.cfg.fields) || [];
      const next = fields.slice(fields.indexOf(f) + 1).find((k) => !rowNode[k].value);
      if (next) setActive(next);
    }

    // ── поиск серверов ──
    async function toggleScan() {
      if (!scanBox.hidden) { scanBox.hidden = true; return; }
      scanBox.hidden = false;
      const sub = el("input", { type: "text", class: "plc-in plc-in--ip", list: "plcNets", placeholder: "192.168.1.0/24", spellcheck: "false",
        title: "Подсеть завода (не больше /22). Подсказки — сети этого ПК; можно вписать свою" });
      const nets = el("datalist", { id: "plcNets" });
      const port = el("input", { type: "number", class: "plc-in plc-in--s", value: "4840" });
      const list = el("div", { class: "plc-scan__list" });
      const go = el("button", { type: "button", class: "btn-secondary plc-btn", text: "Искать", onclick: async () => {
        go.disabled = true; list.replaceChildren(el("span", { class: "plc-hint", text: "Ищу в " + sub.value + "… (до 10 с)" }));
        const r = await get("/api/opcua/scan?subnet=" + encodeURIComponent(sub.value) + "&port=" + (Number(port.value) || 4840), 60000);
        go.disabled = false;
        if (!r || !r.ok) return list.replaceChildren(el("span", { class: "plc-hint plc-err", text: (r && r.error) || "нет ответа" }));
        list.replaceChildren(...(r.servers.length ? r.servers.map((s) => el("button", { type: "button", class: "plc-chip", text: s.url, title: "Взять этот адрес",
          onclick: () => { inp.op_endpoint.value = s.url; scanBox.hidden = true; connect(); } })) : [el("span", { class: "plc-hint", text: "Никого с открытым портом " + port.value + "." })]));
      } });
      scanBox.replaceChildren(lab("Подсеть", sub, nets), lab("Порт", port), go, list);
      const r = await get("/api/opcua/subnets");
      const found = (r && r.subnets) || [];
      nets.replaceChildren(...found.map((n) => el("option", { value: n })));
      if (found.length) sub.value = found[0];
    }

    // ── загрузка и опрос (только пока панель видна) ──
    async function load() {
      const r = await get("/api/plc/source");
      if (!r || !r.config) return say("Не удалось загрузить настройки ПЛК: " + ((r && r.error) || "нет ответа"), "err");
      S.status = r.live; S.running = r.running;
      fill(r.config);
    }
    async function poll() {
      if (root.offsetParent === null || document.hidden) return;
      const r = await get("/api/plc/source", 8000);
      if (r && r.live) { S.status = r.live; S.running = r.running; paintValues(); }
    }
    load();
    setInterval(poll, 3000);
  }

  window.PlcSource = { mount };
})();
