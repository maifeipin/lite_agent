package com.liteagent.mobile.ui.todos

import androidx.compose.animation.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.horizontalScroll
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.liteagent.mobile.core.network.models.TodoItem
import com.liteagent.mobile.ui.common.autoDismissKeyboardOnTap
import com.liteagent.mobile.ui.theme.*

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TodosScreen(
    viewModel: TodosViewModel,
    modifier: Modifier = Modifier
) {
    val uiState by viewModel.uiState.collectAsState()
    val focusManager = LocalFocusManager.current
    val snackbarHostState = remember { SnackbarHostState() }

    var showCreateSheet by remember { mutableStateOf(false) }
    var itemToDelete by remember { mutableStateOf<TodoItem?>(null) }

    LaunchedEffect(uiState.errorMessage) {
        uiState.errorMessage?.let { msg ->
            snackbarHostState.showSnackbar(msg)
            viewModel.clearMessages()
        }
    }

    LaunchedEffect(uiState.actionSuccessMessage) {
        uiState.actionSuccessMessage?.let { msg ->
            snackbarHostState.showSnackbar(msg)
            viewModel.clearMessages()
        }
    }

    Scaffold(
        modifier = modifier
            .fillMaxSize()
            .autoDismissKeyboardOnTap(focusManager),
        containerColor = MaterialTheme.colorScheme.background,
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            TodosTopBar(
                isOffline = uiState.isOffline,
                isRefreshing = uiState.isRefreshing,
                onRefresh = { viewModel.refresh() }
            )
        },
        floatingActionButton = {
            FloatingActionButton(
                onClick = { showCreateSheet = true },
                containerColor = PrimaryIndigo,
                contentColor = Color.White,
                shape = RoundedCornerShape(16.dp),
                elevation = FloatingActionButtonDefaults.elevation(defaultElevation = 6.dp)
            ) {
                Icon(Icons.Default.Add, contentDescription = "新建待办")
            }
        }
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
        ) {
            // 离线模式提示条
            AnimatedVisibility(
                visible = uiState.isOffline,
                enter = expandVertically() + fadeIn(),
                exit = shrinkVertically() + fadeOut()
            ) {
                Surface(
                    color = AccentAmber.copy(alpha = 0.15f),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Icon(
                            imageVector = Icons.Default.CloudOff,
                            contentDescription = null,
                            tint = AccentAmber,
                            modifier = Modifier.size(18.dp)
                        )
                        Text(
                            text = "当前处于离线缓存模式，展示本地沙箱数据",
                            style = MaterialTheme.typography.bodySmall,
                            color = AccentAmber,
                            modifier = Modifier.weight(1f)
                        )
                        Text(
                            text = "点击刷新",
                            style = MaterialTheme.typography.bodySmall,
                            color = PrimaryIndigo,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.clickable { viewModel.refresh() }
                        )
                    }
                }
            }

            // 分类状态过滤 Chips
            TodoFilterChipsRow(
                currentFilter = uiState.filter,
                pendingCount = uiState.pendingCount,
                activeCount = uiState.activeCount,
                doneCount = uiState.doneCount,
                allCount = uiState.todos.size,
                onFilterSelected = { viewModel.setFilter(it) }
            )

            // 待办列表内容区
            if (uiState.isLoading && uiState.todos.isEmpty()) {
                Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(color = PrimaryIndigo)
                }
            } else if (uiState.filteredTodos.isEmpty()) {
                EmptyTodosView(
                    filter = uiState.filter,
                    onCreateClick = { showCreateSheet = true }
                )
            } else {
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 88.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    items(
                        items = uiState.filteredTodos,
                        key = { it.id }
                    ) { item ->
                        TodoCard(
                            item = item,
                            onToggleStatus = { viewModel.toggleTodoStatus(item) },
                            onDelete = { itemToDelete = item }
                        )
                    }
                }
            }
        }
    }

    // 新建待办 Bottom Sheet
    if (showCreateSheet) {
        CreateTodoBottomSheet(
            isCreating = uiState.isCreating,
            onDismiss = { showCreateSheet = false },
            onSubmit = { title, project, desc, dueAt, remindBeforeMins, remindIntervalMins, notifyEnabled, recurCron, priority, dispatchTarget ->
                viewModel.createTodo(
                    title = title,
                    project = project,
                    description = desc,
                    dueAt = dueAt,
                    remindBeforeMins = remindBeforeMins,
                    remindIntervalMins = remindIntervalMins,
                    notifyEnabled = notifyEnabled,
                    recurCron = recurCron,
                    priority = priority,
                    dispatchTarget = dispatchTarget
                ) {
                    showCreateSheet = false
                }
            }
        )
    }

    // 删除确认弹窗
    itemToDelete?.let { item ->
        AlertDialog(
            onDismissRequest = { itemToDelete = null },
            title = { Text("确认删除待办") },
            text = { Text("确定要删除「${item.title}」吗？此操作无法撤销。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        viewModel.deleteTodo(item.id)
                        itemToDelete = null
                    },
                    colors = ButtonDefaults.textButtonColors(contentColor = AccentRose)
                ) {
                    Text("删除", fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(onClick = { itemToDelete = null }) {
                    Text("取消")
                }
            },
            containerColor = SurfaceElevated
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TodosTopBar(
    isOffline: Boolean,
    isRefreshing: Boolean,
    onRefresh: () -> Unit
) {
    TopAppBar(
        title = {
            Column {
                Text(
                    text = "待办事项 (GTD)",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold
                )
                Text(
                    text = if (isOffline) "本地离线沙箱" else "服务端实时同步",
                    style = MaterialTheme.typography.bodySmall,
                    color = if (isOffline) AccentAmber else TextSecondary
                )
            }
        },
        actions = {
            IconButton(onClick = onRefresh, enabled = !isRefreshing) {
                if (isRefreshing) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(20.dp),
                        strokeWidth = 2.dp,
                        color = PrimaryIndigo
                    )
                } else {
                    Icon(Icons.Default.Refresh, contentDescription = "刷新待办")
                }
            }
        },
        colors = TopAppBarDefaults.topAppBarColors(
            containerColor = MaterialTheme.colorScheme.surface
        )
    )
}

@Composable
private fun TodoFilterChipsRow(
    currentFilter: TodoFilter,
    pendingCount: Int,
    activeCount: Int,
    doneCount: Int,
    allCount: Int,
    onFilterSelected: (TodoFilter) -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        TodoFilter.values().forEach { filter ->
            val count = when (filter) {
                TodoFilter.ALL -> allCount
                TodoFilter.PENDING -> pendingCount
                TodoFilter.ACTIVE -> activeCount
                TodoFilter.DONE -> doneCount
            }
            val isSelected = currentFilter == filter

            FilterChip(
                selected = isSelected,
                onClick = { onFilterSelected(filter) },
                label = {
                    Text(
                        text = "${filter.label} $count",
                        fontSize = 12.sp,
                        fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal
                    )
                },
                colors = FilterChipDefaults.filterChipColors(
                    selectedContainerColor = PrimaryIndigo.copy(alpha = 0.2f),
                    selectedLabelColor = PrimaryIndigo,
                    containerColor = SurfaceCard,
                    labelColor = TextSecondary
                ),
                border = FilterChipDefaults.filterChipBorder(
                    enabled = true,
                    selected = isSelected,
                    borderColor = if (isSelected) PrimaryIndigo else SurfaceBorder,
                    selectedBorderColor = PrimaryIndigo
                ),
                shape = RoundedCornerShape(20.dp)
            )
        }
    }
}

@Composable
private fun TodoCard(
    item: TodoItem,
    onToggleStatus: () -> Unit,
    onDelete: () -> Unit
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .clickable { onToggleStatus() },
        colors = CardDefaults.cardColors(
            containerColor = if (item.isCompleted) SurfaceCard.copy(alpha = 0.6f) else SurfaceCard
        ),
        shape = RoundedCornerShape(14.dp),
        border = androidx.compose.foundation.BorderStroke(
            1.dp,
            if (item.isCompleted) SurfaceBorder.copy(alpha = 0.4f) else SurfaceBorder
        )
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(14.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Checkbox 勾选框
            Box(
                modifier = Modifier
                    .size(24.dp)
                    .clip(CircleShape)
                    .background(if (item.isCompleted) AccentEmerald else Color.Transparent)
                    .border(
                        width = 1.5.dp,
                        color = if (item.isCompleted) AccentEmerald else TextMuted,
                        shape = CircleShape
                    ),
                contentAlignment = Alignment.Center
            ) {
                if (item.isCompleted) {
                    Icon(
                        imageVector = Icons.Default.Check,
                        contentDescription = "已完成",
                        tint = Color.White,
                        modifier = Modifier.size(16.dp)
                    )
                }
            }

            Spacer(modifier = Modifier.width(12.dp))

            // 中间标题与信息
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = item.title,
                    style = MaterialTheme.typography.bodyLarge,
                    fontWeight = if (item.isCompleted) FontWeight.Normal else FontWeight.SemiBold,
                    textDecoration = if (item.isCompleted) TextDecoration.LineThrough else TextDecoration.None,
                    color = if (item.isCompleted) TextMuted else TextPrimary,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )

                if (!item.description.isNullOrBlank()) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = item.description,
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis
                    )
                }

                // 标签栏 (项目 & 截止日期 & 进行中标识)
                if (!item.project.isNullOrBlank() || !item.dueAt.isNullOrBlank() || item.status.equals("active", ignoreCase = true)) {
                    Spacer(modifier = Modifier.height(6.dp))
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        if (!item.project.isNullOrBlank()) {
                            Surface(
                                shape = RoundedCornerShape(6.dp),
                                color = PrimaryIndigo.copy(alpha = 0.12f)
                            ) {
                                Text(
                                    text = "# ${item.project}",
                                    fontSize = 11.sp,
                                    color = PrimaryIndigo,
                                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                                )
                            }
                        }

                        if (!item.dueAt.isNullOrBlank()) {
                            Surface(
                                shape = RoundedCornerShape(6.dp),
                                color = SurfaceElevated
                            ) {
                                Row(
                                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(2.dp)
                                ) {
                                    Icon(
                                        imageVector = Icons.Outlined.CalendarToday,
                                        contentDescription = null,
                                        tint = TextSecondary,
                                        modifier = Modifier.size(10.dp)
                                    )
                                    Text(
                                        text = item.dueAt,
                                        fontSize = 11.sp,
                                        color = TextSecondary
                                    )
                                }
                            }
                        }

                        if (item.status.equals("active", ignoreCase = true)) {
                            Surface(
                                shape = RoundedCornerShape(6.dp),
                                color = SecondarySky.copy(alpha = 0.15f)
                            ) {
                                Text(
                                    text = "进行中",
                                    fontSize = 11.sp,
                                    color = SecondarySky,
                                    fontWeight = FontWeight.Medium,
                                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                                )
                            }
                        }
                    }
                }
            }

            Spacer(modifier = Modifier.width(8.dp))

            // 删除按钮
            IconButton(
                onClick = onDelete,
                modifier = Modifier.size(32.dp)
            ) {
                Icon(
                    imageVector = Icons.Outlined.Delete,
                    contentDescription = "删除待办",
                    tint = TextMuted.copy(alpha = 0.6f),
                    modifier = Modifier.size(18.dp)
                )
            }
        }
    }
}

@Composable
private fun EmptyTodosView(
    filter: TodoFilter,
    onCreateClick: () -> Unit
) {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp),
            modifier = Modifier.padding(32.dp)
        ) {
            Surface(
                shape = CircleShape,
                color = SurfaceElevated,
                modifier = Modifier.size(72.dp)
            ) {
                Box(contentAlignment = Alignment.Center) {
                    Icon(
                        imageVector = Icons.Outlined.FactCheck,
                        contentDescription = null,
                        tint = TextSecondary,
                        modifier = Modifier.size(36.dp)
                    )
                }
            }

            Text(
                text = when (filter) {
                    TodoFilter.ALL -> "太棒了，目前没有待办事项"
                    TodoFilter.PENDING -> "没有待处理的待办"
                    TodoFilter.ACTIVE -> "暂无正在进行的待办"
                    TodoFilter.DONE -> "还没有已完成的待办记录"
                },
                style = MaterialTheme.typography.bodyLarge,
                color = TextSecondary
            )

            Button(
                onClick = onCreateClick,
                colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo),
                shape = RoundedCornerShape(12.dp)
            ) {
                Icon(Icons.Default.Add, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(6.dp))
                Text("新建一条待办")
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CreateTodoBottomSheet(
    isCreating: Boolean,
    onDismiss: () -> Unit,
    onSubmit: (
        title: String,
        project: String?,
        desc: String?,
        dueAt: String?,
        remindBeforeMins: Int,
        remindIntervalMins: Int,
        notifyEnabled: Boolean,
        recurCron: String?,
        priority: Int,
        dispatchTarget: String?
    ) -> Unit
) {
    var title by remember { mutableStateOf("") }
    var description by remember { mutableStateOf("") }
    var showDescription by remember { mutableStateOf(false) }

    // 快捷日期与时间
    var dateType by remember { mutableStateOf("tomorrow") } // none, today, tomorrow, weekend, custom
    var customDate by remember { mutableStateOf("") }
    var timeType by remember { mutableStateOf("10:00") } // allday, 09:00, 10:00, 14:00, 18:00, custom
    var customTime by remember { mutableStateOf("") }

    // 高级提醒
    var remindBeforeMins by remember { mutableStateOf(40) } // 提前40分钟
    var remindIntervalMins by remember { mutableStateOf(5) } // 每5分钟
    var notifyEnabled by remember { mutableStateOf(true) }
    var dispatchTarget by remember { mutableStateOf("内置通道推送") }

    // 周期规则
    var recurType by remember { mutableStateOf("none") } // none, daily, workday, weekly, monthly

    // 优先级与项目
    var priority by remember { mutableStateOf(2) } // 0: P0, 1: P1, 2: P2, 3: P3
    var project by remember { mutableStateOf("会议") }
    var customProject by remember { mutableStateOf("") }

    val focusManager = LocalFocusManager.current
    val scrollState = rememberScrollState()

    val computedDueAt = remember(dateType, customDate, timeType, customTime) {
        if (dateType == "none") null
        else {
            val dateStr = when (dateType) {
                "today" -> SimpleDateFormat("yyyy-MM-dd", Locale.getDefault()).format(Date())
                "tomorrow" -> {
                    val cal = Calendar.getInstance().apply { add(Calendar.DAY_OF_YEAR, 1) }
                    SimpleDateFormat("yyyy-MM-dd", Locale.getDefault()).format(cal.time)
                }
                "weekend" -> {
                    val cal = Calendar.getInstance()
                    while (cal.get(Calendar.DAY_OF_WEEK) != Calendar.SATURDAY) {
                        cal.add(Calendar.DAY_OF_YEAR, 1)
                    }
                    SimpleDateFormat("yyyy-MM-dd", Locale.getDefault()).format(cal.time)
                }
                "custom" -> customDate.trim().ifEmpty { null }
                else -> null
            }
            if (dateStr == null) null
            else {
                val timeStr = when (timeType) {
                    "allday" -> "09:00:00"
                    "09:00" -> "09:00:00"
                    "10:00" -> "10:00:00"
                    "14:00" -> "14:00:00"
                    "18:00" -> "18:00:00"
                    "custom" -> {
                        val trimmed = customTime.trim()
                        if (trimmed.isEmpty()) "09:00:00"
                        else if (trimmed.count { it == ':' } == 1) "$trimmed:00"
                        else trimmed
                    }
                    else -> "09:00:00"
                }
                "$dateStr $timeStr"
            }
        }
    }

    val computedRecurCron = when (recurType) {
        "daily" -> "daily"
        "workday" -> "0 9 * * 1-5"
        "weekly" -> "weekly"
        "monthly" -> "0 9 1 * *"
        else -> null
    }

    val finalProject = if (project == "自定义") customProject.trim().ifEmpty { null } else project.trim().ifEmpty { null }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
        containerColor = SurfaceElevated,
        shape = RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .fillMaxHeight(0.9f)
                .padding(horizontal = 20.dp, vertical = 8.dp)
                .imePadding()
                .autoDismissKeyboardOnTap(focusManager)
        ) {
            // 头部标题栏
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        text = "新建待办事项",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold
                    )
                    Surface(
                        color = PrimaryIndigo.copy(alpha = 0.15f),
                        shape = RoundedCornerShape(6.dp)
                    ) {
                        Text(
                            text = "GTD 沉浸交互",
                            color = PrimaryIndigo,
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                        )
                    }
                }
                IconButton(onClick = onDismiss, modifier = Modifier.size(28.dp)) {
                    Icon(Icons.Default.Close, contentDescription = "关闭", tint = TextMuted)
                }
            }

            Spacer(modifier = Modifier.height(10.dp))

            // 主滚动表单
            Column(
                modifier = Modifier
                    .weight(1f)
                    .verticalScroll(scrollState),
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                // 待办标题输入
                OutlinedTextField(
                    value = title,
                    onValueChange = { title = it },
                    label = { Text("待办名称") },
                    placeholder = { Text("例如：明天10点钟的会议") },
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = false,
                    maxLines = 3,
                    shape = RoundedCornerShape(12.dp),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = PrimaryIndigo,
                        unfocusedBorderColor = SurfaceBorder
                    )
                )

                // 截止日期与时间节点
                Card(
                    colors = CardDefaults.cardColors(containerColor = SurfaceCard),
                    shape = RoundedCornerShape(12.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Text(
                            text = "📅 截止日期",
                            style = MaterialTheme.typography.labelMedium,
                            fontWeight = FontWeight.Bold,
                            color = TextSecondary
                        )
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .horizontalScroll(rememberScrollState()),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            listOf(
                                "tomorrow" to "明天 (推荐)",
                                "today" to "今天",
                                "weekend" to "本周末",
                                "custom" to "自定义日期",
                                "none" to "无截止"
                            ).forEach { (key, label) ->
                                FilterChip(
                                    selected = dateType == key,
                                    onClick = { dateType = key },
                                    label = { Text(label, fontSize = 12.sp) },
                                    colors = FilterChipDefaults.filterChipColors(
                                        selectedContainerColor = PrimaryIndigo.copy(alpha = 0.2f),
                                        selectedLabelColor = PrimaryIndigo
                                    )
                                )
                            }
                        }

                        if (dateType == "custom") {
                            OutlinedTextField(
                                value = customDate,
                                onValueChange = { customDate = it },
                                placeholder = { Text("格式：YYYY-MM-DD，如 2026-10-26") },
                                singleLine = true,
                                modifier = Modifier.fillMaxWidth(),
                                shape = RoundedCornerShape(8.dp)
                            )
                        }

                        if (dateType != "none") {
                            Spacer(modifier = Modifier.height(4.dp))
                            Text(
                                text = "⏰ 具体时间点",
                                style = MaterialTheme.typography.labelMedium,
                                fontWeight = FontWeight.Bold,
                                color = TextSecondary
                            )
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .horizontalScroll(rememberScrollState()),
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                listOf(
                                    "10:00" to "10:00 (推荐)",
                                    "09:00" to "09:00",
                                    "14:00" to "14:00",
                                    "18:00" to "18:00",
                                    "allday" to "全天",
                                    "custom" to "自定义时间"
                                ).forEach { (key, label) ->
                                    FilterChip(
                                        selected = timeType == key,
                                        onClick = { timeType = key },
                                        label = { Text(label, fontSize = 12.sp) },
                                        colors = FilterChipDefaults.filterChipColors(
                                            selectedContainerColor = PrimaryIndigo.copy(alpha = 0.2f),
                                            selectedLabelColor = PrimaryIndigo
                                        )
                                    )
                                }
                            }

                            if (timeType == "custom") {
                                OutlinedTextField(
                                    value = customTime,
                                    onValueChange = { customTime = it },
                                    placeholder = { Text("格式：HH:mm，如 10:30") },
                                    singleLine = true,
                                    modifier = Modifier.fillMaxWidth(),
                                    shape = RoundedCornerShape(8.dp)
                                )
                            }
                        }

                        computedDueAt?.let { due ->
                            Text(
                                text = "预计截止时间：$due",
                                style = MaterialTheme.typography.bodySmall,
                                color = PrimaryIndigo,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                    }
                }

                // 高级提醒控制 (提前提醒 + 循环间隔 + 推送通道)
                Card(
                    colors = CardDefaults.cardColors(containerColor = SurfaceCard),
                    shape = RoundedCornerShape(12.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(
                                text = "🔔 智能提醒开关与推送通道",
                                style = MaterialTheme.typography.labelMedium,
                                fontWeight = FontWeight.Bold,
                                color = TextSecondary
                            )
                            Switch(
                                checked = notifyEnabled,
                                onCheckedChange = { notifyEnabled = it }
                            )
                        }

                        if (notifyEnabled) {
                            Text(
                                text = "提前提醒时间：",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextMuted
                            )
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .horizontalScroll(rememberScrollState()),
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                listOf(
                                    40 to "提前40分钟 (推荐)",
                                    0 to "准时提醒",
                                    15 to "提前15分钟",
                                    30 to "提前30分钟",
                                    60 to "提前1小时"
                                ).forEach { (mins, label) ->
                                    FilterChip(
                                        selected = remindBeforeMins == mins,
                                        onClick = { remindBeforeMins = mins },
                                        label = { Text(label, fontSize = 12.sp) },
                                        colors = FilterChipDefaults.filterChipColors(
                                            selectedContainerColor = AccentAmber.copy(alpha = 0.2f),
                                            selectedLabelColor = AccentAmber
                                        )
                                    )
                                }
                            }

                            Text(
                                text = "重复提醒间隔：",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextMuted
                            )
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .horizontalScroll(rememberScrollState()),
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                listOf(
                                    5 to "每5分钟 (推荐)",
                                    0 to "单次提醒",
                                    10 to "每10分钟",
                                    15 to "每15分钟",
                                    30 to "每30分钟"
                                ).forEach { (interval, label) ->
                                    FilterChip(
                                        selected = remindIntervalMins == interval,
                                        onClick = { remindIntervalMins = interval },
                                        label = { Text(label, fontSize = 12.sp) },
                                        colors = FilterChipDefaults.filterChipColors(
                                            selectedContainerColor = AccentAmber.copy(alpha = 0.2f),
                                            selectedLabelColor = AccentAmber
                                        )
                                    )
                                }
                            }

                            Text(
                                text = "推送通知渠道：",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextMuted
                            )
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .horizontalScroll(rememberScrollState()),
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                listOf(
                                    "内置通道推送" to "内置系统推送",
                                    "Telegram" to "Telegram Bot",
                                    "邮件" to "邮件提醒"
                                ).forEach { (channel, label) ->
                                    FilterChip(
                                        selected = dispatchTarget == channel,
                                        onClick = { dispatchTarget = channel },
                                        label = { Text(label, fontSize = 12.sp) },
                                        colors = FilterChipDefaults.filterChipColors(
                                            selectedContainerColor = PrimaryIndigo.copy(alpha = 0.2f),
                                            selectedLabelColor = PrimaryIndigo
                                        )
                                    )
                                }
                            }
                        }
                    }
                }

                // 周期与重复规则
                Card(
                    colors = CardDefaults.cardColors(containerColor = SurfaceCard),
                    shape = RoundedCornerShape(12.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Text(
                            text = "🔄 周期与重复规则",
                            style = MaterialTheme.typography.labelMedium,
                            fontWeight = FontWeight.Bold,
                            color = TextSecondary
                        )
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .horizontalScroll(rememberScrollState()),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            listOf(
                                "none" to "不重复",
                                "daily" to "每天",
                                "workday" to "工作日 (周一至周五)",
                                "weekly" to "每周",
                                "monthly" to "每月"
                            ).forEach { (key, label) ->
                                FilterChip(
                                    selected = recurType == key,
                                    onClick = { recurType = key },
                                    label = { Text(label, fontSize = 12.sp) },
                                    colors = FilterChipDefaults.filterChipColors(
                                        selectedContainerColor = AccentGreen.copy(alpha = 0.2f),
                                        selectedLabelColor = AccentGreen
                                    )
                                )
                            }
                        }
                    }
                }

                // 优先级与所属项目
                Card(
                    colors = CardDefaults.cardColors(containerColor = SurfaceCard),
                    shape = RoundedCornerShape(12.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Text(
                            text = "🚩 优先级",
                            style = MaterialTheme.typography.labelMedium,
                            fontWeight = FontWeight.Bold,
                            color = TextSecondary
                        )
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            listOf(
                                0 to ("P0 紧急" to AccentRed),
                                1 to ("P1 高" to AccentAmber),
                                2 to ("P2 中" to Color(0xFFFBBF24)),
                                3 to ("P3 低" to TextSecondary)
                            ).forEach { (p, pair) ->
                                val (pLabel, pColor) = pair
                                FilterChip(
                                    selected = priority == p,
                                    onClick = { priority = p },
                                    label = { Text(pLabel, fontSize = 12.sp) },
                                    colors = FilterChipDefaults.filterChipColors(
                                        selectedContainerColor = pColor.copy(alpha = 0.2f),
                                        selectedLabelColor = pColor
                                    )
                                )
                            }
                        }

                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = "📁 所属项目",
                            style = MaterialTheme.typography.labelMedium,
                            fontWeight = FontWeight.Bold,
                            color = TextSecondary
                        )
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .horizontalScroll(rememberScrollState()),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            listOf("会议", "工作", "生活", "个人", "自定义").forEach { p ->
                                FilterChip(
                                    selected = project == p,
                                    onClick = { project = p },
                                    label = { Text(p, fontSize = 12.sp) },
                                    colors = FilterChipDefaults.filterChipColors(
                                        selectedContainerColor = PrimaryIndigo.copy(alpha = 0.2f),
                                        selectedLabelColor = PrimaryIndigo
                                    )
                                )
                            }
                        }
                        if (project == "自定义") {
                            OutlinedTextField(
                                value = customProject,
                                onValueChange = { customProject = it },
                                placeholder = { Text("输入自定义项目名称") },
                                singleLine = true,
                                modifier = Modifier.fillMaxWidth(),
                                shape = RoundedCornerShape(8.dp)
                            )
                        }
                    }
                }

                // 详细说明 (可折叠)
                if (showDescription) {
                    OutlinedTextField(
                        value = description,
                        onValueChange = { description = it },
                        label = { Text("详细说明与核验备忘") },
                        placeholder = { Text("添加任务细节或参会人员...") },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 2,
                        maxLines = 4,
                        shape = RoundedCornerShape(12.dp),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = PrimaryIndigo,
                            unfocusedBorderColor = SurfaceBorder
                        )
                    )
                } else {
                    TextButton(
                        onClick = { showDescription = true },
                        modifier = Modifier.align(Alignment.Start)
                    ) {
                        Icon(Icons.Default.Add, contentDescription = null, modifier = Modifier.size(16.dp))
                        Spacer(modifier = Modifier.width(4.dp))
                        Text("添加详细说明 / 备忘", fontSize = 13.sp)
                    }
                }

                Spacer(modifier = Modifier.height(8.dp))
            }

            Spacer(modifier = Modifier.height(10.dp))

            // 立即创建按钮
            Button(
                onClick = {
                    if (title.isNotBlank()) {
                        onSubmit(
                            title.trim(),
                            finalProject,
                            description.trim().ifEmpty { null },
                            computedDueAt,
                            remindBeforeMins,
                            remindIntervalMins,
                            notifyEnabled,
                            computedRecurCron,
                            priority,
                            dispatchTarget
                        )
                    }
                },
                enabled = title.isNotBlank() && !isCreating,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(50.dp),
                colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo),
                shape = RoundedCornerShape(14.dp)
            ) {
                if (isCreating) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(20.dp),
                        color = Color.White,
                        strokeWidth = 2.dp
                    )
                } else {
                    Text("立即创建待办", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                }
            }

            Spacer(modifier = Modifier.height(12.dp))
        }
    }
}
