"""Verify the Mac keyboard adapter with native event objects and native loops.

Nothing native runs by default. --native opens one listen-only keyboard tap
using existing permissions, services Foundation's main run loop, and invokes
the production adapter with synthetic CGEvent objects on its actual HID loop.
It never posts input, changes Caps Lock, moves/hides the cursor, creates a
window, activates an application, connects a peer or forwards input anywhere.
Actual user callbacks are counted only; their key values are never inspected
or recorded. This does not test a physical keyboard or a Bluetooth peer.
"""
import argparse
from collections import Counter
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ProbeState:
    def __init__(self):
        self.lock = threading.RLock()
        self.local = threading.local()
        self.context = (1, True, False)
        self.counts = Counter()
        self.phase_counts = {}
        self.copy_metadata = {}
        self.errors = []
        self.listener = None
        self.pause_main = threading.Event()
        self.main_paused = threading.Event()
        self.done = threading.Event()
        self.delivery_queued = threading.Event()

    def increment(self, name, count=1, metadata=None):
        with self.lock:
            self.counts[name] += count
            if metadata is not None:
                self.phase_counts.setdefault(metadata['phase'], Counter())[name] += count

    @contextmanager
    def stimulus(self, metadata):
        previous = getattr(self.local, 'stimulus', None)
        self.local.stimulus = metadata
        try:
            yield
        finally:
            self.local.stimulus = previous

    def current(self):
        return getattr(self.local, 'stimulus', None)

    def event_context(self):
        # Production main-queue code intentionally does not acquire the engine
        # lock. Tuple replacement here models that same immutable snapshot.
        return self.context

    def callback(self, kind, key, injected=False):
        metadata = self.current()
        if metadata is None:
            # Do not examine key, its type, its repr or user Unicode content.
            self.increment('actual_callbacks_count_only')
            return
        self.increment('synthetic_callbacks', metadata=metadata)
        self.increment('synthetic_' + kind, metadata=metadata)
        self.increment('injected_true' if injected else 'injected_false', metadata=metadata)
        if threading.get_ident() != self.listener.ident:
            raise RuntimeError('Synthetic callback did not execute on the listener HID thread')
        if metadata['expected_callbacks'] == 0:
            raise RuntimeError('A stale or stopped synthetic event was delivered')
        if key != metadata['key'] or bool(injected) != metadata['injected']:
            # Never include key values in the error, even for synthetic input.
            raise RuntimeError('Synthetic key identity or injected flag changed')
        if kind not in metadata['kinds']:
            raise RuntimeError('Synthetic transition changed')

    def snapshot(self):
        with self.lock:
            return dict(counters=dict(self.counts),
                        phases={name: dict(counts) for name, counts in self.phase_counts.items()},
                        retained_copy_metadata=len(self.copy_metadata))


def native_probe(args):
    import Foundation as F
    import HIServices
    import Quartz as Q
    from sharemouse import mac_keyboard as M

    evidence = dict(
        mode='native-keyboard', pairs_requested=args.pairs, phases=[], errors=[],
        boundary='Real listen-only keyboard tap and native main/HID loops; direct synthetic production adapter calls only. Actual input is aggregate-only.',
        posts_os_input=False, changes_caps_lock=False, moves_or_hides_cursor=False,
        creates_or_activates_windows=False, records_key_values=False,
        forwards_input=False, bluetooth_used=False, external_network_used=False,
        native_objects=True, physical_keyboard_verified=False)
    if not Q.CGPreflightListenEventAccess() or not HIServices.AXIsProcessTrusted():
        evidence.update(skipped='Existing Input Monitoring and Accessibility permissions are required; no permission is requested',
                        listeners_alive=[], driver_alive=False)
        return evidence

    state = ProbeState()
    original_event_class = M.Darwin.NSEvent
    original_copy = Q.CGEventCreateCopy
    original_perform = Q.CFRunLoopPerformBlock
    main_ident = threading.get_ident()
    retained_events = []

    class MainThreadEventProxy:
        def eventWithCGEvent_(self, event):
            metadata = state.current()
            if threading.get_ident() != main_ident:
                state.increment('nsevent_wrong_thread', metadata=metadata)
                raise RuntimeError('NSEvent conversion attempted outside the main thread')
            state.increment('nsevent_main_conversions', metadata=metadata)
            if metadata is not None and metadata['category'] != 'media':
                raise RuntimeError('An ordinary Quartz key entered NSEvent conversion')
            return original_event_class.eventWithCGEvent_(event)

        def __getattr__(self, name):
            return getattr(original_event_class, name)

    def guarded_copy(event):
        result = original_copy(event)
        metadata = state.current()
        if metadata is not None:
            if threading.get_ident() != state.listener.ident:
                raise RuntimeError('Synthetic media copy did not run on the HID thread')
            state.increment('cg_event_copies', metadata=metadata)
            with state.lock:
                state.copy_metadata[id(result)] = metadata
        return result

    def perform(loop, mode, callback):
        metadata = state.current()
        if metadata is None:
            return original_perform(loop, mode, callback)
        if threading.get_ident() != main_ident:
            raise RuntimeError('Deferred media scheduling did not run on the main thread')
        if loop is not state.listener._loop:
            raise RuntimeError('Media callback was scheduled on another run loop')
        state.increment('deferred_deliveries_queued', metadata=metadata)
        def invoke():
            with state.stimulus(metadata):
                state.increment('deferred_delivery_attempts', metadata=metadata)
                callback()
        result = original_perform(loop, mode, invoke)
        state.delivery_queued.set()
        return result

    def forbidden(*args, **kwargs):
        state.increment('forbidden_global_input_calls')
        raise RuntimeError('Probe attempted a forbidden global input or cursor operation')

    def create_media(pressed, flags):
        # Construct on the main thread. The event is never posted to macOS.
        event = original_event_class.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            M.Darwin.NSSystemDefined, (0, 0), flags, 0, 0, None,
            M.Darwin.kSystemDefinedEventMediaKeysSubtype,
            (16 << 16) | ((0x0a if pressed else 0x0b) << 8), 0)
        cg_event = event.CGEvent()
        if cg_event is None or Q.CGEventGetType(cg_event) != M.Darwin.NSSystemDefined:
            raise RuntimeError('Could not construct a native system-defined media CGEvent')
        retained_events.extend((event, cg_event))
        return cg_event

    def create_key(vk, event_type, flags):
        event = Q.CGEventCreateKeyboardEvent(None, vk, event_type != Q.kCGEventKeyUp)
        if event is None:
            raise RuntimeError('Could not construct a native keyboard CGEvent')
        Q.CGEventSetType(event, event_type)
        Q.CGEventSetFlags(event, flags)
        retained_events.append(event)
        return event

    flags = (0, Q.kCGEventFlagMaskAlphaShift,
             Q.kCGEventFlagMaskAlphaShift | Q.kCGEventFlagMaskShift | Q.kCGEventFlagMaskCommand)
    media_events = {(pressed, index): create_media(pressed, value)
                    for index, value in enumerate(flags) for pressed in (True, False)}
    caps_events = {index: create_key(0x39, Q.kCGEventFlagsChanged, value)
                   for index, value in enumerate(flags)}
    normal_events = {(pressed, index): create_key(0x31, Q.kCGEventKeyDown if pressed else Q.kCGEventKeyUp, value)
                     for index, value in enumerate(flags) for pressed in (True, False)}

    listener = M.KeyboardListener(on_press=lambda key, injected=False: state.callback('press', key, injected),
                                 on_release=lambda key, injected=False: state.callback('release', key, injected),
                                 event_context=state.event_context, event_lock=state.lock)
    state.listener = listener
    original_decode = listener._decode_media_on_main

    def decode(event, injected, loop, context):
        with state.lock:
            metadata = state.copy_metadata.pop(id(event), None)
        with state.stimulus(metadata):
            state.increment('main_decode_attempts', metadata=metadata)
            original_decode(event, injected, loop, context)
    listener._decode_media_on_main = decode

    def metadata(phase, category, injected=False, expected=True):
        return dict(phase=phase, category=category, injected=injected,
                    key=M.Darwin.Key.media_play_pause if category == 'media' else
                    M.Darwin.Key.caps_lock if category == 'caps' else M.Darwin.Key.space,
                    kinds=('press', 'release'), expected_callbacks=(2 if category == 'caps' else 1) if expected else 0)

    def wait_for(condition, message, timeout=8):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            time.sleep(.002)

    def hid(action):
        completed, failures = threading.Event(), []
        def invoke():
            try:
                if threading.get_ident() != listener.ident:
                    raise RuntimeError('Synthetic stimulus missed the real listener HID loop')
                action()
            except BaseException as exc:
                failures.append(exc)
            finally:
                completed.set()
        loop = getattr(listener, '_loop', None)
        if loop is None or not listener.running:
            raise RuntimeError('Native keyboard listener is not running')
        # Use the saved real API; only production deferred deliveries are wrapped.
        original_perform(loop, Q.kCFRunLoopDefaultMode, invoke)
        Q.CFRunLoopWakeUp(loop)
        wait_for(completed.is_set, 'Native listener did not service a synthetic stimulus')
        if failures:
            raise failures[0]

    def dispatch(event, info):
        with state.stimulus(info):
            state.increment('synthetic_adapter_calls', metadata=info)
            listener._handle_message(None, Q.CGEventGetType(event), event, None, info['injected'])

    def phase_callbacks(name):
        with state.lock:
            return state.phase_counts.get(name, {}).get('synthetic_callbacks', 0)

    def finish_phase(name, expected_callbacks, expected_conversions=None):
        # HID and main barriers ensure that zero-delivery checks actually waited
        # for both queues, rather than merely sampling before work ran.
        main_done = threading.Event()
        F.NSOperationQueue.mainQueue().addOperationWithBlock_(main_done.set)
        wait_for(main_done.is_set, 'Main-queue barrier timed out')
        hid(lambda: None)
        actual = phase_callbacks(name)
        with state.lock:
            conversions = state.phase_counts.get(name, {}).get('nsevent_main_conversions', 0)
        if actual != expected_callbacks:
            raise RuntimeError('Synthetic callback count differs in phase ' + name)
        if expected_conversions is not None and conversions != expected_conversions:
            raise RuntimeError('NSEvent conversion count differs in phase ' + name)
        evidence['phases'].append(dict(name=name, expected_callbacks=expected_callbacks,
                                      actual_callbacks=actual, nsevent_main_conversions=conversions, passed=True))

    def pause_main():
        state.pause_main.set()
        wait_for(state.main_paused.is_set, 'Main run loop did not pause between probe phases')

    def resume_main():
        state.pause_main.clear()
        wait_for(lambda: not state.main_paused.is_set(), 'Main run loop did not resume')

    def driver():
        try:
            wait_for(lambda: getattr(listener, '_loop', None) is not None,
                     'Native keyboard tap did not start')
            if not listener.running or not listener.is_alive():
                raise RuntimeError('Native keyboard tap stopped during startup')
            for category, objects in (('media', media_events), ('normal', normal_events)):
                name = category + '-batch'
                for index in range(args.pairs):
                    injected = bool(index % 2)
                    variant = index % len(flags)
                    def pair(c=category, v=variant, value=injected, events=objects):
                        for pressed in (True, False):
                            info = metadata(name, c, value)
                            info['kinds'] = ('press' if pressed else 'release',)
                            dispatch(events[(pressed, v)], info)
                    hid(pair)
                finish_phase(name, args.pairs * 2, args.pairs * 2 if category == 'media' else 0)
            name = 'caps-batch'
            for index in range(args.pairs):
                info = metadata(name, 'caps', bool(index % 2))
                hid(lambda e=caps_events[index % len(flags)], m=info: dispatch(e, m))
            finish_phase(name, args.pairs * 2, 0)

            name = 'stale-before-main-decode'
            pause_main()
            def stale_before():
                dispatch(media_events[(True, 1)], metadata(name, 'media', expected=False))
                with state.lock:
                    state.context = (state.context[0] + 1, True, False)
            hid(stale_before)
            resume_main()
            finish_phase(name, 0, 0)

            name = 'stale-after-main-decode'
            state.delivery_queued.clear()
            hold, entered, finished = threading.Event(), threading.Event(), threading.Event()
            def held_stale():
                try:
                    dispatch(media_events[(False, 2)], metadata(name, 'media', injected=True, expected=False))
                    entered.set()
                    if not hold.wait(5):
                        raise TimeoutError('HID test hold was not released')
                except BaseException as exc:
                    state.errors.append('HID stale phase ' + type(exc).__name__ + ': ' + str(exc))
                finally:
                    finished.set()
            # Asynchronous submission keeps driver available to release the HID
            # loop only after native main-thread decoding queued its callback.
            original_perform(listener._loop, Q.kCFRunLoopDefaultMode, held_stale)
            Q.CFRunLoopWakeUp(listener._loop)
            try:
                wait_for(entered.is_set, 'HID stale-phase setup timed out')
                wait_for(state.delivery_queued.is_set, 'Native media decoding did not queue a delivery')
                with state.lock:
                    state.context = (state.context[0] + 1, False, False)
            finally:
                hold.set()
            wait_for(finished.is_set, 'HID stale-phase hold did not finish')
            finish_phase(name, 0, 1)

            with state.lock:
                state.context = (state.context[0] + 1, True, False)
            before_name, after_name = 'stop-before-main-decode', 'stop-after-main-decode'
            state.delivery_queued.clear()
            hold, entered, stopped = threading.Event(), threading.Event(), threading.Event()
            def held_stop():
                try:
                    dispatch(media_events[(True, 0)], metadata(after_name, 'media', expected=False))
                    entered.set()
                    if not hold.wait(5):
                        raise TimeoutError('HID stop-phase hold was not released')
                    dispatch(media_events[(False, 1)], metadata(before_name, 'media', expected=False))
                except BaseException as exc:
                    state.errors.append('HID stop phase ' + type(exc).__name__ + ': ' + str(exc))
                finally:
                    listener.stop()
                    stopped.set()
            original_perform(listener._loop, Q.kCFRunLoopDefaultMode, held_stop)
            Q.CFRunLoopWakeUp(listener._loop)
            try:
                wait_for(entered.is_set, 'HID stop-phase setup timed out')
                wait_for(state.delivery_queued.is_set, 'Stop-phase media callback was not queued')
                pause_main()
            finally:
                hold.set()
            wait_for(stopped.is_set, 'Pending keyboard work did not stop')
            resume_main()
            listener.join(5)
            if listener.is_alive():
                raise RuntimeError('Native keyboard listener did not stop')
            main_done = threading.Event()
            F.NSOperationQueue.mainQueue().addOperationWithBlock_(main_done.set)
            wait_for(main_done.is_set, 'Stopped main-queue work did not drain')
            for name, conversions in ((before_name, 0), (after_name, 1)):
                if phase_callbacks(name):
                    raise RuntimeError('Pending keyboard work escaped stop guard')
                with state.lock:
                    actual = state.phase_counts.get(name, {}).get('nsevent_main_conversions', 0)
                if actual != conversions:
                    raise RuntimeError('Stop-phase conversion count differs')
                evidence['phases'].append(dict(name=name, expected_callbacks=0, actual_callbacks=0,
                                              nsevent_main_conversions=actual, passed=True))
        except BaseException as exc:
            state.errors.append(type(exc).__name__ + ': ' + str(exc))
        finally:
            state.pause_main.clear()
            listener.stop()
            try:
                listener.join(5)
            except BaseException as exc:
                state.errors.append('Listener cleanup ' + type(exc).__name__ + ': ' + str(exc))
            state.done.set()

    with ExitStack() as stack:
        stack.enter_context(patch.object(M.Darwin, 'NSEvent', MainThreadEventProxy()))
        stack.enter_context(patch.object(Q, 'CGEventCreateCopy', guarded_copy))
        stack.enter_context(patch.object(Q, 'CFRunLoopPerformBlock', perform))
        # Trap both Quartz functions and pynput's imported aliases. The probe
        # does not need these functions even for setup or cleanup.
        for namespace in (Q, M.Darwin):
            for name in ('CGEventPost', 'CGEventPostToPid', 'CGEventTapPostEvent',
                         'CGWarpMouseCursorPosition', 'CGAssociateMouseAndMouseCursorPosition',
                         'CGDisplayHideCursor', 'CGDisplayShowCursor'):
                if hasattr(namespace, name):
                    stack.enter_context(patch.object(namespace, name, forbidden))
        listener.start()
        worker = threading.Thread(target=driver, name='keyboard-probe-driver', daemon=True)
        worker.start()
        deadline = time.monotonic() + 120
        try:
            while not state.done.is_set():
                if time.monotonic() >= deadline:
                    state.errors.append('TimeoutError: Native keyboard probe exceeded its time limit')
                    break
                if state.pause_main.is_set():
                    state.main_paused.set()
                    time.sleep(.002)
                    continue
                state.main_paused.clear()
                F.NSRunLoop.mainRunLoop().runUntilDate_(F.NSDate.dateWithTimeIntervalSinceNow_(.01))
        finally:
            state.pause_main.clear()
            state.main_paused.clear()
            listener.stop()
            # Keep servicing deferred main work while the driver closes the tap.
            cleanup_deadline = time.monotonic() + 7
            while worker.is_alive() and time.monotonic() < cleanup_deadline:
                F.NSRunLoop.mainRunLoop().runUntilDate_(F.NSDate.dateWithTimeIntervalSinceNow_(.01))
            worker.join(.1)
            try:
                listener.join(1)
            except BaseException as exc:
                state.errors.append('Final listener ' + type(exc).__name__ + ': ' + str(exc))
            # Drain stopped main-queue decodes before restoring instrumentation.
            for _ in range(5):
                F.NSRunLoop.mainRunLoop().runUntilDate_(F.NSDate.dateWithTimeIntervalSinceNow_(.01))

    evidence.update(metrics=state.snapshot(), errors=state.errors,
                    listeners_alive=[listener.name] if listener.is_alive() else [],
                    driver_alive=worker.is_alive(),
                    listener_is_trusted=bool(listener.IS_TRUSTED),
                    listener_loop_cleared=getattr(listener, '_loop', None) is None,
                    native_main_thread_ident=main_ident, native_hid_thread_ident=listener.ident)
    if evidence['listeners_alive'] or evidence['driver_alive']:
        evidence['errors'].append('A native probe thread remains alive')
    if state.counts['forbidden_global_input_calls'] or state.counts['nsevent_wrong_thread']:
        evidence['errors'].append('A native safety boundary was violated')
    if len(evidence['phases']) != 7 and not evidence['errors']:
        evidence['errors'].append('Not all keyboard phases completed')
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--pairs', type=int, default=256)
    parser.add_argument('--output', type=Path, default=Path('/tmp/share-monkey-keyboard-probe.json'))
    args = parser.parse_args()
    if not 1 <= args.pairs <= 2048:
        parser.error('pairs must be between 1 and 2048')
    if args.native and sys.platform != 'darwin':
        parser.error('--native requires macOS')
    if args.native:
        try:
            evidence = native_probe(args)
        except Exception as exc:
            evidence = dict(mode='native-keyboard', errors=[type(exc).__name__ + ': ' + str(exc)],
                            records_key_values=False, posts_os_input=False)
    else:
        evidence = dict(mode='no-native-actions', native_actions=False, errors=[],
                        instruction='Use --native explicitly to run the native listen-only loop probe.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(mode=evidence['mode'], phases_completed=len(evidence.get('phases', [])),
                         errors=evidence.get('errors', []), skipped=evidence.get('skipped'),
                         output=str(args.output)), ensure_ascii=False))
    if evidence.get('errors'):
        raise SystemExit(1)
    if args.native and evidence.get('skipped'):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
