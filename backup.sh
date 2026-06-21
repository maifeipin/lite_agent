#!/bin/bash
# Vaultwarden daily backup script
BACKUP_DIR="/opt/vaultwarden/backups"
DATA_DIR="/opt/vaultwarden/vw-data"
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/vaultwarden_${TIMESTAMP}.tar.gz"

# Use sqlite3 .backup for consistent snapshot
if command -v sqlite3 &>/dev/null; then
    sqlite3 "$DATA_DIR/db.sqlite3" ".backup '$DATA_DIR/db_backup.sqlite3'"
    tar czf "$BACKUP_FILE" -C "$DATA_DIR" db_backup.sqlite3 config.json rsa_key.pem rsa_key.pub.pem 2>/dev/null
    rm -f "$DATA_DIR/db_backup.sqlite3"
else
    tar czf "$BACKUP_FILE" -C "$DATA_DIR" db.sqlite3 config.json rsa_key.pem rsa_key.pub.pem 2>/dev/null
fi

# Cleanup old backups
find "$BACKUP_DIR" -name "vaultwarden_*.tar.gz" -mtime +$KEEP_DAYS -delete

echo "[$(date)] Backup done: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"
