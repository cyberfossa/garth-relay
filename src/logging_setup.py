import logging
import logging.config
from collections.abc import MutableMapping

import structlog

from src.config import get_config


def rename_fields_for_gcp(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, object],
) -> MutableMapping[str, object]:
    if "level" in event_dict:
        event_dict["severity"] = str(event_dict.pop("level")).upper()
    if "logger_name" in event_dict:
        event_dict["logger"] = event_dict.pop("logger_name")
    if "filename" in event_dict:
        event_dict["source_file"] = event_dict.pop("filename")
    if "lineno" in event_dict:
        event_dict["line_number"] = event_dict.pop("lineno")
    if "func_name" in event_dict:
        event_dict["function"] = event_dict.pop("func_name")
    return event_dict


def setup_logging() -> None:
    try:
        settings = get_config()
        log_level = settings.log_level or "INFO"
        debug_mode = settings.debug
    except Exception:
        log_level = "INFO"
        debug_mode = False

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    foreign_pre_chain = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.ExtraAdder(),
    ]

    json_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
        rename_fields_for_gcp,
        structlog.processors.EventRenamer("message"),
        structlog.processors.JSONRenderer(),
    ]

    dev_processors = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.dev.ConsoleRenderer(colors=True),
    ]

    structlog.configure(
        processors=shared_processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "foreign_pre_chain": foreign_pre_chain,
                    "processors": dev_processors if debug_mode else json_processors,
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "root": {"handlers": ["default"], "level": log_level.upper()},
            "loggers": {
                "google": {"level": "WARNING", "propagate": True},
                "urllib3": {"level": "WARNING", "propagate": True},
                "asyncio": {"level": "WARNING", "propagate": True},
            },
        }
    )

    logger = structlog.get_logger(__name__)
    logger.info("Logging configured", log_level=log_level.upper(), debug=debug_mode)
