r"""Render Tauri's NSIS installer template (handlebars) the way tauri-bundler 2.9.4 does. Test fixture only.

Only the handlebars subset that installer.nsi uses is implemented:
- `{{name}}`, `{{this}}`, `{{this.[1]}}`, `{{@key}}`, `{{alias}}`, `{{no-escape x}}`;
- `{{#if x}}...{{/if}}`;
- `{{#each x}}` / `{{#each x as |alias| ~}}...{{/each}}` over lists and dicts;
- standalone block lines (the whole line, with its indentation and newline, disappears);
- `~` whitespace control;
- `\\{{` (one literal backslash, then the expression) and `\{{` (a literal mustache).
Values that Tauri's escape function would change ($, ", `, CR, LF, TAB) are REFUSED rather than guessed; fixture
values never need them. Unknown helpers (the file-association ones) are refused too.

Fidelity: tests/test_installer_template.py renders the UPSTREAM template with the context reconstructed from a
real Tauri-generated installer.nsi (work/ib1, the 10-08 packaging build) and requires byte equality after
BOM/CRLF normalisation.
"""
from __future__ import annotations

import re
from typing import Any

_TAG = re.compile(r"\{\{(~?)([#/]?)\s*(.*?)\s*(~?)\}\}", re.S)
_UNSAFE = re.compile(r'[$"`\r\n\t]')


class TemplateError(ValueError):
    pass


def _tokens(source: str) -> list[tuple[str, Any]]:
    """[("text", str) | ("tag", (kind, expr, strip_before, strip_after, standalone))]."""
    out: list[tuple[str, Any]] = []
    position = 0
    for match in _TAG.finditer(source):
        start, end = match.span()
        if start >= 1 and source[start - 1] == "\\":
            if start >= 2 and source[start - 2] == "\\":  # \\{{ -> "\" + expression
                out.append(("text", source[position:start - 1]))
            else:  # \{{ -> a literal mustache
                out.append(("text", source[position:start - 1] + match.group(0)))
                position = end
                continue
        else:
            out.append(("text", source[position:start]))
        strip_before, kind, expr, strip_after = match.group(1) == "~", match.group(2), match.group(3), \
            match.group(4) == "~"
        standalone = False
        if kind:
            line_start = source.rfind("\n", 0, start) + 1
            line_end = source.find("\n", end)
            line_end = len(source) if line_end < 0 else line_end
            standalone = source[line_start:start].strip() == "" and source[end:line_end].strip() == ""
        out.append(("tag", (kind, expr, strip_before, strip_after, standalone)))
        position = end
    out.append(("text", source[position:]))
    # Whitespace control and standalone lines act on the neighbouring text.
    for index, (kind, value) in enumerate(out):
        if kind != "tag":
            continue
        _, _, strip_before, strip_after, standalone = value
        if index > 0 and out[index - 1][0] == "text":
            text = out[index - 1][1]
            if strip_before:
                text = text.rstrip()
            elif standalone:
                text = text[:text.rfind("\n") + 1]
            out[index - 1] = ("text", text)
        if index + 1 < len(out) and out[index + 1][0] == "text":
            text = out[index + 1][1]
            if strip_after:
                text = text.lstrip()
            elif standalone:
                newline = text.find("\n")
                text = text[newline + 1:] if newline >= 0 else ""
            out[index + 1] = ("text", text)
    return out


def _parse(tokens: list[tuple[str, Any]], index: int = 0, closing: str | None = None):
    nodes: list[Any] = []
    while index < len(tokens):
        kind, value = tokens[index]
        if kind == "text":
            nodes.append(("text", value))
            index += 1
            continue
        tag_kind, expr = value[0], value[1]
        if tag_kind == "/":
            if expr != closing:
                raise TemplateError(f"unexpected {{{{/{expr}}}}}")
            return nodes, index + 1
        if tag_kind == "#":
            name = expr.split()[0]
            body, index = _parse(tokens, index + 1, name)
            nodes.append(("block", name, expr, body))
            continue
        nodes.append(("var", expr))
        index += 1
    if closing is not None:
        raise TemplateError(f"missing {{{{/{closing}}}}}")
    return nodes, index


def _lookup(path: str, scope: list[dict[str, Any]]) -> Any:
    if path.startswith("@"):
        for frame in reversed(scope):
            if path in frame:
                return frame[path]
        raise TemplateError(f"unknown {path}")
    head, *rest = path.split(".")
    value: Any = None
    for frame in reversed(scope):
        if head in frame:
            value = frame[head]
            break
    else:
        raise TemplateError(f"unknown value {head!r}")
    for part in rest:
        if part.startswith("[") and part.endswith("]"):
            value = value[int(part[1:-1])]
        else:
            value = value[part]
    return value


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    if _UNSAFE.search(text):
        raise TemplateError(f"value {text!r} would be escaped by Tauri; the fixture renderer refuses it")
    return text


def _render(nodes: list[Any], scope: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for node in nodes:
        if node[0] == "text":
            parts.append(node[1])
        elif node[0] == "var":
            expr = node[1]
            if expr.startswith("no-escape "):
                parts.append(str(_lookup(expr[len("no-escape "):].strip(), scope)))
            elif " " in expr:
                raise TemplateError(f"helper {expr.split()[0]!r} is not supported by the fixture renderer")
            else:
                parts.append(_escape(_lookup(expr, scope)))
        else:
            _, name, expr, body = node
            words = expr.split()
            if name == "if":
                if _lookup(words[1], scope):
                    parts.append(_render(body, scope))
            elif name == "each":
                items = _lookup(words[1], scope)
                alias = None
                if len(words) >= 4 and words[2] == "as":
                    alias = words[3].strip("|")
                pairs = list(items.items()) if isinstance(items, dict) else list(enumerate(items or []))
                for key, item in pairs:
                    frame = {"this": item, "@key": key, "@index": key}
                    if alias:
                        frame[alias] = item
                    parts.append(_render(body, scope + [frame]))
            else:
                raise TemplateError(f"block helper {name!r} is not supported")
    return "".join(parts)


def render(template: str, context: dict[str, Any]) -> str:
    nodes, _ = _parse(_tokens(template))
    return _render(nodes, [context])


def normalise(text: str) -> str:
    """Tauri writes the rendered script as UTF-8 with a BOM and CRLF; compare without both."""
    return text.lstrip("﻿").replace("\r\n", "\n")


# ------------------------------------------------------------------ context from a real generated script


def context_from_generated(template: str, generated: str) -> dict[str, Any]:
    """Rebuild the render context of a REAL Tauri-generated installer.nsi (fidelity evidence only)."""
    generated = normalise(generated)
    lines = generated.split("\n")
    context: dict[str, Any] = {"file_associations": [], "deep_link_protocols": []}
    defines = dict(re.findall(r'^!define (\w+) "(.*)"$', generated, re.M))
    for name, var in re.findall(r'^!define (\w+) "\{\{(\w+)\}\}"$', template, re.M):
        context[var] = defines[name]
    compression = re.search(r'^!if "(.*)" == "none"$', generated, re.M)
    context["compression"] = compression.group(1)
    plugins = re.search(r'^!addplugindir "(.*)"$', generated.split("!include MUI2.nsh")[0], re.M)
    context["signed_plugins_path"] = plugins.group(1) if plugins else ""
    hooks = re.search(r'^!include "(.*)"$', generated.split('!define WEBVIEW2APPGUID')[0].split("${StrLoc}")[1],
                      re.M)
    context["installer_hooks"] = hooks.group(1) if hooks else ""
    context["languages"] = re.findall(r'^!insertmacro MUI_LANGUAGE "(.*)"$', generated, re.M)
    after = generated.split("!insertmacro MUI_RESERVEFILE_LANGDLL\n")[1].split("Function .onInit")[0]
    context["language_files"] = re.findall(r'^  !include "(.*)"$', after, re.M)
    install = generated.split("Section Install\n")[1].split("SectionEnd")[0]
    resources_part = install.split("; Copy resources\n")[1].split("; Copy external binaries\n")[0]
    binaries_part = install.split("; Copy external binaries\n")[1].split("; Create file associations")[0]
    context["resources_dirs"] = re.findall(r'^    CreateDirectory "\$INSTDIR\\(.*)"$', resources_part, re.M)
    context["resources"] = {source: ["", target] for target, source in
                            re.findall(r'^    File /a "/oname=(.*)" "(.*)"$', resources_part, re.M)}
    context["binaries"] = {source: target for target, source in
                           re.findall(r'^    File /a "/oname=(.*)" "(.*)"$', binaries_part, re.M)}
    uninstall = generated.split("Section Uninstall\n")[1].split("SectionEnd")[0]
    context["resources_ancestors"] = re.findall(r'^  RMDir /REBOOTOK "\$INSTDIR\\(.*)"$', uninstall, re.M)
    if "APP_ASSOCIATE \"" in install or 'Software\\Classes\\' in install:
        raise TemplateError("file associations / deep links in the generated script are not reconstructed")
    del lines
    return context
