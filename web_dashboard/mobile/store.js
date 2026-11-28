/**
 * Mobile PWA 响应式状态管理 + REST + WebSocket 断线续传 (after_seq)
 */
import { reactive } from './vendor/vue.esm-browser.prod.js';

const TOKEN_STORAGE_KEY = 'pc_cli_client_token';
const USER_SESSION_TOKEN_KEY = 'user_session_token';
const USER_REFRESH_TOKEN_KEY = 'user_refresh_token';
const USER_SESSION_EXPIRES_KEY = 'user_session_expires_at';
const TOKEN_SOURCE_KEY = 'pc_cli_token_source';
const API_BASE = '/agent/api/v1/pc-cli';
const REFRESH_LEEWAY_SEC = 90;

function readInitialToken() {
  try {
    const source = sessionStorage.getItem(TOKEN_SOURCE_KEY);
    if (source === 'manual') {
      return localStorage.getItem(TOKEN_STORAGE_KEY) || '';
    }
    const sess = sessionStorage.getItem(USER_SESSION_TOKEN_KEY);
    if (sess && sess.trim()) return sess.trim();
  } catch (_) {}
  return localStorage.getItem(TOKEN_STORAGE_KEY) || '';
}

const STATUS_WEIGHT = {
  waiting_approval: 0,
  running: 1,
  error: 2,
  idle: 3,
};

export const store = reactive({
  token: readInitialToken(),
  wsState: 'disconnected', // disconnected | connecting | connected | auth_required
  nodes: [],
  bots: [],
  activeBotId: null,
  selectedNodeFilter: '',
  eventsByBot: {},
  historySyncSeqByBot: {}, // 独立历史分页同步游标，绝不被实时 WS 推送提前推进
  pendingApprovals: [],
  activeTab: 'bots', // bots | dashboard | manager
  models: [],
  selectedManagerModel: '',
  stats: null,
  managerMessages: [],
  toastMsg: '',
  loadingBots: false,
  loadingEvents: false,
  favorites: [],
  botFavorites: [],
  collectedSourceKeys: [],
  loadingFavorites: false,
});

let _ws = null;
let _reconnectTimer = null;
let _reconnectDelay = 1000;
let _toastTimer = null;
let _subscribeWaiter = null; // { botId, resolve, timer }
let _nativeRefreshSeq = 0;
let _nativeAuthRefreshPending = false;
let _nativeAuthFallbackTimer = null;
let _lastRejectedNativeToken = '';
let _inflightNativeRefresh = null; // { reqId, epoch, status: 'pending' | 'timed_out' | 'superseded' }
const _unsettledNativeRefreshIds = new Set();
let _nativeRefreshTimedOutEpoch = -1;
const _rejectedNativeTokens = new Set();
let _userExplicitOverride = false;
let _webRefreshPromise = null;
let _webAuthRefreshPending = false;
let _credentialEpoch = 0;
let _wsWebRefreshAttempts = 0;
const MAX_WS_WEB_REFRESH_ATTEMPTS = 1;

export function showToast(msg, durationMs = 2600) {
  store.toastMsg = msg;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    store.toastMsg = '';
  }, durationMs);
}

export function getCredentialEpoch() {
  return _credentialEpoch;
}

function resetCredentialBoundState() {
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  if (_ws) {
    const oldWs = _ws;
    _ws = null;
    try { oldWs.close(); } catch (_) {}
  }
  if (_subscribeWaiter) {
    clearTimeout(_subscribeWaiter.timer);
    _subscribeWaiter.resolve(false);
    _subscribeWaiter = null;
  }
  store.nodes = [];
  store.bots = [];
  store.activeBotId = null;
  store.selectedNodeFilter = '';
  store.eventsByBot = {};
  store.historySyncSeqByBot = {};
  store.pendingApprovals = [];
  store.models = [];
  store.selectedManagerModel = '';
  store.stats = null;
  store.managerMessages = [];
  store.loadingBots = false;
  store.loadingEvents = false;
  store.favorites = [];
  store.botFavorites = [];
  store.collectedSourceKeys = [];
  store.loadingFavorites = false;
}

export function saveToken(tok, options = {}) {
  const clean = (tok || '').trim();
  const rawSource = options && options.source;
  const source = rawSource === 'session' ? 'session' : (rawSource === 'native' ? 'native' : 'manual');
  const isInternalRefresh = Boolean(options && options.internalRefresh);
  if (options && options.userInitiated) {
    _userExplicitOverride = true;
  }
  if (!isInternalRefresh) {
    _credentialEpoch += 1;
    _webRefreshPromise = null;
    _wsWebRefreshAttempts = 0;
    _webAuthRefreshPending = false;
    _nativeAuthRefreshPending = false;
    _nativeRefreshTimedOutEpoch = -1;
    _rejectedNativeTokens.clear();
    if (_inflightNativeRefresh && _inflightNativeRefresh.status === 'pending') {
      _inflightNativeRefresh.status = 'superseded';
    }
    if (_nativeAuthFallbackTimer) {
      clearTimeout(_nativeAuthFallbackTimer);
      _nativeAuthFallbackTimer = null;
    }
    resetCredentialBoundState();
  }
  store.token = clean;
  if (source === 'session') {
    try {
      if (clean) {
        sessionStorage.setItem(USER_SESSION_TOKEN_KEY, clean);
        sessionStorage.setItem(TOKEN_SOURCE_KEY, 'session');
        if (options && typeof options.refreshToken === 'string' && options.refreshToken.trim()) {
          sessionStorage.setItem(USER_REFRESH_TOKEN_KEY, options.refreshToken.trim());
        }
        if (options && options.expiresAt) {
          sessionStorage.setItem(USER_SESSION_EXPIRES_KEY, String(options.expiresAt));
        }
      } else {
        sessionStorage.removeItem(USER_SESSION_TOKEN_KEY);
        sessionStorage.removeItem(USER_REFRESH_TOKEN_KEY);
        sessionStorage.removeItem(USER_SESSION_EXPIRES_KEY);
        sessionStorage.removeItem(TOKEN_SOURCE_KEY);
      }
    } catch (_) {}
    return;
  }
  // 手动「Token 直连」或原生注入：仅切换当前连接来源为 manual，保留 user_session_token / user_refresh_token 供主页退出时撤销
  try {
    sessionStorage.setItem(TOKEN_SOURCE_KEY, 'manual');
  } catch (_) {}
  if (clean) {
    localStorage.setItem(TOKEN_STORAGE_KEY, clean);
  } else {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

function canUseWebRefresh() {
  try {
    if (sessionStorage.getItem(TOKEN_SOURCE_KEY) === 'manual') return false;
    const rt = sessionStorage.getItem(USER_REFRESH_TOKEN_KEY);
    return Boolean(rt && rt.trim());
  } catch (_) {
    return false;
  }
}

function isSessionNearExpiry() {
  try {
    const exp = Number(sessionStorage.getItem(USER_SESSION_EXPIRES_KEY) || 0);
    if (!exp) return false;
    return Date.now() / 1000 >= exp - REFRESH_LEEWAY_SEC;
  } catch (_) {
    return false;
  }
}

function clearWebRefreshTokens() {
  try {
    sessionStorage.removeItem(USER_REFRESH_TOKEN_KEY);
    sessionStorage.removeItem(USER_SESSION_EXPIRES_KEY);
  } catch (_) {}
}

export async function refreshWebSession({ failedToken = '' } = {}) {
  if (!canUseWebRefresh()) return false;
  // 若并发调用中已有其他协程率先完成刷新，直接复用新 token，严禁重复提交旧 refresh_token 触发重用检测
  if (failedToken && store.token && store.token !== failedToken) {
    return true;
  }
  if (_webRefreshPromise) {
    return _webRefreshPromise;
  }
  const p = (async () => {
    const startEpoch = _credentialEpoch;
    const startAccessToken = store.token;
    let refreshToken = '';
    try {
      refreshToken = (sessionStorage.getItem(USER_REFRESH_TOKEN_KEY) || '').trim();
    } catch (_) {}
    if (!refreshToken) return false;

    const isStaleContext = () => {
      if (_credentialEpoch !== startEpoch) return true;
      if (!canUseWebRefresh()) return true;
      try {
        if ((sessionStorage.getItem(USER_REFRESH_TOKEN_KEY) || '').trim() !== refreshToken) {
          return true;
        }
      } catch (_) {
        return true;
      }
      return store.token !== startAccessToken;
    };

    try {
      const resp = await fetch('/agent/api/v1/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      const data = await resp.json().catch(() => ({}));
      // 在途期间若用户已切换手动 Token 或重新登录新会话，绝不覆盖新凭证或误删新会话的 refresh_token
      if (isStaleContext()) {
        return false;
      }
      const nextToken = (data.session_token || data.access_token || '').trim();
      if (!resp.ok || !data.success || !nextToken) {
        if (resp.status === 401 || resp.status === 403) {
          clearWebRefreshTokens();
        }
        return false;
      }
      saveToken(nextToken, {
        source: 'session',
        refreshToken: (data.refresh_token || '').trim(),
        expiresAt: data.access_token_expires_at || data.expires_at || '',
        internalRefresh: true,
      });
      try {
        if (data.review_token && data.review_token_expires_at) {
          sessionStorage.setItem('review_token', data.review_token);
          sessionStorage.setItem('review_token_expires_at', String(data.review_token_expires_at));
        }
      } catch (_) {}
      return true;
    } catch (_) {
      return false;
    }
  })().finally(() => {
    if (_webRefreshPromise === p) {
      _webRefreshPromise = null;
    }
  });
  _webRefreshPromise = p;
  return p;
}

async function apiFetch(path, options = {}, _retried = false, _expectedEpoch = _credentialEpoch) {
  if (_credentialEpoch !== _expectedEpoch) {
    throw new Error('登录凭证已变更，请重新操作');
  }
  if (!_retried && canUseWebRefresh() && isSessionNearExpiry()) {
    await refreshWebSession();
    if (_credentialEpoch !== _expectedEpoch) {
      throw new Error('登录凭证已变更，请重新操作');
    }
  }
  const usedToken = store.token;
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (usedToken) {
    headers['Authorization'] = `Bearer ${usedToken}`;
  }
  const resp = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (_credentialEpoch !== _expectedEpoch) {
    throw new Error('登录凭证已变更，请重新操作');
  }
  if (resp.status === 401 || resp.status === 403) {
    if (!_retried && canUseWebRefresh()) {
      const ok = await refreshWebSession({ failedToken: usedToken });
      if (_credentialEpoch !== _expectedEpoch) {
        throw new Error('登录凭证已变更，请重新操作');
      }
      if (ok) {
        return apiFetch(path, options, true, _expectedEpoch);
      }
    }
    if (_credentialEpoch === _expectedEpoch) {
      store.wsState = 'auth_required';
    }
    throw new Error('鉴权失败，请检查访问 Token');
  }
  const data = await resp.json().catch(() => ({}));
  if (_credentialEpoch !== _expectedEpoch) {
    throw new Error('登录凭证已变更，请重新操作');
  }
  if (!resp.ok || data.success === false) {
    throw new Error(data.error || `请求失败 (${resp.status})`);
  }
  return data;
}

export function sortBotsInPlace() {
  store.bots.sort((a, b) => {
    const wa = STATUS_WEIGHT[a.status] ?? 9;
    const wb = STATUS_WEIGHT[b.status] ?? 9;
    if (wa !== wb) return wa - wb;
    return (b.updated_at || 0) - (a.updated_at || 0);
  });
}

export async function fetchNodes() {
  const data = await apiFetch('/nodes');
  store.nodes = data.nodes || [];
  return store.nodes;
}

export async function fetchNodeDirs(nodeId, path = '') {
  const q = path ? `?path=${encodeURIComponent(path)}` : '';
  const data = await apiFetch(`/nodes/${encodeURIComponent(nodeId)}/dirs${q}`);
  return data.entries || [];
}

export async function fetchBots() {
  const startEpoch = _credentialEpoch;
  store.loadingBots = true;
  try {
    const data = await apiFetch('/bots', {}, false, startEpoch);
    store.bots = data.bots || [];
    sortBotsInPlace();
    return store.bots;
  } finally {
    if (_credentialEpoch === startEpoch) {
      store.loadingBots = false;
    }
  }
}

export async function createBot(payload) {
  const data = await apiFetch('/bots', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  await fetchBots();
  return data.bot;
}

export async function updateBot(botId, payload) {
  const data = await apiFetch(`/bots/${encodeURIComponent(botId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
  if (data.bot) {
    const idx = store.bots.findIndex((b) => b.bot_id === botId);
    if (idx >= 0) {
      store.bots.splice(idx, 1, data.bot);
    } else {
      store.bots.push(data.bot);
    }
    sortBotsInPlace();
  }
  return data.bot;
}

export async function deleteBot(botId) {
  await apiFetch(`/bots/${encodeURIComponent(botId)}`, { method: 'DELETE' });
  store.bots = store.bots.filter((b) => b.bot_id !== botId);
  delete store.eventsByBot[botId];
  delete store.historySyncSeqByBot[botId];
  if (store.activeBotId === botId) {
    store.activeBotId = null;
  }
}

export function mergeEvents(botId, newEvents, { advanceHistoryCursor = false } = {}) {
  if (!store.eventsByBot[botId]) {
    store.eventsByBot[botId] = [];
  }
  const existing = store.eventsByBot[botId];
  const seen = new Set(existing.map((e) => e.seq_id));
  let maxBatchSeq = store.historySyncSeqByBot[botId] || 0;
  for (const ev of newEvents) {
    if (!seen.has(ev.seq_id)) {
      existing.push(ev);
      seen.add(ev.seq_id);
    }
    if (advanceHistoryCursor && ev.seq_id > maxBatchSeq) {
      maxBatchSeq = ev.seq_id;
    }
  }
  existing.sort((a, b) => a.seq_id - b.seq_id);
  if (advanceHistoryCursor) {
    store.historySyncSeqByBot[botId] = maxBatchSeq;
  }
}

export async function fetchEventsIncremental(botId) {
  const startEpoch = _credentialEpoch;
  const pageSize = 200;
  const maxPagesPerBatch = 25;
  const initialSeq = store.historySyncSeqByBot[botId] || 0;
  store.loadingEvents = initialSeq === 0;
  let hasMore = false;
  try {
    for (let page = 0; page < maxPagesPerBatch; page++) {
      const afterSeq = store.historySyncSeqByBot[botId] || 0;
      const data = await apiFetch(
        `/bots/${encodeURIComponent(botId)}/events?after_seq=${afterSeq}&limit=${pageSize}`,
        {},
        false,
        startEpoch
      );
      const batch = data.events || [];
      if (batch.length > 0) {
        mergeEvents(botId, batch, { advanceHistoryCursor: true });
      }
      if (batch.length < pageSize) {
        hasMore = false;
        break;
      }
      hasMore = true;
    }
    if (hasMore && _credentialEpoch === startEpoch && store.activeBotId === botId) {
      showToast('历史事件较多，正在继续同步...', 1500);
      setTimeout(() => {
        if (_credentialEpoch === startEpoch && store.activeBotId === botId) {
          fetchEventsIncremental(botId).catch(() => {});
        }
      }, 60);
    }
  } finally {
    if (_credentialEpoch === startEpoch) {
      store.loadingEvents = false;
    }
  }
}

export async function sendTurn(botId, prompt, inputSource = 'text') {
  const data = await apiFetch(`/bots/${encodeURIComponent(botId)}/turns`, {
    method: 'POST',
    body: JSON.stringify({ prompt, input_source: inputSource }),
  });
  if (data.event) {
    mergeEvents(botId, [data.event]);
  }
  return data;
}

export async function cancelTurn(botId) {
  return apiFetch(`/bots/${encodeURIComponent(botId)}/cancel`, { method: 'POST' });
}

export async function fetchPendingApprovals(botId = null) {
  const q = botId ? `?bot_id=${encodeURIComponent(botId)}` : '';
  const data = await apiFetch(`/approvals${q}`);
  store.pendingApprovals = data.approvals || [];
  return store.pendingApprovals;
}

export async function submitApproval(approvalId, decision, inputSource = 'button') {
  const data = await apiFetch(`/approvals/${encodeURIComponent(approvalId)}`, {
    method: 'POST',
    body: JSON.stringify({ decision, input_source: inputSource }),
  });
  store.pendingApprovals = store.pendingApprovals.filter((a) => a.approval_id !== approvalId);
  // 同步标记总管对话中已渲染的授权卡片，防止重复点击
  for (const m of store.managerMessages) {
    if (Array.isArray(m.approvals)) {
      for (const a of m.approvals) {
        if (a.approval_id === approvalId) {
          a.resolved = decision;
        }
      }
    }
  }
  return data;
}

export async function fetchModels() {
  const data = await apiFetch('/models');
  store.models = data.models || [];
  if (!store.selectedManagerModel && data.default_model) {
    store.selectedManagerModel = data.default_model;
  }
  return store.models;
}

export async function fetchStats(days = 7) {
  const data = await apiFetch(`/stats?days=${encodeURIComponent(days)}`);
  store.stats = data.stats || null;
  if (store.stats && Array.isArray(store.stats.pending_approvals)) {
    store.pendingApprovals = store.stats.pending_approvals;
  }
  return store.stats;
}

export async function sendManagerChat(message, modelName = '', priorHistory = null) {
  const history = Array.isArray(priorHistory)
    ? priorHistory
    : store.managerMessages
        .filter((m) => (m.role === 'user' || m.role === 'assistant') && !m.failed)
        .slice(-12)
        .map((m) => ({ role: m.role, content: m.text || '' }));
  return apiFetch('/manager/chat', {
    method: 'POST',
    body: JSON.stringify({
      message,
      model_name: modelName || store.selectedManagerModel || '',
      history,
    }),
  });
}

export async function fetchFavorites({ q = '', botId = '', limit = 30 } = {}) {
  const startEpoch = _credentialEpoch;
  store.loadingFavorites = true;
  try {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (botId) params.set('bot_id', botId);
    params.set('limit', String(limit || 30));
    const data = await apiFetch(`/favorites?${params.toString()}`, {}, false, startEpoch);
    store.favorites = data.favorites || [];
    if (botId && Array.isArray(data.collected_keys)) {
      store.collectedSourceKeys = data.collected_keys;
    }
    return data;
  } finally {
    if (_credentialEpoch === startEpoch) {
      store.loadingFavorites = false;
    }
  }
}

export async function fetchBotFavorites(botId) {
  if (!botId) {
    store.botFavorites = [];
    store.collectedSourceKeys = [];
    return [];
  }
  const startEpoch = _credentialEpoch;
  const params = new URLSearchParams({ bot_id: botId, limit: '50' });
  const data = await apiFetch(`/favorites?${params.toString()}`, {}, false, startEpoch);
  if (_credentialEpoch === startEpoch && store.activeBotId === botId) {
    store.botFavorites = data.favorites || [];
    store.collectedSourceKeys = Array.isArray(data.collected_keys) ? data.collected_keys : [];
  }
  return store.botFavorites;
}

export async function saveFavorite(payload) {
  return apiFetch('/favorites', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function deleteFavorite(favId) {
  const data = await apiFetch(`/favorites/${encodeURIComponent(favId)}`, { method: 'DELETE' });
  store.favorites = store.favorites.filter((f) => f.id !== favId);
  store.botFavorites = store.botFavorites.filter((f) => f.id !== favId);
  return data;
}

function getWsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws/pc-cli/client`;
}

export function subscribeBotOnWs(botId, timeoutMs = 1500) {
  if (_subscribeWaiter) {
    clearTimeout(_subscribeWaiter.timer);
    _subscribeWaiter.resolve(false);
    _subscribeWaiter = null;
  }
  if (!_ws || _ws.readyState !== WebSocket.OPEN || store.wsState !== 'connected') {
    return Promise.resolve(false);
  }
  const target = botId || null;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      if (_subscribeWaiter && _subscribeWaiter.resolve === resolve) {
        _subscribeWaiter = null;
      }
      resolve(false);
    }, timeoutMs);
    _subscribeWaiter = { botId: target, resolve, timer };
    _ws.send(JSON.stringify({ type: 'subscribe', bot_id: target }));
  });
}

export function connectWs() {
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  if (_ws) {
    try { _ws.close(); } catch (_) {}
    _ws = null;
  }
  if (!store.token) {
    if (canUseWebRefresh() && !_webAuthRefreshPending && _wsWebRefreshAttempts < MAX_WS_WEB_REFRESH_ATTEMPTS) {
      _wsWebRefreshAttempts += 1;
      _webAuthRefreshPending = true;
      const wsEpoch = _credentialEpoch;
      store.wsState = 'connecting';
      refreshWebSession().then((ok) => {
        if (_credentialEpoch !== wsEpoch) return;
        _webAuthRefreshPending = false;
        if (ok && store.token) {
          connectWs();
        } else {
          store.wsState = 'auth_required';
        }
      });
      return;
    }
    store.wsState = 'auth_required';
    return;
  }

  if (canUseWebRefresh() && isSessionNearExpiry() && !_webAuthRefreshPending && _wsWebRefreshAttempts < MAX_WS_WEB_REFRESH_ATTEMPTS) {
    _wsWebRefreshAttempts += 1;
    _webAuthRefreshPending = true;
    const wsEpoch = _credentialEpoch;
    store.wsState = 'connecting';
    refreshWebSession().then((ok) => {
      if (_credentialEpoch !== wsEpoch) return;
      _webAuthRefreshPending = false;
      if (ok && store.token) {
        connectWs();
      } else {
        store.wsState = 'auth_required';
      }
    });
    return;
  }

  store.wsState = 'connecting';
  const ws = new WebSocket(getWsUrl());
  const sockEpoch = _credentialEpoch;
  let sentToken = '';
  _ws = ws;

  ws.onopen = () => {
    if (_ws !== ws || _credentialEpoch !== sockEpoch) return;
    sentToken = store.token;
    ws.send(JSON.stringify({ type: 'auth', token: sentToken }));
  };

  ws.onmessage = (evt) => {
    if (_ws !== ws || _credentialEpoch !== sockEpoch) return;
    let msg;
    try { msg = JSON.parse(evt.data); } catch (_) { return; }

    if (msg.type === 'auth.ok') {
      _nativeAuthRefreshPending = false;
      _nativeRefreshTimedOutEpoch = -1;
      _rejectedNativeTokens.clear();
      _lastRejectedNativeToken = '';
      _wsWebRefreshAttempts = 0;
      if (_nativeAuthFallbackTimer) {
        clearTimeout(_nativeAuthFallbackTimer);
        _nativeAuthFallbackTimer = null;
      }
      store.wsState = 'connected';
      _reconnectDelay = 1000;
      if (store.activeBotId) {
        const activeId = store.activeBotId;
        subscribeBotOnWs(activeId)
          .then((ok) => {
            if (ok && _credentialEpoch === sockEpoch && store.activeBotId === activeId) {
              return fetchEventsIncremental(activeId);
            }
          })
          .catch(() => {});
      }
      fetchNodes().catch(() => {});
      fetchBots().catch(() => {});
      fetchPendingApprovals().catch(() => {});
      return;
    }

    if (msg.type === 'subscribe.ok') {
      if (_subscribeWaiter && _subscribeWaiter.botId === (msg.bot_id || null)) {
        clearTimeout(_subscribeWaiter.timer);
        const res = _subscribeWaiter.resolve;
        _subscribeWaiter = null;
        res(true);
      }
      return;
    }

    if (msg.type === 'auth.error') {
      const canAskNative =
        typeof window !== 'undefined' &&
        window.AndroidPcCliVoiceChannel &&
        typeof window.AndroidPcCliVoiceChannel.postMessage === 'function';
      const nativeFailedTok = sentToken || store.token;
      if (canAskNative && _credentialEpoch === sockEpoch && !_nativeAuthRefreshPending && nativeFailedTok !== _lastRejectedNativeToken) {
        const reqId = `nref_${++_nativeRefreshSeq}_${Date.now()}`;
        _nativeAuthRefreshPending = true;
        _inflightNativeRefresh = { reqId, epoch: sockEpoch, status: 'pending' };
        _unsettledNativeRefreshIds.add(reqId);
        _lastRejectedNativeToken = nativeFailedTok;
        ws.close();
        try {
          window.AndroidPcCliVoiceChannel.postMessage(JSON.stringify({ action: 'refreshAuth', reqId }));
        } catch (_) {}
        if (_nativeAuthFallbackTimer) clearTimeout(_nativeAuthFallbackTimer);
        _nativeAuthFallbackTimer = setTimeout(() => {
          // 保留 _inflightNativeRefresh 记录并标记 timed_out + 锁定当前代数无 ID 回调，确保超时后任何后续无 ID 注入均无法覆盖 auth_required
          if (_inflightNativeRefresh && _inflightNativeRefresh.reqId === reqId && _inflightNativeRefresh.status === 'pending') {
            _inflightNativeRefresh.status = 'timed_out';
          }
          if (_credentialEpoch === sockEpoch) {
            _nativeRefreshTimedOutEpoch = sockEpoch;
          }
          if (_nativeAuthRefreshPending && _credentialEpoch === sockEpoch) {
            _nativeAuthRefreshPending = false;
            store.wsState = 'auth_required';
            showToast('WebSocket 鉴权失败，请更新 Token');
          }
        }, 4500);
        return;
      }
      if (!canAskNative && _credentialEpoch === sockEpoch && !_webAuthRefreshPending && _wsWebRefreshAttempts < MAX_WS_WEB_REFRESH_ATTEMPTS) {
        const failedTok = sentToken || store.token;
        // 若同一凭证代数内并发 REST 401 已率先完成刷新并替换了 store.token，无需再次请求 /auth/refresh，直接用新令牌重连一次
        if (failedTok && store.token && store.token !== failedTok) {
          _wsWebRefreshAttempts += 1;
          _webAuthRefreshPending = true;
          ws.close();
          _webAuthRefreshPending = false;
          connectWs();
          return;
        }
        if (canUseWebRefresh()) {
          _wsWebRefreshAttempts += 1;
          _webAuthRefreshPending = true;
          ws.close();
          refreshWebSession({ failedToken: failedTok }).then((ok) => {
            if (_credentialEpoch !== sockEpoch) return;
            _webAuthRefreshPending = false;
            if (ok && store.token && store.token !== failedTok) {
              connectWs();
            } else {
              store.wsState = 'auth_required';
              showToast('会话已过期，请重新登录');
            }
          });
          return;
        }
      }
      _nativeAuthRefreshPending = false;
      store.wsState = 'auth_required';
      showToast('WebSocket 鉴权失败，请更新 Token');
      ws.close();
      return;
    }

    if (msg.type === 'nodes.updated') {
      store.nodes = msg.nodes || [];
      return;
    }

    if (msg.type === 'bot.summary_updated' && msg.bot) {
      const idx = store.bots.findIndex((b) => b.bot_id === msg.bot.bot_id);
      if (idx >= 0) {
        store.bots.splice(idx, 1, msg.bot);
      } else {
        store.bots.push(msg.bot);
      }
      sortBotsInPlace();
      if (msg.bot.status === 'waiting_approval') {
        fetchPendingApprovals().catch(() => {});
      }
      return;
    }

    if (msg.type === 'bot.deleted' && msg.bot_id) {
      store.bots = store.bots.filter((b) => b.bot_id !== msg.bot_id);
      if (store.activeBotId === msg.bot_id) {
        store.activeBotId = null;
      }
      return;
    }

    if (msg.type === 'approval.created' && msg.approval) {
      fetchPendingApprovals().catch(() => {});
      fetchStats().catch(() => {});
      return;
    }

    if (msg.type === 'bot.event' && msg.event) {
      mergeEvents(msg.event.bot_id, [msg.event]);
      if (msg.event.event_type === 'approval_request') {
        fetchPendingApprovals().catch(() => {});
      }
    }
  };

  ws.onclose = () => {
    if (_ws !== ws) return;
    _ws = null;
    if (store.wsState === 'auth_required' || _nativeAuthRefreshPending || _webAuthRefreshPending) return;
    store.wsState = 'disconnected';
    _reconnectTimer = setTimeout(() => {
      _reconnectDelay = Math.min(_reconnectDelay * 2, 15000);
      connectWs();
    }, _reconnectDelay);
  };
}

export function applyNativeToken(newToken, refreshedOk = true, reqId = '') {
  const cleaned = String(newToken || '').trim();
  const cleanReqId = String(reqId || '').trim();

  // 1. 携带配对 reqId 的原生续期响应：必须精确命中当前唯一 pending 且同凭证代数的请求，超时/切换/旧请求一律丢弃
  if (cleanReqId) {
    _unsettledNativeRefreshIds.delete(cleanReqId);
    const cur = _inflightNativeRefresh;
    if (!cur || cur.reqId !== cleanReqId) {
      return;
    }
    _inflightNativeRefresh = null;
    if (_nativeAuthFallbackTimer) {
      clearTimeout(_nativeAuthFallbackTimer);
      _nativeAuthFallbackTimer = null;
    }
    const validPending = cur.status === 'pending' && _nativeAuthRefreshPending && cur.epoch === _credentialEpoch;
    _nativeAuthRefreshPending = false;
    if (!validPending) {
      if (cur.epoch === _credentialEpoch) {
        _nativeRefreshTimedOutEpoch = _credentialEpoch;
      }
      if (cleaned) {
        _rejectedNativeTokens.add(cleaned);
      }
      return;
    }
    if (!cleaned) {
      saveToken('');
      store.wsState = 'auth_required';
      return;
    }
    const changed = cleaned !== store.token;
    if (changed) {
      saveToken(cleaned, { source: 'native', internalRefresh: true });
    }
    if (!refreshedOk && !changed) {
      store.wsState = 'auth_required';
      showToast('WebSocket 鉴权失败，请重新登录或更新 Token');
      return;
    }
    connectWs();
    return;
  }

  // 2. 无 reqId 回调无法证明续期归属：存在在途/未决续期、当前代数已发生续期超时、令牌曾被拒收，或用户已显式覆盖凭证时一律丢弃
  if (
    _nativeAuthRefreshPending ||
    _inflightNativeRefresh !== null ||
    _unsettledNativeRefreshIds.size > 0 ||
    _nativeRefreshTimedOutEpoch === _credentialEpoch ||
    _userExplicitOverride ||
    (cleaned && _rejectedNativeTokens.has(cleaned))
  ) {
    return;
  }

  if (!cleaned) {
    saveToken('');
    store.wsState = 'auth_required';
    return;
  }

  const changed = cleaned !== store.token;
  if (changed) {
    saveToken(cleaned, { source: 'native', internalRefresh: Boolean(store.token) });
  }

  if (changed && store.wsState !== 'connected') {
    connectWs();
  }
}

if (typeof window !== 'undefined') {
  window.__onPcCliNativeToken = applyNativeToken;
}

// 手机锁屏/切后台恢复后自动增量补齐进度
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    if (store.wsState !== 'connected' && (store.token || canUseWebRefresh())) {
      connectWs();
    } else {
      fetchBots().catch(() => {});
      fetchPendingApprovals().catch(() => {});
      if (store.activeBotId) {
        fetchEventsIncremental(store.activeBotId).catch(() => {});
      }
    }
  }
});
