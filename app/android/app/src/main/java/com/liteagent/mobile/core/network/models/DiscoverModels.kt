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

/**
 * 今日 RSS 智能简报模型 (features.content_ops)
 */
data class RssBriefTopicItem(
    @SerializedName("topic") val topic: String = "",
    @SerializedName("sentiment") val sentiment: String = "中",
    @SerializedName("analysis") val analysis: String = ""
)

data class RssBriefResponse(
    @SerializedName("date") val date: String? = null,
    @SerializedName("summary") val summary: String? = null,
    @SerializedName("topics") val topics: List<RssBriefTopicItem> = emptyList(),
    @SerializedName("detail") val detail: String? = null
)

/**
 * 复杂任务编排模型 (core.task_spec)
 */
data class TaskSpecTaskDef(
    @SerializedName("name") val name: String? = null,
    @SerializedName("objective") val objective: String? = null,
    @SerializedName("context") val context: String? = null
)

data class TaskSpecModelPolicyDef(
    @SerializedName("preferred_model") val preferredModel: String? = null,
    @SerializedName("recommended_tier") val recommendedTier: String? = null
)

data class TaskSpecScheduleDef(
    @SerializedName("mode") val mode: String? = "manual",
    @SerializedName("cron") val cron: String? = null,
    @SerializedName("run_at") val runAt: String? = null
)

data class TaskSpecBudgetDef(
    @SerializedName("max_total_tokens") val maxTotalTokens: Int = 0,
    @SerializedName("max_steps") val maxSteps: Int = 0,
    @SerializedName("max_wall_seconds") val maxWallSeconds: Int = 900
)

data class TaskSpecExecutionDef(
    @SerializedName("complexity") val complexity: String? = "standard",
    @SerializedName("model_policy") val modelPolicy: TaskSpecModelPolicyDef? = null,
    @SerializedName("schedule") val schedule: TaskSpecScheduleDef? = null,
    @SerializedName("budget") val budget: TaskSpecBudgetDef? = null
)

data class TaskSpecInner(
    @SerializedName("task") val task: TaskSpecTaskDef? = null,
    @SerializedName("execution") val execution: TaskSpecExecutionDef? = null
)

data class TaskSpecItem(
    @SerializedName("id") val id: String,
    @SerializedName("name") val name: String? = null,
    @SerializedName("status") val status: String = "draft",
    @SerializedName("enabled") val enabled: Boolean = false,
    @SerializedName("last_run_status") val lastRunStatus: String? = null,
    @SerializedName("last_run_at") val lastRunAt: String? = null,
    @SerializedName("last_run_result") val lastRunResult: String? = null,
    @SerializedName("spec") val spec: TaskSpecInner? = null
) {
    val displayTitle: String
        get() = name?.takeIf { it.isNotBlank() }
            ?: spec?.task?.name?.takeIf { it.isNotBlank() }
            ?: "任务 #$id"

    val displayObjective: String
        get() = spec?.task?.objective?.takeIf { it.isNotBlank() } ?: ""

    val canRun: Boolean
        get() = status == "approved" && lastRunStatus != "running"
}

data class TaskSpecsListResponse(
    @SerializedName("data") val data: List<TaskSpecItem> = emptyList(),
    @SerializedName("total") val total: Int = 0
)

data class TaskSpecRunResponse(
    @SerializedName("status") val status: String? = null,
    @SerializedName("task_id") val taskId: String? = null,
    @SerializedName("error") val error: String? = null
)

/**
 * Socks5 代理节点模型 (features.network_ops)
 */
data class Socks5NodeItem(
    @SerializedName("id") val id: Int = 0,
    @SerializedName("servername") val servername: String = "",
    @SerializedName("host") val host: String = "",
    @SerializedName("runcmd") val runcmd: String? = null,
    @SerializedName("clientproxy") val clientproxy: String? = null,
    @SerializedName("memo") val memo: String? = null,
    @SerializedName("is_active") val isActive: Int = 0
) {
    val active: Boolean
        get() = isActive == 1

    val isNaive: Boolean
        get() {
            val cmd = (runcmd ?: "").lowercase()
            return cmd.contains("naive") || cmd.contains("--proxy=") || cmd.isBlank()
        }
}

data class Socks5ListResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("data") val data: List<Socks5NodeItem> = emptyList()
)

data class Socks5TestResultDetail(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("latency_ms") val latencyMs: Double? = null,
    @SerializedName("message") val message: String? = null,
    @SerializedName("error") val error: String? = null,
    @SerializedName("ip") val ip: String? = null
)

data class Socks5TestResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("result") val result: Socks5TestResultDetail? = null
)

data class Socks5SetActiveRequest(
    @SerializedName("id") val id: Int
)

data class Socks5SetActiveResponse(
    @SerializedName("success") val success: Boolean = false,
    @SerializedName("message") val message: String? = null
)
