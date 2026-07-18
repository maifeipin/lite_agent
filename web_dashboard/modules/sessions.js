// ============================================================
//  Module: sessions (SQLite via API)
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
