// ============================================================
//  Chat Assistant Drawer Component (智能助理抽屉、SSE 流式与多模态)
// ============================================================

let chatEventSource = null;

async function switchChatSession(sessionKey, isNew = false, title = '') {
    if (chatEventSource) {
        chatEventSource.close();
        chatEventSource = null;
    }
    if (typeof state !== 'undefined') {
        state.activeChatSessionKey = sessionKey;
    }

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
                bubbleContent += `<details class="chat-reasoning"><summary>🧠 深度思考过程</summary><div class="reasoning-text">${typeof marked !== 'undefined' ? marked.parse(m.reasoning_content) : m.reasoning_content}</div></details>`;
            }

            if (m.tool_calls && Array.isArray(m.tool_calls)) {
                for (const tc of m.tool_calls) {
                    const name = (tc.function && tc.function.name) || tc.name || 'tool';
                    const args = (tc.function && tc.function.arguments) || tc.arguments || '';
                    const argsStr = typeof args === 'object' ? JSON.stringify(args, null, 2) : args;
                    const safeName = typeof h === 'function' ? h(name) : name;
                    const safeArgs = typeof h === 'function' ? h(argsStr) : argsStr;
                    bubbleContent += `<details class="chat-tool-call"><summary>🔧 调用技能/工具: ${safeName}</summary><pre><code>${safeArgs}</code></pre></details>`;
                }
            }

            if (m.content) {
                if (role === 'user' || role === 'system') {
                    bubbleContent += typeof h === 'function' ? h(m.content) : m.content;
                } else {
                    bubbleContent += typeof marked !== 'undefined' ? marked.parse(m.content) : m.content;
                }
            } else if (!bubbleContent) {
                bubbleContent = '(无内容)';
            }

            appendChatBubble(role, bubbleContent);
        }
    } catch(e) {
        const errMsg = typeof h === 'function' ? h(e.message) : e.message;
        history.innerHTML = `<div style="color:var(--danger);padding:20px;text-align:center">载入失败: ${errMsg}</div>`;
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

    if (fab && drawer) fab.addEventListener('click', () => drawer.classList.add('open'));
    if (closeBtn && drawer) {
        closeBtn.addEventListener('click', () => {
            drawer.classList.remove('open');
            drawer.classList.remove('fullscreen');
        });
    }

    if (newSessionBtn) {
        newSessionBtn.addEventListener('click', () => {
            const newKey = `api:dashboard_${Date.now()}`;
            switchChatSession(newKey, true);
        });
    }

    if (fullscreenBtn && drawer) {
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
            const sessionKey = (typeof state !== 'undefined' && state.activeChatSessionKey) ? state.activeChatSessionKey : 'api:dashboard_default';
            if (typeof showModal === 'function') {
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
            }
        });
    }

    function doSend() {
        if (!input) return;
        const text = input.value.trim();
        if (!text) return;
        const safeText = typeof h === 'function' ? h(text) : text;
        appendChatBubble('user', safeText);
        input.value = '';
        sendChatMessage(text);
    }

    if (sendBtn) sendBtn.addEventListener('click', doSend);
    if (input) {
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                doSend();
            }
        });
    }

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

    if (input) {
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
    }

    async function handleOcrUpload(file) {
        if (!input) return;
        const originalPlaceholder = input.placeholder;
        input.value = '';
        input.placeholder = '📷 OCR 正在解析图片，请稍候...';
        input.disabled = true;
        if (sendBtn) sendBtn.disabled = true;
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
            if (sendBtn) sendBtn.disabled = false;
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
    let safe = typeof h === 'function' ? h(text) : text;
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
            const currentSession = typeof state !== 'undefined' ? state.activeChatSessionKey : null;
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

    const rawSessionId = (typeof state !== 'undefined' && state.activeChatSessionKey) ? state.activeChatSessionKey.replace(/^api:/, '') : 'dashboard_default';

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
            const md = typeof marked !== 'undefined' ? marked.parse(data.response || '(空)') : data.response;
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
        const d = typeof jsonParseSafe === 'function' ? jsonParseSafe(e.data) : JSON.parse(e.data);
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
                const resText = d.response || d.progress.result;
                bubbleHtml += typeof marked !== 'undefined' ? marked.parse(resText) : resText;
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
        const indicator = document.getElementById('chat-typing-indicator');
        if (indicator) {
            finishAgentResponse('连接中断，请重试', true);
        }
    };
}
