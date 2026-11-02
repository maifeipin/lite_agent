package com.liteagent.mobile.ui.discover

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.models.BillingSummaryResponse
import com.liteagent.mobile.core.network.models.EmailSummaryItem
import com.liteagent.mobile.core.network.models.RssBriefResponse
import com.liteagent.mobile.core.network.models.RssBriefTopicItem
import com.liteagent.mobile.core.network.models.StatementSummaryItem
import com.liteagent.mobile.core.network.models.TaskSpecItem
import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class DiscoverUiState(
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val searchQuery: String = "",
    val rssBrief: RssBriefResponse? = null,
    val taskSpecs: List<TaskSpecItem> = emptyList(),
    val runningTaskSpecIds: Set<String> = emptySet(),
    val billingSummary: BillingSummaryResponse? = null,
    val emails: List<EmailSummaryItem> = emptyList(),
    val selectedEmail: EmailSummaryItem? = null,
    val selectedEmailHtml: String? = null,
    val isLoadingEmailHtml: Boolean = false,
    val actionMessage: String? = null,
    val errorMessage: String? = null
) {
    val filteredTopics: List<RssBriefTopicItem>
        get() {
            val all = rssBrief?.topics ?: emptyList()
            val q = searchQuery.trim().lowercase()
            if (q.isEmpty()) return all
            return all.filter {
                it.title.lowercase().contains(q) ||
                (it.analysis?.lowercase()?.contains(q) == true) ||
                (it.sentimentReason?.lowercase()?.contains(q) == true)
            }
        }

    val filteredTaskSpecs: List<TaskSpecItem>
        get() {
            val q = searchQuery.trim().lowercase()
            if (q.isEmpty()) return taskSpecs
            return taskSpecs.filter {
                it.name.lowercase().contains(q) ||
                (it.description?.lowercase()?.contains(q) == true) ||
                it.id.lowercase().contains(q)
            }
        }

    val filteredEmails: List<EmailSummaryItem>
        get() {
            val q = searchQuery.trim().lowercase()
            if (q.isEmpty()) return emails
            return emails.filter {
                (it.subject?.lowercase()?.contains(q) == true) ||
                (it.sender?.lowercase()?.contains(q) == true) ||
                (it.summary?.lowercase()?.contains(q) == true)
            }
        }

    val filteredStatements: List<StatementSummaryItem>
        get() {
            val all = billingSummary?.recentStatements ?: emptyList()
            val q = searchQuery.trim().lowercase()
            if (q.isEmpty()) return all
            return all.filter {
                (it.bank?.lowercase()?.contains(q) == true) ||
                (it.cardNumber?.lowercase()?.contains(q) == true) ||
                (it.dueDate?.lowercase()?.contains(q) == true)
            }
        }
}

class DiscoverViewModel(
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient
) : ViewModel() {

    private val _uiState = MutableStateFlow(DiscoverUiState())
    val uiState: StateFlow<DiscoverUiState> = _uiState.asStateFlow()

    private var boundServerUrl: String = ""

    init {
        viewModelScope.launch {
            val server = tokenStorage.getActiveServerUrl()
            boundServerUrl = server
            loadData()
        }
    }

    fun onServerChanged(serverUrl: String) {
        boundServerUrl = serverUrl
        loadData()
    }

    fun updateSearchQuery(query: String) {
        _uiState.value = _uiState.value.copy(searchQuery = query)
    }

    fun clearActionMessage() {
        _uiState.value = _uiState.value.copy(actionMessage = null, errorMessage = null)
    }

    fun loadData() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isLoading = true, errorMessage = null)
            loadInternal()
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isRefreshing = true, errorMessage = null)
            loadInternal()
        }
    }

    private suspend fun loadInternal() {
        val server = boundServerUrl.ifEmpty { tokenStorage.getActiveServerUrl() }
        val rssResult = apiClient.getRssBrief(server)
        val specsResult = apiClient.getTaskSpecs(server)
        val billingResult = apiClient.getBillingSummary(server)
        val mailResult = apiClient.getRecentEmails(20, server)

        val rss = rssResult.getOrNull()
        val specs = specsResult.getOrNull() ?: emptyList()
        val billing = billingResult.getOrNull()
        val emails = mailResult.getOrNull() ?: emptyList()

        _uiState.value = _uiState.value.copy(
            isLoading = false,
            isRefreshing = false,
            rssBrief = rss,
            taskSpecs = specs,
            billingSummary = billing,
            emails = emails
        )
    }

    fun runTaskSpec(specId: String, specName: String = specId) {
        if (specId.isBlank()) return
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(
                runningTaskSpecIds = _uiState.value.runningTaskSpecIds + specId,
                actionMessage = null,
                errorMessage = null
            )
            val server = boundServerUrl.ifEmpty { tokenStorage.getActiveServerUrl() }
            val result = apiClient.runTaskSpec(specId, server)
            val updatedSet = _uiState.value.runningTaskSpecIds - specId
            result.onSuccess { resp ->
                val specs = apiClient.getTaskSpecs(server).getOrNull() ?: _uiState.value.taskSpecs
                _uiState.value = _uiState.value.copy(
                    runningTaskSpecIds = updatedSet,
                    taskSpecs = specs,
                    actionMessage = resp.message ?: "已触发任务「$specName」执行"
                )
            }.onFailure { err ->
                _uiState.value = _uiState.value.copy(
                    runningTaskSpecIds = updatedSet,
                    errorMessage = err.message ?: "任务触发失败"
                )
            }
        }
    }

    fun openEmailDetail(email: EmailSummaryItem) {
        _uiState.value = _uiState.value.copy(
            selectedEmail = email,
            isLoadingEmailHtml = true,
            selectedEmailHtml = null
        )

        val account = email.accountName ?: ""
        val uid = email.uid ?: ""
        if (account.isEmpty() || uid.isEmpty()) {
            _uiState.value = _uiState.value.copy(
                isLoadingEmailHtml = false,
                selectedEmailHtml = "<p style='color:red;'>缺少账户或 UID 标识</p>"
            )
            return
        }

        viewModelScope.launch(kotlinx.coroutines.Dispatchers.IO) {
            try {
                val server = boundServerUrl.ifEmpty { tokenStorage.getActiveServerUrl() }.trimEnd('/')
                val url = if (server.endsWith("/agent")) {
                    "$server/api/v1/email/html?account=${android.net.Uri.encode(account)}&uid=${android.net.Uri.encode(uid)}"
                } else {
                    "$server/agent/api/v1/email/html?account=${android.net.Uri.encode(account)}&uid=${android.net.Uri.encode(uid)}"
                }

                val session = tokenStorage.getSession(server)
                val token = session?.sessionToken ?: session?.sessionId ?: ""

                val client = okhttp3.OkHttpClient()
                val req = okhttp3.Request.Builder()
                    .url(url)
                    .apply {
                        if (token.isNotEmpty()) header("Authorization", "Bearer $token")
                    }
                    .build()

                client.newCall(req).execute().use { response ->
                    val body = response.body?.string() ?: ""
                    _uiState.value = _uiState.value.copy(
                        isLoadingEmailHtml = false,
                        selectedEmailHtml = if (response.isSuccessful) body else "<p style='color:red;'>加载邮件失败: HTTP ${response.code}</p>"
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoadingEmailHtml = false,
                    selectedEmailHtml = "<p style='color:red;'>加载异常: ${e.message}</p>"
                )
            }
        }
    }

    fun closeEmailDetail() {
        _uiState.value = _uiState.value.copy(selectedEmail = null, selectedEmailHtml = null)
    }
}

class DiscoverViewModelFactory(
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient
) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        return DiscoverViewModel(tokenStorage, apiClient) as T
    }
}
