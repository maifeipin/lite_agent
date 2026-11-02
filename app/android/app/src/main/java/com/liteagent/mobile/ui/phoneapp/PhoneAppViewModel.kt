package com.liteagent.mobile.ui.phoneapp

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.network.models.*
import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

data class PhoneAppUiState(
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val devices: List<PhoneDeviceItem> = emptyList(),
    val apps: List<PhoneCatalogAppItem> = emptyList(),
    val batches: List<PhoneBatchItem> = emptyList(),
    val selectedBatchDetail: PhoneBatchDetailResponse? = null,
    val isLoadingDetail: Boolean = false,
    val isSubmittingAction: Boolean = false,
    val errorMessage: String? = null,
    val actionSuccessMessage: String? = null
) {
    val pendingReviewCount: Int
        get() = batches.count { it.status == "pending_review" }

    val waitingApprovalCount: Int
        get() = pendingReviewCount

    val onlineDeviceCount: Int
        get() = devices.count { it.isOnline }
}

class PhoneAppViewModel(
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient,
    private val enablePolling: Boolean = true
) : ViewModel() {

    private val _uiState = MutableStateFlow(PhoneAppUiState())
    val uiState: StateFlow<PhoneAppUiState> = _uiState.asStateFlow()

    private var boundServerUrl: String = ""
    private var boundAccount: String = ""
    private var sessionGeneration: Long = 0L

    private var activeLoadJob: Job? = null
    private var activeDetailPollJob: Job? = null

    init {
        viewModelScope.launch {
            val server = ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl())
            val session = tokenStorage.getSession(server)
            boundServerUrl = server
            boundAccount = session?.username ?: "default"
            loadData()
        }
    }

    fun onSessionChanged(newServerUrl: String, username: String = "") {
        val cleanUrl = ServerUrlNormalizer.normalize(newServerUrl)
        val cleanUser = username.ifBlank { "default" }
        if (cleanUrl == boundServerUrl && cleanUser == boundAccount && _uiState.value.batches.isNotEmpty()) {
            return
        }

        sessionGeneration++
        activeLoadJob?.cancel()
        activeDetailPollJob?.cancel()

        boundServerUrl = cleanUrl
        boundAccount = cleanUser
        _uiState.value = PhoneAppUiState()
        loadData()
    }

    fun onLoggedOut() {
        sessionGeneration++
        activeLoadJob?.cancel()
        activeDetailPollJob?.cancel()
        boundServerUrl = ""
        boundAccount = ""
        _uiState.value = PhoneAppUiState()
    }

    fun loadData(): Job {
        activeLoadJob?.cancel()
        val gen = sessionGeneration
        val job = viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            loadInternal(gen)
        }
        activeLoadJob = job
        return job
    }

    fun refresh(): Job {
        activeLoadJob?.cancel()
        val gen = sessionGeneration
        val job = viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isRefreshing = true, errorMessage = null)
            loadInternal(gen)
        }
        activeLoadJob = job
        return job
    }

    private suspend fun loadInternal(expectedGen: Long) {
        val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
        val devicesRes = apiClient.getPhoneDevices(server)
        val appsRes = apiClient.getPhoneApps(server)
        val batchesRes = apiClient.getPhoneBatches(server)

        if (expectedGen != sessionGeneration) return

        val devices = devicesRes.getOrNull() ?: _uiState.value.devices
        val apps = appsRes.getOrNull() ?: _uiState.value.apps
        val batches = batchesRes.getOrNull() ?: _uiState.value.batches
        val err = batchesRes.exceptionOrNull()?.message

        _uiState.value = _uiState.value.copy(
            isLoading = false,
            isRefreshing = false,
            devices = devices,
            apps = apps,
            batches = batches,
            errorMessage = if (batches.isEmpty()) err else null
        )
    }

    fun selectBatch(batchId: String, startLivePolling: Boolean = false): Job {
        activeDetailPollJob?.cancel()
        val gen = sessionGeneration
        val job = viewModelScope.launch {
            val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
            _uiState.value = _uiState.value.copy(isLoadingDetail = true, errorMessage = null)
            val res = apiClient.getPhoneBatchDetail(batchId, server)
            if (gen != sessionGeneration) return@launch

            if (res.isSuccess) {
                val detail = res.getOrNull()!!
                _uiState.value = _uiState.value.copy(
                    isLoadingDetail = false,
                    selectedBatchDetail = detail
                )
                val status = detail.batch?.status ?: ""
                if (enablePolling && startLivePolling && status in listOf("queued", "running", "cancel_requested")) {
                    startBatchPolling(batchId, gen, server)
                }
            } else {
                _uiState.value = _uiState.value.copy(
                    isLoadingDetail = false,
                    errorMessage = res.exceptionOrNull()?.message ?: "加载批次详情失败"
                )
            }
        }
        activeDetailPollJob = job
        return job
    }

    private fun startBatchPolling(batchId: String, expectedGen: Long, serverUrl: String) {
        activeDetailPollJob = viewModelScope.launch {
            while (isActive && expectedGen == sessionGeneration) {
                delay(3000L)
                val currentDetail = _uiState.value.selectedBatchDetail ?: break
                if (currentDetail.batch?.batchId != batchId) break

                val res = apiClient.getPhoneBatchDetail(batchId, serverUrl)
                if (expectedGen != sessionGeneration) break
                val updated = res.getOrNull() ?: continue
                _uiState.value = _uiState.value.copy(selectedBatchDetail = updated)

                val st = updated.batch?.status ?: ""
                if (st in listOf("succeeded", "failed", "partial_failed", "cancelled", "rejected")) {
                    loadInternal(expectedGen)
                    break
                }
            }
        }
    }

    fun closeBatchDetail() {
        activeDetailPollJob?.cancel()
        _uiState.value = _uiState.value.copy(selectedBatchDetail = null)
    }

    fun createBatch(
        query: String,
        deviceId: String = "c68a3cf2",
        appIds: List<String> = listOf("jd"),
        mode: String = "fixed",
        maxBudget: Double? = null,
        selfOperatedOnly: Boolean = false,
        freeShippingOnly: Boolean = false,
        onSuccess: () -> Unit = {}
    ): Job? {
        val trimmedQuery = query.trim()
        if (trimmedQuery.isEmpty()) {
            _uiState.value = _uiState.value.copy(errorMessage = "请输入商品搜索关键词")
            return null
        }

        val gen = sessionGeneration
        return viewModelScope.launch {
            val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
            _uiState.value = _uiState.value.copy(isSubmittingAction = true, errorMessage = null)
            val filters = mutableMapOf<String, Any>()
            if (selfOperatedOnly) filters["self_operated"] = true
            if (freeShippingOnly) filters["free_shipping"] = true
            val budget = maxBudget?.let { mapOf<String, Any>("max_price" to it) }

            val req = CreatePhoneBatchRequest(
                query = trimmedQuery,
                appIds = appIds.ifEmpty { listOf("jd") },
                deviceId = deviceId.ifBlank { "c68a3cf2" },
                mode = mode,
                filters = filters.ifEmpty { null },
                budget = budget
            )
            val res = apiClient.createPhoneBatch(req, server)
            if (gen != sessionGeneration) return@launch

            if (res.isSuccess) {
                val created = res.getOrNull()!!
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    actionSuccessMessage = "已创建手机筛选批次: ${created.batchId ?: trimmedQuery}"
                )
                onSuccess()
                loadInternal(gen)
                created.batchId?.let { selectBatch(it, startLivePolling = true) }
            } else {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    errorMessage = res.exceptionOrNull()?.message ?: "创建批次失败"
                )
            }
        }
    }

    fun approveAndExecuteBatch(batchId: String): Job {
        val gen = sessionGeneration
        return viewModelScope.launch {
            val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
            _uiState.value = _uiState.value.copy(isSubmittingAction = true, errorMessage = null)
            val apprRes = apiClient.approvePhoneBatch(batchId, server)
            if (gen != sessionGeneration) return@launch

            if (apprRes.isFailure) {
                val err = apprRes.exceptionOrNull()?.message.orEmpty()
                if (!err.contains("approved") && !err.contains("已处于审批通过状态")) {
                    _uiState.value = _uiState.value.copy(
                        isSubmittingAction = false,
                        errorMessage = "审核批准失败: $err"
                    )
                    return@launch
                }
            }

            val execRes = apiClient.executePhoneBatch(batchId, server)
            if (gen != sessionGeneration) return@launch

            if (execRes.isSuccess) {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    actionSuccessMessage = "🛡️ 脚本已审核通过并派发手机执行！"
                )
                loadInternal(gen)
                selectBatch(batchId, startLivePolling = true)
            } else {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    errorMessage = execRes.exceptionOrNull()?.message ?: "派发执行失败"
                )
            }
        }
    }

    fun executeBatch(batchId: String): Job {
        val gen = sessionGeneration
        return viewModelScope.launch {
            val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
            _uiState.value = _uiState.value.copy(isSubmittingAction = true, errorMessage = null)
            val res = apiClient.executePhoneBatch(batchId, server)
            if (gen != sessionGeneration) return@launch

            if (res.isSuccess) {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    actionSuccessMessage = "▶️ 任务已派发手机执行！"
                )
                loadInternal(gen)
                selectBatch(batchId, startLivePolling = true)
            } else {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    errorMessage = res.exceptionOrNull()?.message ?: "执行失败"
                )
            }
        }
    }

    fun rejectBatch(batchId: String, reason: String = "掌上审核驳回"): Job {
        val gen = sessionGeneration
        return viewModelScope.launch {
            val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
            _uiState.value = _uiState.value.copy(isSubmittingAction = true, errorMessage = null)
            val res = apiClient.rejectPhoneBatch(batchId, reason.ifBlank { "掌上审核驳回" }, server)
            if (gen != sessionGeneration) return@launch

            if (res.isSuccess) {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    actionSuccessMessage = "❌ 已驳回该任务脚本"
                )
                loadInternal(gen)
                if (_uiState.value.selectedBatchDetail?.batch?.batchId == batchId) {
                    selectBatch(batchId, startLivePolling = false)
                }
            } else {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    errorMessage = res.exceptionOrNull()?.message ?: "驳回失败"
                )
            }
        }
    }

    fun cancelBatch(batchId: String): Job {
        val gen = sessionGeneration
        return viewModelScope.launch {
            val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
            _uiState.value = _uiState.value.copy(isSubmittingAction = true, errorMessage = null)
            val res = apiClient.cancelPhoneBatch(batchId, "用户在移动端主动取消", server)
            if (gen != sessionGeneration) return@launch

            if (res.isSuccess) {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    actionSuccessMessage = "🛑 已发送取消指令"
                )
                loadInternal(gen)
                if (_uiState.value.selectedBatchDetail?.batch?.batchId == batchId) {
                    selectBatch(batchId, startLivePolling = false)
                }
            } else {
                _uiState.value = _uiState.value.copy(
                    isSubmittingAction = false,
                    errorMessage = res.exceptionOrNull()?.message ?: "取消失败"
                )
            }
        }
    }

    fun resumeDevice(deviceId: String): Job {
        val gen = sessionGeneration
        return viewModelScope.launch {
            val server = boundServerUrl.ifEmpty { ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl()) }
            val res = apiClient.resumePhoneDevice(deviceId, server)
            if (gen != sessionGeneration) return@launch
            if (res.isSuccess) {
                _uiState.value = _uiState.value.copy(actionSuccessMessage = "设备 $deviceId 已恢复就绪")
                loadInternal(gen)
            } else {
                _uiState.value = _uiState.value.copy(errorMessage = res.exceptionOrNull()?.message ?: "恢复设备失败")
            }
        }
    }

    fun clearMessages() {
        _uiState.value = _uiState.value.copy(errorMessage = null, actionSuccessMessage = null)
    }

    override fun onCleared() {
        super.onCleared()
        activeLoadJob?.cancel()
        activeDetailPollJob?.cancel()
    }
}

class PhoneAppViewModelFactory(
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient
) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        return PhoneAppViewModel(tokenStorage, apiClient) as T
    }
}
