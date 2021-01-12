import logging

from patroni.postgresql.connection import get_connection_cursor
from collections import defaultdict

logger = logging.getLogger(__name__)


def compare_slots(s1, s2, dbid='database'):
    return s1['type'] == s2['type'] and (s1['type'] == 'physical' or
                                         s1.get(dbid) == s2.get(dbid) and s1['plugin'] == s2['plugin'])


class SlotsHandler(object):

    def __init__(self, postgresql):
        self._postgresql = postgresql
        self._replication_slots = {}  # already existing replication slots
        self.schedule()

    def _query(self, sql, *params):
        return self._postgresql.query(sql, *params, retry=False)

    def process_permanent_slots(self, slots):
        """
        We want to expose information only about permanent slots that are configured in DCS.
        This function performs such filtering and in addition to that it checks for
        discrepancies and might schedule the resync.
        """

        if slots:
            ret = {}
            for slot in slots:
                if slot['slot_name'] in self._replication_slots:
                    if compare_slots(slot, self._replication_slots[slot['slot_name']], 'datoid'):
                        ret[slot['slot_name']] = slot['confirmed_flush_lsn']
                    else:
                        self._schedule_load_slots = True
            return ret

    def load_replication_slots(self):
        if self._postgresql.major_version >= 90400 and self._schedule_load_slots:
            replication_slots = {}
            cursor = self._query('SELECT slot_name, slot_type, plugin, database, datoid'
                                 ' FROM pg_catalog.pg_replication_slots')
            for r in cursor:
                value = {'type': r[1]}
                if r[1] == 'logical':
                    value.update(plugin=r[2], database=r[3], datoid=r[4])
                replication_slots[r[0]] = value
            self._replication_slots = replication_slots
            self._schedule_load_slots = False

    def ignore_replication_slot(self, cluster, name):
        slot = self._replication_slots[name]
        for matcher in cluster.config.ignore_slots_matchers:
            if ((matcher.get("name") is None or matcher["name"] == name)
               and all(not matcher.get(a) or matcher[a] == slot.get(a) for a in ('database', 'plugin', 'type'))):
                return True
        return False

    def drop_replication_slot(self, name):
        cursor = self._query(('SELECT pg_catalog.pg_drop_replication_slot(%s) WHERE EXISTS (SELECT 1 ' +
                              'FROM pg_catalog.pg_replication_slots WHERE slot_name = %s AND NOT active)'), name, name)
        # In normal situation rowcount should be 1, otherwise either slot doesn't exists or it is still active
        return cursor.rowcount == 1

    def _drop_incorrect_slots(self, cluster, slots):
        # drop old replication slots which are not presented in desired slots
        for name in set(self._replication_slots) - set(slots):
            if not self.ignore_replication_slot(cluster, name) and not self.drop_replication_slot(name):
                logger.error("Failed to drop replication slot '%s'", name)
                self._schedule_load_slots = True

        for name, value in slots.items():
            if name in self._replication_slots and not compare_slots(value, self._replication_slots[name]):
                logger.info("Trying to drop replication slot '%s' because value is changing from %s to %s",
                            name, self._replication_slots[name], value)
                if self.drop_replication_slot(name):
                    self._replication_slots.pop(name)
                else:
                    logger.error("Failed to drop replication slot '%s'", name)
                    self._schedule_load_slots = True

    def _ensure_physical_slots(self, slots):
        immediately_reserve = ', true' if self._postgresql.major_version >= 90600 else ''
        for name, value in slots.items():
            if name not in self._replication_slots and value['type'] == 'physical':
                try:
                    self._query(("SELECT pg_catalog.pg_create_physical_replication_slot(%s{0})" +
                                 " WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_replication_slots" +
                                 " WHERE slot_type = 'physical' AND slot_name = %s)").format(
                                     immediately_reserve), name, name)
                except Exception:
                    logger.exception("Failed to create physical replication slot '%s'", name)
                self._schedule_load_slots = True

    def _ensure_logical_slots_primary(self, slots):
        # Group logical slots to be created by database name
        logical_slots = defaultdict(dict)
        for name, value in slots.items():
            if value['type'] == 'logical':
                if name in self._replication_slots:
                    value['datoid'] = self._replication_slots[name]['datoid']
                else:
                    logical_slots[value['database']][name] = value

        # Create new logical slots
        for database, values in logical_slots.items():
            conn_kwargs = self._postgresql.config.local_connect_kwargs
            conn_kwargs['database'] = database
            with get_connection_cursor(**conn_kwargs) as cur:
                for name, value in values.items():
                    try:
                        cur.execute("SELECT pg_catalog.pg_create_logical_replication_slot(%s, %s)" +
                                    " WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_replication_slots" +
                                    " WHERE slot_type = 'logical' AND slot_name = %s)",
                                    (name, value['plugin'], name))
                    except Exception as e:
                        logger.exception("Failed to create logical replication slot '%s' plugin='%s': %r",
                                         name, value['plugin'], e)
                        slots.pop(name)
                    self._schedule_load_slots = True

    def sync_replication_slots(self, cluster):
        if self._postgresql.major_version >= 90400 and cluster.config:
            try:
                self.load_replication_slots()

                slots = cluster.get_replication_slots(self._postgresql.name, self._postgresql.role)

                self._drop_incorrect_slots(cluster, slots)

                self._ensure_physical_slots(slots)

                if self._postgresql.is_leader():
                    self._ensure_logical_slots_primary(slots)

                self._replication_slots = slots
            except Exception:
                logger.exception('Exception when changing replication slots')
                self._schedule_load_slots = True

    def schedule(self, value=None):
        if value is None:
            value = self._postgresql.major_version >= 90400
        self._schedule_load_slots = value
