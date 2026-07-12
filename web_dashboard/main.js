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
    eventSource: null
};

// 页面加载初始化
document.addEventListener('DOMContentLoaded', () => {
    initSearch();
    initUIEvents();
    fetchStats();
    performSearch();
});

// 获取 Meilisearch 统计信息 (通过 Search API 获取以适配只读 Search Key 限制)
async function fetchStats() {
    try {
        const [emailsRes, rssRes] = await Promise.all([
            fetch('/meili/indexes/emails/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ q: '', limit: 0 })
            }).then(r => r.ok ? r.json() : { estimatedTotalHits: 0 }).catch(() => ({ estimatedTotalHits: 0 })),
            fetch('/meili/indexes/rss/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ q: '', limit: 0 })
            }).then(r => r.ok ? r.json() : { estimatedTotalHits: 0 }).catch(() => ({ estimatedTotalHits: 0 }))
        ]);
        
        state.emailCount = emailsRes.estimatedTotalHits || 0;
        state.rssCount = rssRes.estimatedTotalHits || 0;
        
        document.getElementById('badge-all').textContent = state.emailCount + state.rssCount;
        document.getElementById('badge-emails').textContent = state.emailCount;
        document.getElementById('badge-rss').textContent = state.rssCount;
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
async function performSearch() {
    const grid = document.getElementById('results-grid');
    const countSpan = document.getElementById('results-count');
    
    // 显示加载中
    grid.innerHTML = `
        <div class="loading-placeholder">
            <div class="spinner"></div>
            <p>正在检索中枢索引...</p>
        </div>
    `;
    
    try {
        let docs = [];
        
        // 依据当前过滤源并发请求
        const queryPromises = [];
        
        if (state.activeSource === 'all' || state.activeSource === 'emails') {
            queryPromises.push(
                searchIndex('emails', state.searchQuery).then(res => 
                    res.map(d => ({ ...d, _source: 'emails' }))
                )
            );
        }
        if (state.activeSource === 'all' || state.activeSource === 'rss') {
            queryPromises.push(
                searchIndex('rss', state.searchQuery).then(res => 
                    res.map(d => ({ ...d, _source: 'rss' }))
                )
            );
        }
        
        const resultsArray = await Promise.all(queryPromises);
        docs = resultsArray.flat();
        
        // 根据时间或获取顺序重新排序
        docs.sort((a, b) => {
            const timeA = new Date(a.fetched_at || a.email_date || 0);
            const timeB = new Date(b.fetched_at || b.email_date || 0);
            return timeB - timeA;
        });
        
        state.results = docs;
        renderResults();
        
        countSpan.textContent = `找到 ${docs.length} 个结果`;
    } catch (e) {
        grid.innerHTML = `<div class="loading-placeholder"><p class="error">❌ 检索失败: ${e.message}</p></div>`;
    }
}

// Meilisearch 索引检索 API
async function searchIndex(indexUid, query) {
    try {
        const body = {
            q: query,
            limit: 40,
            attributesToHighlight: ['subject', 'plain_text', 'title', 'content'],
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
}

// 渲染邮件卡片
function renderEmailCard(doc) {
    // 获取高亮或正常字段
    const subject = doc._formatted?.subject || doc.subject || '无主题';
    const sender = doc.sender || '未知发件人';
    const plainText = doc._formatted?.plain_text || doc.plain_text || '';
    const date = doc.email_date || doc.fetched_at || '';
    
    return `
        <div class="result-card">
            <div class="card-meta">
                <span class="tag tag-email">📧 邮件</span>
                <span>${sender}</span>
                <span>${date}</span>
                <span>账户: ${doc.account_name}</span>
            </div>
            <div class="card-title">${subject}</div>
            <div class="card-snippet">${plainText.substring(0, 300)}...</div>
            <div class="card-actions">
                <button class="card-btn btn-reprocess" data-uid="${doc.uid}" data-account="${doc.account_name}">🔄 重新解析</button>
            </div>
        </div>
    `;
}

// 渲染 RSS 卡片
function renderRssCard(doc) {
    const title = doc._formatted?.title || doc.title || '无标题';
    const site = doc.node_name || 'RSS';
    const content = doc._formatted?.content || doc.content || '';
    const date = doc.published || doc.fetched_at || '';
    
    return `
        <div class="result-card">
            <div class="card-meta">
                <span class="tag tag-rss">📰 RSS</span>
                <span>来源: ${site}</span>
                <span>发布: ${date}</span>
            </div>
            <div class="card-title"><a href="${doc.link}" target="_blank" style="color:inherit;text-decoration:none;">${title}</a></div>
            <div class="card-snippet">${content.substring(0, 300)}...</div>
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
    document.querySelectorAll('.command-item').forEach(item => {
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
    updateCommandListSelection();
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
        updateCommandListSelection();
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        state.selectedCommandIndex = (state.selectedCommandIndex - 1 + items.length) % items.length;
        updateCommandListSelection();
    } else if (e.key === 'Enter' && document.activeElement.id !== 'command-input') {
        e.preventDefault();
        items[state.selectedCommandIndex].click();
    }
}

// 更新指令列表选中的高亮类
function updateCommandListSelection() {
    const items = document.querySelectorAll('.command-item');
    items.forEach((item, index) => {
        if (index === state.selectedCommandIndex) {
            item.classList.add('selected');
            item.scrollIntoView({ block: 'nearest' });
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
                session_id: 'web_dashboard_session',
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
