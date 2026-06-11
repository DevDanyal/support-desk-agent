const API = {
  async get(url) { const r = await fetch(url); if (!r.ok) throw new Error(`GET ${url} failed`); return r.json(); },
  async post(url, body) { const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) }); if (!r.ok) throw new Error(`POST ${url} failed`); return r.json(); },
  async patch(url, body) { const r = await fetch(url, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) }); if (!r.ok) throw new Error(`PATCH ${url} failed`); return r.json(); },
  async del(url) { const r = await fetch(url, { method:'DELETE' }); if (!r.ok) throw new Error(`DELETE ${url} failed`); return r.json(); },
};

const $ = (sel, ctx) => (ctx || document).querySelector(sel);
const $$ = (sel, ctx) => [...(ctx || document).querySelectorAll(sel)];

function uid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random()*16|0; return (c==='x'?r:(r&0x3|0x8)).toString(16); });
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function animateCount(el, target, duration = 600) {
  const start = performance.now();
  const from = 0;
  function tick(now) {
    const p = Math.min((now - start) / duration, 1);
    const ease = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(from + (target - from) * ease);
    if (p < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function staggerAppend(parent, items, createFn, delay = 40) {
  items.forEach((item, i) => {
    const el = createFn(item);
    el.style.opacity = '0';
    el.style.transform = 'translateY(8px)';
    el.style.transition = 'all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)';
    parent.appendChild(el);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        el.style.opacity = '1';
        el.style.transform = 'translateY(0)';
      });
    });
  });
}

function toast(msg, type) {
  type = type || 'info';
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  $('#toastContainer').appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transform = 'translateX(28px) scale(0.95)';
    el.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)';
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

function openModal(id) { $(`#${id}`).classList.add('active'); }
function closeModal(id) { $(`#${id}`).classList.remove('active'); }

$$('[data-modal]').forEach(b => b.addEventListener('click', () => closeModal(b.dataset.modal)));
$$('.modal-overlay').forEach(o => o.addEventListener('click', e => { if (e.target === o) closeModal(o.id); }));

// ===== AVATAR UTILITY =====
const AVATAR_COLORS = [
  ['#5b8cff','#3b6fdf'], ['#22d68b','#18b875'], ['#ff9447','#e67320'],
  ['#7c5cff','#5c3cdf'], ['#ff4760','#df273f'], ['#ffd647','#dfb627'],
  ['#ff6b9d','#df4b7d'], ['#00d4aa','#00b48a'], ['#a78bfa','#876bda'],
  ['#f472b6','#d45296'],
];

function getAvatarColor(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function getInitials(name) {
  return name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
}

// ===== DEBOUNCE =====
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ===== LOADING STATE =====
function setLoading(id, loading) {
  const el = $(`#${id}`);
  if (!el) return;
  el.classList.toggle('active', loading);
}

// ===== STATE =====
const state = {
  convId: null,
  conversations: [],
  currentConvIdx: -1,
  stats: null,
  orders: [],
  tickets: [],
  escalations: [],
  customers: [],
  policies: {},
  loading: false,
};

// ===== NAVIGATION =====
let currentPage = 'chat';

function navigate(page) {
  currentPage = page;
  $$('.page').forEach(p => p.classList.remove('active'));
  const el = $(`#page${page.charAt(0).toUpperCase() + page.slice(1)}`);
  if (el) el.classList.add('active');
  $$('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.page === page));
  $('#sidebar').classList.remove('open');
  $('#sidebarOverlay').classList.remove('active');
  if (page !== 'chat') loadPage(page);
}

// Sidebar nav
$$('.nav-item[data-page]').forEach(item => {
  item.addEventListener('click', () => navigate(item.dataset.page));
});

// Overlay
$('#sidebarOverlay')?.addEventListener('click', () => {
  $('#sidebar').classList.remove('open');
  $('#sidebarOverlay').classList.remove('active');
});

// Mobile menu
$('#menuBtn')?.addEventListener('click', () => {
  $('#sidebar').classList.toggle('open');
  $('#sidebarOverlay').classList.toggle('active');
});

function loadPage(page) {
  const f = { dashboard: loadDashboard, orders: loadOrders, tickets: loadTickets, escalations: loadEscalations, customers: loadCustomers, policies: loadPolicies };
  if (f[page]) f[page]();
}

// ===== CHAT =====
const chatInput = $('#chatInput');
const sendBtn = $('#sendBtn');
const chatMessages = $('#chatMessages');
const micBtn = $('#micBtn');

function autoResize() {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
}

chatInput.addEventListener('input', () => { autoResize(); sendBtn.disabled = !chatInput.value.trim() || state.loading; });
chatInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
sendBtn.addEventListener('click', send);
chatMessages.addEventListener('click', e => {
  const chip = e.target.closest('.chip');
  if (chip) { chatInput.value = chip.dataset.msg; autoResize(); send(); }
});

// New chat button
$('#newChatBtnSm')?.addEventListener('click', newChat);
$('#clearChatBtn')?.addEventListener('click', () => {
  if (state.currentConvIdx < 0 && !$('.msg')) { toast('No conversation to clear', 'info'); return; }
  newChat();
  toast('Conversation cleared', 'info');
});

function newChat() {
  state.convId = null;
  chatInput.value = '';
  chatInput.style.height = 'auto';
  sendBtn.disabled = true;
  $$('.msg').forEach(m => m.remove());
  $('.thinking')?.remove();
  showWelcome();
  navigate('chat');
}

// ===== VOICE =====
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let isListening = false;

if (SpeechRecognition) {
  recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.lang = 'en-US';
  recognition.onresult = function(e) {
    chatInput.value = e.results[0][0].transcript;
    autoResize(); sendBtn.disabled = false;
    micBtn.classList.remove('recording'); isListening = false;
    send();
  };
  recognition.onerror = function() {
    micBtn.classList.remove('recording'); isListening = false;
    toast('Voice input failed', 'error');
  };
  recognition.onend = function() {
    micBtn.classList.remove('recording'); isListening = false;
  };
}

micBtn?.addEventListener('click', () => {
  if (!recognition) { toast('Voice not supported in this browser', 'info'); return; }
  if (isListening) { recognition.stop(); micBtn.classList.remove('recording'); isListening = false; return; }
  try { recognition.start(); micBtn.classList.add('recording'); isListening = true; } catch { toast('Voice unavailable', 'error'); }
});

// ===== SEND =====
async function send() {
  const text = chatInput.value.trim();
  if (!text || state.loading) return;

  if (!state.convId) {
    state.conversations.unshift({ id: uid(), label: text.slice(0, 50) + (text.length > 50 ? '...' : ''), messages: [] });
    state.currentConvIdx = 0;
    state.convId = state.conversations[0].id;
  }

  state.loading = true;
  sendBtn.disabled = true;
  chatInput.value = '';
  chatInput.style.height = 'auto';
  $('.welcome')?.remove();

  const uTime = now();
  pushMsg(text, 'user', uTime);
  scrollChat();
  showThinking();

  try {
    const res = await API.post('/api/chat', { message: text, conversation_id: state.convId });
    state.convId = res.conversation_id;
    if (state.currentConvIdx >= 0) state.conversations[state.currentConvIdx].id = res.conversation_id;
    hideThinking();

    if (res.tool_calls && res.tool_calls.length) {
      for (const tc of res.tool_calls) {
        pushMsg(`🔧 Used ${tc.tool} → ${tc.result}`, 'tool', now());
        scrollChat();
      }
    }

    const aTime = now();
    pushMsg(res.reply, 'assistant', aTime);
    scrollChat();

    if (state.currentConvIdx >= 0) {
      const first = state.conversations[state.currentConvIdx].messages[0];
      state.conversations[state.currentConvIdx].label = first ? (first.text.slice(0, 50) + (first.text.length > 50 ? '...' : '')) : 'Chat';
    }
  } catch {
    hideThinking();
    pushMsg('Sorry, I encountered an error connecting to the server. Please try again.', 'assistant');
  } finally {
    state.loading = false;
    sendBtn.disabled = true;
    chatInput.focus();
  }
}

function escapeMsg(text) {
  return esc(text).replace(/\n/g, '<br>');
}

function pushMsg(text, role, time) {
  const d = document.createElement('div');
  d.className = `msg ${role}`;
  const t = time || now();
  const avatar = role === 'assistant' ? 'AI' : role === 'user' ? 'U' : '';
  const extra = role === 'tool' ? '' : `<div class="msg-time">${t}</div>`;
  d.innerHTML = role === 'tool'
    ? `<div class="msg-body"><div class="msg-bubble">${esc(text)}</div></div>`
    : `<div class="msg-avatar">${avatar}</div><div class="msg-body"><div class="msg-bubble">${escapeMsg(text)}</div>${extra}</div>`;
  chatMessages.appendChild(d);

  if (state.currentConvIdx >= 0) {
    state.conversations[state.currentConvIdx].messages.push({ role, text, time: t });
  }
}

function showThinking() {
  const el = document.createElement('div');
  el.className = 'thinking visible';
  el.innerHTML = '<div class="thinking-dots"><span></span><span></span><span></span></div>';
  const last = chatMessages.lastElementChild;
  if (last) last.after(el); else chatMessages.appendChild(el);
  scrollChat();
}

function hideThinking() { $('.thinking')?.remove(); }
function scrollChat() { chatMessages.scrollTop = chatMessages.scrollHeight; }
function now() { return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }

function showWelcome() {
  if ($('.welcome')) return;
  const w = document.createElement('div');
  w.className = 'welcome';
  w.innerHTML = `
    <div class="welcome-icon">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/></svg>
    </div>
    <h1>How can I help you today?</h1>
    <p>Ask me about orders, support tickets, return policies, or escalate issues to a human agent.</p>
    <div class="suggestion-chips">
      <button class="chip" data-msg="What is the status of order ORD-1001?"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/></svg> Track Order</button>
      <button class="chip" data-msg="What is the return policy for electronics?"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg> Return Policy</button>
      <button class="chip" data-msg="Check the status of support ticket TKT-5001"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg> Check Ticket</button>
      <button class="chip" data-msg="I need to escalate an issue about a damaged product"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/></svg> Escalate Issue</button>
    </div>`;
  chatMessages.appendChild(w);
}

// ===== DASHBOARD =====
async function loadDashboard() {
  setLoading('dashboardLoading', true);
  try {
    state.stats = await API.get('/api/stats');
    animateCount($('#statOrders'), state.stats.total_orders);
    animateCount($('#statTickets'), state.stats.total_tickets);
    animateCount($('#statCustomers'), state.stats.total_customers);
    $('#statRevenue').textContent = `$${Math.round(state.stats.revenue)}`;
    animateCount($('#statEscalations'), state.stats.pending_escalations);
    const pendingOrders = (state.stats.order_statuses.processing || 0) + (state.stats.order_statuses.shipped || 0);
    animateCount($('#statPendingOrders'), pendingOrders);
    $('#ordersBadge').textContent = state.stats.total_orders;
    $('#ticketsBadge').textContent = state.stats.total_tickets;
    $('#escBadge').textContent = state.stats.pending_escalations;

    const oCount = state.stats.total_orders;
    const tCount = state.stats.total_tickets;
    $('#orderBadge').textContent = `${oCount} order${oCount !== 1 ? 's' : ''}`;
    $('#ticketBadge').textContent = `${tCount} ticket${tCount !== 1 ? 's' : ''}`;
    const urgentCount = state.stats.priority_counts.urgent || 0;
    $('#priorityBadge').textContent = `${urgentCount} urgent`;

    renderChart('orderChart', state.stats.order_statuses, { processing: '#6c63ff', shipped: '#8880ff', delivered: '#10d48e', cancelled: '#ff5470' });
    renderChart('ticketChart', state.stats.ticket_statuses, { open: '#ff8a50', in_progress: '#6c63ff', resolved: '#10d48e' });
    renderPriorityChart(state.stats.priority_counts);
    renderActivity(state.stats);
  } catch { toast('Failed to load dashboard', 'error'); }
  finally { setLoading('dashboardLoading', false); }
}

$('#refreshDashboardBtn')?.addEventListener('click', () => { loadDashboard(); toast('Dashboard refreshed', 'success'); });

// Dashboard search
$('#dashboardSearch')?.addEventListener('input', debounce(function() {
  const q = this.value.toLowerCase();
  $$('.stat-card').forEach(card => {
    const text = card.textContent.toLowerCase();
    card.style.display = text.includes(q) ? 'flex' : 'none';
  });
}, 200));

function renderChart(id, data, colors) {
  const c = $(`#${id}`);
  const entries = Object.entries(data);
  const max = Math.max(...Object.values(data), 1);
  if (!entries.length) { c.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:20px">No data</div>'; return; }
  c.innerHTML = entries.map(([k, v]) =>
    `<div class="chart-bar-wrap">
      <span class="chart-bar-val">${v}</span>
      <div class="chart-bar" style="height:${(v / max) * 100}%;background:${colors[k] || '#5b8cff'}"></div>
      <span class="chart-bar-label">${k.replace(/_/g, ' ')}</span>
    </div>`
  ).join('');
}

function renderPriorityChart(p) {
  const c = $('#priorityChart');
  const colors = { urgent: '#ff5470', high: '#ff8a50', medium: '#ffcd4a', low: '#6c63ff' };
  const total = Object.values(p).reduce((a, b) => a + b, 0) || 1;
  const entries = Object.entries(p);
  if (!entries.length) { c.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:20px">No data</div>'; return; }
  c.innerHTML = `<div class="priority-list">${entries.map(([k, v]) =>
    `<div class="prio-item">
      <span class="prio-dot" style="background:${colors[k]}"></span>
      <span class="prio-name">${k}</span>
      <div class="prio-track"><div class="prio-bar" style="width:${(v / total) * 100}%;background:${colors[k]}"></div></div>
      <span class="prio-count">${v}</span>
    </div>`
  ).join('')}</div>`;
}

function renderActivity(stats) {
  const feed = $('#activityFeed');
  const activities = [
    ...Object.entries(stats.order_statuses).map(([k, v]) => ({ icon: '📦', text: `<strong>${v}</strong> order(s) are <strong>${k}</strong>`, time: 'now' })),
    ...Object.entries(stats.ticket_statuses).map(([k, v]) => ({ icon: '🎫', text: `<strong>${v}</strong> ticket(s) are <strong>${k.replace('_', ' ')}</strong>`, time: 'now' })),
  ];
  if (stats.pending_escalations > 0) activities.unshift({ icon: '⚠️', text: `<strong>${stats.pending_escalations}</strong> escalation(s) pending`, time: 'now' });
  if (!activities.length) { feed.innerHTML = '<div class="activity-empty">No recent activity</div>'; return; }
  feed.innerHTML = activities.map(a =>
    `<div class="activity-item"><div class="activity-icon">${a.icon}</div><span class="activity-text">${a.text}</span><span class="activity-time">${a.time}</span></div>`
  ).join('');
}

// ===== ORDERS =====
let ordersFiltered = [];

async function loadOrders() {
  setLoading('ordersLoading', true);
  try {
    state.orders = await API.get('/api/orders');
    applyOrderFilters();
  } catch { toast('Failed to load orders', 'error'); }
  finally { setLoading('ordersLoading', false); }
}

function applyOrderFilters() {
  let f = [...state.orders];
  const sf = $('#orderFilter').value;
  const sq = $('#orderSearch').value.toLowerCase();
  if (sf !== 'all') f = f.filter(o => o.status === sf);
  if (sq) f = f.filter(o => o.id.toLowerCase().includes(sq) || o.customer.toLowerCase().includes(sq) || o.items.toLowerCase().includes(sq));
  ordersFiltered = f;
  renderOrders(f);
}

function renderOrders(orders) {
  const body = $('#ordersBody');
  const empty = $('#ordersEmpty');
  if (!orders.length) { body.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  body.innerHTML = orders.map(o =>
    `<tr>
      <td><strong style="color:var(--text);font-weight:600">${esc(o.id)}</strong></td>
      <td>${esc(o.customer)}</td>
      <td>${esc(o.items)}</td>
      <td><span style="font-weight:600;color:var(--text)">$${o.total.toFixed(2)}</span></td>
      <td><span class="status-badge ${esc(o.status)}">${esc(o.status)}</span></td>
      <td>${o.date}</td>
      <td>${o.eta}</td>
      <td>
        <div class="table-actions">
          <button class="table-action-btn danger" onclick="deleteOrder('${esc(o.id)}')" title="Delete">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      </td>
    </tr>`
  ).join('');
}

window.deleteOrder = async function(id) {
  if (!confirm(`Delete order ${id}?`)) return;
  try { await API.del(`/api/orders/${id}`); toast(`Order ${id} deleted`, 'success'); loadOrders(); } catch { toast('Failed to delete order', 'error'); }
};

$('#orderFilter').onchange = applyOrderFilters;
$('#orderSearch').addEventListener('input', debounce(applyOrderFilters, 200));

$('#addOrderBtn').onclick = () => {
  $('#orderCustomer').value = '';
  $('#orderItems').value = '';
  $('#orderTotal').value = '';
  openModal('orderModal');
};

$('#saveOrderBtn').onclick = async () => {
  const customer = $('#orderCustomer').value.trim();
  const items = $('#orderItems').value.trim();
  const total = parseFloat($('#orderTotal').value);
  if (!customer || !items || isNaN(total)) { toast('Please fill all fields', 'error'); return; }
  try {
    await API.post('/api/orders', { customer, items, total });
    toast('Order created successfully', 'success');
    closeModal('orderModal');
    loadOrders();
  } catch { toast('Failed to create order', 'error'); }
};

// ===== TICKETS =====
let ticketsFiltered = [];

async function loadTickets() {
  setLoading('ticketsLoading', true);
  try {
    state.tickets = await API.get('/api/tickets');
    applyTicketFilters();
  } catch { toast('Failed to load tickets', 'error'); }
  finally { setLoading('ticketsLoading', false); }
}

function applyTicketFilters() {
  let f = [...state.tickets];
  const sf = $('#ticketFilter').value;
  const pf = $('#priorityFilter').value;
  const sq = $('#ticketSearch').value.toLowerCase();
  if (sf !== 'all') f = f.filter(t => t.status === sf);
  if (pf !== 'all') f = f.filter(t => t.priority === pf);
  if (sq) f = f.filter(t => t.id.toLowerCase().includes(sq) || t.customer.toLowerCase().includes(sq) || t.issue.toLowerCase().includes(sq));
  ticketsFiltered = f;
  renderTickets(f);
}

function renderTickets(tickets) {
  const body = $('#ticketsBody');
  const empty = $('#ticketsEmpty');
  if (!tickets.length) { body.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  body.innerHTML = tickets.map(t =>
    `<tr>
      <td><strong style="color:var(--text);font-weight:600">${esc(t.id)}</strong></td>
      <td>${esc(t.customer)}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(t.issue)}">${esc(t.issue)}</td>
      <td><span class="priority-badge ${esc(t.priority)}">${esc(t.priority)}</span></td>
      <td><span class="status-badge ${esc(t.status)}">${esc(t.status.replace('_', ' '))}</span></td>
      <td>${esc(t.assigned_to)}</td>
      <td>${t.date}</td>
      <td>
        <div class="table-actions">
          ${t.status !== 'resolved' ? `<button class="table-action-btn success" onclick="resolveTicket('${esc(t.id)}')" title="Resolve"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg></button>` : ''}
          <button class="table-action-btn" onclick="assignTicket('${esc(t.id)}')" title="Assign" style="color:var(--accent)"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg></button>
        </div>
      </td>
    </tr>`
  ).join('');
}

window.resolveTicket = async function(id) {
  try {
    await API.patch(`/api/tickets/${id}`, { status: 'resolved' });
    toast(`Ticket ${id} resolved`, 'success');
    loadTickets();
  } catch { toast('Failed to resolve ticket', 'error'); }
};

window.assignTicket = async function(id) {
  const ticket = state.tickets.find(t => t.id === id);
  if (!ticket) return;
  const agents = ['Sarah Chen', 'Mike Ross', 'Emily Park', 'James Brown'].filter(a => a !== ticket.assigned_to);
  const name = prompt(`Assign to:\n${agents.map(a => `- ${a}`).join('\n')}`, agents[0] || '');
  if (!name) return;
  try {
    await API.patch(`/api/tickets/${id}`, { status: 'in_progress', assigned_to: name });
    toast(`${id} assigned to ${name}`, 'success');
    loadTickets();
  } catch { toast('Failed to assign ticket', 'error'); }
};

$('#ticketFilter').onchange = applyTicketFilters;
$('#priorityFilter').onchange = applyTicketFilters;
$('#ticketSearch').addEventListener('input', debounce(applyTicketFilters, 200));

$('#addTicketBtn').onclick = () => {
  $('#ticketCustomer').value = '';
  $('#ticketIssue').value = '';
  $('#ticketPriority').value = 'medium';
  openModal('ticketModal');
};

$('#saveTicketBtn').onclick = async () => {
  const customer = $('#ticketCustomer').value.trim();
  const issue = $('#ticketIssue').value.trim();
  const priority = $('#ticketPriority').value;
  if (!customer || !issue) { toast('Please fill customer and issue', 'error'); return; }
  try {
    await API.post('/api/tickets', { customer, issue, priority });
    toast('Ticket created successfully', 'success');
    closeModal('ticketModal');
    loadTickets();
  } catch { toast('Failed to create ticket', 'error'); }
};

// ===== ESCALATIONS =====
let escFiltered = [];

async function loadEscalations() {
  setLoading('escLoading', true);
  try {
    state.escalations = await API.get('/api/escalations');
    applyEscFilters();
  } catch { toast('Failed to load escalations', 'error'); }
  finally { setLoading('escLoading', false); }
}

function applyEscFilters() {
  let f = [...state.escalations];
  const sq = $('#escSearch').value.toLowerCase();
  if (sq) f = f.filter(e => e.ticket_id.toLowerCase().includes(sq) || e.customer.toLowerCase().includes(sq) || e.issue.toLowerCase().includes(sq));
  escFiltered = f;
  renderEscalations(f);
}

function renderEscalations(escs) {
  const body = $('#escalationsBody');
  const empty = $('#escEmpty');
  if (!escs.length) { body.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  body.innerHTML = escs.map(e =>
    `<tr>
      <td><strong style="color:var(--text);font-weight:600">${esc(e.ticket_id)}</strong></td>
      <td>${esc(e.customer)}</td>
      <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.issue)}">${esc(e.issue)}</td>
      <td>${e.escalated_at}</td>
      <td><span class="status-badge ${esc(e.status)}">${esc(e.status)}</span></td>
      <td>
        ${e.status === 'pending'
          ? `<button class="table-action-btn success" onclick="openResolveEsc('${esc(e.ticket_id)}')" title="Resolve"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg></button>`
          : ''}
      </td>
    </tr>`
  ).join('');
}

window.openResolveEsc = function(id) {
  $('#resolveModal').dataset.ticketId = id;
  openModal('resolveModal');
};

$('#confirmResolveBtn').onclick = async () => {
  const id = $('#resolveModal').dataset.ticketId;
  if (!id) return;
  try {
    await API.post(`/api/escalations/${id}/resolve`);
    toast(`Escalation ${id} resolved`, 'success');
    closeModal('resolveModal');
    loadEscalations();
  } catch { toast('Failed to resolve escalation', 'error'); }
};

$('#refreshEscBtn')?.addEventListener('click', () => { loadEscalations(); toast('Escalations refreshed', 'info'); });
$('#escSearch').addEventListener('input', debounce(applyEscFilters, 200));

// ===== CUSTOMERS =====
let customersFiltered = [];

async function loadCustomers() {
  setLoading('customersLoading', true);
  try {
    state.customers = await API.get('/api/customers');
    applyCustomerFilters();
  } catch { toast('Failed to load customers', 'error'); }
  finally { setLoading('customersLoading', false); }
}

function applyCustomerFilters() {
  let f = [...state.customers];
  const sq = $('#customerSearch').value.toLowerCase();
  if (sq) f = f.filter(c => c.name.toLowerCase().includes(sq) || c.email.toLowerCase().includes(sq) || c.id.toLowerCase().includes(sq));
  customersFiltered = f;
  renderCustomers(f);
}

function renderCustomers(customers) {
  const body = $('#customersBody');
  const empty = $('#customersEmpty');
  if (!customers.length) { body.innerHTML = ''; empty.style.display = 'block'; return; }
  empty.style.display = 'none';
  body.innerHTML = customers.map(c => {
    const [bg1, bg2] = getAvatarColor(c.name);
    return `<tr>
      <td>
        <div class="customer-cell">
          <div class="customer-avatar-sm" style="background:linear-gradient(135deg,${bg1},${bg2})">${getInitials(c.name)}</div>
          <div>
            <div style="color:var(--text);font-weight:600;font-size:13px">${esc(c.name)}</div>
            <div style="color:var(--text-muted);font-size:10px">${esc(c.id)}</div>
          </div>
        </div>
      </td>
      <td>${esc(c.email)}</td>
      <td><span style="font-weight:600;color:var(--text)">${c.orders}</span></td>
      <td><span style="font-weight:600">${c.tickets}</span></td>
      <td>${c.member_since}</td>
    </tr>`;
  }).join('');
}

$('#refreshCustomersBtn')?.addEventListener('click', () => { loadCustomers(); toast('Customers refreshed', 'info'); });
$('#customerSearch').addEventListener('input', debounce(applyCustomerFilters, 200));

// ===== POLICIES =====
async function loadPolicies() {
  setLoading('policiesLoading', true);
  try {
    state.policies = await API.get('/api/return-policies');
    applyPolicyFilters();
  } catch { toast('Failed to load policies', 'error'); }
  finally { setLoading('policiesLoading', false); }
}

function applyPolicyFilters() {
  const sq = $('#policySearch').value.toLowerCase();
  const p = state.policies;
  const icons = {
    electronics: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="2" width="16" height="20" rx="2"/><line x1="9" y1="22" x2="15" y2="22"/></svg>',
    clothing: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M20.38 3.46L16 2a4 4 0 0 1-8 0L3.62 3.46a2 2 0 0 0-1.34 2.23l.58 3.47a1 1 0 0 0 .99.84H6v10c0 1.1.9 2 2 2h8a2 2 0 0 0 2-2V10h2.15a1 1 0 0 0 .99-.84l.58-3.47a2 2 0 0 0-1.34-2.23z"/></svg>',
    furniture: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M20 9V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v2"/><path d="M2 14h20"/><path d="M4 14v5"/><path d="M20 14v5"/></svg>',
  };
  const entries = Object.entries(p).filter(([cat, policy]) =>
    !sq || cat.toLowerCase().includes(sq) || policy.toLowerCase().includes(sq)
  );
  $('#policiesGrid').innerHTML = entries.length
    ? entries.map(([cat, policy]) =>
        `<div class="policy-card">
          <div class="policy-icon">${icons[cat] || ''}</div>
          <h3>${esc(cat)}</h3>
          <p>${esc(policy)}</p>
        </div>`
      ).join('')
    : '<div style="text-align:center;padding:48px 20px;color:var(--text-muted)"><p>No policies match your search.</p></div>';
  if (entries.length) $('#policiesGrid').style.display = 'grid';
}

$('#policySearch').addEventListener('input', debounce(applyPolicyFilters, 200));

// ===== INIT =====
showWelcome();
loadDashboard();
loadOrders();
loadTickets();
loadEscalations();
loadCustomers();
loadPolicies();

// Keyboard shortcut: Ctrl+/ to focus chat
document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === '/') {
    e.preventDefault();
    if (currentPage === 'chat') chatInput.focus();
    else navigate('chat');
  }
});
