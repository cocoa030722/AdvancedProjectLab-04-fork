from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

TERMS_TEXT = "회의 내용(음성 포함)이 외부 LLM/STT API로 전송되는 것에 동의합니다."


class SignupForm(UserCreationForm):
    agree_terms = forms.BooleanField(
        required=True,
        label=TERMS_TEXT,
        error_messages={"required": "약관에 동의해야 가입할 수 있습니다."},
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username",)
