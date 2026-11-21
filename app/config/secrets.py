from __future__ import annotations

import os
from pathlib import Path

from dotenv.parser import parse_stream
from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator, model_validator

_SECRET_KEYS = ("BOT_TOKEN", "ADMIN_PASSWORD", "OWNER_PASSWORD", "ZABBIX_API_TOKEN")


class SecretSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    BOT_TOKEN: str
    ADMIN_PASSWORD: str
    OWNER_PASSWORD: str
    ZABBIX_API_TOKEN: str

    @field_validator("BOT_TOKEN", "ZABBIX_API_TOKEN", mode="before")
    @classmethod
    def _strip_token(cls, value: object) -> str:
        secret = str(value or "").strip()
        if not secret:
            raise ValueError("empty secret")
        return secret

    @field_validator("ADMIN_PASSWORD", "OWNER_PASSWORD", mode="before")
    @classmethod
    def _strip_password(cls, value: object, info: ValidationInfo) -> str:
        password = str(value or "").strip()
        if len(password) < 16:
            raise ValueError(f"{info.field_name} должен содержать не менее 16 символов")
        return password

    @model_validator(mode="after")
    def _passwords_must_differ(self) -> SecretSettings:
        if self.ADMIN_PASSWORD == self.OWNER_PASSWORD:
            raise ValueError("OWNER_PASSWORD должен отличаться от ADMIN_PASSWORD")
        return self


def _load_env_file_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise RuntimeError(f"Путь к env-файлу не является файлом: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            result: dict[str, str] = {}
            for binding in parse_stream(stream):
                if binding.error:
                    raise RuntimeError(f"Некорректный синтаксис secret env {path}, строка {binding.original.line}")
                if binding.key is None:
                    continue
                key = str(binding.key)
                if key in result:
                    raise RuntimeError(f"Повторяющийся ключ в secret env {path}: {key}")
                if binding.value is None:
                    raise RuntimeError(f"Ключ без присвоенного значения в secret env {path}: {key}")
                result[key] = str(binding.value).strip()
    except RuntimeError:
        raise
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"Не удалось прочитать env-файл {path}: {exc}") from exc
    return result


def _safe_secret_error_fields(exc: ValidationError) -> tuple[str, bool]:
    fields: set[str] = set()
    only_missing = True
    for error in exc.errors():
        if error.get("type") != "missing":
            only_missing = False
        location = error.get("loc") or ()
        if location and str(location[0]) in _SECRET_KEYS:
            fields.add(str(location[0]))
    if not fields:
        # model_validator errors have an empty location. Never include its
        # input repr here because it contains the actual secret values.
        fields.update({"ADMIN_PASSWORD", "OWNER_PASSWORD"})
    return ", ".join(sorted(fields)), only_missing


def load_required_secrets(path: Path, *, fallback_path: Path | None = None) -> SecretSettings:
    merged: dict[str, str] = {}
    checked_sources: list[str] = []

    if fallback_path:
        checked_sources.append(str(fallback_path))
        merged.update(
            {key: value for key, value in _load_env_file_values(fallback_path).items() if key in _SECRET_KEYS}
        )

    checked_sources.append(str(path))
    secret_file_values = _load_env_file_values(path)
    unknown_keys = sorted(set(secret_file_values) - set(_SECRET_KEYS))
    if unknown_keys:
        raise RuntimeError(f"Неизвестные ключи в secret env {path}: {', '.join(unknown_keys)}")
    merged.update({key: value for key, value in secret_file_values.items() if key in _SECRET_KEYS})

    for key in _SECRET_KEYS:
        env_value = os.getenv(key, "").strip()
        if env_value:
            merged[key] = env_value
    checked_sources.append("переменные окружения процесса")

    try:
        return SecretSettings.model_validate(merged)
    except ValidationError as exc:
        fields, only_missing = _safe_secret_error_fields(exc)
        problem = "Не заданы обязательные секреты" if only_missing else "Некорректно заданы обязательные секреты"
        raise RuntimeError(
            f"{problem}: {fields}. Проверены источники: {', '.join(checked_sources)}. "
            "Рекомендуемый вариант: хранить секреты в app/env.secrets; "
            "также поддерживаются переменные окружения процесса."
        ) from None


__all__ = [
    "SecretSettings",
    "load_required_secrets",
]
