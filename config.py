import os
from typing import Any, Dict

import yaml

ROOT_DIR = os.path.dirname(__file__)


def _load_dotenv(path: str, existing_env: set[str], allow_override: bool = False) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                current_value = str(os.environ.get(key, "") or "").strip()
                # Do not treat empty pre-existing env vars as authoritative.
                if key in existing_env and current_value:
                    continue
                if key in os.environ and (not allow_override) and current_value:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                os.environ[key] = value
    except Exception:
        return


_EXISTING_ENV = set(os.environ.keys())
_load_dotenv(os.path.join(ROOT_DIR, ".env"), _EXISTING_ENV, allow_override=False)
_load_dotenv(os.path.join(ROOT_DIR, ".env.local"), _EXISTING_ENV, allow_override=True)

APP_ENV = os.getenv("APP_ENV", "dev")
CONFIG_PATH = os.getenv("CONFIG_PATH", os.path.join(ROOT_DIR, "config.yaml"))

_ENV_ONLY_KEYS = {
    "OPENAI_API_KEY",
    "VISUAL_API_KEY",
    "MINIMAX_API_KEY",
    "VISUAL_MINIMAX_API_KEY",
    "OPENCLAW_API_KEY",
    "GITHUB_TOKEN",
    "GITEE_TOKEN",
    "GITCODE_TOKEN",
    "AUTH_TOKEN_SECRET",
    "PAYMENT_WEBHOOK_SECRET",
    "ROOT_ADMIN_PASSWORD",
    "ROOT_ADMIN_PASSWORD_HASH",
    # WeChat Pay (v3)
    "WECHATPAY_APIV3_KEY",
    "WECHATPAY_PRIVATE_KEY_PEM",
    "WECHATPAY_PRIVATE_KEY_PATH",
}


def _load_config(path: str, env: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw_data: Any = yaml.safe_load(handle)
    except Exception:
        return {}
    data = raw_data or {}
    if isinstance(data, dict) and env in data and isinstance(data[env], dict):
        return dict(data[env])
    if isinstance(data, dict):
        return dict(data)
    return {}


_CONFIG = _load_config(CONFIG_PATH, APP_ENV)


def _get(name: str, default: Any) -> Any:
    if name in os.environ:
        return os.environ[name]
    if name in _ENV_ONLY_KEYS:
        return default
    if isinstance(_CONFIG, dict):
        if name in _CONFIG:
            return _CONFIG[name]
        lower = name.lower()
        if lower in _CONFIG:
            return _CONFIG[lower]
    return default


def _get_network_section() -> Dict[str, Any]:
    if isinstance(_CONFIG, dict):
        section = _CONFIG.get("network") or _CONFIG.get("NETWORK")
        if isinstance(section, dict):
            return section
    return {}


def _get_env_value(keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in os.environ:
            return os.environ.get(key)
    return None


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes"}


def get_proxy_config() -> Dict[str, str]:
    network = _get_network_section()

    def pick(name: str) -> str:
        env_value = _get_env_value((name, name.lower()))
        if env_value is None:
            env_value = network.get(name) or network.get(name.lower())
        return str(env_value or "").strip()

    return {
        "http_proxy": pick("HTTP_PROXY"),
        "https_proxy": pick("HTTPS_PROXY"),
        "no_proxy": pick("NO_PROXY"),
    }


def get_network_settings() -> Dict[str, bool]:
    network = _get_network_section()
    inject_env = _get_env_value(
        ("INJECT_RUNTIME_PROXY", "inject_runtime_proxy", "NETWORK_INJECT_RUNTIME_PROXY")
    )
    if inject_env is None:
        inject_env = network.get("inject_runtime_proxy")
    check_env = _get_env_value(
        ("CHECK_DOCKER_PROXY", "check_docker_proxy", "NETWORK_CHECK_DOCKER_PROXY")
    )
    if check_env is None:
        check_env = network.get("check_docker_proxy")
    force_env = _get_env_value(
        ("FORCE_PROXY", "force_proxy", "NETWORK_FORCE_PROXY")
    )
    if force_env is None:
        force_env = network.get("force_proxy")
    probe_env = _get_env_value(
        ("PROBE_PROXY", "probe_proxy", "NETWORK_PROBE_PROXY")
    )
    if probe_env is None:
        probe_env = network.get("probe_proxy")
    return {
        "inject_runtime_proxy": _parse_bool(inject_env, True),
        "check_docker_proxy": _parse_bool(check_env, True),
        "force_proxy": _parse_bool(force_env, False),
        "probe_proxy": _parse_bool(probe_env, True),
    }


REDIS_URL = _get("REDIS_URL", "redis://localhost:6379/0")
REDIS_DISABLED = str(_get("REDIS_DISABLED", "false")).lower() in {"1", "true", "yes"}
CELERY_ALWAYS_EAGER = str(_get("CELERY_ALWAYS_EAGER", "false")).lower() in {"1", "true", "yes"}
FEATURE_SAAS_ENTITLEMENTS = str(_get("FEATURE_SAAS_ENTITLEMENTS", "false")).lower() in {"1", "true", "yes"}
FEATURE_SAAS_ADMIN_API = str(_get("FEATURE_SAAS_ADMIN_API", "false")).lower() in {"1", "true", "yes"}
API_HOST = _get("API_HOST", "127.0.0.1")
API_PORT = int(_get("API_PORT", "8010"))
_root_path = str(_get("ROOT_PATH", "")).strip()
if _root_path and not _root_path.startswith("/"):
    _root_path = f"/{_root_path}"
ROOT_PATH = _root_path.rstrip("/") if _root_path else ""
APP_VERSION = str(_get("APP_VERSION", "0.5.0"))
GIT_SHA = str(_get("GIT_SHA", "")).strip() or None
PUBLIC_HOST = _get("PUBLIC_HOST", "localhost")
DATABASE_URL = str(
    _get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(ROOT_DIR, '.antihub', 'antihub.db')}",
    )
).strip()
CASE_STORE_BACKEND = str(_get("CASE_STORE_BACKEND", "redis")).strip().lower() or "redis"
CASE_STORE_DATABASE_URL = str(_get("CASE_STORE_DATABASE_URL", DATABASE_URL)).strip() or DATABASE_URL
DATABASE_ECHO = str(_get("DATABASE_ECHO", "false")).lower() in {"1", "true", "yes"}
AUTH_ENABLED = str(_get("AUTH_ENABLED", "true")).lower() in {"1", "true", "yes"}
AUTH_USERS_JSON = str(_get("AUTH_USERS_JSON", "")).strip()
AUTH_TOKEN_SECRET = str(_get("AUTH_TOKEN_SECRET", "")).strip()
AUTH_TOKEN_TTL_SECONDS = int(_get("AUTH_TOKEN_TTL_SECONDS", "43200"))
DISABLE_RECOMMEND_RATE_LIMIT = str(_get("DISABLE_RECOMMEND_RATE_LIMIT", "false")).lower() in {
    "1",
    "true",
    "yes",
}
ROOT_ADMIN_USERNAME = str(_get("ROOT_ADMIN_USERNAME", "root")).strip()
ROOT_ADMIN_PASSWORD = str(_get("ROOT_ADMIN_PASSWORD", "")).strip()
ROOT_ADMIN_PASSWORD_HASH = str(_get("ROOT_ADMIN_PASSWORD_HASH", "")).strip()
ROOT_ADMIN_FORCE_SYNC = str(_get("ROOT_ADMIN_FORCE_SYNC", "false")).lower() in {"1", "true", "yes"}
STARTUP_BOOTSTRAP_ENABLED = str(_get("STARTUP_BOOTSTRAP_ENABLED", "true")).lower() in {
    "1",
    "true",
    "yes",
}
PAYMENT_WEBHOOK_SECRET = str(_get("PAYMENT_WEBHOOK_SECRET", "")).strip()
PAYMENT_PROVIDER = str(_get("PAYMENT_PROVIDER", "mock")).strip().lower() or "mock"
WECHATPAY_API_BASE_URL = str(_get("WECHATPAY_API_BASE_URL", "https://api.mch.weixin.qq.com")).strip().rstrip("/")
WECHATPAY_NOTIFY_URL = str(_get("WECHATPAY_NOTIFY_URL", "")).strip()
WECHATPAY_MCHID = str(_get("WECHATPAY_MCHID", "")).strip()
WECHATPAY_APPID = str(_get("WECHATPAY_APPID", "")).strip()
WECHATPAY_CERT_SERIAL = str(_get("WECHATPAY_CERT_SERIAL", "")).strip()
WECHATPAY_PRIVATE_KEY_PEM = str(_get("WECHATPAY_PRIVATE_KEY_PEM", "")).strip()
WECHATPAY_PRIVATE_KEY_PATH = str(_get("WECHATPAY_PRIVATE_KEY_PATH", "")).strip()
WECHATPAY_PLATFORM_CERT_SERIAL = str(_get("WECHATPAY_PLATFORM_CERT_SERIAL", "")).strip()
WECHATPAY_PLATFORM_CERT_PEM = str(_get("WECHATPAY_PLATFORM_CERT_PEM", "")).strip()
WECHATPAY_PLATFORM_CERT_PATH = str(_get("WECHATPAY_PLATFORM_CERT_PATH", "")).strip()
WECHATPAY_PLATFORM_CERTS_JSON = str(_get("WECHATPAY_PLATFORM_CERTS_JSON", "")).strip()
WECHATPAY_APIV3_KEY = str(_get("WECHATPAY_APIV3_KEY", "")).strip()
DEFAULT_BRANCH = _get("DEFAULT_BRANCH", "main")
BUILD_ROOT = _get("BUILD_ROOT", os.path.join(ROOT_DIR, ".antihub", "builds"))
MANUAL_ROOT = _get("MANUAL_ROOT", os.path.join(ROOT_DIR, ".antihub", "manuals"))
AUTO_MANUAL = str(_get("AUTO_MANUAL", "false")).lower() in {"1", "true", "yes"}
MANUAL_TREE_DEPTH = int(_get("MANUAL_TREE_DEPTH", "2"))
MANUAL_MAX_README_CHARS = int(_get("MANUAL_MAX_README_CHARS", "1200"))
MANUAL_GENERATOR_VERSION = str(_get("MANUAL_GENERATOR_VERSION", "v0.4"))
ANALYZE_ROOT = _get("ANALYZE_ROOT", os.path.join(ROOT_DIR, ".antihub", "analyze"))
REPORT_ROOT = _get("REPORT_ROOT", os.path.join(ROOT_DIR, ".antihub", "reports"))
ANALYZE_TREE_DEPTH = int(_get("ANALYZE_TREE_DEPTH", "3"))
ANALYZE_TREE_MAX_ENTRIES = int(_get("ANALYZE_TREE_MAX_ENTRIES", "400"))
ANALYZE_README_MAX_CHARS = int(_get("ANALYZE_README_MAX_CHARS", "20000"))
ANALYZE_SNIPPET_MAX_CHARS = int(_get("ANALYZE_SNIPPET_MAX_CHARS", "20000"))
MERMAID_VALIDATE_RETRIES = int(_get("MERMAID_VALIDATE_RETRIES", "2"))
MERMAID_VALIDATE_TIMEOUT_SECONDS = int(_get("MERMAID_VALIDATE_TIMEOUT_SECONDS", "180"))
MERMAID_INSTALL_TIMEOUT_SECONDS = int(_get("MERMAID_INSTALL_TIMEOUT_SECONDS", "600"))
ANALYZE_LOCK_PREFIX = _get("ANALYZE_LOCK_PREFIX", "analyze_lock:")
OPENAI_API_KEY = str(_get("OPENAI_API_KEY", "")).strip()
OPENAI_BASE_URL = str(_get("OPENAI_BASE_URL", "https://api.openai.com/v1")).strip()
OPENAI_API_MODEL = str(_get("OPENAI_API_MODEL", "gpt-4o-mini")).strip()
MINIMAX_API_KEY = str(_get("MINIMAX_API_KEY", "")).strip()
MINIMAX_BASE_URL = str(_get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")).strip()
MINIMAX_MODEL = str(_get("MINIMAX_MODEL", "MiniMax-M2.5")).strip()
ANALYZE_LLM_TEMPERATURE = float(_get("ANALYZE_LLM_TEMPERATURE", "0.2"))
ANALYZE_LLM_MAX_TOKENS = int(_get("ANALYZE_LLM_MAX_TOKENS", "1200"))
RECOMMEND_LLM_TEMPERATURE = float(_get("RECOMMEND_LLM_TEMPERATURE", "0.1"))
RECOMMEND_LLM_MAX_TOKENS = int(_get("RECOMMEND_LLM_MAX_TOKENS", "900"))
RECOMMEND_MAX_UPLOAD_BYTES = int(_get("RECOMMEND_MAX_UPLOAD_BYTES", "3145728"))
RECOMMEND_MAX_TEXT_CHARS = int(_get("RECOMMEND_MAX_TEXT_CHARS", "8000"))
RECOMMEND_GITHUB_MAX_RESULTS = int(_get("RECOMMEND_GITHUB_MAX_RESULTS", "40"))
RECOMMEND_TOP_K = int(_get("RECOMMEND_TOP_K", "20"))
RECOMMEND_DEEP_DOC_FETCH_LIMIT = max(1, int(_get("RECOMMEND_DEEP_DOC_FETCH_LIMIT", "4")))
RECOMMEND_DEEP_DOC_TIMEOUT_SECONDS = max(2, int(_get("RECOMMEND_DEEP_DOC_TIMEOUT_SECONDS", "10")))
RECOMMEND_EXTERNAL_SOURCES_ENABLED = str(_get("RECOMMEND_EXTERNAL_SOURCES_ENABLED", "true")).lower() in {
    "1",
    "true",
    "yes",
}
RECOMMEND_ENABLE_GITEE = str(_get("RECOMMEND_ENABLE_GITEE", "true")).lower() in {"1", "true", "yes"}
RECOMMEND_ENABLE_GITCODE = str(_get("RECOMMEND_ENABLE_GITCODE", "true")).lower() in {"1", "true", "yes"}
RECOMMEND_PROVIDER_TIMEOUT_SECONDS = max(1, int(_get("RECOMMEND_PROVIDER_TIMEOUT_SECONDS", "8")))
GITEE_API_BASE_URL = str(_get("GITEE_API_BASE_URL", "https://gitee.com/api/v5")).strip().rstrip("/")
GITEE_TOKEN = str(_get("GITEE_TOKEN", "")).strip()
GITCODE_API_BASE_URL = str(_get("GITCODE_API_BASE_URL", "https://gitcode.com")).strip().rstrip("/")
GITCODE_SEARCH_PATH = str(_get("GITCODE_SEARCH_PATH", "/api/v4/projects")).strip() or "/api/v4/projects"
GITCODE_TOKEN = str(_get("GITCODE_TOKEN", "")).strip()
ONE_CLICK_DEPLOY_POINTS_COST = max(0, int(_get("ONE_CLICK_DEPLOY_POINTS_COST", "0")))
DEEP_SEARCH_POINTS_COST = max(0, int(_get("DEEP_SEARCH_POINTS_COST", "50")))

PORT_POOL_START = int(_get("PORT_POOL_START", "30000"))
PORT_POOL_END = int(_get("PORT_POOL_END", "30100"))
PORT_MODE = str(_get("PORT_MODE", "pool"))  # pool | dynamic

LOG_RETENTION_LINES = int(_get("LOG_RETENTION_LINES", "2000"))
LOG_LIST_PREFIX = _get("LOG_LIST_PREFIX", "logs:")
WS_LOG_CHANNEL_PREFIX = _get("WS_LOG_CHANNEL_PREFIX", "build_logs:")
CASE_PREFIX = _get("CASE_PREFIX", "case:")
MANUAL_PREFIX = _get("MANUAL_PREFIX", "manual:")
MANUAL_META_PREFIX = _get("MANUAL_META_PREFIX", "manual_meta:")
MANUAL_STATUS_PREFIX = _get("MANUAL_STATUS_PREFIX", "manual_status:")
MANUAL_STATS_KEY = _get("MANUAL_STATS_KEY", "manual_stats")
TEMPLATES_PATH = _get("TEMPLATES_PATH", os.path.join(ROOT_DIR, "templates.json"))
PLANS = _get("PLANS", [])

DOCKERFILE_SEARCH_DEPTH = int(_get("DOCKERFILE_SEARCH_DEPTH", "2"))
REPO_SCAN_DEPTH = int(_get("REPO_SCAN_DEPTH", "2"))
DOCKER_BUILD_NETWORK = str(_get("DOCKER_BUILD_NETWORK", "bridge"))
DOCKER_BUILD_NO_CACHE = str(_get("DOCKER_BUILD_NO_CACHE", "false")).lower() in {"1", "true", "yes"}
DOCKER_BUILDKIT_DEFAULT = str(_get("DOCKER_BUILDKIT_DEFAULT", "true")).lower() in {"1", "true", "yes"}
GIT_ENABLE_SUBMODULES = str(_get("GIT_ENABLE_SUBMODULES", "false")).lower() in {"1", "true", "yes"}
GIT_ENABLE_LFS = str(_get("GIT_ENABLE_LFS", "false")).lower() in {"1", "true", "yes"}

CORS_ORIGINS = [
    origin.strip()
    for origin in str(
        _get(
            "CORS_ORIGINS",
            "http://127.0.0.1:5173,http://localhost:5173",
        )
    ).split(",")
    if origin.strip()
]

CASE_LABEL_KEY = _get("CASE_LABEL_KEY", "antihub.case_id")
CASE_LABEL_MANAGED = _get("CASE_LABEL_MANAGED", "antihub.managed")

TIMEOUT_CLONE_SECONDS = int(_get("TIMEOUT_CLONE", _get("GIT_CLONE_TIMEOUT_SECONDS", "120")))
TIMEOUT_BUILD_SECONDS = int(_get("TIMEOUT_BUILD", _get("DOCKER_BUILD_TIMEOUT_SECONDS", "1800")))
TIMEOUT_RUN_SECONDS = int(_get("TIMEOUT_RUN", _get("STARTUP_TIMEOUT_SECONDS", "120")))
TIMEOUT_MANUAL_SECONDS = int(_get("TIMEOUT_MANUAL", "300"))

GIT_CLONE_TIMEOUT_SECONDS = TIMEOUT_CLONE_SECONDS
DOCKER_BUILD_TIMEOUT_SECONDS = TIMEOUT_BUILD_SECONDS
STARTUP_TIMEOUT_SECONDS = TIMEOUT_RUN_SECONDS
ANALYZE_TIMEOUT_SECONDS = int(_get("ANALYZE_TIMEOUT_SECONDS", "300"))
ANALYZE_LOCK_TTL_SECONDS = int(_get("ANALYZE_LOCK_TTL_SECONDS", str(ANALYZE_TIMEOUT_SECONDS + 30)))
VISUALIZE_TIMEOUT_SECONDS = int(_get("VISUALIZE_TIMEOUT_SECONDS", "900"))
WORKER_TASK_MAX_RETRIES = max(0, int(_get("WORKER_TASK_MAX_RETRIES", "2")))
WORKER_TASK_RETRY_DELAY_SECONDS = max(1, int(_get("WORKER_TASK_RETRY_DELAY_SECONDS", "20")))
WORKER_DEAD_LETTER_KEY = str(_get("WORKER_DEAD_LETTER_KEY", "worker:dead_letters")).strip() or "worker:dead_letters"
VISUAL_LOCK_PREFIX = _get("VISUAL_LOCK_PREFIX", "visual_lock:")
VISUAL_LOCK_TTL_SECONDS = int(_get("VISUAL_LOCK_TTL_SECONDS", str(VISUALIZE_TIMEOUT_SECONDS + 30)))
VISUAL_PROVIDER = str(_get("VISUAL_PROVIDER", "minimax")).strip().lower()
VISUAL_API_KEY = str(
    _get(
        "VISUAL_API_KEY",
        _get("MINIMAX_API_KEY", _get("VISUAL_MINIMAX_API_KEY", "")),
    )
).strip()
VISUAL_BASE_URL = str(
    _get(
        "VISUAL_BASE_URL",
        _get("MINIMAX_BASE_URL", _get("VISUAL_MINIMAX_BASE_URL", "")),
    )
).strip()
VISUAL_IMAGE_MODEL = str(
    _get(
        "VISUAL_IMAGE_MODEL",
        _get("MINIMAX_IMAGE_MODEL", _get("VISUAL_MINIMAX_IMAGE_MODEL", "")),
    )
).strip()
VISUAL_IMAGE_ENDPOINT = str(_get("VISUAL_IMAGE_ENDPOINT", "/v1/image_generation")).strip()
VISUAL_IMAGE_SIZE = str(_get("VISUAL_IMAGE_SIZE", "1024x1024")).strip()
VISUAL_IMAGE_TIMEOUT_SECONDS = int(_get("VISUAL_IMAGE_TIMEOUT_SECONDS", "60"))
VISUAL_TEMPLATE_VERSION = str(_get("VISUAL_TEMPLATE_VERSION", "v1")).strip()
VISUAL_TREE_DEPTH = int(_get("VISUAL_TREE_DEPTH", str(ANALYZE_TREE_DEPTH)))
VISUAL_TREE_MAX_ENTRIES = int(_get("VISUAL_TREE_MAX_ENTRIES", str(ANALYZE_TREE_MAX_ENTRIES)))
VISUAL_GRAPH_MAX_NODES = int(_get("VISUAL_GRAPH_MAX_NODES", "120"))
VISUAL_SPOTLIGHT_MAX_CHARS = int(_get("VISUAL_SPOTLIGHT_MAX_CHARS", "1200"))
VISUAL_SPOTLIGHT_MAX_FILES = int(_get("VISUAL_SPOTLIGHT_MAX_FILES", "5"))
VISUAL_LANGUAGE_MAX_FILES = int(_get("VISUAL_LANGUAGE_MAX_FILES", "6000"))
VISUAL_VIDEO_ENABLED = str(_get("VISUAL_VIDEO_ENABLED", "false")).lower() in {"1", "true", "yes"}
VISUAL_RENDER_WEBM = str(_get("VISUAL_RENDER_WEBM", "false")).lower() in {"1", "true", "yes"}
VISUAL_REMOTION_PROJECT = _get("VISUAL_REMOTION_PROJECT", os.path.join(ROOT_DIR, "visualize", "remotion"))
VISUAL_REMOTION_FPS = int(_get("VISUAL_REMOTION_FPS", "30"))
VISUAL_REMOTION_WIDTH = int(_get("VISUAL_REMOTION_WIDTH", "1920"))
VISUAL_REMOTION_HEIGHT = int(_get("VISUAL_REMOTION_HEIGHT", "1080"))
VISUAL_REMOTION_CODEC_MP4 = str(_get("VISUAL_REMOTION_CODEC_MP4", "h264")).strip()
VISUAL_REMOTION_CODEC_WEBM = str(_get("VISUAL_REMOTION_CODEC_WEBM", "vp8")).strip()

CASE_TEMPLATES = _get("CASE_TEMPLATES", [])

INGEST_ROOT = _get("INGEST_ROOT", os.path.join(ROOT_DIR, ".antihub", "ingest"))
INGEST_GIT_DEPTH = int(_get("INGEST_GIT_DEPTH", "1"))
INGEST_MAX_FILES = int(_get("INGEST_MAX_FILES", "20000"))
OPENCLAW_BASE_URL = str(_get("OPENCLAW_BASE_URL", "")).strip()
OPENCLAW_API_KEY = str(_get("OPENCLAW_API_KEY", "")).strip()
OPENCLAW_TIMEOUT_SECONDS = int(_get("OPENCLAW_TIMEOUT_SECONDS", "300"))
OPENCLAW_SKILL_ENDPOINT = str(_get("OPENCLAW_SKILL_ENDPOINT", "/skills/run")).strip()
