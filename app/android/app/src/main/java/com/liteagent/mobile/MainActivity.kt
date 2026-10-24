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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModelProvider
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.SseStreamManager
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.ui.auth.LoginScreen
import com.liteagent.mobile.ui.chat.ChatScreen
import com.liteagent.mobile.ui.chat.ChatViewModel
import com.liteagent.mobile.ui.chat.ChatViewModelFactory
import com.liteagent.mobile.ui.chat.ServerConfigDialog
import com.liteagent.mobile.ui.meetings.MeetingsScreen
import com.liteagent.mobile.ui.meetings.MeetingsViewModel
import com.liteagent.mobile.ui.meetings.MeetingsViewModelFactory
import com.liteagent.mobile.ui.theme.*
import com.liteagent.mobile.ui.todos.TodosScreen
import com.liteagent.mobile.ui.todos.TodosViewModel
import com.liteagent.mobile.ui.todos.TodosViewModelFactory

class MainActivity : ComponentActivity() {

    private lateinit var tokenStorage: SecureTokenStorage
    private lateinit var apiClient: ApiClient
    private lateinit var sseStreamManager: SseStreamManager
    private lateinit var chatViewModel: ChatViewModel
    private lateinit var todosViewModel: TodosViewModel
    private lateinit var meetingsViewModel: MeetingsViewModel

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        tokenStorage = SecureTokenStorage(applicationContext)
        apiClient = ApiClient(tokenStorage)
        sseStreamManager = SseStreamManager(tokenStorage)

        val chatFactory = ChatViewModelFactory(tokenStorage, apiClient, sseStreamManager)
        chatViewModel = ViewModelProvider(this, chatFactory)[ChatViewModel::class.java]

        val todosFactory = TodosViewModelFactory(applicationContext, tokenStorage, apiClient)
        todosViewModel = ViewModelProvider(this, todosFactory)[TodosViewModel::class.java]

        val meetingsFactory = MeetingsViewModelFactory(tokenStorage, apiClient)
        meetingsViewModel = ViewModelProvider(this, meetingsFactory)[MeetingsViewModel::class.java]

        setContent {
            LiteAgentTheme {
                val activeServer by tokenStorage.activeServerUrlFlow.collectAsState(initial = SecureTokenStorage.DEFAULT_SERVER_URL)
                val session by tokenStorage.sessionFlow.collectAsState(initial = null)
                var isCheckingSession by remember { mutableStateOf(true) }
                var isLoggedIn by remember { mutableStateOf(false) }

                LaunchedEffect(activeServer, session) {
                    val valid = tokenStorage.hasValidSession(activeServer)
                    isLoggedIn = valid
                    isCheckingSession = false
                }

                if (isCheckingSession) {
                    Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(color = MaterialTheme.colorScheme.primary)
                    }
                } else if (!isLoggedIn) {
                    LoginScreen(
                        tokenStorage = tokenStorage,
                        apiClient = apiClient,
                        onLoginSuccess = { serverUrl ->
                            isLoggedIn = true
                            val newSession = kotlinx.coroutines.runBlocking { tokenStorage.getSession(serverUrl) }
                            val username = newSession?.username ?: "default"
                            chatViewModel.onSessionChanged(serverUrl, newSession?.sessionId ?: "")
                            todosViewModel.onSessionChanged(serverUrl, username)
                            meetingsViewModel.onSessionChanged(serverUrl, username)
                        }
                    )
                } else {
                    MainAppScaffold(
                        tokenStorage = tokenStorage,
                        apiClient = apiClient,
                        chatViewModel = chatViewModel,
                        todosViewModel = todosViewModel,
                        meetingsViewModel = meetingsViewModel,
                        onLogout = {
                            isLoggedIn = false
                            chatViewModel.onLoggedOut()
                            todosViewModel.onLoggedOut()
                            meetingsViewModel.onLoggedOut()
                        }
                    )
                }
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

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun MainAppScaffold(
    tokenStorage: SecureTokenStorage,
    apiClient: ApiClient,
    chatViewModel: ChatViewModel,
    todosViewModel: TodosViewModel,
    meetingsViewModel: MeetingsViewModel,
    onLogout: () -> Unit = {}
) {
    var selectedTab by remember { mutableStateOf(NavigationTab.CHAT) }
    var showServerConfig by remember { mutableStateOf(false) }
    val isImeVisible = WindowInsets.isImeVisible

    val todosState by todosViewModel.uiState.collectAsState()
    val meetingsState by meetingsViewModel.uiState.collectAsState()

    if (showServerConfig) {
        ServerConfigDialog(
            tokenStorage = tokenStorage,
            apiClient = apiClient,
            onDismiss = { showServerConfig = false },
            onSaved = { newServerUrl ->
                val newSession = kotlinx.coroutines.runBlocking { tokenStorage.getSession(newServerUrl) }
                val username = newSession?.username ?: "default"
                chatViewModel.onServerSwitched(newServerUrl)
                todosViewModel.onSessionChanged(newServerUrl, username)
                meetingsViewModel.onSessionChanged(newServerUrl, username)
            },
            onLoggedOut = {
                showServerConfig = false
                chatViewModel.onLoggedOut()
                todosViewModel.onLoggedOut()
                meetingsViewModel.onLoggedOut()
                onLogout()
            }
        )
    }

    Scaffold(
        modifier = Modifier
            .fillMaxSize()
            .imePadding(),
        containerColor = MaterialTheme.colorScheme.background,
        bottomBar = {
            if (!isImeVisible) {
                NavigationBar(
                    containerColor = MaterialTheme.colorScheme.surface,
                    tonalElevation = 4.dp
                ) {
                    NavigationTab.values().forEach { tab ->
                        NavigationBarItem(
                            selected = selectedTab == tab,
                            onClick = { selectedTab = tab },
                            icon = {
                                when (tab) {
                                    NavigationTab.MEETINGS -> {
                                        BadgedBox(
                                            badge = {
                                                if (meetingsState.waitingCount > 0) {
                                                    Badge(
                                                        containerColor = AccentAmber,
                                                        contentColor = Color.Black
                                                    ) {
                                                        Text(
                                                            text = "${meetingsState.waitingCount}",
                                                            fontWeight = FontWeight.Bold
                                                        )
                                                    }
                                                }
                                            }
                                        ) {
                                            Icon(tab.icon, contentDescription = tab.label)
                                        }
                                    }
                                    NavigationTab.TODOS -> {
                                        BadgedBox(
                                            badge = {
                                                if (todosState.pendingCount > 0) {
                                                    Badge(
                                                        containerColor = SurfaceElevated,
                                                        contentColor = TextPrimary
                                                    ) {
                                                        Text("${todosState.pendingCount}")
                                                    }
                                                }
                                            }
                                        ) {
                                            Icon(tab.icon, contentDescription = tab.label)
                                        }
                                    }
                                    else -> {
                                        Icon(tab.icon, contentDescription = tab.label)
                                    }
                                }
                            },
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
                NavigationTab.TODOS -> TodosScreen(
                    viewModel = todosViewModel
                )
                NavigationTab.MEETINGS -> MeetingsScreen(
                    viewModel = meetingsViewModel
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
