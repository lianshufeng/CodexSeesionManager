from __future__ import annotations

import re


DEFAULT_LANGUAGE = "ja-JP"
COMMON_LANGUAGES = (
    "ja-JP", "en-US", "en-GB", "zh-CN", "zh-TW", "zh-HK", "ko-KR",
    "fr-FR", "de-DE", "es-ES", "pt-BR", "it-IT", "ru-RU", "ar-SA",
    "hi-IN", "id-ID", "vi-VN", "th-TH",
)
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_CONTEXT = re.compile(
    r"\A\s*<(?P<tag>environment_context|codex_apps_client_time_context)>"
    r".*?</(?P=tag)>\s*\Z", re.DOTALL,
)
_ELEMENT = re.compile(
    r"(<(?P<tag>locale|language|system_locale|system_language|os_language)>)[^<>]*"
    r"(</(?P=tag)>)"
)
_FIELDS = ("locale", "language", "system_locale", "system_language", "os_language")


def validate_language(value: str) -> str:
    value = value.strip().replace("_", "-")
    if not _LANGUAGE_TAG.fullmatch(value):
        raise ValueError("请输入有效的语言代码，例如 ja-JP。")
    parts = value.split("-")
    return "-".join([parts[0].lower(), *(
        part.title() if len(part) == 4 else part.upper() if len(part) == 2 else part.lower()
        for part in parts[1:]
    )])


class LanguageRewriter:
    """Override outgoing language declarations; never simulate an OS locale API."""

    def __init__(self, language: str) -> None:
        self.language = validate_language(language)

    @property
    def accept_language(self) -> str:
        primary = self.language.split("-", 1)[0]
        return self.language if primary == self.language else f"{self.language},{primary};q=0.9"

    def rewrite(self, payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        changed = False
        # Only known declaration containers, not arbitrary tool arguments or user data.
        for container in (payload.get("client_metadata"),):
            if isinstance(container, dict):
                for key in _FIELDS:
                    if isinstance(container.get(key), str) and container[key] != self.language:
                        container[key] = self.language
                        changed = True
        params = payload.get("params")
        for metadata in (payload.get("_meta"), params.get("_meta") if isinstance(params, dict) else None):
            if isinstance(metadata, dict) and isinstance(metadata.get("openai/locale"), str):
                if metadata["openai/locale"] != self.language:
                    metadata["openai/locale"] = self.language
                    changed = True
        items = payload.get("input")
        if not isinstance(items, list):
            return changed
        for item in items:
            if not isinstance(item, dict) or item.get("role") not in ("user", "developer"):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "input_text":
                    continue
                original = part.get("text")
                if not isinstance(original, str) or not _CONTEXT.fullmatch(original):
                    continue
                updated = _ELEMENT.sub(lambda match: match[1] + self.language + match[3], original)
                if updated != original:
                    part["text"] = updated
                    changed = True
        return changed
