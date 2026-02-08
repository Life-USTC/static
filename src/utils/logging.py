import logging
import sys

from tqdm import tqdm


class TqdmLoggingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tqdm.write(msg, file=sys.stderr)
        except Exception:
            self.handleError(record)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()

    if getattr(root, "_life_ustc_logging_configured", False):
        root.setLevel(level)
        return

    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = TqdmLoggingHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    setattr(root, "_life_ustc_logging_configured", True)
