# goal.md — MeetAlign 구현 목표와 검증 기준

`구현사항.txt`의 확정 사항을 클로드 코드가 스스로 검증할 수 있는 형태로 옮긴 문서다.
화면 구성(3.x)과 설계 결정(1~9)이 원본이며, 충돌하면 `구현사항.txt`를 따른다.

## 0. 작업 방식

1. 아래 체크리스트를 위에서부터 구현한다. 각 항목에는 **검증 방법**이 있고, 통과해야 `[x]`로 바꾼다.
2. 항목마다 `APL_04/meetalign/tests.py`에 테스트를 추가한다(CLAUDE.md 규칙). 외부 API(STT/LLM)는 항상 mock이며, 테스트에서 실제 네트워크 호출이 발생하면 실패다.
3. 모델을 바꿀 때마다 마이그레이션을 함께 생성한다.
4. 사람의 판단이 필요한 항목은 **섹션 12**에 모아 두었다. 구현 도중에는 묻지 말고, 섹션 1~11이 모두 `[x]`가 된 뒤 한 번에 사용자에게 검사를 요청한다.
5. `[가정]`이 붙은 값은 구현사항에 수치가 없어 임시로 정한 것이다. 섹션 12에서 사용자가 확정한다.

### 공통 검증 명령 (`APL_04/`에서 실행)

```
python manage.py check
python manage.py makemigrations --check --dry-run   # "No changes detected" 이어야 함
python manage.py test                                # 전체 통과
```

모든 항목 완료의 정의: 위 세 명령이 통과하고, 섹션 1~11의 모든 체크박스가 `[x]`이다.

**진행 상태 (2026-10-11):** 섹션 1~11 전부 `[x]`. `check` 이상 없음, `makemigrations --check` 변경 없음, `python manage.py test` 80개 통과(약 1.5초). 섹션 12는 사용자 검사 대기.

구현 모듈(`APL_04/meetalign/`): `models.py`(상태 머신 포함), `views.py`, `urls.py`, `forms.py`(가입·약관), `stt.py`(STT 래퍼), `llm.py`(클로드 호출 전담), `context.py`(맥락 구성·절삭), `pipeline.py`(백그라운드 처리·타임아웃 복구), `audio.py`(길이 측정), `signals.py`(녹음 파일 삭제). 설정값과 환경변수는 `setting/settings.py` 하단 참고.

---

## 1. 데이터 모델 (구현사항 1.x, 2.1, 5.1)

- [x] **G1.1** 커스텀 `User(AbstractUser)`가 `AUTH_USER_MODEL = 'meetalign.User'`로 설정되어 있다.
  검증: `python manage.py check`, 테스트에서 `get_user_model()._meta.label == 'meetalign.User'`.
- [x] **G1.2** 모델이 존재한다: `Team, TeamMembership, Project, Meeting, Recording, Transcript, Minutes, Summary, Question, Answer, Conversation, Message, Comprehension, QuizQuestion, QuizAttempt`.
  검증: 테스트에서 `apps.get_model('meetalign', 이름)` 전부 성공.
- [x] **G1.3** 관계: `Team 1:N Project 1:N Meeting`, `Team.members`는 `TeamMembership`을 through로 사용, `(team, user)`는 유일.
  검증: 중복 멤버십 생성 시 `IntegrityError` 테스트.
- [x] **G1.4** `Meeting.status`는 `created/submitted/processing/done/failed`만 가진다. `ended` 필드는 없다.
  검증: 테스트에서 choices 값 집합 비교, `hasattr(Meeting, 'ended') is False`.
- [x] **G1.5** `Minutes`는 `(meeting, version)`이 유일하고 재생성해도 이전 버전이 남는다.
- [x] **G1.6** 팀 삭제 시 하위 데이터가 CASCADE 삭제된다. 소프트 삭제 필드(`is_deleted`)는 없다.
- [x] **G1.7** `Question.author`가 저장된다(사용자 삭제 시 `SET_NULL`).
- [x] **G1.8** `Recording` 행이 삭제되면 `MEDIA_ROOT`의 파일도 삭제된다.
  검증: 테스트에서 임시 `MEDIA_ROOT`(override_settings)에 파일 저장 → 행 삭제 → 파일 없음. 회의/팀 삭제로 연쇄 삭제될 때도 동일.
- [x] **G1.9** DB 이식성: 코드에 SQLite 전용 SQL이 없다.
  검증: `APL_04/meetalign/` 아래(마이그레이션 제외)에서 `\.raw\(|\.extra\(|RunSQL|sqlite` 검색 결과 없음.
- [x] **G1.10** `DATABASES`는 환경변수(예: `DB_ENGINE`, `DB_NAME` 등)로 PostgreSQL 전환이 가능하고, 환경변수가 없으면 SQLite를 쓴다.
  검증: 설정 로딩 테스트 또는 환경변수를 바꿔 `python manage.py check` 실행(PostgreSQL 접속 자체는 섹션 12).

## 2. 인증과 권한 (2.x, 8.3)

- [x] **G2.1** 회원가입/로그인/로그아웃이 실제로 동작한다(Django 세션 인증). 목업의 `current_user()`와 "demo" 자동 생성 코드는 제거한다.
  검증: 가입 → 로그인 → `request.user` 인증됨 → 로그아웃 후 보호 페이지 접근 시 302(`LOGIN_URL`). `grep -rn "current_user\|\"demo\"" meetalign/views.py` 결과 없음.
- [x] **G2.2** 비밀번호 검증기는 Django 기본 4종(`UserAttributeSimilarity`, `MinimumLength`, `CommonPassword`, `Numeric`)을 사용한다.
  검증: 약한 비밀번호(`1234`)로 가입 시 사용자 미생성 + 폼 오류.
- [x] **G2.3** 회원가입에는 "회의 내용이 외부 LLM/STT API로 전송됨" 약관 동의 체크가 필수다. 미체크 시 가입 거부.
  검증: 테스트로 체크 없이 POST → 사용자 미생성. 가입 화면 HTML에 해당 문구 포함.
- [x] **G2.4** 로그인 없이 `index`, 가입, 로그인 외의 모든 URL에 접근하면 로그인 페이지로 리다이렉트된다.
  검증: 모든 URL name을 순회하며 비로그인 요청이 302 → `/login/` 계열.
- [x] **G2.5** 팀 소속이 아닌 사용자가 팀/프로젝트/회의/대화/회의록/요약/문항 관련 모든 URL에 접근하면 **404**.
  검증: 모든 팀 하위 URL name을 순회하며 다른 팀 사용자로 요청 → 404.
- [x] **G2.6** 팀 생성자는 `TeamMembership.role='leader'`가 된다.
- [x] **G2.7** 팀 참여는 **초대 코드**로만 한다(팀 이름으로 참여 불가). 잘못된 코드는 오류 메시지를 보이고 멤버십을 만들지 않는다.
- [x] **G2.8** 초대 코드는 무작위 문자열이며 팀마다 유일하다. 팀장만 재발급할 수 있고(팀원은 403), 재발급 후 이전 코드로는 참여할 수 없다.
- [x] **G2.9** 회의 생성과 회의 종료는 팀장만 가능하다. 팀원은 **403**, 팀 비소속자는 404.
  검증: 팀원/팀장/비소속자 세 역할로 해당 POST 요청.
- [x] **G2.10** 팀장 양도/탈퇴 기능은 구현하지 않는다(범위 제외).

## 3. URL과 화면 구조 (3.x, 8.x)

- [x] **G3.1** URL은 아래 계층을 따른다(접두사 `/teams/<team_id>/projects/<project_id>/meetings/<meeting_id>/`). 각 URL에는 `reverse()` 가능한 name이 있다.

| name | 경로(접두사 이후) | 화면 |
|---|---|---|
| `index` | `/` | 3.1 |
| `signup`, `login`, `logout` | `/signup/` 등 | 3.2.1 |
| `team_join` | `/teams/join/` | 3.2.2 (생성/초대 코드 참여) |
| `project_list` | `/teams/<t>/projects/` | 팀 화면: 프로젝트 목록, 초대 코드 (G3.2) |
| `project_create` | `/teams/<t>/projects/new/` | 프로젝트 생성(팀장) (G3.2) |
| `invite_regenerate` | `/teams/<t>/invite/regenerate/` | POST, 초대 코드 재발급(팀장) (G2.8) |
| `meeting_list` | `/teams/<t>/projects/<p>/meetings/` | 3.3.1.1 |
| `meeting_create` | `.../meetings/new/` | 3.3.1.2 |
| `meeting_detail` | `.../meetings/<m>/` | 3.3.2 |
| `meeting_result` | `.../<m>/result/` | 3.3.3 |
| `meeting_status` | `.../<m>/status/` | 상태 JSON (4.4) |
| `meeting_regenerate` | `.../<m>/regenerate/` | POST, 회의록 재생성(팀장) (G5.10) |
| `minutes_download` | `.../<m>/minutes/download/` | txt 다운로드 |
| `recording_file` | `.../<m>/recording/` | 녹음 파일(소속 검사 후 전송) (G10.3) |
| `chat` | `.../<m>/chat/` | 3.3.4.1 LLM 대화 |
| `anon_chat` | `.../<m>/anonymous/` | 3.3.4.1 익명 질문 작성 |
| `inbox` | `.../<m>/inbox/` | 3.3.4.2 익명 질문 수신 |
| `inbox_feed` | `.../<m>/inbox/feed/` | 질문 목록 JSON(폴링용) (G7.8) |
| `summary` | `.../<m>/summary/` | 3.4 |
| `answers` | `.../<m>/answers/` | 3.5 팀장 답변 |
| `my_questions` | `.../<m>/my-questions/` | 질문자가 자신의 질문/답변 확인 |
| `understanding` | `.../<m>/understanding/` | 3.6 |

  검증: 테스트에서 위 name 전부 `reverse()` 성공, 구경로(`/meetings/<id>/` 등)는 404.
- [x] **G3.2** 프로젝트를 생성/선택하는 화면이 있다(팀장 생성). 회의 목록은 프로젝트 단위로 보인다.
- [x] **G3.3** 모든 하위 화면에 상위로 돌아가는 링크가 있다(기존 테스트 `test_sub_screens_have_back_to_meeting_link` 유지·갱신).
- [x] **G3.4** 렌더링은 Django 템플릿 + 바닐라 JS다. `package.json`, SPA 프레임워크 번들이 없다.
- [x] **G3.5** 앱은 `meetalign` 하나다(새 Django 앱 추가 금지).

## 4. 녹음과 상태 머신 (3.x)

- [x] **G4.1** 상태 전이는 `created → submitted → processing → done | failed`, `failed → submitted`(재시도), `done → submitted`(회의록 재생성, G5.10)만 허용한다. 그 외 전이는 예외.
  검증: 전이표 전수 테스트(허용/불허 모든 조합). [구현 중 변경: 재생성(G5.10)이 같은 파이프라인을 타도록 `done → submitted`를 추가함]
- [x] **G4.2** 회의 상세 화면에서 브라우저 녹음(MediaRecorder, webm)과 파일 선택 업로드 두 경로가 있다. 종료 버튼은 녹음 중지, 제출 버튼은 업로드 + `submitted` 전환 + 처리 시작이다.
  검증: 화면 HTML에 `MediaRecorder` 스크립트와 `<input type="file">` 존재. 제출 POST 테스트.
- [x] **G4.3** 회의당 녹음은 1개다(`OneToOne`). `submitted` 이후에는 재제출이 거부된다(`failed`는 예외).
- [x] **G4.4** 허용 포맷은 webm, mp3뿐이다. 그 외 확장자/콘텐츠는 거부(HTTP 400 또는 폼 오류, 파일 미저장).
- [x] **G4.5** 파일 크기 상한 `MAX_RECORDING_BYTES`[가정: 100MB]를 넘으면 거부, 길이 상한 `MAX_RECORDING_SECONDS`[가정: 1800 + 허용 오차 10%]를 넘으면 거부한다. 길이는 업로드 후 서버에서 측정한다.
  검증: 길이 측정 함수는 mock, 경계값(상한/상한+1) 테스트.
- [x] **G4.6** `MEDIA_ROOT`는 로컬 디렉터리이며 git에 올라가지 않는다(`.gitignore`에 `media/`).

## 5. AI 처리 파이프라인 (4.x)

- [x] **G5.1** STT는 wrapper 인터페이스 뒤에 있다. 추상 클래스 `STTBackend.transcribe(path) -> str`를 두고, 설정 `STT_BACKEND`(dotted path)로 구현체를 고른다. 구현체는 `LocalSTT`(개발)와 `ApiSTT`(배포용, 스텁 가능) 두 개다. 언어는 한국어(`ko`)로 고정한다.
  검증: 가짜 백엔드를 설정으로 주입해 파이프라인이 동작하는 테스트. 코드에서 STT 라이브러리를 직접 import하는 곳은 구현체 파일 하나뿐.
- [x] **G5.2** LLM 호출은 `meetalign/llm.py` 한 모듈을 통해서만 한다. 함수: `make_minutes`, `make_summary`, `chat_reply`, `make_quiz`, `draft_answer`. Anthropic SDK import는 이 모듈에만 있다.
  검증: `grep -rn "import anthropic\|from anthropic" APL_04` 결과가 `llm.py` 하나. 테스트는 이 모듈의 함수를 mock.
- [x] **G5.3** 용도별 모델은 `settings.CLAUDE_MODELS = {'minutes':…, 'summary':…, 'chat':…, 'quiz':…, 'answer_draft':…}`로 분리된다. 코드에 모델 ID 문자열이 `settings.py` 밖에 하드코딩되어 있지 않다.
  검증: `grep -rn "claude-" APL_04 --include=*.py` 결과가 `settings.py`뿐.
- [x] **G5.4** API 키는 환경변수 `ANTHROPIC_API_KEY`에서만 읽는다. 저장소에 키가 없고 `.env`는 `.gitignore`에 있다.
  검증: `git grep -n "sk-ant"` 결과 없음, `.gitignore`에 `.env` 포함, 키 없이도 `manage.py check`와 `manage.py test`가 통과.
- [x] **G5.5** 제출 시 백그라운드 스레드가 `STT → Transcript 저장 → make_minutes → Minutes 저장 → make_summary → Summary 저장 → make_quiz → Comprehension 저장` 순서로 실행하고 `done`으로 전환한다. 요청은 즉시 반환된다(스레드 비동기).
  검증: mock 백엔드로 전체 흐름 테스트(테스트에서는 스레드 join 또는 동기 실행 훅 사용), 각 레코드가 생성되고 상태가 `done`.
- [x] **G5.6** 스레드는 종료 시 항상 `django.db.connection.close()`를 호출한다(성공/실패 모두).
  검증: 코드에 `finally`로 호출, 테스트에서 mock으로 호출 횟수 확인.
- [x] **G5.7** 어느 단계든 예외가 나면 `failed`가 되고, 사용자에게는 단일 오류 메시지만 보인다(내부 예외 문자열/스택트레이스 미노출). `failed`에서 재제출하면 다시 처리된다.
  검증: 각 단계 mock이 예외를 던지는 케이스 5개 + 응답 HTML에 예외 문구 없음.
- [x] **G5.8** 타임아웃 복구: `processing`에서 `PROCESSING_TIMEOUT_SECONDS`[가정: 1800]를 넘긴 회의를 `failed`로 바꾸는 함수 `recover_stale_meetings()`가 있고, 앱 시작 시 또는 상태 조회 시 호출된다.
  검증: 시간 조작(`freezegun` 또는 `updated_at` 직접 수정)으로 초과/미초과 케이스.
- [x] **G5.9** `meeting_status`는 `{"status": "<값>"}` JSON을 반환하고, 회의 소속 팀원만 접근 가능하다. `meeting_result` 화면의 JS가 이 URL을 주기적으로 조회(폴링)하고 `done`이 되면 다운로드 버튼을 보인다.
  검증: JSON 응답 테스트, 템플릿에 폴링 스크립트(`fetch(`+`setInterval`/`setTimeout`) 존재.
- [x] **G5.10** 회의록 재생성(팀장, `failed` 아님 `done` 상태에서도 가능)은 `Minutes.version`을 올려 새로 저장하고, 새 버전에 대해 Summary와 Comprehension을 새로 만든다. 이전 버전의 모든 데이터와 `QuizAttempt`는 보존된다. "현재 회의록"은 최신 버전이다.

## 6. 회의록 다운로드와 요약 (3.3.3, 3.4, 7.x)

- [x] **G6.1** `minutes_download`는 최신 `Minutes.content`를 `text/plain; charset=utf-8`로, `Content-Disposition: attachment; filename=….txt`로 반환한다. `done`이 아니면 404 또는 409.
  검증: 응답 헤더·본문 일치 테스트, 팀 비소속자 404.
- [x] **G6.2** `summary` 화면은 최신 버전의 `Summary.content`를 보여준다. 요약 입력은 회의록(Minutes)이며 Transcript가 아니다.
  검증: `make_summary` mock의 호출 인자가 최신 `Minutes.content`임을 확인.
- [x] **G6.3** 회의록 형식은 단순 텍스트다(마크다운/HTML 변환 없음).

## 7. 대화창 (3.3.4, 4.5, 5.x)

- [x] **G7.1** LLM 대화(`chat`)와 익명 질문(`anon_chat`)은 서로 다른 URL/페이지이며, 각 화면에서 다른 쪽으로 가는 토글 링크가 있다. 저장 시 `Conversation.kind`가 각각 `llm`/`anonymous`이다.
- [x] **G7.2** LLM 대화는 `Conversation.owner`가 본인인 것만 보인다. 같은 팀의 다른 팀원이 타인의 대화 기록 URL/내용에 접근할 수 없다(404, 화면 HTML에도 미포함).
  검증: 두 팀원으로 대화 생성 후 교차 접근 테스트.
- [x] **G7.3** LLM 답변은 요청-응답 방식(동기)으로 처리된다. 사용자 메시지와 LLM 응답이 모두 `Message`로 저장된다(`sender`: user/llm).
- [x] **G7.4** LLM 호출 프롬프트에는 같은 프로젝트의 회의 요약(최신 버전)이 오래된 회의부터 순서대로 누적되어 들어간다. 시스템 프롬프트와 현재 질문은 항상 포함된다.
- [x] **G7.5** 맥락 글자 수가 `CONTEXT_CHAR_LIMIT`[가정: 30000자, 구현 시 Claude API 사양 확인 후 조정]을 넘으면 가장 오래된 요약부터 프롬프트에서만 제외한다. DB 행은 삭제하지 않는다.
  검증: 컨텍스트 구성 함수 단위 테스트(경계값, 제외 순서, 시스템 프롬프트/질문 보존, 제외 후 `Summary.objects.count()` 불변).
- [x] **G7.6** 맥락 사용량이 `CONTEXT_CHAR_LIMIT × CONTEXT_WARN_RATIO`[가정: 0.8]를 넘은 프로젝트에서 새 회의 생성 화면(`meeting_create`)을 열면 "기존 회의록이 맥락에서 제외될 수 있음" 경고가 표시된다. 미만이면 표시되지 않는다.
- [x] **G7.7** 익명 질문 작성 시 `Question.author`가 저장되지만, `inbox`와 `answers` 화면의 HTML에는 작성자의 username/이름/id가 전혀 나타나지 않는다(팀장 포함).
  검증: 작성자 username을 눈에 띄는 고유 문자열로 만들어 응답 본문에 없음을 단언.
- [x] **G7.8** 익명 질문 수신 화면(`inbox`)은 폴링으로 새 질문을 갱신한다(새 질문 목록 JSON 엔드포인트 또는 부분 HTML + 폴링 스크립트).

## 8. 답변 (3.5, 6.3)

- [x] **G8.1** 답변 작성은 팀장만 가능(팀원 403). 화면에서 `draft_answer`가 만든 초안이 입력칸에 미리 채워지고, 팀장이 수정해 저장하면 `Answer`(질문당 1개, 재저장 시 갱신)가 된다.
  검증: 초안 mock → 폼 초기값 포함 확인 → 저장 후 `Answer.text`, `answered_by`.
- [x] **G8.2** 답변은 질문자 본인에게만 보인다. `my_questions` 화면은 요청자가 쓴 질문과 그 답변만 표시한다. 다른 팀원의 `my_questions`, `inbox` 등 어디에도 타인 질문의 답변이 노출되지 않는다.
  검증: 질문자 A, 팀원 B, 팀장 C로 응답 본문 교차 확인(C의 `answers` 화면은 팀장 답변 작성용이므로 작성 대상 질문 목록은 보이되 작성자 정보는 없음).

## 9. 이해도 검증 (3.6, 6.1, 6.2)

- [x] **G9.1** 문항 생성(`make_quiz`)은 정확히 객관식 3문항을 반환해야 한다. 응답은 JSON이고, 파싱 실패/형식 불일치(문항 수≠3, 선택지 부족, 정답 index 범위 밖)이면 재시도하며(`QUIZ_MAX_RETRIES`[가정: 2]) 모두 실패하면 처리 실패(`failed`)다.
  검증: 정상/파싱 실패 후 성공/전부 실패 mock 케이스.
- [x] **G9.2** 문항은 회의 처리 완료 시 1회 생성·저장되고(`Comprehension` ↔ `Minutes` 1:1), 요청할 때마다 다시 생성하지 않는다.
- [x] **G9.3** 정답(`correct_index`)은 서버에만 있다. 풀이 화면 HTML/JSON 어디에도 정답 정보가 없다.
  검증: 응답 본문에 정답 선택지가 정답으로 표시되는 속성/필드가 없음을 단언(예: `correct` 문자열, `correct_index` 값).
- [x] **G9.4** 제출하면 서버가 채점해 `QuizAttempt.score`와 선택 답안(`answers`)을 저장한다. 재응시하면 새 `QuizAttempt`가 추가된다.
  검증: 전부 정답/일부 정답/무응답 케이스.
- [x] **G9.5** 결과는 본인만 볼 수 있다. 팀장을 포함해 다른 팀원의 `QuizAttempt` 결과를 보여주는 화면/URL/응답이 존재하지 않는다.
  검증: 팀장 계정으로 팀원의 결과 접근 시도(어떤 URL이든 404), 모든 팀원 대상 응답 본문에 타인 점수 미포함. 코드에 타인 결과를 집계하는 쿼리 없음(리뷰 항목).
- [x] **G9.6** 풀이 화면은 최신 회의록 버전의 문항을 쓴다. 이전 버전의 응시 기록은 보존된다.

## 10. 보안과 구성

- [x] **G10.1** 모든 POST 폼에 `{% csrf_token %}`이 있고, AJAX POST는 CSRF 헤더를 보낸다.
  검증: `Client(enforce_csrf_checks=True)` 테스트, 템플릿 grep.
- [x] **G10.2** `SECRET_KEY`와 `DEBUG`는 환경변수로 덮어쓸 수 있다(기본값은 개발용).
- [x] **G10.3** 업로드된 미디어는 로그인 + 팀 소속 검사를 통과해야만 접근 가능하다(`MEDIA_URL`을 정적으로 열어두지 않는다).
  검증: 비로그인/타팀 사용자로 녹음 파일 접근 시 302/404.

## 11. 테스트 품질

- [x] **G11.1** 외부 API는 항상 mock이다. `anthropic` 클라이언트와 STT 라이브러리는 테스트 실행 중 실제로 호출되지 않는다(키 없는 환경에서 전체 통과).
- [x] **G11.2** 상태 전이, 접근 제어(G2.5 전수), 채점, 맥락 구성에 대한 테스트가 있다.
- [x] **G11.3** `python manage.py test` 소요 시간 120초 이내.

---

## 12. 기계로 검증할 수 없는 항목 (모든 구현 완료 후 사용자에게 한 번에 검사 요청)

섹션 1~11이 모두 `[x]`가 된 뒤, 아래 목록을 그대로 사용자에게 제시하고 확인을 받는다. 각 항목에는 사용자가 하는 확인 행동을 적는다.

### 12.1 확정이 필요한 가정값
- [ ] **M1** `MAX_RECORDING_BYTES`(100MB), `MAX_RECORDING_SECONDS`(1800초+10%), `PROCESSING_TIMEOUT_SECONDS`(1800초), `CONTEXT_CHAR_LIMIT`(30000자), `CONTEXT_WARN_RATIO`(0.8), `QUIZ_MAX_RETRIES`(2)가 적절한지.
- [ ] **M2** `CLAUDE_MODELS`의 용도별 모델 선택(비용/품질)과 Claude API 사양에 맞춘 글자 수 한도.

### 12.2 실제 환경에서만 확인 가능
- [ ] **M3** 실제 `ANTHROPIC_API_KEY`로 회의록/요약/대화/문항/답변 초안이 오류 없이 생성되는지(사용자가 키를 설정하고 시연).
- [ ] **M4** 실제 마이크가 있는 브라우저(Chrome 등)에서 MediaRecorder 녹음 → 제출이 동작하는지, 마이크 권한 거부 시 안내가 나오는지.
- [ ] **M5** 실제 한국어 30분 내외 녹음으로 로컬 STT 처리 시간과 정확도가 허용 범위인지.
- [ ] **M6** PostgreSQL에 실제로 연결해 `migrate`와 전체 흐름이 동작하는지(G1.10 이후 사용자 환경에서).
- [ ] **M7** 서버 재시작 후 `processing`에 멈춘 회의가 타임아웃 뒤 `failed`로 복구되고 재시도되는지(실제 runserver 재시작).

### 12.3 품질·UX 판단
- [ ] **M8** 생성된 회의록이 "통일 회의록"으로 쓸 만한지(구조, 누락, 환각), 요약이 회의록을 적절히 압축하는지.
- [ ] **M9** 이해도 검증 문항 3개가 회의 내용에 맞고 정답이 타당한지, 오답 선택지의 난이도.
- [ ] **M10** 답변 초안과 LLM 대화 응답의 품질, 프로젝트 맥락이 실제로 반영되는지.
- [ ] **M11** 화면 UX: 로딩 바의 체감, 폴링 갱신 주기, 오류 메시지 문구, 맥락 제외 경고 문구, 약관 문구(법적 적정성 포함).
- [ ] **M12** 익명 질문 화면에서 작성자를 알아볼 만한 단서(시각, 문체 노출 등)가 없는지 사람이 눈으로 확인.
- [ ] **M13** 코드 리뷰 관점: 타인의 퀴즈 결과를 집계하는 코드가 없는지(G9.5), 외부로 전송되는 데이터 범위가 약관과 일치하는지.
