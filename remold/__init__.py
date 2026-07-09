"""Reshape Python source with LibCST: match structure in the tree, write the new code as plain text, and comments and whitespace come along as ordinary characters."""
__version__ = "0.1.1"



import re
from dataclasses import fields

import libcst as cst
import libcst.matchers as m
from ast_grep_py import SgRoot

__all__ = ['cst', 'm', 'astmap', 'cstmap', 'code', 'whereis']

def _expand(tmpl, mch):
    "Fill `$VAR` holes in `tmpl` from ast-grep match `mch`"
    def _sub(mo):
        node = mch.get_match(mo.group(1))
        if node is None: raise KeyError(f'${mo.group(1)} not captured by the pattern')
        return node.text()
    return re.sub(r'\$([A-Z_][A-Z0-9_]*)', _sub, tmpl)

def astmap(
    *rules # (pattern, repl) pairs; repl is a `$VAR` template or a callable(match)->str
):
    "A `str->str` source transform: apply ast-grep pattern rules in order, reparsing between rules"
    def _f(src):
        for pat,repl in rules:
            root = SgRoot(src, 'python').root()
            edits = [mch.replace(repl(mch) if callable(repl) else _expand(repl, mch))
                     for mch in root.find_all(pattern=pat)]
            if edits: src = root.commit_edits(edits)
        return src
    return _f

_render = cst.Module([]).code_for_node

def code(node):
    "Source text for `node`, trivia included"
    return _render(node)

def _reparse(node, s):
    "Parse `s` as whatever fits where `node` sits"
    if isinstance(node, (cst.SimpleStatementLine, cst.BaseCompoundStatement)):
        mod = cst.parse_module(s)
        stmts = list(mod.body)
        if any(l.comment for l in mod.footer):
            raise ValueError(f'{s!r} ends with a comment after its last statement, which no statement node can carry; put it before or inline')
        if not stmts: return cst.RemovalSentinel.REMOVE
        if mod.header: stmts[0] = stmts[0].with_changes(leading_lines=[*mod.header, *stmts[0].leading_lines])
        return stmts[0] if len(stmts)==1 else cst.FlattenSentinel(stmts)
    if isinstance(node, cst.BaseSmallStatement):
        stmts = cst.parse_module(s).body
        if len(stmts)==1 and isinstance(stmts[0], cst.SimpleStatementLine) and len(stmts[0].body)==1: return stmts[0].body[0]
        raise ValueError(f'{s!r} is not a single small statement, which is what {type(node).__name__} sits as')
    if isinstance(node, cst.BaseExpression): return cst.parse_expression(s)
    raise TypeError(f"Can't reparse a str in place of {type(node).__name__}; return a CST node instead")

def _trailing(n):
    "The `TrailingWhitespace` of statement `n`, or None"
    if isinstance(n, cst.SimpleStatementLine): return n.trailing_whitespace
    if isinstance(n, cst.BaseCompoundStatement):
        if isinstance(n.body, cst.SimpleStatementSuite): return n.body.trailing_whitespace
        if isinstance(n.body, cst.IndentedBlock): return n.body.header
    return None

def _set_trailing(n, tw):
    if isinstance(n, cst.SimpleStatementLine): return n.with_changes(trailing_whitespace=tw)
    if isinstance(n, cst.BaseCompoundStatement):
        if isinstance(n.body, cst.SimpleStatementSuite): return n.with_changes(body=n.body.with_changes(trailing_whitespace=tw))
        if isinstance(n.body, cst.IndentedBlock): return n.with_changes(body=n.body.with_changes(header=tw))
    return n

def _keep_trivia(old, new):
    "Carry `old`'s leading lines and trailing comment onto `new`"
    nodes = list(new.nodes) if isinstance(new, cst.FlattenSentinel) else [new]
    if hasattr(old, 'leading_lines') and hasattr(nodes[0], 'leading_lines'):
        nodes[0] = nodes[0].with_changes(leading_lines=old.leading_lines)
    tw = _trailing(old)
    if tw is not None and tw.comment is not None: nodes[-1] = _set_trailing(nodes[-1], tw)
    return cst.FlattenSentinel(nodes) if isinstance(new, cst.FlattenSentinel) else nodes[0]

class _Mapper(cst.CSTTransformer):
    def __init__(self, matcher, fn, trivia):
        super().__init__()
        self.matcher,self.fn,self.trivia = matcher,fn,trivia

    def on_leave(self, orig, updated):
        caps = m.extract(updated, self.matcher)
        if caps is None: return updated
        res = self.fn(updated, caps)
        if res is None: return updated
        if isinstance(res, str): res = _reparse(updated, res)
        if self.trivia=='keep' and not isinstance(res, cst.RemovalSentinel): res = _keep_trivia(updated, res)
        return res

def cstmap(
    matcher, # A `libcst.matchers` matcher; `m.SaveMatchedNode` captures arrive in `fn`'s second arg
    fn, # `fn(node, caps)` returning None (leave unchanged), a str (reparsed in context), or a CST node
    trivia:str='keep' # 'keep' carries the matched node's leading lines and trailing comment; 'given' means fn's output is everything
):
    "A `str->str` source transform: replace every node matching `matcher` with `fn`'s result"
    def _f(src): return cst.parse_module(src).visit(_Mapper(matcher, fn, trivia)).code
    return _f

def _walk(node, path=''):
    yield path, node
    for f in fields(node):
        v = getattr(node, f.name)
        if isinstance(v, cst.CSTNode): yield from _walk(v, f'{path}.{f.name}')
        elif isinstance(v, (tuple, list)):
            for i,x in enumerate(v):
                if isinstance(x, cst.CSTNode): yield from _walk(x, f'{path}.{f.name}[{i}]')

def whereis(
    src:str, # Source snippet to interrogate
    *frags:str, # Fragments to locate, e.g. 'foo', '# ho'
    contains:bool=False # Match nodes containing the fragment, not just equal to it
) -> dict:
    "For each fragment, the attribute paths where LibCST keeps it, deepest node first"
    mod = cst.parse_module(src)
    def _hit(n, frag):
        try: c = _render(n).strip()
        except Exception: return False
        return frag in c if contains else c==frag
    return {frag: [p for p,n in sorted(((p,n) for p,n in _walk(mod) if p and _hit(n, frag)), key=lambda o: -len(o[0]))]
            for frag in frags}
