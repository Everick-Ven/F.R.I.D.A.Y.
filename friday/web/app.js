/* F.R.I.D.A.Y. v0.2 UI — относительные URL (/api/*), совместимо с любым Live Preview прокси */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let hasAutoSwitchedTab = false;

function showToast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add("hidden"), 4200);
}

async function api(path, opts = {}) {
  const headers = opts.body instanceof FormData
    ? (opts.headers || {})
    : { "Content-Type": "application/json", ...(opts.headers || {}) };
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    let txt = await res.text();
    try {
      const parsed = JSON.parse(txt);
      if (parsed.detail) txt = parsed.detail;
    } catch (_) {}
    throw new Error(txt || `HTTP ${res.status}`);
  }
  return res.json();
}

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function statusBadge(status) {
  const map = {
    pending_review: ["На проверке", "badge-warn"],
    approved: ["Одобрено к ручной отправке", "badge-ok"],
    sent_manually: ["Отправлено вручную", "badge-ok"],
    gate_blocked: ["Заблокировано шлюзом ФЗ-38", "badge-danger"],
    rejected: ["Отклонено", "badge-danger"],
    confirmed: ["confirmed", "badge-ok"],
    proposed: ["proposed", "badge-warn"],
    pending: ["Ожидает ИИ-агента", "badge-warn"],
    completed: ["Выполнена", "badge-ok"],
  };
  const [label, cls] = map[status] || [status, ""];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function switchTab(target) {
  $$("#mainTabs .tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === target));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${target}`));
}

$$("#mainTabs .tab").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function splitComma(val) {
  return String(val || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function splitLinesOrComma(val) {
  return String(val || "")
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function fillNicheForm(cfg) {
  const per = cfg.persona || {};
  const prod = cfg.product || {};
  const icp = cfg.icp || {};
  const comp = cfg.compliance || {};

  $("#nsBizName").value = per.company && per.company !== "Ваша компания" ? per.company : "";
  $("#nsBizWebsite").value = per.website || "";
  $("#nsProdName").value = prod.name && prod.name !== "Ваш продукт или услуга" ? prod.name : "";
  $("#nsProdDesc").value =
    prod.description && !prod.description.startsWith("Опишите, что делает") ? prod.description : "";
  $("#nsProdBenefits").value = (prod.key_benefits || []).join("\n");
  $("#nsProdEntry").value = (prod.offer && prod.offer.entry) || "";
  $("#nsProdPricing").value = prod.pricing || "";
  $("#nsRegulated").value = comp.regulated_niche || "none";

  $("#nsIcpNiche").value = icp.niche || "";
  $("#nsIcpIndustries").value = (icp.industries || []).join(", ");
  $("#nsIcpQueries").value = (icp.search_queries || []).join("\n");
  $("#nsIcpCities").value = (icp.cities || []).join(", ");
  $("#nsIcpTitles").value = (icp.decision_maker_titles || []).join(", ");
  $("#nsIcpHiring").value = (icp.hiring_signals || []).join(", ");

  if (per.name && per.name !== "Ваше имя") $("#nsSenderName").value = per.name;
  if (per.role) $("#nsSenderTitle").value = per.role;
  if (per.telegram) $("#nsSenderTg").value = per.telegram;

  if (!$("#inpQuery").value && (icp.search_queries || []).length > 0) {
    $("#inpQuery").value = icp.search_queries[0];
  }
  if (!$("#inpCity").value && (icp.cities || []).length > 0) {
    $("#inpCity").value = icp.cities[0];
  }
}

async function loadPresets() {
  const { items } = await api("/api/niche/presets");
  const wrap = $("#presetList");
  wrap.innerHTML = items.map((tpl, idx) => `
    <button type="button" class="preset-item" data-preset-idx="${idx}">
      <strong>${esc(tpl.label)}</strong>
      <span>Целевая ниша: ${esc(tpl.icp.niche)}</span>
    </button>
  `).join("");

  $$("#presetList .preset-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tpl = items[Number(btn.dataset.presetIdx)];
      fillNicheForm({
        persona: tpl.persona,
        product: tpl.product,
        icp: tpl.icp,
        compliance: { regulated_niche: tpl.regulated_niche || "none" },
      });
      showToast(`Подставлен шаблон «${tpl.label}» — нажмите «Сохранить нишу и продукт» ниже`);
    });
  });
}

async function loadSummary() {
  const { summary, niche_configured, config, skills } = await api("/api/summary");
  const d = summary.drafts_by_status || {};

  $("#tabAgentCnt").textContent = summary.pending_agent_tasks ?? 0;
  $("#tabDraftsCnt").textContent = (d.pending_review ?? 0) + (d.approved ?? 0);

  const badgeWrap = $("#headerBadges");
  const badgeTxt = $("#nicheBadgeText");
  const callout = $("#nicheCallout");

  if (niche_configured) {
    badgeWrap.classList.remove("unconfigured");
    badgeTxt.textContent = `Ниша: ${config.persona.company} → ${config.icp.niche}`;
    callout.classList.add("ready");
    $("#nicheCalloutTitle").textContent = `Активная ниша настроена: ${config.persona.company} (${config.product.name})`;
    $("#nicheCalloutDesc").textContent =
      `Целевая аудитория (ICP): ${config.icp.niche}. Вы можете в любой момент переобучить систему под другой продукт или перейти на вкладку «2. Конвейер и сбор».`;
    if (!hasAutoSwitchedTab && summary.companies_total > 0) {
      hasAutoSwitchedTab = true;
      switchTab("overview");
    }
  } else {
    badgeWrap.classList.add("unconfigured");
    badgeTxt.textContent = "Шаг 1: настройте нишу и продукт";
    callout.classList.remove("ready");
    hasAutoSwitchedTab = true;
  }

  fillNicheForm(config);

  if (skills && skills.product_knowledge_md) {
    $("#productSkillPreview").textContent = skills.product_knowledge_md;
  }

  $("#kpiGrid").innerHTML = [
    ["Компаний в базе", summary.companies_total],
    ["ЛПР / Контактов", summary.prospects_total],
    ["Сигналов зафиксировано", summary.observations_total],
    ["Черновиков на проверке", d.pending_review || 0],
    ["Одобрено к отправке", d.approved || 0],
    ["Задач ИИ-агенту (Мозг)", summary.pending_agent_tasks || 0],
  ]
    .map(
      ([label, val]) => `
      <div class="kpi">
        <div class="label">${esc(label)}</div>
        <div class="value">${esc(val)}</div>
      </div>`
    )
    .join("");
}

async function loadDrafts() {
  const st = $("#fltDraftStatus").value;
  const ch = $("#fltDraftChannel").value;
  const qs = new URLSearchParams();
  if (st) qs.set("status", st);
  if (ch) qs.set("channel", ch);
  const { items } = await api(`/api/drafts?${qs.toString()}`);

  if (!items.length) {
    $("#draftsList").innerHTML = `<div class="card muted">Черновики пока отсутствуют. Соберите компании на вкладке «2. Конвейер и сбор» и поставьте подготовку черновиков ИИ-агенту рабочей среды.</div>`;
    return;
  }

  $("#draftsList").innerHTML = items
    .map((r) => {
      const fio = [r.last_name, r.first_name, r.middle_name].filter(Boolean).join(" ") || "Руководитель / представитель";
      const tg = r.prospect_telegram || r.company_telegram || "";
      const gateNotes = (r.gate_notes || [])
        .map((n) => `<span class="badge badge-danger">${esc(n)}</span>`)
        .join("");

      return `
      <div class="draft-card">
        <div class="draft-head">
          <div>
            <strong>${esc(r.company_name || r.domain)}</strong>
            ${r.industry ? `<span class="badge">${esc(r.industry)}</span>` : ""}
            · ${esc(fio)} ${r.title ? `(${esc(r.title)})` : ""}
            <div class="draft-meta">
              Город: ${esc(r.city || "—")} · Канал: <strong>${esc(r.channel)}</strong> ·
              Балл ICP: <strong>${esc(r.score)}/100</strong> ·
              Сигнал: <span class="badge">${esc(r.signal_used || "—")}</span> ·
              Мозг: <span class="badge">${esc(r.brain_provider || "agent")}</span>
            </div>
          </div>
          <div>${statusBadge(r.status)}</div>
        </div>
        ${r.subject ? `<div class="draft-meta"><strong>Тема:</strong> ${esc(r.subject)}</div>` : ""}
        <div class="draft-body" id="draft-body-${esc(r.id)}">${esc(r.body)}</div>
        ${r.audit_summary ? `<div class="draft-meta"><strong>Шпаргалка разбора (2–3 факта):</strong> ${esc(r.audit_summary)}</div>` : ""}
        ${gateNotes ? `<div style="margin-bottom:8px">${gateNotes}</div>` : ""}
        <div class="draft-meta">
          Контакты для ручной отправки 1-на-1:
          ${tg ? `<a href="${esc(tg)}" target="_blank" rel="noopener">${esc(tg)}</a>` : "Telegram —"} ·
          Email: ${esc(r.email || "—")} · Тел.: ${esc(r.prospect_phone || "—")}
        </div>
        <div class="btn-row">
          <button class="btn btn-sm btn-primary" data-copy="${esc(r.id)}">Скопировать текст</button>
          <button class="btn btn-sm btn-ok" data-draft-status="${esc(r.id)}:approved">Одобрить</button>
          <button class="btn btn-sm" data-draft-status="${esc(r.id)}:sent_manually">Отметить отправленным вручную</button>
          <button class="btn btn-sm btn-danger" data-draft-status="${esc(r.id)}:rejected">Отклонить</button>
        </div>
      </div>`;
    })
    .join("");

  $$("[data-copy]").forEach((b) => {
    b.addEventListener("click", async () => {
      const id = b.dataset.copy;
      const text = $(`#draft-body-${id}`)?.textContent || "";
      await navigator.clipboard.writeText(text);
      showToast("Текст скопирован в буфер обмена для ручной отправки 1-на-1");
    });
  });

  $$("[data-draft-status]").forEach((b) => {
    b.addEventListener("click", async () => {
      const [id, st] = b.dataset.draftStatus.split(":");
      await api(`/api/drafts/${id}`, { method: "PATCH", body: JSON.stringify({ status: st }) });
      showToast(`Статус черновика обновлён: ${st}`);
      await refreshAll();
    });
  });
}

async function loadCompaniesAndProspects() {
  const req = $("#inpCohortRequire").value.trim();
  const exc = $("#inpCohortExclude").value.trim();
  const qs = new URLSearchParams();
  if (req) qs.set("require", req);
  if (exc) qs.set("exclude", exc);

  const [{ items: companies }, { items: prospects }] = await Promise.all([
    api(`/api/companies?${qs.toString()}`),
    api("/api/prospects"),
  ]);

  $("#cntCompanies").textContent = companies.length;
  $("#tblCompanies tbody").innerHTML = companies
    .map((c) => {
      const links = [
        c.website ? `<a href="${esc(c.website)}" target="_blank" rel="noopener">${esc(c.domain || c.website)}</a>` : "",
        c.hh_url ? `<a href="${esc(c.hh_url)}" target="_blank" rel="noopener">hh.ru</a>` : "",
        c.telegram ? `<a href="${esc(c.telegram)}" target="_blank" rel="noopener">TG</a>` : "",
        c.twogis_url ? `<a href="${esc(c.twogis_url)}" target="_blank" rel="noopener">2ГИС</a>` : "",
        c.ymaps_url ? `<a href="${esc(c.ymaps_url)}" target="_blank" rel="noopener">Я.Карты</a>` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const stack = (c.tech_stack || []).map((t) => `<span class="badge">${esc(t)}</span>`).join("") || "—";
      const sigs = (c.observations || [])
        .map((o) => `<span class="badge" title="${esc(o.value_text)}">${esc(o.signal_code)}</span>`)
        .join("") || "—";
      return `
      <tr>
        <td>
          <strong>${esc(c.name)}</strong>
          ${c.inn ? `<div class="muted">ИНН ${esc(c.inn)}</div>` : ""}
        </td>
        <td>${esc(c.industry || "—")}</td>
        <td>${esc(c.city || "—")}</td>
        <td>${links || "—"}</td>
        <td>${stack}</td>
        <td>${sigs}</td>
      </tr>`;
    })
    .join("");

  $("#cntProspects").textContent = prospects.length;
  $("#tblProspects tbody").innerHTML = prospects
    .map((p) => {
      const contacts = [
        p.telegram ? `<a href="${esc(p.telegram)}" target="_blank" rel="noopener">Telegram</a>` : "",
        p.email ? esc(p.email) : "",
        p.phone ? esc(p.phone) : "",
      ]
        .filter(Boolean)
        .join(" · ");
      return `
      <tr>
        <td><strong>${esc(p.score)}</strong></td>
        <td>
          <strong>${esc(p.full_name)}</strong>
          <div class="muted">${esc(p.title)}</div>
        </td>
        <td>
          ${esc(p.company)}
          <div class="muted">${esc(p.industry || "")} · ${esc(p.city || "")}</div>
        </td>
        <td>${contacts || "—"}</td>
        <td>${esc(p.personalization_notes || "—")}</td>
        <td>
          <div class="btn-row">
            <button class="btn btn-sm btn-primary" data-gen-draft="${esc(p.id)}:agent">ИИ-агенту</button>
            <button class="btn btn-sm" data-gen-draft="${esc(p.id)}:template">Шаблон</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");

  $$("[data-gen-draft]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [pid, mode] = btn.dataset.genDraft.split(":");
      btn.disabled = true;
      try {
        const res = await api("/api/run/draft-single", {
          method: "POST",
          body: JSON.stringify({
            prospect_id: pid,
            channel: "telegram",
            template_preview: mode === "template",
          }),
        });
        showToast(
          res.queued
            ? `Создана задача ИИ-агенту #${res.task_id} (вкладка «3. ИИ-агент»)`
            : "Шаблонный черновик создан (вкладка «4. Черновики»)"
        );
        await refreshAll();
      } catch (e) {
        showToast(`Ошибка: ${e.message}`);
      } finally {
        btn.disabled = false;
      }
    });
  });
}

async function loadSignals() {
  const { items } = await api("/api/signals");
  $("#tblSignals tbody").innerHTML = items
    .map(
      (s) => `
    <tr>
      <td><code>${esc(s.code)}</code></td>
      <td>${esc(s.category)}</td>
      <td>
        <strong>${esc(s.label)}</strong>
        <div class="muted">${esc(s.description)}</div>
      </td>
      <td>${esc(s.collector)}</td>
      <td>${statusBadge(s.status)}</td>
      <td>
        <div class="btn-row">
          <button class="btn btn-sm btn-ok" data-sig="${esc(s.code)}:confirmed">Подтвердить</button>
          <button class="btn btn-sm btn-danger" data-sig="${esc(s.code)}:rejected">Отклонить</button>
        </div>
      </td>
    </tr>`
    )
    .join("");

  $$("[data-sig]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [code, status] = btn.dataset.sig.split(":");
      await api(`/api/signals/${code}`, { method: "PATCH", body: JSON.stringify({ status }) });
      showToast(`Сигнал ${code} -> ${status}`);
      await loadSignals();
    });
  });
}

async function loadAgentTasks() {
  const { items } = await api("/api/agent-tasks?status=pending");
  if (!items.length) {
    $("#agentTasksList").innerHTML = `<div class="card muted">Очередь ожидающих задач ИИ-агента пуста. Создайте задачу обучения на вкладке «1. Ниша и продукт» или поставьте черновики на вкладке «2. Конвейер и сбор».</div>`;
    return;
  }

  $("#agentTasksList").innerHTML = items
    .map(
      (t) => `
    <div class="draft-card">
      <div class="draft-head">
        <div>
          <strong>Задача #${esc(t.id)}</strong> · <code>${esc(t.kind)}</code>
          <div class="draft-meta">CLI: <code>friday tasks show ${esc(t.id)}</code> → <code>friday tasks apply ${esc(t.id)} '&lt;json&gt;'</code></div>
        </div>
        <div>${statusBadge(t.status)}</div>
      </div>
      <div class="btn-row" style="margin-bottom:8px">
        <button class="btn btn-sm" data-copy-prompt="${esc(t.id)}">Скопировать полный промпт задачи</button>
      </div>
      <div class="draft-body" id="task-prompt-${esc(t.id)}" style="max-height:200px;overflow:auto">${esc(t.prompt_text)}</div>
      <div class="form-stack">
        <textarea rows="3" id="task-resp-${esc(t.id)}" placeholder='Вставьте JSON-ответ ИИ-агента (или попросите агента рабочей среды выполнить friday tasks apply ${esc(t.id)})'></textarea>
        <div>
          <button class="btn btn-sm btn-primary" data-complete-task="${esc(t.id)}">Применить JSON-ответ (с проверкой шлюза ФЗ-38)</button>
        </div>
      </div>
    </div>`
    )
    .join("");

  $$("[data-copy-prompt]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const tid = btn.dataset.copyPrompt;
      const txt = $(`#task-prompt-${tid}`)?.textContent || "";
      await navigator.clipboard.writeText(txt);
      showToast(`Промпт задачи #${tid} скопирован в буфер обмена`);
    });
  });

  $$("[data-complete-task]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const tid = btn.dataset.completeTask;
      const raw = $(`#task-resp-${tid}`).value.trim();
      if (!raw) return showToast("Вставьте JSON-ответ");
      try {
        await api(`/api/agent-tasks/${tid}/complete`, {
          method: "POST",
          body: JSON.stringify({ response_json: raw }),
        });
        showToast(`Задача #${tid} успешно применена!`);
        await refreshAll();
      } catch (e) {
        showToast(`Ошибка: ${e.message}`);
      }
    });
  });
}

async function refreshAll() {
  await Promise.all([
    loadSummary(),
    loadDrafts(),
    loadCompaniesAndProspects(),
    loadSignals(),
    loadAgentTasks(),
  ]);
}

// ── Привязка событий Шага 1 (Ниша и продукт) ──

$("#formSaveNiche").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    persona: {
      company: $("#nsBizName").value.trim(),
      website: $("#nsBizWebsite").value.trim(),
      name: $("#nsSenderName").value.trim(),
      role: $("#nsSenderTitle").value.trim(),
      telegram: $("#nsSenderTg").value.trim(),
    },
    product: {
      name: $("#nsProdName").value.trim(),
      description: $("#nsProdDesc").value.trim(),
      key_benefits: splitLinesOrComma($("#nsProdBenefits").value),
      pricing: $("#nsProdPricing").value.trim(),
      offer: {
        entry: $("#nsProdEntry").value.trim(),
        goal: "send_audit",
      },
    },
    icp: {
      niche: $("#nsIcpNiche").value.trim(),
      industries: splitComma($("#nsIcpIndustries").value),
      search_queries: splitLinesOrComma($("#nsIcpQueries").value),
      cities: splitComma($("#nsIcpCities").value),
      decision_maker_titles: splitComma($("#nsIcpTitles").value),
      hiring_signals: splitComma($("#nsIcpHiring").value),
    },
    compliance: {
      regulated_niche: $("#nsRegulated").value,
    },
  };
  try {
    await api("/api/niche/save", { method: "POST", body: JSON.stringify(payload) });
    showToast("Ниша и продукт сохранены в friday.local.yaml и skills/product_knowledge.md!");
    await refreshAll();
  } catch (err) {
    showToast(`Ошибка сохранения: ${err.message}`);
  }
});

$("#formTrainAgent").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    website: $("#trWebsite").value.trim(),
    niche_hint: $("#trNiche").value.trim(),
    brief: $("#trBrief").value.trim(),
    cities: splitComma($("#trCities").value),
  };
  try {
    const res = await api("/api/niche/train", { method: "POST", body: JSON.stringify(payload) });
    showToast(`Создана задача обучения #${res.task_id} (скачано страниц: ${res.pages_crawled})`);
    await refreshAll();
    switchTab("agent");
  } catch (err) {
    showToast(`Ошибка: ${err.message}`);
  }
});

// ── Привязка кнопок Конвейера и Сбора ──

$("#btnRefresh").addEventListener("click", refreshAll);
$("#fltDraftStatus").addEventListener("change", loadDrafts);
$("#fltDraftChannel").addEventListener("change", loadDrafts);

$("#btnApplyCohort").addEventListener("click", loadCompaniesAndProspects);
$("#btnResetCohort").addEventListener("click", () => {
  $("#inpCohortRequire").value = "";
  $("#inpCohortExclude").value = "";
  loadCompaniesAndProspects();
});

$("#btnCopyAgentInstruction").addEventListener("click", async () => {
  const cmd = "Посмотри ожидающие задачи F.R.I.D.A.Y. через `.venv/bin/friday tasks next`, выполни каждую строго по JSON-контракту и примени ответы через `.venv/bin/friday tasks apply <task_id> '<json>'`.";
  await navigator.clipboard.writeText(cmd);
  showToast("Инструкция для ИИ-агента рабочей среды скопирована в буфер обмена!");
});

async function runStage(btn, fn, label) {
  btn.disabled = true;
  try {
    const res = await fn();
    showToast(`${label}: ${JSON.stringify(res)}`);
    await refreshAll();
  } catch (e) {
    showToast(`Ошибка: ${e.message}`);
  } finally {
    btn.disabled = false;
  }
}

$("#btnRunProfile").addEventListener("click", (e) =>
  runStage(e.currentTarget, () => api("/api/run/profile", { method: "POST" }), "Профилирование сайтов")
);
$("#btnRunProspects").addEventListener("click", (e) =>
  runStage(e.currentTarget, () => api("/api/run/prospects", { method: "POST" }), "Поиск ЛПР")
);
$("#btnRunScore").addEventListener("click", (e) =>
  runStage(e.currentTarget, () => api("/api/run/score", { method: "POST" }), "Скоринг ICP")
);
$("#btnRunScoreAi").addEventListener("click", (e) =>
  runStage(e.currentTarget, () => api("/api/run/score?use_ai=true", { method: "POST" }), "Очередь скоринга ИИ-агенту")
);
$("#btnQueueDraftsTg").addEventListener("click", (e) =>
  runStage(
    e.currentTarget,
    () => api("/api/run/drafts", { method: "POST", body: JSON.stringify({ channel: "telegram", template_preview: false }) }),
    "Постановка черновиков Telegram ИИ-агенту"
  )
);
$("#btnQueueDraftsEmail").addEventListener("click", (e) =>
  runStage(
    e.currentTarget,
    () => api("/api/run/drafts", { method: "POST", body: JSON.stringify({ channel: "email", template_preview: false }) }),
    "Постановка черновиков Email ИИ-агенту"
  )
);
$("#btnPreviewTemplateTg").addEventListener("click", (e) =>
  runStage(
    e.currentTarget,
    () => api("/api/run/drafts", { method: "POST", body: JSON.stringify({ channel: "telegram", template_preview: true }) }),
    "Шаблонный предпросмотр Telegram"
  )
);
$("#btnSeedSample").addEventListener("click", (e) =>
  runStage(
    e.currentTarget,
    () => api("/api/run/sample", { method: "POST" }),
    "Тестовые карточки под текущую нишу"
  )
);

$("#formDiscover").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = {
    query: $("#inpQuery").value.trim(),
    city: $("#inpCity").value.trim(),
    provider: $("#selProvider").value,
    limit: Number($("#inpLimit").value || 15),
  };
  showToast("Запущен поиск компаний в РФ...");
  try {
    const res = await api("/api/run/discover", { method: "POST", body: JSON.stringify(payload) });
    showToast(`Найдено новых компаний: ${res.inserted_companies} (источники: ${(res.providers_used || []).join(", ") || "—"})`);
    await refreshAll();
  } catch (err) {
    showToast(`Ошибка поиска: ${err.message}`);
  }
});

$("#formUpload").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fileEl = $("#inpFile");
  if (!fileEl.files || !fileEl.files[0]) return showToast("Выберите файл");
  const fd = new FormData();
  fd.append("file", fileEl.files[0]);
  fd.append("city", $("#inpCity").value.trim());
  try {
    const res = await api("/api/import/upload", { method: "POST", body: fd });
    showToast(`Импортировано из ${res.filename}: +${res.inserted_companies} компаний, +${res.inserted_prospects} ЛПР`);
    fileEl.value = "";
    await refreshAll();
  } catch (err) {
    showToast(`Ошибка импорта: ${err.message}`);
  }
});

Promise.all([loadPresets(), refreshAll()]).catch((e) => showToast(`Ошибка загрузки: ${e.message}`));
