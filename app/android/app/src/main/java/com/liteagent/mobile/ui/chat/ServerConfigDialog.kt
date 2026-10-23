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
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.ui.theme.*
import kotlinx.coroutines.launch

@Composable
fun ServerConfigDialog(
    tokenStorage: SecureTokenStorage,
    apiClient: ApiClient,
    onDismiss: () -> Unit,
    onSaved: (newServerUrl: String) -> Unit
) {
    val scope = rememberCoroutineScope()
    var serverUrl by remember { mutableStateOf("") }
    var generalToken by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }

    var isTesting by remember { mutableStateOf(false) }
    var testResult by remember { mutableStateOf<String?>(null) }
    var isTestSuccess by remember { mutableStateOf(false) }
    var saveError by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        val activeUrl = tokenStorage.getActiveServerUrl()
        serverUrl = activeUrl
        generalToken = tokenStorage.getGeneralToken(activeUrl)
    }

    Dialog(onDismissRequest = onDismiss) {
        Card(
            modifier = Modifier
                .fillMaxWidth()
                .padding(8.dp),
            shape = RoundedCornerShape(20.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            border = androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.outline)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(24.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Icon(
                        imageVector = Icons.Default.Dns,
                        contentDescription = null,
                        tint = MaterialTheme.colorScheme.primary
                    )
                    Text(
                        text = "服务器与安全凭据",
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold
                    )
                }

                Text(
                    text = "凭证按服务器物理隔离存储，由 Android Keystore 硬件级 AES-256-GCM 加密，严禁降级与云备份。",
                    style = MaterialTheme.typography.bodyMedium,
                    color = TextSecondary
                )

                OutlinedTextField(
                    value = serverUrl,
                    onValueChange = {
                        serverUrl = it
                        testResult = null
                        saveError = null
                    },
                    label = { Text("服务地址 (Base URL)") },
                    placeholder = { Text("http://192.168.2.xxx:8000") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    leadingIcon = {
                        Icon(Icons.Default.Link, contentDescription = null, tint = TextMuted)
                    }
                )

                OutlinedTextField(
                    value = generalToken,
                    onValueChange = {
                        generalToken = it
                        testResult = null
                        saveError = null
                    },
                    label = { Text("通用 API 密钥 (Bearer Token)") },
                    placeholder = { Text("输入静态 auth_token") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    visualTransformation = if (showPassword) VisualTransformation.None else PasswordVisualTransformation(),
                    leadingIcon = {
                        Icon(Icons.Default.Key, contentDescription = null, tint = TextMuted)
                    },
                    trailingIcon = {
                        IconButton(onClick = { showPassword = !showPassword }) {
                            Icon(
                                imageVector = if (showPassword) Icons.Default.VisibilityOff else Icons.Default.Visibility,
                                contentDescription = if (showPassword) "隐藏" else "显示",
                                tint = TextMuted
                            )
                        }
                    }
                )

                if (saveError != null) {
                    Surface(
                        color = AccentRose.copy(alpha = 0.15f),
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(
                            text = saveError!!,
                            color = AccentRose,
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(12.dp)
                        )
                    }
                }

                if (testResult != null) {
                    Surface(
                        color = if (isTestSuccess) AccentEmerald.copy(alpha = 0.15f) else AccentRose.copy(alpha = 0.15f),
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(
                            modifier = Modifier.padding(12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Icon(
                                imageVector = if (isTestSuccess) Icons.Default.CheckCircle else Icons.Default.Error,
                                contentDescription = null,
                                tint = if (isTestSuccess) AccentEmerald else AccentRose,
                                modifier = Modifier.size(18.dp)
                            )
                            Text(
                                text = testResult!!,
                                style = MaterialTheme.typography.bodyMedium,
                                color = if (isTestSuccess) AccentEmerald else AccentRose
                            )
                        }
                    }
                }

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    OutlinedButton(
                        onClick = {
                            scope.launch {
                                isTesting = true
                                testResult = null
                                saveError = null
                                try {
                                    // 临时测试连接，绝不篡改持久化存储
                                    val normalized = com.liteagent.mobile.core.network.ServerUrlNormalizer.normalize(serverUrl)
                                    val response = apiClient.testConnectionDirect(normalized, generalToken)
                                    if (response.isSuccessful) {
                                        isTestSuccess = true
                                        val total = response.body()?.total ?: 0
                                        testResult = "连接成功！在线 API 会话数: $total"
                                    } else if (response.code() == 401) {
                                        isTestSuccess = false
                                        testResult = "鉴权失败 (401)：密钥错误或未授权"
                                    } else {
                                        isTestSuccess = false
                                        testResult = "响应异常 (HTTP ${response.code()})"
                                    }
                                } catch (e: Exception) {
                                    isTestSuccess = false
                                    testResult = "连接失败: ${e.localizedMessage ?: e.message}"
                                } finally {
                                    isTesting = false
                                }
                            }
                        },
                        enabled = !isTesting && serverUrl.isNotBlank(),
                        modifier = Modifier.weight(1f)
                    ) {
                        if (isTesting) {
                            CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                        } else {
                            Text("测试连接")
                        }
                    }

                    Button(
                        onClick = {
                            scope.launch {
                                try {
                                    // 只有用户主动保存时，才正式持久化并加密保存
                                    val cleanUrl = com.liteagent.mobile.core.network.ServerUrlNormalizer.normalize(serverUrl)
                                    tokenStorage.setGeneralToken(cleanUrl, generalToken)
                                    tokenStorage.setActiveServerUrl(cleanUrl)
                                    onSaved(cleanUrl)
                                    onDismiss()
                                } catch (e: SecurityException) {
                                    saveError = "加密安全终止: ${e.message}"
                                } catch (e: Exception) {
                                    saveError = "保存失败: ${e.message}"
                                }
                            }
                        },
                        modifier = Modifier.weight(1f)
                    ) {
                        Text("保存配置")
                    }
                }
            }
        }
    }
}
