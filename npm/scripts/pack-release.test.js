"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const { expectedTarballFilename, isInside } = require("./pack-release.js");
const {
  loadLegalCorpus,
  platforms,
  validatePayload,
} = require("./validate-packages.js");

function writeMachO(file, arch) {
  const value = Buffer.alloc(128);
  value.set([0xcf, 0xfa, 0xed, 0xfe], 0);
  value.writeUInt32LE(arch === "x64" ? 0x01000007 : 0x0100000c, 4);
  value.writeUInt32LE(2, 12);
  value.writeUInt32LE(2, 16);
  value.writeUInt32LE(96, 20);
  value.writeUInt32LE(0x19, 32);
  value.writeUInt32LE(72, 36);
  value.write("__TEXT", 40, "ascii");
  value.writeBigUInt64LE(0x100000000n, 56);
  value.writeBigUInt64LE(0x1000n, 64);
  value.writeBigUInt64LE(0n, 72);
  value.writeBigUInt64LE(BigInt(value.length), 80);
  value.writeUInt32LE(5, 88);
  value.writeUInt32LE(5, 92);
  value.writeUInt32LE(0x80000028, 104);
  value.writeUInt32LE(24, 108);
  value.writeBigUInt64LE(120n, 112);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, value, { mode: 0o755 });
}

function writeElf(file, arch) {
  const interpreter = Buffer.from(
    arch === "x64"
      ? "/lib64/ld-linux-x86-64.so.2\0"
      : "/lib/ld-linux-aarch64.so.1\0",
  );
  const value = Buffer.alloc(232 + interpreter.length);
  value.set([0x7f, 0x45, 0x4c, 0x46, 2, 1, 1], 0);
  value.writeUInt16LE(3, 16);
  value.writeUInt16LE(arch === "x64" ? 0x3e : 0xb7, 18);
  value.writeUInt32LE(1, 20);
  value.writeBigUInt64LE(0x1000n, 24);
  value.writeBigUInt64LE(64n, 32);
  value.writeUInt16LE(64, 52);
  value.writeUInt16LE(56, 54);
  value.writeUInt16LE(3, 56);
  value.writeUInt32LE(6, 64);
  value.writeUInt32LE(3, 120);
  value.writeBigUInt64LE(232n, 128);
  value.writeBigUInt64LE(BigInt(interpreter.length), 152);
  value.writeUInt32LE(1, 176);
  value.writeUInt32LE(5, 180);
  value.writeBigUInt64LE(0x1000n, 192);
  value.writeBigUInt64LE(BigInt(value.length), 208);
  value.writeBigUInt64LE(BigInt(value.length), 216);
  interpreter.copy(value, 232);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, value, { mode: 0o755 });
}

function writePe(file, arch) {
  const value = Buffer.alloc(280);
  value.set([0x4d, 0x5a], 0);
  value.writeUInt32LE(64, 0x3c);
  value.set([0x50, 0x45, 0x00, 0x00], 64);
  value.writeUInt16LE(arch === "x64" ? 0x8664 : 0xaa64, 68);
  value.writeUInt16LE(1, 70);
  value.writeUInt16LE(112, 84);
  value.writeUInt16LE(0x0002, 86);
  value.writeUInt16LE(0x020b, 88);
  value.writeUInt32LE(0x1000, 104);
  value.write(".text", 200, "ascii");
  value.writeUInt32LE(40, 208);
  value.writeUInt32LE(0x1000, 212);
  value.writeUInt32LE(40, 216);
  value.writeUInt32LE(240, 220);
  value.writeUInt32LE(0x60000020, 236);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, value);
}

test("release helper derives safe scoped tarball names and detects nested paths", () => {
  assert.equal(
    expectedTarballFilename("@api429/cli-linux-x64-gnu", "0.1.0"),
    "api429-cli-linux-x64-gnu-0.1.0.tgz",
  );
  assert.equal(isInside(path.join("a", "package"), path.join("a", "package", "dist")), true);
  assert.equal(isInside(path.join("a", "package"), path.join("a", "release")), false);
});

test("native validation rejects header-only Mach-O, ELF, and PE stubs", () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "api429-invalid-payload-"));
  try {
    const machO = path.join(temporaryRoot, "api429-macho");
    const machHeader = Buffer.alloc(32);
    machHeader.set([0xcf, 0xfa, 0xed, 0xfe], 0);
    machHeader.writeUInt32LE(0x0100000c, 4);
    fs.writeFileSync(machO, machHeader, { mode: 0o755 });
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-darwin-arm64"), machO),
      /MH_EXECUTE/,
    );

    const elf = path.join(temporaryRoot, "api429-elf");
    const elfHeader = Buffer.alloc(64);
    elfHeader.set([0x7f, 0x45, 0x4c, 0x46, 2, 1, 1], 0);
    elfHeader.writeUInt16LE(0x3e, 18);
    fs.writeFileSync(elf, elfHeader, { mode: 0o755 });
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-linux-x64-gnu"), elf),
      /ET_EXEC or ET_DYN/,
    );

    const pe = path.join(temporaryRoot, "api429.exe");
    const peHeader = Buffer.alloc(240);
    peHeader.set([0x4d, 0x5a], 0);
    peHeader.writeUInt32LE(64, 0x3c);
    peHeader.set([0x50, 0x45, 0x00, 0x00], 64);
    peHeader.writeUInt16LE(0x8664, 68);
    peHeader.writeUInt16LE(1, 70);
    peHeader.writeUInt16LE(112, 84);
    fs.writeFileSync(pe, peHeader);
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-win32-x64"), pe),
      /PE executable image/,
    );
  } finally {
    fs.rmSync(temporaryRoot, { force: true, recursive: true });
  }
});

test("native validation requires a file-backed executable entry point", () => {
  const temporaryRoot = fs.mkdtempSync(path.join(os.tmpdir(), "api429-invalid-entry-"));
  try {
    const machO = path.join(temporaryRoot, "api429-macho");
    writeMachO(machO, "arm64");
    const machPayload = fs.readFileSync(machO);
    machPayload.writeUInt32LE(0x24, 104);
    fs.writeFileSync(machO, machPayload, { mode: 0o755 });
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-darwin-arm64"), machO),
      /no LC_MAIN or LC_UNIXTHREAD/,
    );

    const elf = path.join(temporaryRoot, "api429-elf");
    writeElf(elf, "x64");
    const elfPayload = fs.readFileSync(elf);
    elfPayload.writeUInt32LE(4, 180);
    fs.writeFileSync(elf, elfPayload, { mode: 0o755 });
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-linux-x64-gnu"), elf),
      /entry point is not in a file-backed executable segment/,
    );

    const pe = path.join(temporaryRoot, "api429.exe");
    writePe(pe, "x64");
    const pePayload = fs.readFileSync(pe);
    pePayload.writeUInt32LE(0x40000020, 236);
    fs.writeFileSync(pe, pePayload);
    assert.throws(
      () => validatePayload(platforms.find(({ name }) => name === "@api429/cli-win32-x64"), pe),
      /entry point is not in a file-backed executable section/,
    );
  } finally {
    fs.rmSync(temporaryRoot, { force: true, recursive: true });
  }
});

test(
  "pack-release creates six platform tarballs before the meta tarball and emits checksums",
  { skip: process.platform === "win32", timeout: 30_000 },
  () => {
    const temporaryBase = fs.mkdtempSync(path.join(os.tmpdir(), "api429-pack-release-"));
    try {
      const sourceRoot = path.resolve(__dirname, "..");
      const npmRoot = path.join(temporaryBase, "npm");
      fs.cpSync(sourceRoot, npmRoot, {
        filter(source) {
          return path.basename(source) !== "node_modules" && !source.endsWith(".tgz");
        },
        recursive: true,
      });
      fs.writeFileSync(
        path.join(temporaryBase, "pyproject.toml"),
        '[project]\nname = "api429-cli"\nversion = "0.1.0"\nlicense = "MIT"\n',
      );

      const legalCorpus = loadLegalCorpus();
      for (const record of legalCorpus) {
        const repositoryDestination = path.join(temporaryBase, ...record.path.split("/"));
        fs.mkdirSync(path.dirname(repositoryDestination), { recursive: true });
        fs.copyFileSync(record.source, repositoryDestination);
        for (const platform of platforms) {
          const packageDestination = path.join(
            npmRoot,
            "packages",
            platform.name.slice("@api429/".length),
            ...record.path.split("/"),
          );
          fs.mkdirSync(path.dirname(packageDestination), { recursive: true });
          fs.copyFileSync(record.source, packageDestination);
        }
      }

      writeMachO(path.join(npmRoot, "packages/cli-darwin-arm64/bin/api429"), "arm64");
      writeMachO(path.join(npmRoot, "packages/cli-darwin-x64/bin/api429"), "x64");
      writeElf(path.join(npmRoot, "packages/cli-linux-arm64-gnu/bin/api429"), "arm64");
      writeElf(path.join(npmRoot, "packages/cli-linux-x64-gnu/bin/api429"), "x64");
      writePe(path.join(npmRoot, "packages/cli-win32-arm64/bin/api429.exe"), "arm64");
      writePe(path.join(npmRoot, "packages/cli-win32-x64/bin/api429.exe"), "x64");

      const outputDirectory = path.join(temporaryBase, "release");
      const npm = process.platform === "win32" ? "npm.cmd" : "npm";
      const result = spawnSync(
        npm,
        ["run", "--silent", "pack:release", "--", outputDirectory],
        { cwd: npmRoot, encoding: "utf8" },
      );
      assert.equal(result.status, 0, result.stderr || result.stdout);
      const manifest = JSON.parse(result.stdout);
      assert.equal(manifest.schemaVersion, 1);
      assert.equal(manifest.version, "0.1.0");
      assert.equal(manifest.packages.length, 7);
      assert.equal(manifest.packages.at(-1).name, "@api429/cli");
      assert.equal(manifest.packages.slice(0, -1).every(({ name }) => name !== "@api429/cli"), true);

      for (const entry of manifest.packages) {
        assert.equal(path.dirname(entry.tarball), ".");
        const tarball = path.join(outputDirectory, entry.tarball);
        assert.equal(fs.existsSync(tarball), true);
        const digest = crypto
          .createHash("sha512")
          .update(fs.readFileSync(tarball))
          .digest("hex");
        assert.equal(entry.sha512, digest);
      }
    } finally {
      fs.rmSync(temporaryBase, { force: true, recursive: true });
    }
  },
);
