# `@api429/cli`

Official command-line client for the API429 gateway.

```bash
npm install --global @api429/cli
api429 --version
```

The package installs a small Node.js launcher and one platform-specific native
binary through npm `optionalDependencies`. It never downloads executables from
a `postinstall` script.

Supported targets are macOS, glibc-based Linux, and Windows on x64 and arm64.
Installing with `--omit=optional` is not supported because it removes the native
payload required by the launcher.
