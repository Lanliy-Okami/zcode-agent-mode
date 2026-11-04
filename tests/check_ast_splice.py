# -*- coding: utf-8 -*-
"""Check: does ast.parse accept top-level return/await, and does the DSH splice technique preserve triple-quoted strings?"""
import ast
import asyncio

# 1. Top-level return / await through ast.parse + AsyncFunctionDef splice.
for snippet in ("return 42", "value = 1\nreturn value", "await tools.read({})", "x = await f()\nreturn x"):
    try:
        module = ast.parse(snippet, filename="<model>")
        wrapper = ast.AsyncFunctionDef(
            name="__dsh_main__",
            args=ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]),
            body=module.body or [ast.Pass()],
            decorator_list=[],
            returns=None,
        )
        anchor = module.body[0] if module.body else ast.parse("pass").body[0]
        ast.copy_location(wrapper, anchor)
        wrapped = ast.Module(body=[wrapper], type_ignores=[])
        ast.fix_missing_locations(wrapped)
        code = compile(wrapped, "<model>", "exec", dont_inherit=True)
        print(f"PARSE OK: {snippet!r}")
    except SyntaxError as error:
        print(f"SyntaxError: {snippet!r} -> {error.msg}")

# 2. Triple-quoted string preservation through AST splice vs textual indent.
body = 'content = """line1\nline2"""\nreturn content'
module = ast.parse(body)
wrapper = ast.AsyncFunctionDef(name="f", args=ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]), body=module.body, decorator_list=[], returns=None)
ast.copy_location(wrapper, module.body[0])
wrapped = ast.Module(body=[wrapper], type_ignores=[])
ast.fix_missing_locations(wrapped)
ns = {"__builtins__": __builtins__}
exec(compile(wrapped, "<model>", "exec", dont_inherit=True), ns)
value = asyncio.run(ns["f"]())
print("AST splice preserves string:", value == "line1\nline2", repr(value))

textual = "async def g():\n" + "".join(f"    {line}\n" for line in body.splitlines())
ns2 = {"__builtins__": __builtins__}
exec(compile(textual, "<x>", "exec"), ns2)
value2 = asyncio.run(ns2["g"]())
print("textual indent corrupts string:", value2 != "line1\nline2", repr(value2))
