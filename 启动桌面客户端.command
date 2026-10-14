#!/bin/zsh
cd -- "${0:A:h}" || exit 1
desktop_python="$PWD/.venv/bin/python"
if [[ ! -x "$desktop_python" ]]; then
    printf '请先双击 配置运行环境.command，完成本地环境配置。\n'
    read -r 'desktop_answer?按 Enter 关闭。'
    exit 1
fi
if [[ ! -d "$PWD/dist/Agent 工作台.app" ]]; then
    "$desktop_python" "$PWD/build_desktop.py" || exit 1
fi
exec /usr/bin/open "$PWD/dist/Agent 工作台.app"
