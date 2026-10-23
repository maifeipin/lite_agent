package com.liteagent.mobile.core.network

import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.Response

data class ServerBaseUrlTag(val url: String)
data class SessionIdTag(val sessionId: String)

class AuthInterceptor(
    private val tokenStorage: SecureTokenStorage,
    private val onUnauthorized: (() -> Unit)? = null,
    private val boundBaseUrl: String? = null,
    private val onUnauthorizedWithContext: ((serverUrl: String, tokenUsed: String, sessionIdUsed: String) -> Boolean)? = null
) : Interceptor {

    companion object {
        const val HEADER_CONFIGURED_BASE_URL = "X-Configured-Base-Url"

        fun isAuthPath(path: String): Boolean {
            return path.endsWith("/auth") ||
                   path.endsWith("/auth/refresh") ||
                   path.endsWith("/auth/logout") ||
                   path.contains("/api/v1/auth")
        }
    }

    override fun intercept(chain: Interceptor.Chain): Response {
        val original = chain.request()

        // 显式获取绑定的 Base URL（优先级：请求头 > 请求 Tag > 拦截器构造时绑定的 Base URL）
        val explicitBase = original.header(HEADER_CONFIGURED_BASE_URL)
            ?: original.tag(ServerBaseUrlTag::class.java)?.url
            ?: boundBaseUrl

        val targetServerUrl = if (!explicitBase.isNullOrBlank()) {
            ServerUrlNormalizer.normalize(explicitBase)
        } else {
            ServerUrlNormalizer.extractBaseUrl(original.url)
        }

        // 移除内部专用的私有 Base URL 头，避免发送给远端服务器
        val requestBuilder = original.newBuilder().removeHeader(HEADER_CONFIGURED_BASE_URL)

        val session = runBlocking { tokenStorage.getSession(targetServerUrl) }
        val token = session?.sessionToken ?: ""
        val currentSessionId = session?.sessionId ?: ""

        val boundSessionId = original.tag(SessionIdTag::class.java)?.sessionId ?: currentSessionId

        if (token.isNotBlank() && original.header("Authorization") == null) {
            requestBuilder.header("Authorization", "Bearer $token")
        }
        if (boundSessionId.isNotBlank() && original.tag(SessionIdTag::class.java) == null) {
            requestBuilder.tag(SessionIdTag::class.java, SessionIdTag(boundSessionId))
        }

        val executedRequest = requestBuilder.build()
        val response = chain.proceed(executedRequest)

        // 401 Unauthorized 处理：若非认证端点且存在已用凭据，触发无感刷新与自动重放
        if (response.code == 401 && !isAuthPath(original.url.encodedPath)) {
            val tokenUsed = executedRequest.header("Authorization")?.removePrefix("Bearer ")?.trim() ?: token
            val sessionUsed = executedRequest.tag(SessionIdTag::class.java)?.sessionId ?: boundSessionId
            val refreshed = if (tokenUsed.isNotBlank() && sessionUsed.isNotBlank()) {
                onUnauthorizedWithContext?.invoke(targetServerUrl, tokenUsed, sessionUsed) ?: false
            } else {
                false
            }

            if (refreshed) {
                val refreshedSession = runBlocking { tokenStorage.getSession(targetServerUrl) }
                val newToken = refreshedSession?.sessionToken ?: ""
                val newSessionId = refreshedSession?.sessionId ?: ""

                // 关键防御：重试前严格核验当前会话与发起请求时的会话一致；确定重试时才关闭旧响应并重发
                if (newToken.isNotBlank() && sessionUsed.isNotBlank() && newSessionId == sessionUsed) {
                    response.close()
                    val retryReq = original.newBuilder()
                        .removeHeader(HEADER_CONFIGURED_BASE_URL)
                        .header("Authorization", "Bearer $newToken")
                        .tag(SessionIdTag::class.java, SessionIdTag(newSessionId))
                        .build()
                    return chain.proceed(retryReq)
                }
            } else {
                if (onUnauthorizedWithContext == null) {
                    onUnauthorized?.invoke()
                }
            }
        }
        return response
    }
}
