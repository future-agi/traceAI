"""Read-only, deliberately narrow admission of Apple's default PF hooks.

These functions consume stdout, never a stdout/stderr concatenation. In particular
ALTQ warnings belong to stderr; accepting arbitrary diagnostic lines in the rules
would turn an unsuccessful/ambiguous read into a successful admission.
"""
import re

SCRUB = 'scrub-anchor "com.apple/*" all fragment reassemble'
FILTER = 'anchor "com.apple/*" all'


def lines(text):
    # Only ordinary whitespace is ignorable; control characters are ambiguous.
    if any(ord(char) < 32 and char not in '\n\t\r' for char in text):
        raise ValueError('Control character in PF output')
    return [line.strip() for line in text.splitlines() if line.strip()]


def admit_root(text):
    effective = lines(text)
    if effective not in [[FILTER], [SCRUB, FILTER]]:
        raise ValueError('Expected one default filter hook with optional default normalization hook, and no other rule or hook')
    return {'filter_hook': FILTER, 'normalization_hook': SCRUB if SCRUB in effective else None}


def admit_recursive(text):
    # pfctl -a '*' -sr expands an empty wildcard anchor into an empty block.
    # No child rule, extra hook, quick predicate or stateful pass is admitted.
    effective = lines(text)
    if effective and effective[-2:] == [FILTER + ' {', '}']:
        effective = effective[:-2] + [FILTER]
    return admit_root('\n'.join(effective))


def admit_translation(text):
    effective = lines(text)
    expected = ['nat-anchor "com.apple/*" all', 'rdr-anchor "com.apple/*" all']
    flattened = []
    index = 0
    while index < len(effective):
        line = effective[index]
        if line.endswith(' {'):
            if index + 1 >= len(effective) or effective[index + 1] != '}':
                raise ValueError('Nonempty or ambiguous translation anchor')
            flattened.append(line[:-2])
            index += 2
        else:
            flattened.append(line)
            index += 1
    if flattened != expected:
        raise ValueError('Unexpected translation rule or hook')


def packet_counts(text):
    sections = re.findall(r'(?m)^@\d+ (block[^\n]+)\n(.*?)(?=^@\d+ |\Z)', text, re.DOTALL)
    counts = {}
    for rule, details in sections:
        label = re.search(r'\blabel "(traceai-inet6?-(?:tcp|udp))"', rule)
        packets = re.findall(r'Packets:\s*(\d+)', details)
        if not label or len(packets) != 1 or label.group(1) in counts:
            raise ValueError('Ambiguous UID rule counters')
        counts[label.group(1)] = int(packets[0])
    expected = {f'traceai-{family}-{protocol}' for family in ['inet', 'inet6'] for protocol in ['tcp', 'udp']}
    if set(counts) != expected:
        raise ValueError('Expected one labeled packet counter for each of four UID rules')
    return counts
