"""Stand-ins for the parts of FastAPI main.py touches at import time, for
`shyyctl.exe --lite` only.

The background service has no web UI, but main.py defines its routes,
response types and middleware at module level - importing it pulled in
FastAPI, Starlette, pydantic's FastAPI integration and their dependencies:
about 25MB of a ~60MB process, for routes nothing ever serves. With these
registered as the "fastapi" modules first, main.py's module-level code runs
unchanged (decorators hand the function back, the rest are inert), and the
service code it shares with the full app is the same code either way.
HTTPException stays a real exception, since shared helpers raise it.

Never installed in the full app - see the top of main.py.
"""

import sys
import types


class FastAPI:
    def __init__(self, *args, **kwargs):
        pass

    def _route(self, *args, **kwargs):
        return lambda fn: fn

    get = post = put = patch = delete = _route

    def add_middleware(self, *args, **kwargs):
        pass

    def mount(self, *args, **kwargs):
        pass


class HTTPException(Exception):
    def __init__(self, status_code: int = 500, detail=None, headers=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


class UploadFile:
    pass


def File(*args, **kwargs):
    return None


class _Response:
    def __init__(self, *args, **kwargs):
        pass


class StaticFiles(_Response):
    pass


def install():
    if "fastapi" in sys.modules:
        return
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI, fastapi.HTTPException, fastapi.UploadFile, fastapi.File = (
        FastAPI, HTTPException, UploadFile, File)
    responses = types.ModuleType("fastapi.responses")
    for name in ("Response", "FileResponse", "JSONResponse", "StreamingResponse"):
        setattr(responses, name, type(name, (_Response,), {}))
    staticfiles = types.ModuleType("fastapi.staticfiles")
    staticfiles.StaticFiles = StaticFiles
    fastapi.responses, fastapi.staticfiles = responses, staticfiles
    sys.modules.update({"fastapi": fastapi, "fastapi.responses": responses,
                        "fastapi.staticfiles": staticfiles})
