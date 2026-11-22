"""Strict reader for the quoted Joern DOT export subset in this corpus.

This is not a general DOT parser. It accepts one unnamed directed graph,
quoted decimal node IDs, quoted attribute values, and node/edge statements.
Values may contain escaped characters and literal newlines. Attribute strings
retain their quotes and escapes except for backslash line continuations,
which pydot 4.0.1 removes. This matches builder.load_cpg().

Unsupported syntax raises UnsupportedJoernDot. Use the pydot loader for
broader DOT syntax. No line or statement is silently skipped.
"""

from __future__ import annotations

import html
import re
import networkx as nx


_HEADER = re.compile(r"[ \t\r\n]*digraph[ \t\r\n]*\{")
_SPACE = re.compile(r"[ \t\r\n]*")
_ID = re.compile(r'"([0-9]+)"')
_ATTRIBUTE = re.compile(r'([A-Za-z_][A-Za-z_0-9]*)[ \t\r\n]*=[ \t\r\n]*("(?:[^"\\]|\\[\s\S])*")')


class UnsupportedJoernDot(ValueError):
    """The input is outside the strict reader's supported grammar."""


def parse_joern_dot(text: str, *, keep_edge_labels: frozenset[str] | None = None) -> nx.MultiDiGraph:
    """Read the full graph, or an explicit experimental edge projection.

    A projection keeps every node and every node attribute. It validates all
    statements, including discarded edges. It is not a complete CPG.
    """
    graph = nx.MultiDiGraph()
    first_pair_order = {}
    if keep_edge_labels is not None:
        graph.graph['edge_projection'] = {
            'keep_edge_labels': sorted(keep_edge_labels),
            'input_edges': 0, 'discarded_edges': 0, 'input_edge_labels': {},
        }
    header = _HEADER.match(text)

    def fail(message, offset):
        line = text.count("\n", 0, offset) + 1
        raise UnsupportedJoernDot(f"Line {line}: {message}; offset={offset}")

    if header is None:
        fail("expected an unnamed 'digraph {' header", 0)
    position = header.end()
    edge_section = False

    while True:
        position = _SPACE.match(text, position).end()
        if position >= len(text):
            fail("missing closing graph brace", position)
        if text[position] == "}":
            position = _SPACE.match(text, position + 1).end()
            if position != len(text):
                fail("content after the closing graph brace", position)
            if keep_edge_labels is not None:
                # NetworkX orders neighbors by the first edge of ANY label.
                # A discarded edge can therefore set the order of a later
                # retained edge. Restore both adjacency orders before callers
                # can observe the graph. Reuse each existing adjacency mapping
                # and edge-data mapping; only dictionary insertion order moves.
                # This experiment pins and tests NetworkX 3.6.1 internals.
                for node in graph:
                    successors = graph._succ[node]
                    for neighbor in sorted(successors, key=lambda n: first_pair_order[node][n]):
                        successors[neighbor] = successors.pop(neighbor)
                    predecessors = graph._pred[node]
                    for neighbor in sorted(predecessors, key=lambda n: first_pair_order[n][node]):
                        predecessors[neighbor] = predecessors.pop(neighbor)
            return graph

        origin = position
        source_match = _ID.match(text, position)
        if source_match is None:
            fail("expected a quoted decimal node ID", position)
        source = source_match.group(1)
        position = _SPACE.match(text, source_match.end()).end()
        target = None
        if text.startswith("->", position):
            edge_section = True
            position = _SPACE.match(text, position + 2).end()
            target_match = _ID.match(text, position)
            if target_match is None:
                fail("expected a quoted decimal target ID", position)
            target = target_match.group(1)
            position = _SPACE.match(text, target_match.end()).end()
        elif edge_section:
            fail("node declaration after the first edge is unsupported", origin)

        if not text.startswith("[", position):
            fail("expected an attribute list", position)
        position = _SPACE.match(text, position + 1).end()
        attributes = {}
        while position < len(text) and text[position] != "]":
            attribute = _ATTRIBUTE.match(text, position)
            if attribute is None:
                fail("expected a name and a quoted attribute value", position)
            key, value = attribute.groups()
            if key in attributes:
                fail(f"duplicate attribute {key!r}", position)
            # Match pydot.dot_parser.push_dbl_quoted exactly. Ordinary escaped
            # characters remain quoted; only physical line continuations go.
            if "\\" in value:
                value = value.replace("\\\r\n", "").replace("\\\n", "")
            attributes[key] = value
            following = _SPACE.match(text, attribute.end()).end()
            if following == attribute.end() and not text.startswith("]", following):
                fail("attributes must be separated by whitespace", following)
            position = following

        if not text.startswith("]", position):
            fail("missing closing attribute bracket", position)
        position = _SPACE.match(text, position + 1).end()
        if not text.startswith(";", position):
            fail("expected a statement semicolon", position)
        position += 1

        if target is None:
            if source in graph:
                fail("duplicate node declaration is unsupported", origin)
            graph.add_node(source, **attributes)
        else:
            if source not in graph or target not in graph:
                fail("implicit nodes are unsupported", origin)
            if keep_edge_labels is not None:
                label = html.unescape(attributes.get('label', '').strip().strip('"')).strip().upper()
                stats = graph.graph['edge_projection']
                stats['input_edges'] += 1
                first_pair_order.setdefault(source, {}).setdefault(target, stats['input_edges'])
                stats['input_edge_labels'][label] = stats['input_edge_labels'].get(label, 0) + 1
                if label not in keep_edge_labels:
                    stats['discarded_edges'] += 1
                    continue
            graph.add_edge(source, target, **attributes)
