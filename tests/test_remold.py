import pytest
from remold import cst, m, astmap, cstmap, code, whereis

tf = m.SimpleStatementLine(body=[m.Expr(m.Call(
    func=m.Name('test_fail'),
    args=[m.Arg(m.Lambda(body=m.SaveMatchedNode(m.DoNotCare(), 'body'))),
          m.Arg(keyword=m.Name('contains'), value=m.SaveMatchedNode(m.DoNotCare(), 'msg'))]))])

def _fix(node, caps): return f"with expect_fail(Exception, {code(caps['msg'])}): {code(caps['body'])}"


def test_astmap():
    fix = astmap(("test_fail(lambda: $BODY, contains=$MSG)", "with expect_fail(Exception, $MSG): $BODY"),
                 ("test_fail(lambda: $BODY)", "with expect_fail(Exception): $BODY"))
    src = "x = 1\ntest_fail(lambda: f(x), contains='boom')  # why\ntest_fail(lambda: g())\n"
    assert fix(src) == "x = 1\nwith expect_fail(Exception, 'boom'): f(x)  # why\nwith expect_fail(Exception): g()\n"

    # non-matching forms pass through, later rules see earlier rules' output
    assert fix("test_fail(divide, args=(1,0))\n") == "test_fail(divide, args=(1,0))\n"
    two = astmap(("a()", "b()"), ("b()", "c()"))
    assert two("a()\n") == "c()\n"

    # a callable replacement gets the raw ast-grep match
    up = astmap(("print($X)", lambda mch: f"log({mch['X'].text().upper()})"))
    assert up("print(msg)\n") == "log(MSG)\n"

    # unknown metavariables in the template fail loudly
    with pytest.raises(KeyError): astmap(("print($X)", "log($Y)"))("print(msg)\n")


def test_cstmap():
    src = "x = 1\ntest_fail(lambda: f(x), contains='boom')  # why\ny = 2\n"
    assert cstmap(tf, _fix)(src) == "x = 1\nwith expect_fail(Exception, 'boom'): f(x)  # why\ny = 2\n"

    # leading comments and blank lines survive too
    src = "# setup\n\ntest_fail(lambda: g(), contains='no')\n"
    assert cstmap(tf, _fix)(src) == "# setup\n\nwith expect_fail(Exception, 'no'): g()\n"

    # works at any indentation depth
    src = "def t():\n    test_fail(lambda: f(1), contains='boom')  # why\n"
    assert cstmap(tf, _fix)(src) == "def t():\n    with expect_fail(Exception, 'boom'): f(1)  # why\n"

    # expression context: replacement string parsed as an expression
    up = cstmap(m.Name('a'), lambda n,c: 'a2')
    assert up("b = a + a  # keep\n") == "b = a2 + a2  # keep\n"

    # returning None leaves the node alone; non-matching text untouched
    assert cstmap(tf, lambda n,c: None)("test_fail(lambda: f(), contains='x')\n") == "test_fail(lambda: f(), contains='x')\n"
    assert cstmap(tf, _fix)("test_fail(divide, args=(1,0))\n") == "test_fail(divide, args=(1,0))\n"

    # returning a node is full-surgery mode
    ren = cstmap(m.Name('old'), lambda n,c: n.with_changes(value='new'))
    assert ren("old = old + 1\n") == "new = new + 1\n"


def test_trivia():
    src = "test_fail(lambda: f(), contains='x')  # ho\n"
    # trivia='given': the returned string is the whole truth, so the comment moves where fn puts it
    def fix(node, caps):
        c = node.trailing_whitespace.comment
        new = f"with expect_fail(Exception, {code(caps['msg'])}): {code(caps['body'])}"
        return f"{code(c)}\n{new}" if c else new
    assert cstmap(tf, fix, trivia='given')(src) == "# ho\nwith expect_fail(Exception, 'x'): f()\n"

    # the moved comment indents contextually when the statement is nested
    src = "def t():\n    test_fail(lambda: f(), contains='x')  # ho\n"
    assert cstmap(tf, fix, trivia='given')(src) == "def t():\n    # ho\n    with expect_fail(Exception, 'x'): f()\n"

    # multi-statement strings expand in place
    dup = cstmap(m.SimpleStatementLine(body=[m.Expr(m.Call(func=m.Name('once')))]), lambda n,c: "first()\nsecond()")
    assert dup("a = 1\nonce()\n") == "a = 1\nfirst()\nsecond()\n"

    # empty string removes the statement
    rm = cstmap(m.SimpleStatementLine(body=[m.Expr(m.Call(func=m.Name('gone')))]), lambda n,c: '')
    assert rm("gone()\nkeep()\n") == "keep()\n"

    # unparseable replacement fails loudly
    bad = cstmap(tf, lambda n,c: 'def broken(')
    with pytest.raises(cst.ParserSyntaxError): bad("test_fail(lambda: f(), contains='x')\n")


def test_relayout():
    # the README signature example: code unchanged, comments and line structure are the payload
    def fix(fd, _):
        ps = [code(p).strip() for p in fd.params.params]
        c = fd.body.header.comment
        if c: ps[-1] = f"{ps[-1]} {code(c)}"
        return f"def {fd.name.value}(\n    " + "\n    ".join(ps) + "\n):" + code(fd.body.with_changes(header=cst.TrailingWhitespace()))

    src = "def f(foo, # hey\n      bam, # gg\n      bar): # ho\n    pass\n"
    assert cstmap(m.FunctionDef(), fix, trivia='given')(src) == (
        "def f(\n    foo, # hey\n    bam, # gg\n    bar # ho\n):\n    pass\n")

    # continuation-line whitespace is contextual in LibCST, so nested defs re-indent correctly
    src = "class A:\n    def f(foo, # hey\n          bam,\n          bar): # ho\n        pass\n"
    assert cstmap(m.FunctionDef(), fix, trivia='given')(src) == (
        "class A:\n    def f(\n        foo, # hey\n        bam,\n        bar # ho\n    ):\n        pass\n")


def test_move_across_depths():
    # extract a method to a top-level fastcore-style @patch function: match the *enclosing* ClassDef
    # (matching the method would replace it in place), return the new arrangement as one string
    def fix(cd, _):
        fs = [s for s in cd.body.body if m.matches(s, m.FunctionDef(name=m.Name('f')))]
        if not fs: return None
        fd = fs[0]
        rest = cd.with_changes(body=cd.body.with_changes(body=[s for s in cd.body.body if s is not fd]))
        p1 = code(fd.params.params[0]).strip().rstrip(',')
        ps = ', '.join([f'{p1}:{cd.name.value}'] + [code(p).strip().rstrip(',') for p in fd.params.params[1:]])
        return f"{code(rest)}\n@patch\ndef {fd.name.value}({ps}):{code(fd.body)}"

    src = ("class A:\n    def __init__(self): self.x = 1\n\n"
           "    def f(self, n): # add `n` to `x`\n        y = self.x+n\n        return y\n")
    assert cstmap(m.ClassDef(), fix, trivia='given')(src) == (
        "class A:\n    def __init__(self): self.x = 1\n\n"
        "@patch\ndef f(self:A, n): # add `n` to `x`\n    y = self.x+n\n    return y\n")


def test_whereis():
    src = "def f(foo, # hey\n      bam, # gg\n      bar): # ho\n    pass\n"
    w = whereis(src, 'foo', '# ho', '# hey')
    assert w['foo'] == ['.body[0].params.params[0].name']
    assert w['# ho'][0] == '.body[0].body.header.comment'
    assert w['# hey'][0] == '.body[0].params.params[0].comma.whitespace_after.first_line.comment'
    # contains=True finds fragments inside larger nodes
    w = whereis(src, 'hey', contains=True)
    assert '.body[0].params.params[0].comma.whitespace_after.first_line.comment' in w['hey']


def test_code():
    node = cst.parse_expression('f(x,  y)')
    assert code(node) == 'f(x,  y)'
    p = cst.parse_module("def f(foo, # hey\n      bar): pass\n").body[0].params.params[0]
    assert code(p) == 'foo, # hey\n      '
