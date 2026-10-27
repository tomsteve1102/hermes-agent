"""[tamako-local] pilk 兼容层 —— 用 pysilk（silk-python）顶替 pilk。

为什么需要它：pilk 在 PyPI 上只有到 cp311 的 Windows wheel，本机运行时是 Python 3.14，
回落到源码编译又需要 MSVC（本机没有），所以 pilk 根本装不上；而 `silk-python`（import pysilk）
提供 cp314-win_amd64 的现成 wheel，编解码的正是腾讯 SILK（QQ/微信语音）。

adapter 只用 pilk.silk_to_wav(path, wav_path, rate=16000)，本文件照这个接口实现，
放在 workspace 根目录（在 sys.path 首位，gateway 的 PYTHONPATH 与 CLI 启动器都指向它），
`import pilk` 会命中本文件，不需要改任何上游代码。

依赖：live venv 里的 silk-python（uv pip install --python <live venv python> silk-python）。
注意：workspace 会被 `hermes update`（ZIP 通道）覆盖、venv 会被 `hermes pm install` 重建，
两处都要在更新后补回（见 hermes-update-windows skill 的本地补丁清单）。
"""

from __future__ import annotations

import os
import tempfile
import wave

import pysilk

__all__ = ["silk_to_wav", "decode", "encode"]


def decode(silk_path, pcm_path, rate: int = 16000) -> int:
    """SILK → raw 16-bit PCM 文件；返回 PCM 字节数。"""
    with open(silk_path, "rb") as src, open(pcm_path, "wb") as dst:
        pysilk.decode(src, dst, rate)
    return os.path.getsize(pcm_path)


def encode(pcm_path, silk_path, rate: int = 16000, bit_rate: int = 24000) -> int:
    """raw 16-bit PCM → 腾讯 SILK；返回 SILK 字节数。"""
    with open(pcm_path, "rb") as src, open(silk_path, "wb") as dst:
        pysilk.encode(src, dst, rate, bit_rate, tencent=True)
    return os.path.getsize(silk_path)


def silk_to_wav(silk_path, wav_path, rate: int = 16000) -> int:
    """SILK → 16-bit mono WAV（pilk 的接口形态）；返回写入的 PCM 字节数。"""
    tmp_dir = tempfile.mkdtemp(prefix="pilk-shim-")
    pcm_path = os.path.join(tmp_dir, "out.pcm")
    try:
        size = decode(silk_path, pcm_path, rate)
        with open(pcm_path, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(pcm_path)
        except OSError:
            pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
    if not data:
        raise ValueError(f"pilk shim: decoded no audio from {silk_path}")
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return size
