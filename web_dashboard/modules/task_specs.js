// Complex TaskSpec manager: author, edit, enrich, validate, run and schedule.
registerTabModule({
    id: 'task-specs',
    label: '复杂任务',
    icon: '🧩',
    badgeId: 'badge-task-specs',
    _clickHandler: null,
    _meta: null,
    _autoOpenId: null,
    _fallbackNotice: null,
    _enrichingIds: new Set(),

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
        const output = spec.output || {};
        const statusLabels = {
            draft: '草案', review_required: '待复核', blocked: '阻断',
            needs_ack: '待确认', approved: '已通过'
        };
        const deliveryLabels = {
            auto: '自动回退', email: '邮件', hedgedoc: 'HedgeDoc 公开',
            sqlite: '本地归档', inline: '聊天窗口完整输出'
        };
        const scheduleText = schedule.mode === 'once' ? `一次 ${schedule.run_at || ''}` :
            schedule.mode === 'repeat' ? `重复 ${schedule.cron || ''}` : '手动';
        const last = doc.last_run_status ? `${doc.last_run_status} ${doc.last_run_at || ''}` : '尚未运行';

        // Findings format
        const validation = spec.validation || {};
        const preflightFindings = validation.preflight?.findings || [];
        const reviewFindings = validation.findings || [];
        const allFindings = [...preflightFindings, ...reviewFindings];

        const findingHtml = allFindings.length ? `<details class="task-findings" open>
            <summary>校验与复核建议 (${allFindings.length})</summary>
            <ul class="task-findings-list">${allFindings.map(f => {
                const sev = (f.severity || (f.code && f.code.includes('ERR') ? 'error' : 'warning')).toLowerCase();
                const pathText = f.path ? ` [${h(f.path)}]` : '';
                return `<li class="task-finding-item severity-${sev}">
                    <span class="finding-code">${h(f.code || sev.toUpperCase())}${pathText}</span>: ${h(f.message || '')}
                    ${f.resolution ? `<div class="finding-res">💡 建议解决方式：${h(f.resolution)}</div>` : ''}
                </li>`;
            }).join('')}</ul>
        </details>` : '';

        const isAutoOpen = this._autoOpenId && String(this._autoOpenId) === String(doc.id);
        const fallbackBanner = (isAutoOpen && this._fallbackNotice) ? `
            <div class="task-fallback-banner">
                <span>⚠️</span>
                <span><strong>规则说明</strong>：${h(this._fallbackNotice)}</span>
            </div>` : '';

        const deliveryWarn = (output.full_delivery === 'hedgedoc' || output.full_delivery === 'auto') ?
            '<span class="tag" style="background:#451a03;color:#fdba74;" title="可能生成公开可访问链接">🌐 公开去向</span>' : '';

        const isEnriching = this._enrichingIds.has(String(doc.id));

        return `<div class="card task-spec-card" data-id="${h(doc.id)}">
            <div class="card-meta">
                <span class="tag task-status-${h(doc.status)}">${h(statusLabels[doc.status] || doc.status)}</span>
                <span class="tag">${h(exec.complexity || 'standard')}</span>
                <span class="tag">🤖 ${h(model.preferred_model || model.recommended_tier || 'auto')}</span>
                <span class="tag">⏱ ${h(scheduleText)}</span>
                <span class="tag">📤 ${h(deliveryLabels[output.full_delivery] || output.full_delivery || 'auto')}</span>
                ${deliveryWarn}
                ${doc.enabled ? '<span class="tag task-enabled">调度已启用</span>' : ''}
            </div>
            <h3 class="card-title">${h(doc.name || task.name || '未命名任务')}</h3>
            <div class="card-snippet">${h(task.objective || '')}</div>
            <div class="task-spec-summary">Token ${h(budget.max_total_tokens || 0)} · Steps ${h(budget.max_steps || 0)} · 时长 ${h(budget.max_wall_seconds || 900)}s · ${h(last)}</div>
            ${fallbackBanner}
            ${findingHtml}
            ${doc.last_run_result ? `<details><summary>最近运行结果</summary><pre>${h(doc.last_run_result)}</pre></details>` : ''}
            <div class="task-spec-actions">
                <button class="task-btn task-edit">✏ 编辑规则</button>
                <button class="task-btn task-enrich" ${isEnriching ? 'disabled' : ''} style="background:rgba(56,189,248,0.12);border-color:rgba(56,189,248,0.3);color:#7dd3fc;">
                    ${isEnriching ? '⏳ AI 完善中…' : '✨ AI 完善规则 (可选)'}
                </button>
                <button class="task-btn task-export">⬇ 导出 JSON</button>
                ${doc.status === 'draft' ? '<button class="task-btn task-btn-primary task-confirm">✓ 确认草案</button>' : ''}
                ${doc.status === 'review_required' || doc.status === 'blocked' ? '<button class="task-btn task-btn-primary task-validate">🔍 高价值复核</button>' : ''}
                ${doc.status === 'needs_ack' ? '<button class="task-btn task-ack" style="background:#78350f;color:#fef08a;">⚠ 接受建议风险</button><button class="task-btn task-validate">🔍 重新复核</button>' : ''}
                ${doc.status === 'approved' ? '<button class="task-btn task-btn-primary task-run">▶ 立即执行</button><button class="task-btn task-schedule">⏰ '+(doc.enabled ? '暂停调度' : '启用调度')+'</button>' : ''}
                <button class="task-btn task-delete" title="删除任务">🗑</button>
            </div>
            <div class="task-editor" style="display:${isAutoOpen ? 'block' : 'none'}"></div>
        </div>`;
    },

    async _loadMeta() {
        if (this._meta) return this._meta;
        try {
            const r = await fetch('/agent/api/v1/task-specs/meta');
            this._meta = await r.json();
        } catch {
            this._meta = { models: [], capabilities: {} };
        }
        return this._meta;
    },

    async _showEditor(card, id) {
        const panel = card.querySelector('.task-editor');
        if (panel.style.display !== 'none' && panel.innerHTML.trim() !== '') {
            panel.style.display = 'none';
            return;
        }
        panel.innerHTML = '<div style="color:#9ca3af;padding:10px;text-align:center;">正在加载任务详情与能力定义...</div>';
        panel.style.display = 'block';

        let itemResp, meta;
        try {
            [itemResp, meta] = await Promise.all([
                fetch(`/agent/api/v1/task-specs/${id}`).then(r => r.json()),
                this._loadMeta()
            ]);
        } catch (ex) {
            panel.innerHTML = `<div class="task-editor-errors" style="display:block">加载失败: ${h(ex.message)}</div>`;
            return;
        }

        const s = itemResp.spec || {};
        const t = s.task || {};
        const e = s.execution || {};
        const mp = e.model_policy || {};
        const net = e.network || {};
        const b = e.budget || {};
        const sch = e.schedule || {};
        const out = s.output || {};
        const app = e.approval || {};
        const contract = s.contract || { policy_profile: 'default', revision: 1 };

        // Model options
        const models = (meta.models || []).map(m =>
            `<option value="${h(m.name)}" ${mp.preferred_model === m.name ? 'selected' : ''}>${h(m.name)} · ${h(m.model)}</option>`
        ).join('');
        const tierByModel = {};
        Object.entries(meta.model_tiers || {}).forEach(([tier, names]) => {
            (names || []).forEach(name => { tierByModel[name] = tier; });
        });
        const tierRank = { high: 0, standard: 1, low: 2 };
        const authorModels = [...(meta.models || [])].sort((a, b) =>
            (tierRank[tierByModel[a.name]] ?? 9) - (tierRank[tierByModel[b.name]] ?? 9)
        );
        const authorModelOptions = authorModels.map(m =>
            `<option value="${h(m.name)}">${tierByModel[m.name] === 'high' ? '高价值 · ' : ''}${h(m.name)} · ${h(m.model)}</option>`
        ).join('');

        // Capabilities options
        const caps = new Set((e.capabilities || []).map(x => typeof x === 'string' ? x : x.name));
        const capChecks = Object.keys(meta.capabilities || {}).map(name => {
            const capInfo = meta.capabilities[name] || {};
            const desc = capInfo.description ? ` title="${h(capInfo.description)}"` : '';
            return `<label class="task-cap-item"${desc}><input type="checkbox" class="task-cap" value="${h(name)}" ${caps.has(name) ? 'checked' : ''}> ${h(name)}</label>`;
        }).join('');

        // Initial plan steps array
        let planSteps = Array.isArray(e.plan) ? JSON.parse(JSON.stringify(e.plan)) : [];
        if (planSteps.length === 0 && typeof e.plan === 'object' && e.plan !== null && e.plan.steps) {
            planSteps = e.plan.steps;
        }

        // Required inputs use the backend's keyed object contract. Keep legacy
        // array support only so older browser drafts remain editable.
        const rawReqInputs = t.required_inputs || {};
        let reqInputs = Array.isArray(rawReqInputs) ? rawReqInputs.map(inp => ({
            name: typeof inp === 'string' ? inp : (inp.name || ''),
            description: typeof inp === 'object' ? (inp.description || '') : '',
            required: typeof inp === 'object' ? inp.required !== false : true,
            value: typeof inp === 'object' ? (inp.value ?? '') : '',
            suggestedValue: typeof inp === 'object' ? (inp.suggested_value ?? '') : ''
        })) : Object.entries(rawReqInputs).map(([name, inputSpec]) => {
            const detail = inputSpec && typeof inputSpec === 'object' && !Array.isArray(inputSpec)
                ? inputSpec : { value: inputSpec };
            return {
                name,
                description: detail.description || '',
                required: detail.required !== false,
                value: detail.value ?? '',
                suggestedValue: detail.suggested_value ?? ''
            };
        });

        panel.innerHTML = `
            <div class="task-policy-note">
                🔒 <strong>不可变策略</strong>：${h(contract.policy_profile)} · revision ${h(contract.revision)} (系统核心安全基线，只读保护)
            </div>

            <div class="task-editor-section-title">📋 基础信息与目标</div>
            <label>任务名称<input class="te-name" value="${h(t.name || s.name || '')}" placeholder="例如：每日外币账单汇率统计与归档"></label>
            <label>任务目标 (Objective)<textarea class="te-objective" placeholder="清晰定义任务要达成的具体目标…">${h(t.objective || '')}</textarea></label>
            <label>背景信息 (Context)<textarea class="te-context" placeholder="补充任务执行的上下文背景、前置条件或业务规则…">${h(t.context || '')}</textarea></label>

            <div class="task-editor-section-title">⚙️ 执行与模型预算配置</div>
            <div class="task-editor-grid">
                <label>复杂度等级
                    <select class="te-complexity">
                        ${['simple','standard','complex'].map(x => `<option value="${x}" ${e.complexity===x?'selected':''}>${x}</option>`).join('')}
                    </select>
                </label>
                <label>指定执行模型
                    <select class="te-model">
                        <option value="">按成本等级自动选择</option>
                        ${models}
                    </select>
                </label>
                <label>推荐成本等级
                    <select class="te-tier">
                        ${['low','standard','high'].map(x => `<option value="${x}" ${mp.recommended_tier===x?'selected':''}>${x}</option>`).join('')}
                    </select>
                </label>
                <label>联网权限
                    <select class="te-network">
                        <option value="forbidden" ${net.mode==='forbidden'?'selected':''}>禁止联网 (forbidden)</option>
                        <option value="allowed" ${net.mode==='allowed'?'selected':''}>允许联网 (allowed)</option>
                        <option value="required" ${net.mode==='required'?'selected':''}>必须联网 (required)</option>
                    </select>
                </label>
                <label>Token 预算上限<input type="number" class="te-tokens" value="${h(b.max_total_tokens || 50000)}"></label>
                <label>步骤数量上限<input type="number" class="te-steps" value="${h(b.max_steps || 20)}"></label>
                <label>总时长上限（秒）<input type="number" class="te-wall-seconds" value="${h(b.max_wall_seconds || 900)}"></label>
                <label>并发子任务数<input type="number" class="te-parallel" value="${h(b.max_parallel_tasks || 3)}"></label>
                <label>调度模式
                    <select class="te-schedule-mode">
                        <option value="manual" ${sch.mode==='manual'?'selected':''}>手动触发 (manual)</option>
                        <option value="once" ${sch.mode==='once'?'selected':''}>单次定时 (once)</option>
                        <option value="repeat" ${sch.mode==='repeat'?'selected':''}>周期重复 (repeat)</option>
                    </select>
                </label>
                <label class="te-schedule-run-at-wrap" style="display:${sch.mode==='once'?'flex':'none'}">单次执行时间
                    <input class="te-run-at" value="${h(sch.run_at || '')}" placeholder="2026-08-25T09:00:00+08:00">
                </label>
                <label class="te-schedule-cron-wrap" style="display:${sch.mode==='repeat'?'flex':'none'}">重复 Cron / 快捷时间
                    <input class="te-cron" value="${h(sch.cron || '')}" placeholder="09:00 或 0 9 * * *">
                </label>
            </div>

            <div class="task-editor-section-title">📤 输出与交付策略</div>
            <div class="task-editor-grid">
                <label>完整结果交付去向
                    <select class="te-full-delivery">
                        <option value="auto" ${out.full_delivery==='auto'?'selected':''}>自动根据内容回退 (auto)</option>
                        <option value="email" ${out.full_delivery==='email'?'selected':''}>发送邮件 (email)</option>
                        <option value="hedgedoc" ${out.full_delivery==='hedgedoc'?'selected':''}>发布到 HedgeDoc 笔记 (hedgedoc)</option>
                        <option value="sqlite" ${out.full_delivery==='sqlite'?'selected':''}>仅本地 SQLite 归档 (sqlite)</option>
                        <option value="inline" ${out.full_delivery==='inline'?'selected':''}>聊天窗口直接完整输出 (inline)</option>
                    </select>
                </label>
                <label>聊天窗口即时内容
                    <select class="te-reply-mode">
                        <option value="summary" ${out.reply_mode==='summary'?'selected':''}>提炼精简摘要 (summary)</option>
                        <option value="preview" ${out.reply_mode==='preview'?'selected':''}>保留原文预览 (preview)</option>
                    </select>
                </label>
            </div>
            <div class="te-hedgedoc-warning task-warning-banner" style="display:${(out.full_delivery==='hedgedoc'||out.full_delivery==='auto')?'block':'none'}">
                ⚠️ <strong>公开访问提示</strong>：交付去向包含 HedgeDoc 或 Auto 时，可能生成公开可访问的笔记链接，请确保处理数据无敏感隐私。
            </div>
            <label style="flex-direction:row;align-items:center;gap:8px;margin-top:6px;cursor:pointer;">
                <input type="checkbox" class="te-publish-confirm" style="width:auto;" ${app.confirmed?'checked':''}>
                <span>已明确知晓潜在外部副作用或外部发布风险 (Confirm external actions)</span>
            </label>

            <div class="task-editor-section-title">🛠 所需能力集 (Capabilities)</div>
            <div class="task-capabilities">${capChecks || '<span style="color:#6b7280">暂无能力定义</span>'}</div>

            <div class="task-editor-section-title">📌 规则约束与验收标准</div>
            <div class="task-req-inputs-container">
                <div class="task-req-inputs-heading">
                    <span>必需输入 (Required Inputs)</span>
                    <small>AI 识别出的阻断条件会自动显示；带 <b>*</b> 的值必须填写</small>
                </div>
                <div class="task-req-inputs-list"></div>
                <button type="button" class="task-btn task-add-req-input" style="font-size:0.76rem;align-self:flex-start;">＋ 添加输入项</button>
            </div>
            <label>约束条件（Constraints，每行一条）
                <textarea class="te-constraints" placeholder="例如：禁止使用需要高昂付费的第三方外部商业 API">${h((t.constraints || []).join('\n'))}</textarea>
            </label>
            <label>验收标准（Acceptance Criteria，每行一条）
                <textarea class="te-criteria" placeholder="例如：必须生成包含币种、金额、汇率与人民币折算的总计表格">${h((t.acceptance_criteria || []).join('\n'))}</textarea>
            </label>

            <div class="task-editor-section-title">
                <span>📝 执行计划 (Execution Plan)</span>
            </div>
            <div class="task-plan-container">
                <div class="task-plan-header">
                    <span style="font-size:0.8rem;color:#94a3b8;">支持可视化步骤拖排或直接编辑 JSON</span>
                    <div class="task-mode-toggle">
                        <button type="button" class="task-mode-btn mode-visual active">可视步骤列表</button>
                        <button type="button" class="task-mode-btn mode-json">JSON 源码</button>
                    </div>
                </div>
                <div class="task-plan-visual-wrap">
                    <div class="task-steps-list"></div>
                    <button type="button" class="task-btn task-add-step" style="font-size:0.8rem;">＋ 添加执行步骤</button>
                </div>
                <div class="task-plan-json-wrap" style="display:none;">
                    <textarea class="te-plan task-plan-json" placeholder="[ { 'id': 1, 'name': '步骤名', 'capability': '...' } ]">${h(JSON.stringify(planSteps, null, 2))}</textarea>
                </div>
            </div>

            <div class="task-editor-errors"></div>
            <div class="task-spec-actions" style="margin-top:16px;">
                <button class="task-btn task-btn-primary task-save">💾 保存规则并等待复核</button>
                <button class="task-btn task-save-validate" style="background:#1e3a8a;border-color:#3b82f6;color:#dbeafe;">💾 保存并立即复核</button>
                <label class="task-enrich-model-wrap">AI 完善模型（仅本次）
                    <select class="task-enrich-model">
                        <option value="">系统默认${meta.author_model ? `：${h(meta.author_model)}` : ''}</option>
                        ${authorModelOptions}
                    </select>
                </label>
                <button class="task-btn task-editor-enrich" style="background:rgba(56,189,248,0.12);border-color:rgba(56,189,248,0.3);color:#7dd3fc;">✨ AI 完善规则</button>
                <button class="task-btn task-cancel">取消</button>
            </div>
        `;

        // Interactive Plan Step List Render Helper
        const capNames = Object.keys(meta.capabilities || {});
        const renderSteps = () => {
            const listEl = panel.querySelector('.task-steps-list');
            if (!listEl) return;
            if (planSteps.length === 0) {
                listEl.innerHTML = '<div style="color:#64748b;font-size:0.8rem;padding:8px 0;">暂无步骤，点击下方按钮添加第一个执行步骤，或点击“AI 完善规则”自动生成。</div>';
                return;
            }
            listEl.innerHTML = planSteps.map((step, idx) => {
                const sid = step.id || (idx + 1);
                const sname = step.name || step.title || `步骤 ${idx + 1}`;
                const scap = step.capability || step.action || '';
                const sdesc = step.description || step.input || (typeof step.params === 'object' ? JSON.stringify(step.params) : (step.params || ''));
                const sdep = Array.isArray(step.depends_on) ? step.depends_on.join(', ') : (step.depends_on || '');
                const capOpts = `<option value="">-- 选择执行能力 --</option>` + capNames.map(cn => `<option value="${h(cn)}" ${scap === cn ? 'selected' : ''}>${h(cn)}</option>`).join('');

                return `<div class="task-step-card" data-idx="${idx}">
                    <div class="task-step-card-header">
                        <span class="task-step-badge">Step ${idx + 1} (ID: ${h(sid)})</span>
                        <div class="task-step-card-actions">
                            <button type="button" class="task-btn task-step-btn step-move-up" ${idx === 0 ? 'disabled' : ''} title="上移">▲</button>
                            <button type="button" class="task-btn task-step-btn step-move-down" ${idx === planSteps.length - 1 ? 'disabled' : ''} title="下移">▼</button>
                            <button type="button" class="task-btn task-step-btn step-delete" style="color:#f87171;" title="删除">✕</button>
                        </div>
                    </div>
                    <div class="task-step-grid">
                        <input class="step-name-input" value="${h(sname)}" placeholder="步骤动作名称 (如: 读取当月账单)">
                        <select class="step-cap-input">${capOpts}</select>
                    </div>
                    <input class="step-desc-input" value="${h(sdesc)}" placeholder="输入参数或具体指示描述">
                    <input class="step-dep-input" value="${h(sdep)}" placeholder="前置依赖步骤 ID (例如: 1, 2，可选)">
                </div>`;
            }).join('');
        };

        const syncStepsFromDOM = () => {
            const stepCards = panel.querySelectorAll('.task-step-card');
            const newSteps = [];
            stepCards.forEach((card, idx) => {
                const name = card.querySelector('.step-name-input').value.trim();
                const capability = card.querySelector('.step-cap-input').value;
                const desc = card.querySelector('.step-desc-input').value.trim();
                const depStr = card.querySelector('.step-dep-input').value.trim();
                const deps = depStr ? depStr.split(',').map(x => x.trim()).filter(Boolean) : [];
                newSteps.push({
                    id: idx + 1,
                    name: name || `步骤 ${idx + 1}`,
                    capability: capability || undefined,
                    description: desc || undefined,
                    depends_on: deps.length > 0 ? deps : undefined
                });
            });
            planSteps = newSteps;
            const jsonArea = panel.querySelector('.te-plan');
            if (jsonArea) jsonArea.value = JSON.stringify(planSteps, null, 2);
        };

        renderSteps();

        // Step list event delegation
        const stepsListEl = panel.querySelector('.task-steps-list');
        stepsListEl.oninput = () => syncStepsFromDOM();
        stepsListEl.onchange = () => syncStepsFromDOM();
        stepsListEl.onclick = (ev) => {
            const card = ev.target.closest('.task-step-card');
            if (!card) return;
            const idx = Number(card.dataset.idx);
            syncStepsFromDOM();
            if (ev.target.closest('.step-delete')) {
                planSteps.splice(idx, 1);
                renderSteps();
                syncStepsFromDOM();
            } else if (ev.target.closest('.step-move-up') && idx > 0) {
                const tmp = planSteps[idx - 1];
                planSteps[idx - 1] = planSteps[idx];
                planSteps[idx] = tmp;
                renderSteps();
                syncStepsFromDOM();
            } else if (ev.target.closest('.step-move-down') && idx < planSteps.length - 1) {
                const tmp = planSteps[idx + 1];
                planSteps[idx + 1] = planSteps[idx];
                planSteps[idx] = tmp;
                renderSteps();
                syncStepsFromDOM();
            }
        };

        panel.querySelector('.task-add-step').onclick = () => {
            syncStepsFromDOM();
            planSteps.push({
                id: planSteps.length + 1,
                name: '',
                capability: '',
                description: ''
            });
            renderSteps();
            syncStepsFromDOM();
        };

        // Plan visual vs JSON toggle
        const visualWrap = panel.querySelector('.task-plan-visual-wrap');
        const jsonWrap = panel.querySelector('.task-plan-json-wrap');
        const jsonTextarea = panel.querySelector('.te-plan');
        const modeVisualBtn = panel.querySelector('.mode-visual');
        const modeJsonBtn = panel.querySelector('.mode-json');

        modeVisualBtn.onclick = () => {
            try {
                const parsed = JSON.parse(jsonTextarea.value || '[]');
                planSteps = Array.isArray(parsed) ? parsed : (parsed.steps || []);
                renderSteps();
            } catch {}
            visualWrap.style.display = 'block';
            jsonWrap.style.display = 'none';
            modeVisualBtn.classList.add('active');
            modeJsonBtn.classList.remove('active');
        };

        modeJsonBtn.onclick = () => {
            syncStepsFromDOM();
            visualWrap.style.display = 'none';
            jsonWrap.style.display = 'block';
            modeJsonBtn.classList.add('active');
            modeVisualBtn.classList.remove('active');
        };

        // Required Inputs render helper
        const renderReqInputs = () => {
            const listEl = panel.querySelector('.task-req-inputs-list');
            if (!reqInputs.length) {
                listEl.innerHTML = '<div class="task-req-input-empty">当前任务没有需要用户补充的必需输入。</div>';
                return;
            }
            listEl.innerHTML = reqInputs.map((inp, idx) => {
                const iname = inp.name || '';
                const idesc = inp.description || '';
                const storedValue = inp.value == null ? '' : String(inp.value);
                const suggestedValue = inp.suggestedValue == null ? '' : String(inp.suggestedValue);
                const needsConfirmation = !storedValue && !!suggestedValue;
                const ivalue = storedValue || suggestedValue;
                const required = inp.required !== false;
                return `<div class="task-req-input-row" data-idx="${idx}" data-suggested-value="${h(suggestedValue)}" data-confirmed="${needsConfirmation ? 'false' : 'true'}">
                    <div class="task-req-input-row-header">
                        <span class="req-input-title"><code>${h(iname || '新输入项')}</code><b class="req-input-star" style="display:${required ? 'inline' : 'none'}">*</b></span>
                        <div class="req-input-row-actions">
                            ${needsConfirmation ? '<span class="req-input-confirm-state pending">AI 建议，待确认</span><button type="button" class="task-btn task-step-btn req-input-confirm">确认使用</button>' : '<span class="req-input-confirm-state confirmed">已确认</span>'}
                            <label class="req-input-required-toggle">
                                <input type="checkbox" class="req-input-required" ${required ? 'checked' : ''}> 必填
                            </label>
                            <button type="button" class="task-btn task-step-btn req-input-delete" title="删除输入项" style="color:#f87171;">✕</button>
                        </div>
                    </div>
                    <div class="task-req-input-fields">
                        <label>参数名
                            <input class="req-input-name" value="${h(iname)}" placeholder="例如 bill_data_source">
                        </label>
                        <label>说明
                            <input class="req-input-desc" value="${h(idesc)}" placeholder="该输入项的含义或填写示例">
                        </label>
                        <label><span class="req-input-value-label">当前值${required ? ' *' : ''}</span>
                            <input class="req-input-value" value="${h(ivalue)}" placeholder="请填写后才能通过校验" ${required ? 'required' : ''}>
                        </label>
                    </div>
                </div>`;
            }).join('');
        };

        renderReqInputs();
        const reqListEl = panel.querySelector('.task-req-inputs-list');
        const syncReqInputsFromDOM = () => {
            reqInputs = [...panel.querySelectorAll('.task-req-input-row')].map(row => ({
                name: row.querySelector('.req-input-name').value.trim(),
                description: row.querySelector('.req-input-desc').value.trim(),
                required: row.querySelector('.req-input-required').checked,
                value: row.dataset.confirmed === 'true'
                    ? row.querySelector('.req-input-value').value.trim() : '',
                suggestedValue: row.dataset.suggestedValue || ''
            }));
        };
        reqListEl.onclick = (ev) => {
            if (ev.target.closest('.req-input-confirm')) {
                const row = ev.target.closest('.task-req-input-row');
                row.dataset.confirmed = 'true';
                const state = row.querySelector('.req-input-confirm-state');
                state.textContent = '已确认';
                state.className = 'req-input-confirm-state confirmed';
                ev.target.closest('.req-input-confirm').remove();
                return;
            }
            if (ev.target.closest('.req-input-delete')) {
                const row = ev.target.closest('.task-req-input-row');
                const idx = Number(row.dataset.idx);
                syncReqInputsFromDOM();
                reqInputs.splice(idx, 1);
                renderReqInputs();
            }
        };
        reqListEl.onfocusout = (ev) => {
            if (!ev.target.classList.contains('req-input-value')) return;
            const row = ev.target.closest('.task-req-input-row');
            if (!ev.target.value.trim()) return;
            row.dataset.confirmed = 'true';
            const state = row.querySelector('.req-input-confirm-state');
            if (state) {
                state.textContent = '已确认';
                state.className = 'req-input-confirm-state confirmed';
            }
            row.querySelector('.req-input-confirm')?.remove();
        };
        reqListEl.oninput = (ev) => {
            const row = ev.target.closest('.task-req-input-row');
            if (!row) return;
            if (ev.target.classList.contains('req-input-name')) {
                row.querySelector('.req-input-title code').textContent = ev.target.value.trim() || '新输入项';
            }
        };
        reqListEl.onchange = (ev) => {
            if (!ev.target.classList.contains('req-input-required')) return;
            const row = ev.target.closest('.task-req-input-row');
            const required = ev.target.checked;
            row.querySelector('.req-input-star').style.display = required ? 'inline' : 'none';
            const valueInput = row.querySelector('.req-input-value');
            valueInput.required = required;
            row.querySelector('.req-input-value-label').textContent = required ? '当前值 *' : '当前值';
        };
        panel.querySelector('.task-add-req-input').onclick = () => {
            syncReqInputsFromDOM();
            reqInputs.push({
                name: '', description: '', required: true, value: '', suggestedValue: ''
            });
            renderReqInputs();
        };

        // Schedule mode dropdown change
        const schModeSelect = panel.querySelector('.te-schedule-mode');
        schModeSelect.onchange = () => {
            const mode = schModeSelect.value;
            panel.querySelector('.te-schedule-run-at-wrap').style.display = (mode === 'once') ? 'flex' : 'none';
            panel.querySelector('.te-schedule-cron-wrap').style.display = (mode === 'repeat') ? 'flex' : 'none';
        };

        // Delivery dropdown change
        const delSelect = panel.querySelector('.te-full-delivery');
        delSelect.onchange = () => {
            const v = delSelect.value;
            panel.querySelector('.te-hedgedoc-warning').style.display = (v === 'hedgedoc' || v === 'auto') ? 'block' : 'none';
        };

        panel.querySelector('.task-cancel').onclick = () => { panel.style.display = 'none'; };

        // Save handler
        const doSave = async (andValidate = false) => {
            const btn = andValidate ? panel.querySelector('.task-save-validate') : panel.querySelector('.task-save');
            const err = panel.querySelector('.task-editor-errors');
            btn.disabled = true; err.style.display = 'none'; err.textContent = '';
            try {
                // Ensure plan steps are synced
                if (visualWrap.style.display !== 'none') {
                    syncStepsFromDOM();
                } else {
                    planSteps = JSON.parse(jsonTextarea.value || '[]');
                }

                // Sync required inputs
                const reqRows = panel.querySelectorAll('.task-req-input-row');
                const finalReqInputs = {};
                reqRows.forEach(row => {
                    const n = row.querySelector('.req-input-name').value.trim();
                    const d = row.querySelector('.req-input-desc').value.trim();
                    const value = row.querySelector('.req-input-value').value.trim();
                    const required = row.querySelector('.req-input-required').checked;
                    const confirmed = row.dataset.confirmed === 'true';
                    const suggestedValue = row.dataset.suggestedValue || '';
                    if (!n) return;
                    if (Object.prototype.hasOwnProperty.call(finalReqInputs, n)) {
                        throw new Error(`必需输入名称重复：${n}`);
                    }
                    finalReqInputs[n] = {
                        description: d,
                        required,
                        value: confirmed ? value : '',
                        ...(suggestedValue ? { suggested_value: suggestedValue } : {})
                    };
                });

                const lines = cls => panel.querySelector(cls).value.split('\n').map(x => x.trim()).filter(Boolean);
                const updated = JSON.parse(JSON.stringify(s));
                updated.task = updated.task || {};
                updated.task.name = panel.querySelector('.te-name').value.trim();
                updated.task.objective = panel.querySelector('.te-objective').value.trim();
                updated.task.context = panel.querySelector('.te-context').value.trim();
                updated.task.required_inputs = finalReqInputs;
                updated.task.constraints = lines('.te-constraints');
                updated.task.acceptance_criteria = lines('.te-criteria');

                updated.execution = updated.execution || {};
                updated.execution.complexity = panel.querySelector('.te-complexity').value;
                updated.execution.model_policy = updated.execution.model_policy || {};
                updated.execution.model_policy.preferred_model = panel.querySelector('.te-model').value || undefined;
                updated.execution.model_policy.user_locked = !!updated.execution.model_policy.preferred_model;
                updated.execution.model_policy.recommended_tier = panel.querySelector('.te-tier').value;

                updated.execution.network = updated.execution.network || {};
                updated.execution.network.mode = panel.querySelector('.te-network').value;

                updated.execution.budget = updated.execution.budget || {};
                updated.execution.budget.max_total_tokens = Number(panel.querySelector('.te-tokens').value) || 50000;
                updated.execution.budget.max_steps = Number(panel.querySelector('.te-steps').value) || 20;
                updated.execution.budget.max_wall_seconds = Number(panel.querySelector('.te-wall-seconds').value) || 900;
                updated.execution.budget.max_parallel_tasks = Number(panel.querySelector('.te-parallel').value) || 3;

                updated.execution.capabilities = [...panel.querySelectorAll('.task-cap:checked')].map(x => x.value);

                updated.execution.schedule = updated.execution.schedule || {};
                updated.execution.schedule.mode = panel.querySelector('.te-schedule-mode').value;
                updated.execution.schedule.run_at = panel.querySelector('.te-run-at').value.trim() || undefined;
                updated.execution.schedule.cron = panel.querySelector('.te-cron').value.trim() || undefined;

                updated.execution.plan = planSteps;
                updated.execution.approval = updated.execution.approval || { side_effects: 'confirm' };
                updated.execution.approval.confirmed = panel.querySelector('.te-publish-confirm').checked;

                updated.output = updated.output || {};
                updated.output.full_delivery = panel.querySelector('.te-full-delivery').value;
                updated.output.reply_mode = panel.querySelector('.te-reply-mode').value;

                const r = await fetch(`/agent/api/v1/task-specs/${id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ spec: updated })
                });
                const data = await r.json();
                if (!r.ok) throw new Error(data.error || '保存失败');

                if (andValidate) {
                    await this._action(id, 'validate');
                }

                await performSearch(false);
            } catch (ex) {
                err.textContent = ex.message;
                err.style.display = 'block';
            } finally {
                btn.disabled = false;
            }
        };

        panel.querySelector('.task-save').onclick = () => doSave(false);
        panel.querySelector('.task-save-validate').onclick = () => doSave(true);
        panel.querySelector('.task-editor-enrich').onclick = async () => {
            await this._enrichTask(
                id, panel.querySelector('.task-enrich-model').value
            );
        };
    },

    async _action(id, action, body) {
        const r = await fetch(`/agent/api/v1/task-specs/${id}/${action}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || `${action} 失败`);
        return data;
    },

    async _enrichTask(id, model = '') {
        if (this._enrichingIds.has(String(id))) return;
        this._enrichingIds.add(String(id));
        await performSearch(false);

        try {
            const r = await fetch(`/agent/api/v1/task-specs/${id}/enrich`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model })
            });
            const d = await r.json();

            if (r.status === 409) {
                alert(`⚠️ AI 结果未覆盖你的修改：${d.error || '任务在 AI 完善期间已被修改；已保留你当前编辑的内容'}`);
                return;
            }

            if (!r.ok) {
                throw new Error(d.error || 'AI 完善失败');
            }

            if (d.generation && d.generation.status === 'fallback') {
                this._fallbackNotice = `AI 完善超时/降级：${d.generation.message || '基础规则已保留，可稍后重试'}`;
            } else {
                this._fallbackNotice = null;
            }

            this._autoOpenId = id;
        } catch (ex) {
            alert(`AI 完善任务失败: ${ex.message}`);
        } finally {
            this._enrichingIds.delete(String(id));
            await performSearch(false);
        }
    },

    onMount(container) {
        const header = container.querySelector('.results-header');
        if (header && !header.querySelector('.task-create-bar')) {
            const bar = document.createElement('div');
            bar.className = 'task-create-bar';
            bar.innerHTML = `
                <textarea id="task-new-goal" placeholder="描述复杂任务目标（例如：抓取最近一周外币消费邮件并汇总至 HedgeDoc），将立即生成结构化规则… (支持 Ctrl/Cmd + Enter 快捷创建)"></textarea>
                <div class="task-create-bar-buttons">
                    <select id="task-create-author-model" title="可选；基础规则始终先保存，再调用所选模型完善">
                        <option value="">仅创建基础规则（不调用 AI）</option>
                    </select>
                    <button id="task-create" class="task-btn-primary">＋ 创建任务规则</button>
                    <button id="task-import">⬆ 导入 JSON</button>
                    <input id="task-import-file" type="file" accept="application/json,.json" hidden>
                </div>
                <div class="task-create-status"></div>
            `;
            header.appendChild(bar);

            const goalTextarea = bar.querySelector('#task-new-goal');
            const createAuthorSelect = bar.querySelector('#task-create-author-model');
            this._loadMeta().then(meta => {
                const tierByModel = {};
                Object.entries(meta.model_tiers || {}).forEach(([tier, names]) => {
                    (names || []).forEach(name => { tierByModel[name] = tier; });
                });
                const tierRank = { high: 0, standard: 1, low: 2 };
                const sortedModels = [...(meta.models || [])].sort((a, b) =>
                    (tierRank[tierByModel[a.name]] ?? 9) - (tierRank[tierByModel[b.name]] ?? 9)
                );
                const options = sortedModels.map(m =>
                    `<option value="${h(m.name)}">创建后用 ${tierByModel[m.name] === 'high' ? '高价值 · ' : ''}${h(m.name)} 完善 · ${h(m.model)}</option>`
                ).join('');
                createAuthorSelect.insertAdjacentHTML('beforeend', options);
            });

            // Prevent Enter in goal textarea from submitting global search form
            goalTextarea.addEventListener('keydown', (ev) => {
                if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
                    ev.preventDefault();
                    bar.querySelector('#task-create').click();
                } else if (ev.key === 'Enter') {
                    ev.stopPropagation();
                }
            });

            const create = async () => {
                const goal = goalTextarea.value.trim();
                if (!goal) {
                    alert('请输入复杂任务目标描述');
                    goalTextarea.focus();
                    return;
                }
                const status = bar.querySelector('.task-create-status');
                const selectedAuthorModel = createAuthorSelect.value;
                const buttons = bar.querySelectorAll('button');
                buttons.forEach(x => x.disabled = true);
                status.innerHTML = '⚡ 正在创建基础规则…';

                try {
                    const r = await fetch('/agent/api/v1/task-specs', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ goal })
                    });
                    const d = await r.json();
                    if (!r.ok) throw new Error(d.error || '创建任务失败');

                    goalTextarea.value = '';
                    status.textContent = '';
                    this._fallbackNotice = null;

                    const newId = d.id || (d.data && d.data.id);
                    if (newId) {
                        this._autoOpenId = newId;
                    }

                    await performSearch(false);
                    if (newId && selectedAuthorModel) {
                        status.textContent = `✅ 基础规则已保存，正在用 ${selectedAuthorModel} 完善…`;
                        await this._enrichTask(newId, selectedAuthorModel);
                        status.textContent = '';
                    }
                } catch (ex) {
                    status.innerHTML = `❌ ${h(ex.message)}`;
                } finally {
                    buttons.forEach(x => x.disabled = false);
                }
            };

            bar.querySelector('#task-create').onclick = () => create();

            const fileInput = bar.querySelector('#task-import-file');
            bar.querySelector('#task-import').onclick = () => fileInput.click();
            fileInput.onchange = async () => {
                const file = fileInput.files?.[0];
                if (!file) return;
                const status = bar.querySelector('.task-create-status');
                status.textContent = '正在导入并执行确定性校验…';
                try {
                    const spec = JSON.parse(await file.text());
                    const r = await fetch('/agent/api/v1/task-specs', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ spec })
                    });
                    const d = await r.json();
                    if (!r.ok) throw new Error(d.error || '导入失败');
                    status.textContent = '✅ 导入成功';
                    setTimeout(() => { status.textContent = ''; }, 3000);
                    await performSearch(false);
                } catch (ex) {
                    status.textContent = `❌ ${ex.message}`;
                } finally {
                    fileInput.value = '';
                }
            };
        }

        const grid = container.querySelector('#results-grid');
        if (grid && !this._clickHandler) {
            this._clickHandler = async (event) => {
                const card = event.target.closest('.task-spec-card');
                if (!card) return;
                const id = card.dataset.id;
                try {
                    if (event.target.closest('.task-edit')) {
                        return this._showEditor(card, id);
                    }
                    if (event.target.closest('.task-enrich')) {
                        return this._enrichTask(id);
                    }
                    if (event.target.closest('.task-export')) {
                        const item = await fetch(`/agent/api/v1/task-specs/${id}`).then(r => r.json());
                        const blob = new Blob([JSON.stringify(item.spec, null, 2)], { type: 'application/json' });
                        const url = URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = `task-spec-${id}.json`;
                        a.click();
                        URL.revokeObjectURL(url);
                        return;
                    }
                    if (event.target.closest('.task-confirm')) {
                        await this._action(id, 'confirm');
                    } else if (event.target.closest('.task-validate')) {
                        await this._action(id, 'validate');
                    } else if (event.target.closest('.task-ack')) {
                        const rationale = prompt('请输入接受复核建议风险的原因（Rationale）:') || '';
                        if (!rationale.trim()) return;
                        await this._action(id, 'acknowledge', { rationale });
                    } else if (event.target.closest('.task-run')) {
                        await this._action(id, 'run');
                        alert('已提交执行任务，可在控制台或稍后刷新查看运行结果');
                    } else if (event.target.closest('.task-schedule')) {
                        const enabled = !card.querySelector('.task-enabled');
                        await this._action(id, 'schedule', { enabled });
                    } else if (event.target.closest('.task-delete')) {
                        if (!confirm('确定要永久删除该复杂任务规则吗？此操作不可逆。')) return;
                        const r = await fetch(`/agent/api/v1/task-specs/${id}`, { method: 'DELETE' });
                        if (!r.ok) {
                            const d = await r.json();
                            throw new Error(d.error || '删除失败');
                        }
                    } else {
                        return;
                    }
                    await performSearch(false);
                } catch (ex) {
                    alert(`操作失败: ${ex.message}`);
                }
            };
            grid.addEventListener('click', this._clickHandler);
        }
    },

    onUnmount(container) {
        container.querySelector('.task-create-bar')?.remove();
        const grid = container.querySelector('#results-grid');
        if (grid && this._clickHandler) grid.removeEventListener('click', this._clickHandler);
        this._clickHandler = null;
        this._autoOpenId = null;
        this._fallbackNotice = null;
        this._enrichingIds.clear();
    }
});
