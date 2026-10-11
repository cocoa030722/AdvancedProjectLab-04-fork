"""STT(음성 인식) 백엔드. 설정 STT_BACKEND(dotted path)로 구현체를 교체한다."""
from django.conf import settings
from django.utils.module_loading import import_string


class STTError(Exception):
    """음성 인식 실패."""


class STTBackend:
    language = "ko"

    def transcribe(self, path):
        """오디오 파일 경로를 받아 전사 텍스트를 돌려준다."""
        raise NotImplementedError


class LocalSTT(STTBackend):
    """개발용 로컬 모델(faster-whisper). 패키지가 없으면 STTError."""
    _model = None

    def _get_model(self):
        if LocalSTT._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise STTError("로컬 STT를 쓰려면 faster-whisper 패키지가 필요합니다.") from e
            LocalSTT._model = WhisperModel(settings.LOCAL_STT_MODEL)
        return LocalSTT._model

    def transcribe(self, path):
        segments, _info = self._get_model().transcribe(str(path), language=self.language)
        return "\n".join(s.text.strip() for s in segments if s.text.strip())


class ApiSTT(STTBackend):
    """배포용 외부 STT API. 공급자가 정해지면 구현한다(현재는 스텁)."""

    def transcribe(self, path):
        raise STTError("외부 STT API가 아직 설정되지 않았습니다.")


def get_stt_backend():
    return import_string(settings.STT_BACKEND)()
