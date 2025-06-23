from commands import execute_command

# مثال على تنفيذ مهمة لايك لحسابات TikTok
execute_command(
    platform="tiktok",
    action="like",
    target="https://www.tiktok.com/@someuser/video/1234567890"
)