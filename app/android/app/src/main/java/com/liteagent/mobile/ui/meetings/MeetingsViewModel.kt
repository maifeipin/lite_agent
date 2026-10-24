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

    init {
        viewModelScope.launch {
            boundServerUrl = ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl())
            loadMeetings()
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isRefreshing = true, errorMessage = null)
            loadMeetingsInternal()
        }
    }

    fun loadMeetings() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            loadMeetingsInternal()
        }
    }

    private suspend fun loadMeetingsInternal() {
        val currentServer = boundServerUrl.ifEmpty {
            ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()).also { boundServerUrl = it }
        }

        try {
            val result = apiClient.getReviewMeetings(currentServer)
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
                    errorMessage = null
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
            _uiState.value = _uiState.value.copy(
                isLoading = false,
                isRefreshing = false,
                errorMessage = e.localizedMessage ?: e.message
            )
        }
    }

    fun selectMeeting(meetingId: String) {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isDetailLoading = true, errorMessage = null)
            try {
                val result = apiClient.getReviewMeetingDetail(meetingId, serverUrl = boundServerUrl)
                if (result.isSuccess) {
                    _uiState.value = _uiState.value.copy(
                        selectedMeeting = result.getOrNull(),
                        isDetailLoading = false
                    )
                } else {
                    _uiState.value = _uiState.value.copy(
                        isDetailLoading = false,
                        errorMessage = result.exceptionOrNull()?.message ?: "加载会议详情失败"
                    )
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                _uiState.value = _uiState.value.copy(
                    isDetailLoading = false,
                    errorMessage = e.localizedMessage ?: e.message
                )
            }
        }
    }

    fun closeMeetingDetail() {
        _uiState.value = _uiState.value.copy(selectedMeeting = null)
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
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isSubmittingDecision = true, errorMessage = null, conflictErrorMessage = null)
            val requestId = UUID.randomUUID().toString()

            val request = DecideMeetingRequest(
                decision = decision,
                note = note?.trim()?.ifEmpty { null },
                expectedVersion = expectedVersion,
                requestId = requestId
            )

            try {
                val result = apiClient.decideReviewMeeting(meetingId, request, serverUrl = boundServerUrl)
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
                    loadMeetingsInternal()
                } else {
                    val ex = result.exceptionOrNull()
                    if (ex is ConflictException) {
                        // 409 Conflict 拦截：版本冲突，自动刷新最新状态
                        _uiState.value = _uiState.value.copy(
                            isSubmittingDecision = false,
                            conflictErrorMessage = ex.message ?: "版本冲突：议题已被更新，已自动刷新至最新状态！"
                        )
                        // 自动重新拉取最新详情与列表
                        selectMeeting(meetingId)
                        loadMeetingsInternal()
                    } else {
                        // 瞬态或超时故障：进行状态核验
                        val verifyResult = apiClient.getReviewMeetingDetail(meetingId, serverUrl = boundServerUrl)
                        val verifyDetail = verifyResult.getOrNull()
                        if (verifyDetail != null && verifyDetail.state != "awaiting_approval" && verifyDetail.state != "waiting") {
                            // 实际上已在服务端执行成功
                            _uiState.value = _uiState.value.copy(
                                isSubmittingDecision = false,
                                selectedMeeting = verifyDetail,
                                actionSuccessMessage = "审批已成功同步至服务端"
                            )
                            onSuccess()
                            loadMeetingsInternal()
                        } else {
                            _uiState.value = _uiState.value.copy(
                                isSubmittingDecision = false,
                                errorMessage = ex?.message ?: "审批提交失败"
                            )
                        }
                    }
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                _uiState.value = _uiState.value.copy(
                    isSubmittingDecision = false,
                    errorMessage = e.localizedMessage ?: e.message
                )
            }
        }
    }

    fun onSessionChanged(serverUrl: String) {
        boundServerUrl = ServerUrlNormalizer.normalize(serverUrl)
        _uiState.value = MeetingsUiState()
        loadMeetings()
    }

    fun onLoggedOut() {
        boundServerUrl = ""
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
