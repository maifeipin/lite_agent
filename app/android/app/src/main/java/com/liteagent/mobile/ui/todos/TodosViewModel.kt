package com.liteagent.mobile.ui.todos

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.liteagent.mobile.core.network.ApiClient
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import com.liteagent.mobile.core.network.models.CreateTodoRequest
import com.liteagent.mobile.core.network.models.TodoItem
import com.liteagent.mobile.core.network.models.UpdateTodoRequest
import com.liteagent.mobile.core.security.SecureTokenStorage
import com.liteagent.mobile.core.storage.TodoDatabaseHelper
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class TodoFilter(val label: String) {
    ALL("全部"),
    PENDING("待处理"),
    ACTIVE("进行中"),
    DONE("已完成")
}

data class TodosUiState(
    val todos: List<TodoItem> = emptyList(),
    val filter: TodoFilter = TodoFilter.ALL,
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val isOffline: Boolean = false,
    val errorMessage: String? = null,
    val actionSuccessMessage: String? = null,
    val isCreating: Boolean = false
) {
    val filteredTodos: List<TodoItem>
        get() = when (filter) {
            TodoFilter.ALL -> todos
            TodoFilter.PENDING -> todos.filter { it.status.equals("pending", ignoreCase = true) }
            TodoFilter.ACTIVE -> todos.filter { it.status.equals("active", ignoreCase = true) }
            TodoFilter.DONE -> todos.filter { it.isCompleted }
        }

    val pendingCount: Int
        get() = todos.count { it.status.equals("pending", ignoreCase = true) }

    val activeCount: Int
        get() = todos.count { it.status.equals("active", ignoreCase = true) }

    val doneCount: Int
        get() = todos.count { it.isCompleted }
}

class TodosViewModel(
    private val context: Context,
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient
) : ViewModel() {

    private val _uiState = MutableStateFlow(TodosUiState())
    val uiState: StateFlow<TodosUiState> = _uiState.asStateFlow()

    private var dbHelper: TodoDatabaseHelper? = null
    private var boundServerUrl: String = ""
    private var boundUsername: String = ""

    init {
        viewModelScope.launch {
            val server = ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl())
            val session = tokenStorage.getSession(server)
            val username = session?.username ?: "default"
            initDatabaseAndLoad(server, username)
        }
    }

    private suspend fun initDatabaseAndLoad(serverUrl: String, username: String) {
        withContext(Dispatchers.IO) {
            dbHelper?.close()
            val dbKey = TodoDatabaseHelper.computeDbKey(serverUrl, username)
            val helper = TodoDatabaseHelper(context, dbKey)
            dbHelper = helper
            boundServerUrl = serverUrl
            boundUsername = username

            // 1. 0ms 闪电加载本地 SQLite 离线缓存
            val cached = helper.getAll()
            withContext(Dispatchers.Main) {
                _uiState.value = _uiState.value.copy(
                    todos = cached,
                    isLoading = cached.isEmpty()
                )
            }

            // 2. 异步网络拉取最新服务端待办
            fetchTodosFromServer(isRefresh = false)
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isRefreshing = true, errorMessage = null)
            fetchTodosFromServer(isRefresh = true)
        }
    }

    private suspend fun fetchTodosFromServer(isRefresh: Boolean) {
        val currentServer = boundServerUrl
        try {
            val result = apiClient.getTodos(currentServer)
            if (result.isSuccess) {
                val serverTodos = result.getOrNull() ?: emptyList()
                withContext(Dispatchers.IO) {
                    dbHelper?.saveAll(serverTodos)
                }
                _uiState.value = _uiState.value.copy(
                    todos = serverTodos,
                    isLoading = false,
                    isRefreshing = false,
                    isOffline = false,
                    errorMessage = null
                )
            } else {
                val err = result.exceptionOrNull()?.message ?: "同步待办失败"
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    isRefreshing = false,
                    isOffline = true,
                    errorMessage = if (isRefresh) err else null
                )
            }
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            _uiState.value = _uiState.value.copy(
                isLoading = false,
                isRefreshing = false,
                isOffline = true,
                errorMessage = if (isRefresh) (e.localizedMessage ?: e.message) else null
            )
        }
    }

    fun setFilter(filter: TodoFilter) {
        _uiState.value = _uiState.value.copy(filter = filter)
    }

    /**
     * 乐观更新待办状态：即时翻转本地与 SQLite，后台同步服务端，失败时自动回滚
     */
    fun toggleTodoStatus(item: TodoItem) {
        val oldStatus = item.status
        val newStatus = if (item.isCompleted) "pending" else "done"
        val updatedItem = item.copy(
            status = newStatus,
            updatedAt = System.currentTimeMillis().toString()
        )

        // 立即乐观更新内存列表与 SQLite
        val currentList = _uiState.value.todos
        val updatedList = currentList.map { if (it.id == item.id) updatedItem else it }
        _uiState.value = _uiState.value.copy(todos = updatedList)

        viewModelScope.launch(Dispatchers.IO) {
            dbHelper?.updateStatus(item.id, newStatus)

            // 后台同步服务端
            val result = apiClient.updateTodo(
                id = item.id,
                request = UpdateTodoRequest(status = newStatus),
                serverUrl = boundServerUrl
            )

            if (result.isFailure) {
                // 失败回滚
                dbHelper?.updateStatus(item.id, oldStatus)
                withContext(Dispatchers.Main) {
                    val reverted = _uiState.value.todos.map { if (it.id == item.id) item else it }
                    _uiState.value = _uiState.value.copy(
                        todos = reverted,
                        errorMessage = "状态同步失败: ${result.exceptionOrNull()?.message}"
                    )
                }
            }
        }
    }

    /**
     * 创建新待办事项
     */
    fun createTodo(
        title: String,
        project: String? = null,
        description: String? = null,
        dueAt: String? = null,
        onSuccess: () -> Unit = {}
    ) {
        val trimmedTitle = title.trim()
        if (trimmedTitle.isEmpty()) {
            _uiState.value = _uiState.value.copy(errorMessage = "待办标题不能为空")
            return
        }

        viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isCreating = true, errorMessage = null)
            try {
                val request = CreateTodoRequest(
                    title = trimmedTitle,
                    project = project?.trim()?.ifEmpty { null },
                    description = description?.trim()?.ifEmpty { null },
                    dueAt = dueAt?.trim()?.ifEmpty { null }
                )
                val result = apiClient.createTodo(request, serverUrl = boundServerUrl)
                if (result.isSuccess) {
                    _uiState.value = _uiState.value.copy(
                        isCreating = false,
                        actionSuccessMessage = "待办创建成功"
                    )
                    onSuccess()
                    fetchTodosFromServer(isRefresh = true)
                } else {
                    _uiState.value = _uiState.value.copy(
                        isCreating = false,
                        errorMessage = result.exceptionOrNull()?.message ?: "创建待办失败"
                    )
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                _uiState.value = _uiState.value.copy(
                    isCreating = false,
                    errorMessage = e.localizedMessage ?: e.message
                )
            }
        }
    }

    /**
     * 删除待办事项
     */
    fun deleteTodo(id: String) {
        val currentList = _uiState.value.todos
        val deletedItem = currentList.find { it.id == id } ?: return
        val filteredList = currentList.filter { it.id != id }
        _uiState.value = _uiState.value.copy(todos = filteredList)

        viewModelScope.launch(Dispatchers.IO) {
            dbHelper?.deleteTodo(id)
            val result = apiClient.deleteTodo(id, serverUrl = boundServerUrl)
            if (result.isFailure) {
                // 恢复
                dbHelper?.insertOrUpdate(deletedItem)
                withContext(Dispatchers.Main) {
                    _uiState.value = _uiState.value.copy(
                        todos = currentList,
                        errorMessage = "删除失败: ${result.exceptionOrNull()?.message}"
                    )
                }
            }
        }
    }

    fun onSessionChanged(serverUrl: String, username: String) {
        viewModelScope.launch {
            initDatabaseAndLoad(serverUrl, username)
        }
    }

    fun onLoggedOut() {
        dbHelper?.close()
        dbHelper = null
        boundServerUrl = ""
        boundUsername = ""
        _uiState.value = TodosUiState()
    }

    fun clearMessages() {
        _uiState.value = _uiState.value.copy(errorMessage = null, actionSuccessMessage = null)
    }

    override fun onCleared() {
        super.onCleared()
        dbHelper?.close()
    }
}

class TodosViewModelFactory(
    private val context: Context,
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient
) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        if (modelClass.isAssignableFrom(TodosViewModel::class.java)) {
            return TodosViewModel(context, tokenStorage, apiClient) as T
        }
        throw IllegalArgumentException("Unknown ViewModel class: ${modelClass.name}")
    }
}
