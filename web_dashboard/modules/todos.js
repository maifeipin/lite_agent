// ============================================================
//  Module: todos (HTTP API) — Full CRUD with Time, Period & Reversible Status
// ============================================================
registerTabModule({
    id: 'todos',
    label: '待办',
    icon: '✅',
    badgeId: 'badge-todos',

    _clickHandler: null,
    _sortMode: 'smart_recent', // smart_recent | due_proximity | priority

    _parseEpochMillis(raw) {
        if (!raw || typeof raw !== 'string') return null;
        const s = raw.trim();
        if (!s) return null;
        const normalized = s.includes('T') ? s : s.replace(' ', 'T');
        const ms = Date.parse(normalized);
        return isNaN(ms) ? null : ms;
    },

    _toLocalIsoWithTz(localDatetimeValue) {
        if (!localDatetimeValue) return '';
        const dt = new Date(localDatetimeValue);
        if (isNaN(dt.getTime())) return '';
        const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, '0');
        const tzOffsetMins = -dt.getTimezoneOffset();
        const sign = tzOffsetMins >= 0 ? '+' : '-';
        const tzHours = pad(tzOffsetMins / 60);
        const tzMins = pad(tzOffsetMins % 60);
        return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}` +
            `T${pad(dt.getHours())}:${pad(dt.getMinutes())}:00${sign}${tzHours}:${tzMins}`;
    },

    _formatLocalDueDisplay(rawDue) {
        const ms = this._parseEpochMillis(rawDue);
        if (ms === null) return rawDue ? String(rawDue).slice(0, 16).replace('T', ' ') : '';
        const dt = new Date(ms);
        const pad = (n) => String(n).padStart(2, '0');
        return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
    },

    _sortTodos(items) {
        const nowMs = Date.now();
        const mode = this._sortMode || 'smart_recent';
        return items.sort((a, b) => {
            // 1. 已完成事项自动沉底 (GTD 标准)
            const aDone = (a.status === 'done') ? 1 : 0;
            const bDone = (b.status === 'done') ? 1 : 0;
            if (aDone !== bDone) return aDone - bDone;

            const aDue = this._parseEpochMillis(a.due_at);
            const bDue = this._parseEpochMillis(b.due_at);
            const aUpd = this._parseEpochMillis(a.updated_at) || this._parseEpochMillis(a.created_at) || 0;
            const bUpd = this._parseEpochMillis(b.updated_at) || this._parseEpochMillis(b.created_at) || 0;

            if (mode === 'due_proximity') {
                if ((aDue !== null) !== (bDue !== null)) return aDue !== null ? -1 : 1;
                if (aDue !== null && bDue !== null && aDue !== bDue) {
                    return Math.abs(aDue - nowMs) - Math.abs(bDue - nowMs);
                }
                return bUpd - aUpd;
            }
            if (mode === 'priority') {
                const aPrio = a.priority !== undefined && a.priority !== null ? Number(a.priority) : 2;
                const bPrio = b.priority !== undefined && b.priority !== null ? Number(b.priority) : 2;
                if (aPrio !== bPrio) return aPrio - bPrio;
            }
            // Default: smart_recent (综合最新更新/创建与近期截止时间降序)
            const aEff = Math.max(aUpd, aDue || 0);
            const bEff = Math.max(bUpd, bDue || 0);
            if (aEff !== bEff) return bEff - aEff;
            if ((aDue !== null) !== (bDue !== null)) return aDue !== null ? -1 : 1;
            return ((a.priority ?? 2) - (b.priority ?? 2));
        });
    },

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
        this._sortTodos(filtered);
        return { hits: filtered.slice(offset, offset + limit), total: filtered.length };
    },

    renderCard(doc) {
        const kind = doc.kind || 'todo';
        const kindIcons = { code: '💻', note: '📝', todo: '📋', review: '👀', misc: '📌', recurring: '🔄' };
        const icon = kindIcons[kind] || '📌';
        const project = doc.project || '';
        const title = doc.title || '(无标题)';
        const updated = this._formatLocalDueDisplay(doc.updated_at) || (doc.updated_at || '').slice(0, 16);
        const status = doc.status || 'pending';
        const isDone = status === 'done';
        const statusText = { pending: '待处理', active: '进行中', done: '已完成' }[status] || status;
        const due = doc.due_at ? this._formatLocalDueDisplay(doc.due_at) : '';
        const dueMs = this._parseEpochMillis(doc.due_at);
        const isOverdue = dueMs !== null && dueMs < Date.now() && status !== 'done';
        const cron = doc.recur_cron || '';
        const priority = doc.priority !== undefined && doc.priority !== null ? Number(doc.priority) : 2;
        const prioMeta = {
            0: { text: '🚨 P0 紧急', style: 'background:rgba(239,68,68,0.2);color:#f87171;' },
            1: { text: '⚡ P1 高', style: 'background:rgba(245,158,11,0.2);color:#fbbf24;' },
            2: { text: '📌 P2 中', style: 'background:rgba(99,102,241,0.18);color:#818cf8;' },
            3: { text: '☕ P3 低', style: 'background:rgba(148,163,184,0.15);color:#94a3b8;' },
        }[priority] || { text: '📌 P2 中', style: '' };
        const dispatchTarget = doc.dispatch_target || '内置通道推送';

        // Formats due_at for datetime-local input in local timezone
        let dueForInput = '';
        if (dueMs !== null) {
            try {
                const dt = new Date(dueMs);
                const tzOffset = dt.getTimezoneOffset() * 60000;
                dueForInput = (new Date(dt.getTime() - tzOffset)).toISOString().slice(0, 16);
            } catch (e) {}
        }

        let html = `<div class="card todo-card ${isDone ? 'todo-card-done' : ''}" data-id="${h(doc.id)}">`;
        html += `<div class="card-meta">`;
        html += `<span class="tag" style="${prioMeta.style}">${prioMeta.text}</span>`;
        html += `<span class="tag">${icon} ${h(kind)}</span>`;
        if (project) html += `<span class="tag tag-project">📁 ${h(project)}</span>`;
        html += `<span class="date">${h(updated)}</span>`;
        html += `<span class="tag status-tag status-${status}">${statusText}</span>`;
        if (due) html += `<span class="tag ${isOverdue ? 'due-overdue' : 'due-tag'}">⏰ ${h(due)}</span>`;
        if (due) {
            const beforeMins = doc.remind_before_mins !== undefined ? doc.remind_before_mins : 30;
            const intervalMins = doc.remind_interval_mins || 0;
            let remindStr = `提前: ${beforeMins}分`;
            if (intervalMins > 0) remindStr += ` / 间隔: ${intervalMins}分`;
            html += `<span class="tag remind-tag" style="background: hsl(200, 60%, 25%); color: #8df;">🔔 ${remindStr} (${h(dispatchTarget)})</span>`;
        }
        if (cron) html += `<span class="tag cron-tag">🔄 周期: ${h(cron)}</span>`;
        html += `</div>`;

        html += `<h3 class="card-title">${h(title)}</h3>`;
        if (doc.description) html += `<div class="card-snippet">${h(doc.description)}</div>`;

        const notifyEnabled = doc.notify_enabled !== false && doc.notify_enabled !== 0;
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
        if (!isDone) {
            html += `<button class="todo-btn todo-btn-meeting" data-id="${h(doc.id)}" title="就此待办发起会审会议，生成邀请链接">📣 会议</button>`;
        }
        html += `<button class="todo-btn todo-btn-notify" data-id="${h(doc.id)}" data-enabled="${!notifyEnabled}">${notifyEnabled ? "🔔 关闭提醒" : "🔕 恢复提醒"}</button>`;
        html += `<button class="todo-btn todo-btn-toggle-edit" data-id="${h(doc.id)}" title="编辑时间和周期">✏ 编辑</button>`;
        html += `<button class="todo-btn todo-btn-del" data-id="${h(doc.id)}" title="永久删除">🗑 删除</button>`;
        html += `</div>`;

        // ── Inline Edit Panel ──
        html += `<div class="todo-edit-panel" id="todo-edit-${h(doc.id)}" style="display:none;">`;
        html += `<div class="edit-row">`;
        html += `<input type="text" class="edit-title" value="${h(title)}" placeholder="任务标题" />`;
        html += `<select class="edit-priority" title="优先级">`;
        html += `<option value="0" ${priority === 0 ? 'selected' : ''}>🚨 P0 紧急</option>`;
        html += `<option value="1" ${priority === 1 ? 'selected' : ''}>⚡ P1 高</option>`;
        html += `<option value="2" ${priority === 2 ? 'selected' : ''}>📌 P2 中</option>`;
        html += `<option value="3" ${priority === 3 ? 'selected' : ''}>☕ P3 低</option>`;
        html += `</select>`;
        html += `<select class="edit-kind">`;
        if (!["misc", "code", "recurring"].includes(kind)) html += `<option value="${h(kind)}" selected>${h(kind)}</option>`;
        html += `<option value="misc" ${kind === 'misc' ? 'selected' : ''}>📌 普通 (misc)</option>`;
        html += `<option value="code" ${kind === 'code' ? 'selected' : ''}>💻 代码 (code)</option>`;
        html += `<option value="recurring" ${kind === 'recurring' ? 'selected' : ''}>🔄 周期 (recurring)</option>`;
        html += `</select>`;
        html += `<select class="edit-dispatch" title="推送渠道">`;
        html += `<option value="内置通道推送" ${dispatchTarget === '内置通道推送' ? 'selected' : ''}>🔔 内置系统推送</option>`;
        html += `<option value="Telegram" ${dispatchTarget === 'Telegram' ? 'selected' : ''}>✈️ Telegram</option>`;
        html += `<option value="邮件" ${dispatchTarget === '邮件' ? 'selected' : ''}>✉️ 邮件提醒</option>`;
        html += `<option value="全通道" ${dispatchTarget === '全通道' ? 'selected' : ''}>🌐 全通道</option>`;
        html += `</select>`;
        html += `</div>`;
        html += `<div class="edit-row">`;
        html += `<input type="text" class="edit-project" value="${h(project)}" placeholder="关联项目 (可选)" />`;
        html += `<input type="datetime-local" class="edit-due" value="${dueForInput}" title="到期时间" />`;
        html += `<input type="number" class="edit-before-mins" min="0" value="${doc.remind_before_mins !== undefined ? doc.remind_before_mins : 30}" placeholder="提前(分)" title="到期前多少分钟开始提醒" style="width: 90px;" />`;
        html += `<input type="number" class="edit-interval-mins" min="0" value="${doc.remind_interval_mins || 0}" placeholder="间隔(分)" title="提醒重复间隔(分钟)" style="width: 90px;" />`;
        html += `<input type="text" class="edit-cron" value="${h(cron)}" placeholder="周期配置 (如 09:00 / daily)" />`;
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
                    <select id="todo-new-priority" title="优先级">
                        <option value="0">🚨 P0 紧急</option>
                        <option value="1">⚡ P1 高</option>
                        <option value="2" selected>📌 P2 中</option>
                        <option value="3">☕ P3 低</option>
                    </select>
                    <select id="todo-new-kind">
                        <option value="misc">📌 普通</option>
                        <option value="code">💻 代码</option>
                        <option value="recurring">🔄 周期任务</option>
                    </select>
                    <select id="todo-sort-select" title="GTD 列表排序策略">
                        <option value="smart_recent" ${this._sortMode === 'smart_recent' ? 'selected' : ''}>🕐 排序: 最近待办优先</option>
                        <option value="due_proximity" ${this._sortMode === 'due_proximity' ? 'selected' : ''}>📅 排序: 临近截止优先</option>
                        <option value="priority" ${this._sortMode === 'priority' ? 'selected' : ''}>🔥 排序: 优先级优先</option>
                    </select>
                    <button type="button" id="todo-toggle-more" title="展开更多设置 (时间/周期/项目/推送)">⚙ 时间与提醒</button>
                    <button type="button" id="todo-new-submit">添加待办</button>
                </div>
                <div class="create-more-row" id="todo-more-fields" style="display:none; flex-wrap:wrap; gap:8px; align-items:center;">
                    <input type="datetime-local" id="todo-new-due" title="到期日期与时间 (原生图形选择器)" />
                    <button type="button" class="todo-btn todo-preset-due" data-preset="today18" title="设为今天 18:00">今天18:00</button>
                    <button type="button" class="todo-btn todo-preset-due" data-preset="tomorrow10" title="设为明天 10:00">明天10:00</button>
                    <button type="button" class="todo-btn todo-preset-due" data-preset="weekend10" title="设为本周末 10:00">周末10:00</button>
                    <input type="number" id="todo-new-before-mins" min="0" placeholder="提前(分)" value="40" title="到期前多少分钟开始提醒 (默认 40)" style="width: 90px;" />
                    <input type="number" id="todo-new-interval-mins" min="0" placeholder="间隔(分)" value="5" title="提醒重复频次间隔(分钟)，如 5 分钟" style="width: 90px;" />
                    <select id="todo-new-dispatch" title="推送通知渠道">
                        <option value="内置通道推送" selected>🔔 内置系统推送</option>
                        <option value="Telegram">✈️ Telegram Bot</option>
                        <option value="邮件">✉️ 邮件提醒</option>
                        <option value="全通道">🌐 全通道推送</option>
                    </select>
                    <input type="text" id="todo-new-cron" placeholder="周期配置 (如 09:00, daily)" />
                    <label><input type="checkbox" id="todo-new-notify" checked /> 开启提醒</label>
                    <input type="text" id="todo-new-project" placeholder="关联项目 (如 会议/工作)" />
                </div>
            `;
            header.appendChild(form);

            const titleInput    = form.querySelector('#todo-new-title');
            const prioSelect    = form.querySelector('#todo-new-priority');
            const kindSelect    = form.querySelector('#todo-new-kind');
            const sortSelect    = form.querySelector('#todo-sort-select');
            const dueInput      = form.querySelector('#todo-new-due');
            const beforeInput   = form.querySelector('#todo-new-before-mins');
            const intervalInput = form.querySelector('#todo-new-interval-mins');
            const dispatchSelect = form.querySelector('#todo-new-dispatch');
            const cronInput     = form.querySelector('#todo-new-cron');
            const projInput     = form.querySelector('#todo-new-project');
            const notifyInput   = form.querySelector('#todo-new-notify');
            const moreBtn       = form.querySelector('#todo-toggle-more');
            const moreFields    = form.querySelector('#todo-more-fields');
            const submitBtn     = form.querySelector('#todo-new-submit');

            if (sortSelect) {
                sortSelect.addEventListener('change', () => {
                    this._sortMode = sortSelect.value;
                    performSearch(false);
                });
            }

            form.querySelectorAll('.todo-preset-due').forEach(btn => {
                btn.addEventListener('click', () => {
                    const preset = btn.getAttribute('data-preset');
                    const d = new Date();
                    if (preset === 'today18') {
                        d.setHours(18, 0, 0, 0);
                    } else if (preset === 'tomorrow10') {
                        d.setDate(d.getDate() + 1);
                        d.setHours(10, 0, 0, 0);
                    } else if (preset === 'weekend10') {
                        while (d.getDay() !== 6) d.setDate(d.getDate() + 1);
                        d.setHours(10, 0, 0, 0);
                    }
                    const tzOffset = d.getTimezoneOffset() * 60000;
                    dueInput.value = (new Date(d.getTime() - tzOffset)).toISOString().slice(0, 16);
                });
            });

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
                if (dueInput.value && isNaN(new Date(dueInput.value).getTime())) {
                    alert('请输入或选择有效的截止日期与时间');
                    return;
                }
                submitBtn.disabled = true;
                try {
                    const payload = {
                        title,
                        notify_enabled: notifyInput.checked,
                        kind: kindSelect.value,
                        priority: parseInt(prioSelect.value, 10) || 2,
                        dispatch_target: dispatchSelect.value || '内置通道推送',
                        project: projInput.value.trim() || undefined,
                        recur_cron: cronInput.value.trim() || undefined,
                    };
                    if (dueInput.value) {
                        payload.due_at = this._toLocalIsoWithTz(dueInput.value);
                        payload.remind_before_mins = beforeInput.value === '' ? 40 : Math.max(0, parseInt(beforeInput.value, 10));
                        payload.remind_interval_mins = Math.max(0, parseInt(intervalInput.value, 10) || 0);
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
                const notifyBtn = e.target.closest('.todo-btn-notify');
                const saveEdit  = e.target.closest('.todo-btn-save-edit');
                const cancelEdit = e.target.closest('.todo-btn-cancel-edit');
                const delBtn    = e.target.closest('.todo-btn-del');
                const meetBtn   = e.target.closest('.todo-btn-meeting');

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
                } else if (notifyBtn) {
                    notifyBtn.disabled = true;
                    try {
                        await fetch(`/agent/api/v1/todos/${notifyBtn.getAttribute('data-id')}`, {
                            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({notify_enabled: notifyBtn.getAttribute('data-enabled') === 'true'}),
                        });
                        performSearch(false);
                    } finally { notifyBtn.disabled = false; }
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

                    const newTitle    = panel.querySelector('.edit-title').value.trim();
                    const newPrio     = panel.querySelector('.edit-priority')?.value;
                    const newKind     = panel.querySelector('.edit-kind').value;
                    const newDispatch = panel.querySelector('.edit-dispatch')?.value;
                    const newProj     = panel.querySelector('.edit-project').value.trim();
                    const newDue      = panel.querySelector('.edit-due').value;
                    const newBefore   = panel.querySelector('.edit-before-mins')?.value;
                    const newInterval = panel.querySelector('.edit-interval-mins')?.value;
                    const newCron     = panel.querySelector('.edit-cron').value.trim();
                    const newDesc     = panel.querySelector('.edit-desc').value.trim();

                    saveEdit.disabled = true;
                    try {
                        const payload = {
                            title: newTitle,
                            kind: newKind,
                            priority: newPrio !== undefined ? parseInt(newPrio, 10) : 2,
                            dispatch_target: newDispatch || '内置通道推送',
                            project: newProj,
                            recur_cron: newCron,
                            description: newDesc,
                            remind_before_mins: newBefore !== undefined && newBefore !== '' ? Math.max(0, parseInt(newBefore, 10)) : 30,
                            remind_interval_mins: newInterval !== undefined && newInterval !== '' ? Math.max(0, parseInt(newInterval, 10)) : 0,
                        };
                        payload.due_at = newDue ? this._toLocalIsoWithTz(newDue) : '';

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
                } else if (meetBtn) {
                    const id = meetBtn.getAttribute('data-id');
                    const doc = (typeof state !== 'undefined' ? state.results : [])
                        .find(d => String(d.id) === String(id)) || {};
                    const title = doc.title
                        || meetBtn.closest('.todo-card')?.querySelector('.card-title')?.textContent
                        || '待办会议';
                    const briefLines = [`待办事项：${title}`];
                    if (doc.project) briefLines.push(`项目：${doc.project}`);
                    if (doc.due_at) briefLines.push(`到期：${doc.due_at.slice(0, 16).replace('T', ' ')}`);
                    if (doc.description) briefLines.push(`描述：${doc.description}`);

                    meetBtn.disabled = true;
                    try {
                        const resp = await ReviewMeetingAPI.fetch('/agent/api/v1/review-meetings', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ title, brief: briefLines.join('\n') }),
                        });
                        const data = await resp.json();
                        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
                        const url = data.invite_url;
                        let copied = false;
                        try { await navigator.clipboard.writeText(url); copied = true; } catch {}
                        showModal({
                            title: '会议已创建',
                            icon: '📣',
                            content: `<p>议题：<b>${h(title)}</b></p>` +
                                `<p>${copied ? '✅ 邀请链接已复制到剪贴板，直接粘贴给参会方即可。'
                                             : '⚠️ 自动复制失败，请手动复制下方链接。'}</p>`,
                            input: { value: url },
                            buttons: [
                                { text: '再次复制', class: 'modal-btn-primary', onClick: async ({ close }) => {
                                    try { await navigator.clipboard.writeText(url); } catch {}
                                    close();
                                } },
                                { text: '关闭' },
                            ],
                        });
                    } catch (err) {
                        showModal({ title: '发起会议失败', icon: '⚠️', content: `<p>${h(err.message)}</p>` });
                    } finally {
                        meetBtn.disabled = false;
                    }
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
