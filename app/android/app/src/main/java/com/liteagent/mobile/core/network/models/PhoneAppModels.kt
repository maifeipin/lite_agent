package com.liteagent.mobile.core.network.models

import com.google.gson.annotations.SerializedName

/**
 * Phone App 自动化受控设备与批次三态筛选模型 (features.phone_app)
 */
data class PhoneDeviceItem(
    @SerializedName("device_id") val deviceId: String,
    @SerializedName("model") val model: String? = null,
    @SerializedName("status") val status: String = "offline",
    @SerializedName("status_text") val statusText: String? = null,
    @SerializedName("offline_reason") val offlineReason: String? = null,
    @SerializedName("test_mode") val testMode: Boolean = false
) {
    val isOnline: Boolean
        get() = status == "online"

    val isPaused: Boolean
        get() = status == "paused"

    val displayName: String
        get() = model?.takeIf { it.isNotBlank() } ?: deviceId
}

data class PhoneDevicesResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("devices") val devices: List<PhoneDeviceItem> = emptyList(),
    @SerializedName("error") val error: String? = null
)

data class PhoneCatalogAppItem(
    @SerializedName("app_id") val appId: String,
    @SerializedName("display_name") val displayName: String,
    @SerializedName("status") val status: String = "validated",
    @SerializedName("disabled_reason") val disabledReason: String? = null
) {
    val isValidated: Boolean
        get() = status == "validated"
}

data class PhoneAppsResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("apps") val apps: List<PhoneCatalogAppItem> = emptyList(),
    @SerializedName("error") val error: String? = null
)

data class PhoneBatchItem(
    @SerializedName("batch_id") val batchId: String,
    @SerializedName("query") val query: String = "",
    @SerializedName("device_id") val deviceId: String = "",
    @SerializedName("app_ids") val appIds: List<String> = emptyList(),
    @SerializedName("status") val status: String = "draft",
    @SerializedName("mode") val mode: String = "fixed",
    @SerializedName("created_at") val createdAt: Double = 0.0,
    @SerializedName("scripts") val scripts: Map<String, Any>? = null
)

data class PhoneBatchesResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("batches") val batches: List<PhoneBatchItem> = emptyList(),
    @SerializedName("error") val error: String? = null
)

data class PhoneBatchSummaryStats(
    @SerializedName("matched_count") val matchedCount: Int = 0,
    @SerializedName("needs_verification_count") val needsVerificationCount: Int = 0,
    @SerializedName("rejected_count") val rejectedCount: Int = 0
)

data class PhoneProductEvaluation(
    @SerializedName("status") val status: String = "unknown",
    @SerializedName("reason") val reason: String? = null
)

data class PhoneProductSalesInfo(
    @SerializedName("raw_text") val rawText: String? = null
)

data class PhoneProductLogisticsInfo(
    @SerializedName("free_shipping") val freeShipping: Boolean? = null
)

data class PhoneProductEvidence(
    @SerializedName("verified_badges") val verifiedBadges: List<String> = emptyList()
)

data class PhoneProductResultItem(
    @SerializedName("title") val title: String = "",
    @SerializedName("amount") val amount: Double = 0.0,
    @SerializedName("raw_price_text") val rawPriceText: String? = null,
    @SerializedName("shop_name") val shopName: String? = null,
    @SerializedName("positive_rate") val positiveRate: Double? = null,
    @SerializedName("sales_info") val salesInfo: PhoneProductSalesInfo? = null,
    @SerializedName("logistics_info") val logisticsInfo: PhoneProductLogisticsInfo? = null,
    @SerializedName("evidence") val evidence: PhoneProductEvidence? = null,
    @SerializedName("evaluations") val evaluations: Map<String, PhoneProductEvaluation>? = null
) {
    val displayPrice: String
        get() = rawPriceText?.takeIf { it.isNotBlank() } ?: "¥${String.format("%.2f", amount)}"
}

data class PhoneGroupedResults(
    @SerializedName("matched") val matched: List<PhoneProductResultItem> = emptyList(),
    @SerializedName("needs_verification") val needsVerification: List<PhoneProductResultItem> = emptyList(),
    @SerializedName("rejected") val rejected: List<PhoneProductResultItem> = emptyList()
)

data class PhoneBatchDetailResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("batch") val batch: PhoneBatchItem? = null,
    @SerializedName("summary") val summary: PhoneBatchSummaryStats? = null,
    @SerializedName("grouped_results") val groupedResults: PhoneGroupedResults? = null,
    @SerializedName("test_mode") val testMode: Boolean = false,
    @SerializedName("error") val error: String? = null
)

data class CreatePhoneBatchRequest(
    @SerializedName("query") val query: String,
    @SerializedName("app_ids") val appIds: List<String> = listOf("jd"),
    @SerializedName("device_id") val deviceId: String = "c68a3cf2",
    @SerializedName("action") val action: String = "search_products",
    @SerializedName("mode") val mode: String = "fixed",
    @SerializedName("filters") val filters: Map<String, Any>? = null,
    @SerializedName("budget") val budget: Map<String, Any>? = null
)

data class CreatePhoneBatchResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("batch_id") val batchId: String? = null,
    @SerializedName("status") val status: String? = null,
    @SerializedName("error") val error: String? = null
)

data class PhoneActionRequest(
    @SerializedName("reason") val reason: String? = null,
    @SerializedName("operator") val operator: String = "admin",
    @SerializedName("device_id") val deviceId: String? = null
)

data class PhoneActionResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("status") val status: String? = null,
    @SerializedName("batch_id") val batchId: String? = null,
    @SerializedName("device_id") val deviceId: String? = null,
    @SerializedName("state") val state: String? = null,
    @SerializedName("error") val error: String? = null
)
