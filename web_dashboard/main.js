// ============================================================
//  Auth guard: check sessionStorage, redirect to login if needed
// ============================================================
if (!sessionStorage.getItem('dashboard_auth')) {
    window.location.href = '/login.html';
}

// ============================================================
//  Unified Dashboard — TabModule Registry Pattern
//  每个数据源封装为独立 Module，侧边栏、搜索、卡片渲染均委托给 Module
// ============================================================

// ---- State ----
const state = {
    activeSource: 'all',
    searchQuery: '',
    results: [],
    offset: 0,
    limit: 40,
    hasMore: true,
    isLoading: false,
    selectedCommandIndex: 0,
    currentTaskId: null,
    eventSource: null,
};

// ---- TabModule Registry ----
const tabModules = {};

function registerTabModule(config) {
    tabModules[config.id] = config;
}

// ============================================================
//  Module: emails (Meilisearch)
// ============================================================
registerTabModule({
    id: 'emails',
    label: '邮件',
    icon: '📧',
    badgeId: 'badge-emails',

    async fetchCount() {
        try {
            const r = await fetch('/agent/meili/indexes/emails/stats');
            const d = await r.json();
            return d.numberOfDocuments || 0;
        } catch { return 0; }
    },

    async search(query, offset, limit) {
        const payload = {
            q: query || '',
            sort: ['email_date:desc'],
            offset, limit,
            attributesToHighlight: ['subject', 'sender', 'plain_text', 'summary'],
            highlightPreTag: '<mark>', highlightPostTag: '</mark>',
        };
        const r = await fetch('/agent/meili/indexes/emails/search', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!r.ok) return { hits: [], total: 0 };
        const d = await r.json();
        return {
            hits: (d.hits || []).map(h => { h._module = 'emails'; return h; }),
            total: d.estimatedTotalHits || d.totalHits || 0,
        };
    },

    renderCard(doc) {
        const fmt = doc._formatted || {};
        const subj = fmt.subject || doc.subject || '(无主题)';
        const sender = fmt.sender || doc.sender || '';
        const category = doc.category || '';
        const imp = doc.importance || '';
        const account = doc.account_name || '';
        const date = (doc.email_date || '').slice(0, 16);
        const summary = doc.summary || '';
        const snippet = safeSnippet(fmt.plain_text || doc.plain_text || '', 120);
        const uid = doc.uid || '';
        const acc = doc.account_name || '';

        let html = '<div class="card email-card">';
        html += `<div class="card-meta"><span class="tag">${h(category)} / ${h(imp)}</span>`;
        html += `<span class="sender">${h(sender)}</span>`;
        html += `<span class="date">${date}</span>`;
        if (account) html += `<span class="account-badge">${h(account)}</span>`;
        html += '</div>';
        html += `<h3 class="card-title">${subj}</h3>`;
        if (summary) html += `<div class="ai-summary"><p>📝 ${h(summary)}</p></div>`;
        html += `<div class="card-snippet">${snippet}</div>`;
        html += `<div class="card-actions">`;
        html += `<button class="btn-reprocess" data-account="${h(acc)}" data-uid="${h(uid)}">🔄 重新处理</button>`;
        html += `<button class="btn-view-original" data-account="${h(acc)}" data-uid="${h(uid)}">📄 查看原文</button>`;
        html += '</div></div>';
        return html;
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },
});

// ============================================================
//  Module: rss (Meilisearch)
// ============================================================
registerTabModule({
    id: 'rss',
    label: 'RSS',
    icon: '📰',
    badgeId: 'badge-rss',

    async fetchCount() {
        try {
            const r = await fetch('/agent/meili/indexes/rss/stats');
            const d = await r.json();
            return d.numberOfDocuments || 0;
        } catch { return 0; }
    },

    async search(query, offset, limit) {
        const payload = {
            q: query || '',
            sort: ['published:desc'],
            offset, limit,
            attributesToHighlight: ['title', 'content'],
            highlightPreTag: '<mark>', highlightPostTag: '</mark>',
        };
        const r = await fetch('/agent/meili/indexes/rss/search', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!r.ok) return { hits: [], total: 0 };
        const d = await r.json();
        return {
            hits: (d.hits || []).map(h => { h._module = 'rss'; return h; }),
            total: d.estimatedTotalHits || d.totalHits || 0,
        };
    },

    renderCard(doc) {
        const fmt = doc._formatted || {};
        const title = fmt.title || doc.title || '(无标题)';
        const link = doc.link || '';
        const node = doc.node_name || '';
        const published = (doc.published || '').slice(0, 16);
        const content = safeSnippet(fmt.content || doc.content || '', 150);

        let html = '<div class="card rss-card">';
        html += `<div class="card-meta"><span class="tag">${h(node)}</span>`;
        html += `<span class="date">${published}</span></div>`;
        html += `<h3 class="card-title">`;
        if (link) html += `<a href="${h(link)}" target="_blank" rel="noopener">${title}</a>`;
        else html += title;
        html += '</h3>';
        html += `<div class="card-snippet">${content}</div></div>`;
        return html;
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },
});

// ============================================================
//  Module: todos (HTTP API)
// ============================================================
registerTabModule({
    id: 'todos',
    label: '待办',
    icon: '✅',
    badgeId: 'badge-todos',

    async fetchCount() {
        try {
            const r = await fetch('/agent/api/v1/todos?status=pending,active');
            const d = await r.json();
            return (d.data || []).length;
        } catch { return 0; }
    },

    async _fetchAll() {
        const r = await fetch('/agent/api/v1/todos?status=pending,active');
        const d = await r.json();
        return (d.data || []).map(t => { t._module = 'todos'; return t; });
    },

    async search(query, offset, limit) {
        const all = await this._fetchAll();
        let filtered = all;
        if (query) {
            const q = query.toLowerCase();
            filtered = all.filter(t =>
                (t.title || '').toLowerCase().includes(q) ||
                (t.description || '').toLowerCase().includes(q)
            );
        }
        return { hits: filtered.slice(offset, offset + limit), total: filtered.length };
    },

    renderCard(doc) {
        const kind = doc.kind || 'todo';
        const kindIcons = { code: '💻', note: '📝', todo: '📋', review: '👀' };
        const icon = kindIcons[kind] || '📌';
        const project = doc.project || '';
        const title = doc.title || '(无标题)';
        const updated = (doc.updated_at || '').slice(0, 16);
        const status = doc.status || 'pending';
        const statusText = { pending: '待处理', active: '进行中', done: '已完成' }[status] || status;

        let html = '<div class="card todo-card">';
        html += `<div class="card-meta"><span class="tag">${icon} ${h(kind)}</span>`;
        if (project) html += `<span class="tag">${h(project)}</span>`;
        html += `<span class="date">${updated}</span>`;
        html += `<span class="tag status-tag">${statusText}</span></div>`;
        html += `<h3 class="card-title">${h(title)}</h3>`;
        if (doc.description) html += `<div class="card-snippet">${h(doc.description.slice(0, 200))}</div>`;
        html += '</div>';
        return html;
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },
});

// ============================================================
//  Module: sessions (NEW — SQLite via API)
// ============================================================
registerTabModule({
    id: 'sessions',
    label: '会话',
    icon: '💬',
    badgeId: 'badge-sessions',

    async fetchCount() {
        try {
            const r = await fetch('/agent/api/v1/sessions?limit=1');
            const d = await r.json();
            return d.total || 0;
        } catch { return 0; }
    },

    async search(query, offset, limit) {
        try {
            const r = await fetch(`/agent/api/v1/sessions?limit=${limit}`);
            const d = await r.json();
            let items = (d.sessions || []).map(s => { s._module = 'sessions'; return s; });
            if (query) {
                const q = query.toLowerCase();
                items = items.filter(s =>
                    (s.channel || '').toLowerCase().includes(q) ||
                    (s.model || '').toLowerCase().includes(q) ||
                    (s.goal || '').toLowerCase().includes(q)
                );
            }
            return { hits: items.slice(offset, offset + limit), total: d.total || 0 };
        } catch {
            return { hits: [], total: 0 };
        }
    },

    renderCard(doc) {
        const icon = doc.channel_icon || '🔌';
        const channel = doc.channel || '?';
        const status = doc.status || 'chatting';
        const statusMap = { chatting: '闲聊', working: '执行中', archived: '已归档' };
        const statusText = statusMap[status] || status;
        const statusColor = status === 'working' ? 'var(--warning)' : (status === 'archived' ? 'var(--text-muted)' : 'var(--success)');
        const model = doc.model || '—';
        const tokenUsage = formatTokens(doc.token_usage || 0);
        const toolCalls = doc.tool_calls || 0;
        const goal = doc.goal || '';
        const updated = doc.updated_at ? new Date(doc.updated_at * 1000).toLocaleString('zh-CN') : '—';

        let html = '<div class="card session-card">';
        html += '<div class="card-meta">';
        html += `<span class="tag">${icon} ${h(channel)}</span>`;
        html += `<span class="tag status-tag" style="color:${statusColor}">● ${statusText}</span>`;
        html += `<span class="date">${updated}</span>`;
        html += '</div>';
        if (goal) html += `<div class="ai-summary"><p>🎯 ${h(goal)}</p></div>`;
        html += '<div class="session-stats">';
        html += `<span title="Token 消耗">🪙 ${tokenUsage}</span>`;
        html += `<span title="工具调用次数">🔧 ${toolCalls} 次</span>`;
        html += `<span title="LLM 模型">🧠 ${h(model)}</span>`;
        html += '</div>';
        html += '<div class="card-actions">';
        html += `<button class="card-btn btn-view-session" data-session="${h(doc.session_key)}">📋 查看对话</button>`;
        html += '</div>';
        html += '</div>';
        return html;
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },
});

// ============================================================
//  Helpers
// ============================================================
function h(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatTokens(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
}

function safeSnippet(text, maxLen) {
    if (!text) return '';
    // Preserve <mark> tags, strip everything else
    const marks = [];
    const placeholder = text.replace(/<mark>/g, () => { marks.push('<mark>'); return '\x00M'; })
                           .replace(/<\/mark>/g, () => { marks.push('</mark>'); return '\x00m'; });
    const stripped = placeholder.replace(/<[^>]+>/g, '');
    const escaped = stripped.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    let result = escaped.replace(/\x00M/g, '<mark>').replace(/\x00m/g, '</mark>');
    // Auto-close unclosed marks
    if ((result.match(/<mark>/g) || []).length > (result.match(/<\/mark>/g) || []).length) {
        result += '</mark>';
    }
    if (result.length > maxLen) result = result.slice(0, maxLen) + '...';
    return result;
}

// ============================================================
//  Core: Unified Search Pipeline
// ============================================================
async function performSearch(append = false) {
    if (state.isLoading) return;
    const query = state.searchQuery;
    const limit = state.limit;

    if (!append) {
        state.results = [];
        state.offset = 0;
        state.hasMore = true;
    }

    state.isLoading = true;
    showSearchSpinner(!append);

    try {
        if (state.activeSource === 'all') {
            // Aggregate all modules (except 'all' itself)
            const activeModules = Object.values(tabModules).filter(m => m.search && m.id !== 'all');
            const results = await Promise.all(activeModules.map(async m => {
                try {
                    return await m.search(query, append ? state.offset : 0, Math.floor(limit / activeModules.length));
                } catch { return { hits: [], total: 0 }; }
            }));
            const allHits = results.flatMap(r => r.hits);
            // Sort by date proxy
            allHits.sort((a, b) => {
                const da = a.email_date || a.published || (a.updated_at ? new Date(a.updated_at * 1000).toISOString() : '');
                const db = b.email_date || b.published || (b.updated_at ? new Date(b.updated_at * 1000).toISOString() : '');
                return db.localeCompare(da);
            });
            state.results = append ? [...state.results, ...allHits] : allHits;
            state.hasMore = false; // 'all' view doesn't support true pagination
            state.offset += limit;
        } else {
            const mod = tabModules[state.activeSource];
            if (!mod || !mod.search) {
                state.results = [];
                state.hasMore = false;
            } else {
                const r = await mod.search(query, append ? state.offset : 0, limit);
                state.results = append ? [...state.results, ...r.hits] : r.hits;
                state.hasMore = r.hits.length >= limit;
                state.offset += limit;
            }
        }
    } catch (e) {
        console.error('Search error:', e);
        state.hasMore = false;
    }

    renderResults();
    updateResultsCount();
    state.isLoading = false;
    hideSearchSpinner();
}

function showSearchSpinner(reset) {
    const grid = document.getElementById('results-grid');
    if (reset) {
        grid.innerHTML = '<div class="loading-placeholder"><div class="spinner"></div><p>正在检索...</p></div>';
    }
    const sentinel = document.getElementById('scroll-sentinel');
    sentinel.style.display = '';
    const spinnerEl = sentinel.querySelector('.spinner');
    if (spinnerEl) spinnerEl.style.display = '';
}

function hideSearchSpinner() {
    const sentinel = document.getElementById('scroll-sentinel');
    const spinnerEl = sentinel.querySelector('.spinner');
    if (spinnerEl && !state.hasMore) spinnerEl.style.display = 'none';
    if (!state.hasMore) {
        sentinel.querySelector('span').textContent = state.results.length === 0 ? '' : '— 已加载全部结果 —';
    }
}

// ============================================================
//  Core: Unified Render
// ============================================================
function renderResults() {
    const grid = document.getElementById('results-grid');
    const cards = state.results.map(doc => {
        const mod = tabModules[doc._module];
        return mod && mod.renderCard ? mod.renderCard(doc) : '';
    }).join('');

    grid.innerHTML = cards || '<div class="empty-state"><p>📭 暂无匹配结果</p><p class="muted">尝试切换数据源或修改检索词</p></div>';

    // Re-bind email action buttons
    document.querySelectorAll('.btn-reprocess').forEach(btn => {
        btn.addEventListener('click', () => {
            const acc = btn.getAttribute('data-account');
            const uid = btn.getAttribute('data-uid');
            if (acc && uid) triggerCommand(`/mail_reprocess ${acc} ${uid}`);
        });
    });
    document.querySelectorAll('.btn-view-original').forEach(btn => {
        btn.addEventListener('click', () => {
            const acc = btn.getAttribute('data-account');
            const uid = btn.getAttribute('data-uid');
            if (acc && uid) window.open(`/agent/api/v1/email/html?account=${encodeURIComponent(acc)}&uid=${encodeURIComponent(uid)}`, '_blank');
        });
    });
    // Session: view messages
    document.querySelectorAll('.btn-view-session').forEach(btn => {
        btn.addEventListener('click', async () => {
            const sessionKey = btn.getAttribute('data-session');
            if (!sessionKey) return;
            btn.textContent = '加载中...';
            btn.disabled = true;
            try {
                const r = await fetch(`/agent/api/v1/sessions/messages?session_key=${encodeURIComponent(sessionKey)}&limit=15`);
                const d = await r.json();
                showSessionMessagesModal(sessionKey, d.messages || []);
            } catch(e) {
                alert('加载失败: ' + e.message);
            }
            btn.textContent = '📋 查看对话';
            btn.disabled = false;
        });
    });
}

function updateResultsCount() {
    const el = document.getElementById('results-count');
    const label = tabModules[state.activeSource];
    const name = label && label.label ? label.label : state.activeSource;
    el.textContent = `${state.results.length} 条结果 · ${name}`;
}

// ============================================================
//  Stats: fetch all module counts + update badges
// ============================================================
async function fetchAllStats() {
    let total = 0;
    for (const mod of Object.values(tabModules)) {
        if (!mod.fetchCount) continue;
        try {
            const count = await mod.fetchCount();
            total += count;
            const badgeEl = document.getElementById(mod.badgeId);
            if (badgeEl && mod.renderBadge) {
                mod.renderBadge(badgeEl, count);
            }
        } catch { /* ignore */ }
    }
    const badgeAll = document.getElementById('badge-all');
    if (badgeAll) badgeAll.textContent = total;
}

// ============================================================
//  Init: Search Input
// ============================================================
function initSearch() {
    const input = document.getElementById('search-input');
    let debounceTimer;
    input.addEventListener('input', () => {
        clearTimeout(debounceTimer);
        const val = input.value.trim();
        if (val === '/') {
            openCommandModal();
            input.value = '';
            return;
        }
        debounceTimer = setTimeout(() => {
            state.searchQuery = val;
            state.offset = 0;
            performSearch(false);
        }, 250);
    });
}

// ============================================================
//  IntersectionObserver: Infinite Scroll
// ============================================================
function initIntersectionObserver() {
    const sentinel = document.getElementById('scroll-sentinel');
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting && !state.isLoading && state.hasMore) {
                performSearch(true);
            }
        });
    }, { rootMargin: '200px' });
    observer.observe(sentinel);
}

// ============================================================
//  Filter Button Events
// ============================================================
function initFilterButtons() {
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.activeSource = btn.getAttribute('data-source');
            state.offset = 0;
            performSearch(false);
        });
    });
}

// ============================================================
//  Command Palette (unchanged from original)
// ============================================================
function openCommandModal() {
    document.getElementById('command-modal').style.display = 'flex';
    document.getElementById('command-input').focus();
    state.selectedCommandIndex = 0;
    updateCommandSelection();
}

function closeCommandModal() {
    document.getElementById('command-modal').style.display = 'none';
}

function updateCommandSelection() {
    document.querySelectorAll('.command-item').forEach((el, i) => {
        el.classList.toggle('selected', i === state.selectedCommandIndex);
    });
}

function getSelectedCommandText() {
    const items = document.querySelectorAll('.command-item');
    if (items.length === 0) return '';
    const idx = Math.min(Math.max(0, state.selectedCommandIndex), items.length - 1);
    return items[idx].getAttribute('data-cmd') || '';
}

function triggerCommand(cmdText) {
    if (!cmdText.trim()) return;
    // Open terminal if minimized
    const term = document.getElementById('terminal-window');
    if (term.classList.contains('minimized')) term.classList.remove('minimized');

    const termBody = document.getElementById('terminal-body');
    appendTerminalLine(`> ${cmdText}`, 'user');

    fetch('/agent/api/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: 'dashboard_admin', text: cmdText }),
    })
    .then(r => r.json())
    .then(data => {
        if (data.type === 'sync') {
            appendTerminalLine(data.response || '(无返回)', 'system');
        } else if (data.type === 'async') {
            state.currentTaskId = data.task_id;
            appendTerminalLine(`任务已受理 (ID: ${data.task_id})，等待执行...`, 'system');
            subscribeTaskStream(data.task_id);
        }
    })
    .catch(e => {
        appendTerminalLine(`请求失败: ${e.message}`, 'error');
    });
}

// ============================================================
//  Terminal (unchanged)
// ============================================================
function appendTerminalLine(text, type = 'system') {
    const body = document.getElementById('terminal-body');
    const div = document.createElement('div');
    div.className = `terminal-line ${type}`;
    div.textContent = text;
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
}

function subscribeTaskStream(taskId) {
    if (state.eventSource) state.eventSource.close();
    const es = new EventSource(`/agent/api/v1/task/stream?task_id=${taskId}&session_id=dashboard_admin`);
    state.eventSource = es;
    const seenProgress = new Set();
    let lastMsg = '';

    es.onmessage = (e) => {
        if (e.data === '[DONE]') {
            es.close();
            state.eventSource = null;
            return;
        }
        const d = jsonParseSafe(e.data);
        if (!d) return;
        if (d.status === 'done' || d.status === 'completed') {
            appendTerminalLine(d.response || d.message || '任务完成', 'system');
            es.close();
            state.eventSource = null;
            return;
        }
        if (d.status === 'failed' || d.status === 'error') {
            appendTerminalLine(d.message || d.error || '任务失败', 'error');
            es.close();
            state.eventSource = null;
            return;
        }
        if (d.progress) {
            const key = `${d.progress.done}-${d.progress.total}`;
            if (!seenProgress.has(key)) {
                seenProgress.add(key);
                const msg = `📊 进度: ${d.progress.done}/${d.progress.total} 完成${d.progress.failed ? `, ${d.progress.failed} 失败` : ''}`;
                if (msg !== lastMsg) {
                    lastMsg = msg;
                    appendTerminalLine(msg, 'progress');
                }
            }
        }
    };
    es.onerror = () => {
        es.close();
        state.eventSource = null;
    };
}

function jsonParseSafe(s) {
    try { return JSON.parse(s); } catch { return null; }
}

// ============================================================
//  Chat Drawer (unchanged)
// ============================================================
let chatEventSource = null;

function initChatDrawer() {
    const fab = document.getElementById('chat-fab');
    const drawer = document.getElementById('chat-drawer');
    const closeBtn = document.getElementById('close-drawer-btn');
    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('chat-send-btn');

    fab.addEventListener('click', () => drawer.classList.add('open'));
    closeBtn.addEventListener('click', () => drawer.classList.remove('open'));

    function doSend() {
        const text = input.value.trim();
        if (!text) return;
        appendChatBubble('user', text);
        input.value = '';
        sendChatMessage(text);
    }

    sendBtn.addEventListener('click', doSend);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            doSend();
        }
    });
}

function appendChatBubble(role, html) {
    const history = document.getElementById('chat-history');
    const div = document.createElement('div');
    div.className = `chat-message ${role}`;
    div.innerHTML = `<div class="bubble">${html}</div>`;
    history.appendChild(div);
    history.scrollTop = history.scrollHeight;
}

function sendChatMessage(text) {
    if (chatEventSource) chatEventSource.close();

    // Show typing indicator
    const typingEl = document.createElement('div');
    typingEl.className = 'chat-message agent';
    typingEl.innerHTML = '<div class="bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div>';
    typingEl.id = 'chat-typing-indicator';
    const history = document.getElementById('chat-history');
    history.appendChild(typingEl);
    history.scrollTop = history.scrollHeight;

    function removeTyping() {
        const el = document.getElementById('chat-typing-indicator');
        if (el) el.remove();
    }

    fetch('/agent/api/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: 'dashboard_chat', text }),
    })
    .then(r => r.json())
    .then(data => {
        removeTyping();
        if (data.type === 'sync') {
            const md = marked.parse(data.response || '(空)');
            appendChatBubble('agent', md);
        } else if (data.type === 'async') {
            subscribeChatStream(data.task_id);
        }
    })
    .catch(e => {
        removeTyping();
        appendChatBubble('system', `请求失败: ${e.message}`);
    });
}

function subscribeChatStream(taskId) {
    const es = new EventSource(`/agent/api/v1/task/stream?task_id=${taskId}&session_id=dashboard_chat`);
    chatEventSource = es;
    let bubbleHtml = '';
    const systemLines = [];

    es.onmessage = (e) => {
        if (e.data === '[DONE]') {
            es.close();
            chatEventSource = null;
            if (bubbleHtml) appendChatBubble('agent', bubbleHtml);
            else if (systemLines.length) appendChatBubble('system', systemLines.join('<br>'));
            else appendChatBubble('agent', '任务完成 (无返回内容)');
            return;
        }
        const d = jsonParseSafe(e.data);
        if (!d) return;

        if (d.status === 'done' || d.status === 'completed') {
            if (d.response) {
                bubbleHtml += marked.parse(d.response);
            }
            es.close();
            chatEventSource = null;
            appendChatBubble('agent', bubbleHtml || d.message || '任务完成');
            return;
        }
        if (d.status === 'failed' || d.status === 'error') {
            appendChatBubble('system', d.message || d.error || '任务失败');
            es.close();
            chatEventSource = null;
            return;
        }
        if (d.message && d.status === 'running') {
            systemLines.push(d.message);
        }
    };
    es.onerror = () => {
        es.close();
        chatEventSource = null;
    };
}

// ============================================================
//  Keyboard Shortcuts
// ============================================================
function initKeyboard() {
    document.addEventListener('keydown', (e) => {
        const modal = document.getElementById('command-modal');
        const isModalOpen = modal.style.display === 'flex';
        const activeEl = document.activeElement;
        const isInput = activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'TEXTAREA');

        // Command modal navigation
        if (isModalOpen) {
            if (e.key === 'Escape') { closeCommandModal(); return; }
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                const items = document.querySelectorAll('.command-item');
                if (items.length) state.selectedCommandIndex = Math.min(state.selectedCommandIndex + 1, items.length - 1);
                updateCommandSelection();
                document.getElementById('command-input').value = getSelectedCommandText();
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                state.selectedCommandIndex = Math.max(0, state.selectedCommandIndex - 1);
                updateCommandSelection();
                document.getElementById('command-input').value = getSelectedCommandText();
                return;
            }
            if (e.key === 'Enter') {
                e.preventDefault();
                const cmd = document.getElementById('command-input').value.trim();
                if (cmd) triggerCommand(cmd);
                closeCommandModal();
                return;
            }
            return;
        }

        // Global shortcuts
        if (e.key === '/' && !isInput) {
            e.preventDefault();
            openCommandModal();
        }
        if (e.key === 'Escape' && isInput) {
            activeEl.blur();
        }
    });
}

// ============================================================
//  Terminal Toggle
// ============================================================
function initTerminal() {
    const header = document.getElementById('terminal-header');
    const closeBtn = document.getElementById('term-close');
    const toggleBtn = document.getElementById('term-toggle');
    const terminal = document.getElementById('terminal-window');

    header.addEventListener('click', () => {
        terminal.classList.toggle('minimized');
        toggleBtn.textContent = terminal.classList.contains('minimized') ? '展开' : '收起';
    });
    closeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        terminal.style.display = 'none';
    });
    toggleBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        terminal.classList.toggle('minimized');
        toggleBtn.textContent = terminal.classList.contains('minimized') ? '展开' : '收起';
    });
}

// ============================================================
//  Modal overlay click to close
// ============================================================
function initModalClose() {
    document.getElementById('command-modal').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) closeCommandModal();
    });
}

// ============================================================
//  Session Messages Modal
// ============================================================
function showSessionMessagesModal(sessionKey, messages) {
    // Remove existing modal if any
    document.querySelectorAll('.session-modal-overlay').forEach(el => el.remove());

    const roleIcons = { user: '👤', assistant: '🤖', system: '⚙️', tool: '🔧' };
    const rows = messages.map(m => {
        const icon = roleIcons[m.role] || '💬';
        const time = m.time ? new Date(m.time * 1000).toLocaleTimeString('zh-CN') : '';
        const content = h(m.content || '(空)');
        return `<div class="session-msg"><span class="msg-role">${icon} ${m.role}</span><span class="msg-time">${time}</span><div class="msg-content">${content}</div></div>`;
    }).join('') || '<p style="color:var(--text-muted);text-align:center">暂无消息记录</p>';

    const overlay = document.createElement('div');
    overlay.className = 'session-modal-overlay';
    overlay.innerHTML = `
        <div class="session-modal">
            <div class="session-modal-header">
                <span>📋 会话对话记录</span>
                <span style="font-size:0.78rem;color:var(--text-muted)">${h(sessionKey)}</span>
                <button class="icon-btn session-modal-close">&times;</button>
            </div>
            <div class="session-modal-body">${rows}</div>
        </div>`;
    document.body.appendChild(overlay);

    overlay.addEventListener('click', (e) => {
        if (e.target === overlay || e.target.classList.contains('session-modal-close')) {
            overlay.remove();
        }
    });
    document.addEventListener('keydown', function escHandler(e) {
        if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', escHandler); }
    });
}

// ============================================================
//  Bootstrap
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    initSearch();
    initFilterButtons();
    initIntersectionObserver();
    initTerminal();
    initChatDrawer();
    initKeyboard();
    initModalClose();

    // Load initial data
    fetchAllStats();
    performSearch(false);
});
