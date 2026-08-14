// ============================================================
//  Module: socks5 (HTTP API) — Socks5 Proxy Management
// ============================================================
registerTabModule({
    id: 'socks5',
    label: 'Socks5 代理',
    icon: '🧦',
    badgeId: 'badge-socks5',

    async fetchCount() {
        try {
            const r = await fetch('/agent/api/v1/socks5');
            const d = await r.json();
            return (d.data || []).length;
        } catch {
            try {
                const r2 = await fetch('/api/v1/socks5');
                const d2 = await r2.json();
                return (d2.data || []).length;
            } catch { return 0; }
        }
    },

    async _fetchAll() {
        let items = [];
        try {
            const r = await fetch('/agent/api/v1/socks5');
            const d = await r.json();
            items = d.data || [];
        } catch {
            try {
                const r2 = await fetch('/api/v1/socks5');
                const d2 = await r2.json();
                items = d2.data || [];
            } catch { items = []; }
        }
        return items.map(p => { p._module = 'socks5'; return p; });
    },

    async search(query, offset, limit) {
        const all = await this._fetchAll();
        let filtered = all;
        if (query) {
            const q = query.toLowerCase();
            filtered = all.filter(p =>
                (p.servername || '').toLowerCase().includes(q) ||
                (p.host || '').toLowerCase().includes(q) ||
                (p.runcmd || '').toLowerCase().includes(q) ||
                (p.clientproxy || '').toLowerCase().includes(q) ||
                (p.memo || '').toLowerCase().includes(q)
            );
        }
        return { hits: filtered.slice(offset, offset + limit), total: filtered.length };
    },

    renderCard(doc) {
        const id = doc.id || doc.ID;
        const servername = doc.servername || '(无节点名称)';
        const host = doc.host || '';
        const runcmd = doc.runcmd || '';
        const clientproxy = doc.clientproxy || '';
        const memo = doc.memo || '';
        const isActive = doc.is_active === 1;
        const isNaive = runcmd.toLowerCase().includes('naive') || runcmd.toLowerCase().includes('--proxy=');

        let html = `<div class="card socks5-card ${isActive ? 'socks5-card-active' : ''}" data-id="${h(id)}">`;
        html += `<div class="card-meta">`;
        html += `<span class="tag tag-socks5">🧦 Socks5 节点</span>`;
        if (isActive) {
            html += `<span class="tag tag-active-node">🟢 当前 VPS1 在用</span>`;
        }
        if (host) html += `<span class="tag tag-host">🌐 ${h(host)}</span>`;
        if (memo) html += `<span class="tag tag-memo">📝 ${h(memo)}</span>`;
        html += `<span class="socks5-ping-badge" id="socks5-ping-${h(id)}" style="display:none;"></span>`;
        html += `</div>`;

        html += `<h3 class="card-title">${h(servername)}</h3>`;

        if (clientproxy) {
            html += `<div class="socks5-field"><span class="field-label">客户端代理配置:</span> <code class="code-box">${h(clientproxy)}</code></div>`;
        }
        if (runcmd) {
            html += `<div class="socks5-field"><span class="field-label">启动命令:</span> <code class="code-box code-cmd">${h(runcmd)}</code></div>`;
        }

        // Action Toolbar
        html += `<div class="socks5-actions">`;
        if (isNaive) {
            if (isActive) {
                html += `<button class="socks5-btn socks5-btn-active-disabled" disabled title="该节点已经是 VPS1 当前生效的主节点">🟢 VPS1 当前主节点</button>`;
            } else {
                html += `<button class="socks5-btn socks5-btn-set-active" data-action="set-active" data-id="${h(id)}" title="切换并重启 VPS1 的 naive.service 代理服务">🚀 设为 VPS1 主节点</button>`;
            }
        } else {
            html += `<button class="socks5-btn socks5-btn-disabled" disabled title="Brook 协议节点仅供客户端脚本导出使用，不可直接应用为 VPS1 的 Naive 服务主节点">🚫 Brook 仅导脚本</button>`;
        }
        html += `<button class="socks5-btn socks5-btn-outbound" data-action="test-outbound" data-id="${h(id)}" title="对该节点发起真实的 HTTP 出站网络与数据包出口 IP 回显测试">🌐 出站网络测试</button>`;
        html += `<button class="socks5-btn socks5-btn-test" data-action="test" data-id="${h(id)}" data-host="${h(host)}" title="测试服务器端口 TCP 连通性">⚡ TCP 连通性</button>`;
        html += `<button class="socks5-btn socks5-btn-ps1" data-action="copy-ps1" data-id="${h(id)}" title="复制 Windows PowerShell 检查安装与启动脚本">💻 复制 PS1</button>`;
        html += `<button class="socks5-btn socks5-btn-sh" data-action="copy-sh" data-id="${h(id)}" title="复制 Linux/macOS Shell 检查安装与启动脚本">🐧 复制 SH</button>`;
        html += `<button class="socks5-btn socks5-btn-copy" data-action="copy-cmd" data-cmd="${h(runcmd)}" title="复制原始启动命令">📋 复制命令</button>`;
        html += `<button class="socks5-btn socks5-btn-edit" data-action="toggle-edit" data-id="${h(id)}" title="编辑节点">✏️ 编辑</button>`;
        html += `<button class="socks5-btn socks5-btn-del" data-action="delete" data-id="${h(id)}" title="删除节点">🗑️ 删除</button>`;
        html += `</div>`;

        // Inline Edit Panel
        html += `<div class="socks5-edit-panel" id="socks5-edit-${h(id)}" style="display:none;">`;
        html += `<div class="edit-row">`;
        html += `<input type="text" class="edit-servername" value="${h(servername)}" placeholder="节点名称 (servername)" />`;
        html += `<input type="text" class="edit-host" value="${h(host)}" placeholder="服务器 IP/域名 (host)" />`;
        html += `</div>`;
        html += `<div class="edit-row">`;
        html += `<input type="text" class="edit-clientproxy" value="${h(clientproxy)}" placeholder="客户端代理参数 (clientproxy)" />`;
        html += `<input type="text" class="edit-memo" value="${h(memo)}" placeholder="备注 (memo)" />`;
        html += `</div>`;
        html += `<div class="edit-row">`;
        html += `<textarea class="edit-runcmd" placeholder="启动命令 (runcmd)" rows="2">${h(runcmd)}</textarea>`;
        html += `</div>`;
        html += `<div class="edit-row actions-row">`;
        html += `<button class="socks5-btn socks5-btn-save" data-action="save" data-id="${h(id)}">💾 保存修改</button>`;
        html += `<button class="socks5-btn socks5-btn-cancel" data-action="cancel" data-id="${h(id)}">取消</button>`;
        html += `</div>`;
        html += `</div>`;

        html += `</div>`;
        return html;
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },

    onMount(container) {
        const header = container.querySelector('.results-header');
        if (header && !header.querySelector('.socks5-create-bar')) {
            const form = document.createElement('div');
            form.className = 'socks5-create-bar';
            form.innerHTML = `
                <div class="create-primary-row">
                    <input type="text" id="socks5-new-servername" placeholder="+ 节点名称 (如: HK VPS 01)..." autocomplete="off" />
                    <input type="text" id="socks5-new-host" placeholder="服务器 IP / Host..." autocomplete="off" />
                    <input type="text" id="socks5-new-memo" placeholder="备注 (可选)..." autocomplete="off" />
                    <button type="button" id="socks5-toggle-more" title="展开更多参数 (启动命令/客户端代理)">⚙ 详细配置</button>
                    <button type="button" id="socks5-new-submit">+ 添加 Socks5 节点</button>
                </div>
                <div class="create-more-row" id="socks5-more-fields" style="display:none;">
                    <input type="text" id="socks5-new-clientproxy" placeholder="客户端代理参数 (如 --proxy-server=socks5://127.0.0.1:18988)" />
                    <textarea id="socks5-new-runcmd" placeholder="完整启动命令 (如 naive --proxy=https://...)" rows="2"></textarea>
                </div>
            `;
            header.appendChild(form);

            const nameInput = form.querySelector('#socks5-new-servername');
            const hostInput = form.querySelector('#socks5-new-host');
            const memoInput = form.querySelector('#socks5-new-memo');
            const clientProxyInput = form.querySelector('#socks5-new-clientproxy');
            const runcmdInput = form.querySelector('#socks5-new-runcmd');
            const toggleMoreBtn = form.querySelector('#socks5-toggle-more');
            const moreFields = form.querySelector('#socks5-more-fields');
            const submitBtn = form.querySelector('#socks5-new-submit');

            toggleMoreBtn.addEventListener('click', () => {
                const isHidden = moreFields.style.display === 'none';
                moreFields.style.display = isHidden ? 'flex' : 'none';
                toggleMoreBtn.classList.toggle('active', isHidden);
            });

            const doCreate = async () => {
                const servername = nameInput.value.trim();
                const host = hostInput.value.trim();
                if (!servername || !host) {
                    alert('请输入节点名称和服务器 IP/Host！');
                    return;
                }
                submitBtn.disabled = true;
                try {
                    const payload = {
                        servername,
                        host,
                        memo: memoInput.value.trim(),
                        clientproxy: clientProxyInput.value.trim(),
                        runcmd: runcmdInput.value.trim()
                    };
                    const res = await fetch('/agent/api/v1/socks5', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    if (res.ok) {
                        nameInput.value = '';
                        hostInput.value = '';
                        memoInput.value = '';
                        clientProxyInput.value = '';
                        runcmdInput.value = '';
                        if (typeof performSearch === 'function') performSearch(false);
                        if (typeof fetchAllStats === 'function') fetchAllStats();
                    } else {
                        alert('添加 Socks5 节点失败！');
                    }
                } catch (e) {
                    alert('请求失败: ' + e.message);
                } finally {
                    submitBtn.disabled = false;
                }
            };

            submitBtn.addEventListener('click', doCreate);
        }

        this.initEvents(container);
    },

    onUnmount(container) {
        const bar = container.querySelector('.socks5-create-bar');
        if (bar) bar.remove();
    },

    initEvents(container) {
        const grid = container.querySelector('#results-grid');
        if (!grid || grid._socks5EventsBound) return;
        grid._socks5EventsBound = true;

        grid.addEventListener('click', async (e) => {
            const btn = e.target.closest('.socks5-btn');
            if (!btn) return;
            const action = btn.getAttribute('data-action');
            const card = btn.closest('.socks5-card');
            if (!card) return;
            const id = btn.getAttribute('data-id') || card.getAttribute('data-id');

            // 1. 设置为 VPS1 主节点
            if (action === 'set-active') {
                if (!confirm('确定将该节点设为 VPS1 的当前生效主节点并重新加载 naive.service 服务吗？')) return;
                btn.disabled = true;
                btn.textContent = '⏳ 正在切节点...';
                try {
                    const r = await fetch('/agent/api/v1/socks5/active', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: Number(id) })
                    });
                    const d = await r.json();
                    if (d.success) {
                        alert(d.message || '切换节点成功！');
                        if (typeof performSearch === 'function') performSearch(false);
                    } else {
                        alert('切换失败: ' + (d.message || '未知错误'));
                    }
                } catch (err) {
                    alert('请求失败: ' + err.message);
                } finally {
                    btn.disabled = false;
                    btn.textContent = '🚀 设为 VPS1 主节点';
                }
                return;
            }

            // 2. 出站网络测试 (HTTP Over Socks5 18988)
            if (action === 'test-outbound') {
                const badge = card.querySelector(`#socks5-ping-${id}`);
                if (badge) {
                    badge.style.display = 'inline-block';
                    badge.className = 'socks5-ping-badge testing';
                    badge.textContent = '⏳ 出站网络测试中...';
                }
                try {
                    const r = await fetch(`/agent/api/v1/socks5/health?id=${encodeURIComponent(id)}`);
                    const d = await r.json();
                    if (d.success && d.result && d.result.success) {
                        if (badge) {
                            badge.className = 'socks5-ping-badge online';
                            const ipStr = d.result.ip ? ` · 出站 IP: ${d.result.ip}` : '';
                            const locStr = d.result.loc ? ` (${d.result.loc})` : '';
                            badge.textContent = `🌐 HTTP ${d.result.http_code}${ipStr}${locStr} (${d.result.latency_ms} ms)`;
                        }
                    } else {
                        if (badge) {
                            badge.className = 'socks5-ping-badge offline';
                            badge.textContent = `❌ ${d.result ? d.result.error : '出站异常'}`;
                        }
                    }
                } catch (err) {
                    if (badge) {
                        badge.className = 'socks5-ping-badge offline';
                        badge.textContent = '❌ 超时/异常';
                    }
                }
                return;
            }

            // 3. 服务器 TCP 端口连通性测试
            if (action === 'test') {
                const host = btn.getAttribute('data-host');
                const badge = card.querySelector(`#socks5-ping-${id}`);
                if (badge) {
                    badge.style.display = 'inline-block';
                    badge.className = 'socks5-ping-badge testing';
                    badge.textContent = '⏳ 测试中...';
                }
                try {
                    const r = await fetch(`/agent/api/v1/socks5/test?id=${id}&host=${encodeURIComponent(host)}`);
                    const res = await r.json();
                    if (res.success && res.result && res.result.success) {
                        if (badge) {
                            badge.className = 'socks5-ping-badge online';
                            badge.textContent = `⚡ ${res.result.latency_ms} ms`;
                        }
                    } else {
                        if (badge) {
                            badge.className = 'socks5-ping-badge offline';
                            badge.textContent = `❌ ${res.result ? res.result.error : '连接失败'}`;
                        }
                    }
                } catch (err) {
                    if (badge) {
                        badge.className = 'socks5-ping-badge offline';
                        badge.textContent = '❌ 超时';
                    }
                }
                return;
            }

            // 4. 复制 PS1 部署脚本
            if (action === 'copy-ps1') {
                try {
                    const r = await fetch(`/agent/api/v1/socks5/script?id=${id}&type=ps1`);
                    const d = await r.json();
                    if (d.success && d.script) {
                        await navigator.clipboard.writeText(d.script);
                        const origText = btn.textContent;
                        btn.textContent = '✔ 已复制 PS1 脚本!';
                        setTimeout(() => btn.textContent = origText, 2000);
                    }
                } catch (err) {
                    alert('获取 PS1 脚本失败');
                }
                return;
            }

            // 5. 复制 SH 部署脚本
            if (action === 'copy-sh') {
                try {
                    const r = await fetch(`/agent/api/v1/socks5/script?id=${id}&type=sh`);
                    const d = await r.json();
                    if (d.success && d.script) {
                        await navigator.clipboard.writeText(d.script);
                        const origText = btn.textContent;
                        btn.textContent = '✔ 已复制 SH 脚本!';
                        setTimeout(() => btn.textContent = origText, 2000);
                    }
                } catch (err) {
                    alert('获取 SH 脚本失败');
                }
                return;
            }

            // 6. 复制原始启动命令
            if (action === 'copy-cmd') {
                const cmd = btn.getAttribute('data-cmd') || '';
                if (cmd) {
                    await navigator.clipboard.writeText(cmd);
                    const origText = btn.textContent;
                    btn.textContent = '✔ 已复制!';
                    setTimeout(() => btn.textContent = origText, 2000);
                }
                return;
            }

            // 7. 展开 / 隐藏编辑面板
            if (action === 'toggle-edit') {
                const panel = card.querySelector(`#socks5-edit-${id}`);
                if (panel) {
                    const isHidden = panel.style.display === 'none';
                    panel.style.display = isHidden ? 'block' : 'none';
                }
                return;
            }

            // 8. 取消编辑
            if (action === 'cancel') {
                const panel = card.querySelector(`#socks5-edit-${id}`);
                if (panel) panel.style.display = 'none';
                return;
            }

            // 9. 保存修改
            if (action === 'save') {
                const panel = card.querySelector(`#socks5-edit-${id}`);
                if (!panel) return;
                const servername = panel.querySelector('.edit-servername').value.trim();
                const host = panel.querySelector('.edit-host').value.trim();
                const clientproxy = panel.querySelector('.edit-clientproxy').value.trim();
                const memo = panel.querySelector('.edit-memo').value.trim();
                const runcmd = panel.querySelector('.edit-runcmd').value.trim();

                try {
                    const res = await fetch(`/agent/api/v1/socks5/${id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ servername, host, clientproxy, memo, runcmd })
                    });
                    if (res.ok) {
                        if (typeof performSearch === 'function') performSearch(false);
                    } else {
                        alert('更新节点失败！');
                    }
                } catch (err) {
                    alert('请求失败: ' + err.message);
                }
                return;
            }

            // 10. 删除节点
            if (action === 'delete') {
                if (!confirm('确定要删除此 Socks5 代理节点吗？')) return;
                try {
                    const res = await fetch(`/agent/api/v1/socks5/${id}`, { method: 'DELETE' });
                    if (res.ok) {
                        if (typeof performSearch === 'function') performSearch(false);
                        if (typeof fetchAllStats === 'function') fetchAllStats();
                    } else {
                        alert('删除节点失败！');
                    }
                } catch (err) {
                    alert('请求失败: ' + err.message);
                }
                return;
            }
        });
    }
});
