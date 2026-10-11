import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """커스텀 User. 지금은 기본 User와 동일하게 시작한다."""


def generate_invite_code():
    return secrets.token_urlsafe(8)


class Team(models.Model):
    name = models.CharField(max_length=100, unique=True)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through="TeamMembership", related_name="teams"
    )
    invite_code = models.CharField(max_length=20, unique=True, default=generate_invite_code)

    def __str__(self):
        return self.name

    def regenerate_invite_code(self):
        self.invite_code = generate_invite_code()
        self.save(update_fields=["invite_code"])


class TeamMembership(models.Model):
    class Role(models.TextChoices):
        LEADER = "leader", "팀장"
        MEMBER = "member", "팀원"

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "user"], name="unique_team_user"),
        ]

    def __str__(self):
        return "%s - %s (%s)" % (self.team, self.user, self.get_role_display())


class Project(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Meeting(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "생성"
        SUBMITTED = "submitted", "제출"
        PROCESSING = "processing", "처리중"
        DONE = "done", "완료"
        FAILED = "failed", "실패"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="meetings")
    title = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CREATED)

    def __str__(self):
        return self.title

    @property
    def team(self):
        return self.project.team


class Recording(models.Model):
    """회의당 단일 녹음."""
    meeting = models.OneToOneField(Meeting, on_delete=models.CASCADE, related_name="recording")
    file = models.FileField(upload_to="recordings/%Y/%m/")
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class Transcript(models.Model):
    """STT 결과 텍스트."""
    recording = models.OneToOneField(Recording, on_delete=models.CASCADE, related_name="transcript")
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class Minutes(models.Model):
    """통일 회의록. 재생성 시 version을 올려 이전 버전을 보존한다."""
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="minutes")
    version = models.PositiveIntegerField(default=1)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["meeting", "version"], name="unique_meeting_minutes_version"),
        ]
        ordering = ["-version"]


class Summary(models.Model):
    minutes = models.OneToOneField(Minutes, on_delete=models.CASCADE, related_name="summary")
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class Question(models.Model):
    """회의별 익명 질문. 작성자는 서버에 저장하되 화면에는 노출하지 않는다."""
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="questions")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="questions",
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.text[:30]


class Answer(models.Model):
    """질문에 대한 답변. 질문자 본인에게만 공개한다."""
    question = models.OneToOneField(Question, on_delete=models.CASCADE, related_name="answer")
    text = models.TextField()
    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="answers",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.text[:30]


class Conversation(models.Model):
    class Kind(models.TextChoices):
        LLM = "llm", "LLM 대화"
        ANONYMOUS = "anonymous", "익명 질문"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="conversations")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.LLM)
    created_at = models.DateTimeField(auto_now_add=True)


class Message(models.Model):
    class Sender(models.TextChoices):
        USER = "user", "사용자"
        LLM = "llm", "LLM"
        ANONYMOUS = "anonymous", "익명 질문자"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField(max_length=10, choices=Sender.choices)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class Comprehension(models.Model):
    """회의록 기반 이해도 검증 문항 묶음. 회의록 버전마다 새로 생성한다."""
    minutes = models.OneToOneField(Minutes, on_delete=models.CASCADE, related_name="comprehension")
    created_at = models.DateTimeField(auto_now_add=True)


class QuizQuestion(models.Model):
    comprehension = models.ForeignKey(Comprehension, on_delete=models.CASCADE, related_name="questions")
    text = models.TextField()
    choices = models.JSONField(default=list)
    correct_index = models.PositiveSmallIntegerField()


class QuizAttempt(models.Model):
    comprehension = models.ForeignKey(Comprehension, on_delete=models.CASCADE, related_name="attempts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quiz_attempts")
    answers = models.JSONField(default=dict)  # {quiz_question_id: 선택한 index}
    score = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
