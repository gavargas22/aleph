import logging
from pprint import pprint  # noqa

from aleph.authz import Authz
from aleph.core import db, cache
from aleph.model import Alert, Events, Entity
from aleph.index.indexes import entities_read_index
from aleph.index.util import search_safe, unpack_result, authz_query, MAX_PAGE
from aleph.logic.notifications import publish

log = logging.getLogger(__name__)


def get_alert(alert_id):
    key = cache.object_key(Alert, alert_id)
    data = cache.get_complex(key)
    if data is None:
        alert = Alert.by_id(alert_id)
        if alert is None:
            return
        data = {
            'id': alert.id,
            'query': alert.query,
            'role_id': alert.role_id,
            'notified_at': alert.notified_at,
            'created_at': alert.created_at,
            'updated_at': alert.updated_at
        }
        cache.set_complex(key, data, expire=cache.EXPIRE)
    return data


def refresh_alert(alert, sync=False):
    cache.kv.delete(cache.object_key(Alert, alert.id))


def check_alerts():
    """Go through all alerts."""
    Alert.dedupe()
    db.session.commit()
    for alert_id in Alert.all_ids():
        check_alert(alert_id)


def check_alert(alert_id):
    alert = Alert.by_id(alert_id)
    if alert is None or alert.role is None:
        return
    if not alert.role.is_alertable:
        return
    authz = Authz.from_role(alert.role)
    query = alert_query(alert, authz)
    index = entities_read_index(schema=Entity.THING)
    result = search_safe(index=index, body=query)
    for result in result.get('hits').get('hits', []):
        entity = unpack_result(result)
        if entity is None:
            continue
        log.info('Alert [%s]: %s', alert.query, entity.get('name'))
        params = {
            'alert': alert,
            'role': alert.role,
            'entity': entity
        }
        publish(Events.MATCH_ALERT,
                actor_id=entity.get('uploader_id'),
                params=params)

    alert.update()
    db.session.commit()
    db.session.close()


def alert_query(alert, authz):
    """Construct a search query to find new matching entities and documents
    for a particular alert. Update handling is done via a timestamp of the
    latest known result."""
    # Many users have bookmarked complex queries, otherwise we'd use a
    # precise match query.
    query = {
        'simple_query_string': {
            'query': alert.query,
            'fields': ['text'],
            'default_operator': 'AND',
            'minimum_should_match': '90%'
        }
    }
    filter_since = {
        'range': {
            'created_at': {'gt': alert.notified_at}
        }
    }
    return {
        'size': MAX_PAGE,
        'query': {
            'bool': {
                'should': [query],
                'filter': [filter_since, authz_query(authz)],
                'minimum_should_match': 1
            }
        }
    }
