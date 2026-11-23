package com.liteagent.mobile.ui.pcbots

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.google.gson.Gson
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.ui.theme.PrimaryIndigo
import com.liteagent.mobile.ui.theme.TextSecondary
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

private val pcBotsGson = Gson()

internal data class PcBotsAuthSnapshot(
    val serverUrl: String,
    val targetUrl: String,
    val token: String
)

internal fun buildMobilePwaUrl(serverUrl: String): String {
    val base = ServerUrlNormalizer.normalize(serverUrl).ifBlank {
        ServerUrlNormalizer.normalize(SecureTokenStorage.DEFAULT_SERVER_URL)
    }
    return "$base/mobile/"
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

internal fun buildTokenInjectionScript(token: String): String {
    val quoted = pcBotsGson.toJson(token)
    return """
        (function() {
          try {
            var tok = $quoted;
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

@SuppressLint("SetJavaScriptEnabled")
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PcBotsScreen(
    tokenStorage: SecureTokenStorage,
    onOpenDrawer: () -> Unit = {},
    onToggleQuickBar: () -> Unit = {},
    onOpenSettings: () -> Unit = {},
    modifier: Modifier = Modifier
) {
    val context = LocalContext.current
    val activeServer by tokenStorage.activeServerUrlFlow.collectAsState(initial = SecureTokenStorage.DEFAULT_SERVER_URL)
    val session by tokenStorage.sessionFlow.collectAsState(initial = null)
    val displayTargetUrl = remember(activeServer) { buildMobilePwaUrl(activeServer) }

    // 将服务器地址、目标 URL 与对应 Token 绑定为原子快照：切换服务器时立即清空，严禁把旧服 Token 注入新服
    var readySnapshot by remember { mutableStateOf<PcBotsAuthSnapshot?>(null) }

    LaunchedEffect(activeServer, session) {
        if (readySnapshot?.serverUrl != activeServer) {
            readySnapshot = null
        }
        val targetForServer = buildMobilePwaUrl(activeServer)
        val loadedToken = try {
            tokenStorage.getGeneralToken(activeServer)
        } catch (_: Exception) {
            ""
        }
        readySnapshot = PcBotsAuthSnapshot(
            serverUrl = activeServer,
            targetUrl = targetForServer,
            token = loadedToken
        )
    }

    val currentSnapshotState = rememberUpdatedState(readySnapshot)

    var webViewRef by remember { mutableStateOf<WebView?>(null) }
    var loadProgress by remember { mutableIntStateOf(0) }
    var isLoading by remember { mutableStateOf(true) }
    var canGoBack by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    var lastLoadedTargetUrl by remember { mutableStateOf("") }

    BackHandler(enabled = canGoBack) {
        webViewRef?.goBack()
    }

    // 仅在当前服务器的原子快照就绪后，才加载目标页面或向同源页面同步最新 Token
    LaunchedEffect(readySnapshot) {
        val snap = readySnapshot ?: return@LaunchedEffect
        val wv = webViewRef ?: return@LaunchedEffect
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
            webViewRef?.let { wv ->
                wv.stopLoading()
                wv.destroy()
            }
            webViewRef = null
        }
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
                        IconButton(onClick = onToggleQuickBar) {
                            Icon(Icons.Default.Apps, contentDescription = "快捷切换栏", tint = TextSecondary)
                        }
                        IconButton(
                            onClick = {
                                val snap = currentSnapshotState.value ?: return@IconButton
                                errorMessage = null
                                webViewRef?.let { wv ->
                                    if (wv.url.isNullOrBlank() || !isSameOrigin(wv.url, snap.targetUrl)) {
                                        lastLoadedTargetUrl = snap.targetUrl
                                        wv.loadUrl(snap.targetUrl)
                                    } else {
                                        wv.reload()
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
                                loadWithOverviewMode = true
                            }
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
                            .padding(24.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.Center
                    ) {
                        Icon(
                            imageVector = Icons.Default.CloudOff,
                            contentDescription = null,
                            tint = TextSecondary,
                            modifier = Modifier.size(48.dp)
                        )
                        Spacer(modifier = Modifier.height(12.dp))
                        Text(
                            text = "无法连接到 PC Bots 面板",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold
                        )
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            text = errorMessage ?: "",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary
                        )
                        Spacer(modifier = Modifier.height(16.dp))
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
                            Text("重试加载")
                        }
                    }
                }
            }
        }
    }
}
