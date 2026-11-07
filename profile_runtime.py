"""Device selection and journaled display configuration, owned by the controller.

The UI never writes profiles or display settings. A recognized UU name is a
candidate, not a hardware identifier. Only exact catalog names / saved bindings
select a new profile; a fresh single controller can hand over an active session.
"""
import copy
import json
import math
import re
import time
import uuid
from pathlib import Path

from device_profiles import (CATALOG, ProfileStore, identify, make_profile,
                             normalize_name, preferred_mode, presets, target_spec,
                             validate_profile, _validate_config, catalog_snapshot,
                             canvas_dimensions, catalog_spec, profile_snapshot, fit_resolution)
from controller import atomic_json, read_json, parse_bool, TransitionInterrupted
from storage_recovery import is_storage_error


BINDING_ACTIONS = {'profile_bind', 'profile_unbind'}
PROFILE_ACTIONS = {'profile_apply', 'profile_select', 'profile_automatic', 'profile_cursor'} | BINDING_ACTIONS


class VirtualProvisioner:
    def __init__(self, bd, directory):
        self.bd, self.directory = bd, Path(directory)
        self.path = self.directory / 'managed-virtuals.json'

    @staticmethod
    def resolution_list(profile):
        # BetterDisplay's HiDPI generator halves the pixel sizes in this list.
        # Keep both the logical and 2x pixel entries, in canonical landscape.
        modes = [mode for orientation in ('landscape', 'portrait') for mode in presets(profile, orientation)]
        w, h = sorted(map(int, profile['resolution'].split('x')), reverse=True)
        modes.append(f'{w}x{h}')
        sizes = {tuple(sorted(map(int, mode.split('x')), reverse=True)) for mode in modes}
        sizes.update((w * 2, h * 2) for w, h in tuple(sizes))
        return ','.join(f'{w}x{h}' for w, h in sorted(sizes))

    @staticmethod
    def match(devices, profile):
        spec = target_spec(profile)
        found = [d for d in devices if d['identity'] in spec['identities']]
        if len(found) > 1:
            raise RuntimeError('虚拟屏标识重复，请先检查 BetterDisplay')
        if not found:
            if any(d['name'] == spec['name'] for d in devices):
                raise RuntimeError('同名虚拟屏属于其他配置，未修改该屏幕')
            return None
        device = found[0]
        if device['kind'] != 'VirtualScreen' or device['name'] != spec['name']:
            raise RuntimeError('虚拟屏名称或类型已改变，请核对设备配置')
        return device

    def ensure(self, profile):
        profile = validate_profile(profile)
        descriptor = profile['virtual']
        records = read_json(self.path, {'version': 1, 'devices': {}})
        if (not isinstance(records, dict) or records.get('version') != 1
                or not isinstance(records.get('devices'), dict)):
            raise RuntimeError('受管理虚拟屏记录损坏，已保留原件')
        identity = descriptor['identity']
        existing = records['devices'].get(identity)
        if existing and existing.get('profile_id') != profile['id']:
            raise RuntimeError('虚拟屏已被另一个设备配置占用')
        # Durable reservation makes retry/restart idempotent even if creation
        # succeeds but the CLI response or the following save is lost.
        records['devices'][identity] = {'profile_id': profile['id'], 'virtual': descriptor,
                                        'phase': 'reserved'}
        atomic_json(self.path, records)
        if hasattr(self.bd, '_identifier_cache'):
            self.bd._identifier_cache = None
        devices = self.bd.inventory()
        device = self.match(devices, profile)
        fields = {'useResolutionList': 'on', 'resolutionList': self.resolution_list(profile),
                  'virtualScreenHiDPI': 'on',
                  'flipped': 'on' if profile['orientation'] == 'portrait' else 'off'}
        if device and device['connected']:
            raise RuntimeError('修改虚拟屏规格前需要安全恢复实体屏')
        if getattr(self.bd, 'dry_run', False):
            raise RuntimeError('Dry run refuses virtual provisioning')
        if device is None:
            if not any(d['connected'] and not d.get('mirrored') for d in devices):
                raise RuntimeError('当前没有可用输出，暂不创建新屏幕')
            self.bd._run('create', '-type=VirtualScreen',
                '-virtualScreenName=' + descriptor['name'],
                '-virtualScreenVendorNumber=' + str(descriptor['vendor']),
                '-virtualScreenModelNumber=' + str(descriptor['model']),
                '-virtualScreenSerial=' + str(descriptor['serial']), '-connected=off',
                *('-' + k + '=' + v for k, v in fields.items()))
            self.bd._identifier_cache = None
            # BetterDisplay identifiers are synchronous; the display itself is
            # intentionally offline. Never retry create with a random identity.
            device = self.match(self.bd.inventory(), profile)
            if device is None:
                raise RuntimeError('BetterDisplay 未返回新虚拟屏，保留创建记录供重试')
        else:
            self.bd._run('set', '-type=VirtualScreen', '-tagID=' + device['tag'],
                         *('-' + k + '=' + v for k, v in fields.items()))
        self.bd._identifier_cache = None
        device = self.match(self.bd.inventory(), profile)
        if not device or device['connected']:
            raise RuntimeError('虚拟屏配置后的身份或离线状态核验失败')
        for key in ('useResolutionList', 'virtualScreenHiDPI', 'flipped'):
            actual = self.bd._run('get', '-type=VirtualScreen', '-tagID=' + device['tag'], '-' + key)
            if parse_bool(actual) != (fields[key] == 'on'):
                raise RuntimeError('虚拟屏设置未生效：' + key)
        raw_modes = self.bd._run('get', '-type=VirtualScreen', '-tagID=' + device['tag'], '-resolutionList')
        # BetterDisplay emits pairs or width/height/flags triples, sometimes
        # wrapped in Swift Optional(). Parse rows, never a flat number stream.
        try:
            rows = json.loads(re.sub(r'Optional\((\d+)\)', r'\1', raw_modes))
            if (not isinstance(rows, list) or not rows or
                    any(not isinstance(row, list) or len(row) not in (2, 3)
                        or any(type(n) is not int for n in row) for row in rows)):
                raise ValueError('invalid resolution rows')
            actual_modes = {tuple(row[:2]) for row in rows}
        except (ValueError, TypeError):
            if not re.fullmatch(r'\s*\d+x\d+(?:\s*,\s*\d+x\d+)*\s*', raw_modes):
                raise RuntimeError('无法读取 BetterDisplay 分辨率列表')
            actual_modes = {tuple(map(int, mode.strip().split('x'))) for mode in raw_modes.split(',')}
        expected = {tuple(map(int, s.split('x'))) for s in fields['resolutionList'].split(',')}
        if not expected.issubset(actual_modes):
            raise RuntimeError('虚拟屏分辨率列表核验失败')
        records['devices'][identity]['phase'] = 'configured'
        records['devices'][identity]['orientation'] = profile['orientation']
        records['devices'][identity]['resolution_list'] = fields['resolutionList']
        records['devices'][identity]['canvas_dimensions'] = list(canvas_dimensions(profile)[:2])
        atomic_json(self.path, records)
        return device


class DeviceCoordinator:
    """Serialize configuration commands with Engine under its process lock."""
    def __init__(self, engine, provisioner=None):
        self.engine, self.directory = engine, engine.directory
        self.store = ProfileStore(self.directory)
        self.provisioner = provisioner or VirtualProvisioner(engine.bd, self.directory)
        self.config, self.error, self.last_result = None, None, None
        self.error_category = None
        self.configuration_error = None
        self.results = read_json(self.directory / 'command-results.json', [])
        if not isinstance(self.results, list):
            self.results = []
        self.identity = {'status': 'none', 'source': 'name', 'reason': ''}
        self.display_transition_started = False
        self.last_auto_attempt = None
        self.profile_recovery_blocked = False
        self.transaction_path = self.directory / 'profile-transaction.json'
        self.intent_path = self.directory / 'command-intent.json'
        try:
            cursor = read_json(self.directory / 'cursor-profile-config.json', {})
            if not isinstance(cursor, dict):
                raise ValueError('光标配置记录损坏，已保留原件')
            scale = cursor.get('scale', 3) if cursor.get('enabled', True) else 1
            self.config = self.store.bootstrap(engine._preferred_mode()['resolution'], scale)
            self.last_result = read_json(self.directory / 'command-result.json')
            self._recover_transaction()
            intent = read_json(self.intent_path, {})
            if not isinstance(intent, dict):
                raise ValueError('操作意图记录损坏，已保留原件')
            if intent.get('status') == 'applying':
                receipt = self._terminal_receipt(intent)
                if receipt:
                    # The receipt is durable before the final intent write.
                    # A restart in that gap must finish the intent, not replace
                    # a confirmed outcome with an interruption error.
                    self._record_result(receipt)
                    if self.last_result != receipt and (not self.last_result or self.last_result.get('id') == receipt['id']):
                        atomic_json(self.directory / 'command-result.json', receipt)
                        self.last_result = receipt
                    intent['status'] = receipt['status']
                    atomic_json(self.intent_path, intent)
                elif (intent.get('action') in ('pause', 'recover')
                        and intent.get('id') == self.engine.state.get('last_command')
                        and not self.engine.state.get('desired_enabled')):
                    self._settle_manual_intent(intent)
                else:
                    self._result(intent['id'], 'error', '上次操作在完成确认前中断；请核对当前设置后重试')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.profile_recovery_blocked = True
            self.error_category = 'configuration'
            self.error = '无法读取或恢复设备配置：' + str(exc)
            self.configuration_error = self.error

    def _recover_transaction(self):
        """profiles.json is the commit point; never replay display mutations."""
        transaction = read_json(self.transaction_path, {})
        if not isinstance(transaction, dict):
            raise ValueError('设备配置事务记录损坏，已保留原件')
        if not transaction:
            return
        status = transaction.get('status')
        if status not in ('applying', 'complete', 'error', 'interrupted'):
            raise ValueError('未知设备配置事务状态，已保留原件')
        intent = read_json(self.intent_path, {})
        if not isinstance(intent, dict):
            raise ValueError('操作意图记录损坏，已保留原件')
        completing_receipt = (status == 'complete' and intent.get('status') == 'applying'
                              and intent.get('id') == transaction.get('id'))
        if status != 'applying' and not completing_receipt:
            return
        if (transaction.get('version') != 1 or not isinstance(transaction.get('id'), str)
                or not transaction['id'] or not isinstance(transaction.get('before'), dict)
                or not isinstance(transaction.get('candidate'), dict)):
            raise ValueError('设备配置事务记录损坏，已保留原件')
        before = _validate_config(transaction['before'])
        candidate = _validate_config(transaction['candidate'])
        for field in ('cursor_before', 'cursor_after'):
            if field in transaction and transaction[field] is not None and not isinstance(transaction[field], dict):
                raise ValueError('设备配置事务光标记录损坏，已保留原件')
        committed = self.config == candidate
        if not committed and self.config != before:
            raise ValueError('设备配置已在事务之外改变，已保留配置与事务供核对')
        # The projection snapshot was journaled before any configuration side
        # effect. A crash after profile replacement must complete it; an earlier
        # crash must roll it back. Legacy transactions have no snapshot.
        projection = 'cursor_after' if committed else 'cursor_before'
        if projection in transaction:
            self._restore_cursor_projection(transaction[projection])
        transaction['status'] = 'complete' if committed else 'interrupted'
        atomic_json(self.transaction_path, transaction)
        terminal = next((receipt for receipt in [self.last_result, *reversed(self.results)]
                         if isinstance(receipt, dict) and receipt.get('id') == transaction['id']
                         and receipt.get('status') in ('ok', 'error')
                         and isinstance(receipt.get('message'), str)
                         and (receipt.get('action') == transaction['action'] if transaction.get('action')
                              else receipt.get('action') in PROFILE_ACTIONS)), None)
        if not terminal:
            self._result(transaction['id'], 'ok' if committed else 'error',
                         '设备配置已保存；屏幕恢复完成后按当前设置继续' if committed else
                         '配置过程被中断；原屏幕恢复完成后可重试',
                         current=not self.last_result or self.last_result.get('id') == transaction['id'],
                         action=transaction.get('action'))
        else:
            self._record_result(terminal)
            if self.last_result != terminal and (not self.last_result or self.last_result.get('id') == terminal['id']):
                atomic_json(self.directory / 'command-result.json', terminal)
                self.last_result = terminal

    def _settle_manual_intent(self, intent):
        if self.engine.state.get('journal'):
            self._result(intent['id'], 'applying', '正在恢复实体屏，恢复记录已保留')
        elif self.engine.state['mode'] == 'PAUSED':
            self._result(intent['id'], 'ok', '已恢复并暂停')

    def _profile(self, profile_id=None, config=None):
        config = self.config if config is None else config
        profile_id = profile_id or config['selected_id']
        return next(p for p in config['profiles'] if p['id'] == profile_id)

    def _result(self, command_id, status, message, *, current=True, action=None):
        intent = read_json(self.intent_path, {})
        receipt = {'id': command_id, 'status': status, 'message': message,
                   'timestamp': self.engine.clock()}
        if action:
            receipt['action'] = action
        if isinstance(intent, dict) and intent.get('id') == command_id:
            receipt['action'] = intent.get('action')
        # History retains the outcome even when the latest receipt is replaced
        # by a recovery command before the native requester observes it.
        self._record_result(receipt)
        if current:
            self.last_result = receipt
            atomic_json(self.directory / 'command-result.json', receipt)
        if status in ('ok', 'error'):
            if isinstance(intent, dict) and intent.get('id') == command_id and intent.get('status') == 'applying':
                intent['status'] = status
                atomic_json(self.intent_path, intent)
        if (current and status == 'ok' and not self.profile_recovery_blocked
                and self.error_category != 'configuration'):
            self.error = None

    def _record_result(self, receipt):
        history = [r for r in self.results if isinstance(r, dict) and r.get('id') != receipt['id']]
        history = (history + [copy.deepcopy(receipt)])[-20:]
        if history != self.results:
            atomic_json(self.directory / 'command-results.json', history)
        self.results = history

    def _terminal_receipt(self, intent):
        candidates = [self.last_result, *reversed(self.results)]
        return next((receipt for receipt in candidates
                     if isinstance(receipt, dict)
                     and receipt.get('id') == intent.get('id')
                     and receipt.get('action') == intent.get('action')
                     and receipt.get('action') in PROFILE_ACTIONS | {'enable', 'pause', 'recover'}
                     and receipt.get('status') in ('ok', 'error')
                     and isinstance(receipt.get('message'), str)), None)

    def _intent(self, command):
        atomic_json(self.intent_path, {'version': 1, 'id': command['id'],
                                      'action': command['action'], 'status': 'applying'})

    def publish(self, detector):
        config = self.config or {'profiles': [], 'automatic': False, 'selected_id': None}
        engine_status = self.engine.status()
        engine_error = self.engine.state.get('last_error')
        context = self.engine.state.get('recovery_context') or {}
        # Ordinary recovery reasons describe progress, not failures. Publishing
        # them as an error makes the window retain a misleading warning after
        # reconnect. A distinct restoration failure must still be visible.
        if (context.get('category') in ('manual', 'manual_stop', 'remote_disconnected',
                                        'sleep', 'profile_change', 'process_restart', 'device_change')
                and engine_error == context.get('reason')):
            engine_error = None
        bindings = []
        current = None
        current_names = {}
        names = detector.get('controllers', [])
        for name in names if isinstance(names, list) else []:
            try:
                current_names.setdefault(normalize_name(name), name)
            except (ValueError, TypeError):
                pass
        # Saved relationships remain visible when a live observation ages out.
        # Freshness gates the binding action, not the persisted mapping display.
        if isinstance(names, list) and len(names) == 1 and len(current_names) == 1:
            current = next(iter(current_names))
        for key, profile_id in config.get('bindings', {}).items():
            label = config.get('binding_labels', {}).get(key)
            if not label:
                label = current_names.get(key)
            if not label:
                label = next((name for item in CATALOG for name in [item['label'], *item['aliases']]
                              if normalize_name(name) == key), key)
            bindings.append({'key': key, 'name': label, 'profile_id': profile_id})
        snapshot = {'version': 1, 'timestamp': self.engine.clock(),
            'controller_pid': engine_status['controller_pid'],
            'mode': self.engine.state['mode'], 'desired_enabled': self.engine.state['desired_enabled'],
            'connection': detector.get('state', 'unknown'), 'controllers': detector.get('controllers', []),
            # Read-only fields so the windows share the menu-bar status vocabulary
            # (same keys StatusBarPresentation reads from status.json).
            'trusted': detector.get('trusted') is True, 'sleeping': detector.get('sleeping') is True,
            'status_detail': {key: engine_status.get(key) for key in (
                'mode', 'desired_enabled', 'last_command', 'waiting_for', 'last_error',
                'recovery_context', 'next_retry', 'recovery_pending', 'exit_after_restore',
                'exit_recovery_pending', 'exit_completed', 'active_profile_id',
                'preferred_virtual_mode', 'timestamp', 'controller_pid')},
            'active_profile_id': self.engine.state.get('active_profile_id') if self.engine.state['mode'] == 'ACTIVE' else None,
            'selected_id': config['selected_id'], 'automatic': config['automatic'],
            'profiles': [profile_snapshot(p) for p in config['profiles']], 'catalog': catalog_snapshot(),
            'bindings': bindings,
            'current_binding': next((b for b in bindings if b['key'] == current), None),
            'binding_available': bool(self.config is not None and current
                                      and detector.get('state') == 'connected'
                                      and detector.get('trusted') is True and not detector.get('sleeping')
                                      and type(detector.get('timestamp')) in (int, float)
                                      and math.isfinite(detector['timestamp'])
                                      and 0 <= self.engine.clock()-detector['timestamp'] < self.engine.signal_max_age
                                      and type(detector.get('activeControllerCount')) is int
                                      and detector['activeControllerCount'] == 1
                                      and self.engine.state['mode'] != 'RESTORING'),
            'presets': {p['id']: presets(p) for p in config['profiles']},
            'identity': self.identity, 'last_result': self.last_result, 'results': self.results,
            'configuration_error': self.configuration_error or (self.error if self.profile_recovery_blocked else None),
            'operation_error': self.error if (not self.profile_recovery_blocked
                and self.error_category == 'operation') else None,
            'engine_error': engine_error,
            'error': self.configuration_error or self.error or engine_error}
        atomic_json(self.directory / 'workbench-status.json', snapshot)

    def _set_engine_profile(self, profile):
        if self.engine.state.get('journal'):
            raise RuntimeError('旧屏幕恢复尚未完成，保留原恢复记录')
        self.engine.state.update(active_profile_id=profile['id'], target_spec=target_spec(profile),
                                 preferred_virtual_mode=preferred_mode(profile),
                                 entry_preserve_preferred=True,
                                 managed_virtual_targets=[target_spec(p) for p in self.config['profiles']])
        self.engine._save()

    def _cursor(self, profile):
        value = {'enabled': profile['cursor_scale'] != 1, 'scale': profile['cursor_scale'], 'scope': 'all_supported'}
        path = self.directory / 'cursor-profile-config.json'
        if read_json(path) != value:
            atomic_json(path, value)

    def _restore_cursor_projection(self, previous):
        path = self.directory / 'cursor-profile-config.json'
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic_json(path, previous)

    def _candidate(self, command):
        if self.config is None:
            raise ValueError(self.error or '设备配置不可用')
        candidate = copy.deepcopy(self.config)
        action = command['action']
        if action == 'profile_automatic':
            if type(command.get('automatic')) is not bool:
                raise ValueError('自动匹配必须为开或关')
            candidate['automatic'] = command['automatic']
            return candidate, self._profile(config=candidate)
        profile_id = command.get('profile_id')
        if action == 'profile_cursor':
            current_id = (self.engine.state.get('active_profile_id') if self.engine.state['mode'] == 'ACTIVE'
                          else candidate['selected_id']) or candidate['selected_id']
            if profile_id and profile_id != current_id:
                raise ValueError('光标快捷命令只适用于当前设备；请在工作台应用其他设备的配置')
            profile_id = current_id
        if profile_id:
            try:
                profile = copy.deepcopy(self._profile(profile_id, candidate))
            except StopIteration:
                raise ValueError('设备配置不存在')
            if action == 'profile_apply':
                draft = command.get('draft')
                if not isinstance(draft, dict):
                    raise ValueError('配置草稿必须为对象')
                if set(draft) - {'label', 'catalog_id', 'native_width', 'native_height', 'ppi', 'orientation', 'resolution', 'cursor_scale', 'display_area', 'display_variant'}:
                    raise ValueError('未知的配置字段')
                if 'catalog_id' in draft and draft['catalog_id'] != profile.get('catalog_id'):
                    raise ValueError('请添加新设备来更改屏幕型号或原生参数')
                specification = (catalog_spec(profile['catalog_id'], draft.get('display_variant', profile.get('display_variant')))
                                 if profile.get('catalog_id') else profile)
                for key in ('native_width', 'native_height', 'ppi'):
                    if key in draft and draft[key] != specification.get(key):
                        raise ValueError('请添加新设备来更改屏幕型号或原生参数')
                old_canvas = canvas_dimensions(profile)
                old_short = min(profile['native_width'], profile['native_height'])
                old_variant = profile.get('display_variant')
                old_modes = presets(profile)
                old_mode_index = old_modes.index(profile['resolution']) if profile['resolution'] in old_modes else None
                if specification.get('display_variants'):
                    profile.update({k: specification[k] for k in ('native_width', 'native_height', 'ppi', 'display_variant')})
                profile.update({k: draft[k] for k in ('label', 'orientation', 'resolution', 'cursor_scale', 'display_area', 'display_variant') if k in draft})
                if 'resolution' not in draft and canvas_dimensions(profile) != old_canvas:
                    if old_variant != profile.get('display_variant') and old_mode_index is not None:
                        profile['resolution'] = presets(profile)[old_mode_index]
                    else:
                        logical_short = min(map(int, profile['resolution'].split('x')))
                        logical_short = round(logical_short * min(profile['native_width'], profile['native_height']) / old_short)
                        profile['resolution'] = fit_resolution(f'{logical_short}x{logical_short}', profile)
            elif action == 'profile_cursor':
                profile['cursor_scale'] = command.get('scale')
            profile = validate_profile(profile)
            candidate['profiles'] = [profile if p['id'] == profile['id'] else p for p in candidate['profiles']]
        elif action == 'profile_apply':
            draft = copy.deepcopy(command.get('draft'))
            if not isinstance(draft, dict):
                raise ValueError('需要新设备的配置草稿')
            catalog_id = draft.pop('catalog_id', None)
            if 'new_profile_id' in command:
                draft['id'] = command['new_profile_id']
            profile = make_profile(catalog_id, **draft)
            candidate['profiles'].append(profile)
        else:
            raise ValueError('请先选择设备')
        candidate['selected_id'] = profile['id']
        if 'automatic' in command:
            if type(command['automatic']) is not bool:
                raise ValueError('自动匹配必须为开或关')
            candidate['automatic'] = command['automatic']
        if command.get('bind_name'):
            key = normalize_name(command['bind_name'])
            candidate['bindings'][key] = profile['id']
            candidate.setdefault('binding_labels', {})[key] = command['bind_name']
        # Validate the entire candidate without writing or touching displays.
        from device_profiles import _validate_config
        return _validate_config(candidate), profile

    def _latest_manual(self):
        try:
            command = read_json(self.directory / 'command.json', {})
        except (ValueError, OSError):
            return None
        return (command if isinstance(command, dict) and command.get('id') != self.engine.state.get('last_command')
                and command.get('action') in ('pause', 'recover') else None)

    def _settle_consumed_manual(self):
        try:
            intent = read_json(self.intent_path, {})
        except (ValueError, OSError):
            intent = {}
        if (isinstance(intent, dict) and intent.get('status') == 'applying' and intent.get('action') in ('pause', 'recover')
                and intent.get('id') == self.engine.state.get('last_command')
                and not self.engine.state.get('desired_enabled')):
            self._settle_manual_intent(intent)
        try:
            command = read_json(self.directory / 'command.json', {})
        except (ValueError, OSError):
            return
        if (not isinstance(command, dict) or command.get('action') not in ('pause', 'recover')
                or command.get('id') != self.engine.state.get('last_command')):
            return
        command_id = command['id']
        if any(r.get('id') == command_id and r.get('status') in ('ok', 'error') for r in self.results):
            return
        if self.engine.state['mode'] == 'PAUSED' and not self.engine.state.get('journal'):
            self._result(command_id, 'ok', '已恢复并暂停')
        elif self.engine.state['mode'] == 'RESTORING':
            self._result(command_id, 'applying', '正在恢复实体屏，恢复记录已保留')

    def _live_reconfigure(self, profile, candidate, detector):
        devices = self.engine._inventory()
        device = self.provisioner.match(devices, profile)
        # Offline definitions may be prepared while the old virtual output is
        # still usable. Never rewrite a live definition just to select its mode.
        if not device or not device['connected']:
            self.provisioner.ensure(profile)
        elif not self._mode_available(device, profile):
            # A manually added size or a stale live definition may lack the
            # requested 2x mode. Prepare it through the same journaled buffer.
            self._live_rebuild(profile, candidate, detector)
            return
        self.display_transition_started = True
        self.engine.reconfigure_active(target_spec(profile), preferred_mode(profile), profile['id'],
                                       [target_spec(p) for p in candidate['profiles']],
                                       self._single_controller(detector))

    def _mode_available(self, device, profile):
        if device.get('resolution') == profile['resolution'] and parse_bool(device.get('hiDPI')):
            return True
        query = getattr(self.engine.bd, '_virtual_mode_number', None)
        if query is not None:
            try:
                query(device, preferred_mode(profile))
            except RuntimeError as exc:
                if str(exc) == 'Requested HiDPI mode is not available on the exact virtual output':
                    return False
                raise
        return True

    def _live_rebuild(self, profile, candidate, detector):
        """Rebuild the same bound virtual definition using a temporary output."""
        current = self.engine._verified_target()
        width, height = map(int, current['resolution'].split('x'))
        bridge = make_profile(id='transition-' + uuid.uuid4().hex, label='切换缓冲屏',
                              native_width=width * 2, native_height=height * 2, ppi=profile['ppi'],
                              orientation='portrait' if width < height else 'landscape',
                              resolution=current['resolution'], cursor_scale=profile['cursor_scale'])
        descriptor = bridge['virtual']
        record = dict(identity=descriptor['identity'], name=descriptor['name'], kind='VirtualScreen',
                      profile_id=bridge['id'], fallback_target=copy.deepcopy(current))
        journal = self.engine.state.get('journal')
        if not journal or self.engine.state['mode'] != 'ACTIVE':
            raise RuntimeError('Virtual rebuild requires the active recovery journal')
        # Reservation precedes even offline creation: recovery can remove a
        # buffer created just before the process exits, without guessing names.
        journal.setdefault('transient_virtuals', {})[record['identity']] = record
        journal.setdefault('virtual_targets', {})[record['identity']] = copy.deepcopy(record)
        self.display_transition_started = True
        self.engine._save()
        self.provisioner.ensure(bridge)
        managed = [target_spec(p) for p in candidate['profiles']] + [target_spec(bridge)]
        owner = self._single_controller(detector)
        self.engine.reconfigure_active(target_spec(bridge), preferred_mode(bridge),
                                       self.engine.state['active_profile_id'], managed, owner)
        # The old bound output is now offline; the buffer is verified and main.
        # A pending pause/disconnect must win before changing its definition.
        self.engine.entering = True
        try:
            self.engine._entry_checkpoint()
            self.provisioner.ensure(profile)
            self.engine._entry_checkpoint()
        finally:
            self.engine.entering = False
        self.engine.reconfigure_active(target_spec(profile), preferred_mode(profile),
                                       profile['id'], managed, owner)
        self.engine.discard_transients()
        self.engine.state['managed_virtual_targets'] = [target_spec(p) for p in candidate['profiles']]
        self.engine._save()

    def _apply(self, command, detector):
        if self.profile_recovery_blocked:
            raise ValueError(self.error or '设备配置恢复尚未完成')
        self.display_transition_started = False
        if self.engine.state['mode'] == 'RESTORING':
            raise ValueError('屏幕仍在恢复，请恢复完成后再修改设备配置')
        command_id = command['id']
        candidate, profile = self._candidate(command)
        active = self.engine.state['mode'] == 'ACTIVE'
        previous = self._profile(self.engine.state.get('active_profile_id')) if active else self._profile()
        remote = (active and detector.get('state') == 'connected' and detector.get('trusted') is True)
        count = detector.get('activeControllerCount')
        names = detector.get('controllers', [])
        if active and ((isinstance(count, int) and count > 1) or len(names) > 1):
            raise ValueError('多个控制端在线，先保持当前配置；只连接一台后再应用')
        if active and not remote:
            raise ValueError('正在确认远程状态，已保留当前画面；状态明确后再应用')
        geometry = any(profile.get(k) != previous.get(k) for k in
                       ('id', 'orientation', 'resolution', 'native_width', 'native_height'))
        geometry = geometry or canvas_dimensions(profile) != canvas_dimensions(previous)
        if command['action'] == 'profile_automatic':
            geometry = False
        if remote and geometry and not self._single_controller(detector):
            raise ValueError('正在确认当前控制设备，保留现有画面；确认完成后再应用方向或设备变更')
        transaction = {'version': 1, 'id': command_id, 'action': command['action'], 'status': 'applying',
                       'before': self.config, 'candidate': candidate, 'profile_id': profile['id']}
        changes_cursor = ((remote and command['action'] != 'profile_automatic')
                          or command['action'] == 'profile_cursor')
        if changes_cursor:
            transaction['cursor_before'] = read_json(self.directory / 'cursor-profile-config.json')
            transaction['cursor_after'] = {'enabled': profile['cursor_scale'] != 1,
                                           'scale': profile['cursor_scale'], 'scope': 'all_supported'}
        atomic_json(self.transaction_path, transaction)
        self._result(command_id, 'applying', '正在安全应用设备配置')
        self.publish(detector)
        if remote and geometry:
            definition_change = (profile['id'] == previous['id'] and any(
                profile.get(k) != previous.get(k) for k in ('orientation', 'native_width', 'native_height')))
            definition_change = definition_change or (profile['id'] == previous['id']
                and canvas_dimensions(profile) != canvas_dimensions(previous))
            if definition_change:
                self._live_rebuild(profile, candidate, detector)
            else:
                self._live_reconfigure(profile, candidate, detector)
            if self.engine.state['mode'] != 'ACTIVE':
                raise RuntimeError('新虚拟屏尚未通过画面核验')
        # Offline saves never connect or create displays. Selection/preparation
        # happens only on the next confirmed matching connection.
        # Cursor preferences are a projection of the device profile. If either
        # persistence step fails, restore its previous projection and leave the
        # authoritative profile unchanged. No display recovery for cursor-only
        # failures or a rejected metadata edit.
        cursor_path = self.directory / 'cursor-profile-config.json'
        old_cursor = read_json(cursor_path)
        try:
            if changes_cursor:
                self._cursor(profile)
            self.config = self.store.save(candidate)
        except Exception:
            self._restore_cursor_projection(old_cursor)
            raise
        transaction['status'] = 'complete'
        atomic_json(self.transaction_path, transaction)
        self._result(command_id, 'ok', '已生效于当前远程画面' if remote else '已保存，下次匹配连接时生效')
        self.error = None
        self.configuration_error = None

    def _apply_binding(self, command, detector):
        """Commit name metadata without entering the display/cursor pipeline."""
        if self.profile_recovery_blocked:
            raise ValueError(self.error or '设备配置恢复尚未完成')
        if self.engine.state['mode'] == 'RESTORING':
            raise ValueError('屏幕仍在恢复，请恢复完成后再修改绑定')
        if self.config is None:
            raise ValueError(self.error or '设备配置不可用')
        candidate = self.store.load()
        if candidate is None:
            raise ValueError('设备配置不可用')
        before = copy.deepcopy(candidate)
        profile_id = command.get('profile_id')
        if profile_id not in {p['id'] for p in candidate['profiles']}:
            raise ValueError('设备配置不存在')
        name = command.get('name')
        key = normalize_name(name)
        old = candidate['bindings'].get(key)
        if command['action'] == 'profile_bind':
            if (self._single_controller(detector) != key
                    or type(detector.get('activeControllerCount')) is not int
                    or detector['activeControllerCount'] != 1):
                raise ValueError('请确认只有当前一台控制设备在线后再绑定')
            if 'expected_profile_id' not in command:
                raise ValueError('绑定需要原绑定目标，请刷新后重试')
            expected = command['expected_profile_id']
            if expected is not None and not isinstance(expected, str):
                raise ValueError('原绑定目标无效')
            if old != profile_id and old != expected:
                raise ValueError('绑定已改变，请刷新后重试')
            candidate['bindings'][key] = profile_id
            candidate.setdefault('binding_labels', {})[key] = name
        else:
            if old != profile_id:
                raise ValueError('绑定已改变，请刷新后重试')
            del candidate['bindings'][key]
            candidate.setdefault('binding_labels', {}).pop(key, None)
        transaction = {'version': 1, 'id': command['id'], 'action': command['action'], 'status': 'applying',
                       'before': before, 'candidate': candidate, 'profile_id': profile_id,
                       'metadata_only': True}
        urgent = self._latest_manual()
        if urgent:
            self.engine.tick(detector, urgent)
            self._result(urgent['id'], 'applying' if self.engine.state.get('journal') else 'ok',
                         '正在恢复实体屏' if self.engine.state.get('journal') else '已恢复并暂停')
            raise ValueError('已按你的暂停或恢复操作取消绑定修改')
        atomic_json(self.transaction_path, transaction)
        try:
            self._result(command['id'], 'applying', '正在保存设备绑定')
            urgent = self._latest_manual()
            if urgent:
                self.engine.tick(detector, urgent)
                self._result(urgent['id'], 'applying' if self.engine.state.get('journal') else 'ok',
                             '正在恢复实体屏' if self.engine.state.get('journal') else '已恢复并暂停')
                raise ValueError('已按你的暂停或恢复操作取消绑定修改')
            self.config = self.store.save(candidate)
        except Exception:
            transaction['status'] = 'error'
            atomic_json(self.transaction_path, transaction)
            raise
        transaction['status'] = 'complete'
        atomic_json(self.transaction_path, transaction)
        self._refresh_identity(detector)
        self._result(command['id'], 'ok', '已保存绑定，下次连接时生效' if command['action'] == 'profile_bind'
                     else '已解除绑定，下次连接时生效')
        self.error = None
        self.configuration_error = None

    def _single_controller(self, detector):
        """A held UI snapshot keeps a session alive but cannot select a device."""
        now = self.engine.clock()
        if (detector.get('state') != 'connected' or detector.get('trusted') is not True
                or detector.get('session_held') or detector.get('sleeping')
                or detector.get('session_health_fault')
                or detector.get('raw_state', 'connected') != 'connected'):
            return None
        for key in ('timestamp', 'session_observed_at'):
            stamp = detector.get(key)
            if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or not 0 <= now - stamp < self.engine.signal_max_age):
                return None
        names = detector.get('controllers')
        count = detector.get('activeControllerCount')
        if not isinstance(names, list) or len(names) != 1 or (count is not None and count != 1):
            return None
        try:
            return normalize_name(names[0])
        except ValueError:
            return None

    def _handover(self, detector):
        owner = self.engine.state.get('active_controller_name')
        current = self._single_controller(detector)
        if (not self.profile_recovery_blocked and self.engine.state['mode'] == 'ACTIVE' and self.config and self.config['automatic']
                and owner and current and current != owner):
            if self.identity['status'] != 'matched':
                # An unrecognized controller has no safe owned target. End the
                # old device session and wait for an explicit binding as before.
                self.engine._begin_restore('Remote device changed', afterwards='ARMED', category='device_change')
                self.last_auto_attempt = None
                return
            self.display_transition_started = False
            self.last_auto_attempt = ('discovery', current, detector.get('run_id'),
                                      self.engine.state.get('last_command'))
            old_cursor, cursor_started = None, False
            try:
                candidate = copy.deepcopy(self.config)
                if self.identity.get('profile_id'):
                    profile = self._profile(self.identity['profile_id'])
                else:
                    profile = make_profile(self.identity['catalog_id'])
                    candidate['profiles'].append(profile)
                    # Persist ownership before creating an offline definition;
                    # a retry must reuse the same reserved virtual identity.
                    self.config = self.store.save(candidate)
                    self.identity['profile_id'] = profile['id']
                candidate['selected_id'] = profile['id']
                self.last_auto_attempt = (profile['id'], canvas_dimensions(profile), profile['resolution'],
                                          detector.get('run_id'), self.engine.state.get('last_command'))
                if profile['id'] != self.engine.state.get('active_profile_id'):
                    self._live_reconfigure(profile, candidate, detector)
                    old_cursor = read_json(self.directory / 'cursor-profile-config.json')
                    cursor_started = True
                    self._cursor(profile)
                self.config = self.store.save(candidate)
                self.engine.state.update(active_controller_name=current, entry_controller_name=current)
                self.engine._save()
                self.error = None
                self.configuration_error = None
            except Exception as exc:
                if is_storage_error(exc):
                    raise
                message = str(exc)
                if cursor_started:
                    try:
                        self._restore_cursor_projection(old_cursor)
                    except OSError as rollback_error:
                        message += '; cursor rollback: ' + str(rollback_error)
                destination = exc.destination if isinstance(exc, TransitionInterrupted) else 'ARMED'
                self.engine._begin_restore(message, afterwards=destination, category='device_change_failed')
                self.error = message
                self.error_category = 'configuration'
                self.configuration_error = self.error

    def _auto_prepare(self, detector):
        if self.profile_recovery_blocked:
            return {**detector, 'profile_entry_blocked': True}
        if not self.config or self.engine.state['mode'] != 'ARMED' or not self.engine.state['desired_enabled']:
            return detector
        if detector.get('state') != 'connected' or detector.get('trusted') is not True:
            return detector
        if detector.get('sleeping') or detector.get('session_health_fault'):
            return detector
        owner = self._single_controller(detector)
        if not owner:
            return {**detector, 'profile_entry_blocked': True}
        if self.error and self.last_auto_attempt == ('discovery', owner, detector.get('run_id'),
                                                     self.engine.state.get('last_command')):
            return {**detector, 'profile_entry_blocked': True}
        stamp = detector.get('timestamp')
        if not isinstance(stamp, (int, float)) or not 0 <= self.engine.clock() - stamp < self.engine.signal_max_age:
            return {**detector, 'profile_entry_blocked': True}
        profile = self._profile()
        if self.config['automatic']:
            if self.identity['status'] != 'matched':
                # Hold ARMED without telling Engine the connection is absent.
                return {**detector, 'profile_entry_blocked': True}
            if self.identity.get('profile_id'):
                profile = self._profile(self.identity['profile_id'])
            else:
                profile = make_profile(self.identity['catalog_id'])
                candidate = copy.deepcopy(self.config)
                candidate['profiles'].append(profile)
                candidate['selected_id'] = profile['id']
                self.config = self.store.save(candidate)
                self.identity['profile_id'] = profile['id']
        # Do not run provisioning repeatedly on heartbeat or while a prior
        # failure is awaiting an explicit retry. Enable/new session can retry.
        key = (profile['id'], canvas_dimensions(profile), profile['resolution'],
               detector.get('run_id'), self.engine.state.get('last_command'))
        if self.error and self.last_auto_attempt == key:
            return {**detector, 'profile_entry_blocked': True}
        self.last_auto_attempt = key
        devices = self.engine._inventory()
        device = self.provisioner.match(devices, profile)
        if device and device['connected']:
            current_size = str(device.get('resolution', '')).split('x')
            orientation_mismatch = (len(current_size) == 2 and all(n.isdigit() for n in current_size)
                                    and (int(current_size[0]) < int(current_size[1])) !=
                                    (profile['orientation'] == 'portrait'))
            valid_size = len(current_size) == 2 and all(n.isdigit() and int(n) > 0 for n in current_size)
            canvas = canvas_dimensions(profile)
            aspect_mismatch = (valid_size and abs(int(current_size[0]) / int(current_size[1])
                                                  - canvas[0] / canvas[1]) > .005)
            if orientation_mismatch or aspect_mismatch or not self._mode_available(device, profile):
                if not valid_size:
                    raise RuntimeError('当前虚拟屏尺寸无法核验，恢复记录已保留')
                # This temporary entry view describes the currently visible
                # canvas. The authoritative device specifications remain saved.
                current_profile = make_profile(id=profile['id'], label=profile['label'],
                    native_width=int(current_size[0]) * 2, native_height=int(current_size[1]) * 2,
                    ppi=profile['ppi'], resolution=device['resolution'], cursor_scale=profile['cursor_scale'],
                    orientation='portrait' if int(current_size[0]) < int(current_size[1]) else 'landscape',
                    virtual=profile['virtual'])
                self._set_engine_profile(current_profile)
                self.engine.state['entry_controller_name'] = owner
                try:
                    self.engine._enter()
                    self._live_rebuild(profile, self.config, detector)
                except Exception as exc:
                    if is_storage_error(exc):
                        raise
                    destination = exc.destination if isinstance(exc, TransitionInterrupted) else 'ARMED'
                    self.engine._begin_restore(str(exc), afterwards=destination, category='profile_change_failed')
                    raise
            else:
                self._set_engine_profile(profile)
        else:
            self.provisioner.ensure(profile)
            self._set_engine_profile(profile)
        old_cursor, cursor_started = None, False
        try:
            old_cursor = read_json(self.directory / 'cursor-profile-config.json')
            cursor_started = True
            self._cursor(profile)
            if self.config['selected_id'] != profile['id']:
                candidate = copy.deepcopy(self.config)
                candidate['selected_id'] = profile['id']
                self.config = self.store.save(candidate)
        except Exception as exc:
            if is_storage_error(exc):
                raise
            message = str(exc)
            if cursor_started:
                try:
                    self._restore_cursor_projection(old_cursor)
                except OSError as rollback_error:
                    message += '; cursor rollback: ' + str(rollback_error)
            # An already-online target may have required an early enter and
            # rebuild above. Persistence still belongs to that same transition.
            if self.engine.state.get('journal'):
                self.engine._begin_restore(message, afterwards='ARMED', category='profile_change_failed')
            raise
        self.engine.state['entry_controller_name'] = owner
        self.error = None
        self.configuration_error = None
        return detector

    def _refresh_identity(self, detector):
        self.identity = {'status': 'none', 'source': 'name', 'reason': ''}
        try:
            if self.config:
                names = detector.get('controllers', [])
                self.identity = identify(names, self.config)
                count = detector.get('activeControllerCount')
                if isinstance(count, int) and count > 1:
                    self.identity = {'status': 'multiple', 'source': 'name', 'reason': '多个控制端在线，保持当前配置'}
        except (ValueError, TypeError):
            self.identity = {'status': 'unknown', 'source': 'name', 'reason': '当前 UU 名称不可用于匹配'}

    def tick(self, detector, command=None):
        skip_auto = False
        self._refresh_identity(detector)
        if (isinstance(command, dict) and command.get('action') in (PROFILE_ACTIONS | {'enable', 'pause', 'recover'})
                and (command.get('action') not in ('pause', 'recover')
                     or command.get('id') == self.engine.state.get('last_command'))
                and any(r.get('id') == command.get('id') and r.get('status') in ('ok', 'error') for r in self.results)):
            command = None  # A terminal receipt also prevents replay after interrupted status publication.
        core_command = (command if isinstance(command, dict) and self.engine._valid_command(command)
                        and command.get('id') != self.engine.state.get('last_command') else None)
        # Manual recovery always precedes profile discovery or provisioning.
        if core_command and core_command['action'] in ('pause', 'recover'):
            self._intent(core_command)
            for receipt in list(self.results):
                if (receipt.get('action') == 'enable' and receipt.get('status') == 'applying'
                        and receipt.get('id') != core_command['id']):
                    self._result(receipt['id'], 'error', '启用请求已被暂停或恢复操作取消',
                                 current=False, action='enable')
            self._result(core_command['id'], 'applying', '正在恢复并暂停')
            self.publish(detector)
            self.engine.tick(detector, core_command)
            self._result(core_command['id'], 'ok' if not self.engine.state.get('journal') else 'applying',
                         '已恢复并暂停' if not self.engine.state.get('journal') else '正在恢复实体屏')
            self.publish(detector)
            return detector
        if isinstance(command, dict) and command.get('action') in BINDING_ACTIONS:
            command_id = command.get('id')
            if isinstance(command_id, str) and command_id and len(command_id) <= 200:
                if command_id != self.engine.state.get('last_command'):
                    self._intent(command)
                    self.engine.state['last_command'] = command_id
                    self.engine._save()
                    try:
                        self._apply_binding(command, detector)
                    except Exception as exc:
                        if is_storage_error(exc):
                            raise
                        self.error = str(exc)
                        self.error_category = 'operation'
                        self._result(command_id, 'error', str(exc))
                    # Preserve session health/recovery while preventing this
                    # metadata command from starting a new display session.
                    # A repeated durable command falls through normally below.
                    effective = {**detector, 'profile_entry_blocked': True}
                    self.engine.tick(effective)
                    self._settle_consumed_manual()
                    self.publish(detector)
                    return effective
            command = None
        self._handover(detector)
        configuration_command = (isinstance(command, dict) and command.get('action') in PROFILE_ACTIONS
                                 and command.get('id') != self.engine.state.get('last_command'))
        if configuration_command:
            command_id = command.get('id')
            if not isinstance(command_id, str) or not command_id or len(command_id) > 200:
                command = None
            else:
                self._intent(command)
                self.engine.state['last_command'] = command_id
                self.engine._save()
                had_transaction = False
                try:
                    self._apply(command, detector)
                except Exception as exc:
                    if is_storage_error(exc):
                        raise
                    skip_auto = True
                    transaction = read_json(self.transaction_path, {})
                    had_transaction = transaction.get('id') == command_id and transaction.get('status') == 'applying'
                    if had_transaction:
                        transaction['status'] = 'error'
                        atomic_json(self.transaction_path, transaction)
                        # Invalid drafts never get here. A failed actual switch
                        # retains the exact journal and recovers physical output.
                        destination = (exc.destination if isinstance(exc, TransitionInterrupted) else
                                       ('PAUSED' if not self.engine.state['desired_enabled'] else 'ARMED'))
                        if self.display_transition_started or isinstance(exc, TransitionInterrupted):
                            self.engine._begin_restore(str(exc), afterwards=destination, category='profile_change_failed')
                        if self.display_transition_started and self.engine.state.get('journal'):
                            try:
                                self.engine._restore()
                            except Exception:
                                pass  # Engine retains the journal and retry path.
                    self.error = str(exc)
                    self.error_category = 'configuration' if had_transaction and self.display_transition_started else 'operation'
                    if self.error_category == 'configuration':
                        self.configuration_error = self.error
                    self._result(command_id, 'error', str(exc))
                command = None
        elif isinstance(command, dict) and command.get('action') in PROFILE_ACTIONS:
            command = None  # A durable processed command is not a new core command.
        # Enable is consumed first, without entering until a device is chosen.
        if isinstance(command, dict) and command.get('action') == 'enable':
            self._intent(command)
            self._result(command['id'], 'applying', '正在启用自动化')
            self.publish(detector)
            self.engine.tick({**detector, 'profile_entry_blocked': True}, command)
            command = None
            self.last_auto_attempt = None
            self.error = None if self.config and not self.profile_recovery_blocked else self.error
        try:
            detector = {**detector, 'profile_entry_blocked': True} if skip_auto else self._auto_prepare(detector)
        except Exception as exc:
            if is_storage_error(exc):
                raise
            self.error = str(exc)
            self.error_category = 'configuration'
            self.configuration_error = self.error
            detector = {**detector, 'profile_entry_blocked': True}
        if not self.config:
            detector = {**detector, 'profile_entry_blocked': True}
        before_mode = self.engine.state['mode']
        self.engine.tick(detector, command)
        if self.engine.state['mode'] == 'ACTIVE' and (before_mode != 'ACTIVE' or not self.engine.state.get('active_controller_name')):
            self.engine.state['active_controller_name'] = self._single_controller(detector)
            self.engine._save()
        enable_id = (core_command['id'] if core_command and core_command['action'] == 'enable' else
                     self.last_result['id'] if self.last_result and self.last_result.get('action') == 'enable'
                     and self.last_result.get('status') == 'applying' else None)
        if enable_id:
            state = self.engine.state
            if self.error or not state.get('desired_enabled') or state['mode'] == 'PAUSED':
                self._result(enable_id, 'error',
                             self.error or state.get('last_error') or '自动化未启用，请核对当前状态后重试')
            elif state['mode'] == 'RESTORING':
                self._result(enable_id, 'applying', '正在恢复实体屏，恢复完成后继续自动化')
            elif state['mode'] in ('ARMED', 'ACTIVE'):
                self._result(enable_id, 'ok',
                             '已启用自动化' if state['mode'] == 'ACTIVE' else '已启用，等待匹配连接')
            else:
                self._result(enable_id, 'error', state.get('last_error') or '自动化尚未就绪')
        elif self.last_result and self.last_result.get('status') == 'applying' and self.engine.state['mode'] == 'PAUSED' and not self.engine.state.get('journal'):
            self._result(self.last_result['id'], 'ok', '已恢复并暂停')
        # Capture a manually selected BetterDisplay mode after Engine's genuine
        # positive-session commit; keep it per device across future switches.
        if self.config and not self.profile_recovery_blocked and self.engine.state['mode'] == 'ACTIVE':
            profile_id = self.engine.state.get('active_profile_id')
            if profile_id:
                try:
                    profile = copy.deepcopy(self._profile(profile_id))
                    resolution = self.engine._preferred_mode()['resolution']
                    if resolution != profile['resolution']:
                        profile['resolution'] = resolution
                        profile = validate_profile(profile)
                        candidate = copy.deepcopy(self.config)
                        candidate['profiles'] = [profile if p['id'] == profile_id else p for p in candidate['profiles']]
                        self.config = self.store.save(candidate)
                except (ValueError, StopIteration):
                    # Respect a valid user-selected mode even if its aspect is
                    # outside the profile catalog; do not force it back.
                    pass
        if detector.get('state') == 'disconnected':
            self.last_auto_attempt = None
        self._settle_consumed_manual()
        self.publish(detector)
        return detector
