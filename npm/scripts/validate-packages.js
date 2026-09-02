"use strict";

const fs = require("node:fs");
const path = require("node:path");

const { PACKAGE_BY_TARGET, selectTarget } = require("../packages/cli/lib/launcher.js");

const workspaceRoot = path.resolve(__dirname, "..");
const version = "0.1.0";
const platforms = [
  { arch: "arm64", binary: "api429", libc: undefined, name: "@api429/cli-darwin-arm64", os: "darwin" },
  { arch: "x64", binary: "api429", libc: undefined, name: "@api429/cli-darwin-x64", os: "darwin" },
  { arch: "arm64", binary: "api429", libc: "glibc", name: "@api429/cli-linux-arm64-gnu", os: "linux" },
  { arch: "x64", binary: "api429", libc: "glibc", name: "@api429/cli-linux-x64-gnu", os: "linux" },
  { arch: "arm64", binary: "api429.exe", libc: undefined, name: "@api429/cli-win32-arm64", os: "win32" },
  { arch: "x64", binary: "api429.exe", libc: undefined, name: "@api429/cli-win32-x64", os: "win32" },
];

function packageDirectory(packageName) {
  return path.join(workspaceRoot, "packages", packageName.slice("@api429/".length));
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function equal(actual, expected, message) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function validatePayload(entry, payload) {
  const metadata = fs.lstatSync(payload);
  if (!metadata.isFile()) {
    throw new Error(`${payload} must be a regular file, not a symlink or directory`);
  }
  if (entry.os !== "win32" && (metadata.mode & 0o777) !== 0o755) {
    throw new Error(`${payload} must have POSIX mode 0755`);
  }

  const descriptor = fs.openSync(payload, "r");
  try {
    const header = Buffer.alloc(Math.min(Math.max(metadata.size, 64), 4096));
    const bytesRead = fs.readSync(descriptor, header, 0, header.length, 0);
    if (bytesRead < 8) throw new Error(`${payload} is too small to be a native executable`);

    if (entry.os === "linux") {
      if (bytesRead < 20 || !header.subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46]))) {
        throw new Error(`${payload} is not an ELF executable`);
      }
      if (header[4] !== 2 || header[5] !== 1) {
        throw new Error(`${payload} must be a 64-bit little-endian ELF executable`);
      }
      const elfType = header.readUInt16LE(16);
      if (elfType !== 2 && elfType !== 3) {
        throw new Error(`${payload} ELF type must be ET_EXEC or ET_DYN`);
      }
      const expectedMachine = entry.arch === "x64" ? 0x3e : 0xb7;
      if (header.readUInt16LE(18) !== expectedMachine) {
        throw new Error(`${payload} ELF architecture does not match ${entry.arch}`);
      }
      if (
        header.readUInt32LE(20) !== 1 ||
        header.readBigUInt64LE(24) === 0n ||
        header.readUInt16LE(52) !== 64
      ) {
        throw new Error(`${payload} has an invalid ELF executable header`);
      }

      const programHeaderOffset = Number(header.readBigUInt64LE(32));
      const programHeaderSize = header.readUInt16LE(54);
      const programHeaderCount = header.readUInt16LE(56);
      if (
        !Number.isSafeInteger(programHeaderOffset) ||
        programHeaderOffset < 64 ||
        programHeaderSize < 56 ||
        programHeaderSize > 4096 ||
        programHeaderCount === 0 ||
        programHeaderCount > 1024
      ) {
        throw new Error(`${payload} has an invalid ELF program-header table`);
      }
      let interpreter;
      let hasLoadSegment = false;
      for (let index = 0; index < programHeaderCount; index += 1) {
        const programHeader = Buffer.alloc(programHeaderSize);
        const offset = programHeaderOffset + index * programHeaderSize;
        if (
          fs.readSync(descriptor, programHeader, 0, programHeader.length, offset) !==
          programHeader.length
        ) {
          throw new Error(`${payload} has a truncated ELF program-header table`);
        }
        const programHeaderType = programHeader.readUInt32LE(0);
        if (programHeaderType === 1) hasLoadSegment = true;
        if (programHeaderType !== 3) continue;
        const interpreterOffset = Number(programHeader.readBigUInt64LE(8));
        const interpreterSize = Number(programHeader.readBigUInt64LE(32));
        if (
          !Number.isSafeInteger(interpreterOffset) ||
          !Number.isSafeInteger(interpreterSize) ||
          interpreterSize < 2 ||
          interpreterSize > 4096
        ) {
          throw new Error(`${payload} has an invalid ELF interpreter record`);
        }
        const value = Buffer.alloc(interpreterSize);
        if (
          fs.readSync(descriptor, value, 0, value.length, interpreterOffset) !==
          value.length
        ) {
          throw new Error(`${payload} has a truncated ELF interpreter record`);
        }
        interpreter = value.toString("utf8").replace(/\0.*$/s, "");
      }
      if (!hasLoadSegment) {
        throw new Error(`${payload} ELF executable has no loadable segment`);
      }
      if (!interpreter || !/(?:^|\/)ld-linux[^/]*\.so(?:\.|$)/.test(interpreter)) {
        throw new Error(`${payload} does not declare a glibc dynamic loader`);
      }
      return;
    }

    if (entry.os === "darwin") {
      const magic = header.subarray(0, 4).toString("hex");
      let readUInt32;
      if (magic === "cffaedfe") {
        readUInt32 = (buffer, offset) => buffer.readUInt32LE(offset);
      } else if (magic === "feedfacf") {
        readUInt32 = (buffer, offset) => buffer.readUInt32BE(offset);
      } else {
        throw new Error(`${payload} is not a thin 64-bit Mach-O executable`);
      }
      const cpuType = readUInt32(header, 4);
      const expectedCpuType = entry.arch === "x64" ? 0x01000007 : 0x0100000c;
      if (cpuType !== expectedCpuType) {
        throw new Error(`${payload} Mach-O architecture does not match ${entry.arch}`);
      }
      if (readUInt32(header, 12) !== 2) {
        throw new Error(`${payload} Mach-O filetype must be MH_EXECUTE`);
      }
      const commandCount = readUInt32(header, 16);
      const commandBytes = readUInt32(header, 20);
      if (
        commandCount === 0 ||
        commandCount > 4096 ||
        commandBytes < commandCount * 8 ||
        metadata.size < 32 + commandBytes
      ) {
        throw new Error(`${payload} has an invalid Mach-O load-command table`);
      }
      let commandOffset = 32;
      for (let index = 0; index < commandCount; index += 1) {
        const loadCommand = Buffer.alloc(8);
        if (fs.readSync(descriptor, loadCommand, 0, 8, commandOffset) !== 8) {
          throw new Error(`${payload} has a truncated Mach-O load command`);
        }
        const size = readUInt32(loadCommand, 4);
        if (size < 8 || size % 8 !== 0 || commandOffset + size > 32 + commandBytes) {
          throw new Error(`${payload} has an invalid Mach-O load command`);
        }
        commandOffset += size;
      }
      if (commandOffset !== 32 + commandBytes) {
        throw new Error(`${payload} Mach-O load-command sizes are inconsistent`);
      }
      return;
    }

    if (header[0] !== 0x4d || header[1] !== 0x5a || bytesRead < 64) {
      throw new Error(`${payload} is not a PE executable`);
    }
    const peOffset = header.readUInt32LE(0x3c);
    const peHeader = Buffer.alloc(24);
    if (fs.readSync(descriptor, peHeader, 0, peHeader.length, peOffset) !== peHeader.length) {
      throw new Error(`${payload} has a truncated PE header`);
    }
    if (!peHeader.subarray(0, 4).equals(Buffer.from([0x50, 0x45, 0x00, 0x00]))) {
      throw new Error(`${payload} has an invalid PE signature`);
    }
    const expectedMachine = entry.arch === "x64" ? 0x8664 : 0xaa64;
    if (peHeader.readUInt16LE(4) !== expectedMachine) {
      throw new Error(`${payload} PE architecture does not match ${entry.arch}`);
    }
    const sectionCount = peHeader.readUInt16LE(6);
    const optionalHeaderSize = peHeader.readUInt16LE(20);
    const characteristics = peHeader.readUInt16LE(22);
    if (sectionCount === 0 || sectionCount > 96) {
      throw new Error(`${payload} has an invalid PE section count`);
    }
    if ((characteristics & 0x0002) === 0 || (characteristics & 0x2000) !== 0) {
      throw new Error(`${payload} must be a PE executable image, not a DLL`);
    }
    if (optionalHeaderSize < 112 || optionalHeaderSize > 4096) {
      throw new Error(`${payload} has an invalid PE optional header size`);
    }
    const optionalHeader = Buffer.alloc(optionalHeaderSize);
    if (
      fs.readSync(descriptor, optionalHeader, 0, optionalHeader.length, peOffset + 24) !==
      optionalHeader.length
    ) {
      throw new Error(`${payload} has a truncated PE optional header`);
    }
    if (optionalHeader.readUInt16LE(0) !== 0x020b || optionalHeader.readUInt32LE(16) === 0) {
      throw new Error(`${payload} must be a PE32+ executable with an entry point`);
    }
    const sectionTableSize = sectionCount * 40;
    const sectionTable = Buffer.alloc(sectionTableSize);
    if (
      fs.readSync(
        descriptor,
        sectionTable,
        0,
        sectionTable.length,
        peOffset + 24 + optionalHeaderSize,
      ) !== sectionTable.length
    ) {
      throw new Error(`${payload} has a truncated PE section table`);
    }
  } finally {
    fs.closeSync(descriptor);
  }
}

function validate({ quiet = false, requirePayload = false } = {}) {
  const log = quiet ? () => {} : console.log;
  const rootManifest = readJson(path.join(workspaceRoot, "package.json"));
  if (!rootManifest.private) throw new Error("npm workspace root must remain private");
  equal(rootManifest.workspaces, ["packages/cli"], "root workspaces");
  if (Object.hasOwn(rootManifest, "license")) {
    throw new Error("license must not be guessed in the private workspace manifest");
  }

  const pyproject = fs.readFileSync(path.resolve(workspaceRoot, "..", "pyproject.toml"), "utf8");
  const projectSection = pyproject.split("[project]", 2)[1]?.split(/\n\[/, 1)[0];
  const pythonVersion = projectSection?.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
  equal(pythonVersion, version, "Python and npm release versions");

  const cliDirectory = packageDirectory("@api429/cli");
  const cliManifest = readJson(path.join(cliDirectory, "package.json"));
  equal(cliManifest.version, version, "@api429/cli version");
  equal(cliManifest.bin, { api429: "bin/api429.js" }, "@api429/cli bin mapping");
  equal(
    cliManifest.files,
    ["bin/api429.js", "lib/launcher.js", "README.md"],
    "@api429/cli files",
  );
  equal(
    cliManifest.publishConfig,
    { access: "public", registry: "https://registry.npmjs.org/" },
    "@api429/cli publishConfig",
  );
  if (Object.hasOwn(cliManifest, "license")) {
    throw new Error("license must not be guessed in @api429/cli");
  }
  if (cliManifest.scripts?.postinstall) {
    throw new Error("@api429/cli must not download a binary from postinstall");
  }

  const expectedDependencies = Object.fromEntries(
    platforms.map(({ name }) => [name, version]).sort(([left], [right]) => left.localeCompare(right)),
  );
  equal(cliManifest.optionalDependencies, expectedDependencies, "exact optionalDependencies");
  const expectedLocalDependencies = Object.fromEntries(
    platforms
      .map(({ name }) => [name, `file:packages/${name.slice("@api429/".length)}`])
      .sort(([left], [right]) => left.localeCompare(right)),
  );
  equal(
    rootManifest.optionalDependencies,
    expectedLocalDependencies,
    "private workspace local optionalDependencies",
  );

  const expectedDirectories = new Set(["cli", ...platforms.map(({ name }) => name.slice("@api429/".length))]);
  const actualDirectories = fs
    .readdirSync(path.join(workspaceRoot, "packages"), { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name);
  for (const directory of actualDirectories) {
    if (!expectedDirectories.has(directory)) {
      throw new Error(`unexpected npm workspace package: ${directory}`);
    }
  }

  for (const entry of platforms) {
    const directory = packageDirectory(entry.name);
    const manifest = readJson(path.join(directory, "package.json"));
    equal(manifest.name, entry.name, `${entry.name} name`);
    equal(manifest.version, version, `${entry.name} version`);
    equal(manifest.os, [entry.os], `${entry.name} os`);
    equal(manifest.cpu, [entry.arch], `${entry.name} cpu`);
    equal(
      manifest.publishConfig,
      { access: "public", registry: "https://registry.npmjs.org/" },
      `${entry.name} publishConfig`,
    );
    equal(manifest.files, [`bin/${entry.binary}`], `${entry.name} payload allowlist`);
    equal(manifest.preferUnplugged, true, `${entry.name} preferUnplugged`);
    if (entry.libc) {
      equal(manifest.libc, [entry.libc], `${entry.name} libc`);
    } else if (Object.hasOwn(manifest, "libc")) {
      throw new Error(`${entry.name} must not declare libc`);
    }
    if (Object.hasOwn(manifest, "license")) {
      throw new Error(`license must not be guessed in ${entry.name}`);
    }
    if (manifest.scripts?.postinstall) {
      throw new Error(`${entry.name} must not have a postinstall download`);
    }

    const selected = selectTarget({
      arch: entry.arch,
      libc: entry.libc ?? "unknown",
      platform: entry.os,
    });
    equal(selected.packageName, entry.name, `${entry.name} launcher mapping`);
    equal(PACKAGE_BY_TARGET[selected.target], entry.name, `${entry.name} target mapping`);

    const payload = path.join(directory, "bin", entry.binary);
    if (fs.existsSync(payload)) {
      validatePayload(entry, payload);
      log(`validated payload: ${path.relative(workspaceRoot, payload)}`);
    } else if (requirePayload) {
      throw new Error(`required release payload is missing: ${payload}`);
    } else {
      log(`payload pending release staging: ${path.relative(workspaceRoot, payload)}`);
    }
  }

  log("validated 1 launcher package and 6 platform packages");
}

if (require.main === module) {
  try {
    validate();
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  }
}

module.exports = {
  packageDirectory,
  platforms,
  validate,
  validatePayload,
  workspaceRoot,
};
