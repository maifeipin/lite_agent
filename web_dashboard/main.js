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
    activeFilters: {},       // { source: ['v2ex.com'], type: ['bill'] }
    lastFacets: null,         // Meilisearch facetDistribution from last response
};

// Meili-backed sources (support facets)
const FACET_SOURCES = new Set(['emails', 'rss']);

// ---- Filter builder ----
function buildFilter() {
    const parts = [];
    for (const [field, values] of Object.entries(state.activeFilters)) {
        if (!values || values.length === 0) continue;
        parts.push(values.map(v => `${field} = "${v}"`).join(' OR '));
    }
    if (parts.length === 0) return undefined;
    return parts.map(p => `(${p})`).join(' AND ');
}

// ---- TabModule Registry ----
const tabModules = {};

function registerTabModule(config) {
    tabModules[config.id] = config;
}

// ============================================================
//  Modules are loaded from web_dashboard/modules/*.js
//  (emails.js, rss.js, todos.js, sessions.js)
// ============================================================



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

function getDocTimestamp(doc) {
    if (doc.date) return Number(doc.date);
    if (doc.published) {
        const t = Date.parse(doc.published);
        if (!isNaN(t)) return t / 1000;
    }
    if (doc.email_date) {
        const t = Date.parse(doc.email_date);
        if (!isNaN(t)) return t / 1000;
    }
    if (doc.updated_at) {
        if (typeof doc.updated_at === 'number') return doc.updated_at;
        const t = Date.parse(doc.updated_at);
        if (!isNaN(t)) return t / 1000;
    }
    return 0;
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
    const filterStr = buildFilter();

    try {
        if (state.activeSource === 'all') {
            const activeModules = Object.values(tabModules).filter(m => m.search && m.id !== 'all');
            const results = await Promise.all(activeModules.map(async m => {
                try {
                    return await m.search(query, append ? state.offset : 0, Math.floor(limit / activeModules.length), filterStr);
                } catch { return { hits: [], total: 0, facets: {} }; }
            }));
            const allHits = results.flatMap(r => r.hits);
            allHits.sort((a, b) => getDocTimestamp(b) - getDocTimestamp(a));
            state.results = append ? [...state.results, ...allHits] : allHits;
            state.hasMore = false;
            state.offset += limit;
            state.lastFacets = null;
        } else {
            const mod = tabModules[state.activeSource];
            if (!mod || !mod.search) {
                state.results = [];
                state.hasMore = false;
                state.lastFacets = null;
            } else {
                const r = await mod.search(query, append ? state.offset : 0, limit, filterStr);
                state.results = append ? [...state.results, ...r.hits] : r.hits;
                state.hasMore = r.hits.length >= limit;
                state.offset += limit;
                state.lastFacets = r.facets || null;
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
    if (state.lastFacets) renderFacetPanel(state.lastFacets);
    loadRssBrief();
}

async function loadRssBrief() {
    const briefPanel = document.getElementById('rss-brief-panel');
    if (!briefPanel) return;

    if (state.activeSource !== 'rss') {
        briefPanel.style.display = 'none';
        return;
    }

    try {
        const response = await fetch('/agent/api/v1/rss/brief');
        if (!response.ok) {
            briefPanel.style.display = 'none';
            return;
        }
        const data = await response.json();
        if (data && data.topics && data.topics.length > 0) {
            document.getElementById('rss-brief-date').textContent = data.date || '';
            document.getElementById('rss-brief-summary').textContent = data.summary || '';
            
            const topicsContainer = document.getElementById('rss-brief-topics');
            topicsContainer.innerHTML = '';
            
            data.topics.forEach(t => {
                const sentimentClass = t.sentiment === '正' ? 'sentiment-pos' : (t.sentiment === '负' ? 'sentiment-neg' : 'sentiment-neu');
                const sentimentEmoji = t.sentiment === '正' ? '🟢 正' : (t.sentiment === '负' ? '🔴 负' : '⚪ 中');
                
                const card = document.createElement('div');
                card.className = 'brief-topic-card';
                
                const titleRow = document.createElement('div');
                titleRow.className = 'brief-topic-title-row';
                
                const titleSpan = document.createElement('span');
                titleSpan.textContent = t.topic || '';
                
                const sentimentSpan = document.createElement('span');
                sentimentSpan.className = `brief-topic-sentiment ${sentimentClass}`;
                sentimentSpan.textContent = sentimentEmoji;
                
                titleRow.appendChild(titleSpan);
                titleRow.appendChild(sentimentSpan);
                
                const analysisDiv = document.createElement('div');
                analysisDiv.className = 'brief-topic-analysis';
                analysisDiv.textContent = t.analysis || '';
                
                card.appendChild(titleRow);
                card.appendChild(analysisDiv);
                
                topicsContainer.appendChild(card);
            });
            
            briefPanel.style.display = 'block';
        } else {
            briefPanel.style.display = 'none';
        }
    } catch (e) {
        console.error('Failed to fetch RSS brief:', e);
        briefPanel.style.display = 'none';
    }
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
function _callLifecycle(hook, container) {
    const mod = tabModules[state.activeSource];
    if (mod && typeof mod[hook] === 'function') {
        try { mod[hook](container); } catch(e) { console.warn('[lifecycle]', hook, e); }
    }
}

function initFilterButtons() {
    const container = document.querySelector('.main-container') || document.body;
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            // Unmount current tab
            _callLifecycle('onUnmount', container);

            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.activeSource = btn.getAttribute('data-source');
            state.offset = 0;
            state.activeFilters = {};
            performSearch(false);
            updateFacetPanelVisibility();

            // Mount new tab (deferred so render completes first)
            setTimeout(() => _callLifecycle('onMount', container), 50);
        });
    });
}

// ============================================================
//  Facet Panel — Render / Visibility / Events
// ============================================================
function updateFacetPanelVisibility() {
    const panel = document.getElementById('facet-panel');
    if (!panel) return;
    panel.style.display = FACET_SOURCES.has(state.activeSource) ? '' : 'none';
}

function renderFacetPanel(facetDist) {
    if (!facetDist || !FACET_SOURCES.has(state.activeSource)) return;
    const panel = document.getElementById('facet-panel');
    if (!panel || panel.style.display === 'none') return;

    const groups = [];
    // Build merged value set per group (server values + currently checked)
    for (const [group, serverVals] of Object.entries(facetDist)) {
        const merged = new Set([
            ...Object.keys(serverVals || {}),
            ...(state.activeFilters[group] || [])
        ]);
        if (merged.size === 0) continue;

        const items = [];
        for (const val of merged) {
            const count = (serverVals || {})[val] || 0;
            const checked = (state.activeFilters[group] || []).includes(val);
            items.push({ val, count, checked });
        }
        // Sort by count desc
        items.sort((a, b) => b.count - a.count);
        groups.push({ key: group, items });
    }

    let html = '';
    for (const g of groups) {
        html += `<div class="facet-group"><div class="facet-group-title">${({category:'🗂 分类',topics:'🏷 主题',source:'📂 来源',type:'📄 类型'})[g.key] || g.key}</div>`;
        for (const item of g.items) {
            const id = `facet-${g.key}-${item.val.replace(/[^a-zA-Z0-9]/g, '_')}`;
            html += `<label class="facet-item" for="${id}">`;
            html += `<input type="checkbox" id="${id}" data-facet="${g.key}" data-value="${item.val}" ${item.checked ? 'checked' : ''}>`;
            html += `<span>${h(item.val)}<b class="count">${item.count}</b></span>`;
            html += '</label>';
        }
        html += '</div>';
    }
    panel.innerHTML = html || '';

    // Bind events
    panel.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        cb.addEventListener('change', () => {
            const facet = cb.getAttribute('data-facet');
            const value = cb.getAttribute('data-value');
            if (!state.activeFilters[facet]) state.activeFilters[facet] = [];
            if (cb.checked) {
                if (!state.activeFilters[facet].includes(value)) state.activeFilters[facet].push(value);
            } else {
                state.activeFilters[facet] = state.activeFilters[facet].filter(v => v !== value);
                if (state.activeFilters[facet].length === 0) delete state.activeFilters[facet];
            }
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
    const ocrBtn = document.getElementById('chat-ocr-btn');
    const ocrFileInput = document.getElementById('chat-ocr-file-input');

    const fullscreenBtn = document.getElementById('fullscreen-drawer-btn');

    fab.addEventListener('click', () => drawer.classList.add('open'));
    closeBtn.addEventListener('click', () => {
        drawer.classList.remove('open');
        drawer.classList.remove('fullscreen');
    });

    if (fullscreenBtn) {
        fullscreenBtn.addEventListener('click', () => {
            drawer.classList.toggle('fullscreen');
            const isFullscreen = drawer.classList.contains('fullscreen');
            fullscreenBtn.title = isFullscreen ? '退出全屏' : '全屏';
        });
    }

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

    // Handle OCR Upload Button
    if (ocrBtn && ocrFileInput) {
        ocrBtn.addEventListener('click', () => ocrFileInput.click());
        ocrFileInput.addEventListener('change', (e) => {
            const file = e.target.files[0];
            if (file) {
                handleOcrUpload(file);
            }
            ocrFileInput.value = ''; // Reset to allow uploading same file
        });
    }

    // Intercept Paste events for clipboard image OCR
    input.addEventListener('paste', (e) => {
        const items = (e.clipboardData || e.originalEvent.clipboardData).items;
        for (const item of items) {
            if (item.type.indexOf('image') === 0) {
                e.preventDefault();
                const file = item.getAsFile();
                handleOcrUpload(file);
                break;
            }
        }
    });

    async function handleOcrUpload(file) {
        const originalPlaceholder = input.placeholder;
        input.value = '';
        input.placeholder = '📷 OCR 正在解析图片，请稍候...';
        input.disabled = true;
        sendBtn.disabled = true;
        if (ocrBtn) {
            ocrBtn.disabled = true;
            ocrBtn.style.opacity = 0.5;
        }

        const formData = new FormData();
        formData.append('file', file);

        try {
            const response = await fetch('/agent/api/v1/ocr', {
                method: 'POST',
                body: formData
            });

            const data = await response.json();
            const textToInsert = data.markdown || data.text;
            if (response.ok && textToInsert) {
                const startPos = input.selectionStart;
                const endPos = input.selectionEnd;
                input.value = input.value.substring(0, startPos) + textToInsert + input.value.substring(endPos);
                input.selectionStart = input.selectionEnd = startPos + textToInsert.length;
            } else {
                alert('OCR 解析失败: ' + (data.detail || '未知错误'));
            }
        } catch (error) {
            alert('OCR 请求出错: ' + error.message);
        } finally {
            input.placeholder = originalPlaceholder;
            input.disabled = false;
            sendBtn.disabled = false;
            if (ocrBtn) {
                ocrBtn.disabled = false;
                ocrBtn.style.opacity = 1;
            }
            input.focus();
        }
    }
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
    updateFacetPanelVisibility();

    // Load initial data
    fetchAllStats();
    performSearch(false);

    // Mount the default active tab
    const container = document.querySelector('.main-container') || document.body;
    setTimeout(() => _callLifecycle('onMount', container), 100);
});
