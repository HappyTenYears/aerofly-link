#!/usr/bin/env python3
"""双击运行即可启动 Mock DLL Server，自动清理端口。"""
import subprocess
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 自动杀占用端口的进程
for port in [12345, 12346]:
    r = subprocess.run(
        f'netstat -ano | findstr ":{port} " | findstr "LISTENING"',
        shell=True, capture_output=True, text=True
    )
    for line in r.stdout.strip().split('\n'):
        parts = line.strip().split()
        if parts:
            pid = parts[-1]
            if pid.isdigit():
                print(f'Killing PID {pid} on port {port}...')
                subprocess.run(f'taskkill /F /PID {pid}', shell=True, capture_output=True)

print('Starting Mock DLL Server...')
print('Telemetry : localhost:12345')
print('Command   : localhost:12346')
print('Press Ctrl+C to stop.\n')

# 启动 Mock DLL Server
subprocess.run(
    [sys.executable, '-u', 'core/mock_dll_server.py'],
    cwd=os.path.dirname(os.path.abspath(__file__))
)
