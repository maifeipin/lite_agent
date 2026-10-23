package com.liteagent.mobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModelProvider
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.SseStreamManager
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.ui.chat.ChatScreen
import com.liteagent.mobile.ui.chat.ChatViewModel
import com.liteagent.mobile.ui.chat.ChatViewModelFactory
import com.liteagent.mobile.ui.chat.ServerConfigDialog
import com.liteagent.mobile.ui.theme.*

class MainActivity : ComponentActivity() {

    private lateinit var tokenStorage: SecureTokenStorage
    private lateinit var apiClient: ApiClient
    private lateinit var sseStreamManager: SseStreamManager
    private lateinit var chatViewModel: ChatViewModel

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        tokenStorage = SecureTokenStorage(applicationContext)
        apiClient = ApiClient(tokenStorage)
        sseStreamManager = SseStreamManager(tokenStorage)

        val factory = ChatViewModelFactory(tokenStorage, apiClient, sseStreamManager)
        chatViewModel = ViewModelProvider(this, factory)[ChatViewModel::class.java]

        setContent {
            LiteAgentTheme {
                MainAppScaffold(
                    tokenStorage = tokenStorage,
                    apiClient = apiClient,
                    chatViewModel = chatViewModel
                )
            }
        }
    }
}

enum class NavigationTab(val label: String, val icon: ImageVector) {
    CHAT("助理", Icons.Default.Chat),
    TODOS("待办", Icons.Default.CheckCircle),
    MEETINGS("会审", Icons.Default.Groups),
    DISCOVER("发现", Icons.Default.Explore)
}

@Composable
fun MainAppScaffold(
    tokenStorage: SecureTokenStorage,
    apiClient: ApiClient,
    chatViewModel: ChatViewModel
) {
    var selectedTab by remember { mutableStateOf(NavigationTab.CHAT) }
    var showServerConfig by remember { mutableStateOf(false) }

    if (showServerConfig) {
        ServerConfigDialog(
            tokenStorage = tokenStorage,
            apiClient = apiClient,
            onDismiss = { showServerConfig = false },
            onSaved = { newServerUrl ->
                chatViewModel.onServerSwitched(newServerUrl)
            }
        )
    }

    Scaffold(
        modifier = Modifier.fillMaxSize(),
        containerColor = MaterialTheme.colorScheme.background,
        bottomBar = {
            NavigationBar(
                containerColor = MaterialTheme.colorScheme.surface,
                tonalElevation = 4.dp
            ) {
                NavigationTab.values().forEach { tab ->
                    NavigationBarItem(
                        selected = selectedTab == tab,
                        onClick = { selectedTab = tab },
                        icon = { Icon(tab.icon, contentDescription = tab.label) },
                        label = { Text(tab.label, fontSize = 12.sp) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = MaterialTheme.colorScheme.primary,
                            selectedTextColor = MaterialTheme.colorScheme.primary,
                            unselectedIconColor = TextMuted,
                            unselectedTextColor = TextMuted,
                            indicatorColor = MaterialTheme.colorScheme.surfaceVariant
                        )
                    )
                }
            }
        }
    ) { innerPadding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
        ) {
            when (selectedTab) {
                NavigationTab.CHAT -> ChatScreen(
                    viewModel = chatViewModel,
                    onOpenSettings = { showServerConfig = true }
                )
                NavigationTab.TODOS -> TabPlaceholder(
                    title = "待办事项 (GTD)",
                    subtitle = "服务端为准 + 本地沙箱离线缓存秒开 (Sprint 2)",
                    icon = Icons.Default.FormatListBulleted
                )
                NavigationTab.MEETINGS -> TabPlaceholder(
                    title = "会审决策中枢",
                    subtitle = "防重提交、版本冲突阻断与掌上审批 (Sprint 2)",
                    icon = Icons.Default.Gavel
                )
                NavigationTab.DISCOVER -> TabPlaceholder(
                    title = "发现与概览",
                    subtitle = "RSS 晨间简报、账单速览与真机自动化监控 (Sprint 3)",
                    icon = Icons.Default.AutoAwesome
                )
            }
        }
    }
}

@Composable
fun TabPlaceholder(
    title: String,
    subtitle: String,
    icon: ImageVector
) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
        contentAlignment = Alignment.Center
    ) {
        Card(
            modifier = Modifier
                .fillMaxWidth()
                .padding(24.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
            shape = RoundedCornerShape(16.dp),
            border = androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.outline)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(32.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                Surface(
                    shape = RoundedCornerShape(16.dp),
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    modifier = Modifier.size(64.dp)
                ) {
                    Box(contentAlignment = Alignment.Center) {
                        Icon(
                            imageVector = icon,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.size(32.dp)
                        )
                    }
                }

                Text(
                    text = title,
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold
                )

                Text(
                    text = subtitle,
                    style = MaterialTheme.typography.bodyMedium,
                    color = TextSecondary,
                    textAlign = androidx.compose.ui.text.style.TextAlign.Center
                )
            }
        }
    }
}
