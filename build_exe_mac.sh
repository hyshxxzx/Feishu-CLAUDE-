#!/bin/bash
# Claude Code 飞书同步助手 — macOS 打包脚本
set -e
echo "============================================"
echo " Claude Code 飞书同步助手 — macOS .app 打包"
echo "============================================"

# 检查 Python
if ! command -v python3 &>/dev/null; then
  echo "[错误] 未找到 python3，请先安装 Python 3.8+"
  echo "  brew install python3"
  exit 1
fi

# 安装 pyinstaller
echo "[1/3] 安装 PyInstaller 和托盘依赖..."
pip3 install pyinstaller pystray pillow --quiet

# 打包
echo "[2/3] 打包中（约 1-3 分钟）..."
pyinstaller --onefile --windowed --name "飞书同步助手" \
  feishu_sync_app.py

echo "[3/3] 完成！"
echo ""
echo "✅ .app 位置: dist/飞书同步助手.app"
echo "   直接双击即可运行"
echo ""
echo "若 macOS 提示"无法打开，因为无法验证开发者"："
echo "  系统设置 → 隐私与安全 → 仍要打开"
