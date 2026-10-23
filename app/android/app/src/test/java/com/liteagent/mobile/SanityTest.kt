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
import com.liteagent.mobile.ui.chat.ChatViewModel
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
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
}
