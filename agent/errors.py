"""모든 모드가 같은 모양으로 에러를 돌려주기 위한 예외."""


class AgentError(Exception):
    """호출한 쪽에 {"status": "error", "error": {code, message}} 로 전달되는 예외."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
