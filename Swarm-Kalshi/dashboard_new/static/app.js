/* ============================================================
   Kalshi Swarm Dashboard — app.js
   Vanilla JS, no framework dependencies.
   Auto-refresh every 15 seconds.
   ============================================================ */

'use strict';

// ── State ──────────────────────────────────────────────────
const State = {
  activeTab: 'console',
  tradeFilter: 'all',
  configEditMode: false,
  equityChart: null,
  calibrationChart: null,
  trajectoryChart: null,
  lastRefresh: null,
  cmdHistory: [],
  cmdHistoryIndex: -1,
  data: {
    swarm: null,
    status: null,
    learning: null,
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

function fmtConf(val, decimals = 1) {
  if (val == null) return '—';
  const n = parseFloat(val);
  if (isNaN(n)) return '—';
  const pct = (n <= 1.0 && n > 0.0) ? n * 100 : n;
  return pct.toFixed(decimals) + '%';
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
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => null);
      if (errBody && errBody.error) throw new Error(errBody.error);
      throw new Error(`HTTP ${resp.status}`);
    }
    return await resp.json();
  } catch (err) {
    console.warn(`[fetch] ${url}:`, err);
    return { error: err.message };
  }
}

// ── Data loading ───────────────────────────────────────────
async function loadAll() {
  const [swarm, status, learning, llm, trades, risk, system, equity, config] = await Promise.allSettled([
    apiFetch('/api/swarm/status'),
    apiFetch('/api/status'),
    apiFetch('/api/learning'),
    apiFetch('/api/llm'),
    apiFetch('/api/trades'),
    apiFetch('/api/risk'),
    apiFetch('/api/system'),
    apiFetch('/api/equity'),
    apiFetch('/api/config'),
  ]);

  State.data.swarm    = swarm.status    === 'fulfilled' ? swarm.value    : null;
  State.data.status   = status.status   === 'fulfilled' ? status.value   : null;
  State.data.learning = learning.status === 'fulfilled' ? learning.value : null;
  State.data.llm      = llm.status      === 'fulfilled' ? llm.value      : null;
  State.data.trades   = trades.status   === 'fulfilled' ? trades.value   : null;
  State.data.risk     = risk.status     === 'fulfilled' ? risk.value     : null;
  State.data.system   = system.status   === 'fulfilled' ? system.value   : null;
  State.data.equity   = equity.status   === 'fulfilled' ? equity.value   : null;
  State.data.config   = config.status   === 'fulfilled' ? config.value   : null;

  State.lastRefresh = new Date();
  updateRefreshBadge();
  updateSwarmControlBar();
  renderActiveTab();
}

function updateRefreshBadge() {
  const el = document.getElementById('refresh-time');
  if (el && State.lastRefresh) {
    el.textContent = 'Updated ' + State.lastRefresh.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
}

// ── Swarm Global Control Bar ───────────────────────────────
function updateSwarmControlBar() {
  const sw = State.data.swarm;
  if (!sw) return;

  const isRunning = Boolean(sw.running);
  const isDemo = sw.mode === 'demo';

  // Swarm Status Pill
  const statusPill = document.getElementById('cmd-swarm-status-pill');
  const statusText = document.getElementById('cmd-swarm-status-text');
  if (statusPill && statusText) {
    statusPill.className = 'status-pill ' + (isRunning ? 'status-running' : 'status-stopped');
    statusText.textContent = isRunning ? `SWARM RUNNING (${sw.process_count} proc)` : 'SWARM STOPPED';
  }

  // Trading Mode Pill
  const modePill = document.getElementById('cmd-mode-pill');
  const modeText = document.getElementById('cmd-mode-text');
  if (modePill && modeText) {
    modePill.className = 'status-pill ' + (isDemo ? 'status-demo' : 'status-live');
    modeText.textContent = isDemo ? '🟡 DEMO MODE (SIM)' : '🔴 LIVE CAPITAL';
  }

  // Start / Stop button states
  const startBtn = document.getElementById('cmd-btn-start');
  const stopBtn = document.getElementById('cmd-btn-stop');
  if (startBtn) startBtn.disabled = isRunning;
  if (stopBtn) stopBtn.disabled = !isRunning;
}

// ── Tab routing ────────────────────────────────────────────
function renderActiveTab() {
  switch (State.activeTab) {
    case 'console':  renderConsole();  break;
    case 'overview': renderOverview(); break;
    case 'learning': renderLearning(); break;
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

// ── Command Console Tab ────────────────────────────────────
function renderConsole() {
  const sw = State.data.swarm;
  if (!sw) return;

  const isRunning = Boolean(sw.running);
  const isDemo = sw.mode === 'demo';

  // State card
  const stateBadge = document.getElementById('console-state-badge');
  const procCount = document.getElementById('console-proc-count');
  const pidsList = document.getElementById('console-pids-list');

  if (stateBadge) {
    stateBadge.className = 'badge ' + (isRunning ? 'badge-success' : 'badge-error');
    stateBadge.textContent = isRunning ? 'RUNNING' : 'STOPPED';
  }
  if (procCount) {
    procCount.textContent = `${sw.process_count || 0} active process(es)`;
  }
  if (pidsList) {
    if (isRunning && sw.processes && sw.processes.length > 0) {
      pidsList.innerHTML = sw.processes.map(p => `• PID <code>${p.pid}</code>`).join(' ');
    } else {
      pidsList.textContent = 'No active processes';
    }
  }

  // Mode card
  const modeBadge = document.getElementById('console-mode-badge');
  const modeDesc = document.getElementById('console-mode-desc');
  if (modeBadge) {
    modeBadge.className = 'badge ' + (isDemo ? 'badge-warning' : 'badge-error');
    modeBadge.textContent = isDemo ? 'DEMO (SIMULATION)' : 'LIVE CAPITAL';
  }
  if (modeDesc) {
    modeDesc.textContent = isDemo
      ? 'Safe simulated orders via Kalshi Demo API'
      : '⚠️ LIVE ORDERS WITH REAL CAPITAL ACTIVE';
  }
}

function appendTerminalLine(text, type = 'term-line') {
  const screen = document.getElementById('console-terminal-screen');
  if (!screen) return;

  const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const line = document.createElement('div');
  line.className = `term-line ${type}`;
  line.textContent = `[${timeStr}] ${text}`;
  screen.appendChild(line);
  screen.scrollTop = screen.scrollHeight;
}

function clearTerminal() {
  const screen = document.getElementById('console-terminal-screen');
  if (screen) {
    screen.innerHTML = '<div class="term-line term-sys">Terminal cleared.</div>';
  }
}

async function sendConsoleCommand(rawCmd) {
  const cmd = (rawCmd || '').trim();
  if (!cmd) return;

  // Add to history
  State.cmdHistory.push(cmd);
  State.cmdHistoryIndex = State.cmdHistory.length;

  appendTerminalLine(`swarm> ${cmd}`, 'term-prompt');

  const verb = cmd.toLowerCase().split(' ')[0].replace(/^\//, '');

  if (verb === 'clear') {
    clearTerminal();
    return;
  }

  try {
    let res;
    if (verb === 'start') {
      appendTerminalLine('Starting Kalshi swarm in background...', 'term-info');
      res = await apiFetch('/api/swarm/start', { method: 'POST' });
    } else if (verb === 'stop') {
      appendTerminalLine('Sending stop signal to swarm processes...', 'term-info');
      res = await apiFetch('/api/swarm/stop', { method: 'POST' });
    } else if (verb === 'restart') {
      appendTerminalLine('Restarting Kalshi swarm...', 'term-info');
      res = await apiFetch('/api/swarm/restart', { method: 'POST' });
    } else if (verb === 'mode') {
      const parts = cmd.split(' ');
      if (parts.length > 1) {
        const targetMode = parts[1].toLowerCase();
        if (targetMode === 'live') {
          if (!confirm('⚠️ WARNING: You are switching to LIVE CAPITAL MODE. Real money will be at risk. Are you sure?')) {
            appendTerminalLine('Mode switch to LIVE cancelled by operator.', 'term-warn');
            return;
          }
        }
        res = await apiFetch('/api/swarm/mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: targetMode }),
        });
      } else {
        res = await apiFetch('/api/swarm/status');
      }
    } else {
      res = await apiFetch('/api/swarm/exec', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: cmd }),
      });
    }

    if (res?.output) {
      // Multiline output
      const lines = res.output.split('\n');
      for (const l of lines) {
        if (l.trim()) appendTerminalLine(l, 'term-out');
      }
    } else if (res?.message) {
      appendTerminalLine(res.message, res.ok ? 'term-success' : 'term-error');
      showToast(res.message, !res.ok);
    } else if (res?.error) {
      appendTerminalLine(`Error: ${res.error}`, 'term-error');
      showToast(res.error, true);
    } else if (res?.mode) {
      appendTerminalLine(`Swarm status: ${res.running ? 'RUNNING' : 'STOPPED'} | Mode: ${res.mode.toUpperCase()} | Model: ${res.model}`, 'term-info');
    }

    // Refresh state
    loadAll();
  } catch (err) {
    appendTerminalLine(`Execution failure: ${err.message}`, 'term-error');
    showToast(err.message, true);
  }
}

async function toggleSwarmMode() {
  const sw = State.data.swarm;
  const currentMode = sw?.mode || 'demo';
  const targetMode = currentMode === 'demo' ? 'live' : 'demo';

  if (targetMode === 'live') {
    if (!confirm('⚠️ CRITICAL CONFIRMATION: Switch trading mode to LIVE CAPITAL? Real orders will be submitted to Kalshi.')) {
      return;
    }
  }

  sendConsoleCommand(`mode ${targetMode}`);
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
      const tradePct = clamp((trades / (maxTrades || 1)) * 100, 0, 100);

      let statusBadge;
      if (!active) {
        statusBadge = badge('inactive', 'error');
      } else if (paused) {
        statusBadge = badge('paused', 'warning');
      } else {
        statusBadge = badge('active', 'success');
      }

      return `<div class="bot-card">
        <div class="bot-card-header">
          <div class="bot-name">${bot}</div>
          ${statusBadge}
        </div>
        <div class="bot-metric-grid">
          <div class="bot-metric-item">
            <div class="bot-metric-label">Allocated Cash</div>
            <div class="bot-metric-value">${fmt$(balance)}</div>
          </div>
          <div class="bot-metric-item">
            <div class="bot-metric-label">Today P&L</div>
            <div class="bot-metric-value" style="color:${pnlPos ? 'var(--color-emerald)' : 'var(--color-rose)'}">${pnlPos ? '+' : ''}${fmt$(pnl)}</div>
          </div>
          <div class="bot-metric-item">
            <div class="bot-metric-label">Trade Execution</div>
            <div class="bot-metric-value">${trades} / ${maxTrades}</div>
          </div>
          <div class="bot-metric-item">
            <div class="bot-metric-label">Gatekeeper</div>
            <div class="bot-metric-value">${b.can_trade !== false ? '<span style="color:var(--color-emerald)">Approved</span>' : '<span style="color:var(--color-rose)">Locked</span>'}</div>
          </div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--text-muted);margin-bottom:0.25rem;">
            <span>Daily Quota Utilization</span>
            <span style="font-family:var(--font-mono)">${tradePct.toFixed(0)}%</span>
          </div>
          ${progressBar(tradePct, tradePct > 80 ? 'yellow' : '')}
        </div>
      </div>`;
    }).join('');
  }

  // Equity chart
  renderEquityChart();

  // Stats row
  const llm = State.data.llm;
  const sys = State.data.system;
  const cleanWr = llm?.clean_period?.win_rate_pct ?? 0;
  const llmApproval = llm?.today?.approval_rate_pct ?? 0;
  const tavily = sys?.tavily;
  const uptime = s?.uptime_seconds ?? sys?.uptime_seconds ?? 0;

  const statsEl = document.getElementById('ov-stats-row');
  if (statsEl) {
    statsEl.innerHTML = `
      <div class="card" style="padding:1rem;">
        <div style="font-size:0.72rem;text-transform:uppercase;color:var(--text-muted);font-weight:700;letter-spacing:0.05em;">Win Rate (Clean)</div>
        <div style="font-family:var(--font-mono);font-size:1.6rem;font-weight:800;color:${cleanWr >= 55 ? 'var(--color-emerald)' : (cleanWr > 0 ? 'var(--color-amber)' : '#ffffff')};margin-top:0.2rem;">${fmtPct(cleanWr)}</div>
        <div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.2rem;">Baseline Gate: 55.0%</div>
      </div>
      <div class="card" style="padding:1rem;">
        <div style="font-size:0.72rem;text-transform:uppercase;color:var(--text-muted);font-weight:700;letter-spacing:0.05em;">Gemini Approval Alpha</div>
        <div style="font-family:var(--font-mono);font-size:1.6rem;font-weight:800;color:var(--color-cyan);margin-top:0.2rem;">${fmtPct(llmApproval)}</div>
        <div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.2rem;">Search Grounded</div>
      </div>
      <div class="card" style="padding:1rem;">
        <div style="font-size:0.72rem;text-transform:uppercase;color:var(--text-muted);font-weight:700;letter-spacing:0.05em;">Tavily Macro Search</div>
        <div style="font-family:var(--font-mono);font-size:1.6rem;font-weight:800;color:${tavily?.used_today > 24 ? 'var(--color-amber)' : '#ffffff'};margin-top:0.2rem;">${tavily?.used_today ?? 0} <span style="font-size:1rem;color:var(--text-muted);font-weight:400;">/ ${tavily?.budget ?? 30}</span></div>
        <div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.2rem;">Credits used today</div>
      </div>
      <div class="card" style="padding:1rem;">
        <div style="font-size:0.72rem;text-transform:uppercase;color:var(--text-muted);font-weight:700;letter-spacing:0.05em;">System Uptime</div>
        <div style="font-family:var(--font-mono);font-size:1.6rem;font-weight:800;color:#ffffff;margin-top:0.2rem;">${fmtUptime(uptime)}</div>
        <div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.2rem;">Continuous session</div>
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

// ── Learning & Adaptation Radar ─────────────────────────────
function renderLearning() {
  const data = State.data.learning;
  if (!data) return;

  const sc = data.scorecard ?? {};
  const cal = data.calibration ?? {};
  const cats = data.categories ?? {};
  const weights = data.weights ?? {};
  const llm = data.llm_intelligence ?? {};
  const rolling = data.rolling_win_rates ?? [];

  // 1. Hero scorecard
  const badgeEl = document.getElementById('learn-verdict-badge');
  const stageEl = document.getElementById('learn-stage-title');
  const msgEl = document.getElementById('learn-status-msg');
  const eceEl = document.getElementById('learn-ece-val');
  const brierEl = document.getElementById('learn-brier-val');
  const recalibEl = document.getElementById('learn-recalib-count');

  if (badgeEl) {
    const isL = sc.is_learning || 'CALIBRATING';
    badgeEl.textContent = isL;
    badgeEl.className = 'badge ' + (isL === 'YES' ? 'badge-success' : (isL === 'CALIBRATING' ? 'badge-info' : 'badge-warning'));
  }
  if (stageEl) stageEl.textContent = sc.stage || 'Data Collection & Calibration';
  if (msgEl) msgEl.textContent = sc.status_message || 'Analyzing confidence calibration and strategy evolution.';
  if (eceEl) {
    const ece = cal.expected_calibration_error ?? 0;
    eceEl.textContent = fmtPct(ece);
    eceEl.style.color = ece < 15 ? '#34d399' : (ece < 35 ? '#fbbf24' : '#f43f5e');
  }
  if (brierEl) {
    const brier = cal.brier_score ?? 0;
    brierEl.textContent = brier.toFixed(4);
    brierEl.style.color = brier < 0.20 ? '#34d399' : (brier < 0.35 ? '#38bdf8' : '#fbbf24');
  }
  if (recalibEl) {
    recalibEl.textContent = weights.total_recalibrations ?? 0;
  }

  // 2. Render Charts
  renderCalibrationChart(cal);
  renderTrajectoryChart(rolling);

  // 3. Confidence Buckets Table
  const bucketsTbody = document.getElementById('learn-buckets-tbody');
  if (bucketsTbody) {
    const buckets = cal.buckets ?? [];
    if (buckets.length === 0) {
      bucketsTbody.innerHTML = '<tr><td colspan="5" class="text-muted" style="text-align:center;padding:1rem;">No calibration data yet.</td></tr>';
    } else {
      bucketsTbody.innerHTML = buckets.map(b => {
        const err = b.calibration_error ?? 0;
        const errCls = Math.abs(err) <= 10 ? 'green' : (Math.abs(err) <= 25 ? 'orange' : 'red');
        return `
          <tr>
            <td><strong>${esc(b.label)}</strong></td>
            <td>${b.trades}</td>
            <td>${b.wins}</td>
            <td>${fmtPct(b.observed_win_rate)}</td>
            <td class="${errCls}">${err > 0 ? '+' : ''}${fmtPct(err)}</td>
          </tr>
        `;
      }).join('');
    }
  }

  // 4. Categories Specialization Table
  const catsTbody = document.getElementById('learn-categories-tbody');
  if (catsTbody) {
    const categoryList = cats.categories ?? [];
    if (categoryList.length === 0) {
      catsTbody.innerHTML = '<tr><td colspan="6" class="text-muted" style="text-align:center;padding:1rem;">No category data yet.</td></tr>';
    } else {
      catsTbody.innerHTML = categoryList.slice(0, 10).map(c => {
        const st = c.status;
        const stBadge = st === 'hot'
          ? '<span class="badge badge-success">🔥 Hot</span>'
          : (st === 'cold' ? '<span class="badge badge-danger">❄️ Cold</span>' : '<span class="badge badge-secondary">⚖️ Neutral</span>');
        const pnlStr = fmt$(c.pnl_cents);
        const pnlCls = c.pnl_cents >= 0 ? 'green' : 'red';
        return `
          <tr>
            <td><strong>${esc(c.category)}</strong></td>
            <td>${c.trades}</td>
            <td>${c.wins}</td>
            <td>${fmtPct(c.win_rate_pct)}</td>
            <td><strong>${c.multiplier.toFixed(2)}x</strong></td>
            <td>${stBadge}</td>
          </tr>
        `;
      }).join('');
    }
  }

  // 5. Dynamic Feature Weights & LLM Alpha Grid
  const weightsGrid = document.getElementById('learn-weights-llm-grid');
  if (weightsGrid) {
    const latestBots = weights.latest_weights_by_bot ?? {};
    const botCards = Object.entries(latestBots).map(([bot, wb]) => `
      <div style="background:rgba(255,255,255,0.03); padding:0.85rem; border-radius:6px; border:1px solid rgba(255,255,255,0.06);">
        <div style="font-weight:600; font-size:0.85rem; text-transform:uppercase; color:#94a3b8; margin-bottom:0.5rem;">${esc(bot)} Weights (${wb.recalibrations} updates)</div>
        <div style="font-size:0.8rem; display:flex; flex-direction:column; gap:0.25rem;">
          <div style="display:flex; justify-content:space-between;"><span>Edge</span><strong>${(wb.edge*100).toFixed(1)}%</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Liquidity</span><strong>${(wb.liquidity*100).toFixed(1)}%</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Volume</span><strong>${(wb.volume*100).toFixed(1)}%</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Timing</span><strong>${(wb.timing*100).toFixed(1)}%</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Momentum</span><strong>${(wb.momentum*100).toFixed(1)}%</strong></div>
        </div>
      </div>
    `).join('');

    const defaultWeightsCard = Object.keys(latestBots).length === 0 ? `
      <div style="background:rgba(255,255,255,0.03); padding:0.85rem; border-radius:6px; border:1px solid rgba(255,255,255,0.06);">
        <div style="font-weight:600; font-size:0.85rem; text-transform:uppercase; color:#94a3b8; margin-bottom:0.5rem;">Baseline Feature Weights</div>
        <div style="font-size:0.8rem; display:flex; flex-direction:column; gap:0.25rem;">
          <div style="display:flex; justify-content:space-between;"><span>Edge</span><strong>20.0%</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Liquidity</span><strong>20.0%</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Volume</span><strong>20.0%</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Timing</span><strong>20.0%</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Momentum</span><strong>20.0%</strong></div>
        </div>
        <div style="font-size:0.75rem; color:#64748b; margin-top:0.5rem;">Recalibrates automatically every 25 settled trades.</div>
      </div>
    ` : '';

    const topFlags = (llm.top_red_flags ?? []).slice(0, 4).map(rf => `
      <div style="display:flex; justify-content:space-between; font-size:0.8rem; padding:0.15rem 0;">
        <span style="color:#f43f5e;">• ${esc(rf.flag)}</span>
        <strong style="color:#94a3b8;">${rf.count}x</strong>
      </div>
    `).join('') || '<div style="color:#64748b; font-size:0.8rem;">No rejections flagged yet.</div>';

    weightsGrid.innerHTML = `
      ${botCards || defaultWeightsCard}
      <div style="background:rgba(255,255,255,0.03); padding:0.85rem; border-radius:6px; border:1px solid rgba(255,255,255,0.06);">
        <div style="font-weight:600; font-size:0.85rem; text-transform:uppercase; color:#94a3b8; margin-bottom:0.5rem;">Central LLM Filtering Impact</div>
        <div style="font-size:0.8rem; display:flex; flex-direction:column; gap:0.35rem; margin-bottom:0.75rem;">
          <div style="display:flex; justify-content:space-between;"><span>Total Decisions</span><strong>${llm.total_decisions ?? 0}</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Approved Rate</span><strong style="color:#34d399;">${fmtPct(llm.approval_rate_pct)}</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Filtered Out</span><strong style="color:#f43f5e;">${llm.rejected ?? 0} trades</strong></div>
        </div>
        <div style="font-size:0.75rem; font-weight:600; color:#818cf8; margin-bottom:0.25rem;">Top Rejection Red Flags:</div>
        ${topFlags}
      </div>
    `;
  }
}

function renderCalibrationChart(cal) {
  const canvas = document.getElementById('calibration-chart');
  if (!canvas || typeof Chart === 'undefined') return;

  const buckets = cal.buckets ?? [];
  const labels = buckets.map(b => b.label);
  const observed = buckets.map(b => b.observed_win_rate ?? 0);
  const ideal = buckets.map(b => b.midpoint ?? 50);

  if (State.calibrationChart) {
    State.calibrationChart.data.labels = labels;
    State.calibrationChart.data.datasets[0].data = observed;
    State.calibrationChart.data.datasets[1].data = ideal;
    State.calibrationChart.update('none');
    return;
  }

  State.calibrationChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Observed Win Rate (%)',
          data: observed,
          backgroundColor: 'rgba(99, 102, 241, 0.75)',
          borderColor: '#818cf8',
          borderWidth: 1,
          borderRadius: 4,
        },
        {
          type: 'line',
          label: 'Ideal Calibration (45°)',
          data: ideal,
          borderColor: '#94a3b8',
          borderDash: [5, 5],
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: '#94a3b8',
          fill: false,
        }
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { labels: { color: '#94a3b8', font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${parseFloat(ctx.raw).toFixed(1)}%`,
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#8b949e', font: { size: 10 } },
          grid: { color: 'rgba(48,54,61,0.3)' },
        },
        y: {
          min: 0,
          max: 100,
          ticks: {
            color: '#8b949e',
            font: { size: 10 },
            callback: v => v + '%',
          },
          grid: { color: 'rgba(48,54,61,0.3)' },
        },
      },
    }
  });
}

function renderTrajectoryChart(rolling) {
  const canvas = document.getElementById('learning-trajectory-chart');
  if (!canvas || typeof Chart === 'undefined') return;

  const points = Array.isArray(rolling) ? rolling : [];
  const labels = points.map((p, idx) => `T#${p.trade_index || (idx+1)}`);
  const values = points.map(p => p.rolling_win_rate ?? 0);
  const targetLine = points.map(() => 55);

  if (State.trajectoryChart) {
    State.trajectoryChart.data.labels = labels;
    State.trajectoryChart.data.datasets[0].data = values;
    State.trajectoryChart.data.datasets[1].data = targetLine;
    State.trajectoryChart.update('none');
    return;
  }

  State.trajectoryChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Rolling Win Rate (%)',
          data: values,
          borderColor: '#38bdf8',
          backgroundColor: 'rgba(56, 189, 248, 0.1)',
          borderWidth: 2,
          pointRadius: points.length > 30 ? 0 : 3,
          fill: true,
          tension: 0.3,
        },
        {
          label: 'Target (55%)',
          data: targetLine,
          borderColor: '#34d399',
          borderDash: [4, 4],
          borderWidth: 1.5,
          pointRadius: 0,
          fill: false,
        }
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { labels: { color: '#94a3b8', font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${parseFloat(ctx.raw).toFixed(1)}%`,
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 10 },
          grid: { color: 'rgba(48,54,61,0.3)' },
        },
        y: {
          min: 0,
          max: 100,
          ticks: {
            color: '#8b949e',
            font: { size: 10 },
            callback: v => v + '%',
          },
          grid: { color: 'rgba(48,54,61,0.3)' },
        },
      },
    }
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
      tbody.innerHTML = recent.map((r, idx) => {
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

        return `<tr data-llm-idx="${idx}" title="Click to view full Gemini 2.5 Flash rationale">
          <td class="td-mono td-muted">${fmtDateTime(r.timestamp)}</td>
          <td class="td-cap">${badge(esc(r.bot), 'blue')}</td>
          <td class="td-mono fw-bold">${esc(r.ticker)}</td>
          <td>${approved ? badge('approved','green') : badge('rejected','red')}</td>
          <td>${r.confidence != null ? fmtConf(r.confidence) : '—'}</td>
          <td>${outcomeEl}</td>
          <td class="text-muted" style="max-width:260px;font-size:0.75rem">${esc((r.rationale||'').slice(0,110))}${(r.rationale||'').length > 110 ? '…' : ''}</td>
        </tr>`;
      }).join('');

      tbody.querySelectorAll('tr[data-llm-idx]').forEach(row => {
        row.addEventListener('click', () => {
          const idx = parseInt(row.dataset.llmIdx, 10);
          const item = recent[idx];
          if (!item) return;
          const modal = document.getElementById('llm-modal');
          if (!modal) return;
          document.getElementById('modal-ticker').textContent = item.ticker || 'Trade Decision';
          document.getElementById('modal-bot').innerHTML = badge(item.bot || 'Unknown', 'blue');
          const approved = (item.decision || '').toLowerCase().includes('approv');
          document.getElementById('modal-decision').innerHTML = approved ? badge('APPROVED', 'green') : badge('REJECTED', 'red');
          document.getElementById('modal-confidence').textContent = fmtConf(item.confidence);
          document.getElementById('modal-timestamp').textContent = fmtDateTime(item.timestamp);
          document.getElementById('modal-rationale').textContent = item.rationale || '(No detailed rationale provided)';
          modal.style.display = 'flex';
        });
      });
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
      <td>${t.confidence != null ? fmtConf(t.confidence) : '—'}</td>
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

  // Gemini AI Brain status
  const llmEl = document.getElementById('sys-llm') || document.getElementById('sys-anthropic');
  if (llmEl) {
    const ok = (sys.llm_status === 'ok' || sys.gemini_status === 'ok' || sys.anthropic_status === 'ok');
    const providerName = sys.llm_provider ? (sys.llm_provider.charAt(0).toUpperCase() + sys.llm_provider.slice(1)) : 'Gemini';
    const modelName = sys.llm_model ? ` (${sys.llm_model})` : ' (gemini-2.5-flash)';
    llmEl.innerHTML = `<strong>Gemini Brain:</strong> ${badge(providerName + modelName, 'blue')} ${ok ? badge('ONLINE (Google GenAI)','green') : badge('KEY/API ERROR','red')}`;
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
        healthEl.innerHTML = `<ul class="health-list">` + entries.map(([k, v]) => {
          let status = 'OK';
          let details = '';
          if (typeof v === 'object' && v !== null) {
            status = (v.status || 'OK').toUpperCase();
            details = v.details || (v.issues && v.issues.length ? v.issues.join('; ') : '');
          } else {
            status = String(v).toUpperCase();
            details = status;
          }

          let badgeHtml = badge('OK', 'green');
          if (status === 'WARNING' || status === 'WARN') {
            badgeHtml = badge('WARN', 'yellow');
          } else if (status === 'CRITICAL' || status === 'FAIL' || status === 'ERROR') {
            badgeHtml = badge('FAIL', 'red');
          }

          const label = k.replace(/_/g, ' ').toUpperCase();
          return `<li>${badgeHtml} <strong style="color:var(--text);font-size:0.82rem">${esc(label)}</strong> <span class="text-muted" style="font-size:0.8rem">${esc(details)}</span></li>`;
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

  // Modal close handlers
  const modal = document.getElementById('llm-modal');
  const closeBtn = document.getElementById('modal-close-btn');
  if (closeBtn && modal) {
    closeBtn.addEventListener('click', () => { modal.style.display = 'none'; });
    modal.addEventListener('click', (e) => {
      if (e.target === modal) modal.style.display = 'none';
    });
  }

  // Command Console & Global Bar Buttons
  const cmdStart = document.getElementById('cmd-btn-start');
  const cmdStop = document.getElementById('cmd-btn-stop');
  const cmdRestart = document.getElementById('cmd-btn-restart');
  const cmdMode = document.getElementById('cmd-btn-toggle-mode');
  const cmdHealth = document.getElementById('cmd-btn-health');
  const cmdRadar = document.getElementById('cmd-btn-radar');
  const modeSwitchCard = document.getElementById('console-mode-switch-btn');

  if (cmdStart) cmdStart.addEventListener('click', () => sendConsoleCommand('start'));
  if (cmdStop) cmdStop.addEventListener('click', () => sendConsoleCommand('stop'));
  if (cmdRestart) cmdRestart.addEventListener('click', () => sendConsoleCommand('restart'));
  if (cmdMode) cmdMode.addEventListener('click', toggleSwarmMode);
  if (modeSwitchCard) modeSwitchCard.addEventListener('click', toggleSwarmMode);
  if (cmdHealth) cmdHealth.addEventListener('click', () => sendConsoleCommand('health'));
  if (cmdRadar) cmdRadar.addEventListener('click', () => sendConsoleCommand('radar'));

  // Terminal input & history
  const consoleInp = document.getElementById('console-input');
  const consoleSubmit = document.getElementById('console-submit-btn');

  function handleConsoleSubmit() {
    if (!consoleInp) return;
    const val = consoleInp.value.trim();
    if (val) {
      sendConsoleCommand(val);
      consoleInp.value = '';
    }
  }

  if (consoleSubmit) consoleSubmit.addEventListener('click', handleConsoleSubmit);
  if (consoleInp) {
    consoleInp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        handleConsoleSubmit();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (State.cmdHistory.length > 0 && State.cmdHistoryIndex > 0) {
          State.cmdHistoryIndex--;
          consoleInp.value = State.cmdHistory[State.cmdHistoryIndex] || '';
        }
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (State.cmdHistoryIndex < State.cmdHistory.length - 1) {
          State.cmdHistoryIndex++;
          consoleInp.value = State.cmdHistory[State.cmdHistoryIndex] || '';
        } else {
          State.cmdHistoryIndex = State.cmdHistory.length;
          consoleInp.value = '';
        }
      }
    });
  }

  function updateUtcClock() {
    const el = document.getElementById('utc-clock');
    if (!el) return;
    const now = new Date();
    const h = String(now.getUTCHours()).padStart(2, '0');
    const m = String(now.getUTCMinutes()).padStart(2, '0');
    const s = String(now.getUTCSeconds()).padStart(2, '0');
    el.textContent = `${h}:${m}:${s} UTC`;
  }
  updateUtcClock();
  setInterval(updateUtcClock, 1000);

  // Initial load
  switchTab('console');
  loadAll();

  // Fast real-time auto-refresh every 5s
  setInterval(loadAll, 5000);
});
