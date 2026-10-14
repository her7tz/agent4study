#!/bin/zsh

cd -- "${0:A:h}" || exit 1
export MINIMAL_AGENT_HOME="${MINIMAL_AGENT_HOME:-$PWD/.local}"
export MINIMAL_AGENT_OUTPUT="${MINIMAL_AGENT_OUTPUT:-$PWD/summaries}"
printf '本地 Agent 页面：http://127.0.0.1:8765\n保持此窗口开启，按 Ctrl+C 停止服务。\n\n'
if [[ -x "$PWD/.venv/bin/python" ]]; then
    exec "$PWD/.venv/bin/python" agent.py web --open-browser
fi
exec python3 agent.py web --open-browser
