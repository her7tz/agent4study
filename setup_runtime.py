#!/usr/bin/env python3
"""Prepare this project's media tools without changing the system Python."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def compatible_python(binary):
    try:
        result = subprocess.run(
            [str(binary), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def prepare_python():
    binary = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if binary.is_file():
        if compatible_python(binary):
            return binary
        raise RuntimeError("项目 .venv 的 Python 版本过低，请移走该目录后重新配置。")
    candidates = [os.environ.get("MINIMAL_AGENT_PYTHON"), sys.executable]
    candidates.extend(shutil.which(name) for name in (
        "python3.13", "python3.12", "python3.11", "python3.10", "python3"
    ))
    candidates.extend([
        "/opt/anaconda3/bin/python3.12", "/opt/homebrew/bin/python3.12",
        "/usr/local/bin/python3.12",
    ])
    for candidate in dict.fromkeys(candidate for candidate in candidates if candidate):
        if compatible_python(candidate):
            subprocess.run([candidate, "-m", "venv", str(VENV)], check=True)
            return binary
    raise RuntimeError("视频工具需要 Python 3.10 或更新版本；请先安装后再运行配置程序。")


def prepare_javascript(tools):
    tools.mkdir(parents=True, exist_ok=True)
    if shutil.which("deno"):
        candidates = [Path(shutil.which("deno"))]
    else:
        candidates = []
    node = shutil.which("node")
    if node:
        candidates.append(Path(node))
    candidates.extend(sorted(
        (Path.home() / ".nvm/versions/node").glob("v*/bin/node"), reverse=True
    ))
    for candidate in candidates:
        try:
            result = subprocess.run([str(candidate), "--version"], capture_output=True, text=True, timeout=10)
            match = re.search(r"(?:deno\s+|v)(\d+)\.(\d+)", result.stdout)
            if result.returncode or not match:
                continue
            version = tuple(int(value) for value in match.groups())
            is_deno = candidate.name == "deno"
            if (is_deno and version < (2, 3)) or (not is_deno and version[0] < 22):
                continue
            target = tools / ("deno" if is_deno else "node")
            if target.exists() and not target.is_symlink():
                return
            pending = target.with_name(target.name + ".new")
            if pending.is_symlink() or pending.exists():
                pending.unlink()
            pending.symlink_to(candidate.resolve())
            os.replace(str(pending), str(target))
            print("YouTube 运行时已就绪。", flush=True)
            return
        except (OSError, subprocess.SubprocessError):
            continue
    print("未找到 YouTube 运行时；请安装 Deno 2.3+ 或 Node.js 22+ 后重新配置。", flush=True)


def main():
    parser = argparse.ArgumentParser(description="配置 Agent 的本地视频运行环境")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-model-download", action="store_true")
    args = parser.parse_args()
    data_home = Path(os.environ.get("MINIMAL_AGENT_HOME", str(ROOT / ".local"))).expanduser()
    data_home.mkdir(parents=True, exist_ok=True)
    python = prepare_python()
    if not args.skip_install:
        print("正在安装项目独立环境中的视频工具…", flush=True)
        subprocess.run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--cache-dir", str(data_home / "pip-cache"), "-r", str(ROOT / "requirements.txt"),
        ], check=True)
    prepare_javascript(ROOT / ".local/tools")
    env = dict(os.environ)
    env["MINIMAL_AGENT_HOME"] = str(data_home)
    env["HF_HOME"] = str(data_home / "hf-cache")
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if not args.skip_model_download:
        print("正在准备本地 base 语音识别模型，首次需要下载模型文件…", flush=True)
        subprocess.run([str(python), "-c", """
import os
from pathlib import Path
from huggingface_hub import snapshot_download
from faster_whisper import WhisperModel
folder = Path(os.environ['MINIMAL_AGENT_HOME']) / 'models/whisper-base'
required = ['model.bin', 'config.json', 'tokenizer.json']
if not all((folder / name).is_file() for name in required):
    snapshot_download('Systran/faster-whisper-base', local_dir=str(folder), token=False,
        allow_patterns=['config.json', 'model.bin', 'tokenizer.json', 'vocabulary.*'])
WhisperModel(str(folder), device='cpu', compute_type='int8', cpu_threads=2, local_files_only=True)
print('本地语音识别模型加载成功。', flush=True)
"""], check=True, env=env)
    subprocess.run([str(python), "-c", """
import importlib.metadata
import json
import os
import subprocess
from pathlib import Path
import faster_whisper
import io
import wave
from faster_whisper.audio import decode_audio
from harness import resolve_tool
versions = {name: importlib.metadata.version(name) for name in
    ['yt-dlp', 'faster-whisper', 'av', 'imageio-ffmpeg', 'yt-dlp-ejs']}
audio = io.BytesIO()
with wave.open(audio, 'wb') as writer:
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(16000)
    writer.writeframes(bytes(32000))
audio.seek(0)
decoded = decode_audio(audio)
if decoded.shape != (16000,):
    raise RuntimeError('音频解码检查没有得到预期样本。')
for name in ['yt-dlp', 'ffmpeg']:
    binary = resolve_tool(name)
    if not binary:
        raise RuntimeError('未找到工具：' + name)
    subprocess.run([binary, '--version' if name == 'yt-dlp' else '-version'],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
path = Path(os.environ['MINIMAL_AGENT_HOME']) / 'runtime-versions.json'
path.write_text(json.dumps(versions, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')
print('视频读取、关键帧提取和本地转录工具已就绪。', flush=True)
"""], check=True, cwd=str(ROOT), env=env)
    print("配置完成。请使用 启动前端.command 打开页面。", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print("配置未完成：{}".format(exc), file=sys.stderr)
        raise SystemExit(1)
