import os

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import llm
from .audio import get_duration_seconds
from .context import build_context, context_warning_needed
from .forms import SignupForm
from .models import (Answer, Conversation, Meeting, Message, Project, Question, QuizAttempt,
                     Recording, Team, TeamMembership)
from .pipeline import PROCESSING_FAILED_MESSAGE, recover_stale_meetings, start_processing

WEBM_MAGIC = b"\x1a\x45\xdf\xa3"
CHAT_HISTORY_LIMIT = 20


# ---------- 인증 ----------

def index(request):
    teams = request.user.teams.all() if request.user.is_authenticated else []
    # 데모 계정 목록은 개발 모드에서만 노출한다
    demo_accounts = settings.DEMO_ACCOUNTS if settings.DEBUG else []
    return render(request, "meetalign/index.html", {"teams": teams, "demo_accounts": demo_accounts})


def signup(request):
    if request.user.is_authenticated:
        return redirect("index")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("index")
    return render(request, "meetalign/signup.html", {"form": form})


login_view = LoginView.as_view(template_name="meetalign/login.html", redirect_authenticated_user=True)
logout_view = LogoutView.as_view()


# ---------- 소속/권한 검사 ----------
# 팀 비소속자는 리소스 존재 여부를 숨기기 위해 404, 팀원이 팀장 전용 기능을 쓰면 403.

def _team(request, team_id):
    return get_object_or_404(Team, pk=team_id, memberships__user=request.user)


def _project(request, team_id, project_id):
    return get_object_or_404(
        Project.objects.select_related("team"),
        pk=project_id, team_id=team_id, team__memberships__user=request.user,
    )


def _meeting(request, team_id, project_id, meeting_id):
    return get_object_or_404(
        Meeting.objects.select_related("project__team"),
        pk=meeting_id, project_id=project_id, project__team_id=team_id,
        project__team__memberships__user=request.user,
    )


def _require_leader(request, team):
    if not team.is_leader(request.user):
        raise PermissionDenied


def _ctx(request, meeting, **extra):
    """회의 하위 화면 공통 컨텍스트."""
    team = meeting.project.team
    return {"meeting": meeting, "project": meeting.project, "team": team,
            "is_leader": team.is_leader(request.user), **extra}


def _meeting_url(name, meeting):
    return reverse(name, kwargs={"team_id": meeting.project.team_id,
                                 "project_id": meeting.project_id, "meeting_id": meeting.id})


# ---------- 팀/프로젝트 ----------

@login_required
def team_join(request):
    error = ""
    if request.method == "POST":
        if request.POST.get("action") == "create":
            name = request.POST.get("name", "").strip()
            if not name or Team.objects.filter(name=name).exists():
                error = "팀 이름이 비었거나 이미 존재합니다."
            else:
                team = Team.objects.create(name=name)
                TeamMembership.objects.create(
                    team=team, user=request.user, role=TeamMembership.Role.LEADER)
                return redirect("project_list", team_id=team.id)
        else:
            code = request.POST.get("invite_code", "").strip()
            team = Team.objects.filter(invite_code=code).first() if code else None
            if team is None:
                error = "초대 코드가 올바르지 않습니다."
            else:
                TeamMembership.objects.get_or_create(team=team, user=request.user)
                return redirect("project_list", team_id=team.id)
    return render(request, "meetalign/team_join.html", {"error": error})


@login_required
def project_list(request, team_id):
    team = _team(request, team_id)
    return render(request, "meetalign/project_list.html", {
        "team": team, "projects": team.projects.order_by("created_at", "pk"),
        "is_leader": team.is_leader(request.user),
    })


@login_required
def project_create(request, team_id):
    team = _team(request, team_id)
    _require_leader(request, team)
    error = ""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            error = "프로젝트 이름을 입력하세요."
        else:
            project = Project.objects.create(team=team, name=name)
            return redirect("meeting_list", team_id=team.id, project_id=project.id)
    return render(request, "meetalign/project_create.html", {"team": team, "error": error})


@login_required
@require_POST
def invite_regenerate(request, team_id):
    team = _team(request, team_id)
    _require_leader(request, team)
    team.regenerate_invite_code()
    return redirect("project_list", team_id=team.id)


# ---------- 회의 ----------

@login_required
def meeting_list(request, team_id, project_id):
    project = _project(request, team_id, project_id)
    return render(request, "meetalign/meeting_list.html", {
        "team": project.team, "project": project,
        "meetings": project.meetings.order_by("-created_at", "-pk"),
        "is_leader": project.team.is_leader(request.user),
    })


@login_required
def meeting_create(request, team_id, project_id):
    project = _project(request, team_id, project_id)
    _require_leader(request, project.team)
    error = ""
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        if not title:
            error = "회의 제목을 입력하세요."
        else:
            meeting = Meeting.objects.create(project=project, title=title)
            return redirect(_meeting_url("meeting_detail", meeting))
    return render(request, "meetalign/meeting_create.html", {
        "team": project.team, "project": project, "error": error,
        "context_warning": context_warning_needed(project),
    })


def _recording_error(upload):
    """업로드 파일의 확장자/내용/크기를 검사해 오류 문구를 돌려준다. 문제없으면 None."""
    if upload is None:
        return "녹음 파일이 필요합니다."
    ext = os.path.splitext(upload.name)[1].lower()
    if ext not in settings.ALLOWED_RECORDING_EXTENSIONS:
        return "webm 또는 mp3 파일만 업로드할 수 있습니다."
    if upload.size > settings.MAX_RECORDING_BYTES:
        return "파일 크기가 상한(%dMB)을 넘었습니다." % (settings.MAX_RECORDING_BYTES // (1024 * 1024))
    head = upload.read(4)
    upload.seek(0)
    if ext == ".webm":
        valid = head.startswith(WEBM_MAGIC)
    else:
        valid = head.startswith(b"ID3") or (
            len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if not valid:
        return "파일 내용이 webm/mp3 형식이 아닙니다."
    return None


def _handle_submit(request, meeting):
    """녹음 제출/재제출. (오류 문구, HTTP 상태)를 돌려주며 성공이면 ("", 200)."""
    if not meeting.can_submit:
        return "이미 제출된 회의입니다.", 409
    upload = request.FILES.get("recording")
    existing = getattr(meeting, "recording", None)
    if upload is None:
        # 파일 없이 재시도는 실패 상태에서 기존 녹음이 있을 때만 허용
        if existing is None or meeting.status != Meeting.Status.FAILED:
            return "녹음 파일이 필요합니다.", 400
    else:
        error = _recording_error(upload)
        if error:
            return error, 400
        if existing is not None:
            existing.delete()  # 파일(시그널)과 전사 결과(CASCADE)도 함께 삭제
        recording = Recording.objects.create(meeting=meeting, file=upload)
        duration = get_duration_seconds(recording.file.path)
        if duration is not None and duration > settings.MAX_RECORDING_SECONDS:
            recording.delete()
            return "녹음 길이가 상한(%d분)을 넘었습니다." % (settings.MAX_RECORDING_SECONDS // 60), 400
        recording.duration_seconds = duration
        recording.save(update_fields=["duration_seconds"])
    meeting.transition_to(Meeting.Status.SUBMITTED)
    start_processing(meeting)
    return "", 200


@login_required
def meeting_detail(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    error, status_code = "", 200
    if request.method == "POST":
        _require_leader(request, meeting.project.team)
        error, status_code = _handle_submit(request, meeting)
        if not error:
            return redirect(_meeting_url("meeting_result", meeting))
        meeting.refresh_from_db()
    return render(request, "meetalign/meeting_detail.html", _ctx(
        request, meeting, error=error, recording=getattr(meeting, "recording", None),
        failed_message=PROCESSING_FAILED_MESSAGE,
    ), status=status_code)


@login_required
def meeting_result(request, team_id, project_id, meeting_id):
    recover_stale_meetings()
    meeting = _meeting(request, team_id, project_id, meeting_id)
    return render(request, "meetalign/meeting_result.html", _ctx(
        request, meeting, minutes=meeting.latest_minutes,
        versions=meeting.minutes.order_by("-version"), failed_message=PROCESSING_FAILED_MESSAGE,
    ))


@login_required
def meeting_status(request, team_id, project_id, meeting_id):
    recover_stale_meetings()
    meeting = _meeting(request, team_id, project_id, meeting_id)
    minutes = meeting.latest_minutes
    return JsonResponse({"status": meeting.status,
                         "minutes_version": minutes.version if minutes else None})


@login_required
@require_POST
def meeting_regenerate(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    _require_leader(request, meeting.project.team)
    if meeting.status != Meeting.Status.DONE:
        return HttpResponse("완료된 회의만 재생성할 수 있습니다.", status=409)
    meeting.transition_to(Meeting.Status.SUBMITTED)
    start_processing(meeting)
    return redirect(_meeting_url("meeting_result", meeting))


@login_required
def minutes_download(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    minutes = meeting.latest_minutes
    if meeting.status != Meeting.Status.DONE or minutes is None:
        raise Http404
    response = HttpResponse(minutes.content, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="meeting-%d-v%d.txt"' % (
        meeting.id, minutes.version)
    return response


@login_required
def recording_file(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    recording = getattr(meeting, "recording", None)
    if recording is None:
        raise Http404
    return FileResponse(recording.file.open("rb"), filename=os.path.basename(recording.file.name))


@login_required
def summary(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    minutes = meeting.latest_minutes
    return render(request, "meetalign/summary.html", _ctx(
        request, meeting, minutes=minutes,
        summary=getattr(minutes, "summary", None) if minutes else None,
    ))


# ---------- 대화창 ----------

@login_required
def chat(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    conversation, _ = Conversation.objects.get_or_create(
        meeting=meeting, owner=request.user, kind=Conversation.Kind.LLM)
    error = ""
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        if text:
            context_texts, _excluded = build_context(meeting.project)
            history = list(conversation.messages.values_list("sender", "content"))[-CHAT_HISTORY_LIMIT:]
            try:
                reply = llm.chat_reply(context_texts, history, text)
            except llm.LLMError:
                error = llm.LLM_ERROR_MESSAGE
            else:
                Message.objects.create(conversation=conversation, sender=Message.Sender.USER, content=text)
                Message.objects.create(conversation=conversation, sender=Message.Sender.LLM, content=reply)
                return redirect(_meeting_url("chat", meeting))
    return render(request, "meetalign/chat.html", _ctx(
        request, meeting, chat_messages=conversation.messages.all(), error=error))


@login_required
def anon_chat(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        if text:
            Question.objects.create(meeting=meeting, author=request.user, text=text)
            conversation, _ = Conversation.objects.get_or_create(
                meeting=meeting, owner=request.user, kind=Conversation.Kind.ANONYMOUS)
            Message.objects.create(conversation=conversation, sender=Message.Sender.ANONYMOUS, content=text)
            return redirect(_meeting_url("anon_chat", meeting) + "?sent=1")
    return render(request, "meetalign/anon_chat.html", _ctx(
        request, meeting, sent=request.GET.get("sent") == "1"))


def _question_items(meeting):
    """작성자 정보를 뺀 질문 목록(익명 질문함과 폴링 응답에 공통 사용)."""
    return [
        {"id": q.id, "text": q.text, "answered": hasattr(q, "answer")}
        for q in meeting.questions.select_related("answer").order_by("created_at", "pk")
    ]


@login_required
def inbox(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    return render(request, "meetalign/inbox.html", _ctx(
        request, meeting, questions=_question_items(meeting)))


@login_required
def inbox_feed(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    return JsonResponse({"questions": _question_items(meeting)})


@login_required
def answers(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    _require_leader(request, meeting.project.team)
    if request.method == "POST":
        question_id = request.POST.get("question_id", "")
        if not question_id.isdigit():
            raise Http404
        question = get_object_or_404(Question, pk=int(question_id), meeting=meeting)
        text = request.POST.get("answer", "").strip()
        if text:
            Answer.objects.update_or_create(
                question=question, defaults={"text": text, "answered_by": request.user})
        return redirect(_meeting_url("answers", meeting))
    minutes = meeting.latest_minutes
    questions = list(meeting.questions.select_related("answer").order_by("created_at", "pk"))
    for question in questions:
        if hasattr(question, "answer") or question.draft:
            continue
        try:
            question.draft = llm.draft_answer(minutes.content if minutes else "", question.text)
        except llm.LLMError:
            continue
        question.save(update_fields=["draft"])
    return render(request, "meetalign/answers.html", _ctx(request, meeting, questions=questions))


@login_required
def my_questions(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    questions = (meeting.questions.filter(author=request.user)
                 .select_related("answer").order_by("created_at", "pk"))
    return render(request, "meetalign/my_questions.html", _ctx(request, meeting, questions=questions))


# ---------- 이해도 검증 ----------

@login_required
def understanding(request, team_id, project_id, meeting_id):
    meeting = _meeting(request, team_id, project_id, meeting_id)
    minutes = meeting.latest_minutes
    comprehension = getattr(minutes, "comprehension", None) if minutes else None
    if comprehension is None:
        return render(request, "meetalign/understanding.html", _ctx(request, meeting, comprehension=None))
    questions = list(comprehension.questions.all())
    if request.method == "POST":
        chosen, score = {}, 0
        for q in questions:
            raw = request.POST.get("q_%d" % q.id, "")
            index = int(raw) if raw.isdigit() else None
            chosen[str(q.id)] = index
            if index == q.correct_index:
                score += 1
        attempt = QuizAttempt.objects.create(
            comprehension=comprehension, user=request.user, answers=chosen, score=score)
        return redirect(_meeting_url("understanding", meeting) + "?attempt=%d" % attempt.id)
    attempts = comprehension.attempts.filter(user=request.user)  # 본인 결과만
    result, result_items = None, None
    attempt_id = request.GET.get("attempt", "")
    if attempt_id.isdigit():
        result = attempts.filter(pk=int(attempt_id)).first()
    if result is not None:
        result_items = [
            {"text": q.text, "is_right": result.answers.get(str(q.id)) == q.correct_index}
            for q in questions
        ]
    # 정답(correct_index)은 템플릿에 넘기지 않는다
    items = [{"id": q.id, "text": q.text, "choices": q.choices} for q in questions]
    return render(request, "meetalign/understanding.html", _ctx(
        request, meeting, comprehension=comprehension, questions=items, attempts=attempts,
        result=result, result_items=result_items, total=len(questions),
    ))
