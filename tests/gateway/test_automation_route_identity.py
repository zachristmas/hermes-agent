"""A producer turn borrows a committed human route: display drift must not refuse it, routing must."""
import pytest

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_automation import _route_identity


def _thread(**changes):
    fields = dict(platform=Platform.DISCORD, chat_id='1562079391750815826', chat_type='thread',
                  chat_name='The Study / #digest / Check incoming orders', user_id='u1', user_name='zach',
                  thread_id='1562079391750815826', scope_id='557800413682597888',
                  parent_chat_id='1560308306109661364', message_id='m1')
    fields.update(changes)
    return SessionSource(**fields)


def test_thread_rename_and_auto_thread_labels_do_not_change_route_identity():
    committed = _thread()
    stored = _thread(chat_name='The Study / #digest / can you spawn a claude sonnet session and have it use /leif',
                     auto_thread_created=True, auto_thread_initial_name='can you spawn a claude sonnet session',
                     chat_topic='renamed topic', message_id='1562079391750815826')
    assert _route_identity(committed) == _route_identity(stored)


@pytest.mark.parametrize('changes', [
    {'chat_id': '2'}, {'thread_id': '2'}, {'chat_type': 'channel'}, {'user_id': 'u2'}, {'user_name': 'someone else'},
    {'scope_id': '2'}, {'parent_chat_id': '2'}, {'profile': 'cs'}, {'platform': Platform.TELEGRAM}])
def test_every_routing_field_still_changes_route_identity(changes):
    assert _route_identity(_thread()) != _route_identity(_thread(**changes))
