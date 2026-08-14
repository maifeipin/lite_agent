// ============================================================
//  Terminal & Command Palette Component (拟物化控制台与命令面板)
// ============================================================

function openCommandModal() {
    const modal = document.getElementById('command-modal');
    if (modal) {
        modal.style.display = 'flex';
        const input = document.getElementById('command-input');
        if (input) input.focus();
        if (typeof state !== 'undefined') state.selectedCommandIndex = 0;
        updateCommandSelection();
    }
}

function closeCommandModal() {
    const modal = document.getElementById('command-modal');
    if (modal) modal.style.display = 'none';
}

function updateCommandSelection() {
    const selectedIdx = typeof state !== 'undefined' ? state.selectedCommandIndex : 0;
    document.querySelectorAll('.command-item').forEach((el, i) => {
        el.classList.toggle('selected', i === selectedIdx);
    });
}

function getSelectedCommandText() {
    const items = document.querySelectorAll('.command-item');
    if (items.length === 0) return '';
    const selectedIdx = typeof state !== 'undefined' ? state.selectedCommandIndex : 0;
    const idx = Math.min(Math.max(0, selectedIdx), items.length - 1);
    return items[idx].getAttribute('data-cmd') || '';
}

function triggerCommand(cmdText) {
    if (!cmdText || !cmdText.trim()) return;
    // Open terminal if minimized
    const term = document.getElementById('terminal-window');
    if (term && term.classList.contains('minimized')) {
        term.classList.remove('minimized');
    }

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
            if (typeof state !== 'undefined') state.currentTaskId = data.task_id;
            appendTerminalLine(`任务已受理 (ID: ${data.task_id})，等待执行...`, 'system');
            subscribeTaskStream(data.task_id);
        }
    })
    .catch(e => {
        appendTerminalLine(`请求失败: ${e.message}`, 'error');
    });
}

function appendTerminalLine(text, type = 'system') {
    const body = document.getElementById('terminal-body');
    if (!body) return;
    const div = document.createElement('div');
    div.className = `terminal-line ${type}`;
    div.textContent = text;
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
}

function subscribeTaskStream(taskId) {
    if (typeof state !== 'undefined' && state.eventSource) {
        state.eventSource.close();
    }
    const es = new EventSource(`/agent/api/v1/task/stream?task_id=${taskId}&session_id=dashboard_admin`);
    if (typeof state !== 'undefined') state.eventSource = es;
    const seenProgress = new Set();
    let lastMsg = '';

    es.onmessage = (e) => {
        if (e.data === '[DONE]') {
            es.close();
            if (typeof state !== 'undefined') state.eventSource = null;
            return;
        }
        const d = typeof jsonParseSafe === 'function' ? jsonParseSafe(e.data) : JSON.parse(e.data);
        if (!d) return;
        if (d.status === 'done' || d.status === 'completed') {
            appendTerminalLine(d.response || d.message || '任务完成', 'system');
            es.close();
            if (typeof state !== 'undefined') state.eventSource = null;
            return;
        }
        if (d.status === 'failed' || d.status === 'error') {
            appendTerminalLine(d.message || d.error || '任务失败', 'error');
            es.close();
            if (typeof state !== 'undefined') state.eventSource = null;
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
        if (typeof state !== 'undefined') state.eventSource = null;
        appendTerminalLine('任务流连接中断', 'error');
    };
}

function initTerminal() {
    const header = document.getElementById('terminal-header');
    const closeBtn = document.getElementById('term-close');
    const toggleBtn = document.getElementById('term-toggle');
    const terminal = document.getElementById('terminal-window');

    if (header && terminal && toggleBtn) {
        header.addEventListener('click', () => {
            terminal.classList.toggle('minimized');
            toggleBtn.textContent = terminal.classList.contains('minimized') ? '展开' : '收起';
        });
    }
    if (closeBtn && terminal) {
        closeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            terminal.style.display = 'none';
        });
    }
    if (toggleBtn && terminal) {
        toggleBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            terminal.classList.toggle('minimized');
            toggleBtn.textContent = terminal.classList.contains('minimized') ? '展开' : '收起';
        });
    }
}
