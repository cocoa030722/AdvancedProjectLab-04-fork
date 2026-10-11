from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from .models import (Answer, Meeting, Minutes, Project, Question, Team,
                     TeamMembership)

User = get_user_model()


class PrototypeFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.team = Team.objects.create(name="t")
        TeamMembership.objects.create(team=self.team, user=self.user, role=TeamMembership.Role.LEADER)
        self.project = Project.objects.create(team=self.team, name="p")
        self.meeting = Meeting.objects.create(project=self.project, title="m")

    def test_index_public(self):
        self.assertEqual(self.client.get(reverse("index")).status_code, 200)

    def test_login_pages_kept_but_do_nothing(self):
        for name in ["signup", "login", "logout"]:
            self.assertRedirects(self.client.post(reverse(name)), reverse("index"))
        for name in ["signup", "login"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_no_login_needed(self):
        r = self.client.get(reverse("meeting_list", args=[self.team.id]))
        self.assertEqual(r.status_code, 200)

    def test_team_create_and_join(self):
        self.client.post(reverse("team_join"), {"action": "create", "name": "new"})
        self.assertTrue(Team.objects.get(name="new").members.filter(pk=self.user.pk).exists())
        self.assertEqual(TeamMembership.objects.get(team__name="new").role, TeamMembership.Role.LEADER)
        other_team = Team.objects.create(name="o")
        self.client.post(reverse("team_join"), {"action": "join", "name": "o"})
        self.assertTrue(other_team.members.filter(pk=self.user.pk).exists())

    def test_non_member_forbidden(self):
        meeting = Meeting.objects.create(
            project=Project.objects.create(team=Team.objects.create(name="other"), name="p"), title="x")
        self.assertEqual(self.client.get(reverse("meeting_detail", args=[meeting.id])).status_code, 404)

    def test_demo_user_created_when_no_users(self):
        User.objects.all().delete()
        self.assertContains(self.client.get(reverse("index")), "demo")

    def test_meeting_create_and_all_screens(self):
        self.client.post(reverse("meeting_create", args=[self.team.id]), {"title": "x"})
        self.assertTrue(Meeting.objects.filter(title="x").exists())
        for name in ["meeting_list", "meeting_create"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.team.id])).status_code, 200)
        for name in ["meeting_detail", "meeting_result", "chat", "inbox",
                     "summary", "answers", "understanding"]:
            self.assertEqual(self.client.get(reverse(name, args=[self.meeting.id])).status_code, 200, name)

    def test_submit_and_end(self):
        url = reverse("meeting_detail", args=[self.meeting.id])
        self.assertRedirects(self.client.post(url, {"action": "submit"}),
                             reverse("meeting_result", args=[self.meeting.id]))
        self.assertRedirects(self.client.post(url, {"action": "end"}),
                             reverse("answers", args=[self.meeting.id]))
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)

    def test_anonymous_question_and_answer(self):
        self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "anon", "text": "why?"})
        q = Question.objects.get()
        self.assertEqual(q.author, self.user)
        self.client.post(reverse("answers", args=[self.meeting.id]), {"question_id": q.id, "answer": "because"})
        q.refresh_from_db()
        self.assertEqual(q.answer.text, "because")
        self.client.post(reverse("answers", args=[self.meeting.id]), {"question_id": q.id, "answer": "again"})
        self.assertEqual(Answer.objects.count(), 1)

    def test_llm_chat_does_not_store(self):
        r = self.client.post(reverse("chat", args=[self.meeting.id]), {"mode": "llm", "text": "hi"})
        self.assertContains(r, "가짜 LLM")
        self.assertEqual(Question.objects.count(), 0)

    def test_meeting_detail_back_link(self):
        r = self.client.get(reverse("meeting_detail", args=[self.meeting.id]))
        self.assertContains(r, 'href="%s"' % reverse("meeting_list", args=[self.team.id]))

    def test_sub_screens_have_back_to_meeting_link(self):
        back = 'href="%s"' % reverse("meeting_detail", args=[self.meeting.id])
        for name in ["meeting_result", "chat", "inbox", "summary", "answers", "understanding"]:
            r = self.client.get(reverse(name, args=[self.meeting.id]))
            self.assertContains(r, back, msg_prefix=name)

class ModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a", password="pw12345!")
        self.team = Team.objects.create(name="t")
        self.project = Project.objects.create(team=self.team, name="p")
        self.meeting = Meeting.objects.create(project=self.project, title="m")

    def test_meeting_defaults_and_team_property(self):
        self.assertEqual(self.meeting.status, Meeting.Status.CREATED)
        self.assertEqual(self.meeting.team, self.team)

    def test_invite_code_unique_and_regenerated(self):
        other = Team.objects.create(name="o")
        self.assertNotEqual(self.team.invite_code, other.invite_code)
        old = self.team.invite_code
        self.team.regenerate_invite_code()
        self.assertNotEqual(self.team.invite_code, old)

    def test_membership_unique(self):
        TeamMembership.objects.create(team=self.team, user=self.user)
        with self.assertRaises(IntegrityError):
            TeamMembership.objects.create(team=self.team, user=self.user)

    def test_minutes_versions_preserved(self):
        Minutes.objects.create(meeting=self.meeting, version=1, content="a")
        Minutes.objects.create(meeting=self.meeting, version=2, content="b")
        self.assertEqual(self.meeting.minutes.first().version, 2)
        with self.assertRaises(IntegrityError):
            Minutes.objects.create(meeting=self.meeting, version=2, content="c")

    def test_question_author_kept_when_user_deleted(self):
        q = Question.objects.create(meeting=self.meeting, author=self.user, text="?")
        self.user.delete()
        q.refresh_from_db()
        self.assertIsNone(q.author)

    def test_cascade_delete_team(self):
        self.team.delete()
        self.assertFalse(Meeting.objects.exists())
