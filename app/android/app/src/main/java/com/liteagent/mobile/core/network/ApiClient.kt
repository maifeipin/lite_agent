package com.liteagent.mobile.core.network

import com.google.gson.Gson
import com.liteagent.mobile.core.network.models.*
import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Response
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

open class ApiClient(
    val tokenStorage: SecureTokenStorage,
    val onUnauthorized: (() -> Unit)? = null
) {
    private val gson = Gson()
    private val refreshMutex = Mutex()

    private val baseOkHttpClient: OkHttpClient by lazy {
        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BASIC
        }
        OkHttpClient.Builder()
            .addInterceptor(logging)
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .build()
    }

    val okHttpClient: OkHttpClient by lazy {
        baseOkHttpClient.newBuilder()
            .addInterceptor(
                AuthInterceptor(
                    tokenStorage = tokenStorage,
                    onUnauthorized = onUnauthorized,
                    onUnauthorizedWithContext = { serverUrl, tokenUsed, sessionIdUsed ->
                        // 异步并发合并刷新
                        kotlinx.coroutines.runBlocking {
                            handleUnauthorized(serverUrl, tokenUsed, sessionIdUsed)
                        }
                    }
                )
            )
            .build()
    }

open class AuthenticationException(message: String) : Exception(message)

    private val authRetrofitLock = Any()
    private var cachedAuthRetrofit: Retrofit? = null
    private var cachedAuthBaseUrl: String? = null

    /**
     * 专属认证通道 Retrofit 实例：
     * 不挂载 AuthInterceptor，专供 login / refresh / logout 调用，从根源杜绝递归 401 拦截与 refreshMutex 死锁！
     */
    open fun getAuthApiService(serverUrl: String): ApiService {
        val normalized = ServerUrlNormalizer.normalize(serverUrl)
        synchronized(authRetrofitLock) {
            if (cachedAuthRetrofit == null || cachedAuthBaseUrl != normalized) {
                val rawClient = baseOkHttpClient.newBuilder()
                    .addInterceptor { chain ->
                        val req = chain.request().newBuilder()
                            .header(AuthInterceptor.HEADER_CONFIGURED_BASE_URL, normalized)
                            .tag(ServerBaseUrlTag::class.java, ServerBaseUrlTag(normalized))
                            .build()
                        chain.proceed(req)
                    }
                    .build()

                cachedAuthRetrofit = Retrofit.Builder()
                    .baseUrl(if (normalized.endsWith("/")) normalized else "$normalized/")
                    .client(rawClient)
                    .addConverterFactory(GsonConverterFactory.create())
                    .build()
                cachedAuthBaseUrl = normalized
            }
            return cachedAuthRetrofit!!.create(ApiService::class.java)
        }
    }

    private var currentBaseUrl: String = ""
    private var cachedRetrofit: Retrofit? = null

    open fun getRetrofit(baseUrl: String): Retrofit {
        val normalized = ServerUrlNormalizer.normalize(baseUrl)
        if (cachedRetrofit == null || currentBaseUrl != normalized) {
            currentBaseUrl = normalized

            val boundClient = baseOkHttpClient.newBuilder()
                .addInterceptor { chain ->
                    val req = chain.request().newBuilder()
                        .header(AuthInterceptor.HEADER_CONFIGURED_BASE_URL, normalized)
                        .tag(ServerBaseUrlTag::class.java, ServerBaseUrlTag(normalized))
                        .build()
                    chain.proceed(req)
                }
                .addInterceptor(
                    AuthInterceptor(
                        tokenStorage = tokenStorage,
                        onUnauthorized = onUnauthorized,
                        boundBaseUrl = normalized,
                        onUnauthorizedWithContext = { sUrl, tUsed, sIdUsed ->
                            kotlinx.coroutines.runBlocking {
                                handleUnauthorized(sUrl, tUsed, sIdUsed)
                            }
                        }
                    )
                )
                .build()

            cachedRetrofit = Retrofit.Builder()
                .baseUrl(if (normalized.endsWith("/")) normalized else "$normalized/")
                .client(boundClient)
                .addConverterFactory(GsonConverterFactory.create())
                .build()
        }
        return cachedRetrofit!!
    }

    open suspend fun getApiService(serverUrl: String? = null): ApiService {
        val url = serverUrl ?: tokenStorage.getActiveServerUrl()
        return getRetrofit(url).create(ApiService::class.java)
    }

    /**
     * 统一登录：调用 POST /agent/api/v1/auth，成功后保存会话凭据。
     */
    open suspend fun login(
        serverUrl: String,
        username: String,
        password: String,
        rememberMe: Boolean = true,
        deviceName: String = "OnePlus 9"
    ): Result<LoginResponse> {
        val normalized = ServerUrlNormalizer.normalize(serverUrl)
        return try {
            val service = getAuthApiService(normalized)
            val request = LoginRequest(
                username = username.trim(),
                password = password,
                rememberMe = rememberMe,
                deviceName = deviceName,
                clientType = "android"
            )
            val response = service.login(request)
            if (response.isSuccessful && response.body() != null && response.body()!!.success) {
                val body = response.body()!!
                val token = body.effectiveSessionToken
                val expiresAt = body.effectiveExpiresAt
                val sessionId = body.sessionId ?: ""
                val refreshToken = body.refreshToken
                val refreshExp = body.refreshExpiresAt
                val reviewToken = body.reviewToken
                val reviewExp = body.reviewTokenExpiresAt

                tokenStorage.saveSession(
                    serverUrl = normalized,
                    username = username.trim(),
                    sessionId = sessionId,
                    sessionToken = token,
                    expiresAt = expiresAt,
                    refreshToken = refreshToken,
                    refreshExpiresAt = refreshExp,
                    reviewToken = reviewToken,
                    reviewExpiresAt = reviewExp,
                    rememberMe = rememberMe
                )
                Result.success(body)
            } else {
                val errorMsg = try {
                    val rawErr = response.errorBody()?.string()
                    val parsed = rawErr?.let { gson.fromJson(it, Map::class.java) }
                    parsed?.get("error")?.toString() ?: "登录失败 (${response.code()})"
                } catch (_: Exception) {
                    "登录失败 (${response.code()})"
                }
                Result.failure(Exception(errorMsg))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 会话续期：调用 POST /agent/api/v1/auth/refresh，原子轮换保存新凭证。
     * 全程绑定 expectedSessionId 快照，禁止在刷新后重新读取当前会话作为写回条件，杜绝跨会话串号。
     */
    open suspend fun refresh(
        serverUrl: String,
        refreshToken: String,
        expectedSessionId: String? = null
    ): Result<RefreshResponse> {
        val normalized = ServerUrlNormalizer.normalize(serverUrl)
        return try {
            val targetSessionId = expectedSessionId?.takeIf { it.isNotBlank() }
                ?: tokenStorage.getSession(normalized)?.sessionId
            if (targetSessionId.isNullOrBlank()) {
                return Result.failure(AuthenticationException("无法定位需续期的有效会话快照"))
            }

            val service = getAuthApiService(normalized)
            val response = service.refresh(RefreshRequest(refreshToken.trim()))
            if (response.isSuccessful && response.body() != null && response.body()!!.success) {
                val body = response.body()!!
                val writeSuccess = tokenStorage.updateTokens(
                    serverUrl = normalized,
                    newSessionToken = body.effectiveSessionToken,
                    newExpiresAt = body.effectiveExpiresAt,
                    newRefreshToken = body.refreshToken,
                    newRefreshExpiresAt = body.refreshExpiresAt,
                    newReviewToken = body.reviewToken,
                    newReviewExpiresAt = body.reviewTokenExpiresAt,
                    matchingSessionId = targetSessionId
                )
                if (!writeSuccess) {
                    // 写回失败说明该会话在刷新过程中已被用户注销或已被新会话替换，严禁视为刷新成功
                    Result.failure(AuthenticationException("会话已退出或变更，放弃保存凭证"))
                } else {
                    Result.success(body)
                }
            } else {
                val errorMsg = response.body()?.error ?: "续期失败 (${response.code()})"
                // 明确区分认证拒绝与瞬态网络/服务端错误
                if (response.code() == 401 || (response.code() == 400 && errorMsg.contains("refresh"))) {
                    Result.failure(AuthenticationException(errorMsg))
                } else {
                    Result.failure(Exception(errorMsg))
                }
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 安全退出：撤销服务端统一会话并清理本地加密存储。
     */
    open suspend fun logout(serverUrl: String): Result<Unit> {
        val normalized = ServerUrlNormalizer.normalize(serverUrl)
        return try {
            val session = tokenStorage.getSession(normalized)
            try {
                val service = getAuthApiService(normalized)
                service.logout(
                    LogoutRequest(
                        refreshToken = session?.refreshToken,
                        sessionToken = session?.sessionToken,
                        reviewToken = session?.reviewToken
                    )
                )
            } catch (_: Exception) {
                // 忽略离线或网络异常，确保本地彻底清除
            }
            tokenStorage.clearSession(normalized, matchingSessionId = session?.sessionId)
            Result.success(Unit)
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 并发安全的 401 自动续期合并处理。
     * 严格按请求发起的会话 ID 校验一致性，杜绝跨账号重试；若刷新失败则仅在凭据明确失效时清理，网络故障保留凭据。
     */
    open suspend fun handleUnauthorized(
        serverUrl: String,
        tokenUsed: String,
        sessionIdUsed: String = ""
    ): Boolean {
        return refreshMutex.withLock {
            val currentSession = tokenStorage.getSession(serverUrl)

            // 1. 会话一致性严格校验：
            // 请求必须绑定有效的会话 ID，且必须与当前活跃会话 ID 严格一致。
            // 若当前无活跃会话，或发起请求的会话 ID 缺失/与当前会话不符（如已退出或切换到账号 B），
            // 说明该 401 是旧账号/旧会话的迟到响应，绝对禁止用新会话凭据重发旧请求，且严禁误清新会话！直接丢弃旧请求。
            if (currentSession == null || currentSession.sessionId.isBlank()) {
                return@withLock false
            }
            if (sessionIdUsed.isBlank() || currentSession.sessionId != sessionIdUsed) {
                // 关键防御：会话已改变或未绑定有效会话（如 Alice -> Bob），丢弃旧请求，绝不重试，且严禁误清新账号会话！
                return@withLock false
            }

            // 2. 属于同一会话族内：若当前 token 与触发 401 的 token 不一致，说明已被并发任务刷新过，直接放行重试
            if (currentSession.sessionToken.isNotBlank() && currentSession.sessionToken != tokenUsed) {
                return@withLock true
            }

            // 3. 属于同一会话族且尚未刷新：若存在有效可用的 refresh_token，尝试自动续期
            if (currentSession.canRefresh) {
                val refreshRes = refresh(
                    serverUrl = serverUrl,
                    refreshToken = currentSession.refreshToken!!,
                    expectedSessionId = currentSession.sessionId
                )
                if (refreshRes.isSuccess) {
                    return@withLock true
                }
                val failure = refreshRes.exceptionOrNull()
                // 关键防御：若是网络超时、连接断开或服务端临时 5xx 故障，保留本地会话，绝不误登出！
                if (failure !is AuthenticationException) {
                    return@withLock false
                }
            }

            // 4. 明确凭据失效（被服务端 401 拒绝或无刷新凭据）：在原子事务中精准清理
            tokenStorage.clearSession(
                serverUrl = serverUrl,
                matchingToken = tokenUsed,
                matchingSessionId = currentSession.sessionId
            )
            onUnauthorized?.invoke()
            false
        }
    }

    /**
     * 临时探活测试
     */
    open suspend fun testConnectionDirect(baseUrl: String, token: String): Response<SessionsListResponse> {
        val normalized = ServerUrlNormalizer.normalize(baseUrl)
        val tempClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(10, TimeUnit.SECONDS)
            .addInterceptor { chain ->
                val req = chain.request().newBuilder()
                    .header(AuthInterceptor.HEADER_CONFIGURED_BASE_URL, normalized)
                    .tag(ServerBaseUrlTag::class.java, ServerBaseUrlTag(normalized))
                if (token.isNotBlank()) {
                    req.addHeader("Authorization", "Bearer ${token.trim()}")
                }
                chain.proceed(req.build())
            }
            .build()

        val tempRetrofit = Retrofit.Builder()
            .baseUrl(if (normalized.endsWith("/")) normalized else "$normalized/")
            .client(tempClient)
            .addConverterFactory(GsonConverterFactory.create())
            .build()

        val service = tempRetrofit.create(ApiService::class.java)
        return service.getSessions()
    }
}
