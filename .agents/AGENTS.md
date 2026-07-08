# AI Assistant Development Guidelines

> [!CAUTION]
> **CRITICAL SECURITY RULE: NEVER HARDCODE CREDENTIALS**
> This project is designed to be pushed to public Git repositories (e.g., GitHub, Gitee). You MUST NOT hardcode any sensitive information in the source code.

## 1. Credential Management
- **No Hardcoded Secrets**: Passwords, API keys, database URIs, tokens, and any other credentials MUST NEVER be hardcoded in Python `.py` files, shell scripts, or any source code.
- **Use Configuration Files**: Read all secrets from `config.json` (which is excluded from Git via `.gitignore`) or from environment variables.
- **Update Example Configs**: Whenever you add a new integration that requires credentials, add placeholder keys to `config.example.json` with dummy values (e.g., `"password": "your_password_here"`) so users know how to configure it.

## 2. Skill Development
- Follow the `@skill` decorator pattern used in existing files within the `skills/` directory.
- For database access, initialize the connection inside the function or use a helper that reads from `config.json`. Always ensure connections are closed or managed via context managers (`with` statements).
- Prefer direct protocol connections (e.g., using `psycopg2` for PostgreSQL, `pymongo` for MongoDB) over shell/SSH command execution where applicable, to improve performance and security.

## 3. Deployment and Testing
- Always verify your code changes locally or on the target VPS before considering a task complete.
- **No Temporary Files in Project Directory**: All temporary testing files, scratch scripts, or intermediate data files MUST NOT be placed in the project directory. Always use the dedicated artifact scratch directory (`<appDataDir>\brain\<conversation-id>/scratch/`) or the system `tempfile` module. **Never pollute the project's Git repository with testing artifacts.**
- When creating throwaway test scripts, clean them up after verifying functionality.

## 4. Pair Programming Workflow (结对编程与提交流程)
- **自动生成修改总结**：修改或编写代码后，AI 助手**必须自动生成详细的修改总结（包含核心修改说明和 Git Diff 形式的代码对照）**，供用户交由另一个 AI 进行复核。
- **确认后提交/上线**：在用户没有明确反馈“另一个 AI 已确认，可以提交/上线”之前，AI 助手**严禁**直接执行 `git commit`、`git push` 或向 VPS 部署更新代码。
