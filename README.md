# HY-LMS

Windows + Codex용 비공식 한양대학교 LMS 일정 도우미입니다. LMS 변경을 수집하고, 모델 해석과 독립 Schema QA를 거쳐 로컬 일정·ntfy 알림·Google Calendar에 반영합니다.

**지원:** Windows 11, Python 3.12, 로컬 파일·명령 실행 및 독립 검토용 subagent를 제공하는 Codex. 한양대학교/Google/OpenAI의 공식 제품이 아닙니다. 독립 서버·Docker·Linux 운영은 이 릴리스의 지원 범위가 아닙니다.

## 하는 일

- Canvas/LearningX 자료와 완료·출석·일정 메타데이터 수집
- 변경분 해석 → 형식 검증 → 독립 QA → 원자적 상태 저장
- 새 유형·자료 부족을 사용자에게 막연한 Schema QA 질문으로 떠넘기지 않고 보존/실패 처리
- 직접 확인한 시간표·강의실·온라인/특강 조건을 로컬 SQLite 맥락으로 사용
- ntfy 요약, ICS 내보내기, 전용 Google Calendar 동기화
- 예약 회차 키에 따른 중복 시작 방지와 같은 Codex 채팅 재사용

마감·출석 의무가 없는 자료에 미완료 알림을 임의로 만들지 않습니다. 외부 출력이 성공해도 해석/QA가 실패하면 전체 성공으로 보고하지 않습니다.

## 빠른 시작

새 폴더에서 PowerShell로 실행합니다. 모든 명령은 이 저장소 루트 기준입니다.

```powershell
git clone https://github.com/Gudals0320/hylms.git
cd hylms
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-service-account.txt
.\.venv\Scripts\python.exe -m hylms.setup configure --google-project YOUR_PROJECT_ID --ntfy-topic YOUR_PRIVATE_TOPIC
.\.venv\Scripts\python.exe -m hylms.setup init-state --term 26-2
```

`YOUR_PROJECT_ID`, `YOUR_PRIVATE_TOPIC`, 학기는 본인 값으로 바꿉니다. `configure`는 실제 Windows 프로세스 사용자명을 확인해 `hylms.local.json`에 기록합니다. 키·토큰은 이 파일에 넣지 않습니다. 기존 설정·상태를 덮어쓰지 않습니다. `init-state`는 첫 실제 수집 내용을 빠뜨리지 않도록 명시적인 빈 기준 상태를 만듭니다.

다음으로 [설치와 인증](docs/setup.md)에 따라 Canvas PAT와 본인 Google Calendar 연결을 준비하세요. 인증 설정과 외부 전송 동의가 끝나기 전에는 전체 실행하지 마세요.

```powershell
.\.venv\Scripts\python.exe scripts/install_hylms_skill.py
.\.venv\Scripts\python.exe -m hylms.setup check
```

스킬 설치는 기존 `hylms` 스킬이 있으면 백업 후 교체합니다. 다른 설치 위치를 시험하려면 `--codex-home <별도폴더>`를 사용하세요. 설치된 스킬의 `repository.json`은 로컬 경로만 담으며 Git에 올리지 않습니다. 저장소를 이동하거나 공개본을 업데이트한 뒤에는 스킬을 재설치하세요.

Codex에서 이 프로젝트를 열고 `$hylms`를 명시적으로 호출하세요. 실제 전송 목적지와 데이터 범위를 확인한 사용자 승인 후에만 실행합니다. API 키를 사용하는 별도 LLM 서비스는 없습니다.

## 예약과 승인

정상적인 수동 실행을 먼저 확인한 뒤, 같은 실행 채팅에 직접 예약을 연결하세요. 예: 매일 10:00·19:00, Asia/Seoul. `hylms.local.json`의 `schedule_hours`와 예약 시간을 일치시킵니다. 모델/추론 수준은 사용자가 해당 실행 채팅에서 선택합니다.

PC와 Codex 앱이 실행 중이어야 합니다. 채팅을 아카이브하면 예약을 다시 연결해야 하며, 자동 교체는 제공하지 않습니다. 예약 관리용 고가 모델 채팅을 따로 거치지 않습니다. 반복 승인을 받았더라도 호스트 정책이 작업을 거부할 수 있습니다. 승인 거부는 우회하지 않으며 **영구 무인 실행을 보장하지 않습니다**.

## 개인정보

공개 저장소에는 합성 테스트와 설정 양식만 포함합니다. 본인의 snapshot·다운로드 문서·DB·상태·ICS·인증 자료·실행 로그는 로컬에 남습니다. 저장소가 공개여도 사용자 실행 데이터가 자동 공개되는 것은 아닙니다.

LMS 교수 제공 텍스트/일정 근거는 해석·QA를 위해 Codex 모델에 전달됩니다. ntfy에는 과목·일정·진행 상태·확인 대기 요약이, Google에는 일정 제목·시간·장소·설명·LMS 링크가 전송됩니다. 제출 답안·동료 본문·인증정보·다운로드 문서 본문은 이 경로로 보내지 않습니다. 서비스 제공자의 데이터 처리 정책은 별도로 적용됩니다. ntfy topic을 아는 사람의 접근 가능성을 고려해 고유한 비공개 목적지를 사용하세요.

상세 데이터 경로와 보관 범위는 [보안 안내](SECURITY.md)를 참고하세요.

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
.\.venv\Scripts\python.exe scripts/check_public_tree.py
```

테스트는 `tests/fixtures/config.json`의 가상 설정과 모의 외부 서비스를 사용합니다. 실제 PAT·Google 키·캘린더·ntfy 수신처가 필요하지 않습니다. 첫 실제 설치는 각자의 LMS/Google 환경에서 별도로 확인해야 합니다.

## 라이선스

[MIT](LICENSE). 제3자 라이브러리는 각각의 라이선스를 따릅니다([고지](THIRD_PARTY_NOTICES.md)).
