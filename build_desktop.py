#!/usr/bin/env python3
"""Build a local macOS application reusing this checkout and its Python runtime."""

import os
import plistlib
import shutil
import struct
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "Agent 工作台.app"


def main():
    if sys.platform != "darwin":
        raise RuntimeError("当前桌面客户端构建支持 macOS。")
    python = ROOT / ".venv/bin/python"
    if not python.is_file():
        raise RuntimeError("请先运行 配置运行环境.command，准备项目 Python 环境。")
    source = ROOT / "desktop/AgentDesktop.swift"
    build = ROOT / ".local/desktop-build"
    build.mkdir(parents=True, exist_ok=True)
    bundle = build / (uuid.uuid4().hex + ".app")
    executable = bundle / "Contents/MacOS/AgentDesktop"
    resources = bundle / "Contents/Resources"
    executable.parent.mkdir(parents=True)
    resources.mkdir(parents=True)
    compiler = subprocess.check_output(["xcrun", "--find", "swiftc"], text=True).strip()
    sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
    architecture = os.uname().machine
    print("正在构建桌面客户端…", flush=True)
    subprocess.run([compiler, "-swift-version", "5", "-parse-as-library", "-sdk", sdk,
                    "-target", architecture + "-apple-macos13.0", "-module-cache-path", str(build / "module-cache"),
                    "-framework", "Cocoa", "-framework", "WebKit", "-framework", "Security", str(source), str(ROOT / "desktop/AccountBrowser.swift"), "-o", str(executable)], check=True)
    iconset = build / "Agent.iconset"
    subprocess.run([str(executable), "--write-icon", str(iconset)], check=True)
    # Modern ICNS entries contain the PNG representations produced above.
    # Assemble the container from the generated PNG representations.
    entries = [(b"icp4", "icon_16x16.png"), (b"icp5", "icon_32x32.png"),
               (b"icp6", "icon_32x32@2x.png"), (b"ic07", "icon_128x128.png"),
               (b"ic08", "icon_256x256.png"), (b"ic09", "icon_512x512.png"),
               (b"ic10", "icon_512x512@2x.png"), (b"ic11", "icon_16x16@2x.png"),
               (b"ic12", "icon_32x32@2x.png"), (b"ic13", "icon_128x128@2x.png"),
               (b"ic14", "icon_256x256@2x.png")]
    chunks = []
    for kind, filename in entries:
        data = (iconset / filename).read_bytes()
        chunks.append(kind + struct.pack(">I", len(data) + 8) + data)
    data = b"".join(chunks)
    (resources / "AppIcon.icns").write_bytes(b"icns" + struct.pack(">I", len(data) + 8) + data)
    info = {
        "CFBundleIdentifier": "local.agent.workbench", "CFBundleName": "Agent 工作台",
        "CFBundleDisplayName": "Agent 工作台", "CFBundleExecutable": "AgentDesktop",
        "CFBundlePackageType": "APPL", "CFBundleVersion": "6", "CFBundleShortVersionString": "0.6.0",
        "CFBundleIconFile": "AppIcon", "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "AgentProjectRoot": str(ROOT), "AgentPythonExecutable": str(python), "AgentBackendPort": 8765,
    }
    with (bundle / "Contents/Info.plist").open("wb") as output:
        plistlib.dump(info, output)
    for attribute in ("com.apple.FinderInfo", "com.apple.ResourceFork"):
        subprocess.run(["/usr/bin/xattr", "-dr", attribute, str(bundle)], check=False, capture_output=True)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)], check=True)
    destination = ROOT / "dist" / APP_NAME
    destination.parent.mkdir(exist_ok=True)
    previous = build / ("previous-" + uuid.uuid4().hex + ".app")
    if destination.exists():
        destination.rename(previous)
    try:
        bundle.rename(destination)
    except Exception:
        if previous.exists():
            previous.rename(destination)
        raise
    if previous.exists():
        shutil.rmtree(previous)
    print("桌面客户端已生成：" + str(destination), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("构建失败：" + str(error), file=sys.stderr)
        raise SystemExit(1)
