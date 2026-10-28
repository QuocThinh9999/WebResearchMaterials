from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings


T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)


class AIConfigurationError(RuntimeError):
    pass


class AIResponseError(RuntimeError):
    pass


class AIGateway(Protocol):
    model: str
    embedding_model: str

    def structured_output(self, output_type: type[T], instructions: str, user_input: Any) -> T: ...

    def structured_output_with_pdf(
        self, output_type: type[T], instructions: str, prompt: str, pdf_content: bytes
    ) -> T: ...

    def embeddings(self, texts: list[str]) -> list[list[float]]: ...


class OllamaGateway:
    def __init__(
        self, settings: Settings | None = None, client: httpx.Client | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        settings = settings or get_settings()
        self.model = settings.ollama_model
        self.embedding_model = settings.ollama_embedding_model
        self._base_url = settings.ollama_base_url.rstrip("/")
        self._timeout = min(settings.ollama_timeout_seconds, timeout_seconds) if timeout_seconds is not None else settings.ollama_timeout_seconds
        self._client = client or httpx.Client(timeout=self._timeout)
        self._owns_client = client is None
        self.call_count = 0
        self.last_metrics: dict[str, Any] | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            self.call_count += 1
            response = self._client.post(f"{self._base_url}/{path.lstrip('/')}", json=payload)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Phản hồi không phải JSON object.")
            self.last_metrics = {
                "path": path,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                **{key: result[key] for key in (
                    "total_duration", "load_duration", "prompt_eval_duration", "eval_duration",
                    "prompt_eval_count", "prompt_eval_cached_count", "eval_count",
                ) if key in result},
            }
            logger.info("Chẩn đoán Ollama: %s", self.last_metrics)
            return result
        except httpx.ConnectError as exc:
            raise AIConfigurationError(
                "Không kết nối được Ollama tại "
                f"{self._base_url}. Hãy chạy .\\chay_ollama.ps1 rồi thử lại."
            ) from exc
        except httpx.TimeoutException as exc:
            raise AIResponseError(
                f"Ollama chưa hoàn tất sau {self._timeout:g} giây. Hãy giảm độ dài yêu cầu hoặc dùng mô hình nhỏ hơn."
            ) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise AIConfigurationError(
                    "Ollama chưa có mô hình cần dùng. Hãy chạy .\\cai_dat_ollama.ps1."
                ) from exc
            raise AIResponseError("Ollama từ chối yêu cầu xử lý AI.") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AIResponseError("Không thể nhận kết quả hợp lệ từ Ollama.") from exc

    @staticmethod
    def _message_text(payload: dict[str, Any]) -> str:
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        raise AIResponseError("Ollama không trả về nội dung có cấu trúc.")

    def _structured_payload(
        self,
        output_type: type[T],
        instructions: str,
        user_input: Any,
    ) -> dict[str, Any]:
        content = (
            user_input
            if isinstance(user_input, str)
            else json.dumps(user_input, ensure_ascii=False, default=str)
        )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": content},
            ],
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "format": output_type.model_json_schema(),
            "options": {
                "temperature": 0,
                "num_predict": 160 if output_type.__name__ == "YeuCauChuanHoa" else 1024 if output_type.__name__ in {"PhanTichYeuCauAI", "KetQuaDanhGiaAI"} else 320,
                "num_ctx": 1024 if output_type.__name__ == "YeuCauChuanHoa" else 4096,
            },
        }

    def structured_output(self, output_type: type[T], instructions: str, user_input: Any) -> T:
        request = self._structured_payload(output_type, instructions, user_input)
        try:
            payload = self._post("api/chat", request)
        except AIResponseError as exc:
            if not isinstance(exc.__cause__, httpx.HTTPStatusError) or exc.__cause__.response.status_code != 400:
                raise
            request.pop("think", None)
            request["format"] = "json"
            payload = self._post("api/chat", request)
        try:
            return output_type.model_validate_json(self._message_text(payload))
        except (ValidationError, ValueError) as exc:
            request["messages"][-1]["content"] = "Return only valid compact JSON for the requested fields. " + str(user_input)[:2500]
            retry = self._post("api/chat", request)
            try:
                return output_type.model_validate_json(self._message_text(retry))
            except (ValidationError, ValueError) as final_exc:
                raise AIResponseError("Kết quả AI không đúng cấu trúc dữ liệu yêu cầu.") from final_exc

    def structured_output_with_pdf(
        self, output_type: type[T], instructions: str, prompt: str, pdf_content: bytes
    ) -> T:
        raise AIConfigurationError(
            "Ollama hiện được cấu hình để đọc lớp văn bản của PDF, không gửi trực tiếp tệp PDF "
            "tới mô hình thị giác. Hãy để AI_ALLOW_PDF_VISION=false."
        )

    def embeddings(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = self._post("api/embed", {"model": self.embedding_model, "input": texts})
        vectors = payload.get("embeddings") or []
        if len(vectors) != len(texts) or any(not isinstance(item, list) for item in vectors):
            raise AIResponseError("Ollama trả về embedding không đầy đủ.")
        return vectors
