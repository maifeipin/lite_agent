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
import java.security.MessageDigest

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "lite_agent_secure_prefs")

data class RecoveryTaskState(
    val taskId: String,
    val sessionId: String,
    val lastLogCount: Int,
    val timestamp: Long = System.currentTimeMillis()
)

open class SecureTokenStorage(
    protected val context: Context? = null,
    protected val cryptoEngine: CryptoEngine = AndroidKeyStoreCryptoEngine()
) {
    private val gson = Gson()

    protected val requireDataStore: DataStore<Preferences>
        get() = checkNotNull(context) { "Context is required for DataStore operations in production" }.dataStore

    companion object {
        val KEY_ACTIVE_SERVER_URL = stringPreferencesKey("active_server_url")
        const val DEFAULT_SERVER_URL = "http://10.0.2.2:8000"

        fun hashServerUrl(url: String): String {
            val clean = ServerUrlNormalizer.normalize(url)
            val md = MessageDigest.getInstance("MD5")
            return md.digest(clean.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
        }

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
            val enc = prefs[generalTokenKey(activeServer)] ?: ""
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

    val reviewTokenFlow: Flow<String> by lazy {
        requireDataStore.data.map { prefs ->
            val activeServer = prefs[KEY_ACTIVE_SERVER_URL]?.takeIf { it.isNotBlank() } ?: DEFAULT_SERVER_URL
            val enc = prefs[reviewTokenKey(activeServer)] ?: ""
            val expiresAt = prefs[reviewExpiresKey(activeServer)] ?: 0L
            val now = System.currentTimeMillis() / 1000
            if (enc.isNotBlank() && expiresAt > now) {
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

    open suspend fun getActiveServerUrl(): String {
        return activeServerUrlFlow.first()
    }

    open suspend fun getGeneralToken(serverUrl: String? = null): String {
        val targetUrl = serverUrl ?: getActiveServerUrl()
        val prefs = requireDataStore.data.first()
        val enc = prefs[generalTokenKey(targetUrl)] ?: ""
        return if (enc.isNotBlank()) cryptoEngine.decrypt(enc) else ""
    }

    open suspend fun getReviewToken(serverUrl: String? = null): String {
        val targetUrl = serverUrl ?: getActiveServerUrl()
        val prefs = requireDataStore.data.first()
        val enc = prefs[reviewTokenKey(targetUrl)] ?: ""
        val expiresAt = prefs[reviewExpiresKey(targetUrl)] ?: 0L
        val now = System.currentTimeMillis() / 1000
        return if (enc.isNotBlank() && expiresAt > now) cryptoEngine.decrypt(enc) else ""
    }

    open suspend fun setActiveServerUrl(url: String) {
        val cleanUrl = ServerUrlNormalizer.normalize(url)
        requireDataStore.edit { prefs ->
            prefs[KEY_ACTIVE_SERVER_URL] = cleanUrl
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

            val updated = existing.filterNot { it.taskId == taskId } + RecoveryTaskState(
                taskId = taskId,
                sessionId = sessionId,
                lastLogCount = logCount,
                timestamp = System.currentTimeMillis()
            )
            prefs[key] = gson.toJson(updated)
        }
    }

    open suspend fun clearIncompleteTask(serverUrl: String, taskId: String) {
        requireDataStore.edit { prefs ->
            val key = recoveryTasksKey(serverUrl)
            val json = prefs[key] ?: return@edit
            val existing: List<RecoveryTaskState> = try {
                val type = object : TypeToken<List<RecoveryTaskState>>() {}.type
                gson.fromJson(json, type) ?: emptyList()
            } catch (_: Exception) {
                emptyList()
            }

            val updated = existing.filterNot { it.taskId == taskId }
            if (updated.isEmpty()) {
                prefs.remove(key)
            } else {
                prefs[key] = gson.toJson(updated)
            }
        }
    }

    open suspend fun clearAll() {
        requireDataStore.edit { prefs ->
            prefs.clear()
        }
    }
}
