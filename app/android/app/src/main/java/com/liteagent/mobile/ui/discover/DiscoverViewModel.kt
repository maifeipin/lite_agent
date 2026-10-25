package com.liteagent.mobile.ui.discover

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.models.BillingSummaryResponse
import com.liteagent.mobile.core.network.models.EmailSummaryItem
import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class DiscoverUiState(
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val billingSummary: BillingSummaryResponse? = null,
    val emails: List<EmailSummaryItem> = emptyList(),
    val selectedEmail: EmailSummaryItem? = null,
    val selectedEmailHtml: String? = null,
    val isLoadingEmailHtml: Boolean = false,
    val errorMessage: String? = null
)

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
        val billingResult = apiClient.getBillingSummary(server)
        val mailResult = apiClient.getRecentEmails(20, server)

        val billing = billingResult.getOrNull()
        val emails = mailResult.getOrNull() ?: emptyList()

        _uiState.value = _uiState.value.copy(
            isLoading = false,
            isRefreshing = false,
            billingSummary = billing,
            emails = emails
        )
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
