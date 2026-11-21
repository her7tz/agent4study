#!/bin/zsh
cd -- "${0:A:h}" || exit 1
desktop_python="$PWD/.venv/bin/python"
if [[ ! -x "$desktop_python" ]]; then
    printf '请先双击 配置运行环境.command，完成本地环境配置。\n'
    read -r 'desktop_answer?按 Enter 关闭。'
    exit 1
fi
desktop_bundle="$PWD/dist/Agent 工作台.app"
desktop_archive="$PWD/dist/Agent 工作台.zip"
desktop_executable="$desktop_bundle/Contents/MacOS/AgentDesktop"

desktop_binding_matches() {
    local info="$1/Contents/Info.plist" project python
    project="$(/usr/bin/plutil -extract AgentProjectRoot raw -o - "$info" 2>/dev/null)" || return 1
    python="$(/usr/bin/plutil -extract AgentPythonExecutable raw -o - "$info" 2>/dev/null)" || return 1
    [[ "$project" == "$PWD" && "$python" == "$desktop_python" ]]
}

if [[ ! -x "$desktop_executable" || ! -f "$desktop_archive" || "$PWD/desktop/AgentDesktop.swift" -nt "$desktop_executable" || "$PWD/desktop/AccountBrowser.swift" -nt "$desktop_executable" || "$PWD/desktop/BackendRecovery.swift" -nt "$desktop_executable" || "$PWD/build_desktop.py" -nt "$desktop_executable" ]] ||
   ! desktop_binding_matches "$desktop_bundle"; then
    "$desktop_python" "$PWD/build_desktop.py" || exit 1
fi

if desktop_binding_matches "$desktop_bundle" && /usr/bin/codesign --verify --deep --strict "$desktop_bundle" 2>/dev/null; then
    exec /usr/bin/open "$desktop_bundle"
fi

# A sync provider can attach Finder metadata to the bundle after it was built.
desktop_temp="$(/usr/bin/getconf DARWIN_USER_TEMP_DIR)" || exit 1
desktop_hash="$(/usr/bin/shasum -a 256 "$desktop_archive")" || exit 1
desktop_cache="${desktop_temp%/}/agent-workbench-${desktop_hash%% *}"
desktop_cached_bundle="$desktop_cache/Agent 工作台.app"
if desktop_binding_matches "$desktop_cached_bundle" && /usr/bin/codesign --verify --deep --strict "$desktop_cached_bundle" 2>/dev/null; then
    exec /usr/bin/open "$desktop_cached_bundle"
fi

desktop_stage="$(/usr/bin/mktemp -d "${desktop_temp%/}/agent-workbench.XXXXXXXX")" || exit 1
desktop_verified_bundle="$desktop_stage/Agent 工作台.app"
if ! /usr/bin/ditto -x -k "$desktop_archive" "$desktop_stage" ||
   ! /usr/bin/codesign --verify --deep --strict "$desktop_verified_bundle" ||
   ! desktop_binding_matches "$desktop_verified_bundle"; then
    /bin/rm -rf -- "$desktop_stage"
    printf '桌面压缩包未通过验签或不属于当前项目。请重新构建。\n' >&2
    exit 1
fi
/bin/mkdir -p "$desktop_cache" && /bin/mv -n "$desktop_verified_bundle" "$desktop_cache/"
if desktop_binding_matches "$desktop_cached_bundle" && /usr/bin/codesign --verify --deep --strict "$desktop_cached_bundle" 2>/dev/null; then
    /bin/rm -rf -- "$desktop_stage"
    desktop_verified_bundle="$desktop_cached_bundle"
fi
exec /usr/bin/open "$desktop_verified_bundle"
