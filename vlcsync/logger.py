
import logging
import sys
from pathlib import Path
from typing import Optional

# Default log format
# Example: [INFO] [client_app.py:50] Message
LOG_FORMAT = "[%(levelname)s] [%(filename)s:%(lineno)d] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def setup_logger(
    name: str = "vlcsync",
    log_file: Optional[Path] = None,
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    no_log_file: bool = False
) -> logging.Logger:
    """
    Setup the logger with console and optional file handlers.
    
    Args:
        name: Logger name
        log_file: Path to the log file (optional)
        console_level: Logging level for console output (default: INFO)
        file_level: Logging level for file output (default: DEBUG)
        no_log_file: If True, disables file logging even if log_file is provided
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # Capture everything at the root level

    # Remove existing handlers to avoid duplication if called multiple times
    if logger.handlers:
        logger.handlers.clear()

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    # Console Handler
    c_level = getattr(logging, console_level.upper(), logging.INFO)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(c_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler
    if not no_log_file and log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            f_level = getattr(logging, file_level.upper(), logging.DEBUG)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setLevel(f_level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            # Fallback if we can't write to the log file
            sys.stderr.write(f"Failed to setup file logging at {log_file}: {e}\n")

    return logger
