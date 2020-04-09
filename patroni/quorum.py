import logging

logger = logging.getLogger(__name__)


def clamp(value, min=None, max=None):
    if min is not None and value < min:
        value = min
    if max is not None and value > max:
        value = max
    return value


class QuorumError(Exception):
    pass


class QuorumStateResolver(object):
    """
    Calculates a list of state transition tuples of the form `('sync'/'quorum',number,set_of_names)`

    Synchronous replication state is set in two places. PostgreSQL configuration sets how many and which nodes are
    needed for a commit to succeed, abbreviated as `numsync` and `sync` set here. DCS contains information about how
    many and which nodes need to be interrogated to be sure to see an xlog position containing latest confirmed commit,
    abbreviated as `quorum` and `voters` set. Both pairs have the meaning "ANY n OF set".

    The number of nodes needed for commit to succeed, `numsync`, is also called the replication factor.

    To guarantee zero lost transactions on failover we need to keep the invariant that at all times any subset of
    nodes that can acknowledge a commit overlaps with any subset of nodes that can achieve quorum to promote a new
    leader. Given a desired replication factor and a set of nodes able to participate in sync replication there
    is one optimal state satisfying this condition. Given the node set `active`, the optimal state is:

        sync = voters = active
        numsync = min(replication_factor, len(active))
        quorum = len(active) + 1 - numsync

    We need to be able to produce a series of state changes that take the system to this desired state from any
    other state arbitrary given arbitrary changes is node availability, configuration and interrupted transitions.

    To keep the invariant the rule to follow is that when increasing `numsync` or `quorum`, we need to perform the
    increasing operation first. When decreasing either, the decreasing operation needs to be performed later.

    For simplicity all sync members are considered equal. In Patroni the leader is actually special in that the last
    known leader is always guaranteed to have latest state. This leads to suboptimal number of transitions when
    increasing replication factor and quorum at the same time. If quorum must include leader, which happens when
    transitioning to sync replication, i.e. `quorum, voters = 1, {'leader'}`, then the special leader semantics
    mean that we could set `numsync, sync = 2, {'leader', 's1', 's2'}` in one step without worrying of the case that
    s1, s2 have latest xlog, but not leader. The benefit seems too small to warrant adding any complexity.
    """
    def __init__(self, quorum, voters, numsync, sync, active, sync_wanted):
        self.quorum = quorum
        self.voters = set(voters)
        self.numsync = numsync
        self.sync = set(sync)
        self.active = active
        self.sync_wanted = sync_wanted

    def check_invariants(self):
        if self.quorum and not (len(self.voters | self.sync) < self.quorum + self.numsync):
            raise QuorumError("Quorum and sync not guaranteed to overlap: nodes %d >= quorum %d + sync %d" %
                              (len(self.voters | self.sync), self.quorum, self.numsync))
        if not (self.voters <= self.sync or self.sync <= self.voters):
            raise QuorumError("Mismatched sets: quorum only=%s sync only=%s" %
                              (self.voters - self.sync, self.sync - self.voters))

    def quorum_update(self, quorum, voters):
        if quorum < 1:
            raise QuorumError("Quorum %d < 0 of (%s)" % (quorum, voters))
        self.quorum = quorum
        self.voters = voters
        self.check_invariants()
        logger.debug('quorum %s %s', self.quorum, self.voters)
        return 'quorum', self.quorum, self.voters

    def sync_update(self, numsync, sync):
        self.numsync = numsync
        self.sync = sync
        self.check_invariants()
        logger.debug('sync %s %s', self.numsync, self.sync)
        return 'sync', self.numsync, self.sync

    def __iter__(self):
        transitions = list(self._generate_transitions())
        # Merge 2 transitions of the same type to a single one. This is always safe because skipping the first
        # transition is equivalent to no one observing the intermediate state.
        for cur_transition, next_transition in zip(transitions, transitions[1:]+[None]):
            if next_transition and cur_transition[0] == next_transition[0]:
                continue
            yield cur_transition

    def _generate_transitions(self):
        logger.debug("Quorum state: quorum %s, voters %s, numsync %s, sync %s, active %s, sync_wanted %s",
                     self.quorum, self.voters, self.numsync, self.sync, self.active, self.sync_wanted)
        self.check_invariants()

        # Handle non steady state cases
        if self.sync < self.voters:
            logger.debug("Case 1: synchronous_standby_names subset of DCS state")
            # Case 1: quorum is superset of sync nodes. In the middle of changing quorum.
            # Evict from quorum dead nodes that are not being synced.
            remove_from_quorum = self.voters - (self.sync | self.active)
            if remove_from_quorum:
                yield self.quorum_update(
                    quorum=len(self.voters) - len(remove_from_quorum) + 1 - self.numsync,
                    voters=self.voters - remove_from_quorum)
            # Start syncing to nodes that are in quorum and alive
            add_to_sync = self.voters - self.sync
            if add_to_sync:
                yield self.sync_update(self.numsync, self.sync | add_to_sync)
        elif self.sync > self.voters:
            logger.debug("Case 2: synchronous_standby_names superset of DCS state")
            # Case 2: sync is superset of quorum nodes. In the middle of changing replication factor.
            # Add to quorum voters nodes that are already synced and active
            add_to_quorum = (self.sync - self.voters) & self.active
            if add_to_quorum:
                yield self.quorum_update(
                        quorum=self.quorum,
                        voters=self.voters | add_to_quorum)
            # Remove from sync nodes that are dead
            remove_from_sync = self.sync - self.voters
            if remove_from_sync:
                yield self.sync_update(
                        numsync=min(self.sync_wanted, len(self.sync) - len(remove_from_sync)),
                        sync=self.sync - remove_from_sync)

        # After handling these two cases quorum and sync must match.
        assert self.voters == self.sync

        safety_margin = self.quorum + self.numsync - len(self.voters | self.sync)
        if safety_margin > 1:
            logger.debug("Case 3: replication factor is bigger than needed")
            # Case 3: quorum or replication factor is bigger than needed. In the middle of changing replication factor.
            if self.numsync > self.sync_wanted:
                # Reduce replication factor
                new_sync = clamp(self.sync_wanted, min=len(self.voters) - self.quorum + 1, max=len(self.sync))
                yield self.sync_update(new_sync, self.sync)
            elif len(self.voters) > self.numsync:
                # Reduce quorum
                yield self.quorum_update(len(self.voters) + 1 - self.numsync, self.voters)

        # We are in a steady state point. Find if desired state is different and act accordingly.

        # If any nodes have gone away, evict them
        to_remove = self.sync - self.active
        if to_remove:
            logger.debug("Removing nodes: %s", to_remove)
            can_reduce_quorum_by = self.quorum - 1
            # If we can reduce quorum size try to do so first
            if can_reduce_quorum_by:
                # Pick nodes to remove by sorted order to provide deterministic behavior for tests
                remove = set(sorted(to_remove, reverse=True)[:can_reduce_quorum_by])
                yield self.sync_update(self.numsync, self.sync - remove)
                yield self.quorum_update(self.quorum - can_reduce_quorum_by, self.voters - remove)
                to_remove &= self.sync
            if to_remove:
                assert self.quorum == 1
                yield self.quorum_update(self.quorum, self.voters - to_remove)
                yield self.sync_update(self.numsync - len(to_remove), self.sync - to_remove)

        # If any new nodes, join them to quorum
        to_add = self.active - self.sync
        if to_add:
            # First get to requested replication factor
            logger.debug("Adding nodes: %s", to_add)
            increase_numsync_by = self.sync_wanted - self.numsync
            if increase_numsync_by:
                add = set(sorted(to_add)[:increase_numsync_by])
                yield self.sync_update(self.numsync + len(add), self.sync | add)
                yield self.quorum_update(self.quorum, self.voters | add)
                to_add -= self.sync
            if to_add:
                yield self.quorum_update(self.quorum + len(to_add), self.voters | to_add)
                yield self.sync_update(self.numsync, self.sync | to_add)

        # Apply requested replication factor change
        sync_increase = clamp(self.sync_wanted - self.numsync, min=2 - self.numsync, max=len(self.sync) - self.numsync)
        if sync_increase > 0:
            # Increase replication factor
            logger.debug("Increasing replication factor to %s", self.numsync + sync_increase)
            yield self.sync_update(self.numsync + sync_increase, self.sync)
            yield self.quorum_update(self.quorum - sync_increase, self.voters)
        elif sync_increase < 0:
            # Reduce replication factor
            logger.debug("Reducing replication factor to %s", self.numsync + sync_increase)
            yield self.quorum_update(self.quorum - sync_increase, self.voters)
            yield self.sync_update(self.numsync + sync_increase, self.sync)
