"""In-memory continuity for a positively observed native UU process session.

A heartbeat proves detector health, never a new remote connection. No latch is
loaded from disk, and a controller restart always needs a recent AX observation.
"""
import copy
import math
import time


class SessionPolicy:
    MAX_AGE = 90
    UI_REASONS = frozenset((
        'uu_windows_unavailable', 'ax_snapshot_incomplete',
        'uu_main_content_unavailable', 'ax_local_control_count_unreadable',
        'ax_read_pending', 'uu_ui_hidden', 'uu_ui_minimized',
        'ax_home_disconnect_confirmation_pending',
        'ax_cached_controller_table_empty', 'ax_complete_home_no_local_control'))
    NEGATIVE_REASONS = frozenset(('ax_cached_controller_table_empty',
                                  'ax_complete_home_no_local_control'))

    def __init__(self, clock=time.time):
        self.clock = clock
        self._latch = None
        self._blocked = {}
        self._refresh_requested = None
        self._previous_mode = None

    @staticmethod
    def _number(value):
        try:
            return (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value))
        except (OverflowError, ValueError):
            return False

    @classmethod
    def _fresh(cls, value, now):
        return cls._number(value) and 0 <= now - value < cls.MAX_AGE

    @staticmethod
    def _epoch(raw):
        run = raw.get('run_id')
        detector, uu = raw.get('detector_pid'), raw.get('uuPID')
        if (not isinstance(run, str) or not run
                or any(not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                       for pid in (detector, uu))):
            return None
        return (run, detector, uu)

    def _clear(self):
        if self._latch:
            epoch, observed = self._latch
            self._blocked[epoch] = max(observed, self._blocked.get(epoch, float('-inf')))
        self._latch = None

    def needs_refresh(self, effective, mode, desired_enabled, command_id):
        """Consume one refresh request per epoch/enable intent/read episode.

        The caller only notifies the existing detector. It must wait for a new
        observation instead of interpreting the notification as session proof.
        """
        if (effective.get('state') == 'connected'
                and not effective.get('session_held')):
            self._refresh_requested = None
            return False
        epoch = self._epoch(effective)
        if (mode != 'ARMED' or not desired_enabled or epoch is None
                or effective.get('trusted') is not True
                or effective.get('sleeping') is not False
                or not self._fresh(effective.get('native_timestamp', effective.get('timestamp')), self.clock())
                or (effective.get('raw_state') != 'connected'
                    and effective.get('raw_reason') not in self.UI_REASONS)):
            return False
        key = (epoch, command_id)
        if key == self._refresh_requested:
            return False
        self._refresh_requested = key
        return True

    def evaluate(self, raw, mode):
        raw = raw if isinstance(raw, dict) else {}
        value = copy.deepcopy(raw)
        now = self.clock()
        previous_mode, self._previous_mode = self._previous_mode, mode
        epoch = self._epoch(raw)
        observed = raw.get('session_observed_at')
        value.update(session_policy_version=1, session_held=False,
                     session_source='native', raw_state=raw.get('state'),
                     raw_reason=raw.get('reason'), raw_timestamp=raw.get('timestamp'),
                     policy_evaluated_at=now)
        # A recheck may supply evidence, but native heartbeat and process identity
        # remain those of the long-running detector, never the one-shot probe.
        heartbeat = raw.get('native_timestamp', raw.get('timestamp'))
        healthy = (self._fresh(heartbeat, now) and raw.get('trusted') is True
                   and raw.get('sleeping') is False)
        previous = self._latch
        generation_changed = (previous is not None and epoch != previous[0]
                              and not (raw.get('state') == 'disconnected'
                                       and raw.get('reason') == 'uu_not_running'))
        if generation_changed:
            self._clear()
            if mode == 'ACTIVE':
                value.update(state='unknown', session_health_fault='generation_changed')
                return value
        if not healthy:
            self._clear()
            value['state'] = 'unknown'
            return value
        fresh_positive = (raw.get('state') == 'connected' and epoch is not None
                          and raw.get('session_held') is not True
                          and raw.get('session_source') != 'held_observation'
                          and self._fresh(observed, now))
        # Configuration may keep the engine ARMED across multiple evaluations
        # of one still-fresh observation. Only an uninterrupted duplicate keeps
        # its eligibility; leaving ACTIVE or losing evidence still retires it.
        repeated_armed_positive = (mode == previous_mode == 'ARMED'
                                   and fresh_positive
                                   and self._latch == (epoch, observed))
        if mode != 'ACTIVE' and not repeated_armed_positive:
            self._clear()
        negative = (raw.get('state') == 'disconnected' and
                    (raw.get('reason') == 'uu_not_running' or
                     (raw.get('session_negative_verified') is True
                      and raw.get('reason') in self.NEGATIVE_REASONS)))
        if negative:
            self._clear()
            value['session_source'] = 'verified_negative'
            return value
        if fresh_positive and observed > self._blocked.get(epoch, float('-inf')):
            if self._latch is None or observed >= self._latch[1]:
                self._latch = (epoch, observed)
            value['session_source'] = ('probe' if raw.get('session_verification') else 'native_observation')
            return value
        ui_unknown = (raw.get('state') == 'unknown' and raw.get('reason') in self.UI_REASONS)
        unverified_negative = (raw.get('state') == 'disconnected'
                               and raw.get('reason') in self.NEGATIVE_REASONS)
        cached_positive = (raw.get('state') == 'connected'
                           and self._number(observed) and observed <= now)
        if (mode == 'ACTIVE' and self._latch is not None and epoch == self._latch[0]
                and (ui_unknown or unverified_negative or cached_positive)):
            value.update(state='connected', session_held=True, session_source='held_observation',
                         session_observed_at=self._latch[1], timestamp=heartbeat)
        else:
            value['state'] = 'unknown'
            # Unknown faults outside the UI class break continuity permanently.
            if not (ui_unknown or unverified_negative or cached_positive):
                self._clear()
        return value
