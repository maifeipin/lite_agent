import bcrypt

pwd = b"3hcutR92RuafodYzFyzqpBQq"
salt = bcrypt.gensalt()
hashed = bcrypt.hashpw(pwd, salt).decode()
with open("/etc/nginx/conf.d/dashboard.htpasswd", "w") as f:
    f.write(f"admin:{hashed}\n")
print("htpasswd updated (bcrypt)")
