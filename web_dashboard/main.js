// ============================================================
//  Auth guard: check sessionStorage, redirect to login if needed
// ============================================================
if (!sessionStorage.getItem('dashboard_auth')) {
    window.location.href = '/login.html';
}

// ============================================================
//  Unified Dashboard — Core Orchestrator & State Management
// ============================================================

// ---- Global State ----
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
    lastFacets: null,        // Meilisearch facetDistribution from last response
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
//  Core Helpers
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

function jsonParseSafe(s) {
    try { return JSON.parse(s); } catch { return null; }
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
    if (state.lastFacets && typeof renderFacetPanel === 'function') {
        renderFacetPanel(state.lastFacets);
    }
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
    if (sentinel) {
        sentinel.style.display = '';
        const spinnerEl = sentinel.querySelector('.spinner');
        if (spinnerEl) spinnerEl.style.display = '';
    }
}

function hideSearchSpinner() {
    const sentinel = document.getElementById('scroll-sentinel');
    if (!sentinel) return;
    const spinnerEl = sentinel.querySelector('.spinner');
    if (spinnerEl && !state.hasMore) spinnerEl.style.display = 'none';
    if (!state.hasMore) {
        const textSpan = sentinel.querySelector('span');
        if (textSpan) textSpan.textContent = state.results.length === 0 ? '' : '— 已加载全部结果 —';
    }
}

// ============================================================
//  Core: Unified Results Render
// ============================================================
function renderResults() {
    const grid = document.getElementById('results-grid');
    if (!grid) return;

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
            if (acc && uid && typeof triggerCommand === 'function') {
                triggerCommand(`/mail_reprocess ${acc} ${uid}`);
            }
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
            if (typeof switchChatSession === 'function') {
                switchChatSession(sessionKey, false, title);
            }
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
    if (!el) return;
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
//  Init: Search & Filter Events
// ============================================================
function initSearch() {
    const input = document.getElementById('search-input');
    if (!input) return;
    let debounceTimer;
    input.addEventListener('input', () => {
        clearTimeout(debounceTimer);
        const val = input.value.trim();
        if (val === '/') {
            if (typeof openCommandModal === 'function') openCommandModal();
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

function initIntersectionObserver() {
    const sentinel = document.getElementById('scroll-sentinel');
    if (!sentinel) return;
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting && !state.isLoading && state.hasMore) {
                performSearch(true);
            }
        });
    }, { rootMargin: '200px' });
    observer.observe(sentinel);
}

function _callLifecycle(hook, container) {
    const mod = tabModules[state.activeSource];
    if (mod && typeof mod[hook] === 'function') {
        try { mod[hook](container); } catch(e) { console.warn('[lifecycle]', hook, e); }
    }
}

function initFilterButtons() {
    const container = document.querySelector('.app-container') || document.body;
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
            if (typeof updateFacetPanelVisibility === 'function') {
                updateFacetPanelVisibility();
            }

            // Mount new tab (deferred so render completes first)
            setTimeout(() => _callLifecycle('onMount', container), 50);
        });
    });
}

// ============================================================
//  Sidebar Drawer & Collapse Controls
// ============================================================
function initSidebarDrawer() {
    const container = document.querySelector('.app-container');
    const collapseBtn = document.getElementById('sidebar-collapse-btn');
    const drawerBtn = document.getElementById('sidebar-drawer-btn');
    const backdrop = document.getElementById('sidebar-backdrop');
    
    if (!container) return;
    
    // 读取本地持久化偏好
    const isCollapsed = localStorage.getItem('lite_agent_sidebar_collapsed') === 'true';
    if (isCollapsed) {
        container.classList.add('sidebar-collapsed');
    }
    
    const toggleSidebar = (forceState) => {
        const collapsed = forceState !== undefined ? forceState : !container.classList.contains('sidebar-collapsed');
        if (collapsed) {
            container.classList.add('sidebar-collapsed');
        } else {
            container.classList.remove('sidebar-collapsed');
        }
        localStorage.setItem('lite_agent_sidebar_collapsed', String(collapsed));
    };
    
    if (collapseBtn) collapseBtn.addEventListener('click', () => toggleSidebar(true));
    if (drawerBtn) drawerBtn.addEventListener('click', () => toggleSidebar());
    if (backdrop) backdrop.addEventListener('click', () => toggleSidebar(true));
    
    // 快捷键 '[' 或 'Ctrl+B' / 'Cmd+B' 快速折叠/展开侧边栏
    document.addEventListener('keydown', (e) => {
        const isInput = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
        if (isInput) {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
                e.preventDefault();
                toggleSidebar();
            }
            return;
        }
        if (e.key === '[' || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b')) {
            e.preventDefault();
            toggleSidebar();
        }
    });
}

// ============================================================
//  Keyboard Shortcuts
// ============================================================
function initKeyboard() {
    document.addEventListener('keydown', (e) => {
        const modal = document.getElementById('command-modal');
        const isModalOpen = modal && modal.style.display === 'flex';
        const activeEl = document.activeElement;
        const isInput = activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'TEXTAREA');

        // Command modal navigation
        if (isModalOpen) {
            if (e.key === 'Escape') {
                if (typeof closeCommandModal === 'function') closeCommandModal();
                return;
            }
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                const items = document.querySelectorAll('.command-item');
                if (items.length) state.selectedCommandIndex = Math.min(state.selectedCommandIndex + 1, items.length - 1);
                if (typeof updateCommandSelection === 'function') updateCommandSelection();
                const cmdInput = document.getElementById('command-input');
                if (cmdInput && typeof getSelectedCommandText === 'function') cmdInput.value = getSelectedCommandText();
                return;
            }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                state.selectedCommandIndex = Math.max(0, state.selectedCommandIndex - 1);
                if (typeof updateCommandSelection === 'function') updateCommandSelection();
                const cmdInput = document.getElementById('command-input');
                if (cmdInput && typeof getSelectedCommandText === 'function') cmdInput.value = getSelectedCommandText();
                return;
            }
            if (e.key === 'Enter') {
                e.preventDefault();
                const cmd = document.getElementById('command-input')?.value.trim();
                if (cmd && typeof triggerCommand === 'function') triggerCommand(cmd);
                if (typeof closeCommandModal === 'function') closeCommandModal();
                return;
            }
            return;
        }

        // Global shortcuts
        if (e.key === '/' && !isInput) {
            e.preventDefault();
            if (typeof openCommandModal === 'function') openCommandModal();
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
//  Sidebar Collapse Toggle
// ============================================================
function initSidebarCollapse() {
    const btn = document.getElementById('sidebar-collapse-btn');
    const sidebar = document.querySelector('.sidebar');
    if (!btn || !sidebar) return;

    btn.addEventListener('click', () => {
        sidebar.classList.toggle('collapsed');
        const isCollapsed = sidebar.classList.contains('collapsed');
        localStorage.setItem('sidebar_collapsed', isCollapsed ? 'true' : 'false');
    });

    if (localStorage.getItem('sidebar_collapsed') === 'true') {
        sidebar.classList.add('collapsed');
    }
}

// ============================================================
//  Bootstrap
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    initSidebarDrawer();
    initSearch();
    initFilterButtons();
    initSidebarCollapse();
    initIntersectionObserver();
    if (typeof initTerminal === 'function') initTerminal();
    if (typeof initChatDrawer === 'function') initChatDrawer();
    initKeyboard();
    if (typeof initModalClose === 'function') initModalClose();
    if (typeof initFacetPanelEvents === 'function') initFacetPanelEvents();
    if (typeof updateFacetPanelVisibility === 'function') updateFacetPanelVisibility();

    // Load initial data
    fetchAllStats();
    performSearch(false);

    // Mount the default active tab
    const container = document.querySelector('.app-container') || document.body;
    setTimeout(() => _callLifecycle('onMount', container), 100);
});
