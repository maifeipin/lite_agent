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

    async search(query, offset, limit, filter) {
        const payload = {
            q: query || '',
            sort: ['date:desc'],
            filter,
            offset, limit,
            facets: this.id === 'rss' ? ['category', 'topics', 'source'] : (FACET_SOURCES.has(this.id) ? ['source', 'type'] : undefined),
            attributesToHighlight: ['subject', 'sender', 'plain_text', 'summary'],
            highlightPreTag: '<mark>', highlightPostTag: '</mark>',
        };
        if (!payload.filter) delete payload.filter;
        if (!payload.facets) delete payload.facets;
        const r = await fetch('/agent/meili/indexes/emails/search', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!r.ok) return { hits: [], total: 0, facets: {} };
        const d = await r.json();
        return {
            hits: (d.hits || []).map(h => { h._module = 'emails'; return h; }),
            total: d.estimatedTotalHits || d.totalHits || 0,
            facets: d.facetDistribution || {},
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
