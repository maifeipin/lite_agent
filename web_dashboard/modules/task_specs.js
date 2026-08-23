// Complex TaskSpec manager: author, edit, validate, run and schedule.
registerTabModule({
    id: 'task-specs',
    label: '复杂任务',
    icon: '🧩',
    badgeId: 'badge-task-specs',
    _clickHandler: null,
    _meta: null,

    async fetchCount() {
        try {
            const r = await fetch('/agent/api/v1/task-specs');
            return (await r.json()).total || 0;
        } catch { return 0; }
    },

    async search(query, offset, limit) {
        const r = await fetch('/agent/api/v1/task-specs');
        const data = await r.json();
        let rows = (data.data || []).map(x => { x._module = 'task-specs'; return x; });
        if (query) {
            const q = query.toLowerCase();
            rows = rows.filter(x =>
                (x.name || '').toLowerCase().includes(q) ||
                (x.spec?.task?.objective || '').toLowerCase().includes(q)
            );
        }
        return { hits: rows.slice(offset, offset + limit), total: rows.length };
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },

    renderCard(doc) {
        const spec = doc.spec || {};
        const task = spec.task || {};
        const exec = spec.execution || {};
        const model = exec.model_policy || {};
        const schedule = exec.schedule || {};
        const budget = exec.budget || {};
        const statusLabels = {
            draft: '草案', review_required: '待复核', blocked: '阻断',
            needs_ack: '待确认', approved: '已通过'
        };
        const scheduleText = schedule.mode === 'once' ? `一次 ${schedule.run_at || ''}` :
            schedule.mode === 'repeat' ? `重复 ${schedule.cron || ''}` : '手动';
        const last = doc.last_run_status ? `${doc.last_run_status} ${doc.last_run_at || ''}` : '尚未运行';
        const validation = spec.validation || {};
        const preflightFindings = validation.preflight?.findings || [];
        const reviewFindings = validation.findings || [];
        const findings = [...preflightFindings, ...reviewFindings];
        const findingHtml = findings.length ? `<details class="task-findings" open>
            <summary>校验说明 (${findings.length})</summary>
            <ul>${findings.map(f => `<li><strong>${h(f.code || f.severity || 'INFO')}</strong> ${h(f.message || '')}${f.resolution ? `<br><small>${h(f.resolution)}</small>` : ''}</li>`).join('')}</ul>
        </details>` : '';

        return `<div class="card task-spec-card" data-id="${h(doc.id)}">
            <div class="card-meta">
                <span class="tag task-status-${h(doc.status)}">${h(statusLabels[doc.status] || doc.status)}</span>
                <span class="tag">${h(exec.complexity || 'standard')}</span>
                <span class="tag">🤖 ${h(model.preferred_model || model.recommended_tier || 'auto')}</span>
                <span class="tag">⏱ ${h(scheduleText)}</span>
                ${doc.enabled ? '<span class="tag task-enabled">调度已启用</span>' : ''}
            </div>
            <h3 class="card-title">${h(doc.name)}</h3>
            <div class="card-snippet">${h(task.objective || '')}</div>
            <div class="task-spec-summary">Token ${h(budget.max_total_tokens || 0)} · Steps ${h(budget.max_steps || 0)} · ${h(last)}</div>
            ${findingHtml}
            ${doc.last_run_result ? `<details><summary>最近结果</summary><pre>${h(doc.last_run_result)}</pre></details>` : ''}
            <div class="task-spec-actions">
                <button class="task-btn task-edit">✏ 编辑</button>
                <button class="task-btn task-export">⬇ JSON</button>
                ${doc.status === 'draft' ? '<button class="task-btn task-confirm">✓ 确认草案</button>' : ''}
                ${doc.status === 'review_required' || doc.status === 'blocked' ? '<button class="task-btn task-validate">🔍 高价值复核</button>' : ''}
                ${doc.status === 'needs_ack' ? '<button class="task-btn task-ack">⚠ 接受建议风险</button>' : ''}
                ${doc.status === 'approved' ? '<button class="task-btn task-run">▶ 立即执行</button><button class="task-btn task-schedule">⏰ '+(doc.enabled ? '暂停调度' : '启用调度')+'</button>' : ''}
                <button class="task-btn task-delete">🗑</button>
            </div>
            <div class="task-editor" style="display:none"></div>
        </div>`;
    },

    async _loadMeta() {
        if (this._meta) return this._meta;
        const r = await fetch('/agent/api/v1/task-specs/meta');
        this._meta = await r.json();
        return this._meta;
    },

    async _showEditor(card, id) {
        const panel = card.querySelector('.task-editor');
        if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
        const [itemResp, meta] = await Promise.all([
            fetch(`/agent/api/v1/task-specs/${id}`).then(r => r.json()),
            this._loadMeta()
        ]);
        const s = itemResp.spec;
        const t = s.task || {};
        const e = s.execution || {};
        const mp = e.model_policy || {};
        const net = e.network || {};
        const b = e.budget || {};
        const sch = e.schedule || {};
        const out = s.output || {};
        const models = (meta.models || []).map(m =>
            `<option value="${h(m.name)}" ${mp.preferred_model === m.name ? 'selected' : ''}>${h(m.name)} · ${h(m.model)}</option>`
        ).join('');
        const caps = new Set((e.capabilities || []).map(x => typeof x === 'string' ? x : x.name));
        const capChecks = Object.keys(meta.capabilities || {}).map(name =>
            `<label><input type="checkbox" class="task-cap" value="${h(name)}" ${caps.has(name) ? 'checked' : ''}> ${h(name)}</label>`
        ).join('');
        panel.innerHTML = `
            <div class="task-policy-note">🔒 ${h(s.contract.policy_profile)} · revision ${h(s.contract.revision)} · 系统策略字段不可编辑</div>
            <label>名称<input class="te-name" value="${h(t.name || '')}"></label>
            <label>目标<textarea class="te-objective">${h(t.objective || '')}</textarea></label>
            <label>背景<textarea class="te-context">${h(t.context || '')}</textarea></label>
            <div class="task-editor-grid">
                <label>复杂度<select class="te-complexity">
                    ${['simple','standard','complex'].map(x => `<option ${e.complexity===x?'selected':''}>${x}</option>`).join('')}
                </select></label>
                <label>执行模型<select class="te-model"><option value="">按成本等级自动选择</option>${models}</select></label>
                <label>推荐成本<select class="te-tier">${['low','standard','high'].map(x => `<option ${mp.recommended_tier===x?'selected':''}>${x}</option>`).join('')}</select></label>
                <label>联网<select class="te-network">${['forbidden','allowed','required'].map(x => `<option ${net.mode===x?'selected':''}>${x}</option>`).join('')}</select></label>
                <label>Token 上限<input type="number" class="te-tokens" value="${h(b.max_total_tokens || 50000)}"></label>
                <label>步骤上限<input type="number" class="te-steps" value="${h(b.max_steps || 20)}"></label>
                <label>总时长（秒）<input type="number" class="te-wall-seconds" value="${h(b.max_wall_seconds || 900)}"></label>
                <label>并发任务数<input type="number" class="te-parallel" value="${h(b.max_parallel_tasks || 3)}"></label>
                <label>调度<select class="te-schedule-mode">${['manual','once','repeat'].map(x => `<option ${sch.mode===x?'selected':''}>${x}</option>`).join('')}</select></label>
                <label>一次执行时间<input class="te-run-at" value="${h(sch.run_at || '')}" placeholder="ISO8601 含时区"></label>
                <label>重复规则<input class="te-cron" value="${h(sch.cron || '')}" placeholder="09:00 或 */15 * * * *"></label>
                <label>完整回复去向<select class="te-full-delivery">${['auto','email','hedgedoc','sqlite','inline'].map(x => `<option ${out.full_delivery===x?'selected':''}>${x}</option>`).join('')}</select></label>
                <label>聊天窗口内容<select class="te-reply-mode">${['summary','preview'].map(x => `<option ${out.reply_mode===x?'selected':''}>${x}</option>`).join('')}</select></label>
                <label>外部发布确认<input type="checkbox" class="te-publish-confirm" ${(e.approval||{}).confirmed?'checked':''}></label>
            </div>
            <fieldset><legend>能力</legend><div class="task-capabilities">${capChecks || '尚未配置 capability_map'}</div></fieldset>
            <label>约束（每行一条）<textarea class="te-constraints">${h((t.constraints || []).join('\n'))}</textarea></label>
            <label>验收标准（每行一条）<textarea class="te-criteria">${h((t.acceptance_criteria || []).join('\n'))}</textarea></label>
            <label>执行计划（JSON 数组，高级）<textarea class="te-plan task-plan-json">${h(JSON.stringify(e.plan || [], null, 2))}</textarea></label>
            <div class="task-editor-errors"></div>
            <div class="task-spec-actions"><button class="task-btn task-save">💾 保存并等待复核</button><button class="task-btn task-cancel">取消</button></div>`;
        panel.style.display = 'block';

        panel.querySelector('.task-cancel').onclick = () => { panel.style.display = 'none'; };
        panel.querySelector('.task-save').onclick = async (event) => {
            const btn = event.currentTarget;
            const err = panel.querySelector('.task-editor-errors');
            btn.disabled = true; err.textContent = '';
            try {
                const lines = cls => panel.querySelector(cls).value.split('\n').map(x => x.trim()).filter(Boolean);
                const updated = JSON.parse(JSON.stringify(s));
                updated.task.name = panel.querySelector('.te-name').value.trim();
                updated.task.objective = panel.querySelector('.te-objective').value.trim();
                updated.task.context = panel.querySelector('.te-context').value.trim();
                updated.task.constraints = lines('.te-constraints');
                updated.task.acceptance_criteria = lines('.te-criteria');
                updated.execution.complexity = panel.querySelector('.te-complexity').value;
                updated.execution.model_policy.preferred_model = panel.querySelector('.te-model').value;
                updated.execution.model_policy.user_locked = !!updated.execution.model_policy.preferred_model;
                updated.execution.model_policy.recommended_tier = panel.querySelector('.te-tier').value;
                updated.execution.network.mode = panel.querySelector('.te-network').value;
                updated.execution.budget.max_total_tokens = Number(panel.querySelector('.te-tokens').value);
                updated.execution.budget.max_steps = Number(panel.querySelector('.te-steps').value);
                updated.execution.budget.max_wall_seconds = Number(panel.querySelector('.te-wall-seconds').value);
                updated.execution.budget.max_parallel_tasks = Number(panel.querySelector('.te-parallel').value);
                updated.execution.capabilities = [...panel.querySelectorAll('.task-cap:checked')].map(x => x.value);
                updated.execution.schedule.mode = panel.querySelector('.te-schedule-mode').value;
                updated.execution.schedule.run_at = panel.querySelector('.te-run-at').value.trim();
                updated.execution.schedule.cron = panel.querySelector('.te-cron').value.trim();
                updated.execution.plan = JSON.parse(panel.querySelector('.te-plan').value || '[]');
                updated.execution.approval = updated.execution.approval || {side_effects: 'confirm'};
                updated.execution.approval.confirmed = panel.querySelector('.te-publish-confirm').checked;
                updated.output.full_delivery = panel.querySelector('.te-full-delivery').value;
                updated.output.reply_mode = panel.querySelector('.te-reply-mode').value;
                const r = await fetch(`/agent/api/v1/task-specs/${id}`, {
                    method: 'PATCH', headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({spec: updated})
                });
                const data = await r.json();
                if (!r.ok) throw new Error(data.error || '保存失败');
                await performSearch(false);
            } catch (ex) { err.textContent = ex.message; }
            finally { btn.disabled = false; }
        };
    },

    async _action(id, action, body) {
        const r = await fetch(`/agent/api/v1/task-specs/${id}/${action}`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify(body || {})
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || `${action} 失败`);
        return data;
    },

    onMount(container) {
        const header = container.querySelector('.results-header');
        if (header && !header.querySelector('.task-create-bar')) {
            const bar = document.createElement('div');
            bar.className = 'task-create-bar';
            bar.innerHTML = `<textarea id="task-new-goal" placeholder="描述复杂任务目标，高价值模型将生成可编辑规则…"></textarea>
                <button id="task-generate">✨ 生成任务规则</button>
                <button id="task-create-manual">＋ 手动创建</button>
                <button id="task-import">⬆ 导入 JSON</button>
                <input id="task-import-file" type="file" accept="application/json,.json" hidden>
                <span class="task-create-status"></span>`;
            header.appendChild(bar);
            const create = async generated => {
                const goal = bar.querySelector('#task-new-goal').value.trim();
                if (!goal) return;
                const status = bar.querySelector('.task-create-status');
                const buttons = bar.querySelectorAll('button'); buttons.forEach(x => x.disabled = true);
                status.textContent = generated ? '高价值模型正在生成…' : '正在创建…';
                try {
                    const url = generated ? '/agent/api/v1/task-specs/generate' : '/agent/api/v1/task-specs';
                    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({goal})});
                    const d = await r.json(); if (!r.ok) throw new Error(d.error || '创建失败');
                    bar.querySelector('#task-new-goal').value = ''; status.textContent = '';
                    await performSearch(false);
                } catch(ex) { status.textContent = ex.message; }
                finally { buttons.forEach(x => x.disabled = false); }
            };
            bar.querySelector('#task-generate').onclick = () => create(true);
            bar.querySelector('#task-create-manual').onclick = () => create(false);
            const fileInput = bar.querySelector('#task-import-file');
            bar.querySelector('#task-import').onclick = () => fileInput.click();
            fileInput.onchange = async () => {
                const file = fileInput.files?.[0]; if (!file) return;
                const status = bar.querySelector('.task-create-status');
                status.textContent = '正在导入并校验策略…';
                try {
                    const spec = JSON.parse(await file.text());
                    const r = await fetch('/agent/api/v1/task-specs', {
                        method:'POST', headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({spec})
                    });
                    const d = await r.json(); if (!r.ok) throw new Error(d.error || '导入失败');
                    status.textContent = ''; await performSearch(false);
                } catch(ex) { status.textContent = ex.message; }
                finally { fileInput.value = ''; }
            };
        }

        const grid = container.querySelector('#results-grid');
        if (grid && !this._clickHandler) {
            this._clickHandler = async event => {
                const card = event.target.closest('.task-spec-card');
                if (!card) return;
                const id = card.dataset.id;
                try {
                    if (event.target.closest('.task-edit')) return this._showEditor(card, id);
                    if (event.target.closest('.task-export')) {
                        const item = await fetch(`/agent/api/v1/task-specs/${id}`).then(r => r.json());
                        const blob = new Blob([JSON.stringify(item.spec, null, 2)], {type:'application/json'});
                        const url = URL.createObjectURL(blob); const a = document.createElement('a');
                        a.href = url; a.download = `task-spec-${id}.json`; a.click();
                        URL.revokeObjectURL(url); return;
                    }
                    if (event.target.closest('.task-confirm')) await this._action(id, 'confirm');
                    else if (event.target.closest('.task-validate')) await this._action(id, 'validate');
                    else if (event.target.closest('.task-ack')) {
                        const rationale = prompt('说明接受复核建议风险的原因（可选）') || '';
                        await this._action(id, 'acknowledge', {rationale});
                    } else if (event.target.closest('.task-run')) await this._action(id, 'run');
                    else if (event.target.closest('.task-schedule')) {
                        const enabled = !card.querySelector('.task-enabled');
                        await this._action(id, 'schedule', {enabled});
                    } else if (event.target.closest('.task-delete')) {
                        if (!confirm('永久删除这个复杂任务规则？')) return;
                        await fetch(`/agent/api/v1/task-specs/${id}`, {method:'DELETE'});
                    } else return;
                    await performSearch(false);
                } catch(ex) { alert(ex.message); }
            };
            grid.addEventListener('click', this._clickHandler);
        }
    },

    onUnmount(container) {
        container.querySelector('.task-create-bar')?.remove();
        const grid = container.querySelector('#results-grid');
        if (grid && this._clickHandler) grid.removeEventListener('click', this._clickHandler);
        this._clickHandler = null;
    }
});
