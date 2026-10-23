package com.liteagent.mobile.core.security

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.liteagent.mobile.core.network.ServerUrlNormalizer
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "lite_agent_secure_prefs")

data class RecoveryTaskState(
    val taskId: String,
    val sessionId: String,
    val lastLogCount: Int,
    val timestamp: Long = System.currentTimeMillis()
)

data class UserSession(
    val serverUrl: String,
    val username: String,
    val sessionId: String,
    val sessionToken: String,
    val expiresAt: Long,
    val refreshToken: String? = null,
    val refreshExpiresAt: Long = 0L,
    val reviewToken: String? = null,
    val reviewExpiresAt: Long = 0L,
    val rememberMe: Boolean = false
) {
    val isSessionValid: Boolean
        get() = sessionToken.isNotBlank() && (expiresAt > System.currentTimeMillis() / 1000)

    val canRefresh: Boolean
        get() = !refreshToken.isNullOrBlank() && (refreshExpiresAt > System.currentTimeMillis() / 1000)
}

open class SecureTokenStorage(
    protected val context: Context? = null,
    protected val cryptoEngine: CryptoEngine = AndroidKeyStoreCryptoEngine()
) {
    private val gson = Gson()
    protected val inMemorySessions = ConcurrentHashMap<String, UserSession>()
    protected val sessionMutex = Mutex()

    protected val requireDataStore: DataStore<Preferences>
        get() = checkNotNull(context) { "Context is required for DataStore operations in production" }.dataStore

    companion object {
        val KEY_ACTIVE_SERVER_URL = stringPreferencesKey("active_server_url")
        const val DEFAULT_SERVER_URL = "https://mail.maifeipin.com/"

        fun hashServerUrl(url: String): String {
            val clean = ServerUrlNormalizer.normalize(url)
            val md = MessageDigest.getInstance("MD5")
            return md.digest(clean.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
        }

        fun usernameKey(serverUrl: String) = stringPreferencesKey("username_${hashServerUrl(serverUrl)}")
        fun sessionIdKey(serverUrl: String) = stringPreferencesKey("session_id_${hashServerUrl(serverUrl)}")
        fun sessionTokenKey(serverUrl: String) = stringPreferencesKey("session_token_${hashServerUrl(serverUrl)}")
        fun sessionExpiresKey(serverUrl: String) = longPreferencesKey("session_expires_${hashServerUrl(serverUrl)}")
        fun refreshTokenKey(serverUrl: String) = stringPreferencesKey("refresh_token_${hashServerUrl(serverUrl)}")
        fun refreshExpiresKey(serverUrl: String) = longPreferencesKey("refresh_expires_${hashServerUrl(serverUrl)}")
        fun rememberMeKey(serverUrl: String) = booleanPreferencesKey("remember_me_${hashServerUrl(serverUrl)}")

        fun generalTokenKey(serverUrl: String) = stringPreferencesKey("token_${hashServerUrl(serverUrl)}")
        fun reviewTokenKey(serverUrl: String) = stringPreferencesKey("review_token_${hashServerUrl(serverUrl)}")
        fun reviewExpiresKey(serverUrl: String) = longPreferencesKey("review_expires_${hashServerUrl(serverUrl)}")
        fun recoveryTasksKey(serverUrl: String) = stringPreferencesKey("recovery_tasks_json_${hashServerUrl(serverUrl)}")
    }

    val activeServerUrlFlow: Flow<String> by lazy {
        requireDataStore.data.map { prefs ->
            prefs[KEY_ACTIVE_SERVER_URL]?.takeIf { it.isNotBlank() } ?: DEFAULT_SERVER_URL
        }
    }

    val generalTokenFlow: Flow<String> by lazy {
        requireDataStore.data.map { prefs ->
            val activeServer = prefs[KEY_ACTIVE_SERVER_URL]?.takeIf { it.isNotBlank() } ?: DEFAULT_SERVER_URL
            val mem = inMemorySessions[activeServer]
            if (mem != null && !mem.rememberMe && mem.sessionToken.isNotBlank()) {
                return@map mem.sessionToken
            }
            val enc = prefs[sessionTokenKey(activeServer)] ?: prefs[generalTokenKey(activeServer)] ?: ""
            if (enc.isNotBlank()) {
                try {
                    cryptoEngine.decrypt(enc)
                } catch (_: SecurityException) {
                    ""
                }
            } else {
                ""
            }
        }
    }

    val sessionFlow: Flow<UserSession?> by lazy {
        requireDataStore.data.map { prefs ->
            val activeServer = prefs[KEY_ACTIVE_SERVER_URL]?.takeIf { it.isNotBlank() } ?: DEFAULT_SERVER_URL
            val mem = inMemorySessions[activeServer]
            if (mem != null && !mem.rememberMe) {
                return@map mem
            }
            val username = prefs[usernameKey(activeServer)] ?: ""
            val sessionId = prefs[sessionIdKey(activeServer)] ?: ""
            val encToken = prefs[sessionTokenKey(activeServer)] ?: prefs[generalTokenKey(activeServer)] ?: ""
            val expiresAt = prefs[sessionExpiresKey(activeServer)] ?: 0L
            val encRefresh = prefs[refreshTokenKey(activeServer)] ?: ""
            val refreshExpiresAt = prefs[refreshExpiresKey(activeServer)] ?: 0L
            val encReview = prefs[reviewTokenKey(activeServer)] ?: ""
            val reviewExpiresAt = prefs[reviewExpiresKey(activeServer)] ?: 0L
            val rememberMe = prefs[rememberMeKey(activeServer)] ?: false

            val sessionToken = if (encToken.isNotBlank()) {
                try { cryptoEngine.decrypt(encToken) } catch (_: Exception) { "" }
            } else ""

            if (sessionToken.isBlank() && encRefresh.isBlank()) {
                null
            } else {
                val refreshToken = if (encRefresh.isNotBlank()) {
                    try { cryptoEngine.decrypt(encRefresh) } catch (_: Exception) { null }
                } else null
                val reviewToken = if (encReview.isNotBlank()) {
                    try { cryptoEngine.decrypt(encReview) } catch (_: Exception) { null }
                } else null

                UserSession(
                    serverUrl = activeServer,
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
            }
        }
    }

    open suspend fun getActiveServerUrl(): String {
        return activeServerUrlFlow.first()
    }

    open suspend fun getSession(serverUrl: String? = null): UserSession? {
        val targetUrl = serverUrl ?: getActiveServerUrl()
        val mem = inMemorySessions[targetUrl]
        if (mem != null && !mem.rememberMe) {
            return mem
        }
        val prefs = requireDataStore.data.first()
        val username = prefs[usernameKey(targetUrl)] ?: ""
        val sessionId = prefs[sessionIdKey(targetUrl)] ?: ""
        val encToken = prefs[sessionTokenKey(targetUrl)] ?: prefs[generalTokenKey(targetUrl)] ?: ""
        val expiresAt = prefs[sessionExpiresKey(targetUrl)] ?: 0L
        val encRefresh = prefs[refreshTokenKey(targetUrl)] ?: ""
        val refreshExpiresAt = prefs[refreshExpiresKey(targetUrl)] ?: 0L
        val encReview = prefs[reviewTokenKey(targetUrl)] ?: ""
        val reviewExpiresAt = prefs[reviewExpiresKey(targetUrl)] ?: 0L
        val rememberMe = prefs[rememberMeKey(targetUrl)] ?: false

        val sessionToken = if (encToken.isNotBlank()) {
            try { cryptoEngine.decrypt(encToken) } catch (_: Exception) { "" }
        } else ""

        if (sessionToken.isBlank() && encRefresh.isBlank()) return null

        val refreshToken = if (encRefresh.isNotBlank()) {
            try { cryptoEngine.decrypt(encRefresh) } catch (_: Exception) { null }
        } else null

        val reviewToken = if (encReview.isNotBlank()) {
            try { cryptoEngine.decrypt(encReview) } catch (_: Exception) { null }
        } else null

        return UserSession(
            serverUrl = targetUrl,
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
    }

    open suspend fun hasValidSession(serverUrl: String? = null): Boolean {
        val session = getSession(serverUrl) ?: return false
        return session.isSessionValid || session.canRefresh
    }

    open suspend fun getGeneralToken(serverUrl: String? = null): String {
        val targetUrl = serverUrl ?: getActiveServerUrl()
        val mem = inMemorySessions[targetUrl]
        if (mem != null && !mem.rememberMe && mem.sessionToken.isNotBlank()) {
            return mem.sessionToken
        }
        val prefs = requireDataStore.data.first()
        val enc = prefs[sessionTokenKey(targetUrl)] ?: prefs[generalTokenKey(targetUrl)] ?: ""
        return if (enc.isNotBlank()) cryptoEngine.decrypt(enc) else ""
    }

    open suspend fun getReviewToken(serverUrl: String? = null): String {
        val targetUrl = serverUrl ?: getActiveServerUrl()
        val mem = inMemorySessions[targetUrl]
        val now = System.currentTimeMillis() / 1000
        if (mem != null && !mem.rememberMe && !mem.reviewToken.isNullOrBlank() && mem.reviewExpiresAt > now) {
            return mem.reviewToken
        }
        val prefs = requireDataStore.data.first()
        val enc = prefs[reviewTokenKey(targetUrl)] ?: ""
        val expiresAt = prefs[reviewExpiresKey(targetUrl)] ?: 0L
        return if (enc.isNotBlank() && expiresAt > now) cryptoEngine.decrypt(enc) else ""
    }

    open suspend fun setActiveServerUrl(url: String) {
        val cleanUrl = ServerUrlNormalizer.normalize(url)
        requireDataStore.edit { prefs ->
            prefs[KEY_ACTIVE_SERVER_URL] = cleanUrl
        }
    }

    @Throws(SecurityException::class)
    open suspend fun saveSession(session: UserSession) {
        saveSession(
            serverUrl = session.serverUrl,
            username = session.username,
            sessionId = session.sessionId,
            sessionToken = session.sessionToken,
            expiresAt = session.expiresAt,
            refreshToken = session.refreshToken,
            refreshExpiresAt = session.refreshExpiresAt,
            reviewToken = session.reviewToken,
            reviewExpiresAt = session.reviewExpiresAt,
            rememberMe = session.rememberMe
        )
    }

    @Throws(SecurityException::class)
    open suspend fun saveSession(
        serverUrl: String,
        username: String,
        sessionId: String,
        sessionToken: String,
        expiresAt: Long,
        refreshToken: String? = null,
        refreshExpiresAt: Long = 0L,
        reviewToken: String? = null,
        reviewExpiresAt: Long = 0L,
        rememberMe: Boolean = true
    ) {
        val cleanUrl = ServerUrlNormalizer.normalize(serverUrl)

        sessionMutex.withLock {
            if (!rememberMe) {
                // “不记住登录”：原子切换，仅保存在当前运行进程内存中，杀进程后自动失效，严禁落盘！
                inMemorySessions[cleanUrl] = UserSession(
                    serverUrl = cleanUrl,
                    username = username.trim(),
                    sessionId = sessionId.trim(),
                    sessionToken = sessionToken.trim(),
                    expiresAt = expiresAt,
                    refreshToken = null,
                    refreshExpiresAt = 0L,
                    reviewToken = reviewToken?.trim(),
                    reviewExpiresAt = reviewExpiresAt,
                    rememberMe = false
                )
                // 确保磁盘上不残留凭据，只保留活跃服务地址以供上下文读取
                requireDataStore.edit { prefs ->
                    prefs[KEY_ACTIVE_SERVER_URL] = cleanUrl
                    prefs.remove(usernameKey(cleanUrl))
                    prefs.remove(sessionIdKey(cleanUrl))
                    prefs.remove(sessionTokenKey(cleanUrl))
                    prefs.remove(sessionExpiresKey(cleanUrl))
                    prefs.remove(refreshTokenKey(cleanUrl))
                    prefs.remove(refreshExpiresKey(cleanUrl))
                    prefs.remove(reviewTokenKey(cleanUrl))
                    prefs.remove(reviewExpiresKey(cleanUrl))
                    prefs.remove(generalTokenKey(cleanUrl))
                    prefs[rememberMeKey(cleanUrl)] = false
                }
                return@withLock
            }

            // “记住登录”：原子切换，清除临时内存态，经硬件 Keystore 加密后安全持久化至 DataStore
            inMemorySessions.remove(cleanUrl)
            val encToken = if (sessionToken.isNotBlank()) cryptoEngine.encrypt(sessionToken.trim()) else ""
            val encRefresh = if (!refreshToken.isNullOrBlank()) cryptoEngine.encrypt(refreshToken.trim()) else ""
            val encReview = if (!reviewToken.isNullOrBlank()) cryptoEngine.encrypt(reviewToken.trim()) else ""

            requireDataStore.edit { prefs ->
                prefs[KEY_ACTIVE_SERVER_URL] = cleanUrl
                prefs[usernameKey(cleanUrl)] = username.trim()
                prefs[sessionIdKey(cleanUrl)] = sessionId.trim()
                if (encToken.isNotBlank()) {
                    prefs[sessionTokenKey(cleanUrl)] = encToken
                    prefs[sessionExpiresKey(cleanUrl)] = expiresAt
                    prefs[generalTokenKey(cleanUrl)] = encToken
                } else {
                    prefs.remove(sessionTokenKey(cleanUrl))
                    prefs.remove(sessionExpiresKey(cleanUrl))
                    prefs.remove(generalTokenKey(cleanUrl))
                }
                if (encRefresh.isNotBlank()) {
                    prefs[refreshTokenKey(cleanUrl)] = encRefresh
                    prefs[refreshExpiresKey(cleanUrl)] = refreshExpiresAt
                } else {
                    prefs.remove(refreshTokenKey(cleanUrl))
                    prefs.remove(refreshExpiresKey(cleanUrl))
                }
                prefs[rememberMeKey(cleanUrl)] = true
                if (encReview.isNotBlank()) {
                    prefs[reviewTokenKey(cleanUrl)] = encReview
                    prefs[reviewExpiresKey(cleanUrl)] = reviewExpiresAt
                } else {
                    prefs.remove(reviewTokenKey(cleanUrl))
                    prefs.remove(reviewExpiresKey(cleanUrl))
                }
            }
        }
    }

    @Throws(SecurityException::class)
    open suspend fun updateTokens(
        serverUrl: String,
        newSessionToken: String,
        newExpiresAt: Long,
        newRefreshToken: String? = null,
        newRefreshExpiresAt: Long = 0L,
        newReviewToken: String? = null,
        newReviewExpiresAt: Long = 0L,
        matchingSessionId: String? = null
    ): Boolean {
        val cleanUrl = ServerUrlNormalizer.normalize(serverUrl)

        // 要求当前会话存在且 ID 严格匹配；缺失也必须拒绝。
        if (matchingSessionId.isNullOrBlank()) {
            return false
        }

        return sessionMutex.withLock {
            // 1. 若为内存临时会话，使用 compute 原子条件更新内存会话
            var updatedInMemory = false
            inMemorySessions.compute(cleanUrl) { _, mem ->
                if (mem != null) {
                    if (mem.sessionId.isNotBlank() && mem.sessionId == matchingSessionId) {
                        updatedInMemory = true
                        mem.copy(
                            sessionToken = newSessionToken.trim(),
                            expiresAt = newExpiresAt,
                            refreshToken = newRefreshToken?.trim() ?: mem.refreshToken,
                            refreshExpiresAt = if (newRefreshToken != null) newRefreshExpiresAt else mem.refreshExpiresAt,
                            reviewToken = newReviewToken?.trim() ?: mem.reviewToken,
                            reviewExpiresAt = if (newReviewToken != null) newReviewExpiresAt else mem.reviewExpiresAt
                        )
                    } else {
                        mem
                    }
                } else {
                    null
                }
            }
            if (updatedInMemory) {
                return@withLock true
            }

            // 2. 磁盘会话更新：在 sessionMutex 与 DataStore 原子事务内部比对 matchingSessionId，防止并发覆盖新登录或退出后复活
            var updatedOnDisk = false
            val encToken = if (newSessionToken.isNotBlank()) cryptoEngine.encrypt(newSessionToken.trim()) else ""
            val encRefresh = if (!newRefreshToken.isNullOrBlank()) cryptoEngine.encrypt(newRefreshToken.trim()) else ""
            val encReview = if (!newReviewToken.isNullOrBlank()) cryptoEngine.encrypt(newReviewToken.trim()) else ""

            requireDataStore.edit { prefs ->
                val currentSessionId = prefs[sessionIdKey(cleanUrl)] ?: ""
                // 严格校验：
                // 1) 当前存储必须存在活跃会话（currentSessionId 非空）；若用户已登出，currentSessionId 为空，严禁复活旧凭据！
                // 2) matchingSessionId 必须与 currentSessionId 完全相等；若缺失或已登录其他账号，严禁覆盖！
                if (currentSessionId.isBlank() || currentSessionId != matchingSessionId) {
                    return@edit
                }

                if (encToken.isNotBlank()) {
                    prefs[sessionTokenKey(cleanUrl)] = encToken
                    prefs[sessionExpiresKey(cleanUrl)] = newExpiresAt
                    prefs[generalTokenKey(cleanUrl)] = encToken
                }
                if (encRefresh.isNotBlank()) {
                    prefs[refreshTokenKey(cleanUrl)] = encRefresh
                    prefs[refreshExpiresKey(cleanUrl)] = newRefreshExpiresAt
                }
                if (encReview.isNotBlank()) {
                    prefs[reviewTokenKey(cleanUrl)] = encReview
                    prefs[reviewExpiresKey(cleanUrl)] = newReviewExpiresAt
                }
                updatedOnDisk = true
            }
            updatedOnDisk
        }
    }

    /**
     * 清理会话。
     * 当 matchingToken 或 matchingSessionId 指定时，在原子事务内部进行比对；
     * 仅当匹配时才清理，避免旧请求迟到的 401 误清用户刚建立的新会话。
     */
    open suspend fun clearSession(
        serverUrl: String,
        matchingToken: String? = null,
        matchingSessionId: String? = null
    ): Boolean {
        val cleanUrl = ServerUrlNormalizer.normalize(serverUrl)
        return sessionMutex.withLock {
            var cleared = false

            // 1. 同步原子清理内存会话（使用 compute 实现条件原子移除）
            inMemorySessions.compute(cleanUrl) { _, mem ->
                if (mem != null) {
                    val matchesToken = matchingToken == null || mem.sessionToken == matchingToken
                    val matchesSessionId = matchingSessionId == null || mem.sessionId == matchingSessionId
                    if (matchesToken && matchesSessionId) {
                        cleared = true
                        null // 原子移除
                    } else {
                        mem // 保留不匹配的会话
                    }
                } else {
                    null
                }
            }

            // 2. DataStore 原子事务中完成比对与清除，彻底消除 TOCTOU 竞态漏洞
            requireDataStore.edit { editPrefs ->
                if (matchingSessionId != null) {
                    val currentSessionId = editPrefs[sessionIdKey(cleanUrl)] ?: ""
                    if (currentSessionId.isNotBlank() && currentSessionId != matchingSessionId) {
                        return@edit // 会话 ID 已变动，保留新登录
                    }
                }
                if (matchingToken != null) {
                    val enc = editPrefs[sessionTokenKey(cleanUrl)] ?: editPrefs[generalTokenKey(cleanUrl)] ?: ""
                    val currentToken = if (enc.isNotBlank()) {
                        try { cryptoEngine.decrypt(enc) } catch (_: Exception) { "" }
                    } else ""
                    if (currentToken.isNotBlank() && currentToken != matchingToken) {
                        return@edit // Token 已变动，保留新登录
                    }
                }

                editPrefs.remove(usernameKey(cleanUrl))
                editPrefs.remove(sessionIdKey(cleanUrl))
                editPrefs.remove(sessionTokenKey(cleanUrl))
                editPrefs.remove(sessionExpiresKey(cleanUrl))
                editPrefs.remove(refreshTokenKey(cleanUrl))
                editPrefs.remove(refreshExpiresKey(cleanUrl))
                editPrefs.remove(reviewTokenKey(cleanUrl))
                editPrefs.remove(reviewExpiresKey(cleanUrl))
                editPrefs.remove(generalTokenKey(cleanUrl))
                editPrefs.remove(rememberMeKey(cleanUrl))
                cleared = true
            }
            cleared
        }
    }

    @Throws(SecurityException::class)
    open suspend fun setGeneralToken(serverUrl: String, token: String) {
        val trimmed = token.trim()
        val encrypted = if (trimmed.isNotBlank()) cryptoEngine.encrypt(trimmed) else ""
        requireDataStore.edit { prefs ->
            val key = generalTokenKey(serverUrl)
            if (encrypted.isNotBlank()) {
                prefs[key] = encrypted
            } else {
                prefs.remove(key)
            }
        }
    }

    @Throws(SecurityException::class)
    open suspend fun setReviewMeetingToken(serverUrl: String, token: String, ttlSeconds: Long = 3600) {
        val trimmed = token.trim()
        val encrypted = if (trimmed.isNotBlank()) cryptoEngine.encrypt(trimmed) else ""
        val expiresAt = (System.currentTimeMillis() / 1000) + ttlSeconds
        requireDataStore.edit { prefs ->
            val tokenKey = reviewTokenKey(serverUrl)
            val expKey = reviewExpiresKey(serverUrl)
            if (encrypted.isNotBlank()) {
                prefs[tokenKey] = encrypted
                prefs[expKey] = expiresAt
            } else {
                prefs.remove(tokenKey)
                prefs.remove(expKey)
            }
        }
    }

    open suspend fun clearReviewMeetingToken(serverUrl: String) {
        requireDataStore.edit { prefs ->
            prefs.remove(reviewTokenKey(serverUrl))
            prefs.remove(reviewExpiresKey(serverUrl))
        }
    }

    open suspend fun getIncompleteTasks(serverUrl: String): List<RecoveryTaskState> {
        val prefs = requireDataStore.data.first()
        val json = prefs[recoveryTasksKey(serverUrl)] ?: return emptyList()
        return try {
            val type = object : TypeToken<List<RecoveryTaskState>>() {}.type
            val list: List<RecoveryTaskState> = gson.fromJson(json, type) ?: emptyList()
            list.sortedByDescending { it.timestamp }
        } catch (_: Exception) {
            emptyList()
        }
    }

    open suspend fun getIncompleteTask(serverUrl: String, taskId: String): RecoveryTaskState? {
        return getIncompleteTasks(serverUrl).find { it.taskId == taskId }
    }

    open suspend fun saveIncompleteTask(serverUrl: String, taskId: String, sessionId: String, logCount: Int) {
        requireDataStore.edit { prefs ->
            val key = recoveryTasksKey(serverUrl)
            val existing = try {
                val json = prefs[key]
                if (json != null) {
                    val type = object : TypeToken<List<RecoveryTaskState>>() {}.type
                    gson.fromJson<List<RecoveryTaskState>>(json, type) ?: emptyList()
                } else emptyList()
            } catch (_: Exception) {
                emptyList()
            }
            val filtered = existing.filter { it.taskId != taskId }
            val updated = filtered + RecoveryTaskState(taskId, sessionId, logCount)
            prefs[key] = gson.toJson(updated)
        }
    }

    open suspend fun removeIncompleteTask(serverUrl: String, taskId: String) {
        requireDataStore.edit { prefs ->
            val key = recoveryTasksKey(serverUrl)
            val existing = try {
                val json = prefs[key]
                if (json != null) {
                    val type = object : TypeToken<List<RecoveryTaskState>>() {}.type
                    gson.fromJson<List<RecoveryTaskState>>(json, type) ?: emptyList()
                } else emptyList()
            } catch (_: Exception) {
                emptyList()
            }
            val updated = existing.filter { it.taskId != taskId }
            if (updated.isEmpty()) {
                prefs.remove(key)
            } else {
                prefs[key] = gson.toJson(updated)
            }
        }
    }

    open suspend fun clearIncompleteTask(serverUrl: String, taskId: String) {
        removeIncompleteTask(serverUrl, taskId)
    }

    open suspend fun clearAll() {
        sessionMutex.withLock {
            inMemorySessions.clear()
            requireDataStore.edit { prefs ->
                prefs.clear()
            }
        }
    }
}
