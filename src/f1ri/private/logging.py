"""Keep exception payloads out of private deployment logs."""

from copy import copy
from logging import Formatter


class ServiceFormatter(Formatter):
    def format(self, record):
        if record.exc_info or record.exc_text:
            record = copy(record)
            record.exc_info = record.exc_text = None
        return super().format(record)
