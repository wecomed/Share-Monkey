"""Stress real native callback ownership; never register a tap or post input.

Use --native explicitly. This tests native CGEvent objects through the actual
production C callback, including replacement ownership. It cannot reproduce
every real hardware event payload or certify multi-day dual-machine usage.
"""
import argparse
import ctypes
import gc
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def probe(count):
    import objc
    import Quartz as Q
    from sharemouse.mac_event_tap import EventTap, _CF

    class Stats(ctypes.Structure):
        _fields_ = [('blocks', ctypes.c_uint), ('used', ctypes.c_size_t),
                    ('peak', ctypes.c_size_t), ('allocated', ctypes.c_size_t)]
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    lib.malloc_zone_statistics.argtypes = [ctypes.c_void_p, ctypes.POINTER(Stats)]
    lib.malloc_zone_statistics.restype = None
    _CF.CFGetRetainCount.argtypes = [ctypes.c_void_p]
    _CF.CFGetRetainCount.restype = ctypes.c_long

    def stats():
        gc.collect()
        value = Stats()
        lib.malloc_zone_statistics(None, ctypes.byref(value))
        return dict(malloc_used=value.used, malloc_blocks=value.blocks)

    errors = []
    behavior = 'pass'
    def handler(proxy, kind, event, refcon):
        if behavior == 'drop':
            return None
        if behavior == 'replace':
            return Q.CGEventCreateCopy(event)
        return event
    tap = EventTap(handler, lambda: errors.append(str(sys.exc_info()[1])))
    event = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (100, 100), 0)
    pointer = objc.pyobjc_id(event)
    initial = dict(python=sys.getrefcount(event), native=_CF.CFGetRetainCount(pointer))
    samples = []
    started = time.monotonic()
    try:
        for index in range(count):
            if index % max(1, count // 10) == 0:
                samples.append(dict(calls=index, **stats()))
            # Pass and suppressed events exercise both local and remote modes.
            behavior = 'drop' if index % 4 == 0 else 'pass'
            result = tap.callback(None, Q.kCGEventMouseMoved, pointer, None)
            if result != (None if behavior == 'drop' else pointer):
                raise AssertionError('Callback changed input behavior')
        final = dict(python=sys.getrefcount(event), native=_CF.CFGetRetainCount(pointer))
        if final != initial:
            raise AssertionError('Event references grew: ' + str((initial, final)))
        for index in range(100000):
            # Distinct events expose leaks hidden by repeated use of one proxy.
            with objc.autorelease_pool():
                item = Q.CGEventCreateCopy(event)
                behavior = 'pass'
                raw = objc.pyobjc_id(item)
                if tap.callback(None, 5, raw, None) != raw:
                    raise AssertionError('Distinct-event pass-through failed')
                del item
            if index % 10000 == 0:
                samples.append(dict(distinct_events=index, **stats()))
        behavior = 'replace'
        for _ in range(10000):
            replacement = tap.callback(None, 5, pointer, None)
            if not replacement or replacement == pointer:
                raise AssertionError('Replacement event missing')
            _CF.CFRelease(replacement)  # CoreGraphics owns this returned reference.
        samples.append(dict(calls=count, distinct_events=100000,
                            replacements=10000, **stats()))
    finally:
        tap.close()
    if errors:
        raise AssertionError(errors)
    # Allow initialization/allocator variation, but reject a linear native leak.
    growth = samples[-1]['malloc_used'] - samples[1]['malloc_used']
    if growth > 2 * 1024 * 1024:
        raise AssertionError('Native memory continued growing: ' + str(growth))
    return dict(passed=True, calls=count, distinct_events=100000, replacements=10000,
                elapsed_seconds=time.monotonic() - started,
                initial_references=initial, final_references=final,
                malloc_growth_after_warmup=growth, samples=samples,
                posts_os_input=False, registers_tap=False, reads_real_keys=False,
                hardware_endurance_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--count', type=int, default=1000000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.native or sys.platform != 'darwin':
        parser.error('Run explicitly with --native on macOS')
    if args.count < 10000:
        parser.error('--count must be at least 10000')
    result = probe(args.count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
