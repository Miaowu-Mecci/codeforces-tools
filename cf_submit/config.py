import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .exceptions import CacheNotFound, ConfigError
from .language import LANGUAGE
from .problem import valid_index


@dataclass(frozen=True)
class Competition:
    kind: str
    id: int
    language: str = LANGUAGE

    def __post_init__(self) -> None:
        if self.kind not in ("contest", "gym") or type(self.id) is not int or self.id < 1:
            raise ConfigError("Invalid competition. Use cfset contest ID or cfset gym ID.")
        if self.language != LANGUAGE:
            raise ConfigError(f"Invalid configured language. Use cfset to restore {LANGUAGE}.")


@dataclass(frozen=True)
class Problemset:
    name: str
    problems: dict[str, str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or not isinstance(self.problems, dict)
            or not self.problems
            or any(
                not valid_index(index)
                or index != index.upper()
                or not isinstance(name, str)
                or not name.strip()
                for index, name in self.problems.items()
            )
        ):
            raise ValueError("Invalid competition name or problem list.")


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else Path.home() / ".config" / "cf-submit"
        self.config_path = self.root / "config.json"
        self.cookie_path = self.root / "cookies.json"
        self.cache_dir = self.root / "cache"

    @staticmethod
    def _private_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)

    @contextmanager
    def lock(self, name: str):
        self._private_directory(self.root)
        fd = os.open(self.root / f".{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    @staticmethod
    def read_json(path: Path):
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)

    def write_json(self, path: Path, data: dict) -> None:
        self._private_directory(self.root)
        if path.parent != self.root:
            self._private_directory(path.parent)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)

    def _cache_path(self, competition: Competition) -> Path:
        return self.cache_dir / f"{competition.kind}_{competition.id}.json"

    def current(self) -> Competition:
        try:
            data = self.read_json(self.config_path)
            return Competition(data["type"], data["id"], data["language"])
        except FileNotFoundError:
            raise ConfigError("No current competition. Run cfset contest ID or cfset gym ID.") from None
        except (KeyError, TypeError, ValueError, OSError):
            raise ConfigError("Cannot read competition configuration. Run cfset again.") from None

    def problemset(self, competition: Competition) -> Problemset:
        try:
            data = self.read_json(self._cache_path(competition))
            if (data["type"], data["id"], data["version"]) != (competition.kind, competition.id, 1):
                raise ValueError("Cache metadata mismatch.")
            datetime.fromisoformat(data["updated_at"])
            return Problemset(data["name"], data["problems"])
        except (KeyError, TypeError, ValueError, OSError):
            raise CacheNotFound("Problem cache is missing or invalid. Run cfset refresh.") from None

    def save_competition(
        self, competition: Competition, problemset: Problemset, *, refresh: bool = False
    ) -> None:
        with self.lock("competition"):
            if refresh and self.current() != competition:
                raise ConfigError("The current competition changed during refresh. Run cfset refresh again.")
            self.write_json(self._cache_path(competition), {
                "version": 1,
                "type": competition.kind,
                "id": competition.id,
                "name": problemset.name,
                "problems": problemset.problems,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            if not refresh:
                self.write_json(self.config_path, {
                    "type": competition.kind, "id": competition.id, "language": competition.language,
                })
