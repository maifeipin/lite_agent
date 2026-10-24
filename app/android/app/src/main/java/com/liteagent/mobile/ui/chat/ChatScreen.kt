package com.liteagent.mobile.ui.chat

import androidx.compose.animation.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.platform.LocalFocusManager
import com.liteagent.mobile.core.network.models.MessageItem
import com.liteagent.mobile.core.network.models.SessionItem
import com.liteagent.mobile.ui.common.autoDismissKeyboardOnTap
import com.liteagent.mobile.ui.theme.*

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun ChatScreen(
    viewModel: ChatViewModel,
    onOpenSettings: () -> Unit
) {
    val focusManager = LocalFocusManager.current
    val isImeVisible = WindowInsets.isImeVisible
    val sessions by viewModel.sessions.collectAsState()
    val currentSession by viewModel.currentSession.collectAsState()
    val messages by viewModel.messages.collectAsState()
    val isStreaming by viewModel.isStreaming.collectAsState()
    val taskStatus by viewModel.taskStatus.collectAsState()
    val taskLogs by viewModel.taskLogs.collectAsState()
    val errorMessage by viewModel.errorMessage.collectAsState()
    val pendingRecoveryTasks by viewModel.pendingRecoveryTasks.collectAsState()

    var inputText by remember { mutableStateOf("") }
    var showSessionMenu by remember { mutableStateOf(false) }
    var isThinkingExpanded by remember { mutableStateOf(true) }

    val listState = rememberLazyListState()

    LaunchedEffect(messages.size, taskLogs.size, isImeVisible) {
        if (messages.isNotEmpty() || taskLogs.isNotEmpty()) {
            listState.animateScrollToItem(index = (messages.size + (if (taskLogs.isNotEmpty()) 1 else 0)).coerceAtLeast(0))
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
    ) {
        // 会话切换栏与状态
        Surface(
            color = MaterialTheme.colorScheme.surface,
            tonalElevation = 2.dp,
            modifier = Modifier.fillMaxWidth()
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 16.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Box {
                    Surface(
                        shape = RoundedCornerShape(20.dp),
                        color = MaterialTheme.colorScheme.surfaceVariant,
                        border = androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
                        modifier = Modifier.clickable { showSessionMenu = true }
                    ) {
                        Row(
                            modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(6.dp)
                        ) {
                            Icon(
                                imageVector = Icons.Default.Forum,
                                contentDescription = null,
                                tint = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.size(16.dp)
                            )
                            Text(
                                text = currentSession?.displayTitle ?: "选择 API 会话",
                                style = MaterialTheme.typography.titleMedium,
                                fontSize = 13.sp,
                                maxLines = 1
                            )
                            Icon(
                                imageVector = Icons.Default.ArrowDropDown,
                                contentDescription = null,
                                tint = TextMuted,
                                modifier = Modifier.size(18.dp)
                            )
                        }
                    }

                    DropdownMenu(
                        expanded = showSessionMenu,
                        onDismissRequest = { showSessionMenu = false },
                        modifier = Modifier.background(MaterialTheme.colorScheme.surface)
                    ) {
                        DropdownMenuItem(
                            text = { Text("➕ 新建会话", fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.primary) },
                            onClick = {
                                viewModel.createNewSession()
                                showSessionMenu = false
                            }
                        )
                        HorizontalDivider(color = MaterialTheme.colorScheme.outline.copy(alpha = 0.5f))
                        sessions.forEach { s ->
                            DropdownMenuItem(
                                text = {
                                    Column {
                                        Text(s.displayTitle, maxLines = 1, style = MaterialTheme.typography.bodyMedium)
                                        if (s.status.isNotBlank()) {
                                            Text("状态: ${s.status}", style = MaterialTheme.typography.labelSmall, color = TextMuted)
                                        }
                                    }
                                },
                                onClick = {
                                    viewModel.selectSession(s)
                                    showSessionMenu = false
                                }
                            )
                        }
                    }
                }

                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    IconButton(onClick = { viewModel.createNewSession() }) {
                        Icon(
                            imageVector = Icons.Default.AddComment,
                            contentDescription = "新建会话",
                            tint = MaterialTheme.colorScheme.primary
                        )
                    }
                    IconButton(onClick = onOpenSettings) {
                        Icon(
                            imageVector = Icons.Default.Settings,
                            contentDescription = "服务器与凭据设置",
                            tint = TextSecondary
                        )
                    }
                }
            }
        }

        // 未完成任务恢复横幅 (断网/超时/进程重启后主动提示)
        AnimatedVisibility(visible = pendingRecoveryTasks.isNotEmpty() && !isStreaming) {
            Surface(
                color = AccentAmber.copy(alpha = 0.15f),
                modifier = Modifier.fillMaxWidth()
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.SpaceBetween
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        modifier = Modifier.weight(1f)
                    ) {
                        Icon(
                            imageVector = Icons.Default.SyncProblem,
                            contentDescription = null,
                            tint = AccentAmber,
                            modifier = Modifier.size(18.dp)
                        )
                        Text(
                            text = "检测到未完成的长任务，可继续同步最新进度",
                            style = MaterialTheme.typography.bodySmall,
                            color = AccentAmber
                        )
                    }
                    Button(
                        onClick = { viewModel.resumeRecoveryTask() },
                        colors = ButtonDefaults.buttonColors(containerColor = AccentAmber),
                        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp),
                        modifier = Modifier.height(32.dp)
                    ) {
                        Text("恢复任务", fontSize = 12.sp, color = Color.Black, fontWeight = FontWeight.Bold)
                    }
                }
            }
        }

        // 错误与 401 提示条
        AnimatedVisibility(visible = errorMessage != null) {
            Surface(
                color = AccentRose.copy(alpha = 0.2f),
                modifier = Modifier.fillMaxWidth()
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.SpaceBetween
                ) {
                    Text(
                        text = errorMessage ?: "",
                        style = MaterialTheme.typography.bodySmall,
                        color = AccentRose,
                        modifier = Modifier.weight(1f)
                    )
                    TextButton(onClick = onOpenSettings) {
                        Text("去配置", color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold)
                    }
                }
            }
        }

        // 消息列表与流式进度卡片
        LazyColumn(
            state = listState,
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .padding(horizontal = 16.dp)
                .autoDismissKeyboardOnTap(focusManager),
            verticalArrangement = Arrangement.spacedBy(12.dp),
            contentPadding = PaddingValues(vertical = 16.dp)
        ) {
            if (messages.isEmpty() && taskLogs.isEmpty()) {
                item {
                    EmptyChatGreeting(onSuggestClick = { prompt ->
                        inputText = prompt
                    })
                }
            }

            items(messages) { msg ->
                MessageBubble(message = msg)
            }

            // 任务执行进度与思考日志折叠卡片 (SSE 增量流)
            if (isStreaming || taskLogs.isNotEmpty() || taskStatus == "pending_recovery") {
                item {
                    TaskProgressCard(
                        status = taskStatus ?: "running",
                        isStreaming = isStreaming,
                        logs = taskLogs,
                        isExpanded = isThinkingExpanded,
                        onToggle = { isThinkingExpanded = !isThinkingExpanded },
                        onRetry = { viewModel.resumeRecoveryTask() }
                    )
                }
            }
        }

        // 底部发送工具条
        Surface(
            color = MaterialTheme.colorScheme.surface,
            tonalElevation = 6.dp,
            modifier = Modifier.fillMaxWidth()
        ) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                OutlinedTextField(
                    value = inputText,
                    onValueChange = { inputText = it },
                    placeholder = {
                        Text(
                            text = if (isStreaming) "长任务执行中，可折叠查看日志..." else "输入给 Lite Agent 的问题或指令...",
                            fontSize = 14.sp
                        )
                    },
                    modifier = Modifier.weight(1f),
                    maxLines = 4,
                    shape = RoundedCornerShape(24.dp),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedContainerColor = MaterialTheme.colorScheme.background,
                        unfocusedContainerColor = MaterialTheme.colorScheme.background,
                        focusedBorderColor = MaterialTheme.colorScheme.primary,
                        unfocusedBorderColor = MaterialTheme.colorScheme.outline
                    )
                )

                IconButton(
                    onClick = {
                        if (inputText.isNotBlank() && !isStreaming) {
                            val textToSend = inputText
                            inputText = ""
                            viewModel.sendMessage(textToSend)
                        }
                    },
                    enabled = inputText.isNotBlank() && !isStreaming,
                    modifier = Modifier
                        .size(48.dp)
                        .clip(CircleShape)
                        .background(
                            if (inputText.isNotBlank() && !isStreaming)
                                MaterialTheme.colorScheme.primary
                            else
                                MaterialTheme.colorScheme.surfaceVariant
                        )
                ) {
                    if (isStreaming) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(20.dp),
                            color = MaterialTheme.colorScheme.primary,
                            strokeWidth = 2.dp
                        )
                    } else {
                        Icon(
                            imageVector = Icons.Default.Send,
                            contentDescription = "发送",
                            tint = if (inputText.isNotBlank()) Color.White else TextMuted,
                            modifier = Modifier.size(20.dp)
                        )
                    }
                }
            }
        }
    }
}

@Composable
fun MessageBubble(message: MessageItem) {
    val isUser = message.role == "user"

    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start
    ) {
        if (!isUser) {
            Surface(
                shape = CircleShape,
                color = MaterialTheme.colorScheme.primary.copy(alpha = 0.2f),
                modifier = Modifier
                    .size(32.dp)
                    .padding(top = 4.dp)
            ) {
                Box(contentAlignment = Alignment.Center) {
                    Icon(
                        imageVector = Icons.Default.SmartToy,
                        contentDescription = null,
                        tint = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.size(18.dp)
                    )
                }
            }
            Spacer(modifier = Modifier.width(8.dp))
        }

        Surface(
            shape = if (isUser) {
                RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp, bottomStart = 16.dp, bottomEnd = 2.dp)
            } else {
                RoundedCornerShape(topStart = 16.dp, topEnd = 16.dp, bottomStart = 2.dp, bottomEnd = 16.dp)
            },
            color = if (isUser) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surface,
            border = if (isUser) null else androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
            modifier = Modifier.widthIn(max = 300.dp)
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                if (!message.reasoningContent.isNullOrBlank()) {
                    Surface(
                        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f),
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(bottom = 8.dp)
                    ) {
                        Text(
                            text = "💭 思考过程:\n${message.reasoningContent}",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextSecondary,
                            modifier = Modifier.padding(8.dp)
                        )
                    }
                }

                Text(
                    text = message.content,
                    style = MaterialTheme.typography.bodyLarge,
                    color = if (isUser) Color.White else TextPrimary
                )
            }
        }
    }
}

@Composable
fun TaskProgressCard(
    status: String,
    isStreaming: Boolean,
    logs: List<String>,
    isExpanded: Boolean,
    onToggle: () -> Unit,
    onRetry: () -> Unit
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.outline)
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { onToggle() },
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    if (isStreaming) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(16.dp),
                            color = MaterialTheme.colorScheme.primary,
                            strokeWidth = 2.dp
                        )
                    } else {
                        Icon(
                            imageVector = when (status) {
                                "completed", "done", "succeeded" -> Icons.Default.CheckCircle
                                "partial" -> Icons.Default.CheckCircleOutline
                                "pending_recovery" -> Icons.Default.Refresh
                                "failed", "error" -> Icons.Default.Error
                                else -> Icons.Default.Schedule
                            },
                            contentDescription = null,
                            tint = when (status) {
                                "completed", "done", "succeeded" -> AccentEmerald
                                "partial" -> SecondarySky
                                "pending_recovery" -> AccentAmber
                                "failed", "error" -> AccentRose
                                else -> AccentAmber
                            },
                            modifier = Modifier.size(16.dp)
                        )
                    }

                    val statusLabel = when (status) {
                        "completed", "done", "succeeded" -> "任务执行完成"
                        "partial" -> "任务部分完成 (partial)"
                        "pending_recovery" -> "连接已超时中断 (可恢复)"
                        "failed", "error" -> "任务执行失败"
                        else -> "任务执行中..."
                    }

                    Text(
                        text = "$statusLabel (${logs.size} 条日志)",
                        style = MaterialTheme.typography.titleMedium,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Bold
                    )
                }

                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (status == "pending_recovery" && !isStreaming) {
                        TextButton(onClick = onRetry) {
                            Text("重连", fontSize = 12.sp, color = MaterialTheme.colorScheme.primary)
                        }
                    }
                    Icon(
                        imageVector = if (isExpanded) Icons.Default.ExpandLess else Icons.Default.ExpandMore,
                        contentDescription = null,
                        tint = TextMuted
                    )
                }
            }

            AnimatedVisibility(visible = isExpanded) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 10.dp)
                        .clip(RoundedCornerShape(8.dp))
                        .background(Color(0xFF070B14))
                        .border(1.dp, Color(0xFF1E293B), RoundedCornerShape(8.dp))
                        .padding(10.dp)
                ) {
                    if (logs.isEmpty()) {
                        Text(
                            text = "等待任务调度事件...",
                            fontFamily = FontFamily.Monospace,
                            fontSize = 11.sp,
                            color = TextMuted
                        )
                    } else {
                        logs.takeLast(100).forEach { logLine ->
                            Text(
                                text = "› $logLine",
                                fontFamily = FontFamily.Monospace,
                                fontSize = 11.sp,
                                color = Color(0xFF38BDF8),
                                lineHeight = 16.sp
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun EmptyChatGreeting(
    onSuggestClick: (String) -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 32.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        Surface(
            shape = CircleShape,
            color = MaterialTheme.colorScheme.primary.copy(alpha = 0.15f),
            modifier = Modifier.size(64.dp)
        ) {
            Box(contentAlignment = Alignment.Center) {
                Icon(
                    imageVector = Icons.Default.AutoAwesome,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.size(32.dp)
                )
            }
        }

        Text(
            text = "Lite Agent AI 随身智脑",
            style = MaterialTheme.typography.titleLarge,
            fontWeight = FontWeight.Bold
        )

        Text(
            text = "随时下发指令、提问、拉取任务进度或触发多机协同调度。",
            style = MaterialTheme.typography.bodyMedium,
            color = TextSecondary,
            textAlign = androidx.compose.ui.text.style.TextAlign.Center,
            modifier = Modifier.padding(horizontal = 24.dp)
        )

        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(top = 8.dp),
            horizontalArrangement = Arrangement.Center
        ) {
            SuggestionChip(
                onClick = { onSuggestClick("帮我总结今天的待办事项和未读邮件") },
                label = { Text("总结今日待办与邮件", fontSize = 12.sp) }
            )
        }
    }
}
