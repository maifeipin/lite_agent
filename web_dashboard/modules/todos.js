// ============================================================
//  Module: todos (HTTP API) — with full CRUD management
// ============================================================
registerTabModule({
    id: 'todos',
    label: '待办',
    icon: '✅',
    badgeId: 'badge-todos',

    _clickHandler: null,

    async fetchCount() {
        try {
            const r = await fetch('/agent/api/v1/todos?status=pending,active');
            const d = await r.json();
            return (d.data || []).length;
        } catch { return 0; }
    },

    async _fetchAll() {
        const r = await fetch('/agent/api/v1/todos?status=pending,active');
        const d = await r.json();
        return (d.data || []).map(t => { t._module = 'todos'; return t; });
    },

    async search(query, offset, limit) {
        const all = await this._fetchAll();
        let filtered = all;
        if (query) {
            const q = query.toLowerCase();
            filtered = all.filter(t =>
                (t.title || '').toLowerCase().includes(q) ||
                (t.description || '').toLowerCase().includes(q)
            );
        }
        return { hits: filtered.slice(offset, offset + limit), total: filtered.length };
    },

    renderCard(doc) {
        const kind = doc.kind || 'todo';
        const kindIcons = { code: '💻', note: '📝', todo: '📋', review: '👀', misc: '📌' };
        const icon = kindIcons[kind] || '📌';
        const project = doc.project || '';
        const title = doc.title || '(无标题)';
        const updated = (doc.updated_at || '').slice(0, 16);
        const status = doc.status || 'pending';
        const statusText = { pending: '待处理', active: '进行中', done: '已完成' }[status] || status;
        const due = doc.due_at ? doc.due_at.slice(0, 10) : '';
        const isOverdue = due && new Date(doc.due_at) < new Date();

        let html = `<div class="card todo-card" data-id="${h(doc.id)}">`;
        html += `<div class="card-meta"><span class="tag">${icon} ${h(kind)}</span>`;
        if (project) html += `<span class="tag">${h(project)}</span>`;
        html += `<span class="date">${updated}</span>`;
        html += `<span class="tag status-tag">${statusText}</span>`;
        if (due) html += `<span class="tag ${isOverdue ? 'due-overdue' : 'due-tag'}">⏰ ${due}</span>`;
        html += `</div>`;
        html += `<h3 class="card-title">${h(title)}</h3>`;
        if (doc.description) html += `<div class="card-snippet">${h(doc.description.slice(0, 200))}</div>`;
        html += `<div class="todo-actions">`;
        if (status === 'pending') {
            html += `<button class="todo-btn todo-btn-start" data-id="${h(doc.id)}" title="开始处理">▶ 开始</button>`;
        } else if (status === 'active') {
            html += `<button class="todo-btn todo-btn-done" data-id="${h(doc.id)}" title="标记完成">✔ 完成</button>`;
        }
        html += `<button class="todo-btn todo-btn-del" data-id="${h(doc.id)}" title="永久删除">🗑</button>`;
        html += `</div></div>`;
        return html;
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },

    // ── Lifecycle: injected when Todos tab is activated ──
    onMount(container) {
        // Inject quick-add form into the results-header if not already present
        const header = container.querySelector('.results-header');
        if (header && !header.querySelector('.todo-create-form')) {
            const form = document.createElement('div');
            form.className = 'todo-create-form';
            form.innerHTML = `
                <input type="text" id="todo-new-title" placeholder="+ 快速新建待办..." autocomplete="off" />
                <button id="todo-new-submit" title="添加待办">添加</button>
            `;
            header.appendChild(form);

            const input = form.querySelector('#todo-new-title');
            const btn = form.querySelector('#todo-new-submit');

            const doCreate = async () => {
                const title = input.value.trim();
                if (!title) return;
                btn.disabled = true;
                try {
                    await fetch('/agent/api/v1/todos', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title, kind: 'misc' }),
                    });
                    input.value = '';
                    performSearch(false);
                } finally {
                    btn.disabled = false;
                }
            };

            btn.addEventListener('click', doCreate);
            input.addEventListener('keydown', e => { if (e.key === 'Enter') doCreate(); });
        }

        // Event delegation: handle action buttons inside the results grid
        const grid = container.querySelector('#results-grid');
        if (grid && !this._clickHandler) {
            this._clickHandler = async (e) => {
                const startBtn = e.target.closest('.todo-btn-start');
                const doneBtn  = e.target.closest('.todo-btn-done');
                const delBtn   = e.target.closest('.todo-btn-del');

                if (startBtn) {
                    const id = startBtn.getAttribute('data-id');
                    startBtn.disabled = true;
                    await fetch(`/agent/api/v1/todos/${id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ status: 'active' }),
                    });
                    performSearch(false);
                } else if (doneBtn) {
                    const id = doneBtn.getAttribute('data-id');
                    doneBtn.disabled = true;
                    await fetch(`/agent/api/v1/todos/${id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ status: 'done' }),
                    });
                    performSearch(false);
                } else if (delBtn) {
                    const id = delBtn.getAttribute('data-id');
                    const card = delBtn.closest('.todo-card');
                    const title = card ? card.querySelector('.card-title')?.textContent : id;
                    if (!confirm(`永久删除「${title}」？`)) return;
                    delBtn.disabled = true;
                    await fetch(`/agent/api/v1/todos/${id}`, { method: 'DELETE' });
                    performSearch(false);
                }
            };
            grid.addEventListener('click', this._clickHandler);
        }
    },

    // ── Lifecycle: clean up when leaving Todos tab ──
    onUnmount(container) {
        const header = container.querySelector('.results-header');
        const form = header?.querySelector('.todo-create-form');
        if (form) form.remove();

        const grid = container.querySelector('#results-grid');
        if (grid && this._clickHandler) {
            grid.removeEventListener('click', this._clickHandler);
            this._clickHandler = null;
        }
    },
});
