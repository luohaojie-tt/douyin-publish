@echo off
rem Douyin (creator.douyin.com) auto-publish browser (separate profile, daily browser untouched)
rem First time: scan QR to login douyin in this window, then tell the AI: login done
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9224 --user-data-dir=C:\Users\lhj\.workbuddy\chrome-douyin-profile --no-first-run --no-default-browser-check https://creator.douyin.com/creator-micro/content/upload
