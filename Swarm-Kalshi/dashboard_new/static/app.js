/* ============================================================
   Kalshi Swarm Dashboard — app.js
   Vanilla JS, no framework dependencies.
   Auto-refresh every 15 seconds.
   ============================================================ */

'use strict';

// ── State ──────────────────────────────────────────────────
const State = {
  activeTab: 'overview',
  tradeFilter: 'all',
  configEditMode: false,
  equityChart: null,
  lastRefresh: null,
  data: {
    status: null,
    llm: null,
    trades: null,
    risk: null,
    system: null,
    equity: null,
    config: null,
  },
};

// ── Utilities ──────────────────────────────────────────────
function fmt$(cents) {
  if (cents == null) return '$—';
  const d = cents / 100;
  const sign = d < 0 ? '-' : '';
  return sign + '$' + Math.abs(d).toFixed(2);
}

function fmtPct(val, decimals = 1) {
  if (val == null) return '—';
  const n = parseFloat(val);
  return isNaN(n) ? '—' : n.toFixed(decimals) + '%';
}

function fmtUptime(sec) {
  if (!sec) return '—';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch { return ts; }
}

function fmtDateTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch { return ts; }
}

function clamp(val, lo, hi) {
  return Math.max(lo, Math.min(hi, val));
}

function progressBar(pct, cls = '', tall = false) {
  const w = clamp(pct || 0, 0, 100).toFixed(1);
  const trackCls = tall ? 'progress-track tall' : 'progress-track';
  return `<div class="${trackCls}"><div class="progress-fill ${cls}" style="width:${w}%"></div></div>`;
}

function badge(text, cls) {
  return `<span class="badge badge-${cls}">${text}</span>`;
}

function esc(str) {
  if (str == null) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Toast ──────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg, isError = false) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}

// ── API fetch ──────────────────────────────────────────────
async function apiFetch(url, options = {}) {
  try {
    const resp = await fetch(url, options);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } catch (err) {
    console.warn(`[fetch] ${url}:`, err);
    return null;
  }
}

// ── Data loading ───────────────────────────────────────────
async function loadAll() {
  const [status, llm, trades, risk, system, equity, config] = await Promise.allSettled([
    apiFetch('/api/status'),
    apiFetch('/api/llm'),
    apiFetch('/api/trades'),
    apiFetch('/api/risk'),
    apiFetch('/api/system'),
    apiFetch('/api/equity'),
    apiFetch('/api/config'),
  ]);

  State.data.status = status.status === 'fulfilled' ? status.value : null;
  State.data.llm    = llm.status    === 'fulfilled' ? llm.value    : null;
  State.data.trades = trades.status === 'fulfilled' ? trades.value : null;
  State.data.risk   = risk.status   === 'fulfilled' ? risk.value   : null;
  State.data.system = system.status === 'fulfilled' ? system.value : null;
  State.data.equity = equity.status === 'fulfilled' ? equity.value : null;
  State.data.config = config.status === 'fulfilled' ? config.value : null;

  State.lastRefresh = new Date();
  updateRefreshBadge();
  renderActiveTab();
}

function updateRefreshBadge() {
  const el = document.getElementById('refresh-time');
  if (el && State.lastRefresh) {
    el.textContent = 'Updated ' + State.lastRefresh.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
}

// ── Tab routing ────────────────────────────────────────────
function renderActiveTab() {
  switch (State.activeTab) {
    case 'overview': renderOverview(); break;
    case 'llm':      renderLLM();      break;
    case 'trades':   renderTrades();   break;
    case 'risk':     renderRisk();     break;
    case 'system':   renderSystem();   break;
    case 'controls': renderControls(); break;
    case 'config':   renderConfig();   break;
    case 'admin':    renderAdmin();    break;
  }
}

function switchTab(name) {
  State.activeTab = name;
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === `tab-${name}`);
  });
  renderActiveTab();
}

// ── Overview ───────────────────────────────────────────────
function renderOverview() {
  const s = State.data.status;
  const bots = ['sentinel', 'oracle', 'pulse', 'vanguard'];

  // Portfolio hero
  const portfolioCents = s?.portfolio_cents ?? 0;
  const changePct = s?.portfolio_change_pct ?? 0;
  const changePos = changePct >= 0;
  document.getElementById('ov-portfolio-total').textContent = fmt$(portfolioCents);
  const changeEl = document.getElementById('ov-portfolio-change');
  changeEl.textContent = (changePos ? '+' : '') + fmtPct(changePct) + ' today';
  changeEl.className = 'portfolio-change ' + (changePos ? 'positive' : 'negative');

  // Bot cards
  const container = document.getElementById('ov-bot-cards');
  if (container) {
    container.innerHTML = bots.map(bot => {
      const b = s?.bots?.[bot] ?? {};
      const balance = b.balance_cents ?? 0;
      const pnl = b.daily_pnl_cents ?? 0;
      const trades = b.daily_trades ?? 0;
      const maxTrades = b.max_trades ?? 8;
      const paused = b.paused;
      const active = b.active !== false;
      const pnlPos = pnl >= 0;

      let statusBadge, accentCls;
      if (!active) {
        statusBadge = badge('inactive', 'red');
        accentCls = 'inactive';
      } else if (paused) {
        statusBadge = badge('paused', 'orange');
        accentCls = 'paused';
      } else {
        statusBadge = badge('active', 'green');
        accentCls = '';
      }

      return `<div class="bot-card">
        <div class="bot-card-accent ${accentCls}"></div>
        <div class="bot-card-header">
          <div class="bot-name">${bot}</div>
          ${statusBadge}
        </div>
        <div class="bot-card-body">
          <div>
            <div class="bot-stat-label">Balance</div>
            <div class="bot-stat-value">${fmt$(balance)}</div>
          </div>
          <div>
            <div class="bot-stat-label">Daily PnL</div>
            <div class="bot-stat-value ${pnlPos ? 'positive' : 'negative'}">${pnlPos ? '+' : ''}${fmt$(pnl)}</div>
          </div>
          <div>
            <div class="bot-stat-label">Trades</div>
            <div class="bot-stat-value">${trades}/${maxTrades}</div>
          </div>
          <div>
            <div class="bot-stat-label">Can Trade</div>
            <div class="bot-stat-value">${b.can_trade !== false ? badge('yes','green') : badge('no','red')}</div>
          </div>
        </div>
      </div>`;
    }).join('');
  }

  // Equity chart
  renderEquityChart();

  // Stats row
  const llm = State.data.llm;
  const sys = State.data.system;
  const cleanWr = llm?.clean_period?.win_rate_pct ?? llm?.clean_period?.win_rate_pct ?? 0;
  const llmApproval = llm?.today?.approval_rate_pct ?? 0;
  const tavily = sys?.tavily;
  const uptime = s?.uptime_seconds ?? sys?.uptime_seconds ?? 0;

  const statsEl = document.getElementById('ov-stats-row');
  if (statsEl) {
    statsEl.innerHTML = `
      <div class="stat-box">
        <div class="stat-box-label">Win Rate</div>
        <div class="stat-box-value ${cleanWr >= 55 ? 'green' : (cleanWr > 0 ? '' : 'red')}">${fmtPct(cleanWr)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-label">LLM Approval</div>
        <div class="stat-box-value blue">${fmtPct(llmApproval)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-label">Tavily Credits</div>
        <div class="stat-box-value ${tavily?.used_today > 24 ? 'orange' : ''}">${tavily?.used_today ?? 0}/${tavily?.budget ?? 30}</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-label">Uptime</div>
        <div class="stat-box-value">${fmtUptime(uptime)}</div>
      </div>
    `;
  }
}

function renderEquityChart() {
  const equity = State.data.equity;
  const canvas = document.getElementById('equity-chart');
  if (!canvas) return;

  const points = Array.isArray(equity) ? equity : [];
  const labels = points.map(p => {
    if (p.timestamp) return fmtTime(p.timestamp);
    if (p.t) return fmtTime(p.t);
    return '';
  });
  const values = points.map(p => {
    const v = p.portfolio_cents ?? p.value ?? p.v ?? 0;
    return (v / 100).toFixed(2);
  });

  if (State.equityChart) {
    State.equityChart.data.labels = labels;
    State.equityChart.data.datasets[0].data = values;
    State.equityChart.update('none');
    return;
  }

  // Chart.js must be loaded
  if (typeof Chart === 'undefined') return;

  State.equityChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Portfolio ($)',
        data: values,
        borderColor: '#3fb950',
        backgroundColor: 'rgba(63,185,80,0.08)',
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        fill: true,
        tension: 0.35,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#161b22',
          borderColor: '#30363d',
          borderWidth: 1,
          titleColor: '#c9d1d9',
          bodyColor: '#3fb950',
          callbacks: {
            label: ctx => ' $' + parseFloat(ctx.raw).toFixed(2),
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: '#8b949e',
            font: { size: 10 },
            maxTicksLimit: 8,
            maxRotation: 0,
          },
          grid: { color: 'rgba(48,54,61,0.5)' },
        },
        y: {
          ticks: {
            color: '#8b949e',
            font: { size: 10 },
            callback: v => '$' + v,
          },
          grid: { color: 'rgba(48,54,61,0.5)' },
        },
      },
    },
  });
}

// ── LLM Intelligence ───────────────────────────────────────
function renderLLM() {
  const d = State.data.llm;
  if (!d) { document.getElementById('tab-llm').innerHTML = '<div class="content"><p class="text-muted">No LLM data available.</p></div>'; return; }

  const today = d.today ?? {};
  const cp = d.clean_period ?? {};
  const recent = d.recent_decisions ?? [];

  // Header stats
  const h = document.getElementById('llm-header-stats');
  if (h) {
    h.innerHTML = `
      <div class="stat-box">
        <div class="stat-box-label">Evaluated Today</div>
        <div class="stat-box-value">${today.total ?? 0}</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-label">Approved</div>
        <div class="stat-box-value green">${today.approved ?? 0}</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-label">Approval Rate</div>
        <div class="stat-box-value blue">${fmtPct(today.approval_rate_pct)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-label">Real LLM %</div>
        <div class="stat-box-value">${fmtPct(today.real_llm_pct)}</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-label">Quant Fallback</div>
        <div class="stat-box-value orange">${today.quant_fallback ?? 0}</div>
      </div>
    `;
  }

  // Progress bars
  const pb = document.getElementById('llm-progress-bars');
  if (pb) {
    const realPct = today.real_llm_pct ?? 0;
    const wrPct = cp.win_rate_pct ?? 0;
    const tcCurrent = cp.total_resolved ?? 0;
    const tcTarget = 50;
    const tcPct = clamp((tcCurrent / tcTarget) * 100, 0, 100);

    pb.innerHTML = `
      <div class="card">
        <div class="card-title">Real LLM vs Quant Fallback</div>
        <div class="progress-wrap">
          <div class="progress-label">
            <span>Real LLM calls</span>
            <span class="prog-val">${today.real_llm ?? 0} / ${today.total ?? 0}</span>
          </div>
          ${progressBar(realPct, realPct < 50 ? 'orange' : '', true)}
        </div>
        <div class="mt-1 text-muted" style="font-size:0.75rem">${fmtPct(realPct)} real LLM — ${fmtPct(100 - realPct)} quant fallback</div>
      </div>
      <div class="card mt-2">
        <div class="card-title">Clean Period Win Rate</div>
        <div class="progress-wrap">
          <div class="progress-label">
            <span>Win rate (target: 55%)</span>
            <span class="prog-val">${fmtPct(wrPct)}</span>
          </div>
          ${progressBar(clamp((wrPct / 55) * 100, 0, 100), wrPct >= 55 ? '' : (wrPct > 40 ? 'yellow' : 'red'), true)}
        </div>
      </div>
      <div class="card mt-2">
        <div class="card-title">Clean Trade Count</div>
        <div class="progress-wrap">
          <div class="progress-label">
            <span>Trades resolved (target: 50)</span>
            <span class="prog-val">${tcCurrent} / ${tcTarget}</span>
          </div>
          ${progressBar(tcPct, tcCurrent >= tcTarget ? '' : 'blue', true)}
        </div>
        <div class="mt-1 text-muted" style="font-size:0.75rem">Since ${esc(cp.start_date ?? '—')}</div>
      </div>
    `;
  }

  // Recent decisions table
  const tbody = document.getElementById('llm-decisions-tbody');
  if (tbody) {
    if (recent.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:1.5rem">No recent decisions</td></tr>';
    } else {
      tbody.innerHTML = recent.map(r => {
        const dec = (r.decision || '').toLowerCase();
        const approved = dec.includes('approv');
        const outcomeStr = r.outcome || '';
        const outcomeEl = outcomeStr === 'win'
          ? badge('win', 'green')
          : outcomeStr === 'loss'
          ? badge('loss', 'red')
          : outcomeStr
          ? badge(esc(outcomeStr), 'grey')
          : '<span class="text-muted">—</span>';

        return `<tr>
          <td class="td-mono td-muted">${fmtDateTime(r.timestamp)}</td>
          <td class="td-cap">${esc(r.bot)}</td>
          <td class="td-mono">${esc(r.ticker)}</td>
          <td>${approved ? badge('approved','green') : badge('rejected','red')}</td>
          <td>${r.confidence != null ? fmtPct(parseFloat(r.confidence) * 100) : '—'}</td>
          <td>${outcomeEl}</td>
          <td class="text-muted" style="max-width:200px;font-size:0.75rem">${esc((r.rationale||'').slice(0,100))}${(r.rationale||'').length > 100 ? '…' : ''}</td>
        </tr>`;
      }).join('');
    }
  }
}

// ── Trades ─────────────────────────────────────────────────
function renderTrades() {
  const trades = State.data.trades;
  const filter = State.tradeFilter;
  const filtered = Array.isArray(trades)
    ? (filter === 'all' ? trades : trades.filter(t => t.bot === filter))
    : [];

  const tbody = document.getElementById('trades-tbody');
  if (!tbody) return;

  if (filtered.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:1.5rem">No trades</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.map(t => {
    const outcome = (t.outcome || '').toLowerCase();
    const rowCls = outcome === 'win' ? 'row-win' : outcome === 'loss' ? 'row-loss' : 'row-pending';
    const pnl = t.pnl_cents;
    const pnlEl = pnl != null
      ? `<span class="${pnl >= 0 ? 'td-pos' : 'td-neg'}">${pnl >= 0 ? '+' : ''}${fmt$(pnl)}</span>`
      : '<span class="text-muted">—</span>';
    const outcomeEl = outcome === 'win'
      ? badge('win','green')
      : outcome === 'loss'
      ? badge('loss','red')
      : outcome
      ? badge(esc(outcome), 'grey')
      : badge('pending','grey');

    return `<tr class="${rowCls}">
      <td class="td-mono td-muted" style="font-size:0.75rem">${fmtDateTime(t.timestamp)}</td>
      <td class="td-cap">${badge(esc(t.bot), 'blue')}</td>
      <td class="td-mono fw-bold">${esc(t.ticker)}</td>
      <td class="td-cap td-muted">${esc(t.side)}</td>
      <td>${t.confidence != null ? fmtPct(parseFloat(t.confidence) * 100) : '—'}</td>
      <td>${outcomeEl}</td>
      <td>${pnlEl}</td>
    </tr>`;
  }).join('');
}

// ── Risk ───────────────────────────────────────────────────
function renderRisk() {
  const r = State.data.risk;
  const bots = ['sentinel', 'oracle', 'pulse', 'vanguard'];

  // Drawdown meters
  const ddEl = document.getElementById('risk-drawdown');
  if (ddEl && r?.bots) {
    ddEl.innerHTML = bots.map(bot => {
      const b = r.bots[bot] ?? {};
      const dd = b.drawdown_pct ?? 0;
      const ddCls = dd > 15 ? 'red' : dd > 8 ? 'orange' : dd > 0 ? 'yellow' : '';
      const pnl = b.daily_pnl_cents ?? 0;
      const pnlPos = pnl >= 0;
      return `<div class="card">
        <div class="bot-card-header mb-1">
          <div class="fw-bold td-cap">${bot}</div>
          ${b.paused ? badge('paused','orange') : b.can_trade !== false ? badge('trading','green') : badge('blocked','red')}
        </div>
        <div class="dd-label">Drawdown</div>
        <div class="dd-value">${fmtPct(dd)}</div>
        <div class="progress-wrap mt-1">
          ${progressBar(clamp(dd * 5, 0, 100), ddCls)}
        </div>
        <div class="bot-card-body mt-2">
          <div>
            <div class="bot-stat-label">Balance</div>
            <div class="bot-stat-value">${fmt$(b.balance_cents)}</div>
          </div>
          <div>
            <div class="bot-stat-label">Daily PnL</div>
            <div class="bot-stat-value ${pnlPos ? 'positive' : 'negative'}">${pnlPos ? '+' : ''}${fmt$(pnl)}</div>
          </div>
          <div>
            <div class="bot-stat-label">Peak Balance</div>
            <div class="bot-stat-value">${fmt$(b.peak_balance_cents)}</div>
          </div>
          <div>
            <div class="bot-stat-label">Open Positions</div>
            <div class="bot-stat-value">${b.open_positions ?? 0}</div>
          </div>
        </div>
      </div>`;
    }).join('');
  }

  // Guardrail progress
  const g = r?.guardrail_progress ?? {};
  const wrCur = g.win_rate_current ?? 0;
  const wrTgt = g.win_rate_target ?? 55;
  const tcCur = g.trade_count_current ?? 0;
  const tcTgt = g.trade_count_target ?? 50;
  const dpCur = g.days_positive_pnl ?? 0;
  const dpTgt = g.days_positive_target ?? 14;
  const ready = g.ready_to_loosen ?? false;

  const guardEl = document.getElementById('risk-guardrail');
  if (guardEl) {
    guardEl.innerHTML = `
      <div class="progress-wrap">
        <div class="progress-label">
          <span>Win Rate</span>
          <span class="prog-val">${fmtPct(wrCur)} / ${wrTgt}%</span>
        </div>
        ${progressBar(clamp((wrCur / wrTgt) * 100, 0, 100), wrCur >= wrTgt ? '' : 'yellow', true)}
      </div>
      <div class="progress-wrap mt-2">
        <div class="progress-label">
          <span>Clean Trades</span>
          <span class="prog-val">${tcCur} / ${tcTgt}</span>
        </div>
        ${progressBar(clamp((tcCur / tcTgt) * 100, 0, 100), tcCur >= tcTgt ? '' : 'blue', true)}
      </div>
      <div class="progress-wrap mt-2">
        <div class="progress-label">
          <span>Positive PnL Days</span>
          <span class="prog-val">${dpCur} / ${dpTgt}</span>
        </div>
        ${progressBar(clamp((dpCur / dpTgt) * 100, 0, 100), dpCur >= dpTgt ? '' : 'orange', true)}
      </div>
      <div class="status-banner ${ready ? 'ready' : 'not-ready'}">
        ${ready ? '✓ READY TO LOOSEN GUARDRAILS' : '✗ NOT READY — KEEP GUARDRAILS'}
      </div>
    `;
  }
}

// ── System ─────────────────────────────────────────────────
function renderSystem() {
  const sys = State.data.system;
  if (!sys) return;

  const tavily = sys.tavily ?? {};
  const tavilyPct = tavily.pct ?? 0;
  const tavilyEl = document.getElementById('sys-tavily');
  if (tavilyEl) {
    tavilyEl.innerHTML = `
      <div class="progress-label">
        <span>Tavily calls today</span>
        <span class="prog-val">${tavily.used_today ?? 0} / ${tavily.budget ?? 30}</span>
      </div>
      ${progressBar(tavilyPct, tavilyPct > 80 ? 'orange' : tavilyPct > 95 ? 'red' : '', true)}
      <div class="mt-1 text-muted" style="font-size:0.75rem">${tavilyPct.toFixed(1)}% of daily budget used</div>
    `;
  }

  // Anthropic status
  const anthropicEl = document.getElementById('sys-anthropic');
  if (anthropicEl) {
    const ok = sys.anthropic_status === 'ok';
    anthropicEl.innerHTML = `Anthropic API: ${ok ? badge('OK','green') : badge('ERROR','red')}`;
  }

  // Uptime
  const uptimeEl = document.getElementById('sys-uptime');
  if (uptimeEl) {
    uptimeEl.textContent = fmtUptime(sys.uptime_seconds ?? 0);
  }

  // Log tail
  const logEl = document.getElementById('sys-log-tail');
  if (logEl) {
    const lines = sys.log_tail ?? [];
    logEl.textContent = lines.length > 0 ? lines.join('\n') : '(no log data)';
    logEl.scrollTop = logEl.scrollHeight;
  }

  // Health report
  const healthEl = document.getElementById('sys-health');
  if (healthEl) {
    const hr = sys.health_report ?? {};
    if (!hr || Object.keys(hr).length === 0) {
      healthEl.innerHTML = '<p class="text-muted">No health report available.</p>';
    } else {
      const checks = hr.checks ?? hr;
      if (typeof checks === 'object' && !Array.isArray(checks)) {
        const entries = Object.entries(checks);
        healthEl.innerHTML = `<ul class="health-list">` + entries.map(([k,v]) => {
          const ok = v === true || v === 'ok' || v === 'pass' || (typeof v === 'object' && v?.status === 'ok');
          return `<li>${ok ? badge('OK','green') : badge('FAIL','red')} <span>${esc(k)}</span> ${typeof v === 'object' ? `<span class="text-muted">${esc(JSON.stringify(v))}</span>` : `<span class="text-muted">${esc(String(v))}</span>`}</li>`;
        }).join('') + `</ul>`;
      } else {
        healthEl.innerHTML = `<pre class="code-block" style="max-height:200px">${esc(JSON.stringify(hr, null, 2))}</pre>`;
      }
    }
  }
}

// ── Controls ───────────────────────────────────────────────
function renderControls() {
  const s = State.data.status;
  const bots = ['sentinel', 'oracle', 'pulse', 'vanguard'];

  const container = document.getElementById('controls-bot-list');
  if (container) {
    container.innerHTML = bots.map(bot => {
      const b = s?.bots?.[bot] ?? {};
      const paused = b.paused;
      const pnl = b.daily_pnl_cents ?? 0;
      const pnlPos = pnl >= 0;

      return `<div class="control-card" id="ctrl-card-${bot}">
        <div class="control-card-info">
          <div class="fw-bold td-cap" style="font-size:1rem">${bot}</div>
          <div class="text-muted" style="font-size:0.78rem">
            Balance: ${fmt$(b.balance_cents)} &nbsp;|&nbsp;
            PnL: <span class="${pnlPos ? 'text-green' : 'text-red'}">${pnlPos?'+':''}${fmt$(pnl)}</span> &nbsp;|&nbsp;
            Trades: ${b.daily_trades ?? 0}/${b.max_trades ?? 8}
          </div>
        </div>
        <div class="control-card-btns">
          ${paused
            ? `<button class="btn btn-green" onclick="controlBot('${bot}','resume')">Resume</button>`
            : `<button class="btn btn-ghost" onclick="controlBot('${bot}','pause')">Pause</button>`
          }
        </div>
      </div>`;
    }).join('');
  }
}

async function controlBot(bot, action) {
  const confirmMsg = `${action.toUpperCase()} ${bot}?`;
  if (!confirm(confirmMsg)) return;
  const result = await apiFetch(`/api/control/${action}/${bot}`, { method: 'POST' });
  if (result?.ok) {
    showToast(`${bot} ${action}d successfully`);
    await loadAll();
  } else {
    showToast(`Failed to ${action} ${bot}: ${result?.error ?? 'unknown error'}`, true);
  }
}

// ── Config ─────────────────────────────────────────────────
function renderConfig() {
  const cfg = State.data.config;
  const raw = cfg?.raw ?? '';

  const viewer = document.getElementById('config-viewer');
  const editor = document.getElementById('config-editor');
  const editBtn = document.getElementById('config-edit-btn');
  const saveBtn = document.getElementById('config-save-btn');
  const cancelBtn = document.getElementById('config-cancel-btn');

  if (!viewer || !editor) return;

  if (!State.configEditMode) {
    viewer.textContent = raw || '(config not available)';
    viewer.style.display = 'block';
    editor.style.display = 'none';
    if (editBtn) editBtn.style.display = 'inline-flex';
    if (saveBtn) saveBtn.style.display = 'none';
    if (cancelBtn) cancelBtn.style.display = 'none';
  } else {
    editor.value = raw;
    viewer.style.display = 'none';
    editor.style.display = 'block';
    if (editBtn) editBtn.style.display = 'none';
    if (saveBtn) saveBtn.style.display = 'inline-flex';
    if (cancelBtn) cancelBtn.style.display = 'inline-flex';
  }
}

async function saveConfig() {
  const editor = document.getElementById('config-editor');
  if (!editor) return;
  const yaml = editor.value;
  const result = await apiFetch('/api/config/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ yaml }),
  });
  if (result?.ok) {
    showToast('Config saved. Backup: ' + (result.backup ?? ''));
    State.configEditMode = false;
    await loadAll();
  } else {
    showToast('Save failed: ' + (result?.error ?? 'unknown'), true);
  }
}

// ── Admin ──────────────────────────────────────────────────
function renderAdmin() {
  // Admin tab is mostly interactive; just clear the log display if empty
  const logDisplay = document.getElementById('admin-log-display');
  if (logDisplay && !logDisplay.dataset.loaded) {
    logDisplay.textContent = 'Click "Fetch Logs" to load.';
  }
}

async function adminFetchLogs() {
  const countEl = document.getElementById('admin-log-count');
  const displayEl = document.getElementById('admin-log-display');
  if (!displayEl) return;

  const n = parseInt(countEl?.value ?? '50', 10) || 50;
  displayEl.textContent = 'Loading…';

  const result = await apiFetch('/api/admin/logs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lines: n }),
  });

  if (result?.ok) {
    displayEl.textContent = (result.lines ?? []).join('\n');
    displayEl.dataset.loaded = '1';
    displayEl.scrollTop = displayEl.scrollHeight;
  } else {
    displayEl.textContent = 'Error: ' + (result?.error ?? 'unknown');
    showToast('Failed to fetch logs', true);
  }
}

async function adminVacuum() {
  if (!confirm('Run VACUUM on all databases? This may take a moment.')) return;
  const result = await apiFetch('/api/admin/vacuum', { method: 'POST' });
  if (result?.ok) {
    const dbs = Object.entries(result.vacuumed ?? {}).map(([k,v]) => `${k}: ${v}`).join('\n');
    showToast('Vacuum complete. ' + Object.keys(result.vacuumed ?? {}).length + ' databases processed.');
    const display = document.getElementById('admin-vacuum-result');
    if (display) { display.textContent = dbs || '(no databases found)'; display.style.display = 'block'; }
  } else {
    showToast('Vacuum failed', true);
  }
}

// ── Kill switch ────────────────────────────────────────────
async function killSwarm() {
  const inp = document.getElementById('kill-confirm-input');
  if (!inp) return;
  if (inp.value.trim().toUpperCase() !== 'KILL') {
    showToast('Type KILL in the box to confirm', true);
    return;
  }
  if (!confirm('FINAL CONFIRMATION: Send kill signal to stop the entire swarm?')) {
    inp.value = '';
    return;
  }

  const result = await apiFetch('/api/kill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: 'KILL' }),
  });

  if (result?.ok) {
    showToast('Kill signal sent. Swarm should stop shortly.');
    inp.value = '';
  } else {
    showToast('Kill failed: ' + (result?.error ?? 'unknown'), true);
  }
}

// ── Init ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Tab buttons
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Trade filters
  document.querySelectorAll('.filter-btn[data-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      State.tradeFilter = btn.dataset.filter;
      document.querySelectorAll('.filter-btn[data-filter]').forEach(b => {
        b.classList.toggle('active', b.dataset.filter === State.tradeFilter);
      });
      renderTrades();
    });
  });

  // Config edit/save/cancel
  const editBtn = document.getElementById('config-edit-btn');
  const saveBtn = document.getElementById('config-save-btn');
  const cancelBtn = document.getElementById('config-cancel-btn');

  if (editBtn) editBtn.addEventListener('click', () => {
    State.configEditMode = true;
    renderConfig();
  });
  if (saveBtn) saveBtn.addEventListener('click', saveConfig);
  if (cancelBtn) cancelBtn.addEventListener('click', () => {
    State.configEditMode = false;
    renderConfig();
  });

  // Admin buttons
  const fetchLogsBtn = document.getElementById('admin-fetch-logs-btn');
  if (fetchLogsBtn) fetchLogsBtn.addEventListener('click', adminFetchLogs);

  const vacuumBtn = document.getElementById('admin-vacuum-btn');
  if (vacuumBtn) vacuumBtn.addEventListener('click', adminVacuum);

  // Kill switch
  const killBtn = document.getElementById('kill-btn');
  if (killBtn) killBtn.addEventListener('click', killSwarm);

  // Initial load
  switchTab('overview');
  loadAll();

  // Auto-refresh every 15s
  setInterval(loadAll, 15000);
});
