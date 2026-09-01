const state = {
  dashboard: null,
  assetClass: "stock",
  selectedSymbol: null,
  currentMarket: null,
  view: "overview",
  range: 120,
  indicators: { ma: true, bb: true, volume: true },
  hoverIndex: null,
};

const elements = {
  refresh: document.querySelector("#refresh-button"),
  generatedAt: document.querySelector("#generated-at"),
  systemDot: document.querySelector("#system-dot"),
  watchlist: document.querySelector("#watchlist"),
  watchlistTitle: document.querySelector("#watchlist-title"),
  watchlistCount: document.querySelector("#watchlist-count"),
  metrics: document.querySelector("#metrics-band"),
  dataAlert: document.querySelector("#data-alert"),
  workspace: document.querySelector("#market-workspace"),
  emptyMarket: document.querySelector("#empty-market"),
  chart: document.querySelector("#market-chart"),
  chartTooltip: document.querySelector("#chart-tooltip"),
  chartEmpty: document.querySelector("#chart-empty"),
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

async function fetchDashboard() {
  elements.refresh.classList.add("is-loading");
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`API ${response.status}`);
    state.dashboard = await response.json();
    elements.systemDot.classList.add("is-online");
    elements.generatedAt.textContent = `更新 ${new Date(state.dashboard.generated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;

    const available = marketsForAsset();
    if (!state.selectedSymbol || !available.some((item) => item.symbol === state.selectedSymbol)) {
      state.selectedSymbol = available[0]?.symbol ?? null;
    }
    state.currentMarket = available.find((item) => item.symbol === state.selectedSymbol) ?? null;
    renderAll();
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
  renderAssetButtons();
  renderWatchlist();
  renderMetrics();
  renderMarket();
  renderStrategies();
  renderBacktests();
  renderSystem();
}

function renderAssetButtons() {
  document.querySelectorAll(".asset-button").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.asset === state.assetClass);
  });
  elements.watchlistTitle.textContent = `${assetLabels[state.assetClass]}观察池`;
}

function renderWatchlist() {
  const markets = marketsForAsset();
  const configured = state.dashboard?.summary?.by_asset?.[state.assetClass]?.configured ?? markets.length;
  elements.watchlistCount.textContent = String(configured);
  if (!markets.length) {
    elements.watchlist.innerHTML = `<div class="watch-empty">暂无可用${assetLabels[state.assetClass]}行情</div>`;
    return;
  }

  elements.watchlist.innerHTML = markets.map((item) => `
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
  `).join("");

  elements.watchlist.querySelectorAll("[data-symbol]").forEach((button) => {
    button.addEventListener("click", () => selectSymbol(button.dataset.symbol));
  });
}

function renderMetrics() {
  const summary = state.dashboard?.summary?.by_asset?.[state.assetClass] ?? {};
  const markets = marketsForAsset();
  const stale = markets.filter((item) => item.freshness === "stale").length;
  elements.metrics.innerHTML = [
    ["已配置标的", summary.configured ?? 0, `${summary.available ?? 0} 个有行情`],
    ["偏多", summary.buy ?? 0, "达到策略投票阈值"],
    ["偏空", summary.sell ?? 0, "达到策略投票阈值"],
    ["观望", summary.hold ?? 0, "指标方向未统一"],
    ["数据状态", stale ? `${stale} 个滞后` : "正常", markets[0]?.latest_date ?? "暂无数据"],
  ].map(([label, value, note]) => `
    <div class="metric"><span>${label}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(note)}</small></div>
  `).join("");

  elements.dataAlert.hidden = stale === 0;
  if (stale) {
    elements.dataAlert.textContent = `${assetLabels[state.assetClass]}行情缓存存在滞后，页面展示的是最近一次落盘数据，请勿按实时行情使用。`;
  }
}

function renderMarket() {
  const market = state.currentMarket;
  elements.workspace.hidden = !market;
  elements.emptyMarket.hidden = Boolean(market);
  if (!market) {
    elements.chartTooltip.hidden = true;
    return;
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
  document.querySelector("#strategy-profile").textContent = market.strategy_profile;
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
  const strategies = (state.dashboard?.strategies ?? []).filter((item) => item.asset_class === state.assetClass);
  const body = document.querySelector("#strategy-table");
  const empty = document.querySelector("#strategy-empty");
  document.querySelector("#strategy-subtitle").textContent = `${assetLabels[state.assetClass]}策略独立参数与当前倾向`;
  empty.hidden = strategies.length > 0;
  body.innerHTML = strategies.map((item) => {
    const params = item.parameters;
    const parameterText = `MA ${params.ma_fast}/${params.ma_slow} · RSI ${params.rsi_period}/${params.rsi_oversold}/${params.rsi_overbought} · BB ${params.bb_period}/${params.bb_std}`;
    return `<tr>
      <td class="symbol-cell"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.symbol)}</span></td>
      <td><span class="market-tag">${assetLabels[item.asset_class]}</span></td>
      <td>${escapeHtml(item.profile)}</td>
      <td class="parameter-cell">${escapeHtml(parameterText)}</td>
      <td class="${recClass(item.recommendation)}">${recommendationLabels[item.recommendation]}</td>
      <td>${item.confidence}%</td>
    </tr>`;
  }).join("");
}

function renderBacktests() {
  const backtests = (state.dashboard?.backtests ?? []).filter((item) => item.asset_class === state.assetClass);
  const body = document.querySelector("#backtest-table");
  const empty = document.querySelector("#backtest-empty");
  empty.hidden = backtests.length > 0;
  body.innerHTML = backtests.map((item) => `
    <tr>
      <td class="symbol-cell"><strong>${escapeHtml(item.name || item.symbol)}</strong><span>${escapeHtml(item.symbol)}</span></td>
      <td>${escapeHtml(item.start_date || "--")} — ${escapeHtml(item.end_date || "--")}</td>
      <td class="${Number(item.total_return) >= 0 ? "rec-buy" : "rec-sell"}">${formatNumber(item.total_return)}%</td>
      <td>${formatNumber(item.max_drawdown)}%</td>
      <td>${formatNumber(item.win_rate)}%</td>
      <td>${escapeHtml(item.total_trades ?? 0)}</td>
      <td>${escapeHtml(item.created_at || "--")}</td>
    </tr>
  `).join("");
}

function renderSystem() {
  const system = state.dashboard?.system ?? {};
  const assetSummary = state.dashboard?.summary?.by_asset?.[state.assetClass] ?? {};
  document.querySelector("#system-grid").innerHTML = [
    ["API 服务", system.api === "online" ? "运行中" : "异常", "FastAPI dashboard"],
    ["行情存储", system.data_source || "--", `${assetSummary.available ?? 0} 个标的可用`],
    ["数据库", system.database || "--", "PostgreSQL 待迁移"],
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
  state.hoverIndex = null;
  state.currentMarket = marketsForAsset().find((item) => item.symbol === symbol) ?? null;
  renderWatchlist();
  renderMarket();
}

function switchAsset(assetClass) {
  state.assetClass = assetClass;
  state.hoverIndex = null;
  const markets = marketsForAsset();
  state.selectedSymbol = markets[0]?.symbol ?? null;
  state.currentMarket = markets[0] ?? null;
  renderAll();
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".nav-button").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("is-active", section.id === `view-${view}`));
  if (view === "overview") requestAnimationFrame(drawChart);
}

document.querySelectorAll(".asset-button").forEach((button) => button.addEventListener("click", () => switchAsset(button.dataset.asset)));
document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
document.querySelectorAll("[data-range]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.range = Number(button.dataset.range);
    document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("is-active", item === button));
    if (state.selectedSymbol && state.range > (state.currentMarket?.bars?.length ?? 0)) {
      const response = await fetch(`/api/markets/${encodeURIComponent(state.selectedSymbol)}?limit=${state.range}`, { cache: "no-store" });
      if (response.ok) state.currentMarket = await response.json();
    }
    state.hoverIndex = null;
    renderMarket();
  });
});
document.querySelectorAll("[data-indicator]").forEach((input) => {
  input.addEventListener("change", () => {
    state.indicators[input.dataset.indicator] = input.checked;
    drawChart();
  });
});

elements.refresh.addEventListener("click", fetchDashboard);
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
  if (state.view === "overview") drawChart();
}).observe(elements.chart.parentElement);

fetchDashboard();
