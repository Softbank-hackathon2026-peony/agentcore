"""AgentCore Runtime 진입 파일 (zip 루트).

런타임은 진입 파일을 스크립트로 실행하므로 패키지 밖(루트)에 둔다.
로컬: python main.py → http://localhost:8080/invocations
"""
from agent.app import app

if __name__ == "__main__":
    app.run()
