// ============================================================
//  Module: todos (HTTP API) — Full CRUD with Time, Period & Reversible Status
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
        const r = await fetch('/agent/api/v1/todos?status=pending,active,done');
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
                (t.description || '').toLowerCase().includes(q) ||
                (t.project || '').toLowerCase().includes(q)
            );
        }
        // Place completed (status === 'done') tasks at the end
        filtered.sort((a, b) => {
            const aDone = (a.status === 'done') ? 1 : 0;
            const bDone = (b.status === 'done') ? 1 : 0;
            if (aDone !== bDone) return aDone - bDone;
            return (b.updated_at || '').localeCompare(a.updated_at || '');
        });
        return { hits: filtered.slice(offset, offset + limit), total: filtered.length };
    },

    renderCard(doc) {
        const kind = doc.kind || 'todo';
        const kindIcons = { code: '💻', note: '📝', todo: '📋', review: '👀', misc: '📌', recurring: '🔄' };
        const icon = kindIcons[kind] || '📌';
        const project = doc.project || '';
        const title = doc.title || '(无标题)';
        const updated = (doc.updated_at || '').slice(0, 16);
        const status = doc.status || 'pending';
        const isDone = status === 'done';
        const statusText = { pending: '待处理', active: '进行中', done: '已完成' }[status] || status;
        const due = doc.due_at ? doc.due_at.slice(0, 16).replace('T', ' ') : '';
        const isOverdue = doc.due_at && new Date(doc.due_at) < new Date() && status !== 'done';
        const cron = doc.recur_cron || '';

        // Formats due_at for datetime-local input
        let dueForInput = '';
        if (doc.due_at) {
            try {
                const dt = new Date(doc.due_at);
                const tzOffset = dt.getTimezoneOffset() * 60000;
                dueForInput = (new Date(dt.getTime() - tzOffset)).toISOString().slice(0, 16);
            } catch (e) {}
        }

        let html = `<div class="card todo-card ${isDone ? 'todo-card-done' : ''}" data-id="${h(doc.id)}">`;
        html += `<div class="card-meta">`;
        html += `<span class="tag">${icon} ${h(kind)}</span>`;
        if (project) html += `<span class="tag tag-project">📁 ${h(project)}</span>`;
        html += `<span class="date">${updated}</span>`;
        html += `<span class="tag status-tag status-${status}">${statusText}</span>`;
        if (due) html += `<span class="tag ${isOverdue ? 'due-overdue' : 'due-tag'}">⏰ ${h(due)}</span>`;
        if (cron) html += `<span class="tag cron-tag">🔄 周期: ${h(cron)}</span>`;
        html += `</div>`;

        html += `<h3 class="card-title">${h(title)}</h3>`;
        if (doc.description) html += `<div class="card-snippet">${h(doc.description)}</div>`;

        // ── Action Buttons with Reversible Status Transitions ──
        html += `<div class="todo-actions">`;
        if (status === 'pending') {
            html += `<button class="todo-btn todo-btn-status" data-id="${h(doc.id)}" data-status="active" title="开始处理">▶ 开始</button>`;
            html += `<button class="todo-btn todo-btn-status todo-btn-done" data-id="${h(doc.id)}" data-status="done" title="标记完成">✔ 完成</button>`;
        } else if (status === 'active') {
            html += `<button class="todo-btn todo-btn-status todo-btn-reset" data-id="${h(doc.id)}" data-status="pending" title="恢复待办状态">↩ 恢复待办</button>`;
            html += `<button class="todo-btn todo-btn-status todo-btn-done" data-id="${h(doc.id)}" data-status="done" title="标记完成">✔ 完成</button>`;
        } else if (status === 'done') {
            html += `<button class="todo-btn todo-btn-status todo-btn-reset" data-id="${h(doc.id)}" data-status="pending" title="重新打开任务">↩ 重新打开</button>`;
            html += `<button class="todo-btn todo-btn-status todo-btn-start" data-id="${h(doc.id)}" data-status="active" title="重新开始">▶ 重新开始</button>`;
        }
        html += `<button class="todo-btn todo-btn-toggle-edit" data-id="${h(doc.id)}" title="编辑时间和周期">✏ 编辑</button>`;
        html += `<button class="todo-btn todo-btn-del" data-id="${h(doc.id)}" title="永久删除">🗑 删除</button>`;
        html += `</div>`;

        // ── Inline Edit Panel ──
        html += `<div class="todo-edit-panel" id="todo-edit-${h(doc.id)}" style="display:none;">`;
        html += `<div class="edit-row">`;
        html += `<input type="text" class="edit-title" value="${h(title)}" placeholder="任务标题" />`;
        html += `<select class="edit-kind">`;
        html += `<option value="misc" ${kind === 'misc' ? 'selected' : ''}>📌 普通 (misc)</option>`;
        html += `<option value="code" ${kind === 'code' ? 'selected' : ''}>💻 代码 (code)</option>`;
        html += `<option value="recurring" ${kind === 'recurring' ? 'selected' : ''}>🔄 周期 (recurring)</option>`;
        html += `</select>`;
        html += `</div>`;
        html += `<div class="edit-row">`;
        html += `<input type="text" class="edit-project" value="${h(project)}" placeholder="关联项目 (可选)" />`;
        html += `<input type="datetime-local" class="edit-due" value="${dueForInput}" title="到期时间" />`;
        html += `<input type="text" class="edit-cron" value="${h(cron)}" placeholder="周期配置 (如 09:00 / daily / weekly)" />`;
        html += `</div>`;
        html += `<div class="edit-row">`;
        html += `<input type="text" class="edit-desc" value="${h(doc.description || '')}" placeholder="任务详细描述 (可选)" />`;
        html += `<button class="todo-btn todo-btn-save-edit" data-id="${h(doc.id)}">💾 保存修改</button>`;
        html += `<button class="todo-btn todo-btn-cancel-edit" data-id="${h(doc.id)}">取消</button>`;
        html += `</div>`;
        html += `</div>`;

        html += `</div>`;
        return html;
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },

    // ── Lifecycle: injected when Todos tab is activated ──
    onMount(container) {
        const header = container.querySelector('.results-header');
        if (header && !header.querySelector('.todo-create-bar')) {
            const form = document.createElement('div');
            form.className = 'todo-create-bar';
            form.innerHTML = `
                <div class="create-primary-row">
                    <input type="text" id="todo-new-title" placeholder="+ 新建待办事项..." autocomplete="off" />
                    <select id="todo-new-kind">
                        <option value="misc">📌 普通</option>
                        <option value="code">💻 代码</option>
                        <option value="recurring">🔄 周期任务</option>
                    </select>
                    <button type="button" id="todo-toggle-more" title="展开更多设置 (时间/周期/项目)">⚙ 更多参数</button>
                    <button type="button" id="todo-new-submit">添加待办</button>
                </div>
                <div class="create-more-row" id="todo-more-fields" style="display:none;">
                    <input type="datetime-local" id="todo-new-due" title="到期时间 (可选)" />
                    <input type="text" id="todo-new-cron" placeholder="周期配置 (如 09:00, daily, weekly, 0 9 * * 1)" />
                    <input type="text" id="todo-new-project" placeholder="关联项目 (可选)" />
                </div>
            `;
            header.appendChild(form);

            const titleInput = form.querySelector('#todo-new-title');
            const kindSelect = form.querySelector('#todo-new-kind');
            const dueInput   = form.querySelector('#todo-new-due');
            const cronInput  = form.querySelector('#todo-new-cron');
            const projInput  = form.querySelector('#todo-new-project');
            const moreBtn    = form.querySelector('#todo-toggle-more');
            const moreFields = form.querySelector('#todo-more-fields');
            const submitBtn  = form.querySelector('#todo-new-submit');

            moreBtn.addEventListener('click', () => {
                const isHidden = moreFields.style.display === 'none';
                moreFields.style.display = isHidden ? 'flex' : 'none';
                moreBtn.classList.toggle('active', isHidden);
            });

            kindSelect.addEventListener('change', () => {
                if (kindSelect.value === 'recurring' && moreFields.style.display === 'none') {
                    moreFields.style.display = 'flex';
                    moreBtn.classList.add('active');
                }
            });

            const doCreate = async () => {
                const title = titleInput.value.trim();
                if (!title) return;
                submitBtn.disabled = true;
                try {
                    const payload = {
                        title,
                        kind: kindSelect.value,
                        project: projInput.value.trim() || undefined,
                        recur_cron: cronInput.value.trim() || undefined,
                    };
                    if (dueInput.value) {
                        payload.due_at = new Date(dueInput.value).toISOString();
                    }

                    await fetch('/agent/api/v1/todos', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload),
                    });
                    titleInput.value = '';
                    dueInput.value = '';
                    cronInput.value = '';
                    projInput.value = '';
                    performSearch(false);
                } finally {
                    submitBtn.disabled = false;
                }
            };

            submitBtn.addEventListener('click', doCreate);
            titleInput.addEventListener('keydown', e => { if (e.key === 'Enter') doCreate(); });
        }

        // Event delegation for card actions & inline edit
        const grid = container.querySelector('#results-grid');
        if (grid && !this._clickHandler) {
            this._clickHandler = async (e) => {
                const statusBtn = e.target.closest('.todo-btn-status');
                const editBtn   = e.target.closest('.todo-btn-toggle-edit');
                const saveEdit  = e.target.closest('.todo-btn-save-edit');
                const cancelEdit = e.target.closest('.todo-btn-cancel-edit');
                const delBtn    = e.target.closest('.todo-btn-del');

                if (statusBtn) {
                    const id = statusBtn.getAttribute('data-id');
                    const targetStatus = statusBtn.getAttribute('data-status');
                    statusBtn.disabled = true;
                    await fetch(`/agent/api/v1/todos/${id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ status: targetStatus }),
                    });
                    performSearch(false);
                } else if (editBtn) {
                    const id = editBtn.getAttribute('data-id');
                    const panel = document.getElementById(`todo-edit-${id}`);
                    if (panel) {
                        panel.style.display = panel.style.display === 'none' ? 'flex' : 'none';
                    }
                } else if (cancelEdit) {
                    const id = cancelEdit.getAttribute('data-id');
                    const panel = document.getElementById(`todo-edit-${id}`);
                    if (panel) panel.style.display = 'none';
                } else if (saveEdit) {
                    const id = saveEdit.getAttribute('data-id');
                    const panel = document.getElementById(`todo-edit-${id}`);
                    if (!panel) return;

                    const newTitle = panel.querySelector('.edit-title').value.trim();
                    const newKind  = panel.querySelector('.edit-kind').value;
                    const newProj  = panel.querySelector('.edit-project').value.trim();
                    const newDue   = panel.querySelector('.edit-due').value;
                    const newCron  = panel.querySelector('.edit-cron').value.trim();
                    const newDesc  = panel.querySelector('.edit-desc').value.trim();

                    saveEdit.disabled = true;
                    try {
                        const payload = {
                            title: newTitle,
                            kind: newKind,
                            project: newProj,
                            recur_cron: newCron,
                            description: newDesc,
                        };
                        payload.due_at = newDue ? new Date(newDue).toISOString() : '';

                        await fetch(`/agent/api/v1/todos/${id}`, {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(payload),
                        });
                        performSearch(false);
                    } finally {
                        saveEdit.disabled = false;
                    }
                } else if (delBtn) {
                    const id = delBtn.getAttribute('data-id');
                    const card = delBtn.closest('.todo-card');
                    const title = card ? card.querySelector('.card-title')?.textContent : id;
                    if (!confirm(`永久删除待办「${title}」？`)) return;
                    delBtn.disabled = true;
                    await fetch(`/agent/api/v1/todos/${id}`, { method: 'DELETE' });
                    performSearch(false);
                }
            };
            grid.addEventListener('click', this._clickHandler);
        }
    },

    onUnmount(container) {
        const header = container.querySelector('.results-header');
        const form = header?.querySelector('.todo-create-bar');
        if (form) form.remove();

        const grid = container.querySelector('#results-grid');
        if (grid && this._clickHandler) {
            grid.removeEventListener('click', this._clickHandler);
            this._clickHandler = null;
        }
    },

    _isDoneExpanded: false,

    onPostRender(grid) {
        if (!grid) return;
        // Remove existing toggle bars to avoid duplicates on infinite scroll (append)
        grid.querySelectorAll('.todo-completed-toggle-bar').forEach(el => el.remove());

        const doneCards = grid.querySelectorAll('.todo-card-done');
        if (!doneCards || doneCards.length === 0) return;

        // Create or refresh toggle bar
        const toggleBar = document.createElement('div');
        toggleBar.className = 'todo-completed-toggle-bar' + (this._isDoneExpanded ? ' expanded' : '');
        toggleBar.innerHTML = `
            <span>✅ 已完成事项 (${doneCards.length})</span>
            <span class="arrow">${this._isDoneExpanded ? '▼ 点击收起' : '▶ 点击展开'}</span>
        `;

        // Apply initial collapsed/expanded state
        doneCards.forEach(card => {
            if (this._isDoneExpanded) {
                card.classList.remove('is-collapsed');
            } else {
                card.classList.add('is-collapsed');
            }
        });

        // Insert toggleBar right before the first doneCard
        const firstDone = doneCards[0];
        if (firstDone && firstDone.parentNode) {
            firstDone.parentNode.insertBefore(toggleBar, firstDone);
        }

        // Bind toggle click event
        toggleBar.addEventListener('click', () => {
            this._isDoneExpanded = !this._isDoneExpanded;
            const arrow = toggleBar.querySelector('.arrow');
            if (this._isDoneExpanded) {
                toggleBar.classList.add('expanded');
                if (arrow) arrow.textContent = '▼ 点击收起';
                doneCards.forEach(card => card.classList.remove('is-collapsed'));
            } else {
                toggleBar.classList.remove('expanded');
                if (arrow) arrow.textContent = '▶ 点击展开';
                doneCards.forEach(card => card.classList.add('is-collapsed'));
            }
        });
    },
});
