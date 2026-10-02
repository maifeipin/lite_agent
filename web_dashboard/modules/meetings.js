// ============================================================
//  Module: meetings (会审会议) — 列表 + 多轮发言时间线
//  创建入口在待办卡片「📣 会议」；此处负责查看记录
// ============================================================
registerTabModule({
    id: 'meetings',
    label: '会议',
    icon: '📣',
    badgeId: 'badge-meetings',

    _clickHandler: null,
    _liveAbort: null,

    _stateMeta: {
        open:               { icon: '🟢', text: '进行中' },
        waiting:            { icon: '🟡', text: '等待发言' },
        awaiting_approval:  { icon: '🟠', text: '待人工裁决' },
        approved:           { icon: '✅', text: '已批准' },
        rejected:           { icon: '❌', text: '已否决' },
        changes_requested:  { icon: '🔁', text: '要求修改' },
    },

    _fmtTime(ts) {
        if (!ts) return '';
        return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
    },

    async _fetchList() {
        const r = await ReviewMeetingAPI.fetch('/agent/api/v1/review-meetings');
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        return d.meetings || [];
    },

    async fetchCount() {
        try {
            const list = await this._fetchList();
            return list.filter(m => !m.archived_at).length;
        } catch { return 0; }
    },

    async search(query, offset, limit) {
        let list = await this._fetchList();
        if (query) {
            const q = query.toLowerCase();
            list = list.filter(m =>
                (m.title || '').toLowerCase().includes(q) || (m.id || '').includes(q));
        }
        // 未归档在前，各按更新时间倒序
        list.sort((a, b) =>
            ((a.archived_at ? 1 : 0) - (b.archived_at ? 1 : 0)) ||
            ((b.updated_at || 0) - (a.updated_at || 0)));
        const hits = list.slice(offset, offset + limit)
            .map(m => { m._module = 'meetings'; return m; });
        return { hits, total: list.length };
    },

    renderCard(doc) {
        const archived = !!doc.archived_at;
        const meta = this._stateMeta[doc.state] || { icon: '⚪', text: doc.state || '未知' };
        const stateText = archived ? '📦 已归档' : `${meta.icon} ${meta.text}`;

        let html = `<div class="card meeting-card" data-id="${h(doc.id)}">`;
        html += `<div class="card-meta">`;
        html += `<span class="tag status-tag">${stateText}</span>`;
        html += `<span class="tag">R${doc.round || 1}</span>`;
        html += `<span class="date">${this._fmtTime(doc.updated_at)}</span>`;
        html += `</div>`;
        html += `<h3 class="card-title">${h(doc.title || '(无议题)')}</h3>`;
        html += `<div class="card-snippet">#${h(doc.id)} · 创建于 ${this._fmtTime(doc.created_at)}</div>`;
        html += `<div class="todo-actions">`;
        html += `<button class="todo-btn meeting-btn-view" data-id="${h(doc.id)}" title="查看会议记录">📜 记录</button>`;
        html += `</div>`;
        html += `</div>`;
        return html;
    },

    renderBadge(el, count) {
        el.textContent = count;
        el.style.display = '';
    },

    _eventHtml(ev) {
        const KIND = {
            created: '🎬 创建', invited: '✉️ 邀请', joined: '🙋 加入', left: '👋 离开',
            comment: '💬 评论', review: '🗳 发言', model_vote: '🤖 模型投票',
            prior_model_vote: '🤖 历史投票', attendance_waived: '⏭ 免发言',
            round_closed: '🔒 轮次关闭', approval_requested: '📮 提请审批',
            decision: '⚖️ 人工裁决', revision_started: '🔁 修订开始', archived: '📦 归档',
        };
        const icon = KIND[ev.kind] || `🔹 ${ev.kind}`;
        const time = this._fmtTime(ev.created_at);
        const b = ev.body || {};
        let detail = '';
        if (ev.kind === 'review') {
            detail = `<strong>${h(b.position || '')}</strong>：${h(b.text || '')}` +
                (b.responds_to_seq ? ` <em>(回应 #${b.responds_to_seq})</em>` : '');
        } else if (ev.kind === 'comment') {
            detail = h(b.text || '') + (b.responds_to_seq ? ` <em>(回应 #${b.responds_to_seq})</em>` : '');
        } else if (ev.kind === 'decision') {
            detail = `<strong>${h(b.decision || '')}</strong>` + (b.note ? ` — ${h(b.note)}` : '');
        } else if (ev.kind === 'approval_requested') {
            detail = h(b.summary || '');
        } else if (ev.kind === 'created') {
            detail = (b.participants || []).length ? `参会：${h(b.participants.join('、'))}` : '';
        } else if (ev.kind === 'invited' || ev.kind === 'attendance_waived') {
            detail = h((b.names || [b.name]).filter(Boolean).join('、')) + (b.reason ? `（${h(b.reason)}）` : '');
        }
        const humanCls = ev.actor === 'human' ? ' meeting-event-human' : '';
        return `<div class="meeting-event${humanCls}">` +
            `<div class="meeting-event-head">${icon} <strong>R${ev.round} · ${h(ev.actor)}</strong>` +
            `<span class="meeting-event-time">${time}</span></div>` +
            (detail ? `<div class="meeting-event-body">${detail}</div>` : '') +
            `</div>`;
    },

    _headHtml(snap) {
        const meta = this._stateMeta[snap.state] || { icon: '⚪', text: snap.state };
        const participants = (snap.participants || [])
            .map(p => (p.online ? '🟢 ' : '⚪ ') + p.name +
                (p.metadata ? ' [' + [p.metadata.client, p.metadata.model, p.metadata.session_label]
                    .filter(Boolean).join(' · ') + '，自报]' : '')).join('、') || '（暂无）';
        const missing = snap.missing || [];
        let html = `<span class="tag status-tag">${meta.icon} ${meta.text}</span>` +
            `<span class="tag">R${snap.round}</span>` +
            `<span class="tag">参会：${h(participants)}</span>`;
        if (missing.length) html += `<span class="tag">未发言：${h(missing.join('、'))}</span>`;
        return html;
    },

    _stopLive() {
        if (this._liveAbort) { this._liveAbort.abort(); this._liveAbort = null; }
    },

    async _showDetail(meetingId) {
        this._stopLive();
        const ctrl = new AbortController();
        this._liveAbort = ctrl;
        let snap;
        try {
            const r = await ReviewMeetingAPI.fetch(`/agent/api/v1/review-meetings/${meetingId}`, { signal: ctrl.signal });
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
            if (ctrl.signal.aborted) return;
            snap = d;
        } catch (err) {
            if (ctrl.signal.aborted) return;
            showModal({ title: '读取会议失败', icon: '⚠️', content: `<p>${h(err.message)}</p>` });
            return;
        }

        const events = snap.events || [];
        let lastSeq = events.reduce((m, e) => Math.max(m, e.seq || 0), 0);
        const bodyHtml =
            `<div class="meeting-live">` +
            `<div class="meeting-live-head" id="meeting-live-head">${this._headHtml(snap)}</div>` +
            `<div class="meeting-live-hint" id="meeting-live-hint">${h(snap.hint || '')}</div>` +
            (snap.brief ? `<blockquote class="meeting-live-brief">${h(snap.brief).replace(/\n/g, '<br>')}</blockquote>` : '') +
            `<div class="meeting-live-feed" id="meeting-live-feed">` +
            (events.map(ev => this._eventHtml(ev)).join('') || '<p class="meeting-live-empty">（暂无会议事件）</p>') +
            `</div>` +
            `<div class="meeting-speak" id="meeting-speak" style="${['open', 'awaiting_approval'].includes(snap.state) && !snap.archived_at ? '' : 'display:none'}">` +
            `<input type="text" id="meeting-speak-input" maxlength="2000" ` +
            `placeholder="以主持人身份插话（写入审计链，actor=human）… Enter 发送">` +
            `</div></div>`;
        showModal({ title: `会议记录 · ${h(snap.title)}`, icon: '📣', content: bodyHtml, width: '720px' });

        const overlay = document.querySelector('.universal-modal-overlay');
        const feed = overlay.querySelector('#meeting-live-feed');
        const head = overlay.querySelector('#meeting-live-head');
        const hintEl = overlay.querySelector('#meeting-live-hint');
        const speakBox = overlay.querySelector('#meeting-speak');
        const input = overlay.querySelector('#meeting-speak-input');
        feed.scrollTop = feed.scrollHeight;

        const pollUrl = () => `/agent/api/v1/review-meetings/${meetingId}?since=${lastSeq}&wait=20`;
        const applySnapshot = (d) => {
            const fresh = d.events || [];
            if (fresh.length) {
                lastSeq = fresh.reduce((m, e) => Math.max(m, e.seq || 0), lastSeq);
                const empty = feed.querySelector('.meeting-live-empty');
                if (empty) empty.remove();
                fresh.forEach(ev => feed.insertAdjacentHTML('beforeend', this._eventHtml(ev)));
                feed.scrollTop = feed.scrollHeight;
            }
            head.innerHTML = this._headHtml(d);
            hintEl.textContent = d.hint || '';
            speakBox.style.display = ['open', 'awaiting_approval'].includes(d.state) && !d.archived_at ? '' : 'none';
        };
        const isFinal = (d) => d.archived_at || d.state === 'approved' || d.state === 'rejected';

        const observer = new MutationObserver(() => {
            if (!document.body.contains(overlay)) ctrl.abort();
        });
        observer.observe(document.body, { childList: true });
        const loop = async () => {
            if (isFinal(snap)) return;
            const backoff = () => new Promise(res => setTimeout(res, 4000));
            while (!ctrl.signal.aborted && document.body.contains(feed)) {
                let d;
                try {
                    const r = await ReviewMeetingAPI.fetch(pollUrl(), { signal: ctrl.signal });
                    d = await r.json().catch(() => ({ error: 'HTTP ' + r.status }));
                    if (!r.ok) {
                        if ([400, 401, 403, 404, 421].includes(r.status)) {
                            hintEl.textContent = `直播已停止：${d.error || 'HTTP ' + r.status}`;
                            return;
                        }
                        await backoff();
                        continue;
                    }
                } catch (e) {
                    if (ctrl.signal.aborted) return;
                    await backoff();
                    continue;
                }
                applySnapshot(d);
                if (isFinal(d)) return;  // 终态停止轮询
            }
        };
        loop().finally(() => observer.disconnect());

        input.addEventListener('keydown', async (e) => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            const text = input.value.trim();
            if (!text) return;
            input.value = '';
            try {
                const r = await ReviewMeetingAPI.fetch(`/agent/api/v1/review-meetings/${meetingId}/comments`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text }),
                });
                const d = await r.json();
                if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
            } catch (err) {
                input.value = text;
                alert('插话失败: ' + err.message);
            }
        });
    },

    onMount(container) {
        const grid = container.querySelector('#results-grid');
        if (grid && !this._clickHandler) {
            this._clickHandler = (e) => {
                const viewBtn = e.target.closest('.meeting-btn-view');
                if (viewBtn) this._showDetail(viewBtn.getAttribute('data-id'));
            };
            grid.addEventListener('click', this._clickHandler);
        }
    },

    onUnmount(container) {
        this._stopLive();
        const grid = container.querySelector('#results-grid');
        if (grid && this._clickHandler) {
            grid.removeEventListener('click', this._clickHandler);
            this._clickHandler = null;
        }
    },
});
