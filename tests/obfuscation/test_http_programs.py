"""Build C-family HTTPS clients with every obfuscation pass and run them.

The local TLS server gives the clients real redirects, certificate validation,
and two different HTML documents without depending on a public web service.
"""

import argparse
import re
import shlex
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


USER_AGENT_MARKER = "LLVM-OBFUSCATION-HTTP-CLIENT-MARKER"
DOCUMENTS = {
    "/page": (
        b"<html><head><title>LLVM Obfuscation Fixture</title></head><body>"
        b'<a href="/alpha">alpha</a><a href="/beta">beta</a>'
        b'<a href="/gamma">gamma</a></body></html>',
        "title=LLVM Obfuscation Fixture;links=3;path_bytes=17\n",
    ),
    "/alt-page": (
        b'<html><head><title>Variant Page</title></head><body>'
        b'<a href="/delta">delta</a></body></html>',
        "title=Variant Page;links=1;path_bytes=6\n",
    ),
}
REDIRECTS = {"/start": "/page", "/alt-start": "/alt-page"}
SOURCES = {
    "c": ("http_c.c", "clang", False),
    "cpp": ("http_cpp.cpp", "clang++", False),
    "objc": ("http_objc.m", "clang", True),
    "objcpp": ("http_objcpp.mm", "clang++", True),
}
VARIANTS = {
    "baseline": (),
    "sobf": ("-sobf",),
    "sub": ("-sub", "-sub_loop=1"),
    "split": ("-split", "-split_num=2"),
    "bcf": ("-bcf", "-bcf_prob=100"),
    "fla": ("-fla",),
    "combined": ("-sobf", "-sub", "-split", "-bcf", "-bcf_prob=100", "-fla"),
}


def run(command, timeout=180, check=True):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise AssertionError(f"Timed out after {timeout}s: {command!r}") from error
    if check and result.returncode:
        raise AssertionError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path in REDIRECTS:
            self.send_response(302)
            self.send_header("Location", REDIRECTS[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path in DOCUMENTS:
            document = DOCUMENTS[self.path][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def make_certificate(work_dir):
    certificate = work_dir / "localhost.crt"
    key = work_dir / "localhost.key"
    run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
            "-nodes", "-days", "1", "-keyout", str(key), "-out",
            str(certificate), "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ]
    )
    return certificate, key


def start_server(work_dir):
    certificate, key = make_certificate(work_dir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.paths = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, certificate


def target_body(ir):
    lines = ir.splitlines()
    for first, line in enumerate(lines):
        if re.match(r"^define\b.*@transform_target\(", line):
            for last in range(first + 1, len(lines)):
                if lines[last] == "}":
                    return "\n".join(lines[first + 1 : last])
    raise AssertionError("transform_target definition missing from emitted IR")


def instruction_count(body):
    return len(re.findall(r"^\s+%[^=\n]+\s=\s", body, re.MULTILINE))


def branch_count(body):
    return len(re.findall(r"^\s+br\s", body, re.MULTILINE))


def assert_effect(language, level, name, ir, baseline):
    label = f"{language}/{level}/{name}"
    body = target_body(ir)
    base_body = target_body(baseline)
    if name == "sobf":
        assert USER_AGENT_MARKER in baseline, f"{label}: missing baseline marker"
        assert USER_AGENT_MARKER not in ir, f"{label}: plaintext string remains"
        assert "@llvm.global_ctors" in ir and ".datadiv_decode" in ir, (
            f"{label}: string decoder missing"
        )
    elif name == "sub":
        assert instruction_count(body) > instruction_count(base_body), (
            f"{label}: arithmetic substitution did not expand transform_target"
        )
    elif name == "split":
        assert branch_count(body) > branch_count(base_body), (
            f"{label}: split did not add branches to transform_target"
        )
    elif name == "bcf":
        assert re.search(r"^@x(?:\.\d+)?\s*=\s*common\b", ir, re.MULTILINE), (
            f"{label}: opaque-predicate global missing"
        )
        assert "urem i32" in body and branch_count(body) > branch_count(base_body), (
            f"{label}: bogus control flow missing from transform_target"
        )
    elif name == "fla":
        assert body.count("switch i32") > base_body.count("switch i32"), (
            f"{label}: flatten dispatcher missing from transform_target"
        )
    elif name == "combined":
        assert_effect(language, level, "sobf", ir, baseline)
        assert re.search(r"^@x(?:\.\d+)?\s*=\s*common\b", ir, re.MULTILINE), (
            f"{label}: opaque-predicate global missing"
        )
        assert "urem i32" in body, f"{label}: BCF missing from transform_target"
        assert "switch i32" in body, f"{label}: flatten dispatcher missing"
        assert re.search(r"(?m)^[^\s;][^:\n]*\.split[^:\n]*:", body), (
            f"{label}: split block missing"
        )


def run_client(server, certificate, executable, start_path, expected):
    before = len(server.paths)
    url = f"https://127.0.0.1:{server.server_port}{start_path}"
    actual = run([str(executable), url, str(certificate)], timeout=20)
    assert actual.stdout == expected, (
        f"{executable.name} returned {actual.stdout!r}, expected {expected!r}"
    )
    assert server.paths[before:] == [start_path, REDIRECTS[start_path]], (
        f"{executable.name} did not fetch and follow the HTTPS redirect: "
        f"{server.paths[before:]!r}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clang", type=Path, required=True)
    parser.add_argument("--opt", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--sysroot", type=Path)
    args = parser.parse_args()

    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    clang_path = args.clang.resolve()
    clangxx_path = clang_path.with_name("clang++")
    opt = str(args.opt.resolve())
    sysroot = args.sysroot.resolve() if args.sysroot else None
    if sys.platform == "darwin" and sysroot is None:
        sysroot = Path(run(["xcrun", "--show-sdk-path"]).stdout.strip())
    sysroot_flags = ["-isysroot", str(sysroot)] if sysroot else []
    curl_cflags = shlex.split(run(["pkg-config", "--cflags", "libcurl"]).stdout)
    curl_libs = shlex.split(run(["pkg-config", "--libs", "libcurl"]).stdout)

    server, thread, certificate = start_server(work_dir)
    try:
        for language, (filename, compiler_name, needs_objc) in SOURCES.items():
            source = Path(__file__).with_name(filename).resolve()
            compiler = str(clangxx_path if compiler_name == "clang++" else clang_path)
            objc_flags = (
                ["-fobjc-runtime=gcc"]
                if needs_objc and sys.platform == "linux"
                else []
            )
            objc_link = ["-lobjc"] if needs_objc else []
            for level in ("O0", "O2"):
                ir_by_variant = {}
                for name, options in VARIANTS.items():
                    stem = work_dir / f"{language}-{level}-{name}"
                    ir_path = stem.with_suffix(".ll")
                    executable = stem.with_suffix(".exe") if sys.platform == "win32" else stem
                    flags = [item for option in options for item in ("-mllvm", option)]
                    common = [
                        compiler, *sysroot_flags, f"-{level}",
                        "-fno-discard-value-names", *objc_flags, *flags,
                        *curl_cflags,
                        str(source),
                    ]
                    print(f"[{language}/{level}] compile {name}", flush=True)
                    run([*common, "-S", "-emit-llvm", "-o", str(ir_path)])
                    run([opt, "-passes=verify", "-disable-output", str(ir_path)])
                    run([*common, *curl_libs, *objc_link, "-o", str(executable)])
                    ir_by_variant[name] = ir_path.read_text(encoding="utf-8")
                    for start_path, destination in REDIRECTS.items():
                        run_client(
                            server, certificate, executable, start_path,
                            DOCUMENTS[destination][1],
                        )
                    if name == "baseline":
                        missing = run(
                            [
                                str(executable),
                                f"https://127.0.0.1:{server.server_port}/missing",
                                str(certificate),
                            ],
                            timeout=20,
                            check=False,
                        )
                        assert missing.returncode != 0, (
                            f"{language}/{level}: missing page unexpectedly succeeded"
                        )
                baseline = ir_by_variant["baseline"]
                for name in VARIANTS:
                    if name != "baseline":
                        assert_effect(language, level, name, ir_by_variant[name], baseline)
                print(f"[{language}/{level}] all HTTPS clients and transform effects passed", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
