package com.liteagent.mobile.core.storage

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.liteagent.mobile.core.network.models.TodoItem

/**
 * 本地 SQLite 沙箱存储：按规范化 (serverUrl + "_" + username) 的哈希独立建库，
 * 实现零网络依赖的待办离线秒开与即时乐观更新。
 */
class TodoDatabaseHelper(
    context: Context,
    dbKey: String
) : SQLiteOpenHelper(
    context.applicationContext,
    "todos_${dbKey.filter { it.isLetterOrDigit() }}.db",
    null,
    DATABASE_VERSION
) {
    companion object {
        private const val DATABASE_VERSION = 1
        private const val TABLE_TODOS = "todos"

        private const val COL_ID = "id"
        private const val COL_TITLE = "title"
        private const val COL_DESCRIPTION = "description"
        private const val COL_STATUS = "status"
        private const val COL_KIND = "kind"
        private const val COL_PROJECT = "project"
        private const val COL_DUE_AT = "due_at"
        private const val COL_NOTIFY_ENABLED = "notify_enabled"
        private const val COL_CREATED_AT = "created_at"
        private const val COL_UPDATED_AT = "updated_at"

        fun computeDbKey(serverUrl: String, username: String): String {
            val cleanUrl = try {
                com.liteagent.mobile.core.network.ServerUrlNormalizer.normalize(serverUrl)
            } catch (_: Exception) {
                serverUrl.trim().trimEnd('/')
            }
            val trimmedUser = username.trim()
            val canonical = "$cleanUrl|$trimmedUser"
            val md = java.security.MessageDigest.getInstance("SHA-256")
            val digest = md.digest(canonical.toByteArray(Charsets.UTF_8))
            return digest.joinToString("") { "%02x".format(it) }.take(32)
        }
    }

    override fun onCreate(db: SQLiteDatabase) {
        val sql = """
            CREATE TABLE IF NOT EXISTS $TABLE_TODOS (
                $COL_ID TEXT PRIMARY KEY,
                $COL_TITLE TEXT NOT NULL,
                $COL_DESCRIPTION TEXT,
                $COL_STATUS TEXT NOT NULL,
                $COL_KIND TEXT NOT NULL,
                $COL_PROJECT TEXT,
                $COL_DUE_AT TEXT,
                $COL_NOTIFY_ENABLED TEXT,
                $COL_CREATED_AT TEXT,
                $COL_UPDATED_AT TEXT
            )
        """.trimIndent()
        db.execSQL(sql)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        db.execSQL("DROP TABLE IF EXISTS $TABLE_TODOS")
        onCreate(db)
    }

    @Synchronized
    fun getAll(): List<TodoItem> {
        val list = mutableListOf<TodoItem>()
        val db = readableDatabase
        val cursor = db.query(
            TABLE_TODOS,
            null,
            null,
            null,
            null,
            null,
            "$COL_CREATED_AT DESC"
        )
        cursor.use {
            val idIdx = cursor.getColumnIndexOrThrow(COL_ID)
            val titleIdx = cursor.getColumnIndexOrThrow(COL_TITLE)
            val descIdx = cursor.getColumnIndexOrThrow(COL_DESCRIPTION)
            val statusIdx = cursor.getColumnIndexOrThrow(COL_STATUS)
            val kindIdx = cursor.getColumnIndexOrThrow(COL_KIND)
            val projectIdx = cursor.getColumnIndexOrThrow(COL_PROJECT)
            val dueAtIdx = cursor.getColumnIndexOrThrow(COL_DUE_AT)
            val notifyIdx = cursor.getColumnIndexOrThrow(COL_NOTIFY_ENABLED)
            val createdIdx = cursor.getColumnIndexOrThrow(COL_CREATED_AT)
            val updatedIdx = cursor.getColumnIndexOrThrow(COL_UPDATED_AT)

            while (cursor.moveToNext()) {
                val item = TodoItem(
                    id = cursor.getString(idIdx),
                    title = cursor.getString(titleIdx),
                    description = if (cursor.isNull(descIdx)) null else cursor.getString(descIdx),
                    status = cursor.getString(statusIdx),
                    kind = cursor.getString(kindIdx),
                    project = if (cursor.isNull(projectIdx)) null else cursor.getString(projectIdx),
                    dueAt = if (cursor.isNull(dueAtIdx)) null else cursor.getString(dueAtIdx),
                    notifyEnabled = if (cursor.isNull(notifyIdx)) null else cursor.getString(notifyIdx),
                    createdAt = if (cursor.isNull(createdIdx)) null else cursor.getString(createdIdx),
                    updatedAt = if (cursor.isNull(updatedIdx)) null else cursor.getString(updatedIdx)
                )
                list.add(item)
            }
        }
        return list
    }

    @Synchronized
    fun saveAll(items: List<TodoItem>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.delete(TABLE_TODOS, null, null)
            for (item in items) {
                val values = ContentValues().apply {
                    put(COL_ID, item.id)
                    put(COL_TITLE, item.title)
                    put(COL_DESCRIPTION, item.description)
                    put(COL_STATUS, item.status)
                    put(COL_KIND, item.kind)
                    put(COL_PROJECT, item.project)
                    put(COL_DUE_AT, item.dueAt)
                    put(COL_NOTIFY_ENABLED, item.notifyEnabled?.toString())
                    put(COL_CREATED_AT, item.createdAt)
                    put(COL_UPDATED_AT, item.updatedAt)
                }
                db.insertWithOnConflict(TABLE_TODOS, null, values, SQLiteDatabase.CONFLICT_REPLACE)
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    @Synchronized
    fun insertOrUpdate(item: TodoItem) {
        val db = writableDatabase
        val values = ContentValues().apply {
            put(COL_ID, item.id)
            put(COL_TITLE, item.title)
            put(COL_DESCRIPTION, item.description)
            put(COL_STATUS, item.status)
            put(COL_KIND, item.kind)
            put(COL_PROJECT, item.project)
            put(COL_DUE_AT, item.dueAt)
            put(COL_NOTIFY_ENABLED, item.notifyEnabled?.toString())
            put(COL_CREATED_AT, item.createdAt)
            put(COL_UPDATED_AT, item.updatedAt)
        }
        db.insertWithOnConflict(TABLE_TODOS, null, values, SQLiteDatabase.CONFLICT_REPLACE)
    }

    @Synchronized
    fun updateStatus(id: String, newStatus: String) {
        val db = writableDatabase
        val values = ContentValues().apply {
            put(COL_STATUS, newStatus)
            put(COL_UPDATED_AT, System.currentTimeMillis().toString())
        }
        db.update(TABLE_TODOS, values, "$COL_ID = ?", arrayOf(id))
    }

    @Synchronized
    fun deleteTodo(id: String) {
        val db = writableDatabase
        db.delete(TABLE_TODOS, "$COL_ID = ?", arrayOf(id))
    }

    @Synchronized
    fun clearAll() {
        val db = writableDatabase
        db.delete(TABLE_TODOS, null, null)
    }
}
