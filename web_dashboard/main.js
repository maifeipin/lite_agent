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
    isFacetPanelVisible: false,
    expandedFacetGroups: new Set(),
    activeChatSessionKey: 'api:dashboard_default',
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
    // Session: view messages -> switch to Chat Assistant drawer
    document.querySelectorAll('.btn-view-session, .btn-open-session').forEach(btn => {
        btn.addEventListener('click', async () => {
            const sessionKey = btn.getAttribute('data-session');
            const title = btn.getAttribute('data-title') || '';
            if (!sessionKey) return;
            switchChatSession(sessionKey, false, title);
        });
    });

    // Call module-specific post-render hooks
    const activeMod = tabModules[state.activeSource];
    if (activeMod && typeof activeMod.onPostRender === 'function') {
        activeMod.onPostRender(grid);
    } else if (state.activeSource === 'all') {
        if (tabModules['todos'] && typeof tabModules['todos'].onPostRender === 'function') {
            tabModules['todos'].onPostRender(grid);
        }
    }
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
    const toggleBtn = document.getElementById('global-filter-toggle');
    const isSupported = FACET_SOURCES.has(state.activeSource);
    
    if (toggleBtn) {
        toggleBtn.style.display = isSupported ? 'flex' : 'none';
        
        // Update active filter badge
        let activeCount = 0;
        for (const vals of Object.values(state.activeFilters)) {
            if (vals && vals.length > 0) activeCount += vals.length;
        }
        const badge = document.getElementById('active-filter-count');
        if (badge) {
            badge.style.display = activeCount > 0 ? 'inline-block' : 'none';
            badge.textContent = activeCount;
        }
    }

    if (!panel) return;
    panel.style.display = (isSupported && state.isFacetPanelVisible) ? 'flex' : 'none';
}

function initFacetPanelEvents() {
    // Global Toggle
    const toggleBtn = document.getElementById('global-filter-toggle');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            state.isFacetPanelVisible = !state.isFacetPanelVisible;
            updateFacetPanelVisibility();
        });
    }

    // Expand/Collapse Group
    const panel = document.getElementById('facet-panel');
    if (panel) {
        panel.addEventListener('click', (e) => {
            if (e.target.classList.contains('facet-expand-btn')) {
                const groupKey = e.target.getAttribute('data-group');
                if (state.expandedFacetGroups.has(groupKey)) {
                    state.expandedFacetGroups.delete(groupKey);
                } else {
                    state.expandedFacetGroups.add(groupKey);
                }
                renderFacetPanel(state.lastFacets); // re-render panel
            }
        });
    }
}

function renderFacetPanel(facetDist) {
    if (!facetDist || !FACET_SOURCES.has(state.activeSource)) return;
    const panel = document.getElementById('facet-panel');
    if (!panel) return;

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
        const isExpanded = state.expandedFacetGroups.has(g.key);
        html += `<div class="facet-group"><div class="facet-group-title">${({category:'🗂 分类',topics:'🏷 主题',source:'📂 来源',type:'📄 类型'})[g.key] || g.key}</div>`;
        html += `<div class="facet-items-container">`;
        
        let hiddenCount = 0;
        for (let i = 0; i < g.items.length; i++) {
            const item = g.items[i];
            const shouldHide = i >= 6 && !item.checked;
            if (shouldHide) hiddenCount++;
            
            const id = `facet-${g.key}-${item.val.replace(/[^a-zA-Z0-9]/g, '_')}`;
            const hiddenStyle = (shouldHide && !isExpanded) ? ' style="display:none;"' : '';
            html += `<label class="facet-item"${hiddenStyle} for="${id}">`;
            html += `<input type="checkbox" id="${id}" data-facet="${g.key}" data-value="${item.val}" ${item.checked ? 'checked' : ''}>`;
            html += `<span>${h(item.val)}<b class="count">${item.count}</b></span>`;
            html += `</label>`;
        }
        html += `</div>`;
        
        if (hiddenCount > 0 || isExpanded) {
            const btnText = isExpanded ? '- 收起' : `+ 展开 (${hiddenCount})`;
            html += `<button class="facet-expand-btn" data-group="${g.key}">${btnText}</button>`;
        }
        html += `</div>`;
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

async function switchChatSession(sessionKey, isNew = false, title = '') {
    if (chatEventSource) {
        chatEventSource.close();
        chatEventSource = null;
    }
    state.activeChatSessionKey = sessionKey;

    const tagEl = document.getElementById('chat-session-key-tag');
    if (tagEl) {
        tagEl.textContent = `#${sessionKey.replace(/^api:/, '')}`;
    }

    const headerTitleEl = document.getElementById('chat-header-title');
    if (headerTitleEl) {
        if (title) {
            headerTitleEl.textContent = title;
        } else {
            headerTitleEl.textContent = '智能助理';
            fetch('/agent/api/v1/sessions?limit=50')
                .then(r => r.json())
                .then(d => {
                    const match = (d.sessions || []).find(s => s.session_key === sessionKey);
                    if (match && match.title) {
                        headerTitleEl.textContent = match.title;
                    }
                })
                .catch(() => {});
        }
    }

    const history = document.getElementById('chat-history');
    if (history) history.innerHTML = '';

    const drawer = document.getElementById('chat-drawer');
    if (drawer) drawer.classList.add('open');

    if (isNew) {
        appendChatBubble('agent', '你好！我是你的智能中枢助理。已为你开启全新会话，请问有什么可以帮你的？');
    } else {
        await loadSessionMessagesIntoDrawer(sessionKey);
    }
}

async function loadSessionMessagesIntoDrawer(sessionKey) {
    const history = document.getElementById('chat-history');
    if (!history) return;
    history.innerHTML = '<div class="session-loading" style="text-align:center;padding:20px;color:var(--text-muted)">正在载入历史对话...</div>';

    try {
        const r = await fetch(`/agent/api/v1/sessions/messages?session_key=${encodeURIComponent(sessionKey)}&limit=100`);
        const d = await r.json();
        history.innerHTML = '';
        const msgs = d.messages || [];

        if (msgs.length === 0) {
            appendChatBubble('agent', '该会话暂无历史消息。可以开始向我提问！');
            return;
        }

        for (const m of msgs) {
            const role = m.role || 'assistant';
            let bubbleContent = '';

            if (m.reasoning_content) {
                bubbleContent += `<details class="chat-reasoning"><summary>🧠 深度思考过程</summary><div class="reasoning-text">${marked.parse(m.reasoning_content)}</div></details>`;
            }

            if (m.tool_calls && Array.isArray(m.tool_calls)) {
                for (const tc of m.tool_calls) {
                    const name = (tc.function && tc.function.name) || tc.name || 'tool';
                    const args = (tc.function && tc.function.arguments) || tc.arguments || '';
                    const argsStr = typeof args === 'object' ? JSON.stringify(args, null, 2) : args;
                    bubbleContent += `<details class="chat-tool-call"><summary>🔧 调用技能/工具: ${h(name)}</summary><pre><code>${h(argsStr)}</code></pre></details>`;
                }
            }

            if (m.content) {
                if (role === 'user' || role === 'system') {
                    bubbleContent += h(m.content);
                } else {
                    bubbleContent += marked.parse(m.content);
                }
            } else if (!bubbleContent) {
                bubbleContent = '(无内容)';
            }

            appendChatBubble(role, bubbleContent);
        }
    } catch(e) {
        history.innerHTML = `<div style="color:var(--danger);padding:20px;text-align:center">载入失败: ${h(e.message)}</div>`;
    }
}

function initChatDrawer() {
    const fab = document.getElementById('chat-fab');
    const drawer = document.getElementById('chat-drawer');
    const closeBtn = document.getElementById('close-drawer-btn');
    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('chat-send-btn');
    const ocrBtn = document.getElementById('chat-ocr-btn');
    const ocrFileInput = document.getElementById('chat-ocr-file-input');

    const fullscreenBtn = document.getElementById('fullscreen-drawer-btn');
    const newSessionBtn = document.getElementById('new-session-btn');

    fab.addEventListener('click', () => drawer.classList.add('open'));
    closeBtn.addEventListener('click', () => {
        drawer.classList.remove('open');
        drawer.classList.remove('fullscreen');
    });

    if (newSessionBtn) {
        newSessionBtn.addEventListener('click', () => {
            const newKey = `api:dashboard_${Date.now()}`;
            switchChatSession(newKey, true);
        });
    }

    if (fullscreenBtn) {
        fullscreenBtn.addEventListener('click', () => {
            drawer.classList.toggle('fullscreen');
            const isFullscreen = drawer.classList.contains('fullscreen');
            fullscreenBtn.title = isFullscreen ? '退出全屏' : '全屏';
        });
    }

    const headerTitleEl = document.getElementById('chat-header-title');
    if (headerTitleEl) {
        headerTitleEl.addEventListener('dblclick', () => {
            const currentTitle = headerTitleEl.innerText === '智能助理' ? '' : headerTitleEl.innerText;
            const sessionKey = state.activeChatSessionKey || 'api:dashboard_default';
            showModal({
                title: '修改会话标题',
                icon: '✏️',
                input: { value: currentTitle, placeholder: '请输入新的会话标题...' },
                buttons: [
                    { text: '取消', class: 'modal-btn-secondary', onClick: (m) => m.close() },
                    {
                        text: '保存',
                        class: 'modal-btn-primary',
                        onClick: (m, newVal) => {
                            if (!newVal) return;
                            fetch('/agent/api/v1/session/title', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ session_key: sessionKey, title: newVal })
                            })
                            .then(r => r.json())
                            .then(res => {
                                if (res.status === 'ok') {
                                    headerTitleEl.innerText = newVal;
                                    m.close();
                                    if (typeof performSearch === 'function') performSearch(false);
                                }
                            });
                        }
                    }
                ]
            });
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

    if (ocrBtn && ocrFileInput) {
        ocrBtn.addEventListener('click', () => ocrFileInput.click());
        ocrFileInput.addEventListener('change', (e) => {
            const file = e.target.files[0];
            if (file) {
                handleOcrUpload(file);
            }
            ocrFileInput.value = '';
        });
    }

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

function renderMath(element) {
    if (typeof renderMathInElement === 'function' && element) {
        try {
            renderMathInElement(element, {
                delimiters: [
                    {left: '$$', right: '$$', display: true},
                    {left: '$', right: '$', display: false},
                    {left: '\\(', right: '\\)', display: false},
                    {left: '\\[', right: '\\]', display: true}
                ],
                throwOnError: false
            });
        } catch(e) {
            console.error('KaTeX render error:', e);
        }
    }
}

function appendChatBubble(role, html) {
    const history = document.getElementById('chat-history');
    if (!history) return;
    const div = document.createElement('div');
    div.className = `chat-message ${role}`;
    div.innerHTML = `<div class="bubble">${html}</div>`;
    history.appendChild(div);
    renderMath(div);
    history.scrollTop = history.scrollHeight;
}

function formatLogLineHtml(text) {
    let safe = h(text);
    safe = safe.replace(/(\[ORCH:[A-Z]+\])/g, '<span style="color:#a855f7;font-weight:bold">$1</span>');
    safe = safe.replace(/(\[WORKER:[^\]]+\])/g, '<span style="color:#f59e0b;font-weight:bold">$1</span>');
    safe = safe.replace(/(🔧 [^:]+:)/g, '<span style="color:#06b6d4">$1</span>');
    safe = safe.replace(/(🧠 \[LLM Request\])/g, '<span style="color:#ec4899">$1</span>');
    safe = safe.replace(/(✅ \[LLM Response\])/g, '<span style="color:#10b981">$1</span>');
    safe = safe.replace(/(⚠️ [^:]+:)/g, '<span style="color:#f97316">$1</span>');
    return safe;
}

function appendLiveLogLines(lines) {
    const indicator = document.getElementById('chat-typing-indicator');
    if (!indicator) return;
    const logBody = indicator.querySelector('.live-log-body');
    const logCount = indicator.querySelector('.live-log-count');
    if (!logBody) return;
    for (const line of lines) {
        const div = document.createElement('div');
        div.className = 'live-log-line';
        div.innerHTML = formatLogLineHtml(line);
        logBody.appendChild(div);
    }
    logBody.scrollTop = logBody.scrollHeight;
    if (logCount) {
        const realCount = logBody.querySelectorAll('.live-log-line:not(.placeholder-line)').length;
        logCount.textContent = realCount;
    }
}

function finishAgentResponse(finalMarkdownHtml, isError = false) {
    const indicator = document.getElementById('chat-typing-indicator');
    if (!indicator) {
        if (isError) {
            appendChatBubble('system', finalMarkdownHtml || '任务失败');
        } else {
            appendChatBubble('agent', finalMarkdownHtml || '任务完成 (无返回内容)');
        }
        return;
    }

    try {
        // 1. Remove typing dots & status header line
        const typingHeader = indicator.querySelector('.typing-dots')?.parentElement;
        if (typingHeader) typingHeader.remove();

        // 2. Collapse live execution log details and update summary (or remove if 0 logs)
        const logDetails = indicator.querySelector('.live-execution-log');
        if (logDetails) {
            const countEl = logDetails.querySelector('.live-log-count');
            const count = countEl ? parseInt(countEl.textContent || '0', 10) : 0;
            const realLogLines = logDetails.querySelectorAll('.live-log-line:not(.placeholder-line)');
            if (count === 0 && realLogLines.length === 0) {
                logDetails.remove();
            } else {
                logDetails.open = false;
                const summary = logDetails.querySelector('summary');
                if (summary) {
                    summary.innerHTML = isError ? `⚠️ 执行产生异常日志 (${count} 条)` : `📋 详细执行过程日志 (${count} 条)`;
                }
            }
        }

        // 3. Append response content below the log details inside bubble
        const bubble = indicator.querySelector('.bubble');
        if (bubble) {
            const responseDiv = document.createElement('div');
            responseDiv.className = 'chat-response-content';
            responseDiv.style.marginTop = '8px';
            responseDiv.innerHTML = finalMarkdownHtml || (isError ? '任务失败' : '任务完成 (无返回内容)');
            bubble.appendChild(responseDiv);
            renderMath(responseDiv);
        }
    } catch(e) {
        console.error('finishAgentResponse error:', e);
    } finally {
        // 4. Always remove typing indicator ID so it becomes a permanent chat message
        indicator.removeAttribute('id');

        // 5. Schedule a 1.5s delayed check to update Header title if refined by stage 2 LLM
        setTimeout(() => {
            const currentSession = state.activeChatSessionKey;
            if (currentSession) {
                fetch('/agent/api/v1/sessions?limit=50')
                    .then(r => r.json())
                    .then(d => {
                        const match = (d.sessions || []).find(s => s.session_key === currentSession);
                        if (match && match.title) {
                            const headerTitleEl = document.getElementById('chat-header-title');
                            if (headerTitleEl) headerTitleEl.textContent = match.title;
                        }
                    })
                    .catch(() => {});
            }
        }, 1500);
    }

    const history = document.getElementById('chat-history');
    if (history) history.scrollTop = history.scrollHeight;
}

function sendChatMessage(text) {
    if (chatEventSource) {
        chatEventSource.close();
        chatEventSource = null;
    }

    // Clean up any stale typing indicator element ID to prevent ID collisions
    const staleIndicator = document.getElementById('chat-typing-indicator');
    if (staleIndicator) staleIndicator.removeAttribute('id');

    const rawSessionId = state.activeChatSessionKey.replace(/^api:/, '');

    const typingEl = document.createElement('div');
    typingEl.className = 'chat-message agent';
    typingEl.id = 'chat-typing-indicator';
    typingEl.innerHTML = `
        <div class="bubble">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
                <div class="typing-dots"><span></span><span></span><span></span></div>
                <span style="font-size:0.85rem;color:var(--text-muted)" id="typing-status-text">Agent 正在处理任务...</span>
            </div>
            <details class="live-execution-log" open>
                <summary>⚡ 实时执行日志与调度过程 (<span class="live-log-count">0</span> 条)</summary>
                <div class="live-log-body">
                    <div class="live-log-line placeholder-line" style="color:var(--text-muted)">[*] 任务初始化，连通 Agent 路由中...</div>
                </div>
            </details>
        </div>`;

    const history = document.getElementById('chat-history');
    if (history) {
        history.appendChild(typingEl);
        history.scrollTop = history.scrollHeight;
    }

    fetch('/agent/api/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: rawSessionId, text }),
    })
    .then(r => r.json())
    .then(data => {
        if (data.title) {
            const headerTitleEl = document.getElementById('chat-header-title');
            if (headerTitleEl) headerTitleEl.textContent = data.title;
        }
        if (data.type === 'sync') {
            if (data.logs && Array.isArray(data.logs) && data.logs.length > 0) {
                appendLiveLogLines(data.logs);
            }
            const md = marked.parse(data.response || '(空)');
            finishAgentResponse(md);
        } else if (data.type === 'async') {
            subscribeChatStream(data.task_id, rawSessionId);
        }
    })
    .catch(e => {
        finishAgentResponse(`请求失败: ${e.message}`, true);
    });
}

function subscribeChatStream(taskId, rawSessionId) {
    const es = new EventSource(`/agent/api/v1/task/stream?task_id=${taskId}&session_id=${encodeURIComponent(rawSessionId)}`);
    chatEventSource = es;
    let bubbleHtml = '';

    es.onmessage = (e) => {
        if (e.data === '[DONE]') {
            es.close();
            chatEventSource = null;
            finishAgentResponse(bubbleHtml);
            return;
        }
        const d = jsonParseSafe(e.data);
        if (!d) return;

        if (d.logs && Array.isArray(d.logs) && d.logs.length > 0) {
            appendLiveLogLines(d.logs);
        }

        if (d.status === 'summarizing') {
            const indicator = document.getElementById('chat-typing-indicator');
            const typingText = indicator?.querySelector('#typing-status-text');
            if (typingText) typingText.textContent = '正在生成总结报告...';
        }

        if (d.status === 'done' || d.status === 'completed') {
            if (d.response || (d.progress && d.progress.result)) {
                bubbleHtml += marked.parse(d.response || d.progress.result);
            }
            es.close();
            chatEventSource = null;
            finishAgentResponse(bubbleHtml || d.message);
            return;
        }
        if (d.status === 'failed' || d.status === 'error') {
            es.close();
            chatEventSource = null;
            finishAgentResponse(d.message || d.error || '任务失败', true);
            return;
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
//  Universal Modal Component (通用 UI 对话框组件)
// ============================================================
function showModal(options = {}) {
    document.querySelectorAll('.universal-modal-overlay, .session-modal-overlay').forEach(el => el.remove());

    const overlay = document.createElement('div');
    overlay.className = 'universal-modal-overlay';

    let bodyContent = '';
    if (options.contentType === 'markdown' && options.content) {
        bodyContent = typeof marked !== 'undefined' ? marked.parse(options.content) : options.content;
    } else if (options.content) {
        bodyContent = options.content;
    }

    let inputHtml = '';
    if (options.input) {
        const val = (options.input.value || '').replace(/"/g, '&quot;');
        const ph = options.input.placeholder || '';
        inputHtml = `<input type="text" class="universal-modal-input" value="${val}" placeholder="${ph}" id="universal-modal-input-field" />`;
    }

    let buttonsHtml = '';
    if (options.buttons && options.buttons.length > 0) {
        buttonsHtml = `<div class="universal-modal-footer">` +
            options.buttons.map((btn, idx) => `<button class="modal-btn ${btn.class || 'modal-btn-secondary'}" data-btn-idx="${idx}">${btn.text}</button>`).join('') +
            `</div>`;
    }

    const modalHtml = `
        <div class="universal-modal" style="${options.width ? 'width:' + options.width : ''}">
            <div class="universal-modal-header">
                ${options.icon ? `<span class="universal-modal-icon">${options.icon}</span>` : ''}
                <span class="universal-modal-title">${options.title || '提示'}</span>
                <button class="universal-modal-close">&times;</button>
            </div>
            <div class="universal-modal-body">
                ${bodyContent}
                ${inputHtml}
            </div>
            ${buttonsHtml}
        </div>
    `;

    overlay.innerHTML = modalHtml;
    document.body.appendChild(overlay);
    renderMath(overlay.querySelector('.universal-modal-body'));

    const closeModal = () => overlay.remove();

    const closeBtn = overlay.querySelector('.universal-modal-close');
    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeModal();
    });

    const handleKeyDown = (e) => {
        if (e.key === 'Escape') {
            closeModal();
            document.removeEventListener('keydown', handleKeyDown);
        }
    };
    document.addEventListener('keydown', handleKeyDown);

    const inputEl = overlay.querySelector('#universal-modal-input-field');
    if (inputEl) {
        inputEl.focus();
        inputEl.select();
        inputEl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const primaryBtn = options.buttons ? options.buttons.find(b => b.class && b.class.includes('primary')) : null;
                if (primaryBtn && primaryBtn.onClick) {
                    primaryBtn.onClick({ close: closeModal }, inputEl.value.trim());
                }
            }
        });
    }

    if (options.buttons) {
        options.buttons.forEach((btn, idx) => {
            const btnEl = overlay.querySelector(`[data-btn-idx="${idx}"]`);
            if (btnEl) {
                btnEl.addEventListener('click', () => {
                    const inputVal = inputEl ? inputEl.value.trim() : '';
                    if (btn.onClick) {
                        btn.onClick({ close: closeModal }, inputVal);
                    } else {
                        closeModal();
                    }
                });
            }
        });
    }
}

function showSessionMessagesModal(sessionKey, messages) {
    if (!messages || messages.length === 0) {
        showModal({
            title: `会话消息历史 (${sessionKey})`,
            icon: '💬',
            content: '*（暂无历史消息）*',
            contentType: 'markdown'
        });
        return;
    }

    const roleIcons = { user: '👤 **用户**', assistant: '🤖 **AI**', system: '⚙️ **System**', tool: '🔧 **Tool**' };
    const mdText = messages.map(m => {
        const iconStr = roleIcons[m.role] || `💬 **${m.role}**`;
        const timeStr = m.time ? ` *(${new Date(m.time * 1000).toLocaleTimeString('zh-CN')})*` : '';
        const safeContent = h(m.content || '(空)');
        return `### ${iconStr}${timeStr}\n${safeContent}`;
    }).join('\n\n---\n\n');

    showModal({
        title: `会话消息历史`,
        icon: '📜',
        content: mdText,
        contentType: 'markdown',
        width: '680px'
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
    initFacetPanelEvents();
    updateFacetPanelVisibility();

    // Load initial data
    fetchAllStats();
    performSearch(false);

    // Mount the default active tab
    const container = document.querySelector('.main-container') || document.body;
    setTimeout(() => _callLifecycle('onMount', container), 100);
});
