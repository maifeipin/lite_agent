package com.liteagent.mobile.ui.meetings

import androidx.compose.animation.*
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.liteagent.mobile.core.network.models.MeetingDetail
import com.liteagent.mobile.core.network.models.MeetingEvent
import com.liteagent.mobile.core.network.models.MeetingItem
import com.liteagent.mobile.ui.common.autoDismissKeyboardOnTap
import com.liteagent.mobile.ui.theme.*
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MeetingsScreen(
    viewModel: MeetingsViewModel,
    modifier: Modifier = Modifier
) {
    val uiState by viewModel.uiState.collectAsState()
    val snackbarHostState = remember { SnackbarHostState() }

    var decisionActionTarget by remember { mutableStateOf<String?>(null) } // "approve", "revise", "reject"

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
        modifier = modifier.fillMaxSize(),
        containerColor = MaterialTheme.colorScheme.background,
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            text = "会审决策中枢",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.Bold
                        )
                        Text(
                            text = if (uiState.waitingCount > 0) "有 ${uiState.waitingCount} 项待裁决议题" else "多方研讨与掌上裁决",
                            style = MaterialTheme.typography.bodySmall,
                            color = if (uiState.waitingCount > 0) AccentAmber else TextSecondary
                        )
                    }
                },
                actions = {
                    IconButton(onClick = { viewModel.refresh() }, enabled = !uiState.isRefreshing) {
                        if (uiState.isRefreshing) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(20.dp),
                                strokeWidth = 2.dp,
                                color = PrimaryIndigo
                            )
                        } else {
                            Icon(Icons.Default.Refresh, contentDescription = "刷新会审列表")
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface
                )
            )
        }
    ) { innerPadding ->
        if (uiState.isLoading && uiState.meetings.isEmpty()) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding),
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator(color = PrimaryIndigo)
            }
        } else if (uiState.meetings.isEmpty()) {
            EmptyMeetingsView(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding),
                onRefresh = { viewModel.refresh() }
            )
        } else {
            LazyColumn(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding),
                contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp)
            ) {
                // 置顶「待裁决审批」卡片
                if (uiState.awaitingMeetings.isNotEmpty()) {
                    item {
                        PinnedAwaitingSection(
                            awaitingMeetings = uiState.awaitingMeetings,
                            onSelect = { viewModel.selectMeeting(it.id) }
                        )
                    }
                }

                // 全部会审议题列表
                item {
                    Text(
                        text = "全部会审记录 (${uiState.meetings.size})",
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.Bold,
                        color = TextSecondary,
                        modifier = Modifier.padding(top = 4.dp, bottom = 2.dp)
                    )
                }

                items(
                    items = uiState.meetings,
                    key = { it.id }
                ) { meeting ->
                    MeetingItemCard(
                        meeting = meeting,
                        onClick = { viewModel.selectMeeting(meeting.id) }
                    )
                }
            }
        }
    }

    // 会审详情 Sheet / Full-Screen Modal
    uiState.selectedMeeting?.let { detail ->
        MeetingDetailSheet(
            detail = detail,
            isSubmitting = uiState.isSubmittingDecision,
            onDismiss = { viewModel.closeMeetingDetail() },
            onStartDecision = { action -> decisionActionTarget = action }
        )
    }

    // 审批裁决批注确认弹窗
    decisionActionTarget?.let { action ->
        val currentMeeting = uiState.selectedMeeting
        if (currentMeeting != null) {
            DecisionConfirmDialog(
                action = action,
                meetingTitle = currentMeeting.title,
                isSubmitting = uiState.isSubmittingDecision,
                onDismiss = { decisionActionTarget = null },
                onConfirm = { note ->
                    viewModel.decideMeeting(
                        meetingId = currentMeeting.id,
                        decision = action,
                        note = note,
                        expectedVersion = currentMeeting.lastSeq,
                        onSuccess = {
                            decisionActionTarget = null
                        }
                    )
                }
            )
        }
    }

    // 409 Conflict 版本冲突提示弹窗
    uiState.conflictErrorMessage?.let { conflictMsg ->
        AlertDialog(
            onDismissRequest = { viewModel.clearMessages() },
            icon = {
                Icon(
                    imageVector = Icons.Default.WarningAmber,
                    contentDescription = null,
                    tint = AccentAmber,
                    modifier = Modifier.size(36.dp)
                )
            },
            title = { Text("版本冲突拦截提醒", fontWeight = FontWeight.Bold) },
            text = { Text(conflictMsg) },
            confirmButton = {
                Button(
                    onClick = { viewModel.clearMessages() },
                    colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo)
                ) {
                    Text("查看最新会审详情")
                }
            },
            containerColor = SurfaceElevated
        )
    }
}

/**
 * 顶部高亮置顶卡片：展示待当前管理者审批的紧急会审
 */
@Composable
private fun PinnedAwaitingSection(
    awaitingMeetings: List<MeetingItem>,
    onSelect: (MeetingItem) -> Unit
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = AccentAmber.copy(alpha = 0.08f)
        ),
        shape = RoundedCornerShape(16.dp),
        border = androidx.compose.foundation.BorderStroke(1.5.dp, AccentAmber)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Surface(
                        shape = CircleShape,
                        color = AccentAmber,
                        modifier = Modifier.size(24.dp)
                    ) {
                        Box(contentAlignment = Alignment.Center) {
                            Icon(
                                imageVector = Icons.Default.Gavel,
                                contentDescription = null,
                                tint = Color.Black,
                                modifier = Modifier.size(14.dp)
                            )
                        }
                    }
                    Text(
                        text = "待裁决审批",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                        color = AccentAmber
                    )
                }

                Surface(
                    shape = RoundedCornerShape(12.dp),
                    color = AccentAmber.copy(alpha = 0.2f)
                ) {
                    Text(
                        text = "${awaitingMeetings.size} 项待办",
                        fontSize = 11.sp,
                        fontWeight = FontWeight.Bold,
                        color = AccentAmber,
                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp)
                    )
                }
            }

            Text(
                text = "以下议题各方研讨已就绪，等待最终决策：",
                style = MaterialTheme.typography.bodySmall,
                color = TextSecondary
            )

            awaitingMeetings.forEach { meeting ->
                Surface(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(12.dp))
                        .clickable { onSelect(meeting) },
                    color = SurfaceElevated,
                    shape = RoundedCornerShape(12.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                ) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = meeting.title,
                                style = MaterialTheme.typography.bodyMedium,
                                fontWeight = FontWeight.SemiBold,
                                color = TextPrimary,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                            Spacer(modifier = Modifier.height(2.dp))
                            Text(
                                text = "第 ${meeting.round} 轮研讨 • 编号 ${meeting.id}",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextMuted
                            )
                        }

                        Button(
                            onClick = { onSelect(meeting) },
                            colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo),
                            shape = RoundedCornerShape(8.dp),
                            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp)
                        ) {
                            Text("立即裁决", fontSize = 12.sp, fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }
        }
    }
}

/**
 * 普通会审卡片
 */
@Composable
private fun MeetingItemCard(
    meeting: MeetingItem,
    onClick: () -> Unit
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .clickable { onClick() },
        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
        shape = RoundedCornerShape(14.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                MeetingStateBadge(state = meeting.state)

                Text(
                    text = "第 ${meeting.round} 轮",
                    fontSize = 12.sp,
                    color = TextSecondary,
                    fontWeight = FontWeight.Medium
                )
            }

            Text(
                text = meeting.title,
                style = MaterialTheme.typography.bodyLarge,
                fontWeight = FontWeight.SemiBold,
                color = TextPrimary,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "ID: ${meeting.id}",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextMuted
                )

                if (meeting.updatedAt > 0) {
                    Text(
                        text = formatTime(meeting.updatedAt),
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }
            }
        }
    }
}

/**
 * 状态胶囊标签
 */
@Composable
fun MeetingStateBadge(state: String) {
    val (bgColor, textColor, text) = when (state.lowercase()) {
        "awaiting_approval", "waiting" -> Triple(AccentAmber.copy(alpha = 0.18f), AccentAmber, "等待审批")
        "open" -> Triple(PrimaryIndigo.copy(alpha = 0.18f), PrimaryIndigo, "讨论中")
        "approved" -> Triple(AccentEmerald.copy(alpha = 0.18f), AccentEmerald, "已批准")
        "rejected" -> Triple(AccentRose.copy(alpha = 0.18f), AccentRose, "已否决")
        "changes_requested" -> Triple(SecondarySky.copy(alpha = 0.18f), SecondarySky, "要求修改")
        else -> Triple(SurfaceElevated, TextSecondary, state)
    }

    Surface(
        shape = RoundedCornerShape(6.dp),
        color = bgColor
    ) {
        Text(
            text = text,
            fontSize = 11.sp,
            fontWeight = FontWeight.Bold,
            color = textColor,
            modifier = Modifier.padding(horizontal = 7.dp, vertical = 3.dp)
        )
    }
}

/**
 * 会审详情 Full-Screen ModalBottomSheet
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun MeetingDetailSheet(
    detail: MeetingDetail,
    isSubmitting: Boolean,
    onDismiss: () -> Unit,
    onStartDecision: (String) -> Unit
) {
    val scrollState = rememberScrollState()

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
        containerColor = SurfaceElevated,
        shape = RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .fillMaxHeight(0.88f)
        ) {
            // 详情头部
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 20.dp, vertical = 12.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.weight(1f)
                ) {
                    MeetingStateBadge(state = detail.state)
                    Text(
                        text = detail.title,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }

                IconButton(onClick = onDismiss, modifier = Modifier.size(28.dp)) {
                    Icon(Icons.Default.Close, contentDescription = "关闭", tint = TextMuted)
                }
            }

            HorizontalDivider(color = SurfaceBorder)

            // 可滚动内容区域
            Column(
                modifier = Modifier
                    .weight(1f)
                    .verticalScroll(scrollState)
                    .padding(horizontal = 20.dp, vertical = 14.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                // 议题摘要
                if (!detail.brief.isNullOrBlank()) {
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
                        shape = RoundedCornerShape(12.dp),
                        border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                    ) {
                        Column(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(14.dp),
                            verticalArrangement = Arrangement.spacedBy(6.dp)
                        ) {
                            Text(
                                text = "📋 议题摘要与背景",
                                style = MaterialTheme.typography.labelMedium,
                                fontWeight = FontWeight.Bold,
                                color = TextSecondary
                            )
                            Text(
                                text = detail.brief,
                                style = MaterialTheme.typography.bodyMedium,
                                color = TextPrimary
                            )
                        }
                    }
                }

                // 参会委员会成员
                if (detail.participants.isNotEmpty()) {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(
                            text = "👥 委员会成员 (${detail.participants.size})",
                            style = MaterialTheme.typography.labelMedium,
                            fontWeight = FontWeight.Bold,
                            color = TextSecondary
                        )

                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            detail.participants.forEach { p ->
                                Surface(
                                    shape = RoundedCornerShape(20.dp),
                                    color = SurfaceCard,
                                    border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                                ) {
                                    Row(
                                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 5.dp),
                                        verticalAlignment = Alignment.CenterVertically,
                                        horizontalArrangement = Arrangement.spacedBy(6.dp)
                                    ) {
                                        Box(
                                            modifier = Modifier
                                                .size(8.dp)
                                                .clip(CircleShape)
                                                .background(if (p.online) AccentEmerald else TextMuted)
                                        )
                                        Text(
                                            text = p.name,
                                            fontSize = 12.sp,
                                            color = TextPrimary
                                        )
                                    }
                                }
                            }
                        }
                    }
                }

                // 多轮研讨与裁决时间线
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text(
                        text = "⏱️ 会审研讨过程时间线 (${detail.events.size})",
                        style = MaterialTheme.typography.labelMedium,
                        fontWeight = FontWeight.Bold,
                        color = TextSecondary
                    )

                    if (detail.events.isEmpty()) {
                        Text(
                            text = "暂无详细流转记录",
                            style = MaterialTheme.typography.bodySmall,
                            color = TextMuted
                        )
                    } else {
                        detail.events.forEach { event ->
                            TimelineEventCard(event = event)
                        }
                    }
                }
            }

            // 底部悬浮/停靠裁决操作栏 (仅在待裁决状态出现)
            if (detail.isAwaitingApproval) {
                Surface(
                    color = MaterialTheme.colorScheme.surface,
                    tonalElevation = 8.dp,
                    border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 12.dp)
                            .navigationBarsPadding(),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Text(
                            text = "掌上审批裁决：",
                            style = MaterialTheme.typography.labelMedium,
                            fontWeight = FontWeight.Bold,
                            color = TextSecondary
                        )

                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            // 批准
                            Button(
                                onClick = { onStartDecision("approve") },
                                enabled = !isSubmitting,
                                colors = ButtonDefaults.buttonColors(containerColor = AccentEmerald),
                                shape = RoundedCornerShape(10.dp),
                                modifier = Modifier.weight(1f)
                            ) {
                                Icon(Icons.Default.Check, contentDescription = null, modifier = Modifier.size(16.dp))
                                Spacer(modifier = Modifier.width(4.dp))
                                Text("批 准", fontWeight = FontWeight.Bold)
                            }

                            // 要求修改
                            Button(
                                onClick = { onStartDecision("revise") },
                                enabled = !isSubmitting,
                                colors = ButtonDefaults.buttonColors(containerColor = AccentAmber),
                                shape = RoundedCornerShape(10.dp),
                                modifier = Modifier.weight(1f)
                            ) {
                                Icon(Icons.Default.Edit, contentDescription = null, modifier = Modifier.size(16.dp))
                                Spacer(modifier = Modifier.width(4.dp))
                                Text("要求修改", fontWeight = FontWeight.Bold, color = Color.Black)
                            }

                            // 否决
                            Button(
                                onClick = { onStartDecision("reject") },
                                enabled = !isSubmitting,
                                colors = ButtonDefaults.buttonColors(containerColor = AccentRose),
                                shape = RoundedCornerShape(10.dp),
                                modifier = Modifier.weight(1f)
                            ) {
                                Icon(Icons.Default.Close, contentDescription = null, modifier = Modifier.size(16.dp))
                                Spacer(modifier = Modifier.width(4.dp))
                                Text("否 决", fontWeight = FontWeight.Bold)
                            }
                        }
                    }
                }
            }
        }
    }
}

/**
 * 会审时间线单个事件卡片
 */
@Composable
private fun TimelineEventCard(event: MeetingEvent) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
        shape = RoundedCornerShape(10.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder.copy(alpha = 0.6f))
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    Text(
                        text = event.actor,
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Bold,
                        color = TextPrimary
                    )

                    Surface(
                        shape = RoundedCornerShape(4.dp),
                        color = SurfaceElevated
                    ) {
                        Text(
                            text = "第 ${event.round} 轮",
                            fontSize = 10.sp,
                            color = TextSecondary,
                            modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp)
                        )
                    }
                }

                // 事件类型或投票意见徽章
                when (event.kind) {
                    "review" -> {
                        val pos = event.reviewPosition ?: ""
                        val (posBg, posColor, posText) = when (pos.lowercase()) {
                            "support", "approve" -> Triple(AccentEmerald.copy(alpha = 0.2f), AccentEmerald, "支持")
                            "oppose", "reject" -> Triple(AccentRose.copy(alpha = 0.2f), AccentRose, "反对")
                            "revise" -> Triple(AccentAmber.copy(alpha = 0.2f), AccentAmber, "修改")
                            else -> Triple(SurfaceElevated, TextSecondary, pos)
                        }
                        Surface(shape = RoundedCornerShape(4.dp), color = posBg) {
                            Text(
                                text = "评审: $posText",
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Bold,
                                color = posColor,
                                modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                            )
                        }
                    }
                    "decision" -> {
                        val dec = event.decisionValue ?: ""
                        Surface(shape = RoundedCornerShape(4.dp), color = AccentEmerald.copy(alpha = 0.2f)) {
                            Text(
                                text = "最终裁决: $dec",
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Bold,
                                color = AccentEmerald,
                                modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                            )
                        }
                    }
                    "approval_requested" -> {
                        Surface(shape = RoundedCornerShape(4.dp), color = AccentAmber.copy(alpha = 0.2f)) {
                            Text(
                                text = "申请审批",
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Bold,
                                color = AccentAmber,
                                modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                            )
                        }
                    }
                    else -> {
                        Text(
                            text = event.kind,
                            fontSize = 11.sp,
                            color = TextMuted
                        )
                    }
                }
            }

            // 内容文本
            val contentText = event.reviewText ?: (event.body["note"] as? String) ?: (event.body["text"] as? String)
            if (!contentText.isNullOrBlank()) {
                Text(
                    text = contentText,
                    style = MaterialTheme.typography.bodySmall,
                    color = TextPrimary
                )
            }
        }
    }
}

/**
 * 掌上裁决操作确认弹窗 (附批注)
 */
@Composable
private fun DecisionConfirmDialog(
    action: String,
    meetingTitle: String,
    isSubmitting: Boolean,
    onDismiss: () -> Unit,
    onConfirm: (note: String?) -> Unit
) {
    var note by remember { mutableStateOf("") }
    val focusManager = LocalFocusManager.current

    val (actionName, actionColor) = when (action) {
        "approve" -> "批准" to AccentEmerald
        "revise" -> "要求修改" to AccentAmber
        "reject" -> "否决" to AccentRose
        else -> action to PrimaryIndigo
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Text(text = "确认$actionName会审议题", fontWeight = FontWeight.Bold)
        },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .autoDismissKeyboardOnTap(focusManager),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text(
                    text = "议题：$meetingTitle",
                    style = MaterialTheme.typography.bodySmall,
                    color = TextSecondary
                )

                OutlinedTextField(
                    value = note,
                    onValueChange = { note = it },
                    label = { Text("审批批注 / 修改要求意见 (可选)") },
                    placeholder = { Text("例如：方案可行，准予发布 / 需补充回滚演练步骤...") },
                    modifier = Modifier.fillMaxWidth(),
                    minLines = 3,
                    maxLines = 5,
                    shape = RoundedCornerShape(10.dp),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = actionColor,
                        unfocusedBorderColor = SurfaceBorder
                    )
                )
            }
        },
        confirmButton = {
            Button(
                onClick = { onConfirm(note) },
                enabled = !isSubmitting,
                colors = ButtonDefaults.buttonColors(containerColor = actionColor),
                shape = RoundedCornerShape(8.dp)
            ) {
                if (isSubmitting) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(16.dp),
                        strokeWidth = 2.dp,
                        color = Color.White
                    )
                } else {
                    Text("确认提交$actionName", fontWeight = FontWeight.Bold, color = if (action == "revise") Color.Black else Color.White)
                }
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !isSubmitting) {
                Text("取消")
            }
        },
        containerColor = SurfaceElevated
    )
}

@Composable
private fun EmptyMeetingsView(
    modifier: Modifier = Modifier,
    onRefresh: () -> Unit
) {
    Box(
        modifier = modifier,
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
                        imageVector = Icons.Default.Groups,
                        contentDescription = null,
                        tint = TextSecondary,
                        modifier = Modifier.size(36.dp)
                    )
                }
            }

            Text(
                text = "暂无会审研讨记录",
                style = MaterialTheme.typography.bodyLarge,
                color = TextSecondary
            )

            Button(
                onClick = onRefresh,
                colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo),
                shape = RoundedCornerShape(12.dp)
            ) {
                Icon(Icons.Default.Refresh, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(modifier = Modifier.width(6.dp))
                Text("刷新列表")
            }
        }
    }
}

private fun formatTime(epochSecondsOrMillis: Long): String {
    val millis = if (epochSecondsOrMillis < 10000000000L) epochSecondsOrMillis * 1000 else epochSecondsOrMillis
    val sdf = SimpleDateFormat("MM-dd HH:mm", Locale.getDefault())
    return sdf.format(Date(millis))
}
