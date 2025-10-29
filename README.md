# mcp_class
## 기본 MCP 서버를 고객 별로 상속 받아서 사용


controller/
└─ app.py                # 메타모스트 서버. 슬래시 명령어를 받고 채널아이디 확인

mcp_servers/
├─ core/
│  ├─ base.py            # BaseMCPServer (템플릿 메서드)
│  ├─ toolkit.py         # Tool 프로토콜/믹스인/등록기
│  └─ settings.py        # 공통 설정(Pydantic)
├─ tools/                # 각 MCP 서버에 필요한 툴 설정
│  ├─ k8s.py             # k8s 관련 툴
│  ├─ wiz.py             # wiz 관련 툴
│  ├─ azure.py           # azure 관련 툴
│  ├─ aws.py             # aws 관련 툴
│  └─ prefect_flows.py   # Prefect 트리거 툴
├─ customers/
│  ├─ mcp_cust_01.py     # 고객 01 확장
│  ├─ ...
│  └─ mcp_cust_nn.py     # 고객 nn 확장
└─ main.py               # 실행 진입점: CUSTOMER_ID 환경변수로 고객 서버 선택

[사용자] (메타모스트 Slash Command)
      │
      ▼
[ Mattermost 서버 ]
      │ (HTTP POST)
      ▼
[ mattermost_proxy / app.py ]
   ├─ 채널 ID(channel_id) 확인
   ├─ channel_id → customer_id 매핑
   ├─ customer_id → MCP 서버 URL 매핑
   ├─ 헤더에 x-customer-id / x-channel-id 삽입
   └─ httpx.AsyncClient 로 해당 MCP 서버에 요청 포워딩
      ▼
[ 고객별 MCP 서버 (예: MCP_01, MCP_02) ]
   ├─ 요청 헤더로 고객정보 확인
   ├─ 필요한 도구(k8s, KIS, Prefect 등) 실행
   └─ 결과 JSON 반환
      ▼
[ mattermost_proxy ]
   └─ 결과를 Mattermost에 JSON 또는 webhook으로 응답
      ▼
[ 사용자의 채널에 결과 출력 ]


## 왜 이 구조가 좋은가
상속으로 공통 흐름 고정(Template Method): BaseMCPServer 가 앱 생성·미들웨어·툴 장착을 표준화.
조합으로 유연한 확장(툴 팩토리/믹스인): 고객별로 필요한 툴만 reg.add(...) 해서 기능 차등.
안전한 격리: 고객마다 다른 설정/권한/기능 세트를 명확히 분리(오남용/권한범위 혼선 방지).
기능 재사용 극대화: 동일 툴 클래스(k8s, Prefect, azure, aws)를 여러 고객에서 설정만 바꿔 재사용.
테스트 편리: 각 툴 라우터가 독립되어 단위 테스트/계약 테스트가 쉬움.

## 확장 팁
플러그인 자동탐색: importlib.metadata.entry_points() 로 tool 엔트리포인트 등록 → 고객 모듈이 툴을 외부 패키지로 제공해도 자동 로딩.
기능 플래그/Capability Matrix: 고객별 허용 엔드포인트를 화이트리스트로 강제.
요청 컨텍스트 주입: before_request() 에서 x-channel-id → 내부 API 헤더/로그에 자동 주입.
버전 고정: /health, /info, /capabilities 로 고객별 기능/버전 노출.
보안: 고객별 API Key/BasicAuth 미들웨어, 속도제한, 감사로그.


+-------------------+
| Mattermost (mock) |   ←→  curl 로 모의 요청
+-------------------+
          │
          ▼
+---------------------------+
| mattermost_proxy (app.py) |
| 포트: 3000                |
| - /mattermost/cmd         |
| - /mm/llm, /mm/quick/...  |
+---------------------------+
   │                 │
   ▼                 ▼
+-----------+     +-----------+
| MCP CUST01|     | MCP CUST02|
| 포트:9001 |     | 포트:9002 |
+-----------+     +-----------+
