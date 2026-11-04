"""Позволяет запускать пакет через `python -m friday`."""

from friday.cli import main
from friday.textio import utf8_stdio

if __name__ == "__main__":
    utf8_stdio()
    main()
