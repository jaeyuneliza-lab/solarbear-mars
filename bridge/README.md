# Solar Bear 브리지 (Studio ↔ 서비스 화면)

PDF 하나를 올리면 **Upstage Studio 워크플로(세팅 #22)가 그대로 판정**하고, 그 결과를 서비스 화면 지구본에 바로 띄우는 작은 로컬 서버입니다.
판정 로직은 전부 Studio에 있습니다. 이 서버는 **업로드 → 대기 → 저장 → 표시 → 알림**만 합니다.

```
[서비스 화면] --PDF--> [bridge.py] --/v2/files--> [Upstage]
                                   --/v2/responses (agent + config #22)-->
       <--결과 카드--  results/ 저장 · notifications.jsonl 기록 · (웹훅)
```

## 1. 준비 (처음 한 번)
1. Python 3.9 이상이면 됩니다. 추가 설치는 없습니다.
   - 선택: `pip install pymupdf` 를 하면 PDF의 위성 플룸 이미지도 같이 뜹니다.
2. `.env.example` 을 복사해서 이름을 `.env` 로 바꿉니다.
3. `.env` 의 `UPSTAGE_API_KEY=` 뒤에 Upstage 콘솔(console.upstage.ai → API Keys)에서 받은 키를 **직접** 붙여 넣습니다.
   - 키를 비워 두면 **mock 모드**로 동작합니다. 저장된 #22 결과를 재생하므로 크레딧이 들지 않고, 시연 리허설용입니다.
   - `.env` 는 다른 사람에게 보내거나 GitHub에 올리지 마세요.

## 2. 실행
- `run_bridge.bat` 을 더블클릭합니다. 또는 터미널에서 `python bridge.py` 를 실행합니다.
- 브라우저에서 http://127.0.0.1:8770 이 열립니다.
- 왼쪽 아래에 **Run in Studio** 버튼과 연결 상태가 보입니다.
  - 초록 점 `Studio connected · setting #22`: 실제 API 호출
  - 노란 점 `Bridge in mock mode`: 키 없음, 재생

## 3. 사용
1. **Run in Studio** 를 눌러 케이스 PDF(예: `DEMO-003_all_documents.pdf`)를 고릅니다.
2. 진행 상황이 화면 위쪽에 표시됩니다: 업로드 → Studio 실행 중(보통 몇 분) → 저장.
3. 끝나면 케이스가 지구본에 놓이고 패널이 열립니다. 패널 위에 `Live Studio run` 이 표시됩니다.
4. 결과와 알림은 다음에 남습니다.
   - `results/<케이스>.json`: 다음에 서버를 켜도 다시 뜹니다.
   - `notifications.jsonl`: 판정, 후속 문서 종류, 수신자, 요약.
   - 웹훅 주소를 넣었다면 메시지도 전송됩니다.

## 참고
- GitHub Pages 사이트(정적)에는 이 버튼이 나타나지 않습니다. 브리지로 열었을 때만 보입니다.
- 레포에서 받은 경우: `bridge/` 폴더에서 실행하면 레포 맨 위의 `index.html` 을 화면으로 씁니다.
- 서버는 `127.0.0.1` 에만 열립니다. 같은 컴퓨터에서만 접속되고, 외부에 노출되지 않습니다.
- 실행 1회당 Upstage 크레딧이 듭니다. 같은 파일과 같은 설정이면 7일 동안 캐시된 결과가 돌아올 수 있습니다.
- 심사 대상은 Studio 결과입니다. 이 브리지는 표시와 알림용 보조 도구입니다.
- 오류가 나면 화면에 메시지가 뜹니다. 응답에서 판정 노드 결과를 못 찾으면 원본 응답이 `results/_raw_<job>.json` 으로 저장됩니다.
- 세팅을 바꾸려면 `.env` 의 `STUDIO_CONFIG_ID` 를 바꿉니다(예: #21 = `cfg_geoK2ffFTGH5uM2uacwMgd`).

## 확인된 것 / 아직 확인 안 된 것
- **확인됨:** mock 모드로 PDF 업로드부터 결과 카드, 저장, 알림 기록, 위성 이미지 추출까지 동작합니다. 응답 파서는 #22 16건의 노드 출력을 모두 정확히 분류합니다.
- **미확인 (키를 넣고 첫 실행 때 확인):** 실제 API의 인증 헤더 형식(`Authorization: Bearer`)과 응답의 정확한 모양.
  - 문서 기준으로 구현했습니다.
  - 첫 실행이 실패하면 화면 메시지와 `results/_raw_*.json` 을 보면 원인을 바로 알 수 있습니다.
