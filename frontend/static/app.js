'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let lastSession = null;   // { date, session } of most recent capture
let modalSession = null;  // session currently open in modal
let modalKind = 'anaglyph';

// ── Status polling ─────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const res = await fetch('/status');
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    const dot  = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    if (data.cameras_ready) {
      dot.className  = 'status-dot ready';
      text.textContent = 'Ready';
    } else {
      dot.className  = 'status-dot';
      text.textContent = 'Cameras warming up…';
    }
  } catch {
    const dot = document.getElementById('status-dot');
    dot.className  = 'status-dot error';
    document.getElementById('status-text').textContent = 'Offline';
  }
}

// ── Capture ────────────────────────────────────────────────────────────────
async function triggerCapture() {
  const btn    = document.getElementById('capture-btn');
  const status = document.getElementById('capture-status');

  btn.disabled = true;
  btn.textContent = '● Capturing…';
  status.className = 'capture-status';
  status.textContent = 'Sending shutter signal…';

  try {
    const res  = await fetch('/capture', { method: 'POST' });
    const data = await res.json();

    if (data.status === 'ok') {
      const c = data.capture;
      lastSession = { date: c.date, session: c.session };
      status.className = 'capture-status ok';
      status.textContent = `Saved: ${c.date} / ${c.session}`;
      showAnaglyph(c.date, c.session, c.timestamp);
      loadGallery();
    } else {
      throw new Error(data.message || 'Unknown error');
    }
  } catch (err) {
    status.className = 'capture-status error';
    status.textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span class="capture-icon">⬤</span> Capture Stereo';
  }
}

// ── Anaglyph preview ───────────────────────────────────────────────────────
function showAnaglyph(date, session, timestamp) {
  const card = document.getElementById('anaglyph-card');
  const img  = document.getElementById('anaglyph-img');
  const meta = document.getElementById('anaglyph-meta');

  const url = `/images/${date}/${session}/anaglyph?t=${Date.now()}`;
  img.src = url;
  meta.textContent = `${date}  ·  ${session}  ·  ${formatTs(timestamp)}`;
  card.style.display = '';
}

async function reprocessLast() {
  if (!lastSession) return;
  const method = document.getElementById('method-select').value;
  try {
    const res = await fetch('/reprocess', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...lastSession, method }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      showAnaglyph(lastSession.date, lastSession.session, new Date().toISOString());
    }
  } catch (err) {
    alert('Reprocess failed: ' + err.message);
  }
}

// ── Gallery ────────────────────────────────────────────────────────────────
async function loadGallery() {
  const grid = document.getElementById('gallery-grid');
  try {
    const res  = await fetch('/captures');
    const list = await res.json();

    if (list.length === 0) {
      grid.innerHTML = '<div class="gallery-empty">No captures yet.</div>';
      return;
    }

    grid.innerHTML = list.map(s => `
      <div class="gallery-item" onclick="openModal('${s.date}','${s.session}','${s.timestamp}')">
        <img src="/images/${s.date}/${s.session}/anaglyph" loading="lazy" alt="">
        <div class="gallery-item-label">${s.date}<br>${s.session}</div>
      </div>
    `).join('');
  } catch {
    grid.innerHTML = '<div class="gallery-empty">Failed to load gallery.</div>';
  }
}

// ── Modal ──────────────────────────────────────────────────────────────────
function openModal(date, session, timestamp) {
  modalSession = { date, session };
  modalKind = 'anaglyph';
  document.getElementById('modal-meta').textContent = `${date}  ·  ${session}  ·  ${formatTs(timestamp)}`;
  updateModalImage();
  document.getElementById('modal').classList.add('open');
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
  modalSession = null;
}

function showModalKind(kind) {
  modalKind = kind;
  updateModalImage();
}

function updateModalImage() {
  if (!modalSession) return;
  const { date, session } = modalSession;
  const url = `/images/${date}/${session}/${modalKind}?t=${Date.now()}`;
  document.getElementById('modal-img').src = url;
  document.getElementById('modal-dl').href = url;
  document.getElementById('modal-dl').download = `${session}_${modalKind}.jpg`;

  document.querySelectorAll('.modal-tabs button').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.toLowerCase().includes(modalKind));
  });
}

// ── Helpers ────────────────────────────────────────────────────────────────
function formatTs(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return iso;
  }
}

// ── Boot ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  pollStatus();
  setInterval(pollStatus, 5000);
  loadGallery();

  // Restore stream src on error (network blip reconnect)
  ['left-stream', 'right-stream'].forEach(id => {
    const img = document.getElementById(id);
    img.addEventListener('error', () => {
      setTimeout(() => { img.src = img.src.split('?')[0] + '?t=' + Date.now(); }, 2000);
    });
  });
});
