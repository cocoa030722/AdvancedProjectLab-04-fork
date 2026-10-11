# CLAUDE.md

## 프로젝트 개요
- Django 프로젝트 (AdvancedProjectLab-04)
- 루트 디렉터리: `APL_04/` (`manage.py` 위치)
- 설정 패키지: `APL_04/setting/`
- 앱: `APL_04/meetalign/`

## 명령어
`APL_04/` 디렉터리에서 실행한다.

- 개발 서버: `python manage.py runserver`
- 마이그레이션 생성: `python manage.py makemigrations`
- 마이그레이션 적용: `python manage.py migrate`
- 테스트: `python manage.py test`

## 규칙
- 모델 변경 시 마이그레이션을 함께 생성한다.
- 새 기능에는 `tests.py`에 테스트를 추가한다.
- 명세와 검증 기준은 `goal.md`(G 번호)와 `구현사항.txt`를 따른다. 테스트 이름의 g번호가 goal.md 항목에 대응한다.
- 외부 API(STT/LLM)는 테스트에서 항상 mock이다. Anthropic SDK import는 `meetalign/llm.py`에만, 모델 ID 문자열은 `setting/settings.py`에만 둔다.

## 구조 메모
- 권한: 팀 비소속자는 404, 팀원이 팀장 전용 기능을 쓰면 403 (`views.py`의 `_team/_project/_meeting/_require_leader`).
- 회의 처리: `pipeline.py`가 백그라운드 스레드로 STT → 회의록 → 요약 → 문항을 만든다. 테스트는 `MEETALIGN_RUN_PIPELINE_SYNC=True`로 동기 실행한다.
- 환경변수(`.env` 가능, 커밋 금지): `ANTHROPIC_API_KEY`, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DB_ENGINE=postgresql`(+`DB_NAME` 등), `STT_BACKEND`, `CLAUDE_MODEL_*`, 각종 한도(`settings.py` 하단).