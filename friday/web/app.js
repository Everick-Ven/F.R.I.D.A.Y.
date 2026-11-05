/* F.R.I.D.A.Y. Web Client — относительные /api/* + прямое получение живых данных РФ */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let NICHE_PRESETS = [];
let CURRENT_DRAFT_STATUS = "";
let ACTIVE_CONFIG = null;
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
  ddg_ru: "Веб-поиск",
  web_search: "Веб-поиск",
  osm_ru: "OpenStreetMap",
  osm: "OpenStreetMap",
  import: "Импорт",
  website_team: "Сайт компании",
};

const SIGNAL_SHORT_LABELS = {
  HH_HIRING_ROLE: "Вакансия hh.ru",
  HH_OPEN_ROLES: "Активный найм",
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
  OUTDATED_COPYRIGHT: "Устаревший сайт",
  SITE_UNREACHABLE: "Сбой сайта",
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

const HH_AREA_IDS = {
  "москва": "1",
  "санкт-петербург": "2",
  "екатеринбург": "3",
  "новосибирск": "4",
  "нижний новгород": "66",
  "казань": "88",
  "краснодар": "53",
  "самара": "78",
  "ростов-на-дону": "76",
  "челябинск": "104",
  "уфа": "99",
  "воронеж": "26",
  "пермь": "72",
  "волгоград": "24",
  "красноярск": "54",
  "владивосток": "22",
  "тюмень": "95",
  "сочи": "237",
};

const CITY_BBOX_RU = {
  "москва": [55.57, 37.37, 55.91, 37.85],
  "санкт-петербург": [59.82, 30.14, 60.06, 30.52],
  "екатеринбург": [56.75, 60.48, 56.92, 60.73],
  "новосибирск": [54.92, 82.80, 55.10, 83.08],
  "казань": [55.71, 48.97, 55.86, 49.26],
  "нижний новгород": [56.23, 43.82, 56.37, 44.10],
  "краснодар": [44.98, 38.89, 45.12, 39.11],
  "самара": [53.16, 50.06, 53.30, 50.30],
  "ростов-на-дону": [47.19, 39.60, 47.31, 39.84],
  "челябинск": [55.09, 61.30, 55.25, 61.50],
  "уфа": [54.68, 55.88, 54.84, 56.12],
  "воронеж": [51.60, 39.10, 51.75, 39.32],
  "пермь": [57.95, 56.10, 58.08, 56.38],
  "тюмень": [57.09, 65.45, 57.21, 65.66],
  "владивосток": [43.07, 131.85, 43.21, 132.01],
  "сочи": [43.54, 39.69, 43.64, 39.81],
};

const OSM_TAG_RULES = [
  { kw: ["адвокат", "юрист", "юридич", "право", "коллеги"], tags: [["office", "lawyer"]] },
  { kw: ["стоматолог", "клиник", "медицин", "косметолог", "врач"], tags: [["amenity", "dentist"], ["amenity", "clinic"]] },
  { kw: ["ресторан", "кафе", "кофейн", "пекарн", "horeca"], tags: [["amenity", "restaurant"], ["amenity", "cafe"]] },
  { kw: ["отель", "гостиниц"], tags: [["tourism", "hotel"]] },
  { kw: ["автосервис", "кузовн", "шиномонтаж"], tags: [["shop", "car_repair"]] },
  { kw: ["строй", "архитект", "ремонт", "проект"], tags: [["office", "construction_company"], ["office", "architect"]] },
  { kw: ["бухгалтер", "аудит", "налог"], tags: [["office", "accountant"], ["office", "tax_advisor"]] },
  { kw: ["логист", "перевоз", "склад", "вэд"], tags: [["office", "logistics"], ["office", "transport"]] },
  { kw: ["ит", "it", "crm", "разработк", "интегратор"], tags: [["office", "it"]] },
  { kw: ["маркетинг", "реклам", "seo", "digital"], tags: [["office", "advertising_agency"]] },
];

function showToast(msg, isError = false) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = msg;
  el.style.borderColor = isError ? "rgba(239, 68, 68, 0.5)" : "rgba(255, 255, 255, 0.16)";
  el.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add("hidden"), 3600);
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function stripHtml(html) {
  return String(html || "")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&quot;/gi, '"')
    .replace(/\s+/g, " ")
    .trim();
}

async function safeCopyText(text) {
  const value = String(text ?? "");
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_err) {
      // Fallback для iframe
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

async function fetchJsonSimple(url, timeoutMs = 7500) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    // Без кастомных заголовков — CORS simple request без preflight
    const res = await fetch(url, { method: "GET", signal: ctrl.signal });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/* ── Живой поиск реальных организаций из браузера пользователя (HeadHunter CORS API + Overpass OSM) ── */
async function resolveHhArea(city) {
  const key = String(city || "").trim().toLowerCase();
  if (!key) return "113";
  if (HH_AREA_IDS[key]) return HH_AREA_IDS[key];
  const sug = await fetchJsonSimple(
    `https://api.hh.ru/suggests/areas?text=${encodeURIComponent(city.trim())}`,
    4500
  );
  if (sug && Array.isArray(sug.items) && sug.items.length > 0) {
    return String(sug.items[0].id || "113");
  }
  return "113";
}

function isHrAgency(name) {
  const nl = String(name || "").toLowerCase();
  return (
    nl.includes("кадровое агентство") ||
    nl.includes("подбор персонала") ||
    nl.includes("рекрутинг") ||
    nl === "ancor" ||
    nl === "анкор"
  );
}

async function fetchLiveFromHeadHunter(query, city, limit = 15, onStep = null) {
  const area = await resolveHhArea(city);
  const qClean = query.trim();
  const candidates = new Map();

  // Параллельный поиск по базе работодателей и по активным вакансиям hh.ru
  const [empSearch, vacSearch] = await Promise.all([
    fetchJsonSimple(
      `https://api.hh.ru/employers?text=${encodeURIComponent(qClean)}&area=${area}&per_page=25`,
      7500
    ),
    fetchJsonSimple(
      `https://api.hh.ru/vacancies?text=${encodeURIComponent(qClean)}&area=${area}&per_page=30`,
      7500
    ),
  ]);

  if (empSearch && Array.isArray(empSearch.items)) {
    for (const emp of empSearch.items) {
      const id = String(emp.id || "");
      const name = String(emp.name || "").trim();
      if (!id || !name || isHrAgency(name)) continue;
      candidates.set(id, {
        id,
        name,
        hh_url: emp.alternate_url || `https://hh.ru/employer/${id}`,
        open_vacancies: Number(emp.open_vacancies || 0),
        vac_name: "",
        vac_url: "",
        city: city || "Россия",
        address: "",
        phone: "",
        email: "",
        contact_fio: "",
      });
    }
  }

  if (vacSearch && Array.isArray(vacSearch.items)) {
    for (const vac of vacSearch.items) {
      const emp = vac.employer || {};
      const id = String(emp.id || "");
      const name = String(emp.name || "").trim();
      if (!id || !name || isHrAgency(name)) continue;

      let phone = "";
      if (vac.contacts && Array.isArray(vac.contacts.phones) && vac.contacts.phones.length > 0) {
        const ph = vac.contacts.phones[0];
        phone = ph.formatted || `+${ph.country || "7"} (${ph.city || ""}) ${ph.number || ""}`.trim();
      }
      const existing = candidates.get(id) || {
        id,
        name,
        hh_url: emp.alternate_url || `https://hh.ru/employer/${id}`,
        open_vacancies: 1,
        city: vac.area?.name || city || "Россия",
        address: "",
        phone: "",
        email: "",
        contact_fio: "",
      };
      existing.vac_name = existing.vac_name || String(vac.name || "").trim();
      existing.vac_url = existing.vac_url || String(vac.alternate_url || "");
      existing.address = existing.address || String(vac.address?.raw || "");
      existing.phone = existing.phone || phone;
      existing.email = existing.email || String(vac.contacts?.email || "");
      existing.contact_fio = existing.contact_fio || String(vac.contacts?.name || "");
      candidates.set(id, existing);
    }
  }

  const list = Array.from(candidates.values()).slice(0, Math.min(limit + 6, 20));
  if (!list.length) return [];

  if (onStep) onStep(`Проверка официальных сайтов ${list.length} организаций в HeadHunter…`);

  // Подтягиваем карточки работодателей (там хранится проверенный официальный site_url)
  const enriched = await Promise.all(
    list.map(async (cand) => {
      const detail = await fetchJsonSimple(`https://api.hh.ru/employers/${cand.id}`, 6500);
      const siteUrl = detail?.site_url ? String(detail.site_url).trim() : "";
      const openVac = Number(detail?.open_vacancies ?? cand.open_vacancies ?? 0);
      const inds = Array.isArray(detail?.industries)
        ? detail.industries.map((i) => i.name).filter(Boolean).slice(0, 2).join(", ")
        : "";
      const desc = detail?.description ? stripHtml(detail.description).slice(0, 320) : "";

      const observations = [];
      if (cand.vac_name) {
        observations.push({
          signal_code: "HH_HIRING_ROLE",
          collector: "hh_ru",
          value_text: `Вакансия: «${cand.vac_name}»`,
          evidence_url: cand.vac_url || cand.hh_url,
        });
      }
      if (openVac > 0) {
        observations.push({
          signal_code: "HH_OPEN_ROLES",
          collector: "hh_ru",
          value_num: openVac,
          value_text: `${openVac} открытых вакансий на hh.ru`,
          evidence_url: cand.hh_url,
        });
      }

      return {
        name: cand.name,
        website: siteUrl,
        city: detail?.area?.name || cand.city,
        industry: inds || query,
        address: cand.address,
        phone: cand.phone,
        email: cand.email,
        contact_fio: cand.contact_fio,
        hh_url: cand.hh_url,
        external_id: `hh:${cand.id}`,
        source: "hh_ru",
        source_url: cand.vac_url || cand.hh_url,
        description: desc,
        observations,
      };
    })
  );

  // Сначала компании с проверенным собственным сайтом
  enriched.sort((a, b) => (b.website ? 1 : 0) - (a.website ? 1 : 0));
  return enriched.slice(0, limit);
}

async function fetchLiveFromOverpass(query, city, limit = 12) {
  const bbox = CITY_BBOX_RU[String(city || "").trim().toLowerCase()];
  if (!bbox) return [];
  const bboxStr = bbox.join(",");
  const qlLower = query.toLowerCase();

  let tagClauses = [];
  for (const rule of OSM_TAG_RULES) {
    if (rule.kw.some((k) => qlLower.includes(k))) {
      for (const [k, v] of rule.tags) {
        tagClauses.push(`nwr["${k}"="${v}"](${bboxStr});`);
      }
    }
  }
  if (!tagClauses.length) {
    const firstWord = (query.trim().split(/\s+/)[0] || "").replace(/[^\p{L}\p{N}-]/gu, "").slice(0, 22);
    if (firstWord.length >= 3) {
      tagClauses.push(`nwr["name"~"${firstWord}",i](${bboxStr});`);
    } else {
      return [];
    }
  }

  const overpassQuery = `[out:json][timeout:10];(${tagClauses.join("")});out center 35;`;
  const endpoints = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
  ];

  let data = null;
  for (const ep of endpoints) {
    data = await fetchJsonSimple(`${ep}?data=${encodeURIComponent(overpassQuery)}`, 6500);
    if (data && Array.isArray(data.elements)) break;
  }
  if (!data || !Array.isArray(data.elements)) return [];

  const items = [];
  for (const el of data.elements) {
    const tags = el.tags || {};
    const name = String(tags.name || "").trim();
    const website = String(tags.website || tags["contact:website"] || "").trim();
    const phone = String(tags.phone || tags["contact:phone"] || "").trim();
    const email = String(tags.email || tags["contact:email"] || "").trim();
    const telegram = String(tags["contact:telegram"] || tags.telegram || "").trim();
    const vk = String(tags["contact:vk"] || tags.vk || "").trim();
    if (!name || (!website && !phone)) continue;

    const street = tags["addr:street"] || "";
    const house = tags["addr:housenumber"] || "";
    const address = `${street} ${house}`.trim();
    const osmUrl = `https://www.openstreetmap.org/${el.type || "node"}/${el.id}`;
    const observations = [];
    if (telegram) {
      observations.push({
        signal_code: "TELEGRAM_PUBLIC",
        collector: "osm_ru",
        value_text: telegram,
        evidence_url: website || osmUrl,
      });
    }
    if (!website) {
      observations.push({
        signal_code: "NO_WEBSITE_ON_MAP",
        collector: "osm_ru",
        value_text: "В карточке геосервиса не указан сайт",
        evidence_url: osmUrl,
      });
    }

    items.push({
      name,
      website,
      city,
      industry: query,
      address,
      phone,
      email,
      telegram: telegram.startsWith("@") ? `https://t.me/${telegram.slice(1)}` : telegram,
      vk,
      external_id: `osm:${el.type}:${el.id}`,
      source: "osm_ru",
      source_url: website || osmUrl,
      observations,
    });
  }

  items.sort((a, b) => (b.website ? 1 : 0) - (a.website ? 1 : 0));
  return items.slice(0, limit);
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

    // Если сервер работает в изолированном облачном контейнере — загружаем живые компании напрямую через браузер
    if (job.kind === "discover" && job.result?.network_isolated) {
      const q = job.result.query;
      const c = job.result.city;
      const prov = job.result.provider || "auto";
      const lim = job.result.limit || 15;

      renderJobCard({
        title: "Поиск реальных организаций",
        status: "running",
        percent: 32,
        stage: `Запрос к открытому API HeadHunter и картам: «${q}» (${c})`,
        steps: [
          { label: `Поиск по запросу «${q}»`, state: "active" },
          { label: "Получение официальных сайтов и контактов", state: "pending" },
          { label: "Сохранение и оценка релевантности", state: "pending" },
        ],
      });

      const tasks = [];
      if (prov === "auto" || prov === "hh") {
        tasks.push(
          fetchLiveFromHeadHunter(q, c, lim, (detail) => {
            renderJobCard({
              title: "Поиск реальных организаций",
              status: "running",
              percent: 62,
              stage: detail,
              steps: [
                { label: `Поиск по запросу «${q}»`, state: "done" },
                { label: "Получение официальных сайтов и контактов", state: "active" },
                { label: "Сохранение и оценка релевантности", state: "pending" },
              ],
            });
          })
        );
      }
      if (prov !== "hh") {
        tasks.push(fetchLiveFromOverpass(q, c, Math.max(8, Math.floor(lim / 2))));
      }

      const batches = await Promise.all(tasks);
      const combined = batches.flat();

      if (!combined.length) {
        renderJobCard({
          title: "Поиск завершён",
          status: "failed",
          percent: 100,
          stage: `По запросу «${q}» (${c}) в открытых источниках не найдено карточек. Попробуйте более широкий запрос.`,
          steps: [],
        });
        showToast("Попробуйте уточнить или расширить поисковый запрос", true);
        return null;
      }

      renderJobCard({
        title: "Поиск реальных организаций",
        status: "running",
        percent: 88,
        stage: `Сохранение ${combined.length} найденных организаций…`,
        steps: [
          { label: `Поиск по запросу «${q}»`, state: "done" },
          { label: "Получение официальных сайтов и контактов", state: "done" },
          { label: "Сохранение и оценка релевантности", state: "active" },
        ],
      });

      const saved = await api("/api/import/live-batch", {
        method: "POST",
        body: JSON.stringify({ items: combined }),
      });

      const totalFound = (saved.inserted_companies || 0) + (saved.updated_companies || 0);
      renderJobCard({
        title: "Поиск завершён",
        status: "completed",
        percent: 100,
        stage: `Найдено реальных организаций: ${totalFound}`,
        steps: [
          { label: `Поиск по запросу «${q}»`, state: "done" },
          { label: "Получение официальных сайтов и контактов", state: "done" },
          { label: "Сохранение и оценка релевантности", state: "done" },
        ],
      });
    }

    await loadSummary(true);
    if (onComplete) await onComplete(job);

    JOB_HIDE_TIMER = setTimeout(() => {
      const card = $("#live-job-card");
      if (card) card.classList.add("hidden");
    }, 2800);
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
  $$("#db-subtabs .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.subview === sub));
  $("#subview-companies").classList.toggle("hidden", sub !== "companies");
  $("#subview-prospects").classList.toggle("hidden", sub !== "prospects");
  $("#cohort-controls").classList.toggle("hidden", sub !== "companies");
}

/* ── Загрузка профиля и сводки ── */
function fillNicheFormFromConfig(cfg, skills = {}) {
  if (!cfg) return;
  ACTIVE_CONFIG = cfg;
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
    tbody.innerHTML = `<tr><td colspan="5" class="empty-box">Список компаний пока пуст. Введите запрос и нажмите «Найти компании» или загрузите список из файла.</td></tr>`;
    return;
  }

  tbody.innerHTML = data.items
    .map((c) => {
      const obsHtml = (c.observations || [])
        .map((o) => {
          const label = SIGNAL_SHORT_LABELS[o.signal_code] || o.signal_code;
          const tip = o.value_text ? `${o.value_text}` : o.signal_code;
          return `<span class="tag" title="${esc(tip)}">${esc(label)}</span>`;
        })
        .join("");

      const contacts = [
        c.phone ? esc(c.phone) : "",
        c.telegram ? `<a class="contact-link" href="${esc(c.telegram)}" target="_blank" rel="noopener">Telegram</a>` : "",
        c.whatsapp ? `<a class="contact-link" href="${esc(c.whatsapp)}" target="_blank" rel="noopener">WhatsApp</a>` : "",
        c.vk ? `<a class="contact-link" href="${esc(c.vk)}" target="_blank" rel="noopener">VK</a>` : "",
      ]
        .filter(Boolean)
        .join(" · ");

      const siteUrl = c.website || (c.domain ? `https://${c.domain}` : "");
      const links = [];
      if (siteUrl) {
        links.push(`<a href="${esc(siteUrl)}" target="_blank" rel="noopener">${esc(c.domain || siteUrl)}</a>`);
      }
      if (c.hh_url) {
        links.push(`<a href="${esc(c.hh_url)}" target="_blank" rel="noopener">hh.ru</a>`);
      }
      if (!links.length && c.source_url && c.source_url.startsWith("http")) {
        links.push(`<a href="${esc(c.source_url)}" target="_blank" rel="noopener">Карточка источника</a>`);
      }

      return `
        <tr>
          <td>
            <div class="entity-name">${esc(c.name)}</div>
            <div class="entity-sub">
              ${links.join(" · ") || "—"}
              ${c.inn ? ` · ИНН ${esc(c.inn)}` : ""}
              ${c.address ? ` · ${esc(c.address)}` : ""}
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
    tbody.innerHTML = `<tr><td colspan="6" class="empty-box">Контакты руководителей появятся после поиска или импорта компаний.</td></tr>`;
    return;
  }

  tbody.innerHTML = data.items
    .map((p) => {
      const channels = [
        p.telegram ? `<a class="contact-link" href="${esc(p.telegram)}" target="_blank" rel="noopener">Telegram</a>` : "",
        p.email ? `<a class="contact-link" href="mailto:${esc(p.email)}">${esc(p.email)}</a>` : "",
        p.phone ? esc(p.phone) : "",
        p.source_url && p.source_url.startsWith("http")
          ? `<a class="contact-link" href="${esc(p.source_url)}" target="_blank" rel="noopener">Сайт</a>`
          : "",
      ]
        .filter(Boolean)
        .join(" · ");
      const numScore = p.score ?? p.fit_score ?? 0;
      const scoreCls = numScore >= 65 ? "score-pill high" : "score-pill";
      return `
        <tr>
          <td>
            <div class="entity-name">${esc(p.full_name)}</div>
            <div class="entity-sub">${esc(p.city || "")}</div>
          </td>
          <td>${esc(p.title || "Руководитель")}</td>
          <td>${esc(p.company || "—")}</td>
          <td>${channels || "—"}</td>
          <td><span class="${scoreCls}" title="${esc(p.personalization_notes || "")}">${numScore}</span></td>
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
      const fio = [d.last_name, d.first_name, d.middle_name].filter(Boolean).join(" ") || d.title || "Руководитель";
      const st = STATUS_LABELS[d.status] || { text: d.status, cls: "" };
      const channelName =
        d.channel === "telegram" ? "Telegram" : d.channel === "whatsapp" ? "WhatsApp" : "Email";
      const dScore = d.score ?? d.fit_score ?? 0;

      const contacts = [
        d.telegram ? `<a class="contact-link" href="${esc(d.telegram)}" target="_blank" rel="noopener">Telegram</a>` : "",
        d.email ? `<a class="contact-link" href="mailto:${esc(d.email)}">${esc(d.email)}</a>` : "",
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
              <div class="draft-person">${esc(fio)}${d.title && fio !== d.title ? ` · ${esc(d.title)}` : ""}${d.city ? ` · ${esc(d.city)}` : ""}</div>
            </div>
            <div class="draft-badges">
              <span class="tag">${esc(channelName)}</span>
              ${dScore ? `<span class="score-pill ${dScore >= 65 ? "high" : ""}">${dScore}</span>` : ""}
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
      if (res.gate_passed) {
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
