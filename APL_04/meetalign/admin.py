from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (Answer, Comprehension, Conversation, Meeting, Message, Minutes, Project,
                     Question, QuizAttempt, QuizQuestion, Recording, Summary, Team,
                     TeamMembership, Transcript, User)

admin.site.register(User, UserAdmin)
admin.site.register([Team, TeamMembership, Project, Meeting, Recording, Transcript, Minutes,
                     Summary, Question, Answer, Conversation, Message, Comprehension,
                     QuizQuestion, QuizAttempt])
