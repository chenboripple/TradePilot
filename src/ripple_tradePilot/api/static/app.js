const state = {
  dashboard: null,
  user: null,
  strategies: [],
  backtests: [],
  allStocks: null,
  stockQuery: "",
  stockLimit: 200,
  stockFilters: {
    exchange: "",
    board: "",
    industry: "",
    area: "",
    status: "",
    watch: "",
  },
  assetClass: "stock",
  selectedSymbol: null,
  currentMarket: null,
  selectedStrategyId: "",
  appliedStrategyId: "",
  detailRequest: 0,
  view: "overview",
  range: 120,
  indicators: { ma: true, bb: true, volume: true },
  hoverIndex: null,
  authMode: "login",
  pendingView: null,
};

const elements = {
  refresh: document.querySelector("#refresh-button"),
  generatedAt: document.querySelector("#generated-at"),
  systemDot: document.querySelector("#system-dot"),
  watchlist: document.querySelector("#watchlist"),
  watchlistTitle: document.querySelector("#watchlist-title"),
  watchlistCount: document.querySelector("#watchlist-count"),
  dataAlert: document.querySelector("#data-alert"),
  workspace: document.querySelector("#market-workspace"),
  emptyMarket: document.querySelector("#empty-market"),
  chart: document.querySelector("#market-chart"),
  chartTooltip: document.querySelector("#chart-tooltip"),
  chartEmpty: document.querySelector("#chart-empty"),
  detailStrategy: document.querySelector("#detail-strategy"),
  strategyCalculating: document.querySelector("#strategy-calculating"),
  accountButton: document.querySelector("#account-button"),
  accountMenu: document.querySelector("#account-menu"),
  accountName: document.querySelector("#account-name"),
  accountAvatar: document.querySelector("#account-avatar"),
  authDialog: document.querySelector("#auth-dialog"),
  authForm: document.querySelector("#auth-form"),
  authError: document.querySelector("#auth-error"),
  strategyDialog: document.querySelector("#strategy-dialog"),
  strategyForm: document.querySelector("#strategy-form"),
  strategyError: document.querySelector("#strategy-error"),
  strategyAssetClass: document.querySelector("#strategy-asset-class"),
  strategyStockField: document.querySelector("#strategy-stock-field"),
  strategyStockSymbol: document.querySelector("#strategy-stock-symbol"),
  strategyFutureField: document.querySelector("#strategy-future-field"),
  strategyFutureSymbol: document.querySelector("[name=future_symbol]"),
  stockDialog: document.querySelector("#stock-dialog"),
  stockForm: document.querySelector("#stock-form"),
  stockError: document.querySelector("#stock-error"),
  addStockButton: document.querySelector("#add-stock-button"),
  stockTable: document.querySelector("#stock-table"),
  stockEmpty: document.querySelector("#stock-empty"),
  stockSearch: document.querySelector("#stock-search"),
  stockMore: document.querySelector("#stock-more"),
  stockCatalogStatus: document.querySelector("#stock-catalog-status"),
  syncStocks: document.querySelector("#sync-stocks-button"),
  refreshQuotes: document.querySelector("#refresh-quotes-button"),
  stockFilters: document.querySelectorAll("[data-stock-filter]"),
  stockFilterReset: document.querySelector("#stock-filter-reset"),
};

const recommendationLabels = { BUY: "偏多", SELL: "偏空", HOLD: "观望" };
const voteLabels = { BUY: "偏多", SELL: "偏空", HOLD: "中性" };
const assetLabels = { stock: "股票", future: "期货" };

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function recClass(value) {
  return value === "BUY" ? "rec-buy" : value === "SELL" ? "rec-sell" : "rec-hold";
}

function shortDate(value) {
  if (!value) return "--";
  const parts = String(value).split("-");
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : value;
}

async function apiRequest(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(url, { ...options, headers, cache: "no-store" });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      message = typeof payload.detail === "string" ? payload.detail : message;
    } catch (_) {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

async function fetchCurrentUser() {
  const payload = await apiRequest("/api/auth/me");
  state.user = payload.user;
  renderAuthState();
}

async function loadProtectedData() {
  if (!state.user) {
    state.strategies = [];
    state.backtests = [];
    renderStrategies();
    renderBacktests();
    renderMarket();
    return;
  }
  try {
    const [strategies, backtests] = await Promise.all([
      apiRequest("/api/strategies"),
      apiRequest("/api/backtests"),
    ]);
    state.strategies = strategies.items;
    state.backtests = backtests.items;
    renderStrategies();
    renderBacktests();
    renderMarket();
  } catch (error) {
    if (error.status === 401) {
      state.user = null;
      renderAuthState();
      return;
    }
    throw error;
  }
}

async function fetchStockCatalog() {
  const payload = await apiRequest("/api/stocks");
  state.allStocks = payload.items;
  populateStockFilters();
  populateStrategyStocks();
  renderStockCatalog();
}

function populateStrategyStocks() {
  const selected = elements.strategyStockSymbol.value;
  const stocks = state.allStocks ?? [];
  elements.strategyStockSymbol.innerHTML = [
    '<option value="">从全部数据池选择</option>',
    ...stocks.map((item) => `<option value="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)} · ${escapeHtml(item.name)}</option>`),
  ].join("");
  if (stocks.some((item) => item.symbol === selected)) {
    elements.strategyStockSymbol.value = selected;
  }
}

function updateStrategySymbolField() {
  const isStock = elements.strategyAssetClass.value === "stock";
  elements.strategyStockField.hidden = !isStock;
  elements.strategyFutureField.hidden = isStock;
  elements.strategyStockSymbol.required = isStock;
  elements.strategyFutureSymbol.required = !isStock;
}

function renderAuthState() {
  const authenticated = Boolean(state.user);
  elements.accountButton.hidden = authenticated;
  elements.accountMenu.hidden = !authenticated;
  elements.accountName.textContent = state.user
    ? `${state.user.username} · ${state.user.role === "admin" ? "管理员" : "普通用户"}`
    : "";
  elements.accountAvatar.textContent = state.user?.role === "admin" ? "A" : "U";
  document.querySelectorAll(".guest-content").forEach((element) => { element.hidden = authenticated; });
  document.querySelectorAll(".authenticated-content").forEach((element) => { element.hidden = !authenticated; });
}

function setAuthMode(mode) {
  state.authMode = mode;
  document.querySelectorAll("[data-auth-tab]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.authTab === mode);
  });
  document.querySelector("#auth-submit").textContent = mode === "register" ? "注册并登录" : "登录";
  document.querySelector("#auth-password").autocomplete = mode === "register" ? "new-password" : "current-password";
  elements.authError.hidden = true;
}

function openAuth(mode = "login", pendingView = null) {
  state.pendingView = pendingView;
  setAuthMode(mode);
  if (!elements.authDialog.open) elements.authDialog.showModal();
  document.querySelector("#auth-username").focus();
}

async function fetchDashboard() {
  elements.refresh.classList.add("is-loading");
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`API ${response.status}`);
    state.dashboard = await response.json();
    elements.systemDot.classList.add("is-online");
    elements.generatedAt.textContent = `更新 ${new Date(state.dashboard.generated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;

    const available = marketsForAsset();
    if (state.selectedSymbol && !available.some((item) => item.symbol === state.selectedSymbol)) {
      state.selectedSymbol = null;
      state.selectedStrategyId = "";
      state.appliedStrategyId = "";
    }
    state.currentMarket = available.find((item) => item.symbol === state.selectedSymbol) ?? null;
    renderAll();
    if (state.currentMarket && state.view === "detail" && state.selectedStrategyId) {
      await loadMarketDetail();
    }
  } catch (error) {
    elements.generatedAt.textContent = "连接失败";
    elements.systemDot.classList.remove("is-online");
    elements.dataAlert.hidden = false;
    elements.dataAlert.textContent = `监控数据加载失败：${error.message}`;
  } finally {
    elements.refresh.classList.remove("is-loading");
  }
}

function marketsForAsset() {
  return (state.dashboard?.markets ?? []).filter((item) => item.asset_class === state.assetClass);
}

function renderAll() {
  renderAuthState();
  renderAssetButtons();
  renderWatchlist();
  renderMarket();
  renderStrategies();
  renderBacktests();
  if (state.allStocks !== null) renderStockCatalog();
  renderSystem();
}

function renderAssetButtons() {
  document.querySelectorAll(".asset-button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.asset === state.assetClass);
  });
  elements.watchlistTitle.textContent = `${assetLabels[state.assetClass]}观察池`;
  elements.addStockButton.hidden = !state.user || state.assetClass !== "stock";
}

function renderWatchlist() {
  const markets = marketsForAsset();
  const configured = state.dashboard?.summary?.by_asset?.[state.assetClass]?.configured ?? markets.length;
  elements.watchlistCount.textContent = String(configured);
  if (!markets.length) {
    elements.watchlist.innerHTML = `<div class="watch-empty">暂无可用${assetLabels[state.assetClass]}行情</div>`;
    return;
  }

  elements.watchlist.innerHTML = markets.map((item) => {
    const canManage = Boolean(state.user && item.asset_class === "stock");
    const actions = canManage ? `<span class="watch-actions">
      <button class="watch-action" data-refresh-symbol="${escapeHtml(item.symbol)}" title="更新日线" aria-label="更新 ${escapeHtml(item.name)} 日线">↻</button>
      <button class="watch-action" data-remove-symbol="${escapeHtml(item.symbol)}" title="移出观察池" aria-label="移除 ${escapeHtml(item.name)}">×</button>
    </span>` : "";
    return `<div class="watch-row ${canManage ? "has-actions" : ""}">
      <button class="watch-item ${item.symbol === state.selectedSymbol ? "is-active" : ""}" data-symbol="${escapeHtml(item.symbol)}">
        <span class="watch-item-top">
          <strong>${escapeHtml(item.name)}</strong>
          <span>${formatNumber(item.price)}</span>
        </span>
        <span class="watch-item-bottom">
          <span>${escapeHtml(item.symbol)}</span>
          <span class="side-${item.recommendation.toLowerCase()}">${recommendationLabels[item.recommendation]}</span>
        </span>
      </button>
      ${actions}
    </div>`;
  }).join("");

  elements.watchlist.querySelectorAll("[data-symbol]").forEach((button) => {
    button.addEventListener("click", () => selectSymbol(button.dataset.symbol));
  });
  elements.watchlist.querySelectorAll("[data-refresh-symbol]").forEach((button) => {
    button.addEventListener("click", () => refreshStock(button));
  });
  elements.watchlist.querySelectorAll("[data-remove-symbol]").forEach((button) => {
    button.addEventListener("click", () => removeStock(button.dataset.removeSymbol));
  });
}

async function refreshStock(button) {
  button.disabled = true;
  button.classList.add("is-loading");
  try {
    await apiRequest(`/api/watchlist/${encodeURIComponent(button.dataset.refreshSymbol)}/refresh`, { method: "POST" });
    state.selectedSymbol = button.dataset.refreshSymbol;
    await fetchDashboard();
    if (state.allStocks !== null) await fetchStockCatalog();
  } catch (error) {
    window.alert(error.message);
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
  }
}

async function removeStock(symbol) {
  if (!window.confirm(`确认将 ${symbol} 移出观察池？`)) return;
  try {
    await apiRequest(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
    if (state.selectedSymbol === symbol) state.selectedSymbol = null;
    await fetchDashboard();
    if (state.allStocks !== null) await fetchStockCatalog();
  } catch (error) {
    window.alert(error.message);
  }
}

function renderStockCatalog() {
  const stocks = state.allStocks ?? [];
  const query = state.stockQuery.trim().toLowerCase();
  const matchesValue = (value, selected) => {
    if (!selected) return true;
    if (selected === "__missing__") return !String(value || "").trim();
    return String(value || "") === selected;
  };
  const stockStatus = (item) => {
    if (item.price_kind === "unavailable") return "unavailable";
    if (item.freshness === "stale") return "stale";
    return item.price_kind;
  };
  const filtered = stocks.filter((item) => {
    const queryMatches = !query || (
      `${item.symbol} ${item.name} ${item.exchange} ${item.board} ${item.industry} ${item.area}`
        .toLowerCase()
        .includes(query)
    );
    const watchStatus = item.is_watched ? "watched" : "archived";
    return queryMatches
      && matchesValue(item.exchange, state.stockFilters.exchange)
      && matchesValue(item.board || item.market, state.stockFilters.board)
      && matchesValue(item.industry, state.stockFilters.industry)
      && matchesValue(item.area, state.stockFilters.area)
      && (!state.stockFilters.status || stockStatus(item) === state.stockFilters.status)
      && (!state.stockFilters.watch || watchStatus === state.stockFilters.watch);
  });
  const hasActiveFilters = Boolean(query) || Object.values(state.stockFilters).some(Boolean);
  const visible = filtered.slice(0, state.stockLimit);
  elements.stockEmpty.hidden = filtered.length > 0;
  elements.stockEmpty.textContent = hasActiveFilters ? "没有符合筛选条件的股票" : "暂无股票数据";
  elements.stockMore.hidden = visible.length >= filtered.length;
  elements.stockMore.textContent = `显示更多（${visible.length} / ${filtered.length}）`;
  elements.stockCatalogStatus.textContent = stocks.length
    ? hasActiveFilters
      ? `已筛选 ${filtered.length.toLocaleString("zh-CN")} / ${stocks.length.toLocaleString("zh-CN")} 只`
      : `共 ${stocks.length.toLocaleString("zh-CN")} 只股票`
    : "尚未同步股票清单";
  elements.stockFilterReset.disabled = !hasActiveFilters;
  elements.stockTable.innerHTML = visible.map((item) => {
    const exchangeLabels = { SSE: "上交所", SZSE: "深交所", BSE: "北交所" };
    const hasChange = item.change_pct !== null && item.change_pct !== undefined;
    const changeClass = hasChange ? (Number(item.change_pct) >= 0 ? "rec-buy" : "rec-sell") : "";
    const changeText = hasChange
      ? `${Number(item.change_pct) >= 0 ? "+" : ""}${formatNumber(item.change_pct)}%`
      : "--";
    const freshness = item.price_kind === "realtime"
      ? (item.freshness === "fresh" ? "实时快照" : "快照滞后")
      : item.price_kind === "daily"
        ? (item.freshness === "fresh" ? "日线收盘" : "日线滞后")
        : "暂无行情";
    const priceTime = item.price_time
      ? String(item.price_time).replace("T", " ").slice(0, 16)
      : "--";
    const exchange = exchangeLabels[item.exchange] || item.exchange || "--";
    const board = item.board || item.market || "--";
    let action = '<span class="owner-label">登录后管理</span>';
    if (state.user) {
      action = item.is_watched
        ? `<button class="table-action stock-action is-remove" data-catalog-remove="${escapeHtml(item.symbol)}">移出观察池</button>`
        : `<button class="primary-button stock-action" data-catalog-add="${escapeHtml(item.symbol)}">加入观察池</button>`;
    }
    return `<tr>
      <td class="symbol-cell"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.symbol)}${item.is_default ? " · 默认" : ""}</span></td>
      <td class="symbol-cell"><strong>${escapeHtml(exchange)} / ${escapeHtml(board)}</strong><span>上市 ${escapeHtml(item.list_date || "--")}</span></td>
      <td class="symbol-cell"><strong>${escapeHtml(item.industry || "--")}</strong><span>${escapeHtml(item.area || "--")}</span></td>
      <td>${formatNumber(item.price)}</td>
      <td class="${changeClass}">${changeText}</td>
      <td>${escapeHtml(priceTime)}</td>
      <td><span class="visibility-badge">${freshness}</span></td>
      <td><span class="visibility-badge ${item.is_watched ? "is-watched" : ""}">${item.is_watched ? "观察中" : "已归档"}</span></td>
      <td>${action}</td>
    </tr>`;
  }).join("");
  elements.stockTable.querySelectorAll("[data-catalog-add]").forEach((button) => {
    button.addEventListener("click", () => addCatalogStock(button));
  });
  elements.stockTable.querySelectorAll("[data-catalog-remove]").forEach((button) => {
    button.addEventListener("click", () => removeStock(button.dataset.catalogRemove));
  });
}

function populateStockFilters() {
  const stocks = state.allStocks ?? [];
  const dynamicKeys = ["exchange", "board", "industry", "area"];
  const allLabels = {
    exchange: "全部交易所",
    board: "全部板块",
    industry: "全部行业",
    area: "全部地域",
  };
  const valueLabels = {
    SSE: "上交所",
    SZSE: "深交所",
    BSE: "北交所",
    __missing__: "未标注",
  };
  dynamicKeys.forEach((key) => {
    const select = Array.from(elements.stockFilters).find(
      (item) => item.dataset.stockFilter === key
    );
    const values = new Set();
    stocks.forEach((item) => {
      const raw = key === "board" ? item.board || item.market : item[key];
      values.add(String(raw || "").trim() || "__missing__");
    });
    const options = Array.from(values).sort((left, right) => {
      const order = { SSE: 1, SZSE: 2, BSE: 3, __missing__: 99 };
      if (key === "exchange") {
        return (order[left] || 50) - (order[right] || 50);
      }
      return left.localeCompare(right, "zh-CN");
    });
    select.innerHTML = [
      `<option value="">${allLabels[key]}</option>`,
      ...options.map(
        (value) => `<option value="${escapeHtml(value)}">${escapeHtml(valueLabels[value] || value)}</option>`
      ),
    ].join("");
    if (options.includes(state.stockFilters[key])) {
      select.value = state.stockFilters[key];
    } else {
      state.stockFilters[key] = "";
    }
  });
}

async function addCatalogStock(button) {
  button.disabled = true;
  try {
    const result = await apiRequest("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ symbol: button.dataset.catalogAdd }),
    });
    state.assetClass = "stock";
    state.selectedSymbol = result.item.symbol;
    state.selectedStrategyId = "";
    state.appliedStrategyId = "";
    await fetchDashboard();
    await fetchStockCatalog();
    await switchView("detail");
  } catch (error) {
    window.alert(error.message);
  } finally {
    button.disabled = false;
  }
}

async function syncStockCatalog() {
  elements.syncStocks.disabled = true;
  elements.syncStocks.textContent = "正在同步...";
  try {
    const result = await apiRequest("/api/stocks/refresh", { method: "POST" });
    await Promise.all([fetchStockCatalog(), fetchDashboard()]);
    elements.stockCatalogStatus.textContent = `已同步 ${result.data.count.toLocaleString("zh-CN")} 只股票`;
  } catch (error) {
    window.alert(error.message);
  } finally {
    elements.syncStocks.disabled = false;
    elements.syncStocks.textContent = "同步股票清单";
  }
}

async function refreshStockQuotes() {
  elements.refreshQuotes.disabled = true;
  elements.refreshQuotes.textContent = "正在刷新...";
  try {
    const result = await apiRequest("/api/stocks/quotes/refresh", { method: "POST" });
    await fetchStockCatalog();
    elements.stockCatalogStatus.textContent = `实时行情 ${result.data.count.toLocaleString("zh-CN")} 只 · ${result.data.quote_time.replace("T", " ")}`;
  } catch (error) {
    window.alert(error.message);
  } finally {
    elements.refreshQuotes.disabled = false;
    elements.refreshQuotes.textContent = "刷新实时行情";
  }
}

function strategiesForCurrentMarket() {
  if (!state.currentMarket) return [];
  return state.strategies.filter((item) => (
    !item.is_system
    && item.symbol === state.currentMarket.symbol
    && item.asset_class === state.currentMarket.asset_class
  ));
}

function renderDetailStrategy() {
  const market = state.currentMarket;
  if (!market) {
    elements.detailStrategy.innerHTML = "";
    return;
  }
  const defaultMarket = marketsForAsset().find((item) => item.symbol === market.symbol);
  const strategies = strategiesForCurrentMarket();
  if (state.selectedStrategyId && !strategies.some((item) => String(item.id) === state.selectedStrategyId)) {
    state.selectedStrategyId = "";
  }
  elements.detailStrategy.innerHTML = [
    `<option value="">${escapeHtml(defaultMarket?.strategy_profile || "默认策略")}</option>`,
    ...strategies.map((item) => {
      const owner = item.is_owner ? "我的" : item.owner;
      return `<option value="${item.id}">${escapeHtml(item.name)} · ${escapeHtml(owner)}</option>`;
    }),
  ].join("");
  elements.detailStrategy.value = state.selectedStrategyId;
}

async function loadMarketDetail() {
  const symbol = state.selectedSymbol;
  if (!symbol) return;
  const request = ++state.detailRequest;
  const query = new URLSearchParams({ limit: String(state.range) });
  if (state.selectedStrategyId) query.set("strategy_id", state.selectedStrategyId);
  elements.detailStrategy.disabled = true;
  elements.strategyCalculating.hidden = false;
  try {
    const market = await apiRequest(`/api/markets/${encodeURIComponent(symbol)}?${query}`);
    if (request !== state.detailRequest || symbol !== state.selectedSymbol) return;
    state.currentMarket = market;
    state.appliedStrategyId = state.selectedStrategyId;
    state.hoverIndex = null;
    renderMarket();
  } catch (error) {
    if (request !== state.detailRequest) return;
    state.selectedStrategyId = state.appliedStrategyId;
    renderDetailStrategy();
    elements.dataAlert.hidden = false;
    elements.dataAlert.textContent = `策略趋势计算失败：${error.message}`;
  } finally {
    if (request === state.detailRequest) {
      elements.detailStrategy.disabled = false;
      elements.strategyCalculating.hidden = true;
    }
  }
}

function renderMarket() {
  const market = state.currentMarket;
  elements.workspace.hidden = !market;
  elements.emptyMarket.hidden = Boolean(market);
  if (!market) {
    elements.chartTooltip.hidden = true;
    elements.dataAlert.hidden = true;
    return;
  }

  renderDetailStrategy();
  elements.dataAlert.hidden = market.freshness !== "stale";
  if (market.freshness === "stale") {
    elements.dataAlert.textContent = `${assetLabels[market.asset_class]}行情存在滞后，当前展示的是数据库中最近一次更新数据，请勿按实时行情使用。`;
  }

  document.querySelector("#instrument-class").textContent = assetLabels[market.asset_class];
  document.querySelector("#instrument-exchange").textContent = market.exchange || "--";
  document.querySelector("#instrument-name").textContent = market.name;
  document.querySelector("#instrument-symbol").textContent = market.symbol;
  document.querySelector("#instrument-price").textContent = formatNumber(market.price);

  const change = document.querySelector("#instrument-change");
  change.className = market.change >= 0 ? "price-up" : "price-down";
  change.textContent = `${market.change >= 0 ? "+" : ""}${formatNumber(market.change)}  ${market.change_pct >= 0 ? "+" : ""}${formatNumber(market.change_pct)}%`;

  const freshness = document.querySelector("#freshness-badge");
  freshness.className = `freshness-badge ${market.freshness === "stale" ? "is-stale" : ""}`;
  freshness.textContent = market.freshness === "stale" ? `滞后 ${market.lag_days} 天` : "数据正常";

  const recommendation = document.querySelector("#recommendation");
  recommendation.className = recClass(market.recommendation);
  recommendation.textContent = recommendationLabels[market.recommendation];
  document.querySelector("#confidence").textContent = `方向一致度 ${market.confidence}%`;
  document.querySelector("#decision-reason").textContent = market.reason;

  const voteNames = { ma: "均线趋势", rsi: "RSI 区间", bollinger: "布林位置" };
  document.querySelector("#vote-grid").innerHTML = Object.entries(market.votes).map(([name, vote]) => `
    <div class="vote-item"><span>${voteNames[name]}</span><strong class="${recClass(vote)}">${voteLabels[vote]}</strong></div>
  `).join("");

  const indicators = market.indicators;
  document.querySelector("#indicator-row").innerHTML = [
    [`MA${market.parameters.ma_fast}`, indicators.ma_fast],
    [`MA${market.parameters.ma_slow}`, indicators.ma_slow],
    [`RSI${market.parameters.rsi_period}`, indicators.rsi],
    ["布林中轨", indicators.bb_middle],
  ].map(([label, value]) => `<div class="indicator-item"><span>${label}</span><strong>${formatNumber(value)}</strong></div>`).join("");

  renderSignals(market.signals);
  drawChart();
}

function renderSignals(signals) {
  document.querySelector("#signal-count").textContent = `${signals.length} 条`;
  const list = document.querySelector("#signal-list");
  if (!signals.length) {
    list.innerHTML = '<div class="signal-empty">当前数据区间没有明确方向变化</div>';
    return;
  }
  list.innerHTML = signals.slice(0, 5).map((signal) => `
    <div class="signal-item">
      <time>${shortDate(signal.date)}</time>
      <strong class="${recClass(signal.side)}">${recommendationLabels[signal.side]}</strong>
      <span>${escapeHtml(signal.reason)}</span>
    </div>
  `).join("");
}

function renderStrategies() {
  const strategies = state.strategies.filter((item) => item.asset_class === state.assetClass);
  const body = document.querySelector("#strategy-table");
  const empty = document.querySelector("#strategy-empty");
  if (!state.user) {
    body.innerHTML = "";
    return;
  }
  document.querySelector("#strategy-subtitle").textContent = `${assetLabels[state.assetClass]} · 我的策略与开放策略`;
  empty.hidden = strategies.length > 0;
  body.innerHTML = strategies.map((item) => {
    const params = item.parameters ?? {};
    const parameterText = `MA ${params.ma_fast ?? "--"}/${params.ma_slow ?? "--"} · RSI ${params.rsi_period ?? "--"} · BB ${params.bb_period ?? "--"}/${params.bb_std ?? "--"}`;
    const action = item.is_owner
      ? `<button class="table-action" data-strategy-id="${item.id}" data-visibility="${item.visibility}">${item.visibility === "public" ? "设为私有" : "设为开放"}</button>`
      : "--";
    return `<tr>
      <td class="symbol-cell"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.profile)} · ${escapeHtml(item.symbol)}</span></td>
      <td><span class="owner-label">${escapeHtml(item.owner)}</span></td>
      <td><span class="market-tag">${assetLabels[item.asset_class]}</span></td>
      <td class="parameter-cell">${escapeHtml(parameterText)}</td>
      <td><span class="visibility-badge ${item.visibility === "public" ? "is-public" : ""}">${item.visibility === "public" ? "开放" : "私有"}</span></td>
      <td class="${recClass(item.recommendation)}">${recommendationLabels[item.recommendation] ?? "--"}</td>
      <td>${action}</td>
    </tr>`;
  }).join("");
  body.querySelectorAll("[data-strategy-id]").forEach((button) => {
    button.addEventListener("click", () => toggleStrategyVisibility(button));
  });
}

function renderBacktests() {
  const backtests = state.backtests.filter((item) => item.asset_class === state.assetClass);
  const body = document.querySelector("#backtest-table");
  const empty = document.querySelector("#backtest-empty");
  if (!state.user) {
    body.innerHTML = "";
    return;
  }
  empty.hidden = backtests.length > 0;
  body.innerHTML = backtests.map((item) => `
    <tr>
      <td class="symbol-cell"><strong>${escapeHtml(item.name || item.symbol)}</strong><span>${escapeHtml(item.strategy_name || "未关联策略")} · ${escapeHtml(item.symbol)}</span></td>
      <td>${escapeHtml(item.start_date || "--")} — ${escapeHtml(item.end_date || "--")}</td>
      <td class="${Number(item.total_return) >= 0 ? "rec-buy" : "rec-sell"}">${formatNumber(item.total_return)}%</td>
      <td>${formatNumber(item.max_drawdown)}%</td>
      <td>${formatNumber(item.win_rate)}%</td>
      <td>${escapeHtml(item.total_trades ?? 0)}</td>
      <td>${escapeHtml(item.created_at || "--")}</td>
    </tr>
  `).join("");
}

async function toggleStrategyVisibility(button) {
  button.disabled = true;
  try {
    const visibility = button.dataset.visibility === "public" ? "private" : "public";
    await apiRequest(`/api/strategies/${button.dataset.strategyId}/visibility`, {
      method: "PATCH",
      body: JSON.stringify({ visibility }),
    });
    await loadProtectedData();
  } catch (error) {
    window.alert(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderSystem() {
  const system = state.dashboard?.system ?? {};
  const assetSummary = state.dashboard?.summary?.by_asset?.[state.assetClass] ?? {};
  document.querySelector("#system-grid").innerHTML = [
    ["API 服务", system.api === "online" ? "运行中" : "异常", "FastAPI dashboard"],
    ["行情存储", system.data_source || "--", `${assetSummary.available ?? 0} 个标的可用`],
    ["数据库", system.database || "--", "行情、用户、策略与回测"],
    ["当前市场", assetLabels[state.assetClass], `${assetSummary.configured ?? 0} 个已配置`],
  ].map(([label, value, note]) => `
    <div class="system-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(note)}</small></div>
  `).join("");

  const errors = (system.errors ?? []).filter((item) => !item.asset_class || item.asset_class === state.assetClass);
  document.querySelector("#error-count").textContent = `${errors.length} 项`;
  document.querySelector("#error-list").innerHTML = errors.length
    ? errors.map((item) => `<div class="error-item"><strong>${escapeHtml(item.symbol)}</strong><span>${escapeHtml(item.error)}</span></div>`).join("")
    : '<div class="signal-empty">当前市场没有数据读取异常</div>';
}

function chartBars() {
  return (state.currentMarket?.bars ?? []).slice(-state.range);
}

function drawChart() {
  const canvas = elements.chart;
  const frame = canvas.parentElement;
  const bars = chartBars();
  if (!bars.length || frame.clientWidth === 0) {
    elements.chartEmpty.hidden = false;
    elements.chartEmpty.textContent = "暂无图表数据";
    return;
  }
  elements.chartEmpty.hidden = true;

  const width = frame.clientWidth;
  const height = frame.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const margin = { top: 20, right: 64, bottom: 26, left: 14 };
  const volumeHeight = state.indicators.volume ? 62 : 0;
  const chartBottom = height - margin.bottom - volumeHeight;
  const chartWidth = width - margin.left - margin.right;
  const chartHeight = chartBottom - margin.top;
  const candleStep = chartWidth / bars.length;
  const candleWidth = Math.max(2, Math.min(9, candleStep * 0.62));

  const priceValues = bars.flatMap((bar) => {
    const values = [bar.low, bar.high];
    if (state.indicators.bb) values.push(bar.bb_lower, bar.bb_upper);
    return values.filter((value) => value !== null && value !== undefined);
  });
  let priceMin = Math.min(...priceValues);
  let priceMax = Math.max(...priceValues);
  const pricePadding = Math.max((priceMax - priceMin) * 0.08, priceMax * 0.005);
  priceMin -= pricePadding;
  priceMax += pricePadding;
  const priceY = (value) => margin.top + ((priceMax - value) / (priceMax - priceMin || 1)) * chartHeight;
  const xAt = (index) => margin.left + candleStep * index + candleStep / 2;

  context.strokeStyle = "#e5eaee";
  context.fillStyle = "#7d8993";
  context.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
  context.lineWidth = 1;
  for (let grid = 0; grid <= 4; grid += 1) {
    const y = margin.top + chartHeight * grid / 4;
    context.beginPath();
    context.moveTo(margin.left, y + 0.5);
    context.lineTo(width - margin.right, y + 0.5);
    context.stroke();
    const price = priceMax - (priceMax - priceMin) * grid / 4;
    context.fillText(formatNumber(price), width - margin.right + 8, y + 3);
  }

  const signalMap = new Map((state.currentMarket.signals ?? []).map((item) => [item.date, item.side]));
  bars.forEach((bar, index) => {
    const x = xAt(index);
    const color = bar.close >= bar.open ? "#c23845" : "#12805c";
    context.strokeStyle = color;
    context.fillStyle = color;
    context.beginPath();
    context.moveTo(x, priceY(bar.high));
    context.lineTo(x, priceY(bar.low));
    context.stroke();
    const bodyTop = priceY(Math.max(bar.open, bar.close));
    const bodyBottom = priceY(Math.min(bar.open, bar.close));
    context.fillRect(x - candleWidth / 2, bodyTop, candleWidth, Math.max(bodyBottom - bodyTop, 1));

    const signal = signalMap.get(bar.date);
    if (signal) {
      context.fillStyle = signal === "BUY" ? "#c23845" : "#12805c";
      context.beginPath();
      const markerY = signal === "BUY" ? priceY(bar.low) + 10 : priceY(bar.high) - 10;
      context.arc(x, markerY, 3, 0, Math.PI * 2);
      context.fill();
    }
  });

  function drawLine(key, color, lineWidth = 1.3, dash = []) {
    context.strokeStyle = color;
    context.lineWidth = lineWidth;
    context.setLineDash(dash);
    context.beginPath();
    let active = false;
    bars.forEach((bar, index) => {
      const value = bar[key];
      if (value === null || value === undefined) {
        active = false;
        return;
      }
      const x = xAt(index);
      const y = priceY(value);
      if (!active) context.moveTo(x, y);
      else context.lineTo(x, y);
      active = true;
    });
    context.stroke();
    context.setLineDash([]);
  }

  if (state.indicators.ma) {
    drawLine("ma_fast", "#176b87", 1.5);
    drawLine("ma_slow", "#b57920", 1.5);
  }
  if (state.indicators.bb) {
    drawLine("bb_upper", "#9b7ab2", 1, [4, 3]);
    drawLine("bb_middle", "#a8b1b9", 1, [2, 3]);
    drawLine("bb_lower", "#9b7ab2", 1, [4, 3]);
  }

  if (state.indicators.volume) {
    const volumeTop = chartBottom + 12;
    const maxVolume = Math.max(...bars.map((bar) => bar.volume), 1);
    bars.forEach((bar, index) => {
      const x = xAt(index);
      const barHeight = (bar.volume / maxVolume) * (volumeHeight - 18);
      context.fillStyle = bar.close >= bar.open ? "rgba(194,56,69,.35)" : "rgba(18,128,92,.35)";
      context.fillRect(x - candleWidth / 2, height - margin.bottom - barHeight, candleWidth, barHeight);
    });
    context.fillStyle = "#8c98a3";
    context.fillText("VOL", margin.left, volumeTop + 5);
  }

  const labelCount = Math.min(5, bars.length);
  context.fillStyle = "#7d8993";
  for (let index = 0; index < labelCount; index += 1) {
    const barIndex = Math.round((bars.length - 1) * index / Math.max(labelCount - 1, 1));
    const label = shortDate(bars[barIndex].date);
    const x = xAt(barIndex);
    context.fillText(label, Math.min(x - 14, width - margin.right - 28), height - 8);
  }

  if (state.hoverIndex !== null && bars[state.hoverIndex]) {
    const x = xAt(state.hoverIndex);
    context.strokeStyle = "#7f8a93";
    context.setLineDash([3, 3]);
    context.beginPath();
    context.moveTo(x, margin.top);
    context.lineTo(x, height - margin.bottom);
    context.stroke();
    context.setLineDash([]);
  }

  state.chartGeometry = { margin, chartWidth, candleStep, bars, width, height };
}

async function selectSymbol(symbol) {
  state.selectedSymbol = symbol;
  state.selectedStrategyId = "";
  state.appliedStrategyId = "";
  state.hoverIndex = null;
  state.currentMarket = marketsForAsset().find((item) => item.symbol === symbol) ?? null;
  renderWatchlist();
  renderMarket();
  await switchView("detail");
}

function switchAsset(assetClass) {
  state.assetClass = assetClass;
  state.hoverIndex = null;
  state.selectedSymbol = null;
  state.selectedStrategyId = "";
  state.appliedStrategyId = "";
  state.currentMarket = null;
  renderAll();
}

async function switchView(view) {
  state.view = view;
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("is-active", section.id === `view-${view}`));
  if (view === "detail") requestAnimationFrame(drawChart);
  if (view === "stocks") await fetchStockCatalog();
  if (state.user && ["strategies", "backtests"].includes(view)) await loadProtectedData();
}

document.querySelectorAll(".asset-button").forEach((button) => button.addEventListener("click", () => switchAsset(button.dataset.asset)));
document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
document.querySelectorAll("[data-auth-mode]").forEach((button) => {
  button.addEventListener("click", () => openAuth(button.dataset.authMode, state.view));
});
document.querySelectorAll("[data-auth-tab]").forEach((button) => {
  button.addEventListener("click", () => setAuthMode(button.dataset.authTab));
});
document.querySelectorAll("[data-range]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.range = Number(button.dataset.range);
    document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("is-active", item === button));
    if (state.selectedSymbol && state.range > (state.currentMarket?.bars?.length ?? 0)) {
      await loadMarketDetail();
    }
    state.hoverIndex = null;
    renderMarket();
  });
});
elements.detailStrategy.addEventListener("change", async () => {
  state.selectedStrategyId = elements.detailStrategy.value;
  await loadMarketDetail();
});
document.querySelectorAll("[data-indicator]").forEach((input) => {
  input.addEventListener("change", () => {
    state.indicators[input.dataset.indicator] = input.checked;
    drawChart();
  });
});

elements.refresh.addEventListener("click", async () => {
  await fetchDashboard();
  if (state.view === "stocks") await fetchStockCatalog();
});
elements.syncStocks.addEventListener("click", syncStockCatalog);
elements.refreshQuotes.addEventListener("click", refreshStockQuotes);
elements.stockSearch.addEventListener("input", () => {
  state.stockQuery = elements.stockSearch.value;
  state.stockLimit = 200;
  renderStockCatalog();
});
elements.stockFilters.forEach((select) => {
  select.addEventListener("change", () => {
    state.stockFilters[select.dataset.stockFilter] = select.value;
    state.stockLimit = 200;
    renderStockCatalog();
  });
});
elements.stockFilterReset.addEventListener("click", () => {
  state.stockQuery = "";
  state.stockLimit = 200;
  elements.stockSearch.value = "";
  Object.keys(state.stockFilters).forEach((key) => {
    state.stockFilters[key] = "";
  });
  elements.stockFilters.forEach((select) => {
    select.value = "";
  });
  renderStockCatalog();
});
elements.stockMore.addEventListener("click", () => {
  state.stockLimit += 200;
  renderStockCatalog();
});
elements.accountButton.addEventListener("click", () => openAuth("login"));
document.querySelector("#logout-button").addEventListener("click", async () => {
  await apiRequest("/api/auth/logout", { method: "POST" });
  state.user = null;
  state.strategies = [];
  state.backtests = [];
  state.allStocks = null;
  state.selectedStrategyId = "";
  state.appliedStrategyId = "";
  await fetchDashboard();
});
elements.addStockButton.addEventListener("click", () => {
  elements.stockError.hidden = true;
  elements.stockDialog.showModal();
  elements.stockForm.elements.symbol.focus();
});
document.querySelector("#new-strategy-button").addEventListener("click", async () => {
  elements.strategyError.hidden = true;
  if (state.allStocks === null) await fetchStockCatalog();
  populateStrategyStocks();
  updateStrategySymbolField();
  if (!state.allStocks.length) {
    elements.strategyError.textContent = "请先到全部数据页面同步股票清单";
    elements.strategyError.hidden = false;
  }
  elements.strategyDialog.showModal();
});
elements.strategyAssetClass.addEventListener("change", updateStrategySymbolField);
elements.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.authError.hidden = true;
  const payload = {
    username: elements.authForm.elements.username.value.trim(),
    password: elements.authForm.elements.password.value,
  };
  try {
    const result = await apiRequest(`/api/auth/${state.authMode}`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.user = result.user;
    elements.authDialog.close();
    elements.authForm.reset();
    renderAuthState();
    await fetchDashboard();
    if (state.allStocks !== null) await fetchStockCatalog();
    await loadProtectedData();
    if (state.pendingView) await switchView(state.pendingView);
    state.pendingView = null;
  } catch (error) {
    elements.authError.textContent = error.message;
    elements.authError.hidden = false;
  }
});
elements.stockForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.stockError.hidden = true;
  const submit = elements.stockForm.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    const result = await apiRequest("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ symbol: elements.stockForm.elements.symbol.value.trim() }),
    });
    state.selectedSymbol = result.item.symbol;
    state.assetClass = "stock";
    state.selectedStrategyId = "";
    state.appliedStrategyId = "";
    elements.stockDialog.close();
    elements.stockForm.reset();
    await fetchDashboard();
    if (state.allStocks !== null) await fetchStockCatalog();
    await switchView("detail");
  } catch (error) {
    elements.stockError.textContent = error.message;
    elements.stockError.hidden = false;
  } finally {
    submit.disabled = false;
  }
});
elements.strategyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.strategyError.hidden = true;
  const form = new FormData(elements.strategyForm);
  const payload = {
    name: form.get("name"),
    asset_class: form.get("asset_class"),
    symbol: form.get("asset_class") === "stock"
      ? form.get("stock_symbol")
      : form.get("future_symbol"),
    profile: form.get("profile"),
    visibility: form.get("visibility"),
    parameters: {
      ma_fast: Number(form.get("ma_fast")),
      ma_slow: Number(form.get("ma_slow")),
      rsi_period: Number(form.get("rsi_period")),
      rsi_oversold: 30,
      rsi_overbought: 70,
      bb_period: Number(form.get("bb_period")),
      bb_std: Number(form.get("bb_std")),
    },
  };
  try {
    await apiRequest("/api/strategies", { method: "POST", body: JSON.stringify(payload) });
    elements.strategyDialog.close();
    elements.strategyForm.reset();
    await loadProtectedData();
  } catch (error) {
    elements.strategyError.textContent = error.message;
    elements.strategyError.hidden = false;
  }
});
elements.chart.addEventListener("mousemove", (event) => {
  const geometry = state.chartGeometry;
  if (!geometry) return;
  const rect = elements.chart.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const index = Math.max(0, Math.min(geometry.bars.length - 1, Math.floor((x - geometry.margin.left) / geometry.candleStep)));
  state.hoverIndex = index;
  const bar = geometry.bars[index];
  elements.chartTooltip.hidden = false;
  elements.chartTooltip.style.left = `${Math.min(x + 14, rect.width - 174)}px`;
  elements.chartTooltip.style.top = "18px";
  elements.chartTooltip.innerHTML = `${escapeHtml(bar.date)}<br>开 ${formatNumber(bar.open)}　高 ${formatNumber(bar.high)}<br>低 ${formatNumber(bar.low)}　收 ${formatNumber(bar.close)}<br>量 ${formatNumber(bar.volume, 0)}`;
  drawChart();
});
elements.chart.addEventListener("mouseleave", () => {
  state.hoverIndex = null;
  elements.chartTooltip.hidden = true;
  drawChart();
});

new ResizeObserver(() => {
  if (state.view === "detail") drawChart();
}).observe(elements.chart.parentElement);

async function bootstrap() {
  try {
    await fetchCurrentUser();
    await fetchDashboard();
    if (state.user) await loadProtectedData();
  } catch (error) {
    elements.generatedAt.textContent = "连接失败";
    elements.dataAlert.hidden = false;
    elements.dataAlert.textContent = `应用初始化失败：${error.message}`;
  }
}

bootstrap();
