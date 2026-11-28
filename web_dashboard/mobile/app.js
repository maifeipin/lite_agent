/**
 * Lite Agent PC CLI Mobile PWA 根应用 (零构建 Vue 3 ESM)
 */
import { createApp, computed, nextTick, onMounted, onUnmounted, ref, watch } from './vendor/vue.esm-browser.prod.js';
import {
  cancelTurn,
  connectWs,
  createBot,
  deleteBot,
  deleteFavorite,
  fetchBotFavorites,
  fetchBots,
  fetchEventsIncremental,
  fetchFavorites,
  fetchModels,
  fetchNodeDirs,
  fetchNodes,
  fetchPendingApprovals,
  fetchStats,
  getCredentialEpoch,
  saveFavorite,
  saveToken,
  searchNotes,
  sendManagerChat,
  sendTurn,
  showToast,
  store,
  submitApproval,
  subscribeBotOnWs,
  updateBot,
  updateNoteRules,
} from './store.js?v=18';

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
    const showPromptModal = ref(false);
    const promptEditBotId = ref('');
    const promptEditBotName = ref('');
    const promptEditText = ref('');
    const promptEditHasSession = ref(false);
    const promptEditResetSession = ref(false);
    const savingPrompt = ref(false);
    const selectFavMode = ref(false);
    const selectedFavKeys = ref([]);
    const showCollectModal = ref(false);
    const collectMode = ref('new'); // new | append
    const collectTargetId = ref('');
    const collectTitle = ref('');
    const collectContent = ref('');
    const collectTags = ref('');
    const collectNote = ref('');
    const collectItems = ref([]);
    const savingFavorite = ref(false);
    const showFavoritesDrawer = ref(false);
    const favSearchQuery = ref('');
    const favScope = ref('all'); // all | current_bot
    const favSearchEngine = ref('');
    const showTokenModal = ref(false);
    const authMode = ref('password'); // password | token
    const loginUsername = ref('');
    const loginPassword = ref('');
    const loginError = ref('');
    const loggingIn = ref(false);
    let loginAttemptSeq = 0;
    const tokenInput = ref(localStorage.getItem('pc_cli_client_token') || '');
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

    const instructionTemplates = [
      {
        key: 'general_dev',
        label: '通用开发规范',
        content: '## 角色定位\n你是项目开发工程师，负责代码排查、修改与验证。\n\n## 约束规则\n- 严禁硬编码密码、Token、API Key 或私密配置。\n- 修改前先阅读相关上下文与现有规范（自动遵循工作目录中的 AGENTS.md）。\n- 改完代码后运行对应测试或语法校验，并简洁汇报改动与结果。',
      },
      {
        key: 'readonly_audit',
        label: '只读巡检专家',
        content: '## 角色定位\n你是只读巡检与分析专家，仅负责核查状态、日志与数据。\n\n## 约束规则\n- 严格只读：禁止修改代码/配置、禁止写库、禁止重启或停止线上服务。\n- 结论先总后分，关键指标与异常项优先用 Markdown 表格和证据行呈现。',
      },
      {
        key: 'bill_analyst',
        label: '账单查询助手',
        content: '## 角色定位\n你是个人财务与账单分析助手，负责查询账单、交易明细与解析健康度。\n\n## 约束规则\n- 仅以只读方式读取账单数据库与视图，严禁修改数据或回显邮箱密码/凭证配置。\n- 汇总金额、币种、异常告警时优先使用表格，并标明时间范围与口径。',
      },
      {
        key: 'ops_debugger',
        label: '安全运维排障',
        content: '## 角色定位\n你是服务器与系统运维排障助手，负责定位故障根因并给出最小化修复方案。\n\n## 约束规则\n- 先查日志、进程、端口与配置确认根因，不做猜测性修改。\n- 涉及服务重启或线上变更前，先说明影响面与回滚方式。',
      },
      {
        key: 'rss_meili_digest',
        label: 'RSS与聚合情报专家',
        content: '## 角色定位\n你是 RSS 热点阅读、Meilisearch 信息聚合与邮件/日程简报专家。\n\n## 上下文与数据源\n- 围绕 `scripts/rss_topic`（热点聚类、主题演进、去重与推送）及 Meilisearch 中的新闻、邮件、待办与日程索引开展检索与提炼。\n\n## 约束规则\n- 默认以只读方式检索和汇总，不随意修改索引数据或触发线上推送脚本；如需回写或重跑先征求确认。\n- 输出按「核心要点 / 热点主题 / 邮件与日程提醒」结构化呈现，优先使用 Markdown 表格与时间线，严禁输出密钥或邮箱密码。',
      },
    ];

    function applyInstructionTemplate(key) {
      const tpl = instructionTemplates.find((t) => t.key === key);
      if (!tpl) return;
      const cur = (form.value.system_prompt || '').trim();
      if (!cur) {
        form.value.system_prompt = tpl.content;
      } else if (!cur.includes(tpl.content)) {
        form.value.system_prompt = `${cur}\n\n${tpl.content}`;
      }
    }

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
      system_prompt: '',
      create_cwd_if_missing: false,
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
        const botIdForKey = ev.bot_id || store.activeBotId || '';
        const sourceKey = `pc_cli:${botIdForKey}:${ev.turn_id || ''}:${ev.seq_id}`;
        if (ev.event_type === 'user_message') {
          items.push({
            key: `e-${ev.seq_id}`,
            kind: 'user',
            text: p.text || '',
            rawText: p.text || '',
            seqId: ev.seq_id,
            turnId: ev.turn_id || '',
            sourceKey,
          });
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
            rawText: p.text || '',
            seqId: ev.seq_id,
            turnId: ev.turn_id || '',
            sourceKey,
          });
        } else if (ev.event_type === 'turn_status') {
          items.push({
            key: `e-${ev.seq_id}`,
            kind: 'status',
            botId: ev.bot_id || store.activeBotId || '',
            turnId: ev.turn_id || '',
            status: p.status || '',
            error: p.error || '',
          });
        } else if (ev.event_type === 'error' || ev.event_type === 'warning') {
          items.push({
            key: `e-${ev.seq_id}`,
            kind: 'status',
            botId: ev.bot_id || store.activeBotId || '',
            turnId: ev.turn_id || '',
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

    const selectedNodePlatform = computed(() => {
      const node = store.nodes.find((n) => n.node_id === form.value.node_id);
      return (node && node.platform) || '';
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
      const startEpoch = getCredentialEpoch();
      if (store.activeBotId !== botId) {
        cancelActiveVoice();
        promptInputSource.value = 'text';
        selectFavMode.value = false;
        selectedFavKeys.value = [];
      }
      store.activeBotId = botId;
      window.location.hash = `#bot/${botId}`;
      fetchBotFavorites(botId).catch(() => {});
      await subscribeBotOnWs(botId);
      if (getCredentialEpoch() !== startEpoch || store.activeBotId !== botId) {
        return;
      }
      try {
        await fetchEventsIncremental(botId);
        if (getCredentialEpoch() !== startEpoch || store.activeBotId !== botId) {
          return;
        }
        await fetchPendingApprovals(botId);
        if (getCredentialEpoch() === startEpoch && store.activeBotId === botId) {
          scrollToBottom();
        }
      } catch (e) {
        if (getCredentialEpoch() === startEpoch) {
          showToast(e.message);
        }
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
      if (selectedNodePlatform.value === 'win32') {
        form.value.create_cwd_if_missing = false;
      }
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
          system_prompt: (form.value.system_prompt || '').trim(),
          create_cwd_if_missing: Boolean(form.value.create_cwd_if_missing),
        });
        showCreateDrawer.value = false;
        form.value.name = '';
        form.value.model_select = '';
        form.value.model = '';
        form.value.execution_mode = 'agent';
        form.value.reasoning_effort = '';
        form.value.system_prompt = '';
        form.value.create_cwd_if_missing = false;
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

    function openPromptModal(bot) {
      if (!bot) return;
      if (bot.status === 'running' || bot.status === 'waiting_approval') {
        showToast('当前回合运行中，请待结束或中止后再编辑角色指令');
        return;
      }
      promptEditBotId.value = bot.bot_id;
      promptEditBotName.value = bot.name || bot.bot_id;
      promptEditText.value = bot.system_prompt || '';
      promptEditHasSession.value = Boolean(bot.native_session_id);
      promptEditResetSession.value = false;
      showPromptModal.value = true;
    }

    function applyEditInstructionTemplate(key) {
      const tpl = instructionTemplates.find((t) => t.key === key);
      if (!tpl) return;
      const cur = (promptEditText.value || '').trim();
      if (!cur) {
        promptEditText.value = tpl.content;
      } else if (!cur.includes(tpl.content)) {
        promptEditText.value = `${cur}\n\n${tpl.content}`;
      }
    }

    async function handleSaveSystemPrompt() {
      const targetId = promptEditBotId.value;
      if (!targetId || savingPrompt.value) return;
      const liveBot = store.bots.find((b) => b.bot_id === targetId);
      if (liveBot && (liveBot.status === 'running' || liveBot.status === 'waiting_approval')) {
        showToast('当前回合运行中，请待结束或中止后再保存');
        return;
      }
      savingPrompt.value = true;
      updatingApprovalMode.value = true;
      try {
        const nextPrompt = (promptEditText.value || '').trim();
        const doReset = Boolean(promptEditHasSession.value && promptEditResetSession.value);
        const payload = { system_prompt: nextPrompt };
        if (doReset) {
          payload.reset_session = true;
        }
        await updateBot(targetId, payload);
        showPromptModal.value = false;
        if (doReset) {
          showToast('已保存角色指令并开启新会话（下一条消息立即生效）');
        } else if (promptEditHasSession.value && nextPrompt) {
          showToast('已保存角色指令（将在下次开启「新会话」时注入）');
        } else {
          showToast(nextPrompt ? '已保存角色指令' : '已清空角色指令');
        }
      } catch (e) {
        showToast(e.message || '保存角色指令失败');
      } finally {
        savingPrompt.value = false;
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

    function copyBotId(botId) {
      if (!botId) return;
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        navigator.clipboard.writeText(botId).then(
          () => showToast(`已复制 Bot ID: ${botId}`),
          () => showToast(`Bot ID: ${botId}`)
        );
      } else {
        showToast(`Bot ID: ${botId}`);
      }
    }

    function handleSaveToken() {
      loginAttemptSeq += 1;
      loggingIn.value = false;
      loginError.value = '';
      if (window.location.hash.startsWith('#bot/')) {
        window.location.hash = '';
      }
      saveToken(tokenInput.value, { source: 'manual', userInitiated: true });
      showTokenModal.value = false;
      connectWs();
      refreshAll();
    }

    async function handlePasswordLogin() {
      const username = loginUsername.value.trim();
      const password = loginPassword.value.trim();
      if (!username || !password || loggingIn.value) return;
      const attemptId = ++loginAttemptSeq;
      const startEpoch = getCredentialEpoch();
      loginError.value = '';
      loggingIn.value = true;
      try {
        const resp = await fetch('/agent/api/v1/auth', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username,
            password,
            device_name: isDesktop.value ? 'web-pc-bots' : 'mobile-pwa',
            remember_me: true,
          }),
        });
        const data = await resp.json().catch(() => ({}));
        if (attemptId !== loginAttemptSeq || getCredentialEpoch() !== startEpoch) {
          return;
        }
        const tok = data.session_token || data.access_token || '';
        if (!resp.ok || !data.success || !tok) {
          loginError.value = data.error || '账号或密码错误';
          return;
        }
        if (window.location.hash.startsWith('#bot/')) {
          window.location.hash = '';
        }
        saveToken(tok, {
          source: 'session',
          refreshToken: data.refresh_token || '',
          expiresAt: data.access_token_expires_at || data.expires_at || '',
          userInitiated: true,
        });
        loginPassword.value = '';
        showTokenModal.value = false;
        showToast('登录成功，正在连接...');
        connectWs();
        await refreshAll();
      } catch (e) {
        if (attemptId === loginAttemptSeq && getCredentialEpoch() === startEpoch) {
          loginError.value = (e && e.message) || '网络错误，请稍后重试';
        }
      } finally {
        if (attemptId === loginAttemptSeq) {
          loggingIn.value = false;
        }
      }
    }

    async function refreshAll() {
      try {
        await Promise.all([fetchNodes(), fetchBots(), fetchPendingApprovals(), fetchModels().catch(() => {})]);
      } catch (e) {
        showToast(e.message);
      }
    }

    function isMessageCollected(item) {
      return Boolean(item && item.sourceKey && store.collectedSourceKeys.includes(item.sourceKey));
    }

    function toggleSelectFavMode() {
      selectFavMode.value = !selectFavMode.value;
      if (!selectFavMode.value) {
        selectedFavKeys.value = [];
      }
    }

    function isSelectedFav(item) {
      return Boolean(item && item.sourceKey && selectedFavKeys.value.includes(item.sourceKey));
    }

    function toggleSelectFavItem(item) {
      if (!item || !item.sourceKey) return;
      const idx = selectedFavKeys.value.indexOf(item.sourceKey);
      if (idx >= 0) {
        selectedFavKeys.value.splice(idx, 1);
      } else {
        selectedFavKeys.value.push(item.sourceKey);
      }
    }

    function _buildDefaultCollectTitle(items) {
      for (const it of items) {
        const txt = String(it.text || '').trim();
        if (txt) {
          const first = txt.split(/\r?\n/)[0].replace(/^[#*\-\s>]+/, '').trim();
          if (first) return first.slice(0, 60);
        }
      }
      return (activeBot.value && activeBot.value.name) ? `${activeBot.value.name} 收藏` : 'PC Bot 收藏';
    }

    async function _openCollectModalWithItems(items, overrideContent = '') {
      if (!activeBot.value || !items.length) return;
      collectItems.value = items;
      collectTitle.value = _buildDefaultCollectTitle(items);
      if (overrideContent) {
        collectContent.value = overrideContent;
      } else {
        collectContent.value = items
          .map((it) => (it.role === 'user' ? `**用户提问：**\n${it.text}` : it.text))
          .join('\n\n');
      }
      collectTags.value = '';
      collectNote.value = '';
      collectMode.value = 'new';
      collectTargetId.value = '';
      showCollectModal.value = true;
      try {
        const list = await fetchBotFavorites(activeBot.value.bot_id);
        if (list.length) {
          collectTargetId.value = list[0].id;
        }
      } catch (_) {}
    }

    async function openCollectForSingle(item) {
      if (!item || !activeBot.value) return;
      let selectedExcerpt = '';
      try {
        const sel = window.getSelection ? window.getSelection().toString().trim() : '';
        if (sel && sel.length >= 6 && item.rawText && item.rawText.includes(sel)) {
          selectedExcerpt = sel;
        }
      } catch (_) {}
      const payloadItem = {
        source_key: item.sourceKey,
        turn_id: item.turnId || '',
        seq_id: item.seqId || 0,
        role: item.kind === 'user' ? 'user' : 'assistant',
        text: selectedExcerpt || item.rawText || '',
      };
      await _openCollectModalWithItems([payloadItem], selectedExcerpt);
    }

    async function openCollectForSelected() {
      if (!activeBot.value || !selectedFavKeys.value.length) return;
      const pickedSet = new Set(selectedFavKeys.value);
      const ordered = activeTimeline.value
        .filter((it) => (it.kind === 'user' || it.kind === 'assistant') && pickedSet.has(it.sourceKey))
        .map((it) => ({
          source_key: it.sourceKey,
          turn_id: it.turnId || '',
          seq_id: it.seqId || 0,
          role: it.kind === 'user' ? 'user' : 'assistant',
          text: it.rawText || '',
        }));
      if (!ordered.length) {
        showToast('请先勾选至少一条提问或回复');
        return;
      }
      await _openCollectModalWithItems(ordered);
    }

    async function handleSaveFavorite() {
      if (!activeBot.value || savingFavorite.value) return;
      const content = collectContent.value.trim();
      if (!content) {
        showToast('收藏正文不能为空');
        return;
      }
      if (collectMode.value === 'append' && !collectTargetId.value) {
        showToast('请选择要追加的已有收藏');
        return;
      }
      savingFavorite.value = true;
      try {
        const res = await saveFavorite({
          bot_id: activeBot.value.bot_id,
          bot_name: activeBot.value.name || '',
          node_id: activeBot.value.node_id || '',
          cwd: activeBot.value.cwd || '',
          title: collectTitle.value.trim(),
          content,
          tags: collectTags.value.trim(),
          note: collectNote.value.trim(),
          items: collectItems.value,
          append_to_id: collectMode.value === 'append' ? collectTargetId.value : '',
        });
        showCollectModal.value = false;
        selectFavMode.value = false;
        selectedFavKeys.value = [];
        await fetchBotFavorites(activeBot.value.bot_id).catch(() => {});
        if (showFavoritesDrawer.value) {
          await handleSearchFavorites();
        }
        if (res.deduplicated) {
          showToast('该条内容已在收藏中，未重复创建');
        } else if (res.appended) {
          showToast(res.meili_synced ? '已增量追加到收藏并同步索引' : '已追加到收藏（本地库已保存）');
        } else {
          showToast(res.meili_synced ? '已收藏到知识库并同步索引' : '已收藏到知识库（本地库已保存）');
        }
      } catch (e) {
        showToast(e.message);
      } finally {
        savingFavorite.value = false;
      }
    }

    async function openFavoritesDrawer() {
      showFavoritesDrawer.value = true;
      if (kbDrawerTab.value === 'notes') {
        await handleSearchNotes();
      } else {
        await handleSearchFavorites();
      }
    }

    const kbDrawerTab = ref('favorites'); // favorites | notes
    const noteSearchQuery = ref('');
    const noteSearchError = ref('');

    async function handleSearchNotes() {
      noteSearchError.value = '';
      try {
        const res = await searchNotes({ q: noteSearchQuery.value.trim(), limit: 25, purpose: 'search' });
        if (res && res.error) {
          noteSearchError.value = String(res.error);
        }
      } catch (e) {
        noteSearchError.value = e.message || '检索私有笔记失败';
        showToast(noteSearchError.value);
      }
    }

    async function handleToggleNoteNoQuote(note) {
      if (!note || !note.note_id) return;
      const nextVal = !note.exclude_from_quote;
      try {
        await updateNoteRules(note.note_id, { exclude_from_quote: nextVal });
        note.exclude_from_quote = nextVal;
        showToast(nextVal ? '已设为「排除引用」（本人仍可检索）' : '已恢复允许引用');
      } catch (e) {
        showToast(e.message || '更新规则失败');
      }
    }

    async function handleExcludeNoteFromIndex(note) {
      if (!note || !note.note_id) return;
      if (!window.confirm(`确认将「${note.title}」设为排除索引？将立即从 Meilisearch 移除且不再返回。`)) return;
      try {
        await updateNoteRules(note.note_id, { exclude_from_index: true });
        store.notes = store.notes.filter((n) => n.note_id !== note.note_id);
        showToast('已排除索引并从检索中移除');
      } catch (e) {
        showToast(e.message || '排除索引失败');
      }
    }

    function quoteNoteToInput(note) {
      if (!note) return;
      if (note.exclude_from_quote) {
        showToast('该笔记已标记排除引用');
        return;
      }
      const meta = [note.folder_path, note.url].filter(Boolean).join(' · ');
      const quoteBlock = `【引用笔记：${note.title}】${meta ? `（出处：${meta}）` : ''}\n${note.snippet || ''}\n\n`;
      promptInput.value = promptInput.value.trim()
        ? `${promptInput.value.trim()}\n\n${quoteBlock}`
        : quoteBlock;
      showFavoritesDrawer.value = false;
      showToast('已插入输入框，确认后发送');
    }

    async function handleSearchFavorites() {
      try {
        const targetBotId = favScope.value === 'current_bot' && store.activeBotId ? store.activeBotId : '';
        const res = await fetchFavorites({ q: favSearchQuery.value.trim(), botId: targetBotId, limit: 40 });
        favSearchEngine.value = res.engine || '';
      } catch (e) {
        showToast(e.message);
      }
    }

    function isFavoriteBotAlive(fav) {
      return Boolean(fav && fav.bot_id && store.bots.some((b) => b.bot_id === fav.bot_id));
    }

    function quoteFavoriteToInput(fav) {
      if (!fav) return;
      const meta = [fav.bot_name || fav.bot_id, fav.cwd].filter(Boolean).join(' · ');
      const quoteBlock = `【引用收藏：${fav.title}】${meta ? `（出处：${meta}）` : ''}\n${fav.content}\n\n`;
      promptInput.value = promptInput.value.trim()
        ? `${promptInput.value.trim()}\n\n${quoteBlock}`
        : quoteBlock;
      showFavoritesDrawer.value = false;
      showToast('已插入输入框，确认后发送');
    }

    async function jumpToFavoriteBot(fav) {
      if (!isFavoriteBotAlive(fav)) {
        showToast('原 Bot 已删除，收藏副本已独立保留');
        return;
      }
      showFavoritesDrawer.value = false;
      store.activeTab = 'bots';
      await openBot(fav.bot_id);
    }

    async function handleDeleteFavorite(fav) {
      if (!fav || !fav.id) return;
      if (!window.confirm(`确认取消收藏「${fav.title}」？`)) return;
      try {
        await deleteFavorite(fav.id);
        if (store.activeBotId) {
          await fetchBotFavorites(store.activeBotId).catch(() => {});
        }
        showToast('已取消收藏并从检索移除');
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
      () => {
        tokenInput.value = localStorage.getItem('pc_cli_client_token') || '';
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
      copyBotId,
      showCreateDrawer,
      showPromptModal,
      promptEditBotName,
      promptEditText,
      promptEditHasSession,
      promptEditResetSession,
      savingPrompt,
      openPromptModal,
      applyEditInstructionTemplate,
      handleSaveSystemPrompt,
      showTokenModal,
      authMode,
      loginUsername,
      loginPassword,
      loginError,
      loggingIn,
      tokenInput,
      promptInput,
      sending,
      streamEl,
      managerInput,
      managerSending,
      managerStreamEl,
      switchTab,
      handleSendManager,
      instructionTemplates,
      applyInstructionTemplate,
      form,
      dirEntries,
      dirLoading,
      creating,
      onlineNodes,
      filteredBots,
      activeBot,
      activeBotApprovals,
      activeTimeline,
      selectedNodePlatform,
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
      handlePasswordLogin,
      selectFavMode,
      selectedFavKeys,
      showCollectModal,
      collectMode,
      collectTargetId,
      collectTitle,
      collectContent,
      collectTags,
      collectNote,
      collectItems,
      savingFavorite,
      showFavoritesDrawer,
      favSearchQuery,
      favScope,
      favSearchEngine,
      isMessageCollected,
      toggleSelectFavMode,
      isSelectedFav,
      toggleSelectFavItem,
      openCollectForSingle,
      openCollectForSelected,
      handleSaveFavorite,
      openFavoritesDrawer,
      handleSearchFavorites,
      isFavoriteBotAlive,
      quoteFavoriteToInput,
      jumpToFavoriteBot,
      handleDeleteFavorite,
      kbDrawerTab,
      noteSearchQuery,
      noteSearchError,
      handleSearchNotes,
      handleToggleNoteNoQuote,
      handleExcludeNoteFromIndex,
      quoteNoteToInput,
      refreshAll,
      showToast,
    };
  },
});

app.mount('#app');
