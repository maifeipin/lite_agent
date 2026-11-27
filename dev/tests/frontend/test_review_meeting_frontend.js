// Run with: node tests/frontend/test_review_meeting_frontend.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web_dashboard/meeting_api.js', 'utf8');
const values = new Map();
const calls = [];
let status = 200;
const context = {
    window: { location: { href: '' } }, Headers, Date,
    sessionStorage: {
        getItem: k => values.get(k) ?? null,
        setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k),
    },
    fetch: async (url, options) => { calls.push({ url, options }); return { status }; },
};
vm.runInNewContext(source, context);
const api = context.window.ReviewMeetingAPI;
function login() {
    values.set('review_token', 'temporary-host-session');
    values.set('review_token_expires_at', String(Date.now() / 1000 + 3600));
    values.set('dashboard_auth', 'ok');
}
(async () => {
    login();
    await api.fetch('/agent/api/v1/review-meetings?since=0', { credentials: 'include' });
    assert.equal(calls[0].url, 'https://edge.maifeipin.com/agent/api/v1/review-meetings?since=0');
    assert.equal(calls[0].options.credentials, 'omit');
    assert.equal(calls[0].options.headers.get('Authorization'), 'Bearer temporary-host-session');
    await assert.rejects(api.fetch('/agent/api/v1/chat'));
    assert.equal(calls.length, 1, 'scoped token must never be sent to global API');
    values.set('review_token_expires_at', '1');
    await assert.rejects(api.fetch('/agent/api/v1/review-meetings'));
    assert.equal(calls.length, 1, 'expired token must not be sent');
    assert.equal(values.has('review_token'), false);
    assert.equal(context.window.location.href, '/login.html');
    login(); status = 401;
    await api.fetch('/agent/api/v1/review-meetings');
    assert.equal(values.has('review_token'), false, '401 clears stale credentials');
    login(); status = 200;
    await api.logout();
    const logout = calls.at(-1);
    assert.equal(logout.url, 'https://edge.maifeipin.com/agent/api/v1/review-meetings/logout');
    assert.equal(logout.options.method, 'POST');
    assert.equal(logout.options.body, '{}');
    assert.equal(values.has('review_token'), false);

    // 场景 6：主页登录后附带 user_session_token，即便中途切为手动 Token 直连，退出时仍必须撤销统一会话并清理 sessionStorage
    login();
    values.set('user_session_token', 'sess-tok-123');
    values.set('pc_cli_token_source', 'manual');
    status = 200;
    const beforeCount = calls.length;
    await api.logout();
    const logoutCalls = calls.slice(beforeCount);
    assert.equal(logoutCalls.length, 2);
    assert.equal(logoutCalls[0].url, '/agent/api/v1/auth/logout');
    assert.equal(logoutCalls[0].options.method, 'POST');
    assert.equal(logoutCalls[0].options.headers['Authorization'], 'Bearer sess-tok-123');
    assert.equal(JSON.parse(logoutCalls[0].options.body).session_token, 'sess-tok-123');
    assert.equal(logoutCalls[1].url, 'https://edge.maifeipin.com/agent/api/v1/review-meetings/logout');
    assert.equal(values.has('user_session_token'), false);
    assert.equal(values.has('pc_cli_token_source'), false);
    assert.equal(values.has('review_token'), false);

    // 场景 7：携带 user_refresh_token 退出时同步撤销并清理刷新凭证
    login();
    values.set('user_session_token', 'sess-tok-456');
    values.set('user_refresh_token', 'rt-tok-456');
    values.set('user_session_expires_at', '9999999999');
    const before7 = calls.length;
    await api.logout();
    const logout7 = calls.slice(before7);
    const body7 = JSON.parse(logout7[0].options.body);
    assert.equal(body7.session_token, 'sess-tok-456');
    assert.equal(body7.refresh_token, 'rt-tok-456');
    assert.equal(values.has('user_refresh_token'), false);
    assert.equal(values.has('user_session_expires_at'), false);

    // 场景 8~10：PC CLI store.js 在途刷新凭证切换保护 (P1) 与 WS 有界重连 (P2)
    const { pathToFileURL } = require('node:url');
    const makeStorage = () => {
        const m = new Map();
        return {
            getItem: k => (m.has(k) ? m.get(k) : null),
            setItem: (k, v) => m.set(k, String(v)),
            removeItem: k => m.delete(k),
            clear: () => m.clear(),
        };
    };
    globalThis.sessionStorage = makeStorage();
    globalThis.localStorage = makeStorage();
    globalThis.document = { addEventListener: () => {}, createElement: () => ({}), visibilityState: 'visible' };
    globalThis.window = { location: { protocol: 'https:', host: 'mail.maifeipin.com' } };

    const sockets = [];
    globalThis.WebSocket = class FakeWebSocket {
        constructor(url) {
            this.url = url;
            this.sent = [];
            this.closed = false;
            sockets.push(this);
        }
        send(raw) { this.sent.push(JSON.parse(raw)); }
        close() {
            if (this.closed) return;
            this.closed = true;
            if (this.onclose) this.onclose();
        }
    };

    sessionStorage.setItem('user_session_token', 'sess-old');
    sessionStorage.setItem('user_refresh_token', 'rt-old');
    sessionStorage.setItem('pc_cli_token_source', 'session');

    let pendingResolve = null;
    let refreshCount = 0;
    globalThis.fetch = async (url, opts) => {
        if (url === '/agent/api/v1/auth/refresh') {
            refreshCount += 1;
            if (pendingResolve === 'defer') {
                return await new Promise(r => { pendingResolve = r; });
            }
            return {
                ok: true, status: 200,
                json: async () => ({
                    success: true,
                    session_token: `sess-rot-${refreshCount}`,
                    refresh_token: `rt-rot-${refreshCount}`,
                    access_token_expires_at: Math.floor(Date.now() / 1000) + 3600,
                }),
            };
        }
        return { ok: true, status: 200, json: async () => ({ success: true, bots: [], nodes: [] }) };
    };

    const storeMod = await import(pathToFileURL(fs.realpathSync('web_dashboard/mobile/store.js')).href);

    // 8a: 在途刷新等待期间切换为手动 Token，旧刷新返回 200 不得覆盖手动 Token
    pendingResolve = 'defer';
    const p8a = storeMod.refreshWebSession();
    while (typeof pendingResolve !== 'function') await new Promise(r => setTimeout(r, 1));
    const resolve8a = pendingResolve;
    storeMod.saveToken('manual-override');
    resolve8a({
        ok: true, status: 200,
        json: async () => ({ success: true, session_token: 'stale-sess', refresh_token: 'stale-rt' }),
    });
    assert.equal(await p8a, false);
    assert.equal(storeMod.store.token, 'manual-override');
    assert.equal(sessionStorage.getItem('pc_cli_token_source'), 'manual');

    // 8b: 在途刷新等待期间重新登录新会话，旧刷新返回 401 不得清空新会话的 refresh_token
    storeMod.saveToken('sess-v1', { source: 'session', refreshToken: 'rt-v1' });
    pendingResolve = 'defer';
    const p8b = storeMod.refreshWebSession();
    while (typeof pendingResolve !== 'function') await new Promise(r => setTimeout(r, 1));
    const resolve8b = pendingResolve;
    storeMod.saveToken('sess-v2', { source: 'session', refreshToken: 'rt-v2' });
    resolve8b({ ok: false, status: 401, json: async () => ({ error: 'revoked' }) });
    assert.equal(await p8b, false);
    assert.equal(storeMod.store.token, 'sess-v2');
    assert.equal(sessionStorage.getItem('user_refresh_token'), 'rt-v2');

    // 9: WebSocket 持续拒绝新令牌时最多刷新 1 次即停止在 auth_required，绝不死循环
    pendingResolve = null;
    refreshCount = 0;
    sockets.length = 0;
    storeMod.saveToken('ws-tok-0', { source: 'session', refreshToken: 'ws-rt-0' });
    storeMod.connectWs();
    assert.equal(sockets.length, 1);
    sockets[0].onopen();
    assert.equal(sockets[0].sent[0].token, 'ws-tok-0');
    sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
    await new Promise(r => setTimeout(r, 10));
    assert.equal(refreshCount, 1, 'first auth.error triggers one refresh');
    assert.equal(sockets.length, 2, 'reconnects once with rotated token');
    sockets[1].onopen();
    assert.equal(sockets[1].sent[0].token, 'sess-rot-1');
    sockets[1].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
    await new Promise(r => setTimeout(r, 10));
    assert.equal(refreshCount, 1, 'second consecutive auth.error must NOT refresh again');
    assert.equal(storeMod.store.wsState, 'auth_required');
    assert.equal(sockets.length, 2);

    // 10: WS 发送旧令牌后若并发刷新已更新 store.token，收到 auth.error 时直接重连且不重复调用 /auth/refresh
    refreshCount = 0;
    sockets.length = 0;
    storeMod.saveToken('ws-stale-tok', { source: 'session', refreshToken: 'ws-rt-10' });
    storeMod.connectWs();
    sockets[0].onopen();
    assert.equal(sockets[0].sent[0].token, 'ws-stale-tok');
    // 模拟并发 REST 刷新已更新当前令牌（内部刷新不重置 _wsWebRefreshAttempts）
    await storeMod.refreshWebSession();
    assert.equal(refreshCount, 1);
    assert.equal(storeMod.store.token, 'sess-rot-1');
    sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
    await new Promise(r => setTimeout(r, 10));
    assert.equal(refreshCount, 1, 'must reuse already-rotated token without extra /auth/refresh');
    assert.equal(sockets.length, 2);
    sockets[1].onopen();
    assert.equal(sockets[1].sent[0].token, 'sess-rot-1');

    // 11a: REST 请求 (sendTurn POST) 在途期间切换凭证，旧请求返回 401 时严禁用新凭证重放原 POST
    let turnPostHeaders = [];
    let deferTurnResp = null;
    globalThis.fetch = async (url, opts) => {
        if (url.endsWith('/turns')) {
            turnPostHeaders.push((opts.headers || {}).Authorization);
            if (deferTurnResp === 'defer') {
                return await new Promise(r => { deferTurnResp = r; });
            }
            return { ok: true, status: 200, json: async () => ({ success: true, turn: { turn_id: 't1' } }) };
        }
        if (url === '/agent/api/v1/auth/refresh') {
            if (pendingResolve === 'defer') {
                return await new Promise(r => { pendingResolve = r; });
            }
            return {
                ok: true, status: 200,
                json: async () => ({ success: true, session_token: 'sess-after', refresh_token: 'rt-after' }),
            };
        }
        return { ok: true, status: 200, json: async () => ({ success: true }) };
    };
    storeMod.saveToken('cred-A', { source: 'session', refreshToken: 'rt-A' });
    storeMod.store.wsState = 'connected';
    deferTurnResp = 'defer';
    const turnPromiseA = storeMod.sendTurn('bot_1', 'sensitive action');
    while (typeof deferTurnResp !== 'function') await new Promise(r => setTimeout(r, 1));
    const finishTurnA = deferTurnResp;
    // 在途切换到凭证 B
    storeMod.saveToken('cred-B', { source: 'session', refreshToken: 'rt-B' });
    finishTurnA({ ok: false, status: 401, json: async () => ({ error: 'expired' }) });
    await assert.rejects(turnPromiseA, /登录凭证已变更/);
    assert.equal(turnPostHeaders.length, 1, 'old POST must NEVER be replayed under credential B');
    assert.equal(turnPostHeaders[0], 'Bearer cred-A');
    assert.equal(storeMod.store.wsState, 'connected', 'stale 401 must not pollute new credential wsState');

    // 11b: 临期主动刷新等待期间切换凭证，恢复后立即终止原 POST，不得使用新凭证发出
    turnPostHeaders = [];
    deferTurnResp = null;
    pendingResolve = 'defer';
    storeMod.saveToken('cred-C', {
        source: 'session',
        refreshToken: 'rt-C',
        expiresAt: Math.floor(Date.now() / 1000) + 10, // < 90s leeway -> triggers proactive refresh
    });
    const turnPromiseC = storeMod.sendTurn('bot_1', 'proactive window action');
    while (typeof pendingResolve !== 'function') await new Promise(r => setTimeout(r, 1));
    const finishRefreshC = pendingResolve;
    storeMod.saveToken('cred-D');
    finishRefreshC({
        ok: true, status: 200,
        json: async () => ({ success: true, session_token: 'sess-C2', refresh_token: 'rt-C2' }),
    });
    await assert.rejects(turnPromiseC, /登录凭证已变更/);
    assert.equal(turnPostHeaders.length, 0, 'aborted proactive refresh must not send POST with new credential');
    assert.equal(storeMod.store.token, 'cred-D');

    // 12: 凭证 A 发出 fetchBots() 后切到凭证 B 并先显示 B 的列表，随后 A 的 200 响应迟到，严禁覆盖 B 的列表
    let resolveBotsA = null;
    globalThis.fetch = async (url, opts) => {
        const auth = (opts.headers || {}).Authorization;
        if (url.endsWith('/bots') && auth === 'Bearer cred-list-A') {
            return await new Promise(r => { resolveBotsA = r; });
        }
        if (url.endsWith('/bots') && auth === 'Bearer cred-list-B') {
            return {
                ok: true, status: 200,
                json: async () => ({ success: true, bots: [{ bot_id: 'bot_B', name: 'Bot B', status: 'idle' }] }),
            };
        }
        return { ok: true, status: 200, json: async () => ({ success: true }) };
    };
    storeMod.saveToken('cred-list-A');
    const pendingBotsA = storeMod.fetchBots();
    while (typeof resolveBotsA !== 'function') await new Promise(r => setTimeout(r, 1));
    // 切换到凭证 B 并率先完成 B 的列表加载
    storeMod.saveToken('cred-list-B');
    await storeMod.fetchBots();
    assert.deepEqual(storeMod.store.bots.map(b => b.bot_id), ['bot_B']);
    // 随后凭证 A 的 200 响应迟到
    resolveBotsA({
        ok: true, status: 200,
        json: async () => ({ success: true, bots: [{ bot_id: 'bot_A', name: 'Bot A', status: 'idle' }] }),
    });
    await assert.rejects(pendingBotsA, /登录凭证已变更/);
    assert.equal(storeMod.store.token, 'cred-list-B');
    assert.deepEqual(storeMod.store.bots.map(b => b.bot_id), ['bot_B'], 'late 200 from A must not overwrite B list');
    assert.equal(storeMod.store.loadingBots, false);

    // 13: 显式切换凭证 (A -> 无效 B) 时必须立即清空旧 Bot、历史事件、选中项、游标与加载态；而内部自动续期则保留它们
    globalThis.fetch = async (url, opts) => {
        if (url === '/agent/api/v1/auth/refresh') {
            return {
                ok: true, status: 200,
                json: async () => ({ success: true, session_token: 'cred-A-refreshed', refresh_token: 'rt-A-2' }),
            };
        }
        const auth = (opts.headers || {}).Authorization;
        if (auth === 'Bearer cred-state-A' || auth === 'Bearer cred-A-refreshed') {
            if (url.endsWith('/bots')) {
                return { ok: true, status: 200, json: async () => ({ success: true, bots: [{ bot_id: 'bot_A', status: 'idle' }] }) };
            }
            if (url.includes('/events')) {
                return { ok: true, status: 200, json: async () => ({ success: true, events: [{ seq_id: 7, event_type: 'user_message', payload: { text: 'secret A' } }] }) };
            }
            return { ok: true, status: 200, json: async () => ({ success: true, nodes: [{ node_id: 'nA' }] }) };
        }
        return { ok: false, status: 403, json: async () => ({ error: 'forbidden' }) };
    };
    storeMod.saveToken('cred-state-A', { source: 'session', refreshToken: 'rt-A-1' });
    await storeMod.fetchBots();
    storeMod.store.activeBotId = 'bot_A';
    await storeMod.fetchEventsIncremental('bot_A');
    assert.equal(storeMod.store.bots.length, 1);
    assert.equal(storeMod.store.eventsByBot['bot_A'].length, 1);
    assert.equal(storeMod.store.historySyncSeqByBot['bot_A'], 7);

    // 同一会话内部自动续期必须保留已加载状态与游标
    assert.equal(await storeMod.refreshWebSession(), true);
    assert.equal(storeMod.store.token, 'cred-A-refreshed');
    assert.equal(storeMod.store.bots.length, 1);
    assert.equal(storeMod.store.activeBotId, 'bot_A');
    assert.equal(storeMod.store.eventsByBot['bot_A'].length, 1);
    assert.equal(storeMod.store.historySyncSeqByBot['bot_A'], 7);

    // 显式切换到无效凭证 B（B 鉴权失败 403），绝不可残留 A 的任何数据或卡在 loading 态
    storeMod.store.loadingBots = true;
    storeMod.store.loadingEvents = true;
    storeMod.saveToken('cred-invalid-B');
    assert.deepEqual(storeMod.store.bots, []);
    assert.equal(storeMod.store.activeBotId, null);
    assert.deepEqual(storeMod.store.eventsByBot, {});
    assert.deepEqual(storeMod.store.historySyncSeqByBot, {});
    assert.equal(storeMod.store.loadingBots, false);
    assert.equal(storeMod.store.loadingEvents, false);
    await assert.rejects(storeMod.fetchBots(), /鉴权失败/);
    assert.deepEqual(storeMod.store.bots, []);
    assert.equal(storeMod.store.wsState, 'auth_required');

    // 14: 真实加载 app.js 验证：密码登录请求在途期间用户切到「Token 直连」并保存，迟到的密码登录成功响应不得覆盖手动 Token
    let resolveLogin = null;
    globalThis.fetch = async (url) => {
        if (url === '/agent/api/v1/auth') {
            return await new Promise(r => { resolveLogin = r; });
        }
        return { ok: true, status: 200, json: async () => ({ success: true, bots: [], nodes: [], approvals: [], models: [] }) };
    };
    globalThis.window.location.search = '';
    globalThis.window.location.hash = '';
    globalThis.window.innerWidth = 1280;
    globalThis.window.addEventListener = () => {};
    globalThis.window.removeEventListener = () => {};
    let capturedSetup = null;
    globalThis.__testCreateAppStub = (comp) => ({
        mount() {
            capturedSetup = comp.setup();
            return capturedSetup;
        },
    });
    const vueStubCode = `
      export const ref = (v) => ({ value: v });
      export const computed = (fn) => ({ get value() { return fn(); } });
      export const watch = () => {};
      export const onMounted = () => {};
      export const onUnmounted = () => {};
      export const nextTick = (fn) => Promise.resolve().then(fn);
      export const createApp = (...args) => globalThis.__testCreateAppStub(...args);
    `;
    const vueStubUrl = 'data:text/javascript;base64,' + Buffer.from(vueStubCode).toString('base64');
    const storeAbsUrl = pathToFileURL(fs.realpathSync('web_dashboard/mobile/store.js')).href;
    const rawAppJs = fs.readFileSync('web_dashboard/mobile/app.js', 'utf8')
        .replace("'./vendor/vue.esm-browser.prod.js'", JSON.stringify(vueStubUrl))
        .replace(/from '\.\/store\.js\?v=\d+'/, `from ${JSON.stringify(storeAbsUrl)}`);
    await import('data:text/javascript;base64,' + Buffer.from(rawAppJs).toString('base64'));

    capturedSetup.loginUsername.value = 'admin';
    capturedSetup.loginPassword.value = 'secret';
    const pendingPwdLogin = capturedSetup.handlePasswordLogin();
    while (typeof resolveLogin !== 'function') await new Promise(r => setTimeout(r, 1));
    // 用户在密码请求等待期间切到「Token 直连」并保存
    capturedSetup.tokenInput.value = 'manual-token-kept';
    capturedSetup.handleSaveToken();
    assert.equal(capturedSetup.loggingIn.value, false, 'switching to manual token unlocks loggingIn immediately');
    resolveLogin({
        ok: true, status: 200,
        json: async () => ({ success: true, session_token: 'late-pwd-session', refresh_token: 'late-pwd-rt' }),
    });
    await pendingPwdLogin;
    assert.equal(storeMod.store.token, 'manual-token-kept', 'late password login 200 must NOT overwrite manual token');
    assert.equal(sessionStorage.getItem('pc_cli_token_source'), 'manual');

    // 15: Android bridge 原生续期在途期间用户保存 manual-new，迟到的 applyNativeToken 回调（含配对 reqId 与双参回调）严禁覆盖手动 Token
    const bridgePosts = [];
    globalThis.window.AndroidPcCliVoiceChannel = {
        postMessage: (raw) => bridgePosts.push(JSON.parse(raw)),
    };
    sockets.length = 0;
    storeMod.saveToken('native-old');
    storeMod.connectWs();
    assert.equal(sockets.length, 1);
    sockets[0].onopen();
    sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
    assert.equal(bridgePosts.length, 1);
    assert.equal(bridgePosts[0].action, 'refreshAuth');
    assert.ok(bridgePosts[0].reqId, 'refreshAuth must carry paired reqId');
    // 等待原生续期期间，用户在界面中手动保存 manual-new
    storeMod.saveToken('manual-new');
    assert.equal(storeMod.store.token, 'manual-new');
    // 旧原生续期回调迟到（先带 reqId 回调、再跟一次无 reqId 回调）
    storeMod.applyNativeToken('late-native', true, bridgePosts[0].reqId);
    storeMod.applyNativeToken('late-native', true);
    assert.equal(storeMod.store.token, 'manual-new', 'late native refresh callback must NOT overwrite manual-new');
    assert.equal(localStorage.getItem('pc_cli_client_token'), 'manual-new');

    // 15b: 「旧连接请求续期 → 4.5 秒兜底超时 → 用户保存 manual-new → 旧续期回调迟到」两条桥接形态均必须拒绝
    const realSetTimeout = globalThis.setTimeout;
    const realClearTimeout = globalThis.clearTimeout;
    let fallbackTimeoutFn = null;
    globalThis.setTimeout = (fn, ms, ...rest) => {
        if (ms === 4500) {
            fallbackTimeoutFn = fn;
            return 999001;
        }
        return realSetTimeout(fn, ms, ...rest);
    };
    globalThis.clearTimeout = (id) => {
        if (id === 999001) {
            fallbackTimeoutFn = null;
            return;
        }
        return realClearTimeout(id);
    };
    try {
        // 子路径 A：带配对 reqId 的迟到回调
        bridgePosts.length = 0;
        sockets.length = 0;
        storeMod.saveToken('native-timeout-A');
        storeMod.connectWs();
        sockets[0].onopen();
        sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
        const reqIdA = bridgePosts[0].reqId;
        assert.equal(typeof fallbackTimeoutFn, 'function');
        // 触发 4.5s 兜底超时
        fallbackTimeoutFn();
        assert.equal(storeMod.store.wsState, 'auth_required');
        // 随后用户保存 manual-after-timeout-A
        storeMod.saveToken('manual-after-timeout-A');
        // 旧续期回调迟到（带 reqIdA）
        storeMod.applyNativeToken('late-native-A', true, reqIdA);
        assert.equal(storeMod.store.token, 'manual-after-timeout-A', 'paired reqId callback after timeout+switch must be rejected');

        // 子路径 B：兼容旧版 APK 无 reqId 双参回调（超时 → 切换 → 迟到双参回调）
        bridgePosts.length = 0;
        sockets.length = 0;
        storeMod.saveToken('native-timeout-B');
        storeMod.connectWs();
        sockets[0].onopen();
        sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
        assert.equal(typeof fallbackTimeoutFn, 'function');
        fallbackTimeoutFn();
        assert.equal(storeMod.store.wsState, 'auth_required');
        storeMod.saveToken('manual-after-timeout-B');
        storeMod.applyNativeToken('late-native-B', true);
        assert.equal(storeMod.store.token, 'manual-after-timeout-B', 'legacy 2-arg callback after timeout+switch must be rejected');

        // 子路径 C：仅超时未切换凭证，迟到续期回调同样必须被拒绝
        bridgePosts.length = 0;
        sockets.length = 0;
        storeMod.saveToken('native-timeout-C');
        storeMod.connectWs();
        sockets[0].onopen();
        sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
        const reqIdC = bridgePosts[0].reqId;
        fallbackTimeoutFn();
        assert.equal(storeMod.store.wsState, 'auth_required');
        storeMod.applyNativeToken('late-native-C', true, reqIdC);
        assert.equal(storeMod.store.token, 'native-timeout-C');
        assert.equal(storeMod.store.wsState, 'auth_required');

        // 子路径 D：A 续期超时 → 用户切换到 B → B 又发起新一轮续期 → 此时 A 的迟到无 ID 双参回调（或旧 reqId 回调）绝不可被认成 B 的响应，随后 B 的配对 reqId 正常生效
        bridgePosts.length = 0;
        sockets.length = 0;
        storeMod.saveToken('native-chain-A');
        storeMod.connectWs();
        sockets[0].onopen();
        sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
        const reqIdChainA = bridgePosts[0].reqId;
        fallbackTimeoutFn();
        assert.equal(storeMod.store.wsState, 'auth_required');
        // 切换到 B，B 连接后也收到 auth.error 并发起新一轮续期
        storeMod.saveToken('native-chain-B');
        storeMod.connectWs();
        assert.equal(sockets.length, 2);
        sockets[1].onopen();
        sockets[1].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
        assert.equal(bridgePosts.length, 2);
        const reqIdChainB = bridgePosts[1].reqId;
        assert.notEqual(reqIdChainA, reqIdChainB);
        // 此时 A 的无 ID 迟到双参回调与旧 reqId 回调先后到达，均必须被丢弃且不得破坏 B 的在途续期
        storeMod.applyNativeToken('late-from-A-no-id', true);
        storeMod.applyNativeToken('late-from-A-old-id', true, reqIdChainA);
        assert.equal(storeMod.store.token, 'native-chain-B', 'unattributed / stale A callbacks must NOT overwrite B');
        // 随后 B 的真实配对响应到达，正常更新并重连
        storeMod.applyNativeToken('fresh-for-B', true, reqIdChainB);
        assert.equal(storeMod.store.token, 'fresh-for-B');
        assert.equal(sockets.length, 3);
    } finally {
        globalThis.setTimeout = realSetTimeout;
        globalThis.clearTimeout = realClearTimeout;
        delete globalThis.window.AndroidPcCliVoiceChannel;
    }

    // 16: auth.ok 后等待 subscribe.ok 期间若切换凭证导致订阅等待器返回 false，严禁继续调用 fetchEventsIncremental 拉取旧 Bot 历史
    let eventsFetchCalls = [];
    globalThis.fetch = async (url, opts) => {
        if (url.includes('/events')) {
            eventsFetchCalls.push({ url, auth: (opts.headers || {}).Authorization });
        }
        return { ok: true, status: 200, json: async () => ({ success: true, bots: [], nodes: [], approvals: [], events: [] }) };
    };
    globalThis.WebSocket.OPEN = 1;
    sockets.length = 0;
    storeMod.saveToken('sub-cred-A');
    storeMod.store.activeBotId = 'bot_sub_old';
    storeMod.connectWs();
    sockets[0].readyState = 1;
    sockets[0].onopen();
    sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.ok' }) });
    assert.equal(sockets[0].sent.at(-1).type, 'subscribe');
    assert.equal(sockets[0].sent.at(-1).bot_id, 'bot_sub_old');
    // 尚未收到 subscribe.ok 时切换到新凭证 B（清理订阅等待器并 resolve(false)）
    storeMod.saveToken('sub-cred-B');
    await new Promise(r => setTimeout(r, 10));
    assert.equal(eventsFetchCalls.length, 0, 'cancelled subscribe waiter on auth.ok must NOT fetch old bot events');

    // 17: 全新 Android WebView 实例（_userExplicitOverride === false）下：
    //   (a) refreshAuth 4.5s 超时 → 带 reqId 迟到响应先被拒并清理待决记录 → 紧随其后的无 ID 注入仍必须被拒绝，严禁覆盖 auth_required
    //   (b) refreshAuth 按时成功后，后续主动预检产生的无 ID 热更新仍正常生效
    localStorage.clear();
    sessionStorage.clear();
    let freshTimeoutFn = null;
    globalThis.setTimeout = (fn, ms, ...rest) => {
        if (ms === 4500) {
            freshTimeoutFn = fn;
            return 999002;
        }
        return realSetTimeout(fn, ms, ...rest);
    };
    globalThis.clearTimeout = (id) => {
        if (id === 999002) {
            freshTimeoutFn = null;
            return;
        }
        return realClearTimeout(id);
    };
    try {
        const freshPosts = [];
        globalThis.window.AndroidPcCliVoiceChannel = {
            postMessage: (raw) => freshPosts.push(JSON.parse(raw)),
        };

        // (a) 超时且用户未手动切换凭证（_userExplicitOverride === false）时，先拒带 ID、再拒延迟无 ID 注入
        sockets.length = 0;
        const freshTimeoutMod = await import(`${storeAbsUrl}?fresh_android=1`);
        freshTimeoutMod.applyNativeToken('android-boot-tok', true);
        assert.equal(freshTimeoutMod.store.token, 'android-boot-tok');
        assert.equal(sockets.length, 1);
        sockets[0].onopen();
        sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
        assert.equal(freshPosts.length, 1);
        const timedOutReqId = freshPosts[0].reqId;
        assert.equal(typeof freshTimeoutFn, 'function');
        freshTimeoutFn();
        assert.equal(freshTimeoutMod.store.wsState, 'auth_required');

        // 1) 带配对 reqId 的迟到回调先到达：被拒收并清理 _inflightNativeRefresh / _unsettledNativeRefreshIds
        freshTimeoutMod.applyNativeToken('late-compose-tok', true, timedOutReqId);
        assert.equal(freshTimeoutMod.store.token, 'android-boot-tok');
        assert.equal(freshTimeoutMod.store.wsState, 'auth_required');

        // 2) 稍后无 ID 注入（例如 Compose 观察器交错或外部无 ID 调用）到达：在未手动切换凭证情况下仍必须被拒绝！
        freshTimeoutMod.applyNativeToken('late-compose-tok', true);
        freshTimeoutMod.applyNativeToken('any-unpaired-after-timeout', true);
        assert.equal(freshTimeoutMod.store.token, 'android-boot-tok', 'delayed unpaired injection after timeout must NOT overwrite token');
        assert.equal(freshTimeoutMod.store.wsState, 'auth_required', 'delayed unpaired injection after timeout must NOT clear auth_required');
        assert.equal(sockets.length, 1, 'no reconnect socket may be opened by rejected unpaired injection');

        // 用户显式手动录入新 Token 后可立即恢复连接
        freshTimeoutMod.saveToken('user-explicit-recovery', { userInitiated: true });
        freshTimeoutMod.connectWs();
        assert.equal(freshTimeoutMod.store.token, 'user-explicit-recovery');
        assert.equal(sockets.length, 2);

        // (b) 正常未超时续期路径：带 reqId 续期成功并 auth.ok 后，后续 15 分钟主动预检的无 ID 新令牌可无缝热更新
        freshPosts.length = 0;
        sockets.length = 0;
        localStorage.clear();
        sessionStorage.clear();
        const freshHappyMod = await import(`${storeAbsUrl}?fresh_android=2`);
        freshHappyMod.applyNativeToken('boot-happy-tok', true);
        assert.equal(sockets.length, 1);
        sockets[0].onopen();
        sockets[0].onmessage({ data: JSON.stringify({ type: 'auth.error' }) });
        const happyReqId = freshPosts[0].reqId;
        freshHappyMod.applyNativeToken('renewed-happy-tok', true, happyReqId);
        assert.equal(freshHappyMod.store.token, 'renewed-happy-tok');
        assert.equal(sockets.length, 2);
        sockets[1].readyState = 1;
        sockets[1].onopen();
        sockets[1].onmessage({ data: JSON.stringify({ type: 'auth.ok' }) });
        assert.equal(freshHappyMod.store.wsState, 'connected');
        // 后续主动预检注入全新 Token：热更新 store.token 且不断开已连接 WS
        freshHappyMod.applyNativeToken('proactive-hot-tok', true);
        assert.equal(freshHappyMod.store.token, 'proactive-hot-tok');
        assert.equal(freshHappyMod.store.wsState, 'connected');
        assert.equal(sockets.length, 2);
    } finally {
        globalThis.setTimeout = realSetTimeout;
        globalThis.clearTimeout = realClearTimeout;
        delete globalThis.window.AndroidPcCliVoiceChannel;
    }

    console.log('Meeting & PC CLI session frontend: 18 scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
