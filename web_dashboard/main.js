// 统一中枢核心逻辑 (Unified Dashboard Client)

// 状态管理
const state = {
    activeSource: 'all', // 'all', 'emails', 'rss'
    searchQuery: '',
    results: [],
    emailCount: 0,
    rssCount: 0,
    selectedCommandIndex: 0,
    currentTaskId: null,
    eventSource: null,
    offset: 0,
    limit: 40,
    hasMore: true,
    isLoading: false
};

// 初始化 Session ID
let sessionId = localStorage.getItem('agent_session_id');
if (!sessionId) {
    sessionId = 'web_dashboard_' + Math.random().toString(36).substring(2, 15);
    localStorage.setItem('agent_session_id', sessionId);
}

// 页面加载初始化
document.addEventListener('DOMContentLoaded', () => {
    initSearch();
    initUIEvents();
    fetchStats();
    performSearch();
    initIntersectionObserver();
});

function initIntersectionObserver() {
    const sentinel = document.getElementById('scroll-sentinel');
    if (!sentinel) return;
    
    const observer = new IntersectionObserver((entries) => {
        if (entries[0].isIntersecting && !state.isLoading && state.hasMore) {
            performSearch(true);
        }
    }, { rootMargin: '200px' });
    
    observer.observe(sentinel);
}

// 获取 Meilisearch 真实的数据统计信息
async function fetchStats() {
    try {
        const [emailsRes, rssRes, todosRes, sessionsRes] = await Promise.all([
            fetch('/meili/indexes/emails/stats').then(r => r.ok ? r.json() : { numberOfDocuments: 0 }).catch(() => ({ numberOfDocuments: 0 })),
            fetch('/meili/indexes/rss/stats').then(r => r.ok ? r.json() : { numberOfDocuments: 0 }).catch(() => ({ numberOfDocuments: 0 })),
            fetch('/agent/api/v1/todos').then(r => r.ok ? r.json() : { data: [] }).catch(() => ({ data: [] })),
            fetch('/agent/api/v1/sessions').then(r => r.ok ? r.json() : { data: [] }).catch(() => ({ data: [] }))
        ]);
        
        state.emailCount = emailsRes.numberOfDocuments || 0;
        state.rssCount = rssRes.numberOfDocuments || 0;
        state.todoCount = (todosRes.data || []).length;
        state.sessionCount = (sessionsRes.data || []).length;
        
        document.getElementById('badge-all').textContent = state.emailCount + state.rssCount;
        document.getElementById('badge-emails').textContent = state.emailCount;
        document.getElementById('badge-rss').textContent = state.rssCount;
        
        const badgeTodos = document.getElementById('badge-todos');
        if (badgeTodos) badgeTodos.textContent = state.todoCount;
        const badgeSessions = document.getElementById('badge-sessions');
        if (badgeSessions) badgeSessions.textContent = state.sessionCount;
    } catch (e) {
        console.error('Fetch stats failed:', e);
    }
}

// 搜索入口初始化与防抖
function initSearch() {
    const input = document.getElementById('search-input');
    let debounceTimer;
    
    input.addEventListener('input', (e) => {
        state.searchQuery = e.target.value;
        
        // 如果以 / 开头，触发指令面板，停止普通搜索
        if (state.searchQuery === '/') {
            openCommandModal();
            input.value = '';
            return;
        }
        
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
            performSearch();
        }, 250);
    });
}

// 检索核心方法
async function performSearch(isAppend = false) {
    if (state.isLoading) return;
    
    if (!isAppend) {
        state.offset = 0;
        state.hasMore = true;
        state.results = [];
    }
    
    if (!state.hasMore) return;
    
    state.isLoading = true;
    const grid = document.getElementById('results-grid');
    const countSpan = document.getElementById('results-count');
    const sentinel = document.getElementById('scroll-sentinel');
    
    if (!isAppend) {
        // 显示加载中
        grid.innerHTML = `
            <div class="loading-placeholder">
                <div class="spinner"></div>
                <p>正在检索中枢索引...</p>
            </div>
        `;
    }
    
    if (sentinel) sentinel.style.display = 'block';
    
    try {
        let newDocs = [];
        
        // 依据当前过滤源并发请求
        const queryPromises = [];
        
        if (state.activeSource === 'all' || state.activeSource === 'emails') {
            queryPromises.push(
                searchIndex('emails', state.searchQuery, state.offset, state.limit).then(res => 
                    res.map(d => ({ ...d, _source: 'emails' }))
                )
            );
        }
        if (state.activeSource === 'all' || state.activeSource === 'rss') {
            queryPromises.push(
                searchIndex('rss', state.searchQuery, state.offset, state.limit).then(res => 
                    res.map(d => ({ ...d, _source: 'rss' }))
                )
            );
        }
        if (state.activeSource === 'todos') {
            // 如果仅选择 todos，或者未来在 all 里也展示，可以加在 all 判断里
            // 根据需求，用户如果点了 todos tab 就只展示 todos。 这里我们假设 todos 数量不多，一次性拉取，不支持追加分页。
            if (!isAppend) {
                queryPromises.push(
                    fetchTodos().then(res => 
                        res.filter(t => !state.searchQuery || t.title.includes(state.searchQuery) || (t.description && t.description.includes(state.searchQuery)))
                           .map(d => ({ ...d, _source: 'todos' }))
                    )
                );
            }
        }
        if (state.activeSource === 'sessions') {
            if (!isAppend) {
                queryPromises.push(
                    fetchSessions().then(res => 
                        res.filter(s => !state.searchQuery || (s.goal && s.goal.includes(state.searchQuery)) || s.channel.includes(state.searchQuery))
                           .map(d => ({ ...d, _source: 'sessions' }))
                    )
                );
            }
        }
        
        const resultsArray = await Promise.all(queryPromises);
        newDocs = resultsArray.flat();
        
        if (newDocs.length === 0) {
            state.hasMore = false;
        } else {
            state.results = [...state.results, ...newDocs];
            // 根据时间或获取顺序重新排序
            state.results.sort((a, b) => {
                const timeA = new Date(a.fetched_at || a.email_date || a.updated_at || 0);
                const timeB = new Date(b.fetched_at || b.email_date || b.updated_at || 0);
                return timeB - timeA;
            });
            state.offset += state.limit;
        }
        
        renderResults();
        
        countSpan.textContent = `找到 ${state.results.length} 个结果${state.hasMore ? '' : ' (已触底)'}`;
    } catch (e) {
        if (!isAppend) {
            grid.innerHTML = `<div class="loading-placeholder"><p class="error">❌ 检索失败: ${e.message}</p></div>`;
        } else {
            console.error("加载更多失败:", e);
        }
    } finally {
        state.isLoading = false;
        if (!state.hasMore && sentinel) {
            sentinel.style.display = 'none';
        }
    }
}

// Meilisearch 索引检索 API
async function searchIndex(indexUid, query, offset = 0, limit = 40) {
    try {
        const sortField = indexUid === 'emails' ? 'email_date:desc' : 'published:desc';
        const body = {
            q: query,
            limit: limit,
            offset: offset,
            sort: [sortField],
            attributesToHighlight: ['subject', 'plain_text', 'summary', 'title', 'content'],
            highlightPreTag: '<mark>',
            highlightPostTag: '</mark>'
        };
        
        const res = await fetch(`/meili/indexes/${indexUid}/search`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(body)
        });
        
        if (!res.ok) return [];
        const data = await res.json();
        return data.hits || [];
    } catch (e) {
        console.error(`Search ${indexUid} failed:`, e);
        return [];
    }
}

// 抓取 TODO API
async function fetchTodos() {
    try {
        const res = await fetch(`/agent/api/v1/todos`);
        if (!res.ok) return [];
        const data = await res.json();
        return data.data || [];
    } catch (e) {
        console.error(`Fetch todos failed:`, e);
        return [];
    }
}

// 渲染结果卡片
function renderResults() {
    const grid = document.getElementById('results-grid');
    if (state.results.length === 0) {
        grid.innerHTML = `<div class="loading-placeholder"><p>🔍 未找到任何匹配内容。</p></div>`;
        return;
    }
    
    grid.innerHTML = state.results.map(doc => {
        if (doc._source === 'emails') {
            return renderEmailCard(doc);
        } else if (doc._source === 'todos') {
            return renderTodoCard(doc);
        } else if (doc._source === 'sessions') {
            return renderSessionCard(doc);
        } else {
            return renderRssCard(doc);
        }
    }).join('');
    
    // 绑定卡片操作事件
    document.querySelectorAll('.btn-reprocess').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const uid = e.target.getAttribute('data-uid');
            const account = e.target.getAttribute('data-account');
            triggerCommand(`/reprocess account=${account} uid=${uid}`);
        });
    });

    document.querySelectorAll('.btn-view-original').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const uid = e.target.getAttribute('data-uid');
            const account = e.target.getAttribute('data-account');
            window.open(`/agent/api/v1/email/html?account=${encodeURIComponent(account)}&uid=${encodeURIComponent(uid)}`, '_blank');
        });
    });
}

// 文本安全过滤与截断（保留高亮且不破坏 DOM）
function safeSnippet(html, length = 300) {
    if (!html) return '';
    let text = html.replace(/<mark>/gi, '%%MARK%%').replace(/<\/mark>/gi, '%%/MARK%%');
    text = text.replace(/<[^>]+>/g, ' ');
    text = text.replace(/</g, "&lt;").replace(/>/g, "&gt;");
    if (text.length > length) text = text.substring(0, length) + '...';
    text = text.replace(/%%MARK%%/g, '<mark>').replace(/%%\/MARK%%/g, '</mark>');
    const openCount = (text.match(/<mark>/g) || []).length;
    const closeCount = (text.match(/<\/mark>/g) || []).length;
    if (openCount > closeCount) text += '</mark>';
    return text;
}

// 渲染邮件卡片
function renderEmailCard(doc) {
    // 获取高亮或正常字段
    const subject = safeSnippet(doc._formatted?.subject || doc.subject || '无主题', 200);
    const sender = safeSnippet(doc.sender || '未知发件人', 100);
    const plainText = safeSnippet(doc._formatted?.plain_text || doc.plain_text || '', 300);
    const summary = safeSnippet(doc._formatted?.summary || doc.summary || '', 500);
    const date = doc.email_date || doc.fetched_at || '';
    
    const summaryBlock = summary ? `<div class="card-summary" style="background:#f0f7ff; padding:8px; margin-bottom:8px; border-left: 3px solid #0066cc; border-radius: 4px; font-size: 0.9em; color:#333;"><strong>🤖 AI 摘要：</strong><br/>${summary}</div>` : '';

    return `
        <div class="result-card">
            <div class="card-meta">
                <span class="tag tag-email">📧 邮件</span>
                <span>${sender}</span>
                <span>${date}</span>
                <span>账户: ${doc.account_name}</span>
            </div>
            <div class="card-title">${subject}</div>
            ${summaryBlock}
            <div class="card-snippet">${plainText}</div>
            <div class="card-actions">
                <button class="card-btn btn-reprocess" data-uid="${doc.uid}" data-account="${doc.account_name}">🔄 重新解析</button>
                <button class="card-btn btn-view-original" data-uid="${doc.uid}" data-account="${doc.account_name}">👁️ 查看原文</button>
            </div>
        </div>
    `;
}

// 渲染 RSS 卡片
function renderRssCard(doc) {
    const title = safeSnippet(doc._formatted?.title || doc.title || '无标题', 200);
    const site = safeSnippet(doc.node_name || 'RSS', 100);
    const content = safeSnippet(doc._formatted?.content || doc.content || '', 300);
    const date = doc.published || doc.fetched_at || '';
    
    return `
        <div class="result-card">
            <div class="card-meta">
                <span class="tag tag-rss">📰 RSS</span>
                <span>来源: ${site}</span>
                <span>发布: ${date}</span>
            </div>
            <div class="card-title"><a href="${doc.link}" target="_blank" style="color:inherit;text-decoration:none;">${title}</a></div>
            <div class="card-snippet">${content}</div>
        </div>
    `;
}

// 渲染 TODO 卡片
function renderTodoCard(doc) {
    const title = safeSnippet(doc.title || '无标题', 200);
    const desc = safeSnippet(doc.description || '', 500);
    const date = doc.updated_at ? doc.updated_at.replace('T', ' ').substring(0, 16) : '';
    const kindIcon = doc.kind === 'code' ? '💻' : '📝';
    
    return `
        <div class="result-card" style="border-left: 4px solid #10b981;">
            <div class="card-meta">
                <span class="tag" style="background:#10b98122;color:#10b981;">✅ TODO</span>
                <span>${kindIcon} ${doc.kind}</span>
                ${doc.project ? `<span>📦 ${doc.project}</span>` : ''}
                <span>更新: ${date}</span>
                <span>状态: <strong>${doc.status}</strong></span>
            </div>
            <div class="card-title">${title}</div>
            ${desc ? `<div class="card-snippet" style="background:#f8fafc; padding:8px; border-radius:4px; color:#475569;">${desc}</div>` : ''}
        </div>
    `;
}

// 初始化交互事件
function initUIEvents() {
    // 侧边栏过滤切换
    document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
            const currentBtn = e.currentTarget;
            currentBtn.classList.add('active');
            state.activeSource = currentBtn.getAttribute('data-source');
            performSearch();
        });
    });

    // 指令栏快捷键和事件
    document.querySelector('.search-shortcut').addEventListener('click', openCommandModal);
    
    // 点击模态框遮罩关闭
    const modal = document.getElementById('command-modal');
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeCommandModal();
    });

    // 监听键盘按键
    document.addEventListener('keydown', (e) => {
        // Esc 关闭指令弹窗
        if (e.key === 'Escape') {
            closeCommandModal();
        }
        
        // 斜杠 / 唤起指令面板 (在非 input 输入框内按时)
        if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
            e.preventDefault();
            openCommandModal();
        }
        
        // 弹窗内的键盘上下导航
        if (modal.style.display === 'flex') {
            handleCommandNavigation(e);
        }
    });

    // 绑定指令列表点击
    document.querySelectorAll('.command-item').forEach((item, index) => {
        item.addEventListener('mouseenter', () => {
            state.selectedCommandIndex = index;
            updateCommandListSelection(false);
        });
        item.addEventListener('click', () => {
            const cmd = item.getAttribute('data-cmd');
            closeCommandModal();
            triggerCommand(cmd);
        });
    });

    // 指令输入框回车提交
    document.getElementById('command-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            const cmd = e.target.value.trim();
            if (cmd) {
                closeCommandModal();
                triggerCommand(cmd);
            }
        }
    });

    // 终端展开折叠
    const termHeader = document.getElementById('terminal-header');
    termHeader.addEventListener('click', () => {
        const term = document.getElementById('terminal-window');
        term.classList.toggle('minimized');
        document.getElementById('term-toggle').textContent = term.classList.contains('minimized') ? '展开' : '收起';
    });

    document.getElementById('term-close').addEventListener('click', (e) => {
        e.stopPropagation();
        document.getElementById('terminal-window').style.display = 'none';
    });
}

// 唤起指令弹窗
function openCommandModal() {
    const modal = document.getElementById('command-modal');
    modal.style.display = 'flex';
    const input = document.getElementById('command-input');
    input.value = '';
    setTimeout(() => input.focus(), 50);
    
    state.selectedCommandIndex = 0;
    updateCommandListSelection(false);
}

// 关闭指令弹窗
function closeCommandModal() {
    document.getElementById('command-modal').style.display = 'none';
}

// 处理指令列表键盘上下键导航
function handleCommandNavigation(e) {
    const items = document.querySelectorAll('.command-item');
    if (items.length === 0) return;
    
    if (e.key === 'ArrowDown') {
        e.preventDefault();
        state.selectedCommandIndex = (state.selectedCommandIndex + 1) % items.length;
        updateCommandListSelection(true);
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        state.selectedCommandIndex = (state.selectedCommandIndex - 1 + items.length) % items.length;
        updateCommandListSelection(true);
    } else if (e.key === 'Enter' && document.activeElement.id !== 'command-input') {
        e.preventDefault();
        items[state.selectedCommandIndex].click();
    }
}

// 更新指令列表选中的高亮类
function updateCommandListSelection(fillInput = true) {
    const items = document.querySelectorAll('.command-item');
    items.forEach((item, index) => {
        if (index === state.selectedCommandIndex) {
            item.classList.add('selected');
            item.scrollIntoView({ block: 'nearest' });
            if (fillInput) {
                const cmd = item.getAttribute('data-cmd');
                const input = document.getElementById('command-input');
                if (input) {
                    input.value = cmd;
                }
            }
        } else {
            item.classList.remove('selected');
        }
    });
}

// 向 Agent 下发指令并拉起 SSE 任务终端
async function triggerCommand(commandText) {
    const term = document.getElementById('terminal-window');
    const termBody = document.getElementById('terminal-body');
    
    term.style.display = 'flex';
    term.classList.remove('minimized');
    document.getElementById('term-toggle').textContent = '收起';
    
    termBody.innerHTML = `<div class="terminal-line system">[*] 向 Agent 发送指令: ${commandText}</div>`;
    
    try {
        const res = await fetch('/agent/api/v1/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                session_id: sessionId,
                text: commandText
            })
        });
        
        if (!res.ok) {
            termBody.innerHTML += `<div class="terminal-line error">[!] HTTP 错误 ${res.status}: 接口请求失败</div>`;
            return;
        }
        
        const data = await res.json();
        
        if (data.type === 'sync') {
            // 同步应答直接渲染
            termBody.innerHTML += `<div class="terminal-line success">[Sync Response]</div>`;
            termBody.innerHTML += `<div class="terminal-line info">${formatTerminalOutput(data.response)}</div>`;
        } else if (data.type === 'async' && data.task_id) {
            // 异步任务启动流式监听
            state.currentTaskId = data.task_id;
            startTaskStream(data.task_id);
        } else {
            termBody.innerHTML += `<div class="terminal-line info">${JSON.stringify(data)}</div>`;
        }
        
        // 自动拉取最新 Meilisearch 状态以防新数据同步
        setTimeout(fetchStats, 2000);
        
    } catch (e) {
        termBody.innerHTML += `<div class="terminal-line error">[!] 触发异常: ${e.message}</div>`;
    }
}

// 格式化输出，处理 Markdown
function formatTerminalOutput(text) {
    if (!text) return '';
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\n/g, '<br>');
}

// 建立 SSE (Server-Sent Events) 任务日志连接
function startTaskStream(taskId) {
    if (state.eventSource) {
        state.eventSource.close();
    }
    
    const termBody = document.getElementById('terminal-body');
    termBody.innerHTML += `<div class="terminal-line system">[*] 已建立流连接，正在追踪 Task ID: ${taskId}</div>`;
    
    const url = `/agent/api/v1/task/stream?task_id=${taskId}&session_id=web_dashboard_session`;
    state.eventSource = new EventSource(url);
    
    let lastMsg = null;
    const finishedSubtasks = new Set();
    
    state.eventSource.onmessage = (event) => {
        const decoded = event.data;
        if (decoded === '[DONE]') {
            termBody.innerHTML += `<div class="terminal-line success">\n[*] 任务流已正常关闭 [DONE]</div>`;
            state.eventSource.close();
            state.eventSource = null;
            termBody.scrollTop = termBody.scrollHeight;
            return;
        }
        
        try {
            const data = jsonParseSafe(decoded);
            if (!data) return;
            
            const status = data.status;
            const msg = data.message;
            
            // 避免高频点点点和冗余信息
            if (msg && msg !== lastMsg) {
                termBody.innerHTML += `<div class="terminal-line info">[*] ${msg}</div>`;
                lastMsg = msg;
            } else if (msg) {
                // 如果是进度指示
                const lastLine = termBody.lastElementChild;
                if (lastLine && lastLine.textContent.startsWith('[*] ' + msg)) {
                    lastLine.innerHTML += '.';
                }
            }
            
            // 子任务进度
            const progress = data.progress;
            if (progress && progress.subtasks) {
                for (const sub of progress.subtasks) {
                    const subId = sub.id;
                    const subStatus = sub.status;
                    if ((subStatus === 'done' || subStatus === 'failed') && !finishedSubtasks.has(subId)) {
                        finishedSubtasks.add(subId);
                        const res = sub.result || sub.error || '';
                        const cssClass = subStatus === 'done' ? 'success' : 'error';
                        termBody.innerHTML += `
                            <div class="terminal-line ${cssClass}">
                                [Subtask ${subId}] ${sub.name} -> ${subStatus.toUpperCase()}:<br>${formatTerminalOutput(res)}
                            </div>
                        `;
                    }
                }
            }
            
            if (status === 'done' || status === 'completed' || status === 'failed' || status === 'error') {
                const cssClass = (status === 'done' || status === 'completed') ? 'success' : 'error';
                termBody.innerHTML += `<div class="terminal-line ${cssClass}">\n[*] 任务执行完毕，最终状态: ${status.toUpperCase()}!</div>`;
                state.eventSource.close();
                state.eventSource = null;
            }
            
            // 自动滚屏
            termBody.scrollTop = termBody.scrollHeight;
            
        } catch (e) {
            console.error('Parse event stream data failed:', e);
        }
    };
    
    state.eventSource.onerror = (e) => {
        termBody.innerHTML += `<div class="terminal-line error">[!] SSE 连接中断或发生错误</div>`;
        state.eventSource.close();
        state.eventSource = null;
        termBody.scrollTop = termBody.scrollHeight;
    };
}

function jsonParseSafe(str) {
    try {
        return JSON.parse(str);
    } catch (e) {
        return null;
    }
}

// =========================================================
// 聊天抽屉 (Chat Drawer) 逻辑
// =========================================================

let chatEventSource = null;
let currentChatTaskId = null;
let isChatBusy = false;

document.addEventListener('DOMContentLoaded', () => {
    initChatDrawer();
});

function initChatDrawer() {
    const fab = document.getElementById('chat-fab');
    const drawer = document.getElementById('chat-drawer');
    const closeBtn = document.getElementById('close-drawer-btn');
    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('chat-send-btn');
    
    if (!fab || !drawer) return;
    
    // 开关抽屉
    fab.addEventListener('click', () => {
        drawer.classList.add('open');
        input.focus();
    });
    
    closeBtn.addEventListener('click', () => {
        drawer.classList.remove('open');
    });
    
    // 自动高度和发送
    input.addEventListener('input', function() {
        this.style.height = '48px';
        this.style.height = (this.scrollHeight) + 'px';
    });
    
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendChatMessage();
        }
    });
    
    sendBtn.addEventListener('click', sendChatMessage);
}

async function sendChatMessage() {
    if (isChatBusy) return;
    
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;
    
    input.value = '';
    input.style.height = '48px';
    
    appendChatBubble('user', text);
    
    isChatBusy = true;
    updateChatStatus(true);
    
    // 显示等待状态气泡
    const thinkingId = 'thinking-' + Date.now();
    appendChatBubble('agent', '<div class="typing-dots"><span></span><span></span><span></span></div>', thinkingId);
    
    try {
        const res = await fetch('/agent/api/v1/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                session_id: sessionId, // 复用 dashboard session
                text: text
            })
        });
        
        const thinkingBubble = document.getElementById(thinkingId);
        
        if (!res.ok) {
            if (thinkingBubble) thinkingBubble.innerHTML = '<div class="bubble error">网络请求失败，请重试</div>';
            resetChatStatus();
            return;
        }
        
        const data = await res.json();
        
        if (data.type === 'sync') {
            // 同步应答
            if (thinkingBubble) {
                thinkingBubble.innerHTML = `<div class="bubble">${parseMarkdown(data.response)}</div>`;
            } else {
                appendChatBubble('agent', data.response);
            }
            resetChatStatus();
        } else if (data.type === 'async' && data.task_id) {
            // 异步任务，启动流式解析
            currentChatTaskId = data.task_id;
            startDrawerTaskStream(data.task_id, thinkingBubble);
        } else {
            if (thinkingBubble) thinkingBubble.innerHTML = `<div class="bubble error">未知响应格式</div>`;
            resetChatStatus();
        }
    } catch (e) {
        console.error('Chat error:', e);
        resetChatStatus();
    }
}

function startDrawerTaskStream(taskId, agentBubbleContainer) {
    if (chatEventSource) {
        chatEventSource.close();
    }
    
    const url = `/agent/api/v1/task/stream?task_id=${taskId}&session_id=${sessionId}`;
    chatEventSource = new EventSource(url);
    
    let lastMsg = null;
    const finishedSubtasks = new Set();
    
    // 初始化气泡内容为空，保留引用用于不断追加内容
    let bubbleContentHtml = '';
    const bubbleEl = agentBubbleContainer.querySelector('.bubble') || agentBubbleContainer.appendChild(document.createElement('div'));
    bubbleEl.className = 'bubble';
    bubbleEl.innerHTML = '';
    
    chatEventSource.onmessage = (event) => {
        const decoded = event.data;
        if (decoded === '[DONE]') {
            chatEventSource.close();
            chatEventSource = null;
            resetChatStatus();
            return;
        }
        
        try {
            const data = jsonParseSafe(decoded);
            if (!data) return;
            
            const status = data.status;
            let msg = data.message;
            let newlyAppended = false;
            
            // 系统状态/过程提示，我们使用一个小卡片附在上方，不污染主回答
            if (msg && msg !== lastMsg && status !== 'done' && status !== 'completed') {
                const sysLog = document.createElement('div');
                sysLog.className = 'chat-message system';
                sysLog.innerText = `[${status.toUpperCase()}] ${msg}`;
                agentBubbleContainer.before(sysLog);
                lastMsg = msg;
                newlyAppended = true;
            }
            
            // 子任务进度 (Tools: ops_workspace, web_clip 等)
            const progress = data.progress;
            if (progress && progress.subtasks) {
                for (const sub of progress.subtasks) {
                    const subId = sub.id;
                    const subStatus = sub.status;
                    if ((subStatus === 'done' || subStatus === 'failed') && !finishedSubtasks.has(subId)) {
                        finishedSubtasks.add(subId);
                        const res = sub.result || sub.error || '';
                        const cssColor = subStatus === 'done' ? '#8b5cf6' : 'var(--danger)';
                        
                        // 生成一个 Tool Call 结果的小卡片，拼接在 bubbleHTML 内部或者外部
                        bubbleContentHtml += `
                            <div class="subtask-box" style="border-left-color: ${cssColor}">
                                <strong>[Tool] ${sub.name}</strong><br>
                                <pre><code>${formatTerminalOutput(res)}</code></pre>
                            </div>
                        `;
                        newlyAppended = true;
                    }
                }
            }
            
            // 最终结果输出
            if ((status === 'done' || status === 'completed') && msg) {
                // 这个 msg 通常是最终的回答文本
                bubbleContentHtml += parseMarkdown(msg);
                newlyAppended = true;
            }
            
            if (newlyAppended) {
                bubbleEl.innerHTML = bubbleContentHtml;
                scrollToBottom();
            }
            
            if (status === 'done' || status === 'completed' || status === 'failed' || status === 'error') {
                chatEventSource.close();
                chatEventSource = null;
                resetChatStatus();
            }
            
        } catch (e) {
            console.error('Parse drawer stream event failed:', e);
        }
    };
    
    chatEventSource.onerror = () => {
        bubbleContentHtml += '<br><span style="color:var(--danger)">[连接已断开]</span>';
        bubbleEl.innerHTML = bubbleContentHtml;
        chatEventSource.close();
        resetChatStatus();
    };
}

function appendChatBubble(role, content, id = null) {
    const history = document.getElementById('chat-history');
    const msgDiv = document.createElement('div');
    msgDiv.className = `chat-message ${role}`;
    if (id) msgDiv.id = id;
    
    // 用户消息纯文本，Agent 消息可能带 HTML 或 Markdown
    if (role === 'user') {
        msgDiv.innerHTML = `<div class="bubble">${formatTerminalOutput(content)}</div>`;
    } else {
        // 直接使用传入的内容（如 typing-dots 或 markdown HTML）
        if (content.includes('typing-dots') || content.includes('bubble error')) {
            msgDiv.innerHTML = `<div class="bubble">${content}</div>`;
        } else {
            msgDiv.innerHTML = `<div class="bubble">${parseMarkdown(content)}</div>`;
        }
    }
    
    history.appendChild(msgDiv);
    scrollToBottom();
    return msgDiv;
}

function updateChatStatus(busy) {
    const dot = document.querySelector('.drawer-title .status-dot');
    const sendBtn = document.getElementById('chat-send-btn');
    if (busy) {
        dot.classList.add('busy');
        sendBtn.disabled = true;
    } else {
        dot.classList.remove('busy');
        sendBtn.disabled = false;
    }
}

function resetChatStatus() {
    isChatBusy = false;
    updateChatStatus(false);
}

function scrollToBottom() {
    const history = document.getElementById('chat-history');
    if (history) {
        history.scrollTop = history.scrollHeight;
    }
}

// -----------------------------------------------------------------------------
// Sessions 会话记录集成
// -----------------------------------------------------------------------------

async function fetchSessions() {
    try {
        const res = await fetch('/agent/api/v1/sessions');
        if (!res.ok) throw new Error('Fetch sessions failed');
        const json = await res.json();
        return json.data || [];
    } catch (e) {
        console.error('fetchSessions:', e);
        return [];
    }
}

function renderSessionCard(doc) {
    const safeChannel = safeSnippet(doc.channel || 'unknown', 50);
    const safeModel = safeSnippet(doc.last_model || 'unknown', 50);
    const safeGoal = safeSnippet(doc.goal || '闲聊模式...', 150);
    const safeStatus = safeSnippet(doc.status || 'chatting', 20);
    
    let dateStr = "未知时间";
    if (doc.updated_at) {
        const d = new Date(doc.updated_at * 1000);
        dateStr = d.toLocaleString();
    }
    
    // Status color
    let statusBadgeClass = 'tag-success'; // default to success colors
    if (safeStatus.toLowerCase() === 'working') statusBadgeClass = 'tag-danger';
    else if (safeStatus.toLowerCase() === 'archived') statusBadgeClass = 'tag-secondary';
    
    // Use session key mapping to an icon, if possible, or a default
    let sourceIcon = '💬';
    if (safeChannel.includes('feishu')) sourceIcon = '🕊️';
    if (safeChannel.includes('cli')) sourceIcon = '🖥️';
    if (safeChannel.includes('wecom')) sourceIcon = '🟢';
    
    return `
        <div class="result-card animate__animated animate__fadeInUp animate__faster">
            <div class="card-header">
                <div class="card-source">
                    <span class="source-icon">${sourceIcon}</span>
                    <span class="source-name" style="text-transform: capitalize;">${safeChannel}</span>
                </div>
                <div class="card-date">${dateStr}</div>
            </div>
            <div class="card-title" style="margin-bottom: 8px;">会话: <span style="font-size: 14px; color: var(--text-secondary);">${safeSnippet(doc.session_key || '', 100)}</span></div>
            <div class="card-snippet" style="background:#f8fafc; padding:8px; border-radius:4px; color:#475569; margin-bottom: 12px;">
                <strong>当前目标:</strong> ${safeGoal}
            </div>
            <div class="card-tags">
                <span class="tag ${statusBadgeClass}">Status: ${safeStatus}</span>
                <span class="tag tag-primary">Token: ${doc.token_usage || 0}</span>
                <span class="tag tag-warning">Model: ${safeModel}</span>
                <span class="tag tag-info">Msgs: ${doc.msg_count || 0}</span>
            </div>
        </div>
    `;
}

function parseMarkdown(text) {
    if (!text) return '';
    if (typeof marked !== 'undefined') {
        return marked.parse(text);
    }
    return formatTerminalOutput(text);
}
