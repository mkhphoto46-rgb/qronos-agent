from unittest.mock import patch
import ast
import json
import re
from pathlib import Path

import pytest

from core.web_providers import requests_transport
from core.computer_executor import (
    _default_gui_action, KNOWN_APPS, ComputerOperation,
)


def test_legacy_web_transport_refuses_before_network_io():
    with patch("requests.request") as request:
        with pytest.raises(PermissionError, match="browser adapter"):
            requests_transport("GET", "https://example.com")
        request.assert_not_called()


def test_ungrounded_paint_input_is_disabled():
    with pytest.raises(RuntimeError, match="grounding"):
        _default_gui_action(KNOWN_APPS["paint"], ComputerOperation.DRAW_BASIC_HOUSE)


def test_webview_has_no_ungated_native_opener_permission():
    root = Path(__file__).resolve().parent.parent
    for path in (root / "desktop/src-tauri/capabilities").glob("*.json"):
        capability = json.loads(path.read_text(encoding="utf-8"))
        for permission in capability.get("permissions", []):
            identifier = permission if isinstance(permission, str) else permission["identifier"]
            assert not identifier.startswith("opener:"), path.name


# --------------------------------------------------------------------------
# Product network inventory
#
# Every way a product module could reach a network must be reviewed. The
# scanner below reports, per module, the network *families* it uses: a network
# module imported under any name or alias, a dynamic import (literal or
# computed), asyncio's connection/server APIs, a native network DLL, a COM
# HTTP object, and a subprocess or shell whose arguments name a download tool,
# a web cmdlet or a URL. Each module may use exactly the families its reviewed
# exception lists; an unused grant is stale and fails too, so an exception can
# never quietly widen. Behavioral tests elsewhere still prove each hardened
# sidecar is loopback-only; this inventory proves nothing new slipped past them.

PRODUCT_PACKAGES = ("core", "security")
PRODUCT_FILES = ("build_support/runtime_entry.py",)  # frozen backend entry point

# Imported module (or dotted prefix) -> family. Longest prefix wins.
# urllib.parse/urllib.error, http.HTTPStatus and http.cookies do no I/O.
NETWORK_MODULES = {
    "socket": "socket", "_socket": "socket", "ssl": "ssl", "_ssl": "ssl",
    "selectors": "socket", "requests": "requests", "urllib3": "urllib3",
    "httpx": "httpx", "aiohttp": "aiohttp", "websocket": "websocket",
    "websockets": "websocket", "urllib.request": "urllib.request",
    "http.client": "http.client", "http.server": "http.server",
    "socketserver": "socketserver", "ftplib": "ftplib", "smtplib": "smtplib",
    "imaplib": "imaplib", "poplib": "poplib", "nntplib": "nntplib",
    "telnetlib": "telnetlib", "xmlrpc.client": "xmlrpc", "xmlrpc.server": "xmlrpc",
    "webbrowser": "webbrowser", "win32inet": "native-network",
    # Listener/Client also speak TCP, so a local-pipe use must be granted explicitly.
    "multiprocessing.connection": "multiprocessing-connection",
    "multiprocessing.managers": "multiprocessing-connection",
    "winhttp": "native-network", "pycurl": "native-network",
}
# A parent package whose submodule may be bound by "from P import sub".
NETWORK_PARENTS = {"urllib": {"request"}, "http": {"client", "server"}, "xmlrpc": {"client", "server"},
                   "multiprocessing": {"connection", "managers"}}
# asyncio itself is local (events, queues, subprocesses); these calls are not.
ASYNCIO_NETWORK_CALLS = {
    "open_connection", "start_server", "open_unix_connection", "start_unix_server",
    "create_connection", "create_server", "create_datagram_endpoint",
    "create_unix_connection", "create_unix_server", "sock_connect", "sock_accept",
    "connect_accepted_socket", "start_tls", "getaddrinfo", "getnameinfo",
}
NATIVE_NETWORK_DLLS = {"ws2_32", "wsock32", "mswsock", "winhttp", "wininet", "urlmon", "httpapi", "dnsapi", "websocket"}
COM_NETWORK_OBJECTS = ("xmlhttp", "winhttprequest", "internetexplorer.application", "msxml2.serverxmlhttp")
# Download tools, web cmdlets and .NET network types that a script or child
# process could use. Matched as whole words in subprocess arguments, and the
# web-cmdlet/.NET ones in any product string (an embedded PowerShell script).
SHELL_NETWORK_TOKENS = (
    "curl", "curl.exe", "wget", "wget.exe", "bitsadmin", "certutil", "ftp.exe", "tftp",
    "telnet", "ncat", "nc.exe", "ssh", "scp", "sftp", "mshta", "nslookup", "resolve-dnsname",
    "test-netconnection", "iwr", "irm",
)
SCRIPT_NETWORK_TOKENS = (
    "invoke-webrequest", "invoke-restmethod", "start-bitstransfer", "net.webclient",
    "system.net.webclient", "downloadstring", "downloadfile", "system.net.http",
    "httpclient", "net.sockets", "tcpclient", "webrequest]::", "net.httpwebrequest",
)
URL_SCHEMES = ("http://", "https://", "ftp://", "ws://", "wss://")
PROCESS_CALLS = {
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "os.system", "os.popen", "os.startfile", "os.execv", "os.execve", "os.execl",
    "os.execlp", "os.execvp", "os.spawnv", "os.spawnl", "os.spawnve", "os.spawnlp",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
}
DYNAMIC_IMPORTS = {"__import__", "importlib.import_module", "importlib.__import__", "importlib.util.find_spec", "imp.load_module"}
DYNAMIC_CODE = {"exec", "eval", "compile"}

INVENTORY_FAMILIES = set(NETWORK_MODULES.values()) | {
    "native-network", "dynamic-import", "dynamic-code", "asyncio-network",
    "subprocess-network", "script-network", "com-network",
}

# Module -> (exact families it may use, why). Never a wildcard or package-wide grant.
NETWORK_EXCEPTIONS = {
    "core.local_ipc": (
        {"socket", "requests", "urllib3", "urllib.request", "http.client"},
        "Bounded loopback HTTP; sidecar ownership or local-model provenance; no proxy/redirects.",
    ),
    "core.memory_runtime": ({"socket", "requests"}, "Owned CPU sidecar lifecycle and fixed loopback port probe."),
    "core.whisper_server_runtime": ({"socket", "urllib.request"}, "Owned STT sidecar request objects and loopback readiness."),
    "core.chatterbox_runtime": ({"socket", "urllib.request"}, "Owned TTS sidecar request objects and loopback port allocation."),
    "core.web_providers": ({"requests"}, "Disabled legacy transport; test proves refusal before requests IO."),
    "core.link_client": ({"socket", "ssl"}, "Explicit authenticated/encrypted Device Link transport exception."),
    "core.link_server": ({"socket", "ssl"}, "Permission-controlled Device Link listener and its stop socketpair."),
    "core.link_transport": ({"socket", "ssl"}, "Authenticated/encrypted Device Link TLS transport."),
    "core.link_pairing": ({"socket"}, "Local Device Link address discovery; no inference or web requests."),
    # D8 supervisor/broker channel (Runtime c3649ad, not yet integrated).
    "core.supervisor_ipc": (
        {"multiprocessing-connection"},
        "Windows named pipes only: require_local_address admits only local qronos-<purpose>-<128-bit hex> "
        "pipe names and refuses address tuples, remote server pipes and other names; no TCP endpoint.",
    ),
    "build_support.runtime_entry": (
        {"dynamic-import"},
        "Frozen entry imports only a value of its fixed SERVICES map; proven local below.",
    ),
    # Root-approved 2026-10-08 for the Link worker's Layer 2 dialler.
    "core.link_tunnel": (
        {"socket", "ssl"},
        "Opt-in outbound Device Link relay dialler to a user-configured relay; carries only "
        "end-to-end TLS-PSK link traffic; no content fetch or inference.",
    ),
}
# Approved exceptions whose module has not landed yet. Remove once it exists.
PENDING_EXCEPTIONS = {"core.link_tunnel"}


def _dotted(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - unparse of exotic nodes
        return ""


def _module_family(name: str) -> str | None:
    best = None
    for prefix, family in NETWORK_MODULES.items():
        if name == prefix or name.startswith(prefix + "."):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, family)
    return best[1] if best else None


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                nodes.add(id(body[0].value))
    return nodes


def _strings(node: ast.AST, assigned: dict[str, list[ast.AST]], depth: int = 0) -> list[str]:
    """String constants an argument can carry, following simple local names."""
    found: list[str] = []
    if depth > 3:
        return found
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.append(child.value)
        elif isinstance(child, ast.Name) and child.id in assigned:
            for value in assigned[child.id]:
                found.extend(_strings(value, assigned, depth + 1))
    return found


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_.\-\]:]+", text.lower()))


def network_findings(source: str) -> dict[str, set[str]]:
    """Map each network family a module's source uses to evidence strings."""
    tree = ast.parse(source)
    found: dict[str, set[str]] = {}

    def add(family: str, evidence: str) -> None:
        found.setdefault(family, set()).add(evidence)

    imported_roots: set[str] = set()
    aliases: dict[str, str] = {}  # local name -> canonical dotted name
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def canonical(node: ast.AST) -> str:
        dotted = _dotted(node)
        head, _, rest = dotted.partition(".")
        if head in aliases:
            return aliases[head] + ("." + rest if rest else "")
        return dotted

    assigned: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assigned.setdefault(target.id, []).append(node.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
                family = _module_family(alias.name)
                if family:
                    add(family, f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported_roots.add(node.module.split(".")[0])
            family = _module_family(node.module)
            if family:
                add(family, f"from {node.module} import ...")
            for alias in node.names:
                if alias.name in NETWORK_PARENTS.get(node.module, ()):
                    add(_module_family(f"{node.module}.{alias.name}"), f"from {node.module} import {alias.name}")
        elif isinstance(node, ast.Attribute):
            dotted = canonical(node)
            root = dotted.split(".")[0]
            if root in NETWORK_PARENTS and root in imported_roots:
                family = _module_family(dotted)
                if family:
                    add(family, dotted)
            if node.attr.lower() in NATIVE_NETWORK_DLLS and ("windll" in dotted or "oledll" in dotted or "cdll" in dotted):
                add("native-network", dotted)
        elif isinstance(node, ast.Subscript) and _dotted(node.value) == "sys.modules":
            for text in _strings(node.slice, {}):
                if _module_family(text):
                    add("dynamic-import", f"sys.modules[{text!r}]")

    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            lowered = node.value.lower()
            stem = lowered.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].removesuffix(".dll")
            if stem in NATIVE_NETWORK_DLLS:
                add("native-network", f"library {node.value!r}")
            for token in SCRIPT_NETWORK_TOKENS:
                if token in lowered:
                    add("script-network", token)
            for token in COM_NETWORK_OBJECTS:
                if token in lowered:
                    add("com-network", token)
        if not isinstance(node, ast.Call):
            continue
        name = canonical(node.func)
        tail = name.rsplit(".", 1)[-1]
        if name in DYNAMIC_IMPORTS or tail in {"import_module", "__import__"}:
            literals = [a.value for a in node.args[:1] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if not literals:
                add("dynamic-import", f"{name}(<computed>)")
            elif _module_family(literals[0]):
                add("dynamic-import", f"{name}({literals[0]!r})")
        if name in DYNAMIC_CODE:
            add("dynamic-code", name)
        if tail in ASYNCIO_NETWORK_CALLS and ("asyncio" in imported_roots or name.startswith("asyncio.")):
            add("asyncio-network", name)
        if name in PROCESS_CALLS or tail in {"create_subprocess_exec", "create_subprocess_shell"}:
            texts = []
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                texts.extend(_strings(argument, assigned))
            for text in texts:
                lowered = text.lower()
                words = _words(lowered)
                for token in SHELL_NETWORK_TOKENS + SCRIPT_NETWORK_TOKENS:
                    if token in words or (token in SCRIPT_NETWORK_TOKENS and token in lowered):
                        add("subprocess-network", f"{name}: {token}")
                if any(scheme in lowered for scheme in URL_SCHEMES):
                    add("subprocess-network", f"{name}: url")
        if tail in {"WinDLL", "CDLL", "OleDLL", "LoadLibrary", "LoadLibraryW", "LoadLibraryExW"}:
            for text in _strings(node, assigned):
                stem = text.lower().rsplit("\\", 1)[-1].removesuffix(".dll")
                if stem in NATIVE_NETWORK_DLLS:
                    add("native-network", f"{name}({text!r})")
    return found


def _product_modules(root: Path) -> list[tuple[str, Path]]:
    modules = []
    for package in PRODUCT_PACKAGES:
        for path in sorted((root / package).rglob("*.py")):
            modules.append((".".join(path.relative_to(root).with_suffix("").parts), path))
    for relative in PRODUCT_FILES:
        path = root / relative
        modules.append((".".join(Path(relative).with_suffix("").parts), path))
    return modules


def _inventory_violations(root: Path, exceptions=NETWORK_EXCEPTIONS, pending=PENDING_EXCEPTIONS) -> list[str]:
    violations = []
    seen = set()
    for module, path in _product_modules(root):
        seen.add(module)
        families = network_findings(path.read_text(encoding="utf-8"))
        allowed = exceptions.get(module, (set(), ""))[0]
        for family in sorted(set(families) - allowed):
            violations.append(f"{module}: undeclared {family} via {sorted(families[family])}")
        for family in sorted(allowed - set(families)):
            violations.append(f"{module}: stale grant {family!r} (module no longer uses it)")
    for module in sorted(set(exceptions) - seen - set(pending)):
        violations.append(f"{module}: exception for a module that does not exist")
    for module in sorted(set(pending) & seen):
        violations.append(f"{module}: has landed; remove it from PENDING_EXCEPTIONS")
    return violations


def test_product_network_imports_require_an_explicit_reviewed_exception():
    root = Path(__file__).resolve().parent.parent
    violations = _inventory_violations(root)
    assert not violations, "Unreviewed product network paths:\n" + "\n".join(violations)


def test_every_network_exception_is_exact_and_justified():
    for module, (families, why) in NETWORK_EXCEPTIONS.items():
        assert module.count(".") >= 1 and "*" not in module and not module.endswith("."), module
        assert families and families <= INVENTORY_FAMILIES, module
        assert len(why) > 30, module
    # Device Link is the only internet exception; the sidecars are loopback-only.
    assert {m for m, (f, _) in NETWORK_EXCEPTIONS.items() if "ssl" in f} == {
        "core.link_client", "core.link_server", "core.link_transport", "core.link_tunnel",
    }
    assert PENDING_EXCEPTIONS <= set(NETWORK_EXCEPTIONS)


def test_frozen_entry_dynamic_import_is_limited_to_fixed_local_services():
    root = Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "build_support/runtime_entry.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _dotted(n.func).endswith("import_module")]
    # Allowed arguments: SERVICES[...] or a loop variable over SERVICES.values().
    loop_names = {
        n.target.id for n in ast.walk(tree)
        if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and _dotted(n.iter) == "SERVICES.values()"
    }
    assert calls and all(
        _dotted(c.args[0]).startswith("SERVICES[") or _dotted(c.args[0]) in loop_names for c in calls
    ), [_dotted(c.args[0]) for c in calls]
    assert not any(isinstance(n, ast.Assign) and any(_dotted(t) in loop_names for t in n.targets) for n in ast.walk(tree))
    services = next(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SERVICES" for t in n.targets)
    )
    targets = ast.literal_eval(services)
    assert targets and all(value.startswith("core.") for value in targets.values())
    # Each target is itself covered by the inventory, so the indirection adds no path.
    modules = {name for name, _ in _product_modules(root)}
    assert set(targets.values()) <= modules
    assert not set(targets.values()) & set(NETWORK_EXCEPTIONS)


# Each probe is a whole product module someone could plausibly add. The name is
# the family the inventory must report for it.
ADVERSARIAL_PROBES = {
    "requests": ["import requests\nrequests.get('https://example.com')\n"],
    "socket": [
        "import socket as s\ns.create_connection(('example.com', 443))\n",
        "from socket import create_connection as dial\n",
        "from _socket import socket\n",
        "import selectors\n",
    ],
    "urllib3": ["import urllib3\nurllib3.PoolManager().request('GET', 'https://example.com')\n"],
    "ssl": ["import ssl as _t\n_t.create_default_context()\n"],
    "urllib.request": [
        "from urllib import request\nrequest.urlopen('https://example.com')\n",
        "import urllib\nurllib.request.urlopen('https://example.com')\n",
        "from urllib.request import urlopen as fetch\n",
    ],
    "http.client": ["from http import client as c\nc.HTTPSConnection('example.com')\n"],
    "http.server": ["import http.server\n"],
    "socketserver": ["import socketserver\n"],
    "ftplib": ["import ftplib\nftplib.FTP('example.com')\n"],
    "smtplib": ["from smtplib import SMTP_SSL\n"],
    "imaplib": ["import imaplib\n"],
    "poplib": ["import poplib\n"],
    "xmlrpc": ["from xmlrpc import client\nclient.ServerProxy('https://example.com')\n"],
    "httpx": ["import httpx\n"],
    "aiohttp": ["import aiohttp\n"],
    "websocket": ["import websockets\n"],
    "webbrowser": ["import webbrowser\nwebbrowser.open('https://example.com')\n"],
    "asyncio-network": [
        "import asyncio\nasync def f():\n    await asyncio.open_connection('example.com', 443)\n",
        "import asyncio\nasync def f():\n    loop = asyncio.get_running_loop()\n    await loop.create_connection(object, 'example.com', 443)\n",
        "import asyncio\nasync def f():\n    await asyncio.start_server(print, '0.0.0.0', 80)\n",
    ],
    "dynamic-import": [
        "import importlib\nimportlib.import_module('requests').get('https://example.com')\n",
        "net = __import__('socket')\n",
        "import importlib\nname = 'soc' + 'ket'\nimportlib.import_module(name)\n",
        "from importlib import import_module as load\nload('urllib.request')\n",
        "import sys\nsys.modules['socket']\n",
    ],
    "dynamic-code": ["exec('import socket')\n", "eval(compile('1', 'x', 'eval'))\n"],
    "multiprocessing-connection": [
        "from multiprocessing.connection import Listener\nListener(('0.0.0.0', 6000))\n",
        "import multiprocessing.connection as mpc\nmpc.Client(('example.com', 6000))\n",
        "from multiprocessing import connection\n",
        "from multiprocessing.managers import BaseManager\n",
    ],
    "subprocess-network": [
        "import subprocess\nsubprocess.run(['curl', '-o', 'x', 'https://example.com'])\n",
        "import subprocess\nsubprocess.run('curl.exe https://example.com', shell=True)\n",
        "import subprocess\ntool = 'bitsadmin'\nsubprocess.Popen([tool, '/transfer', 'x'])\n",
        "import subprocess as sp\nsp.check_output(['certutil', '-urlcache', '-f', 'u', 'o'])\n",
        "import os\nos.system('wget example.com')\n",
        "import os\nos.startfile('https://example.com')\n",
        "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('ssh', 'host')\n",
        "import subprocess\nsubprocess.run(['explorer.exe', 'https://example.com'])\n",
    ],
    "script-network": [
        "import subprocess\nSCRIPT = 'Invoke-WebRequest -Uri $u -OutFile $o'\nsubprocess.run(['powershell.exe', '-Command', SCRIPT])\n",
        "SCRIPT = '(New-Object Net.WebClient).DownloadString($u)'\n",
        "def f(u):\n    return f\"[System.Net.Http.HttpClient]::new().GetAsync('{u}')\"\n",
        "S = 'Start-BitsTransfer -Source $u'\n",
    ],
    "native-network": [
        "import ctypes\nctypes.windll.ws2_32.WSAStartup\n",
        "import ctypes\nctypes.WinDLL('winhttp.dll')\n",
        "import ctypes\nlib = 'urlmon'\nctypes.WinDLL(lib).URLDownloadToFileW\n",
        "import ctypes\nctypes.windll.LoadLibraryW('WININET.DLL')\n",
    ],
    "com-network": ["CLASS = 'MSXML2.ServerXMLHTTP.6.0'\n", "X = 'WinHttp.WinHttpRequest.5.1'\n"],
}

# Real local patterns the inventory must not report as network access.
BENIGN_PROBES = [
    "import asyncio\nasync def f():\n    await asyncio.sleep(0)\n    q = asyncio.Queue()\n",
    "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('whisper-cli.exe', '-m', 'model.bin')\n",
    "from urllib.parse import urlparse, quote\nurlparse('https://example.com')\n",
    "import urllib.error\n",
    "from http import HTTPStatus\n",
    "import subprocess\nsubprocess.run(['powershell.exe', '-NoProfile', '-Command', '[Windows.UI.Automation]'])\n",
    "import subprocess\nsubprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv'])\n",
    "'''Docstring: never shell out to curl or Invoke-WebRequest; see https://example.com.'''\n",
    "import ctypes\nctypes.windll.user32.GetForegroundWindow()\n",
    "import re\nre.compile('x')\n",
    "LINK = 'https://example.com'  # a saved link the browser workflow opens, not a fetch\n",
]


def _flat_probes():
    for family, sources in ADVERSARIAL_PROBES.items():
        for index, source in enumerate(sources):
            yield pytest.param(family, source, id=f"{family}-{index}")


@pytest.mark.parametrize("family, source", list(_flat_probes()))
def test_inventory_reports_each_bypass_family(family, source):
    findings = network_findings(source)
    assert family in findings, f"{family} bypass not reported; findings={findings}"


@pytest.mark.parametrize("source", BENIGN_PROBES, ids=lambda s: s.splitlines()[0][:40])
def test_inventory_does_not_flag_local_only_patterns(source):
    assert network_findings(source) == {}


def _fake_product(tmp_path: Path, modules: dict[str, str]) -> Path:
    for package in PRODUCT_PACKAGES:
        (tmp_path / package).mkdir()
        (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "build_support").mkdir()
    (tmp_path / "build_support" / "runtime_entry.py").write_text("", encoding="utf-8")
    for module, source in modules.items():
        path = tmp_path.joinpath(*module.split(".")).with_suffix(".py")
        path.write_text(source, encoding="utf-8")
    return tmp_path


def test_an_exception_cannot_widen_to_another_family(tmp_path):
    root = _fake_product(tmp_path, {"core.link_tunnel": "import socket\nimport ssl\nimport requests\n"})
    violations = _inventory_violations(root, {"core.link_tunnel": NETWORK_EXCEPTIONS["core.link_tunnel"]}, set())
    assert violations == ["core.link_tunnel: undeclared requests via ['import requests']"]


def test_an_exception_for_one_module_does_not_cover_a_sibling(tmp_path):
    root = _fake_product(tmp_path, {
        "core.link_tunnel": "import socket\nimport ssl\n",
        "core.link_tunnel_helper": "import socket\n",
    })
    violations = _inventory_violations(root, {"core.link_tunnel": NETWORK_EXCEPTIONS["core.link_tunnel"]}, set())
    assert violations == ["core.link_tunnel_helper: undeclared socket via ['import socket']"]


def test_stale_and_missing_exceptions_are_reported(tmp_path):
    root = _fake_product(tmp_path, {"core.link_tunnel": "import socket\n"})
    exceptions = {
        "core.link_tunnel": NETWORK_EXCEPTIONS["core.link_tunnel"],
        "core.gone": ({"socket"}, "x" * 40),
    }
    assert _inventory_violations(root, exceptions, set()) == [
        "core.link_tunnel: stale grant 'ssl' (module no longer uses it)",
        "core.gone: exception for a module that does not exist",
    ]
    # Once a pending module lands, the pending marker must be removed.
    assert "core.link_tunnel: has landed; remove it from PENDING_EXCEPTIONS" in _inventory_violations(
        root, {"core.link_tunnel": ({"socket"}, "x" * 40)}, {"core.link_tunnel"},
    )


def test_inventory_scans_nested_packages_and_the_frozen_entry_point(tmp_path):
    root = _fake_product(tmp_path, {})
    (root / "core" / "adapters").mkdir()
    (root / "core" / "adapters" / "fetch.py").write_text("import http.client\n", encoding="utf-8")
    (root / "build_support" / "runtime_entry.py").write_text("import importlib\nimportlib.import_module(x)\n", encoding="utf-8")
    assert _inventory_violations(root, {}, set()) == [
        "core.adapters.fetch: undeclared http.client via ['import http.client']",
        "build_support.runtime_entry: undeclared dynamic-import via ['importlib.import_module(<computed>)']",
    ]


def test_hardened_sidecars_cannot_bypass_the_local_http_boundary():
    root = Path(__file__).resolve().parent.parent
    for filename in ("memory_runtime.py", "whisper_server_runtime.py", "chatterbox_runtime.py"):
        tree = ast.parse((root / "core" / filename).read_text(encoding="utf-8"))
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        forbidden = {"requests.get", "requests.post", "requests.request", "requests.Session", "urllib.request.urlopen", "urllib.request.build_opener"}
        assert not calls & forbidden, f"{filename} bypassed reviewed local IPC: {calls & forbidden}"
