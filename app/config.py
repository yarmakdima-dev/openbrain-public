from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import os
import yaml


class ConfigError(Exception):
    """Raised when CONFIG.yaml is missing or invalid."""


@dataclass
class AppConfig:
    name: str
    environment: str
    timezone: str
    default_response_language: str


@dataclass
class TelegramConfig:
    silent_capture_default: bool
    save_confirmation_text: str
    allowed_manual_commands: List[str] = field(default_factory=list)


@dataclass
class ProviderConfig:
    enabled: bool
    label: str
    model: str
    role: str
    strengths: List[str] = field(default_factory=list)
    fallback_order: List[str] = field(default_factory=list)


@dataclass
class LanguageConfig:
    codes: List[str]
    default_provider: str
    learning_mode: bool


@dataclass
class RoutingConfig:
    default_mode: str
    auto_route_enabled: bool
    task_type_detection_enabled: bool
    language_detection_enabled: bool
    manual_overrides: Dict[str, str] = field(default_factory=dict)
    task_type_provider_preferences: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class MemoryConfig:
    embedding_provider: str
    embedding_model: str
    top_k: int
    similarity_threshold: float


@dataclass
class TaggingConfig:
    enabled: bool
    min_tags: int
    max_tags: int


@dataclass
class TopicsConfig:
    vocabulary: List[str] = field(default_factory=list)


@dataclass
class AskCommandConfig:
    use_memory: bool
    top_k: int
    threshold: float
    answer_same_language_as_user: bool
    fail_message: str


@dataclass
class AskgCommandConfig:
    use_memory: bool
    provider: str


@dataclass
class CommandsConfig:
    ask: AskCommandConfig
    askg: AskgCommandConfig


@dataclass
class HealthConfig:
    startup_provider_validation: bool
    expose_command: bool


@dataclass
class OpenBrainConfig:
    intent_layer_enabled: bool
    app: AppConfig
    telegram: TelegramConfig
    providers: Dict[str, ProviderConfig]
    languages: Dict[str, LanguageConfig]
    routing: RoutingConfig
    memory: MemoryConfig
    tagging: TaggingConfig
    topics: TopicsConfig
    commands: CommandsConfig
    health: HealthConfig

    def get_provider_model(self, provider_name: str) -> str:
        provider = self.providers.get(provider_name)
        if not provider:
            raise ConfigError(f"Unknown provider: {provider_name}")
        return provider.model

    def get_manual_override_provider(self, command: str) -> Optional[str]:
        return self.routing.manual_overrides.get(command)

    def get_language_provider(self, language_code: str) -> Optional[str]:
        normalized = language_code.lower()
        for _, lang_cfg in self.languages.items():
            if normalized in [code.lower() for code in lang_cfg.codes]:
                return lang_cfg.default_provider
        return None

    def is_manual_command(self, text: str) -> bool:
        if not text:
            return False
        first_token = text.strip().split()[0]
        return first_token in self.telegram.allowed_manual_commands


def _require(data: Dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing required config key: {key}")
    return data[key]


def _load_yaml(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise ConfigError(f"CONFIG.yaml not found at: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError("Top-level CONFIG.yaml structure must be a mapping/object")

    return raw


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_app(data: Dict[str, Any]) -> AppConfig:
    return AppConfig(
        name=_require(data, "name"),
        environment=_require(data, "environment"),
        timezone=_require(data, "timezone"),
        default_response_language=_require(data, "default_response_language"),
    )


def _parse_telegram(data: Dict[str, Any]) -> TelegramConfig:
    return TelegramConfig(
        silent_capture_default=bool(_require(data, "silent_capture_default")),
        save_confirmation_text=_require(data, "save_confirmation_text"),
        allowed_manual_commands=list(data.get("allowed_manual_commands", [])),
    )


def _parse_providers(data: Dict[str, Any]) -> Dict[str, ProviderConfig]:
    providers: Dict[str, ProviderConfig] = {}
    for name, cfg in data.items():
        providers[name] = ProviderConfig(
            enabled=bool(_require(cfg, "enabled")),
            label=_require(cfg, "label"),
            model=_require(cfg, "model"),
            role=_require(cfg, "role"),
            strengths=list(cfg.get("strengths", [])),
            fallback_order=list(cfg.get("fallback_order", [])),
        )
    return providers


def _parse_languages(data: Dict[str, Any]) -> Dict[str, LanguageConfig]:
    languages: Dict[str, LanguageConfig] = {}
    for name, cfg in data.items():
        languages[name] = LanguageConfig(
            codes=list(_require(cfg, "codes")),
            default_provider=_require(cfg, "default_provider"),
            learning_mode=bool(_require(cfg, "learning_mode")),
        )
    return languages


def _parse_routing(data: Dict[str, Any]) -> RoutingConfig:
    return RoutingConfig(
        default_mode=_require(data, "default_mode"),
        auto_route_enabled=bool(_require(data, "auto_route_enabled")),
        task_type_detection_enabled=bool(_require(data, "task_type_detection_enabled")),
        language_detection_enabled=bool(_require(data, "language_detection_enabled")),
        manual_overrides=dict(data.get("manual_overrides", {})),
        task_type_provider_preferences=dict(data.get("task_type_provider_preferences", {})),
    )


def _parse_memory(data: Dict[str, Any]) -> MemoryConfig:
    threshold = float(_require(data, "similarity_threshold"))
    if not 0.0 <= threshold <= 1.0:
        raise ConfigError("memory.similarity_threshold must be between 0.0 and 1.0")

    top_k = int(_require(data, "top_k"))
    if top_k <= 0:
        raise ConfigError("memory.top_k must be > 0")

    return MemoryConfig(
        embedding_provider=_require(data, "embedding_provider"),
        embedding_model=_require(data, "embedding_model"),
        top_k=top_k,
        similarity_threshold=threshold,
    )


def _parse_tagging(data: Dict[str, Any]) -> TaggingConfig:
    min_tags = int(_require(data, "min_tags"))
    max_tags = int(_require(data, "max_tags"))

    if min_tags < 0 or max_tags < 0:
        raise ConfigError("tagging min/max must be >= 0")
    if min_tags > max_tags:
        raise ConfigError("tagging.min_tags cannot be greater than tagging.max_tags")

    return TaggingConfig(
        enabled=bool(_require(data, "enabled")),
        min_tags=min_tags,
        max_tags=max_tags,
    )


def _parse_topics(data: Dict[str, Any]) -> TopicsConfig:
    vocabulary = [str(item).strip().lower() for item in list(_require(data, "vocabulary")) if str(item).strip()]
    if not vocabulary:
        raise ConfigError("topics.vocabulary must contain at least one value")
    if len(set(vocabulary)) != len(vocabulary):
        raise ConfigError("topics.vocabulary must not contain duplicates")
    if "other" not in vocabulary:
        raise ConfigError("topics.vocabulary must include 'other'")
    return TopicsConfig(vocabulary=vocabulary)


def _parse_commands(data: Dict[str, Any]) -> CommandsConfig:
    ask = _require(data, "ask")
    askg = _require(data, "askg")

    ask_cfg = AskCommandConfig(
        use_memory=bool(_require(ask, "use_memory")),
        top_k=int(_require(ask, "top_k")),
        threshold=float(_require(ask, "threshold")),
        answer_same_language_as_user=bool(_require(ask, "answer_same_language_as_user")),
        fail_message=_require(ask, "fail_message"),
    )

    if ask_cfg.top_k <= 0:
        raise ConfigError("commands.ask.top_k must be > 0")
    if not 0.0 <= ask_cfg.threshold <= 1.0:
        raise ConfigError("commands.ask.threshold must be between 0.0 and 1.0")

    askg_cfg = AskgCommandConfig(
        use_memory=bool(_require(askg, "use_memory")),
        provider=_require(askg, "provider"),
    )

    return CommandsConfig(ask=ask_cfg, askg=askg_cfg)


def _parse_health(data: Dict[str, Any]) -> HealthConfig:
    return HealthConfig(
        startup_provider_validation=bool(_require(data, "startup_provider_validation")),
        expose_command=bool(_require(data, "expose_command")),
    )


def _validate_cross_references(cfg: OpenBrainConfig) -> None:
    provider_names = set(cfg.providers.keys())

    for provider_name, provider in cfg.providers.items():
        for fallback in provider.fallback_order:
            if fallback not in provider_names:
                raise ConfigError(
                    f"Provider '{provider_name}' has unknown fallback provider '{fallback}'"
                )

    for language_name, language_cfg in cfg.languages.items():
        if language_cfg.default_provider not in provider_names:
            raise ConfigError(
                f"Language '{language_name}' references unknown provider "
                f"'{language_cfg.default_provider}'"
            )

    for command_name, provider_name in cfg.routing.manual_overrides.items():
        if provider_name not in provider_names:
            raise ConfigError(
                f"Manual override '{command_name}' references unknown provider '{provider_name}'"
            )

    for task_type, provider_list in cfg.routing.task_type_provider_preferences.items():
        for provider_name in provider_list:
            if provider_name not in provider_names:
                raise ConfigError(
                    f"Task type '{task_type}' references unknown provider '{provider_name}'"
                )

    if cfg.commands.askg.provider not in provider_names:
        raise ConfigError(
            f"commands.askg.provider references unknown provider '{cfg.commands.askg.provider}'"
        )


def load_config(config_path: Optional[str] = None) -> OpenBrainConfig:
    candidate = config_path or os.getenv("OPENBRAIN_CONFIG", "CONFIG.yaml")
    path = Path(candidate).expanduser().resolve()

    raw = _load_yaml(path)

    cfg = OpenBrainConfig(
        intent_layer_enabled=_parse_bool(
            os.getenv("INTENT_LAYER_ENABLED"),
            default=_parse_bool(raw.get("intent_layer_enabled"), default=False),
        ),
        app=_parse_app(_require(raw, "app")),
        telegram=_parse_telegram(_require(raw, "telegram")),
        providers=_parse_providers(_require(raw, "providers")),
        languages=_parse_languages(_require(raw, "languages")),
        routing=_parse_routing(_require(raw, "routing")),
        memory=_parse_memory(_require(raw, "memory")),
        tagging=_parse_tagging(_require(raw, "tagging")),
        topics=_parse_topics(_require(raw, "topics")),
        commands=_parse_commands(_require(raw, "commands")),
        health=_parse_health(_require(raw, "health")),
    )

    _validate_cross_references(cfg)
    return cfg


_config_cache: Optional[OpenBrainConfig] = None


def get_config() -> OpenBrainConfig:
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def reload_config() -> OpenBrainConfig:
    global _config_cache
    _config_cache = load_config()
    return _config_cache
