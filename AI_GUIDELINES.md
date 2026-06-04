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
- When creating throwaway test scripts, clean them up after verifying functionality.
