from django.urls import include, path

from . import views

meeting_patterns = [
    path("", views.meeting_detail, name="meeting_detail"),
    path("result/", views.meeting_result, name="meeting_result"),
    path("status/", views.meeting_status, name="meeting_status"),
    path("regenerate/", views.meeting_regenerate, name="meeting_regenerate"),
    path("minutes/download/", views.minutes_download, name="minutes_download"),
    path("recording/", views.recording_file, name="recording_file"),
    path("chat/", views.chat, name="chat"),
    path("anonymous/", views.anon_chat, name="anon_chat"),
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/feed/", views.inbox_feed, name="inbox_feed"),
    path("summary/", views.summary, name="summary"),
    path("answers/", views.answers, name="answers"),
    path("my-questions/", views.my_questions, name="my_questions"),
    path("understanding/", views.understanding, name="understanding"),
]

project_patterns = [
    path("meetings/", views.meeting_list, name="meeting_list"),
    path("meetings/new/", views.meeting_create, name="meeting_create"),
    path("meetings/<int:meeting_id>/", include(meeting_patterns)),
]

team_patterns = [
    path("projects/", views.project_list, name="project_list"),
    path("projects/new/", views.project_create, name="project_create"),
    path("invite/regenerate/", views.invite_regenerate, name="invite_regenerate"),
    path("projects/<int:project_id>/", include(project_patterns)),
]

urlpatterns = [
    path("", views.index, name="index"),
    path("signup/", views.signup, name="signup"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("teams/join/", views.team_join, name="team_join"),
    path("teams/<int:team_id>/", include(team_patterns)),
]
