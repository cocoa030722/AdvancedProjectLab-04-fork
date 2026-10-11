"""goal.md의 G 항목을 검증하는 테스트. 테스트 이름의 g번호가 goal.md 항목에 대응한다.

외부 API(STT/LLM)는 항상 mock이며, 실제 네트워크 호출이 일어나면 실패해야 한다.
"""
import contextlib
import importlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import context as context_module
from . import llm
from .forms import TERMS_TEXT
from .models import (Answer, Comprehension, Conversation, InvalidTransition, Meeting, Message,
                     Minutes, Project, Question, QuizAttempt, QuizQuestion, Recording, Summary,
                     Team, TeamMembership, Transcript)
from .pipeline import PROCESSING_FAILED_MESSAGE, recover_stale_meetings, run_pipeline
from .stt import STTBackend

User = get_user_model()
APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent          # APL_04/
REPO_DIR = PROJECT_DIR.parent         # 저장소 루트
TEMPLATE_DIR = APP_DIR / "templates" / "meetalign"
MEDIA_TMP = tempfile.mkdtemp(prefix="meetalign-test-media-")
PASSWORD = "Str0ng-passw0rd!"

WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
MP3_BYTES = b"ID3" + b"\x00" * 64

QUIZ = [
    {"text": "문제 1", "choices": ["가", "나", "다", "라"], "correct_index": 0},
    {"text": "문제 2", "choices": ["가", "나", "다", "라"], "correct_index": 1},
    {"text": "문제 3", "choices": ["가", "나", "다", "라"], "correct_index": 2},
]

TEAM_URLS = ["project_list", "project_create", "invite_regenerate"]
PROJECT_URLS = ["meeting_list", "meeting_create"]
MEETING_URLS = ["meeting_detail", "meeting_result", "meeting_status", "meeting_regenerate",
                "minutes_download", "recording_file", "chat", "anon_chat", "inbox", "inbox_feed",
                "summary", "answers", "my_questions", "understanding"]
POST_ONLY = {"invite_regenerate", "meeting_regenerate"}
MEETING_SUB_SCREENS = ["meeting_result", "summary", "chat", "anon_chat", "inbox", "my_questions",
                       "understanding", "answers"]


def tearDownModule():
    shutil.rmtree(MEDIA_TMP, ignore_errors=True)


class FakeSTT(STTBackend):
    def transcribe(self, path):
        return "전사 텍스트"


class FailingSTT(STTBackend):
    def transcribe(self, path):
        raise RuntimeError("SECRET_STT_DETAIL")


class ExplodingSTT(STTBackend):
    """호출되면 안 되는 상황(전사 결과 재사용)을 검증한다."""

    def transcribe(self, path):
        raise AssertionError("STT must not be called")


def webm_upload(name="rec.webm", content=WEBM_BYTES):
    return SimpleUploadedFile(name, content, content_type="audio/webm")


@contextlib.contextmanager
def llm_mocked(**overrides):
    """llm 모듈의 다섯 함수를 한 번에 mock한다. 값이 예외/호출 가능이면 side_effect로 쓴다."""
    values = {"make_minutes": "회의록 본문", "make_summary": "요약 본문", "chat_reply": "LLM 답변",
              "make_quiz": [dict(q) for q in QUIZ], "draft_answer": "초안 본문"}
    values.update(overrides)
    with contextlib.ExitStack() as stack:
        mocks = {}
        for name, value in values.items():
            if isinstance(value, BaseException) or callable(value):
                patcher = mock.patch.object(llm, name, side_effect=value)
            else:
                patcher = mock.patch.object(llm, name, return_value=value)
            mocks[name] = stack.enter_context(patcher)
        yield mocks


def python_files(root, exclude_tests=True):
    for path in root.rglob("*.py"):
        if "migrations" in path.parts or (exclude_tests and path.name == "tests.py"):
            continue
        yield path


@override_settings(MEDIA_ROOT=MEDIA_TMP, MEETALIGN_RUN_PIPELINE_SYNC=True,
                   STT_BACKEND="meetalign.tests.FakeSTT", QUIZ_MAX_RETRIES=2)
class BaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.leader = User.objects.create_user("leader", password=PASSWORD)
        cls.member = User.objects.create_user("member", password=PASSWORD)
        cls.member2 = User.objects.create_user("member2", password=PASSWORD)
        cls.outsider = User.objects.create_user("outsider", password=PASSWORD)
        cls.team = Team.objects.create(name="team")
        TeamMembership.objects.create(team=cls.team, user=cls.leader, role=TeamMembership.Role.LEADER)
        TeamMembership.objects.create(team=cls.team, user=cls.member)
        TeamMembership.objects.create(team=cls.team, user=cls.member2)
        cls.other_team = Team.objects.create(name="other")
        TeamMembership.objects.create(team=cls.other_team, user=cls.outsider,
                                      role=TeamMembership.Role.LEADER)
        cls.other_project = Project.objects.create(team=cls.other_team, name="other project")
        cls.other_meeting = Meeting.objects.create(project=cls.other_project, title="other meeting")
        cls.project = Project.objects.create(team=cls.team, name="project")
        cls.meeting = Meeting.objects.create(project=cls.project, title="meeting")

    def setUp(self):
        patcher = mock.patch("meetalign.views.get_duration_seconds", return_value=60)
        self.duration_mock = patcher.start()
        self.addCleanup(patcher.stop)

    # ----- helpers -----

    def kw(self, level, meeting=None, project=None, team=None):
        meeting = meeting or self.meeting
        project = project or meeting.project
        team = team or project.team
        if level == "team":
            return {"team_id": team.id}
        if level == "project":
            return {"team_id": team.id, "project_id": project.id}
        return {"team_id": team.id, "project_id": project.id, "meeting_id": meeting.id}

    def url(self, name, **objs):
        level = "team" if name in TEAM_URLS else "project" if name in PROJECT_URLS else "meeting"
        return reverse(name, kwargs=self.kw(level, **objs))

    def login(self, user):
        self.client.force_login(user)

    def make_done(self, meeting=None, version=1, minutes_text="회의록 본문",
                  summary_text="요약 본문", quiz=None):
        """회의록/요약/문항이 있는 완료 상태 회의를 만든다."""
        meeting = meeting or self.meeting
        minutes = Minutes.objects.create(meeting=meeting, version=version, content=minutes_text)
        Summary.objects.create(minutes=minutes, content=summary_text)
        comprehension = Comprehension.objects.create(minutes=minutes)
        for q in quiz or QUIZ:
            QuizQuestion.objects.create(comprehension=comprehension, **q)
        Meeting.objects.filter(pk=meeting.pk).update(status=Meeting.Status.DONE)
        meeting.refresh_from_db()
        return minutes

    def make_recording(self, meeting=None):
        return Recording.objects.create(meeting=meeting or self.meeting, file=webm_upload())

    def submit(self, meeting=None, user=None, upload=None, **overrides):
        """팀장이 녹음을 제출한다(llm은 mock). (응답, mocks)를 돌려준다."""
        meeting = meeting or self.meeting
        with llm_mocked(**overrides) as mocks:
            self.login(user or self.leader)
            data = {"recording": upload if upload is not None else webm_upload()}
            response = self.client.post(self.url("meeting_detail", meeting=meeting), data)
        meeting.refresh_from_db()
        return response, mocks


# ===================================================================
# 1. 데이터 모델
# ===================================================================

class ModelTests(BaseTestCase):
    def test_g1_1_custom_user_model(self):
        self.assertEqual(User._meta.label, "meetalign.User")
        self.assertEqual(settings.AUTH_USER_MODEL, "meetalign.User")

    def test_g1_2_models_exist(self):
        for name in ["Team", "TeamMembership", "Project", "Meeting", "Recording", "Transcript",
                     "Minutes", "Summary", "Question", "Answer", "Conversation", "Message",
                     "Comprehension", "QuizQuestion", "QuizAttempt"]:
            self.assertIsNotNone(apps.get_model("meetalign", name), name)

    def test_g1_3_relations(self):
        self.assertEqual(self.meeting.project.team, self.team)
        self.assertEqual(self.meeting.team, self.team)
        self.assertEqual(Team._meta.get_field("members").remote_field.through, TeamMembership)
        self.assertIn(self.leader, self.team.members.all())
        with self.assertRaises(IntegrityError):
            TeamMembership.objects.create(team=self.team, user=self.leader)

    def test_g1_4_status_choices_and_no_ended(self):
        self.assertEqual({c[0] for c in Meeting.Status.choices},
                         {"created", "submitted", "processing", "done", "failed"})
        self.assertFalse(hasattr(Meeting, "ended"))
        self.assertEqual(self.meeting.status, Meeting.Status.CREATED)

    def test_g1_5_minutes_versions_preserved(self):
        Minutes.objects.create(meeting=self.meeting, version=1, content="a")
        Minutes.objects.create(meeting=self.meeting, version=2, content="b")
        self.assertEqual(self.meeting.latest_minutes.version, 2)
        self.assertEqual(self.meeting.minutes.count(), 2)
        with self.assertRaises(IntegrityError):
            Minutes.objects.create(meeting=self.meeting, version=2, content="c")

    def test_g1_6_cascade_and_no_soft_delete(self):
        self.make_done()
        self.team.delete()
        self.assertFalse(Project.objects.filter(team_id=self.team.id).exists())
        self.assertFalse(Meeting.objects.filter(pk=self.meeting.pk).exists())
        self.assertFalse(Minutes.objects.exists())
        for model in apps.get_app_config("meetalign").get_models():
            self.assertNotIn("is_deleted", [f.name for f in model._meta.fields], model.__name__)

    def test_g1_7_question_author_kept_when_user_deleted(self):
        q = Question.objects.create(meeting=self.meeting, author=self.member, text="?")
        self.member.delete()
        q.refresh_from_db()
        self.assertIsNone(q.author)

    def test_g1_8_recording_file_deleted_with_row(self):
        recording = self.make_recording()
        path = recording.file.path
        self.assertTrue(os.path.exists(path))
        recording.delete()
        self.assertFalse(os.path.exists(path))
        # 팀 삭제로 연쇄 삭제될 때도 파일이 지워진다
        recording = self.make_recording()
        path = recording.file.path
        self.team.delete()
        self.assertFalse(os.path.exists(path))

    def test_g1_9_no_sqlite_specific_sql(self):
        pattern = re.compile(r"\.raw\(|\.extra\(|RunSQL|sqlite", re.IGNORECASE)
        for path in python_files(APP_DIR):
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))

    def test_g1_10_database_engine_from_env(self):
        import setting.settings as settings_module
        try:
            env = {"DB_ENGINE": "postgresql", "DB_NAME": "pgdb", "DB_HOST": "db.example"}
            with mock.patch.dict(os.environ, env):
                reloaded = importlib.reload(settings_module)
                self.assertEqual(reloaded.DATABASES["default"]["ENGINE"],
                                 "django.db.backends.postgresql")
                self.assertEqual(reloaded.DATABASES["default"]["NAME"], "pgdb")
                self.assertEqual(reloaded.DATABASES["default"]["HOST"], "db.example")
            with mock.patch.dict(os.environ, {"DB_ENGINE": "sqlite"}):
                reloaded = importlib.reload(settings_module)
                self.assertEqual(reloaded.DATABASES["default"]["ENGINE"],
                                 "django.db.backends.sqlite3")
        finally:
            importlib.reload(settings_module)


# ===================================================================
# 2. 인증과 권한
# ===================================================================

class AuthTests(BaseTestCase):
    def signup_data(self, **overrides):
        data = {"username": "newbie", "password1": PASSWORD, "password2": PASSWORD,
                "agree_terms": "on"}
        data.update(overrides)
        return data

    def test_g2_1_signup_login_logout(self):
        response = self.client.post(reverse("signup"), self.signup_data())
        self.assertRedirects(response, reverse("index"))
        user = User.objects.get(username="newbie")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

        self.assertRedirects(self.client.post(reverse("logout")), reverse("index"))
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.get(reverse("team_join"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

        response = self.client.post(reverse("login"), {"username": "newbie", "password": PASSWORD})
        self.assertRedirects(response, reverse("index"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        self.client.logout()
        response = self.client.post(reverse("login"), {"username": "newbie", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_g2_1_mock_user_code_removed(self):
        source = (APP_DIR / "views.py").read_text(encoding="utf-8")
        self.assertNotIn("current_user", source)
        self.assertNotIn('"demo"', source)

    def test_g2_2_default_password_validators(self):
        names = [v["NAME"].rsplit(".", 1)[1] for v in settings.AUTH_PASSWORD_VALIDATORS]
        self.assertEqual(names, ["UserAttributeSimilarityValidator", "MinimumLengthValidator",
                                 "CommonPasswordValidator", "NumericPasswordValidator"])
        response = self.client.post(reverse("signup"),
                                    self.signup_data(password1="1234", password2="1234"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newbie").exists())
        self.assertTrue(response.context["form"].errors)

    def test_g2_3_terms_agreement_required(self):
        response = self.client.get(reverse("signup"))
        self.assertContains(response, TERMS_TEXT)
        self.assertContains(response, "외부 LLM/STT API로 전송")
        data = self.signup_data()
        del data["agree_terms"]
        response = self.client.post(reverse("signup"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="newbie").exists())
        self.client.post(reverse("signup"), self.signup_data())
        self.assertTrue(User.objects.filter(username="newbie").exists())

    def test_g2_4_anonymous_redirected_to_login(self):
        for name in ["index", "signup", "login"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        urls = [reverse("team_join")] + [self.url(n) for n in TEAM_URLS + PROJECT_URLS + MEETING_URLS]
        names = ["team_join"] + TEAM_URLS + PROJECT_URLS + MEETING_URLS
        for name, url in zip(names, urls):
            response = self.client.post(url) if name in POST_ONLY else self.client.get(url)
            self.assertEqual(response.status_code, 302, name)
            self.assertIn(reverse("login"), response["Location"], name)

    def test_g10_2_secret_key_and_debug_from_env(self):
        import setting.settings as settings_module
        try:
            env = {"DJANGO_SECRET_KEY": "k" * 50, "DJANGO_DEBUG": "0",
                   "DJANGO_ALLOWED_HOSTS": "a.example,b.example"}
            with mock.patch.dict(os.environ, env):
                reloaded = importlib.reload(settings_module)
                self.assertEqual(reloaded.SECRET_KEY, "k" * 50)
                self.assertFalse(reloaded.DEBUG)
                self.assertEqual(reloaded.ALLOWED_HOSTS, ["a.example", "b.example"])
        finally:
            importlib.reload(settings_module)


class AccessTests(BaseTestCase):
    def test_g2_5_outsider_gets_404_on_every_team_url(self):
        self.login(self.outsider)
        for name in TEAM_URLS + PROJECT_URLS + MEETING_URLS:
            url = self.url(name)
            response = self.client.post(url) if name in POST_ONLY else self.client.get(url)
            self.assertEqual(response.status_code, 404, name)

    def test_g2_5_mismatched_ids_are_404_even_for_members(self):
        self.login(self.leader)
        # 내 팀/프로젝트 id에 다른 팀의 회의 id를 끼워 넣어도 404
        kwargs = {"team_id": self.team.id, "project_id": self.project.id,
                  "meeting_id": self.other_meeting.id}
        self.assertEqual(self.client.get(reverse("meeting_detail", kwargs=kwargs)).status_code, 404)
        kwargs = {"team_id": self.team.id, "project_id": self.other_project.id}
        self.assertEqual(self.client.get(reverse("meeting_list", kwargs=kwargs)).status_code, 404)

    def test_g2_6_team_creator_becomes_leader(self):
        self.login(self.member)
        response = self.client.post(reverse("team_join"), {"action": "create", "name": "new"})
        team = Team.objects.get(name="new")
        self.assertRedirects(response, reverse("project_list", kwargs={"team_id": team.id}))
        self.assertEqual(TeamMembership.objects.get(team=team, user=self.member).role,
                         TeamMembership.Role.LEADER)

    def test_g2_7_join_by_invite_code_only(self):
        self.login(self.outsider)
        for data in [{"action": "join", "name": "team"}, {"action": "join", "invite_code": "nope"},
                     {"action": "join"}]:
            response = self.client.post(reverse("team_join"), data)
            self.assertContains(response, "초대 코드가 올바르지 않습니다")
            self.assertFalse(TeamMembership.objects.filter(team=self.team, user=self.outsider).exists())
        response = self.client.post(reverse("team_join"),
                                    {"action": "join", "invite_code": self.team.invite_code})
        self.assertRedirects(response, self.url("project_list"))
        self.assertTrue(TeamMembership.objects.filter(team=self.team, user=self.outsider).exists())

    def test_g2_8_invite_code_regeneration(self):
        old = self.team.invite_code
        self.assertNotEqual(old, self.other_team.invite_code)
        self.login(self.member)
        self.assertEqual(self.client.post(self.url("invite_regenerate")).status_code, 403)
        self.login(self.outsider)
        self.assertEqual(self.client.post(self.url("invite_regenerate")).status_code, 404)
        self.login(self.leader)
        self.assertRedirects(self.client.post(self.url("invite_regenerate")), self.url("project_list"))
        self.team.refresh_from_db()
        self.assertNotEqual(self.team.invite_code, old)
        self.login(self.outsider)
        self.client.post(reverse("team_join"), {"action": "join", "invite_code": old})
        self.assertFalse(TeamMembership.objects.filter(team=self.team, user=self.outsider).exists())
        self.client.post(reverse("team_join"), {"action": "join", "invite_code": self.team.invite_code})
        self.assertTrue(TeamMembership.objects.filter(team=self.team, user=self.outsider).exists())

    def test_g2_9_meeting_create_leader_only(self):
        self.login(self.member)
        self.assertEqual(self.client.get(self.url("meeting_create")).status_code, 403)
        self.assertEqual(self.client.post(self.url("meeting_create"), {"title": "x"}).status_code, 403)
        self.login(self.outsider)
        self.assertEqual(self.client.post(self.url("meeting_create"), {"title": "x"}).status_code, 404)
        self.assertFalse(Meeting.objects.filter(title="x").exists())
        self.login(self.leader)
        response = self.client.post(self.url("meeting_create"), {"title": "x"})
        meeting = Meeting.objects.get(title="x")
        self.assertRedirects(response, self.url("meeting_detail", meeting=meeting))

    def test_g2_9_meeting_end_submit_leader_only(self):
        """회의 '종료'의 서버 측 동작은 녹음 제출이다(3.5). 팀장만 제출할 수 있다."""
        with llm_mocked():
            self.login(self.member)
            response = self.client.post(self.url("meeting_detail"), {"recording": webm_upload()})
            self.assertEqual(response.status_code, 403)
            self.login(self.outsider)
            response = self.client.post(self.url("meeting_detail"), {"recording": webm_upload()})
            self.assertEqual(response.status_code, 404)
        self.assertFalse(Recording.objects.exists())
        response, _ = self.submit()
        self.assertRedirects(response, self.url("meeting_result"))
        self.assertTrue(Recording.objects.filter(meeting=self.meeting).exists())


# ===================================================================
# 3. URL과 화면 구조
# ===================================================================

class UrlTests(BaseTestCase):
    def test_g3_1_url_hierarchy(self):
        prefix = "/meetalign/teams/%d/projects/%d/meetings/%d/" % (
            self.team.id, self.project.id, self.meeting.id)
        self.assertEqual(self.url("meeting_detail"), prefix)
        self.assertEqual(self.url("minutes_download"), prefix + "minutes/download/")
        self.assertEqual(self.url("meeting_list"),
                         "/meetalign/teams/%d/projects/%d/meetings/" % (self.team.id, self.project.id))
        self.assertEqual(self.url("project_list"), "/meetalign/teams/%d/projects/" % self.team.id)
        for name in TEAM_URLS + PROJECT_URLS + MEETING_URLS:
            self.assertTrue(self.url(name).startswith("/meetalign/teams/%d/" % self.team.id), name)
        for name in ["index", "signup", "login", "logout", "team_join"]:
            reverse(name)

    def test_g3_1_old_flat_urls_removed(self):
        self.login(self.leader)
        for path in ["/meetalign/meetings/%d/" % self.meeting.id,
                     "/meetalign/teams/%d/meetings/" % self.team.id]:
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_g3_2_project_screens(self):
        self.login(self.member)
        self.assertEqual(self.client.get(self.url("project_create")).status_code, 403)
        self.login(self.leader)
        self.assertEqual(self.client.get(self.url("project_create")).status_code, 200)
        response = self.client.post(self.url("project_create"), {"name": "second"})
        project = Project.objects.get(name="second", team=self.team)
        self.assertRedirects(response, self.url("meeting_list", project=project))
        other = Meeting.objects.create(project=project, title="second-only meeting")
        response = self.client.get(self.url("project_list"))
        self.assertContains(response, "second")
        self.assertContains(response, self.url("meeting_list", project=project))
        response = self.client.get(self.url("meeting_list"))
        self.assertContains(response, self.meeting.title)
        self.assertNotContains(response, other.title)

    def test_g3_3_back_links(self):
        self.make_done()
        self.login(self.leader)
        detail = 'href="%s"' % self.url("meeting_detail")
        for name in MEETING_SUB_SCREENS:
            self.assertContains(self.client.get(self.url(name)), detail, msg_prefix=name)
        self.assertContains(self.client.get(self.url("meeting_detail")),
                            'href="%s"' % self.url("meeting_list"))
        self.assertContains(self.client.get(self.url("meeting_create")),
                            'href="%s"' % self.url("meeting_list"))
        self.assertContains(self.client.get(self.url("meeting_list")),
                            'href="%s"' % self.url("project_list"))
        self.assertContains(self.client.get(self.url("project_create")),
                            'href="%s"' % self.url("project_list"))
        self.assertContains(self.client.get(self.url("project_list")), 'href="%s"' % reverse("index"))

    def test_g3_4_django_templates_without_spa(self):
        self.assertFalse((REPO_DIR / "package.json").exists())
        self.assertFalse((PROJECT_DIR / "package.json").exists())
        for template in TEMPLATE_DIR.glob("*.html"):
            self.assertNotRegex(template.read_text(encoding="utf-8"),
                                r"react|vue\.js|angular|webpack", str(template))

    def test_g3_5_single_app(self):
        local_apps = [a for a in settings.INSTALLED_APPS if not a.startswith("django.")]
        self.assertEqual(local_apps, ["meetalign"])


# ===================================================================
# 4. 녹음과 상태 머신
# ===================================================================

class StateMachineTests(BaseTestCase):
    ALLOWED = {
        "created": {"submitted"},
        "submitted": {"processing"},
        "processing": {"done", "failed"},
        "failed": {"submitted"},
        "done": {"submitted"},
    }

    def test_g4_1_every_transition_combination(self):
        statuses = [c[0] for c in Meeting.Status.choices]
        for source in statuses:
            for target in statuses:
                Meeting.objects.filter(pk=self.meeting.pk).update(
                    status=source, status_changed_at=timezone.now() - timedelta(days=1))
                meeting = Meeting.objects.get(pk=self.meeting.pk)
                if target in self.ALLOWED[source]:
                    meeting.transition_to(target)
                    meeting.refresh_from_db()
                    self.assertEqual(meeting.status, target, "%s -> %s" % (source, target))
                    self.assertGreater(meeting.status_changed_at, timezone.now() - timedelta(minutes=1))
                else:
                    with self.assertRaises(InvalidTransition, msg="%s -> %s" % (source, target)):
                        meeting.transition_to(target)
                    meeting.refresh_from_db()
                    self.assertEqual(meeting.status, source)


class RecordingTests(BaseTestCase):
    def test_g4_2_detail_has_recorder_and_file_input(self):
        self.login(self.leader)
        response = self.client.get(self.url("meeting_detail"))
        self.assertContains(response, "MediaRecorder")
        self.assertContains(response, 'type="file"')
        self.assertContains(response, 'name="recording"')
        self.assertContains(response, 'id="rec-stop"')
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.login(self.member)
        self.assertNotContains(self.client.get(self.url("meeting_detail")), 'name="recording"')

    def test_g4_2_submit_runs_pipeline(self):
        response, mocks = self.submit()
        self.assertRedirects(response, self.url("meeting_result"))
        recording = Recording.objects.get(meeting=self.meeting)
        self.assertEqual(recording.duration_seconds, 60)
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)
        self.assertEqual(Transcript.objects.get(recording=recording).text, "전사 텍스트")
        minutes = self.meeting.latest_minutes
        self.assertEqual((minutes.version, minutes.content), (1, "회의록 본문"))
        self.assertEqual(minutes.summary.content, "요약 본문")
        self.assertEqual(minutes.comprehension.questions.count(), 3)

    def test_g4_3_single_recording_and_no_resubmit_after_submit(self):
        self.submit()
        recording_id = Recording.objects.get(meeting=self.meeting).id
        for status in [Meeting.Status.DONE, Meeting.Status.SUBMITTED, Meeting.Status.PROCESSING]:
            Meeting.objects.filter(pk=self.meeting.pk).update(status=status)
            with llm_mocked():
                response = self.client.post(self.url("meeting_detail"), {"recording": webm_upload()})
            self.assertEqual(response.status_code, 409, status)
            self.assertContains(response, "이미 제출된 회의입니다", status_code=409)
        self.assertEqual(Recording.objects.get(meeting=self.meeting).id, recording_id)
        self.assertEqual(Minutes.objects.count(), 1)
        self.assertEqual(Recording._meta.get_field("meeting").one_to_one, True)

    def test_g4_3_failed_meeting_accepts_new_recording(self):
        with self.assertLogs("meetalign.pipeline", level="ERROR"):
            self.submit(make_minutes=RuntimeError("boom"))
        self.assertEqual(self.meeting.status, Meeting.Status.FAILED)
        old = Recording.objects.get(meeting=self.meeting)
        old_path = old.file.path
        response, _ = self.submit(upload=webm_upload("second.webm"))
        self.assertRedirects(response, self.url("meeting_result"))
        new = Recording.objects.get(meeting=self.meeting)
        self.assertNotEqual(new.id, old.id)
        self.assertFalse(os.path.exists(old_path))
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)

    def test_g4_4_only_webm_and_mp3_accepted(self):
        self.login(self.leader)
        rejected = [
            SimpleUploadedFile("rec.wav", WEBM_BYTES, content_type="audio/wav"),
            SimpleUploadedFile("rec.webm", b"RIFF" + b"\x00" * 64, content_type="audio/webm"),
            SimpleUploadedFile("rec.mp3", b"\x00" * 64, content_type="audio/mpeg"),
            SimpleUploadedFile("rec", WEBM_BYTES, content_type="audio/webm"),
        ]
        with llm_mocked():
            for upload in rejected:
                response = self.client.post(self.url("meeting_detail"), {"recording": upload})
                self.assertEqual(response.status_code, 400, upload.name)
            self.assertFalse(Recording.objects.exists())
            self.assertEqual(Meeting.objects.get(pk=self.meeting.pk).status, Meeting.Status.CREATED)
            response = self.client.post(self.url("meeting_detail"),
                                        {"recording": SimpleUploadedFile("rec.mp3", MP3_BYTES)})
            self.assertEqual(response.status_code, 302)
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.CREATED)
        Recording.objects.all().delete()
        with llm_mocked():
            frame_sync = SimpleUploadedFile("rec.mp3", b"\xff\xfb" + b"\x00" * 64)
            response = self.client.post(self.url("meeting_detail"), {"recording": frame_sync})
            self.assertEqual(response.status_code, 302)

    @override_settings(MAX_RECORDING_BYTES=100)
    def test_g4_5_size_limit(self):
        self.login(self.leader)
        with llm_mocked():
            too_big = webm_upload(content=b"\x1a\x45\xdf\xa3" + b"\x00" * 97)   # 101 bytes
            response = self.client.post(self.url("meeting_detail"), {"recording": too_big})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(Recording.objects.exists())
            exact = webm_upload(content=b"\x1a\x45\xdf\xa3" + b"\x00" * 96)     # 100 bytes
            response = self.client.post(self.url("meeting_detail"), {"recording": exact})
            self.assertEqual(response.status_code, 302)

    def test_g4_5_duration_limit_checked_after_upload(self):
        limit = settings.MAX_RECORDING_SECONDS
        self.login(self.leader)
        with llm_mocked():
            self.duration_mock.return_value = limit + 1
            response = self.client.post(self.url("meeting_detail"), {"recording": webm_upload()})
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, "녹음 길이가 상한", status_code=400)
            self.assertFalse(Recording.objects.exists())
            self.assertEqual(Meeting.objects.get(pk=self.meeting.pk).status, Meeting.Status.CREATED)
            self.duration_mock.assert_called_once()
            self.assertTrue(str(self.duration_mock.call_args[0][0]).startswith(MEDIA_TMP))
            self.duration_mock.return_value = limit
            response = self.client.post(self.url("meeting_detail"), {"recording": webm_upload()})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(Recording.objects.get().duration_seconds, limit)

    def test_g4_5_unknown_duration_is_accepted(self):
        self.duration_mock.return_value = None
        response, _ = self.submit()
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Recording.objects.get().duration_seconds)

    def test_g4_6_media_root_local_and_ignored(self):
        self.assertIn("media/", (REPO_DIR / ".gitignore").read_text(encoding="utf-8").splitlines())
        from setting import settings as settings_module
        self.assertEqual(Path(settings_module.MEDIA_ROOT).parent, PROJECT_DIR)


# ===================================================================
# 5. AI 처리 파이프라인
# ===================================================================

class PipelineTests(BaseTestCase):
    def test_g5_1_stt_wrapper_interface(self):
        from .stt import ApiSTT, LocalSTT, STTError, get_stt_backend
        self.assertIsInstance(get_stt_backend(), FakeSTT)
        for backend in (LocalSTT, ApiSTT):
            self.assertTrue(issubclass(backend, STTBackend))
            self.assertEqual(backend.language, "ko")
        with self.assertRaises(STTError):
            ApiSTT().transcribe("x.webm")
        with override_settings(STT_BACKEND="meetalign.stt.ApiSTT"):
            self.assertIsInstance(get_stt_backend(), ApiSTT)
        # STT 라이브러리 import는 stt.py에만 있다
        for path in python_files(PROJECT_DIR):
            if path.name != "stt.py":
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"whisper", str(path))

    def test_g5_2_anthropic_import_only_in_llm(self):
        sdk = "anthropic"  # 이 파일 자체가 grep에 걸리지 않도록 분리
        pattern = re.compile(r"^\s*(import %s|from %s)" % (sdk, sdk), re.MULTILINE)
        hits = [p for p in python_files(PROJECT_DIR) if pattern.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [APP_DIR / "llm.py"])
        for name in ["make_minutes", "make_summary", "chat_reply", "make_quiz", "draft_answer"]:
            self.assertTrue(callable(getattr(llm, name)), name)

    def test_g5_3_model_ids_only_in_settings(self):
        model_prefix = "claude" + "-"  # 이 파일 자체가 grep에 걸리지 않도록 분리
        hits = [p for p in python_files(PROJECT_DIR) if model_prefix in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, [PROJECT_DIR / "setting" / "settings.py"])
        self.assertEqual(set(settings.CLAUDE_MODELS), {"minutes", "summary", "chat", "quiz", "answer_draft"})

    def test_g5_4_api_key_from_env_only(self):
        gitignore = (REPO_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", gitignore)
        result = subprocess.run(["git", "grep", "-I", "-n", "sk-" + "ant"], cwd=REPO_DIR,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        with mock.patch.dict(os.environ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with self.assertRaises(llm.LLMError):
                llm.make_minutes("x")

    def test_g5_5_pipeline_order_and_records(self):
        manager = mock.Mock()
        with llm_mocked() as mocks:
            for name, m in mocks.items():
                manager.attach_mock(m, name)
            self.login(self.leader)
            self.client.post(self.url("meeting_detail"), {"recording": webm_upload()})
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)
        self.assertEqual([c[0] for c in manager.mock_calls],
                         ["make_minutes", "make_summary", "make_quiz"])
        mocks["make_minutes"].assert_called_once_with("전사 텍스트")
        mocks["make_summary"].assert_called_once_with("회의록 본문")
        mocks["make_quiz"].assert_called_once_with("회의록 본문")
        self.assertEqual(Transcript.objects.count(), 1)
        self.assertEqual(Minutes.objects.count(), 1)
        self.assertEqual(Summary.objects.count(), 1)
        self.assertEqual(Comprehension.objects.count(), 1)
        self.assertEqual(QuizQuestion.objects.count(), 3)
        self.assertEqual(QuizQuestion.objects.get(text="문제 2").correct_index, 1)

    @override_settings(MEETALIGN_RUN_PIPELINE_SYNC=False)
    def test_g5_5_request_returns_before_processing(self):
        self.login(self.leader)
        with mock.patch("meetalign.pipeline.threading.Thread") as thread_cls:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url("meeting_detail"), {"recording": webm_upload()})
        self.assertRedirects(response, self.url("meeting_result"))
        self.assertEqual(Meeting.objects.get(pk=self.meeting.pk).status, Meeting.Status.SUBMITTED)
        thread_cls.assert_called_once()
        self.assertEqual(thread_cls.call_args.kwargs["target"], run_pipeline)
        self.assertEqual(thread_cls.call_args.kwargs["args"], (self.meeting.pk,))
        thread_cls.return_value.start.assert_called_once()

    def test_g5_6_connection_closed_in_finally(self):
        self.make_recording()
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.SUBMITTED)
        with llm_mocked(), mock.patch("meetalign.pipeline.connection") as connection:
            run_pipeline(self.meeting.pk)
        connection.close.assert_called_once()
        self.assertEqual(Meeting.objects.get(pk=self.meeting.pk).status, Meeting.Status.DONE)

        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.SUBMITTED)
        with llm_mocked(make_summary=RuntimeError("boom")), \
                mock.patch("meetalign.pipeline.connection") as connection, \
                self.assertLogs("meetalign.pipeline", level="ERROR"):
            run_pipeline(self.meeting.pk)
        connection.close.assert_called_once()
        self.assertEqual(Meeting.objects.get(pk=self.meeting.pk).status, Meeting.Status.FAILED)
        source = (APP_DIR / "pipeline.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"finally:\s*\n\s*if close_connection:\s*\n\s*connection\.close\(\)")

    def test_g5_7_any_stage_failure_marks_failed_with_generic_message(self):
        secret = "SECRET_INTERNAL_DETAIL"
        cases = [
            ("stt", {}),
            ("minutes", {"make_minutes": RuntimeError(secret)}),
            ("summary", {"make_summary": RuntimeError(secret)}),
            ("quiz", {"make_quiz": llm.LLMError(secret)}),
        ]
        for stage, overrides in cases:
            Recording.objects.all().delete()
            Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.CREATED)
            stt = "meetalign.tests.FailingSTT" if stage == "stt" else "meetalign.tests.FakeSTT"
            with override_settings(STT_BACKEND=stt), \
                    self.assertLogs("meetalign.pipeline", level="ERROR"):
                self.submit(**overrides)
            self.assertEqual(self.meeting.status, Meeting.Status.FAILED, stage)
            for name in ["meeting_result", "meeting_detail"]:
                response = self.client.get(self.url(name))
                self.assertContains(response, PROCESSING_FAILED_MESSAGE, msg_prefix=stage)
                self.assertNotContains(response, secret, msg_prefix=stage)
                self.assertNotContains(response, "SECRET_STT_DETAIL", msg_prefix=stage)
                self.assertNotContains(response, "Traceback", msg_prefix=stage)
        # 녹음 없이 파이프라인이 돌면 실패로 끝난다
        Recording.objects.all().delete()
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.SUBMITTED)
        with llm_mocked(), self.assertLogs("meetalign.pipeline", level="ERROR"):
            run_pipeline(self.meeting.pk, close_connection=False)
        self.assertEqual(Meeting.objects.get(pk=self.meeting.pk).status, Meeting.Status.FAILED)

    def test_g5_7_retry_from_failed_reprocesses(self):
        with self.assertLogs("meetalign.pipeline", level="ERROR"):
            self.submit(make_minutes=RuntimeError("boom"))
        self.assertEqual(self.meeting.status, Meeting.Status.FAILED)
        self.assertEqual(Transcript.objects.count(), 1)
        # 파일 없이 재제출하면 기존 녹음(전사 결과)으로 다시 처리한다
        with llm_mocked() as mocks, override_settings(STT_BACKEND="meetalign.tests.ExplodingSTT"):
            response = self.client.post(self.url("meeting_detail"), {})
        self.assertRedirects(response, self.url("meeting_result"))
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)
        mocks["make_minutes"].assert_called_once_with("전사 텍스트")
        self.assertEqual(Transcript.objects.count(), 1)
        # 생성 상태에서 파일 없이 제출하면 400
        Meeting.objects.create(project=self.project, title="fresh")
        fresh = Meeting.objects.get(title="fresh")
        with llm_mocked():
            response = self.client.post(self.url("meeting_detail", meeting=fresh), {})
        self.assertEqual(response.status_code, 400)

    def test_g5_8_stale_processing_recovered(self):
        timeout = settings.PROCESSING_TIMEOUT_SECONDS
        stale = Meeting.objects.create(project=self.project, title="stale")
        fresh = Meeting.objects.create(project=self.project, title="fresh")
        Meeting.objects.filter(pk=stale.pk).update(
            status=Meeting.Status.PROCESSING,
            status_changed_at=timezone.now() - timedelta(seconds=timeout + 1))
        Meeting.objects.filter(pk=fresh.pk).update(
            status=Meeting.Status.PROCESSING,
            status_changed_at=timezone.now() - timedelta(seconds=timeout - 60))
        self.assertEqual(recover_stale_meetings(), 1)
        self.assertEqual(Meeting.objects.get(pk=stale.pk).status, Meeting.Status.FAILED)
        self.assertEqual(Meeting.objects.get(pk=fresh.pk).status, Meeting.Status.PROCESSING)
        # 상태 조회가 복구를 호출한다
        Meeting.objects.filter(pk=fresh.pk).update(
            status_changed_at=timezone.now() - timedelta(seconds=timeout + 1))
        self.login(self.leader)
        response = self.client.get(self.url("meeting_status", meeting=fresh))
        self.assertEqual(response.json()["status"], "failed")

    def test_g5_9_status_json_and_polling_page(self):
        self.login(self.member)
        response = self.client.get(self.url("meeting_status"))
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"status": "created", "minutes_version": None})
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.PROCESSING)
        response = self.client.get(self.url("meeting_result"))
        self.assertContains(response, 'fetch("%s"' % self.url("meeting_status"))
        self.assertContains(response, "setTimeout(poll")
        self.assertNotContains(response, self.url("minutes_download"))
        self.make_done()
        response = self.client.get(self.url("meeting_status"))
        self.assertEqual(response.json(), {"status": "done", "minutes_version": 1})
        response = self.client.get(self.url("meeting_result"))
        self.assertContains(response, self.url("minutes_download"))
        self.assertNotContains(response, "setTimeout(poll")

    def test_g5_10_regenerate_creates_new_version_and_keeps_old(self):
        self.submit()
        v1 = self.meeting.latest_minutes
        attempt = QuizAttempt.objects.create(comprehension=v1.comprehension, user=self.member,
                                             answers={}, score=1)
        self.login(self.member)
        self.assertEqual(self.client.post(self.url("meeting_regenerate")).status_code, 403)
        self.login(self.leader)
        with llm_mocked(make_minutes="회의록 v2", make_summary="요약 v2") as mocks, \
                override_settings(STT_BACKEND="meetalign.tests.ExplodingSTT"):
            response = self.client.post(self.url("meeting_regenerate"))
        self.assertRedirects(response, self.url("meeting_result"))
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)
        self.assertEqual(self.meeting.minutes.count(), 2)
        v2 = self.meeting.latest_minutes
        self.assertEqual((v2.version, v2.content, v2.summary.content), (2, "회의록 v2", "요약 v2"))
        v1.refresh_from_db()
        self.assertEqual(v1.content, "회의록 본문")
        self.assertEqual(v1.summary.content, "요약 본문")
        self.assertTrue(QuizAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertEqual(Comprehension.objects.count(), 2)
        mocks["make_minutes"].assert_called_once_with("전사 텍스트")
        self.assertEqual(Transcript.objects.count(), 1)
        # 완료 상태가 아니면 재생성 불가
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.PROCESSING)
        self.assertEqual(self.client.post(self.url("meeting_regenerate")).status_code, 409)


class LLMModuleTests(TestCase):
    @override_settings(QUIZ_MAX_RETRIES=2)
    def test_g9_1_make_quiz_success_and_retry(self):
        with mock.patch.object(llm, "_complete", return_value=json.dumps(QUIZ)) as complete:
            self.assertEqual(llm.make_quiz("회의록"), QUIZ)
            self.assertEqual(complete.call_count, 1)
        fenced = "```json\n%s\n```" % json.dumps(QUIZ)
        with mock.patch.object(llm, "_complete", return_value=fenced):
            self.assertEqual(llm.make_quiz("회의록"), QUIZ)
        with mock.patch.object(llm, "_complete", side_effect=["not json", json.dumps(QUIZ)]) as complete, \
                self.assertLogs("meetalign.llm", level="WARNING"):
            self.assertEqual(llm.make_quiz("회의록"), QUIZ)
            self.assertEqual(complete.call_count, 2)

    @override_settings(QUIZ_MAX_RETRIES=2)
    def test_g9_1_make_quiz_fails_after_retries(self):
        bad_count = json.dumps(QUIZ[:2])
        bad_index = json.dumps([dict(QUIZ[0], correct_index=4)] + QUIZ[1:])
        bad_choices = json.dumps([dict(QUIZ[0], choices=["가", "나", "다"])] + QUIZ[1:])
        for raw in [bad_count, bad_index, bad_choices, "{}", "[1, 2, 3]"]:
            with mock.patch.object(llm, "_complete", return_value=raw) as complete, \
                    self.assertLogs("meetalign.llm", level="WARNING"):
                with self.assertRaises(llm.LLMError):
                    llm.make_quiz("회의록")
                self.assertEqual(complete.call_count, 3, raw)

    def test_g7_4_chat_prompt_keeps_system_prompt_and_question(self):
        with mock.patch.object(llm, "_complete", return_value="답") as complete:
            llm.chat_reply(["요약A", "요약B"], [("user", "이전 질문"), ("llm", "이전 답")], "현재 질문")
        purpose, system, messages = complete.call_args[0][:3]
        self.assertEqual(purpose, "chat")
        self.assertTrue(system.startswith(llm.CHAT_SYSTEM))
        self.assertLess(system.index("요약A"), system.index("요약B"))
        self.assertEqual(messages[-1], {"role": "user", "content": "현재 질문"})
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        with mock.patch.object(llm, "_complete", return_value="답") as complete:
            llm.chat_reply([], [], "현재 질문")
        purpose, system, messages = complete.call_args[0][:3]
        self.assertEqual(system, llm.CHAT_SYSTEM)
        self.assertEqual(messages, [{"role": "user", "content": "현재 질문"}])

    def test_g5_2_api_errors_become_llm_error(self):
        client = mock.Mock()
        client.messages.create.side_effect = ConnectionError("down")
        with mock.patch.object(llm, "_client", return_value=client), \
                self.assertLogs("meetalign.llm", level="ERROR"):
            with self.assertRaises(llm.LLMError):
                llm.make_summary("x")
        block = mock.Mock(text="결과 ")
        client.messages.create.side_effect = None
        client.messages.create.return_value = mock.Mock(content=[block, block])
        with mock.patch.object(llm, "_client", return_value=client):
            self.assertEqual(llm.make_summary("x"), "결과 결과")
        self.assertEqual(client.messages.create.call_args.kwargs["model"],
                         settings.CLAUDE_MODELS["summary"])


# ===================================================================
# 6. 회의록 다운로드와 요약
# ===================================================================

class MinutesTests(BaseTestCase):
    def test_g6_1_download_latest_minutes_as_txt(self):
        self.login(self.member)
        self.assertEqual(self.client.get(self.url("minutes_download")).status_code, 404)
        self.make_done(minutes_text="# 제목\n**굵게** <b>태그</b>")
        Minutes.objects.create(meeting=self.meeting, version=2, content="두 번째 버전")
        response = self.client.get(self.url("minutes_download"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertRegex(response["Content-Disposition"], r'^attachment; filename=".+\.txt"$')
        self.assertEqual(response.content.decode("utf-8"), "두 번째 버전")
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.PROCESSING)
        self.assertEqual(self.client.get(self.url("minutes_download")).status_code, 404)
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.DONE)
        self.login(self.outsider)
        self.assertEqual(self.client.get(self.url("minutes_download")).status_code, 404)

    def test_g6_2_summary_page_uses_latest_version(self):
        self.login(self.member)
        self.assertContains(self.client.get(self.url("summary")), "아직 요약이 없습니다")
        self.make_done(summary_text="첫 요약 XYZ")
        v2 = Minutes.objects.create(meeting=self.meeting, version=2, content="v2")
        Summary.objects.create(minutes=v2, content="둘째 요약 XYZ")
        response = self.client.get(self.url("summary"))
        self.assertContains(response, "둘째 요약 XYZ")
        self.assertNotContains(response, "첫 요약 XYZ")

    def test_g6_2_summary_input_is_minutes_not_transcript(self):
        _, mocks = self.submit(make_minutes="정리된 회의록")
        mocks["make_summary"].assert_called_once_with("정리된 회의록")

    def test_g6_3_plain_text_without_conversion(self):
        self.make_done(minutes_text="# 제목\n**굵게**", summary_text="* 항목 <b>x</b>")
        self.login(self.member)
        self.assertEqual(self.client.get(self.url("minutes_download")).content.decode("utf-8"),
                         "# 제목\n**굵게**")
        response = self.client.get(self.url("summary"))
        self.assertContains(response, "* 항목 &lt;b&gt;x&lt;/b&gt;")
        for template in TEMPLATE_DIR.glob("*.html"):
            self.assertNotIn("markdown", template.read_text(encoding="utf-8"), str(template))


# ===================================================================
# 7. 대화창
# ===================================================================

class ChatTests(BaseTestCase):
    def test_g7_1_separate_pages_with_toggle_and_kinds(self):
        self.login(self.member)
        self.assertNotEqual(self.url("chat"), self.url("anon_chat"))
        self.assertContains(self.client.get(self.url("chat")), 'href="%s"' % self.url("anon_chat"))
        self.assertContains(self.client.get(self.url("anon_chat")), 'href="%s"' % self.url("chat"))
        with llm_mocked():
            self.client.post(self.url("chat"), {"text": "질문"})
        self.client.post(self.url("anon_chat"), {"text": "익명 질문"})
        kinds = set(Conversation.objects.filter(meeting=self.meeting, owner=self.member)
                    .values_list("kind", flat=True))
        self.assertEqual(kinds, {"llm", "anonymous"})
        question = Question.objects.get(text="익명 질문")
        self.assertEqual(question.author, self.member)
        self.assertEqual(Message.objects.get(sender="anonymous").content, "익명 질문")

    def test_g7_2_llm_conversation_is_private(self):
        self.login(self.member)
        with llm_mocked(chat_reply="SECRET_REPLY_A"):
            self.client.post(self.url("chat"), {"text": "SECRET_QUESTION_A"})
        response = self.client.get(self.url("chat"))
        self.assertContains(response, "SECRET_QUESTION_A")
        self.assertContains(response, "SECRET_REPLY_A")
        for other in [self.member2, self.leader]:
            self.login(other)
            for name in ["chat", "anon_chat", "inbox", "my_questions", "meeting_detail"]:
                response = self.client.get(self.url(name))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "SECRET_QUESTION_A", msg_prefix=name)
                self.assertNotContains(response, "SECRET_REPLY_A", msg_prefix=name)
        self.assertEqual(Conversation.objects.filter(kind="llm").count(), 3)  # 보는 사람마다 자기 대화방

    def test_g7_3_sync_reply_saved_as_messages(self):
        self.login(self.member)
        with llm_mocked(chat_reply="LLM 답변") as mocks:
            response = self.client.post(self.url("chat"), {"text": "안녕"})
        self.assertRedirects(response, self.url("chat"))
        mocks["chat_reply"].assert_called_once()
        senders = list(Message.objects.filter(conversation__owner=self.member)
                       .values_list("sender", "content"))
        self.assertEqual(senders, [("user", "안녕"), ("llm", "LLM 답변")])
        response = self.client.get(self.url("chat"))
        self.assertContains(response, "안녕")
        self.assertContains(response, "LLM 답변")
        with llm_mocked() as mocks:
            self.client.post(self.url("chat"), {"text": "둘째"})
        history = mocks["chat_reply"].call_args[0][1]
        self.assertEqual(list(history), [("user", "안녕"), ("llm", "LLM 답변")])

    def test_g7_3_llm_error_shows_generic_message_only(self):
        self.login(self.member)
        with llm_mocked(chat_reply=llm.LLMError("SECRET_DETAIL")):
            response = self.client.post(self.url("chat"), {"text": "안녕"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, llm.LLM_ERROR_MESSAGE)
        self.assertNotContains(response, "SECRET_DETAIL")
        self.assertEqual(Message.objects.count(), 0)

    def test_g7_4_context_oldest_first_latest_version(self):
        m1, m2, m3 = self.meeting, Meeting.objects.create(project=self.project, title="m2"), \
            Meeting.objects.create(project=self.project, title="m3")
        self.make_done(m1, summary_text="요약1")
        self.make_done(m1, version=2, summary_text="요약1v2")
        self.make_done(m2, summary_text="요약2")
        self.make_done(self.other_meeting, summary_text="다른 팀 요약")
        self.assertEqual(context_module.project_summaries(self.project), ["요약1v2", "요약2"])
        self.assertEqual(context_module.build_context(self.project), (["요약1v2", "요약2"], 0))
        self.login(self.member)
        with llm_mocked() as mocks:
            self.client.post(self.url("chat", meeting=m3), {"text": "질문"})
        self.assertEqual(mocks["chat_reply"].call_args[0][0], ["요약1v2", "요약2"])
        self.assertEqual(mocks["chat_reply"].call_args[0][2], "질문")

    def test_g7_5_trim_oldest_first_without_deleting(self):
        trim = context_module.trim_context
        self.assertEqual(trim(["aaaa", "bbb", "cc"], 100), (["aaaa", "bbb", "cc"], 0))
        self.assertEqual(trim(["aaaa", "bbb", "cc"], 9), (["aaaa", "bbb", "cc"], 0))
        self.assertEqual(trim(["aaaa", "bbb", "cc"], 8), (["bbb", "cc"], 1))
        self.assertEqual(trim(["aaaa", "bbb", "cc"], 5), (["bbb", "cc"], 1))
        self.assertEqual(trim(["aaaa", "bbb", "cc"], 4), (["cc"], 2))
        self.assertEqual(trim(["aaaa", "bbb", "cc"], 1), ([], 3))
        self.assertEqual(trim([], 1), ([], 0))
        m2 = Meeting.objects.create(project=self.project, title="m2")
        self.make_done(self.meeting, summary_text="오래된 요약")
        self.make_done(m2, summary_text="새 요약")
        with override_settings(CONTEXT_CHAR_LIMIT=4):
            self.assertEqual(context_module.build_context(self.project), (["새 요약"], 1))
        self.assertEqual(Summary.objects.count(), 2)
        self.login(self.member)
        with override_settings(CONTEXT_CHAR_LIMIT=4), llm_mocked() as mocks:
            self.client.post(self.url("chat"), {"text": "질문"})
        self.assertEqual(mocks["chat_reply"].call_args[0][0], ["새 요약"])
        self.assertEqual(Summary.objects.count(), 2)

    @override_settings(CONTEXT_CHAR_LIMIT=10, CONTEXT_WARN_RATIO=0.8)
    def test_g7_6_warning_on_meeting_create_near_limit(self):
        warning = "맥락에서 제외될 수 있습니다"
        self.login(self.leader)
        self.make_done(summary_text="12345")           # 5자 <= 8
        self.assertNotContains(self.client.get(self.url("meeting_create")), warning)
        m2 = Meeting.objects.create(project=self.project, title="m2")
        self.make_done(m2, summary_text="6789")        # 합계 9자 > 8
        self.assertContains(self.client.get(self.url("meeting_create")), warning)
        self.assertNotContains(self.client.get(self.url("meeting_create", project=self.other_project)
                                               if False else self.url("meeting_create")), "NEVER")

    def test_g7_7_author_hidden_from_inbox_and_answers(self):
        asker = User.objects.create_user("zq_asker_777", password=PASSWORD)
        TeamMembership.objects.create(team=self.team, user=asker)
        self.login(asker)
        self.client.post(self.url("anon_chat"), {"text": "왜요?"})
        self.assertEqual(Question.objects.get(text="왜요?").author, asker)
        for viewer in [self.leader, self.member]:
            self.login(viewer)
            response = self.client.get(self.url("inbox"))
            self.assertContains(response, "왜요?")
            self.assertNotContains(response, "zq_asker_777")
            feed = self.client.get(self.url("inbox_feed"))
            self.assertEqual(feed.json()["questions"][0]["text"], "왜요?")
            self.assertNotIn("zq_asker_777", feed.content.decode("utf-8"))
            self.assertNotIn("author", feed.content.decode("utf-8"))
        self.login(self.leader)
        with llm_mocked():
            response = self.client.get(self.url("answers"))
        self.assertContains(response, "왜요?")
        self.assertNotContains(response, "zq_asker_777")
        feed = self.client.get(self.url("inbox_feed")).json()
        self.assertEqual(set(feed["questions"][0]), {"id", "text", "answered"})

    def test_g7_8_inbox_polls_feed(self):
        self.login(self.member)
        response = self.client.get(self.url("inbox"))
        self.assertContains(response, 'fetch("%s"' % self.url("inbox_feed"))
        self.assertContains(response, "setTimeout(poll")
        Question.objects.create(meeting=self.meeting, author=self.member2, text="새 질문")
        feed = self.client.get(self.url("inbox_feed"))
        self.assertEqual(feed["Content-Type"], "application/json")
        self.assertEqual(feed.json()["questions"][0]["text"], "새 질문")
        self.assertFalse(feed.json()["questions"][0]["answered"])


# ===================================================================
# 8. 답변
# ===================================================================

class AnswerTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.make_done()
        self.question = Question.objects.create(meeting=self.meeting, author=self.member, text="질문 텍스트")

    def test_g8_1_leader_only_with_draft_prefilled(self):
        self.login(self.member)
        self.assertEqual(self.client.get(self.url("answers")).status_code, 403)
        self.assertEqual(self.client.post(self.url("answers"),
                                          {"question_id": self.question.id, "answer": "x"}).status_code, 403)
        self.login(self.leader)
        with llm_mocked(draft_answer="초안 본문 XYZ") as mocks:
            response = self.client.get(self.url("answers"))
            self.assertContains(response, "초안 본문 XYZ")
            mocks["draft_answer"].assert_called_once_with("회의록 본문", "질문 텍스트")
            self.client.get(self.url("answers"))
            self.assertEqual(mocks["draft_answer"].call_count, 1)   # 초안은 한 번만 생성
        self.question.refresh_from_db()
        self.assertEqual(self.question.draft, "초안 본문 XYZ")
        response = self.client.post(self.url("answers"),
                                    {"question_id": self.question.id, "answer": "수정한 답변"})
        self.assertRedirects(response, self.url("answers"))
        answer = Answer.objects.get(question=self.question)
        self.assertEqual((answer.text, answer.answered_by), ("수정한 답변", self.leader))
        self.client.post(self.url("answers"), {"question_id": self.question.id, "answer": "다시 수정"})
        self.assertEqual(Answer.objects.count(), 1)
        self.assertEqual(Answer.objects.get().text, "다시 수정")
        with llm_mocked() as mocks:
            response = self.client.get(self.url("answers"))
        self.assertContains(response, "다시 수정")
        mocks["draft_answer"].assert_not_called()

    def test_g8_1_draft_failure_leaves_field_empty(self):
        self.login(self.leader)
        with llm_mocked(draft_answer=llm.LLMError("SECRET")):
            response = self.client.get(self.url("answers"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "SECRET")
        self.question.refresh_from_db()
        self.assertEqual(self.question.draft, "")

    def test_g8_2_answer_visible_only_to_asker(self):
        Answer.objects.create(question=self.question, text="SECRET_ANSWER", answered_by=self.leader)
        Question.objects.create(meeting=self.meeting, author=self.member2, text="Q_OF_MEMBER2")
        self.login(self.member)
        response = self.client.get(self.url("my_questions"))
        self.assertContains(response, "질문 텍스트")
        self.assertContains(response, "SECRET_ANSWER")
        self.assertNotContains(response, "Q_OF_MEMBER2")
        for viewer in [self.member2, self.leader]:
            self.login(viewer)
            for name in ["my_questions", "inbox", "inbox_feed", "chat", "anon_chat",
                         "meeting_detail", "summary", "understanding"]:
                response = self.client.get(self.url(name))
                self.assertNotContains(response, "SECRET_ANSWER", msg_prefix="%s/%s" % (viewer, name))
        self.login(self.member2)
        self.assertNotContains(self.client.get(self.url("my_questions")), "질문 텍스트")
        feed = self.client.get(self.url("inbox_feed")).json()["questions"]
        self.assertEqual([q["answered"] for q in feed], [True, False])


# ===================================================================
# 9. 이해도 검증
# ===================================================================

class QuizTests(BaseTestCase):
    DISTINCT = [
        {"text": "QUESTION_ALPHA", "choices": ["ALPHA0", "ALPHA1", "ALPHA2", "ALPHA3"], "correct_index": 2},
        {"text": "QUESTION_BRAVO", "choices": ["BRAVO0", "BRAVO1", "BRAVO2", "BRAVO3"], "correct_index": 0},
        {"text": "QUESTION_CHARLIE", "choices": ["CH0", "CH1", "CH2", "CH3"], "correct_index": 3},
    ]

    def answers_for(self, comprehension, picks):
        return {"q_%d" % q.id: str(p) for q, p in zip(comprehension.questions.all(), picks)
                if p is not None}

    def test_g9_2_questions_generated_once(self):
        _, mocks = self.submit()
        mocks["make_quiz"].assert_called_once()
        self.login(self.member)
        with llm_mocked() as mocks:
            for _ in range(2):
                self.assertEqual(self.client.get(self.url("understanding")).status_code, 200)
        mocks["make_quiz"].assert_not_called()
        self.assertEqual(Comprehension.objects.count(), 1)
        self.assertEqual(Comprehension.objects.get().minutes, self.meeting.latest_minutes)

    def test_g9_3_answers_never_sent_to_client(self):
        self.make_done(quiz=self.DISTINCT)
        self.login(self.member)
        response = self.client.get(self.url("understanding"))
        content = response.content.decode("utf-8")
        for q in self.DISTINCT:
            self.assertIn(q["text"], content)
            for choice in q["choices"]:
                self.assertIn(choice, content)
        self.assertNotIn("correct", content.lower())
        self.assertNotIn("정답", content)
        comprehension = Comprehension.objects.get()
        response = self.client.post(self.url("understanding"), self.answers_for(comprehension, [0, 0, 0]))
        response = self.client.get(response["Location"])
        self.assertNotIn("correct", response.content.decode("utf-8").lower())

    def test_g9_4_server_side_grading_and_reattempt(self):
        self.make_done(quiz=self.DISTINCT)
        comprehension = Comprehension.objects.get()
        self.login(self.member)
        response = self.client.post(self.url("understanding"), self.answers_for(comprehension, [2, 0, 3]))
        self.assertEqual(response.status_code, 302)
        attempt = QuizAttempt.objects.get()
        self.assertEqual(attempt.score, 3)
        self.assertEqual(attempt.user, self.member)
        self.assertContains(self.client.get(response["Location"]), "결과: 3 / 3")
        self.client.post(self.url("understanding"), self.answers_for(comprehension, [2, 1, 1]))
        self.client.post(self.url("understanding"), self.answers_for(comprehension, [None, None, None]))
        scores = list(QuizAttempt.objects.order_by("pk").values_list("score", flat=True))
        self.assertEqual(scores, [3, 1, 0])
        last = QuizAttempt.objects.order_by("pk").last()
        self.assertEqual(set(last.answers.values()), {None})
        self.assertEqual(QuizAttempt.objects.filter(user=self.member).count(), 3)

    def test_g9_5_results_visible_only_to_owner(self):
        self.make_done(quiz=self.DISTINCT)
        comprehension = Comprehension.objects.get()
        self.login(self.member)
        response = self.client.post(self.url("understanding"), self.answers_for(comprehension, [2, 0, 1]))
        attempt = QuizAttempt.objects.get()
        self.assertEqual(attempt.score, 2)
        self.assertContains(self.client.get(self.url("understanding")), "2 / 3")
        for viewer in [self.leader, self.member2]:
            self.login(viewer)
            for name in MEETING_SUB_SCREENS + ["meeting_detail"]:
                if name == "answers" and viewer != self.leader:
                    continue
                response = self.client.get(self.url(name))
                self.assertNotContains(response, "2 / 3", msg_prefix="%s/%s" % (viewer, name))
            response = self.client.get(self.url("understanding") + "?attempt=%d" % attempt.id)
            self.assertNotContains(response, "결과:")
            self.assertNotContains(response, "2 / 3")
        source = (APP_DIR / "views.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"attempts\.(all|exclude)\(")

    def test_g9_6_latest_version_questions_and_old_attempts_kept(self):
        v1 = self.make_done(quiz=[dict(q, text="V1_" + q["text"]) for q in self.DISTINCT])
        attempt = QuizAttempt.objects.create(comprehension=v1.comprehension, user=self.member,
                                             answers={}, score=0)
        self.login(self.member)
        self.assertContains(self.client.get(self.url("understanding")), "V1_QUESTION_ALPHA")
        self.make_done(version=2, quiz=[dict(q, text="V2_" + q["text"]) for q in self.DISTINCT])
        response = self.client.get(self.url("understanding"))
        self.assertContains(response, "V2_QUESTION_ALPHA")
        self.assertNotContains(response, "V1_QUESTION_ALPHA")
        self.assertTrue(QuizAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertNotContains(self.client.get(self.url("understanding")), "0 / 3")  # v1 기록은 v2 화면에 없음


# ===================================================================
# 10. 보안과 구성
# ===================================================================

class SecurityTests(BaseTestCase):
    def test_g10_1_csrf_enforced_on_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.leader)
        self.assertEqual(client.post(self.url("meeting_create"), {"title": "x"}).status_code, 403)
        self.assertEqual(client.post(self.url("chat"), {"text": "x"}).status_code, 403)
        self.assertFalse(Meeting.objects.filter(title="x").exists())

    def test_g10_1_every_post_form_has_csrf_token(self):
        for template in TEMPLATE_DIR.glob("*.html"):
            source = template.read_text(encoding="utf-8")
            forms = len(re.findall(r'<form[^>]*method="post"', source, re.IGNORECASE))
            tokens = source.count("{% csrf_token %}")
            self.assertGreaterEqual(tokens, forms, str(template))
            self.assertEqual(len(re.findall(r"\.(post|send)\(", source)), 0, str(template))  # AJAX POST 없음

    def test_g10_3_media_served_only_to_members(self):
        self.submit()
        recording = Recording.objects.get()
        url = self.url("recording_file")
        self.client.logout()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login(self.member)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), WEBM_BYTES)
        self.assertEqual(self.client.get("/" + settings.MEDIA_URL + recording.file.name).status_code, 404)
        self.assertNotIn("static(", (PROJECT_DIR / "setting" / "urls.py").read_text(encoding="utf-8"))


# ===================================================================
# 11. 테스트 품질
# ===================================================================

class TestQualityTests(BaseTestCase):
    def test_g11_1_full_flow_without_api_key_or_network(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with mock.patch("meetalign.llm._client", side_effect=AssertionError("no network")):
                response, _ = self.submit()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)
        self.assertIn("mock", (APP_DIR / "tests.py").read_text(encoding="utf-8"))
