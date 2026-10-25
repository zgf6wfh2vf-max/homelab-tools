from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace as Obj

from homelab_tools.podpoint.login import LoginCache, cached_session_class, remember, restore


class FakeSession:
    """Stands in for podpointclient's Session: create() 'signs in' and counts how often."""

    sign_ins = 0

    def __init__(self) -> None:
        self.user_id = self.session_id = None

    async def create(self) -> bool:
        FakeSession.sign_ins += 1
        self.user_id, self.session_id = "user-1", "sess-1"
        return True


def test_only_the_first_session_signs_in(tmp_path) -> None:
    FakeSession.sign_ins = 0
    cache = LoginCache(tmp_path / "login.json")
    session_class = cached_session_class(FakeSession, cache)
    for _ in range(24):  # a day of hourly token refreshes
        s = session_class()
        assert asyncio.run(s.create()) is True
        assert (s.user_id, s.session_id) == ("user-1", "sess-1")
    assert FakeSession.sign_ins == 1
    assert LoginCache.load(tmp_path / "login.json").user_id == "user-1"


def test_restore_forces_a_refresh_not_a_sign_in(tmp_path) -> None:
    cache = LoginCache(tmp_path / "login.json", refresh_token="rt", user_id="u", session_id="s")
    auth = Obj(refresh_token=None, access_token=None, access_token_expiry=None)
    assert restore(auth, cache)
    assert auth.refresh_token == "rt" and auth.access_token
    assert auth.access_token_expiry < datetime.now()  # the library will refresh with the token
    assert not restore(auth, LoginCache(tmp_path / "empty.json"))


def test_remember_saves_new_refresh_tokens_privately(tmp_path) -> None:
    path = tmp_path / "state" / "login.json"
    cache = LoginCache(path, user_id="u")
    remember(Obj(refresh_token="rt1"), cache)
    assert LoginCache.load(path).refresh_token == "rt1"
    assert (path.stat().st_mode & 0o777) == 0o600
    remember(Obj(refresh_token=None), cache)  # nothing new: unchanged
    assert LoginCache.load(path).complete


def test_clear_forgets_everything(tmp_path) -> None:
    cache = LoginCache(tmp_path / "login.json", refresh_token="rt", user_id="u")
    cache.save()
    cache.clear()
    assert not cache.complete and not (tmp_path / "login.json").exists()
    assert not LoginCache.load(tmp_path / "missing.json").complete
