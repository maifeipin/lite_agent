// ============================================================
//  Module: phone-apps (Phone App 执行层与三态商品筛选控制台)
//  遵循任务文档第 10 节与第 15 节规范及第一轮复核整改要求 (R05)：
//  - 接入真实创建按钮与宿主 showModal 弹窗组件
//  - 动态读取受控设备与已验证 App 目录，支持多选
//  - 完整录入销量、好评率、店铺资质、物流与预算
//  - AI 脚本安全审核、在线编辑保存与哈希防篡改
//  - 三态聚合结果展示 (符合 / 待核实 / 不符合) 与逐项依据
// ============================================================

registerTabModule({
    id: 'phone-apps',
    label: 'Phone App',
    icon: '📱',
    badgeId: 'badge-phone-apps',

    _activeBatchId: null,
    _pollTimer: null,
    _cachedDevices: [],
    _cachedApps: [],

    onMount(container) {
        window.PhoneAppUI.renderTopBar();
        window.PhoneAppUI.loadDevicesAndApps().then(() => {
            if (document.getElementById('phone-app-topbar')) {
                window.PhoneAppUI.renderTopBar();
            }
        });
    },

    onUnmount(container) {
        window.PhoneAppUI.removeTopBar();
    },

    async fetchCount() {
        try {
            const r = await fetch('/agent/api/v1/phone-app/batches');
            if (!r.ok) return 0;
            const d = await r.json();
            return (d.batches || []).length;
        } catch { return 0; }
    },

    async search(query, offset, limit) {
        try {
            const r = await fetch('/agent/api/v1/phone-app/batches');
            if (!r.ok) return { hits: [], total: 0 };
            const d = await r.json();
            let rows = (d.batches || []).map(b => { b._module = 'phone-apps'; return b; });
            if (query) {
                const q = query.toLowerCase();
                rows = rows.filter(b => (b.query || '').toLowerCase().includes(q) || (b.batch_id || '').includes(q));
            }
            return { hits: rows.slice(offset, offset + limit), total: rows.length };
        } catch {
            return { hits: [], total: 0 };
        }
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = count > 0 ? '' : 'none';
    },

    renderCard(batch) {
        const statusMap = {
            draft: { text: '草稿', color: '#888' },
            pending_review: { text: '待审核', color: '#e6a23c' },
            approved: { text: '已批准', color: '#409eff' },
            queued: { text: '排队中', color: '#909399' },
            running: { text: '执行中 ⏳', color: '#e6a23c' },
            succeeded: { text: '成功完成 ✅', color: '#67c23a' },
            partial_failed: { text: '部分失败 ⚠️', color: '#e6a23c' },
            failed: { text: '失败 ❌', color: '#f56c6c' },
            cancel_requested: { text: '正在停止，等待执行端确认', color: '#e6a23c' },
            cancelled: { text: '已取消 🛑', color: '#909399' }
        };
        const s = statusMap[batch.status] || { text: batch.status, color: '#aaa' };
        const apps = (batch.app_ids || []).join(', ') || 'jd';
        const created = new Date(batch.created_at * 1000).toLocaleString();

        let html = `<div class="card phone-batch-card" onclick="window.PhoneAppUI.openBatchDetail('${batch.batch_id}')">`;
        html += `<div class="card-meta">`;
        html += `<span class="badge" style="background:${s.color}; color:#fff; padding:2px 8px; border-radius:10px;">${s.text}</span>`;
        html += `<span class="tag">📱 设备: ${h(batch.device_id)}</span>`;
        html += `<span class="tag">📦 目标: ${h(apps)}</span>`;
        html += `<span class="date">${created}</span>`;
        html += `</div>`;
        html += `<h3 style="margin:8px 0; font-size:16px;">关键词: <strong>${h(batch.query)}</strong></h3>`;
        html += `<div style="font-size:12px; color:#888;">模式: ${batch.mode || 'fixed'} | 批次: ${batch.batch_id}</div>`;
        html += `<div style="margin-top:8px; display:flex; gap:8px;">`;
        html += `<button class="modal-btn modal-btn-secondary" style="font-size:12px; padding:3px 10px;" onclick="event.stopPropagation(); window.PhoneAppUI.openBatchDetail('${batch.batch_id}')">查看详情 / 结果</button>`;
        if (batch.status === 'pending_review') {
            html += `<button class="modal-btn modal-btn-primary" style="font-size:12px; padding:3px 10px; background:#e6a23c; border-color:#e6a23c;" onclick="event.stopPropagation(); window.PhoneAppUI.openBatchDetail('${batch.batch_id}')">审核脚本 🛡️</button>`;
            html += `<button class="modal-btn modal-btn-danger" style="font-size:12px; padding:3px 10px; background:#dc2626; color:#fff; border:none; border-radius:3px; cursor:pointer;" onclick="event.stopPropagation(); window.PhoneAppUI.rejectBatch('${batch.batch_id}')">驳回 ❌</button>`;
        } else if (batch.status === 'approved') {
            html += `<button class="modal-btn modal-btn-primary" style="font-size:12px; padding:3px 10px;" onclick="event.stopPropagation(); window.PhoneAppUI.executeBatch('${batch.batch_id}')">执行任务 ▶️</button>`;
        } else if (batch.status === 'queued' || batch.status === 'running') {
            html += `<button class="modal-btn modal-btn-danger" style="font-size:12px; padding:3px 10px; background:#ef4444; color:#fff; border:none; border-radius:3px; cursor:pointer;" onclick="event.stopPropagation(); window.PhoneAppUI.cancelBatch('${batch.batch_id}')">取消执行 🛑</button>`;
        }
        html += `</div>`;
        html += `</div>`;
        return html;
    }
});

// ============================================================
//  PhoneAppUI 全局交互控制器
// ============================================================
window.PhoneAppUI = {
    _cachedDevices: [],
    _cachedApps: [],
    _activeBatchId: null,
    _pollTimer: null,

    async init() {
        await this.loadDevicesAndApps();
    },

    renderTopBar() {
        this.removeTopBar();
        const header = document.querySelector('.results-header');
        if (!header) return;

        const bar = document.createElement('div');
        bar.id = 'phone-app-topbar';
        bar.style.cssText = 'display:flex; justify-content:space-between; align-items:center; width:100%; margin-bottom:12px; padding:10px 14px; background:#f1f5f9; border-radius:6px; border:1px solid #e2e8f0; box-sizing:border-box;';

        const isFake = window.PHONE_APP_TEST_MODE === true || (this._cachedDevices || []).some(d => d.test_mode === true);
        const onlineDevCount = (this._cachedDevices || []).filter(d => d.status === 'online').length;
        // 离线时直接披露可定位的原因 (adb 未识别 / 未授权 / hs 守护进程未绑定等)，不再只说"暂无在线设备"
        const offlineDev = (this._cachedDevices || []).find(d => d.status !== 'online' && d.status !== 'paused');
        const offlineHint = offlineDev ? (offlineDev.offline_reason || offlineDev.status_text || '') : '';
        const devText = onlineDevCount > 0
            ? `🟢 ${onlineDevCount} 台受控设备在线`
            : (offlineHint ? `🔴 离线：${offlineHint}` : `🔴 暂无在线设备`);
        const pausedDev = (this._cachedDevices || []).find(d => d.status === 'paused');
        // 设备标识来自 Worker 上报，进入 HTML 前必须转义 (含属性上下文)
        const pausedId = pausedDev ? h(pausedDev.device_id) : '';
        const resumeBtnHtml = pausedDev ? `<button class="modal-btn" style="font-size:12px; padding:5px 12px; background:#f59e0b; color:#fff; border:none; border-radius:4px; font-weight:bold; cursor:pointer;" data-device-id="${pausedId}" onclick="window.PhoneAppUI.resumeDevice(this.dataset.deviceId)">⚠️ 恢复设备 (${pausedId})</button>` : '';

        bar.innerHTML = `
            <div style="display:flex; align-items:center; gap:12px;">
                <span style="font-size:16px;">📱</span>
                <strong>${isFake ? "FAKE 测试：不操作手机" : "Phone App 执行与真机筛选中枢"}</strong>
                <span style="font-size:12px; color:#64748b; background:#fff; padding:2px 8px; border-radius:12px; border:1px solid #cbd5e1;">${h(devText)}</span>
            </div>
            <div style="display:flex; gap:8px;">
                ${resumeBtnHtml}
                <button class="modal-btn modal-btn-secondary" style="font-size:12px; padding:5px 12px;" onclick="window.PhoneAppUI.refreshBatches()">🔄 刷新列表</button>
                <button class="modal-btn modal-btn-primary" style="font-size:13px; padding:6px 16px; font-weight:bold; background:#0284c7; border-color:#0284c7;" onclick="window.PhoneAppUI.openCreateModal()">${isFake ? "➕ 新建 Fake 测试任务" : "➕ 新建手机筛选任务"}</button>
            </div>
        `;
        header.parentNode.insertBefore(bar, header.nextSibling);
    },

    async resumeDevice(deviceId) {
        try {
            const r = await fetch('/agent/api/v1/phone-app/device/resume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_id: deviceId })
            });
            const d = await r.json();
            if (d.success) {
                alert(`设备 ${deviceId} 已成功恢复就绪！`);
                await this.loadDevicesAndApps();
                this.renderTopBar();
            } else {
                alert(`恢复设备失败: ${d.error || '未知错误'}`);
            }
        } catch (e) {
            alert(`恢复请求失败: ${e.message}`);
        }
    },


    removeTopBar() {
        const bar = document.getElementById('phone-app-topbar');
        if (bar) bar.remove();
    },

    refreshBatches() {
        this.loadDevicesAndApps();
        if (typeof performSearch === 'function') performSearch(false);
    },

    async loadDevicesAndApps() {
        try {
            const [rDev, rApps] = await Promise.all([
                fetch('/agent/api/v1/phone-app/devices'),
                fetch('/agent/api/v1/phone-app/apps')
            ]);
            if (rDev.ok) {
                const d = await rDev.json();
                this._cachedDevices = d.devices || [];
            }
            if (rApps.ok) {
                const d = await rApps.json();
                this._cachedApps = d.apps || [];
            }
        } catch (e) {
            console.error('加载设备与应用目录失败:', e);
        }
    },

    async openCreateModal() {
        await this.loadDevicesAndApps();
        const devices = this._cachedDevices || [];
        const apps = this._cachedApps || [];

        const deviceOptions = devices.map(d => {
            const isOnline = d.status === 'online';
            const hint = d.offline_reason ? `：${d.offline_reason}` : '';
            const label = isOnline ? '🟢 在线已就绪' : `🔴 离线${hint}`;
            const tip = d.status_text || label;
            return `<option value="${h(d.device_id)}" title="${h(tip)}">${h(d.model || d.device_id)} [${h(label)}]</option>`;
        }).join('') || '<option value="c68a3cf2">OnePlus 9 (LE2110) [c68a3cf2]</option>';

        const appCheckboxes = apps.map(a => {
            const isValidated = a.status === 'validated';
            const isChecked = isValidated && a.app_id === 'jd' ? 'checked' : '';
            const isDisabled = isValidated ? '' : 'disabled';
            const statusLabel = isValidated ?
                '<span style="color:#16a34a; font-weight:bold;">[已验证]</span>' :
                `<span style="color:#94a3b8;">[${h(a.status)}: ${h(a.disabled_reason || '未完全验证')}]</span>`;
            return `
            <label style="display:inline-flex; align-items:center; gap:6px; margin-right:16px; margin-bottom:6px; font-size:13px; cursor:${isValidated ? 'pointer' : 'not-allowed'}; opacity:${isValidated ? '1' : '0.6'};">
                <input type="checkbox" class="pa-app-choice" value="${h(a.app_id)}" ${isChecked} ${isDisabled}>
                <span>${h(a.display_name)}</span> ${statusLabel}
            </label>
            `;
        }).join('') || '<label><input type="checkbox" class="pa-app-choice" value="jd" checked> 京东 (已验证)</label>';

        const contentHtml = `
        <div style="font-family:sans-serif; color:#1e293b; font-size:13px; line-height:1.5;">
            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px; padding:12px; margin-bottom:14px;">
                <div style="font-weight:bold; margin-bottom:8px; color:#0f172a;">① 受控手机与目标 App (支持多选)</div>
                <div style="display:flex; flex-direction:column; gap:10px;">
                    <div>
                        <label style="font-weight:600; margin-right:8px;">选择设备:</label>
                        <select id="pa-device-select" style="padding:6px 10px; border:1px solid #cbd5e1; border-radius:4px; font-size:13px; background:#fff;">
                            ${deviceOptions}
                        </select>
                    </div>
                    <div>
                        <div style="font-weight:600; margin-bottom:6px;">目标应用:</div>
                        <div id="pa-app-container">
                            ${appCheckboxes}
                        </div>
                    </div>
                </div>
            </div>

            <div style="margin-bottom:14px;">
                <label style="display:block; font-weight:bold; margin-bottom:6px; color:#0f172a;">② 搜索关键词:</label>
                <input type="text" id="pa-query-input" placeholder="例如: 机械键盘 / iPhone 16 / 保温杯" style="width:100%; padding:8px 10px; border:1px solid #cbd5e1; border-radius:4px; font-size:14px; box-sizing:border-box;">
            </div>

            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px; padding:12px; margin-bottom:14px;">
                <div style="font-weight:bold; margin-bottom:10px; color:#0f172a;">③ 确定性三态筛选规则 (符合 / 待核实 / 不符合)</div>
                <div style="display:grid; grid-template-columns: 1fr 1fr; gap:14px;">
                    <div>
                        <label style="display:block; font-weight:600; margin-bottom:4px;">累计销量筛选:</label>
                        <div style="display:flex; align-items:center; gap:6px;">
                            <span>大于</span>
                            <input type="number" id="pa-sales-threshold" value="1000" min="0" style="width:80px; padding:4px 8px; border:1px solid #cbd5e1; border-radius:4px;">
                            <span>件 (区间下界)</span>
                        </div>
                    </div>
                    <div>
                        <label style="display:block; font-weight:600; margin-bottom:4px;">好评率门槛 (%):</label>
                        <div style="display:flex; align-items:center; gap:6px;">
                            <span>不低于</span>
                            <input type="number" id="pa-rate-threshold" value="95.0" step="0.5" min="0" max="100" style="width:70px; padding:4px 8px; border:1px solid #cbd5e1; border-radius:4px;">
                            <span>% (需页面明示)</span>
                        </div>
                    </div>
                    <div>
                        <label style="display:block; font-weight:600; margin-bottom:4px;">店铺资质类型 (OR):</label>
                        <label style="margin-right:12px; cursor:pointer;"><input type="checkbox" id="pa-shop-self" checked> 自营</label>
                        <label style="cursor:pointer;"><input type="checkbox" id="pa-shop-official" checked> 官方旗舰店</label>
                    </div>
                    <div>
                        <label style="display:block; font-weight:600; margin-bottom:4px;">物流履约条件:</label>
                        <label style="margin-right:12px; cursor:pointer;"><input type="checkbox" id="pa-logistics-free" checked> 必须包邮</label>
                        <select id="pa-logistics-partner" style="padding:3px 6px; border:1px solid #cbd5e1; border-radius:4px; font-size:12px;">
                            <option value="jd_delivery">京东物流</option>
                            <option value="">不限配送方</option>
                        </select>
                    </div>
                    <div style="grid-column: span 2;">
                        <label style="display:block; font-weight:600; margin-bottom:4px; color:#64748b;">店铺第三方信用分 (京东搜索卡片未公开数值):</label>
                        <input type="text" disabled value="🔒 平台未公开三方评分数值，已强制依靠自营/官方旗舰店强资质校验，杜绝不可靠推测" style="width:100%; padding:4px 8px; border:1px solid #e2e8f0; background:#f1f5f9; color:#94a3b8; border-radius:4px; font-size:12px; box-sizing:border-box;">
                    </div>
                </div>
            </div>

            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px; padding:12px; margin-bottom:14px;">
                <div style="font-weight:bold; margin-bottom:8px; color:#0f172a;">④ 执行模式与安全控制</div>
                <div style="display:flex; gap:20px; font-size:13px;">
                    <label style="cursor:pointer; display:flex; align-items:center; gap:6px;">
                        <input type="radio" name="pa-mode-choice" value="fixed" checked>
                        <span><strong>预审核固定动作</strong> (首选推荐，已通过沙箱验证)</span>
                    </label>
                    <label style="cursor:pointer; display:flex; align-items:center; gap:6px;">
                        <input type="radio" name="pa-mode-choice" value="custom_ai">
                        <span><strong>AI 生成 DSL 脚本</strong> (严格审核后执行)</span>
                    </label>
                </div>
            </div>

            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px; padding:12px; margin-bottom:14px;">
                <div style="font-weight:bold; margin-bottom:8px; color:#0f172a;">⑤ 资源与预算控制 (Budget)</div>
                <div style="display:grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap:10px; font-size:12px;">
                    <div>
                        <label style="display:block; color:#475569; margin-bottom:2px;">扫描上限</label>
                        <input type="number" id="pa-budget-candidates" value="20" min="1" max="50" style="width:100%; padding:4px 6px; border:1px solid #cbd5e1; border-radius:4px; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="display:block; color:#475569; margin-bottom:2px;">结果上限</label>
                        <input type="number" id="pa-budget-results" value="5" min="1" max="20" style="width:100%; padding:4px 6px; border:1px solid #cbd5e1; border-radius:4px; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="display:block; color:#475569; margin-bottom:2px;">进详情上限</label>
                        <input type="number" id="pa-budget-details" value="5" min="0" max="10" style="width:100%; padding:4px 6px; border:1px solid #cbd5e1; border-radius:4px; box-sizing:border-box;">
                    </div>
                    <div>
                        <label style="display:block; color:#475569; margin-bottom:2px;">总超时(秒)</label>
                        <input type="number" id="pa-budget-timeout" value="180" min="30" max="600" style="width:100%; padding:4px 6px; border:1px solid #cbd5e1; border-radius:4px; box-sizing:border-box;">
                    </div>
                </div>
            </div>
        </div>
        `;

        if (typeof showModal === 'function') {
            showModal({
                title: window.PHONE_APP_TEST_MODE === true ? 'FAKE 商品筛选测试（不操作手机）' : '📱 发起手机商品条件筛选 (Phone App)',
                icon: '📱',
                width: '680px',
                content: contentHtml,
                buttons: [
                    {
                        text: '取消',
                        class: 'modal-btn-secondary',
                        onClick: ({ close }) => close()
                    },
                    {
                        text: '🚀 立即提交任务',
                        class: 'modal-btn-primary',
                        onClick: async ({ close }) => {
                            const success = await this.submitBatchFromForm();
                            if (success) close();
                        }
                    }
                ]
            });
        }
    },

    async submitBatchFromForm() {
        const query = document.getElementById('pa-query-input')?.value?.trim();
        if (!query) {
            alert('请输入搜索关键词');
            return false;
        }

        const deviceId = document.getElementById('pa-device-select')?.value || 'c68a3cf2';
        const checkedAppNodes = document.querySelectorAll('.pa-app-choice:checked');
        const appIds = Array.from(checkedAppNodes).map(n => n.value);
        if (appIds.length === 0) {
            alert('请至少选择一个已验证 App');
            return false;
        }

        const mode = document.querySelector('input[name="pa-mode-choice"]:checked')?.value || 'fixed';
        const salesThresh = parseInt(document.getElementById('pa-sales-threshold')?.value || '1000', 10);
        const rateThresh = parseFloat(document.getElementById('pa-rate-threshold')?.value || '95.0');
        const shopTypes = [];
        if (document.getElementById('pa-shop-self')?.checked) shopTypes.push('self_operated');
        if (document.getElementById('pa-shop-official')?.checked) shopTypes.push('official_flagship');

        const freeShipping = document.getElementById('pa-logistics-free')?.checked || false;
        const deliveryPartner = document.getElementById('pa-logistics-partner')?.value || null;

        const maxCandidates = parseInt(document.getElementById('pa-budget-candidates')?.value || '20', 10);
        const maxResults = parseInt(document.getElementById('pa-budget-results')?.value || '5', 10);
        const maxDetails = parseInt(document.getElementById('pa-budget-details')?.value || '5', 10);
        const totalTimeout = parseInt(document.getElementById('pa-budget-timeout')?.value || '180', 10);

        const payload = {
            query: query,
            app_ids: appIds,
            device_id: deviceId,
            action: 'search_products',
            mode: mode,
            filters: {
                sales: { metric: 'cumulative_sales', operator: 'gt', threshold: salesThresh },
                positive_rate: { operator: 'gte', threshold_percent: rateThresh },
                shop_types: { types: shopTypes },
                logistics: {
                    free_shipping: freeShipping,
                    delivery_partner: deliveryPartner
                }
            },
            budget: {
                max_candidates: maxCandidates,
                max_results: maxResults,
                max_detail_visits: maxDetails,
                total_timeout_seconds: totalTimeout
            }
        };

        try {
            const r = await fetch('/agent/api/v1/phone-app/batches', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const d = await r.json();
            if (!r.ok || !d.success) {
                alert('任务创建失败: ' + (d.error || r.statusText));
                return false;
            }

            const batchId = d.batch_id;
            if (mode === 'custom_ai') {
                const genRes = await fetch(`/agent/api/v1/phone-app/batches/${batchId}/generate`, { method: 'POST' });
                const genData = await genRes.json();
                if (!genRes.ok || !genData.success) {
                    alert('AI 脚本生成失败: ' + (genData.error || genRes.statusText));
                }
                this.openBatchDetail(batchId);
            } else {
                const execRes = await fetch(`/agent/api/v1/phone-app/batches/${batchId}/execute`, { method: 'POST' });
                const execData = await execRes.json();
                if (!execRes.ok || !execData.success) {
                    alert('派发执行失败: ' + (execData.error || execRes.statusText));
                }
                this.openBatchDetail(batchId);
            }

            if (typeof performSearch === 'function') performSearch(false);
            return true;
        } catch (e) {
            alert('网络请求异常: ' + e);
            return false;
        }
    },

    async openBatchDetail(batchId) {
        this._activeBatchId = batchId;
        try {
            const r = await fetch(`/agent/api/v1/phone-app/batches/${batchId}`);
            if (!r.ok) {
                alert('读取批次失败 (HTTP ' + r.status + ')');
                return;
            }
            const d = await r.json();
            this.renderDetailModal(d);
            if (['queued', 'running', 'cancel_requested'].includes(d.batch?.status)) {
                this.startEventPolling(batchId);
            } else if (this._pollTimer) {
                clearInterval(this._pollTimer);
                this._pollTimer = null;
            }
        } catch (e) {
            alert('读取批次详情失败: ' + e);
        }
    },

    renderDetailModal(data) {
        const b = data.batch || {};
        const isFake = window.PHONE_APP_TEST_MODE === true || data.test_mode === true;
        const summary = data.summary || {};
        const grouped = data.grouped_results || {};
        const matched = grouped.matched || [];
        const needsVer = grouped.needs_verification || [];
        const rejected = grouped.rejected || [];

        const statusMap = {
            draft: { text: '草稿', color: '#888' },
            pending_review: { text: '待审核', color: '#e6a23c' },
            approved: { text: '已批准', color: '#409eff' },
            queued: { text: '排队中', color: '#909399' },
            running: { text: '执行中 ⏳', color: '#e6a23c' },
            succeeded: { text: '成功完成 ✅', color: '#67c23a' },
            partial_failed: { text: '部分失败 ⚠️', color: '#e6a23c' },
            failed: { text: '失败 ❌', color: '#f56c6c' },
            cancel_requested: { text: '正在停止，等待执行端确认', color: '#e6a23c' },
            cancelled: { text: '已取消 🛑', color: '#909399' }
        };
        const s = statusMap[b.status] || { text: b.status, color: '#aaa' };

        let modalContent = `
        ${isFake ? '<div role="status" style="background:#fff3cd;color:#663c00;padding:12px;font-weight:bold">FAKE 测试结果：未操作手机，商品均为测试数据</div>' : ''}
        <div style="font-family:sans-serif; color:#1e293b; max-height:75vh; overflow-y:auto; padding-right:6px;">
            <div style="display:flex; justify-content:space-between; align-items:center; background:#f8fafc; padding:10px 14px; border-radius:6px; border:1px solid #e2e8f0; margin-bottom:14px;">
                <div>
                    <h3 style="margin:0 0 4px 0; font-size:16px;">搜索: <strong>${h(b.query)}</strong></h3>
                    <div style="font-size:12px; color:#64748b;">
                        批次: <code>${b.batch_id}</code> | 设备: <strong>${h(b.device_id)}</strong> | 应用: <strong>${h((b.app_ids || []).join(', '))}</strong>
                    </div>
                </div>
                <span style="background:${s.color}; color:#fff; font-size:13px; font-weight:bold; padding:4px 12px; border-radius:12px;">${s.text}</span>
            </div>

            <!-- 三态诊断汇总栏 -->
            <div style="display:flex; gap:12px; margin-bottom:16px;">
                <div style="flex:1; background:#f0fdf4; border:1px solid #bbf7d0; padding:12px; border-radius:6px; text-align:center;">
                    <div style="font-size:24px; font-weight:bold; color:#16a34a;">${summary.matched_count || 0}</div>
                    <div style="font-size:12px; color:#15803d; font-weight:600;">✅ 符合条件 (Matched)</div>
                </div>
                <div style="flex:1; background:#fefce8; border:1px solid #fef08a; padding:12px; border-radius:6px; text-align:center;">
                    <div style="font-size:24px; font-weight:bold; color:#ca8a04;">${summary.needs_verification_count || 0}</div>
                    <div style="font-size:12px; color:#a16207; font-weight:600;">🔍 待核实 (Needs Verification)</div>
                </div>
                <div style="flex:1; background:#fef2f2; border:1px solid #fecaca; padding:12px; border-radius:6px; text-align:center;">
                    <div style="font-size:24px; font-weight:bold; color:#dc2626;">${summary.rejected_count || 0}</div>
                    <div style="font-size:12px; color:#b91c1c; font-weight:600;">❌ 不符合 (Rejected)</div>
                </div>
            </div>

            <!-- 运行中 / 排队中 取消控制栏 -->
            ${(b.status === 'running' || b.status === 'queued') ? `
            <div style="background:#fff1f2; border:1px solid #fecdd3; padding:12px; border-radius:6px; margin-bottom:16px; display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#be123c; font-weight:bold;">⏳ 任务正在${b.status === 'queued' ? '排队' : (isFake ? 'Fake 模拟执行中' : '手机执行中')}，Worker 正在处理中...</span>
                <button class="modal-btn" style="padding:6px 14px; background:#e11d48; color:#fff; border:none; border-radius:4px; cursor:pointer; font-weight:bold;" onclick="window.PhoneAppUI.cancelBatch('${b.batch_id}')">${isFake ? "🛑 取消 Fake 测试" : "🛑 取消手机执行"}</button>
            </div>
            ` : ''}

            <!-- 待审核 DSL 控制台 -->
            ${b.status === 'pending_review' ? `
            <div style="background:#fffbe6; border:1px solid #ffe58f; padding:14px; border-radius:6px; margin-bottom:16px;">
                <h4 style="margin:0 0 8px 0; color:#b45309;">🛡️ AI 生成受限 DSL 脚本待审核 (规范化哈希与白名单校验)</h4>
                <div style="font-size:12px; color:#78350f; margin-bottom:8px;">可直接在线微调步骤，保存后将重新校验防注入并使旧审批作废：</div>
                <textarea id="pa-scripts-editor" rows="7" style="width:100%; box-sizing:border-box; font-family:monospace; font-size:12px; padding:8px; border:1px solid #cbd5e1; border-radius:4px;">${h(JSON.stringify(b.scripts, null, 2))}</textarea>
                <div style="margin-top:10px; display:flex; justify-content:flex-end; gap:8px;">
                    <button class="modal-btn" style="font-size:12px; padding:5px 12px; background:#dc2626; color:#fff; border:none; border-radius:3px; cursor:pointer;" onclick="window.PhoneAppUI.rejectBatch('${b.batch_id}')">❌ 驳回任务</button>
                    <button class="modal-btn modal-btn-secondary" style="font-size:12px; padding:5px 12px;" onclick="window.PhoneAppUI.saveEditedDraft('${b.batch_id}')">💾 保存修改并重新校验</button>
                    <button class="modal-btn modal-btn-primary" style="font-size:12px; padding:5px 14px; background:#16a34a; border-color:#16a34a;" onclick="window.PhoneAppUI.approveAndRun('${b.batch_id}')">${isFake ? "✅ 批准并执行 Fake 测试" : "✅ 批准并派发手机执行"}</button>
                </div>
            </div>
            ` : ''}

            ${b.status === 'approved' ? `
            <div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:12px; border-radius:6px; margin-bottom:16px; display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#15803d; font-weight:bold;">已获得有效授权哈希，等待派发执行。</span>
                <button class="modal-btn modal-btn-primary" style="padding:6px 14px;" onclick="window.PhoneAppUI.executeBatch('${b.batch_id}')">${isFake ? "▶️ 立即执行 Fake 测试" : "▶️ 立即派发手机执行"}</button>
            </div>
            ` : ''}

            <!-- 三态结果展示 -->
            <div style="margin-bottom:14px;">
                <h4 style="margin:12px 0 8px 0; color:#16a34a;">1. 符合条件的商品 (${matched.length})</h4>
                <div style="display:flex; flex-direction:column; gap:8px;">
                    ${matched.length === 0 ? '<div style="color:#94a3b8; font-size:13px;">暂无完全符合的商品</div>' :
                      matched.map(item => this.renderItemCard(item, 'matched')).join('')}
                </div>
            </div>

            <div style="margin-bottom:14px;">
                <h4 style="margin:12px 0 8px 0; color:#ca8a04;">2. 待核实的商品 (${needsVer.length})</h4>
                <div style="display:flex; flex-direction:column; gap:8px;">
                    ${needsVer.length === 0 ? '<div style="color:#94a3b8; font-size:13px;">无待核实项</div>' :
                      needsVer.map(item => this.renderItemCard(item, 'needs_verification')).join('')}
                </div>
            </div>

            <div style="margin-bottom:14px;">
                <h4 style="margin:12px 0 8px 0; color:#dc2626;">3. 不符合条件的商品 (${rejected.length})</h4>
                <div style="display:flex; flex-direction:column; gap:8px;">
                    ${rejected.length === 0 ? '<div style="color:#94a3b8; font-size:13px;">无不符合项</div>' :
                      rejected.map(item => this.renderItemCard(item, 'rejected')).join('')}
                </div>
            </div>
        </div>
        `;

        if (typeof showModal === 'function') {
            showModal({
                title: `📱 批次详情 · ${h(b.query)}`,
                icon: '📱',
                width: '780px',
                content: modalContent,
                buttons: [
                    {
                        text: '关闭',
                        class: 'modal-btn-secondary',
                        onClick: ({ close }) => {
                            if (this._pollTimer) clearInterval(this._pollTimer);
                            close();
                        }
                    }
                ]
            });
        }
    },

    renderItemCard(item, groupType) {
        const p = item.raw_price_text || ('¥' + item.amount);
        const evals = item.evaluations || {};
        const borderColors = { matched: '#16a34a', needs_verification: '#ca8a04', rejected: '#dc2626' };

        const badges = (item.evidence?.verified_badges || []).map(b => {
            if (b === 'self_operated') return '<span style="background:#fee2e2; color:#dc2626; padding:2px 6px; border-radius:3px; font-size:11px; font-weight:bold;">自营</span>';
            if (b === 'official_flagship') return '<span style="background:#dbeafe; color:#2563eb; padding:2px 6px; border-radius:3px; font-size:11px; font-weight:bold;">官方旗舰店</span>';
            return `<span style="background:#f1f5f9; color:#475569; padding:2px 6px; border-radius:3px; font-size:11px;">${h(b)}</span>`;
        }).join(' ');

        let reasons = [];
        for (const [k, v] of Object.entries(evals)) {
            const statusEmoji = v.status === 'pass' ? '✅' : (v.status === 'fail' ? '❌' : '🔍');
            reasons.push(`<span>${statusEmoji} <strong>[${k}]</strong>: ${h(v.reason || '')}</span>`);
        }

        return `
        <div style="border-left:4px solid ${borderColors[groupType]}; background:#fff; border:1px solid #e2e8f0; padding:10px 14px; border-radius:4px; font-size:13px;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <div>
                    ${badges}
                    <strong style="font-size:14px; color:#0f172a; margin-left:4px;">${h(item.title)}</strong>
                </div>
                <span style="color:#e11d48; font-weight:bold; font-size:15px; margin-left:12px;">${p}</span>
            </div>
            <div style="margin-top:6px; color:#64748b; display:flex; gap:16px; font-size:12px; flex-wrap:wrap;">
                <span>🏪 店铺: <strong>${h(item.shop_name || '未注明店铺')}</strong></span>
                <span>📈 销量: <strong>${h(item.sales_info?.raw_text || '未展示')}</strong></span>
                <span>⭐ 好评率: <strong>${item.positive_rate != null ? item.positive_rate + '%' : '未展示'}</strong></span>
                <span>🚚 物流: <strong>${item.logistics_info?.free_shipping === true ? '包邮' : (item.logistics_info?.free_shipping === false ? '不包邮/有运费' : '未明示')}</strong></span>
            </div>
            ${reasons.length > 0 ? `
            <div style="margin-top:8px; font-size:11px; background:#f8fafc; border:1px solid #e2e8f0; padding:6px 10px; border-radius:4px; display:flex; flex-direction:column; gap:3px;">
                ${reasons.join('')}
            </div>` : ''}
        </div>
        `;
    },

    async saveEditedDraft(batchId) {
        const text = document.getElementById('pa-scripts-editor')?.value;
        if (!text) return;
        try {
            const parsed = JSON.parse(text);
            const r = await fetch(`/agent/api/v1/phone-app/batches/${batchId}/draft`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scripts: parsed })
            });
            const d = await r.json();
            if (!r.ok || !d.success) {
                alert('草稿更新失败 (DSL 校验未通过): ' + (d.error || r.statusText));
                return;
            }
            alert('✅ 草稿已更新并重新通过安全校验！');
            this.openBatchDetail(batchId);
        } catch (e) {
            alert('脚本 JSON 格式错误: ' + e);
        }
    },

    async executeBatch(batchId) {
        try {
            const rExec = await fetch(`/agent/api/v1/phone-app/batches/${batchId}/execute`, { method: 'POST' });
            const dExec = await rExec.json();
            if (!rExec.ok || !dExec.success) {
                alert('派发执行失败 (HTTP ' + rExec.status + '): ' + (dExec.error || rExec.statusText));
                return;
            }

            alert(window.PHONE_APP_TEST_MODE === true ? 'FAKE 测试已排队，不操作手机。' : '✅ 任务已派发手机执行！');
            this.openBatchDetail(batchId);
            this.loadBatches();
        } catch (e) {
            alert('请求异常: ' + e);
        }
    },

    async approveAndRun(batchId) {
        try {
            const rAppr = await fetch(`/agent/api/v1/phone-app/batches/${batchId}/approve`, { method: 'POST' });
            const dAppr = await rAppr.json();
            if (!rAppr.ok) {
                const errMsg = (dAppr && dAppr.error) || rAppr.statusText;
                if (!errMsg.includes('approved') && !errMsg.includes('已处于审批通过状态')) {
                    alert('审核批准失败 (HTTP ' + rAppr.status + '): ' + errMsg);
                    return;
                }
            }

            await this.executeBatch(batchId);
        } catch (e) {
            alert('请求异常: ' + e);
        }
    },

    async cancelBatch(batchId) {
        if (!confirm('确定要取消此真机任务吗？若 Worker 正在执行将下发中断信号。')) return;
        try {
            const r = await fetch(`/agent/api/v1/phone-app/batches/${batchId}/cancel`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason: '用户在 Web 控制台手动取消' })
            });
            const d = await r.json();
            if (!r.ok || !d.success) {
                alert('取消失败: ' + (d.error || r.statusText));
                return;
            }
            alert(d.status === 'cancel_requested' ? '已请求停止，等待执行端确认。' : '🛑 任务已取消');
            this.openBatchDetail(batchId);
            if (typeof performSearch === 'function') performSearch(false);
        } catch (e) {
            alert('取消请求异常: ' + e);
        }
    },

    rejectBatch(batchId) {
        showModal({
            title: '驳回脚本',
            content: '请输入驳回原因；驳回后不会执行该任务。',
            input: { value: '脚本不符合执行预期', placeholder: '驳回原因' },
            buttons: [
                { text: '返回', class: 'modal-btn-secondary', onClick: () => this.openBatchDetail(batchId) },
                { text: '确认驳回', class: 'modal-btn-primary', onClick: (_modal, reason) => this.submitRejection(batchId, reason) }
            ]
        });
    },

    async submitRejection(batchId, reason) {
        try {
            const r = await fetch(`/agent/api/v1/phone-app/batches/${batchId}/reject`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason: reason })
            });
            const d = await r.json();
            if (!r.ok || !d.success) {
                alert('驳回失败: ' + (d.error || r.statusText));
                return;
            }
            alert('❌ 任务已驳回');
            this.openBatchDetail(batchId);
            if (typeof performSearch === 'function') performSearch(false);
        } catch (e) {
            alert('驳回请求异常: ' + e);
        }
    },

    startEventPolling(batchId) {
        if (this._pollTimer) clearInterval(this._pollTimer);
        this._pollTimer = setInterval(async () => {
            if (this._activeBatchId !== batchId) {
                clearInterval(this._pollTimer);
                return;
            }
            try {
                const r = await fetch(`/agent/api/v1/phone-app/batches/${batchId}`);
                if (r.ok) {
                    const d = await r.json();
                    if (d.batch?.status in { succeeded: 1, failed: 1, partial_failed: 1, cancelled: 1 }) {
                        clearInterval(this._pollTimer);
                    }
                    this.renderDetailModal(d);
                }
            } catch (e) {}
        }, 3000);
    }
};

// 页面加载时自动预拉取目录
window.PhoneAppUI.init();
