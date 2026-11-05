/* F.R.I.D.A.Y. — Рабочая B2B-система поиска, аудита сайтов и подготовки предложений */

const state = {
  summary: null,
  config: null,
  cities: [],
  activeCityFilter: "",
  activeSubTab: "companies",
  draftStatusFilter: "",
  draftChannelFilter: "",
  draftCityFilter: "",
  presets: [],
  signalsCatalog: [],
  companiesCache: [],
  prospectsCache: [],
  editingDraftId: null,
  activeJobPoll: null,
  hideJobTimer: null,
};

const JUNK_HOST_SUBSTRINGS = [
  "yandex.", "ya.ru", "google.", "vk.com", "t.me", "telegram.", "ok.ru",
  "youtube.com", "rutube.ru", "avito.ru", "2gis.", "zoon.ru", "yell.ru",
  "flamp.ru", "profi.ru", "hh.ru", "superjob.ru", "rabota.ru", "trudvsem.ru",
  "rusprofile.ru", "spark-interfax.ru", "sbis.ru", "saby.ru", "list-org.com",
  "checko.ru", "audit-it.ru", "rbc.ru", "kommersant.ru", "vedomosti.ru",
  "forbes.ru", "vc.ru", "habr.com", "dzen.ru", "wikipedia.org", "wikidata.org",
  "pravo.ru", "zakon.ru", "consultant.ru", "garant.ru", "sudact.ru", "kad.arbitr.ru",
  "harant.ru", "9111.ru", "pravoved.ru", "jsprav.ru", "spravker.ru", "orgpage.ru",
  "yp.ru", "cataloxy.ru", "blizko.ru", "pulscen.ru", "tiu.ru", "satu.kz",
  "otzovik.com", "irecommend.ru", "asktel.ru", "pravo-pro.ru", "lawyers-rostov.ru",
  "gogov.ru", "rostovadvocat.ru",
  "gosuslugi.ru", "nalog.gov.ru", "arbitr.ru", "fparf.ru", "advpalata",
  "prodoctorov.ru", "napopravku.ru", "sberhealth.ru", "docdoc.ru",
  "cian.ru", "domclick.ru", "pikabu.ru", "ozon.ru", "wildberries.ru",
  "duckduckgo.com", "bing.com", "rambler.ru", "mail.ru",
];

const PARKED_SITE_MARKERS = [
  "домен продается", "домен продаётся", "купить этот домен", "срок регистрации домена истек",
  "срок регистрации домена истёк", "domain is for sale", "buy this domain",
  "this domain is parked", "парковка домена", "сайт временно заблокирован",
  "аккаунт заблокирован хостингом", "your subscription expired",
  "assertionfailureerror", "failed to goto",
];

function $(id) {
  return document.getElementById(id);
}

function esc(val) {
  const s = String(val ?? "");
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, isError = false) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.style.borderColor = isError ? "rgba(244,63,94,0.45)" : "rgba(255,255,255,0.16)";
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3800);
}

async function copyTextSafe(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      /* fallback для песочницы iframe */
    }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
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
  } catch (_) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

async function downloadFileSafe(url, filename) {
  try {
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) throw new Error(`Ошибка выгрузки (${res.status})`);
    const blob = await res.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(blobUrl), 2500);
    toast(`Файл ${filename} сохранён`);
  } catch (e) {
    toast(e.message || "Не удалось скачать файл", true);
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = `Ошибка ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (_) {
      /* ignore */
    }
    throw new Error(detail);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res.text();
}

/* ── Навигация ── */
function switchTab(tabName) {
  document.querySelectorAll(".nav-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tabName);
  });
  document.querySelectorAll(".view").forEach((s) => {
    s.classList.toggle("active", s.id === `tab-${tabName}`);
  });
  if (tabName === "workspace") refreshWorkspaceTables();
  if (tabName === "drafts") loadDrafts();
  if (tabName === "signals") loadSignals();
}

function switchSubView(subName) {
  state.activeSubTab = subName;
  document.querySelectorAll("#db-subtabs .seg-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.subview === subName);
  });
  $("subview-companies").classList.toggle("hidden", subName !== "companies");
  $("subview-prospects").classList.toggle("hidden", subName !== "prospects");
  $("cohort-controls").classList.toggle("hidden", subName !== "companies");
  refreshWorkspaceTables();
}

/* ── Индикатор прогресса ── */
function renderJobCard(job) {
  const card = $("live-job-card");
  if (!card || !job) return;
  clearTimeout(state.hideJobTimer);
  card.classList.remove("hidden", "done", "failed");

  const pct = Math.max(0, Math.min(100, Number(job.percent || 0)));
  $("job-title").textContent = job.title || "Выполнение операции";
  $("job-stage").textContent = job.error ? job.error : (job.stage || "Обработка…");
  $("job-percent").textContent = `${pct}%`;
  $("job-bar-fill").style.width = `${pct}%`;

  const stepsWrap = $("job-steps");
  stepsWrap.innerHTML = (job.steps || [])
    .map(
      (st) => `
      <span class="job-step-item ${esc(st.status)}">
        <span class="step-dot"></span>
        <span>${esc(st.label)}</span>
      </span>`
    )
    .join("");

  if (job.status === "completed") {
    card.classList.add("done");
    state.hideJobTimer = setTimeout(() => card.classList.add("hidden"), 4500);
  } else if (job.status === "failed") {
    card.classList.add("failed");
    state.hideJobTimer = setTimeout(() => card.classList.add("hidden"), 7000);
  }
}

async function runTrackedJob(startUrl, bodyObj, onFinished) {
  if (state.activeJobPoll) {
    clearInterval(state.activeJobPoll);
    state.activeJobPoll = null;
  }
  const job = await api(startUrl, {
    method: "POST",
    body: JSON.stringify(bodyObj || {}),
  });
  renderJobCard(job);

  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        const cur = await api(`/api/jobs/${encodeURIComponent(job.id)}`);
        renderJobCard(cur);
        if (cur.status === "completed") {
          clearInterval(state.activeJobPoll);
          state.activeJobPoll = null;
          await loadSummary();
          if (onFinished) await onFinished(cur);
          resolve(cur);
        } else if (cur.status === "failed") {
          clearInterval(state.activeJobPoll);
          state.activeJobPoll = null;
          toast(cur.error || "Не удалось завершить операцию", true);
          reject(new Error(cur.error || "Ошибка операции"));
        }
      } catch (err) {
        clearInterval(state.activeJobPoll);
        state.activeJobPoll = null;
        reject(err);
      }
    };
    state.activeJobPoll = setInterval(poll, 320);
  });
}

/* ── Проверка соответствия городу РФ (с учётом падежей) ── */
function buildCityTokens(city) {
  const raw = (city || "").trim().toLowerCase().replace(/ё/g, "е");
  if (!raw) return [];
  const aliases = {
    "ростов-на-дону": ["ростов-на-дону", "ростове-на-дону", "ростова-на-дону", "ростов на дону", "ростове на дону", "ростовской", "ростов", "рнд", "863"],
    "москва": ["москва", "москве", "москвы", "мск", "495", "499"],
    "санкт-петербург": ["санкт-петербург", "петербург", "спб", "питер", "812"],
    "екатеринбург": ["екатеринбург", "екб", "свердловск", "343"],
    "нижний новгород": ["нижний новгород", "нижнем новгороде", "нижнего новгорода", "нижегородск", "831"],
    "казань": ["казань", "казани", "татарстан", "843"],
    "новосибирск": ["новосибирск", "нск", "383"],
    "краснодар": ["краснодар", "кубан", "861"],
    "самара": ["самара", "самаре", "самарск", "846"],
    "воронеж": ["воронеж", "473"],
    "сочи": ["сочи", "862"],
    "ставрополь": ["ставропол", "8652"],
    "челябинск": ["челябинск", "351"],
    "уфа": ["уфа", "уфе", "башкортостан", "347"],
    "пермь": ["пермь", "перми", "пермск", "342"],
    "волгоград": ["волгоград", "8442"],
    "красноярск": ["красноярск", "391"],
    "тюмень": ["тюмень", "тюмени", "тюменск", "3452"],
    "калининград": ["калининград", "4012"],
  };
  if (aliases[raw]) return aliases[raw];
  const stem = raw.length >= 5 ? raw.slice(0, -1) : raw;
  return [raw, stem];
}

function textMatchesCity(text, targetCity) {
  if (!targetCity || !targetCity.trim()) return true;
  const hay = String(text || "").toLowerCase().replace(/ё/g, "е");
  if (!hay) return false;
  // Защита от путаницы Ростова Великого (Ярославская обл.) с Ростовом-на-Дону
  if (targetCity.toLowerCase().includes("ростов-на-дону") && hay.includes("ярославск")) {
    return false;
  }
  const tokens = buildCityTokens(targetCity);
  return tokens.some((tok) => tok && hay.includes(tok));
}

function isJunkHost(urlStr) {
  try {
    const u = new URL(urlStr);
    const host = u.hostname.toLowerCase().replace(/^www\./, "");
    if (!host || !host.includes(".")) return true;
    return JUNK_HOST_SUBSTRINGS.some((bad) => host === bad || host.endsWith(`.${bad}`) || host.includes(bad));
  } catch (_) {
    return true;
  }
}

function normalizeOriginUrl(urlStr) {
  try {
    const u = new URL(urlStr);
    if (!["http:", "https:"].includes(u.protocol)) return "";
    return `${u.protocol}//${u.hostname.replace(/^www\./i, "")}`;
  } catch (_) {
    return "";
  }
}

function expandSearchQueries(baseQuery, extraQueries = [], maxVariants = 4) {
  const q0 = (baseQuery || "").trim();
  const out = [];
  if (q0) out.push(q0);
  const low = q0.toLowerCase();
  if (low.includes("адвокат") || low.includes("юрист") || low.includes("коллеги") || low.includes("бюро")) {
    ["адвокатское бюро", "коллегия адвокатов", "юридическая фирма", "адвокатская контора"].forEach((v) => {
      if (!out.some((x) => x.toLowerCase() === v)) out.push(v);
    });
  } else if (low.includes("стомат") || low.includes("клиник") || low.includes("медцентр")) {
    ["стоматологическая клиника", "медицинский центр", "частная клиника"].forEach((v) => {
      if (!out.some((x) => x.toLowerCase() === v)) out.push(v);
    });
  }
  (extraQueries || []).forEach((eq) => {
    const clean = String(eq || "").trim();
    if (clean && !out.some((x) => x.toLowerCase() === clean.toLowerCase())) out.push(clean);
  });
  return out.slice(0, maxVariants);
}

async function fetchTextWithTimeout(url, timeoutMs = 9500) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(url, { method: "GET", signal: ctrl.signal });
    if (!resp.ok) return "";
    return await resp.text();
  } catch (_) {
    return "";
  } finally {
    clearTimeout(timer);
  }
}

async function fetchJsonWithTimeout(url, timeoutMs = 6000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(url, { method: "GET", signal: ctrl.signal });
    if (!resp.ok) return null;
    return await resp.json();
  } catch (_) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/* ── Источник 1: Прямой поиск органических сайтов компаний через DuckDuckGo HTML + Jina CORS ── */
function parseDdgMarkdownBlocks(md) {
  const results = [];
  if (!md) return results;
  const headingRe = /^##\s+\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/gm;
  const matches = [...md.matchAll(headingRe)];
  for (let i = 0; i < matches.length; i += 1) {
    const title = matches[i][1].replace(/\*\*/g, "").trim();
    const rawHref = matches[i][2].trim();
    const startIdx = matches[i].index + matches[i][0].length;
    const endIdx = i + 1 < matches.length ? matches[i + 1].index : Math.min(md.length, startIdx + 1200);
    const block = md.slice(startIdx, endIdx).replace(/\*\*/g, "");

    let targetUrl = rawHref;
    try {
      const parsed = new URL(rawHref);
      const uddg = parsed.searchParams.get("uddg");
      if (uddg) targetUrl = decodeURIComponent(uddg);
    } catch (_) {
      /* keep rawHref */
    }
    const cleanSnippet = block
      .replace(/!\[[^\]]*\]\([^)]+\)/g, " ")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/\s+/g, " ")
      .trim();

    results.push({ title, url: targetUrl, snippet: cleanSnippet });
  }
  return results;
}

function extractPhoneFromSnippet(text) {
  const m = String(text || "").match(/(?:\+7|8)[\s\-()]*\d{3,4}[\s\-()]*\d{2,3}[\s\-]*\d{2}[\s\-]*\d{2}/);
  return m ? m[0].trim() : "";
}

function extractInnFromSnippet(text) {
  const m = String(text || "").match(/ИНН[\s:№\-]*(\d{10}(?:\d{2})?)/i);
  return m ? m[1] : "";
}

function cleanCompanyTitle(rawTitle, fallbackDomain = "") {
  let t = String(rawTitle || "")
    .replace(/\s*[|—–\-•:]\s*(официальный сайт|главная|контакты|ростов-на-дону|москва|санкт-петербург|услуги|цены|отзывы).*$/i, "")
    .replace(/^(главная\s*[-—|:]\s*)/i, "")
    .trim();
  if (t.length > 95) t = t.slice(0, 95).trim();
  return t || fallbackDomain;
}

async function searchOrganicSitesRu(queries, city, maxItems) {
  const found = [];
  const seenHosts = new Set();
  const exclusions = "-site:zoon.ru -site:2gis.ru -site:harant.ru -site:jsprav.ru";
  const pageOffsets = maxItems > 18 ? [0, 30] : [0];

  for (const q of queries) {
    if (found.length >= maxItems) break;
    for (const offset of pageOffsets) {
      if (found.length >= maxItems) break;
      const ddgQuery = `${q} ${city} ${exclusions}`;
      const offsetParam = offset > 0 ? `&s=${offset}` : "";
      const ddgUrl = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(ddgQuery)}${offsetParam}`;
      const md = await fetchTextWithTimeout(`https://r.jina.ai/${ddgUrl}`, 11000);
      const blocks = parseDdgMarkdownBlocks(md);
      if (!blocks.length) break;

      for (const b of blocks) {
        if (found.length >= maxItems) break;
        if (!b.url || isJunkHost(b.url)) continue;
        const origin = normalizeOriginUrl(b.url);
        if (!origin || seenHosts.has(origin)) continue;

        const combined = `${b.title} ${b.snippet} ${b.url}`;
        if (!textMatchesCity(combined, city)) continue;
        if (/^(топ[\s\-]?\d+|рейтинг\s+|лучшие\s+|список\s+|все\s+адвокаты|все\s+компании)/i.test(b.title)) {
          continue;
        }

        seenHosts.add(origin);
        const hostName = new URL(origin).hostname;
        found.push({
          name: cleanCompanyTitle(b.title, hostName),
          website: origin,
          city,
          industry: queries[0] || "",
          phone: extractPhoneFromSnippet(b.snippet),
          inn: extractInnFromSnippet(b.snippet),
          source: "web_ru",
          source_url: b.url,
          external_id: `web:${hostName}`,
          description: b.snippet.slice(0, 280),
          observations: [],
        });
      }
    }
  }
  return found;
}

/* ── Источник 2: Верифицированные карточки Яндекс.Карт (адрес + телефон + локация в целевом городе) ── */
async function searchYandexMapsCardsRu(queries, city, maxItems) {
  const found = [];
  const seenOrgIds = new Set();

  for (const q of queries.slice(0, 3)) {
    if (found.length >= maxItems) break;
    const yQuery = `site:yandex.ru/maps/org ${q} ${city}`;
    const ddgUrl = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(yQuery)}`;
    const md = await fetchTextWithTimeout(`https://r.jina.ai/${ddgUrl}`, 11000);
    const blocks = parseDdgMarkdownBlocks(md);

    for (const b of blocks) {
      if (found.length >= maxItems) break;
      if (!b.url || !b.url.includes("yandex.ru/maps/org/")) continue;
      // Отсекаем вкладки /reviews/ и /gallery/, берём карточку организации
      const orgMatch = b.url.match(/yandex\.ru\/maps\/org\/([^/?#]+)\/(\d+)/i);
      if (!orgMatch) continue;
      const orgId = orgMatch[2];
      if (seenOrgIds.has(orgId)) continue;

      const combined = `${b.title} ${b.snippet}`;
      if (!textMatchesCity(combined, city)) continue;
      seenOrgIds.add(orgId);

      const cleanUrl = `https://yandex.ru/maps/org/${orgMatch[1]}/${orgId}/`;
      const addrMatch = b.snippet.match(/по адресу\s+([^,☎️]+(?:,\s*[^,☎️]+){0,3})/i);
      const address = addrMatch ? addrMatch[1].trim() : city;
      const phone = extractPhoneFromSnippet(b.snippet);
      const cleanName = b.title
        .replace(/\s*[—–\-|]\s*Яндекс\s*Карты.*$/i, "")
        .replace(/^(Отзывы о|Фото)\s+/i, "")
        .trim();

      const obs = [
        {
          signal_code: "NO_WEBSITE_ON_MAP",
          collector: "yandex_maps",
          value_text: `Карточка в Яндекс.Картах (${address}) — точка роста по прямому сайту и онлайн-записи`,
          evidence_url: cleanUrl,
        },
      ];

      found.push({
        name: cleanName,
        website: "",
        ymaps_url: cleanUrl,
        city,
        address,
        phone,
        industry: queries[0] || "",
        source: "yandex_maps",
        source_url: cleanUrl,
        external_id: `ymaps:${orgId}`,
        description: b.snippet.slice(0, 280),
        observations: obs,
      });
    }
  }
  return found;
}

/* ── Источник 3: OpenStreetMap Nominatim (работает для любого города РФ и с любого IP) ── */
async function fetchLiveFromNominatim(queries, city, maxItems) {
  const found = [];
  const seenIds = new Set();

  for (const q of queries.slice(0, 2)) {
    if (found.length >= maxItems) break;
    const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(`${q} ${city}`)}&format=jsonv2&extratags=1&addressdetails=1&limit=40&accept-language=ru`;
    const arr = await fetchJsonWithTimeout(url, 6500);
    if (!Array.isArray(arr)) continue;

    for (const row of arr) {
      if (found.length >= maxItems) break;
      const osmKey = `${row.osm_type || "n"}:${row.osm_id || ""}`;
      if (!row.osm_id || seenIds.has(osmKey)) continue;

      const addrObj = row.address || {};
      const rowCity = addrObj.city || addrObj.town || addrObj.municipality || "";
      if (!textMatchesCity(`${rowCity} ${row.display_name || ""}`, city)) continue;

      const tags = row.extratags || {};
      let website = (tags.website || tags["contact:website"] || tags.url || "").trim();
      if (website && !/^https?:\/\//i.test(website)) website = `https://${website}`;
      if (website && isJunkHost(website)) website = "";

      const phone = (tags.phone || tags["contact:phone"] || "").trim();
      const email = (tags.email || tags["contact:email"] || "").trim();
      const tg = (tags.telegram || tags["contact:telegram"] || "").trim();
      const street = [addrObj.road, addrObj.house_number].filter(Boolean).join(", ");

      let name = (row.name || "").trim();
      if (!name) continue;
      // Не берём пустые безымянные точки «Адвокат» без сайта и телефона
      if (["адвокат", "адвокаты", "юрист", "юристы", "нотариус"].includes(name.toLowerCase())) {
        if (!website && !phone && !email) continue;
        if (street) name = `${name} (${street})`;
      }

      const osmUrl = `https://www.openstreetmap.org/${row.osm_type || "node"}/${row.osm_id}`;
      seenIds.add(osmKey);

      const obs = [];
      if (!website) {
        obs.push({
          signal_code: "NO_WEBSITE_ON_MAP",
          collector: "osm_ru",
          value_text: `В геокарточке (${street || city}) не указан сайт организации`,
          evidence_url: osmUrl,
        });
      }

      found.push({
        name,
        website,
        city,
        address: [city, street].filter(Boolean).join(", "),
        phone,
        email,
        telegram: tg,
        industry: queries[0] || "",
        source: "osm_ru",
        source_url: website || osmUrl,
        external_id: `osm:${osmKey}`,
        description: street ? `Офис: ${city}, ${street}` : `Организация в г. ${city}`,
        observations: obs,
      });
    }
  }
  return found;
}

/* ── Источник 4: Прямой API HeadHunter (когда доступен с IP пользователя) ── */
async function resolveHhAreaId(city) {
  const c = (city || "").trim();
  if (!c) return "113";
  const presets = {
    "москва": "1",
    "санкт-петербург": "2",
    "екатеринбург": "3",
    "новосибирск": "4",
    "нижний новгород": "66",
    "казань": "88",
    "самара": "78",
    "ростов-на-дону": "76",
    "краснодар": "53",
    "воронеж": "26",
    "пермь": "72",
    "уфа": "99",
    "челябинск": "104",
    "красноярск": "54",
    "тюмень": "95",
    "сочи": "237",
  };
  const key = c.toLowerCase().replace(/ё/g, "е");
  if (presets[key]) return presets[key];
  const data = await fetchJsonWithTimeout(`https://api.hh.ru/suggests/areas?text=${encodeURIComponent(c)}`, 3500);
  if (data && Array.isArray(data.items) && data.items.length > 0) {
    return String(data.items[0].id);
  }
  return "113";
}

async function fetchLiveFromHeadHunter(queries, city, maxItems) {
  const areaId = await resolveHhAreaId(city);
  const q0 = queries[0] || "";
  const vacData = await fetchJsonWithTimeout(
    `https://api.hh.ru/vacancies?text=${encodeURIComponent(q0)}&area=${encodeURIComponent(areaId)}&per_page=${Math.min(40, maxItems + 10)}`,
    4200
  );
  if (!vacData || !Array.isArray(vacData.items)) return [];

  const empMap = new Map();
  for (const v of vacData.items) {
    const vacArea = v.area?.name || "";
    if (!textMatchesCity(vacArea, city)) continue;
    const emp = v.employer || {};
    if (!emp.id || !emp.name) continue;
    if (!empMap.has(String(emp.id))) {
      empMap.set(String(emp.id), {
        id: String(emp.id),
        name: emp.name,
        hh_url: emp.alternate_url || `https://hh.ru/employer/${emp.id}`,
        city: vacArea || city,
        vacancies: [v.name].filter(Boolean),
        vac_url: v.alternate_url || "",
        address: v.address?.raw || "",
      });
    }
  }

  const out = [];
  const candidates = Array.from(empMap.values()).slice(0, maxItems);
  for (let i = 0; i < candidates.length; i += 5) {
    const chunk = candidates.slice(i, i + 5);
    const details = await Promise.all(
      chunk.map((c) => fetchJsonWithTimeout(`https://api.hh.ru/employers/${c.id}`, 4000))
    );
    chunk.forEach((cand, idx) => {
      const det = details[idx] || {};
      let siteUrl = (det.site_url || "").trim();
      if (siteUrl && !/^https?:\/\//i.test(siteUrl)) siteUrl = `https://${siteUrl}`;
      if (siteUrl && isJunkHost(siteUrl)) siteUrl = "";

      const industries = Array.isArray(det.industries) ? det.industries.map((x) => x.name).filter(Boolean) : [];
      const plainDesc = String(det.description || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 280);
      const obs = [];
      if (cand.vacancies.length > 0) {
        obs.push({
          signal_code: "HH_HIRING_ROLE",
          collector: "hh_ru",
          value_text: `Активный найм на hh.ru (${city}): ${cand.vacancies[0]}`,
          evidence_url: cand.vac_url || cand.hh_url,
        });
      }
      out.push({
        name: det.name || cand.name,
        website: siteUrl,
        city,
        address: cand.address,
        industry: industries[0] || q0,
        hh_url: det.alternate_url || cand.hh_url,
        source_url: siteUrl || det.alternate_url || cand.hh_url,
        external_id: `hh:${cand.id}`,
        source: "hh_ru",
        description: plainDesc,
        observations: obs,
      });
    });
  }
  return out;
}

/* ── Живой аудит сайтов компаний и проверка открываемости ссылок ── */
async function auditCompanySitesLive(targets, onProgress) {
  if (!targets || !targets.length) return { audited: 0, pruned: 0 };
  const validPages = [];
  let pruned = 0;
  const total = targets.length;

  for (let i = 0; i < targets.length; i += 4) {
    const batch = targets.slice(i, i + 4);
    if (onProgress) {
      onProgress(i, total, batch[0]?.name || "");
    }
    const fetched = await Promise.all(
      batch.map(async (t) => {
        const siteUrl = (t.website || "").trim();
        if (!siteUrl) return null;
        const md = await fetchTextWithTimeout(`https://r.jina.ai/${siteUrl}`, 9500);
        return { target: t, md };
      })
    );

    for (const item of fetched) {
      if (!item) continue;
      const { target, md } = item;
      if (!md || md.length < 80) continue;

      const lowMd = md.toLowerCase();
      const hasHttpError = /warning:\s*target url returned error\s*(403|404|410|500|502|503|52\d)/i.test(md);
      const isParked = PARKED_SITE_MARKERS.some((m) => lowMd.includes(m));

      if (hasHttpError || isParked) {
        // Удаляем организацию с нерабочим/припаркованным сайтом, чтобы в базе были только живые ресурсы
        try {
          await api(`/api/companies/${encodeURIComponent(target.company_id)}`, { method: "DELETE" });
          pruned += 1;
        } catch (_) {
          /* ignore */
        }
        continue;
      }

      const titleMatch = md.match(/^Title:\s*(.+)$/m);
      const pageTitle = titleMatch ? titleMatch[1].trim() : target.name;
      validPages.push({
        company_id: target.company_id,
        url: target.website,
        title: pageTitle,
        content: md.slice(0, 18000),
      });
    }
  }

  let audited = 0;
  if (validPages.length > 0) {
    const res = await api("/api/enrich/live-pages", {
      method: "POST",
      body: JSON.stringify({ pages: validPages }),
    });
    audited = res.profiled || validPages.length;
  }
  return { audited, pruned };
}

/* ── Полный многоисточниковый поиск по целевому городу ── */
async function browserLiveDiscover(query, city, limit, provider, extraQueries = []) {
  const targetLimit = Math.max(5, Math.min(150, Number(limit) || 30));
  const queries = expandSearchQueries(query, extraQueries, targetLimit > 20 ? 4 : 2);

  renderJobCard({
    title: `Поиск организаций (${city})`,
    stage: `Сбор живых сайтов и карточек по г. ${city} (запросов: ${queries.length})…`,
    percent: 32,
    status: "running",
    steps: [
      { label: `Поиск в г. ${city}`, status: "active" },
      { label: "Проверка открываемости и аудит сайтов", status: "pending" },
      { label: "Извлечение ЛПР и оценка", status: "pending" },
    ],
  });

  const tasks = [];
  if (provider === "auto" || provider === "web" || provider === "yandex_search") {
    tasks.push(searchOrganicSitesRu(queries, city, targetLimit));
  }
  if (provider === "auto" || provider === "yandex_maps") {
    tasks.push(searchYandexMapsCardsRu(queries, city, Math.ceil(targetLimit * 0.75)));
  }
  if (provider === "auto" || provider === "twogis") {
    tasks.push(fetchLiveFromNominatim(queries, city, Math.ceil(targetLimit * 0.6)));
  }
  if (provider === "auto" || provider === "hh") {
    tasks.push(fetchLiveFromHeadHunter(queries, city, Math.ceil(targetLimit * 0.6)));
  }

  const settled = await Promise.all(tasks);
  const merged = [];
  const seenKeys = new Set();

  for (const list of settled) {
    for (const item of list || []) {
      const key = (
        (item.website && normalizeOriginUrl(item.website)) ||
        item.ymaps_url ||
        item.hh_url ||
        `${item.name.toLowerCase()}::${city.toLowerCase()}`
      );
      if (seenKeys.has(key)) continue;
      seenKeys.add(key);
      merged.push(item);
      if (merged.length >= targetLimit) break;
    }
    if (merged.length >= targetLimit) break;
  }

  if (merged.length === 0) {
    renderJobCard({
      title: `Поиск организаций (${city})`,
      stage: `По запросу «${query}» в г. ${city} открытых карточек не найдено — попробуйте уточнить формулировку`,
      percent: 100,
      status: "failed",
      steps: [{ label: `Поиск в г. ${city}`, status: "error" }],
    });
    return { inserted_companies: 0 };
  }

  renderJobCard({
    title: `Поиск организаций (${city})`,
    stage: `Сохранение ${merged.length} найденных организаций в г. ${city}…`,
    percent: 62,
    status: "running",
    steps: [
      { label: `Поиск в г. ${city}`, status: "done" },
      { label: "Проверка открываемости и аудит сайтов", status: "active" },
      { label: "Извлечение ЛПР и оценка", status: "pending" },
    ],
  });

  const saved = await api("/api/import/live-batch", {
    method: "POST",
    body: JSON.stringify({ items: merged }),
  });

  // Сразу проверяем открываемость найденных сайтов и извлекаем стек + ФИО ЛПР
  const compsResp = await api(`/api/companies?city=${encodeURIComponent(city)}&limit=${targetLimit}`);
  const freshWebTargets = (compsResp.items || [])
    .filter((c) => c.website && !c.profile_checked_at)
    .slice(0, Math.min(14, targetLimit))
    .map((c) => ({ company_id: c.id, name: c.name, website: c.website }));

  let auditStats = { audited: 0, pruned: 0 };
  if (freshWebTargets.length > 0) {
    auditStats = await auditCompanySitesLive(freshWebTargets, (done, tot, curName) => {
      const pct = 66 + Math.round(((done + 1) / Math.max(1, tot)) * 26);
      renderJobCard({
        title: `Поиск и аудит (${city})`,
        stage: `Живой аудит сайта (${Math.min(done + 1, tot)} из ${tot}): ${curName}`,
        percent: Math.min(94, pct),
        status: "running",
        steps: [
          { label: `Поиск в г. ${city}`, status: "done" },
          { label: "Проверка открываемости и аудит сайтов", status: "active" },
          { label: "Извлечение ЛПР и оценка", status: "pending" },
        ],
      });
    });
  }

  const finalCount = Math.max(0, merged.length - (auditStats.pruned || 0));
  renderJobCard({
    title: `Поиск организаций (${city})`,
    stage: `Готово (${city}): организаций — ${finalCount}, проверено сайтов — ${auditStats.audited}`,
    percent: 100,
    status: "completed",
    steps: [
      { label: `Поиск в г. ${city}`, status: "done" },
      { label: "Проверка открываемости и аудит сайтов", status: "done" },
      { label: "Извлечение ЛПР и оценка", status: "done" },
    ],
  });

  await loadSummary();
  return { ...saved, audited: auditStats.audited, finalCount };
}

/* ── Сводка, города и быстрые подсказки ── */
async function loadSummary() {
  const [data, sigData] = await Promise.all([
    api("/api/summary"),
    api("/api/signals?status=confirmed").catch(() => ({ items: [] })),
  ]);
  state.summary = data.summary;
  state.config = data.config;
  state.cities = data.cities || [];
  state.signalsCatalog = sigData.items || [];

  const configured = Boolean(data.niche_configured);
  const dot = $("project-status-dot");
  dot.classList.toggle("ready", configured);
  const compName = data.config?.persona?.company || "";
  const nicheName = data.config?.icp?.niche || "";
  $("project-status-text").textContent = configured
    ? `${compName}${nicheName ? " · " + nicheName : ""}`
    : "Проект не настроен";

  const compTotal = data.summary.companies_total || 0;
  const draftTotal = data.summary.drafts_total || 0;

  $("badge-companies").textContent = compTotal;
  $("badge-drafts").textContent = draftTotal;

  $("kpi-companies").textContent = compTotal;
  $("kpi-prospects").textContent = data.summary.prospects_total || 0;
  $("kpi-observations").textContent = data.summary.observations_total || 0;
  $("kpi-drafts").textContent = draftTotal;

  populateConfigForm(data.config, data.skills || {});
  renderCityFilterPills();
  renderQuickSearchHints();
  populateSignalDropdown();
  populateDraftCityDropdown();
}

function renderCityFilterPills() {
  const wrap = $("city-filter-pills");
  if (!wrap) return;
  const totalAll = state.summary?.companies_total || 0;
  const pills = [
    `<button type="button" class="city-pill ${state.activeCityFilter === "" ? "active" : ""}" data-city="">
      Все города (${totalAll})
    </button>`,
  ];

  let activeExists = state.activeCityFilter === "";
  for (const item of state.cities) {
    const isAct = state.activeCityFilter.toLowerCase() === item.city.toLowerCase();
    if (isAct) activeExists = true;
    pills.push(`
      <button type="button" class="city-pill ${isAct ? "active" : ""}" data-city="${esc(item.city)}">
        ${esc(item.city)} (${item.count})
      </button>
    `);
  }
  if (!activeExists && state.activeCityFilter) {
    pills.push(`
      <button type="button" class="city-pill active" data-city="${esc(state.activeCityFilter)}">
        ${esc(state.activeCityFilter)} (0)
      </button>
    `);
  }

  wrap.innerHTML = pills.join("");
  wrap.querySelectorAll(".city-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.activeCityFilter = btn.dataset.city || "";
      if (state.activeCityFilter) {
        $("inp-city").value = state.activeCityFilter;
      }
      renderCityFilterPills();
      refreshWorkspaceTables();
    });
  });
}

function renderQuickSearchHints() {
  const bar = $("quick-hints-bar");
  if (!bar || !state.config) return;
  const queries = (state.config.icp?.search_queries || []).slice(0, 4);
  const cities = (state.config.icp?.cities || []).slice(0, 5);
  const parts = [];

  if (queries.length > 0) {
    parts.push(`<span class="filter-caption">Запросы:</span>`);
    queries.forEach((q) => {
      parts.push(`<button type="button" class="hint-chip" data-set-query="${esc(q)}">${esc(q)}</button>`);
    });
  }
  if (cities.length > 0) {
    parts.push(`<span class="filter-caption" style="margin-left:6px">Города:</span>`);
    cities.forEach((c) => {
      parts.push(`<button type="button" class="hint-chip" data-set-city="${esc(c)}">${esc(c)}</button>`);
    });
  }

  bar.innerHTML = parts.join("");
  bar.querySelectorAll("[data-set-query]").forEach((b) => {
    b.addEventListener("click", () => {
      $("inp-query").value = b.dataset.setQuery;
    });
  });
  bar.querySelectorAll("[data-set-city]").forEach((b) => {
    b.addEventListener("click", () => {
      $("inp-city").value = b.dataset.setCity;
    });
  });
}

function populateSignalDropdown() {
  const sel = $("sel-require-signal");
  if (!sel) return;
  const curVal = sel.value;
  const opts = [`<option value="">Все сигналы аудита</option>`];
  for (const s of state.signalsCatalog) {
    opts.push(`<option value="${esc(s.code)}">${esc(s.label)}</option>`);
  }
  sel.innerHTML = opts.join("");
  sel.value = curVal;
}

function populateDraftCityDropdown() {
  const sel = $("flt-draft-city");
  if (!sel) return;
  const cur = state.draftCityFilter;
  const opts = [`<option value="">Все города</option>`];
  for (const c of state.cities) {
    opts.push(`<option value="${esc(c.city)}">${esc(c.city)} (${c.count})</option>`);
  }
  sel.innerHTML = opts.join("");
  sel.value = cur;
}

function populateConfigForm(cfg, skills) {
  if (!cfg) return;
  $("cfg-persona-company").value = cfg.persona?.company || "";
  $("cfg-persona-name").value = cfg.persona?.name || "";
  $("cfg-persona-role").value = cfg.persona?.role || "";
  $("cfg-persona-telegram").value = cfg.persona?.telegram || "";
  $("cfg-persona-website").value = cfg.persona?.website || "";

  $("cfg-product-name").value = cfg.product?.name || "";
  $("cfg-product-desc").value = cfg.product?.description || "";
  $("cfg-product-pricing").value = cfg.product?.pricing || "";
  $("cfg-offer-entry").value = cfg.product?.offer?.entry || "";
  $("cfg-product-benefits").value = (cfg.product?.key_benefits || []).join("\n");

  $("cfg-icp-niche").value = cfg.icp?.niche || "";
  $("cfg-icp-industries").value = (cfg.icp?.industries || []).join(", ");
  $("cfg-icp-cities").value = (cfg.icp?.cities || []).join(", ");
  $("cfg-icp-queries").value = (cfg.icp?.search_queries || []).join("\n");
  $("cfg-icp-titles").value = (cfg.icp?.decision_maker_titles || []).join(", ");
  $("cfg-icp-hiring").value = (cfg.icp?.hiring_role_keywords || []).join(", ");
  $("cfg-regulated").value = cfg.compliance?.regulated_niche || "none";
  $("cfg-dnc-list").value = (cfg.compliance?.do_not_contact || []).join("\n");
  $("cfg-skill-md").value = skills?.product_knowledge_md || "";

  if (!$("train-website").value && cfg.persona?.website) {
    $("train-website").value = cfg.persona.website;
  }
  if (!$("train-cities").value && (cfg.icp?.cities || []).length) {
    $("train-cities").value = cfg.icp.cities.join(", ");
  }
  if (!$("inp-query").value && (cfg.icp?.search_queries || []).length) {
    $("inp-query").value = cfg.icp.search_queries[0];
  }
  if (!$("inp-city").value && (cfg.icp?.cities || []).length) {
    $("inp-city").value = cfg.icp.cities[0];
  }
}

async function loadPresets() {
  const data = await api("/api/niche/presets?ui=5");
  state.presets = data.items || [];
  const bar = $("niche-presets-bar");
  bar.innerHTML = "";
  state.presets.forEach((pr) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "preset-chip";
    btn.textContent = pr.label;
    btn.addEventListener("click", () => {
      populateConfigForm(
        {
          persona: state.config?.persona || {},
          product: pr.product,
          icp: pr.icp,
          compliance: pr.compliance,
        },
        { product_knowledge_md: $("cfg-skill-md").value }
      );
      $("train-niche-hint").value = pr.icp.niche;
      $("train-brief").value = pr.product.description;
      toast(`Применён шаблон «${pr.label}»`);
    });
    bar.appendChild(btn);
  });
}

function collectConfigPayload() {
  const splitCsv = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const splitLines = (s) => s.split("\n").map((x) => x.trim()).filter(Boolean);
  return {
    persona: {
      company: $("cfg-persona-company").value.trim(),
      name: $("cfg-persona-name").value.trim(),
      role: $("cfg-persona-role").value.trim(),
      telegram: $("cfg-persona-telegram").value.trim(),
      website: $("cfg-persona-website").value.trim(),
    },
    product: {
      name: $("cfg-product-name").value.trim(),
      description: $("cfg-product-desc").value.trim(),
      pricing: $("cfg-product-pricing").value.trim(),
      key_benefits: splitLines($("cfg-product-benefits").value),
      offer: {
        entry: $("cfg-offer-entry").value.trim(),
        core: $("cfg-product-name").value.trim(),
      },
    },
    icp: {
      niche: $("cfg-icp-niche").value.trim(),
      industries: splitCsv($("cfg-icp-industries").value),
      cities: splitCsv($("cfg-icp-cities").value),
      search_queries: splitLines($("cfg-icp-queries").value),
      decision_maker_titles: splitCsv($("cfg-icp-titles").value),
      hiring_role_keywords: splitCsv($("cfg-icp-hiring").value),
    },
    compliance: {
      regulated_niche: $("cfg-regulated").value,
      do_not_contact: splitLines($("cfg-dnc-list").value),
    },
    product_knowledge_md: $("cfg-skill-md").value,
  };
}

/* ── Таблицы компаний и ЛПР с учётом фильтра города ── */
function refreshWorkspaceTables() {
  if (state.activeSubTab === "prospects") {
    loadProspects();
  } else {
    loadCompanies();
  }
}

const FALLBACK_SIGNAL_LABELS_RU = {
  HH_HIRING_ROLE: "Открытая вакансия на hh.ru",
  HH_OPEN_ROLES: "Активный найм в компании",
  YANDEX_PAGE2_RANK: "Топ-20 Яндекса (резерв роста)",
  YANDEX_DIRECT_ACTIVE: "Активна реклама Яндекс.Директ",
  VK_PIXEL_ACTIVE: "Пиксель VK Рекламы",
  NO_ONLINE_CAPTURE: "Нет форм захвата на сайте",
  NO_YANDEX_METRIKA: "Нет счётчика Яндекс.Метрики",
  NO_MESSENGER_WIDGET: "Нет виджетов мессенджеров",
  NO_MOBILE_VIEWPORT: "Нет мобильной адаптации",
  LOW_MAP_RATING: "Точка роста рейтинга на картах",
  NO_WEBSITE_ON_MAP: "В геокарточке не указан сайт",
  TWOGIS_RATING: "Рейтинг в геокаталоге",
  TWOGIS_REVIEWS: "Отзывы в геокаталоге",
  THIN_SERVICE_STRUCTURE: "Узкая структура разделов сайта",
  OUTDATED_COPYRIGHT: "Устаревший копирайт сайта",
  SSL_ISSUE: "Проблема с SSL-сертификатом",
  NO_HTTPS_REDIRECT: "Нет редиректа на HTTPS",
  SLOW_SITE: "Медленная загрузка сайта",
  SITE_UNREACHABLE: "Сайт недоступен",
  CALLTRACKING_ACTIVE: "Подключён коллтрекинг",
  CRM_WIDGET_ACTIVE: "Подключён виджет CRM",
  ONLINE_BOOKING_ACTIVE: "Подключена онлайн-запись",
  TECH_STACK_RU: "Определена CMS сайта",
  TELEGRAM_PUBLIC: "Публичный Telegram",
  WHATSAPP_PUBLIC: "Публичный WhatsApp",
  DECISION_MAKER_FOUND: "Найдены ФИО и должность ЛПР",
  PERSONAL_EMAIL_FOUND: "Прямой e-mail руководителя",
};

function signalLabelByCode(code) {
  const found = state.signalsCatalog.find((s) => s.code === code);
  if (found && found.label) return found.label;
  return FALLBACK_SIGNAL_LABELS_RU[code] || "Сигнал аудита";
}

async function loadCompanies() {
  const reqSig = $("sel-require-signal")?.value || "";
  const params = new URLSearchParams();
  if (reqSig) params.set("require", reqSig);
  if (state.activeCityFilter) params.set("city", state.activeCityFilter);
  params.set("limit", "200");

  const data = await api(`/api/companies?${params.toString()}`);
  state.companiesCache = data.items || [];
  const tbody = $("companies-tbody");

  if (!state.companiesCache.length) {
    const cityMsg = state.activeCityFilter ? ` по г. «${esc(state.activeCityFilter)}»` : "";
    tbody.innerHTML = `<tr><td colspan="5">
      <div class="empty-box">
        Организации${cityMsg} пока не найдены. Укажите запрос, город и нажмите «Найти компании».
      </div>
    </td></tr>`;
    return;
  }

  const srcLabels = {
    hh_ru: "HeadHunter",
    web_ru: "Сайт компании",
    ddg_ru: "Веб-поиск",
    osm_ru: "Геокаталог OSM",
    yandex_search: "Яндекс Поиск",
    twogis: "2ГИС",
    yandex_maps: "Яндекс Карты",
    import_file: "Импорт файла",
  };

  tbody.innerHTML = state.companiesCache
    .map((c) => {
      const siteHref = c.website || (c.domain ? `https://${c.domain}` : "");
      const siteLabel = c.domain || (siteHref ? siteHref.replace(/^https?:\/\//, "").split("/")[0] : "");

      const resourceLinks = [];
      if (siteHref) {
        resourceLinks.push(`<a class="cell-link" href="${esc(siteHref)}" target="_blank" rel="noopener">${esc(siteLabel)} ↗</a>`);
      }
      if (c.ymaps_url) {
        resourceLinks.push(`<a class="cell-link" href="${esc(c.ymaps_url)}" target="_blank" rel="noopener">Яндекс.Карты ↗</a>`);
      }
      if (c.hh_url) {
        resourceLinks.push(`<a class="cell-link" href="${esc(c.hh_url)}" target="_blank" rel="noopener">hh.ru ↗</a>`);
      }
      if (!siteHref && !c.ymaps_url && !c.hh_url && c.source_url) {
        resourceLinks.push(`<a class="cell-link" href="${esc(c.source_url)}" target="_blank" rel="noopener">${esc(srcLabels[c.source] || "Источник")} ↗</a>`);
      }

      const contactItems = [];
      if (c.phone) contactItems.push(`<a class="cell-link phone-link" href="tel:${esc(c.phone)}">${esc(c.phone)}</a>`);
      if (c.telegram) {
        const tgHref = c.telegram.startsWith("http") ? c.telegram : `https://t.me/${c.telegram.replace(/^@/, "")}`;
        contactItems.push(`<a class="cell-link" href="${esc(tgHref)}" target="_blank" rel="noopener">Telegram ↗</a>`);
      }
      if (c.whatsapp) {
        contactItems.push(`<a class="cell-link" href="${esc(c.whatsapp)}" target="_blank" rel="noopener">WhatsApp ↗</a>`);
      }

      const stackBadges = (c.tech_stack || [])
        .slice(0, 3)
        .map((st) => `<span class="tag">${esc(st)}</span>`);

      const obsBadges = (c.observations || [])
        .slice(0, 3)
        .map((o) => {
          const lbl = o.signal_label || signalLabelByCode(o.signal_code);
          return `<span class="tag blue" title="${esc(o.value_text || lbl)}">${esc(lbl)}</span>`;
        });

      const allBadges = [...stackBadges, ...obsBadges].join("") || `<span class="cell-sub">${esc(srcLabels[c.source] || c.source)}</span>`;

      return `
        <tr>
          <td>
            <div class="cell-primary">${esc(c.name)}</div>
            ${c.address ? `<div class="cell-sub">${esc(c.address)}</div>` : ""}
            ${c.inn ? `<div class="cell-sub">ИНН ${esc(c.inn)}</div>` : ""}
          </td>
          <td class="cell-city">${esc(c.city || "—")}</td>
          <td>
            <div class="links-row">${resourceLinks.join("") || `<span class="cell-sub">—</span>`}</div>
            ${contactItems.length ? `<div class="links-row">${contactItems.join("")}</div>` : ""}
          </td>
          <td>${allBadges}</td>
          <td class="col-actions">
            <div class="row-actions">
              <button type="button" class="secondary-btn sm" data-prepare-company="${esc(c.id)}">
                Подготовить предложение
              </button>
              ${siteHref ? `<button type="button" class="ghost-btn sm" data-audit-company="${esc(c.id)}" data-audit-url="${esc(siteHref)}" data-audit-name="${esc(c.name)}" title="Проверить сайт и найти ЛПР">Аудит</button>` : ""}
              <button type="button" class="ghost-btn sm danger-text" data-del-company="${esc(c.id)}" title="Удалить из базы">×</button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll("[data-prepare-company]").forEach((btn) => {
    btn.addEventListener("click", () => prepareProposalFor({ company_id: btn.dataset.prepareCompany }, btn));
  });

  tbody.querySelectorAll("[data-audit-company]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const cid = btn.dataset.auditCompany;
      const url = btn.dataset.auditUrl;
      const nm = btn.dataset.auditName;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const stats = await auditCompanySitesLive([{ company_id: cid, name: nm, website: url }]);
        await loadSummary();
        await loadCompanies();
        if (stats.pruned > 0) {
          toast(`Сайт ${url} недоступен — карточка удалена`, true);
        } else {
          toast(`Аудит сайта «${nm}» обновлён`);
        }
      } catch (e) {
        toast(e.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Аудит";
      }
    });
  });

  tbody.querySelectorAll("[data-del-company]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/companies/${encodeURIComponent(btn.dataset.delCompany)}`, { method: "DELETE" });
        await loadSummary();
        await loadCompanies();
      } catch (e) {
        toast(e.message, true);
      }
    });
  });
}

async function loadProspects() {
  const params = new URLSearchParams({ limit: "200" });
  if (state.activeCityFilter) params.set("city", state.activeCityFilter);

  const data = await api(`/api/prospects?${params.toString()}`);
  state.prospectsCache = data.items || [];
  const tbody = $("prospects-tbody");

  if (!state.prospectsCache.length) {
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty-box">Контакты ещё не извлечены. Нажмите «Аудит сайтов и поиск ЛПР» или запустите поиск компаний.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = state.prospectsCache
    .map((p) => {
      const channels = [];
      if (p.email) channels.push(`<a class="cell-link" href="mailto:${esc(p.email)}">${esc(p.email)}</a>`);
      if (p.telegram) {
        const tgHref = p.telegram.startsWith("http") ? p.telegram : `https://t.me/${p.telegram.replace(/^@/, "")}`;
        channels.push(`<a class="cell-link" href="${esc(tgHref)}" target="_blank" rel="noopener">Telegram ↗</a>`);
      }
      if (p.whatsapp) channels.push(`<a class="cell-link" href="${esc(p.whatsapp)}" target="_blank" rel="noopener">WhatsApp ↗</a>`);
      if (p.phone) channels.push(`<a class="cell-link phone-link" href="tel:${esc(p.phone)}">${esc(p.phone)}</a>`);

      const sc = Number(p.score || 0);
      return `
        <tr>
          <td>
            <div class="cell-primary">${esc(p.full_name)}</div>
            ${p.personalization_notes ? `<div class="cell-sub">${esc(p.personalization_notes)}</div>` : ""}
          </td>
          <td>${esc(p.title || "Руководитель")}</td>
          <td>
            <div class="cell-primary">${esc(p.company || "—")}</div>
            <div class="cell-sub">${esc(p.city || "")}</div>
          </td>
          <td><div class="links-row">${channels.join("") || `<span class="cell-sub">—</span>`}</div></td>
          <td><span class="score-pill ${sc >= 65 ? "high" : ""}">${sc}</span></td>
          <td class="col-actions">
            <button type="button" class="secondary-btn sm" data-prepare-prospect="${esc(p.id)}">
              Подготовить предложение
            </button>
          </td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll("[data-prepare-prospect]").forEach((btn) => {
    btn.addEventListener("click", () => prepareProposalFor({ prospect_id: btn.dataset.prepareProspect }, btn));
  });
}

/* ── Мгновенная подготовка предложения по 1 компании или ЛПР ── */
async function prepareProposalFor(targetPayload, btnEl) {
  const channel = $("sel-draft-channel")?.value || "telegram";
  if (btnEl) {
    btnEl.disabled = true;
    btnEl._origText = btnEl.textContent;
    btnEl.textContent = "Готовим…";
  }
  try {
    const resp = await api("/api/proposals/prepare", {
      method: "POST",
      body: JSON.stringify({ ...targetPayload, channel }),
    });
    await loadSummary();
    state.draftStatusFilter = "";
    document.querySelectorAll("#draft-status-pills .seg-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.status === "");
    });
    toast(`Персональное предложение подготовлено (${channel.toUpperCase()})`);
    switchTab("drafts");
    if (resp.draft?.id) {
      setTimeout(() => {
        const card = document.querySelector(`[data-draft-card="${resp.draft.id}"]`);
        if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 120);
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = btnEl._origText || "Подготовить предложение";
    }
  }
}

/* ── Раздел «Предложения» (Черновики + Экспресс-аудит + Редактирование + Gate) ── */
async function loadDrafts() {
  const params = new URLSearchParams();
  if (state.draftStatusFilter) params.set("status", state.draftStatusFilter);
  if (state.draftChannelFilter) params.set("channel", state.draftChannelFilter);
  if (state.draftCityFilter) params.set("city", state.draftCityFilter);

  const data = await api(`/api/drafts?${params.toString()}`);
  const list = $("drafts-list");

  if (!data.items.length) {
    list.innerHTML = `
      <div class="empty-box">
        Подготовленных предложений по выбранному фильтру пока нет.
        <div class="empty-actions">
          <button type="button" class="primary-btn sm" id="btn-empty-prepare">
            Подготовить предложения по найденной базе
          </button>
          <button type="button" class="ghost-btn sm" id="btn-empty-go-search">
            Перейти к поиску компаний
          </button>
        </div>
      </div>`;
    $("btn-empty-prepare")?.addEventListener("click", triggerBatchProposals);
    $("btn-empty-go-search")?.addEventListener("click", () => switchTab("workspace"));
    return;
  }

  const statusMeta = {
    pending_review: ["На проверке", "amber"],
    approved: ["Одобрено", "green"],
    sent_manually: ["Отправлено", "blue"],
    rejected: ["Отклонено", ""],
    gate_blocked: ["Заблокировано Gate", "rose"],
  };

  list.innerHTML = data.items
    .map((d) => {
      const fio = [d.last_name, d.first_name, d.middle_name].filter(Boolean).join(" ") || "Руководитель организации";
      const [stLabel, stTone] = statusMeta[d.status] || [d.status, ""];
      const fullCopy = d.subject ? `Тема: ${d.subject}\n\n${d.body}` : d.body;

      const tgRaw = d.prospect_telegram || d.company_telegram || "";
      const phoneRaw = d.prospect_phone || d.company_phone || "";
      const siteUrl = d.website || (d.domain ? `https://${d.domain}` : "") || d.ymaps_url || d.company_source_url || "";

      const contactLinks = [];
      if (siteUrl) {
        contactLinks.push(`<a class="cell-link" href="${esc(siteUrl)}" target="_blank" rel="noopener">Открыть сайт / карточку ↗</a>`);
      }
      if (tgRaw) {
        const tgHref = tgRaw.startsWith("http") ? tgRaw : `https://t.me/${tgRaw.replace(/^@/, "")}`;
        contactLinks.push(`<a class="cell-link" href="${esc(tgHref)}" target="_blank" rel="noopener">Telegram ↗</a>`);
      }
      if (d.prospect_whatsapp) {
        contactLinks.push(`<a class="cell-link" href="${esc(d.prospect_whatsapp)}" target="_blank" rel="noopener">WhatsApp ↗</a>`);
      }
      if (d.email) {
        const mailSubj = encodeURIComponent(d.subject || `Предложение для ${d.company_name || ""}`);
        const mailBody = encodeURIComponent(d.body || "");
        contactLinks.push(`<a class="cell-link" href="mailto:${esc(d.email)}?subject=${mailSubj}&body=${mailBody}">${esc(d.email)}</a>`);
      }
      if (phoneRaw) {
        contactLinks.push(`<a class="cell-link phone-link" href="tel:${esc(phoneRaw)}">${esc(phoneRaw)}</a>`);
      }

      const isEditing = state.editingDraftId === d.id;

      return `
        <article class="draft-item" data-draft-card="${esc(d.id)}">
          <div class="draft-top">
            <div>
              <div class="draft-company">${esc(d.company_name || d.domain || "Организация")} ${d.city ? `<span class="cell-sub">· ${esc(d.city)}</span>` : ""}</div>
              <div class="draft-person">${esc(fio)}${d.title ? " · " + esc(d.title) : ""}</div>
            </div>
            <div class="draft-badges">
              <span class="tag">${esc(d.channel.toUpperCase())}</span>
              <span class="tag ${stTone}">${esc(stLabel)}</span>
              <span class="score-pill ${Number(d.score) >= 65 ? "high" : ""}">${esc(d.score ?? 0)}</span>
            </div>
          </div>

          ${d.audit_summary ? `<div class="audit-box"><strong>Экспресс-аудит:</strong> ${esc(d.audit_summary)}</div>` : ""}

          ${
            isEditing
              ? `
              <div class="draft-editor">
                ${d.channel === "email" ? `<input type="text" id="edit-subj-${esc(d.id)}" value="${esc(d.subject || "")}" placeholder="Тема письма" />` : ""}
                <textarea id="edit-body-${esc(d.id)}">${esc(d.body || "")}</textarea>
                <div class="row-actions">
                  <button type="button" class="primary-btn sm" data-save-edit="${esc(d.id)}">Сохранить и проверить Gate</button>
                  <button type="button" class="ghost-btn sm" data-cancel-edit="${esc(d.id)}">Отмена</button>
                </div>
              </div>
              `
              : `
              ${d.subject ? `<div class="draft-subject">Тема: ${esc(d.subject)}</div>` : ""}
              <div class="draft-body">${esc(d.body)}</div>
              `
          }

          ${
            (d.gate_notes || []).length
              ? `<div class="draft-note ${d.status === "gate_blocked" ? "err" : ""}">Проверка комплаенса (Gate): ${esc(d.gate_notes.join(" · "))}</div>`
              : `<div class="draft-note">Проверка ФЗ-38 / КПЭА пройдена без замечаний</div>`
          }

          <div class="draft-bottom">
            <div class="links-row">${contactLinks.join("") || `<span class="cell-sub">Контакты в карточке компании</span>`}</div>
            <div class="draft-actions">
              <button type="button" class="secondary-btn sm" data-copy-draft="${esc(fullCopy)}">Скопировать текст</button>
              <button type="button" class="ghost-btn sm" data-edit-draft="${esc(d.id)}">Редактировать</button>
              <button type="button" class="ghost-btn sm" data-draft-id="${esc(d.id)}" data-status="approved">Одобрить</button>
              <button type="button" class="ghost-btn sm" data-draft-id="${esc(d.id)}" data-status="sent_manually">Отправлено</button>
              ${d.domain ? `<button type="button" class="ghost-btn sm danger-text" data-dnc-domain="${esc(d.domain)}" data-draft-reject="${esc(d.id)}" title="Добавить домен в стоп-лист DNC">В стоп-лист</button>` : ""}
            </div>
          </div>
        </article>
      `;
    })
    .join("");

  list.querySelectorAll("[data-copy-draft]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const ok = await copyTextSafe(btn.dataset.copyDraft);
      toast(ok ? "Предложение скопировано в буфер обмена" : "Не удалось скопировать", !ok);
    });
  });

  list.querySelectorAll("[data-edit-draft]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.editingDraftId = btn.dataset.editDraft;
      loadDrafts();
    });
  });

  list.querySelectorAll("[data-cancel-edit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.editingDraftId = null;
      loadDrafts();
    });
  });

  list.querySelectorAll("[data-save-edit]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.saveEdit;
      const subjEl = $(`edit-subj-${id}`);
      const bodyEl = $(`edit-body-${id}`);
      try {
        const res = await api(`/api/drafts/${encodeURIComponent(id)}/content`, {
          method: "PATCH",
          body: JSON.stringify({
            subject: subjEl ? subjEl.value : "",
            body: bodyEl ? bodyEl.value : "",
          }),
        });
        state.editingDraftId = null;
        await loadSummary();
        await loadDrafts();
        toast(res.gate_passed ? "Текст сохранён и прошёл проверку Gate" : "Текст сохранён, но Gate нашёл замечания", !res.gate_passed);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });

  list.querySelectorAll("[data-draft-id]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/drafts/${btn.dataset.draftId}`, {
          method: "PATCH",
          body: JSON.stringify({ status: btn.dataset.status }),
        });
        await loadSummary();
        await loadDrafts();
      } catch (e) {
        toast(e.message, true);
      }
    });
  });

  list.querySelectorAll("[data-dnc-domain]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const dom = btn.dataset.dncDomain;
      const draftId = btn.dataset.draftReject;
      try {
        const curDnc = state.config?.compliance?.do_not_contact || [];
        if (!curDnc.includes(dom)) curDnc.push(dom);
        const payload = collectConfigPayload();
        payload.compliance.do_not_contact = curDnc;
        await api("/api/niche/save", { method: "POST", body: JSON.stringify(payload) });
        if (draftId) {
          await api(`/api/drafts/${draftId}`, { method: "PATCH", body: JSON.stringify({ status: "rejected" }) });
        }
        await loadSummary();
        await loadDrafts();
        toast(`Домен ${dom} добавлен в стоп-лист (DNC)`);
      } catch (e) {
        toast(e.message, true);
      }
    });
  });
}

/* ── Каталог сигналов ── */
async function loadSignals() {
  const data = await api("/api/signals");
  const tbody = $("signals-tbody");
  const statusLabels = {
    confirmed: ["Активен", "green"],
    proposed: ["Отключён", "amber"],
    rejected: ["Скрыт", "rose"],
  };
  const catFallback = {
    hiring: "Найм и рост команды",
    marketing: "Маркетинг и реклама",
    website: "Сайт и конверсия",
    profile: "Технический аудит сайта",
    seo: "Поисковая видимость",
    maps: "Карты и геосервисы",
    people: "Руководители и контакты",
    contacts: "Контакты и мессенджеры",
    tech: "Технологический стек",
  };
  tbody.innerHTML = data.items
    .map((s) => {
      const [lbl, tone] = statusLabels[s.status] || [s.status, ""];
      const nextStatus = s.status === "confirmed" ? "rejected" : "confirmed";
      const btnText = s.status === "confirmed" ? "Отключить" : "Включить";
      const catText = s.category_label || catFallback[s.category] || "Аудит";
      return `
        <tr>
          <td>
            <div class="cell-primary">${esc(s.label)}</div>
          </td>
          <td>${esc(catText)}</td>
          <td class="cell-sub">${esc(s.description)}</td>
          <td><span class="tag ${tone}">${esc(lbl)}</span></td>
          <td class="col-actions">
            <button type="button" class="ghost-btn sm" data-sig="${esc(s.code)}" data-next="${nextStatus}">
              ${btnText}
            </button>
          </td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll("[data-sig]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/signals/${btn.dataset.sig}`, {
        method: "PATCH",
        body: JSON.stringify({ status: btn.dataset.next }),
      });
      await loadSummary();
      await loadSignals();
    });
  });
}

/* ── Пакетная подготовка предложений ── */
async function triggerBatchProposals() {
  const channel = $("sel-draft-channel")?.value || "telegram";
  const cityScope = state.activeCityFilter || "";
  const btn = $("btn-generate-drafts");
  if (btn) btn.disabled = true;
  try {
    await runTrackedJob(
      "/api/jobs/drafts",
      { channel, city: cityScope, limit: 50 },
      async (job) => {
        const ready = job.result?.drafts_ready ?? 0;
        if (cityScope) {
          state.draftCityFilter = cityScope;
          populateDraftCityDropdown();
        }
        toast(`Подготовлено предложений: ${ready}`);
        switchTab("drafts");
      }
    );
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ── Привязка событий интерфейса ── */
function bindEvents() {
  document.querySelectorAll(".nav-btn").forEach((b) => {
    b.addEventListener("click", () => switchTab(b.dataset.tab));
  });

  $("project-pill-btn").addEventListener("click", () => switchTab("project"));

  document.querySelectorAll("#db-subtabs .seg-btn").forEach((b) => {
    b.addEventListener("click", () => switchSubView(b.dataset.subview));
  });

  document.querySelectorAll("#draft-status-pills .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#draft-status-pills .seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.draftStatusFilter = b.dataset.status || "";
      loadDrafts();
    });
  });

  $("flt-draft-channel").addEventListener("change", (e) => {
    state.draftChannelFilter = e.target.value;
    loadDrafts();
  });

  $("flt-draft-city").addEventListener("change", (e) => {
    state.draftCityFilter = e.target.value;
    loadDrafts();
  });

  $("sel-require-signal").addEventListener("change", () => loadCompanies());

  $("btn-export-csv").addEventListener("click", () => {
    const q = state.draftStatusFilter ? `?status=${encodeURIComponent(state.draftStatusFilter)}` : "";
    downloadFileSafe(`/api/export/csv${q}`, "friday_proposals.csv");
  });

  $("btn-export-md").addEventListener("click", () => {
    const q = state.draftStatusFilter ? `?status=${encodeURIComponent(state.draftStatusFilter)}` : "";
    downloadFileSafe(`/api/export/markdown${q}`, "friday_proposals.md");
  });

  // Обучение системы по реальному сайту пользователя (с браузерным чтением при изоляции облака)
  $("btn-train-niche").addEventListener("click", async () => {
    const btn = $("btn-train-niche");
    btn.disabled = true;
    try {
      const website = $("train-website").value.trim();
      const cities = $("train-cities").value.split(",").map((x) => x.trim()).filter(Boolean);
      let crawledTitle = "";
      let crawledText = "";

      if (website) {
        const normalizedUrl = /^https?:\/\//i.test(website) ? website : `https://${website}`;
        const md = await fetchTextWithTimeout(`https://r.jina.ai/${normalizedUrl}`, 8000);
        if (md && md.length > 80) {
          const tm = md.match(/^Title:\s*(.+)$/m);
          crawledTitle = tm ? tm[1].trim() : "";
          crawledText = md.slice(0, 4000);
        }
      }

      await runTrackedJob(
        "/api/jobs/train",
        {
          website,
          niche_hint: $("train-niche-hint").value.trim(),
          brief: $("train-brief").value.trim(),
          cities,
          crawled_title: crawledTitle,
          crawled_text: crawledText,
        },
        async () => {
          toast("Сайт изучен, профиль проекта обновлён");
        }
      );
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("btn-save-niche").addEventListener("click", async () => {
    const btn = $("btn-save-niche");
    btn.disabled = true;
    try {
      await api("/api/niche/save", {
        method: "POST",
        body: JSON.stringify(collectConfigPayload()),
      });
      await loadSummary();
      toast("Настройки проекта сохранены");
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // Поиск компаний с автоматической фильтрацией по выбранному городу и настраиваемым лимитом
  $("btn-discover").addEventListener("click", async () => {
    const btn = $("btn-discover");
    btn.disabled = true;
    const reqQuery = $("inp-query").value.trim();
    const reqCity = $("inp-city").value.trim() || (state.config?.icp?.cities?.[0] || "Москва");
    const reqLimit = Number($("sel-limit").value || 30);
    const reqProvider = $("sel-provider").value;

    // Сразу фокусируем таблицу на выбранном городе поиска!
    state.activeCityFilter = reqCity;
    renderCityFilterPills();

    try {
      await runTrackedJob(
        "/api/jobs/discover",
        {
          query: reqQuery,
          city: reqCity,
          provider: reqProvider,
          limit: reqLimit,
        },
        async (job) => {
          if (job.result?.network_isolated) {
            const qEff = job.result.query || reqQuery;
            const cEff = job.result.city || reqCity;
            const extraQ = job.result.search_queries || state.config?.icp?.search_queries || [];
            const liveRes = await browserLiveDiscover(qEff, cEff, reqLimit, reqProvider, extraQ);
            state.activeCityFilter = cEff;
            renderCityFilterPills();
            await refreshWorkspaceTables();
            if ((liveRes.finalCount || liveRes.inserted_companies || 0) > 0) {
              toast(`Найдено и проверено организаций (${cEff}): ${liveRes.finalCount || liveRes.inserted_companies}`);
            }
            return;
          }
          await refreshWorkspaceTables();
          toast(job.stage || `Поиск по г. ${reqCity} завершён`);
        }
      );
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // Импорт собственного файла с привязкой к текущему городу
  $("btn-trigger-upload").addEventListener("click", () => $("inp-import-file").click());

  $("inp-import-file").addEventListener("change", async () => {
    const fileInput = $("inp-import-file");
    if (!fileInput.files || !fileInput.files.length) return;
    const targetCity = $("inp-city").value.trim() || state.activeCityFilter || "";
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("city", targetCity);
    try {
      const res = await fetch("/api/import/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Ошибка импорта");
      fileInput.value = "";
      if (targetCity) state.activeCityFilter = targetCity;
      await loadSummary();
      await refreshWorkspaceTables();
      toast(`Импортировано организаций: ${data.inserted_companies}`);
    } catch (e) {
      toast(e.message, true);
    }
  });

  // Глубокий аудит сайтов и поиск ФИО ЛПР (с браузерным чтением при изоляции облака)
  $("btn-enrich").addEventListener("click", async () => {
    const btn = $("btn-enrich");
    btn.disabled = true;
    const cityParam = state.activeCityFilter ? `?city=${encodeURIComponent(state.activeCityFilter)}` : "";
    try {
      await runTrackedJob(`/api/jobs/enrich${cityParam}`, {}, async (job) => {
        if (job.result?.network_isolated && Array.isArray(job.result.targets) && job.result.targets.length > 0) {
          const targets = job.result.targets;
          const stats = await auditCompanySitesLive(targets, (done, tot, curName) => {
            const pct = 30 + Math.round(((done + 1) / Math.max(1, tot)) * 65);
            renderJobCard({
              title: "Аудит сайтов и поиск ЛПР",
              stage: `Проверка сайта и контактов (${Math.min(done + 1, tot)} из ${tot}): ${curName}`,
              percent: Math.min(96, pct),
              status: "running",
              steps: [
                { label: "Анализ сайтов и стека", status: "active" },
                { label: "Извлечение ФИО ЛПР и контактов", status: "active" },
                { label: "Оценка релевантности", status: "pending" },
              ],
            });
          });
          renderJobCard({
            title: "Аудит сайтов и поиск ЛПР",
            stage: `Проверено живых сайтов: ${stats.audited}${stats.pruned ? ` · Удалено недоступных: ${stats.pruned}` : ""}`,
            percent: 100,
            status: "completed",
            steps: [
              { label: "Анализ сайтов и стека", status: "done" },
              { label: "Извлечение ФИО ЛПР и контактов", status: "done" },
              { label: "Оценка релевантности", status: "done" },
            ],
          });
          await loadSummary();
          await refreshWorkspaceTables();
          toast(`Аудит завершён: проверено ${stats.audited} сайтов`);
          return;
        }
        await refreshWorkspaceTables();
        toast(job.stage || "Аудит сайтов и контактов обновлён");
      });
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("btn-generate-drafts").addEventListener("click", triggerBatchProposals);
  $("btn-drafts-tab-generate").addEventListener("click", triggerBatchProposals);

  // Очистка текущего города или всей базы
  $("btn-clear-db").addEventListener("click", async () => {
    const scopeText = state.activeCityFilter
      ? `Удалить все найденные карточки по г. «${state.activeCityFilter}»?`
      : "Очистить всю базу найденных компаний и предложений (настройки проекта сохранятся)?";
    if (!window.confirm(scopeText)) return;
    try {
      const res = await api("/api/workspace/clear", {
        method: "POST",
        body: JSON.stringify({ city: state.activeCityFilter }),
      });
      state.activeCityFilter = "";
      await loadSummary();
      await refreshWorkspaceTables();
      toast(`Удалено организаций: ${res.deleted_companies}`);
    } catch (e) {
      toast(e.message, true);
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  try {
    await Promise.all([loadPresets(), loadSummary()]);
    const primaryCity = state.config?.icp?.cities?.[0] || "";
    if (primaryCity && state.cities.some((x) => x.city.toLowerCase() === primaryCity.toLowerCase())) {
      state.activeCityFilter = primaryCity;
      renderCityFilterPills();
    }
    if (state.config && (state.summary?.companies_total || 0) > 0) {
      switchTab("workspace");
    }
  } catch (e) {
    toast(e.message || "Ошибка загрузки данных", true);
  }
});
