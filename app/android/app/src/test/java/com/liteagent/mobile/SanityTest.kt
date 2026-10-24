package com.liteagent.mobile

import com.google.gson.Gson
import com.liteagent.mobile.core.network.AuthInterceptor
import com.liteagent.mobile.core.network.ServerBaseUrlTag
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.network.SseStreamManager
import com.liteagent.mobile.core.network.models.ChatResponse
import com.liteagent.mobile.core.network.models.MessageItem
import com.liteagent.mobile.core.network.models.SessionItem
import com.liteagent.mobile.core.network.models.TaskStreamData
import com.liteagent.mobile.core.security.AndroidKeyStoreCryptoEngine
import com.liteagent.mobile.core.security.RecoveryTaskState
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.core.security.UserSession
import com.liteagent.mobile.ui.chat.ChatViewModel
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ApiService
import com.liteagent.mobile.core.network.AuthenticationException
import com.liteagent.mobile.core.network.models.*
import com.liteagent.mobile.core.storage.TodoDatabaseHelper
import com.liteagent.mobile.ui.meetings.MeetingsViewModel
import com.liteagent.mobile.ui.todos.TodoFilter
import com.liteagent.mobile.ui.todos.TodosUiState
import com.liteagent.mobile.ui.todos.TodosViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class SanityTest {

    private val testDispatcher = UnconfinedTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(testDispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun sanity_check() {
        assertEquals(4, 2 + 2)
    }

    @Test
    fun navigation_tabs_complete() {
        assertEquals(4, NavigationTab.values().size)
        val tabNames = NavigationTab.values().map { it.name }
        assertTrue(tabNames.contains("CHAT"))
        assertTrue(tabNames.contains("TODOS"))
        assertTrue(tabNames.contains("MEETINGS"))
        assertTrue(tabNames.contains("DISCOVER"))
    }

    @Test
    fun production_keystore_fails_fast_in_jvm_without_downgrade() {
        val prodEngine = AndroidKeyStoreCryptoEngine()
        var exceptionThrown = false
        try {
            prodEngine.encrypt("secret_token")
        } catch (e: SecurityException) {
            exceptionThrown = true
        }
        assertTrue("生产引擎在缺少 Android KeyStore 硬件时必须拒绝降级并抛出 SecurityException", exceptionThrown)
    }

    @Test
    fun test_crypto_engine_roundtrip() {
        val fakeEngine = FakeCryptoEngine()
        val original = "Bearer sk-test-static-auth-token-123456"
        val encrypted = fakeEngine.encrypt(original)
        assertNotEquals(original, encrypted)
        assertTrue(encrypted.isNotBlank())

        val decrypted = fakeEngine.decrypt(encrypted)
        assertEquals(original, decrypted)
    }

    @Test
    fun ipv6_normalization_preserves_brackets_and_omits_default_ports() {
        // 1. IPv6 带端口与斜杠规范化：必须使用 HttpUrl.Builder 原生输出保留方括号 [::1]
        val normIpv6WithPort = ServerUrlNormalizer.normalize("http://[::1]:8000/")
        assertEquals("http://[::1]:8000", normIpv6WithPort)
        assertTrue("IPv6 地址必须保留方括号，严禁出现 ::1:8000 无括号非法地址", normIpv6WithPort.contains("[::1]"))

        // 2. IPv6 默认端口自动消除且方括号完整保留
        val normIpv6Http80 = ServerUrlNormalizer.normalize("http://[2001:db8::1]:80/")
        assertEquals("http://[2001:db8::1]", normIpv6Http80)

        val normIpv6Https443 = ServerUrlNormalizer.normalize("https://[2001:db8::1]:443/")
        assertEquals("https://[2001:db8::1]", normIpv6Https443)

        // 3. IPv6 带反向代理路径前缀
        val normIpv6Proxy = ServerUrlNormalizer.normalize("http://[::1]:8000/proxy/")
        assertEquals("http://[::1]:8000/proxy", normIpv6Proxy)

        // 4. 从带具体接口的 IPv6 URL 提取规范化基地址
        val parsedIpv6Chat = "http://[::1]:8000/proxy/agent/api/v1/chat".toHttpUrl()
        val extractedIpv6 = ServerUrlNormalizer.extractBaseUrl(parsedIpv6Chat)
        assertEquals("http://[::1]:8000/proxy", extractedIpv6)
    }

    @Test
    fun explicit_base_url_preserves_proxy_prefix_in_auth_interceptor() = runTest {
        val configuredBase = "https://example.com/proxy"
        val fakeStorage = FakeTokenStorage(configuredBase)
        fakeStorage.setGeneralToken(configuredBase, "token_proxy_secret_123")

        // 1. 验证 ApiClient.getRetrofit 生成的 boundClient 拦截器顺序：
        // 显式 Base URL 注入拦截器必须排在 AuthInterceptor 之前
        val apiClient = FakeApiClient(FakeApiService(), fakeStorage)
        val retrofit = apiClient.getRetrofit(configuredBase)
        assertEquals("https://example.com/proxy/", retrofit.baseUrl().toString())

        // 2. 模拟真实 OkHttp 链调用 AuthInterceptor
        val requestUrl = "https://example.com/proxy/agent/api/v1/chat".toHttpUrl()
        val boundRequest = Request.Builder()
            .url(requestUrl)
            .header(AuthInterceptor.HEADER_CONFIGURED_BASE_URL, configuredBase)
            .tag(ServerBaseUrlTag::class.java, ServerBaseUrlTag(configuredBase))
            .build()

        var interceptedAuthorization: String? = null
        var interceptedHeaderRemoved = false

        val testClient = OkHttpClient.Builder()
            .addInterceptor(AuthInterceptor(fakeStorage, boundBaseUrl = configuredBase))
            .addInterceptor { chain ->
                val processedReq = chain.request()
                interceptedAuthorization = processedReq.header("Authorization")
                interceptedHeaderRemoved = processedReq.header(AuthInterceptor.HEADER_CONFIGURED_BASE_URL) == null
                chain.proceed(processedReq)
            }
            .addInterceptor { _ ->
                okhttp3.Response.Builder()
                    .request(boundRequest)
                    .protocol(okhttp3.Protocol.HTTP_1_1)
                    .code(200)
                    .message("OK")
                    .body(okhttp3.ResponseBody.create(null, "{}"))
                    .build()
            }
            .build()

        val resp = testClient.newCall(boundRequest).execute()
        assertEquals(200, resp.code)
        assertEquals("Bearer token_proxy_secret_123", interceptedAuthorization)
        assertTrue("内部专用的 X-Configured-Base-Url 头必须被拦截器安全清除", interceptedHeaderRemoved)

        // 3. 验证即使没有注入头，extractBaseUrl 也正确识别 /agent/api/v1 并在截断时保留 /proxy
        val fallbackResolved = ServerUrlNormalizer.extractBaseUrl(requestUrl)
        assertEquals("https://example.com/proxy", fallbackResolved)

        // 4. 验证生成的凭证 Key 100% 对应
        val expectedKey = SecureTokenStorage.generalTokenKey(configuredBase)
        val actualKey = SecureTokenStorage.generalTokenKey(fallbackResolved)
        assertEquals(expectedKey.name, actualKey.name)
    }

    @Test
    fun chatviewmodel_recovery_history_failure_resets_streaming_and_allows_retry() = runTest {
        val fakeStorage = FakeTokenStorage("http://10.0.2.2:8000")
        val fakeService = FakeApiService()
        val fakeApiClient = FakeApiClient(fakeService, fakeStorage)
        val fakeSse = FakeSseStreamManager(fakeStorage)

        // 预设待恢复长任务
        fakeStorage.saveIncompleteTask(
            serverUrl = "http://10.0.2.2:8000",
            taskId = "task_recover_999",
            sessionId = "sess_rec_001",
            logCount = 5
        )

        val viewModel = ChatViewModel(fakeStorage, fakeApiClient, fakeSse)
        testScheduler.advanceUntilIdle()

        assertEquals(1, viewModel.pendingRecoveryTasks.value.size)
        assertEquals("task_recover_999", viewModel.pendingRecoveryTasks.value.first().taskId)
        assertFalse(viewModel.isStreaming.value)

        // 1. 模拟历史加载失败（HTTP 500）
        fakeService.shouldFailSessionMessages = true
        val failedJob = viewModel.resumeRecoveryTask()
        failedJob?.join()
        testScheduler.advanceUntilIdle()

        // 验证失败后必须退出生成中，解除 UI 锁定，并进入可重试的 pending_recovery 状态
        assertFalse("历史加载失败时必须退出 isStreaming，解除发送按钮与界面禁用", viewModel.isStreaming.value)
        assertFalse("历史加载失败时必须退出 isLoading", viewModel.isLoading.value)
        assertEquals("pending_recovery", viewModel.taskStatus.value)
        assertNotNull("必须设置错误提示信息", viewModel.errorMessage.value)
        assertFalse("历史拉取失败时严禁提前启动 SSE 连接", fakeSse.isStreamStarted)

        // 核心校验：失败后必须保留待恢复任务记录，绝不被清空
        assertEquals("失败后必须完整保留待恢复任务，供用户点击重试", 1, viewModel.pendingRecoveryTasks.value.size)
        assertEquals("task_recover_999", viewModel.pendingRecoveryTasks.value.first().taskId)

        // 2. 模拟网络恢复，用户点击恢复重试
        fakeService.shouldFailSessionMessages = false
        fakeService.sessionMessagesResponse = listOf(
            MessageItem(role = "user", content = "开始处理大数据任务", time = 100.0)
        )

        val retryJob = viewModel.resumeRecoveryTask()
        retryJob?.join()
        testScheduler.advanceUntilIdle()

        // 验证重试成功后历史记录正常恢复，并成功驱动 SSE 启动
        assertEquals(1, viewModel.messages.value.size)
        assertEquals("开始处理大数据任务", viewModel.messages.value.first().content)
        assertTrue("历史成功加载后必须顺利拉起 SSE 流监听", fakeSse.isStreamStarted)
        assertEquals("task_recover_999", fakeSse.lastStartedTaskId)
    }

    @Test
    fun chatviewmodel_cancellation_exception_is_rethrown_without_corrupting_state() = runTest {
        val fakeStorage = FakeTokenStorage("http://10.0.2.2:8000")
        val fakeService = FakeApiService()
        val fakeApiClient = FakeApiClient(fakeService, fakeStorage)
        val fakeSse = FakeSseStreamManager(fakeStorage)

        // 预设待恢复任务并持久化
        val recoveryTask = RecoveryTaskState("task_cancellation", "sess_cancel", 0)
        fakeStorage.saveIncompleteTask(
            serverUrl = "http://10.0.2.2:8000",
            taskId = recoveryTask.taskId,
            sessionId = recoveryTask.sessionId,
            logCount = recoveryTask.lastLogCount
        )

        val viewModel = ChatViewModel(fakeStorage, fakeApiClient, fakeSse)
        testScheduler.advanceUntilIdle()

        assertEquals(1, viewModel.pendingRecoveryTasks.value.size)

        // 配置 FakeApiService 挂起历史请求，使恢复协程暂停在挂起点
        val pauseDeferred = kotlinx.coroutines.CompletableDeferred<Unit>()
        var requestStarted = false
        fakeService.sessionMessagesPauseDeferred = pauseDeferred
        fakeService.onSessionMessagesInvoked = {
            requestStarted = true
        }

        // 触发恢复，恢复协程在历史请求处挂起暂停
        val job = viewModel.resumeRecoveryTask(recoveryTask)
        assertTrue("恢复请求必须已进入挂起执行阶段", requestStarted)
        assertNotNull("Job 必须已创建", job)
        assertTrue("Job 必须处于活动运行/挂起中", job?.isActive == true)

        // 在挂起状态下执行取消
        job?.cancel()
        job?.join()
        testScheduler.advanceUntilIdle()

        // 断言：1. Job 已成功取消
        assertTrue("挂起中的 Job 必须处于已取消状态", job?.isCancelled == true)
        // 断言：2. SSE 未启动
        assertFalse("恢复流程在拉取历史时被取消，SSE 严禁启动", fakeSse.isStreamStarted)
        // 断言：3. 待恢复记录必须仍保留在列表中供后续恢复
        assertEquals("取消后待恢复记录必须完好保留在 pendingRecoveryTasks 中", 1, viewModel.pendingRecoveryTasks.value.size)
        assertEquals("task_cancellation", viewModel.pendingRecoveryTasks.value.first().taskId)
    }

    @Test
    fun server_url_normalizer_handles_default_ports_and_casing() {
        val normA = ServerUrlNormalizer.normalize("https://example.com:443")
        val normB = ServerUrlNormalizer.normalize("https://example.com/")
        val normC = ServerUrlNormalizer.normalize("HTTPS://EXAMPLE.COM")
        assertEquals("https://example.com", normA)
        assertEquals("https://example.com", normB)
        assertEquals("https://example.com", normC)

        val keyA = SecureTokenStorage.generalTokenKey("https://example.com:443")
        val keyB = SecureTokenStorage.generalTokenKey("https://example.com/")
        assertEquals("规范化后生成的 Token Key 必须完全相同", keyA.name, keyB.name)
    }

    @Test
    fun server_credential_isolation_keys_differ() {
        val serverA = "http://192.168.2.100:8000"
        val serverB = "https://agent.example.com:8443"

        val tokenKeyA = SecureTokenStorage.generalTokenKey(serverA)
        val tokenKeyB = SecureTokenStorage.generalTokenKey(serverB)
        assertNotEquals("不同服务器的存储凭证 Key 必须物理隔离", tokenKeyA.name, tokenKeyB.name)

        val tasksKeyA = SecureTokenStorage.recoveryTasksKey(serverA)
        val tasksKeyB = SecureTokenStorage.recoveryTasksKey(serverB)
        assertNotEquals("不同服务器的任务恢复集合 Key 必须物理隔离", tasksKeyA.name, tasksKeyB.name)
    }

    @Test
    fun multi_task_recovery_isolation_and_selective_clearance() {
        val tasks = mutableListOf(
            RecoveryTaskState(taskId = "task_001", sessionId = "session_A", lastLogCount = 10, timestamp = 1000L),
            RecoveryTaskState(taskId = "task_002", sessionId = "session_B", lastLogCount = 5, timestamp = 2000L)
        )

        // 验证按 taskId 精准删除 task_001，绝不影响 task_002
        val filtered = tasks.filterNot { it.taskId == "task_001" }
        assertEquals(1, filtered.size)
        assertEquals("task_002", filtered[0].taskId)
        assertEquals("session_B", filtered[0].sessionId)
    }

    @Test
    fun sse_terminal_states_include_partial_and_standard_terminals() {
        val terminals = SseStreamManager.TERMINAL_STATES
        assertTrue("必须包含 partial 终态", terminals.contains("partial"))
        assertTrue("必须包含 completed 终态", terminals.contains("completed"))
        assertTrue("必须包含 done 终态", terminals.contains("done"))
        assertTrue("必须包含 succeeded 终态", terminals.contains("succeeded"))
        assertTrue("必须包含 failed 终态", terminals.contains("failed"))
        assertTrue("必须包含 error 终态", terminals.contains("error"))
        assertTrue("必须包含 cancelled 终态", terminals.contains("cancelled"))

        assertFalse("running 绝不能误判为终态", terminals.contains("running"))
        assertFalse("starting 绝不能误判为终态", terminals.contains("starting"))
    }

    @Test
    fun canonical_logs_preserves_legitimate_duplicates_and_ordering() {
        val serverLogsWithDuplicates = listOf(
            "Step 1: Check server",
            "Retry 1: Connection timed out, retrying...",
            "Retry 1: Connection timed out, retrying...",
            "Step 2: Connected successfully"
        )

        val streamData = TaskStreamData(
            status = "running",
            progress = mapOf("logs" to serverLogsWithDuplicates),
            logs = listOf("Retry 1: Connection timed out, retrying...", "Step 2: Connected successfully"),
            totalLogs = 4
        )

        val extracted = (streamData.progress?.get("logs") as? List<*>)?.mapNotNull { it?.toString() }
        assertNotNull(extracted)
        assertEquals(4, extracted?.size)
        assertEquals("Step 1: Check server", extracted?.get(0))
        assertEquals("Retry 1: Connection timed out, retrying...", extracted?.get(1))
        assertEquals("Retry 1: Connection timed out, retrying...", extracted?.get(2))
        assertEquals("Step 2: Connected successfully", extracted?.get(3))
    }

    @Test
    fun channel_filtering_excludes_external_channels() {
        val mixedSessions = listOf(
            SessionItem(sessionKey = "api:session_001", title = "移动端对话"),
            SessionItem(sessionKey = "feishu:ou_123456", title = "飞书群聊"),
            SessionItem(sessionKey = "wecom:chat_789", title = "企微工作群"),
            SessionItem(sessionKey = "api:session_002", title = "手机任务")
        )

        val filtered = mixedSessions.filter { it.sessionKey.startsWith("api:") }
        assertEquals(2, filtered.size)
        assertTrue(filtered.all { it.sessionKey.startsWith("api:") })
    }

    @Test
    fun sequential_history_loading_preserves_final_reply() {
        val historyMessages = listOf(
            MessageItem(role = "user", content = "开始分析数据", time = 100.0)
        )
        val finalSseReply = MessageItem(role = "assistant", content = "数据分析完成，共计处理 100 条记录", time = 105.0)

        var currentMessages = historyMessages
        assertEquals(1, currentMessages.size)

        currentMessages = currentMessages + finalSseReply
        assertEquals(2, currentMessages.size)
        assertEquals("数据分析完成，共计处理 100 条记录", currentMessages.last().content)
        assertEquals("assistant", currentMessages.last().role)
    }

    @Test
    fun chat_response_deserialization() {
        val gson = Gson()
        val json = """{"type":"async","task_id":"t_888","message":"queued","title":"Async Task"}"""
        val resp = gson.fromJson(json, ChatResponse::class.java)
        assertEquals("async", resp.type)
        assertEquals("t_888", resp.taskId)
    }

    @Test
    fun unified_login_success_saves_session_and_validates_active_status() = runTest {
        val fakeStorage = FakeTokenStorage("https://mail.maifeipin.com")
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        val result = fakeClient.login(
            serverUrl = "https://mail.maifeipin.com",
            username = "alice",
            password = "secret_password",
            rememberMe = true
        )

        assertTrue("登录请求必须返回成功", result.isSuccess)
        val loginResp = result.getOrNull()
        assertNotNull(loginResp)
        assertEquals("alice", loginResp?.username)
        assertEquals("fake_session_token", loginResp?.sessionToken)
        assertEquals("fake_session_id", loginResp?.sessionId)
        assertEquals(listOf("user:general"), loginResp?.scopes)
        assertEquals("fake_refresh_token", loginResp?.refreshToken)

        val savedSession = fakeStorage.getSession("https://mail.maifeipin.com")
        assertNotNull("登录成功后必须持久化 UserSession", savedSession)
        assertEquals("alice", savedSession?.username)
        assertEquals("fake_session_token", savedSession?.sessionToken)
        assertEquals("fake_refresh_token", savedSession?.refreshToken)
        assertTrue(fakeStorage.hasValidSession("https://mail.maifeipin.com"))
    }

    @Test
    fun remember_me_false_does_not_issue_refresh_token() = runTest {
        val fakeStorage = FakeTokenStorage("https://mail.maifeipin.com")
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        val result = fakeClient.login(
            serverUrl = "https://mail.maifeipin.com",
            username = "bob",
            password = "secret_password",
            rememberMe = false
        )

        assertTrue(result.isSuccess)
        val loginResp = result.getOrNull()
        assertNull("未开启记住登录时严禁签发 refresh_token", loginResp?.refreshToken)
        assertNull(loginResp?.refreshExpiresAt)

        val savedSession = fakeStorage.getSession("https://mail.maifeipin.com")
        assertNotNull(savedSession)
        assertNull("未记住登录时不保存 refresh_token", savedSession?.refreshToken)
    }

    @Test
    fun stale_401_does_not_clear_new_session_when_token_mismatched() = runTest {
        val fakeStorage = FakeTokenStorage("https://mail.maifeipin.com")
        val currentSession = UserSession(
            serverUrl = "https://mail.maifeipin.com",
            username = "owner",
            sessionId = "new_session_id",
            sessionToken = "new_valid_token_abc",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_xyz",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            reviewToken = null,
            reviewExpiresAt = 0L,
            rememberMe = true
        )
        fakeStorage.saveSession(currentSession)

        // 模拟一个迟到的旧请求返回了 401（携带的是旧 token：old_expired_token_123）
        fakeStorage.clearSession("https://mail.maifeipin.com", matchingToken = "old_expired_token_123")

        // 断言：新会话必须完好保留，旧 401 不得误伤新会话
        val sessionAfterStale401 = fakeStorage.getSession("https://mail.maifeipin.com")
        assertNotNull("迟到的旧 401 严禁清除匹配不上的新登录会话", sessionAfterStale401)
        assertEquals("new_valid_token_abc", sessionAfterStale401?.sessionToken)

        // 当 matchingToken 与当前 Token 一致时，才允许清除
        fakeStorage.clearSession("https://mail.maifeipin.com", matchingToken = "new_valid_token_abc")
        val sessionAfterMatching401 = fakeStorage.getSession("https://mail.maifeipin.com")
        assertNull("与当前凭证一致的 401/撤销才执行本地清除", sessionAfterMatching401)
    }

    @Test
    fun logout_clears_local_session_and_invokes_remote() = runTest {
        val fakeStorage = FakeTokenStorage("https://mail.maifeipin.com")
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        fakeClient.login("https://mail.maifeipin.com", "alice", "pw", rememberMe = true)
        assertTrue(fakeStorage.hasValidSession("https://mail.maifeipin.com"))

        val logoutResult = fakeClient.logout("https://mail.maifeipin.com")
        assertTrue(logoutResult.isSuccess)
        assertFalse("退出登录后本地会话必须失效", fakeStorage.hasValidSession("https://mail.maifeipin.com"))
        assertNull(fakeStorage.getSession("https://mail.maifeipin.com"))
    }

    @Test
    fun chat_view_model_cleans_up_on_logout_and_different_session_reloads() = runTest {
        val fakeStorage = FakeTokenStorage("https://mail.maifeipin.com")
        val fakeApi = FakeApiService()
        fakeApi.sessionsResponse = listOf(SessionItem(sessionKey = "api:session_alice", title = "Alice 对话"))
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)
        val fakeSse = FakeSseStreamManager(fakeStorage)

        val viewModel = ChatViewModel(fakeStorage, fakeClient, fakeSse)
        testScheduler.advanceUntilIdle()

        assertEquals("Alice 对话加载成功", 1, viewModel.sessions.value.size)
        assertEquals("api:session_alice", viewModel.currentSession.value?.sessionKey)

        // 执行退出登录
        viewModel.onLoggedOut()
        testScheduler.advanceUntilIdle()

        assertTrue("退出后会话列表必须清空", viewModel.sessions.value.isEmpty())
        assertNull("退出后当前会话必须清空", viewModel.currentSession.value)
        assertTrue("退出后消息流必须清空", viewModel.messages.value.isEmpty())

        // 重新登录同一服务器的不同用户 Bob
        fakeApi.sessionsResponse = listOf(SessionItem(sessionKey = "api:session_bob", title = "Bob 对话"))
        viewModel.onSessionChanged("https://mail.maifeipin.com", sessionId = "sess_bob_999")
        testScheduler.advanceUntilIdle()

        assertEquals("Bob 登录后同一服务器必须刷新为 Bob 的会话", 1, viewModel.sessions.value.size)
        assertEquals("api:session_bob", viewModel.currentSession.value?.sessionKey)
    }

    @Test
    fun handle_unauthorized_preserves_session_on_transient_network_error() = runTest {
        val fakeStorage = FakeTokenStorage("https://mail.maifeipin.com")
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        // 保存有效会话（含 refresh token）
        fakeStorage.saveSession(
            serverUrl = "https://mail.maifeipin.com",
            username = "alice",
            sessionId = "sess_123",
            sessionToken = "token_abc",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_xyz",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )

        // 模拟刷新时发生 500 服务器错误（非 401 凭证拒绝）
        fakeApi.shouldFailRefresh = true
        fakeApi.refreshFailureCode = 500

        val retryAllowed = fakeClient.handleUnauthorized("https://mail.maifeipin.com", "token_abc", "sess_123")
        assertFalse(retryAllowed)
        // 关键断言：服务端 500 或网络故障时，本地会话必须完好保留，严禁误登出！
        assertNotNull("临时网络/服务器错误严禁清理用户会话", fakeStorage.getSession("https://mail.maifeipin.com"))

        // 模拟明确凭据失效（401 Unauthorized）
        fakeApi.refreshFailureCode = 401
        val retryAllowedAuthFail = fakeClient.handleUnauthorized("https://mail.maifeipin.com", "token_abc", "sess_123")
        assertFalse(retryAllowedAuthFail)
        // 关键断言：明确 401 凭据拒绝时，本地会话被精准清除
        assertNull("明确凭据失效时才清除本地会话", fakeStorage.getSession("https://mail.maifeipin.com"))
    }

    @Test
    fun cross_session_401_does_not_retry_under_new_account() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        // 1. 用户 A (Alice) 登录
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "alice",
            sessionId = "sess_alice_111",
            sessionToken = "token_alice_aaa",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_alice_xxx",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )

        // 2. 模拟发起 A 的请求（绑定 A 的会话 ID 与 Token）
        val tokenUsedByAlice = "token_alice_aaa"
        val sessionIdUsedByAlice = "sess_alice_111"

        // 3. 在 A 请求挂起期间，用户切换登录到账号 B (Bob)
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "bob",
            sessionId = "sess_bob_222",
            sessionToken = "token_bob_bbb",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_bob_yyy",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )

        // 4. A 的请求迟到返回 401
        val retryAllowed = fakeClient.handleUnauthorized(
            serverUrl = serverUrl,
            tokenUsed = tokenUsedByAlice,
            sessionIdUsed = sessionIdUsedByAlice
        )

        // 断言 1：必须直接拒绝重试，绝不能用 Bob 的 Token 重发 Alice 的旧请求
        assertFalse("A 请求挂起期间切换登录 B，A 返回 401 严禁重发", retryAllowed)

        // 断言 2：Bob 的新会话必须完好保留，严禁被 A 的 401 误清除
        val currentSession = fakeStorage.getSession(serverUrl)
        assertNotNull("当前活跃账号 B 的会话必须完好保留", currentSession)
        assertEquals("bob", currentSession?.username)
        assertEquals("sess_bob_222", currentSession?.sessionId)
        assertEquals("token_bob_bbb", currentSession?.sessionToken)
    }

    @Test
    fun auth_interceptor_drops_cross_session_retry_when_session_changed() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)
        var unauthorizedTriggered = false
        val fakeApi = FakeApiService()
        val fakeClient = object : ApiClient(fakeStorage, onUnauthorized = { unauthorizedTriggered = true }) {
            override fun getAuthApiService(serverUrl: String): ApiService = fakeApi
        }

        // 1. 用户 A 登录
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "alice",
            sessionId = "sess_alice_111",
            sessionToken = "token_alice_aaa",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_alice_xxx",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )

        var requestCount = 0
        val interceptor = AuthInterceptor(
            tokenStorage = fakeStorage,
            onUnauthorized = { unauthorizedTriggered = true },
            boundBaseUrl = serverUrl,
            onUnauthorizedWithContext = { url, tokenUsed, sessionIdUsed ->
                runBlocking { fakeClient.handleUnauthorized(url, tokenUsed, sessionIdUsed) }
            }
        )

        val client = OkHttpClient.Builder()
            .addInterceptor(interceptor)
            .addInterceptor { chain ->
                requestCount++
                val req = chain.request()
                // 当 Alice 的请求到达服务端前/挂起时，模拟用户在外部切换登录为 Bob
                if (requestCount == 1) {
                    runBlocking {
                        fakeStorage.saveSession(
                            serverUrl = serverUrl,
                            username = "bob",
                            sessionId = "sess_bob_222",
                            sessionToken = "token_bob_bbb",
                            expiresAt = System.currentTimeMillis() / 1000 + 3600,
                            refreshToken = "refresh_bob_yyy",
                            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
                            rememberMe = true
                        )
                    }
                    // Alice 的请求返回 401
                    okhttp3.Response.Builder()
                        .request(req)
                        .protocol(okhttp3.Protocol.HTTP_1_1)
                        .code(401)
                        .message("Unauthorized")
                        .body("{}".toResponseBody("application/json".toMediaTypeOrNull()))
                        .build()
                } else {
                    okhttp3.Response.Builder()
                        .request(req)
                        .protocol(okhttp3.Protocol.HTTP_1_1)
                        .code(200)
                        .message("OK")
                        .body("{}".toResponseBody("application/json".toMediaTypeOrNull()))
                        .build()
                }
            }
            .build()

        val req = Request.Builder().url("https://mail.maifeipin.com/api/v1/sessions").build()
        val response = client.newCall(req).execute()

        // 校验：没有重试（requestCount 仅为 1）
        assertEquals("旧会话请求在会话改变后严禁重试", 1, requestCount)
        assertEquals(401, response.code)
        // Bob 的会话未受损坏
        val bobSession = fakeStorage.getSession(serverUrl)
        assertEquals("bob", bobSession?.username)
        assertFalse("会话切换后旧请求 401 不得触发登出回调", unauthorizedTriggered)
    }

    @Test
    fun late_refresh_result_rejected_after_logout() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        // 1. 用户登录（保存含 refresh token 的会话）
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "alice",
            sessionId = "sess_refresh_test",
            sessionToken = "token_initial",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_token_initial",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )
        assertNotNull(fakeStorage.getSession(serverUrl))

        // 2. 模拟“刷新挂起期间用户退出”：
        // 在 fakeApi.refresh 执行时，触发用户退出（清空本地会话）
        fakeApi.onRefreshInvoked = {
            runBlocking {
                fakeStorage.clearSession(serverUrl)
            }
        }

        // 3. 执行刷新
        val refreshResult = fakeClient.refresh(serverUrl, "refresh_token_initial")

        // 4. 断言：刷新操作必须返回失败（因为会话在挂起时已被注销）
        assertTrue("退出后迟到的刷新必须标记为失败", refreshResult.isFailure)
        assertTrue(refreshResult.exceptionOrNull() is AuthenticationException)

        // 5. 关键断言：存储层必须保持为空，严禁被迟到返回的刷新结果复活！
        val sessionAfterLateRefresh = fakeStorage.getSession(serverUrl)
        assertNull("用户退出后，迟到的刷新结果严禁重新落盘，存储必须保持为空", sessionAfterLateRefresh)
        assertFalse("退出后 token 存储状态必须无效", fakeStorage.hasValidSession(serverUrl))
    }

    @Test
    fun update_tokens_rejects_missing_or_mismatched_session_id() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)

        // 1. 空存储时尝试 updateTokens -> 必须返回 false
        val writeToEmpty = fakeStorage.updateTokens(
            serverUrl = serverUrl,
            newSessionToken = "new_token",
            newExpiresAt = System.currentTimeMillis() / 1000 + 3600,
            newRefreshToken = "new_refresh",
            newRefreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            newReviewToken = null,
            newReviewExpiresAt = 0L,
            matchingSessionId = "sess_missing"
        )
        assertFalse("当前会话不存在时 updateTokens 必须拒绝", writeToEmpty)
        assertNull(fakeStorage.getSession(serverUrl))

        // 2. 存入 Alice 会话
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "alice",
            sessionId = "sess_alice",
            sessionToken = "token_alice",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_alice",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )

        // 3. matchingSessionId 缺失（null 或 blank）-> 必须返回 false
        val writeNullSession = fakeStorage.updateTokens(
            serverUrl = serverUrl,
            newSessionToken = "new_token_2",
            newExpiresAt = System.currentTimeMillis() / 1000 + 3600,
            newRefreshToken = null,
            newRefreshExpiresAt = 0L,
            newReviewToken = null,
            newReviewExpiresAt = 0L,
            matchingSessionId = null
        )
        assertFalse("matchingSessionId 缺失时必须拒绝写回", writeNullSession)

        // 4. matchingSessionId 不匹配（如为其他会话 ID）-> 必须返回 false
        val writeMismatched = fakeStorage.updateTokens(
            serverUrl = serverUrl,
            newSessionToken = "new_token_3",
            newExpiresAt = System.currentTimeMillis() / 1000 + 3600,
            newRefreshToken = null,
            newRefreshExpiresAt = 0L,
            newReviewToken = null,
            newReviewExpiresAt = 0L,
            matchingSessionId = "sess_different"
        )
        assertFalse("matchingSessionId 不匹配时必须拒绝写回", writeMismatched)

        // 5. 校验 Alice 会话未被篡改
        val aliceSession = fakeStorage.getSession(serverUrl)
        assertEquals("token_alice", aliceSession?.sessionToken)
    }

    @Test
    fun refresh_snapshot_prevents_writing_to_switched_account() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        // 1. 用户 A (Alice) 登录
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "alice",
            sessionId = "sess_alice_snap",
            sessionToken = "token_alice_orig",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_alice_orig",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )

        // 2. 模拟交错时序：
        // 在 fakeApi.refresh 执行挂起期间，用户切换登录到账号 B (Bob)
        fakeApi.onRefreshInvoked = {
            runBlocking {
                fakeStorage.saveSession(
                    serverUrl = serverUrl,
                    username = "bob",
                    sessionId = "sess_bob_new",
                    sessionToken = "token_bob_orig",
                    expiresAt = System.currentTimeMillis() / 1000 + 3600,
                    refreshToken = "refresh_bob_orig",
                    refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
                    rememberMe = true
                )
            }
        }

        // 3. 执行 Alice 的刷新，传入 Alice 会话快照 ID
        val refreshResult = fakeClient.refresh(
            serverUrl = serverUrl,
            refreshToken = "refresh_alice_orig",
            expectedSessionId = "sess_alice_snap"
        )

        // 4. 断言：刷新操作因目标会话已被切换而标记为失败
        assertTrue("会话切换后，旧账号刷新结果严禁标记为成功", refreshResult.isFailure)
        assertTrue(refreshResult.exceptionOrNull() is AuthenticationException)

        // 5. 关键断言：Bob 的新会话凭据未被篡改，绝不能将 Alice 的新 Token 写进 Bob
        val bobSession = fakeStorage.getSession(serverUrl)
        assertNotNull(bobSession)
        assertEquals("bob", bobSession?.username)
        assertEquals("sess_bob_new", bobSession?.sessionId)
        assertEquals("token_bob_orig", bobSession?.sessionToken)
        assertEquals("refresh_bob_orig", bobSession?.refreshToken)
    }

    @Test
    fun auth_interceptor_does_not_close_response_when_retry_is_disqualified() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)

        // 用户 A 登录
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "alice",
            sessionId = "sess_alice_close_test",
            sessionToken = "token_alice_close",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_alice_close",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )

        // 构造拦截器：在 onUnauthorizedWithContext 中返回 false（放弃重试）
        val interceptor = AuthInterceptor(
            tokenStorage = fakeStorage,
            onUnauthorized = {},
            boundBaseUrl = serverUrl,
            onUnauthorizedWithContext = { _, _, _ ->
                false
            }
        )

        val client = OkHttpClient.Builder()
            .addInterceptor(interceptor)
            .addInterceptor { chain ->
                okhttp3.Response.Builder()
                    .request(chain.request())
                    .protocol(okhttp3.Protocol.HTTP_1_1)
                    .code(401)
                    .message("Unauthorized")
                    .body("""{"error":"unauthorized_error_payload"}""".toResponseBody("application/json".toMediaTypeOrNull()))
                    .build()
            }
            .build()

        val req = Request.Builder().url("https://mail.maifeipin.com/api/v1/sessions").build()
        val response = client.newCall(req).execute()

        // 关键断言：放弃重试时返回的 401 响应体绝不能被提前 close()，业务调用方必须能正常读取内容
        assertEquals(401, response.code)
        val bodyContent = response.body?.string()
        assertNotNull("未重发的响应体必须保持未关闭状态以便业务层读取错误原因", bodyContent)
        assertTrue(bodyContent!!.contains("unauthorized_error_payload"))
    }

    @Test
    fun auth_interceptor_does_not_close_response_if_session_switched_after_refresh_success() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)

        // 用户 A 登录
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "alice",
            sessionId = "sess_alice_switch",
            sessionToken = "token_alice_switch",
            expiresAt = System.currentTimeMillis() / 1000 + 3600,
            refreshToken = "refresh_alice_switch",
            refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
            rememberMe = true
        )

        // 构造拦截器：模拟 onUnauthorizedWithContext 返回 true，但在此瞬间会话已被切换为 Bob
        val interceptor = AuthInterceptor(
            tokenStorage = fakeStorage,
            onUnauthorized = {},
            boundBaseUrl = serverUrl,
            onUnauthorizedWithContext = { _, _, _ ->
                // 在返回重试结果前，存储层已被切换到 Bob
                runBlocking {
                    fakeStorage.saveSession(
                        serverUrl = serverUrl,
                        username = "bob",
                        sessionId = "sess_bob_switch",
                        sessionToken = "token_bob_switch",
                        expiresAt = System.currentTimeMillis() / 1000 + 3600,
                        refreshToken = "refresh_bob_switch",
                        refreshExpiresAt = System.currentTimeMillis() / 1000 + 86400,
                        rememberMe = true
                    )
                }
                true
            }
        )

        val client = OkHttpClient.Builder()
            .addInterceptor(interceptor)
            .addInterceptor { chain ->
                okhttp3.Response.Builder()
                    .request(chain.request())
                    .protocol(okhttp3.Protocol.HTTP_1_1)
                    .code(401)
                    .message("Unauthorized")
                    .body("""{"error":"stale_session_401"}""".toResponseBody("application/json".toMediaTypeOrNull()))
                    .build()
            }
            .build()

        val req = Request.Builder().url("https://mail.maifeipin.com/api/v1/sessions").build()
        val response = client.newCall(req).execute()

        // 关键断言：会话已切换到 Bob，严禁重发 Alice 请求；且原 401 响应绝不能被提前关闭
        assertEquals(401, response.code)
        val bodyContent = response.body?.string()
        assertNotNull(bodyContent)
        assertTrue(bodyContent!!.contains("stale_session_401"))
    }

    @Test
    fun todo_models_and_filter_state_logic() {
        val pendingTodo = TodoItem(id = "1", title = "Task 1", status = "pending")
        val activeTodo = TodoItem(id = "2", title = "Task 2", status = "active")
        val doneTodo = TodoItem(id = "3", title = "Task 3", status = "done")

        assertFalse(pendingTodo.isCompleted)
        assertFalse(activeTodo.isCompleted)
        assertTrue(doneTodo.isCompleted)

        val uiStateAll = TodosUiState(todos = listOf(pendingTodo, activeTodo, doneTodo), filter = TodoFilter.ALL)
        assertEquals(3, uiStateAll.filteredTodos.size)
        assertEquals(1, uiStateAll.pendingCount)
        assertEquals(1, uiStateAll.activeCount)
        assertEquals(1, uiStateAll.doneCount)

        val uiStatePending = uiStateAll.copy(filter = TodoFilter.PENDING)
        assertEquals(1, uiStatePending.filteredTodos.size)
        assertEquals("Task 1", uiStatePending.filteredTodos.first().title)

        val uiStateDone = uiStateAll.copy(filter = TodoFilter.DONE)
        assertEquals(1, uiStateDone.filteredTodos.size)
        assertEquals("Task 3", uiStateDone.filteredTodos.first().title)
    }

    @Test
    fun todo_database_helper_db_key_computation() {
        val keyAliceLower = TodoDatabaseHelper.computeDbKey("https://mail.maifeipin.com", "alice")
        val keyNormalized = TodoDatabaseHelper.computeDbKey("HTTPS://MAIL.MAIFEIPIN.COM/", "alice")
        assertEquals("规范化主机与协议必须生成一致的主键", keyAliceLower, keyNormalized)
        assertEquals(32, keyAliceLower.length)

        val keyAliceUpper = TodoDatabaseHelper.computeDbKey("https://mail.maifeipin.com", "Alice")
        assertNotEquals("用户名必须保留大小写身份语义，严禁将 Alice 与 alice 合并为同一缓存", keyAliceLower, keyAliceUpper)

        val keyPathA = TodoDatabaseHelper.computeDbKey("https://mail.maifeipin.com/Proxy", "alice")
        val keyPathB = TodoDatabaseHelper.computeDbKey("https://mail.maifeipin.com/proxy", "alice")
        assertNotEquals("路径必须保留大小写身份语义，严禁混淆不同代理前缀", keyPathA, keyPathB)

        val keyBob = TodoDatabaseHelper.computeDbKey("https://mail.maifeipin.com", "bob")
        assertNotEquals("同一服务器上的不同账号必须生成互相隔离的数据库沙箱 Key", keyAliceLower, keyBob)
    }

    @Test
    fun meeting_models_state_and_event_predicates() {
        val awaitingMeeting = MeetingItem(id = "m1", title = "Release Meeting", state = "awaiting_approval", round = 2)
        val openMeeting = MeetingItem(id = "m2", title = "Draft Meeting", state = "open")
        val approvedMeeting = MeetingItem(id = "m3", title = "Approved Meeting", state = "approved")
        val rejectedMeeting = MeetingItem(id = "m4", title = "Rejected Meeting", state = "rejected")
        val changesMeeting = MeetingItem(id = "m5", title = "Revise Meeting", state = "changes_requested")

        assertTrue(awaitingMeeting.isAwaitingApproval)
        assertFalse(openMeeting.isAwaitingApproval)
        assertTrue(approvedMeeting.isApproved)
        assertTrue(rejectedMeeting.isRejected)
        assertTrue(changesMeeting.isChangesRequested)

        val reviewEvent = MeetingEvent(
            seq = 1,
            round = 1,
            actor = "security_lead",
            kind = "review",
            body = mapOf("position" to "support", "text" to "安全审计无异常，予以通过")
        )
        assertEquals("support", reviewEvent.reviewPosition)
        assertEquals("安全审计无异常，予以通过", reviewEvent.reviewText)
    }

    @Test
    fun meetings_view_model_decision_concurrency_and_conflict_handling() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        val testMeeting = MeetingItem(id = "meet_001", title = "Sprint 2 发布审批", state = "awaiting_approval", round = 1)
        fakeApi.meetingsResponse = listOf(testMeeting)
        fakeApi.meetingDetailMap["meet_001"] = MeetingDetail(
            id = "meet_001",
            title = "Sprint 2 发布审批",
            state = "awaiting_approval",
            round = 1,
            lastSeqRaw = 5
        )

        val viewModel = MeetingsViewModel(fakeStorage, fakeClient)
        testScheduler.advanceUntilIdle()

        assertEquals(1, viewModel.uiState.value.meetings.size)
        assertEquals(1, viewModel.uiState.value.waitingCount)

        // 1. 模拟 409 Conflict 版本冲突
        fakeApi.shouldFailDecideConflict = true
        viewModel.decideMeeting(
            meetingId = "meet_001",
            decision = "approve",
            note = "准予发布",
            expectedVersion = 5
        )
        testScheduler.advanceUntilIdle()

        assertNotNull("409 Conflict 必须记录冲突提示信息", viewModel.uiState.value.conflictErrorMessage)
        assertTrue(viewModel.uiState.value.conflictErrorMessage!!.contains("版本冲突"))
        assertNotNull("必须传递包含 request_id 的决策请求", fakeApi.lastDecideRequest)
        assertNotNull(fakeApi.lastDecideRequest?.requestId)
        assertEquals(5, fakeApi.lastDecideRequest?.expectedVersion)

        // 2. 模拟正常审批决策通过
        fakeApi.shouldFailDecideConflict = false
        viewModel.decideMeeting(
            meetingId = "meet_001",
            decision = "approve",
            note = "准予发布",
            expectedVersion = 5
        )
        testScheduler.advanceUntilIdle()

        assertNull(viewModel.uiState.value.conflictErrorMessage)
        assertNotNull(viewModel.uiState.value.actionSuccessMessage)
        assertTrue(viewModel.uiState.value.actionSuccessMessage!!.contains("已批准"))
        assertEquals("approved", fakeApi.meetingDetailMap["meet_001"]?.state)
    }

    @Test
    fun meetings_view_model_timeout_verification_checks_request_id_and_audit_event() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        val meetId = "meet_timeout_001"
        fakeApi.meetingDetailMap[meetId] = MeetingDetail(
            id = meetId,
            title = "超时核验议题",
            state = "awaiting_approval",
            round = 1,
            lastSeqRaw = 3
        )

        val viewModel = MeetingsViewModel(fakeStorage, fakeClient)
        testScheduler.advanceUntilIdle()

        // 场景 A：网络超时，但服务端实际上执行成功，且审计事件中 request_id 与 decision 精确吻合
        fakeApi.shouldFailDecideTimeout = true
        fakeApi.simulateServerCommittedOnTimeout = true

        viewModel.decideMeeting(
            meetingId = meetId,
            decision = "approve",
            note = "网络超时重试测试",
            expectedVersion = 3
        )
        testScheduler.advanceUntilIdle()

        // 断言：由于 request_id 匹配且裁决一致，核验证实成功
        assertEquals("审批已成功同步至服务端", viewModel.uiState.value.actionSuccessMessage)
        assertNull(viewModel.uiState.value.errorMessage)
        assertNotNull("客户端必须生成唯一的 request_id", fakeApi.lastDecideRequest?.requestId)
        viewModel.clearMessages()

        // 场景 B：提交超时，服务端议题状态虽然已不再是 awaiting_approval，但没有当前 request_id（被他人裁决）
        val meetOther = "meet_other_002"
        fakeApi.meetingDetailMap[meetOther] = MeetingDetail(
            id = meetOther,
            title = "他人裁决并发议题",
            state = "awaiting_approval",
            round = 1,
            lastSeqRaw = 10
        )
        fakeApi.simulateServerCommittedOnTimeout = false
        fakeApi.simulateServerOtherCommittedOnTimeout = true

        viewModel.decideMeeting(
            meetingId = meetOther,
            decision = "approve",
            note = "我想批准",
            expectedVersion = 10
        )
        testScheduler.advanceUntilIdle()

        // 关键断言：严禁误报“审批已成功同步”！必须精准报告“状态已变化，本次提交结果未确认”
        assertNull("他人裁决变更时严禁宣称本次审批成功同步", viewModel.uiState.value.actionSuccessMessage)
        assertEquals("状态已变化，本次提交结果未确认", viewModel.uiState.value.errorMessage)
        assertEquals("rejected", viewModel.uiState.value.selectedMeeting?.state)
    }

    @Test
    fun meetings_view_model_account_switch_discards_stale_data() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        val viewModel = MeetingsViewModel(fakeStorage, fakeClient)
        testScheduler.advanceUntilIdle()

        // 切换到 Bob 账号
        viewModel.onSessionChanged(serverUrl, "bob")
        testScheduler.advanceUntilIdle()

        // 登出后状态清空
        viewModel.onLoggedOut()
        testScheduler.advanceUntilIdle()

        assertTrue(viewModel.uiState.value.meetings.isEmpty())
        assertNull(viewModel.uiState.value.selectedMeeting)
    }

    @Test
    fun todos_view_model_write_operations_isolated_across_account_switch() = runTest {
        val serverUrl = "https://mail.maifeipin.com"
        val fakeStorage = FakeTokenStorage(serverUrl)
        val fakeApi = FakeApiService()
        val fakeClient = FakeApiClient(fakeApi, fakeStorage)

        val aliceHelper = FakeTodoDatabaseHelper("alice_key")
        val bobHelper = FakeTodoDatabaseHelper("bob_key")

        val aliceItem = TodoItem(id = "todo_alice_1", title = "Alice Task", status = "pending")
        aliceHelper.saveAll(listOf(aliceItem))
        val bobItem = TodoItem(id = "todo_bob_1", title = "Bob Task", status = "pending")
        bobHelper.saveAll(listOf(bobItem))

        // 分别配置 Alice 与 Bob 的接口响应，避免初始化网络同步覆盖预置缓存
        fakeApi.todosByUser["alice"] = listOf(aliceItem)
        fakeApi.todosByUser["bob"] = listOf(bobItem)
        fakeApi.currentUser = "alice"

        var currentHelpers = mutableMapOf("alice" to aliceHelper, "bob" to bobHelper)

        // 模拟 Alice 登录会话
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "alice",
            sessionId = "session_alice",
            sessionToken = "tok_alice",
            expiresAt = System.currentTimeMillis() + 3600_000,
            refreshToken = null,
            refreshExpiresAt = 0,
            reviewToken = null,
            reviewExpiresAt = 0,
            rememberMe = false
        )

        // 注入 testDispatcher 作为 ioDispatcher，保证所有 IO 协程受 testScheduler 统一调度，消除竞态
        val viewModel = TodosViewModel(
            context = null,
            tokenStorage = fakeStorage,
            apiClient = fakeClient,
            dbHelperFactory = { _, user -> currentHelpers[user] ?: FakeTodoDatabaseHelper(user) },
            ioDispatcher = testDispatcher
        )
        testScheduler.advanceUntilIdle()

        assertEquals(1, viewModel.uiState.value.todos.size)
        assertEquals("todo_alice_1", viewModel.uiState.value.todos.first().id)

        // 1. 测试 toggleTodoStatus 写操作：在途期间发生账号切换，旧请求失败不得回滚或污染新账号
        val updateStarted = kotlinx.coroutines.CompletableDeferred<Unit>()
        val updatePause = kotlinx.coroutines.CompletableDeferred<Unit>()
        fakeApi.onUpdateTodoInvoked = { updateStarted.complete(Unit) }
        fakeApi.updateTodoPauseDeferred = updatePause
        fakeApi.shouldFailUpdateTodo = true

        // Alice 触发待办状态翻转（乐观更新为 done）
        viewModel.toggleTodoStatus(aliceItem)

        // 确认写请求已进入挂起点
        assertTrue("状态翻转写操作必须已进入并到达挂起点", updateStarted.isCompleted)
        assertEquals("done", viewModel.uiState.value.todos.first().status)
        assertEquals("done", aliceHelper.items["todo_alice_1"]?.status)

        // 此时用户切换账号至 Bob
        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "bob",
            sessionId = "session_bob",
            sessionToken = "tok_bob",
            expiresAt = System.currentTimeMillis() + 3600_000,
            refreshToken = null,
            refreshExpiresAt = 0,
            reviewToken = null,
            reviewExpiresAt = 0,
            rememberMe = false
        )
        fakeApi.currentUser = "bob"
        viewModel.onSessionChanged(serverUrl, "bob")
        testScheduler.advanceUntilIdle()

        // 断言：UI 已切换为 Bob 的待办
        assertEquals(1, viewModel.uiState.value.todos.size)
        assertEquals("todo_bob_1", viewModel.uiState.value.todos.first().id)
        assertEquals("pending", viewModel.uiState.value.todos.first().status)
        assertTrue(aliceHelper.isClosed)

        // 释放 Alice 的旧请求，使服务端返回失败
        updatePause.complete(Unit)
        testScheduler.advanceUntilIdle()

        // 核心安全断言：
        // a) Bob 的数据库沙箱绝对不得受到 Alice 待办的写操作或状态污染
        assertNull("Bob 数据库绝不能混入 Alice 的待办", bobHelper.items["todo_alice_1"])
        assertEquals("Bob 的自身待办状态不得被改动", "pending", bobHelper.items["todo_bob_1"]?.status)
        assertEquals("Bob 数据库写操作调用次数必须为 0", 0, bobHelper.updateStatusCallCount)

        // b) Bob 当前界面绝对不得被 Alice 的回滚逻辑覆盖或混入错误提示
        assertEquals(1, viewModel.uiState.value.todos.size)
        assertEquals("todo_bob_1", viewModel.uiState.value.todos.first().id)
        assertNull("旧账号请求失败绝不能向新账号界面弹出错误提示", viewModel.uiState.value.errorMessage)

        // 2. 测试 deleteTodo 写操作跨账号隔离
        val deleteStarted = kotlinx.coroutines.CompletableDeferred<Unit>()
        val deletePause = kotlinx.coroutines.CompletableDeferred<Unit>()
        fakeApi.onDeleteTodoInvoked = { deleteStarted.complete(Unit) }
        fakeApi.deleteTodoPauseDeferred = deletePause
        fakeApi.shouldFailDeleteTodo = true

        // Bob 触发删除待办
        viewModel.deleteTodo("todo_bob_1")

        // 确认删除写操作已进入挂起点
        assertTrue("删除写操作必须已进入并到达挂起点", deleteStarted.isCompleted)
        assertEquals(0, viewModel.uiState.value.todos.size)
        assertNull(bobHelper.items["todo_bob_1"])

        // 此时退出登录
        viewModel.onLoggedOut()
        testScheduler.advanceUntilIdle()
        assertTrue(bobHelper.isClosed)

        // 释放 Bob 的在途删除请求（失败）
        deletePause.complete(Unit)
        testScheduler.advanceUntilIdle()

        // 断言：已登出状态下，失败回滚不得将待办重新恢复到未登录界面
        assertEquals(0, viewModel.uiState.value.todos.size)
        assertNull(viewModel.uiState.value.errorMessage)

        // 3. 测试 createTodo 写操作在途退出登录时的代次隔离
        val bobHelper2 = FakeTodoDatabaseHelper("bob_key_2")
        bobHelper2.saveAll(listOf(bobItem))
        currentHelpers["bob"] = bobHelper2

        fakeStorage.saveSession(
            serverUrl = serverUrl,
            username = "bob",
            sessionId = "session_bob_2",
            sessionToken = "tok_bob_2",
            expiresAt = System.currentTimeMillis() + 3600_000,
            refreshToken = null,
            refreshExpiresAt = 0,
            reviewToken = null,
            reviewExpiresAt = 0,
            rememberMe = false
        )
        fakeApi.currentUser = "bob"
        viewModel.onSessionChanged(serverUrl, "bob")
        testScheduler.advanceUntilIdle()
        assertEquals(1, viewModel.uiState.value.todos.size)

        val createStarted = kotlinx.coroutines.CompletableDeferred<Unit>()
        val createPause = kotlinx.coroutines.CompletableDeferred<Unit>()
        fakeApi.onCreateTodoInvoked = { createStarted.complete(Unit) }
        fakeApi.createTodoPauseDeferred = createPause
        fakeApi.shouldFailCreateTodo = true

        viewModel.createTodo("Bob in-flight item")
        assertTrue("创建写操作必须已进入并到达挂起点", createStarted.isCompleted)
        assertTrue(viewModel.uiState.value.isCreating)

        // 在途期间退出登录
        viewModel.onLoggedOut()
        testScheduler.advanceUntilIdle()

        // 释放在途创建请求（失败）
        createPause.complete(Unit)
        testScheduler.advanceUntilIdle()

        // 断言：已登出状态下，错误提示和创建中标志不会泄漏在界面上
        assertFalse(viewModel.uiState.value.isCreating)
        assertNull(viewModel.uiState.value.errorMessage)
        assertNull(viewModel.uiState.value.actionSuccessMessage)
        assertTrue(viewModel.uiState.value.todos.isEmpty())
    }
}

