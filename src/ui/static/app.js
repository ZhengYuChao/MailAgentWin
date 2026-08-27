/**
 * MailAgent UI — app.js
 * Hash router, Setup flow, Status polling, Settings, Logs streaming.
 * No frameworks. Vanilla ES2020+.
 */

// ── Config ────────────────────────────────────────────────────────────────────
const CSRF_TOKEN = document.cookie.match(/csrf_token=([^;]+)/)?.[1]
  ?? document.querySelector('meta[name="csrf-token"]')?.content
  ?? (() => {
    // Read CSRF token from the X-CSRF-Token response header that was injected
    // into a hidden meta tag by the server when serving index.html.
    // Fall back to fetching it from an endpoint.
    return '';
  })();

// The server injects the CSRF token as a response header X-CSRF-Token on GET /.
// We capture it from the meta tag we request on page load.
let _csrfToken = '';

// ── State ─────────────────────────────────────────────────────────────────────
let _currentView = '';
let _statusPollTimer = null;
let _logPollTimer = null;
let _logCursor = 0;
let _logPaused = false;
let _logBuffer = [];
const MAX_LOG_LINES = 2000;
let _notionAuthPolling = false;
let _notionAuthComplete = false;
let _setupRequestPending = false;
let _settingsDirty = false;

// ── CSRF Token Bootstrap ──────────────────────────────────────────────────────
async function fetchCsrfToken() {
  try {
    const resp = await fetch('/api/csrf', { method: 'GET' });
    const data = await resp.json();
    if (data?.data?.csrfToken) {
      _csrfToken = data.data.csrfToken;
    }
  } catch (e) {
    // Fallback: fetch /
    try {
      const resp = await fetch('/', { method: 'GET' });
      const token = resp.headers.get('X-CSRF-Token');
      if (token) _csrfToken = token;
    } catch (e2) { /* ignore */ }
  }
}

function apiHeaders(extra = {}) {
  const h = {
    'Content-Type': 'application/json',
    ...extra,
  };
  if (_csrfToken) {
    h['X-CSRF-Token'] = _csrfToken;
  }
  return h;
}

async function apiFetch(method, path, body = null, timeoutMs = 90000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const opts = {
      method,
      headers: apiHeaders(),
      signal: ctrl.signal,
    };
    if (body !== null) opts.body = JSON.stringify(body);
    const resp = await fetch(path, opts);
    const text = await resp.text();
    clearTimeout(timer);

    let data;
    try {
      data = JSON.parse(text);
    } catch (parseErr) {
      if (!resp.ok) {
        throw new Error(`Server returned error (${resp.status}): ${text}`);
      }
      throw new Error(`Invalid response format from server: ${text}`);
    }

    return { ok: resp.ok, status: resp.status, data };
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') throw new Error('Request timed out.');
    throw e;
  }
}

// ── Router ────────────────────────────────────────────────────────────────────
function navigate(view) {
  if (_currentView === view) return;
  _currentView = view;

  // Update views
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  const target = document.getElementById(`view-${view}`);
  if (target) target.classList.add('active');

  // Update nav
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === view);
  });

  // Update topbar title
  const titles = { status: 'Status', settings: 'Settings' };
  const titleEl = document.getElementById('topbar-title');
  if (titleEl) titleEl.textContent = titles[view] || '';

  // Hide service state badge (e.g. Running/Stopped) when on Settings or other non-status views
  const serviceStateEl = document.getElementById('service-state');
  if (serviceStateEl) {
    serviceStateEl.style.display = view === 'status' ? '' : 'none';
  }

  // Side effects
  if (view === 'setup') {
    document.body.classList.add('setup-mode');
    const sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.style.display = 'none';
  } else {
    document.body.classList.remove('setup-mode');
    const sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.style.display = '';
  }

  if (view === 'status') {
    startStatusPolling();
  } else {
    stopStatusPolling();
  }
  if (view === 'settings') {
    loadSettings();
  } else {
    stopLogsPolling();
  }

  // Update hash
  history.replaceState(null, '', `#${view}`);
}

// ── Setup ─────────────────────────────────────────────────────────────────────
function initSetup() {
  // Show/hide token
  document.getElementById('btn-toggle-token').addEventListener('click', () => {
    const inp = document.getElementById('f-token');
    const btn = document.getElementById('btn-toggle-token');
    if (inp.type === 'password') {
      inp.type = 'text'; btn.textContent = 'Hide';
    } else {
      inp.type = 'password'; btn.textContent = 'Show';
    }
  });

  // Notion sign-in buttons
  document.getElementById('btn-notion-signin').addEventListener('click', handleNotionSignInButtonClick);
  const skipBtn = document.getElementById('btn-notion-skip');
  if (skipBtn) {
    skipBtn.addEventListener('click', handleNotionSkipClick);
  }

  // Validate and start
  document.getElementById('btn-validate').addEventListener('click', onSetupSubmit);
  document.getElementById('btn-validate').disabled = true;

  // Check on load if auth is already complete
  checkNotionAuthStatus();
}

function updateValidateButton() {
  const ready = _notionAuthComplete;
  document.getElementById('btn-validate').disabled = !ready || _setupRequestPending;
}

let _notionSignInStep = 'initial'; // 'initial' | 'waiting_continue' | 'complete'

async function checkNotionAuthStatus() {
  try {
    const { ok, data } = await apiFetch('GET', '/api/auth/notion-ai/status', null, 5000);
    if (ok && data?.data?.is_complete) {
      _notionAuthComplete = true;

      const signinBtn = document.getElementById('btn-notion-signin');
      const skipBtn = document.getElementById('btn-notion-skip');
      const hint = document.getElementById('notion-signin-hint');
      const status = document.getElementById('notion-signin-status');

      if (skipBtn) {
        skipBtn.style.display = 'inline-flex';
        skipBtn.textContent = 'Use existing login';
        skipBtn.className = 'btn btn-outline';
      }
      if (signinBtn) {
        signinBtn.style.display = 'inline-flex';
        signinBtn.textContent = 'Re-authenticate';
        signinBtn.className = 'btn btn-primary';
      }
      if (hint) {
        hint.innerHTML = '<strong>Existing login detected (notion_auth.json).</strong> You can reuse it or re-authenticate in browser.';
      }
      if (status) {
        status.textContent = 'Ready (existing session available).';
        status.className = 'notion-signin-status success';
      }
      updateValidateButton();
    }
  } catch (e) { /* ignore */ }
}

function handleNotionSkipClick() {
  _notionAuthComplete = true;
  _notionSignInStep = 'complete';

  const skipBtn = document.getElementById('btn-notion-skip');
  const signinBtn = document.getElementById('btn-notion-signin');
  const status = document.getElementById('notion-signin-status');

  if (skipBtn) {
    skipBtn.disabled = true;
    skipBtn.className = 'btn btn-success';
    skipBtn.textContent = '✓ Using existing login';
  }
  if (signinBtn) {
    signinBtn.style.display = 'none';
  }
  if (status) {
    status.textContent = 'Using existing authentication session.';
    status.className = 'notion-signin-status success';
  }
  updateValidateButton();
}

async function handleNotionSignInButtonClick() {
  if (_notionSignInStep === 'initial' || _notionSignInStep === 'complete') {
    await startNotionSignIn();
  } else if (_notionSignInStep === 'waiting_continue') {
    await continueNotionSignIn();
  }
}

// 1. Launch browser (with pre-filled email)
async function startNotionSignIn() {
  const btn = document.getElementById('btn-notion-signin');
  const skipBtn = document.getElementById('btn-notion-skip');
  const status = document.getElementById('notion-signin-status');

  if (skipBtn) skipBtn.style.display = 'none';

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Opening browser…';
  status.textContent = '';
  status.className = 'notion-signin-status';

  const userEmail = val('f-email');

  try {
    const { ok, data } = await apiFetch('POST', '/api/auth/notion-ai/start', { email: userEmail });
    if (!ok) throw new Error(data?.error?.message || 'Failed to open browser.');

    _notionSignInStep = 'waiting_continue';
    btn.disabled = false;
    btn.className = 'btn btn-primary';
    btn.innerHTML = 'Continue →';

    const hint = document.getElementById('notion-signin-hint');
    if (hint) {
      hint.innerHTML = '<strong>Browser is open!</strong> Complete login in Notion and select your preferred AI Model, then click <strong>Continue →</strong>.';
    }
    status.textContent = userEmail
      ? `Opened login with ${userEmail}. Finish login and select AI model, then return here.`
      : 'Waiting for you to complete login in the popup window…';
    status.className = 'notion-signin-status';
  } catch (e) {
    btn.disabled = false;
    btn.className = 'btn btn-outline';
    btn.textContent = 'Sign in to Notion';
    status.textContent = e.message;
    status.className = 'notion-signin-status error';
  }
}

// 2. User finished login in browser and clicks Continue
async function continueNotionSignIn() {
  const btn = document.getElementById('btn-notion-signin');
  const status = document.getElementById('notion-signin-status');

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Saving login state…';

  try {
    const { ok, data } = await apiFetch('POST', '/api/auth/notion-ai/continue');
    if (!ok) throw new Error(data?.error?.message || 'Failed to save authentication.');

    _notionAuthComplete = true;
    _notionSignInStep = 'complete';

    btn.disabled = true;
    btn.className = 'btn btn-success';
    btn.textContent = '✓ Signed in';

    status.textContent = 'Authentication saved successfully!';
    status.className = 'notion-signin-status success';

    updateValidateButton();
  } catch (e) {
    btn.disabled = false;
    btn.className = 'btn btn-primary';
    btn.textContent = 'Continue →';
    status.textContent = e.message;
    status.className = 'notion-signin-status error';
  }
}

async function onSetupSubmit() {
  if (_setupRequestPending) return;

  // Clear errors
  ['token', 'emailTemplate', 'email', 'calendarTemplate'].forEach(clearFieldError);
  setFormError('');
  setProgress('');

  const token = val('f-token');
  const tokenSaved = document.getElementById('f-token')?.dataset.saved === 'true';
  const emailTemplate = val('f-emailTemplate');
  const email = val('f-email');
  let hasError = false;

  if (!token && !tokenSaved) { setFieldError('token', 'Notion Token is required.'); hasError = true; }
  else if (token && !token.startsWith('ntn_')) {
    setFieldError('token', 'Must start with ntn_. Check your token.'); hasError = true;
  }
  if (!emailTemplate) { setFieldError('emailTemplate', 'Email Template is required.'); hasError = true; }
  if (!email) { setFieldError('email', 'Email is required.'); hasError = true; }
  else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    setFieldError('email', 'Enter a valid Email.'); hasError = true;
  }

  if (hasError) {
    setFormError('Complete the required fields.');
    focusFirstError();
    return;
  }

  // Submit
  _setupRequestPending = true;
  const btn = document.getElementById('btn-validate');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Checking';

  const steps = [
    'Checking Notion Token…',
    'Checking Email Template…',
    'Checking Email…',
    val('f-calendarTemplate') ? 'Checking Calendar Template…' : null,
    'Starting workers…',
  ].filter(Boolean);

  let stepIdx = 0;
  setProgress(steps[stepIdx++]);
  const progressInterval = setInterval(() => {
    if (stepIdx < steps.length) setProgress(steps[stepIdx++]);
  }, 4000);

  try {
    const { ok, data } = await apiFetch('POST', '/api/setup/validate-and-start', {
      token,
      emailTemplate: val('f-emailTemplate'),
      email,
      calendarTemplate: val('f-calendarTemplate') || '',
    }, 90000);

    clearInterval(progressInterval);

    if (!ok) {
      const err = data?.error || {};
      const fe = err.fieldErrors || {};
      Object.entries(fe).forEach(([k, v]) => setFieldError(k, v));
      if (err.message) setFormError(err.message);
      else setFormError('Please correct the fields above.');
      focusFirstError();
      btn.innerHTML = 'Validate and start';
      btn.disabled = false;
      setProgress('');
      return;
    }

    // Success — navigate to Status
    setProgress('');
    navigate('status');
    applyWorkerStates(data?.data?.workers || []);

  } catch (e) {
    clearInterval(progressInterval);
    setFormError(e.message || 'MailAgent could not complete setup. Try again.');
    btn.innerHTML = 'Validate and start';
    btn.disabled = false;
    setProgress('');
  } finally {
    _setupRequestPending = false;
  }
}

function val(id) { return document.getElementById(id)?.value?.trim() || ''; }
function setFieldError(field, msg) {
  const errEl = document.getElementById(`err-${field}`);
  const inp = document.getElementById(`f-${field}`);
  if (errEl) { errEl.textContent = msg; errEl.classList.add('show'); }
  if (inp) inp.classList.add('error');
}
function clearFieldError(field) {
  const errEl = document.getElementById(`err-${field}`);
  const inp = document.getElementById(`f-${field}`);
  if (errEl) { errEl.textContent = ''; errEl.classList.remove('show'); }
  if (inp) inp.classList.remove('error');
}
function setFormError(msg) {
  const el = document.getElementById('form-error');
  el.textContent = msg;
  el.classList.toggle('show', !!msg);
}
function setProgress(msg) {
  document.getElementById('setup-progress').textContent = msg;
}
function focusFirstError() {
  const el = document.querySelector('.field-input.error');
  if (el) el.focus();
}

// ── Status ────────────────────────────────────────────────────────────────────
function startStatusPolling() {
  stopStatusPolling();
  pollStatus();
  _statusPollTimer = setInterval(pollStatus, 2000);
  document.addEventListener('visibilitychange', onVisibilityChange);
}
function stopStatusPolling() {
  clearInterval(_statusPollTimer);
  _statusPollTimer = null;
  document.removeEventListener('visibilitychange', onVisibilityChange);
}
function onVisibilityChange() {
  if (document.hidden) {
    stopStatusPolling();
  } else if (_currentView === 'status') {
    startStatusPolling();
  }
}

// ── Toast Notifications ───────────────────────────────────────────────────────
function showToast(message, type = 'info', duration = 5000) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <div class="toast-msg">${escHtml(message)}</div>
    <button class="toast-close" onclick="this.parentElement.remove()">✕</button>
  `;
  container.appendChild(toast);
  if (duration > 0) {
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.3s';
      setTimeout(() => toast.remove(), 300);
    }, duration);
  }
}

let _lastNotifiedErrors = {};

async function pollStatus() {
  try {
    const { ok, data } = await apiFetch('GET', '/api/runtime/status', null, 5000);
    if (!ok) return;
    const d = data?.data || {};
    const currentServiceStatus = d.serviceStatus || 'stopped';
    updateServiceState(currentServiceStatus);
    applyWorkerStates(d.workers || []);

    const forceSyncBtn = document.getElementById('btn-force-sync');
    if (forceSyncBtn) {
      if (currentServiceStatus === 'syncing') {
        forceSyncBtn.disabled = true;
        forceSyncBtn.textContent = 'Syncing…';
      } else {
        forceSyncBtn.disabled = false;
        forceSyncBtn.textContent = 'Force Sync';
      }
    }

    // Check for abnormal workers and alert with Toast
    let hasAbnormal = false;
    (d.workers || []).forEach(w => {
      if (w.status === 'abnormal') {
        hasAbnormal = true;
        const errKey = `${w.id}:${w.reason}`;
        if (!_lastNotifiedErrors[errKey]) {
          _lastNotifiedErrors[errKey] = Date.now();
          const label = WORKER_LABELS[w.id] || w.id;
          showToast(`⚠️ ${label} issue: ${w.reason || 'Abnormal status'}`, 'error', 7000);
        }
      }
    });

    // Show/hide "View logs" link in topbar depending on abnormal state
    const viewLogsBtn = document.getElementById('btn-view-logs');
    if (viewLogsBtn) {
      viewLogsBtn.style.display = hasAbnormal ? 'inline-block' : 'none';
    }
  } catch (e) { /* silent */ }
}

function updateServiceState(state) {
  const el = document.getElementById('service-state');
  const textEl = document.getElementById('service-state-text');
  const dot = document.getElementById('sidebar-dot');
  const sidebarText = document.getElementById('sidebar-status-text');
  if (!el) return;
  el.className = `service-state ${state}`;
  textEl.textContent = capitalize(state);
  if (dot) dot.className = `status-dot ${state}`;
  if (sidebarText) sidebarText.textContent = capitalize(state);
}

function capitalize(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

const WORKER_CONFIG = {
  supervisor: { label: 'Supervisor (Monitor)', icon: '🛡️' },
  mail: { label: 'Mail Worker', icon: '📬' },
  ai: { label: 'AI Worker', icon: '🤖' },
  calendar: { label: 'Calendar Worker', icon: '📅' },
};

function applyWorkerStates(workers) {
  const tbody = document.getElementById('processes-tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  for (const w of workers) {
    tbody.appendChild(buildProcessRow(w));
  }
}

function formatUptime(seconds) {
  if (!seconds || seconds <= 0) return '<span style="color:var(--muted)">—</span>';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function buildProcessRow(w) {
  const tr = document.createElement('tr');
  const cfg = WORKER_CONFIG[w.id] || { label: w.id, icon: '⚙️' };
  const badgeClass = w.status || 'off';
  const badgeText = capitalize(w.status || 'Off');

  let actionHtml = '<span style="color:var(--muted)">—</span>';
  if (w.status === 'abnormal' && w.id === 'ai' && (!w.reason || w.reason.includes('Sign-in'))) {
    actionHtml = `<button class="btn btn-outline btn-sm" onclick="goToAiSignIn()">Sign in</button>`;
  } else if (w.id !== 'supervisor' && w.status !== 'off') {
    actionHtml = `<button class="btn btn-outline btn-sm" onclick="restartWorker('${w.id}')">Restart</button>`;
  }

  const pidText = w.pid && w.pid > 0 ? `<span class="pid-badge">${w.pid}</span>` : `<span style="color:var(--muted)">—</span>`;
  const startedAtText = w.startedAtFormatted && w.startedAtFormatted !== '-'
    ? `<span style="font-size:12px; color:var(--ink); font-family:monospace;">${escHtml(w.startedAtFormatted)}</span>`
    : `<span style="color:var(--muted)">—</span>`;
  const uptimeText = formatUptime(w.uptimeSeconds);

  tr.innerHTML = `
    <td>
      <div class="process-name-cell">
        <span class="process-icon">${cfg.icon}</span>
        <span>${escHtml(cfg.label)}</span>
      </div>
    </td>
    <td>
      <span class="worker-badge ${badgeClass}">
        <span class="badge-dot"></span>${badgeText}
      </span>
    </td>
    <td>${pidText}</td>
    <td>${startedAtText}</td>
    <td><span style="font-size:12.5px; font-weight:500;">${uptimeText}</span></td>
    <td>
      <div class="process-task-text">${escHtml(w.task || (w.status === 'off' ? 'Disabled' : 'Idle'))}</div>
      ${w.reason ? `<div class="process-reason-text">${escHtml(w.reason)}</div>` : ''}
    </td>
    <td style="text-align: right;">
      ${actionHtml}
    </td>
  `;
  return tr;
}

async function restartWorker(id) {
  try {
    await apiFetch('POST', `/api/runtime/workers/${id}/restart`);
    showToast(`Restarting ${WORKER_CONFIG[id]?.label || id}…`, 'info');
    setTimeout(pollStatus, 300);
  } catch (e) { /* silent */ }
}

function goToAiSignIn() { navigate('settings'); /* Switch to AI tab */ activateSettingsTab('ai'); }

function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Settings ──────────────────────────────────────────────────────────────────
function initSettings() {
  // Tab switching
  document.querySelectorAll('.settings-tab').forEach(tab => {
    tab.addEventListener('click', () => activateSettingsTab(tab.dataset.tab));
  });

  // Save button
  document.getElementById('btn-settings-save').addEventListener('click', saveSettings);

  // Theme select change
  const themeSelect = document.getElementById('s-theme');
  if (themeSelect) {
    themeSelect.addEventListener('change', (e) => {
      applyTheme(e.target.value);
    });
  }

  // Toggle-dependent sub-sections
  bindToggle('s-llmAgentEnabled', 'llm-sub');
  bindToggle('s-feishuNotifyEnabled', 'feishu-sub');
  bindToggle('s-alertEnabled', 'alert-sub');
  bindToggle('s-redisEventsEnabled', 'redis-sub');

  // Re-auth button in Settings > AI
  let _reauthStep = 'initial';
  const reauthBtn = document.getElementById('btn-reauth-notion');
  if (reauthBtn) {
    reauthBtn.addEventListener('click', async () => {
      if (_reauthStep === 'initial') {
        reauthBtn.disabled = true;
        reauthBtn.innerHTML = '<span class="spinner"></span> Opening browser…';
        try {
          const { ok, data } = await apiFetch('POST', '/api/auth/notion-ai/start');
          if (!ok) throw new Error(data?.error?.message || 'Failed to open browser.');
          _reauthStep = 'waiting_continue';
          reauthBtn.disabled = false;
          reauthBtn.className = 'btn btn-primary';
          reauthBtn.textContent = 'Save & Complete →';
          showToast('Browser is open! Log in, pick your AI model, then click "Save & Complete".', 'info', 10000);
        } catch (e) {
          reauthBtn.disabled = false;
          reauthBtn.textContent = 'Re-authenticate';
          showToast(e.message, 'error');
        }
      } else if (_reauthStep === 'waiting_continue') {
        reauthBtn.disabled = true;
        reauthBtn.innerHTML = '<span class="spinner"></span> Saving…';
        try {
          const { ok, data } = await apiFetch('POST', '/api/auth/notion-ai/continue');
          if (!ok) throw new Error(data?.error?.message || 'Failed to save authentication.');
          _reauthStep = 'initial';
          reauthBtn.disabled = false;
          reauthBtn.className = 'btn btn-outline';
          reauthBtn.textContent = 'Re-authenticate';
          if (data?.data?.available_ai_models) {
            const avail = data.data.available_ai_models;
            const syncEl = document.getElementById('s-aiModelEmailSync');
            const dailyEl = document.getElementById('s-aiModelDailySummary');
            if (syncEl) populateModelDropdown(syncEl, avail, syncEl.value);
            if (dailyEl) populateModelDropdown(dailyEl, avail, dailyEl.value);
          }
          showToast('✅ Notion AI authenticated successfully!', 'success', 5000);
        } catch (e) {
          reauthBtn.disabled = false;
          reauthBtn.className = 'btn btn-primary';
          reauthBtn.textContent = 'Save & Complete →';
          showToast(e.message, 'error');
        }
      }
    });
  }

  // Sync Models button in Settings > AI
  const syncModelsBtn = document.getElementById('btn-sync-models');
  if (syncModelsBtn) {
    syncModelsBtn.addEventListener('click', async () => {
      const origText = syncModelsBtn.innerHTML;
      syncModelsBtn.disabled = true;
      syncModelsBtn.innerHTML = '<span class="spinner"></span> Syncing from Notion…';
      try {
        const { ok, data } = await apiFetch('POST', '/api/ai/sync-models', {}, 60000);
        if (!ok) {
          showToast('Sync failed: ' + (data?.error?.message || 'Unknown error'), 'error');
          return;
        }
        const avail = data?.data?.available_ai_models || [];
        const syncEl = document.getElementById('s-aiModelEmailSync');
        const dailyEl = document.getElementById('s-aiModelDailySummary');
        if (syncEl) populateModelDropdown(syncEl, avail, syncEl.value);
        if (dailyEl) populateModelDropdown(dailyEl, avail, dailyEl.value);
        showToast(`✅ Synced ${avail.length} models from Notion!`, 'success');
      } catch (e) {
        showToast('Sync failed: ' + e.message, 'error');
      } finally {
        syncModelsBtn.disabled = false;
        syncModelsBtn.innerHTML = origText;
      }
    });
  }

  // Listen for setting changes to toggle Save button enabled state & sync duplicate keys
  const settingsSection = document.getElementById('view-settings');
  if (settingsSection) {
    const handleSettingInput = (e) => {
      const target = e.target;
      if (target && target.dataset && target.dataset.key) {
        const key = target.dataset.key;
        const val = target.type === 'checkbox' ? target.checked : target.value;
        document.querySelectorAll(`#view-settings [data-key="${key}"]`).forEach(el => {
          if (el !== target) {
            if (el.type === 'checkbox') el.checked = val;
            else el.value = val;
          }
        });
      }
      updateSettingsDirtyState();
    };
    settingsSection.addEventListener('input', handleSettingInput);
    settingsSection.addEventListener('change', handleSettingInput);
  }

  // Initialize Logs viewer
  initLogsViewer();
}

// ── Logs tab in Settings ──────────────────────────────────────────────────────
let _logsPollTimer = null;
let _logsPaused = false;
let _rawLogLines = [];
let _logsCursor = -1;

function initLogsViewer() {
  const filterWorker = document.getElementById('log-filter-worker');
  const searchInput = document.getElementById('log-search');
  const pauseBtn = document.getElementById('btn-log-pause');
  const clearBtn = document.getElementById('btn-log-clear');

  if (filterWorker) filterWorker.addEventListener('change', renderLogs);
  if (searchInput) searchInput.addEventListener('input', renderLogs);
  if (pauseBtn) {
    pauseBtn.addEventListener('click', () => {
      _logsPaused = !_logsPaused;
      pauseBtn.textContent = _logsPaused ? 'Resume' : 'Pause';
      pauseBtn.className = _logsPaused ? 'btn btn-primary' : 'btn btn-outline';
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      _rawLogLines = [];
      renderLogs();
    });
  }
}

async function fetchLogs() {
  if (_logsPaused) return;
  try {
    const url = `/api/logs?cursor=${_logsCursor}&max_lines=250`;
    const { ok, data } = await apiFetch('GET', url, null, 5000);
    if (!ok || !data?.data) return;
    const { lines, cursor } = data.data;

    if (_logsCursor <= 0) {
      // First load: replace with tail lines
      _rawLogLines = lines || [];
    } else if (lines && lines.length > 0) {
      // Incremental: append new lines
      _rawLogLines.push(...lines);
      if (_rawLogLines.length > 1000) {
        _rawLogLines = _rawLogLines.slice(-1000);
      }
    }
    _logsCursor = cursor;
    renderLogs();
  } catch (e) { /* silent */ }
}

function renderLogs() {
  const container = document.getElementById('logs-output');
  if (!container) return;

  const workerFilter = (document.getElementById('log-filter-worker')?.value || '').toLowerCase();
  const searchFilter = (document.getElementById('log-search')?.value || '').toLowerCase();

  const filtered = _rawLogLines.filter(line => {
    if (workerFilter && !line.toLowerCase().includes(workerFilter)) return false;
    if (searchFilter && !line.toLowerCase().includes(searchFilter)) return false;
    return true;
  });

  if (filtered.length === 0) {
    container.innerHTML = '<span class="logs-empty">No matching log output.</span>';
    return;
  }

  // Prevent disrupting user's text selection
  const selection = window.getSelection();
  if (selection && selection.toString().length > 0 && container.contains(selection.anchorNode)) {
    return; // defer render until selection is cleared
  }

  const wasAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 40;

  container.innerHTML = filtered.map(line => {
    let levelClass = 'info';
    if (line.includes('| ERROR') || line.includes('| CRITICAL') || line.includes('crashed') || line.includes('Traceback')) {
      levelClass = 'error';
    } else if (line.includes('| WARNING') || line.includes('Warning')) {
      levelClass = 'warning';
    } else if (line.includes('| DEBUG')) {
      levelClass = 'debug';
    }
    return `<div class="log-line ${levelClass}">${escHtml(line)}</div>`;
  }).join('');

  if (wasAtBottom && !_logsPaused) {
    container.scrollTop = container.scrollHeight;
  }
}

function startLogsPolling() {
  stopLogsPolling();
  _logsCursor = -1; // Force tail on open
  fetchLogs();
  _logsPollTimer = setInterval(fetchLogs, 1000);
}

function stopLogsPolling() {
  if (_logsPollTimer) {
    clearInterval(_logsPollTimer);
    _logsPollTimer = null;
  }
}

function activateSettingsTab(tab) {
  document.querySelectorAll('.settings-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  document.querySelectorAll('.settings-panel').forEach(el => {
    el.classList.toggle('active', el.id === `stab-${tab}`);
  });

  // Hide settings save actions on Logs tab
  const headerActions = document.querySelector('.settings-header-actions');
  if (headerActions) {
    headerActions.style.display = tab === 'logs' ? 'none' : 'flex';
  }

  // Poll logs only when Logs tab is active
  if (tab === 'logs') {
    startLogsPolling();
  } else {
    stopLogsPolling();
  }
}

function bindToggle(checkId, subId) {
  const chk = document.getElementById(checkId);
  const sub = document.getElementById(subId);
  if (!chk || !sub) return;
  const update = () => sub.classList.toggle('hidden', !chk.checked);
  chk.addEventListener('change', update);
  update();
}

function populateModelDropdown(selectEl, availableModels, currentValue) {
  if (!selectEl) return;
  let models = [];
  if (Array.isArray(availableModels) && availableModels.length > 0) {
    // Strictly use real models discovered from Notion AI, ensuring 'Auto' is first
    const list = availableModels.filter(m => m && m.toLowerCase() !== 'auto');
    models = ['Auto', ...list];
    if (currentValue && !models.includes(currentValue)) {
      models.push(currentValue);
    }
  } else {
    // Fallback if not yet fetched from Notion
    models = Array.from(new Set(['Auto', currentValue].filter(Boolean)));
  }

  selectEl.innerHTML = '';
  models.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    if (m === currentValue) opt.selected = true;
    selectEl.appendChild(opt);
  });
  if (currentValue) selectEl.value = currentValue;
}

let _originalSettings = {};

function getSettingsFormValues() {
  const values = {};
  document.querySelectorAll('#view-settings [data-key]').forEach(el => {
    const key = el.dataset.key;
    if (el.type === 'checkbox') {
      values[key] = el.checked;
    } else if (el.type === 'number') {
      const n = parseFloat(el.value);
      values[key] = isNaN(n) ? '' : n;
    } else if (el.tagName === 'TEXTAREA') {
      values[key] = el.value ?? '';
    } else {
      values[key] = el.value ?? '';
    }
  });
  return values;
}

function updateSettingsDirtyState() {
  const btn = document.getElementById('btn-settings-save');
  if (!btn) return;
  const current = getSettingsFormValues();
  let dirty = false;
  for (const k in current) {
    if (k === 'notionToken' || k === 'llmApiKey' || k === 'feishu_app_secret') {
      if (typeof current[k] === 'string' && current[k].trim() !== '') {
        dirty = true;
        break;
      }
    } else if (JSON.stringify(current[k]) !== JSON.stringify(_originalSettings[k])) {
      dirty = true;
      break;
    }
  }
  btn.disabled = !dirty;
}

async function loadSettings() {
  try {
    const { ok, data } = await apiFetch('GET', '/api/settings', null, 10000);
    if (!ok) return;
    const cfg = data?.data || {};

    // Populate model dropdowns first
    const available = cfg.available_ai_models || [];
    populateModelDropdown(document.getElementById('s-aiModelEmailSync'), available, cfg.ai_model_email_sync || 'Auto');
    populateModelDropdown(document.getElementById('s-aiModelDailySummary'), available, cfg.ai_model_daily_summary || 'Auto');

    // Populate all inputs and textareas
    document.querySelectorAll('#view-settings [data-key]').forEach(el => {
      const key = el.dataset.key;
      if (el.type === 'checkbox') {
        el.checked = !!cfg[key];
      } else if (el.tagName === 'SELECT') {
        if (cfg[key] !== undefined) el.value = cfg[key];
      } else if (el.tagName === 'TEXTAREA') {
        el.value = cfg[key] ?? '';
      } else {
        // Skip secret placeholders
        if (key === 'notionToken' || key === 'llmApiKey' || key === 'feishu_app_secret') return;
        el.value = cfg[key] ?? '';
      }
    });

    // Update sub-section visibility
    ['s-llmAgentEnabled', 's-feishuNotifyEnabled', 's-alertEnabled', 's-redisEventsEnabled'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.dispatchEvent(new Event('change'));
    });

    // Save baseline for dirty check
    _originalSettings = getSettingsFormValues();
    const saveBtn = document.getElementById('btn-settings-save');
    if (saveBtn) saveBtn.disabled = true;
  } catch (e) { /* silent */ }
}

async function saveSettings() {
  const btn = document.getElementById('btn-settings-save');
  const statusEl = document.getElementById('settings-save-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Saving';
  statusEl.textContent = '';

  const payload = {};
  document.querySelectorAll('#view-settings [data-key]').forEach(el => {
    const key = el.dataset.key;
    if (el.type === 'checkbox') {
      payload[key] = el.checked;
    } else if (el.type === 'number') {
      const n = parseFloat(el.value);
      if (!isNaN(n)) payload[key] = n;
    } else if (el.tagName === 'TEXTAREA') {
      payload[key] = el.value;
    } else if (el.value !== undefined) {
      // Only include secret fields if they have a value (non-empty replacement)
      if ((key === 'notionToken' || key === 'llmApiKey') && !el.value.trim()) return;
      payload[key] = el.value;
    }
  });

  try {
    const { ok, data } = await apiFetch('PUT', '/api/settings', payload, 30000);
    if (!ok) {
      const msg = data?.error?.message || 'Save failed.';
      statusEl.textContent = msg;
      statusEl.style.color = 'var(--danger)';
      btn.disabled = false;
    } else {
      statusEl.textContent = 'Saved';
      statusEl.style.color = 'var(--success)';
      setTimeout(() => { statusEl.textContent = ''; }, 3000);
      _originalSettings = getSettingsFormValues();
      btn.disabled = true;
    }
  } catch (e) {
    statusEl.textContent = e.message || 'Save failed.';
    statusEl.style.color = 'var(--danger)';
    btn.disabled = false;
  } finally {
    btn.textContent = 'Save';
  }
}

// ── Nav item clicks ───────────────────────────────────────────────────────────
document.querySelectorAll('.nav-item[data-view]').forEach(btn => {
  btn.addEventListener('click', () => navigate(btn.dataset.view));
});

// ── Theme (Light / Dark) ──────────────────────────────────────────────────────
function applyTheme(theme) {
  const isDark = theme === 'dark';
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  localStorage.setItem('mailagent_theme', isDark ? 'dark' : 'light');

  const toggleBtn = document.getElementById('btn-theme-toggle');
  if (toggleBtn) {
    toggleBtn.textContent = isDark ? '☀️' : '🌙';
    toggleBtn.title = isDark ? 'Switch to Light mode' : 'Switch to Dark mode';
  }

  const select = document.getElementById('s-theme');
  if (select && select.value !== (isDark ? 'dark' : 'light')) {
    select.value = isDark ? 'dark' : 'light';
  }
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'light';
  const next = current === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  apiFetch('PUT', '/api/settings', { theme: next }).catch(() => {});
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  // Initialize theme from cache immediately
  const initialTheme = localStorage.getItem('mailagent_theme') || 'light';
  applyTheme(initialTheme);

  const themeToggleBtn = document.getElementById('btn-theme-toggle');
  if (themeToggleBtn) {
    themeToggleBtn.addEventListener('click', toggleTheme);
  }

  const restartAllBtn = document.getElementById('btn-restart-all');
  if (restartAllBtn) {
    restartAllBtn.addEventListener('click', async () => {
      restartAllBtn.disabled = true;
      restartAllBtn.textContent = 'Restarting…';
      try {
        await Promise.all([
          apiFetch('POST', '/api/runtime/workers/mail/restart'),
          apiFetch('POST', '/api/runtime/workers/ai/restart')
        ]);
        showToast('Restarting all worker processes…', 'info');
      } catch (e) {
        showToast('Restart failed: ' + e.message, 'error');
      } finally {
        setTimeout(() => {
          restartAllBtn.disabled = false;
          restartAllBtn.textContent = 'Restart All';
        }, 2500);
      }
    });
  }

  const forceSyncBtn = document.getElementById('btn-force-sync');
  if (forceSyncBtn) {
    forceSyncBtn.addEventListener('click', async () => {
      forceSyncBtn.disabled = true;
      forceSyncBtn.textContent = 'Syncing…';
      try {
        const res = await apiFetch('POST', '/api/runtime/force_sync');
        showToast(res.message || 'Force sync initiated.', 'info');
      } catch (e) {
        showToast('Force sync failed: ' + e.message, 'error');
        forceSyncBtn.disabled = false;
        forceSyncBtn.textContent = 'Force Sync';
      }
    });
  }

  await fetchCsrfToken();
  initSetup();
  initSettings();

  // 1. Fetch settings to determine setup state & prefill
  try {
    const { ok, data } = await apiFetch('GET', '/api/settings', null, 5000);
    if (ok) {
      const cfg = data?.data || {};
      if (cfg.theme) {
        applyTheme(cfg.theme);
      }
      // If setup is marked complete OR if email & template are already configured:
      if (cfg.setup_complete || (cfg.email && cfg.email_template_id)) {
        navigate('status');
        return;
      } else {
        // Prefill setup form with previously entered values
        if (cfg.email) {
          const el = document.getElementById('f-email');
          if (el && !el.value) el.value = cfg.email;
        }
        if (cfg.email_template_id) {
          const el = document.getElementById('f-emailTemplate');
          if (el && !el.value) el.value = cfg.email_template_id;
        }
        if (cfg.calendar_template_id) {
          const el = document.getElementById('f-calendarTemplate');
          if (el && !el.value) el.value = cfg.calendar_template_id;
        }
        if (cfg.notionTokenPresent) {
          const el = document.getElementById('f-token');
          if (el) {
            el.placeholder = '•••••••••••••••• (Saved in DPAPI store)';
            el.dataset.saved = 'true';
          }
        }
      }
    }
  } catch (e) { /* continue */ }

  // 2. Check runtime status as fallback
  try {
    const { ok, data } = await apiFetch('GET', '/api/runtime/status', null, 5000);
    if (ok && data?.data?.serviceStatus && data?.data?.serviceStatus !== 'stopped') {
      navigate('status');
      return;
    }
  } catch (e) { /* continue */ }

  // 3. Not configured — show setup view
  navigate('setup');
}

init().catch(console.error);
