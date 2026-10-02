import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)
RETRY_AFTER_SECONDS = "5"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def unavailable(message: str = "일시적으로 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.") -> ApiError:
    return ApiError(503, "UNAVAILABLE", message, {"Retry-After": RETRY_AFTER_SECONDS})


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None, **extra) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, **extra}, status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        fields = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return error_response(422, "VALIDATION_ERROR", "입력값이 올바르지 않습니다.", fields=fields)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return error_response(exc.status_code, code, str(exc.detail))

    async def _dependency_down(request: Request, exc: Exception):
        log.warning("dependency unavailable: %r", exc)
        err = unavailable()
        return error_response(err.status, err.code, err.message, err.headers)

    # Redis 장애, DB 장애(드라이버 에러 / 연결 거부 같은 OSError)는 모두 503 으로 바꾼다.
    for exc_type in (RedisError, DBAPIError, PoolTimeoutError, OSError):
        app.add_exception_handler(exc_type, _dependency_down)

    async def _unhandled(request: Request, exc: Exception):
        # 예상하지 못한 버그. 스택은 로그에만 남기고 클라이언트에는 공통 형식의 500 을 준다.
        log.exception("unhandled error", exc_info=exc)
        return error_response(500, "INTERNAL", "서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")

    app.add_exception_handler(Exception, _unhandled)
