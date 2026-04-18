@echo off
cd /d "E:\Workspace\git\retain-pdf\frontend"
"python" -m http.server 8080 --bind "127.0.0.1" > "E:\Workspace\git\retain-pdf\.run\frontend.log" 2>&1
