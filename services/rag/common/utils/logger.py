import os
import logging.config
import logging
import structlog


# ---------------------------------------------------------
# 1. Read log level from environment
# ---------------------------------------------------------
logging_ini_path = os.path.join(os.path.dirname(__file__), 'logging.config')
logging.config.fileConfig(logging_ini_path, disable_existing_loggers=False)

#LOG_LEVEL = "DEBUG" ##Helper.get_property("APP_LOG_LEVEL")
#print(f"initENV() APP_LOG_LEVEL={LOG_LEVEL}")

# Map string → logging level
LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "ERROR": logging.ERROR,
}
'''
logging.basicConfig(
    level=LEVELS.get(LOG_LEVEL, logging.INFO),
    filename='2test.log',
    format="%(message)s",
)
'''
# ---------------------------------------------------------
# 2. Configure structlog processors
# ---------------------------------------------------------
numeric_level = logging.getLogger("ailogger").getEffectiveLevel()
# Convert the numeric level to its string name
log_level_name = logging.getLevelName(numeric_level)

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),   # Output logs as JSON
    ],
    wrapper_class=structlog.make_filtering_bound_logger(LEVELS.get(log_level_name, logging.INFO)
    ),
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

# ---------------------------------------------------------
# 3. Export logger
# ---------------------------------------------------------
log = structlog.get_logger("ailogger")

def get_logger(name=None):
    return structlog.get_logger(name if name else "ailogger")