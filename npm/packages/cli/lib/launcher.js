"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const PACKAGE_BY_TARGET = Object.freeze({
  "darwin-arm64": "@api429/cli-darwin-arm64",
  "darwin-x64": "@api429/cli-darwin-x64",
  "linux-arm64-gnu": "@api429/cli-linux-arm64-gnu",
  "linux-x64-gnu": "@api429/cli-linux-x64-gnu",
  "win32-arm64": "@api429/cli-win32-arm64",
  "win32-x64": "@api429/cli-win32-x64",
});

const SUPPORTED_TARGETS =
  "macOS, glibc-based Linux, and Windows on x64 or arm64";

class LauncherError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "LauncherError";
  }
}

function detectLinuxLibc(report = process.report) {
  try {
    if (!report || typeof report.getReport !== "function") {
      return "unknown";
    }

    const diagnosticReport = report.getReport();
    if (diagnosticReport?.header?.glibcVersionRuntime) {
      return "glibc";
    }

    const sharedObjects = diagnosticReport?.sharedObjects;
    if (
      Array.isArray(sharedObjects) &&
      sharedObjects.some((entry) => /(?:^|[/\\])(?:ld-)?musl|libc\.musl/i.test(entry))
    ) {
      return "musl";
    }
  } catch {
    // A restricted runtime may disable process reports. Package resolution is
    // still authoritative because npm also filters optional dependencies by libc.
  }

  return "unknown";
}

function selectTarget({ platform, arch, libc = "unknown" }) {
  if (platform === "linux" && libc === "musl") {
    throw new LauncherError(
      "unsupported Linux C library: musl. The current API429 CLI binaries require glibc.",
    );
  }

  const target = platform === "linux" ? `${platform}-${arch}-gnu` : `${platform}-${arch}`;
  const packageName = PACKAGE_BY_TARGET[target];
  if (!packageName) {
    throw new LauncherError(
      `unsupported platform: ${platform}-${arch}. Supported targets: ${SUPPORTED_TARGETS}.`,
    );
  }

  return {
    binaryName: platform === "win32" ? "api429.exe" : "api429",
    packageName,
    target,
  };
}

function resolveBinary(
  target,
  { resolvePackageJson = require.resolve, statSync = fs.statSync } = {},
) {
  let packageJsonPath;
  try {
    packageJsonPath = resolvePackageJson(`${target.packageName}/package.json`);
  } catch (error) {
    throw new LauncherError(
      [
        `required optional package ${target.packageName} is not installed.`,
        "Reinstall @api429/cli without --omit=optional",
        "(for example: npm install --include=optional --global @api429/cli).",
      ].join(" "),
      { cause: error },
    );
  }

  const binaryPath = path.join(
    path.dirname(packageJsonPath),
    "bin",
    target.binaryName,
  );

  try {
    if (!statSync(binaryPath).isFile()) {
      throw new Error("payload is not a regular file");
    }
  } catch (error) {
    throw new LauncherError(
      `native executable is missing from ${target.packageName} at ${binaryPath}. ` +
        "The installation may be incomplete or corrupt; reinstall @api429/cli.",
      { cause: error },
    );
  }

  return binaryPath;
}

function signalExitCode(signal) {
  const number = os.constants.signals?.[signal];
  return Number.isInteger(number) ? 128 + number : 1;
}

function launch(options = {}) {
  const processLike = options.processLike ?? process;
  const platform = options.platform ?? processLike.platform;
  const arch = options.arch ?? processLike.arch;
  const stderr = options.stderr ?? processLike.stderr;
  const argv = options.argv ?? processLike.argv.slice(2);
  const libc =
    options.libc ?? (platform === "linux" ? detectLinuxLibc(options.report) : "unknown");

  let target;
  let binaryPath;
  try {
    target = selectTarget({ platform, arch, libc });
    binaryPath = resolveBinary(target, {
      resolvePackageJson: options.resolvePackageJson,
      statSync: options.statSync,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    stderr.write(`api429: ${message}\n`);
    processLike.exitCode = 1;
    return null;
  }

  const spawnImpl = options.spawnImpl ?? spawn;
  let child;
  try {
    child = spawnImpl(binaryPath, argv, {
      shell: false,
      stdio: "inherit",
      windowsHide: false,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    stderr.write(`api429: failed to start ${binaryPath}: ${message}\n`);
    processLike.exitCode = 1;
    return null;
  }

  let settled = false;
  const signalHandlers = new Map();
  const signals = platform === "win32" ? ["SIGINT", "SIGTERM"] : ["SIGHUP", "SIGINT", "SIGTERM"];

  if (
    typeof processLike.on === "function" &&
    typeof processLike.removeListener === "function" &&
    typeof child.kill === "function"
  ) {
    for (const signal of signals) {
      const handler = () => {
        try {
          child.kill(signal);
        } catch {
          // The child may already have exited between signal delivery and kill.
        }
      };
      try {
        processLike.on(signal, handler);
        signalHandlers.set(signal, handler);
      } catch {
        // Some platforms do not expose every POSIX signal.
      }
    }
  }

  const cleanup = () => {
    for (const [signal, handler] of signalHandlers) {
      processLike.removeListener(signal, handler);
    }
    signalHandlers.clear();
  };

  child.once("error", (error) => {
    if (settled) return;
    settled = true;
    cleanup();
    const message = error instanceof Error ? error.message : String(error);
    stderr.write(`api429: failed to start ${binaryPath}: ${message}\n`);
    processLike.exitCode = 1;
  });

  child.once("exit", (code, signal) => {
    if (settled) return;
    settled = true;
    cleanup();

    if (signal) {
      try {
        processLike.kill(processLike.pid, signal);
      } catch {
        processLike.exitCode = signalExitCode(signal);
      }
      return;
    }

    processLike.exitCode = Number.isInteger(code) ? code : 1;
  });

  return child;
}

module.exports = {
  LauncherError,
  PACKAGE_BY_TARGET,
  detectLinuxLibc,
  launch,
  resolveBinary,
  selectTarget,
  signalExitCode,
};
