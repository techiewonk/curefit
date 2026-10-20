/* Shared helpers for the console pages. */
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const inr = n => n == null ? '-' : '₹' + Number(n).toLocaleString('en-IN');
const num = n => n == null ? '-' : Number(n).toLocaleString('en-IN');
const when = s => s ? String(s).replace('T', ' ').slice(0, 16) : '';
const hours = sec => sec < 90 ? Math.round(sec) + ' s' : sec < 5400 ? Math.round(sec / 60) + ' min'
  : (sec / 3600).toFixed(1) + ' h';
const API = '/admin/console/api/';

function csrf() {
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}
async function api(path, opts) {
  const r = await fetch(API + path, opts);
  const j = await r.json().catch(() => ({error: 'Bad response'}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
const post = (path, body) => api(path, {
  method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf()},
  body: JSON.stringify(body || {})});
const q = obj => Object.entries(obj).filter(([, v]) => v !== '' && v != null)
  .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');

function toast(msg, bad) {
  const t = $('toast'); if (!t) return;
  t.textContent = msg; t.style.borderColor = bad ? '#dc2626' : '#888'; t.style.display = 'block';
  clearTimeout(toast.h); toast.h = setTimeout(() => t.style.display = 'none', 5000);
}
const guard = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };
