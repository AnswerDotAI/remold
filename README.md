# remold

Concisely reshape Python code with [LibCST](https://github.com/Instagram/LibCST) or [ast-grep](https://ast-grep.github.io/).

```bash
pip install remold
```

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

## Development

```bash
pip install -e .[dev]
pytest -q
```

Version lives in `remold/__init__.py` as `__version__`; bump with `ship-bump`. Release with `ship-gh` and `ship-pypi`.
