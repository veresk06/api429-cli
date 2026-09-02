"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const { PACKAGE_BY_TARGET, selectTarget } = require("../packages/cli/lib/launcher.js");

const workspaceRoot = path.resolve(__dirname, "..");
const repositoryRoot = path.resolve(workspaceRoot, "..");
const version = "0.1.0";
const platformLegalPatterns = ["LICENSE", "THIRD_PARTY_NOTICES.md", "licenses/**"];
const requiredLegalPaths = [
  "LICENSE",
  "THIRD_PARTY_NOTICES.md",
  "licenses/BLAKE2-CC0-1.0.txt",
  "licenses/CPython-3.13.13-Windows.txt",
  "licenses/CPython-3.13.13.txt",
  "licenses/HACL-MIT.txt",
  "licenses/MPL-2.0.txt",
  "licenses/OpenSSL-3.txt",
  "licenses/PyInstaller-6.22.2.txt",
  "licenses/PyInstaller-Hooks-Contrib-2026.7.txt",
  "licenses/SQLite.txt",
  "licenses/anyio-4.14.2.txt",
  "licenses/bzip2.txt",
  "licenses/certifi-2026.7.22.txt",
  "licenses/expat.txt",
  "licenses/h11-0.16.0.txt",
  "licenses/httpcore-1.0.9.txt",
  "licenses/httpx-0.28.1.txt",
  "licenses/idna-3.19.txt",
  "licenses/libffi.txt",
  "licenses/liblzma.txt",
  "licenses/libuuid.txt",
  "licenses/mpdecimal.txt",
  "licenses/zlib.txt",
];
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

function sha256(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function assertRegularFile(file, label = file) {
  const metadata = fs.lstatSync(file);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new Error(`${label} must be a regular file, not a symlink or directory`);
  }
  return metadata;
}

function pathEntryExists(file) {
  try {
    fs.lstatSync(file);
    return true;
  } catch (error) {
    if (error && error.code === "ENOENT") return false;
    throw error;
  }
}

function loadLegalCorpus() {
  const manifestPath = path.join(repositoryRoot, "licenses", "manifest.json");
  assertRegularFile(manifestPath, "legal manifest");
  const manifest = readJson(manifestPath);
  equal(Object.keys(manifest).sort(), ["files", "schema_version"], "legal manifest keys");
  equal(manifest.schema_version, 1, "legal manifest schema");
  if (!Array.isArray(manifest.files) || manifest.files.length < 3) {
    throw new Error("legal manifest must contain project and third-party files");
  }

  const seen = new Set();
  const records = manifest.files.map((record) => {
    if (!record || typeof record !== "object" || Array.isArray(record)) {
      throw new Error("legal manifest entries must be objects");
    }
    equal(Object.keys(record).sort(), ["path", "sha256"], "legal manifest entry keys");
    const relative = record.path;
    const isProjectFile = relative === "LICENSE" || relative === "THIRD_PARTY_NOTICES.md";
    const isLicenseText =
      typeof relative === "string" && /^licenses\/[A-Za-z0-9][A-Za-z0-9._+-]*\.txt$/.test(relative);
    if (!isProjectFile && !isLicenseText) {
      throw new Error(`unsafe or unsupported legal corpus path: ${JSON.stringify(relative)}`);
    }
    if (seen.has(relative)) throw new Error(`duplicate legal corpus path: ${relative}`);
    seen.add(relative);
    if (typeof record.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(record.sha256)) {
      throw new Error(`invalid SHA-256 for ${relative}`);
    }
    const source = path.join(repositoryRoot, ...relative.split("/"));
    const metadata = assertRegularFile(source, `legal corpus entry ${relative}`);
    if (metadata.size < 1 || metadata.size > 2 * 1024 * 1024) {
      throw new Error(`legal corpus entry ${relative} has an invalid size`);
    }
    if (sha256(source) !== record.sha256) {
      throw new Error(`legal corpus SHA-256 mismatch for ${relative}`);
    }
    return { path: relative, sha256: record.sha256, size: metadata.size, source };
  });
  const listedPaths = records.map((record) => record.path);
  const expectedOrder = [
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    ...listedPaths.filter((value) => value.startsWith("licenses/")).sort(),
  ];
  equal(listedPaths, expectedOrder, "legal manifest file order");
  const missingRequired = requiredLegalPaths.filter((value) => !seen.has(value));
  if (missingRequired.length > 0) {
    throw new Error(`legal manifest omits required runtime coverage: ${missingRequired.join(", ")}`);
  }
  const expectedDirectoryEntries = [
    "manifest.json",
    ...listedPaths
      .filter((value) => value.startsWith("licenses/"))
      .map((value) => path.basename(value)),
  ].sort();
  const actualDirectoryEntries = fs.readdirSync(path.dirname(manifestPath)).sort();
  equal(actualDirectoryEntries, expectedDirectoryEntries, "legal corpus directory entries");

  const manifestRecord = {
    path: "licenses/manifest.json",
    sha256: sha256(manifestPath),
    size: fs.statSync(manifestPath).size,
    source: manifestPath,
  };
  return [...records.slice(0, 2), manifestRecord, ...records.slice(2)];
}

function validateStagedLegalCorpus(directory, legalCorpus, required) {
  const destinations = legalCorpus.map((record) =>
    path.join(directory, ...record.path.split("/")),
  );
  const present = destinations.map(pathEntryExists);
  const presentCount = present.filter(Boolean).length;
  if (presentCount === 0 && !required) return;
  if (presentCount !== destinations.length) {
    throw new Error(`${directory} has an incomplete staged legal corpus`);
  }
  const expectedLicenseEntries = legalCorpus
    .filter((record) => record.path.startsWith("licenses/"))
    .map((record) => path.basename(record.path))
    .sort();
  const actualLicenseEntries = fs.readdirSync(path.join(directory, "licenses")).sort();
  equal(actualLicenseEntries, expectedLicenseEntries, `${directory} staged license entries`);
  for (let index = 0; index < destinations.length; index += 1) {
    const destination = destinations[index];
    const record = legalCorpus[index];
    assertRegularFile(destination, `staged legal corpus entry ${record.path}`);
    if (sha256(destination) !== record.sha256) {
      throw new Error(`staged legal corpus SHA-256 mismatch for ${record.path}`);
    }
  }
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
      const entryPoint = header.readBigUInt64LE(24);
      let interpreter;
      let hasLoadSegment = false;
      let entryInExecutableSegment = false;
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
        if (programHeaderType === 1) {
          hasLoadSegment = true;
          const flags = programHeader.readUInt32LE(4);
          const fileOffset = programHeader.readBigUInt64LE(8);
          const virtualAddress = programHeader.readBigUInt64LE(16);
          const fileSize = programHeader.readBigUInt64LE(32);
          const memorySize = programHeader.readBigUInt64LE(40);
          if (
            memorySize < fileSize ||
            fileOffset > BigInt(metadata.size) ||
            fileSize > BigInt(metadata.size) - fileOffset
          ) {
            throw new Error(`${payload} has an invalid ELF loadable segment`);
          }
          if (
            (flags & 0x1) !== 0 &&
            fileSize > 0n &&
            entryPoint >= virtualAddress &&
            entryPoint < virtualAddress + fileSize
          ) {
            entryInExecutableSegment = true;
          }
        }
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
      if (!entryInExecutableSegment) {
        throw new Error(`${payload} ELF entry point is not in a file-backed executable segment`);
      }
      if (!interpreter || !/(?:^|\/)ld-linux[^/]*\.so(?:\.|$)/.test(interpreter)) {
        throw new Error(`${payload} does not declare a glibc dynamic loader`);
      }
      return;
    }

    if (entry.os === "darwin") {
      const magic = header.subarray(0, 4).toString("hex");
      let readUInt32;
      let readBigUInt64;
      if (magic === "cffaedfe") {
        readUInt32 = (buffer, offset) => buffer.readUInt32LE(offset);
        readBigUInt64 = (buffer, offset) => buffer.readBigUInt64LE(offset);
      } else if (magic === "feedfacf") {
        readUInt32 = (buffer, offset) => buffer.readUInt32BE(offset);
        readBigUInt64 = (buffer, offset) => buffer.readBigUInt64BE(offset);
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
      let mainEntryOffset;
      let hasUnixThread = false;
      const executableFileRanges = [];
      for (let index = 0; index < commandCount; index += 1) {
        const loadCommand = Buffer.alloc(8);
        if (fs.readSync(descriptor, loadCommand, 0, 8, commandOffset) !== 8) {
          throw new Error(`${payload} has a truncated Mach-O load command`);
        }
        const command = readUInt32(loadCommand, 0);
        const size = readUInt32(loadCommand, 4);
        if (size < 8 || size % 8 !== 0 || commandOffset + size > 32 + commandBytes) {
          throw new Error(`${payload} has an invalid Mach-O load command`);
        }
        const commandBody = Buffer.alloc(size);
        if (fs.readSync(descriptor, commandBody, 0, size, commandOffset) !== size) {
          throw new Error(`${payload} has a truncated Mach-O load command`);
        }
        if (command === 0x19) {
          if (size < 72) {
            throw new Error(`${payload} has a truncated Mach-O LC_SEGMENT_64 command`);
          }
          const fileOffset = readBigUInt64(commandBody, 40);
          const fileSize = readBigUInt64(commandBody, 48);
          const initialProtection = readUInt32(commandBody, 60);
          if (
            fileOffset > BigInt(metadata.size) ||
            fileSize > BigInt(metadata.size) - fileOffset
          ) {
            throw new Error(`${payload} has an invalid Mach-O segment file range`);
          }
          if ((initialProtection & 0x4) !== 0 && fileSize > 0n) {
            executableFileRanges.push([fileOffset, fileOffset + fileSize]);
          }
        } else if (command === 0x80000028) {
          if (size < 24) {
            throw new Error(`${payload} has a truncated Mach-O LC_MAIN command`);
          }
          mainEntryOffset = readBigUInt64(commandBody, 8);
        } else if (command === 0x5) {
          if (size < 16) {
            throw new Error(`${payload} has a truncated Mach-O LC_UNIXTHREAD command`);
          }
          hasUnixThread = true;
        }
        commandOffset += size;
      }
      if (commandOffset !== 32 + commandBytes) {
        throw new Error(`${payload} Mach-O load-command sizes are inconsistent`);
      }
      if (executableFileRanges.length === 0) {
        throw new Error(`${payload} Mach-O executable has no file-backed executable segment`);
      }
      if (mainEntryOffset === undefined && !hasUnixThread) {
        throw new Error(`${payload} Mach-O executable has no LC_MAIN or LC_UNIXTHREAD entry point`);
      }
      if (
        mainEntryOffset !== undefined &&
        !executableFileRanges.some(
          ([start, end]) => mainEntryOffset >= start && mainEntryOffset < end,
        )
      ) {
        throw new Error(`${payload} Mach-O LC_MAIN entry point is not in an executable segment`);
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
    const entryRva = optionalHeader.readUInt32LE(16);
    if (optionalHeader.readUInt16LE(0) !== 0x020b || entryRva === 0) {
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
    let entryInExecutableSection = false;
    for (let index = 0; index < sectionCount; index += 1) {
      const offset = index * 40;
      const virtualSize = sectionTable.readUInt32LE(offset + 8);
      const virtualAddress = sectionTable.readUInt32LE(offset + 12);
      const rawSize = sectionTable.readUInt32LE(offset + 16);
      const rawOffset = sectionTable.readUInt32LE(offset + 20);
      const sectionCharacteristics = sectionTable.readUInt32LE(offset + 36);
      if ((sectionCharacteristics & 0x20000000) === 0) continue;
      if (
        rawSize === 0 ||
        rawOffset > metadata.size ||
        rawSize > metadata.size - rawOffset
      ) {
        throw new Error(`${payload} has an invalid PE executable section`);
      }
      const virtualSpan = Math.max(virtualSize, rawSize);
      if (
        entryRva >= virtualAddress &&
        entryRva - virtualAddress < virtualSpan &&
        entryRva - virtualAddress < rawSize
      ) {
        entryInExecutableSection = true;
      }
    }
    if (!entryInExecutableSection) {
      throw new Error(`${payload} PE entry point is not in a file-backed executable section`);
    }
  } finally {
    fs.closeSync(descriptor);
  }
}

function validate({ quiet = false, requirePayload = false } = {}) {
  const log = quiet ? () => {} : console.log;
  const legalCorpus = loadLegalCorpus();
  const rootManifest = readJson(path.join(workspaceRoot, "package.json"));
  if (!rootManifest.private) throw new Error("npm workspace root must remain private");
  equal(rootManifest.workspaces, ["packages/cli"], "root workspaces");
  equal(rootManifest.license, "MIT", "private workspace license");

  const lockfile = readJson(path.join(workspaceRoot, "package-lock.json"));
  equal(lockfile.lockfileVersion, 3, "npm lockfile version");
  equal(lockfile.packages?.[""]?.license, "MIT", "npm lockfile root license");
  equal(
    lockfile.packages?.["packages/cli"]?.license,
    "MIT",
    "npm lockfile @api429/cli license",
  );
  for (const { name } of platforms) {
    const packagePath = `packages/${name.slice("@api429/".length)}`;
    equal(
      lockfile.packages?.[packagePath]?.license,
      "MIT",
      `npm lockfile ${name} license`,
    );
  }

  const pyproject = fs.readFileSync(path.resolve(workspaceRoot, "..", "pyproject.toml"), "utf8");
  const projectSection = pyproject.split("[project]", 2)[1]?.split(/\n\[/, 1)[0];
  const pythonVersion = projectSection?.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
  equal(pythonVersion, version, "Python and npm release versions");

  const cliDirectory = packageDirectory("@api429/cli");
  const cliManifest = readJson(path.join(cliDirectory, "package.json"));
  equal(cliManifest.version, version, "@api429/cli version");
  equal(cliManifest.license, "MIT", "@api429/cli license");
  equal(cliManifest.bin, { api429: "bin/api429.js" }, "@api429/cli bin mapping");
  equal(
    cliManifest.files,
    [
      "bin/api429.js",
      "lib/launcher.js",
      "README.md",
      "LICENSE",
      "THIRD_PARTY_NOTICES.md",
    ],
    "@api429/cli files",
  );
  equal(
    cliManifest.publishConfig,
    { access: "public", registry: "https://registry.npmjs.org/" },
    "@api429/cli publishConfig",
  );
  const cliLicense = path.join(cliDirectory, "LICENSE");
  const cliNotice = path.join(cliDirectory, "THIRD_PARTY_NOTICES.md");
  assertRegularFile(cliLicense, "@api429/cli LICENSE");
  assertRegularFile(cliNotice, "@api429/cli THIRD_PARTY_NOTICES.md");
  if (sha256(cliLicense) !== sha256(path.join(repositoryRoot, "LICENSE"))) {
    throw new Error("@api429/cli LICENSE must match the project LICENSE exactly");
  }
  const cliNoticeText = fs.readFileSync(cliNotice, "utf8");
  if (!cliNoticeText.includes("@api429/cli-*") || !cliNoticeText.includes("licenses/")) {
    throw new Error("@api429/cli notice must point to platform-package legal files");
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
    equal(manifest.license, "MIT", `${entry.name} license`);
    equal(manifest.os, [entry.os], `${entry.name} os`);
    equal(manifest.cpu, [entry.arch], `${entry.name} cpu`);
    equal(
      manifest.publishConfig,
      { access: "public", registry: "https://registry.npmjs.org/" },
      `${entry.name} publishConfig`,
    );
    equal(
      manifest.files,
      [`bin/${entry.binary}`, ...platformLegalPatterns],
      `${entry.name} payload and legal allowlist`,
    );
    equal(manifest.preferUnplugged, true, `${entry.name} preferUnplugged`);
    if (entry.libc) {
      equal(manifest.libc, [entry.libc], `${entry.name} libc`);
    } else if (Object.hasOwn(manifest, "libc")) {
      throw new Error(`${entry.name} must not declare libc`);
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
    if (pathEntryExists(payload)) {
      validatePayload(entry, payload);
      validateStagedLegalCorpus(directory, legalCorpus, true);
      log(`validated payload: ${path.relative(workspaceRoot, payload)}`);
    } else if (requirePayload) {
      throw new Error(`required release payload is missing: ${payload}`);
    } else {
      validateStagedLegalCorpus(directory, legalCorpus, false);
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
  loadLegalCorpus,
  packageDirectory,
  platformLegalPatterns,
  platforms,
  validate,
  validatePayload,
  workspaceRoot,
};
