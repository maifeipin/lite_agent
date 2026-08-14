// ============================================================
//  Facet Panel Component (分面筛选与智能分类器)
// ============================================================

function updateFacetPanelVisibility() {
    const panel = document.getElementById('facet-panel');
    const toggleBtn = document.getElementById('global-filter-toggle');
    const isSupported = typeof FACET_SOURCES !== 'undefined' && typeof state !== 'undefined' && FACET_SOURCES.has(state.activeSource);
    
    if (toggleBtn) {
        toggleBtn.style.display = isSupported ? 'flex' : 'none';
        
        // Update active filter badge
        let activeCount = 0;
        if (typeof state !== 'undefined' && state.activeFilters) {
            for (const vals of Object.values(state.activeFilters)) {
                if (vals && vals.length > 0) activeCount += vals.length;
            }
        }
        const badge = document.getElementById('active-filter-count');
        if (badge) {
            badge.style.display = activeCount > 0 ? 'inline-block' : 'none';
            badge.textContent = activeCount;
        }
    }

    if (!panel) return;
    panel.style.display = (isSupported && typeof state !== 'undefined' && state.isFacetPanelVisible) ? 'flex' : 'none';
}

function initFacetPanelEvents() {
    // Global Toggle
    const toggleBtn = document.getElementById('global-filter-toggle');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            if (typeof state !== 'undefined') {
                state.isFacetPanelVisible = !state.isFacetPanelVisible;
            }
            updateFacetPanelVisibility();
        });
    }

    // Expand/Collapse Group
    const panel = document.getElementById('facet-panel');
    if (panel) {
        panel.addEventListener('click', (e) => {
            if (e.target.classList.contains('facet-expand-btn')) {
                const groupKey = e.target.getAttribute('data-group');
                if (typeof state !== 'undefined') {
                    if (state.expandedFacetGroups.has(groupKey)) {
                        state.expandedFacetGroups.delete(groupKey);
                    } else {
                        state.expandedFacetGroups.add(groupKey);
                    }
                    renderFacetPanel(state.lastFacets); // re-render panel
                }
            }
        });
    }
}

function renderFacetPanel(facetDist) {
    if (!facetDist || typeof FACET_SOURCES === 'undefined' || typeof state === 'undefined' || !FACET_SOURCES.has(state.activeSource)) return;
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
            html += `<span>${typeof h === 'function' ? h(item.val) : item.val}<b class="count">${item.count}</b></span>`;
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
            if (typeof performSearch === 'function') performSearch(false);
        });
    });
}
