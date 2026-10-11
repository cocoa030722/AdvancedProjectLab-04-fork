"""클로드 API 호출. Anthropic SDK를 import하는 유일한 모듈이다.

용도별 모델은 settings.CLAUDE_MODELS, API 키는 환경변수 ANTHROPIC_API_KEY에서 읽는다.
"""
import json
import logging
import os
import re

from django.conf import settings

logger = logging.getLogger(__name__)

LLM_ERROR_MESSAGE = "응답 생성에 실패했습니다. 잠시 후 다시 시도해 주세요."

MINUTES_SYSTEM = (
    "당신은 회의록 작성자입니다. 주어진 회의 녹취록의 내용을 빠짐없이 정리한 한국어 회의록을 "
    "단순 텍스트로 작성하세요. 마크다운이나 HTML 표기는 쓰지 마세요."
)
SUMMARY_SYSTEM = (
    "주어진 회의록을 핵심 안건, 결정 사항, 할 일 중심으로 짧게 요약하세요. "
    "한국어 단순 텍스트로 작성하세요."
)
CHAT_SYSTEM = (
    "당신은 팀 프로젝트의 회의 내용을 바탕으로 질문에 답하는 도우미입니다. "
    "제공된 회의 요약에 근거해 한국어로 답하고, 근거가 없으면 모른다고 말하세요."
)
QUIZ_SYSTEM = (
    "주어진 회의록을 바탕으로 참석자의 이해도를 확인하는 객관식 문제를 정확히 3개 만드세요. "
    "JSON 배열만 출력하세요. 각 원소는 "
    '{"text": 문제, "choices": [선택지 4개], "correct_index": 정답 번호(0~3)} 형식입니다.'
)
ANSWER_DRAFT_SYSTEM = (
    "당신은 팀장을 돕는 비서입니다. 회의록을 근거로 팀원의 익명 질문에 대한 답변 초안을 "
    "한국어 단순 텍스트로 작성하세요."
)

QUIZ_COUNT = 3
QUIZ_CHOICES = 4


class LLMError(Exception):
    """LLM 호출 또는 응답 해석 실패."""


def _client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
    import anthropic  # 지연 import: 키 없는 환경과 테스트에서도 모듈을 불러올 수 있게 한다

    return anthropic.Anthropic(api_key=api_key)


def _complete(purpose, system, messages, max_tokens=4096):
    """purpose(minutes/summary/chat/quiz/answer_draft)에 설정된 모델로 응답 텍스트를 받는다."""
    model = settings.CLAUDE_MODELS[purpose]
    try:
        response = _client().messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages
        )
    except LLMError:
        raise
    except Exception as e:
        logger.exception("claude api call failed (%s)", purpose)
        raise LLMError("LLM 호출에 실패했습니다.") from e
    return "".join(getattr(block, "text", "") for block in response.content)


def _ask(purpose, system, text, max_tokens=4096):
    return _complete(purpose, system, [{"role": "user", "content": text}], max_tokens)


def make_minutes(transcript_text):
    return _ask("minutes", MINUTES_SYSTEM, transcript_text, 8192).strip()


def make_summary(minutes_text):
    return _ask("summary", SUMMARY_SYSTEM, minutes_text, 2048).strip()


def draft_answer(minutes_text, question_text):
    prompt = "[회의록]\n%s\n\n[질문]\n%s" % (minutes_text, question_text)
    return _ask("answer_draft", ANSWER_DRAFT_SYSTEM, prompt, 1024).strip()


def _merge_history(history):
    """(sender, content) 목록을 user/assistant가 번갈아 나오는 메시지 목록으로 만든다."""
    messages = []
    for sender, content in history:
        role = "assistant" if sender == "llm" else "user"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + content
        else:
            messages.append({"role": role, "content": content})
    if messages and messages[0]["role"] == "assistant":
        messages.pop(0)
    return messages


def chat_reply(context_texts, history, question):
    """프로젝트 맥락(회의 요약, 오래된 순)과 대화 기록을 바탕으로 질문에 답한다."""
    system = CHAT_SYSTEM
    if context_texts:
        system += "\n\n[프로젝트 회의 요약 (오래된 순)]\n" + "\n\n".join(context_texts)
    messages = _merge_history(history)
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n" + question
    else:
        messages.append({"role": "user", "content": question})
    return _complete("chat", system, messages, 2048).strip()


def _parse_quiz(raw):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError("문항 JSON을 해석할 수 없습니다.") from e
    if not isinstance(data, list) or len(data) != QUIZ_COUNT:
        raise LLMError("문항 수가 %d개가 아닙니다." % QUIZ_COUNT)
    items = []
    for item in data:
        if not isinstance(item, dict):
            raise LLMError("문항 형식이 올바르지 않습니다.")
        question, choices, index = item.get("text"), item.get("choices"), item.get("correct_index")
        if not isinstance(question, str) or not question.strip():
            raise LLMError("문항 텍스트가 비었습니다.")
        if (not isinstance(choices, list) or len(choices) != QUIZ_CHOICES
                or not all(isinstance(c, str) and c.strip() for c in choices)):
            raise LLMError("선택지는 %d개여야 합니다." % QUIZ_CHOICES)
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < QUIZ_CHOICES:
            raise LLMError("정답 번호가 범위를 벗어났습니다.")
        items.append({"text": question.strip(), "choices": [c.strip() for c in choices],
                      "correct_index": index})
    return items


def make_quiz(minutes_text):
    """객관식 3문항을 생성한다. 형식이 맞지 않으면 QUIZ_MAX_RETRIES만큼 재시도한다."""
    last_error = None
    for _attempt in range(settings.QUIZ_MAX_RETRIES + 1):
        raw = _ask("quiz", QUIZ_SYSTEM, minutes_text, 2048)
        try:
            return _parse_quiz(raw)
        except LLMError as e:
            last_error = e
            logger.warning("quiz parse failed, retrying: %s", e)
    raise LLMError("문항 생성에 실패했습니다.") from last_error
