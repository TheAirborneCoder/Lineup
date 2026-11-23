# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile the optional texture-decoding helpers into WORK/native.

bcpack.py decodes BCPack-compressed textures in Python. Two small C libraries (bcpack_data/bcpack_native.c and
bcpack_bc7.c) do the same work much faster. They are built here when a C compiler is found:

    Windows   MSVC (cl, found through vswhere or already on PATH), else clang or gcc on PATH
    macOS     cc / clang
    Linux     $CC, cc, gcc or clang

A missing compiler, or a failed compile, never fails anything: the Python path is used and textures decode more
slowly. bcpack.py looks for the libraries in WORK/native first.

    python engine/native_build.py [--force]
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import paths

SOURCES = ("bcpack_native", "bcpack_bc7")
EXPORTS = {"bcpack_native": "bcpack_lanes", "bcpack_bc7": "bc7_rebuild"}
DATA = paths.ENGINE / "bcpack_data"
RECEIPT = "native.json"


def suffix():
    return ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"


def library_names(base):
    """File names bcpack.py accepts for one library on this system."""
    return [base + suffix()]


def source_hash():
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update((DATA / f"{name}.c").read_bytes())
    digest.update(f"{sys.platform}-{platform.machine()}".encode())
    return digest.hexdigest()[:20]


def vcvars():
    """Path of vcvars64.bat (or the ARM64 one) of the newest Visual Studio with the C++ tools, or None."""
    roots = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")]
    for root in filter(None, roots):
        tool = Path(root) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if not tool.is_file():
            continue
        try:
            out = subprocess.run([str(tool), "-latest", "-products", "*", "-requires",
                                  "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property",
                                  "installationPath"], capture_output=True, text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        bat = "vcvarsarm64.bat" if platform.machine().lower() in ("arm64", "aarch64") else "vcvars64.bat"
        path = Path(out.splitlines()[0]) / "VC" / "Auxiliary" / "Build" / bat if out else None
        if path and path.is_file():
            return path
    return None


def find_compiler():
    """('msvc', vcvars bat or None) or ('cc', executable), or None."""
    if sys.platform == "win32":
        if shutil.which("cl"):
            return ("msvc", None)
        bat = vcvars()
        if bat:
            return ("msvc", bat)
    names = [os.environ.get("CC"), "cc", "gcc", "clang"] if sys.platform != "win32" else ["clang", "gcc"]
    for name in filter(None, names):
        found = shutil.which(name)
        if found:
            return ("cc", found)
    return None


def compile_one(compiler, base, folder, scratch):
    source = DATA / f"{base}.c"
    out = folder / (base + suffix())
    kind, tool = compiler
    if kind == "msvc":
        script = scratch / "build.bat"
        lines = ["@echo off"]
        if tool:
            lines.append(f'call "{tool}" >nul 2>&1 || exit /b 1')
        lines.append(f'cl /nologo /O2 /LD "{source}" /Fe:"{out}" /Fo:"{scratch}\\\\" >nul')
        script.write_text("\r\n".join(lines) + "\r\n")
        cmd = ["cmd", "/c", str(script)]
    else:
        shared = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
        cmd = [tool, "-O2", *shared, "-o", str(out), str(source)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=scratch)
    for leftover in (".lib", ".exp"):
        out.with_suffix(leftover).unlink(missing_ok=True)
    if result.returncode or not out.is_file():
        raise RuntimeError((result.stdout + result.stderr).strip()[-600:] or f"exit code {result.returncode}")
    import ctypes
    getattr(ctypes.CDLL(str(out)), EXPORTS[base])      # must load and export its entry point
    return out


def build(force=False, say=print):
    """Build the libraries that are missing or out of date. Returns the receipt; never raises."""
    folder = paths.NATIVE
    receipt_file = folder / RECEIPT
    want = source_hash()
    try:
        old = json.loads(receipt_file.read_text("utf-8"))
    except (OSError, ValueError):
        old = {}
    present = all((folder / n).is_file() for base in SOURCES for n in library_names(base))
    if not force and present and old.get("source") == want and old.get("status") == "built":
        return old
    compiler = find_compiler()
    if compiler is None:
        say("No C compiler found: textures will decode more slowly. "
            + ("Install the Visual Studio Build Tools (C++ workload) or LLVM/MinGW and run again."
               if sys.platform == "win32" else "Install gcc or clang and run again to speed up texture decoding."))
        return {"status": "no compiler", "source": want}
    folder.mkdir(parents=True, exist_ok=True)
    built, error = [], None
    with tempfile.TemporaryDirectory(dir=folder) as scratch:
        for base in SOURCES:
            try:
                compile_one(compiler, base, folder, Path(scratch))
                built.append(base)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                error = f"{base}: {exc}"
                break
    if error:
        for base in built:
            for n in library_names(base):
                (folder / n).unlink(missing_ok=True)
        say(f"Could not build the texture helpers ({error.splitlines()[0][:160]}). "
            "Textures will decode more slowly.")
        result = {"status": "failed", "source": want, "error": error}
    else:
        say(f"Built the texture decoding helpers with {compiler[0]} ({', '.join(built)}).")
        result = {"status": "built", "source": want, "compiler": compiler[0], "libraries": built}
    receipt_file.write_text(json.dumps(result, indent=1), "utf-8")
    return result


if __name__ == "__main__":
    build(force="--force" in sys.argv)
