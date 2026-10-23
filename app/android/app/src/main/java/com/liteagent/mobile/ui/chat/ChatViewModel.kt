package com.liteagent.mobile.ui.chat

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.network.SseStreamManager
import com.liteagent.mobile.core.network.models.*
import com.liteagent.mobile.core.security.RecoveryTaskState
import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.UUID

class ChatViewModel(
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient,
    private val sseStreamManager: SseStreamManager
) : ViewModel() {

    private val _sessions = MutableStateFlow<List<SessionItem>>(emptyList())
    val sessions: StateFlow<List<SessionItem>> = _sessions.asStateFlow()

    private val _currentSession = MutableStateFlow<SessionItem?>(null)
    val currentSession: StateFlow<SessionItem?> = _currentSession.asStateFlow()

    private val _messages = MutableStateFlow<List<MessageItem>>(emptyList())
    val messages: StateFlow<List<MessageItem>> = _messages.asStateFlow()

    private val _isStreaming = MutableStateFlow(false)
    val isStreaming: StateFlow<Boolean> = _isStreaming.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _taskStatus = MutableStateFlow<String?>(null)
    val taskStatus: StateFlow<String?> = _taskStatus.asStateFlow()

    private val _taskLogs = MutableStateFlow<List<String>>(emptyList())
    val taskLogs: StateFlow<List<String>> = _taskLogs.asStateFlow()

    private val _errorMessage = MutableStateFlow<String?>(null)
    val errorMessage: StateFlow<String?> = _errorMessage.asStateFlow()

    private val _pendingRecoveryTasks = MutableStateFlow<List<RecoveryTaskState>>(emptyList())
    val pendingRecoveryTasks: StateFlow<List<RecoveryTaskState>> = _pendingRecoveryTasks.asStateFlow()

    // 竞态保护与服务器/会话严格绑定标识
    private var boundServerUrl: String = ""
    private var boundSessionKey: String = ""
    private var boundTaskId: String? = null

    private var activeLoadJob: Job? = null
    private var activeSendJob: Job? = null
    private var activeSseJob: Job? = null

    init {
        viewModelScope.launch {
            boundServerUrl = ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl())
            refreshRecoveryTasks()
            loadSessions()
        }
    }

    suspend fun refreshRecoveryTasks() {
        val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
        val list = tokenStorage.getIncompleteTasks(server)
        _pendingRecoveryTasks.value = list
    }

    fun onServerSwitched(newServerUrl: String) {
        val cleanUrl = ServerUrlNormalizer.normalize(newServerUrl)
        if (cleanUrl == boundServerUrl) return

        // 切换服务器必须取消所有旧连接、重置内存状态，杜绝向新服务器带入旧凭证或消息
        activeLoadJob?.cancel()
        activeSendJob?.cancel()
        activeSseJob?.cancel()
        sseStreamManager.cancel()

        boundServerUrl = cleanUrl
        boundSessionKey = ""
        boundTaskId = null

        _sessions.value = emptyList()
        _currentSession.value = null
        _messages.value = emptyList()
        _taskLogs.value = emptyList()
        _taskStatus.value = null
        _isStreaming.value = false
        _errorMessage.value = null

        viewModelScope.launch {
            refreshRecoveryTasks()
            loadSessions()
        }
    }

    fun loadSessions(): Job {
        activeLoadJob?.cancel()
        val job = viewModelScope.launch {
            _isLoading.value = true
            val currentServer = ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl())
            boundServerUrl = currentServer

            try {
                val service = apiClient.getApiService()
                val response = service.getSessions()
                if (currentServer != boundServerUrl) return@launch // 丢弃过期服务器响应

                if (response.isSuccessful) {
                    // 仅收纳通道为 API 的会话，规避与企微/飞书等其他外部通道混淆
                    val allList = response.body()?.sessions ?: emptyList()
                    val apiSessions = allList.filter { it.sessionKey.startsWith("api:") }
                    _sessions.value = apiSessions

                    if (_currentSession.value == null && apiSessions.isNotEmpty()) {
                        selectSession(apiSessions.first())
                    } else if (apiSessions.isEmpty()) {
                        createNewSession()
                    }
                } else if (response.code() == 401) {
                    _errorMessage.value = "未授权 (401)：密钥错误或已轮转，请重新配置"
                } else {
                    _errorMessage.value = "获取会话失败 (HTTP ${response.code()})"
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (currentServer == boundServerUrl) {
                    _errorMessage.value = "连接异常: ${e.localizedMessage ?: e.message}"
                }
            } finally {
                if (currentServer == boundServerUrl) {
                    _isLoading.value = false
                }
            }
        }
        activeLoadJob = job
        return job
    }

    fun selectSession(session: SessionItem) {
        if (session.sessionKey == boundSessionKey && _messages.value.isNotEmpty()) return

        // 切换会话时，中断旧会话的历史加载与可能存在的流监听，重置消息队列，防止串线覆盖
        activeLoadJob?.cancel()
        activeSendJob?.cancel()
        activeSseJob?.cancel()
        sseStreamManager.cancel()

        boundSessionKey = session.sessionKey
        boundTaskId = null
        _currentSession.value = session
        _messages.value = emptyList()
        _taskLogs.value = emptyList()
        _taskStatus.value = null
        _isStreaming.value = false

        loadMessages(session.sessionKey)
    }

    fun createNewSession() {
        activeLoadJob?.cancel()
        activeSendJob?.cancel()
        activeSseJob?.cancel()
        sseStreamManager.cancel()

        val newId = UUID.randomUUID().toString().substring(0, 8)
        val newSessionKey = "api:$newId"
        boundSessionKey = newSessionKey
        boundTaskId = null

        val newSession = SessionItem(
            sessionKey = newSessionKey,
            title = "新对话 #$newId",
            status = "chatting"
        )
        _sessions.value = listOf(newSession) + _sessions.value.filter { it.sessionKey != newSessionKey }
        _currentSession.value = newSession
        _messages.value = emptyList()
        _taskLogs.value = emptyList()
        _taskStatus.value = null
        _isStreaming.value = false
    }

    fun loadMessages(sessionKey: String): Job {
        val requestServer = boundServerUrl
        activeLoadJob?.cancel()
        val job = viewModelScope.launch {
            _isLoading.value = true
            try {
                loadMessagesInternal(sessionKey, requestServer)
            } finally {
                if (boundSessionKey == sessionKey && boundServerUrl == requestServer) {
                    _isLoading.value = false
                }
            }
        }
        activeLoadJob = job
        return job
    }

    private suspend fun loadMessagesInternal(sessionKey: String, serverUrl: String): Boolean {
        return try {
            val service = apiClient.getApiService()
            val response = service.getSessionMessages(sessionKey = sessionKey, limit = 50)

            // 竞态校验：仅当返回的结果仍属于当前所选会话和服务器时才写入
            if (boundSessionKey == sessionKey && boundServerUrl == serverUrl) {
                if (response.isSuccessful) {
                    _messages.value = response.body()?.messages ?: emptyList()
                    true
                } else {
                    if (response.code() == 401) {
                        _errorMessage.value = "凭证过期或无效 (401)"
                    } else {
                        _errorMessage.value = "加载历史消息失败 (HTTP ${response.code()})"
                    }
                    false
                }
            } else {
                false
            }
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            if (boundSessionKey == sessionKey && boundServerUrl == serverUrl) {
                _errorMessage.value = "加载历史消息失败: ${e.localizedMessage ?: e.message}"
            }
            false
        }
    }

    fun sendMessage(text: String): Job? {
        val trimmed = text.trim()
        if (trimmed.isEmpty() || _isStreaming.value) return null

        val activeSession = _currentSession.value ?: run {
            createNewSession()
            _currentSession.value!!
        }

        val targetSessionKey = activeSession.sessionKey
        val targetServerUrl = boundServerUrl
        boundSessionKey = targetSessionKey

        val userMsg = MessageItem(
            role = "user",
            content = trimmed,
            time = (System.currentTimeMillis() / 1000).toDouble()
        )
        _messages.value = _messages.value + userMsg

        _isStreaming.value = true
        _taskStatus.value = "starting"
        _taskLogs.value = emptyList()

        activeSendJob?.cancel()
        val job = viewModelScope.launch {
            try {
                val service = apiClient.getApiService()
                val cleanSessionId = targetSessionKey.removePrefix("api:")
                val request = ChatRequest(
                    sessionId = cleanSessionId,
                    text = trimmed,
                    notifyChannels = listOf("api")
                )

                val response = service.sendChatMessage(request)
                if (targetSessionKey != boundSessionKey || targetServerUrl != boundServerUrl) return@launch

                if (response.isSuccessful) {
                    val body = response.body()
                    if (body != null) {
                        if (body.type == "async" && !body.taskId.isNullOrBlank()) {
                            val taskId = body.taskId
                            boundTaskId = taskId
                            _taskStatus.value = "running"

                            // 按任务 ID 精确持久化，绝不盲目覆盖其他未完成任务
                            tokenStorage.saveIncompleteTask(targetServerUrl, taskId, cleanSessionId, 0)
                            refreshRecoveryTasks()

                            connectSseStream(taskId, cleanSessionId, targetSessionKey, targetServerUrl)
                        } else {
                            // 同步完成：仅关闭当前流，绝不清除其他长任务的恢复记录！
                            _isStreaming.value = false
                            _taskStatus.value = "completed"
                            val reply = body.response ?: body.message ?: ""
                            appendAssistantReply(reply, targetSessionKey, targetServerUrl)
                        }
                    }
                } else if (response.code() == 401) {
                    _isStreaming.value = false
                    _taskStatus.value = "failed"
                    _errorMessage.value = "发送失败 (401)：密钥错误或已失效"
                } else {
                    _isStreaming.value = false
                    _taskStatus.value = "failed"
                    _errorMessage.value = "服务端返回错误 (HTTP ${response.code()})"
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (targetSessionKey == boundSessionKey && targetServerUrl == boundServerUrl) {
                    _isStreaming.value = false
                    _taskStatus.value = "failed"
                    _errorMessage.value = "网络异常: ${e.localizedMessage ?: e.message}"
                }
            }
        }
        activeSendJob = job
        return job
    }

    private fun connectSseStream(
        taskId: String,
        cleanSessionId: String,
        sessionKey: String,
        serverUrl: String
    ) {
        activeSseJob?.cancel()
        activeSseJob = viewModelScope.launch {
            try {
                sseStreamManager.startStream(
                    serverUrl = serverUrl,
                    taskId = taskId,
                    sessionId = cleanSessionId,
                    onEvent = { streamData ->
                        if (boundSessionKey != sessionKey || boundServerUrl != serverUrl || boundTaskId != taskId) return@startStream

                        val rawStatus = streamData.status ?: "running"
                        _taskStatus.value = rawStatus

                        // 优先从 progress["logs"] 完整快照同步，保留原始顺序与合法重复日志（如重试提示）
                        val progressLogs = (streamData.progress?.get("logs") as? List<*>)?.mapNotNull { it?.toString() }
                        if (progressLogs != null && progressLogs.isNotEmpty()) {
                            _taskLogs.value = progressLogs
                            viewModelScope.launch {
                                tokenStorage.saveIncompleteTask(serverUrl, taskId, cleanSessionId, progressLogs.size)
                                refreshRecoveryTasks()
                            }
                        } else {
                            // 无完整快照时按增量安全追加（不按文本 contains 去重，保留位置与合法重复）
                            val newLogs = streamData.logs?.map { it.toString() } ?: emptyList()
                            if (newLogs.isNotEmpty()) {
                                val updated = _taskLogs.value + newLogs
                                _taskLogs.value = updated
                                viewModelScope.launch {
                                    tokenStorage.saveIncompleteTask(serverUrl, taskId, cleanSessionId, updated.size)
                                    refreshRecoveryTasks()
                                }
                            }
                        }

                        if (!streamData.response.isNullOrBlank()) {
                            appendAssistantReply(streamData.response, sessionKey, serverUrl)
                        }
                    },
                    onTerminal = { terminalStatus, finalData ->
                        if (boundSessionKey != sessionKey || boundServerUrl != serverUrl || boundTaskId != taskId) return@startStream
                        _isStreaming.value = false
                        _taskStatus.value = terminalStatus
                        boundTaskId = null

                        // 仅按本任务 ID 精确清除，绝不波及其他任务！
                        viewModelScope.launch {
                            tokenStorage.clearIncompleteTask(serverUrl, taskId)
                            refreshRecoveryTasks()
                        }
                        if (!finalData.response.isNullOrBlank()) {
                            appendAssistantReply(finalData.response, sessionKey, serverUrl)
                        }
                    },
                    onPrematureClosed = { _ ->
                        // 非终态断开（如 300s 超时或网络断开），绝不误判为 completed，进入待恢复状态
                        if (boundSessionKey != sessionKey || boundServerUrl != serverUrl || boundTaskId != taskId) return@startStream
                        _isStreaming.value = false
                        _taskStatus.value = "pending_recovery"
                        _errorMessage.value = "任务连接暂时中断（超时或切后台），可点击恢复重连"
                    },
                    onError = { _, code ->
                        if (boundSessionKey != sessionKey || boundServerUrl != serverUrl || boundTaskId != taskId) return@startStream
                        _isStreaming.value = false
                        _taskStatus.value = if (code == 401) "unauthorized" else "pending_recovery"
                        if (code == 401) {
                            _errorMessage.value = "SSE 连接鉴权失败 (401)"
                        } else {
                            _errorMessage.value = "网络中断，任务未完成，可点击重连"
                        }
                    }
                )
            } catch (t: Throwable) {
                if (t is CancellationException) throw t
                // 捕获解密异常或网络建连抛出的未受检异常，进入待恢复状态，杜绝崩溃并反馈界面
                if (boundSessionKey == sessionKey && boundServerUrl == serverUrl && boundTaskId == taskId) {
                    _isStreaming.value = false
                    _taskStatus.value = "pending_recovery"
                    _errorMessage.value = "SSE 启动异常: ${t.localizedMessage ?: t.message}"
                }
            }
        }
    }

    /**
     * 恢复未完成长任务：彻底切换到目标会话上下文，先挂起等待历史加载完毕，再订阅 SSE，防止回复被历史快照覆盖
     */
    fun resumeRecoveryTask(taskState: RecoveryTaskState? = null): Job? {
        val target = taskState ?: _pendingRecoveryTasks.value.firstOrNull() ?: return null
        val currentServer = boundServerUrl
        val targetSessionKey = "api:${target.sessionId}"

        // 1. 中断当前所有活动请求与流监听
        activeLoadJob?.cancel()
        activeSendJob?.cancel()
        activeSseJob?.cancel()
        sseStreamManager.cancel()

        // 2. 彻底切换会话上下文，重置绑定标识与列表
        boundSessionKey = targetSessionKey
        boundTaskId = target.taskId
        _messages.value = emptyList()
        _taskLogs.value = emptyList()
        _isStreaming.value = true
        _taskStatus.value = "reconnecting"

        // 3. 选中或创建合成会话
        val existingSession = _sessions.value.find { it.sessionKey == targetSessionKey }
        if (existingSession != null) {
            _currentSession.value = existingSession
        } else {
            val synthetic = SessionItem(
                sessionKey = targetSessionKey,
                title = "恢复会话 #${target.sessionId}",
                status = "running"
            )
            _sessions.value = listOf(synthetic) + _sessions.value
            _currentSession.value = synthetic
        }

        // 4. 关键修复：串行化等待历史加载完成后，再启动 SSE 监听，防止最终回复被旧历史覆盖
        val job = viewModelScope.launch {
            _isLoading.value = true
            try {
                val loadSuccess = loadMessagesInternal(targetSessionKey, currentServer)

                // 历史加载失败：立即退出生成中状态，保留恢复记录，允许用户点击重试
                if (!loadSuccess) {
                    if (boundSessionKey == targetSessionKey && boundServerUrl == currentServer) {
                        _isStreaming.value = false
                        _taskStatus.value = "pending_recovery"
                        if (_errorMessage.value.isNullOrBlank()) {
                            _errorMessage.value = "历史消息加载失败，未恢复任务连接，可点击重试"
                        }
                    }
                    return@launch
                }

                // 校验挂起等待期间用户未再次切出会话或切换服务器
                if (boundSessionKey != targetSessionKey || boundServerUrl != currentServer || boundTaskId != target.taskId) {
                    return@launch
                }

                // 5. 历史加载确认完成后，启动 SSE 增量监听
                connectSseStream(target.taskId, target.sessionId, targetSessionKey, currentServer)
            } catch (t: Throwable) {
                if (t is CancellationException) throw t
                if (boundSessionKey == targetSessionKey && boundServerUrl == currentServer) {
                    _isStreaming.value = false
                    _taskStatus.value = "pending_recovery"
                    _errorMessage.value = "恢复任务异常: ${t.localizedMessage ?: t.message}"
                }
            } finally {
                if (boundSessionKey == targetSessionKey && boundServerUrl == currentServer) {
                    _isLoading.value = false
                }
            }
        }
        activeLoadJob = job
        return job
    }

    private fun appendAssistantReply(replyText: String, sessionKey: String, serverUrl: String) {
        if (replyText.isBlank()) return
        if (boundSessionKey != sessionKey || boundServerUrl != serverUrl) return

        val existing = _messages.value
        val last = existing.lastOrNull()
        if (last != null && last.role == "assistant" && last.content == replyText) {
            return
        }
        val assistantMsg = MessageItem(
            role = "assistant",
            content = replyText,
            time = (System.currentTimeMillis() / 1000).toDouble()
        )
        _messages.value = existing + assistantMsg
    }

    fun clearError() {
        _errorMessage.value = null
    }

    override fun onCleared() {
        super.onCleared()
        activeLoadJob?.cancel()
        activeSendJob?.cancel()
        activeSseJob?.cancel()
        sseStreamManager.cancel()
    }
}

class ChatViewModelFactory(
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient,
    private val sseStreamManager: SseStreamManager
) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        if (modelClass.isAssignableFrom(ChatViewModel::class.java)) {
            return ChatViewModel(tokenStorage, apiClient, sseStreamManager) as T
        }
        throw IllegalArgumentException("Unknown ViewModel class: ${modelClass.name}")
    }
}
