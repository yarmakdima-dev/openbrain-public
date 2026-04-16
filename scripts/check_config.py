from app.config import get_config, ConfigError


def main() -> int:
    try:
        cfg = get_config()
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}")
        return 1

    print("CONFIG OK")
    print(f"App: {cfg.app.name}")
    print(f"Timezone: {cfg.app.timezone}")
    print(f"Silent capture default: {cfg.telegram.silent_capture_default}")
    print("Providers:")
    for name, provider in cfg.providers.items():
        print(
            f"  - {name}: enabled={provider.enabled}, model={provider.model}, role={provider.role}"
        )
    print("Languages:")
    for name, lang in cfg.languages.items():
        print(
            f"  - {name}: codes={lang.codes}, default_provider={lang.default_provider}, "
            f"learning_mode={lang.learning_mode}"
        )
    print(f"/ask threshold: {cfg.commands.ask.threshold}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())