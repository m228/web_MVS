// Страница «Скорость обработки»: /api/perf → плитки по каждому месту, где обрабатывается картинка.
(function () {
  "use strict";
  const body = document.getElementById("perfBody");
  const stateEl = document.getElementById("perfState");
  const STALE_S = 60;

  const num = (v, d) => (v == null ? "—" : Number(v).toFixed(d == null ? 1 : d).replace(".", ","));
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  // подписи этапов сервиса движения: ключ вида «<камера>:<этап>»
  const CAM_STAGES = [
    ["frame", "Чтение кадра", "Забрать кадр из потока и декодировать"],
    ["shafts", "Валы (палки)", "Поиск палок по цвету: найдено / всего"],
    ["zones", "Зоны движения", "Зоны «стоит / движется» (в том числе шахта жома): активных / всего"],
    ["juice", "Сок (прозрачность)", "Мутность сока по кругам: оценено / всего"],
  ];
  const LEVEL_STAGES = [
    ["level", "Декантатор: весь круг", "Кадр → сервис уровня → уровень в процентах; получено — найдено объектов"],
    ["level_model", "Декантатор: модель", "Только расчёт модели в сервисе уровня"],
  ];
  const APP_STAGES = [
    ["microscope:cv", "Микроскоп: кристаллы", "Один кадр пробы: сайдкар + измерения; получено — найдено кристаллов"],
    ["microscope:fracture", "Микроскоп: разломы", "Поиск зон разлома на кадре; получено — найдено зон"],
  ];

  function tile(label, value, title, cls) {
    const t = el("div", "micro-tile" + (cls ? " " + cls : ""));
    t.appendChild(el("span", null, label));
    t.appendChild(el("b", null, value));
    if (title) t.title = title;
    return t;
  }

  function stageRow(name, hint, m) {
    const row = el("div", "perf-row");
    const head = el("div", "perf-row__name", name);
    head.title = hint || "";
    row.appendChild(head);
    const grid = el("div", "perf-grid");
    if (!m) {
      grid.appendChild(tile("Время", "нет данных", "Замеров пока не было: источник не работает или кадров ещё не было"));
      row.appendChild(grid);
      return row;
    }
    const stale = m.age_s != null && m.age_s > STALE_S;
    const load = m.load;
    grid.appendChild(tile("Время", num(m.avg) + " мс", "Среднее за окно; последнее " + num(m.last) + " мс"));
    grid.appendChild(tile("p95 / макс", num(m.p95, 0) + " / " + num(m.max, 0) + " мс", "95 % кадров быстрее первой цифры; вторая — самый медленный"));
    if (m.of != null) grid.appendChild(tile("Получено", String(m.got == null ? "—" : Math.round(m.got)) + " из " + Math.round(m.of)));
    else if (m.got != null) grid.appendChild(tile("Получено", String(Math.round(m.got))));
    if (load != null) {
      const c = load >= 100 ? "perf-bad" : load >= 60 ? "perf-warn" : "";
      grid.appendChild(tile("Загрузка", num(load, 1) + " %", "Среднее время / интервал между кадрами (" + num(m.budget_ms, 0) + " мс). Выше 100 % — не успеваем", c));
    }
    grid.appendChild(tile("Кадров", String(m.n), stale ? "Последний замер " + Math.round(m.age_s) + " с назад" : ""));
    if (m.note) grid.appendChild(tile("Детали", m.note, m.note));
    if (stale) row.classList.add("is-stale");
    row.appendChild(grid);
    return row;
  }

  function card(title, sub) {
    const c = el("section", "card perf-card");
    const h = el("div", "perf-card__head");
    h.appendChild(el("h2", null, title));
    if (sub) h.appendChild(el("span", "perf-card__sub", sub));
    c.appendChild(h);
    return c;
  }

  function render(d) {
    body.textContent = "";
    const mo = d.motion, pm = (mo && mo.perf) || {};
    if (!mo) {
      const c = card("Сервис движения", "не отвечает");
      c.appendChild(el("p", "perf-note", d.motion_error || "нет связи"));
      body.appendChild(c);
    } else {
      for (const cam of mo.cameras || []) {
        const c = card(cam.name, cam.proc_fps ? "анализ " + cam.proc_fps + " кадр/с" : "");
        let any = false;
        for (const [k, name, hint] of CAM_STAGES) {
          const m = pm[cam.id + ":" + k];
          if (!m && k !== "frame") continue;      // этапа в камере нет (нет валов / зон / сока) — не показываем
          c.appendChild(stageRow(name, hint, m)); any = true;
        }
        for (const g of cam.groups || []) {
          for (const [k, name, hint] of LEVEL_STAGES) c.appendChild(stageRow(name + (cam.groups.length > 1 ? " · " + g.name : ""), hint, pm[g.id + ":" + k]));
          any = true;
        }
        if (!any) c.appendChild(el("p", "perf-note", "Нет данных"));
        body.appendChild(c);
      }
    }
    const ap = d.app || {};
    const a = card("Микроскоп и разломы", "в приложении");
    for (const [k, name, hint] of APP_STAGES) a.appendChild(stageRow(name, hint, ap[k]));
    body.appendChild(a);
  }

  async function tick() {
    try {
      const r = await fetch("/api/perf", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      render(await r.json());
      stateEl.textContent = "обновлено " + new Date().toLocaleTimeString("ru-RU");
    } catch (e) {
      stateEl.textContent = "нет связи с приложением";
    }
  }
  tick();
  setInterval(tick, 2000);
})();
