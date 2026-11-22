import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .exceptions import FileNotFound, InvalidFilename, ProblemNotFound

_FILENAME = re.compile(r"([A-Za-z][0-9]*)\.cpp")
_INDEX = re.compile(r"[A-Za-z][A-Za-z0-9]*", re.ASCII)


def valid_index(index: object) -> bool:
    return isinstance(index, str) and _INDEX.fullmatch(index) is not None


def problem_index(path: Path, explicit: str | None) -> str:
    if path.suffix != ".cpp":
        raise InvalidFilename("Source file must have the .cpp extension.")
    if explicit is not None:
        if not valid_index(explicit):
            raise ProblemNotFound("Invalid problem index.")
        return explicit.upper()
    match = _FILENAME.fullmatch(path.name)
    if match is None:
        raise InvalidFilename("Cannot infer problem index from the filename. Use -p INDEX.")
    return match[1].upper()


@dataclass(frozen=True)
class Source:
    path: Path
    content: bytes
    sha256: str

    @classmethod
    def read(cls, path: Path) -> "Source":
        try:
            if not path.is_file():
                raise FileNotFound("Source file does not exist or is not a regular file.")
            content = path.read_bytes()
        except FileNotFoundError:
            raise FileNotFound("Source file does not exist.") from None
        except OSError:
            raise InvalidFilename("Source file could not be read.") from None
        if not content.strip():
            raise InvalidFilename("Source file is empty.")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidFilename("Source file must be encoded as UTF-8.") from None
        return cls(path, content, hashlib.sha256(content).hexdigest())

    def verify_unchanged(self) -> None:
        if self.read(self.path).sha256 != self.sha256:
            raise InvalidFilename("Source changed after it was read. Run submit again.")
