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
import androidx.lifecycle.lifecycleScope
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.SseStreamManager
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.core.update.UpdateController
import com.liteagent.mobile.ui.auth.LoginScreen
import com.liteagent.mobile.ui.chat.ChatScreen
import com.liteagent.mobile.ui.chat.ChatViewModel
import com.liteagent.mobile.ui.chat.ChatViewModelFactory
import com.liteagent.mobile.ui.chat.ServerConfigDialog
import com.liteagent.mobile.ui.components.UpdateDialogHost
import com.liteagent.mobile.ui.discover.DiscoverScreen
import com.liteagent.mobile.ui.discover.DiscoverViewModel
import com.liteagent.mobile.ui.discover.DiscoverViewModelFactory
import com.liteagent.mobile.ui.meetings.MeetingsScreen
import com.liteagent.mobile.ui.meetings.MeetingsViewModel
import com.liteagent.mobile.ui.meetings.MeetingsViewModelFactory
import com.liteagent.mobile.ui.phoneapp.PhoneAppScreen
import com.liteagent.mobile.ui.phoneapp.PhoneAppViewModel
import com.liteagent.mobile.ui.phoneapp.PhoneAppViewModelFactory
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
    private lateinit var phoneAppViewModel: PhoneAppViewModel
    private lateinit var discoverViewModel: DiscoverViewModel
    private lateinit var updateController: UpdateController

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

        val phoneAppFactory = PhoneAppViewModelFactory(tokenStorage, apiClient)
        phoneAppViewModel = ViewModelProvider(this, phoneAppFactory)[PhoneAppViewModel::class.java]

        val discoverFactory = DiscoverViewModelFactory(tokenStorage, apiClient)
        discoverViewModel = ViewModelProvider(this, discoverFactory)[DiscoverViewModel::class.java]

        updateController = UpdateController(
            context = applicationContext,
            tokenStorage = tokenStorage,
            scope = lifecycleScope
        )
        updateController.autoCheckOnce()

        setContent {
            LiteAgentTheme {
                UpdateDialogHost(controller = updateController)

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
                            phoneAppViewModel.onSessionChanged(serverUrl, username)
                            discoverViewModel.onServerChanged(serverUrl)
                        }
                    )
                } else {
                    MainAppScaffold(
                        tokenStorage = tokenStorage,
                        apiClient = apiClient,
                        chatViewModel = chatViewModel,
                        todosViewModel = todosViewModel,
                        meetingsViewModel = meetingsViewModel,
                        phoneAppViewModel = phoneAppViewModel,
                        discoverViewModel = discoverViewModel,
                        updateController = updateController,
                        onLogout = {
                            isLoggedIn = false
                            chatViewModel.onLoggedOut()
                            todosViewModel.onLoggedOut()
                            meetingsViewModel.onLoggedOut()
                            phoneAppViewModel.onLoggedOut()
                        }
                    )
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        if (::updateController.isInitialized) {
            updateController.onResume()
        }
    }
}

enum class NavigationTab(val label: String, val icon: ImageVector) {
    CHAT("助理", Icons.Default.Chat),
    TODOS("待办", Icons.Default.CheckCircle),
    MEETINGS("会审&执行", Icons.Default.Groups),
    DISCOVER("发现", Icons.Default.Explore)
}

enum class MeetingsExecSubTab(val label: String) {
    MEETINGS("📣 会审决策"),
    PHONE_APPS("📱 自动化执行")
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun MainAppScaffold(
    tokenStorage: SecureTokenStorage,
    apiClient: ApiClient,
    chatViewModel: ChatViewModel,
    todosViewModel: TodosViewModel,
    meetingsViewModel: MeetingsViewModel,
    phoneAppViewModel: PhoneAppViewModel,
    discoverViewModel: DiscoverViewModel,
    updateController: UpdateController,
    onLogout: () -> Unit = {}
) {
    var selectedTab by remember { mutableStateOf(NavigationTab.CHAT) }
    var selectedSubTab by remember { mutableStateOf(MeetingsExecSubTab.MEETINGS) }
    var showServerConfig by remember { mutableStateOf(false) }
    val isImeVisible = WindowInsets.isImeVisible

    val todosState by todosViewModel.uiState.collectAsState()
    val meetingsState by meetingsViewModel.uiState.collectAsState()
    val phoneAppState by phoneAppViewModel.uiState.collectAsState()

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
                phoneAppViewModel.onSessionChanged(newServerUrl, username)
                discoverViewModel.onServerChanged(newServerUrl)
            },
            onLoggedOut = {
                showServerConfig = false
                chatViewModel.onLoggedOut()
                todosViewModel.onLoggedOut()
                meetingsViewModel.onLoggedOut()
                phoneAppViewModel.onLoggedOut()
                onLogout()
            }
        )
    }

    val totalActionRequiredCount = meetingsState.waitingCount + phoneAppState.waitingApprovalCount

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
                                                if (totalActionRequiredCount > 0) {
                                                    Badge(
                                                        containerColor = AccentAmber,
                                                        contentColor = Color.Black
                                                    ) {
                                                        Text(
                                                            text = "$totalActionRequiredCount",
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
                NavigationTab.MEETINGS -> {
                    Column(modifier = Modifier.fillMaxSize()) {
                        Surface(
                            color = MaterialTheme.colorScheme.surface,
                            tonalElevation = 2.dp,
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(horizontal = 16.dp, vertical = 8.dp),
                                horizontalArrangement = Arrangement.spacedBy(10.dp)
                            ) {
                                MeetingsExecSubTab.values().forEach { subTab ->
                                    val isSelected = selectedSubTab == subTab
                                    val subBadgeCount = when (subTab) {
                                        MeetingsExecSubTab.MEETINGS -> meetingsState.waitingCount
                                        MeetingsExecSubTab.PHONE_APPS -> phoneAppState.waitingApprovalCount
                                    }
                                    FilterChip(
                                        selected = isSelected,
                                        onClick = { selectedSubTab = subTab },
                                        label = {
                                            Row(
                                                verticalAlignment = Alignment.CenterVertically,
                                                horizontalArrangement = Arrangement.spacedBy(6.dp)
                                            ) {
                                                Text(
                                                    text = subTab.label,
                                                    fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                                                    fontSize = 13.sp
                                                )
                                                if (subBadgeCount > 0) {
                                                    Surface(
                                                        color = AccentAmber,
                                                        shape = RoundedCornerShape(10.dp)
                                                    ) {
                                                        Text(
                                                            text = "$subBadgeCount",
                                                            color = Color.Black,
                                                            fontSize = 10.sp,
                                                            fontWeight = FontWeight.Bold,
                                                            modifier = Modifier.padding(horizontal = 6.dp, vertical = 1.dp)
                                                        )
                                                    }
                                                }
                                            }
                                        },
                                        modifier = Modifier.weight(1f),
                                        colors = FilterChipDefaults.filterChipColors(
                                            selectedContainerColor = PrimaryIndigo.copy(alpha = 0.2f),
                                            selectedLabelColor = PrimaryIndigo
                                        )
                                    )
                                }
                            }
                        }

                        Box(modifier = Modifier.weight(1f)) {
                            when (selectedSubTab) {
                                MeetingsExecSubTab.MEETINGS -> MeetingsScreen(
                                    viewModel = meetingsViewModel
                                )
                                MeetingsExecSubTab.PHONE_APPS -> PhoneAppScreen(
                                    viewModel = phoneAppViewModel
                                )
                            }
                        }
                    }
                }
                NavigationTab.DISCOVER -> DiscoverScreen(
                    viewModel = discoverViewModel,
                    onCheckUpdate = { updateController.checkManually() },
                    onAskAiWithTopic = { prompt ->
                        chatViewModel.prefillPrompt(prompt)
                        selectedTab = NavigationTab.CHAT
                    }
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
