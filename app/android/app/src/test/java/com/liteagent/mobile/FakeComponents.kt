package com.liteagent.mobile

import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ApiService
import com.liteagent.mobile.core.network.SseStreamManager
import com.liteagent.mobile.core.network.models.*
import com.liteagent.mobile.core.security.RecoveryTaskState
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.core.security.UserSession
import com.liteagent.mobile.core.storage.ITodoDatabaseHelper
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.sync.withLock
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.ResponseBody.Companion.toResponseBody
import retrofit2.Response

class FakeTokenStorage(
    private var activeUrl: String = SecureTokenStorage.DEFAULT_SERVER_URL
) : SecureTokenStorage(context = null, cryptoEngine = FakeCryptoEngine()) {

    private val tokens = mutableMapOf<String, String>()
    private val sessions = mutableMapOf<String, UserSession>()
    private val recoveryTasks = mutableMapOf<String, MutableList<RecoveryTaskState>>()

    override suspend fun getActiveServerUrl(): String = activeUrl

    override suspend fun setActiveServerUrl(url: String) {
        activeUrl = url
    }

    override suspend fun getGeneralToken(serverUrl: String?): String {
        val target = serverUrl ?: activeUrl
        return sessions[target]?.sessionToken ?: tokens[target] ?: ""
    }

    override suspend fun setGeneralToken(serverUrl: String, token: String) {
        tokens[serverUrl] = token
    }

    override suspend fun saveSession(session: UserSession) {
        sessionMutex.withLock {
            sessions[session.serverUrl] = session
            tokens[session.serverUrl] = session.sessionToken
        }
    }

    override suspend fun saveSession(
        serverUrl: String,
        username: String,
        sessionId: String,
        sessionToken: String,
        expiresAt: Long,
        refreshToken: String?,
        refreshExpiresAt: Long,
        reviewToken: String?,
        reviewExpiresAt: Long,
        rememberMe: Boolean
    ) {
        sessionMutex.withLock {
            val s = UserSession(
                serverUrl = serverUrl,
                username = username,
                sessionId = sessionId,
                sessionToken = sessionToken,
                expiresAt = expiresAt,
                refreshToken = refreshToken,
                refreshExpiresAt = refreshExpiresAt,
                reviewToken = reviewToken,
                reviewExpiresAt = reviewExpiresAt,
                rememberMe = rememberMe
            )
            sessions[serverUrl] = s
            tokens[serverUrl] = sessionToken
        }
    }

    override suspend fun getSession(serverUrl: String?): UserSession? {
        val target = serverUrl ?: activeUrl
        return sessionMutex.withLock { sessions[target] }
    }

    override suspend fun updateTokens(
        serverUrl: String,
        newSessionToken: String,
        newExpiresAt: Long,
        newRefreshToken: String?,
        newRefreshExpiresAt: Long,
        newReviewToken: String?,
        newReviewExpiresAt: Long,
        matchingSessionId: String?
    ): Boolean {
        if (matchingSessionId.isNullOrBlank()) {
            return false
        }
        val target = serverUrl
        return sessionMutex.withLock {
            val existing = sessions[target] ?: return@withLock false
            if (existing.sessionId.isBlank() || existing.sessionId != matchingSessionId) {
                return@withLock false
            }
            sessions[target] = existing.copy(
                sessionToken = newSessionToken,
                expiresAt = newExpiresAt,
                refreshToken = newRefreshToken ?: existing.refreshToken,
                refreshExpiresAt = if (newRefreshToken != null) newRefreshExpiresAt else existing.refreshExpiresAt,
                reviewToken = newReviewToken ?: existing.reviewToken,
                reviewExpiresAt = if (newReviewToken != null) newReviewExpiresAt else existing.reviewExpiresAt
            )
            tokens[target] = newSessionToken
            true
        }
    }

    override suspend fun clearSession(
        serverUrl: String,
        matchingToken: String?,
        matchingSessionId: String?
    ): Boolean {
        val target = serverUrl
        return sessionMutex.withLock {
            val current = sessions[target]
            if (current == null) {
                return@withLock false
            }
            if (matchingSessionId != null && current.sessionId.isNotBlank() && current.sessionId != matchingSessionId) {
                return@withLock false
            }
            if (matchingToken != null && current.sessionToken.isNotBlank() && current.sessionToken != matchingToken) {
                return@withLock false
            }
            sessions.remove(target)
            tokens.remove(target)
            true
        }
    }

    override suspend fun hasValidSession(serverUrl: String?): Boolean {
        val target = serverUrl ?: activeUrl
        val s = sessions[target] ?: return false
        val now = System.currentTimeMillis() / 1000
        return (s.sessionToken.isNotBlank() && s.expiresAt > now) ||
               (s.refreshToken != null && s.refreshExpiresAt > now)
    }

    override suspend fun getIncompleteTasks(serverUrl: String): List<RecoveryTaskState> {
        return (recoveryTasks[serverUrl] ?: emptyList()).toList()
    }

    override suspend fun saveIncompleteTask(
        serverUrl: String,
        taskId: String,
        sessionId: String,
        logCount: Int
    ) {
        val list = recoveryTasks.getOrPut(serverUrl) { mutableListOf() }
        list.removeAll { it.taskId == taskId }
        list.add(0, RecoveryTaskState(taskId, sessionId, logCount, System.currentTimeMillis()))
    }

    override suspend fun removeIncompleteTask(serverUrl: String, taskId: String) {
        recoveryTasks[serverUrl]?.removeAll { it.taskId == taskId }
    }

    override suspend fun clearIncompleteTask(serverUrl: String, taskId: String) {
        removeIncompleteTask(serverUrl, taskId)
    }

    override suspend fun clearAll() {
        tokens.clear()
        sessions.clear()
        recoveryTasks.clear()
    }
}

class FakeApiService : ApiService {
    var shouldFailSessionMessages = false
    var failureCode = 500
    var sessionMessagesResponse: List<MessageItem> = emptyList()
    var sessionsResponse: List<SessionItem> = emptyList()
    var sessionMessagesPauseDeferred: CompletableDeferred<Unit>? = null
    var onSessionMessagesInvoked: (() -> Unit)? = null

    var shouldFailLogin = false
    var loginFailureCode = 401
    var loginFailureMessage = "账号或密码错误"

    var shouldFailRefresh = false
    var refreshFailureCode = 401
    var onRefreshInvoked: (() -> Unit)? = null
    var refreshPauseDeferred: CompletableDeferred<Unit>? = null

    override suspend fun login(request: LoginRequest): Response<LoginResponse> {
        if (shouldFailLogin) {
            return Response.error(
                loginFailureCode,
                """{"error":"$loginFailureMessage"}""".toResponseBody("application/json".toMediaTypeOrNull())
            )
        }
        return Response.success(
            LoginResponse(
                success = true,
                sessionToken = "fake_session_token",
                expiresAt = System.currentTimeMillis() / 1000 + 3600,
                username = request.username,
                sessionId = "fake_session_id",
                scopes = listOf("user:general"),
                refreshToken = if (request.rememberMe) "fake_refresh_token" else null,
                refreshExpiresAt = if (request.rememberMe) System.currentTimeMillis() / 1000 + 30 * 86400 else null
            )
        )
    }

    override suspend fun refresh(request: RefreshRequest): Response<RefreshResponse> {
        onRefreshInvoked?.invoke()
        refreshPauseDeferred?.await()
        if (shouldFailRefresh) {
            return Response.error(
                refreshFailureCode,
                """{"error":"refresh failed"}""".toResponseBody("application/json".toMediaTypeOrNull())
            )
        }
        return Response.success(
            RefreshResponse(
                success = true,
                sessionToken = "refreshed_session_token",
                expiresAt = System.currentTimeMillis() / 1000 + 3600,
                refreshToken = "new_rotated_refresh_token",
                refreshExpiresAt = System.currentTimeMillis() / 1000 + 30 * 86400
            )
        )
    }

    override suspend fun logout(request: LogoutRequest): Response<LogoutResponse> {
        return Response.success(LogoutResponse(success = true, message = "已安全登出"))
    }

    override suspend fun getSessions(): Response<SessionsListResponse> {
        return Response.success(SessionsListResponse(sessions = sessionsResponse))
    }

    override suspend fun getSessionMessages(sessionKey: String, limit: Int): Response<MessagesListResponse> {
        onSessionMessagesInvoked?.invoke()
        sessionMessagesPauseDeferred?.await()
        return if (shouldFailSessionMessages) {
            Response.error(
                failureCode,
                """{"error":"failed"}""".toResponseBody("application/json".toMediaTypeOrNull())
            )
        } else {
            Response.success(MessagesListResponse(messages = sessionMessagesResponse))
        }
    }

    override suspend fun renameSession(request: RenameSessionRequest): Response<RenameSessionResponse> {
        sessionsResponse = sessionsResponse.map {
            if (it.sessionKey == request.sessionKey) it.copy(title = request.title) else it
        }
        return Response.success(RenameSessionResponse(success = true, sessionKey = request.sessionKey, title = request.title))
    }

    override suspend fun sendChatMessage(request: ChatRequest): Response<ChatResponse> {
        return Response.success(ChatResponse(type = "sync", message = "ok"))
    }

    var ocrRecognizedText: String = "模拟OCR识别文字结果"
    override suspend fun uploadOcrImage(file: MultipartBody.Part): Response<OcrResponse> {
        return Response.success(OcrResponse(success = true, text = ocrRecognizedText))
    }

    var dashboardCommandsResponse: List<DashboardCommandItem> = emptyList()
    override suspend fun getDashboardCommands(): Response<List<DashboardCommandItem>> {
        return Response.success(dashboardCommandsResponse)
    }

    override suspend fun testConnection(): Response<Map<String, Any>> {
        return Response.success(mapOf("status" to "ok"))
    }

    // --- 待办事项 (Todos) Mock ---
    var todosResponse: List<TodoItem> = emptyList()
    var todosByUser: MutableMap<String, List<TodoItem>> = mutableMapOf()
    var todosResponseProvider: (() -> List<TodoItem>)? = null
    var currentUser: String = "alice"
    var storage: SecureTokenStorage? = null
    var shouldFailTodos = false
    var todosFailureCode = 500

    var shouldFailCreateTodo = false
    var createTodoFailureCode = 500
    var createTodoPauseDeferred: CompletableDeferred<Unit>? = null
    var onCreateTodoInvoked: (() -> Unit)? = null

    var shouldFailUpdateTodo = false
    var updateTodoFailureCode = 500
    var updateTodoPauseDeferred: CompletableDeferred<Unit>? = null
    var onUpdateTodoInvoked: (() -> Unit)? = null

    var shouldFailDeleteTodo = false
    var deleteTodoFailureCode = 500
    var deleteTodoPauseDeferred: CompletableDeferred<Unit>? = null
    var onDeleteTodoInvoked: (() -> Unit)? = null

    override suspend fun getTodos(status: String): Response<TodosResponse> {
        if (shouldFailTodos) {
            return Response.error(todosFailureCode, """{"error":"failed"}""".toResponseBody("application/json".toMediaTypeOrNull()))
        }
        val currentUsername = storage?.getSession()?.username ?: currentUser
        val data = todosResponseProvider?.invoke()
            ?: todosByUser[currentUsername]
            ?: todosResponse
        return Response.success(TodosResponse(success = true, data = data))
    }

    override suspend fun createTodo(request: CreateTodoRequest): Response<CreateTodoResponse> {
        onCreateTodoInvoked?.invoke()
        createTodoPauseDeferred?.await()
        if (shouldFailCreateTodo) {
            return Response.error(createTodoFailureCode, """{"error":"create failed"}""".toResponseBody("application/json".toMediaTypeOrNull()))
        }
        return Response.success(CreateTodoResponse(success = true, message = "created"))
    }

    override suspend fun updateTodo(id: String, request: UpdateTodoRequest): Response<UpdateTodoResponse> {
        onUpdateTodoInvoked?.invoke()
        updateTodoPauseDeferred?.await()
        if (shouldFailUpdateTodo) {
            return Response.error(updateTodoFailureCode, """{"error":"update failed"}""".toResponseBody("application/json".toMediaTypeOrNull()))
        }
        return Response.success(UpdateTodoResponse(success = true, message = "updated"))
    }

    override suspend fun deleteTodo(id: String): Response<Map<String, Any>> {
        onDeleteTodoInvoked?.invoke()
        deleteTodoPauseDeferred?.await()
        if (shouldFailDeleteTodo) {
            return Response.error(deleteTodoFailureCode, """{"error":"delete failed"}""".toResponseBody("application/json".toMediaTypeOrNull()))
        }
        return Response.success(mapOf("success" to true))
    }

    // --- 会审决策中枢 (Review Meetings) Mock ---
    var meetingsResponse: List<MeetingItem> = emptyList()
    var meetingDetailMap = mutableMapOf<String, MeetingDetail>()
    var shouldFailDecideConflict = false
    var shouldFailDecideTimeout = false
    var simulateServerCommittedOnTimeout = false
    var simulateServerOtherCommittedOnTimeout = false
    var decideTimeoutFailureCode = 504
    var lastDecideRequest: DecideMeetingRequest? = null

    override suspend fun getReviewMeetings(): Response<ReviewMeetingsResponse> {
        return Response.success(ReviewMeetingsResponse(meetings = meetingsResponse))
    }

    override suspend fun getReviewMeetingDetail(id: String, since: Int): Response<MeetingDetail> {
        val detail = meetingDetailMap[id] ?: MeetingDetail(id = id, title = "Mock Meeting", state = "open")
        return Response.success(detail)
    }

    override suspend fun decideReviewMeeting(id: String, request: DecideMeetingRequest): Response<MeetingDetail> {
        lastDecideRequest = request
        if (shouldFailDecideConflict) {
            return Response.error(409, """{"error":"版本冲突：议题已被更新"}""".toResponseBody("application/json".toMediaTypeOrNull()))
        }
        if (shouldFailDecideTimeout) {
            val existing = meetingDetailMap[id] ?: MeetingDetail(id = id, title = "Mock Meeting", state = "open")
            if (simulateServerCommittedOnTimeout) {
                val newState = when (request.decision) {
                    "approve" -> "approved"
                    "revise" -> "changes_requested"
                    "reject" -> "rejected"
                    else -> existing.state
                }
                meetingDetailMap[id] = existing.copy(
                    state = newState,
                    lastSeqRaw = existing.lastSeq + 1,
                    events = existing.events + MeetingEvent(
                        seq = existing.lastSeq + 1,
                        round = existing.round,
                        actor = "human",
                        kind = "decision",
                        body = mapOf("decision" to request.decision, "request_id" to request.requestId)
                    )
                )
            } else if (simulateServerOtherCommittedOnTimeout) {
                meetingDetailMap[id] = existing.copy(
                    state = "rejected",
                    lastSeqRaw = existing.lastSeq + 1,
                    events = existing.events + MeetingEvent(
                        seq = existing.lastSeq + 1,
                        round = existing.round,
                        actor = "human",
                        kind = "decision",
                        body = mapOf("decision" to "reject", "request_id" to "bob-concurrent-uuid-999")
                    )
                )
            }
            return Response.error(decideTimeoutFailureCode, """{"error":"Gateway Timeout"}""".toResponseBody("application/json".toMediaTypeOrNull()))
        }
        val existing = meetingDetailMap[id] ?: MeetingDetail(id = id, title = "Mock Meeting", state = "open")
        val newState = when (request.decision) {
            "approve" -> "approved"
            "revise" -> "changes_requested"
            "reject" -> "rejected"
            else -> existing.state
        }
        val updated = existing.copy(state = newState, lastSeqRaw = existing.lastSeq + 1)
        meetingDetailMap[id] = updated
        return Response.success(updated)
    }

    override suspend fun createReviewMeeting(request: CreateMeetingRequest): Response<CreateMeetingResponse> {
        val newId = "meeting-${System.currentTimeMillis()}"
        meetingDetailMap[newId] = MeetingDetail(id = newId, title = request.title, brief = request.brief, state = "open")
        return Response.success(CreateMeetingResponse(id = newId, ownerToken = "mock-owner-token", inviteUrl = "http://mock/join"))
    }

    // --- Phone App 真机自动化执行中枢 Mock ---
    var phoneDevicesResponse: List<PhoneDeviceItem> = emptyList()
    var phoneAppsResponse: List<PhoneCatalogAppItem> = emptyList()
    var phoneBatchesMap = mutableMapOf<String, PhoneBatchItem>()

    override suspend fun getPhoneDevices(): Response<PhoneDevicesResponse> {
        return Response.success(PhoneDevicesResponse(success = true, devices = phoneDevicesResponse))
    }

    override suspend fun getPhoneApps(): Response<PhoneAppsResponse> {
        return Response.success(PhoneAppsResponse(success = true, apps = phoneAppsResponse))
    }

    override suspend fun getPhoneBatches(): Response<PhoneBatchesResponse> {
        return Response.success(PhoneBatchesResponse(success = true, batches = phoneBatchesMap.values.toList()))
    }

    override suspend fun getPhoneBatchDetail(batchId: String): Response<PhoneBatchDetailResponse> {
        val batch = phoneBatchesMap[batchId] ?: PhoneBatchItem(id = batchId, appName = "闲鱼", status = "waiting_approval")
        return Response.success(PhoneBatchDetailResponse(success = true, batch = batch))
    }

    override suspend fun createPhoneBatch(request: CreatePhoneBatchRequest): Response<CreatePhoneBatchResponse> {
        val newId = "batch_${System.currentTimeMillis()}"
        val batch = PhoneBatchItem(
            id = newId,
            deviceId = request.deviceId,
            appId = request.appId,
            appName = request.appName.ifBlank { request.appId },
            query = request.query,
            status = "waiting_approval",
            scriptPreview = "adb shell input tap 500 500 # search ${request.query}"
        )
        phoneBatchesMap[newId] = batch
        return Response.success(CreatePhoneBatchResponse(success = true, batchId = newId, batch = batch, message = "created"))
    }

    override suspend fun approvePhoneBatch(batchId: String, request: Map<String, String>): Response<PhoneActionResponse> {
        val existing = phoneBatchesMap[batchId] ?: PhoneBatchItem(id = batchId, status = "waiting_approval")
        val updated = existing.copy(status = "approved")
        phoneBatchesMap[batchId] = updated
        return Response.success(PhoneActionResponse(success = true, message = "已批准脚本", batch = updated))
    }

    override suspend fun executePhoneBatch(batchId: String, request: Map<String, String>): Response<PhoneActionResponse> {
        val existing = phoneBatchesMap[batchId] ?: PhoneBatchItem(id = batchId, status = "approved")
        val updated = existing.copy(
            status = "completed",
            counts = PhoneBatchCounts(total = 2, qualified = 1, pendingReview = 1, disqualified = 0),
            items = listOf(
                PhoneItemEvaluation(id = "item_1", title = "MacBook M3 Max 64G", priceText = "¥13999", state = "qualified", reason = "成色极佳且价格符合预期"),
                PhoneItemEvaluation(id = "item_2", title = "MacBook M3 32G", priceText = "¥9999", state = "pending_review", reason = "未注明电池循环次数")
            )
        )
        phoneBatchesMap[batchId] = updated
        return Response.success(PhoneActionResponse(success = true, message = "执行完毕", batch = updated))
    }

    override suspend fun rejectPhoneBatch(batchId: String, request: PhoneActionRequest): Response<PhoneActionResponse> {
        val existing = phoneBatchesMap[batchId] ?: PhoneBatchItem(id = batchId, status = "waiting_approval")
        val updated = existing.copy(status = "rejected")
        phoneBatchesMap[batchId] = updated
        return Response.success(PhoneActionResponse(success = true, message = "已驳回", batch = updated))
    }

    override suspend fun cancelPhoneBatch(batchId: String, request: PhoneActionRequest): Response<PhoneActionResponse> {
        val existing = phoneBatchesMap[batchId] ?: PhoneBatchItem(id = batchId, status = "running")
        val updated = existing.copy(status = "cancelled")
        phoneBatchesMap[batchId] = updated
        return Response.success(PhoneActionResponse(success = true, message = "已终止", batch = updated))
    }

    override suspend fun resumePhoneDevice(request: PhoneActionRequest): Response<PhoneActionResponse> {
        return Response.success(PhoneActionResponse(success = true, message = "设备已恢复"))
    }

    // --- 发现中枢：邮件、账单、RSS 简报、TaskSpecs、Socks5 Mock ---
    override suspend fun getRecentEmails(limit: Int): Response<EmailListResponse> {
        return Response.success(EmailListResponse(success = true, emails = emptyList()))
    }

    override suspend fun getBillingSummary(): Response<BillingSummaryResponse> {
        return Response.success(BillingSummaryResponse(success = true, totalStatements = 0, unpaidCount = 0, totalUnpaidCny = 0.0))
    }

    var rssBriefResponse: RssBriefResponse = RssBriefResponse(
        success = true,
        date = "2026-10-25",
        summary = "今日全球科技与开源社区聚焦端侧大模型推理优化。",
        topics = listOf(
            RssBriefTopicItem(title = "Android 端侧 Agent 架构演进", sentiment = "positive", analysis = "本地与云端协同工作流大幅降低延迟", sourceCount = 5)
        )
    )
    override suspend fun getRssBrief(): Response<RssBriefResponse> {
        return Response.success(rssBriefResponse)
    }

    var taskSpecsResponse: List<TaskSpecItem> = listOf(
        TaskSpecItem(id = "spec_daily_digest", name = "每日情报汇总", description = "自动拉取 RSS 与邮件生成简报", enabled = true)
    )
    override suspend fun getTaskSpecs(): Response<TaskSpecsListResponse> {
        return Response.success(TaskSpecsListResponse(success = true, specs = taskSpecsResponse))
    }

    override suspend fun runTaskSpec(id: String, request: Map<String, String>): Response<TaskSpecRunResponse> {
        return Response.success(TaskSpecRunResponse(success = true, taskId = "task_run_$id", message = "任务已启动"))
    }

    var socks5NodesResponse: List<Socks5NodeItem> = listOf(
        Socks5NodeItem(id = 1, name = "HK-Node-01", host = "127.0.0.1", port = 1080, isActive = true, lastStatus = "ok", lastLatencyMs = 32.5)
    )
    override suspend fun getSocks5Nodes(): Response<Socks5ListResponse> {
        return Response.success(Socks5ListResponse(success = true, nodes = socks5NodesResponse))
    }

    override suspend fun testSocks5Node(id: Int, host: String?): Response<Socks5TestResponse> {
        return Response.success(Socks5TestResponse(success = true, status = "ok", latencyMs = 28.0, message = "OK"))
    }

    override suspend fun checkSocks5Outbound(id: Int): Response<Socks5TestResponse> {
        return Response.success(Socks5TestResponse(success = true, status = "ok", latencyMs = 35.0, message = "Outbound OK"))
    }

    override suspend fun setActiveSocks5Node(request: Socks5SetActiveRequest): Response<Socks5SetActiveResponse> {
        socks5NodesResponse = socks5NodesResponse.map { it.copy(isActive = it.id == request.id) }
        return Response.success(Socks5SetActiveResponse(success = true, activeId = request.id, message = "已切换"))
    }
}

class FakeApiClient(
    val fakeService: FakeApiService,
    storage: SecureTokenStorage
) : ApiClient(storage) {
    init {
        fakeService.storage = storage
    }
    override suspend fun getApiService(serverUrl: String?): ApiService = fakeService
    override fun getAuthApiService(serverUrl: String): ApiService = fakeService
}

class FakeSseStreamManager(
    storage: SecureTokenStorage
) : SseStreamManager(storage) {
    var isStreamStarted = false
    var lastStartedTaskId: String? = null
    var isCancelled = false

    override suspend fun startStream(
        serverUrl: String,
        taskId: String,
        sessionId: String,
        onEvent: (TaskStreamData) -> Unit,
        onTerminal: (status: String, finalData: TaskStreamData) -> Unit,
        onPrematureClosed: (lastStatus: String?) -> Unit,
        onError: (Throwable?, Int?) -> Unit
    ) {
        isStreamStarted = true
        lastStartedTaskId = taskId
    }

    override fun cancel() {
        isCancelled = true
        isStreamStarted = false
    }
}

class FakeTodoDatabaseHelper(override val dbKey: String) : ITodoDatabaseHelper {
    val items = mutableMapOf<String, TodoItem>()
    var isClosed = false
    var updateStatusCallCount = 0
    var deleteTodoCallCount = 0
    var insertOrUpdateCallCount = 0

    override fun getAll(): List<TodoItem> = items.values.toList()

    override fun saveAll(newItems: List<TodoItem>) {
        items.clear()
        newItems.forEach { items[it.id] = it }
    }

    override fun insertOrUpdate(item: TodoItem) {
        insertOrUpdateCallCount++
        items[item.id] = item
    }

    override fun updateStatus(id: String, newStatus: String) {
        updateStatusCallCount++
        items[id]?.let {
            items[id] = it.copy(status = newStatus)
        }
    }

    override fun deleteTodo(id: String) {
        deleteTodoCallCount++
        items.remove(id)
    }

    override fun clearAll() {
        items.clear()
    }

    override fun close() {
        isClosed = true
    }
}
