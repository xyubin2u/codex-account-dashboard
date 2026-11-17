(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  const numberFormat = new Intl.NumberFormat('zh-CN');
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const dateFormat = new Intl.DateTimeFormat('zh-CN', {timeZone: timezone, month: '2-digit', day: '2-digit'});
  const timeFormat = new Intl.DateTimeFormat('zh-CN', {timeZone: timezone, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const fullDateFormat = new Intl.DateTimeFormat('zh-CN', {timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const localToday=()=>{const parts=new Intl.DateTimeFormat('en-CA',{timeZone:timezone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const get=k=>parts.find(p=>p.type===k).value;return `${get('year')}-${get('month')}-${get('day')}`;};
  const state = {platform: 'all', filterVersion: 0, range: 'custom', startDate:'2026-11-09',endDate:localToday(),model: 'all', account: 'all', page: 1, limit: 30, live: true,
    metric: 'total_tokens', data: null, busy: false, queued: false, queuedScan: false,
    timer: null, controller: null, updatedAt: null, failed: false, scanFailed: false, scanPending: false,
    rowNodes: new Map(), rowLookup: new Map(), rowsRendered: false, pricingBusy: false,
    heatmapKey: '', modelsKey: '', newCount: 0, disposed: false,
    registeredAccounts: null, knownAccounts: new Map(), accountOptionsKey: ''};

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function svgElement(tag, attributes = {}, text) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function write(id, text) {
    const node = $(id);
    if (node.textContent !== String(text)) node.textContent = text;
  }
  function numeric(value) { return typeof value === 'number' && Number.isFinite(value); }
  function count(value) { return numberFormat.format(numeric(value) ? value : 0); }
  function compact(value) {
    if (!numeric(value)) return '—';
    if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
    if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
    if (value >= 1e4) return `${(value / 1e3).toFixed(1)}K`;
    return count(value);
  }
  function money(value, approx = false) {
    if (!numeric(value)) return '未定价';
    const precision = value !== 0 && Math.abs(value) < 0.0001 ? 8 : value < 1 ? 6 : 4;
    const result = value.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: precision});
    return `${approx ? '≈ ' : ''}$${result}`;
  }
  function percent(value) { return numeric(value) ? `${value.toFixed(1)}%` : '—'; }
  function cachePercent(row) {
    return numeric(row.input_tokens) && row.input_tokens > 0 && numeric(row.cached_input_tokens)
      ? percent(row.cached_input_tokens / row.input_tokens * 100) : '未知';
  }
  function speed(value) { return numeric(value) && value > 0 ? `≈${value.toFixed(value < 10 ? 1 : 0)} tok/s` : '—'; }
  function date(value) { return numeric(value) && value > 0 ? new Date(value * 1000) : null; }
  function dateTime(value) { const d = date(value); return d ? fullDateFormat.format(d) : '时间未知'; }
  function safeLabel(value, fallback) {
    const text = typeof value === 'string' ? value.trim() : '';
    return text && !/[\\/@\r\n]/.test(text) ? text.slice(0, 100) : fallback;
  }
  function modelLabel(value) { return safeLabel(value, '未知模型'); }
  function accountValue(row) { return typeof row.account === 'string' && /^[A-Z]{1,8}$/.test(row.account) && row.account_source !== 'conflict' ? row.account : null; }
  function appLabel(row) { return row.platform === 'agy' || row.app === 'AGY' ? 'AGY' : 'Codex'; }
  function platformLabel(row) { return appLabel(row) === 'AGY' ? 'AGY' : 'GPT'; }
  function accountLabel(row) { return `${platformLabel(row)} ${accountValue(row) ? `账号 ${accountValue(row)}` : '未归属'}`; }
  function modelValues(models) { return [...new Set(models.map(item => typeof item === 'string' ? item : item && item.model).filter(item => typeof item === 'string' && item))]; }
  function renderPlatform() {
    write('platform-scope', {gpt: 'GPT · Codex 本机调用', agy: 'AGY · 本机已记录调用', all: 'GPT + AGY · 按平台分别核对账号'}[state.platform]);
    const basis = {
      gpt: '默认参考 OpenAI Standard API 美元价 · 已核 2026-11-09 · 长上下文自动计价 · 缓存写入按 30m 参考；不含 Fast、区域及工具费用，非订阅扣款。手动价以设置为准。',
      agy: 'AGY 按对应 API 模型参考价：Google / Anthropic 官方美元价；缓存与长上下文按各供应商规则。缺价保留未定价，非订阅扣款或实际账单。',
      all: '合并显示 GPT 与 AGY；GPT 按 OpenAI 官方 / 手动价，AGY 按对应 API 模型参考价（Google / Anthropic 官方价）；缓存与长上下文按各供应商规则。仅汇总已定价部分，非订阅扣款或实际账单。'
    };
    write('cost-basis', basis[state.platform]);
    $('open-pricing').disabled = state.platform !== 'gpt' || !state.data || !modelValues(state.data.available_models || []).length;
    $('open-pricing').title = state.platform === 'gpt' ? '设置 GPT 模型参考单价' : '切到 GPT 可设置其模型单价；AGY 使用 Google / Anthropic 官方 API 参考价';
  }
  function accountEvidence(row) {
    return row.account_source === 'conflict' ? '存在冲突' : accountValue(row) && row.account_source === 'process' ? '本机运行进程核对' : '缺少可靠归属';
  }
  function providerLabel(row) {
    const provider = safeLabel(row.provider, '');
    return provider && provider !== 'unknown' ? provider : '会话日志';
  }
  function query() {
    return new URLSearchParams({platform: state.platform, range: ['today','yesterday'].includes(state.range)?state.range:'custom', model: state.model, account: state.account, tz: timezone, page: String(state.page), limit: String(state.limit),...(!['today','yesterday'].includes(state.range)?{start:state.startDate,end:state.endDate}:{})}).toString();
  }
  function rangeLabel() { return state.range==='custom'?`${state.startDate} 至 ${state.endDate}（含当天）`:{today: '今日', yesterday: '昨日', '7d': '近 7 天', '30d': '近 30 天', '365d': '近一年', all: '全部记录'}[state.range]; }

  async function request(path, options = {}, signal) {
    const response = await fetch(path, {...options, signal, cache: 'no-store', credentials: 'same-origin'});
    if (!response.ok) throw new Error('local-service-unavailable');
    return response.json();
  }
  const postOptions = body => ({method: 'POST', headers: {'Content-Type': 'application/json', 'X-Quota-Request': 'local-dashboard'}, body: JSON.stringify(body)});

  function updateAccountOptions(data = {}) {
    for (const row of [...(data.available_accounts || []), ...(data.accounts || []), ...(data.rows || [])]) {
      const label = accountValue(row);
      if (label) state.knownAccounts.set(`${row.platform === 'agy' ? 'agy' : 'gpt'}:${label}`, label);
    }
    const labels = new Map();
    const includesPlatform = platform => state.platform === 'all' || state.platform === platform;
    for (const [key, label] of state.knownAccounts) {
      if (includesPlatform(key.split(':')[0])) labels.set(label, {current: false});
    }
    for (const row of state.registeredAccounts || []) {
      const platform = row.platform === 'agy' ? 'agy' : 'gpt';
      if (!includesPlatform(platform) || !/^[A-Z]{1,8}$/.test(row.label)) continue;
      labels.set(row.label, {...labels.get(row.label), current: true});
    }
    if (!['all', 'unknown'].includes(state.account) && !labels.has(state.account)) labels.set(state.account, {current: false});
    const choices = [...labels].sort(([a], [b]) => a.length - b.length || a.localeCompare(b));
    const key = JSON.stringify([state.platform, state.registeredAccounts !== null, choices]);
    if (state.accountOptionsKey !== key) {
      const options = [new Option('全部账号', 'all')];
      for (const [label, info] of choices) {
        const historical = state.registeredAccounts !== null && !info.current;
        options.push(new Option(`账号 ${label}${historical ? ' · 历史' : ''}`, label));
      }
      options.push(new Option('未归属', 'unknown'));
      $('account-filter').replaceChildren(...options);
      state.accountOptionsKey = key;
    }
    $('account-filter').value = state.account;
  }

  async function loadAccountRegistry() {
    try {
      const data = await request('/api/accounts', {}, AbortSignal.timeout(7000));
      if (!Array.isArray(data.accounts)) return;
      state.registeredAccounts = data.accounts;
      updateAccountOptions(state.data || {});
    } catch (_) { /* Usage remains available while account management is offline. */ }
  }

  function schedule(delay = 2000) {
    clearTimeout(state.timer);
    state.timer = null;
    if ((state.live || state.scanPending) && !document.hidden && !state.disposed) {
      state.timer = setTimeout(() => refresh(state.live && !state.scanPending), delay);
    }
  }

  async function refresh(scan = true) {
    if (state.disposed) return;
    clearTimeout(state.timer);
    state.timer = null;
    if (state.busy) {
      state.queued = true;
      state.queuedScan = state.queuedScan || scan;
      return;
    }
    const requestedQuery = query();
    const requestedVersion = state.filterVersion;
    const startedAt = Date.now();
    const controller = new AbortController();
    state.controller = controller;
    state.busy = true;
    $('sync-now').disabled = true;
    write('sync-label', '同步中…');
    if (!state.data) write('sync-status', '正在读取本机用量…');
    const timeout = setTimeout(() => controller.abort(), 25000);
    try {
      if (scan && !state.scanPending) {
        // A 202 starts one asynchronous scan; follow it with GETs until it finishes.
        // Keep this intent if the tab becomes hidden before the POST responds.
        state.scanPending = true;
        try {
          await request('/api/usage/scan', postOptions({}), controller.signal);
          state.scanFailed = false;
        } catch (error) {
          if (controller.signal.aborted) throw error;
          state.scanFailed = true;
        }
      }
      const data = await request(`/api/usage?${requestedQuery}`, {}, controller.signal);
      if (!data || !data.summary || !Array.isArray(data.rows) || !data.pagination) throw new Error('invalid-usage-response');
      state.scanPending = Boolean(data.scan && data.scan.running);
      if (requestedVersion !== state.filterVersion || requestedQuery !== query()) {
        state.queued = true;
        return;
      }
      state.data = data;
      state.updatedAt = Date.now();
      state.failed = false;
      render(data);
    } catch (error) {
      if (requestedVersion === state.filterVersion && !state.disposed && !document.hidden && !['paused', 'hidden', 'filter'].includes(controller.signal.reason)) {
        state.failed = true;
        showAlert(state.data ? '本机用量服务暂时未连接，已保留上次数字。可点击“立即同步”重试。' : '暂时无法读取本机用量。请确认额度看板服务正在运行，再点击“立即同步”。');
        if (!state.data) {
          $('metrics').setAttribute('aria-busy', 'false');
          renderEmpty('暂时无法读取用量', '本机服务恢复后可重新同步。');
          const tr = element('tr');
          const td = element('td', 'model-empty', '等待本机用量服务恢复');
          td.colSpan = 4;
          tr.append(td);
          $('models-body').replaceChildren(tr);
          renderAccounts(null, null);
          $('trend-chart').replaceChildren(element('p', 'chart-empty', '本机服务恢复后显示用量趋势。'));
        }
        renderStatus();
      }
    } finally {
      clearTimeout(timeout);
      state.busy = false;
      state.controller = null;
      $('sync-now').disabled = false;
      write('sync-label', '立即同步');
      if (state.queued && !state.disposed && !document.hidden) {
        const queuedScan = state.queuedScan;
        state.queued = false;
        state.queuedScan = false;
        refresh(queuedScan);
      } else {
        state.queued = false;
        state.queuedScan = false;
        schedule(Math.max(0, 2000 - (Date.now() - startedAt)));
      }
    }
  }

  function showAlert(text) {
    write('service-alert', text);
    $('service-alert').hidden = !text;
  }

  function renderStatus() {
    const updated = state.updatedAt ? timeFormat.format(new Date(state.updatedAt)) : null;
    const paused = !state.live || document.hidden;
    $('live-indicator').classList.toggle('paused', paused || state.failed);
    write('live-label', state.failed ? '等待连接' : document.hidden ? '后台暂停' : state.live ? '实时同步' : '已暂停');
    write('live-toggle-label', state.live ? '暂停实时' : '开启实时');
    write('live-toggle-icon', state.live ? 'Ⅱ' : '▷');
    $('live-toggle').setAttribute('aria-pressed', String(state.live));
    const leading = state.failed ? '连接失败 · 保留上次数字' : document.hidden ? '页面在后台，自动同步已暂停' : state.live ? '每 2 秒读取新增日志' : state.scanPending ? '实时已暂停 · 正在完成本次同步' : '实时已暂停 · 可手动同步';
    write('sync-status', `${leading}${updated ? ` · 数据取得于 ${updated}` : ' · 等待首次同步'}`);
    write('footer-updated', updated ? `最近同步 ${dateTime(state.updatedAt / 1000)}` : '尚未同步');
  }

  function renderScan(scan) {
    if (state.scanFailed) showAlert('新增日志扫描暂时失败。以下为已索引数据，可点击“立即同步”重试。');
    else if (scan && scan.error) showAlert('部分本机日志尚未完成扫描，以下保留已索引数据。可再次同步。');
    else showAlert('');
  }

  function renderSummary(summary) {
    $('metrics').setAttribute('aria-busy', 'false');
    write('stat-requests', count(summary.requests));
    write('stat-sessions', `${count(summary.sessions)} 个会话 · ${rangeLabel()}`);
    write('stat-total', compact(summary.total_tokens));
    $('stat-total').title = `${count(summary.total_tokens)} Token`;
    write('stat-total-note', `输入 ${compact(summary.input_tokens)} + 输出 ${compact(summary.output_tokens)}`);
    write('stat-input', compact(summary.uncached_input_tokens));
    $('stat-input').title = `${count(summary.uncached_input_tokens)} Token，不含缓存读取与写入`;
    write('stat-cache', `缓存命中 ${compact(summary.cached_input_tokens)}`);
    write('stat-cache-rate', cachePercent(summary));
    $('stat-cache-rate').title = `缓存读取 ${count(summary.cached_input_tokens)} ÷ 全部输入 ${count(summary.input_tokens)}`;
    write('stat-output', compact(summary.output_tokens));
    $('stat-output').title = `${count(summary.output_tokens)} Token，已包含推理`;
    write('stat-reasoning', `含推理 ${compact(summary.reasoning_output_tokens)}`);
    write('stat-cost', money(summary.estimated_cost_usd, true));
    $('stat-cost').classList.toggle('unpriced', !numeric(summary.estimated_cost_usd));
    const unpriced = numeric(summary.unpriced_requests) ? summary.unpriced_requests : 0;
    write('stat-unpriced', unpriced ? `${numeric(summary.estimated_cost_usd) ? '仅已定价部分 · ' : ''}${count(unpriced)} 次请求未定价` : numeric(summary.estimated_cost_usd) ? (state.platform === 'gpt' ? '官方 / 手动价估算 · 非订阅扣款' : '对应 API 模型参考价 · 非订阅扣款') : state.platform === 'gpt' ? '设置模型单价后显示估算' : '缺少已核定参考价');
    $('stat-unpriced').classList.toggle('warning', unpriced > 0);
  }

  function updateModelOptions(models) {
    const all = modelValues(models);
    const key = JSON.stringify(all);
    if (state.modelsKey === key) return;
    state.modelsKey = key;
    const filterFragment = document.createDocumentFragment();
    const allOption = element('option', '', '全部模型');
    allOption.value = 'all';
    filterFragment.append(allOption);
    all.forEach(model => {
      const option = element('option', '', modelLabel(model));
      option.value = model;
      filterFragment.append(option);
    });
    if (state.model !== 'all' && !all.includes(state.model)) {
      const option = element('option', '', modelLabel(state.model));
      option.value = state.model;
      filterFragment.append(option);
    }
    $('model-filter').replaceChildren(filterFragment);
    $('model-filter').value = state.model;
    $('open-pricing').disabled = state.platform !== 'gpt' || all.length === 0;
    if (!$('pricing-dialog').open) updatePricingOptions(all);
  }

  function updatePricingOptions(models) {
    models = modelValues(models);
    const preferred = state.model === 'all' ? $('pricing-model').value : state.model;
    const fragment = document.createDocumentFragment();
    if (!models.length) {
      const option = element('option', '', '暂无可配置模型');
      option.value = '';
      fragment.append(option);
    }
    models.forEach(model => {
      const option = element('option', '', modelLabel(model));
      option.value = model;
      fragment.append(option);
    });
    $('pricing-model').replaceChildren(fragment);
    if (models.includes(preferred)) $('pricing-model').value = preferred;
  }

  function makeRow(key) {
    const tr = element('tr', 'record-row');
    tr.tabIndex = 0;
    tr.dataset.rowId = key;
    const timeCell = element('td', 'record-time');
    const timeButton = element('button', 'record-time-button');
    timeButton.type = 'button';
    timeButton.tabIndex = -1;
    timeButton.append(element('span'), element('small'));
    timeCell.append(timeButton);
    tr.append(timeCell);
    const appCell = element('td');
    const app = element('span', 'app-name');
    const icon = element('span', 'app-icon', '⌁');
    icon.setAttribute('aria-hidden', 'true');
    app.append(icon, document.createTextNode('Codex'));
    appCell.append(app);
    tr.append(appCell, element('td', 'record-account'), element('td', 'record-provider'), element('td', 'record-model'),
      element('td', 'numeric'), element('td', 'numeric'), element('td', 'numeric'),
      element('td', 'numeric record-cache-rate'), element('td', 'numeric record-cost'), element('td', 'numeric record-speed'));
    tr.addEventListener('click', () => openRecord(key));
    tr.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openRecord(key);
      }
    });
    return tr;
  }

  function updateRow(tr, row) {
    const cells = tr.children;
    const timestamp = date(row.at);
    const time = timestamp ? timeFormat.format(timestamp) : '—';
    const dateText = timestamp ? dateFormat.format(timestamp) : '时间未知';
    cells[0].firstChild.children[0].textContent = time;
    cells[0].firstChild.children[1].textContent = dateText;
    cells[0].title = dateTime(row.at);
    cells[1].firstChild.lastChild.textContent = appLabel(row);
    cells[2].textContent = state.platform === 'all' ? accountLabel(row) : accountValue(row) || '未归属';
    cells[2].classList.toggle('account-unknown', !accountValue(row));
    cells[3].textContent = providerLabel(row);
    cells[3].title = providerLabel(row);
    cells[4].textContent = modelLabel(row.model);
    cells[4].title = modelLabel(row.model);
    [row.uncached_input_tokens, row.output_tokens, row.cached_input_tokens].forEach((value, index) => {
      cells[index + 5].textContent = compact(value);
      cells[index + 5].title = `${count(value)} Token`;
    });
    cells[8].textContent = cachePercent(row);
    cells[8].title = `缓存读取 ${count(row.cached_input_tokens)} ÷ 全部输入 ${count(row.input_tokens)}`;
    cells[9].textContent = money(row.estimated_cost_usd);
    cells[9].classList.toggle('record-unpriced', !numeric(row.estimated_cost_usd));
    cells[9].title = priceContext(row);
    cells[10].textContent = speed(row.speed_tps);
    cells[10].title = '包含等待时间的日志估算速度';
    tr.setAttribute('aria-label', `${dateText} ${time}，${accountLabel(row)}，${modelLabel(row.model)}，新增输入 ${count(row.uncached_input_tokens)}，输出 ${count(row.output_tokens)}，查看明细`);
  }

  function renderEmpty(title, subtitle) {
    state.rowNodes.clear();
    state.rowLookup.clear();
    const tr = element('tr', 'empty-row');
    const td = element('td');
    td.colSpan = 11;
    td.append(element('span', 'empty-icon', '⌁'), element('strong', '', title), element('span', '', subtitle));
    tr.append(td);
    $('records-body').replaceChildren(tr);
  }

  function renderRows(rows, pagination, scan, summary) {
    write('records-count', count(pagination.total));
    const pages = Math.max(1, numeric(pagination.pages) ? pagination.pages : 1);
    write('page-label', `第 ${count(pagination.page || state.page)} / ${count(pages)} 页`);
    $('page-prev').disabled = state.page <= 1;
    $('page-next').disabled = state.page >= pages;
    let note = state.page > 1 ? '正在查看历史页，实时同步保留当前页。' : '仅计入本机已写出用量的请求。';
    if (numeric(summary.speed_tps) && summary.speed_samples > 0) note += ` 平均 ${speed(summary.speed_tps)} · ${count(summary.speed_samples)} 个估算样本`;
    if (scan && scan.skipped_records > 0) note += ` · 已跳过 ${count(scan.skipped_records)} 条无法可靠计数的记录`;
    write('records-note', note);
    if (!rows.length) {
      renderEmpty(scan && scan.running ? '正在建立本机用量索引' : state.page > 1 ? '这一页暂时没有记录' : '这个范围内还没有记录', scan && scan.running ? '首次扫描需要一点时间，记录会逐步出现。' : '可切换时间、模型或账号筛选，再同步本机日志。');
      write('new-records', '');
      return;
    }
    const body = $('records-body');
    const scroller = $('records-scroll');
    const previousScroll = scroller.scrollTop;
    const previousKeys = new Set(state.rowNodes.keys());
    let anchor = null;
    let anchorTop = 0;
    if (previousScroll > 2) {
      const top = scroller.getBoundingClientRect().top + 38;
      anchor = [...body.children].find(node => node.dataset.rowId && node.getBoundingClientRect().bottom > top);
      if (anchor) anchorTop = anchor.getBoundingClientRect().top;
    }
    if (body.querySelector('.empty-row')) body.replaceChildren();
    const nextNodes = new Map();
    const lookup = new Map();
    let cursor = body.firstElementChild;
    let newCount = 0;
    rows.forEach((row, index) => {
      const key = `${row.platform || 'gpt'}:${String(row.id || `${row.at}-${row.model}-${index}`)}`;
      let tr = state.rowNodes.get(key);
      if (!tr) {
        tr = makeRow(key);
        if (state.rowsRendered && state.page === 1) tr.classList.add('row-new');
      }
      updateRow(tr, row);
      if (tr !== cursor) body.insertBefore(tr, cursor);
      cursor = tr.nextElementSibling;
      nextNodes.set(key, tr);
      lookup.set(key, row);
      if (!previousKeys.has(key)) newCount += 1;
    });
    while (cursor) {
      const next = cursor.nextElementSibling;
      cursor.remove();
      cursor = next;
    }
    state.rowNodes = nextNodes;
    state.rowLookup = lookup;
    if (anchor && anchor.isConnected && previousScroll > 2) scroller.scrollTop = previousScroll + anchor.getBoundingClientRect().top - anchorTop;
    else if (previousScroll <= 2) scroller.scrollTop = 0;
    if (state.rowsRendered && state.page === 1 && newCount) {
      state.newCount = newCount;
      write('new-records', `+ ${count(newCount)} 条新记录`);
    }
    state.rowsRendered = true;
  }

  function renderModels(models, summary) {
    const fragment = document.createDocumentFragment();
    models.forEach(row => {
      const tr = element('tr');
      const modelCell = element('td', '', `${state.platform === 'all' && row.platform ? `${platformLabel(row)} · ` : ''}${modelLabel(row.model)}`);
      modelCell.append(element('small', '', `${count(row.requests)} 次请求`));
      const tokens = element('td', 'numeric', compact(row.total_tokens));
      tokens.title = `${count(row.total_tokens)} Token`;
      tr.append(modelCell, tokens, element('td', 'numeric', cachePercent(row)), element('td', 'numeric', money(row.estimated_cost_usd)));
      fragment.append(tr);
    });
    if (!models.length) {
      const tr = element('tr');
      const td = element('td', 'model-empty', '所选范围暂无模型用量');
      td.colSpan = 4;
      tr.append(td);
      fragment.append(tr);
    }
    $('models-body').replaceChildren(fragment);
    write('model-cost-note', summary.unpriced_requests > 0 ? `未定价 ${count(summary.unpriced_requests)} 次请求${numeric(summary.estimated_cost_usd) ? ' · 成本仅含已定价部分' : state.platform === 'gpt' ? ' · 设置单价后显示参考估算' : ' · 缺少已核定参考价'}` : state.platform === 'gpt' ? 'OpenAI Standard / 手动价 · 参考估算，非订阅扣款' : '按对应 API 模型参考价 · AGY 官方价，非订阅扣款');
  }

  function renderAccounts(accounts, summary) {
    const fragment = document.createDocumentFragment();
    const rows = Array.isArray(accounts) ? [...accounts].sort((a, b) => `${a.platform || 'gpt'}:${accountValue(a) || 'Z'}`.localeCompare(`${b.platform || 'gpt'}:${accountValue(b) || 'Z'}`)) : [];
    rows.forEach(row => {
      const tr = element('tr', 'account-summary-row');
      tr.dataset.account = accountValue(row) || 'unknown';
      tr.dataset.platform = row.platform || 'gpt';
      tr.classList.toggle('account-selected', state.account === tr.dataset.account);
      const label = element('td');
      label.append(element('span', accountValue(row) ? 'account-name' : 'account-name account-unknown', accountLabel(row)),
        element('small', '', `${numeric(row.requests) ? count(row.requests) : '未知'} 次请求`));
      const tokens = element('td', 'numeric', compact(row.total_tokens));
      tokens.title = numeric(row.total_tokens) ? `${count(row.total_tokens)} Token` : 'Token 未知';
      const cost = element('td', 'numeric', money(row.estimated_cost_usd));
      if (row.unpriced_requests > 0) cost.append(element('small', '', `${numeric(row.estimated_cost_usd) ? '部分定价 · ' : ''}${count(row.unpriced_requests)} 次未定价`));
      tr.append(label, tokens, element('td', 'numeric', cachePercent(row)), cost);
      fragment.append(tr);
    });
    if (!rows.length) {
      const tr = element('tr');
      const td = element('td', 'model-empty', Array.isArray(accounts) ? '所选时间与模型暂无账号用量；缺证记录保留未归属。' : '账号汇总暂不可用；缺证记录保留未归属。');
      td.colSpan = 4;
      tr.append(td);
      fragment.append(tr);
    }
    $('accounts-body').replaceChildren(fragment);
    const label = state.account === 'all' ? '本次筛选' : state.account === 'unknown' ? '未归属筛选' : `账号 ${state.account} 筛选`;
    write('account-coverage', summary && numeric(summary.attributed_requests) && numeric(summary.unattributed_requests)
      ? `${label}：已归属 ${count(summary.attributed_requests)} 次 · 未归属 ${count(summary.unattributed_requests)} 次；未归属不分摊。`
      : '当前归属数量未知；未归属不分摊。');
  }

  function renderAttribution(attribution) {
    let text = '账号观察状态暂不可用；旧历史无法反推，未归属不分摊。';
    if (attribution && attribution.error) text = '账号归属观察暂时异常，已保留有证据的归属；其余记录保持未归属。';
    else if (attribution && attribution.enabled === true) text = `账号归属观察已启用${date(attribution.started_at) ? `（${dateTime(attribution.started_at)} 起）` : ''}；仅实际观察到的新写入用量可核对账号，旧历史无法反推。`;
    else if (attribution && attribution.enabled === false) text = '账号归属观察未启用；已核对记录保留，其余请求显示未归属。';
    write('attribution-status', text);
    $('attribution-status').classList.toggle('warning', Boolean(attribution && attribution.error));
  }

  function renderTrend(timeline) {
    const target = $('trend-chart');
    const metricName = {total_tokens: '总 Token', requests: '已记录请求', estimated_cost_usd: '参考成本（USD）'}[state.metric];
    write('trend-caption', `${rangeLabel()} · ${metricName}${state.metric === 'estimated_cost_usd' && state.data.summary.unpriced_requests > 0 ? ' · 仅含已定价部分' : ''}`);
    const valid = timeline.filter(item => numeric(item[state.metric]));
    if (!valid.length) {
      target.replaceChildren(element('p', 'chart-empty', state.metric === 'estimated_cost_usd' ? (state.platform === 'gpt' ? '设置模型单价后，在这里查看参考成本趋势。' : '缺少已核定参考价，暂不能显示成本趋势。') : '有了用量记录，这里就会出现趋势。'));
      target.setAttribute('aria-label', `暂无${metricName}趋势`);
      return;
    }
    const width = 660, height = 200, left = 48, right = 17, top = 16, bottom = 32;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const max = Math.max(...valid.map(item => item[state.metric]), 1);
    const svg = svgElement('svg', {viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true'});
    for (let index = 0; index <= 3; index += 1) {
      const y = top + (plotHeight * index / 3);
      svg.append(svgElement('line', {x1: left, y1: y, x2: width - right, y2: y, class: 'chart-grid'}));
      const value = max * (1 - index / 3);
      const label = state.metric === 'estimated_cost_usd' ? money(value).replace('$', '') : compact(Math.round(value));
      svg.append(svgElement('text', {x: left - 8, y: y + 3, 'text-anchor': 'end', class: 'chart-axis-label'}, label));
    }
    const xFor = index => timeline.length > 1 ? left + index * plotWidth / (timeline.length - 1) : left + plotWidth / 2;
    const yFor = value => top + plotHeight - value / max * plotHeight;
    const segments = [];
    let segment = [];
    timeline.forEach((item, index) => {
      if (numeric(item[state.metric])) segment.push([xFor(index), yFor(item[state.metric]), item]);
      else if (segment.length) { segments.push(segment); segment = []; }
    });
    if (segment.length) segments.push(segment);
    segments.forEach(points => {
      const coordinates = points.map(point => `${point[0]},${point[1]}`).join(' ');
      const first = points[0], last = points[points.length - 1];
      if (points.length > 1) {
        svg.append(svgElement('polygon', {points: `${first[0]},${top + plotHeight} ${coordinates} ${last[0]},${top + plotHeight}`, class: 'chart-area'}));
        svg.append(svgElement('polyline', {points: coordinates, class: 'chart-line'}));
      }
      points.forEach(([x, y, item]) => {
        const point = svgElement('circle', {cx: x, cy: y, r: points.length > 60 ? 1.6 : 3, class: 'chart-point'});
        const value = state.metric === 'estimated_cost_usd' ? money(item[state.metric]) : count(item[state.metric]);
        point.append(svgElement('title', {}, `${item.label || dateTime(item.at)} · ${metricName} ${value}`));
        svg.append(point);
      });
    });
    const labelIndices = [...new Set([0, Math.floor((timeline.length - 1) / 2), timeline.length - 1])];
    labelIndices.forEach(index => {
      const item = timeline[index];
      const label = String(item.label || (date(item.at) ? dateFormat.format(date(item.at)) : '')).slice(0, 24);
      svg.append(svgElement('text', {x: xFor(index), y: height - 9, 'text-anchor': index === 0 ? 'start' : index === timeline.length - 1 ? 'end' : 'middle', class: 'chart-axis-label'}, label));
    });
    target.replaceChildren(svg);
    target.setAttribute('aria-label', `${rangeLabel()}${metricName}趋势，${count(valid.length)} 个时间点，最大 ${state.metric === 'estimated_cost_usd' ? money(max) : count(max)}`);
  }

  function renderHeatmap(entries) {
    const key = JSON.stringify([state.platform, state.model, state.account, entries]);
    if (state.heatmapKey === key) return;
    state.heatmapKey = key;
    const byDate = new Map(entries.filter(item => /^\d{4}-\d{2}-\d{2}$/.test(item.date || '')).map(item => [item.date, item]));
    const dateParts = new Intl.DateTimeFormat('en-CA', {timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(new Date());
    const part = type => dateParts.find(value => value.type === type).value;
    const end = new Date(`${part('year')}-${part('month')}-${part('day')}T00:00:00Z`);
    const start = new Date(end);
    start.setUTCDate(start.getUTCDate() - 364);
    const first = new Date(start);
    first.setUTCDate(first.getUTCDate() - (first.getUTCDay() + 6) % 7);
    const cellSize = 11, gap = 3, left = 27, top = 20;
    const weeks = Math.floor((end - first) / 86400000 / 7) + 1;
    const svg = svgElement('svg', {viewBox: `0 0 ${left + weeks * (cellSize + gap) + 3} 124`, 'aria-hidden': 'true'});
    [[0, '一'], [2, '三'], [4, '五']].forEach(([day, label]) => svg.append(svgElement('text', {x: 2, y: top + day * (cellSize + gap) + 9, class: 'heatmap-label'}, label)));
    const max = Math.max(1, ...entries.map(item => numeric(item.requests) ? item.requests : 0));
    let previousMonth = -1;
    let totalRequests = 0;
    let activeDays = 0;
    for (let day = new Date(start); day <= end; day.setUTCDate(day.getUTCDate() + 1)) {
      const dateKey = day.toISOString().slice(0, 10);
      const item = byDate.get(dateKey) || {};
      const requests = numeric(item.requests) ? item.requests : 0;
      totalRequests += requests;
      if (requests > 0) activeDays += 1;
      const week = Math.floor((day - first) / 86400000 / 7);
      const weekday = (day.getUTCDay() + 6) % 7;
      const x = left + week * (cellSize + gap);
      if (day.getUTCMonth() !== previousMonth && weekday < 4) {
        svg.append(svgElement('text', {x, y: 10, class: 'heatmap-label'}, `${day.getUTCMonth() + 1}月`));
        previousMonth = day.getUTCMonth();
      }
      const level = requests <= 0 ? 0 : Math.min(4, Math.max(1, Math.ceil(Math.sqrt(requests / max) * 4)));
      const rect = svgElement('rect', {x, y: top + weekday * (cellSize + gap), width: cellSize, height: cellSize, class: `heat-cell heat-${level}`});
      rect.append(svgElement('title', {}, `${dateKey} · ${count(requests)} 次请求 · ${count(item.total_tokens)} Token · ${money(item.estimated_cost_usd)}`));
      svg.append(rect);
    }
    const scroller = $('heatmap-scroll');
    const previousScroll = scroller.scrollLeft;
    $('heatmap-chart').replaceChildren(svg);
    $('heatmap-chart').setAttribute('aria-label', `近一年共 ${count(totalRequests)} 次请求，${count(activeDays)} 个活跃日`);
    write('heatmap-total', `${count(activeDays)} 个活跃日 / 365`);
    write('heatmap-dates', `${start.toISOString().slice(0, 10)} — ${end.toISOString().slice(0, 10)}`);
    scroller.scrollLeft = previousScroll || scroller.scrollWidth;
  }

  function render(data) {
    renderPlatform();
    renderStatus();
    renderScan(data.scan || {});
    renderSummary(data.summary);
    updateModelOptions(Array.isArray(data.available_models) ? data.available_models : []);
    updateAccountOptions(data);
    renderRows(data.rows, data.pagination, data.scan, data.summary);
    renderModels(Array.isArray(data.models) ? data.models : [], data.summary);
    renderAccounts(data.accounts, data.summary);
    renderAttribution(data.attribution);
    renderTrend(Array.isArray(data.timeline) ? data.timeline : []);
    renderHeatmap(Array.isArray(data.heatmap) ? data.heatmap : []);
  }

  function priceContext(row) {
    const context = {short: '官方标准上下文价', long: '官方长上下文价', manual: '手动统一价', unpriced: '未定价'}[row.price_context]
      || (numeric(row.estimated_cost_usd) ? '参考估算' : '未定价');
    return row.platform === 'agy' && numeric(row.estimated_cost_usd) ? `按对应 API 模型参考价 · ${context}` : context;
  }

  function officialPriceLink(url) {
    if (typeof url !== 'string') return null;
    try {
      const parsed = new URL(url);
      const allowed = ['ai.google.dev', 'platform.claude.com'].includes(parsed.hostname)
        || (parsed.hostname === 'developers.openai.com' && parsed.pathname.startsWith('/api/docs/'));
      if (parsed.protocol !== 'https:' || !allowed || parsed.username || parsed.password || parsed.port) return null;
      const link = element('a', '', '官方 API 价格来源 ↗');
      link.href = parsed.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      return link;
    } catch (error) { return null; }
  }

  function openRecord(key) {
    const row = state.rowLookup.get(key);
    if (!row) return;
    write('record-subtitle', `${dateTime(row.at)} · ${appLabel(row)} · ${modelLabel(row.model)}`);
    const source = {record: '单次记录', snapshot: '用量快照', delta: '累计用量差值'}[row.source] || '本机日志';
    const entries = [['账号', accountLabel(row)], ['归属证据', accountEvidence(row)], ['供应商', providerLabel(row)], ['来源', source],
      ['会话匿名编号', safeLabel(row.session, '—')], ['输入 Token（已含缓存）', count(row.input_tokens)],
      ['新增输入 Token', count(row.uncached_input_tokens)], ['缓存命中 Token', count(row.cached_input_tokens)],
      ['缓存写入 Token', count(row.cache_write_input_tokens)], ['输出 Token（已含推理）', count(row.output_tokens)],
      ['其中推理 Token', count(row.reasoning_output_tokens)], ['总 Token', count(row.total_tokens)],
      ['参考成本 · USD', money(row.estimated_cost_usd, true)], ['日志估算速度', speed(row.speed_tps)],
      ['日志估算耗时', numeric(row.duration_ms) && row.duration_ms > 0 ? `${(row.duration_ms / 1000).toFixed(2)} 秒` : '—'],
      ['缓存命中率', cachePercent(row)], ['本次价格口径', priceContext(row)]];
    if (typeof row.price_model === 'string' && row.price_model) entries.push(['参考 API 模型', modelLabel(row.price_model)]);
    const priceSource = {google_official: 'Google 官方 API 价', anthropic_official: 'Anthropic 官方 API 价', openai_official: 'OpenAI 官方 API 价', manual: '手动参考价'}[row.price_source];
    if (priceSource) entries.push(['价格来源', priceSource]);
    if (typeof row.price_mapping_note === 'string' && row.price_mapping_note) entries.push(['模型价格对应说明', row.price_mapping_note]);
    if (typeof row.price_checked_at === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(row.price_checked_at)) entries.push(['官方价格核验', row.price_checked_at]);
    const fragment = document.createDocumentFragment();
    entries.forEach(([label, value]) => {
      const item = element('div', 'detail-item');
      item.append(element('dt', '', label), element('dd', '', value));
      fragment.append(item);
    });
    const priceLink = officialPriceLink(row.price_url);
    if (priceLink) {
      const item = element('div', 'detail-item');
      const value = element('dd');
      value.append(priceLink);
      item.append(element('dt', '', '官方来源'), value);
      fragment.append(item);
    }
    $('record-detail').replaceChildren(fragment);
    $('record-dialog').showModal();
  }

  function loadPricing() {
    const model = $('pricing-model').value;
    const prices = state.data && state.data.pricing ? state.data.pricing[model] : null;
    [['price-input', 'input'], ['price-cached', 'cached_input'], ['price-write', 'cache_write'], ['price-output', 'output']].forEach(([id, key]) => {
      $(id).value = prices && numeric(prices[key]) ? String(prices[key]) : '';
    });
    renderPricingSource(model, prices);
    write('pricing-error', '');
  }

  function renderPricingSource(model, prices) {
    const source = state.data && state.data.pricing_sources ? state.data.pricing_sources[model] : null;
    const official = source && source.kind === 'openai_official';
    const hasPrices = prices && Object.values(prices).some(numeric);
    const sourceBox = $('pricing-source');
    const fragment = document.createDocumentFragment();
    fragment.append(element('strong', '', official ? 'OpenAI 官方 API 价 · Standard' : hasPrices ? '手动参考价 · 统一价' : '未定价'));
    const checkedAt = official ? source.checked_at : source && source.official_checked_at;
    const checkedDate = typeof checkedAt === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(checkedAt) ? checkedAt : '日期未知';
    if (official) fragment.append(element('small', '', `官方价格核验：${checkedDate} · USD / 百万 Token`));
    else if (source && source.official_available) fragment.append(element('small', '', `当前使用手动价；可恢复的官方参考核验于 ${checkedDate}`));
    else fragment.append(element('small', '', hasPrices ? '当前使用自行设置的参考单价。' : '尚无可用参考价，可填写四项手动单价。'));
    const url = source && (official ? source.url : source.official_url);
    if (typeof url === 'string') {
      try {
        const parsed = new URL(url);
        if (parsed.protocol === 'https:' && parsed.hostname === 'developers.openai.com' && parsed.pathname.startsWith('/api/docs/')) {
          const link = element('a', '', 'OpenAI 官方价格来源 ↗');
          link.href = parsed.href;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          fragment.append(link);
        }
      } catch (error) { /* An invalid source URL is not rendered as a link. */ }
    }
    sourceBox.replaceChildren(fragment);
    write('clear-pricing', source && source.official_available ? '恢复官方价' : '恢复未定价');
    const writeUnknown = official && prices && prices.cache_write === null;
    $('price-write').placeholder = writeUnknown ? '官方未单独定价' : '未设置';
    write('price-write-help', writeUnknown ? '官方未单独定价；出现写入量时保留未定价' : official && source.cache_write_ttl === '30m' ? '30m 缓存写入参考价' : '按日志中的缓存写入量');
    const threshold = official && numeric(source.context_threshold) ? source.context_threshold : null;
    const longPrices = official && source.long_context;
    write('pricing-rule', official && threshold && longPrices
      ? `下方为每次输入不超过 ${count(threshold)} Token 的参考单价；超过后，整次请求自动使用长上下文价。`
      : official ? '下方为 Standard API 参考单价；可填写四项价格，保存为手动统一价。' : '手动价对全部上下文统一适用；保存后替代官方自动长上下文规则。');
    const longBox = $('pricing-long-context');
    longBox.hidden = !(threshold && longPrices);
    longBox.replaceChildren();
    if (threshold && longPrices) {
      longBox.append(element('h3', '', `超过 ${count(threshold)} Token · USD / 百万 Token`));
      const list = element('dl', 'pricing-long-grid');
      [['新增输入', 'input'], ['缓存命中', 'cached_input'], ['缓存写入', 'cache_write'], ['输出', 'output']].forEach(([label, key]) => {
        const item = element('div');
        item.append(element('dt', '', label), element('dd', '', numeric(longPrices[key]) ? money(longPrices[key]) : '官方未单独定价'));
        list.append(item);
      });
      longBox.append(list);
    }
  }

  function openPricing() {
    if (!state.data || state.platform !== 'gpt') return;
    updatePricingOptions(state.data.available_models || []);
    loadPricing();
    $('pricing-dialog').showModal();
  }

  async function savePricing(clear) {
    if (state.pricingBusy) return;
    const model = $('pricing-model').value;
    if (!model) { write('pricing-error', '请先选择一个具体模型。'); return; }
    const body = {model};
    if (clear) body.clear = true;
    else {
      for (const [id, key] of [['price-input', 'input'], ['price-cached', 'cached_input'], ['price-write', 'cache_write'], ['price-output', 'output']]) {
        const value = $(id).value.trim();
        const parsed = Number(value);
        if (!value || !Number.isFinite(parsed) || parsed < 0) {
          write('pricing-error', `四项单价都需填写非负有限数字；可使用“${$('clear-pricing').textContent}”，或填完整四项保存手动价。`);
          $(id).focus();
          return;
        }
        body[key] = parsed;
      }
    }
    state.pricingBusy = true;
    const controls = ['save-pricing', 'clear-pricing', 'pricing-model', 'price-input', 'price-cached', 'price-write', 'price-output'];
    controls.forEach(id => { $(id).disabled = true; });
    write('pricing-error', '');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      await request('/api/usage/pricing', postOptions(body), controller.signal);
      $('pricing-dialog').close();
      await refresh(false);
    } catch (error) {
      write('pricing-error', '未能保存单价，请确认本机服务可用后重试。');
    } finally {
      clearTimeout(timeout);
      state.pricingBusy = false;
      controls.forEach(id => { $(id).disabled = false; });
    }
  }

  function changeFilter() {
    state.filterVersion += 1;
    state.data = null;
    $('metrics').setAttribute('aria-busy', 'true');
    $('open-pricing').disabled = true;
    for (const id of ['stat-requests','stat-total','stat-input','stat-output','stat-cache-rate','stat-cost']) write(id, '—');
    write('stat-sessions', `${rangeLabel()} · 正在读取`);
    write('stat-unpriced', '正在读取所选范围');
    write('sync-status', '正在读取所选范围…');
    renderEmpty('正在读取所选范围', '统计与记录将同时更新。');
    renderAccounts([], null);
    $('models-body').replaceChildren();
    $('trend-chart').replaceChildren(element('p', 'chart-empty', '正在读取所选范围…'));
    $('heatmap-chart').replaceChildren();
    state.heatmapKey = '';
    if (state.controller) state.controller.abort('filter');
    state.page = 1;
    state.rowsRendered = false;
    state.rowNodes.clear();
    write('new-records', '');
    $('records-scroll').scrollTop = 0;
    refresh(false);
  }

  function markRange() {
    document.querySelectorAll('[data-range]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.range === state.range)));
  }
  function applyDates(force = false) {
    const first = $('date-start').value, last = $('date-end').value;
    if (!first || !last || first < '2026-11-09' || first > last || last > localToday()) {
      write('date-note', '请选择 2026-11-09 至今天，结束不能早于开始。');
      return;
    }
    if (!force && first === state.startDate && last === state.endDate && state.range === 'custom') return;
    state.range = 'custom'; state.startDate = first; state.endDate = last;
    markRange();
    write('date-note', `${first} 至 ${last}，包含首尾日期。`);
    changeFilter();
  }
  document.querySelectorAll('[data-range]').forEach(button => button.addEventListener('click', () => {
    state.range = button.dataset.range;
    const end = new Date(`${localToday()}T12:00:00Z`);
    if (state.range === 'yesterday') end.setUTCDate(end.getUTCDate() - 1);
    state.endDate = end.toISOString().slice(0, 10);
    const days = {today: 1, yesterday: 1, '7d': 7, '30d': 30, '365d': 365}[state.range];
    const first = new Date(end);
    if (days) first.setUTCDate(first.getUTCDate() - days + 1);
    state.startDate = state.range === 'all' ? '2026-11-09' : [first.toISOString().slice(0, 10), '2026-11-09'].sort().pop();
    $('date-start').value = state.startDate; $('date-end').value = state.endDate;
    write('date-note', `${state.startDate} 至 ${state.endDate}，包含首尾日期。`);
    markRange(); changeFilter();
  }));
  $('date-start').value = state.startDate; $('date-end').value = state.endDate;
  for (const id of ['date-start', 'date-end']) {
    $(id).max = localToday();
    $(id).addEventListener('change', () => applyDates());
  }
  markRange();
  $('date-filter').addEventListener('submit', event => { event.preventDefault(); applyDates(true); });
  document.querySelectorAll('button[data-platform]').forEach(button => button.addEventListener('click', () => {
    if (state.platform === button.dataset.platform) return;
    state.platform = button.dataset.platform;
    state.model = 'all'; state.account = 'all'; state.modelsKey = ''; state.heatmapKey = '';
    $('model-filter').replaceChildren(new Option('全部模型', 'all'));
    updateAccountOptions();
    if ($('pricing-dialog').open) $('pricing-dialog').close();
    if ($('record-dialog').open) $('record-dialog').close();
    document.querySelectorAll('button[data-platform]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    renderPlatform(); changeFilter();
  }));
  $('model-filter').addEventListener('change', () => { state.model = $('model-filter').value; changeFilter(); });
  $('account-filter').addEventListener('change', () => { state.account = $('account-filter').value; changeFilter(); });
  $('live-toggle').addEventListener('click', () => {
    state.live = !state.live;
    clearTimeout(state.timer);
    state.timer = null;
    if (!state.live && state.controller) state.controller.abort('paused');
    renderStatus();
    if (state.live) refresh(true);
  });
  $('sync-now').addEventListener('click', () => refresh(true));
  $('page-prev').addEventListener('click', () => {
    if (state.page <= 1) return;
    state.page -= 1;
    state.rowsRendered = false;
    state.rowNodes.clear();
    write('new-records', '');
    $('records-scroll').scrollTop = 0;
    refresh(false);
  });
  $('page-next').addEventListener('click', () => {
    const pages = state.data && state.data.pagination ? state.data.pagination.pages : 1;
    if (state.page >= pages) return;
    state.page += 1;
    state.rowsRendered = false;
    state.rowNodes.clear();
    write('new-records', '');
    $('records-scroll').scrollTop = 0;
    refresh(false);
  });
  document.querySelectorAll('[data-metric]').forEach(button => button.addEventListener('click', () => {
    state.metric = button.dataset.metric;
    document.querySelectorAll('[data-metric]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    if (state.data) renderTrend(state.data.timeline || []);
  }));
  $('close-record').addEventListener('click', () => $('record-dialog').close());
  $('record-done').addEventListener('click', () => $('record-dialog').close());
  $('open-pricing').addEventListener('click', openPricing);
  $('close-pricing').addEventListener('click', () => $('pricing-dialog').close());
  $('pricing-model').addEventListener('change', loadPricing);
  $('save-pricing').addEventListener('click', () => savePricing(false));
  $('clear-pricing').addEventListener('click', () => savePricing(true));
  document.addEventListener('visibilitychange', () => {
    clearTimeout(state.timer);
    state.timer = null;
    if (document.hidden) {
      if (state.controller) state.controller.abort('hidden');
    } else { loadAccountRegistry(); if (state.live || state.scanPending) refresh(state.live && !state.scanPending); }
    renderStatus();
  });
  window.addEventListener('pagehide', () => {
    state.disposed = true;
    clearTimeout(state.timer);
    if (state.controller) state.controller.abort();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) { state.disposed = false; refresh(state.live && !state.scanPending); }
  });
  write('timezone-label', timezone);
  $('open-pricing').disabled = true;
  loadAccountRegistry();
  refresh(true);
})();
