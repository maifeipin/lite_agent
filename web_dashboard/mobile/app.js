/**
 * Lite Agent PC CLI Mobile PWA 根应用 (零构建 Vue 3 ESM)
 */
import { createApp, computed, nextTick, onMounted, ref, watch } from './vendor/vue.esm-browser.prod.js';
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
} from './store.js?v=2';

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

function renderMarkdownSafe(text) {
  const escaped = escapeHtml(text);
  if (typeof window.marked !== 'undefined' && typeof window.marked.parse === 'function') {
    return sanitizeRenderedHtml(window.marked.parse(escaped, { breaks: true }));
  }
  return escaped.replace(/\n/g, '<br>');
}

const app = createApp({
  setup() {
    const showCreateDrawer = ref(false);
    const showTokenModal = ref(false);
    const tokenInput = ref(store.token);
    const promptInput = ref('');
    const sending = ref(false);
    const streamEl = ref(null);
    const managerInput = ref('');
    const managerSending = ref(false);
    const managerStreamEl = ref(null);

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
            items.push({ key: `delta-${ev.seq_id}`, kind: 'delta', turnId: ev.turn_id, text: p.delta || '' });
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
      return items;
    });

    const selectedNodeEngines = computed(() => {
      const node = store.nodes.find((n) => n.node_id === form.value.node_id);
      return node && node.engines && node.engines.length ? node.engines : ['codex', 'agy'];
    });

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
      const engines = selectedNodeEngines.value;
      if (!engines.includes(form.value.engine)) {
        form.value.engine = engines[0] || 'codex';
      }
      await loadDirs(form.value.node_id, '');
    }

    async function handleCreateBot() {
      if (!form.value.node_id || !form.value.cwd) {
        showToast('请选择在线 PC 与工作目录');
        return;
      }
      const autoName = form.value.cwd.split(/[\\/]/).filter(Boolean).pop() || 'cli-bot';
      creating.value = true;
      try {
        const bot = await createBot({
          name: (form.value.name || autoName).trim(),
          node_id: form.value.node_id,
          engine: form.value.engine,
          cwd: form.value.cwd.trim(),
          approval_mode: form.value.approval_mode,
          model: form.value.model.trim() || null,
        });
        showCreateDrawer.value = false;
        form.value.name = '';
        form.value.model = '';
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
      if (!text || !store.activeBotId || sending.value) return;
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
      // 先采集已有历史再把本次输入推入 UI，避免后端收到重复的当前用户消息
      const priorHistory = store.managerMessages
        .filter((m) => m.role === 'user' || m.role === 'assistant')
        .slice(-12)
        .map((m) => ({ role: m.role, content: m.text || '' }));
      store.managerMessages.push({ key: `u-${Date.now()}`, role: 'user', text });
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

    async function handleDeleteBot(bot) {
      if (!window.confirm(`确认删除 Bot "${bot.name}" 及其全部历史日志？`)) return;
      try {
        await deleteBot(bot.bot_id);
        showToast(`已删除 ${bot.name}`);
      } catch (e) {
        showToast(e.message);
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
      () => store.wsState,
      (val) => {
        if (val === 'auth_required') {
          showTokenModal.value = true;
        }
      }
    );

    onMounted(async () => {
      await refreshAll();
      connectWs();
      const m = window.location.hash.match(/^#bot\/([^/]+)$/);
      if (m) {
        await openBot(m[1]);
      }
    });

    return {
      store,
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
      statusLabel,
      openBot,
      backToList,
      onSwitchBot,
      openCreateDrawer,
      loadDirs,
      onPickDir,
      onNodeChange,
      handleCreateBot,
      handleSendPrompt,
      handleVoiceToggle,
      hasNativeVoice,
      voiceState,
      voiceTarget,
      handleCancel,
      handleApproval,
      handleDeleteBot,
      handleSaveToken,
      refreshAll,
      showToast,
    };
  },
});

app.mount('#app');
