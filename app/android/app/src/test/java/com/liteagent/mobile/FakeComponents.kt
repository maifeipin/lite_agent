package com.liteagent.mobile

import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ApiService
import com.liteagent.mobile.core.network.SseStreamManager
import com.liteagent.mobile.core.network.models.*
import com.liteagent.mobile.core.security.RecoveryTaskState
import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.CompletableDeferred
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.ResponseBody.Companion.toResponseBody
import retrofit2.Response

class FakeTokenStorage(
    private var activeUrl: String = "http://10.0.2.2:8000"
) : SecureTokenStorage(context = null, cryptoEngine = FakeCryptoEngine()) {

    private val tokens = mutableMapOf<String, String>()
    private val recoveryTasks = mutableMapOf<String, MutableList<RecoveryTaskState>>()

    override suspend fun getActiveServerUrl(): String = activeUrl

    override suspend fun setActiveServerUrl(url: String) {
        activeUrl = url
    }

    override suspend fun getGeneralToken(serverUrl: String?): String {
        val target = serverUrl ?: activeUrl
        return tokens[target] ?: ""
    }

    override suspend fun setGeneralToken(serverUrl: String, token: String) {
        tokens[serverUrl] = token
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

    override suspend fun clearIncompleteTask(serverUrl: String, taskId: String) {
        recoveryTasks[serverUrl]?.removeAll { it.taskId == taskId }
    }

    override suspend fun clearAll() {
        tokens.clear()
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
}

class FakeApiClient(
    val fakeService: FakeApiService,
    storage: SecureTokenStorage
) : ApiClient(storage) {
    override suspend fun getApiService(): ApiService = fakeService
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
