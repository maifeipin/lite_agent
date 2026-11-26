/**
 * Mobile PWA 响应式状态管理 + REST + WebSocket 断线续传 (after_seq)
 */
import { reactive } from './vendor/vue.esm-browser.prod.js';

const TOKEN_STORAGE_KEY = 'pc_cli_client_token';
const USER_SESSION_TOKEN_KEY = 'user_session_token';
const TOKEN_SOURCE_KEY = 'pc_cli_token_source';
const API_BASE = '/agent/api/v1/pc-cli';

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
});

let _ws = null;
let _reconnectTimer = null;
let _reconnectDelay = 1000;
let _toastTimer = null;
let _subscribeWaiter = null; // { botId, resolve, timer }
let _nativeAuthRefreshPending = false;
let _nativeAuthFallbackTimer = null;
let _lastRejectedNativeToken = '';

export function showToast(msg, durationMs = 2600) {
  store.toastMsg = msg;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    store.toastMsg = '';
  }, durationMs);
}

export function saveToken(tok, options = {}) {
  const clean = (tok || '').trim();
  const source = options && options.source === 'session' ? 'session' : 'manual';
  store.token = clean;
  if (source === 'session') {
    try {
      if (clean) {
        sessionStorage.setItem(USER_SESSION_TOKEN_KEY, clean);
        sessionStorage.setItem(TOKEN_SOURCE_KEY, 'session');
      } else {
        sessionStorage.removeItem(USER_SESSION_TOKEN_KEY);
        sessionStorage.removeItem(TOKEN_SOURCE_KEY);
      }
    } catch (_) {}
    return;
  }
  // 手动「Token 直连」或原生注入：仅切换当前连接来源为 manual，保留 user_session_token 供主页退出时撤销
  try {
    sessionStorage.setItem(TOKEN_SOURCE_KEY, 'manual');
  } catch (_) {}
  if (clean) {
    localStorage.setItem(TOKEN_STORAGE_KEY, clean);
  } else {
    localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

async function apiFetch(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (store.token) {
    headers['Authorization'] = `Bearer ${store.token}`;
  }
  const resp = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (resp.status === 401 || resp.status === 403) {
    store.wsState = 'auth_required';
    throw new Error('鉴权失败，请检查访问 Token');
  }
  const data = await resp.json().catch(() => ({}));
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
  store.loadingBots = true;
  try {
    const data = await apiFetch('/bots');
    store.bots = data.bots || [];
    sortBotsInPlace();
    return store.bots;
  } finally {
    store.loadingBots = false;
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
  const pageSize = 200;
  const maxPagesPerBatch = 25;
  const initialSeq = store.historySyncSeqByBot[botId] || 0;
  store.loadingEvents = initialSeq === 0;
  let hasMore = false;
  try {
    for (let page = 0; page < maxPagesPerBatch; page++) {
      const afterSeq = store.historySyncSeqByBot[botId] || 0;
      const data = await apiFetch(
        `/bots/${encodeURIComponent(botId)}/events?after_seq=${afterSeq}&limit=${pageSize}`
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
    if (hasMore && store.activeBotId === botId) {
      showToast('历史事件较多，正在继续同步...', 1500);
      setTimeout(() => {
        if (store.activeBotId === botId) {
          fetchEventsIncremental(botId).catch(() => {});
        }
      }, 60);
    }
  } finally {
    store.loadingEvents = false;
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
    store.wsState = 'auth_required';
    return;
  }

  store.wsState = 'connecting';
  const ws = new WebSocket(getWsUrl());
  _ws = ws;

  ws.onopen = () => {
    if (_ws !== ws) return;
    ws.send(JSON.stringify({ type: 'auth', token: store.token }));
  };

  ws.onmessage = (evt) => {
    if (_ws !== ws) return;
    let msg;
    try { msg = JSON.parse(evt.data); } catch (_) { return; }

    if (msg.type === 'auth.ok') {
      _nativeAuthRefreshPending = false;
      _lastRejectedNativeToken = '';
      if (_nativeAuthFallbackTimer) {
        clearTimeout(_nativeAuthFallbackTimer);
        _nativeAuthFallbackTimer = null;
      }
      store.wsState = 'connected';
      _reconnectDelay = 1000;
      if (store.activeBotId) {
        const activeId = store.activeBotId;
        subscribeBotOnWs(activeId).then(() => fetchEventsIncremental(activeId)).catch(() => {});
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
      if (canAskNative && !_nativeAuthRefreshPending && store.token !== _lastRejectedNativeToken) {
        _nativeAuthRefreshPending = true;
        _lastRejectedNativeToken = store.token;
        ws.close();
        try {
          window.AndroidPcCliVoiceChannel.postMessage(JSON.stringify({ action: 'refreshAuth' }));
        } catch (_) {}
        if (_nativeAuthFallbackTimer) clearTimeout(_nativeAuthFallbackTimer);
        _nativeAuthFallbackTimer = setTimeout(() => {
          if (_nativeAuthRefreshPending) {
            _nativeAuthRefreshPending = false;
            store.wsState = 'auth_required';
            showToast('WebSocket 鉴权失败，请更新 Token');
          }
        }, 4500);
        return;
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
    if (store.wsState === 'auth_required' || _nativeAuthRefreshPending) return;
    store.wsState = 'disconnected';
    _reconnectTimer = setTimeout(() => {
      _reconnectDelay = Math.min(_reconnectDelay * 2, 15000);
      connectWs();
    }, _reconnectDelay);
  };
}

export function applyNativeToken(newToken, refreshedOk = true) {
  const cleaned = String(newToken || '').trim();
  if (_nativeAuthFallbackTimer) {
    clearTimeout(_nativeAuthFallbackTimer);
    _nativeAuthFallbackTimer = null;
  }
  const wasWaitingRefresh = _nativeAuthRefreshPending;
  _nativeAuthRefreshPending = false;

  if (!cleaned) {
    saveToken('');
    store.wsState = 'auth_required';
    return;
  }

  const changed = cleaned !== store.token;
  if (changed) {
    saveToken(cleaned);
  }

  if (wasWaitingRefresh) {
    if (!refreshedOk && !changed) {
      store.wsState = 'auth_required';
      showToast('WebSocket 鉴权失败，请重新登录或更新 Token');
      return;
    }
    connectWs();
    return;
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
    if (store.wsState !== 'connected' && store.token) {
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
