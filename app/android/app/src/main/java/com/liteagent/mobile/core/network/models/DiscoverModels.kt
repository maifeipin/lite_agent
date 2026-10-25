package com.liteagent.mobile.core.network.models

import com.google.gson.annotations.SerializedName

/**
 * 邮件摘要模型 (features.mail_ops)
 */
data class EmailSummaryItem(
    @SerializedName("id") val id: Int,
    @SerializedName("account_name") val accountName: String? = null,
    @SerializedName("uid") val uid: String? = null,
    @SerializedName("sender") val sender: String? = null,
    @SerializedName("subject") val subject: String? = null,
    @SerializedName("email_date") val emailDate: String? = null,
    @SerializedName("importance") val importance: Int? = null,
    @SerializedName("category") val category: String? = null,
    @SerializedName("summary") val summary: String? = null,
    @SerializedName("processed") val processed: Int? = null
)

data class EmailListResponse(
    @SerializedName("success") val success: Boolean,
    @SerializedName("emails") val emails: List<EmailSummaryItem> = emptyList(),
    @SerializedName("error") val error: String? = null
)

/**
 * 账单模型 (features.billing)
 */
data class StatementSummaryItem(
    @SerializedName("id") val id: Int,
    @SerializedName("bank") val bank: String? = null,
    @SerializedName("card_number") val cardNumber: String? = null,
    @SerializedName("due_date") val dueDate: String? = null,
    @SerializedName("amount") val amount: Double = 0.0,
    @SerializedName("currency") val currency: String? = "CNY",
    @SerializedName("paid") val paid: Int = 0,
    @SerializedName("statement_date") val statementDate: String? = null
) {
    val isPaid: Boolean
        get() = paid == 1
}

data class BillingSummaryResponse(
    @SerializedName("success") val success: Boolean,
    @SerializedName("total_statements") val totalStatements: Int = 0,
    @SerializedName("unpaid_count") val unpaidCount: Int = 0,
    @SerializedName("total_unpaid_cny") val totalUnpaidCny: Double = 0.0,
    @SerializedName("recent_statements") val recentStatements: List<StatementSummaryItem> = emptyList(),
    @SerializedName("health_status") val healthStatus: String = "healthy",
    @SerializedName("error") val error: String? = null
)
