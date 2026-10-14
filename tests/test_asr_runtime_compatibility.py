"""Exercise the real audio decoding path with no network or model inference."""

import importlib.util
import io
import unittest
import wave
from unittest.mock import patch

from harness import asr_runtime_status


@unittest.skipUnless(importlib.util.find_spec("faster_whisper"), "本地转录依赖未安装")
class ASRDecodeCompatibilityTests(unittest.TestCase):
    def tearDown(self):
        asr_runtime_status.cache_clear()

    def test_installed_audio_dependencies_can_decode_a_wav_stream(self):
        import numpy as np
        from faster_whisper.audio import decode_audio

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(bytes(32000))
        buffer.seek(0)
        audio = decode_audio(buffer)
        self.assertEqual(audio.shape, (16000,))
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(float(np.max(np.abs(audio))), 0)

    def test_dependency_status_rejects_an_incompatible_decoder(self):
        asr_runtime_status.cache_clear()
        with patch("faster_whisper.audio.decode_audio", side_effect=TypeError(
            "open() got an unexpected keyword argument 'metadata_errors'"
        )):
            status = asr_runtime_status()
        self.assertFalse(status["ready"])
        self.assertIn("音频解码检查失败", status["error"])


if __name__ == "__main__":
    unittest.main()
