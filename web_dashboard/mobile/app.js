/**
 * Lite Agent PC CLI Mobile PWA 根应用 (零构建 Vue 3 ESM)
 */
import { createApp, computed, nextTick, onMounted, onUnmounted, ref, watch } from './vendor/vue.esm-browser.prod.js';
import {
  cancelTurn,
  connectWs,
  createBot,
  deleteBot,
  fetchBots,
  fetchEventsIncremental,
  fetchModels,
  fetchNodeDirs,
  fetchNodes,
  fetchPendingApprovals,
  fetchStats,
  saveToken,
  sendManagerChat,
  sendTurn,
  showToast,
  store,
  submitApproval,
  subscribeBotOnWs,
  updateBot,
} from './store.js?v=5';

function escapeHtml(raw) {
  return String(raw || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function isSafeUrl(url) {
  const trimmed = String(url || '').trim();
  if (!trimmed) return false;
  if (trimmed.startsWith('#') || trimmed.startsWith('/')) return true;
  return /^(https?:|mailto:)/i.test(trimmed);
}

function sanitizeRenderedHtml(html) {
  return String(html || '')
    .replace(/\b(href|src)\s*=\s*"([^"]*)"/gi, (match, attr, val) => {
      if (isSafeUrl(val)) {
        return attr.toLowerCase() === 'href'
          ? `href="${val}" target="_blank" rel="noopener noreferrer"`
          : `${attr}="${val}"`;
      }
      return `${attr}="#"`;
    })
    .replace(/\b(href|src)\s*=\s*'([^']*)'/gi, (match, attr, val) => {
      if (isSafeUrl(val)) {
        return attr.toLowerCase() === 'href'
          ? `href='${val}' target='_blank' rel='noopener noreferrer'`
          : `${attr}='${val}'`;
      }
      return `${attr}='#'`;
    });
}

let _safeMarkdownRenderer = null;

function getSafeMarkdownRenderer() {
  if (_safeMarkdownRenderer) return _safeMarkdownRenderer;
  if (typeof window.marked === 'undefined' || typeof window.marked.Renderer !== 'function') {
    return null;
  }
  const renderer = new window.marked.Renderer();
  // 禁用原始 HTML 直出（防 XSS），同时避免提前 escapeHtml 破坏引用块与代码块转义
  renderer.html = (html) => escapeHtml(typeof html === 'string' ? html : (html && html.text) || '');
  renderer.link = (href, title, text) => {
    const rawHref = typeof href === 'object' && href !== null ? href.href : href;
    const rawText = typeof href === 'object' && href !== null ? href.text : text;
    const url = String(rawHref || '').trim();
    const label = String(rawText || url);
    if (/^file:/i.test(url)) {
      return `<code class="md-file-ref">${label}</code>`;
    }
    if (isSafeUrl(url)) {
      return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
    }
    return `<span>${label}</span>`;
  };
  renderer.image = (href, title, text) => {
    const rawHref = typeof href === 'object' && href !== null ? href.href : href;
    const rawAlt = typeof href === 'object' && href !== null ? href.text : text;
    const url = String(rawHref || '').trim();
    const alt = escapeHtml(rawAlt || '');
    if (isSafeUrl(url)) {
      return `<img src="${escapeHtml(url)}" alt="${alt}" loading="lazy" />`;
    }
    return alt ? `<span>[${alt}]</span>` : '';
  };
  renderer.table = (header, body) =>
    `<div class="md-table-wrap"><table><thead>${header}</thead><tbody>${body}</tbody></table></div>`;
  _safeMarkdownRenderer = renderer;
  return renderer;
}

const _markdownRenderCache = new Map();
const _MARKDOWN_CACHE_LIMIT = 300;

function renderMarkdownSafe(text) {
  const raw = String(text || '');
  const cached = _markdownRenderCache.get(raw);
  if (cached !== undefined) return cached;
  let rendered;
  if (typeof window.marked !== 'undefined' && typeof window.marked.parse === 'function') {
    const renderer = getSafeMarkdownRenderer();
    const opts = { gfm: true, breaks: true };
    if (renderer) opts.renderer = renderer;
    rendered = sanitizeRenderedHtml(window.marked.parse(raw, opts));
  } else {
    rendered = escapeHtml(raw).replace(/\n/g, '<br>');
  }
  if (_markdownRenderCache.size >= _MARKDOWN_CACHE_LIMIT) {
    _markdownRenderCache.clear();
  }
  _markdownRenderCache.set(raw, rendered);
  return rendered;
}

function formatApprovalField(val) {
  if (val === null || val === undefined) return '';
  if (typeof val === 'string') return val.trim();
  if (Array.isArray(val)) {
    if (!val.length) return '';
    if (val.every((x) => typeof x === 'string')) {
      return val
        .map((x) => (/^[A-Za-z0-9_./:@=-]+$/.test(x) ? x : JSON.stringify(x)))
        .join(' ');
    }
    return JSON.stringify(val, null, 2);
  }
  if (typeof val === 'object') {
    return Object.keys(val).length ? JSON.stringify(val, null, 2) : '';
  }
  return String(val).trim();
}

function formatApprovalFullDetail(appr) {
  if (!appr || typeof appr !== 'object') return '';
  const raw = appr.raw_request && typeof appr.raw_request === 'object' ? appr.raw_request : {};
  const tc = raw.toolCall && typeof raw.toolCall === 'object' ? raw.toolCall : {};
  const sections = [];

  const fullTitle = formatApprovalField(tc.title) || String(appr.tool_name || '').trim();
  const rawCmd =
    formatApprovalField(raw.command) ||
    formatApprovalField(tc.rawInput) ||
    formatApprovalField(raw.parsedCmd) ||
    formatApprovalField(raw.changes);
  const summary = String(appr.summary || '').trim();
  const summaryPrefix = summary.replace(/\s*\.\.\.\s*\[(?:truncated|已截断)[^\]]*\]\s*$/i, '').replace(/…$/, '');

  if (fullTitle && fullTitle !== rawCmd) {
    sections.push(fullTitle);
  }
  if (rawCmd) {
    sections.push(rawCmd);
  } else if (summary && summary !== fullTitle && (!summaryPrefix || !fullTitle.startsWith(summaryPrefix))) {
    sections.push(summary);
  }

  const scopeFields = [
    ['执行目录', raw.cwd],
    ['网络目标', raw.networkApprovalContext || raw.network_approval_context],
    ['附加权限', raw.additionalPermissions || raw.additional_permissions],
    ['申请权限', raw.permissions],
    ['授权根目录', raw.grantRoot || raw.writableRoots],
  ];
  for (const [label, val] of scopeFields) {
    const formatted = formatApprovalField(val);
    if (formatted) {
      sections.push(`${label}: ${formatted}`);
    }
  }

  if (typeof raw.reason === 'string' && raw.reason.trim() && !sections.some((s) => s.includes(raw.reason.trim()))) {
    sections.push(`原因: ${raw.reason.trim()}`);
  }
  if (!sections.length && summary) {
    sections.push(summary);
  }
  return sections.join('\n\n');
}

const app = createApp({
  setup() {
    const showCreateDrawer = ref(false);
    const showTokenModal = ref(false);
    const tokenInput = ref(store.token);
    const promptInput = ref('');
    const sending = ref(false);
    const updatingApprovalMode = ref(false);
    const streamEl = ref(null);
    const managerInput = ref('');
    const managerSending = ref(false);
    const managerStreamEl = ref(null);

    const forceDesktop = typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('desktop') === '1';
    const isDesktop = ref(
      typeof window !== 'undefined' ? (forceDesktop || window.innerWidth >= 900) : false
    );
    const handleResize = () => {
      isDesktop.value = forceDesktop || window.innerWidth >= 900;
    };

    // Android 离线语音输入（WebViewCompat.addWebMessageListener 安全消息通道）
    const hasNativeVoice = ref(
      typeof window !== 'undefined' &&
      typeof window.AndroidPcCliVoiceChannel !== 'undefined' &&
      typeof window.AndroidPcCliVoiceChannel.postMessage === 'function'
    );
    const voiceState = ref('idle'); // idle | recording | transcribing
    const voiceTarget = ref(''); // prompt | manager | ''
    const promptInputSource = ref('text');
    let voiceReqSeq = 0;
    let activeVoiceReqId = '';
    let activeVoiceBotId = '';

    // 新建 Bot 表单
    const form = ref({
      node_id: '',
      engine: 'codex',
      cwd: '',
      approval_mode: 'auto',
      execution_mode: 'agent',
      reasoning_effort: '',
      model_select: '',
      model: '',
      name: '',
    });
    const dirEntries = ref([]);
    const dirLoading = ref(false);
    const creating = ref(false);

    const onlineNodes = computed(() => store.nodes.filter((n) => n.status === 'online'));

    const filteredBots = computed(() => {
      if (!store.selectedNodeFilter) return store.bots;
      return store.bots.filter((b) => b.node_id === store.selectedNodeFilter);
    });

    const activeBot = computed(() => store.bots.find((b) => b.bot_id === store.activeBotId) || null);

    const activeBotApprovals = computed(() => {
      if (!store.activeBotId) return [];
      return store.pendingApprovals.filter((a) => a.bot_id === store.activeBotId);
    });

    // 将原始事件流聚合为结构化展示项（把连续 text_delta 合并、把 tool_start/tool_end 配对）
    const activeTimeline = computed(() => {
      if (!store.activeBotId) return [];
      const raw = store.eventsByBot[store.activeBotId] || [];
      const items = [];
      const toolIndexByKey = new Map();

      for (const ev of raw) {
        const p = ev.payload || {};
        if (ev.event_type === 'user_message') {
          items.push({ key: `e-${ev.seq_id}`, kind: 'user', text: p.text || '' });
        } else if (ev.event_type === 'thinking') {
          items.push({ key: `e-${ev.seq_id}`, kind: 'thinking', text: p.text || '' });
        } else if (ev.event_type === 'tool_start') {
          const tkey = `${ev.turn_id}:${p.tool_id || ev.seq_id}`;
          const card = {
            key: `tool-${tkey}`,
            kind: 'tool',
            toolName: p.tool_name || 'tool',
            command: p.command || p.parameters || '',
            output: '',
            done: false,
            isError: false,
          };
          toolIndexByKey.set(tkey, items.length);
          items.push(card);
        } else if (ev.event_type === 'tool_end') {
          const tkey = `${ev.turn_id}:${p.tool_id || ''}`;
          const idx = toolIndexByKey.get(tkey);
          if (idx !== undefined && items[idx]) {
            items[idx].output = p.output || (p.changes ? JSON.stringify(p.changes, null, 2) : '');
            items[idx].done = true;
            items[idx].isError = Boolean(p.is_error);
          } else {
            items.push({
              key: `e-${ev.seq_id}`,
              kind: 'tool',
              toolName: p.tool_name || 'tool',
              command: p.command || '',
              output: p.output || '',
              done: true,
              isError: Boolean(p.is_error),
            });
          }
        } else if (ev.event_type === 'text_delta') {
          const prev = items[items.length - 1];
          if (prev && prev.kind === 'delta' && prev.turnId === ev.turn_id) {
            prev.text += p.delta || '';
          } else {
            items.push({
              key: `delta-${ev.seq_id}`,
              kind: 'delta',
              turnId: ev.turn_id,
              text: p.delta || '',
              html: '',
            });
          }
        } else if (ev.event_type === 'assistant_message') {
          // 若紧跟同一个 turn 的流式 delta，直接替换为最终完整消息避免重复
          const prev = items[items.length - 1];
          if (prev && prev.kind === 'delta' && prev.turnId === ev.turn_id) {
            items.pop();
          }
          items.push({
            key: `e-${ev.seq_id}`,
            kind: 'assistant',
            html: renderMarkdownSafe(p.text || ''),
          });
        } else if (ev.event_type === 'turn_status') {
          items.push({
            key: `e-${ev.seq_id}`,
            kind: 'status',
            status: p.status || '',
            error: p.error || '',
          });
        } else if (ev.event_type === 'error' || ev.event_type === 'warning') {
          items.push({
            key: `e-${ev.seq_id}`,
            kind: 'status',
            status: ev.event_type === 'warning' || p.non_fatal ? 'warning' : 'error',
            error: p.message || '执行异常',
          });
        }
      }
      // 仅对最终保留的流式片段做一次 Markdown 解析，避免增量循环与已被替换片段重复解析
      for (const item of items) {
        if (item.kind === 'delta') {
          item.html = renderMarkdownSafe(item.text);
        }
      }
      return items;
    });

    const selectedNodeEngines = computed(() => {
      const node = store.nodes.find((n) => n.node_id === form.value.node_id);
      return node && node.engines && node.engines.length ? node.engines : ['codex', 'agy'];
    });

    const selectedEngineModels = computed(() => {
      const node = store.nodes.find((n) => n.node_id === form.value.node_id);
      const map = node && node.engine_models && typeof node.engine_models === 'object' ? node.engine_models : {};
      const list = map[form.value.engine];
      return Array.isArray(list) ? list : [];
    });

    const availableReasoningEfforts = computed(() => {
      const defaults = ['low', 'medium', 'high', 'xhigh'];
      const picked = selectedEngineModels.value.find((m) => m.id === form.value.model_select);
      if (picked && Array.isArray(picked.reasoning_efforts) && picked.reasoning_efforts.length) {
        return picked.reasoning_efforts;
      }
      return defaults;
    });

    function executionModeLabel(mode) {
      return {
        agent: '🤖 Agent',
        plan: '📋 Plan',
        chat: '💬 Chat',
      }[mode || 'agent'] || '🤖 Agent';
    }

    function reasoningEffortLabel(effort) {
      return {
        '': '🧠 默认',
        low: '🧠 Low',
        medium: '🧠 Med',
        high: '🧠 High',
        xhigh: '🧠 XHigh',
      }[effort || ''] || `🧠 ${effort}`;
    }

    function reasoningEffortOptionLabel(effort) {
      return {
        low: 'Low (快速)',
        medium: 'Medium (均衡)',
        high: 'High (深度)',
        xhigh: 'XHigh (极限)',
      }[effort] || effort;
    }

    function statusLabel(st) {
      return {
        idle: '空闲',
        running: '运行中',
        waiting_approval: '待审批',
        error: '异常',
      }[st] || st;
    }

    function scrollToBottom() {
      nextTick(() => {
        if (streamEl.value) {
          streamEl.value.scrollTop = streamEl.value.scrollHeight;
        }
      });
    }

    function cancelActiveVoice() {
      const hadActive = Boolean(activeVoiceReqId || voiceState.value !== 'idle');
      activeVoiceReqId = '';
      activeVoiceBotId = '';
      voiceState.value = 'idle';
      voiceTarget.value = '';
      if (hadActive && window.AndroidPcCliVoiceChannel && typeof window.AndroidPcCliVoiceChannel.postMessage === 'function') {
        try {
          window.AndroidPcCliVoiceChannel.postMessage(JSON.stringify({ action: 'cancel' }));
        } catch (_) {}
      }
    }

    async function openBot(botId) {
      if (store.activeBotId !== botId) {
        cancelActiveVoice();
        promptInputSource.value = 'text';
      }
      store.activeBotId = botId;
      window.location.hash = `#bot/${botId}`;
      await subscribeBotOnWs(botId);
      try {
        await fetchEventsIncremental(botId);
        await fetchPendingApprovals(botId);
        scrollToBottom();
      } catch (e) {
        showToast(e.message);
      }
    }

    function backToList() {
      cancelActiveVoice();
      store.activeBotId = null;
      window.location.hash = '';
      subscribeBotOnWs(null);
      fetchBots().catch(() => {});
    }

    function onSwitchBot(evt) {
      const nextId = evt.target.value;
      if (nextId) openBot(nextId);
    }

    async function openCreateDrawer() {
      showCreateDrawer.value = true;
      await fetchNodes().catch(() => {});
      const firstOnline = onlineNodes.value[0];
      if (firstOnline) {
        form.value.node_id = firstOnline.node_id;
        form.value.engine = (firstOnline.engines && firstOnline.engines[0]) || 'codex';
        form.value.cwd = '';
        form.value.model_select = '';
        form.value.model = '';
        form.value.execution_mode = 'agent';
        form.value.reasoning_effort = '';
        await loadDirs(firstOnline.node_id, '');
      }
    }

    async function loadDirs(nodeId, path) {
      if (!nodeId) return;
      dirLoading.value = true;
      try {
        dirEntries.value = await fetchNodeDirs(nodeId, path);
      } catch (e) {
        dirEntries.value = [];
        showToast(e.message);
      } finally {
        dirLoading.value = false;
      }
    }

    async function onPickDir(entry) {
      form.value.cwd = entry.path;
      if (!form.value.name) {
        form.value.name = entry.name;
      }
      await loadDirs(form.value.node_id, entry.path);
    }

    async function onNodeChange() {
      form.value.cwd = '';
      form.value.model_select = '';
      form.value.model = '';
      const engines = selectedNodeEngines.value;
      if (!engines.includes(form.value.engine)) {
        form.value.engine = engines[0] || 'codex';
      }
      await loadDirs(form.value.node_id, '');
    }

    function onEngineChange() {
      form.value.model_select = '';
      form.value.model = '';
      form.value.reasoning_effort = '';
    }

    function onModelSelectChange() {
      if (form.value.model_select !== '__custom__') {
        form.value.model = '';
      }
      if (form.value.reasoning_effort && !availableReasoningEfforts.value.includes(form.value.reasoning_effort)) {
        form.value.reasoning_effort = '';
      }
    }

    async function handleCreateBot() {
      if (!form.value.node_id || !form.value.cwd) {
        showToast('请选择在线 PC 与工作目录');
        return;
      }
      const autoName = form.value.cwd.split(/[\\/]/).filter(Boolean).pop() || 'cli-bot';
      const resolvedModel = form.value.model_select === '__custom__'
        ? form.value.model.trim()
        : form.value.model_select.trim();
      creating.value = true;
      try {
        const bot = await createBot({
          name: (form.value.name || autoName).trim(),
          node_id: form.value.node_id,
          engine: form.value.engine,
          cwd: form.value.cwd.trim(),
          approval_mode: form.value.approval_mode,
          execution_mode: form.value.execution_mode || 'agent',
          reasoning_effort: form.value.reasoning_effort || '',
          model: resolvedModel || null,
        });
        showCreateDrawer.value = false;
        form.value.name = '';
        form.value.model_select = '';
        form.value.model = '';
        form.value.execution_mode = 'agent';
        form.value.reasoning_effort = '';
        showToast(`已创建 Bot: ${bot.name}`);
        await openBot(bot.bot_id);
      } catch (e) {
        showToast(e.message);
      } finally {
        creating.value = false;
      }
    }

    async function handleSendPrompt() {
      const text = promptInput.value.trim();
      if (!text || !store.activeBotId || sending.value || updatingApprovalMode.value) {
        if (updatingApprovalMode.value) {
          showToast('正在切换审批模式，请稍候再发送');
        }
        return;
      }
      sending.value = true;
      const src = promptInputSource.value === 'voice' ? 'voice' : 'text';
      try {
        await sendTurn(store.activeBotId, text, src);
        promptInput.value = '';
        promptInputSource.value = 'text';
        scrollToBottom();
      } catch (e) {
        showToast(e.message);
      } finally {
        sending.value = false;
      }
    }

    function handleVoiceToggle(target) {
      if (!window.AndroidPcCliVoiceChannel || typeof window.AndroidPcCliVoiceChannel.postMessage !== 'function') {
        showToast('请在 Lite Agent Android App 中使用离线语音');
        return;
      }
      const safeTarget = target === 'manager' ? 'manager' : 'prompt';
      if (voiceState.value === 'recording' && activeVoiceReqId) {
        window.AndroidPcCliVoiceChannel.postMessage(JSON.stringify({
          action: 'toggle',
          reqId: activeVoiceReqId,
          target: voiceTarget.value || safeTarget,
          botId: activeVoiceBotId,
        }));
        return;
      }
      const nextReqId = `v_${++voiceReqSeq}_${Date.now()}`;
      const boundBotId = safeTarget === 'prompt' ? String(store.activeBotId || '') : '';
      activeVoiceReqId = nextReqId;
      activeVoiceBotId = boundBotId;
      window.AndroidPcCliVoiceChannel.postMessage(JSON.stringify({
        action: 'toggle',
        reqId: nextReqId,
        target: safeTarget,
        botId: boundBotId,
      }));
    }

    window.__onPcCliVoiceState = (reqId, state, target, message) => {
      if (reqId && activeVoiceReqId && reqId !== activeVoiceReqId) {
        return;
      }
      voiceState.value = state || 'idle';
      voiceTarget.value = state === 'idle' ? '' : (target || '');
      if (state === 'idle') {
        activeVoiceReqId = '';
        activeVoiceBotId = '';
      }
      if (message) {
        showToast(message, state === 'recording' ? 2200 : 3200);
      }
    };

    window.__onPcCliVoiceResult = (reqId, target, botId, text, meta) => {
      if (!reqId || reqId !== activeVoiceReqId) {
        return;
      }
      if (target === 'prompt' && String(botId || '') !== String(store.activeBotId || '')) {
        return;
      }
      const clean = String(text || '').trim();
      if (!clean) return;
      if (target === 'manager') {
        managerInput.value = managerInput.value.trim() ? `${managerInput.value.trim()} ${clean}` : clean;
      } else {
        promptInput.value = promptInput.value.trim() ? `${promptInput.value.trim()} ${clean}` : clean;
        promptInputSource.value = 'voice';
      }
      showToast(meta ? `识别完成 (${meta})` : '离线语音识别完成', 3500);
    };

    async function handleCancel() {
      if (!store.activeBotId) return;
      try {
        const res = await cancelTurn(store.activeBotId);
        showToast(res.forced ? '节点离线，已强制重置状态' : '已下发中止指令');
      } catch (e) {
        showToast(e.message);
      }
    }

    async function handleApproval(approvalId, decision) {
      try {
        const res = await submitApproval(approvalId, decision, 'button');
        const atype = res.approval && res.approval.approval_type;
        if (res.cancel_state === 'cancel_requested') {
          showToast('已请求中止，等待 PC 确认');
        } else if (res.cancel_state === 'hub_marked_cancelled') {
          showToast('Hub 已标记中止，PC 尚未确认');
        } else if (atype === 'bot_delete') {
          showToast(res.executed ? '已确认删除 Bot' : '已拒绝删除 Bot');
        } else if (atype === 'turn_cancel') {
          showToast('已拒绝中止请求');
        } else {
          showToast(res.delivered ? `守护进程已接收审批: ${decision}` : `审批已提交: ${decision}`);
        }
        if (store.activeTab === 'dashboard') {
          fetchStats().catch(() => {});
        }
        fetchBots().catch(() => {});
      } catch (e) {
        showToast(e.message);
        fetchPendingApprovals(store.activeBotId).catch(() => {});
      }
    }

    async function switchTab(tab) {
      if (store.activeTab !== tab) {
        cancelActiveVoice();
      }
      store.activeTab = tab;
      if (tab === 'dashboard') {
        await fetchStats().catch((e) => showToast(e.message));
      } else if (tab === 'manager') {
        await fetchModels().catch(() => {});
      } else {
        await refreshAll();
      }
    }

    async function handleSendManager() {
      const text = managerInput.value.trim();
      if (!text || managerSending.value) return;
      // 先采集已有历史再把本次输入推入 UI，并过滤掉发送失败的旧消息避免污染上下文
      const priorHistory = store.managerMessages
        .filter((m) => (m.role === 'user' || m.role === 'assistant') && !m.failed)
        .slice(-12)
        .map((m) => ({ role: m.role, content: m.text || '' }));
      const userEntry = { key: `u-${Date.now()}`, role: 'user', text, failed: false };
      store.managerMessages.push(userEntry);
      managerInput.value = '';
      managerSending.value = true;
      nextTick(() => {
        if (managerStreamEl.value) managerStreamEl.value.scrollTop = managerStreamEl.value.scrollHeight;
      });
      try {
        const res = await sendManagerChat(text, store.selectedManagerModel, priorHistory);
        store.managerMessages.push({
          key: `a-${Date.now()}`,
          role: 'assistant',
          text: res.reply || '',
          html: renderMarkdownSafe(res.reply || ''),
          model: res.model || '',
          toolTrace: res.tool_trace || [],
          approvals: res.created_approvals || [],
        });
        if (res.created_approvals && res.created_approvals.length) {
          await fetchPendingApprovals().catch(() => {});
        }
        await fetchBots().catch(() => {});
      } catch (e) {
        userEntry.failed = true;
        store.managerMessages.push({
          key: `e-${Date.now()}`,
          role: 'error',
          text: e.message || '总管调用失败',
        });
      } finally {
        managerSending.value = false;
        nextTick(() => {
          if (managerStreamEl.value) managerStreamEl.value.scrollTop = managerStreamEl.value.scrollHeight;
        });
      }
    }

    async function handleToggleApprovalMode(bot) {
      if (!bot || updatingApprovalMode.value || sending.value) return;
      if (bot.status === 'running' || bot.status === 'waiting_approval') {
        showToast('当前回合运行中，请待结束或中止后再切换审批模式');
        return;
      }
      const nextMode = bot.approval_mode === 'ask' ? 'auto' : 'ask';
      updatingApprovalMode.value = true;
      try {
        await updateBot(bot.bot_id, { approval_mode: nextMode });
        showToast(nextMode === 'ask' ? '已切换为需审批 (ask)' : '已切换为自动 (auto)');
      } catch (e) {
        showToast(e.message || '切换审批模式失败');
      } finally {
        updatingApprovalMode.value = false;
      }
    }

    async function handleCycleExecutionMode(bot) {
      if (!bot || updatingApprovalMode.value || sending.value) return;
      if (bot.status === 'running' || bot.status === 'waiting_approval') {
        showToast('当前回合运行中，请待结束或中止后再切换工作模式');
        return;
      }
      const order = ['agent', 'plan', 'chat'];
      const idx = order.indexOf(bot.execution_mode || 'agent');
      const nextMode = order[(idx + 1) % order.length];
      updatingApprovalMode.value = true;
      try {
        await updateBot(bot.bot_id, { execution_mode: nextMode });
        showToast(`已切换工作模式: ${executionModeLabel(nextMode)}`);
      } catch (e) {
        showToast(e.message || '切换工作模式失败');
      } finally {
        updatingApprovalMode.value = false;
      }
    }

    async function handleCycleReasoningEffort(bot) {
      if (!bot || updatingApprovalMode.value || sending.value) return;
      if (bot.status === 'running' || bot.status === 'waiting_approval') {
        showToast('当前回合运行中，请待结束或中止后再切换思考深度');
        return;
      }
      const order = ['', 'low', 'medium', 'high', 'xhigh'];
      const idx = order.indexOf(bot.reasoning_effort || '');
      const nextEffort = order[(idx + 1) % order.length];
      updatingApprovalMode.value = true;
      try {
        await updateBot(bot.bot_id, { reasoning_effort: nextEffort });
        showToast(`已切换思考深度: ${reasoningEffortLabel(nextEffort)}`);
      } catch (e) {
        showToast(e.message || '切换思考深度失败');
      } finally {
        updatingApprovalMode.value = false;
      }
    }

    async function handleResetSession(bot) {
      if (!bot || updatingApprovalMode.value || sending.value) return;
      if (bot.status === 'running' || bot.status === 'waiting_approval') {
        showToast('当前回合运行中，请待结束或中止后再开启新会话');
        return;
      }
      if (!window.confirm(`确认重置 "${bot.name}" 的上下文并开启新会话？（保留 Bot 配置与历史展示记录）`)) return;
      updatingApprovalMode.value = true;
      try {
        await updateBot(bot.bot_id, { reset_session: true });
        showToast('已开启新会话，下一条指令将使用全新上下文');
      } catch (e) {
        showToast(e.message || '开启新会话失败');
      } finally {
        updatingApprovalMode.value = false;
      }
    }

    async function handleDeleteBot(bot) {
      if (!window.confirm(`确认删除 Bot "${bot.name}" 及其全部历史日志？`)) return;
      try {
        await deleteBot(bot.bot_id);
        showToast(`已删除 ${bot.name}`);
        if (isDesktop.value && !store.activeBotId && filteredBots.value.length) {
          await openBot(filteredBots.value[0].bot_id);
        }
      } catch (e) {
        showToast(e.message);
      }
    }

    async function selectDesktopBot(botId) {
      store.activeTab = 'bots';
      if (botId) {
        await openBot(botId);
      }
    }

    function handleSaveToken() {
      saveToken(tokenInput.value);
      showTokenModal.value = false;
      connectWs();
      refreshAll();
    }

    async function refreshAll() {
      try {
        await Promise.all([fetchNodes(), fetchBots(), fetchPendingApprovals(), fetchModels().catch(() => {})]);
      } catch (e) {
        showToast(e.message);
      }
    }

    watch(
      () => activeTimeline.value.length,
      () => scrollToBottom()
    );

    watch(
      () => filteredBots.value,
      (list) => {
        if (isDesktop.value && store.activeTab === 'bots' && !store.activeBotId && list.length) {
          openBot(list[0].bot_id);
        }
      }
    );

    watch(
      () => store.wsState,
      (val) => {
        if (val === 'auth_required') {
          showTokenModal.value = true;
        } else if (val === 'connected') {
          showTokenModal.value = false;
        }
      }
    );

    watch(
      () => store.token,
      (val) => {
        tokenInput.value = val;
      }
    );

    onMounted(async () => {
      window.addEventListener('resize', handleResize);
      await refreshAll();
      connectWs();
      const m = window.location.hash.match(/^#bot\/([^/]+)$/);
      if (m) {
        await openBot(m[1]);
      } else if (isDesktop.value && !store.activeBotId && filteredBots.value.length) {
        await openBot(filteredBots.value[0].bot_id);
      }
    });

    onUnmounted(() => {
      window.removeEventListener('resize', handleResize);
    });

    return {
      store,
      isDesktop,
      selectDesktopBot,
      showCreateDrawer,
      showTokenModal,
      tokenInput,
      promptInput,
      sending,
      streamEl,
      managerInput,
      managerSending,
      managerStreamEl,
      switchTab,
      handleSendManager,
      form,
      dirEntries,
      dirLoading,
      creating,
      onlineNodes,
      filteredBots,
      activeBot,
      activeBotApprovals,
      activeTimeline,
      selectedNodeEngines,
      selectedEngineModels,
      availableReasoningEfforts,
      executionModeLabel,
      reasoningEffortLabel,
      reasoningEffortOptionLabel,
      statusLabel,
      openBot,
      backToList,
      onSwitchBot,
      openCreateDrawer,
      loadDirs,
      onPickDir,
      onNodeChange,
      onEngineChange,
      onModelSelectChange,
      handleCreateBot,
      handleSendPrompt,
      handleVoiceToggle,
      hasNativeVoice,
      voiceState,
      voiceTarget,
      handleCancel,
      handleApproval,
      formatApprovalFullDetail,
      updatingApprovalMode,
      handleToggleApprovalMode,
      handleCycleExecutionMode,
      handleCycleReasoningEffort,
      handleResetSession,
      handleDeleteBot,
      handleSaveToken,
      refreshAll,
      showToast,
    };
  },
});

app.mount('#app');
