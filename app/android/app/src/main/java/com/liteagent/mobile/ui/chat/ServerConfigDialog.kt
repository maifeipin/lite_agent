package com.liteagent.mobile.ui.chat

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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.network.models.Socks5NodeItem
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.core.security.UserSession
import com.liteagent.mobile.ui.theme.AccentAmber
import com.liteagent.mobile.ui.theme.AccentGreen
import com.liteagent.mobile.ui.theme.AccentRose
import com.liteagent.mobile.ui.theme.PrimaryIndigo
import com.liteagent.mobile.ui.theme.SurfaceBorder
import com.liteagent.mobile.ui.theme.SurfaceElevated
import com.liteagent.mobile.ui.theme.TextMuted
import com.liteagent.mobile.ui.theme.TextSecondary
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.*

@Composable
fun AccountSettingsDialog(
    tokenStorage: SecureTokenStorage,
    apiClient: ApiClient,
    onDismiss: () -> Unit,
    onServerSwitched: (newServerUrl: String) -> Unit,
    onLoggedOut: () -> Unit
) {
    val scope = rememberCoroutineScope()
    var activeServerUrl by remember { mutableStateOf("") }
    var userSession by remember { mutableStateOf<UserSession?>(null) }
    var isSwitchingServer by remember { mutableStateOf(false) }
    var newServerUrlInput by remember { mutableStateOf("") }
    var isLoggingOut by remember { mutableStateOf(false) }

    // Socks5 Proxy State
    var socks5Nodes by remember { mutableStateOf<List<Socks5NodeItem>>(emptyList()) }
    var isLoadingNodes by remember { mutableStateOf(false) }
    var testingNodeId by remember { mutableStateOf<Int?>(null) }
    var activatingNodeId by remember { mutableStateOf<Int?>(null) }
    var socks5StatusMessage by remember { mutableStateOf<String?>(null) }
    val nodeLatencies = remember { mutableStateMapOf<Int, Int>() }

    fun loadSocks5(url: String) {
        if (url.isBlank()) return
        scope.launch {
            isLoadingNodes = true
            apiClient.getSocks5Nodes(url).onSuccess { list ->
                socks5Nodes = list
            }
            isLoadingNodes = false
        }
    }

    LaunchedEffect(Unit) {
        val url = tokenStorage.getActiveServerUrl()
        activeServerUrl = url
        newServerUrlInput = url
        userSession = tokenStorage.getSession(url)
        loadSocks5(url)
    }

    val expiresFormatted = remember(userSession) {
        val exp = userSession?.expiresAt ?: 0L
        if (exp > 0L) {
            val sdf = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault())
            sdf.format(Date(exp * 1000))
        } else {
            "未设定"
        }
    }

    Dialog(onDismissRequest = onDismiss) {
        Card(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(max = 640.dp)
                .padding(8.dp),
            shape = RoundedCornerShape(20.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState())
                    .padding(22.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp)
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Icon(
                        imageVector = Icons.Default.AccountCircle,
                        contentDescription = null,
                        tint = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.size(28.dp)
                    )
                    Text(
                        text = "账号与网络中枢设置",
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold
                    )
                }

                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)

                // Current Account Info
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(
                        text = "当前登录账号",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Text(
                        text = userSession?.username?.takeIf { it.isNotBlank() } ?: "未命名用户",
                        style = MaterialTheme.typography.bodyLarge,
                        fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface
                    )
                    Text(
                        text = "会话有效期至：$expiresFormatted",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }

                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)

                // Server Address Info
                if (!isSwitchingServer) {
                    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(
                            text = "已连接服务地址",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text(
                                text = activeServerUrl,
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurface,
                                modifier = Modifier.weight(1f)
                            )
                            TextButton(onClick = { isSwitchingServer = true }) {
                                Text("切换服务器")
                            }
                        }
                    }
                } else {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(
                            text = "输入新服务器地址",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                        OutlinedTextField(
                            value = newServerUrlInput,
                            onValueChange = { newServerUrlInput = it },
                            placeholder = { Text("https://mail.maifeipin.com/") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth()
                        )
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.End
                        ) {
                            TextButton(onClick = { isSwitchingServer = false }) {
                                Text("取消")
                            }
                            Button(
                                onClick = {
                                    val clean = ServerUrlNormalizer.normalize(newServerUrlInput)
                                    scope.launch {
                                        tokenStorage.setActiveServerUrl(clean)
                                        isSwitchingServer = false
                                        onServerSwitched(clean)
                                        onDismiss()
                                    }
                                }
                            ) {
                                Text("确认切换")
                            }
                        }
                    }
                }

                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)

                // Socks5 代理节点监控与测速模块
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(6.dp)
                        ) {
                            Icon(
                                imageVector = Icons.Default.Router,
                                contentDescription = null,
                                tint = PrimaryIndigo,
                                modifier = Modifier.size(18.dp)
                            )
                            Text(
                                text = "Socks5 代理节点监控",
                                style = MaterialTheme.typography.labelLarge,
                                fontWeight = FontWeight.Bold
                            )
                        }
                        IconButton(
                            onClick = { loadSocks5(activeServerUrl) },
                            enabled = !isLoadingNodes,
                            modifier = Modifier.size(28.dp)
                        ) {
                            if (isLoadingNodes) {
                                CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
                            } else {
                                Icon(Icons.Default.Refresh, contentDescription = "刷新节点", modifier = Modifier.size(16.dp))
                            }
                        }
                    }

                    if (!socks5StatusMessage.isNullOrBlank()) {
                        Text(
                            text = socks5StatusMessage!!,
                            style = MaterialTheme.typography.bodySmall,
                            color = PrimaryIndigo,
                            fontSize = 11.sp
                        )
                    }

                    if (socks5Nodes.isEmpty() && !isLoadingNodes) {
                        Text(
                            text = "暂无已配置的 Socks5 代理节点",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextMuted
                        )
                    } else {
                        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                            socks5Nodes.take(5).forEach { node ->
                                val isTesting = testingNodeId == node.id
                                val isActivating = activatingNodeId == node.id
                                val measuredLat = nodeLatencies[node.id]

                                Surface(
                                    color = SurfaceElevated,
                                    shape = RoundedCornerShape(10.dp),
                                    border = androidx.compose.foundation.BorderStroke(
                                        1.dp,
                                        if (node.active) PrimaryIndigo else SurfaceBorder
                                    ),
                                    modifier = Modifier.fillMaxWidth()
                                ) {
                                    Column(
                                        modifier = Modifier.padding(10.dp),
                                        verticalArrangement = Arrangement.spacedBy(4.dp)
                                    ) {
                                        Row(
                                            modifier = Modifier.fillMaxWidth(),
                                            horizontalArrangement = Arrangement.SpaceBetween,
                                            verticalAlignment = Alignment.CenterVertically
                                        ) {
                                            Row(
                                                verticalAlignment = Alignment.CenterVertically,
                                                horizontalArrangement = Arrangement.spacedBy(6.dp),
                                                modifier = Modifier.weight(1f)
                                            ) {
                                                Text(
                                                    text = node.displayLabel,
                                                    style = MaterialTheme.typography.bodySmall,
                                                    fontWeight = FontWeight.Bold,
                                                    maxLines = 1,
                                                    overflow = TextOverflow.Ellipsis
                                                )
                                                if (node.active) {
                                                    Surface(
                                                        color = AccentGreen.copy(alpha = 0.15f),
                                                        shape = RoundedCornerShape(4.dp)
                                                    ) {
                                                        Text(
                                                            text = "当前主节点",
                                                            color = AccentGreen,
                                                            fontSize = 9.sp,
                                                            fontWeight = FontWeight.Bold,
                                                            modifier = Modifier.padding(horizontal = 5.dp, vertical = 1.dp)
                                                        )
                                                    }
                                                }
                                            }

                                            if (measuredLat != null) {
                                                Text(
                                                    text = "$measuredLat ms",
                                                    color = AccentGreen,
                                                    fontSize = 11.sp,
                                                    fontWeight = FontWeight.Bold
                                                )
                                            }
                                        }

                                        Row(
                                            modifier = Modifier.fillMaxWidth(),
                                            horizontalArrangement = Arrangement.End,
                                            verticalAlignment = Alignment.CenterVertically
                                        ) {
                                            TextButton(
                                                onClick = {
                                                    testingNodeId = node.id
                                                    socks5StatusMessage = null
                                                    scope.launch {
                                                        apiClient.testSocks5Node(node.id, serverUrl = activeServerUrl)
                                                            .onSuccess { res ->
                                                                val lat = res.latencyMs?.toInt()
                                                                if (lat != null) {
                                                                    nodeLatencies[node.id] = lat
                                                                }
                                                                socks5StatusMessage = if (res.success) {
                                                                    "节点 ${node.displayLabel} 连通正常 (${lat ?: 0} ms)"
                                                                } else {
                                                                    "节点测速异常: ${res.error ?: res.message ?: "未连通"}"
                                                                }
                                                                loadSocks5(activeServerUrl)
                                                            }
                                                            .onFailure { err ->
                                                                socks5StatusMessage = "测速失败: ${err.message}"
                                                            }
                                                        testingNodeId = null
                                                    }
                                                },
                                                enabled = !isTesting,
                                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 2.dp),
                                                modifier = Modifier.height(28.dp)
                                            ) {
                                                if (isTesting) {
                                                    CircularProgressIndicator(modifier = Modifier.size(12.dp), strokeWidth = 1.5.dp)
                                                } else {
                                                    Text("⚡ 测速", fontSize = 11.sp)
                                                }
                                            }

                                            if (!node.active) {
                                                TextButton(
                                                    onClick = {
                                                        activatingNodeId = node.id
                                                        socks5StatusMessage = null
                                                        scope.launch {
                                                            apiClient.setActiveSocks5Node(node.id, activeServerUrl)
                                                                .onSuccess {
                                                                    socks5StatusMessage = "已切换主节点为 ${node.displayLabel}"
                                                                    loadSocks5(activeServerUrl)
                                                                }
                                                                .onFailure { err ->
                                                                    socks5StatusMessage = "切换失败: ${err.message}"
                                                                }
                                                            activatingNodeId = null
                                                        }
                                                    },
                                                    enabled = !isActivating,
                                                    contentPadding = PaddingValues(horizontal = 8.dp, vertical = 2.dp),
                                                    modifier = Modifier.height(28.dp)
                                                ) {
                                                    Text("🚀 设为主节点", fontSize = 11.sp)
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                Spacer(modifier = Modifier.height(4.dp))

                // Actions: Logout & Dismiss
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    TextButton(onClick = onDismiss) {
                        Text("关闭")
                    }

                    Button(
                        onClick = {
                            isLoggingOut = true
                            scope.launch {
                                apiClient.logout(activeServerUrl)
                                isLoggingOut = false
                                onDismiss()
                                onLoggedOut()
                            }
                        },
                        colors = ButtonDefaults.buttonColors(
                            containerColor = MaterialTheme.colorScheme.errorContainer,
                            contentColor = MaterialTheme.colorScheme.onErrorContainer
                        ),
                        enabled = !isLoggingOut
                    ) {
                        if (isLoggingOut) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(18.dp),
                                strokeWidth = 2.dp,
                                color = MaterialTheme.colorScheme.onErrorContainer
                            )
                        } else {
                            Icon(
                                imageVector = Icons.Default.Logout,
                                contentDescription = null,
                                modifier = Modifier.size(18.dp)
                            )
                            Spacer(modifier = Modifier.width(6.dp))
                            Text("退出登录")
                        }
                    }
                }
            }
        }
    }
}

/**
 * 保持兼容别名
 */
@Composable
fun ServerConfigDialog(
    tokenStorage: SecureTokenStorage,
    apiClient: ApiClient,
    onDismiss: () -> Unit,
    onSaved: (newServerUrl: String) -> Unit,
    onLoggedOut: (() -> Unit)? = null
) {
    AccountSettingsDialog(
        tokenStorage = tokenStorage,
        apiClient = apiClient,
        onDismiss = onDismiss,
        onServerSwitched = onSaved,
        onLoggedOut = onLoggedOut ?: {}
    )
}
