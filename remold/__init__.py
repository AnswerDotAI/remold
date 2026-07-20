r'''Structural search and rewrite for Python source: declarative ast-grep pattern rules, LibCST matcher transforms for everything patterns can't express, and tree-based symbol queries. Use this to edit code by structure (rename calls, move methods, rewrite APIs) where regexes break and `ast` loses comments.

Tools like `ast` throw comments away, and regex rewrites break on real code. remold keeps the source intact and gives you two ways to build composable `str -> str` transforms:

- `astmap(*rules)` applies ast-grep pattern rules. Use it when you can write the rewrite as a before pattern and an after template. Only the matched span is replaced, so comments elsewhere are untouched.
- `cstmap(matcher, fn, trivia='keep')` takes a [LibCST matcher](https://libcst.readthedocs.io/en/latest/matchers.html) and a function, for everything patterns can't express, including transforms that only change comments and whitespace. `fn(node, caps)` returns `None` (leave it), a string (reparsed in place, so the output is guaranteed to parse), or a CST node (full surgery).

The idea behind `cstmap` is to match structure in the tree, where matching is easy, and to write the new code as a plain string, where comments and whitespace are just characters. Two helpers make that workable. `code(node)` renders any node back to source text, trivia included. `whereis(src, *frags)` tells you where LibCST keeps things, so you don't have to search the node docs.

`from remold import *` gives you all four plus `cst` (libcst) and `m` (libcst.matchers).

## Pattern rules

Turn `test_fail(lambda: f(x), contains='boom')` into `with expect_fail(Exception, 'boom'): f(x)`:

```python
from remold import *

fix_tests = astmap(
    ("test_fail(lambda: $BODY, contains=$MSG)", "with expect_fail(Exception, $MSG): $BODY"),
    ("test_fail(lambda: $BODY)",                "with expect_fail(Exception): $BODY"))

print(fix_tests("test_fail(lambda: f(x), contains='boom')  # tricky case\n"))
# with expect_fail(Exception, 'boom'): f(x)  # tricky case
```

Rules are applied in order, with a reparse between each, so later rules see the output of earlier ones. A replacement can also be a `callable(match) -> str` when the new text needs computing. Code that matches no pattern (like `test_fail(divide, args=(1,0))`) is left alone, and an unknown `$VAR` in a template raises a `KeyError`.

## The same transform with matchers

For rewrites patterns can't express, write a LibCST matcher and a function. Here is the same transform in that form:

```python
tf = m.SimpleStatementLine(body=[m.Expr(m.Call(
    func=m.Name('test_fail'),
    args=[m.Arg(m.Lambda(body=m.SaveMatchedNode(m.DoNotCare(), 'body'))),
          m.Arg(keyword=m.Name('contains'), value=m.SaveMatchedNode(m.DoNotCare(), 'msg'))]))])

def fix(node, caps): return f"with expect_fail(Exception, {code(caps['msg'])}): {code(caps['body'])}"

fix_tests = cstmap(tf, fix)
print(fix_tests("test_fail(lambda: f(x), contains='boom')  # tricky case\n"))
# with expect_fail(Exception, 'boom'): f(x)  # tricky case
```

The trailing comment is kept because `trivia='keep'` (the default) copies the matched statement's leading lines and trailing comment onto whatever `fn` returns. The matcher also acts as a guard. A `test_fail(f, args=(1,0))` call doesn't match the arg spec, so it isn't changed.

## Example: move a comment

With `trivia='given'`, the string you return is used exactly as given, so to move a comment you write it where you want it:

```python
def fix(node, caps):
    c = node.trailing_whitespace.comment
    new = f"with expect_fail(Exception, {code(caps['msg'])}): {code(caps['body'])}"
    return f"{code(c)}\n{new}" if c else new

print(cstmap(tf, fix, trivia='given')("test_fail(lambda: f(), contains='x')  # ho\n"))
# # ho
# with expect_fail(Exception, 'x'): f()
```

A multi-line string becomes multiple statements, and returning `''` deletes the statement.

## Example: reformat a signature

Here the code itself doesn't change; only the comments and line layout do. The pieces are read from the tree, and the new layout is built with ordinary string operations:

```python
def fix(fd, _):
    ps = [code(p).strip() for p in fd.params.params]      # each param arrives with its comment attached
    c = fd.body.header.comment                            # a comment after ':' lives on the body header
    if c: ps[-1] = f"{ps[-1]} {code(c)}"
    return f"def {fd.name.value}(\n    " + "\n    ".join(ps) + "\n):" + code(fd.body.with_changes(header=cst.TrailingWhitespace()))

src = """def f(foo, # hey
      bam, # gg
      bar): # ho
    pass
"""
print(cstmap(m.FunctionDef(), fix, trivia='given')(src))
# def f(
#     foo, # hey
#     bam, # gg
#     bar # ho
# ):
#     pass
```

## Moving code across depths

Indentation in LibCST is contextual, not literal. Statement lines, continuation lines inside brackets, and comment lines all record "indent goes here" rather than a column number, and the indent size comes from the target module's `default_indent`. Since `cstmap` parses your returned string as its own little module and splices the resulting statements into the real one, code written at one depth renders correctly at whatever depth it lands, in the target file's indent style. The one thing never re-indented is the content of multiline strings, since changing that would change the program.

This makes depth-changing transforms work without any bookkeeping. Here a method is pulled out of its class and turned into a top-level fastcore `@patch` function. Note the trick: matching the `FunctionDef` would replace the method in place, still inside the class, so to *move* code you match the enclosing `ClassDef` and return the whole new arrangement. A multi-line replacement becomes several sibling statements.

```python
def fix(cd, _):
    fs = [s for s in cd.body.body if m.matches(s, m.FunctionDef(name=m.Name('f')))]
    if not fs: return None
    fd = fs[0]
    rest = cd.with_changes(body=cd.body.with_changes(body=[s for s in cd.body.body if s is not fd]))
    p1 = code(fd.params.params[0]).strip().rstrip(',')
    ps = ', '.join([f'{p1}:{cd.name.value}'] + [code(p).strip().rstrip(',') for p in fd.params.params[1:]])
    return f"{code(rest)}\n@patch\ndef {fd.name.value}({ps}):{code(fd.body)}"

src = """class A:
    def __init__(self): self.x = 1

    def f(self, n): # add `n` to `x`
        y = self.x+n
        return y
"""
print(cstmap(m.ClassDef(), fix, trivia='given')(src))
# class A:
#     def __init__(self): self.x = 1
#
# @patch
# def f(self:A, n): # add `n` to `x`
#     y = self.x+n
#     return y
```

The method body was written at depth 2 and lands at depth 1; `code(fd.body)` carried it along with its comment, and the render re-indented it. The `self` parameter picks up the class as its type annotation, which is how `@patch` knows what to patch.

## whereis

How did that example know a comment after the colon is `fd.body.header.comment`? Ask:

```python
whereis(src, 'foo', '# ho', '# hey')
# {'foo':   ['.body[0].params.params[0].name'],
#  '# ho':  ['.body[0].body.header.comment', ...],
#  '# hey': ['.body[0].params.params[0].comma.whitespace_after.first_line.comment', ...]}
```

Paste a representative snippet, ask for the fragments you care about, and copy the paths into your `fn`. Multiple entries per fragment list the node itself first, then its containers. `contains=True` matches fragments inside larger nodes.

## Finding things

The same structural view works for read-only queries. `astfind(src, pattern)` returns the text of each ast-grep pattern match. `symdefs(src)` returns the names bound in the source's top-level scope, whatever syntax binds them: tuple unpacking, `for`, `with ... as`, walrus, imports, `def`/`class`. `symrefs(src)` returns the names read anywhere in it. Both return the empty set for source that doesn't parse, so callers can map them over mixed content without guarding.

```python
symdefs("cfg, aux = load_cfg()")   # {'cfg', 'aux'}
symrefs("print(render(cfg))")      # {'print', 'render', 'cfg'}
astfind("save(x, ts=1)\nsave(y)\n", "save($$$, ts=$_)")   # ['save(x, ts=1)']
```

These make short names searchable: a text search for where `c1` is defined needs a regex per binding syntax and still floods on two-letter names, while `symdefs` answers from the tree.
'''
__version__ = "0.1.1"



import ast, re
from dataclasses import fields

import libcst as cst
import libcst.matchers as m
from ast_grep_py import SgRoot

__all__ = ['cst', 'm', 'astmap', 'cstmap', 'code', 'whereis', 'astfind', 'symdefs', 'symrefs']

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

def astfind(
    src, # Python source to search
    pattern, # ast-grep pattern, e.g. "save($$$, ts=$_)"
):
    "Source text of each ast-grep `pattern` match in `src`"
    return [mch.text() for mch in SgRoot(src, 'python').root().find_all(pattern=pattern)]

def _target_names(t):
    "Names bound by assignment target `t`"
    if isinstance(t, ast.Name): yield t.id
    elif isinstance(t, ast.Starred): yield from _target_names(t.value)
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts: yield from _target_names(e)

def _bound(n):
    "Names `n` binds in the enclosing scope, recursing into same-scope children only"
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield n.name
        return
    if isinstance(n, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)): return
    if isinstance(n, ast.Assign): yield from (x for t in n.targets for x in _target_names(t))
    elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)): yield from _target_names(n.target)
    elif isinstance(n, (ast.For, ast.AsyncFor)): yield from _target_names(n.target)
    elif isinstance(n, (ast.Import, ast.ImportFrom)): yield from ((a.asname or a.name.split('.')[0]) for a in n.names)
    elif isinstance(n, (ast.With, ast.AsyncWith)): yield from (x for it in n.items if it.optional_vars for x in _target_names(it.optional_vars))
    elif isinstance(n, ast.ExceptHandler) and n.name: yield n.name
    elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name: yield n.name
    elif isinstance(n, ast.MatchMapping) and n.rest: yield n.rest
    for c in ast.iter_child_nodes(n): yield from _bound(c)

def symdefs(
    src, # Python source
):
    "Names bound in `src`'s top-level scope; empty set if it does not parse"
    try: tree = ast.parse(src)
    except SyntaxError: return set()
    return {x for n in tree.body for x in _bound(n)}

def symrefs(
    src, # Python source
):
    "Names referenced anywhere in `src`; empty set if it does not parse"
    try: tree = ast.parse(src)
    except SyntaxError: return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
