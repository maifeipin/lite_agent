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

    async search(query, offset, limit, filter) {
        const payload = {
            q: query || '',
            sort: ['date:desc'],
            filter,
            offset, limit,
            facets: this.id === 'rss' ? ['category', 'topics', 'source'] : (FACET_SOURCES.has(this.id) ? ['source', 'type'] : undefined),
            attributesToHighlight: ['title', 'content'],
            highlightPreTag: '<mark>', highlightPostTag: '</mark>',
        };
        if (!payload.filter) delete payload.filter;
        if (!payload.facets) delete payload.facets;
        const r = await fetch('/agent/meili/indexes/rss/search', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!r.ok) return { hits: [], total: 0, facets: {} };
        const d = await r.json();
        return {
            hits: (d.hits || []).map(h => { h._module = 'rss'; return h; }),
            total: d.estimatedTotalHits || d.totalHits || 0,
            facets: d.facetDistribution || {},
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
