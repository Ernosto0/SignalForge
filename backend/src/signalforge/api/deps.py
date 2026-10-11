"""Request dependencies. Tests override :func:`get_db` (and :func:`get_config`) through
``app.dependency_overrides``."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker

from signalforge.config import Defaults, get_defaults
from signalforge.db.session import get_session_factory


def get_db() -> sessionmaker[Session]:
    return get_session_factory()


def get_config() -> Defaults:
    return get_defaults()


DB = Annotated[sessionmaker[Session], Depends(get_db)]
Config = Annotated[Defaults, Depends(get_config)]
