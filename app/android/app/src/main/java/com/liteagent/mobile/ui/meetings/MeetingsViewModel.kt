package com.liteagent.mobile.ui.meetings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ConflictException
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.network.models.DecideMeetingRequest
import com.liteagent.mobile.core.network.models.MeetingDetail
import com.liteagent.mobile.core.network.models.MeetingItem
import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.UUID

data class MeetingsUiState(
    val meetings: List<MeetingItem> = emptyList(),
    val awaitingMeetings: List<MeetingItem> = emptyList(),
    val selectedMeeting: MeetingDetail? = null,
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val isDetailLoading: Boolean = false,
    val isSubmittingDecision: Boolean = false,
    val conflictErrorMessage: String? = null,
    val errorMessage: String? = null,
    val actionSuccessMessage: String? = null
) {
    val waitingCount: Int
        get() = awaitingMeetings.size
}

class MeetingsViewModel(
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient
) : ViewModel() {

    private val _uiState = MutableStateFlow(MeetingsUiState())
    val uiState: StateFlow<MeetingsUiState> = _uiState.asStateFlow()

    private var boundServerUrl: String = ""
    private var boundUsername: String = ""
    private var sessionEpoch: Long = 0L
    private var activeLoadJob: kotlinx.coroutines.Job? = null
    private var activeDetailJob: kotlinx.coroutines.Job? = null
    private var activeDecisionJob: kotlinx.coroutines.Job? = null

    init {
        activeLoadJob = viewModelScope.launch {
            val server = ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl())
            val session = tokenStorage.getSession(server)
            val username = session?.username ?: "default"
            initSessionAndLoad(server, username)
        }
    }

    private suspend fun initSessionAndLoad(serverUrl: String, username: String) {
        val currentEpoch = ++sessionEpoch
        boundServerUrl = serverUrl
        boundUsername = username
        _uiState.value = MeetingsUiState(isLoading = true)
        loadMeetingsInternal(currentEpoch)
    }

    fun refresh() {
        val currentEpoch = sessionEpoch
        activeLoadJob?.cancel()
        activeLoadJob = viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isRefreshing = true, errorMessage = null)
            loadMeetingsInternal(currentEpoch)
        }
    }

    fun loadMeetings() {
        val currentEpoch = sessionEpoch
        activeLoadJob?.cancel()
        activeLoadJob = viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            loadMeetingsInternal(currentEpoch)
        }
    }

    private suspend fun loadMeetingsInternal(epoch: Long = sessionEpoch) {
        if (epoch != sessionEpoch) return
        val currentServer = boundServerUrl.ifEmpty {
            ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()).also { boundServerUrl = it }
        }

        try {
            val result = apiClient.getReviewMeetings(currentServer)
            if (epoch != sessionEpoch) return

            if (result.isSuccess) {
                val list = result.getOrNull() ?: emptyList()
                val awaiting = list.filter { it.isAwaitingApproval }
                val others = list.filter { !it.isAwaitingApproval }
                // 待裁决审批置顶
                val sorted = awaiting + others

                _uiState.value = _uiState.value.copy(
                    meetings = sorted,
                    awaitingMeetings = awaiting,
                    isLoading = false,
                    isRefreshing = false,
                    errorMessage = if (_uiState.value.errorMessage == "获取会审列表失败") null else _uiState.value.errorMessage
                )
            } else {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    isRefreshing = false,
                    errorMessage = result.exceptionOrNull()?.message ?: "获取会审列表失败"
                )
            }
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            if (epoch == sessionEpoch) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    isRefreshing = false,
                    errorMessage = e.localizedMessage ?: e.message
                )
            }
        }
    }

    private var livePollJob: kotlinx.coroutines.Job? = null

    fun selectMeeting(meetingId: String) {
        val currentEpoch = sessionEpoch
        activeDetailJob?.cancel()
        livePollJob?.cancel()
        activeDetailJob = viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isDetailLoading = true, errorMessage = null)
            try {
                val result = apiClient.getReviewMeetingDetail(meetingId, serverUrl = boundServerUrl)
                if (currentEpoch != sessionEpoch) return@launch

                if (result.isSuccess) {
                    val detail = result.getOrNull()
                    _uiState.value = _uiState.value.copy(
                        selectedMeeting = detail,
                        isDetailLoading = false
                    )
                    startLivePollingIfActive(meetingId, detail)
                } else {
                    _uiState.value = _uiState.value.copy(
                        isDetailLoading = false,
                        errorMessage = result.exceptionOrNull()?.message ?: "加载会议详情失败"
                    )
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (currentEpoch == sessionEpoch) {
                    _uiState.value = _uiState.value.copy(
                        isDetailLoading = false,
                        errorMessage = e.localizedMessage ?: e.message
                    )
                }
            }
        }
    }

    private fun startLivePollingIfActive(meetingId: String, initialDetail: MeetingDetail?) {
        livePollJob?.cancel()
        val state = initialDetail?.state?.lowercase() ?: ""
        val isActive = state in listOf("open", "in_progress", "reviewing", "discussing", "waiting", "awaiting_approval", "voting")
        if (!isActive) return

        livePollJob = viewModelScope.launch {
            while (kotlinx.coroutines.isActive) {
                kotlinx.coroutines.delay(3000L) // 3秒长轮询直播
                val currentEpoch = sessionEpoch
                try {
                    val result = apiClient.getReviewMeetingDetail(meetingId, serverUrl = boundServerUrl)
                    if (currentEpoch != sessionEpoch || _uiState.value.selectedMeeting?.id != meetingId) break
                    if (result.isSuccess) {
                        val updated = result.getOrNull()
                        if (updated != null) {
                            _uiState.value = _uiState.value.copy(selectedMeeting = updated)
                            val updatedState = updated.state.lowercase()
                            if (updatedState !in listOf("open", "in_progress", "reviewing", "discussing", "waiting", "awaiting_approval", "voting")) {
                                break
                            }
                        }
                    }
                } catch (_: Exception) {
                    // ignore network blips during background stream polling
                }
            }
        }
    }

    fun closeMeetingDetail() {
        activeDetailJob?.cancel()
        livePollJob?.cancel()
        _uiState.value = _uiState.value.copy(selectedMeeting = null)
    }

    fun createMeeting(
        title: String,
        brief: String?,
        inviteTtlSeconds: Int = 86400,
        onSuccess: (String) -> Unit = {}
    ) {
        val currentEpoch = sessionEpoch
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            try {
                val req = com.liteagent.mobile.core.network.models.CreateMeetingRequest(
                    title = title.trim(),
                    brief = brief?.trim()?.ifEmpty { null },
                    inviteTtlSeconds = inviteTtlSeconds
                )
                val result = apiClient.createReviewMeeting(req, serverUrl = boundServerUrl)
                if (currentEpoch != sessionEpoch) return@launch
                if (result.isSuccess) {
                    val resp = result.getOrNull()
                    val meetingId = resp?.id ?: ""
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        actionSuccessMessage = "会审会议发起成功 (ID: $meetingId)"
                    )
                    loadMeetingsInternal(currentEpoch)
                    if (meetingId.isNotEmpty()) {
                        onSuccess(meetingId)
                        selectMeeting(meetingId)
                    }
                } else {
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        errorMessage = result.exceptionOrNull()?.message ?: "发起会审失败"
                    )
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (currentEpoch == sessionEpoch) {
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        errorMessage = e.localizedMessage ?: e.message
                    )
                }
            }
        }
    }

    /**
     * 会审掌上裁决：绑定 expected_version 与 UUID request_id，严格拦截 409 Conflict 与超时校验
     */
    fun decideMeeting(
        meetingId: String,
        decision: String, // approve, reject, revise
        note: String?,
        expectedVersion: Int,
        onSuccess: () -> Unit = {}
    ) {
        val currentEpoch = sessionEpoch
        activeDecisionJob?.cancel()
        activeDecisionJob = viewModelScope.launch {
            _uiState.value = _uiState.value.copy(
                isSubmittingDecision = true,
                errorMessage = null,
                conflictErrorMessage = null,
                actionSuccessMessage = null
            )
            val requestId = UUID.randomUUID().toString()

            val request = DecideMeetingRequest(
                decision = decision,
                note = note?.trim()?.ifEmpty { null },
                expectedVersion = expectedVersion,
                requestId = requestId
            )

            try {
                val result = apiClient.decideReviewMeeting(meetingId, request, serverUrl = boundServerUrl)
                if (currentEpoch != sessionEpoch) return@launch

                if (result.isSuccess) {
                    val updated = result.getOrNull()
                    val decisionText = when (decision) {
                        "approve" -> "已批准"
                        "revise" -> "已要求修改"
                        "reject" -> "已否决"
                        else -> "裁决已生效"
                    }
                    _uiState.value = _uiState.value.copy(
                        isSubmittingDecision = false,
                        selectedMeeting = updated,
                        actionSuccessMessage = "审批成功：$decisionText"
                    )
                    onSuccess()
                    loadMeetingsInternal(currentEpoch)
                } else {
                    val ex = result.exceptionOrNull()
                    if (ex is ConflictException) {
                        // 409 Conflict 拦截：版本冲突，自动刷新最新状态
                        _uiState.value = _uiState.value.copy(
                            isSubmittingDecision = false,
                            actionSuccessMessage = null,
                            conflictErrorMessage = ex.message ?: "版本冲突：议题已被更新，已自动刷新至最新状态！"
                        )
                        // 自动重新拉取最新详情与列表
                        selectMeeting(meetingId)
                        loadMeetingsInternal(currentEpoch)
                    } else {
                        // 瞬态或超时故障：进行状态与 request_id 审计事件核验
                        val verifyResult = apiClient.getReviewMeetingDetail(meetingId, serverUrl = boundServerUrl)
                        if (currentEpoch != sessionEpoch) return@launch

                        val verifyDetail = verifyResult.getOrNull()
                        val matchingDecision = verifyDetail?.events?.find {
                            it.kind.equals("decision", ignoreCase = true) && it.requestId == requestId
                        }

                        if (matchingDecision != null && matchingDecision.decisionValue.equals(decision, ignoreCase = true)) {
                            // 审计事件完全匹配本次 request_id 与裁决类型，确认服务端已成功应用
                            _uiState.value = _uiState.value.copy(
                                isSubmittingDecision = false,
                                selectedMeeting = verifyDetail,
                                actionSuccessMessage = "审批已成功同步至服务端"
                            )
                            onSuccess()
                            loadMeetingsInternal(currentEpoch)
                        } else if (verifyDetail != null && !verifyDetail.isAwaitingApproval) {
                            // 状态已变化，但未核实到本次 request_id（可能被其他人裁决或提交失败）
                            _uiState.value = _uiState.value.copy(
                                isSubmittingDecision = false,
                                selectedMeeting = verifyDetail,
                                actionSuccessMessage = null,
                                errorMessage = "状态已变化，本次提交结果未确认"
                            )
                            loadMeetingsInternal(currentEpoch)
                        } else {
                            _uiState.value = _uiState.value.copy(
                                isSubmittingDecision = false,
                                actionSuccessMessage = null,
                                errorMessage = ex?.message ?: "审批提交失败"
                            )
                        }
                    }
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (currentEpoch == sessionEpoch) {
                    _uiState.value = _uiState.value.copy(
                        isSubmittingDecision = false,
                        actionSuccessMessage = null,
                        errorMessage = e.localizedMessage ?: e.message
                    )
                }
            }
        }
    }

    fun onSessionChanged(serverUrl: String, username: String = "") {
        activeLoadJob?.cancel()
        activeDetailJob?.cancel()
        activeDecisionJob?.cancel()
        activeLoadJob = viewModelScope.launch {
            initSessionAndLoad(serverUrl, username)
        }
    }

    fun onLoggedOut() {
        activeLoadJob?.cancel()
        activeDetailJob?.cancel()
        activeDecisionJob?.cancel()
        sessionEpoch++
        boundServerUrl = ""
        boundUsername = ""
        _uiState.value = MeetingsUiState()
    }

    fun clearMessages() {
        _uiState.value = _uiState.value.copy(
            errorMessage = null,
            conflictErrorMessage = null,
            actionSuccessMessage = null
        )
    }
}

class MeetingsViewModelFactory(
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient
) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        if (modelClass.isAssignableFrom(MeetingsViewModel::class.java)) {
            return MeetingsViewModel(tokenStorage, apiClient) as T
        }
        throw IllegalArgumentException("Unknown ViewModel class: ${modelClass.name}")
    }
}
