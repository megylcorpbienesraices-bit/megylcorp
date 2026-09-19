"""Local-only voice adapters for Sophia.

STT: faster-whisper (optional) or whisper.cpp CLI configured by environment.
TTS: Windows SAPI (built into Windows Server/Desktop) or Piper CLI configured locally.
No cloud speech provider and no per-request credits.
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any
import os
import platform
import shutil
import subprocess
import tempfile
from .obs import note as _obs_note


class SophiaVoice:
    def __init__(self) -> None:
        self._lock=RLock();self._whisper=None

    def status(self) -> dict[str,Any]:
        fw=False
        try:
            import faster_whisper  # noqa: F401
            fw=True
        except ModuleNotFoundError:
            fw=False
        except Exception as _e:
            _obs_note('sophia_voice:29', _e)
        whisper_cli=str(os.getenv("ITM_SOPHIA_WHISPER_CLI","")).strip()
        piper=str(os.getenv("ITM_SOPHIA_PIPER_CLI","")).strip()
        return {"stt":{"faster_whisper":fw,"whisper_cpp":bool(whisper_cli and Path(whisper_cli).exists()),"configured":fw or bool(whisper_cli and Path(whisper_cli).exists())},
                "tts":{"windows_sapi":platform.system()=="Windows","piper":bool(piper and Path(piper).exists()),"configured":platform.system()=="Windows" or bool(piper and Path(piper).exists())},
                "billing":"LOCAL_NO_CREDITS"}

    def _fw_model(self):
        with self._lock:
            if self._whisper is not None:return self._whisper
            from faster_whisper import WhisperModel
            model=str(os.getenv("ITM_SOPHIA_STT_MODEL","small"))
            device=str(os.getenv("ITM_SOPHIA_STT_DEVICE","cpu"))
            compute=str(os.getenv("ITM_SOPHIA_STT_COMPUTE","int8" if device=="cpu" else "float16"))
            self._whisper=WhisperModel(model,device=device,compute_type=compute)
            return self._whisper

    def transcribe_bytes(self,data:bytes,suffix:str=".webm") -> str:
        if not data:raise RuntimeError("AUDIO_EMPTY")
        with tempfile.TemporaryDirectory(prefix="itmq_sophia_stt_") as td:
            src=Path(td)/("speech"+(suffix if suffix.startswith(".") else ".webm"));src.write_bytes(data)
            try:
                model=self._fw_model();segments,_info=model.transcribe(str(src),language="es",vad_filter=True,beam_size=3)
                text=" ".join(str(s.text or "").strip() for s in segments).strip()
                if text:return text
            except Exception as fw_exc:
                cli=str(os.getenv("ITM_SOPHIA_WHISPER_CLI","")).strip();model_path=str(os.getenv("ITM_SOPHIA_WHISPER_MODEL_PATH","")).strip()
                if not cli or not Path(cli).exists() or not model_path or not Path(model_path).exists():
                    raise RuntimeError(f"LOCAL_STT_NOT_READY: {fw_exc}")
                wav=src
                ffmpeg=shutil.which("ffmpeg")
                if ffmpeg and src.suffix.lower() not in {".wav",".mp3"}:
                    wav=Path(td)/"speech.wav";subprocess.run([ffmpeg,"-y","-i",str(src),"-ar","16000","-ac","1",str(wav)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                outbase=Path(td)/"out"
                cmd=[cli,"-m",model_path,"-f",str(wav),"-l","es","-otxt","-of",str(outbase)]
                subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=90)
                txt=outbase.with_suffix(".txt")
                if txt.exists():return txt.read_text(encoding="utf-8",errors="ignore").strip()
        raise RuntimeError("LOCAL_STT_NO_TEXT")

    def synthesize_wav(self,text:str) -> bytes:
        msg=str(text or "").strip()
        if not msg:raise RuntimeError("TEXT_EMPTY")
        with tempfile.TemporaryDirectory(prefix="itmq_sophia_tts_") as td:
            out=Path(td)/"sophia.wav"
            piper=str(os.getenv("ITM_SOPHIA_PIPER_CLI","")).strip();model=str(os.getenv("ITM_SOPHIA_PIPER_MODEL","")).strip()
            if piper and Path(piper).exists() and model and Path(model).exists():
                subprocess.run([piper,"--model",model,"--output_file",str(out)],input=msg,text=True,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=60)
            elif platform.system()=="Windows":
                # Windows SAPI is local and does not bill per utterance.
                escaped=msg.replace("'","''")
                script=("Add-Type -AssemblyName System.Speech; "
                        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        f"$s.SetOutputToWaveFile('{str(out).replace(chr(39),chr(39)*2)}'); "
                        f"$s.Speak('{escaped}'); $s.Dispose();")
                subprocess.run(["powershell","-NoProfile","-Command",script],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=60)
            else:
                raise RuntimeError("LOCAL_TTS_NOT_READY")
            if not out.exists():raise RuntimeError("LOCAL_TTS_NO_AUDIO")
            return out.read_bytes()


SOPHIA_VOICE=SophiaVoice()
