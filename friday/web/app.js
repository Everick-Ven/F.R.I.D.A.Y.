/* F.R.I.D.A.Y. v0.2 UI — относительные URL (/api/*), надёжная работа внутри Live Preview iframe */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let cachedPresets = [];
let hasInitializedForm = false;
let pendingTasksCount = 0;

function notify(msg, isError = false) {
  const toast = $("#toast");
  if (toast) {
    toast.textContent = msg;
    toast.style.borderColor = isError ? "var(--danger)" : "var(--primary)";
    toast.classList.remove("hidden");
    clearTimeout(notify._t);
    notify._t = setTimeout(() => toast.classList.add("hidden"), 4800);
  }
  const bar = $("#statusBar");
  if (bar) {
    bar.textContent = msg;
    bar.classList.toggle("error", Boolean(isError));
    bar.classList.remove("hidden");
  }
}

async function safeCopyText(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {
    // Внутри кросс-доменного iframe браузер может запретить Clipboard API — используем fallback
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (_) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

async function downloadEndpoint(url, filename) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  const blob = await res.blob();
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(blobUrl), 2000);
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
      if (parsed.detail) {
        txt = typeof parsed.detail === "string" ? parsed.detail : JSON.stringify(parsed.detail);
      }
    } catch (_) {}
    throw new Error(txt || `HTTP ${res.status}`);
  }
  return res.json();
}

async function withButtonBusy(btn, busyLabel, fn) {
  const prevText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    if (busyLabel) btn.textContent = busyLabel;
  }
  try {
    return await fn();
  } finally {
    if (btn) {
      btn.disabled = false;
      if (busyLabel) btn.textContent = prevText;
    }
  }
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
    pending: ["Ожидает выполнения агентом", "badge-warn"],
    completed: ["Выполнено агентом", "badge-ok"],
    cancelled: ["Отменена", "badge-danger"],
  };
  const [label, cls] = map[status] || [status, ""];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

function switchTab(target) {
  $$("#mainTabs .tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === target));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${target}`));
}

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

function fillNicheForm(cfg, forceOverwrite = false) {
  // Не затираем то, что пользователь прямо сейчас печатает при фоновом автообновлении
  if (hasInitializedForm && !forceOverwrite) return;
  hasInitializedForm = true;

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

  if ((icp.search_queries || []).length > 0 && (forceOverwrite || !$("#inpQuery").value)) {
    $("#inpQuery").value = icp.search_queries[0];
  }
  if ((icp.cities || []).length > 0 && (forceOverwrite || !$("#inpCity").value)) {
    $("#inpCity").value = icp.cities[0];
  }
}

async function loadPresets() {
  const { items } = await api("/api/niche/presets");
  cachedPresets = items || [];
  const wrap = $("#presetList");
  wrap.innerHTML = cachedPresets
    .map(
      (tpl, idx) => `
    <div class="preset-card">
      <div>
        <strong>${esc(tpl.label)}</strong>
        <span>Целевая ниша: ${esc(tpl.icp.niche)}</span>
      </div>
      <div class="btn-row">
        <button type="button" class="btn btn-sm btn-primary" data-preset-apply="${idx}">Применить сразу</button>
        <button type="button" class="btn btn-sm" data-preset-fill="${idx}">В форму</button>
      </div>
    </div>`
    )
    .join("");
}

async function loadSummary(forceFormSync = false) {
  const { summary, niche_configured, config, skills } = await api("/api/summary");
  const d = summary.drafts_by_status || {};
  pendingTasksCount = summary.pending_agent_tasks ?? 0;

  $("#tabAgentCnt").textContent = pendingTasksCount;
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
      `Целевая аудитория (ICP): ${config.icp.niche}. Можно сразу переходить на вкладку «2. Конвейер и сбор» или обновить параметры ниши ниже.`;
  } else {
    badgeWrap.classList.add("unconfigured");
    badgeTxt.textContent = "Шаг 1: настройте нишу и продукт";
    callout.classList.remove("ready");
  }

  fillNicheForm(config, forceFormSync);

  if (skills && skills.product_knowledge_md) {
    $("#productSkillPreview").textContent = skills.product_knowledge_md;
  }

  $("#kpiGrid").innerHTML = [
    ["Компаний в базе", summary.companies_total],
    ["ЛПР / Контактов", summary.prospects_total],
    ["Сигналов зафиксировано", summary.observations_total],
    ["Черновиков на проверке", d.pending_review || 0],
    ["Одобрено к отправке", d.approved || 0],
    ["Задач в очереди агента", pendingTasksCount],
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
    $("#draftsList").innerHTML = `<div class="card muted">Черновики пока отсутствуют. Соберите или загрузите компании на вкладке «2. Конвейер и сбор» и запустите подготовку черновиков.</div>`;
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
              Источник: <span class="badge">${esc(r.brain_provider || "agent")}</span>
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
          <button type="button" class="btn btn-sm btn-primary" data-copy-draft="${esc(r.id)}">Скопировать текст</button>
          <button type="button" class="btn btn-sm btn-ok" data-draft-status="${esc(r.id)}:approved">Одобрить</button>
          <button type="button" class="btn btn-sm" data-draft-status="${esc(r.id)}:sent_manually">Отметить отправленным</button>
          <button type="button" class="btn btn-sm btn-danger" data-draft-status="${esc(r.id)}:rejected">Отклонить</button>
        </div>
      </div>`;
    })
    .join("");
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
  $("#tblCompanies tbody").innerHTML = companies.length
    ? companies
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
        .join("")
    : `<tr><td colspan="6" class="muted">Компании пока не добавлены. На вкладке «2. Конвейер и сбор» запустите поиск, импортируйте CSV/JSON или загрузите 2 тестовые карточки.</td></tr>`;

  $("#cntProspects").textContent = prospects.length;
  $("#tblProspects tbody").innerHTML = prospects.length
    ? prospects
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
                <button type="button" class="btn btn-sm btn-primary" data-gen-draft="${esc(p.id)}:agent">В очередь агенту</button>
                <button type="button" class="btn btn-sm" data-gen-draft="${esc(p.id)}:template">Шаблон сразу</button>
              </div>
            </td>
          </tr>`;
        })
        .join("")
    : `<tr><td colspan="6" class="muted">ЛПР пока не найдены.</td></tr>`;
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
          <button type="button" class="btn btn-sm btn-ok" data-sig="${esc(s.code)}:confirmed">Подтвердить</button>
          <button type="button" class="btn btn-sm btn-danger" data-sig="${esc(s.code)}:rejected">Отклонить</button>
        </div>
      </td>
    </tr>`
    )
    .join("");
}

async function loadAgentTasks() {
  const st = $("#fltTaskStatus")?.value || "pending";
  const { items } = await api(`/api/agent-tasks?status=${encodeURIComponent(st)}`);
  if (!items.length) {
    $("#agentTasksList").innerHTML = `<div class="card muted">Задач с выбранным статусом нет. Когда вы ставите настройку ниши, глубокий скоринг или подготовку черновиков ИИ-агенту, они появляются здесь и автоматически обрабатываются агентом рабочей среды.</div>`;
    return;
  }

  $("#agentTasksList").innerHTML = items
    .map(
      (t) => `
    <div class="draft-card">
      <div class="draft-head">
        <div>
          <strong>${esc(t.kind_label || t.kind)}</strong>
          <span class="badge">#${esc(t.id)}</span>
          <div class="draft-meta" style="margin-top:4px">
            Объект: <strong>${esc(t.target || "—")}</strong>
          </div>
          <div class="draft-meta">
            Создана: ${esc(t.created_at || "—")}
            ${t.completed_at ? ` · Завершена: ${esc(t.completed_at)}` : ""}
          </div>
        </div>
        <div class="btn-row" style="align-items:center">
          ${statusBadge(t.status)}
          ${t.status === "pending" ? `<button type="button" class="btn btn-sm btn-ghost" data-cancel-task="${esc(t.id)}">Отменить</button>` : ""}
        </div>
      </div>
    </div>`
    )
    .join("");
}

async function refreshAll(forceFormSync = false) {
  await Promise.all([
    loadSummary(forceFormSync),
    loadDrafts(),
    loadCompaniesAndProspects(),
    loadSignals(),
    loadAgentTasks(),
  ]);
}

// ── Единое делегирование кликов для всех динамических и навигационных кнопок ──

document.addEventListener("click", async (e) => {
  const tabBtn = e.target.closest("#mainTabs .tab");
  if (tabBtn) {
    switchTab(tabBtn.dataset.tab);
    return;
  }

  const presetFillBtn = e.target.closest("[data-preset-fill]");
  if (presetFillBtn) {
    const tpl = cachedPresets[Number(presetFillBtn.dataset.presetFill)];
    if (!tpl) return;
    fillNicheForm(
      {
        persona: tpl.persona,
        product: tpl.product,
        icp: tpl.icp,
        compliance: { regulated_niche: tpl.regulated_niche || "none" },
      },
      true
    );
    $("#cardNicheForm")?.scrollIntoView({ behavior: "smooth", block: "start" });
    notify(`Шаблон «${tpl.label}» подставлен в анкету ниже — проверьте поля и нажмите «Сохранить нишу и продукт».`);
    return;
  }

  const presetApplyBtn = e.target.closest("[data-preset-apply]");
  if (presetApplyBtn) {
    const tpl = cachedPresets[Number(presetApplyBtn.dataset.presetApply)];
    if (!tpl) return;
    await withButtonBusy(presetApplyBtn, "Сохранение...", async () => {
      try {
        await api("/api/niche/save", {
          method: "POST",
          body: JSON.stringify({
            persona: tpl.persona,
            product: tpl.product,
            icp: tpl.icp,
            compliance: { regulated_niche: tpl.regulated_niche || "none" },
          }),
        });
        await refreshAll(true);
        notify(`Ниша «${tpl.label}» активирована и сохранена в friday.local.yaml.`);
      } catch (err) {
        notify(`Ошибка применения шаблона: ${err.message}`, true);
      }
    });
    return;
  }

  const copyBtn = e.target.closest("[data-copy-draft]");
  if (copyBtn) {
    const id = copyBtn.dataset.copyDraft;
    const text = $(`#draft-body-${id}`)?.textContent || "";
    const ok = await safeCopyText(text);
    if (ok) {
      const prev = copyBtn.textContent;
      copyBtn.textContent = "Скопировано ✓";
      setTimeout(() => { copyBtn.textContent = prev; }, 1800);
      notify("Текст черновика скопирован в буфер обмена для ручной отправки 1-на-1.");
    } else {
      notify("Выделите текст черновика выше и нажмите Ctrl+C / Cmd+C.", true);
    }
    return;
  }

  const draftStBtn = e.target.closest("[data-draft-status]");
  if (draftStBtn) {
    const [id, st] = draftStBtn.dataset.draftStatus.split(":");
    await withButtonBusy(draftStBtn, "...", async () => {
      try {
        await api(`/api/drafts/${id}`, { method: "PATCH", body: JSON.stringify({ status: st }) });
        await refreshAll();
        notify(`Статус черновика изменён на «${st}».`);
      } catch (err) {
        notify(`Ошибка обновления статуса: ${err.message}`, true);
      }
    });
    return;
  }

  const genDraftBtn = e.target.closest("[data-gen-draft]");
  if (genDraftBtn) {
    const [pid, mode] = genDraftBtn.dataset.genDraft.split(":");
    await withButtonBusy(genDraftBtn, "В работе...", async () => {
      try {
        const res = await api("/api/run/draft-single", {
          method: "POST",
          body: JSON.stringify({
            prospect_id: pid,
            channel: "telegram",
            template_preview: mode === "template",
          }),
        });
        await refreshAll();
        if (res.queued) {
          notify(`Задача подготовки черновика #${res.task_id} передана в очередь ИИ-агента.`);
          switchTab("agent");
        } else {
          notify("Шаблонный черновик создан — открыта вкладка «4. Черновики».");
          switchTab("drafts");
        }
      } catch (err) {
        notify(`Ошибка создания черновика: ${err.message}`, true);
      }
    });
    return;
  }

  const sigBtn = e.target.closest("[data-sig]");
  if (sigBtn) {
    const [code, status] = sigBtn.dataset.sig.split(":");
    await withButtonBusy(sigBtn, "...", async () => {
      try {
        await api(`/api/signals/${code}`, { method: "PATCH", body: JSON.stringify({ status }) });
        await loadSignals();
        notify(`Статус сигнала ${code} изменён на ${status}.`);
      } catch (err) {
        notify(`Ошибка обновления сигнала: ${err.message}`, true);
      }
    });
    return;
  }

  const cancelTaskBtn = e.target.closest("[data-cancel-task]");
  if (cancelTaskBtn) {
    const tid = cancelTaskBtn.dataset.cancelTask;
    await withButtonBusy(cancelTaskBtn, "Отмена...", async () => {
      try {
        await api(`/api/agent-tasks/${tid}`, { method: "DELETE" });
        await refreshAll();
        notify(`Задача #${tid} отменена.`);
      } catch (err) {
        notify(`Ошибка отмены задачи: ${err.message}`, true);
      }
    });
  }
});

// ── Верхняя панель (Скачать CSV / Markdown / Обновить) ──

$("#btnRefresh").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Обновление...", async () => {
    await refreshAll();
    notify("Данные панели обновлены.");
  })
);

$("#btnExportCsv").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Скачивание...", async () => {
    try {
      await downloadEndpoint("/api/export/csv", "friday_drafts_ru.csv");
      notify("Файл friday_drafts_ru.csv скачан.");
    } catch (err) {
      notify(`Ошибка скачивания CSV: ${err.message}`, true);
    }
  })
);

$("#btnExportMd").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Скачивание...", async () => {
    try {
      await downloadEndpoint("/api/export/markdown", "friday_drafts_ru.md");
      notify("Пакет friday_drafts_ru.md скачан.");
    } catch (err) {
      notify(`Ошибка скачивания Markdown: ${err.message}`, true);
    }
  })
);

// ── Шаг 1: Сохранение ниши и отправка на обучение ИИ-агенту ──

$("#formSaveNiche").addEventListener("submit", async (e) => {
  e.preventDefault();
  const bizName = $("#nsBizName").value.trim();
  const prodName = $("#nsProdName").value.trim();
  const prodDesc = $("#nsProdDesc").value.trim();
  const prodEntry = $("#nsProdEntry").value.trim();
  const icpNiche = $("#nsIcpNiche").value.trim();
  const queries = splitLinesOrComma($("#nsIcpQueries").value);

  if (!bizName || !prodName || !icpNiche || queries.length === 0) {
    notify("Заполните название компании, продукт, целевую нишу (ICP) и хотя бы 1 поисковый запрос.", true);
    return;
  }

  const payload = {
    persona: {
      company: bizName,
      website: $("#nsBizWebsite").value.trim(),
      name: $("#nsSenderName").value.trim() || "Руководитель направления",
      role: $("#nsSenderTitle").value.trim() || "Руководитель проектов",
      telegram: $("#nsSenderTg").value.trim(),
    },
    product: {
      name: prodName,
      description: prodDesc || prodName,
      key_benefits: splitLinesOrComma($("#nsProdBenefits").value),
      pricing: $("#nsProdPricing").value.trim(),
      offer: {
        entry: prodEntry || "прислать короткий разбор из 3 пунктов",
        goal: "send_audit",
      },
    },
    icp: {
      niche: icpNiche,
      industries: splitComma($("#nsIcpIndustries").value),
      search_queries: queries,
      cities: splitComma($("#nsIcpCities").value),
      decision_maker_titles: splitComma($("#nsIcpTitles").value),
      hiring_signals: splitComma($("#nsIcpHiring").value),
    },
    compliance: {
      regulated_niche: $("#nsRegulated").value,
    },
  };

  await withButtonBusy($("#btnSubmitSaveNiche"), "Сохранение...", async () => {
    try {
      await api("/api/niche/save", { method: "POST", body: JSON.stringify(payload) });
      await refreshAll(true);
      notify(`Ниша «${icpNiche}» и продукт сохранены в friday.local.yaml и skills/product_knowledge.md.`);
    } catch (err) {
      notify(`Ошибка сохранения: ${err.message}`, true);
    }
  });
});

$("#formTrainAgent").addEventListener("submit", async (e) => {
  e.preventDefault();
  const website = $("#trWebsite").value.trim();
  const nicheHint = $("#trNiche").value.trim();
  const brief = $("#trBrief").value.trim();
  if (!website && !nicheHint && !brief) {
    notify("Укажите сайт продукта или краткое описание ниши и вашего продукта.", true);
    return;
  }

  await withButtonBusy($("#btnSubmitTrain"), "Постановка задачи...", async () => {
    try {
      const res = await api("/api/niche/train", {
        method: "POST",
        body: JSON.stringify({
          website,
          niche_hint: nicheHint,
          brief,
          cities: splitComma($("#trCities").value),
        }),
      });
      await refreshAll();
      switchTab("agent");
      notify(`Задача настройки ниши #${res.task_id} поставлена в очередь ИИ-агента — напишите в чат «Выполни задачу F.R.I.D.A.Y.», и агент сразу применит настройки.`);
    } catch (err) {
      notify(`Ошибка постановки задачи: ${err.message}`, true);
    }
  });
});

// ── Фильтры и кнопки Конвейера (вкладка 2) ──

$("#fltDraftStatus").addEventListener("change", loadDrafts);
$("#fltDraftChannel").addEventListener("change", loadDrafts);
$("#fltTaskStatus").addEventListener("change", loadAgentTasks);

$("#btnApplyCohort").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Фильтрация...", async () => {
    await loadCompaniesAndProspects();
    notify("Фильтр когорты применён.");
  })
);
$("#btnResetCohort").addEventListener("click", () => {
  $("#inpCohortRequire").value = "";
  $("#inpCohortExclude").value = "";
  loadCompaniesAndProspects();
  notify("Фильтр когорты сброшен.");
});

$("#btnSeedSample").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Загрузка...", async () => {
    try {
      const res = await api("/api/run/sample", { method: "POST" });
      await refreshAll();
      notify(`Добавлено компаний: ${res.inserted_companies}, ЛПР: ${res.inserted_prospects}, сигналов: ${res.recorded_observations}.`);
    } catch (err) {
      notify(`Ошибка загрузки тестовых карточек: ${err.message}`, true);
    }
  })
);

$("#btnRunProfile").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Проверка сайтов...", async () => {
    try {
      const res = await api("/api/run/profile", { method: "POST" });
      await refreshAll();
      if (!res.checked) {
        notify("Нет непроверенных сайтов в базе — сначала найдите компании, загрузите CSV или добавьте 2 тестовые карточки.");
      } else {
        notify(`Проверено сайтов: ${res.checked} (успешно: ${res.ok}, новых сигналов: ${res.observations}).`);
      }
    } catch (err) {
      notify(`Ошибка профилирования: ${err.message}`, true);
    }
  })
);

$("#btnRunProspects").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Поиск ЛПР...", async () => {
    try {
      const res = await api("/api/run/prospects", { method: "POST" });
      await refreshAll();
      if (!res.companies_scanned) {
        notify("Нет новых компаний для обхода страниц руководства.");
      } else {
        notify(`Проверено компаний: ${res.companies_scanned}, найдено новых ЛПР: ${res.prospects_created}.`);
      }
    } catch (err) {
      notify(`Ошибка поиска ЛПР: ${err.message}`, true);
    }
  })
);

$("#btnRunScore").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Расчёт баллов...", async () => {
    try {
      const res = await api("/api/run/score", { method: "POST" });
      await refreshAll();
      if (!res.scored) {
        notify("В базе пока нет ЛПР для скоринга — сначала добавьте компании или 2 тестовые карточки.");
      } else {
        notify(`Рассчитан балл соответствия ICP для ${res.scored} ЛПР.`);
      }
    } catch (err) {
      notify(`Ошибка скоринга: ${err.message}`, true);
    }
  })
);

$("#btnRunScoreAi").addEventListener("click", (e) =>
  withButtonBusy(e.currentTarget, "Постановка...", async () => {
    try {
      const res = await api("/api/run/score?use_ai=true", { method: "POST" });
      await refreshAll();
      if (!res.queued_agent_tasks) {
        notify("В базе пока нет ЛПР для постановки задач скоринга.");
      } else {
        switchTab("agent");
        notify(`В очередь ИИ-агента поставлено задач скоринга: ${res.queued_agent_tasks}.`);
      }
    } catch (err) {
      notify(`Ошибка: ${err.message}`, true);
    }
  })
);

async function triggerBatchDrafts(btn, channel, templatePreview) {
  await withButtonBusy(btn, "Подготовка...", async () => {
    try {
      const res = await api("/api/run/drafts", {
        method: "POST",
        body: JSON.stringify({ channel, template_preview: templatePreview }),
      });
      await refreshAll();
      if (!res.processed) {
        notify("Нет подходящих ЛПР с баллом выше порога ICP — сначала добавьте компании и запустите скоринг.");
      } else if (templatePreview) {
        switchTab("drafts");
        notify(`Создано шаблонных черновиков (${channel}): ${res.drafts_ready} (заблокировано ФЗ-38: ${res.gate_blocked}).`);
      } else {
        switchTab("agent");
        notify(`В очередь ИИ-агента передано задач на черновики (${channel}): ${res.queued_agent_tasks}.`);
      }
    } catch (err) {
      notify(`Ошибка подготовки черновиков: ${err.message}`, true);
    }
  });
}

$("#btnQueueDraftsTg").addEventListener("click", (e) => triggerBatchDrafts(e.currentTarget, "telegram", false));
$("#btnQueueDraftsEmail").addEventListener("click", (e) => triggerBatchDrafts(e.currentTarget, "email", false));
$("#btnPreviewTemplateTg").addEventListener("click", (e) => triggerBatchDrafts(e.currentTarget, "telegram", true));

$("#formDiscover").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#inpQuery").value.trim();
  const c = $("#inpCity").value.trim();
  if (!q) {
    notify("Введите поисковый запрос или сначала сохраните нишу на вкладке «1. Ниша и продукт».", true);
    return;
  }
  await withButtonBusy($("#btnSubmitDiscover"), "Поиск...", async () => {
    try {
      const res = await api("/api/run/discover", {
        method: "POST",
        body: JSON.stringify({
          query: q,
          city: c || "Москва",
          provider: $("#selProvider").value,
          limit: Number($("#inpLimit").value || 15),
        }),
      });
      await refreshAll();
      if (!res.inserted_companies && !res.updated_companies) {
        notify(
          "Внешний поиск вернул 0 карточек (в облачном контейнере доступ к внешним сайтам ограничен — используйте импорт CSV/JSON/TXT или кнопку «Загрузить 2 тестовые карточки»)."
        );
      } else {
        notify(`Поиск завершён: новых компаний +${res.inserted_companies}, обновлено ${res.updated_companies}.`);
      }
    } catch (err) {
      notify(`Ошибка поиска: ${err.message}`, true);
    }
  });
});

$("#formUpload").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fileEl = $("#inpFile");
  if (!fileEl.files || !fileEl.files[0]) {
    notify("Сначала выберите файл .csv, .json или .txt для импорта.", true);
    return;
  }
  const fd = new FormData();
  fd.append("file", fileEl.files[0]);
  fd.append("city", $("#inpCity").value.trim());
  await withButtonBusy($("#btnSubmitUpload"), "Импорт...", async () => {
    try {
      const res = await api("/api/import/upload", { method: "POST", body: fd });
      fileEl.value = "";
      await refreshAll();
      notify(`Импортировано из ${res.filename}: +${res.inserted_companies} компаний, +${res.inserted_prospects} ЛПР.`);
    } catch (err) {
      notify(`Ошибка импорта: ${err.message}`, true);
    }
  });
});

// Фоновое автообновление очереди (раз в 4 сек, когда есть ожидающие задачи ИИ-агента)
setInterval(() => {
  if (pendingTasksCount > 0 && !document.hidden) {
    Promise.all([loadSummary(false), loadAgentTasks(), loadDrafts()]).catch(() => {});
  }
}, 4000);

Promise.all([loadPresets(), refreshAll(true)]).catch((e) => notify(`Ошибка загрузки: ${e.message}`, true));
