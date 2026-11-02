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
import com.liteagent.mobile.core.storage.ITodoDatabaseHelper
import com.liteagent.mobile.core.storage.TodoDatabaseHelper
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
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

enum class TodoSortMode(val label: String) {
    SMART_RECENT("🕐 最近待办"),
    DUE_PROXIMITY("📅 临近截止"),
    PRIORITY("🔥 优先级")
}

object TodoSortHelper {
    private val isoFormats = listOf(
        "yyyy-MM-dd'T'HH:mm:ss.SSSSSSXXX",
        "yyyy-MM-dd'T'HH:mm:ssXXX",
        "yyyy-MM-dd'T'HH:mm:ssZ",
        "yyyy-MM-dd HH:mm:ss",
        "yyyy-MM-dd HH:mm",
        "yyyy-MM-dd"
    )

    fun parseEpochMillis(raw: String?): Long? {
        val s = raw?.trim()?.takeIf { it.isNotEmpty() } ?: return null
        s.toLongOrNull()?.let { return it }
        // 统一将微秒截断或标准化以兼容 SimpleDateFormat
        val normalized = s.replace(Regex("\\.(\\d{3})\\d+"), ".$1")
        for (pattern in listOf(
            "yyyy-MM-dd'T'HH:mm:ss.SSSXXX",
            "yyyy-MM-dd'T'HH:mm:ssXXX",
            "yyyy-MM-dd'T'HH:mm:ssZ",
            "yyyy-MM-dd'T'HH:mm:ss",
            "yyyy-MM-dd HH:mm:ss",
            "yyyy-MM-dd HH:mm",
            "yyyy-MM-dd"
        )) {
            try {
                val sdf = java.text.SimpleDateFormat(pattern, java.util.Locale.US)
                sdf.isLenient = true
                val d = sdf.parse(normalized)
                if (d != null) return d.time
            } catch (_: Exception) {
            }
        }
        return null
    }

    fun sortTodos(items: List<TodoItem>, mode: TodoSortMode, nowMillis: Long = System.currentTimeMillis()): List<TodoItem> {
        return items.sortedWith(Comparator { a, b ->
            // 1. 已完成自动沉底 (GTD 黄金准则)
            if (a.isCompleted != b.isCompleted) {
                return@Comparator if (a.isCompleted) 1 else -1
            }

            val aDue = parseEpochMillis(a.dueAt)
            val bDue = parseEpochMillis(b.dueAt)
            val aUpd = parseEpochMillis(a.updatedAt) ?: parseEpochMillis(a.createdAt) ?: 0L
            val bUpd = parseEpochMillis(b.updatedAt) ?: parseEpochMillis(b.createdAt) ?: 0L

            when (mode) {
                TodoSortMode.SMART_RECENT -> {
                    // 最近优先：综合取 max(截止时间/最近更新时间)，同时有截止日期的近期任务优先
                    val aHasDue = aDue != null
                    val bHasDue = bDue != null
                    val aEffective = maxOf(aUpd, aDue ?: 0L)
                    val bEffective = maxOf(bUpd, bDue ?: 0L)
                    if (aEffective != bEffective) {
                        bEffective.compareTo(aEffective) // 越新/越近的排越前
                    } else if (aHasDue != bHasDue) {
                        if (aHasDue) -1 else 1
                    } else {
                        (a.priority ?: 2).compareTo(b.priority ?: 2)
                    }
                }
                TodoSortMode.DUE_PROXIMITY -> {
                    // 临近截止优先：有截止时间的排前面，且按距离当前时间 |due - now| 从近到远排序
                    if ((aDue != null) != (bDue != null)) {
                        if (aDue != null) -1 else 1
                    } else if (aDue != null && bDue != null && aDue != bDue) {
                        val aDist = kotlin.math.abs(aDue - nowMillis)
                        val bDist = kotlin.math.abs(bDue - nowMillis)
                        aDist.compareTo(bDist)
                    } else {
                        bUpd.compareTo(aUpd)
                    }
                }
                TodoSortMode.PRIORITY -> {
                    // 优先级优先：P0 -> P1 -> P2 -> P3，同优先级按最近时间降序
                    val aPrio = a.priority ?: 2
                    val bPrio = b.priority ?: 2
                    if (aPrio != bPrio) {
                        aPrio.compareTo(bPrio)
                    } else {
                        val aEff = maxOf(aUpd, aDue ?: 0L)
                        val bEff = maxOf(bUpd, bDue ?: 0L)
                        bEff.compareTo(aEff)
                    }
                }
            }
        })
    }
}

data class TodosUiState(
    val todos: List<TodoItem> = emptyList(),
    val filter: TodoFilter = TodoFilter.ALL,
    val sortMode: TodoSortMode = TodoSortMode.SMART_RECENT,
    val isLoading: Boolean = false,
    val isRefreshing: Boolean = false,
    val isOffline: Boolean = false,
    val errorMessage: String? = null,
    val actionSuccessMessage: String? = null,
    val isCreating: Boolean = false
) {
    val filteredTodos: List<TodoItem>
        get() {
            val base = when (filter) {
                TodoFilter.ALL -> todos
                TodoFilter.PENDING -> todos.filter { it.status.equals("pending", ignoreCase = true) }
                TodoFilter.ACTIVE -> todos.filter { it.status.equals("active", ignoreCase = true) }
                TodoFilter.DONE -> todos.filter { it.isCompleted }
            }
            return TodoSortHelper.sortTodos(base, sortMode)
        }

    val pendingCount: Int
        get() = todos.count { it.status.equals("pending", ignoreCase = true) }

    val activeCount: Int
        get() = todos.count { it.status.equals("active", ignoreCase = true) }

    val doneCount: Int
        get() = todos.count { it.isCompleted }
}

class TodosViewModel(
    private val context: Context? = null,
    private val tokenStorage: SecureTokenStorage,
    private val apiClient: ApiClient,
    private val dbHelperFactory: ((serverUrl: String, username: String) -> ITodoDatabaseHelper)? = null,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO
) : ViewModel() {

    private val _uiState = MutableStateFlow(TodosUiState())
    val uiState: StateFlow<TodosUiState> = _uiState.asStateFlow()

    private var dbHelper: ITodoDatabaseHelper? = null
    private var boundServerUrl: String = ""
    private var boundUsername: String = ""
    private var sessionEpoch: Long = 0L
    private var activeInitJob: Job? = null
    private var activeFetchJob: Job? = null
    private val activeWriteJobs = java.util.Collections.synchronizedSet(mutableSetOf<Job>())

    init {
        activeInitJob = viewModelScope.launch {
            val server = ServerUrlNormalizer.normalize(tokenStorage.getActiveServerUrl())
            val session = tokenStorage.getSession(server)
            val username = session?.username ?: "default"
            initDatabaseAndLoad(server, username)
        }
    }

    private fun launchWriteJob(block: suspend kotlinx.coroutines.CoroutineScope.() -> Unit): Job {
        var job: Job? = null
        job = viewModelScope.launch {
            try {
                block()
            } finally {
                job?.let { activeWriteJobs.remove(it) }
            }
        }
        activeWriteJobs.add(job)
        return job
    }

    private suspend fun initDatabaseAndLoad(serverUrl: String, username: String) {
        val currentEpoch = ++sessionEpoch
        _uiState.value = TodosUiState(isLoading = true)

        withContext(ioDispatcher) {
            val dbKey = TodoDatabaseHelper.computeDbKey(serverUrl, username)
            val newHelper: ITodoDatabaseHelper = dbHelperFactory?.invoke(serverUrl, username)
                ?: TodoDatabaseHelper(
                    context ?: throw IllegalStateException("Context required when no helper factory is provided"),
                    dbKey
                )

            if (currentEpoch != sessionEpoch) {
                newHelper.close()
                return@withContext
            }

            dbHelper?.close()
            dbHelper = newHelper
            boundServerUrl = serverUrl
            boundUsername = username

            // 1. 0ms 闪电加载本地 SQLite 离线缓存
            val cached = newHelper.getAll()
            if (currentEpoch != sessionEpoch) return@withContext

            withContext(Dispatchers.Main) {
                if (currentEpoch == sessionEpoch) {
                    _uiState.value = _uiState.value.copy(
                        todos = cached,
                        isLoading = cached.isEmpty()
                    )
                }
            }

            // 2. 异步网络拉取最新服务端待办
            fetchTodosFromServer(isRefresh = false, epoch = currentEpoch, helper = newHelper)
        }
    }

    fun refresh() {
        val currentEpoch = sessionEpoch
        val currentHelper = dbHelper
        _uiState.value = _uiState.value.copy(isRefreshing = true, errorMessage = null)
        activeFetchJob?.cancel()
        activeFetchJob = viewModelScope.launch {
            fetchTodosFromServer(isRefresh = true, epoch = currentEpoch, helper = currentHelper)
        }
    }

    private suspend fun fetchTodosFromServer(
        isRefresh: Boolean,
        epoch: Long = sessionEpoch,
        helper: ITodoDatabaseHelper? = dbHelper
    ) {
        if (epoch != sessionEpoch || helper == null || helper != dbHelper) return
        val currentServer = boundServerUrl
        try {
            val result = apiClient.getTodos(currentServer)
            if (epoch != sessionEpoch || helper != dbHelper) return

            if (result.isSuccess) {
                val serverTodos = result.getOrNull() ?: emptyList()
                withContext(ioDispatcher) {
                    if (epoch == sessionEpoch && helper == dbHelper) {
                        helper.saveAll(serverTodos)
                    }
                }
                if (epoch == sessionEpoch) {
                    _uiState.value = _uiState.value.copy(
                        todos = serverTodos,
                        isLoading = false,
                        isRefreshing = false,
                        isOffline = false,
                        errorMessage = null
                    )
                }
            } else {
                if (epoch == sessionEpoch) {
                    val err = result.exceptionOrNull()?.message ?: "同步待办失败"
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        isRefreshing = false,
                        isOffline = true,
                        errorMessage = if (isRefresh) err else null
                    )
                }
            }
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            if (epoch == sessionEpoch) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    isRefreshing = false,
                    isOffline = true,
                    errorMessage = if (isRefresh) (e.localizedMessage ?: e.message) else null
                )
            }
        }
    }

    fun setFilter(filter: TodoFilter) {
        _uiState.value = _uiState.value.copy(filter = filter)
    }

    fun setSortMode(sortMode: TodoSortMode) {
        _uiState.value = _uiState.value.copy(sortMode = sortMode)
    }

    /**
     * 乐观更新待办状态：即时翻转本地与 SQLite，后台同步服务端，失败时自动回滚
     * 捕获会话代次与数据库沙箱引用，严格杜绝跨账号回滚与界面污染
     */
    fun toggleTodoStatus(item: TodoItem) {
        val currentEpoch = sessionEpoch
        val currentHelper = dbHelper ?: return
        val currentServer = boundServerUrl

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

        launchWriteJob {
            try {
                withContext(ioDispatcher) {
                    if (currentEpoch != sessionEpoch || currentHelper != dbHelper) return@withContext
                    currentHelper.updateStatus(item.id, newStatus)

                    // 后台同步服务端
                    val result = apiClient.updateTodo(
                        id = item.id,
                        request = UpdateTodoRequest(status = newStatus),
                        serverUrl = currentServer
                    )

                    if (currentEpoch != sessionEpoch || currentHelper != dbHelper) return@withContext

                    if (result.isFailure) {
                        // 失败仅回滚当前所属代次的数据库沙箱
                        currentHelper.updateStatus(item.id, oldStatus)
                        withContext(Dispatchers.Main) {
                            if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                                val reverted = _uiState.value.todos.map { if (it.id == item.id) item else it }
                                _uiState.value = _uiState.value.copy(
                                    todos = reverted,
                                    errorMessage = "状态同步失败: ${result.exceptionOrNull()?.message}"
                                )
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                    withContext(ioDispatcher) {
                        if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                            currentHelper.updateStatus(item.id, oldStatus)
                        }
                    }
                    withContext(Dispatchers.Main) {
                        if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                            val reverted = _uiState.value.todos.map { if (it.id == item.id) item else it }
                            _uiState.value = _uiState.value.copy(
                                todos = reverted,
                                errorMessage = "状态同步失败: ${e.localizedMessage ?: e.message}"
                            )
                        }
                    }
                }
            }
        }
    }

    /**
     * 创建新待办事项：绑定会话代次与数据库实例
     */
    fun createTodo(
        title: String,
        project: String? = null,
        description: String? = null,
        dueAt: String? = null,
        remindBeforeMins: Int = 30,
        remindIntervalMins: Int = 0,
        notifyEnabled: Boolean = true,
        recurCron: String? = null,
        priority: Int = 3,
        dispatchTarget: String? = null,
        onSuccess: () -> Unit = {}
    ) {
        val trimmedTitle = title.trim()
        if (trimmedTitle.isEmpty()) {
            _uiState.value = _uiState.value.copy(errorMessage = "待办标题不能为空")
            return
        }

        val currentEpoch = sessionEpoch
        val currentHelper = dbHelper ?: return
        val currentServer = boundServerUrl

        launchWriteJob {
            _uiState.value = _uiState.value.copy(isCreating = true, errorMessage = null)
            try {
                val request = CreateTodoRequest(
                    title = trimmedTitle,
                    project = project?.trim()?.ifEmpty { null },
                    description = description?.trim()?.ifEmpty { null },
                    dueAt = dueAt?.trim()?.ifEmpty { null },
                    notifyEnabled = notifyEnabled,
                    remindBeforeMins = remindBeforeMins,
                    remindIntervalMins = remindIntervalMins,
                    recurCron = recurCron?.trim()?.ifEmpty { null },
                    priority = priority,
                    dispatchTarget = dispatchTarget?.trim()?.ifEmpty { null }
                )
                val result = apiClient.createTodo(request, serverUrl = currentServer)
                if (currentEpoch != sessionEpoch || currentHelper != dbHelper) return@launchWriteJob

                if (result.isSuccess) {
                    _uiState.value = _uiState.value.copy(
                        isCreating = false,
                        actionSuccessMessage = "待办创建成功"
                    )
                    onSuccess()
                    fetchTodosFromServer(isRefresh = true, epoch = currentEpoch, helper = currentHelper)
                } else {
                    _uiState.value = _uiState.value.copy(
                        isCreating = false,
                        errorMessage = result.exceptionOrNull()?.message ?: "创建待办失败"
                    )
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                    _uiState.value = _uiState.value.copy(
                        isCreating = false,
                        errorMessage = e.localizedMessage ?: e.message
                    )
                }
            }
        }
    }

    /**
     * 删除待办事项：绑定会话代次与数据库沙箱，防止跨会话回滚
     */
    fun deleteTodo(id: String) {
        val currentEpoch = sessionEpoch
        val currentHelper = dbHelper ?: return
        val currentServer = boundServerUrl

        val currentList = _uiState.value.todos
        val deletedItem = currentList.find { it.id == id } ?: return
        val filteredList = currentList.filter { it.id != id }
        _uiState.value = _uiState.value.copy(todos = filteredList)

        launchWriteJob {
            try {
                withContext(ioDispatcher) {
                    if (currentEpoch != sessionEpoch || currentHelper != dbHelper) return@withContext
                    currentHelper.deleteTodo(id)
                    val result = apiClient.deleteTodo(id, serverUrl = currentServer)
                    if (currentEpoch != sessionEpoch || currentHelper != dbHelper) return@withContext

                    if (result.isFailure) {
                        // 恢复
                        currentHelper.insertOrUpdate(deletedItem)
                        withContext(Dispatchers.Main) {
                            if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                                _uiState.value = _uiState.value.copy(
                                    todos = currentList,
                                    errorMessage = "删除失败: ${result.exceptionOrNull()?.message}"
                                )
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                    withContext(ioDispatcher) {
                        if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                            currentHelper.insertOrUpdate(deletedItem)
                        }
                    }
                    withContext(Dispatchers.Main) {
                        if (currentEpoch == sessionEpoch && currentHelper == dbHelper) {
                            _uiState.value = _uiState.value.copy(
                                todos = currentList,
                                errorMessage = "删除失败: ${e.localizedMessage ?: e.message}"
                            )
                        }
                    }
                }
            }
        }
    }

    fun onSessionChanged(serverUrl: String, username: String) {
        activeInitJob?.cancel()
        activeFetchJob?.cancel()
        synchronized(activeWriteJobs) {
            activeWriteJobs.forEach { it.cancel() }
            activeWriteJobs.clear()
        }
        sessionEpoch++
        dbHelper?.close()
        dbHelper = null
        boundServerUrl = ""
        boundUsername = ""
        _uiState.value = TodosUiState(isLoading = true)
        activeInitJob = viewModelScope.launch {
            initDatabaseAndLoad(serverUrl, username)
        }
    }

    fun onLoggedOut() {
        activeInitJob?.cancel()
        activeFetchJob?.cancel()
        synchronized(activeWriteJobs) {
            activeWriteJobs.forEach { it.cancel() }
            activeWriteJobs.clear()
        }
        sessionEpoch++
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
        activeInitJob?.cancel()
        activeFetchJob?.cancel()
        synchronized(activeWriteJobs) {
            activeWriteJobs.forEach { it.cancel() }
            activeWriteJobs.clear()
        }
        dbHelper?.close()
        dbHelper = null
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
