package com.liteagent.mobile.ui.components

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CloudDownload
import androidx.compose.material.icons.filled.ErrorOutline
import androidx.compose.material.icons.filled.NewReleases
import androidx.compose.material.icons.filled.SystemUpdate
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.liteagent.mobile.core.update.AppVersionDto
import com.liteagent.mobile.core.update.UpdateController
import com.liteagent.mobile.ui.theme.*

@Composable
fun UpdateDialogHost(controller: UpdateController) {
    val state by controller.state.collectAsState()

    when (val current = state) {
        is UpdateController.State.Idle -> Unit

        is UpdateController.State.Checking -> AlertDialog(
            onDismissRequest = { controller.dismiss() },
            title = { Text("检查更新") },
            text = {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(20.dp),
                        color = PrimaryIndigo,
                        strokeWidth = 2.dp
                    )
                    Spacer(Modifier.width(12.dp))
                    Text("正在获取最新版本信息…", color = TextSecondary, fontSize = 14.sp)
                }
            },
            confirmButton = {
                TextButton(onClick = { controller.dismiss() }) { Text("取消") }
            },
            containerColor = SurfaceElevated
        )

        is UpdateController.State.UpToDate -> AlertDialog(
            onDismissRequest = { controller.dismiss() },
            icon = { Icon(Icons.Default.NewReleases, contentDescription = null, tint = PrimaryIndigo) },
            title = { Text("已是最新版本", fontWeight = FontWeight.Bold) },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("当前版本：v${current.versionName}", color = TextSecondary, fontSize = 13.sp)
                    Text("渠道：${controller.channel}", color = TextMuted, fontSize = 13.sp)
                }
            },
            confirmButton = {
                TextButton(onClick = { controller.dismiss() }) { Text("好") }
            },
            containerColor = SurfaceElevated
        )

        is UpdateController.State.Available -> UpdateAvailableDialog(
            info = current.info,
            channel = controller.channel,
            onDownload = { controller.download() },
            onSkip = { controller.skipThisVersion() },
            onLater = { controller.dismiss() }
        )

        is UpdateController.State.Downloading -> AlertDialog(
            onDismissRequest = { },
            icon = { Icon(Icons.Default.CloudDownload, contentDescription = null, tint = PrimaryIndigo) },
            title = { Text("正在下载更新", fontWeight = FontWeight.Bold) },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text(
                        text = "v${current.info.versionName ?: current.info.versionCode}",
                        fontSize = 14.sp,
                        fontWeight = FontWeight.Medium
                    )
                    LinearProgressIndicator(
                        progress = { current.progress.fraction },
                        modifier = Modifier.fillMaxWidth().height(6.dp),
                        color = PrimaryIndigo
                    )
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text(
                            text = "${formatBytes(current.progress.receivedBytes)} / ${formatBytes(current.progress.totalBytes)}",
                            fontSize = 12.sp,
                            color = TextSecondary
                        )
                        Text(
                            text = "${(current.progress.fraction * 100).toInt()}%",
                            fontSize = 12.sp,
                            fontWeight = FontWeight.Bold,
                            color = PrimaryIndigo
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = { controller.dismiss() }) { Text("取消") }
            },
            containerColor = SurfaceElevated
        )

        is UpdateController.State.ReadyToInstall -> AlertDialog(
            onDismissRequest = { controller.dismiss() },
            icon = { Icon(Icons.Default.SystemUpdate, contentDescription = null, tint = PrimaryIndigo) },
            title = { Text("下载完成", fontWeight = FontWeight.Bold) },
            text = {
                Text(
                    text = if (current.needsPermission) {
                        "需要授予「安装未知应用」权限才能完成升级。点击下方按钮前往系统设置开启。"
                    } else {
                        "新版本已准备就绪，点击「立即安装」完成覆盖升级（保留所有本地数据与登录态）。"
                    },
                    fontSize = 14.sp,
                    color = TextSecondary
                )
            },
            confirmButton = {
                Button(
                    onClick = { controller.installNow() },
                    colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo)
                ) {
                    Text(if (current.needsPermission) "去授权" else "立即安装")
                }
            },
            dismissButton = {
                TextButton(onClick = { controller.dismiss() }) { Text("稍后") }
            },
            containerColor = SurfaceElevated
        )

        is UpdateController.State.Failed -> AlertDialog(
            onDismissRequest = { controller.dismiss() },
            icon = { Icon(Icons.Default.ErrorOutline, contentDescription = null, tint = AccentAmber) },
            title = { Text("更新失败", fontWeight = FontWeight.Bold) },
            text = {
                Text(current.message, fontSize = 14.sp, color = TextSecondary)
            },
            confirmButton = {
                if (current.info != null) {
                    Button(
                        onClick = { controller.download() },
                        colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo)
                    ) {
                        Text("重试")
                    }
                } else {
                    TextButton(onClick = { controller.dismiss() }) { Text("好") }
                }
            },
            dismissButton = if (current.info != null) {
                { TextButton(onClick = { controller.dismiss() }) { Text("取消") } }
            } else null,
            containerColor = SurfaceElevated
        )
    }
}

@Composable
private fun UpdateAvailableDialog(
    info: AppVersionDto,
    channel: String,
    onDownload: () -> Unit,
    onSkip: () -> Unit,
    onLater: () -> Unit
) {
    AlertDialog(
        onDismissRequest = { if (!info.forceUpdate) onLater() },
        icon = { Icon(Icons.Default.SystemUpdate, contentDescription = null, tint = PrimaryIndigo) },
        title = {
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text("发现新版本", fontWeight = FontWeight.Bold)
                Text(
                    text = "v${info.versionName ?: info.versionCode} (${formatBytes(info.sizeBytes)})",
                    fontSize = 12.sp,
                    color = TextSecondary
                )
            }
        },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 240.dp)
                    .verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Text("更新日志：", fontWeight = FontWeight.Bold, fontSize = 13.sp)
                Text(
                    text = info.changelog?.takeIf { it.isNotBlank() } ?: "无更新说明",
                    fontSize = 13.sp,
                    color = TextSecondary
                )
            }
        },
        confirmButton = {
            Button(
                onClick = onDownload,
                colors = ButtonDefaults.buttonColors(containerColor = PrimaryIndigo)
            ) {
                Text("立即更新")
            }
        },
        dismissButton = if (!info.forceUpdate) {
            {
                Row {
                    TextButton(onClick = onSkip) { Text("跳过此版本", color = TextMuted) }
                    TextButton(onClick = onLater) { Text("稍后") }
                }
            }
        } else null,
        containerColor = SurfaceElevated
    )
}

private fun formatBytes(bytes: Long): String {
    if (bytes <= 0L) return "0 B"
    val units = arrayOf("B", "KB", "MB", "GB")
    var value = bytes.toDouble()
    var unitIndex = 0
    while (value >= 1024.0 && unitIndex < units.lastIndex) {
        value /= 1024.0
        unitIndex++
    }
    return String.format("%.1f %s", value, units[unitIndex])
}
