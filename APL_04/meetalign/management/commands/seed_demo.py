"""settings.DEMO_ACCOUNTS의 데모 계정과 데모 팀/프로젝트를 만든다(여러 번 실행해도 안전)."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from meetalign.models import Project, Team, TeamMembership


class Command(BaseCommand):
    help = "데모 계정(settings.DEMO_ACCOUNTS)과 데모 팀/프로젝트를 생성한다."

    def handle(self, *args, **options):
        User = get_user_model()
        users = []
        for account in settings.DEMO_ACCOUNTS:
            user, created = User.objects.get_or_create(username=account["username"])
            user.set_password(account["password"])
            user.save()
            users.append((user, account["role"]))
            self.stdout.write("%s %s" % ("생성" if created else "갱신", user.username))

        team, _ = Team.objects.get_or_create(name="데모 팀")
        for user, role in users:
            TeamMembership.objects.update_or_create(team=team, user=user, defaults={"role": role})
        Project.objects.get_or_create(team=team, name="데모 프로젝트")
        self.stdout.write("데모 팀 초대 코드: %s" % team.invite_code)
