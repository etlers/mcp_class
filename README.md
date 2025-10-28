# mcp_class
기본 MCP 서버를 고객 별로 상속 받아서 사용

mcp_servers/
├─ core/
│  ├─ base.py            # BaseMCPServer (템플릿 메서드)
│  ├─ toolkit.py         # Tool 프로토콜/믹스인/등록기
│  └─ settings.py        # 공통 설정(Pydantic)
├─ tools/
│  ├─ k8s.py             # 예: k8s 관련 툴
│  ├─ wiz.py             # 예: wiz 관련 툴
│  ├─ azure.py           # 예: azure 관련 툴
│  ├─ aws.py             # 예: aws 관련 툴
│  └─ prefect.py         # 예: Prefect 트리거 툴
├─ customers/
│  ├─ mcp_cust_01.py     # 고객 01 확장
│  ├─ ...
│  └─ mcp_cust_nn.py     # 고객 nn 확장
└─ main.py               # 실행 진입점: CUSTOMER_ID 환경변수로 고객 서버 선택
