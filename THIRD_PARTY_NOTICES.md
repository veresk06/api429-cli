# Third-party notices

API429 CLI's Python package depends on third-party libraries. The standalone
executables additionally embed a Python runtime, the PyInstaller bootloader,
and platform-dependent native code. Those components remain under their own
licenses; the API429 MIT license does not replace them.

The complete license texts distributed with standalone builds are in the
`licenses/` directory. A license text is included in every target's legal
corpus when a component may be present on one or more supported targets;
inclusion does not imply that every target embeds that component.

## Direct standalone runtime

| Component | Version used for release builds | License | License text |
| --- | --- | --- | --- |
| CPython | 3.13.13 | Python Software Foundation License Version 2 and incorporated-software notices | `licenses/CPython-3.13.13.txt` |
| Official CPython Windows binary runtime | 3.13.13 | CPython terms plus the Microsoft Distributable Code conditions shipped by Python.org | `licenses/CPython-3.13.13-Windows.txt` |
| PyInstaller bootloader and runtime hooks | 6.22.2 | GPL-2.0-or-later with the PyInstaller Bootloader Exception; Apache-2.0 for runtime hooks | `licenses/PyInstaller-6.22.2.txt` |
| PyInstaller community runtime hooks, when selected by a build | 2026.7 | Apache-2.0 (other build-time hooks are GPL-2.0-or-later) | `licenses/PyInstaller-Hooks-Contrib-2026.7.txt` |
| anyio | 4.14.2 | MIT | `licenses/anyio-4.14.2.txt` |
| certifi / Mozilla CA certificate data | 2026.7.22 | MPL-2.0 | `licenses/certifi-2026.7.22.txt`, `licenses/MPL-2.0.txt` |
| h11 | 0.16.0 | MIT | `licenses/h11-0.16.0.txt` |
| httpcore | 1.0.9 | BSD-3-Clause | `licenses/httpcore-1.0.9.txt` |
| httpx | 0.28.1 | BSD-3-Clause | `licenses/httpx-0.28.1.txt` |
| idna | 3.19 | BSD-3-Clause | `licenses/idna-3.19.txt` |

The certifi package contains a modified copy of Mozilla's CA certificate
bundle. The corresponding source-form package is available from
<https://pypi.org/project/certifi/2026.7.22/> and
<https://github.com/certifi/python-certifi>.

## Code incorporated in or linked with the standalone Python runtime

CPython's complete license and acknowledgement document is included above.
For clarity and conservative coverage across macOS, Linux, and Windows builds,
the legal corpus also carries the upstream texts for native components that
may be incorporated in or linked with the supported CPython runtime:

| Component | License | License text |
| --- | --- | --- |
| BLAKE2 reference implementation | CC0-1.0 | `licenses/BLAKE2-CC0-1.0.txt` |
| HACL\* cryptographic implementation | MIT | `licenses/HACL-MIT.txt` |
| bzip2 | bzip2-1.0.6 | `licenses/bzip2.txt` |
| Expat | MIT | `licenses/expat.txt` |
| libffi | MIT | `licenses/libffi.txt` |
| liblzma / XZ Utils | 0BSD | `licenses/liblzma.txt` |
| libuuid | BSD-3-Clause | `licenses/libuuid.txt` |
| mpdecimal | BSD-2-Clause | `licenses/mpdecimal.txt` |
| OpenSSL 3.x | Apache-2.0 | `licenses/OpenSSL-3.txt` |
| SQLite | Public domain dedication | `licenses/SQLite.txt` |
| zlib | Zlib | `licenses/zlib.txt` |

Each build manifest records its exact `frozen_native_files`. In the audited
Windows builds this inventory included `VCRUNTIME140*.dll`, `ucrtbase.dll`, and
`api-ms-win-core-*` / `api-ms-win-crt-*` forwarder DLLs from the official
CPython Windows installation. Python.org's x64 and ARM64 3.13.13 embeddable
packages ship the same Windows `LICENSE.txt`; its “Additional Conditions for
this Windows binary build” section expressly addresses further distribution of
Microsoft Distributable Code and lists the controlling restrictions. The
complete file is preserved as `licenses/CPython-3.13.13-Windows.txt`. API429
does not relicense those Microsoft components under MIT.

This notice is informational and is not legal advice. Copyright statements,
warranty disclaimers, attribution requirements, and license exceptions in the
referenced full texts control.
