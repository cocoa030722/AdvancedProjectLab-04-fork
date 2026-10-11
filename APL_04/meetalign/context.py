"""LLM 대화에 넣는 프로젝트 맥락(회의 요약의 누적) 구성."""
from django.conf import settings


def project_summaries(project):
    """프로젝트 회의들의 요약(최신 회의록 버전 기준)을 오래된 회의부터 돌려준다."""
    texts = []
    for meeting in project.meetings.order_by("created_at", "pk"):
        minutes = meeting.latest_minutes
        summary = getattr(minutes, "summary", None) if minutes else None
        if summary:
            texts.append(summary.content)
    return texts


def context_usage(project):
    return sum(len(t) for t in project_summaries(project))


def trim_context(texts, limit):
    """글자 수 합이 limit을 넘으면 가장 오래된 것부터 제외한다. (남은 목록, 제외 개수)를 돌려준다."""
    texts = list(texts)
    excluded = 0
    while texts and sum(len(t) for t in texts) > limit:
        texts.pop(0)
        excluded += 1
    return texts, excluded


def build_context(project, limit=None):
    if limit is None:
        limit = settings.CONTEXT_CHAR_LIMIT
    return trim_context(project_summaries(project), limit)


def context_warning_needed(project):
    """맥락 사용량이 경고 비율을 넘어 새 회의가 기존 요약을 밀어낼 수 있는지."""
    return context_usage(project) > settings.CONTEXT_CHAR_LIMIT * settings.CONTEXT_WARN_RATIO
