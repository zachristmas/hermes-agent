"""A legacy api_server Bot Chat is not a LOCAL session, so a peer turn must not claim it.

Adopting such a row needs a native create-capable transport; the API peer has none, which made
admission fail ``not_found`` before the turn could run. ``bot_chat_tip`` leaves it to the API lane.
"""
from types import SimpleNamespace

from gateway.platforms.api_server_bot_chat import bot_chat_tip


def _authority(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / 'state.db')
    return db, SimpleNamespace(db=db)


def test_legacy_api_server_bot_chat_is_not_claimed_for_authority_admission(tmp_path):
    db, authority = _authority(tmp_path)
    try:
        db.create_session('api_legacy', 'api_server')
        db.set_session_title('api_legacy', 'Bot Chat')
        assert bot_chat_tip(authority, 'api_legacy') is None
    finally:
        db.close()

