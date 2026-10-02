"""AgentCore Runtime 진입점.

로컬 실행: python -m agent.app  → http://localhost:8080/invocations 로 POST
"""
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .handler import handle

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    return handle(payload)


if __name__ == "__main__":
    app.run()
