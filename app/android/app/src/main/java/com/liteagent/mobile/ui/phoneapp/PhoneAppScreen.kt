package com.liteagent.mobile.ui.phoneapp

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
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.google.gson.GsonBuilder
import com.liteagent.mobile.core.network.models.*
import com.liteagent.mobile.ui.theme.*
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PhoneAppScreen(
    viewModel: PhoneAppViewModel,
    modifier: Modifier = Modifier
) {
    val uiState by viewModel.uiState.collectAsState()
    val snackbarHostState = remember { SnackbarHostState() }
    var showCreateDialog by remember { mutableStateOf(false) }
    var rejectTargetBatchId by remember { mutableStateOf<String?>(null) }

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
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        snackbarHost = { SnackbarHost(snackbarHostState) },
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = { showCreateDialog = true },
                containerColor = PrimaryIndigo,
                contentColor = Color.White,
                icon = { Icon(Icons.Default.Add, contentDescription = null) },
                text = { Text("新建筛选任务", fontWeight = FontWeight.Bold) }
            )
        }
    ) { innerPadding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding),
            contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 12.dp, bottom = 88.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            // 1. 受控设备在线感知与诊断状态栏
            item {
                ControlledDevicesStatusCard(
                    devices = uiState.devices,
                    isRefreshing = uiState.isRefreshing,
                    onRefresh = { viewModel.refresh() },
                    onResumeDevice = { devId -> viewModel.resumeDevice(devId) }
                )
            }

            // 2. 批次列表标题栏
            item {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = "自动化筛选批次 (${uiState.batches.size})",
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.Bold,
                        color = TextSecondary
                    )
                    if (uiState.pendingReviewCount > 0) {
                        Surface(
                            color = AccentAmber.copy(alpha = 0.2f),
                            shape = RoundedCornerShape(12.dp)
                        ) {
                            Text(
                                text = "🛡️ ${uiState.pendingReviewCount} 项待审核脚本",
                                color = AccentAmber,
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp)
                            )
                        }
                    }
                }
            }

            if (uiState.isLoading && uiState.batches.isEmpty()) {
                item {
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(vertical = 48.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        CircularProgressIndicator(color = PrimaryIndigo)
                    }
                }
            } else if (uiState.batches.isEmpty()) {
                item {
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
                        shape = RoundedCornerShape(14.dp),
                        border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
                    ) {
                        Column(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(28.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                            verticalArrangement = Arrangement.spacedBy(10.dp)
                        ) {
                            Icon(
                                imageVector = Icons.Default.PhoneAndroid,
                                contentDescription = null,
                                tint = PrimaryIndigo,
                                modifier = Modifier.size(36.dp)
                            )
                            Text(
                                text = "暂无真机自动化批次记录",
                                style = MaterialTheme.typography.titleMedium,
                                fontWeight = FontWeight.Bold
                            )
                            Text(
                                text = "点击右下角「新建筛选任务」可向受控手机下发京东/淘宝商品比价与三态筛选指令。",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextMuted,
                                textAlign = androidx.compose.ui.text.style.TextAlign.Center
                            )
                        }
                    }
                }
            } else {
                items(uiState.batches, key = { it.batchId }) { batch ->
                    PhoneBatchItemCard(
                        batch = batch,
                        isSubmitting = uiState.isSubmittingAction,
                        onOpenDetail = { viewModel.selectBatch(batch.batchId, startLivePolling = true) },
                        onApproveAndRun = { viewModel.approveAndExecuteBatch(batch.batchId) },
                        onExecute = { viewModel.executeBatch(batch.batchId) },
                        onReject = { rejectTargetBatchId = batch.batchId },
                        onCancel = { viewModel.cancelBatch(batch.batchId) }
                    )
                }
            }
        }
    }

    // 新建手机筛选任务弹窗
    if (showCreateDialog) {
        CreatePhoneBatchDialog(
            devices = uiState.devices,
            apps = uiState.apps,
            isSubmitting = uiState.isSubmittingAction,
            onDismiss = { showCreateDialog = false },
            onSubmit = { query, devId, appIds, mode, maxBudget, selfOp, freeShip ->
                viewModel.createBatch(
                    query = query,
                    deviceId = devId,
                    appIds = appIds,
                    mode = mode,
                    maxBudget = maxBudget,
                    selfOperatedOnly = selfOp,
                    freeShippingOnly = freeShip,
                    onSuccess = { showCreateDialog = false }
                )
            }
        )
    }

    // 驳回原因弹窗
    rejectTargetBatchId?.let { targetId ->
        RejectBatchDialog(
            batchId = targetId,
            onDismiss = { rejectTargetBatchId = null },
            onConfirm = { reason ->
                val id = targetId
                rejectTargetBatchId = null
                viewModel.rejectBatch(id, reason)
            }
        )
    }

    // 批次详情与三态商品筛选结果 Sheet
    uiState.selectedBatchDetail?.let { detail ->
        PhoneBatchDetailSheet(
            detail = detail,
            isSubmitting = uiState.isSubmittingAction,
            onDismiss = { viewModel.closeBatchDetail() },
            onApproveAndRun = { bId -> viewModel.approveAndExecuteBatch(bId) },
            onExecute = { bId -> viewModel.executeBatch(bId) },
            onReject = { bId -> rejectTargetBatchId = bId },
            onCancel = { bId -> viewModel.cancelBatch(bId) }
        )
    }
}

@Composable
private fun ControlledDevicesStatusCard(
    devices: List<PhoneDeviceItem>,
    isRefreshing: Boolean,
    onRefresh: () -> Unit,
    onResumeDevice: (String) -> Unit
) {
    val onlineCount = devices.count { it.isOnline }
    val offlineDev = devices.firstOrNull { !it.isOnline && !it.isPaused }
    val pausedDev = devices.firstOrNull { it.isPaused }

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
        shape = RoundedCornerShape(14.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, SurfaceBorder)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
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
                    Surface(
                        color = if (onlineCount > 0) AccentGreen.copy(alpha = 0.15f) else AccentRose.copy(alpha = 0.15f),
                        shape = CircleShape,
                        modifier = Modifier.size(34.dp)
                    ) {
                        Box(contentAlignment = Alignment.Center) {
                            Icon(
                                imageVector = Icons.Default.PhoneAndroid,
                                contentDescription = null,
                                tint = if (onlineCount > 0) AccentGreen else AccentRose,
                                modifier = Modifier.size(18.dp)
                            )
                        }
                    }
                    Column {
                        Text(
                            text = "Phone App 受控真机状态",
                            style = MaterialTheme.typography.titleSmall,
                            fontWeight = FontWeight.Bold
                        )
                        val statusSubtitle = when {
                            onlineCount > 0 -> "🟢 $onlineCount 台受控手机在线已就绪"
                            offlineDev != null -> {
                                val hint = offlineDev.offlineReason ?: offlineDev.statusText ?: "未连接 Worker"
                                "🔴 离线：$hint"
                            }
                            else -> "🔴 暂无在线受控设备"
                        }
                        Text(
                            text = statusSubtitle,
                            style = MaterialTheme.typography.bodySmall,
                            color = if (onlineCount > 0) AccentGreen else TextSecondary
                        )
                    }
                }

                IconButton(onClick = onRefresh, enabled = !isRefreshing) {
                    if (isRefreshing) {
                        CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                    } else {
                        Icon(Icons.Default.Refresh, contentDescription = "刷新设备与批次")
                    }
                }
            }

            if (devices.isNotEmpty()) {
                HorizontalDivider(color = SurfaceBorder)
                devices.forEach { dev ->
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
                            Box(
                                modifier = Modifier
                                    .size(8.dp)
                                    .clip(CircleShape)
                                    .background(
                                        when {
                                            dev.isOnline -> AccentGreen
                                            dev.isPaused -> AccentAmber
                                            else -> AccentRose
                                        }
                                    )
                            )
                            Text(
                                text = "${dev.displayName} (${dev.deviceId})",
                                style = MaterialTheme.typography.bodySmall,
                                fontWeight = FontWeight.SemiBold
                            )
                        }

                        if (dev.isPaused) {
                            Button(
                                onClick = { onResumeDevice(dev.deviceId) },
                                colors = ButtonDefaults.buttonColors(containerColor = AccentAmber),
                                contentPadding = PaddingValues(horizontal = 10.dp, vertical = 2.dp),
                                modifier = Modifier.height(28.dp)
                            ) {
                                Text("恢复就绪", fontSize = 11.sp, color = Color.Black, fontWeight = FontWeight.Bold)
                            }
                        } else {
                            Text(
                                text = dev.statusText ?: if (dev.isOnline) "联机就绪" else (dev.offlineReason ?: "离线"),
                                fontSize = 11.sp,
                                color = if (dev.isOnline) AccentGreen else TextMuted
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PhoneBatchItemCard(
    batch: PhoneBatchItem,
    isSubmitting: Boolean,
    onOpenDetail: () -> Unit,
    onApproveAndRun: () -> Unit,
    onExecute: () -> Unit,
    onReject: () -> Unit,
    onCancel: () -> Unit
) {
    val (statusLabel, statusColor) = remember(batch.status) {
        mapBatchStatus(batch.status)
    }
    val createdFormatted = remember(batch.createdAt) {
        if (batch.createdAt > 0) {
            SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date((batch.createdAt * 1000).toLong()))
        } else ""
    }

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onOpenDetail),
        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
        shape = RoundedCornerShape(14.dp),
        border = androidx.compose.foundation.BorderStroke(
            width = if (batch.status == "pending_review") 1.5.dp else 1.dp,
            color = if (batch.status == "pending_review") AccentAmber else SurfaceBorder
        )
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
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
                    Surface(
                        color = statusColor.copy(alpha = 0.18f),
                        shape = RoundedCornerShape(6.dp)
                    ) {
                        Text(
                            text = statusLabel,
                            color = statusColor,
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp)
                        )
                    }
                    Surface(
                        color = SurfaceElevated,
                        shape = RoundedCornerShape(6.dp)
                    ) {
                        Text(
                            text = "📦 ${batch.appIds.joinToString(",").ifEmpty { "jd" }}",
                            color = TextSecondary,
                            fontSize = 11.sp,
                            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp)
                        )
                    }
                }
                Text(
                    text = createdFormatted,
                    style = MaterialTheme.typography.bodySmall,
                    color = TextMuted
                )
            }

            Text(
                text = "搜索词：${batch.query}",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold
            )

            Text(
                text = "📱 设备: ${batch.deviceId} · 模式: ${batch.mode} · ID: ${batch.batchId.take(10)}",
                style = MaterialTheme.typography.bodySmall,
                color = TextMuted
            )

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End),
                verticalAlignment = Alignment.CenterVertically
            ) {
                OutlinedButton(
                    onClick = onOpenDetail,
                    contentPadding = PaddingValues(horizontal = 10.dp, vertical = 4.dp),
                    modifier = Modifier.height(32.dp)
                ) {
                    Text("详情/三态结果", fontSize = 12.sp)
                }

                when (batch.status) {
                    "pending_review" -> {
                        OutlinedButton(
                            onClick = onReject,
                            enabled = !isSubmitting,
                            colors = ButtonDefaults.outlinedButtonColors(contentColor = AccentRose),
                            contentPadding = PaddingValues(horizontal = 10.dp, vertical = 4.dp),
                            modifier = Modifier.height(32.dp)
                        ) {
                            Text("驳回 ❌", fontSize = 12.sp)
                        }
                        Button(
                            onClick = onApproveAndRun,
                            enabled = !isSubmitting,
                            colors = ButtonDefaults.buttonColors(containerColor = AccentAmber),
                            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp),
                            modifier = Modifier.height(32.dp)
                        ) {
                            Text("审核通过 🛡️", fontSize = 12.sp, color = Color.Black, fontWeight = FontWeight.Bold)
                        }
                    }
                    "approved" -> {
                        Button(
                            onClick = onExecute,
                            enabled = !isSubmitting,
                            colors = ButtonDefaults.buttonColors(containerColor = AccentEmerald),
                            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 4.dp),
                            modifier = Modifier.height(32.dp)
                        ) {
                            Text("立即执行 ▶️", fontSize = 12.sp, color = Color.White, fontWeight = FontWeight.Bold)
                        }
                    }
                    "queued", "running" -> {
                        Button(
                            onClick = onCancel,
                            enabled = !isSubmitting,
                            colors = ButtonDefaults.buttonColors(containerColor = AccentRose),
                            contentPadding = PaddingValues(horizontal = 10.dp, vertical = 4.dp),
                            modifier = Modifier.height(32.dp)
                        ) {
                            Text("取消执行 🛑", fontSize = 12.sp, color = Color.White)
                        }
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun PhoneBatchDetailSheet(
    detail: PhoneBatchDetailResponse,
    isSubmitting: Boolean,
    onDismiss: () -> Unit,
    onApproveAndRun: (String) -> Unit,
    onExecute: (String) -> Unit,
    onReject: (String) -> Unit,
    onCancel: (String) -> Unit
) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    val batch = detail.batch
    val batchId = batch?.batchId ?: ""
    val summary = detail.summary ?: PhoneBatchSummaryStats()
    val grouped = detail.groupedResults ?: PhoneGroupedResults()
    val (statusLabel, statusColor) = mapBatchStatus(batch?.status ?: "")

    var selectedGroupTab by remember { mutableStateOf(0) } // 0: 符合, 1: 待核实, 2: 不符合

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        sheetState = sheetState,
        containerColor = MaterialTheme.colorScheme.surface
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(max = 680.dp)
                .padding(horizontal = 16.dp)
                .padding(bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            // 顶栏信息
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = "📱 筛选批次 · ${batch?.query ?: ""}",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = "设备: ${batch?.deviceId ?: ""} | 应用: ${batch?.appIds?.joinToString(",") ?: "jd"}",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextMuted
                    )
                }

                Surface(
                    color = statusColor.copy(alpha = 0.18f),
                    shape = RoundedCornerShape(8.dp)
                ) {
                    Text(
                        text = statusLabel,
                        color = statusColor,
                        fontSize = 12.sp,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp)
                    )
                }
            }

            // 待审核脚本安全控制台
            if (batch?.status == "pending_review") {
                val prettyScripts = remember(batch.scripts) {
                    try {
                        GsonBuilder().setPrettyPrinting().create().toJson(batch.scripts ?: emptyMap<String, Any>())
                    } catch (_: Exception) {
                        batch.scripts?.toString() ?: "{}"
                    }
                }
                Card(
                    colors = CardDefaults.cardColors(containerColor = AccentAmber.copy(alpha = 0.1f)),
                    shape = RoundedCornerShape(12.dp),
                    border = androidx.compose.foundation.BorderStroke(1.dp, AccentAmber)
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(12.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Text(
                            text = "🛡️ AI 生成受限 DSL 脚本待安全审核",
                            style = MaterialTheme.typography.titleSmall,
                            fontWeight = FontWeight.Bold,
                            color = AccentAmber
                        )
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .heightIn(max = 140.dp)
                                .clip(RoundedCornerShape(8.dp))
                                .background(Color(0xFF070B14))
                                .padding(8.dp)
                                .verticalScroll(rememberScrollState())
                        ) {
                            Text(
                                text = prettyScripts,
                                fontFamily = FontFamily.Monospace,
                                fontSize = 11.sp,
                                color = Color(0xFF38BDF8)
                            )
                        }
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(10.dp, Alignment.End)
                        ) {
                            OutlinedButton(
                                onClick = { onReject(batchId) },
                                enabled = !isSubmitting,
                                colors = ButtonDefaults.outlinedButtonColors(contentColor = AccentRose)
                            ) {
                                Text("❌ 驳回脚本")
                            }
                            Button(
                                onClick = { onApproveAndRun(batchId) },
                                enabled = !isSubmitting,
                                colors = ButtonDefaults.buttonColors(containerColor = AccentEmerald)
                            ) {
                                Text("✅ 审核通过并立即执行", color = Color.White, fontWeight = FontWeight.Bold)
                            }
                        }
                    }
                }
            } else if (batch?.status == "approved") {
                Surface(
                    color = AccentEmerald.copy(alpha = 0.12f),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Row(
                        modifier = Modifier.padding(12.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = "脚本已通过安全校验，等待下发手机执行",
                            style = MaterialTheme.typography.bodySmall,
                            color = AccentEmerald,
                            modifier = Modifier.weight(1f)
                        )
                        Button(
                            onClick = { onExecute(batchId) },
                            enabled = !isSubmitting,
                            colors = ButtonDefaults.buttonColors(containerColor = AccentEmerald)
                        ) {
                            Text("▶️ 立即执行", color = Color.White, fontWeight = FontWeight.Bold)
                        }
                    }
                }
            } else if (batch?.status in listOf("queued", "running")) {
                Surface(
                    color = AccentAmber.copy(alpha = 0.12f),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Row(
                        modifier = Modifier.padding(12.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            modifier = Modifier.weight(1f)
                        ) {
                            CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp, color = AccentAmber)
                            Text(
                                text = "真机正在执行自动化筛选，每 3 秒自动同步...",
                                style = MaterialTheme.typography.bodySmall,
                                color = AccentAmber
                            )
                        }
                        TextButton(onClick = { onCancel(batchId) }, enabled = !isSubmitting) {
                            Text("🛑 取消", color = AccentRose, fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }

            // 三态商品筛选结果统计卡片 (可点击切换分组)
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                TriStateStatTab(
                    label = "✅ 符合条件",
                    count = summary.matchedCount.coerceAtLeast(grouped.matched.size),
                    color = AccentGreen,
                    selected = selectedGroupTab == 0,
                    onClick = { selectedGroupTab = 0 },
                    modifier = Modifier.weight(1f)
                )
                TriStateStatTab(
                    label = "🟡 待核实",
                    count = summary.needsVerificationCount.coerceAtLeast(grouped.needsVerification.size),
                    color = AccentAmber,
                    selected = selectedGroupTab == 1,
                    onClick = { selectedGroupTab = 1 },
                    modifier = Modifier.weight(1f)
                )
                TriStateStatTab(
                    label = "❌ 不符合",
                    count = summary.rejectedCount.coerceAtLeast(grouped.rejected.size),
                    color = AccentRose,
                    selected = selectedGroupTab == 2,
                    onClick = { selectedGroupTab = 2 },
                    modifier = Modifier.weight(1f)
                )
            }

            val currentItems = when (selectedGroupTab) {
                0 -> grouped.matched
                1 -> grouped.needsVerification
                else -> grouped.rejected
            }

            LazyColumn(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f, fill = false),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                if (currentItems.isEmpty()) {
                    item {
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(vertical = 32.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                text = "该分组下暂无商品记录",
                                style = MaterialTheme.typography.bodySmall,
                                color = TextMuted
                            )
                        }
                    }
                } else {
                    items(currentItems) { item ->
                        PhoneProductItemCard(
                            item = item,
                            accentColor = when (selectedGroupTab) {
                                0 -> AccentGreen
                                1 -> AccentAmber
                                else -> AccentRose
                            }
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun TriStateStatTab(
    label: String,
    count: Int,
    color: Color,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier
) {
    Surface(
        modifier = modifier.clickable(onClick = onClick),
        color = if (selected) color.copy(alpha = 0.2f) else SurfaceElevated,
        shape = RoundedCornerShape(10.dp),
        border = androidx.compose.foundation.BorderStroke(
            width = if (selected) 1.5.dp else 1.dp,
            color = if (selected) color else SurfaceBorder
        )
    ) {
        Column(
            modifier = Modifier.padding(vertical = 10.dp, horizontal = 6.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(
                text = "$count",
                style = MaterialTheme.typography.titleLarge,
                fontWeight = FontWeight.ExtraBold,
                color = color
            )
            Text(
                text = label,
                fontSize = 11.sp,
                fontWeight = FontWeight.SemiBold,
                color = if (selected) color else TextSecondary
            )
        }
    }
}

@Composable
private fun PhoneProductItemCard(
    item: PhoneProductResultItem,
    accentColor: Color
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = SurfaceCard),
        shape = RoundedCornerShape(10.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, accentColor.copy(alpha = 0.5f))
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
                verticalAlignment = Alignment.Top
            ) {
                Text(
                    text = item.title,
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Bold,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f)
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = item.displayPrice,
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.ExtraBold,
                    color = AccentRose
                )
            }

            // 店铺、销量、好评率、物流标签
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text(
                    text = "🏪 ${item.shopName ?: "未注明店铺"}",
                    fontSize = 11.sp,
                    color = TextSecondary
                )
                item.salesInfo?.rawText?.let {
                    Text(text = "📈 $it", fontSize = 11.sp, color = TextSecondary)
                }
                item.positiveRate?.let {
                    Text(text = "⭐ ${it}%", fontSize = 11.sp, color = TextSecondary)
                }
                item.logisticsInfo?.freeShipping?.let { free ->
                    Text(
                        text = if (free) "🚚 包邮" else "🚚 有运费",
                        fontSize = 11.sp,
                        color = if (free) AccentGreen else TextMuted
                    )
                }
            }

            // 规则判定依据
            val evals = item.evaluations.orEmpty()
            if (evals.isNotEmpty()) {
                Surface(
                    color = SurfaceElevated,
                    shape = RoundedCornerShape(6.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Column(
                        modifier = Modifier.padding(8.dp),
                        verticalArrangement = Arrangement.spacedBy(2.dp)
                    ) {
                        evals.forEach { (key, ev) ->
                            val icon = when (ev.status) {
                                "pass" -> "✅"
                                "fail" -> "❌"
                                else -> "🔍"
                            }
                            Text(
                                text = "$icon [$key]: ${ev.reason ?: ""}",
                                fontSize = 11.sp,
                                color = TextSecondary
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun CreatePhoneBatchDialog(
    devices: List<PhoneDeviceItem>,
    apps: List<PhoneCatalogAppItem>,
    isSubmitting: Boolean,
    onDismiss: () -> Unit,
    onSubmit: (
        query: String,
        deviceId: String,
        appIds: List<String>,
        mode: String,
        maxBudget: Double?,
        selfOperatedOnly: Boolean,
        freeShippingOnly: Boolean
    ) -> Unit
) {
    var query by remember { mutableStateOf("") }
    var selectedDeviceId by remember(devices) {
        mutableStateOf(devices.firstOrNull()?.deviceId ?: "c68a3cf2")
    }
    var selectedAppId by remember { mutableStateOf("jd") }
    var mode by remember { mutableStateOf("fixed") }
    var budgetInput by remember { mutableStateOf("") }
    var selfOperatedOnly by remember { mutableStateOf(true) }
    var freeShippingOnly by remember { mutableStateOf(true) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Text("➕ 新建真机商品筛选任务", fontWeight = FontWeight.Bold)
        },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it },
                    label = { Text("商品搜索关键词 *") },
                    placeholder = { Text("例如: Type-C 100W 编织快充线") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )

                Text("目标应用与执行模式", style = MaterialTheme.typography.labelMedium, color = TextSecondary)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilterChip(
                        selected = selectedAppId == "jd",
                        onClick = { selectedAppId = "jd" },
                        label = { Text("京东 (已验证)") }
                    )
                    FilterChip(
                        selected = mode == "fixed",
                        onClick = { mode = if (mode == "fixed") "custom_ai" else "fixed" },
                        label = { Text(if (mode == "fixed") "标准固定模式" else "AI生成DSL脚本") }
                    )
                }

                OutlinedTextField(
                    value = budgetInput,
                    onValueChange = { budgetInput = it },
                    label = { Text("最高预算上限 (元, 可选)") },
                    placeholder = { Text("例如: 50") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth()
                )

                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    FilterChip(
                        selected = selfOperatedOnly,
                        onClick = { selfOperatedOnly = !selfOperatedOnly },
                        label = { Text("优先自营/旗舰") }
                    )
                    FilterChip(
                        selected = freeShippingOnly,
                        onClick = { freeShippingOnly = !freeShippingOnly },
                        label = { Text("要求包邮") }
                    )
                }
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    val maxPrice = budgetInput.toDoubleOrNull()
                    onSubmit(
                        query,
                        selectedDeviceId,
                        listOf(selectedAppId),
                        mode,
                        maxPrice,
                        selfOperatedOnly,
                        freeShippingOnly
                    )
                },
                enabled = query.isNotBlank() && !isSubmitting,
                colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo)
            ) {
                Text("创建任务")
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
private fun RejectBatchDialog(
    batchId: String,
    onDismiss: () -> Unit,
    onConfirm: (String) -> Unit
) {
    var reason by remember { mutableStateOf("脚本不符合安全预期") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("驳回自动化脚本 ($batchId)", fontWeight = FontWeight.Bold) },
        text = {
            OutlinedTextField(
                value = reason,
                onValueChange = { reason = it },
                label = { Text("驳回原因") },
                modifier = Modifier.fillMaxWidth()
            )
        },
        confirmButton = {
            Button(
                onClick = { onConfirm(reason) },
                colors = ButtonDefaults.buttonColors(containerColor = AccentRose)
            ) {
                Text("确认驳回", color = Color.White)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("取消")
            }
        }
    )
}

private fun mapBatchStatus(status: String): Pair<String, Color> {
    return when (status) {
        "draft" -> "草稿" to TextMuted
        "pending_review" -> "待审核 🛡️" to AccentAmber
        "approved" -> "已批准" to SecondarySky
        "queued" -> "排队中" to TextSecondary
        "running" -> "执行中 ⏳" to AccentAmber
        "succeeded" -> "成功完成 ✅" to AccentGreen
        "partial_failed" -> "部分失败 ⚠️" to AccentAmber
        "failed", "rejected" -> "失败/已驳回 ❌" to AccentRose
        "cancel_requested", "cancelled" -> "已取消 🛑" to TextMuted
        else -> status.ifBlank { "未知" } to TextMuted
    }
}
