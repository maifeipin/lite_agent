package com.liteagent.mobile.core.network

import com.liteagent.mobile.core.network.models.SessionsListResponse
import com.liteagent.mobile.core.security.SecureTokenStorage
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
            .addInterceptor(AuthInterceptor(tokenStorage, onUnauthorized))
            .build()
    }

    private var currentBaseUrl: String = ""
    private var cachedRetrofit: Retrofit? = null

    open fun getRetrofit(baseUrl: String): Retrofit {
        val normalized = ServerUrlNormalizer.normalize(baseUrl)
        if (cachedRetrofit == null || currentBaseUrl != normalized) {
            currentBaseUrl = normalized

            // 关键修复：显式将配置的规范化 Base URL 注入请求
            // 注入拦截器必须位于 AuthInterceptor 之前，确保 AuthInterceptor 执行时能直接读取
            // 同时在 AuthInterceptor 构造函数中也绑定 boundBaseUrl，双重确保绝不依靠路径猜测
            val boundClient = baseOkHttpClient.newBuilder()
                .addInterceptor { chain ->
                    val req = chain.request().newBuilder()
                        .header(AuthInterceptor.HEADER_CONFIGURED_BASE_URL, normalized)
                        .tag(ServerBaseUrlTag::class.java, ServerBaseUrlTag(normalized))
                        .build()
                    chain.proceed(req)
                }
                .addInterceptor(AuthInterceptor(tokenStorage, onUnauthorized, boundBaseUrl = normalized))
                .build()

            cachedRetrofit = Retrofit.Builder()
                .baseUrl(if (normalized.endsWith("/")) normalized else "$normalized/")
                .client(boundClient)
                .addConverterFactory(GsonConverterFactory.create())
                .build()
        }
        return cachedRetrofit!!
    }

    open suspend fun getApiService(): ApiService {
        val url = tokenStorage.getActiveServerUrl()
        return getRetrofit(url).create(ApiService::class.java)
    }

    /**
     * 临时探活测试：使用纯内存临时客户端，不修改任何持久化凭据与当前活动配置
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
