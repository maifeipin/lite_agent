package com.liteagent.mobile.ui.chat

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import com.liteagent.mobile.core.network.models.DashboardCommandItem
import com.liteagent.mobile.core.network.models.MessageItem
import com.liteagent.mobile.ui.common.autoDismissKeyboardOnTap
import com.liteagent.mobile.ui.theme.*
import java.io.File

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun ChatScreen(
    viewModel: ChatViewModel,
    onOpenSettings: () -> Unit,
    onOpenDrawer: () -> Unit = {},
    onToggleQuickBar: () -> Unit = {},
    totalBadgeCount: Int = 0
) {
    val context = LocalContext.current
    val focusManager = LocalFocusManager.current
    val isImeVisible = WindowInsets.isImeVisible
    val sessions by viewModel.sessions.collectAsState()
    val currentSession by viewModel.currentSession.collectAsState()
    val messages by viewModel.messages.collectAsState()
    val isStreaming by viewModel.isStreaming.collectAsState()
    val taskStatus by viewModel.taskStatus.collectAsState()
    val taskLogs by viewModel.taskLogs.collectAsState()
    val errorMessage by viewModel.errorMessage.collectAsState()
    val actionMessage by viewModel.actionMessage.collectAsState()
    val pendingRecoveryTasks by viewModel.pendingRecoveryTasks.collectAsState()
    val commands by viewModel.commands.collectAsState()
    val isUploadingOcr by viewModel.isUploadingOcr.collectAsState()
    val prefilledDraft by viewModel.prefilledDraft.collectAsState()

    var inputText by remember { mutableStateOf("") }
    var showSessionMenu by remember { mutableStateOf(false) }
    var showRenameDialog by remember { mutableStateOf(false) }
    var showCommandSheet by remember { mutableStateOf(false) }
    var showOcrSourceMenu by remember { mutableStateOf(false) }
    var isThinkingExpanded by remember { mutableStateOf(true) }
    var pendingCameraUri by remember { mutableStateOf<Uri?>(null) }

    val snackbarHostState = remember { SnackbarHostState() }
    val listState = rememberLazyListState()

    // 接收跨 Tab 预填的话题提示词
    LaunchedEffect(prefilledDraft) {
        prefilledDraft?.let { draft ->
            inputText = draft
            viewModel.consumePrefilledDraft()
        }
    }

    LaunchedEffect(actionMessage) {
        actionMessage?.let { msg ->
            snackbarHostState.showSnackbar(msg)
            viewModel.clearActionMessage()
        }
    }

    LaunchedEffect(messages.size, taskLogs.size, isImeVisible) {
        if (messages.isNotEmpty() || taskLogs.isNotEmpty()) {
            listState.animateScrollToItem(index = (messages.size + (if (taskLogs.isNotEmpty()) 1 else 0)).coerceAtLeast(0))
        }
    }

    // 处理 Uri 图片读取并上传 OCR
    val handleImageUri: (Uri, String) -> Unit = { uri, defaultName ->
        try {
            val bytes = context.contentResolver.openInputStream(uri)?.use { it.readBytes() }
            val mimeType = context.contentResolver.getType(uri) ?: "image/jpeg"
            if (bytes != null && bytes.isNotEmpty()) {
                viewModel.uploadOcrImage(
                    imageBytes = bytes,
                    fileName = defaultName,
                    mimeType = mimeType,
                    onTextExtracted = { extracted ->
                        inputText = if (inputText.isBlank()) {
                            extracted
                        } else {
                            "$inputText\n$extracted"
                        }
                    }
                )
            }
        } catch (_: Exception) {
        }
    }

    // 1. 系统相册选择器 (Android 13+ Photo Picker，免存储权限)
    val photoPickerLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.PickVisualMedia()
    ) { uri: Uri? ->
        if (uri != null) {
            handleImageUri(uri, "gallery_ocr.jpg")
        }
    }

    // 2. 系统相机拍照 Launcher
    val takePictureLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.TakePicture()
    ) { success: Boolean ->
        val uri = pendingCameraUri
        if (success && uri != null) {
            handleImageUri(uri, "camera_ocr.jpg")
        }
    }

    val launchCameraCapture = {
        try {
            val dir = File(context.cacheDir, "ocr_camera").apply { mkdirs() }
            val photoFile = File(dir, "capture_${System.currentTimeMillis()}.jpg")
            val uri = FileProvider.getUriForFile(
                context,
                "${context.packageName}.fileprovider",
                photoFile
            )
            pendingCameraUri = uri
            takePictureLauncher.launch(uri)
        } catch (_: Exception) {
        }
    }

    val cameraPermissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestPermission()
    ) { isGranted: Boolean ->
        if (isGranted) {
            launchCameraCapture()
        }
    }

    Scaffold(
        modifier = Modifier.fillMaxSize(),
        containerColor = MaterialTheme.colorScheme.background,
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        snackbarHost = { SnackbarHost(snackbarHostState) }
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
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
                        .padding(horizontal = 8.dp, vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.SpaceBetween
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(4.dp),
                        modifier = Modifier.weight(1f)
                    ) {
                        IconButton(
                            onClick = onOpenDrawer,
                            modifier = Modifier.size(38.dp)
                        ) {
                            BadgedBox(
                                badge = {
                                    if (totalBadgeCount > 0) {
                                        Badge(
                                            containerColor = AccentAmber,
                                            contentColor = Color.Black
                                        ) {
                                            Text("$totalBadgeCount", fontSize = 9.sp, fontWeight = FontWeight.Bold)
                                        }
                                    }
                                }
                            ) {
                                Icon(
                                    imageVector = Icons.Default.Menu,
                                    contentDescription = "打开导航抽屉",
                                    tint = TextPrimary
                                )
                            }
                        }

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
                                if (currentSession != null) {
                                    DropdownMenuItem(
                                        text = { Text("✏️ 重命名当前会话", color = TextPrimary) },
                                        onClick = {
                                            showSessionMenu = false
                                            showRenameDialog = true
                                        }
                                    )
                                }
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

                        if (currentSession != null) {
                            IconButton(
                                onClick = { showRenameDialog = true },
                                modifier = Modifier.size(32.dp)
                            ) {
                                Icon(
                                    imageVector = Icons.Default.Edit,
                                    contentDescription = "重命名会话",
                                    tint = TextMuted,
                                    modifier = Modifier.size(16.dp)
                                )
                            }
                        }
                    }

                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(2.dp)
                    ) {
                        IconButton(
                            onClick = onToggleQuickBar,
                            modifier = Modifier.size(36.dp)
                        ) {
                            Icon(
                                imageVector = Icons.Default.Apps,
                                contentDescription = "展开或收起快捷导航栏",
                                tint = TextSecondary,
                                modifier = Modifier.size(20.dp)
                            )
                        }
                        IconButton(
                            onClick = { viewModel.createNewSession() },
                            modifier = Modifier.size(36.dp)
                        ) {
                            Icon(
                                imageVector = Icons.Default.AddComment,
                                contentDescription = "新建会话",
                                tint = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.size(20.dp)
                            )
                        }
                        IconButton(
                            onClick = onOpenSettings,
                            modifier = Modifier.size(36.dp)
                        ) {
                            Icon(
                                imageVector = Icons.Default.Settings,
                                contentDescription = "服务器与凭据设置",
                                tint = TextSecondary,
                                modifier = Modifier.size(20.dp)
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
                        Row {
                            TextButton(onClick = { viewModel.clearError() }) {
                                Text("忽略", color = TextSecondary)
                            }
                            TextButton(onClick = onOpenSettings) {
                                Text("去配置", color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold)
                            }
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
                        EmptyChatGreeting(
                            onSuggestClick = { prompt -> inputText = prompt },
                            onOpenCommandSheet = { showCommandSheet = true }
                        )
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

            // 底部多模态发送工具条 (含 [📷 OCR] + [/ 指令] + 输入框 + 发送)
            Surface(
                color = MaterialTheme.colorScheme.surface,
                tonalElevation = 6.dp,
                modifier = Modifier.fillMaxWidth()
            ) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 10.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    // 1. [ 📷 ] 相机/相册 OCR 按钮
                    Box {
                        IconButton(
                            onClick = { showOcrSourceMenu = true },
                            enabled = !isUploadingOcr && !isStreaming,
                            modifier = Modifier
                                .size(38.dp)
                                .clip(CircleShape)
                                .background(MaterialTheme.colorScheme.surfaceVariant)
                        ) {
                            if (isUploadingOcr) {
                                CircularProgressIndicator(
                                    modifier = Modifier.size(18.dp),
                                    color = MaterialTheme.colorScheme.primary,
                                    strokeWidth = 2.dp
                                )
                            } else {
                                Icon(
                                    imageVector = Icons.Default.CameraAlt,
                                    contentDescription = "拍照或选图 OCR 识别",
                                    tint = MaterialTheme.colorScheme.primary,
                                    modifier = Modifier.size(18.dp)
                                )
                            }
                        }

                        DropdownMenu(
                            expanded = showOcrSourceMenu,
                            onDismissRequest = { showOcrSourceMenu = false },
                            modifier = Modifier.background(MaterialTheme.colorScheme.surface)
                        ) {
                            DropdownMenuItem(
                                text = { Text("📷 拍照识别票据/文档") },
                                onClick = {
                                    showOcrSourceMenu = false
                                    val granted = ContextCompat.checkSelfPermission(
                                        context,
                                        Manifest.permission.CAMERA
                                    ) == PackageManager.PERMISSION_GRANTED
                                    if (granted) {
                                        launchCameraCapture()
                                    } else {
                                        cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
                                    }
                                }
                            )
                            DropdownMenuItem(
                                text = { Text("🖼️ 从相册选择图片 OCR") },
                                onClick = {
                                    showOcrSourceMenu = false
                                    photoPickerLauncher.launch(
                                        PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
                                    )
                                }
                            )
                        }
                    }

                    // 2. [ / ] 快速指令面板按钮
                    Surface(
                        shape = CircleShape,
                        color = MaterialTheme.colorScheme.surfaceVariant,
                        modifier = Modifier
                            .size(38.dp)
                            .clickable(enabled = !isStreaming) { showCommandSheet = true }
                    ) {
                        Box(contentAlignment = Alignment.Center) {
                            Text(
                                text = "/",
                                fontWeight = FontWeight.ExtraBold,
                                fontFamily = FontFamily.Monospace,
                                fontSize = 16.sp,
                                color = SecondarySky
                            )
                        }
                    }

                    // 3. 多行自适应输入框
                    OutlinedTextField(
                        value = inputText,
                        onValueChange = { inputText = it },
                        placeholder = {
                            Text(
                                text = when {
                                    isUploadingOcr -> "📷 OCR 正在解析图片文字..."
                                    isStreaming -> "长任务执行中，可折叠查看日志..."
                                    else -> "输入问题、/指令 或 📷拍票据..."
                                },
                                fontSize = 13.sp
                            )
                        },
                        enabled = !isUploadingOcr,
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

                    // 4. 发送按钮
                    IconButton(
                        onClick = {
                            if (inputText.isNotBlank() && !isStreaming && !isUploadingOcr) {
                                val textToSend = inputText
                                inputText = ""
                                viewModel.sendMessage(textToSend)
                            }
                        },
                        enabled = inputText.isNotBlank() && !isStreaming && !isUploadingOcr,
                        modifier = Modifier
                            .size(44.dp)
                            .clip(CircleShape)
                            .background(
                                if (inputText.isNotBlank() && !isStreaming && !isUploadingOcr)
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

    // 快速指令面板 BottomSheet (Command Palette)
    if (showCommandSheet) {
        CommandPaletteSheet(
            commands = commands,
            onDismiss = { showCommandSheet = false },
            onSelectCommand = { cmd, sendImmediately ->
                showCommandSheet = false
                if (cmd.name == "/new") {
                    viewModel.createNewSession()
                } else if (sendImmediately) {
                    viewModel.sendMessage(cmd.name)
                } else {
                    inputText = if (cmd.name.startsWith("/")) "${cmd.name} " else cmd.name
                }
            }
        )
    }

    // 会话重命名弹窗
    if (showRenameDialog && currentSession != null) {
        RenameSessionDialog(
            currentTitle = currentSession?.displayTitle ?: "",
            onDismiss = { showRenameDialog = false },
            onConfirm = { newTitle ->
                showRenameDialog = false
                viewModel.renameCurrentSession(newTitle)
            }
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CommandPaletteSheet(
    commands: List<DashboardCommandItem>,
    onDismiss: () -> Unit,
    onSelectCommand: (DashboardCommandItem, Boolean) -> Unit
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var filterQuery by remember { mutableStateOf("") }

    val filtered = remember(commands, filterQuery) {
        if (filterQuery.isBlank()) commands
        else {
            val q = filterQuery.lowercase()
            commands.filter {
                it.name.lowercase().contains(q) || it.description.lowercase().contains(q)
            }
        }
    }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = MaterialTheme.colorScheme.surface
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp)
                .padding(bottom = 28.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Icon(
                        imageVector = Icons.Default.Terminal,
                        contentDescription = null,
                        tint = SecondarySky
                    )
                    Text(
                        text = "快捷指令控制台 (Command Palette)",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold
                    )
                }
                Text(
                    text = "${filtered.size} 项可用",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextMuted
                )
            }

            OutlinedTextField(
                value = filterQuery,
                onValueChange = { filterQuery = it },
                placeholder = { Text("搜索指令名称或功能描述...", fontSize = 13.sp) },
                leadingIcon = { Icon(Icons.Default.Search, contentDescription = null, modifier = Modifier.size(18.dp)) },
                singleLine = true,
                shape = RoundedCornerShape(12.dp),
                modifier = Modifier.fillMaxWidth()
            )

            LazyColumn(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 360.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                items(filtered) { cmd ->
                    Card(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clickable { onSelectCommand(cmd, false) },
                        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
                        shape = RoundedCornerShape(10.dp),
                        border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                    ) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 12.dp, vertical = 10.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Column(
                                modifier = Modifier.weight(1f),
                                verticalArrangement = Arrangement.spacedBy(4.dp)
                            ) {
                                Row(
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                                ) {
                                    Surface(
                                        color = PrimaryIndigo.copy(alpha = 0.15f),
                                        shape = RoundedCornerShape(6.dp)
                                    ) {
                                        Text(
                                            text = cmd.name,
                                            fontFamily = FontFamily.Monospace,
                                            fontWeight = FontWeight.Bold,
                                            fontSize = 12.sp,
                                            color = PrimaryIndigo,
                                            modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp)
                                        )
                                    }
                                    if (cmd.category.isNotBlank()) {
                                        Text(
                                            text = cmd.category,
                                            fontSize = 10.sp,
                                            color = TextMuted
                                        )
                                    }
                                }
                                if (cmd.description.isNotBlank()) {
                                    Text(
                                        text = cmd.description,
                                        style = MaterialTheme.typography.bodySmall,
                                        color = TextSecondary
                                    )
                                }
                            }

                            TextButton(
                                onClick = { onSelectCommand(cmd, true) }
                            ) {
                                Text("直发 ▶", fontSize = 12.sp, fontWeight = FontWeight.Bold, color = AccentEmerald)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun RenameSessionDialog(
    currentTitle: String,
    onDismiss: () -> Unit,
    onConfirm: (String) -> Unit
) {
    var titleInput by remember { mutableStateOf(currentTitle) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("重命名当前会话", fontWeight = FontWeight.Bold) },
        text = {
            OutlinedTextField(
                value = titleInput,
                onValueChange = { titleInput = it },
                label = { Text("会话标题") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
        },
        confirmButton = {
            Button(
                onClick = { onConfirm(titleInput) },
                enabled = titleInput.isNotBlank()
            ) {
                Text("保存")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("取消")
            }
        }
    )
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
    onSuggestClick: (String) -> Unit,
    onOpenCommandSheet: () -> Unit = {}
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
            text = "随时下发指令、拍照票据 OCR、拉取任务进度或触发多机协同调度。",
            style = MaterialTheme.typography.bodyMedium,
            color = TextSecondary,
            textAlign = androidx.compose.ui.text.style.TextAlign.Center,
            modifier = Modifier.padding(horizontal = 24.dp)
        )

        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(top = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.CenterHorizontally)
        ) {
            SuggestionChip(
                onClick = { onSuggestClick("帮我总结今天的待办事项和未读邮件") },
                label = { Text("总结今日待办与邮件", fontSize = 12.sp) }
            )
            SuggestionChip(
                onClick = onOpenCommandSheet,
                label = { Text("⚡ 快捷指令面板 (/)", fontSize = 12.sp) }
            )
        }
    }
}
