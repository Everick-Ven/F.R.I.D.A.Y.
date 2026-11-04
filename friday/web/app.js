const state = {
  overview: null,
  companies: [],
  drafts: [],
  signals: [],
  tasks: [],
};

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3200);
}

function esc(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const err = await res.json();
      detail = err.detail || JSON.stringify(err);
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

// Переключение вкладок
document.getElementById("main-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById(`view-${btn.dataset.tab}`).classList.add("active");
});

async function refreshAll() {
  const [ov, comp, dr, sig, tsk] = await Promise.all([
    api("/api/overview"),
    api("/api/companies"),
    api(buildDraftsUrl()),
    api("/api/signals"),
    api("/api/tasks?status=pending"),
  ]);
  state.overview = ov;
  state.companies = comp.companies || [];
  state.drafts = dr.drafts || [];
  state.signals = sig.signals || [];
  state.tasks = tsk.tasks || [];

  renderOverview();
  renderCompanies();
  renderDrafts();
  renderSignals();
  renderProviders();
  renderTasks();
}

function buildDraftsUrl() {
  const st = document.getElementById("draft-status-filter").value;
  const ch = document.getElementById("draft-channel-filter").value;
  const params = new URLSearchParams();
  if (st) params.set("status", st);
  if (ch) params.set("channel", ch);
  const qs = params.toString();
  return "/api/drafts" + (qs ? `?${qs}` : "");
}

function renderOverview() {
  const ov = state.overview;
  if (!ov) return;
  const s = ov.summary;
  const confirmedSigs = (s.signals_by_status && s.signals_by_status.confirmed) || 0;
  const pendingDrafts = (s.drafts_by_status && s.drafts_by_status.pending_review) || 0;
  const approvedDrafts = (s.drafts_by_status && s.drafts_by_status.approved) || 0;

  document.getElementById("tab-count-companies").textContent = s.companies_total || 0;
  document.getElementById("tab-count-drafts").textContent = s.drafts_total || 0;
  document.getElementById("tab-count-tasks").textContent = s.pending_agent_tasks || 0;

  const badge = document.getElementById("brain-badge");
  badge.textContent = `Brain: ${ov.brain.provider}${ov.brain.model ? " (" + ov.brain.model + ")" : ""}`;
  badge.className = "badge " + (ov.brain.ready ? "badge-ok" : "badge-warn");

  const kpis = [
    { label: "Практик в реестре", value: s.companies_total, sub: `Непрофилировано сайтов: ${s.unprofiled_companies}` },
    { label: "Контактов адвокатов / ЛПР", value: s.prospects_total, sub: `Новых: ${(s.prospects_by_status && s.prospects_by_status.new) || 0}` },
    { label: "Подтверждено сигналов", value: confirmedSigs, sub: `Фактов-наблюдений в БД: ${s.observations_total}` },
    { label: "Черновиков к ручному заходу", value: pendingDrafts + approvedDrafts, sub: `Одобрено: ${approvedDrafts} · Ждут проверки: ${pendingDrafts}` },
  ];

  document.getElementById("kpi-grid").innerHTML = kpis
    .map(
      (k) => `
    <div class="kpi-card">
      <div class="kpi-label">${esc(k.label)}</div>
      <div class="kpi-value">${esc(k.value)}</div>
      <div class="kpi-sub">${esc(k.sub)}</div>
    </div>`
    )
    .join("");

  document.getElementById("brain-details").innerHTML = `
    <div><strong>Провайдер анализа:</strong> <code>${esc(ov.brain.provider)}</code> — ${esc(ov.brain.message)}</div>
    <div><strong>Бюро:</strong> ${esc(ov.persona.company)} (${esc(ov.persona.name)})</div>
    <div><strong>Города ICP:</strong> ${esc((ov.icp.cities || []).join(", "))}</div>
    <div><strong>Специализации ICP:</strong> ${esc((ov.icp.practice_areas || []).join(", "))}</div>
    <div><strong>Правовой контур:</strong> Режим «Разведка + черновики» (автоотправка отключена по ст. 18 ФЗ-38; фильтр ФЗ-38 и ст. 17 КПЭА включён)</div>
  `;
}

function fitBadgeClass(score) {
  if (score >= 70) return "badge-ok";
  if (score >= 45) return "badge-warn";
  return "badge-neutral";
}

function renderCompanies() {
  const q = (document.getElementById("company-search").value || "").toLowerCase().trim();
  const rows = state.companies.filter((c) => {
    if (!q) return true;
    const hay = `${c.company} ${c.city} ${c.legal_form} ${c.contacts} ${c.signals} ${c.why_fit}`.toLowerCase();
    return hay.includes(q);
  });

  const tbody = document.getElementById("companies-tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">Практики пока не добавлены. Импортируйте список во вкладке «Импорт и источники» или запустите <code>friday discover</code>.</td></tr>`;
    return;
  }

  tbody.innerHTML = rows
    .map(
      (r) => `
    <tr>
      <td><span class="fit-score ${fitBadgeClass(r.fit)}">${r.fit}</span></td>
      <td>
        <strong>${esc(r.company)}</strong>
        ${r.legal_form ? `<div class="muted" style="margin:2px 0">${esc(r.legal_form)}</div>` : ""}
        ${r.website ? `<a href="${esc(r.website)}" target="_blank" rel="noopener">${esc(r.website)}</a>` : ""}
      </td>
      <td>${esc(r.city || "—")}</td>
      <td>${esc(r.contacts || "—")}</td>
      <td>
        <div>${esc(r.signals || "—")}</div>
        ${r.tech_stack ? `<div class="muted" style="margin:2px 0">Стек: ${esc(r.tech_stack)}</div>` : ""}
      </td>
      <td>
        <span class="badge badge-neutral">${esc(r.status)}</span>
        <div class="muted" style="margin:4px 0 0">${esc(r.status_note)}</div>
      </td>
    </tr>`
    )
    .join("");
}

function statusBadgeForDraft(st) {
  const map = {
    pending_review: ["Ждёт проверки · ФЗ-38/КПЭА ✓", "badge-ok"],
    approved: ["Одобрен к ручной отправке", "badge-ok"],
    sent_manually: ["Отправлено вручную", "badge-neutral"],
    gate_blocked: ["Заблокирован фильтром ФЗ-38/КПЭА", "badge-err"],
    rejected: ["Отклонён", "badge-neutral"],
  };
  return map[st] || [st, "badge-neutral"];
}

function renderDrafts() {
  const box = document.getElementById("drafts-list");
  if (!state.drafts.length) {
    box.innerHTML = `<div class="card muted">Черновиков по выбранному фильтру пока нет. Нажмите «2. Оценить и собрать черновики» на вкладке Обзор.</div>`;
    return;
  }

  box.innerHTML = state.drafts
    .map((d) => {
      const [stLabel, stCls] = statusBadgeForDraft(d.status);
      const fio = [d.last_name, d.first_name, d.middle_name].filter(Boolean).join(" ") || "Общий контакт";
      const targetContact =
        d.channel === "email"
          ? d.email || "email не указан"
          : d.prospect_telegram || d.company_telegram || d.prospect_phone || "контакт на сайте";
      const gateNotes = (d.gate_notes || []).length
        ? `<div class="muted" style="margin-bottom:8px">Заметки фильтра: ${esc(d.gate_notes.join(" · "))}</div>`
        : "";

      return `
      <div class="draft-card" data-id="${esc(d.id)}">
        <div class="draft-header">
          <div>
            <strong>${esc(d.company_name || "Практика")}</strong> (${esc(d.city || "РФ")}) ·
            <span>${esc(fio)}${d.title ? `, ${esc(d.title)}` : ""}</span>
            <div class="muted" style="margin:2px 0">
              Канал: <strong>${esc(d.channel.toUpperCase())}</strong> (${esc(targetContact)}) ·
              Сигнал: <code>${esc(d.signal_used || "ICP")}</code> · Score: ${esc(d.score)}/100
            </div>
          </div>
          <span class="badge ${stCls}">${esc(stLabel)}</span>
        </div>

        ${d.audit_summary ? `<div class="draft-dossier">${esc(d.audit_summary)}</div>` : ""}
        ${gateNotes}

        ${
          d.channel === "email"
            ? `<input type="text" class="input draft-subj" style="width:100%;margin-bottom:8px" value="${esc(d.subject)}" placeholder="Тема письма">`
            : ""
        }
        <textarea rows="5" class="draft-body-box">${esc(d.body)}</textarea>

        <div class="action-row">
          <button class="btn btn-primary btn-sm" onclick="copyDraft('${esc(d.id)}')">Скопировать текст</button>
          <button class="btn btn-outline btn-sm" onclick="saveDraftStatus('${esc(d.id)}', 'approved')">Сохранить и одобрить</button>
          <button class="btn btn-outline btn-sm" onclick="saveDraftStatus('${esc(d.id)}', 'sent_manually')">Отметить: отправлено вручную</button>
          <button class="btn btn-outline btn-sm" onclick="saveDraftStatus('${esc(d.id)}', 'rejected')">Отклонить</button>
        </div>
      </div>`;
    })
    .join("");
}

window.copyDraft = async function (id) {
  const card = document.querySelector(`.draft-card[data-id="${id}"]`);
  const text = card.querySelector(".draft-body-box").value;
  await navigator.clipboard.writeText(text);
  showToast("Текст скопирован в буфер обмена для ручной отправки");
};

window.saveDraftStatus = async function (id, status) {
  const card = document.querySelector(`.draft-card[data-id="${id}"]`);
  const body = card.querySelector(".draft-body-box").value;
  const subjEl = card.querySelector(".draft-subj");
  const subject = subjEl ? subjEl.value : undefined;
  try {
    await api(`/api/drafts/${id}`, {
      method: "POST",
      body: JSON.stringify({ status, body, subject }),
    });
    showToast("Статус черновика обновлён");
    await refreshAll();
  } catch (err) {
    showToast("Ошибка: " + err.message);
  }
};

function renderSignals() {
  const box = document.getElementById("signals-list");
  box.innerHTML = state.signals
    .map((s) => {
      const cls =
        s.status === "confirmed" ? "badge-ok" : s.status === "rejected" ? "badge-err" : "badge-warn";
      return `
      <div class="card" style="padding:14px 18px">
        <div class="draft-header" style="margin-bottom:6px">
          <div>
            <code>${esc(s.code)}</code> · <strong>${esc(s.label)}</strong>
            <span class="muted">(${esc(s.category)} / ${esc(s.cost_note)})</span>
          </div>
          <div class="action-row">
            <span class="badge ${cls}">${esc(s.status)}</span>
            <button class="btn btn-outline btn-sm" onclick="setSignal('${esc(s.code)}', 'confirmed')">Подтвердить</button>
            <button class="btn btn-outline btn-sm" onclick="setSignal('${esc(s.code)}', 'rejected')">Отклонить</button>
          </div>
        </div>
        <div class="muted" style="margin:0">${esc(s.description)}</div>
      </div>`;
    })
    .join("");
}

window.setSignal = async function (code, status) {
  await api(`/api/signals/${code}`, { method: "POST", body: JSON.stringify({ status }) });
  await refreshAll();
};

function renderProviders() {
  if (!state.overview) return;
  const box = document.getElementById("providers-list");
  box.innerHTML = (state.overview.providers || [])
    .map(
      (p) => `
    <div style="border:1px solid var(--border);border-radius:8px;padding:12px">
      <div style="display:flex;justify-content:space-between;gap:8px">
        <strong>${esc(p.label)} (<code>${esc(p.key)}</code>)</strong>
        <span class="badge ${p.configured ? "badge-ok" : "badge-warn"}">
          ${p.configured ? "Готов" : "Нужен ключ: " + p.missing_keys.join(", ")}
        </span>
      </div>
      <div class="muted" style="margin:4px 0 0">${esc(p.blurb)}</div>
    </div>`
    )
    .join("");
}

function renderTasks() {
  const box = document.getElementById("tasks-list");
  if (!state.tasks.length) {
    box.innerHTML = `<div class="muted">Очередь задач пуста. Задачи появляются здесь и в <code>data/agent_tasks/</code>, когда выбран режим <code>brain.provider: agent</code>.</div>`;
    return;
  }
  box.innerHTML = state.tasks
    .map(
      (t) => `
    <div class="draft-card">
      <div class="draft-header">
        <strong>Задача <code>${esc(t.id)}</code> · тип: <code>${esc(t.kind)}</code></strong>
        <span class="badge badge-warn">${esc(t.status)}</span>
      </div>
      <details style="margin-bottom:10px">
        <summary style="cursor:pointer;font-size:13px">Показать сгенерированный промпт с фактами</summary>
        <pre style="white-space:pre-wrap;font-size:12px;background:var(--surface-alt);padding:10px;border-radius:6px">${esc(t.prompt_text)}</pre>
      </details>
      <textarea id="task-ans-${esc(t.id)}" rows="4" class="textarea" placeholder="Вставьте JSON-ответ ИИ-агента..."></textarea>
      <button class="btn btn-primary btn-sm" onclick="submitAgentTask('${esc(t.id)}')">Применить JSON-ответ</button>
    </div>`
    )
    .join("");
}

window.submitAgentTask = async function (taskId) {
  const val = document.getElementById(`task-ans-${taskId}`).value;
  try {
    await api(`/api/tasks/${taskId}/apply`, {
      method: "POST",
      body: JSON.stringify({ response_json: val }),
    });
    showToast("Ответ ИИ-агента успешно применён");
    await refreshAll();
  } catch (err) {
    showToast("Ошибка: " + err.message);
  }
};

// События кнопок
document.getElementById("btn-confirm-free").addEventListener("click", async () => {
  const r = await api("/api/signals/bulk", { method: "POST", body: JSON.stringify({ mode: "confirm_free" }) });
  showToast(`Подтверждено бесплатных сигналов: ${r.updated}`);
  await refreshAll();
});

document.getElementById("btn-sig-free").addEventListener("click", async () => {
  await api("/api/signals/bulk", { method: "POST", body: JSON.stringify({ mode: "confirm_free" }) });
  showToast("Все бесплатные сигналы подтверждены");
  await refreshAll();
});

document.getElementById("btn-sig-all").addEventListener("click", async () => {
  await api("/api/signals/bulk", { method: "POST", body: JSON.stringify({ mode: "confirm_all" }) });
  showToast("Все сигналы словаря подтверждены");
  await refreshAll();
});

async function runPrepare(providerOverride = "") {
  const notice = document.getElementById("pipeline-result");
  notice.textContent = "Выполняется профилирование, оценка и проверка черновиков...";
  notice.classList.remove("hidden");
  try {
    const res = await api("/api/pipeline/prepare", {
      method: "POST",
      body: JSON.stringify({ min_score: 35, limit: 20, provider_override: providerOverride }),
    });
    notice.textContent =
      `Готово: профилировано сайтов — ${res.profiled_companies}, оценено контактов — ${res.scout.scored}, ` +
      `создано черновиков — ${res.writer.drafts_ready}` +
      (res.writer.queued_task_ids.length ? `, поставлено задач ИИ-агенту — ${res.writer.queued_task_ids.length}` : "");
    await refreshAll();
  } catch (err) {
    notice.textContent = "Ошибка: " + err.message;
  }
}

document.getElementById("btn-run-prepare").addEventListener("click", () => runPrepare(""));
document.getElementById("btn-run-template").addEventListener("click", () => runPrepare("template"));

document.getElementById("btn-do-import").addEventListener("click", async () => {
  const content = document.getElementById("import-textarea").value;
  const default_city = document.getElementById("import-city").value;
  const is_registry = document.getElementById("import-is-registry").checked;
  try {
    const r = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ content, default_city, is_registry }),
    });
    showToast(`Импортировано: новых практик ${r.new_companies}, контактов ${r.new_prospects}, сигналов ${r.observations_written}`);
    document.getElementById("import-textarea").value = "";
    await refreshAll();
  } catch (err) {
    showToast("Ошибка импорта: " + err.message);
  }
});

document.getElementById("company-search").addEventListener("input", renderCompanies);
document.getElementById("draft-status-filter").addEventListener("change", refreshAll);
document.getElementById("draft-channel-filter").addEventListener("change", refreshAll);

refreshAll().catch((err) => showToast("Ошибка загрузки: " + err.message));
