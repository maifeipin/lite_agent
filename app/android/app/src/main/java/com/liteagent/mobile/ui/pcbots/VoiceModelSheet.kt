package com.liteagent.mobile.ui.pcbots

import androidx.compose.foundation.BorderStroke
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.liteagent.mobile.core.voice.OfflineVoiceModelId
import com.liteagent.mobile.core.voice.OfflineVoiceModelManager
import com.liteagent.mobile.core.voice.OfflineVoiceResult
import com.liteagent.mobile.ui.theme.PrimaryIndigo
import com.liteagent.mobile.ui.theme.SurfaceElevated
import com.liteagent.mobile.ui.theme.TextMuted
import com.liteagent.mobile.ui.theme.TextPrimary
import com.liteagent.mobile.ui.theme.TextSecondary
import kotlinx.coroutines.launch
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VoiceModelSheet(
    modelManager: OfflineVoiceModelManager,
    mirrorBaseUrl: String?,
    lastResult: OfflineVoiceResult?,
    onModelDeleted: suspend (OfflineVoiceModelId) -> Unit,
    onDismiss: () -> Unit
) {
    val selectedModel by modelManager.selectedModel.collectAsState()
    val installedIds by modelManager.installedIds.collectAsState()
    val downloadProgress by modelManager.downloadProgress.collectAsState()
    val scope = rememberCoroutineScope()
    var statusMessage by remember { mutableStateOf<String?>(null) }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        containerColor = MaterialTheme.colorScheme.surface
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp)
                .padding(bottom = 28.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = "离线语音模型管理（飞行模式可用）",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold,
                        color = TextPrimary
                    )
                    Text(
                        text = "下载一次永久离线识别，试用后可随时一键删除释放空间",
                        style = MaterialTheme.typography.bodySmall,
                        color = TextSecondary
                    )
                }
                IconButton(onClick = onDismiss) {
                    Icon(Icons.Default.Close, contentDescription = "关闭")
                }
            }

            if (lastResult != null) {
                Surface(
                    color = PrimaryIndigo.copy(alpha = 0.14f),
                    shape = RoundedCornerShape(10.dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        text = String.format(
                            Locale.US,
                            "上次真机实测 · %s · 音频 %.1fs · 推理 %.2fs (RTF %.2f)",
                            lastResult.modelId.displayName.substringBefore("（"),
                            lastResult.audioDurationSec,
                            lastResult.inferenceSec,
                            lastResult.rtf
                        ),
                        style = MaterialTheme.typography.labelMedium,
                        color = PrimaryIndigo,
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)
                    )
                }
            }

            if (!statusMessage.isNullOrBlank()) {
                Text(
                    text = statusMessage ?: "",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error
                )
            }

            OfflineVoiceModelId.values().forEach { spec ->
                val isInstalled = installedIds.contains(spec)
                val isSelected = selectedModel == spec
                val isDownloadingThis = downloadProgress?.modelId == spec
                val installedMb = remember(installedIds, spec) {
                    if (isInstalled) modelManager.getInstalledSizeMb(spec) else 0L
                }

                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = if (isSelected) SurfaceElevated else MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.35f)
                    ),
                    border = if (isSelected) BorderStroke(1.dp, PrimaryIndigo) else null,
                    shape = RoundedCornerShape(14.dp),
                    modifier = Modifier.fillMaxWidth()
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
                            Column(modifier = Modifier.weight(1f)) {
                                Text(
                                    text = spec.displayName,
                                    style = MaterialTheme.typography.titleSmall,
                                    fontWeight = FontWeight.SemiBold,
                                    color = TextPrimary
                                )
                                Text(
                                    text = spec.subtitle,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = TextSecondary
                                )
                                Text(
                                    text = if (isInstalled) {
                                        "已下载 · 占用约 ${if (installedMb > 0) installedMb else spec.approxDiskMb} MB"
                                    } else {
                                        "下载约 ${spec.approxDownloadMb} MB · 解压约 ${spec.approxDiskMb} MB"
                                    },
                                    style = MaterialTheme.typography.labelSmall,
                                    color = TextMuted
                                )
                            }
                            if (isSelected && isInstalled) {
                                Surface(
                                    color = PrimaryIndigo,
                                    shape = RoundedCornerShape(8.dp)
                                ) {
                                    Text(
                                        text = "使用中",
                                        color = Color.White,
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.Bold,
                                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp)
                                    )
                                }
                            }
                        }

                        if (isDownloadingThis) {
                            val prog = downloadProgress
                            val pct = prog?.percent ?: -1
                            val dlMb = (prog?.downloadedBytes ?: 0L) / (1024L * 1024L)
                            val totMb = (prog?.totalBytes ?: -1L) / (1024L * 1024L)
                            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                if (pct in 0..100) {
                                    LinearProgressIndicator(
                                        progress = { pct / 100f },
                                        modifier = Modifier.fillMaxWidth(),
                                        color = PrimaryIndigo
                                    )
                                } else {
                                    LinearProgressIndicator(
                                        modifier = Modifier.fillMaxWidth(),
                                        color = PrimaryIndigo
                                    )
                                }
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.SpaceBetween,
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                        Text(
                                            text = when {
                                                prog?.phase == "extracting" -> "正在校验 SHA-256 与解压模型..."
                                                totMb > 0 -> "断点续传下载中：$pct% ($dlMb / $totMb MB)"
                                                else -> "断点续传下载中：已接收 $dlMb MB"
                                            },
                                            style = MaterialTheme.typography.labelSmall,
                                            color = TextSecondary
                                        )
                                    TextButton(onClick = { modelManager.cancelDownload() }) {
                                        Text("取消", fontSize = 12.sp)
                                    }
                                }
                            }
                        } else {
                            Row(
                                modifier = Modifier.fillMaxWidth(),
                                horizontalArrangement = Arrangement.spacedBy(8.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                if (!isInstalled) {
                                    Button(
                                        onClick = {
                                            statusMessage = null
                                            scope.launch {
                                                val res = modelManager.downloadAndExtractModel(spec, mirrorBaseUrl = mirrorBaseUrl)
                                                res.onFailure { err ->
                                                    statusMessage = err.message ?: "下载失败，已保留断点，点击可继续"
                                                }
                                            }
                                        },
                                        enabled = downloadProgress == null,
                                        contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp)
                                    ) {
                                        Icon(Icons.Default.Download, contentDescription = null, modifier = Modifier.size(16.dp))
                                        Spacer(modifier = Modifier.width(6.dp))
                                        Text("下载并启用", fontSize = 13.sp)
                                    }
                                } else {
                                    if (!isSelected) {
                                        Button(
                                            onClick = {
                                                statusMessage = null
                                                modelManager.selectModel(spec)
                                            },
                                            contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp)
                                        ) {
                                            Icon(Icons.Default.Check, contentDescription = null, modifier = Modifier.size(16.dp))
                                            Spacer(modifier = Modifier.width(6.dp))
                                            Text("切换使用", fontSize = 13.sp)
                                        }
                                    }
                                    OutlinedButton(
                                        onClick = {
                                            statusMessage = null
                                            scope.launch {
                                                onModelDeleted(spec)
                                                modelManager.deleteModel(spec)
                                            }
                                        },
                                        enabled = downloadProgress == null,
                                        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp)
                                    ) {
                                        Icon(Icons.Default.DeleteOutline, contentDescription = null, modifier = Modifier.size(16.dp))
                                        Spacer(modifier = Modifier.width(4.dp))
                                        Text("删除释放空间", fontSize = 12.sp)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
