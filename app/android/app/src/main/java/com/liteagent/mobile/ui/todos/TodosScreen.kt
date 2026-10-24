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
            onSubmit = { title, project, desc, dueAt ->
                viewModel.createTodo(title, project, desc, dueAt) {
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
    onSubmit: (title: String, project: String?, desc: String?, dueAt: String?) -> Unit
) {
    var title by remember { mutableStateOf("") }
    var project by remember { mutableStateOf("") }
    var dueAt by remember { mutableStateOf("") }
    var description by remember { mutableStateOf("") }
    val focusManager = LocalFocusManager.current

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
        containerColor = SurfaceElevated,
        shape = RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 20.dp, vertical = 8.dp)
                .imePadding()
                .autoDismissKeyboardOnTap(focusManager),
            verticalArrangement = Arrangement.spacedBy(14.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "新建待办事项",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold
                )
                IconButton(onClick = onDismiss, modifier = Modifier.size(28.dp)) {
                    Icon(Icons.Default.Close, contentDescription = "关闭", tint = TextMuted)
                }
            }

            // 待办标题输入
            OutlinedTextField(
                value = title,
                onValueChange = { title = it },
                label = { Text("待办名称 (必填)") },
                placeholder = { Text("例如：完成 Sprint 2 会审审批真机验收") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = false,
                maxLines = 3,
                shape = RoundedCornerShape(12.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = PrimaryIndigo,
                    unfocusedBorderColor = SurfaceBorder
                )
            )

            // 所属项目
            OutlinedTextField(
                value = project,
                onValueChange = { project = it },
                label = { Text("所属项目 (可选)") },
                placeholder = { Text("例如：LiteAgent-Mobile") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                leadingIcon = {
                    Icon(Icons.Outlined.Folder, contentDescription = null, tint = TextMuted)
                },
                shape = RoundedCornerShape(12.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = PrimaryIndigo,
                    unfocusedBorderColor = SurfaceBorder
                )
            )

            // 截止日期
            OutlinedTextField(
                value = dueAt,
                onValueChange = { dueAt = it },
                label = { Text("截止日期 (可选)") },
                placeholder = { Text("例如：2026-10-25 18:00") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                leadingIcon = {
                    Icon(Icons.Outlined.CalendarToday, contentDescription = null, tint = TextMuted)
                },
                shape = RoundedCornerShape(12.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = PrimaryIndigo,
                    unfocusedBorderColor = SurfaceBorder
                )
            )

            // 详细说明
            OutlinedTextField(
                value = description,
                onValueChange = { description = it },
                label = { Text("详细说明 (可选)") },
                placeholder = { Text("添加更多任务细节或核验要点...") },
                modifier = Modifier.fillMaxWidth(),
                minLines = 2,
                maxLines = 4,
                shape = RoundedCornerShape(12.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = PrimaryIndigo,
                    unfocusedBorderColor = SurfaceBorder
                )
            )

            Spacer(modifier = Modifier.height(4.dp))

            // 提交按钮
            Button(
                onClick = {
                    if (title.isNotBlank()) {
                        onSubmit(title, project, description, dueAt)
                    }
                },
                enabled = title.isNotBlank() && !isCreating,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(48.dp),
                colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo),
                shape = RoundedCornerShape(12.dp)
            ) {
                if (isCreating) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(20.dp),
                        color = Color.White,
                        strokeWidth = 2.dp
                    )
                } else {
                    Text("立即创建", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                }
            }

            Spacer(modifier = Modifier.height(16.dp))
        }
    }
}
