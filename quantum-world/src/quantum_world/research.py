"""Read-only service adapter for the independently running research workspace."""

import json
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError

from .store import WorldError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ResearchClient:
    def __init__(self, settings):
        self.url = settings.research_url
        self.token = settings.research_token

    def query(self, name, params):
        if not self.url or not self.token:
            raise WorldError(
                "RESEARCH_UNCONFIGURED", "Research workspace is not configured"
            )
        query = {"task": params["task"]}
        if name == "GetResearchNode":
            query["node_id"] = params["node_id"]
        else:
            query.update(query=params.get("query", ""), limit=params.get("limit", 5))
        request = Request(
            self.url + "/api/agent/research?" + urlencode(query),
            headers={"Authorization": "Bearer " + self.token},
        )
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=8) as response:
                raw = response.read(80001)
            if len(raw) > 80000:
                raise ValueError("Response exceeds budget")
            data = json.loads(raw)
            if (
                data.get("scope") != "public_and_official_only"
                or data.get("read_only") is not True
                or data.get("task") != params["task"]
            ):
                raise ValueError("Unexpected research response")
            if (
                len(json.dumps(data, ensure_ascii=False).encode()) > 55000
                or len(json.dumps(data, ensure_ascii=False)) > 23000
            ):
                raise ValueError("Research result exceeds protocol budget")
            return data
        except HTTPError as error:
            raise WorldError(
                "RESEARCH_HTTP_ERROR", f"Research workspace returned HTTP {error.code}"
            ) from None
        except (OSError, ValueError):
            raise WorldError(
                "RESEARCH_UNAVAILABLE",
                "Research workspace unavailable or invalid response",
            ) from None
