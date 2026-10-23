package com.liteagent.mobile.ui.chat

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.core.security.UserSession
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

    LaunchedEffect(Unit) {
        val url = tokenStorage.getActiveServerUrl()
        activeServerUrl = url
        newServerUrlInput = url
        userSession = tokenStorage.getSession(url)
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
                .padding(8.dp),
            shape = RoundedCornerShape(20.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(24.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp)
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
                        text = "账号与服务设置",
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold
                    )
                }

                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)

                // Current Account Info
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
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
                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
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

                Spacer(modifier = Modifier.height(8.dp))

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
