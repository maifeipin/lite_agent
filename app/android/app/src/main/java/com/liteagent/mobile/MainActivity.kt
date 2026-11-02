package com.liteagent.mobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
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

enum class NavigationTab(val label: String, val shortLabel: String, val icon: ImageVector) {
    CHAT("AI 助理", "助理", Icons.Default.Chat),
    TODOS("待办日程 (GTD)", "待办", Icons.Default.CheckCircle),
    MEETINGS("会审与执行", "会审&执行", Icons.Default.Groups),
    DISCOVER("发现与概览", "发现", Icons.Default.Explore)
}

enum class MeetingsExecSubTab(val label: String) {
    MEETINGS("📣 会审决策"),
    PHONE_APPS("📱 自动化执行")
}

@OptIn(ExperimentalLayoutApi::class, ExperimentalMaterial3Api::class)
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
    var showQuickSwitchBar by remember { mutableStateOf(false) }

    val drawerState = rememberDrawerState(initialValue = DrawerValue.Closed)
    val coroutineScope = rememberCoroutineScope()

    val chatSessions by chatViewModel.sessions.collectAsState()
    val currentChatSession by chatViewModel.currentSession.collectAsState()
    val todosState by todosViewModel.uiState.collectAsState()
    val meetingsState by meetingsViewModel.uiState.collectAsState()
    val phoneAppState by phoneAppViewModel.uiState.collectAsState()
    val activeServer by tokenStorage.activeServerUrlFlow.collectAsState(initial = SecureTokenStorage.DEFAULT_SERVER_URL)
    val activeSession by tokenStorage.sessionFlow.collectAsState(initial = null)

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
    val totalDrawerBadgeCount = todosState.pendingCount + totalActionRequiredCount
    val openDrawer: () -> Unit = {
        coroutineScope.launch { drawerState.open() }
    }
    val toggleQuickBar: () -> Unit = {
        showQuickSwitchBar = !showQuickSwitchBar
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        gesturesEnabled = true,
        drawerContent = {
            ModalDrawerSheet(
                drawerContainerColor = MaterialTheme.colorScheme.surface,
                modifier = Modifier.width(300.dp)
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .verticalScroll(rememberScrollState())
                        .padding(horizontal = 12.dp, vertical = 12.dp)
                ) {
                    // 1. 抽屉顶部品牌与账号状态卡片
                    Surface(
                        color = SurfaceElevated,
                        shape = RoundedCornerShape(16.dp),
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(bottom = 12.dp)
                    ) {
                        Column(modifier = Modifier.padding(14.dp)) {
                            Row(
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.SpaceBetween,
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                Row(
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(10.dp)
                                ) {
                                    Surface(
                                        color = PrimaryIndigo,
                                        shape = RoundedCornerShape(10.dp),
                                        modifier = Modifier.size(36.dp)
                                    ) {
                                        Box(contentAlignment = Alignment.Center) {
                                            Icon(
                                                imageVector = Icons.Default.SmartToy,
                                                contentDescription = null,
                                                tint = Color.White,
                                                modifier = Modifier.size(20.dp)
                                            )
                                        }
                                    }
                                    Column {
                                        Text(
                                            text = "Lite Agent 智脑",
                                            style = MaterialTheme.typography.titleSmall,
                                            fontWeight = FontWeight.Bold,
                                            color = TextPrimary
                                        )
                                        Text(
                                            text = "账号: ${activeSession?.username ?: "已登录"} · v${updateController.currentVersionName}",
                                            style = MaterialTheme.typography.labelSmall,
                                            color = TextSecondary
                                        )
                                    }
                                }
                                IconButton(
                                    onClick = {
                                        coroutineScope.launch { drawerState.close() }
                                        showServerConfig = true
                                    },
                                    modifier = Modifier.size(32.dp)
                                ) {
                                    Icon(
                                        imageVector = Icons.Default.Settings,
                                        contentDescription = "网络与凭据设置",
                                        tint = TextSecondary,
                                        modifier = Modifier.size(18.dp)
                                    )
                                }
                            }
                            Spacer(modifier = Modifier.height(6.dp))
                            Text(
                                text = activeServer,
                                style = MaterialTheme.typography.labelSmall,
                                color = TextMuted,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                        }
                    }

                    Text(
                        text = "核心工作台",
                        style = MaterialTheme.typography.labelSmall,
                        color = TextMuted,
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp)
                    )

                    // 2. 五大工作台一级直达导航项
                    NavigationDrawerItem(
                        icon = { Icon(Icons.Default.Chat, contentDescription = null) },
                        label = { Text("AI 助理", fontWeight = FontWeight.Medium) },
                        selected = selectedTab == NavigationTab.CHAT,
                        onClick = {
                            selectedTab = NavigationTab.CHAT
                            coroutineScope.launch { drawerState.close() }
                        },
                        modifier = Modifier.padding(vertical = 2.dp)
                    )

                    NavigationDrawerItem(
                        icon = { Icon(Icons.Default.CheckCircle, contentDescription = null) },
                        label = { Text("待办日程 (GTD)", fontWeight = FontWeight.Medium) },
                        badge = {
                            if (todosState.pendingCount > 0) {
                                Surface(
                                    color = PrimaryIndigo.copy(alpha = 0.2f),
                                    shape = RoundedCornerShape(10.dp)
                                ) {
                                    Text(
                                        text = "${todosState.pendingCount}",
                                        color = PrimaryIndigo,
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.Bold,
                                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp)
                                    )
                                }
                            }
                        },
                        selected = selectedTab == NavigationTab.TODOS,
                        onClick = {
                            selectedTab = NavigationTab.TODOS
                            coroutineScope.launch { drawerState.close() }
                        },
                        modifier = Modifier.padding(vertical = 2.dp)
                    )

                    NavigationDrawerItem(
                        icon = { Icon(Icons.Default.Groups, contentDescription = null) },
                        label = { Text("会审决策中枢", fontWeight = FontWeight.Medium) },
                        badge = {
                            if (meetingsState.waitingCount > 0) {
                                Surface(
                                    color = AccentAmber,
                                    shape = RoundedCornerShape(10.dp)
                                ) {
                                    Text(
                                        text = "${meetingsState.waitingCount}",
                                        color = Color.Black,
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.Bold,
                                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp)
                                    )
                                }
                            }
                        },
                        selected = selectedTab == NavigationTab.MEETINGS && selectedSubTab == MeetingsExecSubTab.MEETINGS,
                        onClick = {
                            selectedTab = NavigationTab.MEETINGS
                            selectedSubTab = MeetingsExecSubTab.MEETINGS
                            coroutineScope.launch { drawerState.close() }
                        },
                        modifier = Modifier.padding(vertical = 2.dp)
                    )

                    NavigationDrawerItem(
                        icon = { Icon(Icons.Default.PhoneAndroid, contentDescription = null) },
                        label = { Text("手机自动化执行", fontWeight = FontWeight.Medium) },
                        badge = {
                            if (phoneAppState.waitingApprovalCount > 0) {
                                Surface(
                                    color = AccentAmber,
                                    shape = RoundedCornerShape(10.dp)
                                ) {
                                    Text(
                                        text = "${phoneAppState.waitingApprovalCount}",
                                        color = Color.Black,
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.Bold,
                                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp)
                                    )
                                }
                            }
                        },
                        selected = selectedTab == NavigationTab.MEETINGS && selectedSubTab == MeetingsExecSubTab.PHONE_APPS,
                        onClick = {
                            selectedTab = NavigationTab.MEETINGS
                            selectedSubTab = MeetingsExecSubTab.PHONE_APPS
                            coroutineScope.launch { drawerState.close() }
                        },
                        modifier = Modifier.padding(vertical = 2.dp)
                    )

                    NavigationDrawerItem(
                        icon = { Icon(Icons.Default.Explore, contentDescription = null) },
                        label = { Text("发现与概览", fontWeight = FontWeight.Medium) },
                        selected = selectedTab == NavigationTab.DISCOVER,
                        onClick = {
                            selectedTab = NavigationTab.DISCOVER
                            coroutineScope.launch { drawerState.close() }
                        },
                        modifier = Modifier.padding(vertical = 2.dp)
                    )

                    HorizontalDivider(
                        modifier = Modifier.padding(vertical = 10.dp),
                        color = MaterialTheme.colorScheme.outline.copy(alpha = 0.4f)
                    )

                    // 3. 抽屉内快捷历史会话列表
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 12.dp, vertical = 4.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = "最近对话 (${chatSessions.size})",
                            style = MaterialTheme.typography.labelSmall,
                            color = TextMuted
                        )
                        Text(
                            text = "➕ 新建",
                            style = MaterialTheme.typography.labelSmall,
                            color = PrimaryIndigo,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.clickable {
                                chatViewModel.createNewSession()
                                selectedTab = NavigationTab.CHAT
                                coroutineScope.launch { drawerState.close() }
                            }
                        )
                    }

                    chatSessions.take(8).forEach { sess ->
                        val isCurrentSess = currentChatSession?.sessionKey == sess.sessionKey && selectedTab == NavigationTab.CHAT
                        Surface(
                            color = if (isCurrentSess) PrimaryIndigo.copy(alpha = 0.14f) else Color.Transparent,
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier
                                .fillMaxWidth()
                                .clickable {
                                    chatViewModel.selectSession(sess)
                                    selectedTab = NavigationTab.CHAT
                                    coroutineScope.launch { drawerState.close() }
                                }
                        ) {
                            Row(
                                modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                Icon(
                                    imageVector = Icons.Default.Forum,
                                    contentDescription = null,
                                    tint = if (isCurrentSess) PrimaryIndigo else TextMuted,
                                    modifier = Modifier.size(15.dp)
                                )
                                Text(
                                    text = sess.displayTitle,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = if (isCurrentSess) PrimaryIndigo else TextPrimary,
                                    fontWeight = if (isCurrentSess) FontWeight.SemiBold else FontWeight.Normal,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                    modifier = Modifier.weight(1f)
                                )
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(12.dp))
                    HorizontalDivider(color = MaterialTheme.colorScheme.outline.copy(alpha = 0.4f))
                    Spacer(modifier = Modifier.height(8.dp))

                    // 4. 抽屉底部快捷设置与检查更新
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        OutlinedButton(
                            onClick = {
                                coroutineScope.launch { drawerState.close() }
                                showServerConfig = true
                            },
                            modifier = Modifier.weight(1f),
                            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 6.dp)
                        ) {
                            Icon(Icons.Default.Settings, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("节点与设置", fontSize = 12.sp)
                        }
                        OutlinedButton(
                            onClick = {
                                coroutineScope.launch { drawerState.close() }
                                updateController.checkManually()
                            },
                            modifier = Modifier.weight(1f),
                            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 6.dp)
                        ) {
                            Icon(Icons.Default.SystemUpdate, contentDescription = null, modifier = Modifier.size(16.dp))
                            Spacer(modifier = Modifier.width(4.dp))
                            Text("检查更新", fontSize = 12.sp)
                        }
                    }
                }
            }
        }
    ) {
        Scaffold(
            modifier = Modifier
                .fillMaxSize()
                .imePadding(),
            containerColor = MaterialTheme.colorScheme.background,
            contentWindowInsets = WindowInsets.systemBars
        ) { innerPadding ->
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(innerPadding)
            ) {
                // 可自由伸缩的顶部快捷切换胶囊条（点击顶栏九宫格按钮展开/收起，选择后自动收起）
                AnimatedVisibility(
                    visible = showQuickSwitchBar,
                    enter = expandVertically() + fadeIn(),
                    exit = shrinkVertically() + fadeOut()
                ) {
                    Surface(
                        color = SurfaceElevated,
                        tonalElevation = 4.dp,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .horizontalScroll(rememberScrollState())
                                .padding(horizontal = 12.dp, vertical = 6.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            FilterChip(
                                selected = selectedTab == NavigationTab.CHAT,
                                onClick = {
                                    selectedTab = NavigationTab.CHAT
                                    showQuickSwitchBar = false
                                },
                                label = { Text("💬 助理", fontSize = 12.sp) }
                            )
                            FilterChip(
                                selected = selectedTab == NavigationTab.TODOS,
                                onClick = {
                                    selectedTab = NavigationTab.TODOS
                                    showQuickSwitchBar = false
                                },
                                label = {
                                    Text(
                                        if (todosState.pendingCount > 0) "✅ 待办 (${todosState.pendingCount})" else "✅ 待办",
                                        fontSize = 12.sp
                                    )
                                }
                            )
                            FilterChip(
                                selected = selectedTab == NavigationTab.MEETINGS && selectedSubTab == MeetingsExecSubTab.MEETINGS,
                                onClick = {
                                    selectedTab = NavigationTab.MEETINGS
                                    selectedSubTab = MeetingsExecSubTab.MEETINGS
                                    showQuickSwitchBar = false
                                },
                                label = {
                                    Text(
                                        if (meetingsState.waitingCount > 0) "📣 会审 (${meetingsState.waitingCount})" else "📣 会审",
                                        fontSize = 12.sp
                                    )
                                }
                            )
                            FilterChip(
                                selected = selectedTab == NavigationTab.MEETINGS && selectedSubTab == MeetingsExecSubTab.PHONE_APPS,
                                onClick = {
                                    selectedTab = NavigationTab.MEETINGS
                                    selectedSubTab = MeetingsExecSubTab.PHONE_APPS
                                    showQuickSwitchBar = false
                                },
                                label = {
                                    Text(
                                        if (phoneAppState.waitingApprovalCount > 0) "📱 执行 (${phoneAppState.waitingApprovalCount})" else "📱 执行",
                                        fontSize = 12.sp
                                    )
                                }
                            )
                            FilterChip(
                                selected = selectedTab == NavigationTab.DISCOVER,
                                onClick = {
                                    selectedTab = NavigationTab.DISCOVER
                                    showQuickSwitchBar = false
                                },
                                label = { Text("🧭 发现", fontSize = 12.sp) }
                            )
                        }
                    }
                }

                Box(modifier = Modifier.weight(1f)) {
                    when (selectedTab) {
                        NavigationTab.CHAT -> ChatScreen(
                            viewModel = chatViewModel,
                            onOpenSettings = { showServerConfig = true },
                            onOpenDrawer = openDrawer,
                            onToggleQuickBar = toggleQuickBar,
                            totalBadgeCount = totalDrawerBadgeCount
                        )
                        NavigationTab.TODOS -> TodosScreen(
                            viewModel = todosViewModel,
                            onOpenDrawer = openDrawer,
                            onToggleQuickBar = toggleQuickBar,
                            onOpenSettings = { showServerConfig = true }
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
                                            .padding(horizontal = 8.dp, vertical = 4.dp),
                                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                                        verticalAlignment = Alignment.CenterVertically
                                    ) {
                                        IconButton(
                                            onClick = openDrawer,
                                            modifier = Modifier.size(38.dp)
                                        ) {
                                            Icon(
                                                imageVector = Icons.Default.Menu,
                                                contentDescription = "打开导航抽屉",
                                                tint = TextPrimary
                                            )
                                        }
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
                                        IconButton(
                                            onClick = toggleQuickBar,
                                            modifier = Modifier.size(36.dp)
                                        ) {
                                            Icon(
                                                imageVector = Icons.Default.Apps,
                                                contentDescription = "快捷切换栏",
                                                tint = TextSecondary,
                                                modifier = Modifier.size(20.dp)
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
                            },
                            onOpenDrawer = openDrawer,
                            onToggleQuickBar = toggleQuickBar,
                            onOpenSettings = { showServerConfig = true }
                        )
                    }
                }
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
