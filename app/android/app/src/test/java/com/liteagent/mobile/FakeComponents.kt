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

    override suspend fun sendChatMessage(request: ChatRequest): Response<ChatResponse> {
        return Response.success(ChatResponse(type = "sync", message = "ok"))
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
