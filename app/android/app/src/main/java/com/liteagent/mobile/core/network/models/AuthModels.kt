package com.liteagent.mobile.core.network.models

import com.google.gson.annotations.SerializedName

data class LoginRequest(
    @SerializedName("username") val username: String,
    @SerializedName("password") val password: String,
    @SerializedName("remember_me") val rememberMe: Boolean = true,
    @SerializedName("client_type") val clientType: String = "android",
    @SerializedName("device_name") val deviceName: String = "OnePlus 9",
    @SerializedName("device_id") val deviceId: String = ""
)

data class LoginResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("username") val username: String? = null,
    @SerializedName("session_id") val sessionId: String? = null,
    @SerializedName("session_token") val sessionToken: String? = null,
    @SerializedName("access_token") val accessToken: String? = null,
    @SerializedName("expires_at") val expiresAt: Long = 0L,
    @SerializedName("access_token_expires_at") val accessTokenExpiresAt: Long = 0L,
    @SerializedName("refresh_token") val refreshToken: String? = null,
    @SerializedName("refresh_expires_at") val refreshExpiresAt: Long? = null,
    @SerializedName("scopes") val scopes: List<String>? = null,
    @SerializedName("remember_me") val rememberMe: Boolean = false,
    @SerializedName("review_token") val reviewToken: String? = null,
    @SerializedName("review_token_expires_at") val reviewTokenExpiresAt: Long = 0L,
    @SerializedName("scope") val scope: String? = null,
    @SerializedName("error") val error: String? = null
) {
    val effectiveSessionToken: String
        get() = sessionToken?.takeIf { it.isNotBlank() }
            ?: accessToken?.takeIf { it.isNotBlank() }
            ?: ""

    val effectiveExpiresAt: Long
        get() = if (expiresAt > 0L) expiresAt else accessTokenExpiresAt
}

data class RefreshRequest(
    @SerializedName("refresh_token") val refreshToken: String
)

data class RefreshResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("username") val username: String? = null,
    @SerializedName("session_id") val sessionId: String? = null,
    @SerializedName("session_token") val sessionToken: String? = null,
    @SerializedName("access_token") val accessToken: String? = null,
    @SerializedName("expires_at") val expiresAt: Long = 0L,
    @SerializedName("access_token_expires_at") val accessTokenExpiresAt: Long = 0L,
    @SerializedName("refresh_token") val refreshToken: String? = null,
    @SerializedName("refresh_expires_at") val refreshExpiresAt: Long? = null,
    @SerializedName("review_token") val reviewToken: String? = null,
    @SerializedName("review_token_expires_at") val reviewTokenExpiresAt: Long = 0L,
    @SerializedName("error") val error: String? = null
) {
    val effectiveSessionToken: String
        get() = sessionToken?.takeIf { it.isNotBlank() }
            ?: accessToken?.takeIf { it.isNotBlank() }
            ?: ""

    val effectiveExpiresAt: Long
        get() = if (expiresAt > 0L) expiresAt else accessTokenExpiresAt
}

data class LogoutRequest(
    @SerializedName("refresh_token") val refreshToken: String? = null,
    @SerializedName("session_token") val sessionToken: String? = null,
    @SerializedName("review_token") val reviewToken: String? = null
)

data class LogoutResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("message") val message: String? = null,
    @SerializedName("error") val error: String? = null
)
