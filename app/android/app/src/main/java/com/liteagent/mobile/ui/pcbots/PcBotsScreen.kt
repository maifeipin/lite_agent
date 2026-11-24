package com.liteagent.mobile.ui.pcbots

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.view.ViewGroup
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.google.gson.Gson
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.core.voice.OfflineAsrEngine
import com.liteagent.mobile.core.voice.OfflineVoiceModelManager
import com.liteagent.mobile.core.voice.OfflineVoiceResult
import com.liteagent.mobile.ui.theme.PrimaryIndigo
import com.liteagent.mobile.ui.theme.SurfaceElevated
import com.liteagent.mobile.ui.theme.TextSecondary
import kotlinx.coroutines.launch
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.util.Locale

private val pcBotsGson = Gson()
private const val VOICE_WEB_MESSAGE_CHANNEL = "AndroidPcCliVoiceChannel"

internal data class PcBotsAuthSnapshot(
    val serverUrl: String,
    val targetUrl: String,
    val token: String
)

private data class VoiceBridgeRequest(
    val action: String? = null,
    val reqId: String? = null,
    val target: String? = null,
    val botId: String? = null
)

private data class ActiveVoiceSession(
    val reqId: String,
    val target: String, // prompt | manager | offline
    val botId: String
)

internal fun buildMobilePwaUrl(serverUrl: String): String {
    val base = ServerUrlNormalizer.normalize(serverUrl).ifBlank {
        ServerUrlNormalizer.normalize(SecureTokenStorage.DEFAULT_SERVER_URL)
    }
    return "$base/mobile/"
}

internal fun extractOriginRule(targetUrl: String): String? {
    val u = targetUrl.toHttpUrlOrNull() ?: return null
    val scheme = u.scheme.lowercase()
    val host = u.host.lowercase()
    val defaultPort = if (scheme == "https") 443 else 80
    val hostPart = if (host.contains(":")) "[$host]" else host
    return if (u.port == defaultPort) "$scheme://$hostPart" else "$scheme://$hostPart:${u.port}"
}

internal fun isSameOrigin(url: String?, targetUrl: String): Boolean {
    if (url.isNullOrBlank() || targetUrl.isBlank()) return false
    val u1 = url.toHttpUrlOrNull() ?: return false
    val u2 = targetUrl.toHttpUrlOrNull() ?: return false
    return u1.scheme.equals(u2.scheme, ignoreCase = true) &&
        u1.host.equals(u2.host, ignoreCase = true) &&
        u1.port == u2.port
}

internal fun resolveTokenForUrl(url: String?, snapshot: PcBotsAuthSnapshot?): String? {
    if (snapshot == null) return null
    return if (isSameOrigin(url, snapshot.targetUrl)) snapshot.token else null
}

internal fun buildTokenInjectionScript(token: String, refreshedOk: Boolean = true): String {
    val quoted = pcBotsGson.toJson(token)
    val okLiteral = if (refreshedOk) "true" else "false"
    return """
        (function() {
          try {
            var tok = $quoted;
            if (typeof window.__onPcCliNativeToken === 'function') {
              window.__onPcCliNativeToken(tok, $okLiteral);
              return;
            }
            var key = 'pc_cli_client_token';
            var prev = window.localStorage.getItem(key) || '';
            if (tok) {
              if (prev !== tok) {
                window.localStorage.setItem(key, tok);
                window.location.reload();
              }
            } else if (prev) {
              window.localStorage.removeItem(key);
              window.location.reload();
            }
          } catch (e) {}
        })();
    """.trimIndent()
}

internal fun buildVoiceStateDispatchScript(reqId: String, state: String, target: String, message: String): String {
    val r = pcBotsGson.toJson(reqId)
    val s = pcBotsGson.toJson(state)
    val t = pcBotsGson.toJson(target)
    val m = pcBotsGson.toJson(message)
    return "if (window.__onPcCliVoiceState) { window.__onPcCliVoiceState($r, $s, $t, $m); }"
}

internal fun buildVoiceResultDispatchScript(
    reqId: String,
    target: String,
    botId: String,
    text: String,
    meta: String
): String {
    val r = pcBotsGson.toJson(reqId)
    val t = pcBotsGson.toJson(target)
    val b = pcBotsGson.toJson(botId)
    val txt = pcBotsGson.toJson(text)
    val m = pcBotsGson.toJson(meta)
    return "if (window.__onPcCliVoiceResult) { window.__onPcCliVoiceResult($r, $t, $b, $txt, $m); }"
}

@SuppressLint("SetJavaScriptEnabled")
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PcBotsScreen(
    tokenStorage: SecureTokenStorage,
    apiClient: ApiClient? = null,
    onOpenDrawer: () -> Unit = {},
    onToggleQuickBar: () -> Unit = {},
    onOpenSettings: () -> Unit = {},
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val clipboardManager = LocalClipboardManager.current
    val scope = rememberCoroutineScope()

    val activeServer by tokenStorage.activeServerUrlFlow.collectAsState(initial = SecureTokenStorage.DEFAULT_SERVER_URL)
    val session by tokenStorage.sessionFlow.collectAsState(initial = null)
    val displayTargetUrl = remember(activeServer) { buildMobilePwaUrl(activeServer) }

    // 离线语音模型管理器与 Sherpa-ONNX 引擎
    val modelManager = remember(context.applicationContext) { OfflineVoiceModelManager(context.applicationContext) }
    val asrEngine = remember(modelManager) { OfflineAsrEngine(modelManager) }
    val selectedVoiceModel by modelManager.selectedModel.collectAsState()
    var showVoiceModelSheet by remember { mutableStateOf(false) }
    var lastVoiceResult by remember { mutableStateOf<OfflineVoiceResult?>(null) }
    var activeVoiceSession by remember { mutableStateOf<ActiveVoiceSession?>(null) }
    var pendingPermissionSession by remember { mutableStateOf<ActiveVoiceSession?>(null) }

    // 飞行模式离线兜底草稿箱状态
    var offlineDraftText by remember { mutableStateOf("") }
    var offlineVoiceStatus by remember { mutableStateOf("") }
    var offlineRecording by remember { mutableStateOf(false) }
    var offlineTranscribing by remember { mutableStateOf(false) }

    // 将服务器地址、目标 URL 与对应 Token 绑定为原子快照：切换服务器时立即清空，严禁把旧服 Token 注入新服
    var readySnapshot by remember { mutableStateOf<PcBotsAuthSnapshot?>(null) }

    suspend fun syncAuthSnapshot(serverUrl: String, forceRefresh: Boolean = false): PcBotsAuthSnapshot {
        val normNew = ServerUrlNormalizer.normalize(serverUrl)
        val normPrev = readySnapshot?.serverUrl?.let { ServerUrlNormalizer.normalize(it) }
        if (normPrev != null && normPrev != normNew) {
            readySnapshot = null
        }
        try {
            apiClient?.ensureFreshSession(serverUrl, force = forceRefresh)
        } catch (_: Exception) {
        }
        val targetForServer = buildMobilePwaUrl(serverUrl)
        val loadedToken = try {
            tokenStorage.getGeneralToken(serverUrl)
        } catch (_: Exception) {
            ""
        }
        val snap = PcBotsAuthSnapshot(
            serverUrl = serverUrl,
            targetUrl = targetForServer,
            token = loadedToken
        )
        readySnapshot = snap
        return snap
    }

    LaunchedEffect(activeServer, session?.sessionId, session?.sessionToken) {
        syncAuthSnapshot(activeServer)
    }

    // 长时间停留在 PC Bots 页面（>1 小时）时，每 15 分钟静默预检续期并在页面内热更新 Token（不触发整页 reload）
    LaunchedEffect(activeServer) {
        while (true) {
            kotlinx.coroutines.delay(15 * 60_000L)
            syncAuthSnapshot(activeServer)
        }
    }

    val currentSnapshotState = rememberUpdatedState(readySnapshot)
    val currentServerState = rememberUpdatedState(activeServer)

    var webViewRef by remember { mutableStateOf<WebView?>(null) }
    var registeredChannelTarget by remember { mutableStateOf<Pair<WebView, String>?>(null) }
    var loadProgress by remember { mutableIntStateOf(0) }
    var isLoading by remember { mutableStateOf(true) }
    var canGoBack by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    var lastLoadedTargetUrl by remember { mutableStateOf("") }

    fun dispatchVoiceState(sess: ActiveVoiceSession, state: String, message: String = "") {
        if (sess.target == "offline") {
            offlineRecording = (state == "recording")
            offlineTranscribing = (state == "transcribing")
            if (message.isNotBlank()) offlineVoiceStatus = message
            return
        }
        val wv = webViewRef ?: return
        wv.post {
            val snap = currentSnapshotState.value
            if (snap != null && isSameOrigin(wv.url, snap.targetUrl)) {
                wv.evaluateJavascript(
                    buildVoiceStateDispatchScript(sess.reqId, state, sess.target, message),
                    null
                )
            }
        }
    }

    fun dispatchVoiceResult(sess: ActiveVoiceSession, text: String, meta: String) {
        if (sess.target == "offline") {
            offlineDraftText = if (offlineDraftText.isBlank()) text else "${offlineDraftText.trim()} $text"
            offlineVoiceStatus = "识别完成 ($meta)"
            return
        }
        val wv = webViewRef ?: return
        wv.post {
            val snap = currentSnapshotState.value
            if (snap != null && isSameOrigin(wv.url, snap.targetUrl)) {
                wv.evaluateJavascript(
                    buildVoiceResultDispatchScript(sess.reqId, sess.target, sess.botId, text, meta),
                    null
                )
            }
        }
    }

    fun stopAndRunTranscription(sess: ActiveVoiceSession) {
        val spec = modelManager.selectedModel.value
        dispatchVoiceState(sess, "transcribing", "本地识别中 (${spec.displayName.substringBefore("（")})...")
        scope.launch {
            val res = asrEngine.stopAndTranscribe(spec)
            res.onSuccess { vr ->
                lastVoiceResult = vr
                val meta = String.format(
                    Locale.US,
                    "%s · %.2fs (RTF %.2f)",
                    vr.modelId.displayName.substringBefore("（"),
                    vr.inferenceSec,
                    vr.rtf
                )
                if (activeVoiceSession?.reqId == sess.reqId) {
                    if (vr.text.isBlank()) {
                        dispatchVoiceState(sess, "idle", "未识别到有效语音内容")
                    } else {
                        dispatchVoiceResult(sess, vr.text, meta)
                        dispatchVoiceState(sess, "idle", "")
                    }
                    activeVoiceSession = null
                }
            }.onFailure { err ->
                if (activeVoiceSession?.reqId == sess.reqId) {
                    dispatchVoiceState(sess, "idle", err.message ?: "离线语音识别失败")
                    activeVoiceSession = null
                }
            }
        }
    }

    fun beginRecording(sess: ActiveVoiceSession) {
        val spec = modelManager.selectedModel.value
        if (!modelManager.isInstalled(spec)) {
            showVoiceModelSheet = true
            dispatchVoiceState(sess, "idle", "请先下载离线语音模型")
            return
        }
        activeVoiceSession = sess
        val startRes = asrEngine.startRecording(
            onAutoStop = {
                stopAndRunTranscription(sess)
            }
        )
        startRes.onSuccess {
            dispatchVoiceState(sess, "recording", "正在聆听（${spec.displayName.substringBefore("（")}）...")
        }.onFailure { err ->
            activeVoiceSession = null
            dispatchVoiceState(sess, "idle", err.message ?: "启动录音失败")
        }
    }

    val recordAudioPermissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestPermission()
    ) { granted ->
        val pending = pendingPermissionSession
        pendingPermissionSession = null
        if (granted && pending != null) {
            beginRecording(pending)
        } else if (pending != null) {
            dispatchVoiceState(pending, "idle", "未授予麦克风权限，无法语音输入")
        }
    }

    fun handleVoiceToggle(reqId: String, target: String, botId: String) {
        val safeTarget = when (target) {
            "manager", "offline" -> target
            else -> "prompt"
        }
        val current = activeVoiceSession
        if (asrEngine.isRecording && current != null) {
            stopAndRunTranscription(current)
            return
        }
        val nextSession = ActiveVoiceSession(
            reqId = reqId.ifBlank { "native_${System.currentTimeMillis()}" },
            target = safeTarget,
            botId = botId
        )
        val granted = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            pendingPermissionSession = nextSession
            recordAudioPermissionLauncher.launch(Manifest.permission.RECORD_AUDIO)
            return
        }
        beginRecording(nextSession)
    }

    fun cancelActiveVoiceSession() {
        val cur = activeVoiceSession
        activeVoiceSession = null
        asrEngine.cancelRecording()
        if (cur != null) {
            dispatchVoiceState(cur, "idle", "")
        }
    }

    fun registerVoiceWebMessageListener(wv: WebView, targetUrl: String) {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            return
        }
        val originRule = extractOriginRule(targetUrl) ?: return
        if (registeredChannelTarget?.first === wv && registeredChannelTarget?.second == originRule) return
        try {
            WebViewCompat.removeWebMessageListener(wv, VOICE_WEB_MESSAGE_CHANNEL)
        } catch (_: Exception) {
        }
        WebViewCompat.addWebMessageListener(
            wv,
            VOICE_WEB_MESSAGE_CHANNEL,
            setOf(originRule)
        ) { view, message, sourceOrigin, isMainFrame, _ ->
            // 仅接受可信主 Frame + 严格同源调用，彻底阻断 iframe 滥用
            if (!isMainFrame) return@addWebMessageListener
            val snap = currentSnapshotState.value ?: return@addWebMessageListener
            if (!isSameOrigin(sourceOrigin.toString(), snap.targetUrl)) return@addWebMessageListener
            val raw = message.data ?: return@addWebMessageListener
            val req = try {
                pcBotsGson.fromJson(raw, VoiceBridgeRequest::class.java)
            } catch (_: Exception) {
                null
            } ?: return@addWebMessageListener
            view.post {
                when (req.action) {
                    "toggle" -> handleVoiceToggle(
                        reqId = req.reqId.orEmpty(),
                        target = req.target.orEmpty(),
                        botId = req.botId.orEmpty()
                    )
                    "cancel" -> cancelActiveVoiceSession()
                    "openModels" -> showVoiceModelSheet = true
                    "refreshAuth" -> {
                        scope.launch {
                            val targetSrv = currentServerState.value
                            var refreshedOk = false
                            try {
                                refreshedOk = apiClient?.ensureFreshSession(targetSrv, force = true) ?: false
                            } catch (_: Exception) {
                            }
                            val snap = syncAuthSnapshot(targetSrv, forceRefresh = false)
                            val tok = resolveTokenForUrl(view.url, snap)
                            if (tok != null) {
                                view.evaluateJavascript(buildTokenInjectionScript(tok, refreshedOk = refreshedOk), null)
                            }
                        }
                    }
                }
            }
        }
        registeredChannelTarget = wv to originRule
    }

    // 退到后台时自动取消录音并在锁保护下释放 Native 模型内存；回到前台时主动检查并续期即将过期的会话 Token
    DisposableEffect(lifecycleOwner, activeServer) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_STOP) {
                if (asrEngine.isRecording) {
                    cancelActiveVoiceSession()
                }
                scope.launch {
                    asrEngine.releaseModel()
                }
            } else if (event == Lifecycle.Event.ON_RESUME) {
                scope.launch {
                    syncAuthSnapshot(activeServer)
                }
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
        }
    }

    BackHandler(enabled = canGoBack) {
        webViewRef?.goBack()
    }

    // 仅在当前服务器的原子快照就绪后，才绑定同源消息通道、加载目标页面或同步最新 Token
    LaunchedEffect(readySnapshot) {
        val snap = readySnapshot ?: return@LaunchedEffect
        val wv = webViewRef ?: return@LaunchedEffect
        registerVoiceWebMessageListener(wv, snap.targetUrl)
        if (lastLoadedTargetUrl != snap.targetUrl) {
            errorMessage = null
            lastLoadedTargetUrl = snap.targetUrl
            wv.loadUrl(snap.targetUrl)
        } else {
            val tok = resolveTokenForUrl(wv.url, snap)
            if (tok != null) {
                wv.evaluateJavascript(buildTokenInjectionScript(tok), null)
            }
        }
    }

    DisposableEffect(Unit) {
        onDispose {
            modelManager.cancelDownload()
            asrEngine.destroy()
            webViewRef?.let { wv ->
                if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
                    try {
                        WebViewCompat.removeWebMessageListener(wv, VOICE_WEB_MESSAGE_CHANNEL)
                    } catch (_: Exception) {
                    }
                }
                wv.stopLoading()
                wv.destroy()
            }
            webViewRef = null
            registeredChannelTarget = null
        }
    }

    if (showVoiceModelSheet) {
        VoiceModelSheet(
            modelManager = modelManager,
            mirrorBaseUrl = ServerUrlNormalizer.normalize(activeServer),
            lastResult = lastVoiceResult,
            onModelDeleted = { deletedSpec ->
                asrEngine.releaseIfMatches(deletedSpec)
            },
            onDismiss = { showVoiceModelSheet = false }
        )
    }

    Scaffold(
        modifier = modifier.fillMaxSize(),
        containerColor = MaterialTheme.colorScheme.background,
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        topBar = {
            Column {
                TopAppBar(
                    windowInsets = WindowInsets(0, 0, 0, 0),
                    navigationIcon = {
                        IconButton(onClick = onOpenDrawer) {
                            Icon(Icons.Default.Menu, contentDescription = "打开导航抽屉")
                        }
                    },
                    title = {
                        Column {
                            Text(
                                text = "PC 终端 (Bots)",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                            Text(
                                text = displayTargetUrl.removePrefix("https://").removePrefix("http://"),
                                style = MaterialTheme.typography.bodySmall,
                                color = TextSecondary,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                        }
                    },
                    actions = {
                        IconButton(onClick = { showVoiceModelSheet = true }) {
                            Icon(Icons.Default.Mic, contentDescription = "离线语音模型设置", tint = TextSecondary)
                        }
                        IconButton(onClick = onToggleQuickBar) {
                            Icon(Icons.Default.Apps, contentDescription = "快捷切换栏", tint = TextSecondary)
                        }
                        IconButton(
                            onClick = {
                                scope.launch {
                                    val snap = syncAuthSnapshot(activeServer, forceRefresh = false)
                                    errorMessage = null
                                    webViewRef?.let { wv ->
                                        if (wv.url.isNullOrBlank() || !isSameOrigin(wv.url, snap.targetUrl)) {
                                            lastLoadedTargetUrl = snap.targetUrl
                                            wv.loadUrl(snap.targetUrl)
                                        } else {
                                            val tok = resolveTokenForUrl(wv.url, snap)
                                            if (tok != null) {
                                                val quoted = pcBotsGson.toJson(tok)
                                                val syncOnlyScript = "(function(){try{var t=$quoted;if(t){window.localStorage.setItem('pc_cli_client_token',t);}else{window.localStorage.removeItem('pc_cli_client_token');}}catch(e){}})();"
                                                wv.evaluateJavascript(syncOnlyScript) {
                                                    wv.reload()
                                                }
                                            } else {
                                                wv.reload()
                                            }
                                        }
                                    }
                                }
                            }
                        ) {
                            Icon(Icons.Default.Refresh, contentDescription = "刷新")
                        }
                        IconButton(onClick = onOpenSettings) {
                            Icon(Icons.Default.Settings, contentDescription = "设置", tint = TextSecondary)
                        }
                    }
                )
                if (isLoading && loadProgress in 1..99) {
                    LinearProgressIndicator(
                        progress = { loadProgress / 100f },
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(2.dp),
                        color = PrimaryIndigo
                    )
                }
            }
        }
    ) { innerPadding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
        ) {
            val activeSnap = readySnapshot
            if (activeSnap == null) {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(color = PrimaryIndigo)
                }
            } else {
                AndroidView(
                    modifier = Modifier.fillMaxSize(),
                    factory = { ctx ->
                        WebView(ctx).apply {
                            layoutParams = ViewGroup.LayoutParams(
                                ViewGroup.LayoutParams.MATCH_PARENT,
                                ViewGroup.LayoutParams.MATCH_PARENT
                            )
                            isVerticalScrollBarEnabled = true
                            overScrollMode = android.view.View.OVER_SCROLL_IF_CONTENT_SCROLLS
                            setOnTouchListener { v, event ->
                                when (event.actionMasked) {
                                    android.view.MotionEvent.ACTION_DOWN,
                                    android.view.MotionEvent.ACTION_MOVE -> {
                                        v.parent?.requestDisallowInterceptTouchEvent(true)
                                    }
                                    android.view.MotionEvent.ACTION_UP,
                                    android.view.MotionEvent.ACTION_CANCEL -> {
                                        v.parent?.requestDisallowInterceptTouchEvent(false)
                                    }
                                }
                                false
                            }
                            setBackgroundColor(android.graphics.Color.parseColor("#0B1120"))
                            settings.apply {
                                javaScriptEnabled = true
                                domStorageEnabled = true
                                databaseEnabled = true
                                cacheMode = WebSettings.LOAD_DEFAULT
                                allowFileAccess = false
                                allowContentAccess = false
                                loadsImagesAutomatically = true
                                useWideViewPort = true
                                loadWithOverviewMode = false
                            }

                            registerVoiceWebMessageListener(this, activeSnap.targetUrl)

                            webChromeClient = object : WebChromeClient() {
                                override fun onProgressChanged(view: WebView?, newProgress: Int) {
                                    loadProgress = newProgress
                                    isLoading = newProgress < 100
                                }

                                override fun onPermissionRequest(request: PermissionRequest?) {
                                    if (request == null) return
                                    val snap = currentSnapshotState.value
                                    val originUrl = request.origin?.toString()
                                    if (snap != null && isSameOrigin(originUrl, snap.targetUrl)) {
                                        val allowed = request.resources.filter {
                                            it == PermissionRequest.RESOURCE_AUDIO_CAPTURE
                                        }.toTypedArray()
                                        if (allowed.isNotEmpty()) {
                                            request.grant(allowed)
                                            return
                                        }
                                    }
                                    request.deny()
                                }
                            }
                            webViewClient = object : WebViewClient() {
                                override fun shouldOverrideUrlLoading(
                                    view: WebView?,
                                    request: WebResourceRequest?
                                ): Boolean {
                                    val uri = request?.url ?: return false
                                    val urlStr = uri.toString()
                                    val snap = currentSnapshotState.value
                                    if (snap != null && isSameOrigin(urlStr, snap.targetUrl)) {
                                        return false
                                    }
                                    if (uri.scheme == "http" || uri.scheme == "https") {
                                        try {
                                            context.startActivity(Intent(Intent.ACTION_VIEW, uri))
                                        } catch (_: Exception) {
                                        }
                                        return true
                                    }
                                    return true
                                }

                                override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                                    isLoading = true
                                    errorMessage = null
                                }

                                override fun onPageFinished(view: WebView?, url: String?) {
                                    isLoading = false
                                    canGoBack = view?.canGoBack() == true
                                    val tok = resolveTokenForUrl(url, currentSnapshotState.value)
                                    if (view != null && tok != null) {
                                        view.evaluateJavascript(
                                            buildTokenInjectionScript(tok),
                                            null
                                        )
                                    }
                                }

                                override fun onReceivedError(
                                    view: WebView?,
                                    request: WebResourceRequest?,
                                    error: WebResourceError?
                                ) {
                                    if (request?.isForMainFrame == true) {
                                        isLoading = false
                                        errorMessage = error?.description?.toString()
                                            ?: "页面加载失败，请检查网络连接"
                                    }
                                }
                            }
                            webViewRef?.let { oldWv ->
                                if (oldWv !== this) {
                                    oldWv.stopLoading()
                                    oldWv.destroy()
                                }
                            }
                            webViewRef = this
                            lastLoadedTargetUrl = activeSnap.targetUrl
                            loadUrl(activeSnap.targetUrl)
                        }
                    },
                    update = { view ->
                        canGoBack = view.canGoBack()
                    }
                )
            }

            if (errorMessage != null) {
                Surface(
                    color = MaterialTheme.colorScheme.background,
                    modifier = Modifier.fillMaxSize()
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxSize()
                            .verticalScroll(rememberScrollState())
                            .padding(20.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Spacer(modifier = Modifier.height(8.dp))
                        Icon(
                            imageVector = Icons.Default.CloudOff,
                            contentDescription = null,
                            tint = TextSecondary,
                            modifier = Modifier.size(40.dp)
                        )
                        Text(
                            text = "当前处于离线状态，无法连接 PC Bots",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold
                        )
                        Text(
                            text = errorMessage ?: "",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary
                        )
                        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            Button(
                                onClick = {
                                    val snap = currentSnapshotState.value ?: return@Button
                                    errorMessage = null
                                    lastLoadedTargetUrl = snap.targetUrl
                                    webViewRef?.loadUrl(snap.targetUrl)
                                }
                            ) {
                                Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(18.dp))
                                Spacer(modifier = Modifier.width(6.dp))
                                Text("重试联网")
                            }
                            OutlinedButton(onClick = { showVoiceModelSheet = true }) {
                                Icon(Icons.Default.Tune, contentDescription = null, modifier = Modifier.size(18.dp))
                                Spacer(modifier = Modifier.width(6.dp))
                                Text("管理语音模型")
                            }
                        }

                        HorizontalDivider(modifier = Modifier.padding(vertical = 4.dp))

                        // 飞行模式冷启动兜底：无需联网即可测试/使用本地离线语音识别草稿箱
                        Card(
                            colors = CardDefaults.cardColors(containerColor = SurfaceElevated),
                            shape = RoundedCornerShape(14.dp),
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Column(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(14.dp),
                                verticalArrangement = Arrangement.spacedBy(10.dp)
                            ) {
                                Text(
                                    text = "离线语音草稿箱（飞行模式可用）",
                                    style = MaterialTheme.typography.titleSmall,
                                    fontWeight = FontWeight.SemiBold
                                )
                                Text(
                                    text = "当前模型：${selectedVoiceModel.displayName}",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = TextSecondary
                                )
                                if (offlineVoiceStatus.isNotBlank()) {
                                    Text(
                                        text = offlineVoiceStatus,
                                        style = MaterialTheme.typography.labelMedium,
                                        color = PrimaryIndigo
                                    )
                                }
                                OutlinedTextField(
                                    value = offlineDraftText,
                                    onValueChange = { offlineDraftText = it },
                                    placeholder = { Text("点击下方按钮说话，识别结果会离线填入此处...") },
                                    minLines = 3,
                                    maxLines = 6,
                                    modifier = Modifier.fillMaxWidth()
                                )
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                                ) {
                                    Button(
                                        onClick = {
                                            handleVoiceToggle(
                                                reqId = "offline_${System.currentTimeMillis()}",
                                                target = "offline",
                                                botId = ""
                                            )
                                        },
                                        enabled = !offlineTranscribing,
                                        modifier = Modifier.weight(1f)
                                    ) {
                                        Icon(
                                            imageVector = if (offlineRecording) Icons.Default.Stop else Icons.Default.Mic,
                                            contentDescription = null,
                                            modifier = Modifier.size(18.dp)
                                        )
                                        Spacer(modifier = Modifier.width(6.dp))
                                        Text(
                                            when {
                                                offlineTranscribing -> "本地识别中..."
                                                offlineRecording -> "停止并识别"
                                                else -> "开始离线语音"
                                            }
                                        )
                                    }
                                    OutlinedButton(
                                        onClick = {
                                            if (offlineDraftText.isNotBlank()) {
                                                clipboardManager.setText(AnnotatedString(offlineDraftText))
                                                offlineVoiceStatus = "已复制到剪贴板"
                                            }
                                        },
                                        enabled = offlineDraftText.isNotBlank()
                                    ) {
                                        Icon(Icons.Default.ContentCopy, contentDescription = null, modifier = Modifier.size(16.dp))
                                        Spacer(modifier = Modifier.width(4.dp))
                                        Text("复制")
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
