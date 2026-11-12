// PL Hunter capture panel — capture-only. No pipeline logic, no results
// table, no live Amazon calls. POSTs /api/capture to the backend with the
// token stored in chrome.storage.local.

const DEFAULT_ORIGIN = 'http://localhost:8000';
const ASIN_RE = /\b([A-Z0-9]{10})\b/i;

const input = document.getElementById('asin-input');
const statusEl = document.getElementById('status');
const dashboardLink = document.getElementById('dashboard-link');
const tokenInput = document.getElementById('token-input');
const originInput = document.getElementById('origin-input');

function showStatus(msg, kind) {
  statusEl.hidden = false;
  statusEl.textContent = msg;
  statusEl.className = 'status' + (kind ? ' ' + kind : '');
}

async function getSettings() {
  const stored = await chrome.storage.local.get(['plh_token', 'plh_origin']);
  return {
    token: stored.plh_token || '',
    origin: (stored.plh_origin || DEFAULT_ORIGIN).replace(/\/$/, ''),
  };
}

function extractAsinOrUrl(raw) {
  const value = raw.trim();
  if (!value) return null;
  if (/^https?:\/\//i.test(value)) return { url: value };
  const m = value.match(ASIN_RE);
  if (m) return { asin: m[1].toUpperCase() };
  return null;
}

// Pre-fill from the active tab when it is an Amazon product page.
chrome.tabs?.query({ active: true, currentWindow: true }, (tabs) => {
  const url = tabs && tabs[0] && tabs[0].url;
  if (url && /amazon\./i.test(url)) input.value = url;
});

document.getElementById('capture-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const payload = extractAsinOrUrl(input.value);
  if (!payload) {
    showStatus('Enter a 10-character ASIN or an Amazon product URL.', 'error');
    return;
  }
  const { token, origin } = await getSettings();
  if (!token) {
    showStatus('No API token saved. Add it under Settings below.', 'error');
    return;
  }
  const btn = document.getElementById('capture-btn');
  btn.disabled = true;
  showStatus('Capturing…');
  dashboardLink.hidden = true;
  try {
    const res = await fetch(`${origin}/api/capture`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Token': token },
      body: JSON.stringify(payload),
    });
    if (res.status === 401) {
      showStatus('Unauthorized — the saved API token is missing or wrong. Update it under Settings.', 'error');
      return;
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || body.error || detail;
      } catch (_) { /* keep statusText */ }
      showStatus(`Capture failed (${res.status}): ${detail}`, 'error');
      return;
    }
    const data = await res.json();
    showStatus(`Captured. Audit job #${data.job_id} created.`, 'ok');
    const link = data.deep_link || `${origin}/`;
    dashboardLink.href = link;
    dashboardLink.hidden = false;
  } catch (err) {
    showStatus(`Could not reach the backend at ${origin}. Is the server running? (${err.message})`, 'error');
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('save-token-btn').addEventListener('click', async () => {
  await chrome.storage.local.set({ plh_token: tokenInput.value.trim() });
  showStatus('Token saved in extension storage.', 'ok');
});

document.getElementById('save-origin-btn').addEventListener('click', async () => {
  const origin = originInput.value.trim().replace(/\/$/, '') || DEFAULT_ORIGIN;
  await chrome.storage.local.set({ plh_origin: origin });
  dashboardLink.href = `${origin}/`;
  showStatus(`Backend origin saved: ${origin}`, 'ok');
});

// Load saved settings into the panel.
getSettings().then(({ token, origin }) => {
  tokenInput.value = token;
  originInput.value = origin;
  dashboardLink.href = `${origin}/`;
});
