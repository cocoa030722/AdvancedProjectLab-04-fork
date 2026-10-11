"""회의 처리 파이프라인: STT -> 회의록 -> 요약 -> 이해도 문항. 백그라운드 스레드로 실행한다."""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from . import llm
from .models import Comprehension, Meeting, Minutes, QuizQuestion, Summary, Transcript
from .stt import get_stt_backend

logger = logging.getLogger(__name__)

PROCESSING_FAILED_MESSAGE = "회의록 생성에 실패했습니다. 다시 제출해 주세요."


def start_processing(meeting):
    """submitted 상태의 회의 처리를 시작한다. 테스트에서는 설정으로 동기 실행한다."""
    if getattr(settings, "MEETALIGN_RUN_PIPELINE_SYNC", False):
        run_pipeline(meeting.pk, close_connection=False)
        return None
    thread = threading.Thread(target=run_pipeline, args=(meeting.pk,), daemon=True)
    transaction.on_commit(thread.start)
    return thread


def run_pipeline(meeting_id, close_connection=True):
    try:
        meeting = Meeting.objects.get(pk=meeting_id)
        meeting.transition_to(Meeting.Status.PROCESSING)
        _process(meeting)
        meeting.transition_to(Meeting.Status.DONE)
    except Exception:
        logger.exception("meeting %s processing failed", meeting_id)
        _mark_failed(meeting_id)
    finally:
        if close_connection:
            connection.close()


def _mark_failed(meeting_id):
    try:
        meeting = Meeting.objects.get(pk=meeting_id)
        if meeting.can_transition_to(Meeting.Status.FAILED):
            meeting.transition_to(Meeting.Status.FAILED)
    except Exception:
        logger.exception("meeting %s could not be marked as failed", meeting_id)


def _process(meeting):
    recording = meeting.recording
    transcript = getattr(recording, "transcript", None)
    if transcript is None:
        text = get_stt_backend().transcribe(recording.file.path)
        transcript = Transcript.objects.create(recording=recording, text=text)

    version = (meeting.minutes.aggregate(Max("version"))["version__max"] or 0) + 1
    minutes = Minutes.objects.create(
        meeting=meeting, version=version, content=llm.make_minutes(transcript.text)
    )
    Summary.objects.create(minutes=minutes, content=llm.make_summary(minutes.content))

    quiz = llm.make_quiz(minutes.content)
    comprehension = Comprehension.objects.create(minutes=minutes)
    QuizQuestion.objects.bulk_create([
        QuizQuestion(comprehension=comprehension, text=q["text"],
                     choices=q["choices"], correct_index=q["correct_index"])
        for q in quiz
    ])


def recover_stale_meetings():
    """처리중에 멈춘 회의(서버 재시작 등)를 타임아웃 뒤 실패로 돌린다. 돌린 개수를 반환한다."""
    cutoff = timezone.now() - timedelta(seconds=settings.PROCESSING_TIMEOUT_SECONDS)
    return Meeting.objects.filter(
        status=Meeting.Status.PROCESSING, status_changed_at__lt=cutoff
    ).update(status=Meeting.Status.FAILED, status_changed_at=timezone.now())
