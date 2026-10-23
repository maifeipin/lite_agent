package com.liteagent.mobile.core.network

import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.Response

data class ServerBaseUrlTag(val url: String)

class AuthInterceptor(
    private val tokenStorage: SecureTokenStorage,
    private val onUnauthorized: (() -> Unit)? = null,
    private val boundBaseUrl: String? = null
) : Interceptor {

    companion object {
        const val HEADER_CONFIGURED_BASE_URL = "X-Configured-Base-Url"
    }

    override fun intercept(chain: Interceptor.Chain): Response {
        val original = chain.request()

        // 显式获取绑定的 Base URL（优先级：请求头 > 请求 Tag > 拦截器构造时绑定的 Base URL）
        // 绝不依赖请求路径猜测，确保反向代理前缀（如 /proxy）100% 保留
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

        val token = runBlocking { tokenStorage.getGeneralToken(targetServerUrl) }
        if (token.isNotBlank() && original.header("Authorization") == null) {
            requestBuilder.header("Authorization", "Bearer $token")
        }

        val response = chain.proceed(requestBuilder.build())
        if (response.code == 401) {
            onUnauthorized?.invoke()
        }
        return response
    }
}
