"""Small, synchronous DeepSeek adapter with bounded retries and safe errors."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from .config import Settings
from .extraction import split_turns, validate_extraction
from .schemas import AnalysisInput, CompanyResolution, Extraction, SearchResult

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
PROMPTS = Path(__file__).with_name("prompts")


class DeepSeekError(Exception):
    """Only a stable code and a credential-free summary cross the provider boundary."""

    def __init__(self, code: str, message: str = "DeepSeek 服务暂不可用"):
        super().__init__(message)
        self.code = code


class DeepSeekClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.request_count = 0
        self._http = httpx.Client(
            base_url=settings.base_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
            transport=transport,
            # Keep provider requests independent of system/environment proxy changes.
            trust_env=False,
            follow_redirects=False,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeepSeekError("deadline_exceeded", "本次分析已达到总时限，请稍后重试")
        return remaining

    def _request_json(self, path: str, payload: dict, deadline: float) -> dict:
        if not self.settings.api_key:
            raise DeepSeekError("missing_api_key", "尚未配置 DeepSeek API 密钥")
        for attempt in range(3):
            timeout = min(30.0, self.settings.timeout, self._remaining(deadline))
            try:
                self.request_count += 1
                response = self._http.post(path, json=payload, timeout=timeout)
            except (httpx.TimeoutException, httpx.TransportError):
                error = DeepSeekError("provider_unavailable", "DeepSeek 请求超时或连接暂不可用")
            else:
                self._remaining(deadline)
                status = response.status_code
                if 200 <= status < 300:
                    try:
                        result = response.json()
                        if not isinstance(result, dict):
                            raise ValueError
                        return result
                    except (ValueError, TypeError):
                        raise DeepSeekError(
                            "invalid_response", "DeepSeek 返回了无法解析的数据"
                        ) from None
                permanent = {
                    401: ("authentication_failed", "DeepSeek API 身份验证失败，请检查密钥配置"),
                    402: ("insufficient_balance", "DeepSeek 账户余额不足"),
                    403: ("permission_denied", "DeepSeek 账户没有使用该服务的权限"),
                    404: (
                        "model_unavailable"
                        if path == "chat/completions"
                        else "endpoint_unavailable",
                        "配置的模型或服务端点不可用",
                    ),
                }
                if status in permanent:
                    raise DeepSeekError(*permanent[status])
                if status != 429 and not 500 <= status < 600:
                    raise DeepSeekError(
                        "request_rejected", "DeepSeek 拒绝了请求，请检查模型和服务配置"
                    )
                error = DeepSeekError("provider_unavailable", "DeepSeek 服务繁忙或暂不可用")
            if attempt == 2:
                raise error from None
            delay = attempt + 1
            if self._remaining(deadline) <= delay:
                raise DeepSeekError("deadline_exceeded", "剩余分析时间不足以继续重试") from None
            time.sleep(delay)
        raise AssertionError("unreachable")

    def chat_json(
        self, prompt: str, payload: dict, schema: type[ResponseModel], deadline: float
    ) -> ResponseModel:
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {"data": payload, "schema": schema.model_json_schema()}, ensure_ascii=False
                ),
            },
        ]
        for repair in range(2):
            try:
                data = self._request_json(
                    "chat/completions",
                    {
                        "model": self.settings.model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "thinking": {"type": self.settings.thinking},
                        "temperature": 0,
                        "max_tokens": 6000,
                    },
                    deadline,
                )
                choice = data["choices"][0]
                content = choice["message"]["content"]
                if (
                    choice.get("finish_reason") == "length"
                    or not isinstance(content, str)
                    or not content.strip()
                ):
                    raise ValueError
                return schema.model_validate_json(content)
            except DeepSeekError as error:
                if error.code != "invalid_response":
                    raise
            except (AttributeError, KeyError, IndexError, TypeError, ValueError, ValidationError):
                pass
            if repair == 0:
                # Never include raw invalid output: it may contain credentials or injected instructions.
                messages.append(
                    {
                        "role": "user",
                        "content": "修复上次格式错误：仅输出符合给定 schema 的完整 JSON 对象。未知值必须为 null，不得输出 Markdown、解释或补造证据。",
                    }
                )
        raise DeepSeekError("invalid_response", "DeepSeek 未能返回符合结构要求的完整结果")

    def extract(self, input: AnalysisInput, deadline: float) -> Extraction:
        prompt = (PROMPTS / "extraction.md").read_text(encoding="utf-8")
        extracted = self.chat_json(
            prompt,
            {
                "turns": [turn.model_dump() for turn in split_turns(input.transcript)],
                "call_time": input.call_time,
            },
            Extraction,
            deadline,
        )
        return validate_extraction(extracted, input)

    def search(self, query: str, deadline: float) -> list[SearchResult]:
        if not self.settings.search_enabled:
            raise DeepSeekError("search_disabled", "企业检索已关闭")
        data = self._request_json(
            "v1/tools/search", {"query": query[:128], "count": 5, "need_content": True}, deadline
        )
        try:
            data = data.get("data", data)
            rows = data["results"]
            if not isinstance(rows, list):
                raise ValueError
            now = datetime.now(timezone.utc).isoformat()
            return [
                SearchResult(
                    url=row["url"],
                    title=row.get("title") or "",
                    content=row.get("content") or "",
                    retrieved_at=now,
                )
                for row in rows[:5]
            ]
        except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
            raise DeepSeekError(
                "invalid_response", "DeepSeek Search 返回的数据格式不正确"
            ) from None

    def fetch(self, url: str, deadline: float) -> SearchResult:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise DeepSeekError("invalid_url", "网页来源地址无效")
        data = self._request_json("v1/tools/fetch", {"url": url}, deadline)
        try:
            data = data.get("data", data)
            return SearchResult(
                url=url,
                title=data.get("title") or "",
                content=data["content"],
                retrieved_at=datetime.now(timezone.utc).isoformat(),
            )
        except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
            raise DeepSeekError("invalid_response", "DeepSeek Fetch 返回的数据格式不正确") from None

    def resolve_company(
        self, extraction: Extraction, sources: list[SearchResult], deadline: float
    ) -> CompanyResolution:
        prompt = (PROMPTS / "company.md").read_text(encoding="utf-8")
        return self.chat_json(
            prompt,
            {
                "company_mention": extraction.company_mention,
                "area": extraction.facts.get("area").value
                if extraction.facts.get("area")
                else None,
                "industry": extraction.facts.get("industry").value
                if extraction.facts.get("industry")
                else None,
                "sources": [source.model_dump() for source in sources],
            },
            CompanyResolution,
            deadline,
        )
