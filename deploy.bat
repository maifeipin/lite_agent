@echo off
echo Packaging lite_agent (excluding pyc, cache, and deploy.bat)...
tar -c -z -f deploy.tgz --exclude=__pycache__ --exclude=*.pyc --exclude=deploy.bat --exclude=.git * .gitignore

echo Deploying lite_agent to VPS1...
scp deploy.tgz vps1:/root/lite_agent/
ssh vps1 "cd /root/lite_agent && tar -xzf deploy.tgz && rm deploy.tgz && systemctl restart feishu-bot"

del deploy.tgz
echo Deployment finished!
