/* F.R.I.D.A.Y. Web Client — относительные пути /api/* (совместимо с iframe Live Preview) */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let NICHE_PRESETS = [];
let CURRENT_DRAFT_STATUS = "";
let ACTIVE_SUBVIEW = "companies";
let JOB_HIDE_TIMER = null;
const DRAFT_CACHE = new Map();

const STATUS_LABELS = {
  pending_review: { text: "На проверке", cls: "warn" },
  approved: { text: "Одобрен", cls: "ok" },
  sent_manually: { text: "Отправлен", cls: "ok" },
  rejected: { text: "Отклонён", cls: "" },
  gate_blocked: { text: "Требует правки", cls: "err" },
};

const SOURCE_LABELS = {
  hh_ru: "HeadHunter",
  yandex_search: "Яндекс",
  twogis: "2ГИС",
  yandex_maps: "Яндекс Карты",
  web_search: "Веб-поиск",
  osm: "Карты",
  import_file: "Импорт",
  website_team: "Сайт компании",
};

const SIGNAL_SHORT_LABELS = {
  HH_HIRING_ROLE: "Вакансия hh.ru",
  YANDEX_DIRECT_ACTIVE: "Яндекс Директ",
  NO_ONLINE_CAPTURE: "Нет захвата заявок",
  NO_YANDEX_METRIKA: "Без Метрики",
  YANDEX_PAGE2_RANK: "Топ-20 Яндекса",
  THIN_SERVICE_STRUCTURE: "Узкая структура сайта",
  TELEGRAM_PUBLIC: "Telegram",
  WHATSAPP_PUBLIC: "WhatsApp",
  TECH_STACK_RU: "CMS / Стек",
  LOW_MAP_RATING: "Рейтинг на картах",
  NO_WEBSITE_ON_MAP: "Нет сайта в карточке",
};

const CATEGORY_LABELS = {
  hiring: "Найм",
  marketing: "Маркетинг",
  website: "Сайт и конверсия",
  seo: "Поиск",
  maps: "Геосервисы",
  contacts: "Контакты",
  tech: "Технологии",
  company: "Профиль",
};

function showToast(msg, isError = false) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = msg;
  el.style.borderColor = isError ? "rgba(239, 68, 68, 0.5)" : "rgba(255, 255, 255, 0.16)";
  el.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add("hidden"), 3400);
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function safeCopyText(text) {
  const value = String(text ?? "");
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_err) {
      // Fallback для песочницы iframe
    }
  }
  const ta = document.createElement("textarea");
  ta.value = value;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "-9999px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (_err) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

async function downloadExport(url, filename, mimeType) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Ошибка ${res.status}`);
    const text = await res.text();
    const blob = new Blob([text], { type: mimeType });
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1500);
    showToast(`Файл ${filename} сохранён`);
  } catch (e) {
    showToast(e.message, true);
  }
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    throw new Error(data.detail || `Ошибка запроса (${res.status})`);
  }
  return data;
}

/* ── Живой индикатор прогресса ── */
function renderJobCard(job) {
  const card = $("#live-job-card");
  if (!card) return;
  clearTimeout(JOB_HIDE_TIMER);
  card.classList.remove("hidden", "done", "failed");
  if (job.status === "completed") card.classList.add("done");
  if (job.status === "failed") card.classList.add("failed");

  $("#job-title").textContent = job.title || "Выполнение";
  $("#job-stage").textContent = job.stage || "";
  $("#job-percent").textContent = `${job.percent ?? 0}%`;
  $("#job-bar-fill").style.width = `${job.percent ?? 0}%`;

  const stepsWrap = $("#job-steps");
  stepsWrap.innerHTML = (job.steps || [])
    .map(
      (s) => `
      <span class="job-step-item ${esc(s.state)}">
        <span class="step-dot"></span>
        <span>${esc(s.label)}</span>
      </span>`
    )
    .join("");
}

async function runTrackedJob(path, payload, { button = null, onComplete = null } = {}) {
  if (button) button.disabled = true;
  try {
    let job = await api(path, {
      method: "POST",
      body: payload !== undefined ? JSON.stringify(payload) : "{}",
    });
    renderJobCard(job);

    while (job.status === "running") {
      await new Promise((r) => setTimeout(r, 220));
      job = await api(`/api/jobs/${encodeURIComponent(job.id)}`);
      renderJobCard(job);
    }

    if (job.status === "failed") {
      showToast(job.error || "Не удалось выполнить операцию", true);
      return null;
    }

    await loadSummary(true);
    if (onComplete) await onComplete(job);

    JOB_HIDE_TIMER = setTimeout(() => {
      const card = $("#live-job-card");
      if (card) card.classList.add("hidden");
    }, 2600);
    return job;
  } catch (e) {
    showToast(e.message, true);
    const card = $("#live-job-card");
    if (card) card.classList.add("hidden");
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

/* ── Переключение разделов ── */
function switchTab(tabId) {
  $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabId));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `tab-${tabId}`));
  if (tabId === "workspace") {
    loadCompanies();
    loadProspects();
  }
  if (tabId === "drafts") loadDrafts();
  if (tabId === "signals") loadSignals();
}

function switchSubview(sub) {
  ACTIVE_SUBVIEW = sub;
  $$("#db-subtabs .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.subview === sub));
  $("#subview-companies").classList.toggle("hidden", sub !== "companies");
  $("#subview-prospects").classList.toggle("hidden", sub !== "prospects");
  $("#cohort-controls").classList.toggle("hidden", sub !== "companies");
}

/* ── Загрузка профиля и сводки ── */
function fillNicheFormFromConfig(cfg, skills = {}) {
  if (!cfg) return;
  const p = cfg.persona || {};
  const prod = cfg.product || {};
  const icp = cfg.icp || {};
  const comp = cfg.compliance || {};

  const cleanVal = (v, placeholder) => (!v || v === placeholder ? "" : v);

  $("#cfg-persona-company").value = cleanVal(p.company, "Ваша компания");
  $("#cfg-persona-name").value = cleanVal(p.name, "Не задано");
  $("#cfg-persona-role").value = p.role || "";
  $("#cfg-persona-telegram").value = p.telegram || "";
  $("#cfg-persona-website").value = p.website || "";

  $("#cfg-product-name").value = cleanVal(prod.name, "Не настроен");
  $("#cfg-product-desc").value = prod.description || "";
  $("#cfg-product-pricing").value = prod.pricing || "";
  $("#cfg-product-benefits").value = (prod.key_benefits || []).join("\n");
  $("#cfg-offer-entry").value = (prod.offer && prod.offer.entry) || "";

  $("#cfg-icp-niche").value = cleanVal(icp.niche, "Не настроена");
  $("#cfg-icp-industries").value = (icp.industries || []).join(", ");
  $("#cfg-icp-queries").value = (icp.search_queries || []).join("\n");
  $("#cfg-icp-cities").value = (icp.cities || []).join(", ");
  $("#cfg-icp-titles").value = (icp.decision_maker_titles || []).join(", ");
  $("#cfg-icp-hiring").value = (icp.hiring_signals || []).join(", ");
  $("#cfg-regulated").value = comp.regulated_niche || "none";

  if (skills && typeof skills.product_knowledge_md === "string") {
    $("#cfg-skill-md").value = skills.product_knowledge_md;
  }

  // Синхронизируем поля быстрого старта, если они ещё пустые
  if (!$("#train-website").value && p.website) $("#train-website").value = p.website;
  if (!$("#train-niche-hint").value && cleanVal(icp.niche, "Не настроена")) {
    $("#train-niche-hint").value = icp.niche;
  }
  if (!$("#train-brief").value && prod.description) $("#train-brief").value = prod.description;
  if (!$("#train-cities").value && (icp.cities || []).length) {
    $("#train-cities").value = icp.cities.join(", ");
  }

  if (!$("#inp-query").value && (icp.search_queries || []).length) {
    $("#inp-query").value = icp.search_queries[0];
  }
  if (!$("#inp-city").value && (icp.cities || []).length) {
    $("#inp-city").value = icp.cities[0];
  }
}

async function loadSummary(forceFormFill = false) {
  const data = await api("/api/summary");
  const s = data.summary || {};

  $("#kpi-companies").textContent = s.companies_total ?? 0;
  $("#kpi-prospects").textContent = s.prospects_total ?? 0;
  $("#kpi-observations").textContent = s.observations_total ?? 0;
  $("#kpi-drafts").textContent = s.drafts_total ?? 0;

  $("#badge-companies").textContent = s.companies_total ?? 0;
  $("#badge-drafts").textContent = s.drafts_total ?? 0;

  const dot = $("#project-status-dot");
  const txt = $("#project-status-text");
  if (data.niche_configured) {
    dot.classList.add("ready");
    const compName = data.config?.persona?.company || "Проект";
    const nicheName = data.config?.icp?.niche || "";
    txt.textContent = nicheName ? `${compName} · ${nicheName}` : compName;
  } else {
    dot.classList.remove("ready");
    txt.textContent = "Новый проект";
  }

  if (forceFormFill) {
    fillNicheFormFromConfig(data.config, data.skills);
  }
  return data;
}

async function loadNichePresets() {
  const data = await api("/api/niche/presets");
  NICHE_PRESETS = data.items || [];
  const bar = $("#niche-presets-bar");
  bar.innerHTML = NICHE_PRESETS.map(
    (p, idx) =>
      `<button type="button" class="preset-chip" data-preset-idx="${idx}">${esc(p.short_label || p.label)}</button>`
  ).join("");
}

function splitLines(val) {
  return String(val || "")
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function splitCsv(val) {
  return String(val || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

async function saveNicheManual() {
  const btn = $("#btn-save-niche");
  btn.disabled = true;
  try {
    const payload = {
      persona: {
        name: $("#cfg-persona-name").value.trim(),
        company: $("#cfg-persona-company").value.trim(),
        role: $("#cfg-persona-role").value.trim(),
        telegram: $("#cfg-persona-telegram").value.trim(),
        website: $("#cfg-persona-website").value.trim(),
      },
      product: {
        name: $("#cfg-product-name").value.trim(),
        description: $("#cfg-product-desc").value.trim(),
        pricing: $("#cfg-product-pricing").value.trim(),
        key_benefits: splitLines($("#cfg-product-benefits").value),
        offer: {
          entry: $("#cfg-offer-entry").value.trim(),
          goal: "send_audit",
        },
      },
      icp: {
        niche: $("#cfg-icp-niche").value.trim(),
        industries: splitCsv($("#cfg-icp-industries").value),
        search_queries: splitLines($("#cfg-icp-queries").value),
        cities: splitCsv($("#cfg-icp-cities").value),
        decision_maker_titles: splitCsv($("#cfg-icp-titles").value),
        hiring_signals: splitCsv($("#cfg-icp-hiring").value),
      },
      compliance: {
        regulated_niche: $("#cfg-regulated").value,
      },
      product_knowledge_md: $("#cfg-skill-md").value,
    };
    await api("/api/niche/save", { method: "POST", body: JSON.stringify(payload) });
    await loadSummary(true);
    showToast("Изменения профиля сохранены");
  } catch (e) {
    showToast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

/* ── Таблицы базы и сигналов ── */
async function loadCompanies() {
  const req = ($("#inp-require-codes")?.value || "").trim();
  const qs = new URLSearchParams();
  if (req) qs.set("require", req);
  const data = await api(`/api/companies?${qs.toString()}`);
  const tbody = $("#companies-tbody");

  if (!data.items.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-box">Список компаний пока пуст. Запустите поиск по запросу, импортируйте файл или добавьте демо-данные.</td></tr>`;
    return;
  }

  tbody.innerHTML = data.items
    .map((c) => {
      const obsHtml = (c.observations || [])
        .map((o) => {
          const label = SIGNAL_SHORT_LABELS[o.signal_code] || o.signal_code;
          const tip = o.value_text ? `${o.signal_code}: ${o.value_text}` : o.signal_code;
          return `<span class="tag" title="${esc(tip)}">${esc(label)}</span>`;
        })
        .join("");
      const contacts = [
        c.phone ? esc(c.phone) : "",
        c.telegram ? `<a class="contact-link" href="${esc(c.telegram)}" target="_blank" rel="noopener">Telegram</a>` : "",
        c.whatsapp ? `<a class="contact-link" href="${esc(c.whatsapp)}" target="_blank" rel="noopener">WhatsApp</a>` : "",
      ]
        .filter(Boolean)
        .join(" · ");

      const siteUrl = c.website || (c.domain ? `https://${c.domain}` : "");
      return `
        <tr>
          <td>
            <div class="entity-name">${esc(c.name)}</div>
            <div class="entity-sub">
              ${siteUrl ? `<a href="${esc(siteUrl)}" target="_blank" rel="noopener">${esc(c.domain || c.website)}</a>` : "—"}
              ${c.inn ? ` · ИНН ${esc(c.inn)}` : ""}
            </div>
          </td>
          <td>${esc(c.city || "—")}</td>
          <td>${contacts || "—"}</td>
          <td>${obsHtml || '<span class="entity-sub">—</span>'}</td>
          <td><span class="entity-sub">${esc(SOURCE_LABELS[c.source] || c.source || "—")}</span></td>
        </tr>`;
    })
    .join("");
}

async function loadProspects() {
  const data = await api("/api/prospects");
  const tbody = $("#prospects-tbody");

  if (!data.items.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-box">Контакты руководителей появятся после поиска или обогащения компаний.</td></tr>`;
    return;
  }

  tbody.innerHTML = data.items
    .map((p) => {
      const channels = [
        p.telegram ? `<a class="contact-link" href="${esc(p.telegram)}" target="_blank" rel="noopener">Telegram</a>` : "",
        p.email ? esc(p.email) : "",
        p.phone ? esc(p.phone) : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const scoreCls = p.fit_score >= 65 ? "score-pill high" : "score-pill";
      return `
        <tr>
          <td>
            <div class="entity-name">${esc(p.full_name)}</div>
            <div class="entity-sub">${esc(p.city || "")}</div>
          </td>
          <td>${esc(p.title || "Руководитель")}</td>
          <td>${esc(p.company || "—")}</td>
          <td>${channels || "—"}</td>
          <td><span class="${scoreCls}" title="${esc(p.score_reason || "")}">${p.fit_score}</span></td>
          <td style="text-align:right">
            <button type="button" class="ghost-btn sm" data-single-draft="${esc(p.id)}">Написать</button>
          </td>
        </tr>`;
    })
    .join("");
}

async function loadDrafts() {
  const ch = $("#flt-draft-channel").value;
  const qs = new URLSearchParams();
  if (CURRENT_DRAFT_STATUS) qs.set("status", CURRENT_DRAFT_STATUS);
  if (ch) qs.set("channel", ch);

  const data = await api(`/api/drafts?${qs.toString()}`);
  const box = $("#drafts-list");
  DRAFT_CACHE.clear();

  if (!data.items.length) {
    box.innerHTML = `<div class="empty-box">Черновиков по выбранному фильтру пока нет. Перейдите в «Поиск и база» и нажмите «Создать черновики».</div>`;
    return;
  }

  box.innerHTML = data.items
    .map((d) => {
      DRAFT_CACHE.set(String(d.id), (d.subject ? `Тема: ${d.subject}\n\n` : "") + (d.body || ""));
      const fio = [d.last_name, d.first_name, d.middle_name].filter(Boolean).join(" ") || "Руководитель";
      const st = STATUS_LABELS[d.status] || { text: d.status, cls: "" };
      const channelName =
        d.channel === "telegram" ? "Telegram" : d.channel === "whatsapp" ? "WhatsApp" : "Email";

      const contacts = [
        d.telegram ? `<a class="contact-link" href="${esc(d.telegram)}" target="_blank" rel="noopener">Telegram</a>` : "",
        d.email ? esc(d.email) : "",
        d.phone ? esc(d.phone) : "",
        d.website ? `<a class="contact-link" href="${esc(d.website)}" target="_blank" rel="noopener">Сайт</a>` : "",
      ]
        .filter(Boolean)
        .join(" · ");

      return `
        <article class="draft-item">
          <div class="draft-top">
            <div>
              <div class="draft-company">${esc(d.company_name || "Компания")}</div>
              <div class="draft-person">${esc(fio)}${d.title ? ` · ${esc(d.title)}` : ""}${d.city ? ` · ${esc(d.city)}` : ""}</div>
            </div>
            <div class="draft-badges">
              <span class="tag">${esc(channelName)}</span>
              ${d.fit_score ? `<span class="score-pill ${d.fit_score >= 65 ? "high" : ""}">${d.fit_score}</span>` : ""}
              <span class="tag ${st.cls}">${esc(st.text)}</span>
            </div>
          </div>

          ${d.subject ? `<div class="draft-subject">Тема: <strong style="color:var(--text)">${esc(d.subject)}</strong></div>` : ""}
          <div class="draft-body">${esc(d.body)}</div>

          ${d.audit_summary ? `<div class="draft-note">Основание: ${esc(d.audit_summary)}</div>` : ""}
          ${d.gate_notes && d.status === "gate_blocked" ? `<div class="draft-note err">Замечание проверки: ${esc(d.gate_notes)}</div>` : ""}

          <div class="draft-bottom">
            <div class="draft-contacts">${contacts}</div>
            <div class="draft-actions">
              <button type="button" class="secondary-btn sm" data-copy-draft="${esc(d.id)}">Копировать</button>
              <button type="button" class="ghost-btn sm" data-draft-id="${esc(d.id)}" data-draft-status="approved">Одобрить</button>
              <button type="button" class="ghost-btn sm" data-draft-id="${esc(d.id)}" data-draft-status="sent_manually">Отправлено</button>
              <button type="button" class="ghost-btn sm danger" data-draft-id="${esc(d.id)}" data-draft-status="rejected">Отклонить</button>
            </div>
          </div>
        </article>`;
    })
    .join("");
}

async function loadSignals() {
  const data = await api("/api/signals");
  const tbody = $("#signals-tbody");
  tbody.innerHTML = data.items
    .map((s) => {
      const isActive = s.status === "confirmed";
      const statusPill = isActive
        ? `<span class="tag ok">Активен</span>`
        : `<span class="tag">Отключён</span>`;
      const nextStatus = isActive ? "rejected" : "confirmed";
      const actionLabel = isActive ? "Отключить" : "Включить";
      const title = SIGNAL_SHORT_LABELS[s.code] || s.code;
      return `
        <tr>
          <td>
            <div class="entity-name">${esc(title)}</div>
            <div class="entity-sub">${esc(s.code)}</div>
          </td>
          <td>${esc(CATEGORY_LABELS[s.category] || s.category)}</td>
          <td>${esc(s.description)}</td>
          <td>${statusPill}</td>
          <td style="text-align:right">
            <button type="button" class="ghost-btn sm" data-sig-code="${esc(s.code)}" data-sig-next="${nextStatus}">
              ${actionLabel}
            </button>
          </td>
        </tr>`;
    })
    .join("");
}

/* ── Обработчики событий ── */
document.addEventListener("click", async (ev) => {
  const navBtn = ev.target.closest(".nav-btn");
  if (navBtn) {
    switchTab(navBtn.dataset.tab);
    return;
  }

  const pillNav = ev.target.closest("[data-nav]");
  if (pillNav) {
    switchTab(pillNav.dataset.nav);
    return;
  }

  const subBtn = ev.target.closest("[data-subview]");
  if (subBtn) {
    switchSubview(subBtn.dataset.subview);
    return;
  }

  const statusPill = ev.target.closest("#draft-status-pills .seg-btn");
  if (statusPill) {
    CURRENT_DRAFT_STATUS = statusPill.dataset.status || "";
    $$("#draft-status-pills .seg-btn").forEach((b) => b.classList.toggle("active", b === statusPill));
    loadDrafts();
    return;
  }

  const presetBtn = ev.target.closest("[data-preset-idx]");
  if (presetBtn) {
    const preset = NICHE_PRESETS[Number(presetBtn.dataset.presetIdx)];
    if (preset) {
      $("#train-website").value = preset.persona?.website || "";
      $("#train-niche-hint").value = preset.icp?.niche || "";
      $("#train-brief").value = preset.product?.description || "";
      $("#train-cities").value = (preset.icp?.cities || []).join(", ");
      fillNicheFormFromConfig({
        persona: preset.persona,
        product: preset.product,
        icp: preset.icp,
        compliance: { regulated_niche: preset.regulated_niche || "none" },
      });
      showToast(`Заполнен шаблон «${preset.short_label || preset.label}»`);
    }
    return;
  }

  const copyBtn = ev.target.closest("[data-copy-draft]");
  if (copyBtn) {
    const text = DRAFT_CACHE.get(copyBtn.dataset.copyDraft) || "";
    const ok = await safeCopyText(text);
    showToast(ok ? "Текст скопирован в буфер обмена" : "Не удалось скопировать текст", !ok);
    return;
  }

  const draftStBtn = ev.target.closest("[data-draft-status]");
  if (draftStBtn) {
    try {
      await api(`/api/drafts/${encodeURIComponent(draftStBtn.dataset.draftId)}`, {
        method: "PATCH",
        body: JSON.stringify({ status: draftStBtn.dataset.draftStatus }),
      });
      await loadDrafts();
      await loadSummary();
    } catch (e) {
      showToast(e.message, true);
    }
    return;
  }

  const singleDraftBtn = ev.target.closest("[data-single-draft]");
  if (singleDraftBtn) {
    singleDraftBtn.disabled = true;
    try {
      const ch = $("#sel-draft-channel").value || "telegram";
      const res = await api("/api/run/draft-single", {
        method: "POST",
        body: JSON.stringify({
          prospect_id: singleDraftBtn.dataset.singleDraft,
          channel: ch,
          template_preview: true,
        }),
      });
      await loadSummary();
      if (res.ok) {
        showToast("Черновик подготовлен");
        switchTab("drafts");
      } else {
        showToast("Черновик требует правки — проверьте вкладку «Черновики»", true);
        switchTab("drafts");
      }
    } catch (e) {
      showToast(e.message, true);
    } finally {
      singleDraftBtn.disabled = false;
    }
    return;
  }

  const sigBtn = ev.target.closest("[data-sig-code]");
  if (sigBtn) {
    try {
      await api(`/api/signals/${encodeURIComponent(sigBtn.dataset.sigCode)}`, {
        method: "PATCH",
        body: JSON.stringify({ status: sigBtn.dataset.sigNext }),
      });
      await loadSignals();
    } catch (e) {
      showToast(e.message, true);
    }
  }
});

/* ── Кнопки действий с живым прогрессом ── */
$("#btn-train-niche").addEventListener("click", async (ev) => {
  const payload = {
    website: $("#train-website").value.trim(),
    niche_hint: $("#train-niche-hint").value.trim(),
    brief: $("#train-brief").value.trim(),
    cities: splitCsv($("#train-cities").value),
  };
  await runTrackedJob("/api/jobs/train", payload, {
    button: ev.currentTarget,
    onComplete: async () => {
      showToast("Профиль проекта сформирован и сохранён");
    },
  });
});

$("#btn-save-niche").addEventListener("click", saveNicheManual);

$("#btn-discover").addEventListener("click", async (ev) => {
  const payload = {
    query: $("#inp-query").value.trim(),
    city: $("#inp-city").value.trim(),
    provider: $("#sel-provider").value,
    limit: 15,
  };
  await runTrackedJob("/api/jobs/discover", payload, {
    button: ev.currentTarget,
    onComplete: async () => {
      await loadCompanies();
      await loadProspects();
    },
  });
});

$("#btn-enrich").addEventListener("click", async (ev) => {
  await runTrackedJob("/api/jobs/enrich", {}, {
    button: ev.currentTarget,
    onComplete: async () => {
      await loadCompanies();
      await loadProspects();
    },
  });
});

$("#btn-sample").addEventListener("click", async (ev) => {
  await runTrackedJob("/api/jobs/sample", {}, {
    button: ev.currentTarget,
    onComplete: async () => {
      await loadCompanies();
      await loadProspects();
    },
  });
});

$("#btn-generate-drafts").addEventListener("click", async (ev) => {
  const ch = $("#sel-draft-channel").value || "telegram";
  await runTrackedJob(
    "/api/jobs/drafts",
    { channel: ch, limit: 20 },
    {
      button: ev.currentTarget,
      onComplete: async (job) => {
        const ready = job?.result?.drafts_ready ?? 0;
        showToast(`Подготовлено черновиков: ${ready}`);
        switchTab("drafts");
      },
    }
  );
});

$("#btn-trigger-upload").addEventListener("click", () => {
  $("#inp-import-file").click();
});

$("#inp-import-file").addEventListener("change", async (ev) => {
  const file = ev.target.files?.[0];
  if (!file) return;
  const btn = $("#btn-trigger-upload");
  btn.disabled = true;
  try {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("city", $("#inp-city").value.trim());
    const res = await fetch("/api/import/upload", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Ошибка загрузки");
    showToast(`Импортировано компаний: ${data.inserted_companies}`);
    await loadSummary();
    await loadCompanies();
    await loadProspects();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    ev.target.value = "";
    btn.disabled = false;
  }
});

$("#btn-filter-cohort").addEventListener("click", loadCompanies);
$("#flt-draft-channel").addEventListener("change", loadDrafts);

$("#btn-export-csv").addEventListener("click", () => {
  downloadExport("/api/export/csv", "friday_drafts.csv", "text/csv;charset=utf-8");
});
$("#btn-export-md").addEventListener("click", () => {
  downloadExport("/api/export/markdown", "friday_drafts.md", "text/markdown;charset=utf-8");
});

/* ── Инициализация ── */
(async () => {
  try {
    await loadNichePresets();
    const state = await loadSummary(true);
    if (state.niche_configured && (state.summary?.companies_total ?? 0) > 0) {
      switchTab("workspace");
    } else {
      switchTab("project");
    }
  } catch (e) {
    showToast(`Ошибка запуска: ${e.message}`, true);
  }
})();
