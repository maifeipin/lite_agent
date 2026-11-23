package com.liteagent.mobile.core.network

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

object ServerUrlNormalizer {

    /**
     * 规范化服务器基址 URL：
     * 1. 自动补全 http:// 前缀（若缺失）；
     * 2. 利用 HttpUrl.Builder 原生输出标准 URL，自动规范处理 IPv6 方括号与标准端口；
     * 3. 移除末尾斜杠；保留反向代理可能配置的路径前缀（不包含尾部斜杠）。
     */
    fun normalize(rawUrl: String): String {
        var input = rawUrl.trim()
        if (input.isEmpty()) return ""
        if (!input.startsWith("http://", ignoreCase = true) && !input.startsWith("https://", ignoreCase = true)) {
            input = "http://$input"
        }

        val parsed = input.toHttpUrlOrNull() ?: return input.removeSuffix("/").lowercase()
        return extractBaseUrl(parsed)
    }

    /**
     * 从 HttpUrl 中提取规范化 Base URL，全程使用 HttpUrl.Builder，避免手动拼接导致 IPv6 丢失方括号
     */
    fun extractBaseUrl(url: HttpUrl): String {
        val cleanPath = url.encodedPath.removeSuffix("/")
        val builder = url.newBuilder()
            .query(null)
            .fragment(null)

        // 若传入包含具体接口的完整 URL（如 /proxy/agent/api/v1/chat），保留配置的反向代理前缀（如 /proxy）
        val basePath = if (cleanPath.contains("/agent/api/v1") || cleanPath.contains("/api/v1")) {
            val idx = cleanPath.indexOf("/agent/api/v1").takeIf { it >= 0 }
                ?: cleanPath.indexOf("/api/v1")
            cleanPath.substring(0, idx)
        } else {
            cleanPath
        }

        builder.encodedPath(if (basePath.isEmpty()) "/" else basePath)
        // HttpUrl.toString() 会自动保留 IPv6 方括号 [::1]，并自动省略默认端口 80/443
        val fullUrl = builder.build().toString()
        return fullUrl.removeSuffix("/")
    }
}
