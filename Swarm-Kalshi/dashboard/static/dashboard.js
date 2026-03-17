/**
 * dashboard.js — Kalshi Bot Swarm Command Center
 * Extracted from dashboard.html for cleanliness.
 *
 * NOTE: The main dashboard.html has the JS inline for portability when running
 * without the static folder. This file mirrors that logic and can be used when
 * linking separately via <script src="/static/dashboard.js">.
 *
 * Key constants that must be set before this script loads (via template injection):
 *   window.BOT_NAMES  — array of bot name strings
 *   window.BOT_DISPLAY — object mapping bot names to {name, specialist, color}
 *   window.REFRESH_MS — auto-refresh interval in milliseconds
 *   window.MANUAL_CONTROLS_LOCKED — boolean
 */

'use strict';

// Utility: format cents as dollar string
function fmt$(cents) {
  if (cents == null || isNaN(cents)) return '$0.00';
  const d = cents / 100;
  return d >= 0 ? '+$' + d.toFixed(2) : '-$' + Math.abs(d).toFixed(2);
}

function fmtDollars(dollars) {
  if (dollars == null || isNaN(dollars)) return '$0.00';
  return dollars >= 0 ? '+$' + dollars.toFixed(2) : '-$' + Math.abs(dollars).toFixed(2);
}

function pnlClass(v) { return v >= 0 ? 'positive pnl-pos' : 'negative pnl-neg'; }
function stateClass(s) { return ['running','paused','stopped','error','unknown'].includes(s) ? s : 'unknown'; }

function escapeHtml(text) {
  return (text || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Fetch helpers
async function fetchJSON(url) {
  try { const r = await fetch(url); return await r.json(); }
  catch(e) { console.warn('Fetch error:', url, e); return null; }
}

// Chart helpers
const charts = {};
function makeChart(id, cfg) {
  const el = document.getElementById(id);
  if (!el) return null;
  if (charts[id]) { charts[id].destroy(); }
  charts[id] = new Chart(el.getContext('2d'), cfg);
  return charts[id];
}

const CHART_OPTS = {
  scales: {
    x: { ticks: { color: '#6b7c8d' }, grid: { color: 'rgba(255,255,255,.05)' } },
    y: { ticks: { color: '#6b7c8d' }, grid: { color: 'rgba(255,255,255,.05)' } },
  },
  plugins: { legend: { labels: { color: '#cdd6e0', font: { size: 11 } } } },
  responsive: true,
  maintainAspectRatio: false,
};

// Kill Switch
async function triggerKillSwitch() {
  if (!confirm('EMERGENCY KILL SWITCH\n\nThis will immediately stop ALL bots.\n\nAre you absolutely sure?')) return;
  if (!confirm('Second confirmation: Stop all bots NOW?')) return;
  const res = await fetch('/api/kill-switch', { method: 'POST' });
  const data = await res.json();
  if (data.success) {
    alert('Kill switch activated. All bots have been stopped.\n' + JSON.stringify(data.results, null, 2));
  } else {
    alert('Kill switch failed: ' + JSON.stringify(data));
  }
}

// Watchlist functions
async function addToWatchlist() {
  const ticker = document.getElementById('wl-ticker').value.trim().toUpperCase();
  const label = document.getElementById('wl-label').value.trim();
  if (!ticker) { alert('Ticker is required.'); return; }
  const res = await fetch('/api/watchlist', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ ticker, label }),
  });
  const data = await res.json();
  if (data.success) {
    document.getElementById('wl-ticker').value = '';
    document.getElementById('wl-label').value = '';
    refreshWatchlist();
  } else {
    alert('Error: ' + data.error);
  }
}

async function removeFromWatchlist(ticker) {
  if (!confirm(`Remove ${ticker} from watchlist?`)) return;
  await fetch(`/api/watchlist/${encodeURIComponent(ticker)}`, { method: 'DELETE' });
  refreshWatchlist();
}

async function refreshWatchlist() {
  const data = await fetchJSON('/api/watchlist');
  const container = document.getElementById('watchlist-items');
  if (!container) return;
  if (!data || !data.length) {
    container.innerHTML = '<div style="color:var(--text-dim);font-size:.84rem;padding:8px">No tickers on watchlist yet.</div>';
    return;
  }
  container.innerHTML = data.map(w => `
    <div class="watchlist-item">
      <span class="wl-ticker">${w.ticker}</span>
      ${w.label ? `<span class="wl-label">${w.label}</span>` : ''}
      <span style="margin-left:auto;font-size:.7rem;color:var(--text-dim)">${(w.added_at||'').slice(0,10)}</span>
      <span class="wl-del" onclick="removeFromWatchlist('${w.ticker}')">✕ Remove</span>
    </div>
  `).join('');
}

// Journal functions
async function refreshJournal() {
  const botFilter = document.getElementById('jnl-filter-bot')?.value || '';
  let url = '/api/journal';
  if (botFilter) url += '?bot=' + encodeURIComponent(botFilter);
  const data = await fetchJSON(url);
  const container = document.getElementById('journal-entries');
  if (!container) return;
  if (!data || !data.length) {
    container.innerHTML = '<div style="color:var(--text-dim);font-size:.84rem;padding:8px">No journal entries yet.</div>';
    return;
  }
  const BOT_DISPLAY = window.BOT_DISPLAY || {};
  container.innerHTML = data.map(e => {
    const botColor = e.bot_name ? (BOT_DISPLAY[e.bot_name]?.color || '#6b7c8d') : '#6b7c8d';
    const botName = e.bot_name ? (BOT_DISPLAY[e.bot_name]?.name || e.bot_name) : 'All bots';
    return `
    <div class="journal-entry">
      <div class="je-meta">
        <span style="color:${botColor};font-weight:600">${botName}</span>
        <span>${e.date || (e.created_at||'').slice(0,10)}</span>
        ${e.trade_id ? `<span>Trade #${e.trade_id}</span>` : ''}
        <span style="color:var(--text-dim);font-size:.68rem">${(e.created_at||'').slice(11,16)} UTC</span>
      </div>
      <div class="je-note">${escapeHtml(e.note)}</div>
      <span class="je-del" onclick="deleteJournalEntry(${e.id})">✕ Delete</span>
    </div>`;
  }).join('');
}

async function addJournalEntry() {
  const note = document.getElementById('jnl-note').value.trim();
  if (!note) { alert('Note text is required.'); return; }
  const bot_name = document.getElementById('jnl-bot').value;
  const date = document.getElementById('jnl-date').value;
  const trade_id = document.getElementById('jnl-trade-id').value.trim();
  const res = await fetch('/api/journal', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ note, bot_name, date, trade_id }),
  });
  const data = await res.json();
  if (data.success) {
    document.getElementById('jnl-note').value = '';
    document.getElementById('jnl-trade-id').value = '';
    refreshJournal();
  } else {
    alert('Error: ' + data.error);
  }
}

async function deleteJournalEntry(id) {
  if (!confirm('Delete this journal entry?')) return;
  await fetch(`/api/journal/${id}`, { method: 'DELETE' });
  refreshJournal();
}

// Config editor
async function loadConfig() {
  const data = await fetchJSON('/api/config');
  if (!data) return;
  const el = document.getElementById('config-editor');
  const pathEl = document.getElementById('config-path');
  const statusEl = document.getElementById('config-status');
  if (el) el.value = data.yaml_text || '';
  if (pathEl) pathEl.textContent = data.path || '';
  if (statusEl) statusEl.textContent = data.success ? 'Config loaded.' : ('Error: ' + data.error);
}

async function saveConfig() {
  const el = document.getElementById('config-editor');
  const statusEl = document.getElementById('config-status');
  if (!el) return;
  const yaml_text = el.value;
  if (!confirm('Save config changes to swarm_config.yaml?\n\nA backup will be created automatically.')) return;
  if (statusEl) statusEl.textContent = 'Saving...';
  const res = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ yaml_text }),
  });
  const data = await res.json();
  if (statusEl) {
    statusEl.textContent = data.success
      ? `Saved successfully at ${new Date().toLocaleTimeString()}.`
      : 'Save failed: ' + data.error;
    statusEl.style.color = data.success ? 'var(--green)' : 'var(--red)';
  }
}

// Admin panel
async function refreshAdminLogs() {
  const lines = parseInt(document.getElementById('log-lines-select')?.value || 200);
  const data = await fetchJSON(`/api/admin/logs?lines=${lines}`);
  const viewer = document.getElementById('log-viewer');
  if (!viewer) return;
  if (!data) { viewer.innerHTML = '<div class="log-line">Failed to load logs.</div>'; return; }
  if (data.error) { viewer.innerHTML = `<div class="log-line error">${escapeHtml(data.error)}</div>`; return; }
  viewer.innerHTML = (data.lines || []).map(line => {
    let cls = 'info';
    const ll = line.toLowerCase();
    if (ll.includes('error') || ll.includes('exception') || ll.includes('critical')) cls = 'error';
    else if (ll.includes('warn') || ll.includes('warning')) cls = 'warn';
    return `<div class="log-line ${cls}">${escapeHtml(line)}</div>`;
  }).join('');
  viewer.scrollTop = viewer.scrollHeight;
}

async function adminAction(action, confirmMsg) {
  if (!confirm(confirmMsg)) return;
  const res = await fetch(`/api/admin/${action}`, { method: 'POST' });
  const data = await res.json();
  alert(data.success ? 'Done: ' + JSON.stringify(data, null, 2) : 'Failed: ' + (data.error || JSON.stringify(data)));
  if (action === 'log-rotate') refreshAdminLogs();
  refreshAuditLog();
}

async function refreshAuditLog() {
  const data = await fetchJSON('/api/audit-log?limit=50');
  const feed = document.getElementById('admin-audit-feed');
  if (!feed) return;
  if (!data || !data.length) {
    feed.innerHTML = '<div style="color:var(--text-dim);font-size:.82rem;padding:8px">No audit events yet.</div>';
    return;
  }
  feed.innerHTML = data.map(e => `
    <div class="audit-item">
      <span class="aud-time">${(e.created_at||'').slice(0,16).replace('T',' ')}</span>
      <span class="aud-action">${escapeHtml(e.action)}</span>
      <span class="aud-target">${escapeHtml(e.target||'')}</span>
      <span class="aud-detail">${escapeHtml(e.detail||'')}</span>
    </div>
  `).join('');
}

// Export functions that the HTML needs
window.triggerKillSwitch = triggerKillSwitch;
window.addToWatchlist = addToWatchlist;
window.removeFromWatchlist = removeFromWatchlist;
window.refreshWatchlist = refreshWatchlist;
window.refreshJournal = refreshJournal;
window.addJournalEntry = addJournalEntry;
window.deleteJournalEntry = deleteJournalEntry;
window.loadConfig = loadConfig;
window.saveConfig = saveConfig;
window.refreshAdminLogs = refreshAdminLogs;
window.adminAction = adminAction;
window.refreshAuditLog = refreshAuditLog;
window.fmt$ = fmt$;
window.fmtDollars = fmtDollars;
window.makeChart = makeChart;
window.CHART_OPTS = CHART_OPTS;
