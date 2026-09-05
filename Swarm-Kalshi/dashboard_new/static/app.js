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
    const data = await resp.json();
    return (data && !data.error) ? data : null;
  } catch (err) {
    console.warn(`[fetch] ${url}:`, err);
    return null;
  }
}

// ── Data loading ───────────────────────────────────────────
let _isLoading = false;

async function loadAll(forceAll = false) {
  if (_isLoading) return;
  _isLoading = true;

  try {
    // Always fetch core status
    const fetchPromises = [
      apiFetch('/api/swarm/status').then(d => { if (d) State.data.swarm = d; }),
      apiFetch('/api/status').then(d => { if (d) State.data.status = d; }),
    ];

    // Tab-targeted loading: Only fetch what the active tab actually displays.
    // This reduces SSH tunnel network contention and eliminates data flickering.
    const active = State.activeTab;

    if (forceAll || active === 'learning') {
      fetchPromises.push(
        apiFetch('/api/learning').then(d => { if (d) State.data.learning = d; }),
        apiFetch('/api/llm').then(d => { if (d) State.data.llm = d; })
      );
    }
    if (forceAll || active === 'overview' || active === 'console') {
      fetchPromises.push(
        apiFetch('/api/equity').then(d => { if (d) State.data.equity = d; }),
        apiFetch('/api/llm').then(d => { if (d) State.data.llm = d; }),
        apiFetch('/api/system').then(d => { if (d) State.data.system = d; })
      );
    }
    if (forceAll || active === 'trades') {
      fetchPromises.push(apiFetch('/api/trades').then(d => { if (d) State.data.trades = d; }));
    }
    if (forceAll || active === 'risk') {
      fetchPromises.push(apiFetch('/api/risk').then(d => { if (d) State.data.risk = d; }));
    }
    if (forceAll || active === 'system') {
      fetchPromises.push(apiFetch('/api/system').then(d => { if (d) State.data.system = d; }));
    }
    if (forceAll || active === 'config') {
      fetchPromises.push(apiFetch('/api/config').then(d => { if (d) State.data.config = d; }));
    }

    await Promise.allSettled(fetchPromises);

    State.lastRefresh = new Date();
    updateRefreshBadge();
    updateSwarmControlBar();
    renderActiveTab();
  } finally {
    _isLoading = false;
  }
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
  const auth = State.data.status?.exchange_auth || State.data.system?.exchange_auth || State.data.risk?.exchange_auth;

  // Global degradation banner
  const banner = document.getElementById('exchange-auth-banner');
  const bannerSub = document.getElementById('exchange-auth-banner-sub');
  if (banner) {
    if (auth && (auth.status === 'degraded' || auth.verified === false)) {
      banner.style.display = 'flex';
      if (bannerSub) {
        bannerSub.textContent = auth.reason
          ? `${auth.reason}. Exchange balance, positions, and live exposure are currently UNVERIFIED. Live trading is locked fail-closed.`
          : 'HTTP 401: Failed to authenticate with Kalshi API. Exchange balance, positions, and live exposure are currently UNVERIFIED. Live trading is locked fail-closed.';
      }
    } else {
      banner.style.display = 'none';
    }
  }

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
  // Fetch fresh data for newly selected tab
  loadAll(false);
}

// ── Command Console Tab ────────────────────────────────────
function renderConsole() {
  const sw = State.data.swarm;
  if (!sw || sw.error) return;

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

  // Strip ANSI escape codes if present
  const cleanText = (text == null ? '' : String(text)).replace(/\x1B\[[0-9;]*[a-zA-Z]/g, '');

  const line = document.createElement('div');
  line.className = `term-line ${type}`;
  if (type === 'term-out') {
    line.textContent = cleanText || ' ';
  } else {
    const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    line.textContent = `[${timeStr}] ${cleanText}`;
  }
  screen.appendChild(line);
  screen.scrollTop = screen.scrollHeight;
}

function clearTerminal() {
  const screen = document.getElementById('console-terminal-screen');
  if (screen) {
    screen.innerHTML = '<div class="term-line term-sys">Terminal cleared. Type "help" to view available commands.</div>';
  }
}

async function sendConsoleCommand(rawCmd) {
  const cmd = (rawCmd || '').trim();
  if (!cmd) return;

  // Add to history
  State.cmdHistory.push(cmd);
  State.cmdHistoryIndex = State.cmdHistory.length;

  appendTerminalLine(`swarm> ${cmd}`, 'term-prompt');

  try {
    const res = await apiFetch('/api/swarm/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: cmd }),
    });

    if (res?.error) {
      appendTerminalLine(`Error: ${res.error}`, 'term-err');
    } else {
      const output = res?.output || res?.message || `Executed command: ${cmd}`;
      appendTerminalLine(output, 'term-out');
    }
  } catch (err) {
    appendTerminalLine(`Error: ${err.message}`, 'term-err');
  }

  // Refresh status immediately
  await loadAll();
}

async function triggerModeSwitch() {
  const sw = State.data.swarm;
  const currentMode = sw?.mode || 'demo';
  const targetMode = currentMode === 'demo' ? 'live' : 'demo';

  if (targetMode === 'live') {
    const confirmed = confirm(
      '⚠️ LIVE TRADING CONFIRMATION ⚠️\n\n' +
      'You are about to enable LIVE TRADING mode with REAL CAPITAL on Kalshi exchange.\n\n' +
      'Fail-closed sensor validation and emergency risk limits will remain strictly active.\n\n' +
      'Are you sure you want to proceed to LIVE CAPITAL mode?'
    );
    if (!confirmed) {
      showToast('Live mode switch cancelled');
      return;
    }
  }

  sendConsoleCommand(`mode ${targetMode}`);
}

const toggleSwarmMode = triggerModeSwitch;

// ── Overview ───────────────────────────────────────────────
function renderOverview() {
  const s = State.data.status;
  if (!s || s.error) return;
  const bots = ['sentinel', 'oracle', 'pulse', 'vanguard'];
  const authVerified = s?.balance_verified !== false && s?.exchange_auth?.verified !== false;

  // Portfolio hero
  const portfolioCents = s?.portfolio_cents ?? 0;
  const changePct = s?.portfolio_change_pct ?? 0;
  const changePos = changePct >= 0;
  const totalEl = document.getElementById('ov-portfolio-total');
  if (totalEl) {
    if (!authVerified) {
      totalEl.innerHTML = `${fmt$(portfolioCents)} <span class="badge badge-unverified" title="Exchange balance unverified — cached locally">UNVERIFIED (LOCAL CACHE)</span>`;
    } else {
      totalEl.textContent = fmt$(portfolioCents);
    }
  }
  const changeEl = document.getElementById('ov-portfolio-change');
  if (changeEl) {
    changeEl.textContent = (changePos ? '+' : '') + fmtPct(changePct) + ' today';
    changeEl.className = 'portfolio-change ' + (changePos ? 'positive' : 'negative');
  }

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
      const isVerified = b.balance_verified !== false && authVerified;
      const swarmRunning = State.data.swarm?.running === true;

      let statusBadge;
      if (!swarmRunning) {
        if (paused) {
          statusBadge = badge('paused', 'warning');
        } else if (!isVerified) {
          statusBadge = '<span class="badge badge-degraded">SENSORS UNVERIFIED</span>';
        } else {
          statusBadge = '<span class="badge badge-info">READY</span>';
        }
      } else if (!active) {
        statusBadge = badge('inactive', 'error');
      } else if (paused) {
        statusBadge = badge('paused', 'warning');
      } else if (!isVerified) {
        statusBadge = '<span class="badge badge-degraded">DEGRADED SENSORS</span>';
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
            <div class="bot-metric-label">Shared Pool</div>
            <div class="bot-metric-value">${fmt$(balance)}${!isVerified ? ' <span class="badge badge-unverified" style="font-size:0.65rem">UNVERIFIED</span>' : ''}</div>
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
            <div class="bot-metric-value">${!isVerified ? '<span style="color:var(--color-rose)">Fail-Closed</span>' : (b.can_trade !== false ? '<span style="color:var(--color-emerald)">Approved</span>' : '<span style="color:var(--color-rose)">Locked</span>')}</div>
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
  const hasCleanTrades = (llm?.clean_period?.total_resolved || 0) > 0;
  const cleanWr = hasCleanTrades ? llm.clean_period.win_rate_pct : null;
  const hasLlmDecisions = (llm?.today?.total || 0) > 0;
  const llmApproval = hasLlmDecisions ? llm.today.approval_rate_pct : null;
  const tavily = sys?.tavily;
  const uptime = s?.uptime_seconds ?? sys?.uptime_seconds ?? 0;

  const statsEl = document.getElementById('ov-stats-row');
  if (statsEl) {
    statsEl.innerHTML = `
      <div class="card" style="padding:1rem;">
        <div style="font-size:0.72rem;text-transform:uppercase;color:var(--text-muted);font-weight:700;letter-spacing:0.05em;">Win Rate (Clean)</div>
        <div style="font-family:var(--font-mono);font-size:1.6rem;font-weight:800;color:${hasCleanTrades && cleanWr >= 55 ? 'var(--color-emerald)' : (hasCleanTrades && cleanWr > 0 ? 'var(--color-amber)' : 'var(--text-muted)')};margin-top:0.2rem;">${fmtPct(cleanWr)}</div>
        <div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.2rem;">${hasCleanTrades ? 'Baseline Gate: 55.0%' : 'Awaiting 50 settled trades'}</div>
      </div>
      <div class="card" style="padding:1rem;">
        <div style="font-size:0.72rem;text-transform:uppercase;color:var(--text-muted);font-weight:700;letter-spacing:0.05em;">Gemini Approval Alpha</div>
        <div style="font-family:var(--font-mono);font-size:1.6rem;font-weight:800;color:${hasLlmDecisions ? 'var(--color-cyan)' : 'var(--text-muted)'};margin-top:0.2rem;">${fmtPct(llmApproval)}</div>
        <div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.2rem;">${hasLlmDecisions ? 'Search Grounded' : 'Awaiting decisions'}</div>
      <div class="card" style="padding:1rem;">
        <div style="font-size:0.72rem;text-transform:uppercase;color:var(--text-muted);font-weight:700;letter-spacing:0.05em;">Web Search & Verification</div>
        <div style="font-family:var(--font-mono);font-size:1.6rem;font-weight:800;color:var(--color-emerald);margin-top:0.2rem;">ACTIVE</div>
        <div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.2rem;">Google Gemini Search Grounding</div>
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
  if (!data || data.error) return;

  const sc = data.scorecard ?? {};
  const cal = data.calibration ?? {};
  const cats = data.categories ?? {};
  const weights = data.weights ?? {};
  const llm = data.llm_intelligence ?? {};
  const rolling = data.rolling_win_rates ?? [];

  // 1. Hero scorecard
  const hasSettled = (cal.total_settled || 0) > 0;
  const badgeEl = document.getElementById('learn-verdict-badge');
  const stageEl = document.getElementById('learn-stage-title');
  const msgEl = document.getElementById('learn-status-msg');
  const eceEl = document.getElementById('learn-ece-val');
  const brierEl = document.getElementById('learn-brier-val');
  const recalibEl = document.getElementById('learn-recalib-count');

  if (badgeEl) {
    const isL = sc.is_learning || 'INSUFFICIENT_DATA';
    badgeEl.textContent = isL;
    badgeEl.className = 'badge ' + (isL === 'YES' ? 'badge-success' : (isL === 'CALIBRATING' ? 'badge-info' : 'badge-warning'));
  }
  if (stageEl) stageEl.textContent = sc.stage || 'Data Collection & Calibration';
  if (msgEl) msgEl.textContent = sc.status_message || 'Analyzing confidence calibration and strategy evolution.';
  if (eceEl) {
    const ece = cal.expected_calibration_error;
    if (hasSettled && ece != null) {
      eceEl.textContent = fmtPct(ece);
      eceEl.style.color = ece < 15 ? '#34d399' : (ece < 35 ? '#fbbf24' : '#f43f5e');
    } else {
      eceEl.textContent = '— (No Data)';
      eceEl.style.color = '#94a3b8';
    }
  }
  if (brierEl) {
    const brier = cal.brier_score;
    if (hasSettled && brier != null) {
      brierEl.textContent = brier.toFixed(4);
      brierEl.style.color = brier < 0.20 ? '#34d399' : (brier < 0.35 ? '#38bdf8' : '#fbbf24');
    } else {
      brierEl.textContent = '0.2500 (Prior Baseline)';
      brierEl.style.color = '#94a3b8';
    }
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
        const hasTrades = (b.trades || 0) > 0;
        const obsWrStr = hasTrades && b.observed_win_rate != null ? fmtPct(b.observed_win_rate) : '—';
        let errStr = '—';
        let errCls = 'text-muted';
        if (hasTrades && b.calibration_error != null) {
          const err = b.calibration_error;
          errStr = (err > 0 ? '+' : '') + fmtPct(err);
          errCls = Math.abs(err) <= 10 ? 'green' : (Math.abs(err) <= 25 ? 'orange' : 'red');
        }
        return `
          <tr>
            <td><strong>${esc(b.label)}</strong></td>
            <td>${b.trades}</td>
            <td>${hasTrades ? b.wins : '—'}</td>
            <td>${obsWrStr}</td>
            <td class="${errCls}">${errStr}</td>
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
        const hasDecided = (c.settled || 0) > 0;
        const st = c.status;
        const stBadge = st === 'hot'
          ? '<span class="badge badge-success">🔥 Hot</span>'
          : (st === 'cold' ? '<span class="badge badge-danger">❄️ Cold</span>' : '<span class="badge badge-secondary">⚖️ Neutral</span>');
        const pnlStr = fmt$(c.pnl_cents);
        const pnlCls = c.pnl_cents >= 0 ? 'green' : 'red';
        const wrStr = hasDecided && c.win_rate_pct != null ? fmtPct(c.win_rate_pct) : '—';
        const multStr = hasDecided ? `${c.multiplier.toFixed(2)}x` : '1.00x (Baseline)';
        return `
          <tr>
            <td><strong>${esc(c.category)}</strong></td>
            <td>${c.trades}</td>
            <td>${hasDecided ? c.wins : '—'}</td>
            <td>${wrStr}</td>
            <td><strong>${multStr}</strong></td>
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

    const hasDecisions = (llm.total_decisions || 0) > 0;
    const appRateStr = hasDecisions && llm.approval_rate_pct != null ? fmtPct(llm.approval_rate_pct) : 'N/A (No decisions)';

    weightsGrid.innerHTML = `
      ${botCards || defaultWeightsCard}
      <div style="background:rgba(255,255,255,0.03); padding:0.85rem; border-radius:6px; border:1px solid rgba(255,255,255,0.06);">
        <div style="font-weight:600; font-size:0.85rem; text-transform:uppercase; color:#94a3b8; margin-bottom:0.5rem;">Central LLM Filtering Impact</div>
        <div style="font-size:0.8rem; display:flex; flex-direction:column; gap:0.35rem; margin-bottom:0.75rem;">
          <div style="display:flex; justify-content:space-between;"><span>Total Decisions</span><strong>${llm.total_decisions ?? 0}</strong></div>
          <div style="display:flex; justify-content:space-between;"><span>Approved Rate</span><strong style="color:${hasDecisions ? '#34d399' : '#94a3b8'};">${appRateStr}</strong></div>
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
  const hasSettled = (cal.total_settled || 0) > 0;
  const observed = buckets.map(b => ((b.trades || 0) > 0 && b.observed_win_rate != null) ? b.observed_win_rate : null);
  const ideal = buckets.map(b => b.midpoint ?? 50);

  const observedLabel = hasSettled ? 'Observed Win Rate (%)' : 'Observed (Awaiting Observations)';

  if (State.calibrationChart) {
    State.calibrationChart.data.labels = labels;
    State.calibrationChart.data.datasets[0].label = observedLabel;
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
          label: observedLabel,
          data: observed,
          backgroundColor: 'rgba(99, 102, 241, 0.75)',
          borderColor: '#818cf8',
          borderWidth: 1,
          borderRadius: 4,
        },
        {
          type: 'line',
          label: 'Ideal Reference Calibration (45°)',
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
            label: ctx => {
              if (ctx.raw === null) return ` ${ctx.dataset.label}: No observations yet`;
              return ` ${ctx.dataset.label}: ${parseFloat(ctx.raw).toFixed(1)}%`;
            }
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
  if (!d || d.error) return;

  const today = d.today ?? {};
  const cp = d.clean_period ?? {};
  const recent = d.recent_decisions ?? [];

  // Header stats
  const h = document.getElementById('llm-header-stats');
  if (h) {
    const hasToday = (today.total || 0) > 0;
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
        <div class="stat-box-value ${hasToday ? 'blue' : 'text-muted'}">${hasToday && today.approval_rate_pct != null ? fmtPct(today.approval_rate_pct) : 'N/A'}</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-label">Real LLM %</div>
        <div class="stat-box-value ${hasToday ? '' : 'text-muted'}">${hasToday && today.real_llm_pct != null ? fmtPct(today.real_llm_pct) : 'N/A'}</div>
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
    const hasToday = (today.total || 0) > 0;
    const realPct = hasToday ? (today.real_llm_pct ?? 0) : 0;
    const hasClean = (cp.total_resolved || 0) > 0;
    const wrPct = hasClean ? (cp.win_rate_pct ?? 0) : 0;
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
        <div class="mt-1 text-muted" style="font-size:0.75rem">${hasToday ? `${fmtPct(today.real_llm_pct)} real LLM — ${fmtPct(100 - today.real_llm_pct)} quant fallback` : 'Awaiting LLM calls'}</div>
      </div>
      <div class="card mt-2">
        <div class="card-title">Clean Period Win Rate</div>
        <div class="progress-wrap">
          <div class="progress-label">
            <span>Win rate (target: 55%)</span>
            <span class="prog-val">${hasClean && cp.win_rate_pct != null ? fmtPct(wrPct) : 'N/A (0 settled)'}</span>
          </div>
          ${progressBar(hasClean ? clamp((wrPct / 55) * 100, 0, 100) : 0, wrPct >= 55 ? '' : (wrPct > 40 ? 'yellow' : 'red'), true)}
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
        <div class="mt-1 text-muted" style="font-size:0.75rem">Since ${esc(cp.start_date ?? '—')} (Fresh build warmup)</div>
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

  // Load and render autonomous supervisor / overseer diagnostics
  loadAndRenderOverseer();
}

async function loadAndRenderOverseer() {
  try {
    const res = await fetch('/api/supervisor/latest');
    if (!res.ok) return;
    const data = await res.json();
    renderOverseer(data);
  } catch (err) {
    console.error('Failed to load supervisor data:', err);
  }
}

function renderOverseer(report) {
  if (!report) return;
  
  const gradeBadge = document.getElementById('overseer-grade-badge');
  const lastTime = document.getElementById('overseer-last-audit-time');
  const healthScore = document.getElementById('overseer-health-score');
  const summarySub = document.getElementById('overseer-summary-sub');
  const autoActions = document.getElementById('overseer-auto-actions-count');
  const synthesisBox = document.getElementById('overseer-synthesis-box');
  const synthesisText = document.getElementById('overseer-synthesis-text');
  const tbody = document.getElementById('overseer-findings-tbody');

  const grade = report.health_grade || 'A';
  if (gradeBadge) {
    gradeBadge.textContent = 'GRADE ' + grade;
    gradeBadge.className = 'badge ' + (grade.startsWith('A') ? 'badge-success' : (grade.startsWith('B') ? 'badge-info' : (grade === 'C' ? 'badge-warning' : 'badge-error')));
  }
  if (lastTime && report.timestamp) {
    lastTime.textContent = 'Last audit: ' + new Date(report.timestamp).toLocaleTimeString() + ' UTC (' + (report.total_findings || 0) + ' findings)';
  }
  if (healthScore) {
    healthScore.textContent = (report.score_pct != null ? report.score_pct : '--') + '%';
    healthScore.style.color = (report.score_pct >= 85 ? 'var(--color-emerald)' : (report.score_pct >= 70 ? 'var(--color-amber)' : 'var(--color-rose)'));
  }
  if (summarySub && report.summary) {
    summarySub.textContent = report.summary;
  }
  if (autoActions) {
    autoActions.textContent = report.auto_actions_count || 0;
  }

  if (synthesisBox && synthesisText) {
    if (report.ai_synthesis) {
      synthesisText.textContent = report.ai_synthesis;
      synthesisBox.style.display = 'block';
    } else {
      synthesisBox.style.display = 'none';
    }
  }

  if (tbody) {
    const findings = report.findings || [];
    if (findings.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-muted" style="text-align:center;padding:1rem">✅ No active supervisor findings. System operating nominally.</td></tr>';
    } else {
      tbody.innerHTML = findings.map(f => {
        const sev = (f.severity || 'info').toLowerCase();
        const sevBadge = sev === 'critical' ? '<span class="badge badge-error">CRITICAL</span>' : (sev === 'warning' ? '<span class="badge badge-warning">WARNING</span>' : '<span class="badge badge-info">INFO</span>');
        const autoAction = f.auto_action_taken ? `<span style="color:var(--color-emerald)">⚡ ${esc(f.auto_action_taken)}</span>` : `<span class="text-muted">${esc(f.recommended_action || 'Monitoring')}</span>`;
        return `
          <tr>
            <td>${sevBadge}</td>
            <td><strong style="font-size:0.75rem; text-transform:uppercase; color:#94a3b8;">${esc(f.category || 'general')}</strong></td>
            <td><strong style="color:#ffffff;">${esc(f.title || '')}</strong></td>
            <td style="font-size:0.8rem; color:#cbd5e1;">${esc(f.description || '')}</td>
            <td style="font-size:0.8rem;">${autoAction}</td>
          </tr>
        `;
      }).join('');
    }
  }
}

window.runOverseerAuditNow = async function() {
  const btn = document.getElementById('btn-run-overseer-audit');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Auditing…';
  }
  try {
    const res = await fetch('/api/supervisor/run_now', { method: 'POST' });
    const data = await res.json();
    if (data.ok && data.report) {
      renderOverseer(data.report);
      showToast('AI Supervisor Audit completed.', 'success');
    } else {
      showToast(data.error || 'Audit failed', 'error');
    }
  } catch (err) {
    showToast('Failed running audit: ' + err, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '⚡ Run Audit Now';
    }
  }
};

// ── Trades Blotter ───────────────────────────────────────────
function renderTrades() {
  const trades = State.data.trades;
  if (!trades || trades.error) return;
  const filter = State.tradeFilter || 'all';
  const allList = Array.isArray(trades) ? trades : [];
  const filtered = filter === 'all' ? allList : allList.filter(t => t.bot === filter);

  // Update Blotter counters in toolbar
  const totalCountEl = document.getElementById('blotter-total-count');
  const winCountEl = document.getElementById('blotter-win-count');
  const lossCountEl = document.getElementById('blotter-loss-count');
  const netPnlEl = document.getElementById('blotter-net-pnl');

  const wins = allList.filter(t => (t.outcome || '').toLowerCase() === 'win').length;
  const losses = allList.filter(t => (t.outcome || '').toLowerCase() === 'loss').length;
  const netCents = allList.reduce((acc, t) => acc + (parseInt(t.pnl_cents, 10) || 0), 0);

  if (totalCountEl) totalCountEl.textContent = allList.length;
  if (winCountEl) winCountEl.textContent = wins;
  if (lossCountEl) lossCountEl.textContent = losses;
  if (netPnlEl) {
    netPnlEl.textContent = (netCents >= 0 ? '+' : '') + fmt$(netCents);
    netPnlEl.style.color = netCents > 0 ? 'var(--color-emerald)' : (netCents < 0 ? 'var(--color-rose)' : '#ffffff');
  }

  // Update filter buttons
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });

  const tbody = document.getElementById('trades-tbody');
  if (!tbody) return;

  if (filtered.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="7" style="text-align:center; padding:3rem 1.5rem; color:var(--text-muted);">
          <div style="font-size:1.4rem; margin-bottom:0.4rem;">📡</div>
          <div style="font-weight:700; color:var(--text-secondary); margin-bottom:0.25rem;">No Active Executions Recorded</div>
          <div style="font-size:0.78rem;">The swarm is monitoring Kalshi prediction orderbooks. New trades will stream here automatically.</div>
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = filtered.map(t => {
    const outcome = (t.outcome || '').toLowerCase();
    const rowCls = outcome === 'win' ? 'row-win' : outcome === 'loss' ? 'row-loss' : 'row-pending';
    const pnl = t.pnl_cents;
    const pnlEl = pnl != null
      ? `<span class="${pnl >= 0 ? 'td-pos' : 'td-neg'}">${pnl >= 0 ? '+' : ''}${fmt$(pnl)}</span>`
      : '<span class="text-muted">—</span>';

    const sideBadge = (t.side || '').toUpperCase() === 'YES'
      ? '<span class="badge" style="background:rgba(16,185,129,0.15); color:#34d399; border:1px solid rgba(16,185,129,0.35);">YES</span>'
      : '<span class="badge" style="background:rgba(244,63,94,0.15); color:#fb7185; border:1px solid rgba(244,63,94,0.35);">NO</span>';

    const outcomeEl = outcome === 'win'
      ? badge('WIN', 'green')
      : outcome === 'loss'
      ? badge('LOSS', 'red')
      : outcome
      ? badge(esc(outcome.toUpperCase()), 'grey')
      : '<span class="badge badge-warning">PENDING</span>';

    return `<tr class="${rowCls}">
      <td class="td-mono td-muted" style="font-size:0.75rem">${fmtDateTime(t.timestamp)}</td>
      <td class="td-cap"><span class="badge badge-info">${esc(t.bot)}</span></td>
      <td class="td-mono fw-bold" style="color:#ffffff;">${esc(t.ticker)}</td>
      <td>${sideBadge}</td>
      <td class="td-mono">${t.confidence != null ? fmtConf(t.confidence) : '—'}</td>
      <td>${outcomeEl}</td>
      <td>${pnlEl}</td>
    </tr>`;
  }).join('');
}

// ── Institutional Risk Matrix ──────────────────────────────
function renderRisk() {
  const r = State.data.risk;
  if (!r || r.error) return;
  const bots = ['sentinel', 'oracle', 'pulse', 'vanguard'];
  const auth = r?.exchange_auth || State.data.status?.exchange_auth;
  const authVerified = r?.balance_verified !== false && auth?.verified !== false;

  // Global Risk Badge
  const globalBadge = document.getElementById('risk-global-state-badge');
  if (globalBadge) {
    const isPaused = bots.some(b => r?.bots?.[b]?.paused);
    const hasHighDd = bots.some(b => (r?.bots?.[b]?.drawdown_pct || 0) > 15);
    if (!authVerified || auth?.status === 'degraded') {
      globalBadge.textContent = 'DEGRADED (EXCHANGE SENSORS OFFLINE)';
      globalBadge.className = 'badge badge-degraded';
    } else if (hasHighDd) {
      globalBadge.textContent = 'CIRCUIT BREAKER ACTIVE';
      globalBadge.className = 'badge badge-danger';
    } else if (isPaused) {
      globalBadge.textContent = 'BOTS PAUSED';
      globalBadge.className = 'badge badge-warning';
    } else {
      globalBadge.textContent = 'NOMINAL (CAPITAL SAFE)';
      globalBadge.className = 'badge badge-success';
    }
  }

  // Drawdown cards
  const ddEl = document.getElementById('risk-drawdown');
  if (ddEl) {
    const swarmRunning = State.data.swarm?.running === true;
    ddEl.innerHTML = bots.map(bot => {
      const b = r?.bots?.[bot] ?? {};
      const isVerified = b.balance_verified !== false && authVerified;
      const hasBaseline = (b.peak_balance_cents > 0) && b.drawdown_pct !== null && b.drawdown_pct !== undefined;
      const dd = hasBaseline ? b.drawdown_pct : null;

      let ddDisplay;
      let ddColor;
      let ddWidth = 0;
      let ddFillClass = '';

      if (!isVerified) {
        ddDisplay = '<span style="font-size:1.1rem;letter-spacing:0.04em;">UNVERIFIED</span>';
        ddColor = 'var(--color-amber)';
        ddWidth = 100;
        ddFillClass = 'yellow';
      } else if (!hasBaseline) {
        ddDisplay = '<span style="font-size:1.3rem;color:var(--text-muted);">N/A</span>';
        ddColor = 'var(--text-muted)';
        ddWidth = 0;
      } else {
        ddDisplay = fmtPct(dd);
        ddColor = dd > 15 ? 'var(--color-rose)' : dd > 5 ? 'var(--color-amber)' : 'var(--color-emerald)';
        ddWidth = clamp(dd * 6.66, 0, 100);
        ddFillClass = dd > 15 ? 'red' : dd > 5 ? 'yellow' : '';
      }

      let botBadge;
      if (!swarmRunning) {
        if (b.paused) {
          botBadge = '<span class="badge badge-warning">PAUSED</span>';
        } else if (!isVerified) {
          botBadge = '<span class="badge badge-degraded">SENSORS UNVERIFIED</span>';
        } else {
          botBadge = '<span class="badge badge-info">READY</span>';
        }
      } else if (b.paused) {
        botBadge = '<span class="badge badge-warning">PAUSED</span>';
      } else if (!isVerified) {
        botBadge = '<span class="badge badge-degraded">SENSORS UNVERIFIED</span>';
      } else if (b.can_trade !== false) {
        botBadge = '<span class="badge badge-success">ACTIVE TRADING</span>';
      } else {
        botBadge = '<span class="badge badge-danger">BLOCKED</span>';
      }

      const pnl = b.daily_pnl_cents ?? 0;
      const pnlPos = pnl >= 0;
      const pnlColor = pnlPos ? 'var(--color-emerald)' : 'var(--color-rose)';

      return `
        <div class="risk-card">
          <div class="risk-card-header">
            <div style="font-weight:800; font-size:1.05rem; text-transform:capitalize; color:#ffffff;">${esc(bot)}</div>
            ${botBadge}
          </div>
          
          <div class="risk-dd-box">
            <div class="risk-dd-title-row">
              <span class="risk-dd-title">Current Drawdown</span>
              <span style="font-size:0.72rem; color:var(--text-muted);">Max Cap: 15.0%</span>
            </div>
            <div class="risk-dd-number" style="color:${ddColor};">${ddDisplay}</div>
            ${!hasBaseline && isVerified ? '<div style="font-size:0.7rem;color:var(--text-muted);margin-top:0.2rem;">No high-water baseline</div>' : ''}
            <div class="progress-track" style="margin-top:0.5rem;">
              <div class="progress-fill ${ddFillClass}" style="width:${ddWidth}%;"></div>
            </div>
          </div>

          <div class="bot-metric-grid">
            <div class="bot-metric-item">
              <div class="bot-metric-label">Allocated Capital</div>
              <div class="bot-metric-value">${fmt$(b.balance_cents)}${!isVerified ? ' <span class="badge badge-unverified" style="font-size:0.65rem">UNVERIFIED</span>' : ''}</div>
            </div>
            <div class="bot-metric-item">
              <div class="bot-metric-label">Session P&amp;L</div>
              <div class="bot-metric-value" style="color:${pnlColor};">${pnlPos ? '+' : ''}${fmt$(pnl)}</div>
            </div>
            <div class="bot-metric-item">
              <div class="bot-metric-label">High Watermark</div>
              <div class="bot-metric-value">${fmt$(b.peak_balance_cents)}${!isVerified ? ' <span class="badge badge-unverified" style="font-size:0.65rem">UNVERIFIED</span>' : ''}</div>
            </div>
            <div class="bot-metric-item">
              <div class="bot-metric-label">Open Positions</div>
              <div class="bot-metric-value" style="color:var(--color-cyan);">${isVerified ? `${b.open_positions ?? 0} contracts` : '<span class="badge badge-unverified">UNVERIFIED</span>'}</div>
            </div>
          </div>
        </div>
      `;
    }).join('');
  }

  // Guardrail Milestone Progress
  const g = r?.guardrail_progress ?? {};
  const wrCur = g.win_rate_current ?? 0;
  const wrTgt = g.win_rate_target ?? 55;
  const tcCur = g.trade_count_current ?? 0;
  const tcTgt = g.trade_count_target ?? 50;
  const dpCur = g.days_positive_pnl ?? 0;
  const dpTgt = g.days_positive_target ?? 14;
  const ready = g.ready_to_loosen ?? false;

  const milestonesEl = document.getElementById('risk-guardrail-milestones');
  if (milestonesEl) {
    const wrPassed = wrCur >= wrTgt && tcCur >= 10;
    const tcPassed = tcCur >= tcTgt;
    const dpPassed = dpCur >= dpTgt;

    milestonesEl.innerHTML = `
      <div class="milestone-item">
        <div class="milestone-header">
          <span class="milestone-name">1. Win Rate Baseline</span>
          <span class="badge ${wrPassed ? 'badge-success' : 'badge-warning'}">${tcCur > 0 ? fmtPct(wrCur) : 'N/A'} / ${wrTgt}%</span>
        </div>
        <div style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.75rem;">Minimum 55.0% prediction edge required to scale capital.</div>
        <div class="progress-track tall">
          <div class="progress-fill ${wrPassed ? '' : 'yellow'}" style="width:${tcCur > 0 ? clamp((wrCur / wrTgt) * 100, 0, 100) : 0}%;"></div>
        </div>
      </div>

      <div class="milestone-item">
        <div class="milestone-header">
          <span class="milestone-name">2. Sample Significance</span>
          <span class="badge ${tcPassed ? 'badge-success' : 'badge-info'}">${tcCur} / ${tcTgt} Trades</span>
        </div>
        <div style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.75rem;">50 resolved outcomes needed to rule out variance.</div>
        <div class="progress-track tall">
          <div class="progress-fill" style="width:${clamp((tcCur / tcTgt) * 100, 0, 100)}%;"></div>
        </div>
      </div>

      <div class="milestone-item">
        <div class="milestone-header">
          <span class="milestone-name">3. P&amp;L Consistency Streak</span>
          <span class="badge ${dpPassed ? 'badge-success' : 'badge-secondary'}">${dpCur} / ${dpTgt} Days</span>
        </div>
        <div style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.75rem;">14 cumulative profitable trading days required.</div>
        <div class="progress-track tall">
          <div class="progress-fill purple" style="width:${clamp((dpCur / dpTgt) * 100, 0, 100)}%;"></div>
        </div>
      </div>
    `;
  }

  const bannerEl = document.getElementById('risk-guardrail-banner');
  if (bannerEl) {
    bannerEl.innerHTML = `
      <div class="status-banner ${ready ? 'ready' : 'not-ready'}">
        <span>${ready ? '✓ ALL 3 GATES CLEARED — CAPITAL SCALING AUTHORIZED' : '🛡️ GATEKEEPER ENGAGED (0/3 MILESTONES CLEARED) — RETAINING CAPITAL PRESERVATION LIMITS'}</span>
        <span style="font-size:0.78rem; font-weight:600; opacity:0.85;">${ready ? 'Threshold loosened 70%→65%' : 'Autonomous Scaling Interlock Active'}</span>
      </div>
    `;
  }
}

// ── System Telemetry ───────────────────────────────────────
function renderSystem() {
  const sys = State.data.system;
  if (!sys || sys.error) return;

  const tavilyEl = document.getElementById('sys-tavily');
  if (tavilyEl) {
    tavilyEl.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:baseline; margin:0.25rem 0 0.5rem;">
        <span style="font-family:var(--font-mono); font-size:1.1rem; font-weight:800; color:var(--color-emerald);">ACTIVE</span>
        <span class="badge badge-success">Google Grounding</span>
      </div>
      <div class="mt-1 text-muted" style="font-size:0.75rem;">Real-Time Web Intelligence</div>
    `;
  }

  // Gemini AI Brain status
  const llmEl = document.getElementById('sys-llm');
  if (llmEl) {
    const ok = (sys.llm_status === 'ok' || sys.gemini_status === 'ok');
    llmEl.innerHTML = `
      <div style="display:flex; align-items:center; gap:0.5rem; margin:0.25rem 0;">
        <span style="font-family:var(--font-mono); font-size:1.1rem; font-weight:800; color:${ok ? 'var(--color-emerald)' : 'var(--color-rose)'};">${ok ? 'ONLINE' : 'OFFLINE'}</span>
        <span class="badge badge-info">Gemini 2.5 Flash</span>
      </div>
      <div style="font-size:0.75rem; color:var(--text-muted);">Search-Grounded Macro Inference</div>
    `;
  }

  // Uptime
  const uptimeEl = document.getElementById('sys-uptime');
  if (uptimeEl) {
    uptimeEl.textContent = fmtUptime(sys.uptime_seconds ?? 0);
  }

  // Health report grid
  const healthGrid = document.getElementById('sys-health-grid');
  const healthSummary = document.getElementById('sys-health-summary');
  if (healthGrid) {
    const hr = sys.health_report ?? {};
    const checks = hr.checks ?? hr;
    if (typeof checks === 'object' && !Array.isArray(checks) && Object.keys(checks).length > 0) {
      const entries = Object.entries(checks);
      const passCount = entries.filter(([_, v]) => (v.status || 'OK').toUpperCase() === 'OK').length;
      const degCount = entries.filter(([_, v]) => (v.status || '').toUpperCase() === 'DEGRADED').length;
      const unkCount = entries.filter(([_, v]) => (v.status || '').toUpperCase() === 'UNKNOWN').length;
      const failCount = entries.filter(([_, v]) => ['CRITICAL','FAIL','ERROR'].includes((v.status || '').toUpperCase())).length;

      let summaryText = `${passCount}/${entries.length} Checks Nominal`;
      if (degCount > 0) summaryText += ` | ${degCount} DEGRADED`;
      if (unkCount > 0) summaryText += ` | ${unkCount} UNKNOWN`;
      if (failCount > 0) summaryText += ` | ${failCount} FAIL`;

      if (healthSummary) healthSummary.textContent = hr.summary ? hr.summary.split('\n')[0] : summaryText;

      healthGrid.innerHTML = entries.map(([k, v]) => {
        let status = 'OK';
        let details = '';
        if (typeof v === 'object' && v !== null) {
          status = (v.status || 'OK').toUpperCase();
          details = v.details || (v.issues && v.issues.length ? v.issues.join('; ') : '');
        } else {
          status = String(v).toUpperCase();
          details = status;
        }

        let badgeClass = 'badge-success';
        if (status === 'DEGRADED') badgeClass = 'badge-degraded';
        else if (status === 'UNKNOWN') badgeClass = 'badge-unknown';
        else if (status === 'NOT_APPLICABLE' || status === 'SKIP') badgeClass = 'badge-na';
        else if (status === 'WARNING' || status === 'WARN') badgeClass = 'badge-warning';
        else if (status === 'CRITICAL' || status === 'FAIL' || status === 'ERROR') badgeClass = 'badge-danger';
        else if (status === 'INFO') badgeClass = 'badge-info';

        const label = k.replace(/_/g, ' ');
        return `
          <div class="health-item-card">
            <span class="badge ${badgeClass}">${esc(status)}</span>
            <div class="health-item-info">
              <div class="health-item-title">${esc(label)}</div>
              <div class="health-item-desc" title="${esc(details)}">${esc(details || 'Nominal execution')}</div>
            </div>
          </div>
        `;
      }).join('');
    } else {
      healthGrid.innerHTML = '<p class="text-muted" style="padding:1rem;">16 automated health checks running in background…</p>';
    }
  }

  // Log tail
  const logEl = document.getElementById('sys-log-tail');
  if (logEl) {
    const lines = sys.log_tail ?? [];
    logEl.textContent = lines.length > 0 ? lines.join('\n') : '(no log records captured)';
    logEl.scrollTop = logEl.scrollHeight;
  }
}

// ── Bot Dispatch Controls ──────────────────────────────────
function renderControls() {
  const s = State.data.status;
  const bots = ['sentinel', 'oracle', 'pulse', 'vanguard'];
  const auth = s?.exchange_auth || State.data.system?.exchange_auth;
  const authVerified = s?.balance_verified !== false && auth?.verified !== false;
  const swarmRunning = State.data.swarm?.running === true;

  const container = document.getElementById('controls-bot-list');
  if (container) {
    const degradedNote = !authVerified
      ? `<div class="card section-gap" style="background:rgba(225,29,72,0.12);border-color:rgba(225,29,72,0.4);margin-bottom:1.25rem;">
          <div style="font-weight:700;color:#fda4af;display:flex;align-items:center;gap:0.5rem;">
            <span>⚠️</span> EXCHANGE AUTHENTICATION DEGRADED — ORDER EXECUTION SENSORS OFFLINE
          </div>
          <div style="font-size:0.8rem;color:#fecdd3;margin-top:0.35rem;">
            Emergency order cancellations and live execution capability are DEGRADED / UNVERIFIED due to HTTP 401 Unauthorized status with Kalshi API.
          </div>
        </div>`
      : '';

    container.innerHTML = degradedNote + bots.map(bot => {
      const b = s?.bots?.[bot] ?? {};
      const paused = b.paused;
      const pnl = b.daily_pnl_cents ?? 0;
      const pnlPos = pnl >= 0;
      const tradeCount = b.daily_trades ?? 0;
      const maxTrades = b.max_trades ?? 8;
      const quotaPct = (tradeCount / maxTrades) * 100;
      const isVerified = b.balance_verified !== false && authVerified;

      let botBadge;
      if (!swarmRunning) {
        if (paused) {
          botBadge = '<span class="badge badge-warning">PAUSED</span>';
        } else if (!isVerified) {
          botBadge = '<span class="badge badge-degraded">SENSORS UNVERIFIED</span>';
        } else {
          botBadge = '<span class="badge badge-info">READY</span>';
        }
      } else if (paused) {
        botBadge = '<span class="badge badge-warning">PAUSED</span>';
      } else if (!isVerified) {
        botBadge = '<span class="badge badge-degraded">SENSORS UNVERIFIED</span>';
      } else {
        botBadge = '<span class="badge badge-success">ACTIVE TRADING</span>';
      }

      return `
        <div class="control-deck-card" id="ctrl-card-${bot}">
          <div class="control-deck-info">
            <div class="bot-avatar">${bot.charAt(0)}</div>
            <div>
              <div style="font-weight:800; font-size:1.1rem; text-transform:capitalize; color:#ffffff; display:flex; align-items:center; gap:0.6rem;">
                ${esc(bot)}
                ${botBadge}
              </div>
              <div style="font-size:0.78rem; color:var(--text-muted); margin-top:0.25rem;">
                Allocated Capital: <strong style="color:#ffffff;">${fmt$(b.balance_cents)}</strong>${!isVerified ? ' <span class="badge badge-unverified" style="font-size:0.65rem">UNVERIFIED</span>' : ''} &nbsp;|&nbsp;
                Session P&amp;L: <strong style="color:${pnlPos ? 'var(--color-emerald)' : 'var(--color-rose)'};">${pnlPos ? '+' : ''}${fmt$(pnl)}</strong>
              </div>
            </div>
          </div>

          <div style="display:flex; align-items:center; gap:2rem; flex-wrap:wrap;">
            <div style="min-width:140px;">
              <div style="display:flex; justify-content:space-between; font-size:0.72rem; color:var(--text-muted); margin-bottom:0.35rem;">
                <span>Daily Quota</span>
                <strong style="color:#ffffff;">${tradeCount} / ${maxTrades}</strong>
              </div>
              <div class="progress-track" style="width:140px;">
                <div class="progress-fill ${quotaPct > 80 ? 'yellow' : ''}" style="width:${clamp(quotaPct, 0, 100)}%;"></div>
              </div>
            </div>

            <div style="display:flex; gap:0.5rem;">
              ${paused
                ? `<button class="btn btn-green" onclick="controlBot('${bot}','resume')">▶ Resume</button>`
                : `<button class="btn btn-ghost" onclick="controlBot('${bot}','pause')">⏸ Pause</button>`
              }
            </div>
          </div>
        </div>
      `;
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
  if (!confirm('FINAL CONFIRMATION: Send kill signal to stop the entire swarm and cancel active exchange orders?')) {
    inp.value = '';
    return;
  }

  const result = await apiFetch('/api/kill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: 'KILL' }),
  });

  if (result?.ok) {
    const canc = result.cancellation || {};
    const cancSummary = canc.details ? `\n\nExchange Cancellation:\nStatus: ${canc.status} (${canc.exchange_verified ? 'Verified' : 'UNVERIFIED'})\n${canc.details}` : '';
    alert(`EMERGENCY KILL SIGNAL EXECUTED\n\n${result.message || 'Swarm processes halted.'}${cancSummary}`);
    showToast(result.message || 'Kill signal sent. Swarm stopped.');
    inp.value = '';
    await loadAll();
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
  if (cmdMode) cmdMode.addEventListener('click', triggerModeSwitch);
  if (modeSwitchCard) modeSwitchCard.addEventListener('click', triggerModeSwitch);
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
  loadAll(true);

  // Smooth real-time auto-refresh every 8s
  setInterval(() => loadAll(false), 8000);
});
