# 설치와 인증

설정 작업은 본인의 일반 Windows 계정으로 수행합니다. Python worker는 그 계정의 Credential Manager와 현재 사용자 범위 DPAPI를 사용합니다. `USERNAME` 환경변수만으로 다른 계정의 실행을 허용하지 않습니다. 모든 Python 명령은 저장소의 `.venv/Scripts/python.exe`로 실행하세요.

## 1. Canvas

본인 LMS에서 허용된 개인 API 토큰(PAT)을 발급합니다. `python hylms_snapshot.py auth rotate`를 실행하면 토큰과 만료일을 대화형으로 받습니다. 토큰은 명령줄 인자·채팅·설정 JSON에 넣지 않습니다.

`python hylms_snapshot.py auth check`는 실제 LMS API에 접근하는 인증 점검입니다. 토큰을 바꾸는 rotate는 이전 토큰 폐기를 시도하므로 기존 설치의 인증을 공유하며 시험하지 마세요.

## 2. Google 서비스 계정과 기존 사용자 소유 캘린더

Google Cloud 프로젝트에서 Calendar API를 활성화하고 전용 서비스 계정을 준비합니다. 이 도구를 위해 프로젝트 Owner/Editor 역할이나 도메인 전체 위임을 부여할 필요는 없습니다. 키 발급이 조직 정책으로 금지된 경우 정책을 우회하지 마세요.

1. 본인 Google Calendar에서 **전용 보조 캘린더**를 준비합니다. 기본 캘린더는 사용하지 않습니다. 서비스 계정으로 캘린더를 생성하지 않습니다.
2. 그 캘린더 하나만 서비스 계정 이메일에 `일정 변경(writer)`으로 공유합니다.
3. 서비스 계정 JSON 키를 본인 PC에 내려받고 등록합니다:

   `python -m hylms.google_calendar auth service-account import --key-file <다운로드한 JSON>`

   등록 시 설정한 프로젝트·계정 형식·키·Google 토큰 엔드포인트를 검증하고 `%LOCALAPPDATA%/HY-LMS/credentials/`에 현재 사용자 DPAPI로 암호화합니다. 평문 원본은 등록 후 자동 삭제하지 않으므로 확인 후 본인이 안전하게 정리하세요.

4. `python -m hylms.setup calendar-marker`를 실행합니다. 출력의 `description`을 전용 캘린더 설명 전체로 설정하고 `installation_id`를 보관합니다. 기존 관리 캘린더의 설명을 바꿔 이전 설치를 덮어쓰지 마세요.
5. 캘린더 설정의 Calendar ID로 연결합니다:

   `python -m hylms.setup bind-calendar --calendar-id <ID> --installation-id <출력된 값>`

   이 명령은 Google 캘린더/일정을 읽고 식별자·설명·권한을 검증한 뒤 로컬 binding만 저장합니다. 기존 binding 또는 기존 HY-LMS 관리 일정이 있으면 중단합니다. 일정이나 캘린더를 생성·수정·삭제하지 않습니다.
6. `python -m hylms.google_calendar auth service-account check`로 조회 검증합니다. 실제 일정 쓰기는 아직 검증된 것이 아닙니다.

요청 scope는 `calendar.events`, `calendar.calendars.readonly`입니다. 서비스 계정 키 삭제·권한 철회·Google 장애가 발생할 수 있으며 영구 인증을 보장하지 않습니다. 장애 시 OAuth로 자동 전환하거나 키를 자동 재발급하지 않습니다.

## 3. 로컬 과목 맥락

선택 사항입니다. 본인이 확인한 과목 ID·시간·강의실·운영 형태를 `hylms.course_context.save_courses(root, term, courses)`로 로컬 SQLite에 저장할 수 있습니다. 가상 형식은 `examples/course-context.json`을 참고하세요. 실제 자료는 Git에서 제외되는 별도 파일로 관리합니다. 온라인 전용·참고용 시간·특강 조건을 구분하며 시간표만으로 출석 의무나 학사 날짜를 만들지 않습니다.

## 4. Codex와 첫 실행

`python scripts/install_hylms_skill.py` 후 Codex 프로젝트에서 `$hylms`를 호출합니다. 스킬은 설치 시 생성된 locator로 저장소를 찾고 그 저장소의 venv Python을 사용합니다.

실행 순서: 수집 → 변경분 해석 → preview 검증 → 독립 Schema QA → 상태 저장 → projection → ntfy → ICS → Google. 실제 변경이 없으면 해석 또는 QA 요청이 생략될 수 있습니다. QA 검토자를 사용할 수 없거나 권한 검토가 거부되면 이를 실패로 보고하며 대체 pass를 만들지 않습니다.

처음에는 생성·수정·완료 제외 항목을 실제 캘린더에서 확인하세요. 개인 운영 설치에서 가져온 state/binding을 이 새 설치에 임의 복사하거나 두 설치를 같은 캘린더에 동시에 연결하지 마세요.

## 복구

- 시작 전 승인 차단: worker 미시작을 확인하고 실제 거부 사유를 해결합니다. 다른 launcher로 우회하지 않습니다.
- 형식 오류: 현재 요청에 대한 제한된 응답 수정만 허용합니다. 같은 회차를 새 키로 다시 실행하지 않습니다.
- QA 실패: state/cursor를 보존하고 다음 정식 실행에서 검토합니다.
- Calendar 실패: 기존 binding과 journal을 보존합니다. 캘린더/일정을 삭제해서 초기화하지 않습니다.
- 학기 전환/기존 설치 이전: 별도 검토가 필요합니다. 초기화 명령은 기존 상태를 덮어쓰지 않습니다.
