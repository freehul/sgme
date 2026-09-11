@echo off
rem 启动评测进度看板服务（8899）。用 wmic process call create 脱离式拉起，
rem 或在交互式会话里双击运行都行。日志落 eval\results\<run>\progress_server.out
cd /d D:\Projects\SGME
.venv\Scripts\python.exe eval\progress_server.py --output eval/results/lme100_refined --log eval/results/lme100_refined/run.log --port 8899 --expect 100 >> eval\results\lme100_refined\progress_server.out 2>&1
