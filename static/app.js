'use strict';

// All remote and generated content is inserted as text; links allow HTTP(S) only.
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const node = (tag, className = '', text = '') => {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined && text !== null) result.textContent = String(text);
  return result;
};
const add = (parent, ...children) => { children.flat().filter(Boolean).forEach(child => parent.append(child)); return parent; };
const safeUrl = value => { try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; } };
const link = (text, url, className = '') => { const result = node('a', className, text); const safe = safeUrl(url); if (safe) { result.href = safe; result.target = '_blank'; result.rel = 'noopener noreferrer'; } else { result.removeAttribute('href'); } return result; };
const button = (text, className, handler) => { const result = node('button', className, text); result.type = 'button'; result.addEventListener('click', handler); return result; };
const badge = (text, color = '') => node('span', `badge ${color}`, text);
const storage = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(`aim-radar:${key}`)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(`aim-radar:${key}`, JSON.stringify(value)); return true; } catch { toast('浏览器未允许本地存储，阅读标记仅在本次打开时保留。'); return false; } }
};
const fallbackDirections = [
  { id: 'remote_sensing', name: '遥感多模态基础模型及下游适配' },
  { id: 'uav', name: '无人机多模态环境感知与世界动作模型' },
  { id: 'few_shot', name: '预训练基础模型的小样本下游泛化' },
  { id: 'hallucination', name: '多模态大模型幻觉抑制' },
  { id: 'noisy_labels', name: '标签噪声关联学习' }
];
const shortDirections = { remote_sensing: '遥感基础模型', uav: '无人机感知与动作', few_shot: '小样本泛化', hallucination: '多模态幻觉抑制', noisy_labels: '标签噪声学习' };
const directionColors = ['#779460', '#729a9c', '#a197b3', '#c0a174', '#a78088'];
const sourceNames = { huggingface: 'Hugging Face', arxiv: 'arXiv', openreview: 'OpenReview', proceedings: '会议论文集', news: '全球行业动态' };
const views = {
  overview: ['本周概览', '这周，值得读什么？', '从海量信息中，找到与你的研究真正相关的进展。'],
  papers: ['论文精选', '值得精读的研究进展。', '围绕五个研究方向，追踪论文、公开投稿与新近收录。'],
  news: ['行业动态', '关注变化，也关注影响。', '放眼全球 AI 行业，理解新模型、产品与重要研究事件。'],
  conferences: ['会议雷达', '下一篇好论文，在哪里？', '追踪正式论文集的新增记录，区分首次基线与本期变化。'],
  saved: ['我的阅读清单', '把线索，留给下一次灵感。', '你的个人阅读清单保存在当前浏览器中，可随时导出。'],
  history: ['历史周报', '让每一周的发现，留下来。', '按采集区间回看本地周报，研究进展有迹可循。'],
  settings: ['设置与连接', '为你的研究，设好雷达。', '连接你自己的 Codex，选择信息来源，查看研究方向。']
};
const state = { view: 'overview', profile: { directions: fallbackDirections }, settings: {}, codex: null, reports: [], report: null, job: null, csrf: '', direction: '', source: '', search: '', saved: readSavedItems(), read: storage.get('read', {}), useCodex: storage.get('use-codex', true), loaded: false, bootError: false, submitting: false };
let pollTimer, pollFailures = 0, toastTimer, reportLoadVersion = 0;
Object.assign(state, { tracking: [], trackingLoaded: false, trackingError: '', trackingJob: null, trackingOptOut: storage.get('tracking-opt-out', {}), trackingMigrating: false, analysis: {}, analysisLoading: new Set(), analysisJobs: {}, detailItem: null });

function readSavedItems() {
  const result = {};
  const migrated = storage.get('saved-legacy-migrated', false);
  (migrated ? ['saved'] : ['favorites', 'reading-list', 'saved']).forEach(name => {
    const stored = storage.get(name, {});
    (Array.isArray(stored) ? stored : Object.values(stored || {})).forEach(item => { if (item && typeof item === 'object' && (item.id || item.url || item.title)) result[String(item.id || item.url || item.title)] = item; });
  });
  if (!migrated) {
    try { localStorage.setItem('aim-radar:saved', JSON.stringify(result)); localStorage.setItem('aim-radar:saved-legacy-migrated', 'true'); } catch { /* Read-only storage still permits this session to use the legacy list. */ }
  }
  return result;
}

function shanghaiToday() {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
  return `${parts.find(p => p.type === 'year').value}-${parts.find(p => p.type === 'month').value}-${parts.find(p => p.type === 'day').value}`;
}
function dateShift(value, days) { const date = new Date(`${value}T12:00:00Z`); date.setUTCDate(date.getUTCDate() + days); return date.toISOString().slice(0, 10); }
function setPeriod(period) {
  const today = shanghaiToday();
  const weekday = (new Date(`${today}T12:00:00Z`).getUTCDay() + 6) % 7;
  let start = dateShift(today, -weekday), end = today;
  if (period === 'last') { start = dateShift(start, -7); end = dateShift(start, 6); }
  if (period === 'seven') start = dateShift(today, -6);
  $('#start-date').value = start; $('#end-date').value = end;
  $$('.segmented button').forEach(item => item.classList.toggle('selected', item.dataset.period === period));
}
function dateText(value) {
  if (!value) return '日期未提供';
  const raw = String(value);
  // Date-only source calendar days stay intact; timestamps use the report timezone.
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw.replaceAll('-', '.');
  if (/^\d{4}-\d{2}-\d{2}T/.test(raw)) {
    const date = new Date(raw);
    if (!Number.isNaN(date.getTime())) {
      const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date);
      return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type).value).join('.');
    }
  }
  return raw;
}
function dateTime(value) { if (!value) return '时间未提供'; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date); }
function rangeText(report) { return `${dateText(report.start)} — ${dateText(report.end)}`; }
function directions() { return state.profile.directions?.length ? state.profile.directions : fallbackDirections; }
function directionName(id) { return directions().find(item => item.id === id)?.name || id; }
function trimText(text, max = 170) { const value = typeof text === 'string' ? text : ''; return value.length > max ? value.slice(0, max) + '…' : value; }
function asText(value) { if (typeof value === 'string') return value; if (Array.isArray(value)) return value.map(asText).join('；'); if (value && typeof value === 'object') return value.title || value.name || value.summary || value.message || ''; return value == null ? '' : String(value); }
function toast(message) { clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').classList.remove('hidden'); toastTimer = setTimeout(() => $('#toast').classList.add('hidden'), 4200); }
function notice(message = '', info = false) { $('#global-notice').textContent = message; $('#global-notice').className = `notice ${info ? 'info' : ''} ${message ? '' : 'hidden'}`; }

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || 30000);
  try {
    const response = await fetch(path, { cache: 'no-store', ...options, signal: controller.signal, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf, ...options.headers } });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(data.error || `请求失败（${response.status}）`); error.status = response.status; throw error; }
    return data;
  } catch (error) { if (error.name === 'AbortError') throw new Error('请求超时。请检查本地服务，稍后重试。'); throw error; }
  finally { clearTimeout(timer); }
}
async function bootstrap(initial = true) {
  try {
    const data = await api('/api/bootstrap');
    state.profile = data.profile || state.profile; state.settings = data.settings || {}; state.codex = data.codex; state.reports = data.reports || []; state.csrf = data.csrf_token || ''; state.loaded = true; state.bootError = false;
    if (initial) state.report = data.latest_report || null;
    state.job = data.active_job?.type && data.active_job.type !== 'report' ? null : data.active_job || null;
    if (data.active_job?.type && data.active_job.type !== 'report') resumeFeatureJob(data.active_job);
    updateConnection(); updateCounts(); renderView(); renderReportContext(); renderJob();
    if (state.job && ['queued', 'running'].includes(state.job.status)) schedulePoll(400);
    if (!initial) notice('已重新连接本地服务。', true);
    loadTracking().then(migrateSavedTracking);
  } catch (error) {
    state.loaded = true; state.bootError = true; updateConnection();
    notice(`无法连接本地服务：${error.message || '请确认 AIM Radar 服务正在运行。'}`);
    renderView();
  }
}
function updateConnection() {
  const label = $('#connection-label'), dot = $('#connection-button .status-dot');
  if (state.bootError) { label.textContent = '本地服务未连接'; dot.className = 'status-dot error'; }
  else if (state.codex?.available && state.codex?.authenticated) { label.textContent = 'Codex 已连接'; dot.className = 'status-dot'; }
  else if (state.codex?.available) { label.textContent = state.codex.authenticated === false ? 'Codex 待登录' : 'Codex 待检查'; dot.className = 'status-dot error'; }
  else { label.textContent = 'Codex 尚未连接'; dot.className = 'status-dot muted'; }
}
function updateCounts() { $('#nav-paper-count').textContent = state.report ? (state.report.papers || []).length : '—'; $('#nav-saved-count').textContent = Object.keys(state.saved).length; }
function setView(view) {
  if (!views[view]) return;
  state.view = view; state.search = ''; state.direction = ''; state.source = '';
  if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
  renderView(); window.scrollTo({ top: 0, behavior: 'auto' });
}
function renderView() {
  const [name, title, description] = views[state.view];
  $('#breadcrumb').textContent = name; $('#page-title').textContent = title; $('#page-description').textContent = description;
  $$('.nav-item').forEach(item => { const active = item.dataset.view === state.view; item.classList.toggle('active', active); item.setAttribute('aria-current', active ? 'page' : 'false'); });
  const reporting = ['overview', 'papers', 'news', 'conferences'].includes(state.view);
  $('#period-bar').classList.toggle('hidden', !reporting); renderReportContext();
  const container = $('#content-view'); container.replaceChildren();
  if (!state.loaded) { add(container, node('div', 'skeleton'), node('div', 'skeleton')); return; }
  if (state.view === 'settings') renderSettings(container);
  else if (state.view === 'history') renderHistory(container);
  else if (state.view === 'saved') renderSaved(container);
  else if (state.view === 'conferences') renderConferences(container);
  else if (state.view === 'news') renderNews(container);
  else if (state.view === 'papers') renderPapers(container);
  else renderOverview(container);
}
function renderReportContext() {
  const visible = state.report && ['overview', 'papers', 'news', 'conferences'].includes(state.view);
  $('#report-context').classList.toggle('hidden', !visible);
  if (!visible) return;
  $('#report-identity').textContent = `${rangeText(state.report)} · ${dateTime(state.report.created_at)} 生成${state.report.status === 'partial' ? ' · 采集 / 分析存在限制' : ''}`;
  const codex = state.report.analysis_mode === 'codex';
  $('#analysis-badge').textContent = codex ? '✦ Codex 重点分析 + 规则初筛' : '规则筛选 · 未经 Codex 分析';
  $('#analysis-badge').className = `badge ${codex ? '' : 'amber'}`;
}
function sectionHeading(title, english, description = '', actionText = '', actionHandler) {
  const result = node('div', 'section-heading'), left = node('div');
  const h2 = node('h2', '', title); add(h2, node('span', '', english)); add(left, h2);
  if (description) add(left, node('p', '', description)); add(result, left);
  if (actionText) add(result, button(actionText, '', actionHandler)); return result;
}
function emptyState(title, description, action = true, compact = false) {
  const result = node('div', `empty-state ${compact ? 'compact' : ''}`);
  add(result, node('div', 'empty-radar'), node('h3', '', title), node('p', '', description));
  if (action) add(result, button(state.bootError ? '重新连接本地服务 ↻' : '✦ 开始生成第一份周报', 'button primary', state.bootError ? () => bootstrap(false) : generate));
  return result;
}
function reportMissing() { return emptyState('本周的发现，从这里开始', '选好上方日期，雷达将检索公开研究来源，按研究组的五个方向筛选、整理并保存周报。'); }
function renderStats(container) {
  const report = state.report, sources = (report?.sources || []).filter(item => item.status !== 'disabled');
  const count = report ? new Set((report.papers || []).flatMap(item => item.directions || [])).size : null;
  const values = [
    ['精选论文', report ? (report.papers || []).length : '—', '与你的研究方向相关', '▤'],
    ['行业动态', report ? (report.news || []).length : '—', '全球 AI 重要进展', '◉'],
    ['方向覆盖', report ? `${count} / 5` : '— / 5', report ? '本期论文匹配的研究方向' : '五个方向，持续追踪', '⌘'],
    ['信息来源', report ? `${sources.filter(item => item.status === 'ok').length} / ${sources.length}` : '—', report ? '完整采集 / 本期启用来源' : '生成后显示真实采集状态', '⌁']
  ];
  const result = node('section', 'overview-stats'); result.setAttribute('aria-label', '本期周报统计');
  values.forEach(([label, value, note, symbol]) => {
    const stat = node('div', 'stat'), heading = node('div', 'stat-label', label); add(heading, node('span', 'stat-symbol', symbol)); add(stat, heading, node('div', 'stat-value', value), node('p', 'stat-note', note)); add(result, stat);
  }); add(container, result);
}
function renderOverview(container) {
  renderStats(container);
  const grid = node('div', 'dashboard-grid'), main = node('div', 'main-column'), right = node('aside', 'right-column');
  add(main, sectionHeading('本周论文精选', 'PAPERS TO READ', '', state.report ? '查看全部 ↗' : '', () => setView('papers')));
  if (state.report?.summary) add(main, node('div', 'summary-box', asText(state.report.summary)));
  add(main, directionChips());
  if (!state.report) add(main, reportMissing());
  else { add(main, filters('papers'), node('div', 'result-meta'), node('div', 'paper-list')); }
  const note = node('div', 'overview-note'); add(note, node('strong', '', '每个信号，都有出处'), node('span', '', '首次发表、当周更新与社区收录分别标注；公开投稿不代表正式录用。分析依据公开摘要和公告，阅读原文后再形成判断。')); add(main, note);
  if ((state.report?.news || []).length) {
    const news = node('section', 'overview-section-gap'); add(news, sectionHeading('行业观察', 'INDUSTRY SIGNALS', '全球视野，独立于论文方向筛选', '全部动态 ↗', () => setView('news')));
    state.report.news.slice(0, 2).forEach(item => { const card = node('article', 'mini-news'); add(card, node('h3', '', item.title), node('p', '', trimText(item.summary || item.abstract, 140)), button('查看变化与影响 ↗', '', () => openDetail(item))); add(news, card); }); add(main, news);
  }
  add(right, sourcePanel(), highlightsPanel(), profilePanel()); add(grid, main, right); add(container, grid);
  if (state.report) renderResults();
}
function directionChips() {
  const result = node('div', 'direction-grid'); result.setAttribute('aria-label', '按研究方向筛选');
  const all = button('全部方向', `direction-chip ${!state.direction ? 'selected' : ''}`, () => chooseDirection('')); all.dataset.direction = ''; all.setAttribute('aria-pressed', String(!state.direction)); add(result, all);
  directions().forEach((direction, index) => { const chip = button(shortDirections[direction.id] || direction.name, `direction-chip ${state.direction === direction.id ? 'selected' : ''}`, () => chooseDirection(direction.id)); chip.style.setProperty('--chip-color', directionColors[index % 5]); chip.title = direction.name; chip.dataset.direction = direction.id; chip.setAttribute('aria-pressed', String(state.direction === direction.id)); add(result, chip); });
  return result;
}
function chooseDirection(value) { state.direction = value; $$('.direction-chip').forEach(item => { item.classList.toggle('selected', item.dataset.direction === value); item.setAttribute('aria-pressed', String(item.dataset.direction === value)); }); renderResults(); }
function filters(kind) {
  const result = node('div', 'filter-bar'), search = node('div', 'search-input'), input = node('input');
  input.type = 'search'; input.placeholder = kind === 'news' ? '搜索动态、模型或机构…' : '搜索论文标题、关键词或作者…'; input.value = state.search; input.setAttribute('aria-label', kind === 'news' ? '搜索行业动态' : '搜索论文');
  input.addEventListener('input', event => { state.search = event.target.value; renderResults(); }); add(search, node('span', '', '⌕'), input);
  const select = node('select'); select.setAttribute('aria-label', '按信息来源筛选'); add(select, new Option('全部来源', ''));
  const items = kind === 'news' ? state.report?.news : state.view === 'saved' ? Object.values(state.saved) : state.report?.papers;
  [...new Set((items || []).map(item => item.source).filter(Boolean))].sort().forEach(source => add(select, new Option(source, source)));
  select.value = state.source; select.addEventListener('change', event => { state.source = event.target.value; renderResults(); }); add(result, search, select); return result;
}
function renderPapers(container) {
  add(container, sectionHeading('论文精选', 'RESEARCH PAPERS', '结合方向匹配与研究价值排序；完整来源与日期依据见详情。'));
  if (!state.report) { add(container, reportMissing()); return; }
  add(container, directionChips(), filters('papers'), node('div', 'result-meta'), node('div', 'paper-list')); renderResults();
}
function renderNews(container) {
  add(container, sectionHeading('全球 AI 行业动态', 'INDUSTRY SIGNALS', '关注实际变化、研究影响与下一步行动；不限于五个论文方向。'));
  if (!state.report) { add(container, reportMissing()); return; }
  add(container, filters('news'), node('div', 'result-meta'), node('div', 'paper-list')); renderResults();
}
function filteredItems() {
  let items = state.view === 'saved' ? Object.values(state.saved) : state.view === 'news' ? (state.report?.news || []) : (state.report?.papers || []);
  const query = state.search.trim().toLocaleLowerCase();
  return items.filter(item => (!state.direction || (item.directions || []).includes(state.direction)) && (!state.source || item.source === state.source) && (!query || [item.title, item.summary, item.abstract, ...(item.authors || [])].map(asText).join(' ').toLocaleLowerCase().includes(query)));
}
function renderResults() {
  const list = $('.paper-list', $('#content-view')); if (!list) return;
  const items = filteredItems(); list.replaceChildren();
  const overview = state.view === 'overview', shown = overview ? items.slice(0, 4) : items;
  shown.forEach(item => add(list, paperCard(item)));
  if (!shown.length) {
    const hasFilters = state.search || state.direction || state.source;
    add(list, emptyState(hasFilters ? '没有找到匹配的内容' : state.view === 'saved' ? '你的阅读清单，等一个好问题' : state.view === 'news' ? '本期暂无确认的行业动态' : '本期暂无匹配的论文', hasFilters ? '试试其他关键词、方向或信息来源。' : state.view === 'saved' ? '在论文或动态中点击「加入清单」，把值得追踪的研究留下来。' : '可以查看本期来源状态，或调整日期范围后再次采集。', false, true));
  }
  const meta = $('.result-meta', $('#content-view'));
  if (meta) { meta.replaceChildren(); add(meta, node('span', '', `${items.length} 条${overview && items.length > 4 ? ' · 展示前 4 条' : ''}`), node('span', '', state.view === 'saved' ? '当前浏览器 · 个人标记' : state.report?.analysis_mode === 'codex' ? '按优先级排序 · 分析模式逐条标注' : '按规则相关性排序')); }
}
function itemKey(item) { return String(item.id || item.url || item.title); }
function itemIsNews(item) { return item._is_news || item.kind === 'news' || item.kind === 'industry' || (state.report?.news || []).some(entry => itemKey(entry) === itemKey(item)); }
function dateBasisLabel(item) {
  return { original_publication: '首次发表', updated_this_week: '本期更新', hf_featured_day: '社区收录', first_observed: '首次观测', official_publication: '官方发布', public_online_this_week: '本期首次公开', accepted_this_week: '本期录用标记' }[item.date_basis] || '来源日期';
}
function publicStatus(item) {
  const value = String(item.status || '').toLowerCase();
  if (/录用|同行评审|预印本|公开投稿|官方论文集|撤回|撤稿|拒稿|社区推荐/.test(value)) return String(item.status);
  if (/withdraw|reject/.test(value)) return '撤稿 / 未录用';
  if (/accept|published|proceeding/.test(value) && !/unaccepted|not_accepted/.test(value)) return '已录用 / 正式发表';
  if (/submit|review|preprint|arxiv/.test(value) || /openreview|arxiv/i.test(item.source || '') || /arxiv\.org|openreview\.net/.test(item.url || '')) return '预印本 / 公开投稿';
  return item.venue || (itemIsNews(item) ? '官方 / 公开信息' : '录用状态未确认');
}
function paperCard(item) {
  const key = itemKey(item), news = itemIsNews(item), card = node('article', `paper-card ${news ? 'news-card' : ''} ${state.read[key] ? 'read-card' : ''}`);
  const top = node('div', 'paper-top'), left = node('div'), kicker = node('div', 'paper-kicker');
  (item.directions || []).slice(0, 2).forEach((id, index) => add(kicker, badge(shortDirections[id] || directionName(id), index ? 'blue' : '')));
  if (news && !(item.directions || []).length) add(kicker, badge('全球 AI 动态', 'blue'));
  add(kicker, badge(item.source || '公开来源', 'outline'));
  add(kicker, badge(item.ai_enriched ? '✦ Codex 分析' : '规则初筛', item.ai_enriched ? '' : 'amber'));
  add(left, kicker, add(node('h3'), link(item.title || '未命名条目', item.url)));
  add(top, left);
  const score = Number(item.score);
  if (Number.isFinite(score)) { const scoreBlock = node('div', 'score'); add(scoreBlock, node('strong', '', Math.round(score)), node('small', '', news ? '关注指数' : '相关指数')); add(top, scoreBlock); }
  add(card, top);
  const summary = item.summary || item.abstract;
  add(card, node('p', 'summary', trimText(asText(summary) || '来源尚未提供摘要，请打开原文查看。', state.view === 'overview' ? 190 : 260)));
  if (item.why_it_matters) add(card, add(node('div', 'relevance'), node('span', '', '↳'), node('div', '', asText(item.why_it_matters))));
  if (news && item.suggested_action) add(card, node('p', 'next-action', `下一步 · ${asText(item.suggested_action)}`));
  const footer = node('div', 'paper-footer'), meta = node('div', 'paper-meta');
  add(meta, node('span', '', `${dateBasisLabel(item)} ${dateText(item.published_at)}`));
  if (item.original_published_at && dateText(item.original_published_at) !== dateText(item.published_at)) add(meta, node('span', '', `· 首发 ${dateText(item.original_published_at)}`));
  if (!news) add(meta, node('span', '', '·'), node('span', '', publicStatus(item)));
  const tools = node('div', 'paper-tools');
  add(tools, button(news ? '详情 ↗' : '深读与核对 ↗', '', () => openDetail(item)));
  if (!news && trackingFor(item)) add(kicker, badge('跨周追踪中', 'blue'));
  const saveButton = button(state.saved[key] ? '★ 已加入' : '☆ 加入清单', state.saved[key] ? 'is-saved' : '', () => toggleSaved(item)); saveButton.setAttribute('aria-label', `${state.saved[key] ? '移出清单' : '加入清单'}：${item.title}`); saveButton.setAttribute('aria-pressed', String(Boolean(state.saved[key])));
  const readButton = button(state.read[key] ? '✓ 已读' : '标记已读', '', () => { state.read[key] = !state.read[key]; storage.set('read', state.read); renderResults(); }); readButton.setAttribute('aria-pressed', String(Boolean(state.read[key])));
  add(tools, saveButton, readButton); add(footer, meta, tools); add(card, footer); return card;
}
function toggleSaved(item) {
  const key = itemKey(item); if (state.saved[key]) { delete state.saved[key]; toast('已从个人阅读清单移出'); } else { state.saved[key] = { ...item, _saved_at: new Date().toISOString(), _report_id: item._report_id || reportIdFor(item), _is_news: itemIsNews(item) }; toast('已加入个人阅读清单'); }
  storage.set('saved', state.saved); updateCounts(); renderResults();
  if (state.saved[key] && !itemIsNews(item) && !trackingStopped(item)) migrateSavedTracking();
}
function sourcePanel() {
  const panel = node('section', 'side-panel'); add(panel, add(node('h2'), document.createTextNode('信息来源'), node('span', '', 'SOURCE HEALTH')));
  const sources = state.report?.sources || [];
  const defaults = Object.entries(sourceNames).map(([id, name]) => ({ id, name, status: state.settings.sources?.[id] === false ? 'disabled' : 'pending' }));
  const appendSource = (target, source) => {
    const row = node('div', `source-item ${source.status === 'disabled' ? 'disabled' : ''}`), statusNames = { ok: '已完成', partial: '部分完成', error: '采集失败', disabled: '已关闭', pending: '待采集' };
    add(row, node('span', 'source-orb', (source.name || sourceNames[source.id] || 'S').slice(0, 1).toUpperCase()), node('strong', '', source.name || sourceNames[source.id] || source.id), node('span', `source-state ${['error', 'partial'].includes(source.status) ? 'problem' : ''}`, `${statusNames[source.status] || source.status}${typeof source.count === 'number' && source.status !== 'disabled' ? ` · ${source.count}` : ''}`));
    if (source.message) row.title = source.message; add(target, row);
  };
  const displayed = sources.length ? sources : defaults;
  const core = displayed.filter(source => Object.hasOwn(sourceNames, source.id));
  const industry = displayed.filter(source => !Object.hasOwn(sourceNames, source.id));
  core.forEach(source => appendSource(panel, source));
  if (industry.length) {
    const more = node('details', 'source-details'), complete = industry.filter(source => source.status === 'ok').length;
    add(more, node('summary', '', `行业动态 · ${complete} / ${industry.length} 个来源完整`));
    industry.forEach(source => appendSource(more, source)); add(panel, more);
  }
  const warnings = state.report?.warnings || [];
  const failures = sources.filter(source => ['error', 'partial'].includes(source.status));
  const limitations = warnings.length ? warnings.map(asText) : failures.map(source => `${source.name || source.id}：${source.message || '来源未完整采集'}`);
  if (limitations.length) {
    const details = node('details', 'source-details warning-details'), list = node('ul', 'warning-list');
    add(details, node('summary', '', `采集限制与说明 · ${limitations.length} 项`));
    limitations.forEach(warning => add(list, node('li', '', warning))); add(details, list); add(panel, details);
  }
  add(panel, node('p', 'source-description', state.report ? '状态针对当前选中的周报。采集失败或关闭的来源不计入“完整采集”。' : '真实采集状态将在生成后显示。启用来源可在设置中调整。'));
  add(panel, button('管理信息来源 ↗', 'text-button', () => setView('settings'))); return panel;
}
function highlightsPanel() {
  const panel = node('section', 'side-panel'); add(panel, add(node('h2'), document.createTextNode('本期阅读线索'), node('span', '', 'HIGHLIGHTS')));
  const papers = state.report?.papers || [];
  if (!papers.length) add(panel, node('div', 'panel-divider'), node('p', '', '生成周报后，这里会提取值得优先阅读的研究线索，并关联研究组已有工作。'));
  papers.slice(0, 3).forEach((item, index) => { const row = node('div', 'highlight'), content = node('div'); add(content, link(trimText(item.title, 76), item.url), node('small', '', trimText(asText(item.why_it_matters || item.summary), 70))); $('a', content).className = ''; $('a', content).style.cssText = 'font-size:10px;font-weight:500;color:#75876a;line-height:1.8;display:block'; add(row, node('span', '', `0${index + 1}`), content); add(panel, row); }); return panel;
}
function profilePanel() {
  const panel = node('section', 'side-panel profile-panel'); add(panel, node('div', 'profile-title', 'BUILT AROUND YOUR RESEARCH'), node('h3', '', '以研究组为原点，\n发现下一步的可能。'), node('p', '', '五个方向，关联已有工作与潜在实验切口。每条线索都尽量回答：它为什么值得你读？'), link('了解研究组 ↗', state.profile.url || 'https://multimodality.group/', 'profile-link')); return panel;
}
function renderConferences(container) {
  add(container, sectionHeading('正式论文集与会议覆盖', 'CONFERENCE RADAR', '首次采集建立基线；后续采集仅将新增论文集标为变化。'));
  if (!state.report) { add(container, reportMissing()); return; }
  const entries = state.report.conferences || [];
  if (!entries.length) { add(container, emptyState('本期暂无会议追踪记录', '会议来源可能尚未启用或暂不可用。请在设置与本期来源状态中查看覆盖情况。', false, true), sourcePanel()); return; }
  const grid = node('div', 'conference-grid');
  entries.forEach(entry => {
    const card = node('article', 'conference-card');
    const status = entry.baseline ? '首次基线 · 不代表本周新增' : entry.status === 'error' ? '暂不可用' : (entry.new_volumes || []).length ? '发现新增论文集' : entry.status === 'disabled' ? '未启用' : '已检查';
    add(card, badge(status, entry.baseline || entry.status === 'error' ? 'amber' : ''), node('h3', '', entry.name || entry.id), node('p', '', entry.message || '未报告进一步变化。'));
    (entry.new_volumes || []).forEach(volume => { const row = node('div', 'volume-item'); if (typeof volume === 'string') row.textContent = volume; else add(row, link(volume.title || volume.name || volume.id || '新增论文集', volume.url), volume.date ? node('p', '', dateText(volume.date)) : null); add(card, row); });
    if (entry.url) add(card, link('查看官方来源 ↗', entry.url)); add(grid, card);
  }); add(container, grid, node('p', 'footer-note', '会议雷达反映公开论文集与官方页面的可访问状态。论文首次发表、论文集收录和会议录用属于不同事件。'));
}
function renderHistory(container) {
  add(container, sectionHeading('已保存的周报', 'REPORT ARCHIVE', `${state.reports.length} 份本地记录 · 每份周报保留独立采集日期与来源状态。`));
  if (!state.reports.length) { add(container, emptyState('第一份周报，会成为你的研究档案', '选择一个日期区间并生成周报，之后就可以在这里回看与导出。', false)); return; }
  const list = node('div', 'history-list');
  state.reports.forEach(report => {
    const card = node('article', 'history-card'), info = node('div', 'history-info');
    add(info, node('h3', '', rangeText(report)), node('p', '', `${dateTime(report.created_at)} 生成 · ${report.paper_count ?? report.papers?.length ?? '—'} 篇论文 · ${report.news_count ?? report.news?.length ?? '—'} 条动态`));
    add(card, node('div', 'history-date', '▤'), info, badge(report.status === 'partial' ? '含未完成项' : '已保存', report.status === 'partial' ? 'amber' : ''), button(state.report?.id === report.id ? '当前周报 ↗' : '打开周报 ↗', 'button small', () => loadReport(report.id))); add(list, card);
  }); add(container, list);
}
async function loadReport(id) {
  const version = ++reportLoadVersion;
  notice('正在打开周报…', true);
  try { const data = await api(`/api/reports/${encodeURIComponent(id)}`); if (version !== reportLoadVersion) return; state.report = data.report; state.direction = ''; state.search = ''; state.source = ''; updateCounts(); notice(); setView('overview'); }
  catch (error) { if (version === reportLoadVersion) notice(`无法打开周报：${error.message}`); }
}
function renderSaved(container) {
  const toolbar = node('div', 'saved-toolbar'); add(toolbar, node('p', '', '阅读标记保存在当前浏览器。开启跨周追踪后，检查记录另存于本机工作空间。\n旧清单可直接深读，或通过下方入口补充注册追踪。'), button('导出阅读清单 ↓', 'button small', exportSaved)); add(container, toolbar, trackingOverview(), directionChips(), filters('papers'), node('div', 'result-meta'), node('div', 'paper-list')); renderResults();
}
function downloadBlob(text, fileName, type) { const url = URL.createObjectURL(new Blob([text], { type })); const anchor = node('a'); anchor.href = url; anchor.download = fileName; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
function exportSaved() {
  const items = Object.values(state.saved); if (!items.length) { toast('先把感兴趣的条目加入阅读清单。'); return; }
  const lines = ['# AIM Radar · 个人阅读清单', '', `导出日期：${shanghaiToday()}（Asia/Shanghai）`, ''];
  items.forEach(item => { lines.push(`## ${item.title}`, '', `原始来源：${safeUrl(item.url) || '未提供安全链接'}`, `来源：${item.source || '未提供'}；${dateBasisLabel(item)}：${dateText(item.published_at)}`, `已读：${state.read[itemKey(item)] ? '是' : '否'}`, '', asText(item.summary || item.abstract), '', `研究关联：${asText(item.why_it_matters) || '未提供'}`, ''); });
  downloadBlob(lines.join('\n'), `AIM-Radar-阅读清单-${shanghaiToday()}.md`, 'text/markdown;charset=utf-8'); toast('已导出阅读清单');
}
function renderSettings(container) {
  const layout = node('div', 'settings-layout'), main = node('div'), aside = node('aside');
  const form = node('form'); form.id = 'settings-form';
  const codex = node('section', 'settings-card'); add(codex, node('h2', '', 'Codex 连接'), node('p', '', '调用这台电脑上已登录的 Codex CLI。研究分析在你的本地会话中运行，无需填写 API Key。'));
  const connection = node('div', 'connection-box'); connection.id = 'settings-connection'; fillConnectionBox(connection); add(codex, connection);
  const fields = node('div', 'field-row'); add(fields, formField('Codex 路径', 'codex_path', state.settings.codex_path || '', '留空使用系统 PATH 中的 codex；也可填写完整可执行文件路径。'), formField('分析模型', 'model', state.settings.model || 'gpt-6.1-sol', '本机 Codex 账号须具备所选模型的访问权限。')); add(codex, fields);
  const use = node('div', 'source-setting'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.id = 'use-codex'; checkbox.checked = state.useCodex; checkbox.addEventListener('change', () => { state.useCodex = checkbox.checked; storage.set('use-codex', state.useCodex); });
  const useLabel = node('label'); useLabel.htmlFor = 'use-codex'; add(useLabel, node('strong', '', '生成时使用 Codex 深度分析'), node('small', '', '关闭后仅运行来源检索与规则筛选，周报会明确标记分析模式。')); add(use, checkbox, useLabel); add(codex, use);
  const test = button('检查 Codex 连接 ↻', 'button small', testCodex); test.id = 'test-codex'; add(codex, add(node('div', 'settings-actions'), test), node('div', 'form-status')); $('.form-status', codex).id = 'codex-test-status'; add(form, codex);
  const sources = node('section', 'settings-card'); add(sources, node('h2', '', '公开信息来源'), node('p', '', '论文按五个研究方向筛选；行业动态单独追踪全球 AI 进展。来源关闭、失效或限流都会在周报中显示。'));
  const descriptions = { huggingface: '每日社区收录论文；收录日期与首次发表日期分别记录。', arxiv: '公开预印本与当期更新，预印本不代表正式录用。', openreview: '公开投稿与评审记录，录用状态以官方信息为准。', proceedings: '公开会议论文集，首次运行建立基线。', news: '官方模型与产品公告、研究机构博客及其他公开动态。' };
  Object.entries(sourceNames).forEach(([id, name]) => { const row = node('div', 'source-setting'), input = node('input'); input.type = 'checkbox'; input.id = `source-${id}`; input.name = `source-${id}`; input.checked = state.settings.sources?.[id] !== false; const label = node('label'); label.htmlFor = input.id; add(label, node('strong', '', name), node('small', '', descriptions[id])); add(row, input, label); add(sources, row); });
  add(sources, formField('OpenReview 会议', 'openreview_venues', (state.settings.openreview_venues || ['ICLR', 'ICML', 'NeurIPS']).join(', '), '用逗号分隔，例如 ICLR, ICML, NeurIPS。'), formField('补充公开资讯 Feed', 'extra_feeds', (state.settings.extra_feeds || []).join('\n'), '每行一个公开 HTTPS RSS / Atom 地址。空白表示不添加。', true)); add(form, sources);
  const actions = node('div', 'settings-actions'), submit = node('button', 'button primary', '保存设置'); submit.type = 'submit'; submit.id = 'save-settings'; add(actions, submit, node('span', 'settings-message', '设置将用于下一次采集。')); $('.settings-message', actions).id = 'save-status'; add(form, actions); form.addEventListener('submit', saveSettings); add(main, form);
  const profile = node('section', 'side-panel'); add(profile, add(node('h2'), document.createTextNode('研究组画像'), node('span', '', 'RESEARCH PROFILE')), node('p', 'footer-note', state.profile.name || 'AIM · 多模态智能研究组'));
  directions().forEach(direction => { const block = node('div', 'profile-direction'); add(block, node('h3', '', direction.name)); if (direction.description) add(block, node('p', '', direction.description)); if (direction.anchors?.length) { const anchors = node('div', 'anchor-list'); direction.anchors.forEach(anchor => add(anchors, node('span', '', asText(anchor)))); add(block, anchors); } add(profile, block); }); add(profile, link('查看研究组网站 ↗', state.profile.url || 'https://multimodality.group/', 'profile-link')); add(aside, profile); add(layout, main, aside); add(container, layout);
}
function formField(label, name, value, help, multiline = false) { const field = node('label', 'field', label), input = node(multiline ? 'textarea' : 'input'); input.name = name; input.value = value; if (multiline) input.rows = 3; else input.type = 'text'; input.autocomplete = 'off'; add(field, input, node('span', '', help)); return field; }
function fillConnectionBox(box) {
  box.replaceChildren(); const codex = state.codex;
  add(box, node('strong', '', state.bootError ? '本地服务未连接' : codex?.available && codex?.authenticated ? '● Codex 已连接' : codex?.available ? '○ Codex 需要检查登录状态' : '○ 尚未找到 Codex'));
  add(box, node('div', '', codex?.message || '在终端安装并登录 Codex CLI 后，点击检查连接。'));
  if (codex?.path) add(box, node('code', '', codex.path));
  if (codex?.version) add(box, node('div', '', `版本 ${codex.version}`));
}
async function testCodex() {
  const test = $('#test-codex'), status = $('#codex-test-status'); test.disabled = true; status.textContent = '正在检查本机 Codex 与登录状态…';
  try { const data = await api('/api/codex/test', { method: 'POST', body: '{}', timeout: 65000 }); state.codex = data.codex; updateConnection(); if ($('#settings-connection')) fillConnectionBox($('#settings-connection')); if ($('#codex-test-status')) $('#codex-test-status').textContent = state.codex?.authenticated ? '连接检查完成，可以开始生成周报。' : state.codex?.message || '连接检查完成，请确认登录状态。'; }
  catch (error) { if ($('#codex-test-status')) { $('#codex-test-status').textContent = `检查失败：${error.message}`; $('#codex-test-status').classList.add('error-text'); } }
  finally { if ($('#test-codex')) $('#test-codex').disabled = false; }
}
async function saveSettings(event) {
  event.preventDefault(); const form = event.currentTarget, submit = $('#save-settings'), status = $('#save-status');
  const values = new FormData(form), sources = {}; Object.keys(sourceNames).forEach(id => { sources[id] = values.has(`source-${id}`); });
  const feeds = String(values.get('extra_feeds') || '').split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  if (feeds.some(feed => !safeUrl(feed) || !feed.startsWith('https://'))) { status.textContent = '补充 Feed 必须是完整的公开 HTTPS 地址。'; status.classList.add('error-text'); return; }
  const payload = { model: String(values.get('model') || '').trim(), codex_path: String(values.get('codex_path') || '').trim(), sources, openreview_venues: String(values.get('openreview_venues') || '').split(/[,，\n]/).map(value => value.trim()).filter(Boolean), extra_feeds: feeds };
  if (!payload.model) { status.textContent = '请填写分析模型。'; status.classList.add('error-text'); return; }
  submit.disabled = true; status.textContent = '正在保存…'; status.classList.remove('error-text');
  try { const data = await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) }); state.settings = data.settings; if ($('#save-status')) $('#save-status').textContent = '✓ 已保存，将用于下一次采集。'; toast('设置已保存'); }
  catch (error) { if ($('#save-status')) { $('#save-status').textContent = `保存失败：${error.message}`; $('#save-status').classList.add('error-text'); } }
  finally { if ($('#save-settings')) $('#save-settings').disabled = false; }
}
function openDetail(item) {
  state.detailItem = item;
  const container = $('#detail-content'); container.replaceChildren();
  const kicker = node('div', 'paper-kicker'); (item.directions || []).forEach(id => add(kicker, badge(directionName(id)))); add(kicker, badge(item.source || '公开来源', 'outline'));
  const title = node('h2', 'detail-title', item.title || '研究详情'); title.id = 'detail-title';
  const dates = [`${dateBasisLabel(item)}：${dateText(item.published_at)}`];
  if (item.original_published_at && (item.date_basis !== 'original_publication' || dateText(item.original_published_at) !== dateText(item.published_at))) dates.push(`首次公开发表：${dateText(item.original_published_at)}`);
  if (item.updated_at) dates.push(`来源更新：${dateText(item.updated_at)}`);
  if (item.featured_day) dates.push(`Hugging Face 收录日：${dateText(item.featured_day)}（来源站点日历）`);
  if (item.online_at) dates.push(`OpenReview 公开日期：${dateText(item.online_at)}`);
  if (item.acceptance_marked_at) dates.push(`官方标记录用日期：${dateText(item.acceptance_marked_at)}`);
  if (item.discovered_at) dates.push(`本机检索发现：${dateText(item.discovered_at)}`);
  add(container, kicker, title, node('div', 'detail-meta', `${(item.authors || []).map(asText).join(' · ')}${item.authors?.length ? '\n' : ''}${publicStatus(item)}\n${dates.join('\n')}\n时间戳按 Asia/Shanghai 显示；纯日期保留来源日历。`));
  const section = (label, value) => { if (!value) return; const result = node('section', 'detail-section'); add(result, node('h3', '', label), node('p', '', asText(value))); add(container, result); };
  section('本条分析方式', item.ai_enriched ? 'Codex 基于公开摘要与来源信息整理研究关联及建议；评分为阅读优先级参考。' : '本条采用规则匹配与来源摘要，未由 Codex 单独分析。评分为方向相关性参考。');
  section(itemIsNews(item) ? '本次发生了什么' : '研究摘要', item.summary || item.abstract || '来源未提供摘要。');
  section('为什么与我们相关', item.why_it_matters);
  if (item.related_work?.length) section('关联的已有工作', item.related_work);
  section('建议的下一步', item.suggested_action);
  if (!itemIsNews(item)) add(container, analysisPanel(item), trackingPanel(item));
  if (item.abstract && item.abstract !== item.summary) {
    const abstract = node('details', 'detail-section source-details');
    add(abstract, node('summary', '', '展开完整原始摘要'), node('p', '', item.abstract)); add(container, abstract);
  }
  const sourceSection = node('section', 'detail-section'); add(sourceSection, node('h3', '', '来源与日期依据'));
  const sourceList = item.sources?.length ? item.sources : [{ name: item.source || '原始来源', url: item.url, published_at: item.published_at, date_basis: item.date_basis }];
  sourceList.forEach(source => { const row = node('div', 'detail-source'); add(row, link(source.name || source.source || '原始来源', source.url || item.url), node('span', '', `${dateBasisLabel(source)}：${dateText(source.published_at)}${source.message ? ` · ${source.message}` : ''}`)); add(sourceSection, row); }); add(container, sourceSection);
  section('阅读提示', '摘要与关联分析基于公开摘要、元信息和公告，不等同于完整论文评审。社区收录或公开投稿不代表正式录用。');
  const footer = node('div', 'detail-footer'); add(footer, link('打开原始来源 ↗', item.url, 'button primary'), button(state.saved[itemKey(item)] ? '★ 移出阅读清单' : '☆ 加入阅读清单', 'button', () => { toggleSaved(item); openDetail(item); })); add(container, footer);
  if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
  if (!itemIsNews(item)) loadAnalysis(item);
}
// Deep reading and tracking remain separate jobs from weekly report generation.
function reportIdFor(item) {
  if (item._report_id) return item._report_id;
  return [...(state.report?.papers || []), ...(state.report?.news || [])].some(entry => itemKey(entry) === itemKey(item)) ? state.report.id : undefined;
}
function workIdentity(item) {
  let aid = String(item.arxiv_id || '').trim().replace(/^arxiv:/i, '');
  if (!aid) {
    try { const url = new URL(item.url); if (/(^|\.)arxiv\.org$/i.test(url.hostname)) aid = decodeURIComponent(url.pathname).replace(/^\/(abs|pdf|html)\//, '').replace(/\.pdf$/i, ''); } catch { /* Other sources retain their stable item ID. */ }
  }
  aid = aid.replace(/v\d+$/i, '');
  return /^(?:\d{4}\.\d{4,5}|[a-z-]+\/\d{7})$/i.test(aid) ? `arxiv:${aid.toLowerCase()}` : itemKey(item);
}
function trackingStopped(item) { return Boolean(state.trackingOptOut[workIdentity(item)] || state.trackingOptOut[itemKey(item)]); }
function setTrackingStopped(item, stopped) {
  if (stopped) { state.trackingOptOut[workIdentity(item)] = true; state.trackingOptOut[itemKey(item)] = true; }
  else {
    delete state.trackingOptOut[workIdentity(item)]; delete state.trackingOptOut[itemKey(item)];
    Object.values(state.saved).filter(saved => workIdentity(saved) === workIdentity(item)).forEach(saved => delete state.trackingOptOut[itemKey(saved)]);
  }
  storage.set('tracking-opt-out', state.trackingOptOut);
}
async function itemRequest(path, item, extra = {}) {
  const payload = { report_id: reportIdFor(item), item_id: itemKey(item), item, ...extra };
  try { return await api(path, { method: 'POST', body: JSON.stringify(payload) }); }
  catch (error) {
    if (!payload.report_id || error.status !== 400 || !/报告|条目不存在/.test(error.message)) throw error;
    // A saved source snapshot remains usable after its original report is removed.
    delete payload.report_id;
    return api(path, { method: 'POST', body: JSON.stringify(payload) });
  }
}
function detailRefresh(item) {
  if ($('#detail-dialog').open && itemKey(state.detailItem || {}) === itemKey(item)) {
    const dialog = $('#detail-dialog'), scroll = dialog.scrollTop;
    const expanded = $$('details', dialog).filter(section => section.open).map(section => section.className);
    const settings = $('.tracking-settings', dialog), repositories = settings && $('textarea', settings), confirmation = settings && $('[name="repositories_confirmed"]', settings);
    const draft = settings?.open && repositories ? { repositories: repositories.value, confirmed: confirmation?.checked } : null;
    const focused = document.activeElement === repositories;
    const selection = focused ? [repositories.selectionStart, repositories.selectionEnd] : null;
    openDetail(item);
    $$('details', dialog).forEach(section => { if (expanded.includes(section.className)) section.open = true; });
    if (draft) {
      const next = $('.tracking-settings textarea', dialog), nextConfirmation = $('[name="repositories_confirmed"]', dialog);
      if (next) { next.value = draft.repositories; if (focused) { next.focus({ preventScroll: true }); next.setSelectionRange(...selection); } }
      if (nextConfirmation) nextConfirmation.checked = draft.confirmed;
    }
    dialog.scrollTop = scroll;
  }
}
function listText(parent, values, empty = '') {
  const items = (Array.isArray(values) ? values : values ? [values] : []).map(asText).filter(Boolean);
  if (!items.length) { if (empty) add(parent, node('p', 'muted-copy', empty)); return; }
  const list = node('ul', 'research-list'); items.forEach(value => add(list, node('li', '', value))); add(parent, list);
}
function labelledValue(parent, label, value, fallback = '未知 · 现有证据未提供') {
  const row = node('div', 'evidence-row'); add(row, node('strong', '', label), node('p', '', asText(value) || fallback)); add(parent, row);
}
function jobStatus(job) {
  const result = node('div', 'research-progress'); result.setAttribute('role', 'status');
  const progress = Number(job.progress);
  add(result, node('strong', '', job.message || (job.status === 'queued' ? '等待开始…' : '正在检查来源与证据…')));
  if (Number.isFinite(progress)) { const bar = node('progress'); bar.max = 100; bar.value = Math.max(0, Math.min(100, progress)); bar.setAttribute('aria-label', '检查进度'); add(result, bar, node('span', '', `${Math.round(bar.value)}%`)); }
  return result;
}
function analysisPanel(item) {
  const key = itemKey(item), analysis = state.analysis[key] || item.research_analysis, job = state.analysisJobs[key];
  const panel = node('section', 'detail-section research-panel');
  add(panel, node('h3', '', '动机与实验核对'), node('p', 'muted-copy', '按需读取这篇论文，逐项核对作者主张、对应实验与证据位置，并与组内真实工作比较。'));
  const run = button(analysis ? '重新核对这篇论文 ↻' : '开始动机与实验核对', 'button primary small', () => startAnalysis(item));
  run.disabled = Boolean(job && ['queued', 'running'].includes(job.status)) || state.analysisLoading.has(key); add(panel, run);
  if (state.analysisLoading.has(key) && !analysis) add(panel, node('p', 'inline-status', '正在读取已保存的核对结果…'));
  if (job && ['queued', 'running'].includes(job.status)) { add(panel, jobStatus(job)); if (analysis) add(panel, node('p', 'inline-status', '正在重新核对；下方暂时保留上次已保存的结果。')); }
  if (job?.status === 'failed' || job?.error) add(panel, node('p', 'inline-status error-text', `本次核对未完成：${job.error || job.message}。可重试；已有结果保留。`));
  if (!analysis) { add(panel, badge('证据不足 · 尚未核对全文', 'amber'), node('p', 'inline-status', '当前摘要可辅助判断阅读价值；实验是否支撑主张，需取得全文后核对。')); return panel; }
  const full = analysis.full_text || {}, hasFull = ['ok', 'available', 'fetched', 'ready', 'success'].includes(full.status);
  const provenance = node('div', 'evidence-provenance');
  add(provenance, badge(!hasFull ? '证据不足 · 未取得可核对全文' : analysis.status === 'completed' ? '已取得全文 · 已逐项核对' : '已取得全文 · 尚未完成证据核对', hasFull && analysis.status === 'completed' ? '' : 'amber'));
  if (analysis.judgment_basis) add(provenance, node('p', '', analysis.judgment_basis));
  if (analysis.created_at) add(provenance, node('span', '', `核对时间 ${dateText(analysis.created_at)} ${dateTime(analysis.created_at).split(' ').pop()}`));
  add(provenance, node('p', '', '本次核对使用最近取得的正文，不限定为周报当时的论文版本。'));
  if (full.message) add(provenance, node('p', '', full.message));
  if (full.version_note) add(provenance, node('p', '', full.version_note));
  if (safeUrl(full.url)) add(provenance, link('本次使用的原文 ↗', full.url));
  add(panel, provenance);
  labelledValue(panel, '研究动机', analysis.motivation, '证据不足 · 尚未确认研究动机');
  labelledValue(panel, '要解决的问题', analysis.problem, '证据不足 · 尚未确认具体问题');
  const claims = Array.isArray(analysis.claims) ? analysis.claims : [];
  add(panel, node('h4', '', '作者主张 → 对应实验 → 支撑判定'));
  if (!hasFull) add(panel, node('p', 'evidence-empty', '全文证据不足，暂不判定实验支持程度。请打开来源，或稍后重试核对。'));
  else if (!claims.length) add(panel, node('p', 'evidence-empty', '未提取到可核对的主张与实验；不能据此认定已获支撑。'));
  else claims.forEach((claim, index) => {
    const card = node('article', 'claim-card');
    const support = { supported: ['有证据支撑', ''], partial: ['部分支撑', 'amber'], unsupported: ['未充分支撑', 'red'], insufficient: ['证据不足', 'outline'] }[claim.support] || ['证据不足', 'outline'];
    add(card, add(node('div', 'claim-heading'), node('h4', '', `主张 ${index + 1}`), badge(...support)));
    labelledValue(card, '作者主张', claim.claim);
    labelledValue(card, '对应实验 / 已验证点', claim.experiment);
    labelledValue(card, '证据定位', claim.evidence_location, '未提供可复查的章节、表格或图号');
    if (claim.evidence_quote) add(card, node('blockquote', 'evidence-quote', claim.evidence_quote));
    add(card, node('strong', 'row-label', '缺口与待验证项')); listText(card, claim.gaps, '未列出具体缺口；仍需结合原文确认。'); add(panel, card);
  });
  if (analysis.limitations?.length) { add(panel, node('h4', '', '本次核对的限制')); listText(panel, analysis.limitations); }
  add(panel, comparisonPanel(analysis)); return panel;
}
function comparisonPanel(analysis) {
  const panel = node('section', 'comparison-panel'); add(panel, node('h3', '', '与组内工作逐项对比'));
  const comparisons = Array.isArray(analysis.anchor_comparisons) ? analysis.anchor_comparisons : [];
  if (!comparisons.length) { add(panel, node('p', 'evidence-empty', '尚无可核实的组内论文对比。完成全文核对后显示实际关联工作、来源和未知项。')); return panel; }
  comparisons.forEach(entry => {
    const card = node('article', 'comparison-card'), anchor = entry.anchor;
    add(card, node('h4', '', asText(anchor) || '关联组内工作'));
    const source = entry.source_url || anchor?.url; if (safeUrl(source)) add(card, link('核对组内论文来源 ↗', source));
    labelledValue(card, '比较依据', entry.evidence_basis);
    const table = node('table', 'comparison-table'), caption = node('caption', 'sr-only', '当前论文与组内工作的差异'); add(table, caption);
    const body = node('tbody'); [['任务', 'task'], ['数据', 'data'], ['方法', 'method'], ['监督方式', 'supervision'], ['评估指标', 'evaluation']].forEach(([label, field]) => { const row = node('tr'); add(row, node('th', '', label), node('td', '', asText(entry[field]) || '未知 · 待查原文')); add(body, row); }); add(table, body); add(card, table);
    labelledValue(card, '可比性与边界', entry.comparability);
    labelledValue(card, '可借鉴点', entry.borrowable_points || entry.takeaways);
    labelledValue(card, '最小实验方案', entry.minimal_experiment);
    add(card, node('strong', 'row-label', '未知项')); listText(card, entry.unknowns, '未列明；不表示没有未知项。'); add(panel, card);
  }); return panel;
}
async function loadAnalysis(item) {
  const key = itemKey(item); if (Object.hasOwn(state.analysis, key) || state.analysisLoading.has(key)) return;
  state.analysisLoading.add(key);
  try { const data = await api(`/api/analysis/${encodeURIComponent(key)}`); if (data.analysis || !state.analysis[key]) state.analysis[key] = data.analysis || null; }
  catch (error) {
    if (!Object.hasOwn(state.analysis, key)) state.analysis[key] = item.research_analysis || null;
    if (!['queued', 'running'].includes(state.analysisJobs[key]?.status)) state.analysisJobs[key] = { status: 'failed', error: `读取已有结果失败：${error.message}` };
  }
  finally { state.analysisLoading.delete(key); detailRefresh(item); }
}
async function startAnalysis(item) {
  const key = itemKey(item); state.analysisJobs[key] = { status: 'queued', message: '正在启动本篇核对…' }; detailRefresh(item);
  try {
    const data = await itemRequest('/api/analysis', item);
    state.analysisJobs[key] = data.job; detailRefresh(item);
    await followFeatureJob(data.job, job => { state.analysisJobs[key] = job; if (job.analysis) state.analysis[key] = job.analysis; detailRefresh(item); });
  } catch (error) { state.analysisJobs[key] = { status: 'failed', error: error.message }; detailRefresh(item); }
}
async function followFeatureJob(initial, update) {
  let job = initial;
  while (job && ['queued', 'running'].includes(job.status)) {
    await new Promise(resolve => setTimeout(resolve, 1800));
    job = (await api(`/api/jobs/${encodeURIComponent(job.id)}`)).job; update(job);
  }
}
function resumeFeatureJob(job) {
  if (job.type === 'tracking') {
    state.trackingJob = job;
    followFeatureJob(job, current => { state.trackingJob = current; if (state.view === 'saved') renderView(); if (state.detailItem) detailRefresh(state.detailItem); }).then(loadTracking).catch(error => { state.trackingJob = { status: 'failed', error: error.message }; if (state.view === 'saved') renderView(); });
  } else if (job.item_id) {
    state.analysisJobs[job.item_id] = job;
    followFeatureJob(job, current => { state.analysisJobs[job.item_id] = current; if (current.analysis) state.analysis[job.item_id] = current.analysis; if (state.detailItem && itemKey(state.detailItem) === job.item_id) detailRefresh(state.detailItem); }).catch(error => { state.analysisJobs[job.item_id] = { status: 'failed', error: error.message }; if (state.detailItem) detailRefresh(state.detailItem); });
  }
}
async function migrateSavedTracking() {
  if (!state.trackingLoaded || state.trackingError || state.trackingMigrating) return;
  state.trackingMigrating = true;
  try {
    for (const item of Object.values(state.saved).filter(item => !itemIsNews(item) && !trackingStopped(item))) {
      if (trackingFor(item) || trackingStopped(item)) continue;
      try { const data = await itemRequest('/api/tracking', item, { tracked: true }); if (data.item) state.tracking.push(data.item); }
      catch { /* Keep legacy saved items available; the explicit registration action can retry. */ }
    }
  } finally { state.trackingMigrating = false; if (state.view === 'saved') renderView(); else renderResults(); if (state.detailItem) detailRefresh(state.detailItem); }
}
function trackingFor(item) { return state.tracking.find(entry => entry.id === itemKey(item) || workIdentity(entry.item || {}) === workIdentity(item)); }
function trackingStatus(entry) {
  if (entry.errors?.length) return ['检查失败 / 部分来源失败', 'red'];
  if ((entry.changes || []).length) return ['发现真实变化', ''];
  if (!entry.last_checked_at) return ['待首次检查', 'outline'];
  const snapshot = entry.snapshot || {};
  const known = snapshot.arxiv_version || snapshot.openreview_status || (snapshot.repositories || []).some(repo => repo.commit_sha) || ['code', 'weights', 'data'].some(field => snapshot[field]?.status && snapshot[field].status !== 'unknown');
  if (!known) return ['尚未建立可比较基线', 'amber'];
  return ['已建立基线 · 暂无新变化', 'blue'];
}
async function loadTracking() {
  try { const data = await api('/api/tracking'); state.tracking = [...new Map((data.items || []).map(entry => [workIdentity(entry.item || { id: entry.id }), entry])).values()]; state.trackingLoaded = true; state.trackingError = ''; }
  catch (error) { state.trackingLoaded = true; state.trackingError = error.message; }
  if (state.view === 'saved') renderView(); else renderResults();
  if (state.detailItem) detailRefresh(state.detailItem);
}
function trackingOverview() {
  const panel = node('section', 'tracking-overview');
  const heading = node('div', 'feature-heading'); add(heading, node('h2', '', '跨周追踪'), button('刷新状态 ↻', 'text-button', loadTracking)); add(panel, heading);
  add(panel, node('p', 'muted-copy', '追踪记录保存在本机。本地服务运行期间每 24 小时复查，也可随时手动检查。首次成功观测只建立基线，之后才记录变化。'));
  if (state.trackingError) add(panel, node('p', 'inline-status error-text', `暂时无法读取追踪记录：${state.trackingError}`));
  else if (!state.trackingLoaded) add(panel, node('p', 'inline-status', '正在读取本机追踪记录…'));
  const unsynced = Object.values(state.saved).filter(item => !itemIsNews(item) && !trackingFor(item) && !trackingStopped(item));
  const actions = node('div', 'feature-actions');
  if (unsynced.length) add(actions, button(`为 ${unsynced.length} 篇已收藏论文开启追踪`, 'button small', registerSaved));
  const check = button('立即检查所有追踪论文 ↻', 'button small', () => checkTracking()); check.disabled = !state.tracking.length || Boolean(state.trackingJob && ['queued', 'running'].includes(state.trackingJob.status)); add(actions, check); add(panel, actions);
  if (state.trackingJob && ['queued', 'running'].includes(state.trackingJob.status)) add(panel, jobStatus(state.trackingJob));
  if (state.trackingJob?.status === 'failed') add(panel, node('p', 'inline-status error-text', state.trackingJob.error || state.trackingJob.message || '检查未完成，可重试。'));
  if (!state.tracking.length) add(panel, node('p', 'inline-status', '尚未开启追踪。在论文详情中确认代码仓库并开启，或为已收藏论文批量开启。'));
  state.tracking.forEach(entry => {
    const row = node('div', 'tracking-row'); add(row, button(entry.item?.title || entry.id, 'tracked-title', () => openDetail({ ...entry.item, _report_id: entry.report_id })), badge(...trackingStatus(entry)), node('span', 'tracking-time', entry.last_checked_at ? `上次检查 ${dateTime(entry.last_checked_at)}` : '尚未检查')); add(panel, row);
  }); return panel;
}
async function registerSaved() {
  const items = Object.values(state.saved).filter(item => !itemIsNews(item) && !trackingFor(item) && !trackingStopped(item)); let count = 0, failures = [];
  for (const item of items) {
    if (trackingFor(item) || trackingStopped(item)) continue;
    try { const data = await itemRequest('/api/tracking', item, { tracked: true }); if (data.item) { state.tracking.push(data.item); count++; } }
    catch (error) { failures.push(`${trimText(item.title, 40)}：${error.message}`); }
  }
  await loadTracking(); toast(`已为 ${count} 篇论文开启追踪${failures.length ? `，${failures.length} 篇未成功` : ''}`);
  if (failures.length) notice(failures.join('\n'));
}
function trackingPanel(item) {
  const entry = trackingFor(item), panel = node('section', 'detail-section research-panel');
  const heading = node('div', 'feature-heading'); add(heading, node('h3', '', '跨周追踪与变化提醒'), badge(entry ? trackingStatus(entry)[0] : '未开启追踪', entry ? trackingStatus(entry)[1] : 'outline')); add(panel, heading);
  add(panel, node('p', 'muted-copy', '持续复查论文版本、公开状态与已确认代码仓库。代码、权重和数据分别记录；检查失败不会算作内容变化。'));
  if (state.trackingError) add(panel, node('p', 'inline-status error-text', `追踪记录暂不可用：${state.trackingError}`));
  if (entry?.last_checked_at) add(panel, node('p', 'inline-status', `最近检查 ${dateTime(entry.last_checked_at)}`));
  if (entry?.repositories?.length) add(panel, badge(entry.confirmed ? '仓库归属已确认 · 未验证实现完整性' : '仓库归属待确认', entry.confirmed ? 'blue' : 'amber'));
  if (entry) {
    const snapshot = entry.snapshot || {}, assets = node('div', 'asset-grid');
    [['代码', 'code'], ['权重', 'weights'], ['数据', 'data']].forEach(([label, field]) => {
      const value = snapshot[field], block = node('div', 'asset-status'); add(block, node('strong', '', label), node('span', '', assetText(value))); if (value && typeof value === 'object' && safeUrl(value.url)) add(block, link('查看来源 ↗', value.url)); add(assets, block);
    }); add(panel, assets);
    if (snapshot.arxiv_version) labelledValue(panel, '论文版本', snapshot.arxiv_version);
    if (snapshot.openreview_status) labelledValue(panel, '公开评审 / 录用状态', snapshot.openreview_status);
    (snapshot.repositories || []).forEach(repo => {
      const block = node('div', 'repository-snapshot');
      add(block, link(repo.url || '已确认仓库', repo.url));
      if (repo.commit_sha) labelledValue(block, '最近提交', repo.commit_sha);
      if (repo.release) labelledValue(block, '最近发布', repo.release);
      add(panel, block);
    });
    if (entry.errors?.length) { const errors = node('div', 'tracking-errors'); add(errors, node('strong', '', '检查失败（未计为变化）')); listText(errors, entry.errors.map(error => typeof error === 'string' ? error : `${error.source || error.url || '来源'}：${error.message || error.error || '无法读取'}`)); add(panel, errors); }
    add(panel, changesDetails(entry));
  }
  const settings = node('details', 'tracking-settings'); add(settings, node('summary', '', entry ? '监督设置 · 编辑与确认代码仓库' : '监督设置 · 开启追踪'));
  const form = node('form'), repositoryField = formField('代码仓库地址', 'repositories', repositoryUrls(entry, item).join('\n'), '每行一个 https://github.com/作者/仓库，最多 5 个。请核对作者归属；留空表示暂未确认仓库。', true);
  const confirmRow = node('label', 'confirm-repository'), confirm = node('input'); confirm.type = 'checkbox'; confirm.name = 'repositories_confirmed'; confirm.checked = Boolean(entry?.confirmed); $('textarea', repositoryField).addEventListener('input', () => { confirm.checked = false; }); add(confirmRow, confirm, node('span', '', '我已核对这些仓库属于该论文，可纳入后续检查'));
  const feedback = node('p', 'inline-status'); feedback.setAttribute('role', 'status');
  const submit = node('button', 'button primary small', entry ? '保存监督设置' : '开启跨周追踪'); submit.type = 'submit';
  add(form, repositoryField, confirmRow, submit, feedback);
  form.addEventListener('submit', async event => {
    event.preventDefault(); const repositories = $('[name="repositories"]', form).value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    if (repositories.length > 5 || repositories.some(url => !/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/.test(url))) { feedback.textContent = '请填写有效的 https://github.com/作者/仓库 地址，最多 5 个。'; return; }
    if (repositories.length && !confirm.checked) { feedback.textContent = '请先核对并勾选仓库归属，或留空后开启论文追踪。'; return; }
    submit.disabled = true; feedback.textContent = '正在保存…';
    try { await itemRequest('/api/tracking', item, { tracked: true, repositories, confirmed: confirm.checked }); setTrackingStopped(item, false); await loadTracking(); toast('监督设置已保存；下次成功检查将建立或更新基线。'); }
    catch (error) { feedback.textContent = `保存失败：${error.message}`; }
    finally { submit.disabled = false; }
  });
  const candidates = entry?.snapshot?.candidate_repositories || [];
  if (candidates.length) {
    const candidateBox = node('div', 'repository-candidates'); add(candidateBox, node('strong', '', '从论文页面发现的候选仓库 · 归属待确认'), node('p', 'inline-status', '候选可能来自引用或相关工作。请先打开核对，再填入并确认。'));
    candidates.forEach(candidate => {
      const row = node('div', 'candidate-row'); add(row, link(candidate.url, candidate.url));
      if (candidate.evidence_source) add(row, link('发现来源 ↗', candidate.evidence_source));
      add(row, button('填入仓库设置', 'button small', () => {
        const input = $('[name="repositories"]', form), values = input.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
        if (!values.includes(candidate.url)) values.push(candidate.url); input.value = values.join('\n'); confirm.checked = false; input.focus(); feedback.textContent = '已填入候选，请核对归属、勾选确认后保存。';
      })); add(candidateBox, row);
    }); add(settings, candidateBox);
  }
  add(settings, form); add(panel, settings);
  if (entry) {
    const actions = node('div', 'feature-actions'), check = button('立即检查所有追踪论文 ↻', 'button small', () => checkTracking(item)); check.disabled = Boolean(state.trackingJob && ['queued', 'running'].includes(state.trackingJob.status));
    add(actions, check, button('停止追踪', 'text-button', async () => {
      try { await itemRequest('/api/tracking', item, { tracked: false }); setTrackingStopped(item, true); await loadTracking(); toast('已停止追踪，阅读清单中的论文仍可查看。'); }
      catch (error) { toast(`未能停止追踪：${error.message}`); }
    })); add(panel, actions);
  }
  if (state.trackingJob && ['queued', 'running'].includes(state.trackingJob.status)) add(panel, jobStatus(state.trackingJob)); return panel;
}
function repositoryUrls(entry, item) {
  const values = entry?.repositories ?? entry?.snapshot?.repositories ?? item.repositories ?? (item.code_url ? [item.code_url] : []);
  return (Array.isArray(values) ? values : [values]).map(value => typeof value === 'string' ? value : value?.url).filter(value => safeUrl(value));
}
function assetText(value) {
  if (value == null) return '未知 · 尚无核实记录';
  if (typeof value === 'boolean') return value ? '已发现公开链接' : '未发现公开链接';
  if (typeof value === 'object') return value.message || assetText(value.status ?? value.available);
  return ({ available: '已发现公开链接', open: '已开源', public: '已公开', released: '已发布', unavailable: '尚未发现公开链接', not_found: '尚未发现公开链接', unknown: '未知 · 尚待核实', not_checked: '尚未检查', not_released: '暂未发布', closed: '暂未公开' })[String(value)] || String(value);
}
function changeValue(value) { return value && typeof value === 'object' ? assetText(value) : asText(value); }
function changesDetails(entry) {
  const changes = entry.changes || [], details = node('details', 'change-details');
  add(details, node('summary', '', `查看变化详情 · ${changes.length} 条`));
  if (!changes.length) add(details, node('p', 'inline-status', entry.last_checked_at ? '当前没有记录到真实变化；首次成功检查仅建立基线。' : '首次检查后显示结果。'));
  changes.slice().sort((a, b) => String(b.at || b.detected_at || '').localeCompare(String(a.at || a.detected_at || ''))).forEach(change => {
    const block = node('article', 'change-card');
    if (typeof change === 'string') add(block, node('p', '', change));
    else {
      add(block, node('strong', '', change.title || change.summary || change.message || change.field || change.type || '来源发生变化'));
      if (change.checked_at || change.detected_at || change.at) add(block, node('span', 'tracking-time', dateTime(change.checked_at || change.detected_at || change.at)));
      if (change.before !== undefined || change.old !== undefined) labelledValue(block, '此前', changeValue(change.before ?? change.old), '未记录');
      if (change.after !== undefined || change.new !== undefined) labelledValue(block, '现在', changeValue(change.after ?? change.new), '未记录');
      if (safeUrl(change.url || change.source_url)) add(block, link('复查变化来源 ↗', change.url || change.source_url));
    } add(details, block);
  }); return details;
}
async function checkTracking(item) {
  state.trackingJob = { status: 'queued', message: '正在启动追踪检查…' }; if (state.view === 'saved') renderView(); if (state.detailItem) detailRefresh(state.detailItem);
  const update = job => { state.trackingJob = job; if (state.view === 'saved') renderView(); if (state.detailItem) detailRefresh(state.detailItem); };
  try {
    const data = await api('/api/tracking/check', { method: 'POST', body: '{}' }); update(data.job);
    await followFeatureJob(data.job, update); await loadTracking();
    if (state.trackingJob?.status === 'completed') toast('检查已完成。基线、真实变化与失败项已分别更新。');
  } catch (error) { update({ status: 'failed', error: `检查未完成：${error.message}。已有追踪记录保留，可重试。` }); }
}

function renderJob() {
  const active = state.job && ['queued', 'running'].includes(state.job.status);
  $('#job-panel').classList.toggle('hidden', !active); $('#generate-button').disabled = Boolean(active) || state.submitting || state.bootError || !state.loaded;
  $('#generate-button').textContent = active ? '正在生成…' : state.submitting ? '正在启动…' : '✦ 生成周报';
  if (!active) return;
  const stageNames = { queued: '排队准备中', starting: '正在准备采集', collecting: '正在收集研究进展', fetching: '正在收集研究进展', analyzing: '正在分析研究关联', codex: 'Codex 正在分析研究关联', saving: '正在保存本期周报' };
  $('#job-stage').textContent = stageNames[state.job.stage] || state.job.stage || '正在生成研究周报';
  const progress = Math.max(0, Math.min(100, Number(state.job.progress) || 0)); $('#job-percent').textContent = `${progress}%`; $('#job-progress').style.width = `${progress}%`;
  $('#job-message').textContent = state.job.message || '可以切换页面或稍后回来，采集任务会继续运行。';
}
async function generate() {
  if (state.submitting) return;
  if (state.job && ['queued', 'running'].includes(state.job.status)) { toast('已有一份周报正在生成。'); return; }
  if (state.bootError) { await bootstrap(false); return; }
  const start = $('#start-date').value, end = $('#end-date').value;
  if (!start || !end || start > end) { notice('请选择有效的开始与结束日期，开始日期不能晚于结束日期。'); return; }
  const days = Math.round((new Date(`${end}T12:00:00Z`) - new Date(`${start}T12:00:00Z`)) / 86400000) + 1;
  if (days > 14) { notice('每次最多采集 14 天，请缩短日期区间。'); return; }
  if (end > shanghaiToday()) { notice('结束日期不能晚于北京时间今天。'); return; }
  state.submitting = true; renderJob(); notice();
  try { const data = await api('/api/runs', { method: 'POST', body: JSON.stringify({ start, end, use_codex: state.useCodex }) }); state.job = data.job; pollFailures = 0; schedulePoll(1000); }
  catch (error) { notice(`未能启动采集：${error.message}`); if (error.status === 409) await bootstrap(false); }
  finally { state.submitting = false; renderJob(); }
}
function schedulePoll(delay = 1800) { clearTimeout(pollTimer); pollTimer = setTimeout(pollJob, delay); }
async function pollJob() {
  if (!state.job?.id) return;
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(state.job.id)}`); state.job = data.job; pollFailures = 0; renderJob();
    if (['queued', 'running'].includes(state.job.status)) { schedulePoll(); return; }
    if (state.job.status === 'completed' && state.job.report_id) {
      const completedId = state.job.report_id;
      const [reportData, bootstrapData] = await Promise.all([api(`/api/reports/${encodeURIComponent(completedId)}`), api('/api/bootstrap')]);
      state.report = reportData.report; state.reports = bootstrapData.reports || state.reports; state.codex = bootstrapData.codex || state.codex;
      state.direction = ''; state.search = ''; state.source = ''; updateCounts(); updateConnection();
      if (!['settings', 'saved', 'history'].includes(state.view)) renderView();
      else if (state.view === 'history') renderView();
      renderReportContext(); notice(state.report.status === 'partial' ? '本期周报已保存，采集或分析存在限制。请查看来源状态与说明。' : `周报已保存：${rangeText(state.report)}。`, state.report.status !== 'partial'); toast('本期周报已生成并保存');
    } else notice(`生成未完成：${state.job.error || state.job.message || '任务已停止，请检查设置后重试。'}${state.report ? ' 已保留上一份周报。' : ''}`);
  } catch (error) { pollFailures++; $('#job-message').textContent = `暂时无法读取进度：${error.message}。正在自动重连；现有任务可能仍在运行。`; schedulePoll(Math.min(12000, 2000 * pollFailures)); }
}
function exportReport(format) { if (!state.report) return; const anchor = node('a'); anchor.href = `/api/reports/${encodeURIComponent(state.report.id)}/export?format=${format}`; anchor.download = ''; document.body.append(anchor); anchor.click(); anchor.remove(); }

$('#navigation').addEventListener('click', event => { const nav = event.target.closest('[data-view]'); if (nav) setView(nav.dataset.view); });
$('.brand').addEventListener('click', event => { event.preventDefault(); setView('overview'); });
$$('[data-period]').forEach(item => item.addEventListener('click', () => setPeriod(item.dataset.period)));
['#start-date', '#end-date'].forEach(selector => { $(selector).max = shanghaiToday(); $(selector).addEventListener('change', () => $$('.segmented button').forEach(button => button.classList.remove('selected'))); });
$('#generate-button').addEventListener('click', generate);
$('#connection-button').addEventListener('click', () => state.bootError ? bootstrap(false) : setView('settings'));
$('#close-detail').addEventListener('click', () => $('#detail-dialog').close());
$('#detail-dialog').addEventListener('click', event => { if (event.target === $('#detail-dialog')) { const box = $('#detail-dialog').getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) $('#detail-dialog').close(); } });
$('#export-md').addEventListener('click', () => exportReport('markdown')); $('#export-json').addEventListener('click', () => exportReport('json'));
setPeriod('week');
if (views[location.hash.slice(1)]) state.view = location.hash.slice(1);
renderView(); renderJob(); bootstrap();
