try:
    from astrbot.api.web import json_response, request
except ImportError:

    def json_response(payload: dict[str, object]) -> dict[str, object]:
        return payload

    request = None
