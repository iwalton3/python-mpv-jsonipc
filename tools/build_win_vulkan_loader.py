#!/usr/bin/env python3
"""Build the Vulkan loader the Windows CI puts beside mpv.exe, from pinned source.

shinchiro's mpv builds link ``vulkan-1.dll`` as a **hard** import from the
20260808 release onward. The Vulkan loader is installed by a GPU driver, not
by Windows, so a CI runner has none and Windows refuses to start mpv.exe at
all -- every live test fails for a reason unrelated to this library. Upstream
mpv's own Windows builds ship the loader beside mpv.exe for the same reason.
Before this, CI pinned the last build without the import (20260610) until
shinchiro pruned it from their releases.

Copied from jellyfin-mpv-shim's ``tools/build_win_vulkan_loader.py``, which
ships the same loader in its installer; the measurements behind it are there.
Changes here: the architecture is checked against mpv.exe (``--against``),
with the PE header read inline instead of through that repo's
``check_win_arch``.

Built rather than downloaded: the sources are two pinned, hashed tarballs and
a CMake run, and the alternative is redistributing someone else's binary.
"""

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request

#: Pinned, with hashes. A build input fetched over the network is a supply
#: chain, and the version is bumped deliberately -- not followed. The loader
#: and the headers move together: they are one SDK tag, and a loader built
#: against mismatched headers is the one combination that compiles and then
#: reports the wrong API version at runtime.
VERSION = "1.4.357.0"
_TAG = "vulkan-sdk-%s" % VERSION
_ARCHIVE = ("https://github.com/KhronosGroup/%s/archive/refs/tags/"
            "%s.tar.gz")
SOURCES = {
    "Vulkan-Headers":
        "e87dce08116151f6b6d7de6b6faf41498e87e6cf848ff16fa3bd5402190ad4a3",
    "Vulkan-Loader":
        "54f2537df22313768da0317dda2abdaaab7711b4081c48c869a79db343d0ae70",
}

#: What CMake emits, by platform. Only the Windows name is used; the
#: others exist so the fetch, the hash pin and the CMake invocation stay
#: runnable on a developer's machine, which is the only place they get
#: exercised outside a CI runner.
_OUTPUT_NAMES = {
    "nt": ("vulkan-1.dll",),
    "darwin": ("libvulkan.1.dylib", "libvulkan.dylib"),
}
_DEFAULT_OUTPUTS = ("libvulkan.so.1", "libvulkan.so")


def _output_names():
    if os.name == "nt":
        return _OUTPUT_NAMES["nt"]
    if sys.platform == "darwin":
        return _OUTPUT_NAMES["darwin"]
    return _DEFAULT_OUTPUTS


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fetch_one(dest_dir, name, sha256):
    archive = os.path.join(dest_dir, "%s-%s.tar.gz" % (name, _TAG))
    url = _ARCHIVE % (name, _TAG)
    if os.path.exists(archive):
        data = open(archive, "rb").read()
    else:
        print("downloading %s" % url)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        with open(archive, "wb") as handle:
            handle.write(data)

    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256:
        # Removed, so a retry re-downloads rather than re-checking a bad file
        # that is now cached.
        os.unlink(archive)
        raise SystemExit(
            "error: %s hashed %s, expected %s -- refusing to build it"
            % (archive, digest, sha256))

    source = os.path.join(dest_dir, "%s-%s" % (name, _TAG))
    if not os.path.isdir(source):
        with tarfile.open(archive) as tar:
            # `filter` is the 3.12+ spelling and the default from 3.14; named
            # so this does not depend on which of those the runner has.
            try:
                tar.extractall(dest_dir, filter="data")
            except TypeError:
                tar.extractall(dest_dir)
    return source


def fetch(dest_dir):
    """Download and verify both tarballs. Returns (headers, loader) dirs."""
    os.makedirs(dest_dir, exist_ok=True)
    return tuple(_fetch_one(dest_dir, name, SOURCES[name])
                 for name in ("Vulkan-Headers", "Vulkan-Loader"))


def _cmake(args, cwd):
    subprocess.run(["cmake"] + args, cwd=cwd, check=True)


def build(headers_src, loader_src, jobs=None):
    """Configure and build. Returns the path to the loader CMake produced."""
    prefix = os.path.join(os.path.dirname(headers_src), "_prefix")

    # The headers are a header-only install, but the loader wants them
    # *installed* -- VULKAN_HEADERS_INSTALL_DIR is a prefix, not a source
    # tree. Done here rather than with UPDATE_DEPS=ON, which is how
    # upstream's own CI does it: that clones a revision chosen by a file in
    # the tarball, at build time, with no hash. The whole point of pinning
    # is lost if the build then fetches its own dependency.
    hdr_build = os.path.join(headers_src, "_build")
    _cmake(["-S", ".", "-B", hdr_build, "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_INSTALL_PREFIX=" + prefix], headers_src)
    _cmake(["--install", hdr_build], headers_src)

    build_dir = os.path.join(loader_src, "_build")
    configure = [
        "-S", ".", "-B", build_dir,
        "-DCMAKE_BUILD_TYPE=Release",
        "-DVULKAN_HEADERS_INSTALL_DIR=" + prefix,
        # Off: the test suite wants googletest (another unpinned fetch) and
        # proves nothing about a loader we are only asking to load.
        "-DBUILD_TESTS=OFF",
        # Refuse the network explicitly. Without it a missing dependency is
        # a silent clone rather than a failed build, which is the one
        # outcome a pinned build must not have.
        "-DUPDATE_DEPS=OFF",
    ]
    if os.name != "nt":
        # WSI backends the CI has no use for, and which drag
        # in X11/Wayland headers that a build machine need not have. On
        # Windows the only surface is Win32 and there is nothing to turn
        # off.
        configure += ["-DBUILD_WSI_XCB_SUPPORT=OFF",
                      "-DBUILD_WSI_XLIB_SUPPORT=OFF",
                      "-DBUILD_WSI_WAYLAND_SUPPORT=OFF"]
    _cmake(configure, loader_src)

    compile_cmd = ["--build", build_dir, "--config", "Release"]
    if jobs:
        compile_cmd += ["--parallel", str(jobs)]
    _cmake(compile_cmd, loader_src)

    wanted = _output_names()
    for root, _dirs, files in os.walk(build_dir):
        for name in wanted:
            # By preference order, not by walk order. Resolved rather than
            # skipped-if-a-symlink: the ELF build's real file is
            # `libvulkan.so.<version>`, which is a name this cannot know,
            # and both the names it does know are links to it. (Windows,
            # the platform this is for, has one real `vulkan-1.dll` and
            # realpath is a no-op there.)
            if name in files:
                return os.path.realpath(os.path.join(root, name))
    raise SystemExit("error: the build produced none of %s under %s"
                     % (", ".join(wanted), build_dir))


def pe_machine(path):
    """The COFF machine type of a PE file (.exe or .dll), as an integer."""
    with open(path, "rb") as handle:
        header = handle.read(0x40)
        if len(header) < 0x40 or header[:2] != b"MZ":
            raise SystemExit("error: %s is not a PE image" % path)
        (pe_offset,) = struct.unpack_from("<I", header, 0x3C)
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\0\0":
            raise SystemExit("error: %s has no PE signature" % path)
        (machine,) = struct.unpack("<H", handle.read(2))
    return machine


def verify(dll, against):
    """Refuse a loader whose architecture does not match *against* (mpv.exe),
    the file that imports it: Windows would refuse mpv.exe and every live test
    would fail as "mpv will not start". A mismatch is not a build error
    anywhere, so it is checked here."""
    want, got = pe_machine(against), pe_machine(dll)
    if want != got:
        raise SystemExit("error: built a %#06x Vulkan loader for a %#06x %s"
                         % (got, want, os.path.basename(against)))
    print("%s: machine %#06x, matching %s"
          % (os.path.basename(dll), got, os.path.basename(against)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", default=None,
                        help="where to put the library (default: repo root)")
    parser.add_argument("--work", default=None,
                        help="scratch directory for the source tree")
    parser.add_argument("-j", "--jobs", type=int, default=None)
    parser.add_argument("--against", default=None,
                        help="the mpv.exe it is for; its architecture must match")
    args = parser.parse_args(argv)

    root = _repo_root()
    work = args.work or os.path.join(root, "_vulkan_loader_build")
    headers_src, loader_src = fetch(work)
    built = build(headers_src, loader_src, jobs=args.jobs)
    if args.against:
        verify(built, args.against)

    output = args.output or os.path.join(root, os.path.basename(built))
    if os.path.isdir(output):
        output = os.path.join(output, os.path.basename(built))
    shutil.copy2(built, output)
    print("wrote %s (%d bytes)" % (output, os.path.getsize(output)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
