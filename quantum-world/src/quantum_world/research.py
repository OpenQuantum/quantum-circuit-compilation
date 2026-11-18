"""Service adapter for the independently running research workspace."""

import json
import re
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError

from .store import WorldError


PUBLIC_RESEARCH = "/api/agent/research"
WORLD_RESULTS = "/api/world/optimization/v1"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ResearchClient:
    def __init__(self, settings):
        self.url = (settings.research_url or "").rstrip("/")
        self.token = settings.research_token
        self.results_token = getattr(settings, "research_results_token", None)
        self.world_id = settings.world_id

    def _request(self, path, token, *, params=None, body=None, max_bytes=80000):
        if not self.url or not token:
            raise WorldError(
                "RESEARCH_UNCONFIGURED", "Research workspace is not configured"
            )
        url = self.url + path
        if params:
            url += "?" + urlencode(params)
        headers = {"Authorization": "Bearer " + token}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        request = Request(url, data=data, headers=headers)
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=8) as response:
                raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError("Response exceeds budget")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Expected research JSON object")
            return result
        except HTTPError as error:
            raise WorldError(
                "RESEARCH_HTTP_ERROR", f"Research workspace returned HTTP {error.code}"
            ) from None
        except (OSError, ValueError):
            raise WorldError(
                "RESEARCH_UNAVAILABLE",
                "Research workspace unavailable or invalid response",
            ) from None

    def _public(self, query):
        data = self._request(PUBLIC_RESEARCH, self.token, params=query)
        serialized = json.dumps(data, ensure_ascii=False)
        if (
            data.get("scope") != "public_and_official_only"
            or data.get("read_only") is not True
            or data.get("task") != query["task"]
            or not isinstance(data.get("nodes"), list)
            or len(serialized.encode()) > 55000
            or len(serialized) > 23000
        ):
            raise WorldError("RESEARCH_UNAVAILABLE", "Invalid public research response")
        return data

    def query(self, name, params):
        query = {"task": params["task"]}
        if name == "GetResearchNode":
            return self._public({**query, "node_id": params["node_id"]})

        text = params.get("query", "")
        query.update(query=text, limit=params.get("limit", 5))
        if not params.get("fallback") or query["task"] != "invsqrt" or not text.strip():
            return self._public(query)

        # The Workspace matches contiguous text. Try shorter terms from the request
        # before widening to the usual reference-generator vocabulary.
        short_terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,23}|[\u3400-\u9fff]{2,8}", text)
        short_terms = [
            term for term in short_terms
            if (len(term) >= 3 or term.casefold() == "rz" or not term.isascii())
            and term.casefold() not in {
                "invsqrt", "find", "search", "for", "and", "the", "term",
                "candidate", "improvement", "优化", "改进",
            }
        ]
        seen = set()
        for term in [text, *short_terms[:2], "precision", "Newton", ""]:
            if term.casefold() in seen:
                continue
            seen.add(term.casefold())
            data = self._public({**query, "query": term})
            if data["nodes"] or not term:
                return {**data, "matched_query": term}
        return data

    def write_result(self, record):
        if record.get("world_id") != self.world_id or record.get("task") != "invsqrt":
            raise WorldError("RESEARCH_RESULT_SCOPE", "Result is outside this World's experiment scope")
        result = self._request(WORLD_RESULTS + "/experiments", self.results_token, body=record)
        returned = result.get("record")
        if not isinstance(returned, dict) or any(
            returned.get(key) != record.get(key)
            for key in ("world_id", "user_id", "task", "experiment_id")
        ) or not isinstance(result.get("created"), bool):
            raise WorldError("RESEARCH_UNAVAILABLE", "Invalid experiment result response")
        return result

    def history(self, user_id, limit=8):
        scope = {"world_id": self.world_id, "user_id": user_id, "task": "invsqrt"}
        result = self._request(
            WORLD_RESULTS + "/history", self.results_token,
            params={"user_id": user_id, "task": "invsqrt", "limit": limit},
            max_bytes=100000,
        )
        records = result.get("records")
        if (
            any(result.get(key) != value for key, value in scope.items())
            or not isinstance(records, list)
            or len(records) > limit
            or any(not isinstance(record, dict) or any(
                record.get(key) != value for key, value in scope.items()
            ) for record in records)
        ):
            raise WorldError("RESEARCH_UNAVAILABLE", "Invalid experiment history scope")
        return records
