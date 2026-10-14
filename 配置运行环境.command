#!/bin/zsh

cd -- "${0:A:h}" || exit 1
export MINIMAL_AGENT_HOME="${MINIMAL_AGENT_HOME:-$PWD/.local}"
python3 setup_runtime.py
result=$?
printf '\n按回车关闭窗口。'
read
exit "$result"
